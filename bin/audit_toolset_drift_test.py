"""Tests for ``bin/audit-toolset-drift.py`` (Plan #44 Task #23).

Hyphenated filename loaded by path via ``importlib.util``. Tests
override the SNAPSHOTS_DIR to a tmp directory so they don't write to
the real ``agents/toolset-snapshots/``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "bin" / "audit-toolset-drift.py"


_DRIFT_MODULE = None


def _load_drift_module():
    """Load (or return cached) drift module. One instance per test session."""
    global _DRIFT_MODULE
    if _DRIFT_MODULE is not None:
        return _DRIFT_MODULE
    for p in (REPO_ROOT / "agents", REPO_ROOT / "orchestrator"):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    spec = importlib.util.spec_from_file_location("audit_toolset_drift", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _DRIFT_MODULE = mod
    return mod


@pytest.fixture()
def drift_mod():
    return _load_drift_module()


@pytest.fixture()
def isolated_snapshots(tmp_path, monkeypatch, drift_mod):
    """Redirect SNAPSHOTS_DIR to a tmp directory so tests don't write to repo."""
    monkeypatch.setattr(drift_mod, "SNAPSHOTS_DIR", tmp_path)
    return tmp_path


def _make_client(tools_per_agent: dict[str, list]):
    from update_prompts import AGENTS  # type: ignore

    def retrieve(agent_id, **_):
        for short_name, cfg in AGENTS.items():
            if cfg.get("id") == agent_id:
                return SimpleNamespace(
                    id=agent_id,
                    tools=tools_per_agent.get(short_name, []),
                )
        raise RuntimeError(f"unknown id {agent_id}")

    client = MagicMock()
    client.beta.agents.retrieve.side_effect = retrieve
    return client


def test_first_run_writes_snapshot_and_exits_zero(
    drift_mod, isolated_snapshots, monkeypatch
):
    """No prior snapshot → write the initial one and exit 0."""
    from update_prompts import AGENTS  # type: ignore

    tools = {
        name: [{"type": "agent_toolset_20260401"}]
        for name, cfg in AGENTS.items()
        if cfg.get("id")
    }
    client = _make_client(tools)
    monkeypatch.setattr(drift_mod, "_build_client", lambda: client)

    rc = drift_mod.main([])
    assert rc == 0
    files = list(isolated_snapshots.glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text())
    # Each provisioned agent should be in the snapshot.
    for name, cfg in AGENTS.items():
        if cfg.get("id"):
            assert name in payload


def test_no_drift_when_snapshots_equal(drift_mod, isolated_snapshots, monkeypatch):
    """Identical tools[] between runs → no drift, rc 0."""
    from update_prompts import AGENTS  # type: ignore

    tools = {
        name: [{"type": "agent_toolset_20260401"}]
        for name, cfg in AGENTS.items()
        if cfg.get("id")
    }
    # Pre-seed yesterday's snapshot identical to today's.
    yesterday = (date.today() - timedelta(days=7)).isoformat()
    (isolated_snapshots / f"{yesterday}.json").write_text(
        json.dumps(tools, indent=2, sort_keys=True)
    )
    client = _make_client(tools)
    monkeypatch.setattr(drift_mod, "_build_client", lambda: client)

    rc = drift_mod.main([])
    assert rc == 0


def test_drift_detected_when_tools_change(drift_mod, isolated_snapshots, monkeypatch):
    """A changed tool description triggers drift detection."""
    from update_prompts import AGENTS  # type: ignore

    # Yesterday's snapshot has the old description.
    prior_tools = {
        name: [{"type": "agent_toolset_20260401", "description": "old"}]
        for name, cfg in AGENTS.items()
        if cfg.get("id")
    }
    yesterday = (date.today() - timedelta(days=7)).isoformat()
    (isolated_snapshots / f"{yesterday}.json").write_text(
        json.dumps(prior_tools, indent=2, sort_keys=True)
    )

    # Today's live state has a tweaked description on the Coordinator.
    today_tools = {
        name: [{"type": "agent_toolset_20260401", "description": "old"}]
        for name, cfg in AGENTS.items()
        if cfg.get("id")
    }
    today_tools["coordinator"] = [
        {"type": "agent_toolset_20260401", "description": "NEW WORDING"}
    ]

    client = _make_client(today_tools)
    monkeypatch.setattr(drift_mod, "_build_client", lambda: client)
    rc = drift_mod.main([])
    assert rc == 1


def test_diff_function_classifies_added_removed_changed(drift_mod):
    """Unit-test the pure-function diff so the CLI shell is testable in isolation."""
    prior = {
        "coordinator": [{"type": "agent_toolset_20260401"}],
        "dream": [{"type": "agent_toolset_20260401"}],
    }
    current = {
        "coordinator": [{"type": "agent_toolset_20260401", "extra": "x"}],
        "new_agent": [{"type": "agent_toolset_20260401"}],
        # dream removed
    }
    drift = drift_mod.diff_snapshots(prior, current)
    joined = "\n".join(drift)
    assert "coordinator" in joined and "changed" in joined
    assert "new_agent" in joined and "NEW" in joined
    assert "dream" in joined and "REMOVED" in joined


def test_no_write_skips_disk_persistence(drift_mod, isolated_snapshots, monkeypatch):
    """--no-write should diff against prior but not write a new snapshot."""
    from update_prompts import AGENTS  # type: ignore

    tools = {
        name: [{"type": "agent_toolset_20260401"}]
        for name, cfg in AGENTS.items()
        if cfg.get("id")
    }
    yesterday = (date.today() - timedelta(days=7)).isoformat()
    (isolated_snapshots / f"{yesterday}.json").write_text(
        json.dumps(tools, indent=2, sort_keys=True)
    )
    client = _make_client(tools)
    monkeypatch.setattr(drift_mod, "_build_client", lambda: client)

    rc = drift_mod.main(["--no-write"])
    assert rc == 0
    files_today = [
        p for p in isolated_snapshots.glob("*.json") if p.name != f"{yesterday}.json"
    ]
    assert files_today == []
