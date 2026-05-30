"""Tests for ``orchestrator/compresr_telemetry.py`` and its digest wiring.

Plan #37, Task #66. Two layers of coverage:

1. ``compresr_client.compress_prompt`` writes the new ``query_present`` and
   ``cache_hit`` columns correctly on every code path (success, cache hit,
   fallback variants).
2. ``compresr_telemetry`` aggregations compute the right rates / averages
   given a stubbed ``_fetch`` and the renderer emits the expected block shape.
3. ``cost_digest.build_digest_message`` includes the compression block when a
   summary is supplied and omits it when ``total_calls == 0`` or
   ``compresr_summary is None``.

DB and Slack side-effects are mocked. No live calls.

Run::

    cd orchestrator && python3 -m pytest compresr_telemetry_test.py -q
"""

from __future__ import annotations

import os
import sys
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# Mirror the .env stubbing pattern from the other test files so this module
# can be loaded in any environment.
for _k, _v in {
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
    os.environ.setdefault(_k, _v)


# ──────────────────────────────────────────────────────────────────────────
# compresr_client telemetry — query_present + cache_hit columns
# ──────────────────────────────────────────────────────────────────────────


def _fake_compresr_result(compressed_text: str):
    return SimpleNamespace(
        data=SimpleNamespace(
            compressed_context=compressed_text,
            original_tokens=None,
            compressed_tokens=None,
        )
    )


def _install_fake_sdk(compressed_text: str = "OUT"):
    fake_instance = MagicMock()
    fake_instance.compress = MagicMock(
        return_value=_fake_compresr_result(compressed_text)
    )
    fake_class = MagicMock(return_value=fake_instance)
    fake_module = MagicMock()
    fake_module.CompressionClient = fake_class
    sys.modules["compresr"] = fake_module
    return fake_class, fake_instance


def _remove_fake_sdk():
    sys.modules.pop("compresr", None)


@pytest.fixture(autouse=True)
def _isolate_module_state():
    """Reset module-level circuit / warn state between tests."""
    import compresr_client

    compresr_client._CIRCUIT_STATE.clear()
    compresr_client._WARNED_NO_KEY.clear()
    compresr_client._WARNED_SDK_MISSING = False
    yield
    compresr_client._CIRCUIT_STATE.clear()
    compresr_client._WARNED_NO_KEY.clear()
    compresr_client._WARNED_SDK_MISSING = False
    _remove_fake_sdk()


@pytest.fixture
def fake_db():
    """Capture every compresr_calls insert into a list of dicts.

    Mirrors the fixture in compresr_client_test.py but extended for the new
    ``query_present`` and ``cache_hit`` columns. Cache reads pull from the
    same in-memory dict so cache-hit telemetry can be verified.
    """
    cache_store: dict = {}
    calls_store: list[dict] = []

    class _FakeCursor:
        def __init__(self):
            self._fetched = None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            s = sql.lower().strip()
            if s.startswith("select compressed_text from compresr_cache"):
                # 2026-05-11: cache lookup now binds a TTL parameter for the
                # ``INTERVAL '1 day' * %s`` clause. Accept both 2- and
                # 3-tuple shapes so the fixture survives the change.
                if len(params) == 3:
                    ch, mdl, _ttl_days = params
                else:
                    ch, mdl = params
                cached = cache_store.get((ch, mdl))
                self._fetched = (cached,) if cached else None
            elif s.startswith("insert into compresr_cache"):
                ch, mdl, ctext, ic, cc = params
                cache_store.setdefault((ch, mdl), ctext)
            elif s.startswith("delete from compresr_cache"):
                # ``expire_old_cache`` issues a DELETE; the fixture treats
                # every cached row as fresh so this is a no-op here.
                pass
            elif s.startswith("insert into compresr_calls"):
                (
                    call_site,
                    model,
                    content_hash,
                    input_chars,
                    compressed_chars,
                    compression_ratio,
                    latency_ms,
                    fallback,
                    fallback_reason,
                    query_present,
                    cache_hit,
                ) = params
                calls_store.append(
                    {
                        "call_site": call_site,
                        "model": model,
                        "content_hash": content_hash,
                        "input_chars": input_chars,
                        "compressed_chars": compressed_chars,
                        "compression_ratio": compression_ratio,
                        "latency_ms": latency_ms,
                        "fallback": fallback,
                        "fallback_reason": fallback_reason,
                        "query_present": query_present,
                        "cache_hit": cache_hit,
                    }
                )

        def fetchone(self):
            return self._fetched

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

        def commit(self):
            pass

        def close(self):
            pass

    with (
        patch("db_adapter.DATABASE_URL", "postgres://test"),
        patch("db_adapter._connect", return_value=_FakeConn()),
    ):
        yield SimpleNamespace(cache=cache_store, calls=calls_store)


@pytest.fixture
def configured(monkeypatch):
    import config

    monkeypatch.setattr(config, "COMPRESR_API_KEY", "cmp_fake_key")
    monkeypatch.setattr(config, "COMPRESS_SELF_HEAL_ENABLED", True)
    monkeypatch.setattr(config, "COMPRESS_SELF_IMPROVE_ENABLED", True)
    monkeypatch.setattr(config, "COMPRESS_ADHOC_KICKOFF", True)


def test_query_present_true_for_latte_path(configured, fake_db):
    """latte_v1 with a query → ``query_present=True`` in the call row."""
    import compresr_client

    _install_fake_sdk(compressed_text="X")
    compresr_client.compress_prompt(
        "a" * 5000,
        model="latte_v1",
        query="why did this fail",
        call_site="self_heal",
        min_chars=100,
    )
    row = fake_db.calls[-1]
    assert row["query_present"] is True
    assert row["cache_hit"] is False
    assert row["fallback"] is False


def test_query_present_false_for_espresso_path(configured, fake_db):
    """espresso_v1 with no query → ``query_present=False``."""
    import compresr_client

    _install_fake_sdk(compressed_text="X")
    compresr_client.compress_prompt(
        "b" * 5000,
        model="espresso_v1",
        call_site="self_improve",
        min_chars=100,
    )
    row = fake_db.calls[-1]
    assert row["query_present"] is False
    assert row["cache_hit"] is False


def test_cache_hit_column_set_on_cache_hit(configured, fake_db):
    """Second call with same text → ``cache_hit=True`` and SDK NOT called."""
    import compresr_client

    _, sdk_instance = _install_fake_sdk(compressed_text="CACHED-OUT")
    payload = "c" * 5000

    compresr_client.compress_prompt(
        payload, model="espresso_v1", call_site="self_improve", min_chars=100
    )
    # First row: not a cache hit.
    assert fake_db.calls[0]["cache_hit"] is False

    compresr_client.compress_prompt(
        payload, model="espresso_v1", call_site="self_improve", min_chars=100
    )
    # Second row: cache hit, SDK NOT invoked twice.
    assert fake_db.calls[1]["cache_hit"] is True
    assert fake_db.calls[1]["fallback"] is False
    assert sdk_instance.compress.call_count == 1


def test_query_present_true_when_query_with_espresso(configured, fake_db):
    """A query passed to espresso_v1 still records ``query_present=True``.

    The SDK ignores the query for espresso, but the column reflects the
    caller's intent for telemetry — so we can later distinguish "caller
    forgot to set latte_v1" from "caller really meant no query".
    """
    import compresr_client

    _install_fake_sdk(compressed_text="X")
    compresr_client.compress_prompt(
        "d" * 5000,
        model="espresso_v1",
        query="should be ignored at SDK level",
        call_site="self_improve",
        min_chars=100,
    )
    row = fake_db.calls[-1]
    assert row["query_present"] is True


def test_cache_hit_false_on_fallback_paths(configured, fake_db):
    """All fallback paths record ``cache_hit=False``."""
    import compresr_client

    _, sdk_instance = _install_fake_sdk()
    sdk_instance.compress.side_effect = RuntimeError("boom")

    compresr_client.compress_prompt(
        "e" * 5000, model="espresso_v1", call_site="self_improve", min_chars=100
    )
    assert fake_db.calls[-1]["fallback"] is True
    assert fake_db.calls[-1]["cache_hit"] is False


# ──────────────────────────────────────────────────────────────────────────
# Aggregation queries — stub _fetch and verify the produced dict shape
# ──────────────────────────────────────────────────────────────────────────


def test_summarize_zero_rows_returns_empty_shape(monkeypatch):
    """No rows in window → all None/0 fields but no exception."""
    import compresr_telemetry

    monkeypatch.setattr(compresr_telemetry, "_fetch", lambda sql, params: [])

    out = compresr_telemetry.summarize(days=7)
    assert out["total_calls"] == 0
    assert out["cache_hit_rate"] is None
    assert out["fallback_rate"] is None
    assert out["avg_savings_ratio"] is None
    assert out["avg_latency_ms"] is None
    assert out["by_call_site"] == []


def test_summarize_computes_rates_and_ratio(monkeypatch):
    """A populated row produces the expected derived fractions."""
    import compresr_telemetry

    def _fake_fetch(sql, params):
        # Three different queries hit _fetch: counts, latency, by_call_site.
        s = sql.lower()
        if "percentile_cont" in s:
            return [{"avg_ms": 250.0, "p95_ms": 1200.0, "n": 100}]
        if "group by call_site" in s:
            return [
                {
                    "call_site": "self_heal",
                    "calls": 80,
                    "cache_hits": 30,
                    "fallbacks": 4,
                    "avg_ratio": 0.55,
                },
                {
                    "call_site": "self_improve",
                    "calls": 20,
                    "cache_hits": 10,
                    "fallbacks": 0,
                    "avg_ratio": 0.40,
                },
            ]
        # The first/main counts query.
        return [
            {
                "total": 100,
                "cache_hits": 40,
                "fallbacks": 4,
                "successful": 96,
                "avg_ratio": 0.50,
            }
        ]

    monkeypatch.setattr(compresr_telemetry, "_fetch", _fake_fetch)

    out = compresr_telemetry.summarize(days=1)
    assert out["total_calls"] == 100
    assert out["cache_hits"] == 40
    assert out["fallbacks"] == 4
    assert out["successful_calls"] == 96
    assert out["cache_hit_rate"] == pytest.approx(0.40)
    assert out["fallback_rate"] == pytest.approx(0.04)
    assert out["avg_savings_ratio"] == pytest.approx(0.50)
    assert out["avg_latency_ms"] == 250.0
    assert out["p95_latency_ms"] == 1200.0
    assert len(out["by_call_site"]) == 2


def test_cache_hit_rate_returns_none_when_no_rows(monkeypatch):
    """Zero-row window → None rather than 0.0."""
    import compresr_telemetry

    monkeypatch.setattr(
        compresr_telemetry,
        "_fetch",
        lambda sql, params: [{"total": 0, "hits": 0}],
    )
    assert compresr_telemetry.cache_hit_rate(days=1) is None


def test_fallback_rate_basic(monkeypatch):
    """3/10 fallbacks → fallback_rate=0.3."""
    import compresr_telemetry

    monkeypatch.setattr(
        compresr_telemetry,
        "_fetch",
        lambda sql, params: [{"total": 10, "fallbacks": 3}],
    )
    assert compresr_telemetry.fallback_rate(days=1) == pytest.approx(0.3)


def test_avg_savings_ratio_excludes_fallbacks(monkeypatch):
    """Confirm the query carries ``fallback IS FALSE`` so the SQL filters at the source."""
    import compresr_telemetry

    captured_sql = {}

    def _spy(sql, params):
        captured_sql["sql"] = sql
        return [{"avg_ratio": 0.45, "n": 12}]

    monkeypatch.setattr(compresr_telemetry, "_fetch", _spy)
    out = compresr_telemetry.avg_savings_ratio(days=1)
    assert out == pytest.approx(0.45)
    assert "fallback is false" in captured_sql["sql"].lower()


def test_latency_stats_handles_empty(monkeypatch):
    """No rows in window → n=0, fields None."""
    import compresr_telemetry

    monkeypatch.setattr(
        compresr_telemetry,
        "_fetch",
        lambda sql, params: [{"avg_ms": None, "p95_ms": None, "n": 0}],
    )
    out = compresr_telemetry.latency_stats(days=1)
    assert out["n"] == 0
    assert out["avg_ms"] is None
    assert out["p95_ms"] is None


def test_no_db_returns_empty_summary(monkeypatch):
    """DATABASE_URL unset → ``_fetch`` short-circuits, summarize returns zero-shape."""
    import compresr_telemetry
    import db_adapter

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "")
    out = compresr_telemetry.summarize(days=1)
    assert out["total_calls"] == 0
    assert out["cache_hit_rate"] is None
    assert out["by_call_site"] == []


