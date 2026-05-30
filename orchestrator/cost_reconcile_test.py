"""Tests for ``cost_collector.reconcile_daily`` — Plan #35 task #42.

Covers:
  * Drift math + threshold gates (OK / watch / alert / undefined)
  * Direction labeling (under vs. over)
  * Slack-notifier shape: only fires above the 10% watch threshold
  * Dedup: a second call for the same (date, direction) suppresses the alert
  * Severity escalation: > 25% drift adds the MODEL_COSTS_PER_MTOK refresh line
  * Graceful degradation: no DB, no notifier, undefined drift_pct, exceptions

All DB and Slack calls are mocked. Run::

    cd orchestrator && python3 -m pytest cost_reconcile_test.py -q
"""

from __future__ import annotations

import os
import sys
from datetime import date
from unittest.mock import MagicMock


# Mirror cost_collector_test.py — set stub env vars BEFORE the first import
# so config.py's require_env() doesn't raise on a fresh worktree without .env.
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


# ──────────────────────────────────────────────────────────────────────────
# fakes
# ──────────────────────────────────────────────────────────────────────────


class _FakeCursor:
    """Minimal psycopg2-cursor-shaped fake. Records SQL + params per call.

    For reconcile_daily we need two distinct queries paths:
      * compute_reconciliation (3 SELECT + fetchall on session_costs / msgs / anthropic)
      * dedup lookup (SELECT 1 FROM cost_reconciliation_alerts → fetchone)
      * dedup write (INSERT … ON CONFLICT DO NOTHING)

    fetchall_results / fetchone_results are FIFO queues so each call pops the
    expected row set.
    """

    def __init__(self, fetchall_results=None, fetchone_results=None):
        self._fetchall = list(fetchall_results or [])
        self._fetchone = list(fetchone_results or [])
        self.executed = []  # list of (sql, params)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return self._fetchall.pop(0) if self._fetchall else []

    def fetchone(self):
        return self._fetchone.pop(0) if self._fetchone else None


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0
        self.closed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


def _patch_db(monkeypatch, cursor, db_url="postgres://test"):
    import cost_collector
    import db_adapter

    fake_conn = _FakeConn(cursor)
    monkeypatch.setattr(db_adapter, "DATABASE_URL", db_url)
    monkeypatch.setattr(db_adapter, "_connect", lambda: fake_conn)
    _ = cost_collector  # silence unused
    return fake_conn


def _cursor_with(local_rows, anthropic_rows, *, already_alerted=False):
    """Build a cursor seeded so compute_reconciliation + dedup behave as expected.

    Order of fetchall calls inside compute_reconciliation:
        1. session_costs grouped by model
        2. messages_api_calls grouped by model
        3. anthropic_daily_costs grouped by model

    Then dedup _already_alerted does one fetchone (1 if already alerted, None
    otherwise), and _record_alert does an INSERT (no fetch).

    local_rows / anthropic_rows: list[(model, cost)] tuples.
    """
    return _FakeCursor(
        fetchall_results=[
            local_rows,  # session_costs
            [],  # messages_api_calls — empty by default
            anthropic_rows,  # anthropic_daily_costs
        ],
        fetchone_results=[(1,) if already_alerted else None],
    )


# ══════════════════════════════════════════════════════════════════════════
# Drift math + threshold gates
# ══════════════════════════════════════════════════════════════════════════


def test_reconcile_within_tolerance_logs_ok_no_alert(monkeypatch, caplog):
    """|drift_pct| <= 10% → severity 'ok', no Slack call, no DB write."""
    import cost_collector

    # Anthropic $10.00, local $9.50 → drift +5% (under)
    cursor = _cursor_with(
        local_rows=[("claude-sonnet-4-6", 9.5)],
        anthropic_rows=[("claude-sonnet-4-6", 10.0)],
    )
    _patch_db(monkeypatch, cursor)

    notifier = MagicMock()
    with caplog.at_level("INFO"):
        result = cost_collector.reconcile_daily("2026-05-10", notifier=notifier)

    assert result["severity"] == "ok"
    assert result["alerted"] is False
    assert result["deduped"] is False
    assert result["direction"] is None
    assert abs(result["drift_pct"] - 0.05) < 1e-9
    notifier.assert_not_called()
    # No alert INSERT (only compute_reconciliation queries fired)
    assert not any(
        "INSERT INTO cost_reconciliation_alerts" in sql for sql, _ in cursor.executed
    )
    # Log line confirms the OK path
    assert any(
        "reconcile_daily" in r.message and "OK" in r.message for r in caplog.records
    )


