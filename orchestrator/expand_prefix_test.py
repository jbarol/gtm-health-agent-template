"""Tests for the `expand:` prefix detector in slack_bot.

Run:
    cd orchestrator && python3 -m pytest expand_prefix_test.py
"""

from __future__ import annotations

import pytest

from slack_bot import EXPAND_PREFIXES, _detect_expand_prefix


@pytest.mark.parametrize(
    "input_text,expect_is_expand,expect_stripped",
    [
        ("expand: why is win rate down?", True, "why is win rate down?"),
        ("long: tell me about pipeline", True, "tell me about pipeline"),
        ("details: how is churn?", True, "how is churn?"),
        ("full version: q1 summary please", True, "q1 summary please"),
        ("full: just the headlines", True, "just the headlines"),
        ("verbose: lay it on me", True, "lay it on me"),
        # Case-insensitive
        ("EXPAND: why is win rate down?", True, "why is win rate down?"),
        ("Expand: why?", True, "why?"),
        # No prefix
        ("why is win rate down?", False, "why is win rate down?"),
        ("expanding the pipeline analysis", False, "expanding the pipeline analysis"),
        ("", False, ""),
        # Whitespace preservation
        ("expand:   leading spaces", True, "leading spaces"),
        # Prefix only — empty question
        ("expand:", True, ""),
    ],
)
def test_detect_expand_prefix(input_text, expect_is_expand, expect_stripped):
    is_expand, stripped = _detect_expand_prefix(input_text)
    assert is_expand == expect_is_expand
    assert stripped == expect_stripped


def test_all_prefixes_detected():
    """Every prefix in EXPAND_PREFIXES is detected when used."""
    for prefix in EXPAND_PREFIXES:
        is_expand, _ = _detect_expand_prefix(f"{prefix} test question")
        assert is_expand, f"Prefix {prefix!r} not detected"


def test_feedback_prefix_not_mistaken_for_expand():
    """A `remember ...` message shouldn't trigger expand detection."""
    is_expand, _ = _detect_expand_prefix("remember to query only New Business deals")
    assert not is_expand
