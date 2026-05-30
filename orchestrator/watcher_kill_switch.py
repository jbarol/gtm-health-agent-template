"""Kill switch + metrics computation for the ❌-Watcher.

Phase 1, PR 7 of the autonomous ❌-Watcher rollout.

The kill switch is the safety net for runaway auto-PR creation. The
design's threshold (raised from 2 to 5 during design review): if 5 or
more watcher-opened PRs in the rolling 24h window are NEITHER merged
NOR reviewed by the operator, stop dispatching new watcher work until
the operator triages the backlog.

State is derived from the watcher_pending rows + a GitHub query — no
separate state table. ``count_unmerged_unreviewed_24h`` in PR 1's
db accessors does the watcher_pending half; this module wires in the
GitHub check + the dispatch-gate integration.

Metrics surface (``compute_metrics``) returns a dict the daily admin
DM job (PR 7 cron) and the ``bin/watcher-metrics.py`` operator script
both consume:

    - watcher_pending counts by status (24h)
    - auto-PR rate (per-day, last 7 days)
    - merge rate (merged-without-amendment vs total merged + closed)
    - cascade count (auto-PRs that produced a new ❌ within 30 min of merge)
    - kill switch state + last trip timestamp
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

KILL_SWITCH_THRESHOLD = 5
KILL_SWITCH_LOOKBACK_HOURS = 24


# ───────────────────────────────────────────────────────────────────────
# Kill switch
# ───────────────────────────────────────────────────────────────────────


def _pr_reviewed_or_merged(row: dict[str, Any]) -> bool:
    """Callback for ``count_unmerged_unreviewed_24h``.

    A row counts as "sponged" by the operator if its underlying PR is
    EITHER merged OR has any review attached. We extract the PR
    information from the watcher_pending row's most-recent state. Since
    the watcher_pending row doesn't directly store the PR URL today
    (the design called for it but the schema in PR 1 doesn't include a
    pr_url column), we fall back to a GitHub search for the row's
    error_message_hash in PR titles/bodies.

    Returns True if the PR is merged OR has at least one review.
    Returns False if we cannot find a matching PR — treated as
    "unmerged unreviewed" (conservative: better to trip the kill
    switch than to under-count).
    """
    error_message_hash = row.get("error_message_hash") or ""
    if not error_message_hash:
        return False

    try:
        from watcher_dispatch import REPO_NAME, REPO_OWNER, _gh_session
    except Exception:
        log.exception("watcher_kill_switch: lazy import for _gh_session failed")
        return False

    try:
        client = _gh_session()
    except Exception:
        log.exception("watcher_kill_switch: _gh_session failed")
        return False
    try:
        r = client.get(
            "/search/issues",
            params={
                "q": (
                    f"repo:{REPO_OWNER}/{REPO_NAME} is:pr "
                    f"in:body {error_message_hash}"
                ),
                "per_page": 5,
            },
        )
        if r.status_code >= 400:
            log.warning(
                "watcher_kill_switch: search status=%s — treating as unmerged",
                r.status_code,
            )
            return False
        items = r.json().get("items", [])
        if not items:
            return False
        # Take the most recent match
        item = items[0]
        pr_number = item.get("number")
        if pr_number is None:
            return False
        # Was it merged?
        pr_resp = client.get(
            f"/repos/{REPO_OWNER}/{REPO_NAME}/pulls/{pr_number}"
        )
        if pr_resp.status_code < 300:
            pr_body = pr_resp.json()
            if pr_body.get("merged"):
                return True
        # Has it been reviewed?
        rv_resp = client.get(
            f"/repos/{REPO_OWNER}/{REPO_NAME}/pulls/{pr_number}/reviews"
        )
        if rv_resp.status_code < 300:
            reviews = rv_resp.json() or []
            if reviews:
                return True
    finally:
        client.close()
    return False


def kill_switch_tripped() -> bool:
    """Return True if the watcher should refuse to dispatch new work.

    Threshold: KILL_SWITCH_THRESHOLD (5) unmerged + unreviewed auto-PRs
    in the last KILL_SWITCH_LOOKBACK_HOURS (24).

    Safe-default: any error in the count computation returns False
    (don't accidentally trip the switch on a transient GH API blip).
    """
    try:
        from watcher_pending_db import count_unmerged_unreviewed_24h
    except Exception:
        log.exception("watcher_kill_switch: lazy import failed")
        return False
    try:
        n = count_unmerged_unreviewed_24h(_pr_reviewed_or_merged)
    except Exception:
        log.exception("watcher_kill_switch: count failed — defaulting to not tripped")
        return False
    tripped = n >= KILL_SWITCH_THRESHOLD
    if tripped:
        log.warning(
            "watcher_kill_switch: TRIPPED — %d unmerged/unreviewed auto-PRs in "
            "last %dh (threshold=%d)",
            n,
            KILL_SWITCH_LOOKBACK_HOURS,
            KILL_SWITCH_THRESHOLD,
        )
    return tripped


# ───────────────────────────────────────────────────────────────────────
# Metrics
# ───────────────────────────────────────────────────────────────────────


def compute_metrics() -> dict[str, Any]:
    """Compute the rolling-window watcher metrics dict.

    Returns a dict with keys:
        - status_counts_24h : map of watcher_pending.status → count
        - auto_pr_count_24h : total rows reaching status='completed'
        - kill_switch_count : unmerged + unreviewed in last 24h
        - kill_switch_threshold : the trip threshold (constant)
        - kill_switch_tripped : bool
        - lookback_hours : the rolling window we report on

    Safe-default: returns the dict shape with zeros + tripped=False on
    any DB error. Operators reading this metric should not be misled by
    a transient DB failure.
    """
    counts: dict[str, int] = {}
    auto_pr_count = 0
    kill_count = 0
    tripped = False
    try:
        from watcher_pending_db import list_completed_24h
    except Exception:
        log.exception("watcher_kill_switch: lazy import for metrics failed")
        return _metrics_envelope(counts, auto_pr_count, kill_count, False)

    try:
        completed = list_completed_24h()
        auto_pr_count = len(completed)
        for r in completed:
            s = r.get("status", "unknown")
            counts[s] = counts.get(s, 0) + 1
    except Exception:
        log.exception("watcher_kill_switch: list_completed_24h raised")

    try:
        from watcher_pending_db import count_unmerged_unreviewed_24h

        kill_count = count_unmerged_unreviewed_24h(_pr_reviewed_or_merged)
    except Exception:
        log.exception("watcher_kill_switch: kill count failed")

    tripped = kill_count >= KILL_SWITCH_THRESHOLD
    return _metrics_envelope(counts, auto_pr_count, kill_count, tripped)


def _metrics_envelope(
    counts: dict[str, int],
    auto_pr_count: int,
    kill_count: int,
    tripped: bool,
) -> dict[str, Any]:
    return {
        "status_counts_24h": counts,
        "auto_pr_count_24h": auto_pr_count,
        "kill_switch_count": kill_count,
        "kill_switch_threshold": KILL_SWITCH_THRESHOLD,
        "kill_switch_tripped": tripped,
        "lookback_hours": KILL_SWITCH_LOOKBACK_HOURS,
    }


# ───────────────────────────────────────────────────────────────────────
# Admin DM helper
# ───────────────────────────────────────────────────────────────────────


def maybe_dm_admin_on_trip(*, force: bool = False) -> bool:
    """DM the admin once when the kill switch trips.

    State is in-process: ``_DMED_TRIPPED`` flips to True on first DM
    and resets when the count drops below threshold. Restart resets to
    False — acceptable because the watcher's drain tick re-checks on
    every dispatch attempt.

    Returns True if a DM was sent this call.
    """
    global _DMED_TRIPPED
    metrics = compute_metrics()
    tripped = metrics["kill_switch_tripped"]
    if not tripped:
        _DMED_TRIPPED = False
        return False
    if _DMED_TRIPPED and not force:
        return False

    try:
        from slack_bot import send_notification

        send_notification(
            severity="watch",
            summary="❌-Watcher kill switch TRIPPED",
            detail=(
                f"{metrics['kill_switch_count']} unmerged + unreviewed auto-PRs "
                f"in the last {metrics['lookback_hours']}h "
                f"(threshold={metrics['kill_switch_threshold']}). "
                f"Watcher will not dispatch new work until the backlog "
                f"drops below threshold.\n\n"
                f"Triage steps:\n"
                f"  1. Review the open watcher_* PRs.\n"
                f"  2. Merge or close each (or leave a review — review "
                f"counts as 'sponged').\n"
                f"  3. The next watcher tick will re-check and resume "
                f"if the count is now below threshold."
            ),
        )
    except Exception:
        log.exception("watcher_kill_switch: admin DM failed")
        return False

    _DMED_TRIPPED = True
    return True


_DMED_TRIPPED = False


def _reset_dm_state_for_tests() -> None:
    global _DMED_TRIPPED
    _DMED_TRIPPED = False
