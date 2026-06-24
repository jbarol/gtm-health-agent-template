"""Unit tests for session_runner — F8 post-report canvas push hook (Plan #33).

The broader _dispatch_post_report behavior is covered by
dispatch_post_report_test.py. This file focuses on the new F8 hook:

  - Successful post_report fires surface_pusher.push_to_canvas(portco_key) async
  - Failure inside push_to_canvas does NOT bubble up — the Slack post still
    succeeds and the dispatch returns {"ok": True}
  - When portco_key is None (cron-style callers), the hook is skipped

surface_pusher is patched via sys.modules so this test passes even when the
real surface_pusher module has not landed on main yet (F6 lands in parallel).

Run:
    cd orchestrator && python3 -m pytest session_runner_test.py -q
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import types
from unittest.mock import MagicMock, patch


def _make_valid_quick_answer_tool_input() -> dict:
    return {
        "response_type": "quick_answer",
        "payload": {
            "metric": "Win rate",
            "value": "23.4%",
            "as_of": "2026-05-11",
            "source": "Salesforce",
        },
    }


def _install_fake_surface_pusher(push_fn) -> None:
    """Register a fake `surface_pusher` module in sys.modules.

    The hook in session_runner does `from surface_pusher import push_to_canvas`
    inside a try/except, so a sys.modules entry is the cleanest seam.
    """
    fake = types.ModuleType("surface_pusher")
    # setattr keeps pyright happy — ModuleType has no static knowledge
    # of the dynamic push_to_canvas attribute we're injecting.
    setattr(fake, "push_to_canvas", push_fn)
    sys.modules["surface_pusher"] = fake


def _remove_fake_surface_pusher() -> None:
    sys.modules.pop("surface_pusher", None)


def _wait_for(predicate, timeout: float = 2.0, interval: float = 0.01) -> bool:
    """Poll until predicate() is truthy or the timeout elapses."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_post_report_fires_canvas_push_with_portco_key():
    """A successful post_report fires push_to_canvas(portco_key) async."""
    from session_runner import _dispatch_post_report

    push_called = threading.Event()
    push_args = []

    def fake_push(portco):
        push_args.append(portco)
        push_called.set()

    _install_fake_surface_pusher(fake_push)
    try:
        with patch("session_runner.send_notification") as mock_send:
            mock_send.return_value = "ts-success"
            result_text = _dispatch_post_report(
                _make_valid_quick_answer_tool_input(),
                thread_ts="thread-1",
                session_id="sess-1",
                verbosity="summary",
                portco_key="acme",
            )

        # The post itself succeeded
        result = json.loads(result_text)
        assert result["ok"] is True, result_text

        # The async push fired with the right portco
        assert push_called.wait(timeout=2.0), (
            "push_to_canvas was not invoked within 2s of post_report"
        )
        assert push_args == ["acme"]
    finally:
        _remove_fake_surface_pusher()


def test_post_report_swallows_canvas_push_exception():
    """A failure inside push_to_canvas does NOT bubble out of _dispatch_post_report.

    Per Plan #33 failure-mode table: "Skip; daily 8am cron catches up."
    The Slack post still succeeded; the hook just logs [SURFACE_PUSH_FAILED].
    """
    from session_runner import _dispatch_post_report

    push_called = threading.Event()

    # Use a threading.excepthook override so pytest doesn't surface the
    # intentional exception as an unhandled-thread-exception warning.
    captured_thread_excs = []

    def _swallow_thread_exc(args):
        captured_thread_excs.append(args)

    prior_excepthook = threading.excepthook
    threading.excepthook = _swallow_thread_exc

    def exploding_push(portco):
        push_called.set()
        raise RuntimeError("simulated surface_pusher failure")

    _install_fake_surface_pusher(exploding_push)
    try:
        with patch("session_runner.send_notification") as mock_send:
            mock_send.return_value = "ts-success"
            # This must not raise even though push_to_canvas blows up.
            result_text = _dispatch_post_report(
                _make_valid_quick_answer_tool_input(),
                thread_ts="thread-2",
                session_id="sess-2",
                verbosity="summary",
                portco_key="acme",
            )

        result = json.loads(result_text)
        assert result["ok"] is True, result_text
        assert result["message_ts"] == "ts-success"

        # The push thread ran (and crashed silently on its own daemon thread)
        assert push_called.wait(timeout=2.0)
        # The thread exception was swallowed by the daemon — captured here
        # only for the test to confirm it really fired.
        assert _wait_for(lambda: len(captured_thread_excs) >= 1, timeout=2.0), (
            "expected the daemon thread to raise the simulated failure"
        )
    finally:
        threading.excepthook = prior_excepthook
        _remove_fake_surface_pusher()


def test_post_report_skips_canvas_push_when_portco_key_missing():
    """Cron-style callers (no portco_key) should not fire the canvas push.

    The 08:00 PT daily cron catches up for those callers.
    """
    from session_runner import _dispatch_post_report

    push_called = threading.Event()

    def fake_push(portco):
        push_called.set()

    _install_fake_surface_pusher(fake_push)
    try:
        with patch("session_runner.send_notification") as mock_send:
            mock_send.return_value = "ts-cron"
            result_text = _dispatch_post_report(
                _make_valid_quick_answer_tool_input(),
                thread_ts=None,
                session_id="sess-cron",
                verbosity="summary",
                portco_key=None,
            )

        result = json.loads(result_text)
        assert result["ok"] is True

        # Give it a moment to be sure no thread was spawned.
        assert not push_called.wait(timeout=0.2), (
            "push_to_canvas should NOT be called when portco_key is None"
        )
    finally:
        _remove_fake_surface_pusher()


# ──────────────────────────────────────────────────────────────────────────
# B3 — _dispatch_tool virtualizes large db_query results
# ──────────────────────────────────────────────────────────────────────────


def test_db_query_above_threshold_returns_virtualized_handle(tmp_path, monkeypatch):
    """A db_query that returns >RESULT_VIRTUALIZE_THRESHOLD rows is streamed
    to .xlsx and the dispatcher returns the compact handle (preview + summary
    stats + file_path) instead of the raw 200-row JSON.
    """
    import session_runner

    big_records = [
        {"Id": f"00Q{i:06}", "Stage": "Open" if i % 2 else "Closed"} for i in range(200)
    ]
    fake_db_result = {"records": big_records, "totalSize": 200}

    monkeypatch.setattr(
        session_runner.db_adapter,
        "is_db_available",
        lambda: True,
    )
    monkeypatch.setattr(
        session_runner.db_adapter,
        "query",
        lambda sql: fake_db_result,
    )

    # Redirect output_dir for the test so we don't try to write to /mnt.
    real_virtualize = None
    import result_virtualize

    real_virtualize = result_virtualize.virtualize_result

    def virtualize_to_tmp(rows, tool_name, output_dir=None):
        return real_virtualize(rows, tool_name, output_dir=str(tmp_path))

    monkeypatch.setattr(result_virtualize, "virtualize_result", virtualize_to_tmp)

    result_text = session_runner._dispatch_tool(
        "db_query",
        {"sql": "SELECT Id, Stage FROM Lead"},
        session_id="sess-bigq",
    )
    result = json.loads(result_text)

    # Returned the compact handle, NOT the raw 200-row records list.
    assert "row_count" in result
    assert result["row_count"] == 200
    assert "preview" in result
    assert len(result["preview"]) == 10  # PREVIEW_ROW_COUNT
    assert "file_path" in result
    assert result["file_path"]
    assert os.path.exists(result["file_path"])

    # And the file path was tracked on the session for post_report fallback.
    tracked = session_runner._consume_virtualized_files("sess-bigq")
    assert tracked == [result["file_path"]]


def test_db_query_under_threshold_returns_raw_records(monkeypatch):
    """Small db_query result skips virtualization — raw records JSON returned."""
    import session_runner

    small_records = [{"Id": f"00Q{i:06}"} for i in range(10)]
    fake_db_result = {"records": small_records, "totalSize": 10}

    monkeypatch.setattr(session_runner.db_adapter, "is_db_available", lambda: True)
    monkeypatch.setattr(session_runner.db_adapter, "query", lambda sql: fake_db_result)

    result_text = session_runner._dispatch_tool(
        "db_query",
        {"sql": "SELECT Id FROM Lead LIMIT 10"},
        session_id="sess-smallq",
    )
    result = json.loads(result_text)

    # The raw shape — no virtualization handle, no file_path key.
    assert "records" in result
    assert len(result["records"]) == 10
    assert "file_path" not in result


# ──────────────────────────────────────────────────────────────────────────
# B4 — _dispatch_post_report attaches virtualized files via files.upload_v2
# ──────────────────────────────────────────────────────────────────────────


def test_post_report_uploads_attachments_from_payload(tmp_path, monkeypatch):
    """post_report with payload.attachments uploads each file in-thread."""
    import session_runner

    # SESSION_OUTPUT_DIR whitelist (PR #99 security fix): the validator
    # only accepts attachments under this prefix. Point it at tmp_path
    # so this test exercises the full upload path.
    monkeypatch.setenv("SESSION_OUTPUT_DIR", str(tmp_path))

    # Create a fake file the dispatcher will reference.
    f1 = tmp_path / "leads.xlsx"
    f1.write_bytes(b"dummy")

    payload = {
        "headline": "Win rate down",
        "attachments": [str(f1)],
    }
    tool_input = {"response_type": "ad_hoc_investigation_result", "payload": payload}

    posted_files = []

    def fake_post_file(file_path, reply_to=None, channel=None, **kw):
        posted_files.append(file_path)

    with (
        patch("session_runner.send_notification", return_value="ts-x"),
        patch("session_runner.post_file", side_effect=fake_post_file),
    ):
        result_text = session_runner._dispatch_post_report(
            tool_input,
            thread_ts="thread-x",
            session_id="sess-att",
            verbosity="summary",
        )
        # Both the agent-supplied xlsx AND the auto-generated .docx
        # (floating-prancing-trinket PR 6) must be uploaded.
        assert _wait_for(lambda: len(posted_files) >= 2, timeout=2.0)

    result = json.loads(result_text)
    assert result["ok"] is True
    # Agent-supplied attachment + auto-generated .docx sibling.
    assert result["attachments_count"] == 2
    assert str(f1) in posted_files
    assert any(p.endswith(".docx") for p in posted_files), (
        f"expected auto-generated .docx alongside xlsx; got {posted_files}"
    )


def test_post_report_uploads_tracked_virtualized_files_when_agent_forgets(
    tmp_path, monkeypatch
):
    """Safety net: the agent didn't include attachments in the payload but the
    session has virtualized files tracked — those still get uploaded.
    """
    import session_runner

    monkeypatch.setenv("SESSION_OUTPUT_DIR", str(tmp_path))

    f1 = tmp_path / "leads.xlsx"
    f1.write_bytes(b"dummy")

    # Pretend a prior tool call virtualized this file.
    session_runner._track_virtualized_file("sess-forget", str(f1))

    payload = {"headline": "Win rate down"}  # NO attachments field
    tool_input = {"response_type": "ad_hoc_investigation_result", "payload": payload}

    posted_files = []

    def fake_post_file(file_path, reply_to=None, channel=None, **kw):
        posted_files.append(file_path)

    with (
        patch("session_runner.send_notification", return_value="ts-y"),
        patch("session_runner.post_file", side_effect=fake_post_file),
    ):
        session_runner._dispatch_post_report(
            tool_input,
            thread_ts="thread-y",
            session_id="sess-forget",
            verbosity="summary",
        )
        # Virtualized xlsx + auto-generated .docx sibling
        # (floating-prancing-trinket PR 6).
        assert _wait_for(lambda: len(posted_files) >= 2, timeout=2.0)

    assert str(f1) in posted_files
    assert any(p.endswith(".docx") for p in posted_files), (
        f"expected auto-generated .docx alongside virtualized xlsx; got {posted_files}"
    )
    # Counter was consumed — subsequent post_report wouldn't re-upload.
    assert session_runner._consume_virtualized_files("sess-forget") == []


# ──────────────────────────────────────────────────────────────────────────
# B1 — Recovery threshold: bloated session is archived and DB row cleared
#      BEFORE the fresh-start branch can be undone by a thread→session
#      lookup. Mocks the exact contradiction observed for
#      sesn_EXAMPLE (Railway logs 2026-05-12):
#        1) "cannot resume, will restart"
#        2) "Starting fresh session"
#        3) "Restored session ... from DB"  ← MUST NOT happen post-fix
#        4) "Continuing session"             ← MUST NOT happen post-fix
# ──────────────────────────────────────────────────────────────────────────


