"""Post-session self-healing: reviews sessions, learns from mistakes, updates memory and code."""

import json
import logging
import re
from datetime import date
from pathlib import Path
from typing import Optional

import anthropic
import cost_collector
from _messages_usage import log_messages_usage
import batch_runner
from batch_runner import BATCH_CACHE_TTL
from compresr_client import compress_prompt
from config import ANTHROPIC_API_KEY, HEALTH_STORE_ID

log = logging.getLogger(__name__)

_SELF_HEAL_MODEL = "claude-sonnet-4-6"

# Callback name registered with batch_runner. When a batch poll finds an ended
# self_heal batch, batch_runner._dispatch_results looks up this key in the
# callback registry the poller passes in (main.py is expected to compose
# ``{"self_heal": self_heal._handle_batch_completion, ...}``). The name matches
# the call_site used in submit_batch so callers don't need to remember a
# separate symbol — see ``batch_runner.submit_batch`` defaulting
# ``callback_name`` to ``call_site``.
BATCH_CALLBACK_NAME = "self_heal"

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

ORCHESTRATOR_DIR = Path(__file__).parent
AGENTS_DIR = ORCHESTRATOR_DIR.parent / "agents"


def review_session(session_id: str, session_type: str = "ad-hoc"):
    """Review a completed session for learnings and improvements.

    When BATCH_PROCESSING_ENABLED=true, the analysis Messages API call is
    enqueued onto the Anthropic Batches API (50% off, async). The downstream
    side effects (``_save_learnings`` + ``_apply_code_fixes``) are deferred
    to ``_handle_batch_completion`` which fires when ``batch_runner`` polls
    and finds the result. When the env is false (default) or the enqueue
    fails, we fall back to the original realtime path immediately so no
    learnings are dropped.
    """
    log.info(f"Self-heal: reviewing session {session_id} ({session_type})")

    try:
        events = client.beta.sessions.events.list(session_id=session_id)
    except Exception:
        log.exception(f"Failed to fetch events for session {session_id}")
        return

    tool_errors = []
    tool_calls = []
    agent_messages = []
    session_errors = []

    for e in events.data:
        if e.type == "agent.custom_tool_use":
            tool_calls.append({"name": e.name, "input": e.input, "id": e.id})

        elif e.type == "user.custom_tool_result":
            content_text = ""
            if hasattr(e, "content") and e.content:
                for b in e.content:
                    if hasattr(b, "text"):
                        content_text = b.text
            if '"error"' in content_text:
                matching_call = next(
                    (
                        tc
                        for tc in tool_calls
                        if tc["id"] == getattr(e, "custom_tool_use_id", None)
                    ),
                    None,
                )
                tool_errors.append(
                    {
                        "tool": matching_call["name"] if matching_call else "unknown",
                        "input": matching_call["input"] if matching_call else {},
                        "error": content_text[:500],
                    }
                )

        elif e.type == "agent.message":
            if hasattr(e, "content") and e.content:
                for b in e.content:
                    if hasattr(b, "text") and b.text:
                        agent_messages.append(b.text[:300])

        elif e.type == "session.error":
            error_data = e.error if hasattr(e, "error") else None
            session_errors.append(
                {
                    "type": getattr(error_data, "type", "unknown")
                    if error_data
                    else "unknown",
                    "message": getattr(error_data, "message", "") if error_data else "",
                }
            )

    if not tool_errors and not session_errors:
        log.info(f"Self-heal: session {session_id} completed cleanly — no issues found")
        _save_clean_session(session_id, session_type, len(tool_calls))
        return

    analysis = _analyze_session(
        session_id,
        session_type,
        tool_errors,
        session_errors,
        tool_calls,
        agent_messages,
    )

    # When BATCH_PROCESSING_ENABLED is true and the enqueue succeeded,
    # _analyze_session returns None. Downstream side effects (save_learnings,
    # apply_code_fixes) are deferred to _handle_batch_completion via the
    # batch_runner poll loop. Logging the queued state here keeps the
    # operator-visible signal — they see "queued" in the log line and know
    # to expect the learnings to land minutes-to-hours later.
    if analysis is None:
        log.info(
            f"Self-heal: session {session_id} queued for batch analysis "
            f"({len(tool_errors)} tool errors, {len(session_errors)} session errors) — "
            f"learnings will be saved when the batch completes"
        )
        return

    _save_learnings(session_id, analysis)

    if analysis.get("code_fixes"):
        _apply_code_fixes(analysis["code_fixes"])

    log.info(
        f"Self-heal: session {session_id} reviewed — {len(tool_errors)} tool errors, {len(session_errors)} session errors"
    )


