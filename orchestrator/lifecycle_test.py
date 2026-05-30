"""Unit tests for ``orchestrator.lifecycle``.

Covers the contract documented in lifecycle.py:

    - In-memory idempotency: second call for same inv_id no-ops.
    - DB idempotency: ``UPDATE ... WHERE status NOT IN (...)`` race-loser
      returns the stored state and skips reaction flip.
    - State → action mapping for every terminal value.
    - Reaction-failure does NOT revert the DB row.
    - DB-failure still attempts reaction (defensive — preserves user
      visibility even when DB is down).
    - NOT_DELIVERED at terminalize_lifecycle is an invariant violation,
      logged and treated as TERMINAL_FAILURE.

Run::

    cd orchestrator && python3 -m pytest lifecycle_test.py -v
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from lifecycle import (  # pyright: ignore[reportMissingImports]
    DeliveryState,
    _is_terminalized,
    _remember_terminalized,
    _run_investigation_guarded,
    _terminalized,
    terminalize_lifecycle,
)


# ─────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_idempotency_map():
    """Wipe the in-memory idempotency map before each test."""
    _terminalized.clear()
    yield
    _terminalized.clear()


@pytest.fixture
def mock_db_won(monkeypatch):
    """Patch update_investigation_atomic to return True (we won the race)."""
    mock = MagicMock(return_value=True)
    monkeypatch.setattr("db_adapter.update_investigation_atomic", mock)
    return mock


@pytest.fixture
def mock_db_lost(monkeypatch):
    """Patch update_investigation_atomic to return False (someone else won)."""
    mock = MagicMock(return_value=False)
    monkeypatch.setattr("db_adapter.update_investigation_atomic", mock)
    return mock


@pytest.fixture
def mock_transition_reaction(monkeypatch):
    """Patch transition_reaction in slack_bot so tests don't hit Slack."""
    mock = MagicMock(return_value=True)
    monkeypatch.setattr("slack_bot.transition_reaction", mock)
    return mock


# ─────────────────────────────────────────────────────────────────────────
# Enum invariants
# ─────────────────────────────────────────────────────────────────────────


def test_db_status_mapping():
    """Each state maps to the correct ``investigations.status`` value."""
    assert DeliveryState.DELIVERED_VIA_POST_REPORT.db_status() == "completed"
    assert DeliveryState.DELIVERED_VIA_POST_ANALYSIS.db_status() == "completed"
    assert DeliveryState.DELIVERED_VIA_LEGACY_SLACK_TOOL.db_status() == "completed"
    assert DeliveryState.TERMINAL_FAILURE.db_status() == "failed"
    assert DeliveryState.NO_OUTPUT.db_status() == "failed"
    assert DeliveryState.USER_CANCELLED.db_status() == "cancelled"
    # NOT_DELIVERED is invariant-violation territory but must still map.
    assert DeliveryState.NOT_DELIVERED.db_status() == "failed"


def test_reaction_emoji_mapping():
    """is_delivered() states map to ✅, everything else to ❌."""
    assert (
        DeliveryState.DELIVERED_VIA_POST_REPORT.reaction_emoji() == "white_check_mark"
    )
    assert (
        DeliveryState.DELIVERED_VIA_POST_ANALYSIS.reaction_emoji() == "white_check_mark"
    )
    assert (
        DeliveryState.DELIVERED_VIA_LEGACY_SLACK_TOOL.reaction_emoji()
        == "white_check_mark"
    )
    assert DeliveryState.TERMINAL_FAILURE.reaction_emoji() == "x"
    assert DeliveryState.USER_CANCELLED.reaction_emoji() == "x"
    assert DeliveryState.NO_OUTPUT.reaction_emoji() == "x"


def test_is_delivered_predicate():
    """is_delivered separates the three delivery states from the rest."""
    delivered = {
        DeliveryState.DELIVERED_VIA_POST_REPORT,
        DeliveryState.DELIVERED_VIA_POST_ANALYSIS,
        DeliveryState.DELIVERED_VIA_LEGACY_SLACK_TOOL,
    }
    for s in DeliveryState:
        assert s.is_delivered() == (s in delivered), (
            f"{s} is_delivered() mismatch (expected {s in delivered})"
        )


# ─────────────────────────────────────────────────────────────────────────
# In-memory idempotency
# ─────────────────────────────────────────────────────────────────────────


