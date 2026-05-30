"""Tests for ``status_responder.status_snippet``.

The responder is the in-thread "what's going on?" output for Track F's
meta-intent routing. It must:
  * Never raise on partial / missing data.
  * Return a useful string when the investigation row is missing.
  * Surface status, age, session id, derived events, and an ETA when we
    have historical wall-clock samples.

Run:
    cd orchestrator && python3 -m pytest status_responder_test.py -v
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock


for _key, _value in {
    "ANTHROPIC_API_KEY": "sk-ant-test",
    "SLACK_BOT_TOKEN": "xoxb-test",
    "SLACK_APP_TOKEN": "xapp-test",
    "SLACK_CHANNEL_ID": "C0TEST",
    "ENVIRONMENT_ID": "env_test",
    "DREAM_AGENT_ID": "agent_test_dream",
    "COORDINATOR_ID": "agent_test_coord",
    "QUICK_AGENT_ID": "agent_test_quick",
    "METHODOLOGY_STORE_ID": "memstore_test_m",
    "HEALTH_STORE_ID": "memstore_test_h",
}.items():
    os.environ.setdefault(_key, _value)


import status_responder  # noqa: E402


# ─── Pure helpers ────────────────────────────────────────────────────────────


def test_age_string_seconds():
    started = datetime.now(timezone.utc) - timedelta(seconds=42)
    out = status_responder._age_string(started)
    assert "s" in out


def test_age_string_minutes():
    started = datetime.now(timezone.utc) - timedelta(minutes=5, seconds=20)
    out = status_responder._age_string(started)
    assert out.startswith("5m")


def test_age_string_hours():
    started = datetime.now(timezone.utc) - timedelta(hours=2, minutes=30)
    out = status_responder._age_string(started)
    assert out.startswith("2h")


def test_age_string_none():
    assert status_responder._age_string(None) == "unknown age"


def test_age_string_naive_datetime_handled():
    """Naive datetimes get coerced to UTC rather than raising."""
    started = datetime.utcnow() - timedelta(minutes=1)  # naive
    out = status_responder._age_string(started)
    assert "m" in out or "s" in out


def test_format_eta_line_remaining(monkeypatch):
    started = datetime.now(timezone.utc) - timedelta(minutes=2)
    line = status_responder._format_eta_line(started, p50_seconds=600)
    # Elapsed = 2m, p50 = 10m → ~8m remaining.
    assert line is not None
    assert "remaining" in line
    assert "typical run: 10m" in line


def test_format_eta_line_running_long():
    started = datetime.now(timezone.utc) - timedelta(minutes=20)
    line = status_responder._format_eta_line(started, p50_seconds=600)
    assert line is not None
    assert "Running long" in line


def test_format_eta_line_no_sample_returns_none():
    started = datetime.now(timezone.utc) - timedelta(minutes=2)
    assert status_responder._format_eta_line(started, p50_seconds=None) is None


def test_format_eta_line_zero_p50_returns_none():
    started = datetime.now(timezone.utc) - timedelta(minutes=2)
    assert status_responder._format_eta_line(started, p50_seconds=0) is None


def test_format_eta_line_no_started_returns_none():
    assert status_responder._format_eta_line(None, p50_seconds=600) is None


def test_recent_events_includes_status_and_session():
    started = datetime(2026, 5, 11, 21, 44, tzinfo=timezone.utc)
    events = status_responder._recent_events(
        {
            "started_at": started,
            "status": "running",
            "session_id": "sesn_EXAMPLE",
            "recovery_count": 0,
        }
    )
    assert any("queued at" in e for e in events)
    assert any("session sesn_EXAMPLE" in e for e in events)
    assert any("status: running" in e for e in events)


def test_recent_events_recovery_count_pluralized():
    started = datetime(2026, 5, 11, 21, 44, tzinfo=timezone.utc)
    events = status_responder._recent_events(
        {
            "started_at": started,
            "status": "running",
            "session_id": "sesn_EXAMPLE",
            "recovery_count": 2,
        }
    )
    assert any("recovered 2 times" in e for e in events)


def test_recent_events_capped_at_five():
    started = datetime(2026, 5, 11, tzinfo=timezone.utc)
    completed = datetime(2026, 5, 11, 1, tzinfo=timezone.utc)
    events = status_responder._recent_events(
        {
            "started_at": started,
            "status": "completed",
            "session_id": "sesn_EXAMPLE",
            "recovery_count": 3,
            "completed_at": completed,
        }
    )
    assert len(events) <= 5


# ─── status_snippet end-to-end ───────────────────────────────────────────────


def _fake_db(monkeypatch, row):
    """Patch db_adapter so status_snippet returns ``row`` from its lookup."""
    import db_adapter

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "postgres://stub", raising=False)

    fake_cur = MagicMock()
    fake_cur.fetchone.return_value = row
    fake_cur.__enter__ = lambda self: self
    fake_cur.__exit__ = lambda self, *a: False

    fake_conn = MagicMock()
    fake_conn.cursor.return_value = fake_cur
    fake_conn.close = MagicMock()

    monkeypatch.setattr(db_adapter, "_connect", lambda: fake_conn)
    # Block the p50 lookup so the test isn't ETA-line dependent unless
    # the test sets it.
    monkeypatch.setattr(status_responder, "_p50_adhoc_runtime_seconds", lambda: None)
    # Block the Anthropic session retrieve so it doesn't network.
    monkeypatch.setattr(status_responder, "_session_archived", lambda sid: None)


def test_status_snippet_missing_row_returns_friendly_message(monkeypatch):
    _fake_db(monkeypatch, None)
    out = status_responder.status_snippet(999)
    assert "No active investigation" in out


def test_status_snippet_running_includes_status_and_age(monkeypatch):
    started = datetime.now(timezone.utc) - timedelta(minutes=3)
    _fake_db(
        monkeypatch,
        {
            "id": 42,
            "thread_ts": "T1",
            "channel_id": "C1",
            "user_id": "U1",
            "question": "what changed?",
            "portco_key": "acme",
            "session_id": "sesn_EXAMPLE",
            "agent_id": "agent_xyz",
            "status": "running",
            "started_at": started,
            "completed_at": None,
            "error_message": None,
            "recovery_count": 0,
            "container_id": "cont-1",
        },
    )
    out = status_responder.status_snippet(42)
    assert "Status: *running*" in out
    assert "Portco: `acme`" in out
    assert "sesn_EXAMPLE" in out
    assert "investigation #42" in out
    assert "Recent events" in out


def test_status_snippet_completed_uses_short_summary(monkeypatch):
    started = datetime.now(timezone.utc) - timedelta(minutes=12)
    completed = datetime.now(timezone.utc) - timedelta(minutes=1)
    _fake_db(
        monkeypatch,
        {
            "id": 43,
            "thread_ts": "T1",
            "channel_id": "C1",
            "user_id": "U1",
            "question": "what?",
            "portco_key": "acme",
            "session_id": "sesn_EXAMPLE",
            "agent_id": "agent_xyz",
            "status": "completed",
            "started_at": started,
            "completed_at": completed,
            "error_message": None,
            "recovery_count": 0,
            "container_id": "cont-1",
        },
    )
    out = status_responder.status_snippet(43)
    assert "Done" in out
    assert "sesn_EXAMPLE" in out
    assert "Findings" in out


def test_status_snippet_cancelled_summary(monkeypatch):
    started = datetime.now(timezone.utc) - timedelta(minutes=4)
    _fake_db(
        monkeypatch,
        {
            "id": 44,
            "thread_ts": "T1",
            "channel_id": "C1",
            "user_id": "U1",
            "question": "what?",
            "portco_key": "acme",
            "session_id": "sesn_EXAMPLE",
            "agent_id": "agent_xyz",
            "status": "cancelled",
            "started_at": started,
            "completed_at": datetime.now(timezone.utc),
            "error_message": "user cancelled",
            "recovery_count": 0,
            "container_id": "cont-1",
        },
    )
    out = status_responder.status_snippet(44)
    assert "Cancelled" in out


def test_status_snippet_eta_included_when_sample_present(monkeypatch):
    started = datetime.now(timezone.utc) - timedelta(minutes=3)
    _fake_db(
        monkeypatch,
        {
            "id": 45,
            "thread_ts": "T1",
            "channel_id": "C1",
            "user_id": "U1",
            "question": "what?",
            "portco_key": "acme",
            "session_id": "sesn_EXAMPLE",
            "agent_id": "agent_xyz",
            "status": "running",
            "started_at": started,
            "completed_at": None,
            "error_message": None,
            "recovery_count": 0,
            "container_id": "cont-1",
        },
    )
    # Override the p50 stub from _fake_db so an ETA line gets rendered.
    monkeypatch.setattr(status_responder, "_p50_adhoc_runtime_seconds", lambda: 600.0)
    out = status_responder.status_snippet(45)
    assert "ETA" in out or "Running long" in out


def test_status_snippet_archived_session_warning(monkeypatch):
    started = datetime.now(timezone.utc) - timedelta(minutes=2)
    _fake_db(
        monkeypatch,
        {
            "id": 46,
            "thread_ts": "T1",
            "channel_id": "C1",
            "user_id": "U1",
            "question": "what?",
            "portco_key": "acme",
            "session_id": "sesn_EXAMPLE",
            "agent_id": "agent_xyz",
            "status": "running",
            "started_at": started,
            "completed_at": None,
            "error_message": None,
            "recovery_count": 0,
            "container_id": "cont-1",
        },
    )
    monkeypatch.setattr(status_responder, "_session_archived", lambda sid: True)
    out = status_responder.status_snippet(46)
    assert "orphaned" in out


def test_status_snippet_never_raises_on_db_error(monkeypatch):
    """Even if every DB / API call blows up, we return a string."""
    import db_adapter

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "postgres://stub", raising=False)

    def explode(*a, **kw):
        raise RuntimeError("postgres unreachable")

    monkeypatch.setattr(db_adapter, "_connect", explode)
    monkeypatch.setattr(status_responder, "_p50_adhoc_runtime_seconds", lambda: None)

    out = status_responder.status_snippet(99)
    assert isinstance(out, str) and out  # no exception, non-empty
    assert "No active investigation" in out


def test_p50_lookup_returns_none_when_db_unset(monkeypatch):
    import db_adapter

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "", raising=False)
    assert status_responder._p50_adhoc_runtime_seconds() is None


def test_p50_lookup_returns_none_on_query_error(monkeypatch):
    import db_adapter

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "postgres://stub", raising=False)

    def explode():
        raise RuntimeError("nope")

    monkeypatch.setattr(db_adapter, "_connect", explode)
    assert status_responder._p50_adhoc_runtime_seconds() is None


def test_session_archived_returns_none_when_session_id_missing():
    assert status_responder._session_archived(None) is None
    assert status_responder._session_archived("") is None
