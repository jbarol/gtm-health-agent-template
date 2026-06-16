"""Runs Managed Agent sessions: dream, investigation, and ad-hoc Slack queries."""

import contextlib
import json
import logging
import os
import random
import threading
import time
import unicodedata
import uuid
from datetime import datetime, timezone, timedelta as _timedelta
from pathlib import Path
from typing import Any, Callable, Optional, cast

import anthropic
import httpx
import config as _config
from config import (
    ANTHROPIC_API_KEY,
    ENVIRONMENT_ID,
    DREAM_AGENT_ID,
    COORDINATOR_ID,
    QUICK_AGENT_ID,
    PROMPT_ENGINEER_ID,
    METHODOLOGY_STORE_ID,
    HEALTH_STORE_ID,
    ACME_VAULT_ID,
    SLACK_VAULT_ID,
)
from slack_bot import (
    send_notification,
    post_analysis,
    post_chart_file,
    post_file,
    transition_reaction,
    REACTION_RECEIVED,
    REACTION_WORKING,
)
from portco_registry import get_portco_by_channel, extract_portco_from_question
from self_heal import review_session
from compresr_client import compress_prompt
import db_adapter
import editor as response_editor
import response_schemas
import response_renderer

log = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def sanitize_session_title(s: str, max_chars: int = 60) -> str:
    """Strip Unicode control/format chars and truncate for Anthropic session titles.

    Anthropic's Sessions API rejects ``session.title`` containing any Unicode
    control (Cc) or format (Cf) character with HTTP 400
    ``title: must not contain Unicode control or format characters``.
    Slack thread-context payloads routinely contain ``\\n``; pasted prompts
    can carry BOM (U+FEFF) or zero-width joiners — none safe for the title.

    Live repro 2026-05-21 15:49 PT (this repo): a thread follow-up question
    starting with ``"Thread context (earlier messages):\\n..."`` made the
    first 40 chars of the Prompt Engineer title contain a literal newline.
    Anthropic returned 400 and the preprocess pass was lost; the
    investigation fell through to the unimproved-prompt path.

    Returns ``"(untitled)"`` if every char is stripped — Anthropic also
    rejects empty titles.
    """
    cleaned = "".join(c for c in s if unicodedata.category(c) not in ("Cc", "Cf"))
    cleaned = cleaned[:max_chars].strip()
    return cleaned or "(untitled)"


class _FollowupBlocked(Exception):
    """Thread follow-up user.message rejected with HTTP 400.

    Raised when an existing session has unresolved ``requires_action`` events
    and cannot accept a new ``user.message``. Caught above
    ``_run_investigation_guarded`` so the lifecycle guard does not mark the
    user's message ❌; the follow-up path posts a Slack notice instead.

    Plan #47 Workstream A. Workstream B will add the retry queue.
    """

    def __init__(self, original_error: anthropic.BadRequestError):
        self.original_error = original_error
        super().__init__(str(original_error))


_FOLLOWUP_BLOCKED_USER_MSG = (
    "Your follow-up was received but the agent is mid sub-agent dispatch. Retry in 30s."
)


def _is_requires_action_400(error: anthropic.BadRequestError) -> bool:
    """True iff the 400 body/message indicates pending requires_action events.

    Codex P2 fix (2026-05-18): the follow-up sentinel must only catch the
    retryable case. Non-retryable 400s (invalid event payload, expired
    session, permission denied) need to fall through to the existing
    failure path so the user isn't told to retry into the same failure.
    """
    body = getattr(error, "body", None) or {}
    msg = ""
    if isinstance(body, dict):
        err = body.get("error") or {}
        if isinstance(err, dict):
            msg = str(err.get("message") or "")
    if not msg:
        msg = str(getattr(error, "message", "") or str(error))
    msg_lower = msg.lower()
    return "requires_action" in msg_lower or "requires action" in msg_lower


def _handle_followup_blocked(
    *,
    session_id: str,
    thread_ts: Optional[str],
    channel_id: Optional[str],
    user_id: Optional[str],
    event_ts: Optional[str],
    inv_id: Optional[int],
    error: anthropic.BadRequestError,
) -> None:
    """Surface a thread-followup 400 to the user.

    Plan #47 Workstream A only: post a Slack thread reply, log structured
    fields, and terminalize the investigation so the inv_id row doesn't
    sit stuck in 'running' and the user's ⏰ reaction flips to ❌. No
    retry enqueue — Workstream B replaces this with a followup_pending
    transition that the retry queue consumes.
    """
    log.warning(
        "[FOLLOWUP_PREFLIGHT_400] session=%s thread=%s inv_id=%s "
        "error_code=400 error_body=%s",
        session_id,
        thread_ts,
        inv_id,
        str(getattr(error, "message", str(error)))[:200],
    )
    try:
        send_notification(
            "watch",
            _FOLLOWUP_BLOCKED_USER_MSG,
            reply_to=thread_ts,
            channel=channel_id,
            requester_id=user_id,
        )
    except Exception:
        log.exception(
            "Failed to post followup-blocked notice to thread=%s session=%s",
            thread_ts,
            session_id,
        )

    # Plan #52 PR-A (2026-05-19): keep 👁 receipt, drop ⏰ working, do NOT
    # add ❌. The user's follow-up reply already has a "On it" ack from the
    # kickoff path; adding ❌ on top of that is the triple-signal that
    # confused Jared on 2026-05-19. We want the message to settle into a
    # clean state — 👁 stays (the bot saw the reply), ⏰ comes off (no
    # active work), no ❌ (this is a retryable condition, not a crash).
    #
    # Codex review on PR-A (2026-05-19) caught the earlier event_ts=None
    # variant: that path skipped ALL reaction handling in terminalize_lifecycle,
    # which left ⏰ stuck on the message forever. Correct fix is to remove
    # ⏰ explicitly here, THEN call terminalize_lifecycle with event_ts=None
    # so the lifecycle path doesn't try to re-flip a reaction that's
    # already settled. The DB row still terminalizes as TERMINAL_FAILURE
    # with the diagnostic error_message so operators reading
    # session_costs.outcome can distinguish blocked-followups from real
    # failures, and Workstream B's retry queue (when it ships) can
    # continue to consume those rows.
    #
    # Project rationale: docs/plans/50-stop-lifecycle-and-continue-routing.md §2
    if event_ts and channel_id:
        try:
            from slack_bot import REACTION_WORKING, remove_reaction

            remove_reaction(channel_id, event_ts, REACTION_WORKING)
        except Exception:
            log.exception(
                "[FOLLOWUP_REACTION_CLEANUP_FAILED] thread=%s event_ts=%s — "
                "non-fatal; the lifecycle DB row still terminalizes",
                thread_ts,
                event_ts,
            )
    try:
        from lifecycle import DeliveryState, terminalize_lifecycle

        terminalize_lifecycle(
            DeliveryState.TERMINAL_FAILURE,
            event_ts=None,
            channel_id=channel_id,
            inv_id=inv_id,
            error_message="followup_blocked:session_in_requires_action",
        )
    except Exception:
        log.exception(
            "[FOLLOWUP_TERMINALIZE_FAILED] inv_id=%s session=%s — Slack "
            "notice already posted; this is a bookkeeping leak",
            inv_id,
            session_id,
        )


CONTAINER_ID = os.environ.get("RAILWAY_DEPLOYMENT_ID") or str(uuid.uuid4())[:12]

# Keyed on ``(channel_id, thread_ts)`` so that two Slack channels can
# share a thread_ts without cross-pollinating sessions. Mirrors the
# composite PK on the ``thread_sessions`` DB table (migration 00AJ).
_thread_sessions: dict[tuple[str, str], str] = {}
# Parallel map of the ``config_version`` stamp that was current when
# ``_thread_sessions[key]`` was written. The reuse path compares this
# against ``db_adapter.current_config_version()`` and rejects mismatches
# so a prompt deploy invalidates every cached Coordinator session
# (migration 00AM, Plan #44 PR 8). Stored alongside instead of as a
# tuple so the recovery path at line ~4311 can keep its single-value
# write semantics.
_thread_session_versions: dict[tuple[str, str], Optional[str]] = {}
_thread_sessions_lock = threading.Lock()
_THREAD_SESSION_MAX = 50


def _int_env(name: str, default: int) -> int:
    """Read an int env var, falling back to ``default`` on missing/garbage."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("%s=%r is not an int; using default %d", name, raw, default)
        return default


# B1: when recovering an interrupted investigation, if the prior session's
# input-side tokens exceed this threshold, do NOT resume — archive the old
# session, clear the thread→session map, and start a fresh session. The
# 500K default leaves ~500K headroom under the 1M cap and aligns with the
# 750K watch threshold in session_watch.
RECOVERY_FRESH_THRESHOLD = _int_env("RECOVERY_FRESH_THRESHOLD", 500_000)

# B3: tool results above this row count are streamed to /mnt/session/outputs
# as an .xlsx and the model receives a compact handle (preview + summary
# stats + file_path) instead of the raw rows. Tunable for the threshold
# discovery work in future plans without a code change.
RESULT_VIRTUALIZE_THRESHOLD = _int_env("RESULT_VIRTUALIZE_THRESHOLD", 50)

# Per-session tracker of virtualized file paths. The Coordinator may forget
# to attach a virtualized file_path to post_report.payload.attachments — when
# that happens we still want the dispatcher to upload the file so the user
# gets the data. Keyed by session_id, value is a list of file paths from this
# session's virtualized tool results.
_session_virtualized_files: dict[str, list[str]] = {}
_session_virtualized_files_lock = threading.Lock()

# Plan #48 / Plan #52 PR-D: per-session split-files override. When the user's
# question contains a split-file keyword, this is set True so
# _dispatch_post_report skips consolidation and uploads N individual files.
# Dormant until Plan #52 PR-E wires the consume call.
_session_split_files: dict[str, bool] = {}
_session_split_files_lock = threading.Lock()

_SPLIT_FILES_KEYWORDS: tuple[str, ...] = (
    "separate files",
    "split files",
    "individual workbooks",
    "one per dataset",
    "csv each",
    "keep separate",
    "don't merge",
    "do not merge",
    "separate attachments",
    "individual files",
    "split by file",
    "separate xlsx",
    "separate excel",
)


def _detect_split_files(question: str) -> bool:
    """True if the user's question contains a split-files keyword (case-insensitive)."""
    q = (question or "").lower()
    return any(kw in q for kw in _SPLIT_FILES_KEYWORDS)


def _register_split_files_pref(session_id: Optional[str], split: bool) -> None:
    """Record the split-files preference for this session."""
    if not session_id:
        return
    with _session_split_files_lock:
        _session_split_files[session_id] = split


def _consume_split_files_pref(session_id: Optional[str]) -> bool:
    """Return and CLEAR the split-files preference (default False = consolidate)."""
    if not session_id:
        return False
    with _session_split_files_lock:
        return _session_split_files.pop(session_id, False)


# B7: per-session post_report validation retry counter. The agent gets up to
# POST_REPORT_MAX_RETRIES validation rejections (each fed back as
# ``is_error=true`` with the Pydantic error) before the dispatcher gives up
# and posts a single neutral line in-thread. Module-level dict keeps the
# count scoped to one session; cleared when the session completes (best
# effort — orphan entries get garbage-collected on process restart).
_post_report_retry_counts: dict[str, int] = {}
_post_report_retry_lock = threading.Lock()

POST_REPORT_MAX_RETRIES = 3


# Plan #52 PR-G: investigation statuses that short-circuit
# _dispatch_post_report. See the guard at the top of _dispatch_post_report
# for rationale. "completed" is intentionally EXCLUDED because the only
# path to "completed" is the success-side terminalize call in this same
# function — including it would self-terminate every legitimate retry.
# "archived" IS included (OQ1) because an archived row is no longer the
# operator's source of truth and we should not be posting new prose into
# the thread it points at.
_POST_REPORT_TERMINAL_GUARD_STATUSES = frozenset(
    {"cancelled", "failed", "interrupted", "orphan_dead_lettered", "archived"}
)


# Plan #52 PR-G (codex P1 follow-up): per-session marker that the
# cancelled-guard in ``_dispatch_post_report`` short-circuited a post_report
# call. The guard return dict carries ``_terminal: True``, but the outer
# session loop (``_stream_and_handle``) lives in a different scope from
# the caller's fallback (``run_adhoc_mcp_session`` line ~6155+ and
# friends), which decides whether to emit the "Investigation didn't
# produce a final report" Slack message when ``delivery_state.is_delivered()``
# is False. Without this marker, that caller-side fallback fires even
# though the investigation was already terminalized by the watchdog /
# /stop / recovery path that owns the terminal state — producing two
# contradictory user-facing posts on the same Slack message (the ❌
# watchdog notice AND the "incomplete" fallback). This registry lets
# the caller-side fallback consume the marker and suppress the
# redundant post. Lifetime is bounded by ``_consume_post_report_cancelled_guard``
# which clears on read (one-shot).
_post_report_cancelled_guard_sessions: set[str] = set()
_post_report_cancelled_guard_lock = threading.Lock()


def _mark_post_report_cancelled_guard_fired(session_id: str) -> None:
    """Record that the cancelled-guard fired for ``session_id``.

    Called from ``_dispatch_post_report`` immediately before the
    short-circuit return. Idempotent — multiple guard hits on the same
    session are treated as one (the marker just needs to be present
    for the caller-side fallback to suppress).
    """
    if not session_id:
        return
    with _post_report_cancelled_guard_lock:
        _post_report_cancelled_guard_sessions.add(session_id)


def _consume_post_report_cancelled_guard(session_id: str) -> bool:
    """Return True and clear the marker if the guard fired for ``session_id``.

    One-shot semantics: each session_id is consumed exactly once even if
    multiple fallback sites query it (the first site clears, subsequent
    queries see False and proceed normally — but in practice the
    caller-side fallback runs once per session lifecycle).
    """
    if not session_id:
        return False
    with _post_report_cancelled_guard_lock:
        if session_id in _post_report_cancelled_guard_sessions:
            _post_report_cancelled_guard_sessions.discard(session_id)
            return True
        return False


def _bump_post_report_retry(session_id: str) -> int:
    """Increment and return this session's post_report retry count."""
    if not session_id:
        return 0
    with _post_report_retry_lock:
        n = _post_report_retry_counts.get(session_id, 0) + 1
        _post_report_retry_counts[session_id] = n
        return n


def _clear_post_report_retries(session_id: str) -> None:
    """Drop the retry counter on success (or on terminal give-up)."""
    if not session_id:
        return
    with _post_report_retry_lock:
        _post_report_retry_counts.pop(session_id, None)


def _track_virtualized_file(session_id: Optional[str], file_path: str) -> None:
    """Record a virtualized file path against the session for later post_report
    attachment fallback. No-op when ``session_id`` is None."""
    if not session_id or not file_path:
        return
    with _session_virtualized_files_lock:
        _session_virtualized_files.setdefault(session_id, []).append(file_path)


def _consume_virtualized_files(session_id: Optional[str]) -> list[str]:
    """Pop and return the virtualized file paths for ``session_id``.

    Called by ``_dispatch_post_report`` after a successful Slack post. Files
    explicitly listed in ``payload.attachments`` win — this is the safety
    net for the case where the agent forgot to include them.
    """
    if not session_id:
        return []
    with _session_virtualized_files_lock:
        return _session_virtualized_files.pop(session_id, [])


# PR 10 — Block duplicate failed tool retries within a short window.
# Failure mode: sub-agents fire two parallel tool calls that both error,
# then retry them in parallel again 1 second later. Wastes cache and
# tokens. The guard records (session_id, tool_name, sha256(input_json))
# whenever a tool call returns an error, and blocks a second identical
# call within DUPLICATE_RETRY_WINDOW_SECONDS. Legitimate retries with a
# changed input (typo fix, narrowed date range) pass through because
# their hash differs. TTL sweep on each dispatch garbage-collects
# entries older than 10s.
DUPLICATE_RETRY_WINDOW_SECONDS = 5
_DUPLICATE_RETRY_TTL_SECONDS = 10

# Keyed by (session_id, tool_name, input_hash). Value is a dict with
# ``timestamp`` (float, monotonic seconds since epoch) and ``count``
# (int — how many distinct failures of this exact call have been
# recorded; useful for the error message). Module-level dict with
# session_id baked into the key avoids cross-session pollution — two
# concurrent sessions calling the same tool with the same input get
# independent guard slots.
_RECENT_FAILED_TOOL_CALLS: dict[tuple[str, str, str], dict] = {}
_recent_failed_tool_calls_lock = threading.Lock()


def _tool_input_hash(tool_input: dict) -> str:
    """Stable SHA-256 hash of a tool input dict.

    Sort keys before serializing so semantically-identical inputs with
    different key order hash to the same value. Falls back to
    ``str(tool_input)`` if the input is not JSON-serializable.
    """
    import hashlib

    try:
        canonical = json.dumps(tool_input, sort_keys=True, default=str)
    except (TypeError, ValueError):
        canonical = str(tool_input)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _sweep_failed_tool_calls(now: float) -> None:
    """Drop entries older than ``_DUPLICATE_RETRY_TTL_SECONDS`` from the
    failure log. Caller must hold the lock."""
    stale = [
        key
        for key, entry in _RECENT_FAILED_TOOL_CALLS.items()
        if now - entry["timestamp"] > _DUPLICATE_RETRY_TTL_SECONDS
    ]
    for key in stale:
        _RECENT_FAILED_TOOL_CALLS.pop(key, None)


def _check_duplicate_retry(
    session_id: Optional[str], tool_name: str, tool_input: dict
) -> Optional[str]:
    """Return an error JSON string if this exact tool call failed within
    the last ``DUPLICATE_RETRY_WINDOW_SECONDS`` seconds, else None.

    No-op when ``session_id`` is None (e.g. tests that bypass the
    session wrapper). Sweeps stale entries on each call.
    """
    if not session_id:
        return None
    now = time.time()
    key = (session_id, tool_name, _tool_input_hash(tool_input))
    with _recent_failed_tool_calls_lock:
        _sweep_failed_tool_calls(now)
        entry = _RECENT_FAILED_TOOL_CALLS.get(key)
        if entry is None:
            return None
        age = now - entry["timestamp"]
        if age > DUPLICATE_RETRY_WINDOW_SECONDS:
            return None
        count = entry["count"]
    log.warning(
        "[DUPLICATE_RETRY_BLOCKED] tool=%s session=%s age=%.1fs count=%d",
        tool_name,
        session_id,
        age,
        count,
    )
    return json.dumps(
        {
            "error": "duplicate_retry_too_fast",
            "message": (
                f"Last call to {tool_name} with same input failed "
                f"{age:.0f}s ago. Wait or fix root cause."
            ),
            "tool": tool_name,
            "retry_after_seconds": max(0.0, DUPLICATE_RETRY_WINDOW_SECONDS - age),
        }
    )


def _register_failed_tool_call(
    session_id: Optional[str], tool_name: str, tool_input: dict
) -> None:
    """Record that a tool call returned an error so a second identical
    call within ``DUPLICATE_RETRY_WINDOW_SECONDS`` will be blocked.

    No-op when ``session_id`` is None.
    """
    if not session_id:
        return
    now = time.time()
    key = (session_id, tool_name, _tool_input_hash(tool_input))
    with _recent_failed_tool_calls_lock:
        _sweep_failed_tool_calls(now)
        prior = _RECENT_FAILED_TOOL_CALLS.get(key)
        count = (prior["count"] + 1) if prior else 1
        _RECENT_FAILED_TOOL_CALLS[key] = {"timestamp": now, "count": count}


def _result_is_error(result_json: str) -> bool:
    """Return True if the dispatcher's return value carries an error.

    Tools surface failures as a JSON object with an ``"error"`` key
    (the existing convention — see the bare-exception handler at the
    bottom of ``_dispatch_tool`` and the per-tool ``{"error": ...}``
    returns).  Returns False on parse failures (a non-JSON return is
    not the dispatcher's contract — treat it as success).

    Subtlety (codex review of PR 10): some tools — e.g.
    ``review_rfp_draft`` via ``RFPReviewResult.to_dict()`` (previously
    also ``write_prose``, removed 2026-05-27 when the Writing Agent
    moved to multiagent dispatch) — ALWAYS include an ``error`` key in
    their payload, defaulting to ``""`` on success. Treating "key
    present" as failure would poison the duplicate-retry cache for valid
    repeat calls (rejection loops can fire identical inputs within
    seconds). So:
      • If ``ok`` is explicitly present, trust it: ``ok=False`` → error,
        ``ok=True`` → success regardless of the ``error`` field value.
      • Otherwise fall back to truthy ``error`` (empty string, ``None``,
        and ``0`` are all "no error here").
    """
    try:
        parsed = json.loads(result_json)
    except (TypeError, ValueError):
        return False
    if not isinstance(parsed, dict):
        return False
    # When ``ok`` is provided, it is the authoritative success flag.
    if "ok" in parsed:
        return parsed["ok"] is False
    # No ``ok`` field — require a truthy ``error`` value.
    return bool(parsed.get("error"))


# Verbosity is REQUEST-scoped, not session-scoped. The verbosity for each
# Slack message is plumbed through run_adhoc_mcp_session -> _stream_and_handle
# -> _dispatch_tool -> _dispatch_post_report as a parameter. No shared state.
# Concurrent investigations in the same session_id cannot stomp each other.


# ---------------------------------------------------------------------------
# Tool-capability map (PR 3, floating-prancing-trinket plan)
#
# Mirrors the per-agent tool config in ``agents/setup_agents.py`` so the
# orchestrator can detect when the Coordinator dispatches a tool-shaped
# task to a sub-agent that lacks the required tool. The Anthropic Managed
# Agents runtime does not enforce tool-capability at the multiagent boundary
# — a Coordinator can instruct any sub-agent to "search Kapa for X" even
# when that sub-agent's tools[] does not include
# ``search_knowledge_base``. Pre-PR-3 failure mode: Pipeline
# Monitor (no Kapa) received such an instruction, could not call the
# tool, then cited stale memory rather than erroring cleanly. Wasted a
# turn and produced wrong output.
#
# Keys are the env-var names that resolve to each agent's ID (the same
# names threaded through ``.github/workflows/deploy-prompts.yml``). Values
# are the set of CUSTOM TOOL names declared in each agent's tools[] in
# ``setup_agents.py`` — built-ins from ``agent_toolset_20260401`` (python,
# files, bash) are deliberately omitted because they're universal and not
# the source of dispatch mismatches. MCP-server tools are listed by their
# advertised tool name (e.g. Kapa MCP exposed
# ``search_knowledge_base`` before the 2026-05-13 REST pivot;
# the name persists as a custom tool today).
#
# IMPORTANT: this map MUST stay in sync with the per-agent tools[] block
# in ``agents/setup_agents.py``. The comment block above each tool roster
# constant (SUB_AGENT_DATA_TOOLS, SUB_AGENT_DATA_TOOLS_WITH_KAPA, etc.)
# in setup_agents.py points readers here. When you add or remove a tool
# from a roster, update this map in the same PR.
#
# Tools NOT relevant for multiagent-dispatch routing (post_report,
# send_slack_notification, generate_chart) are still listed for
# completeness — they describe what the agent CAN do, which is what the
# Coordinator may reference in dispatch text. The dispatch guard only
# fires on the subset enumerated in ``_TOOL_HINTS_TO_CHECK`` below.
# write_prose was removed 2026-05-27 — prose composition is now a
# multiagent delegation to WRITING_AGENT_ID, not a custom tool call.
# ---------------------------------------------------------------------------
TOOL_CAPABILITY_MAP: dict[str, set[str]] = {
    "COORDINATOR_ID": {
        "post_report",
        "materialize_xlsx",
        "send_slack_notification",
        "search_knowledge_base",
    },
    "QUICK_ANSWER_ID": {
        "db_query",
        "dump_sf_query",
        "query_artifact",
        "post_report",
        "search_knowledge_base",
    },
    "DREAM_AGENT_ID": {
        "db_query",
        "query_artifact",
        "search_knowledge_base",
    },
    "PIPELINE_MONITOR_ID": {
        "db_query",
        "dump_sf_query",
        "query_artifact",
    },
    "SALES_MONITOR_ID": {
        "db_query",
        "dump_sf_query",
        "query_artifact",
    },
    "POSTSALES_MONITOR_ID": {
        "db_query",
        "dump_sf_query",
        "query_artifact",
        "search_knowledge_base",
    },
    "STATISTICIAN_ID": {
        "db_query",
        "dump_sf_query",
        "query_artifact",
    },
    "ADVERSARIAL_REVIEWER_ID": {
        "db_query",
        "query_artifact",
    },
    "CROSS_DOMAIN_SYNTHESIZER_ID": {
        "db_query",
        "query_artifact",
        "search_knowledge_base",
    },
    "CHART_DESIGNER_ID": {
        "db_query",
        "query_artifact",
        "generate_chart",
    },
    "WRITING_AGENT_ID": {
        # Writing Agent joined the Coordinator's multiagent roster
        # 2026-05-27. Its <data_access_contract> prompt block lets it
        # call query_artifact to sanity-check suspicious numbers in the
        # delegation payload before composing prose. The dispatch guard
        # has to see query_artifact here or it will reject a Coordinator
        # delegation that names the tool with a false
        # ``tool_capability_mismatch``.
        "query_artifact",
    },
    "PROMPT_ENGINEER_ID": set(),
}


# Map from agent display names (the ``to_agent_name`` value attached to
# ``agent.thread_message_sent`` events) → the env-var key in
# ``TOOL_CAPABILITY_MAP``. Display names come from each agent's ``name=``
# in ``client.beta.agents.create()`` in ``setup_agents.py``. Case-
# insensitive lookup in ``_lookup_capability_key`` tolerates "Pipeline
# Monitor" vs "pipeline monitor".
_AGENT_NAME_TO_ENV_KEY: dict[str, str] = {
    "gtm health coordinator": "COORDINATOR_ID",
    "coordinator": "COORDINATOR_ID",
    "gtm quick answer": "QUICK_ANSWER_ID",
    "quick answer": "QUICK_ANSWER_ID",
    "gtm dream analyst": "DREAM_AGENT_ID",
    "dream agent": "DREAM_AGENT_ID",
    "pipeline monitor": "PIPELINE_MONITOR_ID",
    "sales process monitor": "SALES_MONITOR_ID",
    "sales monitor": "SALES_MONITOR_ID",
    "post-sales monitor": "POSTSALES_MONITOR_ID",
    "postsales monitor": "POSTSALES_MONITOR_ID",
    "statistician": "STATISTICIAN_ID",
    "adversarial reviewer": "ADVERSARIAL_REVIEWER_ID",
    "cross-domain synthesizer": "CROSS_DOMAIN_SYNTHESIZER_ID",
    "cross domain synthesizer": "CROSS_DOMAIN_SYNTHESIZER_ID",
    "chart designer": "CHART_DESIGNER_ID",
    "gtm writing agent": "WRITING_AGENT_ID",
    "writing agent": "WRITING_AGENT_ID",
    "prompt engineer": "PROMPT_ENGINEER_ID",
}


# Tool names that, when referenced in a dispatch body, trigger the
# capability check. Limited to tools where a missing entry causes a
# silent stale-cite failure (the Kapa case). Tools like ``post_report``
# are not in this list because Coordinator owns them and never dispatches
# them out — but they're still in TOOL_CAPABILITY_MAP for completeness.
#
# ``soqlQuery`` is deliberately EXCLUDED even though it appears in
# dispatch bodies. It is an MCP-toolset tool (lives behind
# ``SF_MCP_TOOLSET``), NOT a custom tool name, so it is never present in
# any TOOL_CAPABILITY_MAP entry — adding it to the hints list would make
# every Salesforce dispatch flag as ``tool_capability_mismatch`` with
# ``redispatch_to: (none)``. The Coordinator's standard safety reminder
# ("use ``dump_sf_query``, do NOT call ``soqlQuery``") makes this an
# almost-guaranteed false positive on every data-side dispatch.
# Codex review PR #194 caught this — see commit 7167869 + fix.
_TOOL_HINTS_TO_CHECK: tuple[str, ...] = (
    "search_knowledge_base",
    "dump_sf_query",
    "generate_chart",
    "query_artifact",
    "db_query",
    "materialize_xlsx",
)


# Kapa-enabled fallback list surfaced in the structured error so the
# Coordinator knows where to redispatch. Pulled from TOOL_CAPABILITY_MAP
# at module-load so the lists never drift.
_KAPA_CAPABLE_AGENTS: tuple[str, ...] = tuple(
    sorted(
        env_key
        for env_key, tools in TOOL_CAPABILITY_MAP.items()
        if "search_knowledge_base" in tools
    )
)


def _lookup_capability_key(agent_name):
    """Map a ``to_agent_name`` value to its TOOL_CAPABILITY_MAP env-var key.

    Returns ``None`` when the name isn't recognized (unknown agents skip
    the check rather than fail loud — adding a new agent shouldn't break
    routing on the day of provisioning).
    """
    if not agent_name:
        return None
    return _AGENT_NAME_TO_ENV_KEY.get(agent_name.strip().lower())


def _extract_text_from_event(event) -> str:
    """Pull a single plain-text representation out of a multiagent event.

    Supports the event shapes the SDK ships today; the caller in
    ``_stream_and_handle`` only invokes this on
    ``agent.thread_message_sent`` (the Coordinator's committed
    dispatch), which carries the full body in ``content=[...text...]``.
    Fallbacks to ``body`` (str) and empty string keep the substring
    search safe — false-negatives are safer than false-positives here.
    """
    content = getattr(event, "content", None)
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            text = getattr(block, "text", None) or (
                block.get("text") if isinstance(block, dict) else None
            )
            if text:
                parts.append(text)
        if parts:
            return "\n".join(parts)
    body = getattr(event, "body", None)
    if isinstance(body, str):
        return body
    return ""


def check_dispatch_capability(
    to_agent_name,
    dispatch_body,
):
    """Return a structured error dict if the dispatch targets a missing tool.

    Walks ``_TOOL_HINTS_TO_CHECK`` looking for any tool name mentioned in
    ``dispatch_body`` (substring match). If found, looks up the
    destination agent's tool set in ``TOOL_CAPABILITY_MAP``. If the agent
    is enumerated AND does not carry that tool, returns:

        {
            "error": "tool_capability_mismatch",
            "destination_agent": <env-var key>,
            "missing_tool": <tool name>,
            "message": <human-readable explanation, machine-parseable>,
            "redispatch_to": <comma-separated list of agents that have the tool>,
        }

    Returns ``None`` when:
      - No tool hint matches the body
      - The destination agent is unknown (defensive — don't break routing
        on a new agent that hasn't been added to the map yet)
      - The destination DOES have the tool

    Used by the multiagent dispatch path in ``_stream_and_handle`` to
    inject a recoverable error back to the Coordinator session so it
    can re-plan rather than dispatch into the void.
    """
    if not dispatch_body:
        return None
    cap_key = _lookup_capability_key(to_agent_name)
    if cap_key is None:
        return None
    # Writing Agent exemption (codex P2, 2026-05-27). The Writing Agent
    # delegation body is a structured prose-composition payload — the
    # Coordinator pastes ``{response_shape, payload, feedback?}`` JSON
    # whose ``payload`` contains arbitrary findings, often with
    # provenance fields like ``evidence_query: "db_query ..."`` or
    # methodology notes referencing ``materialize_xlsx``. Those are
    # DATA strings, not tool dispatch instructions, but the
    # substring-based capability guard cannot tell the difference. The
    # Writing Agent itself is a single-turn prose composer that does
    # not dispatch sub-tools (only ``query_artifact`` and
    # ``reasoning_summary``), so the guard's protection here is low-value
    # and its false-positive blast radius is high — every prose
    # delegation that mentions a data-pull tool name in its provenance
    # would be rejected. Skip the scan.
    if cap_key == "WRITING_AGENT_ID":
        return None
    agent_tools = TOOL_CAPABILITY_MAP.get(cap_key)
    if agent_tools is None:
        return None
    body_lower = dispatch_body.lower()
    for tool_name in _TOOL_HINTS_TO_CHECK:
        if tool_name.lower() not in body_lower:
            continue
        if tool_name in agent_tools:
            return None  # destination has the tool — dispatch is fine
        # Mismatch — build the structured error.
        capable_agents = sorted(
            env_key
            for env_key, tools in TOOL_CAPABILITY_MAP.items()
            if tool_name in tools
        )
        capable_list = ", ".join(capable_agents) if capable_agents else "(none)"
        message = (
            f"error: tool_capability_mismatch — destination agent "
            f"{cap_key} does not have tool {tool_name}. "
            f"Redispatch to one of: {capable_list}."
        )
        return {
            "error": "tool_capability_mismatch",
            "destination_agent": cap_key,
            "missing_tool": tool_name,
            "message": message,
            "redispatch_to": capable_agents,
        }
    return None


# Plan #44 Task #9 — Pin agent version on every sessions.create() call.
# The pin file (agents/active_versions.json) is loaded into config.AGENT_VERSIONS
# at import time. If a name is missing from the file, we fall back to the bare
# agent ID so the call still works (Anthropic resolves to latest version).
#
# ``_AGENT_NAME_BY_ID`` is the reverse map we need at the call sites: each
# sessions.create has the agent ID in hand and needs the pin file key to look
# up the version. The mapping is established at module-load time from
# environment IDs; tests can patch it for synthetic agents.
_AGENT_NAME_BY_ID: dict[str, str] = {}
for _name, _id_value in (
    ("coordinator", COORDINATOR_ID),
    ("dream", DREAM_AGENT_ID),
    ("quick_answer", QUICK_AGENT_ID),
    ("prompt_engineer", PROMPT_ENGINEER_ID),
    # writing_agent is read lazily by writing_agent.py itself (it pulls from
    # os.environ at call time so deploy rotation works without a restart);
    # it does its own pin resolution and is NOT registered here.
):
    if _id_value:
        _AGENT_NAME_BY_ID[_id_value] = _name


