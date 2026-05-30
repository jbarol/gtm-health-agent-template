"""Tests for the Plan #41 versioning surface in ``agents/update_prompts.py``.

Covers ``bootstrap_active_versions_file``, ``read_active_versions``,
and ``write_active_versions``. The original deploy ``main()`` is
unchanged in shape and integration-tested against live state during
the actual deploy workflow.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock


REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO_ROOT / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))


def test_write_and_read_active_versions_roundtrip(tmp_path, monkeypatch):
    """Write, then read — output equals input. Sorted-keys formatting."""
    import update_prompts as up  # type: ignore

    pin = tmp_path / "pins.json"
    monkeypatch.setattr(up, "ACTIVE_VERSIONS_PATH", pin)

    payload = {"coordinator": 27, "quick_answer": 11, "dream": 7}
    up.write_active_versions(payload)
    assert up.read_active_versions() == payload

    raw = pin.read_text()
    # Sorted-keys assertion: coordinator > dream > quick_answer alphabetically.
    keys_in_order = [
        k
        for k in payload.keys()
        if False  # placeholder
    ]
    # Parse + check json order via the raw text.
    parsed = json.loads(raw)
    sorted_keys = sorted(payload.keys())
    assert list(parsed.keys()) == sorted_keys


def test_read_active_versions_missing_file_returns_empty(tmp_path, monkeypatch):
    import update_prompts as up  # type: ignore

    monkeypatch.setattr(up, "ACTIVE_VERSIONS_PATH", tmp_path / "nope.json")
    assert up.read_active_versions() == {}


def test_bootstrap_when_file_exists_is_noop(tmp_path, monkeypatch):
    """Bootstrap must not overwrite an existing pin file."""
    import update_prompts as up  # type: ignore

    pin = tmp_path / "pins.json"
    existing = {"coordinator": 99}
    pin.write_text(json.dumps(existing))
    monkeypatch.setattr(up, "ACTIVE_VERSIONS_PATH", pin)

    fake_client = MagicMock()
    result = up.bootstrap_active_versions_file(client_obj=fake_client)
    assert result == existing
    fake_client.beta.agents.retrieve.assert_not_called()
    # File untouched.
    assert json.loads(pin.read_text()) == existing


def test_bootstrap_when_file_missing_reads_live_versions(tmp_path, monkeypatch):
    """No pin file = retrieve every agent and write a fresh one."""
    import update_prompts as up  # type: ignore

    pin = tmp_path / "pins.json"
    monkeypatch.setattr(up, "ACTIVE_VERSIONS_PATH", pin)
    assert not pin.exists()

    # Build a fake client that returns a unique version per agent ID.
    id_to_name = {cfg["id"]: name for name, cfg in up.AGENTS.items() if cfg.get("id")}
    fake_versions = {
        name: 100 + i for i, name in enumerate(sorted(id_to_name.values()))
    }

    def retrieve(agent_id, **_):
        return SimpleNamespace(id=agent_id, version=fake_versions[id_to_name[agent_id]])

    fake_client = MagicMock()
    fake_client.beta.agents.retrieve.side_effect = retrieve

    result = up.bootstrap_active_versions_file(client_obj=fake_client)
    assert result == fake_versions
    assert pin.exists()
    on_disk = json.loads(pin.read_text())
    assert on_disk == fake_versions


def test_bootstrap_skips_agents_with_empty_id(tmp_path, monkeypatch):
    """If an agent has an empty ID (env unset), it's omitted from the bootstrap."""
    import update_prompts as up  # type: ignore

    pin = tmp_path / "pins.json"
    monkeypatch.setattr(up, "ACTIVE_VERSIONS_PATH", pin)

    # Patch AGENTS to include one agent with no ID — the SDK must NOT
    # be called for it.
    patched_agents = {
        "coordinator": {"id": "agent_abc", "model": "claude-opus-4-8"},
        "ghost_agent": {"id": "", "model": "claude-haiku-4-5"},
    }
    monkeypatch.setattr(up, "AGENTS", patched_agents)

    fake_client = MagicMock()
    fake_client.beta.agents.retrieve.return_value = SimpleNamespace(
        id="agent_abc", version=42
    )

    result = up.bootstrap_active_versions_file(client_obj=fake_client)
    assert result == {"coordinator": 42}
    # Only one retrieve call (the agent with an empty ID was skipped).
    assert fake_client.beta.agents.retrieve.call_count == 1


