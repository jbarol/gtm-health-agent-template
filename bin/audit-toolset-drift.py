#!/usr/bin/env python3
"""Snapshot each agent's tools[] and diff against the most recent prior snapshot.

Plan #44 Task #23 — weekly drift canary for ``agent_toolset_20260401``.
The date suffix on the toolset ID matches the beta header
``managed-agents-2026-04-01``; per the docs, the tool schemas behind
the toolset can float within the dated contract. Breaking changes
would ship as a new dated toolset ID. This canary detects drift WITHIN
the contract: tool schema/description changes that landed without an
agent update on our side.

Usage:
    python bin/audit-toolset-drift.py            # snapshot + diff
    python bin/audit-toolset-drift.py --no-write # diff-only (CI dry run)
    python bin/audit-toolset-drift.py --verbose  # print full diff payload

For every agent in ``agents/update_prompts.py:AGENTS`` with a non-empty
ID, retrieves the live config via ``client.beta.agents.retrieve(id)``
and writes the normalized ``tools[*]`` payload to
``agents/toolset-snapshots/<YYYY-MM-DD>.json``. Then diffs the new
snapshot against the most recent prior snapshot in the same directory.

Exit codes:
    0 — no drift, or no prior snapshot to compare against (first run).
    1 — drift detected; the script writes the new snapshot, prints
        the diff summary, and exits non-zero so the GitHub Actions
        canary fails and Slack-notifies admins.

See ``docs/runbooks/managed-agents-conformance.md`` for the operator
workflow on a drift alert (review diff → inherit or update agents).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO_ROOT / "agents"
SNAPSHOTS_DIR = AGENTS_DIR / "toolset-snapshots"

for _p in (AGENTS_DIR,):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _load_env() -> None:
    """Manual dotenv loader matching ``orchestrator/config.py``."""
    dotenv = REPO_ROOT / ".env"
    if not dotenv.exists():
        return
    for line in dotenv.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def _build_client():
    """Return an Anthropic client. Imported lazily so tests can stub."""
    import anthropic  # noqa: WPS433

    return anthropic.Anthropic()


def _normalize(value):
    """Recursively normalize SDK objects to plain dicts/lists for JSON dump."""
    if hasattr(value, "model_dump"):
        return _normalize(value.model_dump(exclude_none=True))
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    return value


def collect_snapshot(client=None) -> dict[str, list]:
    """Return ``{agent_short_name: tools[*]}`` for every provisioned agent."""
    from update_prompts import AGENTS  # type: ignore

    if client is None:
        client = _build_client()

    snapshot: dict[str, list] = {}
    for name, cfg in sorted(AGENTS.items()):
        agent_id = cfg.get("id") or ""
        if not agent_id:
            print(f"[SKIP] {name:25s} no agent ID (env var unset)")
            continue
        try:
            agent = client.beta.agents.retrieve(agent_id)
        except Exception as exc:
            print(f"[FAIL] {name:25s} retrieve failed: {exc}")
            continue
        snapshot[name] = _normalize(list(getattr(agent, "tools", None) or []))
        print(f"[OK] {name:25s} captured {len(snapshot[name])} tool(s)")
    return snapshot


def _most_recent_prior(snapshots_dir: Path, today_file: Path) -> Path | None:
    """Return the most recent snapshot file older than ``today_file``."""
    if not snapshots_dir.exists():
        return None
    candidates = sorted(
        p for p in snapshots_dir.glob("*.json") if p.is_file() and p != today_file
    )
    return candidates[-1] if candidates else None


def diff_snapshots(prior: dict, current: dict) -> list[str]:
    """Return a list of drift lines (empty = no drift)."""
    drift: list[str] = []
    prior_agents = set(prior.keys())
    current_agents = set(current.keys())

    for added in sorted(current_agents - prior_agents):
        drift.append(f"agent {added}: NEW (no prior snapshot)")
    for removed in sorted(prior_agents - current_agents):
        drift.append(f"agent {removed}: REMOVED (was in prior snapshot)")

    for name in sorted(prior_agents & current_agents):
        if prior[name] != current[name]:
            drift.append(f"agent {name}: tools[] changed since prior snapshot")
    return drift


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Snapshot each agent's tools[] and diff against the most "
            "recent prior snapshot. See "
            "docs/runbooks/managed-agents-conformance.md."
        )
    )
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="Diff-only; do not write a new snapshot file (useful in CI dry runs).",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print the full new-vs-old diff payload for each drifted agent.",
    )
    args = parser.parse_args(argv)

    _load_env()
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)

    snapshot = collect_snapshot()
    today = date.today().isoformat()
    today_file = SNAPSHOTS_DIR / f"{today}.json"

    prior_file = _most_recent_prior(SNAPSHOTS_DIR, today_file)
    if prior_file is None:
        print()
        print("=" * 72)
        print("No prior snapshot found — this is the first run.")
        if not args.no_write:
            today_file.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")
            print(f"Wrote initial snapshot to {today_file}")
        print()
        print("Next steps:")
        print("  - Re-run weekly to establish baseline drift detection.")
        print("  - First drift alert may take 1+ weeks depending on cron cadence.")
        return 0

    try:
        prior_snapshot = json.loads(prior_file.read_text())
    except Exception as exc:
        print(f"[FAIL] could not parse prior snapshot {prior_file}: {exc}")
        return 1

    drift = diff_snapshots(prior_snapshot, snapshot)

    if not args.no_write:
        today_file.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")

    print()
    print("=" * 72)
    if not drift:
        print(f"No drift detected (compared {today_file.name} vs {prior_file.name}).")
        print()
        print("Next steps:")
        print("  - No action required.")
        print("  - Re-run weekly (cron handles this automatically).")
        return 0

    print(f"Drift detected ({len(drift)} agent(s) changed):")
    for line in drift:
        print(f"  - {line}")
    if args.verbose:
        print()
        print("Drift detail (current vs prior):")
        for name in sorted(set(prior_snapshot) & set(snapshot)):
            if prior_snapshot[name] != snapshot[name]:
                print(f"--- {name} (prior) ---")
                print(json.dumps(prior_snapshot[name], indent=2, sort_keys=True))
                print(f"+++ {name} (current) +++")
                print(json.dumps(snapshot[name], indent=2, sort_keys=True))
    print()
    print("Next steps:")
    print("  1. Review the diff against the most recent Anthropic")
    print("     release notes. Most floating changes within")
    print("     agent_toolset_20260401 are safe to inherit.")
    print("  2. If the change is unsafe (e.g. a tool argument renamed),")
    print("     update setup_agents.py + run agents/update_subagent_tools.py")
    print("     to re-publish agent definitions that match the new shape.")
    print("  3. If the toolset ID itself changes (e.g.")
    print("     agent_toolset_2026XXXX), update setup_agents.py to point")
    print("     at the new ID after vetting.")
    print("  4. See docs/runbooks/managed-agents-conformance.md for")
    print("     the full operator workflow.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
