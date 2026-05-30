-- Plan #44 Task #24 — `/flag` admin Slack command (decision row #25).
--
-- ``flag_overrides`` is a Postgres-backed override layer for in-process
-- feature flags. ``config.py`` reads each flag via
-- ``flag_overrides.get_flag(name, env_default)`` which returns the DB
-- override if present, else falls back to ``os.environ.get(name, env_default)``.
--
-- Operator workflow: at 2am during an incident, the Railway dashboard
-- isn't reachable from a phone. /flag lets the admin flip flags directly
-- from Slack:
--     /flag COMPRESSION_ENABLED false
--     /flag SMOKE_PROBE_LEVEL full
--     /flag SF_MCP_VIA_VAULT true
--
-- The Postgres-backed layer survives the next Railway redeploy — unlike
-- a process-local override which evaporates on container restart, exactly
-- when an incident response most needs to NOT lose the workaround.
--
-- Per decision row #25 the initial whitelist is enforced at the
-- Slack-command layer (``orchestrator/slack_bot.py``) — the DB table
-- itself accepts any (name, value) pair so a future flag doesn't need a
-- migration. The whitelist is intentionally short and incident-relevant.

CREATE TABLE IF NOT EXISTS flag_overrides (
    flag_name  TEXT        PRIMARY KEY,
    flag_value TEXT        NOT NULL,
    actor      TEXT        NOT NULL,
    ts         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Audit index for "what did we flip recently?" queries.
CREATE INDEX IF NOT EXISTS idx_flag_overrides_ts
    ON flag_overrides(ts DESC);
