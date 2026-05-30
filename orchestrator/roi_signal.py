"""ROI signal: "cost per useful answer" (Plan #30 D3).

Joins the two ledgers shipped earlier in the program:

  * ``session_costs``  — every Managed-Agent session, with full attribution
    (portco_key, thread_ts, trigger, …) and ``cost_usd``. Source-of-truth for
    the dollars half of the ratio.

  * ``feedback_events`` — one row per user signal on a bot message
    (positive / negative / neutral). Source-of-truth for the "useful?" half.
    Currently emoji-only (D1); text-mode is D2.

Math
----

A portco's spend over the window is summed from ``session_costs``. Feedback
counts come from ``feedback_events`` joined to the same portco via
``session_costs.thread_ts = feedback_events.thread_ts`` — this anchors
spend to the exact threads the partners actually reacted to, so cost that
never produced a thread (and therefore no feedback opportunity) does NOT
inflate the "useful" denominator.

Definitions (per Plan #30 D3 spec):

  * ``cost_per_positive``  =  total_cost_usd / positive_count
                              (None when positive_count == 0)

  * ``cost_per_useful``    =  total_cost_usd / (positive_count + neutral_count)
                              — "neutral is implicit good-enough; only negative is wasted."
                              float('inf') when positive_count + neutral_count == 0
                              **and** negative_count > 0 (all-negative portco).
                              None when there is no feedback at all.

  * ``useful_rate``        =  (positive_count + neutral_count) / total_events
                              None when total_events == 0.

Edge cases:

  * A portco that has cost but no feedback events  → all three ROI cells are
    ``None`` (we have spend but cannot grade it).
  * A portco with feedback but no cost in the window → all three are ``None``
    on the cost side; the row is still emitted so the operator sees the
    feedback count.
  * All-negative portco                            → ``cost_per_useful`` is
    ``float('inf')`` as the documented sentinel; the digest renderer keys off
    ``useful_rate == 0.0`` for the watch line.

The single public entry point is :func:`compute_roi`. The digest integration
lives in :func:`render_digest_block`; ``cost_digest.build_digest_message``
calls it and appends the output when non-empty.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

import db_adapter

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────
# Tunables
# ──────────────────────────────────────────────────────────────────────────

DEFAULT_WINDOW_DAYS = 7

# A portco whose ``useful_rate`` falls below this threshold gets a watch
# banner at the top of the digest's ROI block. Matches the
# "actionability degradation" trigger in Plan #30's prompt-improvement loop.
USEFUL_RATE_WATCH_THRESHOLD = 0.50


# ──────────────────────────────────────────────────────────────────────────
# DB helpers (mirror the cost_digest pattern — small, swallow on failure)
# ──────────────────────────────────────────────────────────────────────────


def _fetch(sql: str, params: tuple) -> list[dict]:
    """Run a SELECT and return rows as dicts. Empty list on any failure.

    Mirrors ``cost_digest._fetch`` so the two modules behave identically
    when DATABASE_URL is unset or psycopg2 raises mid-query — neither
    surface should ever raise into APScheduler.
    """
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
        log.exception("roi_signal: DB query failed (sql=%s)", sql[:80])
        return []


# ──────────────────────────────────────────────────────────────────────────
# Per-portco cost + feedback rollups
# ──────────────────────────────────────────────────────────────────────────


def fetch_cost_per_portco(start_date: date, end_date: date) -> list[dict]:
    """Per-portco spend over [start_date, end_date] inclusive.

    Pulls from ``session_costs`` only — ``messages_api_calls`` is excluded
    because those calls (self_heal / self_improve / cost_collector) don't
    map to a Slack thread the user can react to, so they can't participate
    in the ROI ratio in any meaningful way.

    Returns rows shaped like::

        {"portco_key": "acme",
         "total_cost_usd": 12.40,
         "sessions": 27}

    Rows sorted by cost desc. NULL portco_key becomes ``'(none)'`` so the
    rollup never has a NULL bucket.
    """
    return _fetch(
        """
        SELECT COALESCE(portco_key, '(none)') AS portco_key,
               SUM(cost_usd)::float          AS total_cost_usd,
               COUNT(*)                      AS sessions
        FROM session_costs
        WHERE recorded_at::date BETWEEN %s AND %s
        GROUP BY COALESCE(portco_key, '(none)')
        ORDER BY SUM(cost_usd) DESC
        """,
        (start_date, end_date),
    )


def fetch_feedback_counts_per_portco(start_date: date, end_date: date) -> list[dict]:
    """Per-portco feedback counts, joined to session_costs via ``thread_ts``.

    The join is the load-bearing part of the spec: feedback only "counts"
    against a portco if a session actually ran for that portco on the same
    thread. This prevents a stray reaction in a non-portco channel from
    distorting any portco's rate.

    Returns rows shaped like::

        {"portco_key": "acme",
         "positive_count": 4,
         "negative_count": 1,
         "neutral_count":  2}

    Portcos with no feedback at all are absent — the caller decides whether
    to emit a row for them (``compute_roi`` always does, to surface the
    "cost without grade" case).
    """
    return _fetch(
        """
        SELECT COALESCE(sc.portco_key, '(none)') AS portco_key,
               SUM(CASE WHEN fe.signal = 'positive' THEN 1 ELSE 0 END)::int AS positive_count,
               SUM(CASE WHEN fe.signal = 'negative' THEN 1 ELSE 0 END)::int AS negative_count,
               SUM(CASE WHEN fe.signal = 'neutral'  THEN 1 ELSE 0 END)::int AS neutral_count
        FROM feedback_events fe
        JOIN session_costs sc
          ON sc.thread_ts = fe.thread_ts
         AND sc.thread_ts IS NOT NULL
         AND sc.thread_ts <> ''
        WHERE fe.ts::date BETWEEN %s AND %s
        GROUP BY COALESCE(sc.portco_key, '(none)')
        """,
        (start_date, end_date),
    )


# ──────────────────────────────────────────────────────────────────────────
# Pure ROI math (no DB) — kept separate so it's trivially unit-testable
# ──────────────────────────────────────────────────────────────────────────


def _roi_cells(
    *,
    total_cost_usd: Optional[float],
    positive_count: int,
    negative_count: int,
    neutral_count: int,
) -> dict:
    """Compute ``cost_per_positive`` / ``cost_per_useful`` / ``useful_rate``.

    Returns a dict with exactly those three keys. Edge cases per module docstring.
    Inputs are non-negative integers; total_cost_usd may be ``None`` (no spend).
    """
    total_events = positive_count + negative_count + neutral_count
    useful_count = positive_count + neutral_count

    # No feedback at all → cannot grade. All ROI cells are None.
    if total_events == 0:
        return {
            "cost_per_positive": None,
            "cost_per_useful": None,
            "useful_rate": None,
        }

    useful_rate = useful_count / total_events

    # No cost recorded → can compute the rate but cost-per-X is meaningless.
    if total_cost_usd is None or total_cost_usd <= 0:
        return {
            "cost_per_positive": None,
            "cost_per_useful": None,
            "useful_rate": useful_rate,
        }

    cost_per_positive = (
        (total_cost_usd / positive_count) if positive_count > 0 else None
    )
    if useful_count > 0:
        cost_per_useful: Optional[float] = total_cost_usd / useful_count
    else:
        # All-negative case: cost was spent, nothing was useful. Sentinel
        # value so downstream consumers (digest, dashboards) can render
        # "∞" or "all wasted." float('inf') is JSON-unsafe but the dict is
        # only consumed in-process, never serialized to JSON.
        cost_per_useful = float("inf")

    return {
        "cost_per_positive": cost_per_positive,
        "cost_per_useful": cost_per_useful,
        "useful_rate": useful_rate,
    }


# ──────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────


def compute_roi(window_days: int = DEFAULT_WINDOW_DAYS) -> dict:
    """Compute the per-portco and overall ROI snapshot.

    Args:
        window_days: Trailing window, ending today. ``7`` matches the
            digest cadence; ``90`` is the typical longitudinal review
            window. Must be a positive integer.

    Returns:
        ::

            {
              "window_days": 7,
              "by_portco": [
                {
                  "portco_key":        "acme",
                  "total_cost_usd":    12.40,
                  "positive_count":    4,
                  "negative_count":    1,
                  "neutral_count":     2,
                  "cost_per_positive": 3.10,   # None when 0 positives
                  "cost_per_useful":   2.07,   # None|inf per spec
                  "useful_rate":       0.857,  # None when 0 events
                },
                ...
              ],
              "overall": {  # same shape, summed across all portcos }
            }

        Sorted by ``total_cost_usd`` descending. Empty ``by_portco`` and an
        all-zero ``overall`` block when DATABASE_URL is unset.
    """
    if window_days <= 0:
        raise ValueError(f"window_days must be positive, got {window_days}")

    end_date = date.today()
    start_date = end_date - timedelta(days=window_days - 1)

    cost_rows = fetch_cost_per_portco(start_date, end_date)
    feedback_rows = fetch_feedback_counts_per_portco(start_date, end_date)

    # Index feedback by portco for the join — feedback may exist without cost
    # (rare but possible: cost was in a prior window, the reaction happens now).
    feedback_by_portco: dict[str, dict] = {r["portco_key"]: r for r in feedback_rows}
    cost_by_portco: dict[str, dict] = {r["portco_key"]: r for r in cost_rows}

    all_portcos = set(cost_by_portco.keys()) | set(feedback_by_portco.keys())

    by_portco: list[dict] = []
    for portco_key in all_portcos:
        cost_row = cost_by_portco.get(portco_key, {})
        fb_row = feedback_by_portco.get(portco_key, {})
        total_cost = float(cost_row.get("total_cost_usd") or 0.0)
        pos = int(fb_row.get("positive_count") or 0)
        neg = int(fb_row.get("negative_count") or 0)
        neu = int(fb_row.get("neutral_count") or 0)
        cells = _roi_cells(
            total_cost_usd=total_cost if total_cost > 0 else None,
            positive_count=pos,
            negative_count=neg,
            neutral_count=neu,
        )
        by_portco.append(
            {
                "portco_key": portco_key,
                "total_cost_usd": total_cost,
                "positive_count": pos,
                "negative_count": neg,
                "neutral_count": neu,
                **cells,
            }
        )

    by_portco.sort(key=lambda r: r.get("total_cost_usd") or 0, reverse=True)

    overall_cost = sum((r["total_cost_usd"] or 0) for r in by_portco)
    overall_pos = sum(r["positive_count"] for r in by_portco)
    overall_neg = sum(r["negative_count"] for r in by_portco)
    overall_neu = sum(r["neutral_count"] for r in by_portco)
    overall_cells = _roi_cells(
        total_cost_usd=overall_cost if overall_cost > 0 else None,
        positive_count=overall_pos,
        negative_count=overall_neg,
        neutral_count=overall_neu,
    )
    overall = {
        "total_cost_usd": overall_cost,
        "positive_count": overall_pos,
        "negative_count": overall_neg,
        "neutral_count": overall_neu,
        **overall_cells,
    }

    return {
        "window_days": window_days,
        "by_portco": by_portco,
        "overall": overall,
    }


# ──────────────────────────────────────────────────────────────────────────
# Digest rendering — pure function, no DB
# ──────────────────────────────────────────────────────────────────────────


def _fmt_usd(amount: float) -> str:
    return f"${amount:,.2f}"


def _fmt_pct(rate: Optional[float]) -> str:
    if rate is None:
        return "n/a"
    return f"{rate * 100:.0f}%"


def _fmt_cost_per(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    if value == float("inf"):
        return "∞"
    return _fmt_usd(value)


def render_digest_block(roi: dict) -> str:
    """Render the ROI block for ``cost_digest.build_digest_message``.

    Returns the empty string when there's no useful content to show — the
    caller appends nothing rather than printing an empty section header.

    Output shape (when content present)::

        :warning: Watch — useful_rate < 50% for: acme (40%), acme (33%)

        *Cost per useful answer (last 7d)*
        portco       cost     pos/neg/neu   $/useful   useful%
        acme     $12.40   4/1/2         $2.07      86%
        acme          $3.20   0/2/0         ∞           0%
        (none)        $0.50   0/0/0         n/a        n/a

    The watch line uses the same emoji + tone as the existing drift watch
    in ``cost_digest`` so the digest reads consistently.
    """
    by_portco = roi.get("by_portco") or []
    if not by_portco:
        return ""

    window_days = roi.get("window_days") or DEFAULT_WINDOW_DAYS

    # Watch line — any portco whose useful_rate is below threshold *and*
    # actually has feedback (None useful_rate means no feedback, which we
    # don't surface as a regression).
    watch_portcos = [
        r
        for r in by_portco
        if r.get("useful_rate") is not None
        and r["useful_rate"] < USEFUL_RATE_WATCH_THRESHOLD
    ]

    lines: list[str] = []
    if watch_portcos:
        bits = ", ".join(
            f"{r['portco_key']} ({_fmt_pct(r['useful_rate'])})" for r in watch_portcos
        )
        lines.append(
            f":warning: Watch — useful_rate < "
            f"{int(USEFUL_RATE_WATCH_THRESHOLD * 100)}% for: {bits}"
        )
        lines.append("")

    lines.append(f"*Cost per useful answer (last {window_days}d)*")
    # Header — fixed-width-ish columns matched by the rows below. Slack
    # mrkdwn doesn't have real tables; this is the same style the daily
    # cost digest uses for the top-sessions block.
    lines.append("portco       cost     pos/neg/neu   $/useful   useful%")
    for r in by_portco:
        portco = (r["portco_key"] or "")[:12].ljust(12)
        cost = _fmt_usd(r["total_cost_usd"] or 0.0).rjust(8)
        ratio = (
            f"{r['positive_count']}/{r['negative_count']}/{r['neutral_count']}".ljust(
                13
            )
        )
        cpu = _fmt_cost_per(r.get("cost_per_useful")).rjust(8)
        useful = _fmt_pct(r.get("useful_rate")).rjust(6)
        lines.append(f"{portco} {cost} {ratio} {cpu}   {useful}")

    return "\n".join(lines)
