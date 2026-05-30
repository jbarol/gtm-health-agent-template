"""
GTM Health Agent Orchestrator
1. Slack bot — receives questions and feedback, dispatches investigations
2. Cron scheduler — nightly jobs (self-improve, db sync, forecast, dream)
3. Tool proxy — handles custom tool calls (send_slack_notification, generate_chart, db_query, save_snapshot_batch)
4. Feedback — writes user instructions to portco-scoped memory store
5. /health HTTP endpoint — Z2 deploy verification (`build_commit`, `active_versions`)
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, Future
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable, Optional, cast

import anthropic
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import config
from slack_bot import (
    set_question_handler,
    set_feedback_handler,
    send_notification,
    start_socket_mode,
    stop_socket_mode,
)
from session_runner import (
    run_dream_session,
    run_investigation_session,
    run_adhoc_investigation,
    run_sync_session,
    run_forecast_analysis,
    recover_interrupted_investigations,
    sanitize_session_title,
)
from self_improve import check_for_updates
from prompt_patch_promoter import promote_prompt_patches
from session_watch import check_active_sessions
from db_adapter import is_db_available, ensure_schema
from portco_registry import get_all_portcos

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("orchestrator")

# Raise Python's recursion limit at orchestrator boot. The Anthropic SDK's
# response-parsing code path (anthropic/_models.py:construct_type) recurses
# through deeply-nested response types and can exceed the default 1000-frame
# limit on large sessions. Observed 2026-05-15 on sesn_EXAMPLE
# (inv_id=139, 6.87M cached input) — RecursionError on events.send terminated
# an otherwise-healthy investigation with TERMINAL_FAILURE → ❌ on the user's
# Slack message. The narrow `except RecursionError` in session_runner._stream_and_handle
# is the belt; this is the suspenders.
sys.setrecursionlimit(5000)

memory_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

# ─────────────────────────────────────────────────────────────────────────────
# Readiness gate (Plan #42 PR2)
# ─────────────────────────────────────────────────────────────────────────────
# ``/health`` always returns 200 once this process is up — Railway uses it for
# liveness. ``/ready`` returns 200 only when ``_READY = True``. The pre-deploy
# smoke probe sets ``_READY`` AFTER it has confirmed the basics (BUILD_COMMIT
# match, dump_sf_query, Quick Answer agent). A failing probe leaves ``_READY``
# false; Railway's healthcheckTimeout fires, marks the deploy failed, and
# holds the previous image. Plan #42 decision D4 ("/ready vs /health split").
_READY: bool = False
# Reason returned in the JSON body when ``/ready`` is False. One of:
#   "smoke_probe_pending" — process is up, probe hasn't been invoked yet.
#   "smoke_probe_failed"  — probe ran and failed at least one check.
#   "smoke_probe_disabled" — operator turned the probe off with the env var.
_READY_REASON: str = "smoke_probe_pending"
# Snapshot of the last smoke-probe check_results dict so ``/ready`` can echo
# the failing check back to the caller without re-running the probe.
_READY_CHECK_RESULTS: dict = {}

MAX_CONCURRENT_INVESTIGATIONS = 5
SHUTDOWN_TIMEOUT_SECONDS = 300  # 5 minutes
_executor = ThreadPoolExecutor(
    max_workers=MAX_CONCURRENT_INVESTIGATIONS, thread_name_prefix="investigation"
)
_active_count = 0
_active_lock = threading.Lock()
_shutting_down = False

# Track active futures so we can wait on them during shutdown
_active_futures: dict[Future, str] = {}  # future -> question snippet
_futures_lock = threading.Lock()


def _run_investigation(
    question,
    user_id,
    thread_ts,
    channel_id,
    improved_prompt=None,
    verbosity: str = "summary",
    event_ts: Optional[str] = None,
    response_shape: Optional[str] = None,
):
    """Run a single investigation in the thread pool.

    ``event_ts`` is the Slack timestamp of the user's original message — it
    travels alongside the question so the lifecycle reaction emoji
    (👁 → ⏰ → ✅/❌) can be flipped on the right message as the
    investigation moves through its phases. Optional for cron-style callers
    that have no triggering Slack message.

    ``response_shape`` is the Prompt Engineer's classification (PR 5,
    hybrid_data_synthesis). When set, the Coordinator kickoff names it
    explicitly so mixed-intent questions take the validation-pipeline
    branch instead of the data-pull shortcut.
    """
    global _active_count
    try:
        run_adhoc_investigation(
            improved_prompt or question,
            user_id,
            thread_ts,
            channel_id,
            already_preprocessed=bool(improved_prompt),
            verbosity=verbosity,
            event_ts=cast(str, event_ts),
            response_shape=cast(str, response_shape),
        )
    except Exception as exc:
        log.exception(f"Investigation failed: {question[:80]}")
        # Twelfth lifecycle gap (PR #162 closed 11): when the inner runner
        # raises BEFORE it can mint the investigations row + call
        # terminalize_lifecycle itself, the user's message stays stuck on
        # ⏰ forever. Live repro 2026-05-14 20:17 PT: three consecutive
        # HTTP 500s from Anthropic's POST /v1/sessions blocked Coordinator
        # session creation; the SDK gave up; this except fired; the user
        # got no ❌ reaction and no failure post in Slack.
        #
        # Flip the reaction to ❌ directly here using ``inv_id=None``.
        # terminalize_lifecycle short-circuits the in-memory + DB
        # idempotency checks when inv_id is None and just flips the
        # reaction on ``event_ts`` / ``channel_id``. Bare except by design
        # — this is the last-mile safety net; nothing should escape.
        try:
            from lifecycle import DeliveryState, terminalize_lifecycle

            terminalize_lifecycle(
                DeliveryState.TERMINAL_FAILURE,
                event_ts=event_ts,
                channel_id=channel_id,
                inv_id=None,
                error_message=f"outer_exception:{type(exc).__name__}:{exc}"[:500],
            )
        except Exception:
            log.exception(
                "Outer-exception terminalize_lifecycle failed — user message "
                "will stay stuck on ⏰ (lifecycle invariant broken). "
                "Investigate ASAP."
            )
    finally:
        with _active_lock:
            _active_count -= 1
        log.info(f"Investigation complete — {_active_count} still running")


def _on_future_done(future: Future):
    """Callback to clean up completed futures from the tracking dict."""
    with _futures_lock:
        snippet = _active_futures.pop(future, "<unknown>")
    log.debug(f"Future cleaned up: {snippet}")


def _preprocess_prompt(question: str, portco_key: str = "acme") -> dict | None:
    """Call the Prompt Engineer agent to improve the question and extract a plan.

    Returns a dict with keys: improved_prompt, plan_steps, expected_output,
    summary, risk_flags, response_shape. Returns None if the agent is
    unavailable or fails.

    portco_key MUST be passed — without it the Prompt Engineer's system
    prompt cannot resolve ``/{portco}/instructions.md`` and falls back to
    "no portco context" risk-flagged output. Live repro 2026-05-13 18:03
    PT: session sesn_EXAMPLE burned 45s running
    ``find / -name instructions.md`` because the orchestrator omitted
    the portco context and the health memory store wasn't attached.

    Observability (Plan P2 / sub3 incident 2026-05-19):

    * Every return-None path emits a structured log line with the prefix
      ``[PE_RETURN_NONE:<reason>]`` plus the PE session_id (when available),
      elapsed seconds since session creation, exception type / message
      where applicable, and the first 200 chars of any raw text the model
      produced. The five reasons are: ``agent_unconfigured``,
      ``session_create_failed``, ``empty_text_parts``,
      ``json_parse_failed``, ``invalid_schema``, ``exception``.

    * On transient SDK failures (``APIConnectionError``,
      ``InternalServerError``, ``APITimeoutError``) the call retries once
      after a 1.5 s sleep. ``BadRequestError`` and ``json.JSONDecodeError``
      do NOT retry — they are deterministic failure modes that won't
      change on a second attempt.

    * Every invocation records a row in ``messages_api_calls`` via
      ``cost_collector.track_prompt_engineer_call`` so the operator can
      audit volume + cost even when the function returns None (the
      forensic signal the sub3 incident lacked).
    """
    # Path 1 — agent not provisioned. No session ID, no elapsed time.
    if not config.PROMPT_ENGINEER_ID:
        log.warning(
            "[PE_RETURN_NONE:agent_unconfigured] PROMPT_ENGINEER_ID env var "
            "is empty — preprocess skipped"
        )
        _track_pe_outcome(
            outcome="agent_unconfigured",
            usage=None,
            elapsed_s=0.0,
            portco_key=portco_key,
        )
        return None

    pe_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    # Plan #44 Task #9: pin the Prompt Engineer agent version when the
    # active_versions.json pin file lists it. Plan #44 Task #13 decision
    # row #12: Prompt Engineer is single-turn — no session-level
    # instructions. If the pin file lacks the key (which it does today
    # — the pin will land with Bundle A Task #1), fall back to the bare
    # ID so this call still works.
    _pin_version = config.AGENT_VERSIONS.get("prompt_engineer")
    agent_arg = (
        {"type": "agent", "id": config.PROMPT_ENGINEER_ID, "version": _pin_version}
        if isinstance(_pin_version, int) and config.PROMPT_ENGINEER_ID
        else config.PROMPT_ENGINEER_ID
    )
    # Canonical memory mount path — health store is mounted at
    # ``/mnt/memory/gtm-health-memory/`` (see Coordinator system
    # prompt). Naming the full path here saves the Prompt Engineer
    # ~45 seconds of bash filesystem exploration looking for the
    # file (live trace 2026-05-13 18:33 PT — agent burned 3 bash
    # calls + ~45s after trying ``/mnt/memory/acme/...`` first
    # and failing, before finding the actual path).
    _instr_path = f"/mnt/memory/gtm-health-memory/{portco_key}/instructions.md"
    preprocess_prompt = (
        f"Portco: {portco_key}\n\n"
        f"User question: {question}\n\n"
        f"Read {_instr_path} from the memory store FIRST (exact path, "
        "do NOT explore the filesystem — it carries standing data "
        "rules that may rewrite the question). Then analyze the "
        "question and return ONE JSON object with EXACTLY these "
        "keys (no markdown fences, no commentary around the JSON):\n\n"
        '- "improved_prompt": the original question rewritten for '
        "clarity, with portco-specific data rules from instructions.md "
        "injected verbatim where they apply. Keep the intent identical.\n"
        '- "summary": a one-sentence plain-English summary of what '
        "will be investigated (max 80 chars).\n"
        '- "plan_steps": a list of 2-5 short strings describing what '
        'the agent will do (e.g. "Query pipeline by stage and rep").\n'
        '- "expected_output": a short string describing what the user '
        'will receive (e.g. "Breakdown table, trend chart, and risk '
        'flags").\n'
        '- "risk_flags": a list of strings noting anything ambiguous '
        "or risky about the question. Empty list if none.\n"
        '- "response_shape": one of `one_fact`, `comparative`, `why`, '
        "`briefing`, `table`, `methodology`, `data_pull`, "
        "`hybrid_data_synthesis`. See the response_shape_taxonomy in "
        "your system prompt. Bias toward `hybrid_data_synthesis` on "
        "ambiguous mixed-intent questions — the Coordinator forces "
        "Adversarial Reviewer + Statistician + Writing-Agent-delegation "
        "only when this shape lands.\n\n"
        "Return ONLY the JSON object."
    )

    # ──────────────────────────────────────────────────────────────────
    # Retry contract — single retry on transient SDK errors only.
    #
    # Retry: APIConnectionError, InternalServerError, APITimeoutError.
    # No retry: BadRequestError (same input → same 400) and
    # JSONDecodeError (same prompt → same model drift). Both are
    # deterministic; retrying wastes a second PE call.
    # ──────────────────────────────────────────────────────────────────
    _TRANSIENT = (
        anthropic.APIConnectionError,
        anthropic.InternalServerError,
        anthropic.APITimeoutError,
    )
    _RETRY_BACKOFF_S = 1.5
    _MAX_ATTEMPTS = 2  # 1 original + 1 retry
    session_id: str | None = None
    started_at: float = time.monotonic()
    raw: str = ""
    last_attempt = 0
    text_parts: list[str] = []
    saw_session_error = False
    session_usage = None

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        last_attempt = attempt
        started_at = time.monotonic()
        text_parts = []
        saw_session_error = False
        session_id = None
        session_usage = None
        try:
            # Attach the health memory store so the agent can read
            # /{portco}/instructions.md via the canonical mount path. Without
            # this resource the agent has no way to load standing user rules
            # — see live repro cited in the docstring.
            session = pe_client.beta.sessions.create(
                agent=cast(Any, agent_arg),
                environment_id=config.ENVIRONMENT_ID,
                title=sanitize_session_title(f"Preprocess: {question}"),
                resources=[
                    {
                        "type": "memory_store",
                        "memory_store_id": config.HEALTH_STORE_ID,
                        "access": "read_only",
                        "instructions": (
                            f"Read /{portco_key}/instructions.md for data rules, "
                            "field corrections, and standing instructions that "
                            "must be injected into the improved prompt."
                        ),
                    },
                ],
            )
            # The SDK guarantees session.id is a str on success. We assign it
            # to a local first so the outer-scope ``session_id`` variable
            # narrows from ``str | None`` to ``str`` for the calls below.
            _sid: str = session.id
            session_id = _sid

            with pe_client.beta.sessions.events.stream(session_id=_sid) as stream:
                pe_client.beta.sessions.events.send(
                    session_id=_sid,
                    events=[
                        {
                            "type": "user.message",
                            "content": [{"type": "text", "text": preprocess_prompt}],
                        }
                    ],
                )
                for event in stream:
                    if event.type == "agent.message":
                        for block in event.content:
                            if hasattr(block, "text") and block.text:
                                text_parts.append(block.text)
                    elif event.type == "session.status_idle":
                        break
                    elif event.type == "session.error":
                        saw_session_error = True
                        log.warning(
                            "Prompt Engineer session error during preprocess "
                            f"(session_id={_sid})"
                        )
                        break

            # Successful streaming completion — retrieve usage for the
            # cost ledger before falling through to parsing. A retrieve
            # failure is non-fatal (we just record usage=None).
            try:
                _retrieved = pe_client.beta.sessions.retrieve(_sid)
                session_usage = getattr(_retrieved, "usage", None)
            except Exception:
                session_usage = None
            break  # leave the retry loop on a clean stream

        except _TRANSIENT as exc:
            elapsed = time.monotonic() - started_at
            if attempt < _MAX_ATTEMPTS:
                log.warning(
                    "[PE_RETRY:transient] attempt %d/%d failed "
                    "(session_id=%s, elapsed=%.2fs, exc=%s: %s) — sleeping %.2fs "
                    "before retry",
                    attempt,
                    _MAX_ATTEMPTS,
                    session_id,
                    elapsed,
                    type(exc).__name__,
                    str(exc)[:200],
                    _RETRY_BACKOFF_S,
                )
                time.sleep(_RETRY_BACKOFF_S)
                continue
            # Final transient failure — bail through the catch-all path.
            log.warning(
                "[PE_RETURN_NONE:session_error] transient SDK failure persisted "
                "across %d attempts (session_id=%s, elapsed=%.2fs, exc=%s: %s)",
                _MAX_ATTEMPTS,
                session_id,
                elapsed,
                type(exc).__name__,
                str(exc)[:200],
            )
            _track_pe_outcome(
                outcome="session_error",
                usage=session_usage,
                elapsed_s=elapsed,
                portco_key=portco_key,
            )
            return None
        except anthropic.BadRequestError as exc:
            # Deterministic 400 — same input would fail same way. No retry.
            elapsed = time.monotonic() - started_at
            log.warning(
                "[PE_RETURN_NONE:session_create_failed] BadRequestError "
                "(session_id=%s, elapsed=%.2fs, msg=%s)",
                session_id,
                elapsed,
                str(exc)[:200],
            )
            _track_pe_outcome(
                outcome="session_create_failed",
                usage=session_usage,
                elapsed_s=elapsed,
                portco_key=portco_key,
            )
            return None
        except Exception as exc:
            # Catch-all — emit a structured line with the exception type and
            # the existing exception traceback. Returns None without retry
            # because we don't know whether the failure is transient.
            elapsed = time.monotonic() - started_at
            log.warning(
                "[PE_RETURN_NONE:exception] unexpected error during PE call "
                "(attempt=%d, session_id=%s, elapsed=%.2fs, exc=%s: %s)",
                last_attempt,
                session_id,
                elapsed,
                type(exc).__name__,
                str(exc)[:200],
            )
            log.exception("Prompt preprocessing failed (unexpected exception)")
            _track_pe_outcome(
                outcome="exception",
                usage=session_usage,
                elapsed_s=elapsed,
                portco_key=portco_key,
            )
            return None

    elapsed = time.monotonic() - started_at

    # Path 2 — stream completed with no text (silent path that the sub3
    # incident exposed). Distinguish "session reported an error" from
    # "session ended cleanly but produced nothing" via the prefix.
    if not text_parts:
        reason_tag = "session_error" if saw_session_error else "empty_text_parts"
        log.warning(
            "[PE_RETURN_NONE:%s] no text_parts returned "
            "(session_id=%s, elapsed=%.2fs, attempts=%d, saw_session_error=%s)",
            reason_tag,
            session_id,
            elapsed,
            last_attempt,
            saw_session_error,
        )
        _track_pe_outcome(
            outcome=reason_tag,
            usage=session_usage,
            elapsed_s=elapsed,
            portco_key=portco_key,
        )
        try:
            if session_id:
                pe_client.beta.sessions.archive(session_id)
        except Exception:
            pass
        return None

    raw = "".join(text_parts).strip()
    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()

    # Path 3 — JSON parse failure (model drift). Deterministic; no retry.
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning(
            "[PE_RETURN_NONE:json_parse_failed] model output did not parse as JSON "
            "(session_id=%s, elapsed=%.2fs, exc=%s, raw[:200]=%r)",
            session_id,
            elapsed,
            str(exc)[:200],
            raw[:200],
        )
        _track_pe_outcome(
            outcome="json_parse_failed",
            usage=session_usage,
            elapsed_s=elapsed,
            portco_key=portco_key,
        )
        try:
            if session_id:
                pe_client.beta.sessions.archive(session_id)
        except Exception:
            pass
        return None

    # Path 4 — schema validation. The Coordinator depends on
    # improved_prompt + summary; the rest is best-effort. Missing
    # improved_prompt OR summary is treated as invalid_schema so the
    # caller falls back to the boilerplate ack instead of posting a
    # half-populated plan.
    if not isinstance(result, dict) or not result.get("improved_prompt"):
        log.warning(
            "[PE_RETURN_NONE:invalid_schema] result missing required keys "
            "(session_id=%s, elapsed=%.2fs, keys=%s, raw[:200]=%r)",
            session_id,
            elapsed,
            list(result.keys()) if isinstance(result, dict) else type(result).__name__,
            raw[:200],
        )
        _track_pe_outcome(
            outcome="invalid_schema",
            usage=session_usage,
            elapsed_s=elapsed,
            portco_key=portco_key,
        )
        try:
            if session_id:
                pe_client.beta.sessions.archive(session_id)
        except Exception:
            pass
        return None

    log.info(
        f"Preprocessing complete: {result.get('summary', '')} "
        f"(session_id={session_id}, elapsed={elapsed:.2f}s)"
    )

    # Archive the short-lived session
    try:
        if session_id:
            pe_client.beta.sessions.archive(session_id)
    except Exception:
        pass

    _track_pe_outcome(
        outcome="ok",
        usage=session_usage,
        elapsed_s=elapsed,
        portco_key=portco_key,
    )
    return result


# Prompt Engineer always runs on Sonnet 4.6 (see
# agents/provision_prompt_engineer.py:PROMPT_ENGINEER_MODEL). Pinned here so
# the cost ledger uses the same string MODEL_COSTS_PER_MTOK keys on.
_PROMPT_ENGINEER_MODEL = "claude-sonnet-4-6"


def _track_pe_outcome(
    outcome: str,
    *,
    usage,
    elapsed_s: float,
    portco_key: str,
) -> None:
    """Wrapper around ``cost_collector.track_prompt_engineer_call`` that
    never raises into ``_preprocess_prompt``.

    The PE preprocess path is hot — observability must never break the
    callsite. A DB outage or import failure here only loses a forensic
    row, not the request.
    """
    try:
        from cost_collector import track_prompt_engineer_call

        track_prompt_engineer_call(
            outcome=outcome,
            model=_PROMPT_ENGINEER_MODEL,
            usage=usage,
            elapsed_s=elapsed_s,
            portco_key=portco_key,
        )
    except Exception:
        log.exception(
            "[PE_TRACK_FAILED] track_prompt_engineer_call(outcome=%s) raised "
            "— continuing without DB row",
            outcome,
        )


def _build_ack_message(preprocess: dict) -> str:
    """Build a rich Slack acknowledgment from preprocessing output."""
    summary = preprocess.get("summary", "").strip()
    steps = preprocess.get("plan_steps", [])
    expected = preprocess.get("expected_output", "").strip()
    risk_flags = preprocess.get("risk_flags", [])

    parts = []

    # Line 1: receipt + summary
    if summary:
        parts.append(f"Got it — {summary[0].lower()}{summary[1:]}")
    else:
        parts.append("Got it — investigating now.")

    # Plan
    if steps:
        parts.append("")
        parts.append("*Plan:*")
        for i, step in enumerate(steps, 1):
            parts.append(f"{i}. {step}")

    # Expected output
    if expected:
        parts.append("")
        parts.append(f"*Expected output:* {expected}")

    # Risk flags
    if risk_flags:
        parts.append("")
        flags_text = ", ".join(risk_flags)
        parts.append(f":warning: _{flags_text}_")

    return "\n".join(parts)


def on_slack_question(
    user_id: str,
    text: str,
    thread_ts: str,
    channel_id: Optional[str] = None,
    ack_fn: Optional[Callable] = None,
    verbosity: str = "summary",
    event_ts: Optional[str] = None,
):
    global _active_count
    if _shutting_down:
        log.warning(f"Rejecting question during shutdown: {text[:80]}")
        return
    log.info(
        f"Slack question from {user_id} in {channel_id} (verbosity={verbosity}): {text}"
    )

    # Resolve the portco BEFORE preprocessing so the Prompt Engineer
    # session has both the portco context in its user message AND the
    # health memory store attached for /{portco}/instructions.md.
    # Defaults to "acme" — the only active portco today — when
    # channel_id is missing or doesn't resolve.
    portco_key = "acme"
    if channel_id:
        try:
            from portco_registry import get_portco_by_channel

            _portco = get_portco_by_channel(channel_id)
            if _portco and _portco.get("key"):
                portco_key = _portco["key"]
        except Exception:
            log.exception(
                "portco_registry.get_portco_by_channel failed; "
                "falling back to portco_key=acme"
            )

    # Preprocess the prompt to get a plan and improved prompt
    preprocess = _preprocess_prompt(text, portco_key=portco_key)
    improved_prompt = None
    response_shape = None

    if preprocess and ack_fn:
        ack_msg = _build_ack_message(preprocess)
        ack_fn(ack_msg)
        improved_prompt = preprocess.get("improved_prompt")
        # PR 5 (hybrid_data_synthesis): forward the Prompt Engineer's
        # response_shape classification all the way to the Coordinator
        # kickoff so the validation-pipeline branch fires for
        # mixed-intent questions instead of relying on the Coordinator
        # to re-derive the shape from the prose.
        response_shape = preprocess.get("response_shape")
    elif ack_fn:
        ack_fn(
            "On it — investigating now. I'll post findings in this thread "
            "when done, including any charts and data files."
        )

    with _active_lock:
        _active_count += 1
        active = _active_count
    log.info(f"Submitting investigation ({active} active)")
    future = _executor.submit(
        _run_investigation,
        text,
        user_id,
        thread_ts,
        channel_id,
        improved_prompt,
        verbosity,
        event_ts,
        response_shape,
    )
    snippet = text[:80]
    with _futures_lock:
        _active_futures[future] = snippet
    future.add_done_callback(_on_future_done)


def on_slack_feedback(
    user_id: str, text: str, thread_ts: str, channel_id: Optional[str] = None
):
    """Write user feedback/instructions to the portco-scoped memory store."""
    log.info(f"Feedback from {user_id}: {text}")
    try:
        from portco_registry import get_portco_by_channel

        portco_key = "acme"
        if channel_id:
            portco = get_portco_by_channel(channel_id)
            if portco:
                portco_key = portco["key"]

        today = date.today().isoformat()
        instructions_path = f"/{portco_key}/instructions.md"

        existing = None
        try:
            memories = memory_client.beta.memory_stores.memories.list(
                config.HEALTH_STORE_ID,
                path_prefix=f"/{portco_key}/instructions",
            )
            from anthropic.types.beta.memory_stores import BetaManagedAgentsMemory

            for m in memories.data:
                if isinstance(m, BetaManagedAgentsMemory) and m.path == instructions_path:
                    existing = m
                    break
        except Exception:
            pass

        entry = f"\n- {text} _(from <@{user_id}>, {today})_"

        if existing is not None:
            current = memory_client.beta.memory_stores.memories.retrieve(
                existing.id,
                memory_store_id=config.HEALTH_STORE_ID,
            )
            updated = (current.content or "") + entry
            memory_client.beta.memory_stores.memories.update(
                existing.id,
                memory_store_id=config.HEALTH_STORE_ID,
                content=updated,
            )
        else:
            memory_client.beta.memory_stores.memories.create(
                config.HEALTH_STORE_ID,
                path=instructions_path,
                content=f"# Standing Instructions — {portco_key.title()}\n\nPortco-specific instructions for all investigations.\n{entry}",
            )

        log.info(f"Feedback saved to {instructions_path}")
    except Exception:
        log.exception("Failed to save feedback to memory store")


def scheduled_dream():
    log.info("Starting scheduled dream session")
    try:
        session_id = run_dream_session()
        log.info(f"Dream session completed: {session_id}")
    except Exception:
        log.exception("Dream session failed")
        send_notification("watch", "Nightly dream session failed — check Railway logs")
        return

    log.info("Dream complete — handing off to investigation")
    try:
        inv_session_id = run_investigation_session()
        log.info(f"Investigation session completed: {inv_session_id}")
    except Exception:
        log.exception("Post-dream investigation failed")
        send_notification(
            "watch", "Post-dream investigation session failed — check Railway logs"
        )


def run_full_nightly_pipeline():
    """Run the entire nightly pipeline sequentially. Used for testing."""
    log.info("=== NIGHTLY PIPELINE START (manual trigger) ===")
    send_notification(
        "info",
        "Nightly pipeline starting (manual trigger): self-improve → DB sync → forecast → dream → investigation",
    )

    steps = [
        ("Self-improvement", scheduled_self_improve),
        ("Learnings Compaction", scheduled_compact_learnings),
        ("DB Sync", scheduled_db_sync),
        ("Forecast Analysis", scheduled_forecast),
        ("Dream → Investigation", scheduled_dream),
        ("Prompt-Patch Promoter", scheduled_promote_prompt_patches),
        ("Code-fix Issues Promotion", scheduled_codefix_issues_promote),
    ]

    results = []
    for name, fn in steps:
        log.info(f"--- Pipeline step: {name} ---")
        try:
            fn()
            results.append(f":white_check_mark: {name}")
            log.info(f"--- {name}: OK ---")
        except Exception:
            log.exception(f"--- {name}: FAILED ---")
            results.append(f":x: {name}")

    summary = "\n".join(results)
    send_notification("info", f"Nightly pipeline complete:\n{summary}")
    log.info("=== NIGHTLY PIPELINE END ===")


def scheduled_self_improve():
    log.info("Starting Managed Agents docs-diff check")
    try:
        check_for_updates()
    except Exception:
        log.exception("Managed Agents docs-diff check failed")
        send_notification(
            "watch",
            "Managed Agents docs-diff check failed — check Railway logs",
        )


def scheduled_forecast():
    """Nightly pipeline movement and forecast analysis."""
    log.info("Starting forecast analysis")
    try:
        session_id = run_forecast_analysis()
        log.info(f"Forecast analysis completed: {session_id}")
    except Exception:
        log.exception("Forecast analysis failed")
        send_notification(
            "watch", "Nightly forecast analysis failed — check Railway logs"
        )


def scheduled_codefix_issues_promote():
    """Weekly Saturday 09:30 PT promotion of code_fix observations to GitHub issues.

    Plan #20 — closes the outgoing side of the readback gap. Task #18
    (PR #185) made every agent READ ``/system/learnings.md``; Task #19's
    prompt-patches cron runs at 09:00 PT Saturday; this cron runs 30
    minutes later so the two don't share an Anthropic API rate window.

    The function in ``codefix_issue_creator.create_issues_from_learnings``
    classifies every learning block as
    ``prompt_patch | code_fix | runbook | observation``, dedupes
    ``code_fix`` blocks against a fingerprint ledger, and creates a
    GitHub issue per new fingerprint via ``gh issue create``. Never
    raises — partial failures admin-DM.
    """
    log.info("Starting code-fix → GitHub issue promotion")
    try:
        from codefix_issue_creator import create_issues_from_learnings

        blocks_seen, issues_created, urls, ok = create_issues_from_learnings()
        log.info(
            "Code-fix promotion: %d blocks seen, %d issues created, success=%s",
            blocks_seen,
            issues_created,
            ok,
        )
        if urls:
            for url in urls:
                log.info("Code-fix issue created: %s", url)
    except Exception:
        log.exception("Code-fix issue promotion failed")
        send_notification(
            "watch",
            "Code-fix issue promotion failed — check Railway logs",
        )


def scheduled_compresr_cache_expiry():
    """Daily 04:00 PT pass to expire compresr_cache rows older than 7 days.

    The compresr SDK caches compressed text keyed by
    (sha256(text||model||query), model). Plan #37 documented a 7-day TTL;
    the audit on 2026-05-11 (docs/proposals/compresr-audit-2026-05-11.md §2)
    caught that the TTL was never actually enforced — rows accumulated
    forever, and a stale row could shadow a fresher SDK compression.
    ``compresr_client.expire_old_cache`` is the deleter; this wrapper logs
    the row count and shields APScheduler from any exception.

    04:00 PT is an idle hour: after the 03:00 forecast run, before the
    05:00 dream → investigation. The job is cheap (single DELETE) so the
    timing only matters for avoiding contention with the cost-pull jobs
    that start at 06:00.
    """
    log.info("Starting compresr_cache expiry sweep")
    try:
        from compresr_client import expire_old_cache

        deleted = expire_old_cache()
        log.info(f"Compresr cache expiry: {deleted} row(s) deleted")
    except Exception:
        log.exception("Compresr cache expiry failed")
        send_notification(
            "watch",
            "Compresr cache expiry failed — stale rows may shadow fresh "
            "compressions until the next run",
        )


def scheduled_session_artifact_sweep():
    """Daily 04:30 PT pass: delete .parquet / .xlsx / .csv files older than
    14 days under ``SESSION_OUTPUT_DIR``.

    Theme B (2026-05-16) moved the canonical artifact store to a Railway
    Volume so files survive Anthropic's sandbox TTL. With persistence comes
    the need to garbage-collect — Railway Volumes are durable but not
    infinite. 14 days is generous enough that a Slack thread re-opened a
    week later still resolves cleanly. 04:30 PT slots between the 04:00
    compresr expiry and the 05:00 dream run.
    """
    log.info("Starting session artifact sweep")
    try:
        from artifact_paths import sweep_session_artifacts

        stats = sweep_session_artifacts(max_age_days=14)
        log.info(
            "Session artifact sweep done: scanned=%d deleted=%d freed_mb=%.2f errors=%d",
            stats["scanned"],
            stats["deleted"],
            stats["freed_bytes"] / 1_048_576.0,
            stats["error_count"],
        )
    except Exception:
        log.exception("Session artifact sweep failed")


def scheduled_schema_introspection():
    """Daily 02:00 PT pass: refresh ``/{portco}/schema_cache.md`` for every
    Salesforce-backed portco by querying FieldDefinition + writing the
    machine-authored cache.

    Theme C (2026-05-16). Pre-fix, schema_cache.md was hand-curated and
    drifted: today's incident found it missing Closed_Lost_Notes__c (45.1%
    fill on 7,716 CL opps) and listing Forecast_Category__c which doesn't
    exist in the org. This cron rewrites the cache from the live SF
    schema each night.

    Per-portco failures don't stop other portcos. The whole job is wrapped
    so an unexpected exception only logs + admin-DMs.
    """
    log.info("Starting nightly schema introspection")
    try:
        from portco_registry import get_all_portcos, get_data_source
        from schema_introspection import introspect_portco
        from session_runner import _get_sf_client

        results: list[dict] = []
        for portco in get_all_portcos():
            portco_key = portco.get("key")
            if not portco_key:
                continue
            crm_cfg = get_data_source(portco_key, "crm") or {}
            if crm_cfg.get("type") != "salesforce":
                continue
            sf_client = None
            try:
                sf_client = _get_sf_client(portco_key)
            except Exception:
                log.exception(
                    "schema_introspection: _get_sf_client(%s) failed", portco_key
                )
                continue
            if sf_client is None:
                log.info("schema_introspection: skipping %s — no SF client", portco_key)
                continue
            stats = introspect_portco(sf_client, portco_key)
            results.append(stats)
            log.info(
                "schema_introspection: portco=%s sobjects=%d fields=%d "
                "free_text=%d error=%s",
                stats["portco_key"],
                stats["sobjects_queried"],
                stats["fields_total"],
                stats["free_text_fields"],
                stats["error"] or "-",
            )
        ok = sum(1 for r in results if not r.get("error"))
        log.info(
            "Nightly schema introspection done: %d/%d portcos succeeded",
            ok,
            len(results),
        )
    except Exception:
        log.exception("Nightly schema introspection failed")
        try:
            send_notification(
                "watch",
                "Nightly schema introspection failed — schema_cache.md may "
                "be stale until the next run. Investigate Railway logs.",
                admin_only=True,
            )
        except Exception:
            log.exception("schema_introspection admin notify failed")


def scheduled_promote_prompt_patches():
    """Weekly Saturday 09:00 PT — promote un-applied prompt_patch entries
    into a draft GitHub PR. Task #19.

    Reads ``/system/prompt_patches.md`` from the health memory store,
    filters fingerprints already in ``/system/prompt_patches_applied.md``,
    asks Sonnet 4.6 for ONE coherent diff against
    ``agents/setup_agents.py``, and opens a draft PR. Never raises; every
    failure path DMs admins via ``send_notification(admin_only=True)``.
    """
    log.info("Starting weekly prompt-patch promoter")
    try:
        seen, applied, pr_url, ok = promote_prompt_patches()
        log.info(
            f"Prompt-patch promoter complete: seen={seen} applied={applied} "
            f"pr={pr_url or '-'} ok={ok}"
        )
    except Exception:
        # promote_prompt_patches contracts not to raise, but defense in depth:
        # APScheduler should never see an unhandled exception from a job.
        log.exception("Prompt-patch promoter raised unexpectedly")
        send_notification(
            "watch",
            "Prompt-patch promoter raised unexpectedly — check Railway logs",
        )


def scheduled_db_sync():
    """Nightly snapshot: pull all SF data into Railway Postgres via MCP sessions."""
    if not is_db_available():
        log.warning("DB sync skipped — DATABASE_URL not configured or unreachable")
        return
    log.info("Starting nightly DB snapshot")
    portcos = get_all_portcos()
    synced = 0
    for p in portcos:
        try:
            snapshot_id = run_sync_session(p["key"])
            if snapshot_id:
                synced += 1
        except Exception:
            log.exception(f"DB snapshot failed for {p['key']}")
    log.info(f"DB snapshot complete: {synced}/{len(portcos)} portcos")


def scheduled_pull_anthropic_costs():
    """Daily 06:00 PT pull of Anthropic Admin Usage & Cost API into Postgres.

    Plan #35 (docs/plans/35-cost-tracking-and-reporting.md, task #38). Idempotent
    upsert via cost_collector.pull_anthropic_daily_costs — re-running the same
    day is safe. Defaults to a 3-day lookback so late-arriving rows reconcile.

    Skipped at registration time when ANTHROPIC_ADMIN_KEY is unset; this wrapper
    is the runtime-safety net (rate-table swap, transient HTTP, container
    restart re-runs) and must never crash the scheduler thread.
    """
    log.info("Starting Anthropic Admin API daily cost pull")
    try:
        from cost_collector import pull_anthropic_daily_costs

        rows = pull_anthropic_daily_costs(days_back=3)
        log.info(f"Anthropic daily cost pull completed: {rows} rows upserted")
    except Exception:
        log.exception("Anthropic daily cost pull failed")
        send_notification(
            "watch",
            "Anthropic daily cost pull failed — check Railway logs (reconciliation will be stale)",
        )


def scheduled_purge_session_thread_events():
    """Daily 06:00 PT purge of session_thread_events rows older than 30 days.

    Plan #44 Task #16 / decision row #11. The migration
    ``00AD_session_thread_events.sql`` documents a 30-day TTL via a worker
    that calls ``db_adapter.purge_session_thread_events_older_than(30)``
    on the same cron as the cost-reconciliation pipeline. Railway-managed
    Postgres does not expose pg_cron, so the orchestrator owns the sweep.

    The function is idempotent and capped at 50K rows per call so the
    DELETE lock window stays short under the documented 500-1K
    inserts/session peak. Without this scheduler entry the ledger would
    grow unbounded — the migration's stated retention contract would be
    a lie.

    Never crashes the scheduler thread; logs and admin-DMs on failure.
    """
    log.info("Starting session_thread_events 30-day TTL purge")
    try:
        from db_adapter import purge_session_thread_events_older_than

        deleted = purge_session_thread_events_older_than(days=30)
        log.info(f"session_thread_events purge completed: {deleted} row(s) deleted")
    except Exception:
        log.exception("session_thread_events purge failed")
        send_notification(
            "watch",
            "session_thread_events 30-day TTL purge failed — ledger growth "
            "is unbounded until the next run succeeds (Plan #44 Task #16)",
        )


def scheduled_sweep_orphan_thread_placeholders():
    """15-min sweep of orphan ``nightly_run_threads`` placeholder rows.

    Task #22 — the DB-first ordering in :func:`slack_thread_registry.
    get_or_create_thread` INSERTs a placeholder row (``thread_ts = NULL``)
    BEFORE posting to Slack and DELETEs it on Slack-post failure. If the
    claiming process crashes BETWEEN the INSERT and either the UPDATE
    or the DELETE, the orphan sits NULL forever and blocks every future
    call on the same ``(run_id, theme, channel_id)`` key.

    This cron DELETEs placeholders older than 10 minutes — long enough
    that a slow Slack post is never mistaken for an orphan, short enough
    that a stuck key is unblocked within one digest window. Never crashes
    the scheduler thread.
    """
    try:
        from slack_thread_registry import _sweep_orphan_placeholders

        _sweep_orphan_placeholders(max_age_minutes=10)
    except Exception:
        log.exception("nightly_run_threads orphan sweep failed")


def scheduled_reconcile_costs():
    """Daily 07:00 PT reconciliation of local cost estimates vs. Anthropic billing.

    Plan #35 (docs/plans/35-cost-tracking-and-reporting.md, task #42). Runs an
    hour after the 06:00 PT ``scheduled_pull_anthropic_costs`` cron so the
    ground-truth Anthropic Admin API data for the prior UTC day is already in
    ``anthropic_daily_costs`` when we compare.

    Behavior:
      * ``|drift_pct| > 10%`` → ``cost_collector.reconcile_daily`` posts a Slack
        watch notice (deduped once per day per direction via
        ``cost_reconciliation_alerts``).
      * ``|drift_pct| > 25%`` → additionally logs a recommended
        ``MODEL_COSTS_PER_MTOK`` refresh — pricing-table drift is the most
        likely cause of large miss.
      * Otherwise → logs ``reconciliation OK`` and exits silently.

    Never crashes the scheduler thread.
    """
    log.info("Starting daily cost reconciliation")
    try:
        from cost_collector import reconcile_daily

        result = reconcile_daily()
        log.info(
            f"Cost reconciliation completed: date={result['date']} "
            f"severity={result['severity']} drift_pct={result['drift_pct']} "
            f"alerted={result['alerted']} deduped={result['deduped']}"
        )
    except Exception:
        log.exception("Daily cost reconciliation failed")
        send_notification(
            "watch",
            "Daily cost reconciliation failed — check Railway logs (drift visibility degraded)",
        )


def scheduled_cost_digest():
    """Daily 08:00 PT DM digest of yesterday's cost summary.

    Plan #35 (docs/plans/35-cost-tracking-and-reporting.md, task #41). Runs an
    hour after ``scheduled_reconcile_costs`` (07:00 PT) so the drift number on
    the digest matches the watch notice (if any) the reconciliation cron
    already posted. Always registered — when ``DATABASE_URL`` is unset the
    digest body degrades gracefully, and when no admin users are configured
    ``send_daily_cost_digest`` short-circuits with a logged warning.

    Never crashes the scheduler thread.
    """
    log.info("Starting daily cost digest")
    try:
        from cost_digest import send_daily_cost_digest

        result = send_daily_cost_digest()
        log.info(
            f"Cost digest completed: date={result['date']} "
            f"recipients={len(result['recipients'])} sent={result['sent']} "
            f"failed={result['failed']} skipped_reason={result.get('skipped_reason')}"
        )
    except Exception:
        log.exception("Daily cost digest failed")
        send_notification(
            "watch",
            "Daily cost digest failed — check Railway logs (operators won't get yesterday's spend DM)",
        )


def scheduled_compact_learnings():
    """Daily 00:30 PT compaction of /system/learnings.md.

    Plan #18 — close the readback gap. self_heal has been writing session
    learnings to ``/system/learnings.md`` since 2026-04, but no agent
    prompt ever read the file. Every Specialist + Coordinator now reads
    ``/system/learnings_compact.md`` BEFORE its first tool call; this cron
    is the producer.

    Runs at 00:30 PT — after midnight so it picks up every session that
    closed during the prior day, before the 01:00 PT DB sync starts
    competing for memory-store quota. Single Sonnet 4.6 call; mirrors the
    prompt-cache pattern in self_heal.py so re-runs hit the 5m cache.

    Never crashes the scheduler thread — ``compact_learnings`` returns
    ``success=False`` on any failure and admin-DMs internally.
    """
    log.info("Starting learnings compaction")
    try:
        from learnings_compactor import compact_learnings

        input_chars, output_chars, tokens, success = compact_learnings()
        log.info(
            "Learnings compaction done: input=%d chars output=%d chars "
            "tokens=%d success=%s",
            input_chars,
            output_chars,
            tokens,
            success,
        )
    except Exception:
        log.exception("Learnings compaction failed")
        send_notification(
            "watch",
            "Learnings compaction failed — agents will continue using yesterday's "
            "compact (if any) until the next 00:30 PT run",
            admin_only=True,
        )


def scheduled_surface_refresh():
    """Daily 08:00 PT walk of active portcos pushing each to its Canvas.

    Plan #33 F9 — the "even if nothing changed" trigger. Reaction-driven
    sync (in ``slack_bot.handle_reaction_added``) and the manual
    ``/refresh-surface`` slash command cover the event-driven and admin
    paths; this cron is the safety net so the Canvas never drifts more
    than 24 hours from the underlying findings, even on quiet days.

    Failure isolation is per-portco — one portco's bad data must not
    block the others from refreshing. Matches the ``scheduled_db_sync``
    idiom. Lazy imports because F6 (``surface_pusher.push_to_canvas``)
    may not be on main yet when F9 lands; that's a logged failure, not
    an import-time crash.

    Never crashes the scheduler thread.
    """
    log.info("Starting daily surface refresh")
    try:
        from surface_pusher import push_to_canvas
        from portco_registry import get_all_portcos
    except Exception:
        log.exception(
            "[SURFACE_PUSH_FAILED] daily refresh: import failed "
            "(surface_pusher may not be on main yet)"
        )
        return

    portcos = []
    try:
        portcos = get_all_portcos()
    except Exception:
        log.exception("[SURFACE_PUSH_FAILED] daily refresh: portco enumeration failed")
        return

    succeeded = 0
    failed = 0
    for p in portcos:
        key = p.get("key", "") if isinstance(p, dict) else ""
        if not key:
            continue
        try:
            push_to_canvas(key)
            succeeded += 1
        except Exception:
            failed += 1
            log.exception(f"[SURFACE_PUSH_FAILED] daily refresh: {key}")
    log.info(
        f"Daily surface refresh complete: {succeeded}/{len(portcos)} pushed, "
        f"{failed} failed"
    )

    # Plan #49 — Channel description refresh pass.
    # Walks every active portco's `slack_channel` plus the RFP channel and
    # reapplies the bot-owned channel `purpose` text. Failure isolation is
    # per-channel; an error on one channel does not block the rest. The
    # description push is fast (one conversations.info + one conditional
    # setPurpose per channel) so it fits inside the 08:00 PT cron window.
    # Lives in the same scheduled job as the Canvas push because both are
    # "surface state" refreshes that belong together conceptually.
    try:
        from channel_descriptions import push_channel_description, RFP_CHANNEL_ID

        all_channel_ids = [p.get("slack_channel") for p in portcos if p.get("slack_channel")]
        if RFP_CHANNEL_ID not in all_channel_ids:
            all_channel_ids.append(RFP_CHANNEL_ID)
        desc_succeeded = 0
        desc_failed = 0
        for ch_id in all_channel_ids:
            try:
                if push_channel_description(ch_id):
                    desc_succeeded += 1
                else:
                    desc_failed += 1
            except Exception:
                desc_failed += 1
                log.exception("[CHANNEL_DESC_FAILED] daily refresh: %s", ch_id)
        log.info(
            "Daily channel desc refresh: %d/%d ok, %d failed",
            desc_succeeded,
            len(all_channel_ids),
            desc_failed,
        )
    except Exception:
        log.exception(
            "[CHANNEL_DESC_FAILED] daily refresh: import or setup failed"
        )


def _batch_callback_registry() -> dict:
    """Compose the callback registry passed to ``batch_runner.poll_pending_batches``.

    Lazy-imported so the test harness (which patches ``BackgroundScheduler`` and
    runs ``main.main`` end-to-end) doesn't pull self_heal/self_improve into the
    module-level import chain. The registry maps the ``call_site`` /
    ``callback_name`` recorded on each batch row to the function that should
    run when the batch completes.

    Plan #36 task #53: when a new batch-eligible call site is added, register
    its completion handler here.
    """
    from self_heal import _handle_batch_completion as self_heal_complete
    from self_improve import _handle_batch_completion as self_improve_complete

    return {
        "self_heal": self_heal_complete,
        "self_improve": self_improve_complete,
    }


# Plan #52 PR-Y: deduplicate Slack watch notices from scheduler jobs.
# Per-notice cooldown of 60 min so a transient DB hiccup doesn't fire
# a fresh Slack alert every 15 min. Cleared on process restart —
# alert-fatigue prevention only, not durable state.
_SCHEDULER_WATCH_COOLDOWN_SECONDS = 60 * 60
_scheduler_watch_last_post: dict[str, float] = {}


def _scheduler_watch_notice(key: str, body: str) -> None:
    """Fire a 'watch' notice through send_notification, deduped by key.

    The first call for a given key always fires; subsequent calls within
    ``_SCHEDULER_WATCH_COOLDOWN_SECONDS`` are suppressed. We use a ``None``
    sentinel (not a ``0.0`` default) because ``time.monotonic()`` returns
    process-uptime — early in the process, ``now - 0.0`` is well under the
    cooldown and would suppress the very first notice.
    """
    import time as _time

    now = _time.monotonic()
    last = _scheduler_watch_last_post.get(key)
    if last is not None and now - last < _SCHEDULER_WATCH_COOLDOWN_SECONDS:
        return
    try:
        send_notification("watch", body)
    except Exception:
        # Codex P2 on PR #249: do NOT update the cooldown timestamp when the
        # Slack send fails — otherwise a transient blip suppresses every
        # subsequent alert for the full cooldown even though no notice was
        # delivered. Swallow + log so the caller doesn't crash APScheduler;
        # the next call will retry the send.
        log.exception("Scheduler watch notice failed for key=%s", key)
        return
    _scheduler_watch_last_post[key] = now


def scheduled_batch_flush():
    """Hourly reconcile of in-flight batches against Anthropic's view.

    Plan #36 task #53. Today's batch_runner submits one request per batch
    inline (no client-side buffer to drain), so the "flush" surface is really
    a periodic recovery pass — ``batch_runner.recover_orphan_batches`` reads
    every row still marked ``status='submitted'`` and checks whether the
    Anthropic-side batch has actually finished / been canceled. The result is
    that prior-container batches converge to ``ended`` (where the next poll
    can dispatch them) or ``failed`` (where the row stays out of the
    permanent backlog) regardless of which container submitted them.

    Hourly cadence matches the plan's recommendation; this is the cheap
    housekeeping job. The expensive end-to-end dispatch lives in
    ``scheduled_batch_poll`` (every 15 minutes).

    Never crashes the scheduler thread.
    """
    log.info("Starting batch flush / orphan-recovery sweep")
    try:
        import batch_runner

        recovered = batch_runner.recover_orphan_batches()
        log.info(f"Batch flush completed: {recovered} row(s) reconciled")
    except Exception:
        log.exception("Batch flush failed")
        _scheduler_watch_notice(
            "batch_flush_failed",
            "Batch flush failed — check Railway logs (orphan batches may stall)",
        )


def scheduled_batch_poll():
    """Every-15-minutes poll of pending Anthropic batches; dispatch completions.

    Plan #36 task #53. Iterates every ``batch_jobs`` row still marked
    ``status='submitted'``, retrieves the batch from Anthropic, and when it's
    ``ended`` streams the results through the registered callback (e.g.
    ``self_heal._handle_batch_completion`` writes learnings to memory, fires
    code-fix patches; ``self_improve._handle_batch_completion`` saves the
    nightly DM and pings operators). Cost rows are also written per request
    by ``batch_runner._log_batch_cost`` so Plan #35 reconciliation can split
    batch vs realtime spend.

    The 15-minute cadence is what the plan recommends — most batches finish
    inside an hour, so polling four times an hour keeps the worst-case
    learning-delivery latency under ~20 minutes once the batch ends. We can
    tighten this later if we ever hit the volumes where it matters.

    Never crashes the scheduler thread.
    """
    log.info("Starting batch poll")
    try:
        import batch_runner

        registry = _batch_callback_registry()
        completed = batch_runner.poll_pending_batches(registry)
        if completed:
            log.info(f"Batch poll completed: {completed} batch(es) dispatched")
        else:
            log.debug("Batch poll completed: no batches ready")
    except Exception:
        log.exception("Batch poll failed")
        _scheduler_watch_notice(
            "batch_poll_failed",
            "Batch poll failed — check Railway logs (batch results stalled)",
        )


def _handle_shutdown(signum, frame, scheduler: Optional[BackgroundScheduler] = None):
    """Graceful shutdown: stop accepting work, drain active investigations, exit."""
    global _shutting_down
    if _shutting_down:
        log.warning("Duplicate shutdown signal — forcing exit")
        sys.exit(1)
    _shutting_down = True

    sig_name = signal.Signals(signum).name
    log.info(f"Shutdown signal received ({sig_name}) — draining active investigations")

    # 1. Stop accepting new Slack events
    log.info("Stopping Slack socket mode handler")
    try:
        stop_socket_mode()
        log.info("Slack socket mode stopped")
    except Exception:
        log.exception("Error stopping Slack socket mode")

    # 2. Stop the scheduler so no new cron jobs fire
    if scheduler:
        log.info("Shutting down APScheduler")
        try:
            scheduler.shutdown(wait=False)
            log.info("APScheduler stopped")
        except Exception:
            log.exception("Error shutting down scheduler")

    # 2b. Drain the ❌-watcher executor BEFORE waiting on the main
    # investigation pool. Watcher jobs are independent and may share
    # the same shutdown deadline; draining concurrently keeps total
    # shutdown wall-clock at SHUTDOWN_TIMEOUT_SECONDS, not 2× that.
    try:
        from watcher_worker import shutdown_watcher_executor

        shutdown_watcher_executor(timeout_seconds=SHUTDOWN_TIMEOUT_SECONDS)
    except Exception:
        log.exception("Error draining ❌-watcher executor")

    # 3. Wait for in-flight investigations to finish
    with _futures_lock:
        pending = dict(_active_futures)

    if pending:
        log.info(
            f"Waiting for {len(pending)} active investigation(s) to complete (timeout={SHUTDOWN_TIMEOUT_SECONDS}s):"
        )
        for fut, snippet in pending.items():
            log.info(f"  - {snippet}")

        completed = []
        abandoned = []
        # Shut down the executor: wait=True blocks until all submitted work finishes.
        # We set a deadline and cancel anything still pending after the timeout.
        _executor.shutdown(wait=False, cancel_futures=False)

        from concurrent.futures import wait as futures_wait

        remaining = set(pending.keys())
        import time

        deadline = time.monotonic() + SHUTDOWN_TIMEOUT_SECONDS

        while remaining and time.monotonic() < deadline:
            time_left = deadline - time.monotonic()
            if time_left <= 0:
                break
            done, remaining = futures_wait(remaining, timeout=min(time_left, 10))
            for fut in done:
                snippet = pending.get(fut, "<unknown>")
                exc = fut.exception()
                if exc:
                    log.warning(f"  Completed with error: {snippet} — {exc}")
                else:
                    log.info(f"  Completed: {snippet}")
                completed.append(snippet)

        for fut in remaining:
            snippet = pending.get(fut, "<unknown>")
            log.warning(f"  Abandoned (timeout): {snippet}")
            abandoned.append(snippet)
            fut.cancel()

        log.info(
            f"Shutdown drain complete: {len(completed)} completed, {len(abandoned)} abandoned"
        )

        shutdown_msg = f":warning: *Bot restarting* — {len(completed)} investigations completed, {len(abandoned)} abandoned"
        if completed:
            shutdown_msg += "\n*Completed:*\n" + "\n".join(f"- {s}" for s in completed)
        if abandoned:
            shutdown_msg += "\n*Abandoned (may need re-run):*\n" + "\n".join(
                f"- {s}" for s in abandoned
            )
            log.warning(
                f"Abandoned investigations (sessions may still be running on Anthropic): "
                f"{abandoned}"
            )
        try:
            send_notification("watch", shutdown_msg)
        except Exception:
            log.warning("Failed to post shutdown summary to Slack")
    else:
        log.info("No active investigations — shutting down immediately")
        _executor.shutdown(wait=False)

    log.info("Graceful shutdown complete — exiting")
    sys.exit(0)


# ─────────────────────────────────────────────────────────────────────────────
# /health HTTP endpoint (B10, Z2 deploy verification)
# ─────────────────────────────────────────────────────────────────────────────

# Captured at module-load time so /health reports when the process actually
# came up, not when the HTTP request landed. Used by Z2 to verify the live
# container is the build it claims to be.
_DEPLOY_STARTED_AT = datetime.now(timezone.utc).isoformat()

# Path to the agent active-version pin file. Optional — older deploys before
# Plan #41 V1 don't have one, in which case /health returns ``{}``.
_ACTIVE_VERSIONS_PATH = Path(__file__).parent.parent / "agents" / "active_versions.json"


def _read_active_versions() -> dict:
    """Read agents/active_versions.json into a dict, returning {} on any failure.

    The file is optional — until Plan #41 V1 lands it won't exist. /health
    must never crash because of a missing file; it just returns the empty
    map and the operator interprets that as "version pinning not yet
    deployed."
    """
    try:
        if _ACTIVE_VERSIONS_PATH.exists():
            with open(_ACTIVE_VERSIONS_PATH, "r") as fh:
                data = json.load(fh)
                return data if isinstance(data, dict) else {}
    except Exception:
        log.exception("Failed to read active_versions.json — returning empty")
    return {}


def _build_health_payload() -> dict:
    """Assemble the /health JSON payload. Pure function — easy to test.

    ``active_versions`` reads the pin file fresh on each request so the
    operator can ``git pull`` + ``kill -HUP`` and verify the new pins are
    live without a restart. ``pinned_versions`` (Plan #44 Task #8) surfaces
    what the orchestrator process LOADED at boot via
    ``config.AGENT_VERSIONS`` — the operative pin used by every
    ``sessions.create`` call (Task #9). The two values should match in
    steady state; a delta means a redeploy is required to pick up the new
    pin file from disk.
    """
    return {
        "build_commit": os.environ.get("BUILD_COMMIT", "unknown"),
        "deploy_started_at": _DEPLOY_STARTED_AT,
        "active_versions": _read_active_versions(),
        "pinned_versions": dict(config.AGENT_VERSIONS),
        "status": "ok",
    }


def _build_ready_payload() -> tuple[int, dict]:
    """Compose the ``/ready`` JSON payload + HTTP status code.

    Returns ``(status_code, payload)``. 200 when ``_READY`` is True; 503
    otherwise with a ``reason`` and (when available) the last smoke-probe
    check_results. Plan #42 PR2 — Railway uses ``healthcheckPath = "/ready"``,
    so this route is what gates the deploy.
    """
    if _READY:
        return 200, {
            "ready": True,
            "reason": "smoke_probe_passed",
            "check_results": _READY_CHECK_RESULTS,
        }
    return 503, {
        "ready": False,
        "reason": _READY_REASON,
        "check_results": _READY_CHECK_RESULTS,
    }


class _HealthHandler(BaseHTTPRequestHandler):
    """GET /health → 200 always. GET /ready → 200/503 per ``_READY``."""

    # Silence the default per-request stdout spam; route through the
    # orchestrator logger at DEBUG so production logs aren't flooded.
    def log_message(self, format, *args):
        log.debug("health-http: " + format, *args)

    def do_GET(self):  # noqa: N802 — http.server enforces the camelCase name
        path = self.path.split("?")[0]
        if path in ("/health", "/healthz"):
            payload = _build_health_payload()
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/ready":
            status, payload = _build_ready_payload()
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):  # noqa: N802 — http.server enforces the camelCase name
        # Plan #44 Task #14 — Anthropic webhook receiver. Signature
        # validation, dedupe, and dispatch all live in
        # anthropic_webhooks_register.process_webhook_post.
        if self.path.split("?")[0] != "/webhooks/anthropic":
            self.send_response(404)
            self.end_headers()
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length > 0 else b""
            headers = {k: v for k, v in self.headers.items()}
            from anthropic_webhooks_register import process_webhook_post

            status_code, response_body = process_webhook_post(body, headers)
        except Exception as exc:
            log.exception("Anthropic webhook handler failed: %s", exc)
            status_code, response_body = 500, '{"error": "internal"}'
        response_bytes = (
            response_body.encode("utf-8")
            if isinstance(response_body, str)
            else (response_body or b"")
        )
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response_bytes)))
        self.end_headers()
        self.wfile.write(response_bytes)


def _start_health_server() -> Optional[HTTPServer]:
    """Bind a minimal stdlib HTTP server for /health on the configured port.

    Honors ``PORT`` (Railway sets this) with an 8080 fallback. Binds on a
    daemon thread so it shuts down with the process. Returns the server
    instance for tests; production callers ignore it.

    Best-effort: if the port is already taken (e.g. local dev with another
    server running), log a warning and return None instead of crashing the
    whole orchestrator. The bot still works without /health — it just loses
    the Z2 verification surface.
    """
    port = int(os.environ.get("PORT", "8080"))
    try:
        server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    except OSError as e:
        log.warning(
            "Could not bind /health server on port %d: %s — continuing without it",
            port,
            e,
        )
        return None
    t = threading.Thread(target=server.serve_forever, daemon=True, name="health-http")
    t.start()
    log.info("Health endpoint listening on port %d (GET /health)", port)
    return server


def _run_pre_deploy_smoke_probe() -> bool:
    """Run the Plan #42 PR2 smoke probe and update the readiness gate.

    Returns the final value of ``_READY``. Three states:

    * ``SMOKE_PROBE_ENABLED=false`` (case-insensitive) — probe is skipped,
      ``_READY`` flips to True immediately, ``_READY_REASON`` becomes
      ``"smoke_probe_disabled"``, and an admin DM warns the operator that
      this deploy was not validated.
    * Probe ran and passed (including inconclusive-PASS on Anthropic 429/503)
      — ``_READY = True``.
    * Probe ran and failed — ``_READY`` stays False; ``/ready`` returns 503;
      Railway healthcheckTimeout fires and holds the previous image.

    Never raises: probe failures inside the probe module are already captured
    in the returned ``SmokeResult``; here we additionally guard against import
    failures so a malformed probe module cannot crash the orchestrator boot.
    """
    global _READY, _READY_REASON, _READY_CHECK_RESULTS

    enabled = os.environ.get("SMOKE_PROBE_ENABLED", "true").strip().lower() != "false"
    if not enabled:
        log.warning("SMOKE_PROBE_ENABLED=false — skipping pre-deploy smoke probe")
        _READY = True
        _READY_REASON = "smoke_probe_disabled"
        _READY_CHECK_RESULTS = {}
        try:
            from smoke_probe import render_disabled_dm

            summary, detail = render_disabled_dm()
            send_notification("watch", summary, detail, admin_only=True)
        except Exception:
            log.exception("smoke_probe: render_disabled_dm/admin-DM failed")
        return _READY

    try:
        from smoke_probe import run_smoke_probe
    except Exception:
        log.exception(
            "smoke_probe import failed — leaving _READY=False (deploy will be "
            "held by Railway healthcheckTimeout)"
        )
        _READY = False
        _READY_REASON = "smoke_probe_pending"
        _READY_CHECK_RESULTS = {}
        return _READY

    try:
        result = run_smoke_probe()
    except Exception:
        # Defensive: run_smoke_probe is documented as never-raises, but if a
        # bug breaks that contract we must NOT bring the orchestrator down.
        log.exception(
            "smoke_probe: run_smoke_probe() raised unexpectedly — "
            "treating as failed probe"
        )
        _READY = False
        _READY_REASON = "smoke_probe_failed"
        _READY_CHECK_RESULTS = {}
        return _READY

    _READY = bool(result.passed)
    _READY_CHECK_RESULTS = result.check_results or {}
    _READY_REASON = "smoke_probe_passed" if _READY else "smoke_probe_failed"
    log.info(
        "smoke_probe outcome: passed=%s anthropic=%s reason=%s",
        result.passed,
        result.anthropic_status,
        result.reason or "(none)",
    )
    return _READY


def main():
    log.info("GTM Health Agent Orchestrator starting")

    # Plan #42 PR2 — Startup ordering with the readiness gate:
    #   (1) ensure_schema()                     — create smoke_probe_runs etc.
    #   (2) recover_interrupted_investigations() — survive prior container.
    #   (3) run_pre_deploy_smoke_probe()        — sets ``_READY``.
    #   (4) _start_health_server()              — exposes /health + /ready.
    #   (5) start_socket_mode()                 — ONLY when _READY is True.
    # If smoke probe fails, /ready returns 503; Railway's healthcheckTimeout
    # fires and the deploy is marked failed (the previous image keeps
    # serving Slack traffic). Socket mode does NOT start. /health remains
    # 200 so the operator can curl the container for diagnostics.
    if is_db_available():
        log.info("Railway Postgres connected — dual-source routing active")
        ensure_schema()
        # Task #23 — one-time cleanup of orphan sessions flagged in prior
        # carry-over snapshots. These are already known to the operator;
        # mark them ``orphan_dead_lettered`` if they're still stuck in
        # 'running' before the recovery loop sees them. No admin DM (we
        # already know about these specific sessions). New orphans get
        # the live admin-DM path inside recover_interrupted_investigations.
        try:
            from db_adapter import cleanup_known_orphans

            cleaned = cleanup_known_orphans()
            if cleaned:
                log.info(
                    "cleanup_known_orphans: marked %d known orphan investigation(s) "
                    "as orphan_dead_lettered (ids=%s)",
                    len(cleaned),
                    cleaned,
                )
        except Exception:
            log.exception("cleanup_known_orphans failed — continuing startup")
        try:
            recovered = recover_interrupted_investigations()
            if recovered:
                log.info(
                    f"Recovered {len(recovered)} interrupted investigation(s) from prior container"
                )
                send_notification(
                    "info",
                    f"Bot restarted — recovered {len(recovered)} interrupted investigation(s) from the previous container.",
                )
        except Exception:
            log.exception("Investigation recovery failed — continuing startup")

        # ❌-Watcher startup catch-up sweep (Phase 1 PR 3). Back-fills
        # watcher_pending rows for failures terminalized in the last
        # CATCH_UP_WINDOW_MINUTES that the lifecycle hook missed (because
        # the orchestrator was down during the failure window — Railway
        # deploy gap, OOM crash, etc.). Recursion guard inside the
        # catch-up SELECT excludes the watcher's own agent ID.
        # No-op when WATCHER_ENABLED != 'true'.
        try:
            from watcher_worker import catch_up_on_startup

            catch_up_on_startup()
        except Exception:
            log.exception("Watcher catch-up sweep failed — continuing startup")
    else:
        log.info("Railway Postgres not available — using MCP only")

    _run_pre_deploy_smoke_probe()

    _start_health_server()

    set_question_handler(on_slack_question)
    set_feedback_handler(on_slack_feedback)

    scheduler = BackgroundScheduler(timezone=config.TIMEZONE)

    from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_MISSED

    def _on_job_event(event):
        try:
            if event.exception:
                log.error(f"Scheduler job '{event.job_id}' failed: {event.exception}")
            elif hasattr(event, "code") and event.code == EVENT_JOB_MISSED:
                log.warning(f"Scheduler job '{event.job_id}' missed its scheduled run")
                send_notification(
                    "watch",
                    f"Scheduled job '{event.job_id}' missed — container may have been down",
                )
        except Exception:
            log.exception("Error in scheduler event listener")

    scheduler.add_listener(
        _on_job_event, EVENT_JOB_ERROR | EVENT_JOB_EXECUTED | EVENT_JOB_MISSED
    )

    # The five user-facing Slack cron jobs (dream, self-improve, forecast,
    # cost-reconcile, cost-digest) were retired 2026-05-14 — the daily push
    # output was noise without a defined job-to-be-done. The PAUSE_DAILY_MESSAGES
    # kill switch went away with them. Underlying functions (scheduled_dream,
    # scheduled_forecast, scheduled_self_improve, scheduled_reconcile_costs,
    # scheduled_cost_digest) remain importable for ``RUN_NIGHTLY_NOW`` manual
    # testing and for any future alert-based replacement. Re-introduce
    # registrations here only after the JTBD discussion lands.
    scheduler.add_job(
        scheduled_db_sync,
        CronTrigger.from_crontab("0 1 * * *", timezone=config.TIMEZONE),
        id="db-sync",
        name="Nightly DB Sync",
    )

    # Plan #18 — Daily 00:30 PT learnings compaction. Reads the verbose
    # ledger self_heal has been appending to /system/learnings.md since
    # 2026-04 and collapses it into a flat rules-by-tool list at
    # /system/learnings_compact.md. Every Specialist + Coordinator prompt
    # now reads that file BEFORE its first tool call. Runs at 00:30 PT
    # — after midnight so it picks up the prior day's sessions, before
    # the 01:00 PT DB sync starts competing for memory-store quota.
    scheduler.add_job(
        scheduled_compact_learnings,
        CronTrigger(hour=0, minute=30, timezone="America/Los_Angeles"),
        id="learnings-compact",
        name="Learnings Compactor",
        max_instances=1,
        coalesce=True,
    )

    # Task #19 — Weekly Saturday 09:00 PT prompt-patch promoter.
    scheduler.add_job(
        scheduled_promote_prompt_patches,
        CronTrigger(
            day_of_week="sat",
            hour=9,
            minute=0,
            timezone="America/Los_Angeles",
        ),
        id="prompt-patches-promote",
        name="Prompt Patches Promoter",
        max_instances=1,
        coalesce=True,
    )
    log.info("Registered prompt-patch promoter cron at Sat 09:00 America/Los_Angeles")

    # Plan #20 — Weekly Saturday 09:30 PT promotion of self_heal code_fix
    # observations to GitHub issues. Sits 30 minutes after Task #19's
    # prompt-patches cron so the two don't compete for the Anthropic API
    # rate window on the same morning.
    scheduler.add_job(
        scheduled_codefix_issues_promote,
        CronTrigger(
            day_of_week="sat",
            hour=9,
            minute=30,
            timezone=config.TIMEZONE,
        ),
        id="codefix-issues-promote",
        name="Code-fix Issue Promotion (Sat 09:30 PT)",
        max_instances=1,
        coalesce=True,
    )

    # Plan #37 follow-up (audit 2026-05-11) — Daily compresr_cache expiry.
    # Sits at 04:00 PT, between the 03:00 forecast and the 05:00 dream run,
    # in the quietest hour of the night cron pipeline.
    scheduler.add_job(
        scheduled_compresr_cache_expiry,
        CronTrigger.from_crontab("0 4 * * *", timezone=config.TIMEZONE),
        id="compresr-cache-expiry",
        name="Compresr Cache Expiry",
    )

    # Theme B (2026-05-16) — daily session artifact sweep at 04:30 PT.
    # Deletes .parquet/.xlsx/.csv older than 14 days under SESSION_OUTPUT_DIR
    # (Railway Volume in prod). Slots between compresr expiry and the 05:00
    # dream run.
    scheduler.add_job(
        scheduled_session_artifact_sweep,
        CronTrigger.from_crontab("30 4 * * *", timezone=config.TIMEZONE),
        id="session-artifact-sweep",
        name="Session Artifact TTL Sweep",
    )

    # Theme C (2026-05-16) — nightly schema introspection at 02:00 PT.
    # Rewrites /{portco}/schema_cache.md from live SF FieldDefinition so
    # the cache never drifts off the live org again.
    scheduler.add_job(
        scheduled_schema_introspection,
        CronTrigger.from_crontab("0 2 * * *", timezone=config.TIMEZONE),
        id="nightly-schema-introspection",
        name="Nightly Schema Introspection",
    )

    # Plan #35 task #38 — Anthropic Admin Usage & Cost API daily pull.
    # Scheduled at 06:00 PT (well past Anthropic's ~5-minute freshness window
    # for the prior UTC day). Requires ANTHROPIC_ADMIN_KEY; absent → skip
    # registration so dev environments and degraded prod don't churn the
    # scheduler with a guaranteed-no-op job every morning.
    if config.ANTHROPIC_ADMIN_KEY:
        scheduler.add_job(
            scheduled_pull_anthropic_costs,
            CronTrigger.from_crontab("0 6 * * *", timezone=config.TIMEZONE),
            id="anthropic-cost-pull",
            name="Anthropic Admin API Daily Cost Pull",
        )
        log.info(
            f"Registered Anthropic Admin API daily cost pull at 06:00 {config.TIMEZONE}"
        )
    else:
        log.warning(
            "ANTHROPIC_ADMIN_KEY unset — skipping Anthropic daily cost pull "
            "registration (Plan #35 reconciliation runs in degraded mode)"
        )

    # Plan #44 Task #16 / decision row #11 — session_thread_events 30-day TTL
    # sweep. The migration ``00AD_session_thread_events.sql`` documents that
    # this runs "on the same cron schedule as the cost reconciliation job
    # (06:00 PT)". Registered unconditionally (DATABASE_URL-gated inside the
    # purge function so dev / degraded prod degrade gracefully). Without
    # this entry the ledger grows unbounded — the retention contract in the
    # migration would be aspirational instead of enforced.
    scheduler.add_job(
        scheduled_purge_session_thread_events,
        CronTrigger.from_crontab("0 6 * * *", timezone=config.TIMEZONE),
        id="session-thread-events-purge",
        name="Session Thread Events 30-day TTL Purge",
    )
    log.info(
        f"Registered session_thread_events 30-day TTL purge at 06:00 {config.TIMEZONE}"
    )

    # Task #22 — Orphan placeholder sweep for nightly_run_threads. The
    # DB-first claim in slack_thread_registry leaves a NULL-ts row in the
    # table while the Slack post is in flight; the cleanup path DELETEs
    # the row on Slack-post failure. If the claiming process crashes
    # between the INSERT and either the UPDATE or the DELETE, the orphan
    # sits forever and blocks every future call on the same key. Sweeps
    # every 15 min; max_instances=1 + coalesce so a slow run doesn't queue.
    scheduler.add_job(
        scheduled_sweep_orphan_thread_placeholders,
        "interval",
        minutes=15,
        id="nightly-thread-orphan-sweep",
        name="Nightly Thread Orphan-Placeholder Sweep",
        max_instances=1,
        coalesce=True,
    )

    # Plan #33 F9 — Daily 08:00 PT surface refresh. Walks active portcos and
    # pushes each to its Canvas; the reaction-driven sync and the
    # ``/refresh-surface`` slash command cover the event-driven and admin
    # paths, this cron is the "even if nothing changed" safety net.
    # max_instances=1 + coalesce=True so a slow tick (e.g. one portco's
    # Canvas API call timing out) doesn't queue overlapping runs the next
    # morning.
    scheduler.add_job(
        scheduled_surface_refresh,
        CronTrigger(hour=8, minute=0, timezone="America/Los_Angeles"),
        id="surface_refresh_daily",
        name="Daily Surface Refresh",
        max_instances=1,
        coalesce=True,
    )

    # ❌-Watcher drain tick (Phase 1 PR 3). Every 30s, claim up to N
    # pending watcher_pending rows (N = free slots in the dedicated
    # WatcherThreadPoolExecutor) and dispatch them to the executor. The
    # drain function is a no-op when WATCHER_ENABLED != 'true' so this
    # job is safe to register on Day 1 of rollout. max_instances=1 +
    # coalesce so a slow drain doesn't queue overlapping ticks.
    try:
        from watcher_worker import scheduled_watcher_drain

        scheduler.add_job(
            scheduled_watcher_drain,
            "interval",
            seconds=30,
            id="watcher_drain_30s",
            name="❌-Watcher Queue Drain",
            max_instances=1,
            coalesce=True,
        )
        log.info("Registered ❌-watcher drain at 30s interval")
    except Exception:
        log.exception("Failed to register ❌-watcher drain — continuing")

    # Session-size canary — every 30s, scan active sessions for proximity to
    # the 1M-input-token-per-turn cap. Fires :warning: at 750K (gives the
    # user ~250K headroom) and :rotating_light: at 950K (last-chance archive
    # +replay notice). Motivation: session sesn_EXAMPLE
    # (2026-05-11) died silently at 1.12M tokens — no answer, no warning.
    # max_instances=1 + coalesce=True so a slow tick doesn't queue.
    scheduler.add_job(
        check_active_sessions,
        "interval",
        seconds=30,
        id="session_watch_canary",
        name="Session-Size Canary",
        max_instances=1,
        coalesce=True,
    )

    # Plan #36 task #53 — Batch processing hooks. Only registered when the
    # kill switch is on so dev environments and degraded prod don't churn
    # the scheduler with no-op jobs. Both jobs are wrapped in try/except
    # that posts a Slack watch notice on failure — APScheduler must never
    # see an unhandled exception from either wrapper.
    if config.BATCH_PROCESSING_ENABLED:
        # Hourly flush: reconcile prior-container batches against Anthropic's
        # view so orphan rows converge to a terminal state. Idempotent.
        scheduler.add_job(
            scheduled_batch_flush,
            "interval",
            hours=1,
            id="batch-flush",
            name="Batch Flush / Orphan Recovery",
            max_instances=1,
            coalesce=True,
        )
        # Every 15 minutes: poll Anthropic for ended batches, dispatch results
        # through the callback registry (self_heal + self_improve). Cheap when
        # there's nothing pending (early-exits on an empty DB query).
        scheduler.add_job(
            scheduled_batch_poll,
            "interval",
            minutes=15,
            id="batch-poll",
            name="Batch Poll",
            max_instances=1,
            coalesce=True,
        )
        log.info("Batch processing hooks registered — flush hourly, poll every 15m")
    else:
        log.info(
            "BATCH_PROCESSING_ENABLED=false — skipping batch flush + poll "
            "registration (self_heal + self_improve stay realtime)"
        )

    if os.environ.get("RUN_NIGHTLY_NOW"):
        from datetime import datetime, timedelta, timezone as tz
        from zoneinfo import ZoneInfo

        # Loud warning every startup the env var is set — the Python-process
        # ``os.environ.pop`` after firing does NOT remove the Railway env var,
        # so leaving it set turns every container restart into another run.
        # Symptom on 2026-05-11: an unrelated prompt PR merge auto-deployed at
        # 16:00 PT, the container restarted, and the full nightly pipeline
        # fired at 16:02 PT.
        log.warning(
            "RUN_NIGHTLY_NOW is set. Pipeline will fire 2min after startup IF "
            "the per-day marker doesn't already exist. REMOVE FROM RAILWAY ENV "
            "AFTER FIRST INTENTIONAL RUN to prevent re-trigger on container restart."
        )

        # Fire-once-per-day guard. The marker file is keyed on the Pacific
        # date (matches the rest of the nightly cron timezone), so an
        # intentional run at 09:00 PT and an accidental container restart
        # at 16:00 PT on the same day will only fire once.
        pacific_today = datetime.now(ZoneInfo(config.TIMEZONE)).date().isoformat()
        marker_path = f"/tmp/nightly_now_fired_{pacific_today}"

        if os.path.exists(marker_path):
            log.info(
                f"RUN_NIGHTLY_NOW set but already fired today (marker: {marker_path}) "
                f"— skipping. Remove the Railway env var to silence this warning."
            )
        else:
            run_at = datetime.now(tz.utc) + timedelta(minutes=2)

            def _run_and_clear():
                # Write the marker BEFORE invoking the pipeline so a crash
                # mid-run still blocks the next restart from double-firing.
                try:
                    with open(marker_path, "w") as fh:
                        fh.write(datetime.now(tz.utc).isoformat())
                except OSError:
                    log.exception(
                        f"Failed to write RUN_NIGHTLY_NOW marker at {marker_path} "
                        "— per-day guard is degraded for this run"
                    )
                try:
                    run_full_nightly_pipeline()
                finally:
                    os.environ.pop("RUN_NIGHTLY_NOW", None)
                    log.info(
                        "RUN_NIGHTLY_NOW cleared in process env. "
                        "Remove from Railway env to prevent re-trigger on next deploy."
                    )

            scheduler.add_job(
                _run_and_clear,
                "date",
                run_date=run_at,
                id="nightly-now",
                name="Nightly Pipeline (manual test)",
            )
            log.info(
                f"Manual nightly pipeline scheduled for {run_at.strftime('%H:%M:%S')} (2 minutes from now)"
            )

    scheduler.start()
    cost_pull_status = "6am" if config.ANTHROPIC_ADMIN_KEY else "disabled"
    log.info(
        f"Scheduler started — Learnings compactor: 00:30, DB sync: 1am, "
        f"Compresr cache expiry: 4am, Anthropic cost pull: {cost_pull_status}, "
        f"session_thread_events purge: 6am, Surface refresh: 8am, "
        f"Code-fix issues promote: Sat 9:30am ({config.TIMEZONE}). "
        f"Daily Slack-posting crons (dream/self-improve/forecast/cost-reconcile/"
        f"cost-digest) retired 2026-05-14 pending JTBD redefinition."
    )

    # Register SIGTERM/SIGINT handlers for graceful shutdown
    def shutdown_handler(signum, frame):
        _handle_shutdown(signum, frame, scheduler=scheduler)

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)
    log.info("Graceful shutdown handlers registered (SIGTERM, SIGINT)")

    # Plan #42 PR2 — Socket Mode binds ONLY after the smoke probe has flipped
    # ``_READY`` to True. A failed probe leaves the container running so
    # ``/health`` is still reachable for diagnostics, but the bot refuses to
    # accept Slack traffic against an unvalidated build. Railway's healthcheck
    # against ``/ready`` will mark the deploy failed and hold the previous
    # image; this branch is the in-process belt-and-suspenders for the same
    # contract.
    if not _READY:
        log.error(
            "Pre-deploy smoke probe did not pass (reason=%s). Socket Mode "
            "will NOT start; /ready returns 503 and Railway will hold the "
            "previous image. /health stays 200 for diagnostics.",
            _READY_REASON,
        )
        return

    # Plan: Design A (2026-05-15) — start the stalled-session watchdog.
    # Runs as a daemon thread; honors WATCHDOG_ENABLED env (default true).
    # Polls running investigations every WATCHDOG_POLL_SECONDS and recovers
    # sessions stuck past STALL_THRESHOLD_SECONDS (default 600s/10min) via a
    # 3-tier escalation: gentle user.message wakeup → user.interrupt on
    # stranded sub-threads → Slack ❌ + admin DM + archive.
    try:
        from session_watchdog import start_watchdog
        from session_runner import (
            CONTAINER_ID,
            _archive_and_invalidate_session,
            client as _watchdog_client,
        )
        from slack_bot import send_notification as _watchdog_send_notification
        from lifecycle import terminalize_lifecycle as _watchdog_terminalize
        import db_adapter as _watchdog_db

        start_watchdog(
            client=_watchdog_client,
            db_adapter_mod=_watchdog_db,
            container_id=CONTAINER_ID,
            send_notification_fn=_watchdog_send_notification,
            terminalize_fn=lambda **kw: _watchdog_terminalize(
                state=__import__("lifecycle").DeliveryState.TERMINAL_FAILURE, **kw
            ),
            archive_session_fn=_archive_and_invalidate_session,
        )
    except Exception:
        log.exception("Failed to start session watchdog — proceeding without it")

    log.info("Starting Slack socket mode")
    start_socket_mode()


if __name__ == "__main__":
    main()
