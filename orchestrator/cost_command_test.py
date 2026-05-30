"""Tests for the ``/cost`` Slack slash command (Plan #35, Task #40).

Covers three layers:

  1. Argument parsing (parse_cost_args) — every documented variant.
  2. Rollup queries (rollup_by_portco / _by_trigger / _by_model) — DB mocked.
  3. Top-level handler (handle_cost_command) — formatting, drift thresholds,
     degraded modes (DATABASE_URL unset, reconciliation failure).

DB and config are stubbed via the same patterns ``cost_collector_test.py`` uses
so the two suites read identically.

Run:
    cd orchestrator && python3 -m pytest cost_command_test.py -q
"""

from __future__ import annotations

import os
import sys
from datetime import date
from unittest.mock import MagicMock


# Same defensive .env stubbing pattern as cost_collector_test — keep imports
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
sys.modules.pop("cost_collector", None)
sys.modules.pop("cost_queries", None)


# ──────────────────────────────────────────────────────────────────────────
# argument parser
# ──────────────────────────────────────────────────────────────────────────


def test_parse_empty_defaults_to_today_all_portcos():
    from cost_queries import parse_cost_args

    a = parse_cost_args("")
    assert a == {"mode": "rollup", "window": "today", "portco": None, "raw": ""}


def test_parse_today_explicit():
    from cost_queries import parse_cost_args

    a = parse_cost_args("today")
    assert a["window"] == "today"
    assert a["portco"] is None
    assert a["mode"] == "rollup"


def test_parse_week_no_portco():
    from cost_queries import parse_cost_args

    a = parse_cost_args("week")
    assert a["window"] == "week"
    assert a["portco"] is None


def test_parse_month_no_portco():
    from cost_queries import parse_cost_args

    a = parse_cost_args("month")
    assert a["window"] == "month"


def test_parse_portco_only():
    """``/cost acme`` → today's spend, single portco."""
    from cost_queries import parse_cost_args

    a = parse_cost_args("acme")
    assert a["window"] == "today"
    assert a["portco"] == "acme"


def test_parse_portco_and_window_either_order():
    """Order shouldn't matter — both forms produce the same spec."""
    from cost_queries import parse_cost_args

    a = parse_cost_args("acme week")
    b = parse_cost_args("week acme")
    assert a["portco"] == "acme" and a["window"] == "week"
    assert b["portco"] == "acme" and b["window"] == "week"


def test_parse_reconcile_mode():
    from cost_queries import parse_cost_args

    a = parse_cost_args("reconcile")
    assert a["mode"] == "reconcile"


def test_parse_reconcile_ignores_other_tokens():
    """``/cost reconcile acme`` still reconciles — portco is irrelevant."""
    from cost_queries import parse_cost_args

    a = parse_cost_args("reconcile acme")
    assert a["mode"] == "reconcile"


def test_parse_case_insensitive():
    from cost_queries import parse_cost_args

    a = parse_cost_args("WEEK ACME")
    assert a["window"] == "week"
    assert a["portco"] == "acme"


# ──────────────────────────────────────────────────────────────────────────
# window_dates
# ──────────────────────────────────────────────────────────────────────────


def test_window_dates_today():
    from cost_queries import window_dates

    start, end = window_dates("today", date(2026, 5, 11))
    assert start == date(2026, 5, 11)
    assert end == date(2026, 5, 11)


def test_window_dates_week_is_seven_days_inclusive():
    from cost_queries import window_dates

    start, end = window_dates("week", date(2026, 5, 11))
    assert end == date(2026, 5, 11)
    assert (end - start).days == 6  # 7 days inclusive


def test_window_dates_month_is_thirty_days_inclusive():
    from cost_queries import window_dates

    start, end = window_dates("month", date(2026, 5, 11))
    assert (end - start).days == 29  # 30 days inclusive


# ──────────────────────────────────────────────────────────────────────────
# DB helpers shared with rollup tests
# ──────────────────────────────────────────────────────────────────────────


class _RealDictCursor:
    """Mimics psycopg2.extras.RealDictCursor: fetchall() returns list[dict]."""

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


def _patch_rolllup_db(monkeypatch, fetch_results, db_url="postgres://test"):
    import db_adapter

    conn = _RealDictConn(fetch_results)
    monkeypatch.setattr(db_adapter, "DATABASE_URL", db_url)
    monkeypatch.setattr(db_adapter, "_connect", lambda: conn)
    return conn


# ──────────────────────────────────────────────────────────────────────────
# rollup_by_portco
# ──────────────────────────────────────────────────────────────────────────


