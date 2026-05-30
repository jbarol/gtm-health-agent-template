"""Tests for orchestrator/compresr_client.py (Plan #37, Task #59).

Fully mocked — no real Compresr API calls, no real DB. Each test resets the
process-local circuit-breaker state and the one-time warning latches so tests
remain order-independent.

Covers:
  * Fallback when COMPRESR_API_KEY is unset.
  * Fallback when the per-site COMPRESS_* flag is False.
  * Skip when len(text) < min_chars.
  * Cache hit: second call with the same hash returns the cached text and
    does NOT call the SDK.
  * Circuit breaker: opens after 5 consecutive failures; subsequent calls
    fall back without invoking the SDK; reset_circuit clears state.
  * DB telemetry: one compresr_calls row written on every path (success +
    every fallback variant), with the correct columns populated.

Run:
    cd orchestrator && python3 -m pytest compresr_client_test.py -q
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# Required env vars for config.py to import without raising. We use setdefault
# so a real .env (when present) takes precedence. Mirrors _REQUIRED_DUMMIES in
# config_test.py.
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
# Fixtures + helpers
# ---------------------------------------------------------------------------


def _fake_compresr_result(compressed_text: str):
    """Build a SimpleNamespace shaped like the Compresr SDK response."""
    return SimpleNamespace(
        data=SimpleNamespace(
            compressed_context=compressed_text,
            original_tokens=None,
            compressed_tokens=None,
        )
    )


def _install_fake_sdk(compressed_text: str = "COMPRESSED"):
    """Inject a fake `compresr` module into sys.modules and return the client mock.

    The fake module exposes `CompressionClient(api_key=...).compress(...)`,
    which is what compresr_client._call_compresr imports.
    """
    fake_client_instance = MagicMock(name="CompressionClient_instance")
    fake_client_instance.compress = MagicMock(
        return_value=_fake_compresr_result(compressed_text)
    )
    fake_client_class = MagicMock(
        name="CompressionClient", return_value=fake_client_instance
    )
    fake_module = MagicMock(name="compresr")
    fake_module.CompressionClient = fake_client_class
    sys.modules["compresr"] = fake_module
    return fake_client_class, fake_client_instance


def _remove_fake_sdk():
    sys.modules.pop("compresr", None)


@pytest.fixture(autouse=True)
def _isolate_module_state():
    """Reset the module-level process state between tests."""
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
    """Patch db_adapter so cache + telemetry calls land in in-memory lists.

    Returns a SimpleNamespace with:
      * cache: dict[(hash, model)] -> compressed_text
      * calls: list of dicts (one per _record_call invocation)
    """
    cache_store: dict = {}
    calls_store: list[dict] = []

    class _FakeCursor:
        def __init__(self):
            self._last_sql = ""
            self._last_params = None
            self._fetched = None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=None):
            self._last_sql = sql
            self._last_params = params
            sql_lower = sql.lower().strip()
            if sql_lower.startswith("select compressed_text from compresr_cache"):
                # 2026-05-11: the SELECT now also binds the TTL (CACHE_TTL_DAYS)
                # as a third parameter for the ``INTERVAL '1 day' * %s`` clause.
                # Older callers only passed (content_hash, model); we accept
                # either shape so the fixture remains backwards-compatible.
                if len(params) == 3:
                    content_hash, model, _ttl_days = params
                else:
                    content_hash, model = params
                cached = cache_store.get((content_hash, model))
                self._fetched = (cached,) if cached else None
            elif sql_lower.startswith("insert into compresr_cache"):
                ch, mdl, ctext, ic, cc = params
                cache_store.setdefault((ch, mdl), ctext)
            elif sql_lower.startswith("delete from compresr_cache"):
                # ``expire_old_cache`` issues a DELETE with the TTL bound as
                # the only parameter; the fake fixture treats every cached
                # row as fresh, so it's a no-op here. The unit test for
                # ``expire_old_cache`` patches the cursor directly so this
                # branch only matters to make sure unrelated tests don't
                # crash if a future change starts firing the delete.
                pass
            elif sql_lower.startswith("insert into compresr_calls"):
                # Plan #37 task #66 added query_present + cache_hit columns to
                # the telemetry write — destructure 11 params now, not 9.
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
def configured_compresr(monkeypatch):
    """Set up config so all three call sites are enabled with a fake API key."""
    import config

    monkeypatch.setattr(config, "COMPRESR_API_KEY", "cmp_fake_test_key")
    monkeypatch.setattr(config, "COMPRESS_SELF_HEAL_ENABLED", True)
    monkeypatch.setattr(config, "COMPRESS_SELF_IMPROVE_ENABLED", True)
    monkeypatch.setattr(config, "COMPRESS_ADHOC_KICKOFF", True)


# ---------------------------------------------------------------------------
# Env-flag gating
# ---------------------------------------------------------------------------


def test_fallback_when_api_key_unset(monkeypatch, fake_db):
    """Empty COMPRESR_API_KEY → silent fallback to original text."""
    import config
    import compresr_client

    monkeypatch.setattr(config, "COMPRESR_API_KEY", "")
    monkeypatch.setattr(config, "COMPRESS_SELF_HEAL_ENABLED", True)

    out = compresr_client.compress_prompt(
        "this is the original payload" * 100,
        model="latte_v1",
        query="why",
        call_site="self_heal",
    )
    assert out == "this is the original payload" * 100
    # Telemetry row recorded with fallback=True / reason="disabled".
    assert len(fake_db.calls) == 1
    row = fake_db.calls[0]
    assert row["fallback"] is True
    assert row["fallback_reason"] == "disabled"
    assert row["call_site"] == "self_heal"
    assert row["input_chars"] == len("this is the original payload" * 100)


def test_fallback_when_site_flag_disabled(monkeypatch, fake_db):
    """Key set but per-site flag False → silent fallback."""
    import config
    import compresr_client

    monkeypatch.setattr(config, "COMPRESR_API_KEY", "cmp_fake_key")
    monkeypatch.setattr(config, "COMPRESS_SELF_HEAL_ENABLED", False)
    monkeypatch.setattr(config, "COMPRESS_SELF_IMPROVE_ENABLED", True)

    out = compresr_client.compress_prompt(
        "x" * 5000,
        model="espresso_v1",
        call_site="self_heal",
    )
    assert out == "x" * 5000
    assert fake_db.calls[-1]["fallback"] is True
    assert fake_db.calls[-1]["fallback_reason"] == "disabled"


def test_unknown_call_site_falls_back(monkeypatch, fake_db):
    """Unknown call_site name should not crash — silent fallback."""
    import config
    import compresr_client

    monkeypatch.setattr(config, "COMPRESR_API_KEY", "cmp_fake_key")

    out = compresr_client.compress_prompt("x" * 5000, call_site="not_a_real_site")
    assert out == "x" * 5000
    assert fake_db.calls[-1]["fallback"] is True
    assert fake_db.calls[-1]["fallback_reason"] == "disabled"


# ---------------------------------------------------------------------------
# min_chars threshold
# ---------------------------------------------------------------------------


def test_skip_compression_below_min_chars(configured_compresr, fake_db):
    """Text shorter than min_chars → return original, no SDK call."""
    import compresr_client

    _install_fake_sdk(compressed_text="SHORT")

    out = compresr_client.compress_prompt(
        "tiny",
        model="espresso_v1",
        call_site="self_improve",
        min_chars=1000,
    )
    assert out == "tiny"
    row = fake_db.calls[-1]
    assert row["fallback"] is True
    assert row["fallback_reason"] == "below_min_chars"
    assert row["input_chars"] == 4


# ---------------------------------------------------------------------------
# Cache lookup
# ---------------------------------------------------------------------------


def test_cache_hit_second_call_skips_sdk(configured_compresr, fake_db):
    """A second compress_prompt call with the same text returns cached output and never touches the SDK."""
    import compresr_client

    _, sdk_instance = _install_fake_sdk(compressed_text="CMP-OUTPUT")
    payload = "lorem ipsum dolor sit amet " * 500

    # First call: SDK runs once, cache populated.
    out1 = compresr_client.compress_prompt(
        payload,
        model="espresso_v1",
        call_site="self_improve",
        min_chars=100,
    )
    assert out1 == "CMP-OUTPUT"
    assert sdk_instance.compress.call_count == 1

    # Second call: cache hit, SDK NOT invoked a second time.
    out2 = compresr_client.compress_prompt(
        payload,
        model="espresso_v1",
        call_site="self_improve",
        min_chars=100,
    )
    assert out2 == "CMP-OUTPUT"
    assert sdk_instance.compress.call_count == 1  # unchanged

    # Two telemetry rows: first is success, second is cache_hit.
    assert len(fake_db.calls) == 2
    assert fake_db.calls[0]["fallback"] is False
    assert fake_db.calls[0]["fallback_reason"] is None
    assert fake_db.calls[1]["fallback"] is False
    assert fake_db.calls[1]["fallback_reason"] == "cache_hit"


def test_cache_keyed_by_model(configured_compresr, fake_db):
    """Different model on same text → separate cache key → SDK called again."""
    import compresr_client

    _, sdk_instance = _install_fake_sdk(compressed_text="OUT")
    payload = "a" * 5000

    compresr_client.compress_prompt(
        payload, model="espresso_v1", call_site="self_improve", min_chars=100
    )
    compresr_client.compress_prompt(
        payload,
        model="latte_v1",
        query="why",
        call_site="self_improve",
        min_chars=100,
    )
    assert sdk_instance.compress.call_count == 2


def test_cache_key_does_not_collide_on_different_queries(configured_compresr, fake_db):
    """Two ``latte_v1`` calls with the same text but different queries must
    hit different cache rows.

    Audit fix (docs/proposals/compresr-audit-2026-05-11.md §2): pre-fix the
    cache key was ``sha256(text)[:32]``, so the SECOND caller received the
    first caller's compression — built for a different question. That's a
    real correctness hazard. The fix binds ``query`` into the hash; this
    test pins it.
    """
    import compresr_client

    _, sdk_instance = _install_fake_sdk()
    # Each query gets its own compressed result so we can verify the right
    # one is returned to each caller.
    results = iter(
        [
            _fake_compresr_result("RESULT-FOR-QUERY-A"),
            _fake_compresr_result("RESULT-FOR-QUERY-B"),
        ]
    )
    sdk_instance.compress.side_effect = lambda **kwargs: next(results)
    payload = "shared payload " * 500

    out_a = compresr_client.compress_prompt(
        payload,
        model="latte_v1",
        query="question A",
        call_site="self_heal",
        min_chars=100,
    )
    out_b = compresr_client.compress_prompt(
        payload,
        model="latte_v1",
        query="question B",
        call_site="self_heal",
        min_chars=100,
    )
    # Both calls hit the SDK (no collision); each gets its own compression.
    assert sdk_instance.compress.call_count == 2
    assert out_a == "RESULT-FOR-QUERY-A"
    assert out_b == "RESULT-FOR-QUERY-B"
    # Two distinct cache rows persisted.
    assert len(fake_db.cache) == 2


def test_cache_hit_replays_same_query(configured_compresr, fake_db):
    """A repeat ``latte_v1`` call with the same text AND same query hits cache.

    Sanity-check that the query-aware key still functions as a cache when the
    query genuinely repeats — i.e. we didn't break ordinary cache hits while
    fixing the collision.
    """
    import compresr_client

    _, sdk_instance = _install_fake_sdk(compressed_text="CACHED-OUT")
    payload = "repeat payload " * 500

    out_1 = compresr_client.compress_prompt(
        payload,
        model="latte_v1",
        query="same question",
        call_site="self_heal",
        min_chars=100,
    )
    out_2 = compresr_client.compress_prompt(
        payload,
        model="latte_v1",
        query="same question",
        call_site="self_heal",
        min_chars=100,
    )
    assert out_1 == "CACHED-OUT"
    assert out_2 == "CACHED-OUT"
    # SDK only invoked once — the second call hit the cache.
    assert sdk_instance.compress.call_count == 1
    # Telemetry: success row + cache_hit row.
    assert fake_db.calls[-1]["cache_hit"] is True


# ---------------------------------------------------------------------------
# Cache expiry helper
# ---------------------------------------------------------------------------


def test_expire_old_cache_deletes_stale_rows(monkeypatch):
    """``expire_old_cache`` issues a DELETE with the TTL bound."""
    import compresr_client
    import db_adapter

    mock_cursor = MagicMock()
    mock_cursor.rowcount = 3
    mock_cursor.__enter__ = lambda self: self
    mock_cursor.__exit__ = lambda self, *a: False

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "postgres://test")
    monkeypatch.setattr(db_adapter, "_connect", MagicMock(return_value=mock_conn))

    deleted = compresr_client.expire_old_cache(ttl_days=7)

    mock_cursor.execute.assert_called_once()
    sql, params = mock_cursor.execute.call_args[0]
    assert "DELETE FROM compresr_cache" in sql
    assert "INTERVAL '1 day'" in sql
    assert params == (7,)
    mock_conn.commit.assert_called_once()
    assert deleted == 3


def test_expire_old_cache_handles_no_database_url(monkeypatch):
    """No DATABASE_URL → returns 0 without raising."""
    import compresr_client
    import db_adapter

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "")
    assert compresr_client.expire_old_cache() == 0


def test_expire_old_cache_swallows_db_exception(monkeypatch):
    """A raising _connect() returns 0; never propagates."""
    import compresr_client
    import db_adapter

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "postgres://test")
    monkeypatch.setattr(
        db_adapter, "_connect", MagicMock(side_effect=RuntimeError("db down"))
    )
    assert compresr_client.expire_old_cache() == 0


def test_expire_old_cache_default_ttl_matches_constant():
    """The default ttl_days matches ``CACHE_TTL_DAYS`` (7 today)."""
    import compresr_client
    import inspect

    sig = inspect.signature(compresr_client.expire_old_cache)
    assert sig.parameters["ttl_days"].default == compresr_client.CACHE_TTL_DAYS
    assert compresr_client.CACHE_TTL_DAYS == 7


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


def test_circuit_breaker_opens_after_five_failures(configured_compresr, fake_db):
    """Five consecutive SDK exceptions trip the breaker; subsequent calls skip the SDK entirely."""
    import compresr_client

    _, sdk_instance = _install_fake_sdk()
    sdk_instance.compress.side_effect = RuntimeError("upstream is down")

    payload = "x" * 5000

    # First 5 calls: each fails through the SDK, increments the failure
    # counter. The 5th call trips the breaker AT the end of the call.
    for i in range(5):
        assert not compresr_client.is_circuit_open("self_improve"), (
            f"breaker should still be closed before call {i + 1}"
        )
        out = compresr_client.compress_prompt(
            payload + str(i),  # vary text so we don't hit the cache
            model="espresso_v1",
            call_site="self_improve",
            min_chars=100,
        )
        assert out == payload + str(i)

    assert compresr_client.is_circuit_open("self_improve") is True
    # All 5 calls should have hit the SDK.
    assert sdk_instance.compress.call_count == 5

    # 6th call: breaker open → short-circuit, SDK NOT called again.
    out = compresr_client.compress_prompt(
        payload + "after-break",
        model="espresso_v1",
        call_site="self_improve",
        min_chars=100,
    )
    assert out == payload + "after-break"
    assert sdk_instance.compress.call_count == 5  # unchanged
    assert fake_db.calls[-1]["fallback_reason"] == "circuit_open"


def test_circuit_breaker_isolated_per_call_site(configured_compresr, fake_db):
    """A tripped breaker on one site must not affect another site."""
    import compresr_client

    _, sdk_instance = _install_fake_sdk()
    sdk_instance.compress.side_effect = RuntimeError("boom")
    payload = "y" * 5000

    for i in range(5):
        compresr_client.compress_prompt(
            payload + str(i),
            model="espresso_v1",
            call_site="self_improve",
            min_chars=100,
        )

    assert compresr_client.is_circuit_open("self_improve") is True
    assert compresr_client.is_circuit_open("self_heal") is False
    assert compresr_client.is_circuit_open("adhoc_kickoff") is False


def test_reset_circuit_clears_state(configured_compresr, fake_db):
    """reset_circuit() drops the failure count and reopens the site."""
    import compresr_client

    _, sdk_instance = _install_fake_sdk()
    sdk_instance.compress.side_effect = RuntimeError("nope")
    for i in range(5):
        compresr_client.compress_prompt(
            ("z" * 5000) + str(i),
            model="espresso_v1",
            call_site="self_improve",
            min_chars=100,
        )
    assert compresr_client.is_circuit_open("self_improve") is True

    compresr_client.reset_circuit("self_improve")
    assert compresr_client.is_circuit_open("self_improve") is False

    # Switch the SDK to succeed and confirm it gets called again.
    sdk_instance.compress.side_effect = None
    sdk_instance.compress.return_value = _fake_compresr_result("OK")
    out = compresr_client.compress_prompt(
        "fresh-payload-" + ("z" * 5000),
        model="espresso_v1",
        call_site="self_improve",
        min_chars=100,
    )
    assert out == "OK"


def test_successful_call_resets_failure_counter(configured_compresr, fake_db):
    """A success after 4 failures must reset the counter so the breaker doesn't trip on the next single failure."""
    import compresr_client

    _, sdk_instance = _install_fake_sdk()
    payload = "p" * 5000

    # 4 failures
    sdk_instance.compress.side_effect = RuntimeError("transient")
    for i in range(4):
        compresr_client.compress_prompt(
            payload + str(i),
            model="espresso_v1",
            call_site="self_improve",
            min_chars=100,
        )
    assert compresr_client.is_circuit_open("self_improve") is False

    # 1 success → counter resets.
    sdk_instance.compress.side_effect = None
    sdk_instance.compress.return_value = _fake_compresr_result("OK")
    compresr_client.compress_prompt(
        payload + "success",
        model="espresso_v1",
        call_site="self_improve",
        min_chars=100,
    )

    # 1 more failure → must NOT trip the breaker (counter is reset to 0).
    sdk_instance.compress.side_effect = RuntimeError("transient again")
    compresr_client.compress_prompt(
        payload + "fail-after-success",
        model="espresso_v1",
        call_site="self_improve",
        min_chars=100,
    )
    assert compresr_client.is_circuit_open("self_improve") is False


