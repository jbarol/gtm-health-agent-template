#!/usr/bin/env python3
"""Probe Opportunity for the free-text closed-lost notes field (Design F).

Reads Acme's SF schema via the same MCP-vault credential the agents
use, prints every candidate text-typed custom field whose label or API
name hints at loss-reason / loss-notes content.

Read-only. Never modifies the SF org. Use this once to discover the
canonical field name, then bake it into:
  - /acme/schema_cache.md (memory store), and
  - the Sales Monitor / Post-Sales Monitor CL query templates in
    agents/update_prompts.py.

Usage:
    python3 bin/probe_loss_notes_field.py [--portco acme]

Why this exists:
    sesn_EXAMPLE (2026-05-15) pulled Loss_Reason__c +
    the four picklist flags but missed the qualitative free-text NOTES
    column the user actually wanted. User confirmed the column exists
    (they pulled it themselves and pasted into Claude separately).
    Hunting it by hand is the right one-time investment.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load_env() -> None:
    dotenv = REPO / ".env"
    if not dotenv.exists():
        return
    for line in dotenv.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


# Patterns most likely to indicate the qualitative loss-notes column.
# The user mentioned UI/UX and SSO notes — those are free-text
# elaborations rather than picklists, so we look for `text` / `textarea`
# field types and label/name hints.
_HINT_TOKENS = (
    "loss_note",
    "loss_notes",
    "lost_note",
    "lost_notes",
    "loss_reason",
    "loss_description",
    "lost_description",
    "loss_detail",
    "loss_comment",
    "closed_lost",
    "why_lost",
    "lost_competitor",
    "lost_to",
)
_FREE_TEXT_SOAP_TYPES = ("string", "textarea", "richtextarea", "longtextarea")


def _candidate_match(name: str, label: str, soap_type: str) -> str | None:
    """Return a short reason string if this field is a candidate, else None."""
    lname = (name or "").lower()
    llabel = (label or "").lower()
    if soap_type not in _FREE_TEXT_SOAP_TYPES:
        return None
    for token in _HINT_TOKENS:
        if token in lname:
            return f"name-token {token!r}"
        if token.replace("_", " ") in llabel:
            return f"label-token {token!r}"
    # Catch-all: any free-text field with "lost" or "loss" in either ID.
    if "lost" in lname or "loss" in lname:
        return "broad lost/loss in name"
    if "lost" in llabel or "loss" in llabel:
        return "broad lost/loss in label"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--portco", default="acme")
    args = parser.parse_args(argv)

    _load_env()

    try:
        # Use the same session_runner helper the orchestrator uses so we
        # honor the SF MCP vault credential and rate-limit policy.
        sys.path.insert(0, str(REPO / "orchestrator"))
        from session_runner import _get_sf_client  # type: ignore
    except Exception as e:
        print(f"FAILED to import session_runner._get_sf_client: {e}", file=sys.stderr)
        return 2

    try:
        client = _get_sf_client(args.portco)
    except Exception as e:
        print(
            f"FAILED to acquire SF client for portco={args.portco}: {e}",
            file=sys.stderr,
        )
        return 3

    try:
        desc = client.Opportunity.describe()
    except Exception as e:
        print(f"FAILED to describe Opportunity: {e}", file=sys.stderr)
        return 4

    candidates: list[dict] = []
    for f in desc.get("fields", []):
        name = f.get("name", "")
        label = f.get("label", "")
        soap = f.get("soapType", "").split(":")[-1].lower()
        match = _candidate_match(name, label, soap)
        if match:
            candidates.append(
                {
                    "name": name,
                    "label": label,
                    "type": soap,
                    "length": f.get("length"),
                    "reason": match,
                }
            )

    if not candidates:
        print(
            "No candidate fields found. Patterns may need widening — see _HINT_TOKENS."
        )
        return 1

    print(
        f"Found {len(candidates)} candidate free-text loss field(s) on Opportunity:\n"
    )
    for c in candidates:
        print(
            f"  {c['name']:<48} {c['label']:<60} [{c['type']}, len={c['length']}] "
            f"({c['reason']})"
        )
    print()
    print("Next steps:")
    print(
        "  1. Pick the canonical field (probably the longest length / most general label)."
    )
    print("  2. Add it to /acme/schema_cache.md under 'Closed Lost notes field'.")
    print("  3. Update Sales Monitor's CL query template in agents/update_prompts.py.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
