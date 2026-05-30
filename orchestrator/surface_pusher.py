"""Slack Canvas push layer for the persistent-state surface (Plan #33 F6 + F11).

`push_to_canvas(portco)` is the only public entry point. It glues
together F1 (DB helpers), F4 (compute), and F5 (renderer) and writes
the resulting markdown to a per-portco Slack Canvas:

  1. Read the cached row via ``db_adapter.get_surface_state(portco)``
     and capture ``known_version_at_compute``.
  2. Compute a fresh ``SurfaceState`` via
     ``surface_compute.compute_surface(portco)``.
  3. Render it to Canvas markdown via ``surface_renderer.render(state)``.
  4. **Stale-edit drop (F11):** re-read the DB row right before pushing.
     If ``current_version != known_version_at_compute`` another writer
     bumped the row mid-flight, so drop this push as stale (return True
     no-op — this is not an error).
  5. If the markdown is byte-identical to the cached ``rendered_md``,
     skip the Slack write (no-op) and return True.
  6. Otherwise call ``canvases.edit`` (when a ``canvas_id`` already
     exists) or ``conversations.canvases.create`` (first time), then
     upsert the new ``rendered_md`` + ``canvas_id`` via
     ``db_adapter.upsert_surface_state``.

Failure semantics (acceptance criteria from Plan #33 F6 + F11):

  * On ``ratelimited`` we honor the ``Retry-After`` header and retry
    with jittered exponential back-off, up to ``MAX_RETRIES`` attempts.
  * On any other exception we log ``[SURFACE_PUSH_FAILED]`` with the
    portco + error, leave the previous ``rendered_md`` untouched in
    the DB (so the next push retries cleanly), and return False.
  * **Daily-deduped admin watch notice (F11):** when a portco accumulates
    3 consecutive failures inside a 60-minute rolling window, DM every
    admin user once per UTC date. The rolling window and per-day dedup
    set are module-level dicts — simplest substrate that matches the
    cost reconciliation drift-watch pattern (Plan #35 task #42).

Failures NEVER bubble up — ``push_to_canvas`` returns False (or True
for a stale-edit drop, which is a deliberate no-op) instead of raising.
The Slack client is fetched lazily (``_slack_client()``) so importing
this module never triggers a Slack auth round-trip. That matters for
both unit tests and the cold-start path of the orchestrator.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Deque, Dict, Optional, Tuple

from slack_sdk.errors import SlackApiError

import db_adapter
import portco_registry
import surface_compute
import surface_renderer

log = logging.getLogger(__name__)

# Retry policy for ``ratelimited`` errors. Slack publishes the exact
# back-off in the ``Retry-After`` header; we honor that and add a
# jittered exponential floor so successive 429s don't hot-loop.
MAX_RETRIES = 3
DEFAULT_RETRY_AFTER_SECONDS = 1.0

# Watch-notice trigger: 3 failures inside a 60-minute rolling window
# triggers a single Slack DM to admins per UTC date per portco. Mirrors
# the cost reconciliation drift-watch deduping pattern (Plan #35 task #42).
FAILURE_WINDOW_SECONDS = 60 * 60  # 60 minutes
FAILURE_THRESHOLD = 3

# Title shown in the Slack Canvas header. The body is the rendered
# markdown — the title is metadata only.
CANVAS_TITLE_TEMPLATE = "GTM Health — {portco} State"

# ─── Watch-notice dedup state (in-process) ──────────────────────────────
# Picked the simplest substrate per Plan #33 line 396: a module-level
# dict gated on UTC date stamp. The cost-collector pattern uses a DB
# table because reconciliation runs from a cron and must survive across
# container restarts — but surface pushes are far more frequent and
# clustered in time, so an in-process rolling window catches the
# "3 in 60 min" signal accurately. Per-day dedup also lives in-process:
# a container restart would at worst surface one duplicate DM, never a
# missed one. No new DB table required.
#
# Both maps are guarded by ``_state_lock`` because the scheduler thread
# and the Slack bot thread can both invoke ``push_to_canvas``.
_state_lock = threading.Lock()
_recent_failures: Dict[str, Deque[float]] = {}
_alerted_today: Dict[Tuple[str, str], bool] = {}


def _slack_client():
    """Lazy import to avoid a Slack auth round-trip at module load."""
    from slack_bot import app  # noqa: WPS433  — local import is intentional

    return app.client


def _channel_for_portco(portco: str) -> Optional[str]:
    """Resolve a portco key to its Slack channel ID via the registry."""
    cfg = portco_registry.get_portco_config(portco)
    if not cfg:
        return None
    return cfg.get("slack_channel")


def _document_content(markdown: str) -> dict:
    """Slack Canvas API payload — markdown variant."""
    return {"type": "markdown", "markdown": markdown}


def _retry_after_seconds(exc: SlackApiError, attempt: int) -> float:
    """Extract the ``Retry-After`` header from a Slack 429, with jittered fallback.

    Slack's ``Retry-After`` is in seconds. When the header is missing or
    unparseable we use jittered exponential back-off so the second 429
    doesn't immediately re-fire — ``base * (2 ** attempt) + random jitter``.
    A small jitter is added to the header value too, so two concurrent
    pushers don't sync up and hit Slack on the same wall-clock tick.
    """
    headers = {}
    response = getattr(exc, "response", None)
    if response is not None:
        # slack_sdk's SlackResponse exposes .headers; tests pass a plain
        # MagicMock or dict so we handle both.
        headers = getattr(response, "headers", None) or {}
        if not headers and isinstance(response, dict):
            headers = response.get("headers", {}) or {}
    raw = headers.get("Retry-After") or headers.get("retry-after")
    jitter = random.uniform(0, 1)
    try:
        # Header present: honor it, with a small (<= 1s) jitter on top.
        return max(float(raw), DEFAULT_RETRY_AFTER_SECONDS) + jitter
    except (TypeError, ValueError):
        # Header missing / malformed: jittered exponential back-off.
        return DEFAULT_RETRY_AFTER_SECONDS * (2**attempt) + jitter


def _call_slack_with_retry(callable_fn, *, op: str, portco: str):
    """Invoke a Slack API callable, retrying on ``ratelimited`` only.

    Other ``SlackApiError`` codes (channel_not_found, invalid_auth, …)
    bubble up to the caller, which logs ``[SURFACE_PUSH_FAILED]`` and
    returns False.
    """
    attempt = 0
    while True:
        try:
            return callable_fn()
        except SlackApiError as exc:
            err_code = ""
            response = getattr(exc, "response", None)
            if response is not None:
                if hasattr(response, "get"):
                    err_code = response.get("error", "") or ""
                elif isinstance(response, dict):
                    err_code = response.get("error", "") or ""
            if err_code != "ratelimited" or attempt >= MAX_RETRIES:
                raise
            delay = _retry_after_seconds(exc, attempt)
            log.warning(
                "Slack ratelimited on %s for portco=%s; "
                "retrying in %.2fs (attempt %d/%d)",
                op,
                portco,
                delay,
                attempt + 1,
                MAX_RETRIES,
            )
            time.sleep(delay)
            attempt += 1


# ─── Failure tracking + admin watch notice ──────────────────────────────


def _utc_date_str() -> str:
    """UTC date stamp used as the per-day dedup key for admin notices."""
    return datetime.now(timezone.utc).date().isoformat()


def _record_failure_and_maybe_notify(portco: str, error: Exception) -> None:
    """Track a push failure and emit a deduped admin DM when threshold crosses.

    Always called from the ``except`` block in ``push_to_canvas`` — must
    never raise. Reads/writes module-level dicts under ``_state_lock``.

    Behaviour:
      * Append ``now`` to ``_recent_failures[portco]``.
      * Evict entries older than ``FAILURE_WINDOW_SECONDS`` (60 min).
      * If the window holds ``>= FAILURE_THRESHOLD`` (3) failures AND we
        have not yet DMed admins for ``(portco, utc_today)``, send the
        watch notice and mark the day as alerted.
      * Per-day dedup: at most one notice per portco per UTC date.
    """
    try:
        now = time.time()
        with _state_lock:
            window = _recent_failures.setdefault(portco, deque())
            window.append(now)
            cutoff = now - FAILURE_WINDOW_SECONDS
            while window and window[0] < cutoff:
                window.popleft()
            should_notify = len(window) >= FAILURE_THRESHOLD
            day_key = (portco, _utc_date_str())
            if should_notify and _alerted_today.get(day_key):
                # Already DMed admins for this portco today — suppress.
                log.info(
                    "surface_pusher: %d failures in 60min for portco=%s, "
                    "but admins already notified today — suppressing",
                    len(window),
                    portco,
                )
                return
            if not should_notify:
                return
            # Reserve the day key *inside* the lock so two threads
            # crossing the threshold concurrently can only send one DM.
            _alerted_today[day_key] = True
            failure_count = len(window)

        _send_admin_watch_notice(portco, failure_count, error)
    except Exception:  # noqa: BLE001 — must never raise
        log.exception(
            "surface_pusher: failed to evaluate/dispatch admin watch notice "
            "for portco=%s",
            portco,
        )


def _send_admin_watch_notice(portco: str, failure_count: int, error: Exception) -> None:
    """DM every admin a one-line surface-push failure watch notice.

    Mirrors ``cost_collector.reconcile_daily``'s style: a short ``:large_yellow_circle:``
    watch line with portco, recent failure count, and the most recent error.
    Slack delivery failures are swallowed — APScheduler must keep running.
    """
    try:
        from portco_registry import get_admin_user_ids
    except Exception:
        log.exception("surface_pusher: admin user lookup import failed")
        return
    admins = get_admin_user_ids() or []
    if not admins:
        log.warning(
            "surface_pusher: no admin_user_ids configured — would have "
            "notified for portco=%s after %d failures",
            portco,
            failure_count,
        )
        return

    try:
        from slack_bot import send_dm
    except Exception:
        log.exception("surface_pusher: send_dm import failed")
        return

    msg = (
        f":large_yellow_circle: *WATCH* — surface push for `{portco}` has "
        f"failed {failure_count} times in the last hour. "
        f"Latest error: `{type(error).__name__}: {error}`. "
        f"Cached state in `surface_state` is preserved; the next push "
        f"will retry. See `[SURFACE_PUSH_FAILED]` logs for detail."
    )
    sent = 0
    for uid in admins:
        if not uid:
            continue
        try:
            send_dm(uid, msg)
            sent += 1
        except Exception:
            log.exception(
                "surface_pusher: failed to DM admin %s for portco=%s",
                uid,
                portco,
            )
    log.warning(
        "surface_pusher: posted [SURFACE_PUSH_FAILED] watch notice to "
        "%d/%d admins for portco=%s (failure_count=%d)",
        sent,
        len(admins),
        portco,
        failure_count,
    )


# Test helper: tests need a clean slate for the in-process state. Public
# so the suite doesn't have to reach into module privates.
def _reset_failure_state() -> None:
    """Clear the rolling-window and per-day dedup maps. Test-only."""
    with _state_lock:
        _recent_failures.clear()
        _alerted_today.clear()


# ─── Public entry point ─────────────────────────────────────────────────


def push_to_canvas(portco: str) -> bool:
    """Sync the persistent-state surface for ``portco`` to Slack Canvas.

    Returns True on success — including the two deliberate no-op cases:
      * cached rendered_md is byte-identical to the freshly rendered body, or
      * the DB ``version`` advanced between compute and push (stale-edit drop).

    Returns False on any failure. The last-good ``rendered_md`` in the
    DB is preserved so the next push can re-attempt without losing
    state. After 3 failures in 60 min, DMs admins (deduped per UTC date).
    """
    try:
        cached = db_adapter.get_surface_state(portco) or {}
        prior_md = cached.get("rendered_md", "") or ""
        canvas_id = cached.get("canvas_id")
        known_version_at_compute = cached.get("version")

        state = surface_compute.compute_surface(portco)
        new_md = surface_renderer.render(state)

        # ── Stale-edit drop (F11) ─────────────────────────────────────
        # Re-read the row right before pushing. If another writer bumped
        # the version in the gap between compute and push, our rendered
        # markdown is already stale — drop the push rather than clobber
        # newer state. Plan #33: "Drop edits older than current state."
        if known_version_at_compute is not None:
            fresh = db_adapter.get_surface_state(portco) or {}
            current_version = fresh.get("version")
            if (
                current_version is not None
                and current_version != known_version_at_compute
            ):
                log.info(
                    "surface_pusher stale edit dropped for portco=%s "
                    "(known_version=%s, current_version=%s)",
                    portco,
                    known_version_at_compute,
                    current_version,
                )
                return True

        if new_md == prior_md and canvas_id:
            # Byte-identical output + canvas already exists → skip.
            log.info(
                "surface_pusher no-op for portco=%s (rendered_md unchanged)",
                portco,
            )
            return True

        channel_id = _channel_for_portco(portco)
        if not channel_id and not canvas_id:
            # Without a channel we can't create a canvas, and without a
            # canvas_id we have nothing to edit either.
            raise RuntimeError(f"no slack_channel configured for portco={portco}")

        client = _slack_client()
        doc = _document_content(new_md)

        if canvas_id:
            _call_slack_with_retry(
                lambda: client.canvases_edit(
                    canvas_id=canvas_id,
                    changes=[{"operation": "replace", "document_content": doc}],
                ),
                op="canvases.edit",
                portco=portco,
            )
        else:
            resp = _call_slack_with_retry(
                lambda: client.conversations_canvases_create(
                    channel_id=channel_id,
                    title=CANVAS_TITLE_TEMPLATE.format(portco=portco),
                    document_content=doc,
                ),
                op="conversations.canvases.create",
                portco=portco,
            )
            # slack_sdk returns a SlackResponse that supports both
            # attribute and dict access; tests typically return a dict.
            new_id = None
            if resp is not None:
                if hasattr(resp, "get"):
                    new_id = resp.get("canvas_id")
                elif isinstance(resp, dict):
                    new_id = resp.get("canvas_id")
            if not new_id:
                raise RuntimeError(
                    "conversations.canvases.create returned no canvas_id"
                )
            canvas_id = new_id

        db_adapter.upsert_surface_state(
            portco,
            state.model_dump(),
            new_md,
            canvas_id,
        )
        log.info(
            "surface_pusher pushed for portco=%s canvas_id=%s (%d chars)",
            portco,
            canvas_id,
            len(new_md),
        )
        return True
    except Exception as exc:  # noqa: BLE001 — broad on purpose
        log.error(
            "[SURFACE_PUSH_FAILED] portco=%s error=%s",
            portco,
            exc,
        )
        _record_failure_and_maybe_notify(portco, exc)
        return False