# ---------------------------------------------------------------------------
# Telemetry coverage
# ---------------------------------------------------------------------------


def test_telemetry_recorded_on_success(configured_compresr, fake_db):
    """A successful compression writes exactly one fully-populated row."""
    import compresr_client

    _install_fake_sdk(compressed_text="SHORTER")
    payload = "input" * 1000  # 5000 chars

    out = compresr_client.compress_prompt(
        payload,
        model="espresso_v1",
        call_site="self_improve",
        min_chars=100,
    )
    assert out == "SHORTER"

    assert len(fake_db.calls) == 1
    row = fake_db.calls[0]
    assert row["call_site"] == "self_improve"
    assert row["model"] == "espresso_v1"
    assert row["fallback"] is False
    assert row["fallback_reason"] is None
    assert row["input_chars"] == len(payload)
    assert row["compressed_chars"] == len("SHORTER")
    assert row["compression_ratio"] == pytest.approx(len("SHORTER") / len(payload))
    assert row["content_hash"] is not None
    assert len(row["content_hash"]) == 32  # sha256 truncated to 32 hex chars
    assert row["latency_ms"] >= 0


def test_telemetry_recorded_on_sdk_failure(configured_compresr, fake_db):
    """SDK exception → fallback=True, fallback_reason carries the exception type."""
    import compresr_client

    _, sdk_instance = _install_fake_sdk()
    sdk_instance.compress.side_effect = ValueError("invalid input")

    payload = "y" * 5000
    out = compresr_client.compress_prompt(
        payload,
        model="espresso_v1",
        call_site="self_improve",
        min_chars=100,
    )
    assert out == payload  # original returned

    row = fake_db.calls[-1]
    assert row["fallback"] is True
    assert row["fallback_reason"] == "sdk_error:ValueError"
    assert row["input_chars"] == len(payload)
    # On fallback we report compressed_chars == input_chars and ratio == 1.0.
    assert row["compressed_chars"] == len(payload)
    assert row["compression_ratio"] == 1.0