def test_recovery_archives_bloated_session_and_invalidates_db_map():
    """Recovery of a bloated session: archive + DB delete must run BEFORE
    run_adhoc_mcp_session would do its thread→session lookup. Otherwise the
    lookup re-attaches the bot to the dead session and inherits its cached
    context (the $13.87 incident).
    """
    import session_runner

    # Bloated usage: 600K input-side > 500K threshold.
    bloated_usage = MagicMock()
    bloated_usage.input_tokens = 100_000
    bloated_usage.output_tokens = 0
    bloated_usage.cache_read_input_tokens = 400_000
    cc = MagicMock()
    cc.ephemeral_5m_input_tokens = 100_000
    cc.ephemeral_1h_input_tokens = 0
    bloated_usage.cache_creation = cc

    bloated_session = MagicMock()
    bloated_session.status = "idle"
    bloated_session.usage = bloated_usage

    interrupted_inv = {
        "id": 42,
        "question": "How many leads booked discovery calls?",
        "thread_ts": "1234567890.000100",
        "channel_id": "C01TEST",
        "user_id": "U01TEST",
        "session_id": "sesn_EXAMPLE",
        "portco_key": "acme",
        "recovery_count": 0,
    }

    # Track the order of side effects. The fix is correct iff archive +
    # delete_thread_session both fire BEFORE run_adhoc_mcp_session.
    call_order = []

    def fake_archive(sid):
        call_order.append(("archive", sid))

    def fake_delete_thread_session(thread_ts, channel_id=None):
        # Migration 00AJ — channel_id is now positional-or-keyword.
        call_order.append(("delete_thread_session", thread_ts, channel_id))

    def fake_run_adhoc(*args, **kwargs):
        call_order.append(("run_adhoc_mcp_session", kwargs.get("existing_inv_id")))
        # Verify the DB row is gone by the time fresh-start fires.
        thread_ts = args[2] if len(args) > 2 else None
        return "sesn_EXAMPLE"

    with (
        patch.object(
            session_runner.db_adapter,
            "get_interrupted_investigations",
            return_value=[interrupted_inv],
        ),
        patch.object(session_runner.db_adapter, "mark_investigation_recovering"),
        patch.object(session_runner.db_adapter, "update_investigation"),
        patch.object(
            session_runner.db_adapter,
            "delete_thread_session",
            side_effect=fake_delete_thread_session,
        ),
        patch.object(
            session_runner.client.beta.sessions,
            "retrieve",
            return_value=bloated_session,
        ),
        patch.object(
            session_runner.client.beta.sessions, "archive", side_effect=fake_archive
        ),
        patch.object(session_runner, "send_notification"),
        patch.object(
            session_runner, "run_adhoc_mcp_session", side_effect=fake_run_adhoc
        ) as mock_run_fresh,
    ):
        recovered = session_runner.recover_interrupted_investigations()

    # The fresh session was created.
    mock_run_fresh.assert_called_once()
    assert recovered == [42]

    # Order: archive + delete_thread_session both run BEFORE run_adhoc_mcp_session.
    # The exact order between archive and delete doesn't matter for correctness;
    # what matters is that NEITHER follows the fresh-start.
    step_names = [step[0] for step in call_order]
    assert step_names.index("archive") < step_names.index("run_adhoc_mcp_session"), (
        f"archive must precede fresh-start; order was {step_names}"
    )
    assert step_names.index("delete_thread_session") < step_names.index(
        "run_adhoc_mcp_session"
    ), f"delete_thread_session must precede fresh-start; order was {step_names}"

    # The archived session id matches and the deleted thread matches.
    archive_call = [s for s in call_order if s[0] == "archive"][0]
    assert archive_call[1] == "sesn_EXAMPLE"
    delete_call = [s for s in call_order if s[0] == "delete_thread_session"][0]
    assert delete_call[1] == "1234567890.000100"
    # Migration 00AJ — delete is scoped to channel; assert channel_id flowed through.
    assert delete_call[2] == "C01TEST"


def test_recovery_resumes_when_session_within_threshold():
    """Lean session (under threshold) resumes normally — no archive, no fresh."""
    import session_runner

    lean_usage = MagicMock()
    lean_usage.input_tokens = 10_000
    lean_usage.output_tokens = 0
    lean_usage.cache_read_input_tokens = 50_000
    cc = MagicMock()
    cc.ephemeral_5m_input_tokens = 5_000
    cc.ephemeral_1h_input_tokens = 0
    lean_usage.cache_creation = cc

    lean_session = MagicMock()
    lean_session.status = "idle"
    lean_session.usage = lean_usage

    interrupted_inv = {
        "id": 43,
        "question": "lean question",
        "thread_ts": "1234567890.000200",
        "channel_id": "C01TEST",
        "user_id": "U01TEST",
        "session_id": "sesn_EXAMPLE",
        "portco_key": "acme",
        "recovery_count": 0,
    }

    # PR 8 / codex P2 (2026-05-14): the recovery branch now refuses to
    # resume when the row's stored config_version does not match the
    # live value. Seed the thread_sessions lookup with the live stamp so
    # this "lean session resumes" path is exercised (the stale-version
    # archive branch is covered by a sibling test).
    live_cfg = session_runner.db_adapter.current_config_version()

    with (
        patch.object(
            session_runner.db_adapter,
            "get_interrupted_investigations",
            return_value=[interrupted_inv],
        ),
        patch.object(
            session_runner.db_adapter,
            "get_thread_session_record",
            return_value=("sesn_EXAMPLE", live_cfg),
        ),
        patch.object(session_runner.db_adapter, "mark_investigation_recovering"),
        patch.object(session_runner.db_adapter, "update_investigation"),
        patch.object(session_runner.db_adapter, "delete_thread_session") as mock_delete,
        patch.object(
            session_runner.client.beta.sessions, "retrieve", return_value=lean_session
        ),
        patch.object(session_runner.client.beta.sessions, "archive") as mock_archive,
        patch.object(session_runner, "send_notification"),
        patch.object(
            session_runner,
            "_stream_and_handle",
            return_value=(
                [],
                __import__("lifecycle").DeliveryState.DELIVERED_VIA_POST_REPORT,
                None,
                [],
            ),
        ),
        patch.object(session_runner, "_download_session_files"),
        patch.object(session_runner, "run_adhoc_mcp_session") as mock_run_fresh,
    ):
        session_runner.recover_interrupted_investigations()

    # Lean session: resume path. No archive, no DB delete, no fresh start.
    mock_archive.assert_not_called()
    mock_delete.assert_not_called()
    mock_run_fresh.assert_not_called()


def test_recovery_archives_when_config_version_stale():
    """Recovery refuses to resume when the row's stored config_version
    no longer matches the live value (codex P2, 2026-05-14).

    The session's ``multiagent.agents`` roster pins are frozen at
    create time. If a prompt deploy bumped ``active_versions.json``
    while the session was offline, resuming would silently run the old
    sub-agent pins for the rest of the thread. The guard must archive
    the old session and start fresh instead.
    """
    import session_runner

    # Lean usage so the size-threshold branch does not pre-empt our
    # config-version branch.
    lean_usage = MagicMock()
    lean_usage.input_tokens = 10_000
    lean_usage.output_tokens = 0
    lean_usage.cache_read_input_tokens = 50_000
    cc = MagicMock()
    cc.ephemeral_5m_input_tokens = 5_000
    cc.ephemeral_1h_input_tokens = 0
    lean_usage.cache_creation = cc

    lean_session = MagicMock()
    lean_session.status = "idle"
    lean_session.usage = lean_usage

    interrupted_inv = {
        "id": 44,
        "question": "stale-prompt question",
        "thread_ts": "1234567890.000300",
        "channel_id": "C01TEST",
        "user_id": "U01TEST",
        "session_id": "sesn_EXAMPLE",
        "portco_key": "acme",
        "recovery_count": 0,
    }

    with (
        patch.object(
            session_runner.db_adapter,
            "get_interrupted_investigations",
            return_value=[interrupted_inv],
        ),
        # Row stamp != live stamp. Note: the live stamp comes from
        # ``current_config_version()`` reading the real
        # ``agents/active_versions.json``; we just need the stored
        # stamp to differ.
        patch.object(
            session_runner.db_adapter,
            "get_thread_session_record",
            return_value=("sesn_EXAMPLE", "deadbeefcafef00d"),
        ),
        patch.object(session_runner.db_adapter, "mark_investigation_recovering"),
        patch.object(session_runner.db_adapter, "update_investigation"),
        patch.object(session_runner.db_adapter, "delete_thread_session"),
        patch.object(
            session_runner.client.beta.sessions, "retrieve", return_value=lean_session
        ),
        patch.object(session_runner.client.beta.sessions, "archive") as mock_archive,
        patch.object(session_runner, "send_notification"),
        patch.object(
            session_runner, "run_adhoc_mcp_session", return_value="sesn_EXAMPLE"
        ) as mock_run_fresh,
    ):
        recovered = session_runner.recover_interrupted_investigations()

    # Stale config_version: archive the old session, then fresh-start.
    mock_archive.assert_called_once_with("sesn_EXAMPLE")
    mock_run_fresh.assert_called_once()
    assert recovered == [44]


def test_recovery_archives_when_live_config_version_unknown():
    """Fail-closed parity: when ``current_config_version()`` returns
    ``None`` (pin file missing/unreadable) the recovery branch must
    NOT resume — same policy the reuse path uses on the cache lookup
    side. Verified by patching ``current_config_version`` to return
    ``None`` for this test.
    """
    import session_runner

    lean_usage = MagicMock()
    lean_usage.input_tokens = 10_000
    lean_usage.output_tokens = 0
    lean_usage.cache_read_input_tokens = 50_000
    cc = MagicMock()
    cc.ephemeral_5m_input_tokens = 5_000
    cc.ephemeral_1h_input_tokens = 0
    lean_usage.cache_creation = cc

    lean_session = MagicMock()
    lean_session.status = "idle"
    lean_session.usage = lean_usage

    interrupted_inv = {
        "id": 45,
        "question": "fail-closed question",
        "thread_ts": "1234567890.000400",
        "channel_id": "C01TEST",
        "user_id": "U01TEST",
        "session_id": "sesn_EXAMPLE",
        "portco_key": "acme",
        "recovery_count": 0,
    }

    with (
        patch.object(
            session_runner.db_adapter,
            "get_interrupted_investigations",
            return_value=[interrupted_inv],
        ),
        patch.object(
            session_runner.db_adapter,
            "get_thread_session_record",
            return_value=("sesn_EXAMPLE", "abc123def456abcd"),
        ),
        patch.object(
            session_runner.db_adapter,
            "current_config_version",
            return_value=None,
        ),
        patch.object(session_runner.db_adapter, "mark_investigation_recovering"),
        patch.object(session_runner.db_adapter, "update_investigation"),
        patch.object(session_runner.db_adapter, "delete_thread_session"),
        patch.object(
            session_runner.client.beta.sessions, "retrieve", return_value=lean_session
        ),
        patch.object(session_runner.client.beta.sessions, "archive") as mock_archive,
        patch.object(session_runner, "send_notification"),
        patch.object(
            session_runner, "run_adhoc_mcp_session", return_value="sesn_EXAMPLE"
        ) as mock_run_fresh,
    ):
        recovered = session_runner.recover_interrupted_investigations()

    mock_archive.assert_called_once_with("sesn_EXAMPLE")
    mock_run_fresh.assert_called_once()
    assert recovered == [45]


def test_post_report_hook_tolerates_missing_surface_pusher_module():
    """When surface_pusher is not importable (F6 not landed yet), the hook
    swallows the ImportError and the post still succeeds.
    """
    from session_runner import _dispatch_post_report

    # Make sure surface_pusher is NOT present in sys.modules. The lazy
    # `from surface_pusher import push_to_canvas` will then raise ImportError,
    # which the hook's broad try/except swallows.
    _remove_fake_surface_pusher()
    # Also block any real import path while the test runs.
    blocker = MagicMock(side_effect=ImportError("surface_pusher not landed"))
    # None sentinel in sys.modules is the documented Python idiom for
    # blocking imports (see import system docs). Pyright sees sys.modules
    # as dict[str, ModuleType] and flags this — type: ignore is correct.
    sys.modules["surface_pusher"] = None  # type: ignore[assignment]  # noqa: PLE0606

    try:
        with patch("session_runner.send_notification") as mock_send:
            mock_send.return_value = "ts-no-pusher"
            result_text = _dispatch_post_report(
                _make_valid_quick_answer_tool_input(),
                thread_ts="thread-no-pusher",
                session_id="sess-no-pusher",
                verbosity="summary",
                portco_key="acme",
            )

        result = json.loads(result_text)
        assert result["ok"] is True
        assert result["message_ts"] == "ts-no-pusher"
    finally:
        # Restore sys.modules so other tests aren't affected.
        sys.modules.pop("surface_pusher", None)
        _ = blocker  # silence "unused" lint


# ──────────────────────────────────────────────────────────────────────────
# Thread-continuation inv_id minting (2026-05-13 codex Option A)
# ──────────────────────────────────────────────────────────────────────────
#
# Production incident: sesn_EXAMPLE (2026-05-13 05:15 UTC)
# completed successfully but the lifecycle log showed inv_id=None on the
# terminalize calls — the existing-session-reuse branch inherited the
# caller's inv_id (None for new follow-ups) instead of minting a new row.
# The Slack reaction flipped correctly but investigations.status stayed
# stuck at 'running' from the original thread message.
#
# The six tests below lock in the new contract: one investigations row
# per Slack user message that triggers work, regardless of whether the
# session itself is reused.


def _stub_session_runner_for_thread_followup(
    session_runner, monkeypatch, *, existing_inv_id_already_set=False
):
    """Stub the moving parts of run_adhoc_mcp_session for thread-followup
    integration tests. Returns the MagicMocks the test can assert on.

    Tests using this fixture should set existing_inv_id_already_set=True
    only when they pass a non-None ``existing_inv_id`` to
    run_adhoc_mcp_session — the assertion semantics for create_investigation
    differ between mint-new and reuse-existing cases.
    """
    # 1. Thread-session lookup: pretend a session exists with the current
    # config_version stamp so the reuse path proceeds (mismatch would
    # trigger the rotate-stale branch — see Plan #44 PR 8).
    mock_get_thread_session = MagicMock(return_value="sesn_EXAMPLE")
    mock_get_thread_session_record = MagicMock(
        return_value=("sesn_EXAMPLE", "deadbeefcafe1234")
    )
    monkeypatch.setattr(
        session_runner.db_adapter, "get_thread_session", mock_get_thread_session
    )
    monkeypatch.setattr(
        session_runner.db_adapter,
        "get_thread_session_record",
        mock_get_thread_session_record,
    )
    # Pin the live config_version so the reuse compare matches.
    monkeypatch.setattr(
        session_runner.db_adapter,
        "current_config_version",
        MagicMock(return_value="deadbeefcafe1234"),
    )
    # 2. Investigation row helpers. transition_queued_to_running returns
    # True (won the race) so the test path proceeds to streaming.
    mock_create = MagicMock(return_value=999)
    mock_update = MagicMock()
    mock_transition_qr = MagicMock(return_value=True)
    monkeypatch.setattr(session_runner.db_adapter, "create_investigation", mock_create)
    monkeypatch.setattr(session_runner.db_adapter, "update_investigation", mock_update)
    monkeypatch.setattr(
        session_runner.db_adapter,
        "transition_queued_to_running",
        mock_transition_qr,
    )
    # 3. Stub _stream_and_handle so we don't actually open a stream.
    from lifecycle import DeliveryState

    mock_stream = MagicMock(
        return_value=([], DeliveryState.DELIVERED_VIA_POST_REPORT, None, [])
    )
    monkeypatch.setattr(session_runner, "_stream_and_handle", mock_stream)
    # 4. Stub the surface push + file download so the function returns.
    monkeypatch.setattr(session_runner, "_download_session_files", MagicMock())
    monkeypatch.setattr(session_runner, "transition_reaction", MagicMock())
    # 5. Stub _log_session_usage so it doesn't try the network.
    monkeypatch.setattr(session_runner, "_log_session_usage", MagicMock())
    # 6. Resolve portco without network.
    monkeypatch.setattr(
        session_runner, "_resolve_portco", MagicMock(return_value="acme")
    )
    return {
        "create": mock_create,
        "update": mock_update,
        "transition_qr": mock_transition_qr,
        "stream": mock_stream,
        "get_thread_session": mock_get_thread_session,
    }


