#!/usr/bin/env python3
"""One-shot backfill of trailing 30 days of Anthropic cost data.

Calls ``cost_collector.pull_anthropic_daily_costs`` for the trailing N-day
window. Idempotent thanks to the upsert keyed by
``(bucket_date, model, workspace_id, service_tier)`` — re-runs overwrite
prior rows with the latest Anthropic-reported numbers.

The Admin API ``usage_report`` + ``cost_report`` endpoints both accept a
``starting_at``/``ending_at`` window, so a single call paginates through
the whole 30-day window. Today (UTC start-of-day) is excluded because the
day is still in flight and Anthropic billing data is incomplete until the
next pull at 06:00 Pacific.

Run after deploying Plan #35 with ``ANTHROPIC_ADMIN_KEY`` set in the env::

    python scripts/cost_backfill_30d.py

Options::

    --days N      Override the default 30-day window. Anthropic's 1d bucket
                  cap is 31, so values > 31 are not honoured by the API.
    --dry-run     Resolve env + window, log what *would* happen, exit 0
                  without hitting Anthropic or Postgres.

Required env (loaded via ``orchestrator/config.py``):
    ANTHROPIC_ADMIN_KEY   Admin API key (``sk-ant-admin...``).
    DATABASE_URL          Railway Postgres connection string.

Exit codes:
    0   Success or dry-run.
    1   Missing required env, or Admin API / DB error from the pull.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Make the orchestrator package importable without `pip install -e`. Mirrors
# the import pattern used by the rest of the orchestrator (everything sits
# flat in orchestrator/, no package init), so we just prepend that directory
# to sys.path before importing config / cost_collector / db_adapter.
_ORCHESTRATOR_DIR = Path(__file__).resolve().parent.parent / "orchestrator"
sys.path.insert(0, str(_ORCHESTRATOR_DIR))

import config  # noqa: E402  — must come after sys.path mutation
import cost_collector  # noqa: E402
import db_adapter  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("cost_backfill_30d")

# Anthropic's Admin Usage & Cost API caps daily-bucket windows at 31 days
# per https://platform.claude.com/docs/en/manage-claude/usage-cost-api.
# We don't enforce a hard cap (Anthropic returns an error if exceeded);
# we just warn so the operator knows what to expect.
MAX_DAILY_BUCKETS = 31

# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────


def _expected_dates(days_back: int) -> list[str]:
    """Return the list of UTC ``YYYY-MM-DD`` dates that the pull should cover.

    Matches the same window logic used inside
    ``cost_collector.pull_anthropic_daily_costs``: end of window is
    start-of-today UTC, start of window is ``days_back`` days earlier.
    Today is excluded (incomplete data).
    """
    now_utc = datetime.now(timezone.utc)
    end = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    return [
        (end - timedelta(days=i + 1)).strftime("%Y-%m-%d") for i in range(days_back)
    ]


def _query_rows_per_date(expected_dates: list[str]) -> dict[str, int]:
    """Return ``{date: row_count}`` for the expected dates from ``anthropic_daily_costs``.

    Used only for the end-of-run summary so the operator can see which dates
    actually have data. Returns an empty dict when the DB is unreachable
    (the pull itself already errored out in that case).
    """
    if not db_adapter.DATABASE_URL:
        return {}
    counts: dict[str, int] = {d: 0 for d in expected_dates}
    try:
        conn = db_adapter._connect()
        try:
            with conn.cursor() as cur:
                # Single round-trip: ask for all expected dates at once.
                cur.execute(
                    "SELECT bucket_date::text, COUNT(*) "
                    "FROM anthropic_daily_costs "
                    "WHERE bucket_date::text = ANY(%s) "
                    "GROUP BY bucket_date",
                    (expected_dates,),
                )
                for bucket_date, count in cur.fetchall():
                    counts[bucket_date] = int(count)
        finally:
            conn.close()
    except Exception:
        log.exception("Failed to query per-date row counts; summary will be partial")
    return counts


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="One-shot 30-day backfill of Anthropic daily cost data."
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Days of history to pull (default 30). Anthropic caps daily "
        "buckets at 31; larger values may be silently truncated.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve env + window, log what would happen, exit without "
        "hitting Anthropic or Postgres.",
    )
    return parser.parse_args()


# ──────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────


def main() -> int:
    args = _parse_args()

    if args.days <= 0:
        log.error("--days must be a positive integer (got %d)", args.days)
        return 1
    if args.days > MAX_DAILY_BUCKETS:
        log.warning(
            "--days=%d exceeds Anthropic's documented 1d bucket cap of %d; "
            "the Admin API may truncate or reject the request",
            args.days,
            MAX_DAILY_BUCKETS,
        )

    expected_dates = _expected_dates(args.days)
    start_iso = expected_dates[-1] if expected_dates else "?"
    end_iso = expected_dates[0] if expected_dates else "?"
    log.info(
        "Backfill window: %s → %s (%d days, today excluded)",
        start_iso,
        end_iso,
        args.days,
    )

    # Surface env state up front. The pull function would log these too, but
    # printing them at the top of the script's output makes a dry-run useful.
    admin_key_present = bool(config.ANTHROPIC_ADMIN_KEY)
    db_url_present = bool(db_adapter.DATABASE_URL)
    log.info(
        "Env check: ANTHROPIC_ADMIN_KEY %s, DATABASE_URL %s",
        "set" if admin_key_present else "UNSET",
        "set" if db_url_present else "UNSET",
    )

    if args.dry_run:
        log.info("--dry-run: skipping pull and DB writes; exiting 0")
        return 0

    if not admin_key_present:
        log.error(
            "ANTHROPIC_ADMIN_KEY is unset — cannot backfill. Set it in the "
            "environment and re-run."
        )
        return 1
    if not db_url_present:
        log.error(
            "DATABASE_URL is unset — cannot persist backfill. Set it in the "
            "environment and re-run."
        )
        return 1

    log.info(
        "Calling cost_collector.pull_anthropic_daily_costs(days_back=%d)", args.days
    )
    rows_upserted = cost_collector.pull_anthropic_daily_costs(days_back=args.days)
    log.info("Pull returned %d upserted row(s)", rows_upserted)

    # Per-date breakdown — pulled from the DB so we report what's actually
    # persisted (which can be less than the requested window if Anthropic
    # had no traffic on some days, or if data arrived after the pull).
    per_date_counts = _query_rows_per_date(expected_dates)
    dates_with_data = sorted(d for d, n in per_date_counts.items() if n > 0)
    dates_skipped = sorted(d for d, n in per_date_counts.items() if n == 0)

    log.info("─" * 60)
    log.info("Backfill summary")
    log.info("  Window:           %s → %s (%d days)", start_iso, end_iso, args.days)
    log.info("  Rows upserted:    %d (this run)", rows_upserted)
    log.info(
        "  Dates with data:  %d  %s", len(dates_with_data), dates_with_data or "(none)"
    )
    log.info(
        "  Dates with no data: %d  %s",
        len(dates_skipped),
        dates_skipped or "(none)",
    )
    if rows_upserted == 0 and not dates_with_data:
        log.warning(
            "No rows were upserted and no existing rows were found for the "
            "window. Verify ANTHROPIC_ADMIN_KEY has access to the org's "
            "cost data and that the window has billable traffic."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
