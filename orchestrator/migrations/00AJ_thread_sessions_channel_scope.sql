-- Plan: scope thread→session lookups by channel_id so multi-portco safety holds.
--
-- Background. ``thread_sessions`` stores the Slack thread→Anthropic session
-- mapping that lets follow-up messages in a thread reuse the existing agent
-- session across container restarts. The original schema keyed only on
-- ``thread_ts`` because the bot ran in a single Slack channel for a single
-- portco. ``thread_ts`` is NOT globally unique across Slack workspaces or
-- channels — Slack generates each timestamp per-channel — so once a second
-- portco-scoped channel comes online, two unrelated threads with identical
-- ``thread_ts`` values would collide. Worse, the lookup would resolve the
-- wrong portco's session into the new thread, cross-pollinating portco
-- context across customers.
--
-- The fix is a composite primary key on ``(channel_id, thread_ts)``. Codex
-- flagged this on the original thread persistence work; this migration is
-- the long-promised follow-up before the multi-portco rollout.
--
-- Backfill strategy (codex P2 follow-up). Two passes, evidence-first:
--
--   Pass 1 — join ``investigations`` on ``thread_ts`` and copy the real
--   ``channel_id``. The investigations table has carried ``channel_id``
--   since the first cut, so any row that was minted via an ad-hoc
--   investigation (the only path that writes ``thread_sessions`` today)
--   has a corresponding investigation row with the truth. This recovers
--   DM threads and any non-master channel rows that the original
--   hardcoded backfill would have mis-scoped.
--
--   Pass 2 — for any row that ``investigations`` does NOT cover, fall
--   back to ``C0000000000`` (Acme's master channel). Today's prod
--   has every active row tied to that channel; this fallback covers the
--   edge case of a thread_sessions row whose matching investigation
--   was already TTL-evicted from the 7-day window. False positives are
--   bounded: the master channel is the only active portco channel, so
--   stamping an unknown row to it gives at most a one-time bad lookup
--   that gets recreated on the next Slack message.
--
-- Going forward, ``save_thread_session`` requires ``channel_id`` so
-- future portcos cannot write a NULL row. The hardcode never re-runs
-- after the column is set NOT NULL — the PK constraint enforces it.
--
-- The transaction wraps DROP + ADD so the table is never in a state without
-- a primary key. ``IF EXISTS`` clauses keep the migration idempotent.

BEGIN;

-- 1. Ensure the column exists. It was added incidentally on some installs
--    via newer save_thread_session payloads, so use IF NOT EXISTS.
ALTER TABLE thread_sessions
    ADD COLUMN IF NOT EXISTS channel_id TEXT;

-- 2a. Evidence-first backfill from ``investigations``. The DISTINCT ON
--     picks the most recent investigation per thread_ts so that if a
--     thread moved between channels (rare but possible) the freshest
--     channel wins. Skip rows already populated by a prior partial run.
UPDATE thread_sessions ts
    SET channel_id = inv.channel_id
    FROM (
        SELECT DISTINCT ON (thread_ts)
            thread_ts,
            channel_id
        FROM investigations
        WHERE thread_ts IS NOT NULL
          AND channel_id IS NOT NULL
        ORDER BY thread_ts, started_at DESC
    ) inv
    WHERE ts.channel_id IS NULL
      AND ts.thread_ts = inv.thread_ts;

-- 2b. Fallback: any orphan row (no matching investigation) gets the
--     single active portco channel. On fresh installs (empty table)
--     this is a no-op.
UPDATE thread_sessions
    SET channel_id = 'C0000000000'
    WHERE channel_id IS NULL;

-- 3. Enforce NOT NULL now that every row has a channel_id.
ALTER TABLE thread_sessions
    ALTER COLUMN channel_id SET NOT NULL;

-- 4. Drop the old primary key (``thread_ts`` alone). The constraint name
--    is the PostgreSQL default: ``<table>_pkey``.
ALTER TABLE thread_sessions
    DROP CONSTRAINT IF EXISTS thread_sessions_pkey;

-- 5. Install the composite primary key. This is the load-bearing line:
--    two rows sharing a ``thread_ts`` across different channels are now
--    valid; a duplicate ``(channel_id, thread_ts)`` is rejected.
ALTER TABLE thread_sessions
    ADD CONSTRAINT thread_sessions_pkey
    PRIMARY KEY (channel_id, thread_ts);

COMMIT;