def test_window_bounds_inclusive():
    """``days=7`` covers the trailing 7 days inclusive (end - 6)."""
    import compresr_telemetry

    start, end = compresr_telemetry._window_bounds(days=7, target=date(2026, 5, 10))
    assert end == date(2026, 5, 10)
    assert start == date(2026, 5, 4)


# ──────────────────────────────────────────────────────────────────────────
# Renderer — Slack block shape
# ──────────────────────────────────────────────────────────────────────────


def test_render_digest_block_empty_when_no_calls():
    """``total_calls=0`` → empty string (block omitted by the caller)."""
    import compresr_telemetry

    assert compresr_telemetry.render_digest_block({}) == ""
    assert compresr_telemetry.render_digest_block({"total_calls": 0}) == ""


def test_render_digest_block_happy_path():
    """A populated summary renders header + counts + rates + savings + latency + by-site."""
    import compresr_telemetry

    summary = {
        "days": 1,
        "total_calls": 47,
        "cache_hits": 21,
        "cache_hit_rate": 0.4468,
        "fallbacks": 3,
        "fallback_rate": 0.0638,
        "successful_calls": 44,
        "avg_savings_ratio": 0.48,
        "avg_latency_ms": 320.0,
        "p95_latency_ms": 980.0,
        "by_call_site": [
            {
                "call_site": "self_heal",
                "calls": 38,
                "cache_hits": 17,
                "fallbacks": 2,
                "avg_ratio": 0.5,
            },
            {
                "call_site": "self_improve",
                "calls": 9,
                "cache_hits": 4,
                "fallbacks": 0,
                "avg_ratio": 0.4,
            },
        ],
    }

    block = compresr_telemetry.render_digest_block(summary)
    assert "*Compression savings:*" in block
    assert "Calls: 47" in block
    assert "cache hits 21 / fallbacks 3" in block
    assert "Cache hit-rate: 45%" in block
    assert "Fallback rate: 6%" in block
    # 1 - 0.48 = 0.52 → 52% savings
    assert "Avg savings: 52%" in block
    # compressed/input = 48%
    assert "48% of input" in block
    assert "Latency: avg 320ms / p95 980ms" in block
    # By-site line
    assert "self_heal 38" in block
    assert "self_improve 9" in block


