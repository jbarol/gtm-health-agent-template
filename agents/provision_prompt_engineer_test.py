"""Tests for ``agents/provision_prompt_engineer.py`` (Plan #44 Task #1).

The provisioner is a thin shim that calls ``client.beta.agents.create``
with the prompt sourced from ``update_prompts.PROMPTS['prompt_engineer']``.
The provisioner ALWAYS creates a new agent — it does not detect or skip
existing ones (intentional; the mint-and-paste workflow is documented
in the docstring and mirrored from provision_writing_agent.py).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO_ROOT / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))


def test_provisioner_mints_new_agent_with_correct_model(monkeypatch, capsys):
    """Smoke test: provisioner calls agents.create with sonnet model + prompt."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    import provision_prompt_engineer as ppe  # type: ignore
    import update_prompts as up  # type: ignore

    fake_agent = SimpleNamespace(id="agent_freshly_minted", version=1)
    fake_client = MagicMock()
    fake_client.beta.agents.create.return_value = fake_agent

    monkeypatch.setattr(ppe.anthropic, "Anthropic", lambda api_key: fake_client)
    # Ensure the prompt is populated; setup_agents-style import side
    # effects already do this at module load.
    assert up.PROMPTS.get("prompt_engineer")

    ppe.main()
    out = capsys.readouterr().out
    assert "PROMPT_ENGINEER_ID=agent_freshly_minted" in out
    assert fake_client.beta.agents.create.call_count == 1
    kwargs = fake_client.beta.agents.create.call_args.kwargs
    assert kwargs["model"] == "claude-sonnet-4-6"
    assert kwargs["system"] == up.PROMPTS["prompt_engineer"]


def test_provisioner_exits_when_api_key_missing(monkeypatch):
    """Without ANTHROPIC_API_KEY the script exits with a clear error."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    import provision_prompt_engineer as ppe  # type: ignore

    with pytest.raises(SystemExit) as exc:
        ppe.main()
    assert "ANTHROPIC_API_KEY" in str(exc.value)


def test_provisioner_exits_when_prompt_missing(monkeypatch):
    """If PROMPTS['prompt_engineer'] is somehow empty, fail loud."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    import provision_prompt_engineer as ppe  # type: ignore
    import update_prompts as up  # type: ignore

    # Patch the PROMPTS dict the provisioner reads.
    monkeypatch.setitem(up.PROMPTS, "prompt_engineer", "")
    monkeypatch.setattr(ppe.anthropic, "Anthropic", lambda api_key: MagicMock())

    with pytest.raises(SystemExit) as exc:
        ppe.main()
    assert "PROMPTS['prompt_engineer']" in str(exc.value)
