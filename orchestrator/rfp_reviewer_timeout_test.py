"""Plan #52 PR-C: Reviewer timeout ceiling regression guard + finalize coverage.

Three tests:

1. ``test_default_timeout_is_at_least_1200s`` — assertion regression guard.
   Without this, a future PR that lowers the constant back to 600 would not
   trip any test, and the next live RFP run would re-hit the 601s wall-clock
   timeout observed on 2026-05-19 in session sesn_EXAMPLE.

2. ``test_run_review_returns_timeout_on_slow_stream`` — confirms the
   ``RFPReviewResult(ok=False, error='timeout')`` soft-PASS path fires
   when ``_stream_and_handle`` exceeds the timeout. Use a tiny per-test
   timeout to keep the test fast.

3. ``test_finalize_called_on_timeout`` — confirms the ``_finalize``
   path (which writes ``session_costs`` and archives the Anthropic
   session) still runs on the timeout branch.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

# Plan #52 PR-C (codex P2 fix): stub Anthropic/Slack env BEFORE importing
# rfp_reviewer. config.require_env raises at import time for any missing
# var, so in a clean CI checkout without a developer .env this test file
# would fail during pytest collection rather than at run time. setdefault
# leaves any real env values intact.
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

# Ensure orchestrator/ is on sys.path so bare imports below resolve.
sys.path.insert(0, str(Path(__file__).parent))

import rfp_reviewer  # noqa: E402


def test_default_timeout_is_at_least_1200s():
    """Regression guard — never let the ceiling drop back below 1200s."""
    assert rfp_reviewer.DEFAULT_TIMEOUT_SECONDS >= 1200.0


def _make_blocking_stream(stop_event: threading.Event):
    """Plan #52 PR-C (codex P2 fix): return a fake stream that blocks on a
    threading.Event instead of sleeping. The test signals the event AFTER
    assertions complete, which lets the daemon thread's finally block run
    while mocks are still active. Avoids the worker escaping the
    ``with patch(...)`` block and calling the real Anthropic API.
    """

    def _slow_stream(*_a, **_k):
        # Block until the test releases us. Cap at 30s as a safety net
        # against test-author mistakes; far longer than any healthy
        # timeout-test path.
        stop_event.wait(timeout=30.0)
        return ([], None, None, None)

    return _slow_stream


def test_run_review_returns_timeout_on_slow_stream():
    """When ``_stream_and_handle`` exceeds the per-call timeout,
    ``run_review`` returns ``RFPReviewResult(ok=False, error='timeout')``
    without raising."""
    stop = threading.Event()
    slow_stream = _make_blocking_stream(stop)
    with (
        patch("session_runner._stream_and_handle", side_effect=slow_stream),
        patch.object(rfp_reviewer, "_client") as mock_client_fn,
        patch.object(rfp_reviewer, "_rfp_reviewer_id", return_value="agent-test"),
        patch("session_runner._archive_session"),
        patch("session_runner._log_session_usage"),
    ):
        mock_sess = MagicMock()
        mock_sess.id = "sess-timeout-test"
        mock_client_fn.return_value.beta.sessions.create.return_value = mock_sess
        try:
            result = rfp_reviewer.run_review(
                qa_index=[],
                feedback=None,
                timeout_seconds=0.5,
            )
            assert result.ok is False
            assert result.error == "timeout"
            assert result.session_id == "sess-timeout-test"
        finally:
            # Release the daemon thread so its finally block runs with
            # _archive_session still mocked.
            stop.set()
            # Wait for the daemon thread to exit before letting the
            # patch context teardown.
            for t in threading.enumerate():
                if t.name.startswith("rfp-review-"):
                    t.join(timeout=2.0)


def test_finalize_called_on_timeout():
    """``_archive_session`` and ``_log_session_usage`` are invoked on
    the timeout branch via ``_finalize``."""
    stop = threading.Event()
    slow_stream = _make_blocking_stream(stop)
    with (
        patch("session_runner._stream_and_handle", side_effect=slow_stream),
        patch.object(rfp_reviewer, "_client") as mock_client_fn,
        patch.object(rfp_reviewer, "_rfp_reviewer_id", return_value="agent-test"),
        patch("session_runner._archive_session") as mock_archive,
        patch("session_runner._log_session_usage") as mock_log,
    ):
        mock_sess = MagicMock()
        mock_sess.id = "sess-finalize-test"
        mock_client_fn.return_value.beta.sessions.create.return_value = mock_sess
        try:
            rfp_reviewer.run_review(
                qa_index=[],
                feedback=None,
                timeout_seconds=0.5,
            )
            # _finalize calls _archive_session and _log_session_usage at
            # least once on the timeout branch.
            assert mock_archive.called
            assert any(
                c.args and c.args[0] == "sess-finalize-test"
                for c in mock_archive.call_args_list
            )
            assert mock_log.called
        finally:
            stop.set()
            for t in threading.enumerate():
                if t.name.startswith("rfp-review-"):
                    t.join(timeout=2.0)