def test_render_digest_block_emits_watch_line_above_threshold():
    """``fallback_rate > 25%`` → leading ":warning: Watch" line."""
    import compresr_telemetry

    summary = {
        "total_calls": 10,
        "cache_hits": 0,
        "cache_hit_rate": 0.0,
        "fallbacks": 4,
        "fallback_rate": 0.40,
        "successful_calls": 6,
        "avg_savings_ratio": 0.50,
        "avg_latency_ms": 100.0,
        "p95_latency_ms": 200.0,
        "by_call_site": [],
    }
    block = compresr_telemetry.render_digest_block(summary)
    assert block.splitlines()[0].startswith(":warning: Watch")
    assert "40%" in block.splitlines()[0]


def test_render_digest_block_handles_missing_metrics():
    """``avg_savings_ratio=None`` (only fallbacks) → "n/a" label without crash."""
    import compresr_telemetry

    summary = {
        "total_calls": 3,
        "cache_hits": 0,
        "cache_hit_rate": 0.0,
        "fallbacks": 3,
        "fallback_rate": 1.0,
        "successful_calls": 0,
        "avg_savings_ratio": None,
        "avg_latency_ms": None,
        "p95_latency_ms": None,
        "by_call_site": [],
    }
    block = compresr_telemetry.render_digest_block(summary)
    assert "Avg savings: n/a" in block
    assert "avg n/a" in block
    assert "p95 n/a" in block