def _build_summary(
    session_id, session_type, tool_errors, session_errors, tool_calls, agent_messages
):
    """Build the JSON-serializable session summary used as the Messages API user message.

    Pulled out as a named function so both the realtime and batch paths build
    the same payload and the compresr POC integration runs identically in
    both — protecting the shadow eval (Plan #37 Task #64) from drifting when
    we switch tiers.
    """
    return {
        "session_id": session_id,
        "type": session_type,
        "tool_call_count": len(tool_calls),
        "tool_errors": tool_errors,
        "session_errors": session_errors,
        "agent_messages_sample": agent_messages[:5],
    }


# System prompt is a module-level constant so the realtime path, the batch
# request builder, and any future paths share the exact same bytes — which
# matters for cache hits. The Messages API caches by exact prompt match on
# the first ephemeral-cache marker, so keeping this in one place guarantees
# the cache prefix doesn't drift across call sites.
#
# Size note (PR fix/caching-and-compresr, 2026-05-11): this prompt is
# deliberately >1024 effective system tokens. Sonnet's prompt-caching
# minimum is 1024 tokens — any cached block below that silently no-ops at
# the API edge. Earlier versions of this constant were ~470 tokens, which
# means the ``cache_control: ephemeral 1h`` marker was decorative for
# months. Do NOT trim this back below ~1100 tokens without verifying via
# ``client.messages.count_tokens`` that the cached block still clears the
# floor. The cost-audit doc that found the bug lives at
# ``docs/proposals/cache-audit-2026-05-11.md``.
_SELF_HEAL_SYSTEM_PROMPT = (
    "You are a session reviewer for a GTM Health Agent built on Anthropic's "
    "Managed Agents API. The system runs 11 specialized agents in four tiers: "
    "Coordinator (Opus 4.8) orchestrates sub-agents, runs validation, posts to "
    "Slack; Dream (Sonnet 4.6) generates nightly investigation hypotheses; "
    "Quick Answer (Sonnet 4.6) handles single-fact lookups; three Specialists "
    "(Pipeline Monitor, Sales Process Monitor, Post-Sales Monitor on Sonnet 4.6) "
    "query Salesforce via MCP and report confidence-tagged findings; "
    "Statistician (Opus 4.8) runs CIs, p-values, regression, survival analysis; "
    "Chart Designer (Sonnet 4.6) renders visualizations via QuickChart; "
    "Adversarial Reviewer (Opus 4.8) runs a five-check challenge process on "
    "every finding before it reaches Slack; Cross-Domain Synthesizer (Opus 4.8) "
    "names cross-signal patterns; Report Writer (Sonnet 4.6) assembles "
    "validated findings into polished Slack messages and .docx/.xlsx reports; "
    "Prompt Engineer (Sonnet 4.6) preprocesses Slack questions and generates "
    "acks.\n\n"
    "The Python orchestrator bridges Slack Socket Mode, Salesforce (via MCP "
    "vaults — Acme is the only active portco today), and the Anthropic "
    "Managed Agents API. Custom tools registered with sessions: "
    "send_slack_notification (severity=watch|info, optional reply_to), "
    "generate_chart (QuickChart wrapper), db_query (read-only against Railway "
    "Postgres snapshots), save_snapshot_batch (writes daily ACV/stage/owner "
    "rollups), post_report (the structured-output variant of "
    "send_slack_notification — when an agent calls this, treat the run as "
    "having delivered its final answer). MCP tools from the Acme vault: "
    "soqlQuery (free-form SOQL string) and describeSObject (schema "
    "introspection). MCP-tool failures auto-approve via "
    "user.tool_confirmation when ``evaluated_permission == 'ask'``.\n\n"
    "SOQL constraints agents commonly violate — flag any of these as the root "
    "cause when you see them:\n"
    "- SOQL does NOT support: CASE, COALESCE, FLOOR, subqueries in SELECT, "
    "  arithmetic between fields, window functions, CTEs.\n"
    "- No column aliases in ORDER BY — must use the aggregate function "
    "  expression itself (``ORDER BY COUNT(Id) DESC``, NOT ``ORDER BY cnt``).\n"
    "- CloseDate is a DATE field (no T/Z suffix); CreatedDate / LastModifiedDate "
    "  are DATETIME (with T/Z). Mixing the two is a silent zero-row result.\n"
    "- THIS_QUARTER / NEXT_QUARTER literals are NOT valid — compute the ISO "
    "  date range in Python and inline literal dates instead.\n"
    "- Long text fields (>255 chars: Description, etc.) cannot be used in "
    "  GROUP BY or DISTINCT.\n"
    "- Aggregate queries (COUNT/SUM/AVG/MIN/MAX) cannot use LIMIT — drop the "
    "  LIMIT, then re-aggregate in Python if you need top-N.\n"
    "- RecordType.Name for record type filtering, not RecordTypeId (the ID is "
    "  per-org and unstable across sandboxes).\n"
    "- IN ()/NOT IN () with >200 values 414s the URL; chunk in Python.\n"
    "- WHERE on formula fields is fine; ORDER BY on formula fields can throw "
    "  ``MALFORMED_QUERY: cannot ORDER BY`` — switch to the underlying field.\n"
    "- HAVING is supported only after GROUP BY; do not use it as a WHERE "
    "  substitute.\n\n"
    "Common failure modes ordered roughly by frequency:\n"
    "1. SOQL syntax errors from unsupported functions (CASE/COALESCE/FLOOR).\n"
    "2. Wrong field names — schema mismatch between org reality and the "
    "   agent's assumed catalog. Memory note must list the corrected field.\n"
    "3. Rate-limit / overloaded errors from too many concurrent sessions or "
    "   API-Limit-Exceeded from SF — retry with exponential backoff or run "
    "   serially.\n"
    "4. Null / empty result handling — agent forgets to branch on zero rows "
    '   and posts ``"no data"`` instead of the substantive ``"no closed-won '
    '   in window X"`` framing the user actually needs.\n'
    "5. Inefficient queries — full ``SELECT *`` record pull when ``SELECT "
    "   COUNT(Id)`` suffices, or pulling 50K rows when an aggregate would do.\n"
    '6. Orchestration chatter ("Let me query Salesforce...") posted to Slack '
    "   instead of the actual finding — coordinator should suppress these.\n"
    "7. Unicode control characters in session titles from Slack formatting "
    "   (em-dashes, smart quotes) breaking downstream URL encoding.\n"
    "8. Chart duplication — the same chart URL posted twice in the same "
    "   message (the chart-tool's idempotency key drifted).\n"
    "9. Agent referencing local container paths like /mnt/session/outputs/ in "
    "   user-visible Slack messages — those paths don't exist for the reader.\n"
    "10. Coordinator calling sub-agents that produce no output and then "
    "    silently dropping the response (multi-agent routing bug).\n"
    "11. Adversarial Reviewer rubber-stamping findings without running all "
    "    five checks — look for review messages shorter than ~200 chars as a "
    "    proxy for skipped review.\n\n"
    "Output contract — you must respond with a single JSON object matching "
    "this shape:\n"
    "{\n"
    '  "learnings": [\n'
    "    {\n"
    '      "issue": "<one-line description of what went wrong>",\n'
    '      "root_cause": "<technical root cause, not a symptom>",\n'
    '      "memory_note": "<text that will be appended to /system/learnings.md so the '
    "agent doesn't repeat the mistake — write it from the agent's POV>\",\n"
    '      "code_fix": null | "<one-paragraph description of a system-prompt or tool-handling change>"\n'
    "    }\n"
    "  ],\n"
    '  "code_fixes": [\n'
    "    {\n"
    '      "file": "<orchestrator/<file>.py or agents/setup_agents.py>",\n'
    '      "description": "<one-line summary>",\n'
    '      "change": "<a concrete change you\'d suggest to the maintainer>"\n'
    "    }\n"
    "  ]\n"
    "}\n"
    "Return ONLY the JSON object — no preamble, no markdown fence, no trailing "
    "commentary. A non-JSON response is a parse failure that gets logged and "
    "trips the compresr regression guard.\n\n"
    "Good fix vs bad fix — examples:\n"
    'GOOD: ``{"issue": "SOQL referenced Opportunity.LeadSource_c instead of '
    'LeadSource", "root_cause": "Stale schema_cache.md entry from prior '
    'sandbox", "memory_note": "On Acme prod, LeadSource is a standard '
    "field (Opportunity.LeadSource). Run describeSObject before assuming "
    'custom-field names ending in _c.", "code_fix": "In the Pipeline '
    "Monitor system prompt, add: 'When in doubt, call describeSObject before "
    "writing the SOQL.'\"}``\n"
    "GOOD: identifying that a Specialist hallucinated MCP unavailability by "
    "running ``ls /var/run/`` and ``which sfdx`` instead of just calling "
    '``soqlQuery({"q": "SELECT Id FROM Account LIMIT 1"})`` — the fix is a '
    'specialist-prompt note saying "verify MCP access by attempting a '
    'trivial call, not by filesystem inspection".\n'
    'BAD: ``{"issue": "Test failed", "root_cause": "Unknown", '
    '"memory_note": "investigate later", "code_fix": null}`` — vague '
    "issue, no root cause, no actionable memory note. Reject.\n"
    "BAD: a code_fix that suggests mocking the database, skipping the test, "
    "or adding ``except Exception: pass`` around the failing call. These are "
    "anti-patterns — never propose them.\n"
    "BAD: suggesting we hardcode field names that should be schema-discovered, "
    "or hardcoding dates that should be computed from ``date.today()``.\n"
    "BAD: code_fix targeting agent system prompts that we don't own (e.g. the "
    "Anthropic-side server-config of a managed agent) — those live in "
    "``agents/setup_agents.py`` and ``agents/update_prompts.py``; a fix that "
    'asks the operator to "edit the agent\'s server prompt manually" is '
    "useless. Suggest the update_prompts.py path instead.\n\n"
    "Anti-patterns you must avoid in your output:\n"
    "- Do NOT suggest disabling tests, adding broad try/except, or "
    "  short-circuiting validation to hide the symptom.\n"
    "- Do NOT recommend mocking out the database, the Anthropic API, the MCP "
    '  vault, or any external dependency as a "fix" — those are valid for '
    "  test isolation, never for production.\n"
    "- Do NOT propose ignoring an error class globally; the right move is "
    "  always to handle the specific error case with a typed branch.\n"
    "- Do NOT suggest rewriting a function unless the actual evidence shows "
    "  the function is structurally wrong. Most session errors are data "
    "  errors or prompt errors, not code errors.\n"
    "- Do NOT propose changes to ``setup_agents.py`` without also noting the "
    "  prompt-deploy step (``update_prompts.py`` or the deploy-prompts CI "
    "  workflow) — merging the file alone does NOT update the live agent.\n\n"
    "Be specific. Issues that recur (e.g. the same SOQL bug across 3 sessions "
    "in a week) deserve a code_fix entry. One-off blips don't. When in doubt, "
    "write the memory_note and leave code_fix=null."
)