def test_reconcile_watch_threshold_posts_slack(monkeypatch):
    """10% < |drift_pct| <= 25% → severity 'watch', Slack notifier fires once."""
    import cost_collector

    # Anthropic $10, local $8.50 → drift +15% under
    cursor = _cursor_with(
        local_rows=[("claude-sonnet-4-6", 8.5)],
        anthropic_rows=[("claude-sonnet-4-6", 10.0)],
    )
    _patch_db(monkeypatch, cursor)

    notifier = MagicMock()
    result = cost_collector.reconcile_daily("2026-05-10", notifier=notifier)

    assert result["severity"] == "watch"
    assert result["direction"] == "under"
    assert result["alerted"] is True
    assert result["deduped"] is False
    notifier.assert_called_once()
    severity_arg, summary_arg = notifier.call_args.args
    assert severity_arg == "watch"
    assert "Cost reconciliation drift" in summary_arg
    assert "2026-05-10" in summary_arg
    assert "under-estimated" in summary_arg
    assert "+15.0%" in summary_arg
    # 25% refresh-recommendation must NOT appear at watch severity
    assert "Drift exceeds 25%" not in summary_arg
    # Dedup write happened
    assert any(
        "INSERT INTO cost_reconciliation_alerts" in sql for sql, _ in cursor.executed
    )


def test_reconcile_alert_threshold_adds_refresh_line(monkeypatch, caplog):
    """|drift_pct| > 25% → severity 'alert', Slack post includes MODEL_COSTS hint."""
    import cost_collector

    # Anthropic $10, local $6 → drift +40% under
    cursor = _cursor_with(
        local_rows=[("claude-opus-4-8", 6.0)],
        anthropic_rows=[("claude-opus-4-8", 10.0)],
    )
    _patch_db(monkeypatch, cursor)

    notifier = MagicMock()
    with caplog.at_level("WARNING"):
        result = cost_collector.reconcile_daily("2026-05-10", notifier=notifier)

    assert result["severity"] == "alert"
    assert result["direction"] == "under"
    assert result["alerted"] is True
    notifier.assert_called_once()
    summary_arg = notifier.call_args.args[1]
    assert "Drift exceeds 25%" in summary_arg
    assert "MODEL_COSTS_PER_MTOK" in summary_arg
    # Warning-level log line for the refresh recommendation
    assert any("MODEL_COSTS_PER_MTOK refresh" in r.message for r in caplog.records)


def test_reconcile_over_estimation_direction(monkeypatch):
    """Local > Anthropic → drift_pct negative → direction 'over'."""
    import cost_collector

    # Anthropic $8, local $10 → drift -25% over (exactly at boundary → 'watch')
    cursor = _cursor_with(
        local_rows=[("claude-sonnet-4-6", 10.0)],
        anthropic_rows=[("claude-sonnet-4-6", 8.0)],
    )
    _patch_db(monkeypatch, cursor)

    notifier = MagicMock()
    result = cost_collector.reconcile_daily("2026-05-09", notifier=notifier)

    # |-0.25| == 0.25 → NOT > 25% threshold → still watch, not alert
    assert result["severity"] == "watch"
    assert result["direction"] == "over"
    summary_arg = notifier.call_args.args[1]
    assert "over-estimated" in summary_arg


def test_reconcile_alert_over_estimation_above_25(monkeypatch):
    """|drift| > 25% on the over side still escalates to 'alert'."""
    import cost_collector

    # Anthropic $4, local $6 → drift -50% over
    cursor = _cursor_with(
        local_rows=[("claude-sonnet-4-6", 6.0)],
        anthropic_rows=[("claude-sonnet-4-6", 4.0)],
    )
    _patch_db(monkeypatch, cursor)

    notifier = MagicMock()
    result = cost_collector.reconcile_daily("2026-05-09", notifier=notifier)

    assert result["severity"] == "alert"
    assert result["direction"] == "over"
    summary_arg = notifier.call_args.args[1]
    assert "Drift exceeds 25%" in summary_arg