def _resolve_agent_param(agent_id: str):
    """Return the ``agent`` argument for ``sessions.create``.

    Resolves the effective pin for the agent in this order:
      1. Postgres override (Bundle E ``/pin`` slash command, Task #10)
      2. File pin (``agents/active_versions.json``, loaded into
         ``config.AGENT_VERSIONS`` at boot, Task #8)
      3. Bare ID — Anthropic resolves to latest (pre-Plan-#44 behavior)

    Returns the structured form
    ``{"type": "agent", "id": ..., "version": N}`` when a pin resolves;
    otherwise the bare ID string.
    """
    if not agent_id:
        return agent_id
    name = _AGENT_NAME_BY_ID.get(agent_id)
    if not name:
        return agent_id
    file_pin = _config.AGENT_VERSIONS.get(name)
    try:
        # Lazy import to avoid pulling psycopg2 + DATABASE_URL resolution
        # at module-load time for tests/CLIs that don't need pins.
        from version_pin_overrides import effective_pin

        version = effective_pin(name, file_pin if isinstance(file_pin, int) else None)
    except Exception:
        version = file_pin if isinstance(file_pin, int) else None
    if not isinstance(version, int):
        return agent_id
    return {"type": "agent", "id": agent_id, "version": version}


def invalidate_thread_session_cache_for_agent(agent_name: str) -> dict[str, int]:
    """Drop any cached thread→session entries whose session targets ``agent_name``.

    Per Plan #44 decision row #4: when an agent's pin changes (rollback,
    /pin override, prompt redeploy), in-flight thread sessions that were
    created against the OLD version must NOT be reused for the next user
    message — they will keep responding from the stale snapshot. This helper
    clears the in-memory thread map and the DB-backed thread_sessions table.
    Bundle E's /pin and rollback handlers will call this.

    Today the call is a best-effort cache flush. The agent_name argument is
    intentionally not filtered yet (we don't track agent-per-session), so
    the conservative behavior is to invalidate ALL thread sessions when ANY
    pinned agent changes. That's the safest reading of decision row #4 —
    the alternative (allowing follow-ups to silently stay on the old pin)
    is the failure mode the decision row was explicitly avoiding.

    Returns ``{"memory_evicted": N, "db_rows_cleared": M}`` (closing-review
    fix 2026-05-13). Previously this returned a single ``int`` derived
    from the in-memory dict length, which diverges from the DB-side
    rowcount after a container restart: the memory cache is empty on a
    fresh container while the DB still holds the historical rows. The
    two-key dict surfaces both numbers so an operator inspecting the
    Slack ack message after a ``/pin`` can tell whether the live cache
    or the persistent ledger was the bigger contributor.
    """
    with _thread_sessions_lock:
        memory_evicted = len(_thread_sessions)
        _thread_sessions.clear()
        _thread_session_versions.clear()
    # Best-effort DB clear. Errors are logged, not raised — the caller may
    # be the Slack /pin handler and an exception would kill the slash
    # command response. Bundle E owns the DB side once thread sessions
    # carry agent_id metadata; today we clear ``thread_sessions`` entirely.
    db_rows_cleared = 0
    try:
        if hasattr(db_adapter, "clear_all_thread_sessions"):
            db_rows_cleared = int(db_adapter.clear_all_thread_sessions() or 0)
    except Exception:
        log.exception("Failed to clear thread_sessions DB rows during pin invalidation")
    log.info(
        "Invalidated thread sessions for agent=%s (Plan #44 row #4): "
        "memory_evicted=%d db_rows_cleared=%d",
        agent_name,
        memory_evicted,
        db_rows_cleared,
    )
    return {
        "memory_evicted": memory_evicted,
        "db_rows_cleared": db_rows_cleared,
    }


# Plan #44 Task #16 — Buffer session_thread_events inserts per session.
#
# Per decision row #11, we buffer rows in-process and flush on
# session.status_idle so a 500-1K/session insert burst doesn't hammer the DB.
# Keyed by session_id; the buffer is cleared on flush or session terminate.
_thread_event_buffer: dict[str, list[dict]] = {}
_thread_event_buffer_lock = threading.Lock()

# Event types we capture into session_thread_events. The set is conservative
# — additions land here as we observe new types in production (per the
# retry_status enum discovery pattern). Anything not listed gets ignored.
_THREAD_EVENT_TYPES_TO_CAPTURE = {
    "session.thread_started",
    "session.thread_terminated",
    "session.thread_status_idle",
    "session.thread_status_running",
    "session.thread_status_rescheduled",
    "agent.thread_message_started",
    "agent.thread_message_completed",
    "agent.thread_message_delta",
}


def _redact_event_payload(event_type: str, raw_payload: dict) -> dict:
    """Strip PII-sensitive fields before persistence.

    Per Plan #44 decision row #11: redact ``tool_input.q`` (SOQL — may
    contain account/lead names + filters that name individuals) and
    ``content[*]`` (free-form text that may carry user-supplied PII).
    Keep IDs, timestamps, agent_name, thread_id, stop_reason — those
    are forensic gold without exposing data.
    """
    if not isinstance(raw_payload, dict):
        return {}
    redacted = {}
    for key, value in raw_payload.items():
        if key == "tool_input" and isinstance(value, dict):
            # Drop SOQL (the canonical "q") and any other long string
            # values; keep the keys themselves so we know what fields
            # were passed.
            ti = {}
            for k, v in value.items():
                if k == "q" or (isinstance(v, str) and len(v) > 256):
                    ti[k] = "[REDACTED]"
                else:
                    ti[k] = v
            redacted["tool_input"] = ti
        elif key == "content":
            # content is the user-facing text or tool-result body.
            # Always redact — replace with a length marker for forensics.
            if isinstance(value, list):
                redacted["content"] = [{"redacted_blocks": len(value)}]
            elif isinstance(value, str):
                redacted["content"] = f"[REDACTED:{len(value)} chars]"
            else:
                redacted["content"] = "[REDACTED]"
        else:
            redacted[key] = value
    return redacted


def _buffer_thread_event(
    session_id: str,
    event,
    *,
    portco_key: Optional[str] = None,
) -> None:
    """Append a thread event to the per-session buffer.

    Best-effort: never raises. The buffer flushes on session.status_idle
    or session terminate; on process crash the buffer is lost, which is
    acceptable for telemetry data.
    """
    if not session_id:
        return
    event_type = getattr(event, "type", "") or ""
    if event_type not in _THREAD_EVENT_TYPES_TO_CAPTURE:
        return

    thread_id = getattr(event, "session_thread_id", None) or getattr(
        event, "thread_id", None
    )
    ts = getattr(event, "processed_at", None) or getattr(event, "created_at", None)

    # Pull a structured payload off the SDK event object. The exact shape
    # depends on event type; we serialize-and-redact rather than reach into
    # every variant. ``model_dump`` is the anthropic SDK's standard pydantic
    # serializer; we fall back to vars() and {} on the way out so the buffer
    # cannot crash the event loop.
    raw_payload = {}
    try:
        if hasattr(event, "model_dump"):
            raw_payload = event.model_dump()
        else:
            raw_payload = dict(vars(event))
    except Exception:
        raw_payload = {}

    payload = _redact_event_payload(event_type, raw_payload)
    # Capture processed_at separately as a string so consumers can
    # re-order without re-parsing the redacted blob.
    try:
        if ts is not None and hasattr(ts, "isoformat"):
            payload["processed_at"] = ts.isoformat()
        elif ts:
            payload["processed_at"] = str(ts)
        else:
            payload["processed_at"] = None
    except Exception:
        pass
    if portco_key:
        payload["portco_key"] = portco_key

    agent_name = (
        getattr(event, "agent_name", None) or getattr(event, "agent_id", None) or None
    )

    entry = {
        "session_id": session_id,
        "thread_id": thread_id,
        "event_type": event_type,
        "agent_name": agent_name,
        "ts": ts,
        "payload_json": payload,
    }
    with _thread_event_buffer_lock:
        _thread_event_buffer.setdefault(session_id, []).append(entry)


def _flush_thread_event_buffer(session_id: str) -> int:
    """Flush buffered events for ``session_id`` to Postgres. Returns row count."""
    if not session_id:
        return 0
    with _thread_event_buffer_lock:
        events = _thread_event_buffer.pop(session_id, [])
    if not events:
        return 0
    try:
        return db_adapter.insert_session_thread_events(events)
    except Exception:
        log.exception(
            "Flushing session_thread_events buffer failed for session=%s — "
            "events dropped (telemetry-only, not load-bearing)",
            session_id,
        )
        return 0


# Plan #44 Task #13 — Build session-level ``instructions`` for multi-turn
# sessions. Docs cap this at 4096 chars; we keep well under that by emitting
# a compact identity + portco + channel + standing-rules block. Single-turn
# sessions (Quick Answer, Prompt Engineer, Writing Agent) skip this — they
# pay 4096 chars × per-call cost with no second-turn cache payoff.
# VERBOSITY is request-scoped (it varies per Slack message) and is therefore
# excluded — putting it in instructions would lock the thread to one mode.

# Hard ceiling per docs (managed-agents/sessions: ``instructions`` ≤ 4096).
_SESSION_INSTRUCTIONS_MAX_CHARS = 4096


def _build_session_instructions(
    *,
    portco_key: Optional[str] = None,
    channel_id: Optional[str] = None,
    extra_lines: Optional[list] = None,
) -> str:
    """Compose session-level ``instructions`` payload (≤4096 chars).

    Includes portco identity + channel and a short standing-rules block.
    The agent's system prompt already carries the per-agent persona; this
    field carries the per-SESSION facts. Truncation is conservative — we
    drop trailing extras before any identity line.
    """
    lines = []
    if portco_key:
        lines.append(f"Portco: {portco_key}")
    if channel_id:
        lines.append(f"Slack channel: {channel_id}")
    # Standing rules — short. Long-form rules live in /{portco}/instructions.md
    # in the health memory store and are read at first-turn time by the
    # agent. Repeating them here would just waste tokens. What goes here are
    # the conventions that apply regardless of portco:
    lines.append(
        "Standing rules: read /{portco}/instructions.md from the health memory "
        "store FIRST. Numbers with commas; percentages with 1 decimal. Never "
        "post unvalidated findings; the validation pipeline is mandatory."
    )
    if extra_lines:
        lines.extend(extra_lines)
    out = "\n".join(lines)
    if len(out) > _SESSION_INSTRUCTIONS_MAX_CHARS:
        # Trim from the end (extras-first). Identity lines stay intact.
        out = out[: _SESSION_INSTRUCTIONS_MAX_CHARS - 3] + "..."
    return out


def _prepend_session_instructions(
    user_text: str,
    *,
    portco_key: Optional[str] = None,
    channel_id: Optional[str] = None,
    extra_lines: Optional[list] = None,
) -> str:
    """Fold ``_build_session_instructions`` output into the first user
    message body. Anthropic SDK 0.100.0+ removed the ``instructions=``
    kwarg on ``Sessions.create``; sessions that previously relied on
    it now carry the same text as a prefix to the first ``user.message``
    so the agent reads it on turn 1.

    Returns the original ``user_text`` if there are no instructions to
    fold (defensive — keeps the caller's branch-free).
    """
    body = _build_session_instructions(
        portco_key=portco_key,
        channel_id=channel_id,
        extra_lines=extra_lines,
    )
    if not body:
        return user_text
    return f"[Session instructions]\n{body}\n\n[Task]\n{user_text}"


# Plan #44 Task #5 — ``OUTPUTS_DIR`` is the LOCAL download cache for files
# pulled back from Anthropic's session via the Files API before they get
# uploaded to Slack. This is the orchestrator container's host disk, NOT
# the agent-visible session disk. Cross-reference:
#   - Agent-visible writes target /mnt/session/outputs (see _build_adhoc_prompt
#     and every "Write report to /mnt/session/outputs/…" instruction).
#   - This local cache holds the .xlsx/.csv/.png files we download via
#     ``client.beta.files.download()`` in ``_download_session_files``
#     (~L1467 below) and then ``post_file`` to Slack.
# Renaming this constant breaks the local cache path on Railway (which uses
# /tmp for ephemeral host storage) and is intentionally NOT done here.
# See Plan #44 decision row #9.
OUTPUTS_DIR = Path("/tmp/gtm-health-agent/outputs")
RUBRICS_DIR = Path(__file__).parent.parent / "rubrics"

MEMORY_RESOURCES = [
    {
        "type": "memory_store",
        "memory_store_id": METHODOLOGY_STORE_ID,
        "access": "read_only",
        "instructions": "GTM methodology reference. Check benchmarks, metric definitions, and investigation patterns here.",
    },
    {
        "type": "memory_store",
        "memory_store_id": HEALTH_STORE_ID,
        "access": "read_write",
        "instructions": (
            "Persistent GTM health memory. CRITICAL: At the start of EVERY run, read "
            "/{portco}/instructions.md FIRST — it contains mandatory data rules (which fields "
            "to use, what to exclude, how to segment). Then read metrics.md, open_questions.md, "
            "findings.md, resolved.md, schema_cache.md. Write updates directly as you discover "
            "new findings or resolve questions."
        ),
    },
]

# Kapa is a REST custom tool (see orchestrator/kapa_rest_tool.py); it does
# not require a vault at session create time. Only SF + Slack vaults attach.
def _collect_vault_ids() -> list:
    """Vaults attached at session create time: the env-configured SF/Slack
    vaults PLUS every vault declared in portco_config.json (top-level
    ``vault_ids`` map + per-portco ``vault_id``), so multi-portco forks attach
    their own SF MCP vaults via config rather than only the single
    ACME_VAULT_ID env var. Placeholder values (``vlt_REPLACE...``) and blanks
    are skipped so a fresh fork never sends a placeholder to the API."""
    ids = [ACME_VAULT_ID, SLACK_VAULT_ID]
    try:
        from portco_registry import get_all_portcos, get_registry

        ids.extend((get_registry().get("vault_ids", {}) or {}).values())
        for _p in get_all_portcos():
            _crm = (_p.get("data_sources", {}) or {}).get("crm", {}) or {}
            ids.append(_p.get("vault_id") or _crm.get("vault_id"))
    except Exception:
        pass
    out, seen = [], set()
    for _v in ids:
        _s = str(_v)
        if (
            _v
            and _v not in seen
            and "..." not in _s
            and not _s.startswith("vlt_REPLACE")
        ):
            seen.add(_v)
            out.append(_v)
    return out


VAULT_IDS = _collect_vault_ids()

QUICK_ANSWER_PATTERNS = [
    "how many",
    "what is the",
    "what's the",
    "what are the",
    "list the",
    "show me the",
    "count of",
    "total",
    "who owns",
    "when did",
    "when was",
    "what stage",
    "which rep",
]


def _ensure_dirs():
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)


def _upload_file(path: Path) -> str:
    with open(path, "rb") as f:
        uploaded = client.beta.files.upload(file=f)
    return uploaded.id


def _is_simple_lookup(question: str) -> bool:
    """Only route to Quick Answer for genuinely simple, single-fact lookups."""
    q = question.lower()
    if len(q.split()) > 20:
        return False
    return any(q.startswith(p) for p in QUICK_ANSWER_PATTERNS)


def _dispatch_tool(
    tool_name: str,
    tool_input: dict,
    thread_ts: Optional[str] = None,
    session_id: Optional[str] = None,
    verbosity: str = "summary",
    portco_key: Optional[str] = None,
    user_id: Optional[str] = None,
    event_ts: Optional[str] = None,
    channel_id: Optional[str] = None,
    inv_id: Optional[int] = None,
) -> str:
    """Execute a custom tool and return the result as a JSON string.

    verbosity is request-scoped (passed from _stream_and_handle for this
    specific message). post_report renders in this mode; other tools ignore it.

    portco_key is forwarded to _dispatch_post_report so the post-report hook
    can fire the canvas push for the right portco. Other tools ignore it.

    user_id is the Slack user ID of the thread's original requester. Forwarded
    to _dispatch_post_report so the @-mention pings the asker, not the global
    SLACK_NOTIFY_USER_IDS admin list. None for cron flows.

    event_ts + channel_id, when set, identify the user's original Slack
    message so the lifecycle reaction (👁 → ⏰ → ✅/❌) can be flipped
    on the right message at the post_report success boundary.
    """
    # PR 10: short-circuit when the same tool call failed for this session
    # within the duplicate-retry window. The agent's prompt is instructed
    # to serialize retries; this is the orchestrator-side enforcement.
    blocked = _check_duplicate_retry(session_id, tool_name, tool_input)
    if blocked is not None:
        return blocked

    result = _dispatch_tool_impl(
        tool_name,
        tool_input,
        thread_ts=thread_ts,
        session_id=session_id,
        verbosity=verbosity,
        portco_key=portco_key,
        user_id=user_id,
        event_ts=event_ts,
        channel_id=channel_id,
        inv_id=inv_id,
    )
    if _result_is_error(result):
        _register_failed_tool_call(session_id, tool_name, tool_input)
    return result


def _dispatch_tool_impl(
    tool_name: str,
    tool_input: dict,
    thread_ts: Optional[str] = None,
    session_id: Optional[str] = None,
    verbosity: str = "summary",
    portco_key: Optional[str] = None,
    user_id: Optional[str] = None,
    event_ts: Optional[str] = None,
    channel_id: Optional[str] = None,
    inv_id: Optional[int] = None,
) -> str:
    """Inner tool dispatcher. Returns a JSON string with either the tool's
    result or a structured ``{"error": ...}`` payload.

    ``_dispatch_tool`` wraps this with the duplicate-retry guard. The
    split keeps the existing dispatch ladder unchanged.
    """
    log.info(f"Custom tool call: {tool_name}({json.dumps(tool_input)[:200]})")

    try:
        if tool_name == "send_slack_notification":
            summary = tool_input["summary"]
            detail = tool_input.get("detail", "")
            combined = (summary + " " + detail).lower()
            chatter_signals = [
                "let me dispatch",
                "i'll dispatch",
                "dispatching",
                "i'll wait for",
                "waiting for the",
                "awaiting results",
                "let me ask",
                "sending to",
                "handing off to",
                "pipeline monitor to",
                "sales monitor to",
                "post-sales monitor to",
                "statistician to",
                "chart designer to",
                "adversarial reviewer to",
                "ending turn",
                "unblocked",
                "blocked on",
            ]
            if any(sig in combined for sig in chatter_signals):
                log.warning(
                    f"Blocked orchestration chatter from Slack: {summary[:120]}"
                )
                return json.dumps(
                    {"ok": True, "blocked": "orchestration_chatter", "message_ts": ""}
                )

            reply_to = tool_input.get("reply_to") or thread_ts
            ts = send_notification(
                severity=tool_input["severity"],
                summary=summary,
                detail=detail,
                reply_to=reply_to,
            )
            return json.dumps({"ok": True, "message_ts": ts})

        elif tool_name == "save_snapshot_batch":
            records = tool_input["records"]
            db_adapter.write_records(
                tool_input["snapshot_id"],
                tool_input["portco_key"],
                tool_input["object_type"],
                records,
            )
            return json.dumps({"ok": True, "saved": len(records)})

        elif tool_name == "db_query":
            if not db_adapter.is_db_available():
                return json.dumps({"error": "Database not available"})
            sql = tool_input["sql"].strip()
            # Read-only guard: SELECT and WITH (CTE-prefixed SELECT) are
            # both allowed. The validator below enforces the same
            # constraint via its ``non_select`` code, but this fast-path
            # rejection avoids importing the validator on a DROP/UPDATE/
            # INSERT call. Codex P2 review #3 on PR #178 — WITH queries
            # were being silently rejected here before reaching the
            # validator.
            sql_head = sql.upper().lstrip("(").lstrip()
            if not (sql_head.startswith("SELECT") or sql_head.startswith("WITH")):
                return json.dumps({"error": "Only SELECT/WITH queries are allowed"})
            # Schema-aware pre-flight (Plan #44 SQL validator).
            # Catches the typo'd-column / missing-table class of bugs
            # BEFORE Postgres raises a raw psycopg2 exception that the
            # model can't act on. Pass-through on empty schema (DB
            # unavailable, snapshot not yet built).
            from sql_validator import validate_sql

            schema_snapshot = db_adapter.get_schema_snapshot()
            validation = validate_sql(sql, schema_snapshot)
            if not validation.get("ok"):
                log.info(
                    "db_query rejected by validator (%s): %s",
                    validation.get("code"),
                    validation.get("error"),
                )
                return json.dumps(validation)
            result = db_adapter.query(sql)
            records = result.get("records") or []
            # B3: virtualize any list-shaped result above the threshold.
            # Keeps massive result sets out of the model's context — the model
            # gets preview + summary stats + a file_path it can use with the
            # Python tool or attach to post_report. The legacy ``max_rows``
            # truncation path is preserved for callers that pass it
            # explicitly, but is now a no-op for the typical >50-row case
            # because virtualization runs first.
            if len(records) > RESULT_VIRTUALIZE_THRESHOLD:
                virtualized = _virtualize_tool_result(
                    records, tool_name, session_id=session_id
                )
                return json.dumps(virtualized, default=str)
            max_rows = tool_input.get("max_rows", 500)
            if len(records) > max_rows:
                result["records"] = records[:max_rows]
                result["truncated"] = True
                result["totalSize"] = result["totalSize"]
            return json.dumps(result, default=str)

        elif tool_name == "generate_chart":
            chart_bytes = _render_chart_bytes(tool_input)
            reply_to = tool_input.get("reply_to") or thread_ts
            ts = post_chart_file(
                title=tool_input["title"],
                chart_bytes=chart_bytes,
                reply_to=reply_to,
            )
            return json.dumps({"ok": True, "message_ts": ts})

        elif tool_name == "post_report":
            return _dispatch_post_report(
                tool_input,
                thread_ts,
                session_id,
                verbosity,
                portco_key=portco_key,
                user_id=user_id,
                event_ts=event_ts,
                channel_id=channel_id,
                inv_id=inv_id,
            )

        elif tool_name == "write_prose":
            # Compatibility shim for sessions persisted under the pre-2026-05-27
            # Coordinator prompt + tool schema. Those sessions can still emit
            # ``agent.custom_tool_use { tool_name: "write_prose" }`` on Slack
            # follow-ups after deploy; with the real dispatcher removed, the
            # fall-through ``Unknown tool: write_prose`` error terminalizes
            # the thread. Instead, return the same ``{ok: false, error: ...}``
            # fail-soft shape the old write_prose handler used on failures,
            # so the old Coordinator prompt's rejection loop falls through
            # cleanly to direct post_report (its documented retry budget
            # treats ok=false as one strike). When sessions stamped with
            # config_version < 2026-05-27 are all archived (via the
            # prompt-deploy session invalidation in
            # ``_archive_and_invalidate_session``), this branch can be
            # removed. Until then, leave it in place — a one-time strike
            # against the rejection budget costs the user one extra turn,
            # but the alternative is a stuck Slack thread.
            log.info(
                "[WRITE_PROSE_COMPAT_SHIM] session=%s emitted retired write_prose "
                "tool — returning fail-soft to drive Coordinator fallthrough.",
                session_id,
            )
            return json.dumps(
                {
                    "ok": False,
                    "error": (
                        "write_prose_retired: the write_prose custom tool was "
                        "removed 2026-05-27. Skip the rejection loop and call "
                        "post_report directly; the Writing Agent is now a "
                        "multiagent-roster sub-agent and the Coordinator's "
                        "current prompt delegates to it instead of calling a "
                        "custom tool. This session was created under the prior "
                        "prompt — its remaining turns must fall through to "
                        "direct post_report."
                    ),
                    "prose": "",
                    "caveats": [],
                    "decision_recommendation": "",
                    "duration_seconds": 0.0,
                    "session_id": session_id or "",
                }
            )

        elif tool_name == "review_rfp_draft":
            return _dispatch_review_rfp_draft(tool_input)

        elif tool_name == "reasoning_summary":
            return _dispatch_reasoning_summary(tool_input, session_id=session_id)

        elif tool_name == "search_knowledge_base":
            # Kapa REST custom tool — replaces the retired Kapa MCP toolset
            # (2026-05-13 pivot; see orchestrator/kapa_rest_tool.py docstring).
            # The agent prompts that previously used the MCP toolset don't
            # know the difference — same tool name, same input shape (query).
            from kapa_rest_tool import search_kapa

            from config import KAPA_ACME_API_KEY, KAPA_ACME_PROJECT_ID

            # Resolve Kapa credentials from the active portco's knowledge config
            # (env-var names are declared in portco_config.json), so a multi-portco
            # fork resolves each portco's own KAPA_<KEY>_* vars. Falls back to the
            # Acme example vars so a single-portco "acme" setup works out of the
            # box and tests that patch config.KAPA_ACME_* still hold.
            _kapa_api_key, _kapa_project_id = KAPA_ACME_API_KEY, KAPA_ACME_PROJECT_ID
            if portco_key:
                try:
                    from portco_registry import get_portco_config

                    _knowledge = (
                        (get_portco_config(portco_key) or {})
                        .get("data_sources", {})
                        .get("knowledge", {})
                    )
                    _key_env = _knowledge.get("api_key_env")
                    _proj_env = _knowledge.get("project_id_env")
                    if _key_env and _key_env != "KAPA_ACME_API_KEY":
                        _kapa_api_key = os.environ.get(_key_env, "")
                    if _proj_env and _proj_env != "KAPA_ACME_PROJECT_ID":
                        _kapa_project_id = os.environ.get(_proj_env, "")
                except Exception:
                    pass  # fall back to the Acme defaults on any config issue

            result = search_kapa(
                query=tool_input.get("query", ""),
                api_key=_kapa_api_key,
                project_id=_kapa_project_id,
            )
            if not result.get("ok"):
                # Structured error for the agent. Surface the error type
                # but not the API key. Matches the way dump_sf_query
                # signals failure — agent prompts know how to ignore.
                return json.dumps(
                    {
                        "ok": False,
                        "error": result.get("error", "unknown"),
                        "detail": (result.get("detail", "") or "")[:300],
                    }
                )
            # Mirror the MCP search tool's payload shape: a single
            # ``content`` field with markdown body + sources appendix.
            return json.dumps(
                {
                    "ok": True,
                    "content": result.get("content", ""),
                    "is_uncertain": result.get("is_uncertain", False),
                    "source_count": result.get("source_count", 0),
                    "elapsed_s": result.get("elapsed_s", 0.0),
                }
            )

        elif tool_name == "dump_sf_query":
            # Track G of Iteration 2 in plan ``misty-squishing-badger``:
            # materialize a Salesforce SOQL query to a Parquet file
            # server-side and return a compact handle. Raw rows never enter
            # the agent's context, so the Coordinator does not bloat on a
            # 3,209-lead pull (live test on commit 90b9bb5 hit 966K of 1M).
            #
            # Lazy import — avoids a circular dependency, since
            # ``sf_dump_tool`` calls back into ``session_runner._get_sf_client``
            # for credential resolution.
            from sf_dump_tool import dump_sf_query

            result = dump_sf_query(
                soql=tool_input["soql"],
                portco_key=tool_input["portco_key"],
                label=tool_input["label"],
                # PR 9: default-shrunk handle (<= 8 KB). Agents that need the
                # full payload pass ``expand=true`` in their tool input.
                expand=bool(tool_input.get("expand", False)),
            )
            log.info(
                "[DUMP_SF_QUERY] portco=%s label=%s rows=%d file=%s",
                tool_input.get("portco_key"),
                tool_input.get("label"),
                result.get("count", 0),
                result.get("file_path") or "(none)",
            )
            # Track the materialized file on the session so
            # ``_dispatch_post_report`` can attach it even if the agent
            # forgets to include it in ``payload.attachments``.
            if result.get("file_path"):
                _track_virtualized_file(session_id, result["file_path"])
            return json.dumps(result, default=str)

        elif tool_name == "query_artifact":
            # Track H of Iteration 2: run DuckDB SQL against previously-
            # materialized artifact files. Lazy import to avoid pulling
            # duckdb at module-load time (some tests don't need it).
            from artifact_query_tool import query_artifact

            result = query_artifact(
                file_paths=tool_input["file_paths"],
                sql=tool_input["sql"],
                # Design #16 (2026-05-15) — let the Coordinator name the output
                # file semantically (e.g. q2_2026_propensity_scored). Sanitized
                # in query_artifact; bad names fall back to qa_<ts>_<uuid>.
                output_name=tool_input.get("output_name"),
            )
            log.info(
                "[QUERY_ARTIFACT] files=%d rows=%d inline=%s",
                len(tool_input.get("file_paths", [])),
                result.get("row_count", 0),
                result.get("inline", True),
            )
            if not result.get("inline") and result.get("file_path"):
                _track_virtualized_file(session_id, result["file_path"])
            return json.dumps(result, default=str)

        elif tool_name == "materialize_xlsx":
            # Deliverable-export tool added 2026-05-13 after the call-prep
            # session (sesn_EXAMPLE) silently failed at the
            # COPY rejection. Lets the Coordinator turn one or more Parquet
            # handles into a single named .xlsx for Slack upload without
            # trying to bypass query_artifact's read-only sandbox.
            from materialize_xlsx_tool import materialize_xlsx

            result = materialize_xlsx(
                output_name=tool_input["output_name"],
                file_paths=tool_input.get("file_paths"),
                sql=tool_input.get("sql"),
                sheet_name=tool_input.get("sheet_name", "data"),
                sheets=tool_input.get("sheets"),
            )
            log.info(
                "[MATERIALIZE_XLSX] ok=%s sheets=%d total_rows=%d path=%s",
                result.get("ok", False),
                len(result.get("sheets") or []),
                result.get("total_rows", 0),
                result.get("file_path") or "(none)",
            )
            # Track the produced xlsx on the session so `_dispatch_post_report`
            # can attach it even if the agent forgets to include it in
            # `payload.attachments`. Matches the dump_sf_query / query_artifact
            # contract.
            if result.get("ok") and result.get("file_path"):
                _track_virtualized_file(session_id, result["file_path"])
            return json.dumps(result, default=str)

        elif tool_name == "watcher_create_branch":
            # Phase 1 PR 5 — ❌-Watcher's custom tool surface. All 4
            # branches call into watcher_dispatch which owns the GH
            # REST plumbing + path allowlist + branch-prefix guard +
            # conflict check. Each handler returns a {ok, ...} envelope
            # the watcher agent can parse without raising.
            from watcher_dispatch import watcher_create_branch

            return json.dumps(
                watcher_create_branch(
                    branch_name=tool_input["branch_name"],
                    inv_id=inv_id,
                    session_id=session_id,
                )
            )

        elif tool_name == "watcher_write_file":
            from watcher_dispatch import get_active_branch, watcher_write_file

            branch = tool_input.get("branch") or get_active_branch(session_id)
            if not branch:
                return json.dumps(
                    {
                        "ok": False,
                        "reason": "no_active_branch",
                        "details": (
                            "Call watcher_create_branch before watcher_write_file."
                        ),
                    }
                )
            return json.dumps(
                watcher_write_file(
                    path=tool_input["path"],
                    content=tool_input["content"],
                    commit_message=tool_input["commit_message"],
                    branch=branch,
                )
            )

        elif tool_name == "watcher_create_pr":
            from watcher_dispatch import get_active_branch, watcher_create_pr

            branch = tool_input.get("branch") or get_active_branch(session_id)
            if not branch:
                return json.dumps(
                    {
                        "ok": False,
                        "reason": "no_active_branch",
                        "details": (
                            "Call watcher_create_branch before watcher_create_pr."
                        ),
                    }
                )
            return json.dumps(
                watcher_create_pr(
                    title=tool_input["title"],
                    body=tool_input["body"],
                    branch=branch,
                    session_id=session_id,
                )
            )

        elif tool_name == "watcher_add_comment":
            from watcher_dispatch import watcher_add_comment

            return json.dumps(
                watcher_add_comment(
                    pr_number=tool_input["pr_number"],
                    body=tool_input["body"],
                )
            )

        return json.dumps({"error": f"Unknown tool: {tool_name}"})

    except Exception as e:
        log.error(f"Tool {tool_name} failed: {e}")
        return json.dumps(
            {
                "error": str(e),
                "tool": tool_name,
                "hint": "Revise your query and try again. SOQL does not support CASE, COALESCE, or subqueries in SELECT.",
            }
        )


def _virtualize_tool_result(
    rows: list, tool_name: str, session_id: Optional[str] = None
) -> dict:
    """Stream a list-shaped tool result to .xlsx and return a compact handle.

    Wraps ``result_virtualize.virtualize_result`` with a small bridge:
      - Logs ``[RESULT_VIRTUALIZED]`` so an operator tailing Railway logs can
        see when a tool result was bypassed. No Slack post — virtualization
        is invisible to the user; they get the data via the attached file.
      - Tracks the produced file path on the session so
        ``_dispatch_post_report`` can attach it even when the agent forgets
        to include it in ``payload.attachments``.

    Designed to be reused as more locally-dispatched query tools land
    (hubspot_query, zoho_query — future). Cost is the openpyxl streaming
    write; for a 3,209-row Lead pull this is ~80 ms on Railway dynos.
    """
    from result_virtualize import virtualize_result  # lazy — avoid cold start cost

    handle = virtualize_result(rows, tool_name)
    file_path = handle.get("file_path")
    log.info(
        "[RESULT_VIRTUALIZED] tool=%s rows=%d file=%s",
        tool_name,
        handle.get("row_count", 0),
        file_path or "(none)",
    )
    if file_path:
        _track_virtualized_file(session_id, file_path)
    return handle


def _dispatch_review_rfp_draft(tool_input: dict) -> str:
    """Route the RFP Responder's ``review_rfp_draft`` call to the Reviewer.

    Tool input shape:
        {
          "qa_index": [{question_id, question, category, answer,
                        sources, basis, flagged, flag_reason}, ...],
          "feedback": "fix notes summary"  // optional, retry only
        }

    Returns a JSON string with the RFPReviewResult fields. The RFP
    Responder branches on ``verdict`` (PASS / REVISE) and revises up
    to 2x. ``ok=False`` is treated as a soft PASS by the responder's
    prompt to avoid blocking on a Reviewer outage.

    Never raises. ``rfp_reviewer.run_review`` follows the same fail-soft
    pattern the prior ``writing_agent.write_prose`` dispatch used (the
    Writing Agent is now a multiagent-roster delegation, no longer a
    custom tool) — every failure path returns ``RFPReviewResult(ok=False,
    ...)``.
    """
    try:
        # ``type: ignore`` covers Pyright cache lag on this newly-added
        # sibling module in ``orchestrator/``.
        from rfp_reviewer import run_review  # type: ignore[import-not-found]
    except Exception as e:
        log.error(f"rfp_reviewer import failed: {e}")
        return json.dumps({"ok": False, "error": f"rfp_reviewer_import_failed: {e}"})

    qa_index = tool_input.get("qa_index")
    feedback = tool_input.get("feedback")

    if not isinstance(qa_index, list):
        return json.dumps(
            {
                "ok": False,
                "error": "qa_index must be a JSON array of question records",
            }
        )
    if not qa_index:
        return json.dumps(
            {
                "ok": False,
                "error": "qa_index is empty — nothing to review",
            }
        )

    result = run_review(qa_index=qa_index, feedback=feedback)
    return json.dumps(result.to_dict())


