"""Cost rollup queries — the read-side of Plan #35's cost ledger.

This module powers the ``/cost`` Slack slash command and any future reporting
surface that needs aggregated views of ``session_costs`` + ``messages_api_calls``.

Three rollup dimensions, all over a [start_date, end_date] inclusive window:

  * ``rollup_by_portco``  — per-portco totals from session_costs, plus a
                            synthetic ``(messages-api)`` row for non-session traffic.
  * ``rollup_by_trigger`` — per-trigger totals (slack_mention, cron, recovery, …).
                            Session-side only (messages-api has no trigger).
  * ``rollup_by_model``   — per-model totals across both ledgers, summed.

All three return ``list[dict]`` sorted by ``cost_usd DESC``. Empty list when
DATABASE_URL is unset — callers render a graceful "cost tracking not configured"
message rather than crash.

The command parser + formatter live here too, separated from ``slack_bot.py`` so
they can be unit-tested without touching the Bolt app.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

import db_adapter
from cost_collector import compute_reconciliation

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────
# Argument parsing
# ──────────────────────────────────────────────────────────────────────────

VALID_WINDOWS = {"today", "week", "month"}
WINDOW_DAYS = {"today": 1, "week": 7, "month": 30}


def parse_cost_args(text: str) -> dict:
    """Parse the raw slash-command text into a structured spec.

    Accepts any whitespace-separated combo of:
      * a window token (``today`` / ``week`` / ``month``) — defaults to ``today``
      * a portco key (anything else) — defaults to ``None`` (all portcos)
      * the literal ``reconcile`` for the drift report

    Order does not matter — ``/cost week acme`` and ``/cost acme week``
    are equivalent. Unknown tokens are treated as portco names; the SQL query
    just returns zero rows for unknown portcos.

    Returns::

        {"mode": "rollup" | "reconcile",
         "window": "today" | "week" | "month",
         "portco": str | None,
         "raw": str}
    """
    raw = (text or "").strip()
    tokens = raw.lower().split()

    if "reconcile" in tokens:
        return {"mode": "reconcile", "window": "today", "portco": None, "raw": raw}

    window: str = "today"
    portco: Optional[str] = None
    for tok in tokens:
        if tok in VALID_WINDOWS:
            window = tok
        elif tok:
            portco = tok  # last non-window token wins

    return {"mode": "rollup", "window": window, "portco": portco, "raw": raw}


def window_dates(window: str, today: Optional[date] = None) -> tuple[date, date]:
    """Resolve a window keyword to an inclusive (start, end) date pair.

    Uses ``today`` (default = ``date.today()``) as the upper bound. The lower
    bound is ``today`` for ``today``, ``today - 6`` for ``week`` (7 days
    inclusive), ``today - 29`` for ``month`` (30 days inclusive). These match
    the user-facing labels in the rendered output.
    """
    end = today or date.today()
    days = WINDOW_DAYS.get(window, 1)
    start = end - timedelta(days=days - 1)
    return start, end


# ──────────────────────────────────────────────────────────────────────────
# Rollup queries
# ──────────────────────────────────────────────────────────────────────────


def _fetch_rows(sql: str, params: tuple) -> list[dict]:
    """Run a SELECT and return a list of dicts. Empty list on any failure."""
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
        log.exception("cost_queries: DB query failed (sql=%s)", sql[:80])
        return []


def rollup_by_portco(
    start_date: date,
    end_date: date,
    *,
    portco: Optional[str] = None,
) -> list[dict]:
    """Aggregate spend by portco over [start_date, end_date].

    Joins session_costs (attribution-rich) with a synthetic ``(messages-api)``
    bucket sourced from messages_api_calls (non-session traffic — self_heal,
    self_improve). Cache hit % is computed across the session rows only.

    When ``portco`` is given, the filter is applied to the session_costs side;
    the messages-api row is omitted (its traffic is not portco-attributed).

    Returns rows shaped like::

        {"portco": "acme",
         "cost_usd": 4.21,
         "sessions": 47,
         "cache_pct": 92.1}

    Rows are sorted by ``cost_usd DESC``.
    """
    portco_filter = ""
    params_list: list = [start_date, end_date]
    if portco:
        portco_filter = "AND portco_key = %s"
        params_list.append(portco)

    sessions_sql = f"""
        SELECT COALESCE(portco_key, '(none)') AS portco,
               SUM(cost_usd)::float AS cost_usd,
               COUNT(*) AS sessions,
               CASE WHEN SUM(input_tokens + cache_read_tokens
                            + cache_write_5m_tokens + cache_write_1h_tokens) > 0
                    THEN ROUND(
                        100.0 * SUM(cache_read_tokens)::numeric
                        / NULLIF(SUM(input_tokens + cache_read_tokens
                                  + cache_write_5m_tokens + cache_write_1h_tokens), 0),
                        1)::float
                    ELSE NULL END AS cache_pct
        FROM session_costs
        WHERE recorded_at::date BETWEEN %s AND %s
        {portco_filter}
        GROUP BY COALESCE(portco_key, '(none)')
    """
    session_rows = _fetch_rows(sessions_sql, tuple(params_list))

    messages_rows: list[dict] = []
    if not portco:
        messages_sql = """
            SELECT '(messages-api)' AS portco,
                   SUM(cost_usd)::float AS cost_usd,
                   COUNT(*) AS sessions,
                   NULL::float AS cache_pct
            FROM messages_api_calls
            WHERE recorded_at::date BETWEEN %s AND %s
            HAVING COUNT(*) > 0
        """
        messages_rows = _fetch_rows(messages_sql, (start_date, end_date))

    rows = session_rows + messages_rows
    rows.sort(key=lambda r: r.get("cost_usd") or 0, reverse=True)
    return rows


def rollup_by_trigger(
    start_date: date,
    end_date: date,
    *,
    portco: Optional[str] = None,
) -> list[dict]:
    """Aggregate spend by trigger (slack_mention, cron, recovery, …).

    Session-side only. messages_api_calls has no trigger column (those calls
    are cron-bound to self_heal/self_improve and don't share the taxonomy).

    Returns rows shaped like::

        {"trigger": "slack_mention",
         "cost_usd": 3.20,
         "sessions": 8}

    Rows sorted ``cost_usd DESC``.
    """
    portco_filter = ""
    params_list: list = [start_date, end_date]
    if portco:
        portco_filter = "AND portco_key = %s"
        params_list.append(portco)

    sql = f"""
        SELECT COALESCE(trigger, '(unknown)') AS trigger,
               SUM(cost_usd)::float AS cost_usd,
               COUNT(*) AS sessions
        FROM session_costs
        WHERE recorded_at::date BETWEEN %s AND %s
        {portco_filter}
        GROUP BY COALESCE(trigger, '(unknown)')
        ORDER BY SUM(cost_usd) DESC
    """
    return _fetch_rows(sql, tuple(params_list))


def rollup_by_model(
    start_date: date,
    end_date: date,
    *,
    portco: Optional[str] = None,
) -> list[dict]:
    """Aggregate spend by model across BOTH ledgers.

    Sums session_costs and messages_api_calls grouped by model. When ``portco``
    is given, only session_costs contribute (messages-api isn't portco-tagged).

    Returns rows shaped like::

        {"model": "claude-opus-4-8", "cost_usd": 4.80, "sessions": 12}

    Rows sorted ``cost_usd DESC``.
    """
    portco_filter = ""
    params_list: list = [start_date, end_date]
    if portco:
        portco_filter = "AND portco_key = %s"
        params_list.append(portco)

    session_sql = f"""
        SELECT model,
               SUM(cost_usd)::float AS cost_usd,
               COUNT(*) AS sessions
        FROM session_costs
        WHERE recorded_at::date BETWEEN %s AND %s
        {portco_filter}
        GROUP BY model
    """
    session_rows = _fetch_rows(session_sql, tuple(params_list))

    msg_rows: list[dict] = []
    if not portco:
        msg_sql = """
            SELECT model,
                   SUM(cost_usd)::float AS cost_usd,
                   COUNT(*) AS sessions
            FROM messages_api_calls
            WHERE recorded_at::date BETWEEN %s AND %s
            GROUP BY model
        """
        msg_rows = _fetch_rows(msg_sql, (start_date, end_date))

    merged: dict[str, dict] = {}
    for r in session_rows + msg_rows:
        model = r.get("model") or "(unknown)"
        entry = merged.setdefault(
            model, {"model": model, "cost_usd": 0.0, "sessions": 0}
        )
        entry["cost_usd"] += float(r.get("cost_usd") or 0)
        entry["sessions"] += int(r.get("sessions") or 0)

    rows = list(merged.values())
    rows.sort(key=lambda r: r["cost_usd"], reverse=True)
    return rows


# ──────────────────────────────────────────────────────────────────────────
# Rendering — Slack mrkdwn output
# ──────────────────────────────────────────────────────────────────────────


def _fmt_usd(amount: float) -> str:
    """Format a USD amount as ``$X.XX``. Sub-cent values round to $0.00."""
    return f"${amount:,.2f}"


def _window_label(window: str, start: date, end: date) -> str:
    """Human label for the window header (e.g. ``today (2026-05-11)``)."""
    if window == "today":
        return f"today ({end.isoformat()})"
    return f"last {WINDOW_DAYS[window]} days ({start.isoformat()} → {end.isoformat()})"


def render_rollup(
    *,
    window: str,
    start: date,
    end: date,
    portco: Optional[str],
    portco_rows: list[dict],
    trigger_rows: list[dict],
    model_rows: list[dict],
) -> str:
    """Render rollups into Slack mrkdwn matching the Plan #35 output shape."""
    scope = f" — {portco}" if portco else ""
    lines: list[str] = [f"*Cost{scope} — {_window_label(window, start, end)}*", ""]

    total = sum((r.get("cost_usd") or 0) for r in portco_rows)

    lines.append("*By portco:*")
    if portco_rows:
        for r in portco_rows:
            cache = (
                f", {r['cache_pct']:.0f}% cached"
                if r.get("cache_pct") is not None
                else ""
            )
            sessions = r.get("sessions") or 0
            label = "calls" if r["portco"] == "(messages-api)" else "sessions"
            lines.append(
                f"• {r['portco']}: {_fmt_usd(r['cost_usd'])} "
                f"({sessions} {label}{cache})"
            )
    else:
        lines.append("• _no spend in window_")

    lines.extend(["", "*By trigger:*"])
    if trigger_rows:
        for r in trigger_rows:
            lines.append(
                f"• {r['trigger']}: {_fmt_usd(r['cost_usd'])} "
                f"({r.get('sessions') or 0} sessions)"
            )
    else:
        lines.append("• _no sessions in window_")

    lines.extend(["", "*By model:*"])
    if model_rows:
        for r in model_rows:
            lines.append(
                f"• {r['model']}: {_fmt_usd(r['cost_usd'])} "
                f"({r.get('sessions') or 0} calls)"
            )
    else:
        lines.append("• _no calls in window_")

    lines.extend(["", f"_Total: {_fmt_usd(total)}_"])
    return "\n".join(lines)


def render_reconciliation(recon: dict) -> str:
    """Render the drift report. Pre-pends a watch/critical prefix when over thresholds.

    Thresholds (per Plan #35):
      * |drift_pct| > 10%  → ``:warning: Watch:`` prefix
      * |drift_pct| > 25%  → ``:rotating_light: CRITICAL — refresh MODEL_COSTS_PER_MTOK`` prefix

    Crit supersedes watch (single prefix). When drift_pct is None (no Anthropic
    data — typical for "today" before the morning cron runs), no prefix is added
    and the line reads ``Drift: n/a — Anthropic billing not yet available``.
    """
    date_str = recon.get("date", "")
    local = float(recon.get("local_total_usd") or 0)
    anthropic = float(recon.get("anthropic_total_usd") or 0)
    drift_usd = float(recon.get("drift_usd") or 0)
    drift_pct = recon.get("drift_pct")

    prefix = ""
    if drift_pct is not None:
        abs_pct = abs(drift_pct)
        if abs_pct > 0.25:
            prefix = ":rotating_light: CRITICAL — refresh MODEL_COSTS_PER_MTOK\n\n"
        elif abs_pct > 0.10:
            prefix = ":warning: Watch:\n\n"

    lines = [f"*Reconciliation — {date_str}*", ""]
    lines.append(f"Local estimate: {_fmt_usd(local)}")
    lines.append(f"Anthropic billing: {_fmt_usd(anthropic)}")
    if drift_pct is None:
        lines.append("Drift: n/a — Anthropic billing not yet available")
    else:
        sign = "+" if drift_usd >= 0 else ""
        pct_pct = drift_pct * 100
        sign_pct = "+" if pct_pct >= 0 else ""
        tolerance = (
            "within tolerance"
            if abs(drift_pct) <= 0.10
            else ("outside tolerance" if abs(drift_pct) <= 0.25 else "critical drift")
        )
        lines.append(
            f"Drift: {sign}{_fmt_usd(drift_usd)} "
            f"({sign_pct}{pct_pct:.1f}%) — {tolerance}"
        )

    return prefix + "\n".join(lines)


def render_not_configured() -> str:
    """Friendly message when DATABASE_URL is unset (degraded mode)."""
    return (
        "*Cost tracking not configured.*\n\n"
        "The `/cost` command requires `DATABASE_URL` to be set so the cost "
        "ledger is reachable. Local-only deploys do not persist cost data."
    )


# ──────────────────────────────────────────────────────────────────────────
# Top-level dispatcher — used by slack_bot.on_cost_command
# ──────────────────────────────────────────────────────────────────────────


def handle_cost_command(text: str, *, today: Optional[date] = None) -> str:
    """Parse args, run queries, and return the rendered mrkdwn message.

    This is the entire ``/cost`` flow minus the Slack adapter. ``slack_bot``
    just calls this and passes the result to ``respond()``.

    No exceptions escape — DB failures and missing data are rendered as
    informational messages rather than crashing the Bolt event loop.
    """
    if not db_adapter.DATABASE_URL:
        return render_not_configured()

    args = parse_cost_args(text)
    today = today or date.today()

    if args["mode"] == "reconcile":
        # Reconciliation is always for yesterday — that's when Anthropic
        # billing has settled.
        target = today - timedelta(days=1)
        try:
            recon = compute_reconciliation(target)
        except Exception:
            log.exception("cost: reconciliation failed")
            return f"*Reconciliation — {target.isoformat()}*\n\n_query failed_"
        return render_reconciliation(recon)

    start, end = window_dates(args["window"], today)
    portco = args["portco"]
    portco_rows = rollup_by_portco(start, end, portco=portco)
    trigger_rows = rollup_by_trigger(start, end, portco=portco)
    model_rows = rollup_by_model(start, end, portco=portco)
    return render_rollup(
        window=args["window"],
        start=start,
        end=end,
        portco=portco,
        portco_rows=portco_rows,
        trigger_rows=trigger_rows,
        model_rows=model_rows,
    )
