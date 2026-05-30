"""Tests for ``cost_collector`` — Admin API pull, Messages-API tracking, reconciliation.

All HTTP and DB interactions are mocked. No live Anthropic or Postgres calls.

Run:
    cd orchestrator && python3 -m pytest cost_collector_test.py -q
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# Set the env vars ``config.py`` requires BEFORE first import — the worktree
# checkout has no ``.env``, so without these the ``import config`` inside
# ``cost_collector`` raises at module load. setdefault means a real .env (when
# present, e.g. on dev laptops or in CI with real secrets) still wins.
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

# Drop any cached import of config — if a prior test (e.g. config_test) loaded
# it without these vars and it raised, the partial module sticks in sys.modules
# and breaks our subsequent import. Same defensive pattern config_test uses.
sys.modules.pop("config", None)
sys.modules.pop("cost_collector", None)


# ──────────────────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────────────────


def _fake_usage(
    input_tokens=0,
    output_tokens=0,
    cache_read_input_tokens=None,
    cache_creation_input_tokens=None,
):
    """SimpleNamespace shaped like anthropic.types.Usage for the Messages API.

    Mirrors messages_api_usage_test._fake_usage so the two test modules read
    identically. We don't import anthropic at all — getattr() reads attributes
    off SimpleNamespace exactly the same way it reads off the SDK Pydantic
    models.
    """
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
    )


class _FakeCursor:
    """Captures executed SQL + params for assertions.

    Implements just enough of psycopg2's cursor protocol for the patterns
    cost_collector uses: execute(), fetchall(), context manager. Returned rows
    are configurable per fetch via _next_fetch.
    """

    def __init__(self, fetch_results=None):
        # fetch_results: list of row-lists, popped in FIFO order on fetchall()
        self._fetch_results = list(fetch_results or [])
        self.executed = []  # list of (sql, params) tuples

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        if self._fetch_results:
            return self._fetch_results.pop(0)
        return []


class _FakeConn:
    """Captures commit() and close(). cursor() returns the same _FakeCursor."""

    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0
        self.closed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


def _patch_db(monkeypatch, cursor, db_url="postgres://test"):
    """Patch db_adapter so cost_collector sees a working DB. Returns the FakeConn."""
    import cost_collector
    import db_adapter

    fake_conn = _FakeConn(cursor)
    monkeypatch.setattr(db_adapter, "DATABASE_URL", db_url)
    monkeypatch.setattr(db_adapter, "_connect", lambda: fake_conn)
    # cost_collector reads db_adapter.DATABASE_URL at call time, so the patch
    # above is sufficient — no need to also patch cost_collector's reference.
    _ = cost_collector  # silence unused-import linter
    return fake_conn


# ══════════════════════════════════════════════════════════════════════════
# pull_anthropic_daily_costs
# ══════════════════════════════════════════════════════════════════════════


def test_pull_returns_zero_when_admin_key_unset(monkeypatch, caplog):
    """No ANTHROPIC_ADMIN_KEY → log warning, return 0, no HTTP, no DB."""
    import cost_collector

    monkeypatch.setattr(cost_collector.config, "ANTHROPIC_ADMIN_KEY", "")
    # If httpx is called, fail loud
    with patch.object(cost_collector.httpx, "Client") as mock_client:
        result = cost_collector.pull_anthropic_daily_costs()
    assert result == 0
    mock_client.assert_not_called()
    assert any(
        "ANTHROPIC_ADMIN_KEY unset" in r.message
        for r in caplog.records
        if r.levelname == "WARNING"
    )


def test_pull_returns_zero_when_db_unset(monkeypatch):
    """ADMIN key present but no DATABASE_URL → don't even hit Anthropic; bail."""
    import cost_collector
    import db_adapter

    monkeypatch.setattr(cost_collector.config, "ANTHROPIC_ADMIN_KEY", "sk-admin-test")
    monkeypatch.setattr(db_adapter, "DATABASE_URL", "")
    with patch.object(cost_collector.httpx, "Client") as mock_client:
        result = cost_collector.pull_anthropic_daily_costs()
    assert result == 0
    mock_client.assert_not_called()


