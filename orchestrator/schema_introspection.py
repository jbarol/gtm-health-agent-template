"""Nightly Salesforce schema introspection — keeps schema_cache.md fresh.

Theme C (2026-05-16). Pre-fix, ``/{portco}/schema_cache.md`` in the
Anthropic memory store was hand-curated; today's incident found it both
missing critical fields (``Closed_Lost_Notes__c``, 45.1% fill on 7,716
Closed Lost opps) and listing fields that don't exist in the live org
(``Forecast_Category__c`` → actually ``ForecastCategoryName``).

This module runs daily at 02:00 PT via APScheduler. For each Salesforce-
backed portco it queries ``FieldDefinition`` for the 7 most-active
objects, renders a machine-authored ``schema_cache.md``, and writes it
back to the portco's memory store directory. The hand-curated operator
notes live in a SEPARATE file (``manual_schema_notes.md``) the Coordinator
reads alongside — this avoids destroying tribal-knowledge during the
nightly rewrite.

Best-effort:
- Per-portco failure does not stop the run for other portcos.
- A full failure is logged + admin-DMed but never raises out of the
  scheduled callable.
- If the org rejects FieldDefinition introspection (rare; usually
  permission), the previous schema_cache.md is preserved.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)


# The seven objects the GTM Health Agent touches most. Adding more is
# cheap — each adds one FieldDefinition query — but the prompt-context
# cost on the read side scales linearly, so keep this tight.
INTROSPECTED_SOBJECTS = (
    "Opportunity",
    "Lead",
    "Account",
    "Contact",
    "OpportunityContactRole",
    "Task",
    "User",
)


def fetch_field_definitions(sf_client, sobject: str) -> list[dict]:
    """Query FieldDefinition for one sObject. Returns rows or []."""
    soql = (
        "SELECT QualifiedApiName, Label, DataType, Length "
        f"FROM FieldDefinition "
        f"WHERE EntityDefinition.QualifiedApiName = '{sobject}' "
        "ORDER BY DataType, QualifiedApiName"
    )
    try:
        resp = sf_client.query_all(soql)
        return list(resp.get("records") or [])
    except Exception as exc:
        log.warning(
            "schema_introspection: FieldDefinition query failed for %s: %s",
            sobject,
            exc,
        )
        return []


def render_schema_cache(portco_key: str, by_sobject: dict[str, list[dict]]) -> str:
    """Render the machine-authored schema_cache.md text.

    Sections:
      - Top frontmatter: timestamp + warning that this file is auto-generated
      - SOQL long-text constraint reminder
      - Per-sObject field listing, with a "Free-text candidates" sub-table
        that surfaces Long-Text-Area / Rich-Text-Area fields first (these
        are the loss-reason / notes fields agents actually need to find).
    """
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    out: list[str] = []
    out.append(f"# SF Schema Cache — {portco_key} (AUTO-GENERATED)")
    out.append("")
    out.append(
        f"_Last introspected: {ts}._ This file is rewritten nightly by "
        "`orchestrator/schema_introspection.py`. **Do not edit by hand** — "
        "your changes will be overwritten. Operator nuance "
        "(rollout dates, semantic conventions, gotchas) lives in "
        "`manual_schema_notes.md` in this same directory."
    )
    out.append("")
    out.append("## SOQL constraints (read me before composing queries)")
    out.append("")
    out.append(
        "Long-Text-Area, Rich-Text-Area, and Text-Area(>255) fields on "
        "Salesforce CANNOT appear in WHERE filters or aggregate functions. "
        "See `<data_access_contract>` in the system prompt for the SELECT-"
        "then-DuckDB workaround. The free-text candidates listed below "
        "are exactly the fields this constraint applies to."
    )
    out.append("")

    for sobject in INTROSPECTED_SOBJECTS:
        rows = by_sobject.get(sobject, [])
        if not rows:
            continue
        out.append(f"## {sobject}")
        out.append("")
        # Free-text candidates first — these are the ones that need the
        # special handling.
        free_text = [
            r
            for r in rows
            if r.get("DataType", "").startswith(
                ("Long Text Area", "Rich Text Area", "Text Area")
            )
        ]
        if free_text:
            out.append("### Free-text candidates")
            out.append("")
            out.append("| Field | DataType | Length |")
            out.append("|---|---|---|")
            for r in free_text:
                out.append(
                    f"| `{r.get('QualifiedApiName')}` | "
                    f"{r.get('DataType')} | {r.get('Length', '')} |"
                )
            out.append("")
        # Full field listing
        out.append("### All fields")
        out.append("")
        out.append("| Field | Label | DataType | Length |")
        out.append("|---|---|---|---|")
        for r in rows:
            out.append(
                f"| `{r.get('QualifiedApiName')}` | "
                f"{(r.get('Label') or '').replace('|', '\\|')} | "
                f"{r.get('DataType')} | {r.get('Length', '')} |"
            )
        out.append("")

    return "\n".join(out)


def write_schema_cache(
    portco_key: str,
    content: str,
    memory_root: Optional[str] = None,
) -> str:
    """Write the schema_cache.md to the portco's memory directory.

    Returns the on-disk path. Raises only on a fundamental filesystem
    problem (caller wraps in try/except).
    """
    root = memory_root or os.environ.get(
        "MEMORY_STORE_ROOT", "/mnt/memory/gtm-health-memory"
    )
    target_dir = os.path.join(root, portco_key)
    os.makedirs(target_dir, exist_ok=True)
    target_path = os.path.join(target_dir, "schema_cache.md")
    with open(target_path, "w", encoding="utf-8") as f:
        f.write(content)
    return target_path


def introspect_portco(sf_client, portco_key: str) -> dict:
    """Run the full introspection for one portco. Returns stats dict.

    sf_client is the simple_salesforce client (or any object exposing
    ``query_all(soql)`` returning ``{"records": [...]}``).
    """
    stats: dict = {
        "portco_key": portco_key,
        "sobjects_queried": 0,
        "fields_total": 0,
        "free_text_fields": 0,
        "path": None,
        "error": None,
    }
    try:
        by_sobject: dict[str, list[dict]] = {}
        for sobject in INTROSPECTED_SOBJECTS:
            rows = fetch_field_definitions(sf_client, sobject)
            if rows:
                by_sobject[sobject] = rows
                stats["sobjects_queried"] += 1
                stats["fields_total"] += len(rows)
                stats["free_text_fields"] += sum(
                    1
                    for r in rows
                    if r.get("DataType", "").startswith(
                        ("Long Text Area", "Rich Text Area", "Text Area")
                    )
                )
        if not by_sobject:
            stats["error"] = "no_sobjects_returned_data"
            return stats
        content = render_schema_cache(portco_key, by_sobject)
        stats["path"] = write_schema_cache(portco_key, content)
    except Exception as exc:
        log.exception(
            "schema_introspection: portco=%s failed",
            portco_key,
        )
        stats["error"] = f"{type(exc).__name__}: {exc}"
    return stats
