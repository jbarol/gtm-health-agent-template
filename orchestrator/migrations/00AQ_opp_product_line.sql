-- Adds product_line column to opportunities table so Postgres-routed
-- queries can do ``Industry × Product Line`` without a live MCP round
-- trip. Motivation: the 2026-05-18 wtaylor incident showed a sub-agent
-- (sub3) reach for ``Product_Line__c`` directly off the Opportunity in
-- SF because the column did not exist locally. Every cross-cut by
-- product line had to pay a fresh SF query cost; this column makes the
-- dimension first-class alongside ``stage_name``, ``record_type``, and
-- ``account_id``.
--
-- Source field: ``Opportunity.Product_Line__c`` (Salesforce custom
-- field). Filtered through ``_build_select_clause`` against
-- describeSObject so a portco whose org does not have the field is
-- not nuked — the column simply stays NULL for that portco's rows.
-- See OPTIONAL_SYNC_FIELDS["Opportunity"] in
-- ``orchestrator/session_runner.py``.
--
-- Disambiguation rule: a single string straight off the Opportunity
-- record. No multi-line-item join — we deliberately avoided
-- ``OpportunityLineItem.ProductFamily__c`` to keep the sync flat and
-- single-row-per-opp. If a portco needs line-item-level granularity
-- later, add a separate ``opportunity_line_items`` table; do not
-- overload this column.
--
-- Forward-only. Backwards compatible (additive + nullable). No down
-- migration — to drop the column the operator runs
-- ``ALTER TABLE opportunities DROP COLUMN product_line;`` by hand
-- after confirming no view/query references it.
--
-- Backfill: prior snapshots (#14 and earlier) keep NULL. The next
-- nightly sync populates snapshot #15 onward. Operators who need
-- historical coverage run ``bin/backfill_opportunity_product_line.py``
-- manually after deploy.
ALTER TABLE opportunities
    ADD COLUMN IF NOT EXISTS product_line TEXT;

CREATE INDEX IF NOT EXISTS idx_opps_product_line
    ON opportunities(snapshot_id, product_line)
    WHERE product_line IS NOT NULL;
