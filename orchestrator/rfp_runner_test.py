"""Unit tests for rfp_runner — public gating + dispatch path.

The heavy lifting (Slack download, Anthropic Files upload, session
stream) is mocked. These tests lock in the routing behavior so a
future refactor cannot silently re-enable the RFP path when env
vars are unset or accept an unsupported file type.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Same env stub the slack_bot tests use so config.py loads without
# Railway credentials in CI.
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")
os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test")
os.environ.setdefault("SLACK_CHANNEL_ID", "C-test")
os.environ.setdefault("ENVIRONMENT_ID", "env_test")
os.environ.setdefault("DREAM_AGENT_ID", "agent_test")
os.environ.setdefault("COORDINATOR_ID", "agent_test")
os.environ.setdefault("QUICK_AGENT_ID", "agent_test")
os.environ.setdefault("METHODOLOGY_STORE_ID", "mem_test")
os.environ.setdefault("HEALTH_STORE_ID", "mem_test")

sys.path.insert(0, os.path.dirname(__file__))

import rfp_runner  # type: ignore[import-not-found]  # noqa: E402


# ---------------------------------------------------------------------------
# is_rfp_channel — env-driven feature gate
# ---------------------------------------------------------------------------


def test_is_rfp_channel_false_when_env_unset(monkeypatch):
    monkeypatch.delenv("RFP_CHANNEL_ID", raising=False)
    assert rfp_runner.is_rfp_channel("CANY") is False
    assert rfp_runner.is_rfp_channel(None) is False
    assert rfp_runner.is_rfp_channel("") is False


def test_is_rfp_channel_matches_configured(monkeypatch):
    monkeypatch.setenv("RFP_CHANNEL_ID", "CRFP123")
    assert rfp_runner.is_rfp_channel("CRFP123") is True
    assert rfp_runner.is_rfp_channel("CSOMETHINGELSE") is False
    assert rfp_runner.is_rfp_channel(None) is False


# ---------------------------------------------------------------------------
# _extract_extension — lowercased, no leading dot, empty when absent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("foo.xlsx", "xlsx"),
        ("foo.XLSX", "xlsx"),
        ("RFP - Q3 2026.docx", "docx"),
        ("foo.PDF", "pdf"),
        ("no_extension", ""),
        ("trailing.dot.", ""),
        ("multi.dotted.name.docx", "docx"),
    ],
)
def test_extract_extension(name, expected):
    assert rfp_runner._extract_extension(name) == expected


# ---------------------------------------------------------------------------
# handle_rfp_message — early exits
# ---------------------------------------------------------------------------


def test_handle_rfp_message_no_files_posts_hint():
    say = MagicMock()
    event = {"channel": "CRFP123", "ts": "1700000000.0", "user": "U1"}
    rfp_runner.handle_rfp_message(event, say)
    say.assert_called_once()
    args, kwargs = say.call_args
    assert "Drop an RFP file" in args[0]
    assert kwargs["thread_ts"] == "1700000000.0"


def test_handle_rfp_message_unsupported_extension_posts_warning(monkeypatch):
    monkeypatch.setenv("RFP_RESPONDER_ID", "agent_rfp_test")
    say = MagicMock()
    event = {
        "channel": "CRFP123",
        "ts": "1700000000.0",
        "user": "U1",
        "files": [{"name": "screenshot.png", "id": "F1"}],
    }
    rfp_runner.handle_rfp_message(event, say)
    say.assert_called_once()
    args, _ = say.call_args
    assert "can only draft responses" in args[0]
    assert "png" in args[0]


def test_handle_rfp_message_missing_agent_id_posts_warning(monkeypatch):
    monkeypatch.delenv("RFP_RESPONDER_ID", raising=False)
    say = MagicMock()
    event = {
        "channel": "CRFP123",
        "ts": "1700000000.0",
        "user": "U1",
        "files": [{"name": "rfp.xlsx", "id": "F1"}],
    }
    rfp_runner.handle_rfp_message(event, say)
    say.assert_called_once()
    args, _ = say.call_args
    assert "RFP_RESPONDER_ID" in args[0]


def test_handle_rfp_message_spawns_daemon_thread(monkeypatch):
    """Happy-path entry: post ack, then dispatch to a daemon thread.

    The worker function itself is patched to a no-op so we only verify
    the dispatch contract — never actually open httpx clients or call
    Anthropic.
    """
    monkeypatch.setenv("RFP_RESPONDER_ID", "agent_rfp_test")
    say = MagicMock()
    event = {
        "channel": "CRFP123",
        "ts": "1700000000.0",
        "user": "U1",
        "files": [
            {
                "name": "rfp.xlsx",
                "id": "F1",
                "url_private_download": "https://files.slack.com/...",
                "size": 1024,
                "mimetype": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            }
        ],
    }

    started = {"called": False, "daemon": None}

    class FakeThread:
        def __init__(self, *_, target=None, kwargs=None, daemon=None, **_kw):
            self._target = target
            self._kwargs = kwargs
            started["daemon"] = daemon

        def start(self):
            started["called"] = True

    with patch.object(rfp_runner.threading, "Thread", FakeThread):
        rfp_runner.handle_rfp_message(event, say)

    # The ack always goes out before the thread spins up.
    assert say.call_count == 1
    args, kwargs = say.call_args
    assert "Drafting RFP response" in args[0]
    assert kwargs["thread_ts"] == "1700000000.0"
    assert started["called"] is True
    assert started["daemon"] is True


def test_handle_rfp_message_extra_files_get_a_single_note(monkeypatch):
    """Multi-file upload: process the first, mention the rest in the ack."""
    monkeypatch.setenv("RFP_RESPONDER_ID", "agent_rfp_test")
    say = MagicMock()
    event = {
        "channel": "CRFP123",
        "ts": "1700000000.0",
        "user": "U1",
        "files": [
            {
                "name": "rfp.xlsx",
                "id": "F1",
                "url_private_download": "https://files.slack.com/...",
            },
            {"name": "extra1.docx", "id": "F2"},
            {"name": "extra2.pdf", "id": "F3"},
        ],
    }

    class _NoopThread:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            pass

    with patch.object(rfp_runner.threading, "Thread", _NoopThread):
        rfp_runner.handle_rfp_message(event, say)

    assert say.call_count == 1
    ack = say.call_args[0][0]
    assert "Drafting RFP response from `rfp.xlsx`" in ack
    assert "extra1.docx" in ack
    assert "extra2.pdf" in ack


# ---------------------------------------------------------------------------
# _process_rfp_safe — top-level catch never re-raises
# ---------------------------------------------------------------------------


def test_process_rfp_safe_catches_worker_crash():
    """A worker crash posts an error to Slack and returns; never re-raises."""
    say = MagicMock()
    with patch.object(rfp_runner, "_process_rfp", side_effect=RuntimeError("boom")):
        # Should not raise.
        rfp_runner._process_rfp_safe(
            file_info={"name": "rfp.xlsx", "id": "F1"},
            ext="xlsx",
            thread_ts="1700000000.0",
            channel_id="CRFP123",
            user_id="U1",
            say=say,
        )
    say.assert_called_once()
    args, kwargs = say.call_args
    assert "RFP drafting failed unexpectedly" in args[0]
    assert kwargs["thread_ts"] == "1700000000.0"


# ---------------------------------------------------------------------------
# slack_bot._route_file_share_event — guards that protect the question pipeline
#
# Tests the extracted helper directly because the @app.event-decorated
# handle_message is uncallable from unit tests (conftest stubs slack_bolt.App
# as a MagicMock so the decorator returns a MagicMock, not the function).
# A regression that flips any of these gates would let bot-posted file_share
# events spawn spurious RFP sessions, or let uploads in non-RFP channels
# burn an Anthropic session.
# ---------------------------------------------------------------------------


def _import_slack_bot():
    """Import slack_bot lazily — conftest stubs slack_bolt at collection time."""
    try:
        import slack_bot  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover - env dependent
        pytest.skip(f"slack_bot import failed in this env: {exc}")
    return slack_bot


def test_file_share_in_rfp_channel_dispatches_to_runner(monkeypatch):
    """A genuine user file upload in the RFP channel reaches the runner."""
    monkeypatch.setenv("RFP_CHANNEL_ID", "CRFP123")
    monkeypatch.setenv("RFP_RESPONDER_ID", "agent_rfp_test")
    slack_bot = _import_slack_bot()
    slack_bot._seen_events.clear()

    called = {"hit": False}

    def fake_handle(event, say):
        called["hit"] = True

    monkeypatch.setattr(rfp_runner, "handle_rfp_message", fake_handle)
    monkeypatch.setattr(rfp_runner, "is_rfp_channel", lambda c: c == "CRFP123")

    result = slack_bot._route_file_share_event(
        {
            "subtype": "file_share",
            "channel": "CRFP123",
            "user": "U1",
            "ts": "1700000000.0",
            "event_ts": "1700000000.0",
            "files": [{"name": "rfp.xlsx", "id": "F1"}],
        },
        MagicMock(),
    )
    assert result is True
    assert called["hit"] is True


def test_file_share_with_bot_id_does_not_dispatch(monkeypatch):
    """A bot-posted file upload must NOT trigger the RFP runner.

    Workflow apps and integrations post via ``bot_id``; treating their
    uploads as RFP intake would burn Anthropic sessions for free.
    """
    monkeypatch.setenv("RFP_CHANNEL_ID", "CRFP123")
    monkeypatch.setenv("RFP_RESPONDER_ID", "agent_rfp_test")
    slack_bot = _import_slack_bot()
    slack_bot._seen_events.clear()

    monkeypatch.setattr(
        rfp_runner,
        "handle_rfp_message",
        lambda *_args, **_kwargs: pytest.fail("RFP runner should not run for bots"),
    )
    monkeypatch.setattr(rfp_runner, "is_rfp_channel", lambda c: c == "CRFP123")

    result = slack_bot._route_file_share_event(
        {
            "subtype": "file_share",
            "channel": "CRFP123",
            "user": "U1",
            "bot_id": "B0001",
            "ts": "1700000000.0",
            "event_ts": "1700000000.0",
            "files": [{"name": "rfp.xlsx", "id": "F1"}],
        },
        MagicMock(),
    )
    assert result is False


def test_file_share_with_bot_profile_does_not_dispatch(monkeypatch):
    """Some integrations only set ``bot_profile``; that variant is also filtered.

    Mirrors the dual-check pattern used in slack_bot's reaction handler.
    """
    monkeypatch.setenv("RFP_CHANNEL_ID", "CRFP123")
    monkeypatch.setenv("RFP_RESPONDER_ID", "agent_rfp_test")
    slack_bot = _import_slack_bot()
    slack_bot._seen_events.clear()

    monkeypatch.setattr(
        rfp_runner,
        "handle_rfp_message",
        lambda *_args, **_kwargs: pytest.fail(
            "RFP runner should not run for bot_profile events"
        ),
    )
    monkeypatch.setattr(rfp_runner, "is_rfp_channel", lambda c: c == "CRFP123")

    result = slack_bot._route_file_share_event(
        {
            "subtype": "file_share",
            "channel": "CRFP123",
            "user": "U1",
            "bot_profile": {"id": "B0001", "name": "workflow-bot"},
            "ts": "1700000000.0",
            "event_ts": "1700000000.0",
            "files": [{"name": "rfp.xlsx", "id": "F1"}],
        },
        MagicMock(),
    )
    assert result is False


def test_file_share_in_non_rfp_channel_falls_through(monkeypatch):
    """A file upload in any channel other than the RFP channel is dropped."""
    monkeypatch.setenv("RFP_CHANNEL_ID", "CRFP123")
    monkeypatch.setenv("RFP_RESPONDER_ID", "agent_rfp_test")
    slack_bot = _import_slack_bot()
    slack_bot._seen_events.clear()

    monkeypatch.setattr(
        rfp_runner,
        "handle_rfp_message",
        lambda *_args, **_kwargs: pytest.fail(
            "RFP runner must not run for non-RFP channel uploads"
        ),
    )
    monkeypatch.setattr(rfp_runner, "is_rfp_channel", lambda c: c == "CRFP123")

    result = slack_bot._route_file_share_event(
        {
            "subtype": "file_share",
            "channel": "CSOMEOTHER",
            "user": "U1",
            "ts": "1700000000.0",
            "event_ts": "1700000000.0",
            "files": [{"name": "rfp.xlsx", "id": "F1"}],
        },
        MagicMock(),
    )
    assert result is False


def test_file_share_duplicate_event_is_deduped(monkeypatch):
    """Slack redelivery of the same file_share event must dispatch only once."""
    monkeypatch.setenv("RFP_CHANNEL_ID", "CRFP123")
    monkeypatch.setenv("RFP_RESPONDER_ID", "agent_rfp_test")
    slack_bot = _import_slack_bot()
    slack_bot._seen_events.clear()

    calls = {"count": 0}

    def fake_handle(event, say):
        calls["count"] += 1

    monkeypatch.setattr(rfp_runner, "handle_rfp_message", fake_handle)
    monkeypatch.setattr(rfp_runner, "is_rfp_channel", lambda c: c == "CRFP123")

    payload = {
        "subtype": "file_share",
        "channel": "CRFP123",
        "user": "U1",
        "ts": "1700000000.0",
        "event_ts": "1700000000.0",
        "files": [{"name": "rfp.xlsx", "id": "F1"}],
    }
    r1 = slack_bot._route_file_share_event(payload, MagicMock())
    r2 = slack_bot._route_file_share_event(payload, MagicMock())
    # Both return True (claimed), but the runner only fires once.
    assert r1 is True
    assert r2 is True
    assert calls["count"] == 1, "Second delivery should be deduped"
