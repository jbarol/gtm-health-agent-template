"""Tests for the ref-footer helper — Plan #46 §5 tests 1–9.

Run:
    cd orchestrator && python3 -m pytest ref_footer_test.py -v
"""

from __future__ import annotations

from ref_footer import format_ref_footer, ref_context_block


# ---------------------------------------------------------------------------
# format_ref_footer
# ---------------------------------------------------------------------------


def test_format_full() -> None:
    """session_id + inv_id → both fields joined with ' · '."""
    out = format_ref_footer(session_id="sesn_EXAMPLE", inv_id=42)
    assert out == "_ref: sesn_EXAMPLE · inv 42_"


def test_format_session_only() -> None:
    """session_id only → no inv segment."""
    out = format_ref_footer(session_id="sesn_EXAMPLE")
    assert out == "_ref: sesn_EXAMPLE_"


def test_format_inv_only() -> None:
    """inv_id only → no session segment."""
    out = format_ref_footer(inv_id=42)
    assert out == "_ref: inv 42_"


def test_format_context_only() -> None:
    """Static context string (e.g. cost digest) → verbatim body."""
    out = format_ref_footer(context="cost-digest 2026-05-17")
    assert out == "_ref: cost-digest 2026-05-17_"


def test_format_none() -> None:
    """No args → None so the caller skips the block."""
    assert format_ref_footer() is None


def test_session_id_abbreviation() -> None:
    """Full ULID-style session ID slices to ``sesn_`` + 8 chars."""
    out = format_ref_footer(session_id="sesn_EXAMPLE")
    assert out == "_ref: sesn_EXAMPLE_"


def test_session_id_non_standard_prefix() -> None:
    """ID without the ``sesn_`` prefix is handled without crashing.

    Guards against the §11.6 contract-drift risk: if Anthropic changes the
    prefix, the helper falls back to ``session_id[:12]`` so the message
    still carries a usable log-search anchor.
    """
    out = format_ref_footer(session_id="abcdef0123456789xyz")
    # No crash, and the body still identifies the session via a prefix slice.
    assert out is not None
    assert out.startswith("_ref: ")
    assert "abcdef012345" in out


# ---------------------------------------------------------------------------
# ref_context_block
# ---------------------------------------------------------------------------


def test_ref_context_block_structure() -> None:
    """Returned dict matches the Slack Block Kit ``context`` shape."""
    blk = ref_context_block(session_id="sesn_EXAMPLE", inv_id=42)
    assert blk is not None
    assert blk["type"] == "context"
    assert isinstance(blk["elements"], list)
    assert len(blk["elements"]) == 1
    assert blk["elements"][0]["type"] == "mrkdwn"
    assert blk["elements"][0]["text"] == "_ref: sesn_EXAMPLE · inv 42_"


def test_ref_context_block_none() -> None:
    """No identifiers → no block."""
    assert ref_context_block() is None
