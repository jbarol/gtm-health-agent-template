"""Tests for ``feedback_aggregate`` and the ``/feedback`` slash command (Plan #30 D2).

Covers four layers:

  1. Window math — ``_window_start`` clamping, default behavior.
  2. Argument parsing — every documented variant of ``/feedback`` text.
  3. Aggregation queries — DB mocked via the same _RealDictConn pattern as
     ``cost_command_test.py``.
  4. Top-level dispatcher + Slack handler — rendering, empty-data fallbacks,
     missing-DB graceful degradation, and the ack/respond contract.

Run:
    cd orchestrator && python3 -m pytest feedback_aggregate_test.py -q
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock


# Same defensive .env stubbing pattern as cost_command_test — keep imports
# clean when the worktree has no .env file.
for _k in (
    "ANTHROPIC_API_KEY",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "SLACK_CHANNEL_ID",
    "ENVIRONMENT_ID",
    "DREAM_AGENT_ID",
    "COORDINATOR_ID",
    "QUICK_AGENT_ID",
    "METHODOLOGY_STORE_ID",
    "HEALTH_STORE_ID",
):
    os.environ.setdefault(_k, "test-stub")

sys.modules.pop("config", None)
sys.modules.pop("feedback_aggregate", None)


# ──────────────────────────────────────────────────────────────────────────
# DB fakery — RealDictCursor-compatible
# ──────────────────────────────────────────────────────────────────────────


class _RealDictCursor:
    """Mimics psycopg2.extras.RealDictCursor: fetchall() returns list[dict].

    Each call to ``fetchall()`` consumes one preset result list, so a single
    request can drive multiple back-to-back queries (mirrors cost_command_test).
    """

    def __init__(self, fetch_results):
        self._results = list(fetch_results or [])
        self.executed: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        if self._results:
            return self._results.pop(0)
        return []


class _RealDictConn:
    def __init__(self, fetch_results):
        self._cursor = _RealDictCursor(fetch_results)
        self.closed = False

    def cursor(self, cursor_factory=None):
        return self._cursor

    def close(self):
        self.closed = True


def _patch_db(monkeypatch, fetch_results, db_url="postgres://test"):
    """Patch db_adapter to use the fake connection. Returns it for assertions."""
    import db_adapter

    conn = _RealDictConn(fetch_results)
    monkeypatch.setattr(db_adapter, "DATABASE_URL", db_url)
    monkeypatch.setattr(db_adapter, "_connect", lambda: conn)
    return conn


# ──────────────────────────────────────────────────────────────────────────
# Window math
# ──────────────────────────────────────────────────────────────────────────


def test_window_start_default_seven_days():
    """Default window is 7 days from ``now``."""
    from feedback_aggregate import _window_start

    now = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)
    start = _window_start(7, now=now)
    assert (now - start).days == 7


def test_window_start_clamps_to_one():
    """Zero/negative input must not produce an empty/inverted window."""
    from feedback_aggregate import _window_start

    now = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)
    assert (now - _window_start(0, now=now)).days == 1
    assert (now - _window_start(-3, now=now)).days == 1


def test_window_start_thirty_days():
    from feedback_aggregate import _window_start

    now = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)
    start = _window_start(30, now=now)
    assert (now - start).days == 30


# ──────────────────────────────────────────────────────────────────────────
# Argument parser
# ──────────────────────────────────────────────────────────────────────────


def test_parse_empty_defaults_to_portco_seven_days():
    from feedback_aggregate import parse_feedback_args

    a = parse_feedback_args("")
    assert a == {"view": "portco", "window_days": 7, "raw": ""}


def test_parse_view_only_each_variant():
    from feedback_aggregate import parse_feedback_args

    for view in ("portco", "agent", "trigger", "negative"):
        a = parse_feedback_args(view)
        assert a["view"] == view
        assert a["window_days"] == 7


def test_parse_window_only_keeps_default_view():
    """``/feedback 30`` → portco view + 30-day window."""
    from feedback_aggregate import parse_feedback_args

    a = parse_feedback_args("30")
    assert a["view"] == "portco"
    assert a["window_days"] == 30


def test_parse_view_plus_window_either_order():
    from feedback_aggregate import parse_feedback_args

    a = parse_feedback_args("agent 30")
    b = parse_feedback_args("30 agent")
    assert a["view"] == "agent" and a["window_days"] == 30
    assert b["view"] == "agent" and b["window_days"] == 30


def test_parse_case_insensitive():
    from feedback_aggregate import parse_feedback_args

    a = parse_feedback_args("NEGATIVE 14")
    assert a["view"] == "negative"
    assert a["window_days"] == 14


def test_parse_unknown_token_ignored():
    """Unknown tokens don't change the view or window — forward-compat."""
    from feedback_aggregate import parse_feedback_args

    a = parse_feedback_args("acme agent")
    assert a["view"] == "agent"
    assert a["window_days"] == 7


