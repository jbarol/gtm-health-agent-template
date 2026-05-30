"""Generate human-readable status snippets for in-flight investigations.

Reads from the ``investigations`` table and (best-effort) the Anthropic
Managed Agents sessions API to assemble a snapshot of where the agent is
in a given Slack thread. Driven by the in-thread meta-intent router in
``slack_bot.py`` — when a user says "status update" inside an existing
investigation thread, this is what they get back instead of the canned
kickoff template.

Design notes
============

  * **Never raise.** The function returns a best-effort message even on
    partial data (missing session, no historical sample, DB outage). The
    caller posts the return value verbatim to Slack — a traceback would
    bubble up as an unfriendly Bolt error, defeating the point of the
    fast intent-routing pathway.
  * **No new tables.** The ``investigations`` table doesn't carry an
    audit-trail column today, so we derive the closest signal we have:
    status + age + (if a session ID is attached) the session's archived
    flag from the Anthropic API. If a richer event ledger lands later,
    expand ``_recent_events`` to read it.
  * **ETA = p50 wall-clock for completed ``slack-adhoc`` sessions over
    the last 30 days.** Computed from ``session_costs`` joined to
    ``investigations`` by ``session_id``. Pure DB query — no extra Anthropic
    API call. If the sample is empty, the snippet omits the ETA line
    rather than guessing.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)


def _age_string(started_at) -> str:
    """Format ``started_at`` (tz-aware datetime) as ``Xm Ys`` or ``Xh Ym``.

    Returns ``"unknown age"`` if the input is None / not parseable. The
    investigations table stores ``started_at`` as ``TIMESTAMPTZ``, so the
    psycopg2 ``RealDictCursor`` hands us a tz-aware ``datetime``. We still
    coerce naive datetimes to UTC defensively.
    """
    if started_at is None:
        return "unknown age"
    try:
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        elapsed = (now - started_at).total_seconds()
    except Exception:
        return "unknown age"
    if elapsed < 0:
        return "just now"
    if elapsed < 60:
        return f"{int(elapsed)}s"
    if elapsed < 3600:
        m, s = divmod(int(elapsed), 60)
        return f"{m}m {s}s"
    h, rem = divmod(int(elapsed), 3600)
    m, _ = divmod(rem, 60)
    return f"{h}h {m}m"


def _p50_adhoc_runtime_seconds() -> Optional[float]:
    """Median wall-clock for completed ``slack-adhoc`` investigations.

    Joins ``investigations`` to itself via ``completed_at - started_at``
    over the last 30 days. Falls back to None when:
      * DB unavailable
      * the sample is empty
      * the query raises for any reason

    Returns float seconds when computable. The caller decides how to
    surface "unknown" (we just don't print an ETA line).
    """
    try:
        import db_adapter

        if not db_adapter.DATABASE_URL:
            return None
        conn = db_adapter._connect()
        try:
            with conn.cursor() as cur:
                # PERCENTILE_CONT over completed adhoc-ish investigations.
                # We don't have a ``trigger`` column on ``investigations``,
                # but adhoc rows are the ones that fill ``user_id`` and
                # ``thread_ts``. Cron jobs (dream / forecast) have neither.
                cur.execute(
                    """
                    SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (
                        ORDER BY EXTRACT(EPOCH FROM (completed_at - started_at))
                    )
                    FROM investigations
                    WHERE status = 'completed'
                      AND completed_at IS NOT NULL
                      AND started_at IS NOT NULL
                      AND thread_ts IS NOT NULL
                      AND user_id IS NOT NULL
                      AND started_at > NOW() - INTERVAL '30 days'
                    """
                )
                row = cur.fetchone()
                if not row or row[0] is None:
                    return None
                return float(row[0])
        finally:
            conn.close()
    except Exception as e:
        log.debug(f"p50 runtime lookup failed: {e}")
        return None


def _format_eta_line(started_at, p50_seconds: Optional[float]) -> Optional[str]:
    """Render the ETA line, or None when we shouldn't show one.

    Logic:
      * No p50 sample → return None (don't bluff).
      * Age < p50 → "ETA ~Xm remaining (typical run is Ym)".
      * Age >= p50 → "Running long — typical run is Ym, this is now Zm".
    """
    if p50_seconds is None or p50_seconds <= 0:
        return None
    if started_at is None:
        return None
    try:
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
    except Exception:
        return None

    typical_min = max(1, int(round(p50_seconds / 60)))
    elapsed_min = max(0, int(round(elapsed / 60)))

    if elapsed < p50_seconds:
        remaining_min = max(1, int(round((p50_seconds - elapsed) / 60)))
        return f"ETA: ~{remaining_min}m remaining (typical run: {typical_min}m)."
    return (
        f"Running long — typical run is {typical_min}m, this one is at {elapsed_min}m."
    )


def _session_archived(session_id: Optional[str]) -> Optional[bool]:
    """Return True/False for archived state, or None when we can't tell.

    Best-effort retrieve via the Anthropic SDK; absence of a session ID,
    a network blip, a 404, or a missing client all collapse to None.
    """
    if not session_id:
        return None
    try:
        # Lazy import — keeps the responder usable in DB-only test paths
        # where the Anthropic client may not be configured.
        from session_runner import client as _client

        s = _client.beta.sessions.retrieve(session_id)
        archived_at = getattr(s, "archived_at", None)
        return archived_at is not None
    except Exception as e:
        log.debug(f"sessions.retrieve failed for {session_id}: {e}")
        return None


def _recent_events(_inv_row: dict) -> list[str]:
    """Return up to 5 audit-trail-ish event strings.

    The ``investigations`` table doesn't carry an event log column today.
    We derive a coarse trail from the row itself:
      * created
      * (optional) recovered N time(s)
      * current status

    When a richer ledger lands (per-tool events, per-message logs), this
    is the seam to replace.
    """
    events: list[str] = []

    started = _inv_row.get("started_at")
    if started is not None:
        events.append(f"queued at {started.isoformat(timespec='seconds')}")

    rc = _inv_row.get("recovery_count") or 0
    if rc:
        s = "" if rc == 1 else "s"
        events.append(f"recovered {rc} time{s} after container restart")

    sid = _inv_row.get("session_id")
    if sid:
        events.append(f"session {sid}")

    status = _inv_row.get("status") or "unknown"
    events.append(f"status: {status}")

    completed = _inv_row.get("completed_at")
    if completed is not None and status in ("completed", "failed", "cancelled"):
        events.append(f"finished at {completed.isoformat(timespec='seconds')}")

    return events[-5:]


def _completed_snippet(inv_row: dict) -> str:
    """Render a brief summary for a finished investigation."""
    status = inv_row.get("status") or "unknown"
    sid = inv_row.get("session_id") or "(no session id)"
    age = _age_string(inv_row.get("started_at"))
    completed_at = inv_row.get("completed_at")
    completed_line = ""
    if completed_at is not None:
        try:
            completed_line = (
                f"\nFinished at {completed_at.isoformat(timespec='seconds')}."
            )
        except Exception:
            completed_line = ""

    if status == "completed":
        lead = ":white_check_mark: Done."
    elif status == "failed":
        lead = ":x: Failed."
    elif status == "cancelled":
        lead = ":no_entry_sign: Cancelled."
    else:
        lead = f"Status: *{status}*."

    return (
        f"{lead} Investigation started {age} ago.\n"
        f"Session: `{sid}`."
        f"{completed_line}\n"
        f"Findings and any data files are posted upthread."
    )


def status_snippet(investigation_id: int) -> str:
    """Return a multi-line status message for Slack.

    Includes:
      * Investigation status (queued / running / completed / failed /
        cancelled / interrupted).
      * Session ID (clickable in logs).
      * Age since started.
      * The last few audit-trail-ish events derived from the row.
      * An ETA estimate based on age vs. p50 wall-clock for ``slack-adhoc``
        sessions over the last 30 days (omitted when we have no sample).

    Returns a friendly message even on partial data — never raises.
    """
    # Defensive: handle the "no DB / no row" case early. We never raise,
    # we just return a useful string.
    inv_row: Optional[dict] = None
    try:
        import db_adapter

        if db_adapter.DATABASE_URL and investigation_id:
            conn = db_adapter._connect()
            try:
                import psycopg2.extras  # type: ignore

                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        "SELECT id, thread_ts, channel_id, user_id, question, "
                        "portco_key, session_id, agent_id, status, started_at, "
                        "completed_at, error_message, recovery_count, "
                        "container_id "
                        "FROM investigations WHERE id = %s",
                        (investigation_id,),
                    )
                    row = cur.fetchone()
                    inv_row = dict(row) if row else None
            finally:
                conn.close()
    except Exception as e:
        log.debug(f"status_snippet: row lookup failed: {e}")
        inv_row = None

    if inv_row is None:
        return (
            ":mag: No active investigation found for this thread "
            "(it may have completed and been archived, or the DB is "
            "temporarily unreachable)."
        )

    status = inv_row.get("status") or "unknown"

    # Completed / failed / cancelled investigations get a short closing
    # summary rather than the in-flight breakdown.
    if status in ("completed", "failed", "cancelled", "interrupted"):
        return _completed_snippet(inv_row)

    # In-flight (queued / running / anything else) — full breakdown.
    age = _age_string(inv_row.get("started_at"))
    sid = inv_row.get("session_id") or "(not yet attached)"
    portco = inv_row.get("portco_key") or "unknown"

    lines = [
        f":mag: Status update for investigation #{inv_row['id']}",
        f"Status: *{status}* — running for {age}.",
        f"Session: `{sid}`",
        f"Portco: `{portco}`",
    ]

    # Best-effort session inspection — bubble the archive state into the
    # message so a user can tell when a session has been killed out from
    # under the row (orphaned investigation).
    archived = _session_archived(inv_row.get("session_id"))
    if archived is True:
        lines.append(
            "_The underlying Anthropic session has been archived — this row "
            "is orphaned. Re-ask the question to start fresh._"
        )

    # Audit trail (best-effort, derived from the row).
    events = _recent_events(inv_row)
    if events:
        lines.append("")
        lines.append("Recent events:")
        lines.extend(f"  • {e}" for e in events)

    # ETA line (omitted when we have no historical sample).
    eta = _format_eta_line(inv_row.get("started_at"), _p50_adhoc_runtime_seconds())
    if eta:
        lines.append("")
        lines.append(eta)

    return "\n".join(lines)
