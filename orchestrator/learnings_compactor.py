"""Nightly compactor: collapse /system/learnings.md into a flat rules-by-tool list.

Plan #18 — close the readback gap. self_heal has been writing session
learnings to ``/system/learnings.md`` and ``/system/prompt_patches.md`` in
the health memory store since 2026-04, but ZERO agent prompts read either
file. 37 prompt patches and 11 learnings blocks have accumulated unread.

This module runs nightly at 00:30 PT, reads the verbose learnings ledger,
and asks Sonnet 4.6 to collapse it into a flat rules-by-tool list capped
at 4000 chars. The compact output lands at ``/system/learnings_compact.md``,
which every Specialist + Coordinator prompt now reads BEFORE its first
tool call.

Mirrors the prompt-cache pattern in ``self_heal.py``: ``cache_control:
{"type": "ephemeral"}`` on the system block so re-runs hit the 5m cache.
Mirrors the 409-conflict upsert in ``self_improve._save_to_memory`` so
double-runs on the same day don't crash — catch ``ConflictError``,
extract ``conflicting_memory_id``, fall back to ``memories.update``.

Never raises. On any failure path the function logs, admin-DMs, and
returns ``(input_chars, output_chars, tokens_used, False)``.
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

import anthropic

from _messages_usage import log_messages_usage
from config import ANTHROPIC_API_KEY, HEALTH_STORE_ID

log = logging.getLogger(__name__)

_COMPACTOR_MODEL = "claude-sonnet-4-6"

# Paths in the health memory store. The source is the unbounded ledger
# self_heal appends to; the destination is the bounded rules-by-tool list
# every Specialist + Coordinator now reads at session start.
_LEARNINGS_SOURCE_PATH = "/system/learnings.md"
_LEARNINGS_COMPACT_PATH = "/system/learnings_compact.md"

# Hard cap on compact output. The agent prompts read this file at the START
# of every session — a 30K-char file would add ~7.5K tokens of fixed overhead
# to every Coordinator + Specialist turn. 4K chars (~1K tokens) is enough for
# rules across the dozen tools we actually call.
_COMPACT_MAX_CHARS = 4000

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# System prompt sized to clear Sonnet's 1024-token prompt-caching floor.
# Without this the ``cache_control: ephemeral`` marker below silently no-ops
# at the API edge — same audit finding that drove the self_heal / self_improve
# expansion 2026-05-11 (see ``docs/proposals/cache-audit-2026-05-11.md``).
# Re-runs on the same source content hit the 5m cache and pay $0 for input.
_COMPACTOR_SYSTEM_PROMPT = (
    "You are a learnings compactor for the GTM Health Agent — a Slack-based "
    "GTM operations analyst built on Anthropic's Managed Agents API. The "
    "system runs eight orchestrated agents (Coordinator, Pipeline Monitor, "
    "Sales Process Monitor, Post-Sales Monitor, Statistician, Adversarial "
    "Reviewer, Cross-Domain Synthesizer, Chart Designer) that query "
    "Salesforce through dump_sf_query and the Railway Postgres snapshot "
    "through db_query, plus custom tools for charts, reports, and Slack "
    "posting.\n\n"
    "Every session, the post-run self-heal pipeline appends a verbose block "
    "to /system/learnings.md describing what went wrong, the root cause, and "
    "a memory note the agent should apply next time. After 11 sessions and "
    "37 prompt patches the file is too long for an agent to read every turn "
    "— 80K+ chars of timestamped blocks with overlapping notes. Your job is "
    "to collapse it into a flat rules-by-tool list the next session can "
    "consume in under a second.\n\n"
    "Tools the agents commonly call (group by these names):\n"
    "- db_query — read-only SQL against the Railway Postgres SF snapshot.\n"
    "- dump_sf_query — paginate a SOQL query, materialize to Parquet, return "
    "a compact handle. The ONLY path to live Salesforce as of Iteration 3.\n"
    "- query_artifact — DuckDB SQL against materialized Parquet/CSV files.\n"
    "- post_report — emit the structured final answer that lands in Slack.\n"
    "- send_slack_notification — content-free progress updates only.\n"
    "- generate_chart — QuickChart wrapper for visualizations.\n"
    "- (write_prose) — retired 2026-05-27; prose composition is now a "
    "multiagent delegation to the Writing Agent (in the Coordinator's "
    "roster), not a custom tool. Any learning that says \"call write_prose\" "
    "needs rewriting as \"delegate to the Writing Agent\".\n"
    "- search_knowledge_base — Kapa REST tool for Confluence + "
    "Jira + help-docs lookups.\n"
    "- materialize_xlsx — build an .xlsx from one or more Parquet handles.\n"
    "- soqlQuery / describeSObject — REMOVED from every sub-agent's tool "
    "registry in Iteration 3. Any learning that references these is stale; "
    "rewrite the rule against dump_sf_query.\n\n"
    "Output contract:\n"
    "- Group rules by tool name. Use ``## <tool>`` h2 headers, one per tool, "
    "in the order tools appear above.\n"
    "- Under each header, terse imperative bullets — ``- Always X`` or "
    "``- Never Y``. Each bullet ≤120 chars. Drop the verb-noun preamble "
    "where it adds nothing.\n"
    "- Drop duplicates aggressively. Two learnings that say the same thing "
    "in different words collapse to one rule. Three learnings about the "
    "same SOQL pitfall collapse to one rule that names the pitfall.\n"
    "- Drop one-off blips. If a rule only ever appeared in one session and "
    "nothing similar followed, leave it out — the signal-to-noise floor is "
    "≥2 occurrences for inclusion.\n"
    "- Drop stale rules. References to soqlQuery, describeSObject, or any "
    "API removed in Iteration 3 should be either rewritten against the "
    "current tool or dropped entirely if no longer applicable.\n"
    "- Total length must be under 4000 chars including headers. If you have "
    "more, cut the lowest-value rules — the agent reads this every session "
    "and verbosity is a tax on every turn.\n"
    "- Output plain markdown. No preamble (no ``Here is...``), no trailing "
    "commentary (no ``This list covers...``), no code fences. The file is "
    "consumed verbatim — anything outside the markdown body is wasted tokens.\n\n"
    "Style notes:\n"
    "- Imperative form. ``Always reuse a Parquet handle from dump_sf_query`` "
    "not ``You should reuse Parquet handles``.\n"
    "- Concrete. ``Never use CASE in SOQL — Salesforce rejects it`` not "
    "``Be careful with conditional logic``.\n"
    "- One thought per bullet. If two rules need a connector, they are two "
    "rules.\n"
    "- No second-person address. The reader is the agent, not a human. "
    "``Always probe access via db_query({sql: 'SELECT 1'})`` not ``You should "
    "always probe access...``.\n\n"
    "Good output vs bad output:\n"
    "GOOD: ``## dump_sf_query\\n- Always include explicit date ranges; "
    "queries without them time out on large tables.\\n- Never assume "
    "Iter2-era custom-field names — run describeSObject equivalent (FIELDS "
    "STANDARD) first.``\n"
    "BAD: ``Here are the things I learned about dump_sf_query. The agent "
    "should remember that it is important to include date ranges...`` — "
    "preamble + second person + verbose framing.\n"
    "BAD: a single bullet stacking three rules with semicolons — split.\n"
    "BAD: keeping a 2026-04 rule that says ``Use soqlQuery for schema "
    "discovery`` — soqlQuery is removed; rewrite or drop.\n\n"
    "Anti-patterns to filter out:\n"
    "- Apology bullets. ``- Apologize for the previous incorrect SOQL`` "
    "is operator-facing, not agent-facing — drop. The agent doesn't "
    "apologize to itself the next session.\n"
    "- Conversational rules. ``- Be more careful with date ranges`` is "
    "softer than the operator wants — rewrite as concrete: ``- Always "
    "specify date ranges with both lower and upper bounds in WHERE``.\n"
    "- Rules that reference a specific session id (``sesn_EXAMPLE...``) — the "
    "next session doesn't share state. Generalize the rule or drop.\n"
    "- Rules that reference a specific Slack thread_ts. Same reason.\n"
    "- Rules that contradict a more recent rule. Keep the most recent.\n"
    "- Rules that name a removed agent (Writing Agent v1, Report Writer, "
    "etc.). Drop. The current agent set is in CLAUDE.md.\n"
    "- Rules that name a removed tool (soqlQuery, describeSObject, "
    "directQuery_bypass). Drop or rewrite.\n"
    "- Multi-paragraph rationale. A rule that needs a paragraph to explain "
    "itself is two rules, or a code-fix proposal masquerading as a rule. "
    "Compress to a single imperative bullet or drop.\n\n"
    "Decision rules when two learnings conflict:\n"
    "- The more specific wins. ``Never use CASE in SOQL — Salesforce "
    "rejects it`` beats ``Be careful with conditional logic``.\n"
    "- The more recent wins on factual claims (e.g. tool registry "
    "changes). Iteration 3 (2026-05-12) removed soqlQuery; anything "
    "before that referencing it is stale.\n"
    "- The more observed wins on quantitative claims. ``Retry once on "
    "transient SF 5xx; second retry is wasted`` (observed 4 times) beats "
    "``Retry up to 3 times`` (observed once).\n\n"
    "Coverage check at end of output:\n"
    "- Every tool that has at least one referenced learning gets a "
    "``## <tool>`` header. Don't merge tools.\n"
    "- Every header has at least one bullet. Headers with zero bullets "
    "after deduplication should be dropped, not left empty.\n"
    "- Total chars under 4000 including headers, blank lines, and "
    "bullets. Count yourself before returning.\n\n"
    "When the source file is empty or unreadable, return a single line "
    "explaining there are no learnings to compact yet — the agent prompts "
    "treat that as a clean slate. Do NOT invent placeholder rules or "
    "echo the system prompt back; the compact file is consumed verbatim."
)


def _conflicting_memory_id(err: anthropic.ConflictError) -> Optional[str]:
    """Extract ``conflicting_memory_id`` from a 409 response body.

    Mirrors ``self_improve._conflicting_memory_id``. The Anthropic SDK puts
    the parsed JSON body on ``err.body``; the field we need lives under
    ``body["error"]["conflicting_memory_id"]``. Defensive against the
    transport path that surfaces the body as a raw string.
    """
    body = getattr(err, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            mid = error.get("conflicting_memory_id")
            if isinstance(mid, str) and mid:
                return mid
    return None


def _read_source_learnings() -> Optional[str]:
    """Read the verbose learnings ledger from the health memory store.

    Returns:
        - The file content as a string when the read succeeded.
        - ``""`` when the read succeeded but the file does not exist
          (first nightly run before any session learnings).
        - ``None`` when the read FAILED for any reason (transient API
          error, auth failure, etc.). Callers MUST distinguish None
          from "" because writing the empty-source placeholder on a
          transient failure would overwrite previously-compacted real
          content via the upsert path. Codex/Claude review 2026-05-14
          P1 finding.
    """
    try:
        memories = client.beta.memory_stores.memories.list(
            HEALTH_STORE_ID,
            path_prefix="/system/learnings",
        )
        for m in memories.data:
            if m.path == _LEARNINGS_SOURCE_PATH:
                current = client.beta.memory_stores.memories.retrieve(
                    m.id,
                    memory_store_id=HEALTH_STORE_ID,
                )
                return current.content or ""
        # List succeeded; the file genuinely does not exist yet.
        return ""
    except Exception:
        log.exception(
            "learnings_compactor: failed to read %s — returning None to "
            "signal transient failure (caller must NOT overwrite compact)",
            _LEARNINGS_SOURCE_PATH,
        )
        return None


def _write_compact(content: str) -> None:
    """Write the compacted output to ``/system/learnings_compact.md``.

    Idempotent within a day via the 409-conflict upsert pattern. Mirrors
    ``self_improve._save_to_memory``: try create first; on
    ``memory_path_conflict_error`` pull ``conflicting_memory_id`` from the
    error body and fall back to update with the same content. Safe to call
    when we KNOW we want to overwrite — used only on the normal compaction
    path where the source has real content.
    """
    try:
        client.beta.memory_stores.memories.create(
            HEALTH_STORE_ID,
            path=_LEARNINGS_COMPACT_PATH,
            content=content,
        )
    except anthropic.ConflictError as e:
        memory_id = _conflicting_memory_id(e)
        if not memory_id:
            raise
        client.beta.memory_stores.memories.update(
            memory_id,
            memory_store_id=HEALTH_STORE_ID,
            content=content,
        )
        log.info(
            "learnings_compactor: %s already existed; updated %s in place",
            _LEARNINGS_COMPACT_PATH,
            memory_id,
        )


def _create_compact_if_missing(content: str) -> bool:
    """Create ``/system/learnings_compact.md`` ONLY if it does not exist.

    Used ONLY on the empty-source placeholder path: if real compacted
    content already exists from a prior day, a 409 here means "leave
    the real content alone, do NOT overwrite with a placeholder." This
    is the safety guarantee against the transient-read-failure data-
    loss scenario flagged in the 2026-05-14 review.

    Returns True when a fresh placeholder was written, False when a
    pre-existing compact was preserved.
    """
    try:
        client.beta.memory_stores.memories.create(
            HEALTH_STORE_ID,
            path=_LEARNINGS_COMPACT_PATH,
            content=content,
        )
        return True
    except anthropic.ConflictError:
        # Pre-existing content wins. We never call ``update`` here —
        # that's the whole point of this helper vs ``_write_compact``.
        log.info(
            "learnings_compactor: %s already exists with prior content; "
            "leaving as-is (placeholder NOT written)",
            _LEARNINGS_COMPACT_PATH,
        )
        return False


def compact_learnings() -> Tuple[int, int, int, bool]:
    """Compact /system/learnings.md to a flat rules-by-tool list.

    Single Sonnet 4.6 call. Reads the verbose ledger, asks the model to
    collapse it into ``## <tool>`` blocks of terse imperative rules,
    writes the result to ``/system/learnings_compact.md``.

    Returns ``(input_chars, output_chars, tokens_used, success)`` for
    telemetry. Never raises — every failure path returns ``success=False``
    and admin-DMs.
    """
    source = _read_source_learnings()

    if source is None:
        # Transient read failure. The compact file may already hold real
        # content from a prior day — overwriting it here would destroy
        # that. Return without writing so the next nightly run retries.
        # Codex/Claude review 2026-05-14 P1 finding.
        log.warning(
            "learnings_compactor: source read failed; preserving any "
            "existing compact and returning success=False"
        )
        return (0, 0, 0, False)

    input_chars = len(source)

    if not source.strip():
        # First nightly run before any learnings exist, or self_heal has
        # never flushed. Use the create-only helper: if a real compact
        # already exists, we leave it alone rather than clobbering with
        # the placeholder. Codex/Claude review 2026-05-14 P1 fix.
        placeholder = (
            "# Learnings — Compact\n\n"
            "No session learnings have been compacted yet. Treat as a clean slate.\n"
        )
        try:
            wrote_fresh = _create_compact_if_missing(placeholder)
            if wrote_fresh:
                log.info(
                    "learnings_compactor: source empty; wrote placeholder to %s",
                    _LEARNINGS_COMPACT_PATH,
                )
                return (0, len(placeholder), 0, True)
            # Pre-existing compact preserved. Report success — the system
            # is in a healthy state, just nothing new to compact.
            return (0, 0, 0, True)
        except Exception:
            log.exception("learnings_compactor: failed to write placeholder")
            return (0, 0, 0, False)

    try:
        response = client.messages.create(
            model=_COMPACTOR_MODEL,
            max_tokens=2000,
            system=[
                {
                    "type": "text",
                    "text": _COMPACTOR_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Source — /system/learnings.md ({input_chars} chars):\n\n"
                        f"{source}\n\n"
                        f"Compact this into a flat rules-by-tool list per the "
                        f"contract. Cap at {_COMPACT_MAX_CHARS} chars total. "
                        f"Return ONLY the markdown body — no preamble, no fences."
                    ),
                }
            ],
        )
    except Exception:
        log.exception("learnings_compactor: Sonnet call failed")
        try:
            from slack_bot import send_notification

            send_notification(
                "watch",
                "Learnings compactor failed — agents will continue using yesterday's "
                "compact (if any) until the next 00:30 PT run",
                admin_only=True,
            )
        except Exception:
            log.debug("learnings_compactor: admin-DM also failed (non-fatal)")
        return (input_chars, 0, 0, False)

    try:
        log_messages_usage("learnings_compactor", _COMPACTOR_MODEL, response.usage)
    except Exception:
        log.exception("learnings_compactor: usage logging failed (non-fatal)")

    text = next((b.text for b in response.content if b.type == "text"), "").strip()
    if not text:
        log.warning("learnings_compactor: Sonnet returned empty text — aborting write")
        return (input_chars, 0, 0, False)

    # Enforce the hard 4K cap defensively — the model is asked to respect
    # it, but a single oversize run shouldn't poison the file for every
    # session that reads it next day.
    if len(text) > _COMPACT_MAX_CHARS:
        log.warning(
            "learnings_compactor: output %d chars exceeds %d cap — truncating",
            len(text),
            _COMPACT_MAX_CHARS,
        )
        text = text[:_COMPACT_MAX_CHARS]

    output_chars = len(text)
    tokens_used = (
        (getattr(response.usage, "input_tokens", 0) or 0)
        + (getattr(response.usage, "output_tokens", 0) or 0)
        + (getattr(response.usage, "cache_read_input_tokens", 0) or 0)
        + (getattr(response.usage, "cache_creation_input_tokens", 0) or 0)
    )

    try:
        _write_compact(text)
    except Exception:
        log.exception(
            "learnings_compactor: failed to write %s", _LEARNINGS_COMPACT_PATH
        )
        try:
            from slack_bot import send_notification

            send_notification(
                "watch",
                "Learnings compactor produced output but failed to write to "
                "memory store — yesterday's compact (if any) will still serve",
                admin_only=True,
            )
        except Exception:
            log.debug("learnings_compactor: admin-DM also failed (non-fatal)")
        return (input_chars, output_chars, tokens_used, False)

    log.info(
        "learnings_compactor: compacted %d chars -> %d chars (%d tokens)",
        input_chars,
        output_chars,
        tokens_used,
    )
    return (input_chars, output_chars, tokens_used, True)