# ──────────────────────────────────────────────────────────────────────────
# cost_digest.build_digest_message — compression block wired in correctly
# ──────────────────────────────────────────────────────────────────────────


def _recon(
    local: float = 0.0, anthropic: float = 0.0, drift_pct: float | None = None
) -> dict:
    return {
        "date": "2026-05-10",
        "local_total_usd": local,
        "anthropic_total_usd": anthropic,
        "drift_usd": (anthropic - local) if drift_pct is None else 0,
        "drift_pct": drift_pct,
    }


def test_build_digest_message_includes_compression_block():
    """When a non-empty compresr_summary is passed, the block appears after drift / before top sessions."""
    import cost_digest

    summary = {
        "total_calls": 12,
        "cache_hits": 5,
        "cache_hit_rate": 5 / 12,
        "fallbacks": 1,
        "fallback_rate": 1 / 12,
        "successful_calls": 11,
        "avg_savings_ratio": 0.55,
        "avg_latency_ms": 150.0,
        "p95_latency_ms": 600.0,
        "by_call_site": [
            {
                "call_site": "self_heal",
                "calls": 12,
                "cache_hits": 5,
                "fallbacks": 1,
                "avg_ratio": 0.55,
            }
        ],
    }
    msg = cost_digest.build_digest_message(
        date(2026, 5, 10),
        total=4.22,
        portco_rows=[{"portco": "acme", "cost_usd": 4.22, "sessions": 12}],
        trigger_rows=[{"trigger": "cron", "cost_usd": 4.22, "sessions": 12}],
        cache_pct=70.0,
        recon=_recon(local=4.22, anthropic=4.30, drift_pct=0.02),
        top_sessions=[
            {
                "cost_usd": 0.5,
                "trigger": "cron",
                "portco": "acme",
                "session_id": "sesn_EXAMPLE",
                "thread_ts": None,
            }
        ],
        compresr_summary=summary,
    )
    # The compression block exists and is positioned BETWEEN the drift line
    # and the top-sessions block.
    drift_idx = msg.index("Drift vs Anthropic billing")
    compresr_idx = msg.index("*Compression savings:*")
    top_idx = msg.index("*Top sessions:*")
    assert drift_idx < compresr_idx < top_idx

    # Spot-check key metrics flowed through.
    assert "Calls: 12" in msg
    assert "Avg savings: 45%" in msg  # 1 - 0.55
    assert "self_heal 12" in msg


