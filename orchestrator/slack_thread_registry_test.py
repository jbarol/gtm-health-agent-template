"""Tests for slack_thread_registry (Plan #11, Task #22).

Verifies the DB-first ordering: INSERT placeholder → post Slack → UPDATE.
Mocks the Slack poster + db_adapter._connect so no live Slack or DB
calls fire. Mirrors the patching pattern from
``session_cost_persist_test.py`` and ``dispatch_post_report_test.py``.

Run:
    cd orchestrator && python3 -m pytest slack_thread_registry_test.py -v
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_cache():
    """Reset the in-memory cache before every test."""
    from slack_thread_registry import _clear_cache_for_tests

    _clear_cache_for_tests()
    yield
    _clear_cache_for_tests()


@pytest.fixture
def no_db(monkeypatch):
    """Empty DATABASE_URL — registry DB helpers short-circuit.

    All claim/lookup/update calls return their unavailable-mode value
    (False, None, no-op). Tests that exercise the cache-only happy path
    use this fixture; tests that need the DB path patch ``DATABASE_URL``
    truthy AND mock ``_connect``.
    """
    import db_adapter

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "")
    yield


def _capture_poster(return_ts: str = "1234567890.000001"):
    """Build a stub poster that records its calls and returns ``return_ts``."""
    calls: list[dict] = []

    def _poster(**kwargs):
        calls.append(kwargs)
        return return_ts

    return _poster, calls


def _make_db_mock(fetchone_values, rowcount_values=None):
    """Build a (mock_conn, mock_cursor) pair scripted across calls.

    ``fetchone_values`` is a list iterated one per ``cursor.execute`` cycle
    so multi-step flows (claim, lookup, update, delete) can sequence
    return values deterministically. ``rowcount_values`` is the parallel
    list for rowcount-driven branches; default is rowcount=1 each time.
    """
    fetch_iter = iter(fetchone_values)
    rowcount_iter = iter(rowcount_values if rowcount_values is not None else [1] * 64)

    mock_cursor = MagicMock()

    def _fetchone():
        try:
            return next(fetch_iter)
        except StopIteration:
            return None

    def _execute(*args, **kwargs):
        # Pop the next rowcount value per execute so consecutive calls
        # walk through the script in order.
        try:
            mock_cursor.rowcount = next(rowcount_iter)
        except StopIteration:
            mock_cursor.rowcount = 0

    mock_cursor.fetchone.side_effect = _fetchone
    mock_cursor.execute.side_effect = _execute

    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    return mock_conn, mock_cursor


# ---------------------------------------------------------------------------
# Test 1: happy path — claim wins, Slack post succeeds, UPDATE runs
# ---------------------------------------------------------------------------


def test_happy_path_claim_post_update_returns_real_ts(monkeypatch):
    """Win the claim → post Slack → UPDATE → returned ts is the real one."""
    import db_adapter
    from slack_thread_registry import get_or_create_thread

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "postgres://test")

    # Lookup returns None (no existing row); claim INSERT rowcount=1 (won);
    # update + lookup not strictly needed after that.
    mock_conn, mock_cursor = _make_db_mock(
        fetchone_values=[None],
        rowcount_values=[0, 1, 1],  # lookup, claim INSERT, UPDATE
    )

    poster, calls = _capture_poster("ts-happy")
    with patch.object(db_adapter, "_connect", return_value=mock_conn):
        result = get_or_create_thread(
            run_id="nightly-2026-05-14",
            theme="pipeline_review",
            channel_id="C123",
            parent_summary="Pipeline coverage 0.6x — below plan",
            poster=poster,
        )

    assert result == "ts-happy"
    assert len(calls) == 1
    assert calls[0]["reply_to"] is None
    assert calls[0]["channel"] == "C123"

    sql_calls = [c.args[0] for c in mock_cursor.execute.call_args_list]
    assert any("INSERT INTO nightly_run_threads" in s for s in sql_calls)
    assert any("UPDATE nightly_run_threads" in s for s in sql_calls)


# ---------------------------------------------------------------------------
# Test 2: second caller in same container hits cache (no DB at all)
# ---------------------------------------------------------------------------


def test_second_call_returns_cached_ts_without_reposting(no_db):
    """Same key returns the same ts and does NOT post a second parent."""
    from slack_thread_registry import _cache, get_or_create_thread

    # Seed the in-memory cache directly (no-db fixture means the DB path
    # is a no-op and the first call would return None without a DB).
    _cache[("nightly-2026-05-14", "forecast_analysis", "C456")] = "ts-cached"

    poster, calls = _capture_poster("ts-should-not-be-used")
    result = get_or_create_thread(
        run_id="nightly-2026-05-14",
        theme="forecast_analysis",
        channel_id="C456",
        parent_summary="Forecast: $4.2M Q3 commit",
        poster=poster,
    )

    assert result == "ts-cached"
    assert len(calls) == 0


# ---------------------------------------------------------------------------
# Test 3: concurrent claim race — loser polls and gets winner's ts
# ---------------------------------------------------------------------------


def test_concurrent_claim_race_loser_polls_until_winner_lands_ts(monkeypatch):
    """Second caller hits ON CONFLICT → polls → returns the winner's ts."""
    import db_adapter
    from slack_thread_registry import get_or_create_thread

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "postgres://test")
    # Speed up the poll loop so the test doesn't wait 500ms.
    monkeypatch.setattr("slack_thread_registry._PLACEHOLDER_POLL_INTERVAL_S", 0.001)

    # Sequence:
    #   1. initial _db_lookup → None (no row yet)
    #   2. _db_try_claim INSERT → rowcount=0 (we LOST)
    #   3. first poll lookup → returns NULL (placeholder still in flight)
    #   4. second poll lookup → returns "ts-winner" (winner updated)
    mock_conn, mock_cursor = _make_db_mock(
        fetchone_values=[
            None,  # initial lookup
            (None,),  # first poll — placeholder NULL
            ("ts-winner",),  # second poll — winner landed
        ],
        rowcount_values=[0, 0],  # lookup, claim (we lost)
    )

    poster, calls = _capture_poster("ts-should-not-fire")
    with patch.object(db_adapter, "_connect", return_value=mock_conn):
        result = get_or_create_thread(
            run_id="nightly-race",
            theme="pipeline_review",
            channel_id="C123",
            parent_summary="racing summary",
            poster=poster,
        )

    assert result == "ts-winner"
    # CRITICAL: the loser never posted a second parent.
    assert len(calls) == 0


