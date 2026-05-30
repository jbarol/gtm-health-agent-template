"""Tests for SSE auto-reconnect in session_runner._iter_session_events_with_reconnect.

Background: 2026-05-15 incident — two production investigations died on
``httpx.RemoteProtocolError`` raised by ``for event in stream:`` in
``_stream_and_handle``. The Anthropic-side SSE connection was idle for 76s
and 474s respectively before the drop. The generator under test wraps
``client.beta.sessions.events.stream()`` in a retry loop that backfills any
missed events via ``events.list(created_at_gt=...)`` and reopens the stream.

Run:
    cd orchestrator && python3 -m pytest session_runner_sse_reconnect_test.py -q
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import anthropic
import httpx
import pytest


def _ev(eid: str, etype: str = "agent.message", *, created_at: datetime | None = None):
    """Build a minimal event object with the fields the generator reads."""
    return SimpleNamespace(
        id=eid,
        type=etype,
        created_at=created_at or datetime(2026, 5, 15, 18, 0, 0, tzinfo=timezone.utc),
    )


def _stream_cm(events_iterable):
    """Build a fake context-manager that yields a fake stream of events.

    Mirrors the shape of ``client.beta.sessions.events.stream(...)``.
    """

    class _CM:
        def __enter__(self):
            return iter(events_iterable)

        def __exit__(self, exc_type, exc_val, exc_tb):
            return False

    return _CM()


def _stream_cm_then_raise(events_iterable, raise_exc):
    """Yield events then raise. Models a mid-stream disconnect."""

    def gen():
        for e in events_iterable:
            yield e
        raise raise_exc

    class _CM:
        def __enter__(self):
            return gen()

        def __exit__(self, exc_type, exc_val, exc_tb):
            # On exception, propagate
            return False

    return _CM()


def _list_resp(data, next_page=None):
    """Build a fake events.list response."""
    return SimpleNamespace(data=data, next_page=next_page)


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────


def test_happy_path_yields_all_events_and_returns():
    """No drop. Generator yields each event then stops when the stream closes."""
    import session_runner

    events = [_ev("e1"), _ev("e2"), _ev("e3")]
    mock_client = MagicMock()
    mock_client.beta.sessions.events.stream.return_value = _stream_cm(events)

    sleeps = []
    with patch.object(session_runner, "client", mock_client):
        gen = session_runner._iter_session_events_with_reconnect(
            session_id="sesn_EXAMPLE",
            initial_send_events=None,
            _sleep=sleeps.append,
        )
        out = list(gen)

    assert [e.id for e in out] == ["e1", "e2", "e3"]
    assert sleeps == []  # no reconnects, no backoff sleeps
    # stream was opened exactly once with a 120s read timeout
    assert mock_client.beta.sessions.events.stream.call_count == 1
    timeout_arg = mock_client.beta.sessions.events.stream.call_args.kwargs["timeout"]
    assert isinstance(timeout_arg, httpx.Timeout)
    assert timeout_arg.read == 120.0


def test_transient_drop_reconnects_and_backfills():
    """Stream drops mid-iteration; generator reopens via events.list + new stream."""
    import session_runner

    e_pre_drop = _ev("e1")  # delivered before the drop
    e_during_gap = _ev(
        "e2",
        created_at=datetime(2026, 5, 15, 18, 0, 5, tzinfo=timezone.utc),
    )
    e_post_reopen = _ev(
        "e3",
        created_at=datetime(2026, 5, 15, 18, 0, 10, tzinfo=timezone.utc),
    )

    drop = httpx.RemoteProtocolError(
        "peer closed connection without sending complete message body"
    )

    # First stream open: yield e1 then raise. Second open: yield e3.
    cm_first = _stream_cm_then_raise([e_pre_drop], drop)
    cm_second = _stream_cm([e_post_reopen])

    mock_client = MagicMock()
    mock_client.beta.sessions.events.stream.side_effect = [cm_first, cm_second]

    # events.list returns e2 (missed during the disconnect window) as a single page.
    mock_client.beta.sessions.events.list.return_value = _list_resp([e_during_gap])

    sleeps = []
    with patch.object(session_runner, "client", mock_client):
        gen = session_runner._iter_session_events_with_reconnect(
            session_id="sesn_EXAMPLE_test",
            initial_send_events=None,
            _sleep=sleeps.append,
        )
        out = list(gen)

    assert [e.id for e in out] == ["e1", "e2", "e3"], (
        "expected pre-drop event, then backfilled gap, then post-reopen event"
    )
    # Reconnect slept exactly once with backoff > 0
    assert len(sleeps) == 1
    assert 2.0 <= sleeps[0] <= 4.0  # base 2.0 + jitter [0, 1]
    # events.list was called with created_at_gt = the last seen event's ts
    list_call_kwargs = mock_client.beta.sessions.events.list.call_args.kwargs
    assert list_call_kwargs["session_id"] == "sesn_EXAMPLE_test"
    assert list_call_kwargs["created_at_gt"] == e_pre_drop.created_at
    assert list_call_kwargs["order"] == "asc"
    # Two stream opens (initial + reconnect)
    assert mock_client.beta.sessions.events.stream.call_count == 2


def test_initial_send_events_sent_only_on_first_open():
    """The kickoff events.send fires once. Reconnects MUST NOT re-send the kickoff."""
    import session_runner

    drop = httpx.ReadError("connection lost")
    cm_first = _stream_cm_then_raise([_ev("e1")], drop)
    cm_second = _stream_cm([_ev("e2")])

    mock_client = MagicMock()
    mock_client.beta.sessions.events.stream.side_effect = [cm_first, cm_second]
    mock_client.beta.sessions.events.list.return_value = _list_resp([])

    kickoff = [
        {
            "type": "user.message",
            "content": [{"type": "text", "text": "kickoff prompt"}],
        }
    ]

    sleeps = []
    with patch.object(session_runner, "client", mock_client):
        gen = session_runner._iter_session_events_with_reconnect(
            session_id="sesn_EXAMPLE_test",
            initial_send_events=kickoff,
            _sleep=sleeps.append,
        )
        out = list(gen)

    assert [e.id for e in out] == ["e1", "e2"]
    # events.send called EXACTLY once with the kickoff payload
    send_calls = mock_client.beta.sessions.events.send.call_args_list
    assert len(send_calls) == 1, (
        f"kickoff should fire once; got {len(send_calls)} calls — "
        "reconnect must not re-send the kickoff or it duplicates a user.message"
    )
    assert send_calls[0].kwargs["events"] == kickoff


def test_exhausted_attempts_reraises_last_exception():
    """After SSE_MAX_RECONNECT_ATTEMPTS failed attempts, re-raise."""
    import session_runner

    drop = httpx.RemoteProtocolError("persistent disconnect")
    # Every stream open raises immediately (no events yielded).
    cms = [
        _stream_cm_then_raise([], drop)
        for _ in range(session_runner.SSE_MAX_RECONNECT_ATTEMPTS + 1)
    ]

    mock_client = MagicMock()
    mock_client.beta.sessions.events.stream.side_effect = cms
    mock_client.beta.sessions.events.list.return_value = _list_resp([])

    sleeps = []
    with patch.object(session_runner, "client", mock_client):
        gen = session_runner._iter_session_events_with_reconnect(
            session_id="sesn_EXAMPLE_test",
            initial_send_events=None,
            _sleep=sleeps.append,
        )
        with pytest.raises(httpx.RemoteProtocolError):
            list(gen)

    # Backed off SSE_MAX_RECONNECT_ATTEMPTS times before giving up
    assert len(sleeps) == session_runner.SSE_MAX_RECONNECT_ATTEMPTS
    # Backoff was monotonically non-decreasing (jitter aside) — exponential schedule
    for prev, curr in zip(sleeps, sleeps[1:]):
        # Each sleep is base * 2^(n-1) + jitter; with jitter [0, 1], the
        # next sleep is at least the prev sleep's base portion - 1.
        assert curr >= prev - 1.0


def test_anthropic_internal_server_error_is_retried():
    """anthropic.InternalServerError is a transient signal (5xx) — retry, don't raise."""
    import session_runner

    # Build a real APIStatusError instance — uses internal SDK shape, so we
    # synthesize a minimal mock that quacks like one.
    iserr = anthropic.InternalServerError(
        message="500 server error",
        response=MagicMock(status_code=500, headers={}),
        body=None,
    )

    cm_first = _stream_cm_then_raise([_ev("e1")], iserr)
    cm_second = _stream_cm([_ev("e2")])

    mock_client = MagicMock()
    mock_client.beta.sessions.events.stream.side_effect = [cm_first, cm_second]
    mock_client.beta.sessions.events.list.return_value = _list_resp([])

    sleeps = []
    with patch.object(session_runner, "client", mock_client):
        gen = session_runner._iter_session_events_with_reconnect(
            session_id="sesn_EXAMPLE_test",
            initial_send_events=None,
            _sleep=sleeps.append,
        )
        out = list(gen)

    assert [e.id for e in out] == ["e1", "e2"]
    assert len(sleeps) == 1


