"""Tests for ``bin/stamp_superseded_memory.py`` (Plan #46 / PR 4).

Smoke-level coverage: --touched-files matches the Kapa topic, finds
every Kapa-mentioning transient_infra entry in a temp memory dir, and
stamps each one with superseded_at_commit.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Iterator

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "bin" / "stamp_superseded_memory.py"


def _load_script_module():
    """Load ``bin/stamp_superseded_memory.py`` by path."""
    # Make orchestrator/ importable so memory_hygiene resolves.
    orch = REPO_ROOT / "orchestrator"
    if str(orch) not in sys.path:
        sys.path.insert(0, str(orch))
    spec = importlib.util.spec_from_file_location(
        "stamp_superseded_memory", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def script_module():
    return _load_script_module()


@pytest.fixture
def memory_seed_with_kapa(tmp_path: Path) -> Iterator[Path]:
    """Build a temp memory_seed/system/ with a Kapa transient_infra entry
    and a non-matching methodology entry."""
    sysdir = tmp_path / "system"
    sysdir.mkdir(parents=True)
    (sysdir / "kapa_status.md").write_text(
        "---\n"
        "kind: transient_infra\n"
        "valid_through_commit: oldsha111\n"
        "last_verified_at: 2026-05-14T20:46:37Z\n"
        "status: operational\n"
        "---\n"
        "\n"
        "# Kapa REST tool status — operational\n"
        "\n"
        "Live probe of search_knowledge_base passed at HTTP 200.\n"
    )
    (sysdir / "doc_updates.md").write_text(
        "---\nkind: methodology\n---\n\nDaily doc crawl summary.\n"
    )
    (sysdir / "unrelated_outage.md").write_text(
        "---\n"
        "kind: transient_infra\n"
        "valid_through_commit: oldsha222\n"
        "---\n"
        "\n"
        "Postgres pgbouncer outage 2026-05-01. Connection pool exhaustion.\n"
    )
    yield sysdir


def test_kapa_touched_files_stamps_kapa_entry(
    script_module, memory_seed_with_kapa: Path
) -> None:
    stamped, activated = script_module.stamp(
        touched_files=["orchestrator/kapa_rest_tool.py"],
        commit_sha="newsha999",
        memory_dir=memory_seed_with_kapa,
        dry_run=False,
    )
    assert "kapa" in activated
    stamped_names = sorted(p.name for p in stamped)
    assert stamped_names == ["kapa_status.md"]
    new_text = (memory_seed_with_kapa / "kapa_status.md").read_text()
    assert "superseded_at_commit: newsha999" in new_text
    # Unrelated transient_infra entry should be untouched.
    untouched = (memory_seed_with_kapa / "unrelated_outage.md").read_text()
    assert "superseded_at_commit" not in untouched


def test_dry_run_does_not_write(script_module, memory_seed_with_kapa: Path) -> None:
    before = (memory_seed_with_kapa / "kapa_status.md").read_text()
    stamped, _ = script_module.stamp(
        touched_files=["orchestrator/kapa_rest_tool.py"],
        commit_sha="newsha999",
        memory_dir=memory_seed_with_kapa,
        dry_run=True,
    )
    assert len(stamped) == 1
    after = (memory_seed_with_kapa / "kapa_status.md").read_text()
    assert before == after


def test_no_matching_touched_files_is_noop(
    script_module, memory_seed_with_kapa: Path
) -> None:
    stamped, activated = script_module.stamp(
        touched_files=["orchestrator/main.py", "docs/README.md"],
        commit_sha="newsha999",
        memory_dir=memory_seed_with_kapa,
        dry_run=False,
    )
    assert activated == []
    assert stamped == []


def test_missing_memory_dir_is_noop(script_module, tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist"
    stamped, activated = script_module.stamp(
        touched_files=["orchestrator/kapa_rest_tool.py"],
        commit_sha="abc",
        memory_dir=missing,
        dry_run=False,
    )
    assert activated == ["kapa"]
    assert stamped == []


def test_idempotent_same_sha_does_not_restamp(
    script_module, memory_seed_with_kapa: Path
) -> None:
    sha = "newsha999"
    first, _ = script_module.stamp(
        touched_files=["orchestrator/kapa_rest_tool.py"],
        commit_sha=sha,
        memory_dir=memory_seed_with_kapa,
        dry_run=False,
    )
    assert len(first) == 1
    # Re-run with the same SHA — script should skip already-stamped entries.
    second, _ = script_module.stamp(
        touched_files=["orchestrator/kapa_rest_tool.py"],
        commit_sha=sha,
        memory_dir=memory_seed_with_kapa,
        dry_run=False,
    )
    assert second == []


def test_main_entrypoint_runs_end_to_end(
    script_module, memory_seed_with_kapa: Path, capsys, monkeypatch
) -> None:
    monkeypatch.setenv("GITHUB_SHA", "cisha777")
    rc = script_module.main(
        [
            "--touched-files",
            "orchestrator/kapa_rest_tool.py",
            "--memory-dir",
            str(memory_seed_with_kapa),
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "Activated topics: kapa" in captured.out
    assert "Stamped 1 entries" in captured.out
    assert "kapa_status.md" in captured.out
    final = (memory_seed_with_kapa / "kapa_status.md").read_text()
    assert "superseded_at_commit: cisha777" in final


def test_flatten_paths_supports_comma_separated(script_module) -> None:
    out = script_module._flatten_paths(
        ["orchestrator/kapa_rest_tool.py,bin/probe_kapa.py"]
    )
    assert out == ["orchestrator/kapa_rest_tool.py", "bin/probe_kapa.py"]
