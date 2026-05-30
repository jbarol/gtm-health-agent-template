"""Tests for the snapshot dedupe guard in db_adapter.write_records (Task #9).

Background — 2026-05-14 same-day-rerun bug. ``create_snapshot`` upserts on
``(portco_key, snapshot_date)`` and re-returns the existing ``snapshot_id``
when a snapshot already exists for the day. Without a DELETE pre-step the
second sync of the same day INSERTed another full batch of rows alongside
the morning's batch — observed total 345,847 opportunities where 172,936
were expected, with the morning half carrying NULL ``account_id`` (pre PR
#170) and the afternoon half carrying the populated column.

The fix: ``write_records`` now DELETEs rows for
``(snapshot_id, portco_key)`` on the target table BEFORE re-inserting,
wrapped in a single transaction so a mid-batch failure rolls back the
DELETE too. This file pins that behavior.

Mocking follows the pattern in ``db_sync_test.py`` — a fake cursor that
records every (sql, params) call into an in-memory list.

Run:
    cd orchestrator && python3 -m pytest snapshot_dedupe_test.py -v
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

# Config.py raises if these are missing. setdefault means a real .env wins.
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
# Fake DB plumbing — in-memory list of (sql, params), with explicit
# transaction tracking so we can assert atomicity.
# ---------------------------------------------------------------------------


class _FakeCursor:
    """Records every (sql, params) the production code emits.

    ``raise_on_insert`` (set via _FakeConn.raise_on_insert) makes the
    cursor raise on the Nth INSERT after the DELETE. Used to prove that
    when the INSERT step fails, the prior DELETE rolls back too.
    """

    def __init__(self, conn: "_FakeConn"):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql: str, params=None):
        sql_norm = " ".join(sql.split())
        self._conn.calls.append((sql_norm, params))
        if sql_norm.startswith("INSERT INTO") and self._conn.raise_on_insert:
            # Crash on first INSERT so we can prove rollback semantics.
            self._conn.raise_on_insert = False
            raise RuntimeError("simulated DB write failure")

    def fetchone(self):
        return None


class _FakeConn:
    def __init__(self):
        self.calls: list[tuple[str, object]] = []
        self.committed = False
        self.rolledback = False
        self.closed = False
        self.raise_on_insert = False

    def cursor(self, cursor_factory=None):
        return _FakeCursor(self)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolledback = True

    def close(self):
        self.closed = True


@pytest.fixture
def fake_conn():
    """Yields a fresh _FakeConn patched into db_adapter._connect for one test."""
    conn = _FakeConn()
    with (
        patch("db_adapter.DATABASE_URL", "postgres://test"),
        patch("db_adapter._connect", lambda: conn),
    ):
        yield conn


# ---------------------------------------------------------------------------
# Minimal SF-shape record builders. Only the fields write_records reads.
# ---------------------------------------------------------------------------


def _opp(sf_id: str = "0061") -> dict:
    return {
        "Id": sf_id,
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
    }


def _lead(sf_id: str = "00Q1") -> dict:
    return {
        "Id": sf_id,
        "Name": "Acme Lead",
        "Status": "Open - Not Contacted",
        "LeadSource": "Trade show",
        "OwnerId": "0051",
        "Owner": {"Name": "Rep Lead"},
        "CreatedDate": "2026-01-01T00:00:00.000+0000",
        "ConvertedDate": None,
        "IsConverted": False,
    }


# ---------------------------------------------------------------------------
# 1. First write for a (snapshot_id, portco, object_type) just inserts.
#    The DELETE still fires (idempotency); it just removes zero rows on a
#    fresh table. We assert the call ordering and the table/scope params.
# ---------------------------------------------------------------------------


def test_first_write_issues_delete_then_insert(fake_conn):
    import db_adapter

    db_adapter.write_records(
        snapshot_id=8,
        portco_key="acme",
        object_type="Opportunity",
        records=[_opp("OPP-A"), _opp("OPP-B")],
    )

    # First call must be the DELETE for this (snapshot_id, portco) on
    # the ``opportunities`` table. Then one INSERT per record.
    calls = fake_conn.calls
    assert len(calls) == 3, f"expected 1 DELETE + 2 INSERTs, got {len(calls)}"

    delete_sql, delete_params = calls[0]
    assert delete_sql.startswith(
        "DELETE FROM opportunities WHERE snapshot_id = %s AND portco_key = %s"
    )
    assert delete_params == (8, "acme")

    for sql, _params in calls[1:]:
        assert sql.startswith("INSERT INTO opportunities")

    assert fake_conn.committed is True
    assert fake_conn.rolledback is False


# ---------------------------------------------------------------------------
# 2. Second write for the same (snapshot_id, portco, object_type) DELETEs
#    again before re-inserting. The second batch overwrites — final state
#    matches the second batch's row count, NOT the sum of both batches.
#    (We approximate "final state" by counting INSERT calls in the second
#     write only; the DELETE in front guarantees the first batch is gone.)
# ---------------------------------------------------------------------------


def test_second_write_deletes_before_reinsert(fake_conn):
    import db_adapter

    # First batch — 3 rows.
    db_adapter.write_records(
        snapshot_id=8,
        portco_key="acme",
        object_type="Opportunity",
        records=[_opp("OPP-1"), _opp("OPP-2"), _opp("OPP-3")],
    )

    first_call_count = len(fake_conn.calls)
    assert first_call_count == 4  # 1 DELETE + 3 INSERTs

    # Second batch — 2 rows. Must DELETE first, then INSERT 2 more.
    db_adapter.write_records(
        snapshot_id=8,
        portco_key="acme",
        object_type="Opportunity",
        records=[_opp("OPP-1"), _opp("OPP-2")],
    )

    second_batch_calls = fake_conn.calls[first_call_count:]
    assert len(second_batch_calls) == 3  # 1 DELETE + 2 INSERTs

    second_delete_sql, second_delete_params = second_batch_calls[0]
    assert second_delete_sql.startswith(
        "DELETE FROM opportunities WHERE snapshot_id = %s AND portco_key = %s"
    )
    assert second_delete_params == (8, "acme")
    for sql, _params in second_batch_calls[1:]:
        assert sql.startswith("INSERT INTO opportunities")

    # Net effect after both writes: the snapshot ends up holding the
    # second batch's 2 rows, not 3 + 2 = 5. Without the DELETE step the
    # bug would have left 5 rows where 2 were expected.


# ---------------------------------------------------------------------------
# 3. Different object_types within the same snapshot do not collide. An
#    Opportunity write must DELETE from ``opportunities`` only, never from
#    ``leads``. Codex P1 risk if the wrong table got nuked.
# ---------------------------------------------------------------------------


def test_object_type_isolation_no_cross_table_delete(fake_conn):
    import db_adapter

    # Write Leads first.
    db_adapter.write_records(
        snapshot_id=8,
        portco_key="acme",
        object_type="Lead",
        records=[_lead("L1"), _lead("L2")],
    )

    # Then write Opportunities. The Opp write's DELETE must hit
    # ``opportunities`` only — Leads stay intact.
    lead_call_count = len(fake_conn.calls)
    db_adapter.write_records(
        snapshot_id=8,
        portco_key="acme",
        object_type="Opportunity",
        records=[_opp("OPP-X")],
    )

    opp_calls = fake_conn.calls[lead_call_count:]
    assert len(opp_calls) == 2  # 1 DELETE + 1 INSERT
    opp_delete_sql, _ = opp_calls[0]
    assert opp_delete_sql.startswith("DELETE FROM opportunities ")
    assert "DELETE FROM leads" not in opp_delete_sql

    # Confirm at the corpus level: nowhere across both write_records
    # invocations did the code DELETE from a table that did not match
    # the object_type being written.
    for sql, _params in fake_conn.calls:
        if not sql.startswith("DELETE FROM "):
            continue
        # Lead writes touch leads; Opp writes touch opportunities.
        assert sql.startswith("DELETE FROM leads ") or sql.startswith(
            "DELETE FROM opportunities "
        )


# ---------------------------------------------------------------------------
# 4. Atomicity: if INSERT fails mid-batch, the surrounding transaction
#    rolls back so the DELETE never lands. The snapshot keeps its prior
#    rows; a botched retry never leaves the user with an empty snapshot.
# ---------------------------------------------------------------------------


def test_delete_rolls_back_when_insert_fails(fake_conn):
    import db_adapter

    fake_conn.raise_on_insert = True

    with pytest.raises(RuntimeError, match="simulated DB write failure"):
        db_adapter.write_records(
            snapshot_id=8,
            portco_key="acme",
            object_type="Opportunity",
            records=[_opp("OPP-A"), _opp("OPP-B")],
        )

    # DELETE was issued (call[0]); INSERT crashed on first attempt
    # (call[1]); commit never happened; rollback DID.
    assert any(
        sql.startswith("DELETE FROM opportunities ") for sql, _ in fake_conn.calls
    )
    assert fake_conn.committed is False
    assert fake_conn.rolledback is True


# ---------------------------------------------------------------------------
# 5. Lead / Contact / Account paths route to the right tables. Cheap
#    coverage so a future refactor of the table_by_object map cannot
#    silently break a single object_type.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 6. Empty batch is a no-op — the DELETE never runs, so an SF outage that
#    returns zero records on a same-day re-run cannot silently wipe the
#    morning's healthy data. session_runner's per-object exception
#    handler already records 0 as the failure-mode count; this guard
#    keeps the prior batch intact so /cost, canvas surfaces, and
#    downstream queries keep working until the next successful sync.
# ---------------------------------------------------------------------------


def test_empty_batch_skips_delete_and_insert(fake_conn):
    import db_adapter

    db_adapter.write_records(
        snapshot_id=8,
        portco_key="acme",
        object_type="Opportunity",
        records=[],
    )

    # No DB activity at all. _connect should not even be called, but
    # the fixture has us routed there — assert no SQL hit the cursor.
    assert fake_conn.calls == []
    assert fake_conn.committed is False


@pytest.mark.parametrize(
    "object_type,table_name,record_fn",
    [
        ("Lead", "leads", lambda: _lead("L1")),
        (
            "Contact",
            "contacts",
            lambda: {
                "Id": "003A",
                "Name": "Jane Doe",
                "Email": "jane@acme.com",
                "Title": "VP Sales",
                "AccountId": "001A",
                "OwnerId": "005A",
                "Owner": {"Name": "Rep Contact"},
                "LeadSource": "Inbound",
                "CreatedDate": "2026-01-01T00:00:00.000+0000",
                "LastActivityDate": None,
            },
        ),
        (
            "Account",
            "accounts",
            lambda: {
                "Id": "001A",
                "Name": "Acme Inc",
                "RecordType": {"Name": "Customer"},
                "Industry": "Manufacturing",
                "Customer_Tier__c": "Tier 1",
                "Contract_Status__c": "Active",
                "Region__c": "NA",
                "BillingCountry": "US",
                "CreatedDate": "2026-01-01T00:00:00.000+0000",
                "ARR__c": 250000,
            },
        ),
    ],
)
def test_delete_targets_matching_table_for_each_object_type(
    fake_conn, object_type, table_name, record_fn
):
    import db_adapter

    db_adapter.write_records(
        snapshot_id=8,
        portco_key="acme",
        object_type=object_type,
        records=[record_fn()],
    )

    delete_sql, delete_params = fake_conn.calls[0]
    assert delete_sql.startswith(
        f"DELETE FROM {table_name} WHERE snapshot_id = %s AND portco_key = %s"
    )
    assert delete_params == (8, "acme")
