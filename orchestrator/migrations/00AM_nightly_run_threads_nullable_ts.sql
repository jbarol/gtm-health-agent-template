-- Task #22 — DB-first ordering for the slack_thread_registry.
--
-- The PR #184 flow (post Slack → cache → persist) had a P2 race:
-- if persist failed AND the container restarted before the next call
-- for the same (run_id, theme, channel_id) key, the in-memory cache
-- evaporated and the DB had no row, so a SECOND parent message got
-- posted and the theme's artifacts forked across two threads.
--
-- The fix is "DB-first": INSERT a placeholder row (thread_ts=NULL)
-- atomically, then post Slack, then UPDATE the placeholder with the
-- real ts. ON CONFLICT (run_id, theme, channel_id) DO NOTHING tells
-- us whether WE won the claim; if we lost, we re-SELECT and wait
-- briefly for the winning writer to land the real ts.
--
-- That ordering requires:
--   1. thread_ts must be NULLABLE so the placeholder can sit in the
--      table while the Slack call is in flight.
--   2. A timestamp column we can use to sweep orphan placeholders
--      whose claiming process crashed between INSERT and the UPDATE
--      (so the row sits NULL forever blocking the next legitimate
--      retry on the same key). A 15-minute cron in main.py reaps
--      placeholders older than max_age_minutes.
--
-- Idempotent: re-running this migration is a no-op thanks to the
-- IF EXISTS / IF NOT EXISTS clauses and the conditional DROP NOT NULL
-- (Postgres treats DROP NOT NULL on an already-nullable column as a
-- no-op without an error).

ALTER TABLE IF EXISTS nightly_run_threads
    ALTER COLUMN thread_ts DROP NOT NULL;

ALTER TABLE IF EXISTS nightly_run_threads
    ADD COLUMN IF NOT EXISTS placeholder_created_at TIMESTAMPTZ DEFAULT NOW();
