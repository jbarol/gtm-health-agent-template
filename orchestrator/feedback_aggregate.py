"""Feedback aggregation views (Plan #30 D2).

Reads from ``feedback_events`` (D1) and joins to ``session_costs`` to surface
four rollup views over a sliding window:

  * ``aggregate_by_portco``   — positive/negative/neutral counts per portco
  * ``aggregate_by_agent``    — same, keyed by agent_id (join through thread_ts
                                 → session_costs.agent_id)
  * ``aggregate_by_trigger``  — same, keyed by trigger (slack_mention, cron, …)
  * ``top_negative_recent``   — drill-down: most-recent negative-signal events

All views return ``list[dict]`` sorted in a stable, sensible order (positive_rate
asc for the by-X aggregates so noisy categories surface first; ts desc for the
drill-down). DB failures and missing DATABASE_URL return an empty list rather
than raising — the ``/feedback`` slash command renders graceful fallbacks.

The agent/trigger joins use ``feedback_events.thread_ts = session_costs.thread_ts``
when the column is populated. Where the same thread spawns multiple sessions
(thread reuse for follow-ups), we pick the latest session per thread via a
``DISTINCT ON`` projection — the most recent agent/trigger context wins.

Signal taxonomy reminder (D1 contract):
    positive  — ``thumbsup``, ``+1``, ``heavy_check_mark``, ``white_check_mark``,
                 ``tada``, ``100``, ``fire``, ``clap``, ``bow``
    negative  — ``thumbsdown``, ``-1``, ``x``, ``no_entry``, ``confused``,
                 ``disappointed``, ``cry``, ``rage``
    neutral   — reserved (no emoji currently maps to it; text-feedback in D2+
                 may emit this signal for ``remember/always/never`` text that's
                 informational rather than corrective)

The ``positive_rate`` field is positive / (positive + negative + neutral), so
a zero-feedback portco won't appear in the result set at all (it's grouped out
by the SQL HAVING clause). Total is always ≥ 1 for any returned row.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import db_adapter

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────
# Shared helpers
# ──────────────────────────────────────────────────────────────────────────


def _window_start(window_days: int, *, now: Optional[datetime] = None) -> datetime:
    """Return the inclusive lower bound of the window (UTC, timezone-aware).

    ``window_days`` is clamped to ``>= 1`` so a caller passing 0/negative still
    gets a sane 1-day window rather than an empty result set or SQL error.
    """
    days = max(1, int(window_days))
    n = now or datetime.now(timezone.utc)
    return n - timedelta(days=days)


def _fetch_rows(sql: str, params: tuple) -> list[dict]:
    """Run a SELECT and return a list of dicts. Empty list on any failure.

    Mirrors ``cost_queries._fetch_rows`` so the two read-side modules behave
    identically under DB outage / missing DATABASE_URL.
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
        log.exception("feedback_aggregate: DB query failed (sql=%s)", sql[:80])
        return []


def _positive_rate(positive: int, negative: int, neutral: int) -> float:
    """Compute positive / total. Returns 0.0 when total is 0 (defensive — the
    SQL HAVING clause should already exclude zero-total rows)."""
    total = positive + negative + neutral
    if total <= 0:
        return 0.0
    return round(positive / total, 4)


# ──────────────────────────────────────────────────────────────────────────
# Aggregations
# ──────────────────────────────────────────────────────────────────────────


def aggregate_by_portco(
    window_days: int = 7,
    *,
    now: Optional[datetime] = None,
) -> list[dict]:
    """Per-portco signal counts over the window.

    Returns rows shaped like::

        {"portco_key": "acme",
         "positive_count": 12,
         "negative_count": 3,
         "neutral_count": 0,
         "total": 15,
         "positive_rate": 0.8,
         "last_event_at": datetime(...)}

    Sorted by ``positive_rate ASC`` — noisy/struggling portcos surface first.
    Empty portco_key rolls up as ``(unknown)`` so the row isn't lost.
    """
    start = _window_start(window_days, now=now)
    sql = """
        SELECT COALESCE(NULLIF(portco_key, ''), '(unknown)') AS portco_key,
               SUM(CASE WHEN signal = 'positive' THEN 1 ELSE 0 END)::int AS positive_count,
               SUM(CASE WHEN signal = 'negative' THEN 1 ELSE 0 END)::int AS negative_count,
               SUM(CASE WHEN signal = 'neutral'  THEN 1 ELSE 0 END)::int AS neutral_count,
               COUNT(*)::int AS total,
               MAX(ts) AS last_event_at
        FROM feedback_events
        WHERE ts >= %s
        GROUP BY COALESCE(NULLIF(portco_key, ''), '(unknown)')
        HAVING COUNT(*) > 0
    """
    rows = _fetch_rows(sql, (start,))
    for r in rows:
        r["positive_rate"] = _positive_rate(
            int(r.get("positive_count") or 0),
            int(r.get("negative_count") or 0),
            int(r.get("neutral_count") or 0),
        )
    rows.sort(key=lambda r: (r["positive_rate"], -int(r.get("total") or 0)))
    return rows


