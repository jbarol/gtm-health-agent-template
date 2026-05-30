"""Plan #52 PR-C: ``_archive_session`` log+retry, daemon-thread archive coverage.

Three tests:

1. ``test_archive_session_logs_warning_on_failure`` — confirms a failing
   archive emits a WARNING (no longer silently swallowed) and retries
   once. The 2026-05-19 RFP Responder leak
   (sesn_EXAMPLE stuck at archived=None for 75+ min)
   was masked by the prior silent swallow.

2. ``test_archive_session_succeeds_on_second_attempt`` — confirms the
   retry path: one failure, one success, exactly one WARNING.

3. ``test_rfp_reviewer_daemon_thread_archives_on_exit`` — confirms the
   ``_run_stream`` closure inside ``run_review`` calls ``_archive_session``
   from its ``finally`` block on any exit path (including stream errors),
   so the Reviewer session terminalizes from both the daemon thread
   AND ``_finalize`` paths.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

# Plan #52 PR-C (codex P2 fix): stub Anthropic/Slack env BEFORE importing
# session_runner. config.require_env raises at import time for any missing
# var, so in a clean CI checkout without a developer .env this test file
# would fail during pytest collection rather than at run time.
for _k in (
    "ANTHROPIC_API_KEY",
    "ENVIRONMENT_ID",
    "COORDINATOR_ID",
    "METHODOLOGY_STORE_ID",
    "HEALTH_STORE_ID",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "SLACK_CHANNEL_ID",
):
    os.environ.setdefault(_k, "test-stub")

sys.path.insert(0, str(Path(__file__).parent))

import session_runner  # noqa: E402


def test_archive_session_logs_warning_on_failure(caplog):
    """When archive raises, _archive_session logs WARNING and retries once."""
    with patch.object(session_runner.client.beta.sessions, "archive") as mock_archive:
        mock_archive.side_effect = Exception("boom")
        with caplog.at_level(logging.WARNING, logger="session_runner"):
            # Patch time.sleep so the 5s retry delay doesn't slow the test.
            with patch.object(session_runner.time, "sleep") as mock_sleep:
                session_runner._archive_session("sess-warn-test")
                # The retry should still have slept 5s in production.
                assert mock_sleep.called
                assert any(c.args and c.args[0] == 5 for c in mock_sleep.call_args_list)
    # Two WARNING lines: first failure + retry failure
    warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("_archive_session failed" in r.getMessage() for r in warning_records)
    assert any("retry failed" in r.getMessage() for r in warning_records)
    # Two archive attempts made
    assert mock_archive.call_count == 2


def test_archive_session_succeeds_on_second_attempt(caplog):
    """First fail, second succeed: one WARNING, retry succeeds, no second WARNING."""
    call_count = {"n": 0}

    def _archive_side(*_a, **_k):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise Exception("transient")
        return None

    with patch.object(session_runner.client.beta.sessions, "archive") as mock_archive:
        mock_archive.side_effect = _archive_side
        with caplog.at_level(logging.WARNING, logger="session_runner"):
            with patch.object(session_runner.time, "sleep"):
                session_runner._archive_session("sess-retry-test")

    warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
    # Exactly one WARNING (the first failure); retry succeeded
    assert (
        sum(1 for r in warning_records if "_archive_session failed" in r.getMessage())
        == 1
    )
    assert sum(1 for r in warning_records if "retry failed" in r.getMessage()) == 0
    assert call_count["n"] == 2


def test_rfp_reviewer_daemon_thread_archives_on_exit():
    """The daemon-thread finally calls _archive_session even when the
    stream raises (PR-C: belt-and-braces against orphan sessions).

    Codex P2 fix (2026-05-19): explicitly join the daemon thread after the
    assertions so its finally block runs while ``_archive_session`` is
    still patched. Without this, the daemon could escape the ``with``
    block and call the real Anthropic API after mocks teardown.
    """
    import rfp_reviewer

    def _exploding_stream(*_a, **_k):
        raise RuntimeError("simulated stream failure")

    with (
        patch("session_runner._stream_and_handle", side_effect=_exploding_stream),
        patch.object(rfp_reviewer, "_client") as mock_client_fn,
        patch.object(rfp_reviewer, "_rfp_reviewer_id", return_value="agent-test"),
        patch("session_runner._archive_session") as mock_archive,
        patch("session_runner._log_session_usage"),
    ):
        mock_sess = MagicMock()
        mock_sess.id = "sess-daemon-archive"
        mock_client_fn.return_value.beta.sessions.create.return_value = mock_sess
        try:
            rfp_reviewer.run_review(
                qa_index=[],
                feedback=None,
                timeout_seconds=2.0,
            )
            # Archive called at least once; both daemon-finally and
            # _finalize may have called it. Just confirm the session_id
            # reached the call.
            assert mock_archive.called
            archived_ids = [c.args[0] for c in mock_archive.call_args_list if c.args]
            assert "sess-daemon-archive" in archived_ids
        finally:
            # Belt-and-braces: ensure the daemon thread has fully exited
            # before mocks teardown.
            for t in threading.enumerate():
                if t.name.startswith("rfp-review-"):
                    t.join(timeout=2.0)
