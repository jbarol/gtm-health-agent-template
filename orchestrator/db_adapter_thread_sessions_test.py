"""Tests for thread_sessions DB helpers (Task #7, migration 00AJ).

Locks in the composite-PK contract on ``(channel_id, thread_ts)``. All
three helpers — ``save_thread_session``, ``get_thread_session``,
``delete_thread_session`` — must include channel_id in the SQL and the
parameter tuple. Without channel scope two portco-bound Slack channels
sharing a ``thread_ts`` value would cross-pollinate sessions.

Mocks ``db_adapter._connect`` exactly like ``session_cost_persist_test.py``
so no live DB connection is needed.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def _mock_conn_and_cursor():
    """Build a MagicMock pair that satisfies the ``with conn.cursor() as cur:``
    context-manager pattern used throughout db_adapter.
    """
    cursor = MagicMock()
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn, cursor


# ───────────────────────────────────────────────────────────────────────
# save_thread_session
# ───────────────────────────────────────────────────────────────────────


def test_save_thread_session_writes_composite_key_columns():
    """The INSERT must list channel_id, thread_ts, session_id, portco_key,
    config_version — in that order — and the conflict target must match
    the composite PK. ``config_version`` (Plan #44 PR 8) lets the reuse
    path invalidate stale sessions after a prompt deploy.
    """
    import db_adapter

    conn, cursor = _mock_conn_and_cursor()
    with (
        patch.object(db_adapter, "DATABASE_URL", "postgres://test"),
        patch.object(db_adapter, "_connect", return_value=conn),
        patch.object(
            db_adapter, "current_config_version", return_value="deadbeefcafe1234"
        ),
    ):
        db_adapter.save_thread_session(
            "1737654321.000100",
            "sesn_EXAMPLE",
            portco_key="acme",
            channel_id="C0000000000",
        )

    # First execute = INSERT; second = TTL sweep DELETE.
    assert cursor.execute.call_count == 2
    insert_sql, insert_params = cursor.execute.call_args_list[0][0]
    assert "INSERT INTO thread_sessions" in insert_sql
    assert (
        "(channel_id, thread_ts, session_id, portco_key, config_version)" in insert_sql
    )
    assert "ON CONFLICT (channel_id, thread_ts)" in insert_sql
    # Parameter order must match the column list verbatim — psycopg2 binds
    # positionally, so a swap here corrupts every row.
    assert insert_params == (
        "C0000000000",
        "1737654321.000100",
        "sesn_EXAMPLE",
        "acme",
        "deadbeefcafe1234",
    )
    conn.commit.assert_called_once()


def test_save_thread_session_missing_channel_is_noop():
    """Without channel_id the helper must skip the write — two portcos
    sharing a thread_ts value would otherwise collide on the next lookup.
    """
    import db_adapter

    with (
        patch.object(db_adapter, "DATABASE_URL", "postgres://test"),
        patch.object(db_adapter, "_connect") as mock_connect,
    ):
        db_adapter.save_thread_session(
            "1737654321.000100",
            "sesn_EXAMPLE",
            portco_key="acme",
            channel_id=None,
        )
    mock_connect.assert_not_called()


def test_save_thread_session_no_database_url_is_silent():
    import db_adapter

    with (
        patch.object(db_adapter, "DATABASE_URL", ""),
        patch.object(db_adapter, "_connect") as mock_connect,
    ):
        db_adapter.save_thread_session(
            "1737654321.000100",
            "sesn_EXAMPLE",
            portco_key="acme",
            channel_id="C001",
        )
    mock_connect.assert_not_called()


# ───────────────────────────────────────────────────────────────────────
# get_thread_session
# ───────────────────────────────────────────────────────────────────────


def test_get_thread_session_filters_by_channel_and_thread():
    """The lookup must filter on both columns and bind channel_id first
    (matching the parameter order in save_thread_session).
    """
    import db_adapter

    conn, cursor = _mock_conn_and_cursor()
    cursor.fetchone.return_value = ("sesn_EXAMPLE",)
    with (
        patch.object(db_adapter, "DATABASE_URL", "postgres://test"),
        patch.object(db_adapter, "_connect", return_value=conn),
    ):
        result = db_adapter.get_thread_session("1737654321.000100", "C0000000000")

    assert result == "sesn_EXAMPLE"
    sql, params = cursor.execute.call_args[0]
    assert "WHERE channel_id = %s AND thread_ts = %s" in sql
    assert "RETURNING session_id" in sql
    # Order: channel_id first, thread_ts second — matches the WHERE clause.
    assert params == ("C0000000000", "1737654321.000100")
    conn.commit.assert_called_once()


def test_get_thread_session_miss_returns_none():
    """Composite-key miss returns None, not a crash."""
    import db_adapter

    conn, cursor = _mock_conn_and_cursor()
    cursor.fetchone.return_value = None
    with (
        patch.object(db_adapter, "DATABASE_URL", "postgres://test"),
        patch.object(db_adapter, "_connect", return_value=conn),
    ):
        result = db_adapter.get_thread_session("1737654321.000100", "C0000000000")
    assert result is None


def test_get_thread_session_missing_channel_returns_none():
    """Without channel_id, return None instead of letting a bare thread_ts
    lookup pull the wrong portco's session. Defensive double-belt — the
    new schema's PK already enforces this server-side.
    """
    import db_adapter

    with (
        patch.object(db_adapter, "DATABASE_URL", "postgres://test"),
        patch.object(db_adapter, "_connect") as mock_connect,
    ):
        result = db_adapter.get_thread_session("1737654321.000100", None)
    assert result is None
    mock_connect.assert_not_called()


# ───────────────────────────────────────────────────────────────────────
# delete_thread_session
# ───────────────────────────────────────────────────────────────────────


def test_delete_thread_session_filters_by_channel_and_thread():
    """Delete is scoped to the composite key. A bare thread_ts delete would
    wipe an unrelated portco's row on the next multi-portco rollout.
    """
    import db_adapter

    conn, cursor = _mock_conn_and_cursor()
    with (
        patch.object(db_adapter, "DATABASE_URL", "postgres://test"),
        patch.object(db_adapter, "_connect", return_value=conn),
    ):
        db_adapter.delete_thread_session("1737654321.000100", "C0000000000")

    sql, params = cursor.execute.call_args[0]
    assert "DELETE FROM thread_sessions" in sql
    assert "WHERE channel_id = %s AND thread_ts = %s" in sql
    assert params == ("C0000000000", "1737654321.000100")
    conn.commit.assert_called_once()


def test_delete_thread_session_missing_channel_is_noop():
    import db_adapter

    with (
        patch.object(db_adapter, "DATABASE_URL", "postgres://test"),
        patch.object(db_adapter, "_connect") as mock_connect,
    ):
        db_adapter.delete_thread_session("1737654321.000100", None)
    mock_connect.assert_not_called()


def test_delete_thread_session_missing_thread_is_noop():
    import db_adapter

    with (
        patch.object(db_adapter, "DATABASE_URL", "postgres://test"),
        patch.object(db_adapter, "_connect") as mock_connect,
    ):
        db_adapter.delete_thread_session(None, "C0000000000")
    mock_connect.assert_not_called()


def test_save_thread_session_swallows_db_exception():
    """Errors here are best-effort — never break the calling session."""
    import db_adapter

    with (
        patch.object(db_adapter, "DATABASE_URL", "postgres://test"),
        patch.object(db_adapter, "_connect", side_effect=Exception("boom")),
    ):
        # Must NOT raise.
        db_adapter.save_thread_session(
            "1737654321.000100",
            "sesn_EXAMPLE",
            portco_key="acme",
            channel_id="C001",
        )


def test_get_thread_session_swallows_db_exception():
    import db_adapter

    with (
        patch.object(db_adapter, "DATABASE_URL", "postgres://test"),
        patch.object(db_adapter, "_connect", side_effect=Exception("boom")),
    ):
        result = db_adapter.get_thread_session("1737654321.000100", "C001")
    assert result is None


def test_delete_thread_session_swallows_db_exception():
    import db_adapter

    with (
        patch.object(db_adapter, "DATABASE_URL", "postgres://test"),
        patch.object(db_adapter, "_connect", side_effect=Exception("boom")),
    ):
        # Must NOT raise.
        db_adapter.delete_thread_session("1737654321.000100", "C001")


# ───────────────────────────────────────────────────────────────────────
# delete_thread_session_by_session_id (codex P2 #3 fallback)
# ───────────────────────────────────────────────────────────────────────


def test_delete_by_session_id_filters_on_session_column():
    """The recovery-time fallback for legacy NULL-channel rows. Sweeps the
    bloated session_id out of thread_sessions even when channel_id is
    unknown so the next Slack follow-up cannot restore the dead session.
    """
    import db_adapter

    conn, cursor = _mock_conn_and_cursor()
    cursor.rowcount = 1
    with (
        patch.object(db_adapter, "DATABASE_URL", "postgres://test"),
        patch.object(db_adapter, "_connect", return_value=conn),
    ):
        result = db_adapter.delete_thread_session_by_session_id("sesn_EXAMPLE")

    assert result == 1
    sql, params = cursor.execute.call_args[0]
    assert "DELETE FROM thread_sessions" in sql
    assert "WHERE session_id = %s" in sql
    assert params == ("sesn_EXAMPLE",)
    conn.commit.assert_called_once()


def test_delete_by_session_id_returns_rowcount():
    import db_adapter

    conn, cursor = _mock_conn_and_cursor()
    cursor.rowcount = 3
    with (
        patch.object(db_adapter, "DATABASE_URL", "postgres://test"),
        patch.object(db_adapter, "_connect", return_value=conn),
    ):
        assert db_adapter.delete_thread_session_by_session_id("s") == 3


def test_delete_by_session_id_missing_arg_is_zero():
    import db_adapter

    with (
        patch.object(db_adapter, "DATABASE_URL", "postgres://test"),
        patch.object(db_adapter, "_connect") as mock_connect,
    ):
        assert db_adapter.delete_thread_session_by_session_id("") == 0
        assert db_adapter.delete_thread_session_by_session_id(None) == 0
    mock_connect.assert_not_called()


def test_delete_by_session_id_no_database_url_is_zero():
    import db_adapter

    with (
        patch.object(db_adapter, "DATABASE_URL", ""),
        patch.object(db_adapter, "_connect") as mock_connect,
    ):
        assert db_adapter.delete_thread_session_by_session_id("sesn_EXAMPLE") == 0
    mock_connect.assert_not_called()


def test_delete_by_session_id_swallows_db_exception():
    import db_adapter

    with (
        patch.object(db_adapter, "DATABASE_URL", "postgres://test"),
        patch.object(db_adapter, "_connect", side_effect=Exception("boom")),
    ):
        assert db_adapter.delete_thread_session_by_session_id("sesn_EXAMPLE") == 0


# ───────────────────────────────────────────────────────────────────────
# current_config_version + config_version stamping (Plan #44 PR 8)
# ───────────────────────────────────────────────────────────────────────


def test_current_config_version_is_16char_sha256_prefix(tmp_path, monkeypatch):
    """The stamp is the first 16 hex chars of sha256(active_versions.json).

    Verifies the contract: changing the file changes the stamp; the
    return is exactly 16 hex characters; the second call returns the
    same value (process-scoped cache).
    """
    import hashlib

    import db_adapter

    # Build a fake repo layout so db_adapter resolves the right pin file:
    #   <tmp>/orchestrator/db_adapter.py  (__file__)
    #   <tmp>/agents/active_versions.json
    fake_orchestrator = tmp_path / "orchestrator"
    fake_orchestrator.mkdir()
    fake_agents = tmp_path / "agents"
    fake_agents.mkdir()
    pin_path = fake_agents / "active_versions.json"
    pin_path.write_text('{"coordinator": 55, "writing_agent": 4}')

    db_adapter._reset_config_version_cache_for_tests()
    monkeypatch.setattr(
        db_adapter, "__file__", str(fake_orchestrator / "db_adapter.py")
    )
    first = db_adapter.current_config_version()
    expected = hashlib.sha256(pin_path.read_bytes()).hexdigest()[:16]
    assert first == expected
    assert len(first) == 16
    assert all(c in "0123456789abcdef" for c in first)
    # Cached — second call is identical even if the file changes underneath
    # (the cache is intentionally process-scoped; deploy starts a fresh
    # container).
    pin_path.write_text('{"coordinator": 56}')
    second = db_adapter.current_config_version()
    assert second == first
    db_adapter._reset_config_version_cache_for_tests()


def test_current_config_version_returns_none_when_file_missing(tmp_path, monkeypatch):
    """A missing pin file must not raise — return None and let the reuse
    path treat it as ``stale``.
    """
    import db_adapter

    fake_orchestrator = tmp_path / "orchestrator"
    fake_orchestrator.mkdir()
    db_adapter._reset_config_version_cache_for_tests()
    monkeypatch.setattr(
        db_adapter, "__file__", str(fake_orchestrator / "db_adapter.py")
    )
    assert db_adapter.current_config_version() is None
    db_adapter._reset_config_version_cache_for_tests()


def test_save_thread_session_stamps_config_version(monkeypatch):
    """Every INSERT must bind the current config_version into the row.

    The orchestrator compares this stamp on reuse to invalidate sessions
    after a prompt deploy. Without it the column would always be NULL
    and the reuse check would silently rotate every cached session on
    every Slack message.
    """
    import db_adapter

    conn, cursor = _mock_conn_and_cursor()
    with (
        patch.object(db_adapter, "DATABASE_URL", "postgres://test"),
        patch.object(db_adapter, "_connect", return_value=conn),
        patch.object(
            db_adapter, "current_config_version", return_value="abc123def456beef"
        ),
    ):
        db_adapter.save_thread_session(
            "1737654321.000100",
            "sesn_EXAMPLE",
            portco_key="acme",
            channel_id="C0000000000",
        )

    insert_sql, insert_params = cursor.execute.call_args_list[0][0]
    assert "config_version" in insert_sql
    assert "ON CONFLICT (channel_id, thread_ts)" in insert_sql
    # The five-tuple matches the column order in the INSERT.
    assert insert_params == (
        "C0000000000",
        "1737654321.000100",
        "sesn_EXAMPLE",
        "acme",
        "abc123def456beef",
    )


def test_save_thread_session_handles_none_config_version(monkeypatch):
    """When the pin file is missing (current_config_version returns None)
    the INSERT still goes through — the NULL stamp will fail the reuse
    check on the next lookup, which is the correct fail-closed behavior.
    """
    import db_adapter

    conn, cursor = _mock_conn_and_cursor()
    with (
        patch.object(db_adapter, "DATABASE_URL", "postgres://test"),
        patch.object(db_adapter, "_connect", return_value=conn),
        patch.object(db_adapter, "current_config_version", return_value=None),
    ):
        db_adapter.save_thread_session(
            "1737654321.000100",
            "sesn_EXAMPLE",
            portco_key="acme",
            channel_id="C0000000000",
        )

    insert_sql, insert_params = cursor.execute.call_args_list[0][0]
    assert "config_version" in insert_sql
    assert insert_params[-1] is None


def test_get_thread_session_record_returns_session_and_version():
    """The richer lookup returns ``(session_id, config_version)`` so the
    reuse-or-rotate decision in session_runner can compare stamps
    without a second round-trip.
    """
    import db_adapter

    conn, cursor = _mock_conn_and_cursor()
    cursor.fetchone.return_value = ("sesn_EXAMPLE", "feedfacecafebabe")
    with (
        patch.object(db_adapter, "DATABASE_URL", "postgres://test"),
        patch.object(db_adapter, "_connect", return_value=conn),
    ):
        result = db_adapter.get_thread_session_record(
            "1737654321.000100", "C0000000000"
        )

    assert result == ("sesn_EXAMPLE", "feedfacecafebabe")
    sql, params = cursor.execute.call_args[0]
    assert "RETURNING session_id, config_version" in sql
    assert params == ("C0000000000", "1737654321.000100")


def test_get_thread_session_record_handles_pre_pr8_row_without_version():
    """Pre-PR8 rows carry NULL config_version. The helper must still
    return a tuple — the reuse path treats NULL as ``stale, force fresh``.
    """
    import db_adapter

    conn, cursor = _mock_conn_and_cursor()
    cursor.fetchone.return_value = ("sesn_EXAMPLE", None)
    with (
        patch.object(db_adapter, "DATABASE_URL", "postgres://test"),
        patch.object(db_adapter, "_connect", return_value=conn),
    ):
        result = db_adapter.get_thread_session_record(
            "1737654321.000100", "C0000000000"
        )

    assert result == ("sesn_EXAMPLE", None)


def test_get_thread_session_record_miss_returns_none():
    import db_adapter

    conn, cursor = _mock_conn_and_cursor()
    cursor.fetchone.return_value = None
    with (
        patch.object(db_adapter, "DATABASE_URL", "postgres://test"),
        patch.object(db_adapter, "_connect", return_value=conn),
    ):
        result = db_adapter.get_thread_session_record(
            "1737654321.000100", "C0000000000"
        )
    assert result is None


def test_get_thread_session_record_missing_channel_returns_none():
    import db_adapter

    with (
        patch.object(db_adapter, "DATABASE_URL", "postgres://test"),
        patch.object(db_adapter, "_connect") as mock_connect,
    ):
        assert db_adapter.get_thread_session_record("1737654321.000100", None) is None
    mock_connect.assert_not_called()


def test_get_thread_session_back_compat_returns_session_only():
    """The original single-value helper still returns the session id (the
    old contract). Existing callers in production read only session_id;
    the richer record lookup is opt-in.
    """
    import db_adapter

    conn, cursor = _mock_conn_and_cursor()
    cursor.fetchone.return_value = ("sesn_EXAMPLE", "stampabc")
    with (
        patch.object(db_adapter, "DATABASE_URL", "postgres://test"),
        patch.object(db_adapter, "_connect", return_value=conn),
    ):
        result = db_adapter.get_thread_session("1737654321.000100", "C0000000000")
    assert result == "sesn_EXAMPLE"


# ───────────────────────────────────────────────────────────────────────
# get_interrupted_investigations — queued-orphan recovery (Theme A,
# 2026-05-16). Pre-extension the WHERE only matched status='running', so
# rows queued by a dying container (e.g. inv 32 on 2026-05-14) sat in
# 'queued' forever. The fix extends the WHERE to also catch 'queued' rows
# older than 15 minutes with a stale container_id.
# ───────────────────────────────────────────────────────────────────────


import pytest


@pytest.fixture
def stub_psycopg2():
    """db_adapter does ``import psycopg2.extras`` inside its query helpers.
    On dev machines without psycopg2 installed in the pytest interpreter,
    the import raises and the function silently returns []. This fixture
    inserts a minimal stub so the import succeeds and the function reaches
    its SQL execute. The cursor_factory kwarg is then passed to the mocked
    conn.cursor which ignores it.
    """
    import sys

    saved = {
        "psycopg2": sys.modules.get("psycopg2"),
        "psycopg2.extras": sys.modules.get("psycopg2.extras"),
    }
    if "psycopg2" not in sys.modules:
        sys.modules["psycopg2"] = MagicMock(name="psycopg2_stub")
    if "psycopg2.extras" not in sys.modules:
        extras_stub = MagicMock(name="psycopg2.extras_stub")
        extras_stub.RealDictCursor = MagicMock(name="RealDictCursor")
        sys.modules["psycopg2.extras"] = extras_stub
    yield
    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v


def test_get_interrupted_includes_running_and_queued_orphans(stub_psycopg2):
    """The SQL must update BOTH 'running' AND 'queued' rows whose
    container_id is stale, with a 15-minute floor on queued to protect
    live containers' newly-queued work.
    """
    import db_adapter

    conn, cursor = _mock_conn_and_cursor()
    cursor.fetchall.return_value = []
    with (
        patch.object(db_adapter, "DATABASE_URL", "postgres://test"),
        patch.object(db_adapter, "_connect", return_value=conn),
    ):
        db_adapter.get_interrupted_investigations("container-current")
        sql, params = cursor.execute.call_args[0]
    # Both status types listed
    assert "status = 'running'" in sql
    assert "status = 'queued'" in sql
    # Container scope: stale-or-null
    assert "container_id IS NULL OR container_id != %s" in sql
    # Queued has a TTL floor — live container's just-queued work is protected
    assert "INTERVAL '15 minutes'" in sql
    # Update target: row marked 'interrupted' so the recovery loop can resume
    assert "UPDATE investigations SET status = 'interrupted'" in sql
    # Container id is bound positionally exactly once (the only parameter)
    assert params == ("container-current",)


def test_get_interrupted_returns_queued_orphan_rows(stub_psycopg2):
    """When the DB returns a queued row with session_id=NULL, the helper
    surfaces it unchanged so the recovery loop can fresh-start it.
    """
    import db_adapter

    conn, cursor = _mock_conn_and_cursor()
    cursor.fetchall.return_value = [
        {
            "id": 32,
            "question": "Give me a breakdown of customers by product",
            "thread_ts": "1778720619.678229",
            "channel_id": "C0000000000",
            "user_id": "U_TEST",
            "portco_key": "acme",
            "session_id": None,  # Queued — never started a session
            "agent_id": None,
            "recovery_count": 0,
            "started_at": None,
            "event_ts": "1778720619.678229",
        }
    ]
    with (
        patch.object(db_adapter, "DATABASE_URL", "postgres://test"),
        patch.object(db_adapter, "_connect", return_value=conn),
    ):
        rows = db_adapter.get_interrupted_investigations("container-current")

    assert len(rows) == 1
    assert rows[0]["id"] == 32
    assert rows[0]["session_id"] is None  # Recovery loop must handle this case
    conn.commit.assert_called_once()


def test_get_interrupted_empty_container_id_still_matches(stub_psycopg2):
    """An empty current_container_id (no Railway deployment id env) must
    still hit the WHERE — rows with container_id IS NULL match, and rows
    with any non-empty container_id != '' also match. Defensive: prevents
    the recovery sweep silently doing nothing on a misconfigured container.
    """
    import db_adapter

    conn, cursor = _mock_conn_and_cursor()
    cursor.fetchall.return_value = []
    with (
        patch.object(db_adapter, "DATABASE_URL", "postgres://test"),
        patch.object(db_adapter, "_connect", return_value=conn),
    ):
        db_adapter.get_interrupted_investigations(None)

    _, params = cursor.execute.call_args[0]
    assert params == ("",)


def test_get_interrupted_no_database_url_is_empty():
    import db_adapter

    with (
        patch.object(db_adapter, "DATABASE_URL", ""),
        patch.object(db_adapter, "_connect") as mock_connect,
    ):
        rows = db_adapter.get_interrupted_investigations("c1")
    assert rows == []
    mock_connect.assert_not_called()


def test_get_interrupted_swallows_db_exception(stub_psycopg2):
    import db_adapter

    with (
        patch.object(db_adapter, "DATABASE_URL", "postgres://test"),
        patch.object(db_adapter, "_connect", side_effect=Exception("boom")),
    ):
        rows = db_adapter.get_interrupted_investigations("c1")
    assert rows == []