def test_telemetry_recorded_when_sdk_missing(configured_compresr, fake_db):
    """ImportError on the SDK → fallback with reason 'sdk_not_installed'."""
    import compresr_client

    # Ensure the fake module is NOT installed, then force ImportError.
    _remove_fake_sdk()

    def _raise_import(text, **kw):
        raise ImportError("No module named 'compresr'")

    with patch.object(compresr_client, "_call_compresr", side_effect=_raise_import):
        out = compresr_client.compress_prompt(
            "x" * 5000,
            model="espresso_v1",
            call_site="self_improve",
            min_chars=100,
        )
    assert out == "x" * 5000
    assert fake_db.calls[-1]["fallback_reason"] == "sdk_not_installed"


def test_fallback_when_compressed_is_not_shorter(configured_compresr, fake_db):
    """If Compresr returns something the same length or longer, fall back."""
    import compresr_client

    payload = "a" * 5000
    _install_fake_sdk(compressed_text="z" * 6000)  # LONGER than input

    out = compresr_client.compress_prompt(
        payload,
        model="espresso_v1",
        call_site="self_improve",
        min_chars=100,
    )
    assert out == payload
    assert fake_db.calls[-1]["fallback_reason"] == "no_compression_benefit"


def test_fallback_when_compressed_is_empty(configured_compresr, fake_db):
    """If Compresr returns empty string, fall back and count as failure."""
    import compresr_client

    _install_fake_sdk(compressed_text="")
    payload = "a" * 5000

    out = compresr_client.compress_prompt(
        payload,
        model="espresso_v1",
        call_site="self_improve",
        min_chars=100,
    )
    assert out == payload
    assert fake_db.calls[-1]["fallback_reason"] == "empty_result"