def test_rollup_by_portco_returns_sorted_rows(monkeypatch):
    """Two session rows + one messages-api row → three rows sorted by cost desc."""
    from cost_queries import rollup_by_portco

    _patch_rolllup_db(
        monkeypatch,
        fetch_results=[
            # session_costs result
            [
                {
                    "portco": "acme",
                    "cost_usd": 4.21,
                    "sessions": 47,
                    "cache_pct": 92.0,
                },
                {
                    "portco": "(none)",
                    "cost_usd": 1.50,
                    "sessions": 5,
                    "cache_pct": 80.0,
                },
            ],
            # messages_api result
            [
                {
                    "portco": "(messages-api)",
                    "cost_usd": 0.94,
                    "sessions": 16,
                    "cache_pct": None,
                }
            ],
        ],
    )
    rows = rollup_by_portco(date(2026, 5, 11), date(2026, 5, 11))
    assert len(rows) == 3
    assert rows[0]["portco"] == "acme"
    assert rows[1]["portco"] == "(none)"
    assert rows[2]["portco"] == "(messages-api)"
    assert rows[0]["cost_usd"] == 4.21


def test_rollup_by_portco_filters_to_portco_and_skips_messages(monkeypatch):
    """When portco is given, messages-api is omitted (no portco attribution there)."""
    from cost_queries import rollup_by_portco

    conn = _patch_rolllup_db(
        monkeypatch,
        fetch_results=[
            [
                {
                    "portco": "acme",
                    "cost_usd": 2.0,
                    "sessions": 10,
                    "cache_pct": 70.0,
                }
            ],
        ],
    )
    rows = rollup_by_portco(date(2026, 5, 1), date(2026, 5, 11), portco="acme")
    assert len(rows) == 1
    assert rows[0]["portco"] == "acme"
    # Confirm the SQL had the portco filter and there was no messages-api query
    assert len(conn._cursor.executed) == 1
    sql, params = conn._cursor.executed[0]
    assert "portco_key = %s" in sql
    assert "acme" in params


def test_rollup_by_portco_empty_no_db(monkeypatch):
    """No DATABASE_URL → empty list, no crash."""
    import db_adapter
    from cost_queries import rollup_by_portco

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "")
    assert rollup_by_portco(date(2026, 5, 1), date(2026, 5, 11)) == []


# ──────────────────────────────────────────────────────────────────────────
# rollup_by_trigger / rollup_by_model
# ──────────────────────────────────────────────────────────────────────────


def test_rollup_by_trigger_returns_session_rows(monkeypatch):
    from cost_queries import rollup_by_trigger

    _patch_rolllup_db(
        monkeypatch,
        fetch_results=[
            [
                {"trigger": "slack_mention", "cost_usd": 3.20, "sessions": 8},
                {"trigger": "cron", "cost_usd": 0.84, "sessions": 1},
            ],
        ],
    )
    rows = rollup_by_trigger(date(2026, 5, 11), date(2026, 5, 11))
    assert [r["trigger"] for r in rows] == ["slack_mention", "cron"]


def test_rollup_by_model_merges_session_and_messages(monkeypatch):
    """Same model in both ledgers → summed in a single row."""
    from cost_queries import rollup_by_model

    _patch_rolllup_db(
        monkeypatch,
        fetch_results=[
            # session_costs grouped by model
            [
                {"model": "claude-opus-4-8", "cost_usd": 4.80, "sessions": 12},
                {"model": "claude-sonnet-4-6", "cost_usd": 0.25, "sessions": 5},
            ],
            # messages_api grouped by model
            [{"model": "claude-sonnet-4-6", "cost_usd": 0.10, "sessions": 16}],
        ],
    )
    rows = rollup_by_model(date(2026, 5, 11), date(2026, 5, 11))
    assert len(rows) == 2
    by_model = {r["model"]: r for r in rows}
    assert abs(by_model["claude-sonnet-4-6"]["cost_usd"] - 0.35) < 1e-9
    assert by_model["claude-sonnet-4-6"]["sessions"] == 21
    assert by_model["claude-opus-4-8"]["cost_usd"] == 4.80


# ──────────────────────────────────────────────────────────────────────────
# handle_cost_command — top-level dispatch + render
# ──────────────────────────────────────────────────────────────────────────


def test_handle_cost_command_without_db_returns_not_configured(monkeypatch):
    """DATABASE_URL unset → graceful "not configured" message, never throws."""
    import db_adapter
    from cost_queries import handle_cost_command

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "")
    out = handle_cost_command("")
    assert "Cost tracking not configured" in out