def test_parse_negative_int_ignored():
    """Negative integer ignored as a window — keeps the default."""
    from feedback_aggregate import parse_feedback_args

    a = parse_feedback_args("-3 trigger")
    assert a["view"] == "trigger"
    assert a["window_days"] == 7


# ──────────────────────────────────────────────────────────────────────────
# aggregate_by_portco
# ──────────────────────────────────────────────────────────────────────────


def test_aggregate_by_portco_sorted_by_positive_rate_asc(monkeypatch):
    """Lower positive_rate sorts first — noisy portcos surface first."""
    from feedback_aggregate import aggregate_by_portco

    _patch_db(
        monkeypatch,
        fetch_results=[
            [
                {
                    "portco_key": "acme",
                    "positive_count": 4,
                    "negative_count": 1,
                    "neutral_count": 0,
                    "total": 5,
                    "last_event_at": datetime(2026, 5, 10, tzinfo=timezone.utc),
                },
                {
                    "portco_key": "acme",
                    "positive_count": 1,
                    "negative_count": 4,
                    "neutral_count": 0,
                    "total": 5,
                    "last_event_at": datetime(2026, 5, 9, tzinfo=timezone.utc),
                },
            ]
        ],
    )
    rows = aggregate_by_portco(7)
    assert [r["portco_key"] for r in rows] == ["acme", "acme"]
    assert rows[0]["positive_rate"] == 0.2
    assert rows[1]["positive_rate"] == 0.8


def test_aggregate_by_portco_uses_window(monkeypatch):
    """The window_days argument is passed to the WHERE clause."""
    from feedback_aggregate import aggregate_by_portco

    conn = _patch_db(monkeypatch, fetch_results=[[]])
    now = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)
    aggregate_by_portco(7, now=now)
    sql, params = conn._cursor.executed[0]
    assert "feedback_events" in sql
    assert params[0] == now - timedelta(days=7)


def test_aggregate_by_portco_empty_no_db(monkeypatch):
    """No DATABASE_URL → empty list, no crash."""
    import db_adapter
    from feedback_aggregate import aggregate_by_portco

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "")
    assert aggregate_by_portco(7) == []


def test_aggregate_by_portco_db_exception_returns_empty(monkeypatch):
    """A DB exception during the query must return empty — never propagate."""
    import db_adapter
    from feedback_aggregate import aggregate_by_portco

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "postgres://test")
    monkeypatch.setattr(
        db_adapter, "_connect", lambda: (_ for _ in ()).throw(RuntimeError("DB down"))
    )
    assert aggregate_by_portco(7) == []


# ──────────────────────────────────────────────────────────────────────────
# aggregate_by_agent
# ──────────────────────────────────────────────────────────────────────────


def test_aggregate_by_agent_returns_rows_with_agent_id(monkeypatch):
    """Join through session_costs surfaces the agent_id key."""
    from feedback_aggregate import aggregate_by_agent

    _patch_db(
        monkeypatch,
        fetch_results=[
            [
                {
                    "agent_id": "agt_coordinator",
                    "positive_count": 10,
                    "negative_count": 2,
                    "neutral_count": 0,
                    "total": 12,
                    "last_event_at": datetime(2026, 5, 10, tzinfo=timezone.utc),
                },
                {
                    "agent_id": "(unattributed)",
                    "positive_count": 0,
                    "negative_count": 3,
                    "neutral_count": 0,
                    "total": 3,
                    "last_event_at": datetime(2026, 5, 9, tzinfo=timezone.utc),
                },
            ]
        ],
    )
    rows = aggregate_by_agent(7)
    by_agent = {r["agent_id"]: r for r in rows}
    assert by_agent["agt_coordinator"]["positive_rate"] > 0.8
    assert by_agent["(unattributed)"]["positive_rate"] == 0.0
    # Sort puts the worst-rate row first.
    assert rows[0]["agent_id"] == "(unattributed)"


