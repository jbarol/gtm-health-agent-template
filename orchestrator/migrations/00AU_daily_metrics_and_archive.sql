-- Incident 2026-06-16 — long-term GTM history without unbounded volume growth.
--
-- Background: the nightly SF→Postgres sync appends a FULL copy of every
-- object (accounts, contacts, leads, opportunities) under a new snapshot_id
-- and nothing ever removed them (db_adapter.py header: "prior snapshots are
-- never deleted"). At ~125 MB/snapshot that is ~45 GB/year — the 5 GB
-- Postgres volume filled and wedged the DB in an end-of-recovery checkpoint
-- crash loop ("No space left on device").
--
-- Fix is a three-tier retention model that keeps the history the snapshots
-- exist for while bounding the hot DB:
--   1. daily_metrics  — compact per-portco-per-day rollup, kept FOREVER.
--                       This is the "what was pipeline on date X / what
--                       changed" layer. A few KB/day.
--   2. Parquet archive — full nightly row set written to object storage
--                       (snapshots.archived_at / archive_uri track it),
--                       kept FOREVER, ~50x cheaper per GB than the DB volume.
--   3. hot raw window  — the bulky child rows live in Postgres only ~60 days
--                       (purge gated on rollup + archive, see db_adapter).
--
-- The snapshots metadata row itself is NEVER deleted — it stays as the
-- forever index alongside daily_metrics; only the heavy child rows age out.
--
-- Safe to re-run. Every DDL is IF NOT EXISTS / ADD COLUMN IF NOT EXISTS.

-- Tier 1: forever metric rollup.
CREATE TABLE IF NOT EXISTS daily_metrics (
    id SERIAL PRIMARY KEY,
    portco_key TEXT NOT NULL REFERENCES portcos(key),
    snapshot_id INTEGER REFERENCES snapshots(id) ON DELETE SET NULL,
    snapshot_date DATE NOT NULL,
    -- Flexible metric bag. Stored as JSONB so the rollup set can grow
    -- without a migration; the full raw rows live in the Parquet archive
    -- forever, so any metric we forgot today can be recomputed later.
    metrics JSONB NOT NULL DEFAULT '{}',
    computed_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(portco_key, snapshot_date)
);

CREATE INDEX IF NOT EXISTS idx_daily_metrics_portco_date
    ON daily_metrics(portco_key, snapshot_date DESC);

-- Tiers 1+2 bookkeeping on the snapshot row. The purge (Tier 3) reads
-- these to decide whether a snapshot's heavy child rows are safe to drop:
-- metrics_rolled_up_at proves Tier 1 captured the day; archived_at proves
-- Tier 2 persisted the raw rows off-volume.
ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS metrics_rolled_up_at TIMESTAMPTZ;
ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;
ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS archive_uri TEXT;
-- Set once the heavy child rows have been purged, so a re-run of the purge
-- is a cheap no-op and operators can see which snapshots are "rollup-only".
ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS raw_purged_at TIMESTAMPTZ;
