"""Tests for ``agents/verify_active_versions.py`` (Plan #41 — D3)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO_ROOT / "agents"

# Ensure the agents/ dir is on sys.path so `import update_prompts` /
# `import verify_active_versions` resolve when pytest runs us from
# either repo root or the agents/ dir.
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))


@pytest.fixture()
def pin_file(tmp_path, monkeypatch):
    """Redirect ACTIVE_VERSIONS_PATH to a temp file with a fixed baseline."""
    import update_prompts  # type: ignore

    pin = tmp_path / "active_versions.json"
    pin.write_text(
        json.dumps(
            {
                "coordinator": 27,
                "quick_answer": 11,
                "dream": 7,
                "pipeline_monitor": 6,
                "sales_monitor": 6,
                "postsales_monitor": 6,
                "statistician": 8,
                "chart_designer": 3,
                "adversarial_reviewer": 4,
                "cross_domain_synthesizer": 2,
                "writing_agent": 1,
                "prompt_engineer": 1,
                # Plan #52 PR-F
                "rfp_reviewer": 1,
                "rfp_responder": 1,
            },
            indent=2,
            sort_keys=True,
        )
    )
    monkeypatch.setattr(update_prompts, "ACTIVE_VERSIONS_PATH", pin)
    return pin


def _make_client(live_versions: dict[str, int]):
    """Mock client whose retrieve() returns the live_versions value per ID."""
    from update_prompts import AGENTS  # type: ignore

    id_to_name = {cfg["id"]: name for name, cfg in AGENTS.items() if cfg.get("id")}

    def retrieve(agent_id, version=None, **_):
        name = id_to_name[agent_id]
        return SimpleNamespace(id=agent_id, version=live_versions[name])

    client = MagicMock()
    client.beta.agents.retrieve.side_effect = retrieve
    return client


def test_all_versions_match_returns_empty(pin_file):
    """When live state matches pins, verify() returns []."""
    from verify_active_versions import verify  # type: ignore

    client = _make_client(
        {
            "coordinator": 27,
            "quick_answer": 11,
            "dream": 7,
            "pipeline_monitor": 6,
            "sales_monitor": 6,
            "postsales_monitor": 6,
            "statistician": 8,
            "chart_designer": 3,
            "adversarial_reviewer": 4,
            "cross_domain_synthesizer": 2,
            "writing_agent": 1,
            "prompt_engineer": 1,
            # Plan #52 PR-F
            "rfp_reviewer": 1,
            "rfp_responder": 1,
        }
    )
    assert verify(client=client) == []


def test_drift_returns_mismatch_line(pin_file):
    """Coordinator drifted from v27 (pin) to v30 (live) — verify reports it."""
    from verify_active_versions import verify  # type: ignore

    client = _make_client(
        {
            "coordinator": 30,  # drift!
            "quick_answer": 11,
            "dream": 7,
            "pipeline_monitor": 6,
            "sales_monitor": 6,
            "postsales_monitor": 6,
            "statistician": 8,
            "chart_designer": 3,
            "adversarial_reviewer": 4,
            "cross_domain_synthesizer": 2,
            "writing_agent": 1,
            "prompt_engineer": 1,
            # Plan #52 PR-F
            "rfp_reviewer": 1,
            "rfp_responder": 1,
        }
    )
    mismatches = verify(client=client)
    assert len(mismatches) == 1
    assert "coordinator: live 30, pin 27" in mismatches[0]


def test_empty_pin_file_is_latest_mode_not_drift(tmp_path, monkeypatch, capsys):
    """Empty/missing pin file is the intentional FORK DEFAULT ("latest mode").

    The template ships agents/active_versions.json = {} so a fresh fork resolves
    every agent to its latest version. With no pins there is nothing to verify
    (no pin can disagree with live), so verify() returns [] (no drift) and prints
    an explanatory note — keeping the verify-agent-versions CI guardrail green
    for a freshly-forked repo instead of failing it on the empty default."""
    import update_prompts  # type: ignore

    missing = tmp_path / "active_versions.json"  # doesn't exist → read returns {}
    monkeypatch.setattr(update_prompts, "ACTIVE_VERSIONS_PATH", missing)

    from verify_active_versions import verify  # type: ignore

    client = MagicMock()
    mismatches = verify(client=client)
    assert mismatches == []  # empty pins = latest mode, not drift
    out = capsys.readouterr().out
    assert "latest-version mode" in out
    # No retrieve calls — short-circuited before hitting the API.
    client.beta.agents.retrieve.assert_not_called()


def test_pin_missing_for_an_agent_flags_explicitly(pin_file):
    """An agent in AGENTS but missing from the pin file is reported."""
    # Strip coordinator from the pin file.
    pins = json.loads(pin_file.read_text())
    del pins["coordinator"]
    pin_file.write_text(json.dumps(pins, indent=2, sort_keys=True))

    from verify_active_versions import verify  # type: ignore

    client = _make_client(
        {
            "coordinator": 27,
            "quick_answer": 11,
            "dream": 7,
            "pipeline_monitor": 6,
            "sales_monitor": 6,
            "postsales_monitor": 6,
            "statistician": 8,
            "chart_designer": 3,
            "adversarial_reviewer": 4,
            "cross_domain_synthesizer": 2,
            "writing_agent": 1,
            "prompt_engineer": 1,
            # Plan #52 PR-F
            "rfp_reviewer": 1,
            "rfp_responder": 1,
        }
    )
    mismatches = verify(client=client)
    coord_lines = [m for m in mismatches if "coordinator" in m]
    assert len(coord_lines) == 1
    assert "pin missing" in coord_lines[0]


def test_pinned_agent_without_id_fails_loud_not_silently(tmp_path, monkeypatch):
    """An agent that IS in the pin file but has no ID env var must fail
    loud — silent skip was the failure mode codex flagged on PR #98 (P2):
    WRITING_AGENT_ID could go missing in CI and writing_agent's version
    drift would never be caught.
    """
    import update_prompts  # type: ignore

    pin = tmp_path / "active_versions.json"
    pin.write_text(
        json.dumps({"coordinator": 27, "writing_agent": 5}, sort_keys=True, indent=2)
    )
    monkeypatch.setattr(update_prompts, "ACTIVE_VERSIONS_PATH", pin)

    # Patch AGENTS so writing_agent has an empty id (simulates CI workflow
    # forgetting to thread WRITING_AGENT_ID through).
    monkeypatch.setattr(
        update_prompts,
        "AGENTS",
        {
            "coordinator": {"id": "agent_abc", "model": "claude-opus-4-8"},
            "writing_agent": {"id": "", "model": "claude-haiku-4-5"},
        },
    )

    from verify_active_versions import verify  # type: ignore

    client = MagicMock()
    client.beta.agents.retrieve.return_value = SimpleNamespace(
        id="agent_abc", version=27
    )

    mismatches = verify(client=client)
    writing_lines = [m for m in mismatches if "writing_agent" in m]
    assert len(writing_lines) == 1
    assert "WRITING_AGENT_ID" in writing_lines[0]
    assert "verification cannot run" in writing_lines[0]


def test_unpinned_agent_without_id_is_still_skipped(tmp_path, monkeypatch):
    """An agent without an ID AND without a pin entry is genuinely
    unprovisioned — still skip silently. Only the (pinned, no-id) combo
    fails loud."""
    import update_prompts  # type: ignore

    pin = tmp_path / "active_versions.json"
    # writing_agent intentionally NOT pinned.
    pin.write_text(json.dumps({"coordinator": 27}, sort_keys=True, indent=2))
    monkeypatch.setattr(update_prompts, "ACTIVE_VERSIONS_PATH", pin)
    monkeypatch.setattr(
        update_prompts,
        "AGENTS",
        {
            "coordinator": {"id": "agent_abc", "model": "claude-opus-4-8"},
            "writing_agent": {"id": "", "model": "claude-haiku-4-5"},
        },
    )

    from verify_active_versions import verify  # type: ignore

    client = MagicMock()
    client.beta.agents.retrieve.return_value = SimpleNamespace(
        id="agent_abc", version=27
    )

    mismatches = verify(client=client)
    assert mismatches == []


def test_retrieve_failure_reports_per_agent(pin_file):
    """If retrieve() raises for one agent, the failure is reported, not silenced."""
    from update_prompts import AGENTS  # type: ignore
    from verify_active_versions import verify  # type: ignore

    coord_id = AGENTS["coordinator"]["id"]
    id_to_name = {cfg["id"]: name for name, cfg in AGENTS.items() if cfg.get("id")}

    def retrieve(agent_id, version=None, **_):
        if agent_id == coord_id:
            raise RuntimeError("503: backend down")
        return SimpleNamespace(
            id=agent_id,
            version={
                "quick_answer": 11,
                "dream": 7,
                "pipeline_monitor": 6,
                "sales_monitor": 6,
                "postsales_monitor": 6,
                "statistician": 8,
                "chart_designer": 3,
                "adversarial_reviewer": 4,
                "cross_domain_synthesizer": 2,
                "writing_agent": 1,
                "prompt_engineer": 1,
                # Plan #52 PR-F
                "rfp_reviewer": 1,
                "rfp_responder": 1,
            }[id_to_name[agent_id]],
        )

    client = MagicMock()
    client.beta.agents.retrieve.side_effect = retrieve

    mismatches = verify(client=client)
    coord_lines = [m for m in mismatches if "coordinator" in m]
    assert len(coord_lines) == 1
    assert "retrieve failed" in coord_lines[0]
    assert "503" in coord_lines[0]


# ---------------------------------------------------------------------------
# Plan #44 Task #6 — Coordinator multiagent pin parity
# ---------------------------------------------------------------------------


def _make_client_with_multiagent(
    live_versions: dict[str, int],
    coord_multiagent_pins: dict[str, int] | None,
):
    """Mock client where the Coordinator retrieve carries a multiagent block.

    Every non-coordinator retrieve returns the live_versions entry for that
    agent. The Coordinator retrieve returns a SimpleNamespace whose
    ``multiagent.agents[i].version`` matches ``coord_multiagent_pins[id]``
    when provided, or has no multiagent attribute when ``None`` is passed.
    """
    from update_prompts import AGENTS  # type: ignore

    id_to_name = {cfg["id"]: name for name, cfg in AGENTS.items() if cfg.get("id")}
    coord_id = AGENTS["coordinator"]["id"]

    def retrieve(agent_id, version=None, **_):
        if agent_id == coord_id:
            kwargs = {"id": agent_id, "version": live_versions["coordinator"]}
            if coord_multiagent_pins is not None:
                kwargs["multiagent"] = SimpleNamespace(
                    type="coordinator",
                    agents=[
                        SimpleNamespace(id=sub_id, type="agent", version=pinned_v)
                        for sub_id, pinned_v in coord_multiagent_pins.items()
                    ],
                )
            return SimpleNamespace(**kwargs)
        # Non-coordinator: look up by name.
        return SimpleNamespace(id=agent_id, version=live_versions[id_to_name[agent_id]])

    client = MagicMock()
    client.beta.agents.retrieve.side_effect = retrieve
    return client


def test_multiagent_pin_parity_passes_when_pins_match_live(pin_file):
    """Coordinator multiagent pins == live sub-agent versions → no drift line."""
    from update_prompts import AGENTS  # type: ignore
    from verify_active_versions import verify  # type: ignore

    live = {
        "coordinator": 27,
        "quick_answer": 11,
        "dream": 7,
        "pipeline_monitor": 6,
        "sales_monitor": 6,
        "postsales_monitor": 6,
        "statistician": 8,
        "chart_designer": 3,
        "adversarial_reviewer": 4,
        "cross_domain_synthesizer": 2,
        "writing_agent": 1,
        "prompt_engineer": 1,
        # Plan #52 PR-F
        "rfp_reviewer": 1,
        "rfp_responder": 1,
    }
    # Coordinator pins every sub-agent at their live version — parity OK.
    coord_pins = {
        AGENTS[name]["id"]: ver
        for name, ver in live.items()
        if name != "coordinator" and AGENTS.get(name, {}).get("id")
    }
    client = _make_client_with_multiagent(live, coord_pins)
    assert verify(client=client) == []


def test_multiagent_pin_parity_reports_drift_on_stale_pin(pin_file):
    """One sub-agent pinned at an old version inside the Coordinator's
    multiagent.agents[] block produces a drift line — even though the
    per-agent loop above sees the sub-agent at its live (correct) version.

    This is the 2026-05-11 $47 incident shape: sub-agent prompt landed on
    Anthropic, sub-agent's own .version advanced, pin file was refreshed,
    but the Coordinator's multiagent snapshot was never re-published, so
    production traffic continued to dispatch to the stale prompt.
    """
    from update_prompts import AGENTS  # type: ignore
    from verify_active_versions import verify  # type: ignore

    live = {
        "coordinator": 27,
        "quick_answer": 11,
        "dream": 7,
        "pipeline_monitor": 6,
        "sales_monitor": 6,
        "postsales_monitor": 6,
        "statistician": 8,
        "chart_designer": 3,
        "adversarial_reviewer": 4,
        "cross_domain_synthesizer": 2,
        "writing_agent": 1,
        "prompt_engineer": 1,
        # Plan #52 PR-F
        "rfp_reviewer": 1,
        "rfp_responder": 1,
    }
    coord_pins = {
        AGENTS[name]["id"]: ver
        for name, ver in live.items()
        if name != "coordinator" and AGENTS.get(name, {}).get("id")
    }
    # Drift: Coordinator still has Pipeline Monitor pinned at v3, even
    # though live Pipeline Monitor is at v6 (the per-agent loop sees v6
    # correctly).
    pipe_id = AGENTS["pipeline_monitor"]["id"]
    coord_pins[pipe_id] = 3

    client = _make_client_with_multiagent(live, coord_pins)
    mismatches = verify(client=client)
    drift_lines = [m for m in mismatches if "multiagent pin drift" in m]
    assert len(drift_lines) == 1, (
        f"Expected 1 multiagent pin drift line; got {drift_lines}"
    )
    assert pipe_id in drift_lines[0]
    assert "v3" in drift_lines[0]
    assert "v6" in drift_lines[0]
    # Per-agent loop still passes for Pipeline Monitor (live matches pin
    # file) — only the multiagent block is stale.
    per_agent_pipe = [
        m
        for m in mismatches
        if m.startswith("agent pipeline_monitor:") and "multiagent" not in m
    ]
    assert per_agent_pipe == []


def test_multiagent_pin_parity_no_block_means_no_drift(pin_file):
    """Coordinator with no multiagent attribute → no drift, no crash.

    Mirrors a single-agent Coordinator (no sub-agents pinned). The parity
    check must short-circuit cleanly rather than treat the absent block
    as drift.
    """
    from verify_active_versions import verify  # type: ignore

    live = {
        "coordinator": 27,
        "quick_answer": 11,
        "dream": 7,
        "pipeline_monitor": 6,
        "sales_monitor": 6,
        "postsales_monitor": 6,
        "statistician": 8,
        "chart_designer": 3,
        "adversarial_reviewer": 4,
        "cross_domain_synthesizer": 2,
        "writing_agent": 1,
        "prompt_engineer": 1,
        # Plan #52 PR-F
        "rfp_reviewer": 1,
        "rfp_responder": 1,
    }
    # coord_multiagent_pins=None ⇒ retrieve() returns no `multiagent`
    # attribute. The helper returns [] without firing any extra retrieves.
    client = _make_client_with_multiagent(live, None)
    mismatches = verify(client=client)
    drift_lines = [m for m in mismatches if "multiagent" in m]
    assert drift_lines == []