def test_aggregate_by_agent_sql_joins_session_costs(monkeypatch):
    """Pin the join shape — query must reference both tables."""
    from feedback_aggregate import aggregate_by_agent

    conn = _patch_db(monkeypatch, fetch_results=[[]])
    aggregate_by_agent(7)
    sql, _ = conn._cursor.executed[0]
    assert "feedback_events" in sql
    assert "session_costs" in sql
    assert "agent_id" in sql


# ──────────────────────────────────────────────────────────────────────────
# aggregate_by_trigger
# ──────────────────────────────────────────────────────────────────────────


def test_aggregate_by_trigger_returns_rows_with_trigger(monkeypatch):
    """Per-trigger rollup surfaces slack_mention vs cron etc."""
    from feedback_aggregate import aggregate_by_trigger

    _patch_db(
        monkeypatch,
        fetch_results=[
            [
                {
                    "trigger": "slack_mention",
                    "positive_count": 8,
                    "negative_count": 2,
                    "neutral_count": 0,
                    "total": 10,
                    "last_event_at": datetime(2026, 5, 11, tzinfo=timezone.utc),
                },
                {
                    "trigger": "cron",
                    "positive_count": 1,
                    "negative_count": 4,
                    "neutral_count": 0,
                    "total": 5,
                    "last_event_at": datetime(2026, 5, 10, tzinfo=timezone.utc),
                },
            ]
        ],
    )
    rows = aggregate_by_trigger(7)
    by_trigger = {r["trigger"]: r for r in rows}
    assert by_trigger["slack_mention"]["positive_rate"] == 0.8
    assert by_trigger["cron"]["positive_rate"] == 0.2
    # Lower rate sorts first.
    assert rows[0]["trigger"] == "cron"


# ──────────────────────────────────────────────────────────────────────────
# top_negative_recent
# ──────────────────────────────────────────────────────────────────────────


def test_top_negative_recent_returns_channel_link(monkeypatch):
    """Each row is enriched with a Slack-mrkdwn channel link."""
    from feedback_aggregate import top_negative_recent

    _patch_db(
        monkeypatch,
        fetch_results=[
            [
                {
                    "ts": datetime(2026, 5, 11, 10, 0, tzinfo=timezone.utc),
                    "portco_key": "acme",
                    "channel_id": "C0123",
                    "thread_ts": "T1",
                    "agent_message_ts": "M1",
                    "user_id": "U_alice",
                    "raw_text": "thumbsdown",
                },
                {
                    "ts": datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc),
                    "portco_key": "acme",
                    "channel_id": "",  # missing channel → empty link
                    "thread_ts": "T2",
                    "agent_message_ts": "M2",
                    "user_id": "U_bob",
                    "raw_text": "x",
                },
            ]
        ],
    )
    rows = top_negative_recent(limit=5, window_days=7)
    assert len(rows) == 2
    assert rows[0]["channel_link"] == "<#C0123>"
    assert rows[1]["channel_link"] == ""


def test_top_negative_recent_caps_limit(monkeypatch):
    """Limit is clamped to <=50 to keep mrkdwn output small."""
    from feedback_aggregate import top_negative_recent

    conn = _patch_db(monkeypatch, fetch_results=[[]])
    top_negative_recent(limit=99999, window_days=7)
    sql, params = conn._cursor.executed[0]
    # params is (start_ts, limit)
    assert params[1] == 50


def test_top_negative_recent_default_limit_is_five(monkeypatch):
    from feedback_aggregate import top_negative_recent

    conn = _patch_db(monkeypatch, fetch_results=[[]])
    top_negative_recent()
    _, params = conn._cursor.executed[0]
    assert params[1] == 5


