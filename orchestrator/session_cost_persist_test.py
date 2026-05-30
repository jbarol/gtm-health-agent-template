"""Tests for _persist_session_cost (Plan #35 Task #35).

Verifies that the session_costs row is written correctly given mocked
usage parts + attribution kwargs. Mocks db_adapter._connect so no real
DB connection is needed.

Plan #42 PR1 extension: the ``outcome`` column is now written and
inferred from session terminal state / audit-trail markers — see the
``_infer_session_outcome`` and outcome-write tests at the bottom of
the file.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _mk_usage_parts(input=100, output=50, cache_read=200, cw5=10, cw1=0):
    return {
        "input": input,
        "output": output,
        "cache_read": cache_read,
        "cache_write_5m": cw5,
        "cache_write_1h": cw1,
    }


def test_persist_writes_full_attribution_row():
    """Every kwarg flows into the INSERT params in the documented order."""
    from session_runner import _persist_session_cost

    mock_cursor = MagicMock()
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    with patch("session_runner.db_adapter") as mock_db:
        mock_db.DATABASE_URL = "postgres://test"
        mock_db._connect.return_value = mock_conn
        _persist_session_cost(
            session_id="sess-1",
            agent_id="agent_xyz",
            model="claude-opus-4-8",
            portco_key="acme",
            channel_id="C123",
            thread_ts="T456",
            user_id="U789",
            trigger="slack-adhoc",
            verbosity="expanded",
            usage_parts=_mk_usage_parts(),
            cost_usd=0.0123,
            tier="realtime",
        )

    mock_cursor.execute.assert_called_once()
    sql, params = mock_cursor.execute.call_args[0]
    assert "INSERT INTO session_costs" in sql
    # 2026-05-14: ON CONFLICT (session_id) DO UPDATE upsert — multi-turn
    # Slack thread follow-ups reuse the same session and write cumulative
    # token counts each turn. Without the upsert each turn would INSERT
    # a duplicate row with growing cumulative and reports would
    # double-count. Migration 00AI_session_costs_unique_session_id.sql
    # adds the unique index this clause depends on.
    assert "ON CONFLICT (session_id) DO UPDATE" in sql
    assert "input_tokens          = EXCLUDED.input_tokens" in sql
    # recorded_at intentionally NOT in the SET list so a follow-up turn on a
    # later day doesn't migrate the whole session's cost to that day. Codex
    # P2 finding 2026-05-14.
    assert "recorded_at" not in sql.split("DO UPDATE SET")[1].split("--")[0]
    # Spot-check the parameter ordering — column count matches placeholder
    # count. cache_hit_pct sits between cost_usd and tier (Plan #35 audit
    # 2026-05-11). With input=100, cache_read=200, cw5=10, cw1=0 the total
    # input-side traffic is 310, so cache_hit_pct = round(200/310*100, 2).
    # outcome (Plan #42 PR1 D11) is appended after tier; default is 'success'.
    expected_cache_hit_pct = round(100.0 * 200 / (100 + 200 + 10 + 0), 2)
    assert params == (
        "sess-1",
        "agent_xyz",
        "claude-opus-4-8",
        "acme",
        "C123",
        "T456",
        "U789",
        "slack-adhoc",
        "expanded",
        100,
        50,
        200,
        10,
        0,
        0.0123,
        expected_cache_hit_pct,
        "realtime",
        "success",
    )
    mock_conn.commit.assert_called_once()


def test_persist_no_database_url_skips_silently():
    """No DATABASE_URL → degraded mode, no DB call, no error."""
    from session_runner import _persist_session_cost

    with patch("session_runner.db_adapter") as mock_db:
        mock_db.DATABASE_URL = ""  # empty
        _persist_session_cost(
            session_id="sess-x",
            agent_id=None,
            model="m",
            portco_key=None,
            channel_id=None,
            thread_ts=None,
            user_id=None,
            trigger="cron-dream",
            verbosity=None,
            usage_parts=_mk_usage_parts(),
            cost_usd=0.0,
            tier="realtime",
        )
        mock_db._connect.assert_not_called()


def test_persist_swallows_db_exception():
    """DB errors are logged, not raised — ledger problems can't block sessions."""
    from session_runner import _persist_session_cost

    with patch("session_runner.db_adapter") as mock_db:
        mock_db.DATABASE_URL = "postgres://test"
        mock_db._connect.side_effect = Exception("boom")
        # Should NOT raise
        _persist_session_cost(
            session_id="sess-2",
            agent_id=None,
            model="m",
            portco_key=None,
            channel_id=None,
            thread_ts=None,
            user_id=None,
            trigger="cron-investigation",
            verbosity=None,
            usage_parts=_mk_usage_parts(),
            cost_usd=0.0,
            tier="realtime",
        )


