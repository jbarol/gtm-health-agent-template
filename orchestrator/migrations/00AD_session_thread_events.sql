-- Plan #44 Task #16 — Session thread events ledger.
--
-- Captures session.thread_* and agent.thread_message_* events from
-- _stream_and_handle so we can diagnose hung sub-agents, count concurrent
-- threads against the 25-thread cap, and provide structured replacement
-- for the heuristic backfill in session_costs.outcome (Plan #42's gripe).
--
-- Per Plan #44 decision row #11 (PII + retention):
--   * payload_json is capped at 4 KB by the writer (orchestrator side) —
--     long content + raw SOQL is truncated before insert.
--   * tool_input.q (SOQL) and content[*] fields are redacted at write
--     time. The SQL side only enforces the schema; the orchestrator owns
--     the redaction.
--   * 30-day TTL — see the cleanup approach below.
--   * Index on (session_id, created_at) ONLY. No event_type index. Most
--     queries are "what happened in this session" not "all dispatch
--     events across all sessions".
--   * Writers buffer inserts per session and flush on session.status_idle
--     to keep INSERT load below the 500-1K/session peak.
--
-- 30-day TTL approach: a worker job calls
-- ``db_adapter.purge_session_thread_events_older_than(30)`` on the same
-- cron schedule as the cost reconciliation job (06:00 PT). Postgres
-- pg_cron is not available in the Railway-managed Postgres tier; we use
-- a plain DELETE from the orchestrator instead. The function is
-- idempotent and capped at 50K rows per call to avoid long locks.
--
-- Cross-thread vs in-thread ordering (per research finding):
--   * In-thread reconstruction: ORDER BY thread_id, created_at.
--   * Cross-thread (e.g. parent-thread debugging): ORDER BY created_at.
--   * Anthropic emits a processed_at timestamp; we store it in payload_json
--     under "processed_at" for forensic replay when needed.
--
-- Append-only by design. Updates and deletes only happen via the TTL
-- purge worker.

CREATE TABLE IF NOT EXISTS session_thread_events (
    id BIGSERIAL PRIMARY KEY,
    session_id TEXT NOT NULL,
    thread_id TEXT,
    event_type TEXT NOT NULL,
    agent_name TEXT,
    ts TIMESTAMPTZ,
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Single index per decision row #11. The session_id is the primary
-- forensic lookup; created_at orders within a session. Cross-thread is
-- a rare debug case that justifies a sequential scan over a recent
-- window.
CREATE INDEX IF NOT EXISTS idx_session_thread_events_session_created
    ON session_thread_events(session_id, created_at);
