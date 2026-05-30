-- Railway Postgres schema for GTM Health Agent
-- Daily snapshots from Salesforce. Append-only — never delete prior snapshots.

CREATE TABLE IF NOT EXISTS portcos (
    key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    sf_org_id TEXT,
    sf_instance_url TEXT,
    arr_tier TEXT,
    status TEXT DEFAULT 'active',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS snapshots (
    id SERIAL PRIMARY KEY,
    portco_key TEXT NOT NULL REFERENCES portcos(key),
    snapshot_date DATE NOT NULL,
    started_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    record_counts JSONB DEFAULT '{}',
    status TEXT DEFAULT 'running',
    UNIQUE(portco_key, snapshot_date)
);

CREATE TABLE IF NOT EXISTS opportunities (
    id SERIAL PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    portco_key TEXT NOT NULL REFERENCES portcos(key),
    sf_id TEXT NOT NULL,
    name TEXT,
    stage_name TEXT,
    amount DECIMAL(15,2),
    close_date DATE,
    created_date TIMESTAMPTZ,
    last_activity_date DATE,
    last_modified_date TIMESTAMPTZ,
    owner_id TEXT,
    owner_name TEXT,
    lead_source TEXT,
    record_type TEXT,
    is_closed BOOLEAN,
    is_won BOOLEAN,
    probability DECIMAL(5,2),
    fiscal_quarter INTEGER,
    fiscal_year INTEGER,
    -- account_id: links Opportunity → Account (sf_id of the related Account).
    -- Added 2026-05-14 after live repro sesn_EXAMPLE where
    -- the agent composed ``SELECT ... FROM opportunities o JOIN accounts a
    -- ON a.sf_id = o.accountid`` and Postgres returned ``column o.accountid
    -- does not exist``. Now nullable + indexed below so the JOIN is cheap.
    account_id TEXT,
    -- product_line: SF custom field ``Product_Line__c`` straight off the
    -- Opportunity. First-class so cross-cuts like ``Industry × Product Line``
    -- run against Postgres instead of a live SF MCP query. Filtered through
    -- ``_build_select_clause`` against describeSObject — portcos whose org
    -- lacks ``Product_Line__c`` simply write NULL. Added 2026-05-19 after
    -- the wtaylor incident, where sub3 had to hit live SF for every
    -- product-line cross-cut because Postgres only carried stage/record_type.
    -- See migrations/00AQ_opp_product_line.sql.
    product_line TEXT
);
-- Idempotency guard for partially-migrated databases: CREATE TABLE IF NOT
-- EXISTS above is a no-op if `opportunities` already exists from an OLDER
-- schema that predates account_id/product_line, which would then make the
-- indexes below fail with `column ... does not exist`. ADD COLUMN IF NOT
-- EXISTS is a no-op on a fresh DB (the column already came from CREATE TABLE)
-- and back-fills the column on an old one — so this file stays safe to run on
-- every boot regardless of the DB's prior shape.
ALTER TABLE opportunities ADD COLUMN IF NOT EXISTS account_id TEXT;
ALTER TABLE opportunities ADD COLUMN IF NOT EXISTS product_line TEXT;
CREATE INDEX IF NOT EXISTS idx_opps_account_id ON opportunities(account_id);
CREATE INDEX IF NOT EXISTS idx_opps_product_line
    ON opportunities(snapshot_id, product_line)
    WHERE product_line IS NOT NULL;

CREATE TABLE IF NOT EXISTS leads (
    id SERIAL PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    portco_key TEXT NOT NULL REFERENCES portcos(key),
    sf_id TEXT NOT NULL,
    name TEXT,
    status TEXT,
    lead_source TEXT,
    owner_id TEXT,
    owner_name TEXT,
    created_date TIMESTAMPTZ,
    converted_date TIMESTAMPTZ,
    is_converted BOOLEAN,
    funnel_stage TEXT,
    mql_date TIMESTAMPTZ,
    sql_date TIMESTAMPTZ,
    -- Discovery_Call_Booked__c from Salesforce. Defaulted to TIMESTAMPTZ
    -- per the naming-convention inference; flip to BOOLEAN + re-run the
    -- migration if describe(Lead) returns a checkbox type for this field.
    discovery_call_booked TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS contacts (
    id SERIAL PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    portco_key TEXT NOT NULL REFERENCES portcos(key),
    sf_id TEXT NOT NULL,
    name TEXT,
    email TEXT,
    title TEXT,
    account_id TEXT,
    owner_id TEXT,
    owner_name TEXT,
    lead_source TEXT,
    created_date TIMESTAMPTZ,
    last_activity_date DATE
);

CREATE TABLE IF NOT EXISTS accounts (
    id SERIAL PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    portco_key TEXT NOT NULL REFERENCES portcos(key),
    sf_id TEXT NOT NULL,
    name TEXT,
    record_type TEXT,
    industry TEXT,
    customer_tier TEXT,
    contract_status TEXT,
    region TEXT,
    billing_country TEXT,
    created_date TIMESTAMPTZ,
    arr DECIMAL(15,2)
);

-- Indexes for snapshot-based queries
CREATE INDEX IF NOT EXISTS idx_snapshots_portco_date ON snapshots(portco_key, snapshot_date DESC);
CREATE INDEX IF NOT EXISTS idx_opps_snapshot ON opportunities(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_opps_portco_stage ON opportunities(portco_key, stage_name);
CREATE INDEX IF NOT EXISTS idx_leads_snapshot ON leads(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_leads_portco_status ON leads(portco_key, status);
CREATE INDEX IF NOT EXISTS idx_contacts_snapshot ON contacts(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_accounts_snapshot ON accounts(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_accounts_portco_type ON accounts(portco_key, record_type);

-- Thread-to-session map: survives container restarts so follow-ups work after deploy.
-- Composite PK on (channel_id, thread_ts) — Slack ``thread_ts`` is only unique
-- within a channel, so multi-portco channels would otherwise collide. See
-- migration 00AJ_thread_sessions_channel_scope.sql for the rationale and
-- backfill.
CREATE TABLE IF NOT EXISTS thread_sessions (
    channel_id TEXT NOT NULL,
    thread_ts TEXT NOT NULL,
    session_id TEXT NOT NULL,
    portco_key TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    last_used_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (channel_id, thread_ts)
);

CREATE INDEX IF NOT EXISTS idx_thread_sessions_last_used ON thread_sessions(last_used_at DESC);

-- Investigation tracker: every ad-hoc and scheduled investigation is recorded here.
-- On container restart, rows with status='running' were interrupted and need recovery.
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
    container_id TEXT,
    event_ts TEXT
);
-- Idempotency guard (same rationale as opportunities above): event_ts was a
-- later addition, so back-fill it before the partial index that references it.
ALTER TABLE investigations ADD COLUMN IF NOT EXISTS event_ts TEXT;

CREATE INDEX IF NOT EXISTS idx_investigations_status ON investigations(status);
CREATE INDEX IF NOT EXISTS idx_investigations_thread ON investigations(thread_ts);
CREATE INDEX IF NOT EXISTS idx_investigations_event_ts
    ON investigations(channel_id, event_ts)
    WHERE event_ts IS NOT NULL;

-- Helper function: get latest snapshot ID for a portco
CREATE OR REPLACE FUNCTION latest_snapshot(p_portco TEXT)
RETURNS INTEGER AS $$
    SELECT id FROM snapshots
    WHERE portco_key = p_portco AND status = 'complete'
    ORDER BY snapshot_date DESC LIMIT 1;
$$ LANGUAGE sql STABLE;

-- Views reference the latest completed snapshot
CREATE OR REPLACE VIEW pipeline_by_stage AS
SELECT
    o.portco_key,
    o.stage_name,
    COUNT(*) as opp_count,
    SUM(o.amount) as total_amount,
    AVG(o.amount) as avg_amount,
    MIN(o.created_date) as oldest_created,
    COUNT(*) FILTER (WHERE o.close_date < CURRENT_DATE) as past_due_count,
    SUM(o.amount) FILTER (WHERE o.close_date < CURRENT_DATE) as past_due_amount
FROM opportunities o
JOIN snapshots s ON o.snapshot_id = s.id
WHERE o.is_closed = false
  AND s.id = latest_snapshot(o.portco_key)
GROUP BY o.portco_key, o.stage_name;

CREATE OR REPLACE VIEW pipeline_age_buckets AS
SELECT
    o.portco_key,
    o.stage_name,
    CASE
        WHEN CURRENT_DATE - o.created_date::date <= 30 THEN '0-30 days'
        WHEN CURRENT_DATE - o.created_date::date <= 60 THEN '31-60 days'
        WHEN CURRENT_DATE - o.created_date::date <= 90 THEN '61-90 days'
        WHEN CURRENT_DATE - o.created_date::date <= 180 THEN '91-180 days'
        WHEN CURRENT_DATE - o.created_date::date <= 365 THEN '181-365 days'
        ELSE '365+ days'
    END as age_bucket,
    COUNT(*) as opp_count,
    SUM(o.amount) as total_amount
FROM opportunities o
JOIN snapshots s ON o.snapshot_id = s.id
WHERE o.is_closed = false
  AND s.id = latest_snapshot(o.portco_key)
GROUP BY o.portco_key, o.stage_name, age_bucket;

CREATE OR REPLACE VIEW win_rate_by_quarter AS
SELECT
    o.portco_key,
    EXTRACT(YEAR FROM o.close_date) as close_year,
    EXTRACT(QUARTER FROM o.close_date) as close_quarter,
    COUNT(*) FILTER (WHERE o.is_won = true) as won_count,
    COUNT(*) FILTER (WHERE o.is_closed = true) as closed_count,
    CASE WHEN COUNT(*) FILTER (WHERE o.is_closed = true) > 0
        THEN ROUND(100.0 * COUNT(*) FILTER (WHERE o.is_won = true) / COUNT(*) FILTER (WHERE o.is_closed = true), 1)
        ELSE 0
    END as win_rate,
    SUM(o.amount) FILTER (WHERE o.is_won = true) as won_amount
FROM opportunities o
JOIN snapshots s ON o.snapshot_id = s.id
WHERE o.is_closed = true
  AND s.id = latest_snapshot(o.portco_key)
GROUP BY o.portco_key, close_year, close_quarter;

CREATE OR REPLACE VIEW lead_funnel AS
SELECT
    o.portco_key,
    o.funnel_stage,
    COUNT(*) as lead_count,
    COUNT(*) FILTER (WHERE o.is_converted = true) as converted_count,
    CASE WHEN COUNT(*) > 0
        THEN ROUND(100.0 * COUNT(*) FILTER (WHERE o.is_converted = true) / COUNT(*), 1)
        ELSE 0
    END as conversion_rate
FROM leads o
JOIN snapshots s ON o.snapshot_id = s.id
WHERE s.id = latest_snapshot(o.portco_key)
GROUP BY o.portco_key, o.funnel_stage;

-- Snapshot comparison view: compare any two snapshots for a portco
CREATE OR REPLACE VIEW snapshot_summary AS
SELECT
    s.id as snapshot_id,
    s.portco_key,
    s.snapshot_date,
    s.status,
    (s.record_counts->>'opportunities')::int as opp_count,
    (s.record_counts->>'leads')::int as lead_count,
    (s.record_counts->>'contacts')::int as contact_count,
    (s.record_counts->>'accounts')::int as account_count
FROM snapshots s
ORDER BY s.portco_key, s.snapshot_date DESC;
