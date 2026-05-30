"""Tests for ``memory_hygiene`` — TTL + commit-based invalidation."""

from __future__ import annotations

from typing import Iterator

import pytest

import memory_hygiene as mh


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Strip any pre-existing BUILD_COMMIT / RAILWAY_GIT_COMMIT_SHA so
    individual tests start from a known empty state."""
    monkeypatch.delenv("BUILD_COMMIT", raising=False)
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    yield


_OPERATIONAL_ENTRY = """\
---
kind: transient_infra
valid_through_commit: abc123def
last_verified_at: 2026-05-15T03:43:28Z
status: operational
---

# Kapa REST tool status — operational

Live probe passed. Re-verify with bin/probe_kapa.py.
"""

_NON_TRANSIENT_ENTRY = """\
---
kind: methodology
title: GTM Audit Benchmarks
---

Benchmarks for win rate, NRR, etc.
"""


# ─────────────────────────────────────────────────────────────────────────────
# parse_frontmatter
# ─────────────────────────────────────────────────────────────────────────────


def test_parse_frontmatter_extracts_flat_keys() -> None:
    fields = mh.parse_frontmatter(_OPERATIONAL_ENTRY)
    assert fields["kind"] == "transient_infra"
    assert fields["valid_through_commit"] == "abc123def"
    assert fields["last_verified_at"] == "2026-05-15T03:43:28Z"
    assert fields["status"] == "operational"


def test_parse_frontmatter_returns_empty_on_no_delimiter() -> None:
    assert mh.parse_frontmatter("just body text, no frontmatter") == {}


def test_parse_frontmatter_returns_empty_on_unclosed_delimiter() -> None:
    text = "---\nkind: transient_infra\nvalid_through_commit: abc\n"
    # No closing ``---`` — malformed, must return empty.
    assert mh.parse_frontmatter(text) == {}


def test_parse_frontmatter_handles_empty_input() -> None:
    assert mh.parse_frontmatter("") == {}


# ─────────────────────────────────────────────────────────────────────────────
# is_stale
# ─────────────────────────────────────────────────────────────────────────────


def test_is_stale_matching_commit_not_stale() -> None:
    assert mh.is_stale(_OPERATIONAL_ENTRY, "abc123def") is False


def test_is_stale_differing_commit_is_stale() -> None:
    assert mh.is_stale(_OPERATIONAL_ENTRY, "ffffffff") is True


def test_is_stale_superseded_stamp_is_stale_regardless_of_commit() -> None:
    text = mh.mark_superseded(_OPERATIONAL_ENTRY, "deadbeef")
    # Even if the live BUILD_COMMIT matches valid_through_commit, the
    # explicit supersede stamp wins — the deploy that touched the
    # underlying contract is what made this entry untrustworthy.
    assert mh.is_stale(text, "abc123def") is True


def test_is_stale_non_transient_infra_never_stale_by_commit() -> None:
    assert mh.is_stale(_NON_TRANSIENT_ENTRY, "ffffffff") is False
    assert mh.is_stale(_NON_TRANSIENT_ENTRY, None) is False


def test_is_stale_missing_valid_through_commit_treated_as_stale() -> None:
    text = "---\nkind: transient_infra\nstatus: operational\n---\n\nbody"
    assert mh.is_stale(text, "abc123def") is True


def test_is_stale_unknown_build_commit_treated_as_stale() -> None:
    # Conservative behavior: if we don't know which build we're on,
    # don't trust a commit-stamped guarantee.
    assert mh.is_stale(_OPERATIONAL_ENTRY, None) is True
    assert mh.is_stale(_OPERATIONAL_ENTRY, "") is True


def test_is_stale_no_frontmatter_not_stale() -> None:
    assert mh.is_stale("just plain markdown\n", "abc123def") is False


# ─────────────────────────────────────────────────────────────────────────────
# mark_superseded
# ─────────────────────────────────────────────────────────────────────────────


def test_mark_superseded_appends_to_existing_frontmatter() -> None:
    out = mh.mark_superseded(_OPERATIONAL_ENTRY, "newcommit99")
    fields = mh.parse_frontmatter(out)
    assert fields["superseded_at_commit"] == "newcommit99"
    # Original fields preserved.
    assert fields["kind"] == "transient_infra"
    assert fields["valid_through_commit"] == "abc123def"
    assert fields["status"] == "operational"
    # Body preserved verbatim.
    assert "Kapa REST tool status" in out
    assert "Live probe passed" in out


def test_mark_superseded_idempotent_same_sha() -> None:
    out1 = mh.mark_superseded(_OPERATIONAL_ENTRY, "newcommit99")
    out2 = mh.mark_superseded(out1, "newcommit99")
    assert out1 == out2


def test_mark_superseded_updates_existing_stamp() -> None:
    out1 = mh.mark_superseded(_OPERATIONAL_ENTRY, "firstsupersede")
    out2 = mh.mark_superseded(out1, "secondsupersede")
    fields = mh.parse_frontmatter(out2)
    assert fields["superseded_at_commit"] == "secondsupersede"
    # Should only appear once in the frontmatter.
    assert out2.count("superseded_at_commit:") == 1


def test_mark_superseded_minimal_frontmatter() -> None:
    minimal = "---\nkind: transient_infra\n---\nbody"
    out = mh.mark_superseded(minimal, "abc")
    fields = mh.parse_frontmatter(out)
    assert fields["kind"] == "transient_infra"
    assert fields["superseded_at_commit"] == "abc"


def test_mark_superseded_no_frontmatter_synthesizes_one() -> None:
    out = mh.mark_superseded("plain body\n", "abc")
    fields = mh.parse_frontmatter(out)
    assert fields["superseded_at_commit"] == "abc"
    assert "plain body" in out


def test_mark_superseded_rejects_empty_sha() -> None:
    with pytest.raises(ValueError):
        mh.mark_superseded(_OPERATIONAL_ENTRY, "")


# ─────────────────────────────────────────────────────────────────────────────
# current_build_commit
# ─────────────────────────────────────────────────────────────────────────────


def test_current_build_commit_reads_build_commit(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BUILD_COMMIT", "deadbeef1234")
    assert mh.current_build_commit() == "deadbeef1234"


def test_current_build_commit_falls_back_to_railway_sha(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "railwaysha999")
    assert mh.current_build_commit() == "railwaysha999"


def test_current_build_commit_prefers_build_commit_over_railway(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BUILD_COMMIT", "primary")
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "fallback")
    assert mh.current_build_commit() == "primary"


def test_current_build_commit_returns_none_when_unset(clean_env: None) -> None:
    assert mh.current_build_commit() is None


def test_current_build_commit_treats_empty_string_as_unset(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BUILD_COMMIT", "")
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "   ")
    assert mh.current_build_commit() is None