def _build_messages_request(compressed_summary: str) -> dict:
    """Build the kwargs dict for a ``client.messages.create`` / batch params call.

    Returns the exact same shape used by both the realtime path
    (``client.messages.create(**request)``) and the batch path
    (``submit_batch(requests=[{..., "params": request}])``). Keeping the
    builder pure makes the two paths byte-identical on Anthropic's side
    so the batch result is a drop-in replacement for the realtime response.
    """
    return {
        "model": _SELF_HEAL_MODEL,
        "max_tokens": 4000,
        "system": [
            {
                "type": "text",
                "text": _SELF_HEAL_SYSTEM_PROMPT,
                # 1h TTL avoids cache expiry mid-batch and lets repeated
                # reviews on the same stable system prompt hit cache reads
                # instead of paying for fresh input. See BATCH_CACHE_TTL in
                # batch_runner.py for the full rationale (Task #54, Plan #36).
                "cache_control": {"type": "ephemeral", "ttl": BATCH_CACHE_TTL},
            }
        ],
        "messages": [
            {
                "role": "user",
                "content": (
                    f"Review this managed agent session and identify improvements.\n\n"
                    f"Session summary:\n{compressed_summary}\n\n"
                    f"For each issue found, provide:\n"
                    f"1. What went wrong (specific error)\n"
                    f"2. Root cause\n"
                    f"3. Memory update: what should be added to the agent's memory so it doesn't repeat this mistake\n"
                    f"4. Code fix: if applicable, what should change in the system prompt or tool handling\n\n"
                    f"Respond in JSON format:\n"
                    f'{{"learnings": [{{"issue": "...", "root_cause": "...", "memory_note": "...", "code_fix": null | "..."}}], '
                    f'"code_fixes": [{{"file": "...", "description": "...", "change": "..."}}]}}'
                ),
            }
        ],
    }


