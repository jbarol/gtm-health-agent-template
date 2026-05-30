"""Plan #36 — thin wrapper over the Anthropic Message Batches API.

Provides a DB-backed submit/poll/recover lifecycle for non-interactive Messages
API calls that tolerate ≥1 minute of latency. Batch-tier requests are billed at
50% of the realtime input/output rates (cache rates unchanged per the Batches
API docs).

Today's call sites (wired by Tasks #51/#52, not here):
  - ``self_heal._analyze_session``  → Sonnet 4.6, post-session reviews.
  - ``self_improve._analyze_changes`` → Sonnet 4.6, nightly doc crawl.

Public surface:
  - ``submit_batch(call_site, model, requests) -> str | None``
  - ``poll_pending_batches(callback_registry) -> int``
  - ``recover_orphan_batches() -> int``

When ``BATCH_PROCESSING_ENABLED`` is False, ``submit_batch`` returns ``None``
immediately so the caller falls back to the realtime path.

Cost telemetry (Plan #36 task #55): when a batch result is retrieved, the
per-request usage object is forwarded to
``cost_collector.track_messages_call(..., tier="batch", batch_id=batch_id)``.
This writes one row per completed request to ``messages_api_calls`` with the
batch-tier multiplier applied, so the reconciliation surfaces in Plan #35 can
split batch vs realtime spend without further plumbing.

The per-request ``call_site`` recorded against the cost ledger defaults to the
batch-level ``call_site`` (e.g. ``"self_heal"``). Callers that want finer
attribution (e.g. ``"self_heal._analyze_session"``) can override on a
per-request basis by including ``"call_site"`` in the request's ``context``
dict — that value wins at dispatch time.

See ``docs/plans/36-batch-processing.md`` for the full design rationale.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Optional, cast

import anthropic
import cost_collector
import db_adapter
from config import ANTHROPIC_API_KEY, BATCH_PROCESSING_ENABLED

log = logging.getLogger(__name__)

# Batch tier pricing: flat 50% off list per the Anthropic Batches API docs
# (https://platform.claude.com/docs/en/build-with-claude/batch-processing).
# Both fresh input and cache reads/writes get the same discount.
BATCH_RATE_MULTIPLIER = 0.5

# Cache TTL for batched Messages API requests. The Anthropic default is 5m,
# which can expire mid-batch (Batches API takes up to 24h to complete; most
# finish within an hour). The docs explicitly recommend the 1h variant for
# batches — see https://platform.claude.com/docs/en/build-with-claude/batch-processing.
#
# Bumped from the implicit 5m default to 1h (Task #54 of Plan #36) because:
#   1. Batch result lookups are repeated across the same payload within short
#      windows — e.g. self_improve nightly crawls hit near-identical doc pages.
#      A 1h TTL meaningfully reduces cache-write rewrites on the next pull.
#   2. The 5m TTL routinely expires before a batch completes, forcing the
#      system prompt to be re-billed as a fresh input on the next request.
#   3. 1h TTL costs 2× the 5m TTL on cache_write but is read at the same rate;
#      breakeven is one extra cache hit per write, which both call sites
#      clear easily (self_heal: ~30 reviews/day on a stable system prompt;
#      self_improve: nightly on a near-identical payload).
BATCH_CACHE_TTL = "1h"

# Realtime rates per $/MTOK. Duplicated from session_runner.MODEL_COSTS_PER_MTOK
# per Plan #36 — Plan #35 will consolidate. Keep them in sync until then.
REALTIME_RATES_PER_MTOK = {
    # Opus 4.5–4.8 share $5/$25 list pricing (verified 2026-05-29 vs
    # platform.claude.com). opus-4-7 corrected from stale $15/$75 (Opus-4/4.1).
    "claude-opus-4-8": {
        "input": 5.0,
        "output": 25.0,
        "cache_write_5m": 6.25,
        "cache_write_1h": 10.0,
        "cache_read": 0.5,
    },
    "claude-opus-4-7": {
        "input": 5.0,
        "output": 25.0,
        "cache_write_5m": 6.25,
        "cache_write_1h": 10.0,
        "cache_read": 0.5,
    },
    "claude-sonnet-4-6": {
        "input": 3.0,
        "output": 15.0,
        "cache_write_5m": 3.75,
        "cache_write_1h": 6.0,
        "cache_read": 0.3,
    },
    "claude-haiku-4-5": {
        "input": 0.80,
        "output": 4.0,
        "cache_write_5m": 1.0,
        "cache_write_1h": 1.6,
        "cache_read": 0.08,
    },
}


client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ── Cost helpers ────────────────────────────────────────────────────────────


def _batch_cost_for_usage(usage: dict, model: str) -> float:
    """Compute batch-tier cost in dollars from a per-request usage dict.

    Accepts a plain dict (not the SDK object) since results come back via the
    JSONL stream as JSON-decoded blobs. Missing fields are treated as 0.
    Unknown models fall back to Sonnet 4.6 rates and log a warning.
    """
    rates = REALTIME_RATES_PER_MTOK.get(model)
    if rates is None:
        log.warning(
            f"No batch rates for model {model!r}; falling back to claude-sonnet-4-6"
        )
        rates = REALTIME_RATES_PER_MTOK["claude-sonnet-4-6"]

    def _g(key: str) -> int:
        return int(usage.get(key) or 0)

    input_tok = _g("input_tokens")
    output_tok = _g("output_tokens")
    cache_read = _g("cache_read_input_tokens")
    cache_write = _g("cache_creation_input_tokens")
    # Messages API returns a single scalar for cache writes; price at 5m rate
    # (matches _messages_usage convention).
    cost = (
        input_tok * rates["input"]
        + output_tok * rates["output"]
        + cache_read * rates["cache_read"]
        + cache_write * rates["cache_write_5m"]
    ) / 1_000_000
    return cost * BATCH_RATE_MULTIPLIER


def _usage_to_dict(usage) -> dict:
    """Normalize SDK / JSON usage objects to a plain dict.

    Result objects come from the JSONL stream and may be either a dict (raw
    JSON) or an SDK-wrapped object depending on Anthropic SDK version. Handle
    both shapes so the cost path stays simple.
    """
    if usage is None:
        return {}
    if isinstance(usage, dict):
        return usage
    return {
        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", 0)
        or 0,
    }


# ── DB persistence helpers ──────────────────────────────────────────────────


def _record_submission(
    batch_id: str,
    call_site: str,
    model: str,
    requests: list[dict],
    callback_name: str,
):
    """Insert one ``batch_jobs`` row + one ``batch_job_requests`` per request.

    Falls back to a debug log if Postgres is unavailable — the orchestrator's
    DB layer treats missing ``DATABASE_URL`` as non-fatal everywhere else.
    """
    if not db_adapter.DATABASE_URL:
        log.debug(f"DATABASE_URL unset; skipping persistence for batch {batch_id}")
        return
    conn = db_adapter._connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO batch_jobs "
                "(batch_id, call_site, model, request_count, status) "
                "VALUES (%s, %s, %s, %s, 'submitted')",
                (batch_id, call_site, model, len(requests)),
            )
            for req in requests:
                context = req.get("context") or {}
                cur.execute(
                    "INSERT INTO batch_job_requests "
                    "(batch_id, request_id, callback_name, context_json, status) "
                    "VALUES (%s, %s, %s, %s::jsonb, 'pending')",
                    (
                        batch_id,
                        req["custom_id"],
                        callback_name,
                        json.dumps(context),
                    ),
                )
            conn.commit()
    finally:
        conn.close()


def _list_submitted_batch_ids() -> list[dict]:
    """Return rows for every batch_jobs row still in status='submitted'."""
    if not db_adapter.DATABASE_URL:
        return []
    import psycopg2.extras

    conn = db_adapter._connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT batch_id, call_site, model FROM batch_jobs "
                "WHERE status = 'submitted' ORDER BY submitted_at ASC"
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _fetch_pending_requests(batch_id: str) -> list[dict]:
    """Load (request_id, callback_name, context_json) for a batch."""
    if not db_adapter.DATABASE_URL:
        return []
    import psycopg2.extras

    conn = db_adapter._connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT request_id, callback_name, context_json "
                "FROM batch_job_requests WHERE batch_id = %s",
                (batch_id,),
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _mark_request_complete(
    batch_id: str,
    request_id: str,
    result_text: str,
    result_usage: dict,
    status: str,
):
    if not db_adapter.DATABASE_URL:
        return
    conn = db_adapter._connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE batch_job_requests SET "
                "result_text = %s, result_usage = %s::jsonb, status = %s, "
                "completed_at = NOW() WHERE batch_id = %s AND request_id = %s",
                (
                    result_text,
                    json.dumps(result_usage or {}),
                    status,
                    batch_id,
                    request_id,
                ),
            )
            conn.commit()
    finally:
        conn.close()


def _mark_batch_status(batch_id: str, status: str, error_message: Optional[str] = None):
    if not db_adapter.DATABASE_URL:
        return
    conn = db_adapter._connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE batch_jobs SET status = %s, ended_at = NOW(), "
                "error_message = %s WHERE batch_id = %s",
                (status, error_message, batch_id),
            )
            conn.commit()
    finally:
        conn.close()


def _log_batch_cost(
    call_site: str, model: str, batch_id: str, usage: dict, _cost_usd: float = 0.0
):
    """Forward a completed batch request to ``cost_collector.track_messages_call``.

    Plan #36 task #55: instead of duplicating the ``messages_api_calls`` insert
    inside this module, delegate to the canonical cost-ledger helper. That
    keeps the batch-tier multiplier (input/output halved, cache unchanged)
    consistent with realtime tracking and lets Plan #35's reconciliation
    surfaces split spend by ``tier``.

    The ``_cost_usd`` arg is retained for backward compatibility with callers
    that still pass it (notably the existing test suite) — the actual cost is
    re-computed inside ``track_messages_call`` from the usage object and the
    batch-tier multiplier, so this parameter is intentionally ignored.

    Usage shape: the dispatcher already normalized the result's ``usage`` block
    to a plain dict via ``_usage_to_dict``. ``track_messages_call`` reads token
    counts via ``getattr``, which works on both objects and dicts (``getattr``
    on a dict returns the default since dicts don't expose token attrs as
    attributes), so wrap the dict in a ``SimpleNamespace`` before forwarding.
    """
    from types import SimpleNamespace

    usage_obj = SimpleNamespace(
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cache_read_input_tokens=int(usage.get("cache_read_input_tokens") or 0),
        cache_creation_input_tokens=int(usage.get("cache_creation_input_tokens") or 0),
    )
    cost_collector.track_messages_call(
        call_site=call_site,
        model=model,
        usage=usage_obj,
        tier="batch",
        batch_id=batch_id,
    )


# ── Public API ──────────────────────────────────────────────────────────────


def submit_batch(
    call_site: str,
    model: str,
    requests: list[dict],
    callback_name: Optional[str] = None,
) -> Optional[str]:
    """Submit a batch of Messages API requests via ``client.messages.batches.create``.

    Args:
      call_site: e.g. ``"self_heal"`` or ``"self_improve"``. Logged + persisted
        for cost-attribution. Also used as the default callback registry key.
      model: model name, used for batch-tier cost computation at completion.
      requests: list of ``{"custom_id": str, "params": {...messages.create kwargs}}``
        dicts. May also include an optional ``"context": {...}`` field per
        request — that JSON is round-tripped to the completion callback so the
        caller doesn't have to keep its own correlation map.
      callback_name: registry key for the completion handler. Defaults to
        ``call_site`` when not provided.

    Returns the Anthropic batch_id on success, or ``None`` if
    ``BATCH_PROCESSING_ENABLED`` is False (caller should fall back to realtime)
    or if ``requests`` is empty.
    """
    if not BATCH_PROCESSING_ENABLED:
        log.info(
            f"BATCH_PROCESSING_ENABLED=false; {call_site} submit_batch returning None"
        )
        return None
    if not requests:
        log.info(f"{call_site} submit_batch called with empty requests; skipping")
        return None

    cb_name = callback_name or call_site
    # Strip our locally-introduced "context" field before sending to Anthropic.
    api_requests = [
        {"custom_id": r["custom_id"], "params": r["params"]} for r in requests
    ]

    try:
        batch = client.messages.batches.create(requests=cast(Any, api_requests))
    except Exception:
        log.exception(f"{call_site} batch create failed")
        return None

    batch_id = getattr(batch, "id", None)
    if not batch_id:
        log.error(f"{call_site} batch create returned no id; got {batch!r}")
        return None

    try:
        _record_submission(batch_id, call_site, model, requests, cb_name)
    except Exception:
        log.exception(
            f"{call_site} failed to persist batch {batch_id}; "
            "Anthropic-side batch still queued"
        )

    log.info(f"{call_site} submitted batch {batch_id} with {len(requests)} request(s)")
    return batch_id


def poll_pending_batches(callback_registry: dict[str, Callable]) -> int:
    """Poll every ``status='submitted'`` batch; dispatch completions.

    Args:
      callback_registry: maps callback_name -> a callable with signature
        ``callback(request_id, context_json, result_text, result_usage)``.
        Unknown callbacks are logged and the row is still marked complete.

    Returns the number of batches transitioned to ``ended`` or ``failed``.
    Errors on any single batch are caught and logged — the poller never
    crashes the caller (typically an APScheduler interval job).
    """
    rows = _list_submitted_batch_ids()
    if not rows:
        return 0

    completed = 0
    for row in rows:
        batch_id = row["batch_id"]
        call_site = row["call_site"]
        model = row["model"]
        try:
            batch = client.messages.batches.retrieve(batch_id)
        except Exception:
            log.exception(f"Retrieve failed for batch {batch_id}; will retry next poll")
            # Don't mark failed — transient network errors should be retried.
            continue

        status = getattr(batch, "processing_status", None)
        if status != "ended":
            log.debug(f"batch {batch_id} still {status}; skipping")
            continue

        # Wrap the rest of the per-batch work so one bad row (transient DB
        # hiccup, missing batch_job_requests row, dispatch exception) does NOT
        # crash poll_pending_batches and trigger the scheduler's catch-all
        # Slack watch notice. Plan #52 PR-Y: per-batch isolation.
        try:
            # Pull pending request context map before iterating results.
            pending_rows = _fetch_pending_requests(batch_id)
            context_map = {r["request_id"]: r for r in pending_rows}

            try:
                results = client.messages.batches.results(batch_id)
            except Exception:
                log.exception(f"Results fetch failed for batch {batch_id}")
                _mark_batch_status(batch_id, "failed", "results fetch failed")
                completed += 1
                continue

            try:
                _dispatch_results(
                    batch_id=batch_id,
                    call_site=call_site,
                    model=model,
                    results=results,
                    context_map=context_map,
                    callback_registry=callback_registry,
                )
                _mark_batch_status(batch_id, "ended")
            except Exception as e:
                log.exception(f"Dispatch failed for batch {batch_id}")
                _mark_batch_status(batch_id, "failed", str(e)[:500])
            completed += 1
        except Exception:
            # Catch-all for the per-batch loop body — typically a DB error in
            # _fetch_pending_requests. Log and move on; the next poll retries.
            # Do NOT increment completed and do NOT touch the row's status.
            log.exception(
                f"Per-batch processing failed for batch {batch_id}; will retry next poll"
            )
            continue

    return completed


def recover_orphan_batches() -> int:
    """Reconcile prior-container batches against Anthropic's view.

    On container startup we may have rows still marked ``submitted`` whose
    Anthropic-side batches have actually finished (or failed) while we were
    down. For each one, retrieve current state and update locally. If the
    batch is ``ended`` we DO NOT auto-dispatch — the caller is expected to
    invoke ``poll_pending_batches`` immediately after, which handles
    end-to-end dispatch with the registered callbacks.

    Returns the count of rows whose local status changed.
    """
    rows = _list_submitted_batch_ids()
    if not rows:
        return 0

    recovered = 0
    for row in rows:
        batch_id = row["batch_id"]
        try:
            batch = client.messages.batches.retrieve(batch_id)
        except Exception:
            log.exception(f"Recovery retrieve failed for batch {batch_id}")
            continue

        status = getattr(batch, "processing_status", None)
        if status == "ended":
            # Leave as 'submitted' so poll_pending_batches picks it up and
            # runs the callback path. We only count it as "recovered" in the
            # sense that we confirmed liveness; status doesn't change here.
            log.info(
                f"Recovered batch {batch_id}: Anthropic says ended, will dispatch on next poll"
            )
            recovered += 1
        elif status in ("canceling", "canceled"):
            _mark_batch_status(batch_id, "failed", f"anthropic status={status}")
            recovered += 1
        elif status == "in_progress":
            log.info(f"Batch {batch_id} still in_progress on Anthropic side")

    return recovered


# ── Internals ──────────────────────────────────────────────────────────────


def _dispatch_results(
    batch_id: str,
    call_site: str,
    model: str,
    results,
    context_map: dict,
    callback_registry: dict[str, Callable],
):
    """Iterate the JSONL result stream, dispatch each to its callback.

    ``results`` may be an iterable of SDK result objects or plain dicts. We
    normalize both before extracting fields.
    """
    for result in results:
        custom_id = _attr(result, "custom_id")
        if not custom_id:
            log.warning(f"batch {batch_id}: result missing custom_id, skipping")
            continue

        result_inner = _attr(result, "result")
        result_type = _attr(result_inner, "type") or "errored"

        message = _attr(result_inner, "message")
        text = _extract_text(message)
        usage = _usage_to_dict(_attr(message, "usage"))

        row = context_map.get(custom_id, {})
        callback_name = row.get("callback_name") or call_site
        context = row.get("context_json") or {}
        if isinstance(context, str):
            try:
                context = json.loads(context)
            except Exception:
                context = {}

        # Cost is only logged for succeeded results — errored/canceled/expired
        # are not billed per Batches API docs. Per-request ``call_site`` (when
        # supplied in the request context) overrides the batch-level value so
        # callers like ``self_heal`` can attribute cost to the actual function
        # (e.g. ``self_heal._analyze_session``) rather than the buffer-flush
        # call-site (``self_heal``).
        if result_type == "succeeded":
            ledger_call_site = (
                context.get("call_site") if isinstance(context, dict) else None
            ) or call_site
            _log_batch_cost(ledger_call_site, model, batch_id, usage)

        callback = callback_registry.get(callback_name)
        if callback is None:
            log.warning(
                f"batch {batch_id}: no callback registered for {callback_name!r}; "
                f"request {custom_id} marked complete without dispatch"
            )
        else:
            try:
                callback(custom_id, context, text, usage)
            except Exception:
                log.exception(
                    f"callback {callback_name} raised for "
                    f"batch={batch_id} request={custom_id}"
                )

        _mark_request_complete(batch_id, custom_id, text, usage, result_type)


def _attr(obj, key):
    """Read a field from either a dict or an SDK object. Returns None on miss."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _extract_text(message) -> str:
    """Concatenate every text block out of a batch result's message.content."""
    if message is None:
        return ""
    content = _attr(message, "content") or []
    parts = []
    for block in content:
        if _attr(block, "type") == "text":
            t = _attr(block, "text") or ""
            if t:
                parts.append(t)
    return "".join(parts)
