"""Integration tests closing autoplan test gaps #12 and #13.

#12 — post_report duplicate suppression (E3 fix verification).
    The fix in `_stream_and_handle` flips `agent_posted_to_slack=True` for
    `post_report` events in addition to `send_slack_notification`. Without
    it, `run_adhoc_mcp_session` would fall back to `post_analysis` and
    double-post the same report to Slack. We drive a synthesized event
    stream through `_stream_and_handle` and assert the flag flips correctly
    on every relevant code path.

#13 — Concurrent verbosity sessions race (E2 fix verification).
    Verbosity is REQUEST-scoped (passed as a parameter) rather than a
    module-level LRU. We spawn N threads, each calling
    `_dispatch_post_report` with a different verbosity, gate them on a
    `threading.Barrier` so they fire together, and verify each thread's
    rendered output reflects its own verbosity — no cross-thread bleed.

Run:
    cd orchestrator && python3 -m pytest concurrent_dispatch_test.py
"""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_event(event_type: str, **kwargs) -> SimpleNamespace:
    """Build a fake session event with the same attribute shape as the real one."""
    return SimpleNamespace(type=event_type, **kwargs)


def _make_stream(events):
    """Build a context-manager that yields the given events when iterated.

    Matches `client.beta.sessions.events.stream(session_id=...)` semantics:
    a context manager that, on __enter__, returns an iterable of events.
    """

    class _FakeStream:
        def __enter__(self):
            return iter(events)

        def __exit__(self, exc_type, exc, tb):
            return False

    return _FakeStream()


# ---------------------------------------------------------------------------
# #12 — post_report duplicate suppression integration test (E3 coverage)
# ---------------------------------------------------------------------------


def test_post_report_event_flips_posted_flag():
    """A successful post_report dispatch must mark the session as delivered.

    The E3 fix originally flipped a boolean ``agent_posted_to_slack`` flag.
    That optimistic bool was replaced by the honest ``DeliveryState`` state
    machine (lifecycle.DeliveryState): _stream_and_handle now returns a
    DeliveryState in the second tuple slot. A confirmed post_report delivery
    promotes to ``DELIVERED_VIA_POST_REPORT`` (which ``.is_delivered()`` is
    True for) only AFTER the dispatcher returns ``ok=True`` — not at the
    custom_tool_use event boundary. The fallback-suppression contract the E3
    fix protects is unchanged: a delivered state still suppresses the caller's
    second post_analysis call.
    """
    from lifecycle import DeliveryState
    from session_runner import _stream_and_handle

    # Event sequence:
    # 1) agent.custom_tool_use with name="post_report"  → flips flag, queues tool
    # 2) session.status_idle with stop_reason.type=requires_action and the
    #    tool_use_id in event_ids → dispatches the tool, sends result back
    # 3) session.status_idle with stop_reason.type=end_turn → loop breaks
    tool_use_id = "evt_post_report_1"

    post_report_event = _make_event(
        "agent.custom_tool_use",
        id=tool_use_id,
        name="post_report",
        input={
            "response_type": "quick_answer",
            "payload": {
                "metric": "Win rate",
                "value": "23.4%",
                "as_of": "2026-05-11",
                "source": "Salesforce",
            },
        },
    )
    requires_action_event = _make_event(
        "session.status_idle",
        stop_reason=SimpleNamespace(type="requires_action", event_ids=[tool_use_id]),
    )
    end_event = _make_event(
        "session.status_idle",
        stop_reason=SimpleNamespace(type="end_turn"),
    )

    fake_stream_cm = _make_stream([post_report_event, requires_action_event, end_event])

    # We need to capture the return tuple. Patch client.beta.sessions.events
    # to return our fake stream, and stub events.send so dispatch results
    # don't hit Anthropic. Also patch send_notification so _dispatch_post_report
    # doesn't try to talk to Slack.
    with (
        patch("session_runner.client") as mock_client,
        patch("session_runner.send_notification") as mock_send,
    ):
        mock_client.beta.sessions.events.stream.return_value = fake_stream_cm
        mock_client.beta.sessions.events.send = MagicMock()
        mock_send.return_value = "ts-12-1"

        text_parts, delivery_state, error_type, soql_queries = _stream_and_handle(
            session_id="sess-12-1",
            send_events=None,
            thread_ts="thread-12-1",
            verbosity="summary",
        )

    # The E3 fix (post-DeliveryState refactor): a confirmed post_report
    # delivery promotes to DELIVERED_VIA_POST_REPORT, which is a delivered
    # state — this is what suppresses the caller's fallback post_analysis.
    assert delivery_state == DeliveryState.DELIVERED_VIA_POST_REPORT, (
        "post_report dispatch did not promote to DELIVERED_VIA_POST_REPORT — "
        "E3 regression"
    )
    assert delivery_state.is_delivered() is True
    assert error_type is None
    # _dispatch_post_report should have been driven, which posts to Slack once.
    assert mock_send.call_count == 1
    # And the tool result should have been sent back to the session.
    assert mock_client.beta.sessions.events.send.called