def aggregate_by_agent(
    window_days: int = 7,
    *,
    now: Optional[datetime] = None,
) -> list[dict]:
    """Per-agent signal counts over the window.

    Joins ``feedback_events`` to ``session_costs`` on ``thread_ts`` to recover
    the ``agent_id`` attribution. Feedback events with no matching session row
    (e.g. reactions on bot posts that pre-date the cost ledger, or on system
    messages) bucket into ``(unattributed)`` so the row isn't dropped.

    When a thread has multiple sessions (follow-ups reuse the session id), we
    pick the latest session via ``DISTINCT ON`` — the agent that most recently
    spoke to the thread owns the feedback.

    Same shape as ``aggregate_by_portco`` but keyed by ``agent_id``.
    """
    start = _window_start(window_days, now=now)
    sql = """
        WITH latest_session AS (
            SELECT DISTINCT ON (thread_ts) thread_ts, agent_id
            FROM session_costs
            WHERE thread_ts IS NOT NULL AND thread_ts <> ''
            ORDER BY thread_ts, recorded_at DESC
        )
        SELECT COALESCE(NULLIF(ls.agent_id, ''), '(unattributed)') AS agent_id,
               SUM(CASE WHEN fe.signal = 'positive' THEN 1 ELSE 0 END)::int AS positive_count,
               SUM(CASE WHEN fe.signal = 'negative' THEN 1 ELSE 0 END)::int AS negative_count,
               SUM(CASE WHEN fe.signal = 'neutral'  THEN 1 ELSE 0 END)::int AS neutral_count,
               COUNT(*)::int AS total,
               MAX(fe.ts) AS last_event_at
        FROM feedback_events fe
        LEFT JOIN latest_session ls
            ON ls.thread_ts = fe.thread_ts
        WHERE fe.ts >= %s
        GROUP BY COALESCE(NULLIF(ls.agent_id, ''), '(unattributed)')
        HAVING COUNT(*) > 0
    """
    rows = _fetch_rows(sql, (start,))
    for r in rows:
        r["positive_rate"] = _positive_rate(
            int(r.get("positive_count") or 0),
            int(r.get("negative_count") or 0),
            int(r.get("neutral_count") or 0),
        )
    rows.sort(key=lambda r: (r["positive_rate"], -int(r.get("total") or 0)))
    return rows


def aggregate_by_trigger(
    window_days: int = 7,
    *,
    now: Optional[datetime] = None,
) -> list[dict]:
    """Per-trigger signal counts over the window.

    Same join pattern as ``aggregate_by_agent`` but groups on
    ``session_costs.trigger`` (slack_mention, cron, recovery, …). Useful for
    spotting whether scheduled/cron-driven posts get worse reception than
    user-prompted ones.
    """
    start = _window_start(window_days, now=now)
    sql = """
        WITH latest_session AS (
            SELECT DISTINCT ON (thread_ts) thread_ts, trigger
            FROM session_costs
            WHERE thread_ts IS NOT NULL AND thread_ts <> ''
            ORDER BY thread_ts, recorded_at DESC
        )
        SELECT COALESCE(NULLIF(ls.trigger, ''), '(unattributed)') AS trigger,
               SUM(CASE WHEN fe.signal = 'positive' THEN 1 ELSE 0 END)::int AS positive_count,
               SUM(CASE WHEN fe.signal = 'negative' THEN 1 ELSE 0 END)::int AS negative_count,
               SUM(CASE WHEN fe.signal = 'neutral'  THEN 1 ELSE 0 END)::int AS neutral_count,
               COUNT(*)::int AS total,
               MAX(fe.ts) AS last_event_at
        FROM feedback_events fe
        LEFT JOIN latest_session ls
            ON ls.thread_ts = fe.thread_ts
        WHERE fe.ts >= %s
        GROUP BY COALESCE(NULLIF(ls.trigger, ''), '(unattributed)')
        HAVING COUNT(*) > 0
    """
    rows = _fetch_rows(sql, (start,))
    for r in rows:
        r["positive_rate"] = _positive_rate(
            int(r.get("positive_count") or 0),
            int(r.get("negative_count") or 0),
            int(r.get("neutral_count") or 0),
        )
    rows.sort(key=lambda r: (r["positive_rate"], -int(r.get("total") or 0)))
    return rows


