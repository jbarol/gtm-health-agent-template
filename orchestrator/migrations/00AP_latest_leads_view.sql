-- Plan: Design H (2026-05-15) — latest_leads view to scope queries to MAX snapshot.
--
-- Symptom: sesn_EXAMPLE noticed Postgres returned 60,343 rows where SF
-- returned 30,172 for what looked like the same query. The Postgres ``leads``
-- table accumulates rows across nightly snapshots (snapshot_id increments each
-- run) and the schema_cache.md guidance to "query leads" let an agent join
-- across historical snapshots. The ~2x ratio matched 2 snapshots: yesterday
-- + today.
--
-- This view materializes the latest-snapshot subset per portco so any
-- ``SELECT ... FROM latest_leads WHERE portco_key = '<pk>'`` lands a single
-- snapshot's rows. Specialists in this codebase MUST prefer ``latest_leads``
-- over raw ``leads`` for "current state" questions.
--
-- The raw ``leads`` table stays untouched — historical questions still work
-- ("show me the count by status on 2026-05-10" still hits ``leads`` filtered
-- by snapshot_id). Only the default-current-state path moves.
--
-- Read-only. Idempotent. Safe to re-apply.

CREATE OR REPLACE VIEW latest_leads AS
SELECT l.*
FROM leads l
JOIN (
    SELECT portco_key, MAX(snapshot_id) AS latest_snapshot_id
    FROM leads
    GROUP BY portco_key
) latest
  ON l.portco_key = latest.portco_key
 AND l.snapshot_id = latest.latest_snapshot_id;

COMMENT ON VIEW latest_leads IS 'Plan: Design H (2026-05-15). Per-portco latest snapshot of leads. Specialists should prefer this view over raw leads for current-state questions to avoid the 2x-row accumulation bug observed on 2026-05-15 (sesn_EXAMPLE: 60,343 Postgres rows vs 30,172 SF live).';