def test_send_slack_notification_event_also_flips_posted_flag():
    """Sanity: send_slack_notification (the legacy free-form path) still marks
    the session delivered. Under the DeliveryState refactor this promotes to
    DELIVERED_VIA_LEGACY_SLACK_TOOL once the dispatcher returns ``ok=True`` with
    a non-empty ``message_ts`` (the mock returns ``ts-12-2``)."""
    from lifecycle import DeliveryState
    from session_runner import _stream_and_handle

    tool_use_id = "evt_send_slack_1"
    legacy_event = _make_event(
        "agent.custom_tool_use",
        id=tool_use_id,
        name="send_slack_notification",
        input={
            "summary": "Some finding worth surfacing",
            "severity": "info",
        },
    )
    requires_action_event = _make_event(
        "session.status_idle",
        stop_reason=SimpleNamespace(type="requires_action", event_ids=[tool_use_id]),
    )
    end_event = _make_event(
        "session.status_idle",
        stop_reason=SimpleNamespace(type="end_turn"),
    )

    fake_stream_cm = _make_stream([legacy_event, requires_action_event, end_event])

    with (
        patch("session_runner.client") as mock_client,
        patch("session_runner.send_notification") as mock_send,
    ):
        mock_client.beta.sessions.events.stream.return_value = fake_stream_cm
        mock_client.beta.sessions.events.send = MagicMock()
        mock_send.return_value = "ts-12-2"

        _, delivery_state, _, _ = _stream_and_handle(
            session_id="sess-12-2",
            send_events=None,
            thread_ts="thread-12-2",
            verbosity="summary",
        )

    assert delivery_state == DeliveryState.DELIVERED_VIA_LEGACY_SLACK_TOOL
    assert delivery_state.is_delivered() is True


def test_unrelated_tool_does_not_flip_posted_flag():
    """A non-posting tool (db_query, save_snapshot_batch, etc.) must NOT promote
    the delivery state. It stays NOT_DELIVERED so the caller's legitimate
    fallback post_analysis path still fires."""
    from lifecycle import DeliveryState
    from session_runner import _stream_and_handle

    tool_use_id = "evt_db_query_1"
    db_event = _make_event(
        "agent.custom_tool_use",
        id=tool_use_id,
        name="db_query",
        input={"sql": "SELECT Id FROM Account LIMIT 1"},
    )
    requires_action_event = _make_event(
        "session.status_idle",
        stop_reason=SimpleNamespace(type="requires_action", event_ids=[tool_use_id]),
    )
    end_event = _make_event(
        "session.status_idle",
        stop_reason=SimpleNamespace(type="end_turn"),
    )

    fake_stream_cm = _make_stream([db_event, requires_action_event, end_event])

    with (
        patch("session_runner.client") as mock_client,
        patch("session_runner.db_adapter") as mock_db,
    ):
        mock_client.beta.sessions.events.stream.return_value = fake_stream_cm
        mock_client.beta.sessions.events.send = MagicMock()
        mock_db.is_db_available.return_value = True
        mock_db.query.return_value = {"records": [], "totalSize": 0}

        _, delivery_state, _, _ = _stream_and_handle(
            session_id="sess-12-3",
            send_events=None,
            thread_ts="thread-12-3",
            verbosity="summary",
        )

    assert delivery_state == DeliveryState.NOT_DELIVERED, (
        "Non-posting tool promoted the delivery state — would suppress "
        "the legitimate fallback post_analysis path"
    )
    assert delivery_state.is_delivered() is False