# ──────────────────────────────────────────────────────────────────────────
# Empty-data graceful fall
# ──────────────────────────────────────────────────────────────────────────


def test_handle_feedback_command_empty_db_returns_no_feedback_message(monkeypatch):
    """Zero rows but DB is reachable → friendly "no feedback recorded" message."""
    import feedback_aggregate

    monkeypatch.setattr(feedback_aggregate.db_adapter, "DATABASE_URL", "postgres://x")
    monkeypatch.setattr(feedback_aggregate, "aggregate_by_portco", lambda *a, **kw: [])
    out = feedback_aggregate.handle_feedback_command("")
    assert "no feedback recorded" in out.lower()


def test_handle_feedback_command_negative_view_no_rows(monkeypatch):
    """Drill-down view with zero negatives → "no negative feedback" message."""
    import feedback_aggregate

    monkeypatch.setattr(feedback_aggregate.db_adapter, "DATABASE_URL", "postgres://x")
    monkeypatch.setattr(feedback_aggregate, "top_negative_recent", lambda *a, **kw: [])
    out = feedback_aggregate.handle_feedback_command("negative")
    assert "no negative feedback" in out.lower()


def test_handle_feedback_command_without_db_returns_not_configured(monkeypatch):
    """DATABASE_URL unset → graceful "not configured" message, never throws."""
    import db_adapter
    from feedback_aggregate import handle_feedback_command

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "")
    out = handle_feedback_command("")
    assert "Feedback tracking not configured" in out


# ──────────────────────────────────────────────────────────────────────────
# Top-level dispatcher — view routing
# ──────────────────────────────────────────────────────────────────────────


def test_handle_feedback_command_portco_view_renders_table(monkeypatch):
    """Default portco view emits a fixed-width code block with all 5 columns."""
    import feedback_aggregate

    monkeypatch.setattr(feedback_aggregate.db_adapter, "DATABASE_URL", "postgres://x")
    monkeypatch.setattr(
        feedback_aggregate,
        "aggregate_by_portco",
        lambda *a, **kw: [
            {
                "portco_key": "acme",
                "positive_count": 12,
                "negative_count": 3,
                "neutral_count": 0,
                "total": 15,
                "positive_rate": 0.8,
                "last_event_at": datetime(2026, 5, 11, tzinfo=timezone.utc),
            }
        ],
    )
    out = feedback_aggregate.handle_feedback_command("")
    assert "Feedback by portco" in out
    assert "acme" in out
    assert "```" in out  # code-block table
    # Column header
    assert "POS" in out and "NEG" in out and "TOTAL" in out


def test_handle_feedback_command_agent_view_routes_correctly(monkeypatch):
    import feedback_aggregate

    calls = {"agent": 0, "portco": 0}
    monkeypatch.setattr(feedback_aggregate.db_adapter, "DATABASE_URL", "postgres://x")
    monkeypatch.setattr(
        feedback_aggregate,
        "aggregate_by_agent",
        lambda *a, **kw: (
            (calls.__setitem__("agent", calls["agent"] + 1) or [])
            or [
                {
                    "agent_id": "agt_quick",
                    "positive_count": 1,
                    "negative_count": 0,
                    "neutral_count": 0,
                    "total": 1,
                    "positive_rate": 1.0,
                    "last_event_at": datetime(2026, 5, 11, tzinfo=timezone.utc),
                }
            ]
        ),
    )
    monkeypatch.setattr(
        feedback_aggregate,
        "aggregate_by_portco",
        lambda *a, **kw: calls.__setitem__("portco", calls["portco"] + 1) or [],
    )
    out = feedback_aggregate.handle_feedback_command("agent")
    assert calls["agent"] == 1
    assert calls["portco"] == 0
    assert "Feedback by agent" in out
    assert "agt_quick" in out


def test_handle_feedback_command_trigger_view_routes_correctly(monkeypatch):
    import feedback_aggregate

    monkeypatch.setattr(feedback_aggregate.db_adapter, "DATABASE_URL", "postgres://x")
    monkeypatch.setattr(
        feedback_aggregate,
        "aggregate_by_trigger",
        lambda *a, **kw: [
            {
                "trigger": "slack_mention",
                "positive_count": 5,
                "negative_count": 1,
                "neutral_count": 0,
                "total": 6,
                "positive_rate": 0.833,
                "last_event_at": datetime(2026, 5, 11, tzinfo=timezone.utc),
            }
        ],
    )
    out = feedback_aggregate.handle_feedback_command("trigger")
    assert "Feedback by trigger" in out
    assert "slack_mention" in out


