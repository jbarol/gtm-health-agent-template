"""Tests for the Plan #42 PR2 startup ordering and ``/ready`` gate.

These tests monkey-patch ``smoke_probe.run_smoke_probe`` to return a chosen
outcome, run ``main.main()`` end-to-end with all heavy side-effects stubbed,
and assert that:

  * Smoke probe FAIL → ``_READY`` stays False, ``/ready`` returns 503,
    ``start_socket_mode()`` is NOT called.
  * Smoke probe PASS → ``_READY`` flips True, ``/ready`` returns 200,
    ``start_socket_mode()`` IS called.
  * Inconclusive PASS (Anthropic 429/503) → treated as PASS for gating.
  * ``SMOKE_PROBE_ENABLED=false`` → probe skipped, ``_READY=True`` immediately,
    admin DM warns.

The HTTP routing is tested by instantiating ``_HealthHandler`` against a
mock request and asserting the response.

Run:
    cd orchestrator && python3 -m pytest main_startup_ordering_test.py -q
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import sys
from io import BytesIO
from unittest.mock import MagicMock, patch


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


def _import_main_fresh():
    """Re-import ``main`` so module-globals (_READY) reset across tests."""
    sys.modules.pop("config", None)
    sys.modules.pop("main", None)
    sys.modules.pop("smoke_probe", None)
    with patch("anthropic.Anthropic", MagicMock()):
        return importlib.import_module("main")


# ──────────────────────────────────────────────────────────────────────────
# Stubs reused across tests
# ──────────────────────────────────────────────────────────────────────────


class _FakeScheduler:
    """Stand-in for ``BackgroundScheduler`` that records ``add_job`` calls."""

    def __init__(self, *args, **kwargs):
        self.jobs = []
        self.started = False

    def add_job(self, func, trigger=None, *, id=None, name=None, **kwargs):  # noqa: A002
        self.jobs.append(
            {"func": func, "trigger": trigger, "id": id, "name": name, "kwargs": kwargs}
        )

    def add_listener(self, *args, **kwargs):
        pass

    def start(self):
        self.started = True

    def shutdown(self, **kwargs):
        pass


def _run_main_with_probe_outcome(
    *, passed: bool, anthropic_status: str = "ok", smoke_enabled: bool = True
):
    """Drive ``main.main()`` with a stubbed smoke-probe outcome.

    Returns the fresh ``main`` module so tests can assert on its
    module-globals (``_READY``, etc.).
    """
    main = _import_main_fresh()

    fake_sched = _FakeScheduler()
    if smoke_enabled:
        os.environ["SMOKE_PROBE_ENABLED"] = "true"
    else:
        os.environ["SMOKE_PROBE_ENABLED"] = "false"

    # Make the smoke probe return our chosen outcome. Importing inside
    # _run_pre_deploy_smoke_probe means the patch target is the smoke_probe
    # module's ``run_smoke_probe`` attribute, not ``main.run_smoke_probe``.
    import smoke_probe

    fake_result = smoke_probe.SmokeResult(
        passed=passed,
        reason="" if passed else "failed_checks: sf",
        elapsed_s=2.0,
        check_results={
            "build_commit": {"ok": True, "detail": "match"},
            "dump_sf_query": {
                "ok": passed,
                "detail": "ok" if passed else "auth failed",
            },
            "quick_answer": {"ok": passed, "detail": "ok" if passed else "skipped"},
        },
        anthropic_status=anthropic_status,
    )

    with (
        patch.object(smoke_probe, "run_smoke_probe", return_value=fake_result),
        patch.object(smoke_probe, "render_disabled_dm", return_value=("WARN", "warn")),
        patch.object(main, "BackgroundScheduler", return_value=fake_sched),
        patch.object(main, "set_question_handler"),
        patch.object(main, "set_feedback_handler"),
        patch.object(main, "start_socket_mode") as mocked_socket,
        patch.object(main, "is_db_available", return_value=False),
        patch.object(main, "send_notification"),
        patch.object(main, "_start_health_server"),
        patch.object(main, "ensure_schema"),
        patch.object(main, "recover_interrupted_investigations", return_value=[]),
        patch("signal.signal"),
    ):
        main.main()
        return main, mocked_socket


# ──────────────────────────────────────────────────────────────────────────
# _READY gate
# ──────────────────────────────────────────────────────────────────────────


def test_smoke_probe_pass_flips_ready_and_starts_socket_mode():
    main, mocked_socket = _run_main_with_probe_outcome(passed=True)
    assert main._READY is True
    assert main._READY_REASON == "smoke_probe_passed"
    mocked_socket.assert_called_once()


def test_smoke_probe_fail_keeps_ready_false_and_skips_socket_mode():
    main, mocked_socket = _run_main_with_probe_outcome(passed=False)
    assert main._READY is False
    assert main._READY_REASON == "smoke_probe_failed"
    mocked_socket.assert_not_called()


def test_inconclusive_pass_still_starts_socket_mode():
    main, mocked_socket = _run_main_with_probe_outcome(
        passed=True, anthropic_status="rate_limited"
    )
    assert main._READY is True
    mocked_socket.assert_called_once()


def test_disabled_env_sets_ready_true_and_skips_probe():
    main, mocked_socket = _run_main_with_probe_outcome(
        passed=False, smoke_enabled=False
    )
    # Even though our fake probe was set to FAIL, the disabled env var
    # short-circuits before calling it.
    assert main._READY is True
    assert main._READY_REASON == "smoke_probe_disabled"
    mocked_socket.assert_called_once()


# ──────────────────────────────────────────────────────────────────────────
# /ready route response shape
# ──────────────────────────────────────────────────────────────────────────


def _invoke_handler_get(main, path: str) -> tuple[int, dict]:
    """Drive ``main._HealthHandler.do_GET`` against a mocked request."""

    class _MockRequest:
        def makefile(self, *args, **kwargs):
            return BytesIO(f"GET {path} HTTP/1.1\r\n\r\n".encode())

    class _CapturingHandler(main._HealthHandler):
        def __init__(self, request, client_address, server):
            self._response_status = None
            self._response_headers: list[tuple[str, str]] = []
            self._response_body = b""
            self.rfile = request.makefile()
            self.wfile = BytesIO()
            self.request = request
            self.client_address = client_address
            self.server = server
            # Skip the BaseHTTPRequestHandler __init__ logic that calls
            # handle() — we want to invoke do_GET manually.
            self.command = "GET"
            self.path = path
            self.request_version = "HTTP/1.1"
            self.headers = {}

        def send_response(self, code, message=None):
            self._response_status = code

        def send_header(self, key, value):
            self._response_headers.append((key, value))

        def end_headers(self):
            pass

    handler = _CapturingHandler(_MockRequest(), ("127.0.0.1", 0), None)
    handler.do_GET()
    body = handler.wfile.getvalue()
    return handler._response_status, json.loads(body) if body else {}


def test_ready_route_returns_503_when_not_ready():
    main = _import_main_fresh()
    main._READY = False
    main._READY_REASON = "smoke_probe_failed"
    main._READY_CHECK_RESULTS = {
        "dump_sf_query": {"ok": False, "detail": "auth failed"}
    }
    status, payload = _invoke_handler_get(main, "/ready")
    assert status == 503
    assert payload["ready"] is False
    assert payload["reason"] == "smoke_probe_failed"
    assert "dump_sf_query" in payload["check_results"]


def test_ready_route_returns_200_when_ready():
    main = _import_main_fresh()
    main._READY = True
    main._READY_REASON = "smoke_probe_passed"
    main._READY_CHECK_RESULTS = {}
    status, payload = _invoke_handler_get(main, "/ready")
    assert status == 200
    assert payload["ready"] is True


def test_health_route_remains_200_when_not_ready():
    """``/health`` must stay 200 even when the smoke probe failed —
    Railway uses ``/ready`` for the gate, ``/health`` is liveness only."""
    main = _import_main_fresh()
    main._READY = False
    status, payload = _invoke_handler_get(main, "/health")
    assert status == 200
    assert payload["status"] == "ok"


def test_unknown_route_returns_404():
    main = _import_main_fresh()
    status, _payload = _invoke_handler_get(main, "/unknown")
    assert status == 404


# ──────────────────────────────────────────────────────────────────────────
# _run_pre_deploy_smoke_probe — direct tests
# ──────────────────────────────────────────────────────────────────────────


def test_pre_deploy_probe_swallows_probe_import_failure(caplog):
    main = _import_main_fresh()
    # Inject a busted smoke_probe so the import inside
    # _run_pre_deploy_smoke_probe fails.
    sys.modules["smoke_probe"] = None  # type: ignore[assignment]
    os.environ["SMOKE_PROBE_ENABLED"] = "true"
    with caplog.at_level(logging.ERROR, logger="orchestrator"):
        result = main._run_pre_deploy_smoke_probe()
    sys.modules.pop("smoke_probe", None)
    assert result is False
    assert main._READY is False
    assert any("smoke_probe import failed" in r.message for r in caplog.records)


def test_pre_deploy_probe_swallows_probe_exception(caplog):
    main = _import_main_fresh()
    import smoke_probe

    os.environ["SMOKE_PROBE_ENABLED"] = "true"
    with patch.object(smoke_probe, "run_smoke_probe", side_effect=RuntimeError("boom")):
        with caplog.at_level(logging.ERROR, logger="orchestrator"):
            result = main._run_pre_deploy_smoke_probe()
    assert result is False
    assert main._READY is False
    assert main._READY_REASON == "smoke_probe_failed"