# PR 11 — every agent calls ``reasoning_summary(text=...)`` before its final
# response with a ≤200-token recap (what it did / found / surprised it /
# couldn't resolve). The dispatcher appends the recap to a single rolling
# memory file at ``/system/session_reasoning_log.md`` in the health memory
# store so post-mortems can see sub-thread reasoning even when
# ``agent.thinking`` events emit zero-byte content for the configured
# session. Never raises; never stalls the session; the call returns
# {"ok": True} synchronously so the agent's final response goes right after.
REASONING_SUMMARY_MAX_CHARS = 1500
REASONING_LOG_PATH = "/system/session_reasoning_log.md"
_REASONING_LOG_CAP_CHARS = 90_000
# Process-local lock around the read-modify-write on
# /system/session_reasoning_log.md. The orchestrator runs up to
# MAX_CONCURRENT_INVESTIGATIONS sessions in parallel; without a lock
# two concurrent reasoning_summary dispatches both read the old
# content, both append their block, and the slower writer's content
# clobbers the faster one's append — silent loss of one recap. The
# lock is process-local; if the deploy ever scales to multiple
# containers this becomes "best-effort" again, but for the current
# single-container Railway service it eliminates the in-process race.
_reasoning_log_write_lock = threading.Lock()

# Background-write executor for reasoning_summary dispatches. The dispatcher
# is fire-and-forget: the agent's tool-use loop does not need to wait for
# the memory-store round-trip. Measured 2026-05-15: each call takes ~575ms
# (3 sequential HTTP round-trips against the memory store). At that latency,
# the SSE stream sits idle while the dispatch runs — not enough to cause an
# edge LB drop on its own (today's incidents had 76s-474s idle windows from
# the agent thinking, not from this dispatcher), but it's still a foot-gun
# waiting for a slower variant to hit.
#
# Worker count = 2: enough to absorb concurrent calls from the up-to-MAX
# parallel investigations without starving on a single slow round-trip,
# small enough that the queue can't grow unbounded (the agent's tool-use
# loop generates calls at human-thought speed, not millisecond-burst speed).
_reasoning_summary_executor = None  # lazy-init to avoid spinning threads at import


def _get_reasoning_summary_executor():
    global _reasoning_summary_executor
    if _reasoning_summary_executor is None:
        from concurrent.futures import ThreadPoolExecutor

        _reasoning_summary_executor = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="reasoning_summary",
        )
    return _reasoning_summary_executor


def _append_reasoning_block_safe(block: str, header_session: str) -> None:
    """Append a reasoning block under the process-local lock, swallowing failures.

    Called from the ThreadPoolExecutor worker. Never raises — a network blip
    on the memory store must not crash the worker thread or leave a stranded
    Future the dispatcher can't observe.
    """
    try:
        with _reasoning_log_write_lock:
            _append_reasoning_block(block)
    except Exception as exc:
        log.warning(
            "[REASONING_SUMMARY_WRITE_FAILED] session=%s err=%s",
            header_session,
            exc,
        )


def _dispatch_reasoning_summary(
    tool_input: dict, session_id: Optional[str] = None
) -> str:
    """Append the agent's reasoning recap to the health-store reasoning log.

    The tool input shape is ``{"text": "<≤1500 char recap>"}``. The text is
    truncated to ``REASONING_SUMMARY_MAX_CHARS`` if longer (no error — agents
    that overshoot still get useful telemetry). The dispatcher queues one
    block per call to a background ThreadPoolExecutor:

        ## <session_id> @ <iso_ts>
        <text>

        ---

    The dispatcher returns ``{"ok": True, "stored": "pending"}`` synchronously
    in <1ms so the agent's tool-use loop never waits on memory-store I/O —
    matching the dispatcher's original contract ("never stalls the session;
    the call returns {ok: True} synchronously so the agent's final response
    goes right after"). The write happens on the executor; failures are
    logged via ``_append_reasoning_block_safe`` and the recap is lost for
    that turn. The docstring already acknowledges this is observability
    infrastructure: losing one block is harmless, blocking the agent isn't.

    Why off-thread (2026-05-15): two prod investigations died on
    ``httpx.RemoteProtocolError`` during the agent's thinking phase, not
    during this dispatcher, but every inline custom-tool dispatcher holds
    the SSE iterator idle while it runs HTTP I/O — a small risk surface
    we don't need to keep open for a fire-and-forget observability write.
    """
    raw = tool_input.get("text", "")
    if not isinstance(raw, str):
        raw = str(raw or "")
    text = raw[:REASONING_SUMMARY_MAX_CHARS]
    truncated = len(raw) > REASONING_SUMMARY_MAX_CHARS

    iso_ts = datetime.now(timezone.utc).isoformat()
    header_session = session_id or "unknown_session"
    block = f"## {header_session} @ {iso_ts}\n{text}\n\n---\n"

    try:
        _get_reasoning_summary_executor().submit(
            _append_reasoning_block_safe, block, header_session
        )
    except Exception as exc:
        # Executor submit can fail if the pool is shutting down (rare —
        # only at orchestrator shutdown). Fall back to the synchronous
        # path so the recap is still attempted. Even synchronous failure
        # is swallowed by _append_reasoning_block_safe.
        log.warning(
            "[REASONING_SUMMARY_EXECUTOR_SUBMIT_FAILED] session=%s err=%s — "
            "falling back to synchronous write",
            header_session,
            exc,
        )
        _append_reasoning_block_safe(block, header_session)

    return json.dumps({"ok": True, "stored": "pending", "truncated": truncated})


def _append_reasoning_block(block: str) -> None:
    """Append one reasoning block to ``/system/session_reasoning_log.md``.

    Uses the same list/retrieve/update/create pattern as
    ``self_heal._save_clean_session`` — the live API offers no native
    append op. If the rolling log grows past ``_REASONING_LOG_CAP_CHARS``
    we keep the tail so the file never breaches the memory-store size
    cap (the live ceiling is materially higher; this is a conservative
    self-imposed limit matching the pattern in self_heal).
    """
    if not HEALTH_STORE_ID:
        raise RuntimeError("HEALTH_STORE_ID not configured")

    existing = None
    try:
        memories = client.beta.memory_stores.memories.list(
            HEALTH_STORE_ID,
            path_prefix="/system/session_reasoning_log",
        )
        for m in getattr(memories, "data", []) or []:
            if getattr(m, "path", None) == REASONING_LOG_PATH:
                existing = m
                break
    except Exception:
        # ``list`` failures are not fatal — fall through to create-or-update.
        existing = None

    if existing is not None:
        current = client.beta.memory_stores.memories.retrieve(
            existing.id,
            memory_store_id=HEALTH_STORE_ID,
        )
        updated = (getattr(current, "content", "") or "") + block
        if len(updated) > _REASONING_LOG_CAP_CHARS:
            updated = updated[-_REASONING_LOG_CAP_CHARS:]
        client.beta.memory_stores.memories.update(
            existing.id,
            memory_store_id=HEALTH_STORE_ID,
            content=updated,
        )
    else:
        client.beta.memory_stores.memories.create(
            HEALTH_STORE_ID,
            path=REASONING_LOG_PATH,
            content=(
                "# Session Reasoning Log\n\n"
                "Per-agent pre-final-response recaps. Append-only by "
                "session_runner._dispatch_reasoning_summary (PR 11).\n\n"
            )
            + block,
        )


FAILED_REPORT_DIR = Path("/mnt/session/outputs")


# Plan: Design C (2026-05-15). Words that, when used by the agent to admit
# the row-bearing numbers aren't real, mean we MUST reject the post_report.
# Tuned narrow: only fire on the specific lexicon the agent uses to flag
# fabricated rows (live observation 2026-05-15 from sesn_EXAMPLE on
# 4 of these terms). "Representative" alone is too generic; require it to
# co-occur with a row-bearing term.
_FABRICATED_HARD_TERMS = (
    "synthetic",
    "hardcoded",
    "hard-coded",
    "spec-embedded",
    "spec embedded",
    "fabricated",
)
_FABRICATED_SOFT_TERMS = (
    "representative",
    "placeholder",
    "illustrative",
    "made up",
)
_ROW_BEARING_TERMS = (
    "top ",  # "Top 25", "top 15", etc.
    "table",
    "tables",
    "row-level",
    "row level",
    "past-due",
    "past due",
    "section",
)


def _detect_fabricated_rows_in_payload(payload: dict) -> Optional[str]:
    """Walk a post_report payload looking for self-admitted fabricated rows.

    Returns a short explanation string when a violation is found, or None
    when the payload is clean. The Coordinator caller fed this through
    ``_retry_or_give_up`` so it routes back into the same retry+terminal
    machinery the schema-validator uses.

    The check looks at strings the agent writes (headline, value, detail,
    cross_domain_pattern, methodology, etc.) — any user-visible prose. We
    do NOT walk into raw row data or file paths.
    """
    if not isinstance(payload, dict):
        return None

    def _strings(obj):
        if isinstance(obj, str):
            yield obj
        elif isinstance(obj, dict):
            for k, v in obj.items():
                # Skip schema/structural keys; they often contain words like
                # "table" or "section" without any fabrication implication.
                if k in {"file_path", "attachments", "schema", "_meta", "type"}:
                    continue
                yield from _strings(v)
        elif isinstance(obj, list):
            for item in obj:
                yield from _strings(item)

    for s in _strings(payload):
        if not s:
            continue
        lower = s.lower()
        if any(term in lower for term in _FABRICATED_HARD_TERMS):
            return f"Found a fabricated-row marker in the payload: ...{s[:200]!r}..."
        if any(term in lower for term in _FABRICATED_SOFT_TERMS):
            if any(rt in lower for rt in _ROW_BEARING_TERMS):
                return (
                    f"Found a soft-fabrication marker co-occurring with a "
                    f"row-bearing claim: ...{s[:200]!r}..."
                )
    return None


def _post_report_terminal_failure(
    *,
    reply_to: Optional[str],
    session_id: str,
    response_type: str,
    payload,
    error_history: list[str],
    inv_id: Optional[int] = None,
    event_ts: Optional[str] = None,
    channel_id: Optional[str] = None,
) -> str:
    """Final post_report give-up. ONE neutral in-thread line + forensic dump.

    Triggered when the agent has burned through ``POST_REPORT_MAX_RETRIES``
    attempts across any of these routes (via ``_retry_or_give_up``):
    schema_validation_failed, unknown_response_type, payload_not_object,
    renderer_failed, send_notification_failed. No Pydantic traces, no
    @-mention, no severity emoji — the user gets a clean human sentence
    and the operator gets a JSON file for forensics. The session log is
    preserved (the orchestrator never deletes session history) so
    self_heal can still inspect what went wrong.

    Lifecycle (added 2026-05-13): on terminal give-up, the user's
    original Slack message flips ⏰ → ❌ via ``terminalize_lifecycle``.
    Pre-refactor this function was unreachable from the emoji code path —
    the user would see a neutral "I couldn't assemble a report" line
    next to their original message still stuck on ⏰. The lifecycle call
    closes that gap and atomically marks the investigation row as
    ``failed`` so /cost and analytics stop reporting these as completed.
    """
    log.warning(
        "[POST_REPORT_GIVE_UP] session=%s response_type=%s after %d failures",
        session_id,
        response_type,
        len(error_history),
    )

    # Forensic dump — write the failed payload + the chain of error messages
    # to a JSON file the operator can inspect. Best-effort: directory may
    # not exist locally (tests). Failure here must not cascade.
    dump_path: Optional[str] = None
    try:
        FAILED_REPORT_DIR.mkdir(parents=True, exist_ok=True)
        ts = (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace(":", "")
        )
        dump_path = str(FAILED_REPORT_DIR / f"failed_report_{ts}.json")
        with open(dump_path, "w") as fh:
            json.dump(
                {
                    "session_id": session_id,
                    "response_type": response_type,
                    "payload": payload,
                    "error_history": error_history,
                },
                fh,
                indent=2,
                default=str,
            )
    except Exception:
        log.exception("[POST_REPORT_GIVE_UP] forensic dump write failed; continuing")

    # ONE brief in-thread line. No traces, no markup, no @ping.
    # ``channel=channel_id`` is load-bearing in multi-portco — without it
    # send_notification falls back to the default channel for the reply_to
    # thread, which in cross-channel cases can land in the wrong place.
    if reply_to:
        try:
            send_notification(
                severity="info",
                summary=(
                    "I couldn't assemble a final report for this question. "
                    "The session log is preserved."
                ),
                reply_to=reply_to,
                channel=channel_id,
            )
        except Exception:
            log.exception("[POST_REPORT_GIVE_UP] terminal Slack post failed")

    # Centralized lifecycle terminalization: flip ⏰ → ❌ + mark
    # investigations.status='failed' atomically. Idempotent — if some
    # other terminal path raced this one to a verdict, the lifecycle
    # call no-ops and the row stays at the winning state.
    from lifecycle import DeliveryState, terminalize_lifecycle

    terminalize_lifecycle(
        DeliveryState.TERMINAL_FAILURE,
        event_ts=event_ts,
        channel_id=channel_id,
        inv_id=inv_id,
        error_message=f"post_report_give_up:{response_type}",
    )

    # Clear the per-session counter so a subsequent thread message starts fresh.
    _clear_post_report_retries(session_id)

    return json.dumps(
        {
            "error": "post_report_give_up",
            "response_type": response_type,
            "dump_path": dump_path,
            "attempts": len(error_history),
        }
    )


def _session_output_dir() -> str:
    """Return the canonical session output directory.

    Configurable via ``SESSION_OUTPUT_DIR`` so tests can override the real
    ``/mnt/session/outputs`` mount. We canonicalize once here so callers
    compare against a real path (no trailing slashes, symlinks resolved).
    """
    raw = os.environ.get("SESSION_OUTPUT_DIR") or "/mnt/session/outputs"
    return os.path.realpath(raw)


def _is_safe_attachment_path(path: str) -> bool:
    """Return True iff ``path`` is safely under the session output directory.

    Security: ``payload.attachments`` is model-controlled and ultimately
    influenced by user prompts, so an agent could be induced to attach
    arbitrary readable files (``/etc/passwd``, ``.env``, etc.) and exfiltrate
    them via Slack. Whitelist to the session output prefix.

    Rules:
        1. Resolve to an absolute canonical path (``os.path.realpath``).
        2. Reject if any segment in the original path is a symlink
           (symlinks can break out of the prefix even when ``realpath``
           lands inside it, e.g. if the symlink target is later replaced).
        3. Accept iff the canonical path is inside ``_session_output_dir()``
           via ``os.path.commonpath``.
        4. Reject empty/None/non-str paths.
    """
    if not path or not isinstance(path, str):
        return False

    safe_root = _session_output_dir()

    # Walk every segment of the (absolute) path looking for symlinks.
    # If the user passed a relative path we still resolve to absolute first
    # so the symlink walk is meaningful.
    abs_input = os.path.abspath(path)
    segments: list[str] = []
    current = abs_input
    while True:
        parent, _ = os.path.split(current)
        if parent == current:
            segments.append(current)
            break
        segments.append(current)
        current = parent
    for seg in segments:
        try:
            if os.path.islink(seg):
                return False
        except OSError:
            # If we can't stat a parent (permission, missing), be conservative
            # and reject. Inside the legit session output dir these are
            # all owned by us, so stat should never fail in practice.
            return False

    canonical = os.path.realpath(abs_input)
    try:
        common = os.path.commonpath([canonical, safe_root])
    except ValueError:
        # Cross-drive on Windows or otherwise incommensurable — reject.
        return False
    return common == safe_root


def _filter_safe_attachments(paths: list[str]) -> list[str]:
    """Whitelist attachment paths to the session output directory.

    Drops anything that fails ``_is_safe_attachment_path``. Logs each
    rejection so a forensic trail exists if the agent is ever induced
    to leak. Never raises — the post must still go through with whatever
    attachments are safe (or with none).
    """
    safe: list[str] = []
    for p in paths or []:
        if _is_safe_attachment_path(p):
            safe.append(p)
        else:
            log.warning(
                "[ATTACHMENT_PATH_REJECTED] path=%s reason=outside_session_outputs",
                p,
            )
    return safe


def _prefer_xlsx_sibling(path: str) -> str:
    """Return ``path``'s ``.xlsx`` sibling if it exists, else ``path`` unchanged.

    Slack users get an xlsx they can open in Excel; the agent keeps reasoning
    about the Parquet handle for query_artifact. The xlsx siblings are
    written at materialization time by ``xlsx_export.parquet_to_xlsx_sibling``
    inside ``sf_dump_tool.dump_sf_query`` and
    ``artifact_query_tool._virtualize_query_result``. If the xlsx write
    failed (e.g. openpyxl memory pressure on a multi-million-row pull),
    fall back to uploading the Parquet — better some artifact than none.
    """
    if not path or not path.endswith(".parquet"):
        return path
    xlsx_candidate = os.path.splitext(path)[0] + ".xlsx"
    if os.path.exists(xlsx_candidate) and _is_safe_attachment_path(xlsx_candidate):
        return xlsx_candidate
    return path


def _attach_files_async(
    files: list[str], reply_to: Optional[str], channel: Optional[str] = None
) -> None:
    """Upload each file in ``files`` to the Slack thread on a daemon thread.

    Asynchronous so the tool result returns to the agent immediately. Each
    upload runs in its own try/except — one bad path doesn't block the rest.

    Security: every path is re-validated immediately before upload via
    ``_is_safe_attachment_path``. ``_dispatch_post_report`` already filters
    the list upstream, but this second check is a defense-in-depth belt-
    and-suspenders — the cost is one ``realpath`` per attachment.

    Parquet → xlsx swap: ``_prefer_xlsx_sibling`` rewrites any Parquet path
    to its .xlsx sibling before upload so Slack users get a spreadsheet
    they can open. The model never sees this swap — it's a last-mile UX
    transform on the orchestrator side.
    """
    if not files:
        return

    def _upload_all():
        for raw_path in files:
            path = _prefer_xlsx_sibling(raw_path)
            try:
                if not _is_safe_attachment_path(path):
                    log.warning(
                        "[ATTACHMENT_PATH_REJECTED] path=%s reason=outside_session_outputs",
                        path,
                    )
                    continue
                if not os.path.exists(path):
                    log.warning(
                        "[POST_REPORT_ATTACH] file missing: %s — skipping", path
                    )
                    continue
                post_file(file_path=path, reply_to=reply_to, channel=channel)
                if path != raw_path:
                    log.info(
                        "[POST_REPORT_ATTACH] uploaded %s (xlsx sibling of %s)",
                        path,
                        os.path.basename(raw_path),
                    )
                else:
                    log.info("[POST_REPORT_ATTACH] uploaded %s", path)
            except Exception:
                log.exception("[POST_REPORT_ATTACH] upload failed for %s", path)

    threading.Thread(target=_upload_all, daemon=True).start()


def _docx_title_from_validated(validated) -> str:
    """Pick the most descriptive single-line title for the .docx heading.

    Every response schema except QuickAnswer carries a ``headline`` field
    (validated by Pydantic — see ``response_schemas.py``). QuickAnswer uses
    ``metric`` instead. Fall back to a generic title rather than raise.
    """
    if validated is None:
        return "GTM Health Report"
    return (
        getattr(validated, "headline", None)
        or getattr(validated, "metric", None)
        or "GTM Health Report"
    )


def _render_docx_sibling(
    rendered: str, validated, session_id: Optional[str]
) -> Optional[str]:
    """Generate a .docx for this post_report and return its absolute path.

    Mirrors ``xlsx_export.parquet_to_xlsx_sibling``'s contract: returns the
    new path on success or ``None`` on any failure. Failure is silent — the
    Slack post + xlsx attachments still go through. The file name pattern
    matches the Parquet/.xlsx convention in ``sf_dump_tool.dump_sf_query``:
    ``report_<utc_iso>_<short_uuid>.docx`` under ``SESSION_OUTPUT_DIR`` so the
    attachment whitelist (``_is_safe_attachment_path``) accepts it.

    Passes ``validated.tables`` through to the renderer so per-rep /
    per-account answers (which carry their data in TableBlocks, not in the
    Slack mrkdwn text) still produce a complete Word document.
    """
    try:
        from word_doc_renderer import render_docx

        out_dir = _session_output_dir()
        os.makedirs(out_dir, exist_ok=True)
        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        short_id = uuid.uuid4().hex[:4]
        # ``report`` prefix mirrors ``sf_<label>`` in sf_dump_tool — same
        # readability shape, different domain (prose vs. data).
        filename = f"report_{ts}_{short_id}.docx"
        out_path = os.path.join(out_dir, filename)
        title = _docx_title_from_validated(validated)
        # Schemas other than AdHocInvestigationResponse don't carry tables;
        # gate on attribute presence so future schemas can opt in without
        # touching this code.
        tables = getattr(validated, "tables", None) or None
        result = render_docx(
            prose=rendered,
            title=title,
            output_path=out_path,
            tables=tables,
        )
        if not result:
            return None
        # Defense in depth: only return the path if it passes the same
        # attachment whitelist the agent-supplied paths are filtered through.
        if not _is_safe_attachment_path(result):
            log.warning(
                "[WORD_DOC_RENDER_REJECTED] generated docx %s failed safe-path "
                "check; skipping attachment",
                result,
            )
            return None
        return result
    except Exception as e:
        # render_docx already handles its own exceptions; this catches
        # anything in the path-prep / import path before we got there.
        log.warning("[WORD_DOC_RENDER_FAILED] sibling generation failed: %s", e)
        return None


def _build_validation_detail(exc: Exception, payload: dict) -> str:
    """Build a field-level validation detail string for post_report retries.

    Theme D (2026-05-16). Pre-fix, the retry hint was a generic "fix the
    Pydantic errors" string with the raw ValidationError text. The agent
    often burned its 3-retry budget on the same too-long field because
    the message didn't say HOW long it was or BY HOW MUCH to trim.

    This helper:
      - Walks the Pydantic ``loc`` path into ``payload`` to find the
        actual value
      - For length errors, computes ``len(actual) - limit`` and tells
        the agent exactly how many characters to remove
      - For other errors, surfaces the field path + Pydantic message
      - Falls back gracefully when the error isn't a Pydantic
        ValidationError (some callers pass plain Exception)

    Output is bounded to ~1500 chars so the retry detail fits the
    existing 1200-char envelope after the prefix lines.
    """
    from pydantic import ValidationError

    errors: list = []
    try:
        if isinstance(exc, ValidationError):
            errors = list(exc.errors())
    except Exception:
        errors = []
    if not errors:
        return str(exc)[:1500]

    def _walk(obj, loc):
        cur = obj
        for key in loc:
            try:
                if isinstance(cur, dict):
                    cur = cur.get(key)
                elif isinstance(cur, list) and isinstance(key, int):
                    cur = cur[key] if 0 <= key < len(cur) else None
                else:
                    return None
            except Exception:
                return None
        return cur

    lines = ["Your post_report payload failed schema validation:"]
    for err in errors[:8]:  # cap so an explosion of errors still fits 1500
        loc = err.get("loc", ()) if isinstance(err, dict) else ()
        msg = err.get("msg", "") if isinstance(err, dict) else ""
        path = ".".join(str(p) for p in loc) or "(root)"
        actual = _walk(payload, loc)
        if "at most" in msg and isinstance(actual, str):
            try:
                limit = int([t for t in msg.split() if t.isdigit()][-1])
                over = len(actual) - limit
                lines.append(
                    f"  - {path}: {len(actual)} chars (max {limit}). "
                    f"Trim by {over} chars."
                )
                continue
            except Exception:
                pass
        if "missing" in msg.lower() or "required" in msg.lower():
            lines.append(f"  - {path}: required field is missing.")
            continue
        if "extra" in msg.lower():
            lines.append(f"  - {path}: extra field not allowed by schema.")
            continue
        lines.append(f"  - {path}: {msg}")
    lines.append("Resubmit post_report with these fixes — do not change other fields.")
    return "\n".join(lines)[:1500]


def _should_dispatch_chart(payload: dict) -> Optional[str]:
    """Return a Chart Designer goal string if the payload has chartable
    content, else None.

    Theme D (2026-05-16). The Coordinator's default was to skip Chart
    Designer entirely on ``verbosity=normal`` payloads — session 50's
    10-quarter time series + 7-bucket distribution shipped as an xlsx
    with no visualization. This deterministic heuristic detects:

      - Time-series tables (column name contains quarter/month/week/
        date/period AND ≥4 rows)
      - Categorical distributions (≥5 rows AND a numeric value column
        like count/arr/pct)

    Conservative on purpose: only fires when both signals are clearly
    present. Falsely missing a chart is cheap (no Slack noise);
    falsely producing one is annoying. Returns the goal string the
    Coordinator should pass to Chart Designer, or None.
    """
    if not isinstance(payload, dict):
        return None
    tables = payload.get("tables") or []
    if not isinstance(tables, list):
        return None
    TIME_TOKENS = ("quarter", "month", "week", "date", "period")
    VALUE_TOKENS = ("count", "arr", "pct", "percent", "sum", "total", "mean")
    for tbl in tables:
        if not isinstance(tbl, dict):
            continue
        cols = [str(c).lower() for c in (tbl.get("columns") or [])]
        rows = tbl.get("rows") or []
        if not isinstance(rows, list) or not cols:
            continue
        # Time-series: explicit time-dim column AND enough data points
        if any(any(tok in c for tok in TIME_TOKENS) for c in cols) and len(rows) >= 4:
            name = tbl.get("name") or "table"
            return (
                f"Plot the time series in `{name}`. Pick the most "
                "insight-bearing numeric column. Single-line chart, "
                "concise title that states the insight (not the data)."
            )
        # Categorical distribution: ≥5 buckets and a numeric value column
        if len(rows) >= 5 and any(any(tok in c for tok in VALUE_TOKENS) for c in cols):
            name = tbl.get("name") or "table"
            return (
                f"Plot the distribution in `{name}` as a bar chart. "
                "Top 10 buckets if more than 10 rows. Title states the "
                "insight."
            )
    return None