def test_persist_batch_tier_propagates():
    from session_runner import _persist_session_cost

    mock_cursor = MagicMock()
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    with patch("session_runner.db_adapter") as mock_db:
        mock_db.DATABASE_URL = "postgres://test"
        mock_db._connect.return_value = mock_conn
        _persist_session_cost(
            session_id="sess-batch",
            agent_id="a",
            model="claude-sonnet-4-6",
            portco_key="acme",
            channel_id=None,
            thread_ts=None,
            user_id=None,
            trigger="batch-self-heal",
            verbosity=None,
            usage_parts=_mk_usage_parts(),
            cost_usd=0.005,
            tier="batch",
        )
    _, params = mock_cursor.execute.call_args[0]
    # outcome (Plan #42 PR1) is now the trailing column. tier is the
    # second-to-last; cache_hit_pct precedes tier; cost_usd precedes that.
    assert params[-1] == "success"  # default outcome
    assert params[-2] == "batch"  # tier
    assert params[-4] == 0.005  # cost
    # cache_hit_pct = 200 / (100 + 200 + 10 + 0) * 100 ≈ 64.52
    assert params[-3] == round(100.0 * 200 / 310, 2)


def test_persist_cache_hit_pct_zero_when_no_input_traffic():
    """No input/cache/write tokens at all → cache_hit_pct is 0.0, never NaN.

    Plan #35 audit (2026-05-11): the ``cost_rollup_daily`` view depends on
    every session_costs row carrying a real numeric value. A division-by-zero
    would have to be filtered out of the view; defaulting to 0 keeps the SQL
    aggregation clean.
    """
    from session_runner import _persist_session_cost

    mock_cursor = MagicMock()
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    with patch("session_runner.db_adapter") as mock_db:
        mock_db.DATABASE_URL = "postgres://test"
        mock_db._connect.return_value = mock_conn
        _persist_session_cost(
            session_id="sess-empty",
            agent_id="a",
            model="claude-sonnet-4-6",
            portco_key="acme",
            channel_id=None,
            thread_ts=None,
            user_id=None,
            trigger="cron",
            verbosity=None,
            usage_parts=_mk_usage_parts(input=0, cache_read=0, cw5=0, cw1=0),
            cost_usd=0.0,
            tier="realtime",
        )
    _, params = mock_cursor.execute.call_args[0]
    # cache_hit_pct is between cost_usd and tier; outcome trails tier
    # (Plan #42 PR1 D11). Index -3 = cache_hit_pct, -2 = tier, -1 = outcome.
    assert params[-3] == 0.0


def test_persist_cache_hit_pct_full_cache_hit():
    """All input came from cache → cache_hit_pct is 100.0."""
    from session_runner import _persist_session_cost

    mock_cursor = MagicMock()
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    with patch("session_runner.db_adapter") as mock_db:
        mock_db.DATABASE_URL = "postgres://test"
        mock_db._connect.return_value = mock_conn
        _persist_session_cost(
            session_id="sess-cached",
            agent_id="a",
            model="claude-sonnet-4-6",
            portco_key="acme",
            channel_id=None,
            thread_ts=None,
            user_id=None,
            trigger="cron",
            verbosity=None,
            usage_parts=_mk_usage_parts(input=0, cache_read=500, cw5=0, cw1=0),
            cost_usd=0.0001,
            tier="realtime",
        )
    _, params = mock_cursor.execute.call_args[0]
    # Plan #42 PR1 D11 — outcome trails tier; cache_hit_pct sits at -3.
    assert params[-3] == 100.0