def test_handle_cost_command_today_renders_all_three_sections(monkeypatch):
    """Happy path — full output shape matches Plan #35."""
    from cost_queries import handle_cost_command

    # Stub the three rollup helpers so we don't need a real DB query path.
    import cost_queries

    monkeypatch.setattr(cost_queries.db_adapter, "DATABASE_URL", "postgres://x")
    monkeypatch.setattr(
        cost_queries,
        "rollup_by_portco",
        lambda *a, **kw: [
            {"portco": "acme", "cost_usd": 4.21, "sessions": 47, "cache_pct": 92.0},
            {
                "portco": "(messages-api)",
                "cost_usd": 0.94,
                "sessions": 16,
                "cache_pct": None,
            },
        ],
    )
    monkeypatch.setattr(
        cost_queries,
        "rollup_by_trigger",
        lambda *a, **kw: [
            {"trigger": "slack_mention", "cost_usd": 3.20, "sessions": 8},
            {"trigger": "cron", "cost_usd": 0.84, "sessions": 1},
        ],
    )
    monkeypatch.setattr(
        cost_queries,
        "rollup_by_model",
        lambda *a, **kw: [
            {"model": "claude-opus-4-8", "cost_usd": 4.80, "sessions": 12},
            {"model": "claude-sonnet-4-6", "cost_usd": 0.35, "sessions": 21},
        ],
    )
    out = handle_cost_command("today", today=date(2026, 5, 11))
    assert "*Cost — today (2026-05-11)*" in out
    assert "*By portco:*" in out
    assert "*By trigger:*" in out
    assert "*By model:*" in out
    assert "acme: $4.21 (47 sessions, 92% cached)" in out
    assert "(messages-api): $0.94 (16 calls)" in out
    assert "slack_mention: $3.20" in out
    assert "claude-opus-4-8: $4.80" in out
    # Total = sum of portco rows
    assert "Total: $5.15" in out


def test_handle_cost_command_scopes_to_portco(monkeypatch):
    """When portco is in args, the header reads ``Cost — acme — today...``."""
    from cost_queries import handle_cost_command
    import cost_queries

    monkeypatch.setattr(cost_queries.db_adapter, "DATABASE_URL", "postgres://x")
    monkeypatch.setattr(
        cost_queries,
        "rollup_by_portco",
        lambda *a, **kw: [
            {"portco": "acme", "cost_usd": 1.0, "sessions": 3, "cache_pct": 50.0}
        ],
    )
    monkeypatch.setattr(cost_queries, "rollup_by_trigger", lambda *a, **kw: [])
    monkeypatch.setattr(cost_queries, "rollup_by_model", lambda *a, **kw: [])
    out = handle_cost_command("acme week", today=date(2026, 5, 11))
    assert "Cost — acme — last 7 days" in out
    assert "2026-05-05" in out
    assert "2026-05-11" in out


# ──────────────────────────────────────────────────────────────────────────
# Reconciliation drift thresholds
# ──────────────────────────────────────────────────────────────────────────


def test_reconcile_within_tolerance_no_prefix(monkeypatch):
    """|drift_pct| <= 10% → plain output, no warning prefix."""
    from cost_queries import handle_cost_command
    import cost_queries

    monkeypatch.setattr(cost_queries.db_adapter, "DATABASE_URL", "postgres://x")
    monkeypatch.setattr(
        cost_queries,
        "compute_reconciliation",
        lambda d: {
            "date": "2026-05-10",
            "local_total_usd": 5.04,
            "anthropic_total_usd": 5.32,
            "drift_usd": 0.28,
            "drift_pct": 0.0526,  # 5.26%
            "by_model": {},
        },
    )
    out = handle_cost_command("reconcile", today=date(2026, 5, 11))
    assert ":warning:" not in out
    assert ":rotating_light:" not in out
    assert "within tolerance" in out
    assert "Local estimate: $5.04" in out
    assert "Anthropic billing: $5.32" in out


def test_reconcile_watch_threshold_adds_warning_prefix(monkeypatch):
    """|drift_pct| > 10% (but <= 25%) → ``:warning: Watch:`` prefix."""
    from cost_queries import handle_cost_command
    import cost_queries

    monkeypatch.setattr(cost_queries.db_adapter, "DATABASE_URL", "postgres://x")
    monkeypatch.setattr(
        cost_queries,
        "compute_reconciliation",
        lambda d: {
            "date": "2026-05-10",
            "local_total_usd": 4.0,
            "anthropic_total_usd": 5.0,
            "drift_usd": 1.0,
            "drift_pct": 0.20,  # 20%
            "by_model": {},
        },
    )
    out = handle_cost_command("reconcile", today=date(2026, 5, 11))
    assert out.startswith(":warning: Watch:")
    assert ":rotating_light:" not in out
    assert "outside tolerance" in out


