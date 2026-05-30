"""Tests for ``scripts/upsert_slack_channel_canvas.py``.

The script wraps Slack's ``conversations.canvases.create`` and
``canvases.edit`` APIs. These tests mock the ``slack_sdk.WebClient`` so
the suite runs offline. The first test exercises ``--dry-run`` and
asserts the exact API method names that would be invoked (per the PR's
acceptance criteria).

Run:
    cd orchestrator && python3 -m pytest upsert_slack_channel_canvas_test.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Make scripts/ importable as a flat module path. The script lives at
# repo_root/scripts/upsert_slack_channel_canvas.py.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


@pytest.fixture
def script_module(tmp_path, monkeypatch):
    """Import the script fresh with isolated paths for content + state."""

    sys.modules.pop("upsert_slack_channel_canvas", None)
    import upsert_slack_channel_canvas as mod  # type: ignore[import-not-found]

    # Redirect file paths into a tmp directory so the test never touches
    # repo state. Content file gets fixture markdown; portco config gets
    # a minimal active-only fixture.
    content_file = tmp_path / "channel-canvas.md"
    content_file.write_text("# Test canvas\n\nHello world.\n")

    portco_file = tmp_path / "portco_config.json"
    portco_file.write_text(
        json.dumps(
            {
                "portcos": {
                    "acme": {
                        "name": "Acme",
                        "status": "active",
                        "slack_channel": "C0000000000",
                    },
                    "pending_portco": {
                        "name": "Pending",
                        "status": "pending_crm",
                        "slack_channel": None,
                    },
                    "active_no_channel": {
                        "name": "Active No Channel",
                        "status": "active",
                        "slack_channel": None,
                    },
                }
            }
        )
    )

    state_dir = tmp_path / ".canvases"

    monkeypatch.setattr(mod, "CONTENT_FILE", content_file)
    monkeypatch.setattr(mod, "PORTCO_CONFIG", portco_file)
    monkeypatch.setattr(mod, "STATE_DIR", state_dir)
    monkeypatch.setattr(mod, "STATE_FILE", state_dir / "state.json")

    return mod


def test_load_portco_channels_filters_to_active_with_channel(script_module):
    """Only active portcos with a non-null slack_channel are returned."""
    channels = script_module.load_portco_channels()
    assert channels == [("acme", "C0000000000")]


def test_build_document_content_returns_markdown_payload(script_module):
    """The Slack canvas payload uses ``type: markdown`` directly."""
    payload = script_module.build_document_content("# Title\n\nbody")
    assert payload == {"type": "markdown", "markdown": "# Title\n\nbody"}


def test_canvas_state_roundtrip(script_module, tmp_path):
    """State writes and reads cleanly to ``.canvases/state.json``."""
    state = script_module.CanvasState()
    state.set("C111", "F_aaa")
    state.set("C222", "F_bbb")
    state.save()

    reloaded = script_module.CanvasState.load()
    assert reloaded.get("C111") == "F_aaa"
    assert reloaded.get("C222") == "F_bbb"


def test_dry_run_invokes_create_method_name_and_skips_slack(
    script_module, monkeypatch, caplog
):
    """--dry-run logs the conversations.canvases.create payload and never
    calls the Slack client.

    Acceptance criterion from the PR: the test must assert the exact API
    method names that would be invoked. We mock the WebClient so any
    accidental call raises, and we check that the dry-run path mentions
    ``conversations.canvases.create`` in its log output.
    """
    import logging

    caplog.set_level(logging.INFO, logger="upsert_slack_channel_canvas")

    fake_client = MagicMock(name="WebClient")
    # Both methods on the Slack client should NEVER be called in --dry-run.
    fake_client.conversations_canvases_create.side_effect = AssertionError(
        "create called in dry-run"
    )
    fake_client.canvases_edit.side_effect = AssertionError("edit called in dry-run")
    fake_client.conversations_info.side_effect = AssertionError(
        "conversations.info called in dry-run"
    )

    with patch.object(script_module, "WebClient", return_value=fake_client):
        results = script_module.run(dry_run=True)

    assert len(results) == 1
    assert results[0]["portco"] == "acme"
    assert results[0]["channel"] == "C0000000000"
    assert results[0]["action"] == "created"
    assert results[0]["ok"] is True

    # The dry-run log must reference the create method name (no existing
    # state file => create path).
    log_text = "\n".join(r.message for r in caplog.records)
    assert "conversations.canvases.create" in log_text


def test_dry_run_with_existing_state_uses_edit_method_name(
    script_module, monkeypatch, caplog
):
    """When state.json already maps the channel to a canvas_id, dry-run
    prints the ``canvases.edit`` payload instead of create."""
    import logging

    # Pre-seed state so the channel is "known."
    state = script_module.CanvasState()
    state.set("C0000000000", "F_existing_canvas")
    state.save()

    caplog.set_level(logging.INFO, logger="upsert_slack_channel_canvas")

    fake_client = MagicMock(name="WebClient")
    fake_client.conversations_canvases_create.side_effect = AssertionError(
        "create called in dry-run"
    )
    fake_client.canvases_edit.side_effect = AssertionError("edit called in dry-run")

    with patch.object(script_module, "WebClient", return_value=fake_client):
        results = script_module.run(dry_run=True)

    assert results[0]["action"] == "edited"
    assert results[0]["canvas_id"] == "F_existing_canvas"

    log_text = "\n".join(r.message for r in caplog.records)
    assert "canvases.edit" in log_text


def test_only_channel_filters(script_module):
    """``--channel`` limits to a single channel; mismatched ID is a no-op."""
    fake_client = MagicMock(name="WebClient")
    with patch.object(script_module, "WebClient", return_value=fake_client):
        results = script_module.run(only_channel="C_NOPE", dry_run=True)
    assert results == []


def test_live_create_calls_slack_client(script_module, monkeypatch):
    """When not in dry-run, the create path invokes
    ``client.conversations_canvases_create`` (the slack_sdk method name
    that resolves to the ``conversations.canvases.create`` HTTP API)."""

    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")

    fake_client = MagicMock(name="WebClient")
    fake_client.conversations_info.return_value = {"channel": {"properties": {}}}
    fake_client.conversations_canvases_create.return_value = {
        "ok": True,
        "canvas_id": "F_new_canvas",
    }

    with patch.object(script_module, "WebClient", return_value=fake_client):
        results = script_module.run(dry_run=False)

    fake_client.conversations_canvases_create.assert_called_once()
    fake_client.canvases_edit.assert_not_called()
    assert results[0]["canvas_id"] == "F_new_canvas"
    assert results[0]["action"] == "created"


def test_live_edit_calls_slack_client(script_module, monkeypatch):
    """With existing state, the edit path invokes
    ``client.canvases_edit`` (the slack_sdk method name for the
    ``canvases.edit`` HTTP API) with a ``replace`` operation."""

    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")

    # Seed state with a known canvas_id.
    state = script_module.CanvasState()
    state.set("C0000000000", "F_existing")
    state.save()

    fake_client = MagicMock(name="WebClient")
    fake_client.canvases_edit.return_value = {"ok": True}

    with patch.object(script_module, "WebClient", return_value=fake_client):
        results = script_module.run(dry_run=False)

    fake_client.conversations_canvases_create.assert_not_called()
    fake_client.canvases_edit.assert_called_once()
    call_kwargs = fake_client.canvases_edit.call_args.kwargs
    assert call_kwargs["canvas_id"] == "F_existing"
    assert call_kwargs["changes"][0]["operation"] == "replace"
    assert call_kwargs["changes"][0]["document_content"]["type"] == "markdown"
    assert results[0]["action"] == "edited"


def test_lookup_adopts_existing_channel_canvas(script_module, monkeypatch):
    """If ``conversations.info`` reports an existing canvas, we adopt it
    (call ``canvases.edit``) instead of creating a duplicate."""

    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")

    fake_client = MagicMock(name="WebClient")
    fake_client.conversations_info.return_value = {
        "channel": {"properties": {"canvas": {"file_id": "F_preexisting"}}}
    }
    fake_client.canvases_edit.return_value = {"ok": True}

    with patch.object(script_module, "WebClient", return_value=fake_client):
        results = script_module.run(dry_run=False)

    fake_client.conversations_canvases_create.assert_not_called()
    fake_client.canvases_edit.assert_called_once()
    assert results[0]["canvas_id"] == "F_preexisting"


def test_live_run_without_token_raises(script_module, monkeypatch):
    """A live run with no ``SLACK_BOT_TOKEN`` env raises a clear error."""

    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="SLACK_BOT_TOKEN"):
        script_module.run(dry_run=False)


def test_main_returns_nonzero_on_failure(script_module, monkeypatch):
    """If any channel upsert fails, ``main`` exits 1."""

    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")

    from slack_sdk.errors import SlackApiError

    fake_response = MagicMock()
    fake_response.get.return_value = "channel_not_found"

    fake_client = MagicMock(name="WebClient")
    fake_client.conversations_info.return_value = {"channel": {"properties": {}}}
    fake_client.conversations_canvases_create.side_effect = SlackApiError(
        message="channel_not_found", response=fake_response
    )

    monkeypatch.setattr(sys, "argv", ["upsert_slack_channel_canvas.py"])

    with patch.object(script_module, "WebClient", return_value=fake_client):
        rc = script_module.main()

    assert rc == 1
