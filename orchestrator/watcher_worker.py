"""Worker pool + scheduler tick + catch-up sweep for the ❌-watcher.

Phase 1, PR 3 of the autonomous ❌-Watcher Managed Agent
(docs/proposals/watcher-design-20260521-210800.md, APPROVED 2026-05-26).

This module ships the dispatch plumbing — APScheduler cron tick,
dedicated WatcherThreadPoolExecutor (max_workers=5, separate from the
main investigation pool so a slow watcher cannot starve user sessions),
startup catch-up sweep, and graceful drain on SIGTERM.

The actual job runner (``_run_watcher_job``) is a stub in this PR that
just marks rows as ``completed``. PR 4 replaces the stub with the real
Managed Agents session dispatch (provision watcher agent, send the
diagnose prompt, run the fix-writing pass, open the draft PR).

Why a separate pool: a watcher session can take 5-15 min wall clock for
diagnose + fix-writing. With ``max_workers=5`` shared with the
investigation pool, a burst of ❌s would starve user investigations.
Five dedicated workers absorb the burst without touching user latency.

Why scheduler tick instead of inline dispatch in the enqueue hook: keeps
the lifecycle terminalize path fast and synchronous. The scheduler
drains in the background with no contention.
"""

from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)


# ───────────────────────────────────────────────────────────────────────
# Pool configuration
# ───────────────────────────────────────────────────────────────────────

MAX_WATCHER_WORKERS = 5
SHUTDOWN_TIMEOUT_SECONDS = 300  # 5 min — mirrors main investigation pool
CATCH_UP_WINDOW_MINUTES = 30  # back-fill failures from the last 30 min
SCHEDULER_TICK_SECONDS = 30  # poll watcher_pending every 30s

_executor = ThreadPoolExecutor(
    max_workers=MAX_WATCHER_WORKERS, thread_name_prefix="watcher"
)
_active_futures: dict[Future, int] = {}  # future -> row_id
_futures_lock = threading.Lock()
_shutting_down = False


def _watcher_enabled() -> bool:
    return os.environ.get("WATCHER_ENABLED", "false").strip().lower() == "true"


def _watcher_agent_id() -> Optional[str]:
    raw = (os.environ.get("WATCHER_AGENT_ID") or "").strip()
    return raw or None


# ───────────────────────────────────────────────────────────────────────
# Scheduler tick — runs every 30s via APScheduler
# ───────────────────────────────────────────────────────────────────────


def scheduled_watcher_drain(
    job_runner: Optional[Callable[[dict[str, Any]], None]] = None,
) -> int:
    """Poll watcher_pending and dispatch claimed rows to the executor.

    Returns the number of rows submitted this tick. ``job_runner`` is
    injectable for tests; default is ``_run_watcher_job`` defined below.

    Wrapped to never raise: APScheduler swallows job exceptions but logs
    them; we don't want to flood error telemetry with one entry per
    failed claim attempt.
    """
    if not _watcher_enabled():
        return 0
    if _shutting_down:
        return 0
    # Kill switch check (Phase 1 PR 7). Stateless — derived from GH +
    # watcher_pending each tick. When tripped, also DM the admin once
    # so the operator gets a clear "go triage" signal instead of
    # silently watching the queue back up.
    try:
        from watcher_kill_switch import (
            kill_switch_tripped,
            maybe_dm_admin_on_trip,
        )

        if kill_switch_tripped():
            maybe_dm_admin_on_trip()
            return 0
    except Exception:
        log.exception(
            "watcher: kill_switch check raised — proceeding (safe-default)"
        )
    runner = job_runner or _run_watcher_job

    # Lazy import — keeps unit tests importing this module without
    # paying the cost of importing watcher_pending_db (and psycopg2) at
    # module-load time.
    try:
        from watcher_pending_db import claim_watcher_pending
    except Exception:
        log.exception("watcher: lazy import for claim_watcher_pending failed")
        return 0

    # Pull up to (free workers) rows in one tick. ``_active_futures``
    # tracks in-flight rows; never overcommit the pool.
    with _futures_lock:
        free_slots = max(0, MAX_WATCHER_WORKERS - len(_active_futures))
    if free_slots == 0:
        return 0

    try:
        rows = claim_watcher_pending(limit=free_slots)
    except Exception:
        log.exception("watcher: claim_watcher_pending raised")
        return 0

    submitted = 0
    for row in rows:
        try:
            fut = _executor.submit(runner, row)
        except RuntimeError:
            # Executor shutdown raced with this submit (SIGTERM mid-drain).
            log.warning("watcher: executor shut down mid-drain; abandoning row %s", row.get("id"))
            _mark_abandoned(row.get("id"))
            continue
        row_id = row.get("id")
        if row_id is None:
            continue
        with _futures_lock:
            _active_futures[fut] = row_id
        fut.add_done_callback(_on_job_done)
        submitted += 1

    if submitted:
        log.info("watcher: drained %d row(s) this tick", submitted)
    return submitted


