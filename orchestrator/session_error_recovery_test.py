"""Tests for the terminal session.error → Slack recovery message helper.

Verifies _post_session_error_to_slack posts a terse, actionable Slack message
in the user's thread when a session terminates with a context-budget overrun
or other terminal error, and that _suggested_next_action maps each error class
to the right remedy.

Motivation: session sesn_EXAMPLE (2026-05-11) crossed the 1M
token cap and died silently. The user saw only the 7-hour-old ack and no
follow-up. This regression test prevents that pattern from returning.

Also covers the `session.status_rescheduled` handler added off the 2026-05-11
self-improve report (transient Anthropic retries must NOT break the event
loop and must NOT post to Slack — the investigation is still progressing).

Run:
    cd orchestrator && python3 -m pytest session_error_recovery_test.py
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# _suggested_next_action — error-class → remedy mapping
# ---------------------------------------------------------------------------


def test_suggested_next_action_prompt_too_long():
    from session_runner import _suggested_next_action

    msg = "prompt is too long: 1,119,846 > 1,000,000 maximum"
    out = _suggested_next_action(msg)
    assert "narrower date range" in out
    assert "LIMIT" in out
    assert "aggregate" in out


def test_suggested_next_action_rate_limit():
    from session_runner import _suggested_next_action

    out = _suggested_next_action("rate_limit_error: too many requests")
    assert "Rate-limited" in out
    assert "retry" in out.lower()


def test_suggested_next_action_overloaded():
    from session_runner import _suggested_next_action

    out = _suggested_next_action("model is overloaded — 503")
    assert "overloaded" in out.lower()
    assert "retry" in out.lower()


def test_suggested_next_action_unknown_defaults_to_logs():
    from session_runner import _suggested_next_action

    out = _suggested_next_action("some weird unknown failure")
    assert "logs" in out.lower()


def test_suggested_next_action_empty_string():
    from session_runner import _suggested_next_action

    out = _suggested_next_action("")
    assert "logs" in out.lower()


# ---------------------------------------------------------------------------
# _post_session_error_to_slack — Slack post behavior
# ---------------------------------------------------------------------------


def test_post_session_error_posts_to_thread():
    """Terminal error with a thread_ts → calls send_notification with reply_to."""
    from session_runner import _post_session_error_to_slack

    with patch("session_runner.send_notification") as mock_send:
        mock_send.return_value = "ts-recovery"
        ts = _post_session_error_to_slack(
            thread_ts="1234567890.000100",
            error_type="unknown_error",
            error_message="prompt is too long: 1,119,846 > 1,000,000",
            session_id="sesn_EXAMPLE",
        )

    assert ts == "ts-recovery"
    mock_send.assert_called_once()
    kwargs = mock_send.call_args.kwargs
    assert kwargs["severity"] == "watch"
    assert kwargs["reply_to"] == "1234567890.000100"
    summary = kwargs["summary"]
    # Session id is surfaced so the user (or an admin) can grep the logs.
    assert "sesn_EXAMPLE" in summary
    # Original error type is surfaced.
    assert "unknown_error" in summary
    # The user-facing remedy text is included.
    assert "narrower date range" in summary
    # The headline is the "Investigation halted" cue, not a generic error.
    assert "Investigation halted" in summary


def test_post_session_error_returns_empty_on_no_thread():
    """No thread_ts → no Slack call, return empty string."""
    from session_runner import _post_session_error_to_slack

    with patch("session_runner.send_notification") as mock_send:
        ts = _post_session_error_to_slack(
            thread_ts="",
            error_type="unknown_error",
            error_message="prompt is too long",
            session_id="sesn_EXAMPLE",
        )

    assert ts == ""
    mock_send.assert_not_called()


def test_post_session_error_swallows_slack_exception():
    """A Slack failure during recovery-post must not raise — log only."""
    from session_runner import _post_session_error_to_slack

    with patch("session_runner.send_notification") as mock_send:
        mock_send.side_effect = RuntimeError("slack down")
        ts = _post_session_error_to_slack(
            thread_ts="thread-x",
            error_type="unknown_error",
            error_message="prompt is too long",
            session_id="sesn_EXAMPLE",
        )

    assert ts == ""  # graceful degrade


def test_post_session_error_trims_long_messages():
    """Very long error messages get trimmed so Slack doesn't reject the post."""
    from session_runner import _post_session_error_to_slack

    huge_msg = "x" * 5000
    with patch("session_runner.send_notification") as mock_send:
        mock_send.return_value = "ts-trim"
        _post_session_error_to_slack(
            thread_ts="thread-x",
            error_type="unknown_error",
            error_message=huge_msg,
            session_id="sesn_EXAMPLE",
        )

    summary = mock_send.call_args.kwargs["summary"]
    # The error body should be capped well below the original 5000 chars.
    assert len(summary) < 1000
    assert "..." in summary