def test_reconcile_boundary_exactly_10pct_is_ok(monkeypatch):
    """At exactly 10% the gate is closed — only > 10% triggers a watch."""
    import cost_collector

    # Anthropic $10, local $9 → drift +10% (boundary; <= WATCH_THRESHOLD = ok)
    cursor = _cursor_with(
        local_rows=[("claude-sonnet-4-6", 9.0)],
        anthropic_rows=[("claude-sonnet-4-6", 10.0)],
    )
    _patch_db(monkeypatch, cursor)

    notifier = MagicMock()
    result = cost_collector.reconcile_daily("2026-05-10", notifier=notifier)
    assert result["severity"] == "ok"
    notifier.assert_not_called()


def test_reconcile_undefined_when_anthropic_zero(monkeypatch, caplog):
    """drift_pct == None (no Anthropic data) → severity 'undefined', no alert."""
    import cost_collector

    # session_costs has $5, but anthropic_daily_costs empty → drift_pct = None
    cursor = _cursor_with(
        local_rows=[("claude-sonnet-4-6", 5.0)],
        anthropic_rows=[],
    )
    _patch_db(monkeypatch, cursor)

    notifier = MagicMock()
    with caplog.at_level("INFO"):
        result = cost_collector.reconcile_daily("2026-05-10", notifier=notifier)

    assert result["severity"] == "undefined"
    assert result["drift_pct"] is None
    assert result["alerted"] is False
    assert result["direction"] is None
    notifier.assert_not_called()
    assert any("undefined drift" in r.message for r in caplog.records)


# ══════════════════════════════════════════════════════════════════════════
# Dedup
# ══════════════════════════════════════════════════════════════════════════


def test_reconcile_dedup_suppresses_duplicate_alert(monkeypatch):
    """A row already in cost_reconciliation_alerts → skip the Slack post."""
    import cost_collector

    cursor = _cursor_with(
        local_rows=[("claude-sonnet-4-6", 8.5)],
        anthropic_rows=[("claude-sonnet-4-6", 10.0)],
        already_alerted=True,  # _already_alerted returns True
    )
    _patch_db(monkeypatch, cursor)

    notifier = MagicMock()
    result = cost_collector.reconcile_daily("2026-05-10", notifier=notifier)

    assert result["severity"] == "watch"
    assert result["direction"] == "under"
    assert result["alerted"] is False
    assert result["deduped"] is True
    notifier.assert_not_called()
    # No alert insert when deduped
    assert not any(
        "INSERT INTO cost_reconciliation_alerts" in sql for sql, _ in cursor.executed
    )


def test_reconcile_dedup_is_keyed_by_direction(monkeypatch):
    """A prior 'under' alert does NOT suppress a fresh 'over' alert.

    Different directions → separate dedup rows. Same date, but flipped sign
    counts as a new event worth surfacing.
    """
    import cost_collector

    # Same day was already alerted in the 'over' direction; today is 'under'
    # → must NOT suppress.
    cursor = _FakeCursor(
        fetchall_results=[
            [("claude-sonnet-4-6", 8.5)],  # local
            [],
            [("claude-sonnet-4-6", 10.0)],  # anthropic → drift +15% under
        ],
        fetchone_results=[None],  # _already_alerted checks for direction='under'
    )
    _patch_db(monkeypatch, cursor)

    notifier = MagicMock()
    result = cost_collector.reconcile_daily("2026-05-10", notifier=notifier)
    assert result["direction"] == "under"
    assert result["alerted"] is True
    notifier.assert_called_once()


def test_reconcile_dedup_lookup_uses_correct_params(monkeypatch):
    """Dedup SELECT must filter by (bucket_date, direction) — not by date alone."""
    import cost_collector

    cursor = _cursor_with(
        local_rows=[("claude-sonnet-4-6", 8.5)],
        anthropic_rows=[("claude-sonnet-4-6", 10.0)],
        already_alerted=True,
    )
    _patch_db(monkeypatch, cursor)
    cost_collector.reconcile_daily("2026-05-10", notifier=MagicMock())

    dedup_sql = [
        (sql, params)
        for sql, params in cursor.executed
        if "FROM cost_reconciliation_alerts" in sql
    ]
    assert len(dedup_sql) == 1
    sql, params = dedup_sql[0]
    assert "bucket_date = %s AND direction = %s" in sql
    assert params == ("2026-05-10", "under")