def test_build_digest_message_omits_block_when_summary_none():
    """``compresr_summary=None`` → no compression block, no errors."""
    import cost_digest

    msg = cost_digest.build_digest_message(
        date(2026, 5, 10),
        total=1.00,
        portco_rows=[{"portco": "acme", "cost_usd": 1.00, "sessions": 1}],
        trigger_rows=[{"trigger": "cron", "cost_usd": 1.00, "sessions": 1}],
        cache_pct=60.0,
        recon=_recon(local=1.00, anthropic=1.00, drift_pct=0.0),
        top_sessions=[],
        compresr_summary=None,
    )
    assert "*Compression savings:*" not in msg


def test_build_digest_message_omits_block_when_summary_empty():
    """``compresr_summary={"total_calls": 0, ...}`` → no compression block."""
    import cost_digest

    empty = {
        "total_calls": 0,
        "cache_hits": 0,
        "cache_hit_rate": None,
        "fallbacks": 0,
        "fallback_rate": None,
        "successful_calls": 0,
        "avg_savings_ratio": None,
        "avg_latency_ms": None,
        "p95_latency_ms": None,
        "by_call_site": [],
    }
    msg = cost_digest.build_digest_message(
        date(2026, 5, 10),
        total=1.00,
        portco_rows=[{"portco": "acme", "cost_usd": 1.00, "sessions": 1}],
        trigger_rows=[{"trigger": "cron", "cost_usd": 1.00, "sessions": 1}],
        cache_pct=60.0,
        recon=_recon(local=1.00, anthropic=1.00, drift_pct=0.0),
        top_sessions=[],
        compresr_summary=empty,
    )
    assert "*Compression savings:*" not in msg


def test_build_digest_message_omits_block_when_summary_default_param():
    """No ``compresr_summary`` kwarg (legacy callers) still works."""
    import cost_digest

    msg = cost_digest.build_digest_message(
        date(2026, 5, 10),
        total=1.00,
        portco_rows=[{"portco": "acme", "cost_usd": 1.00, "sessions": 1}],
        trigger_rows=[{"trigger": "cron", "cost_usd": 1.00, "sessions": 1}],
        cache_pct=60.0,
        recon=_recon(local=1.00, anthropic=1.00, drift_pct=0.0),
        top_sessions=[],
    )
    assert "*Compression savings:*" not in msg