def test_post_session_error_rate_limit_remedy():
    """Rate-limit errors get the "wait and retry" remedy, not "narrow scope"."""
    from session_runner import _post_session_error_to_slack

    with patch("session_runner.send_notification") as mock_send:
        mock_send.return_value = "ts-rate"
        _post_session_error_to_slack(
            thread_ts="thread-x",
            error_type="rate_limit_error",
            error_message="rate_limit: too many requests",
            session_id="sesn_EXAMPLE",
        )

    summary = mock_send.call_args.kwargs["summary"]
    assert "Rate-limited" in summary
    # The "narrow scope" advice should NOT appear for a rate-limit error.
    assert "narrower date range" not in summary


# ---------------------------------------------------------------------------
# session.status_rescheduled handler — transient retries must NOT break loop
# ---------------------------------------------------------------------------
#
# Per the Managed Agents docs (events-and-streaming):
#   "session.status_rescheduled — A transient error occurred and the session is
#    retrying automatically."
#
# Translation: Anthropic is retrying the request internally, the session is
# still alive, and the next event will be a normal status_running / agent.message.
# The orchestrator must:
#   - Continue iterating the event stream (do NOT break)
#   - NOT post anything to Slack (the user's investigation is still progressing)
#   - Log at INFO so the event is visible without triggering ops noise


def _make_event(event_type: str, **kwargs):
    return SimpleNamespace(type=event_type, **kwargs)


def _make_stream(events):
    class _FakeStream:
        def __enter__(self):
            return iter(events)

        def __exit__(self, exc_type, exc, tb):
            return False

    return _FakeStream()


def test_status_rescheduled_continues_loop_no_slack(caplog):
    """A status_rescheduled event must NOT break the loop, NOT post to Slack,
    and must emit an INFO log so the retry is visible in session logs."""
    from session_runner import _stream_and_handle

    rescheduled = _make_event("session.status_rescheduled")
    agent_msg = _make_event(
        "agent.message",
        content=[SimpleNamespace(text="continuing work after retry")],
    )
    end_event = _make_event(
        "session.status_idle",
        stop_reason=SimpleNamespace(type="end_turn"),
    )

    fake_stream_cm = _make_stream([rescheduled, agent_msg, end_event])

    with (
        patch("session_runner.client") as mock_client,
        patch("session_runner.send_notification") as mock_send,
        caplog.at_level(logging.INFO, logger="session_runner"),
    ):
        mock_client.beta.sessions.events.stream.return_value = fake_stream_cm
        mock_client.beta.sessions.events.send = MagicMock()

        text_parts, posted, error_type, _ = _stream_and_handle(
            session_id="sesn_EXAMPLE_1",
            send_events=None,
            thread_ts="thread-rescheduled-1",
            verbosity="summary",
        )

    # The loop did NOT break on the rescheduled event — we got to the
    # agent.message that followed and accumulated its text.
    assert "continuing work after retry" in "".join(text_parts), (
        "Loop broke on session.status_rescheduled — events after the retry were dropped"
    )
    # No Slack post on a transient retry. The user's ack is still valid.
    mock_send.assert_not_called()
    # No error_type captured.
    assert error_type is None
    # And the loop did not promote delivery state (no post happened).
    # Post-refactor (2026-05-13) ``posted`` is a DeliveryState enum;
    # ``is_delivered()`` is False for NOT_DELIVERED / TERMINAL_FAILURE /
    # USER_CANCELLED / NO_OUTPUT.
    assert not posted.is_delivered()
    # INFO log emitted, with session id and the "transient retry" cue.
    rescheduled_logs = [
        r for r in caplog.records if "rescheduled" in r.getMessage().lower()
    ]
    assert rescheduled_logs, "No log emitted for session.status_rescheduled"
    rec = rescheduled_logs[0]
    assert rec.levelno == logging.INFO, (
        f"Expected INFO for status_rescheduled, got {rec.levelname}"
    )
    msg = rec.getMessage()
    assert "sesn_EXAMPLE_1" in msg
    assert "transient retry" in msg.lower()
    assert "continuing event loop" in msg.lower()