def _on_job_done(fut: Future) -> None:
    with _futures_lock:
        _active_futures.pop(fut, None)


# ───────────────────────────────────────────────────────────────────────
# Job runner — real Managed Agents dispatch (PR 6 replaces PR 3's stub)
# ───────────────────────────────────────────────────────────────────────


def _run_watcher_job(row: dict[str, Any]) -> None:
    """Run the watcher Managed Agent session for one ❌ row.

    Flow:
        1. Resolve WATCHER_AGENT_ID + ENVIRONMENT_ID. Missing either →
           diagnose_only (cannot dispatch without provisioned agent).
        2. Open a Managed Agents session targeting WATCHER_AGENT_ID.
        3. Send a single user.message with the failure context.
        4. Stream via _stream_and_handle — the agent's tool calls route
           through orchestrator/session_runner._dispatch_tool which has
           the 4 watcher_* branches (PR 5).
        5. After the session ends, inspect tool-call results to find
           any PR opened by the agent.
        6. Mark the row completed (PR opened) or diagnose_only (no PR).
        7. DM the admin with the outcome. Schedule a 10-min codex
           verdict poll if a PR was opened.

    Failure handling:
        - Missing env vars       → diagnose_only (no retry; operator
          must provision the agent and set env vars first)
        - sessions.create raises → failed_retry (transient upstream)
        - _stream_and_handle raises → failed_retry (transient)

    The watcher_pending state machine handles backoff + abandonment via
    the claim/mark cycle.
    """
    row_id = row.get("id")
    if row_id is None:
        return

    inv_id = row.get("inv_id")
    error_message = row.get("error_message") or ""
    error_message_hash = row.get("error_message_hash") or ""
    repeat_count = row.get("repeat_count", 1)
    catch_up = row.get("catch_up", False)

    watcher_agent_id = (os.environ.get("WATCHER_AGENT_ID") or "").strip()
    environment_id = (os.environ.get("ENVIRONMENT_ID") or "").strip()
    if not watcher_agent_id or not environment_id:
        log.warning(
            "watcher: WATCHER_AGENT_ID or ENVIRONMENT_ID unset — marking "
            "row_id=%s as diagnose_only",
            row_id,
        )
        _mark_diagnose_only(row_id)
        return

    try:
        import anthropic  # noqa: WPS433
    except Exception:
        log.exception("watcher: anthropic SDK import failed")
        _mark_failed_retry(row_id)
        return

    client = anthropic.Anthropic()

    kickoff = (
        f"<failure>\n"
        f"inv_id: {inv_id}\n"
        f"error_message_hash: {error_message_hash}\n"
        f"repeat_count: {repeat_count}\n"
        f"catch_up: {catch_up}\n"
        f"error_message:\n{error_message}\n"
        f"</failure>\n\n"
        "Follow your system prompt: read the error context, locate the "
        "call site via grep + read, identify the root cause, write a "
        "minimal fix on a new branch (watcher_create_branch / "
        "watcher_write_file), open a draft PR (watcher_create_pr), "
        "and close with a self-review checklist comment "
        "(watcher_add_comment). If the fix area is outside the "
        "allowlist or the root cause is unclear, escalate to "
        "diagnose-only mode."
    )

    try:
        session = client.beta.sessions.create(
            agent={"id": watcher_agent_id},  # type: ignore[arg-type]
            environment_id=environment_id,
            title=f"watcher: inv_id={inv_id} hash={error_message_hash[:10]}",
        )
    except Exception:
        log.exception("watcher: sessions.create failed for row_id=%s", row_id)
        _mark_failed_retry(row_id)
        return

    session_id = session.id
    log.info(
        "watcher: session %s created for row_id=%s inv_id=%s",
        session_id,
        row_id,
        inv_id,
    )

    pr_url: Optional[str] = None
    pr_number: Optional[int] = None
    try:
        from session_runner import _stream_and_handle  # noqa: WPS433

        agent_text_parts, _opened_tools, error_type, _ = _stream_and_handle(
            session_id,
            send_events=[
                {
                    "type": "user.message",
                    "content": [{"type": "text", "text": kickoff}],
                }
            ],
            thread_ts=None,
            verbosity="summary",
            portco_key=None,
            user_id=None,
            channel_id=None,
            inv_id=None,
        )
        if error_type:
            log.warning(
                "watcher: session %s ended with error_type=%s",
                session_id,
                error_type,
            )

        for tcall in (_opened_tools or []):
            if isinstance(tcall, dict) and tcall.get("name") == "watcher_create_pr":
                result = tcall.get("result") or {}
                if isinstance(result, dict) and result.get("ok"):
                    pr_url = result.get("pr_url")
                    pr_number = result.get("pr_number")
                    break
    except Exception:
        log.exception(
            "watcher: _stream_and_handle raised for session=%s row_id=%s",
            session_id,
            row_id,
        )
        _mark_failed_retry(row_id)
        return

    if pr_url:
        _mark_completed(row_id)
        _dm_admin_pr_opened(
            inv_id=inv_id,
            pr_url=pr_url,
            error_message_hash=error_message_hash,
        )
        if pr_number is not None:
            _schedule_codex_poll(pr_number=pr_number, inv_id=inv_id)
        log.info("watcher: completed row_id=%s pr=%s", row_id, pr_url)
    else:
        _mark_diagnose_only(row_id)
        _dm_admin_diagnose_only(
            inv_id=inv_id,
            error_message_hash=error_message_hash,
            summary="(no PR opened — see session log)",
        )
        log.info("watcher: diagnose_only row_id=%s (no PR opened)", row_id)


