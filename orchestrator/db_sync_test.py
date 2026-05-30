"""Tests for the nightly Salesforce → Postgres sync path (db_adapter.write_records).

Regression coverage for fix/lead-sync-schema: the four Lead custom-field
columns (Funnel_Stage__c, MQL_SDR_Accepted_Date_Time__c,
SDR_Qualified_Date_Time__c, Discovery_Call_Booked__c) must round-trip
from a SF record dict into the leads table without losing any value.

Fully mocked — no real Postgres. Uses an in-memory list keyed off the
INSERT statement to satisfy the contract:

    write_records(snapshot_id, portco_key, "Lead", [record])
    →  one row appended with every column populated from the SF dict;
       any field not provided by SF stays None.

Run:
    cd orchestrator && python3 -m pytest db_sync_test.py -q
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

# Config.py raises if these are missing. setdefault means a real .env
# (when present) wins — mirrors surface_db_test bootstrap.
for _key, _value in {
    "ANTHROPIC_API_KEY": "sk-ant-test",
    "SLACK_BOT_TOKEN": "xoxb-test",
    "SLACK_APP_TOKEN": "xapp-test",
    "SLACK_CHANNEL_ID": "C0TEST",
    "ENVIRONMENT_ID": "env_test",
    "DREAM_AGENT_ID": "agent_test_dream",
    "COORDINATOR_ID": "agent_test_coord",
    "QUICK_AGENT_ID": "agent_test_quick",
    "METHODOLOGY_STORE_ID": "memstore_test_m",
    "HEALTH_STORE_ID": "memstore_test_h",
}.items():
    os.environ.setdefault(_key, _value)


# ---------------------------------------------------------------------------
# Fake DB plumbing — in-memory list of (sql, params)
# ---------------------------------------------------------------------------


class _FakeCursor:
    """Cursor that records every (sql, params) the production code emits.

    After Task #9 (2026-05-14): write_records issues a DELETE for prior
    rows in the (snapshot_id, portco_key) slice followed by N INSERTs.
    The fake captures both so tests can assert on the DELETE pre-step
    AND on each INSERT's column order + full positional payload."""

    def __init__(self, store: dict):
        self._store = store

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql: str, params=None):
        sql_norm = " ".join(sql.split())
        self._store.setdefault("calls", []).append((sql_norm, params))

    def fetchone(self):
        return None


class _FakeConn:
    def __init__(self, store: dict):
        self._store = store

    def cursor(self, cursor_factory=None):
        return _FakeCursor(self._store)

    def commit(self):
        self._store["committed"] = True

    def rollback(self):
        self._store["rolledback"] = True

    def close(self):
        self._store["closed"] = True


@pytest.fixture
def fake_db():
    store: dict = {}
    with (
        patch("db_adapter.DATABASE_URL", "postgres://test"),
        patch("db_adapter._connect", lambda: _FakeConn(store)),
    ):
        yield store


# ---------------------------------------------------------------------------
# Round-trip: every Lead column from SF populates without dropping fields
# ---------------------------------------------------------------------------


