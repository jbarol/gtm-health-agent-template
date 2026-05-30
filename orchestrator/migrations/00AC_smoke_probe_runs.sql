-- Plan #42 PR2 — Pre-deploy smoke probe observability table.
-- Plan #44 Task #20 — ``check_d_ok`` column added (additive, IF NOT EXISTS).
--
-- Each ``orchestrator.smoke_probe.run_smoke_probe()`` invocation appends one
-- row here so the daily cost digest (and any future dashboard) can plot the
-- 7-day pass rate. Without this ledger, smoke-probe flakiness is invisible
-- until it blocks a critical deploy (plan #42 decision D12).
--
-- Row semantics:
--   * ``deploy_sha`` — the ``BUILD_COMMIT`` env var the container booted with.
--     May be empty when the probe runs in ``--local`` mode.
--   * ``started_at`` — when the probe began. Default NOW() so the writer can
--     omit it.
--   * ``passed`` — overall outcome. ``true`` includes the inconclusive-PASS
--     state (Anthropic 429/503 in Check C or D); see ``anthropic_status``.
--   * ``check_a_ok`` / ``check_b_ok`` / ``check_c_ok`` / ``check_d_ok`` —
--     per-check booleans. NULL means the check was skipped (e.g.
--     ``--check sf`` only ran Check B and left A/C/D as NULL; Check D is also
--     NULL at SMOKE_PROBE_LEVEL=quick).
--   * ``anthropic_status`` — ``'ok' | 'rate_limited' | 'unavailable'``.
--     Treated as PASS at the outcome level when ``passed = true`` and
--     ``anthropic_status != 'ok'`` — that's the inconclusive-PASS path
--     (plan #42 decision D7).
--   * ``elapsed_s`` — wall-clock seconds for the entire probe.
--   * ``reason`` — short human-readable explanation when ``passed = false``,
--     e.g. ``"check_b_failed: SF auth failure"``. Empty when ``passed = true``.
--     Also populated to ``"probe_disabled_via_level"`` for SMOKE_PROBE_LEVEL=off
--     runs (Plan #44 Task #20).
--
-- Idempotency: the same container can crash and restart; both attempts are
-- intentionally logged. The writer does NOT enforce a unique constraint on
-- ``(deploy_sha, started_at)`` because ``started_at`` is per-row and the
-- caller already supplies a fresh ``started_at`` per attempt.

CREATE TABLE IF NOT EXISTS smoke_probe_runs (
    id BIGSERIAL PRIMARY KEY,
    deploy_sha TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    passed BOOLEAN NOT NULL,
    check_a_ok BOOLEAN,
    check_b_ok BOOLEAN,
    check_c_ok BOOLEAN,
    check_d_ok BOOLEAN,
    anthropic_status TEXT,
    elapsed_s FLOAT,
    reason TEXT
);

-- Forward-compat: deploys that already created the table (Plan #42 PR2 ran
-- this migration before Task #20 was written) need the column added in-place.
-- ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` is the cleanest way to make the
-- migration idempotent across both first-run and upgrade paths.
ALTER TABLE smoke_probe_runs
    ADD COLUMN IF NOT EXISTS check_d_ok BOOLEAN;

CREATE INDEX IF NOT EXISTS idx_smoke_probe_runs_started_at
    ON smoke_probe_runs(started_at);
