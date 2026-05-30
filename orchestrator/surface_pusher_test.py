"""Tests for ``surface_pusher.push_to_canvas`` (Plan #33 F6).

Mock ``slack_sdk.WebClient`` plus the F1 DB helpers and the F4
compute layer so the suite runs offline. Mirrors the mocking pattern
established by ``upsert_slack_channel_canvas_test.py``.

Run:
    cd orchestrator && python3 -m pytest surface_pusher_test.py -q
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from slack_sdk.errors import SlackApiError

import surface_pusher
from surface_schemas import SurfaceState


# ---------- Fixtures ----------


@pytest.fixture
def fake_state():
    """A minimal SurfaceState — `compute_surface` is mocked to return this."""
    return SurfaceState(portco="acme", generated_at="2026-05-11T14:00:00")


@pytest.fixture
def fake_client():
    """A MagicMock Slack client with happy-path return values."""
    client = MagicMock(name="WebClient")
    client.conversations_canvases_create.return_value = {
        "ok": True,
        "canvas_id": "F_new_canvas",
    }
    client.canvases_edit.return_value = {"ok": True}
    return client


def _slack_api_error(code: str, retry_after: str | None = None) -> SlackApiError:
    """Build a SlackApiError whose .response.get("error") returns ``code``."""
    response = MagicMock(name="SlackResponse")
    response.get = lambda key, default=None: {"error": code}.get(key, default)
    response.headers = {"Retry-After": retry_after} if retry_after else {}
    return SlackApiError(message=code, response=response)


@pytest.fixture(autouse=True)
def _reset_failure_state():
    """Clear surface_pusher's in-process failure dedup state before each test.

    The watch-notice deduper persists across calls inside a single
    process, which is exactly what we want at runtime but unhelpful
    across unrelated unit tests.
    """
    surface_pusher._reset_failure_state()
    yield
    surface_pusher._reset_failure_state()


# ---------- Happy paths ----------


def test_first_push_creates_canvas_and_persists_id(
    fake_state, fake_client, monkeypatch
):
    """No cached row → conversations.canvases.create is invoked, then
    db_adapter.upsert_surface_state is called with the new canvas_id."""
    monkeypatch.setattr(surface_pusher.db_adapter, "get_surface_state", lambda p: None)
    monkeypatch.setattr(
        surface_pusher.surface_compute, "compute_surface", lambda p: fake_state
    )
    monkeypatch.setattr(
        surface_pusher.surface_renderer, "render", lambda s: "# Surface body\n"
    )
    monkeypatch.setattr(
        surface_pusher.portco_registry,
        "get_portco_config",
        lambda p: {"slack_channel": "C123"},
    )
    upsert = MagicMock(name="upsert_surface_state", return_value=True)
    monkeypatch.setattr(surface_pusher.db_adapter, "upsert_surface_state", upsert)
    monkeypatch.setattr(surface_pusher, "_slack_client", lambda: fake_client)

    ok = surface_pusher.push_to_canvas("acme")

    assert ok is True
    fake_client.conversations_canvases_create.assert_called_once()
    fake_client.canvases_edit.assert_not_called()
    kwargs = fake_client.conversations_canvases_create.call_args.kwargs
    assert kwargs["channel_id"] == "C123"
    assert kwargs["document_content"] == {
        "type": "markdown",
        "markdown": "# Surface body\n",
    }
    upsert.assert_called_once()
    upsert_kwargs = upsert.call_args
    # Positional args: portco, state_json, rendered_md, canvas_id
    assert upsert_kwargs.args[0] == "acme"
    assert upsert_kwargs.args[2] == "# Surface body\n"
    assert upsert_kwargs.args[3] == "F_new_canvas"


def test_existing_canvas_id_calls_edit(fake_state, fake_client, monkeypatch):
    """canvas_id already cached → canvases.edit with operation=replace."""
    monkeypatch.setattr(
        surface_pusher.db_adapter,
        "get_surface_state",
        lambda p: {
            "rendered_md": "# Old body\n",
            "canvas_id": "F_existing",
            "state_json": {},
            "version": 5,
        },
    )
    monkeypatch.setattr(
        surface_pusher.surface_compute, "compute_surface", lambda p: fake_state
    )
    monkeypatch.setattr(
        surface_pusher.surface_renderer, "render", lambda s: "# New body\n"
    )
    monkeypatch.setattr(
        surface_pusher.portco_registry,
        "get_portco_config",
        lambda p: {"slack_channel": "C123"},
    )
    upsert = MagicMock(return_value=True)
    monkeypatch.setattr(surface_pusher.db_adapter, "upsert_surface_state", upsert)
    monkeypatch.setattr(surface_pusher, "_slack_client", lambda: fake_client)

    ok = surface_pusher.push_to_canvas("acme")

    assert ok is True
    fake_client.canvases_edit.assert_called_once()
    fake_client.conversations_canvases_create.assert_not_called()
    kwargs = fake_client.canvases_edit.call_args.kwargs
    assert kwargs["canvas_id"] == "F_existing"
    assert kwargs["changes"][0]["operation"] == "replace"
    assert kwargs["changes"][0]["document_content"]["markdown"] == "# New body\n"
    # The old canvas_id should be preserved through the upsert.
    assert upsert.call_args.args[3] == "F_existing"


def test_unchanged_markdown_is_a_noop(fake_state, fake_client, monkeypatch):
    """rendered_md byte-identical to cache → skip Slack and DB writes."""
    monkeypatch.setattr(
        surface_pusher.db_adapter,
        "get_surface_state",
        lambda p: {
            "rendered_md": "# Same body\n",
            "canvas_id": "F_existing",
            "state_json": {},
            "version": 1,
        },
    )
    monkeypatch.setattr(
        surface_pusher.surface_compute, "compute_surface", lambda p: fake_state
    )
    monkeypatch.setattr(
        surface_pusher.surface_renderer, "render", lambda s: "# Same body\n"
    )
    upsert = MagicMock()
    monkeypatch.setattr(surface_pusher.db_adapter, "upsert_surface_state", upsert)
    monkeypatch.setattr(surface_pusher, "_slack_client", lambda: fake_client)

    ok = surface_pusher.push_to_canvas("acme")

    assert ok is True
    fake_client.canvases_edit.assert_not_called()
    fake_client.conversations_canvases_create.assert_not_called()
    upsert.assert_not_called()


# ---------- Retry / failure paths ----------


def test_ratelimited_once_then_succeeds(fake_state, fake_client, monkeypatch):
    """Slack returns ``ratelimited`` once, then the retry succeeds.

    The pusher honors Retry-After, sleeps, retries, returns True.
    """
    monkeypatch.setattr(
        surface_pusher.db_adapter,
        "get_surface_state",
        lambda p: {
            "rendered_md": "# Old\n",
            "canvas_id": "F_existing",
            "state_json": {},
            "version": 1,
        },
    )
    monkeypatch.setattr(
        surface_pusher.surface_compute, "compute_surface", lambda p: fake_state
    )
    monkeypatch.setattr(surface_pusher.surface_renderer, "render", lambda s: "# New\n")
    monkeypatch.setattr(surface_pusher.db_adapter, "upsert_surface_state", MagicMock())
    monkeypatch.setattr(surface_pusher, "_slack_client", lambda: fake_client)
    sleep_mock = MagicMock()
    monkeypatch.setattr(surface_pusher.time, "sleep", sleep_mock)

    fake_client.canvases_edit.side_effect = [
        _slack_api_error("ratelimited", retry_after="2"),
        {"ok": True},
    ]

    ok = surface_pusher.push_to_canvas("acme")

    assert ok is True
    assert fake_client.canvases_edit.call_count == 2
    # Sleep was called with at least the Retry-After value (2s).
    sleep_mock.assert_called_once()
    assert sleep_mock.call_args.args[0] >= 2.0


def test_ratelimited_exhausts_retries_returns_false(
    fake_state, fake_client, monkeypatch
):
    """4 consecutive 429s exceed MAX_RETRIES → return False, no upsert,
    AND after 3 such failures in 60 min an admin watch DM fires (F11)."""
    monkeypatch.setattr(
        surface_pusher.db_adapter,
        "get_surface_state",
        lambda p: {
            "rendered_md": "# Last good\n",
            "canvas_id": "F_existing",
            "state_json": {},
            "version": 1,
        },
    )
    monkeypatch.setattr(
        surface_pusher.surface_compute, "compute_surface", lambda p: fake_state
    )
    monkeypatch.setattr(surface_pusher.surface_renderer, "render", lambda s: "# New\n")
    upsert = MagicMock()
    monkeypatch.setattr(surface_pusher.db_adapter, "upsert_surface_state", upsert)
    monkeypatch.setattr(surface_pusher, "_slack_client", lambda: fake_client)
    monkeypatch.setattr(surface_pusher.time, "sleep", MagicMock())

    # Admin DM plumbing: configure admins + capture send_dm calls.
    send_dm_mock = MagicMock(name="send_dm")
    import sys
    import types

    fake_slack_bot = types.ModuleType("slack_bot")
    fake_slack_bot.send_dm = send_dm_mock
    monkeypatch.setitem(sys.modules, "slack_bot", fake_slack_bot)
    monkeypatch.setattr("portco_registry.get_admin_user_ids", lambda: ["U_ADMIN"])

    fake_client.canvases_edit.side_effect = [
        _slack_api_error("ratelimited", retry_after="1") for _ in range(5)
    ]

    # First two failures: no admin DM yet (under threshold of 3).
    assert surface_pusher.push_to_canvas("acme") is False
    fake_client.canvases_edit.reset_mock()
    fake_client.canvases_edit.side_effect = [
        _slack_api_error("ratelimited", retry_after="1") for _ in range(5)
    ]
    assert surface_pusher.push_to_canvas("acme") is False
    assert send_dm_mock.call_count == 0  # only 2 failures so far → no DM

    # Third failure: threshold crossed → admin DM fires exactly once.
    fake_client.canvases_edit.reset_mock()
    fake_client.canvases_edit.side_effect = [
        _slack_api_error("ratelimited", retry_after="1") for _ in range(5)
    ]
    ok = surface_pusher.push_to_canvas("acme")

    assert ok is False
    # Initial call + MAX_RETRIES retries = MAX_RETRIES+1 total attempts.
    assert fake_client.canvases_edit.call_count == surface_pusher.MAX_RETRIES + 1
    upsert.assert_not_called()  # last-good rendered_md preserved in DB
    assert send_dm_mock.call_count == 1
    assert send_dm_mock.call_args.args[0] == "U_ADMIN"
    assert "acme" in send_dm_mock.call_args.args[1]
    assert "WATCH" in send_dm_mock.call_args.args[1]


def test_generic_exception_logs_failed_and_returns_false(
    fake_state, fake_client, monkeypatch, caplog
):
    """A non-ratelimited Slack error logs SURFACE_PUSH_FAILED and
    returns False without clearing the cached rendered_md."""
    import logging

    monkeypatch.setattr(
        surface_pusher.db_adapter,
        "get_surface_state",
        lambda p: {
            "rendered_md": "# Last good\n",
            "canvas_id": "F_existing",
            "state_json": {},
            "version": 1,
        },
    )
    monkeypatch.setattr(
        surface_pusher.surface_compute, "compute_surface", lambda p: fake_state
    )
    monkeypatch.setattr(surface_pusher.surface_renderer, "render", lambda s: "# New\n")
    upsert = MagicMock()
    monkeypatch.setattr(surface_pusher.db_adapter, "upsert_surface_state", upsert)
    monkeypatch.setattr(surface_pusher, "_slack_client", lambda: fake_client)

    fake_client.canvases_edit.side_effect = _slack_api_error("channel_not_found")

    caplog.set_level(logging.ERROR, logger="surface_pusher")
    ok = surface_pusher.push_to_canvas("acme")

    assert ok is False
    upsert.assert_not_called()
    log_text = "\n".join(r.message for r in caplog.records)
    assert "SURFACE_PUSH_FAILED" in log_text
    assert "acme" in log_text


def test_non_slack_exception_logs_failed_and_returns_false(
    fake_state, fake_client, monkeypatch, caplog
):
    """A bug in renderer or compute layer surfaces as SURFACE_PUSH_FAILED."""
    import logging

    monkeypatch.setattr(surface_pusher.db_adapter, "get_surface_state", lambda p: None)
    monkeypatch.setattr(
        surface_pusher.surface_compute, "compute_surface", lambda p: fake_state
    )

    def _broken_render(state):
        raise ValueError("renderer bug")

    monkeypatch.setattr(surface_pusher.surface_renderer, "render", _broken_render)
    upsert = MagicMock()
    monkeypatch.setattr(surface_pusher.db_adapter, "upsert_surface_state", upsert)
    monkeypatch.setattr(surface_pusher, "_slack_client", lambda: fake_client)

    caplog.set_level(logging.ERROR, logger="surface_pusher")
    ok = surface_pusher.push_to_canvas("acme")

    assert ok is False
    upsert.assert_not_called()
    log_text = "\n".join(r.message for r in caplog.records)
    assert "SURFACE_PUSH_FAILED" in log_text
    assert "renderer bug" in log_text


# ---------- F11: stale-edit drop ----------


def test_stale_edit_dropped_when_version_advanced(
    fake_state, fake_client, monkeypatch, caplog
):
    """Another writer bumped surface_state.version mid-flight → drop the
    push as stale. Return True (deliberate no-op, not an error), and
    never call canvases.edit / canvases.create / upsert_surface_state."""
    import logging

    # First call (start of push_to_canvas) returns version=5.
    # Second call (stale-edit guard right before push) returns version=6.
    versions = iter(
        [
            {
                "rendered_md": "# Old body\n",
                "canvas_id": "F_existing",
                "state_json": {},
                "version": 5,
            },
            {
                "rendered_md": "# Old body\n",
                "canvas_id": "F_existing",
                "state_json": {},
                "version": 6,  # advanced by another writer
            },
        ]
    )
    monkeypatch.setattr(
        surface_pusher.db_adapter, "get_surface_state", lambda p: next(versions)
    )
    monkeypatch.setattr(
        surface_pusher.surface_compute, "compute_surface", lambda p: fake_state
    )
    monkeypatch.setattr(
        surface_pusher.surface_renderer, "render", lambda s: "# New body\n"
    )
    upsert = MagicMock()
    monkeypatch.setattr(surface_pusher.db_adapter, "upsert_surface_state", upsert)
    monkeypatch.setattr(surface_pusher, "_slack_client", lambda: fake_client)

    caplog.set_level(logging.INFO, logger="surface_pusher")
    ok = surface_pusher.push_to_canvas("acme")

    assert ok is True
    fake_client.canvases_edit.assert_not_called()
    fake_client.conversations_canvases_create.assert_not_called()
    upsert.assert_not_called()
    log_text = "\n".join(r.message for r in caplog.records)
    assert "stale edit dropped" in log_text


# ---------- F11: daily-deduped admin watch DM ----------


def test_admin_dm_deduped_per_day(fake_state, fake_client, monkeypatch):
    """Once admins are DMed for a portco today, further failures the same
    day do NOT trigger another DM (per-UTC-day dedup). Exercise this by
    crossing the 3-failure threshold, then triggering a 4th failure and
    asserting send_dm was called exactly once."""
    monkeypatch.setattr(
        surface_pusher.db_adapter,
        "get_surface_state",
        lambda p: {
            "rendered_md": "# Last good\n",
            "canvas_id": "F_existing",
            "state_json": {},
            "version": 1,
        },
    )
    monkeypatch.setattr(
        surface_pusher.surface_compute, "compute_surface", lambda p: fake_state
    )
    monkeypatch.setattr(surface_pusher.surface_renderer, "render", lambda s: "# New\n")
    monkeypatch.setattr(surface_pusher.db_adapter, "upsert_surface_state", MagicMock())
    monkeypatch.setattr(surface_pusher, "_slack_client", lambda: fake_client)
    monkeypatch.setattr(surface_pusher.time, "sleep", MagicMock())

    send_dm_mock = MagicMock(name="send_dm")
    import sys
    import types

    fake_slack_bot = types.ModuleType("slack_bot")
    fake_slack_bot.send_dm = send_dm_mock
    monkeypatch.setitem(sys.modules, "slack_bot", fake_slack_bot)
    monkeypatch.setattr("portco_registry.get_admin_user_ids", lambda: ["U_ADMIN"])

    # Drive 4 failures back-to-back; each push retries 3x then gives up.
    for _ in range(4):
        fake_client.canvases_edit.side_effect = [
            _slack_api_error("ratelimited", retry_after="1") for _ in range(5)
        ]
        result = surface_pusher.push_to_canvas("acme")
        assert result is False

    # 3rd failure crosses the threshold; 4th is suppressed by per-day dedup.
    assert send_dm_mock.call_count == 1
    assert "acme" in send_dm_mock.call_args.args[1]
