"""Plan #47 Workstream A — thread-followup 400 surfacing.

The thread-followup branch of ``run_adhoc_mcp_session`` posts a
``user.message`` to an existing Anthropic session. When that session has
unresolved ``requires_action`` events, Anthropic returns HTTP 400. Pre-fix,
the ``BadRequestError`` propagated uncaught through
``_iter_session_events_with_reconnect`` → ``_run_investigation_guarded`` →
``terminalize_lifecycle(TERMINAL_FAILURE)``, placing ❌ on the user's
follow-up with no Slack reply.

Workstream A converts the 400 to a private ``_FollowupBlocked`` sentinel,
caught above the lifecycle guard, and posts a Slack thread reply explaining
the deferral. Workstream B (retry queue) is NOT in scope here.

Tests below mock ``client.beta.sessions.events.send`` and
``send_notification`` to validate the four §3.6 acceptance behaviors.

Run:
    cd orchestrator && python3 -m pytest plan_47_workstream_a_test.py -v
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import anthropic
import httpx
import pytest


def _bad_request(
    message: str = "session has pending requires_action",
) -> anthropic.BadRequestError:
    """Build a BadRequestError with the response/body the SDK expects."""
    response = httpx.Response(
        status_code=400,
        request=httpx.Request("POST", "https://api.anthropic.com/v1/sessions/x/events"),
    )
    return anthropic.BadRequestError(
        message, response=response, body={"error": {"message": message}}
    )


def _internal_error(message: str = "boom") -> anthropic.InternalServerError:
    response = httpx.Response(
        status_code=500,
        request=httpx.Request("POST", "https://api.anthropic.com/v1/sessions/x/events"),
    )
    return anthropic.InternalServerError(
        message, response=response, body={"error": {"message": message}}
    )


def _stream_cm(events_iterable):
    """Fake ``client.beta.sessions.events.stream(...)`` context manager."""

    class _CM:
        def __enter__(self):
            return iter(events_iterable)

        def __exit__(self, exc_type, exc_val, exc_tb):
            return False

    return _CM()


def _end_turn_event():
    return SimpleNamespace(
        id="ev_end",
        type="session.status_idle",
        stop_reason=SimpleNamespace(type="end_turn"),
        created_at=None,
    )


# ---------------------------------------------------------------------------
# _iter_session_events_with_reconnect: followup_mode behavior
# ---------------------------------------------------------------------------


def test_iter_followup_mode_converts_400_to_sentinel():
    """Initial send raises 400 + followup_mode=True → raises _FollowupBlocked."""
    import session_runner

    mock_client = MagicMock()
    mock_client.beta.sessions.events.stream.return_value = _stream_cm([])
    mock_client.beta.sessions.events.send.side_effect = _bad_request()

    with patch.object(session_runner, "client", mock_client):
        gen = session_runner._iter_session_events_with_reconnect(
            session_id="sesn_EXAMPLE",
            initial_send_events=[
                {"type": "user.message", "content": [{"type": "text", "text": "Q"}]}
            ],
            followup_mode=True,
            _sleep=lambda *_: None,
        )
        with pytest.raises(session_runner._FollowupBlocked) as ei:
            list(gen)

    assert isinstance(ei.value.original_error, anthropic.BadRequestError)


def test_iter_followup_mode_non_retryable_400_propagates():
    """Codex P2 fix: 400 WITHOUT 'requires_action' in body still propagates raw.

    Only the pending-requires_action 400 is retryable. Non-retryable 400s
    (invalid event payload, expired session, permission denied) must fall
    through to the existing failure path so the user isn't told to retry
    into the same failure.
    """
    import session_runner

    mock_client = MagicMock()
    mock_client.beta.sessions.events.stream.return_value = _stream_cm([])
    mock_client.beta.sessions.events.send.side_effect = _bad_request(
        "invalid event payload format"
    )

    with patch.object(session_runner, "client", mock_client):
        gen = session_runner._iter_session_events_with_reconnect(
            session_id="sesn_EXAMPLE",
            initial_send_events=[
                {"type": "user.message", "content": [{"type": "text", "text": "Q"}]}
            ],
            followup_mode=True,  # even in followup_mode, non-retryable 400 propagates
            _sleep=lambda *_: None,
        )
        with pytest.raises(anthropic.BadRequestError):
            list(gen)


def test_is_requires_action_400_classifier():
    """Helper correctly identifies retryable vs non-retryable 400s."""
    from session_runner import _is_requires_action_400

    # Retryable
    assert (
        _is_requires_action_400(
            _bad_request("session has pending requires_action events")
        )
        is True
    )
    assert (
        _is_requires_action_400(_bad_request("Requires Action events outstanding"))
        is True
    )

    # Non-retryable
    assert _is_requires_action_400(_bad_request("invalid event payload")) is False
    assert _is_requires_action_400(_bad_request("session expired")) is False
    assert _is_requires_action_400(_bad_request("permission denied")) is False


def test_iter_default_mode_propagates_400():
    """followup_mode defaults to False → 400 propagates raw (no sentinel)."""
    import session_runner

    mock_client = MagicMock()
    mock_client.beta.sessions.events.stream.return_value = _stream_cm([])
    mock_client.beta.sessions.events.send.side_effect = _bad_request()

    with patch.object(session_runner, "client", mock_client):
        gen = session_runner._iter_session_events_with_reconnect(
            session_id="sesn_EXAMPLE",
            initial_send_events=[
                {"type": "user.message", "content": [{"type": "text", "text": "Q"}]}
            ],
            _sleep=lambda *_: None,
        )
        with pytest.raises(anthropic.BadRequestError):
            list(gen)


# ---------------------------------------------------------------------------
# Workstream A.2 (2026-05-19) — wrap also catches stream-init 400s
# ---------------------------------------------------------------------------


def test_iter_followup_mode_converts_stream_init_400_to_sentinel():
    """Stream context-manager entry raises 400 + followup_mode=True → _FollowupBlocked.

    Live repro inv 59 (sesn_EXAMPLE, 2026-05-19 04:57:32 UTC):
    a thread follow-up hit 400 on stream init when the session still had
    pending requires_action events from the prior stalled turn. Workstream A
    only wrapped events.send; the stream() __enter__ raised raw BadRequestError
    that bypassed the sentinel and terminalized as ❌. A.2 extends the wrap.
    """
    import session_runner

    mock_client = MagicMock()
    # Simulate the SDK raising 400 on the GET that opens the stream.
    mock_client.beta.sessions.events.stream.side_effect = _bad_request(
        "session has pending requires_action events"
    )

    with patch.object(session_runner, "client", mock_client):
        gen = session_runner._iter_session_events_with_reconnect(
            session_id="sesn_EXAMPLE",
            initial_send_events=[
                {"type": "user.message", "content": [{"type": "text", "text": "Q"}]}
            ],
            followup_mode=True,
            _sleep=lambda *_: None,
        )
        with pytest.raises(session_runner._FollowupBlocked) as ei:
            list(gen)

    assert isinstance(ei.value.original_error, anthropic.BadRequestError)
    # And events.send was never called (stream entry blocked us first).
    mock_client.beta.sessions.events.send.assert_not_called()


def test_iter_followup_mode_stream_init_non_retryable_400_propagates():
    """Stream-init 400 WITHOUT 'requires_action' still propagates raw.

    Symmetric with the events.send filter (test_iter_followup_mode_non_retryable_400_propagates).
    Only the pending-requires_action 400 is retryable; an "invalid request"
    or "session expired" 400 must fall through so the user isn't told to
    retry into the same deterministic failure.
    """
    import session_runner

    mock_client = MagicMock()
    mock_client.beta.sessions.events.stream.side_effect = _bad_request(
        "session expired"
    )

    with patch.object(session_runner, "client", mock_client):
        gen = session_runner._iter_session_events_with_reconnect(
            session_id="sesn_EXAMPLE",
            initial_send_events=[
                {"type": "user.message", "content": [{"type": "text", "text": "Q"}]}
            ],
            followup_mode=True,
            _sleep=lambda *_: None,
        )
        with pytest.raises(anthropic.BadRequestError):
            list(gen)


def test_sse_max_reconnect_attempts_sized_above_watchdog_tier_3():
    """SSE budget must exceed watchdog Tier 3 timing so the watchdog wins.

    Watchdog timeline: Tier 1 nudge at ~11 min, Tier 2 interrupt ~13 min,
    Tier 3 terminate ~15 min. SSE budget = ATTEMPTS × READ_TIMEOUT + backoff.
    At 7 × 120 + ~90s backoff = 15.5 min, SSE exhausts AFTER Tier 3 — only
    fires if the watchdog itself is dead. Lowering this constant below 6
    re-introduces the race that left sub3 inv 58 stranded on 2026-05-19.
    """
    from session_runner import (
        SSE_BASE_BACKOFF_S,
        SSE_MAX_BACKOFF_S,
        SSE_MAX_RECONNECT_ATTEMPTS,
        SSE_READ_TIMEOUT_S,
    )

    # Worst-case total budget: each attempt waits up to read_timeout,
    # then sleeps up to MAX_BACKOFF before the next attempt.
    backoff_total = sum(
        min(SSE_BASE_BACKOFF_S * (2**i), SSE_MAX_BACKOFF_S)
        for i in range(SSE_MAX_RECONNECT_ATTEMPTS)
    )
    budget_seconds = SSE_MAX_RECONNECT_ATTEMPTS * SSE_READ_TIMEOUT_S + backoff_total
    # Watchdog Tier 3 fires at STALL_THRESHOLD (600) + WATCHDOG_POLL (60)
    # + 2× TIER_ESCALATION (120) = 900s. SSE budget must clear it.
    watchdog_tier_3_seconds = 600 + 60 + 2 * 120
    assert budget_seconds > watchdog_tier_3_seconds, (
        f"SSE budget {budget_seconds:.0f}s must exceed watchdog Tier 3 "
        f"{watchdog_tier_3_seconds}s — see Plan #47 Workstream A.2 rationale."
    )


# ---------------------------------------------------------------------------
# §3.6 acceptance tests
# ---------------------------------------------------------------------------


def test_followup_400_posts_slack_notice():
    """A 400 on the follow-up send produces a Slack thread reply AND
    terminalizes the inv_id (codex P2 fix, 2026-05-18).

    The lifecycle GUARD (around `_run_investigation_guarded`) still does
    NOT fire — see `test_lifecycle_guard_passes_through_followup_blocked`.
    The helper itself now terminalizes the inv_id directly so the row
    doesn't sit stuck in 'running' until Workstream B's retry queue
    consumes it (B isn't shipped yet).

    Plan #52 PR-A (2026-05-19): event_ts is passed as None to
    terminalize_lifecycle so no ❌ is added — the 👁 reaction stays,
    consistent with the "On it" ack already posted. See
    `test_followup_blocked_no_reaction_flip` for the dedicated assertion.
    """
    import session_runner

    notifications = MagicMock(return_value="1234.5678")
    terminalize = MagicMock()

    with (
        patch.object(session_runner, "send_notification", notifications),
        patch("lifecycle.terminalize_lifecycle", terminalize),
    ):
        session_runner._handle_followup_blocked(
            session_id="sesn_EXAMPLE",
            thread_ts="1779.999",
            channel_id="C0",
            user_id="U0",
            event_ts="1779.000",
            inv_id=42,
            error=_bad_request(),
        )

    notifications.assert_called_once()
    args, kwargs = notifications.call_args
    assert args[0] == "watch"
    assert "Your follow-up was received" in args[1]
    assert "Retry in 30s" in args[1]
    assert kwargs["reply_to"] == "1779.999"
    assert kwargs["channel"] == "C0"
    assert kwargs["requester_id"] == "U0"
    # Codex P2 fix: helper terminalizes the inv_id so it doesn't sit stuck
    terminalize.assert_called_once()
    targs, tkwargs = terminalize.call_args
    # State is TERMINAL_FAILURE with the diagnostic error_message
    from lifecycle import DeliveryState

    assert targs[0] == DeliveryState.TERMINAL_FAILURE
    assert tkwargs["inv_id"] == 42
    # Plan #52 PR-A: event_ts=None so no ❌ reaction is added
    assert tkwargs["event_ts"] is None
    assert tkwargs["channel_id"] == "C0"
    assert "followup_blocked" in tkwargs["error_message"]


def test_followup_400_terminalizes_db_row():
    """After a 400, the inv_id row is terminalized to TERMINAL_FAILURE
    via terminalize_lifecycle so it doesn't sit stuck in 'running'.

    Plan #52 PR-A (2026-05-19): supersedes the prior
    `test_followup_400_flips_reaction_via_terminalize` — the helper no
    longer flips the user's message reaction (event_ts=None), but the
    DB row still terminalizes. Workstream B will swap TERMINAL_FAILURE
    → followup_pending so the retry queue can move it back to 'running'
    on success; until then TERMINAL_FAILURE + a diagnostic error_message
    keeps the bookkeeping honest.
    """
    import session_runner

    notifications = MagicMock(return_value="1234.5678")
    terminalize = MagicMock()

    with (
        patch.object(session_runner, "send_notification", notifications),
        patch("lifecycle.terminalize_lifecycle", terminalize),
    ):
        session_runner._handle_followup_blocked(
            session_id="sesn_EXAMPLE",
            thread_ts="1779.999",
            channel_id="C0",
            user_id="U0",
            event_ts="1779.000",
            inv_id=42,
            error=_bad_request(),
        )

    terminalize.assert_called_once()


def test_followup_blocked_no_reaction_flip():
    """Plan #52 PR-A: _handle_followup_blocked passes event_ts=None to
    terminalize_lifecycle so the user's follow-up message keeps its 👁
    reaction (consistent with the "On it" kickoff ack) instead of
    flipping to ❌.

    Live repro 2026-05-19 Acme channel: Jared replied "continue" in
    an active investigation thread; the bot posted the "On it" ack AND
    added a ❌ reaction. The "On it" came from the kickoff ack; the ❌
    came from this helper's terminalize_lifecycle call when the
    follow-up 400'd against a session in `requires_action`. Removing the
    reaction flip is the safe, minimal fix — the DB row still cancels.
    """
    import session_runner

    notifications = MagicMock(return_value="1234.5678")
    terminalize = MagicMock()

    # _handle_followup_blocked imports terminalize_lifecycle lazily from
    # the lifecycle module inside the function; patch there so the call
    # routes through the mock.
    with (
        patch.object(session_runner, "send_notification", notifications),
        patch("lifecycle.terminalize_lifecycle", terminalize),
    ):
        session_runner._handle_followup_blocked(
            session_id="sesn_EXAMPLE",
            thread_ts="1779.999",
            channel_id="C0",
            user_id="U0",
            event_ts="1234567890.123456",
            inv_id=42,
            error=_bad_request(),
        )

    terminalize.assert_called_once()
    _targs, tkwargs = terminalize.call_args
    assert tkwargs["event_ts"] is None, (
        "Plan #52 PR-A requires event_ts=None so no ❌ reaction is added; "
        f"got event_ts={tkwargs['event_ts']!r}"
    )


def test_followup_blocked_removes_working_reaction():
    """Plan #52 PR-A (codex P2 fix, 2026-05-19): _handle_followup_blocked
    must explicitly remove the ⏰ working reaction before calling
    terminalize_lifecycle with event_ts=None.

    Without this, the prior fix's event_ts=None caused terminalize_lifecycle
    to skip ALL reaction handling, leaving the user's message stuck on ⏰
    forever. The desired end state is: 👁 receipt stays, ⏰ working removed,
    no ❌ added.
    """
    import session_runner

    notifications = MagicMock(return_value="1234.5678")
    terminalize = MagicMock()
    remove_reaction_mock = MagicMock()

    with (
        patch.object(session_runner, "send_notification", notifications),
        patch("lifecycle.terminalize_lifecycle", terminalize),
        patch("slack_bot.remove_reaction", remove_reaction_mock),
    ):
        session_runner._handle_followup_blocked(
            session_id="sesn_EXAMPLE",
            thread_ts="1779.999",
            channel_id="C0",
            user_id="U0",
            event_ts="1234567890.123456",
            inv_id=42,
            error=_bad_request(),
        )

    # remove_reaction called with the working emoji on the user's event_ts.
    remove_reaction_mock.assert_called_once()
    rargs = remove_reaction_mock.call_args[0]
    assert rargs[0] == "C0", f"channel_id expected 'C0', got {rargs[0]!r}"
    assert rargs[1] == "1234567890.123456", (
        f"event_ts expected '1234567890.123456', got {rargs[1]!r}"
    )
    # The third positional arg is the REACTION_WORKING constant ("alarm_clock").
    from slack_bot import REACTION_WORKING

    assert rargs[2] == REACTION_WORKING, (
        f"emoji expected REACTION_WORKING ({REACTION_WORKING!r}), got {rargs[2]!r}"
    )


def test_followup_blocked_skips_reaction_removal_without_event_ts():
    """Defense-in-depth: when event_ts is None (e.g., recovery path), the
    helper must not attempt to remove a reaction it cannot target. The DB
    terminalize still fires."""
    import session_runner

    notifications = MagicMock(return_value="1234.5678")
    terminalize = MagicMock()
    remove_reaction_mock = MagicMock()

    with (
        patch.object(session_runner, "send_notification", notifications),
        patch("lifecycle.terminalize_lifecycle", terminalize),
        patch("slack_bot.remove_reaction", remove_reaction_mock),
    ):
        session_runner._handle_followup_blocked(
            session_id="sesn_EXAMPLE",
            thread_ts="1779.999",
            channel_id="C0",
            user_id="U0",
            event_ts=None,
            inv_id=42,
            error=_bad_request(),
        )

    remove_reaction_mock.assert_not_called()
    terminalize.assert_called_once()


def test_followup_terminalize_failure_does_not_propagate():
    """If terminalize_lifecycle raises, the helper logs and swallows.

    The Slack notice has already been posted; a DB or Slack reaction
    failure during terminalize is a bookkeeping leak, not a user-facing
    failure. Surfacing the exception here would propagate up through
    `run_adhoc_mcp_session` and re-terminalize via the guard's
    `except BaseException` path — defeating the carve-out.
    """
    import session_runner

    notifications = MagicMock(return_value="1234.5678")
    terminalize = MagicMock(side_effect=RuntimeError("db down"))

    with (
        patch.object(session_runner, "send_notification", notifications),
        patch("lifecycle.terminalize_lifecycle", terminalize),
    ):
        # Must NOT raise — helper is the last-mile boundary
        session_runner._handle_followup_blocked(
            session_id="sesn_EXAMPLE",
            thread_ts="1779.999",
            channel_id="C0",
            user_id="U0",
            event_ts="1779.000",
            inv_id=42,
            error=_bad_request(),
        )

    notifications.assert_called_once()
    terminalize.assert_called_once()


def test_followup_200_no_notice_posted():
    """Happy-path follow-up: send returns 200, generator yields normally,
    and _handle_followup_blocked is NEVER reached.
    """
    import session_runner

    mock_client = MagicMock()
    mock_client.beta.sessions.events.stream.return_value = _stream_cm(
        [_end_turn_event()]
    )
    mock_client.beta.sessions.events.send.return_value = None  # 200 OK

    notifications = MagicMock()
    with (
        patch.object(session_runner, "client", mock_client),
        patch.object(session_runner, "send_notification", notifications),
    ):
        gen = session_runner._iter_session_events_with_reconnect(
            session_id="sesn_EXAMPLE",
            initial_send_events=[
                {"type": "user.message", "content": [{"type": "text", "text": "Q"}]}
            ],
            followup_mode=True,
            _sleep=lambda *_: None,
        )
        events = list(gen)

    assert len(events) == 1
    mock_client.beta.sessions.events.send.assert_called_once()
    notifications.assert_not_called()


def test_followup_non_400_error_propagates():
    """A 500 / InternalServerError on the follow-up send must NOT be
    converted to _FollowupBlocked. Existing reconnect logic (retry then
    fall through to lifecycle guard) is preserved.
    """
    import session_runner

    mock_client = MagicMock()
    mock_client.beta.sessions.events.stream.return_value = _stream_cm([])
    mock_client.beta.sessions.events.send.side_effect = _internal_error()

    with patch.object(session_runner, "client", mock_client):
        gen = session_runner._iter_session_events_with_reconnect(
            session_id="sesn_EXAMPLE",
            initial_send_events=[
                {"type": "user.message", "content": [{"type": "text", "text": "Q"}]}
            ],
            followup_mode=True,
            max_attempts=1,  # exhaust quickly
            _sleep=lambda *_: None,
        )
        # Must NOT raise _FollowupBlocked. InternalServerError is in
        # SSE_TRANSIENT_EXCEPTIONS so the generator retries up to
        # max_attempts and re-raises the original exception.
        with pytest.raises(anthropic.InternalServerError):
            list(gen)


def test_lifecycle_guard_passes_through_followup_blocked():
    """_FollowupBlocked must propagate through _run_investigation_guarded
    WITHOUT calling terminalize_lifecycle.

    Without this carve-out the guard's `except BaseException` would mark
    the user's message ❌ before the outer follow-up branch could post
    its Slack notice.
    """
    import lifecycle
    import session_runner

    def _raiser(*_args, **_kwargs):
        raise session_runner._FollowupBlocked(_bad_request())

    terminalize = MagicMock()
    with patch("lifecycle.terminalize_lifecycle", terminalize):
        with pytest.raises(session_runner._FollowupBlocked):
            lifecycle._run_investigation_guarded(
                42,
                "1779.999",
                "C0",
                _raiser,
            )

    terminalize.assert_not_called()


def test_lifecycle_guard_still_terminalizes_other_exceptions():
    """Sanity: the carve-out does NOT short-circuit unrelated exceptions.
    Existing TERMINAL_FAILURE path for non-_FollowupBlocked errors is
    preserved.
    """
    import lifecycle

    def _raiser(*_args, **_kwargs):
        raise RuntimeError("unrelated boom")

    terminalize = MagicMock()
    with patch("lifecycle.terminalize_lifecycle", terminalize):
        with pytest.raises(RuntimeError):
            lifecycle._run_investigation_guarded(
                42,
                "1779.999",
                "C0",
                _raiser,
            )

    terminalize.assert_called_once()