# ---------------------------------------------------------------------------
# Test 4: claim won but Slack post fails → placeholder DELETED
# ---------------------------------------------------------------------------


def test_slack_post_failure_deletes_placeholder_for_retry(monkeypatch):
    """Claim wins → poster raises → placeholder DELETED → next call retries."""
    import db_adapter
    from slack_thread_registry import get_or_create_thread

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "postgres://test")

    # Sequence:
    #   1. initial lookup → None
    #   2. claim INSERT → rowcount=1 (won)
    #   3. (poster raises — no DB call)
    #   4. delete placeholder → rowcount=1
    mock_conn, mock_cursor = _make_db_mock(
        fetchone_values=[None],
        rowcount_values=[0, 1, 1],
    )

    fail_count = [0]

    def _failing_poster(**kwargs):
        fail_count[0] += 1
        raise RuntimeError("Slack API down")

    with patch.object(db_adapter, "_connect", return_value=mock_conn):
        result = get_or_create_thread(
            run_id="nightly-2026-05-14",
            theme="cost_report",
            channel_id="C123",
            parent_summary="Cost report",
            poster=_failing_poster,
        )

    assert result is None
    assert fail_count[0] == 1

    # A DELETE was issued so a retry from a fresh container has a clean slot.
    sql_calls = [c.args[0] for c in mock_cursor.execute.call_args_list]
    assert any("DELETE FROM nightly_run_threads" in s for s in sql_calls)


# ---------------------------------------------------------------------------
# Test 5: orphan sweep — NULL+old rows deleted, others survive
# ---------------------------------------------------------------------------


def test_orphan_sweep_deletes_only_old_null_rows(monkeypatch):
    """Sweep DELETE clauses match NULL ts older than max_age_minutes."""
    import db_adapter
    from slack_thread_registry import _sweep_orphan_placeholders

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "postgres://test")

    mock_conn, mock_cursor = _make_db_mock(
        fetchone_values=[],
        rowcount_values=[3],  # pretend 3 orphans got swept
    )

    with patch.object(db_adapter, "_connect", return_value=mock_conn):
        deleted = _sweep_orphan_placeholders(max_age_minutes=10)

    assert deleted == 3
    sql_calls = [c.args[0] for c in mock_cursor.execute.call_args_list]
    assert len(sql_calls) == 1
    sql = sql_calls[0]
    # The DELETE filters both NULL ts AND the age window.
    assert "DELETE FROM nightly_run_threads" in sql
    assert "thread_ts IS NULL" in sql
    assert "INTERVAL '10 minutes'" in sql