def _make_httpx_responder(payloads_by_path):
    """Build an httpx.Client stand-in that returns canned JSON per URL path.

    payloads_by_path: {path_substring: json_payload}. The first matching
    substring wins, so caller can disambiguate usage vs. cost endpoints.
    """

    def _get(url, headers=None, params=None):
        for path, payload in payloads_by_path.items():
            if path in url:
                resp = MagicMock()
                resp.json.return_value = payload
                resp.raise_for_status.return_value = None
                return resp
        raise AssertionError(f"Unexpected URL: {url}")

    fake_client = MagicMock()
    fake_client.get.side_effect = _get
    return fake_client


def test_pull_writes_correct_rows_with_upsert(monkeypatch):
    """Happy path: Admin API returns one day of data → one upsert row written."""
    import cost_collector

    monkeypatch.setattr(cost_collector.config, "ANTHROPIC_ADMIN_KEY", "sk-admin-test")

    usage_payload = {
        "data": [
            {
                "starting_at": "2026-05-10T00:00:00Z",
                "ending_at": "2026-05-11T00:00:00Z",
                "results": [
                    {
                        "model": "claude-sonnet-4-6",
                        "workspace_id": "wrkspc_default",
                        "service_tier": "standard",
                        "uncached_input_tokens": 1000,
                        "output_tokens": 500,
                        "cache_read_input_tokens": 4000,
                        "cache_creation": {
                            "ephemeral_5m_input_tokens": 2000,
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
                "starting_at": "2026-05-10T00:00:00Z",
                "ending_at": "2026-05-11T00:00:00Z",
                "results": [
                    # Three rows for one (model, workspace, tier) tuple — one
                    # per token type. Should sum to a single anthropic_daily_costs row.
                    # Amounts are in cents as decimal strings.
                    {
                        "model": "claude-sonnet-4-6",
                        "workspace_id": "wrkspc_default",
                        "service_tier": "standard",
                        "token_type": "uncached_input_tokens",
                        "amount": "300.00",  # $3.00
                    },
                    {
                        "model": "claude-sonnet-4-6",
                        "workspace_id": "wrkspc_default",
                        "service_tier": "standard",
                        "token_type": "output_tokens",
                        "amount": "750.00",  # $7.50
                    },
                    {
                        "model": "claude-sonnet-4-6",
                        "workspace_id": "wrkspc_default",
                        "service_tier": "standard",
                        "token_type": "cache_read_input_tokens",
                        "amount": "120.00",  # $1.20
                    },
                ],
            }
        ],
        "has_more": False,
        "next_page": None,
    }

    fake_client = _make_httpx_responder(
        {
            "/usage_report/messages": usage_payload,
            "/cost_report": cost_payload,
        }
    )
    cursor = _FakeCursor()
    _patch_db(monkeypatch, cursor)
    result = cost_collector.pull_anthropic_daily_costs(days_back=1, client=fake_client)

    assert result == 1, "exactly one (date, model, workspace, tier) row"
    assert len(cursor.executed) == 1
    sql, params = cursor.executed[0]
    assert "INSERT INTO anthropic_daily_costs" in sql
    assert "ON CONFLICT" in sql
    # Param order matches the INSERT: bucket_date, model, workspace_id,
    # service_tier, input, output, cache_read, cache_write, cost_usd.
    bucket_date, model, ws, tier, inp, out, cr, cw, cost_usd = params
    assert bucket_date == "2026-05-10"
    assert model == "claude-sonnet-4-6"
    assert ws == "wrkspc_default"
    assert tier == "standard"
    assert inp == 1000
    assert out == 500
    assert cr == 4000
    assert cw == 2000
    # Cost sums: ($3.00 + $7.50 + $1.20) = $11.70
    assert abs(cost_usd - 11.70) < 1e-9


def test_pull_handles_pagination(monkeypatch):
    """``has_more=true`` triggers a second request with the ``page`` token."""
    import cost_collector

    monkeypatch.setattr(cost_collector.config, "ANTHROPIC_ADMIN_KEY", "sk-admin-test")

    usage_page_1 = {
        "data": [
            {
                "starting_at": "2026-05-09T00:00:00Z",
                "ending_at": "2026-05-10T00:00:00Z",
                "results": [
                    {
                        "model": "claude-opus-4-8",
                        "workspace_id": "wrk_a",
                        "service_tier": "standard",
                        "uncached_input_tokens": 100,
                        "output_tokens": 50,
                        "cache_read_input_tokens": 0,
                        "cache_creation": {},
                    }
                ],
            }
        ],
        "has_more": True,
        "next_page": "page_xyz",
    }
    usage_page_2 = {
        "data": [
            {
                "starting_at": "2026-05-10T00:00:00Z",
                "ending_at": "2026-05-11T00:00:00Z",
                "results": [
                    {
                        "model": "claude-opus-4-8",
                        "workspace_id": "wrk_a",
                        "service_tier": "standard",
                        "uncached_input_tokens": 200,
                        "output_tokens": 100,
                        "cache_read_input_tokens": 0,
                        "cache_creation": {},
                    }
                ],
            }
        ],
        "has_more": False,
        "next_page": None,
    }
    cost_payload = {"data": [], "has_more": False, "next_page": None}

    call_pages = []

    def _get(url, headers=None, params=None):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        if "/usage_report/messages" in url:
            page = (params or {}).get("page")
            call_pages.append(("usage", page))
            resp.json.return_value = (
                usage_page_2 if page == "page_xyz" else usage_page_1
            )
        else:
            resp.json.return_value = cost_payload
        return resp

    fake_client = MagicMock()
    fake_client.get.side_effect = _get
    cursor = _FakeCursor()
    _patch_db(monkeypatch, cursor)
    result = cost_collector.pull_anthropic_daily_costs(days_back=2, client=fake_client)

    assert ("usage", None) in call_pages
    assert ("usage", "page_xyz") in call_pages
    # Two distinct (date, model, workspace, tier) keys → two upserts
    assert result == 2


def test_pull_returns_zero_on_http_error(monkeypatch):
    """Admin API 5xx / network error → log + return 0 (don't crash the cron)."""
    import cost_collector

    monkeypatch.setattr(cost_collector.config, "ANTHROPIC_ADMIN_KEY", "sk-admin-test")

    def _boom(url, headers=None, params=None):
        resp = MagicMock()
        resp.raise_for_status.side_effect = cost_collector.httpx.HTTPError("boom")
        return resp

    fake_client = MagicMock()
    fake_client.get.side_effect = _boom
    cursor = _FakeCursor()
    _patch_db(monkeypatch, cursor)
    result = cost_collector.pull_anthropic_daily_costs(days_back=1, client=fake_client)
    assert result == 0
    assert cursor.executed == []


def test_pull_handles_empty_response(monkeypatch):
    """Both endpoints return zero buckets → 0 rows written, no error."""
    import cost_collector

    monkeypatch.setattr(cost_collector.config, "ANTHROPIC_ADMIN_KEY", "sk-admin-test")
    empty = {"data": [], "has_more": False, "next_page": None}
    fake_client = _make_httpx_responder(
        {"/usage_report/messages": empty, "/cost_report": empty}
    )
    cursor = _FakeCursor()
    _patch_db(monkeypatch, cursor)
    result = cost_collector.pull_anthropic_daily_costs(days_back=3, client=fake_client)
    assert result == 0
    assert cursor.executed == []


def test_pull_sends_admin_api_headers(monkeypatch):
    """Verify the auth header (x-api-key) and version are set correctly."""
    import cost_collector

    monkeypatch.setattr(cost_collector.config, "ANTHROPIC_ADMIN_KEY", "sk-admin-XYZ")
    empty = {"data": [], "has_more": False, "next_page": None}
    captured = []

    def _get(url, headers=None, params=None):
        captured.append((url, headers, params))
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = empty
        return resp

    fake_client = MagicMock()
    fake_client.get.side_effect = _get
    cursor = _FakeCursor()
    _patch_db(monkeypatch, cursor)
    cost_collector.pull_anthropic_daily_costs(days_back=1, client=fake_client)
    assert any(h["x-api-key"] == "sk-admin-XYZ" for _, h, _ in captured)
    assert all(h["anthropic-version"] == "2023-06-01" for _, h, _ in captured)


# ══════════════════════════════════════════════════════════════════════════
# track_messages_call
# ══════════════════════════════════════════════════════════════════════════


def test_track_messages_call_writes_correct_cost(monkeypatch):
    """Verify the dollar math for a realtime sonnet call.

    Sonnet 4.6 rates per MTOK: input=$3.00, output=$15.00, cache_read=$0.30,
    cache_write_5m=$3.75.

    Inputs: 1000 input, 500 output, 4000 cache_read, 2000 cache_write
    Expected cost:
        (1000 * 3.0 + 500 * 15.0 + 4000 * 0.30 + 2000 * 3.75) / 1_000_000
        = (3000 + 7500 + 1200 + 7500) / 1_000_000
        = 19200 / 1_000_000 = $0.0192
    """
    import cost_collector

    cursor = _FakeCursor()
    _patch_db(monkeypatch, cursor)

    usage = _fake_usage(
        input_tokens=1000,
        output_tokens=500,
        cache_read_input_tokens=4000,
        cache_creation_input_tokens=2000,
    )
    cost_collector.track_messages_call(
        call_site="self_heal",
        model="claude-sonnet-4-6",
        usage=usage,
    )
    assert len(cursor.executed) == 1
    sql, params = cursor.executed[0]
    assert "INSERT INTO messages_api_calls" in sql
    (
        call_site,
        model,
        inp,
        out,
        cr,
        cw,
        cost_usd,
        tier,
        batch_id,
    ) = params
    assert call_site == "self_heal"
    assert model == "claude-sonnet-4-6"
    assert inp == 1000
    assert out == 500
    assert cr == 4000
    assert cw == 2000
    assert tier == "realtime"
    assert batch_id is None
    assert abs(cost_usd - 0.0192) < 1e-9


def test_track_messages_call_applies_batch_multiplier(monkeypatch):
    """tier='batch' halves input + output rates (cache rates unchanged).

    Sonnet 4.6 inputs: 1000 input, 500 output, 4000 cache_read, 2000 cache_write.
    Realtime cost = $0.0192 (above). Batch cost:
        (1000 * 1.5 + 500 * 7.5 + 4000 * 0.30 + 2000 * 3.75) / 1_000_000
        = (1500 + 3750 + 1200 + 7500) / 1_000_000
        = 13950 / 1_000_000 = $0.01395
    """
    import cost_collector

    cursor = _FakeCursor()
    _patch_db(monkeypatch, cursor)

    usage = _fake_usage(
        input_tokens=1000,
        output_tokens=500,
        cache_read_input_tokens=4000,
        cache_creation_input_tokens=2000,
    )
    cost_collector.track_messages_call(
        call_site="self_heal",
        model="claude-sonnet-4-6",
        usage=usage,
        tier="batch",
        batch_id="msgbatch_01abc",
    )
    sql, params = cursor.executed[0]
    cost_usd = params[6]
    tier = params[7]
    batch_id = params[8]
    assert tier == "batch"
    assert batch_id == "msgbatch_01abc"
    assert abs(cost_usd - 0.01395) < 1e-9


def test_track_messages_call_handles_none_usage(monkeypatch):
    """A response with usage=None must still write a zero-cost row without crashing."""
    import cost_collector

    cursor = _FakeCursor()
    _patch_db(monkeypatch, cursor)
    cost_collector.track_messages_call(
        call_site="self_improve",
        model="claude-sonnet-4-6",
        usage=None,
    )
    _, params = cursor.executed[0]
    inp, out, cr, cw, cost_usd = params[2], params[3], params[4], params[5], params[6]
    assert (inp, out, cr, cw) == (0, 0, 0, 0)
    assert cost_usd == 0.0


def test_track_messages_call_unknown_model_zero_cost(monkeypatch, caplog):
    """An unknown model name logs a warning, returns 0 cost, still writes the row."""
    import cost_collector

    cursor = _FakeCursor()
    _patch_db(monkeypatch, cursor)
    usage = _fake_usage(input_tokens=10000, output_tokens=5000)
    cost_collector.track_messages_call(
        call_site="self_heal",
        model="claude-future-model-9-9",
        usage=usage,
    )
    _, params = cursor.executed[0]
    assert params[6] == 0.0
    assert any("No cost rates" in r.message for r in caplog.records)


def test_track_messages_call_noop_without_db(monkeypatch):
    """No DATABASE_URL → no _connect, no exception."""
    import cost_collector
    import db_adapter

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "")
    called = {"connect": False}

    def _trap_connect():
        called["connect"] = True
        raise AssertionError("Should not connect when DATABASE_URL unset")

    monkeypatch.setattr(db_adapter, "_connect", _trap_connect)

    usage = _fake_usage(input_tokens=10, output_tokens=10)
    cost_collector.track_messages_call(
        call_site="self_heal", model="claude-sonnet-4-6", usage=usage
    )
    assert called["connect"] is False


def test_track_messages_call_swallows_db_errors(monkeypatch, caplog):
    """A DB failure must not propagate — cost tracking is observability, not load-bearing."""
    import cost_collector
    import db_adapter

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "postgres://test")

    def _boom():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(db_adapter, "_connect", _boom)

    # Must not raise
    cost_collector.track_messages_call(
        call_site="self_heal",
        model="claude-sonnet-4-6",
        usage=_fake_usage(input_tokens=100, output_tokens=50),
    )
    assert any("failed to persist self_heal call" in r.message for r in caplog.records)


# ══════════════════════════════════════════════════════════════════════════
# compute_reconciliation
# ══════════════════════════════════════════════════════════════════════════


def test_reconciliation_drift_pct_positive(monkeypatch):
    """Anthropic billed $10, local estimated $9 → drift +10% (we under-estimated)."""
    import cost_collector

    cursor = _FakeCursor(
        fetch_results=[
            [("claude-sonnet-4-6", 7.0)],  # session_costs
            [("claude-sonnet-4-6", 2.0)],  # messages_api_calls
            [("claude-sonnet-4-6", 10.0)],  # anthropic_daily_costs
        ]
    )
    _patch_db(monkeypatch, cursor)
    result = cost_collector.compute_reconciliation("2026-05-10")
    assert result["date"] == "2026-05-10"
    assert result["local_total_usd"] == 9.0
    assert result["anthropic_total_usd"] == 10.0
    assert result["drift_usd"] == 1.0
    assert result["drift_pct"] == 0.10
    assert result["by_model"]["claude-sonnet-4-6"] == {
        "local": 9.0,
        "anthropic": 10.0,
    }


def test_reconciliation_drift_pct_negative(monkeypatch):
    """Anthropic billed $8, local estimated $10 → drift -25% (we over-estimated)."""
    import cost_collector

    cursor = _FakeCursor(
        fetch_results=[
            [("claude-opus-4-8", 10.0)],
            [],
            [("claude-opus-4-8", 8.0)],
        ]
    )
    _patch_db(monkeypatch, cursor)
    result = cost_collector.compute_reconciliation("2026-05-09")
    assert result["local_total_usd"] == 10.0
    assert result["anthropic_total_usd"] == 8.0
    assert result["drift_usd"] == -2.0
    assert result["drift_pct"] == -0.25


def test_reconciliation_handles_div_zero(monkeypatch):
    """When Anthropic total is 0 (no billing pulled), drift_pct is None — not Inf."""
    import cost_collector

    cursor = _FakeCursor(
        fetch_results=[
            [("claude-sonnet-4-6", 5.0)],
            [],
            [],  # no anthropic rows
        ]
    )
    _patch_db(monkeypatch, cursor)
    result = cost_collector.compute_reconciliation("2026-05-10")
    assert result["anthropic_total_usd"] == 0.0
    assert result["local_total_usd"] == 5.0
    assert result["drift_pct"] is None
    assert result["drift_usd"] == -5.0


def test_reconciliation_aggregates_across_models(monkeypatch):
    """Multiple models roll up correctly into totals + by_model breakdown."""
    import cost_collector

    cursor = _FakeCursor(
        fetch_results=[
            # session_costs
            [("claude-sonnet-4-6", 3.0), ("claude-opus-4-8", 2.0)],
            # messages_api_calls
            [("claude-sonnet-4-6", 1.0)],
            # anthropic_daily_costs
            [("claude-sonnet-4-6", 4.5), ("claude-opus-4-8", 2.2)],
        ]
    )
    _patch_db(monkeypatch, cursor)
    result = cost_collector.compute_reconciliation("2026-05-10")
    assert result["local_total_usd"] == 6.0
    assert result["anthropic_total_usd"] == 6.7
    assert result["by_model"]["claude-sonnet-4-6"]["local"] == 4.0
    assert result["by_model"]["claude-sonnet-4-6"]["anthropic"] == 4.5
    assert result["by_model"]["claude-opus-4-8"]["local"] == 2.0
    assert result["by_model"]["claude-opus-4-8"]["anthropic"] == 2.2


def test_reconciliation_accepts_date_object(monkeypatch):
    """compute_reconciliation(date.today()) is valid — not just strings."""
    from datetime import date as _date

    import cost_collector

    cursor = _FakeCursor(fetch_results=[[], [], []])
    _patch_db(monkeypatch, cursor)
    result = cost_collector.compute_reconciliation(_date(2026, 5, 10))
    assert result["date"] == "2026-05-10"


def test_reconciliation_degrades_without_db(monkeypatch):
    """No DATABASE_URL → empty payload with drift_pct=None, no exception."""
    import cost_collector
    import db_adapter

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "")
    result = cost_collector.compute_reconciliation("2026-05-10")
    assert result == {
        "date": "2026-05-10",
        "local_total_usd": 0.0,
        "anthropic_total_usd": 0.0,
        "drift_usd": 0.0,
        "drift_pct": None,
        "by_model": {},
    }


