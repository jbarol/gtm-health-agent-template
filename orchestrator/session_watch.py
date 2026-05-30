"""Session-size canary — early-warning alerts for sessions approaching the 1M cap.

Background:
    A Managed Agents session that crosses the 1M-input-token-per-turn cap dies
    silently with no answer to the user. Today (2026-05-11) session
    ``sesn_EXAMPLE`` died at 1.12M tokens before posting any
    answer. A second session ``sesn_EXAMPLE`` survived but
    cost $47 and shipped a partial deliverable. Both could have been
    intercepted with a 750K early warning.

What this does:
    Polls the ``investigations`` table every 30s for rows with
    ``status='running'``. For each row, retrieves the session's current usage
    via ``client.beta.sessions.retrieve(session_id)`` and computes the
    input-side total:

        input_side = cache_creation_5m + cache_read + input

    Two thresholds:
      * **750K** → ``:warning:`` watch notice — gives the user ~250K headroom
        to archive+replay or compress before termination.
      * **950K** → ``:rotating_light:`` imminent termination notice — last
        chance to intervene.

    Alerts are deduped per session_id (one watch + one imminent notice each)
    via a module-level ``alerted_session_ids`` set. The set never gets cleared
    intra-process: once a session has crossed a threshold, re-alerting on the
    next 30-second tick would be noise.

    Every per-session call is wrapped in try/except so one bad retrieve (404,
    transient HTTP, malformed usage payload) can't kill the rest of the loop.

Wiring:
    Registered in ``main.py`` as a 30-second APScheduler interval job with
    ``max_instances=1`` and ``coalesce=True`` — if a tick takes longer than
    30s (slow API, many active sessions), the next tick is skipped rather
    than queued.

Run mode:
    The canary is best-effort. Errors are logged, never raised. A misfire
    here must not crash the APScheduler thread.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import anthropic

from config import ANTHROPIC_API_KEY
import db_adapter

log = logging.getLogger(__name__)


WATCH_THRESHOLD = 750_000  # input-side tokens — early warning, ~250K headroom
IMMINENT_THRESHOLD = 950_000  # input-side tokens — last-chance notice before death

# Per-session dedup state. Module-level on purpose: the canary runs on a
# 30-second cadence and we never want a second :warning: for the same session
# in the same process. Tuple keys are (session_id, threshold_label).
#   ("sesn_EXAMPLE", "watch")     — 750K alert already posted
#   ("sesn_EXAMPLE", "imminent")  — 950K alert already posted
# Cleared only on process restart, which is the right cadence: container
# restart deletes the in-flight set and we re-alert from scratch.
alerted_session_ids: set[tuple[str, str]] = set()

# Anthropic client — module-level so the test suite can monkeypatch it cleanly
# (``patch("session_watch.client")``). API key is read at import time which
# matches the rest of the orchestrator.
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def _compute_input_side(usage) -> int:
    """Extract the input-side token total from a session usage object.

    The 1M cap is on *input tokens per turn*: new input + cache reads + cache
    writes. Output tokens don't count against the cap. We sum the three input
    categories the Managed Agents usage object surfaces:

        usage.input_tokens                                — new uncached input
        usage.cache_read_input_tokens                     — served from cache
        usage.cache_creation.ephemeral_5m_input_tokens    — written to 5m cache

    The 1h cache write field exists too but no current agent uses 1h caching,
    so we keep this aligned with what session_runner._extract_usage_parts
    reports for symmetry.

    Returns 0 on any malformed shape — the caller treats unknown as "no
    alert", which is the safe default.
    """
    if usage is None:
        return 0
    try:
        input_tok = getattr(usage, "input_tokens", 0) or 0
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cc = getattr(usage, "cache_creation", None)
        cache_write_5m = 0
        if cc is not None:
            cache_write_5m = getattr(cc, "ephemeral_5m_input_tokens", 0) or 0
        return int(input_tok) + int(cache_read) + int(cache_write_5m)
    except Exception:
        log.exception("session_watch: failed to compute input-side total")
        return 0


def _thread_permalink(channel_id: Optional[str], thread_ts: Optional[str]) -> str:
    """Render a Slack permalink for the investigation thread.

    Best-effort — the Slack API call may fail (rate limit, gone channel),
    in which case we fall back to a plain ``channel/ts`` label so the alert
    is still actionable. Lazy slack_bot import so this module loads cleanly
    in test environments that stub slack_bolt.
    """
    if not channel_id or not thread_ts:
        return "(no thread)"
    try:
        from slack_bot import app  # type: ignore

        result = app.client.chat_getPermalink(channel=channel_id, message_ts=thread_ts)
        link = result.get("permalink") if isinstance(result, dict) else None
        if link:
            return f"<{link}|thread>"
    except Exception:
        log.debug(
            "session_watch: chat_getPermalink failed for %s/%s — falling back",
            channel_id,
            thread_ts,
        )
    return f"{channel_id}/{thread_ts}"


def _send_alert(
    severity: str,
    summary: str,
    channel_id: Optional[str] = None,
    thread_ts: Optional[str] = None,
) -> None:
    """Log a canary alert. NO Slack side effects.

    Operational telemetry (canary watch/imminent notices) is internal — the
    user-facing channel only shows ack → finished report. Per the 2026-05-12
    self-heal fix: the canary is allowed to log, never to post. If the
    operator needs visibility, they can tail the Railway log or grep for
    ``session_watch: WATCH/IMMINENT alert fired``.

    The ``channel_id`` / ``thread_ts`` args are retained for log context
    (so a tail can show which investigation tripped the threshold) but
    are no longer used to route a Slack message.
    """
    if channel_id or thread_ts:
        log.warning(
            "session_watch alert [%s] (channel=%s thread=%s): %s",
            severity,
            channel_id or "(none)",
            thread_ts or "(none)",
            summary,
        )
    else:
        log.warning("session_watch alert [%s]: %s", severity, summary)


def _get_running_investigations() -> list[dict]:
    """Pull rows from ``investigations`` where status IN ('queued','running').

    Returns dicts with keys: id, session_id, thread_ts, channel_id,
    portco_key, started_at. Returns empty list on DB unavailable.

    Best-effort — DB errors log and return empty. The canary is non-critical
    and shouldn't take down the scheduler if Postgres is briefly unreachable.
    """
    if not db_adapter.DATABASE_URL:
        return []
    try:
        import psycopg2.extras  # type: ignore

        conn = db_adapter._connect()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT id, session_id, thread_ts, channel_id, "
                    "portco_key, started_at "
                    "FROM investigations "
                    "WHERE status IN ('queued', 'running') "
                    "AND session_id IS NOT NULL"
                )
                rows = [dict(r) for r in cur.fetchall()]
                return rows
        finally:
            conn.close()
    except Exception:
        log.exception("session_watch: failed to query running investigations")
        return []


def _session_age_minutes(started_at) -> float:
    """Compute minutes elapsed since ``started_at``. Returns 0.0 on any error."""
    if started_at is None:
        return 0.0
    try:
        # ``started_at`` from psycopg2 RealDictCursor is a tz-aware datetime.
        from datetime import datetime, timezone as tz

        now = datetime.now(tz.utc)
        if started_at.tzinfo is None:
            # Treat naive as UTC — best we can do.
            started_at = started_at.replace(tzinfo=tz.utc)
        return (now - started_at).total_seconds() / 60.0
    except Exception:
        return 0.0


def _check_one_session(row: dict) -> None:
    """Inspect a single active session for threshold crossings.

    Wrapped in a try/except by ``check_active_sessions`` so one bad row
    doesn't kill the loop, but we also catch + log here so the row context
    (session_id, portco) is in the log line.
    """
    session_id = row.get("session_id")
    if not session_id:
        return

    # Short-circuit: if both thresholds already fired for this session, skip
    # the API call entirely. Saves a retrieve call per tick per dead session
    # that's still rotting in the investigations table.
    if (session_id, "watch") in alerted_session_ids and (
        session_id,
        "imminent",
    ) in alerted_session_ids:
        return

    try:
        s = client.beta.sessions.retrieve(session_id)
    except Exception as e:
        # 404 on archived sessions, transient HTTP, malformed responses.
        # Log at debug — running this every 30s would spam at warning.
        log.debug("session_watch: retrieve failed for %s: %s", session_id, e)
        return

    usage = getattr(s, "usage", None)
    input_side = _compute_input_side(usage)

    if input_side < WATCH_THRESHOLD:
        return

    portco_key = row.get("portco_key") or "(unknown)"
    thread_ts = row.get("thread_ts")
    channel_id = row.get("channel_id")
    age_min = _session_age_minutes(row.get("started_at"))
    link = _thread_permalink(channel_id, thread_ts)

    # Imminent threshold first (more severe). Note: a session can cross
    # 750K then 950K within the same tick (rare but possible on a slow
    # poll). We want both alerts to fire — first the watch, then the
    # imminent — so the user sees the escalation.
    if (
        input_side >= WATCH_THRESHOLD
        and (
            session_id,
            "watch",
        )
        not in alerted_session_ids
    ):
        summary = (
            f":warning: Session `{session_id}` crossed 750K input-side tokens "
            f"({input_side:,} of 1M cap). Age: {age_min:.0f} min. "
            f"Portco: {portco_key}. Investigation: {link}."
        )
        _send_alert("watch", summary, channel_id=channel_id, thread_ts=thread_ts)
        alerted_session_ids.add((session_id, "watch"))
        log.warning(
            "session_watch: WATCH alert fired for %s (%d tokens, portco=%s)",
            session_id,
            input_side,
            portco_key,
        )

    if (
        input_side >= IMMINENT_THRESHOLD
        and (
            session_id,
            "imminent",
        )
        not in alerted_session_ids
    ):
        summary = (
            f":rotating_light: Session `{session_id}` crossed 950K "
            f"({input_side:,} of 1M cap) — imminent termination. "
            f"Consider archive+replay. Age: {age_min:.0f} min. "
            f"Portco: {portco_key}. Investigation: {link}."
        )
        _send_alert("critical", summary, channel_id=channel_id, thread_ts=thread_ts)
        alerted_session_ids.add((session_id, "imminent"))
        log.warning(
            "session_watch: IMMINENT alert fired for %s (%d tokens, portco=%s)",
            session_id,
            input_side,
            portco_key,
        )


def check_active_sessions() -> dict:
    """APScheduler job — poll all active sessions for token-cap proximity.

    Registered in ``main.py`` as a 30-second interval job with
    ``max_instances=1`` and ``coalesce=True``. The 30s cadence is chosen so:

      * A session adding ~100K tokens per turn (worst-case observed) gets at
        least one tick between crossings of 750K and 950K, giving the user
        time to react.
      * The polling overhead is bounded: at most ``MAX_CONCURRENT_INVESTIGATIONS``
        (currently 5) ``sessions.retrieve`` calls per minute = ~10 RPM. Well
        below any rate limit.

    Returns a small dict for logging/debugging:
        {"checked": N, "alerted_watch": M, "alerted_imminent": K}

    Never raises — a misfire here must not crash APScheduler.
    """
    start = time.monotonic()
    rows = _get_running_investigations()
    if not rows:
        return {"checked": 0, "alerted_watch": 0, "alerted_imminent": 0}

    watch_before = sum(1 for k in alerted_session_ids if k[1] == "watch")
    imminent_before = sum(1 for k in alerted_session_ids if k[1] == "imminent")

    for row in rows:
        try:
            _check_one_session(row)
        except Exception:
            sid = row.get("session_id", "(unknown)")
            log.exception(
                "session_watch: per-session check failed for %s — continuing", sid
            )
            continue

    watch_after = sum(1 for k in alerted_session_ids if k[1] == "watch")
    imminent_after = sum(1 for k in alerted_session_ids if k[1] == "imminent")
    elapsed = time.monotonic() - start

    result = {
        "checked": len(rows),
        "alerted_watch": watch_after - watch_before,
        "alerted_imminent": imminent_after - imminent_before,
    }
    if result["alerted_watch"] or result["alerted_imminent"]:
        log.info(
            "session_watch: tick complete (%.2fs) — checked=%d watch=%d imminent=%d",
            elapsed,
            result["checked"],
            result["alerted_watch"],
            result["alerted_imminent"],
        )
    else:
        log.debug(
            "session_watch: tick complete (%.2fs) — checked=%d, no alerts",
            elapsed,
            result["checked"],
        )
    return result