def test_persist_cache_hit_pct_partial():
    """Half input fresh, half cached → cache_hit_pct ≈ 50.0."""
    from session_runner import _persist_session_cost

    mock_cursor = MagicMock()
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    with patch("session_runner.db_adapter") as mock_db:
        mock_db.DATABASE_URL = "postgres://test"
        mock_db._connect.return_value = mock_conn
        _persist_session_cost(
            session_id="sess-half",
            agent_id="a",
            model="claude-sonnet-4-6",
            portco_key="acme",
            channel_id=None,
            thread_ts=None,
            user_id=None,
            trigger="cron",
            verbosity=None,
            usage_parts=_mk_usage_parts(input=100, cache_read=100, cw5=0, cw1=0),
            cost_usd=0.01,
            tier="realtime",
        )
    _, params = mock_cursor.execute.call_args[0]
    # Plan #42 PR1 D11 — outcome trails tier; cache_hit_pct sits at -3.
    assert params[-3] == 50.0


def test_persist_outcome_error_written():
    """Plan #42 PR1 D11 — explicit ``outcome='error'`` is persisted as the
    trailing column. Verifies the INSERT path actually writes the new field
    rather than ignoring the kwarg.
    """
    from session_runner import _persist_session_cost

    mock_cursor = MagicMock()
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    with patch("session_runner.db_adapter") as mock_db:
        mock_db.DATABASE_URL = "postgres://test"
        mock_db._connect.return_value = mock_conn
        _persist_session_cost(
            session_id="sess-err",
            agent_id="a",
            model="claude-sonnet-4-6",
            portco_key="acme",
            channel_id=None,
            thread_ts=None,
            user_id=None,
            trigger="slack-adhoc",
            verbosity=None,
            usage_parts=_mk_usage_parts(),
            cost_usd=0.01,
            tier="realtime",
            outcome="error",
        )
    sql, params = mock_cursor.execute.call_args[0]
    assert "outcome" in sql
    assert params[-1] == "error"


def test_persist_outcome_defaults_to_success():
    """The default value matches the migration default ``'success'``."""
    from session_runner import _persist_session_cost

    mock_cursor = MagicMock()
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    with patch("session_runner.db_adapter") as mock_db:
        mock_db.DATABASE_URL = "postgres://test"
        mock_db._connect.return_value = mock_conn
        _persist_session_cost(
            session_id="sess-default",
            agent_id="a",
            model="claude-sonnet-4-6",
            portco_key="acme",
            channel_id=None,
            thread_ts=None,
            user_id=None,
            trigger="cron",
            verbosity=None,
            usage_parts=_mk_usage_parts(),
            cost_usd=0.0,
            tier="realtime",
        )
    _, params = mock_cursor.execute.call_args[0]
    assert params[-1] == "success"


# ───────────────────────────────────────────────────────────────────────
# Plan #42 PR1 D11 — outcome inference + end-to-end propagation
# ───────────────────────────────────────────────────────────────────────


def test_infer_outcome_writing_agent_fallthrough_marker_is_error():
    from session_runner import _infer_session_outcome

    s = SimpleNamespace(status="completed")
    assert (
        _infer_session_outcome(s, ["[WRITING_AGENT_FALLTHROUGH]", "post_report"])
        == "error"
    )


def test_infer_outcome_surface_push_failed_marker_is_error():
    from session_runner import _infer_session_outcome

    s = SimpleNamespace(status="completed")
    assert (
        _infer_session_outcome(s, ["post_report", "[SURFACE_PUSH_FAILED]"]) == "error"
    )


