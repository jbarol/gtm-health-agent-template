"""Plan #44 Task #15 — emit ``user.interrupt`` to stop a running session.

The Anthropic Managed Agents API supports a ``user.interrupt`` event
that cleanly stops a session that is mid-stream. We expose
:func:`interrupt_session` as the single entry point so the Slack
``/stop`` slash command (in :mod:`slack_bot`) can hit it without
touching :mod:`session_runner` — keeping merge isolation against
Bundle B.

The helper:

  1. Looks up the session's running token total + agent model so the
     reply can quote "tokens burned" and "estimated cost" — the rollback
     decision an operator most often needs at 2am (decision row #24).
  2. Sends a ``user.interrupt`` event on the session's events channel.
  3. Returns a structured dict the caller renders into Slack mrkdwn.

Never raises — every failure path returns ``ok=False`` with a non-empty
``error`` so the slash command can post a clear message instead of a
Bolt traceback.
"""

from __future__ import annotations

import logging
from typing import Optional, TypedDict

log = logging.getLogger(__name__)


class InterruptResult(TypedDict, total=False):
    ok: bool
    tokens_burned: int
    cost_usd: float
    thread_id: str
    session_id: str
    error: str


def _build_client():
    """Lazy import so test code can stub anthropic.Anthropic before this
    module's first use. Mirrors the pattern in ``bin/rollback-agent.py``.
    """
    import anthropic  # local — see docstring

    return anthropic.Anthropic()


