"""Tests for ``orchestrator/watcher_pending_db.py``.

Fully mocked — no real Postgres. The accessor surface is small and
deterministic: enqueue, claim, mark, catch_up_sweep,
count_unmerged_unreviewed_24h.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "orchestrator"))


# Bootstrap env vars so any sibling import that touches config.py at
# module load (none here, but safe in this repo) does not raise.
for _key, _value in {
    "DATABASE_URL": "postgres://test/db",
}.items():
    os.environ.setdefault(_key, _value)


import watcher_pending_db as wpd  # noqa: E402


def _mock_cursor(fetchone=None, fetchall=None, description=None):
    cur = MagicMock()
    if fetchone is not None:
        cur.fetchone.return_value = fetchone
    if fetchall is not None:
        cur.fetchall.return_value = fetchall
    if description is not None:
        cur.description = description
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=None)
    return cur


def _mock_conn(cursors: list[MagicMock]) -> MagicMock:
    conn = MagicMock()
    conn.cursor.side_effect = cursors
    conn.closed = False
    return conn


# ───────────────────────────────────────────────────────────────────────
# enqueue_watcher_pending
# ───────────────────────────────────────────────────────────────────────


def test_enqueue_returns_inserted_id():
    cur = _mock_cursor(fetchone=(42,))
    conn = _mock_conn([cur])
    with patch.object(wpd, "_connect", return_value=conn):
        result = wpd.enqueue_watcher_pending(
            inv_id=100,
            channel_id="C123",
            thread_ts="1716315045.000100",
            error_category="schema_validation_failure",
            error_message_hash="abc123def456",
        )
    assert result == 42
    conn.commit.assert_called_once()
    conn.close.assert_called_once()


def test_enqueue_returns_existing_id_on_conflict():
    """The RETURNING clause yields the existing row id on conflict (ON CONFLICT
    DO UPDATE clause specifies repeat_count bump)."""
    cur = _mock_cursor(fetchone=(7,))
    conn = _mock_conn([cur])
    with patch.object(wpd, "_connect", return_value=conn):
        result = wpd.enqueue_watcher_pending(
            inv_id=200,
            channel_id="C123",
            thread_ts="1716315050.000200",
            error_category=None,
            error_message_hash="dup_hash",
        )
    assert result == 7


def test_enqueue_returns_none_when_no_database_url():
    with patch.dict(os.environ, {"DATABASE_URL": ""}, clear=False):
        result = wpd.enqueue_watcher_pending(
            inv_id=1, channel_id=None, thread_ts=None,
            error_category=None, error_message_hash="x",
        )
    assert result is None


def test_enqueue_populates_source_inv_ids():
    """ON CONFLICT path appends inv_id into source_inv_ids array."""
    cur = _mock_cursor(fetchone=(1,))
    conn = _mock_conn([cur])
    with patch.object(wpd, "_connect", return_value=conn):
        wpd.enqueue_watcher_pending(
            inv_id=42, channel_id=None, thread_ts=None,
            error_category=None, error_message_hash="h",
        )
    _sql, _params = cur.execute.call_args.args
    # SQL must reference source_inv_ids array op in BOTH the INSERT and
    # the ON CONFLICT clause
    assert "source_inv_ids" in _sql
    assert "array_append" in _sql or "ARRAY[" in _sql


def test_enqueue_passes_catch_up_flag():
    cur = _mock_cursor(fetchone=(1,))
    conn = _mock_conn([cur])
    with patch.object(wpd, "_connect", return_value=conn):
        wpd.enqueue_watcher_pending(
            inv_id=1, channel_id=None, thread_ts=None,
            error_category=None, error_message_hash="h",
            catch_up=True,
        )
    args, _ = cur.execute.call_args
    _sql, params = args
    # catch_up sits between hash and the trailing source_inv_ids params;
    # The catch_up boolean is positionally after the hash and before the
    # two CASE-clause inv_ids — index 5 in the param tuple.
    # Param order: (inv_id, channel_id, thread_ts, error_category,
    #               error_message_hash, catch_up, inv_id_for_case, inv_id_for_array)
    assert params[5] is True


# ───────────────────────────────────────────────────────────────────────
# claim_watcher_pending
# ───────────────────────────────────────────────────────────────────────


def test_claim_returns_empty_on_no_db():
    with patch.dict(os.environ, {"DATABASE_URL": ""}, clear=False):
        assert wpd.claim_watcher_pending(limit=5) == []


def test_claim_returns_empty_on_limit_zero():
    assert wpd.claim_watcher_pending(limit=0) == []


def test_claim_includes_transient_skipped_after_backoff():
    """transient_skipped is documented as requeue-after-backoff. Verify it
    appears in the SELECT eligibility predicate."""
    cur = _mock_cursor(fetchall=[], description=[("id",)])
    conn = _mock_conn([cur])
    with patch.object(wpd, "_connect", return_value=conn):
        wpd.claim_watcher_pending(limit=1)
    _sql, _params = cur.execute.call_args.args
    assert "transient_skipped" in _sql
    assert "failed_retry" in _sql


def test_claim_returns_dict_rows():
    description = [
        ("id",), ("inv_id",), ("channel_id",), ("thread_ts",),
        ("error_category",), ("error_message_hash",),
        ("repeat_count",), ("attempts",), ("catch_up",),
        ("first_seen_at",), ("created_at",),
    ]
    rows = [
        (1, 100, "C1", "ts1", "cat_a", "hash_a", 1, 1, False,
         datetime.now(timezone.utc), datetime.now(timezone.utc)),
        (2, 101, "C2", "ts2", "cat_b", "hash_b", 3, 2, False,
         datetime.now(timezone.utc), datetime.now(timezone.utc)),
    ]
    cur = _mock_cursor(fetchall=rows, description=description)
    conn = _mock_conn([cur])
    with patch.object(wpd, "_connect", return_value=conn):
        out = wpd.claim_watcher_pending(limit=10)
    assert len(out) == 2
    assert out[0]["id"] == 1
    assert out[0]["error_category"] == "cat_a"
    assert out[1]["attempts"] == 2


# ───────────────────────────────────────────────────────────────────────
# mark_watcher_pending
# ───────────────────────────────────────────────────────────────────────


def test_mark_rejects_unknown_status():
    with pytest.raises(ValueError, match="invalid status"):
        wpd.mark_watcher_pending(1, status="not_a_real_status")


@pytest.mark.parametrize(
    "status",
    [
        wpd.STATUS_PENDING,
        wpd.STATUS_RUNNING,
        wpd.STATUS_COMPLETED,
        wpd.STATUS_TRANSIENT_SKIPPED,
        wpd.STATUS_FAILED_RETRY,
        wpd.STATUS_ABANDONED,
        wpd.STATUS_DIAGNOSE_ONLY,
    ],
)
def test_mark_accepts_all_documented_statuses(status):
    cur = _mock_cursor()
    conn = _mock_conn([cur])
    with patch.object(wpd, "_connect", return_value=conn):
        wpd.mark_watcher_pending(99, status=status)
    args, _ = cur.execute.call_args
    _sql, params = args
    assert params[0] == status


def test_mark_with_error_category_uses_two_param_update():
    cur = _mock_cursor()
    conn = _mock_conn([cur])
    with patch.object(wpd, "_connect", return_value=conn):
        wpd.mark_watcher_pending(
            7, status=wpd.STATUS_COMPLETED, error_category="schema_validation_failure"
        )
    args, _ = cur.execute.call_args
    sql, params = args
    assert "error_category" in sql
    assert params == (wpd.STATUS_COMPLETED, "schema_validation_failure", 7)


def test_mark_no_op_without_database_url():
    with patch.dict(os.environ, {"DATABASE_URL": ""}, clear=False):
        # Should not raise, should not connect
        with patch.object(wpd, "_connect") as fake_connect:
            wpd.mark_watcher_pending(1, status=wpd.STATUS_COMPLETED)
            fake_connect.assert_not_called()


# ───────────────────────────────────────────────────────────────────────
# catch_up_sweep
# ───────────────────────────────────────────────────────────────────────


def test_catch_up_sweep_enqueues_missing_rows():
    """Two candidates returned; both get enqueued (no existing pending row)."""
    candidates = [
        (101, "C1", "ts1", "TypeError: foo at inv_id=101"),
        (102, "C2", "ts2", "ConnectionError: dropped at inv_id=102"),
    ]
    select_cur = _mock_cursor(fetchall=candidates)
    # Two enqueues, each gets its own cursor
    enqueue_cur_1 = _mock_cursor(fetchone=(201,))
    enqueue_cur_2 = _mock_cursor(fetchone=(202,))
    select_conn = _mock_conn([select_cur])
    enqueue_conn_1 = _mock_conn([enqueue_cur_1])
    enqueue_conn_2 = _mock_conn([enqueue_cur_2])

    # _connect is called once for the SELECT and once per enqueue
    connect_sequence = [select_conn, enqueue_conn_1, enqueue_conn_2]
    with patch.object(wpd, "_connect", side_effect=connect_sequence):
        out = wpd.catch_up_sweep(since=datetime.now(timezone.utc) - timedelta(minutes=30))
    assert out == [101, 102]


def test_catch_up_sweep_uses_failed_status():
    """Status filter must be 'failed' (DeliveryState.db_status writes), not
    'terminal_failure'/'no_output' (the enum names)."""
    select_cur = _mock_cursor(fetchall=[])
    select_conn = _mock_conn([select_cur])
    with patch.object(wpd, "_connect", return_value=select_conn):
        wpd.catch_up_sweep(since=datetime.now(timezone.utc))
    _sql, _params = select_cur.execute.call_args.args
    assert "i.status = 'failed'" in _sql
    assert "terminal_failure" not in _sql
    assert "no_output" not in _sql


def test_catch_up_sweep_uses_source_inv_ids_for_dedup():
    """Hash-collapsed pending rows must cover ALL source inv_ids, not
    just the first one stored in the legacy inv_id column."""
    select_cur = _mock_cursor(fetchall=[])
    select_conn = _mock_conn([select_cur])
    with patch.object(wpd, "_connect", return_value=select_conn):
        wpd.catch_up_sweep(since=datetime.now(timezone.utc))
    _sql, _params = select_cur.execute.call_args.args
    assert "source_inv_ids" in _sql
    assert "ANY(" in _sql or "ANY (" in _sql


def test_catch_up_sweep_recursion_guard_excludes_watcher_agent():
    """When watcher_agent_id is set, the SELECT must exclude rows where
    investigations.agent_id matches the watcher's own agent ID."""
    select_cur = _mock_cursor(fetchall=[])
    select_conn = _mock_conn([select_cur])
    with patch.object(wpd, "_connect", return_value=select_conn):
        wpd.catch_up_sweep(
            since=datetime.now(timezone.utc),
            watcher_agent_id="agent_WATCHER_XYZ",
        )
    sql, params = select_cur.execute.call_args.args
    assert "agent_id" in sql
    assert "<>" in sql or "!=" in sql
    assert "agent_WATCHER_XYZ" in params


