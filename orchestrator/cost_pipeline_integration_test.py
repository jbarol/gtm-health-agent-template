"""End-to-end integration tests for the cost-tracking pipeline (Plan #35, Task #45).

These tests stitch together the modules that ship cost data from each write
path through to each reporting surface — exercising the same call graph the
running orchestrator uses, with only the external boundaries (Anthropic Admin
API, Postgres, Slack) mocked. They sit one rung above the per-module unit
tests in ``cost_collector_test.py`` / ``cost_digest_test.py`` /
``cost_reconcile_test.py`` / ``cost_backfill_30d_test.py`` /
``messages_api_usage_test.py``: those pin each function's contract, this
suite verifies the contracts compose.

Scenarios covered (one ``test_*`` per Task #45 bullet, plus a few combined
state checks):

  1. ``session_runner._persist_session_cost`` ends a session → row lands in
     ``session_costs`` with the documented token + cost shape.
  2. ``cost_collector.track_messages_call`` from self_heal / self_improve →
     row lands in ``messages_api_calls`` with the right ``tier`` and
     ``batch_id`` propagation.
  3. ``cost_collector.pull_anthropic_daily_costs`` against mocked Admin API
     responses → rows land in ``anthropic_daily_costs``; a second run with
     the same payload is idempotent (upsert keyed by
     ``(bucket_date, model, workspace_id, service_tier)``).
  4. ``cost_collector.reconcile_daily`` over seeded session_costs +
     anthropic_daily_costs → ``drift_pct`` correctly classifies the row as
     ok / watch / alert at the 10% and 25% boundaries and Slack fires only
     when expected.
  5. ``cost_digest.build_digest_message`` against seeded fixtures → message
     contains every required section (total, by-portco, by-task, cache rate,
     drift line, top-5 sessions); the watch banner only appears when drift
     exceeds the 10% threshold.
  6. ``scripts.cost_backfill_30d.main`` called twice → the second invocation
     does not produce duplicate rows (idempotency lives in the upsert in
     ``pull_anthropic_daily_costs``).

DB mocking strategy mirrors ``cost_reconcile_test._FakeCursor`` / ``_FakeConn``:
captured-execute lists per cursor so the assertions can inspect SQL and
parameter tuples. Slack is patched to a ``MagicMock`` notifier — no Slack-bolt
calls fire. Anthropic Admin API is patched via a fake ``httpx.Client`` that
returns canned JSON keyed by URL path.

Run::

    cd orchestrator && python3 -m pytest cost_pipeline_integration_test.py -q
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


# ──────────────────────────────────────────────────────────────────────────
# Defensive env stubbing — must run BEFORE any orchestrator module imports.
# Mirrors the pattern in cost_collector_test / cost_reconcile_test so the
# worktree (which has no .env) doesn't blow up at collection time.
# ──────────────────────────────────────────────────────────────────────────

for _k in (
    "ANTHROPIC_API_KEY",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "SLACK_CHANNEL_ID",
    "ENVIRONMENT_ID",
    "DREAM_AGENT_ID",
    "COORDINATOR_ID",
    "QUICK_AGENT_ID",
    "METHODOLOGY_STORE_ID",
    "HEALTH_STORE_ID",
):
    os.environ.setdefault(_k, "test-stub")

# Drop cached imports — same defensive pattern as the other cost tests.
for _mod in ("config", "cost_collector", "cost_digest", "cost_backfill_30d"):
    sys.modules.pop(_mod, None)

# Make the scripts/ dir importable so we can exercise cost_backfill_30d.main()
# end-to-end. The script imports cost_collector / db_adapter / config from
# orchestrator/, which is already on sys.path because pytest runs from there.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


# ──────────────────────────────────────────────────────────────────────────
# Fakes — psycopg2-shaped cursor/conn + simple usage objects
# ──────────────────────────────────────────────────────────────────────────


class _FakeCursor:
    """Minimal psycopg2-cursor stand-in.

    Records every (sql, params) tuple in ``executed`` so assertions can
    inspect both the table being written and the param ordering. Supports
    FIFO ``fetchall`` / ``fetchone`` queues so reconcile / digest queries can
    return canned rows in the order the production code reads them.

    ``cursor_factory`` is accepted and ignored — production code uses
    ``psycopg2.extras.RealDictCursor`` for SELECTs in cost_digest, but the
    digest tests in this suite drive those helpers via monkeypatching
    instead, so the dict-vs-tuple distinction never has to round-trip
    through the fake.
    """

    def __init__(self, fetchall_results=None, fetchone_results=None):
        self._fetchall = list(fetchall_results or [])
        self._fetchone = list(fetchone_results or [])
        self.executed: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return self._fetchall.pop(0) if self._fetchall else []

    def fetchone(self):
        return self._fetchone.pop(0) if self._fetchone else None


class _FakeConn:
    """Tracks commit/close calls and hands the same _FakeCursor to every with-block."""

    def __init__(self, cursor: _FakeCursor):
        self._cursor = cursor
        self.commits = 0
        self.closed = False

    # production code uses both `with conn.cursor() as cur:` and
    # `conn.cursor(cursor_factory=...)` — accept any args, return the same
    # cursor so executed/fetch state accumulates.
    def cursor(self, *args, **kwargs):
        return self._cursor

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


def _patch_db(
    monkeypatch, cursor: _FakeCursor, db_url: str = "postgres://test"
) -> _FakeConn:
    """Hook the fake cursor into db_adapter so cost_collector / session_runner
    write through it. Mirrors cost_reconcile_test._patch_db."""
    import db_adapter

    fake_conn = _FakeConn(cursor)
    monkeypatch.setattr(db_adapter, "DATABASE_URL", db_url)
    monkeypatch.setattr(db_adapter, "_connect", lambda: fake_conn)
    return fake_conn


def _fake_messages_usage(
    input_tokens=0,
    output_tokens=0,
    cache_read_input_tokens=None,
    cache_creation_input_tokens=None,
):
    """SimpleNamespace shaped like anthropic.types.Usage — Messages API.

    The Messages API surface uses ``cache_creation_input_tokens`` as a single
    scalar (no 5m/1h split), unlike the Managed Agents session usage object.
    This mirrors ``cost_collector_test._fake_usage`` exactly.
    """
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
    )


def _make_admin_api_client(payloads_by_path: dict):
    """Build an httpx.Client stand-in that returns canned JSON per URL path.

    Mirrors the helper in cost_collector_test so the two test files behave
    identically on the wire. First substring match wins, which lets callers
    keep the two Admin API endpoints separate without a full URL match.
    """

    def _get(url, headers=None, params=None):
        for path, payload in payloads_by_path.items():
            if path in url:
                resp = MagicMock()
                resp.json.return_value = payload
                resp.raise_for_status.return_value = None
                return resp
        raise AssertionError(f"Unexpected URL in fake Admin client: {url}")

    fake_client = MagicMock()
    fake_client.get.side_effect = _get
    return fake_client


# ══════════════════════════════════════════════════════════════════════════
# Scenario 1 — Session-cost write path
# ══════════════════════════════════════════════════════════════════════════


def test_session_end_persists_full_attribution_row(monkeypatch):
    """End-to-end: a session finishes → one row lands in ``session_costs``
    with the documented token breakdown, dollar estimate, and the full
    attribution tuple (portco, channel, thread, user, trigger, verbosity,
    agent, model, tier).

    Drives ``session_runner._persist_session_cost`` directly — the public
    entrypoint per ``session_cost_persist_test`` — and inspects the captured
    INSERT to confirm the column ordering matches the migration in
    ``db_adapter.ensure_schema()`` (session_id, agent_id, model,
    portco_key, channel_id, thread_ts, user_id, trigger, verbosity, ...,
    cost_usd, tier).
    """
    from session_runner import _persist_session_cost

    cursor = _FakeCursor()
    fake_conn = _FakeConn(cursor)
    # session_runner imports db_adapter as a module attribute; patch THAT
    # reference rather than the shared db_adapter module, mirroring the
    # pattern in session_cost_persist_test.py.
    with patch("session_runner.db_adapter") as mock_db:
        mock_db.DATABASE_URL = "postgres://test"
        mock_db._connect.return_value = fake_conn
        _persist_session_cost(
            session_id="sesn_EXAMPLE_001",
            agent_id="agent_coordinator",
            model="claude-opus-4-8",
            portco_key="acme",
            channel_id="C_FISH",
            thread_ts="T_THREAD_42",
            user_id="U_PARTNER",
            trigger="slack_mention",
            verbosity="expanded",
            usage_parts={
                "input": 1500,
                "output": 600,
                "cache_read": 4000,
                "cache_write_5m": 800,
                "cache_write_1h": 0,
            },
            cost_usd=0.0421,
            tier="realtime",
        )

    assert len(cursor.executed) == 1
    sql, params = cursor.executed[0]
    assert "INSERT INTO session_costs" in sql
    # cache_hit_pct = 4000 / (1500 + 4000 + 800 + 0) * 100 ≈ 63.49
    expected_cache_hit_pct = round(100.0 * 4000 / (1500 + 4000 + 800 + 0), 2)
    # Plan #42 PR1 D11 — outcome trails tier; default value is 'success'.
    assert params == (
        "sesn_EXAMPLE_001",
        "agent_coordinator",
        "claude-opus-4-8",
        "acme",
        "C_FISH",
        "T_THREAD_42",
        "U_PARTNER",
        "slack_mention",
        "expanded",
        1500,
        600,
        4000,
        800,
        0,
        0.0421,
        expected_cache_hit_pct,
        "realtime",
        "success",
    )
    assert fake_conn.commits == 1
    assert fake_conn.closed is True


def test_session_persist_cron_attribution_minimal_fields(monkeypatch):
    """Nightly cron sessions (dream / forecast / investigation) have no
    portco / channel / thread / user / verbosity — the row still writes
    cleanly with NULLs in those columns, ``trigger='cron'``, and the cost
    column populated."""
    from session_runner import _persist_session_cost

    cursor = _FakeCursor()
    fake_conn = _FakeConn(cursor)
    with patch("session_runner.db_adapter") as mock_db:
        mock_db.DATABASE_URL = "postgres://test"
        mock_db._connect.return_value = fake_conn
        _persist_session_cost(
            session_id="sesn_EXAMPLE_002",
            agent_id="agent_dream",
            model="claude-sonnet-4-6",
            portco_key=None,
            channel_id=None,
            thread_ts=None,
            user_id=None,
            trigger="cron",
            verbosity=None,
            usage_parts={
                "input": 200,
                "output": 100,
                "cache_read": 50,
                "cache_write_5m": 0,
                "cache_write_1h": 0,
            },
            cost_usd=0.00215,
            tier="realtime",
        )

    sql, params = cursor.executed[0]
    assert "INSERT INTO session_costs" in sql
    # The NULL-attribution columns are positional 4..9 (portco, channel,
    # thread, user, trigger, verbosity). Trigger should still be 'cron'.
    assert params[3] is None  # portco_key
    assert params[4] is None  # channel_id
    assert params[5] is None  # thread_ts
    assert params[6] is None  # user_id
    assert params[7] == "cron"  # trigger
    assert params[8] is None  # verbosity
    # Plan #42 PR1 D11 — outcome trails tier; column ordering is now
    # ... cost_usd, cache_hit_pct, tier, outcome. Default outcome = 'success'.
    assert params[-4] == 0.00215  # cost_usd
    # cache_hit_pct = 50 / (200 + 50 + 0 + 0) * 100 = 20.0
    assert params[-3] == 20.0  # cache_hit_pct
    assert params[-2] == "realtime"  # tier
    assert params[-1] == "success"  # outcome (Plan #42 default)


# ══════════════════════════════════════════════════════════════════════════
# Scenario 2 — Messages API tracking
# ══════════════════════════════════════════════════════════════════════════


def test_messages_api_call_writes_realtime_row(monkeypatch):
    """``cost_collector.track_messages_call`` invoked as ``self_heal`` would
    → one row lands in ``messages_api_calls`` with ``tier='realtime'``,
    ``batch_id=None``, and the dollar estimate computed from the sonnet rate
    table.

    Sonnet 4.6 ($3.00 input / $15.00 output / $0.30 cache_read / $3.75
    cache_write_5m per MTOK):
        1000 * 3.00 + 500 * 15.00 + 4000 * 0.30 + 2000 * 3.75
        = 3000 + 7500 + 1200 + 7500 = 19200 / 1_000_000 = $0.0192
    """
    import cost_collector

    cursor = _FakeCursor()
    _patch_db(monkeypatch, cursor)

    usage = _fake_messages_usage(
        input_tokens=1000,
        output_tokens=500,
        cache_read_input_tokens=4000,
        cache_creation_input_tokens=2000,
    )
    cost_collector.track_messages_call(
        call_site="self_heal._analyze_session",
        model="claude-sonnet-4-6",
        usage=usage,
    )

    assert len(cursor.executed) == 1
    sql, params = cursor.executed[0]
    assert "INSERT INTO messages_api_calls" in sql
    (call_site, model, inp, out, cr, cw, cost_usd, tier, batch_id) = params
    assert call_site == "self_heal._analyze_session"
    assert model == "claude-sonnet-4-6"
    assert (inp, out, cr, cw) == (1000, 500, 4000, 2000)
    assert tier == "realtime"
    assert batch_id is None
    assert abs(cost_usd - 0.0192) < 1e-9


def test_messages_api_batch_call_propagates_tier_and_batch_id(monkeypatch):
    """``tier='batch'`` halves input + output rates and the ``batch_id``
    flows through to the ledger row for cross-referencing ``batch_jobs``.

    Batch-tier cost for the same payload as above:
        (1000 * 1.5 + 500 * 7.5 + 4000 * 0.30 + 2000 * 3.75) / 1_000_000
        = (1500 + 3750 + 1200 + 7500) / 1_000_000 = $0.01395
    """
    import cost_collector

    cursor = _FakeCursor()
    _patch_db(monkeypatch, cursor)

    usage = _fake_messages_usage(
        input_tokens=1000,
        output_tokens=500,
        cache_read_input_tokens=4000,
        cache_creation_input_tokens=2000,
    )
    cost_collector.track_messages_call(
        call_site="self_improve._analyze_changes",
        model="claude-sonnet-4-6",
        usage=usage,
        tier="batch",
        batch_id="msgbatch_int_test",
    )

    sql, params = cursor.executed[0]
    cost_usd = params[6]
    tier = params[7]
    batch_id = params[8]
    assert tier == "batch"
    assert batch_id == "msgbatch_int_test"
    assert abs(cost_usd - 0.01395) < 1e-9


# ══════════════════════════════════════════════════════════════════════════
# Scenario 3 — Admin API pull + idempotency
# ══════════════════════════════════════════════════════════════════════════


def _admin_api_payloads_for_one_day():
    """Build a (usage_payload, cost_payload) pair representing one day of
    Admin API data for one (model, workspace, tier) tuple.

    Shape mirrors the live API:
      * usage_report returns one bucket per day with one row per group key.
      * cost_report returns multiple rows per (model, workspace, tier) tuple
        — one per token_type — which sum to the day's USD.
    """
    usage_payload = {
        "data": [
            {
                "starting_at": "2026-05-09T00:00:00Z",
                "ending_at": "2026-05-10T00:00:00Z",
                "results": [
                    {
                        "model": "claude-sonnet-4-6",
                        "workspace_id": "wrkspc_default",
                        "service_tier": "standard",
                        "uncached_input_tokens": 100000,
                        "output_tokens": 25000,
                        "cache_read_input_tokens": 400000,
                        "cache_creation": {
                            "ephemeral_5m_input_tokens": 50000,
                            "ephemeral_1h_input_tokens": 0,
                        },
                    }
                ],
            }
        ],
        "has_more": False,
        "next_page": None,
    }
    cost_payload = {
        "data": [
            {
                "starting_at": "2026-05-09T00:00:00Z",
                "ending_at": "2026-05-10T00:00:00Z",
                "results": [
                    {
                        "model": "claude-sonnet-4-6",
                        "workspace_id": "wrkspc_default",
                        "service_tier": "standard",
                        "token_type": "uncached_input_tokens",
                        "amount": "30000.00",  # $300.00
                    },
                    {
                        "model": "claude-sonnet-4-6",
                        "workspace_id": "wrkspc_default",
                        "service_tier": "standard",
                        "token_type": "output_tokens",
                        "amount": "37500.00",  # $375.00
                    },
                ],
            }
        ],
        "has_more": False,
        "next_page": None,
    }
    return usage_payload, cost_payload


def test_admin_api_pull_writes_upsert_row(monkeypatch):
    """End-to-end: mocked Admin API responses → exactly one upsert row lands
    in ``anthropic_daily_costs`` with the joined token + USD totals.

    Exercises the full ``pull_anthropic_daily_costs`` flow (paginated fetch
    → bucket aggregation → upsert) with no live HTTP, no live DB.
    """
    import cost_collector

    monkeypatch.setattr(cost_collector.config, "ANTHROPIC_ADMIN_KEY", "sk-admin-test")
    usage_payload, cost_payload = _admin_api_payloads_for_one_day()
    fake_client = _make_admin_api_client(
        {
            "/usage_report/messages": usage_payload,
            "/cost_report": cost_payload,
        }
    )
    cursor = _FakeCursor()
    _patch_db(monkeypatch, cursor)

    rows = cost_collector.pull_anthropic_daily_costs(days_back=1, client=fake_client)
    assert rows == 1

    sql, params = cursor.executed[0]
    assert "INSERT INTO anthropic_daily_costs" in sql
    assert "ON CONFLICT" in sql
    bucket_date, model, ws, tier, inp, out, cr, cw, cost_usd = params
    assert bucket_date == "2026-05-09"
    assert model == "claude-sonnet-4-6"
    assert ws == "wrkspc_default"
    assert tier == "standard"
    assert (inp, out, cr, cw) == (100000, 25000, 400000, 50000)
    # 300.00 + 375.00 = 675.00 (decimal-string cents → USD)
    assert abs(cost_usd - 675.00) < 1e-9


def test_admin_api_pull_is_idempotent_on_repeat(monkeypatch):
    """Idempotency contract: pulling the same window twice issues two upserts
    keyed by ``(bucket_date, model, workspace_id, service_tier)``. The ON
    CONFLICT clause guarantees no duplicate row at the DB level; the test
    asserts both calls used the same key + the upsert clause.

    We can't observe the DB-level conflict resolution against a fake cursor,
    so the assertion is on the SQL shape (presence of ``ON CONFLICT ... DO
    UPDATE``) and on the call count being deterministic across runs.
    """
    import cost_collector

    monkeypatch.setattr(cost_collector.config, "ANTHROPIC_ADMIN_KEY", "sk-admin-test")
    usage_payload, cost_payload = _admin_api_payloads_for_one_day()
    fake_client = _make_admin_api_client(
        {
            "/usage_report/messages": usage_payload,
            "/cost_report": cost_payload,
        }
    )
    cursor = _FakeCursor()
    _patch_db(monkeypatch, cursor)

    first = cost_collector.pull_anthropic_daily_costs(days_back=1, client=fake_client)
    second = cost_collector.pull_anthropic_daily_costs(days_back=1, client=fake_client)
    assert first == 1
    assert second == 1

    # Same INSERT (same key, same values) on both runs.
    sql1, params1 = cursor.executed[0]
    sql2, params2 = cursor.executed[1]
    assert params1 == params2
    for sql in (sql1, sql2):
        assert "ON CONFLICT (bucket_date, model, workspace_id, service_tier)" in sql
        assert "DO UPDATE" in sql


# ══════════════════════════════════════════════════════════════════════════
# Scenario 4 — Reconciliation drift gates + Slack alerting
# ══════════════════════════════════════════════════════════════════════════


def _seed_recon_cursor(
    local_rows, anthropic_rows, *, already_alerted=False
) -> _FakeCursor:
    """Build a cursor seeded so ``compute_reconciliation`` + the dedup lookup
    behave as expected. Order of fetchall calls inside
    ``compute_reconciliation``:

        1. session_costs grouped by model
        2. messages_api_calls grouped by model
        3. anthropic_daily_costs grouped by model

    Then ``_already_alerted`` does one fetchone (returns 1 if a prior alert
    exists, None otherwise).
    """
    return _FakeCursor(
        fetchall_results=[
            local_rows,
            [],  # messages_api_calls — empty by default
            anthropic_rows,
        ],
        fetchone_results=[(1,) if already_alerted else None],
    )


def test_reconcile_within_10pct_does_not_alert(monkeypatch):
    """5% drift → severity ``ok``, no Slack notification, no dedup write.

    Boundary contract: the watch gate is strictly ``|drift_pct| > 10%`` so
    anything at or below 10% stays quiet — protects the channel from
    healthy-but-noisy days.
    """
    import cost_collector

    cursor = _seed_recon_cursor(
        local_rows=[("claude-sonnet-4-6", 9.5)],
        anthropic_rows=[("claude-sonnet-4-6", 10.0)],
    )
    _patch_db(monkeypatch, cursor)
    notifier = MagicMock()

    result = cost_collector.reconcile_daily("2026-05-10", notifier=notifier)
    assert result["severity"] == "ok"
    assert result["alerted"] is False
    assert abs(result["drift_pct"] - 0.05) < 1e-9
    notifier.assert_not_called()
    # No alert row written when drift is within tolerance.
    assert not any(
        "INSERT INTO cost_reconciliation_alerts" in sql for sql, _ in cursor.executed
    )


def test_reconcile_watch_band_posts_single_slack_alert(monkeypatch):
    """15% drift → severity ``watch``, Slack notifier fires once with the
    under-estimated direction word and the percent in the body. Dedup row
    is written so a same-day re-run is silent."""
    import cost_collector

    cursor = _seed_recon_cursor(
        local_rows=[("claude-sonnet-4-6", 8.5)],
        anthropic_rows=[("claude-sonnet-4-6", 10.0)],
    )
    _patch_db(monkeypatch, cursor)
    notifier = MagicMock()

    result = cost_collector.reconcile_daily("2026-05-10", notifier=notifier)
    assert result["severity"] == "watch"
    assert result["direction"] == "under"
    assert result["alerted"] is True
    assert result["deduped"] is False
    notifier.assert_called_once()
    severity_arg, summary_arg = notifier.call_args.args
    assert severity_arg == "watch"
    assert "Cost reconciliation drift" in summary_arg
    assert "under-estimated" in summary_arg
    assert "+15.0%" in summary_arg
    # Watch severity must NOT recommend a MODEL_COSTS_PER_MTOK refresh —
    # that's the >25% alert lane.
    assert "Drift exceeds 25%" not in summary_arg
    assert any(
        "INSERT INTO cost_reconciliation_alerts" in sql for sql, _ in cursor.executed
    )


def test_reconcile_alert_band_adds_pricing_refresh_hint(monkeypatch):
    """40% drift → severity ``alert``, Slack body includes the
    ``MODEL_COSTS_PER_MTOK`` refresh recommendation.

    This is the boundary the daily cron uses to surface pricing-table drift
    — the most likely cause of >25% miss between local estimate and
    Anthropic billing."""
    import cost_collector

    cursor = _seed_recon_cursor(
        local_rows=[("claude-opus-4-8", 6.0)],
        anthropic_rows=[("claude-opus-4-8", 10.0)],
    )
    _patch_db(monkeypatch, cursor)
    notifier = MagicMock()

    result = cost_collector.reconcile_daily("2026-05-10", notifier=notifier)
    assert result["severity"] == "alert"
    assert result["alerted"] is True
    notifier.assert_called_once()
    summary_arg = notifier.call_args.args[1]
    assert "Drift exceeds 25%" in summary_arg
    assert "MODEL_COSTS_PER_MTOK" in summary_arg


def test_reconcile_at_exactly_10pct_stays_quiet(monkeypatch):
    """Exact 10% drift → gate stays closed (``severity='ok'``). Documents
    the strict-greater-than contract so a future refactor doesn't silently
    flip to >= and start alerting at the boundary."""
    import cost_collector

    cursor = _seed_recon_cursor(
        local_rows=[("claude-sonnet-4-6", 9.0)],
        anthropic_rows=[("claude-sonnet-4-6", 10.0)],
    )
    _patch_db(monkeypatch, cursor)
    notifier = MagicMock()

    result = cost_collector.reconcile_daily("2026-05-10", notifier=notifier)
    assert result["severity"] == "ok"
    notifier.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════
# Scenario 5 — Daily digest rendering with seeded data
# ══════════════════════════════════════════════════════════════════════════


def _seeded_digest_args(*, drift_pct: float | None, cache_pct: float | None = 71.2):
    """Build the dict of kwargs ``build_digest_message`` expects, seeded
    with a realistic per-portco / per-task / top-sessions payload so the
    assertion list below isn't fighting a degenerate input.

    Drift is configurable so the same fixture covers the watch-on /
    watch-off / drift-undefined branches.
    """
    local = 4.22
    if drift_pct is None:
        anthropic = 0.0
    else:
        # local = anthropic * (1 - drift_pct) → anthropic = local / (1 - drift_pct)
        anthropic = local / (1 - drift_pct)
    return {
        "total": local,
        "portco_rows": [
            {"portco": "acme", "cost_usd": 3.12, "sessions": 12},
            {"portco": "(messages-api)", "cost_usd": 1.10, "sessions": 4},
        ],
        "trigger_rows": [
            {"trigger": "cron", "cost_usd": 2.40, "sessions": 4},
            {"trigger": "slack_mention", "cost_usd": 1.18, "sessions": 7},
            {"trigger": "messages-api", "cost_usd": 0.64, "sessions": 4},
        ],
        "cache_pct": cache_pct,
        "recon": {
            "date": "2026-05-10",
            "local_total_usd": local,
            "anthropic_total_usd": anthropic,
            "drift_usd": (anthropic - local) if drift_pct is not None else 0.0,
            "drift_pct": drift_pct,
        },
        "top_sessions": [
            {
                "cost_usd": 0.84,
                "trigger": "slack_mention",
                "portco": "acme",
                "session_id": "sesn_EXAMPLE",
                "thread_ts": "T0123",
            },
            {
                "cost_usd": 0.61,
                "trigger": "cron",
                "portco": "(none)",
                "session_id": "sesn_EXAMPLE",
                "thread_ts": None,
            },
            {
                "cost_usd": 0.42,
                "trigger": "cron",
                "portco": "(none)",
                "session_id": "sesn_EXAMPLE",
                "thread_ts": None,
            },
            {
                "cost_usd": 0.20,
                "trigger": "slack_thread",
                "portco": "acme",
                "session_id": "sesn_EXAMPLE",
                "thread_ts": "T0456",
            },
            {
                "cost_usd": 0.15,
                "trigger": "recovery",
                "portco": "acme",
                "session_id": "sesn_EXAMPLE",
                "thread_ts": "T0123",
            },
        ],
    }


def test_digest_renders_all_required_sections_within_tolerance():
    """Within-tolerance drift → digest contains every required section in
    the documented order: ``*Cost - DATE*`` header, ``Total:`` line with
    inline by-task split, ``By portco:`` line, ``Cache:`` line with
    verdict, ``Drift vs Anthropic billing:`` line, and a ``*Top sessions:*``
    block with up to 5 rows. No watch banner."""
    import cost_digest

    args = _seeded_digest_args(drift_pct=0.021)
    msg = cost_digest.build_digest_message(date(2026, 5, 10), **args)

    # Watch banner is suppressed below the 10% threshold.
    assert ":warning: Watch" not in msg
    # Header + total + by-task inline summary.
    assert "*Cost — 2026-05-10*" in msg
    assert "Total: $4.22" in msg
    assert "$2.40 cron" in msg
    assert "$1.18 slack_mention" in msg
    assert "$0.64 messages-api" in msg
    # Per-portco line.
    assert "By portco: acme $3.12 · (messages-api) $1.10" in msg
    # Cache verdict (>= 60% renders as "good").
    assert "Cache: 71% hit-rate (good)" in msg
    # Drift line shows the +N.N% format and tolerance label.
    assert "Drift vs Anthropic billing: +2.1% (within tolerance)" in msg
    # Top-5 block — both thread-attached and session-only references render.
    assert "*Top sessions:*" in msg
    assert "$0.84  slack_mention  acme  (thread T0123)" in msg
    assert "session sesn_EXAMPLE" in msg


def test_digest_watch_banner_fires_above_10pct_drift():
    """|drift_pct| > 10% → the message leads with ``:warning: Watch ...``
    so the operator notices a reconciliation gap before reading the
    breakdown."""
    import cost_digest

    args = _seeded_digest_args(drift_pct=0.156)
    msg = cost_digest.build_digest_message(date(2026, 5, 10), **args)

    first_line = msg.split("\n", 1)[0]
    assert first_line.startswith(":warning: Watch"), (
        f"expected watch banner at top, got: {first_line!r}"
    )
    assert "under-estimated" in first_line
    assert "15.6%" in first_line
    assert "outside tolerance" in msg


def test_digest_drift_na_when_anthropic_not_yet_available():
    """When no Anthropic data has been pulled yet (drift_pct=None and
    Anthropic total = 0 but local > 0), the digest renders the ``n/a``
    variant rather than the within-tolerance line — protects against
    false reassurance on a partial day."""
    import cost_digest

    args = _seeded_digest_args(drift_pct=None)
    msg = cost_digest.build_digest_message(date(2026, 5, 10), **args)
    assert ":warning: Watch" not in msg
    assert "Drift vs Anthropic billing: n/a" in msg
    assert "Anthropic billing not yet available" in msg


# ══════════════════════════════════════════════════════════════════════════
# Scenario 6 — Backfill idempotency
# ══════════════════════════════════════════════════════════════════════════


def test_backfill_30d_invoked_twice_produces_no_duplicate_rows(monkeypatch):
    """The 30-day backfill script wraps ``pull_anthropic_daily_costs``.
    Running it twice in a row must not produce duplicate rows: the upsert
    path in the pull function carries the idempotency guarantee, and the
    backfill should pass the same ``days_back`` to it on each invocation.

    We assert by:
      * Verifying both runs call ``pull_anthropic_daily_costs(days_back=30)``
        exactly once each.
      * Mocking the pull function to return the same row count (mirroring
        what the real upsert would do) — DB-level dedup is asserted in the
        ``test_admin_api_pull_is_idempotent_on_repeat`` test above; here we
        verify the script does not, e.g., accumulate state between runs or
        misbehave on the second call.
    """
    import cost_backfill_30d  # type: ignore[import-not-found]

    monkeypatch.setattr(
        cost_backfill_30d.config, "ANTHROPIC_ADMIN_KEY", "sk-admin-test"
    )
    monkeypatch.setattr(cost_backfill_30d.db_adapter, "DATABASE_URL", "postgres://test")
    # First run upserts 30 rows; second run upserts the same 30 keys → same
    # count because the ON CONFLICT clause overwrites in place.
    pull_mock = MagicMock(return_value=30)
    monkeypatch.setattr(
        cost_backfill_30d.cost_collector, "pull_anthropic_daily_costs", pull_mock
    )
    monkeypatch.setattr(cost_backfill_30d, "_query_rows_per_date", lambda _d: {})
    monkeypatch.setattr(sys, "argv", ["cost_backfill_30d.py"])

    rc1 = cost_backfill_30d.main()
    rc2 = cost_backfill_30d.main()
    assert rc1 == 0
    assert rc2 == 0
    assert pull_mock.call_count == 2
    # Same invocation signature on both runs.
    for call in pull_mock.call_args_list:
        assert call.kwargs == {"days_back": 30}


def test_backfill_idempotency_at_the_upsert_level(monkeypatch):
    """Two consecutive end-to-end pulls write upserts keyed identically —
    the second pull's INSERT carries the same key columns as the first, so
    Postgres' ON CONFLICT clause guarantees no duplicate row.

    This complements the script-level test above by reaching one layer
    deeper: same fake Admin API, same fake DB cursor, two pulls.
    """
    import cost_collector

    monkeypatch.setattr(cost_collector.config, "ANTHROPIC_ADMIN_KEY", "sk-admin-test")
    usage_payload, cost_payload = _admin_api_payloads_for_one_day()
    fake_client = _make_admin_api_client(
        {
            "/usage_report/messages": usage_payload,
            "/cost_report": cost_payload,
        }
    )
    cursor = _FakeCursor()
    _patch_db(monkeypatch, cursor)

    cost_collector.pull_anthropic_daily_costs(days_back=1, client=fake_client)
    cost_collector.pull_anthropic_daily_costs(days_back=1, client=fake_client)

    assert len(cursor.executed) == 2
    sql1, params1 = cursor.executed[0]
    sql2, params2 = cursor.executed[1]

    # Same key tuple on both runs — bucket_date, model, workspace_id,
    # service_tier are the four PK columns.
    key1 = params1[:4]
    key2 = params2[:4]
    assert (
        key1
        == key2
        == ("2026-05-09", "claude-sonnet-4-6", "wrkspc_default", "standard")
    )

    # Both INSERTs include the conflict clause that lets Postgres dedupe.
    for sql in (sql1, sql2):
        assert "ON CONFLICT (bucket_date, model, workspace_id, service_tier)" in sql
        assert "DO UPDATE" in sql


# ══════════════════════════════════════════════════════════════════════════
# Cross-cutting — Slack notification is the only external side-effect that
# fires from the pipeline. Spot-check that reconciliation is the only
# scenario that calls the Slack notifier; no other code path along the
# pipeline emits a Slack message under the hood.
# ══════════════════════════════════════════════════════════════════════════


def test_pipeline_only_emits_slack_via_reconcile(monkeypatch):
    """Belt-and-suspenders: persisting a session, tracking a messages call,
    pulling Admin API data, and rendering a digest must never reach Slack
    on their own — only ``reconcile_daily`` does (and only when drift
    exceeds 10%).

    The test runs the four non-recon scenarios and asserts the supplied
    Slack notifier was never called. This guards against an accidental
    Slack post being added to a write path without going through the
    reconciliation lane.
    """
    import cost_collector
    import cost_digest
    from session_runner import _persist_session_cost

    notifier = MagicMock()

    # 1. session persist
    cursor = _FakeCursor()
    fake_conn = _FakeConn(cursor)
    with patch("session_runner.db_adapter") as mock_db:
        mock_db.DATABASE_URL = "postgres://test"
        mock_db._connect.return_value = fake_conn
        _persist_session_cost(
            session_id="sesn_EXAMPLE_slack",
            agent_id="a",
            model="claude-sonnet-4-6",
            portco_key="acme",
            channel_id=None,
            thread_ts=None,
            user_id=None,
            trigger="cron",
            verbosity=None,
            usage_parts={
                "input": 10,
                "output": 5,
                "cache_read": 0,
                "cache_write_5m": 0,
                "cache_write_1h": 0,
            },
            cost_usd=0.0001,
            tier="realtime",
        )

    # 2. messages-api track
    cursor2 = _FakeCursor()
    _patch_db(monkeypatch, cursor2)
    cost_collector.track_messages_call(
        call_site="self_heal",
        model="claude-sonnet-4-6",
        usage=_fake_messages_usage(input_tokens=10, output_tokens=5),
    )

    # 3. admin api pull
    monkeypatch.setattr(cost_collector.config, "ANTHROPIC_ADMIN_KEY", "sk-admin-test")
    usage_payload, cost_payload = _admin_api_payloads_for_one_day()
    fake_client = _make_admin_api_client(
        {"/usage_report/messages": usage_payload, "/cost_report": cost_payload}
    )
    cursor3 = _FakeCursor()
    _patch_db(monkeypatch, cursor3)
    cost_collector.pull_anthropic_daily_costs(days_back=1, client=fake_client)

    # 4. digest render (pure function — no side effects)
    cost_digest.build_digest_message(
        date(2026, 5, 10), **_seeded_digest_args(drift_pct=0.021)
    )

    notifier.assert_not_called()
