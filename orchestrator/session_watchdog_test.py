"""Tests for orchestrator/session_watchdog.py (Plan: Design A).

Run:
    cd orchestrator && python3 -m pytest session_watchdog_test.py -v
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import session_watchdog


@pytest.fixture(autouse=True)
def _reset_state():
    """Each test gets a clean tier-state map."""
    session_watchdog._reset_state_for_tests()
    yield
    session_watchdog._reset_state_for_tests()


def _session(*, updated_seconds_ago: float, archived: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        archived_at=(datetime.now(timezone.utc) if archived else None),
        updated_at=datetime.now(timezone.utc) - timedelta(seconds=updated_seconds_ago),
    )


def _event(event_type: str, **kwargs):
    return SimpleNamespace(type=event_type, **kwargs)


def _idle_requires_action():
    return _event(
        "session.status_idle",
        stop_reason=SimpleNamespace(type="requires_action"),
    )


def test_evaluate_skips_archived_session():
    client = MagicMock()
    client.beta.sessions.retrieve.return_value = _session(
        updated_seconds_ago=9999, archived=True
    )
    v = session_watchdog.evaluate(
        inv_id=1, session_id="sesn_EXAMPLE", client=client, threshold_seconds=60
    )
    assert v.action == "skip"
    assert v.reason == "archived"


def test_evaluate_ok_when_within_threshold():
    client = MagicMock()
    client.beta.sessions.retrieve.return_value = _session(updated_seconds_ago=30)
    v = session_watchdog.evaluate(
        inv_id=1, session_id="sesn_EXAMPLE", client=client, threshold_seconds=600
    )
    assert v.action == "ok"
    assert v.reason == "within_threshold"


def test_evaluate_tier1_on_stranded_subagent():
    """The 17YiJ pattern: sent > received, parent stuck in requires_action."""
    client = MagicMock()
    client.beta.sessions.retrieve.return_value = _session(updated_seconds_ago=900)
    # 9 sent, 8 received → 1 stranded.
    events = [_idle_requires_action()] + (
        [_event("agent.thread_message_sent")] * 9
        + [_event("agent.thread_message_received")] * 8
    )
    client.beta.sessions.events.list.return_value = SimpleNamespace(data=events)
    v = session_watchdog.evaluate(
        inv_id=42, session_id="sesn_EXAMPLE", client=client, threshold_seconds=600
    )
    assert v.action == "tier1"
    assert v.sent == 9
    assert v.received == 8


def test_evaluate_ok_when_idle_with_terminal_stop_reason():
    """If the parent is idle with stop_reason != requires_action AND sent==received,
    the session terminated cleanly. Don't touch it."""
    client = MagicMock()
    client.beta.sessions.retrieve.return_value = _session(updated_seconds_ago=900)
    events = [
        _event(
            "session.status_idle",
            stop_reason=SimpleNamespace(type="end_turn"),
        )
    ] + [
        _event("agent.thread_message_sent"),
        _event("agent.thread_message_received"),
    ]
    client.beta.sessions.events.list.return_value = SimpleNamespace(data=events)
    v = session_watchdog.evaluate(
        inv_id=42, session_id="sesn_EXAMPLE", client=client, threshold_seconds=600
    )
    assert v.action == "ok"
    assert v.reason == "idle_terminal_stop"


def test_evaluate_escalates_to_tier2_after_tier1_wait():
    """After tier1 fires, the next stranded tick should wait then escalate to tier2."""
    # Seed tier1 state to mimic prior tick.
    monotonic_now = [1_000_000.0]
    session_watchdog._now = lambda: monotonic_now[0]
    session_watchdog._TIER_STATE[42] = session_watchdog.TierState(
        tier=1, last_action_at=monotonic_now[0] - 200
    )

    client = MagicMock()
    client.beta.sessions.retrieve.return_value = _session(updated_seconds_ago=900)
    events = [_idle_requires_action()] + (
        [_event("agent.thread_message_sent")] * 3
        + [_event("agent.thread_message_received")] * 2
    )
    client.beta.sessions.events.list.return_value = SimpleNamespace(data=events)

    v = session_watchdog.evaluate(
        inv_id=42, session_id="sesn_EXAMPLE", client=client, threshold_seconds=600
    )
    assert v.action == "tier2", v.reason


