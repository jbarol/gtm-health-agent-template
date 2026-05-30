-- Plan #44 PR 8 — stale Coordinator session invalidation by config_version.
--
-- Failure mode this closes. After a prompt deploy, the Coordinator
-- ``multiagent.agents`` roster pins each sub-agent at the version that
-- was current when the parent was last updated (see CLAUDE.md
-- "Multi-agent orchestration" — pins are snapshotted, not live).
-- Follow-up messages in an existing Slack thread reuse the OLD
-- Coordinator session via ``thread_sessions`` → that session carries the
-- OLD roster snapshot AND the OLD prompt revisions for every agent.
-- Users keep hitting stale behavior for hours after a deploy.
--
-- Fix. Stamp every row with the ``config_version`` of the current
-- ``agents/active_versions.json`` (16-char sha256 hex prefix). On
-- reuse, the orchestrator compares the cached row's stamp to the
-- live value; on mismatch it archives the old session and starts a
-- fresh one. The lookup path uses the indexed column to skip stale
-- rows without a sequential scan.
--
-- Idempotency. The migration runner records this filename in
-- ``schema_migrations`` so it never runs twice, but each statement is
-- also written ``IF NOT EXISTS`` so a manual re-run on a partial DB
-- is a safe no-op. The column is nullable on purpose — rows written
-- before this migration applied carry NULL and the orchestrator
-- treats NULL as "unknown, force fresh" to avoid trusting an
-- un-stamped row across a prompt deploy.

ALTER TABLE thread_sessions
    ADD COLUMN IF NOT EXISTS config_version TEXT;

CREATE INDEX IF NOT EXISTS idx_thread_sessions_config_version
    ON thread_sessions(config_version);
