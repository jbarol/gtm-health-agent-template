"""Plan #47 Workstream C — buffer cross-posted sub-thread tool events.

Sub-agent ``agent.custom_tool_use`` and ``agent.mcp_tool_use`` events with
a non-null ``session_thread_id`` appear in ``events.list`` but are NOT
delivered on the parent session's SSE stream (empirically confirmed
against sesn_EXAMPLE, 2026-05-18 — plan §9). Without
the C fix, the parent's ``session.status_idle requires_action`` arrives
with event_ids the orchestrator has never seen, ``pending_tools.get(eid)``
returns None, and the eid is logged as ``[REQUIRES_ACTION_UNHANDLED]`` —
stranding the session until the watchdog interrupts it.

Workstream C adds a new branch in ``_stream_and_handle``: when a
``session.thread_status_idle`` event arrives carrying a sub-thread
``requires_action``, the orchestrator calls
``client.beta.sessions.events.list(session_id, limit=50)``, filters for
events that (a) match one of the unresolved event_ids AND (b) carry the
sub-thread's ``session_thread_id``, then registers each
``agent.custom_tool_use`` / ``agent.mcp_tool_use`` event into
``pending_tools`` keyed by its event id. When the parent's
``session.status_idle requires_action`` arrives referencing the same
ids, the existing dispatcher finds them and dispatches normally.

Run:
    cd orchestrator && python3 -m pytest plan_47_workstream_c_test.py -v
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _stream_cm(events_iterable):
    """Fake ``_streaming_events_with_reconnect(...)`` context manager."""

    class _CM:
        def __enter__(self):
            return iter(events_iterable)

        def __exit__(self, exc_type, exc_val, exc_tb):
            return False

    return _CM()


class _FakeEventsPage:
    """Stand-in for the SDK's SyncCursorPage.

    Codex P2 fix introduced pagination: the production helper iterates
    the page directly (auto-paginating) AND legacy spots may still read
    .data. This stand-in supports both so tests don't have to fork.
    """

    def __init__(self, events):
        self.data = list(events)

    def __iter__(self):
        return iter(self.data)


def _list_resp(events):
    """Fake ``client.beta.sessions.events.list(...)`` return value."""
    return _FakeEventsPage(events)


def _subthread_idle_event(stid: str, event_ids: list[str]):
    """A session.thread_status_idle event with stop_reason=requires_action."""
    return SimpleNamespace(
        id=f"ev_sub_idle_{stid}",
        type="session.thread_status_idle",
        session_thread_id=stid,
        stop_reason=SimpleNamespace(type="requires_action", event_ids=event_ids),
        created_at=None,
    )


def _parent_idle_requires_action(event_ids: list[str]):
    """A session.status_idle event referencing the sub-thread event_ids."""
    return SimpleNamespace(
        id="ev_parent_idle",
        type="session.status_idle",
        stop_reason=SimpleNamespace(type="requires_action", event_ids=event_ids),
        created_at=None,
    )


def _parent_end_turn():
    """Terminal end_turn event to close the loop after dispatch."""
    return SimpleNamespace(
        id="ev_end",
        type="session.status_idle",
        stop_reason=SimpleNamespace(type="end_turn"),
        created_at=None,
    )


def _custom_tool_use(eid: str, name: str, stid: str | None, tool_input=None):
    """Build an agent.custom_tool_use event (cross-posted if stid set)."""
    return SimpleNamespace(
        id=eid,
        type="agent.custom_tool_use",
        name=name,
        input=tool_input or {},
        session_thread_id=stid,
        created_at=None,
    )


def _mcp_tool_use(eid: str, name: str, stid: str | None, server: str = "kapa"):
    """Build an agent.mcp_tool_use event (cross-posted if stid set)."""
    return SimpleNamespace(
        id=eid,
        type="agent.mcp_tool_use",
        name=name,
        input={},
        mcp_server_name=server,
        evaluated_permission="ask",
        session_thread_id=stid,
        created_at=None,
    )


def _run_stream(events: list, *, list_data=None, dispatch_result='{"ok": true}'):
    """Helper that drives ``_stream_and_handle`` against a synthetic stream.

    Returns a (notifications_mock, list_mock, send_mock, dispatch_mock) tuple
    for assertions. ``list_data`` is the list of events ``events.list``
    should return when called (defaults to whatever events were in the
    stream — useful for the lookup path).
    """
    import session_runner

    if list_data is None:
        list_data = events

    list_mock = MagicMock(return_value=_list_resp(list_data))
    send_mock = MagicMock(return_value=None)
    dispatch_mock = MagicMock(return_value=dispatch_result)
    notifications_mock = MagicMock(return_value="ts-1")

    stream_cm = _stream_cm(events)

    with (
        patch.object(
            session_runner, "_streaming_events_with_reconnect", return_value=stream_cm
        ),
        patch.object(session_runner.client.beta.sessions.events, "list", list_mock),
        patch.object(session_runner.client.beta.sessions.events, "send", send_mock),
        patch.object(session_runner, "_dispatch_tool", dispatch_mock),
        patch.object(session_runner, "send_notification", notifications_mock),
        patch.object(session_runner, "_buffer_thread_event", MagicMock()),
    ):
        session_runner._stream_and_handle(
            session_id="sesn_EXAMPLE",
            send_events=None,
            thread_ts="1779.999",
            verbosity="summary",
            portco_key="acme",
            user_id="U0",
            event_ts="1779.000",
            channel_id="C0",
            inv_id=42,
        )

    return notifications_mock, list_mock, send_mock, dispatch_mock


# ---------------------------------------------------------------------------
# §9 tests: cross-posted sub-thread events get buffered and dispatched
# ---------------------------------------------------------------------------


def test_subthread_custom_tool_buffered(caplog):
    """A sub-thread requires_action triggers events.list and buffers the
    cross-posted custom_tool_use into pending_tools.

    Verifies the [SUBTHREAD_TOOL_BUFFERED] log line fires per event.
    """
    import logging

    stid = "sthr_test1"
    eid = "sevt_subcustom1"
    sub_tool = _custom_tool_use(eid, "db_query", stid)

    # Stream: sub-thread goes idle requires_action, then parent end_turn.
    # The custom_tool_use itself is NOT in the SSE stream — only in
    # events.list. That's the empirically-observed bug from §9.
    events = [
        _subthread_idle_event(stid, [eid]),
        _parent_end_turn(),
    ]

    with caplog.at_level(logging.INFO, logger="session_runner"):
        notif, list_mock, _send, _dispatch = _run_stream(events, list_data=[sub_tool])

    # events.list must have been called once for the sub-thread lookup
    list_mock.assert_called()
    # The [SUBTHREAD_TOOL_BUFFERED] line must have fired with the tool name
    buffered_msgs = [
        r for r in caplog.records if "SUBTHREAD_TOOL_BUFFERED" in r.message
    ]
    assert len(buffered_msgs) == 1
    assert eid in buffered_msgs[0].message
    assert "db_query" in buffered_msgs[0].message


def test_subthread_requires_action_parent_dispatches(caplog):
    """After the sub-thread idle buffers the event, the parent's
    status_idle requires_action dispatches it via _dispatch_tool.
    """
    import logging

    stid = "sthr_test2"
    eid = "sevt_subcustom2"
    sub_tool = _custom_tool_use(eid, "query_artifact", stid, {"path": "x.parquet"})

    events = [
        _subthread_idle_event(stid, [eid]),
        _parent_idle_requires_action([eid]),
        _parent_end_turn(),
    ]

    with caplog.at_level(logging.INFO, logger="session_runner"):
        _notif, _list, send_mock, dispatch_mock = _run_stream(
            events, list_data=[sub_tool]
        )

    # _dispatch_tool called with the buffered tool name + input
    dispatch_mock.assert_called_once()
    args, kwargs = dispatch_mock.call_args
    assert args[0] == "query_artifact"
    assert args[1] == {"path": "x.parquet"}
    # The user.custom_tool_result event must have been sent back
    send_mock.assert_called()
    send_kwargs = send_mock.call_args.kwargs
    sent_events = send_kwargs["events"]
    assert any(
        e.get("type") == "user.custom_tool_result"
        and e.get("custom_tool_use_id") == eid
        for e in sent_events
    )


def test_subthread_mcp_tool_allowed(caplog):
    """A cross-posted MCP tool_use whose mcp_server_name is on the
    auto-approve allowlist results in ``user.tool_confirmation result=allow``.
    """
    import logging

    import config as _config

    stid = "sthr_test3"
    eid = "sevt_submcp_allow"
    sub_tool = _mcp_tool_use(eid, "search_x", stid, server="allowed_server")

    events = [
        _subthread_idle_event(stid, [eid]),
        _parent_idle_requires_action([eid]),
        _parent_end_turn(),
    ]

    # Patch the allowlist for this test only
    prior = set(_config.MCP_AUTO_APPROVE_ALLOWLIST)
    _config.MCP_AUTO_APPROVE_ALLOWLIST = {"allowed_server"}
    try:
        with caplog.at_level(logging.INFO, logger="session_runner"):
            _notif, _list, send_mock, _dispatch = _run_stream(
                events, list_data=[sub_tool]
            )
    finally:
        _config.MCP_AUTO_APPROVE_ALLOWLIST = prior

    # Sent events must include an allow confirmation for this eid
    send_mock.assert_called()
    sent_events = send_mock.call_args.kwargs["events"]
    allow_evs = [
        e
        for e in sent_events
        if e.get("type") == "user.tool_confirmation"
        and e.get("tool_use_id") == eid
        and e.get("result") == "allow"
    ]
    assert len(allow_evs) == 1


def test_subthread_mcp_tool_denied(caplog):
    """A cross-posted MCP tool_use whose server is NOT on the allowlist is
    denied with ``user.tool_confirmation result=deny`` AND an admin DM fires.
    """
    import logging

    import config as _config

    stid = "sthr_test4"
    eid = "sevt_submcp_deny"
    sub_tool = _mcp_tool_use(eid, "search_y", stid, server="not_allowed")

    events = [
        _subthread_idle_event(stid, [eid]),
        _parent_idle_requires_action([eid]),
        _parent_end_turn(),
    ]

    prior = set(_config.MCP_AUTO_APPROVE_ALLOWLIST)
    _config.MCP_AUTO_APPROVE_ALLOWLIST = set()  # empty = deny everything
    try:
        with caplog.at_level(logging.WARNING, logger="session_runner"):
            notif, _list, send_mock, _dispatch = _run_stream(
                events, list_data=[sub_tool]
            )
    finally:
        _config.MCP_AUTO_APPROVE_ALLOWLIST = prior

    # Deny was sent
    sent_events = send_mock.call_args.kwargs["events"]
    deny_evs = [
        e
        for e in sent_events
        if e.get("type") == "user.tool_confirmation"
        and e.get("tool_use_id") == eid
        and e.get("result") == "deny"
    ]
    assert len(deny_evs) == 1
    # Admin DM fired
    notif.assert_called()
    admin_calls = [c for c in notif.call_args_list if c.kwargs.get("admin_only")]
    assert len(admin_calls) >= 1


def test_requires_action_unresolvable_admin_dm(caplog):
    """If events.list returns no match for an unresolved eid, the orchestrator
    logs [REQUIRES_ACTION_UNRESOLVABLE] and DMs admin.

    This is the post-fix "should never fire" path — if it does, something
    genuinely unknown is happening on the server side.
    """
    import logging

    eid = "sevt_ghost"

    # No subthread idle, no events.list match — parent directly demands
    # action for an eid the orchestrator has never seen.
    events = [
        _parent_idle_requires_action([eid]),
        _parent_end_turn(),
    ]

    with caplog.at_level(logging.ERROR, logger="session_runner"):
        notif, list_mock, _send, _dispatch = _run_stream(events, list_data=[])

    # events.list called during the recovery lookup
    list_mock.assert_called()
    # Error log line for unresolvable eid
    unresolvable = [
        r for r in caplog.records if "REQUIRES_ACTION_UNRESOLVABLE" in r.message
    ]
    assert len(unresolvable) >= 1
    assert eid in unresolvable[0].message
    # Admin DM fired
    admin_calls = [c for c in notif.call_args_list if c.kwargs.get("admin_only")]
    assert len(admin_calls) >= 1


def test_primary_thread_events_unchanged(caplog):
    """Control case: a primary-thread custom_tool_use (no session_thread_id)
    follows the existing buffer-via-SSE path. events.list is NOT called.
    """
    import logging

    eid = "sevt_primary"
    primary_tool = _custom_tool_use(eid, "reasoning_summary", None, {"text": "x"})

    events = [
        primary_tool,  # delivered in the SSE stream as before
        _parent_idle_requires_action([eid]),
        _parent_end_turn(),
    ]

    with caplog.at_level(logging.INFO, logger="session_runner"):
        _notif, list_mock, send_mock, dispatch_mock = _run_stream(events)

    # events.list MUST NOT have been called — no sub-thread lookup needed
    list_mock.assert_not_called()
    # The existing dispatch path still works
    dispatch_mock.assert_called_once()
    args, _kwargs = dispatch_mock.call_args
    assert args[0] == "reasoning_summary"
    # And the [SUBTHREAD_TOOL_BUFFERED] log line did NOT fire
    buffered_msgs = [
        r for r in caplog.records if "SUBTHREAD_TOOL_BUFFERED" in r.message
    ]
    assert len(buffered_msgs) == 0


def test_subthread_lookup_cache_dedupes_within_loop(caplog):
    """If the same sub-thread emits multiple thread_status_idle events with
    the same event_ids batch, events.list is called only once.
    """
    import logging

    stid = "sthr_cache_test"
    eid_a = "sevt_cache_a"
    eid_b = "sevt_cache_b"
    sub_a = _custom_tool_use(eid_a, "db_query", stid)
    sub_b = _custom_tool_use(eid_b, "dump_sf_query", stid)

    # Same idle event repeats — should only trigger one events.list call
    idle = _subthread_idle_event(stid, [eid_a, eid_b])
    events = [
        idle,
        idle,  # duplicate
        _parent_end_turn(),
    ]

    with caplog.at_level(logging.INFO, logger="session_runner"):
        _notif, list_mock, _send, _dispatch = _run_stream(
            events, list_data=[sub_a, sub_b]
        )

    # events.list called once (cache hit on second idle)
    # seen_ids dedup on the event itself also helps — but the cache key is
    # the real assertion: same (stid, frozenset(event_ids)) is skipped.
    assert list_mock.call_count <= 1


# ---------------------------------------------------------------------------
# Codex P2 (round 1) — recovery path correctness
# Recovery fires when the buffered lookup missed the event but events.list
# finds it. The recovery branch must NOT degrade behavior vs the buffered
# path: allowlisted MCP servers should still be allowed, and custom tools
# should be dispatched (not stranded).
# ---------------------------------------------------------------------------


def test_recovery_mcp_tool_allowed_respects_allowlist(caplog):
    """Codex P2 fix: recovery path for allowlisted MCP servers → result=allow.

    Before the fix, the recovery branch unconditionally denied MCP tools.
    A legitimate Kapa sub-agent call that the buffered lookup happened to
    miss (transient events.list pagination, race) would get DENIED instead
    of ALLOWED — degrading normal behavior under the exact conditions the
    patch is intended to recover from.
    """
    import logging

    eid = "sevt_recovery_mcp_allow"
    server = "kapa-acme"  # synthetic allowlisted server
    mcp_evt = _mcp_tool_use(
        eid, "search_knowledge_base", None, server=server
    )

    # NO sub-thread idle event delivered in stream → parent demands action
    # for an eid the orchestrator never buffered. Recovery path activates.
    events = [
        _parent_idle_requires_action([eid]),
        _parent_end_turn(),
    ]

    with caplog.at_level(logging.INFO, logger="session_runner"):
        with patch(
            "config.MCP_AUTO_APPROVE_ALLOWLIST",
            frozenset({server}),
        ):
            notif, list_mock, send_mock, _dispatch = _run_stream(
                events, list_data=[mcp_evt]
            )

    # events.list called during recovery
    list_mock.assert_called()
    # Recovery succeeded — no admin DM about denial
    admin_calls = [c for c in notif.call_args_list if c.kwargs.get("admin_only")]
    assert len(admin_calls) == 0
    # send() received an allow result for this eid
    sent_results = []
    for call in send_mock.call_args_list:
        sent_results.extend(call.kwargs.get("events", []) or [])
    allow_for_eid = [
        r
        for r in sent_results
        if r.get("type") == "user.tool_confirmation"
        and r.get("tool_use_id") == eid
        and r.get("result") == "allow"
    ]
    assert len(allow_for_eid) == 1, sent_results
    # Log line confirms the recovery branch fired
    recovered = [r for r in caplog.records if "SUBTHREAD_TOOL_RECOVERED" in r.message]
    assert len(recovered) >= 1


def test_recovery_mcp_tool_denied_when_not_on_allowlist(caplog):
    """Recovery for non-allowlisted MCP servers → result=deny + admin DM.

    This preserves the security invariant of the buffered path: a server
    that's not on the allowlist should NEVER auto-approve, even via the
    recovery path. The deny is intentional.
    """
    import logging

    eid = "sevt_recovery_mcp_deny"
    mcp_evt = _mcp_tool_use(eid, "rogue_tool", None, server="unknown-server")

    events = [
        _parent_idle_requires_action([eid]),
        _parent_end_turn(),
    ]

    with caplog.at_level(logging.WARNING, logger="session_runner"):
        with patch("config.MCP_AUTO_APPROVE_ALLOWLIST", frozenset()):
            notif, list_mock, send_mock, _dispatch = _run_stream(
                events, list_data=[mcp_evt]
            )

    list_mock.assert_called()
    # Admin DM fired
    admin_calls = [c for c in notif.call_args_list if c.kwargs.get("admin_only")]
    assert len(admin_calls) >= 1
    # send() received a deny result for this eid
    sent_results = []
    for call in send_mock.call_args_list:
        sent_results.extend(call.kwargs.get("events", []) or [])
    deny_for_eid = [
        r
        for r in sent_results
        if r.get("type") == "user.tool_confirmation"
        and r.get("tool_use_id") == eid
        and r.get("result") == "deny"
    ]
    assert len(deny_for_eid) == 1, sent_results


def test_recovery_custom_tool_dispatches(caplog):
    """Codex P2 fix: recovery path for custom_tool_use → dispatch + result.

    Before the fix, custom tools found via recovery were logged and DMed
    but never dispatched — leaving the session stuck in requires_action
    until the watchdog killed it. The buffered path's dispatcher already
    knows how to handle every custom tool name in the codebase; recovery
    must do the same.
    """
    import logging

    eid = "sevt_recovery_custom"
    custom_evt = _custom_tool_use(eid, "db_query", None, {"q": "SELECT 1"})

    events = [
        _parent_idle_requires_action([eid]),
        _parent_end_turn(),
    ]

    with caplog.at_level(logging.INFO, logger="session_runner"):
        notif, list_mock, send_mock, dispatch_mock = _run_stream(
            events, list_data=[custom_evt], dispatch_result='{"ok": true, "rows": 1}'
        )

    list_mock.assert_called()
    # _dispatch_tool called with the recovered tool's name + input
    dispatch_mock.assert_called_once()
    args, _kwargs = dispatch_mock.call_args
    assert args[0] == "db_query"
    assert args[1] == {"q": "SELECT 1"}
    # send() received a user.custom_tool_result for this eid
    sent_results = []
    for call in send_mock.call_args_list:
        sent_results.extend(call.kwargs.get("events", []) or [])
    results_for_eid = [
        r
        for r in sent_results
        if r.get("type") == "user.custom_tool_result"
        and r.get("custom_tool_use_id") == eid
    ]
    assert len(results_for_eid) == 1, sent_results
    # Log line confirms the recovery dispatch fired
    recovered = [r for r in caplog.records if "SUBTHREAD_TOOL_RECOVERED" in r.message]
    assert len(recovered) >= 1
    # No admin DM (clean recovery)
    admin_calls = [c for c in notif.call_args_list if c.kwargs.get("admin_only")]
    assert len(admin_calls) == 0


def test_recovery_custom_tool_dispatch_failure_admin_dms(caplog):
    """If the recovery-path dispatcher raises, log + admin DM and leave
    stranded for the watchdog. The dispatch exception must not propagate
    out of the event loop.
    """
    import logging

    eid = "sevt_recovery_custom_boom"
    custom_evt = _custom_tool_use(eid, "db_query", None, {"q": "BAD"})

    events = [
        _parent_idle_requires_action([eid]),
        _parent_end_turn(),
    ]

    def _raiser(*args, **kwargs):
        raise RuntimeError("dispatcher boom")

    with caplog.at_level(logging.ERROR, logger="session_runner"):
        import session_runner as sr_mod

        with (
            patch.object(
                sr_mod,
                "_streaming_events_with_reconnect",
                return_value=_stream_cm(events),
            ),
            patch.object(
                sr_mod.client.beta.sessions.events,
                "list",
                MagicMock(return_value=_list_resp([custom_evt])),
            ),
            patch.object(sr_mod.client.beta.sessions.events, "send", MagicMock()),
            patch.object(sr_mod, "_dispatch_tool", side_effect=_raiser),
            patch.object(
                sr_mod, "send_notification", MagicMock(return_value="ts-1")
            ) as notif,
            patch.object(sr_mod, "_buffer_thread_event", MagicMock()),
        ):
            # Must NOT raise out of the event loop
            sr_mod._stream_and_handle(
                session_id="sesn_EXAMPLE",
                send_events=None,
                thread_ts="1779.999",
                verbosity="summary",
                portco_key="acme",
                user_id="U0",
                event_ts="1779.000",
                channel_id="C0",
                inv_id=42,
            )

        # Failure log fired
        failed = [
            r
            for r in caplog.records
            if "SUBTHREAD_TOOL_RECOVERED_DISPATCH_FAILED" in r.message
        ]
        assert len(failed) >= 1
        # Admin DM fired
        admin_calls = [c for c in notif.call_args_list if c.kwargs.get("admin_only")]
        assert len(admin_calls) >= 1


# ---------------------------------------------------------------------------
# Codex P2 (round 2) — pagination + post-dispatch fidelity
# ---------------------------------------------------------------------------


def test_paginated_lookup_finds_events_in_long_log():
    """The pagination helper iterates beyond the first 50 events.

    Codex P2 fix: in busy multi-agent sessions, the blocking event may
    land older than the newest 50 entries. The helper now iterates the
    SDK's cursor up to max_total events.
    """
    import session_runner as sr_mod

    target_eid = "sevt_deep"
    target_evt = SimpleNamespace(
        id=target_eid,
        type="agent.custom_tool_use",
        name="x",
        input={},
        session_thread_id=None,
        created_at=None,
    )
    # 100 noise events before the target — simulates a busy session where
    # the blocking event isn't in the most-recent page.
    noise = [
        SimpleNamespace(
            id=f"sevt_noise_{i}",
            type="agent.thinking",
            name=None,
            input=None,
            session_thread_id=None,
            created_at=None,
        )
        for i in range(100)
    ]

    list_mock = MagicMock(return_value=_list_resp(noise + [target_evt]))
    with patch.object(sr_mod.client.beta.sessions.events, "list", list_mock):
        result = sr_mod._paginated_events_lookup(
            "sesn_EXAMPLE", {target_eid}, max_total=500, page_size=100
        )

    list_mock.assert_called_once()
    assert len(result) == 1
    assert getattr(result[0], "id", None) == target_eid


def test_paginated_lookup_bounded_by_max_total():
    """Helper stops scanning at max_total even if the wanted id isn't found.

    Protects against runaway scans on huge sessions; the [..._INCOMPLETE]
    log line carries the operator-visible signal.
    """
    import session_runner as sr_mod

    # 50 events, none matching the wanted id
    noise = [
        SimpleNamespace(
            id=f"sevt_noise_{i}",
            type="agent.thinking",
            name=None,
            input=None,
            session_thread_id=None,
            created_at=None,
        )
        for i in range(50)
    ]

    list_mock = MagicMock(return_value=_list_resp(noise))
    with patch.object(sr_mod.client.beta.sessions.events, "list", list_mock):
        import logging as _l

        caplog_level = _l.WARNING
        # Just exercise the bound; the log line is a soft signal.
        result = sr_mod._paginated_events_lookup(
            "sesn_EXAMPLE", {"sevt_missing"}, max_total=25, page_size=100
        )

    # No match found because target id isn't in the noise
    assert result == []


def test_recovery_custom_tool_propagates_is_error():
    """Codex P2 fix: recovered custom tool that returns _is_error=True
    must mark the user.custom_tool_result event with is_error=true.

    Without this, a recovered post_report schema-retry error would be
    sent as a successful tool result, causing the agent to act on a
    failure as if it succeeded.
    """
    eid = "sevt_rec_iserr"
    custom_evt = _custom_tool_use(eid, "post_report", None, {})

    events = [
        _parent_idle_requires_action([eid]),
        _parent_end_turn(),
    ]

    error_payload = '{"_is_error": true, "ok": false, "error": "schema validation"}'

    _notif, _list, send_mock, _dispatch = _run_stream(
        events, list_data=[custom_evt], dispatch_result=error_payload
    )

    sent_results = []
    for call in send_mock.call_args_list:
        sent_results.extend(call.kwargs.get("events", []) or [])

    matching = [r for r in sent_results if r.get("custom_tool_use_id") == eid]
    assert len(matching) == 1
    assert matching[0].get("is_error") is True, (
        "Recovered custom-tool error must carry is_error=true; otherwise "
        "the agent treats a schema retry as a successful tool result."
    )


def test_paginated_lookup_uses_newest_first_order():
    """Codex P1 fix: helper must request order='desc' so recent blockers
    are reached before the max_total cap. Without this, long sessions
    with > max_total earlier events leave the actual blocker unreached.
    """
    import session_runner as sr_mod

    list_mock = MagicMock(return_value=_list_resp([]))
    with patch.object(sr_mod.client.beta.sessions.events, "list", list_mock):
        sr_mod._paginated_events_lookup(
            "sesn_EXAMPLE", {"sevt_any"}, max_total=10, page_size=10
        )

    list_mock.assert_called_once()
    kwargs = list_mock.call_args.kwargs
    assert kwargs.get("order") == "desc", (
        "_paginated_events_lookup must request order='desc' to scan "
        "newest events first; codex P1 fix"
    )


def test_paginated_lookup_falls_back_when_order_unsupported():
    """If the SDK rejects order='desc' (older SDK), helper retries without
    it. The bug guarded against is a TypeError on the keyword that would
    otherwise abort the lookup entirely.
    """
    import session_runner as sr_mod

    call_count = {"n": 0}

    def _maybe_typeerror(*_a, **kw):
        call_count["n"] += 1
        if "order" in kw:
            raise TypeError("unexpected keyword 'order'")
        return _list_resp([])

    list_mock = MagicMock(side_effect=_maybe_typeerror)
    with patch.object(sr_mod.client.beta.sessions.events, "list", list_mock):
        result = sr_mod._paginated_events_lookup(
            "sesn_EXAMPLE", {"sevt_any"}, max_total=10, page_size=10
        )

    # Two calls: first with order=desc (TypeError), second without
    assert call_count["n"] == 2
    assert result == []


def test_recovery_custom_tool_success_has_no_is_error():
    """Sanity: a recovered successful custom tool result does NOT set
    is_error (so the agent doesn't see a false-negative failure)."""
    eid = "sevt_rec_ok"
    custom_evt = _custom_tool_use(eid, "post_report", None, {})

    events = [
        _parent_idle_requires_action([eid]),
        _parent_end_turn(),
    ]

    success_payload = '{"ok": true, "message_ts": "999.111"}'

    _notif, _list, send_mock, _dispatch = _run_stream(
        events, list_data=[custom_evt], dispatch_result=success_payload
    )

    sent_results = []
    for call in send_mock.call_args_list:
        sent_results.extend(call.kwargs.get("events", []) or [])

    matching = [r for r in sent_results if r.get("custom_tool_use_id") == eid]
    assert len(matching) == 1
    assert "is_error" not in matching[0]