def _dispatch_post_report(
    tool_input: dict,
    thread_ts: Optional[str] = None,
    session_id: Optional[str] = None,
    verbosity: str = "summary",
    portco_key: Optional[str] = None,
    user_id: Optional[str] = None,
    event_ts: Optional[str] = None,
    channel_id: Optional[str] = None,
    inv_id: Optional[int] = None,
) -> str:
    """Validate a structured report payload, render it, and post to Slack.

    verbosity is REQUEST-scoped — passed from _stream_and_handle for this
    specific Slack message. No shared mutable state. Concurrent investigations
    can never stomp each other's verbosity.

    event_ts + channel_id, when set, identify the user's original Slack
    message. On the success path the lifecycle reaction transitions
    ⏰ → ✅ on that message. Cron flows leave these None and skip the
    reaction.

    Failure handling (B7, 2026-05-12 self-heal):
      - unknown response_type / payload-not-dict / renderer crash:
        return ``is_error=true`` with a short explanation. The agent retries.
      - Pydantic ``ValidationError``: same — return the exact error so the
        agent can fix the field on the next attempt. Up to
        ``POST_REPORT_MAX_RETRIES`` attempts per session. On the 4th rejection
        the dispatcher gives up, writes a forensic dump, and posts ONE neutral
        in-thread line. No Pydantic traces visible to the user, no [POST_REPORT_FAILED]
        watch notice, no @-ping.

    On successful post (response_type validated + Slack notification sent),
    fires `surface_pusher.push_to_canvas(portco_key)` asynchronously via a
    daemon thread. Failures in the canvas push are swallowed and logged as
    `[SURFACE_PUSH_FAILED]` per Plan #33 failure-mode table — the daily 08:00
    PT cron catches up. The import is lazy so this module can be tested and
    deployed even while `surface_pusher` is being landed in a parallel PR.

    Return shape — string-encoded JSON. On the success path the JSON is the
    classic ``{ok: True, message_ts, ...}``. On the in-band failure path
    (validation rejection that the agent should retry), the JSON includes a
    ``_is_error: true`` marker; ``_stream_and_handle`` reads that marker and
    sets ``is_error=true`` on the ``user.custom_tool_result`` event.
    """
    response_type = (
        tool_input.get("response_type") if isinstance(tool_input, dict) else None
    )
    payload = tool_input.get("payload") if isinstance(tool_input, dict) else None
    reply_to = (
        tool_input.get("reply_to") if isinstance(tool_input, dict) else None
    ) or thread_ts
    # Plan #11: theme-tagged post_reports (nightly crons) route every artifact
    # for the same (run_id, theme, channel_id) tuple into ONE Slack thread.
    # The registry call below runs AFTER schema validation so we have a real
    # summary line to anchor the parent message.
    theme = tool_input.get("theme") if isinstance(tool_input, dict) else None
    nightly_run_id = (
        tool_input.get("nightly_run_id") if isinstance(tool_input, dict) else None
    )

    # Accept the canonical 3-tier verbosity (Plan #31 E1) and the legacy
    # 2-tier ``summary``/``expanded`` aliases. The renderer's
    # ``_normalize_verbosity`` collapses everything to the canonical tier
    # internally; we just guard against typos before getting there.
    if verbosity not in ("summary", "expanded", "terse", "normal", "verbose"):
        log.warning(
            f"_dispatch_post_report: invalid verbosity {verbosity!r}; defaulting to summary"
        )
        verbosity = "summary"

    # Plan #52 PR-G: cancelled-guard. Between the moment the Coordinator
    # decided to call post_report and the moment we dispatch the tool,
    # another path (watchdog Tier 3, user /stop, recovery sweep) may have
    # already terminalized this investigation row. Posting a stale draft
    # at that point puts the Coordinator's prose in the user's thread on
    # top of the existing :x: notice — two contradictory signals on the
    # same Slack message. Short-circuit BEFORE any side effect.
    #
    # We consult the DB (authoritative, single ~5ms read) rather than the
    # Anthropic session.status (one network call per post_report) or an
    # in-memory cancelled-session set (drift risk). inv_id is already
    # threaded through this function from _stream_and_handle.
    #
    # We do NOT mark the investigation row failed here — the path that
    # cancelled it owns the terminal state. Our job is to avoid emitting
    # stale prose, not to second-guess the lifecycle.
    if inv_id is not None:
        try:
            inv_row = db_adapter.get_investigation_by_id(inv_id)
        except Exception:
            inv_row = None
            log.debug(
                "[POST_REPORT_CANCELLED_GUARD] db read failed for inv_id=%s; "
                "proceeding with dispatch",
                inv_id,
            )
        current_status = (inv_row or {}).get("status")
        if current_status in _POST_REPORT_TERMINAL_GUARD_STATUSES:
            log.warning(
                "[POST_REPORT_CANCELLED] session_id=%s inv_id=%s "
                "status=%s — skipping Slack post, editor, surface push",
                session_id,
                inv_id,
                current_status,
            )
            # Codex P1 (PR #252 follow-up). Mark this session so the
            # caller-side fallback in ``_stream_and_handle``'s consumer
            # (``run_adhoc_mcp_session`` line ~6155+, the recovery resume
            # path, etc.) can suppress its "Investigation didn't produce
            # a final report" Slack post. The watchdog Tier 3 / /stop /
            # recovery path that terminalized the row already owns the
            # user-facing terminal notice; emitting a second contradictory
            # post on the same Slack message defeats the cancelled-guard's
            # purpose. The ``_terminal: True`` key on the return dict
            # carries the same signal through the JSON tool-result channel
            # so the post-dispatch promotion logic in ``_stream_and_handle``
            # can pin it directly, independent of any session_id-keyed
            # registry lookups.
            if session_id:
                _mark_post_report_cancelled_guard_fired(session_id)
            return json.dumps(
                {
                    "ok": False,
                    "skipped": True,
                    "_terminal": True,
                    "reason": "investigation_already_terminalized",
                    "inv_status": current_status,
                }
            )

    # Codex P2 (PR #252 follow-up). The cancelled-guard registry has
    # one-shot semantics — ``_consume_post_report_cancelled_guard``
    # only runs on the caller-side fallback branch (the
    # NOT_DELIVERED path in ``run_adhoc_mcp_session``). If the guard
    # fired on a prior turn but a later turn delivers successfully,
    # the session_id stays in ``_post_report_cancelled_guard_sessions``
    # forever. Thread follow-ups reuse the same session, so that
    # stale marker can later be consumed by an unrelated turn's
    # fallback decision and suppress a legitimate "investigation
    # incomplete / no output" post — hiding a real failure.
    #
    # Drain any stale marker at the start of every dispatch where
    # the guard did NOT fire this turn. This bounds the marker
    # lifetime to the turn it was set in. Cheap (one set lookup
    # under a lock) and idempotent — no-op if the set is empty.
    if session_id:
        _consume_post_report_cancelled_guard(session_id)

    def _retry_or_give_up(reason: str, detail: str) -> str:
        """Feed a structured error back to the agent. After
        POST_REPORT_MAX_RETRIES, give up with a neutral terminal line.

        No Slack post is made on the retry path — the agent self-corrects
        silently. Operational telemetry stays in the Railway logs.
        """
        attempt = _bump_post_report_retry(session_id) if session_id else 1
        log.warning(
            "[POST_REPORT_RETRY] session=%s attempt=%d/%d reason=%s detail=%s",
            session_id,
            attempt,
            POST_REPORT_MAX_RETRIES,
            reason,
            detail[:300],
        )
        if attempt >= POST_REPORT_MAX_RETRIES:
            history = [f"attempt {i + 1}: {detail}" for i in range(attempt)]
            return _post_report_terminal_failure(
                reply_to=reply_to,
                session_id=session_id or "",
                response_type=response_type or "(unknown)",
                payload=payload,
                error_history=history,
                inv_id=inv_id,
                event_ts=event_ts,
                channel_id=channel_id,
            )
        return json.dumps(
            {
                "_is_error": True,
                "error": reason,
                "attempt": attempt,
                "max_attempts": POST_REPORT_MAX_RETRIES,
                "detail": detail[:1200],
                "retry_hint": (
                    "Your post_report payload failed schema validation. "
                    "Read the detail field carefully — it lists the exact "
                    "Pydantic errors with field paths. Fix the listed fields "
                    "and call post_report again. Do not post to Slack via "
                    "send_slack_notification — the orchestrator handles "
                    "delivery."
                ),
            }
        )

    if response_type not in response_schemas.RESPONSE_TYPES:
        return _retry_or_give_up(
            "unknown_response_type",
            f"response_type={response_type!r}; must be one of: "
            f"{sorted(response_schemas.RESPONSE_TYPES)}",
        )

    if not isinstance(payload, dict):
        return _retry_or_give_up(
            "payload_not_object",
            f"payload must be a JSON object (dict); got {type(payload).__name__}",
        )

    # Plan: Design C (2026-05-15) — synthetic-data guard. Reject any payload
    # whose findings text admits the row-bearing numbers are fabricated.
    # Live failure: sesn_EXAMPLE (2026-05-15) — a Statistician
    # sub-agent couldn't find its input parquet and fell back to "spec-embedded
    # statistics"; its recap said "All computed tables (Sections 4-8, 10) use
    # synthetic/hardcoded data rather than actual parquet-computed values. The
    # coordinator/recipient should be aware that the row-level tables (Top 25,
    # past-due top 15, etc.) are representative". The Coordinator was about to
    # ship that payload. This guard rejects it and forces a halt-and-regenerate.
    fab_violation = _detect_fabricated_rows_in_payload(payload)
    if fab_violation is not None:
        return _retry_or_give_up(
            "synthetic_data_detected",
            (
                f"{fab_violation} "
                "STOP — do NOT ship this payload. The numbers are fabricated, "
                "not computed from real data. Halt the post_report attempt, "
                "dispatch dump_sf_query (or query_artifact against an "
                "existing parquet handle) to regenerate the missing input, "
                "wait for the new artifact, then rebuild the findings from "
                "the actual rows. If the input artifact is genuinely "
                "irrecoverable, fail honestly: post a one-line note that "
                "the data is unavailable; do NOT substitute spec statistics."
            ),
        )

    # Pre-validation editor pass: audit and reduce the payload to fit
    # audience-appropriate targets BEFORE Pydantic ever sees it. The editor
    # tightens fields below their schema caps (headline 100 vs cap 80-140,
    # value 120 vs cap 200, cross_domain_pattern 180 vs cap 200) and drops
    # optional fields that cannot be trimmed cleanly. Deterministic, no LLM
    # call, never mutates the input. Every edit is logged at INFO so the
    # operator can see what the editor did.
    try:
        payload, edit_log = response_editor.edit_payload(response_type, payload)
        for entry in edit_log:
            log.info(f"[EDITOR] {entry}")
    except Exception as e:
        # Editor failures are non-fatal — pass the original payload through
        # to validation. Logged loud so self_heal can spot a regression.
        log.exception(f"[EDITOR] pre-validation edit raised; passing through: {e}")

    # Validate against the schema. Pydantic raises ValidationError on shape
    # mismatch, length-cap overrun, or extra fields (extra="forbid").
    try:
        validated = response_schemas.parse_payload(response_type, payload)
    except Exception as e:
        # Theme D (2026-05-16): build a field-level retry hint that tells
        # the agent the actual length and how much to trim — the previous
        # generic message led to multi-attempt retries on the same field.
        err_msg = _build_validation_detail(e, payload)
        return _retry_or_give_up("schema_validation_failed", err_msg)

    # Theme D (2026-05-16). Audit-trail chart-suggestion. The Coordinator's
    # updated prompt instructs it to dispatch Chart Designer when the
    # heuristic conditions apply; this log line lets us monitor compliance
    # without forcing a behavior change in the orchestrator. If the next
    # week shows the Coordinator ignoring suggestions, we'll promote this
    # to an actual dispatch in a follow-up.
    try:
        chart_goal = _should_dispatch_chart(payload)
        if chart_goal:
            log.info(
                "[CHART_RECOMMENDED] response_type=%s goal=%s session=%s",
                response_type,
                chart_goal[:200],
                session_id,
            )
    except Exception:
        log.debug("chart heuristic check failed", exc_info=True)

    try:
        rendered, extra_blocks = response_renderer.render_payload(
            cast(Any, validated), mode=verbosity
        )
    except Exception as e:
        return _retry_or_give_up("renderer_failed", str(e)[:600])

    # Suppress the expand: footer hint when reply_to is None (cron context —
    # there's no thread for the user to reply in). Both ``summary`` (legacy)
    # and ``normal`` (canonical, Plan #31 E1) render with the footer.
    if reply_to is None and verbosity in ("summary", "normal"):
        rendered = rendered.replace(
            "\n\n_Reply `expand:` in this thread for the full analysis._", ""
        )

    # Plan #11: theme-anchored thread routing for nightly cron output. The
    # registry posts a parent message with the agent's summary line on the
    # first call per (nightly_run_id, theme, channel_id) and returns the
    # thread_ts for every reply thereafter. We anchor BEFORE the main
    # post_report send so the full report lands as a reply, not as a
    # second top-level message.
    if theme and channel_id:
        try:
            from slack_thread_registry import (
                get_or_create_thread,
                VALID_THEMES,
            )

            if theme in VALID_THEMES:
                # Extract the parent-summary line from the validated payload.
                # Every response_type except quick_answer carries a ``headline``;
                # quick_answer uses ``metric``. Fall back to the rendered text
                # (first line) if neither attribute exists — defensive against
                # a future schema that diverges.
                parent_summary = (
                    getattr(validated, "headline", None)
                    or getattr(validated, "metric", None)
                    or rendered.split("\n", 1)[0]
                )
                anchored = get_or_create_thread(
                    run_id=nightly_run_id,
                    theme=theme,
                    channel_id=channel_id,
                    parent_summary=parent_summary,
                    # Forward the in-module send_notification reference so
                    # tests that patch ``session_runner.send_notification``
                    # see the registry's parent post too. The registry's
                    # default fallback imports from slack_bot, which is the
                    # same function — passing it explicitly just keeps the
                    # patch surface uniform.
                    poster=cast(Callable[..., str], send_notification),
                )
                if anchored:
                    reply_to = anchored
            else:
                log.warning(
                    "[NIGHTLY_THREAD_UNKNOWN_THEME] theme=%r not in "
                    "VALID_THEMES; posting unthreaded",
                    theme,
                )
        except Exception:
            # The registry NEVER blocks delivery. If it raises, fall through
            # to the legacy reply_to behavior — the user still gets the
            # report, just unthreaded.
            log.exception(
                "[NIGHTLY_THREAD_ANCHOR_FAILED] theme=%s — falling through "
                "to unthreaded post",
                theme,
            )

    severity = _infer_severity(validated)

    try:
        ts = send_notification(
            severity=severity,
            summary=rendered,
            reply_to=reply_to,
            extra_blocks=extra_blocks or None,
            requester_id=user_id,
        )
    except Exception as e:
        log.error(f"send_notification failed for post_report: {e}")
        return _retry_or_give_up("send_notification_failed", str(e)[:300])

    log.info(
        f"post_report sent: type={response_type} mode={verbosity} "
        f"len={len(rendered)} ts={ts}"
    )

    # B4: attach virtualized files. Two sources:
    #   1. Files explicitly listed in payload.attachments (the agent's
    #      first-class way to ship data to the user — documented in the
    #      Coordinator's virtualization_contract prompt block).
    #   2. Files tracked on the session as ``[RESULT_VIRTUALIZED]`` outputs
    #      that the agent forgot to attach. Best-effort safety net so the
    #      user always gets the data when a tool result was streamed to disk.
    payload_attachments = (
        getattr(validated, "attachments", None) or [] if validated is not None else []
    )
    fallback_attachments = _consume_virtualized_files(session_id)
    # Union with order preserved. Explicit > fallback. De-dupe so we don't
    # upload the same .xlsx twice in the rare double-list case.
    all_attachments: list[str] = []
    seen_paths: set[str] = set()
    for p in list(payload_attachments) + list(fallback_attachments):
        if not p or p in seen_paths:
            continue
        seen_paths.add(p)
        all_attachments.append(p)
    # Security: whitelist attachment paths to the session output directory.
    # ``payload.attachments`` is model-controlled — a prompt-injected agent
    # could otherwise upload ``/etc/passwd`` or ``.env``. Filter once here
    # so the rejection log fires at the dispatch boundary; the async
    # uploader re-validates as defense in depth.
    all_attachments = _filter_safe_attachments(all_attachments)

    # Plan #48 Phase 3 / Plan #52 PR-E + PR-H: consolidate xlsx
    # attachments into one workbook before upload. Default-on as of
    # PR-H (2026-05-22, after the 48h observation window of the
    # env-var-enabled mode in prod cleared with zero
    # [XLSX_CONSOLIDATE_FAILED] log lines). The kill switch
    # XLSX_CONSOLIDATE_ENABLED=false is preserved for emergency
    # rollback without a code change.
    # _consume_split_files_pref pops the session preference (default
    # False = consolidate). When _split=True the user explicitly
    # requested separate files via _SPLIT_FILES_KEYWORDS. Re-register
    # after consuming so post_report retries (POST_REPORT_MAX_RETRIES=3)
    # honour the same preference instead of silently consolidating on
    # attempt 2.
    if os.environ.get("XLSX_CONSOLIDATE_ENABLED", "true").lower() == "true":
        _split = _consume_split_files_pref(session_id)
        # Re-register so schema-validation retries see the same preference.
        _register_split_files_pref(session_id, _split)
        if _split:
            log.info(
                "[XLSX_CONSOLIDATE_SKIPPED_USER_OVERRIDE] session=%s portco=%s",
                session_id,
                portco_key or "unknown",
            )
        else:
            # Codex P2 fix (PR-E review): Parquet handles from dump_sf_query /
            # query_artifact are swapped to their .xlsx siblings just-in-time
            # by `_attach_files_async` → `_prefer_xlsx_sibling`. We apply the
            # same swap UP FRONT so consolidation actually sees those xlsx
            # files. Without this, the common virtualized-query path skips
            # consolidation entirely (only literal `.xlsx` paths get merged)
            # and N siblings still upload separately, defeating PR-E for the
            # most common multi-file case. Mutating all_attachments here means
            # downstream upload sees the swapped paths too — the original
            # second swap inside _attach_files_async is now a no-op for these
            # entries but stays as defense-in-depth for any other call site.
            all_attachments = [_prefer_xlsx_sibling(p) for p in all_attachments]
            xlsx_paths = [p for p in all_attachments if p.lower().endswith(".xlsx")]
            non_xlsx = [p for p in all_attachments if not p.lower().endswith(".xlsx")]
            if len(xlsx_paths) >= 2:
                try:
                    from xlsx_consolidate import consolidate_xlsx_files

                    merged, did_consolidate = consolidate_xlsx_files(
                        paths=xlsx_paths,
                        output_dir=_session_output_dir(),
                        portco_key=portco_key or "unknown",
                        size_cap_mb=float(
                            os.environ.get("XLSX_CONSOLIDATE_SIZE_CAP_MB", "50")
                        ),
                    )
                    if did_consolidate:
                        all_attachments = merged + non_xlsx
                        log.info(
                            "[XLSX_CONSOLIDATED] session=%s portco=%s "
                            "files_merged=%d output=%s",
                            session_id,
                            portco_key or "unknown",
                            len(xlsx_paths),
                            merged[0] if merged else "(none)",
                        )
                    else:
                        # Size-cap fallback — DM admins if configured.
                        try:
                            from slack_bot import send_dm

                            admin_ids = os.environ.get("SLACK_ADMIN_USER_IDS", "")
                            msg = (
                                f"[XLSX_CONSOLIDATE_SKIPPED_SIZE] "
                                f"session={session_id} "
                                f"portco={portco_key or 'unknown'} "
                                f"files="
                                f"{[os.path.basename(p) for p in xlsx_paths]}"
                            )
                            for uid in admin_ids.split(","):
                                uid = uid.strip()
                                if uid:
                                    send_dm(uid, msg)
                        except Exception:
                            # DM failure never blocks delivery.
                            log.debug(
                                "Admin DM on size-cap fallback failed",
                                exc_info=True,
                            )
                except Exception:
                    log.exception(
                        "[XLSX_CONSOLIDATE_FAILED] session=%s portco=%s "
                        "— falling through to original N-file upload",
                        session_id,
                        portco_key or "unknown",
                    )
                    # all_attachments unchanged; original N files upload.

    # Floating-prancing-trinket PR 6: auto-generate a Word doc sibling for
    # every post_report. Users routinely ask for "Word + Excel" deliverables;
    # the xlsx side is already auto-generated as a Parquet sibling
    # (xlsx_export.parquet_to_xlsx_sibling). The .docx is rendered from the
    # already-composed prose (the Slack mrkdwn the Writing Agent produced and
    # the renderer assembled). The Writing Agent never raises and neither
    # does render_docx — on any failure the user still gets the Slack post +
    # xlsx, just without the Word file.
    docx_path = _render_docx_sibling(rendered, validated, session_id)
    if docx_path:
        all_attachments.append(docx_path)

    _attach_files_async(all_attachments, reply_to=reply_to)

    # Successful post — clear the retry counter for the next message in this
    # thread.
    _clear_post_report_retries(session_id)

    # F8 (Plan #33): fire the canvas push asynchronously after every
    # successful post_report. Lazy import so this module imports cleanly
    # even when surface_pusher hasn't landed yet. Daemon thread so the
    # tool result is returned to the agent immediately. Failures are
    # swallowed per Plan #33 failure-mode table — the daily 08:00 PT cron
    # catches up.
    if portco_key:
        try:
            from surface_pusher import push_to_canvas

            threading.Thread(
                target=push_to_canvas, args=(portco_key,), daemon=True
            ).start()
        except Exception:
            log.exception(
                "[SURFACE_PUSH_FAILED] surface push from post_report failed; continuing"
            )

    # Centralized lifecycle terminalization: flip ⏰ → ✅ AND mark
    # investigations.status='completed' atomically. The two-layer
    # idempotency (in-memory + DB UPDATE WHERE status NOT IN
    # terminal_states) means concurrent terminal paths cannot create
    # contradictory state (e.g. status='completed' + emoji=❌). The
    # bare transition_reaction call this replaced only flipped the
    # emoji — DB status was set separately at session_runner.py:3530
    # in the outer flow, opening a window where the two could disagree.
    from lifecycle import DeliveryState, terminalize_lifecycle

    terminalize_lifecycle(
        DeliveryState.DELIVERED_VIA_POST_REPORT,
        event_ts=event_ts,
        channel_id=channel_id,
        inv_id=inv_id,
    )

    return json.dumps(
        {
            "ok": True,
            "message_ts": ts,
            "response_type": response_type,
            "mode": verbosity,
            "rendered_chars": len(rendered),
            "attachments_count": len(all_attachments),
        }
    )


# _esc_slack lives in response_renderer (alias: response_renderer.escape_slack)
# so the renderer and the dispatch fallback path use the same escape logic.


def _infer_severity(validated) -> str:
    """Map a validated response schema to the Slack notification severity."""
    rt = getattr(validated, "response_type", None)
    if rt == "anomaly_alert":
        return validated.severity  # critical or watch
    if rt in ("ad_hoc_investigation_result", "nightly_digest"):
        findings = (
            getattr(validated, "findings", None)
            or getattr(validated, "changes_overnight", None)
            or []
        )
        severities = {f.severity for f in findings}
        if "critical" in severities:
            return "critical"
        if "watch" in severities:
            return "watch"
    if rt == "weekly_status":
        severities = {p.severity for p in validated.portco_lines}
        if "critical" in severities:
            return "critical"
        if "watch" in severities:
            return "watch"
    return "info"


CHART_COLORS = [
    "rgba(54, 162, 235, 0.8)",
    "rgba(255, 99, 132, 0.8)",
    "rgba(75, 192, 192, 0.8)",
    "rgba(255, 206, 86, 0.8)",
    "rgba(153, 102, 255, 0.8)",
    "rgba(255, 159, 64, 0.8)",
    "rgba(46, 204, 113, 0.8)",
    "rgba(231, 76, 60, 0.8)",
]


def _render_chart_bytes(tool_input: dict) -> bytes:
    """Render a chart as PNG bytes via QuickChart."""
    from quickchart import QuickChart

    chart_type = tool_input["chart_type"]
    is_stacked = chart_type == "stacked_bar" or tool_input.get("options", {}).get(
        "scales", {}
    ).get("x", {}).get("stacked")
    if chart_type == "stacked_bar":
        chart_type = "bar"

    datasets = []
    for i, ds in enumerate(tool_input["data"]["datasets"]):
        data_values = ds.get("values") or ds.get("data") or []
        dataset = {
            "label": ds["label"],
            "data": data_values,
            "backgroundColor": ds.get(
                "backgroundColor", CHART_COLORS[i % len(CHART_COLORS)]
            ),
        }
        if is_stacked:
            dataset["stack"] = "stack0"
        datasets.append(dataset)

    options = {
        "plugins": {
            "title": {
                "display": True,
                "text": tool_input["title"],
                "font": {"size": 16},
            },
        },
        **(tool_input.get("options") or {}),
    }

    if is_stacked:
        options.setdefault("scales", {})
        options["scales"].setdefault("x", {})["stacked"] = True
        options["scales"].setdefault("y", {})["stacked"] = True

    config = {
        "type": chart_type,
        "data": {
            "labels": tool_input["data"]["labels"],
            "datasets": datasets,
        },
        "options": options,
    }

    qc = QuickChart()
    qc.width = 700
    qc.height = 400
    qc.device_pixel_ratio = 2.0
    qc.config = config
    return qc.get_bytes()


MODEL_COSTS_PER_MTOK = {
    # Per Anthropic pricing:
    #   input: standard input tokens
    #   output: output tokens
    #   cache_write_5m: input × 1.25 (5-minute ephemeral cache write)
    #   cache_write_1h: input × 2 (1-hour ephemeral cache write)
    #   cache_read: input × 0.10 (cached input read)
    # Opus 4.5–4.8 share $5/$25 list pricing (verified 2026-05-29 vs
    # platform.claude.com). The opus-4-7 row below was stale Opus-4/4.1 pricing
    # ($15/$75) and over-estimated every opus-4-7 session ~3×; corrected so
    # historical opus-4-7 cost rows reconcile against the real charge.
    "claude-opus-4-8": {
        "input": 5.0,
        "output": 25.0,
        "cache_write_5m": 6.25,
        "cache_write_1h": 10.0,
        "cache_read": 0.5,
    },
    "claude-opus-4-7": {
        "input": 5.0,
        "output": 25.0,
        "cache_write_5m": 6.25,
        "cache_write_1h": 10.0,
        "cache_read": 0.5,
    },
    "claude-sonnet-4-6": {
        "input": 3.0,
        "output": 15.0,
        "cache_write_5m": 3.75,
        "cache_write_1h": 6.0,
        "cache_read": 0.3,
    },
    "claude-haiku-4-5": {
        "input": 0.80,
        "output": 4.0,
        "cache_write_5m": 1.0,
        "cache_write_1h": 1.6,
        "cache_read": 0.08,
    },
}