# ──────────────────────────────────────────────────────────────────────────
# send_daily_cost_digest fetches the compresr summary
# ──────────────────────────────────────────────────────────────────────────


def test_send_digest_calls_compresr_summarize(monkeypatch):
    """End-to-end: send_daily_cost_digest invokes ``compresr_telemetry.summarize``
    for the same day and passes the result through to build_digest_message.
    """
    import cost_digest
    import compresr_telemetry
    import db_adapter

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "postgres://test")

    # Stub the cost-side helpers — we only care about the compresr pathway here.
    monkeypatch.setattr(cost_digest, "_total_cost", lambda d: 4.22)
    monkeypatch.setattr(
        cost_digest,
        "_portco_totals",
        lambda d: [{"portco": "acme", "cost_usd": 4.22, "sessions": 1}],
    )
    monkeypatch.setattr(
        cost_digest,
        "_trigger_totals",
        lambda d: [{"trigger": "cron", "cost_usd": 4.22, "sessions": 1}],
    )
    monkeypatch.setattr(cost_digest, "_cache_hit_pct", lambda d: 70.0)
    monkeypatch.setattr(cost_digest, "_top_sessions", lambda d, limit=5: [])
    monkeypatch.setattr(
        cost_digest,
        "compute_reconciliation",
        lambda d: _recon(local=4.22, anthropic=4.30, drift_pct=0.02),
    )

    fake_summary = {
        "total_calls": 7,
        "cache_hits": 3,
        "cache_hit_rate": 3 / 7,
        "fallbacks": 0,
        "fallback_rate": 0.0,
        "successful_calls": 7,
        "avg_savings_ratio": 0.45,
        "avg_latency_ms": 200.0,
        "p95_latency_ms": 500.0,
        "by_call_site": [
            {
                "call_site": "self_heal",
                "calls": 7,
                "cache_hits": 3,
                "fallbacks": 0,
                "avg_ratio": 0.45,
            }
        ],
    }

    summarize_spy = MagicMock(return_value=fake_summary)
    monkeypatch.setattr(compresr_telemetry, "summarize", summarize_spy)

    sender = MagicMock()
    result = cost_digest.send_daily_cost_digest(
        date(2026, 5, 10), sender=sender, admin_ids=["U_TEST"]
    )

    summarize_spy.assert_called_once_with(days=1, target=date(2026, 5, 10))
    assert result["sent"] == 1
    msg = sender.call_args.args[1]
    assert "*Compression savings:*" in msg
    assert "Calls: 7" in msg
    assert "Avg savings: 55%" in msg  # 1 - 0.45


def test_send_digest_swallows_compresr_summarize_failure(monkeypatch):
    """If summarize() raises, the digest still renders (without the block)."""
    import cost_digest
    import compresr_telemetry
    import db_adapter

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "postgres://test")
    monkeypatch.setattr(cost_digest, "_total_cost", lambda d: 2.10)
    monkeypatch.setattr(cost_digest, "_portco_totals", lambda d: [])
    monkeypatch.setattr(cost_digest, "_trigger_totals", lambda d: [])
    monkeypatch.setattr(cost_digest, "_cache_hit_pct", lambda d: None)
    monkeypatch.setattr(cost_digest, "_top_sessions", lambda d, limit=5: [])
    monkeypatch.setattr(
        cost_digest,
        "compute_reconciliation",
        lambda d: _recon(local=2.10, anthropic=2.10, drift_pct=0.0),
    )

    def _boom(*a, **kw):
        raise RuntimeError("compresr_calls table missing")

    monkeypatch.setattr(compresr_telemetry, "summarize", _boom)

    sender = MagicMock()
    result = cost_digest.send_daily_cost_digest(
        date(2026, 5, 10), sender=sender, admin_ids=["U_TEST"]
    )
    assert result["sent"] == 1
    msg = sender.call_args.args[1]
    # Cost block still rendered; compression block omitted.
    assert "Total: $2.10" in msg
    assert "*Compression savings:*" not in msg