def _parse_analysis(text: str) -> dict:
    """Extract the JSON ``{learnings, code_fixes}`` blob from the model's text.

    Shared between the realtime path and the deferred batch-callback path
    so the saved-to-memory shape is identical regardless of tier. Falls
    back to a learnings-of-one capturing the raw text if the model emitted
    non-JSON, matching the realtime behavior before the refactor.

    Records the parse outcome to the compresr regression guard (Plan #37
    task #67) so trailing-24h vs 14d baseline math can detect quality
    regressions and auto-disable compression when parse-failure rate
    exceeds 2x baseline. Best-effort: any failure is logged and swallowed.
    """
    parsed: Optional[dict] = None
    json_match = re.search(r"\{[\s\S]*\}", text)
    if json_match:
        try:
            parsed = json.loads(json_match.group())
        except json.JSONDecodeError:
            parsed = None

    try:
        from compresr_regression_guard import record_parse_outcome

        record_parse_outcome("self_heal", parsed_ok=parsed is not None)
    except Exception:
        log.debug("self_heal: record_parse_outcome failed (non-fatal)")

    if parsed is not None:
        return parsed

    return {
        "learnings": [
            {
                "issue": "Analysis produced non-JSON output",
                "root_cause": "N/A",
                "memory_note": text[:500],
            }
        ],
        "code_fixes": [],
    }