def _mark_completed(row_id: Optional[int]) -> None:
    if row_id is None:
        return
    try:
        from watcher_pending_db import STATUS_COMPLETED, mark_watcher_pending

        mark_watcher_pending(row_id, status=STATUS_COMPLETED)
    except Exception:
        log.exception("watcher: mark completed failed for row_id=%s", row_id)


def _mark_diagnose_only(row_id: Optional[int]) -> None:
    if row_id is None:
        return
    try:
        from watcher_pending_db import (
            STATUS_DIAGNOSE_ONLY,
            mark_watcher_pending,
        )

        mark_watcher_pending(row_id, status=STATUS_DIAGNOSE_ONLY)
    except Exception:
        log.exception(
            "watcher: mark diagnose_only failed for row_id=%s", row_id
        )


def _dm_admin_pr_opened(
    *, inv_id: Optional[int], pr_url: str, error_message_hash: str
) -> None:
    try:
        from slack_bot import send_notification  # noqa: WPS433

        send_notification(
            severity="info",
            summary=f"❌-watcher opened draft PR for inv_id={inv_id}",
            detail=(
                f"Hash: `{error_message_hash}`\n"
                f"PR: {pr_url}\n"
                "Review the codex verdict before merging."
            ),
        )
    except Exception:
        log.exception("watcher: admin DM (PR opened) failed")