def test_thread_followup_with_no_inv_id_mints_new_row(monkeypatch):
    """Follow-up in an existing thread → new investigation row minted.

    Pre-fix: this branch inherited inv_id=None from the caller and the
    DB row was never touched. Post-fix: a fresh row is minted and the
    new inv_id is plumbed into _stream_and_handle.
    """
    import session_runner

    stubs = _stub_session_runner_for_thread_followup(session_runner, monkeypatch)

    session_runner.run_adhoc_mcp_session(
        question="follow-up question",
        user_id="U999",
        thread_ts="1737654321.000100",
        channel_id="C999",
        existing_inv_id=None,
        event_ts="1737654322.000200",
    )

    # New row was created on the follow-up.
    stubs["create"].assert_called_once()
    create_kwargs = stubs["create"].call_args.kwargs
    assert create_kwargs["question"] == "follow-up question"
    assert create_kwargs["thread_ts"] == "1737654321.000100"
    assert create_kwargs["event_ts"] == "1737654322.000200"

    # The new row was atomically transitioned queued → running via the
    # race-safe helper (codex P2 fix). Asserts the new-id + reused
    # session_id were both threaded correctly.
    stubs["transition_qr"].assert_called_once_with(999, "sesn_EXAMPLE")

    # _stream_and_handle received the new inv_id.
    stream_kwargs = stubs["stream"].call_args.kwargs
    assert stream_kwargs["inv_id"] == 999


def test_thread_followup_with_explicit_inv_id_reuses_it(monkeypatch):
    """Caller-supplied existing_inv_id (recovery path) → no new row minted.

    Used by recover_interrupted_investigations to resume an existing
    row. Minting a duplicate row here would corrupt the audit trail.
    """
    import session_runner

    stubs = _stub_session_runner_for_thread_followup(
        session_runner, monkeypatch, existing_inv_id_already_set=True
    )

    session_runner.run_adhoc_mcp_session(
        question="resumed",
        user_id="U999",
        thread_ts="1737654321.000100",
        channel_id="C999",
        existing_inv_id=42,
        event_ts="1737654322.000200",
    )

    # No new row created — we reused inv_id=42.
    stubs["create"].assert_not_called()
    # _stream_and_handle received the reused inv_id.
    stream_kwargs = stubs["stream"].call_args.kwargs
    assert stream_kwargs["inv_id"] == 42


def test_thread_followup_exception_in_stream_terminalizes_via_guard(monkeypatch):
    """Uncaught exception in _stream_and_handle on the reuse branch →
    terminalize_lifecycle(TERMINAL_FAILURE, ...) fires before re-raise.

    Pre-fix the reuse branch had no _run_investigation_guarded wrapper —
    an exception would bubble to the caller with the row still 'running'.
    """
    import session_runner

    # Set up the standard stubs, then override _stream_and_handle to raise.
    stubs = _stub_session_runner_for_thread_followup(session_runner, monkeypatch)
    stubs["stream"].side_effect = RuntimeError("boom in stream")

    # Capture terminalize calls.
    mock_terminalize = MagicMock()
    monkeypatch.setattr("lifecycle.terminalize_lifecycle", mock_terminalize)

    import pytest as _pytest

    with _pytest.raises(RuntimeError, match="boom in stream"):
        session_runner.run_adhoc_mcp_session(
            question="will explode",
            user_id="U999",
            thread_ts="1737654321.000100",
            channel_id="C999",
            existing_inv_id=None,
            event_ts="1737654322.000200",
        )

    # New row was still minted before the exception.
    stubs["create"].assert_called_once()
    # _run_investigation_guarded fired terminalize_lifecycle(TERMINAL_FAILURE)
    # with the new inv_id (999).
    from lifecycle import DeliveryState

    terminal_calls = [
        c
        for c in mock_terminalize.call_args_list
        if c.args and c.args[0] == DeliveryState.TERMINAL_FAILURE
    ]
    assert len(terminal_calls) == 1
    assert terminal_calls[0].kwargs.get("inv_id") == 999
    assert "unhandled_exception" in terminal_calls[0].kwargs.get("error_message", "")


def test_thread_followup_post_report_terminalize_once_per_inv_id(monkeypatch):
    """post_report success → terminalize_lifecycle called with the new inv_id.

    Locks in that the in-memory idempotency map's call count matches
    what the operator expects from /cost / analytics.
    """
    import session_runner

    stubs = _stub_session_runner_for_thread_followup(session_runner, monkeypatch)

    mock_terminalize = MagicMock(return_value=None)
    monkeypatch.setattr("lifecycle.terminalize_lifecycle", mock_terminalize)

    session_runner.run_adhoc_mcp_session(
        question="success path",
        user_id="U999",
        thread_ts="1737654321.000100",
        channel_id="C999",
        existing_inv_id=None,
        event_ts="1737654322.000200",
    )

    # New row minted (inv_id=999).
    stubs["create"].assert_called_once()
    # The delivered-via-post_report terminalize was called with inv_id=999.
    from lifecycle import DeliveryState

    delivered_calls = [
        c
        for c in mock_terminalize.call_args_list
        if c.args and c.args[0] == DeliveryState.DELIVERED_VIA_POST_REPORT
    ]
    assert len(delivered_calls) >= 1
    assert delivered_calls[0].kwargs.get("inv_id") == 999


def test_get_investigation_for_thread_prefers_active_row():
    """The new ORDER BY returns the active row before the latest terminal.

    With one row per Slack user message, a thread accumulates rows.
    /stop and in-thread cancel need the running row, not the latest
    completed one.
    """
    import db_adapter

    fake_cur = MagicMock()
    # Fake the row that the SQL returns — the test cares about the
    # query SHAPE, not the DB behavior.
    fake_cur.fetchone.return_value = {
        "id": 42,
        "thread_ts": "1737654321.000100",
        "status": "running",
        "channel_id": "C999",
        "user_id": "U1",
        "question": "q",
        "portco_key": "acme",
        "session_id": "sesn_EXAMPLE",
        "agent_id": "agent_x",
        "started_at": None,
        "completed_at": None,
        "error_message": None,
        "recovery_count": 0,
        "container_id": "c1",
        "event_ts": "1737654322.000200",
    }
    fake_conn = MagicMock()
    fake_conn.cursor.return_value.__enter__.return_value = fake_cur

    with (
        patch.object(db_adapter, "_connect", return_value=fake_conn),
        patch.object(db_adapter, "DATABASE_URL", "postgres://test"),
    ):
        result = db_adapter.get_investigation_for_thread("1737654321.000100")

    assert result is not None
    assert result["id"] == 42
    # Verify the SQL includes the new ORDER BY clause that prefers
    # queued/running rows.
    executed_sql = fake_cur.execute.call_args.args[0]
    assert "CASE WHEN status IN ('queued', 'running') THEN 0 ELSE 1 END" in executed_sql


def test_stop_in_thread_with_mixed_rows_picks_active(monkeypatch):
    """End-to-end: /stop in a thread with running + completed rows picks
    the running one to interrupt and terminalize as USER_CANCELLED.

    Locks in that the ORDER BY change + the /stop handler chain
    together do the right thing under the realistic 'thread with
    multiple investigations' shape.
    """
    import slack_bot

    # Mock the investigation lookup to return the running row (which is
    # what the new ORDER BY surfaces over the latest completed row).
    running_row = {
        "id": 42,
        "thread_ts": "1737654321.000100",
        "status": "running",
        "channel_id": "C999",
        "user_id": "U_AUTHOR",
        "session_id": "sesn_EXAMPLE",
        "agent_id": "agent_x",
        "event_ts": "1737654322.000200",
    }
    monkeypatch.setattr(
        "db_adapter.get_investigation_for_thread",
        MagicMock(return_value=running_row),
    )
    # Stub the interrupt — say it succeeded.
    fake_interrupt = MagicMock()
    fake_interrupt.interrupt_session = MagicMock(
        return_value={"ok": True, "tokens_burned": 1234, "cost_usd": 0.01}
    )
    monkeypatch.setitem(sys.modules, "session_interrupt", fake_interrupt)

    mock_terminalize = MagicMock()
    monkeypatch.setattr("lifecycle.terminalize_lifecycle", mock_terminalize)
    monkeypatch.setattr(
        slack_bot, "_stop_command_enabled", MagicMock(return_value=True)
    )

    result = slack_bot._handle_stop_command(
        raw_text="",
        user_id="U_AUTHOR",
        _channel_id="C999",
        current_thread_ts="1737654321.000100",
    )

    # The interrupt fired against the active session, not a stale one.
    fake_interrupt.interrupt_session.assert_called_once_with("sesn_EXAMPLE")
    # terminalize_lifecycle was called with USER_CANCELLED + the active
    # row's inv_id (42).
    from lifecycle import DeliveryState

    user_cancelled_calls = [
        c
        for c in mock_terminalize.call_args_list
        if c.args and c.args[0] == DeliveryState.USER_CANCELLED
    ]
    assert len(user_cancelled_calls) == 1
    assert user_cancelled_calls[0].kwargs.get("inv_id") == 42
    assert ":octagonal_sign:" in result


def test_fallback_skips_post_report_emits_neutral_post_and_attaches_files(
    monkeypatch, tmp_path
):
    """Plan v2 PR 1 (2026-05-14): when the Coordinator forgets to call
    post_report and the stream returns with accumulated agent.message
    text but no delivered state, the fallback path must NOT echo the
    agent transcript to Slack (the previous behavior leaked
    chain-of-thought with a truncated-SOQL title). Instead it must:

      1. Emit a single neutral post_analysis with the literal title
         "Investigation incomplete" (NEVER the question or any SOQL).
      2. NOT include any of the agent's text in the analysis body.
      3. Consume virtualized files tracked on the session and upload
         each via post_file.
      4. Terminalize the lifecycle as NO_OUTPUT (the user's reaction
         lands on ❌ to reflect that no findings were delivered).
    """
    import session_runner

    stubs = _stub_session_runner_for_thread_followup(session_runner, monkeypatch)

    from lifecycle import DeliveryState

    leaked_narration = (
        "Let me scope the task and check the size of the open opp set.\n"
        "I'll wait for the three specialists to return.\n"
        "Pipeline Monitor confirmed Kapa is down. I'll use the static "
        "product portfolio in memory as a fallback. Waiting on the "
        "two data pulls.\n"
        "Waiting on the statistician's full build."
    )
    leaked_soql_draft = (
        "SELECT Id, Name, StageName, RecordType.Name FROM Opportunity "
        "WHERE CloseDate >= 2026-04-01 AND CloseDate <= 2026-06-30"
    )

    stubs["stream"].return_value = (
        [leaked_narration],
        DeliveryState.NOT_DELIVERED,
        None,
        [],
    )

    fake_parquet = tmp_path / "sf_open_opps.parquet"
    fake_parquet.write_text("parquet-payload")
    fake_xlsx = tmp_path / "sf_open_opps.xlsx"
    fake_xlsx.write_text("xlsx-payload")
    with session_runner._session_virtualized_files_lock:
        session_runner._session_virtualized_files["sesn_EXAMPLE"] = [
            str(fake_parquet),
            str(fake_xlsx),
            "/tmp/does-not-exist.xlsx",
        ]

    captured_post_analysis = MagicMock()
    captured_attach_async = MagicMock()
    captured_terminalize = MagicMock()
    monkeypatch.setattr(session_runner, "post_analysis", captured_post_analysis)
    monkeypatch.setattr(session_runner, "_attach_files_async", captured_attach_async)
    monkeypatch.setattr("lifecycle.terminalize_lifecycle", captured_terminalize)

    # The fallback's preview count uses _prefer_xlsx_sibling +
    # _is_safe_attachment_path to mirror what _attach_files_async
    # actually uploads. _is_safe_attachment_path whitelists to the
    # session output dir, which tmp_path isn't, so without these
    # stubs the preview would always count zero. Mock both helpers
    # to accept the test paths; _attach_files_async itself is
    # already stubbed.
    monkeypatch.setattr(
        session_runner,
        "_is_safe_attachment_path",
        lambda p: not p.startswith("/tmp/does-not-exist"),
    )
    monkeypatch.setattr(session_runner, "_prefer_xlsx_sibling", lambda p: p)

    session_runner.run_adhoc_mcp_session(
        question=leaked_soql_draft,
        user_id="U999",
        thread_ts="1737654321.000100",
        channel_id="C999",
        existing_inv_id=None,
        event_ts="1737654322.000200",
    )

    # 1. post_analysis called exactly once, with the literal neutral
    # title — NOT the SOQL draft that polluted ``question``.
    assert captured_post_analysis.call_count == 1
    pa_kwargs = captured_post_analysis.call_args.kwargs
    assert pa_kwargs["title"] == "Investigation incomplete"
    assert pa_kwargs["queries"] == []

    # 2. The agent narration is NOT in the analysis body — the whole
    # point of this fix.
    body = pa_kwargs["analysis_text"]
    assert "Pipeline Monitor" not in body
    assert "statistician" not in body
    assert "Let me scope" not in body
    assert "Waiting on" not in body
    # And the SOQL draft must not be in the title or body either.
    assert "SELECT" not in pa_kwargs["title"]
    assert "SELECT" not in body
    # The body should reference the session ID for debuggability.
    assert "sesn_EXAMPLE" in body
    assert "2 raw output" in body  # the two files that exist

    # 3. Attachment path: _attach_files_async was called once with the
    # full tracked list. We delegate to that vetted pipeline so it can
    # apply the Parquet→.xlsx-sibling swap, the safe-path check, and
    # the existence filter on its daemon thread (codex P2 review
    # 2026-05-14 — the previous direct post_file loop here bypassed
    # the swap, sending raw .parquet files Slack users can't open).
    assert captured_attach_async.call_count == 1
    attach_args = captured_attach_async.call_args
    files_arg = (
        attach_args.args[0] if attach_args.args else attach_args.kwargs.get("files")
    )
    assert files_arg is not None, "expected _attach_files_async to be called with files"
    assert str(fake_parquet) in files_arg
    assert str(fake_xlsx) in files_arg
    assert "/tmp/does-not-exist.xlsx" in files_arg  # full list — async filters
    # reply_to threaded through so the upload lands in the same Slack
    # thread as the neutral failure post.
    assert attach_args.kwargs.get("reply_to") == "1737654321.000100"

    # 4. Lifecycle terminalized as NO_OUTPUT with a descriptive
    # error_message — NOT DELIVERED_VIA_POST_ANALYSIS (which would
    # have flipped to ✅ and contradicted the "incomplete" framing).
    no_output_calls = [
        c
        for c in captured_terminalize.call_args_list
        if c.args and c.args[0] == DeliveryState.NO_OUTPUT
    ]
    assert len(no_output_calls) == 1
    assert (
        no_output_calls[0].kwargs.get("error_message")
        == "coordinator_skipped_post_report"
    )

    # 5. Virtualized files tracker was drained (no leak across
    # sessions).
    with session_runner._session_virtualized_files_lock:
        assert "sesn_EXAMPLE" not in session_runner._session_virtualized_files


