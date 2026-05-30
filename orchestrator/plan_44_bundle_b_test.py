"""Tests for Plan #44 Bundle B — orchestrator core conformance.

Covers:
- Task #8: AGENT_VERSIONS loaded at boot, surfaced in /health.
- Task #9: _resolve_agent_param shapes the agent argument correctly.
- Task #9 row #4: invalidate_thread_session_cache_for_agent clears the cache.
- Task #13: _build_session_instructions stays under 4096 chars and includes
  portco/channel identity.
- Task #16: redact_event_payload strips SOQL + content; buffer + flush
  round-trip; retry_status unknown-enum branch.
- Task #18: recover_interrupted_investigations archives rows past 25 days.
- Task #22: MCP auto-approve allowlist gates the dispatcher.

All Anthropic + Slack + DB side effects are mocked. No live calls.

Run:
    cd orchestrator && python3 -m pytest plan_44_bundle_b_test.py -q
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

# Set env vars BEFORE importing config — same defensive pattern as
# main_test.py and cost_collector_test.
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
    "PROMPT_ENGINEER_ID",
    "WRITING_AGENT_ID",
):
    os.environ.setdefault(_k, f"stub-{_k.lower()}")


# ---------------------------------------------------------------------------
# Task #8 — AGENT_VERSIONS loaded at boot
# ---------------------------------------------------------------------------


def test_agent_versions_present_at_module_load():
    import config

    # The shipped agents/active_versions.json is intentionally empty ({}) so a
    # fresh fork resolves every agent to its latest version (a stale pin would
    # 404 a freshly-provisioned v1 agent). Assert the loader RAN at module load
    # and produced a well-formed dict (the mechanism) — not that it carries any
    # particular shipped pin. Every value, if present, must be an int.
    assert isinstance(config.AGENT_VERSIONS, dict)
    assert all(isinstance(v, int) for v in config.AGENT_VERSIONS.values())


def test_health_payload_includes_pinned_versions():
    """main._build_health_payload surfaces config.AGENT_VERSIONS as pinned_versions."""
    sys.modules.pop("main", None)
    with patch("anthropic.Anthropic", MagicMock()):
        import main

        payload = main._build_health_payload()
    assert "pinned_versions" in payload
    assert isinstance(payload["pinned_versions"], dict)
    # The active_versions read-from-disk value should match the boot-time
    # copy in steady state. (A drift means redeploy required to pick up the
    # latest pin file — by design.)
    assert payload["pinned_versions"] == payload["active_versions"]


# ---------------------------------------------------------------------------
# Task #9 — _resolve_agent_param
# ---------------------------------------------------------------------------


def _load_session_runner():
    """Import session_runner under a Slack-bolt stub. Returns the module."""
    sys.modules.pop("session_runner", None)
    sys.modules.setdefault("slack_bolt", MagicMock())
    sys.modules.setdefault("slack_bolt.adapter", MagicMock())
    sys.modules.setdefault("slack_bolt.adapter.socket_mode", MagicMock())
    with patch("anthropic.Anthropic", MagicMock()):
        import session_runner

        return session_runner


def test_resolve_agent_param_pinned_returns_structured_form(monkeypatch):
    sr = _load_session_runner()
    # The shipped active_versions.json is empty ({}), so inject a pin for the
    # mapped name to exercise the structured-form branch of the resolver
    # (monkeypatch.setitem auto-reverts after the test).
    monkeypatch.setitem(sr._config.AGENT_VERSIONS, "coordinator", 7)
    # Force a known mapping: assign a fake ID to coordinator name.
    sr._AGENT_NAME_BY_ID["agent_x"] = "coordinator"
    try:
        result = sr._resolve_agent_param("agent_x")
        assert isinstance(result, dict)
        assert result["type"] == "agent"
        assert result["id"] == "agent_x"
        assert isinstance(result["version"], int)
    finally:
        sr._AGENT_NAME_BY_ID.pop("agent_x", None)


def test_resolve_agent_param_unmapped_id_falls_through_to_bare():
    sr = _load_session_runner()
    # An ID that's not in _AGENT_NAME_BY_ID should return the bare ID.
    result = sr._resolve_agent_param("agent_never_seen")
    assert result == "agent_never_seen"


def test_resolve_agent_param_pin_missing_for_name_falls_through_to_bare():
    """If the name is registered but the pin file lacks the key, return bare ID."""
    sr = _load_session_runner()
    sr._AGENT_NAME_BY_ID["agent_unpinned"] = "some_agent_without_a_pin"
    try:
        result = sr._resolve_agent_param("agent_unpinned")
        assert result == "agent_unpinned"
    finally:
        sr._AGENT_NAME_BY_ID.pop("agent_unpinned", None)


def test_resolve_agent_param_empty_id_passes_through():
    sr = _load_session_runner()
    # Empty / falsy input passes through unchanged.
    assert sr._resolve_agent_param("") == ""
    assert sr._resolve_agent_param(None) is None


# ---------------------------------------------------------------------------
# Task #9 (decision row #4) — invalidate_thread_session_cache_for_agent
# ---------------------------------------------------------------------------


def test_invalidate_thread_session_cache_returns_two_key_dict():
    """Closing-review fix 2026-05-13: memory and DB rowcounts are separate.

    After a container restart, ``_thread_sessions`` is empty while the
    ``thread_sessions`` DB table still holds the prior container's rows.
    The single-int return collapsed these — the caller couldn't tell
    which surface mattered. The new contract is a dict.
    """
    sr = _load_session_runner()
    # Migration 00AJ — thread map is keyed on (channel_id, thread_ts).
    sr._thread_sessions[("C001", "thread_a")] = "sess_a"
    sr._thread_sessions[("C002", "thread_b")] = "sess_b"
    with patch.object(sr.db_adapter, "clear_all_thread_sessions", return_value=5):
        result = sr.invalidate_thread_session_cache_for_agent("coordinator")
    assert isinstance(result, dict)
    assert result == {"memory_evicted": 2, "db_rows_cleared": 5}
    assert sr._thread_sessions == {}


def test_invalidate_thread_session_cache_memory_db_diverge_after_restart():
    """Memory is empty but DB has rows — the dict surfaces the difference."""
    sr = _load_session_runner()
    # Simulate the post-restart state: in-memory map empty, DB still has rows.
    sr._thread_sessions.clear()
    with patch.object(sr.db_adapter, "clear_all_thread_sessions", return_value=12):
        result = sr.invalidate_thread_session_cache_for_agent("coordinator")
    assert result["memory_evicted"] == 0
    assert result["db_rows_cleared"] == 12


def test_invalidate_thread_session_cache_swallows_db_errors():
    """A DB failure must not propagate to the caller (the Slack handler).

    Memory eviction still happens; the dict reports the memory side
    accurately and 0 for the DB side. Caller can detect partial success.
    """
    sr = _load_session_runner()
    # Migration 00AJ — thread map is keyed on (channel_id, thread_ts).
    sr._thread_sessions[("C001", "thread_x")] = "sess_x"

    def _boom():
        raise RuntimeError("simulated db outage")

    with patch.object(sr.db_adapter, "clear_all_thread_sessions", side_effect=_boom):
        # Must not raise.
        result = sr.invalidate_thread_session_cache_for_agent("coordinator")
    assert sr._thread_sessions == {}
    assert result == {"memory_evicted": 1, "db_rows_cleared": 0}


# ---------------------------------------------------------------------------
# Task #13 — _build_session_instructions
# ---------------------------------------------------------------------------


def test_build_session_instructions_includes_identity():
    sr = _load_session_runner()
    text = sr._build_session_instructions(portco_key="acme", channel_id="C123")
    assert "Portco: acme" in text
    assert "Slack channel: C123" in text
    assert "Standing rules" in text


def test_build_session_instructions_handles_missing_args():
    sr = _load_session_runner()
    text = sr._build_session_instructions()
    # Always includes the standing-rules block at minimum.
    assert "Standing rules" in text


def test_build_session_instructions_truncates_to_4096_chars():
    sr = _load_session_runner()
    long_extras = ["x" * 2000 for _ in range(10)]
    text = sr._build_session_instructions(
        portco_key="acme",
        channel_id="C123",
        extra_lines=long_extras,
    )
    assert len(text) <= 4096
    # Trailing truncation marker should be present.
    assert text.endswith("...")


# ---------------------------------------------------------------------------
# Task #16 — Thread event capture
# ---------------------------------------------------------------------------


def test_redact_event_payload_strips_soql_from_tool_input():
    sr = _load_session_runner()
    raw = {
        "tool_input": {"q": "SELECT Id FROM Lead", "limit": 10},
        "agent_name": "coordinator",
    }
    out = sr._redact_event_payload("agent.thread_message_completed", raw)
    assert out["tool_input"]["q"] == "[REDACTED]"
    assert out["tool_input"]["limit"] == 10
    assert out["agent_name"] == "coordinator"


def test_redact_event_payload_strips_content_blocks():
    sr = _load_session_runner()
    raw = {
        "content": [
            {"type": "text", "text": "personally identifying detail"},
            {"type": "text", "text": "more pii"},
        ],
    }
    out = sr._redact_event_payload("agent.thread_message_completed", raw)
    assert out["content"] == [{"redacted_blocks": 2}]


def test_redact_event_payload_keeps_safe_keys():
    sr = _load_session_runner()
    raw = {"thread_id": "tid_1", "agent_name": "coordinator", "stop_reason": "end_turn"}
    out = sr._redact_event_payload("session.thread_terminated", raw)
    assert out == raw


def test_buffer_thread_event_only_captures_known_types():
    sr = _load_session_runner()
    sr._thread_event_buffer.pop("test_sess", None)

    # An unrelated event type — should be ignored.
    junk = MagicMock(type="agent.message", session_thread_id="tid_x")
    sr._buffer_thread_event("test_sess", junk)
    assert "test_sess" not in sr._thread_event_buffer

    # A captured type — should land in the buffer.
    captured = MagicMock(
        type="session.thread_started",
        session_thread_id="tid_1",
        processed_at=datetime(2026, 5, 13, tzinfo=timezone.utc),
        agent_name="coordinator",
    )
    captured.model_dump = lambda: {"type": "session.thread_started"}
    sr._buffer_thread_event("test_sess", captured, portco_key="acme")
    assert "test_sess" in sr._thread_event_buffer
    assert len(sr._thread_event_buffer["test_sess"]) == 1
    entry = sr._thread_event_buffer["test_sess"][0]
    assert entry["session_id"] == "test_sess"
    assert entry["thread_id"] == "tid_1"
    assert entry["event_type"] == "session.thread_started"
    assert entry["payload_json"]["portco_key"] == "acme"
    sr._thread_event_buffer.pop("test_sess", None)


def test_flush_thread_event_buffer_calls_db_insert():
    sr = _load_session_runner()
    sr._thread_event_buffer["sess_flush"] = [
        {
            "session_id": "sess_flush",
            "thread_id": "tid_a",
            "event_type": "session.thread_started",
            "agent_name": None,
            "ts": None,
            "payload_json": {},
        }
    ]
    with patch.object(
        sr.db_adapter, "insert_session_thread_events", return_value=1
    ) as mock_insert:
        flushed = sr._flush_thread_event_buffer("sess_flush")
    assert flushed == 1
    assert mock_insert.call_count == 1
    # Buffer cleared.
    assert "sess_flush" not in sr._thread_event_buffer


def test_flush_thread_event_buffer_swallows_db_errors():
    sr = _load_session_runner()
    sr._thread_event_buffer["sess_err"] = [
        {
            "session_id": "sess_err",
            "thread_id": None,
            "event_type": "session.thread_started",
            "agent_name": None,
            "ts": None,
            "payload_json": {},
        }
    ]
    with patch.object(
        sr.db_adapter,
        "insert_session_thread_events",
        side_effect=RuntimeError("db down"),
    ):
        flushed = sr._flush_thread_event_buffer("sess_err")
    # 0 returned on failure, never raises.
    assert flushed == 0


# ---------------------------------------------------------------------------
# Task #18 — Archive aging investigations past 25 days
# ---------------------------------------------------------------------------


def test_recover_archives_rows_older_than_25_days():
    sr = _load_session_runner()
    old_started = datetime.now(timezone.utc) - timedelta(days=30)
    fresh_started = datetime.now(timezone.utc) - timedelta(days=2)
    interrupted_rows = [
        {
            "id": 1,
            "question": "old question",
            "thread_ts": "thread_old",
            "channel_id": "C1",
            "user_id": "U1",
            "session_id": "sess_old",
            "portco_key": "acme",
            "recovery_count": 0,
            "started_at": old_started,
        },
        {
            "id": 2,
            "question": "fresh question",
            "thread_ts": "thread_fresh",
            "channel_id": "C1",
            "user_id": "U1",
            "session_id": "sess_fresh",
            "portco_key": "acme",
            "recovery_count": 0,
            "started_at": fresh_started,
        },
    ]
    with (
        patch.object(
            sr.db_adapter,
            "get_interrupted_investigations",
            return_value=interrupted_rows,
        ),
        patch.object(sr.db_adapter, "update_investigation") as mock_update,
        patch.object(sr.db_adapter, "mark_investigation_recovering"),
        patch.object(sr.db_adapter, "delete_thread_session"),
        patch.object(sr, "send_notification") as mock_notify,
        patch.object(sr, "_stream_and_handle", return_value=([], True, None, [])),
        patch.object(sr, "_download_session_files"),
        patch.object(sr, "run_adhoc_mcp_session") as mock_run_fresh,
        patch.object(sr.client.beta.sessions, "retrieve") as mock_retrieve,
    ):
        mock_retrieve.side_effect = RuntimeError("should not retrieve for archived row")
        mock_run_fresh.return_value = "new_sess"
        sr.recover_interrupted_investigations()

    # The old row should be marked archived; the fresh row should NOT.
    archived_calls = [c for c in mock_update.call_args_list if c.args[1] == "archived"]
    assert len(archived_calls) == 1
    assert archived_calls[0].args[0] == 1
    # Admin DM fired for the archive.
    admin_calls = [
        c for c in mock_notify.call_args_list if c.kwargs.get("admin_only") is True
    ]
    assert any("archived" in (c.args[1] or "").lower() for c in admin_calls)


def test_recover_keeps_fresh_rows():
    """Fresh rows go through the normal resume path, not the archive branch."""
    sr = _load_session_runner()
    fresh_started = datetime.now(timezone.utc) - timedelta(days=1)
    interrupted_rows = [
        {
            "id": 5,
            "question": "fresh question",
            "thread_ts": "thread_fresh",
            "channel_id": "C1",
            "user_id": "U1",
            "session_id": "sess_fresh",
            "portco_key": "acme",
            "recovery_count": 0,
            "started_at": fresh_started,
        }
    ]
    fake_session = MagicMock(status="idle", usage=MagicMock())
    with (
        patch.object(
            sr.db_adapter,
            "get_interrupted_investigations",
            return_value=interrupted_rows,
        ),
        patch.object(sr.db_adapter, "update_investigation") as mock_update,
        patch.object(sr.db_adapter, "mark_investigation_recovering"),
        patch.object(sr.db_adapter, "delete_thread_session"),
        patch.object(sr, "send_notification"),
        patch.object(sr, "_stream_and_handle", return_value=([], True, None, [])),
        patch.object(sr, "_download_session_files"),
        patch.object(sr.client.beta.sessions, "retrieve", return_value=fake_session),
        patch.object(sr, "_compute_session_input_side", return_value=10_000),
        patch.object(sr, "run_adhoc_mcp_session", return_value="new_sess"),
    ):
        sr.recover_interrupted_investigations()

    # Fresh rows: archive must NOT be called.
    archived_calls = [c for c in mock_update.call_args_list if c.args[1] == "archived"]
    assert archived_calls == []


# ---------------------------------------------------------------------------
# Task #22 — MCP auto-approve allowlist gates the dispatcher
# ---------------------------------------------------------------------------


def test_mcp_auto_approve_constant_starts_empty():
    """Production default: nothing auto-approved."""
    import config

    assert config.MCP_AUTO_APPROVE_ALLOWLIST == set()


# ---------------------------------------------------------------------------
# Task #16 — retry_status enum discovery branch fires admin DM
# ---------------------------------------------------------------------------


def test_unknown_retry_status_branch_is_present():
    """The unknown-enum admin DM message text is present in _stream_and_handle.

    A precise behavioral test would require building a synthetic event
    stream; that's tested through the existing session_runner integration
    tests once the retry_status type lands in production. This canary
    confirms the branch wasn't accidentally deleted.
    """
    import inspect

    sr = _load_session_runner()
    src = inspect.getsource(sr._stream_and_handle)
    assert "UNKNOWN_RETRY_STATUS_TYPE" in src
    assert "admin_only=True" in src


def test_mcp_unrecognized_server_branch_is_present():
    """The UNRECOGNIZED_MCP_SERVER log + admin DM branch is present in
    _stream_and_handle.
    """
    import inspect

    sr = _load_session_runner()
    src = inspect.getsource(sr._stream_and_handle)
    assert "UNRECOGNIZED_MCP_SERVER" in src
    assert "MCP_AUTO_APPROVE_ALLOWLIST" in src


# ---------------------------------------------------------------------------
# db_adapter — new helpers (no real DB; mock the connection)
# ---------------------------------------------------------------------------


def test_clear_all_thread_sessions_no_db_returns_zero(monkeypatch):
    import db_adapter

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "")
    assert db_adapter.clear_all_thread_sessions() == 0


def test_clear_all_thread_sessions_returns_rowcount(monkeypatch):
    import db_adapter

    cur = MagicMock()
    cur.rowcount = 3
    conn = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    monkeypatch.setattr(db_adapter, "DATABASE_URL", "postgres://test")
    monkeypatch.setattr(db_adapter, "_connect", MagicMock(return_value=conn))
    assert db_adapter.clear_all_thread_sessions() == 3
    cur.execute.assert_called_once_with("DELETE FROM thread_sessions")


def test_insert_session_thread_events_no_db_returns_zero(monkeypatch):
    import db_adapter

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "")
    rows = [
        {
            "session_id": "s1",
            "thread_id": "t1",
            "event_type": "session.thread_started",
            "agent_name": None,
            "ts": None,
            "payload_json": {"key": "value"},
        }
    ]
    assert db_adapter.insert_session_thread_events(rows) == 0


def test_insert_session_thread_events_truncates_large_payload(monkeypatch):
    import db_adapter

    cur = MagicMock()
    conn = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    monkeypatch.setattr(db_adapter, "DATABASE_URL", "postgres://test")
    monkeypatch.setattr(db_adapter, "_connect", MagicMock(return_value=conn))

    huge_payload = {"data": "x" * 10_000}
    rows = [
        {
            "session_id": "s1",
            "thread_id": "t1",
            "event_type": "session.thread_started",
            "agent_name": None,
            "ts": None,
            "payload_json": huge_payload,
        }
    ]
    inserted = db_adapter.insert_session_thread_events(rows)
    assert inserted == 1
    # Inspect the executemany payload — it should be a truncated string
    # under the 4 KB cap.
    args, _ = cur.executemany.call_args
    sql, values = args
    serialized_payload = values[0][5]  # 6th tuple element is payload_json
    assert len(serialized_payload.encode("utf-8")) <= 4096


def test_insert_session_thread_events_skips_blank_session_id(monkeypatch):
    import db_adapter

    cur = MagicMock()
    conn = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    monkeypatch.setattr(db_adapter, "DATABASE_URL", "postgres://test")
    monkeypatch.setattr(db_adapter, "_connect", MagicMock(return_value=conn))
    rows = [
        {"session_id": "", "event_type": "session.thread_started"},
        {"session_id": "s1", "event_type": "session.thread_started"},
    ]
    assert db_adapter.insert_session_thread_events(rows) == 1


def test_purge_session_thread_events_no_db_returns_zero(monkeypatch):
    import db_adapter

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "")
    assert db_adapter.purge_session_thread_events_older_than(30) == 0


def test_purge_session_thread_events_uses_days_interval(monkeypatch):
    import db_adapter

    cur = MagicMock()
    cur.rowcount = 17
    conn = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    monkeypatch.setattr(db_adapter, "DATABASE_URL", "postgres://test")
    monkeypatch.setattr(db_adapter, "_connect", MagicMock(return_value=conn))
    assert db_adapter.purge_session_thread_events_older_than(30, batch_size=1000) == 17
    args, _ = cur.execute.call_args
    sql, params = args
    assert "DELETE FROM session_thread_events" in sql
    assert params == ("30", 1000)


def test_purge_session_thread_events_returns_zero_on_invalid_days(monkeypatch):
    import db_adapter

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "postgres://test")
    assert db_adapter.purge_session_thread_events_older_than(0) == 0
    assert db_adapter.purge_session_thread_events_older_than(-5) == 0


# ---------------------------------------------------------------------------
# Closing-review fix: 30-day TTL purge scheduler registration (HIGH #1)
# ---------------------------------------------------------------------------


def test_scheduled_purge_session_thread_events_success_logs_rows(caplog):
    """The scheduler wrapper logs the deletion count and never raises."""
    import logging as _logging

    sys.modules.pop("main", None)
    with patch("anthropic.Anthropic", MagicMock()):
        import main as _main

    with (
        patch(
            "db_adapter.purge_session_thread_events_older_than",
            return_value=42,
        ),
        patch.object(_main, "send_notification") as mock_notify,
        caplog.at_level(_logging.INFO, logger="orchestrator"),
    ):
        _main.scheduled_purge_session_thread_events()

    msgs = [r.getMessage() for r in caplog.records]
    assert any("session_thread_events purge completed" in m for m in msgs), (
        f"expected completion log line, got: {msgs}"
    )
    assert any("42" in m for m in msgs), f"expected row count in logs: {msgs}"
    mock_notify.assert_not_called()


def test_scheduled_purge_session_thread_events_swallows_exceptions(caplog):
    """A DB outage must not crash the scheduler thread; admin watch fires."""
    import logging as _logging

    sys.modules.pop("main", None)
    with patch("anthropic.Anthropic", MagicMock()):
        import main as _main

    def _boom(*_a, **_kw):
        raise RuntimeError("simulated db outage")

    with (
        patch(
            "db_adapter.purge_session_thread_events_older_than",
            side_effect=_boom,
        ),
        patch.object(_main, "send_notification") as mock_notify,
        caplog.at_level(_logging.ERROR, logger="orchestrator"),
    ):
        # MUST NOT raise — scheduler threads silently die on unhandled
        # exceptions and the next purge attempt never fires.
        _main.scheduled_purge_session_thread_events()

    # A watch notice should have fired so an operator catches the failure.
    mock_notify.assert_called_once()
    args, _kw = mock_notify.call_args
    assert args[0] == "watch"
    assert "session_thread_events" in args[1]
    assert "purge failed" in args[1].lower()


def test_main_registers_session_thread_events_purge_job(caplog):
    """main() registers the 30-day TTL purge at 06:00 PT (HIGH #1 fix).

    Without this, the migration's stated 30-day retention is a lie —
    rows accumulate forever and the per-session insert burst eventually
    bloats Postgres. The migration file
    ``orchestrator/migrations/00AD_session_thread_events.sql`` explicitly
    documents the intended cron slot.
    """
    import logging as _logging

    # Use the same shared scaffolding pattern as the existing cost-cron
    # tests. We don't import _FakeScheduler from main_test.py (cross-file
    # coupling is fragile); the inline stand-in matches that contract.
    class _FakeScheduler:
        def __init__(self, *_a, **_kw):
            self.jobs: list[dict] = []

        def add_job(self, func, trigger=None, *, id=None, name=None, **kwargs):  # noqa: A002
            self.jobs.append(
                {
                    "func": func,
                    "trigger": trigger,
                    "id": id,
                    "name": name,
                    "kwargs": kwargs,
                }
            )

        def add_listener(self, *_a, **_kw):
            pass

        def start(self):
            pass

        def shutdown(self, **_kw):
            pass

    sys.modules.pop("main", None)
    with patch("anthropic.Anthropic", MagicMock()):
        import main as _main

    fake_sched = _FakeScheduler()
    _main.config.ANTHROPIC_ADMIN_KEY = ""

    with (
        patch.object(_main, "BackgroundScheduler", return_value=fake_sched),
        patch.object(_main, "set_question_handler"),
        patch.object(_main, "set_feedback_handler"),
        patch.object(_main, "start_socket_mode"),
        patch.object(_main, "is_db_available", return_value=False),
        patch.object(_main, "send_notification"),
        patch("signal.signal"),
        caplog.at_level(_logging.INFO, logger="orchestrator"),
    ):
        _main.main()

    purge_jobs = [
        j for j in fake_sched.jobs if j["id"] == "session-thread-events-purge"
    ]
    assert len(purge_jobs) == 1, (
        f"expected exactly one session-thread-events-purge job, got: "
        f"{[j['id'] for j in fake_sched.jobs]}"
    )
    job = purge_jobs[0]
    assert job["func"] is _main.scheduled_purge_session_thread_events
    assert job["name"] == "Session Thread Events 30-day TTL Purge"
    trigger_repr = repr(job["trigger"])
    assert "hour='6'" in trigger_repr, (
        f"expected 6am hour in trigger, got: {trigger_repr}"
    )
    assert "minute='0'" in trigger_repr
    # Startup log line so operators scanning the boot output see the wire-up.
    assert any(
        "session_thread_events 30-day TTL purge at 06:00" in r.message
        for r in caplog.records
    )


# ---------------------------------------------------------------------------
# Closing-review fix: _stream_and_handle three-branch retry_status (HIGH #2)
# ---------------------------------------------------------------------------
#
# Branch (1) — rs_type in KNOWN ("terminal", "exhausted") → break + Slack.
# Branch (2) — rs_type in ("", None)                       → continue, no DM.
# Branch (3) — rs_type non-empty, unknown                   → continue + DM.
#
# Each branch gets its own behavioral test that drives a synthetic stream
# through _stream_and_handle, asserts the correct exit and the correct
# side effects (Slack post, admin DM, neither).


def _make_event(event_type, **kwargs):
    from types import SimpleNamespace

    return SimpleNamespace(type=event_type, **kwargs)


def _make_stream(events):
    class _FakeStream:
        def __enter__(self):
            return iter(events)

        def __exit__(self, exc_type, exc, tb):
            return False

    return _FakeStream()


def test_session_error_branch_1_known_terminal_breaks_and_posts_slack():
    """Known terminal types ("exhausted") break the loop and Slack-notify.

    Trailing events after the session.error must NOT be processed.
    """
    sr = _load_session_runner()

    from types import SimpleNamespace

    err_event = _make_event(
        "session.error",
        error=SimpleNamespace(
            type="overloaded",
            message="prompt is too long: 1,119,846 > 1,000,000",
            retry_status=SimpleNamespace(type="exhausted"),
        ),
    )
    trailing = _make_event(
        "agent.message",
        content=[SimpleNamespace(text="should not be reached")],
    )

    fake_stream = _make_stream([err_event, trailing])

    with (
        patch.object(sr, "client") as mock_client,
        patch.object(sr, "send_notification") as mock_send,
    ):
        mock_client.beta.sessions.events.stream.return_value = fake_stream
        mock_client.beta.sessions.events.send = MagicMock()
        mock_send.return_value = "ts-terminal"

        text_parts, _, error_type, _ = sr._stream_and_handle(
            session_id="sesn_EXAMPLE_1",
            send_events=None,
            thread_ts="thread-branch-1",
            verbosity="summary",
        )

    assert "should not be reached" not in "".join(text_parts)
    assert error_type == "overloaded"
    # Slack recovery post fired on the user's thread.
    mock_send.assert_called_once()
    assert mock_send.call_args.kwargs.get("reply_to") == "thread-branch-1"


def test_session_error_branch_2_empty_rs_type_continues_without_admin_dm(caplog):
    """Empty retry_status.type means Anthropic is still retrying internally.

    The loop must continue (next event consumed), no admin DM fires, and
    the log line is at WARNING level. This is the path the old single
    branch swallowed silently — making the empty-rs_type case
    indistinguishable from an unknown enum.
    """
    import logging as _logging

    sr = _load_session_runner()

    from types import SimpleNamespace

    err_event = _make_event(
        "session.error",
        error=SimpleNamespace(
            type="transient_blip",
            message="upstream timeout, retrying",
            retry_status=SimpleNamespace(type=""),  # empty string is branch (2)
        ),
    )
    # A following agent.message proves the loop continued past the error.
    follow_up = _make_event(
        "agent.message",
        content=[SimpleNamespace(text="post-retry agent text")],
    )
    end_event = _make_event(
        "session.status_idle",
        stop_reason=SimpleNamespace(type="end_turn"),
    )

    fake_stream = _make_stream([err_event, follow_up, end_event])

    with (
        patch.object(sr, "client") as mock_client,
        patch.object(sr, "send_notification") as mock_send,
        caplog.at_level(_logging.WARNING, logger="session_runner"),
    ):
        mock_client.beta.sessions.events.stream.return_value = fake_stream
        mock_client.beta.sessions.events.send = MagicMock()

        text_parts, _, error_type, _ = sr._stream_and_handle(
            session_id="sesn_EXAMPLE_2",
            send_events=None,
            thread_ts="thread-branch-2",
            verbosity="summary",
        )

    # The loop continued past the error and consumed the follow-up text.
    assert "post-retry agent text" in "".join(text_parts), (
        "branch (2) must continue the loop; the event after the error was dropped"
    )
    # Branch (2) does NOT page admins — empty rs_type is the *expected*
    # transient-retry path.
    admin_dms = [
        c for c in mock_send.call_args_list if c.kwargs.get("admin_only") is True
    ]
    assert admin_dms == [], f"branch (2) must NOT fire admin DM, got: {admin_dms}"
    # No Slack post in-thread either — the user's ack is still live.
    in_thread_posts = [
        c
        for c in mock_send.call_args_list
        if c.kwargs.get("reply_to") == "thread-branch-2"
    ]
    assert in_thread_posts == []
    # error_type is assigned on every session.error event (function
    # behavior, not branch-specific). The branch (2) contract is the
    # *side effects*: no admin DM, no Slack post, loop continues.
    assert error_type == "transient_blip"
    # A WARNING-level log mentioning transient retry.
    warning_logs = [
        r
        for r in caplog.records
        if r.levelno == _logging.WARNING and "transient retry" in r.message.lower()
    ]
    assert warning_logs, (
        "branch (2) must emit a WARNING log mentioning 'transient retry'"
    )


def test_session_error_branch_3_unknown_rs_type_fires_admin_dm_and_continues(caplog):
    """A non-empty unknown retry_status.type fires the discovery admin DM.

    The loop still continues (treat as transient by default), but the
    operator gets paged so the enum value can be confirmed and the
    known-set updated in code.
    """
    import logging as _logging

    sr = _load_session_runner()

    from types import SimpleNamespace

    err_event = _make_event(
        "session.error",
        error=SimpleNamespace(
            type="overload_event",
            message="something Anthropic-side is wedged",
            retry_status=SimpleNamespace(type="degraded"),  # never-seen value
        ),
    )
    follow_up = _make_event(
        "agent.message",
        content=[SimpleNamespace(text="post-discovery agent text")],
    )
    end_event = _make_event(
        "session.status_idle",
        stop_reason=SimpleNamespace(type="end_turn"),
    )

    fake_stream = _make_stream([err_event, follow_up, end_event])

    with (
        patch.object(sr, "client") as mock_client,
        patch.object(sr, "send_notification") as mock_send,
        caplog.at_level(_logging.ERROR, logger="session_runner"),
    ):
        mock_client.beta.sessions.events.stream.return_value = fake_stream
        mock_client.beta.sessions.events.send = MagicMock()

        text_parts, _, error_type, _ = sr._stream_and_handle(
            session_id="sesn_EXAMPLE_3",
            send_events=None,
            thread_ts="thread-branch-3",
            verbosity="summary",
        )

    # Loop continued past the unknown-enum error.
    assert "post-discovery agent text" in "".join(text_parts), (
        "branch (3) must continue the loop; the event after the error was dropped"
    )
    # Admin DM fired — that's the whole point of branch (3).
    admin_dms = [
        c for c in mock_send.call_args_list if c.kwargs.get("admin_only") is True
    ]
    assert len(admin_dms) == 1, (
        f"branch (3) must fire exactly one admin DM, got {len(admin_dms)}: {admin_dms}"
    )
    assert "degraded" in admin_dms[0].args[1], (
        "admin DM body must surface the unknown rs_type value"
    )
    # No terminal Slack post in the user's thread (the run isn't dead).
    in_thread_posts = [
        c
        for c in mock_send.call_args_list
        if c.kwargs.get("reply_to") == "thread-branch-3"
    ]
    assert in_thread_posts == []
    # error_type is captured on every session.error event; branch (3)
    # is identified by the admin-DM + continue side effects above.
    assert error_type == "overload_event"
    # ERROR-level log line with the magic tag.
    error_logs = [
        r
        for r in caplog.records
        if r.levelno == _logging.ERROR and "UNKNOWN_RETRY_STATUS_TYPE" in r.message
    ]
    assert error_logs, (
        "branch (3) must emit ERROR log with [UNKNOWN_RETRY_STATUS_TYPE] tag"
    )


def test_session_error_no_dead_code_path():
    """Source-level guard: ensure the second redundant branch was removed.

    The original code had two ``if retry_status and rs_type not in
    _KNOWN_RETRY_STATUS_TYPES`` blocks back-to-back. The first ended in
    ``continue``, making the second permanently unreachable. The fix
    collapses them into three distinct (mutually exclusive) branches.

    Verify by counting the actual ``if`` statements that use each
    discriminator, ignoring docstring/comment mentions (which are
    expected to also reference the predicate for documentation).
    """
    import inspect
    import re

    sr = _load_session_runner()
    src = inspect.getsource(sr._stream_and_handle)
    # Count only ``if ... rs_type in ("", None)`` *statements* (i.e. lines
    # that begin with ``if`` after whitespace). Comments are fine; what
    # matters is that the runtime code has exactly one branch.
    branch_2_if = re.findall(
        r'^\s*if\s+retry_status\s+and\s+rs_type\s+in\s+\("",\s*None\)\s*:',
        src,
        flags=re.MULTILINE,
    )
    assert len(branch_2_if) == 1, (
        f'branch (2) ``if retry_status and rs_type in ("", None):`` must '
        f"appear exactly once as a runtime statement, got: {branch_2_if}"
    )
    # Branch (3) — unknown enum — may be either single-line or wrapped
    # across multiple lines via ``if (\n ... \n):`` style. Accept both
    # shapes; what matters is that the runtime check exists exactly once
    # and the duplicate dead-code branch is gone.
    branch_3_single = re.findall(
        r"^\s*if\s+retry_status\s+and\s+rs_type\s+and\s+rs_type\s+not\s+in\s+_KNOWN_RETRY_STATUS_TYPES\s*:",
        src,
        flags=re.MULTILINE,
    )
    branch_3_wrapped = re.findall(
        r"if\s*\(\s*retry_status\s+and\s+rs_type\s+and\s+rs_type\s+not\s+in\s+_KNOWN_RETRY_STATUS_TYPES\s*\)\s*:",
        src,
        flags=re.DOTALL,
    )
    branch_3_total = len(branch_3_single) + len(branch_3_wrapped)
    assert branch_3_total == 1, (
        f"branch (3) unknown-enum ``if`` must appear exactly once "
        f"(the duplicate dead-code branch was removed), got "
        f"single={branch_3_single} wrapped={branch_3_wrapped}"
    )


# ---------------------------------------------------------------------------
# Closing-review fix: MCP deny path lock-in (MEDIUM #4)
# ---------------------------------------------------------------------------
#
# Anthropic Managed Agents docs (events-and-streaming, "user.tool_confirmation")
# state that the orchestrator sends ``result="deny"`` to refuse a tool
# call and the session resumes; the agent receives the denial in its
# tool-result stream and can re-plan. We were sending the explicit deny
# but no test pinned the resume-after-deny contract — so an SDK upgrade
# could silently break it.


def test_mcp_deny_path_sends_explicit_deny_and_resumes_session():
    """Unauthorized MCP server: dispatcher sends ``result="deny"`` AND continues.

    The deny is sent through events.send and the loop processes the
    following events (a status_idle end_turn closes the run cleanly).
    The orchestrator never hangs waiting for the agent to re-plan.
    """
    sr = _load_session_runner()

    from types import SimpleNamespace

    # Ensure no allowlist entries make the test pass for the wrong reason.
    sr._config.MCP_AUTO_APPROVE_ALLOWLIST = set()

    tool_use_id = "evt_unauth_mcp_1"

    # Stage 1: agent attempts an MCP tool whose server isn't in the
    # allowlist; the SDK gives us a "ask"-permission MCP event.
    mcp_event = _make_event(
        "agent.mcp_tool_use",
        id=tool_use_id,
        name="someUnauthorizedTool",
        input={"q": "SELECT Id FROM Account LIMIT 1"},
        mcp_server_name="evil_unregistered_server",
        evaluated_permission="ask",
    )
    # Stage 2: requires_action with the tool_use_id in event_ids — the
    # dispatcher fires here.
    requires_action_event = _make_event(
        "session.status_idle",
        stop_reason=SimpleNamespace(
            type="requires_action",
            event_ids=[tool_use_id],
        ),
    )
    # Stage 3: agent re-plans and finishes normally — proving the
    # session did NOT hang after the deny.
    post_deny_msg = _make_event(
        "agent.message",
        content=[SimpleNamespace(text="re-planning after deny")],
    )
    end_event = _make_event(
        "session.status_idle",
        stop_reason=SimpleNamespace(type="end_turn"),
    )

    fake_stream = _make_stream(
        [mcp_event, requires_action_event, post_deny_msg, end_event]
    )

    sent_events: list = []

    def _capture_send(session_id, events):
        sent_events.append({"session_id": session_id, "events": events})

    with (
        patch.object(sr, "client") as mock_client,
        patch.object(sr, "send_notification"),  # silence admin DM
    ):
        mock_client.beta.sessions.events.stream.return_value = fake_stream
        mock_client.beta.sessions.events.send = MagicMock(side_effect=_capture_send)

        text_parts, _, error_type, _ = sr._stream_and_handle(
            session_id="sesn_EXAMPLE_path",
            send_events=None,
            thread_ts="thread-deny",
            verbosity="summary",
        )

    # 1. Exactly one events.send call carrying a tool_confirmation with
    #    result="deny" tied to our tool_use_id.
    deny_payloads = []
    for call in sent_events:
        for ev in call["events"] or []:
            if (
                isinstance(ev, dict)
                and ev.get("type") == "user.tool_confirmation"
                and ev.get("tool_use_id") == tool_use_id
            ):
                deny_payloads.append(ev)
    assert len(deny_payloads) == 1, (
        f"expected exactly one user.tool_confirmation for the unauthorized "
        f"MCP tool, got: {deny_payloads}"
    )
    assert deny_payloads[0]["result"] == "deny", (
        f"expected explicit result='deny' (Anthropic Managed Agents docs); "
        f"got: {deny_payloads[0]}"
    )

    # 2. The session resumed: the post-deny agent.message was consumed.
    assert "re-planning after deny" in "".join(text_parts), (
        "after sending deny, the orchestrator must continue the event "
        "loop and consume subsequent agent.message events — locking in "
        "the docs-stated 'session resumes after deny' contract"
    )

    # 3. No terminal error captured (deny is not a session-killing error).
    assert error_type is None


def test_mcp_deny_path_doc_reference_present_in_source():
    """The Anthropic docs URL anchor is present in the deny-path comment.

    Operators reading the code six months from now should be able to
    open the docs without grepping the SDK source. The closing-review
    fix added an explicit URL anchor pointing at the
    events-and-streaming page.
    """
    import inspect

    sr = _load_session_runner()
    src = inspect.getsource(sr._stream_and_handle)
    assert "docs.anthropic.com" in src, "deny-path doc reference URL must be in source"
    assert "events-and-streaming" in src, (
        "deny-path comment must reference the events-and-streaming doc page"
    )
