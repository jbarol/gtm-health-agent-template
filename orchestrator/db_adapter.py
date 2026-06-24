"""Railway Postgres adapter with daily snapshots.

Each nightly sync creates a new snapshot — prior snapshots are never deleted.
Views always reference the latest completed snapshot for each portco.
Historical snapshots enable day-over-day comparison (pipeline movement, new leads, closed deals).
"""

import hashlib
import json
import logging
import os
from datetime import date
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Cached 16-char sha256 prefix of ``agents/active_versions.json``. Computed
# once on first call and reused for the lifetime of the process — the file
# only changes across deploys, so a per-call recompute would burn IO on
# every Slack message. Module load-time recompute is intentionally NOT
# done here; the file may not exist yet during ``import db_adapter`` in
# test contexts.
_CONFIG_VERSION_CACHE: Optional[str] = None


def current_config_version() -> Optional[str]:
    """Return the 16-char sha256 prefix of ``agents/active_versions.json``.

    The orchestrator uses this stamp to invalidate cached Coordinator
    sessions on prompt deploys (Plan #44 PR 8). After a deploy, every
    ``thread_sessions`` row written by the prior container carries the
    OLD stamp; the reuse check in ``session_runner`` rejects mismatches
    and starts a fresh session instead of resuming with stale prompts.

    Cached after first compute. Returns ``None`` on any failure — the
    reuse path treats ``None`` as "unknown, force fresh" so a missing
    pin file cannot silently keep stale sessions alive.
    """
    global _CONFIG_VERSION_CACHE
    if _CONFIG_VERSION_CACHE is not None:
        return _CONFIG_VERSION_CACHE
    try:
        pin_path = (
            Path(__file__).resolve().parent.parent / "agents" / "active_versions.json"
        )
        data = pin_path.read_bytes()
        _CONFIG_VERSION_CACHE = hashlib.sha256(data).hexdigest()[:16]
        return _CONFIG_VERSION_CACHE
    except Exception as e:
        log.debug("current_config_version compute failed: %s", e)
        return None


def _reset_config_version_cache_for_tests() -> None:
    """Test hook — reset the module-level cache between assertions.

    Production code never calls this. The cache is intentionally
    process-scoped; a deploy starts a fresh container which recomputes.
    """
    global _CONFIG_VERSION_CACHE
    _CONFIG_VERSION_CACHE = None


# Same-day signal keywords. These trigger live MCP fallback (the Postgres
# snapshot can be up to 24h stale, so any "right now" question must skip it).
#
# Two classes:
#   ANCHORED — phrases that ONLY mean "I care about freshness". Plain substring
#              match is safe.
#   AMBIGUOUS — short, common words that can appear inside date ranges
#               ("between Sept 1 and today", "current quarter", "live pipeline").
#               These match only when used in a freshness context, not when
#               embedded in a literal date range. The matcher below excludes
#               ranges of the form "<phrase> and today", "to today",
#               "through today", and "as of today".
#
# Smoking gun (2026-05-11, session sesn_EXAMPLE): a 3,209-row
# list-pull query — "Leads where CreatedDate is between September 1 and today,
# Discovery_Call_Booked__c is not null..." — burned 3.5M input tokens because
# the substring "today" in "and today" tripped the same-day fallback, the
# orchestrator skipped the Postgres-context hint, and the agent loaded every
# Lead row into context via MCP rather than streaming to a file.
SAME_DAY_ANCHORED_KEYWORDS = [
    "right now",
    "this morning",
    "this afternoon",
    "just closed",
    "just came in",
    "real-time",
    "real time",
    "updated today",
    "changed today",
]

SAME_DAY_AMBIGUOUS_KEYWORDS = [
    "today",
    "just",
    "live",
    "current",
]

# Substrings that signal "today" is part of a date range, not a freshness ask.
# When ``today`` appears in one of these constructions, do NOT treat it as a
# same-day signal.
#
# Tightened 2026-05-11 (codex review on PR #95): "and today" alone is too
# permissive — it matches generic conjunctions like "compare yesterday and
# today pipeline," which is a same-day comparison and MUST go through MCP
# for partial-day freshness. Range constructions with "and today" require
# an explicit upper-bound preposition ("between"/"from"/"since") earlier in
# the sentence; that pairing is checked separately via _RANGE_AND_TODAY_PREPS.
_DATE_RANGE_TODAY_PATTERNS = [
    "to today",
    "through today",
    "thru today",
    "until today",
    "as of today",
    "up to today",
]

# When "and today" appears, only treat it as a range upper-bound if one of
# these prepositions appears EARLIER in the sentence ("between Sep 1, 2025
# and today", "from last quarter and today", "since Q3 ... and today").
# Otherwise it's a generic conjunction and we keep the same-day routing.
_RANGE_AND_TODAY_PREPS = ("between", "from", "since")

# Back-compat alias — older callers / tests import SAME_DAY_KEYWORDS directly.
SAME_DAY_KEYWORDS = SAME_DAY_ANCHORED_KEYWORDS + SAME_DAY_AMBIGUOUS_KEYWORDS


def is_db_available() -> bool:
    if not DATABASE_URL:
        return False
    try:
        import psycopg2

        conn = psycopg2.connect(DATABASE_URL)
        conn.close()
        return True
    except Exception:
        return False