def test_thread_followup_cancelled_in_race_window_skips_stream(monkeypatch):
    """If /stop fires between create_investigation and the running flip,
    transition_queued_to_running returns False and we bail out of the
    stream — preserves the user's cancel intent.
    """
    import session_runner

    stubs = _stub_session_runner_for_thread_followup(session_runner, monkeypatch)
    # Simulate /stop landing in the race window — the queued→running
    # transition fails because the row is already cancelled.
    stubs["transition_qr"].return_value = False

    session_runner.run_adhoc_mcp_session(
        question="will be cancelled mid-race",
        user_id="U999",
        thread_ts="1737654321.000100",
        channel_id="C999",
        existing_inv_id=None,
        event_ts="1737654322.000200",
    )

    # New row was created (race insert).
    stubs["create"].assert_called_once()
    # transition_queued_to_running was attempted but lost the race.
    stubs["transition_qr"].assert_called_once()
    # Stream was SKIPPED — the user's stop intent wins.
    stubs["stream"].assert_not_called()


# ──────────────────────────────────────────────────────────────────────────
# config_version invalidation (Plan #44 PR 8, migration 00AN)
# ──────────────────────────────────────────────────────────────────────────


def _clear_thread_session_caches(session_runner) -> None:
    """Empty both in-memory maps so a test starts from a clean slate."""
    with session_runner._thread_sessions_lock:
        session_runner._thread_sessions.clear()
        session_runner._thread_session_versions.clear()


def test_cached_session_with_current_config_version_is_reused(monkeypatch):
    """When the cached row's stamp matches the live config_version,
    the reuse path proceeds — same behavior as pre-PR8.
    """
    import session_runner

    stubs = _stub_session_runner_for_thread_followup(session_runner, monkeypatch)
    _clear_thread_session_caches(session_runner)

    # Notify is captured so we can verify it does NOT fire on a match.
    notify_mock = MagicMock()
    monkeypatch.setattr(session_runner, "send_notification", notify_mock)
    archive_mock = MagicMock()
    monkeypatch.setattr(session_runner, "_archive_and_invalidate_session", archive_mock)

    session_runner.run_adhoc_mcp_session(
        question="match-version follow-up",
        user_id="U999",
        thread_ts="1737654321.000100",
        channel_id="C999",
        existing_inv_id=None,
        event_ts="1737654322.000200",
    )

    # Stream fired against the cached session — the reuse path won.
    stubs["stream"].assert_called_once()
    stream_call = stubs["stream"].call_args
    # Second positional is the existing session id when reusing.
    args = stream_call.args
    assert "sesn_EXAMPLE" in args, args
    # Archive + notify must NOT fire on a match.
    archive_mock.assert_not_called()
    notify_mock.assert_not_called()


def test_cached_session_with_stale_config_version_is_rotated(monkeypatch):
    """When the cached row's stamp does NOT match the live config_version,
    the orchestrator archives the old session, drops both caches, and
    falls through to mint a fresh session.
    """
    import session_runner

    _stub_session_runner_for_thread_followup(session_runner, monkeypatch)
    _clear_thread_session_caches(session_runner)

    # DB lookup returns a row stamped with the OLD config_version.
    monkeypatch.setattr(
        session_runner.db_adapter,
        "get_thread_session_record",
        MagicMock(return_value=("sesn_EXAMPLE", "oldstamp00000000")),
    )
    # Live current_config_version is the NEW stamp — mismatch triggers rotate.
    monkeypatch.setattr(
        session_runner.db_adapter,
        "current_config_version",
        MagicMock(return_value="newstamp00000000"),
    )

    archive_mock = MagicMock()
    monkeypatch.setattr(session_runner, "_archive_and_invalidate_session", archive_mock)

    # Slack notice should fire so the user sees a clean break.
    notify_mock = MagicMock()
    monkeypatch.setattr(session_runner, "send_notification", notify_mock)

    # The fresh-session branch creates a new Anthropic session — stub it.
    fresh_session = MagicMock(id="sesn_EXAMPLE")
    fake_client = MagicMock()
    fake_client.beta.sessions.create.return_value = fresh_session
    monkeypatch.setattr(session_runner, "client", fake_client)
    monkeypatch.setattr(
        session_runner, "_resolve_agent_param", MagicMock(return_value="agent_x")
    )
    monkeypatch.setattr(
        session_runner, "_preprocess_prompt", MagicMock(return_value={})
    )
    monkeypatch.setattr(session_runner, "_build_adhoc_prompt", lambda *a, **k: "p")
    monkeypatch.setattr(
        session_runner, "_prepend_session_instructions", lambda *a, **k: "p"
    )
    monkeypatch.setattr(session_runner, "_is_simple_lookup", lambda q: True)
    monkeypatch.setattr(session_runner.db_adapter, "save_thread_session", MagicMock())

    session_runner.run_adhoc_mcp_session(
        question="stale-version follow-up",
        user_id="U999",
        thread_ts="1737654321.000100",
        channel_id="C999",
        existing_inv_id=None,
        event_ts="1737654322.000200",
    )

    # The stale session was archived + invalidated.
    archive_mock.assert_called_once()
    archive_args = archive_mock.call_args
    assert archive_args.args[0] == "sesn_EXAMPLE"
    assert archive_args.kwargs.get("channel_id") == "C999"
    assert archive_args.kwargs.get("thread_ts") == "1737654321.000100"

    # The user got a Slack notice in-thread that the session rotated.
    assert notify_mock.call_count >= 1
    notice_kwargs = notify_mock.call_args.kwargs
    assert notice_kwargs.get("reply_to") == "1737654321.000100"
    assert notice_kwargs.get("channel") == "C999"

    # A fresh Anthropic session was minted — the stale id is gone.
    fake_client.beta.sessions.create.assert_called_once()


def test_cached_session_with_null_config_version_is_rotated(monkeypatch):
    """Pre-PR8 rows carry NULL ``config_version``. Reuse must fail closed —
    treat NULL as ``unknown, force fresh`` rather than trusting the row.
    """
    import session_runner

    _stub_session_runner_for_thread_followup(session_runner, monkeypatch)
    _clear_thread_session_caches(session_runner)

    monkeypatch.setattr(
        session_runner.db_adapter,
        "get_thread_session_record",
        MagicMock(return_value=("sesn_EXAMPLE_PR8", None)),
    )
    monkeypatch.setattr(
        session_runner.db_adapter,
        "current_config_version",
        MagicMock(return_value="newstamp00000000"),
    )

    archive_mock = MagicMock()
    monkeypatch.setattr(session_runner, "_archive_and_invalidate_session", archive_mock)
    monkeypatch.setattr(session_runner, "send_notification", MagicMock())

    fresh_session = MagicMock(id="sesn_EXAMPLE")
    fake_client = MagicMock()
    fake_client.beta.sessions.create.return_value = fresh_session
    monkeypatch.setattr(session_runner, "client", fake_client)
    monkeypatch.setattr(
        session_runner, "_resolve_agent_param", MagicMock(return_value="agent_x")
    )
    monkeypatch.setattr(
        session_runner, "_preprocess_prompt", MagicMock(return_value={})
    )
    monkeypatch.setattr(session_runner, "_build_adhoc_prompt", lambda *a, **k: "p")
    monkeypatch.setattr(
        session_runner, "_prepend_session_instructions", lambda *a, **k: "p"
    )
    monkeypatch.setattr(session_runner, "_is_simple_lookup", lambda q: True)
    monkeypatch.setattr(session_runner.db_adapter, "save_thread_session", MagicMock())

    session_runner.run_adhoc_mcp_session(
        question="legacy follow-up",
        user_id="U999",
        thread_ts="1737654321.000100",
        channel_id="C999",
        existing_inv_id=None,
        event_ts="1737654322.000200",
    )

    archive_mock.assert_called_once()
    fake_client.beta.sessions.create.assert_called_once()


def test_in_memory_cache_with_stale_version_falls_through_to_db(monkeypatch):
    """If a hot cache entry has a stale stamp, the orchestrator must NOT
    blindly reuse it. The reuse path falls through to the DB lookup,
    which then either returns the current stamp (already rotated by
    another worker) or surfaces the stale row for archive.
    """
    import session_runner

    _stub_session_runner_for_thread_followup(session_runner, monkeypatch)
    _clear_thread_session_caches(session_runner)

    # Seed the in-memory cache with a stale stamp.
    with session_runner._thread_sessions_lock:
        session_runner._thread_sessions[("C999", "1737654321.000100")] = (
            "sesn_EXAMPLE_MEM"
        )
        session_runner._thread_session_versions[("C999", "1737654321.000100")] = (
            "oldmemstamp00000"
        )

    # DB lookup returns the same stale row (so we still rotate).
    monkeypatch.setattr(
        session_runner.db_adapter,
        "get_thread_session_record",
        MagicMock(return_value=("sesn_EXAMPLE_MEM", "oldmemstamp00000")),
    )
    monkeypatch.setattr(
        session_runner.db_adapter,
        "current_config_version",
        MagicMock(return_value="newmemstamp00000"),
    )

    archive_mock = MagicMock()
    monkeypatch.setattr(session_runner, "_archive_and_invalidate_session", archive_mock)
    monkeypatch.setattr(session_runner, "send_notification", MagicMock())

    fresh_session = MagicMock(id="sesn_EXAMPLE")
    fake_client = MagicMock()
    fake_client.beta.sessions.create.return_value = fresh_session
    monkeypatch.setattr(session_runner, "client", fake_client)
    monkeypatch.setattr(
        session_runner, "_resolve_agent_param", MagicMock(return_value="agent_x")
    )
    monkeypatch.setattr(
        session_runner, "_preprocess_prompt", MagicMock(return_value={})
    )
    monkeypatch.setattr(session_runner, "_build_adhoc_prompt", lambda *a, **k: "p")
    monkeypatch.setattr(
        session_runner, "_prepend_session_instructions", lambda *a, **k: "p"
    )
    monkeypatch.setattr(session_runner, "_is_simple_lookup", lambda q: True)
    monkeypatch.setattr(session_runner.db_adapter, "save_thread_session", MagicMock())

    session_runner.run_adhoc_mcp_session(
        question="stale-memory follow-up",
        user_id="U999",
        thread_ts="1737654321.000100",
        channel_id="C999",
        existing_inv_id=None,
        event_ts="1737654322.000200",
    )

    archive_mock.assert_called_once()
    archive_args = archive_mock.call_args.args
    assert archive_args[0] == "sesn_EXAMPLE_MEM"
    fake_client.beta.sessions.create.assert_called_once()


def test_archive_invalidate_clears_version_map(monkeypatch):
    """_archive_and_invalidate_session must drop the parallel
    ``_thread_session_versions`` entry alongside the session map,
    otherwise the next reuse would re-stamp the dead session with a
    fresh version and skip rotation forever.
    """
    import session_runner

    _clear_thread_session_caches(session_runner)
    with session_runner._thread_sessions_lock:
        session_runner._thread_sessions[("C999", "T1")] = "sesn_EXAMPLE"
        session_runner._thread_session_versions[("C999", "T1")] = "stamp_v1"

    monkeypatch.setattr(session_runner.client, "beta", MagicMock())
    monkeypatch.setattr(session_runner.db_adapter, "delete_thread_session", MagicMock())

    session_runner._archive_and_invalidate_session(
        "sesn_EXAMPLE", thread_ts="T1", channel_id="C999"
    )

    with session_runner._thread_sessions_lock:
        assert ("C999", "T1") not in session_runner._thread_sessions
        assert ("C999", "T1") not in session_runner._thread_session_versions


# ──────────────────────────────────────────────────────────────────────────
# Task #23 — Orphan dead-letter policy on max recovery attempts
# ──────────────────────────────────────────────────────────────────────────
#
# Pre-Task #23: hitting MAX_RECOVERY_ATTEMPTS (2) silently terminalized the
# row via TERMINAL_FAILURE (→ status='failed') and asked the user to re-ask.
# Operators had no signal — the orphan session ``sesn_EXAMPLE``
# sat in 'running' for days while we silently gave up.
#
# Post-Task #23:
#   (a) row → 'orphan_dead_lettered' (new terminal state, migration 00AO)
#   (b) admin DM via send_notification(admin_only=True) with session_id,
#       thread permalink, original question, error history
#   (c) user-facing in-thread message says "an admin has been notified"
#   (d) NO 3rd silent restart — the loop stops for this row