def test_catch_up_sweep_no_guard_when_agent_id_absent():
    """Without watcher_agent_id, no agent_id filter — tests + dev mode."""
    select_cur = _mock_cursor(fetchall=[])
    select_conn = _mock_conn([select_cur])
    with patch.object(wpd, "_connect", return_value=select_conn):
        wpd.catch_up_sweep(since=datetime.now(timezone.utc))
    sql, params = select_cur.execute.call_args.args
    assert "agent_id" not in sql
    assert len(params) == 1  # only ``since``


def test_catch_up_sweep_empty_when_no_db():
    with patch.dict(os.environ, {"DATABASE_URL": ""}, clear=False):
        out = wpd.catch_up_sweep(since=datetime.now(timezone.utc))
    assert out == []


# ───────────────────────────────────────────────────────────────────────
# Kill-switch counter
# ───────────────────────────────────────────────────────────────────────


def test_list_completed_24h_excludes_diagnose_only():
    """diagnose_only outcomes have no PR — must not enter the kill-switch
    denominator."""
    cur = _mock_cursor(fetchall=[], description=[("id",)])
    conn = _mock_conn([cur])
    with patch.object(wpd, "_connect", return_value=conn):
        wpd.list_completed_24h()
    _sql, _params = cur.execute.call_args.args
    assert "diagnose_only" not in _sql
    assert "status = 'completed'" in _sql