def _dm_admin_diagnose_only(
    *, inv_id: Optional[int], error_message_hash: str, summary: str
) -> None:
    try:
        from slack_bot import send_notification  # noqa: WPS433

        send_notification(
            severity="watch",
            summary=f"❌-watcher diagnose-only for inv_id={inv_id}",
            detail=(
                f"Hash: `{error_message_hash}`\n"
                f"No PR opened (fix area outside allowlist OR root "
                f"cause unclear).\n\n"
                f"Summary: {summary}"
            ),
        )
    except Exception:
        log.exception("watcher: admin DM (diagnose-only) failed")


def _schedule_codex_poll(*, pr_number: int, inv_id: Optional[int]) -> None:
    """Fire-and-forget background poll for the codex review verdict.

    The CI workflow ``.github/workflows/codex-review.yml`` runs on PR
    open with ``continue-on-error: true``. A 401 transient leaves it
    GREEN with no comment — exactly the gotcha the watcher is designed
    to catch. The poll waits up to 10 min for a comment, then falls
    through to the workflow-run conclusion + log tail and labels the
    DM accordingly.
    """
    try:
        _executor.submit(_poll_codex_verdict, pr_number, inv_id)
    except RuntimeError:
        log.warning(
            "watcher: executor shut down — skipping codex poll for PR %d",
            pr_number,
        )


def _poll_codex_verdict(pr_number: int, inv_id: Optional[int]) -> None:
    """Poll for the codex review comment for up to 10 min."""
    import time

    try:
        from watcher_dispatch import REPO_NAME, REPO_OWNER, _gh_session  # noqa: WPS433
    except Exception:
        log.exception("watcher: codex poll import failed")
        return

    deadline = time.time() + 600  # 10 min
    verdict: Optional[str] = None
    log_tail: Optional[str] = None

    try:
        client = _gh_session()
    except Exception:
        log.exception("watcher: codex poll _gh_session failed")
        return
    try:
        while time.time() < deadline:
            r = client.get(
                f"/repos/{REPO_OWNER}/{REPO_NAME}/issues/{pr_number}/comments"
            )
            if r.status_code < 300:
                for c in r.json() or []:
                    body = (c.get("body") or "")
                    if "Codex Review" in body or "## Codex" in body:
                        verdict = body[:1000]
                        break
                if verdict:
                    break
            time.sleep(30)

        if not verdict:
            r = client.get(
                f"/repos/{REPO_OWNER}/{REPO_NAME}/actions/runs",
                params={"per_page": 10},
            )
            if r.status_code < 300:
                for run in r.json().get("workflow_runs", []) or []:
                    if (run.get("name") or "").lower().startswith("codex"):
                        log_tail = (
                            f"workflow conclusion={run.get('conclusion')}, "
                            f"status={run.get('status')}, "
                            f"url={run.get('html_url')}"
                        )
                        break
    finally:
        client.close()

    try:
        from slack_bot import send_notification  # noqa: WPS433

        if verdict:
            send_notification(
                severity="info",
                summary=(
                    f"Codex review verdict for ❌-watcher PR "
                    f"#{pr_number} (inv_id={inv_id})"
                ),
                detail=verdict,
            )
        elif log_tail:
            send_notification(
                severity="watch",
                summary=(
                    f"Codex did NOT comment on watcher PR "
                    f"#{pr_number} (inv_id={inv_id})"
                ),
                detail=f"Likely 401 fake-green. {log_tail}",
            )
        else:
            send_notification(
                severity="watch",
                summary=f"Codex review status unknown for PR #{pr_number}",
                detail=(
                    "Neither a comment nor a workflow run was found "
                    "within 10 min."
                ),
            )
    except Exception:
        log.exception("watcher: codex verdict DM failed")