def test_idempotent_in_memory(mock_db_won, mock_transition_reaction):
    """Second terminalize for same inv_id is a no-op — returns stored state."""
    first = terminalize_lifecycle(
        DeliveryState.DELIVERED_VIA_POST_REPORT,
        event_ts="1737654321.000100",
        channel_id="C123",
        inv_id=42,
    )
    assert first == DeliveryState.DELIVERED_VIA_POST_REPORT
    assert mock_db_won.call_count == 1
    assert mock_transition_reaction.call_count == 1

    # Second call with a DIFFERENT requested state should still return the
    # first-recorded state and NOT touch DB or Slack.
    second = terminalize_lifecycle(
        DeliveryState.TERMINAL_FAILURE,
        event_ts="1737654321.000100",
        channel_id="C123",
        inv_id=42,
    )
    assert second == DeliveryState.DELIVERED_VIA_POST_REPORT
    assert mock_db_won.call_count == 1  # NOT incremented
    assert mock_transition_reaction.call_count == 1  # NOT incremented


def test_in_memory_lru_eviction():
    """The bounded LRU evicts oldest entries past 256."""
    from lifecycle import _TERMINALIZED_MAX  # pyright: ignore[reportMissingImports]

    for i in range(_TERMINALIZED_MAX + 5):
        _remember_terminalized(i, DeliveryState.DELIVERED_VIA_POST_REPORT)

    # First 5 should be evicted
    assert _is_terminalized(0) is None
    assert _is_terminalized(4) is None
    # Latest entries remain
    assert (
        _is_terminalized(_TERMINALIZED_MAX + 4)
        == DeliveryState.DELIVERED_VIA_POST_REPORT
    )


# ─────────────────────────────────────────────────────────────────────────
# DB idempotency
# ─────────────────────────────────────────────────────────────────────────


def test_idempotent_via_db_guard_reconciles_from_persisted_status(
    mock_db_lost, mock_transition_reaction, monkeypatch
):
    """DB-loser reconciles reaction from persisted status (codex P2 fix).

    Pre-fix behavior: when the DB UPDATE matched 0 rows, the loser
    skipped the Slack flip entirely. If the winner died after its DB
    update but before its Slack flip, the user's message stayed stuck
    on ⏰ forever and recovery couldn't pick it up (row was already
    terminal). Post-fix: the loser reads the row's final status and
    flips the reaction based on what landed — best-effort
    reconciliation closes the deploy-overlap split-brain window.
    """
    # Mock get_investigation_by_id to return a 'failed' row — what the
    # winner persisted before dying.
    mock_get = MagicMock(return_value={"id": 99, "status": "failed"})
    monkeypatch.setattr("db_adapter.get_investigation_by_id", mock_get)

    result = terminalize_lifecycle(
        DeliveryState.TERMINAL_FAILURE,
        event_ts="1737654321.000100",
        channel_id="C123",
        inv_id=99,
    )
    # We tried but lost the DB race.
    assert mock_db_lost.call_count == 1
    # Reconciliation: we read the persisted status and flip the emoji.
    assert mock_get.call_count == 1
    assert mock_transition_reaction.call_count == 1
    # In-memory map records the reconciled state.
    assert _is_terminalized(99) == DeliveryState.TERMINAL_FAILURE


def test_db_won_flips_reaction_and_records(mock_db_won, mock_transition_reaction):
    """When DB UPDATE wins, Slack reaction flips and in-memory map records."""
    terminalize_lifecycle(
        DeliveryState.TERMINAL_FAILURE,
        event_ts="1737654321.000100",
        channel_id="C123",
        inv_id=7,
        error_message="test_error_detail",
    )
    mock_db_won.assert_called_once_with(7, "failed", error_message="test_error_detail")
    mock_transition_reaction.assert_called_once()
    call_args = mock_transition_reaction.call_args
    assert call_args.args[0] == "C123"
    assert call_args.args[1] == "1737654321.000100"
    assert call_args.kwargs.get("add") == "x"
    assert _is_terminalized(7) == DeliveryState.TERMINAL_FAILURE


# ─────────────────────────────────────────────────────────────────────────
# Defensive paths
# ─────────────────────────────────────────────────────────────────────────


