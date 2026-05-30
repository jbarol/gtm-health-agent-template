"""Tests for ``channel_descriptions`` (Plan #49).

Cover:
  * the three-way ``classify_channel`` lookup,
  * ``render_description`` substitution + length guard,
  * ``push_channel_description`` decision tree (skip on human-owned,
    skip on identical, write on drift, write on first-time),
  * the ``CHANNEL_DESC_PUSH_ENABLED`` kill switch,
  * SlackApiError handling (ratelimited retry + non-retryable error).

Run:
    cd orchestrator && python3 -m pytest channel_descriptions_test.py -q
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

# Required env vars for config.py to import without raising. setdefault
# means a real .env wins.
for _key, _value in {
    "ANTHROPIC_API_KEY": "sk-ant-test",
    "SLACK_BOT_TOKEN": "xoxb-test",
    "SLACK_APP_TOKEN": "xapp-test",
    "SLACK_CHANNEL_ID": "C0TEST",
    "ENVIRONMENT_ID": "env_test",
    "DREAM_AGENT_ID": "agent_test_dream",
    "COORDINATOR_ID": "agent_test_coord",
    "QUICK_AGENT_ID": "agent_test_quick",
    "METHODOLOGY_STORE_ID": "memstore_test_m",
    "HEALTH_STORE_ID": "memstore_test_h",
}.items():
    os.environ.setdefault(_key, _value)


from slack_sdk.errors import SlackApiError  # noqa: E402

import channel_descriptions  # noqa: E402


# ---------- Fixtures ----------


def _slack_api_error(code: str, retry_after: str | None = None) -> SlackApiError:
    """Build a SlackApiError whose .response.get('error') returns ``code``."""
    response = MagicMock(name="SlackResponse")
    response.get = lambda key, default=None: {"error": code}.get(key, default)
    response.headers = {"Retry-After": retry_after} if retry_after else {}
    return SlackApiError(message=code, response=response)


def _info_response(purpose_value: str | None) -> dict:
    """Mock a ``conversations.info`` response payload."""
    return {
        "ok": True,
        "channel": {"purpose": {"value": purpose_value} if purpose_value is not None else {"value": None}},
    }


@pytest.fixture
def fake_client():
    """Mock Slack client with happy-path return values."""
    client = MagicMock(name="WebClient")
    client.conversations_info.return_value = _info_response("")
    client.conversations_setPurpose.return_value = {"ok": True}
    return client


@pytest.fixture(autouse=True)
def _reset_failure_state():  # pyright: ignore[reportUnusedFunction]
    """Clear in-process failure dedup state before/after each test.

    The ``@pytest.fixture(autouse=True)`` decorator registers this with
    pytest's fixture system, which invokes it before/after every test.
    Pyright doesn't see that indirect invocation, hence the ignore.
    """
    channel_descriptions._reset_failure_state()
    yield
    channel_descriptions._reset_failure_state()


@pytest.fixture(autouse=True)
def _force_kill_switch_on(monkeypatch):  # pyright: ignore[reportUnusedFunction]
    """Default every test to CHANNEL_DESC_PUSH_ENABLED=true; opt out per-test.

    See ``_reset_failure_state`` for the autouse-fixture rationale.
    """
    monkeypatch.delenv("CHANNEL_DESC_PUSH_ENABLED", raising=False)
    yield


# ---------- Classifier ----------


def test_classify_rfp_channel():
    assert channel_descriptions.classify_channel("C0000000001") == "rfp"


def test_classify_portco_channel(monkeypatch):
    monkeypatch.setattr(
        channel_descriptions.portco_registry,
        "get_portco_by_channel",
        lambda _cid: {"key": "acme", "name": "Acme"},
    )
    assert (
        channel_descriptions.classify_channel("C0000000000") == "portco_analyst"
    )


def test_classify_unknown_channel(monkeypatch):
    monkeypatch.setattr(
        channel_descriptions.portco_registry,
        "get_portco_by_channel",
        lambda _cid: None,
    )
    assert channel_descriptions.classify_channel("CXNOTREAL") == "unknown"


def test_classify_portco_registry_raises(monkeypatch):
    """Registry lookup raises → graceful degrade to 'unknown', no propagation."""

    def boom(_cid):
        raise RuntimeError("registry kaput")

    monkeypatch.setattr(
        channel_descriptions.portco_registry,
        "get_portco_by_channel",
        boom,
    )
    assert channel_descriptions.classify_channel("CXBOOM") == "unknown"


def test_classify_empty_channel_id():
    assert channel_descriptions.classify_channel("") == "unknown"


# ---------- Renderer ----------


def test_render_rfp_starts_with_sentinel():
    out = channel_descriptions.render_description("rfp")
    assert out.startswith(channel_descriptions.SENTINEL)


def test_render_rfp_length():
    out = channel_descriptions.render_description("rfp")
    assert len(out) <= channel_descriptions.PURPOSE_MAX_CHARS


def test_render_portco_analyst_substitution():
    out = channel_descriptions.render_description(
        "portco_analyst", portco_name="Acme"
    )
    assert out.startswith(channel_descriptions.SENTINEL)
    assert "Acme" in out
    assert "{portco_name}" not in out


def test_render_portco_analyst_length():
    out = channel_descriptions.render_description(
        "portco_analyst", portco_name="Acme"
    )
    assert len(out) <= channel_descriptions.PURPOSE_MAX_CHARS


def test_render_unknown_length():
    out = channel_descriptions.render_description("unknown")
    assert out.startswith(channel_descriptions.SENTINEL)
    assert len(out) <= channel_descriptions.PURPOSE_MAX_CHARS


def test_render_enforces_length_limit(monkeypatch):
    """Inject an overlong template → output is 247 chars + '...'."""
    monkeypatch.setattr(
        channel_descriptions, "_TEMPLATE_UNKNOWN", "x" * 500
    )
    out = channel_descriptions.render_description("unknown")
    assert len(out) == channel_descriptions.PURPOSE_MAX_CHARS
    assert out.endswith("...")


# ---------- Drift / sentinel guard ----------


def test_skip_human_owned_purpose(fake_client, monkeypatch):
    """conversations.info returns purpose w/o sentinel → no setPurpose call."""
    fake_client.conversations_info.return_value = _info_response(
        "Important channel purpose — set by a human."
    )
    monkeypatch.setattr(
        channel_descriptions, "_slack_client", lambda: fake_client
    )
    monkeypatch.setattr(
        channel_descriptions.portco_registry,
        "get_portco_by_channel",
        lambda _cid: None,
    )

    ok = channel_descriptions.push_channel_description("CXHUMAN")

    assert ok is True
    fake_client.conversations_setPurpose.assert_not_called()


def test_skip_identical_purpose(fake_client, monkeypatch):
    """Current purpose == rendered target → no setPurpose call."""
    monkeypatch.setattr(
        channel_descriptions.portco_registry,
        "get_portco_by_channel",
        lambda _cid: {"key": "acme", "name": "Acme"},
    )
    target = channel_descriptions.render_description(
        "portco_analyst", portco_name="Acme"
    )
    fake_client.conversations_info.return_value = _info_response(target)
    monkeypatch.setattr(
        channel_descriptions, "_slack_client", lambda: fake_client
    )

    ok = channel_descriptions.push_channel_description("C0000000000")

    assert ok is True
    fake_client.conversations_setPurpose.assert_not_called()


def test_writes_when_drifted(fake_client, monkeypatch):
    """Current sentinel-prefixed purpose differs from target → setPurpose."""
    monkeypatch.setattr(
        channel_descriptions.portco_registry,
        "get_portco_by_channel",
        lambda _cid: {"key": "acme", "name": "Acme"},
    )
    fake_client.conversations_info.return_value = _info_response(
        channel_descriptions.SENTINEL + "OLD COPY"
    )
    monkeypatch.setattr(
        channel_descriptions, "_slack_client", lambda: fake_client
    )

    ok = channel_descriptions.push_channel_description("C0000000000")

    assert ok is True
    fake_client.conversations_setPurpose.assert_called_once()
    kwargs = fake_client.conversations_setPurpose.call_args.kwargs
    assert kwargs["channel"] == "C0000000000"
    assert kwargs["purpose"].startswith(channel_descriptions.SENTINEL)
    assert "Acme" in kwargs["purpose"]


def test_writes_on_first_time(fake_client, monkeypatch):
    """Empty current purpose → setPurpose is called."""
    monkeypatch.setattr(
        channel_descriptions.portco_registry,
        "get_portco_by_channel",
        lambda _cid: {"key": "acme", "name": "Acme"},
    )
    fake_client.conversations_info.return_value = _info_response("")
    monkeypatch.setattr(
        channel_descriptions, "_slack_client", lambda: fake_client
    )

    ok = channel_descriptions.push_channel_description("C0000000000")

    assert ok is True
    fake_client.conversations_setPurpose.assert_called_once()


def test_first_time_no_existing_purpose(fake_client, monkeypatch):
    """conversations.info returns None for purpose.value → setPurpose called."""
    monkeypatch.setattr(
        channel_descriptions.portco_registry,
        "get_portco_by_channel",
        lambda _cid: {"key": "acme", "name": "Acme"},
    )
    # purpose.value is None — emulates a freshly-created channel.
    fake_client.conversations_info.return_value = {
        "ok": True,
        "channel": {"purpose": {"value": None}},
    }
    monkeypatch.setattr(
        channel_descriptions, "_slack_client", lambda: fake_client
    )

    ok = channel_descriptions.push_channel_description("C0000000000")

    assert ok is True
    fake_client.conversations_setPurpose.assert_called_once()


# ---------- Kill switch ----------


def test_kill_switch_false(fake_client, monkeypatch):
    """CHANNEL_DESC_PUSH_ENABLED=false → return True, never touch Slack."""
    monkeypatch.setenv("CHANNEL_DESC_PUSH_ENABLED", "false")
    monkeypatch.setattr(
        channel_descriptions, "_slack_client", lambda: fake_client
    )

    ok = channel_descriptions.push_channel_description("C0000000000")

    assert ok is True
    fake_client.conversations_info.assert_not_called()
    fake_client.conversations_setPurpose.assert_not_called()


# ---------- Failure handling ----------


def test_conversations_info_error(fake_client, monkeypatch):
    """conversations.info raises → get_current_purpose returns '' (per
    docstring contract), push_channel_description falls through to a
    first-time setPurpose attempt, which also raises with the same
    error (channel_not_found, not_in_channel, etc.) and is caught by
    the outer handler. Net result: returns False, logs
    ``[CHANNEL_DESC_FAILED]``.
    """
    fake_client.conversations_info.side_effect = _slack_api_error(
        "channel_not_found"
    )
    # setPurpose will hit the same underlying failure mode for these
    # error classes; model that explicitly so the test reflects the
    # real-world failure path.
    fake_client.conversations_setPurpose.side_effect = _slack_api_error(
        "channel_not_found"
    )
    monkeypatch.setattr(
        channel_descriptions, "_slack_client", lambda: fake_client
    )
    monkeypatch.setattr(
        channel_descriptions.portco_registry,
        "get_portco_by_channel",
        lambda _cid: None,
    )

    ok = channel_descriptions.push_channel_description("CXMISSING")

    assert ok is False


def test_set_purpose_ratelimited_then_succeeds(fake_client, monkeypatch):
    """First setPurpose raises ratelimited; second succeeds → True."""
    monkeypatch.setattr(
        channel_descriptions.portco_registry,
        "get_portco_by_channel",
        lambda _cid: {"key": "acme", "name": "Acme"},
    )
    fake_client.conversations_info.return_value = _info_response("")
    fake_client.conversations_setPurpose.side_effect = [
        _slack_api_error("ratelimited", retry_after="0"),
        {"ok": True},
    ]
    monkeypatch.setattr(
        channel_descriptions, "_slack_client", lambda: fake_client
    )
    # Skip the back-off sleep so the test runs instantly.
    monkeypatch.setattr(channel_descriptions.time, "sleep", lambda s: None)

    ok = channel_descriptions.push_channel_description("C0000000000")

    assert ok is True
    assert fake_client.conversations_setPurpose.call_count == 2


def test_set_purpose_non_retryable_error(fake_client, monkeypatch):
    """setPurpose raises channel_not_found → returns False, no retry."""
    monkeypatch.setattr(
        channel_descriptions.portco_registry,
        "get_portco_by_channel",
        lambda _cid: {"key": "acme", "name": "Acme"},
    )
    fake_client.conversations_info.return_value = _info_response("")
    fake_client.conversations_setPurpose.side_effect = _slack_api_error(
        "channel_not_found"
    )
    monkeypatch.setattr(
        channel_descriptions, "_slack_client", lambda: fake_client
    )

    ok = channel_descriptions.push_channel_description("CXBROKEN")

    assert ok is False
    assert fake_client.conversations_setPurpose.call_count == 1
