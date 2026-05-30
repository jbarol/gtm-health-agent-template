"""Tests for db_adapter._apply_migrations — the auto-migration runner.

Why this matters:
    On 2026-05-14 we discovered every Slack adhoc since 2026-05-13 was
    silently failing to write its investigation row because migration
    00AH (event_ts column on investigations) had never been applied to
    prod. The .sql migration files were a parallel path requiring
    manual `psql` invocation; container restarts didn't pick them up.
    `_apply_migrations` closes that gap by walking the migrations dir
    on every boot and applying anything not yet recorded in
    ``schema_migrations``. The tests below pin the runner's contract:
    apply once, skip if already applied, leave the tracking table
    untouched on failure (so retry on next boot works).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def _mk_conn():
    """Build a mock psycopg2 connection that records every execute call.

    Each ``cur.execute`` returns the mock cursor, so chained ``.fetchone``
    calls work. ``executed`` records (sql, params) tuples in order.
    """
    executed: list = []
    fetch_results: list = []

    cursor = MagicMock()
    # Default: SELECT 1 FROM schema_migrations returns None (not applied).
    # Tests can override by appending to ``fetch_results``.
    cursor.fetchone.side_effect = lambda: (
        fetch_results.pop(0) if fetch_results else None
    )

    def _execute(sql, params=None):
        executed.append((sql, params))
        return cursor

    cursor.execute.side_effect = _execute

    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    conn.cursor.return_value.__exit__.return_value = False

    return conn, cursor, executed, fetch_results


def test_apply_migrations_creates_tracking_table_then_applies_files(tmp_path):
    """First run: schema_migrations created, all .sql files applied + tracked."""
    import db_adapter

    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "00AA_first.sql").write_text("ALTER TABLE foo ADD COLUMN bar TEXT;")
    (migrations / "00AB_second.sql").write_text("CREATE INDEX idx_foo ON foo(bar);")

    conn, cursor, executed, _ = _mk_conn()

    with patch.object(db_adapter, "__file__", str(tmp_path / "db_adapter.py")):
        db_adapter._apply_migrations(conn)

    sqls = [sql for sql, _ in executed]
    joined = "\n".join(sqls)

    # Tracking table created
    assert "CREATE TABLE IF NOT EXISTS schema_migrations" in joined
    # Both migrations executed in sort order
    assert any("ALTER TABLE foo ADD COLUMN bar TEXT" in s for s in sqls)
    assert any("CREATE INDEX idx_foo ON foo(bar)" in s for s in sqls)
    # Both migrations recorded
    insert_calls = [
        params
        for sql, params in executed
        if sql and "INSERT INTO schema_migrations" in sql
    ]
    assert ("00AA_first.sql",) in insert_calls
    assert ("00AB_second.sql",) in insert_calls
    # Commit fired at least once (CREATE table + per-migration body + per-INSERT)
    assert conn.commit.call_count >= 3


def test_apply_migrations_skips_already_applied(tmp_path):
    """If a filename is in schema_migrations, its SQL body must NOT run."""
    import db_adapter

    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "00AA_done.sql").write_text("SELECT 'should not run';")
    (migrations / "00AB_todo.sql").write_text("SELECT 'should run';")

    conn, cursor, executed, fetch_results = _mk_conn()
    # First lookup (00AA_done): returns (1,) — already applied.
    # Second lookup (00AB_todo): returns None — not applied.
    fetch_results.append((1,))
    fetch_results.append(None)

    with patch.object(db_adapter, "__file__", str(tmp_path / "db_adapter.py")):
        db_adapter._apply_migrations(conn)

    sqls = [sql for sql, _ in executed]
    # 00AA body never executed
    assert not any("'should not run'" in s for s in sqls)
    # 00AB body did execute
    assert any("'should run'" in s for s in sqls)
    # Only 00AB recorded
    insert_calls = [
        params
        for sql, params in executed
        if sql and "INSERT INTO schema_migrations" in sql
    ]
    assert insert_calls == [("00AB_todo.sql",)]


def test_apply_migrations_missing_directory_no_op(tmp_path):
    """If orchestrator/migrations doesn't exist, the runner is a no-op."""
    import db_adapter

    conn, cursor, executed, _ = _mk_conn()
    # Point db_adapter.__file__ at a path whose sibling 'migrations' dir
    # does NOT exist — the function should bail before touching the DB.
    fake_root = tmp_path / "nonexistent_pkg"
    fake_root.mkdir()
    with patch.object(db_adapter, "__file__", str(fake_root / "db_adapter.py")):
        db_adapter._apply_migrations(conn)

    # No SQL executed, no commit
    assert executed == []
    conn.commit.assert_not_called()