def _mark_abandoned(row_id: Optional[int]) -> None:
    if row_id is None:
        return
    try:
        from watcher_pending_db import STATUS_ABANDONED, mark_watcher_pending

        mark_watcher_pending(row_id, status=STATUS_ABANDONED)
    except Exception:
        log.exception("watcher: mark abandoned failed for row_id=%s", row_id)


def _mark_failed_retry(row_id: Optional[int]) -> None:
    if row_id is None:
        return
    try:
        from watcher_pending_db import (
            STATUS_FAILED_RETRY,
            mark_watcher_pending,
        )

        mark_watcher_pending(row_id, status=STATUS_FAILED_RETRY)
    except Exception:
        log.exception("watcher: mark failed_retry failed for row_id=%s", row_id)


# ───────────────────────────────────────────────────────────────────────
# Startup catch-up sweep
# ───────────────────────────────────────────────────────────────────────


def catch_up_on_startup() -> int:
    """Back-fill watcher_pending for failures terminalized in the catch-up window.

    Called from ``main.py`` once during boot, after schema migrations and
    BEFORE the scheduler starts. Recursion guard is the watcher's own
    agent ID — propagated via ``WATCHER_AGENT_ID`` env var.

    Returns the number of rows enqueued.
    """
    if not _watcher_enabled():
        return 0
    try:
        from watcher_pending_db import catch_up_sweep
    except Exception:
        log.exception("watcher: lazy import for catch_up_sweep failed")
        return 0
    since = datetime.now(timezone.utc) - timedelta(minutes=CATCH_UP_WINDOW_MINUTES)
    try:
        enqueued = catch_up_sweep(
            since=since,
            watcher_agent_id=_watcher_agent_id(),
        )
    except Exception:
        log.exception("watcher: catch_up_sweep raised")
        return 0
    if enqueued:
        log.info(
            "watcher: catch-up sweep enqueued %d row(s) from last %d min",
            len(enqueued),
            CATCH_UP_WINDOW_MINUTES,
        )
    return len(enqueued)


# ───────────────────────────────────────────────────────────────────────
# Graceful shutdown
# ───────────────────────────────────────────────────────────────────────


def shutdown_watcher_executor(timeout_seconds: int = SHUTDOWN_TIMEOUT_SECONDS) -> None:
    """Drain the watcher executor on SIGTERM.

    Sets the shutting-down flag so the next scheduler tick is a no-op,
    then waits up to ``timeout_seconds`` for in-flight jobs to finish.
    Pending jobs that exceed the deadline are abandoned — better than
    a hung shutdown.
    """
    global _shutting_down
    _shutting_down = True
    # Wait on tracked futures with a deadline. Snapshot keys under the
    # lock because _on_job_done mutates _active_futures from the worker
    # threads as jobs complete.
    with _futures_lock:
        pending = list(_active_futures.keys())
    if pending:
        from concurrent.futures import wait

        wait(pending, timeout=timeout_seconds)
    try:
        _executor.shutdown(wait=False, cancel_futures=True)
    except TypeError:  # pragma: no cover — older Python without cancel_futures
        _executor.shutdown(wait=False)
    log.info(
        "watcher: executor drained on shutdown (timeout=%ds, in_flight=%d)",
        timeout_seconds,
        len(pending),
    )


def reset_for_tests() -> None:
    """Test-only helper: clear in-flight tracking + revive the executor.

    Pytest cases that exercise the scheduler tick mutate
    ``_active_futures`` and may trigger the shutdown flag. This restores
    a clean baseline so test order does not matter.
    """
    global _executor, _shutting_down
    with _futures_lock:
        _active_futures.clear()
    _shutting_down = False
    _executor = ThreadPoolExecutor(
        max_workers=MAX_WATCHER_WORKERS, thread_name_prefix="watcher"
    )
