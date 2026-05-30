"""Tests for ``orchestrator/anthropic_webhooks_register.py``.

The module exposes a framework-agnostic helper that Bundle B's main.py
will import. We verify the two paths:

  - :func:`process_webhook_post` round-trips through
    :func:`anthropic_webhooks.handle_webhook` (no extra logic — the
    register module is a stable import surface).
  - :func:`register_webhook_routes` no-ops when given ``None`` (stdlib
    path) and logs without raising.
"""

from __future__ import annotations

from unittest.mock import MagicMock


def test_process_webhook_post_returns_handle_webhook_value(monkeypatch):
    import anthropic_webhooks_register

    monkeypatch.setattr(
        "anthropic_webhooks_register.handle_webhook",
        lambda body, headers: (200, '{"ok": true}'),
    )

    status, body = anthropic_webhooks_register.process_webhook_post(b"{}", {})

    assert status == 200
    assert body == '{"ok": true}'


def test_register_routes_handles_none_app():
    import anthropic_webhooks_register

    # Must not raise even with no app to mount on.
    anthropic_webhooks_register.register_webhook_routes(None)


def test_register_routes_flask_style_app():
    """A mock app exposing ``.route(path, methods=[...])(handler)`` —
    verifies the Flask code path doesn't raise."""
    import anthropic_webhooks_register

    fake_app = MagicMock(name="flask.Flask")

    # Flask's route(path, methods=...) returns a decorator. We just need
    # something callable that accepts the inner view function.
    decorator = MagicMock(name="route_decorator")
    fake_app.route.return_value = decorator

    anthropic_webhooks_register.register_webhook_routes(fake_app)

    fake_app.route.assert_called_once_with(
        anthropic_webhooks_register.WEBHOOK_PATH, methods=["POST"]
    )


def test_webhook_path_constant_is_stable():
    import anthropic_webhooks_register

    assert anthropic_webhooks_register.WEBHOOK_PATH == "/webhooks/anthropic"
