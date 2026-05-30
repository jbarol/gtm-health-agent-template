"""Tests for ``orchestrator.session_watch`` — the session-size canary.

Verifies the 750K/950K input-side token thresholds fire exactly once per
session per tier, that retrieval errors don't kill the loop, and that the
input-side math sums the three input categories the Managed Agents usage
object surfaces.

Motivation: today (2026-05-11) session ``sesn_EXAMPLE``
died silently at 1.12M tokens. The canary should have caught it at 750K
and given the user a chance to archive+replay before the 1M cap.

Run:
    cd orchestrator && python3 -m pytest session_watch_test.py -v
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch


# Defensive env stubbing — mirrors cost_collector_test / main_test so this
# suite runs cleanly on a fresh worktree without ``.env``.
for _k in (
    "ANTHROPIC_API_KEY",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "SLACK_CHANNEL_ID",
    "ENVIRONMENT_ID",
    "DREAM_AGENT_ID",
    "COORDINATOR_ID",
    "QUICK_AGENT_ID",
    "METHODOLOGY_STORE_ID",
    "HEALTH_STORE_ID",
):
    os.environ.setdefault(_k, "test-stub")

sys.modules.pop("config", None)
sys.modules.pop("session_watch", None)


# ──────────────────────────────────────────────────────────────────────────
# fakes — usage object + row factories
# ──────────────────────────────────────────────────────────────────────────


def _make_usage(input_tok: int = 0, cache_read: int = 0, cache_write_5m: int = 0):
    """Build a usage-shaped object matching the Managed Agents SDK.

    Mirrors session_runner._extract_usage_parts attribute access. We use
    SimpleNamespace via MagicMock(spec=None) so getattr() returns the
    configured values rather than auto-created Mock children.
    """
    cache_creation = MagicMock()
    cache_creation.ephemeral_5m_input_tokens = cache_write_5m
    cache_creation.ephemeral_1h_input_tokens = 0

    usage = MagicMock()
    usage.input_tokens = input_tok
    usage.output_tokens = 0
    usage.cache_read_input_tokens = cache_read
    usage.cache_creation = cache_creation
    return usage


def _make_session(input_tok: int = 0, cache_read: int = 0, cache_write_5m: int = 0):
    """Build a session-shaped object with ``.usage`` populated."""
    s = MagicMock()
    s.usage = _make_usage(input_tok, cache_read, cache_write_5m)
    return s


def _row(
    session_id: str = "sesn_EXAMPLE",
    portco_key: str = "acme",
    thread_ts: str = "1234567890.000100",
    channel_id: str = "C01TEST",
    age_minutes: float = 5.0,
):
    """Build an investigations-row dict matching the canary's expectations."""
    return {
        "id": 1,
        "session_id": session_id,
        "thread_ts": thread_ts,
        "channel_id": channel_id,
        "portco_key": portco_key,
        "started_at": datetime.now(timezone.utc) - timedelta(minutes=age_minutes),
    }


def _reset_state():
    """Clear module-level dedup state between tests."""
    import session_watch

    session_watch.alerted_session_ids.clear()


# ──────────────────────────────────────────────────────────────────────────
# _compute_input_side — token math
# ──────────────────────────────────────────────────────────────────────────


def test_compute_input_side_sums_three_categories():
    """input + cache_read + cache_write_5m. Output tokens excluded."""
    from session_watch import _compute_input_side

    usage = _make_usage(input_tok=100_000, cache_read=500_000, cache_write_5m=200_000)
    assert _compute_input_side(usage) == 800_000


def test_compute_input_side_handles_none():
    """No usage object → 0. Safe default when shape is unknown."""
    from session_watch import _compute_input_side

    assert _compute_input_side(None) == 0


def test_compute_input_side_handles_missing_cache_creation():
    """``usage.cache_creation = None`` should not crash. Returns input + cache_read."""
    from session_watch import _compute_input_side

    usage = MagicMock()
    usage.input_tokens = 100_000
    usage.cache_read_input_tokens = 200_000
    usage.cache_creation = None
    assert _compute_input_side(usage) == 300_000


def test_compute_input_side_handles_missing_5m_field():
    """``cache_creation`` exists but lacks the 5m field → fall back to 0."""
    from session_watch import _compute_input_side

    cc = MagicMock(spec=[])  # spec=[] means no attributes
    usage = MagicMock()
    usage.input_tokens = 50_000
    usage.cache_read_input_tokens = 50_000
    usage.cache_creation = cc
    assert _compute_input_side(usage) == 100_000