def _extract_usage_parts(usage) -> dict:
    """Pull all relevant fields out of a Managed Agents session usage object.

    The session usage shape is:
        input_tokens:               new uncached input tokens this turn (or aggregate)
        output_tokens:              output tokens
        cache_read_input_tokens:    tokens served from cache
        cache_creation:
            ephemeral_5m_input_tokens:  tokens written to 5-minute cache
            ephemeral_1h_input_tokens:  tokens written to 1-hour cache

    `input_tokens` does NOT include cached tokens. The three input categories
    (input, cache_read, cache_creation) are disjoint.
    """
    if usage is None:
        return {
            "input": 0,
            "output": 0,
            "cache_read": 0,
            "cache_write_5m": 0,
            "cache_write_1h": 0,
        }
    input_tok = getattr(usage, "input_tokens", 0) or 0
    output_tok = getattr(usage, "output_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cw5, cw1 = 0, 0
    cc = getattr(usage, "cache_creation", None)
    if cc is not None:
        cw5 = getattr(cc, "ephemeral_5m_input_tokens", 0) or 0
        cw1 = getattr(cc, "ephemeral_1h_input_tokens", 0) or 0
    return {
        "input": input_tok,
        "output": output_tok,
        "cache_read": cache_read,
        "cache_write_5m": cw5,
        "cache_write_1h": cw1,
    }


def _resolve_model_rates(model_hint: str = None) -> dict:
    """Look up cost rates for a model, tolerating dated suffixes.

    Anthropic sometimes returns dated model IDs in usage responses (e.g.
    'claude-haiku-4-5-20251001'). Our MODEL_COSTS_PER_MTOK keys are short
    canonical forms ('claude-haiku-4-5'). Match by longest prefix so the
    cost calculator stays accurate when Anthropic resolves the alias to a
    dated form on response — without needing to keep the table in lock-step
    with every dated release.

    Falls back to Sonnet rates as a conservative default if no prefix matches.
    """
    if not model_hint:
        return MODEL_COSTS_PER_MTOK["claude-sonnet-4-6"]
    if model_hint in MODEL_COSTS_PER_MTOK:
        return MODEL_COSTS_PER_MTOK[model_hint]
    candidates = sorted(
        (k for k in MODEL_COSTS_PER_MTOK if model_hint.startswith(k)),
        key=len,
        reverse=True,
    )
    if candidates:
        return MODEL_COSTS_PER_MTOK[candidates[0]]
    log.warning(
        f"_resolve_model_rates: no rate for model_hint={model_hint!r}; "
        f"using sonnet-4-6 fallback (cost estimate may be off)."
    )
    return MODEL_COSTS_PER_MTOK["claude-sonnet-4-6"]


def _estimate_cost(usage, model_hint: str = None) -> float:
    """Estimate cost in dollars from a session usage object.

    Bug fixed 2026-05-11: the prior implementation did `input - cache_read`
    which produced negative values (input is already exclusive of cache_read
    in the Anthropic API) and silently dropped the cache_write cost — leading
    to underestimates of 30-50% on long sessions.

    Robustness 2026-05-11 (Task #27 verification): rate lookup now tolerates
    dated model IDs via longest-prefix match. Without this, Anthropic returning
    'claude-haiku-4-5-20251001' would fall through to Sonnet rates silently.
    """
    rates = _resolve_model_rates(model_hint)
    u = _extract_usage_parts(usage)
    return (
        u["input"] * rates["input"] / 1_000_000
        + u["output"] * rates["output"] / 1_000_000
        + u["cache_read"] * rates["cache_read"] / 1_000_000
        + u["cache_write_5m"] * rates["cache_write_5m"] / 1_000_000
        + u["cache_write_1h"] * rates["cache_write_1h"] / 1_000_000
    )


def _cache_hit_pct(usage) -> float:
    """Cache hit percentage: cache_read / (input + cache_read + cache_writes).

    Returns 0.0 when there were no input tokens of any kind.
    """
    u = _extract_usage_parts(usage)
    total_input = (
        u["input"] + u["cache_read"] + u["cache_write_5m"] + u["cache_write_1h"]
    )
    if total_input == 0:
        return 0.0
    return round(100 * u["cache_read"] / total_input, 1)


def _suggested_next_action(error_message: str) -> str:
    """Map a terminal session.error message to a one-line user-facing remedy.

    Tailors the suggestion to the underlying failure mode:
      - "prompt is too long" → context budget exceeded, narrow scope
      - rate limit / 429 → retry later
      - overloaded → retry later
      - everything else → check logs
    """
    msg = (error_message or "").lower()
    if "prompt is too long" in msg or "context" in msg and "too long" in msg:
        return (
            "The query returned more data than fits in one session. Retry "
            "with a narrower date range, an explicit `LIMIT`, or ask for an "
            "aggregate breakdown instead of a full list."
        )
    if "rate_limit" in msg or "rate limit" in msg or "429" in msg:
        return "Rate-limited by the model provider. Wait a few minutes and retry."
    if "overloaded" in msg or "503" in msg or "service_unavailable" in msg:
        return "The model is temporarily overloaded. Retry in a few minutes."
    return "Check the orchestrator logs for the full error trace."


def _post_session_error_to_slack(
    thread_ts: str,
    error_type: str,
    error_message: str,
    session_id: str,
) -> str:
    """Post a terse, actionable recovery message in the user's Slack thread.

    Called from _stream_and_handle when a session.error fires with a terminal
    or exhausted retry status. Without this, the user sees only the original
    ack post and never learns the run died (see session
    sesn_EXAMPLE — 7-hour-old ack, no follow-up).

    Returns the message timestamp on success, "" on failure. Never raises —
    a failed recovery post must not cascade into a second exception in the
    event-loop.
    """
    if not thread_ts:
        return ""

    # Trim the raw error to something readable inside Slack while keeping the
    # critical numbers (e.g. "1,119,846 > 1,000,000").
    trimmed = (error_message or "N/A").strip()
    if len(trimmed) > 300:
        trimmed = trimmed[:300] + "..."

    next_action = _suggested_next_action(error_message or "")

    msg = (
        f"*Investigation halted* (session `{session_id}`, error "
        f"`{error_type}`: {trimmed}). {next_action}"
    )

    try:
        return send_notification(
            severity="watch",
            summary=msg,
            reply_to=thread_ts,
        )
    except Exception as e:
        log.error(f"Failed to post session-error recovery message to Slack: {e}")
        return ""


# ───────────────── SSE auto-reconnect ─────────────────
#
# Background: 2026-05-15 incident — two production investigations (inv_id=39
# sesn_EXAMPLE, inv_id=40 sesn_EXAMPLE) both
# died on httpx.RemoteProtocolError raised by `for event in stream:` in
# _stream_and_handle. The Anthropic-side SSE connection was idle for 76s and
# 474s respectively before the drop — the orchestrator was simply waiting for
# the agent's next event while the agent was thinking. An edge/LB or NLB along
# the path silently dropped the long-idle connection, the httpx iterator
# raised, and the lifecycle guard at lifecycle.py:417 flipped the user's
# message to ❌ with the Anthropic-side session still alive and orphaned.
#
# The fix is auto-reconnect with gap backfill:
#   1. On a transient transport exception, sleep with jittered backoff.
#   2. Drain any events emitted during the disconnect window via
#      events.list(created_at_gt=last_seen_created_at, order="asc"). This is
#      the only API surface that lets us replay missed events; events.stream()
#      itself accepts no resume cursor.
#   3. Reopen events.stream() from scratch.
#   4. Continue iterating. The caller dedupes by event.id so re-receiving
#      the same event during overlap is harmless.
#
# Also drops the SDK's default read timeout (10 minutes) to 120 seconds so a
# silent server-side stall surfaces in 2 min rather than 10. Heartbeat is not
# viable — the Managed Agents API has no `user.ping` event type and any other
# `user.message` would corrupt the agent's state machine. Verified against the
# anthropic SDK 0.100.0 source (resources/beta/sessions/events.py:194-243 —
# stream() has no after_id/before_id/page parameter).

# Exceptions caught at the SSE reconnect boundary. httpx.TransportError is the
# umbrella for Read/Write/Connect/Pool/Close/Local/Remote protocol errors and
# all timeout subclasses. anthropic.APIConnectionError / APITimeoutError wrap
# the SDK-level retry surface. InternalServerError + RateLimitError catch the
# 5xx + 429 cases that show up as SSE error frames. json.JSONDecodeError and
# OSError are belt-and-suspenders for malformed frames + close-path leaks.
# RecursionError is NOT in this list — it's handled separately (log + raise to
# the lifecycle guard, same as before).
SSE_TRANSIENT_EXCEPTIONS = (
    httpx.TransportError,
    anthropic.APIConnectionError,
    anthropic.APITimeoutError,
    anthropic.InternalServerError,
    anthropic.RateLimitError,
    json.JSONDecodeError,
    OSError,
)

# Read timeout on the SSE stream connection. The SDK default is 600s; that
# means a silent server-side stall stays invisible for ten minutes. 120s is
# aggressive enough to surface stalls quickly while still tolerating slow
# model turns. Write/pool/connect kept at SDK defaults.
SSE_READ_TIMEOUT_S = 120.0

# Reconnect budget. Anthropic incidents shorter than ~30s slide under this;
# longer outages exhaust attempts and fall through to the lifecycle guard
# as TERMINAL_FAILURE (same end state as today, but only after we tried).
#
# Sized so the watchdog wins the race on a stalled Coordinator session:
#   - Watchdog Tier 1 (gentle nudge) fires at STALL_THRESHOLD (600s) +
#     WATCHDOG_POLL_SECONDS (60s)  ≈ 11 min
#   - Watchdog Tier 2 (interrupt sub-thread) fires ~13 min
#   - Watchdog Tier 3 (terminate) fires ~15 min
#   - SSE budget at 7 attempts × 120s read + 90s backoff ≈ 15.5 min
# Before 2026-05-19 the budget was 5 attempts ≈ 11 min, which raced the
# watchdog and usually beat it — see sub3 incident inv 58 (ReadTimeout at
# 04:48:41 UTC, never got a watchdog nudge). Extending to 7 gives the
# watchdog ~4 min of headroom to nudge before SSE gives up.
SSE_MAX_RECONNECT_ATTEMPTS = 7
SSE_BASE_BACKOFF_S = 2.0
SSE_MAX_BACKOFF_S = 30.0


def _iter_session_events_with_reconnect(
    session_id: str,
    initial_send_events: Optional[list] = None,
    *,
    max_attempts: int = SSE_MAX_RECONNECT_ATTEMPTS,
    read_timeout_s: float = SSE_READ_TIMEOUT_S,
    followup_mode: bool = False,
    _sleep=time.sleep,
):
    """Yield Managed Agents session events, auto-reconnecting on transient drops.

    On any exception in ``SSE_TRANSIENT_EXCEPTIONS`` raised by the SDK call
    chain, sleeps with jittered exponential backoff, then:

    1. Pages ``events.list(session_id, created_at_gt=last_seen_created_at,
       order='asc')`` to drain anything Anthropic emitted while we were
       disconnected. Yields each.
    2. Reopens ``events.stream(session_id, timeout=...)``. Re-sends
       ``initial_send_events`` ONLY on the very first stream open (subsequent
       reopens leave the session state untouched).

    Caller deduplicates by ``event.id``. The generator does not dedup itself —
    it deliberately re-yields any event whose ``created_at`` is on the cusp
    of the disconnect window so the caller never silently misses one.

    Stops when the live stream closes cleanly (session terminated server-side)
    or when ``max_attempts`` reconnect attempts have been exhausted. On
    exhaustion, the most recent transient exception is re-raised so the
    lifecycle guard at lifecycle.py:417 can flip ❌.
    """
    attempt = 0
    initial_sent = False
    # Track the most recent processed event's created_at for the next backfill.
    # Stored as the SDK's datetime when available; the events.list query
    # accepts datetime or str. Update after every successful yield.
    # Most recent processed event's created_at. Stored as the SDK's datetime
    # (or str) when available; events.list accepts either. Updated after
    # every yield.
    last_seen_ts = None

    while True:
        try:
            # 1. Backfill from events.list — anything emitted while we were
            #    disconnected. Skipped on the very first iteration when
            #    last_seen_ts is still None (the live stream will deliver
            #    everything from session start).
            if last_seen_ts is not None:
                log.info(
                    "[SSE_BACKFILL] session=%s draining events after %s "
                    "(reconnect attempt %d)",
                    session_id,
                    last_seen_ts,
                    attempt,
                )
                page_cursor: Optional[str] = None
                backfill_count = 0
                while True:
                    list_kwargs = {
                        "session_id": session_id,
                        "limit": 100,
                        "order": "asc",
                        "created_at_gt": last_seen_ts,
                    }
                    if page_cursor:
                        list_kwargs["page"] = page_cursor
                    resp = client.beta.sessions.events.list(**list_kwargs)
                    page_events = list(getattr(resp, "data", []) or [])
                    for ev in page_events:
                        yield ev
                        backfill_count += 1
                        ca = getattr(ev, "created_at", None)
                        if ca is not None:
                            last_seen_ts = ca
                    page_cursor = getattr(resp, "next_page", None)
                    if not page_cursor:
                        break
                log.info(
                    "[SSE_BACKFILL_DRAINED] session=%s replayed %d events",
                    session_id,
                    backfill_count,
                )

            # 2. Live stream. read=120s so a silent server-side stall surfaces
            #    in 2 min instead of the SDK's 10-min default.
            stream_timeout = httpx.Timeout(
                connect=5.0,
                read=read_timeout_s,
                write=600.0,
                pool=600.0,
            )
            # Plan #47 Workstream A.2 (2026-05-19): the OUTER try/except
            # below catches BadRequestError raised by ``events.stream()``
            # context-manager entry — Workstream A only wrapped the inner
            # ``events.send()``. Live repro inv 59 (sesn_EXAMPLE,
            # 2026-05-19 04:57:32 UTC): a thread follow-up hit 400 on stream
            # init when the session still had pending requires_action events
            # from the prior stalled turn. With only Workstream A in place
            # the exception bypassed _FollowupBlocked and terminalized as ❌
            # with no Slack reply. The inner try/except around events.send
            # is preserved as defense-in-depth.
            try:
                with client.beta.sessions.events.stream(
                    session_id=session_id,
                    timeout=stream_timeout,
                ) as stream:
                    # Send the kickoff events ONCE on the first stream open. On
                    # subsequent reopens after a drop, the session already has
                    # the kickoff in its event log — re-sending would either be
                    # rejected as a duplicate user.message or, worse, accepted
                    # and confuse the agent's state machine.
                    if not initial_sent and initial_send_events:
                        try:
                            client.beta.sessions.events.send(
                                session_id=session_id,
                                events=initial_send_events,
                            )
                        except anthropic.BadRequestError as e:
                            if followup_mode and _is_requires_action_400(e):
                                raise _FollowupBlocked(e) from e
                            raise
                        initial_sent = True

                    for ev in stream:
                        yield ev
                        ca = getattr(ev, "created_at", None)
                        if ca is not None:
                            last_seen_ts = ca
            except anthropic.BadRequestError as e:
                if followup_mode and _is_requires_action_400(e):
                    raise _FollowupBlocked(e) from e
                raise

            # Live stream closed cleanly. Done.
            return

        except SSE_TRANSIENT_EXCEPTIONS as e:
            attempt += 1
            if attempt > max_attempts:
                log.error(
                    "[SSE_RECONNECT_EXHAUSTED] session=%s after %d attempts: "
                    "%s: %s — falling through to lifecycle guard",
                    session_id,
                    attempt - 1,
                    type(e).__name__,
                    e,
                )
                raise
            sleep_s = min(
                SSE_BASE_BACKOFF_S * (2 ** (attempt - 1)), SSE_MAX_BACKOFF_S
            ) + random.uniform(0.0, 1.0)
            log.warning(
                "[SSE_DISCONNECT] session=%s exception=%s last_event_ts=%s "
                "— reconnecting in %.1fs (attempt %d/%d): %s",
                session_id,
                type(e).__name__,
                last_seen_ts,
                sleep_s,
                attempt,
                max_attempts,
                e,
            )
            _sleep(sleep_s)
            # Loop body retries: backfill via events.list + reopen stream().


@contextlib.contextmanager
def _streaming_events_with_reconnect(
    session_id: str,
    initial_send_events: Optional[list] = None,
    **kwargs,
):
    """Context-manager wrapper around ``_iter_session_events_with_reconnect``.

    Lets callers keep the ``with stream: for event in stream:`` shape they
    used with the bare ``client.beta.sessions.events.stream()`` context
    manager. The wrapper guarantees the generator is closed on context exit
    so any open ``events.stream()`` inside the generator is also closed
    (the generator's inner ``with`` block handles that on GeneratorExit).
    """
    gen = _iter_session_events_with_reconnect(
        session_id=session_id,
        initial_send_events=initial_send_events,
        **kwargs,
    )
    try:
        yield gen
    finally:
        gen.close()


def _paginated_events_lookup(
    session_id: str,
    want_ids: set,
    *,
    max_total: int = 500,
    page_size: int = 100,
) -> list:
    """Page through events.list until all want_ids are found or max_total scanned.

    Codex P2 fix (2026-05-18): single events.list(limit=50) calls were
    missing blocking events in busy multi-agent sessions where the
    requires_action event_ids landed older than the newest 50 events.
    The SDK's list() returns a SyncCursorPage; iterating it auto-paginates.

    Codex P1 fix (2026-05-18): the SDK defaults to ``order="asc"`` (oldest
    first). For requires_action recovery the blocker is always recent, so
    asc ordering causes long sessions with >max_total earlier events to
    never reach the actual blocker — the recovery path then declares it
    unresolvable and the session stays stuck. Pass ``order="desc"`` so the
    iterator yields newest-first. Fall back to default ordering if the
    SDK rejects the kwarg (older SDK versions don't accept it).
    """
    if not want_ids:
        return []
    matched: list = []
    matched_ids: set = set()
    scanned = 0
    try:
        try:
            iterator = client.beta.sessions.events.list(
                session_id=session_id, limit=page_size, order="desc"
            )
        except TypeError:
            log.debug(
                "[EVENTS_LIST_ORDER_UNSUPPORTED] session=%s — falling back to "
                "default ordering; SDK may not accept `order=` yet",
                session_id,
            )
            iterator = client.beta.sessions.events.list(
                session_id=session_id, limit=page_size
            )
        for ev in iterator:
            scanned += 1
            eid_local = getattr(ev, "id", None)
            if eid_local in want_ids and eid_local not in matched_ids:
                matched.append(ev)
                matched_ids.add(eid_local)
            if len(matched_ids) >= len(want_ids) or scanned >= max_total:
                break
    except Exception:
        log.exception(
            "[EVENTS_LIST_LOOKUP_FAILED] session=%s scanned=%d found=%d/%d",
            session_id,
            scanned,
            len(matched_ids),
            len(want_ids),
        )
    if matched_ids < want_ids:
        log.warning(
            "[EVENTS_LIST_LOOKUP_INCOMPLETE] session=%s scanned=%d found=%d/%d "
            "want=%s missing=%s",
            session_id,
            scanned,
            len(matched_ids),
            len(want_ids),
            sorted(want_ids),
            sorted(want_ids - matched_ids),
        )
    return matched


def _stream_and_handle(
    session_id: str,
    send_events=None,
    thread_ts: Optional[str] = None,
    verbosity: str = "summary",
    portco_key: Optional[str] = None,
    user_id: Optional[str] = None,
    event_ts: Optional[str] = None,
    channel_id: Optional[str] = None,
    inv_id: Optional[int] = None,
    followup_mode: bool = False,
):
    """Stream a session, handling custom tools via the requires_action pattern.

    verbosity is REQUEST-scoped (this specific Slack message's mode) and gets
    forwarded to _dispatch_tool for every tool call in this stream.

    portco_key (when known by the caller) is forwarded to _dispatch_tool so
    that successful post_report dispatches can fire the F8 canvas push for
    the correct portco. Cron-style callers (forecast analysis, etc.) leave it
    None — the canvas push is skipped in that case and the 08:00 PT cron
    catches up.

    user_id is the Slack user ID of the thread's original requester (e.g. the
    person who asked the question). Forwarded to _dispatch_tool → post_report
    so the @-mention on the result pings the asker, not the global
    SLACK_NOTIFY_USER_IDS admin list. Cron callers leave it None and the
    admin-list fallback applies.

    event_ts + channel_id, when set, identify the user's original Slack
    message. Forwarded to _dispatch_tool → _dispatch_post_report so the
    lifecycle reaction (⏰ → ✅) can be flipped on the right message at
    the post_report success boundary.

    Returns (agent_text_parts, delivery_state, error_type, soql_queries).

    ``delivery_state`` (a ``lifecycle.DeliveryState``) replaces the previous
    optimistic ``agent_posted_to_slack: bool``. The bool was set the moment
    an ``agent.custom_tool_use`` event fired for ``send_slack_notification``
    or ``post_report`` — BEFORE the dispatcher confirmed delivery. Combined
    with the orchestration-chatter blocklist (line 585) that silently
    suppresses tool-use without posting, the flag lied: it could be True
    for sessions where the user got nothing, AND the caller's fallback
    ``post_analysis`` was skipped on that false promotion.

    Honest promotion (post-refactor): state stays ``NOT_DELIVERED`` until
    the dispatcher's JSON result confirms a real Slack post:
      - ``send_slack_notification`` with ``ok=True AND message_ts`` (not
        blocked by chatter) → ``DELIVERED_VIA_LEGACY_SLACK_TOOL``
      - ``post_report`` with ``ok=True`` (not retry, not give-up) →
        ``DELIVERED_VIA_POST_REPORT``
    """
    from lifecycle import DeliveryState  # lazy to avoid circular at module load

    agent_text_parts = []
    delivery_state = DeliveryState.NOT_DELIVERED
    error_type = None
    pending_tools = {}
    soql_queries = []

    seen_ids = set()

    # Plan #47 Workstream C: cache of (session_thread_id, frozenset(event_ids))
    # tuples we've already issued an events.list lookup for, to avoid duplicate
    # fetches inside a single _stream_and_handle loop. See §6.7 of the plan —
    # one events.list call per sub-thread requires_action batch, not per eid.
    _subthread_lookup_cache: set = set()

    # Auto-reconnecting stream. Wraps events.stream() in a retry loop that
    # survives transient transport drops by replaying missed events via
    # events.list(created_at_gt=...). See _iter_session_events_with_reconnect
    # for the full failure-mode rationale. seen_ids dedup below is what makes
    # the backfill-then-reopen overlap window safe.
    with _streaming_events_with_reconnect(
        session_id=session_id,
        initial_send_events=send_events,
        followup_mode=followup_mode,
    ) as stream:
        for event in stream:
            if hasattr(event, "id") and event.id:
                if event.id in seen_ids:
                    continue
                seen_ids.add(event.id)
            # Plan #44 Task #16: capture multiagent thread events for the
            # session_thread_events ledger. Filtered to a known whitelist;
            # everything else is a no-op. Buffered, flushed on status_idle.
            try:
                _buffer_thread_event(session_id, event, portco_key=portco_key)
            except Exception:
                log.exception(
                    "Thread event buffer failed for session=%s — telemetry "
                    "skipped, primary loop continues",
                    session_id,
                )
            # PR 3 (floating-prancing-trinket plan): when the Coordinator
            # dispatches a task to a sub-agent, check the dispatch body
            # for tool-name hints before the sub-agent picks it up. If
            # the destination lacks the referenced tool, inject a
            # structured error back into the parent session so the
            # Coordinator can re-plan. The sub-agent still receives the
            # original message — we cannot intercept server-side routing
            # — but the Coordinator gets a parseable signal to redispatch
            # to a capable agent. See TOOL_CAPABILITY_MAP / check_dispatch_capability.
            #
            # Only ``agent.thread_message_sent`` is consumed here — that
            # event fires when the Coordinator commits a dispatch to a
            # specific sub-agent, with the destination in
            # ``to_agent_name`` and the full body settled. The earlier
            # ``agent.thread_message_started`` / ``..._delta`` events
            # may carry partial bodies and don't always populate the
            # ``to_agent_name`` field reliably — checking them risks
            # false positives on partial text and on sub-agent → parent
            # reply traffic. ``to_agent_name`` is required (no
            # ``agent_name`` fallback) so a sub-agent's outbound reply
            # to the Coordinator — which carries the SENDER name in
            # ``agent_name`` but no ``to_agent_name`` — does not get
            # mis-classified as a dispatch with a missing tool.
            if event.type == "agent.thread_message_sent":
                try:
                    to_agent_name = getattr(event, "to_agent_name", None)
                    if not to_agent_name:
                        cap_err = None
                    else:
                        cap_err = check_dispatch_capability(
                            to_agent_name=to_agent_name,
                            dispatch_body=_extract_text_from_event(event),
                        )
                    if cap_err is not None:
                        log.warning(
                            "[TOOL_CAPABILITY_MISMATCH] session=%s %s",
                            session_id,
                            cap_err["message"],
                        )
                        try:
                            client.beta.sessions.events.send(
                                session_id=session_id,
                                events=[
                                    {
                                        "type": "user.message",
                                        "content": [
                                            {
                                                "type": "text",
                                                "text": cap_err["message"],
                                            }
                                        ],
                                    }
                                ],
                            )
                        except Exception:
                            log.exception(
                                "Failed to inject tool_capability_mismatch "
                                "error back to session=%s",
                                session_id,
                            )
                except Exception:
                    log.exception(
                        "Tool capability check failed for session=%s — "
                        "primary loop continues",
                        session_id,
                    )

            if event.type == "agent.message":
                for block in event.content:
                    if hasattr(block, "text") and block.text:
                        agent_text_parts.append(block.text)
                        log.info(f"Agent: {block.text[:200]}")

            elif event.type == "agent.custom_tool_use":
                pending_tools[event.id] = event
                # Honest delivery tracking (2026-05-13 refactor): delivery_state
                # is NOT promoted here. The previous code promoted optimistically
                # at the tool_use event — but the orchestration-chatter blocklist
                # (session_runner.py:585) silently suppresses send_slack_notification
                # without posting, leaving the user with nothing AND the fallback
                # path skipped. Promotion now happens after _dispatch_tool returns
                # and only if the dispatcher's JSON result confirms a real
                # delivery. See the dispatch-result block below.
                pass
            elif event.type == "agent.mcp_tool_use":
                perm = getattr(event, "evaluated_permission", None)
                if perm == "ask":
                    pending_tools[event.id] = event
                if event.name in ("soqlQuery", "describeSObject"):
                    soql_queries.append(
                        {
                            "tool": event.name,
                            "input": event.input,
                        }
                    )

            elif event.type == "session.thread_status_idle":
                # Plan #47 Workstream C — buffer cross-posted sub-thread tool
                # events so the parent's session.status_idle requires_action
                # can dispatch them. Sub-agent agent.custom_tool_use /
                # agent.mcp_tool_use events appear in events.list with a
                # non-null session_thread_id but are NOT delivered on the
                # parent SSE stream (empirically confirmed against
                # sesn_EXAMPLE, 2026-05-18 — see plan §9).
                # Without this branch, pending_tools.get(eid) returns None
                # at the parent requires_action and the eid is logged as
                # [REQUIRES_ACTION_UNHANDLED], silently stranding the
                # session until the watchdog interrupts it.
                sub_stop_reason = getattr(event, "stop_reason", None)
                sub_sr_type = (
                    getattr(sub_stop_reason, "type", None) if sub_stop_reason else None
                )
                stid = getattr(event, "session_thread_id", None)
                if sub_sr_type == "requires_action" and stid:
                    sub_event_ids = tuple(
                        getattr(sub_stop_reason, "event_ids", []) or []
                    )
                    cache_key = (stid, frozenset(sub_event_ids))
                    if sub_event_ids and cache_key not in _subthread_lookup_cache:
                        _subthread_lookup_cache.add(cache_key)
                        wanted = set(sub_event_ids)
                        # Codex P2 fix (2026-05-18): paginate events.list so
                        # the blocking event isn't missed when it's older
                        # than the newest 50 events in busy multi-agent
                        # sessions. Bounded scan to protect against runaway.
                        sub_data = _paginated_events_lookup(
                            session_id, wanted, max_total=500, page_size=100
                        )
                        for sub_e in sub_data:
                            if getattr(sub_e, "id", None) not in wanted:
                                continue
                            if getattr(sub_e, "session_thread_id", None) != stid:
                                continue
                            sub_type = getattr(sub_e, "type", "")
                            if sub_type not in (
                                "agent.custom_tool_use",
                                "agent.mcp_tool_use",
                            ):
                                continue
                            if sub_e.id in pending_tools:
                                continue
                            pending_tools[sub_e.id] = sub_e
                            log.info(
                                "[SUBTHREAD_TOOL_BUFFERED] event_id=%s "
                                "thread=%s tool=%s",
                                sub_e.id,
                                stid,
                                getattr(sub_e, "name", "?"),
                            )

            elif event.type == "session.status_idle":
                stop_reason = getattr(event, "stop_reason", None)
                sr_type = getattr(stop_reason, "type", None) if stop_reason else None

                if sr_type == "requires_action":
                    event_ids = getattr(stop_reason, "event_ids", [])
                    results = []
                    for eid in event_ids:
                        tool_event = pending_tools.get(eid)
                        if tool_event:
                            if tool_event.type == "agent.mcp_tool_use":
                                # Plan #44 Task #22 / decision row #14:
                                # require an explicit allowlist for MCP
                                # auto-approve. Today no MCP servers are
                                # attached in production so the allowlist
                                # is empty; this branch logs + admin-DMs
                                # any unrecognized server instead of
                                # silently approving. Bundle D may add the
                                # SF vault server name once the SF MCP
                                # path is re-enabled.
                                mcp_server = (
                                    getattr(tool_event, "mcp_server_name", None) or ""
                                )
                                if mcp_server in _config.MCP_AUTO_APPROVE_ALLOWLIST:
                                    results.append(
                                        {
                                            "type": "user.tool_confirmation",
                                            "tool_use_id": eid,
                                            "result": "allow",
                                        }
                                    )
                                else:
                                    log.warning(
                                        "[UNRECOGNIZED_MCP_SERVER] "
                                        "mcp_server_name=%r session=%s "
                                        "tool=%s — NOT auto-approving "
                                        "(allowlist=%s)",
                                        mcp_server,
                                        session_id,
                                        getattr(tool_event, "name", "?"),
                                        sorted(_config.MCP_AUTO_APPROVE_ALLOWLIST),
                                    )
                                    try:
                                        send_notification(
                                            "watch",
                                            "Unrecognized MCP server requested "
                                            f"approval: `{mcp_server or '<unset>'}`. "
                                            "The auto-approve allowlist is empty by "
                                            "default — Plan #44 Task #22. Add the "
                                            "server name to MCP_AUTO_APPROVE_ALLOWLIST "
                                            "if this is expected.",
                                            detail=(
                                                f"session={session_id}\n"
                                                f"tool={getattr(tool_event, 'name', '?')}"
                                            ),
                                            admin_only=True,
                                        )
                                    except Exception:
                                        log.exception(
                                            "Admin DM about unrecognized MCP "
                                            "server failed — log line above is "
                                            "the audit trail"
                                        )
                                    # Deny — explicit "deny" result per
                                    # Anthropic Managed Agents docs:
                                    # https://docs.anthropic.com/en/docs/build-with-claude/managed-agents/events-and-streaming#user-tool-confirmation
                                    # ("Send result='deny' to refuse the
                                    # tool call; the session resumes and
                                    # the agent receives a denial in its
                                    # tool-result stream.")
                                    # Closing-review fix 2026-05-13: send
                                    # the explicit deny rather than relying
                                    # on absence-of-confirmation; the SDK
                                    # contract is that the session resumes
                                    # after a deny and the agent gets the
                                    # signal to re-plan. The synthetic test
                                    # ``test_mcp_deny_path_resumes_session``
                                    # in plan_44_bundle_b_test.py locks
                                    # this contract in.
                                    results.append(
                                        {
                                            "type": "user.tool_confirmation",
                                            "tool_use_id": eid,
                                            "result": "deny",
                                        }
                                    )
                            else:
                                result_text = _dispatch_tool(
                                    tool_event.name,
                                    tool_event.input,
                                    thread_ts=thread_ts,
                                    session_id=session_id,
                                    verbosity=verbosity,
                                    portco_key=portco_key,
                                    user_id=user_id,
                                    event_ts=event_ts,
                                    channel_id=channel_id,
                                    inv_id=inv_id,
                                )
                                # B7: when the dispatcher signals a recoverable
                                # in-band failure (e.g. post_report schema
                                # validation), surface ``is_error=true`` so the
                                # agent self-corrects instead of treating the
                                # JSON as a normal result. The marker key
                                # ``_is_error`` is added by _retry_or_give_up
                                # in _dispatch_post_report.
                                tool_result_event = {
                                    "type": "user.custom_tool_result",
                                    "custom_tool_use_id": eid,
                                    "content": [{"type": "text", "text": result_text}],
                                }
                                parsed = None
                                try:
                                    parsed = json.loads(result_text)
                                    if (
                                        isinstance(parsed, dict)
                                        and parsed.get("_is_error") is True
                                    ):
                                        tool_result_event["is_error"] = True
                                except (json.JSONDecodeError, TypeError):
                                    # Non-JSON results (legacy paths) just go
                                    # through as plain success results.
                                    pass

                                # Honest delivery-state promotion. Only promote
                                # AFTER the dispatcher confirms a real Slack
                                # post — never at the agent.custom_tool_use
                                # event boundary. State never demotes once
                                # delivered.
                                if (
                                    isinstance(parsed, dict)
                                    and parsed.get("ok") is True
                                    and not delivery_state.is_delivered()
                                ):
                                    name = getattr(tool_event, "name", "") or ""
                                    if name == "post_report":
                                        # _dispatch_post_report already called
                                        # terminalize_lifecycle on its way out;
                                        # this is for the caller's fallback
                                        # suppression contract.
                                        delivery_state = (
                                            DeliveryState.DELIVERED_VIA_POST_REPORT
                                        )
                                    elif name == "send_slack_notification":
                                        # Chatter blocklist returns
                                        # {"ok": True, "blocked": "orchestration_chatter",
                                        #  "message_ts": ""} — guard on the
                                        # message_ts (non-empty) before
                                        # promoting. Without this the latent
                                        # bug from pre-refactor reappears.
                                        if parsed.get("message_ts"):
                                            delivery_state = DeliveryState.DELIVERED_VIA_LEGACY_SLACK_TOOL
                                # Codex P1 (PR #252 follow-up). When the
                                # cancelled-guard short-circuits post_report
                                # because another path (watchdog Tier 3,
                                # /stop, recovery) already terminalized the
                                # investigation, the result dict carries
                                # ``_terminal: True``. The caller-side
                                # fallback consults
                                # ``_consume_post_report_cancelled_guard``
                                # to suppress its "no output / incomplete"
                                # Slack post; this log line is the audit
                                # trail operators can grep against the
                                # ``[POST_REPORT_CANCELLED]`` warning the
                                # dispatcher emitted moments earlier.
                                if (
                                    isinstance(parsed, dict)
                                    and parsed.get("_terminal") is True
                                    and (getattr(tool_event, "name", "") or "")
                                    == "post_report"
                                ):
                                    log.info(
                                        "[POST_REPORT_GUARD_TERMINAL] session=%s — "
                                        "cancelled-guard fired; caller-side "
                                        "fallback will be suppressed via "
                                        "_consume_post_report_cancelled_guard",
                                        session_id,
                                    )
                                results.append(tool_result_event)
                            del pending_tools[eid]
                        else:
                            # Plan #47 Workstream C (2026-05-18). Cross-posted
                            # sub-thread requires_action events are NOT
                            # auto-routed by the SDK — empirically confirmed
                            # against sesn_EXAMPLE (plan §2.2,
                            # §9). The session.thread_status_idle branch above
                            # buffers them into pending_tools via events.list.
                            # If we still land here post-fix, the lookup
                            # either failed or returned no match — neither is
                            # recoverable in-band. Try events.list one more
                            # time on the parent endpoint to identify the
                            # event type and respond appropriately (deny MCP
                            # so the session resumes; leave custom-tool for
                            # the watchdog).
                            tool_event_recovered = None
                            # Codex P2 fix (2026-05-18): same pagination as
                            # the buffering lookup. Single-page (limit=50)
                            # missed events older than the newest 50 in
                            # busy sessions, defeating the recovery path.
                            recovered_list = _paginated_events_lookup(
                                session_id, {eid}, max_total=500, page_size=100
                            )
                            for re_ev in recovered_list:
                                if getattr(re_ev, "id", None) == eid:
                                    tool_event_recovered = re_ev
                                    break

                            if tool_event_recovered is None:
                                log.error(
                                    "[REQUIRES_ACTION_UNRESOLVABLE] event_id=%s "
                                    "session=%s — events.list returned no "
                                    "matching event; cannot dispatch or deny. "
                                    "Watchdog will pick up the session if it "
                                    "stays idle past STALL_THRESHOLD_SECONDS.",
                                    eid,
                                    session_id,
                                )
                                try:
                                    send_notification(
                                        "watch",
                                        "Unresolvable requires_action event "
                                        f"on session `{session_id}`: "
                                        f"`{eid}` not found in events.list. "
                                        "Plan #47 Workstream C — the new "
                                        "[REQUIRES_ACTION_UNRESOLVABLE] path "
                                        "fired. Investigate.",
                                        detail=(
                                            f"session={session_id}\nevent_id={eid}"
                                        ),
                                        admin_only=True,
                                    )
                                except Exception:
                                    log.exception(
                                        "Admin DM about unresolvable "
                                        "requires_action failed — log line "
                                        "above is the audit trail"
                                    )
                            elif (
                                getattr(tool_event_recovered, "type", "")
                                == "agent.mcp_tool_use"
                            ):
                                # Codex P2 fix (2026-05-18): mirror the
                                # buffered MCP allowlist path. Recovery should
                                # not flip allow→deny for legitimate sub-agent
                                # Kapa/SF reads just because the initial
                                # sub-thread lookup missed; apply the same
                                # MCP_AUTO_APPROVE_ALLOWLIST gate.
                                mcp_server_r = (
                                    getattr(
                                        tool_event_recovered, "mcp_server_name", None
                                    )
                                    or ""
                                )
                                if mcp_server_r in _config.MCP_AUTO_APPROVE_ALLOWLIST:
                                    log.info(
                                        "[SUBTHREAD_TOOL_RECOVERED] event_id=%s "
                                        "session=%s tool=%s mcp_server=%s — "
                                        "allowing per MCP_AUTO_APPROVE_ALLOWLIST.",
                                        eid,
                                        session_id,
                                        getattr(tool_event_recovered, "name", "?"),
                                        mcp_server_r,
                                    )
                                    results.append(
                                        {
                                            "type": "user.tool_confirmation",
                                            "tool_use_id": eid,
                                            "result": "allow",
                                        }
                                    )
                                else:
                                    log.warning(
                                        "[REQUIRES_ACTION_UNRESOLVABLE] event_id=%s "
                                        "session=%s tool=%s mcp_server=%r — "
                                        "denying MCP call (not on allowlist=%s).",
                                        eid,
                                        session_id,
                                        getattr(tool_event_recovered, "name", "?"),
                                        mcp_server_r,
                                        sorted(_config.MCP_AUTO_APPROVE_ALLOWLIST),
                                    )
                                    results.append(
                                        {
                                            "type": "user.tool_confirmation",
                                            "tool_use_id": eid,
                                            "result": "deny",
                                        }
                                    )
                                    try:
                                        send_notification(
                                            "watch",
                                            "Unresolvable MCP requires_action on "
                                            f"session `{session_id}`: "
                                            f"`{eid}` denied — server `{mcp_server_r or '<unset>'}` "
                                            "not on MCP_AUTO_APPROVE_ALLOWLIST. "
                                            "Plan #47 Workstream C — investigate why "
                                            "the sub-thread lookup missed it.",
                                            detail=(
                                                f"session={session_id}\n"
                                                f"event_id={eid}\n"
                                                f"tool={getattr(tool_event_recovered, 'name', '?')}"
                                            ),
                                            admin_only=True,
                                        )
                                    except Exception:
                                        log.exception(
                                            "Admin DM about unresolvable MCP "
                                            "event failed — log line above is "
                                            "the audit trail"
                                        )
                            else:
                                # Codex P2 fix (2026-05-18): custom tool found
                                # via recovery has the same .name/.input shape
                                # as a buffered one, so dispatch it through
                                # the existing _dispatch_tool path rather than
                                # stranding the session for the watchdog. The
                                # buffered path was just earlier in the loop;
                                # recovery is the safety net, and the safety
                                # net should COMPLETE the work, not just log.
                                try:
                                    result_text = _dispatch_tool(
                                        tool_event_recovered.name,
                                        tool_event_recovered.input,
                                        thread_ts=thread_ts,
                                        session_id=session_id,
                                        verbosity=verbosity,
                                        portco_key=portco_key,
                                        user_id=user_id,
                                        event_ts=event_ts,
                                        channel_id=channel_id,
                                        inv_id=inv_id,
                                    )
                                except Exception:
                                    log.exception(
                                        "[SUBTHREAD_TOOL_RECOVERED_DISPATCH_FAILED] "
                                        "event_id=%s session=%s tool=%s — "
                                        "dispatcher raised; admin-DMing and "
                                        "leaving stranded for watchdog.",
                                        eid,
                                        session_id,
                                        getattr(tool_event_recovered, "name", "?"),
                                    )
                                    try:
                                        send_notification(
                                            "watch",
                                            "Custom tool dispatch raised in "
                                            f"recovery path on session `{session_id}`: "
                                            f"`{eid}` "
                                            f"(`{getattr(tool_event_recovered, 'name', '?')}`). "
                                            "Plan #47 Workstream C — watchdog will "
                                            "interrupt; investigate dispatcher fault.",
                                            detail=(
                                                f"session={session_id}\n"
                                                f"event_id={eid}\n"
                                                f"tool={getattr(tool_event_recovered, 'name', '?')}"
                                            ),
                                            admin_only=True,
                                        )
                                    except Exception:
                                        log.exception(
                                            "Admin DM about recovery-dispatch "
                                            "failure failed — log line above "
                                            "is the audit trail"
                                        )
                                else:
                                    log.info(
                                        "[SUBTHREAD_TOOL_RECOVERED] event_id=%s "
                                        "session=%s tool=%s — dispatched via "
                                        "recovery path.",
                                        eid,
                                        session_id,
                                        getattr(tool_event_recovered, "name", "?"),
                                    )
                                    # Codex P2 fix (2026-05-18): mirror the
                                    # buffered path's post-dispatch logic.
                                    # Without this, a recovered post_report
                                    # schema-retry error gets sent as a
                                    # success (no is_error=true), and a
                                    # successful recovered post_report
                                    # leaves delivery_state on
                                    # NOT_DELIVERED so the runner's
                                    # incomplete/no-output fallback fires
                                    # a second time.
                                    tool_result_event = {
                                        "type": "user.custom_tool_result",
                                        "custom_tool_use_id": eid,
                                        "content": [
                                            {"type": "text", "text": result_text}
                                        ],
                                    }
                                    parsed = None
                                    try:
                                        parsed = json.loads(result_text)
                                        if (
                                            isinstance(parsed, dict)
                                            and parsed.get("_is_error") is True
                                        ):
                                            tool_result_event["is_error"] = True
                                    except (json.JSONDecodeError, TypeError):
                                        pass
                                    if (
                                        isinstance(parsed, dict)
                                        and parsed.get("ok") is True
                                        and not delivery_state.is_delivered()
                                    ):
                                        recovered_name = (
                                            getattr(tool_event_recovered, "name", "")
                                            or ""
                                        )
                                        if recovered_name == "post_report":
                                            delivery_state = (
                                                DeliveryState.DELIVERED_VIA_POST_REPORT
                                            )
                                        elif (
                                            recovered_name == "send_slack_notification"
                                        ):
                                            if parsed.get("message_ts"):
                                                delivery_state = DeliveryState.DELIVERED_VIA_LEGACY_SLACK_TOOL
                                    # Codex P1 (PR #252 follow-up). Mirror the
                                    # buffered path's [POST_REPORT_GUARD_TERMINAL]
                                    # audit-log line for the Workstream-C
                                    # recovery branch.
                                    if (
                                        isinstance(parsed, dict)
                                        and parsed.get("_terminal") is True
                                        and (
                                            getattr(tool_event_recovered, "name", "")
                                            or ""
                                        )
                                        == "post_report"
                                    ):
                                        log.info(
                                            "[POST_REPORT_GUARD_TERMINAL] session=%s "
                                            "(via Workstream-C recovery) — "
                                            "cancelled-guard fired; caller-side "
                                            "fallback will be suppressed via "
                                            "_consume_post_report_cancelled_guard",
                                            session_id,
                                        )
                                    results.append(tool_result_event)
                    if results:
                        try:
                            client.beta.sessions.events.send(
                                session_id=session_id,
                                events=results,
                            )
                        except anthropic.BadRequestError as e:
                            log.warning(
                                f"Tool result send failed (sub-agent thread may have expired): {e}"
                            )
                        except RecursionError as e:
                            # Anthropic SDK bug observed 2026-05-15 on
                            # sesn_EXAMPLE (inv_id=139,
                            # 6.87M cached input). _models.construct_type
                            # recurses through a deeply-nested response type
                            # and blows Python's default 1000-frame limit,
                            # turning a routine events.send into an
                            # unhandled exception → TERMINAL_FAILURE → ❌
                            # on the user's Slack message. Pushing a
                            # custom_tool_result is best-effort plumbing —
                            # losing one shouldn't kill the investigation.
                            log.error(
                                "Tool result send hit RecursionError in "
                                "anthropic SDK type-construction "
                                "(session=%s, results=%d): %s — continuing",
                                session_id,
                                len(results),
                                e,
                            )
                        except anthropic.APIError as e:
                            # Other SDK-level transients (rate limit,
                            # overloaded, connection error). Same rationale:
                            # don't promote a transient send miss into a
                            # terminalize.
                            log.warning(
                                "Tool result send hit APIError "
                                "(session=%s): %s: %s — continuing",
                                session_id,
                                type(e).__name__,
                                e,
                            )
                    continue
                elif sr_type == "max_tokens":
                    log.info("Session hit max_tokens — sending continuation")
                    client.beta.sessions.events.send(
                        session_id=session_id,
                        events=[
                            {
                                "type": "user.message",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": "Your response was cut off. Continue from where you stopped.",
                                    }
                                ],
                            }
                        ],
                    )
                    continue
                else:
                    log.info(f"Session idle — stop_reason: {sr_type}")
                    # Plan #44 Task #16: flush buffered thread events on
                    # the terminal idle (decision row #11 — buffer per
                    # session, flush on status_idle).
                    try:
                        flushed = _flush_thread_event_buffer(session_id)
                        if flushed:
                            log.debug(
                                "Flushed %d session_thread_events for %s",
                                flushed,
                                session_id,
                            )
                    except Exception:
                        log.exception(
                            "Final thread-event buffer flush failed for %s",
                            session_id,
                        )
                    break

            elif event.type == "session.error":
                error_data = event.error if hasattr(event, "error") else None
                error_type = (
                    getattr(error_data, "type", "unknown") if error_data else "unknown"
                )
                error_message = (
                    getattr(error_data, "message", "N/A") if error_data else "N/A"
                )
                retry_status = getattr(error_data, "retry_status", None)
                rs_type = getattr(retry_status, "type", "") if retry_status else ""
                # Plan #44 Task #16 — retry_status enum discovery. Docs do
                # NOT enumerate this field; empirically we know "terminal"
                # and "exhausted" are the terminal values. Three distinct
                # branches below (closing-review fix 2026-05-13 — the
                # original code had an unreachable second arm because the
                # first arm's ``continue`` swallowed every retry_status
                # whose ``type`` was non-empty and unknown):
                #
                #   1. rs_type in _KNOWN_RETRY_STATUS_TYPES → fall through
                #      to the terminal-error branch below.
                #   2. rs_type in ("", None) → Anthropic is still retrying
                #      internally; log + continue without admin-paging.
                #   3. rs_type is a non-empty unknown value → log
                #      ``[UNKNOWN_RETRY_STATUS_TYPE]``, admin-DM, treat as
                #      transient (continue). This is how we discover a
                #      new enum value without a code-change-and-redeploy
                #      cycle.
                _KNOWN_RETRY_STATUS_TYPES = ("terminal", "exhausted")
                if retry_status and rs_type in ("", None):
                    # Branch (2) — Anthropic's transient-retry path. Empty
                    # ``type`` means "still trying," not "we have a new
                    # enum value." Quiet log line, no admin DM.
                    log.warning(
                        f"Session error (transient retry, empty retry_status.type): "
                        f"{error_type} — {error_message}"
                    )
                    continue
                if (
                    retry_status
                    and rs_type
                    and rs_type not in _KNOWN_RETRY_STATUS_TYPES
                ):
                    # Branch (3) — non-empty unknown value. Loud log,
                    # admin DM, treat as transient. Update the known-enum
                    # set in session_runner._stream_and_handle once the
                    # new value is confirmed.
                    log.error(
                        "[UNKNOWN_RETRY_STATUS_TYPE] type=%s session=%s "
                        "error_type=%s error_message=%s — treating as "
                        "TRANSIENT (re-queue). Update the known-enum set "
                        "in session_runner._stream_and_handle once the "
                        "new value is confirmed.",
                        rs_type,
                        session_id,
                        error_type,
                        (error_message or "")[:200],
                    )
                    try:
                        send_notification(
                            "watch",
                            f"Unknown `retry_status.type={rs_type!r}` from "
                            f"Anthropic for session `{session_id}`. Plan #44 "
                            "Task #16 — orchestrator treats it as transient "
                            "by default. Confirm the enum value and update "
                            "the known set.",
                            detail=(
                                f"error_type={error_type}\n"
                                f"error_message={(error_message or '')[:300]}"
                            ),
                            admin_only=True,
                        )
                    except Exception:
                        log.exception(
                            "Admin DM about unknown retry_status failed — "
                            "the log line above is the audit trail"
                        )
                    continue
                # Branch (1) — known terminal types (``terminal``,
                # ``exhausted``) fall through to the terminal-error path
                # below. Slack-notify the user in-thread and break out of
                # the stream loop.
                log.error(f"Session error: {error_type} — {error_message}")
                # Terminal error — notify the user in-thread so they know the
                # run died. Without this, users stare at a stale ack with no
                # follow-up (see session sesn_EXAMPLE, where
                # the run crossed the 1M-token cap at 1,119,846 and the
                # Coordinator never posted again).
                if thread_ts:
                    _post_session_error_to_slack(
                        thread_ts=thread_ts,
                        error_type=error_type,
                        error_message=error_message,
                        session_id=session_id,
                    )
                # Plan #44 Task #16: flush buffered thread events on
                # terminal session.error.
                try:
                    _flush_thread_event_buffer(session_id)
                except Exception:
                    log.exception(
                        "Thread-event buffer flush failed for %s",
                        session_id,
                    )
                break

            elif event.type == "session.status_rescheduled":
                # Per Managed Agents docs (events-and-streaming):
                # "session.status_rescheduled — A transient error occurred and
                # the session is retrying automatically." This is NOT a failure
                # mode. The session is still alive, Anthropic is retrying the
                # request internally, and the next event will be a normal
                # status_running / agent.message. Do NOT break out of the loop
                # and do NOT notify Slack — the user's investigation is still
                # progressing. Log at INFO so the event is visible in the
                # session log without triggering ops-monitoring noise.
                log.info(
                    f"Session {session_id} rescheduled by Anthropic "
                    "(transient retry). Continuing event loop."
                )

            elif event.type == "session.status_running":
                # Docs: "Agent is actively processing." Purely informational —
                # emitted every time the session transitions from idle/queued
                # back to actively generating. No action needed.
                log.debug(f"Session {session_id} running")

            elif event.type == "agent.thread_context_compacted":
                log.warning(
                    "Session context was compacted — earlier context may be summarized"
                )

            elif event.type == "session.status_terminated":
                # Docs: "Session ended due to an unrecoverable error." This is
                # the post-retry-exhaustion terminal state. Exit the loop; the
                # session.error handler above is what posts the user-facing
                # Slack recovery message (we only reach this if no session.error
                # arrived first, which is the abrupt-disconnect case).
                log.info(f"Session {session_id} terminated")
                # Plan #44 Task #16: flush buffered thread events on
                # terminal session.status_terminated.
                try:
                    _flush_thread_event_buffer(session_id)
                except Exception:
                    log.exception(
                        "Thread-event buffer flush failed for %s",
                        session_id,
                    )
                break

    return agent_text_parts, delivery_state, error_type, soql_queries