def test_latte_v1_passes_query_to_sdk(configured_compresr, fake_db):
    """latte_v1 path must forward the query kwarg to client.compress()."""
    import compresr_client

    _, sdk_instance = _install_fake_sdk(compressed_text="OUT")
    payload = "b" * 5000

    compresr_client.compress_prompt(
        payload,
        model="latte_v1",
        query="Review session abc123 for tool errors",
        call_site="self_heal",
        min_chars=100,
    )
    args, kwargs = sdk_instance.compress.call_args
    assert kwargs["compression_model_name"] == "latte_v1"
    assert kwargs["query"] == "Review session abc123 for tool errors"
    assert kwargs["context"] == payload


def test_espresso_v1_ignores_query(configured_compresr, fake_db):
    """espresso_v1 path must NOT forward a query kwarg even if one is supplied."""
    import compresr_client

    _, sdk_instance = _install_fake_sdk(compressed_text="OUT")

    compresr_client.compress_prompt(
        "c" * 5000,
        model="espresso_v1",
        query="should be ignored",
        call_site="self_improve",
        min_chars=100,
    )
    args, kwargs = sdk_instance.compress.call_args
    assert "query" not in kwargs
    assert kwargs["compression_model_name"] == "espresso_v1"


# ---------------------------------------------------------------------------
# Content hashing
# ---------------------------------------------------------------------------