# ══════════════════════════════════════════════════════════════════════════
# Internal helpers (light coverage for tricky pieces)
# ══════════════════════════════════════════════════════════════════════════


def test_parse_amount_cents_to_usd_decimal_string():
    """Admin API returns amount as decimal string in cents — must divide by 100."""
    import cost_collector

    assert cost_collector._parse_amount_cents_to_usd("123.45") == pytest.approx(1.2345)
    assert cost_collector._parse_amount_cents_to_usd("0") == 0.0
    assert cost_collector._parse_amount_cents_to_usd(None) == 0.0
    assert cost_collector._parse_amount_cents_to_usd("not_a_number") == 0.0


def test_aggregate_usage_buckets_sums_cache_creation():
    """5m + 1h cache writes collapse to a single cache_write_tokens column."""
    import cost_collector

    buckets = [
        {
            "starting_at": "2026-05-10T00:00:00Z",
            "ending_at": "2026-05-11T00:00:00Z",
            "results": [
                {
                    "model": "claude-sonnet-4-6",
                    "workspace_id": "ws1",
                    "service_tier": "standard",
                    "uncached_input_tokens": 100,
                    "output_tokens": 50,
                    "cache_read_input_tokens": 200,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": 30,
                        "ephemeral_1h_input_tokens": 70,
                    },
                }
            ],
        }
    ]
    out = cost_collector._aggregate_usage_buckets(buckets)
    key = ("2026-05-10", "claude-sonnet-4-6", "ws1", "standard")
    assert out[key]["cache_write_tokens"] == 100
    assert out[key]["input_tokens"] == 100
    assert out[key]["output_tokens"] == 50
    assert out[key]["cache_read_tokens"] == 200


