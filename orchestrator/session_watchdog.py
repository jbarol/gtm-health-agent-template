"""Stalled-session watchdog (Plan: Design A, 2026-05-15).

Background thread that polls running investigations every
``WATCHDOG_POLL_SECONDS`` and detects sessions that have gone silent for
longer than ``STALL_THRESHOLD_SECONDS``. Escalates through three tiers:

  Tier 1 — gentle: inject a ``user.message`` into the primary thread asking
           the Coordinator to proceed with what it has, or interrupt the
           stuck sub-thread.
  Tier 2 — assertive: list session threads and ``user.interrupt`` any
           non-primary thread stuck in ``requires_action`` or ``running``
           (per multi-agent docs: interrupt marks pending tools denied and
           emits ``session.thread_status_idle`` with stop_reason=end_turn).
  Tier 3 — terminate: post ❌ + Slack failure notice in-thread, admin DM,
           mark investigation ``failed`` in DB, archive Anthropic session.

Lives in its own thread; never blocks the slack-bot listener or the
investigation worker. All side effects are best-effort; any exception in
one investigation's tick must not stop the loop.

Symptom this attacks: 4 of 5 sessions observed on 2026-05-15 stuck in
``session.status_idle`` with ``stop_reason.type == requires_action`` after
the Coordinator dispatched sub-agents and never resumed. Per
``platform.claude.com/docs/en/managed-agents/multi-agent``, the parent
SHOULD auto-resume when ``agent.thread_message_received`` arrives; when it
doesn't (stranded sub-agent thread that can't complete its task), the
parent is permanently blocked.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

# Defaults — overridable via env. Tunable so operators can scale this
# back during incident response if the watchdog itself becomes noisy.
STALL_THRESHOLD_SECONDS = int(os.environ.get("STALL_THRESHOLD_SECONDS", "600"))
WATCHDOG_POLL_SECONDS = int(os.environ.get("WATCHDOG_POLL_SECONDS", "60"))
WATCHDOG_ENABLED = os.environ.get("WATCHDOG_ENABLED", "true").lower() != "false"
# Tier-2 wait before escalating to terminate. Short; we already gave Tier 1
# a STALL_THRESHOLD window. If the user.message didn't unstick within the
# Tier-2 window plus poll cadence, the session is unlikely to recover.
TIER_ESCALATION_SECONDS = int(os.environ.get("WATCHDOG_TIER_ESCALATION_SECONDS", "120"))

# Per-investigation state for tier tracking. Process-local; on container
# restart, watchdog state resets and recovery starts over from Tier 1 — a
# safe default because the recover_interrupted_investigations() flow on
# boot handles container-restart cases separately.
_TIER_STATE_LOCK = threading.Lock()
_TIER_STATE: dict[int, "TierState"] = {}


@dataclass
class TierState:
    """Tracks where we are in the escalation ladder for one investigation."""

    tier: int = 0  # 0 = healthy / never triggered, 1/2/3 = last tier fired
    last_action_at: float = 0.0  # unix ts of most recent tier action


@dataclass
class WatchdogVerdict:
    """Per-tick outcome for one investigation. Used by tests + telemetry."""

    inv_id: int
    session_id: str
    action: str  # "skip" | "tier1" | "tier2" | "tier3" | "ok"
    reason: str
    sent: int = 0
    received: int = 0
    age_seconds: float = 0.0


def _now() -> float:
    """Indirection so tests can monkeypatch time."""
    return time.time()


def _session_age_seconds(session: Any) -> float:
    updated = getattr(session, "updated_at", None)
    if updated is None:
        return 0.0
    if isinstance(updated, str):
        try:
            updated = datetime.fromisoformat(updated.replace("Z", "+00:00"))
        except ValueError:
            return 0.0
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - updated).total_seconds()


def _count_dispatch_imbalance(events: list) -> tuple[int, int]:
    """Return (thread_message_sent_count, thread_message_received_count).

    A positive imbalance (sent > received) is the strongest stranded-sub-agent
    signal. Both event types live on the primary thread per the multi-agent
    docs ("the event is cross-posted to the **primary thread**").
    """
    sent = 0
    received = 0
    for e in events or []:
        t = getattr(e, "type", None)
        if t == "agent.thread_message_sent":
            sent += 1
        elif t == "agent.thread_message_received":
            received += 1
    return sent, received


def _last_event_is_requires_action(events: list) -> bool:
    """True if the most recent ``session.status_idle`` event is requires_action.

    Events from ``events.list`` come back newest-first. We look for the most
    recent ``session.status_idle`` and check its ``stop_reason.type``. Anything
    else (end_turn, max_tokens) means the session terminated naturally and is
    not stranded — skip it.
    """
    for e in events or []:
        if getattr(e, "type", None) != "session.status_idle":
            continue
        sr = getattr(e, "stop_reason", None)
        sr_type = getattr(sr, "type", None) if sr else None
        return sr_type == "requires_action"
    return False


def evaluate(
    *,
    inv_id: int,
    session_id: str,
    client: Any,
    threshold_seconds: int = STALL_THRESHOLD_SECONDS,
) -> WatchdogVerdict:
    """Decide what tier (if any) applies to one investigation. Read-only.

    Pure decision function. Returns a verdict; the caller (``tick``) is
    responsible for executing tier actions and updating ``_TIER_STATE``.
    """
    try:
        session = client.beta.sessions.retrieve(session_id)
    except Exception as e:
        return WatchdogVerdict(
            inv_id=inv_id,
            session_id=session_id,
            action="skip",
            reason=f"session_retrieve_failed: {e}",
        )

    if getattr(session, "archived_at", None) is not None:
        return WatchdogVerdict(
            inv_id=inv_id,
            session_id=session_id,
            action="skip",
            reason="archived",
        )

    age = _session_age_seconds(session)
    if age < threshold_seconds:
        return WatchdogVerdict(
            inv_id=inv_id,
            session_id=session_id,
            action="ok",
            reason="within_threshold",
            age_seconds=age,
        )

    try:
        evs = client.beta.sessions.events.list(session_id=session_id, limit=100)
        events = list(getattr(evs, "data", []) or [])
    except Exception as e:
        return WatchdogVerdict(
            inv_id=inv_id,
            session_id=session_id,
            action="skip",
            reason=f"events_list_failed: {e}",
            age_seconds=age,
        )

    sent, received = _count_dispatch_imbalance(events)
    requires_action = _last_event_is_requires_action(events)

    stranded = sent > received
    if not stranded and not requires_action:
        # Session is idle but in a terminal stop_reason — let it be.
        return WatchdogVerdict(
            inv_id=inv_id,
            session_id=session_id,
            action="ok",
            reason="idle_terminal_stop",
            sent=sent,
            received=received,
            age_seconds=age,
        )

    # Stranded. Tier choice depends on prior state.
    with _TIER_STATE_LOCK:
        prior = _TIER_STATE.get(inv_id, TierState())

    if prior.tier == 0:
        action = "tier1"
        reason = f"stalled {age:.0f}s; sent={sent} received={received}; first wakeup"
    elif prior.tier == 1:
        elapsed = _now() - prior.last_action_at
        if elapsed < TIER_ESCALATION_SECONDS:
            action = "ok"
            reason = (
                f"tier1 fired {elapsed:.0f}s ago; waiting {TIER_ESCALATION_SECONDS}s "
                f"before escalation"
            )
        else:
            action = "tier2"
            reason = (
                f"stalled {age:.0f}s; tier1 didn't unstick; interrupting sub-threads"
            )
    elif prior.tier == 2:
        elapsed = _now() - prior.last_action_at
        if elapsed < TIER_ESCALATION_SECONDS:
            action = "ok"
            reason = f"tier2 fired {elapsed:.0f}s ago; waiting before terminal"
        else:
            action = "tier3"
            reason = "tier2 didn't recover the session — terminating"
    else:
        # tier 3 already fired; nothing more to do.
        action = "ok"
        reason = "already_terminated"

    return WatchdogVerdict(
        inv_id=inv_id,
        session_id=session_id,
        action=action,
        reason=reason,
        sent=sent,
        received=received,
        age_seconds=age,
    )


# ---- Tier action helpers (each tolerates and logs every failure) -----------


def _send_tier1_wakeup(*, client: Any, session_id: str, inv_id: int) -> bool:
    """Inject a gentle user.message to nudge the Coordinator to ship."""
    try:
        client.beta.sessions.events.send(
            session_id=session_id,
            events=[
                {
                    "type": "user.message",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "[watchdog] One of your dispatched sub-agents "
                                "appears not to have returned. Please proceed "
                                "with the results you already have and call "
                                "post_report with current findings. If a "
                                "specific sub-agent is genuinely needed, "
                                "re-dispatch it explicitly; otherwise ship "
                                "what you have."
                            ),
                        }
                    ],
                }
            ],
        )
        log.info(
            "[WATCHDOG_RECOVERY_FIRED] tier=1 inv_id=%s session=%s",
            inv_id,
            session_id,
        )
        return True
    except Exception as exc:
        log.warning(
            "[WATCHDOG_TIER1_SEND_FAILED] inv_id=%s session=%s err=%s",
            inv_id,
            session_id,
            exc,
        )
        return False


def _interrupt_stranded_threads(*, client: Any, session_id: str, inv_id: int) -> int:
    """Interrupt any non-primary thread stuck in running/requires_action.

    Per multi-agent docs: ``user.interrupt`` with ``session_thread_id`` marks
    pending tools denied and emits ``session.thread_status_idle`` with
    ``stop_reason: end_turn`` directly; the model is not sampled. That's the
    correct way to unblock a stranded sub-agent thread so the parent can
    proceed.
    """
    interrupted = 0
    try:
        threads = client.beta.sessions.threads.list(session_id)
        for t in getattr(threads, "data", []) or []:
            # Skip primary thread (parent_thread_id is None per docs).
            if getattr(t, "parent_thread_id", None) is None:
                continue
            status = (getattr(t, "status", "") or "").lower()
            if status not in ("running", "requires_action"):
                continue
            try:
                client.beta.sessions.events.send(
                    session_id=session_id,
                    events=[
                        {
                            "type": "user.interrupt",
                            "session_thread_id": t.id,
                        }
                    ],
                )
                interrupted += 1
            except Exception as exc:
                log.warning(
                    "[WATCHDOG_TIER2_INTERRUPT_FAILED] inv_id=%s thread=%s err=%s",
                    inv_id,
                    getattr(t, "id", "?"),
                    exc,
                )
    except Exception as exc:
        log.warning(
            "[WATCHDOG_TIER2_LIST_FAILED] inv_id=%s session=%s err=%s",
            inv_id,
            session_id,
            exc,
        )
    log.info(
        "[WATCHDOG_RECOVERY_FIRED] tier=2 inv_id=%s session=%s interrupted=%d",
        inv_id,
        session_id,
        interrupted,
    )
    return interrupted


def _terminate_stalled(
    *,
    client: Any,
    session_id: str,
    inv_id: int,
    thread_ts: Optional[str],
    channel_id: Optional[str],
    event_ts: Optional[str],
    db_adapter_mod: Any,
    send_notification_fn: Optional[Callable],
    terminalize_fn: Optional[Callable],
    archive_session_fn: Optional[Callable],
) -> None:
    """Tier 3: ❌ in-thread, admin DM, mark failed, archive session."""
    # Thread Slack notice — best effort. Don't crash if Slack is down.
    if send_notification_fn:
        try:
            send_notification_fn(
                severity="watch",
                summary=(
                    ":x: Investigation stalled — the agent didn't deliver a "
                    "final report within the expected window. Try rephrasing "
                    "the question, or contact admins."
                ),
                reply_to=thread_ts,
                channel=channel_id,
            )
        except Exception as exc:
            log.warning(
                "[WATCHDOG_TIER3_THREAD_NOTICE_FAILED] inv_id=%s err=%s",
                inv_id,
                exc,
            )
        # Admin DM with forensics.
        try:
            send_notification_fn(
                severity="watch",
                summary=(
                    f":warning: Watchdog terminated stalled session "
                    f"`{session_id}` (inv_id={inv_id})"
                ),
                detail=(
                    f"thread_ts={thread_ts}\n"
                    f"channel_id={channel_id}\n"
                    f"session_id={session_id}\n"
                    "Reason: parent thread idle past STALL_THRESHOLD_SECONDS "
                    "with sent>received imbalance OR persistent requires_action. "
                    "Tier 1+2 did not recover."
                ),
                admin_only=True,
            )
        except Exception as exc:
            log.warning(
                "[WATCHDOG_TIER3_ADMIN_DM_FAILED] inv_id=%s err=%s",
                inv_id,
                exc,
            )

    # Mark investigation failed in DB.
    try:
        if hasattr(db_adapter_mod, "mark_investigation_failed"):
            db_adapter_mod.mark_investigation_failed(
                inv_id, error_message="watchdog_terminated_stalled_session"
            )
    except Exception as exc:
        log.warning("[WATCHDOG_TIER3_DB_FAILED] inv_id=%s err=%s", inv_id, exc)

    # Archive the Anthropic session.
    if archive_session_fn:
        try:
            archive_session_fn(
                session_id,
                thread_ts=thread_ts,
                channel_id=channel_id,
            )
        except Exception as exc:
            log.warning(
                "[WATCHDOG_TIER3_ARCHIVE_FAILED] inv_id=%s session=%s err=%s",
                inv_id,
                session_id,
                exc,
            )

    # Terminalize lifecycle (❌ reaction, error_message in DB).
    if terminalize_fn:
        try:
            terminalize_fn(
                event_ts=event_ts,
                channel_id=channel_id,
                inv_id=inv_id,
                error_message="watchdog_terminated_stalled_session",
            )
        except Exception as exc:
            log.warning(
                "[WATCHDOG_TIER3_TERMINALIZE_FAILED] inv_id=%s err=%s",
                inv_id,
                exc,
            )

    log.info(
        "[WATCHDOG_RECOVERY_FIRED] tier=3 inv_id=%s session=%s — terminated",
        inv_id,
        session_id,
    )


# ---- Tick + loop -----------------------------------------------------------


def tick(
    *,
    client: Any,
    db_adapter_mod: Any,
    container_id: str,
    send_notification_fn: Optional[Callable] = None,
    terminalize_fn: Optional[Callable] = None,
    archive_session_fn: Optional[Callable] = None,
) -> list[WatchdogVerdict]:
    """One pass: list running investigations in this container, evaluate each.

    Returns the list of verdicts for telemetry / test assertions. Never
    raises — a failure in any investigation must not stop the loop.
    """
    verdicts: list[WatchdogVerdict] = []
    try:
        rows = _list_running_investigations(db_adapter_mod, container_id)
    except Exception as exc:
        log.warning("[WATCHDOG_LIST_FAILED] err=%s", exc)
        return verdicts

    for row in rows:
        inv_id = row.get("id")
        session_id = row.get("session_id")
        if not inv_id or not session_id:
            continue
        try:
            v = evaluate(inv_id=inv_id, session_id=session_id, client=client)
        except Exception as exc:
            log.warning(
                "[WATCHDOG_EVAL_FAILED] inv_id=%s session=%s err=%s",
                inv_id,
                session_id,
                exc,
            )
            continue
        verdicts.append(v)

        if v.action == "tier1":
            ok = _send_tier1_wakeup(client=client, session_id=session_id, inv_id=inv_id)
            if ok:
                with _TIER_STATE_LOCK:
                    _TIER_STATE[inv_id] = TierState(tier=1, last_action_at=_now())
        elif v.action == "tier2":
            _interrupt_stranded_threads(
                client=client, session_id=session_id, inv_id=inv_id
            )
            with _TIER_STATE_LOCK:
                _TIER_STATE[inv_id] = TierState(tier=2, last_action_at=_now())
        elif v.action == "tier3":
            _terminate_stalled(
                client=client,
                session_id=session_id,
                inv_id=inv_id,
                thread_ts=row.get("thread_ts"),
                channel_id=row.get("channel_id"),
                event_ts=row.get("event_ts"),
                db_adapter_mod=db_adapter_mod,
                send_notification_fn=send_notification_fn,
                terminalize_fn=terminalize_fn,
                archive_session_fn=archive_session_fn,
            )
            with _TIER_STATE_LOCK:
                _TIER_STATE[inv_id] = TierState(tier=3, last_action_at=_now())

    return verdicts


def _list_running_investigations(db_adapter_mod: Any, container_id: str) -> list[dict]:
    """Live investigations attached to THIS container. Read-only.

    Uses a DB query specific to the watchdog. If db_adapter doesn't have
    a tailored function, fall back to a raw query via its connection.
    """
    if hasattr(db_adapter_mod, "list_running_investigations_for_container"):
        return db_adapter_mod.list_running_investigations_for_container(container_id)
    # Fallback: raw query through the DB module's _connect helper.
    if not getattr(db_adapter_mod, "DATABASE_URL", None):
        return []
    try:
        import psycopg2.extras

        conn = db_adapter_mod._connect()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT id, session_id, thread_ts, channel_id, event_ts, "
                    "started_at "
                    "FROM investigations "
                    "WHERE status = 'running' "
                    "AND container_id = %s "
                    "AND session_id IS NOT NULL "
                    "ORDER BY started_at ASC",
                    (container_id or "",),
                )
                return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()
    except Exception as exc:
        log.debug("_list_running_investigations failed: %s", exc)
        return []


_watchdog_thread: Optional[threading.Thread] = None
_stop_event: Optional[threading.Event] = None


def start_watchdog(
    *,
    client: Any,
    db_adapter_mod: Any,
    container_id: str,
    send_notification_fn: Optional[Callable] = None,
    terminalize_fn: Optional[Callable] = None,
    archive_session_fn: Optional[Callable] = None,
    poll_seconds: int = WATCHDOG_POLL_SECONDS,
) -> Optional[threading.Thread]:
    """Start the watchdog background thread. Idempotent.

    Honors ``WATCHDOG_ENABLED`` env (default true). Returns the thread
    handle, or None if disabled or already running.
    """
    global _watchdog_thread, _stop_event

    if not WATCHDOG_ENABLED:
        log.info("Watchdog disabled (WATCHDOG_ENABLED=false)")
        return None
    if _watchdog_thread is not None and _watchdog_thread.is_alive():
        return _watchdog_thread

    local_stop = threading.Event()
    _stop_event = local_stop

    def _loop():
        log.info(
            "Watchdog started: poll=%ds threshold=%ds tier_wait=%ds",
            poll_seconds,
            STALL_THRESHOLD_SECONDS,
            TIER_ESCALATION_SECONDS,
        )
        while not local_stop.is_set():
            try:
                tick(
                    client=client,
                    db_adapter_mod=db_adapter_mod,
                    container_id=container_id,
                    send_notification_fn=send_notification_fn,
                    terminalize_fn=terminalize_fn,
                    archive_session_fn=archive_session_fn,
                )
            except Exception:
                log.exception("Watchdog tick crashed — loop continues")
            local_stop.wait(poll_seconds)
        log.info("Watchdog stopped")

    t = threading.Thread(target=_loop, name="session-watchdog", daemon=True)
    t.start()
    _watchdog_thread = t
    return t


def stop_watchdog(timeout: float = 5.0) -> None:
    """Stop the watchdog thread. For shutdown + tests."""
    global _watchdog_thread, _stop_event
    if _stop_event is not None:
        _stop_event.set()
    if _watchdog_thread is not None:
        _watchdog_thread.join(timeout=timeout)
    _watchdog_thread = None
    _stop_event = None


def _reset_state_for_tests() -> None:
    """Clear in-process tier state. Test-only helper."""
    with _TIER_STATE_LOCK:
        _TIER_STATE.clear()