def _download_session_files(session_id: str, reply_to: str = None, channel: str = None):
    """Download output files from a session and upload to Slack.

    ``channel`` (when set) routes the upload to a specific channel,
    overriding ``post_file``'s default of ``SLACK_CHANNEL_ID``. Callers
    that drive sessions in a non-default channel (RFP intake, future
    per-portco intake) must pass it so the file lands in the same
    thread the summary went to. Without this, ``post_file`` falls back
    to ``SLACK_CHANNEL_ID`` while keeping the caller's ``reply_to``
    thread_ts — Slack then either uploads to the wrong channel or
    rejects the thread_ts as not-in-this-channel.
    """
    for _ in range(3):
        files = client.beta.files.list(
            scope_id=session_id,
            betas=["managed-agents-2026-04-01"],
        )
        if files.data:
            break
        time.sleep(2)
    else:
        return

    _ensure_dirs()
    for f in files.data:
        try:
            content = client.beta.files.download(f.id)
            out_path = OUTPUTS_DIR / f.filename
            out_path.parent.mkdir(parents=True, exist_ok=True)
            content.write_to_file(str(out_path))
            log.info(f"Downloaded: {out_path}")

            suffix = out_path.suffix.lower()
            if suffix in (".xlsx", ".csv", ".docx", ".pptx", ".png", ".pdf"):
                post_file(
                    file_path=str(out_path),
                    reply_to=reply_to,
                    channel=channel,
                )
                log.info(f"Uploaded to Slack: {out_path.name}")
        except Exception as e:
            log.warning(f"Skipping non-downloadable file {f.id} ({f.filename}): {e}")


def _infer_session_outcome(s, tool_names: Optional[list]) -> str:
    """Best-effort terminal-state label for ``session_costs.outcome``.

    Plan #42 PR1 (D11). The new column lets ``bin/measure-deploy-risk.py``
    compute Acme error rates per hour without a separate incident log.
    The label is intentionally coarse — ``success | error | abandoned`` —
    because the histogram script aggregates by hour, not by failure mode.

    Inference order (first match wins):
      1. Audit-trail markers in ``tool_names`` (``[WRITING_AGENT_FALLTHROUGH]``,
         ``[SURFACE_PUSH_FAILED]``) → ``error``.
      2. Anthropic session ``status`` of ``failed`` / ``error`` → ``error``.
      3. Status ``cancelled`` / ``timed_out`` → ``abandoned``.
      4. Anything else, including the normal ``completed`` / ``idle`` /
         ``archived`` terminal states → ``success``.

    Never raises — degrades to ``success`` if the session object is missing
    or its shape is unexpected. The migration default is also ``success``,
    so a degraded inference matches the database default exactly.
    """
    if tool_names:
        for name in tool_names:
            if not isinstance(name, str):
                continue
            if "[WRITING_AGENT_FALLTHROUGH]" in name or "[SURFACE_PUSH_FAILED]" in name:
                return "error"
    try:
        status = getattr(s, "status", None)
    except Exception:
        status = None
    if isinstance(status, str):
        lower = status.lower()
        if lower in ("failed", "error", "errored"):
            return "error"
        if lower in ("cancelled", "canceled", "timed_out", "abandoned"):
            return "abandoned"
    return "success"


def _log_session_usage(
    session_id: str,
    session_type: str,
    *,
    portco_key: str = None,
    channel_id: str = None,
    thread_ts: str = None,
    user_id: str = None,
    trigger: str = None,
    verbosity: str = None,
    agent_id: str = None,
    response_length_chars: int = None,
    tool_names: list = None,
    tier: str = "realtime",
    outcome: Optional[str] = None,
):
    """Log token usage + estimated cost for a completed session AND persist a
    row to session_costs for downstream cost rollups (Plan #35).

    Logs all four input-token categories explicitly so cache effectiveness
    is visible:
        input        = new uncached tokens this turn
        cache_write  = tokens written to ephemeral cache (5m + 1h)
        cache_read   = tokens served from cache
        cache %      = cache_read / (input + cache_read + cache_write)

    Attribution kwargs (Plan #35 Task #35) are persisted to session_costs:
        portco_key, channel_id, thread_ts, user_id, trigger ('cron'|'slack'|
        'recovery'|'dream'), verbosity, agent_id.

    Observability kwargs (Plan #21 — Eng E12 deferred to v1.1):
        response_length_chars — cumulative output text length (chars)
        tool_names — list of distinct tool names used (post_report,
                     send_slack_notification, soqlQuery, etc.)

    Best-effort persistence — a DB write failure does not propagate to the
    session loop. DATABASE_URL not being set means the ledger is skipped
    (matches db_adapter.ensure_schema's degraded-mode behavior).
    """
    try:
        s = client.beta.sessions.retrieve(session_id)
        u = s.usage
        agent_model = getattr(s, "model", None)
        cost = _estimate_cost(u, agent_model)
        parts = _extract_usage_parts(u)
        cache_pct = _cache_hit_pct(u)
        cache_write_total = parts["cache_write_5m"] + parts["cache_write_1h"]
        tool_summary = ""
        if tool_names:
            uniq = sorted(set(tool_names))
            tool_summary = f" tools={','.join(uniq)}"
        resp_summary = ""
        if response_length_chars is not None:
            resp_summary = f" out_chars={response_length_chars:,}"
        # Plan #42 PR1 D11 — derive ``outcome`` from session terminal state +
        # audit-trail markers so ``bin/measure-deploy-risk.py`` can compute
        # the Acme error-rate-per-hour histogram without a separate
        # incident log. Caller may override with an explicit kwarg.
        resolved_outcome = outcome or _infer_session_outcome(s, tool_names)
        log.info(
            f"Session usage [{session_type}] {session_id}: "
            f"input={parts['input']:,} output={parts['output']:,} "
            f"cache_read={parts['cache_read']:,} cache_write={cache_write_total:,} "
            f"({cache_pct}% cached) cost=${cost:.4f} outcome={resolved_outcome}"
            f"{resp_summary}{tool_summary}"
        )
        _persist_session_cost(
            session_id=session_id,
            agent_id=agent_id,
            model=agent_model or "unknown",
            portco_key=portco_key,
            channel_id=channel_id,
            thread_ts=thread_ts,
            user_id=user_id,
            trigger=trigger or session_type,
            verbosity=verbosity,
            usage_parts=parts,
            cost_usd=cost,
            tier=tier,
            outcome=resolved_outcome,
        )
    except Exception:
        log.exception(f"Failed to log usage for session {session_id}")


def _persist_session_cost(
    *,
    session_id: str,
    agent_id: str,
    model: str,
    portco_key: str,
    channel_id: str,
    thread_ts: str,
    user_id: str,
    trigger: str,
    verbosity: str,
    usage_parts: dict,
    cost_usd: float,
    tier: str,
    outcome: str = "success",
):
    """Persist a single session_costs row. Best-effort — swallows DB errors
    and logs them so the session loop is never blocked by ledger problems.

    ``outcome`` (Plan #42 PR1 D11) labels the session's terminal state
    (``success | error | abandoned``) so ``bin/measure-deploy-risk.py``
    can compute the Acme error-rate-per-hour histogram. The migration
    default is ``success`` — that matches the kwarg default exactly so
    callers without outcome plumbing see no behavior change.
    """
    if not getattr(db_adapter, "DATABASE_URL", ""):
        return

    # Compute cache_hit_pct here so the column the ``cost_rollup_daily`` view
    # expects (per Plan #35, line 86) is populated for every row. Originally
    # the column existed on paper but the INSERT skipped it — caught in
    # docs/proposals/cache-audit-2026-05-11.md §3. The denominator is every
    # input-side token category: fresh input, cache reads, and both cache
    # write tiers. When there's no input traffic at all, the hit rate is 0%
    # by convention (rather than undefined / NaN) so the view never has to
    # filter null rows.
    input_tokens = usage_parts.get("input", 0)
    cache_read_tokens = usage_parts.get("cache_read", 0)
    cache_write_5m = usage_parts.get("cache_write_5m", 0)
    cache_write_1h = usage_parts.get("cache_write_1h", 0)
    total_input = input_tokens + cache_read_tokens + cache_write_5m + cache_write_1h
    cache_hit_pct = (
        round(100.0 * cache_read_tokens / total_input, 2) if total_input > 0 else 0.0
    )

    try:
        conn = db_adapter._connect()
        try:
            with conn.cursor() as cur:
                # ON CONFLICT (session_id) DO UPDATE because
                # ``client.beta.sessions.retrieve().usage`` is CUMULATIVE across
                # all turns in a session. Slack thread follow-ups reuse the
                # session and call _log_session_usage once per turn — without
                # the upsert each turn would INSERT a new row carrying a higher
                # cumulative, double-counting spend across multi-turn sessions.
                # The unique index sits in migration 00AI + ensure_schema.
                cur.execute(
                    """
                    INSERT INTO session_costs (
                        session_id, agent_id, model,
                        portco_key, channel_id, thread_ts, user_id,
                        trigger, verbosity,
                        input_tokens, output_tokens,
                        cache_read_tokens, cache_write_5m_tokens, cache_write_1h_tokens,
                        cost_usd, cache_hit_pct, tier, outcome
                    ) VALUES (
                        %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s,
                        %s, %s,
                        %s, %s, %s,
                        %s, %s, %s, %s
                    )
                    ON CONFLICT (session_id) DO UPDATE SET
                        agent_id              = EXCLUDED.agent_id,
                        model                 = EXCLUDED.model,
                        portco_key            = EXCLUDED.portco_key,
                        channel_id            = EXCLUDED.channel_id,
                        thread_ts             = EXCLUDED.thread_ts,
                        user_id               = EXCLUDED.user_id,
                        trigger               = EXCLUDED.trigger,
                        verbosity             = EXCLUDED.verbosity,
                        input_tokens          = EXCLUDED.input_tokens,
                        output_tokens         = EXCLUDED.output_tokens,
                        cache_read_tokens     = EXCLUDED.cache_read_tokens,
                        cache_write_5m_tokens = EXCLUDED.cache_write_5m_tokens,
                        cache_write_1h_tokens = EXCLUDED.cache_write_1h_tokens,
                        cost_usd              = EXCLUDED.cost_usd,
                        cache_hit_pct         = EXCLUDED.cache_hit_pct,
                        tier                  = EXCLUDED.tier,
                        outcome               = EXCLUDED.outcome
                        -- recorded_at INTENTIONALLY preserved on upsert. /cost, the
                        -- daily digest, and cost_rollup_daily all filter on
                        -- recorded_at::date, so overwriting to NOW() on a follow-up
                        -- in a later day would migrate the entire cumulative cost
                        -- to the follow-up day, undercounting yesterday and
                        -- inflating today. Codex P2 finding 2026-05-14.
                    """,
                    (
                        session_id,
                        agent_id,
                        model,
                        portco_key,
                        channel_id,
                        thread_ts,
                        user_id,
                        trigger,
                        verbosity,
                        input_tokens,
                        usage_parts["output"],
                        cache_read_tokens,
                        cache_write_5m,
                        cache_write_1h,
                        cost_usd,
                        cache_hit_pct,
                        tier,
                        outcome,
                    ),
                )
                conn.commit()
        finally:
            conn.close()
    except Exception:
        log.exception(f"Failed to persist session_costs for {session_id}")


def _archive_session(session_id: str):
    """Archive a completed session to free resources.

    Plan #52 PR-C: previously this silently swallowed all exceptions,
    masking archive-rejection failures for sessions still in
    requires_action (e.g. RFP Reviewer after timeout, Responder after
    trailing reasoning_summary). Now logs WARNING on failure and
    retries once after a 5s sleep. Second failure also logs. Latency
    is paid only on the failure path; happy path is unchanged.
    """
    try:
        client.beta.sessions.archive(session_id)
        return
    except Exception as e:
        log.warning(
            "_archive_session failed for session=%s: %s — retrying in 5s",
            session_id,
            e,
        )
    time.sleep(5)
    try:
        client.beta.sessions.archive(session_id)
    except Exception as e:
        log.warning(
            "_archive_session retry failed for session=%s: %s — giving up",
            session_id,
            e,
        )


# --- Scheduled sessions ---


def run_dream_session():
    """Run the dream session — generates investigation plan."""
    rubric_file_id = _upload_file(RUBRICS_DIR / "dream_rubric.md")

    # Plan #44 Task #13: dream is multi-turn (Coordinator reads its plan
    # next morning) but is a cron job with no portco/channel context — the
    # plan covers all portcos. SDK 0.100.0+ removed ``instructions=`` on
    # Sessions.create; the standing-rules text folds into the first
    # user.message body below.
    session = client.beta.sessions.create(
        agent=_resolve_agent_param(DREAM_AGENT_ID),
        environment_id=ENVIRONMENT_ID,
        title="GTM Dream Session",
        vault_ids=VAULT_IDS,
        resources=[
            *MEMORY_RESOURCES,
            {
                "type": "file",
                "file_id": rubric_file_id,
                "mount_path": "/workspace/dream_rubric.md",
            },
        ],
    )
    log.info(f"Dream session created: {session.id}")

    _stream_and_handle(
        session.id,
        send_events=[
            {
                "type": "user.message",
                "content": [
                    {
                        "type": "text",
                        "text": _prepend_session_instructions(
                            "Review the GTM health memory at /mnt/memory/gtm-health/ for all portcos. "
                            "Generate hypotheses about what might have changed, what open questions to "
                            "pursue, and what new patterns to look for. Write a prioritized investigation plan.\n\n"
                            "Follow the quality criteria in /workspace/dream_rubric.md.\n\n"
                            "Write the plan to /mnt/session/outputs/dream_plan.json.\n"
                            "Update the memory store directly at /mnt/memory/gtm-health/ with any changes.",
                            extra_lines=[
                                "Cron-triggered dream pass. Generate hypotheses across all "
                                "portcos and write the investigation plan to "
                                "/mnt/session/outputs/dream_plan.json."
                            ],
                        ),
                    }
                ],
            }
        ],
    )

    _download_session_files(session.id)
    _log_session_usage(
        session.id,
        "dream",
        trigger="cron-dream",
        agent_id=DREAM_AGENT_ID,
    )
    review_session(session.id, "dream")
    _archive_session(session.id)
    send_notification(
        "info", "Dream session complete — investigation plan ready for review"
    )
    log.info("Dream session complete")
    return session.id


def run_investigation_session():
    """Run the investigation session — coordinator + specialists."""
    rubric_file_id = _upload_file(RUBRICS_DIR / "investigation_rubric.md")

    resources = [
        *MEMORY_RESOURCES,
        {
            "type": "file",
            "file_id": rubric_file_id,
            "mount_path": "/workspace/investigation_rubric.md",
        },
    ]

    dream_plan = OUTPUTS_DIR / "dream_plan.json"
    if dream_plan.exists():
        plan_file_id = _upload_file(dream_plan)
        resources.append(
            {
                "type": "file",
                "file_id": plan_file_id,
                "mount_path": "/workspace/dream_plan.json",
            }
        )

    # Plan #44 Task #9/#13 — Coordinator is multi-turn. SDK 0.100.0+
    # removed ``instructions=`` on Sessions.create; standing rules now
    # ride on the first user.message body via
    # ``_prepend_session_instructions``.
    session = client.beta.sessions.create(
        agent=_resolve_agent_param(COORDINATOR_ID),
        environment_id=ENVIRONMENT_ID,
        title="GTM Investigation Session",
        vault_ids=VAULT_IDS,
        resources=resources,
    )
    log.info(f"Investigation session created: {session.id}")

    _stream_and_handle(
        session.id,
        send_events=[
            {
                "type": "user.message",
                "content": [
                    {
                        "type": "text",
                        "text": _prepend_session_instructions(
                            "Read the GTM health memory at /mnt/memory/gtm-health/ and the dream plan "
                            "at /workspace/dream_plan.json (if present). Investigate all three domains.\n\n"
                            "Follow the quality criteria in /workspace/investigation_rubric.md.\n\n"
                            "Send Slack notifications for critical findings as you discover them.\n\n"
                            "Write report to /mnt/session/outputs/weekly_report.md.\n"
                            "Write data to /mnt/session/outputs/findings_data.csv.\n"
                            "Write remediation scripts to /mnt/session/outputs/scripts/.\n\n"
                            "Update the memory store directly at /mnt/memory/gtm-health/.",
                            extra_lines=[
                                "Cron-triggered weekly investigation. Investigate all three "
                                "domains (pipeline, sales process, post-sales) per the rubric "
                                "at /workspace/investigation_rubric.md."
                            ],
                        ),
                    }
                ],
            }
        ],
    )

    _download_session_files(session.id)
    _log_session_usage(
        session.id,
        "investigation",
        trigger="cron-investigation",
        agent_id=COORDINATOR_ID,
    )
    review_session(session.id, "investigation")
    _archive_session(session.id)
    send_notification("info", "Investigation complete — weekly report ready")
    log.info("Investigation session complete")
    return session.id


# --- Nightly forecast & pipeline movement analysis ---


def run_forecast_analysis():
    """Run the coordinator with a forecast analysis prompt.

    Compares today's snapshot against prior snapshots to find pipeline movement,
    forecast changes, and trends segmented by region/product/rep/source.
    Dispatches to the statistician for rigorous analysis and chart agent for visuals.
    """
    # Plan #44 Task #9/#13 — multi-turn Coordinator forecast pass. SDK
    # 0.100.0+ removed ``instructions=`` on Sessions.create; standing
    # rules now ride on the first user.message body via
    # ``_prepend_session_instructions`` below.
    session = client.beta.sessions.create(
        agent=_resolve_agent_param(COORDINATOR_ID),
        environment_id=ENVIRONMENT_ID,
        title="Forecast & Pipeline Movement Analysis",
        vault_ids=VAULT_IDS,
        resources=MEMORY_RESOURCES,
    )
    log.info(f"Forecast analysis session created: {session.id}")

    prompt = (
        "Run a pipeline movement and forecast analysis. Use the Statistician and Chart Designer.\n\n"
        "## Pipeline Movement (snapshot comparison)\n"
        "Query Salesforce for current pipeline. Compare against memory for prior snapshots.\n"
        "For each portco, identify:\n"
        "- Deals that moved forward (stage progression)\n"
        "- Deals that slipped (close date pushed out)\n"
        "- Deals that went dark (no activity in 14+ days)\n"
        "- New deals added since last snapshot\n"
        "- Deals lost since last snapshot\n"
        "- Net pipeline change in dollars\n\n"
        "Segment ALL of the above by: region, product/record type, owner (rep), lead source.\n"
        "Flag any segment where movement is disproportionate (e.g., one rep lost 40% of their pipeline).\n\n"
        "## Forecast Analysis (send to Statistician)\n"
        "Have the Statistician run:\n"
        "1. Weighted pipeline forecast: current pipeline × historical stage-specific conversion rates, "
        "with prediction intervals. Compare to prior week's forecast.\n"
        "2. Trend decomposition on weekly pipeline totals: is the trend accelerating, decelerating, or flat?\n"
        "3. Forecast accuracy tracking: compare prior forecasts to actual outcomes. "
        "Are we consistently over- or under-forecasting? By how much? By segment?\n"
        "4. Velocity analysis: average days-in-stage by stage, compared to 30/60/90 day trailing averages. "
        "Which stages are getting faster or slower?\n"
        "5. Coverage ratio: open pipeline / quota target by rep and region. Flag anyone below 3x.\n\n"
        "## Output\n"
        "Have the Chart Designer produce:\n"
        "- Pipeline waterfall (opening → new → won → lost → slipped → closing)\n"
        "- Forecast trend with confidence band\n"
        "- Stage velocity heatmap (stage × time period)\n"
        "- Coverage ratio by rep\n\n"
        "Send findings to Slack. Write full report to /mnt/session/outputs/forecast_report.md.\n"
        "Update memory with new forecast numbers and any concerning trends.\n"
        "Flag anything that needs immediate attention as critical."
    )

    text_parts, delivery_state, error_type, _ = _stream_and_handle(
        session.id,
        send_events=[
            {
                "type": "user.message",
                "content": [
                    {
                        "type": "text",
                        "text": _prepend_session_instructions(
                            prompt,
                            extra_lines=[
                                "Cron-triggered nightly forecast pass. Dispatch the "
                                "Statistician for rigorous quantitative analysis and the "
                                "Chart Designer for visuals."
                            ],
                        ),
                    }
                ],
            }
        ],
    )

    if error_type:
        log.error(f"Forecast analysis error: {error_type}")
        send_notification("watch", f"Forecast analysis failed: {error_type}")
        return None

    if not delivery_state.is_delivered() and text_parts:
        analysis = "\n\n".join(text_parts)
        post_analysis(
            title="Pipeline & Forecast Analysis",
            analysis_text=analysis,
        )

    _download_session_files(session.id)
    _log_session_usage(
        session.id,
        "forecast",
        trigger="cron-forecast",
        agent_id=COORDINATOR_ID,
    )
    review_session(session.id, "forecast")
    _archive_session(session.id)
    log.info("Forecast analysis complete")
    return session.id


# --- Nightly DB snapshot via direct Salesforce REST API ---

# Base SELECT clauses that every supported SF org is expected to provide.
# Custom fields that may not exist on every org live in OPTIONAL_FIELDS and
# are filtered via describeSObject at sync time. This keeps a portco that
# does not have the GTM custom-field package from nuking its entire Lead
# sync with a "No such column" SOQL error.
SYNC_OBJECTS = {
    "Opportunity": (
        "Id, Name, StageName, Amount, CloseDate, CreatedDate, "
        "LastActivityDate, LastModifiedDate, OwnerId, Owner.Name, LeadSource, "
        "RecordType.Name, IsClosed, IsWon, Probability, FiscalQuarter, FiscalYear, "
        "AccountId, "
        # Custom field that db_adapter.write_records maps into the
        # opportunities.product_line column. Optional per
        # OPTIONAL_SYNC_FIELDS so a portco SF org without
        # Product_Line__c writes NULL instead of erroring the whole
        # Opportunity sync. Added 2026-05-19 after the wtaylor incident
        # (sub3 had to hit live SF because Postgres lacked the column).
        "Product_Line__c"
    ),
    "Lead": (
        "Id, Name, Status, LeadSource, OwnerId, Owner.Name, "
        "CreatedDate, ConvertedDate, IsConverted, "
        # Custom fields that db_adapter.write_records already maps into the
        # leads table columns (discovery_call_booked, funnel_stage, mql_date, sql_date).
        "Discovery_Call_Booked__c, Funnel_Stage__c, "
        "MQL_SDR_Accepted_Date_Time__c, SDR_Qualified_Date_Time__c"
    ),
    "Contact": (
        "Id, Name, Email, Title, AccountId, OwnerId, Owner.Name, "
        "LeadSource, CreatedDate, LastActivityDate"
    ),
    "Account": (
        "Id, Name, RecordType.Name, Industry, BillingCountry, CreatedDate, "
        # Custom fields that db_adapter.write_records already maps into the
        # accounts table columns (customer_tier, contract_status, region, arr).
        "Customer_Tier__c, Contract_Status__c, Region__c, ARR__c"
    ),
}

# Per-object SELECT fields that may legitimately be absent on some portco
# orgs. Filtered against describeSObject() at sync time. Keep these names
# Salesforce-API-exact (case-sensitive, trailing __c).
OPTIONAL_SYNC_FIELDS = {
    "Opportunity": (
        # Product_Line__c — single-string canonical product line per opp.
        # Optional because the org may not have provisioned the field yet;
        # in that case the sync writes NULL to opportunities.product_line
        # rather than failing the whole Opportunity sync.
        "Product_Line__c",
    ),
    "Lead": (
        "Discovery_Call_Booked__c",
        "Funnel_Stage__c",
        "MQL_SDR_Accepted_Date_Time__c",
        "SDR_Qualified_Date_Time__c",
    ),
    # Account custom fields aren't part of the SF standard package; not every
    # portco's org has them. Without filtering, a missing field nukes the
    # whole Account sync with an INVALID_FIELD error and the snapshot
    # records 0 accounts (live incident 2026-05-14 21:18 UTC: Contract_Status__c
    # absent on Acme's Account object → WATCH alert in Slack).
    "Account": (
        "Customer_Tier__c",
        "Contract_Status__c",
        "Region__c",
        "ARR__c",
    ),
}


def _describe_object_fields(sf, object_type: str) -> set[str]:
    """Return the set of API field names available on ``object_type``.

    Uses simple_salesforce's per-object describe handle. Empty set on any
    failure — the caller then keeps the static SELECT clause and lets the
    SOQL path surface the real error rather than masking it as "all fields
    missing".
    """
    try:
        descr = getattr(sf, object_type).describe()
    except Exception:
        log.exception(f"describe({object_type}) failed — skipping field filter")
        return set()
    return {f.get("name") for f in descr.get("fields", []) if f.get("name")}


def _build_select_clause(
    object_type: str, base_clause: str, available_fields: set[str]
) -> tuple[str, list[str]]:
    """Filter ``base_clause`` against the optional-field allowlist + describe.

    Returns ``(clause, missing)`` where ``clause`` is the SELECT body to use
    for this sync and ``missing`` is the list of optional fields that were
    dropped because the describe call did not list them. ``missing`` is
    surfaced to the caller so the operator gets a per-portco warning
    listing partially-synced columns.
    """
    optional = OPTIONAL_SYNC_FIELDS.get(object_type, ())
    if not optional or not available_fields:
        # No optional fields declared OR describe failed → keep the raw
        # base clause unchanged. A real "No such column" error then
        # surfaces in the SOQL execute below.
        return base_clause, []

    fields = [f.strip() for f in base_clause.split(",")]
    optional_set = set(optional)
    missing = [f for f in fields if f in optional_set and f not in available_fields]
    if not missing:
        return base_clause, []

    kept = [f for f in fields if not (f in optional_set and f not in available_fields)]
    return ", ".join(kept), missing


