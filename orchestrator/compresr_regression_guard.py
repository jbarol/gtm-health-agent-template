"""Per-call-site quality-regression guard for Compresr compression.

Plan #37, Task #67. Compresr compression is lossy — token deletion can drop
fields the downstream model needed to write valid JSON. The two consumers of
compressed output (``self_heal._analyze_session`` and
``self_improve._analyze_changes``) both ask the model for a JSON response, so
the cheapest, model-agnostic regression signal we have is whether that JSON
parses.

This module tracks downstream JSON parse outcomes per call site, maintains a
rolling 14-day baseline, and auto-disables compression for a call site when its
trailing-24h parse-failure rate exceeds ``2x`` the prior 14-day baseline. A
Slack watch notice is posted once per call site per day (deduped via the
``compresr_site_disabled`` table).

Public API
----------
- ``record_parse_outcome(call_site, parsed_ok) -> None``
  Call from ``self_heal._analyze_session`` and ``self_improve._analyze_changes``
  AFTER attempting to ``json.loads`` the model's reply. ``parsed_ok=True``
  means the JSON parsed; ``parsed_ok=False`` means the parse failed. The
  function reuses ``compresr_calls.downstream_ok`` to persist the outcome by
  stamping the most-recent non-fallback row for that call site.

- ``should_auto_disable(call_site) -> bool``
  Read the rolling stats from ``compresr_calls`` and return True iff the
  trailing-24h parse-failure rate is more than ``REGRESSION_MULTIPLIER`` times
  the prior 14-day baseline. Requires both windows to have at least
  ``MIN_SAMPLES`` rows to avoid tripping on tiny sample sizes.

- ``disable_call_site(call_site, reason) -> None``
  Insert a row into ``compresr_site_disabled`` (PK = ``call_site``) and post a
  Slack watch notice — deduped to once per call site per day via the
  ``disabled_at`` timestamp.

- ``is_disabled(call_site) -> bool``
  Checked by ``compresr_client.compress_prompt`` before compressing. Cheap
  one-row lookup. Returns False when DATABASE_URL is unset.

- ``run_regression_check() -> dict``
  Convenience helper for a cron job: iterates known call sites, runs
  ``should_auto_disable``, calls ``disable_call_site`` when triggered. Returns
  a summary suitable for logging.

Failure modes
-------------
All functions silently no-op when ``DATABASE_URL`` is unset, when the DB is
unreachable, or when the Slack notifier is unavailable. The guard is
observability — it must never crash the calling code.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Optional

import db_adapter

log = logging.getLogger(__name__)


# Call sites monitored by the guard. ``compresr_client.compress_prompt`` reads
# these via ``is_disabled`` before compressing.
KNOWN_CALL_SITES = ("self_heal", "self_improve")

# Threshold math:
#   * 24h window vs prior 14-day baseline.
#   * Auto-disable when 24h rate > REGRESSION_MULTIPLIER * baseline rate.
#   * Require MIN_SAMPLES rows in each window to avoid tripping on noise.
REGRESSION_MULTIPLIER = 2.0
BASELINE_DAYS = 14
RECENT_HOURS = 24
MIN_SAMPLES = 10


# ──────────────────────────────────────────────────────────────────────────
# DB helpers (best-effort; never raise to caller)
# ──────────────────────────────────────────────────────────────────────────


def _connect():
    """Return a psycopg2 connection or raise — caller wraps in try/except."""
    return db_adapter._connect()


def record_parse_outcome(call_site: str, parsed_ok: bool) -> None:
    """Stamp the most recent non-fallback compresr_calls row for ``call_site``
    with the downstream JSON-parse outcome.

    Called from ``self_heal._analyze_session`` and
    ``self_improve._analyze_changes`` immediately after they attempt to
    ``json.loads`` the model's reply. We reuse the existing
    ``compresr_calls.downstream_ok`` column instead of introducing a new table
    so the regression query is one JOIN-free aggregation.

    Best-effort: missing DB or write failure logs at debug and returns. The
    guard fails open — better to miss a regression signal than to break the
    calling code path.
    """
    if not db_adapter.DATABASE_URL:
        return
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                # Update the latest non-fallback row for this call site. If the
                # caller had compression disabled (or the prompt was below
                # min_chars), there's no row to stamp and the parse outcome is
                # not regression-relevant — we only care about JSON parses
                # downstream of an *actually compressed* prompt.
                cur.execute(
                    """
                    UPDATE compresr_calls
                       SET downstream_ok = %s
                     WHERE id = (
                         SELECT id FROM compresr_calls
                          WHERE call_site = %s
                            AND fallback IS FALSE
                            AND downstream_ok IS NULL
                          ORDER BY created_at DESC
                          LIMIT 1
                     )
                    """,
                    (parsed_ok, call_site),
                )
                conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.debug(
            "compresr_regression_guard: record_parse_outcome failed for %s: %s",
            call_site,
            e,
        )


def _failure_rate(
    call_site: str,
    *,
    window_start_sql: str,
    window_end_sql: str,
) -> tuple[int, int, Optional[float]]:
    """Return (total, failures, rate) over a window for ``call_site``.

    A row counts toward the window iff:
      * ``call_site`` matches AND
      * ``fallback IS FALSE`` (compression actually ran) AND
      * ``downstream_ok IS NOT NULL`` (downstream actually attempted to parse)

    ``rate = failures / total`` when total > 0, else None.

    Window bounds are expressed in SQL so callers can pass either an interval
    string (e.g. ``NOW() - INTERVAL '24 hours'``) or an explicit timestamp.
    """
    if not db_adapter.DATABASE_URL:
        return (0, 0, None)
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT
                        COUNT(*)::int AS total,
                        SUM(CASE WHEN downstream_ok IS FALSE THEN 1 ELSE 0 END)::int
                            AS failures
                    FROM compresr_calls
                    WHERE call_site = %s
                      AND fallback IS FALSE
                      AND downstream_ok IS NOT NULL
                      AND created_at >= {window_start_sql}
                      AND created_at <  {window_end_sql}
                    """,
                    (call_site,),
                )
                row = cur.fetchone()
                if not row:
                    return (0, 0, None)
                total, failures = int(row[0] or 0), int(row[1] or 0)
                rate = (failures / total) if total > 0 else None
                return (total, failures, rate)
        finally:
            conn.close()
    except Exception as e:
        log.debug(
            "compresr_regression_guard: _failure_rate failed for %s: %s",
            call_site,
            e,
        )
        return (0, 0, None)


