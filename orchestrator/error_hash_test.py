"""Tests for ``orchestrator/error_hash.py`` (shared dedup hash).

Mirrors the normalization coverage in bin/audit_error_categories_test.py
so any regression here surfaces in either suite.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "orchestrator"))

import error_hash  # noqa: E402


def test_first_line_caps_at_500_chars():
    long = "x" * 1000
    assert len(error_hash.first_line(long)) == 500


def test_first_line_handles_empty_and_none():
    assert error_hash.first_line(None) == ""
    assert error_hash.first_line("") == ""


def test_first_line_strips_after_newline():
    msg = "first line\nrest of stack trace\n  at frame 1"
    assert error_hash.first_line(msg) == "first line"


def test_normalize_strips_labeled_ids():
    msg = "request req_01HXYZ failed at msg_AbCdEf for event_99zz"
    out = error_hash.normalize(msg)
    assert "req_<ID>" in out
    assert "msg_<ID>" in out
    assert "event_<ID>" in out
    assert "req_01HXYZ" not in out


def test_normalize_strips_sesn_EXAMPLE():
    """Real Managed Agents IDs use ``sesn_`` prefix."""
    msg = "sesn_EXAMPLE exhausted token budget"
    out = error_hash.normalize(msg)
    assert "sesn_EXAMPLE" not in out
    assert "sesn_<ID>" in out


def test_normalize_uuid_before_sha_does_not_fragment():
    """SHA strip must not eat UUID hex groups individually."""
    a = "Session 019e6a5b-831e-7fe0-abb0-91ee7a97cfaa died"
    b = "Session aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee died"
    assert error_hash.compute(a) == error_hash.compute(b)


def test_normalize_strips_inv_ids():
    msg = "Lifecycle terminalize failed for inv_id=12345 (inv 67 retry)"
    out = error_hash.normalize(msg)
    assert "inv_id=<N>" in out
    assert "inv <N>" in out
    assert "12345" not in out


def test_normalize_strips_paths_and_shas():
    msg = "AttributeError at /Users/jb/repos/x/orchestrator/main.py line 4242 commit 595d717c7047ba"
    out = error_hash.normalize(msg)
    assert "<PATH>" in out
    assert "<SHA>" in out
    assert "/Users/jb" not in out
    assert "595d717c7047ba" not in out
    assert "4242" not in out


def test_normalize_strips_filename_timestamps_and_iso():
    msg = "Snapshot 20260521-153045 conflicts with 2026-05-21T15:30:45Z"
    out = error_hash.normalize(msg)
    assert "<FNTS>" in out
    assert "<ISO_TS>" in out
    assert "20260521-153045" not in out
    assert "2026-05-21T15:30:45" not in out


def test_normalize_strips_slack_channel_and_ts():
    msg = "Slack post failed in C09ABC1234 at 1716315045.123456"
    out = error_hash.normalize(msg)
    assert "C<CHANNEL>" in out
    assert "<SLACK_TS>" in out


def test_normalize_strips_uuids():
    msg = "Session 019e6a5b-831e-7fe0-abb0-91ee7a97cfaa failed"
    out = error_hash.normalize(msg)
    assert "<UUID>" in out
    assert "019e6a5b" not in out


def test_compute_collapses_same_root_to_same_hash():
    a = "Lifecycle terminalize failed for inv_id=12345 (commit 595d717)"
    b = "Lifecycle terminalize failed for inv_id=99999 (commit abcdef0)"
    assert error_hash.compute(a) == error_hash.compute(b)


def test_compute_returns_16_hex_chars():
    h = error_hash.compute("some error")
    assert len(h) == 16
    assert all(c in "0123456789abcdef" for c in h)


def test_compute_handles_none():
    h = error_hash.compute(None)
    assert isinstance(h, str)
    assert len(h) == 16


def test_compute_distinct_for_different_roots():
    assert error_hash.compute("TypeError") != error_hash.compute("ConnectionError")