def test_lead_round_trip_with_discovery_call_booked(fake_db):
    """The four Lead custom fields survive write_records intact.

    Regression for fix/lead-sync-schema. Before the fix:
      - Discovery_Call_Booked__c was not in the INSERT column list at all
        (Postgres lacked the column) → field was silently dropped.
      - Funnel_Stage__c / MQL_SDR_Accepted_Date_Time__c /
        SDR_Qualified_Date_Time__c columns existed but were NULL on every
        row because the SELECT clause in SYNC_OBJECTS["Lead"] did not
        request them.

    After the fix, the SELECT clause requests all four (verified in
    a separate assertion below), the INSERT lists discovery_call_booked,
    and write_records passes every value through positionally.
    """
    import db_adapter

    sf_lead = {
        "Id": "00Q000000000ABC",
        "Name": "Acme Corp",
        "Status": "Open - Not Contacted",
        "LeadSource": "Inbound — web",
        "OwnerId": "0050000000ZYX",
        "Owner": {"Name": "Rep Smith"},
        "CreatedDate": "2025-09-12T17:00:00.000+0000",
        "ConvertedDate": None,
        "IsConverted": False,
        "Funnel_Stage__c": "MQL",
        "MQL_SDR_Accepted_Date_Time__c": "2025-09-14T12:30:00.000+0000",
        "SDR_Qualified_Date_Time__c": "2025-09-18T15:00:00.000+0000",
        "Discovery_Call_Booked__c": "2025-09-20T20:00:00.000+0000",
    }

    db_adapter.write_records(
        snapshot_id=42,
        portco_key="acme",
        object_type="Lead",
        records=[sf_lead],
    )

    calls = fake_db["calls"]
    # Task #9 (2026-05-14): write_records now DELETEs prior rows for
    # (snapshot_id, portco_key) before inserting, so the call sequence
    # is DELETE + N INSERTs instead of N INSERTs alone.
    assert len(calls) == 2, "expected one DELETE + one INSERT per lead record"
    delete_sql, delete_params = calls[0]
    assert delete_sql.startswith("DELETE FROM leads ")
    assert delete_params == (42, "acme")
    sql, params = calls[1]

    # 1) Column list includes the new discovery_call_booked + the three
    #    previously-NULL columns.
    assert "INSERT INTO leads" in sql
    for column in (
        "discovery_call_booked",
        "funnel_stage",
        "mql_date",
        "sql_date",
    ):
        assert column in sql, f"missing {column} in INSERT column list"

    # 2) Positional payload matches column order (snapshot_id, portco_key,
    #    sf_id, name, status, lead_source, owner_id, owner_name,
    #    created_date, converted_date, is_converted, funnel_stage,
    #    mql_date, sql_date, discovery_call_booked).
    assert params == (
        42,
        "acme",
        "00Q000000000ABC",
        "Acme Corp",
        "Open - Not Contacted",
        "Inbound — web",
        "0050000000ZYX",
        "Rep Smith",
        "2025-09-12T17:00:00.000+0000",
        None,
        False,
        "MQL",
        "2025-09-14T12:30:00.000+0000",
        "2025-09-18T15:00:00.000+0000",
        "2025-09-20T20:00:00.000+0000",
    )
    assert fake_db.get("committed") is True


def test_lead_round_trip_missing_custom_fields_keeps_nulls(fake_db):
    """Portcos without the SF custom fields write NULLs rather than crash."""
    import db_adapter

    sf_lead = {
        "Id": "00Q000000000DEF",
        "Name": "No-CF Co",
        "Status": "Open - Contacted",
        "LeadSource": "Trade show",
        "OwnerId": "0050000000ABC",
        "Owner": {"Name": "Rep Doe"},
        "CreatedDate": "2025-10-01T12:00:00.000+0000",
        "ConvertedDate": None,
        "IsConverted": False,
        # Discovery_Call_Booked__c / Funnel_Stage__c / MQL_/SDR_ dates
        # intentionally absent — mirrors a portco SF org that lacks the
        # custom-field package. r.get() must return None and the INSERT
        # must still succeed.
    }

    db_adapter.write_records(
        snapshot_id=99,
        portco_key="acme",
        object_type="Lead",
        records=[sf_lead],
    )

    # Task #9 (2026-05-14): the INSERT now sits at index 1, behind the
    # idempotency DELETE pre-step at index 0.
    sql, params = fake_db["calls"][1]
    assert "INSERT INTO leads" in sql
    # Last four positional args are funnel_stage, mql_date, sql_date,
    # discovery_call_booked — all None.
    assert params[-4:] == (None, None, None, None)


def test_sync_objects_lead_select_clause_requests_custom_fields():
    """SYNC_OBJECTS['Lead'] must SELECT all 4 custom fields.

    Without this the INSERT writes NULL even on portcos that have the
    fields populated in SF. Regression guard for the original bug.
    """
    from session_runner import SYNC_OBJECTS

    lead_clause = SYNC_OBJECTS["Lead"]
    for field in (
        "Discovery_Call_Booked__c",
        "Funnel_Stage__c",
        "MQL_SDR_Accepted_Date_Time__c",
        "SDR_Qualified_Date_Time__c",
    ):
        assert field in lead_clause, (
            f"SYNC_OBJECTS['Lead'] missing {field} — SOQL SELECT clause "
            "will write NULL to Postgres on next sync."
        )