def test_reaction_failure_does_not_revert_db(mock_db_won, monkeypatch):
    """Slack reaction flip failure must NOT roll back the DB row."""
    # transition_reaction returns False on Slack failure (already swallows
    # internally in real code) — verify we don't try to undo the DB update.
    mock_reaction = MagicMock(return_value=False)
    monkeypatch.setattr("slack_bot.transition_reaction", mock_reaction)

    result = terminalize_lifecycle(
        DeliveryState.TERMINAL_FAILURE,
        event_ts="1737654321.000100",
        channel_id="C123",
        inv_id=11,
    )
    # DB was updated (won the race)
    assert mock_db_won.call_count == 1
    # Slack was attempted and failed
    assert mock_reaction.call_count == 1
    # In-memory map still records — terminalization is durable.
    assert _is_terminalized(11) == DeliveryState.TERMINAL_FAILURE


def test_no_event_ts_skips_reaction(mock_db_won, mock_transition_reaction):
    """Cron flows with no event_ts skip the Slack flip but still update DB."""
    terminalize_lifecycle(
        DeliveryState.DELIVERED_VIA_POST_REPORT,
        event_ts=None,
        channel_id=None,
        inv_id=5,
    )
    assert mock_db_won.call_count == 1
    assert mock_transition_reaction.call_count == 0
    assert _is_terminalized(5) == DeliveryState.DELIVERED_VIA_POST_REPORT


def test_no_inv_id_skips_db(mock_transition_reaction, monkeypatch):
    """No inv_id (orphan terminalize) skips DB but still flips reaction."""
    mock_db = MagicMock()
    monkeypatch.setattr("db_adapter.update_investigation_atomic", mock_db)
    terminalize_lifecycle(
        DeliveryState.TERMINAL_FAILURE,
        event_ts="1737654321.000100",
        channel_id="C123",
        inv_id=None,
    )
    assert mock_db.call_count == 0  # No DB call without inv_id
    assert mock_transition_reaction.call_count == 1  # Reaction still flips


# ─────────────────────────────────────────────────────────────────────────
# NOT_DELIVERED invariant violation
# ─────────────────────────────────────────────────────────────────────────


def test_not_delivered_treated_as_terminal_failure(
    mock_db_won, mock_transition_reaction, caplog
):
    """NOT_DELIVERED at terminalize is invariant violation — logged, mapped to TERMINAL_FAILURE."""
    with caplog.at_level(logging.ERROR, logger="lifecycle"):
        terminalize_lifecycle(
            DeliveryState.NOT_DELIVERED,
            event_ts="1737654321.000100",
            channel_id="C123",
            inv_id=88,
        )
    # Error log fired
    assert any("invariant violation" in r.message.lower() for r in caplog.records)
    # DB updated as 'failed' (TERMINAL_FAILURE's db_status)
    mock_db_won.assert_called_once()
    args, kwargs = mock_db_won.call_args
    assert args[1] == "failed"
    assert "invariant_violation" in kwargs.get("error_message", "")
    # Recorded as TERMINAL_FAILURE in memory
    assert _is_terminalized(88) == DeliveryState.TERMINAL_FAILURE


# ─────────────────────────────────────────────────────────────────────────
# _run_investigation_guarded
# ─────────────────────────────────────────────────────────────────────────


def test_guarded_runner_passes_through_on_success(
    mock_db_won, mock_transition_reaction
):
    """Successful runner returns its value with no terminalization."""

    def runner():
        return "happy_value"

    result = _run_investigation_guarded(100, "1737654321.000100", "C123", runner)
    assert result == "happy_value"
    # No terminalize calls — the runner is responsible for terminalization
    # on its happy path.
    assert mock_db_won.call_count == 0
    assert mock_transition_reaction.call_count == 0


def test_guarded_runner_terminalizes_on_exception(
    mock_db_won, mock_transition_reaction
):
    """Uncaught exception flips ❌ and re-raises."""

    class BoomError(RuntimeError):
        pass

    def runner():
        raise BoomError("something exploded")

    with pytest.raises(BoomError):
        _run_investigation_guarded(200, "1737654321.000100", "C123", runner)

    # Terminalized as TERMINAL_FAILURE
    assert _is_terminalized(200) == DeliveryState.TERMINAL_FAILURE
    mock_db_won.assert_called_once()
    args, kwargs = mock_db_won.call_args
    assert args[1] == "failed"
    assert kwargs["error_message"] == "unhandled_exception:BoomError"
    mock_transition_reaction.assert_called_once()


def test_guarded_runner_terminalize_failure_does_not_mask_original(
    mock_db_won, monkeypatch
):
    """If terminalize_lifecycle ITSELF raises, the original exception still propagates."""
    # Force transition_reaction to explode
    mock_reaction = MagicMock(side_effect=RuntimeError("slack down"))
    monkeypatch.setattr("slack_bot.transition_reaction", mock_reaction)

    class BoomError(RuntimeError):
        pass

    def runner():
        raise BoomError("original exception")

    # Original exception is what bubbles out — terminalize_lifecycle's
    # failure is caught internally.
    with pytest.raises(BoomError, match="original exception"):
        _run_investigation_guarded(300, "1737654321.000100", "C123", runner)


