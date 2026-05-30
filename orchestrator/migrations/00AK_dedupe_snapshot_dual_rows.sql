-- Migration 00AK — dedupe historical dual-row snapshots (Task #9).
--
-- Background. ``create_snapshot`` upserts on (portco_key, snapshot_date)
-- and re-returns the existing ``snapshot_id`` for the day. Before today's
-- fix, ``write_records`` INSERTed unconditionally — so a same-day re-run
-- (e.g. RUN_NIGHTLY_NOW=1 in the afternoon after the 08:29 UTC cron)
-- appended a second copy of every row alongside the morning's batch.
--
-- Observed on 2026-05-14: opportunities held 345,847 rows where 172,936
-- were expected. The morning rows carry NULL ``account_id`` (pre-PR #170,
-- merged 2026-05-14 morning), the afternoon rows carry the populated
-- column. Any consumer doing ``SELECT ... FROM opportunities WHERE
-- snapshot_id = <latest>`` sees the duplicates and aggregations
-- double-count.
--
-- Fix going forward: ``write_records`` now DELETEs rows for the target
-- (snapshot_id, portco_key, table) before re-inserting, wrapped in a
-- single transaction.
--
-- This migration is the one-shot cleanup for the historical dual-row
-- state. For each (snapshot_id, sf_id) in each of the four snapshot
-- tables, keep the row with the MAX(id). MAX(id) is the most recent
-- INSERT, which carries the most-complete columns (e.g. populated
-- ``account_id`` for opportunities). Older partial-fill rows get
-- dropped.
--
-- Strategy notes:
--   * One DELETE per table inside a single transaction. If any DELETE
--     fails the whole migration rolls back and ``schema_migrations`` is
--     not stamped, so the next boot retries cleanly.
--   * NULL ``sf_id`` should not exist (table column constraint), but
--     PARTITION BY tolerates it — NULL groups dedupe within themselves.
--   * Use ROW_NUMBER() OVER (PARTITION BY snapshot_id, sf_id ORDER BY
--     id DESC) and DELETE rows with rn > 1. One sequential scan plus
--     one sort per table (O(n log n)). Sub-second on the 345k-row
--     opportunities table — well under Railway's /ready 1m30s
--     healthcheck window. The earlier
--     ``id NOT IN (SELECT MAX(id) GROUP BY snapshot_id, sf_id)``
--     pattern degraded to a hash anti-join over the full table and
--     timed out the deploy on 2026-05-14 16:19 PT, leaving the
--     migration perpetually in-flight (txn rollback on container
--     kill).
--   * Idempotent by construction: after the first run every
--     (snapshot_id, sf_id) partition has exactly one row, every row
--     has rn = 1, and the DELETE matches nothing on re-run.

BEGIN;

DELETE FROM opportunities o
USING (
    SELECT id, ROW_NUMBER() OVER (
        PARTITION BY snapshot_id, sf_id ORDER BY id DESC
    ) AS rn
    FROM opportunities
) r
WHERE o.id = r.id AND r.rn > 1;

DELETE FROM leads l
USING (
    SELECT id, ROW_NUMBER() OVER (
        PARTITION BY snapshot_id, sf_id ORDER BY id DESC
    ) AS rn
    FROM leads
) r
WHERE l.id = r.id AND r.rn > 1;

DELETE FROM contacts c
USING (
    SELECT id, ROW_NUMBER() OVER (
        PARTITION BY snapshot_id, sf_id ORDER BY id DESC
    ) AS rn
    FROM contacts
) r
WHERE c.id = r.id AND r.rn > 1;

DELETE FROM accounts a
USING (
    SELECT id, ROW_NUMBER() OVER (
        PARTITION BY snapshot_id, sf_id ORDER BY id DESC
    ) AS rn
    FROM accounts
) r
WHERE a.id = r.id AND r.rn > 1;

COMMIT;
