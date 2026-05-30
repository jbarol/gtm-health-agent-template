-- Adds discovery_call_booked column to leads table to support
-- Postgres-routed historical queries on the Discovery_Call_Booked__c
-- Salesforce custom field. Prior to this migration the field was
-- NULL in every Postgres row, forcing every Discovery-Call query
-- through live MCP (token blowup risk).
--
-- If Salesforce describe(Lead) returns Boolean for Discovery_Call_Booked__c
-- instead of DateTime, replace TIMESTAMPTZ with BOOLEAN and re-apply.
ALTER TABLE leads
    ADD COLUMN IF NOT EXISTS discovery_call_booked TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS leads_discovery_call_booked_idx
    ON leads(portco_key, discovery_call_booked)
    WHERE discovery_call_booked IS NOT NULL;
