"""One-off: archive the retired Report Writer agent on Anthropic.

The Report Writer (Sonnet 4.6, ``agent_EXAMPLE_report_writer``) was
superseded by the Writing Agent (Haiku 4.5) on 2026-05-11.  This script
archives the agent on Anthropic so it no longer appears in active agent
lists and cannot be invoked.  The Managed Agents API exposes ``archive``,
not hard-delete; archive is the SDK's terminal state.

Destructive operation — requires ``--confirm`` to actually archive.
Without the flag, the script prints the current state of the agent and
exits.

Usage:
    set -a && source .env && set +a
    python3 agents/decommission_report_writer.py            # dry-run
    python3 agents/decommission_report_writer.py --confirm  # archive
"""

from __future__ import annotations

import argparse
import os
import sys

import anthropic

REPORT_WRITER_ID = "agent_EXAMPLE_report_writer"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Actually delete the agent (default: dry-run / print state only).",
    )
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY is unset — source .env first.", file=sys.stderr)
        return 2

    client = anthropic.Anthropic(api_key=api_key)

    # Read-before-write: print current state so the operator sees what they're
    # about to delete.
    try:
        current = client.beta.agents.retrieve(REPORT_WRITER_ID)
    except anthropic.NotFoundError:
        print(f"Agent {REPORT_WRITER_ID} not found — already deleted.")
        return 0
    except Exception as exc:
        print(f"ERROR retrieving agent: {exc}", file=sys.stderr)
        return 1

    print("=" * 72)
    print(f"  id:      {getattr(current, 'id', '?')}")
    print(f"  name:    {getattr(current, 'name', '?')}")
    print(f"  model:   {getattr(getattr(current, 'model', None), 'id', '?')}")
    print(f"  version: {getattr(current, 'version', '?')}")
    print("=" * 72)

    archived_at = getattr(current, "archived_at", None)
    if archived_at is not None:
        print(f"\nAlready archived at {archived_at}. No action taken.")
        return 0

    if not args.confirm:
        print("\nDRY-RUN. Pass --confirm to actually archive.")
        return 0

    print(f"\nArchiving {REPORT_WRITER_ID} ...")
    try:
        result = client.beta.agents.archive(REPORT_WRITER_ID)
    except Exception as exc:
        print(f"ERROR during archive: {exc}", file=sys.stderr)
        return 1

    archived_at = getattr(result, "archived_at", None)
    if archived_at is None:
        print("WARN: archive call returned no archived_at timestamp.")
        return 1
    print(f"Archived at {archived_at}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