def test_bootstrap_retrieve_failure_is_logged_not_raised(tmp_path, monkeypatch, capsys):
    """A retrieve() failure for one agent must not abort the bootstrap of others."""
    import update_prompts as up  # type: ignore

    pin = tmp_path / "pins.json"
    monkeypatch.setattr(up, "ACTIVE_VERSIONS_PATH", pin)
    patched_agents = {
        "coordinator": {"id": "agent_ok", "model": "claude-opus-4-8"},
        "dream": {"id": "agent_broken", "model": "claude-sonnet-4-6"},
    }
    monkeypatch.setattr(up, "AGENTS", patched_agents)

    def retrieve(agent_id, **_):
        if agent_id == "agent_broken":
            raise RuntimeError("503: gateway timeout")
        return SimpleNamespace(id=agent_id, version=42)

    fake_client = MagicMock()
    fake_client.beta.agents.retrieve.side_effect = retrieve

    result = up.bootstrap_active_versions_file(client_obj=fake_client)
    assert result == {"coordinator": 42}
    out = capsys.readouterr().out
    assert "[BOOTSTRAP-FAIL]" in out
    assert "agent_broken" in out


# ---------------------------------------------------------------------------
# Plan #44 Task #1 — Prompt Engineer in CI deploy path
# ---------------------------------------------------------------------------


def test_prompt_engineer_is_registered_in_agents():
    """The Prompt Engineer must appear in AGENTS so update_prompts.py deploys it."""
    import update_prompts as up  # type: ignore

    assert "prompt_engineer" in up.AGENTS, (
        "Plan #44 Task #1 — prompt_engineer must be in AGENTS so "
        ".github/workflows/deploy-prompts.yml pushes its system prompt "
        "on every merge. Without this, the live agent runs on whatever "
        "was manually pasted into the Anthropic Console."
    )
    cfg = up.AGENTS["prompt_engineer"]
    assert cfg["model"] == "claude-sonnet-4-6", (
        "Prompt Engineer must run on Sonnet 4.6 — the cheap pre-flight "
        "tier; Opus would defeat the cost-efficiency goal of "
        "preprocessing."
    )
    # ID may be empty in CI (env unset) — that's fine; the deploy loop
    # SKIPs missing IDs cleanly, same shape as WRITING_AGENT_ID pre-PR #75.


def test_prompt_engineer_prompt_is_loaded():
    """PROMPTS['prompt_engineer'] must be populated at import time (not lazily)."""
    import update_prompts as up  # type: ignore

    assert "prompt_engineer" in up.PROMPTS, (
        "PROMPTS['prompt_engineer'] must be defined at module-load time, "
        "not lazily loaded like writing_agent — the agent has no orchestrator/* "
        "source of truth to defer to."
    )
    prompt = up.PROMPTS["prompt_engineer"]
    assert len(prompt) > 500, "prompt should be substantive (≥500 chars)"
    # Key markers — the prompt should at minimum mention the JSON
    # output keys and the memory store path.
    assert "improved_prompt" in prompt
    assert "instructions.md" in prompt


# ---------------------------------------------------------------------------
# Plan #44 Task #6 — update_prompts.main() tail-calls
# update_subagent_tools.republish_coordinator_multiagent
# ---------------------------------------------------------------------------