def top_negative_recent(
    limit: int = 5,
    window_days: int = 7,
    *,
    now: Optional[datetime] = None,
) -> list[dict]:
    """Most-recent negative-signal events for hands-on drill-down.

    Returns rows shaped like::

        {"ts": datetime(...),
         "portco_key": "acme",
         "channel_id": "C0123",
         "thread_ts": "1234567890.123",
         "agent_message_ts": "1234567890.456",
         "channel_link": "<#C0123>",     # Slack mrkdwn channel link
         "user_id": "U0987",
         "raw_text": "thumbsdown"}

    Sorted ``ts DESC``. The ``channel_link`` field is purely a render-side
    convenience — pre-formatted Slack mrkdwn so the slash-command output can
    drop it in without re-templating per row.
    """
    start = _window_start(window_days, now=now)
    capped = max(1, min(int(limit or 5), 50))  # hard cap at 50 to keep mrkdwn small
    sql = """
        SELECT ts,
               COALESCE(NULLIF(portco_key, ''), '(unknown)') AS portco_key,
               channel_id,
               thread_ts,
               agent_message_ts,
               user_id,
               raw_text
        FROM feedback_events
        WHERE signal = 'negative' AND ts >= %s
        ORDER BY ts DESC
        LIMIT %s
    """
    rows = _fetch_rows(sql, (start, capped))
    for r in rows:
        ch = (r.get("channel_id") or "").strip()
        # Slack channel mention syntax: <#C12345>. Falls back to the literal
        # id when empty (still useful for log scans, just not clickable).
        r["channel_link"] = f"<#{ch}>" if ch else ""
    return rows


# ──────────────────────────────────────────────────────────────────────────
# Slack mrkdwn rendering — used by /feedback in slack_bot.py
# ──────────────────────────────────────────────────────────────────────────


_VALID_VIEWS = {"portco", "agent", "trigger", "negative"}


def parse_feedback_args(text: str) -> dict:
    """Parse the raw slash-command text into a structured spec.

    Accepts any whitespace-separated combo of:
      * a view token (``portco`` / ``agent`` / ``trigger`` / ``negative``) —
        defaults to ``portco``
      * a window override (integer day count, e.g. ``30``) — defaults to 7
      * any unknown token: ignored (forward-compat)

    Order does not matter — ``/feedback agent 30`` and ``/feedback 30 agent``
    are equivalent. ``/feedback 30`` keeps the default ``portco`` view.

    Returns::

        {"view": "portco" | "agent" | "trigger" | "negative",
         "window_days": int,
         "raw": str}
    """
    raw = (text or "").strip()
    tokens = raw.lower().split()

    view = "portco"
    window_days = 7
    for tok in tokens:
        if tok in _VALID_VIEWS:
            view = tok
            continue
        # Integer day-count override.
        try:
            n = int(tok)
            if n > 0:
                window_days = n
        except ValueError:
            pass  # ignore unknown tokens

    return {"view": view, "window_days": window_days, "raw": raw}


def _fmt_rate(rate: float) -> str:
    """Format a 0-1 rate as ``XX%`` (no decimals). Edge case: 100% on small
    samples is honest — the row also includes total so the reader can judge."""
    return f"{int(round(rate * 100))}%"


def _fmt_ts(ts) -> str:
    """Render a timestamp as ``YYYY-MM-DD HH:MM UTC`` for the drill-down view.

    Accepts a ``datetime`` (preferred) or a string (pass-through). Returns an
    empty string for None — defensive against rows where the DB column was
    NULL despite the DEFAULT NOW() clause.
    """
    if ts is None:
        return ""
    if isinstance(ts, datetime):
        return ts.strftime("%Y-%m-%d %H:%M UTC")
    return str(ts)