# ══════════════════════════════════════════════════════════════════════════
# Plan P2 — track_prompt_engineer_call (sub3 incident 2026-05-19)
# ══════════════════════════════════════════════════════════════════════════
#
# The PE preprocess path uses the Managed Agents Sessions API, whose
# ``usage`` object exposes ``cache_creation.ephemeral_5m_input_tokens`` +
# ``cache_creation.ephemeral_1h_input_tokens`` (the TTL split) instead of
# the Messages-API ``cache_creation_input_tokens`` scalar. The new helper
# ``track_prompt_engineer_call`` flattens that shape and writes one row to
# ``messages_api_calls`` per invocation — even on the return-None paths.


def _fake_session_usage(
    input_tokens=0,
    output_tokens=0,
    cache_read_input_tokens=0,
    cw_5m=0,
    cw_1h=0,
):
    """Session-shaped usage: ``cache_creation`` is a nested object, not a scalar.

    Mirrors ``anthropic.types.SessionUsage`` enough for ``_extract_session_usage``
    to walk it via getattr().
    """
    cc = SimpleNamespace(
        ephemeral_5m_input_tokens=cw_5m,
        ephemeral_1h_input_tokens=cw_1h,
    )
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
        cache_creation=cc,
    )


def test_track_pe_call_writes_ok_outcome_to_call_site(monkeypatch):
    """``outcome="ok"`` → ``call_site == "prompt_engineer_preprocess"``.

    Asserts the dollar math is correct for a Sonnet 4.6 call with both TTL
    cache-write tokens summed into a single ``cache_write_tokens`` value
    (the schema doesn't split TTL — we collapse both into the 5m bucket
    for the rate lookup since neither cron sets the 1h beta header).
    """
    import cost_collector

    cursor = _FakeCursor()
    _patch_db(monkeypatch, cursor)

    usage = _fake_session_usage(
        input_tokens=1000,
        output_tokens=500,
        cache_read_input_tokens=4000,
        cw_5m=1500,
        cw_1h=500,  # both should land in cache_write_tokens
    )
    cost_collector.track_prompt_engineer_call(
        outcome="ok",
        model="claude-sonnet-4-6",
        usage=usage,
        elapsed_s=1.23,
        portco_key="acme",
    )
    assert len(cursor.executed) == 1
    _, params = cursor.executed[0]
    (
        call_site,
        model,
        inp,
        out,
        cr,
        cw,
        cost_usd,
        tier,
        batch_id,
    ) = params
    assert call_site == "prompt_engineer_preprocess"
    assert model == "claude-sonnet-4-6"
    assert inp == 1000
    assert out == 500
    assert cr == 4000
    assert cw == 2000, "5m + 1h cache_creation tokens must sum into one column"
    assert tier == "realtime"
    assert batch_id is None
    # Cost math: (1000*3 + 500*15 + 4000*0.3 + 2000*3.75) / 1_000_000
    # = (3000 + 7500 + 1200 + 7500) / 1_000_000 = $0.0192
    assert abs(cost_usd - 0.0192) < 1e-9