# ---------------------------------------------------------------------------
# Opportunity product_line round-trip — the wtaylor-incident motivation
# ---------------------------------------------------------------------------


def test_opportunity_round_trip_with_product_line(fake_db):
    """SF Product_Line__c lands in opportunities.product_line.

    Regression guard for ``feat/sync-product-line`` (2026-05-19). Before
    this change, a sub-agent investigating ``Industry × Product Line``
    had to pay a live SF MCP query for every cross-cut because the
    column did not exist in Postgres. With the new column, the
    dimension is first-class on every Opportunity row.
    """
    import db_adapter

    sf_opp = {
        "Id": "0061",
        "Name": "Acme Opp",
        "StageName": "Prospecting",
        "Amount": 50000,
        "CloseDate": "2026-06-30",
        "CreatedDate": "2026-01-01T00:00:00.000+0000",
        "LastActivityDate": None,
        "LastModifiedDate": "2026-05-01T00:00:00.000+0000",
        "OwnerId": "0051",
        "Owner": {"Name": "Rep One"},
        "LeadSource": "Inbound",
        "RecordType": {"Name": "New Business"},
        "IsClosed": False,
        "IsWon": False,
        "Probability": 25,
        "FiscalQuarter": 2,
        "FiscalYear": 2026,
        "AccountId": "0011",
        "Product_Line__c": "Acme Advanced",
    }

    db_adapter.write_records(
        snapshot_id=15,
        portco_key="acme",
        object_type="Opportunity",
        records=[sf_opp],
    )

    # call[0] is the idempotency DELETE, call[1] is the INSERT.
    sql, params = fake_db["calls"][1]
    assert "INSERT INTO opportunities" in sql
    assert "product_line" in sql, (
        "INSERT column list missing product_line — opportunities.product_line "
        "will stay NULL on every sync."
    )
    # product_line is the last positional in the new column order — see
    # db_adapter.write_records.
    assert params[-1] == "Acme Advanced"
    assert fake_db.get("committed") is True


def test_opportunity_round_trip_missing_product_line_keeps_null(fake_db):
    """Portcos whose SF org lacks Product_Line__c write NULL, not crash.

    Same resilience guarantee as the Lead custom-field tests: the column
    is additive + nullable, ``r.get('Product_Line__c')`` returns None
    when the field is absent, and the INSERT still succeeds.
    """
    import db_adapter

    sf_opp = {
        "Id": "0062",
        "Name": "No-PL Opp",
        "StageName": "Qualification",
        "Amount": 12000,
        "CloseDate": "2026-07-30",
        "CreatedDate": "2026-02-01T00:00:00.000+0000",
        "LastActivityDate": None,
        "LastModifiedDate": "2026-05-01T00:00:00.000+0000",
        "OwnerId": "0051",
        "Owner": {"Name": "Rep Two"},
        "LeadSource": "Outbound",
        "RecordType": {"Name": "New Business"},
        "IsClosed": False,
        "IsWon": False,
        "Probability": 10,
        "FiscalQuarter": 3,
        "FiscalYear": 2026,
        "AccountId": "0012",
        # Product_Line__c intentionally absent — mirrors a portco SF org
        # that has not provisioned the field. r.get() must return None
        # and the INSERT must still succeed.
    }

    db_adapter.write_records(
        snapshot_id=15,
        portco_key="acme",
        object_type="Opportunity",
        records=[sf_opp],
    )

    sql, params = fake_db["calls"][1]
    assert "INSERT INTO opportunities" in sql
    # product_line is the last positional in the new column order.
    assert params[-1] is None


