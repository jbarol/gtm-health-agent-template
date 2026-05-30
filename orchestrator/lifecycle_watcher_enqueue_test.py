"""Tests for the ❌-watcher enqueue hook inside ``terminalize_lifecycle``.

Covers ``_enqueue_watcher_pending_for_terminalized`` and its wiring
from ``terminalize_lifecycle``. The hook must:

    - NOT enqueue on success (state.is_delivered())
    - NOT enqueue when WATCHER_ENABLED != 'true' (safe-rollout default)
    - NOT enqueue when investigations.agent_id == WATCHER_AGENT_ID
      (recursion guard)
    - Enqueue cleanly when all gates pass
    - NEVER raise out — terminalize_lifecycle contract is sacred

Fully mocked. No real Postgres / Slack.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

# Bootstrap env BEFORE importing lifecycle — monkeypatch.setattr resolves
# string paths by re-importing the target module, which loads config.py.
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
    "DATABASE_URL": "postgres://test/db",
}.items():
    os.environ.setdefault(_key, _value)


from lifecycle import (  # pyright: ignore[reportMissingImports]  # noqa: E402
    DeliveryState,
    _enqueue_watcher_pending_for_terminalized,
    _terminalized,
    terminalize_lifecycle,
)


# ─────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_idempotency_map():
    _terminalized.clear()
    yield
    _terminalized.clear()


@pytest.fixture(autouse=True)
def _watcher_enabled(monkeypatch):
    """Default to enabled for these tests so each one tests one axis."""
    monkeypatch.setenv("WATCHER_ENABLED", "true")
    monkeypatch.setenv("WATCHER_AGENT_ID", "agent_WATCHER_TEST")


@pytest.fixture
def mock_get_investigation(monkeypatch):
    """Patch db_adapter.get_investigation_by_id with a simple stub."""
    mock = MagicMock(
        return_value={
            "id": 42,
            "agent_id": "agent_user_facing",
            "channel_id": "C123",
            "thread_ts": "1716315045.000100",
            "error_message": "TypeError: cannot subscript",
        }
    )
    monkeypatch.setattr("db_adapter.get_investigation_by_id", mock)
    return mock


@pytest.fixture
def mock_enqueue(monkeypatch):
    """Patch watcher_pending_db.enqueue_watcher_pending."""
    mock = MagicMock(return_value=99)
    monkeypatch.setattr("watcher_pending_db.enqueue_watcher_pending", mock)
    return mock


@pytest.fixture
def mock_terminalize_deps(monkeypatch):
    """Patch the terminalize_lifecycle dependencies that fire before the hook."""
    monkeypatch.setattr(
        "db_adapter.update_investigation_atomic", MagicMock(return_value=True)
    )
    monkeypatch.setattr(
        "slack_bot.transition_reaction", MagicMock(return_value=True)
    )
    # _log_cost_for_terminalized_inv loads session_runner; stub it
    monkeypatch.setattr(
        "lifecycle._log_cost_for_terminalized_inv", MagicMock()
    )


# ─────────────────────────────────────────────────────────────────────────
# Standalone helper tests
# ─────────────────────────────────────────────────────────────────────────


def test_skips_on_success(mock_get_investigation, mock_enqueue):
    _enqueue_watcher_pending_for_terminalized(
        42, DeliveryState.DELIVERED_VIA_POST_REPORT
    )
    mock_enqueue.assert_not_called()
    mock_get_investigation.assert_not_called()


def test_skips_when_watcher_disabled(
    monkeypatch, mock_get_investigation, mock_enqueue
):
    monkeypatch.setenv("WATCHER_ENABLED", "false")
    _enqueue_watcher_pending_for_terminalized(
        42, DeliveryState.TERMINAL_FAILURE
    )
    mock_enqueue.assert_not_called()
    mock_get_investigation.assert_not_called()


def test_skips_when_watcher_env_unset(
    monkeypatch, mock_get_investigation, mock_enqueue
):
    monkeypatch.delenv("WATCHER_ENABLED", raising=False)
    _enqueue_watcher_pending_for_terminalized(
        42, DeliveryState.TERMINAL_FAILURE
    )
    mock_enqueue.assert_not_called()


def test_recursion_guard_when_agent_is_watcher(
    monkeypatch, mock_enqueue
):
    """Investigation owned by the watcher's own agent must NOT re-enqueue."""
    monkeypatch.setattr(
        "db_adapter.get_investigation_by_id",
        MagicMock(
            return_value={
                "id": 42,
                "agent_id": "agent_WATCHER_TEST",  # matches WATCHER_AGENT_ID
                "channel_id": "C123",
                "thread_ts": "1716315045.000100",
                "error_message": "watcher failure",
            }
        ),
    )
    _enqueue_watcher_pending_for_terminalized(
        42, DeliveryState.TERMINAL_FAILURE
    )
    mock_enqueue.assert_not_called()


