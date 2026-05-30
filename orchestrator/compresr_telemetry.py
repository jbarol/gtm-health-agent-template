"""Compresr per-call telemetry aggregations.

Plan #37, Task #66. Read-only rollups over ``compresr_calls`` for the daily DM
digest and any future reporting surface. All queries operate on a trailing
window of N days ending at "yesterday" so the digest line matches the rest of
the per-day rollups in ``cost_digest.py``.

Three aggregations:

* ``cache_hit_rate(days)`` — fraction of calls served from ``compresr_cache``
  (``cache_hit IS TRUE``) over the window. Cache hits are the cheapest path —
  no SDK round-trip, no Compresr-side spend — so a sustained low number is the
  first signal that the cache TTL or content-hashing strategy needs tuning.
* ``fallback_rate(days)`` — fraction of calls that returned the original text
  unchanged (``fallback IS TRUE``). The plan promises a watch notice when this
  exceeds 25% over 24h; this query is the input to that check.
* ``avg_savings_ratio(days)`` — mean ``compressed_chars / input_chars`` across
  SUCCESSFUL compressions only (``fallback IS FALSE``). Excludes fallback rows
  whose ratio is fixed at 1.0 — including them would mask real savings under a
  pile of disabled-site no-ops. ``1 - ratio`` is the human-readable savings %.
* ``latency_stats(days)`` — average + p95 wall-clock latency of every call
  (fallback rows included; their latency is the gate decision time, which is
  what the operator actually pays in wall-clock).

All four return ``None`` (or zero-row dicts) when ``DATABASE_URL`` is unset or
the window contains no rows. Never raise — the digest must keep rendering even
when telemetry is empty.

``summarize(days)`` packages all four into one dict for ``cost_digest.py``::

    {
      "days": int,
      "total_calls": int,
      "cache_hits": int,
      "cache_hit_rate": float | None,        # 0..1
      "fallbacks": int,
      "fallback_rate": float | None,         # 0..1
      "successful_calls": int,
      "avg_savings_ratio": float | None,     # 0..1 (compressed/input)
      "avg_latency_ms": float | None,
      "p95_latency_ms": float | None,
      "by_call_site": list[dict],            # per-call-site breakdown
    }

``render_digest_block(summary)`` produces the multi-line Slack-friendly block
the digest appends after the by-task split.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

import db_adapter

log = logging.getLogger(__name__)


# Default trailing window (days). Matches Plan #37's "fallback rate over 24h"
# language at the floor (``days=1``); the digest passes ``days=1`` to show
# yesterday's number alongside the rest of the daily rollup.
DEFAULT_WINDOW_DAYS = 1
FALLBACK_WATCH_THRESHOLD = 0.25  # > 25% over the window → digest emits a watch line


# ──────────────────────────────────────────────────────────────────────────
# Window helper
# ──────────────────────────────────────────────────────────────────────────


def _window_bounds(days: int, target: Optional[date] = None) -> tuple[date, date]:
    """Return an inclusive (start, end) date pair anchored at ``target``.

    ``days=1`` and ``target=yesterday`` covers just yesterday; ``days=7`` covers
    the trailing week ending yesterday. Both endpoints inclusive.
    """
    end = target or (date.today() - timedelta(days=1))
    start = end - timedelta(days=max(days, 1) - 1)
    return start, end


# ──────────────────────────────────────────────────────────────────────────
# DB fetch helper — mirrors cost_digest._fetch for graceful no-DB degradation
# ──────────────────────────────────────────────────────────────────────────


def _fetch(sql: str, params: tuple) -> list[dict]:
    """Run a SELECT and return list of dicts. Empty on failure / missing DB."""
    if not db_adapter.DATABASE_URL:
        return []
    try:
        import psycopg2.extras

        conn = db_adapter._connect()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params)
                return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()
    except Exception:
        log.exception("compresr_telemetry: DB query failed (sql=%s)", sql[:80])
        return []


# ──────────────────────────────────────────────────────────────────────────
# Individual aggregations
# ──────────────────────────────────────────────────────────────────────────


def cache_hit_rate(
    days: int = DEFAULT_WINDOW_DAYS, target: Optional[date] = None
) -> Optional[float]:
    """Fraction of calls served from cache over the trailing ``days`` window.

    Returns ``None`` when the window contains zero rows so the renderer can
    show "n/a" rather than a misleading "0.0%".
    """
    start, end = _window_bounds(days, target)
    rows = _fetch(
        """
        SELECT
            COUNT(*)::bigint AS total,
            COUNT(*) FILTER (WHERE cache_hit IS TRUE)::bigint AS hits
        FROM compresr_calls
        WHERE created_at::date BETWEEN %s AND %s
        """,
        (start, end),
    )
    if not rows:
        return None
    total = rows[0].get("total") or 0
    hits = rows[0].get("hits") or 0
    if not total:
        return None
    return float(hits) / float(total)


def fallback_rate(
    days: int = DEFAULT_WINDOW_DAYS, target: Optional[date] = None
) -> Optional[float]:
    """Fraction of calls that fell back to the original text over the window.

    Plan #37 sets 25% as the digest watch threshold — when this value exceeds
    ``FALLBACK_WATCH_THRESHOLD``, the digest leads its compression block with a
    watch line.
    """
    start, end = _window_bounds(days, target)
    rows = _fetch(
        """
        SELECT
            COUNT(*)::bigint AS total,
            COUNT(*) FILTER (WHERE fallback IS TRUE)::bigint AS fallbacks
        FROM compresr_calls
        WHERE created_at::date BETWEEN %s AND %s
        """,
        (start, end),
    )
    if not rows:
        return None
    total = rows[0].get("total") or 0
    fallbacks = rows[0].get("fallbacks") or 0
    if not total:
        return None
    return float(fallbacks) / float(total)


def avg_savings_ratio(
    days: int = DEFAULT_WINDOW_DAYS, target: Optional[date] = None
) -> Optional[float]:
    """Mean ``compressed_chars / input_chars`` across SUCCESSFUL compressions.

    Successful = ``fallback IS FALSE``. Excludes fallback rows whose ratio is
    pinned at 1.0; mixing them in would mask actual savings under a pile of
    disabled-site no-ops. ``1 - return value`` is the human-readable savings
    percentage (e.g. 0.45 → 55% reduction).
    """
    start, end = _window_bounds(days, target)
    rows = _fetch(
        """
        SELECT AVG(compression_ratio)::float AS avg_ratio,
               COUNT(*)::bigint AS n
        FROM compresr_calls
        WHERE created_at::date BETWEEN %s AND %s
          AND fallback IS FALSE
          AND compression_ratio IS NOT NULL
          AND input_chars > 0
        """,
        (start, end),
    )
    if not rows:
        return None
    n = rows[0].get("n") or 0
    if not n:
        return None
    avg = rows[0].get("avg_ratio")
    return float(avg) if avg is not None else None


def latency_stats(
    days: int = DEFAULT_WINDOW_DAYS, target: Optional[date] = None
) -> dict:
    """Wall-clock latency stats (mean + p95) across all calls in the window.

    Returns ``{"avg_ms": float | None, "p95_ms": float | None, "n": int}``.
    Fallback rows are included on purpose — they represent the wall-clock cost
    the caller actually paid, not the SDK's compress time.
    """
    start, end = _window_bounds(days, target)
    rows = _fetch(
        """
        SELECT
            AVG(latency_ms)::float AS avg_ms,
            PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms)::float AS p95_ms,
            COUNT(*)::bigint AS n
        FROM compresr_calls
        WHERE created_at::date BETWEEN %s AND %s
          AND latency_ms IS NOT NULL
        """,
        (start, end),
    )
    if not rows:
        return {"avg_ms": None, "p95_ms": None, "n": 0}
    return {
        "avg_ms": rows[0].get("avg_ms"),
        "p95_ms": rows[0].get("p95_ms"),
        "n": int(rows[0].get("n") or 0),
    }


def by_call_site(
    days: int = DEFAULT_WINDOW_DAYS, target: Optional[date] = None
) -> list[dict]:
    """Per-call-site totals — calls, fallback rate, avg ratio. Sorted by call count desc."""
    start, end = _window_bounds(days, target)
    return _fetch(
        """
        SELECT
            call_site,
            COUNT(*)::bigint AS calls,
            COUNT(*) FILTER (WHERE cache_hit IS TRUE)::bigint AS cache_hits,
            COUNT(*) FILTER (WHERE fallback IS TRUE)::bigint AS fallbacks,
            AVG(compression_ratio) FILTER (WHERE fallback IS FALSE)::float AS avg_ratio
        FROM compresr_calls
        WHERE created_at::date BETWEEN %s AND %s
        GROUP BY call_site
        ORDER BY COUNT(*) DESC
        """,
        (start, end),
    )


# ──────────────────────────────────────────────────────────────────────────
# Bundled summary — single DB pass per metric, used by cost_digest
# ──────────────────────────────────────────────────────────────────────────


def summarize(days: int = DEFAULT_WINDOW_DAYS, target: Optional[date] = None) -> dict:
    """Compute every metric the digest needs in one call.

    Returns a dict with all six fields described in the module docstring.
    Always returns a dict — callers branch on ``total_calls == 0`` to decide
    whether to render the block at all.
    """
    start, end = _window_bounds(days, target)
    counts = _fetch(
        """
        SELECT
            COUNT(*)::bigint AS total,
            COUNT(*) FILTER (WHERE cache_hit IS TRUE)::bigint AS cache_hits,
            COUNT(*) FILTER (WHERE fallback IS TRUE)::bigint AS fallbacks,
            COUNT(*) FILTER (WHERE fallback IS FALSE)::bigint AS successful,
            AVG(compression_ratio) FILTER (WHERE fallback IS FALSE AND input_chars > 0)::float
                AS avg_ratio
        FROM compresr_calls
        WHERE created_at::date BETWEEN %s AND %s
        """,
        (start, end),
    )
    lat = latency_stats(days, target)
    sites = by_call_site(days, target)

    if not counts:
        # No DB / query failed.
        return {
            "days": days,
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
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

    row = counts[0]
    total = int(row.get("total") or 0)
    hits = int(row.get("cache_hits") or 0)
    fb = int(row.get("fallbacks") or 0)
    ok = int(row.get("successful") or 0)
    avg_ratio = row.get("avg_ratio")

    hit_rate = (hits / total) if total else None
    fb_rate = (fb / total) if total else None

    return {
        "days": days,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "total_calls": total,
        "cache_hits": hits,
        "cache_hit_rate": hit_rate,
        "fallbacks": fb,
        "fallback_rate": fb_rate,
        "successful_calls": ok,
        "avg_savings_ratio": float(avg_ratio) if avg_ratio is not None else None,
        "avg_latency_ms": lat.get("avg_ms"),
        "p95_latency_ms": lat.get("p95_ms"),
        "by_call_site": sites,
    }


# ──────────────────────────────────────────────────────────────────────────
# Digest block renderer — pure function, called by cost_digest.build_digest_message
# ──────────────────────────────────────────────────────────────────────────


def _fmt_pct(frac: Optional[float]) -> str:
    if frac is None:
        return "n/a"
    return f"{frac * 100:.0f}%"


def _fmt_ms(ms: Optional[float]) -> str:
    if ms is None:
        return "n/a"
    return f"{ms:.0f}ms"


def render_digest_block(summary: dict) -> str:
    """Render the "Compression savings" multi-line block for the daily digest.

    Returns an empty string when ``total_calls == 0`` so the caller can append
    unconditionally without producing a stub block on days with no compression
    activity (this happens when ``COMPRESR_API_KEY`` is unset or every call
    site flag is off — both of which are valid production configurations).

    Output shape::

        *Compression savings:*
        Calls: 47  (cache hits 21 / fallbacks 3)
        Cache hit-rate: 45% · Fallback rate: 6%
        Avg savings: 52% (compressed→ 48% of input) · Latency: avg 320ms / p95 980ms
        By site: self_heal 38 (5% fb) · self_improve 9 (0% fb)

    A "watch" line precedes the block when ``fallback_rate`` exceeds
    ``FALLBACK_WATCH_THRESHOLD`` (Plan #37 → 25% over 24h).
    """
    if not summary or not summary.get("total_calls"):
        return ""

    lines: list[str] = []

    fb_rate = summary.get("fallback_rate")
    if fb_rate is not None and fb_rate > FALLBACK_WATCH_THRESHOLD:
        lines.append(
            f":warning: Watch — Compresr fallback rate {_fmt_pct(fb_rate)} "
            f"(> {int(FALLBACK_WATCH_THRESHOLD * 100)}%); investigate vendor "
            "health or key rotation."
        )

    lines.append("*Compression savings:*")
    lines.append(
        f"Calls: {summary['total_calls']}  "
        f"(cache hits {summary['cache_hits']} / fallbacks {summary['fallbacks']})"
    )

    cache_pct = _fmt_pct(summary.get("cache_hit_rate"))
    fb_pct = _fmt_pct(fb_rate)
    lines.append(f"Cache hit-rate: {cache_pct} · Fallback rate: {fb_pct}")

    ratio = summary.get("avg_savings_ratio")
    if ratio is not None:
        savings_pct = (1.0 - ratio) * 100
        ratio_pct = ratio * 100
        savings_line = (
            f"Avg savings: {savings_pct:.0f}% (compressed → {ratio_pct:.0f}% of input)"
        )
    else:
        savings_line = "Avg savings: n/a (no successful compressions)"

    avg_ms = summary.get("avg_latency_ms")
    p95_ms = summary.get("p95_latency_ms")
    latency_line = f"Latency: avg {_fmt_ms(avg_ms)} / p95 {_fmt_ms(p95_ms)}"

    lines.append(f"{savings_line} · {latency_line}")

    sites = summary.get("by_call_site") or []
    if sites:
        rendered = []
        for s in sites:
            calls = s.get("calls") or 0
            site_fb = s.get("fallbacks") or 0
            site_fb_pct = (site_fb / calls) if calls else 0
            rendered.append(
                f"{s.get('call_site', '(unknown)')} {calls} "
                f"({site_fb_pct * 100:.0f}% fb)"
            )
        lines.append(f"By site: {' · '.join(rendered)}")

    return "\n".join(lines)