def test_recover_max_attempts_dispatches_admin_dm():
    """When recovery_count >= 2, the dead-letter path fires:
    - send_notification called with admin_only=True
    - row updated to 'orphan_dead_lettered'
    - run_adhoc_mcp_session is NOT called (no 3rd silent retry)
    """
    import session_runner

    interrupted_inv = {
        "id": 77,
        "question": "Why did win-rate drop?",
        "thread_ts": "1234567890.000300",
        "channel_id": "C01TEST",
        "user_id": "U01TEST",
        "session_id": "sesn_EXAMPLE",
        "portco_key": "acme",
        "recovery_count": 2,  # hits the >= MAX_RECOVERY_ATTEMPTS branch
        "error_message": "previous attempt: timeout after 60m",
        "event_ts": "1234567890.000400",
        # Fresh started_at so the 25-day archive branch doesn't fire first.
        "started_at": None,
    }

    admin_dm_calls = []
    user_thread_calls = []

    def fake_send_notification(*args, **kwargs):
        # Capture full call signature so tests can assert against positional
        # severity/summary as well as kwargs.
        call = {"args": args, "kwargs": kwargs}
        if kwargs.get("admin_only") is True:
            admin_dm_calls.append(call)
        elif kwargs.get("reply_to"):
            user_thread_calls.append(call)
        return "ts-stub"

    dead_letter_calls = []

    def fake_dead_letter(inv_id, error_message=None):
        dead_letter_calls.append((inv_id, error_message))
        return True  # this caller won the race

    with (
        patch.object(
            session_runner.db_adapter,
            "get_interrupted_investigations",
            return_value=[interrupted_inv],
        ),
        patch.object(
            session_runner.db_adapter,
            "mark_investigation_orphan_dead_lettered",
            side_effect=fake_dead_letter,
        ),
        patch.object(session_runner.db_adapter, "update_investigation_atomic"),
        patch.object(session_runner.db_adapter, "update_investigation"),
        patch.object(session_runner.db_adapter, "mark_investigation_recovering"),
        patch.object(session_runner.db_adapter, "delete_thread_session"),
        patch.object(
            session_runner, "send_notification", side_effect=fake_send_notification
        ),
        patch.object(
            session_runner,
            "_thread_permalink_for_admin_dm",
            return_value="<https://slack.com/x|thread>",
        ),
        patch.object(session_runner, "run_adhoc_mcp_session") as mock_run_fresh,
        patch.object(session_runner.client.beta.sessions, "retrieve"),
    ):
        session_runner.recover_interrupted_investigations()

    # (a) admin DM fired with admin_only=True
    assert len(admin_dm_calls) == 1, (
        f"expected exactly one admin DM; got {len(admin_dm_calls)}"
    )
    admin_call = admin_dm_calls[0]
    # send_notification("watch", summary, detail=..., admin_only=True)
    severity, summary = admin_call["args"][0], admin_call["args"][1]
    detail = admin_call["kwargs"].get("detail", "")
    assert severity == "watch"
    # Summary mentions the dead-letter status and the inv id.
    assert "dead-lettered" in summary, summary
    assert "#77" in summary, summary
    # session_id and full context surfaced in detail
    assert "sesn_EXAMPLE" in detail
    assert "Why did win-rate drop?" in detail
    assert "timeout after 60m" in detail
    assert "acme" in detail

    # (b) row marked orphan_dead_lettered via the targeted helper
    assert len(dead_letter_calls) == 1, (
        f"expected one orphan_dead_lettered transition; got {dead_letter_calls}"
    )
    assert dead_letter_calls[0][0] == 77

    # (c) user-thread message posted with admin-notified language
    assert len(user_thread_calls) == 1
    user_msg = user_thread_calls[0]
    assert user_msg["kwargs"].get("reply_to") == "1234567890.000300"
    user_summary = user_msg["args"][1]
    assert "admin has been notified" in user_summary

    # (d) NO 3rd silent retry
    mock_run_fresh.assert_not_called()


def test_recover_max_attempts_dead_letter_before_terminalize():
    """Regression for the [P0] caught in PR #198 review (2026-05-14): the
    recovery loop must call ``_dead_letter_orphan_investigation`` BEFORE
    ``terminalize_lifecycle``. Reverse order means the row lands in
    'failed' first (via the lifecycle reconciliation path because the
    atomic UPDATE excludes 'interrupted'), and then the dead-letter
    UPDATE matches zero rows because 'failed' is excluded from its
    allow-list — silently dropping the operator handoff.
    """
    import session_runner

    interrupted_inv = {
        "id": 91,
        "question": "ordering matters",
        "thread_ts": "1234567890.000999",
        "channel_id": "C01ORDER",
        "user_id": "U01ORDER",
        "session_id": "sesn_EXAMPLE",
        "portco_key": "acme",
        "recovery_count": 2,
        "error_message": "prior crash",
        "event_ts": "1234567890.000888",
        "started_at": None,
    }

    call_order: list[str] = []

    def stub_dead_letter(inv):
        call_order.append("dead_letter")

    def stub_terminalize(*args, **kwargs):
        call_order.append("terminalize")

    with (
        patch.object(
            session_runner.db_adapter,
            "get_interrupted_investigations",
            return_value=[interrupted_inv],
        ),
        patch.object(session_runner.db_adapter, "update_investigation"),
        patch.object(session_runner.db_adapter, "mark_investigation_recovering"),
        patch.object(session_runner.db_adapter, "delete_thread_session"),
        patch.object(
            session_runner,
            "_dead_letter_orphan_investigation",
            side_effect=stub_dead_letter,
        ),
        patch.object(session_runner, "send_notification", return_value="ts"),
        patch.object(session_runner.client.beta.sessions, "retrieve"),
        patch.dict(
            sys.modules,
            {"lifecycle": MagicMock(terminalize_lifecycle=stub_terminalize)},
        ),
    ):
        session_runner.recover_interrupted_investigations()

    assert call_order == ["dead_letter", "terminalize"], (
        f"dead-letter must run BEFORE terminalize_lifecycle; got {call_order}"
    )


def test_orphan_dead_lettered_status_transition():
    """_dead_letter_orphan_investigation calls
    mark_investigation_orphan_dead_lettered (NOT the generic
    update_investigation_atomic, which forbids status='interrupted'), then
    dispatches admin DM. Idempotent: when the dead-letter UPDATE returns
    False (some other path already terminalized), skip the admin DM to
    avoid double-pinging.
    """
    import session_runner

    inv = {
        "id": 88,
        "question": "What's the lead-to-opp conversion?",
        "thread_ts": "1234567890.000500",
        "channel_id": "C01TEST",
        "session_id": "sesn_EXAMPLE",
        "portco_key": "acme",
        "recovery_count": 2,
        "error_message": "anthropic 500",
    }

    # Path 1: dead-letter UPDATE wins → DM fires.
    with (
        patch.object(
            session_runner.db_adapter,
            "mark_investigation_orphan_dead_lettered",
            return_value=True,
        ) as mock_dl,
        patch.object(session_runner, "send_notification") as mock_notify,
        patch.object(
            session_runner, "_thread_permalink_for_admin_dm", return_value="(perm)"
        ),
    ):
        session_runner._dead_letter_orphan_investigation(inv)

    # Called with inv_id positional + error_message kwarg; no status arg
    # (the helper encodes the status itself).
    mock_dl.assert_called_once()
    assert mock_dl.call_args.args[0] == 88
    err = mock_dl.call_args.kwargs.get("error_message") or ""
    assert "max_recovery_attempts_exceeded" in err
    assert "Task #23" in err

    # Admin DM fired with admin_only=True.
    notify_calls = [
        c for c in mock_notify.call_args_list if c.kwargs.get("admin_only") is True
    ]
    assert len(notify_calls) == 1
    # Detail block lists everything the operator needs.
    detail = notify_calls[0].kwargs.get("detail", "")
    assert "sesn_EXAMPLE" in detail
    assert "acme" in detail
    assert "What's the lead-to-opp conversion?" in detail
    assert "anthropic 500" in detail

    # Path 2: dead-letter UPDATE lost the race (some other terminal path
    # won) → we skip the DM. No double-ping.
    with (
        patch.object(
            session_runner.db_adapter,
            "mark_investigation_orphan_dead_lettered",
            return_value=False,
        ),
        patch.object(session_runner, "send_notification") as mock_notify_2,
        patch.object(
            session_runner, "_thread_permalink_for_admin_dm", return_value="(perm)"
        ),
    ):
        session_runner._dead_letter_orphan_investigation(inv)

    # No admin DM on the second call.
    admin_dms = [
        c for c in mock_notify_2.call_args_list if c.kwargs.get("admin_only") is True
    ]
    assert admin_dms == []


def test_mark_investigation_orphan_dead_lettered_accepts_interrupted():
    """Regression for the [P0] caught in PR #198 review (2026-05-14): the
    DB helper MUST allow status='interrupted' as a precondition because
    ``recover_interrupted_investigations`` flips rows ``running →
    interrupted`` BEFORE the dead-letter branch runs. The generic
    ``update_investigation_atomic`` excludes 'interrupted' from its
    WHERE NOT IN list, which is why a dedicated helper exists.
    """
    import db_adapter as db

    captured_sql = []
    captured_params = []

    fake_cur = MagicMock()

    def fake_execute(sql, params=None):
        captured_sql.append(sql)
        captured_params.append(params)

    fake_cur.execute.side_effect = fake_execute
    fake_cur.rowcount = 1  # row was in 'interrupted' and got flipped
    fake_conn = MagicMock()
    fake_conn.cursor.return_value.__enter__.return_value = fake_cur

    with (
        patch.object(db, "_connect", return_value=fake_conn),
        patch.object(db, "DATABASE_URL", "postgres://test"),
    ):
        won = db.mark_investigation_orphan_dead_lettered(
            inv_id=42, error_message="task #23 dead-letter"
        )

    assert won is True
    # The WHERE clause must accept queued/running/interrupted — NOT use a
    # NOT-IN clause that would exclude 'interrupted'.
    sql = captured_sql[0]
    assert "status IN ('queued','running','interrupted')" in sql, sql
    assert "orphan_dead_lettered" in sql
    fake_conn.commit.assert_called()


def test_mark_investigation_orphan_dead_lettered_no_op_on_terminal():
    """Already-terminal rows return False (the caller skips the admin DM)."""
    import db_adapter as db

    fake_cur = MagicMock()
    fake_cur.rowcount = 0  # WHERE status IN (...) matched nothing
    fake_conn = MagicMock()
    fake_conn.cursor.return_value.__enter__.return_value = fake_cur

    with (
        patch.object(db, "_connect", return_value=fake_conn),
        patch.object(db, "DATABASE_URL", "postgres://test"),
    ):
        won = db.mark_investigation_orphan_dead_lettered(inv_id=99)

    assert won is False


def test_cleanup_known_orphans_marks_specific_session():
    """cleanup_known_orphans marks sesn_EXAMPLE
    'orphan_dead_lettered' if it's still in 'running' state.

    No admin DM for known orphans (the operator already named them in
    the carry-over snapshot).
    """
    import db_adapter as db

    # The known session id is fixed in the module — assert it's in the list.
    assert "sesn_EXAMPLE" in db.KNOWN_ORPHAN_SESSION_IDS

    # Stub a cursor that returns one row id for the known session and
    # zero rows for any other session id we test with.
    captured_sql = []
    captured_params = []

    fake_cur = MagicMock()

    def fake_execute(sql, params=None):
        captured_sql.append(sql)
        captured_params.append(params)

    def fake_fetchall():
        # Return one fake row for the most recent execute call. Caller
        # iterates this and pulls row[0].
        last_params = captured_params[-1] if captured_params else None
        if last_params and last_params[1] == "sesn_EXAMPLE":
            return [(4242,)]
        return []

    fake_cur.execute.side_effect = fake_execute
    fake_cur.fetchall.side_effect = fake_fetchall
    fake_conn = MagicMock()
    fake_conn.cursor.return_value.__enter__.return_value = fake_cur

    with (
        patch.object(db, "_connect", return_value=fake_conn),
        patch.object(db, "DATABASE_URL", "postgres://test"),
    ):
        transitioned = db.cleanup_known_orphans(known_session_ids=("sesn_EXAMPLE",))

    # The known orphan was transitioned.
    assert transitioned == [4242]

    # The UPDATE flips status to orphan_dead_lettered ONLY when status='running'.
    assert any("orphan_dead_lettered" in s for s in captured_sql)
    assert any("status = 'running'" in s for s in captured_sql)
    # Commit fired.
    fake_conn.commit.assert_called()


def test_cleanup_known_orphans_no_op_when_session_not_running():
    """If the known orphan is already terminal, the UPDATE matches 0 rows
    and cleanup_known_orphans returns an empty list. Idempotent on rerun.
    """
    import db_adapter as db

    fake_cur = MagicMock()
    fake_cur.fetchall.return_value = []  # WHERE status='running' matched nothing
    fake_conn = MagicMock()
    fake_conn.cursor.return_value.__enter__.return_value = fake_cur

    with (
        patch.object(db, "_connect", return_value=fake_conn),
        patch.object(db, "DATABASE_URL", "postgres://test"),
    ):
        transitioned = db.cleanup_known_orphans(known_session_ids=("sesn_EXAMPLE",))

    assert transitioned == []


def test_cleanup_known_orphans_no_db_url():
    """Without DATABASE_URL the cleanup returns [] without touching anything.
    Required for local dev / pytest without postgres.
    """
    import db_adapter as db

    with patch.object(db, "DATABASE_URL", ""):
        result = db.cleanup_known_orphans()

    assert result == []


# ---------------------------------------------------------------------------
# PR 3 (floating-prancing-trinket plan) — tool capability map + dispatch guard
# ---------------------------------------------------------------------------


def test_tool_capability_map_kapa_membership():
    """TOOL_CAPABILITY_MAP encodes the documented Kapa access table.

    Source of truth: CLAUDE.md "Which agents have Kapa access" table.
    Coordinator, Quick Answer, Dream Agent, Post-Sales Monitor, and
    Cross-Domain Synthesizer get Kapa; everyone else does not.
    """
    from session_runner import TOOL_CAPABILITY_MAP

    kapa = "search_knowledge_base"
    assert kapa in TOOL_CAPABILITY_MAP["COORDINATOR_ID"]
    assert kapa in TOOL_CAPABILITY_MAP["QUICK_ANSWER_ID"]
    assert kapa in TOOL_CAPABILITY_MAP["DREAM_AGENT_ID"]
    assert kapa in TOOL_CAPABILITY_MAP["POSTSALES_MONITOR_ID"]
    assert kapa in TOOL_CAPABILITY_MAP["CROSS_DOMAIN_SYNTHESIZER_ID"]
    # The five agents listed as "no Kapa" in CLAUDE.md must not carry it.
    assert kapa not in TOOL_CAPABILITY_MAP["PIPELINE_MONITOR_ID"]
    assert kapa not in TOOL_CAPABILITY_MAP["SALES_MONITOR_ID"]
    assert kapa not in TOOL_CAPABILITY_MAP["STATISTICIAN_ID"]
    assert kapa not in TOOL_CAPABILITY_MAP["ADVERSARIAL_REVIEWER_ID"]
    assert kapa not in TOOL_CAPABILITY_MAP["CHART_DESIGNER_ID"]