def ensure_schema():
    """Create tables that don't exist yet. Safe to call on every startup (IF NOT EXISTS).

    Schema groups:
      Core            — thread_sessions, investigations (original).
      Cost tracking   — session_costs, anthropic_daily_costs, messages_api_calls,
                        cost_rollup_daily VIEW (Plan #35). Two-ledger architecture.
      Batch jobs      — batch_jobs, batch_job_requests, pending_self_heal_reviews
                        (Plan #36). Queue + state for Anthropic Batches API path.
      Compresr        — compresr_cache, compresr_calls (Plan #37). Content-hash
                        cache + per-call telemetry.
      Surface         — surface_state (Plan #33). Per-portco operating
                        state + rendered Slack Canvas markdown.
      Feedback        — feedback_events (Plan #30 D1). User-signal capture
                        from Slack emoji reactions (D1) + text feedback (D2).
      Verbosity       — channel_verbosity_preferences (Plan #31 E2). One
                        row per Slack channel storing its default verbosity
                        for the slack_bot._resolve_verbosity prefix-vs-pref
                        resolution order.
    """
    if not DATABASE_URL:
        return
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                # ── Base schema (db_schema.sql) ──────────────────────────
                # Apply the checked-in base schema FIRST so a brand-new
                # database has the snapshot tables (portcos, snapshots,
                # opportunities, leads, contacts, accounts), thread_sessions,
                # investigations, and the reporting VIEWs before any of the
                # inline ALTERs below run. db_schema.sql is fully idempotent
                # (CREATE TABLE/INDEX IF NOT EXISTS, CREATE OR REPLACE VIEW),
                # so re-applying on every boot is a no-op on existing
                # databases. Without this, a fresh DB crashes at the
                # ``ALTER TABLE leads`` block below with
                # ``relation "leads" does not exist`` — the inline DDL
                # assumed db_schema.sql had been applied out-of-band (manual
                # psql / Railway init), which a clean install / E2E harness /
                # fork never did. Fix surfaced by an end-to-end smoke run
                # against an empty throwaway database.
                _base_schema = Path(__file__).parent / "db_schema.sql"
                if _base_schema.is_file():
                    cur.execute(_base_schema.read_text())

                # ── Core ─────────────────────────────────────────────────
                # Composite PK on (channel_id, thread_ts) — Slack ``thread_ts``
                # is unique only within a channel, so multi-portco channels
                # would collide on identical timestamps without channel_id in
                # the key. See migration 00AJ_thread_sessions_channel_scope.sql
                # for the rationale and the single-portco backfill that
                # converts pre-existing prod rows to the new shape.
                #
                # The ALTER block AFTER the CREATE is the load-bearing part on
                # already-deployed databases. ``CREATE TABLE IF NOT EXISTS`` is
                # a no-op when the old single-column-PK shape already exists,
                # so without the in-line ALTER pre-existing Railway prod would
                # quietly lose thread persistence — save_thread_session +
                # get_thread_session would catch ``column "channel_id" does
                # not exist`` and log-and-swallow forever (codex P2 #1 review).
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS thread_sessions (
                        channel_id TEXT NOT NULL,
                        thread_ts TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        portco_key TEXT,
                        config_version TEXT,
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        last_used_at TIMESTAMPTZ DEFAULT NOW(),
                        PRIMARY KEY (channel_id, thread_ts)
                    );
                    CREATE INDEX IF NOT EXISTS idx_thread_sessions_last_used
                        ON thread_sessions(last_used_at DESC);
                    -- Mirror of migration 00AN_thread_session_config_version.sql.
                    -- ``config_version`` is the 16-char sha256 prefix of
                    -- ``agents/active_versions.json`` at the time the row was
                    -- written. The session_runner rejects reuse on stamp
                    -- mismatch so a prompt deploy invalidates every cached
                    -- Coordinator session.
                    ALTER TABLE thread_sessions
                        ADD COLUMN IF NOT EXISTS config_version TEXT;
                    CREATE INDEX IF NOT EXISTS idx_thread_sessions_config_version
                        ON thread_sessions(config_version);

                    -- Idempotent migration mirror of 00AJ. Each step is safe
                    -- to re-run on the new shape (column already present,
                    -- NULL backfill matches zero rows, PK swap short-circuits
                    -- via constraint-name presence checks).
                    --
                    -- Backfill order: investigations.channel_id first (the
                    -- evidence-based truth), then hardcoded master channel
                    -- as the orphan fallback. The DO block defends against
                    -- installs that ran ensure_schema before ``investigations``
                    -- existed — pg_class probe avoids ``relation "investigations"
                    -- does not exist`` on a brand-new database (codex P2 #2 review).
                    ALTER TABLE thread_sessions
                        ADD COLUMN IF NOT EXISTS channel_id TEXT;
                    DO $$
                    BEGIN
                        IF EXISTS (
                            SELECT 1 FROM pg_class
                            WHERE relname = 'investigations'
                              AND relkind = 'r'
                        ) THEN
                            EXECUTE $sql$
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
                                  AND ts.thread_ts = inv.thread_ts
                            $sql$;
                        END IF;
                    END$$;
                    UPDATE thread_sessions
                        SET channel_id = 'C0000000000'
                        WHERE channel_id IS NULL;
                    ALTER TABLE thread_sessions
                        ALTER COLUMN channel_id SET NOT NULL;
                    DO $$
                    BEGIN
                        IF EXISTS (
                            SELECT 1
                            FROM information_schema.table_constraints
                            WHERE table_name = 'thread_sessions'
                              AND constraint_name = 'thread_sessions_pkey'
                              AND constraint_type = 'PRIMARY KEY'
                        ) AND NOT EXISTS (
                            SELECT 1
                            FROM information_schema.key_column_usage
                            WHERE table_name = 'thread_sessions'
                              AND constraint_name = 'thread_sessions_pkey'
                              AND column_name = 'channel_id'
                        ) THEN
                            ALTER TABLE thread_sessions
                                DROP CONSTRAINT thread_sessions_pkey;
                            ALTER TABLE thread_sessions
                                ADD CONSTRAINT thread_sessions_pkey
                                PRIMARY KEY (channel_id, thread_ts);
                        END IF;
                    END$$;

                    CREATE TABLE IF NOT EXISTS investigations (
                        id SERIAL PRIMARY KEY,
                        thread_ts TEXT,
                        channel_id TEXT,
                        user_id TEXT,
                        question TEXT NOT NULL,
                        portco_key TEXT,
                        session_id TEXT,
                        agent_id TEXT,
                        status TEXT NOT NULL DEFAULT 'queued',
                        started_at TIMESTAMPTZ DEFAULT NOW(),
                        completed_at TIMESTAMPTZ,
                        error_message TEXT,
                        recovery_count INTEGER DEFAULT 0,
                        container_id TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_investigations_status
                        ON investigations(status);
                    CREATE INDEX IF NOT EXISTS idx_investigations_thread
                        ON investigations(thread_ts);
                """)

                # ── Cost tracking (Plan #35) ─────────────────────────────
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS session_costs (
                        id BIGSERIAL PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        agent_id TEXT,
                        model TEXT NOT NULL,
                        portco_key TEXT,
                        channel_id TEXT,
                        thread_ts TEXT,
                        user_id TEXT,
                        trigger TEXT,
                        verbosity TEXT,
                        input_tokens BIGINT NOT NULL DEFAULT 0,
                        output_tokens BIGINT NOT NULL DEFAULT 0,
                        cache_read_tokens BIGINT NOT NULL DEFAULT 0,
                        cache_write_5m_tokens BIGINT NOT NULL DEFAULT 0,
                        cache_write_1h_tokens BIGINT NOT NULL DEFAULT 0,
                        cost_usd NUMERIC(12, 6) NOT NULL DEFAULT 0,
                        cache_hit_pct NUMERIC(5, 2) NOT NULL DEFAULT 0.00,
                        tier TEXT NOT NULL DEFAULT 'realtime',
                        outcome TEXT NOT NULL DEFAULT 'success',
                        recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                    -- 2026-05-11 backfill: ensure already-deployed Railway
                    -- instances grow the column. ``cache_hit_pct`` was added
                    -- after the original Plan #35 ship — see migration file
                    -- 00Z_session_cost_cache_hit_pct.sql and the caching
                    -- audit at docs/proposals/cache-audit-2026-05-11.md §3.
                    ALTER TABLE session_costs
                        ADD COLUMN IF NOT EXISTS cache_hit_pct
                        NUMERIC(5, 2) NOT NULL DEFAULT 0.00;
                    -- Plan #42 PR1 D11 — ``outcome`` lets bin/measure-deploy-risk.py
                    -- compute Acme error-rate-per-hour without a separate
                    -- incident log. Mirrors migration 00AB_session_costs_outcome.sql.
                    ALTER TABLE session_costs
                        ADD COLUMN IF NOT EXISTS outcome
                        TEXT NOT NULL DEFAULT 'success';
                    CREATE INDEX IF NOT EXISTS idx_session_costs_recorded_at
                        ON session_costs(recorded_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_session_costs_portco
                        ON session_costs(portco_key, recorded_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_session_costs_trigger
                        ON session_costs(trigger, recorded_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_session_costs_outcome
                        ON session_costs(outcome, recorded_at DESC);
                    -- 2026-05-14 session_costs.session_id must be unique so
                    -- multi-turn sessions don't double-count. The orchestrator
                    -- INSERTs with ON CONFLICT (session_id) DO UPDATE SET ...
                    -- to overwrite the cumulative on every turn. Mirrors
                    -- migration 00AI_session_costs_unique_session_id.sql.
                    --
                    -- Dedupe FIRST, then add the index. Existing deploys may
                    -- have duplicate rows (pre-2026-05-14 behavior) — without
                    -- the DELETE, CREATE UNIQUE INDEX would fail and roll
                    -- back the whole ensure_schema transaction. Keep the row
                    -- with MAX(id) per session_id (latest INSERT carries the
                    -- highest cumulative usage, which is what we want).
                    DELETE FROM session_costs
                    WHERE id NOT IN (
                        SELECT MAX(id) FROM session_costs GROUP BY session_id
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_session_costs_session_id_unique
                        ON session_costs(session_id);

                    CREATE TABLE IF NOT EXISTS anthropic_daily_costs (
                        bucket_date DATE NOT NULL,
                        model TEXT NOT NULL,
                        workspace_id TEXT NOT NULL DEFAULT '',
                        service_tier TEXT NOT NULL DEFAULT 'standard',
                        input_tokens BIGINT NOT NULL DEFAULT 0,
                        output_tokens BIGINT NOT NULL DEFAULT 0,
                        cache_read_tokens BIGINT NOT NULL DEFAULT 0,
                        cache_write_tokens BIGINT NOT NULL DEFAULT 0,
                        cost_usd NUMERIC(12, 6) NOT NULL DEFAULT 0,
                        pulled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        PRIMARY KEY (bucket_date, model, workspace_id, service_tier)
                    );

                    CREATE TABLE IF NOT EXISTS messages_api_calls (
                        id BIGSERIAL PRIMARY KEY,
                        call_site TEXT NOT NULL,
                        model TEXT NOT NULL,
                        input_tokens BIGINT NOT NULL DEFAULT 0,
                        output_tokens BIGINT NOT NULL DEFAULT 0,
                        cache_read_tokens BIGINT NOT NULL DEFAULT 0,
                        cache_write_tokens BIGINT NOT NULL DEFAULT 0,
                        cost_usd NUMERIC(12, 6) NOT NULL DEFAULT 0,
                        tier TEXT NOT NULL DEFAULT 'realtime',
                        batch_id TEXT,
                        recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                    CREATE INDEX IF NOT EXISTS idx_messages_api_calls_recorded_at
                        ON messages_api_calls(recorded_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_messages_api_calls_site
                        ON messages_api_calls(call_site, recorded_at DESC);

                    CREATE OR REPLACE VIEW cost_rollup_daily AS
                    SELECT
                        DATE_TRUNC('day', recorded_at)::DATE AS bucket_date,
                        model,
                        COALESCE(portco_key, '(none)') AS portco_key,
                        SUM(cost_usd) AS local_cost_usd,
                        SUM(input_tokens) AS input_tokens,
                        SUM(output_tokens) AS output_tokens,
                        SUM(cache_read_tokens) AS cache_read_tokens,
                        SUM(cache_write_5m_tokens + cache_write_1h_tokens) AS cache_write_tokens
                    FROM session_costs
                    GROUP BY DATE_TRUNC('day', recorded_at), model, COALESCE(portco_key, '(none)')
                    UNION ALL
                    SELECT
                        DATE_TRUNC('day', recorded_at)::DATE AS bucket_date,
                        model,
                        '(messages-api)' AS portco_key,
                        SUM(cost_usd) AS local_cost_usd,
                        SUM(input_tokens) AS input_tokens,
                        SUM(output_tokens) AS output_tokens,
                        SUM(cache_read_tokens) AS cache_read_tokens,
                        SUM(cache_write_tokens) AS cache_write_tokens
                    FROM messages_api_calls
                    GROUP BY DATE_TRUNC('day', recorded_at), model;

                    -- Reconciliation alert dedup ledger (Plan #35, task #42).
                    -- One row per (bucket_date, direction) we've already alerted
                    -- on. cost_collector.reconcile_daily checks this table before
                    -- posting to Slack so repeated cron runs in the same day
                    -- don't spam the channel. ``direction`` is 'under' (we
                    -- under-estimated) or 'over' (we over-estimated). ``severity``
                    -- captures whether it was a >10% watch or >25% alert.
                    CREATE TABLE IF NOT EXISTS cost_reconciliation_alerts (
                        bucket_date DATE NOT NULL,
                        direction TEXT NOT NULL,
                        severity TEXT NOT NULL,
                        drift_pct NUMERIC(6, 4) NOT NULL,
                        drift_usd NUMERIC(12, 6) NOT NULL,
                        alerted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        PRIMARY KEY (bucket_date, direction)
                    );
                """)

                # ── Batch jobs (Plan #36) ────────────────────────────────
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS batch_jobs (
                        batch_id TEXT PRIMARY KEY,
                        call_site TEXT NOT NULL,
                        model TEXT NOT NULL,
                        request_count INTEGER NOT NULL,
                        status TEXT NOT NULL DEFAULT 'submitted',
                        submitted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        ended_at TIMESTAMPTZ,
                        error_message TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_batch_jobs_status
                        ON batch_jobs(status, submitted_at DESC);

                    CREATE TABLE IF NOT EXISTS batch_job_requests (
                        id BIGSERIAL PRIMARY KEY,
                        batch_id TEXT NOT NULL REFERENCES batch_jobs(batch_id) ON DELETE CASCADE,
                        request_id TEXT NOT NULL,
                        callback_name TEXT NOT NULL,
                        context_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                        result_text TEXT,
                        result_usage JSONB,
                        status TEXT NOT NULL DEFAULT 'pending',
                        completed_at TIMESTAMPTZ,
                        UNIQUE (batch_id, request_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_batch_job_requests_batch
                        ON batch_job_requests(batch_id);

                    CREATE TABLE IF NOT EXISTS pending_self_heal_reviews (
                        id BIGSERIAL PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        session_type TEXT,
                        review_payload JSONB NOT NULL,
                        enqueued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        batch_id TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_pending_self_heal_unbatched
                        ON pending_self_heal_reviews(enqueued_at)
                        WHERE batch_id IS NULL;
                """)

                # ── Compresr (Plan #37) ──────────────────────────────────
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS compresr_cache (
                        content_hash TEXT NOT NULL,
                        model TEXT NOT NULL,
                        compressed_text TEXT NOT NULL,
                        input_chars INTEGER NOT NULL,
                        compressed_chars INTEGER NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        PRIMARY KEY (content_hash, model)
                    );
                    CREATE INDEX IF NOT EXISTS idx_compresr_cache_created_at
                        ON compresr_cache(created_at DESC);

                    CREATE TABLE IF NOT EXISTS compresr_calls (
                        id BIGSERIAL PRIMARY KEY,
                        call_site TEXT NOT NULL,
                        model TEXT NOT NULL,
                        content_hash TEXT,
                        input_chars INTEGER NOT NULL DEFAULT 0,
                        compressed_chars INTEGER NOT NULL DEFAULT 0,
                        compression_ratio NUMERIC(6, 3),
                        latency_ms INTEGER,
                        fallback BOOLEAN NOT NULL DEFAULT FALSE,
                        fallback_reason TEXT,
                        downstream_ok BOOLEAN,
                        query_present BOOLEAN NOT NULL DEFAULT FALSE,
                        cache_hit BOOLEAN NOT NULL DEFAULT FALSE,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                    CREATE INDEX IF NOT EXISTS idx_compresr_calls_site_time
                        ON compresr_calls(call_site, created_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_compresr_calls_fallback
                        ON compresr_calls(fallback, created_at DESC)
                        WHERE fallback IS TRUE;

                    -- Idempotent ALTERs for the per-call telemetry columns
                    -- added by Plan #37 task #66. ``query_present`` lets
                    -- aggregations distinguish latte_v1 (query-aware) from
                    -- espresso_v1 (no query). ``cache_hit`` lifts the
                    -- cache-hit signal out of the ``fallback_reason`` string
                    -- so it can be queried directly via WHERE cache_hit IS TRUE
                    -- instead of WHERE fallback_reason = 'cache_hit'.
                    ALTER TABLE compresr_calls
                        ADD COLUMN IF NOT EXISTS query_present BOOLEAN NOT NULL DEFAULT FALSE;
                    ALTER TABLE compresr_calls
                        ADD COLUMN IF NOT EXISTS cache_hit BOOLEAN NOT NULL DEFAULT FALSE;

                    -- Per-site disabled flag for the regression guard
                    -- (Plan #37 task #67). One row per call site. Cleared
                    -- manually via DELETE FROM compresr_site_disabled WHERE
                    -- call_site = '...' once the operator has investigated
                    -- the regression and (presumably) tuned the call site or
                    -- waived the alert. ``ON CONFLICT (call_site) DO UPDATE``
                    -- in disable_call_site refreshes the timestamp + reason
                    -- so repeated runs show the most recent diagnostic.
                    CREATE TABLE IF NOT EXISTS compresr_site_disabled (
                        call_site TEXT PRIMARY KEY,
                        disabled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        reason TEXT
                    );
                """)

                # ── Channel verbosity preferences (Plan #31 E2) ──────────
                # One row per Slack channel — primary key is channel_id so
                # repeated /verbosity calls upsert in place. ``updated_by``
                # records the user_id of the last operator to change the
                # setting (informational; no auth gating today).
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS channel_verbosity_preferences (
                        channel_id TEXT PRIMARY KEY,
                        verbosity TEXT NOT NULL,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_by TEXT
                    );
                """)

                # ── Managed Agents docs-diff snapshot store ─────────────
                # One row per crawled doc page. ``content_hash`` is the
                # truncated SHA-256 of the page body (matches
                # ``self_improve._hash_content``). The previous implementation
                # persisted hashes to ``/tmp/gtm-health-agent/self-improve/``
                # — Railway's filesystem is ephemeral, so every container
                # restart blanked the baseline and the next nightly run
                # treated every tracked page as "new". Persisting in Postgres
                # keeps the baseline across deploys; the comparison reads the
                # previous row and only flags real diffs.
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS managed_agents_doc_snapshots (
                        page_url TEXT PRIMARY KEY,
                        content_hash TEXT NOT NULL DEFAULT '',
                        fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        last_run TIMESTAMPTZ
                    );
                    CREATE INDEX IF NOT EXISTS idx_managed_agents_doc_snapshots_fetched
                        ON managed_agents_doc_snapshots(fetched_at DESC);
                """)

                # ── Surface state (Plan #33) ────────────────────────────
                cur.execute("""
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
                """)

                # ── Session thread events (Plan #44 Task #16) ───────────
                # Append-only ledger of session.thread_* and
                # agent.thread_message_* events from _stream_and_handle.
                # See orchestrator/migrations/00AD_session_thread_events.sql
                # for the canonical schema notes including the PII/retention
                # rules from Plan #44 decision row #11.
                cur.execute("""
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
                    CREATE INDEX IF NOT EXISTS idx_session_thread_events_session_created
                        ON session_thread_events(session_id, created_at);
                """)

                # ── Feedback capture (Plan #30 D1) ───────────────────────
                # One row per user signal on a bot-authored message.
                # ``source`` is 'emoji' (D1, reaction-driven) or 'text'
                # (D2, "remember…/always…/never…" text feedback). Dedup
                # tuple is (portco_key, agent_message_ts, user_id, signal,
                # source) — Slack occasionally redelivers reaction_added,
                # so the ON CONFLICT in feedback_capture.record_feedback
                # collapses duplicates silently.
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS feedback_events (
                        id BIGSERIAL PRIMARY KEY,
                        ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        portco_key TEXT NOT NULL DEFAULT '',
                        channel_id TEXT NOT NULL DEFAULT '',
                        thread_ts TEXT NOT NULL DEFAULT '',
                        user_id TEXT NOT NULL DEFAULT '',
                        agent_message_ts TEXT NOT NULL DEFAULT '',
                        signal TEXT NOT NULL,
                        source TEXT NOT NULL,
                        raw_text TEXT
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS uniq_feedback_events_dedup
                        ON feedback_events (
                            portco_key, agent_message_ts, user_id, signal, source
                        );
                    CREATE INDEX IF NOT EXISTS idx_feedback_events_ts
                        ON feedback_events(ts DESC);
                    CREATE INDEX IF NOT EXISTS idx_feedback_events_portco
                        ON feedback_events(portco_key, ts DESC);
                """)

                # ── Leads.discovery_call_booked backfill ────────────────
                # Adds the discovery_call_booked column on already-deployed
                # instances. The base leads table is created from
                # db_schema.sql at bootstrap; this ALTER mirrors migration
                # file 00AA_lead_discovery_call_booked.sql so Railway
                # picks up the column without a manual psql step.
                # Defaulted to TIMESTAMPTZ — if describe(Lead) returns
                # Boolean for Discovery_Call_Booked__c, flip the type and
                # re-run.
                cur.execute("""
                    ALTER TABLE leads
                        ADD COLUMN IF NOT EXISTS discovery_call_booked TIMESTAMPTZ;
                    CREATE INDEX IF NOT EXISTS leads_discovery_call_booked_idx
                        ON leads(portco_key, discovery_call_booked)
                        WHERE discovery_call_booked IS NOT NULL;
                """)

                # ── Smoke probe runs (Plan #42 PR2; Plan #44 Task #20) ──
                # One row per pre-deploy smoke probe invocation. The 7-day
                # pass rate feeds the daily cost digest; the per-check
                # booleans help diagnose flaky checks (decision D12).
                # Mirrors migrations/00AC_smoke_probe_runs.sql so already-
                # deployed Railway instances pick the table up without a
                # manual psql step. ``check_d_ok`` was added by Plan #44
                # Task #20 — additive ALTER for upgrade paths.
                cur.execute("""
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
                    ALTER TABLE smoke_probe_runs
                        ADD COLUMN IF NOT EXISTS check_d_ok BOOLEAN;
                    CREATE INDEX IF NOT EXISTS idx_smoke_probe_runs_started_at
                        ON smoke_probe_runs(started_at);
                """)

                conn.commit()

            # Auto-apply orchestrator/migrations/*.sql idempotently. Closes
            # the gap that bit sesn_EXAMPLE on 2026-05-14:
            # migration 00AH (event_ts column on investigations) had never
            # been applied to prod, so create_investigation() raised a
            # column-doesn't-exist error which got swallowed at log.debug
            # level — every Slack adhoc since 2026-05-13 silently failed
            # to write its investigation row, breaking recovery + /cost
            # attribution. Inline ensure_schema only covers what's in
            # this file; .sql migrations were a parallel path requiring
            # manual psql. Now they're auto-applied on every boot.
            _apply_migrations(conn)

            log.info(
                "Schema migration complete — core + cost + batch + compresr "
                "+ surface + feedback tables verified"
            )
        finally:
            conn.close()
    except Exception:
        # Re-raise so Railway sees a failed boot. The previous swallow
        # logged the failure but let the orchestrator continue against a
        # stale schema — exactly the silent-drift pattern this PR is
        # meant to eliminate. Codex P2 finding 2026-05-14: if a new
        # migration is broken or a production DDL operation fails,
        # surface it to the operator instead of pretending boot is OK.
        # ``if not DATABASE_URL`` at the top still handles the "no DB
        # configured" path with graceful degradation; this catch-all
        # only fires when DATABASE_URL is set and DDL actually failed.
        log.exception("Schema migration failed — propagating to abort startup")
        raise


def _apply_migrations(conn):
    """Idempotently apply orchestrator/migrations/*.sql in lexicographic order.

    State tracking: ``schema_migrations`` table records each applied filename.
    On boot we walk the migrations dir, skip files already in the table, and
    execute the rest. Every existing migration is built with IF NOT EXISTS
    semantics so re-applying is safe — but the table still serves as the
    audit trail.

    Transaction model: each migration is wrapped in its own implicit
    transaction. If a migration fails, the ``schema_migrations`` INSERT
    never runs, so the next boot retries. Failures are logged + raised
    so the caller (ensure_schema) records the boot-time failure.

    Migration files MAY contain their own BEGIN/COMMIT pairs — psycopg2
    respects them. The ``schema_migrations`` INSERT runs after the
    migration body, in a fresh implicit transaction, and commits
    explicitly.
    """
    from pathlib import Path

    migrations_dir = Path(__file__).parent / "migrations"
    if not migrations_dir.is_dir():
        log.info("No migrations directory found; skipping auto-migrations")
        return

    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                filename TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        conn.commit()

    files = sorted(migrations_dir.glob("*.sql"))
    applied_now = 0
    for f in files:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM schema_migrations WHERE filename = %s", (f.name,)
            )
            if cur.fetchone():
                continue

        log.info(f"Applying migration {f.name}")
        try:
            sql = f.read_text()
            with conn.cursor() as cur:
                cur.execute(sql)
            conn.commit()
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO schema_migrations (filename) VALUES (%s) "
                    "ON CONFLICT (filename) DO NOTHING",
                    (f.name,),
                )
            conn.commit()
            applied_now += 1
            log.info(f"Migration {f.name} applied")
        except Exception:
            conn.rollback()
            log.exception(f"Migration {f.name} FAILED — will retry on next boot")
            raise

    if applied_now:
        log.info(f"Applied {applied_now} migration(s) on this boot")
    else:
        log.info(f"All {len(files)} migration(s) already applied")

    # Refresh the schema snapshot cache so a startup (or migration) call
    # primes the validator with the current information_schema rows.
    # Best-effort: failures are logged and the cache stays at whatever
    # the last successful refresh produced.
    try:
        refresh_schema_snapshot()
    except Exception:
        log.exception("Schema snapshot refresh failed")


# ---------------------------------------------------------------------------
# Schema snapshot (Plan #44 — SQL validator)
#
# The orchestrator's ``db_query`` custom tool consults this snapshot
# BEFORE handing the SQL to Postgres, so the agent gets a structured
# "column X doesn't exist on table Y" error rather than a raw psycopg2
# UndefinedColumn exception (which streams back through context and
# burns tokens without giving the model anything to act on).
#
# The snapshot is cheap to refresh — one query against information_schema
# for the public schema. We cache it on a module global and refresh on
# every ensure_schema() call (i.e. at startup, when the schema actually
# changes). Sync jobs that ALTER TABLE on the fly should also call
# ``refresh_schema_snapshot()`` to keep the cache current.
# ---------------------------------------------------------------------------

_SCHEMA_SNAPSHOT_CACHE: dict[str, set[str]] = {}


def get_schema_snapshot() -> dict[str, set[str]]:
    """Return the cached information_schema.columns map.

    Returns:
        ``{table_name: {column_name, ...}, ...}`` with lower-case keys
        and values. Empty dict when the DB is unavailable or has not
        been queried yet — callers (e.g. the validator) treat that as
        a pass-through signal.
    """
    return _SCHEMA_SNAPSHOT_CACHE


def refresh_schema_snapshot() -> dict[str, set[str]]:
    """Re-query information_schema.columns and replace the cache.

    Returns the freshly-populated map. On any DB error the cache is
    left untouched and an empty dict is returned (so a startup-time
    refresh failure doesn't wipe a previously-good cache mid-run).

    Idempotent and cheap: one round-trip, a handful of KB on typical
    portco schemas. Safe to call from sync jobs that ALTER TABLE.
    """
    global _SCHEMA_SNAPSHOT_CACHE
    if not DATABASE_URL:
        # No DB → cache stays empty → validator passes through.
        return {}
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT table_name, column_name "
                    "FROM information_schema.columns "
                    "WHERE table_schema = 'public'"
                )
                fresh: dict[str, set[str]] = {}
                for table_name, column_name in cur.fetchall():
                    if not table_name or not column_name:
                        continue
                    fresh.setdefault(table_name.lower(), set()).add(column_name.lower())
            _SCHEMA_SNAPSHOT_CACHE = fresh
            log.info(
                "Refreshed schema snapshot: %d tables, %d columns total",
                len(fresh),
                sum(len(cols) for cols in fresh.values()),
            )
            return fresh
        finally:
            conn.close()
    except Exception:
        log.exception("Schema snapshot refresh failed; cache unchanged")
        return {}


def _is_today_a_range_upper_bound(q_lower: str) -> bool:
    """True iff 'today' appears as the upper bound of a date range.

    A genuine range construction either:
      (a) uses a non-ambiguous range preposition directly before 'today'
          (``through today``, ``until today``, ``as of today``,
          ``up to today``, plain ``to today``), or
      (b) pairs ``and today`` with one of {between, from, since} appearing
          earlier in the sentence — e.g. ``between Sep 1 and today``.

    Plain ``X and today`` without an upstream range preposition is a generic
    conjunction (``compare yesterday and today``) and must keep its
    same-day classification.
    """
    # (a) explicit "<prep> today" forms — unambiguous range upper bound.
    if any(pat in q_lower for pat in _DATE_RANGE_TODAY_PATTERNS):
        return True

    # (b) "and today" only counts as a range if a range preposition appears
    # earlier in the sentence.
    idx = q_lower.find("and today")
    if idx == -1:
        return False
    prefix = q_lower[:idx]
    return any(prep in prefix for prep in _RANGE_AND_TODAY_PREPS)


def needs_same_day_data(question: str) -> bool:
    """True iff the question requires intra-day-fresh data (live MCP).

    The Postgres snapshot is refreshed at 1am Pacific; it can be up to 24h
    stale. Any question that says "what is happening RIGHT NOW" must skip
    the snapshot and hit MCP.

    Tightened 2026-05-11 to stop tripping on date-range queries that end
    with "and today" / "through today" — those are historical pulls (the
    user wants Sept 1 through today's date), not freshness asks.

    Tightened again 2026-05-11 (codex review on PR #95): bare "and today"
    no longer suppresses freshness routing on its own — only when paired
    with an upstream range preposition (between / from / since). This
    preserves same-day routing for "compare yesterday and today pipeline."
    """
    q_lower = question.lower()

    # Anchored phrases — unambiguous freshness signal.
    for kw in SAME_DAY_ANCHORED_KEYWORDS:
        if kw in q_lower:
            return True

    # Ambiguous keywords (today, just, live, current) — only trip if NOT
    # used inside a date-range construction.
    for kw in SAME_DAY_AMBIGUOUS_KEYWORDS:
        if kw not in q_lower:
            continue
        # "today" specifically gets the date-range exclusion. The other
        # ambiguous words don't have a common date-range idiom.
        if kw == "today":
            if _is_today_a_range_upper_bound(q_lower):
                continue
            # "today's pipeline", "what about today" — still a freshness ask.
            return True
        # "just" — only a freshness signal when used as "just <verb>"
        # (just closed, just came in, just opened). Plain "just" inside a
        # query like "just leads from Q4" is not a freshness signal. Both
        # of the closed-form versions are already in the anchored list, so
        # bare "just" here is too ambiguous to flip — skip.
        if kw == "just":
            continue
        # "live", "current" — accept as same-day signals.
        return True

    return False


def _connect():
    import psycopg2

    return psycopg2.connect(DATABASE_URL)


class DbQueryError(Exception):
    """A db_query failure with a classified ``kind``.

    Kinds (#283, #284, #310, #311):
      - ``unavailable``: the database could not be reached at all.
      - ``connection``: connection dropped mid-query (retryable).
      - ``permission``: the role lacks privilege (not retryable).
      - ``query``: the SQL itself is wrong — bad column/table/syntax.

    The classification lets the dispatcher surface an actionable message
    and lets the circuit breaker decide whether a retry could ever succeed.
    """

    def __init__(self, kind: str, message: str):
        self.kind = kind
        super().__init__(message)


def query(sql: str, params: tuple = None) -> dict:
    import psycopg2
    import psycopg2.extras

    try:
        conn = _connect()
    except psycopg2.OperationalError as e:
        raise DbQueryError("unavailable", f"database unavailable: {e}") from e

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            return {"records": [dict(r) for r in rows], "totalSize": len(rows)}
    except psycopg2.OperationalError as e:
        raise DbQueryError("connection", f"connection lost mid-query: {e}") from e
    except psycopg2.errors.InsufficientPrivilege as e:
        raise DbQueryError("permission", f"permission denied: {e}") from e
    except psycopg2.Error as e:
        raise DbQueryError("query", f"query failed: {e}") from e
    finally:
        conn.close()


def get_last_sync(portco_key: str) -> str:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT snapshot_date FROM snapshots "
                "WHERE portco_key = %s AND status = 'complete' "
                "ORDER BY snapshot_date DESC LIMIT 1",
                (portco_key,),
            )
            row = cur.fetchone()
            return row[0].isoformat() if row and row[0] else "never"
    finally:
        conn.close()


def create_snapshot(portco_key: str) -> int:
    """Create a new snapshot for today. Returns snapshot_id."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO portcos (key, name) VALUES (%s, %s) "
                "ON CONFLICT (key) DO NOTHING",
                (portco_key, portco_key.title()),
            )
            cur.execute(
                "INSERT INTO snapshots (portco_key, snapshot_date) "
                "VALUES (%s, %s) "
                "ON CONFLICT (portco_key, snapshot_date) "
                "DO UPDATE SET started_at = NOW(), status = 'running', record_counts = '{}' "
                "RETURNING id",
                (portco_key, date.today()),
            )
            snapshot_id = cur.fetchone()[0]
            conn.commit()
            log.info(
                f"Created snapshot {snapshot_id} for {portco_key} ({date.today()})"
            )
            return snapshot_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def write_records(
    snapshot_id: int, portco_key: str, object_type: str, records: list[dict]
):
    """Write a batch of SF records to the snapshot. Called by the sync tool handler.

    Idempotent on (snapshot_id, portco_key, object_type): before inserting,
    DELETE any prior rows for that key so a same-day re-run does not
    append a second copy. Background — ``create_snapshot`` upserts on
    ``(portco_key, snapshot_date)`` and re-returns the existing
    ``snapshot_id`` for the day, so without this DELETE pre-step the
    second run would write 2x rows for every sf_id (observed 2026-05-14:
    345,847 opportunities where 172,936 were expected, half with the
    pre-PR-#170 NULL ``account_id``, half with the populated column).

    The DELETE + INSERT batch runs inside a single transaction; if any
    INSERT raises, the entire batch — including the DELETE — rolls back
    so the snapshot is never left empty. The previous rows survive a
    failed retry.
    """
    table_by_object = {
        "Opportunity": "opportunities",
        "Lead": "leads",
        "Contact": "contacts",
        "Account": "accounts",
    }
    # Don't DELETE-then-insert on an empty batch — a zero-record sync
    # (SF outage, malformed describe, query-builder regression) would
    # silently wipe a healthy morning snapshot to nothing. The caller
    # (session_runner._sync_object) records ``record_counts[k]=0``
    # for an empty fetch, which is the right surface signal for the
    # operator; we let the previous rows stand so /cost, the canvas
    # surfaces, and downstream queries keep working.
    if not records:
        log.info(
            f"write_records: empty batch for {object_type} on snapshot "
            f"{snapshot_id}/{portco_key}; skipping DELETE+INSERT to "
            f"preserve prior rows."
        )
        return
    conn = _connect()
    try:
        with conn.cursor() as cur:
            # Idempotency pre-step: nuke any prior rows for this
            # (snapshot_id, portco_key, object_type) before re-inserting.
            # Scoping by ``portco_key`` keeps cross-portco rows on a
            # shared (theoretical) snapshot row safe; scoping by table
            # name (one table per object_type) means an Opportunity write
            # never touches Lead rows.
            target_table = table_by_object.get(object_type)
            if target_table:
                cur.execute(
                    f"DELETE FROM {target_table} "  # noqa: S608 — table name from whitelist above
                    "WHERE snapshot_id = %s AND portco_key = %s",
                    (snapshot_id, portco_key),
                )

            if object_type == "Opportunity":
                for r in records:
                    owner_name = ""
                    if isinstance(r.get("Owner"), dict):
                        owner_name = r["Owner"].get("Name", "")
                    record_type = ""
                    if isinstance(r.get("RecordType"), dict):
                        record_type = r["RecordType"].get("Name", "")
                    cur.execute(
                        "INSERT INTO opportunities (snapshot_id, portco_key, sf_id, name, "
                        "stage_name, amount, close_date, created_date, last_activity_date, "
                        "last_modified_date, owner_id, owner_name, lead_source, record_type, "
                        "is_closed, is_won, probability, fiscal_quarter, fiscal_year, "
                        "account_id, product_line) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (
                            snapshot_id,
                            portco_key,
                            r.get("Id"),
                            r.get("Name"),
                            r.get("StageName"),
                            r.get("Amount"),
                            r.get("CloseDate"),
                            r.get("CreatedDate"),
                            r.get("LastActivityDate"),
                            r.get("LastModifiedDate"),
                            r.get("OwnerId"),
                            owner_name,
                            r.get("LeadSource"),
                            record_type,
                            r.get("IsClosed"),
                            r.get("IsWon"),
                            r.get("Probability"),
                            r.get("FiscalQuarter"),
                            r.get("FiscalYear"),
                            r.get("AccountId"),
                            # Product_Line__c — populated by snapshot #15+
                            # after migration 00AQ_opp_product_line.sql. Older
                            # snapshots stay NULL unless the operator runs
                            # bin/backfill_opportunity_product_line.py.
                            r.get("Product_Line__c"),
                        ),
                    )

            elif object_type == "Lead":
                for r in records:
                    owner_name = ""
                    if isinstance(r.get("Owner"), dict):
                        owner_name = r["Owner"].get("Name", "")
                    cur.execute(
                        "INSERT INTO leads (snapshot_id, portco_key, sf_id, name, status, "
                        "lead_source, owner_id, owner_name, created_date, converted_date, "
                        "is_converted, funnel_stage, mql_date, sql_date, "
                        "discovery_call_booked) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (
                            snapshot_id,
                            portco_key,
                            r.get("Id"),
                            r.get("Name"),
                            r.get("Status"),
                            r.get("LeadSource"),
                            r.get("OwnerId"),
                            owner_name,
                            r.get("CreatedDate"),
                            r.get("ConvertedDate"),
                            r.get("IsConverted"),
                            r.get("Funnel_Stage__c"),
                            r.get("MQL_SDR_Accepted_Date_Time__c"),
                            r.get("SDR_Qualified_Date_Time__c"),
                            r.get("Discovery_Call_Booked__c"),
                        ),
                    )

            elif object_type == "Contact":
                for r in records:
                    owner_name = ""
                    if isinstance(r.get("Owner"), dict):
                        owner_name = r["Owner"].get("Name", "")
                    cur.execute(
                        "INSERT INTO contacts (snapshot_id, portco_key, sf_id, name, email, "
                        "title, account_id, owner_id, owner_name, lead_source, "
                        "created_date, last_activity_date) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (
                            snapshot_id,
                            portco_key,
                            r.get("Id"),
                            r.get("Name"),
                            r.get("Email"),
                            r.get("Title"),
                            r.get("AccountId"),
                            r.get("OwnerId"),
                            owner_name,
                            r.get("LeadSource"),
                            r.get("CreatedDate"),
                            r.get("LastActivityDate"),
                        ),
                    )

            elif object_type == "Account":
                for r in records:
                    record_type = ""
                    if isinstance(r.get("RecordType"), dict):
                        record_type = r["RecordType"].get("Name", "")
                    cur.execute(
                        "INSERT INTO accounts (snapshot_id, portco_key, sf_id, name, "
                        "record_type, industry, customer_tier, contract_status, region, "
                        "billing_country, created_date, arr) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (
                            snapshot_id,
                            portco_key,
                            r.get("Id"),
                            r.get("Name"),
                            record_type,
                            r.get("Industry"),
                            r.get("Customer_Tier__c"),
                            r.get("Contract_Status__c"),
                            r.get("Region__c"),
                            r.get("BillingCountry"),
                            r.get("CreatedDate"),
                            r.get("ARR__c"),
                        ),
                    )

            conn.commit()
            log.info(
                f"Wrote {len(records)} {object_type} records to snapshot {snapshot_id}"
            )

    except Exception:
        conn.rollback()
        log.exception(
            f"Failed to write {object_type} records to snapshot {snapshot_id}"
        )
        raise
    finally:
        conn.close()


def complete_snapshot(snapshot_id: int, record_counts: dict):
    """Mark a snapshot as complete with final record counts."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE snapshots SET status = 'complete', completed_at = NOW(), "
                "record_counts = %s WHERE id = %s",
                (json.dumps(record_counts), snapshot_id),
            )
            conn.commit()
            log.info(f"Snapshot {snapshot_id} complete: {record_counts}")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_thread_session(thread_ts: str, channel_id: str) -> Optional[str]:
    """Look up the session ID for a Slack thread in a specific channel.

    Returns None if not found or DB unavailable. ``channel_id`` is required
    because Slack's ``thread_ts`` is only unique within a channel — once a
    second portco-scoped channel comes online, two unrelated threads with
    identical timestamps could collide. See migration
    00AJ_thread_sessions_channel_scope.sql for the schema change rationale.

    Back-compat helper. The richer ``get_thread_session_record`` returns
    the session_id alongside the row's ``config_version`` stamp so the
    reuse-or-rotate decision in ``session_runner`` can compare against
    ``current_config_version()`` without a second round-trip.
    """
    record = get_thread_session_record(thread_ts, channel_id)
    return record[0] if record else None


def get_thread_session_record(
    thread_ts: str, channel_id: str
) -> Optional[tuple[str, Optional[str]]]:
    """Look up ``(session_id, config_version)`` for a Slack thread.

    Returns ``None`` on miss or DB-unavailable. ``config_version`` is
    ``None`` on rows written before migration 00AM applied — the reuse
    path treats ``None`` as "unknown, force fresh" so a pre-stamp row
    cannot silently survive a prompt deploy.
    """
    if not DATABASE_URL or not channel_id:
        return None
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE thread_sessions SET last_used_at = NOW() "
                    "WHERE channel_id = %s AND thread_ts = %s "
                    "RETURNING session_id, config_version",
                    (channel_id, thread_ts),
                )
                row = cur.fetchone()
                conn.commit()
                if not row:
                    return None
                return (row[0], row[1] if len(row) > 1 else None)
        finally:
            conn.close()
    except Exception as e:
        log.debug(f"Thread session lookup failed: {e}")
        return None


def save_thread_session(
    thread_ts: str,
    session_id: str,
    portco_key: str = None,
    channel_id: str = None,
):
    """Persist a thread→session mapping. Upserts so restarts don't lose context.

    ``channel_id`` is part of the composite primary key
    ``(channel_id, thread_ts)`` (migration 00AJ). When unset the write is
    skipped — without a channel scope two portcos sharing a Slack
    ``thread_ts`` value would cross-pollinate sessions on the next
    ``get_thread_session`` lookup.

    Every write stamps the row with ``current_config_version()`` (16-char
    sha256 of ``agents/active_versions.json``). The reuse path in
    ``session_runner`` compares this stamp to the live value and rejects
    mismatches so prompt deploys do not strand stale Coordinator
    sessions in active threads (migration 00AM, Plan #44 PR 8).
    """
    if not DATABASE_URL or not channel_id:
        return
    cfg_version = current_config_version()
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO thread_sessions "
                    "(channel_id, thread_ts, session_id, portco_key, config_version) "
                    "VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT (channel_id, thread_ts) DO UPDATE SET "
                    "session_id = EXCLUDED.session_id, "
                    "config_version = EXCLUDED.config_version, "
                    "last_used_at = NOW()",
                    (channel_id, thread_ts, session_id, portco_key, cfg_version),
                )
                cur.execute(
                    "DELETE FROM thread_sessions WHERE last_used_at < NOW() - INTERVAL '7 days'",
                )
                conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.debug(f"Thread session save failed: {e}")


def delete_thread_session(thread_ts: str, channel_id: str) -> None:
    """Drop the thread→session mapping for ``(channel_id, thread_ts)``.

    Used by ``recover_interrupted_investigations`` (B1, 2026-05-12 self-heal)
    when an old session is too bloated to resume. Deleting the row BEFORE
    creating a fresh session prevents the later ``get_thread_session`` lookup
    from putting the bot back on the dead session — the exact bug behind
    ``sesn_EXAMPLE``'s 5.9M-token recovery blowup.

    Idempotent. No-op when DB is unavailable or ``channel_id`` is missing.
    """
    if not DATABASE_URL or not thread_ts or not channel_id:
        return
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM thread_sessions "
                    "WHERE channel_id = %s AND thread_ts = %s",
                    (channel_id, thread_ts),
                )
                conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.debug(f"Thread session delete failed: {e}")


def delete_thread_session_by_session_id(session_id: str) -> int:
    """Drop every thread_sessions row pointing at ``session_id``.

    Recovery fallback (codex P2 #3 review). When an interrupted
    investigation has NULL ``channel_id`` (legacy rows from before
    migration 00AH carried channel_id consistently), the composite-key
    ``delete_thread_session`` cannot run. Without a cleanup the bloated
    session id sits in ``thread_sessions`` even after archive — the next
    live Slack message in the master channel re-attaches the bot to the
    dead session via ``get_thread_session``.

    The session_id column is not indexed; this scan is acceptable
    because (a) it only runs on the recovery path and (b) the table
    holds at most a few hundred rows under the 7-day TTL. Returns the
    rowcount for observability; 0 on miss / DB-unavailable.
    """
    if not DATABASE_URL or not session_id:
        return 0
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM thread_sessions WHERE session_id = %s",
                    (session_id,),
                )
                deleted = cur.rowcount or 0
                conn.commit()
                return int(deleted)
        finally:
            conn.close()
    except Exception as e:
        log.debug(f"Thread session delete-by-session-id failed: {e}")
        return 0


def clear_all_thread_sessions() -> int:
    """Drop every row in ``thread_sessions``.

    Plan #44 Task #9 / decision row #4: when a pinned agent version changes,
    the next follow-up in any active thread must NOT reuse the existing
    session — that session was bound to the old pin. The simplest and
    safest behavior is to clear the entire map (we don't track agent
    affinity per thread today). Returns the number of rows deleted for
    observability; 0 means the DB is unavailable or already empty.
    """
    if not DATABASE_URL:
        return 0
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM thread_sessions")
                deleted = cur.rowcount or 0
                conn.commit()
                return int(deleted)
        finally:
            conn.close()
    except Exception as e:
        log.debug(f"clear_all_thread_sessions failed: {e}")
        return 0


# ---------------------------------------------------------------------------
# Session thread events (Plan #44 Task #16)
# ---------------------------------------------------------------------------

# Per Plan #44 decision row #11, payloads are capped at this many bytes
# (after JSON serialization). Long agent.thread_message_* content carries
# user text + raw SOQL — both PII-sensitive and bulky. Writers must redact
# tool_input.q and content before serialization.
_SESSION_THREAD_EVENT_MAX_PAYLOAD_BYTES = 4096


def insert_session_thread_events(events: list) -> int:
    """Insert a batch of session_thread_events rows.

    ``events`` is a list of dicts with keys:
        session_id (str, required)
        thread_id (str | None)
        event_type (str, required)
        agent_name (str | None)
        ts (datetime | None) — when Anthropic emitted the event
        payload_json (dict | None) — already-redacted payload; serialized
                                     and truncated to 4 KB here.

    Best-effort: any insert failure logs at DEBUG and the batch is dropped.
    The session loop must never stall on telemetry writes. Returns the
    number of rows inserted.
    """
    if not DATABASE_URL or not events:
        return 0
    try:
        import json as _json

        conn = _connect()
        try:
            with conn.cursor() as cur:
                rows = []
                for ev in events:
                    sid = ev.get("session_id") or ""
                    if not sid:
                        continue
                    payload = ev.get("payload_json") or {}
                    serialized = _json.dumps(payload, default=str)
                    encoded_len = len(serialized.encode("utf-8"))
                    if encoded_len > _SESSION_THREAD_EVENT_MAX_PAYLOAD_BYTES:
                        # Truncate in-place. Worst case: malformed JSON
                        # on read — the consumer (Bundle E debugging
                        # surface) must tolerate this. PII safety > pretty.
                        # Suffix marker is 18 bytes; reserve at least that
                        # much room so the final blob lands at or under
                        # the cap.
                        _SUFFIX = '"_truncated":true}'
                        max_body = _SESSION_THREAD_EVENT_MAX_PAYLOAD_BYTES - len(
                            _SUFFIX
                        )
                        serialized = serialized[:max_body] + _SUFFIX
                    rows.append(
                        (
                            sid,
                            ev.get("thread_id"),
                            ev.get("event_type") or "unknown",
                            ev.get("agent_name"),
                            ev.get("ts"),
                            serialized,
                        )
                    )
                if not rows:
                    return 0
                cur.executemany(
                    "INSERT INTO session_thread_events "
                    "(session_id, thread_id, event_type, agent_name, ts, payload_json) "
                    "VALUES (%s, %s, %s, %s, %s, %s::jsonb)",
                    rows,
                )
                conn.commit()
                return len(rows)
        finally:
            conn.close()
    except Exception as e:
        log.debug(f"insert_session_thread_events failed: {e}")
        return 0


def purge_session_thread_events_older_than(
    days: int = 30, batch_size: int = 50000
) -> int:
    """Delete session_thread_events rows older than ``days``.

    Caller (the daily TTL worker) should invoke once per scheduler tick.
    Capped at ``batch_size`` per call so the lock window stays short
    (decision row #11 — DB load was a real concern at the 500-1K
    inserts/session peak). Returns the row count deleted; 0 means either
    nothing to purge or the DB is unavailable.
    """
    if not DATABASE_URL or days <= 0:
        return 0
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM session_thread_events "
                    "WHERE id IN ("
                    "  SELECT id FROM session_thread_events "
                    "  WHERE created_at < NOW() - (%s || ' days')::interval "
                    "  LIMIT %s"
                    ")",
                    (str(int(days)), int(batch_size)),
                )
                deleted = cur.rowcount or 0
                conn.commit()
                return int(deleted)
        finally:
            conn.close()
    except Exception as e:
        log.debug(f"purge_session_thread_events_older_than failed: {e}")
        return 0


# Heavy snapshot child tables — full SF row copies written each night. These
# are the only things the hot-window purge touches; the snapshots metadata
# row and daily_metrics rollup are kept forever (incident 2026-06-16).
_SNAPSHOT_CHILD_TABLES = ("opportunities", "leads", "contacts", "accounts")

# Days of raw child rows kept hot in Postgres. Older rows are dropped only
# AFTER the day's rollup + (if enabled) Parquet archive are confirmed, so
# nothing the snapshots exist for is lost — only the bulky hot copy ages out.
RAW_HOT_WINDOW_DAYS = int(os.environ.get("RAW_HOT_WINDOW_DAYS", "60"))


def get_snapshot_date(snapshot_id: int):
    """Return the ``snapshot_date`` (datetime.date) for a snapshot, or None."""
    if not DATABASE_URL:
        return None
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT snapshot_date FROM snapshots WHERE id = %s", (snapshot_id,)
                )
                row = cur.fetchone()
                return row[0] if row else None
        finally:
            conn.close()
    except Exception:
        log.exception(f"get_snapshot_date failed for snapshot {snapshot_id}")
        return None


def compute_and_store_daily_metrics(snapshot_id: int, portco_key: str) -> bool:
    """Tier 1: roll a completed snapshot up into the forever ``daily_metrics`` row.

    Runs a handful of aggregate queries scoped to ``snapshot_id`` and upserts
    one compact JSONB row keyed by (portco_key, snapshot_date). This is the
    "what was pipeline on date X / what changed" layer the snapshots exist
    for — kept forever at a few KB/day while the bulky raw rows age out of
    Postgres (see :func:`purge_raw_rows_older_than`).

    Idempotent: re-running for the same day overwrites the row and re-stamps
    ``snapshots.metrics_rolled_up_at``. Day-over-day "what changed" is derived
    at query time by diffing consecutive daily_metrics rows, so no deltas are
    stored here. Returns True on success, False if the DB is unavailable or
    the snapshot has no date.
    """
    if not DATABASE_URL:
        return False
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT snapshot_date FROM snapshots WHERE id = %s", (snapshot_id,)
                )
                row = cur.fetchone()
                if not row:
                    return False
                snap_date = row[0]
                metrics = _aggregate_snapshot_metrics(cur, snapshot_id, snap_date)
                cur.execute(
                    "INSERT INTO daily_metrics "
                    "  (portco_key, snapshot_id, snapshot_date, metrics, computed_at) "
                    "VALUES (%s, %s, %s, %s, NOW()) "
                    "ON CONFLICT (portco_key, snapshot_date) DO UPDATE SET "
                    "  snapshot_id = EXCLUDED.snapshot_id, "
                    "  metrics = EXCLUDED.metrics, "
                    "  computed_at = NOW()",
                    (portco_key, snapshot_id, snap_date, json.dumps(metrics)),
                )
                cur.execute(
                    "UPDATE snapshots SET metrics_rolled_up_at = NOW() WHERE id = %s",
                    (snapshot_id,),
                )
                conn.commit()
                log.info(
                    f"daily_metrics rolled up for {portco_key} {snap_date} "
                    f"(snapshot {snapshot_id})"
                )
                return True
        finally:
            conn.close()
    except Exception:
        log.exception(
            f"compute_and_store_daily_metrics failed for snapshot {snapshot_id}"
        )
        return False


def _aggregate_snapshot_metrics(cur, snapshot_id: int, snap_date) -> dict:
    """Run the rollup aggregate queries for one snapshot; return a JSON-able dict.

    Split out from :func:`compute_and_store_daily_metrics` so it is unit-test
    friendly (a fake cursor can feed canned rows). All money/count math is
    plain Postgres aggregation scoped by ``snapshot_id``.
    """

    def _f(v):  # Decimal/None -> float for JSON
        return float(v) if v is not None else 0.0

    # Opportunities — pipeline + movement.
    cur.execute(
        "SELECT "
        "  COUNT(*) FILTER (WHERE is_closed = false), "
        "  COALESCE(SUM(amount) FILTER (WHERE is_closed = false), 0), "
        "  COALESCE(AVG(amount) FILTER (WHERE is_closed = false), 0), "
        "  COALESCE(SUM(amount * COALESCE(probability, 0) / 100.0) "
        "           FILTER (WHERE is_closed = false), 0), "
        "  COUNT(*), "
        "  COUNT(*) FILTER (WHERE created_date::date = %s), "
        "  COUNT(*) FILTER (WHERE is_won = true AND close_date = %s), "
        "  COALESCE(SUM(amount) FILTER (WHERE is_won = true AND close_date = %s), 0), "
        "  COUNT(*) FILTER (WHERE is_closed = true AND is_won = false "
        "                   AND close_date = %s), "
        "  COUNT(*) FILTER (WHERE is_won = true), "
        "  COUNT(*) FILTER (WHERE is_closed = true) "
        "FROM opportunities WHERE snapshot_id = %s",
        (snap_date, snap_date, snap_date, snap_date, snapshot_id),
    )
    o = cur.fetchone()
    won_all, closed_all = int(o[9]), int(o[10])
    pipeline = {
        "open_opp_count": int(o[0]),
        "open_pipeline_amount": _f(o[1]),
        "avg_open_deal_size": _f(o[2]),
        "weighted_open_pipeline": _f(o[3]),
        "total_opp_count": int(o[4]),
        "new_opps_today": int(o[5]),
        "won_today_count": int(o[6]),
        "won_today_amount": _f(o[7]),
        "lost_today_count": int(o[8]),
        "lifetime_won_count": won_all,
        "lifetime_closed_count": closed_all,
        "lifetime_win_rate_pct": round(100.0 * won_all / closed_all, 2)
        if closed_all
        else 0.0,
    }

    # Open pipeline by stage.
    cur.execute(
        "SELECT COALESCE(stage_name, '(none)'), COUNT(*), COALESCE(SUM(amount), 0) "
        "FROM opportunities WHERE snapshot_id = %s AND is_closed = false "
        "GROUP BY stage_name",
        (snapshot_id,),
    )
    pipeline["by_stage"] = {
        r[0]: {"count": int(r[1]), "amount": _f(r[2])} for r in cur.fetchall()
    }

    # Leads — funnel.
    cur.execute(
        "SELECT "
        "  COUNT(*), "
        "  COUNT(*) FILTER (WHERE created_date::date = %s), "
        "  COUNT(*) FILTER (WHERE is_converted = true), "
        "  COUNT(*) FILTER (WHERE mql_date IS NOT NULL), "
        "  COUNT(*) FILTER (WHERE sql_date IS NOT NULL), "
        "  COUNT(*) FILTER (WHERE discovery_call_booked IS NOT NULL) "
        "FROM leads WHERE snapshot_id = %s",
        (snap_date, snapshot_id),
    )
    le = cur.fetchone()
    total_leads, converted = int(le[0]), int(le[2])
    leads = {
        "total_leads": total_leads,
        "new_leads_today": int(le[1]),
        "converted_count": converted,
        "mql_count": int(le[3]),
        "sql_count": int(le[4]),
        "discovery_booked_count": int(le[5]),
        "lead_conversion_rate_pct": round(100.0 * converted / total_leads, 2)
        if total_leads
        else 0.0,
    }
    cur.execute(
        "SELECT COALESCE(funnel_stage, '(none)'), COUNT(*) "
        "FROM leads WHERE snapshot_id = %s GROUP BY funnel_stage",
        (snapshot_id,),
    )
    leads["by_funnel_stage"] = {r[0]: int(r[1]) for r in cur.fetchall()}

    # Accounts / contacts — counts + ARR.
    cur.execute(
        "SELECT COUNT(*), COALESCE(SUM(arr), 0) FROM accounts WHERE snapshot_id = %s",
        (snapshot_id,),
    )
    a = cur.fetchone()
    cur.execute("SELECT COUNT(*) FROM contacts WHERE snapshot_id = %s", (snapshot_id,))
    c = cur.fetchone()

    return {
        "pipeline": pipeline,
        "leads": leads,
        "accounts": {"account_count": int(a[0]), "total_arr": _f(a[1])},
        "contacts": {"contact_count": int(c[0])},
    }


def mark_snapshot_archived(snapshot_id: int, archive_uri: str) -> bool:
    """Tier 2 bookkeeping: record that a snapshot's raw rows are in cold storage.

    Called by the archiver after a successful Parquet upload. The purge
    (Tier 3) requires ``archived_at`` to be set before dropping the hot
    child rows whenever archiving is enabled.
    """
    if not DATABASE_URL:
        return False
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE snapshots SET archived_at = NOW(), archive_uri = %s "
                    "WHERE id = %s",
                    (archive_uri, snapshot_id),
                )
                conn.commit()
                return True
        finally:
            conn.close()
    except Exception:
        log.exception(f"mark_snapshot_archived failed for snapshot {snapshot_id}")
        return False


def fetch_snapshot_rows(snapshot_id: int, table: str) -> list[dict]:
    """Return all rows of one snapshot child table as dicts (for archival).

    ``table`` must be one of the whitelisted snapshot child tables. Used by
    the Parquet archiver to stream a snapshot to cold storage before the
    hot rows are purged.
    """
    if not DATABASE_URL or table not in _SNAPSHOT_CHILD_TABLES:
        return []
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT * FROM {table} WHERE snapshot_id = %s",  # noqa: S608 — whitelist
                    (snapshot_id,),
                )
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]
        finally:
            conn.close()
    except Exception:
        log.exception(f"fetch_snapshot_rows failed ({table}, snapshot {snapshot_id})")
        return []


def purge_raw_rows_older_than(
    days: int = RAW_HOT_WINDOW_DAYS,
    archive_required: bool = True,
    max_snapshots_per_call: int = 10,
) -> int:
    """Tier 3: drop the bulky child rows of snapshots older than ``days``.

    This is the bounded hot-window sweep that keeps Postgres from refilling
    after incident 2026-06-16. It deletes ONLY the heavy child rows
    (opportunities/leads/contacts/accounts); the ``snapshots`` metadata row
    and the forever ``daily_metrics`` rollup are never touched, so "pipeline
    on date X" still answers for every historical day.

    A snapshot's raw rows are eligible only when:
      * it is older than ``days`` (by snapshot_date), AND
      * ``metrics_rolled_up_at`` is set (Tier 1 captured the day), AND
      * if ``archive_required``, ``archived_at`` is set (Tier 2 put the raw
        rows in cold storage).

    So nothing is deleted until it is preserved in at least the rollup (and,
    when archiving is on, the Parquet archive too). ``max_snapshots_per_call``
    caps the lock window so the first post-incident drain of the backlog
    spreads across daily ticks. Returns the number of snapshots whose rows
    were purged; 0 means nothing eligible or the DB is unavailable.
    """
    if not DATABASE_URL or days <= 0:
        return 0
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM snapshots "
                    "WHERE snapshot_date < CURRENT_DATE - (%s || ' days')::interval "
                    "  AND metrics_rolled_up_at IS NOT NULL "
                    "  AND raw_purged_at IS NULL "
                    "  AND (%s = false OR archived_at IS NOT NULL) "
                    "ORDER BY snapshot_date ASC LIMIT %s",
                    (str(int(days)), archive_required, int(max_snapshots_per_call)),
                )
                old_ids = [r[0] for r in cur.fetchall()]
                if not old_ids:
                    return 0
                for child in _SNAPSHOT_CHILD_TABLES:
                    cur.execute(
                        f"DELETE FROM {child} WHERE snapshot_id = ANY(%s)",  # noqa: S608 — whitelist
                        (old_ids,),
                    )
                cur.execute(
                    "UPDATE snapshots SET raw_purged_at = NOW() WHERE id = ANY(%s)",
                    (old_ids,),
                )
                conn.commit()
                log.info(
                    f"purge_raw_rows_older_than: purged raw rows for "
                    f"{len(old_ids)} snapshot(s) older than {days}d "
                    f"(archive_required={archive_required})"
                )
                return len(old_ids)
        finally:
            conn.close()
    except Exception:
        log.exception("purge_raw_rows_older_than failed")
        return 0


def create_investigation(
    question: str,
    thread_ts: str = None,
    channel_id: str = None,
    user_id: str = None,
    portco_key: str = None,
    container_id: str = None,
    event_ts: str = None,
) -> Optional[int]:
    """Record a new investigation. Returns the investigation ID, or None if DB unavailable.

    ``event_ts`` (added 2026-05-13 alongside migration 00AH) is the Slack
    timestamp of the user's original message. ``recover_interrupted_investigations``
    reads it after a container restart to repair the lifecycle reaction on
    the right message in the thread. Cron flows (dream, forecast) leave it
    NULL.
    """
    if not DATABASE_URL:
        return None
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO investigations "
                    "(question, thread_ts, channel_id, user_id, portco_key, status, container_id, event_ts) "
                    "VALUES (%s, %s, %s, %s, %s, 'queued', %s, %s) RETURNING id",
                    (
                        question,
                        thread_ts,
                        channel_id,
                        user_id,
                        portco_key,
                        container_id,
                        event_ts,
                    ),
                )
                inv_id = cur.fetchone()[0]
                conn.commit()
                return inv_id
        finally:
            conn.close()
    except Exception as e:
        log.debug(f"Failed to create investigation record: {e}")
        return None


def update_investigation(
    inv_id: int, status: str, session_id: str = None, error_message: str = None
):
    """Update investigation status. Called at key lifecycle transitions.

    Status values that auto-fill ``completed_at``: completed, failed,
    interrupted, cancelled. ``cancelled`` was added 2026-05-13 alongside
    the lifecycle-terminalization refactor — it's distinct from ``failed``
    so /cost and recovery filters can exclude user-initiated stops from
    failure-rate metrics.
    """
    if not DATABASE_URL or not inv_id:
        return
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                sets = ["status = %s"]
                vals = [status]
                if session_id:
                    sets.append("session_id = %s")
                    vals.append(session_id)
                if status in ("completed", "failed", "interrupted", "cancelled"):
                    sets.append("completed_at = NOW()")
                if error_message:
                    sets.append("error_message = %s")
                    vals.append(error_message)
                vals.append(inv_id)
                cur.execute(
                    f"UPDATE investigations SET {', '.join(sets)} WHERE id = %s",
                    vals,
                )
                conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.debug(f"Failed to update investigation {inv_id}: {e}")


def transition_queued_to_running(inv_id: int, session_id: str) -> bool:
    """Move an investigation from 'queued' to 'running' atomically.

    Returns True iff cursor.rowcount==1, i.e. the row was still in
    'queued' when this UPDATE fired. Returns False if the row has
    already been cancelled (by /stop or in-thread cancel intent),
    terminalized, or doesn't exist.

    Used by the existing-session-reuse branch in run_adhoc_mcp_session
    to close the queued→running race window (codex P2, 2026-05-13).
    Pre-fix: unconditional update_investigation(inv_id, 'running')
    would re-open a cancelled row if /stop landed in the millisecond
    between create_investigation and the running-flip. Post-fix:
    caller MUST check the return value before proceeding to stream —
    if False, the row was cancelled and streaming would override the
    user's intent.
    """
    if not DATABASE_URL or not inv_id:
        return False
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE investigations SET status = 'running', session_id = %s "
                    "WHERE id = %s AND status = 'queued'",
                    (session_id, inv_id),
                )
                won = cur.rowcount == 1
                conn.commit()
                return won
        finally:
            conn.close()
    except Exception as e:
        log.debug(f"transition_queued_to_running({inv_id}) failed: {e}")
        return False


def mark_investigation_orphan_dead_lettered(
    inv_id: int,
    error_message: str = None,
) -> bool:
    """Task #23 — atomically flip an investigation row to ``orphan_dead_lettered``.

    Unlike ``update_investigation_atomic`` (which excludes ``interrupted``
    from its allow-list because it's modelled as terminal-ish), this helper
    accepts the precondition ``status IN ('queued', 'running', 'interrupted')``
    because ``recover_interrupted_investigations`` flips rows ``running →
    interrupted`` on its first SELECT-FOR-UPDATE step, and the dead-letter
    branch runs AFTER that flip. Without this targeted path the dead-letter
    helper's UPDATE matched zero rows in production and the admin DM
    silently never fired — observed against ``sesn_EXAMPLE``.

    Returns True iff this caller won the race (cursor.rowcount == 1).
    Returns False on already-terminal rows (``completed``/``failed``/
    ``cancelled``/``archived``/``orphan_dead_lettered``), missing inv_id,
    missing DATABASE_URL, or DB error. The caller uses the False return to
    skip the admin DM and avoid double-pinging when some other terminal
    path raced to the row first (e.g. /stop landed in the same window).
    """
    if not DATABASE_URL or not inv_id:
        return False
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                sets = [
                    "status = 'orphan_dead_lettered'",
                    "completed_at = NOW()",
                ]
                vals: list = []
                if error_message:
                    sets.append("error_message = %s")
                    vals.append(error_message)
                vals.append(inv_id)
                cur.execute(
                    f"UPDATE investigations SET {', '.join(sets)} "
                    "WHERE id = %s "
                    "AND status IN ('queued','running','interrupted')",
                    vals,
                )
                won = cur.rowcount == 1
                conn.commit()
                return won
        finally:
            conn.close()
    except Exception as e:
        log.debug(f"mark_investigation_orphan_dead_lettered({inv_id}) failed: {e}")
        return False


def update_investigation_atomic(
    inv_id: int,
    status: str,
    error_message: str = None,
) -> bool:
    """Terminalize an investigation row, returning True iff this caller won the race.

    Issues ``UPDATE investigations SET status=... WHERE id=... AND status NOT
    IN (terminal_states)``. ``cursor.rowcount`` is 1 if the row was still
    non-terminal and got flipped here, 0 if some other path already
    terminalized it. This is the DB-layer half of the two-layer idempotency
    fence used by ``lifecycle.terminalize_lifecycle`` — the in-memory map
    deduplicates inside a process, this guard deduplicates across processes
    (e.g. an old container draining while a new container started after a
    deploy).

    Returns False on missing inv_id, missing DATABASE_URL, or DB error.
    A False return tells the caller NOT to fire the Slack reaction
    transition — some other path already did.
    """
    if not DATABASE_URL or not inv_id:
        return False
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                sets = ["status = %s", "completed_at = NOW()"]
                vals = [status]
                if error_message:
                    sets.append("error_message = %s")
                    vals.append(error_message)
                vals.append(inv_id)
                cur.execute(
                    f"UPDATE investigations SET {', '.join(sets)} "
                    "WHERE id = %s "
                    "AND status NOT IN ("
                    "'completed','failed','cancelled','archived',"
                    "'interrupted','orphan_dead_lettered'"
                    ")",
                    vals,
                )
                won = cur.rowcount == 1
                conn.commit()
                return won
        finally:
            conn.close()
    except Exception as e:
        log.debug(f"update_investigation_atomic({inv_id}) failed: {e}")
        return False


def get_investigation_for_thread(thread_ts: str) -> Optional[dict]:
    """Return the most recent investigation row for a Slack thread.

    Used by the in-thread meta-intent router (Track F) to look up whether a
    thread already has an investigation attached — status / cancel / pause
    intents only fire when there's something to report on / stop.

    Returns the row as a dict (keys: id, thread_ts, channel_id, user_id,
    question, portco_key, session_id, agent_id, status, started_at,
    completed_at, error_message, recovery_count, container_id) or None
    when no row exists / DB unavailable / DB query fails. Best-effort —
    must never raise.

    Multiple investigations can be tied to one thread over time
    (recovery, follow-ups); we return the most-recently-started row.
    """
    if not DATABASE_URL or not thread_ts:
        return None
    try:
        import psycopg2.extras

        conn = _connect()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # ORDER BY prefers active (queued/running) rows over
                # terminal ones (2026-05-13 codex follow-up). With one
                # investigations row per Slack user message (commit
                # introducing thread-continuation row minting), a thread
                # accumulates rows over time: e.g. 5 follow-ups → 5 rows
                # where the latest is 'running' and the older 4 are
                # 'completed'. /stop and in-thread cancel intents
                # should pick the active row, not the most-recently-
                # started terminal one.
                cur.execute(
                    "SELECT id, thread_ts, channel_id, user_id, question, "
                    "portco_key, session_id, agent_id, status, started_at, "
                    "completed_at, error_message, recovery_count, container_id, "
                    "event_ts "
                    "FROM investigations "
                    "WHERE thread_ts = %s "
                    "ORDER BY "
                    "  CASE WHEN status IN ('queued', 'running') THEN 0 ELSE 1 END, "
                    "  started_at DESC NULLS LAST, "
                    "  id DESC "
                    "LIMIT 1",
                    (thread_ts,),
                )
                row = cur.fetchone()
                return dict(row) if row else None
        finally:
            conn.close()
    except Exception as e:
        log.debug(f"Failed to lookup investigation for thread {thread_ts}: {e}")
        return None


def get_investigation_by_id(inv_id: int) -> Optional[dict]:
    """Return an investigation row by id. Returns None on miss / DB error.

    Used by ``lifecycle.terminalize_lifecycle`` reconciliation: when a
    DB-guard UPDATE matches 0 rows (another path already terminalized),
    we need to read the row's final status to know what reaction to
    flip on the Slack message — the requested state may not match what
    actually landed.
    """
    if not DATABASE_URL or not inv_id:
        return None
    try:
        import psycopg2.extras

        conn = _connect()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT id, thread_ts, channel_id, user_id, question, "
                    "portco_key, session_id, agent_id, status, started_at, "
                    "completed_at, error_message, recovery_count, container_id, "
                    "event_ts "
                    "FROM investigations WHERE id = %s",
                    (inv_id,),
                )
                row = cur.fetchone()
                return dict(row) if row else None
        finally:
            conn.close()
    except Exception as e:
        log.debug(f"get_investigation_by_id({inv_id}) failed: {e}")
        return None


def cancel_investigation(inv_id: int, reason: str = "user cancelled") -> bool:
    """Mark an in-flight investigation as cancelled (Track F — in-thread cancel intent).

    Only acts on rows whose current ``status`` is ``queued`` or ``running``.
    Already-terminal rows (``completed`` / ``failed`` / ``cancelled`` /
    ``interrupted``) are left untouched — overwriting them would rewrite
    historical outcomes and corrupt the analytics that read this table
    (e.g. ``status_responder`` ETA baselines, codex review PR #97 comment
    3223872886).

    Returns True when a row was updated, False when the investigation was
    already in a terminal state OR on DB error / missing id. The caller
    distinguishes the two cases by checking the row status before calling
    (or by inspecting downstream user-facing copy).

    Best-effort — never raises so the Slack handler keeps responding.
    """
    if not DATABASE_URL or not inv_id:
        return False
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE investigations "
                    "SET status = 'cancelled', "
                    "    completed_at = NOW(), "
                    "    error_message = %s "
                    "WHERE id = %s "
                    "  AND status IN ('queued', 'running')",
                    (reason, inv_id),
                )
                conn.commit()
                return cur.rowcount > 0
        finally:
            conn.close()
    except Exception as e:
        log.debug(f"Failed to cancel investigation {inv_id}: {e}")
        return False


def get_interrupted_investigations(current_container_id: str = None) -> list:
    """Find investigations that died with a prior container — both 'running'
    and 'queued' orphans.

    Returns dicts with: id, question, thread_ts, channel_id, user_id,
    portco_key, session_id, agent_id, recovery_count, started_at, event_ts.

    Theme A (2026-05-16) extended the WHERE to include ``status = 'queued'``
    rows whose container_id is stale AND that have been queued > 15 minutes.
    Pre-extension behavior: a row queued by a dying container (e.g. the user
    submitted a question while a deploy rolled the orchestrator) sat in
    ``queued`` forever because ``recover_interrupted_investigations`` only
    looked at ``running``. Live incident inv 32 sat queued since 2026-05-14
    until this fix. The 15-min floor protects rows belonging to a currently
    live container that just hasn't started them yet.

    Queued rows have ``session_id IS NULL``; the recovery loop handles that
    by falling through to a fresh-start path with the original question.

    Plan #44 Task #18: started_at is surfaced so the caller can compute
    age and archive rows older than the 25-day TTL.
    """
    if not DATABASE_URL:
        return []
    try:
        import psycopg2.extras

        conn = _connect()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "UPDATE investigations SET status = 'interrupted', "
                    "completed_at = NOW() "
                    "WHERE (container_id IS NULL OR container_id != %s) "
                    "AND ("
                    "  status = 'running' "
                    "  OR (status = 'queued' "
                    "      AND started_at < NOW() - INTERVAL '15 minutes')"
                    ") "
                    "RETURNING id, question, thread_ts, channel_id, user_id, "
                    "portco_key, session_id, agent_id, recovery_count, started_at, "
                    "event_ts",
                    (current_container_id or "",),
                )
                rows = [dict(r) for r in cur.fetchall()]
                conn.commit()
                return rows
        finally:
            conn.close()
    except Exception as e:
        log.debug(f"Failed to query interrupted investigations: {e}")
        return []


def list_running_investigations_for_container(container_id: str) -> list[dict]:
    """Investigations currently 'running' in THIS container. Read-only.

    Used by the session watchdog (Plan: Design A) to scan for stalled
    sessions. Returns dicts with: id, session_id, thread_ts, channel_id,
    event_ts, started_at. Filters to ``session_id IS NOT NULL`` because
    a row in 'running' state without a session id is mid-creation and
    nothing to act on.
    """
    if not DATABASE_URL:
        return []
    try:
        import psycopg2.extras

        conn = _connect()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT id, session_id, thread_ts, channel_id, event_ts, "
                    "started_at "
                    "FROM investigations "
                    "WHERE status = 'running' "
                    "AND container_id = %s "
                    "AND session_id IS NOT NULL "
                    "ORDER BY started_at ASC NULLS LAST",
                    (container_id or "",),
                )
                return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()
    except Exception as e:
        log.debug(
            f"list_running_investigations_for_container({container_id}) failed: {e}"
        )
        return []


def mark_investigation_failed(inv_id: int, error_message: str = None) -> bool:
    """Mark an investigation row 'failed'. Thin wrapper for the watchdog.

    Re-uses ``update_investigation_atomic`` so the two-layer idempotency
    fence keeps working — if some other path already terminalized the row
    (e.g. a late post_report success), this call returns False and the
    DB row reflects the winning state.
    """
    return update_investigation_atomic(
        inv_id, status="failed", error_message=error_message
    )


def mark_investigation_recovering(
    inv_id: int, new_session_id: str = None, container_id: str = None
):
    """Mark an interrupted investigation as being recovered. Updates container_id to prevent re-recovery by the same container."""
    if not DATABASE_URL:
        return
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE investigations SET status = 'running', "
                    "recovery_count = recovery_count + 1, "
                    "session_id = COALESCE(%s, session_id), "
                    "container_id = COALESCE(%s, container_id), "
                    "completed_at = NULL, started_at = NOW() "
                    "WHERE id = %s",
                    (new_session_id, container_id, inv_id),
                )
                conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.debug(f"Failed to mark investigation {inv_id} recovering: {e}")


# ---------------------------------------------------------------------------
# Task #23 — Orphan dead-letter cleanup
# ---------------------------------------------------------------------------

# Sessions flagged in prior carry-over snapshots that we've already accounted
# for. ``cleanup_known_orphans`` marks any of these still in 'running' state
# as ``orphan_dead_lettered`` at startup, with no admin DM (the operator
# already knows about these). New orphans get the live admin-DM path in
# ``session_runner.recover_interrupted_investigations``.
KNOWN_ORPHAN_SESSION_IDS: tuple[str, ...] = ("sesn_EXAMPLE",)


def cleanup_known_orphans(
    known_session_ids: tuple[str, ...] = KNOWN_ORPHAN_SESSION_IDS,
) -> list[int]:
    """One-time startup cleanup for sessions already flagged in prior snapshots.

    For each session id in ``known_session_ids`` that's still attached to an
    investigation row with ``status='running'``, atomically flip the row to
    ``orphan_dead_lettered`` and log a one-line note. No admin DM — the
    operator already named these sessions in the carry-over snapshot, and
    double-pinging would be noise.

    Returns the list of investigation IDs that were transitioned. Empty list
    on no-op (none found, all already terminal, DB unavailable). Best-effort:
    never raises.
    """
    if not DATABASE_URL or not known_session_ids:
        return []
    transitioned: list[int] = []
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                for sid in known_session_ids:
                    if not sid:
                        continue
                    cur.execute(
                        "UPDATE investigations "
                        "SET status = 'orphan_dead_lettered', "
                        "    completed_at = NOW(), "
                        "    error_message = COALESCE(error_message, %s) "
                        "WHERE session_id = %s AND status = 'running' "
                        "RETURNING id",
                        (
                            "known orphan from prior carry-over snapshot "
                            "(Task #23 cleanup)",
                            sid,
                        ),
                    )
                    rows = cur.fetchall() or []
                    for row in rows:
                        inv_id = row[0]
                        transitioned.append(int(inv_id))
                        log.info(
                            "cleanup_known_orphans: marked investigation %s "
                            "(session=%s) as orphan_dead_lettered",
                            inv_id,
                            sid,
                        )
                conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.debug(f"cleanup_known_orphans failed: {e}")
    return transitioned


def fail_snapshot(snapshot_id: int):
    """Mark a snapshot as failed."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE snapshots SET status = 'failed', completed_at = NOW() WHERE id = %s",
                (snapshot_id,),
            )
            conn.commit()
    except Exception:
        conn.rollback()
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Channel verbosity preferences (Plan #31 E2).
#
# slack_bot._resolve_verbosity reads here for the prefix-vs-pref resolution
# order, and slack_bot.on_verbosity_command writes here for the /verbosity
# slash command. Both helpers degrade gracefully when the DB is unavailable
# — the verbosity layer must never break the message handler loop.
# ─────────────────────────────────────────────────────────────────────────────


def get_channel_verbosity(channel_id: str) -> Optional[str]:
    """Return the stored verbosity for ``channel_id``, or None if unset.

    Returns None on every failure mode (no DATABASE_URL, missing table,
    query error). Caller is responsible for falling back to a default.
    """
    if not DATABASE_URL or not channel_id:
        return None
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT verbosity FROM channel_verbosity_preferences "
                    "WHERE channel_id = %s",
                    (channel_id,),
                )
                row = cur.fetchone()
                return row[0] if row else None
        finally:
            conn.close()
    except Exception as e:
        log.debug(f"get_channel_verbosity({channel_id}) failed: {e}")
        return None


def set_channel_verbosity(
    channel_id: str, verbosity: str, updated_by: Optional[str] = None
) -> bool:
    """Upsert the channel's verbosity preference.

    Returns True on success, False if the DB is unavailable or the write
    fails. Caller (slash command handler) surfaces a user-visible warning
    on False so the operator knows the preference was not persisted.
    """
    if not DATABASE_URL or not channel_id:
        return False
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO channel_verbosity_preferences "
                    "(channel_id, verbosity, updated_at, updated_by) "
                    "VALUES (%s, %s, NOW(), %s) "
                    "ON CONFLICT (channel_id) DO UPDATE SET "
                    "verbosity = EXCLUDED.verbosity, "
                    "updated_at = NOW(), "
                    "updated_by = EXCLUDED.updated_by",
                    (channel_id, verbosity, updated_by),
                )
                conn.commit()
                return True
        finally:
            conn.close()
    except Exception as e:
        log.debug(f"set_channel_verbosity({channel_id}, {verbosity}) failed: {e}")
        return False


# ── Managed Agents docs-diff snapshot helpers ─────────────────────────────
# These persist the doc-page hashes that ``self_improve.check_for_updates``
# diffs against. Stored in Postgres (not /tmp) so the baseline survives
# Railway container restarts — otherwise every fresh deploy looks like a
# brand-new doc set and the diff DM is a useless "everything is new" report.


def load_managed_agents_doc_snapshots() -> dict:
    """Return ``{page_url: content_hash, ...}`` for tracked Managed Agents
    doc pages. Empty dict when DATABASE_URL is unset or the read fails — the
    caller falls back to the filesystem cache in dev environments."""
    if not DATABASE_URL:
        return {}
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT page_url, content_hash FROM managed_agents_doc_snapshots"
                )
                rows = cur.fetchall()
                return {row[0]: row[1] for row in rows}
        finally:
            conn.close()
    except Exception:
        log.exception("Failed to load Managed Agents doc snapshots from DB")
        return {}


def save_managed_agents_doc_snapshots(hashes: dict) -> bool:
    """Upsert one row per page. Returns True on success, False when no DB is
    available (or the write fails) so callers can fall back to filesystem.

    Idempotent on ``page_url``: re-running the diff with unchanged content
    refreshes ``last_run`` but leaves ``content_hash`` alone via the
    ``WHERE...DISTINCT FROM`` predicate (preserves ``fetched_at``).
    """
    if not DATABASE_URL or not hashes:
        return False
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                for page_url, content_hash in hashes.items():
                    cur.execute(
                        "INSERT INTO managed_agents_doc_snapshots "
                        "(page_url, content_hash, fetched_at, last_run) "
                        "VALUES (%s, %s, NOW(), NOW()) "
                        "ON CONFLICT (page_url) DO UPDATE SET "
                        "  content_hash = EXCLUDED.content_hash, "
                        "  fetched_at = CASE "
                        "    WHEN managed_agents_doc_snapshots.content_hash "
                        "         IS DISTINCT FROM EXCLUDED.content_hash "
                        "    THEN NOW() "
                        "    ELSE managed_agents_doc_snapshots.fetched_at "
                        "  END, "
                        "  last_run = NOW()",
                        (page_url, content_hash or ""),
                    )
                conn.commit()
                return True
        finally:
            conn.close()
    except Exception:
        log.exception("Failed to persist Managed Agents doc snapshots to DB")
        return False


# ── Surface state helpers (Plan #33) ─────────────────────────────────────
# CRUD layer over ``surface_state``. The renderer/pusher read the cached
# Markdown to skip no-op Canvas writes; the compute layer upserts a fresh
# row whenever portco state changes (new finding, decision logged, etc.).
# All helpers degrade gracefully when DATABASE_URL is unset — local dev
# can still exercise the rest of the surface pipeline without a Postgres
# instance.


def get_surface_state(portco: str) -> Optional[dict]:
    """Return the cached surface state for a portco.

    Returns a dict with keys ``state_json`` (decoded), ``rendered_md``,
    ``canvas_id``, ``version``, ``updated_at`` — or None when the row
    doesn't exist or the DB is unavailable.
    """
    if not DATABASE_URL or not portco:
        return None
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT state_json, rendered_md, canvas_id, version, "
                    "updated_at FROM surface_state WHERE portco = %s",
                    (portco,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                state_json, rendered_md, canvas_id, version, updated_at = row
                # psycopg2 returns JSONB as a parsed dict; defensively
                # handle the str case (older drivers) so we always hand
                # callers a dict.
                if isinstance(state_json, str):
                    try:
                        state_json = json.loads(state_json)
                    except Exception:
                        state_json = {}
                return {
                    "state_json": state_json or {},
                    "rendered_md": rendered_md or "",
                    "canvas_id": canvas_id,
                    "version": int(version) if version is not None else 1,
                    "updated_at": updated_at,
                }
        finally:
            conn.close()
    except Exception as e:
        log.debug(f"get_surface_state failed for {portco}: {e}")
        return None


def upsert_surface_state(
    portco: str,
    state_json: dict,
    rendered_md: str,
    canvas_id: Optional[str] = None,
) -> bool:
    """Insert or update the surface state row for a portco.

    Always bumps ``version`` and refreshes ``updated_at``. Returns True
    on success, False when the DB is unavailable or the write fails.
    """
    if not DATABASE_URL or not portco:
        return False
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO surface_state "
                    "(portco, state_json, rendered_md, canvas_id, "
                    " updated_at, version) "
                    "VALUES (%s, %s::jsonb, %s, %s, NOW(), 1) "
                    "ON CONFLICT (portco) DO UPDATE SET "
                    "  state_json = EXCLUDED.state_json, "
                    "  rendered_md = EXCLUDED.rendered_md, "
                    "  canvas_id = EXCLUDED.canvas_id, "
                    "  updated_at = NOW(), "
                    "  version = surface_state.version + 1",
                    (
                        portco,
                        json.dumps(state_json or {}),
                        rendered_md or "",
                        canvas_id,
                    ),
                )
                conn.commit()
                return True
        finally:
            conn.close()
    except Exception as e:
        log.debug(f"upsert_surface_state failed for {portco}: {e}")
        return False


def bump_surface_version(portco: str) -> Optional[int]:
    """Increment the version counter for a portco's surface row.

    Used by the renderer when it wants to invalidate a cached Markdown
    body without rewriting state_json. Returns the new version number,
    or None if the row doesn't exist or the DB is unavailable.
    """
    if not DATABASE_URL or not portco:
        return None
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE surface_state SET "
                    "  version = version + 1, "
                    "  updated_at = NOW() "
                    "WHERE portco = %s "
                    "RETURNING version",
                    (portco,),
                )
                row = cur.fetchone()
                conn.commit()
                return int(row[0]) if row else None
        finally:
            conn.close()
    except Exception as e:
        log.debug(f"bump_surface_version failed for {portco}: {e}")
        return None


def get_canvas_id(portco: str) -> Optional[str]:
    """Return the Slack Canvas ID associated with a portco's surface row.

    The pusher uses this to PATCH an existing Canvas instead of creating
    a new one. Returns None when the row doesn't exist, the canvas hasn't
    been created yet, or the DB is unavailable.
    """
    if not DATABASE_URL or not portco:
        return None
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT canvas_id FROM surface_state WHERE portco = %s",
                    (portco,),
                )
                row = cur.fetchone()
                return row[0] if row and row[0] else None
        finally:
            conn.close()
    except Exception as e:
        log.debug(f"get_canvas_id failed for {portco}: {e}")
        return None
