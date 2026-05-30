"""Centralized lifecycle finalization for Slack-triggered investigations.

Why this exists
---------------
Pre-refactor, the orchestrator tracked investigation delivery state
optimistically via a ``agent_posted_to_slack: bool`` flag inside
``_stream_and_handle`` (session_runner.py:1691). The flag was set when an
``agent.custom_tool_use`` event fired for ``send_slack_notification`` or
``post_report`` — BEFORE the actual Slack dispatch succeeded. Combined with
the orchestration-chatter blocklist (session_runner.py:585) that silently
suppresses tool-use without posting, the flag lied: it could be True for
investigations where the user got nothing.

Compounding that, the ⏰ → ❌ Slack reaction flip lived at a single
catastrophic-exception site (session_runner.py:3456). All ten other
terminal paths — schema-validation give-up, status_terminated without
session.error, fallback post_analysis, /stop, "Investigation produced no
output", existing-session reuse, etc. — bypassed it. Users saw their
message stuck on ⏰ indefinitely on most failure modes.

Incident (2026-05-13): session ``sesn_EXAMPLE`` exercised
this exact pattern. The Coordinator went idle after a ``COPY ... TO 'foo.xlsx'``
rejection inside ``query_artifact``, never called ``post_report``, was
logged as ``outcome=success`` with cost $5.00. The user message stayed on
⏰. ``investigations.status`` was marked ``completed`` for an investigation
that delivered nothing.

What this module does
---------------------
1. **`DeliveryState` enum.** Replaces the optimistic boolean with an
   honest enum that distinguishes how the user got their answer (or didn't).
   Promotion happens at the real delivery boundary (after
   ``send_notification`` returns a ts), never at the tool-use event.

2. **`terminalize_lifecycle(state, event_ts, channel_id, inv_id,
   error_message=None)`.** Single exit boundary for every terminal path.
   Two-layer idempotency:

   - In-memory ``OrderedDict`` (bounded 256) deduplicates inside a
     process — second call for the same ``inv_id`` returns immediately.
   - DB guard ``UPDATE ... WHERE id=%s AND status NOT IN (terminal
     states)`` (in ``db_adapter.update_investigation_atomic``) deduplicates
     across processes — old container draining + new container after
     deploy, or concurrent terminal paths racing.

   Order: **DB update first, then reaction flip.** If the row is already
   terminal in DB, skip the reaction flip too — preserves the invariant
   that ``investigations.status`` and the Slack emoji never contradict.

3. **`_run_investigation_guarded(...)`.** Wraps the previously-unguarded
   exception window between the ⏰ flip (session_runner.py:3401) and the
   ``_stream_and_handle`` return. Catches any unhandled exception, flips
   to ``TERMINAL_FAILURE`` with ``error_message='unhandled_exception:<Type>'``,
   then re-raises so existing logging is preserved.

Idempotency invariants
----------------------
- ``terminalize_lifecycle`` is safe to call any number of times for any
  ``inv_id``. Subsequent calls are no-ops.
- It is safe to call from concurrent threads or processes.
- Failure to flip the Slack reaction (rate limit, network error) does NOT
  revert the DB row — ``transition_reaction`` swallows the failure
  internally. The row stays terminal; the reaction is stuck on whatever
  Slack had it on. The user can refresh.
- Failure to update the DB row prevents the reaction flip — preserves
  invariant.

Plan reference: /Users/jb/.claude/plans/except-what-i-really-binary-river.md
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from enum import Enum
from typing import Any, Callable, Optional

__all__ = [
    "DeliveryState",
    "terminalize_lifecycle",
    "_run_investigation_guarded",
]


log = logging.getLogger(__name__)


# In-memory idempotency map. Per-process, not per-container — survives
# the lifetime of the orchestrator process. ``OrderedDict`` so we can LRU-
# evict; bound at 256 entries (an investigation typically lives 30s-15min,
# so the per-process active set is small).
_TERMINALIZED_MAX = 256
_terminalized: "OrderedDict[int, DeliveryState]" = OrderedDict()
_terminalized_lock = threading.Lock()


class DeliveryState(str, Enum):
    """Honest delivery state for a Slack-triggered investigation.

    ``str, Enum`` mixin so values serialize cleanly into logs and DB rows.

    Promotion rules (state machine — never demotes):
    ``NOT_DELIVERED`` → any other state at the honest delivery boundary
    (after ``send_notification`` returns a ts). Terminal states never
    transition further.
    """

    # No Slack output yet. The session is still running OR ended without
    # delivering anything.
    NOT_DELIVERED = "not_delivered"

    # User received the structured ``post_report`` Slack message —
    # rendered, validated, file-attached. The canonical happy path.
    DELIVERED_VIA_POST_REPORT = "delivered_via_post_report"

    # Fallback path: agent emitted free-text but never called
    # ``post_report``; the orchestrator wrapped it via ``post_analysis``.
    # Still counts as a real delivery — the user got an answer.
    DELIVERED_VIA_POST_ANALYSIS = "delivered_via_post_analysis"

    # Legacy ``send_slack_notification`` custom tool — used by Quick
    # Answer and some sub-agents. Only set AFTER send_notification returns
    # a ts; the chatter-blocklist "blocked" branch does NOT promote.
    DELIVERED_VIA_LEGACY_SLACK_TOOL = "delivered_via_legacy_slack_tool"

    # ----- Failure terminal states -----

    # Catastrophic failure: session.error terminal, session.status_terminated,
    # unguarded exception, _post_report_terminal_failure give-up.
    TERMINAL_FAILURE = "terminal_failure"

    # User used /stop or in-thread cancel. Distinct from TERMINAL_FAILURE
    # so /cost and recovery filters can exclude user-initiated stops from
    # failure-rate metrics. User-facing emoji is still ❌ (UX parity — to
    # the user it's "didn't finish") but DB status is 'cancelled'.
    USER_CANCELLED = "user_cancelled"

    # Session ran to completion but produced no Slack output and no
    # agent text. Today (session_runner.py:3525) this fires an admin
    # watch DM. Post-refactor it ALSO flips the user's emoji to ❌.
    NO_OUTPUT = "no_output"

    def is_delivered(self) -> bool:
        """True if the user got SOME Slack output via this state."""
        return self in (
            DeliveryState.DELIVERED_VIA_POST_REPORT,
            DeliveryState.DELIVERED_VIA_POST_ANALYSIS,
            DeliveryState.DELIVERED_VIA_LEGACY_SLACK_TOOL,
        )

    def is_terminal(self) -> bool:
        """True if this state is a final state (not NOT_DELIVERED)."""
        return self != DeliveryState.NOT_DELIVERED

    def db_status(self) -> str:
        """Map this state to the ``investigations.status`` value."""
        if self in (
            DeliveryState.DELIVERED_VIA_POST_REPORT,
            DeliveryState.DELIVERED_VIA_POST_ANALYSIS,
            DeliveryState.DELIVERED_VIA_LEGACY_SLACK_TOOL,
        ):
            return "completed"
        if self == DeliveryState.USER_CANCELLED:
            return "cancelled"
        # TERMINAL_FAILURE, NO_OUTPUT, NOT_DELIVERED (invariant violation)
        return "failed"

    def reaction_emoji(self) -> str:
        """Map this state to the Slack reaction emoji name (no colons)."""
        if self.is_delivered():
            return "white_check_mark"  # ✅
        # All non-delivered terminal states use ❌. NOT_DELIVERED is
        # treated as TERMINAL_FAILURE at terminalize time.
        return "x"


def _state_from_db_status(db_status: str) -> "DeliveryState":
    """Map a persisted ``investigations.status`` value back to a DeliveryState.

    Used by the reconciliation path in ``terminalize_lifecycle`` when the
    DB row is already terminal — we read the persisted status and flip
    the Slack reaction based on what landed, not what this caller
    requested.

    The mapping is intentionally narrow: only the four states the
    terminator writes to DB (``completed``, ``failed``, ``cancelled``)
    plus a defensive fallback. ``archived`` and ``interrupted`` are not
    mapped because they are post-hoc bookkeeping and shouldn't roll
    back the user's emoji.
    """
    s = (db_status or "").strip().lower()
    if s == "completed":
        # We can't tell whether the original path was post_report,
        # post_analysis, or legacy slack tool from the status alone.
        # All three map to ✅ so DELIVERED_VIA_POST_REPORT is a safe
        # representative.
        return DeliveryState.DELIVERED_VIA_POST_REPORT
    if s == "cancelled":
        return DeliveryState.USER_CANCELLED
    # ``failed`` or anything unrecognized — defensive ❌.
    return DeliveryState.TERMINAL_FAILURE


def _remember_terminalized(inv_id: int, state: DeliveryState) -> None:
    """Bounded LRU insert into the in-memory idempotency map."""
    with _terminalized_lock:
        _terminalized[inv_id] = state
        _terminalized.move_to_end(inv_id)
        while len(_terminalized) > _TERMINALIZED_MAX:
            _terminalized.popitem(last=False)


def _is_terminalized(inv_id: int) -> Optional[DeliveryState]:
    """Return the recorded terminal state for ``inv_id`` if present."""
    with _terminalized_lock:
        return _terminalized.get(inv_id)


def terminalize_lifecycle(
    state: DeliveryState,
    *,
    event_ts: Optional[str],
    channel_id: Optional[str],
    inv_id: Optional[int],
    error_message: Optional[str] = None,
) -> DeliveryState:
    """Single exit boundary for every terminal path. Idempotent.

    Args:
        state: the delivery state to terminalize as. Must be terminal
            (not ``NOT_DELIVERED``). If ``NOT_DELIVERED`` is passed, logs
            an invariant violation and treats it as ``TERMINAL_FAILURE``.
        event_ts: Slack ts of the user's original message. If None or
            empty, the reaction flip is skipped (cron flows have no
            user message to flip).
        channel_id: Slack channel containing the original message.
            Skipped if None.
        inv_id: investigations row id for the DB guard. If None, the
            in-memory idempotency check is skipped and the DB row is not
            updated (cron flows or unattached investigations).
        error_message: optional detail string written to
            ``investigations.error_message``. Truncated to 500 chars by
            the DB layer.

    Returns:
        The actual final state. May differ from ``state`` if another
        path already terminalized this investigation (the recorded
        winning state is returned).
    """
    # Invariant: NOT_DELIVERED is a transient state, not a terminal state.
    # If callers reach terminalization with NOT_DELIVERED something is wrong
    # in the upstream state machine; treat as TERMINAL_FAILURE so the
    # reaction still flips to ❌ and the DB row reflects reality.
    if state == DeliveryState.NOT_DELIVERED:
        log.error(
            "lifecycle: NOT_DELIVERED reached terminalize_lifecycle "
            "(inv_id=%s) — invariant violation, treating as TERMINAL_FAILURE",
            inv_id,
        )
        state = DeliveryState.TERMINAL_FAILURE
        if not error_message:
            error_message = "invariant_violation:not_delivered_at_terminalize"

    # In-memory idempotency: did we (this process) already terminalize
    # this inv_id? Second call is a no-op.
    if inv_id is not None:
        existing = _is_terminalized(inv_id)
        if existing is not None:
            log.debug(
                "lifecycle: inv_id=%s already terminalized as %s "
                "(requested=%s — ignoring)",
                inv_id,
                existing.value,
                state.value,
            )
            return existing

    # DB-layer idempotency: ``UPDATE ... WHERE status NOT IN (terminal
    # states)``. If the row was already terminal, ``won`` is False — some
    # other path (or a previous container) already terminalized. Skip the
    # Slack reaction in that case to preserve the invariant
    # status_in_db <==> emoji_on_slack.
    won_db = True
    if inv_id is not None:
        # Lazy import to avoid an import cycle: db_adapter is already a
        # peer module but importing at module-load time fans out.
        from db_adapter import update_investigation_atomic

        won_db = update_investigation_atomic(
            inv_id, state.db_status(), error_message=error_message
        )
        if not won_db:
            # Reconciliation path (codex P2, 2026-05-13). The DB row is
            # already terminal, but the previous winner may have died
            # before flipping the Slack reaction — e.g. orchestrator
            # crashed mid-terminalize, or a deploy-overlap window killed
            # the old container after its DB update but before its Slack
            # call. Without reconciliation, the user's message would
            # stay stuck on ⏰ forever, and recover_interrupted_investigations
            # cannot pick it up because the row is no longer 'running'.
            #
            # Read the row's actual final status from the DB and flip
            # the reaction based on that — NOT our caller's requested
            # state, which may not match what landed.
            try:
                # We don't have thread_ts here in the function signature.
                # Instead, fetch the row by inv_id directly via a separate
                # adapter helper. If reconciliation lookup fails for any
                # reason (DB down, lazy-import miss), best-effort fall
                # back to flipping based on the requested state — better
                # than a guaranteed stuck ⏰.
                from db_adapter import get_investigation_by_id

                row = get_investigation_by_id(inv_id)
                if row is not None:
                    db_status = (row.get("status") or "").strip()
                    final_state = _state_from_db_status(db_status)
                    log.info(
                        "lifecycle: inv_id=%s reconciling reaction from "
                        "DB-final status=%s (requested=%s)",
                        inv_id,
                        db_status,
                        state.value,
                    )
                    if event_ts and channel_id:
                        from slack_bot import REACTION_WORKING, transition_reaction

                        transition_reaction(
                            channel_id,
                            event_ts,
                            remove=REACTION_WORKING,
                            add=final_state.reaction_emoji(),
                        )
                    _remember_terminalized(inv_id, final_state)
                    return final_state
            except Exception:
                log.exception(
                    "lifecycle: inv_id=%s reconciliation failed; falling "
                    "back to flipping reaction based on caller's requested "
                    "state",
                    inv_id,
                )
            # Best-effort fallback: flip based on requested state.
            if event_ts and channel_id:
                from slack_bot import REACTION_WORKING, transition_reaction

                transition_reaction(
                    channel_id,
                    event_ts,
                    remove=REACTION_WORKING,
                    add=state.reaction_emoji(),
                )
            _remember_terminalized(inv_id, state)
            return state

    # DB row updated successfully. Now flip the Slack reaction. We import
    # transition_reaction lazily for the same reason as above (and because
    # slack_bot imports lifecycle, breaking the cycle).
    if event_ts and channel_id:
        from slack_bot import REACTION_WORKING, transition_reaction

        # The user's message is on ⏰. Flip to ✅ or ❌. transition_reaction
        # swallows Slack failures internally — failure to flip does NOT
        # revert the DB row.
        transition_reaction(
            channel_id,
            event_ts,
            remove=REACTION_WORKING,
            add=state.reaction_emoji(),
        )

    # Record in the in-memory idempotency map AFTER both DB and Slack
    # flip — ordering ensures a re-entry mid-flip-failure would still
    # complete the unfinished work.
    if inv_id is not None:
        _remember_terminalized(inv_id, state)
        # Cost accounting unification (Theme A, 2026-05-16). Every terminal
        # path that has won the DB-side idempotency race logs cost here —
        # watchdog Tier 3, SDK ReadTimeout, recovery-max-attempts, and the
        # normal post_report path all funnel through this point. Before
        # this, only happy-path adhoc completion at session_runner.py
        # logged, so failed sessions (e.g. inv 50 on 2026-05-16, ~$10-15
        # burned) never appeared in session_costs. The helper itself
        # catches all exceptions, but we wrap defensively so a future
        # refactor of the helper cannot break the terminalize contract.
        try:
            _log_cost_for_terminalized_inv(inv_id, state)
        except Exception:
            log.exception(
                "lifecycle: _log_cost_for_terminalized_inv(%s) raised — "
                "swallowing to preserve terminalize contract",
                inv_id,
            )

        # Autonomous ❌-Watcher enqueue (Phase 1 PR 2). Fires AFTER cost
        # logging so a watcher enqueue failure cannot drop the cost row.
        # Wrapped in try/except for the same defensiveness as cost: the
        # watcher is observability, not correctness — its failure must
        # not break terminalize.
        try:
            _enqueue_watcher_pending_for_terminalized(inv_id, state)
        except Exception:
            log.exception(
                "lifecycle: _enqueue_watcher_pending_for_terminalized(%s) raised — "
                "swallowing to preserve terminalize contract",
                inv_id,
            )

    log.info(
        "lifecycle: terminalized inv_id=%s as %s (db_won=%s, reaction=%s)",
        inv_id,
        state.value,
        won_db,
        bool(event_ts and channel_id),
    )
    return state


def _enqueue_watcher_pending_for_terminalized(
    inv_id: int, state: DeliveryState
) -> None:
    """Best-effort: enqueue a watcher_pending row for a failed investigation.

    Phase 1 PR 2 of the autonomous ❌-Watcher Managed Agent
    (docs/proposals/watcher-design-20260521-210800.md).

    Short-circuits when:
      - the state is delivered (only failures are interesting to watcher)
      - ``WATCHER_ENABLED`` env var is not literally ``"true"`` (safe rollout)
      - the investigation's ``agent_id`` matches ``WATCHER_AGENT_ID`` —
        the recursion guard prevents the watcher from triggering itself
        when its own draft-PR session terminalizes as failure
      - the investigation row lookup fails or returns nothing

    All-or-nothing semantic: every short-circuit is a clean return, never
    a raise. The caller (``terminalize_lifecycle``) wraps this in
    try/except as belt-and-suspenders, but the function aims to never
    need that fallback.
    """
    import os  # local import — module already imports os in some flows

    if state.is_delivered():
        return

    if os.environ.get("WATCHER_ENABLED", "false").strip().lower() != "true":
        return

    watcher_agent_id = (os.environ.get("WATCHER_AGENT_ID") or "").strip()

    try:
        from db_adapter import get_investigation_by_id
    except Exception:
        log.exception("watcher: lazy import for get_investigation_by_id failed")
        return

    try:
        row = get_investigation_by_id(inv_id)
    except Exception:
        log.exception("watcher: get_investigation_by_id(%s) failed", inv_id)
        return
    if not row:
        return

    # Recursion guard — placed before any further work so the watcher's
    # own session terminalization is a no-op. The same guard lives in
    # the startup catch-up sweep so both entry points are protected.
    if watcher_agent_id and (row.get("agent_id") or "") == watcher_agent_id:
        log.debug(
            "watcher: inv_id=%s is a watcher-owned session — skipping enqueue",
            inv_id,
        )
        return

    try:
        from error_hash import compute as _compute_hash
        from watcher_pending_db import enqueue_watcher_pending
    except Exception:
        log.exception("watcher: lazy import for enqueue failed")
        return

    error_message = row.get("error_message") or ""
    error_hash_value = _compute_hash(error_message)

    try:
        new_id = enqueue_watcher_pending(
            inv_id=inv_id,
            channel_id=row.get("channel_id"),
            thread_ts=row.get("thread_ts"),
            error_category=None,  # classified later by the worker
            error_message_hash=error_hash_value,
            catch_up=False,
        )
    except Exception:
        log.exception(
            "watcher: enqueue_watcher_pending(inv_id=%s) raised", inv_id
        )
        return

    if new_id is not None:
        log.info(
            "watcher: enqueued inv_id=%s hash=%s row_id=%s",
            inv_id,
            error_hash_value,
            new_id,
        )


def _log_cost_for_terminalized_inv(inv_id: int, state: DeliveryState) -> None:
    """Best-effort: log session cost for a terminalized Slack-driven investigation.

    Reads the investigations row by ``inv_id`` and calls ``_log_session_usage``
    with attribution sourced from the row. Outcome is the DeliveryState value so
    ``/cost`` dashboards can split delivered vs. failed cleanly.

    Why this lives in lifecycle (not session_runner): every terminal path —
    happy or failed, Coordinator-driven or watchdog-driven or recovery-driven —
    already calls ``terminalize_lifecycle``. Inserting cost-log here means
    watchdog Tier 3 + SDK timeout + recovery-max-attempts inherit cost logging
    for free.

    Lazy imports to avoid an import cycle (``session_runner`` imports
    ``lifecycle``). Failures here MUST NOT propagate — cost-log is observability,
    not correctness.
    """
    try:
        from db_adapter import get_investigation_by_id
        from session_runner import _log_session_usage
    except Exception:
        log.exception("lifecycle: lazy import for cost-log failed")
        return

    try:
        row = get_investigation_by_id(inv_id)
    except Exception:
        log.exception("lifecycle: get_investigation_by_id(%s) failed", inv_id)
        return
    if not row:
        return
    session_id = row.get("session_id")
    if not session_id:
        # Investigation never reached a session (e.g. queued + container died
        # before transition_queued_to_running). Nothing to bill.
        return

    # Map internal DeliveryState values to the outcome taxonomy that
    # downstream reporting (e.g. bin/measure-deploy-risk.py) expects:
    # "success" | "error" | "abandoned".  Using state.value directly
    # would write "terminal_failure"/"no_output" which are not counted
    # by the hourly error-rate and deploy-correlation queries.
    if state.is_delivered():
        outcome_label = "success"
    elif state == DeliveryState.USER_CANCELLED:
        outcome_label = "abandoned"
    else:
        # TERMINAL_FAILURE, NO_OUTPUT, and any future failure states.
        outcome_label = "error"

    # agent_id is often NULL on investigation rows for ad-hoc sessions
    # because the row is created/updated before the agent is assigned.
    # Prefer the row value but fall back to the well-known COORDINATOR_ID
    # so per-agent cost breakdowns retain fidelity.
    agent_id = row.get("agent_id")
    if not agent_id:
        try:
            from session_runner import COORDINATOR_ID as _COORDINATOR_ID
            agent_id = _COORDINATOR_ID
        except Exception:
            pass  # Leave agent_id as None rather than raising.

    try:
        _log_session_usage(
            session_id,
            "adhoc",
            portco_key=row.get("portco_key"),
            channel_id=row.get("channel_id"),
            thread_ts=row.get("thread_ts"),
            user_id=row.get("user_id"),
            agent_id=agent_id,
            trigger="slack-adhoc",
            outcome=outcome_label,
        )
    except Exception:
        log.exception(
            "lifecycle: _log_session_usage(%s) failed for inv_id=%s",
            session_id,
            inv_id,
        )


def _run_investigation_guarded(
    _lifecycle_inv_id: Optional[int],
    _lifecycle_event_ts: Optional[str],
    _lifecycle_channel_id: Optional[str],
    runner_fn: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Wrap a session runner in a try/except that terminalizes on uncaught exception.

    Closes the unguarded exception window at session_runner.py:3400-3441.
    Today an exception between the ⏰ flip and ``_stream_and_handle``
    return goes uncaught — the investigation row stays in 'running', the
    user's emoji stays on ⏰. With this wrapper, any exception triggers
    ``terminalize_lifecycle(TERMINAL_FAILURE, ...)`` BEFORE the exception
    propagates, so the user sees ❌ and the DB row reflects reality.

    The exception is re-raised after terminalization so existing logging
    and observability paths fire normally.

    The lifecycle coordinates are prefixed ``_lifecycle_`` so they don't
    collide with the runner's own ``inv_id``/``event_ts``/``channel_id``
    kwargs when forwarded via ``**kwargs``. Without the prefix, Python
    raises ``TypeError: got multiple values for argument 'event_ts'``
    when the runner's call signature names match the wrapper's. Caught
    at runtime 2026-05-13 when wiring the existing-session-reuse branch.
    """
    try:
        return runner_fn(*args, **kwargs)
    except BaseException as exc:  # noqa: BLE001 — explicit catch-all is intentional
        # Plan #47 Workstream A: _FollowupBlocked is a recoverable signal
        # (HTTP 400 on a thread follow-up because the session has pending
        # requires_action), not a terminal failure. The follow-up branch
        # in run_adhoc_mcp_session handles it and posts a Slack reply.
        # Propagate it without terminalize so the user's message keeps
        # 👁 instead of ❌.
        try:
            from session_runner import _FollowupBlocked  # lazy import — avoid circular
        except Exception:
            _FollowupBlocked = None  # type: ignore[assignment]
        if _FollowupBlocked is not None and isinstance(exc, _FollowupBlocked):
            raise
        log.exception(
            "lifecycle: unguarded exception in investigation runner "
            "(inv_id=%s) — terminalizing as TERMINAL_FAILURE",
            _lifecycle_inv_id,
        )
        try:
            terminalize_lifecycle(
                DeliveryState.TERMINAL_FAILURE,
                event_ts=_lifecycle_event_ts,
                channel_id=_lifecycle_channel_id,
                inv_id=_lifecycle_inv_id,
                error_message=f"unhandled_exception:{type(exc).__name__}",
            )
        except Exception:
            # Last-ditch safety: terminalize_lifecycle should never raise,
            # but if it does we don't want to mask the original exception.
            log.exception("lifecycle: terminalize_lifecycle ITSELF failed")
        raise
