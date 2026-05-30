"""Tests for ``agents/provision_watcher_agent.py``.

The provision script's contract:
    1. Always builds the same agent spec deterministically.
    2. The 4-tool surface is exactly the 4 watcher_* tools — no more,
       no less. This is what enforces the design's "tight 4-tool
       allowlist by construction" promise.
    3. ``--dry-run`` prints without calling the API.
    4. ``--rotate`` is a no-op (back-compat with the original design's
       PAT-rotation runbook).
    5. The actual ``client.beta.agents.create`` call uses the built spec.

No live API calls — anthropic.Anthropic is patched throughout.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO_ROOT / "agents"
sys.path.insert(0, str(AGENTS_DIR))

for _key, _value in {
    "ANTHROPIC_API_KEY": "sk-ant-test",
}.items():
    os.environ.setdefault(_key, _value)


import provision_watcher_agent as pwa  # noqa: E402


# ───────────────────────────────────────────────────────────────────────
# Spec contract
# ───────────────────────────────────────────────────────────────────────


def test_spec_includes_exactly_four_watcher_tools():
    spec = pwa._build_spec()
    names = sorted(t["name"] for t in spec["tools"])
    assert names == sorted(
        [
            "watcher_create_branch",
            "watcher_write_file",
            "watcher_create_pr",
            "watcher_add_comment",
        ]
    )
    assert len(spec["tools"]) == 4


def test_spec_uses_opus_48_for_diagnosis():
    spec = pwa._build_spec()
    assert spec["model"] == "claude-opus-4-8"


def test_spec_system_prompt_mentions_diagnose_only_mode():
    """The diagnose-only mode contract must be in the prompt — otherwise
    the agent will try to fix things outside the allowlist."""
    spec = pwa._build_spec()
    assert "diagnose-only" in spec["system"].lower()
    assert (
        "diagnose-only-mode" in spec["system"].lower()
        or "diagnose-only mode" in spec["system"].lower()
    )


def test_spec_system_prompt_lists_blocked_paths():
    """The agent must know the allowlist BEFORE attempting writes."""
    spec = pwa._build_spec()
    sys_prompt = spec["system"]
    for blocked in (
        "agents/*.py",
        ".github/workflows",
        "Dockerfile",
        "railway.toml",
        ".env",
    ):
        assert blocked in sys_prompt


def test_spec_system_prompt_includes_branch_prefix_rule():
    spec = pwa._build_spec()
    assert "watcher/<inv_id>-" in spec["system"]


def test_spec_no_mcp_servers():
    """Design deviation: this PR uses custom tools, not GH MCP. Verify
    that the spec doesn't accidentally smuggle in an mcp_servers key
    that would conflict with the orchestrator's dispatch contract."""
    spec = pwa._build_spec()
    assert "mcp_servers" not in spec


# ───────────────────────────────────────────────────────────────────────
# CLI flags
# ───────────────────────────────────────────────────────────────────────


def test_dry_run_does_not_call_anthropic(capsys):
    with patch("anthropic.Anthropic") as fake:
        rc = pwa.main(["--dry-run"])
    assert rc == 0
    fake.assert_not_called()
    out = capsys.readouterr().out
    assert "watcher_create_branch" in out
    assert "watcher_write_file" in out


def test_rotate_is_noop(capsys):
    with patch("anthropic.Anthropic") as fake:
        rc = pwa.main(["--rotate"])
    assert rc == 0
    fake.assert_not_called()
    out = capsys.readouterr().out
    assert "no-op" in out.lower()


def test_provision_calls_create_with_spec(capsys):
    """Live provisioning path — patched Anthropic client."""
    fake_agent = MagicMock(id="agent_test_watcher_id")
    fake_client = MagicMock()
    fake_client.beta.agents.create.return_value = fake_agent
    with patch("anthropic.Anthropic", return_value=fake_client):
        rc = pwa.main([])
    assert rc == 0
    fake_client.beta.agents.create.assert_called_once()
    kwargs = fake_client.beta.agents.create.call_args.kwargs
    assert kwargs["name"].startswith("GTM Health Agent")
    assert kwargs["model"] == "claude-opus-4-8"
    names = sorted(t["name"] for t in kwargs["tools"])
    assert names == sorted(
        [
            "watcher_create_branch",
            "watcher_write_file",
            "watcher_create_pr",
            "watcher_add_comment",
        ]
    )
    out = capsys.readouterr().out
    assert "WATCHER_AGENT_ID=agent_test_watcher_id" in out
    # Operator runbook must remind to leave WATCHER_ENABLED=false on Day 1
    assert "WATCHER_ENABLED=false" in out


def test_provision_handles_missing_sdk():
    """If anthropic isn't installed, exit 2 with a clear message."""
    with patch.dict(sys.modules, {"anthropic": None}):
        # Force the import to fail by removing it from sys.modules and
        # patching __import__ to raise.
        real_import = (
            __builtins__["__import__"]
            if isinstance(__builtins__, dict)
            else __builtins__.__import__
        )

        def _failing_import(name, *args, **kwargs):
            if name == "anthropic":
                raise ImportError("no module")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=_failing_import):
            rc = pwa.main([])
        assert rc == 2