def test_count_unmerged_unreviewed_24h_filters_by_callback():
    description = [
        ("id",), ("inv_id",), ("error_message_hash",), ("status",),
        ("attempts",), ("first_seen_at",), ("last_attempt_at",),
        ("updated_at",),
    ]
    now = datetime.now(timezone.utc)
    rows = [
        (1, 100, "h1", "completed", 1, now, now, now),
        (2, 101, "h2", "completed", 1, now, now, now),
        (3, 102, "h3", "diagnose_only", 1, now, now, now),
    ]
    cur = _mock_cursor(fetchall=rows, description=description)
    conn = _mock_conn([cur])

    # Inject: row id 2 was merged/reviewed; rows 1 and 3 are dangling
    def reviewed_or_merged(row):
        return row["id"] == 2

    with patch.object(wpd, "_connect", return_value=conn):
        count = wpd.count_unmerged_unreviewed_24h(reviewed_or_merged)
    assert count == 2


def test_count_unmerged_unreviewed_24h_zero_on_empty():
    cur = _mock_cursor(fetchall=[], description=[("id",)])
    conn = _mock_conn([cur])
    with patch.object(wpd, "_connect", return_value=conn):
        count = wpd.count_unmerged_unreviewed_24h(lambda _row: False)
    assert count == 0


def test_count_unmerged_unreviewed_24h_zero_when_no_db():
    with patch.dict(os.environ, {"DATABASE_URL": ""}, clear=False):
        count = wpd.count_unmerged_unreviewed_24h(lambda _row: False)
    assert count == 0