# Pricing table — kept in sync with
# ``session_runner.MODEL_COSTS_PER_MTOK``. Duplicated (not imported) so
# this module doesn't pull config.py at test time. The two should agree;
# Bundle B's session_runner is the canonical source. If they drift, the
# /stop reply over-estimates by a few cents — load-bearing action is the
# interrupt itself, not the cost line.
_MODEL_COSTS_PER_MTOK = {
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


def _extract_usage_parts(usage) -> dict:
    """Pull the five token categories out of a Managed Agents usage obj.

    Mirrors ``session_runner._extract_usage_parts`` exactly. Duplicated
    to avoid pulling that module (which transitively requires
    ``ANTHROPIC_API_KEY`` at import time). Returns zeros on any field
    that's missing — the WHO_BURNED_THIS line in Slack reads a 0 as
    "couldn't extract" but the interrupt is still emitted.
    """
    if usage is None:
        return {
            "input": 0,
            "output": 0,
            "cache_read": 0,
            "cache_write_5m": 0,
            "cache_write_1h": 0,
        }
    input_tok = getattr(usage, "input_tokens", 0) or 0
    output_tok = getattr(usage, "output_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cw5, cw1 = 0, 0
    cc = getattr(usage, "cache_creation", None)
    if cc is not None:
        cw5 = getattr(cc, "ephemeral_5m_input_tokens", 0) or 0
        cw1 = getattr(cc, "ephemeral_1h_input_tokens", 0) or 0
    return {
        "input": input_tok,
        "output": output_tok,
        "cache_read": cache_read,
        "cache_write_5m": cw5,
        "cache_write_1h": cw1,
    }


def _resolve_model_rates(model_hint: str) -> dict:
    """Match a model id (possibly dated, e.g. claude-haiku-4-5-20251001)
    to the closest entry in :data:`_MODEL_COSTS_PER_MTOK`. Returns ``{}``
    when no match — caller defaults to Opus rates to bias the estimate
    upward."""
    if not model_hint:
        return {}
    if model_hint in _MODEL_COSTS_PER_MTOK:
        return _MODEL_COSTS_PER_MTOK[model_hint]
    # Longest-prefix match on canonical key.
    for key in sorted(_MODEL_COSTS_PER_MTOK, key=len, reverse=True):
        if model_hint.startswith(key):
            return _MODEL_COSTS_PER_MTOK[key]
    return {}


def _estimate_cost_for_session(usage, model_id: str) -> float:
    """Compute the rough USD spend for ``usage`` and ``model_id``.

    Returns 0.0 on every failure path so the slash command never crashes
    on a usage-shape change. The interrupt itself is the load-bearing
    action; the cost number is operator garnish.
    """
    try:
        rates = _resolve_model_rates(model_id) if model_id else None
        if not rates:
            # Default to the most expensive model so the operator's
            # estimate is biased upward — a "stop runaway" decision is
            # rarely hurt by an over-estimate.
            rates = _MODEL_COSTS_PER_MTOK.get("claude-opus-4-8", {})
        parts = _extract_usage_parts(usage)
        total = (
            parts["input"] * rates.get("input", 0.0)
            + parts["output"] * rates.get("output", 0.0)
            + parts["cache_read"] * rates.get("cache_read", 0.0)
            + parts["cache_write_5m"] * rates.get("cache_write_5m", 0.0)
            + parts["cache_write_1h"] * rates.get("cache_write_1h", 0.0)
        ) / 1_000_000.0
        return round(total, 4)
    except Exception:
        log.debug("interrupt cost estimate failed", exc_info=True)
        return 0.0


def _sum_tokens(usage) -> int:
    """Return total tokens (input + output + all cache categories)."""
    try:
        parts = _extract_usage_parts(usage)
        return (
            int(parts.get("input", 0) or 0)
            + int(parts.get("output", 0) or 0)
            + int(parts.get("cache_read", 0) or 0)
            + int(parts.get("cache_write_5m", 0) or 0)
            + int(parts.get("cache_write_1h", 0) or 0)
        )
    except Exception:
        log.debug("interrupt token-sum failed", exc_info=True)
        return 0


def interrupt_session(
    session_id: str,
    thread_id: Optional[str] = None,
    *,
    client=None,
) -> InterruptResult:
    """Send ``user.interrupt`` to ``session_id``.

    Returns a dict shaped like :class:`InterruptResult`. Never raises;
    on failure the dict carries ``ok=False`` and an ``error`` string.

    ``thread_id`` is forwarded to the events.send call. The Anthropic
    SDK's events surface accepts it as an optional kwarg — the Slack
    command typically omits it (passes ``None``) so the interrupt
    targets the whole session rather than a specific sub-agent thread.

    ``client`` is injectable for tests (``MagicMock`` stand-in for
    ``anthropic.Anthropic``). Production callers pass ``None`` and we
    build a fresh client.
    """
    if not session_id:
        return {
            "ok": False,
            "tokens_burned": 0,
            "cost_usd": 0.0,
            "thread_id": thread_id or "",
            "session_id": "",
            "error": "session_id is required",
        }

    cli = client or _build_client()

    # Fetch the session metadata FIRST so we have token + cost numbers
    # to report. If the retrieve fails we still try to interrupt — the
    # interrupt is the priority; the numbers are a "nice to have."
    tokens_burned = 0
    cost_usd = 0.0
    try:
        s = cli.beta.sessions.retrieve(session_id)
        usage = getattr(s, "usage", None)
        model_id = getattr(s, "model", "") or ""
        tokens_burned = _sum_tokens(usage)
        cost_usd = _estimate_cost_for_session(usage, model_id)
    except Exception as e:
        log.warning(
            f"interrupt_session: retrieve({session_id}) failed: {e!r} "
            "— attempting interrupt anyway"
        )

    # Build the user.interrupt event. The SDK's contract is
    # `events.send(session_id=..., events=[{"type": "user.interrupt"}])`.
    # Passing ``thread_id`` is forwarded as a top-level kwarg so the
    # interrupt scopes to a sub-agent thread when set.
    try:
        send_kwargs: dict = {
            "session_id": session_id,
            "events": [{"type": "user.interrupt"}],
        }
        if thread_id:
            send_kwargs["thread_id"] = thread_id
        cli.beta.sessions.events.send(**send_kwargs)
    except Exception as e:
        log.exception(f"interrupt_session: events.send failed for {session_id}")
        return {
            "ok": False,
            "tokens_burned": tokens_burned,
            "cost_usd": cost_usd,
            "thread_id": thread_id or "",
            "session_id": session_id,
            "error": f"events.send failed: {e}",
        }

    return {
        "ok": True,
        "tokens_burned": tokens_burned,
        "cost_usd": cost_usd,
        "thread_id": thread_id or "",
        "session_id": session_id,
        "error": "",
    }
