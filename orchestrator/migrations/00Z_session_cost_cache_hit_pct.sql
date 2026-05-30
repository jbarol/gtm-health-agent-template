-- Plan #35 cost-tracking follow-up — add ``cache_hit_pct`` to session_costs.
--
-- Background. The ``cost_rollup_daily`` view documented in Plan #35
-- (docs/plans/35-cost-tracking-and-reporting.md, line 86) was supposed to
-- expose ``ROUND(AVG(cache_hit_pct), 1)`` per (portco, day, model). The view
-- in db_adapter.ensure_schema computed the numerator/denominator on the fly
-- from raw token columns, but the underlying column was never created on
-- ``session_costs`` and the per-session value was never populated. The
-- caching audit on 2026-05-11 (docs/proposals/cache-audit-2026-05-11.md §3)
-- caught the gap.
--
-- This migration adds the column with a sensible default and is safe to run
-- repeatedly. ``ensure_schema`` in db_adapter.py now creates the column on
-- fresh installs; this file is for already-deployed Railway instances where
-- the table predates the fix.

ALTER TABLE session_costs
    ADD COLUMN IF NOT EXISTS cache_hit_pct NUMERIC(5, 2) NOT NULL DEFAULT 0.00;