# ──────────────────────────────────────────────────────────────────────────
# check_active_sessions — threshold gates
# ──────────────────────────────────────────────────────────────────────────


def test_alert_fires_at_750k_threshold():
    """Session at 800K input-side → :warning: watch alert posted exactly once."""
    _reset_state()
    import session_watch

    row = _row(session_id="sesn_EXAMPLE")
    session = _make_session(
        input_tok=300_000, cache_read=400_000, cache_write_5m=100_000
    )

    with patch.object(session_watch, "_get_running_investigations", return_value=[row]):
        with patch.object(session_watch, "client") as mock_client:
            mock_client.beta.sessions.retrieve.return_value = session
            with patch.object(session_watch, "_send_alert") as mock_alert:
                with patch.object(
                    session_watch, "_thread_permalink", return_value="<link>"
                ):
                    result = session_watch.check_active_sessions()

    assert result["alerted_watch"] == 1
    assert result["alerted_imminent"] == 0
    mock_alert.assert_called_once()
    args, kwargs = mock_alert.call_args
    assert args[0] == "watch"
    summary = args[1]
    assert "sesn_EXAMPLE" in summary
    assert "750K" in summary
    assert "800,000" in summary  # comma-formatted token count
    assert "acme" in summary


def test_dedupe_prevents_second_alert_for_same_session():
    """Two consecutive ticks above 750K → only one alert. Dedup state holds."""
    _reset_state()
    import session_watch

    row = _row(session_id="sesn_EXAMPLE")
    session = _make_session(input_tok=800_000)

    with patch.object(session_watch, "_get_running_investigations", return_value=[row]):
        with patch.object(session_watch, "client") as mock_client:
            mock_client.beta.sessions.retrieve.return_value = session
            with patch.object(session_watch, "_send_alert") as mock_alert:
                with patch.object(
                    session_watch, "_thread_permalink", return_value="<link>"
                ):
                    session_watch.check_active_sessions()
                    session_watch.check_active_sessions()  # second tick
                    session_watch.check_active_sessions()  # third tick

    # Three ticks but only one Slack post — dedup set caught the 2nd and 3rd.
    assert mock_alert.call_count == 1
    assert ("sesn_EXAMPLE", "watch") in session_watch.alerted_session_ids


def test_retrieval_error_doesnt_kill_loop():
    """One session's retrieve fails → next session still gets checked."""
    _reset_state()
    import session_watch

    rows = [
        _row(session_id="sesn_EXAMPLEA"),
        _row(session_id="sesn_EXAMPLEB", thread_ts="1234567890.000200"),
    ]
    good_session = _make_session(input_tok=800_000)

    def fake_retrieve(session_id):
        if session_id == "sesn_EXAMPLEA":
            raise RuntimeError("404 session not found")
        return good_session

    with patch.object(session_watch, "_get_running_investigations", return_value=rows):
        with patch.object(session_watch, "client") as mock_client:
            mock_client.beta.sessions.retrieve.side_effect = fake_retrieve
            with patch.object(session_watch, "_send_alert") as mock_alert:
                with patch.object(
                    session_watch, "_thread_permalink", return_value="<link>"
                ):
                    result = session_watch.check_active_sessions()

    # Loop survived the broken session and alerted on the good one.
    assert result["checked"] == 2
    assert result["alerted_watch"] == 1
    mock_alert.assert_called_once()
    summary = mock_alert.call_args.args[1]
    assert "sesn_EXAMPLEB" in summary
    assert "sesn_EXAMPLEA" not in summary


# ──────────────────────────────────────────────────────────────────────────
# imminent (950K) escalation
# ──────────────────────────────────────────────────────────────────────────


def test_imminent_threshold_fires_at_950k():
    """Single tick at 970K → both :warning: AND :rotating_light: posted."""
    _reset_state()
    import session_watch

    row = _row(session_id="sesn_EXAMPLE")
    session = _make_session(input_tok=970_000)

    with patch.object(session_watch, "_get_running_investigations", return_value=[row]):
        with patch.object(session_watch, "client") as mock_client:
            mock_client.beta.sessions.retrieve.return_value = session
            with patch.object(session_watch, "_send_alert") as mock_alert:
                with patch.object(
                    session_watch, "_thread_permalink", return_value="<link>"
                ):
                    result = session_watch.check_active_sessions()

    # Both alerts fire on the same tick — watch first, then imminent.
    assert result["alerted_watch"] == 1
    assert result["alerted_imminent"] == 1
    assert mock_alert.call_count == 2
    severities = [call.args[0] for call in mock_alert.call_args_list]
    assert severities == ["watch", "critical"]
    imminent_summary = mock_alert.call_args_list[1].args[1]
    assert "950K" in imminent_summary
    assert "imminent termination" in imminent_summary
    assert "archive+replay" in imminent_summary