def compute_rates(call_site: str) -> dict:
    """Return both windows' totals + rates for ``call_site``.

    Useful for tests and operators. Schema::

        {
            "call_site": str,
            "recent": {"total": int, "failures": int, "rate": float | None},
            "baseline": {"total": int, "failures": int, "rate": float | None},
            "threshold": float | None,   # baseline.rate * REGRESSION_MULTIPLIER
            "trips": bool,               # True iff guard would fire
            "reason": str | None,        # why it didn't trip
        }
    """
    # Recent window: last RECENT_HOURS hours.
    recent_total, recent_fail, recent_rate = _failure_rate(
        call_site,
        window_start_sql=f"NOW() - INTERVAL '{RECENT_HOURS} hours'",
        window_end_sql="NOW()",
    )
    # Baseline window: prior BASELINE_DAYS days, ENDING at the start of the
    # recent window so the two windows do not overlap.
    baseline_total, baseline_fail, baseline_rate = _failure_rate(
        call_site,
        window_start_sql=(
            f"NOW() - INTERVAL '{BASELINE_DAYS} days' - INTERVAL '{RECENT_HOURS} hours'"
        ),
        window_end_sql=f"NOW() - INTERVAL '{RECENT_HOURS} hours'",
    )

    out = {
        "call_site": call_site,
        "recent": {
            "total": recent_total,
            "failures": recent_fail,
            "rate": recent_rate,
        },
        "baseline": {
            "total": baseline_total,
            "failures": baseline_fail,
            "rate": baseline_rate,
        },
        "threshold": None,
        "trips": False,
        "reason": None,
    }

    # Gate 1: enough samples in both windows. Below MIN_SAMPLES the rate is
    # noise — refuse to disable so a single bad day doesn't kill compression.
    if recent_total < MIN_SAMPLES:
        out["reason"] = "insufficient_recent_samples"
        return out
    if baseline_total < MIN_SAMPLES:
        out["reason"] = "insufficient_baseline_samples"
        return out

    # Gate 2: if the baseline has zero failures, anything in recent looks
    # infinitely bad. Use an absolute floor of 0.05 (5% failure rate) to avoid
    # disabling for the very first observed failure.
    if baseline_rate is None or baseline_rate == 0:
        if recent_rate is not None and recent_rate >= 0.05:
            out["threshold"] = 0.05
            out["trips"] = True
            out["reason"] = "baseline_zero_recent_nonzero"
        else:
            out["reason"] = "baseline_zero_recent_clean"
        return out

    # Gate 3: the canonical 2x check.
    threshold = baseline_rate * REGRESSION_MULTIPLIER
    out["threshold"] = threshold
    if recent_rate is not None and recent_rate > threshold:
        out["trips"] = True
        out["reason"] = "recent_exceeds_2x_baseline"
    else:
        out["reason"] = "within_threshold"
    return out


