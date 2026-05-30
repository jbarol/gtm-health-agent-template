-- Plan #42 PR1 — add ``outcome`` column to session_costs.
--
-- Background. The original ``bin/measure-deploy-risk.py`` design (Plan #42
-- v1) referenced a "manual incident log" that didn't exist. To make the
-- measurement script actually compute Acme error rates per hour, the
-- ledger needs a per-row outcome label that callers can compute at session
-- terminal-state time and the histogram script can SELECT directly.
--
-- This migration is purely additive. Existing rows default to ``success``.
-- If the ``raw_usage_json`` forensics column exists (Plan #35 §spec line 91 —
-- created on installs that ran the full Plan #35 INSERT path; absent on
-- installs that predate it), backfill rows whose JSON carries one of the
-- audit-trail failure markers documented in CLAUDE.md
-- (``[WRITING_AGENT_FALLTHROUGH]``, ``[SURFACE_PUSH_FAILED]``) or an
-- explicit ``error_summary`` JSON field to ``error``. The DO block makes
-- the backfill skip cleanly on installs without the column instead of
-- aborting the migration. Other terminal states (``abandoned``,
-- ``recovered``) are added forward-only by the orchestrator.
--
-- Safe to re-run. ``ADD COLUMN IF NOT EXISTS`` short-circuits if the
-- column is already present, and the UPDATE only flips rows still at the
-- default ``success`` value.

ALTER TABLE session_costs
    ADD COLUMN IF NOT EXISTS outcome TEXT NOT NULL DEFAULT 'success';

CREATE INDEX IF NOT EXISTS idx_session_costs_outcome
    ON session_costs(outcome, recorded_at DESC);

-- Backfill: only run if the optional ``raw_usage_json`` column exists.
-- Substring matching against ``::TEXT`` is safe regardless of whether the
-- column is stored as TEXT, JSON, or JSONB across environments.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = 'session_costs'
          AND column_name = 'raw_usage_json'
    ) THEN
        EXECUTE $sql$
            UPDATE session_costs
            SET outcome = 'error'
            WHERE outcome = 'success'
              AND raw_usage_json IS NOT NULL
              AND (
                  raw_usage_json::TEXT LIKE '%[WRITING_AGENT_FALLTHROUGH]%'
                  OR raw_usage_json::TEXT LIKE '%[SURFACE_PUSH_FAILED]%'
                  OR raw_usage_json::TEXT LIKE '%"error_summary":%'
              )
        $sql$;
    END IF;
END$$;