def _get_sf_oauth_client_credentials_token(
    domain: str, client_id: str, client_secret: str
) -> tuple[str, str]:
    """Exchange Connected App client_credentials for an access token + instance URL.

    Salesforce's OAuth 2.0 Client Credentials Flow is the modern server-to-server
    auth path. The Connected App must (a) have ``Enable Client Credentials Flow``
    turned on and (b) have a ``Run As`` user configured under Edit Policies — the
    SOAP username/password+token flow is being deprecated by SF, and orgs with
    SSO enforcement reject it with a generic ``INVALID_LOGIN`` even when
    credentials are correct.

    ``domain`` is the org's My Domain (e.g. ``your-org.my.salesforce.com``).
    The token endpoint lives ONLY at that host — ``login.salesforce.com`` returns
    ``invalid_grant: request not supported on this domain``.

    Returns ``(access_token, instance_url)``. Raises on any failure with the
    raw SF error message so the operator sees the actual cause (missing run-as
    user, invalid client secret, locked Connected App, etc.).
    """
    import httpx

    if not domain.startswith("http"):
        domain = f"https://{domain.rstrip('/')}"
    token_url = f"{domain}/services/oauth2/token"
    resp = httpx.post(
        token_url,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        try:
            body = resp.json()
            err = body.get("error", "unknown")
            desc = body.get("error_description", "")
            raise RuntimeError(
                f"SF OAuth client_credentials failed ({resp.status_code} {err}): {desc}"
            )
        except ValueError:
            raise RuntimeError(
                f"SF OAuth client_credentials returned non-JSON {resp.status_code}: {resp.text[:300]}"
            )
    data = resp.json()
    access_token = data.get("access_token")
    instance_url = data.get("instance_url")
    if not access_token or not instance_url:
        raise RuntimeError(
            f"SF OAuth response missing access_token/instance_url: keys={list(data.keys())}"
        )
    return access_token, instance_url


def _get_sf_client(portco_key: str = None):
    """Create a Salesforce REST API client with per-portco credentials.

    Resolution order (first one with complete creds wins):

    1. **Per-portco OAuth Client Credentials Flow** — when ``portco_config.json``
       maps ``consumer_key_env`` + ``consumer_secret_env`` + ``domain_env``,
       exchange client_id+secret for an access token at the org's My Domain
       and construct a ``Salesforce(instance_url=..., session_id=...)`` client.
       This is the path for OAuth-enforced orgs (SSO + MFA on API logins).
    2. **Per-portco SOAP username/password+token** — legacy path. Works for
       orgs that haven't disabled "Password Login" on the API profile.
    3. **Global SF_USERNAME/SF_PASSWORD/SF_SECURITY_TOKEN** — default fallback.
    4. **Global SF_INSTANCE_URL + SF_ACCESS_TOKEN** — pre-issued bearer token.

    Adding a new SF org = add config entry + env vars, no code changes.
    """
    from simple_salesforce import Salesforce
    from portco_registry import get_portco_config

    sf_creds: dict = {}
    if portco_key:
        pc = get_portco_config(portco_key)
        if pc:
            sf_creds = (
                pc.get("data_sources", {}).get("crm", {}).get("sf_credentials", {})
            )

    # 1. OAuth Client Credentials Flow (preferred for SSO-enforced orgs).
    consumer_key = os.environ.get(sf_creds.get("consumer_key_env", ""), "")
    consumer_secret = os.environ.get(sf_creds.get("consumer_secret_env", ""), "")
    sf_domain = os.environ.get(sf_creds.get("domain_env", ""), "")
    if consumer_key and consumer_secret and sf_domain:
        access_token, instance_url = _get_sf_oauth_client_credentials_token(
            sf_domain, consumer_key, consumer_secret
        )
        return Salesforce(instance_url=instance_url, session_id=access_token)

    # 2. Per-portco SOAP username/password+token.
    username = os.environ.get(sf_creds.get("username_env", ""), "") if sf_creds else ""
    password = os.environ.get(sf_creds.get("password_env", ""), "") if sf_creds else ""
    token = os.environ.get(sf_creds.get("token_env", ""), "") if sf_creds else ""

    # 3. Global SF_USERNAME/SF_PASSWORD/SF_SECURITY_TOKEN fallback.
    if not username:
        username = os.environ.get("SF_USERNAME", "")
        password = os.environ.get("SF_PASSWORD", "")
        token = os.environ.get("SF_SECURITY_TOKEN", "")

    if username and password:
        return Salesforce(
            username=username,
            password=password,
            security_token=token or "",
        )

    # 4. Global pre-issued bearer token.
    sf_instance = os.environ.get("SF_INSTANCE_URL", "")
    sf_access = os.environ.get("SF_ACCESS_TOKEN", "")
    if sf_instance and sf_access:
        return Salesforce(instance_url=sf_instance, session_id=sf_access)

    raise RuntimeError(
        f"Salesforce credentials not configured for {portco_key or 'default'}. "
        f"Set OAuth Client Credentials (consumer_key_env + consumer_secret_env + "
        f"domain_env in portco_config.json) OR SOAP username/password/token, "
        f"OR global SF_INSTANCE_URL + SF_ACCESS_TOKEN."
    )


def run_sync_session(portco_key: str):
    """Sync all SF data for a portco into a Postgres snapshot via direct REST API.

    Bypasses Managed Agent sessions entirely — no LLM tokens consumed.
    Uses simple_salesforce with native query_more() pagination.
    """
    if not db_adapter.is_db_available():
        log.warning("DB sync skipped — DATABASE_URL not configured")
        return None

    try:
        sf = _get_sf_client(portco_key)
    except RuntimeError as e:
        log.error(f"DB sync skipped — {e}")
        return None

    snapshot_id = db_adapter.create_snapshot(portco_key)
    record_counts = {}

    # Describe each object once up front so we can filter optional custom
    # fields per org. Cached for the lifetime of this sync — no re-describe
    # per page or per object retry.
    describe_cache: dict[str, set[str]] = {}

    for object_type, fields in SYNC_OBJECTS.items():
        table_key = (
            "opportunities"
            if object_type == "Opportunity"
            else object_type.lower() + "s"
        )
        try:
            # Filter optional fields against describe so a missing custom
            # field on one portco does not nuke the whole Lead sync.
            if object_type in OPTIONAL_SYNC_FIELDS:
                if object_type not in describe_cache:
                    describe_cache[object_type] = _describe_object_fields(
                        sf, object_type
                    )
                select_clause, missing = _build_select_clause(
                    object_type, fields, describe_cache[object_type]
                )
                if missing:
                    log.warning(
                        "[lead-sync] %s: dropping %d optional %s field(s) — "
                        "not in describeSObject for this portco: %s. "
                        "Postgres columns will be NULL for these.",
                        portco_key,
                        len(missing),
                        object_type,
                        ", ".join(missing),
                    )
            else:
                select_clause = fields

            records = []
            soql = f"SELECT {select_clause} FROM {object_type}"
            result = sf.query(soql)
            records.extend(result["records"])
            while not result["done"]:
                result = sf.query_more(result["nextRecordsUrl"], identifier_is_url=True)
                records.extend(result["records"])

            log.info(f"Fetched {len(records)} {object_type} records from Salesforce")
            db_adapter.write_records(snapshot_id, portco_key, object_type, records)
            record_counts[table_key] = len(records)
            log.info(f"Synced {len(records)} {object_type} records to Postgres")
        except Exception:
            log.exception(f"Sync failed for {object_type} in {portco_key}")
            record_counts[table_key] = 0

    total = sum(record_counts.values())
    if total == 0:
        log.error(f"Sync produced 0 records for {portco_key}")
        db_adapter.fail_snapshot(snapshot_id)
        send_notification("watch", f"DB sync for {portco_key} produced 0 records")
        return None

    db_adapter.complete_snapshot(snapshot_id, record_counts)
    log.info(f"Sync complete for {portco_key}: {record_counts}")

    # Post-sync retention pipeline (incident 2026-06-16). The snapshot is now
    # known-good, so (1) roll it up into the forever daily_metrics row and
    # (2) archive the full raw rows to Parquet cold storage. Both are
    # best-effort and must never break the sync: a rollup/archive failure
    # just leaves the snapshot un-stamped, which holds its hot rows in
    # Postgres (the purge is gated on both) until the next run succeeds.
    try:
        db_adapter.compute_and_store_daily_metrics(snapshot_id, portco_key)
    except Exception:
        log.exception(f"daily_metrics rollup failed for {portco_key} (non-fatal)")
    try:
        import snapshot_archive

        snap_date = db_adapter.get_snapshot_date(snapshot_id)
        snapshot_archive.archive_snapshot(snapshot_id, portco_key, snap_date)
    except Exception:
        log.exception(f"snapshot archive failed for {portco_key} (non-fatal)")

    failed_objects = [k for k, v in record_counts.items() if v == 0]
    if failed_objects:
        send_notification(
            "watch",
            f"DB sync for {portco_key} partial — failed objects: {', '.join(failed_objects)}",
        )

    return snapshot_id


# --- Ad-hoc Slack questions ---


def _resolve_portco(question: str, channel_id: str = None) -> str:
    """Determine portco from channel or question text."""
    portco_key = None
    if channel_id:
        portco = get_portco_by_channel(channel_id)
        if portco:
            portco_key = portco["key"]
    if not portco_key:
        portco_key = extract_portco_from_question(question)
    return portco_key or "acme"


def _get_db_context(portco_key: str) -> str:
    """Pull summary stats from Railway Postgres if available."""
    if not db_adapter.is_db_available():
        return ""
    try:
        last_sync = db_adapter.get_last_sync(portco_key)
        pipeline = db_adapter.query(
            "SELECT * FROM pipeline_by_stage WHERE portco_key = %s",
            (portco_key,),
        )
        win_rate = db_adapter.query(
            "SELECT * FROM win_rate_by_quarter WHERE portco_key = %s "
            "ORDER BY close_year DESC, close_quarter DESC LIMIT 4",
            (portco_key,),
        )
        parts = [f"\n\nHISTORICAL DB CONTEXT (last sync: {last_sync}):"]
        if pipeline["records"]:
            parts.append("Pipeline by stage:")
            for r in pipeline["records"]:
                parts.append(
                    f"  {r['stage_name']}: {r['opp_count']} opps, ${r['total_amount']:,.0f}"
                )
        if win_rate["records"]:
            parts.append("Win rate by quarter:")
            for r in win_rate["records"]:
                parts.append(
                    f"  Q{int(r['close_quarter'])} {int(r['close_year'])}: {r['win_rate']}% ({r['won_count']} won)"
                )
        parts.append(
            "Use this as baseline context. Query Salesforce MCP for current/detailed data."
        )
        return "\n".join(parts)
    except Exception as e:
        log.debug(f"DB context fetch failed: {e}")
        return ""


def _preprocess_prompt(question: str, portco_key: str) -> dict:
    """Run the question through the Prompt Engineer agent for refinement.

    Returns a dict with five keys:
        ``improved_prompt`` — the refined question with data rules injected,
                              field names corrected, and output format
                              specified (string).
        ``summary``         — one-sentence echo of what was asked (string).
        ``plan_steps``      — list of investigation steps the bot will run
                              (list of strings).
        ``expected_output`` — short description of what the user will see
                              when the investigation completes (string).
        ``risk_flags``      — list of caveats / data-quality concerns
                              relevant to this question (list of strings).

    Returns ``{"improved_prompt": question}`` (other fields blank) if
    preprocessing is unavailable, fails, or times out (30s) — callers
    must tolerate the partial shape and fall back to the original
    question for the investigation while keeping any partial ack copy
    they have.
    """
    fallback = {
        "improved_prompt": question,
        "summary": "",
        "plan_steps": [],
        "expected_output": "",
        "risk_flags": [],
    }
    if not PROMPT_ENGINEER_ID:
        return fallback

    try:
        # Plan #44 Task #9: pin the Prompt Engineer agent version. Plan #44
        # Task #13 decision row #12 explicitly excludes single-turn agents
        # from session-level ``instructions`` — Prompt Engineer is single-
        # turn (one request, one JSON response), so no instructions= here.
        session = client.beta.sessions.create(
            agent=_resolve_agent_param(PROMPT_ENGINEER_ID),
            environment_id=ENVIRONMENT_ID,
            title=sanitize_session_title(f"Prompt preprocessing: {question}"),
            resources=[
                {
                    "type": "memory_store",
                    "memory_store_id": HEALTH_STORE_ID,
                    "access": "read_only",
                    "instructions": (
                        f"Read /{portco_key}/instructions.md for data rules, field "
                        "corrections, and standing instructions that must be injected "
                        "into the improved prompt."
                    ),
                },
            ],
        )
        log.info(f"Prompt Engineer session created: {session.id}")

        # Stream with a 30-second timeout
        text_parts = []
        deadline = time.monotonic() + 30

        with client.beta.sessions.events.stream(session_id=session.id) as stream:
            client.beta.sessions.events.send(
                session_id=session.id,
                events=[
                    {
                        "type": "user.message",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    f"Portco: {portco_key}\n\n"
                                    f"User question: {question}\n\n"
                                    "Read /{portco_key}/instructions.md from the memory store and"
                                    " return a JSON object with the following keys (ALL required,"
                                    " no extra text):\n\n"
                                    "- improved_prompt (string): the refined question with data"
                                    " rules injected, field names corrected, and output format"
                                    " specified. This is what the investigation runs against.\n"
                                    "- summary (string, ≤140 chars): one-sentence restatement of"
                                    " what the user asked, in plain English.\n"
                                    "- plan_steps (array of 2-5 strings): the steps the agent will"
                                    " take, e.g. ['Pull customer list grouped by Product__c from"
                                    " Account', 'Map each product key to its release date and"
                                    " version', 'Render a 3-column table']. Make each step concrete,"
                                    " naming the SF object/table when relevant.\n"
                                    "- expected_output (string, ≤200 chars): description of what"
                                    " the user will see when the investigation completes (e.g."
                                    " 'a Slack table + .xlsx attachment with product, customer"
                                    " count, version, last release date').\n"
                                    "- risk_flags (array of strings, can be empty): caveats or"
                                    " data-quality concerns (e.g. 'Product__c is sparsely populated"
                                    " before 2024', 'last release date depends on the"
                                    " Release_History__c picklist which may lag actual GA').\n\n"
                                    "Return ONLY the JSON object, no other text."
                                ),
                            }
                        ],
                    }
                ],
            )

            for event in stream:
                if time.monotonic() > deadline:
                    log.warning(
                        "Prompt Engineer timed out after 30s — using original question"
                    )
                    break

                if event.type == "agent.message":
                    for block in event.content:
                        if hasattr(block, "text") and block.text:
                            text_parts.append(block.text)

                elif event.type == "session.status_idle":
                    break

                elif event.type == "session.status_rescheduled":
                    # Transient retry — keep waiting on the same stream.
                    # See _stream_and_handle for the full doc-cite.
                    log.info(
                        f"Prompt Engineer session {session.id} rescheduled "
                        "by Anthropic (transient retry). Continuing event loop."
                    )

                elif event.type in ("session.error", "session.status_terminated"):
                    log.warning(f"Prompt Engineer session ended: {event.type}")
                    break

        # Archive regardless of outcome
        _archive_session(session.id)

        if not text_parts:
            log.warning("Prompt Engineer returned no output — using original question")
            return fallback

        raw_output = "".join(text_parts).strip()

        # Parse the JSON response
        try:
            # Handle cases where the model wraps JSON in markdown code fences
            cleaned = raw_output
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[-1]  # drop first ``` line
                if cleaned.endswith("```"):
                    cleaned = cleaned[: -len("```")]
                cleaned = cleaned.strip()

            parsed = json.loads(cleaned)
            improved = (parsed.get("improved_prompt") or "").strip()
            if not improved:
                log.warning(
                    "Prompt Engineer JSON missing 'improved_prompt' key — using original"
                )
                return fallback

            log.info(
                f"Prompt Engineer refined question ({len(question)} -> {len(improved)} chars)"
            )
            # Coerce non-string fields into the documented shape so downstream
            # consumers (ack renderer, Coordinator user-message body) can rely
            # on it without per-call type checks.
            plan_steps = parsed.get("plan_steps") or []
            if isinstance(plan_steps, str):
                plan_steps = [plan_steps]
            risk_flags = parsed.get("risk_flags") or []
            if isinstance(risk_flags, str):
                risk_flags = [risk_flags]
            return {
                "improved_prompt": improved,
                "summary": (parsed.get("summary") or "").strip(),
                "plan_steps": [str(s).strip() for s in plan_steps if s][:5],
                "expected_output": (parsed.get("expected_output") or "").strip(),
                "risk_flags": [str(s).strip() for s in risk_flags if s][:5],
            }
        except (json.JSONDecodeError, AttributeError) as e:
            log.warning(
                f"Prompt Engineer output not valid JSON ({e}) — using original question"
            )
            return fallback

    except Exception as e:
        log.warning(f"Prompt preprocessing failed: {e} — using original question")
        return fallback


def _build_adhoc_prompt(
    question: str, portco_key: str, response_shape: str = None
) -> str:
    """Build the user message for an ad-hoc MCP session.

    Routing rule (Plan: historical-pulls-via-postgres, 2026-05-11):
      - Historical data on standard fields (>24h old, no custom fields) →
        prefer ``db_query`` against Railway Postgres. The snapshot covers
        the standard columns listed in db_schema.sql.
      - Same-day / freshness-anchored queries → use Salesforce MCP.
      - Any list-pull expected to return >500 rows → MUST use the
        streaming Python-script-to-xlsx pattern, regardless of source.
        Never accept full record bodies into the agent context.

    ``response_shape`` is the Prompt Engineer's classification (PR 5).
    When set to ``hybrid_data_synthesis``, we inject an explicit
    reminder of the validation-pipeline contract directly into the
    kickoff so the data-pull-only shortcut cannot fire on a
    mixed-intent question. The Coordinator's own system prompt
    already mandates the steps; this hint makes the binding visible
    in the first user turn so it shows up in the cached context.
    """
    db_context = ""
    is_same_day = db_adapter.needs_same_day_data(question)
    if not is_same_day:
        db_context = _get_db_context(portco_key)

    # Synced fields (db_schema.sql + db_adapter.write_records + SYNC_OBJECTS
    # SELECT clauses above) — the authoritative list of what Postgres has.
    # Custom fields persisted: Lead.funnel_stage / mql_date / sql_date;
    # Account.customer_tier / contract_status / region / arr. Anything not
    # in that list is MCP-only.
    routing_block = (
        "DATA-SOURCE ROUTING (read first, choose the right path):\n"
        "- For HISTORICAL data >24h old on synced fields (Lead.Status/Source/Owner/"
        "CreatedDate/ConvertedDate/IsConverted/Funnel_Stage__c/MQL_SDR_Accepted_Date_Time__c/"
        "SDR_Qualified_Date_Time__c; Opportunity.Stage/Amount/CloseDate/CreatedDate/"
        "LastActivityDate/LastModifiedDate/IsClosed/IsWon/RecordType/Probability/"
        "FiscalQuarter/FiscalYear/AccountId; Account.RecordType/Industry/BillingCountry/CreatedDate/"
        "Customer_Tier__c/Contract_Status__c/Region__c/ARR__c; Contact.Email/Title/"
        "Account/LeadSource/CreatedDate/LastActivityDate), use the db_query custom tool "
        "against Railway Postgres. It is faster, cheaper, and does NOT stream rows "
        "through your context window.\n"
        "- For SAME-DAY/live data, OR queries that need custom fields NOT in the Postgres "
        "schema (Discovery_Call_Booked__c, Vertical__c, and any other portco-specific "
        "custom field absent from db_schema.sql), use Salesforce MCP (soqlQuery, "
        "describeSObject).\n"
        "- For ANY list-pull >500 rows (regardless of source): NEVER load full rows into context. "
        "Write a Python script to /mnt/session/outputs/<name>.py that pages through the data "
        "(soqlQuery + nextRecordsUrl, OR db_query with LIMIT/OFFSET) and appends each batch "
        "directly to /mnt/session/outputs/<name>.xlsx via openpyxl. The xlsx is auto-uploaded "
        "to Slack. The 1M-token cap will terminate the run before a 3000-row pull finishes "
        "if you stream JSON back into the conversation.\n"
        f"- needs_same_day_data classification for this question: {'same-day' if is_same_day else 'historical'}.\n\n"
    )

    # PR 5: Prompt Engineer's response_shape classification — when set,
    # name it explicitly so the Coordinator binds to the right
    # validation branch on the very first turn (the system prompt
    # describes the binding; this prepends a concrete hint to the
    # user-side message where the agent can't miss it). For
    # hybrid_data_synthesis specifically, the full Adversarial +
    # Statistician + Writing-Agent-delegation pipeline is mandatory.
    shape_hint = ""
    if response_shape:
        shape_hint = (
            f"Prompt Engineer classified this question's response_shape as "
            f"`{response_shape}`. "
        )
        if response_shape == "hybrid_data_synthesis":
            shape_hint += (
                "This is a hybrid data-pull + analytical-enrichment + "
                "prose-synthesis question. The full validation pipeline "
                "is MANDATORY before post_report: Adversarial Reviewer "
                "review of every finding, Statistician validation of "
                "every quantitative claim, Writing Agent delegation "
                "for the user-facing narrative. The data-pull-only "
                "shortcut is forbidden — even if every row is correct, "
                "skipping Adversarial Reviewer or Statistician is a "
                "wrong answer. Carry the full underlying rows in an "
                ".xlsx attachment alongside the prose."
            )
        shape_hint += "\n\n"

    return (
        f'A team member asked: "{question}"\n\n'
        f"Portco: {portco_key}\n\n"
        f"{shape_hint}"
        f"BEFORE DOING ANYTHING: Read /{portco_key}/instructions.md from the memory store. "
        f"It contains mandatory data rules — which fields to use, what to exclude, how to segment. "
        f"Violating these rules produces wrong numbers.\n\n"
        f"{routing_block}"
        f"Investigate using the right data source per the routing block above. Follow this loop:\n"
        f"1. Discover schema if needed (describeSObject for SF, or db_schema.sql columns for Postgres)\n"
        f"2. Plan queries; for list-pulls >500 rows, write a streaming xlsx script BEFORE running the full query\n"
        f"3. Validate results — if a query returns 0 rows or errors, fix and retry\n"
        f"4. Check for anomalies — if data looks incomplete, run follow-up queries\n"
        f"5. Analyze the data thoroughly with specific numbers\n"
        f"6. Call post_report exactly once with the final structured payload.\n\n"
        f"SOQL RULES:\n"
        f"- CloseDate = DATE only (2024-01-01, no T/Z)\n"
        f"- CreatedDate = DATETIME (2024-01-01T00:00:00Z)\n"
        f"- No CASE, COALESCE, FLOOR, or subqueries in SELECT\n"
        f"- No column aliases in ORDER BY — use the aggregate function\n"
        f"- Use CALENDAR_YEAR()/CALENDAR_QUARTER() for time grouping\n"
        f"- THIS_YEAR/LAST_YEAR are valid; THIS_QUARTER/NEXT_QUARTER are NOT\n"
        f"- Long text/textarea fields (Vertical__c, Customer_Tier__c, Region__c) CANNOT be used in GROUP BY — use MIN/MAX/COUNT instead or filter with WHERE\n"
        f"- COUNT/SUM/AVG aggregate queries CANNOT use LIMIT — remove LIMIT from non-grouped aggregate queries\n"
        f"- Use GROUP BY for aggregate analysis, not individual records\n"
        f"- Add LIMIT 20 if pulling individual records (but NOT with aggregate functions)\n"
        f"- RecordType.Name = 'New Business' for new business opps\n"
        f"- Filter CreatedDate >= 2024-01-01T00:00:00Z — data before 2024 is unreliable\n\n"
        f"FINAL OUTPUT:\n"
        f"- Call post_report exactly once at the end of your investigation. Do NOT use send_slack_notification for findings.\n"
        f"- For simple single-fact lookups: response_type='quick_answer' with {{metric, value, as_of, source}}.\n"
        f"- For investigations with multiple findings: response_type='ad_hoc_investigation_result' with {{headline, key_metrics[≤5], findings[≤4], cross_domain_pattern?, open_questions[≤3], methodology_note?}}.\n"
        f"- send_slack_notification is reserved for content-free progress updates only (no numbers, no findings).\n"
        f"- Field length caps are enforced by the orchestrator — extra-long strings are rejected. Be specific and tight.\n"
        f"- The renderer handles all Slack mrkdwn formatting. Emit plain text strings — no asterisks, dashes, pipes, or other formatting tokens.\n"
        f"- Numbers with commas (1,234), percentages with 1 decimal (42.3%).\n"
        f"{db_context}"
    )


def run_adhoc_mcp_session(
    question: str,
    user_id: str,
    thread_ts: Optional[str] = None,
    channel_id: Optional[str] = None,
    already_preprocessed: bool = False,
    existing_inv_id: Optional[int] = None,
    verbosity: str = "summary",
    event_ts: Optional[str] = None,
    response_shape: Optional[str] = None,
):
    """Run an ad-hoc question as a managed agent session with SF MCP.

    Reuses the existing session if this is a follow-up in the same Slack thread.
    Set already_preprocessed=True if the question was already refined by the
    Prompt Engineer (avoids double-preprocessing).

    ``response_shape`` is the Prompt Engineer's classification (PR 5,
    hybrid_data_synthesis). When set, ``_build_adhoc_prompt`` adds an
    explicit hint to the Coordinator kickoff so mixed-intent questions
    take the validation-pipeline branch.
    Set existing_inv_id to reuse an existing investigations row (recovery path).

    verbosity controls post_report rendering:
        "summary" (default)  — headline + one-line evidence + footer hint
        "expanded"           — all schema fields
    Per-message: each Slack message's verbosity is set independently, no
    inheritance from prior messages in the same thread. To get an expanded
    response, the user prefixes that specific message with `expand:`.

    event_ts is the timestamp of the user's original Slack message. When
    set (alongside channel_id) the lifecycle reaction emoji (👁 → ⏰ →
    ✅/❌) is flipped on that message as the investigation moves through
    its phases. Cron-style callers leave it None and the reactions are
    skipped — they never have a triggering user message to react to.
    """
    portco_key = _resolve_portco(question, channel_id)

    existing_session_id = None
    # Multi-portco safety: lookups MUST be scoped by channel_id. A bare
    # ``thread_ts`` lookup would collide across channels (Slack timestamps
    # are channel-scoped). When channel_id is missing — cron-flow callers,
    # legacy entry points — skip the thread-session reuse entirely and
    # mint a fresh session instead of risking a wrong-portco resume.
    thread_key = (channel_id, thread_ts) if (thread_ts and channel_id) else None
    if thread_key:
        cached_version: Optional[str] = None
        with _thread_sessions_lock:
            existing_session_id = _thread_sessions.get(thread_key)
            if existing_session_id:
                cached_version = _thread_session_versions.get(thread_key)
        # If the in-memory cache is missing the version stamp (e.g. row
        # came from a pre-PR8 process), fall through to the DB lookup
        # below — the DB read returns the stored ``config_version`` and
        # restores both maps in lock-step.
        if existing_session_id and cached_version is None:
            existing_session_id = None
        if not existing_session_id:
            record = db_adapter.get_thread_session_record(thread_ts, channel_id)
            if record:
                db_session_id, db_version = record
                existing_session_id = db_session_id
                cached_version = db_version
                with _thread_sessions_lock:
                    _thread_sessions[thread_key] = existing_session_id
                    _thread_session_versions[thread_key] = cached_version
                log.info(
                    f"Restored session {existing_session_id} from DB for "
                    f"channel={channel_id} thread={thread_ts}"
                )

        # Prompt-deploy invalidation (Plan #44 PR 8). The Coordinator's
        # ``multiagent.agents`` roster pins each sub-agent at its
        # version-at-update time — pins are snapshotted, not live. So a
        # prompt deploy after this session was minted strands the
        # cached session on stale sub-agent prompts. Compare the row's
        # ``config_version`` (16-char sha256 of
        # ``agents/active_versions.json`` when the row was written) to
        # the live value; on mismatch archive the dead session,
        # drop the cache rows, Slack-notify the thread, and fall
        # through to the fresh-session branch below.
        #
        # Fail-closed (codex P2, 2026-05-14): when
        # ``current_config_version()`` returns ``None`` — pin file
        # missing/unreadable — we treat the live version as "unknown"
        # and rotate anyway. The opposite policy (skip rotation when
        # current is unknown) would let stale or un-stamped sessions
        # survive precisely when the prompt-deploy state cannot be
        # verified, defeating the whole guard.
        current_version = db_adapter.current_config_version()
        if existing_session_id and (
            current_version is None or cached_version != current_version
        ):
            log.info(
                "Rotating stale Coordinator session %s for channel=%s thread=%s "
                "(cached config_version=%s != current=%s) — prompt deploy "
                "invalidated the cached session",
                existing_session_id,
                channel_id,
                thread_ts,
                cached_version,
                current_version,
            )
            _archive_and_invalidate_session(
                existing_session_id,
                thread_ts=thread_ts,
                channel_id=channel_id,
            )
            with _thread_sessions_lock:
                _thread_session_versions.pop(thread_key, None)
            existing_session_id = None
            # Best-effort thread notice so the user knows why the next
            # response opens a new context. Failure to post never blocks
            # the investigation — Slack outages must not stall the bot.
            try:
                send_notification(
                    severity="info",
                    summary=(
                        ":arrows_counterclockwise: New conversation started for this "
                        "thread — the agent's prompts were just updated."
                    ),
                    reply_to=thread_ts,
                    channel=channel_id,
                )
            except Exception:
                log.exception(
                    "Failed to post prompt-deploy rotation notice to thread %s",
                    thread_ts,
                )

    inv_id = existing_inv_id

    if existing_session_id:
        log.info(f"Continuing session {existing_session_id} for thread {thread_ts}")
        # Plan #48 / Plan #52 PR-D: re-evaluate split preference on each follow-up.
        _register_split_files_pref(existing_session_id, _detect_split_files(question))
        # Thread-follow-up reuses the existing session — there's no fresh
        # session create boundary, but the user did post a new message and
        # got 👁 on it. Flip to ⏰ immediately so the indicator matches
        # the rest of the lifecycle (👁 → ⏰ → ✅/❌). Safe even when the
        # remove fails (e.g. earlier 👁 add was rate-limited) — Slack
        # silently no-ops missing reactions.
        if event_ts and channel_id:
            transition_reaction(
                channel_id,
                event_ts,
                remove=REACTION_RECEIVED,
                add=REACTION_WORKING,
            )

        # Mint a new investigation row for this follow-up message (codex
        # Option A, 2026-05-13). Pre-fix the existing-session-reuse branch
        # inherited inv_id from the caller (typically None for thread
        # follow-ups), and `terminalize_lifecycle(...)` was called with
        # inv_id=None — the Slack reaction flipped correctly (event_ts +
        # channel_id were threaded) but the investigations DB row was
        # never updated, leaving the analytics-lie window open. Live
        # incident: sesn_EXAMPLE (2026-05-13 05:15 UTC).
        #
        # One row per Slack user message keeps lifecycle semantics
        # monotonic (no un-terminalizing an already-completed row) and
        # gives /cost a clean per-question breakdown.
        if not inv_id:
            inv_id = db_adapter.create_investigation(
                question=question,
                thread_ts=thread_ts,
                channel_id=channel_id,
                user_id=user_id,
                portco_key=portco_key,
                container_id=CONTAINER_ID,
                event_ts=event_ts,
            )

        # Atomic queued→running transition (codex P2, 2026-05-13).
        # The row was just inserted as 'queued'. If a user sent /stop
        # or in-thread cancel in the millisecond between create_investigation
        # and now, the row could already be 'cancelled'. An unconditional
        # update to 'running' would re-open the cancelled row and the
        # stream would proceed — the user's stop intent would be silently
        # lost. transition_queued_to_running uses WHERE status='queued'
        # and returns False if the row is no longer queued, in which
        # case we bail out of the stream entirely.
        if inv_id:
            won = db_adapter.transition_queued_to_running(inv_id, existing_session_id)
            if not won:
                log.info(
                    "Thread follow-up inv_id=%s was cancelled between insert "
                    "and run — skipping stream",
                    inv_id,
                )
                return None

        # Wrap _stream_and_handle in the guarded runner (codex follow-up,
        # 2026-05-13). The fresh-session branch already wraps; the reuse
        # branch did not. Without this, an uncaught exception between
        # the ⏰ flip and the stream return leaves the new investigation
        # row in 'running' and the emoji stuck on ⏰.
        from lifecycle import _run_investigation_guarded

        # Plan #47 Workstream A: _FollowupBlocked is caught OUTSIDE the
        # guarded runner so the lifecycle guard does not fire
        # terminalize(TERMINAL_FAILURE) — the user's follow-up keeps 👁
        # rather than ❌. Any non-400 error still flows through the guard
        # and terminates as TERMINAL_FAILURE (existing behavior).
        try:
            text_parts, delivery_state, error_type, soql_queries = (
                _run_investigation_guarded(
                    inv_id,
                    event_ts,
                    channel_id,
                    _stream_and_handle,
                    existing_session_id,
                    send_events=[
                        {
                            "type": "user.message",
                            "content": [{"type": "text", "text": question}],
                        }
                    ],
                    thread_ts=thread_ts,
                    verbosity=verbosity,
                    portco_key=portco_key,
                    user_id=user_id,
                    event_ts=event_ts,
                    channel_id=channel_id,
                    inv_id=inv_id,
                    followup_mode=True,
                )
            )
        except _FollowupBlocked as e:
            log.warning(
                "[FOLLOWUP_PREFLIGHT_400] session=%s thread=%s error=%s",
                existing_session_id,
                thread_ts,
                e.original_error,
            )
            _handle_followup_blocked(
                session_id=existing_session_id,
                thread_ts=thread_ts,
                channel_id=channel_id,
                user_id=user_id,
                event_ts=event_ts,
                inv_id=inv_id,
                error=e.original_error,
            )
            return None
    else:
        log.info(f"New MCP session for: {question[:80]} (portco={portco_key})")
        is_simple = _is_simple_lookup(question)
        agent_id = QUICK_AGENT_ID if is_simple else COORDINATOR_ID

        if not inv_id:
            inv_id = db_adapter.create_investigation(
                question=question,
                thread_ts=thread_ts,
                channel_id=channel_id,
                user_id=user_id,
                portco_key=portco_key,
                container_id=CONTAINER_ID,
                event_ts=event_ts,
            )

        effective_question = question
        prompt_plan: dict = {}
        if not already_preprocessed:
            # Run Prompt Engineer for BOTH Quick Answer and Coordinator paths
            # so every question gets a rich ack — the previous gate
            # (``not is_simple``) skipped Quick Answer entirely, which left
            # short / single-fact questions with no acknowledgment at all
            # while the lookup ran. Cost is acceptable: Sonnet, single
            # turn, ~$0.002 / call.
            prompt_plan = _preprocess_prompt(question, portco_key)
            improved = prompt_plan.get("improved_prompt") or question
            if improved != question:
                log.info("Using Prompt Engineer preprocessed question")
                effective_question = improved
            # Post the rich ack BEFORE we open the downstream agent
            # session. If the session create fails, the user has still
            # seen the bot restate their question + plan, so they know
            # the bot understood. Best-effort — a failed ack never
            # blocks the investigation.
            try:
                from slack_bot import post_rich_ack

                post_rich_ack(
                    prompt_plan=prompt_plan,
                    question=question,
                    thread_ts=thread_ts,
                    channel_id=channel_id,
                )
            except Exception:
                log.exception("post_rich_ack failed — continuing with investigation")

        # Plan #44 Task #9 — pin the resolved agent version.
        # Anthropic SDK 0.100.0+ removed the ``instructions=`` kwarg on
        # Sessions.create (live incident 2026-05-13 — every adhoc
        # Coordinator session crashed with TypeError). For the multi-turn
        # Coordinator path the standing-rules text now folds into the
        # first user.message body via ``_prepend_session_instructions``
        # below. Quick Answer is single-turn and never carried these
        # instructions even before the SDK change.
        session = client.beta.sessions.create(
            agent=_resolve_agent_param(agent_id),
            environment_id=ENVIRONMENT_ID,
            title=sanitize_session_title(f"Ad-hoc: {question}"),
            vault_ids=VAULT_IDS,
            resources=MEMORY_RESOURCES,
        )
        log.info(f"Ad-hoc MCP session created: {session.id} (agent={agent_id})")
        # Plan #48 / Plan #52 PR-D: record split preference for this session.
        _register_split_files_pref(session.id, _detect_split_files(question))

        # Lifecycle reaction: 👁 → ⏰ once the agent session is live. The
        # session create call is the right boundary because that's when the
        # bot is actually "working" — preprocessing alone doesn't justify
        # the working indicator. Both calls swallow exceptions internally;
        # a failed transition never blocks the investigation.
        if event_ts and channel_id:
            transition_reaction(
                channel_id,
                event_ts,
                remove=REACTION_RECEIVED,
                add=REACTION_WORKING,
            )

        db_adapter.update_investigation(inv_id, "running", session_id=session.id)

        if thread_ts and channel_id:
            save_key = (channel_id, thread_ts)
            save_version = db_adapter.current_config_version()
            with _thread_sessions_lock:
                _thread_sessions[save_key] = session.id
                _thread_session_versions[save_key] = save_version
                while len(_thread_sessions) > _THREAD_SESSION_MAX:
                    evicted = next(iter(_thread_sessions))
                    _thread_sessions.pop(evicted, None)
                    _thread_session_versions.pop(evicted, None)
            db_adapter.save_thread_session(
                thread_ts, session.id, portco_key, channel_id=channel_id
            )

        prompt = _build_adhoc_prompt(
            effective_question, portco_key, response_shape=response_shape
        )
        if not is_simple:
            # Fold the standing rules into the first user.message body
            # (SDK no longer accepts ``instructions=`` — see comment above
            # at sessions.create).
            prompt = _prepend_session_instructions(
                prompt,
                portco_key=portco_key,
                channel_id=channel_id,
            )

        # Tier B Compresr opt-in (CLAUDE.md "Prompt compression" section).
        # The kickoff text becomes the first user.message body — when it
        # carries a pasted CSV or report it's often the biggest single
        # payload of the session. compress_prompt is gated on the
        # COMPRESS_ADHOC_KICKOFF env flag (default false) AND a 4 KB
        # floor. The ``>=`` check intentionally matches the ``< min_chars``
        # short-circuit inside compress_prompt — same threshold, same
        # semantics, no off-by-one between the two layers. The outer
        # guard saves a function call + try/except on the short-prompt
        # hot path. latte_v1 is the right model because the user's
        # request is the natural relevance anchor. Pass
        # effective_question as the query: it's the same text that
        # _build_adhoc_prompt fed into ``prompt``, so the anchor
        # matches the content. Using the raw question instead would
        # mis-anchor whenever the Prompt Engineer rewrote it.
        # compress_prompt silently falls back to the original text on
        # any failure (missing key, SDK error, regression-guard
        # tripped, or negative-ROI result), mirroring the defensive
        # pattern in self_heal._analyze_session and
        # self_improve._analyze_changes.
        if _config.COMPRESS_ADHOC_KICKOFF and len(prompt) >= 4096:
            original_chars = len(prompt)
            try:
                compressed_prompt = compress_prompt(
                    prompt,
                    model="latte_v1",
                    query=effective_question,
                    call_site="adhoc_kickoff",
                    min_chars=4096,
                )
            except Exception:
                # Defensive: compress_prompt is documented not to raise,
                # but if it ever does we keep the original text rather
                # than breaking the kickoff path. Mirrors the silent-
                # fallback contract used at the other call sites.
                log.exception(
                    "compress_prompt raised for call_site=adhoc_kickoff — "
                    "falling back to original prompt"
                )
                compressed_prompt = prompt
            # Guard against an empty/None result and against a worse
            # result. compress_prompt already enforces both internally
            # but a belt-and-braces check here keeps the call-site
            # contract obvious: we never SEND a worse prompt than what
            # we built.
            if (
                isinstance(compressed_prompt, str)
                and compressed_prompt
                and len(compressed_prompt) < original_chars
            ):
                ratio = len(compressed_prompt) / original_chars
                log.info(
                    "Compresr adhoc_kickoff: %d -> %d chars (ratio=%.3f)",
                    original_chars,
                    len(compressed_prompt),
                    ratio,
                )
                prompt = compressed_prompt

        # Wrap _stream_and_handle in the guarded runner so any uncaught
        # exception between session create and stream return flips ❌
        # on the user's message + marks the investigation failed. Pre-
        # refactor this window was unguarded — an exception inside
        # _stream_and_handle bubbled to main._run_investigation and was
        # only logged, leaving the row in 'running' and the emoji stuck
        # on ⏰ (codex P1 #1, 2026-05-13).
        from lifecycle import _run_investigation_guarded

        text_parts, delivery_state, error_type, soql_queries = (
            _run_investigation_guarded(
                inv_id,
                event_ts,
                channel_id,
                _stream_and_handle,
                session.id,
                send_events=[
                    {
                        "type": "user.message",
                        "content": [{"type": "text", "text": prompt}],
                    }
                ],
                thread_ts=thread_ts,
                verbosity=verbosity,
                portco_key=portco_key,
                user_id=user_id,
                event_ts=event_ts,
                channel_id=channel_id,
                inv_id=inv_id,
            )
        )
        existing_session_id = session.id

    # Centralized lifecycle terminalization (2026-05-13 refactor).
    # All four exit paths (session error, post_report-already-delivered,
    # fallback post_analysis success, no-output) route through
    # ``terminalize_lifecycle`` which atomically updates investigations
    # row status AND flips the Slack reaction in one place. The bare
    # ``transition_reaction`` + ``update_investigation`` pair this
    # replaced left a window where the two could disagree (e.g. crash
    # mid-flow leaving status='running' + emoji=❌ or vice versa).
    from lifecycle import DeliveryState, terminalize_lifecycle

    if error_type:
        if thread_ts and channel_id:
            with _thread_sessions_lock:
                _thread_sessions.pop((channel_id, thread_ts), None)
                _thread_session_versions.pop((channel_id, thread_ts), None)
        log.error(f"MCP session error: {error_type}")
        terminalize_lifecycle(
            DeliveryState.TERMINAL_FAILURE,
            event_ts=event_ts,
            channel_id=channel_id,
            inv_id=inv_id,
            error_message=error_type,
        )
        raise RuntimeError(f"MCP session failed: {error_type}")

    if delivery_state.is_delivered():
        # ``_dispatch_post_report`` (the post_report path) already called
        # terminalize_lifecycle on the success branch — this call is a
        # no-op via the in-memory idempotency map. For the legacy
        # send_slack_notification path, no internal terminalize fired,
        # so the call below is the load-bearing one. Either way the row
        # is correctly marked completed and the emoji is ✅.
        log.info(
            "Agent posted findings to Slack directly via %s",
            delivery_state.value,
        )
        terminalize_lifecycle(
            delivery_state,
            event_ts=event_ts,
            channel_id=channel_id,
            inv_id=inv_id,
        )
    elif _consume_post_report_cancelled_guard(existing_session_id):
        # Codex P1 (PR #252 follow-up). The cancelled-guard in
        # ``_dispatch_post_report`` short-circuited a post_report call
        # because another path (watchdog Tier 3, /stop, recovery sweep)
        # already terminalized this investigation. That owning path also
        # owns the user-facing terminal Slack notice — the ❌ emoji flip
        # via ``terminalize_lifecycle`` plus, for the watchdog Tier 3
        # case, the explicit in-thread ":x: Investigation stalled..."
        # post (see ``session_watchdog._tier3_terminate``).
        #
        # The legacy fallback below ("Investigation didn't produce a
        # final report" via ``post_analysis``, or the orchestration-
        # chatter watch line, or the bare "Investigation produced no
        # output" watch line) would emit a SECOND contradictory
        # user-facing post on top of that terminal notice — exactly the
        # double-post race the codex P1 finding flagged. Short-circuit
        # here: the row is already terminal, the emoji is already ❌,
        # the user already has the right message.
        log.info(
            "[POST_REPORT_GUARD_FALLBACK_SUPPRESSED] session=%s inv_id=%s — "
            "cancelled-guard fired; the watchdog/(stop)/recovery path that "
            "terminalized this row owns the user-facing terminal notice. "
            "Skipping fallback post_analysis / watch line to avoid a "
            "second contradictory Slack post.",
            existing_session_id,
            inv_id,
        )
        # No terminalize_lifecycle call here — the owning path already
        # made one and the in-memory idempotency map would no-op it
        # anyway. We deliberately do NOT promote ``delivery_state`` to
        # any other value: the audit-log line above is the trail
        # operators can use to verify the suppression fired, and the
        # state stays at ``NOT_DELIVERED`` to reflect the honest fact
        # that the Coordinator's prose never reached the user.
    elif text_parts:
        orchestration_keywords = [
            "dispatched",
            "awaiting",
            "ending turn",
            "specialists",
            "unblocked",
            "blocked on",
        ]
        analysis = "\n\n".join(text_parts)
        orchestration_lines = sum(
            1
            for line in analysis.split("\n")
            if any(kw in line.lower() for kw in orchestration_keywords)
        )
        total_lines = max(len(analysis.split("\n")), 1)

        if orchestration_lines / total_lines > 0.5:
            log.warning(
                "Agent output is mostly orchestration chatter — not posting to Slack"
            )
            send_notification(
                "watch",
                "Investigation completed but produced no user-facing findings. The coordinator may not have reached the report-writing step.",
                reply_to=thread_ts,
            )
            # No user-facing delivery — treat as NO_OUTPUT (❌ on the
            # user's message, status='failed'). Without this the user
            # would see their message stuck on ⏰ while only the admin
            # got the watch line.
            terminalize_lifecycle(
                DeliveryState.NO_OUTPUT,
                event_ts=event_ts,
                channel_id=channel_id,
                inv_id=inv_id,
                error_message="orchestration_chatter_only",
            )
        else:
            # Plan v2 PR 1 (2026-05-14 codex review):
            # We used to fall through here and post the accumulated
            # agent.message transcript as ``post_analysis`` with
            # ``title=question[:100]``. That leaked chain-of-thought
            # to Slack (e.g. "Pipeline Monitor confirmed Kapa is
            # down. Waiting on the statistician's full build.") and
            # used a truncated SOQL draft as the title whenever
            # ``question`` had been overwritten mid-session. Per the
            # 2026-05-14 codex review of the failed Acme
            # opp-analysis investigation, the fix is to NEVER post
            # raw agent text — the Coordinator is supposed to call
            # ``post_report`` after the Writing Agent has composed
            # prose, and silence + a neutral failure line is the
            # right user-facing behavior when it doesn't.
            #
            # We still attach any virtualized files the agent
            # materialized (e.g. Parquet XLSX siblings) so the user
            # gets the raw data even when the narrative failed. The
            # ``_download_session_files`` call below picks up
            # Anthropic-side files; this block handles the local
            # virtualized-files tracker so it doesn't leak across
            # sessions.
            log.warning(
                "Coordinator did not call post_report — emitting "
                "neutral failure line instead of agent transcript "
                "(session=%s)",
                existing_session_id,
            )
            fallback_attachments = _consume_virtualized_files(existing_session_id)
            # Pre-compute which tracked paths will actually upload so
            # the summary's "Attaching N" count matches what the user
            # sees. ``_attach_files_async`` will re-apply the same
            # swap + validation + existence check on its daemon
            # thread; we mirror that logic here just for the count.
            # The Parquet→xlsx swap matters because the agent tracks
            # ``.parquet`` handles but the user wants the ``.xlsx``
            # sibling Slack can open (codex P2 review, 2026-05-14).
            previewed_uploads = [
                p
                for p in (_prefer_xlsx_sibling(fp) for fp in fallback_attachments)
                if _is_safe_attachment_path(p) and Path(p).exists()
            ]
            summary_lines = [
                ":warning: Investigation didn't produce a final report.",
                f"Session ID: `{existing_session_id}`",
            ]
            if previewed_uploads:
                summary_lines.append(
                    f"Attaching {len(previewed_uploads)} raw output "
                    "file(s) the agent had materialized."
                )
            else:
                summary_lines.append(
                    "No output files were materialized before the session ended."
                )
            try:
                post_analysis(
                    title="Investigation incomplete",
                    analysis_text="\n".join(summary_lines),
                    queries=[],
                    reply_to=thread_ts,
                    requester_id=user_id,
                )
            except Exception:
                log.exception(
                    "Neutral fallback post_analysis raised — continuing to file uploads"
                )
            # Hand the full tracked list to the vetted attachment
            # pipeline. It applies _prefer_xlsx_sibling + path
            # validation + existence check identical to what we used
            # above for the count, then post_file on a daemon thread.
            # Sharing the pipeline keeps the Parquet→xlsx swap and
            # the safe-path guard in one place.
            _attach_files_async(fallback_attachments, reply_to=thread_ts)
            # The user's message reaction lands on ❌ because no
            # findings were delivered. ``NO_OUTPUT`` is the right
            # terminal state — DELIVERED_VIA_POST_ANALYSIS would
            # have flipped to ✅ which contradicts the "incomplete"
            # framing of the post we just emitted.
            terminalize_lifecycle(
                DeliveryState.NO_OUTPUT,
                event_ts=event_ts,
                channel_id=channel_id,
                inv_id=inv_id,
                error_message="coordinator_skipped_post_report",
            )
    else:
        send_notification(
            "watch", "Investigation produced no output.", reply_to=thread_ts
        )
        terminalize_lifecycle(
            DeliveryState.NO_OUTPUT,
            event_ts=event_ts,
            channel_id=channel_id,
            inv_id=inv_id,
            error_message="no_output",
        )

    _download_session_files(existing_session_id, reply_to=thread_ts)

    # NOTE: the previous ``db_adapter.update_investigation(inv_id,
    # "completed")`` call that lived here has been removed. The
    # ``terminalize_lifecycle`` calls above own the investigations.status
    # update atomically. Keeping a second update_investigation call
    # would reintroduce the race the refactor was meant to close (status
    # could flip back to 'completed' even after a TERMINAL_FAILURE
    # terminalization).

    # Cost accounting now lives inside ``lifecycle.terminalize_lifecycle``
    # (Theme A, 2026-05-16). Every terminalize_lifecycle call above passed
    # ``inv_id`` and won the DB-side idempotency race, which triggers
    # ``_log_cost_for_terminalized_inv`` → ``_log_session_usage``. Removing
    # the previously-redundant call here saves one ``client.beta.sessions.
    # retrieve`` round-trip per session AND ensures failed sessions
    # (TERMINAL_FAILURE, NO_OUTPUT) ALSO log cost — they used to skip this
    # block entirely because the raise at line 5144 or early return paths
    # never reached it.

    with _thread_sessions_lock:
        still_tracked = (
            (channel_id, thread_ts) in _thread_sessions
            if (thread_ts and channel_id)
            else False
        )
    if not existing_session_id or not still_tracked:
        review_session(existing_session_id, "adhoc")
    return existing_session_id