def test_tool_capability_map_dump_sf_query_membership():
    """Only the data-side agents carry dump_sf_query — reasoning agents don't.

    The three Monitors + Statistician + Quick Answer materialize SF reads;
    Adversarial Reviewer, Cross-Domain Synthesizer, and Chart Designer
    consume findings, never raw rows.
    """
    from session_runner import TOOL_CAPABILITY_MAP

    tool = "dump_sf_query"
    assert tool in TOOL_CAPABILITY_MAP["PIPELINE_MONITOR_ID"]
    assert tool in TOOL_CAPABILITY_MAP["SALES_MONITOR_ID"]
    assert tool in TOOL_CAPABILITY_MAP["POSTSALES_MONITOR_ID"]
    assert tool in TOOL_CAPABILITY_MAP["STATISTICIAN_ID"]
    assert tool in TOOL_CAPABILITY_MAP["QUICK_ANSWER_ID"]
    assert tool not in TOOL_CAPABILITY_MAP["ADVERSARIAL_REVIEWER_ID"]
    assert tool not in TOOL_CAPABILITY_MAP["CROSS_DOMAIN_SYNTHESIZER_ID"]
    assert tool not in TOOL_CAPABILITY_MAP["CHART_DESIGNER_ID"]
    assert tool not in TOOL_CAPABILITY_MAP["DREAM_AGENT_ID"]


def test_check_dispatch_capability_kapa_to_pipeline_monitor_flags_error():
    """Pipeline Monitor lacks Kapa — a Kapa-shaped dispatch returns a structured error.

    The failure mode from the plan: Coordinator dispatched a Kapa-shaped
    sub-task to Pipeline Monitor; sub-agent could not call the tool and
    cited stale memory instead of erroring. This check intercepts before
    the silent failure happens.
    """
    from session_runner import check_dispatch_capability

    body = (
        "Pipeline Monitor — please search_knowledge_base "
        "for recent Commerce GTM release notes and summarize."
    )
    err = check_dispatch_capability("Pipeline Monitor", body)
    assert err is not None, "expected a tool_capability_mismatch error"
    assert err["error"] == "tool_capability_mismatch"
    assert err["destination_agent"] == "PIPELINE_MONITOR_ID"
    assert err["missing_tool"] == "search_knowledge_base"
    # Machine-parseable message includes the destination, missing tool,
    # and at least one Kapa-capable agent for redispatch.
    assert "PIPELINE_MONITOR_ID" in err["message"]
    assert "search_knowledge_base" in err["message"]
    # The redispatch list must include the five Kapa-enabled agents
    # documented in CLAUDE.md so the Coordinator knows where to re-route.
    redispatch = set(err["redispatch_to"])
    assert "COORDINATOR_ID" in redispatch
    assert "POSTSALES_MONITOR_ID" in redispatch
    assert "CROSS_DOMAIN_SYNTHESIZER_ID" in redispatch
    assert "QUICK_ANSWER_ID" in redispatch
    assert "DREAM_AGENT_ID" in redispatch


def test_check_dispatch_capability_kapa_to_postsales_passes():
    """Post-Sales Monitor HAS Kapa — same dispatch body returns None (no error)."""
    from session_runner import check_dispatch_capability

    body = (
        "Post-Sales Monitor — please search_knowledge_base "
        "for recent Commerce changes affecting tier-1 customers."
    )
    assert check_dispatch_capability("Post-Sales Monitor", body) is None


def test_check_dispatch_capability_unknown_agent_returns_none():
    """Unknown agent name skips the check rather than failing loud.

    Defensive: adding a new agent shouldn't break routing on the day
    of provisioning. The next deploy fills in the map.
    """
    from session_runner import check_dispatch_capability

    body = "search_knowledge_base for X"
    assert check_dispatch_capability("Brand New Agent", body) is None


def test_check_dispatch_capability_dump_sf_query_to_reasoning_agent_flags():
    """Adversarial Reviewer lacks dump_sf_query — flag it.

    Iter3 reasoning agents don't touch raw SF; they consume findings.
    A dispatch instructing them to dump_sf_query is a routing bug.
    """
    from session_runner import check_dispatch_capability

    body = "Adversarial Reviewer — run dump_sf_query against Lead and check."
    err = check_dispatch_capability("Adversarial Reviewer", body)
    assert err is not None
    assert err["missing_tool"] == "dump_sf_query"
    assert err["destination_agent"] == "ADVERSARIAL_REVIEWER_ID"


def test_check_dispatch_capability_no_tool_hint_returns_none():
    """Generic prose with no tool name returns None (the common path)."""
    from session_runner import check_dispatch_capability

    body = "Pipeline Monitor — summarize this week's lead volume by source."
    assert check_dispatch_capability("Pipeline Monitor", body) is None


def test_check_dispatch_capability_empty_body_returns_none():
    """An empty or None dispatch body returns None — no false positives."""
    from session_runner import check_dispatch_capability

    assert check_dispatch_capability("Pipeline Monitor", "") is None
    assert check_dispatch_capability("Pipeline Monitor", None) is None


def test_dispatch_guard_injects_error_when_kapa_dispatched_to_pipeline_monitor():
    """End-to-end: thread_message_sent event with Kapa hint to Pipeline Monitor
    triggers a structured error injection back into the parent session.

    Mocks the SDK stream so we can assert the orchestrator called
    ``client.beta.sessions.events.send`` with a ``user.message`` carrying
    the tool_capability_mismatch error.
    """
    import session_runner

    # Build a synthetic thread_message_sent event whose ``to_agent_name``
    # is Pipeline Monitor and whose ``content`` references Kapa.
    fake_event = MagicMock()
    fake_event.type = "agent.thread_message_sent"
    fake_event.id = "ev_dispatch_1"
    fake_event.to_agent_name = "Pipeline Monitor"
    fake_event.agent_name = "Pipeline Monitor"
    text_block = MagicMock()
    text_block.text = (
        "Pipeline Monitor: please call search_knowledge_base "
        "for the latest Commerce GTM release notes."
    )
    fake_event.content = [text_block]

    # Terminator event so the stream loop exits cleanly.
    idle_event = MagicMock()
    idle_event.type = "session.status_idle"
    idle_event.id = "ev_idle_1"
    stop_reason = MagicMock()
    stop_reason.type = "end_turn"
    idle_event.stop_reason = stop_reason

    class _FakeStream:
        def __enter__(self_inner):
            return iter([fake_event, idle_event])

        def __exit__(self_inner, *a):
            return False

    sent_events: list = []

    def _record_send(*, session_id, events):
        sent_events.append((session_id, events))

    fake_client = MagicMock()
    fake_client.beta.sessions.events.stream.return_value = _FakeStream()
    fake_client.beta.sessions.events.send.side_effect = _record_send

    with patch.object(session_runner, "client", fake_client):
        # _buffer_thread_event uses db_adapter; stub the insert path so
        # the test doesn't need a live DB.
        with patch.object(
            session_runner.db_adapter, "insert_session_thread_events", return_value=0
        ):
            session_runner._stream_and_handle(
                session_id="sess_dispatch_test",
                thread_ts=None,
                verbosity="summary",
                portco_key="acme",
            )

    # The orchestrator should have injected exactly one user.message back
    # to the session carrying the tool_capability_mismatch error.
    injected = [
        (sid, evs)
        for (sid, evs) in sent_events
        if any(isinstance(e, dict) and e.get("type") == "user.message" for e in evs)
    ]
    assert injected, (
        f"expected a user.message error injection; got sends: {sent_events!r}"
    )
    _, evs = injected[0]
    msg_event = next(e for e in evs if e.get("type") == "user.message")
    msg_text = msg_event["content"][0]["text"]
    assert "tool_capability_mismatch" in msg_text
    assert "PIPELINE_MONITOR_ID" in msg_text
    assert "search_knowledge_base" in msg_text


def test_dispatch_guard_silent_when_destination_has_tool():
    """Same Kapa-shaped dispatch to Post-Sales Monitor: no error injection."""
    import session_runner

    fake_event = MagicMock()
    fake_event.type = "agent.thread_message_sent"
    fake_event.id = "ev_dispatch_2"
    fake_event.to_agent_name = "Post-Sales Monitor"
    fake_event.agent_name = "Post-Sales Monitor"
    text_block = MagicMock()
    text_block.text = (
        "Post-Sales Monitor: please call search_knowledge_base "
        "for Commerce changes affecting tier-1 customers."
    )
    fake_event.content = [text_block]

    idle_event = MagicMock()
    idle_event.type = "session.status_idle"
    idle_event.id = "ev_idle_2"
    stop_reason = MagicMock()
    stop_reason.type = "end_turn"
    idle_event.stop_reason = stop_reason

    class _FakeStream:
        def __enter__(self_inner):
            return iter([fake_event, idle_event])

        def __exit__(self_inner, *a):
            return False

    sent_events: list = []
    fake_client = MagicMock()
    fake_client.beta.sessions.events.stream.return_value = _FakeStream()
    fake_client.beta.sessions.events.send.side_effect = lambda *, session_id, events: (
        sent_events.append((session_id, events))
    )

    with patch.object(session_runner, "client", fake_client):
        with patch.object(
            session_runner.db_adapter, "insert_session_thread_events", return_value=0
        ):
            session_runner._stream_and_handle(
                session_id="sess_dispatch_ok",
                thread_ts=None,
                verbosity="summary",
                portco_key="acme",
            )

    # No user.message injections — Post-Sales Monitor has Kapa, dispatch is fine.
    injected = [
        (sid, evs)
        for (sid, evs) in sent_events
        if any(isinstance(e, dict) and e.get("type") == "user.message" for e in evs)
    ]
    assert not injected, f"expected NO error injection; got sends: {sent_events!r}"


# PR 10 — Duplicate-retry blocker (serialize failed tool calls)
# ──────────────────────────────────────────────────────────────────────────


def _clear_duplicate_retry_state():
    """Drop the module-level failure log so tests do not bleed into each other."""
    import session_runner

    with session_runner._recent_failed_tool_calls_lock:
        session_runner._RECENT_FAILED_TOOL_CALLS.clear()


def test_duplicate_failed_call_within_window_is_blocked():
    """Two identical failed db_query calls inside 5s: the second is blocked."""
    import session_runner

    _clear_duplicate_retry_state()

    bad_input = {"sql": "DROP TABLE users"}  # rejected by the read-only guard

    first = session_runner._dispatch_tool(
        "db_query", bad_input, session_id="sess-dup-1"
    )
    first_parsed = json.loads(first)
    # Sanity: the first call really did error so it registers a failure
    # (the read-only guard surfaces an "error" key).
    assert "error" in first_parsed, first

    second = session_runner._dispatch_tool(
        "db_query", bad_input, session_id="sess-dup-1"
    )
    second_parsed = json.loads(second)
    assert second_parsed.get("error") == "duplicate_retry_too_fast", second
    assert second_parsed.get("tool") == "db_query"
    assert "Wait or fix root cause" in second_parsed.get("message", "")


def test_duplicate_failed_call_after_window_is_not_blocked(monkeypatch):
    """Same failed call after 5s passes through to the dispatcher again.

    Override time.time so the test does not actually sleep 5s — the guard
    reads ``time.time()`` on every call to compute the age.
    """
    import session_runner

    _clear_duplicate_retry_state()

    bad_input = {"sql": "DROP TABLE users"}
    clock = {"now": 1_000_000.0}

    def fake_time():
        return clock["now"]

    monkeypatch.setattr(session_runner.time, "time", fake_time)

    first = session_runner._dispatch_tool(
        "db_query", bad_input, session_id="sess-dup-window"
    )
    assert "error" in json.loads(first)

    # Advance past the 5s window AND past the 10s TTL so the entry is also
    # garbage-collected — second call must reach the real dispatcher.
    clock["now"] += session_runner.DUPLICATE_RETRY_WINDOW_SECONDS + 6.0
    second = session_runner._dispatch_tool(
        "db_query", bad_input, session_id="sess-dup-window"
    )
    second_parsed = json.loads(second)
    # Not blocked → it returns whatever the real dispatcher returned (in
    # this case the same read-only guard error). The blocker's payload
    # has the sentinel ``error == "duplicate_retry_too_fast"``; the real
    # error does not, so a simple inequality check is enough.
    assert second_parsed.get("error") != "duplicate_retry_too_fast", second


def test_different_input_does_not_block():
    """A failed call followed by a DIFFERENT input passes through.

    Legitimate retries (fixed SOQL, narrowed range) must not be blocked.
    """
    import session_runner

    _clear_duplicate_retry_state()

    first = session_runner._dispatch_tool(
        "db_query",
        {"sql": "DROP TABLE users"},
        session_id="sess-different-input",
    )
    assert "error" in json.loads(first)

    second = session_runner._dispatch_tool(
        "db_query",
        {"sql": "DROP TABLE accounts"},  # different sql → different hash
        session_id="sess-different-input",
    )
    second_parsed = json.loads(second)
    assert second_parsed.get("error") != "duplicate_retry_too_fast", second


def test_successful_call_does_not_block_subsequent_identical_call():
    """A call that did NOT error must not register a failure.

    save_snapshot_batch returns ``{"ok": True, "saved": N}`` on success —
    use it with db_adapter.write_records patched out so it really
    succeeds. Then re-issue the same input; the dispatcher must not see
    a duplicate-retry block.
    """
    import session_runner

    _clear_duplicate_retry_state()

    payload = {
        "snapshot_id": "snap-1",
        "portco_key": "acme",
        "object_type": "Account",
        "records": [{"Id": "0011x000003ABCD"}],
    }

    with patch("session_runner.db_adapter.write_records") as mock_write:
        mock_write.return_value = None
        first = session_runner._dispatch_tool(
            "save_snapshot_batch", payload, session_id="sess-success"
        )
        first_parsed = json.loads(first)
        assert first_parsed.get("ok") is True, first

        second = session_runner._dispatch_tool(
            "save_snapshot_batch", payload, session_id="sess-success"
        )
        second_parsed = json.loads(second)
        assert second_parsed.get("error") != "duplicate_retry_too_fast", second
        assert second_parsed.get("ok") is True, second


