-- Migration 00AI — make session_costs.session_id UNIQUE + dedupe historical rows.
--
-- Background:
--   client.beta.sessions.retrieve(session_id).usage returns CUMULATIVE
--   counts across all turns in the session. Slack thread follow-ups
--   reuse the same Managed Agents session, and _log_session_usage fires
--   once per turn. The original Plan #35 schema had only PRIMARY KEY (id
--   BIGSERIAL) and the INSERT had no ON CONFLICT clause, so each follow-up
--   appended a new row with a higher cumulative — aggregations across
--   session_costs over-count token spend by N-1 turns per multi-turn
--   session.
--
--   The intent (docs/plans/35-cost-tracking-and-reporting.md:94) was a
--   UNIQUE index on session_id; it shipped only in the design doc, not
--   in the actual ensure_schema DDL. This migration repairs that and the
--   orchestrator switches to INSERT ... ON CONFLICT (session_id) DO
--   UPDATE SET ... so each session has exactly one row carrying the
--   final cumulative.
--
-- Strategy:
--   1. Within a single transaction, dedupe by keeping MAX(id) per
--      session_id. MAX(id) is the latest INSERT for that session, which
--      carries the highest cumulative — that's the row we want.
--   2. Add a UNIQUE index on session_id. CREATE UNIQUE INDEX IF NOT
--      EXISTS is idempotent — safe to re-run.

BEGIN;

DELETE FROM session_costs
WHERE id NOT IN (
    SELECT MAX(id)
    FROM session_costs
    GROUP BY session_id
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_session_costs_session_id_unique
    ON session_costs(session_id);

COMMIT;