def test_handle_feedback_command_negative_view_routes_correctly(monkeypatch):
    """Negative view → "Top negative feedback" header with bullets, not table."""
    import feedback_aggregate

    monkeypatch.setattr(feedback_aggregate.db_adapter, "DATABASE_URL", "postgres://x")
    monkeypatch.setattr(
        feedback_aggregate,
        "top_negative_recent",
        lambda *a, **kw: [
            {
                "ts": datetime(2026, 5, 11, 14, 30, tzinfo=timezone.utc),
                "portco_key": "acme",
                "channel_id": "C0001",
                "channel_link": "<#C0001>",
                "thread_ts": "T",
                "agent_message_ts": "M",
                "user_id": "U_alice",
                "raw_text": "thumbsdown",
            }
        ],
    )
    out = feedback_aggregate.handle_feedback_command("negative")
    assert "Top negative feedback" in out
    assert "acme" in out
    assert "<#C0001>" in out
    assert "<@U_alice>" in out
    assert "thumbsdown" in out
    # Drill-down view uses bullets, not a code-block table.
    assert "•" in out
    assert "```" not in out


def test_handle_feedback_command_window_override_passes_through(monkeypatch):
    """``/feedback 30`` must pass window_days=30 to the aggregator."""
    import feedback_aggregate

    captured: dict = {}

    def _fake_agg(window_days, *, now=None):
        captured["window_days"] = window_days
        return []

    monkeypatch.setattr(feedback_aggregate.db_adapter, "DATABASE_URL", "postgres://x")
    monkeypatch.setattr(feedback_aggregate, "aggregate_by_portco", _fake_agg)
    feedback_aggregate.handle_feedback_command("30")
    assert captured["window_days"] == 30


# ──────────────────────────────────────────────────────────────────────────
# Slack handler — ack/respond contract
# ──────────────────────────────────────────────────────────────────────────


def test_on_feedback_command_calls_ack_and_respond(monkeypatch):
    """The slash-command handler ack()s immediately, then respond()s with mrkdwn."""
    import feedback_aggregate
    import slack_bot

    monkeypatch.setattr(
        feedback_aggregate,
        "handle_feedback_command",
        lambda text: f"*Feedback*\n\nGot: {text!r}",
    )

    ack = MagicMock()
    respond = MagicMock()
    slack_bot.on_feedback_command(ack, {"text": "agent"}, respond)
    ack.assert_called_once()
    respond.assert_called_once()
    kwargs = respond.call_args.kwargs
    assert "Feedback" in kwargs.get("text", "")
    # Public response so the channel sees the rollup
    assert kwargs.get("response_type") == "in_channel"


def test_on_feedback_command_swallows_handler_error(monkeypatch):
    """If the query layer crashes, the user sees a friendly fallback — no exception."""
    import feedback_aggregate
    import slack_bot

    def _boom(text):
        raise RuntimeError("DB on fire")

    monkeypatch.setattr(feedback_aggregate, "handle_feedback_command", _boom)

    ack = MagicMock()
    respond = MagicMock()
    # Must not raise
    slack_bot.on_feedback_command(ack, {"text": ""}, respond)
    ack.assert_called_once()
    respond.assert_called_once()
    assert "query failed" in respond.call_args.kwargs.get("text", "")


def test_on_feedback_command_handles_missing_command_text(monkeypatch):
    """Empty/None command dict → no crash, defaults to empty text."""
    import feedback_aggregate
    import slack_bot

    captured = {"text": None}

    def _fake_handler(text):
        captured["text"] = text
        return "ok"

    monkeypatch.setattr(feedback_aggregate, "handle_feedback_command", _fake_handler)

    ack = MagicMock()
    respond = MagicMock()
    slack_bot.on_feedback_command(ack, None, respond)
    assert captured["text"] == ""
    respond.assert_called_once()