def test_sync_objects_opportunity_select_clause_requests_product_line():
    """SYNC_OBJECTS['Opportunity'] must SELECT Product_Line__c.

    Without this the INSERT writes NULL even on portcos that have the
    field populated in SF — same failure mode as the original Lead bug
    that motivated fix/lead-sync-schema. Regression guard for the
    wtaylor incident.
    """
    from session_runner import SYNC_OBJECTS

    opp_clause = SYNC_OBJECTS["Opportunity"]
    assert "Product_Line__c" in opp_clause, (
        "SYNC_OBJECTS['Opportunity'] missing Product_Line__c — SOQL SELECT "
        "clause will write NULL to opportunities.product_line on next sync."
    )


def test_optional_sync_fields_opportunity_includes_product_line():
    """Product_Line__c must be in OPTIONAL_SYNC_FIELDS['Opportunity'].

    Without this, a portco whose SF org has not provisioned the field
    would fail the entire Opportunity sync with INVALID_FIELD instead
    of writing NULL for the column. Same protection pattern as the
    Lead custom fields and Account fields.
    """
    from session_runner import OPTIONAL_SYNC_FIELDS

    assert "Opportunity" in OPTIONAL_SYNC_FIELDS, (
        "OPTIONAL_SYNC_FIELDS missing Opportunity entry — a portco without "
        "Product_Line__c will fail the whole Opportunity sync with "
        "INVALID_FIELD instead of degrading gracefully."
    )
    assert "Product_Line__c" in OPTIONAL_SYNC_FIELDS["Opportunity"]


def test_build_select_clause_drops_opportunity_product_line_when_missing():
    """describe gating drops Product_Line__c on portcos without the field.

    Mirrors the Lead-field tests below. Without this gate the SOQL
    rejects the whole SELECT with INVALID_FIELD and the Opportunity
    sync writes 0 rows for that portco — observed failure mode for
    Account.Contract_Status__c on 2026-05-14 before the same gate was
    applied there.
    """
    from session_runner import (
        OPTIONAL_SYNC_FIELDS,
        SYNC_OBJECTS,
        _build_select_clause,
    )

    # describe returns every standard Opportunity field but does NOT
    # include Product_Line__c — simulates a portco SF org without the
    # custom field.
    available = {
        "Id",
        "Name",
        "StageName",
        "Amount",
        "CloseDate",
        "CreatedDate",
        "LastActivityDate",
        "LastModifiedDate",
        "OwnerId",
        "Owner.Name",
        "LeadSource",
        "RecordType.Name",
        "IsClosed",
        "IsWon",
        "Probability",
        "FiscalQuarter",
        "FiscalYear",
        "AccountId",
    }

    select_clause, missing = _build_select_clause(
        "Opportunity", SYNC_OBJECTS["Opportunity"], available
    )

    assert missing == ["Product_Line__c"]
    assert "Product_Line__c" in OPTIONAL_SYNC_FIELDS["Opportunity"]
    assert "Product_Line__c" not in select_clause
    # Required standard fields all survive the filter.
    for required in (
        "Id",
        "Name",
        "StageName",
        "Amount",
        "AccountId",
    ):
        assert required in select_clause


# ---------------------------------------------------------------------------
# Resilient SELECT building: describeSObject filters optional custom fields
# ---------------------------------------------------------------------------