def test_content_hash_is_32_hex_chars():
    """Sanity-check the cache key shape."""
    import compresr_client

    h = compresr_client._content_hash("hello world")
    assert len(h) == 32
    assert all(c in "0123456789abcdef" for c in h)


def test_content_hash_is_deterministic():
    """Same input → same hash (with the same model/query bound in)."""
    import compresr_client

    assert compresr_client._content_hash("abc") == compresr_client._content_hash("abc")
    assert compresr_client._content_hash("abc") != compresr_client._content_hash("abd")
    # Same text + same model + same query → same hash.
    assert compresr_client._content_hash(
        "abc", model="latte_v1", query="q1"
    ) == compresr_client._content_hash("abc", model="latte_v1", query="q1")


def test_content_hash_includes_model():
    """Different model on the same text → different hash.

    Audit fix (docs/proposals/compresr-audit-2026-05-11.md §2): the key must
    bind model so a Latte and an Espresso pass on the same text never collide.
    """
    import compresr_client

    assert compresr_client._content_hash(
        "same text", model="espresso_v1"
    ) != compresr_client._content_hash("same text", model="latte_v1")


def test_content_hash_includes_query():
    """Different query on the same text + model → different hash.

    Two ``latte_v1`` calls on the same text with different queries must get
    different cache keys — otherwise the second caller receives a compression
    made for someone else's question (correctness hazard).
    """
    import compresr_client

    assert compresr_client._content_hash(
        "same text", model="latte_v1", query="question A"
    ) != compresr_client._content_hash(
        "same text", model="latte_v1", query="question B"
    )