def test_evaluate_holds_during_tier_wait():
    """Tier-1 just fired; next tick within TIER_ESCALATION_SECONDS should wait."""
    monotonic_now = [1_000_000.0]
    session_watchdog._now = lambda: monotonic_now[0]
    session_watchdog._TIER_STATE[42] = session_watchdog.TierState(
        tier=1,
        last_action_at=monotonic_now[0] - 30,  # only 30s ago
    )

    client = MagicMock()
    client.beta.sessions.retrieve.return_value = _session(updated_seconds_ago=900)
    events = [_idle_requires_action()] + [_event("agent.thread_message_sent")]
    client.beta.sessions.events.list.return_value = SimpleNamespace(data=events)

    v = session_watchdog.evaluate(
        inv_id=42, session_id="sesn_EXAMPLE", client=client, threshold_seconds=600
    )
    assert v.action == "ok"
    assert "waiting" in v.reason


def test_evaluate_skips_on_retrieve_failure():
    client = MagicMock()
    client.beta.sessions.retrieve.side_effect = RuntimeError("boom")
    v = session_watchdog.evaluate(
        inv_id=1, session_id="sesn_EXAMPLE", client=client, threshold_seconds=60
    )
    assert v.action == "skip"
    assert "session_retrieve_failed" in v.reason


def test_tick_fires_tier1_and_records_state():
    """Full tick: stranded session → tier1 user.message sent + state recorded."""
    client = MagicMock()
    client.beta.sessions.retrieve.return_value = _session(updated_seconds_ago=900)
    client.beta.sessions.events.list.return_value = SimpleNamespace(
        data=[_idle_requires_action(), _event("agent.thread_message_sent")]
    )

    db = SimpleNamespace(
        list_running_investigations_for_container=lambda cid: [
            {
                "id": 7,
                "session_id": "sesn_EXAMPLE",
                "thread_ts": "T1",
                "channel_id": "C1",
                "event_ts": "evt-1",
            }
        ],
    )

    verdicts = session_watchdog.tick(
        client=client,
        db_adapter_mod=db,
        container_id="cont-1",
    )

    assert len(verdicts) == 1
    assert verdicts[0].action == "tier1"
    # The user.message inject must have been sent.
    sends = client.beta.sessions.events.send.call_args_list
    assert any(
        c.kwargs.get("session_id") == "sesn_EXAMPLE"
        and c.kwargs["events"][0]["type"] == "user.message"
        for c in sends
    )
    # State stored.
    assert session_watchdog._TIER_STATE[7].tier == 1


def test_tick_fires_tier3_terminate_path():
    """Tier 3: when prior tier was 2 + escalation elapsed, terminate path runs."""
    monotonic_now = [1_000_000.0]
    session_watchdog._now = lambda: monotonic_now[0]
    session_watchdog._TIER_STATE[7] = session_watchdog.TierState(
        tier=2, last_action_at=monotonic_now[0] - 300
    )

    client = MagicMock()
    client.beta.sessions.retrieve.return_value = _session(updated_seconds_ago=2000)
    client.beta.sessions.events.list.return_value = SimpleNamespace(
        data=[_idle_requires_action(), _event("agent.thread_message_sent")]
    )

    db = SimpleNamespace(
        list_running_investigations_for_container=lambda cid: [
            {
                "id": 7,
                "session_id": "sesn_EXAMPLE",
                "thread_ts": "T1",
                "channel_id": "C1",
                "event_ts": "evt-1",
            }
        ],
        mark_investigation_failed=MagicMock(),
    )
    notify = MagicMock()
    terminalize = MagicMock()
    archive = MagicMock()

    session_watchdog.tick(
        client=client,
        db_adapter_mod=db,
        container_id="cont-1",
        send_notification_fn=notify,
        terminalize_fn=terminalize,
        archive_session_fn=archive,
    )

    # All three terminal side effects fired.
    assert notify.call_count >= 1
    db.mark_investigation_failed.assert_called_once_with(
        7, error_message="watchdog_terminated_stalled_session"
    )
    archive.assert_called_once()
    terminalize.assert_called_once()


def test_count_dispatch_imbalance_handles_empty_list():
    sent, received = session_watchdog._count_dispatch_imbalance([])
    assert sent == 0 and received == 0


def test_last_event_is_requires_action_returns_false_when_no_idle_event():
    assert (
        session_watchdog._last_event_is_requires_action(
            [_event("agent.message"), _event("agent.tool_use")]
        )
        is False
    )


def test_session_age_seconds_handles_iso_string():
    s = SimpleNamespace(
        updated_at=(datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    )
    assert 100 <= session_watchdog._session_age_seconds(s) <= 140


def test_start_watchdog_respects_env_flag(monkeypatch):
    monkeypatch.setattr(session_watchdog, "WATCHDOG_ENABLED", False)
    out = session_watchdog.start_watchdog(
        client=MagicMock(),
        db_adapter_mod=SimpleNamespace(),
        container_id="cont-1",
    )
    assert out is None
