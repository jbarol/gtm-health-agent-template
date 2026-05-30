"""Tests for the Slack placeholder guard (Plan: Design I, 2026-05-15).

Run:
    cd orchestrator && python3 -m pytest placeholder_guard_test.py -v
"""

from __future__ import annotations

import slack_bot


def test_clean_message_passes():
    assert (
        slack_bot._check_for_unfilled_placeholders(
            "Q2 pipeline is $16.3M ARR across 1,858 opps."
        )
        is None
    )


def test_catches_the_live_l9xzx_leak():
    """The exact string from sesn_EXAMPLE on 2026-05-15."""
    leak = slack_bot._check_for_unfilled_placeholders(
        "Show me trend of [an unspecified metric] over last 6 quarters."
    )
    assert leak == "[an unspecified metric]"


def test_catches_year_placeholder():
    leak = slack_bot._check_for_unfilled_placeholders(
        "Closed lost in [year] vs [year-1]."
    )
    assert leak == "[year]"


def test_skips_short_footnote_markers():
    """[1], [12], [ab] are 1-2 chars inside → below the 3-char min."""
    assert (
        slack_bot._check_for_unfilled_placeholders(
            "Three findings flagged [1] win rate [2] cycle time [3] discount mix."
        )
        is None
    )


def test_skips_known_status_brackets():
    """[draft], [redacted], [TBD] are common in analyses and allowed."""
    for word in ["draft", "redacted", "urgent", "tba", "tbd", "wip"]:
        assert (
            slack_bot._check_for_unfilled_placeholders(
                f"This finding is [{word}] pending review."
            )
            is None
        ), word


def test_skips_slack_mrkdwn_links():
    """`<url|label>` uses angle brackets, not square — should pass cleanly."""
    assert (
        slack_bot._check_for_unfilled_placeholders(
            "See the run at <https://example.com|the run page> for details."
        )
        is None
    )


def test_skips_markdown_links():
    """Markdown `[label](url)` syntax: the ``]`` is immediately followed by
    ``(``. Without the negative lookahead the regex caught the label and the
    guard blocked legitimate links. Self-review caught this 2026-05-15."""
    assert (
        slack_bot._check_for_unfilled_placeholders(
            "See the run at [the run page](https://example.com) for details."
        )
        is None
    )
    assert (
        slack_bot._check_for_unfilled_placeholders(
            "Owner [Hunter Dorsey](https://salesforce.com/005/X) closed 8 deals."
        )
        is None
    )


def test_empty_input_returns_none():
    assert slack_bot._check_for_unfilled_placeholders("") is None
    assert slack_bot._check_for_unfilled_placeholders(None) is None


def test_guard_can_be_disabled_via_env(monkeypatch):
    monkeypatch.setenv("PLACEHOLDER_GUARD_ENABLED", "false")
    assert slack_bot._placeholder_guard_enabled() is False
    monkeypatch.setenv("PLACEHOLDER_GUARD_ENABLED", "true")
    assert slack_bot._placeholder_guard_enabled() is True


def test_guard_default_is_enabled(monkeypatch):
    monkeypatch.delenv("PLACEHOLDER_GUARD_ENABLED", raising=False)
    assert slack_bot._placeholder_guard_enabled() is True