def test_infer_outcome_failed_status_is_error():
    from session_runner import _infer_session_outcome

    s = SimpleNamespace(status="failed")
    assert _infer_session_outcome(s, ["post_report"]) == "error"


def test_infer_outcome_cancelled_status_is_abandoned():
    from session_runner import _infer_session_outcome

    s = SimpleNamespace(status="cancelled")
    assert _infer_session_outcome(s, None) == "abandoned"


def test_infer_outcome_timed_out_status_is_abandoned():
    from session_runner import _infer_session_outcome

    s = SimpleNamespace(status="timed_out")
    assert _infer_session_outcome(s, None) == "abandoned"


def test_infer_outcome_completed_status_is_success():
    from session_runner import _infer_session_outcome

    s = SimpleNamespace(status="completed")
    assert _infer_session_outcome(s, ["post_report", "soqlQuery"]) == "success"


def test_infer_outcome_missing_status_attr_is_success():
    """Defensive: an object without ``.status`` falls back to success.

    Matches the migration default so degraded inference paths don't
    accidentally label clean sessions as errors.
    """
    from session_runner import _infer_session_outcome

    class NoStatus:
        pass

    assert _infer_session_outcome(NoStatus(), None) == "success"


def test_log_session_usage_propagates_inferred_outcome():
    """End-to-end: a failed-status session reaches ``_persist_session_cost``
    with ``outcome='error'`` (Plan #42 PR1 D11). Confirms the wiring between
    the inference helper, ``_log_session_usage``, and the INSERT path.
    """
    import session_runner

    fake_session = SimpleNamespace(
        status="failed",
        model="claude-opus-4-8",
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )

    persist_calls = {}

    def fake_persist(**kwargs):
        persist_calls.update(kwargs)

    with (
        patch.object(
            session_runner.client.beta.sessions, "retrieve", return_value=fake_session
        ),
        patch.object(session_runner, "_persist_session_cost", side_effect=fake_persist),
        patch.object(session_runner, "_estimate_cost", return_value=0.01),
        patch.object(
            session_runner,
            "_extract_usage_parts",
            return_value={
                "input": 10,
                "output": 5,
                "cache_read": 0,
                "cache_write_5m": 0,
                "cache_write_1h": 0,
            },
        ),
        patch.object(session_runner, "_cache_hit_pct", return_value=0.0),
    ):
        session_runner._log_session_usage(
            "sess-fail",
            "adhoc",
            portco_key="acme",
            trigger="slack-adhoc",
            tool_names=["post_report"],
        )

    assert persist_calls.get("outcome") == "error"


def test_log_session_usage_explicit_outcome_overrides_inference():
    """Caller can pass an explicit ``outcome=`` and it wins over the
    inferred value. Lets future call sites (e.g. recovery worker
    detecting an explicit user abandonment) override the default.
    """
    import session_runner

    fake_session = SimpleNamespace(
        status="completed",
        model="claude-sonnet-4-6",
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )
    persist_calls = {}

    def fake_persist(**kwargs):
        persist_calls.update(kwargs)

    with (
        patch.object(
            session_runner.client.beta.sessions, "retrieve", return_value=fake_session
        ),
        patch.object(session_runner, "_persist_session_cost", side_effect=fake_persist),
        patch.object(session_runner, "_estimate_cost", return_value=0.0),
        patch.object(
            session_runner,
            "_extract_usage_parts",
            return_value={
                "input": 10,
                "output": 5,
                "cache_read": 0,
                "cache_write_5m": 0,
                "cache_write_1h": 0,
            },
        ),
        patch.object(session_runner, "_cache_hit_pct", return_value=0.0),
    ):
        session_runner._log_session_usage(
            "sess-explicit",
            "recovery",
            outcome="abandoned",
        )

    assert persist_calls.get("outcome") == "abandoned"
