"""Regression tests for the historical-pulls-through-Postgres routing fix.

Production incident 2026-05-11 (session sesn_EXAMPLE):
a 3,209-row Lead list-pull query — "all Leads where CreatedDate is between
September 1, 2025 and today, Discovery_Call_Booked__c is not null" — burned
3.5M input tokens (crossed 950K of the 1M cap) because the bot routed it
through MCP and accumulated every Lead row in the agent's context.

Root cause:
  1. `needs_same_day_data` matched the bare substring "today" inside the
     date range "and today", flipping the question to the same-day path.
  2. That made `_build_adhoc_prompt` skip the `_get_db_context` call, so
     the agent never saw the Postgres baseline.
  3. The agent's prompts did not contain an explicit "for >500-row pulls,
     stream to xlsx, do not load rows into context" rule at the user-prompt
     layer (it lived buried in the Coordinator system prompt).

These tests pin the corrected routing.

Run:
    cd orchestrator && python3 -m pytest historical_routing_test.py -q
"""

from __future__ import annotations

from unittest.mock import patch


# ---------------------------------------------------------------------------
# Same-day classifier
# ---------------------------------------------------------------------------


def test_date_range_ending_in_today_is_historical():
    """The production-failing question must classify as historical."""
    from db_adapter import needs_same_day_data

    q = (
        "Pull all Leads where CreatedDate is between September 1, 2025 "
        "and today, Discovery_Call_Booked__c date field is not null"
    )
    assert needs_same_day_data(q) is False, (
        "Date-range queries that end with 'and today' must NOT trip the "
        "same-day fallback. This is the routing bug that caused the "
        "sesn_EXAMPLE context blowup."
    )


def test_through_today_is_historical():
    from db_adapter import needs_same_day_data

    assert needs_same_day_data("opps from Q4 through today") is False
    assert needs_same_day_data("leads to today") is False
    assert needs_same_day_data("all accounts up to today") is False
    assert needs_same_day_data("opps as of today") is False


def test_between_and_today_is_historical():
    """`between X and today` is the canonical range form — historical."""
    from db_adapter import needs_same_day_data

    q = "Pull all Leads where CreatedDate is between September 1, 2025 and today"
    assert needs_same_day_data(q) is False
    assert needs_same_day_data("opps from last quarter and today") is False
    assert needs_same_day_data("leads since July 1 and today") is False


def test_compare_yesterday_and_today_is_same_day():
    """Codex regression: `compare yesterday and today` is a same-day ask.

    No `between`/`from`/`since` precedes `and today`, so it's a generic
    conjunction — partial-day freshness matters and we must route to MCP.
    """
    from db_adapter import needs_same_day_data

    assert needs_same_day_data("compare yesterday and today pipeline") is True
    assert needs_same_day_data("yesterday and today, what's different") is True


def test_today_vs_yesterday_is_same_day():
    """Same-day comparison phrasings must route to MCP."""
    from db_adapter import needs_same_day_data

    assert needs_same_day_data("today vs yesterday") is True
    assert needs_same_day_data("what changed today") is True
    assert needs_same_day_data("today's pipeline") is True


def test_today_freshness_still_trips():
    """The legitimate freshness asks must still route through MCP."""
    from db_adapter import needs_same_day_data

    assert needs_same_day_data("what is happening today") is True
    assert needs_same_day_data("today's pipeline") is True
    assert needs_same_day_data("show me today's new leads") is True


def test_anchored_freshness_keywords_still_trip():
    from db_adapter import needs_same_day_data

    assert needs_same_day_data("what's the pipeline right now") is True
    assert needs_same_day_data("opps that just closed") is True
    assert needs_same_day_data("real-time pipeline state") is True
    assert needs_same_day_data("any leads come in this morning") is True


def test_ambiguous_keywords_route_correctly():
    from db_adapter import needs_same_day_data

    # "live" / "current" — freshness signal.
    assert needs_same_day_data("show me the live pipeline") is True
    assert needs_same_day_data("current quarter ARR") is True
    # No keywords — historical.
    assert needs_same_day_data("opps created in last quarter") is False
    assert needs_same_day_data("win rate for Q3 2024") is False


# ---------------------------------------------------------------------------
# Ad-hoc prompt builder
# ---------------------------------------------------------------------------


def test_adhoc_prompt_includes_routing_block_for_historical():
    """The ad-hoc prompt MUST tell the agent to prefer Postgres for
    historical >24h-old data on standard fields."""
    import session_runner

    q = (
        "Pull all Leads where CreatedDate is between September 1, 2025 "
        "and today, Discovery_Call_Booked__c is not null"
    )

    with patch.object(session_runner, "_get_db_context", return_value=""):
        prompt = session_runner._build_adhoc_prompt(q, "acme")

    # The routing block is present, by name.
    assert "DATA-SOURCE ROUTING" in prompt, (
        "Ad-hoc prompt must include explicit data-source routing rules "
        "so the agent doesn't default to MCP for historical pulls."
    )
    # The Postgres path is named explicitly.
    assert "db_query" in prompt
    assert "Railway Postgres" in prompt
    # The list-pull streaming rule is restated at the user-prompt layer.
    assert "500 rows" in prompt or ">500 rows" in prompt
    assert "/mnt/session/outputs/" in prompt
    assert ".xlsx" in prompt
    # The classifier verdict is rendered so the agent can audit the
    # orchestrator's routing call.
    assert "needs_same_day_data classification" in prompt
    assert "historical" in prompt


def test_adhoc_prompt_marks_same_day_classification():
    import session_runner

    q = "what's the pipeline right now"
    with patch.object(session_runner, "_get_db_context", return_value=""):
        prompt = session_runner._build_adhoc_prompt(q, "acme")

    assert "same-day" in prompt
    # MCP still named as the right tool for the same-day path.
    assert "soqlQuery" in prompt


def test_historical_query_pulls_db_context():
    """The historical path must NOT skip the Postgres baseline."""
    import session_runner

    q = (
        "Pull all Leads where CreatedDate is between September 1, 2025 "
        "and today, Discovery_Call_Booked__c is not null"
    )

    with patch.object(
        session_runner,
        "_get_db_context",
        return_value="\n\nHISTORICAL DB CONTEXT (last sync: 2026-05-11):\n...",
    ) as mock_ctx:
        prompt = session_runner._build_adhoc_prompt(q, "acme")

    mock_ctx.assert_called_once_with("acme")
    assert "HISTORICAL DB CONTEXT" in prompt