def test_main_tail_calls_republish_coordinator_multiagent(tmp_path, monkeypatch):
    """update_prompts.main() must invoke republish_coordinator_multiagent at the end.

    This closes the silent failure mode behind the 2026-05-11 $47 incident:
    a sub-agent prompt update lands on Anthropic but the Coordinator's
    multiagent snapshot never advances, so production traffic continues to
    dispatch to the old version.

    The contract: the tail call passes the list of non-coordinator sub-agent
    IDs from AGENTS, and the call is unconditional (the function itself
    detects no-drift and skips).
    """
    import update_prompts as up  # type: ignore
    import update_subagent_tools as ust  # type: ignore

    # Isolate pin file writes to the temp directory so we don't scribble
    # on the real on-disk active_versions.json.
    pin = tmp_path / "active_versions.json"
    monkeypatch.setattr(up, "ACTIVE_VERSIONS_PATH", pin)

    # Skip the writing-agent prompt loader (it reaches into
    # orchestrator/config.py which requires Slack tokens).
    monkeypatch.setattr(up, "_load_writing_agent_prompt", lambda: None)

    # Patch AGENTS to a small fixed shape so the test owns the expected
    # sub-agent ID list. Three sub-agents + one coordinator — the tail
    # call must receive the three sub-agent IDs, NOT the coordinator's ID.
    monkeypatch.setattr(
        up,
        "AGENTS",
        {
            "coordinator": {"id": "agent_coord_x", "model": "claude-opus-4-8"},
            "pipeline_monitor": {
                "id": "agent_pipe_x",
                "model": "claude-sonnet-4-6",
            },
            "statistician": {
                "id": "agent_stat_x",
                "model": "claude-opus-4-8",
            },
        },
    )
    monkeypatch.setattr(
        up,
        "PROMPTS",
        {
            "coordinator": "coord prompt",
            "pipeline_monitor": "pipe prompt",
            "statistician": "stat prompt",
        },
    )

    # Fake Anthropic client: retrieve returns a SimpleNamespace; update
    # returns a fresh SimpleNamespace with version bumped by 1.
    retrieve_calls: list[str] = []

    def fake_retrieve(agent_id, **_):
        retrieve_calls.append(agent_id)
        return SimpleNamespace(
            id=agent_id,
            version=10,
            model=SimpleNamespace(id="claude-opus-4-8"),
        )

    def fake_update(**kwargs):
        return SimpleNamespace(id=kwargs["agent_id"], version=kwargs["version"] + 1)

    fake_client = MagicMock()
    fake_client.beta.agents.retrieve.side_effect = fake_retrieve
    fake_client.beta.agents.update.side_effect = fake_update

    # Stub republish_coordinator_multiagent at the module the tail call
    # imports it from. The function is imported inside update_prompts.main()
    # via `from update_subagent_tools import republish_coordinator_multiagent`,
    # so patching the source module works because update_prompts hasn't
    # cached a reference at module-load time.
    republish_calls: list[tuple] = []

    def fake_republish(client, sub_agent_ids):
        republish_calls.append((client, list(sub_agent_ids)))
        # Simulate the function detecting no drift and returning unchanged.
        return "unchanged", 10

    monkeypatch.setattr(ust, "republish_coordinator_multiagent", fake_republish)

    # Run main() with the fake client.
    up.main(client_obj=fake_client)

    # Assertion 1: republish was called exactly once.
    assert len(republish_calls) == 1, (
        f"main() must tail-call republish_coordinator_multiagent exactly "
        f"once; got {len(republish_calls)} calls"
    )

    # Assertion 2: the call passed the fake client through (not a fresh
    # anthropic.Anthropic() instance).
    passed_client, passed_ids = republish_calls[0]
    assert passed_client is fake_client, (
        "republish must receive the same client main() used for the prompt "
        "updates so all calls share the same auth/transport"
    )

    # Assertion 3: the ID list excludes the coordinator and includes every
    # other sub-agent in AGENTS (in this fixture: pipe + stat).
    assert set(passed_ids) == {"agent_pipe_x", "agent_stat_x"}, (
        f"republish must receive the union of non-coordinator sub-agent IDs; "
        f"got {passed_ids}"
    )
    assert "agent_coord_x" not in passed_ids, (
        "The Coordinator's own ID must NOT be in the sub-agent list — that "
        "would create a self-referential multiagent block"
    )


