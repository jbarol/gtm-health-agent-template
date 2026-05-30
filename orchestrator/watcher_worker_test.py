"""Tests for ``orchestrator/watcher_worker.py``.

Covers:
    - scheduled_watcher_drain no-ops when disabled or shutting down
    - drain pulls only as many rows as there are free executor slots
    - drain submits each row to the executor and tracks the future
    - _run_watcher_job stub marks the row completed on success
    - _run_watcher_job marks failed_retry on error
    - catch_up_on_startup uses CATCH_UP_WINDOW_MINUTES and propagates
      WATCHER_AGENT_ID
    - shutdown_watcher_executor waits on in-flight futures
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

# Env bootstrap before importing watcher_worker (which transitively pulls
# watcher_pending_db when scheduled_watcher_drain fires its lazy import).
for _key, _value in {
    "DATABASE_URL": "postgres://test/db",
    "ANTHROPIC_API_KEY": "sk-ant-test",
    "SLACK_BOT_TOKEN": "xoxb-test",
    "SLACK_APP_TOKEN": "xapp-test",
    "SLACK_CHANNEL_ID": "C0TEST",
    "ENVIRONMENT_ID": "env_test_bootstrap",  # overridden per-test as needed
    "DREAM_AGENT_ID": "agent_test_dream",
    "COORDINATOR_ID": "agent_test_coord",
    "QUICK_AGENT_ID": "agent_test_quick",
    "METHODOLOGY_STORE_ID": "memstore_test_m",
    "HEALTH_STORE_ID": "memstore_test_h",
    "WATCHER_GH_TOKEN": "ghp_test_token",
}.items():
    os.environ.setdefault(_key, _value)


import watcher_worker as ww  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_state():
    ww.reset_for_tests()
    yield
    ww.reset_for_tests()


@pytest.fixture
def _watcher_enabled(monkeypatch):
    monkeypatch.setenv("WATCHER_ENABLED", "true")


@pytest.fixture
def _watcher_disabled(monkeypatch):
    monkeypatch.setenv("WATCHER_ENABLED", "false")


# ───────────────────────────────────────────────────────────────────────
# scheduled_watcher_drain
# ───────────────────────────────────────────────────────────────────────


def test_drain_noop_when_disabled(_watcher_disabled):
    """No DB call, no submit when WATCHER_ENABLED=false."""
    fake_claim = MagicMock(return_value=[])
    with patch("watcher_pending_db.claim_watcher_pending", fake_claim):
        submitted = ww.scheduled_watcher_drain()
    assert submitted == 0
    fake_claim.assert_not_called()


def test_drain_noop_when_shutting_down(_watcher_enabled, monkeypatch):
    monkeypatch.setattr(ww, "_shutting_down", True)
    fake_claim = MagicMock(return_value=[])
    with patch("watcher_pending_db.claim_watcher_pending", fake_claim):
        submitted = ww.scheduled_watcher_drain()
    assert submitted == 0
    fake_claim.assert_not_called()


def test_drain_submits_rows_to_executor(_watcher_enabled):
    rows = [{"id": i, "error_message_hash": f"h{i}"} for i in range(3)]
    fake_claim = MagicMock(return_value=rows)

    # Replace _run_watcher_job with a fast no-op so we don't hit the DB
    runs: list[int] = []

    def _fake_runner(row):
        runs.append(row["id"])

    with patch("watcher_pending_db.claim_watcher_pending", fake_claim):
        submitted = ww.scheduled_watcher_drain(job_runner=_fake_runner)

    assert submitted == 3
    fake_claim.assert_called_once()
    # claim called with limit == MAX_WATCHER_WORKERS (5 - 0 in-flight)
    _args, kwargs = fake_claim.call_args
    assert kwargs == {"limit": ww.MAX_WATCHER_WORKERS}


def test_drain_caps_to_free_slots(_watcher_enabled, monkeypatch):
    """Pretend 4 workers are busy; claim only requests 1 slot."""
    # Manufacture pretend in-flight futures
    monkeypatch.setattr(
        ww, "_active_futures", {MagicMock(): i for i in range(4)}
    )
    fake_claim = MagicMock(return_value=[])
    with patch("watcher_pending_db.claim_watcher_pending", fake_claim):
        ww.scheduled_watcher_drain(job_runner=lambda r: None)
    _args, kwargs = fake_claim.call_args
    assert kwargs == {"limit": 1}


def test_drain_skips_when_pool_full(_watcher_enabled, monkeypatch):
    """All 5 slots taken → no claim attempt at all."""
    monkeypatch.setattr(
        ww, "_active_futures", {MagicMock(): i for i in range(5)}
    )
    fake_claim = MagicMock(return_value=[])
    with patch("watcher_pending_db.claim_watcher_pending", fake_claim):
        submitted = ww.scheduled_watcher_drain(job_runner=lambda r: None)
    assert submitted == 0
    fake_claim.assert_not_called()


def test_drain_swallows_claim_error(_watcher_enabled):
    fake_claim = MagicMock(side_effect=RuntimeError("DB down"))
    with patch("watcher_pending_db.claim_watcher_pending", fake_claim):
        submitted = ww.scheduled_watcher_drain(job_runner=lambda r: None)
    assert submitted == 0


# ───────────────────────────────────────────────────────────────────────
# _run_watcher_job stub
# ───────────────────────────────────────────────────────────────────────


def test_job_runner_marks_diagnose_only_when_env_unset(monkeypatch):
    """Without WATCHER_AGENT_ID + ENVIRONMENT_ID, no dispatch is possible."""
    monkeypatch.delenv("WATCHER_AGENT_ID", raising=False)
    monkeypatch.delenv("ENVIRONMENT_ID", raising=False)
    fake_mark = MagicMock()
    with patch("watcher_pending_db.mark_watcher_pending", fake_mark):
        ww._run_watcher_job({"id": 42, "error_message_hash": "abc"})
    fake_mark.assert_called_once()
    _args, kwargs = fake_mark.call_args
    assert kwargs["status"] == "diagnose_only"


def test_job_runner_marks_completed_when_pr_opened(monkeypatch):
    """Happy path: session runs, agent calls watcher_create_pr, PR URL bubbles
    up to the job runner → completed + admin DM + codex poll scheduled."""
    monkeypatch.setenv("WATCHER_AGENT_ID", "agent_test_watcher")
    monkeypatch.setenv("ENVIRONMENT_ID", "env_test")

    fake_session = MagicMock(id="sesn_EXAMPLE_watcher")
    fake_client = MagicMock()
    fake_client.beta.sessions.create.return_value = fake_session

    # _stream_and_handle returns (text_parts, opened_tools, error_type, _)
    fake_stream_return = (
        ["agent text"],
        [
            {
                "name": "watcher_create_pr",
                "result": {
                    "ok": True,
                    "pr_number": 999,
                    "pr_url": "https://github.com/x/y/pull/999",
                },
            }
        ],
        None,
        None,
    )

    fake_mark = MagicMock()
    fake_dm = MagicMock()
    fake_schedule = MagicMock()
    with patch("anthropic.Anthropic", return_value=fake_client), \
         patch("session_runner._stream_and_handle", return_value=fake_stream_return), \
         patch("watcher_pending_db.mark_watcher_pending", fake_mark), \
         patch.object(ww, "_dm_admin_pr_opened", fake_dm), \
         patch.object(ww, "_schedule_codex_poll", fake_schedule):
        ww._run_watcher_job({"id": 42, "error_message_hash": "abc"})

    fake_mark.assert_called_once()
    _args, kwargs = fake_mark.call_args
    assert kwargs["status"] == "completed"
    fake_dm.assert_called_once()
    fake_schedule.assert_called_once()


def test_job_runner_marks_diagnose_only_when_no_pr(monkeypatch):
    """Agent ran but never opened a PR (diagnose-only mode triggered)."""
    monkeypatch.setenv("WATCHER_AGENT_ID", "agent_test_watcher")
    monkeypatch.setenv("ENVIRONMENT_ID", "env_test")

    fake_session = MagicMock(id="sesn_EXAMPLE")
    fake_client = MagicMock()
    fake_client.beta.sessions.create.return_value = fake_session
    fake_stream_return = (["text"], [], None, None)

    fake_mark = MagicMock()
    with patch("anthropic.Anthropic", return_value=fake_client), \
         patch("session_runner._stream_and_handle", return_value=fake_stream_return), \
         patch("watcher_pending_db.mark_watcher_pending", fake_mark), \
         patch.object(ww, "_dm_admin_diagnose_only"):
        ww._run_watcher_job({"id": 42, "error_message_hash": "abc"})

    fake_mark.assert_called_once()
    _args, kwargs = fake_mark.call_args
    assert kwargs["status"] == "diagnose_only"


def test_job_runner_failed_retry_when_sessions_create_raises(monkeypatch):
    monkeypatch.setenv("WATCHER_AGENT_ID", "agent_test_watcher")
    monkeypatch.setenv("ENVIRONMENT_ID", "env_test")

    fake_client = MagicMock()
    fake_client.beta.sessions.create.side_effect = RuntimeError("API down")

    fake_mark = MagicMock()
    with patch("anthropic.Anthropic", return_value=fake_client), \
         patch("watcher_pending_db.mark_watcher_pending", fake_mark):
        ww._run_watcher_job({"id": 42, "error_message_hash": "abc"})

    fake_mark.assert_called_once()
    _args, kwargs = fake_mark.call_args
    assert kwargs["status"] == "failed_retry"


def test_job_runner_failed_retry_when_stream_raises(monkeypatch):
    monkeypatch.setenv("WATCHER_AGENT_ID", "agent_test_watcher")
    monkeypatch.setenv("ENVIRONMENT_ID", "env_test")

    fake_client = MagicMock()
    fake_client.beta.sessions.create.return_value = MagicMock(id="sesn_EXAMPLE")

    fake_mark = MagicMock()
    with patch("anthropic.Anthropic", return_value=fake_client), \
         patch("session_runner._stream_and_handle", side_effect=RuntimeError("stream broke")), \
         patch("watcher_pending_db.mark_watcher_pending", fake_mark):
        ww._run_watcher_job({"id": 42, "error_message_hash": "abc"})

    fake_mark.assert_called_once()
    _args, kwargs = fake_mark.call_args
    assert kwargs["status"] == "failed_retry"


def test_job_runner_noop_on_missing_id():
    fake_mark = MagicMock()
    with patch("watcher_pending_db.mark_watcher_pending", fake_mark):
        ww._run_watcher_job({"error_message_hash": "abc"})
    fake_mark.assert_not_called()


# ───────────────────────────────────────────────────────────────────────
# catch_up_on_startup
# ───────────────────────────────────────────────────────────────────────


def test_catch_up_noop_when_disabled(_watcher_disabled):
    fake_sweep = MagicMock(return_value=[])
    with patch("watcher_pending_db.catch_up_sweep", fake_sweep):
        n = ww.catch_up_on_startup()
    assert n == 0
    fake_sweep.assert_not_called()


def test_catch_up_propagates_watcher_agent_id(_watcher_enabled, monkeypatch):
    monkeypatch.setenv("WATCHER_AGENT_ID", "agent_WATCHER_XYZ")
    fake_sweep = MagicMock(return_value=[101, 102])
    with patch("watcher_pending_db.catch_up_sweep", fake_sweep):
        n = ww.catch_up_on_startup()
    assert n == 2
    _args, kwargs = fake_sweep.call_args
    assert kwargs["watcher_agent_id"] == "agent_WATCHER_XYZ"
    # since arg should be ~ CATCH_UP_WINDOW_MINUTES ago
    since = kwargs["since"]
    assert isinstance(since, datetime)
    delta = (datetime.now(timezone.utc) - since).total_seconds() / 60
    assert ww.CATCH_UP_WINDOW_MINUTES - 1 < delta < ww.CATCH_UP_WINDOW_MINUTES + 1


def test_catch_up_swallows_sweep_error(_watcher_enabled):
    fake_sweep = MagicMock(side_effect=RuntimeError("DB down"))
    with patch("watcher_pending_db.catch_up_sweep", fake_sweep):
        n = ww.catch_up_on_startup()
    assert n == 0


# ───────────────────────────────────────────────────────────────────────
# shutdown_watcher_executor
# ───────────────────────────────────────────────────────────────────────


def test_shutdown_sets_flag_and_drains():
    """After shutdown, drain ticks must no-op."""
    ww.shutdown_watcher_executor(timeout_seconds=1)
    assert ww._shutting_down is True
    fake_claim = MagicMock(return_value=[])
    with patch.dict(os.environ, {"WATCHER_ENABLED": "true"}, clear=False):
        with patch("watcher_pending_db.claim_watcher_pending", fake_claim):
            submitted = ww.scheduled_watcher_drain(job_runner=lambda r: None)
    assert submitted == 0
    fake_claim.assert_not_called()
