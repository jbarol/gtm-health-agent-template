"""Tests for ``bin/audit-mcp-toolsets.py`` (Plan #44 Task #3).

The script's filename has a hyphen, so we load it by path via
``importlib.util``. Tests mock the Anthropic SDK — no network.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "bin" / "audit-mcp-toolsets.py"


def _load_audit_module():
    """Load ``bin/audit-mcp-toolsets.py`` by path (hyphen in filename)."""
    for p in (REPO_ROOT / "agents", REPO_ROOT / "orchestrator"):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    spec = importlib.util.spec_from_file_location("audit_mcp_toolsets", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def audit_mod():
    return _load_audit_module()


def _make_client(agents_to_tools: dict[str, list]):
    """Build a MagicMock client whose retrieve() returns the supplied tools."""
    from update_prompts import AGENTS  # type: ignore

    def retrieve(agent_id, **_):
        for short_name, cfg in AGENTS.items():
            if cfg.get("id") == agent_id:
                tools = agents_to_tools.get(short_name, [])
                return SimpleNamespace(id=agent_id, tools=tools)
        raise RuntimeError(f"unknown id: {agent_id}")

    client = MagicMock()
    client.beta.agents.retrieve.side_effect = retrieve
    return client


def test_clean_state_returns_empty(audit_mod, monkeypatch):
    """When no agent carries an mcp_toolset, audit() returns [] and main() = 0."""
    from update_prompts import AGENTS  # type: ignore

    # Give every provisioned agent a clean tool list.
    clean_tools = {
        name: [
            {"type": "agent_toolset_20260401"},
            {"type": "custom", "name": "db_query"},
        ]
        for name, cfg in AGENTS.items()
        if cfg.get("id")
    }
    # Ensure prompt_engineer has an id so its presence is exercised.
    monkeypatch.setenv("PROMPT_ENGINEER_ID", "agent_pe_test")
    # Reload AGENTS so the new env value is picked up.
    import update_prompts

    update_prompts.AGENTS["prompt_engineer"]["id"] = "agent_pe_test"
    clean_tools["prompt_engineer"] = [{"type": "agent_toolset_20260401"}]

    client = _make_client(clean_tools)
    orphans = audit_mod.audit(client=client)
    assert orphans == []


def test_orphan_mcp_toolset_is_flagged(audit_mod, capsys):
    """An agent with an mcp_toolset entry is reported and exits 1."""
    from update_prompts import AGENTS  # type: ignore

    tools_per_agent: dict[str, list] = {}
    for name, cfg in AGENTS.items():
        if not cfg.get("id"):
            continue
        if name == "coordinator":
            tools_per_agent[name] = [
                {"type": "agent_toolset_20260401"},
                {"type": "mcp_toolset", "mcp_server_name": "salesforce"},
            ]
        else:
            tools_per_agent[name] = [{"type": "agent_toolset_20260401"}]

    client = _make_client(tools_per_agent)
    orphans = audit_mod.audit(client=client)
    assert len(orphans) == 1
    assert "coordinator" in orphans[0]
    assert "mcp_toolset" in orphans[0]
    assert "salesforce" in orphans[0]


def test_main_prints_next_steps_when_clean(audit_mod, capsys, monkeypatch):
    """A clean audit prints a 'Next steps' block (matches rollback-agent UX)."""
    from update_prompts import AGENTS  # type: ignore

    monkeypatch.setenv("PROMPT_ENGINEER_ID", "agent_pe_test")
    import update_prompts

    update_prompts.AGENTS["prompt_engineer"]["id"] = "agent_pe_test"
    clean_tools = {
        name: [{"type": "agent_toolset_20260401"}]
        for name, cfg in AGENTS.items()
        if cfg.get("id")
    }
    client = _make_client(clean_tools)

    monkeypatch.setattr(audit_mod, "_build_client", lambda: client)
    rc = audit_mod.main(["--verbose"])

    captured = capsys.readouterr().out
    assert rc == 0
    assert "Next steps" in captured
    assert "no orphan" in captured.lower()


def test_main_returns_one_on_orphan(audit_mod, monkeypatch):
    """main() exits non-zero when audit() returns at least one orphan."""
    from update_prompts import AGENTS  # type: ignore

    tools_per_agent: dict[str, list] = {}
    for name, cfg in AGENTS.items():
        if not cfg.get("id"):
            continue
        if name == "statistician":
            tools_per_agent[name] = [
                {"type": "mcp_toolset", "mcp_server_name": "salesforce"}
            ]
        else:
            tools_per_agent[name] = [{"type": "agent_toolset_20260401"}]

    client = _make_client(tools_per_agent)
    monkeypatch.setattr(audit_mod, "_build_client", lambda: client)
    rc = audit_mod.main([])
    assert rc == 1


def test_retrieve_failure_is_logged_not_silenced(audit_mod, monkeypatch, capsys):
    """When retrieve() raises for one agent, the rest of the audit continues."""
    from update_prompts import AGENTS  # type: ignore

    coord_id = AGENTS["coordinator"]["id"]

    def retrieve(agent_id, **_):
        if agent_id == coord_id:
            raise RuntimeError("503: backend down")
        return SimpleNamespace(id=agent_id, tools=[])

    client = MagicMock()
    client.beta.agents.retrieve.side_effect = retrieve
    orphans = audit_mod.audit(client=client)
    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "coordinator" in out
    # No mcp_toolset orphans (everyone else has tools=[])
    assert orphans == []
