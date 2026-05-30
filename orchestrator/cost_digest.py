"""Daily cost DM digest — Plan #35 task #41.

Posts yesterday's spend summary to each admin user as a Slack DM. Wired into
``main.py`` as an APScheduler cron at 08:00 PT — one hour after the
``scheduled_reconcile_costs`` job (07:00 PT) so the drift number on the digest
matches whatever Slack-watch notice (if any) was already posted by the
reconciliation cron.

Message shape (per Plan #35 line 240):

    [optional watch line if |drift| > 10%]
    Cost - YYYY-MM-DD
    Total: $X.XX  (by-task split)
    By portco: <name $X.XX> ...
    Cache: NN% hit-rate
    Drift vs Anthropic billing: +N.N% (within tolerance) | n/a
    Top sessions
      $X.XX  task  portco  "title"  (thread T...)

Admin user list source: ``portco_registry.get_admin_user_ids()`` — sourced from
the top-level ``admin_user_ids`` key in ``portco_config.json``. No new env var
introduced; the existing config already covers the operator notification list.

Graceful degradation:
  * No ``DATABASE_URL`` -> emit a "cost tracking not configured" digest so the
    operator notices something is wrong rather than getting silence.
  * No admin users configured -> log a warning and return (caller doesn't crash).
  * Slack send failure per-user -> log and continue (one DM failure must not
    block the others).
  * No Anthropic data yet -> ``Drift: n/a``, no watch line.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

import compresr_telemetry
import db_adapter
import roi_signal
from cost_collector import compute_reconciliation

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────
# Query helpers — read directly off session_costs / messages_api_calls
# ──────────────────────────────────────────────────────────────────────────


def _fetch(sql: str, params: tuple) -> list[dict]:
    """Run a SELECT and return a list of dicts. Empty on failure / missing DB."""
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
        log.exception("cost_digest: DB query failed (sql=%s)", sql[:80])
        return []


def _portco_totals(target: date) -> list[dict]:
    """Per-portco spend for ``target`` (session_costs + a synthetic messages-api row)."""
    sess = _fetch(
        """
        SELECT COALESCE(portco_key, '(none)') AS portco,
               SUM(cost_usd)::float AS cost_usd,
               COUNT(*) AS sessions
        FROM session_costs
        WHERE recorded_at::date = %s
        GROUP BY COALESCE(portco_key, '(none)')
        """,
        (target,),
    )
    msgs = _fetch(
        """
        SELECT '(messages-api)' AS portco,
               SUM(cost_usd)::float AS cost_usd,
               COUNT(*) AS sessions
        FROM messages_api_calls
        WHERE recorded_at::date = %s
        HAVING COUNT(*) > 0
        """,
        (target,),
    )
    rows = sess + msgs
    rows.sort(key=lambda r: r.get("cost_usd") or 0, reverse=True)
    return rows


def _trigger_totals(target: date) -> list[dict]:
    """Per-task-type (``trigger`` column) spend for ``target``. Session-side only."""
    rows = _fetch(
        """
        SELECT COALESCE(trigger, '(unknown)') AS trigger,
               SUM(cost_usd)::float AS cost_usd,
               COUNT(*) AS sessions
        FROM session_costs
        WHERE recorded_at::date = %s
        GROUP BY COALESCE(trigger, '(unknown)')
        ORDER BY SUM(cost_usd) DESC
        """,
        (target,),
    )
    # Append messages-api as a synthetic "task" so the by-task split sums to the
    # same total as the by-portco split. Caller renders this last.
    msgs = _fetch(
        """
        SELECT 'messages-api' AS trigger,
               SUM(cost_usd)::float AS cost_usd,
               COUNT(*) AS sessions
        FROM messages_api_calls
        WHERE recorded_at::date = %s
        HAVING COUNT(*) > 0
        """,
        (target,),
    )
    return rows + msgs


def _cache_hit_pct(target: date) -> Optional[float]:
    """Weighted cache hit rate across all sessions on ``target``.

    Returns ``None`` if there were no sessions (so the renderer can omit the
    line cleanly rather than printing ``0.0%`` and looking like a failed
    deploy). Cache-read tokens are the numerator; total input + cache-read +
    cache-write are the denominator.
    """
    rows = _fetch(
        """
        SELECT
            SUM(cache_read_tokens)::bigint AS hit,
            SUM(input_tokens + cache_read_tokens
                + cache_write_5m_tokens + cache_write_1h_tokens)::bigint AS total
        FROM session_costs
        WHERE recorded_at::date = %s
        """,
        (target,),
    )
    if not rows:
        return None
    hit = rows[0].get("hit") or 0
    total = rows[0].get("total") or 0
    if not total:
        return None
    return 100.0 * float(hit) / float(total)


def _total_cost(target: date) -> float:
    """Combined local-ledger total (session_costs + messages_api_calls) for ``target``."""
    rows = _fetch(
        """
        SELECT (
          (SELECT COALESCE(SUM(cost_usd), 0)::float FROM session_costs
           WHERE recorded_at::date = %s)
          +
          (SELECT COALESCE(SUM(cost_usd), 0)::float FROM messages_api_calls
           WHERE recorded_at::date = %s)
        ) AS total
        """,
        (target, target),
    )
    if not rows:
        return 0.0
    return float(rows[0].get("total") or 0)


def _smoke_probe_7d_summary() -> Optional[dict]:
    """Trailing 7-day smoke-probe pass rate for the digest's lead line.

    Returns ``None`` when there are no probe rows in the window (fresh deploy,
    feature disabled). Otherwise a dict with ``pass_count``, ``total``,
    ``pass_pct``, and a list of recent failing deploy SHAs so the operator can
    click straight to the diff. Plan #42 PR2.
    """
    rows = _fetch(
        """
        SELECT
            COUNT(*) FILTER (WHERE passed) AS pass_count,
            COUNT(*) AS total
        FROM smoke_probe_runs
        WHERE started_at >= NOW() - INTERVAL '7 days'
        """,
        (),
    )
    if not rows or not rows[0].get("total"):
        return None
    pass_count = int(rows[0].get("pass_count") or 0)
    total = int(rows[0].get("total") or 0)
    pass_pct = 100.0 * pass_count / total if total else 0.0

    failing: list[str] = []
    if pass_count < total:
        fail_rows = _fetch(
            """
            SELECT COALESCE(deploy_sha, '(unknown)') AS deploy_sha
            FROM smoke_probe_runs
            WHERE passed = false
              AND started_at >= NOW() - INTERVAL '7 days'
            ORDER BY started_at DESC
            LIMIT 10
            """,
            (),
        )
        failing = [str(r["deploy_sha"])[:8] for r in fail_rows]
    return {
        "pass_count": pass_count,
        "total": total,
        "pass_pct": pass_pct,
        "failing_shas": failing,
    }


def _top_sessions(target: date, limit: int = 5) -> list[dict]:
    """Top-N most expensive sessions on ``target`` for the operator drill-down.

    Returns the trigger label, portco, cost, and thread_ts so an operator can
    click straight to the conversation in Slack. messages-api calls are
    intentionally not included — they're aggregate-only at the digest level.
    """
    return _fetch(
        """
        SELECT cost_usd::float AS cost_usd,
               COALESCE(trigger, '(unknown)') AS trigger,
               COALESCE(portco_key, '(none)') AS portco,
               session_id,
               thread_ts
        FROM session_costs
        WHERE recorded_at::date = %s
        ORDER BY cost_usd DESC
        LIMIT %s
        """,
        (target, limit),
    )


# ──────────────────────────────────────────────────────────────────────────
# Rendering
# ──────────────────────────────────────────────────────────────────────────


WATCH_THRESHOLD = 0.10  # |drift_pct| > 10% -> lead with a watch line


def _fmt_usd(amount: float) -> str:
    return f"${amount:,.2f}"


def _by_task_summary(trigger_rows: list[dict]) -> str:
    """Inline ``$X.XX category / $X.XX category`` summary appended to the Total line."""
    if not trigger_rows:
        return ""
    parts = [f"{_fmt_usd(r.get('cost_usd') or 0)} {r['trigger']}" for r in trigger_rows]
    return " (" + " / ".join(parts) + ")"


def _drift_line(recon: dict) -> tuple[str, bool]:
    """Build the ``Drift vs Anthropic billing`` line and a watch-needed flag.

    Returns ``(line, needs_watch)`` where ``needs_watch`` is True when
    ``|drift_pct| > 10%`` — the digest leads with a one-line watch banner in
    that case (mirrors the reconciliation Slack notice).
    """
    drift_pct = recon.get("drift_pct")
    local = float(recon.get("local_total_usd") or 0)
    anthropic = float(recon.get("anthropic_total_usd") or 0)

    if drift_pct is None:
        if anthropic == 0 and local == 0:
            return "Drift vs Anthropic billing: n/a (no spend recorded)", False
        return (
            "Drift vs Anthropic billing: n/a (Anthropic billing not yet available)",
            False,
        )

    pct = drift_pct * 100
    sign = "+" if pct >= 0 else ""
    abs_pct = abs(drift_pct)
    tolerance = (
        "within tolerance"
        if abs_pct <= 0.10
        else ("outside tolerance" if abs_pct <= 0.25 else "critical drift")
    )
    line = (
        f"Drift vs Anthropic billing: {sign}{pct:.1f}% ({tolerance}) — "
        f"local {_fmt_usd(local)} vs Anthropic {_fmt_usd(anthropic)}"
    )
    return line, abs_pct > WATCH_THRESHOLD


def _render_smoke_probe_line(summary: Optional[dict]) -> Optional[str]:
    """Render the ``[SMOKE PROBE] 7d pass rate ...`` line for the digest.

    Returns ``None`` when there is no probe data (fresh deploy, feature
    disabled) so the caller can omit the section entirely. Plan #42 PR2.
    """
    if not summary or not summary.get("total"):
        return None
    pass_count = summary["pass_count"]
    total = summary["total"]
    pct = summary["pass_pct"]
    failing = summary.get("failing_shas") or []
    if pass_count == total:
        return f"[SMOKE PROBE] 7d pass rate: {pass_count}/{total} (100%) — clean week"
    failing_blob = ""
    if failing:
        failing_blob = f" — failing SHAs: {', '.join(failing)}"
    return (
        f"[SMOKE PROBE] 7d pass rate: {pass_count}/{total} ({pct:.0f}%){failing_blob}"
    )


def build_digest_message(
    target: date,
    *,
    total: float,
    portco_rows: list[dict],
    trigger_rows: list[dict],
    cache_pct: Optional[float],
    recon: dict,
    top_sessions: list[dict],
    compresr_summary: Optional[dict] = None,
    roi: Optional[dict] = None,
    smoke_probe_summary: Optional[dict] = None,
) -> str:
    """Render the digest body. Pure function — no DB, no Slack.

    Kept narrow so tests can pin every branch (watch line on/off, drift n/a,
    no spend, missing cache, no top sessions, compression block on/off,
    ROI block on/off, smoke probe line on/off).

    ``compresr_summary`` is the dict returned by
    ``compresr_telemetry.summarize()``. When ``None`` or empty
    (``total_calls == 0``), the compression block is omitted entirely — same
    pattern as the existing "no top sessions" path. This keeps the digest
    quiet when ``COMPRESR_API_KEY`` is unset or every call-site flag is off.

    ``roi`` is the dict returned by ``roi_signal.compute_roi(window_days=7)``.
    When ``None`` or when ``roi["by_portco"]`` is empty, the ROI block is
    omitted. Same gating pattern as ``compresr_summary``. Plan #30 D3.

    ``smoke_probe_summary`` is the dict returned by ``_smoke_probe_7d_summary``.
    When ``None`` the smoke-probe lead line is omitted. Plan #42 PR2 — the
    line prepends the digest so an operator scanning DMs sees deploy health
    before spend metrics.
    """
    lines: list[str] = []
    smoke_line = _render_smoke_probe_line(smoke_probe_summary)
    if smoke_line:
        lines.append(smoke_line)
        lines.append("")

    drift_line, needs_watch = _drift_line(recon)

    if needs_watch:
        drift_pct = recon.get("drift_pct") or 0.0
        direction = "under-estimated" if drift_pct > 0 else "over-estimated"
        lines.append(
            f":warning: Watch — local cost {direction} by "
            f"{abs(drift_pct) * 100:.1f}% vs Anthropic billing."
        )
        lines.append("")

    lines.append(f"*Cost — {target.isoformat()}*")
    lines.append(f"Total: {_fmt_usd(total)}{_by_task_summary(trigger_rows)}")

    if portco_rows:
        portco_summary = " · ".join(
            f"{r['portco']} {_fmt_usd(r.get('cost_usd') or 0)}" for r in portco_rows
        )
        lines.append(f"By portco: {portco_summary}")
    else:
        lines.append("By portco: _no spend recorded_")

    if cache_pct is not None:
        verdict = "good" if cache_pct >= 60 else ("ok" if cache_pct >= 30 else "low")
        lines.append(f"Cache: {cache_pct:.0f}% hit-rate ({verdict})")
    else:
        lines.append("Cache: n/a (no sessions)")

    lines.append(drift_line)

    # Compression savings block (Plan #37 task #66) — sits between the cost
    # rollup lines and the top-sessions drill-down so all spend metrics group
    # together. Renders an empty string and is appended as nothing when there
    # were no compresr_calls rows in the trailing window.
    compresr_block = compresr_telemetry.render_digest_block(compresr_summary or {})
    if compresr_block:
        lines.append("")
        lines.append(compresr_block)

    # ROI block (Plan #30 D3) — "cost per useful answer" rollup. Sits after
    # the spend metrics so an operator reads dollars-out first, then dollars-
    # per-useful-answer. ``render_digest_block`` returns "" when there's no
    # by-portco data, which mirrors the compresr pattern above.
    roi_block = roi_signal.render_digest_block(roi or {})
    if roi_block:
        lines.append("")
        lines.append(roi_block)

    if top_sessions:
        lines.append("")
        lines.append("*Top sessions:*")
        for row in top_sessions:
            cost = _fmt_usd(row.get("cost_usd") or 0)
            trig = row.get("trigger") or "(unknown)"
            portco = row.get("portco") or "(none)"
            thread = row.get("thread_ts")
            session_id = row.get("session_id") or ""
            ref = f"thread {thread}" if thread else f"session {session_id}"
            lines.append(f"  {cost}  {trig}  {portco}  ({ref})")

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────
# Send loop
# ──────────────────────────────────────────────────────────────────────────


def _resolve_admin_ids() -> list[str]:
    """Look up admin Slack user IDs.

    Source preference (first hit wins):
      1. ``config.ADMIN_USER_IDS`` (comma-separated env var; future-proof if
         someone adds it later).
      2. ``config.SLACK_ADMIN_USER_ID`` (single-user fallback).
      3. ``portco_registry.get_admin_user_ids()`` — the existing source-of-truth
         (top-level ``admin_user_ids`` key in ``portco_config.json``).

    Returns an empty list when nothing is configured; the caller logs a warning
    and exits cleanly.
    """
    try:
        import config

        env_list = getattr(config, "ADMIN_USER_IDS", None)
        if env_list:
            if isinstance(env_list, str):
                return [u.strip() for u in env_list.split(",") if u.strip()]
            return [u for u in env_list if u]
        single = getattr(config, "SLACK_ADMIN_USER_ID", None)
        if single:
            return [single]
    except Exception:  # pragma: no cover - defensive
        log.debug("cost_digest: config import failed; falling back to portco_registry")

    try:
        from portco_registry import get_admin_user_ids

        return get_admin_user_ids()
    except Exception:
        log.exception("cost_digest: portco_registry admin lookup failed")
        return []


def _default_target_date() -> date:
    """Yesterday (PT-aligned via local ``date.today()``)."""
    return date.today() - timedelta(days=1)


def send_daily_cost_digest(
    target_date: Optional[date] = None,
    *,
    sender=None,
    admin_ids: Optional[list[str]] = None,
) -> dict:
    """Compose yesterday's cost digest and DM it to every admin user.

    Args:
        target_date: which UTC-day to summarize. Defaults to yesterday.
        sender: optional ``callable(user_id, text)`` for tests. Defaults to
            ``slack_bot.send_dm``. Imported lazily so this module can be loaded
            without Slack creds.
        admin_ids: optional admin user list override. Defaults to
            ``_resolve_admin_ids()``.

    Returns:
        Dict with the rendered message and per-user delivery counts so the
        cron wrapper can log a one-line summary::

            {"date": "YYYY-MM-DD",
             "message": "...",
             "recipients": ["U..."],
             "sent": int,
             "failed": int,
             "skipped_reason": str | None}

    Never raises — DB / Slack failures degrade to a logged warning so the
    APScheduler thread keeps running.
    """
    target = target_date or _default_target_date()

    admins = admin_ids if admin_ids is not None else _resolve_admin_ids()
    if not admins:
        log.warning(
            "send_daily_cost_digest: no admin users configured — "
            "skipping digest for %s (set admin_user_ids in portco_config.json)",
            target.isoformat(),
        )
        return {
            "date": target.isoformat(),
            "message": "",
            "recipients": [],
            "sent": 0,
            "failed": 0,
            "skipped_reason": "no_admin_users",
        }

    # Resolve the Slack sender lazily — keeps this module importable from
    # scripts and tests without Slack creds.
    if sender is None:
        try:
            from slack_bot import send_dm as sender  # type: ignore
        except Exception:
            log.exception("send_daily_cost_digest: slack_bot import failed")
            sender = None

    # Render
    if not db_adapter.DATABASE_URL:
        message = (
            f"*Cost — {target.isoformat()}*\n\n"
            "_Cost tracking not configured (DATABASE_URL unset). The daily "
            "digest is degraded — no spend data persisted on this deploy._"
        )
    else:
        total = _total_cost(target)
        portco_rows = _portco_totals(target)
        trigger_rows = _trigger_totals(target)
        cache_pct = _cache_hit_pct(target)
        try:
            recon = compute_reconciliation(target)
        except Exception:
            log.exception("send_daily_cost_digest: reconciliation lookup failed")
            recon = {
                "date": target.isoformat(),
                "local_total_usd": total,
                "anthropic_total_usd": 0.0,
                "drift_usd": 0.0,
                "drift_pct": None,
            }
        top = _top_sessions(target, limit=5)
        # Plan #37 task #66 — Compresr per-call telemetry for the same day.
        # ``summarize`` swallows DB errors and returns a zero-row dict, so a
        # failing query never breaks the rest of the digest.
        try:
            compresr_summary = compresr_telemetry.summarize(days=1, target=target)
        except Exception:
            log.exception("send_daily_cost_digest: compresr telemetry lookup failed")
            compresr_summary = None
        # Plan #30 D3 — ROI signal over the trailing 7 days (a 1-day window
        # has too little feedback to be useful). compute_roi swallows DB
        # errors via its internal _fetch helper, so the worst case is an
        # empty by_portco list — the renderer then emits nothing.
        try:
            roi = roi_signal.compute_roi(window_days=7)
        except Exception:
            log.exception("send_daily_cost_digest: roi_signal lookup failed")
            roi = None
        # Plan #42 PR2 — smoke-probe 7d pass rate. Renders nothing when
        # there are no probe rows in the window (the table is fresh, or
        # SMOKE_PROBE_ENABLED has been false for a week).
        try:
            smoke_probe_summary = _smoke_probe_7d_summary()
        except Exception:
            log.exception("send_daily_cost_digest: smoke_probe summary failed")
            smoke_probe_summary = None
        message = build_digest_message(
            target,
            total=total,
            portco_rows=portco_rows,
            trigger_rows=trigger_rows,
            cache_pct=cache_pct,
            recon=recon,
            top_sessions=top,
            compresr_summary=compresr_summary,
            roi=roi,
            smoke_probe_summary=smoke_probe_summary,
        )

    sent = 0
    failed = 0
    if sender is None:
        log.warning(
            "send_daily_cost_digest: no Slack sender available — digest "
            "computed but not delivered (date=%s)",
            target.isoformat(),
        )
        return {
            "date": target.isoformat(),
            "message": message,
            "recipients": admins,
            "sent": 0,
            "failed": 0,
            "skipped_reason": "no_sender",
        }

    for uid in admins:
        try:
            sender(uid, message)
            sent += 1
        except Exception:
            log.exception(
                "send_daily_cost_digest: DM failed for user=%s date=%s",
                uid,
                target.isoformat(),
            )
            failed += 1

    log.info(
        "send_daily_cost_digest: date=%s recipients=%d sent=%d failed=%d",
        target.isoformat(),
        len(admins),
        sent,
        failed,
    )
    return {
        "date": target.isoformat(),
        "message": message,
        "recipients": admins,
        "sent": sent,
        "failed": failed,
        "skipped_reason": None,
    }