def test_build_select_clause_drops_optional_fields_not_in_describe():
    """When describe reports only 2 of the 4 Lead custom fields, the SELECT
    clause must include only those 2 — not the 4 that SYNC_OBJECTS asks for.

    Regression for Codex review (PR #96, P1): without describe-driven
    filtering, a portco missing one of the custom fields would surface a
    SOQL "No such column" error and zero out the whole Lead sync.
    """
    from session_runner import (
        OPTIONAL_SYNC_FIELDS,
        SYNC_OBJECTS,
        _build_select_clause,
    )

    # Pretend describe returns only the standard fields + 2 of the 4
    # custom fields. Funnel_Stage__c + SDR_Qualified_Date_Time__c are
    # missing in this org.
    available = {
        "Id",
        "Name",
        "Status",
        "LeadSource",
        "OwnerId",
        "Owner.Name",
        "CreatedDate",
        "ConvertedDate",
        "IsConverted",
        "Discovery_Call_Booked__c",
        "MQL_SDR_Accepted_Date_Time__c",
    }

    select_clause, missing = _build_select_clause(
        "Lead", SYNC_OBJECTS["Lead"], available
    )

    # 1) Missing fields are exactly the two not in `available`, both
    #    flagged in OPTIONAL_SYNC_FIELDS.
    assert set(missing) == {
        "Funnel_Stage__c",
        "SDR_Qualified_Date_Time__c",
    }
    for f in missing:
        assert f in OPTIONAL_SYNC_FIELDS["Lead"]

    # 2) SELECT clause keeps the 2 present custom fields and drops the
    #    missing ones — operator gets a partial-sync, not a zero-sync.
    assert "Discovery_Call_Booked__c" in select_clause
    assert "MQL_SDR_Accepted_Date_Time__c" in select_clause
    assert "Funnel_Stage__c" not in select_clause
    assert "SDR_Qualified_Date_Time__c" not in select_clause

    # 3) Required (non-optional) fields all remain.
    for required in (
        "Id",
        "Name",
        "Status",
        "LeadSource",
        "OwnerId",
        "Owner.Name",
        "CreatedDate",
        "ConvertedDate",
        "IsConverted",
    ):
        assert required in select_clause


def test_build_select_clause_all_fields_present_returns_unchanged():
    """When every optional field is in describe, the SELECT clause is
    byte-identical to the static SYNC_OBJECTS entry."""
    from session_runner import SYNC_OBJECTS, _build_select_clause

    available = {
        "Id",
        "Name",
        "Status",
        "LeadSource",
        "OwnerId",
        "Owner.Name",
        "CreatedDate",
        "ConvertedDate",
        "IsConverted",
        "Discovery_Call_Booked__c",
        "Funnel_Stage__c",
        "MQL_SDR_Accepted_Date_Time__c",
        "SDR_Qualified_Date_Time__c",
    }

    select_clause, missing = _build_select_clause(
        "Lead", SYNC_OBJECTS["Lead"], available
    )

    assert missing == []
    assert select_clause == SYNC_OBJECTS["Lead"]


def test_build_select_clause_describe_failure_keeps_static_clause():
    """If describe returns an empty set (failure path), the SELECT clause
    is kept as-is so the real SOQL error — not a silenced one — surfaces.
    """
    from session_runner import SYNC_OBJECTS, _build_select_clause

    select_clause, missing = _build_select_clause("Lead", SYNC_OBJECTS["Lead"], set())

    assert missing == []
    assert select_clause == SYNC_OBJECTS["Lead"]


# ---------------------------------------------------------------------------
# Empty-batch safety — write_records([]) must NEVER wipe prior snapshot rows
# ---------------------------------------------------------------------------


def test_empty_batch_skips_delete_and_preserves_prior_rows():
    """``write_records(records=[])`` must short-circuit before opening the DB
    connection so an SF outage returning zero records can't nuke a healthy
    morning snapshot via the new DELETE pre-step.

    Regression for the self-review concern that motivated commit 2 of
    PR #180: the DELETE-then-INSERT contract is only safe when paired with
    an early-return on empty input. Without the early-return, a SOQL
    failure that yields ``records=[]`` would issue ``DELETE FROM <table>
    WHERE snapshot_id=%s AND portco_key=%s`` and commit it, leaving the
    snapshot empty for the rest of the day.

    Asserts the negative: ``_connect`` is never called, no SQL executes,
    no commit/rollback fires. The patch is at module-load time so the
    test would catch any future refactor that moves the guard after
    the connection open.
    """
    import db_adapter

    captured: dict = {"connect_calls": 0, "sql_calls": []}

    def _fail_if_called():
        captured["connect_calls"] += 1
        raise AssertionError(
            "write_records([]) opened a DB connection — early return is broken"
        )

    with (
        patch("db_adapter.DATABASE_URL", "postgres://test"),
        patch("db_adapter._connect", _fail_if_called),
    ):
        db_adapter.write_records(
            snapshot_id=42,
            portco_key="acme",
            object_type="Opportunity",
            records=[],
        )

    assert captured["connect_calls"] == 0
    assert captured["sql_calls"] == []
