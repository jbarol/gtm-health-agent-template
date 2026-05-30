"""Backfill the product_line column in Postgres for existing opportunities.

The nightly Salesforce → Postgres sync started writing
``opportunities.product_line`` from snapshot #15 onward (after
``feat/sync-product-line`` landed and migration
``00AQ_opp_product_line.sql`` was applied). Snapshots #14 and earlier
have NULL in the column.

This script is opt-in: most analyses only need go-forward coverage. Run
it manually when an operator needs the column populated on a specific
historical snapshot (e.g., to repeat the wtaylor-style ``Industry ×
Product Line`` cross-cut against a prior point-in-time).

Usage:
    # Backfill every existing Opportunity in every portco, every snapshot
    python bin/backfill_opportunity_product_line.py

    # One portco
    python bin/backfill_opportunity_product_line.py --portco acme

    # One snapshot (e.g., the wtaylor snapshot)
    python bin/backfill_opportunity_product_line.py --snapshot-id 14

Idempotent — safe to re-run. Reads the live SF value at run time, so an
opp whose Product_Line__c has been edited since the original snapshot
will get the *current* value, not the historical one. That trade-off is
deliberate: SF does not retain a snapshot history of the field, so any
backfill is necessarily best-effort against today's data. Snapshots are
append-only point-in-time records of the rest of the row, but
``product_line`` will reflect "as of this backfill" rather than "as of
this snapshot's original sync date." Document that caveat with the
operator if they're using this for forensics.

Why this exists:
    The wtaylor incident (2026-05-18) showed that a sub-agent (sub3)
    used SF's ``Product_Line__c`` custom field via live MCP because
    Postgres did not carry the column. Every cross-cut paid a fresh
    SF query cost. ``feat/sync-product-line`` made the column
    first-class going forward; this script is the historical-fill
    escape hatch for snapshots that pre-date the change.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# Make ``orchestrator/`` importable regardless of cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "orchestrator"))

import db_adapter  # noqa: E402
from portco_registry import get_all_portcos  # noqa: E402
from session_runner import _get_sf_client  # noqa: E402

log = logging.getLogger("backfill_opportunity_product_line")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

BATCH_SIZE = 200

CUSTOM_FIELDS = ("Id", "Product_Line__c")


def _chunks(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _fetch_sf_records(sf, sf_ids: list[str]) -> dict[str, dict]:
    """Return {sf_id: {field: value}} for the given Opportunity ids."""
    if not sf_ids:
        return {}

    quoted = ",".join(f"'{sid}'" for sid in sf_ids)
    soql = (
        f"SELECT {', '.join(CUSTOM_FIELDS)} FROM Opportunity WHERE Id IN ({quoted})"
    )
    result = sf.query(soql)
    records = list(result.get("records", []))
    while not result.get("done", True):
        result = sf.query_more(result["nextRecordsUrl"], identifier_is_url=True)
        records.extend(result.get("records", []))
    return {r["Id"]: r for r in records if r.get("Id")}


def _backfill_portco(portco_key: str, snapshot_id: int | None) -> tuple[int, int]:
    """Backfill one portco. Returns (updated_count, missing_count).

    If ``snapshot_id`` is given, only opps in that snapshot are touched.
    Otherwise every opp for the portco gets the live Product_Line__c
    written into its row regardless of which snapshot it belongs to.
    """
    log.info(
        "Starting backfill for portco=%s snapshot_id=%s",
        portco_key,
        snapshot_id if snapshot_id is not None else "ALL",
    )

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
            if snapshot_id is not None:
                cur.execute(
                    "SELECT DISTINCT sf_id FROM opportunities "
                    "WHERE portco_key = %s AND snapshot_id = %s "
                    "AND sf_id IS NOT NULL",
                    (portco_key, snapshot_id),
                )
            else:
                cur.execute(
                    "SELECT DISTINCT sf_id FROM opportunities "
                    "WHERE portco_key = %s AND sf_id IS NOT NULL",
                    (portco_key,),
                )
            sf_ids = [row[0] for row in cur.fetchall()]

        log.info(
            "Found %d distinct opportunities in Postgres for %s",
            len(sf_ids),
            portco_key,
        )

        for batch in _chunks(sf_ids, BATCH_SIZE):
            records = _fetch_sf_records(sf, batch)
            with conn.cursor() as cur:
                for sf_id in batch:
                    rec = records.get(sf_id)
                    if rec is None:
                        missing += 1
                        continue
                    if snapshot_id is not None:
                        cur.execute(
                            "UPDATE opportunities SET product_line = %s "
                            "WHERE sf_id = %s AND portco_key = %s "
                            "AND snapshot_id = %s",
                            (
                                rec.get("Product_Line__c"),
                                sf_id,
                                portco_key,
                                snapshot_id,
                            ),
                        )
                    else:
                        cur.execute(
                            "UPDATE opportunities SET product_line = %s "
                            "WHERE sf_id = %s AND portco_key = %s",
                            (
                                rec.get("Product_Line__c"),
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
    parser.add_argument(
        "--snapshot-id",
        type=int,
        default=None,
        help=(
            "Restrict backfill to one snapshot_id (default: every snapshot "
            "for the portco). The current SF Product_Line__c value is written "
            "regardless of the snapshot's original sync date — see module "
            "docstring for the as-of caveat."
        ),
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
    results: list[tuple[str, str, str]] = []
    for key in keys:
        try:
            u, m = _backfill_portco(key, args.snapshot_id)
            total_updated += u
            total_missing += m
            results.append((key, "SUCCESS", f"{u} rows updated"))
        except Exception as exc:
            log.exception("Portco %s failed — continuing with next", key)
            results.append((key, "FAILED", f"{type(exc).__name__}: {exc}"))

    failed = [r for r in results if r[1] == "FAILED"]

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

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