def test_recursion_guard_passes_for_user_facing_agent(
    mock_get_investigation, mock_enqueue
):
    """Investigation owned by a non-watcher agent should enqueue."""
    _enqueue_watcher_pending_for_terminalized(
        42, DeliveryState.TERMINAL_FAILURE
    )
    mock_enqueue.assert_called_once()
    kwargs = mock_enqueue.call_args.kwargs
    assert kwargs["inv_id"] == 42
    assert kwargs["channel_id"] == "C123"
    assert kwargs["thread_ts"] == "1716315045.000100"
    assert kwargs["error_message_hash"]  # non-empty hash
    assert kwargs["catch_up"] is False


def test_no_op_when_investigation_missing(monkeypatch, mock_enqueue):
    monkeypatch.setattr(
        "db_adapter.get_investigation_by_id", MagicMock(return_value=None)
    )
    _enqueue_watcher_pending_for_terminalized(
        42, DeliveryState.TERMINAL_FAILURE
    )
    mock_enqueue.assert_not_called()


def test_swallows_get_investigation_error(monkeypatch, mock_enqueue):
    monkeypatch.setattr(
        "db_adapter.get_investigation_by_id",
        MagicMock(side_effect=RuntimeError("DB down")),
    )
    # Must NOT raise.
    _enqueue_watcher_pending_for_terminalized(
        42, DeliveryState.TERMINAL_FAILURE
    )
    mock_enqueue.assert_not_called()


def test_swallows_enqueue_error(
    monkeypatch, mock_get_investigation
):
    monkeypatch.setattr(
        "watcher_pending_db.enqueue_watcher_pending",
        MagicMock(side_effect=RuntimeError("queue full")),
    )
    # Must NOT raise.
    _enqueue_watcher_pending_for_terminalized(
        42, DeliveryState.TERMINAL_FAILURE
    )


# ─────────────────────────────────────────────────────────────────────────
# Integration: terminalize_lifecycle → hook
# ─────────────────────────────────────────────────────────────────────────


def test_terminalize_failure_invokes_watcher_enqueue(
    mock_terminalize_deps, mock_get_investigation, mock_enqueue
):
    """End-to-end: ❌ terminalize fires the hook."""
    terminalize_lifecycle(
        DeliveryState.TERMINAL_FAILURE,
        event_ts="1716315045.000100",
        channel_id="C123",
        inv_id=42,
        error_message="something broke",
    )
    mock_enqueue.assert_called_once()


def test_terminalize_success_does_not_invoke_watcher_enqueue(
    mock_terminalize_deps, mock_get_investigation, mock_enqueue
):
    """End-to-end: ✅ terminalize must NOT fire the hook."""
    terminalize_lifecycle(
        DeliveryState.DELIVERED_VIA_POST_REPORT,
        event_ts="1716315045.000100",
        channel_id="C123",
        inv_id=42,
    )
    mock_enqueue.assert_not_called()


def test_terminalize_swallows_watcher_failure(
    monkeypatch, mock_terminalize_deps, mock_get_investigation
):
    """If the watcher enqueue raises, terminalize still returns cleanly."""
    monkeypatch.setattr(
        "watcher_pending_db.enqueue_watcher_pending",
        MagicMock(side_effect=RuntimeError("kaboom")),
    )
    result = terminalize_lifecycle(
        DeliveryState.TERMINAL_FAILURE,
        event_ts="1716315045.000100",
        channel_id="C123",
        inv_id=42,
    )
    assert result == DeliveryState.TERMINAL_FAILURE