def test_post_report_failed_dispatch_does_not_promote_delivery():
    """An invalid post_report that never delivers must NOT promote the delivery
    state — it stays NOT_DELIVERED.

    This is the inverse of the original (pre-DeliveryState) test, which
    asserted the flag flipped True the moment the post_report event was *seen*,
    before dispatch confirmed delivery. That optimistic promotion was the bug
    the DeliveryState refactor removed (see _stream_and_handle docstring): the
    old bool could read True for sessions where the user got NOTHING, and that
    false promotion suppressed the caller's fallback post_analysis. The honest
    contract: state only promotes to DELIVERED_VIA_POST_REPORT after the
    dispatcher returns ``ok=True``. An invalid response_type exhausts the retry
    budget and never returns ``ok=True``, so the state correctly stays
    NOT_DELIVERED — which is exactly what lets the caller's fallback fire so
    the user still gets an answer.
    """
    from lifecycle import DeliveryState
    from session_runner import _stream_and_handle

    tool_use_id = "evt_post_report_fail"
    post_report_event = _make_event(
        "agent.custom_tool_use",
        id=tool_use_id,
        name="post_report",
        input={"response_type": "nonexistent_type", "payload": {}},
    )
    requires_action_event = _make_event(
        "session.status_idle",
        stop_reason=SimpleNamespace(type="requires_action", event_ids=[tool_use_id]),
    )
    end_event = _make_event(
        "session.status_idle",
        stop_reason=SimpleNamespace(type="end_turn"),
    )

    fake_stream_cm = _make_stream([post_report_event, requires_action_event, end_event])

    with (
        patch("session_runner.client") as mock_client,
        patch("session_runner.send_notification") as mock_send,
    ):
        mock_client.beta.sessions.events.stream.return_value = fake_stream_cm
        mock_client.beta.sessions.events.send = MagicMock()
        # _dispatch_post_report on an unknown response_type still posts a
        # [POST_REPORT_FAILED] watch notice (E7), which itself goes through
        # send_notification. So one call is expected.
        mock_send.return_value = "ts-12-4"

        _, delivery_state, _, _ = _stream_and_handle(
            session_id="sess-12-4",
            send_events=None,
            thread_ts="thread-12-4",
            verbosity="summary",
        )

    # Invalid response_type (dispatch never returns ok=True) must NOT promote:
    # the state stays NOT_DELIVERED so the caller's fallback post_analysis can
    # still deliver an answer to the user.
    assert delivery_state == DeliveryState.NOT_DELIVERED
    assert delivery_state.is_delivered() is False


# ---------------------------------------------------------------------------
# #13 — Concurrent verbosity sessions race test (E2 coverage)
# ---------------------------------------------------------------------------