def test_status_rescheduled_followed_by_terminated_breaks_cleanly():
    """If a retry eventually fails with status_terminated, the loop should
    break on terminated — confirming both events handle their own role."""
    from session_runner import _stream_and_handle

    rescheduled = _make_event("session.status_rescheduled")
    terminated = _make_event("session.status_terminated")
    # Add a trailing agent.message that should NOT be processed (loop
    # already broke on terminated).
    trailing = _make_event(
        "agent.message",
        content=[SimpleNamespace(text="should not be reached")],
    )

    fake_stream_cm = _make_stream([rescheduled, terminated, trailing])

    with (
        patch("session_runner.client") as mock_client,
        patch("session_runner.send_notification") as mock_send,
    ):
        mock_client.beta.sessions.events.stream.return_value = fake_stream_cm
        mock_client.beta.sessions.events.send = MagicMock()

        text_parts, _, error_type, _ = _stream_and_handle(
            session_id="sesn_EXAMPLE_then_terminated",
            send_events=None,
            thread_ts="thread-x",
            verbosity="summary",
        )

    # Loop broke on terminated — the trailing message was not consumed.
    assert "should not be reached" not in "".join(text_parts)
    # Terminated by itself doesn't trigger the recovery Slack post (only
    # session.error does — see PR #35 fix C and existing tests above).
    mock_send.assert_not_called()
    assert error_type is None


def test_session_error_still_breaks_loop_after_change():
    """Sanity check: making status_rescheduled non-terminal did not weaken
    the session.error handler. A terminal error still breaks the loop AND
    posts the recovery message to Slack."""
    from session_runner import _stream_and_handle

    err_event = _make_event(
        "session.error",
        error=SimpleNamespace(
            type="unknown_error",
            message="prompt is too long: 1,119,846 > 1,000,000",
            retry_status=SimpleNamespace(type="exhausted"),
        ),
    )
    # A trailing agent.message that should NOT be reached.
    trailing = _make_event(
        "agent.message",
        content=[SimpleNamespace(text="should not be reached")],
    )

    fake_stream_cm = _make_stream([err_event, trailing])

    with (
        patch("session_runner.client") as mock_client,
        patch("session_runner.send_notification") as mock_send,
    ):
        mock_client.beta.sessions.events.stream.return_value = fake_stream_cm
        mock_client.beta.sessions.events.send = MagicMock()
        mock_send.return_value = "ts-recovery"

        text_parts, _, error_type, _ = _stream_and_handle(
            session_id="sesn_EXAMPLE_err",
            send_events=None,
            thread_ts="thread-terminal",
            verbosity="summary",
        )

    # Loop broke on session.error — trailing event not processed.
    assert "should not be reached" not in "".join(text_parts)
    # Recovery message went to Slack.
    mock_send.assert_called_once()
    assert mock_send.call_args.kwargs["reply_to"] == "thread-terminal"
    assert error_type == "unknown_error"


def test_status_idle_end_turn_still_breaks_after_change():
    """Sanity check: end-of-turn idle event still terminates the loop
    normally after the dispatcher additions."""
    from session_runner import _stream_and_handle

    agent_msg = _make_event(
        "agent.message",
        content=[SimpleNamespace(text="final answer")],
    )
    end_event = _make_event(
        "session.status_idle",
        stop_reason=SimpleNamespace(type="end_turn"),
    )

    fake_stream_cm = _make_stream([agent_msg, end_event])

    with (
        patch("session_runner.client") as mock_client,
        patch("session_runner.send_notification") as mock_send,
    ):
        mock_client.beta.sessions.events.stream.return_value = fake_stream_cm
        mock_client.beta.sessions.events.send = MagicMock()

        text_parts, posted, error_type, _ = _stream_and_handle(
            session_id="sesn_EXAMPLE_end",
            send_events=None,
            thread_ts="thread-normal",
            verbosity="summary",
        )

    assert "final answer" in "".join(text_parts)
    assert error_type is None
    # Post-refactor: ``posted`` is a DeliveryState enum.
    assert not posted.is_delivered()
    mock_send.assert_not_called()