def test_reconcile_critical_threshold_adds_rotating_light_prefix(monkeypatch):
    """|drift_pct| > 25% → ``:rotating_light: CRITICAL — refresh MODEL_COSTS_PER_MTOK`` prefix."""
    from cost_queries import handle_cost_command
    import cost_queries

    monkeypatch.setattr(cost_queries.db_adapter, "DATABASE_URL", "postgres://x")
    monkeypatch.setattr(
        cost_queries,
        "compute_reconciliation",
        lambda d: {
            "date": "2026-05-10",
            "local_total_usd": 2.0,
            "anthropic_total_usd": 5.0,
            "drift_usd": 3.0,
            "drift_pct": 0.60,  # 60%
            "by_model": {},
        },
    )
    out = handle_cost_command("reconcile", today=date(2026, 5, 11))
    assert out.startswith(":rotating_light: CRITICAL")
    assert "refresh MODEL_COSTS_PER_MTOK" in out
    assert "critical drift" in out


def test_reconcile_critical_supersedes_watch(monkeypatch):
    """When drift exceeds both thresholds, only the critical prefix appears."""
    from cost_queries import handle_cost_command
    import cost_queries

    monkeypatch.setattr(cost_queries.db_adapter, "DATABASE_URL", "postgres://x")
    monkeypatch.setattr(
        cost_queries,
        "compute_reconciliation",
        lambda d: {
            "date": "2026-05-10",
            "local_total_usd": 1.0,
            "anthropic_total_usd": 5.0,
            "drift_usd": 4.0,
            "drift_pct": 0.80,
            "by_model": {},
        },
    )
    out = handle_cost_command("reconcile", today=date(2026, 5, 11))
    assert ":rotating_light:" in out
    # Crit message present, watch message absent
    assert "CRITICAL" in out
    assert "Watch:" not in out


def test_reconcile_handles_none_drift_pct(monkeypatch):
    """drift_pct=None (no anthropic data yet) → no prefix, ``n/a`` line."""
    from cost_queries import handle_cost_command
    import cost_queries

    monkeypatch.setattr(cost_queries.db_adapter, "DATABASE_URL", "postgres://x")
    monkeypatch.setattr(
        cost_queries,
        "compute_reconciliation",
        lambda d: {
            "date": "2026-05-10",
            "local_total_usd": 2.0,
            "anthropic_total_usd": 0.0,
            "drift_usd": -2.0,
            "drift_pct": None,
            "by_model": {},
        },
    )
    out = handle_cost_command("reconcile", today=date(2026, 5, 11))
    assert ":warning:" not in out
    assert ":rotating_light:" not in out
    assert "n/a" in out


def test_reconcile_uses_yesterday_date(monkeypatch):
    """The reconciliation target is always today - 1d (Anthropic billing settles overnight)."""
    from cost_queries import handle_cost_command
    import cost_queries

    captured: dict = {}

    def _fake_recon(d):
        captured["date"] = d
        return {
            "date": str(d),
            "local_total_usd": 0,
            "anthropic_total_usd": 0,
            "drift_usd": 0,
            "drift_pct": None,
            "by_model": {},
        }

    monkeypatch.setattr(cost_queries.db_adapter, "DATABASE_URL", "postgres://x")
    monkeypatch.setattr(cost_queries, "compute_reconciliation", _fake_recon)
    handle_cost_command("reconcile", today=date(2026, 5, 11))
    assert captured["date"] == date(2026, 5, 10)


# ──────────────────────────────────────────────────────────────────────────
# Slack handler smoke — verify the registered handler shape works
# ──────────────────────────────────────────────────────────────────────────


def test_on_cost_command_calls_ack_and_respond(monkeypatch):
    """The slash-command handler ack()s immediately, then respond()s with mrkdwn."""
    import cost_queries
    import slack_bot

    monkeypatch.setattr(
        cost_queries,
        "handle_cost_command",
        lambda text: f"*Cost — today*\n\nGot: {text!r}",
    )

    ack = MagicMock()
    respond = MagicMock()
    slack_bot.on_cost_command(ack, {"text": "week"}, respond)
    ack.assert_called_once()
    respond.assert_called_once()
    kwargs = respond.call_args.kwargs
    assert "Cost — today" in kwargs.get("text", "")
    # Public response so the channel sees the breakdown
    assert kwargs.get("response_type") == "in_channel"


def test_on_cost_command_swallows_handler_error(monkeypatch):
    """If the query layer crashes, the user sees a friendly fallback — no exception."""
    import cost_queries
    import slack_bot

    def _boom(text):
        raise RuntimeError("DB on fire")

    monkeypatch.setattr(cost_queries, "handle_cost_command", _boom)

    ack = MagicMock()
    respond = MagicMock()
    # Must not raise
    slack_bot.on_cost_command(ack, {"text": ""}, respond)
    ack.assert_called_once()
    respond.assert_called_once()
    assert "query failed" in respond.call_args.kwargs.get("text", "")
