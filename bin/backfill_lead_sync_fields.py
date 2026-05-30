"""Backfill the 4 Lead custom-field columns in Postgres for existing rows.

Reads each portco from portco_config.json. For each portco with SF
configured, queries SF for every Lead the portco has in Postgres,
fetches the 4 custom fields, and UPDATEs the Postgres row.

Idempotent — safe to re-run. Logs each portco's update count.

Usage:
    python bin/backfill_lead_sync_fields.py                   # all portcos
    python bin/backfill_lead_sync_fields.py --portco acme # one portco

Why this exists:
    Before fix/lead-sync-schema, the nightly Lead sync SELECT clause did
    not pull Discovery_Call_Booked__c / Funnel_Stage__c /
    MQL_SDR_Accepted_Date_Time__c / SDR_Qualified_Date_Time__c. The
    INSERT referenced three of those fields, so the columns were
    populated with NULL on every existing snapshot row. This script
    rewrites those columns from live SF for every Lead currently in
    the leads table. The new column ``discovery_call_booked`` is also
    backfilled by the same SOQL fetch.

    Z2 (Railway redeploy) applies the migration; Z4 runs this script.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# Make `orchestrator/` importable regardless of cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "orchestrator"))

import db_adapter  # noqa: E402
from portco_registry import get_all_portcos  # noqa: E402
from session_runner import _get_sf_client  # noqa: E402

log = logging.getLogger("backfill_lead_sync_fields")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

BATCH_SIZE = 200

CUSTOM_FIELDS = (
    "Id",
    "Discovery_Call_Booked__c",
    "Funnel_Stage__c",
    "MQL_SDR_Accepted_Date_Time__c",
    "SDR_Qualified_Date_Time__c",
)


def _chunks(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _fetch_sf_records(sf, sf_ids: list[str]) -> dict[str, dict]:
    """Return {sf_id: {field: value}} for the given Lead ids."""
    if not sf_ids:
        return {}

    quoted = ",".join(f"'{sid}'" for sid in sf_ids)
    soql = f"SELECT {', '.join(CUSTOM_FIELDS)} FROM Lead WHERE Id IN ({quoted})"
    result = sf.query(soql)
    records = list(result.get("records", []))
    while not result.get("done", True):
        result = sf.query_more(result["nextRecordsUrl"], identifier_is_url=True)
        records.extend(result.get("records", []))
    return {r["Id"]: r for r in records if r.get("Id")}


def _backfill_portco(portco_key: str) -> tuple[int, int]:
    """Backfill one portco. Returns (updated_count, missing_count)."""
    log.info("Starting backfill for portco=%s", portco_key)

    try:
        sf = _get_sf_client(portco_key)
    except RuntimeError as exc:
        log.warning("Skipping %s — SF client unavailable: %s", portco_key, exc)
        return 0, 0

    conn = db_adapter._connect()
    updated = 0
    missing = 0
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT sf_id FROM leads "
                "WHERE portco_key = %s AND sf_id IS NOT NULL",
                (portco_key,),
            )
            sf_ids = [row[0] for row in cur.fetchall()]

        log.info("Found %d distinct leads in Postgres for %s", len(sf_ids), portco_key)

        for batch in _chunks(sf_ids, BATCH_SIZE):
            records = _fetch_sf_records(sf, batch)
            with conn.cursor() as cur:
                for sf_id in batch:
                    rec = records.get(sf_id)
                    if rec is None:
                        missing += 1
                        continue
                    cur.execute(
                        "UPDATE leads SET "
                        "discovery_call_booked = %s, "
                        "funnel_stage = %s, "
                        "mql_date = %s, "
                        "sql_date = %s "
                        "WHERE sf_id = %s AND portco_key = %s",
                        (
                            rec.get("Discovery_Call_Booked__c"),
                            rec.get("Funnel_Stage__c"),
                            rec.get("MQL_SDR_Accepted_Date_Time__c"),
                            rec.get("SDR_Qualified_Date_Time__c"),
                            sf_id,
                            portco_key,
                        ),
                    )
                    updated += cur.rowcount
            conn.commit()
            log.info(
                "  batch committed — running totals: updated=%d, missing=%d",
                updated,
                missing,
            )
    except Exception:
        conn.rollback()
        log.exception("Backfill failed for %s — rolled back open batch", portco_key)
        raise
    finally:
        conn.close()

    log.info(
        "Completed backfill for %s — updated=%d rows, missing=%d ids",
        portco_key,
        updated,
        missing,
    )
    return updated, missing


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--portco",
        help="Backfill a single portco (default: all active portcos).",
    )
    args = parser.parse_args()

    if not os.environ.get("DATABASE_URL"):
        log.error("DATABASE_URL not set — refusing to run")
        return 2

    if args.portco:
        keys = [args.portco]
    else:
        keys = [p["key"] for p in get_all_portcos()]

    if not keys:
        log.warning("No portcos to process — exiting")
        return 0

    total_updated = 0
    total_missing = 0
    # Per-portco status list, in execution order, so the summary prints a
    # stable, machine-greppable line per portco. Each item is
    # (key, "SUCCESS" | "FAILED", detail). detail is a row count for the
    # success path and an error string for the failure path.
    results: list[tuple[str, str, str]] = []
    for key in keys:
        try:
            u, m = _backfill_portco(key)
            total_updated += u
            total_missing += m
            results.append((key, "SUCCESS", f"{u} rows updated"))
        except Exception as exc:
            log.exception("Portco %s failed — continuing with next", key)
            results.append((key, "FAILED", f"{type(exc).__name__}: {exc}"))

    failed = [r for r in results if r[1] == "FAILED"]

    # Print a clean human + machine-readable summary so operators and
    # automation can tell a partial failure from a clean run. Codex review
    # PR #96 P2: a previous run swallowed exceptions and exited 0 even on
    # full failure, leaving historical Lead columns unfilled with no
    # detectable error state.
    log.info("Backfill summary:")
    for key, status, detail in results:
        log.info("  %s: %s (%s)", key, status, detail)
    log.info(
        "%d portcos processed, %d failed.",
        len(results),
        len(failed),
    )
    log.info(
        "Totals — updated=%d rows, missing=%d ids",
        total_updated,
        total_missing,
    )

    # Exit nonzero if any portco failed. Single-target runs (--portco) get
    # the same treatment — a single failure exits with code 1 so the
    # caller's automation can react.
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