def test_track_pe_call_encodes_outcome_into_call_site(monkeypatch):
    """Each return-None reason produces a distinctly-greppable ``call_site``
    value of the form ``prompt_engineer_preprocess:<reason>``.

    Encoding outcome into ``call_site`` lets us keep the existing
    ``messages_api_calls`` schema (no migration required) while still
    making the forensic signal queryable.
    """
    import cost_collector

    for outcome in (
        "agent_unconfigured",
        "session_create_failed",
        "empty_text_parts",
        "session_error",
        "json_parse_failed",
        "invalid_schema",
        "exception",
    ):
        cursor = _FakeCursor()
        _patch_db(monkeypatch, cursor)
        cost_collector.track_prompt_engineer_call(
            outcome=outcome,
            model="claude-sonnet-4-6",
            usage=None,
            elapsed_s=0.5,
            portco_key="acme",
        )
        _, params = cursor.executed[0]
        call_site = params[0]
        assert call_site == f"prompt_engineer_preprocess:{outcome}", (
            f"outcome {outcome!r} must produce call_site "
            f"prompt_engineer_preprocess:{outcome}, got {call_site!r}"
        )


def test_track_pe_call_persists_row_even_when_usage_is_none(monkeypatch):
    """The forensic value of P2 is that a row lands EVEN on the return-None
    paths where the SDK never produced usage. Confirm the row writes with
    all-zero token counts."""
    import cost_collector

    cursor = _FakeCursor()
    _patch_db(monkeypatch, cursor)
    cost_collector.track_prompt_engineer_call(
        outcome="empty_text_parts",
        model="claude-sonnet-4-6",
        usage=None,
        elapsed_s=2.0,
        portco_key="acme",
    )
    assert len(cursor.executed) == 1
    _, params = cursor.executed[0]
    # Tokens zero out; row still lands.
    assert params[2:6] == (0, 0, 0, 0)
    assert params[6] == 0.0
    assert params[0] == "prompt_engineer_preprocess:empty_text_parts"