def test_content_hash_no_query_versus_empty_query():
    """``query=None`` and ``query=''`` collapse to the same key.

    Both represent the absence of a question (e.g. espresso_v1's no-query
    path) and should hit the same cache row.
    """
    import compresr_client

    assert compresr_client._content_hash(
        "text", model="espresso_v1", query=None
    ) == compresr_client._content_hash("text", model="espresso_v1", query="")


# ---------------------------------------------------------------------------
# DB unavailable — must not raise
# ---------------------------------------------------------------------------


def test_runs_without_database(monkeypatch, configured_compresr):
    """When DATABASE_URL is empty, telemetry calls silently no-op and compression still works."""
    import compresr_client
    import db_adapter

    _install_fake_sdk(compressed_text="OK-NO-DB")
    monkeypatch.setattr(db_adapter, "DATABASE_URL", "")

    out = compresr_client.compress_prompt(
        "n" * 5000,
        model="espresso_v1",
        call_site="self_improve",
        min_chars=100,
    )
    assert out == "OK-NO-DB"  # compression still happened


def test_db_exception_does_not_break_compression(monkeypatch, configured_compresr):
    """A raising _connect() must not break the calling code — fall through silently."""
    import compresr_client
    import db_adapter

    _install_fake_sdk(compressed_text="STILL-WORKS")
    monkeypatch.setattr(db_adapter, "DATABASE_URL", "postgres://test")
    monkeypatch.setattr(
        db_adapter, "_connect", MagicMock(side_effect=RuntimeError("db down"))
    )

    out = compresr_client.compress_prompt(
        "m" * 5000,
        model="espresso_v1",
        call_site="self_improve",
        min_chars=100,
    )
    # Cache lookup failed silently; SDK ran; cache write failed silently;
    # telemetry write failed silently. Caller still gets compressed text.
    assert out == "STILL-WORKS"