# ──────────────────────────────────────────────────────────────────────────
# Auto-disable (with per-day Slack dedup)
# ──────────────────────────────────────────────────────────────────────────


def should_auto_disable(call_site: str) -> bool:
    """Return True iff the regression check trips for ``call_site``.

    Thin wrapper over ``compute_rates`` so callers can chain
    ``if should_auto_disable(site): disable_call_site(site, ...)`` without
    threading the diagnostic dict through.
    """
    return bool(compute_rates(call_site).get("trips"))


def is_disabled(call_site: str) -> bool:
    """Return True iff ``call_site`` has a row in ``compresr_site_disabled``.

    Used by ``compresr_client.compress_prompt`` to short-circuit before any
    SDK or cache work. Cheap PK lookup — no aggregation, no scan.

    Failure modes:
      * ``DATABASE_URL`` unset → False (compression continues unconditionally).
      * DB unreachable → False, log at debug. Fail open: missing the disabled
        flag is less harmful than crashing the call site.
    """
    if not db_adapter.DATABASE_URL:
        return False
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM compresr_site_disabled WHERE call_site = %s",
                    (call_site,),
                )
                return cur.fetchone() is not None
        finally:
            conn.close()
    except Exception as e:
        log.debug(
            "compresr_regression_guard: is_disabled lookup failed for %s: %s",
            call_site,
            e,
        )
        return False


def _already_notified_today(call_site: str) -> bool:
    """Return True iff a row already exists for ``call_site`` AND its
    ``disabled_at`` is on or after the start of today (UTC).

    This is the Slack-notice dedup: we never want to spam admins more than once
    per day per call site even if the regression-check cron runs more often.
    """
    if not db_adapter.DATABASE_URL:
        return False
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1 FROM compresr_site_disabled
                     WHERE call_site = %s
                       AND disabled_at::date >= %s
                    """,
                    (call_site, date.today()),
                )
                return cur.fetchone() is not None
        finally:
            conn.close()
    except Exception as e:
        log.debug(
            "compresr_regression_guard: _already_notified_today failed for %s: %s",
            call_site,
            e,
        )
        return False


def _persist_disabled(call_site: str, reason: str) -> None:
    """Insert or refresh the ``compresr_site_disabled`` row.

    Primary key is ``call_site`` so we use ``ON CONFLICT DO UPDATE`` to refresh
    the timestamp and reason — a tripped site should always show the most
    recent diagnostic, not the first one.
    """
    if not db_adapter.DATABASE_URL:
        return
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO compresr_site_disabled
                        (call_site, disabled_at, reason)
                    VALUES (%s, NOW(), %s)
                    ON CONFLICT (call_site) DO UPDATE
                       SET disabled_at = EXCLUDED.disabled_at,
                           reason = EXCLUDED.reason
                    """,
                    (call_site, reason),
                )
                conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.debug(
            "compresr_regression_guard: _persist_disabled failed for %s: %s",
            call_site,
            e,
        )