# ---------------------------------------------------------------------------
# Test 6: legacy happy-path (PR #184) — DB-hit returns persisted ts
# ---------------------------------------------------------------------------


def test_db_hit_returns_persisted_ts_without_reposting(monkeypatch):
    """Memory cache empty but DB has the row → reuse the persisted ts.

    Simulates container restart after a successful prior run: the DB
    holds a row with a real (non-NULL) thread_ts. Lookup returns it and
    we never reach the claim path.
    """
    import db_adapter
    from slack_thread_registry import get_or_create_thread

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "postgres://test")

    mock_conn, mock_cursor = _make_db_mock(
        fetchone_values=[("ts-persisted",)],
    )

    poster, calls = _capture_poster("ts-should-not-be-used")
    with patch.object(db_adapter, "_connect", return_value=mock_conn):
        result = get_or_create_thread(
            run_id="nightly-restarted",
            theme="investigation_finding",
            channel_id="C789",
            parent_summary="Investigation: late MQL flow",
            poster=poster,
        )

    assert result == "ts-persisted"
    assert len(calls) == 0


# ---------------------------------------------------------------------------
# Test 7: parent message contains agent's summary + pointer verbatim
# ---------------------------------------------------------------------------


def test_parent_message_contains_summary_and_pointer_verbatim(no_db):
    """The agent's summary goes in unmodified; the pointer is appended."""
    from slack_thread_registry import (
        DEFAULT_PARENT_POINTER,
        get_or_create_thread,
    )

    poster, calls = _capture_poster("ts-summary")
    agent_summary = (
        "Pipeline coverage 0.6x vs target — partner channel weakness driving the gap"
    )
    # With no DB, claim fails (returns False) → call falls through to
    # the loser-polling branch which eventually returns None. To exercise
    # the body-formatting path we seed the cache miss + bypass via poster
    # injection only — body formatting happens BEFORE the poster call,
    # so we can capture the call argument even without DB access by
    # forcing the claim path. The no_db fixture short-circuits all DB
    # helpers, so we need a one-off DB shim that lets the claim "win".
    import db_adapter

    db_adapter.DATABASE_URL = "postgres://test"
    mock_conn, _cur = _make_db_mock(
        fetchone_values=[None],
        rowcount_values=[0, 1, 1],
    )

    with patch.object(db_adapter, "_connect", return_value=mock_conn):
        get_or_create_thread(
            run_id="nightly-2026-05-14",
            theme="pipeline_review",
            channel_id="C123",
            parent_summary=agent_summary,
            poster=poster,
        )

    db_adapter.DATABASE_URL = ""

    assert len(calls) == 1
    posted_summary = calls[0]["summary"]
    assert agent_summary in posted_summary
    assert DEFAULT_PARENT_POINTER in posted_summary
    assert posted_summary.index(agent_summary) < posted_summary.index(
        DEFAULT_PARENT_POINTER
    )
    assert "\n\n" + DEFAULT_PARENT_POINTER in posted_summary


# ---------------------------------------------------------------------------
# Test 8: default run_id falls back to today's UTC date when missing
# ---------------------------------------------------------------------------


def test_default_run_id_uses_today_when_not_supplied(no_db):
    """Callers may omit run_id; the registry generates a UTC-date key."""
    from slack_thread_registry import _cache, _today_run_id, get_or_create_thread

    default_id = _today_run_id()
    # Seed cache so the no-DB path returns immediately.
    _cache[(default_id, "cost_report", "C123")] = "ts-default"

    poster, calls = _capture_poster("ts-should-not-fire")
    a = get_or_create_thread(
        run_id=None,
        theme="cost_report",
        channel_id="C123",
        parent_summary="Cost report",
        poster=poster,
    )
    b = get_or_create_thread(
        run_id=default_id,
        theme="cost_report",
        channel_id="C123",
        parent_summary="Cost report",
        poster=poster,
    )

    assert a == b == "ts-default"
    assert len(calls) == 0