def test_below_threshold_no_alert():
    """Session at 600K → under the 750K floor → no Slack call."""
    _reset_state()
    import session_watch

    row = _row(session_id="sesn_EXAMPLE")
    session = _make_session(input_tok=600_000)

    with patch.object(session_watch, "_get_running_investigations", return_value=[row]):
        with patch.object(session_watch, "client") as mock_client:
            mock_client.beta.sessions.retrieve.return_value = session
            with patch.object(session_watch, "_send_alert") as mock_alert:
                result = session_watch.check_active_sessions()

    assert result["alerted_watch"] == 0
    assert result["alerted_imminent"] == 0
    mock_alert.assert_not_called()


def test_no_active_sessions_returns_zeros():
    """Empty investigations table → graceful no-op."""
    _reset_state()
    import session_watch

    with patch.object(session_watch, "_get_running_investigations", return_value=[]):
        result = session_watch.check_active_sessions()

    assert result == {"checked": 0, "alerted_watch": 0, "alerted_imminent": 0}


def test_already_alerted_session_skips_retrieve():
    """Once both thresholds fired, the canary short-circuits before retrieve."""
    _reset_state()
    import session_watch

    session_watch.alerted_session_ids.add(("sesn_EXAMPLE", "watch"))
    session_watch.alerted_session_ids.add(("sesn_EXAMPLE", "imminent"))

    row = _row(session_id="sesn_EXAMPLE")

    with patch.object(session_watch, "_get_running_investigations", return_value=[row]):
        with patch.object(session_watch, "client") as mock_client:
            with patch.object(session_watch, "_send_alert") as mock_alert:
                session_watch.check_active_sessions()

    # No retrieve, no alert — pure short-circuit.
    mock_client.beta.sessions.retrieve.assert_not_called()
    mock_alert.assert_not_called()


# ──────────────────────────────────────────────────────────────────────────
# _send_alert — log-only, NO Slack side effects (B6, 2026-05-12 self-heal)
# ──────────────────────────────────────────────────────────────────────────


def test_send_alert_does_not_post_to_slack(caplog):
    """``_send_alert`` must log only — never call ``send_notification``.

    Pre-B6 the canary posted WATCH/CRITICAL notices into the user's Slack
    thread, which polluted the channel with operational telemetry. Per the
    guiding principle ("Just fucking figure it out yourself and post it"),
    canary alerts now log and stay out of Slack.
    """
    import logging
    import session_watch

    # Patch the module-level slack_bot import path. The function does a
    # lazy ``from slack_bot import send_notification`` — verify it's not
    # called by patching the slack_bot module attribute directly.
    import sys

    fake_slack_bot = MagicMock()
    fake_slack_bot.send_notification = MagicMock()
    prev = sys.modules.get("slack_bot")
    sys.modules["slack_bot"] = fake_slack_bot
    try:
        with caplog.at_level(logging.WARNING, logger="session_watch"):
            session_watch._send_alert(
                "watch",
                "Session crossed 750K input-side tokens",
                channel_id="C01TEST",
                thread_ts="1234567890.000100",
            )
        # No Slack post.
        fake_slack_bot.send_notification.assert_not_called()
        # But the alert IS recorded in the log so the operator can audit.
        assert any(
            "session_watch alert" in rec.message
            and "Session crossed 750K input-side tokens" in rec.message
            for rec in caplog.records
        )
    finally:
        if prev is not None:
            sys.modules["slack_bot"] = prev
        else:
            sys.modules.pop("slack_bot", None)


def test_send_alert_log_without_thread_context(caplog):
    """No thread_ts / channel_id → still logs, no Slack call attempted."""
    import logging
    import session_watch

    with caplog.at_level(logging.WARNING, logger="session_watch"):
        session_watch._send_alert("critical", "Imminent termination at 970K")

    assert any(
        "session_watch alert" in rec.message
        and "Imminent termination at 970K" in rec.message
        for rec in caplog.records
    )
