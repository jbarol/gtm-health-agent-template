-- Phase 1, PR 1 of the autonomous ❌-Watcher Managed Agent.
-- Design doc: docs/proposals/watcher-design-20260521-210800.md (APPROVED 2026-05-26).
--
-- ``watcher_pending`` is the debounce + dispatch queue for the watcher.
-- The lifecycle.terminalize_lifecycle hook enqueues one row per ❌
-- (filtered by the recursion guard ``agent_id != WATCHER_AGENT_ID``);
-- the APScheduler cron polls this table and the WatcherThreadPoolExecutor
-- claims pending rows and dispatches a Managed Agent investigation.
--
-- Restart survivability: the queue lives in Postgres, not memory. On
-- startup, the catch-up sweep back-fills rows for investigations
-- terminalized in the last 30 min that lack a matching watcher_pending
-- row.
--
-- Deduplication: the partial unique index on
-- (error_message_hash) WHERE status='pending' means two ❌s with the
-- same normalized error message will collapse into a single pending row;
-- the second enqueue increments ``repeat_count`` via ON CONFLICT.
--
-- Status state machine:
--     pending           — enqueued, awaiting worker pickup
--     running           — claimed by a worker, investigation in flight
--     completed         — investigation done, PR opened (or diagnose-only)
--     transient_skipped — upstream 5xx/rate-limit shortcut, requeue
--     failed_retry      — sessions.create failed, attempts < 4
--     abandoned         — attempts == 4 with no success; admin DMed
--     diagnose_only     — fix area outside allowlist OR GH PAT 401, no PR
--
-- Safe to re-run. Every DDL is IF NOT EXISTS or ADD COLUMN IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS watcher_pending (
    id BIGSERIAL PRIMARY KEY,
    inv_id INTEGER REFERENCES investigations(id) ON DELETE SET NULL,
    channel_id TEXT,
    thread_ts TEXT,
    error_category TEXT,
    error_message_hash TEXT NOT NULL,
    -- All source inv_ids that have collapsed onto this pending row via
    -- the partial unique index on (error_message_hash) WHERE status='pending'.
    -- Without this, the startup catch-up sweep can't tell that an inv was
    -- already covered by an existing pending row whose inv_id field carries
    -- a different (earlier) source — it would re-enqueue a duplicate watcher
    -- run after the first row transitions off 'pending'. inv_id (singular)
    -- preserves the first source for backward-compat with code that reads
    -- one ID; source_inv_ids is the canonical full list.
    source_inv_ids INTEGER[] NOT NULL DEFAULT ARRAY[]::INTEGER[],
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    repeat_count INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TIMESTAMPTZ,
    catch_up BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- GIN index on source_inv_ids — needed for the catch-up sweep's
-- ``i.id = ANY(wp.source_inv_ids)`` predicate to stay fast at scale.
CREATE INDEX IF NOT EXISTS idx_watcher_pending_source_inv_ids
    ON watcher_pending USING GIN (source_inv_ids);

-- Partial unique index for natural deduplication of pending and running rows.
-- Two ❌s with the same normalized error hash collapse into one pending
-- row; the second enqueue uses ON CONFLICT DO UPDATE to bump
-- repeat_count. The index covers both 'pending' AND 'running' so that
-- an identical error arriving while the first investigation is still in
-- flight also collapses rather than spawning a concurrent duplicate job.
-- Once status flips to a terminal state (completed/failed_retry/etc),
-- a fresh ❌ with the same hash can re-enqueue — that is the intended
-- semantic so we don't lose signal on a recurrence after the first fix
-- shipped.
CREATE UNIQUE INDEX IF NOT EXISTS idx_watcher_pending_hash_pending
    ON watcher_pending(error_message_hash)
    WHERE status IN ('pending', 'running');

-- The scheduler scans by (status, created_at) every 30s; this index
-- covers the dequeue path.
CREATE INDEX IF NOT EXISTS idx_watcher_pending_status_created
    ON watcher_pending(status, created_at);

-- Catch-up sweep on startup uses (status, inv_id) to find pending rows
-- and to detect investigations missing a row.
CREATE INDEX IF NOT EXISTS idx_watcher_pending_inv
    ON watcher_pending(inv_id);

-- Auto-update updated_at on row mutation. Keeps the audit trail honest
-- without forcing every db_adapter accessor to remember to bump it.
CREATE OR REPLACE FUNCTION watcher_pending_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_watcher_pending_updated_at ON watcher_pending;
CREATE TRIGGER trg_watcher_pending_updated_at
    BEFORE UPDATE ON watcher_pending
    FOR EACH ROW
    EXECUTE FUNCTION watcher_pending_set_updated_at();
