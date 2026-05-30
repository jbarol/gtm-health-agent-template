"""Cache-aware usage logging for non-session Messages API calls.

The Managed Agents session API exposes a nested ``usage.cache_creation`` object
that splits 5-minute vs. 1-hour ephemeral cache writes (see
``session_runner.py:_extract_usage_parts``). The Messages API does NOT —
its ``usage.cache_creation_input_tokens`` is a single scalar covering all
cache writes, and there is no per-TTL breakdown unless the caller used
``extra_headers`` to request the 1-hour beta.

This module wraps that shape into a uniform extract/cost/log triple
so ``self_heal.py`` and ``self_improve.py`` don't each duplicate the
same arithmetic.

Plan #35 (docs/plans/35-cost-tracking-and-reporting.md) will persist
these calls to a Postgres ``messages_api_calls`` table via a future
``cost_collector`` module. Until then, the log line is the only record.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

# Pricing in $/MTOK. Mirrors session_runner.MODEL_COSTS_PER_MTOK; we duplicate
# instead of importing because session_runner already imports self_heal at
# module load (which now imports this helper), and that creates a circular
# import on cold start.
#
# Plan #35 (docs/plans/35-cost-tracking-and-reporting.md) consolidates pricing
# into a shared module — when that lands, both this file and session_runner
# switch to the shared table and the duplication goes away.
#
# Note: only the 5-minute cache-write rate is needed here. The Messages API
# returns a single cache_creation_input_tokens scalar; without the 1-hour
# beta header (which neither self_heal nor self_improve sets), Anthropic
# bills writes at the 5m rate.
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


def extract_messages_usage(usage: Any) -> dict:
    """Pull the four token categories out of a Messages API usage object.

    Shape (per Anthropic SDK):
        input_tokens                  — new uncached input tokens
        output_tokens                 — output tokens
        cache_read_input_tokens       — tokens served from cache (may be None)
        cache_creation_input_tokens   — tokens written to cache, single scalar
                                        (may be None when no caching configured)

    Unlike Managed Agents sessions, there is NO nested ``cache_creation``
    object — the 5m/1h split is not available unless the caller passed the
    1-hour cache beta header AND examined the alternate response shape. For
    our use (self_heal / self_improve, default 5m caching), the scalar is
    sufficient.

    All None / missing values are coerced to 0 so callers can sum safely.
    """
    if usage is None:
        return {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    return {
        "input": getattr(usage, "input_tokens", 0) or 0,
        "output": getattr(usage, "output_tokens", 0) or 0,
        "cache_read": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "cache_write": getattr(usage, "cache_creation_input_tokens", 0) or 0,
    }


def estimate_messages_cost(usage: Any, model: str) -> float:
    """Estimate cost in dollars for a single Messages API response.

    The Messages API doesn't split cache writes by TTL, so the
    ``cache_write`` bucket is priced at the 5-minute rate — which is the
    default when the 1-hour beta header isn't sent. If a future caller
    needs the 1-hour rate, add a ``ttl="1h"`` arg and select the
    ``cache_write_1h`` rate instead.

    Returns 0.0 when the model isn't in MODEL_COSTS_PER_MTOK rather than
    raising — keeps logging non-fatal if Anthropic ships a new model name
    before our rate table is updated.
    """
    rates = MODEL_COSTS_PER_MTOK.get(model)
    if rates is None:
        log.warning(f"No cost rates for model {model!r}; reporting cost=$0.0000")
        return 0.0
    u = extract_messages_usage(usage)
    return (
        u["input"] * rates["input"] / 1_000_000
        + u["output"] * rates["output"] / 1_000_000
        + u["cache_read"] * rates["cache_read"] / 1_000_000
        + u["cache_write"] * rates["cache_write_5m"] / 1_000_000
    )


def cache_hit_pct(usage: Any) -> float:
    """Cache hit % = cache_read / (input + cache_read + cache_write).

    Returns 0.0 when there were no input-side tokens at all. Mirrors
    ``session_runner._cache_hit_pct`` so log lines stay comparable across
    sessions and one-shot calls.
    """
    u = extract_messages_usage(usage)
    total = u["input"] + u["cache_read"] + u["cache_write"]
    if total == 0:
        return 0.0
    return round(100 * u["cache_read"] / total, 1)


def log_messages_usage(caller: str, model: str, usage: Any) -> dict:
    """Emit a single INFO log line summarizing the call's token usage.

    Format (one line, ASCII-only for Railway log viewer):
        ``<caller> call (<model>): input=X, output=Y, cache_read=Z, cache_write=W, cost=$D.DDDD, cache_hit_pct=Q%``

    Returns the parsed numbers as a dict so tests (and future
    Plan #35 persistence wrappers) can assert on them without parsing
    the log string.
    """
    parts = extract_messages_usage(usage)
    cost = estimate_messages_cost(usage, model)
    hit_pct = cache_hit_pct(usage)
    log.info(
        f"{caller} call ({model}): "
        f"input={parts['input']}, output={parts['output']}, "
        f"cache_read={parts['cache_read']}, cache_write={parts['cache_write']}, "
        f"cost=${cost:.4f}, cache_hit_pct={hit_pct}%"
    )
    return {
        "input": parts["input"],
        "output": parts["output"],
        "cache_read": parts["cache_read"],
        "cache_write": parts["cache_write"],
        "cost": cost,
        "cache_hit_pct": hit_pct,
    }
