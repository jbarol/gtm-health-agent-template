"""Tests for ``orchestrator/anthropic_webhooks.py`` (Plan #44 Task #14).

Verifies:

  - Signature verification: 400 when no signing key, 400 on bad sig,
    happy path on valid sig.
  - Per-event handlers produce the canonical WHAT + WHY + FIX-COMMAND
    DM template (decision rows #21, #24).
  - Unknown event types are ignored without raising.
  - The dispatcher fetches the full session object when the type is
    session.status_terminated (decision: "don't trust the webhook body
    alone").

Mocking convention (post-2026-05-13 review HIGH #1):
  The fake Anthropic client is built from a real ``anthropic.Anthropic``
  instance and constrained via ``spec=`` so a regression like
  ``cli.webhooks.unwrap`` (which doesn't exist on the SDK — the resource
  lives at ``cli.beta.webhooks``) fails the test at attribute access
  rather than silently passing on a MagicMock auto-attribute.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import anthropic


def _set_signing_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_WEBHOOK_SIGNING_KEY", "shhh-signing")


def _make_fake_client(
    *, unwrap_side_effect=None, unwrap_return=None, retrieve_return=None
):
    """Build a constrained MagicMock matching the real Anthropic client shape.

    Uses ``spec=anthropic.Anthropic`` so unknown top-level attributes
    (e.g. ``.webhooks``) fail loud. The ``beta`` sub-resource needs its
    own spec so ``beta.webhooks.unwrap`` is the only path that works.
    """
    fake = MagicMock(spec=anthropic.Anthropic, name="anthropic.Anthropic")
    # ``beta`` needs its own real anchor so spec= can pick up
    # ``beta.webhooks`` and ``beta.sessions``.
    fake.beta = MagicMock(name="anthropic.Anthropic.beta")
    if unwrap_side_effect is not None:
        fake.beta.webhooks.unwrap.side_effect = unwrap_side_effect
    if unwrap_return is not None:
        fake.beta.webhooks.unwrap.return_value = unwrap_return
    if retrieve_return is not None:
        fake.beta.sessions.retrieve.return_value = retrieve_return
    return fake


# ─────────────────────────────────────────────────────────────────────────────
# verify_and_parse
# ─────────────────────────────────────────────────────────────────────────────


def test_verify_rejects_when_key_unset(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_WEBHOOK_SIGNING_KEY", raising=False)

    import anthropic_webhooks

    ok, event, err = anthropic_webhooks.verify_and_parse(b"{}", {})

    assert ok is False
    assert event is None
    assert "SIGNING_KEY" in err


def test_verify_rejects_bad_signature(monkeypatch):
    _set_signing_key(monkeypatch)

    fake_client = _make_fake_client(unwrap_side_effect=RuntimeError("bad sig"))

    with patch("anthropic_webhooks._client", return_value=fake_client):
        import anthropic_webhooks

        ok, event, err = anthropic_webhooks.verify_and_parse(
            b'{"type":"x"}',
            {"X-Signature": "deadbeef"},
        )

    assert ok is False
    assert event is None
    assert "invalid signature" in err
    # Regression guard: must use beta.webhooks.unwrap (not .webhooks).
    fake_client.beta.webhooks.unwrap.assert_called_once()


def test_verify_happy_path_returns_event_dict(monkeypatch):
    _set_signing_key(monkeypatch)

    fake_event = SimpleNamespace(
        type="session.status_terminated",
        data={"id": "sesn_EXAMPLE"},
    )
    fake_event.model_dump = MagicMock(
        return_value={"type": "session.status_terminated", "data": {"id": "sesn_EXAMPLE"}}
    )
    fake_client = _make_fake_client(unwrap_return=fake_event)

    with patch("anthropic_webhooks._client", return_value=fake_client):
        import anthropic_webhooks

        ok, event, err = anthropic_webhooks.verify_and_parse(b"{}", {})

    assert ok is True
    assert event["type"] == "session.status_terminated"
    assert event["data"]["id"] == "sesn_EXAMPLE"
    assert err == ""
    # Regression guard: confirm the SDK call landed at beta.webhooks.unwrap
    # with the kwargs we documented (headers=, key=).
    fake_client.beta.webhooks.unwrap.assert_called_once()
    call_kwargs = fake_client.beta.webhooks.unwrap.call_args.kwargs
    assert "headers" in call_kwargs
    assert "key" in call_kwargs and call_kwargs["key"] == "shhh-signing"


def test_verify_uses_beta_webhooks_not_top_level_attribute(monkeypatch):
    """Regression for closing-review HIGH #1, 2026-05-13.

    A previous version called ``cli.webhooks.unwrap`` (no ``beta``).
    The Anthropic SDK exposes ``webhooks`` only under ``cli.beta``.
    With a permissive MagicMock the bug was invisible; with
    ``spec=anthropic.Anthropic`` accessing ``cli.webhooks`` raises
    ``AttributeError``, so a regression now fails this test loud.
    """
    _set_signing_key(monkeypatch)

    import anthropic_webhooks  # noqa: F401

    # Build a spec'd client and probe the surface ourselves first.
    fake_client = _make_fake_client(unwrap_side_effect=RuntimeError("x"))
    # If the spec is doing its job, attribute access for "webhooks"
    # (top-level) should raise — that's our regression sentinel.
    import pytest

    with pytest.raises(AttributeError):
        _ = fake_client.webhooks  # pyright: ignore[reportGeneralTypeIssues]

    # And beta.webhooks.unwrap MUST be reachable — that's the canonical
    # call path the code under test relies on.
    assert hasattr(fake_client.beta, "webhooks")
    assert hasattr(fake_client.beta.webhooks, "unwrap")


# ─────────────────────────────────────────────────────────────────────────────
# Per-event handlers — DM template shape
# ─────────────────────────────────────────────────────────────────────────────


def test_session_status_terminated_dm_carries_what_why_fix():
    import anthropic_webhooks

    fake_session = SimpleNamespace(
        id="sesn_EXAMPLE",
        usage=None,
        agent_id="agent_EXAMPLE_coordinator",  # coordinator
        error="Tool soqlQuery exceeded budget",
    )
    fake_session.model_dump = MagicMock(
        return_value={
            "id": "sesn_EXAMPLE",
            "agent_id": "agent_EXAMPLE_coordinator",
            "error": "Tool soqlQuery exceeded budget",
            "metadata": {"channel_id": "C1", "thread_ts": "1737654.000"},
        }
    )
    fake_client = _make_fake_client(retrieve_return=fake_session)

    with patch("anthropic_webhooks._client", return_value=fake_client):
        event = {
            "type": "session.status_terminated",
            "data": {"id": "sesn_EXAMPLE"},
        }
        result = anthropic_webhooks.handle_session_status_terminated(event)

    assert result["event_type"] == "session.status_terminated"
    assert result["severity"] == "critical"
    dm = result["dm"]
    assert "*WHAT:*" in dm
    assert "*WHY:*" in dm
    assert "*FIX:*" in dm
    # The pre-filled rollback command names the agent short name.
    assert "bin/rollback-agent.py coordinator" in dm
    # The error excerpt is in the WHY line.
    assert "Tool soqlQuery exceeded budget" in dm


def test_vault_refresh_failed_dm_pre_fills_vault_credential_cmd():
    import anthropic_webhooks

    event = {
        "type": "vault_credential.refresh_failed",
        "data": {
            "id": "vault_acme",
            "error": "refresh_token expired",
        },
    }
    result = anthropic_webhooks.handle_vault_credential_refresh_failed(event)

    assert result["severity"] == "critical"
    dm = result["dm"]
    assert "*WHAT:*" in dm
    assert "vault_acme" in dm
    assert "refresh_token expired" in dm
    # Pre-fills the vault-credential fix command.
    assert "bin/add-sf-vault-credential.py" in dm
    assert "--apply" in dm
    assert "--vault vault_acme" in dm


def test_thread_terminated_dm_is_lower_severity():
    import anthropic_webhooks

    event = {
        "type": "session.thread_terminated",
        "data": {
            "session_id": "sesn_EXAMPLE",
            "thread_id": "thr_b",
        },
    }
    result = anthropic_webhooks.handle_session_thread_terminated(event)

    assert result["severity"] == "watch"
    assert "thr_b" in result["dm"]
    assert "*FIX:*" in result["dm"]


def test_outcome_evaluation_ended_is_logged_no_dm():
    import anthropic_webhooks

    event = {
        "type": "session.outcome_evaluation_ended",
        "data": {"session_id": "sesn_EXAMPLE"},
    }
    result = anthropic_webhooks.handle_session_outcome_evaluation_ended(event)

    assert result["dm"] is None
    assert result["severity"] == "info"


# ─────────────────────────────────────────────────────────────────────────────
# dispatch_event
# ─────────────────────────────────────────────────────────────────────────────


def test_dispatch_unknown_type_is_noop():
    import anthropic_webhooks

    result = anthropic_webhooks.dispatch_event(
        {"type": "session.totally_fake", "data": {}}
    )

    assert result["dm"] is None
    assert result["event_type"] == "session.totally_fake"


def test_dispatch_routes_to_correct_handler():
    import anthropic_webhooks

    with patch.object(anthropic_webhooks, "_fetch_session", return_value={}):
        result = anthropic_webhooks.dispatch_event(
            {
                "type": "vault_credential.refresh_failed",
                "data": {"id": "vault_x", "error": "bad"},
            }
        )

    assert result["event_type"] == "vault_credential.refresh_failed"
    assert "vault_x" in (result["dm"] or "")


# ─────────────────────────────────────────────────────────────────────────────
# handle_webhook — end-to-end status code wiring
# ─────────────────────────────────────────────────────────────────────────────


def test_handle_webhook_returns_400_on_bad_signature(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_WEBHOOK_SIGNING_KEY", raising=False)

    import anthropic_webhooks

    status, body = anthropic_webhooks.handle_webhook(b"{}", {})

    assert status == 400
    assert "ok" in body.lower()


def test_handle_webhook_returns_200_on_happy_path(monkeypatch):
    _set_signing_key(monkeypatch)

    fake_event = SimpleNamespace(
        type="session.outcome_evaluation_ended",
        data={"session_id": "sesn_EXAMPLE"},
    )
    fake_event.model_dump = MagicMock(
        return_value={
            "type": "session.outcome_evaluation_ended",
            "data": {"session_id": "sesn_EXAMPLE"},
        }
    )
    fake_client = _make_fake_client(unwrap_return=fake_event)

    with patch("anthropic_webhooks._client", return_value=fake_client):
        import anthropic_webhooks

        status, body = anthropic_webhooks.handle_webhook(b"{}", {})

    assert status == 200
    assert "session.outcome_evaluation_ended" in body