def _analyze_session(
    session_id, session_type, tool_errors, session_errors, tool_calls, agent_messages
):
    """Use Sonnet to analyze what went wrong and what to fix.

    Returns:
      * A parsed analysis dict ``{"learnings": [...], "code_fixes": [...]}``
        when the call ran realtime (default).
      * ``None`` when ``BATCH_PROCESSING_ENABLED=true`` AND the request was
        successfully enqueued to the Batches API. Caller (``review_session``)
        must treat ``None`` as "deferred" and skip the side-effect calls;
        ``_handle_batch_completion`` runs them later.

    On batch enqueue failure (network error, kill switch flipped, empty
    response), we silently fall through to the realtime path so a Batches
    API outage never blocks self-heal.
    """
    summary = _build_summary(
        session_id,
        session_type,
        tool_errors,
        session_errors,
        tool_calls,
        agent_messages,
    )

    # Compresr POC site (Plan #37, Task #63) — the FIRST Compresr integration.
    # Shadow eval (Task #64) must run for 1 week before broader rollout per
    # Plan #37. compress_prompt silently falls back to the original text when
    # COMPRESR_API_KEY is unset, the per-site flag is off, the circuit breaker
    # is open, or any SDK failure occurs.
    summary_text = json.dumps(summary, indent=2, default=str)
    compressed_summary = compress_prompt(
        summary_text,
        model="latte_v1",
        query=session_id,
        call_site="self_heal",
        min_chars=1000,
    )

    request_params = _build_messages_request(compressed_summary)

    # Batch path: enqueue and defer. submit_batch returns None when the kill
    # switch is off, the SDK errors, or the requests list is empty — any of
    # which routes us to the realtime fallback below.
    batch_id = batch_runner.submit_batch(
        call_site="self_heal",
        model=_SELF_HEAL_MODEL,
        requests=[
            {
                "custom_id": session_id,
                "params": request_params,
                # Context is round-tripped to ``_handle_batch_completion`` via
                # the ``batch_job_requests.context_json`` column. It carries
                # everything the callback needs to fire the deferred side
                # effects (memory write keyed by session_id; code-fix path
                # keyed off the parsed analysis).
                "context": {
                    "session_id": session_id,
                    "session_type": session_type,
                    "tool_error_count": len(tool_errors),
                    "session_error_count": len(session_errors),
                    # Per-request call_site overrides the batch-level value
                    # at cost-logging time (see batch_runner._dispatch_results).
                    # We want the ledger to credit the original function, not
                    # the buffer-flush site, so reconciliation can split
                    # spend by call path the same way realtime does.
                    "call_site": "self_heal._analyze_session",
                },
            }
        ],
        callback_name=BATCH_CALLBACK_NAME,
    )
    if batch_id:
        log.info(
            f"Self-heal: enqueued session {session_id} into batch {batch_id} "
            f"(deferred analysis)"
        )
        return None

    # Realtime fallback: either BATCH_PROCESSING_ENABLED=false, the SDK
    # errored, or we got an empty request list. Hit the Messages API
    # directly and return the parsed analysis like before the refactor.
    response = client.messages.create(**request_params)

    # Log token usage + cost for this Messages API call. The log line is the
    # human-readable record; track_messages_call persists the row to the
    # ``messages_api_calls`` ledger (Plan #35, Task #39) for cost rollups +
    # reconciliation against the Anthropic Admin API ground truth. Both calls
    # swallow their own exceptions — cost tracking is observability, not
    # load-bearing.
    try:
        log_messages_usage("Self-heal", _SELF_HEAL_MODEL, response.usage)
    except Exception:
        log.exception("Failed to log self-heal usage (non-fatal)")
    try:
        cost_collector.track_messages_call(
            call_site="self_heal._analyze_session",
            model=_SELF_HEAL_MODEL,
            usage=response.usage,
        )
    except Exception:
        log.exception("Failed to persist self-heal cost row (non-fatal)")

    text = next((b.text for b in response.content if b.type == "text"), "{}")
    return _parse_analysis(text)