def test_main_skips_republish_when_no_sub_agents(tmp_path, monkeypatch):
    """If AGENTS has only the coordinator (no sub-agent IDs), the tail call is skipped.

    This is the bootstrap edge case where the coordinator is provisioned
    but no sub-agents exist yet. The republish should NOT be called with
    an empty list — a single-agent install has nothing to re-pin.
    """
    import update_prompts as up  # type: ignore
    import update_subagent_tools as ust  # type: ignore

    pin = tmp_path / "active_versions.json"
    monkeypatch.setattr(up, "ACTIVE_VERSIONS_PATH", pin)
    monkeypatch.setattr(up, "_load_writing_agent_prompt", lambda: None)
    monkeypatch.setattr(
        up,
        "AGENTS",
        {"coordinator": {"id": "agent_coord_only", "model": "claude-opus-4-8"}},
    )
    monkeypatch.setattr(up, "PROMPTS", {"coordinator": "coord prompt"})

    def fake_retrieve(agent_id, **_):
        return SimpleNamespace(
            id=agent_id,
            version=10,
            model=SimpleNamespace(id="claude-opus-4-8"),
        )

    def fake_update(**kwargs):
        return SimpleNamespace(id=kwargs["agent_id"], version=kwargs["version"] + 1)

    fake_client = MagicMock()
    fake_client.beta.agents.retrieve.side_effect = fake_retrieve
    fake_client.beta.agents.update.side_effect = fake_update

    republish_calls: list = []

    def fake_republish(client, sub_agent_ids):
        republish_calls.append((client, list(sub_agent_ids)))
        return "unchanged", 10

    monkeypatch.setattr(ust, "republish_coordinator_multiagent", fake_republish)

    up.main(client_obj=fake_client)

    assert republish_calls == [], (
        "Empty sub-agent list ⇒ skip the tail call. Without this guard, "
        "republish_coordinator_multiagent would attempt to re-publish a "
        "Coordinator with no sub-agents, which is a no-op the API rejects."
    )


def test_main_refreshes_coordinator_pin_after_republish_bump(tmp_path, monkeypatch):
    """If the multiagent re-publish bumps the Coordinator, the pin file is refreshed.

    The closing fix for the 2026-05-11 incident: when the tail call ships
    a Coordinator update (because sub-agent versions drifted), the
    Coordinator's own version advances. The pin file must reflect that so
    the inline verify-deployed-versions gate later in the workflow finds
    parity, not stale data.
    """
    import update_prompts as up  # type: ignore
    import update_subagent_tools as ust  # type: ignore

    pin = tmp_path / "active_versions.json"
    pin.write_text(json.dumps({"coordinator": 10}, indent=2))
    monkeypatch.setattr(up, "ACTIVE_VERSIONS_PATH", pin)
    monkeypatch.setattr(up, "_load_writing_agent_prompt", lambda: None)

    monkeypatch.setattr(
        up,
        "AGENTS",
        {
            "coordinator": {"id": "agent_coord_y", "model": "claude-opus-4-8"},
            "pipeline_monitor": {
                "id": "agent_pipe_y",
                "model": "claude-sonnet-4-6",
            },
        },
    )
    monkeypatch.setattr(
        up,
        "PROMPTS",
        {"coordinator": "coord prompt", "pipeline_monitor": "pipe prompt"},
    )

    def fake_retrieve(agent_id, **_):
        return SimpleNamespace(
            id=agent_id,
            version=10,
            model=SimpleNamespace(id="claude-opus-4-8"),
        )

    def fake_update(**kwargs):
        return SimpleNamespace(id=kwargs["agent_id"], version=kwargs["version"] + 1)

    fake_client = MagicMock()
    fake_client.beta.agents.retrieve.side_effect = fake_retrieve
    fake_client.beta.agents.update.side_effect = fake_update

    def fake_republish(client, sub_agent_ids):
        # Simulate the API bumping the Coordinator from v11 to v12 during
        # the multiagent re-snapshot.
        return "updated", 12

    monkeypatch.setattr(ust, "republish_coordinator_multiagent", fake_republish)

    up.main(client_obj=fake_client)

    on_disk = json.loads(pin.read_text())
    assert on_disk.get("coordinator") == 12, (
        f"After a republish bump, the pin file must reflect the new "
        f"Coordinator version. Got: {on_disk}"
    )
