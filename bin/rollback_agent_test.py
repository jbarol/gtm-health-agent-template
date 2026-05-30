"""Tests for ``bin/rollback-agent.py`` (Plan #41 — D2).

The script's filename has a hyphen, so we load it by path via
``importlib.util``. Tests mock the Anthropic SDK, slack_bot, and the
``gh``/``git`` subprocess calls — no network or git side effects.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "bin" / "rollback-agent.py"


def _load_rollback_module():
    """Load ``bin/rollback-agent.py`` by path (hyphen in filename)."""
    # Make agents/ importable so the script's top-level imports work.
    for p in (REPO_ROOT / "agents", REPO_ROOT / "orchestrator"):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))

    spec = importlib.util.spec_from_file_location("rollback_agent", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def rollback_mod():
    return _load_rollback_module()


@pytest.fixture()
def isolated_pin_file(tmp_path, monkeypatch):
    """Redirect update_prompts.ACTIVE_VERSIONS_PATH to a temp file.

    Each test gets a clean pin file pre-loaded with the same fixture
    state — coordinator at v27 (the bootstrap baseline from this PR).
    """
    import update_prompts  # type: ignore

    pin = tmp_path / "active_versions.json"
    pin.write_text(json.dumps({"coordinator": 27, "quick_answer": 11}, indent=2))
    monkeypatch.setattr(update_prompts, "ACTIVE_VERSIONS_PATH", pin)
    return pin


def _make_client(*, current_version: int, target_version_known: bool = True):
    """Build a MagicMock client that mimics the Anthropic SDK shape."""
    client = MagicMock(name="anthropic.Anthropic")
    current = SimpleNamespace(
        id="agent_EXAMPLE_coordinator",
        version=current_version,
        model=SimpleNamespace(id="claude-opus-4-8"),
        system="<current system prompt>",
    )
    target = SimpleNamespace(
        id="agent_EXAMPLE_coordinator",
        version=26,
        model=SimpleNamespace(id="claude-opus-4-8"),
        system="<target system prompt v26>",
    )
    updated = SimpleNamespace(
        id="agent_EXAMPLE_coordinator",
        version=current_version + 1,
        model=SimpleNamespace(id="claude-opus-4-8"),
        system="<target system prompt v26>",
    )

    def retrieve(agent_id, version=None, **kwargs):
        if version is None:
            return current
        if not target_version_known:
            raise RuntimeError("404: version not found")
        return target

    client.beta.agents.retrieve.side_effect = retrieve
    client.beta.agents.update.return_value = updated
    return client


def test_successful_rollback_writes_pin_and_calls_sdk(rollback_mod, isolated_pin_file):
    """Happy path: rollback updates Anthropic AND writes pin AND opens PR + DM."""
    client = _make_client(current_version=27)

    with (
        patch.object(rollback_mod, "_open_pr") as mock_pr,
        patch.object(rollback_mod, "_send_dm_safe") as mock_dm,
    ):
        new_version = rollback_mod.rollback(
            "coordinator",
            26,
            open_pr=True,
            send_dm=True,
            client=client,
        )

    # SDK called correctly: retrieve current, retrieve target, then update.
    assert client.beta.agents.retrieve.call_count == 2
    client.beta.agents.update.assert_called_once()
    call_kwargs = client.beta.agents.update.call_args.kwargs
    assert call_kwargs["version"] == 27, "optimistic-lock on current version"
    assert call_kwargs["system"] == "<target system prompt v26>"
    assert call_kwargs["model"] == "claude-opus-4-8"

    # Pin file updated.
    pins = json.loads(isolated_pin_file.read_text())
    assert pins["coordinator"] == 28, "new active version = v_new (current + 1)"
    assert pins["quick_answer"] == 11, "other agents untouched"
    assert new_version == 28

    # Side effects fired.
    mock_pr.assert_called_once_with("coordinator", 28, 26)
    mock_dm.assert_called_once()
    dm_text = mock_dm.call_args.args[0]
    assert "coordinator" in dm_text and "v26" in dm_text and "v28" in dm_text


def test_same_version_rollback_is_noop_with_friendly_error(
    rollback_mod, isolated_pin_file
):
    """``--to-version`` matches current = SystemExit with a friendly message."""
    client = _make_client(current_version=27)

    with pytest.raises(SystemExit) as exc_info:
        rollback_mod.rollback(
            "coordinator", 27, open_pr=False, send_dm=False, client=client
        )

    msg = str(exc_info.value)
    assert "already at version 27" in msg
    assert "different --to-version" in msg

    # No SDK update call, pin file untouched.
    client.beta.agents.update.assert_not_called()
    pins = json.loads(isolated_pin_file.read_text())
    assert pins["coordinator"] == 27


def test_unknown_version_returns_nonzero_with_clear_message(
    rollback_mod, isolated_pin_file
):
    """A version the server doesn't know about aborts with a clear message."""
    client = _make_client(current_version=27, target_version_known=False)

    with pytest.raises(SystemExit) as exc_info:
        rollback_mod.rollback(
            "coordinator", 999, open_pr=False, send_dm=False, client=client
        )

    msg = str(exc_info.value)
    assert "Unknown version" in msg
    assert "--to-version 999" in msg
    assert "coordinator" in msg

    client.beta.agents.update.assert_not_called()


