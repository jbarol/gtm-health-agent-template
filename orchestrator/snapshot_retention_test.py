"""Tests for the snapshot retention model (incident 2026-06-16).

Covers the db_adapter Tier 1 (daily_metrics rollup) and Tier 3 (archive-gated
hot-window purge) functions. Mocks ``db_adapter._connect`` exactly like
``db_adapter_thread_sessions_test.py``.
"""

import json
from datetime import date
from unittest.mock import MagicMock, patch

import db_adapter


def _mock_conn_and_cursor():
    cursor = MagicMock()
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn, cursor


# --- _aggregate_snapshot_metrics: the pure math layer ---------------------


def test_aggregate_snapshot_metrics_computes_rates_and_blocks():
    cur = MagicMock()
    snap_date = date(2026, 6, 16)
    # Order of cur.fetchone()/fetchall() calls mirrors the function body.
    cur.fetchone.side_effect = [
        # opps agg: open_count, open_amt, avg, weighted, total, new_today,
        #           won_today_ct, won_today_amt, lost_today_ct, won_all, closed_all
        (10, 500000, 50000, 250000, 25, 3, 2, 80000, 1, 12, 16),
        # leads agg: total, new_today, converted, mql, sql, discovery
        (200, 7, 50, 120, 60, 18),
        # accounts: count, total_arr
        (40, 4200000),
        # contacts: count
        (350,),
    ]
    cur.fetchall.side_effect = [
        # by_stage
        [("Discovery", 6, 200000), ("Negotiation", 4, 300000)],
        # by_funnel_stage (Postgres COALESCEs NULL -> '(none)' in-SQL)
        [("MQL", 120), ("SQL", 60), ("(none)", 20)],
    ]

    m = db_adapter._aggregate_snapshot_metrics(cur, 99, snap_date)

    assert m["pipeline"]["open_opp_count"] == 10
    assert m["pipeline"]["open_pipeline_amount"] == 500000.0
    assert m["pipeline"]["weighted_open_pipeline"] == 250000.0
    # 12 won / 16 closed = 75%
    assert m["pipeline"]["lifetime_win_rate_pct"] == 75.0
    assert m["pipeline"]["by_stage"]["Negotiation"] == {
        "count": 4,
        "amount": 300000.0,
    }
    # 50 converted / 200 leads = 25%
    assert m["leads"]["lead_conversion_rate_pct"] == 25.0
    assert m["leads"]["discovery_booked_count"] == 18
    assert m["leads"]["by_funnel_stage"]["(none)"] == 20
    assert m["accounts"] == {"account_count": 40, "total_arr": 4200000.0}
    assert m["contacts"] == {"contact_count": 350}


def test_aggregate_handles_zero_denominators():
    cur = MagicMock()
    cur.fetchone.side_effect = [
        (0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),  # no opps, no closed -> no div by zero
        (0, 0, 0, 0, 0, 0),  # no leads
        (0, 0),
        (0,),
    ]
    cur.fetchall.side_effect = [[], []]
    m = db_adapter._aggregate_snapshot_metrics(cur, 1, date(2026, 6, 16))
    assert m["pipeline"]["lifetime_win_rate_pct"] == 0.0
    assert m["leads"]["lead_conversion_rate_pct"] == 0.0


# --- compute_and_store_daily_metrics: upsert + stamp ----------------------


def test_compute_and_store_daily_metrics_upserts_and_stamps():
    conn, cursor = _mock_conn_and_cursor()
    cursor.fetchone.side_effect = [
        (date(2026, 6, 16),),  # SELECT snapshot_date
        (5, 100000, 20000, 50000, 8, 1, 0, 0, 0, 3, 4),  # opps
        (90, 2, 30, 40, 20, 9),  # leads
        (12, 1000000),  # accounts
        (75,),  # contacts
    ]
    cursor.fetchall.side_effect = [
        [("Discovery", 5, 100000)],
        [("MQL", 40)],
    ]
    with (
        patch.object(db_adapter, "DATABASE_URL", "postgres://test"),
        patch.object(db_adapter, "_connect", return_value=conn),
    ):
        ok = db_adapter.compute_and_store_daily_metrics(42, "acme")

    assert ok is True
    conn.commit.assert_called_once()
    sqls = [c[0][0] for c in cursor.execute.call_args_list]
    assert any("INSERT INTO daily_metrics" in s for s in sqls)
    assert any("UPDATE snapshots SET metrics_rolled_up_at" in s for s in sqls)
    # the metrics JSON payload round-trips and carries the rollup
    insert_call = next(
        c for c in cursor.execute.call_args_list if "INSERT INTO daily_metrics" in c[0][0]
    )
    payload = json.loads(insert_call[0][1][3])
    assert payload["pipeline"]["open_opp_count"] == 5
    assert payload["accounts"]["account_count"] == 12


def test_compute_metrics_no_db_returns_false():
    with (
        patch.object(db_adapter, "DATABASE_URL", ""),
        patch.object(db_adapter, "_connect") as mock_connect,
    ):
        assert db_adapter.compute_and_store_daily_metrics(1, "acme") is False
    mock_connect.assert_not_called()


# --- purge_raw_rows_older_than: archive-gated hot-window sweep -------------


def test_purge_gates_on_rollup_and_archive_when_required():
    conn, cursor = _mock_conn_and_cursor()
    cursor.fetchall.return_value = [(11,), (12,)]
    with (
        patch.object(db_adapter, "DATABASE_URL", "postgres://test"),
        patch.object(db_adapter, "_connect", return_value=conn),
    ):
        n = db_adapter.purge_raw_rows_older_than(days=60, archive_required=True)

    assert n == 2
    select_sql, select_params = cursor.execute.call_args_list[0][0]
    # eligibility predicates present
    assert "metrics_rolled_up_at IS NOT NULL" in select_sql
    assert "raw_purged_at IS NULL" in select_sql
    assert "archived_at IS NOT NULL" in select_sql
    assert select_params[0] == "60"
    assert select_params[1] is True
    # one DELETE per child table + the raw_purged_at stamp
    deletes = [c[0][0] for c in cursor.execute.call_args_list if "DELETE FROM" in c[0][0]]
    assert len(deletes) == 4
    assert any("UPDATE snapshots SET raw_purged_at" in c[0][0] for c in cursor.execute.call_args_list)
    conn.commit.assert_called_once()


def test_purge_archive_not_required_passes_false():
    conn, cursor = _mock_conn_and_cursor()
    cursor.fetchall.return_value = []  # nothing eligible
    with (
        patch.object(db_adapter, "DATABASE_URL", "postgres://test"),
        patch.object(db_adapter, "_connect", return_value=conn),
    ):
        n = db_adapter.purge_raw_rows_older_than(days=60, archive_required=False)
    assert n == 0
    _, params = cursor.execute.call_args_list[0][0]
    assert params[1] is False
    # nothing eligible -> no DELETE issued
    assert not any("DELETE FROM" in c[0][0] for c in cursor.execute.call_args_list)


def test_purge_no_db_returns_zero():
    with (
        patch.object(db_adapter, "DATABASE_URL", ""),
        patch.object(db_adapter, "_connect") as mock_connect,
    ):
        assert db_adapter.purge_raw_rows_older_than() == 0
    mock_connect.assert_not_called()


def test_fetch_snapshot_rows_rejects_non_whitelisted_table():
    with patch.object(db_adapter, "DATABASE_URL", "postgres://test"):
        assert db_adapter.fetch_snapshot_rows(1, "snapshots") == []
        assert db_adapter.fetch_snapshot_rows(1, "investigations; DROP") == []