def test_result_is_error_respects_ok_true_with_empty_error_field():
    """Regression — codex review of PR 10.

    Some custom-tool result payloads (e.g. ``review_rfp_draft`` via
    ``RFPReviewResult.to_dict()``; previously also ``write_prose`` via
    ``WritingAgentResult.to_dict()`` — that custom-tool path was retired
    2026-05-27 when the Writing Agent moved into the multiagent roster)
    ALWAYS carry an ``error`` field, defaulting to ``""`` on success.
    The duplicate-retry guard must NOT treat that as a failure or it
    will poison the cache and block legitimate retries that fire
    identical inputs seconds apart (e.g. a rejection loop on a
    structured-tool contract).
    """
    import session_runner

    success_with_empty_error = json.dumps(
        {
            "ok": True,
            "prose": "Hello.",
            "caveats": [],
            "decision_recommendation": "",
            "error": "",
            "duration_seconds": 0.1,
            "session_id": "sess-tool-1",
        }
    )
    assert session_runner._result_is_error(success_with_empty_error) is False

    # And the symmetric failure case still classifies correctly.
    failure_with_ok_false = json.dumps(
        {
            "ok": False,
            "prose": "",
            "error": "tool_timeout",
        }
    )
    assert session_runner._result_is_error(failure_with_ok_false) is True


def test_ttl_sweep_removes_stale_entries(monkeypatch):
    """Failure entries older than the TTL get garbage-collected on the
    next call. The dispatcher's failure log must not grow unbounded."""
    import session_runner

    _clear_duplicate_retry_state()

    clock = {"now": 2_000_000.0}
    monkeypatch.setattr(session_runner.time, "time", lambda: clock["now"])

    # Record a failure at t=0.
    session_runner._register_failed_tool_call(
        "sess-ttl", "db_query", {"sql": "SELECT 1"}
    )
    assert len(session_runner._RECENT_FAILED_TOOL_CALLS) == 1

    # Jump past the 10s TTL.
    clock["now"] += session_runner._DUPLICATE_RETRY_TTL_SECONDS + 1.0

    # Any dispatch entry triggers the sweep. Use the public check helper
    # so we exercise the same sweep path the dispatcher uses.
    result = session_runner._check_duplicate_retry(
        "sess-ttl", "other_tool", {"sql": "SELECT 2"}
    )
    assert result is None  # different key — not blocked
    assert len(session_runner._RECENT_FAILED_TOOL_CALLS) == 0, (
        "Stale entry should have been swept by _check_duplicate_retry"
    )


# PR 11 — reasoning_summary custom tool
#
# Every agent calls reasoning_summary(text=...) BEFORE its final response
# with a ≤200-token recap (what it did / found / surprised / unresolved).
# The orchestrator appends the recap to
# /system/session_reasoning_log.md in the health memory store. The
# dispatcher must:
#   1. Use the canonical path and format
#   2. Truncate text >1500 chars without raising
#   3. Swallow memory-store write failures (log + continue)
#   4. Return quickly with ok=True so the agent's tool-use loop never
#      stalls on observability infrastructure
# ──────────────────────────────────────────────────────────────────────────


def _install_reasoning_memory_stubs(
    monkeypatch,
    *,
    existing_content=None,
    list_raises=False,
    retrieve_raises=False,
    update_raises=False,
    create_raises=False,
):
    """Patch session_runner.client.beta.memory_stores.memories.* in-place.

    Returns a dict capturing every call's positional + keyword args so the
    test can assert path / content / store_id flowed through correctly.
    """
    import session_runner
    import types as _types

    calls = {
        "list": [],
        "retrieve": [],
        "update": [],
        "create": [],
    }

    class _Item:
        def __init__(self, id_, path, content):
            self.id = id_
            self.path = path
            self.content = content

    class _ListResult:
        def __init__(self, data):
            self.data = data

    state = {
        "item": (
            _Item("mem_1", session_runner.REASONING_LOG_PATH, existing_content)
            if existing_content is not None
            else None
        ),
    }

    def _list(store_id, path_prefix=None):
        calls["list"].append({"store_id": store_id, "path_prefix": path_prefix})
        if list_raises:
            raise RuntimeError("simulated list failure")
        return _ListResult([state["item"]] if state["item"] else [])

    def _retrieve(mem_id, memory_store_id=None):
        calls["retrieve"].append({"mem_id": mem_id, "memory_store_id": memory_store_id})
        if retrieve_raises:
            raise RuntimeError("simulated retrieve failure")
        return state["item"]

    def _update(mem_id, memory_store_id=None, content=None):
        calls["update"].append(
            {
                "mem_id": mem_id,
                "memory_store_id": memory_store_id,
                "content": content,
            }
        )
        if update_raises:
            raise RuntimeError("simulated update failure")
        if state["item"] is not None:
            state["item"].content = content
        return state["item"]

    def _create(store_id, path, content):
        calls["create"].append({"store_id": store_id, "path": path, "content": content})
        if create_raises:
            raise RuntimeError("simulated create failure")
        state["item"] = _Item("mem_new", path, content)
        return state["item"]

    fake_memories = _types.SimpleNamespace(
        list=_list,
        retrieve=_retrieve,
        update=_update,
        create=_create,
    )
    monkeypatch.setattr(
        session_runner.client.beta.memory_stores,
        "memories",
        fake_memories,
        raising=False,
    )
    # Ensure HEALTH_STORE_ID is set so the dispatcher doesn't bail early.
    monkeypatch.setattr(
        session_runner, "HEALTH_STORE_ID", "memstore_test", raising=False
    )

    # The dispatcher now submits the write to a background ThreadPoolExecutor.
    # Swap in a synchronous executor stub so tests can assert on the captured
    # `calls` dict immediately after the dispatch — same observation model
    # as before the fire-and-forget refactor, no Future plumbing required.
    class _SyncExecutor:
        def submit(self, fn, *args, **kwargs):
            fn(*args, **kwargs)
            return None

    monkeypatch.setattr(
        session_runner, "_reasoning_summary_executor", _SyncExecutor(), raising=False
    )
    return calls


def test_reasoning_summary_writes_to_canonical_path_on_first_call(monkeypatch):
    """When the rolling log does not exist, the dispatcher creates it
    at /system/session_reasoning_log.md with the block format
    ``## <session_id> @ <iso_ts>\\n<text>\\n\\n---\\n``."""
    import session_runner

    calls = _install_reasoning_memory_stubs(monkeypatch, existing_content=None)

    result_text = session_runner._dispatch_reasoning_summary(
        {"text": "did X, found Y, surprised by Z, couldn't resolve W"},
        session_id="sesn_EXAMPLE",
    )
    result = json.loads(result_text)
    assert result["ok"] is True
    assert (
        result["stored"] == "pending"
    )  # fire-and-forget; write runs on the (sync-stubbed) executor
    assert result["truncated"] is False

    # Created (not updated) because no prior memory file existed.
    assert len(calls["create"]) == 1
    create_call = calls["create"][0]
    assert create_call["path"] == "/system/session_reasoning_log.md"
    assert create_call["store_id"] == "memstore_test"
    # The block contains the session header and the recap text.
    assert "## sesn_EXAMPLE @ " in create_call["content"]
    assert (
        "did X, found Y, surprised by Z, couldn't resolve W" in create_call["content"]
    )
    assert "\n---\n" in create_call["content"]
    # Update path was NOT taken on first call.
    assert calls["update"] == []


def test_reasoning_summary_appends_to_existing_log(monkeypatch):
    """A second call appends a new block to the existing log content
    instead of overwriting."""
    import session_runner

    prior = (
        "# Session Reasoning Log\n\nPer-agent recaps.\n\n"
        "## sesn_EXAMPLE @ 2026-05-13T00:00:00+00:00\nprior recap\n\n---\n"
    )
    calls = _install_reasoning_memory_stubs(monkeypatch, existing_content=prior)

    result_text = session_runner._dispatch_reasoning_summary(
        {"text": "second recap"},
        session_id="sesn_EXAMPLE",
    )
    result = json.loads(result_text)
    assert result["ok"] is True
    assert (
        result["stored"] == "pending"
    )  # fire-and-forget; write runs on the (sync-stubbed) executor

    # Update was called (not create) because the file already existed.
    assert calls["create"] == []
    assert len(calls["update"]) == 1
    new_content = calls["update"][0]["content"]
    # Prior block is preserved verbatim.
    assert "## sesn_EXAMPLE @ 2026-05-13T00:00:00+00:00\nprior recap" in new_content
    # New block is appended at the end with the new session id + text.
    assert "## sesn_EXAMPLE @ " in new_content
    assert "second recap" in new_content
    assert new_content.endswith("---\n")


def test_reasoning_summary_truncates_text_over_1500_chars(monkeypatch):
    """A recap longer than REASONING_SUMMARY_MAX_CHARS is truncated to
    1500 chars; the result advertises ``truncated=True`` so the agent
    can adapt next turn but the call still succeeds."""
    import session_runner

    calls = _install_reasoning_memory_stubs(monkeypatch, existing_content=None)

    long_text = "x" * 5000
    result_text = session_runner._dispatch_reasoning_summary(
        {"text": long_text}, session_id="sesn_EXAMPLE"
    )
    result = json.loads(result_text)
    assert result["ok"] is True
    assert result["truncated"] is True

    # The stored body contains exactly REASONING_SUMMARY_MAX_CHARS x's,
    # NOT all 5000.
    stored_content = calls["create"][0]["content"]
    body_start = stored_content.find("\n", stored_content.index("## sesn_EXAMPLE")) + 1
    body_end = stored_content.find("\n\n---", body_start)
    body = stored_content[body_start:body_end]
    assert len(body) == session_runner.REASONING_SUMMARY_MAX_CHARS
    assert body == "x" * session_runner.REASONING_SUMMARY_MAX_CHARS


def test_reasoning_summary_swallows_memory_store_failures(monkeypatch):
    """If the memory-store write raises (network blip, missing store ID,
    transient API error), the dispatcher logs and returns ok=True. The
    agent's tool-use loop must never stall on observability infra.

    Covers the create branch (no prior log). See the ``update`` and
    ``retrieve`` companion tests below for the append branch.
    """
    import session_runner

    # Force every memory-store operation to raise so both the "first
    # write" path (create) and the "append" path (list+retrieve+update)
    # blow up cleanly.
    calls = _install_reasoning_memory_stubs(
        monkeypatch,
        existing_content=None,
        create_raises=True,
    )

    result_text = session_runner._dispatch_reasoning_summary(
        {"text": "should not raise even though memory store fails"},
        session_id="sesn_EXAMPLE",
    )
    # The tool call returns ok=True so the agent can move on, with
    # stored=False as the honest signal.
    result = json.loads(result_text)
    assert result["ok"] is True
    # Post-2026-05-15: dispatcher returns "pending" regardless of memory-store
    # outcome because the write is fire-and-forget on a ThreadPoolExecutor.
    # The failure path is observed via log warnings + the absence of an
    # update/create that completed cleanly (not via the return value).
    assert result["stored"] == "pending"
    # The dispatcher DID attempt to create (the API call was made).
    assert len(calls["create"]) == 1


def test_reasoning_summary_returns_immediately_without_waiting_for_write(monkeypatch):
    """Fire-and-forget contract: dispatcher returns in <50ms even when the
    memory-store write takes seconds. Today's incident wasn't caused by this
    dispatcher (Agent 4 measured ~575ms actual on prod), but every inline
    custom-tool dispatcher that does HTTP I/O holds the SSE iterator idle —
    we keep this one off-thread as hygiene against a slower variant.
    """
    import session_runner
    import time as _time
    import types as _types

    # Synchronous executor stub via the standard test helper would make the
    # write inline, defeating the latency test. Use a real ThreadPoolExecutor
    # here so the dispatch returns BEFORE the slow memory-store call runs.
    from concurrent.futures import ThreadPoolExecutor

    real_executor = ThreadPoolExecutor(max_workers=1)

    write_started = threading.Event()
    write_finished = threading.Event()

    def _slow_list(store_id, path_prefix=None):
        write_started.set()
        _time.sleep(0.5)  # 500ms — simulate prod-measured 575ms median
        write_finished.set()
        return _types.SimpleNamespace(data=[])

    def _slow_create(store_id, path, content):
        return _types.SimpleNamespace(id="mem_new", path=path, content=content)

    fake_memories = _types.SimpleNamespace(
        list=_slow_list,
        retrieve=lambda *a, **kw: None,
        update=lambda *a, **kw: None,
        create=_slow_create,
    )
    monkeypatch.setattr(
        session_runner.client.beta.memory_stores,
        "memories",
        fake_memories,
        raising=False,
    )
    monkeypatch.setattr(
        session_runner, "HEALTH_STORE_ID", "memstore_test", raising=False
    )
    monkeypatch.setattr(
        session_runner, "_reasoning_summary_executor", real_executor, raising=False
    )

    t0 = _time.perf_counter()
    result_text = session_runner._dispatch_reasoning_summary(
        {"text": "fire and forget"}, session_id="sesn_EXAMPLE_test"
    )
    elapsed_ms = (_time.perf_counter() - t0) * 1000.0

    result = json.loads(result_text)
    assert result["ok"] is True
    assert result["stored"] == "pending"
    # The dispatcher must return well before the 500ms slow write completes.
    # 50ms gives plenty of headroom for ThreadPoolExecutor.submit() overhead
    # while still being orders of magnitude under the slow write itself.
    assert elapsed_ms < 50.0, (
        f"dispatcher took {elapsed_ms:.1f}ms — must be <50ms (fire-and-forget) "
        "or the SSE iterator gets held idle waiting on a memory-store round-trip"
    )
    # Confirm the background write was actually queued + started.
    assert write_started.wait(timeout=2.0), "background write never started"
    # Drain the executor before the test exits so the slow write completes.
    assert write_finished.wait(timeout=2.0), "background write never finished"
    real_executor.shutdown(wait=True)


