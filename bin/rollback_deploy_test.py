"""Tests for ``bin/rollback-deploy.py`` (Plan #42 PR3).

The script's filename has a hyphen, so we load it by path via
``importlib.util`` — same pattern as ``bin/rollback_agent_test.py``.

Tests mock ``gh run download`` and the per-agent ``bin/rollback-agent.py``
subprocess invocation. No network, no git side effects, no Anthropic
calls.

What we cover (per Plan #42 PR3 test plan):

    1. Diff logic identifies the agents that changed between
       pre-deploy and current pin files.
    2. The per-agent rollback invocation calls ``bin/rollback-agent.py``
       once per changed agent with the right ``--to-version``.
    3. Exit code is 0 on clean rollback, 1 on partial, 2 on
       download/parse failure.
    4. Dry-run prints the plan but never invokes the rollback subprocess.
    5. Admin DM fires only on ``--apply`` (success or partial).
    6. Empty-diff is treated as a clean success — no rollback attempts.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "bin" / "rollback-deploy.py"


def _load_module():
    """Load ``bin/rollback-deploy.py`` by path (hyphen in filename)."""
    for p in (REPO_ROOT / "agents", REPO_ROOT / "orchestrator"):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))

    spec = importlib.util.spec_from_file_location("rollback_deploy", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod():
    return _load_module()


@pytest.fixture()
def pre_deploy_file(tmp_path):
    """Write a fake pre-deploy artifact and return its path."""
    p = tmp_path / "pre_deploy" / "active_versions.json"
    p.parent.mkdir(parents=True)
    p.write_text(
        json.dumps(
            {
                "coordinator": 36,
                "quick_answer": 13,
                "writing_agent": 3,
            },
            indent=2,
        )
    )
    return p


@pytest.fixture()
def current_pin_file(tmp_path):
    """Write the on-disk pin file with two agents bumped."""
    p = tmp_path / "active_versions.json"
    p.write_text(
        json.dumps(
            {
                "coordinator": 37,  # bumped → must roll back
                "quick_answer": 14,  # bumped → must roll back
                "writing_agent": 3,  # unchanged
            },
            indent=2,
        )
    )
    return p


def test_diff_identifies_changed_agents(mod):
    pre = {"coordinator": 36, "quick_answer": 13, "writing_agent": 3}
    cur = {"coordinator": 37, "quick_answer": 14, "writing_agent": 3}
    diffs = mod._compute_diff(pre, cur)
    assert diffs == [
        ("coordinator", 36, 37),
        ("quick_answer", 13, 14),
    ]


def test_diff_warns_on_added_and_removed_agents(mod, capsys):
    pre = {"coordinator": 36, "quick_answer": 13}  # no writing_agent
    cur = {"coordinator": 37, "writing_agent": 3}  # no quick_answer
    diffs = mod._compute_diff(pre, cur)
    assert diffs == [("coordinator", 36, 37)]
    out = capsys.readouterr().out
    assert "writing_agent" in out and "added by this deploy" in out
    assert "quick_answer" in out and "removed by this deploy" in out


def test_dry_run_invokes_no_rollback_subprocess(
    mod, pre_deploy_file, current_pin_file, tmp_path
):
    """Dry-run prints plan but never runs ``bin/rollback-agent.py``."""
    invoked: list[tuple[str, int]] = []

    def fake_rollback(name, target, *, apply, runner=None):
        invoked.append((name, target))
        return True, f"would roll back to v{target}"

    rc = mod.rollback_deploy(
        "7891234567",
        apply=False,
        pin_path=current_pin_file,
        download_fn=lambda run_id, wd: pre_deploy_file,
        rollback_fn=fake_rollback,
        dm_fn=MagicMock(),
    )

    assert rc == 0
    # Dry-run: rollback_fn is still consulted (so the test can verify
    # the per-agent call shape), but ``apply=False`` is threaded so the
    # real script never mutates Anthropic.
    assert invoked == [("coordinator", 36), ("quick_answer", 13)]


def test_apply_invokes_rollback_agent_per_changed_agent(
    mod, pre_deploy_file, current_pin_file
):
    """``--apply`` triggers one ``rollback-agent.py`` call per diff."""
    invoked: list[tuple[str, int, bool]] = []

    def fake_rollback(name, target, *, apply, runner=None):
        invoked.append((name, target, apply))
        return True, f"rolled back to v{target}"

    dm = MagicMock()
    rc = mod.rollback_deploy(
        "7891234567",
        apply=True,
        pin_path=current_pin_file,
        download_fn=lambda run_id, wd: pre_deploy_file,
        rollback_fn=fake_rollback,
        dm_fn=dm,
    )

    assert rc == 0
    assert invoked == [
        ("coordinator", 36, True),
        ("quick_answer", 13, True),
    ]
    # Admin DM fires on success.
    dm.assert_called_once()
    dm_text = dm.call_args.args[0]
    assert "APPLIED" in dm_text
    assert "coordinator" in dm_text and "v37 -> v36" in dm_text
    assert "quick_answer" in dm_text and "v14 -> v13" in dm_text


def test_partial_failure_returns_exit_1(mod, pre_deploy_file, current_pin_file):
    """If one agent fails to roll back, exit code is 1 (partial)."""

    def fake_rollback(name, target, *, apply, runner=None):
        if name == "quick_answer":
            return False, "simulated SDK 500"
        return True, f"rolled back to v{target}"

    dm = MagicMock()
    rc = mod.rollback_deploy(
        "7891234567",
        apply=True,
        pin_path=current_pin_file,
        download_fn=lambda run_id, wd: pre_deploy_file,
        rollback_fn=fake_rollback,
        dm_fn=dm,
    )

    assert rc == 1
    dm.assert_called_once()
    dm_text = dm.call_args.args[0]
    assert "[FAIL]" in dm_text and "quick_answer" in dm_text


def test_no_diff_is_clean_success(mod, tmp_path):
    """When pre == current, exit 0 with no rollback invocations."""
    pre = tmp_path / "pre_deploy" / "active_versions.json"
    pre.parent.mkdir(parents=True)
    pre.write_text(json.dumps({"coordinator": 37}))

    current = tmp_path / "active_versions.json"
    current.write_text(json.dumps({"coordinator": 37}))

    rollback_calls = []
    dm = MagicMock()

    def fake_rollback(name, target, *, apply, runner=None):
        rollback_calls.append((name, target))
        return True, ""

    rc = mod.rollback_deploy(
        "7891234567",
        apply=True,
        pin_path=current,
        download_fn=lambda run_id, wd: pre,
        rollback_fn=fake_rollback,
        dm_fn=dm,
    )

    assert rc == 0
    assert rollback_calls == []
    dm.assert_not_called()


def test_download_failure_returns_exit_2(mod, current_pin_file):
    """``gh run download`` failure short-circuits with exit 2."""

    def fake_download(run_id, dest):
        raise SystemExit(
            f"Failed to download artifact from run {run_id}: simulated 404"
        )

    dm = MagicMock()
    rc = mod.rollback_deploy(
        "7891234567",
        apply=True,
        pin_path=current_pin_file,
        download_fn=fake_download,
        rollback_fn=MagicMock(),
        dm_fn=dm,
    )

    assert rc == 2
    dm.assert_not_called()


def test_parse_failure_returns_exit_2(mod, tmp_path, current_pin_file):
    """Malformed pre-deploy JSON short-circuits with exit 2."""
    bad = tmp_path / "pre_deploy" / "active_versions.json"
    bad.parent.mkdir(parents=True)
    bad.write_text("this is not json")

    rc = mod.rollback_deploy(
        "7891234567",
        apply=True,
        pin_path=current_pin_file,
        download_fn=lambda run_id, wd: bad,
        rollback_fn=MagicMock(),
        dm_fn=MagicMock(),
    )

    assert rc == 2


def test_dry_run_does_not_send_dm(mod, pre_deploy_file, current_pin_file):
    """DM only fires on ``--apply`` to avoid Slack spam from dry-runs."""

    def fake_rollback(name, target, *, apply, runner=None):
        return True, "ok"

    dm = MagicMock()
    rc = mod.rollback_deploy(
        "7891234567",
        apply=False,
        pin_path=current_pin_file,
        download_fn=lambda run_id, wd: pre_deploy_file,
        rollback_fn=fake_rollback,
        dm_fn=dm,
    )
    assert rc == 0
    dm.assert_not_called()


def test_invoke_rollback_agent_real_subprocess_failure(mod, monkeypatch):
    """``_invoke_rollback_agent`` returns ``(False, ...)`` on subprocess error."""

    def fake_run(cmd, check, cwd):
        raise subprocess.CalledProcessError(returncode=2, cmd=cmd, output="boom")

    ok, msg = mod._invoke_rollback_agent("coordinator", 26, apply=True, runner=fake_run)
    assert ok is False
    assert "exit 2" in msg


def test_invoke_rollback_agent_dry_run_no_subprocess(mod):
    """Dry-run path never calls the subprocess runner."""
    calls = []

    def fake_run(cmd, check, cwd):
        calls.append(cmd)

    ok, msg = mod._invoke_rollback_agent(
        "coordinator", 26, apply=False, runner=fake_run
    )
    assert ok is True
    assert "would invoke" in msg
    assert calls == []


def test_missing_pin_file_returns_exit_2(mod, tmp_path):
    """When ``agents/active_versions.json`` is missing, exit 2."""
    rc = mod.rollback_deploy(
        "7891234567",
        apply=True,
        pin_path=tmp_path / "does-not-exist.json",
        download_fn=lambda run_id, wd: tmp_path / "pre.json",
        rollback_fn=MagicMock(),
        dm_fn=MagicMock(),
    )
    assert rc == 2


def test_summary_includes_recovery_time_for_apply(
    mod, pre_deploy_file, current_pin_file, capsys
):
    """The Next-steps block reports per-agent and total recovery time."""

    def fake_rollback(name, target, *, apply, runner=None):
        return True, f"rolled back to v{target}"

    rc = mod.rollback_deploy(
        "7891234567",
        apply=True,
        pin_path=current_pin_file,
        download_fn=lambda run_id, wd: pre_deploy_file,
        rollback_fn=fake_rollback,
        dm_fn=MagicMock(),
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Total recovery time" in out
    assert "Per-agent average" in out
    assert "Runbook: docs/runbook-prompt-rollback.md" in out
