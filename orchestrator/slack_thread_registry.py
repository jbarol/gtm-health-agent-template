"""Per-run, per-theme Slack thread registry (Plan #11).

When a nightly cron posts multiple artifacts (charts, tables, findings) for
the same THEME, they all belong in ONE Slack thread. The user shouldn't have
to scroll past five top-level messages to assemble what is logically one
report. This module owns the registry that maps
``(run_id, theme, channel_id) -> thread_ts``.

Design note — DB-first ordering (Task #22, supersedes PR #184):

    Postgres is authoritative. Each call to :func:`get_or_create_thread`
    that misses the in-memory cache attempts an atomic
    ``INSERT ... ON CONFLICT DO NOTHING`` of a placeholder row with
    ``thread_ts = NULL`` BEFORE posting to Slack. RETURNING tells us
    whether we won the claim:

      * Won  → post the Slack parent → UPDATE the row with the real ts
               → cache + return. If the Slack post raises, DELETE the
               placeholder so the next call can retry cleanly.
      * Lost → re-SELECT the row. If ``thread_ts`` is already populated,
               cache + return it. If still NULL (another writer is mid-
               flight), poll briefly; if still NULL after the budget,
               give up and post unthreaded.

    The orphan-sweep cron (every 15 min, registered in main.py) DELETEs
    placeholder rows older than ``max_age_minutes`` to catch the rare
    case where the claiming process crashed between INSERT and Slack
    post. The remaining race window is bounded by that sweep interval.

The five canonical themes (one parent thread per theme per run) are:

    * ``pipeline_review``
    * ``forecast_analysis``
    * ``dream_plan``
    * ``investigation_finding``   (multiple threads per run is OK; each
      investigation gets its own thread via a unique run_id+theme combo
      or by appending the investigation id to the theme — see the
      docstring of :func:`get_or_create_thread`)
    * ``cost_report``

The crons that produce these threads were retired in PR #164 pending a
JTBD redesign. This module is the WIRING so when they come back, the
threading shape is already in place — no second migration needed.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

log = logging.getLogger(__name__)


# Canonical theme set. Imported by `_dispatch_post_report` so it can validate
# the agent's payload BEFORE we touch Slack. New themes get added here.
VALID_THEMES: frozenset[str] = frozenset(
    {
        "pipeline_review",
        "forecast_analysis",
        "dream_plan",
        "investigation_finding",
        "cost_report",
    }
)


# Default closing line appended to the parent message. The agent's summary
# goes in verbatim ABOVE this line; we never paraphrase the agent. The
# pointer line is deliberately literal (no fancy formatting) so it reads
# the same in mobile clients that strip mrkdwn.
DEFAULT_PARENT_POINTER = "More details in thread ↓"  # "↓"


# Placeholder-wait budget when a concurrent claim is in flight. Five
# 100ms polls = 500ms total wait before we give up and post unthreaded.
# The Slack chat.postMessage P50 is ~200ms so the typical case lands on
# the first or second retry.
_PLACEHOLDER_POLL_INTERVAL_S = 0.1
_PLACEHOLDER_POLL_RETRIES = 5


# In-memory cache. Keyed on ``(run_id, theme, channel_id)``. Tuple keys
# avoid the string-concatenation collision class. The lock guards the dict
# only; the Slack post + DB upsert happen outside the lock to keep the
# critical section short.
_cache: dict[tuple[str, str, str], str] = {}
_cache_lock = threading.Lock()


def _today_run_id() -> str:
    """Default run_id keyed on UTC date.

    The cron scheduler runs in Pacific; cross-day boundary edge cases are
    handled by the caller passing an explicit ``run_id``. This helper is
    just the convenience default when none is supplied.
    """
    return "nightly-" + datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _db_lookup(run_id: str, theme: str, channel_id: str) -> Optional[str]:
    """Look up the real thread_ts for this key. None on miss OR placeholder.

    Returns the persisted ``thread_ts`` only if it is non-NULL. A NULL
    column means a placeholder is in flight (another writer claimed the
    slot but hasn't posted yet); the caller treats that as a miss and
    polls.
    """
    try:
        import db_adapter
    except Exception:  # pragma: no cover — import error in test stubs
        return None

    if not getattr(db_adapter, "DATABASE_URL", ""):
        return None

    try:
        conn = db_adapter._connect()
    except Exception:
        log.exception(
            "[NIGHTLY_THREAD_LOOKUP_FAILED] could not connect to DB; "
            "falling through to post fresh parent",
        )
        return None

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT thread_ts FROM nightly_run_threads "
                "WHERE run_id = %s AND theme = %s AND channel_id = %s",
                (run_id, theme, channel_id),
            )
            row = cur.fetchone()
        if not row:
            return None
        return row[0]  # may be None if a placeholder is in flight
    except Exception:
        log.exception("[NIGHTLY_THREAD_LOOKUP_FAILED] query raised")
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


# Sentinels for _db_try_claim's tri-state return value. "won" / "lost"
# describe the row contention outcome; "unavailable" means DB-down or
# connection raised — the caller skips the DB plumbing entirely and
# falls back to a memory-only post-then-cache flow (degraded mode, but
# better than dropping the parent).
_CLAIM_WON = "won"
_CLAIM_LOST = "lost"
_CLAIM_UNAVAILABLE = "unavailable"


def _db_try_claim(run_id: str, theme: str, channel_id: str) -> str:
    """Attempt an atomic placeholder INSERT. Tri-state return.

    Uses ``INSERT ... ON CONFLICT (run_id, theme, channel_id) DO NOTHING``
    with ``thread_ts = NULL``. Postgres reports row count: 1 on win, 0 on
    conflict. We do NOT use RETURNING because some Postgres versions only
    return rows on actual INSERT, not on conflict — rowcount is the
    portable signal.

    Returns one of :data:`_CLAIM_WON`, :data:`_CLAIM_LOST`,
    :data:`_CLAIM_UNAVAILABLE`. The caller routes on the sentinel:

      * WON         → post Slack then UPDATE the placeholder.
      * LOST        → poll for the winner's ts; fall back to unthreaded.
      * UNAVAILABLE → DB-down degraded mode; post to Slack, cache in
                      memory only, never reach the DB plumbing.
    """
    try:
        import db_adapter
    except Exception:  # pragma: no cover
        return _CLAIM_UNAVAILABLE

    if not getattr(db_adapter, "DATABASE_URL", ""):
        return _CLAIM_UNAVAILABLE

    try:
        conn = db_adapter._connect()
    except Exception:
        log.exception(
            "[NIGHTLY_THREAD_CLAIM_FAILED] could not connect to DB; "
            "falling back to memory-only mode",
        )
        return _CLAIM_UNAVAILABLE

    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO nightly_run_threads "
                "(run_id, theme, channel_id, thread_ts, placeholder_created_at) "
                "VALUES (%s, %s, %s, NULL, NOW()) "
                "ON CONFLICT (run_id, theme, channel_id) DO NOTHING",
                (run_id, theme, channel_id),
            )
            won = cur.rowcount == 1
        conn.commit()
        return _CLAIM_WON if won else _CLAIM_LOST
    except Exception:
        log.exception("[NIGHTLY_THREAD_CLAIM_FAILED] insert raised")
        try:
            conn.rollback()
        except Exception:
            pass
        return _CLAIM_UNAVAILABLE
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _db_update_ts(run_id: str, theme: str, channel_id: str, thread_ts: str) -> None:
    """UPDATE the placeholder row with the real Slack thread_ts.

    Called by the claim winner after a successful Slack post. Restricted
    to rows where thread_ts is NULL so a buggy double-call cannot
    overwrite a real ts.
    """
    try:
        import db_adapter
    except Exception:  # pragma: no cover
        return

    if not getattr(db_adapter, "DATABASE_URL", ""):
        return

    try:
        conn = db_adapter._connect()
    except Exception:
        log.exception(
            "[NIGHTLY_THREAD_UPDATE_FAILED] could not connect to DB; "
            "in-memory cache still holds the ts for this container",
        )
        return

    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE nightly_run_threads "
                "SET thread_ts = %s "
                "WHERE run_id = %s AND theme = %s AND channel_id = %s "
                "AND thread_ts IS NULL",
                (thread_ts, run_id, theme, channel_id),
            )
        conn.commit()
    except Exception:
        log.exception("[NIGHTLY_THREAD_UPDATE_FAILED] update raised")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _db_delete_placeholder(run_id: str, theme: str, channel_id: str) -> None:
    """DELETE our placeholder after a Slack-post failure.

    Only removes rows where thread_ts IS NULL so a racing winner's real
    ts is never deleted (defense in depth — the only caller is the
    Slack-post failure branch immediately after a successful claim).
    """
    try:
        import db_adapter
    except Exception:  # pragma: no cover
        return

    if not getattr(db_adapter, "DATABASE_URL", ""):
        return

    try:
        conn = db_adapter._connect()
    except Exception:
        log.exception(
            "[NIGHTLY_THREAD_DELETE_FAILED] could not connect to DB; "
            "orphan-sweep cron will reap the placeholder",
        )
        return

    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM nightly_run_threads "
                "WHERE run_id = %s AND theme = %s AND channel_id = %s "
                "AND thread_ts IS NULL",
                (run_id, theme, channel_id),
            )
        conn.commit()
    except Exception:
        log.exception("[NIGHTLY_THREAD_DELETE_FAILED] delete raised")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _sweep_orphan_placeholders(max_age_minutes: int = 10) -> int:
    """DELETE placeholders older than ``max_age_minutes``. Returns row count.

    Catches the case where a claiming process crashed between the INSERT
    and either the Slack post or the placeholder cleanup. Without this
    sweep, the orphan would block every future call on the same
    (run_id, theme, channel_id) key forever.

    Wired as a 15-minute APScheduler cron in main.py. Idempotent and
    safe to run on a clean table — DELETE matches zero rows and returns 0.
    """
    try:
        import db_adapter
    except Exception:  # pragma: no cover
        return 0

    if not getattr(db_adapter, "DATABASE_URL", ""):
        return 0

    try:
        conn = db_adapter._connect()
    except Exception:
        log.exception(
            "[NIGHTLY_THREAD_SWEEP_FAILED] could not connect to DB",
        )
        return 0

    try:
        with conn.cursor() as cur:
            # ``%s minutes`` interpolated as a SQL fragment is safe because
            # we cast to int first — no user-supplied SQL surface area.
            cur.execute(
                "DELETE FROM nightly_run_threads "
                "WHERE thread_ts IS NULL "
                "AND placeholder_created_at < NOW() - INTERVAL '%s minutes'"
                % int(max_age_minutes),
            )
            deleted = cur.rowcount
        conn.commit()
        if deleted:
            log.info(
                "[NIGHTLY_THREAD_SWEEP] reaped %d orphan placeholder(s) "
                "older than %d minute(s)",
                deleted,
                max_age_minutes,
            )
        return deleted
    except Exception:
        log.exception("[NIGHTLY_THREAD_SWEEP_FAILED] delete raised")
        try:
            conn.rollback()
        except Exception:
            pass
        return 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_or_create_thread(
    run_id: Optional[str],
    theme: str,
    channel_id: str,
    parent_summary: str,
    parent_pointer: str = DEFAULT_PARENT_POINTER,
    *,
    poster: Optional[Callable[..., str]] = None,
) -> Optional[str]:
    """Return the ``thread_ts`` to use as ``reply_to`` for this theme.

    DB-first ordering (Task #22): INSERT placeholder → post Slack →
    UPDATE placeholder with real ts. If the Slack post fails, DELETE
    the placeholder so the next call retries cleanly.

    Args:
        run_id: caller-supplied identifier for this cron run. When falsy,
            defaults to a UTC-date-keyed string. Pass an explicit run_id
            to group themes across timezone-spanning runs (e.g. a forecast
            cron that starts at 23:55 PT crossing midnight).
        theme: one of :data:`VALID_THEMES`. The function does NOT reject
            unknown themes here — the schema in `_dispatch_post_report`
            enforces that. We accept anything here so a hot-fix theme
            doesn't require a registry redeploy.
        channel_id: Slack channel to post in. Different channels for the
            same (run_id, theme) get different threads.
        parent_summary: the agent's headline / top-of-report copy. Pasted
            verbatim into the parent message. Never paraphrased.
        parent_pointer: closing line appended to the parent summary.
            Defaults to "More details in thread ↓".
        poster: optional override of the Slack posting function. The
            production code path passes ``slack_bot.send_notification``;
            tests inject a mock. Signature must accept
            ``severity``, ``summary``, and ``reply_to=None`` kwargs and
            return the new message ``ts``.

    Returns:
        The Slack ``thread_ts`` to pass as ``reply_to`` on every reply
        for this theme, or None if posting the parent message failed.
        ``None`` is a safe sentinel — the caller falls back to posting
        as a top-level message rather than dropping the data.
    """
    if not run_id:
        run_id = _today_run_id()

    if not channel_id:
        log.warning(
            "[NIGHTLY_THREAD_NO_CHANNEL] theme=%s — cannot anchor a thread "
            "without a channel_id; caller will post unthreaded",
            theme,
        )
        return None

    key = (run_id, theme, channel_id)

    # Fast path: in-memory hit.
    with _cache_lock:
        ts = _cache.get(key)
    if ts:
        return ts

    # Check DB for an already-landed real ts.
    db_ts = _db_lookup(run_id, theme, channel_id)
    if db_ts:
        with _cache_lock:
            existing = _cache.get(key)
            if existing:
                return existing
            _cache[key] = db_ts
        return db_ts

    # Miss in both layers — attempt an atomic claim before touching Slack.
    claim = _db_try_claim(run_id, theme, channel_id)

    if claim == _CLAIM_LOST:
        # Someone else claimed the slot. Poll the DB briefly for them
        # to land the real ts. If their write is still in flight after
        # the budget, give up and post unthreaded so we don't double-post.
        for _ in range(_PLACEHOLDER_POLL_RETRIES):
            time.sleep(_PLACEHOLDER_POLL_INTERVAL_S)
            polled = _db_lookup(run_id, theme, channel_id)
            if polled:
                with _cache_lock:
                    _cache[key] = polled
                return polled
        log.warning(
            "[NIGHTLY_THREAD_PLACEHOLDER_STUCK] theme=%s run_id=%s — "
            "concurrent writer never landed a thread_ts within %.1fs; "
            "caller will post unthreaded",
            theme,
            run_id,
            _PLACEHOLDER_POLL_INTERVAL_S * _PLACEHOLDER_POLL_RETRIES,
        )
        return None

    # WON or UNAVAILABLE: we proceed to post Slack. UNAVAILABLE skips
    # the DB UPDATE / placeholder cleanup (there's no row to manage),
    # but we still cache in memory so subsequent calls in this process
    # reuse the ts. This is the degraded-mode fallback when DATABASE_URL
    # is unset or the DB is unreachable — equivalent to the pre-DB-first
    # behavior of PR #184.
    db_backed = claim == _CLAIM_WON
    body = (parent_summary or "").rstrip()
    if parent_pointer:
        # Two newlines so Slack renders the pointer as its own paragraph,
        # not glued to the last line of the summary.
        body = (body + "\n\n" + parent_pointer).strip()

    if poster is None:
        try:
            from slack_bot import send_notification

            poster = send_notification
        except Exception:
            log.exception(
                "[NIGHTLY_THREAD_POSTER_UNAVAILABLE] slack_bot import failed; "
                "no parent message posted",
            )
            if db_backed:
                _db_delete_placeholder(run_id, theme, channel_id)
            return None

    post_failed = False
    new_ts: Optional[str] = None
    try:
        # severity='info' is intentional — the parent is a summary header,
        # not an alert. The reply that the agent ships next can carry the
        # critical/watch severity if needed. ``reply_to`` is None: this is
        # the new top-level message that anchors the thread.
        new_ts = poster(
            severity="info",
            summary=body,
            reply_to=None,
            channel=channel_id,
        )
    except TypeError:
        # Some test posters don't accept ``channel``. Retry without it
        # so the registry stays test-friendly.
        try:
            new_ts = poster(
                severity="info",
                summary=body,
                reply_to=None,
            )
        except Exception:
            log.exception(
                "[NIGHTLY_THREAD_POST_FAILED] parent message post raised",
            )
            post_failed = True
    except Exception:
        log.exception("[NIGHTLY_THREAD_POST_FAILED] parent message post raised")
        post_failed = True

    if post_failed or not new_ts:
        if not post_failed:
            log.warning(
                "[NIGHTLY_THREAD_POST_FAILED] poster returned empty ts for "
                "theme=%s run_id=%s — caller will post unthreaded",
                theme,
                run_id,
            )
        # Release the claim so the next call (possibly from a fresh
        # container after a restart) can retry cleanly.
        if db_backed:
            _db_delete_placeholder(run_id, theme, channel_id)
        return None

    # Persist the real ts BEFORE the cache write so a concurrent reader
    # in another container sees the same authoritative value. In the
    # DB-unavailable degraded mode we skip persistence and rely on the
    # in-memory cache alone for this process's lifetime.
    if db_backed:
        _db_update_ts(run_id, theme, channel_id, new_ts)

    with _cache_lock:
        # Recheck under lock — another thread may have raced ahead via
        # the polling branch above.
        existing = _cache.get(key)
        if existing:
            return existing
        _cache[key] = new_ts

    return new_ts


def _clear_cache_for_tests() -> None:
    """Reset the in-memory cache. ONLY for tests.

    Tests that exercise the get-or-create flow must start clean so that a
    prior test's writes don't leak into the next test's assertions. The
    function is private (single-leading-underscore) and lives at module
    scope so tests can call it from a fixture.
    """
    with _cache_lock:
        _cache.clear()
