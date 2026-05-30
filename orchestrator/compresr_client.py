"""Compresr SDK wrapper — content-hash cache, circuit breaker, env-gating, DB telemetry.

Plan #37, Task #59. Thin wrapper around the Compresr Python SDK that the call
sites in self_heal._analyze_session, self_improve._analyze_changes, and
session_runner.run_adhoc_mcp_session route through. Compression is opt-in per
site (via the COMPRESS_*_ENABLED flags from config.py) and silently falls back
to the original text on any failure — compression must never break the calling
code.

Public API
----------
- compress_prompt(text, *, model, query, call_site, min_chars) -> str
  Compress text via Compresr or return original on fallback. Records telemetry
  on every call. Reads the cache before any network call; writes the cache on
  success. Trips a per-call-site circuit breaker after 5 consecutive failures.

- is_circuit_open(call_site) -> bool
  Check whether the per-site breaker is currently open.

- reset_circuit(call_site) -> None
  Manually clear the breaker (e.g. from a /unfreeze-compresr slash command).

Fallback contract
-----------------
Returns the original text immediately when ANY of:
  * COMPRESR_API_KEY unset, OR the site flag is False (silent, logs once).
  * len(text) < min_chars (deliberate skip; not a failure).
  * Circuit breaker open for this call_site.
  * Compresr SDK import fails (package not installed).
  * SDK constructor or .compress() raises ANY exception.
  * Compression returns longer text than the input (negative ROI guard).

Every call writes exactly one row to compresr_calls; cache hits skip the
network entirely but still record telemetry so the daily digest sees them.

Per-call telemetry columns populated on every call (Plan #37, task #66):
  * ``ts`` / ``created_at`` — wall-clock timestamp (server default).
  * ``call_site`` — "self_heal" | "self_improve" | "adhoc_kickoff".
  * ``model`` — "espresso_v1" or "latte_v1".
  * ``query_present`` — True iff a query was passed (latte_v1 path).
  * ``input_chars`` / ``compressed_chars`` — pre- and post-compression sizes.
  * ``cache_hit`` — True iff served from compresr_cache (no SDK call).
  * ``fallback`` — True iff the original text was returned unchanged.
  * ``fallback_reason`` — short reason code when ``fallback IS TRUE``.
  * ``latency_ms`` — wall-clock duration of the compress_prompt call.

These columns power ``orchestrator/compresr_telemetry.py`` and the
"Compression savings" block in the daily DM digest.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from typing import Optional

log = logging.getLogger(__name__)

# Circuit breaker config — module-level constants so tests can monkeypatch.
CIRCUIT_BREAKER_THRESHOLD = 5  # consecutive failures before site is opened
# Map call_site -> {"failures": int, "open": bool} (process-local; no DB).
_CIRCUIT_STATE: dict[str, dict] = {}
_CIRCUIT_LOCK = threading.Lock()

# One-time warning latch so "no key set" doesn't spam logs on every call.
_WARNED_NO_KEY: set[str] = set()
_WARNED_SDK_MISSING = False


# ---------------------------------------------------------------------------
# Env-flag mapping — keep in sync with config.py
# ---------------------------------------------------------------------------

# Each call_site maps to the COMPRESS_*_ENABLED flag attribute on config.py.
# We import config lazily inside _is_site_enabled so tests can monkeypatch the
# flags via importlib.reload(config) without this module caching stale values.
_CALL_SITE_TO_FLAG = {
    "self_heal": "COMPRESS_SELF_HEAL_ENABLED",
    "self_improve": "COMPRESS_SELF_IMPROVE_ENABLED",
    "adhoc_kickoff": "COMPRESS_ADHOC_KICKOFF",
}


def _is_site_enabled(call_site: str) -> bool:
    """Return True if both the API key is set AND the per-site flag is True."""
    try:
        import config  # local import — picks up monkeypatched module under tests
    except Exception:  # config can fail to import in some test contexts
        return False
    if not getattr(config, "COMPRESR_API_KEY", ""):
        return False
    flag_attr = _CALL_SITE_TO_FLAG.get(call_site)
    if flag_attr is None:
        # Unknown call site — refuse to compress so misconfigured callers
        # silently fall back rather than accidentally compressing.
        return False
    return bool(getattr(config, flag_attr, False))


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


def is_circuit_open(call_site: str) -> bool:
    """Check whether the per-site breaker is currently open."""
    with _CIRCUIT_LOCK:
        state = _CIRCUIT_STATE.get(call_site)
        return bool(state and state.get("open"))


def reset_circuit(call_site: str) -> None:
    """Manually clear the breaker (e.g. via an admin slash command)."""
    with _CIRCUIT_LOCK:
        _CIRCUIT_STATE.pop(call_site, None)
    log.info("Compresr circuit breaker reset for call_site=%s", call_site)


def _record_failure(call_site: str) -> None:
    """Increment the failure counter; open the breaker at the threshold."""
    with _CIRCUIT_LOCK:
        state = _CIRCUIT_STATE.setdefault(call_site, {"failures": 0, "open": False})
        state["failures"] += 1
        if state["failures"] >= CIRCUIT_BREAKER_THRESHOLD and not state["open"]:
            state["open"] = True
            log.warning(
                "Compresr circuit breaker OPENED for call_site=%s after %d "
                "consecutive failures — subsequent calls will fall back to "
                "original text until reset_circuit() is called.",
                call_site,
                state["failures"],
            )


def _record_success(call_site: str) -> None:
    """Reset the consecutive-failure counter (but never the open flag)."""
    with _CIRCUIT_LOCK:
        state = _CIRCUIT_STATE.get(call_site)
        if state and not state.get("open"):
            state["failures"] = 0


# ---------------------------------------------------------------------------
# Content hashing
# ---------------------------------------------------------------------------


def _content_hash(text: str, model: str = "", query: Optional[str] = None) -> str:
    """sha256(text || model || (query or '')) truncated to 32 hex chars.

    Audit fix (docs/proposals/compresr-audit-2026-05-11.md §2): the original
    implementation hashed text alone, which meant two ``latte_v1`` calls on
    the same text with different queries collided. The second caller got the
    first caller's compression, made for a different question — a real
    correctness hazard once latte_v1 traffic diversifies. The cache PK on
    ``compresr_cache`` is ``(content_hash, model)``, so binding model + query
    into the hash itself preserves the existing PK while making the key
    safe.

    ``model`` and ``query`` default to empty so any legacy caller that
    forgets to pass them still gets the original sha256(text) shape for
    backwards compatibility — but every internal call site now passes them
    in. The new default is to ALWAYS pass model.
    """
    payload = f"{text}||{model}||{query or ''}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# DB cache (best-effort; falls through silently when DB is unavailable)
# ---------------------------------------------------------------------------

# 7-day TTL for compresr_cache rows. The cache stores compressed text keyed
# by (content_hash, model). Stale rows still semantically valid for replay,
# but Compresr's compression model may improve over time and the on-disk
# bytes accumulate without bound otherwise. Plan #37 task documented the
# TTL; this constant + ``expire_old_cache`` enforce it.
CACHE_TTL_DAYS = 7


def _cache_lookup(content_hash: str, model: str) -> Optional[str]:
    """Return cached compressed_text for (content_hash, model), or None.

    Honors the ``CACHE_TTL_DAYS`` window — rows older than the TTL are
    treated as cache misses so a stale entry never overrides a fresher
    SDK compression. The row is left in place; ``expire_old_cache`` is the
    explicit deleter so lookups stay cheap (no write inside a read path).
    """
    try:
        import db_adapter

        if not db_adapter.DATABASE_URL:
            return None
        conn = db_adapter._connect()
        try:
            with conn.cursor() as cur:
                # Postgres-safe: ``INTERVAL '1 day' * N`` lets us bind N as a
                # placeholder. ``INTERVAL '%s days'`` would inline the
                # placeholder as a string and fail with a syntax error.
                cur.execute(
                    "SELECT compressed_text FROM compresr_cache "
                    "WHERE content_hash = %s AND model = %s "
                    "AND created_at >= NOW() - INTERVAL '1 day' * %s",
                    (content_hash, model, CACHE_TTL_DAYS),
                )
                row = cur.fetchone()
                return row[0] if row else None
        finally:
            conn.close()
    except Exception as e:
        log.debug("Compresr cache lookup failed: %s", e)
        return None


def expire_old_cache(ttl_days: int = CACHE_TTL_DAYS) -> int:
    """Delete ``compresr_cache`` rows older than ``ttl_days``.

    Wired into the APScheduler cron at 04:00 PT (idle hour, after the batch
    flush job but well before the morning cost-pull jobs). Returns the
    number of rows deleted so the caller can log a one-line summary.
    Best-effort — silently returns 0 when DATABASE_URL is unset or the
    DELETE fails, so a transient DB outage cannot crash the scheduler.

    Audit reference: docs/proposals/compresr-audit-2026-05-11.md §2 noted
    that the 7-day TTL was documented but unenforced.
    """
    try:
        import db_adapter

        if not db_adapter.DATABASE_URL:
            return 0
        conn = db_adapter._connect()
        try:
            with conn.cursor() as cur:
                # See _cache_lookup for the INTERVAL-binding pattern. We can't
                # interpolate the days literal into the INTERVAL string itself.
                cur.execute(
                    "DELETE FROM compresr_cache "
                    "WHERE created_at < NOW() - INTERVAL '1 day' * %s",
                    (ttl_days,),
                )
                deleted = cur.rowcount or 0
                conn.commit()
                if deleted:
                    log.info(
                        "Compresr cache expiry: deleted %d row(s) older than %d days",
                        deleted,
                        ttl_days,
                    )
                return deleted
        finally:
            conn.close()
    except Exception as e:
        log.debug("Compresr cache expiry failed: %s", e)
        return 0


def _cache_put(
    content_hash: str,
    model: str,
    compressed_text: str,
    input_chars: int,
    compressed_chars: int,
) -> None:
    """Persist (content_hash, model) -> compressed_text. Best-effort."""
    try:
        import db_adapter

        if not db_adapter.DATABASE_URL:
            return
        conn = db_adapter._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO compresr_cache "
                    "(content_hash, model, compressed_text, input_chars, compressed_chars) "
                    "VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT (content_hash, model) DO NOTHING",
                    (
                        content_hash,
                        model,
                        compressed_text,
                        input_chars,
                        compressed_chars,
                    ),
                )
                conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.debug("Compresr cache write failed: %s", e)


def _record_call(
    *,
    call_site: str,
    model: str,
    content_hash: Optional[str],
    input_chars: int,
    compressed_chars: int,
    compression_ratio: Optional[float],
    latency_ms: int,
    fallback: bool,
    fallback_reason: Optional[str],
    query_present: bool = False,
    cache_hit: bool = False,
) -> None:
    """Insert one row into compresr_calls. Best-effort — never raises.

    ``query_present`` flags whether a non-None ``query`` was passed (i.e. the
    call used latte_v1 query-aware compression). ``cache_hit`` is set when the
    compressed text came from ``compresr_cache`` rather than the SDK. Both are
    promoted out of ``fallback_reason`` so the telemetry aggregations in
    ``compresr_telemetry.py`` can query indexed boolean columns directly.
    """
    try:
        import db_adapter

        if not db_adapter.DATABASE_URL:
            return
        conn = db_adapter._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO compresr_calls "
                    "(call_site, model, content_hash, input_chars, compressed_chars, "
                    " compression_ratio, latency_ms, fallback, fallback_reason, "
                    " query_present, cache_hit) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
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
                    ),
                )
                conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.debug("Compresr telemetry write failed: %s", e)


# ---------------------------------------------------------------------------
# Compresr SDK invocation
# ---------------------------------------------------------------------------


def _call_compresr(text: str, *, model: str, query: Optional[str]) -> str:
    """Call the Compresr SDK; return compressed text or raise on any failure.

    Separated from compress_prompt for easy mocking. Imports the SDK lazily so
    the package being uninstalled is handled by the caller.
    """
    from compresr import CompressionClient  # type: ignore[import-not-found]

    import config

    client = CompressionClient(api_key=config.COMPRESR_API_KEY)
    kwargs = {"context": text, "compression_model_name": model}
    if query is not None and model == "latte_v1":
        kwargs["query"] = query
    result = client.compress(**kwargs)
    # SDK returns result.data.compressed_context per the verified shape.
    return result.data.compressed_context


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def compress_prompt(
    text: str,
    *,
    model: str = "espresso_v1",
    query: Optional[str] = None,
    call_site: str,
    min_chars: int = 0,
) -> str:
    """Compress `text` via Compresr, falling back to the original on any failure.

    Args:
      text: The prompt text to compress.
      model: "espresso_v1" (general-purpose) or "latte_v1" (query-aware).
      query: Required for "latte_v1"; ignored for "espresso_v1".
      call_site: One of "self_heal", "self_improve", "adhoc_kickoff". Used for
        env-flag gating, telemetry, and the circuit breaker.
      min_chars: Skip compression entirely if len(text) < min_chars (overhead
        would exceed benefit). Counted as a fallback in telemetry with reason
        "below_min_chars".

    Returns:
      Compressed text on success, otherwise the original `text` unchanged.
      Never raises — compression failure must never break the calling code.
    """
    global _WARNED_SDK_MISSING

    input_chars = len(text)
    query_present = query is not None
    started_ns = time.monotonic_ns()

    def _elapsed_ms() -> int:
        return max(0, int((time.monotonic_ns() - started_ns) / 1_000_000))

    def _fallback(reason: str, *, count_failure: bool = False) -> str:
        if count_failure:
            _record_failure(call_site)
        _record_call(
            call_site=call_site,
            model=model,
            content_hash=None,
            input_chars=input_chars,
            compressed_chars=input_chars,
            compression_ratio=1.0,
            latency_ms=_elapsed_ms(),
            fallback=True,
            fallback_reason=reason,
            query_present=query_present,
            cache_hit=False,
        )
        return text

    # 1. Gate on env flag + key.
    if not _is_site_enabled(call_site):
        if call_site not in _WARNED_NO_KEY:
            log.info(
                "Compresr disabled for call_site=%s (key unset or flag off); "
                "falling back to original text.",
                call_site,
            )
            _WARNED_NO_KEY.add(call_site)
        return _fallback("disabled")

    # 1b. Honor the per-site regression-guard auto-disable flag (Plan #37
    # task #67). When compresr_regression_guard.run_regression_check has
    # written a row to compresr_site_disabled for this call site (because the
    # trailing-24h JSON-parse failure rate exceeded 2x the 14-day baseline),
    # short-circuit to the original text. Import is lazy so the guard module
    # being unimportable can't break compression.
    try:
        from compresr_regression_guard import is_disabled as _site_disabled  # type: ignore
    except Exception:
        _site_disabled = None  # type: ignore
    if _site_disabled is not None and _site_disabled(call_site):
        return _fallback("regression_disabled")

    # 2. Skip if below the size threshold — overhead > benefit.
    if input_chars < min_chars:
        return _fallback("below_min_chars")

    # 3. Honor the circuit breaker.
    if is_circuit_open(call_site):
        return _fallback("circuit_open")

    # 4. Cache lookup. Hash binds model + query so two latte_v1 calls on the
    # same text with different queries don't collide (audit fix
    # docs/proposals/compresr-audit-2026-05-11.md §2). espresso_v1 ignores
    # query upstream, so the hash is identical regardless of whether one was
    # passed for that model — preserves cache hits on the espresso path.
    effective_query = query if model == "latte_v1" else None
    chash = _content_hash(text, model=model, query=effective_query)
    cached = _cache_lookup(chash, model)
    if cached is not None:
        compressed_chars = len(cached)
        ratio = (compressed_chars / input_chars) if input_chars else 1.0
        _record_call(
            call_site=call_site,
            model=model,
            content_hash=chash,
            input_chars=input_chars,
            compressed_chars=compressed_chars,
            compression_ratio=ratio,
            latency_ms=_elapsed_ms(),
            fallback=False,
            fallback_reason="cache_hit",
            query_present=query_present,
            cache_hit=True,
        )
        _record_success(call_site)
        return cached

    # 5. Call the SDK.
    try:
        compressed = _call_compresr(text, model=model, query=query)
    except ImportError as e:
        if not _WARNED_SDK_MISSING:
            log.warning(
                "Compresr SDK not installed (%s) — falling back. Install with "
                "`pip install 'compresr>=2.5,<3'`.",
                e,
            )
            _WARNED_SDK_MISSING = True
        return _fallback("sdk_not_installed")
    except Exception as e:
        log.warning(
            "Compresr SDK call failed for call_site=%s: %s — falling back.",
            call_site,
            e,
        )
        return _fallback(f"sdk_error:{type(e).__name__}", count_failure=True)

    # 6. Sanity-check the result. Defensive: an empty or longer result is a
    # regression and should fall back rather than hurt downstream prompts.
    if not isinstance(compressed, str) or not compressed:
        return _fallback("empty_result", count_failure=True)
    if len(compressed) >= input_chars:
        return _fallback("no_compression_benefit")

    # 7. Success — persist to cache, record telemetry, reset failure counter.
    compressed_chars = len(compressed)
    ratio = compressed_chars / input_chars
    _cache_put(chash, model, compressed, input_chars, compressed_chars)
    _record_call(
        call_site=call_site,
        model=model,
        content_hash=chash,
        input_chars=input_chars,
        compressed_chars=compressed_chars,
        compression_ratio=ratio,
        latency_ms=_elapsed_ms(),
        fallback=False,
        fallback_reason=None,
        query_present=query_present,
        cache_hit=False,
    )
    _record_success(call_site)
    return compressed