def _post_slack_notice(call_site: str, reason: str, rates: dict) -> None:
    """DM admins about the auto-disable. Best-effort; logs and returns on any
    failure. Uses the same admin-resolution path as ``cost_digest`` so the
    notice goes to the same operators.
    """
    try:
        from cost_digest import _resolve_admin_ids

        admins = _resolve_admin_ids()
    except Exception:
        log.debug("compresr_regression_guard: admin resolution failed")
        admins = []

    if not admins:
        log.warning(
            "compresr_regression_guard: compresr compression auto-disabled for "
            "%s (reason=%s) but no admin users configured to notify",
            call_site,
            reason,
        )
        return

    recent = rates.get("recent", {})
    baseline = rates.get("baseline", {})
    msg = (
        f":rotating_light: *Compresr auto-disabled — `{call_site}`*\n"
        f"Reason: `{reason}`\n"
        f"24h failure rate: {_fmt_pct(recent.get('rate'))} "
        f"({recent.get('failures', 0)}/{recent.get('total', 0)})\n"
        f"14d baseline: {_fmt_pct(baseline.get('rate'))} "
        f"({baseline.get('failures', 0)}/{baseline.get('total', 0)})\n"
        f"Threshold (2x baseline): {_fmt_pct(rates.get('threshold'))}\n"
        f"Compression for this call site is now bypassed. "
        f"Clear the row in `compresr_site_disabled` to re-enable."
    )

    try:
        from slack_bot import send_dm
    except Exception:
        log.warning(
            "compresr_regression_guard: slack_bot unavailable — would have posted: %s",
            msg,
        )
        return

    for uid in admins:
        try:
            send_dm(uid, msg)
        except Exception:
            log.exception("compresr_regression_guard: DM failed for admin %s", uid)


def _fmt_pct(rate: Optional[float]) -> str:
    """Render a rate in 0..1 as a percentage with two decimals, or 'n/a'."""
    if rate is None:
        return "n/a"
    return f"{rate * 100:.2f}%"


def disable_call_site(call_site: str, reason: str) -> None:
    """Persist the disabled flag and post a deduped Slack notice.

    The DB row is written unconditionally (an existing row gets its timestamp
    and reason refreshed via ``ON CONFLICT DO UPDATE``). The Slack DM is
    suppressed when ``_already_notified_today`` returns True — that's the
    "deduped to once per day per call site" promise in the plan.
    """
    rates = compute_rates(call_site)

    deduped = _already_notified_today(call_site)
    _persist_disabled(call_site, reason)

    if deduped:
        log.info(
            "compresr_regression_guard: %s already disabled today — "
            "refreshed row but suppressing duplicate Slack notice",
            call_site,
        )
        return

    log.warning(
        "compresr_regression_guard: auto-disabling %s (reason=%s, "
        "recent=%s baseline=%s threshold=%s)",
        call_site,
        reason,
        rates.get("recent"),
        rates.get("baseline"),
        rates.get("threshold"),
    )
    _post_slack_notice(call_site, reason, rates)


# ──────────────────────────────────────────────────────────────────────────
# Cron entry point — iterate known sites, disable any that trip
# ──────────────────────────────────────────────────────────────────────────


def run_regression_check() -> dict:
    """Sweep ``KNOWN_CALL_SITES`` and disable any that exceed the threshold.

    Returns a dict for logging::

        {
            "checked_at": "YYYY-MM-DD",
            "sites": [
                {"call_site": str, "tripped": bool, "rates": {...}}, ...
            ],
            "tripped": [str, ...],
        }

    Never raises — each site is wrapped in its own try/except so one bad query
    doesn't skip the others.
    """
    summary: dict = {
        "checked_at": date.today().isoformat(),
        "sites": [],
        "tripped": [],
    }
    for site in KNOWN_CALL_SITES:
        try:
            rates = compute_rates(site)
            tripped = bool(rates.get("trips"))
            summary["sites"].append(
                {"call_site": site, "tripped": tripped, "rates": rates}
            )
            if tripped:
                summary["tripped"].append(site)
                disable_call_site(site, rates.get("reason") or "regression")
        except Exception:
            log.exception(
                "compresr_regression_guard: regression check failed for %s",
                site,
            )
    return summary


__all__ = [
    "REGRESSION_MULTIPLIER",
    "BASELINE_DAYS",
    "RECENT_HOURS",
    "MIN_SAMPLES",
    "KNOWN_CALL_SITES",
    "record_parse_outcome",
    "should_auto_disable",
    "is_disabled",
    "disable_call_site",
    "compute_rates",
    "run_regression_check",
]
