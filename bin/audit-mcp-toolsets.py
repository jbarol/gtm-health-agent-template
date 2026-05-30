#!/usr/bin/env python3
"""Audit every Managed Agent for orphan ``mcp_toolset`` entries.

Plan #44 Task #3. Iteration 3 removed the Salesforce ``mcp_toolset`` from
every sub-agent — every SF read now routes through ``dump_sf_query``,
which materializes to Parquet on the Railway session disk. The
auto-approve path in ``session_runner.py`` still assumes some agents
carry an ``mcp_toolset`` entry; this script flags any that remain so we
either fix the agent definition or document why it stays.

Usage:
    python bin/audit-mcp-toolsets.py
    python bin/audit-mcp-toolsets.py --verbose

Reads agent IDs from ``agents/update_prompts.py:AGENTS`` (already pulls
from .env + hard-coded fallbacks). For each agent: retrieves the live
configuration via ``client.beta.agents.retrieve(id)``, summarizes the
``tools[]`` list, and flags any entry whose ``type`` field equals
``"mcp_toolset"``. Exits 0 when no orphans found; exits 1 with a clear
"orphan found" summary otherwise.

Read-only by design — there is no ``--apply`` or destructive mode. The
``--dry-run`` flag is accepted for symmetry with other scripts but is
the only mode of operation.

See ``docs/runbooks/managed-agents-conformance.md`` for the operator
workflow on each failure mode.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO_ROOT / "agents"

# Make ``agents/`` importable so we can pull the AGENTS registry directly
# rather than re-deriving it. Matches the pattern in bin/rollback-agent.py.
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


def _tool_summary(tool) -> str:
    """Render a one-line summary of a tool entry."""
    if hasattr(tool, "model_dump"):
        t = tool.model_dump(exclude_none=True)
    elif isinstance(tool, dict):
        t = tool
    else:
        t = {k: v for k, v in vars(tool).items() if not k.startswith("_")}
    type_str = t.get("type") or "<no-type>"
    name_str = t.get("name") or ""
    if type_str == "custom" and name_str:
        return f"custom:{name_str}"
    if type_str == "mcp_toolset":
        server = t.get("mcp_server_name") or t.get("mcp_server") or "?"
        return f"mcp_toolset:{server}"
    return type_str


def audit(client=None, verbose: bool = False) -> list[str]:
    """Return a list of orphan-finding lines. Empty list = clean."""
    from update_prompts import AGENTS  # type: ignore

    if client is None:
        client = _build_client()

    orphans: list[str] = []
    for name, cfg in sorted(AGENTS.items()):
        agent_id = cfg.get("id") or ""
        if not agent_id:
            if verbose:
                print(f"[SKIP] {name:25s} no agent ID (env var unset)")
            continue
        try:
            agent = client.beta.agents.retrieve(agent_id)
        except Exception as exc:
            print(f"[FAIL] {name:25s} ({agent_id}): retrieve failed: {exc}")
            continue
        tools = list(getattr(agent, "tools", None) or [])
        summaries = [_tool_summary(t) for t in tools]
        mcp_entries = [s for s in summaries if s.startswith("mcp_toolset")]
        if mcp_entries:
            orphans.append(
                f"agent {name} ({agent_id}): orphan mcp_toolset(s): "
                f"{', '.join(mcp_entries)}"
            )
            print(
                f"[ORPHAN] {name:25s} ({agent_id}): "
                f"{len(tools)} tool(s): {', '.join(summaries)}"
            )
        elif verbose:
            print(
                f"[OK] {name:25s} ({agent_id}): "
                f"{len(tools)} tool(s): {', '.join(summaries)}"
            )
    return orphans


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Audit every Managed Agent for orphan mcp_toolset entries. "
            "See docs/runbooks/managed-agents-conformance.md."
        )
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print every agent's tool summary, not just orphans.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help=(
            "Accepted for symmetry — this script is read-only. No "
            "alternative mode of operation."
        ),
    )
    args = parser.parse_args(argv)

    _load_env()
    orphans = audit(verbose=args.verbose)

    print()
    if orphans:
        print("=" * 72)
        print(f"Found {len(orphans)} orphan mcp_toolset entry(s):")
        for line in orphans:
            print(f"  - {line}")
        print()
        print("Next steps:")
        print("  1. Confirm the orphan is intended (some agent may still")
        print("     legitimately carry an mcp_toolset entry).")
        print("  2. If unintended: clear via `agents/update_subagent_tools.py`")
        print("     (it passes `mcp_servers=[]` when the target tools[]")
        print("     contains no mcp_toolset).")
        print("  3. If intended: document in agents/setup_agents.py or the")
        print("     plan that owns the exception, and re-run the audit.")
        print("  4. See docs/runbooks/managed-agents-conformance.md for")
        print("     the full operator workflow.")
        return 1

    print("=" * 72)
    print("All audited agents are clean — no orphan mcp_toolset entries.")
    print()
    print("Next steps:")
    print("  - No action required.")
    print("  - Re-run after the next sub-agent provisioning round or")
    print("    after merging a PR that touches agents/setup_agents.py.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