def test_track_pe_call_noop_without_db(monkeypatch):
    """No DATABASE_URL → no _connect, no exception (same contract as
    ``track_messages_call``)."""
    import cost_collector
    import db_adapter

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "")

    def _trap_connect():
        raise AssertionError("Should not connect when DATABASE_URL unset")

    monkeypatch.setattr(db_adapter, "_connect", _trap_connect)

    cost_collector.track_prompt_engineer_call(
        outcome="ok",
        model="claude-sonnet-4-6",
        usage=None,
        elapsed_s=1.0,
        portco_key="acme",
    )


def test_track_pe_call_swallows_db_errors(monkeypatch, caplog):
    """A DB failure must not propagate — observability path is non-load-bearing."""
    import cost_collector
    import db_adapter

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "postgres://test")
    monkeypatch.setattr(
        db_adapter, "_connect", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    cost_collector.track_prompt_engineer_call(
        outcome="ok",
        model="claude-sonnet-4-6",
        usage=None,
        elapsed_s=1.0,
        portco_key="acme",
    )
    assert any(
        "track_prompt_engineer_call: failed to persist outcome=ok" in r.message
        for r in caplog.records
    )


def test_extract_session_usage_handles_messages_api_shape():
    """``_extract_session_usage`` must work transparently when handed a
    Messages-API-shaped usage object (scalar ``cache_creation_input_tokens``).

    Cross-shape resilience guards against an accidental ``track_messages_call``
    → ``track_prompt_engineer_call`` swap during a refactor.
    """
    import cost_collector

    # Messages-API shape — scalar cache_creation_input_tokens, no
    # cache_creation nested object.
    usage = SimpleNamespace(
        input_tokens=200,
        output_tokens=100,
        cache_read_input_tokens=300,
        cache_creation_input_tokens=400,
    )
    parts = cost_collector._extract_session_usage(usage)
    assert parts == {
        "input": 200,
        "output": 100,
        "cache_read": 300,
        "cache_write": 400,
    }
