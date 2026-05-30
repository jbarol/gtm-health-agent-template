-- Plan #33 — Persistent state surface.
--
-- ``surface_state`` is the per-portco "operating state" cache that backs
-- the Slack Canvas surface. One row per portco; ``state_json`` carries the
-- typed SurfaceState payload (open questions, recent findings, cost block,
-- decision log) and ``rendered_md`` is the materialized Markdown the
-- pusher writes to the Canvas. ``canvas_id`` records which Slack Canvas
-- the renderer is updating so subsequent pushes patch the same surface
-- instead of creating a new one. ``version`` is a monotonic counter the
-- renderer uses to detect concurrent writes and to short-circuit no-op
-- pushes when the rendered Markdown hasn't changed.
--
-- The CREATE TABLE statement is also embedded in db_adapter.ensure_schema
-- so production startup is idempotent without requiring an out-of-band
-- migration runner. This file exists as the canonical reference and for
-- manual operator runs (``psql -f 00Y_surface_state.sql``) on staging.

CREATE TABLE IF NOT EXISTS surface_state (
    portco       TEXT PRIMARY KEY,
    state_json   JSONB       NOT NULL DEFAULT '{}'::jsonb,
    rendered_md  TEXT        NOT NULL DEFAULT '',
    canvas_id    TEXT,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    version      BIGINT      NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_surface_state_updated_at
    ON surface_state(updated_at DESC);