def test_apply_migrations_failure_does_not_track_so_retry_works(tmp_path):
    """If a migration body raises, schema_migrations INSERT must NOT happen.

    Next boot the runner finds the row missing and re-tries — the only
    correct behavior for a partial-apply path.
    """
    import db_adapter

    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "00AA_broken.sql").write_text("INVALID SQL THAT WILL RAISE;")

    conn, cursor, executed, _ = _mk_conn()

    # Make ONLY the migration-body execute raise.
    def _execute(sql, params=None):
        executed.append((sql, params))
        if "INVALID SQL" in sql:
            raise RuntimeError("simulated migration failure")
        return cursor

    cursor.execute.side_effect = _execute

    raised = False
    try:
        with patch.object(db_adapter, "__file__", str(tmp_path / "db_adapter.py")):
            db_adapter._apply_migrations(conn)
    except RuntimeError as exc:
        raised = True
        assert "simulated migration failure" in str(exc)

    assert raised, "Migration failure should propagate (caller handles it)"
    # No INSERT into schema_migrations happened
    insert_calls = [
        sql for sql, _ in executed if sql and "INSERT INTO schema_migrations" in sql
    ]
    assert insert_calls == []
    # Rollback fired
    conn.rollback.assert_called()


def test_00am_thread_session_config_version_is_idempotent_and_indexed():
    """The 00AM migration body must:

      - ALTER TABLE thread_sessions ADD COLUMN IF NOT EXISTS config_version TEXT
      - CREATE INDEX IF NOT EXISTS idx_thread_sessions_config_version

    ``IF NOT EXISTS`` on both keeps the migration safe to re-run, even
    after the inline ``ensure_schema`` mirror has already created the
    column on a fresh install. Without the index a sequential scan on
    every reuse decision would land on the hottest table in prod.
    """
    from pathlib import Path

    migration_path = (
        Path(__file__).parent / "migrations" / "00AN_thread_session_config_version.sql"
    )
    sql = migration_path.read_text()
    # Column add — idempotent on re-runs of partial state.
    assert "ADD COLUMN IF NOT EXISTS config_version" in sql
    assert "ALTER TABLE thread_sessions" in sql
    # Index — must reference the right column name.
    assert "CREATE INDEX IF NOT EXISTS idx_thread_sessions_config_version" in sql
    assert "ON thread_sessions(config_version)" in sql


def test_00am_migration_applies_cleanly_via_runner(tmp_path):
    """Drive the migration through the real ``_apply_migrations`` runner.

    The 00AM body is small (one ALTER + one CREATE INDEX) so we can let
    the runner execute it against the same mock-cursor shape used in the
    other migration tests. This confirms the runner accepts the SQL and
    records the filename in ``schema_migrations``.
    """
    import shutil
    from pathlib import Path

    import db_adapter

    real_migration = (
        Path(__file__).parent / "migrations" / "00AN_thread_session_config_version.sql"
    )
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    shutil.copy(real_migration, migrations / "00AN_thread_session_config_version.sql")

    conn, cursor, executed, _ = _mk_conn()

    from unittest.mock import patch

    with patch.object(db_adapter, "__file__", str(tmp_path / "db_adapter.py")):
        db_adapter._apply_migrations(conn)

    insert_calls = [
        params
        for sql, params in executed
        if sql and "INSERT INTO schema_migrations" in sql
    ]
    assert ("00AN_thread_session_config_version.sql",) in insert_calls


def test_apply_migrations_runs_files_in_sort_order(tmp_path):
    """Lexicographic order — 00AA before 00AB before 00Y before 00Z.

    This matches our convention: 00AA → 00AH for sequential phase
    migrations, then 00Y / 00Z for one-off late additions that landed
    out of band. Path.glob doesn't guarantee order; sorted() does.
    """
    import db_adapter

    migrations = tmp_path / "migrations"
    migrations.mkdir()
    # Write in non-lexicographic order to confirm sorted() is responsible.
    (migrations / "00Z_last.sql").write_text("SELECT 'z';")
    (migrations / "00AA_first.sql").write_text("SELECT 'a';")
    (migrations / "00AB_second.sql").write_text("SELECT 'b';")
    (migrations / "00Y_third.sql").write_text("SELECT 'y';")

    conn, cursor, executed, _ = _mk_conn()

    with patch.object(db_adapter, "__file__", str(tmp_path / "db_adapter.py")):
        db_adapter._apply_migrations(conn)

    # Extract the order of migration filenames inserted into schema_migrations.
    insert_order = [
        params[0]
        for sql, params in executed
        if sql and "INSERT INTO schema_migrations" in sql
    ]
    assert insert_order == [
        "00AA_first.sql",
        "00AB_second.sql",
        "00Y_third.sql",
        "00Z_last.sql",
    ]