# ─────────────────────────────────────────────────────────────────────────
# Cost-log unification (Theme A, 2026-05-16)
# ─────────────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_cost_log(monkeypatch):
    """Patch the cost-log helper at the lifecycle module level.

    We mock at this seam (not at ``_log_session_usage``) because
    ``_log_session_usage`` imports the anthropic SDK at module load time;
    the test environment doesn't have it installed. Mocking at the
    lifecycle seam tests the integration contract (does terminalize
    invoke cost-log at the right time with the right inv_id+state?)
    without depending on session_runner being importable.
    """
    mock = MagicMock(return_value=None)
    monkeypatch.setattr("lifecycle._log_cost_for_terminalized_inv", mock)
    return mock


def test_terminalize_logs_cost_once_for_won_db(
    mock_db_won, mock_transition_reaction, mock_cost_log
):
    """When DB UPDATE wins, the cost-log helper is invoked exactly once with
    the inv_id and the requested DeliveryState.
    """
    terminalize_lifecycle(
        DeliveryState.TERMINAL_FAILURE,
        event_ts="1737654321.000100",
        channel_id="C123",
        inv_id=500,
        error_message="unhandled_exception:ReadTimeout",
    )

    mock_cost_log.assert_called_once_with(500, DeliveryState.TERMINAL_FAILURE)


def test_terminalize_does_not_log_cost_when_db_lost(
    mock_db_lost, mock_transition_reaction, mock_cost_log, monkeypatch
):
    """DB UPDATE lost (another path already terminalized) → no cost log.
    The winning path already logged; double-logging would duplicate rows
    in session_costs.
    """
    # The DB-lost path also calls get_investigation_by_id for reconciliation.
    # Stub it so we don't hit the real DB.
    monkeypatch.setattr(
        "db_adapter.get_investigation_by_id",
        MagicMock(return_value={"id": 500, "status": "failed"}),
    )

    terminalize_lifecycle(
        DeliveryState.TERMINAL_FAILURE,
        event_ts="1737654321.000100",
        channel_id="C123",
        inv_id=500,
    )
    mock_cost_log.assert_not_called()


def test_terminalize_idempotency_logs_cost_only_on_first_call(
    mock_db_won, mock_transition_reaction, mock_cost_log
):
    """Three re-entries for same inv_id → cost logged exactly once.
    The in-memory idempotency check short-circuits before reaching the
    cost-log path on subsequent calls.
    """
    for _ in range(3):
        terminalize_lifecycle(
            DeliveryState.DELIVERED_VIA_POST_REPORT,
            event_ts="1737654321.000100",
            channel_id="C123",
            inv_id=500,
        )
    assert mock_cost_log.call_count == 1


def test_terminalize_skips_cost_log_when_no_inv_id(
    mock_db_won, mock_transition_reaction, mock_cost_log
):
    """No inv_id → no DB row to look up, no cost log.
    Cron-driven paths (dream/forecast) use their own _log_session_usage
    calls directly; terminalize_lifecycle without inv_id is a no-op for
    cost.
    """
    terminalize_lifecycle(
        DeliveryState.TERMINAL_FAILURE,
        event_ts=None,
        channel_id=None,
        inv_id=None,
    )
    mock_cost_log.assert_not_called()


def test_terminalize_swallows_cost_log_exceptions(
    mock_db_won, mock_transition_reaction, monkeypatch
):
    """Cost-log failure MUST NOT bubble or change the returned state.
    Observability bug should not corrupt the terminalize contract.
    """
    boom = MagicMock(side_effect=RuntimeError("session_costs DB down"))
    monkeypatch.setattr("lifecycle._log_cost_for_terminalized_inv", boom)

    result = terminalize_lifecycle(
        DeliveryState.DELIVERED_VIA_POST_REPORT,
        event_ts="1737654321.000100",
        channel_id="C123",
        inv_id=500,
    )
    # Returned state unchanged despite cost-log explosion
    assert result == DeliveryState.DELIVERED_VIA_POST_REPORT
    boom.assert_called_once()
    # In-memory idempotency still records — re-entry would be a no-op
    assert _is_terminalized(500) == DeliveryState.DELIVERED_VIA_POST_REPORT