def test_unknown_agent_short_name_aborts(rollback_mod, isolated_pin_file):
    """Unknown agent name lists the known ones."""
    client = _make_client(current_version=27)

    with pytest.raises(SystemExit) as exc_info:
        rollback_mod.rollback(
            "not_a_real_agent",
            26,
            open_pr=False,
            send_dm=False,
            client=client,
        )

    msg = str(exc_info.value)
    assert "Unknown agent 'not_a_real_agent'" in msg
    assert "coordinator" in msg, "lists known agents to help the operator"


def test_open_pr_calls_git_and_gh(rollback_mod, monkeypatch):
    """``_open_pr`` shells out to git + gh in order; failure of one stops chain."""
    calls = []

    def fake_run(cmd, check, cwd):
        calls.append(cmd)
        return MagicMock(returncode=0)

    monkeypatch.setattr(rollback_mod.subprocess, "run", fake_run)
    rollback_mod._open_pr("coordinator", 28, 26)

    # Five subprocess invocations in order: checkout, add, commit, push, gh.
    assert len(calls) == 5
    assert calls[0][:3] == ["git", "checkout", "-b"]
    assert "rollback/coordinator-to-v26" in calls[0]
    assert calls[1][:2] == ["git", "add"]
    assert calls[2][:2] == ["git", "commit"]
    assert calls[3][:2] == ["git", "push"]
    assert calls[4][:3] == ["gh", "pr", "create"]


def test_send_dm_safe_no_admins_skips_gracefully(rollback_mod, monkeypatch):
    """When no admins are configured, function returns silently (no raise)."""
    fake_slack_bot = MagicMock()
    fake_cost_digest = MagicMock()
    fake_cost_digest._resolve_admin_ids = MagicMock(return_value=[])

    monkeypatch.setitem(sys.modules, "slack_bot", fake_slack_bot)
    monkeypatch.setitem(sys.modules, "cost_digest", fake_cost_digest)

    rollback_mod._send_dm_safe("test message")  # must not raise
    fake_slack_bot.send_dm.assert_not_called()


def test_send_dm_safe_send_failure_does_not_propagate(rollback_mod, monkeypatch):
    """If Slack send raises, function must catch and continue."""
    fake_slack_bot = MagicMock()
    fake_slack_bot.send_dm.side_effect = RuntimeError("slack down")
    fake_cost_digest = MagicMock()
    fake_cost_digest._resolve_admin_ids = MagicMock(return_value=["U123"])

    monkeypatch.setitem(sys.modules, "slack_bot", fake_slack_bot)
    monkeypatch.setitem(sys.modules, "cost_digest", fake_cost_digest)

    rollback_mod._send_dm_safe("test")  # must not raise
    fake_slack_bot.send_dm.assert_called_once_with("U123", "test")


def test_no_pr_and_no_dm_flags_skip_side_effects(rollback_mod, isolated_pin_file):
    """``open_pr=False`` and ``send_dm=False`` skip those steps."""
    client = _make_client(current_version=27)

    with (
        patch.object(rollback_mod, "_open_pr") as mock_pr,
        patch.object(rollback_mod, "_send_dm_safe") as mock_dm,
    ):
        rollback_mod.rollback(
            "coordinator",
            26,
            open_pr=False,
            send_dm=False,
            client=client,
        )

    mock_pr.assert_not_called()
    mock_dm.assert_not_called()
    # SDK update still happened — flags only suppress side effects.
    client.beta.agents.update.assert_called_once()
