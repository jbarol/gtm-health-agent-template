"""Cost collector — bridges Anthropic Admin API to the local cost ledger.

Plan #35 (docs/plans/35-cost-tracking-and-reporting.md) describes a two-ledger
architecture:

  * ``session_costs``      — fine-grained, attribution-rich (per-session, per-portco,
                              per-thread, per-user). Written by
                              ``session_runner._log_session_usage``.
  * ``messages_api_calls`` — parallel ledger for non-session Messages API traffic
                              (``self_heal``, ``self_improve``). Written by the
                              ``track_messages_call`` helper below.
  * ``anthropic_daily_costs`` — ground-truth daily rollup pulled from Anthropic's
                                Admin Usage & Cost API. Written by the
                                ``pull_anthropic_daily_costs`` cron in this module.

The Admin API cannot break down by portco/task/thread — that attribution lives
only in the local ledgers. Reconciliation compares local sums to the
Anthropic-billed daily total per model. Drift > 10% / 25% triggers Slack alerts
(thresholds enforced by the caller, not here).

Public API:
    pull_anthropic_daily_costs(days_back=3)   — cron job entrypoint
    track_messages_call(call_site, model, usage, *, tier="realtime", batch_id=None)
    compute_reconciliation(date)              — drift math for /cost reconcile
    reconcile_daily(target_date=None)         — drift gates + Slack alerting (task #42)

This module is intentionally a thin wrapper: it does not own session-side
attribution (that's Task #35). It owns the Admin-API side and the non-session
Messages bookkeeping.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

import config
import db_adapter

log = logging.getLogger(__name__)

# Pricing in $/MTOK. Mirrors session_runner.MODEL_COSTS_PER_MTOK and
# _messages_usage.MODEL_COSTS_PER_MTOK — duplicated for the same circular-import
# reason. When Plan #35 consolidates the table into a shared module, this copy
# should go away.
#
# track_messages_call only needs the four Messages-API categories (input,
# output, cache_read, single cache_write at the 5m rate — the Messages API
# does not return a TTL split unless the 1-hour beta header is set, which
# neither self_heal nor self_improve does).
MODEL_COSTS_PER_MTOK = {
    # Opus 4.5–4.8 share $5/$25 list pricing (verified 2026-05-29 vs
    # platform.claude.com). opus-4-7 corrected from stale $15/$75 (Opus-4/4.1).
    "claude-opus-4-8": {
        "input": 5.0,
        "output": 25.0,
        "cache_write_5m": 6.25,
        "cache_read": 0.5,
    },
    "claude-opus-4-7": {
        "input": 5.0,
        "output": 25.0,
        "cache_write_5m": 6.25,
        "cache_read": 0.5,
    },
    "claude-sonnet-4-6": {
        "input": 3.0,
        "output": 15.0,
        "cache_write_5m": 3.75,
        "cache_read": 0.3,
    },
    "claude-haiku-4-5": {
        "input": 0.80,
        "output": 4.0,
        "cache_write_5m": 1.0,
        "cache_read": 0.08,
    },
}

# Anthropic Batches API discount: input + output are billed at half rate.
# Per https://platform.claude.com/docs/en/build-with-claude/batch-processing.
# Cache read/write rates are unchanged (cache is still cache).
_BATCH_TIER_MULTIPLIER = 0.5

ADMIN_API_BASE = "https://api.anthropic.com"
ADMIN_API_VERSION = "2023-06-01"
USER_AGENT = "gtm-health-agent/1.0 (+https://github.com/your-org/gtm-health-agent)"


# ──────────────────────────────────────────────────────────────────────────
# track_messages_call — Messages API per-call ledger
# ──────────────────────────────────────────────────────────────────────────


def _extract_messages_usage(usage: Any) -> dict:
    """Pull the four token categories out of a Messages-API ``usage`` object.

    Messages API has a SINGLE ``cache_creation_input_tokens`` scalar (no nested
    5m/1h object — that exists only on the Managed Agents session API). Missing
    or ``None`` values coerce to 0.
    """
    if usage is None:
        return {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    return {
        "input": getattr(usage, "input_tokens", 0) or 0,
        "output": getattr(usage, "output_tokens", 0) or 0,
        "cache_read": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "cache_write": getattr(usage, "cache_creation_input_tokens", 0) or 0,
    }


def _estimate_messages_cost(usage: Any, model: str, tier: str) -> float:
    """Estimate cost in USD for one Messages API call.

    Batch tier halves the input + output rates (cache rates unchanged). Unknown
    models return 0 with a warning rather than crashing — keeps logging non-fatal
    when Anthropic ships a new model name before the rate table is updated.
    """
    rates = MODEL_COSTS_PER_MTOK.get(model)
    if rates is None:
        log.warning("No cost rates for model %r; reporting cost=$0.0000", model)
        return 0.0
    u = _extract_messages_usage(usage)
    mult = _BATCH_TIER_MULTIPLIER if tier == "batch" else 1.0
    return (
        u["input"] * rates["input"] * mult / 1_000_000
        + u["output"] * rates["output"] * mult / 1_000_000
        + u["cache_read"] * rates["cache_read"] / 1_000_000
        + u["cache_write"] * rates["cache_write_5m"] / 1_000_000
    )


def _extract_session_usage(usage: Any) -> dict:
    """Pull Messages-API-shaped token counts out of a Managed Agents session
    usage object.

    Sessions API returns ``cache_creation`` as a nested object with
    ``ephemeral_5m_input_tokens`` + ``ephemeral_1h_input_tokens`` (the TTL
    split), whereas the Messages API returns a single
    ``cache_creation_input_tokens`` scalar. This helper flattens the session
    shape onto the Messages-API surface so ``messages_api_calls`` (which has
    a single ``cache_write_tokens`` column) can swallow both.

    Returns the same {"input", "output", "cache_read", "cache_write"} dict
    that ``_extract_messages_usage`` produces — same downstream contract.
    """
    if usage is None:
        return {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    # If this is a Messages-API usage object it has the scalar field;
    # short-circuit through the existing helper.
    if hasattr(usage, "cache_creation_input_tokens"):
        return _extract_messages_usage(usage)
    cw5, cw1 = 0, 0
    cc = getattr(usage, "cache_creation", None)
    if cc is not None:
        cw5 = getattr(cc, "ephemeral_5m_input_tokens", 0) or 0
        cw1 = getattr(cc, "ephemeral_1h_input_tokens", 0) or 0
    return {
        "input": getattr(usage, "input_tokens", 0) or 0,
        "output": getattr(usage, "output_tokens", 0) or 0,
        "cache_read": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "cache_write": cw5 + cw1,
    }


def _estimate_cost_from_parts(parts: dict, model: str, tier: str) -> float:
    """Cost math for an already-extracted token-parts dict.

    Pulled out of ``_estimate_messages_cost`` so the session-shape usage
    helper above can share the dollar math without re-extracting.
    """
    rates = MODEL_COSTS_PER_MTOK.get(model)
    if rates is None:
        log.warning("No cost rates for model %r; reporting cost=$0.0000", model)
        return 0.0
    mult = _BATCH_TIER_MULTIPLIER if tier == "batch" else 1.0
    return (
        parts["input"] * rates["input"] * mult / 1_000_000
        + parts["output"] * rates["output"] * mult / 1_000_000
        + parts["cache_read"] * rates["cache_read"] / 1_000_000
        + parts["cache_write"] * rates["cache_write_5m"] / 1_000_000
    )


def track_prompt_engineer_call(
    outcome: str,
    model: str,
    usage: Any,
    *,
    elapsed_s: float | None = None,
    portco_key: str | None = None,
) -> None:
    """Persist one Prompt Engineer preprocess call to ``messages_api_calls``.

    The PE preprocess path uses the Managed Agents Sessions API, not the
    Messages API, but it sits in the same observability bucket as
    ``self_heal`` / ``self_improve``: a single short-lived agent call
    whose cost we want to attribute back to a call site. We re-use
    ``messages_api_calls`` rather than spinning up a third table — adding
    a column would require a migration, which P2 explicitly does NOT
    authorize. Outcome is encoded into the ``call_site`` field as
    ``prompt_engineer_preprocess`` for the success path or
    ``prompt_engineer_preprocess:<reason>`` for each return-None branch.
    The ``portco_key`` and ``elapsed_s`` arguments are accepted for the
    log line but not persisted (no columns for them — see migration note
    in P2 plan).

    Args:
        outcome: ``"ok"`` or one of the return-None reasons emitted
            by ``main._preprocess_prompt`` (``"agent_unconfigured"``,
            ``"session_create_failed"``, ``"empty_text_parts"``,
            ``"session_error"``, ``"json_parse_failed"``,
            ``"invalid_schema"``, ``"exception"``).
        model: model name (always ``"claude-sonnet-4-6"`` for the PE today).
        usage: session usage object from
            ``client.beta.sessions.retrieve(session_id).usage``, or
            ``None`` when the call failed before usage was available.
        elapsed_s: wall-clock seconds from session creation to outcome,
            logged for ad-hoc forensics.
        portco_key: portco context for the log line.

    No-op when ``DATABASE_URL`` is unset. All errors are caught and logged
    so a DB outage cannot break the calling code (cost tracking is
    observability, not load-bearing — same contract as
    ``track_messages_call``).
    """
    call_site = (
        "prompt_engineer_preprocess"
        if outcome == "ok"
        else f"prompt_engineer_preprocess:{outcome}"
    )
    parts = _extract_session_usage(usage)
    cost = _estimate_cost_from_parts(parts, model, tier="realtime")
    log.info(
        "[PE_TRACK] outcome=%s portco=%s elapsed=%.2fs in=%d out=%d "
        "cache_read=%d cache_write=%d cost=$%.4f",
        outcome,
        portco_key or "(none)",
        elapsed_s if elapsed_s is not None else -1.0,
        parts["input"],
        parts["output"],
        parts["cache_read"],
        parts["cache_write"],
        cost,
    )
    if not db_adapter.DATABASE_URL:
        log.debug("track_prompt_engineer_call: DATABASE_URL unset, skipping persist")
        return
    try:
        conn = db_adapter._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO messages_api_calls "
                    "(call_site, model, input_tokens, output_tokens, "
                    "cache_read_tokens, cache_write_tokens, cost_usd, tier, batch_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        call_site,
                        model,
                        parts["input"],
                        parts["output"],
                        parts["cache_read"],
                        parts["cache_write"],
                        cost,
                        "realtime",
                        None,
                    ),
                )
                conn.commit()
        finally:
            conn.close()
    except Exception:
        log.exception(
            "track_prompt_engineer_call: failed to persist outcome=%s (model=%s)",
            outcome,
            model,
        )


def track_messages_call(
    call_site: str,
    model: str,
    usage: Any,
    *,
    tier: str = "realtime",
    batch_id: str | None = None,
) -> None:
    """Persist one Messages-API call to ``messages_api_calls``.

    Args:
        call_site: caller label (e.g. ``"self_heal"``, ``"self_improve"``).
        model: model name (e.g. ``"claude-sonnet-4-6"``).
        usage: ``response.usage`` from the Anthropic SDK call.
        tier: ``"realtime"`` (default) or ``"batch"``. When ``"batch"``, the
            input + output rates are halved.
        batch_id: optional Anthropic batch ID for cross-referencing the
            ``batch_jobs`` table (Plan #36).

    No-op when ``DATABASE_URL`` is unset. All errors are caught and logged so
    a DB outage cannot break the calling code (cost tracking is observability,
    not load-bearing).
    """
    if not db_adapter.DATABASE_URL:
        log.debug("track_messages_call: DATABASE_URL unset, skipping persist")
        return
    parts = _extract_messages_usage(usage)
    cost = _estimate_messages_cost(usage, model, tier)
    try:
        conn = db_adapter._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO messages_api_calls "
                    "(call_site, model, input_tokens, output_tokens, "
                    "cache_read_tokens, cache_write_tokens, cost_usd, tier, batch_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        call_site,
                        model,
                        parts["input"],
                        parts["output"],
                        parts["cache_read"],
                        parts["cache_write"],
                        cost,
                        tier,
                        batch_id,
                    ),
                )
                conn.commit()
        finally:
            conn.close()
    except Exception:
        log.exception(
            "track_messages_call: failed to persist %s call (model=%s, tier=%s)",
            call_site,
            model,
            tier,
        )


# ──────────────────────────────────────────────────────────────────────────
# pull_anthropic_daily_costs — Admin API daily pull
# ──────────────────────────────────────────────────────────────────────────


def _parse_amount_cents_to_usd(amount: str | float | int) -> float:
    """Convert the Admin API cost ``amount`` (decimal string in cents) to USD.

    The endpoint returns amounts as decimal strings in lowest currency units —
    e.g. ``"123.45"`` in USD represents $1.2345. Defensive against numeric
    inputs in case the SDK or shape changes.
    """
    try:
        return float(amount) / 100.0
    except (TypeError, ValueError):
        return 0.0


def _fetch_admin_paginated(
    path: str,
    params: dict,
    api_key: str,
    *,
    client: httpx.Client | None = None,
) -> list[dict]:
    """Walk all pages of an Admin API endpoint and return the merged ``data`` rows.

    Concatenates the ``data`` arrays across pages. Stops when ``has_more`` is
    false or absent. Caller is responsible for unpacking per-bucket ``results``.
    """
    url = f"{ADMIN_API_BASE}{path}"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": ADMIN_API_VERSION,
        "User-Agent": USER_AGENT,
    }
    all_rows: list[dict] = []
    page: str | None = None
    own_client = client is None
    c = client or httpx.Client(timeout=30.0)
    try:
        while True:
            q = dict(params)
            if page:
                q["page"] = page
            resp = c.get(url, headers=headers, params=q)
            resp.raise_for_status()
            body = resp.json()
            all_rows.extend(body.get("data") or [])
            if not body.get("has_more"):
                break
            page = body.get("next_page")
            if not page:
                break
    finally:
        if own_client:
            c.close()
    return all_rows


def _aggregate_usage_buckets(buckets: list[dict]) -> dict[tuple, dict]:
    """Collapse usage_report buckets into a (date, model, workspace, tier) → tokens map.

    Each bucket has a ``starting_at`` and a ``results`` list with one entry per
    (model, workspace_id, service_tier) tuple when grouped that way. The same
    (date, model, workspace, tier) key can appear once because we request 1d
    buckets, but we still sum defensively in case Anthropic ever splits within
    a day.
    """
    out: dict[tuple, dict] = {}
    for bucket in buckets:
        starting_at = bucket.get("starting_at")
        if not starting_at:
            continue
        bucket_date = starting_at[:10]  # "YYYY-MM-DDTHH:MM:SSZ" → "YYYY-MM-DD"
        for row in bucket.get("results") or []:
            model = row.get("model") or ""
            workspace_id = row.get("workspace_id") or ""
            service_tier = row.get("service_tier") or "standard"
            cache_creation = row.get("cache_creation") or {}
            cw_5m = cache_creation.get("ephemeral_5m_input_tokens") or 0
            cw_1h = cache_creation.get("ephemeral_1h_input_tokens") or 0
            key = (bucket_date, model, workspace_id, service_tier)
            acc = out.setdefault(
                key,
                {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                },
            )
            acc["input_tokens"] += row.get("uncached_input_tokens") or 0
            acc["output_tokens"] += row.get("output_tokens") or 0
            acc["cache_read_tokens"] += row.get("cache_read_input_tokens") or 0
            acc["cache_write_tokens"] += cw_5m + cw_1h
    return out


def _aggregate_cost_buckets(buckets: list[dict]) -> dict[tuple, float]:
    """Collapse cost_report buckets into a (date, model, workspace, tier) → USD map.

    When grouped by ``description``, the cost endpoint emits one row per
    (model, service_tier, token_type, context_window). We sum across token_type
    and context_window. Amount is a decimal string in cents (e.g. ``"123.45"``
    = $1.2345); we convert once here.
    """
    out: dict[tuple, float] = {}
    for bucket in buckets:
        starting_at = bucket.get("starting_at")
        if not starting_at:
            continue
        bucket_date = starting_at[:10]
        for row in bucket.get("results") or []:
            model = row.get("model") or ""
            workspace_id = row.get("workspace_id") or ""
            service_tier = row.get("service_tier") or "standard"
            usd = _parse_amount_cents_to_usd(row.get("amount", 0))
            key = (bucket_date, model, workspace_id, service_tier)
            out[key] = out.get(key, 0.0) + usd
    return out


def pull_anthropic_daily_costs(
    days_back: int = 3,
    *,
    client: httpx.Client | None = None,
) -> int:
    """Pull the trailing N days of cost & usage data from Anthropic Admin API.

    Hits both endpoints:
      * ``/v1/organizations/usage_report/messages`` — token breakdown
        (grouped by model, workspace_id, service_tier, bucket_width=1d).
      * ``/v1/organizations/cost_report`` — USD costs grouped by
        workspace_id + description (description parses to model + service_tier).

    Joins the two on ``(bucket_date, model, workspace_id, service_tier)`` and
    upserts one row per key into ``anthropic_daily_costs``. Idempotent — a
    daily re-run of the same window overwrites prior rows with the latest
    Anthropic-reported numbers (data can settle for up to 5 minutes after a
    request completes, so a 3-day default lookback catches late arrivals).

    Args:
        days_back: how many days back to pull (UTC). Default 3.
        client: optional httpx.Client for tests. When None, a new client is
            created and closed per call.

    Returns:
        Number of rows upserted (post-merge across both endpoints). Returns 0
        when ``ANTHROPIC_ADMIN_KEY`` is unset (degraded mode — log a warning
        and skip; the local ledger keeps working).
    """
    api_key = config.ANTHROPIC_ADMIN_KEY
    if not api_key:
        log.warning(
            "pull_anthropic_daily_costs: ANTHROPIC_ADMIN_KEY unset, "
            "skipping (degraded mode — reconciliation disabled)"
        )
        return 0
    if not db_adapter.DATABASE_URL:
        log.warning("pull_anthropic_daily_costs: DATABASE_URL unset, skipping persist")
        return 0

    now_utc = datetime.now(timezone.utc)
    # Floor to start-of-day UTC; Anthropic snaps each bucket to start-of-day.
    end = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days_back)
    iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")

    usage_params = {
        "starting_at": iso(start),
        "ending_at": iso(end),
        "bucket_width": "1d",
        "group_by[]": ["model", "workspace_id", "service_tier"],
        "limit": 31,
    }
    cost_params = {
        "starting_at": iso(start),
        "ending_at": iso(end),
        "bucket_width": "1d",
        "group_by[]": ["workspace_id", "description"],
        "limit": 31,
    }

    try:
        usage_buckets = _fetch_admin_paginated(
            "/v1/organizations/usage_report/messages",
            usage_params,
            api_key,
            client=client,
        )
        cost_buckets = _fetch_admin_paginated(
            "/v1/organizations/cost_report",
            cost_params,
            api_key,
            client=client,
        )
    except httpx.HTTPError:
        log.exception("pull_anthropic_daily_costs: Admin API request failed")
        return 0

    usage_by_key = _aggregate_usage_buckets(usage_buckets)
    cost_by_key = _aggregate_cost_buckets(cost_buckets)
    all_keys = set(usage_by_key) | set(cost_by_key)
    if not all_keys:
        log.info("pull_anthropic_daily_costs: no rows returned for window")
        return 0

    rows_written = 0
    try:
        conn = db_adapter._connect()
        try:
            with conn.cursor() as cur:
                for key in all_keys:
                    bucket_date, model, workspace_id, service_tier = key
                    tokens = usage_by_key.get(
                        key,
                        {
                            "input_tokens": 0,
                            "output_tokens": 0,
                            "cache_read_tokens": 0,
                            "cache_write_tokens": 0,
                        },
                    )
                    cost_usd = cost_by_key.get(key, 0.0)
                    cur.execute(
                        "INSERT INTO anthropic_daily_costs "
                        "(bucket_date, model, workspace_id, service_tier, "
                        "input_tokens, output_tokens, cache_read_tokens, "
                        "cache_write_tokens, cost_usd) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT (bucket_date, model, workspace_id, service_tier) "
                        "DO UPDATE SET "
                        "input_tokens = EXCLUDED.input_tokens, "
                        "output_tokens = EXCLUDED.output_tokens, "
                        "cache_read_tokens = EXCLUDED.cache_read_tokens, "
                        "cache_write_tokens = EXCLUDED.cache_write_tokens, "
                        "cost_usd = EXCLUDED.cost_usd, "
                        "pulled_at = NOW()",
                        (
                            bucket_date,
                            model,
                            workspace_id,
                            service_tier,
                            tokens["input_tokens"],
                            tokens["output_tokens"],
                            tokens["cache_read_tokens"],
                            tokens["cache_write_tokens"],
                            cost_usd,
                        ),
                    )
                    rows_written += 1
                conn.commit()
        finally:
            conn.close()
    except Exception:
        log.exception("pull_anthropic_daily_costs: DB upsert failed")
        return 0

    log.info(
        "pull_anthropic_daily_costs: upserted %d rows for window %s → %s",
        rows_written,
        iso(start),
        iso(end),
    )
    return rows_written


# ──────────────────────────────────────────────────────────────────────────
# compute_reconciliation — drift math for /cost reconcile and daily digest
# ──────────────────────────────────────────────────────────────────────────


def compute_reconciliation(target_date: str | date) -> dict:
    """Compare locally-estimated cost vs. Anthropic-billed cost for one day.

    Local total = SUM(cost_usd) over session_costs.recorded_at::date == target
                + SUM(cost_usd) over messages_api_calls.recorded_at::date == target.
    Anthropic total = SUM(cost_usd) over anthropic_daily_costs WHERE bucket_date == target.

    Drift convention (matches Plan #35):
        drift_usd = anthropic_total - local_total
        drift_pct = (anthropic_total - local_total) / anthropic_total

    When Anthropic total is 0 (no billing pulled yet, or the day truly had no
    spend), ``drift_pct`` is ``None`` to signal undefined — callers should
    suppress alerts in that case rather than divide by zero.

    Returns a dict with::

        {
            "date": "YYYY-MM-DD",
            "local_total_usd": float,
            "anthropic_total_usd": float,
            "drift_usd": float,
            "drift_pct": float | None,
            "by_model": {model: {"local": float, "anthropic": float}, ...},
        }

    When DATABASE_URL is unset, returns zeros with ``drift_pct = None`` rather
    than raising — keeps slash-command handlers / digest renderers degraded
    gracefully.
    """
    if isinstance(target_date, date):
        date_str = target_date.isoformat()
    else:
        date_str = str(target_date)

    empty: dict = {
        "date": date_str,
        "local_total_usd": 0.0,
        "anthropic_total_usd": 0.0,
        "drift_usd": 0.0,
        "drift_pct": None,
        "by_model": {},
    }
    if not db_adapter.DATABASE_URL:
        return empty

    try:
        conn = db_adapter._connect()
        try:
            with conn.cursor() as cur:
                # Local: sessions
                cur.execute(
                    "SELECT model, COALESCE(SUM(cost_usd), 0)::float AS cost "
                    "FROM session_costs "
                    "WHERE recorded_at::date = %s "
                    "GROUP BY model",
                    (date_str,),
                )
                session_rows = cur.fetchall()
                # Local: messages-api
                cur.execute(
                    "SELECT model, COALESCE(SUM(cost_usd), 0)::float AS cost "
                    "FROM messages_api_calls "
                    "WHERE recorded_at::date = %s "
                    "GROUP BY model",
                    (date_str,),
                )
                msg_rows = cur.fetchall()
                # Anthropic ground truth
                cur.execute(
                    "SELECT model, COALESCE(SUM(cost_usd), 0)::float AS cost "
                    "FROM anthropic_daily_costs "
                    "WHERE bucket_date = %s "
                    "GROUP BY model",
                    (date_str,),
                )
                anthropic_rows = cur.fetchall()
        finally:
            conn.close()
    except Exception:
        log.exception("compute_reconciliation: DB query failed")
        return empty

    by_model: dict[str, dict[str, float]] = {}
    for model, cost in session_rows + msg_rows:
        m = model or "unknown"
        by_model.setdefault(m, {"local": 0.0, "anthropic": 0.0})
        by_model[m]["local"] += float(cost or 0)
    for model, cost in anthropic_rows:
        m = model or "unknown"
        by_model.setdefault(m, {"local": 0.0, "anthropic": 0.0})
        by_model[m]["anthropic"] += float(cost or 0)

    local_total = sum(b["local"] for b in by_model.values())
    anthropic_total = sum(b["anthropic"] for b in by_model.values())
    drift_usd = anthropic_total - local_total
    drift_pct: float | None
    if anthropic_total > 0:
        drift_pct = drift_usd / anthropic_total
    else:
        drift_pct = None

    return {
        "date": date_str,
        "local_total_usd": round(local_total, 6),
        "anthropic_total_usd": round(anthropic_total, 6),
        "drift_usd": round(drift_usd, 6),
        "drift_pct": (round(drift_pct, 4) if drift_pct is not None else None),
        "by_model": {
            m: {"local": round(v["local"], 6), "anthropic": round(v["anthropic"], 6)}
            for m, v in by_model.items()
        },
    }


# ──────────────────────────────────────────────────────────────────────────
# reconcile_daily — Plan #35 task #42 — drift gates + Slack alerting
# ──────────────────────────────────────────────────────────────────────────


WATCH_THRESHOLD = 0.10  # |drift_pct| > 10% → post a Slack watch notice
ALERT_THRESHOLD = 0.25  # |drift_pct| > 25% → recommend MODEL_COSTS_PER_MTOK refresh


def _drift_direction(drift_pct: float) -> str:
    """Map a signed drift to a stable direction label used for alert dedup.

    Local cost is the estimate; Anthropic cost is the ground truth. We compute
    ``drift_pct = (anthropic - local) / anthropic`` upstream, so::

        drift_pct > 0  → Anthropic billed more than we estimated → 'under'
        drift_pct < 0  → Anthropic billed less than we estimated → 'over'

    Using a single label per direction is what keeps the dedup primary key
    ``(bucket_date, direction)`` small and predictable.
    """
    return "under" if drift_pct > 0 else "over"


def _already_alerted(bucket_date_str: str, direction: str) -> bool:
    """Return True if we have already posted an alert for this (date, direction).

    Looked up against ``cost_reconciliation_alerts``. Any DB error degrades to
    "not yet alerted" — we'd rather emit a duplicate Slack message than swallow
    a legitimate drift. Returns False when DATABASE_URL is unset so tests and
    dev environments behave deterministically.
    """
    if not db_adapter.DATABASE_URL:
        return False
    try:
        conn = db_adapter._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM cost_reconciliation_alerts "
                    "WHERE bucket_date = %s AND direction = %s",
                    (bucket_date_str, direction),
                )
                return cur.fetchone() is not None
        finally:
            conn.close()
    except Exception:
        log.exception(
            "reconcile_daily: dedup lookup failed for %s/%s (treating as not-yet-alerted)",
            bucket_date_str,
            direction,
        )
        return False


def _record_alert(
    bucket_date_str: str,
    direction: str,
    severity: str,
    drift_pct: float,
    drift_usd: float,
) -> None:
    """Persist a dedup row so subsequent runs for the same (date, direction) skip.

    The table PK is ``(bucket_date, direction)`` — ``ON CONFLICT DO NOTHING``
    keeps this idempotent. Failures are swallowed; the worst case is a
    duplicate Slack message on the next cron tick, not a crashed scheduler.
    """
    if not db_adapter.DATABASE_URL:
        return
    try:
        conn = db_adapter._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO cost_reconciliation_alerts "
                    "(bucket_date, direction, severity, drift_pct, drift_usd) "
                    "VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT (bucket_date, direction) DO NOTHING",
                    (bucket_date_str, direction, severity, drift_pct, drift_usd),
                )
                conn.commit()
        finally:
            conn.close()
    except Exception:
        log.exception(
            "reconcile_daily: failed to record alert for %s/%s",
            bucket_date_str,
            direction,
        )


def _format_alert_message(recon: dict, direction: str, severity: str) -> str:
    """Render the Slack watch line for a reconciliation alert.

    ``severity == "alert"`` (|drift| > 25%) gets an extra line recommending a
    ``MODEL_COSTS_PER_MTOK`` review — pricing-table drift is the most likely
    cause of large miss.
    """
    pct = recon["drift_pct"]
    pct_str = f"{pct * 100:+.1f}%" if pct is not None else "n/a"
    local = recon["local_total_usd"]
    actual = recon["anthropic_total_usd"]
    drift_usd = recon["drift_usd"]

    verb = "under-estimated" if direction == "under" else "over-estimated"

    msg = (
        f"Cost reconciliation drift — {recon['date']}: "
        f"local {verb} by {pct_str} (${abs(drift_usd):.4f}). "
        f"Local estimate ${local:.4f} vs Anthropic ${actual:.4f}."
    )
    if severity == "alert":
        msg += (
            " Drift exceeds 25% — review MODEL_COSTS_PER_MTOK in "
            "session_runner.py and cost_collector.py for pricing-table drift."
        )
    return msg


def reconcile_daily(
    target_date: str | date | None = None,
    *,
    notifier=None,
) -> dict:
    """Compare local estimate vs. Anthropic billing for ``target_date`` and alert.

    Behavior (Plan #35, task #42):

      * Computes the drift via :func:`compute_reconciliation`.
      * When ``drift_pct`` is None (no Anthropic billing pulled yet, or the day
        had zero spend), logs "reconciliation undefined" and returns without
        alerting.
      * When ``|drift_pct| > 25%``: posts a Slack watch notice AND logs a
        recommended ``MODEL_COSTS_PER_MTOK`` refresh.
      * When ``|drift_pct| > 10%``: posts a Slack watch notice.
      * Otherwise: logs "reconciliation OK" with the drift value.
      * Slack notices are deduplicated by ``(bucket_date, direction)`` against
        ``cost_reconciliation_alerts`` so re-running the cron in the same day
        doesn't spam the channel. A flip in direction (e.g. yesterday under,
        today over) still triggers a fresh post.

    Args:
        target_date: the UTC date to reconcile. Defaults to "yesterday" (UTC).
        notifier: optional ``callable(severity, summary)`` injection point for
            tests. Defaults to ``slack_bot.send_notification``. Lazy import so
            this module can be imported in environments without Slack creds.

    Returns:
        Dict with keys::

            {
                "date": "YYYY-MM-DD",
                "drift_pct": float | None,
                "drift_usd": float,
                "local_total_usd": float,
                "anthropic_total_usd": float,
                "severity": "ok" | "watch" | "alert" | "undefined",
                "direction": "under" | "over" | None,
                "alerted": bool,    # True if a Slack message was posted
                "deduped": bool,    # True if we suppressed a duplicate alert
            }

    Never raises — a misfire here must not crash APScheduler.
    """
    if target_date is None:
        # Yesterday in local-system terms — the 06:00 PT pull cron runs first,
        # so by 07:00 PT yesterday's Anthropic ground-truth row has been
        # upserted into anthropic_daily_costs.
        target_date = date.today() - timedelta(days=1)

    if isinstance(target_date, date):
        date_str = target_date.isoformat()
    else:
        date_str = str(target_date)

    recon = compute_reconciliation(target_date)
    drift_pct = recon.get("drift_pct")
    drift_usd = recon.get("drift_usd", 0.0)
    local_total = recon.get("local_total_usd", 0.0)
    anthropic_total = recon.get("anthropic_total_usd", 0.0)

    # Resolve the notifier here, after compute_reconciliation, so a missing
    # slack_bot import (e.g. running this from a script or pytest) never blocks
    # the math. Tests inject ``notifier`` directly to assert call shape.
    if notifier is None:
        try:
            from slack_bot import send_notification as notifier  # type: ignore
        except Exception:
            notifier = None

    base = {
        "date": date_str,
        "drift_pct": drift_pct,
        "drift_usd": drift_usd,
        "local_total_usd": local_total,
        "anthropic_total_usd": anthropic_total,
    }

    if drift_pct is None:
        log.info(
            "reconcile_daily: %s — undefined drift (no Anthropic billing yet "
            "or zero spend; local=$%.4f, anthropic=$%.4f)",
            date_str,
            local_total,
            anthropic_total,
        )
        return {
            **base,
            "severity": "undefined",
            "direction": None,
            "alerted": False,
            "deduped": False,
        }

    abs_pct = abs(drift_pct)
    if abs_pct <= WATCH_THRESHOLD:
        log.info(
            "reconcile_daily: %s — OK (drift %+.2f%%, local=$%.4f, anthropic=$%.4f)",
            date_str,
            drift_pct * 100,
            local_total,
            anthropic_total,
        )
        return {
            **base,
            "severity": "ok",
            "direction": None,
            "alerted": False,
            "deduped": False,
        }

    severity = "alert" if abs_pct > ALERT_THRESHOLD else "watch"
    direction = _drift_direction(drift_pct)

    if severity == "alert":
        log.warning(
            "reconcile_daily: %s — drift %+.2f%% exceeds 25%% — recommend "
            "MODEL_COSTS_PER_MTOK refresh in session_runner.py / "
            "cost_collector.py (local=$%.4f, anthropic=$%.4f)",
            date_str,
            drift_pct * 100,
            local_total,
            anthropic_total,
        )
    else:
        log.warning(
            "reconcile_daily: %s — drift %+.2f%% exceeds 10%% (local=$%.4f, "
            "anthropic=$%.4f)",
            date_str,
            drift_pct * 100,
            local_total,
            anthropic_total,
        )

    if _already_alerted(date_str, direction):
        log.info(
            "reconcile_daily: %s/%s already alerted today — suppressing duplicate",
            date_str,
            direction,
        )
        return {
            **base,
            "severity": severity,
            "direction": direction,
            "alerted": False,
            "deduped": True,
        }

    msg = _format_alert_message(recon, direction, severity)
    if notifier is not None:
        try:
            notifier("watch", msg)
        except Exception:
            log.exception("reconcile_daily: Slack notification failed for %s", date_str)
    else:
        log.warning(
            "reconcile_daily: no Slack notifier available — would have posted: %s",
            msg,
        )

    _record_alert(date_str, direction, severity, drift_pct, drift_usd)

    return {
        **base,
        "severity": severity,
        "direction": direction,
        "alerted": True,
        "deduped": False,
    }