def _handle_batch_completion(
    request_id: str, context: dict, result_text: str, result_usage: dict
):
    """Batch completion handler for self_heal-tier requests.

    Signature matches ``batch_runner._dispatch_results``'s callback contract:
    ``(custom_id, context, text, usage) -> None``. Wired into the registry
    main.py passes to ``poll_pending_batches``. The cost row was already
    written by ``batch_runner._log_batch_cost`` before this callback fires
    (succeeded results only), so we only need to run the side effects that
    the realtime path runs after parsing the response — namely save the
    learnings and apply any code fixes.

    Errors are swallowed so a single bad result can't poison the entire
    poll loop. The realtime path has the same swallow semantics inside
    ``_save_learnings`` / ``_apply_code_fixes``, so behavior is consistent.
    """
    session_id = (
        context.get("session_id") if isinstance(context, dict) else None
    ) or request_id
    try:
        if not result_text:
            log.info(
                f"Self-heal batch result for {session_id} was empty "
                f"(likely errored/expired); skipping memory write"
            )
            return

        analysis = _parse_analysis(result_text)

        _save_learnings(session_id, analysis)

        if analysis.get("code_fixes"):
            _apply_code_fixes(analysis["code_fixes"])

        log.info(
            f"Self-heal batch completion: session {session_id} learnings saved "
            f"({len(analysis.get('learnings', []))} entries, "
            f"{len(analysis.get('code_fixes', []))} prompt patches)"
        )
    except Exception:
        log.exception(
            f"Self-heal batch completion handler raised for session {session_id}"
        )