def run_adhoc_investigation(
    question: str,
    user_id: str,
    thread_ts: str = None,
    channel_id: str = None,
    already_preprocessed: bool = False,
    verbosity: str = "summary",
    event_ts: str = None,
    response_shape: str = None,
):
    """Route ad-hoc questions through MCP session.

    ``event_ts`` threads the user's original Slack message timestamp
    through to ``run_adhoc_mcp_session`` so the lifecycle reaction emoji
    (👁 → ⏰ → ✅/❌) can be flipped on the right message.

    ``response_shape`` is the Prompt Engineer's classification (PR 5).
    When set, ``_build_adhoc_prompt`` injects an explicit hint into the
    Coordinator kickoff so hybrid_data_synthesis questions take the
    Adversarial Reviewer + Statistician + Writing-Agent-delegation
    pipeline.
    """
    log.info(f"Ad-hoc investigation (verbosity={verbosity}): {question[:80]}")
    return run_adhoc_mcp_session(
        question,
        user_id,
        thread_ts,
        channel_id,
        already_preprocessed=already_preprocessed,
        verbosity=verbosity,
        event_ts=event_ts,
        response_shape=response_shape,
    )


def _compute_session_input_side(usage) -> int:
    """Input-side token total for a session usage object (B1).

    Mirrors ``session_watch._compute_input_side``: input + cache_read +
    cache_write_5m. Duplicated here instead of imported because
    ``session_watch`` imports ``slack_bot`` at module load, which we don't
    want to pull into the recovery path. Both implementations stay in sync
    via the ``_extract_usage_parts`` shape.

    Returns 0 on any malformed shape — safest default for the recovery
    decision (treats unknown as "no bloat, resume normally").
    """
    parts = _extract_usage_parts(usage)
    return int(parts["input"]) + int(parts["cache_read"]) + int(parts["cache_write_5m"])


def _archive_and_invalidate_session(
    session_id: str,
    thread_ts: Optional[str] = None,
    channel_id: Optional[str] = None,
) -> None:
    """Archive an Anthropic session and delete its thread→session DB row.

    Called during recovery when the old session is too bloated to safely
    resume. Order matters: archive the Anthropic session first (so retries
    can't double-charge for the same context), then clear the in-memory
    thread map, then delete the DB row. The DB row delete is the critical
    step — without it, ``run_adhoc_mcp_session`` looks up the thread on the
    next call, finds the dead session ID, and "restores" it. That's the
    bug behind ``sesn_EXAMPLE``'s 5.9M-token blowup.

    ``channel_id`` is required to scope the lookup. Without it the delete
    silently no-ops — the alternative (deleting every row matching
    ``thread_ts`` across channels) would risk wiping an unrelated portco's
    session on the next multi-portco rollout.

    All three steps are best-effort and never raise. The recovery loop must
    keep moving even if one step fails.
    """
    try:
        # SDK reality: session archive is at client.beta.sessions.archive (NOT
        # client.beta.agents.sessions.archive — that path doesn't exist in
        # anthropic>=0.92). Other call sites in this file (stress_test_*,
        # _archive_session_for_compaction) use the same path.
        client.beta.sessions.archive(session_id)
        log.info("Archived bloated session %s", session_id)
    except Exception as e:
        log.warning("Failed to archive session %s: %s — continuing", session_id, e)

    if thread_ts and channel_id:
        with _thread_sessions_lock:
            _thread_sessions.pop((channel_id, thread_ts), None)
            _thread_session_versions.pop((channel_id, thread_ts), None)
        try:
            db_adapter.delete_thread_session(thread_ts, channel_id)
            log.info(
                "Cleared thread→session DB row for channel=%s thread_ts=%s (was %s)",
                channel_id,
                thread_ts,
                session_id,
            )
        except Exception as e:
            log.warning(
                "Failed to delete thread→session DB row for (%s, %s): %s — continuing",
                channel_id,
                thread_ts,
                e,
            )
    elif session_id:
        # Legacy fallback (codex P2 #3 review). The interrupted investigation
        # is missing ``channel_id`` (NULL on pre-00AH rows), so the
        # composite-key delete cannot fire. The bloated thread_sessions row
        # would otherwise survive archive and re-attach the next master-
        # channel follow-up to the dead session. Sweep by session_id —
        # acceptable scan because the table is small under the 7-day TTL.
        try:
            cleared = db_adapter.delete_thread_session_by_session_id(session_id)
            if cleared:
                log.info(
                    "Cleared %d thread→session DB row(s) by session_id=%s "
                    "(legacy NULL channel path)",
                    cleared,
                    session_id,
                )
        except Exception as e:
            log.warning(
                "Failed to delete thread→session DB row by session_id=%s: %s — continuing",
                session_id,
                e,
            )


_INVESTIGATION_ARCHIVE_TTL_DAYS = 25


def _thread_permalink_for_admin_dm(
    channel_id: Optional[str], thread_ts: Optional[str]
) -> str:
    """Render a Slack permalink for an admin-DM payload — best effort.

    Returns ``<url|thread>`` when ``chat_getPermalink`` succeeds, falls back
    to ``channel/thread_ts`` so the admin still has something they can paste
    into the Slack URL bar. ``(no thread)`` when both args are absent.

    Lazy slack_bot import so test environments that stub slack_bolt still
    see this module load cleanly. Never raises.
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
            "recovery: chat_getPermalink failed for %s/%s — falling back",
            channel_id,
            thread_ts,
        )
    return f"{channel_id}/{thread_ts}"


def _dead_letter_orphan_investigation(inv: dict) -> None:
    """Task #23 — dead-letter an investigation that hit max recovery attempts.

    Mark the row as ``orphan_dead_lettered`` (a new terminal status added
    alongside this function — see migration 00AO). Then DM admins via
    ``send_notification(admin_only=True)`` with the full context so a human
    can decide whether to re-run, hand-craft the answer, or close out.

    Inputs come from a ``get_interrupted_investigations`` row dict: id,
    session_id, question, thread_ts, channel_id, error_message, etc.

    Ordering: this helper is called BEFORE ``terminalize_lifecycle`` in
    ``recover_interrupted_investigations``. The reason: by the time the
    recovery loop runs, ``get_interrupted_investigations`` has already
    flipped the row from ``running`` → ``interrupted``. Calling
    ``terminalize_lifecycle(TERMINAL_FAILURE, ...)`` first would land the
    row in ``failed`` (or be reconciled to ``failed`` by the lifecycle
    reconciliation path), which would then block this helper's
    ``orphan_dead_lettered`` write — leaving the operator with no row
    update AND no admin DM. The earlier review of this PR (2026-05-14)
    caught that production silently dropped the DM on every
    max-recovery-exceeded row because of this ordering.

    Uses ``mark_investigation_orphan_dead_lettered`` (Task #23 helper)
    instead of the generic ``update_investigation_atomic`` because the
    generic helper's ``WHERE status NOT IN`` clause excludes
    ``interrupted`` — which is exactly the state of the row at this point.

    Best-effort end-to-end. The admin DM is the parallel operator-side
    handoff; the user-facing Slack reaction is flipped by the caller via
    ``terminalize_lifecycle`` AFTER this returns.
    """
    inv_id = inv["id"]
    session_id = inv.get("session_id") or "(none)"
    question = inv.get("question") or ""
    thread_ts = inv.get("thread_ts")
    channel_id = inv.get("channel_id")
    recovery_count = inv.get("recovery_count", 0)
    error_message = inv.get("error_message") or "(no error message recorded)"
    portco_key = inv.get("portco_key") or "(none)"

    # 1. Mark the row dead-lettered (atomic — no-op if some other terminal
    #    path already won the race, e.g. /stop landed in the same window).
    try:
        won = db_adapter.mark_investigation_orphan_dead_lettered(
            inv_id,
            error_message=(
                f"max_recovery_attempts_exceeded after {recovery_count} retries; "
                "dead-lettered to admin DM (Task #23)"
            ),
        )
        if not won:
            log.info(
                "Investigation %s already terminal — skipping dead-letter "
                "DM to avoid double-pinging admins",
                inv_id,
            )
            return
    except Exception:
        log.exception("Failed to mark investigation %s orphan_dead_lettered", inv_id)
        # Fall through to the DM anyway — losing the row update is bad,
        # but losing visibility to the human reviewer is worse.

    # 2. DM the admins with everything they need to investigate.
    permalink = _thread_permalink_for_admin_dm(channel_id, thread_ts)
    summary = (
        f"Investigation #{inv_id} dead-lettered after {recovery_count} "
        f"recovery attempts (orphan_dead_lettered)."
    )
    detail = (
        f"*session_id*: `{session_id}`\n"
        f"*thread*: {permalink}\n"
        f"*portco*: {portco_key}\n"
        f"*question*: {question[:500]}\n"
        f"*last error*: {error_message[:500]}"
    )
    try:
        send_notification(
            "watch",
            summary,
            detail=detail,
            admin_only=True,
        )
    except Exception:
        log.exception(
            "Admin DM for orphan_dead_lettered investigation %s failed", inv_id
        )


def recover_interrupted_investigations():
    """Find investigations interrupted by a container restart and resume or restart them.

    Called once at startup. For each interrupted investigation:
    1. Plan #44 Task #18 — if last_activity_at < NOW() - 25 days, mark
       ``archived`` and admin-DM. Anthropic's container checkpoint TTL is
       30 days; the 25-day cutoff leaves safety margin. The investigation's
       /mnt/session/outputs/ is gone, so resume would silently fail.
    2. Try to resume the existing Anthropic session (it may still be alive)
    3. If the session is dead, start a fresh session with the original question
    4. Post a Slack message in the thread explaining the restart
    """
    interrupted = db_adapter.get_interrupted_investigations(CONTAINER_ID)
    if not interrupted:
        log.info("No interrupted investigations to recover")
        return []

    log.info(f"Found {len(interrupted)} interrupted investigation(s) — recovering")
    MAX_RECOVERY_ATTEMPTS = 2
    recovered = []

    # Plan #44 Task #18 — archive aging rows past the 25-day TTL.
    _now = datetime.now(timezone.utc)
    _archive_cutoff = _now - _timedelta(days=_INVESTIGATION_ARCHIVE_TTL_DAYS)

    for inv in interrupted:
        inv_id = inv["id"]
        question = inv["question"]
        thread_ts = inv.get("thread_ts")
        channel_id = inv.get("channel_id")
        user_id = inv.get("user_id")
        old_session_id = inv.get("session_id")
        portco_key = inv.get("portco_key") or "acme"
        recovery_count = inv.get("recovery_count", 0)
        # Use started_at as the activity proxy. The investigations table
        # has no last_activity_at column today; started_at is updated by
        # ``mark_investigation_recovering`` so it tracks the most recent
        # attempt. If the value is missing or unparsable, treat as fresh
        # (don't accidentally archive recent work).
        started_at = inv.get("started_at")
        if started_at and isinstance(started_at, datetime):
            # Compare in UTC; psycopg2 returns aware datetimes from
            # TIMESTAMPTZ columns. If somehow naive, assume UTC.
            sa = (
                started_at
                if started_at.tzinfo
                else started_at.replace(tzinfo=timezone.utc)
            )
            if sa < _archive_cutoff:
                age_days = (_now - sa).days
                log.warning(
                    "Investigation %s last active %d days ago "
                    "(> %d-day TTL) — archiving",
                    inv_id,
                    age_days,
                    _INVESTIGATION_ARCHIVE_TTL_DAYS,
                )
                db_adapter.update_investigation(
                    inv_id,
                    "archived",
                    error_message=(
                        f"auto-archived after {age_days}d idle (Plan #44 Task #18)"
                    ),
                )
                try:
                    send_notification(
                        "watch",
                        f"Investigation #{inv_id} archived after {age_days} "
                        f"days idle (> {_INVESTIGATION_ARCHIVE_TTL_DAYS}-day "
                        "TTL). Anthropic container checkpoint expires at "
                        "30 days; recovery would have silently failed.",
                        detail=(
                            f"question: {question[:200]}\n"
                            f"portco: {portco_key}\n"
                            f"thread_ts: {thread_ts or '(none)'}\n"
                            f"session_id: {old_session_id or '(none)'}"
                        ),
                        admin_only=True,
                    )
                except Exception:
                    log.exception(
                        "Admin DM for archived investigation %s failed",
                        inv_id,
                    )
                continue

        if recovery_count >= MAX_RECOVERY_ATTEMPTS:
            # Task #23 — dead-letter policy. Pre-Task #23 this branch
            # silently marked the row 'failed' (via TERMINAL_FAILURE) and
            # told the user to re-ask. That left orphan sessions sitting
            # in 'running' for days when the recovery attempts themselves
            # had failed silently — exactly what happened to
            # ``sesn_EXAMPLE``. New policy:
            #   (a) mark the row 'orphan_dead_lettered' (new terminal
            #       state, see migration 00AO) so analytics can split
            #       "we gave up and asked a human" from one-shot crashes
            #   (b) DM admins with session_id + thread permalink +
            #       original question + error history so a human can
            #       decide what to do
            #   (c) terminalize lifecycle so the user's Slack message
            #       still flips ⏰ → ❌ (reconciliation path picks up the
            #       just-written orphan_dead_lettered status)
            #   (d) post a single user-facing note in the thread (the
            #       user is still on the hook to re-ask if they want a
            #       fresh run — we promise nothing more)
            # Critically: do NOT silently restart a 3rd time. The loop
            # stops here for this row.
            #
            # Order matters: dead-letter (a)+(b) BEFORE terminalize (c).
            # ``get_interrupted_investigations`` already flipped the row
            # to 'interrupted' a few lines up. If terminalize ran first
            # it would land the row in 'failed' (via reconciliation
            # because the atomic UPDATE excludes 'interrupted'), which
            # would then block the dead-letter UPDATE because 'failed' is
            # in the dead-letter NOT-IN list. The dead-letter helper
            # explicitly accepts ``status IN
            # ('queued','running','interrupted')`` so this ordering lets
            # the row land in 'orphan_dead_lettered' as designed.
            log.warning(
                "Investigation %s hit max recovery attempts (%d) — "
                "dead-lettering to admin",
                inv_id,
                recovery_count,
            )
            _dead_letter_orphan_investigation(inv)

            from lifecycle import DeliveryState, terminalize_lifecycle

            terminalize_lifecycle(
                DeliveryState.TERMINAL_FAILURE,
                event_ts=inv.get("event_ts"),
                channel_id=channel_id,
                inv_id=inv_id,
                error_message="max_recovery_attempts_exceeded",
            )
            if thread_ts:
                send_notification(
                    "watch",
                    f"I was interrupted by a restart and couldn't recover this investigation after {recovery_count} attempts. An admin has been notified. Please re-ask your question if you still need an answer.",
                    reply_to=thread_ts,
                    channel=channel_id,
                )
            continue

        log.info(
            f"Recovering investigation {inv_id}: {question[:80]} (session={old_session_id})"
        )

        if thread_ts:
            send_notification(
                "info",
                "I was interrupted by a container restart. Resuming your investigation now.",
                reply_to=thread_ts,
                channel=channel_id,
            )

        resumed = False
        # Prompt-deploy guard during recovery (codex P2, 2026-05-14). The
        # session's ``multiagent.agents`` roster is snapshotted at session
        # create time — Anthropic does not re-bind pins on resume. So if
        # the live ``config_version`` has shifted since this session was
        # minted, resuming would silently run the OLD sub-agent prompts
        # for the rest of the thread, defeating PR 8's whole purpose.
        # Look up the row's stored stamp and compare. We treat:
        #   - mismatch  → force fresh
        #   - row NULL while live is known → force fresh (un-stamped row,
        #     can't prove the session is current)
        #   - live None → force fresh (fail-closed, same as reuse path)
        recovery_db_version: Optional[str] = None
        if thread_ts and channel_id:
            try:
                _rec = db_adapter.get_thread_session_record(thread_ts, channel_id)
                if _rec and _rec[0] == old_session_id:
                    recovery_db_version = _rec[1]
            except Exception:
                log.debug(
                    "Recovery: thread_session lookup failed for inv_id=%s",
                    inv_id,
                )
        live_cfg_version = db_adapter.current_config_version()
        config_version_stale = (
            live_cfg_version is None or recovery_db_version != live_cfg_version
        )
        if old_session_id:
            try:
                s = client.beta.sessions.retrieve(old_session_id)
                s_status = getattr(s, "status", None)
                # B1 (2026-05-12): if the prior session is alive BUT bloated
                # past the threshold, do NOT resume. Resuming pays the cache-
                # read cost for the entire prior context — see incident
                # ``sesn_EXAMPLE`` (5.9M cached tokens,
                # $13.87 for one recovery session). Archive the dead weight
                # and start fresh.
                input_side = _compute_session_input_side(getattr(s, "usage", None))
                if (
                    s_status in ("idle", "active")
                    and input_side > RECOVERY_FRESH_THRESHOLD
                ):
                    log.warning(
                        "Session %s alive but bloated (input_side=%d > threshold=%d) "
                        "— archiving and starting fresh",
                        old_session_id,
                        input_side,
                        RECOVERY_FRESH_THRESHOLD,
                    )
                    _archive_and_invalidate_session(
                        old_session_id, thread_ts=thread_ts, channel_id=channel_id
                    )
                    # Fall through to the fresh-start branch below.
                elif s_status in ("idle", "active") and config_version_stale:
                    log.warning(
                        "Session %s alive but config_version stale "
                        "(stored=%s != live=%s) — archiving and starting fresh "
                        "instead of resuming stale sub-agent pins",
                        old_session_id,
                        recovery_db_version,
                        live_cfg_version,
                    )
                    _archive_and_invalidate_session(
                        old_session_id, thread_ts=thread_ts, channel_id=channel_id
                    )
                    # Fall through to fresh-start; do NOT stamp the cache
                    # with the live version, the old session is gone.
                elif s_status in ("idle", "active"):
                    log.info(
                        f"Session {old_session_id} still alive (status={s_status}) — resuming"
                    )
                    db_adapter.mark_investigation_recovering(
                        inv_id, container_id=CONTAINER_ID
                    )

                    if thread_ts and channel_id:
                        with _thread_sessions_lock:
                            _thread_sessions[(channel_id, thread_ts)] = old_session_id
                            # Recovery path: this branch only runs when the
                            # row's stored ``config_version`` already
                            # matches the live value (the stale-version
                            # check above forces fresh otherwise). Stamp
                            # the in-memory cache with the live version
                            # so follow-up messages in this thread reuse
                            # the resumed session instead of rotating it.
                            _thread_session_versions[(channel_id, thread_ts)] = (
                                live_cfg_version
                            )

                    # Pull the persisted event_ts off the investigation row
                    # so terminalize_lifecycle on the resumed session can
                    # repair the ORIGINAL Slack message (codex P2 #3,
                    # 2026-05-13). Pre-fix, event_ts was always None here
                    # and the emoji repair was silently skipped.
                    resumed_event_ts = inv.get("event_ts")
                    text_parts, delivery_state, error_type, _ = _stream_and_handle(
                        old_session_id,
                        send_events=[
                            {
                                "type": "user.message",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": (
                                            "The orchestrator was restarted. Please continue where you left off. "
                                            "If you were mid-investigation, finish and post your findings to Slack."
                                        ),
                                    }
                                ],
                            }
                        ],
                        thread_ts=thread_ts,
                        portco_key=portco_key,
                        user_id=user_id,
                        event_ts=resumed_event_ts,
                        channel_id=channel_id,
                        inv_id=inv_id,
                    )

                    if error_type:
                        log.warning(
                            f"Resumed session {old_session_id} hit error: {error_type}"
                        )
                        # Terminalize via lifecycle so the original Slack
                        # message gets ❌ on resume failure. Pre-fix this
                        # path only logged and left the row in 'recovering'.
                        from lifecycle import DeliveryState, terminalize_lifecycle

                        terminalize_lifecycle(
                            DeliveryState.TERMINAL_FAILURE,
                            event_ts=resumed_event_ts,
                            channel_id=channel_id,
                            inv_id=inv_id,
                            error_message=f"resume_failed:{error_type}",
                        )
                    else:
                        from lifecycle import DeliveryState, terminalize_lifecycle

                        if delivery_state.is_delivered():
                            terminalize_lifecycle(
                                delivery_state,
                                event_ts=resumed_event_ts,
                                channel_id=channel_id,
                                inv_id=inv_id,
                            )
                        elif _consume_post_report_cancelled_guard(old_session_id):
                            # Codex P1 (PR #252 follow-up). Mirror the
                            # ``run_adhoc_mcp_session`` suppression branch —
                            # if the cancelled-guard fired on the resumed
                            # session, the owning path (watchdog/(stop)/
                            # recovery) already terminalized the row and
                            # posted the user-facing terminal notice. Skip
                            # the recovery fallback ``post_analysis`` so we
                            # don't emit a second contradictory post on the
                            # same thread.
                            log.info(
                                "[POST_REPORT_GUARD_FALLBACK_SUPPRESSED] "
                                "(recovery resume) session=%s inv_id=%s — "
                                "skipping (Recovered)-titled post_analysis "
                                "because cancelled-guard fired.",
                                old_session_id,
                                inv_id,
                            )
                        elif text_parts:
                            analysis = "\n\n".join(text_parts)
                            post_analysis(
                                title=f"(Recovered) {question[:80]}",
                                analysis_text=analysis,
                                reply_to=thread_ts,
                                requester_id=user_id,
                            )
                            terminalize_lifecycle(
                                DeliveryState.DELIVERED_VIA_POST_ANALYSIS,
                                event_ts=resumed_event_ts,
                                channel_id=channel_id,
                                inv_id=inv_id,
                            )
                        else:
                            terminalize_lifecycle(
                                DeliveryState.NO_OUTPUT,
                                event_ts=resumed_event_ts,
                                channel_id=channel_id,
                                inv_id=inv_id,
                                error_message="no_output_after_resume",
                            )
                        _download_session_files(old_session_id, reply_to=thread_ts)
                        resumed = True
                        recovered.append(inv_id)
                        log.info(f"Successfully resumed investigation {inv_id}")
                else:
                    log.info(
                        f"Session {old_session_id} is {s_status} — cannot resume, will restart"
                    )
                    _archive_and_invalidate_session(
                        old_session_id, thread_ts=thread_ts, channel_id=channel_id
                    )
            except Exception as e:
                log.info(f"Session {old_session_id} not resumable: {e} — will restart")
                # Even on a retrieve error, clear the DB map so the fresh-
                # start branch below isn't undone by the subsequent
                # ``get_thread_session`` lookup inside run_adhoc_mcp_session.
                # channel_id is required by the new helper signature — without
                # it the DB row delete silently skips and the stale session
                # gets restored on the next thread message (codex P2 review).
                _archive_and_invalidate_session(
                    old_session_id, thread_ts=thread_ts, channel_id=channel_id
                )

        if not resumed:
            log.info(f"Starting fresh session for interrupted investigation {inv_id}")
            try:
                db_adapter.mark_investigation_recovering(
                    inv_id, container_id=CONTAINER_ID
                )
                session_id = run_adhoc_mcp_session(
                    question,
                    user_id,
                    thread_ts,
                    channel_id,
                    already_preprocessed=True,
                    existing_inv_id=inv_id,
                )
                recovered.append(inv_id)
                log.info(
                    f"Restarted investigation {inv_id} with new session {session_id}"
                )
            except Exception:
                log.exception(f"Failed to restart investigation {inv_id}")
                db_adapter.update_investigation(
                    inv_id, "failed", error_message="restart failed"
                )
                if thread_ts:
                    send_notification(
                        "watch",
                        "I tried to restart your investigation after a restart but hit an error. Please re-ask your question.",
                        reply_to=thread_ts,
                        channel=channel_id,
                    )

    log.info(
        f"Recovery complete: {len(recovered)}/{len(interrupted)} investigations recovered"
    )
    return recovered
