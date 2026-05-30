"""Plan #44 Task #14 — webhook route registration helper.

Bundle E does not modify :mod:`orchestrator.main` (Bundle B owns it). This
module exposes a single :func:`register_webhook_routes` function that
Bundle B's main.py will import at integration time:

    from anthropic_webhooks_register import register_webhook_routes
    register_webhook_routes(http_server_or_app)

The orchestrator's HTTP layer is the stdlib ``http.server.HTTPServer``
running a ``BaseHTTPRequestHandler`` subclass (see ``main._HealthHandler``).
We can't register routes on that handler from outside the class, so the
integration shape is to compose: Bundle B's main.py adds a
``/webhooks/anthropic`` branch in ``_HealthHandler.do_POST`` that calls
:func:`process_webhook_post`.

This module keeps the framework-agnostic processing pipeline:

    process_webhook_post(body: bytes, headers: dict) -> (status, body)

so the integration point is mechanical — no HTTP plumbing leaks into
``anthropic_webhooks.py``, and Bundle B's main.py changes are small
enough to merge cleanly.
"""

from __future__ import annotations

import logging
from typing import Tuple

from anthropic_webhooks import handle_webhook

log = logging.getLogger(__name__)


WEBHOOK_PATH = "/webhooks/anthropic"


def process_webhook_post(body: bytes, headers: dict) -> Tuple[int, str]:
    """Process an incoming Anthropic webhook POST.

    Bundle B's ``main._HealthHandler.do_POST`` (added at integration
    time) will call this helper. Signature is plain Python so it does
    NOT couple Bundle E to a specific HTTP framework.

    Returns ``(http_status, json_body)``. The caller writes the response.
    """
    return handle_webhook(body, headers)


def register_webhook_routes(app) -> None:
    """Compatibility shim for an eventual move to Flask/aiohttp.

    Today the orchestrator uses stdlib ``http.server`` — there is no
    ``app`` to mount routes on. We keep this function as a stable import
    surface so a future framework swap (Plan #44 mentions Flask) won't
    require touching every caller. If ``app`` exposes a ``route`` or
    ``add_url_rule`` method we attempt to register the handler; otherwise
    we log and no-op (the integration is mechanical via
    :func:`process_webhook_post` instead).

    Bundle B's main.py is encouraged to call this AND wire
    :func:`process_webhook_post` into the stdlib handler — once the
    framework swap lands, only the stdlib branch goes away.
    """
    if app is None:
        log.info(
            "register_webhook_routes: no app provided — stdlib http.server "
            "integration uses process_webhook_post directly"
        )
        return

    # Flask-style: ``app.route(path, methods=["POST"])(handler)``.
    route = getattr(app, "route", None)
    if callable(route):
        try:

            def _flask_view():  # pragma: no cover — only fires if Flask added
                from flask import request  # type: ignore

                status, body = process_webhook_post(
                    request.get_data(), dict(request.headers)
                )
                return body, status, {"Content-Type": "application/json"}

            route(WEBHOOK_PATH, methods=["POST"])(_flask_view)
            log.info(f"registered Flask route {WEBHOOK_PATH} (POST)")
            return
        except Exception:
            log.exception("Flask route registration failed")

    # aiohttp-style: ``app.router.add_post(path, handler)``.
    router = getattr(app, "router", None)
    add_post = getattr(router, "add_post", None) if router else None
    if callable(add_post):
        try:

            async def _aiohttp_view(request):  # pragma: no cover
                body = await request.read()
                status, body_str = process_webhook_post(body, dict(request.headers))
                from aiohttp import web  # type: ignore

                return web.Response(
                    text=body_str,
                    status=status,
                    content_type="application/json",
                )

            add_post(WEBHOOK_PATH, _aiohttp_view)
            log.info(f"registered aiohttp route {WEBHOOK_PATH} (POST)")
            return
        except Exception:
            log.exception("aiohttp route registration failed")

    log.info(
        "register_webhook_routes: app has no recognized router; "
        "use process_webhook_post directly"
    )