def _render_by_x(view: str, rows: list[dict], window_days: int) -> str:
    """Shared renderer for the three "by-X" aggregates.

    ``view`` is one of ``portco`` / ``agent`` / ``trigger`` — used for the
    header label and the row-key column name. All three views share the same
    five count/rate columns so we keep one renderer for them.
    """
    key_col = {
        "portco": "portco_key",
        "agent": "agent_id",
        "trigger": "trigger",
    }[view]
    label = {
        "portco": "portco",
        "agent": "agent",
        "trigger": "trigger",
    }[view]

    header = f"*Feedback by {label} — last {window_days} day(s)*"

    if not rows:
        return f"{header}\n\n_no feedback recorded yet in window_"

    # Slack mrkdwn doesn't render pipe tables, but a fixed-width code block
    # does. Mirrors what cost_queries does for the equivalent surface.
    lines: list[str] = [header, "", "```"]
    lines.append(
        f"{'KEY':<28} {'POS':>4} {'NEG':>4} {'NEU':>4} {'TOTAL':>6} {'RATE':>5}"
    )
    lines.append("-" * 60)
    for r in rows:
        key = str(r.get(key_col) or "(unknown)")[:28]
        pos = int(r.get("positive_count") or 0)
        neg = int(r.get("negative_count") or 0)
        neu = int(r.get("neutral_count") or 0)
        tot = int(r.get("total") or 0)
        rate = float(r.get("positive_rate") or 0.0)
        lines.append(
            f"{key:<28} {pos:>4} {neg:>4} {neu:>4} {tot:>6} {_fmt_rate(rate):>5}"
        )
    lines.append("```")
    return "\n".join(lines)


def _render_negative(rows: list[dict], window_days: int) -> str:
    """Drill-down renderer for ``/feedback negative``."""
    header = f"*Top negative feedback — last {window_days} day(s)*"
    if not rows:
        return f"{header}\n\n_no negative feedback recorded yet in window_"

    lines: list[str] = [header, ""]
    for r in rows:
        ts_label = _fmt_ts(r.get("ts"))
        portco = r.get("portco_key") or "(unknown)"
        channel = r.get("channel_link") or r.get("channel_id") or ""
        raw = r.get("raw_text") or ""
        user = r.get("user_id") or ""
        user_label = f"<@{user}>" if user else "_unknown user_"
        # One bullet per event. Compact: timestamp · portco · channel · who
        # reacted · the literal emoji. The agent_message_ts isn't surfaced
        # in v1 — Slack doesn't render deep-links from raw ts in mrkdwn,
        # and the channel link is enough to jump and scroll.
        lines.append(
            f"• {ts_label} — *{portco}* in {channel} — {user_label} reacted `:{raw}:`"
        )
    return "\n".join(lines)


def render_not_configured() -> str:
    """Friendly message when DATABASE_URL is unset (degraded mode)."""
    return (
        "*Feedback tracking not configured.*\n\n"
        "The `/feedback` command requires `DATABASE_URL` to be set so the "
        "feedback ledger is reachable. Local-only deploys do not persist "
        "feedback signals."
    )


def handle_feedback_command(text: str, *, now: Optional[datetime] = None) -> str:
    """Parse args, run aggregation, and return the rendered mrkdwn message.

    This is the entire ``/feedback`` flow minus the Slack adapter. ``slack_bot``
    just calls this and passes the result to ``respond()``.

    No exceptions escape — DB failures and missing data render as informational
    messages rather than crashing the Bolt event loop. ``slack_bot`` adds an
    outer try/except as belt-and-suspenders.
    """
    if not db_adapter.DATABASE_URL:
        return render_not_configured()

    args = parse_feedback_args(text)
    window_days = args["window_days"]
    view = args["view"]

    if view == "portco":
        rows = aggregate_by_portco(window_days, now=now)
        return _render_by_x("portco", rows, window_days)
    if view == "agent":
        rows = aggregate_by_agent(window_days, now=now)
        return _render_by_x("agent", rows, window_days)
    if view == "trigger":
        rows = aggregate_by_trigger(window_days, now=now)
        return _render_by_x("trigger", rows, window_days)
    if view == "negative":
        rows = top_negative_recent(window_days=window_days, now=now)
        return _render_negative(rows, window_days)

    # Unreachable: parse_feedback_args clamps view to _VALID_VIEWS. If it
    # ever happens, fall back to the default rather than 500.
    rows = aggregate_by_portco(window_days, now=now)
    return _render_by_x("portco", rows, window_days)