def test_non_transient_exception_propagates_immediately():
    """Non-transient exceptions (e.g., ValueError from caller body) propagate without retry."""
    import session_runner

    cm = _stream_cm_then_raise([_ev("e1")], ValueError("real bug, not network"))
    mock_client = MagicMock()
    mock_client.beta.sessions.events.stream.return_value = cm
    mock_client.beta.sessions.events.list.return_value = _list_resp([])

    sleeps = []
    with patch.object(session_runner, "client", mock_client):
        gen = session_runner._iter_session_events_with_reconnect(
            session_id="sesn_EXAMPLE_err",
            initial_send_events=None,
            _sleep=sleeps.append,
        )
        with pytest.raises(ValueError, match="real bug"):
            list(gen)

    # No retries — ValueError isn't transient
    assert sleeps == []
    assert mock_client.beta.sessions.events.stream.call_count == 1


def test_backfill_pages_multiple_times():
    """events.list paginates through the gap; generator yields all pages in order."""
    import session_runner

    e_pre = _ev("e1", created_at=datetime(2026, 5, 15, 18, 0, 0, tzinfo=timezone.utc))
    drop = httpx.ReadError("oops")
    page_1 = [
        _ev("g1", created_at=datetime(2026, 5, 15, 18, 0, 1, tzinfo=timezone.utc)),
        _ev("g2", created_at=datetime(2026, 5, 15, 18, 0, 2, tzinfo=timezone.utc)),
    ]
    page_2 = [
        _ev("g3", created_at=datetime(2026, 5, 15, 18, 0, 3, tzinfo=timezone.utc)),
    ]
    e_post = _ev("e2", created_at=datetime(2026, 5, 15, 18, 0, 4, tzinfo=timezone.utc))

    cm_first = _stream_cm_then_raise([e_pre], drop)
    cm_second = _stream_cm([e_post])

    mock_client = MagicMock()
    mock_client.beta.sessions.events.stream.side_effect = [cm_first, cm_second]
    mock_client.beta.sessions.events.list.side_effect = [
        _list_resp(page_1, next_page="cursor_page2"),
        _list_resp(page_2, next_page=None),
    ]

    sleeps = []
    with patch.object(session_runner, "client", mock_client):
        gen = session_runner._iter_session_events_with_reconnect(
            session_id="sesn_EXAMPLE",
            initial_send_events=None,
            _sleep=sleeps.append,
        )
        out = list(gen)

    assert [e.id for e in out] == ["e1", "g1", "g2", "g3", "e2"]
    # First list call: no page cursor. Second: cursor_page2.
    list_calls = mock_client.beta.sessions.events.list.call_args_list
    assert len(list_calls) == 2
    assert "page" not in list_calls[0].kwargs
    assert list_calls[1].kwargs["page"] == "cursor_page2"
