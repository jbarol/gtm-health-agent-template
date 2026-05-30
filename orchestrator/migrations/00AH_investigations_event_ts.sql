-- Plan: Centralized lifecycle finalization for Slack reactions.
--
-- Adds ``event_ts`` to the ``investigations`` table so post-restart
-- recovery can repair the 👁/⏰ reaction on the user's ORIGINAL Slack
-- message (not the in-thread restart message). Without this column the
-- recovery path knows the channel + thread but not which message in the
-- thread is the one that should carry the lifecycle reaction.
--
-- Plan reference: /Users/jb/.claude/plans/except-what-i-really-binary-river.md
-- Incident reference: session sesn_EXAMPLE (2026-05-13)
-- went idle without calling post_report; the user's message stayed on ⏰
-- forever. Centralized terminalization (this plan) closes that gap.
--
-- TEXT not TIMESTAMPTZ: Slack ts values are strings like
-- "1737654321.000100". The reactions API requires the exact original
-- string — losing trailing zeros via TIMESTAMPTZ casting breaks
-- reactions.add. Preserve verbatim.
--
-- Partial index: cron-flow investigations (dream, forecast) leave
-- event_ts NULL because they aren't tied to a user message. Indexing
-- those NULL rows wastes space and pushes recovery scans through
-- irrelevant data.

ALTER TABLE investigations
    ADD COLUMN IF NOT EXISTS event_ts TEXT;

-- Composite index: ``recover_interrupted_investigations`` (session_runner.py)
-- needs to repair a reaction by (channel_id, event_ts). The partial WHERE
-- keeps the index small.
CREATE INDEX IF NOT EXISTS idx_investigations_event_ts
    ON investigations(channel_id, event_ts)
    WHERE event_ts IS NOT NULL;