def test_concurrent_verbosity_no_bleed():
    """Five threads, each with a different verbosity, must each produce
    output rendered in their OWN verbosity. No cross-thread bleed.

    Drives `_dispatch_post_report` directly with thread-specific verbosity.
    A `threading.Barrier` forces all five to enter the dispatcher
    simultaneously, maximizing the chance of any shared mutable state
    surfacing as a bleed bug.
    """
    from session_runner import _dispatch_post_report

    # Mix of summary and expanded across threads. We use the quick_answer
    # schema because the rendered summary vs expanded text is easy to tell
    # apart (expanded adds "Source: Salesforce").
    verbosities = ["summary", "expanded", "summary", "expanded", "summary"]
    n_threads = len(verbosities)

    payload = {
        "metric": "Win rate",
        "value": "23.4%",
        "as_of": "2026-05-11",
        "source": "Salesforce",
    }
    tool_input = {"response_type": "quick_answer", "payload": payload}

    barrier = threading.Barrier(n_threads)

    # Each thread will write its rendered Slack summary into this dict,
    # keyed by thread index. We use a lock since dict assignment isn't
    # strictly atomic across all Python implementations.
    rendered_per_thread: dict[int, str] = {}
    modes_per_thread: dict[int, str] = {}
    results_lock = threading.Lock()

    # Per-thread send_notification call records, captured via a side_effect
    # function. This sidesteps the fact that a single MagicMock shared across
    # threads can interleave its call_args record — we want per-thread isolation
    # in the captured data.
    call_records: list[tuple[int, dict]] = []
    records_lock = threading.Lock()

    def fake_send_notification(
        severity,
        summary,
        detail="",
        reply_to=None,
        channel=None,
        extra_blocks=None,
        requester_id=None,
    ):
        # Identify which thread we're on by reply_to (we encode the thread
        # index there).
        idx = int(reply_to.split("-")[-1])
        with records_lock:
            call_records.append(
                (idx, {"severity": severity, "summary": summary, "reply_to": reply_to})
            )
        return f"ts-thread-{idx}"

    def worker(idx: int, verbosity: str):
        try:
            # Synchronize: all threads fire _dispatch_post_report together.
            barrier.wait(timeout=5)
            result_text = _dispatch_post_report(
                tool_input,
                thread_ts=f"thread-{idx}",
                session_id=f"sess-{idx}",
                verbosity=verbosity,
            )
            result = json.loads(result_text)
            with results_lock:
                modes_per_thread[idx] = result.get("mode", "")
        except Exception as e:
            with results_lock:
                modes_per_thread[idx] = f"ERROR: {e}"

    # Patch send_notification at the module level. The patch is active across
    # all threads spawned during the with-block.
    with patch("session_runner.send_notification", side_effect=fake_send_notification):
        threads = [
            threading.Thread(target=worker, args=(i, v))
            for i, v in enumerate(verbosities)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
            assert not t.is_alive(), f"Thread {t.name} hung"

    # Every thread completed and reported its own mode back as the verbosity
    # it was given — no bleed.
    for idx, expected_verbosity in enumerate(verbosities):
        assert idx in modes_per_thread, f"Thread {idx} did not record a mode"
        assert modes_per_thread[idx] == expected_verbosity, (
            f"Thread {idx} expected mode={expected_verbosity!r}, got "
            f"{modes_per_thread[idx]!r} — VERBOSITY BLED across threads"
        )

    # Every thread produced exactly one Slack post.
    assert len(call_records) == n_threads, (
        f"Expected {n_threads} Slack posts, got {len(call_records)}"
    )

    # Verify each call's rendered text matches its own thread's verbosity.
    # Quick_answer expanded mode includes "Source: Salesforce" in the body;
    # summary mode does not. (Confirmed by dispatch_post_report_test.py:88-89.)
    for idx, kwargs in call_records:
        rendered = kwargs["summary"]
        verbosity = verbosities[idx]
        if verbosity == "expanded":
            assert "Source: Salesforce" in rendered, (
                f"Thread {idx} (verbosity=expanded) missing 'Source:' line — "
                f"rendered in {verbosity} mode but got summary-style output"
            )
        else:  # summary
            assert "Source: Salesforce" not in rendered, (
                f"Thread {idx} (verbosity=summary) contains 'Source:' line — "
                f"rendered in expanded mode despite summary verbosity (BLEED)"
            )


def test_concurrent_verbosity_invalid_input_per_thread():
    """Concurrent threads, mix of valid and invalid payloads, each thread sees
    its own outcome — no cross-thread bleed of validation errors either.

    This is a second axis of the E2 fix: not just verbosity but the entire
    request context (response_type, payload, etc.) must be request-scoped.
    """
    from session_runner import _dispatch_post_report

    # Three threads with valid payloads, two with broken (wrong types).
    # PR #87 removed string max_length caps; the invalid trigger is now a
    # type error (value as a list) which Pydantic still rejects.
    valid_payload = {
        "metric": "Win rate",
        "value": "23.4%",
        "as_of": "2026-05-11",
        "source": "Salesforce",
    }
    invalid_payload = {
        "metric": "Win rate",
        "value": ["not", "a", "string"],  # type mismatch — Pydantic rejects
        "as_of": "2026-05-11",
        "source": "Salesforce",
    }
    cases = [
        ("valid", valid_payload),
        ("invalid", invalid_payload),
        ("valid", valid_payload),
        ("invalid", invalid_payload),
        ("valid", valid_payload),
    ]
    n_threads = len(cases)

    barrier = threading.Barrier(n_threads)
    outcomes: dict[int, str] = {}
    outcomes_lock = threading.Lock()

    def worker(idx: int, payload: dict):
        try:
            barrier.wait(timeout=5)
            result_text = _dispatch_post_report(
                {"response_type": "quick_answer", "payload": payload},
                thread_ts=f"thread-{idx}",
                session_id=f"sess-{idx}",
                verbosity="summary",
            )
            result = json.loads(result_text)
            outcome = "ok" if result.get("ok") else result.get("error", "?")
            with outcomes_lock:
                outcomes[idx] = outcome
        except Exception as e:
            with outcomes_lock:
                outcomes[idx] = f"EXC: {e}"

    with patch("session_runner.send_notification") as mock_send:
        mock_send.return_value = "ts-mixed"
        threads = [
            threading.Thread(target=worker, args=(i, payload))
            for i, (_, payload) in enumerate(cases)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
            assert not t.is_alive()

    # Each thread saw its own outcome — no cross-bleed.
    for idx, (expected_kind, _) in enumerate(cases):
        assert idx in outcomes, f"Thread {idx} did not complete"
        if expected_kind == "valid":
            assert outcomes[idx] == "ok", (
                f"Thread {idx} expected ok, got {outcomes[idx]!r}"
            )
        else:
            assert outcomes[idx] == "schema_validation_failed", (
                f"Thread {idx} expected schema_validation_failed, got {outcomes[idx]!r}"
            )
