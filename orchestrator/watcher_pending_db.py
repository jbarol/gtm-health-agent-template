"""Database accessors for the ``watcher_pending`` queue.

Phase 1, PR 1 of the autonomous ❌-Watcher Managed Agent. Schema lives
in ``orchestrator/migrations/00AT_watcher_pending.sql``; this module
holds the typed Python interface that the lifecycle hook, scheduler,
and worker pool use.

Functions:
    enqueue_watcher_pending(inv_id, ..., error_message_hash)
        Insert a row, or bump repeat_count on conflict with an existing
        pending row sharing the same hash.

    claim_watcher_pending(limit=N)
        Atomically mark up to N pending rows as ``running`` and return
        them. Uses ``FOR UPDATE SKIP LOCKED`` so multiple workers can
        claim disjoint subsets without contention.

    mark_watcher_pending(id, status, **extras)
        Transition a row to a terminal or retry state. Records
        last_attempt_at and bumps attempts when status='failed_retry'.

    catch_up_sweep(since)
        Find investigations terminalized after ``since`` that lack a
        matching ``watcher_pending`` row, and enqueue them with
        ``catch_up=true``. Returns the list of enqueued inv_ids.

    count_unmerged_unreviewed_24h(reviewed_check_fn)
        For the kill-switch denominator. Returns the count of completed
        watcher rows in the last 24h whose PR was neither merged nor
        reviewed. ``reviewed_check_fn(pr_url) -> bool`` is injected so
        the GitHub query lives in the caller, not here.

All functions are no-ops (return None / empty list) when ``DATABASE_URL``
is unset — matches the rest of db_adapter's local-test posture.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)


# Status values must mirror the migration's documented enum. Encoded as
# module-level constants so callers and tests share one source of truth.
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_TRANSIENT_SKIPPED = "transient_skipped"
STATUS_FAILED_RETRY = "failed_retry"
STATUS_ABANDONED = "abandoned"
STATUS_DIAGNOSE_ONLY = "diagnose_only"

VALID_STATUSES = frozenset(
    [
        STATUS_PENDING,
        STATUS_RUNNING,
        STATUS_COMPLETED,
        STATUS_TRANSIENT_SKIPPED,
        STATUS_FAILED_RETRY,
        STATUS_ABANDONED,
        STATUS_DIAGNOSE_ONLY,
    ]
)


def _database_url() -> str:
    return os.environ.get("DATABASE_URL", "")


def _connect():
    """Open a Postgres connection. Imported lazily so tests can stub.

    Mirrors ``db_adapter._connect`` but kept private to this module so
    test patching does not bleed into the broader adapter surface.
    """
    import psycopg2  # noqa: WPS433

    return psycopg2.connect(_database_url())


# ───────────────────────────────────────────────────────────────────────
# Enqueue
# ───────────────────────────────────────────────────────────────────────


def enqueue_watcher_pending(
    *,
    inv_id: Optional[int],
    channel_id: Optional[str],
    thread_ts: Optional[str],
    error_category: Optional[str],
    error_message_hash: str,
    catch_up: bool = False,
) -> Optional[int]:
    """Insert a watcher_pending row or bump an existing pending duplicate.

    Returns the row id on insert, the existing row id on conflict.
    Returns None when ``DATABASE_URL`` is unset.

    Dedup semantics: the partial unique index
    ``(error_message_hash) WHERE status='pending'`` means a fresh ❌ with
    the same normalized hash collapses onto the existing pending row and
    increments ``repeat_count``. Once status flips off 'pending', a
    re-occurrence is treated as a brand-new signal.
    """
    if not _database_url():
        return None
    # source_inv_ids tracks every inv_id that has collapsed onto this
    # pending row (including the first one). On conflict, append the new
    # inv_id IF it isn't already in the array. Postgres ``array_append``
    # would always append; we use a CASE so we don't accumulate
    # duplicates if the same inv enqueues twice.
    sql = """
        INSERT INTO watcher_pending (
            inv_id, channel_id, thread_ts,
            error_category, error_message_hash, catch_up,
            source_inv_ids
        )
        VALUES (
            %s, %s, %s, %s, %s, %s,
            CASE WHEN %s::INTEGER IS NULL
                THEN ARRAY[]::INTEGER[]
                ELSE ARRAY[%s::INTEGER]
            END
        )
        ON CONFLICT (error_message_hash) WHERE status IN ('pending', 'running')
        DO UPDATE
            SET repeat_count = watcher_pending.repeat_count + 1,
                source_inv_ids = CASE
                    WHEN EXCLUDED.inv_id IS NULL THEN watcher_pending.source_inv_ids
                    WHEN EXCLUDED.inv_id = ANY(watcher_pending.source_inv_ids)
                        THEN watcher_pending.source_inv_ids
                    ELSE array_append(watcher_pending.source_inv_ids, EXCLUDED.inv_id)
                END,
                updated_at = NOW()
        RETURNING id
    """
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    inv_id,
                    channel_id,
                    thread_ts,
                    error_category,
                    error_message_hash,
                    catch_up,
                    inv_id,
                    inv_id,
                ),
            )
            row = cur.fetchone()
        conn.commit()
        return int(row[0]) if row else None
    finally:
        conn.close()


# ───────────────────────────────────────────────────────────────────────
# Claim (worker dequeue)
# ───────────────────────────────────────────────────────────────────────


def claim_watcher_pending(limit: int = 1) -> list[dict[str, Any]]:
    """Atomically mark up to ``limit`` pending rows as ``running``.

    Uses ``UPDATE ... FROM (SELECT ... FOR UPDATE SKIP LOCKED)`` so
    concurrent workers do not block each other on the same rows. Returns
    the claimed rows as dicts. Empty list if no pending work or no DB.

    Failed retry rows are picked up here too only when their last_attempt_at
    has aged past the retry backoff (1m / 5m / 15m / 1h based on attempts).
    Pure pending rows are always eligible. Stale running rows (last_attempt_at
    older than 30 minutes) are also reclaimed to recover from process crashes
    or Railway restarts that kill a worker mid-flight.
    """
    if not _database_url() or limit <= 0:
        return []
    # The retry backoff schedule: index = attempts (capped at 4).
    # attempt 0 → eligible immediately (pending)
    # attempt 1 → wait 1 min
    # attempt 2 → wait 5 min
    # attempt 3 → wait 15 min
    # attempt 4 → wait 1 hour (then mark abandoned, handled by caller)
    #
    # transient_skipped (upstream 5xx / rate-limit shortcut) is also
    # eligible after the same backoff schedule — the design treats it
    # as a retry, not a terminal state. Without this, a single 429 on
    # sessions.create would strand the row forever.
    sql = """
        WITH eligible AS (
            SELECT id
            FROM watcher_pending
            WHERE
                (status = 'pending')
                OR (
                    status IN ('failed_retry', 'transient_skipped')
                    AND last_attempt_at IS NOT NULL
                    AND last_attempt_at <
                        NOW() - (CASE attempts
                            WHEN 1 THEN INTERVAL '1 minute'
                            WHEN 2 THEN INTERVAL '5 minutes'
                            WHEN 3 THEN INTERVAL '15 minutes'
                            ELSE INTERVAL '1 hour'
                        END)
                )
                OR (
                    status = 'running'
                    AND last_attempt_at IS NOT NULL
                    AND last_attempt_at < NOW() - INTERVAL '30 minutes'
                )
            ORDER BY created_at ASC
            LIMIT %s
            FOR UPDATE SKIP LOCKED
        )
        UPDATE watcher_pending wp
        SET status = 'running',
            last_attempt_at = NOW(),
            attempts = wp.attempts + 1
        FROM eligible
        WHERE wp.id = eligible.id
        RETURNING wp.id, wp.inv_id, wp.channel_id, wp.thread_ts,
                  wp.error_category, wp.error_message_hash,
                  wp.repeat_count, wp.attempts, wp.catch_up,
                  wp.first_seen_at, wp.created_at
    """
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (limit,))
            description = cur.description or []
            cols = [d[0] for d in description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        conn.commit()
        return rows
    finally:
        conn.close()


# ───────────────────────────────────────────────────────────────────────
# Mark (worker → terminal/retry)
# ───────────────────────────────────────────────────────────────────────


def mark_watcher_pending(
    row_id: int,
    *,
    status: str,
    error_category: Optional[str] = None,
) -> None:
    """Transition a watcher_pending row to a new status.

    ``status`` must be one of the documented enum values; raises
    ``ValueError`` otherwise. ``error_category`` is optionally written
    when the worker has classified the error (e.g. after the diagnose
    pass). Bumps updated_at via trigger.
    """
    if status not in VALID_STATUSES:
        raise ValueError(
            f"invalid status {status!r}; expected one of {sorted(VALID_STATUSES)}"
        )
    if not _database_url():
        return
    if error_category is None:
        sql = "UPDATE watcher_pending SET status = %s, last_attempt_at = NOW() WHERE id = %s"
        params: tuple = (status, row_id)
    else:
        sql = (
            "UPDATE watcher_pending "
            "SET status = %s, error_category = %s, last_attempt_at = NOW() "
            "WHERE id = %s"
        )
        params = (status, error_category, row_id)
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


# ───────────────────────────────────────────────────────────────────────
# Catch-up sweep
# ───────────────────────────────────────────────────────────────────────


def catch_up_sweep(
    since: datetime,
    *,
    watcher_agent_id: Optional[str] = None,
) -> list[int]:
    """Enqueue catch_up=true rows for terminalized investigations missing one.

    Used by the orchestrator's startup hook to repopulate the queue
    after a Railway deploy gap. Returns the list of inv_ids enqueued.

    Filters on the persisted ``investigations.status='failed'`` value
    that ``DeliveryState.db_status()`` writes for TERMINAL_FAILURE,
    NO_OUTPUT, and the NOT_DELIVERED invariant case.

    ``watcher_agent_id`` is the recursion guard. When set (the typical
    case), watcher-owned investigations are excluded inside the SELECT
    so the function cannot enqueue a watcher run for the watcher's own
    failure — preventing a recursive cascade after a watcher crash. When
    not set (test fixtures, single-orchestrator dev), the guard is a
    no-op.
    """
    if not _database_url():
        return []
    # Recursion guard: exclude rows where investigations.agent_id matches
    # the watcher's own agent ID. Effective only after the watcher agent
    # provisioning (PR 4) sets agent_id at investigation-creation time —
    # before PR 4 lands, watcher-owned rows have NULL agent_id and would
    # not be excluded. The defense is layered:
    #
    #   - PR 1 (this file): SQL recursion guard
    #   - PR 4: watcher agent's investigation creation sets agent_id
    #   - WATCHER_ENABLED=false is the practical safety net until PR 4 ships
    #
    # NULL is admitted by design — non-watcher rows from existing code
    # paths that don't set agent_id (most adhoc sessions) must still be
    # enqueued.
    # Recursion guard applied unconditionally when watcher_agent_id is
    # provided. NULL agent_id is admitted — legacy rows without agent_id
    # must still be enqueued.
    agent_clause = "AND (i.agent_id IS NULL OR i.agent_id <> %s)" if watcher_agent_id else ""
    # "Already in queue" check uses source_inv_ids so hash-collapsed
    # rows correctly cover all their source investigations — not just
    # the first one stored in the legacy ``inv_id`` column.
    sql = f"""
        SELECT i.id, i.channel_id, i.thread_ts, i.error_message
        FROM investigations i
        LEFT JOIN watcher_pending wp
            ON i.id = ANY(wp.source_inv_ids) OR wp.inv_id = i.id
        WHERE i.completed_at >= %s
          AND i.status = 'failed'
          AND wp.id IS NULL
          {agent_clause}
        ORDER BY i.completed_at ASC
    """
    params: tuple
    if watcher_agent_id:
        params = (since, watcher_agent_id)
    else:
        params = (since,)
    enqueued: list[int] = []
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            candidates = cur.fetchall()
        for inv_id, channel_id, thread_ts, error_message in candidates:
            # Hash computation is shared with bin/audit-error-categories.py
            # so the dedup key matches the audit's bucketing exactly.
            from error_hash import compute as _compute_hash  # noqa: WPS433

            h = _compute_hash(error_message)
            new_id = enqueue_watcher_pending(
                inv_id=inv_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                error_category=None,
                error_message_hash=h,
                catch_up=True,
            )
            if new_id is not None:
                enqueued.append(inv_id)
    finally:
        conn.close()
    return enqueued


# ───────────────────────────────────────────────────────────────────────
# Kill-switch counter
# ───────────────────────────────────────────────────────────────────────


def list_completed_24h() -> list[dict[str, Any]]:
    """Return completed watcher_pending rows from the last 24h.

    Filters to ``status='completed'`` only — ``diagnose_only`` is
    excluded because no PR is created on that path (fix area outside
    allowlist OR GitHub auth failure → admin DM, no draft PR). Treating
    diagnose_only as a "PR backlog" entry inflates the kill-switch
    denominator and would falsely trip the safety shutdown after enough
    diagnose-only outcomes pile up.

    The kill-switch denominator (unmerged AND unreviewed auto-PRs) is
    computed by the caller, which has the GitHub MCP to check PR state.
    This function just hands back the rows that could be in the
    denominator — the caller filters down with reviewed/merged checks.
    """
    if not _database_url():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    sql = """
        SELECT id, inv_id, error_message_hash, status, attempts,
               first_seen_at, last_attempt_at, updated_at
        FROM watcher_pending
        WHERE status = 'completed'
          AND updated_at >= %s
        ORDER BY updated_at DESC
    """
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (cutoff,))
            description = cur.description or []
            cols = [d[0] for d in description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        return rows
    finally:
        conn.close()


def count_unmerged_unreviewed_24h(
    reviewed_or_merged: Callable[[dict[str, Any]], bool],
) -> int:
    """Stateless kill-switch counter.

    Walks the last 24h of completed watcher rows and returns the count
    that are NEITHER merged NOR reviewed per the injected callback.
    ``reviewed_or_merged(row)`` should return True if the operator has
    either merged the auto-PR or left a review (any review type counts
    as "sponged").
    """
    rows = list_completed_24h()
    count = 0
    for r in rows:
        try:
            if not reviewed_or_merged(r):
                count += 1
        except Exception:  # pragma: no cover — defensive
            log.exception(
                "watcher kill-switch: reviewed_or_merged check raised for id=%s",
                r.get("id"),
            )
    return count