# ══════════════════════════════════════════════════════════════════════════
# Direction / formatting helpers
# ══════════════════════════════════════════════════════════════════════════


def test_drift_direction_labels():
    """_drift_direction maps signed pct → stable label used for dedup PK."""
    import cost_collector

    assert cost_collector._drift_direction(0.15) == "under"
    assert cost_collector._drift_direction(0.0001) == "under"
    assert cost_collector._drift_direction(-0.15) == "over"
    # Zero is treated as 'over' but reconcile_daily short-circuits on 'ok'
    # before this helper is called, so the behavior at 0 is unobservable.


def test_format_alert_message_watch_omits_refresh_hint():
    """Watch severity messages must not mention MODEL_COSTS_PER_MTOK."""
    import cost_collector

    recon = {
        "date": "2026-05-10",
        "local_total_usd": 8.5,
        "anthropic_total_usd": 10.0,
        "drift_usd": 1.5,
        "drift_pct": 0.15,
    }
    msg = cost_collector._format_alert_message(recon, "under", "watch")
    assert "Cost reconciliation drift" in msg
    assert "2026-05-10" in msg
    assert "under-estimated" in msg
    assert "+15.0%" in msg
    assert "$1.5000" in msg
    assert "Drift exceeds 25%" not in msg
    assert "MODEL_COSTS_PER_MTOK" not in msg


def test_format_alert_message_alert_adds_refresh_hint():
    """Alert severity messages must mention MODEL_COSTS_PER_MTOK."""
    import cost_collector

    recon = {
        "date": "2026-05-10",
        "local_total_usd": 6.0,
        "anthropic_total_usd": 10.0,
        "drift_usd": 4.0,
        "drift_pct": 0.4,
    }
    msg = cost_collector._format_alert_message(recon, "under", "alert")
    assert "Drift exceeds 25%" in msg
    assert "MODEL_COSTS_PER_MTOK" in msg


# ══════════════════════════════════════════════════════════════════════════
# Graceful degradation
# ══════════════════════════════════════════════════════════════════════════


def test_reconcile_no_db_returns_undefined(monkeypatch):
    """Without DATABASE_URL, compute_reconciliation returns zeros → undefined drift."""
    import cost_collector
    import db_adapter

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "")
    notifier = MagicMock()
    result = cost_collector.reconcile_daily("2026-05-10", notifier=notifier)
    assert result["severity"] == "undefined"
    assert result["drift_pct"] is None
    notifier.assert_not_called()


def test_reconcile_default_target_date_is_yesterday(monkeypatch):
    """target_date=None → date.today() - 1 day (UTC)."""
    import cost_collector
    import db_adapter

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "")
    captured = {}
    real_compute = cost_collector.compute_reconciliation

    def _capture(d):
        captured["date"] = d
        return real_compute(d)

    monkeypatch.setattr(cost_collector, "compute_reconciliation", _capture)
    cost_collector.reconcile_daily()
    assert isinstance(captured["date"], date)
    assert captured["date"] == date.today().fromordinal(date.today().toordinal() - 1)


def test_reconcile_swallows_notifier_exceptions(monkeypatch, caplog):
    """A broken Slack notifier must not crash the cron — log + move on."""
    import cost_collector

    cursor = _cursor_with(
        local_rows=[("claude-sonnet-4-6", 8.5)],
        anthropic_rows=[("claude-sonnet-4-6", 10.0)],
    )
    _patch_db(monkeypatch, cursor)

    def _explode(severity, summary):
        raise RuntimeError("slack down")

    with caplog.at_level("ERROR"):
        result = cost_collector.reconcile_daily("2026-05-10", notifier=_explode)
    # Notifier failed, but the result still reports alerted=True (we attempted)
    # — the failure is logged but the dedup row is still written so we don't
    # retry-spam the channel on the next tick.
    assert result["alerted"] is True
    assert any("Slack notification failed" in r.message for r in caplog.records)


def test_reconcile_accepts_date_object(monkeypatch):
    """date object (not just string) is accepted and isoformat'd into the output."""
    import cost_collector

    cursor = _cursor_with(local_rows=[], anthropic_rows=[])
    _patch_db(monkeypatch, cursor)
    result = cost_collector.reconcile_daily(date(2026, 5, 10), notifier=MagicMock())
    assert result["date"] == "2026-05-10"