def _save_learnings(session_id, analysis):
    """Save session learnings to the memory store."""
    today = date.today().isoformat()
    learnings = analysis.get("learnings", [])
    if not learnings:
        return

    notes = [f"# Session Learnings — {session_id} ({today})\n"]
    for l in learnings:
        notes.append(f"## {l.get('issue', 'Unknown issue')}")
        notes.append(f"- **Root cause:** {l.get('root_cause', 'Unknown')}")
        notes.append(f"- **Memory note:** {l.get('memory_note', 'None')}")
        if l.get("code_fix"):
            notes.append(f"- **Code fix:** {l['code_fix']}")
        notes.append("")

    content = "\n".join(notes)

    try:
        existing = None
        try:
            memories = client.beta.memory_stores.memories.list(
                HEALTH_STORE_ID,
                path_prefix="/system/learnings",
            )
            for m in memories.data:
                if m.path == "/system/learnings.md":
                    existing = m
                    break
        except Exception:
            pass

        if existing:
            current = client.beta.memory_stores.memories.retrieve(
                existing.id,
                memory_store_id=HEALTH_STORE_ID,
            )
            updated = current.content + f"\n---\n\n{content}"
            if len(updated) > 90000:
                updated = updated[-90000:]
            client.beta.memory_stores.memories.update(
                existing.id,
                memory_store_id=HEALTH_STORE_ID,
                content=updated,
            )
        else:
            client.beta.memory_stores.memories.create(
                HEALTH_STORE_ID,
                path="/system/learnings.md",
                content=content,
            )

        log.info(f"Self-heal: saved {len(learnings)} learnings to memory store")
    except Exception:
        log.exception("Failed to save learnings to memory store")


def _save_clean_session(session_id, session_type, tool_call_count):
    """Record a clean session for tracking success rate."""
    today = date.today().isoformat()
    try:
        existing = None
        try:
            memories = client.beta.memory_stores.memories.list(
                HEALTH_STORE_ID,
                path_prefix="/system/session_log",
            )
            for m in memories.data:
                if m.path == "/system/session_log.md":
                    existing = m
                    break
        except Exception:
            pass

        entry = f"\n- {today} | {session_id} | {session_type} | {tool_call_count} tool calls | clean"

        if existing:
            current = client.beta.memory_stores.memories.retrieve(
                existing.id,
                memory_store_id=HEALTH_STORE_ID,
            )
            updated = current.content + entry
            if len(updated) > 90000:
                updated = updated[-90000:]
            client.beta.memory_stores.memories.update(
                existing.id,
                memory_store_id=HEALTH_STORE_ID,
                content=updated,
            )
        else:
            client.beta.memory_stores.memories.create(
                HEALTH_STORE_ID,
                path="/system/session_log.md",
                content=f"# Session Log\n\nTrack session outcomes for self-improvement.\n{entry}",
            )
    except Exception:
        log.exception("Failed to log clean session")


def _apply_code_fixes(code_fixes):
    """Apply code fixes suggested by the analysis. Only touches system prompts in memory, not source code."""
    if not code_fixes:
        return

    today = date.today().isoformat()
    for fix in code_fixes:
        description = fix.get("description", "")
        change = fix.get("change", "")
        if not change:
            continue

        try:
            existing = None
            try:
                memories = client.beta.memory_stores.memories.list(
                    HEALTH_STORE_ID,
                    path_prefix="/system/prompt_patches",
                )
                for m in memories.data:
                    if m.path == "/system/prompt_patches.md":
                        existing = m
                        break
            except Exception:
                pass

            entry = (
                f"\n\n## Patch — {today}\n**Issue:** {description}\n**Fix:** {change}"
            )

            if existing:
                current = client.beta.memory_stores.memories.retrieve(
                    existing.id,
                    memory_store_id=HEALTH_STORE_ID,
                )
                updated = current.content + entry
                client.beta.memory_stores.memories.update(
                    existing.id,
                    memory_store_id=HEALTH_STORE_ID,
                    content=updated,
                )
            else:
                client.beta.memory_stores.memories.create(
                    HEALTH_STORE_ID,
                    path="/system/prompt_patches.md",
                    content=f"# System Prompt Patches\n\nAuto-generated improvements from session reviews.{entry}",
                )

            log.info(f"Self-heal: applied prompt patch — {description[:100]}")
        except Exception:
            log.exception(f"Failed to apply code fix: {description[:100]}")
