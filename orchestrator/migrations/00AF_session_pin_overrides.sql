-- Plan #44 Task #10 — Slack `/pin` hot pin override (decision row #20).
--
-- The default agent-version pin source is ``agents/active_versions.json``,
-- a file under source control updated by ``agents/update_prompts.py`` after
-- every prompt deploy. The file is the durable baseline.
--
-- ``session_pin_overrides`` is the **operator-controlled** override layer
-- that sits ABOVE the file pin and BELOW the SDK default. The /pin Slack
-- command writes here. ``session_runner.py``'s session-create code reads
-- ``effective_pin(agent_name, file_pin)`` from ``version_pin_overrides.py``,
-- which checks this table first and falls back to ``file_pin`` (and then
-- to the SDK default of "latest").
--
-- Persisting to Postgres (NOT an ephemeral local file) means an override
-- set via Slack at 2am survives the next Railway redeploy. Decision row #20
-- on Plan #44 rejected the ephemeral-file approach as a false-durability
-- trap.
--
-- /health exposes the effective pin AND its source (file | override |
-- default) AND actor + timestamp so the operator can audit who pinned
-- what (decision row #20 second sentence).

CREATE TABLE IF NOT EXISTS session_pin_overrides (
    agent_name TEXT        PRIMARY KEY,
    version    INT         NOT NULL,
    actor      TEXT        NOT NULL,
    ts         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Audit index — admins reviewing recent overrides do so by timestamp.
CREATE INDEX IF NOT EXISTS idx_session_pin_overrides_ts
    ON session_pin_overrides(ts DESC);