def test_reasoning_summary_swallows_append_branch_retrieve_failure(monkeypatch):
    """Append branch parity for the swallow-failure contract: when a
    prior log exists and ``retrieve`` raises, the dispatcher still
    returns ok=True with stored=False rather than surfacing the
    exception to the agent's tool-use loop.
    """
    import session_runner

    prior = (
        "# Session Reasoning Log\n\nPrior recaps.\n\n"
        "## sesn_EXAMPLE @ 2026-05-13T00:00:00+00:00\nprior recap\n\n---\n"
    )
    calls = _install_reasoning_memory_stubs(
        monkeypatch,
        existing_content=prior,
        retrieve_raises=True,
    )

    result_text = session_runner._dispatch_reasoning_summary(
        {"text": "append-branch retrieve failure"},
        session_id="sesn_EXAMPLE_fail",
    )
    result = json.loads(result_text)
    assert result["ok"] is True
    # Post-2026-05-15: dispatcher returns "pending" regardless of memory-store
    # outcome because the write is fire-and-forget on a ThreadPoolExecutor.
    # The failure path is observed via log warnings + the absence of an
    # update/create that completed cleanly (not via the return value).
    assert result["stored"] == "pending"
    # Retrieve was attempted; update was never reached.
    assert len(calls["retrieve"]) == 1
    assert calls["update"] == []


def test_reasoning_summary_swallows_append_branch_update_failure(monkeypatch):
    """If retrieve succeeds but update raises (network drop between the
    two API calls), the dispatcher still returns ok=True with
    stored=False. Confirms swallow coverage across both append-branch
    call sites.
    """
    import session_runner

    prior = (
        "# Session Reasoning Log\n\nPrior recaps.\n\n"
        "## sesn_EXAMPLE @ 2026-05-13T00:00:00+00:00\nprior recap\n\n---\n"
    )
    calls = _install_reasoning_memory_stubs(
        monkeypatch,
        existing_content=prior,
        update_raises=True,
    )

    result_text = session_runner._dispatch_reasoning_summary(
        {"text": "append-branch update failure"},
        session_id="sesn_EXAMPLE_fail",
    )
    result = json.loads(result_text)
    assert result["ok"] is True
    # Post-2026-05-15: dispatcher returns "pending" regardless of memory-store
    # outcome because the write is fire-and-forget on a ThreadPoolExecutor.
    # The failure path is observed via log warnings + the absence of an
    # update/create that completed cleanly (not via the return value).
    assert result["stored"] == "pending"
    assert len(calls["retrieve"]) == 1
    assert len(calls["update"]) == 1


def test_reasoning_summary_concurrent_writes_do_not_lose_blocks(monkeypatch):
    """Cross-session concurrency: when N reasoning_summary dispatches
    fire in parallel (5 simultaneous investigations is the production
    cap), every block must land in the final log content.

    Without a process-local write lock, two concurrent dispatches both
    retrieve the same prior content, both append their own block, and
    the slower writer's update clobbers the faster one → silent loss.
    The ``_reasoning_log_write_lock`` serializes the
    list+retrieve+update sequence to eliminate the in-process race.
    """
    import session_runner
    import threading as _threading

    calls = _install_reasoning_memory_stubs(monkeypatch, existing_content=None)

    n_threads = 8
    barrier = _threading.Barrier(n_threads)
    errors: list[Exception] = []

    def _fire(idx: int) -> None:
        try:
            # Force all threads to land in the dispatcher at the same
            # time so the race window is maximally exposed.
            barrier.wait(timeout=2.0)
            session_runner._dispatch_reasoning_summary(
                {"text": f"recap_{idx}"},
                session_id=f"sesn_EXAMPLE_{idx}",
            )
        except Exception as exc:  # pragma: no cover — surfaces real bugs
            errors.append(exc)

    threads = [_threading.Thread(target=_fire, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    assert not errors, f"thread errors: {errors!r}"

    # Reconstruct the final log content: one create + (n-1) updates,
    # last write wins for the visible content. The lock guarantees
    # serialization so each block appends to the prior result.
    if calls["create"]:
        final = calls["create"][0]["content"]
    else:  # pragma: no cover — defensive
        final = ""
    for upd in calls["update"]:
        final = upd["content"]

    for i in range(n_threads):
        assert f"recap_{i}" in final, (
            f"recap_{i} missing from final log — likely lost to a "
            f"concurrent write race. Final content tail:\n{final[-500:]}"
        )


def test_reasoning_summary_dispatch_via_dispatch_tool(monkeypatch):
    """``_dispatch_tool('reasoning_summary', ...)`` routes through to
    the dispatcher and never returns an "Unknown tool" error."""
    import session_runner

    calls = _install_reasoning_memory_stubs(monkeypatch, existing_content=None)

    result_text = session_runner._dispatch_tool(
        "reasoning_summary",
        {"text": "routed via dispatch_tool"},
        session_id="sesn_EXAMPLE",
    )
    result = json.loads(result_text)
    assert result.get("ok") is True
    assert "error" not in result
    assert len(calls["create"]) == 1
    assert "routed via dispatch_tool" in calls["create"][0]["content"]


def test_reasoning_summary_handles_missing_session_id(monkeypatch):
    """When session_id is None (cron-style caller), the dispatcher still
    writes the block using ``unknown_session`` as the header label —
    never crashes on a None header."""
    import session_runner

    calls = _install_reasoning_memory_stubs(monkeypatch, existing_content=None)

    result_text = session_runner._dispatch_reasoning_summary(
        {"text": "no session id"}, session_id=None
    )
    result = json.loads(result_text)
    assert result["ok"] is True
    assert "## unknown_session @ " in calls["create"][0]["content"]


# ─────────────────────────────────────────────────────────────────────────────
# sanitize_session_title — Anthropic Sessions API rejects titles with any
# Unicode Cc/Cf char with HTTP 400. Live repro 2026-05-21 15:49 PT: a Slack
# thread follow-up question starting with
# ``"Thread context (earlier messages):\n..."`` made the first 40 chars of
# the Prompt Engineer title contain a literal newline and Anthropic returned
# ``title: must not contain Unicode control or format characters``.
# ─────────────────────────────────────────────────────────────────────────────


def test_sanitize_session_title_strips_newline():
    """Newline (\\n, category Cc) is the live repro from 2026-05-21 — must
    be stripped so the Anthropic API does not reject the session title."""
    from session_runner import sanitize_session_title

    result = sanitize_session_title(
        "Thread context (earlier messages):\nfollow-up about pipeline"
    )
    assert "\n" not in result
    assert result.startswith("Thread context (earlier messages):")


def test_sanitize_session_title_strips_carriage_return_and_tab():
    """Any C0 control (\\r, \\t, \\x00-\\x1f) must be stripped."""
    from session_runner import sanitize_session_title

    assert "\r" not in sanitize_session_title("foo\rbar")
    assert "\t" not in sanitize_session_title("foo\tbar")
    assert "\x07" not in sanitize_session_title("foo\x07bar")


def test_sanitize_session_title_strips_bom_and_zero_width_chars():
    """U+FEFF (BOM), U+200B (ZWSP), U+200D (ZWJ), U+202E (RTL override) —
    all Cf-category chars that Anthropic rejects."""
    from session_runner import sanitize_session_title

    s = "hello﻿ world‍​‮"
    result = sanitize_session_title(s)
    assert "﻿" not in result
    assert "‍" not in result
    assert "​" not in result
    assert "‮" not in result
    assert result == "hello world"


def test_sanitize_session_title_preserves_legitimate_unicode():
    """Latin extended, em-dash, CJK — these are NOT Cc/Cf and must survive."""
    from session_runner import sanitize_session_title

    assert sanitize_session_title("Café — 北京") == "Café — 北京"


def test_sanitize_session_title_truncates_to_max_chars():
    """Cap at ``max_chars`` (default 60) to fit Anthropic's title limit."""
    from session_runner import sanitize_session_title

    long = "a" * 200
    assert sanitize_session_title(long) == "a" * 60
    assert sanitize_session_title(long, max_chars=30) == "a" * 30


def test_sanitize_session_title_falls_back_when_all_stripped():
    """Defensive: Anthropic also rejects empty titles. If every char was Cc/Cf,
    return a placeholder so the session create still succeeds."""
    from session_runner import sanitize_session_title

    assert sanitize_session_title("\n\r\t\x00‍﻿") == "(untitled)"
    assert sanitize_session_title("") == "(untitled)"
    assert sanitize_session_title("   ") == "(untitled)"


def test_sanitize_session_title_trims_outer_whitespace():
    """After stripping controls, trim leading/trailing whitespace so the
    title doesn't render as ' foo '."""
    from session_runner import sanitize_session_title

    assert sanitize_session_title("  foo  ") == "foo"
    # Newlines are stripped to nothing (category Cc), so the result is
    # bare ``"  foo  "`` before the trailing ``.strip()`` removes the
    # outer spaces.
    assert sanitize_session_title("\n  foo  ") == "foo"


def test_sanitize_session_title_caps_full_string_not_just_question():
    """Codex review (PR #258, P2): pass the full prefixed title to the
    sanitizer so the prefix counts toward the 60-char cap. If we sanitized
    only the question and prefixed afterward, an ``"Ad-hoc: "`` prefix
    (8 chars) plus the 60-char sanitized question would land a 68-char
    title at Anthropic. Pass the prefixed string so the cap really is 60."""
    from session_runner import sanitize_session_title

    long_q = "a" * 200
    result = sanitize_session_title(f"Ad-hoc: {long_q}")
    assert len(result) == 60
    assert result.startswith("Ad-hoc: ")


# ---------------------------------------------------------------------------
# Task 1 (F3): SOQL error hint must only attach to dump_sf_query failures.
# Issues #287, #290, #301, #313, #315, #326.
# ---------------------------------------------------------------------------


def test_db_query_exception_does_not_emit_soql_hint(monkeypatch):
    import session_runner

    monkeypatch.setattr(session_runner.db_adapter, "is_db_available", lambda: True)
    monkeypatch.setattr(session_runner.db_adapter, "get_schema_snapshot", lambda: {})

    def boom(sql):
        raise RuntimeError("connection reset by peer")

    monkeypatch.setattr(session_runner.db_adapter, "query", boom)
    out = json.loads(
        session_runner._dispatch_tool(
            "db_query", {"sql": "SELECT id FROM opportunities"}, session_id="s-hint-1"
        )
    )
    assert "error" in out
    assert "soql" not in json.dumps(out).lower()


def test_dump_sf_query_exception_keeps_soql_hint(monkeypatch):
    import session_runner
    import sf_dump_tool

    def boom(*a, **k):
        raise RuntimeError("Malformed request")

    monkeypatch.setattr(sf_dump_tool, "dump_sf_query", boom)
    out = json.loads(
        session_runner._dispatch_tool(
            "dump_sf_query",
            {"soql": "SELECT Id FROM Lead", "portco_key": "fishbowl", "label": "x"},
            session_id="s-hint-2",
        )
    )
    assert "error" in out
    assert "soql" in json.dumps(out).lower()


# ---------------------------------------------------------------------------
# Task 2 (F7): db_query surfaces classified DbQueryError, never bubbles raw.
# Issues #283, #284, #310, #311.
# ---------------------------------------------------------------------------


def test_db_query_surfaces_classified_error_kind(monkeypatch):
    import session_runner

    monkeypatch.setattr(session_runner.db_adapter, "is_db_available", lambda: True)
    monkeypatch.setattr(session_runner.db_adapter, "get_schema_snapshot", lambda: {})

    def boom(sql):
        raise session_runner.db_adapter.DbQueryError(
            "connection", "connection lost mid-query: server closed"
        )

    monkeypatch.setattr(session_runner.db_adapter, "query", boom)
    out = json.loads(
        session_runner._dispatch_tool(
            "db_query", {"sql": "SELECT id FROM opportunities"}, session_id="s-tax-1"
        )
    )
    assert out["ok"] is False
    assert out["error_kind"] == "connection"
    assert "soql" not in json.dumps(out).lower()


# ---------------------------------------------------------------------------
# Task 3 (F2): circuit-break repeated identical failures + latch SF auth.
# Issues #294, #295, #299, #319, #320, #324, #329.
# ---------------------------------------------------------------------------


def _reset_failure_state(sr):
    sr._RECENT_FAILED_TOOL_CALLS.clear()
    sr._TOOL_FAILURE_COUNTS.clear()
    sr._AUTH_FAILED_SESSIONS.clear()


def test_repeated_identical_failure_trips_circuit_breaker(monkeypatch):
    import session_runner as sr

    _reset_failure_state(sr)
    monkeypatch.setattr(sr.db_adapter, "is_db_available", lambda: True)
    monkeypatch.setattr(sr.db_adapter, "get_schema_snapshot", lambda: {})

    def flaky(sql):
        raise sr.db_adapter.DbQueryError("query", "column foo does not exist")

    monkeypatch.setattr(sr.db_adapter, "query", flaky)
    args = ("db_query", {"sql": "SELECT foo FROM opportunities"})
    for _ in range(3):
        sr._dispatch_tool(*args, session_id="s-cb")
    out = json.loads(sr._dispatch_tool(*args, session_id="s-cb"))
    assert out.get("error_kind") == "circuit_open"


def test_sf_auth_failure_latches_session(monkeypatch):
    import session_runner as sr
    import sf_dump_tool

    _reset_failure_state(sr)

    def auth_boom(*a, **k):
        raise RuntimeError("INVALID_SESSION_ID: Session expired or invalid")

    monkeypatch.setattr(sf_dump_tool, "dump_sf_query", auth_boom)
    sr._dispatch_tool(
        "dump_sf_query",
        {"soql": "SELECT Id FROM Lead", "portco_key": "fishbowl", "label": "x"},
        session_id="s-auth",
    )
    # A DIFFERENT SF query in the same session must be aborted pre-dispatch.
    out = json.loads(
        sr._dispatch_tool(
            "dump_sf_query",
            {
                "soql": "SELECT Name FROM Account",
                "portco_key": "fishbowl",
                "label": "y",
            },
            session_id="s-auth",
        )
    )
    assert out.get("error_kind") == "auth_aborted"
    assert "auth" in json.dumps(out).lower()


# ---------------------------------------------------------------------------
# Task 5 (F6a): generate_chart validates data.labels before rendering. #289.
# ---------------------------------------------------------------------------


def test_generate_chart_missing_labels_returns_clean_error():
    import session_runner as sr

    out = json.loads(
        sr._dispatch_tool(
            "generate_chart",
            {
                "chart_type": "bar",
                "title": "T",
                "data": {"datasets": [{"label": "x", "values": [1]}]},
            },
            session_id="s-chart",
        )
    )
    assert out.get("ok") is False
    assert "labels" in json.dumps(out).lower()
    assert "soql" not in json.dumps(out).lower()
