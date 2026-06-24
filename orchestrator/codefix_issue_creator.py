"""Weekly cron: promote self_heal ``code_fix`` observations to GitHub issues.

Plan #20 — close the outgoing side of the readback gap. Plan #18 (PR #185)
made every agent READ ``/system/learnings.md`` via a compacted rules file.
This module closes the OPPOSITE direction: the verbose ledger contains
``code_fix`` proposals (runtime SOQL validator, schema-drift guard, retry
budget tuning, etc.) that no one converts to engineering work. They pile
up unread.

This cron runs Saturday 09:30 PT — 30 minutes after the Task #19
prompt-patches promotion cron so the two don't share an Anthropic API
rate window. Reads ``/system/learnings.md`` from the health memory store,
batches a single Sonnet 4.6 categorization call across every block,
takes the blocks classified as ``code_fix``, fingerprints them, dedupes
against ``/system/codefix_issues_created.md``, and opens a GitHub issue
for each new fingerprint via ``gh issue create``.

Fingerprint scheme:
    sha256(normalized_error_pattern + "::" + file_path + "::" + proposed_action)

Normalization on each input:
- Lowercase
- Collapse repeated whitespace to single space
- Strip leading/trailing whitespace
- Strip session-id-shaped tokens (``sesn_EXAMPLE...``) and thread_ts timestamps

The fingerprint is 64 hex chars (full SHA-256). The ledger stores
``fingerprint, issue_url, created_at`` per row in markdown table format
so a human can read it.

Never raises. On any failure path the function logs, admin-DMs, and
returns ``(blocks_seen, issues_created, [issue_urls], False)``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import List, Optional, Tuple

import anthropic

from _messages_usage import log_messages_usage
from config import ANTHROPIC_API_KEY, HEALTH_STORE_ID
from github_issue_helper import create_gh_issue, list_open_issues_with_label

log = logging.getLogger(__name__)

_CLASSIFIER_MODEL = "claude-sonnet-4-6"

# Memory paths. Source is the verbose self_heal ledger; the applied ledger
# tracks fingerprints we've already created issues for so the cron is
# idempotent across runs.
_LEARNINGS_SOURCE_PATH = "/system/learnings.md"
_APPLIED_LEDGER_PATH = "/system/codefix_issues_created.md"

# GitHub labels every auto-created issue gets. Filter via:
#   gh issue list --label "auto,from-self-heal"
_ISSUE_LABELS = "auto,from-self-heal"

# Anthropic client. Module-level so tests can monkeypatch ``client.messages``
# the same way ``self_heal`` tests do.
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# System prompt sized above Sonnet's 1024-token prompt-caching floor so the
# ``cache_control: ephemeral`` marker below isn't decorative. Re-runs against
# stable source content hit the cache and pay $0 for the system block.
#
# The contract: input = the full ``/system/learnings.md`` content. Output =
# a single JSON array, one entry per block, ``{block_id, kind, fingerprint_terms}``.
# We do the fingerprinting in Python (deterministic), not in the model.
_CLASSIFIER_SYSTEM_PROMPT = (
    "You are a triage classifier for the GTM Health Agent's session-learnings "
    "ledger. The agent is a Slack-based PE-firm GTM operations analyst built "
    "on Anthropic's Managed Agents API. After every session, a post-run "
    "self-heal pipeline appends a verbose block to /system/learnings.md "
    "describing what went wrong, the root cause, a memory note, and "
    "optionally a code-fix proposal. Eleven sessions and 37 proposals later "
    "the file is 80K+ chars of blocks separated by ``---`` lines.\n\n"
    "Your job is to classify every block as exactly one of four kinds, so "
    "downstream automation can route each one to the right destination:\n\n"
    "- prompt_patch — the proposed action is a change to an agent's system "
    "  prompt (a memory note to add, a rule to encode, a tool-usage clause "
    "  to insert). These are handled by a separate cron that updates "
    "  agents/update_prompts.py — NOT your job here.\n"
    "- code_fix — the proposed action requires a code change in the Python "
    "  orchestrator. Examples: 'add a runtime SOQL validator that checks "
    "  for CASE/COALESCE/FLOOR before submitting', 'cap MCP result rows at "
    "  N before passing to the agent', 'change the session_runner retry "
    "  budget from 3 to 1 on 5xx', 'detect schema drift in db_query and "
    "  invalidate the cached describe'. This kind triggers a GitHub issue.\n"
    "- runbook — the proposed action is operational and not code. Examples: "
    "  'rotate the Kapa API key after the next outage', 'document the "
    "  Salesforce sandbox refresh process', 'add a Slack alert for the "
    "  09:00 PT canvas refresh'. These accumulate in memory; no automation "
    "  fires yet.\n"
    "- observation — no action proposed, or the block is a clean-session "
    "  record, or the model wrote ``code_fix: null`` and the memory_note "
    "  is purely informational. Skip.\n\n"
    "Heuristics to pick the right kind:\n"
    "- If the block has a populated ``Code fix:`` line AND that text "
    "  describes a change in an orchestrator/*.py file, a tool handler, a "
    "  retry policy, a validation check, or any other code construct: "
    "  ``code_fix``.\n"
    "- If the block has a populated ``Code fix:`` line AND that text "
    "  describes a change to a system prompt, a rule to add to "
    "  setup_agents.py, or a memory-note clause: ``prompt_patch``.\n"
    "- If the ``Code fix:`` line is missing, says ``null``, or the entire "
    "  block is a one-line memory_note with no action verb: ``observation``.\n"
    "- ``runbook`` is rare — only when the action is operational (ops "
    "  procedure, key rotation, monitoring config) and not editable in "
    "  this repo.\n\n"
    "Edge cases:\n"
    "- A block proposes BOTH a prompt change AND a code change: classify "
    "  as ``code_fix`` (the harder change wins; the prompt half can be "
    "  re-derived by a future operator reading the issue).\n"
    "- A block proposes ``add a logging statement so we can see X``: "
    "  ``code_fix`` (logging is code).\n"
    "- A block proposes ``the Coordinator should delegate to the Writing "
    "  Agent before post_report``: ``prompt_patch`` (Coordinator-prompt "
    "  rule, no Python edit needed).\n"
    "- A block proposes ``add tests for the regression we just hit``: "
    "  ``code_fix`` (tests are code).\n\n"
    "Fingerprint terms — for blocks classified as ``code_fix`` you MUST "
    "also return three normalization-friendly strings the Python side "
    "will hash together:\n"
    "- error_pattern: a short canonical phrase that names the failure "
    "  class. ``SOQL CASE rejected``, ``mcp result row overflow``, "
    "  ``writing agent delegation timeout``, ``schema cache stale``. "
    "  Lowercase, no session ids, no thread timestamps, no dates. Two "
    "  blocks that describe the same failure must produce the IDENTICAL "
    "  error_pattern string. This is the de-duplication backbone — "
    "  pick stable wording.\n"
    "- file_path: the orchestrator path the fix lands in. "
    "  ``orchestrator/session_runner.py``, ``orchestrator/db_query.py``, "
    "  ``orchestrator/self_heal.py``. If the block names multiple "
    "  files, pick the primary one. If unclear, return ``orchestrator/`` "
    "  (trailing slash, root-only). NEVER return absolute paths or "
    "  paths outside the repo.\n"
    "- proposed_action: a short canonical phrase that names what changes. "
    "  ``add runtime SOQL validator``, ``cap mcp result rows``, ``shrink "
    "  retry budget on 5xx``, ``invalidate describe cache on drift``. "
    "  Same stability requirement as error_pattern — two blocks proposing "
    "  the same change must collide.\n"
    "For all other kinds (prompt_patch, runbook, observation) return an "
    "empty object ``{}`` for fingerprint_terms — the Python side won't "
    "look at it.\n\n"
    "Block IDs — every block in the source starts with a heading line. "
    "Use the heading text up to the first newline as the block_id. If "
    "two blocks share a heading (rare), append `` #2``, `` #3`` to "
    "disambiguate so the output array entries stay 1:1 with input "
    "blocks. The Python side keys ledger writes by block_id so this "
    "stability matters.\n\n"
    "Output contract — return ONLY a JSON array. No preamble, no "
    "markdown fence, no trailing commentary. Each entry is exactly:\n"
    "{\n"
    '  "block_id": "<heading text of the block>",\n'
    '  "kind": "prompt_patch" | "code_fix" | "runbook" | "observation",\n'
    '  "fingerprint_terms": {\n'
    '    "error_pattern": "<lowercase canonical phrase>",\n'
    '    "file_path": "orchestrator/<file>.py",\n'
    '    "proposed_action": "<lowercase canonical phrase>"\n'
    "  } | {}\n"
    "}\n\n"
    "Anti-patterns to avoid in your output:\n"
    "- Do NOT paraphrase the block. Your output goes into a GitHub "
    "  issue VERBATIM via a separate Python step — your job is "
    "  classification + fingerprint, not copywriting.\n"
    "- Do NOT include session_ids, thread_ts, dates, or any "
    "  per-occurrence noise in the fingerprint_terms. Those break "
    "  deduplication.\n"
    "- Do NOT return ``code_fix`` if the proposed action is a memory "
    "  note. Memory notes are the agent talking to itself next session; "
    "  they're prompt_patches.\n"
    "- Do NOT skip a block. If the kind is unclear, classify as "
    "  ``observation`` (the no-op path). Skipping breaks the 1:1 "
    "  contract the Python side relies on.\n"
    "- Do NOT invent blocks. The output array length must equal the "
    "  number of blocks the model can identify in the source.\n\n"
    "Cost note: this prompt is sent with cache_control: ephemeral so "
    "the system block hits the cache on every Saturday re-run. The "
    "user message (the source ledger) changes daily, so we only pay "
    "for input on the new chars. Keep your output JSON compact — every "
    "token costs."
)


def _conflicting_memory_id(err: anthropic.ConflictError) -> Optional[str]:
    """Extract ``conflicting_memory_id`` from a 409 response body.

    Mirrors ``self_improve._conflicting_memory_id`` and
    ``learnings_compactor._conflicting_memory_id``.
    """
    body = getattr(err, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            mid = error.get("conflicting_memory_id")
            if isinstance(mid, str) and mid:
                return mid
    return None


def _read_memory_file(path: str) -> Optional[str]:
    """Read a memory file by path. Returns ``""`` if file missing, ``None`` on transient error."""
    try:
        memories = client.beta.memory_stores.memories.list(
            HEALTH_STORE_ID,
            path_prefix=path.rsplit("/", 1)[0] or "/",
        )
        for m in memories.data:
            if m.path == path:
                current = client.beta.memory_stores.memories.retrieve(
                    m.id,
                    memory_store_id=HEALTH_STORE_ID,
                )
                return current.content or ""
        return ""
    except Exception:
        log.exception("codefix_issue_creator: failed to read %s", path)
        return None


def _upsert_memory_file(path: str, content: str) -> None:
    """Create-or-update a memory file at ``path``. Idempotent via 409-upsert."""
    try:
        client.beta.memory_stores.memories.create(
            HEALTH_STORE_ID,
            path=path,
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


def _normalize(s: str) -> str:
    """Normalize a fingerprint input: lowercase, strip session ids / thread ts, collapse whitespace."""
    s = s.lower().strip()
    # Strip session-id-shaped tokens (``sesn_<base32-ish>``)
    s = re.sub(r"sesn_[a-z0-9]+", "", s)
    # Strip thread_ts-shaped tokens (``1234567890.123456``)
    s = re.sub(r"\b\d{10}\.\d{1,6}\b", "", s)
    # Strip ISO dates
    s = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", "", s)
    # Collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _fingerprint(error_pattern: str, file_path: str) -> str:
    """Compute the fingerprint over (normalized error pattern, file path).

    ``proposed_action`` was dropped from the key (was a 3-tuple) because the
    classifier's free-text action phrasing drifts run-to-run ("add a runtime
    soql validator" vs "add runtime soql validator"), minting a fresh
    fingerprint for the SAME root-cause bug. That is exactly how #303/#305 and
    #328/#330 escaped dedup. Keying on (error, file) collapses them.
    """
    payload = f"{_normalize(error_pattern)}::{_normalize(file_path)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _split_blocks(source: str) -> List[Tuple[str, str]]:
    """Split the source ledger into ``(block_id, block_text)`` tuples.

    The ledger uses ``---`` as a block separator (see ``self_heal._save_learnings``).
    Each block starts with a ``# Session Learnings — <session_id> (<date>)`` heading,
    then has ``## <issue>`` sub-headings per learning. We treat each ``##``
    sub-heading as a block — that's the granularity that maps 1:1 to a
    potential code_fix proposal.

    Returns ``[(block_id, block_text), ...]`` where ``block_id`` is the ``##``
    heading text and ``block_text`` is the heading + all bullet lines under
    it, up to the next ``##`` or ``---`` or EOF.

    Disambiguates duplicate headings by appending `` #2``, `` #3``, ... so
    the classifier's output array can stay 1:1 with input blocks.
    """
    blocks: List[Tuple[str, str]] = []
    seen_ids: dict[str, int] = {}
    lines = source.splitlines()
    i = 0
    current_id: Optional[str] = None
    current_buf: List[str] = []

    def _flush():
        nonlocal current_id, current_buf
        if current_id is not None and current_buf:
            text = "\n".join(current_buf).strip()
            if text:
                seen_ids[current_id] = seen_ids.get(current_id, 0) + 1
                if seen_ids[current_id] > 1:
                    disambiguated = f"{current_id} #{seen_ids[current_id]}"
                else:
                    disambiguated = current_id
                blocks.append((disambiguated, text))
        current_id = None
        current_buf = []

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        # Block boundary: ``## <heading>`` starts a new block; ``---`` flushes.
        if stripped.startswith("## "):
            _flush()
            current_id = stripped[3:].strip()
            current_buf = [line]
        elif stripped == "---":
            _flush()
        elif current_id is not None:
            current_buf.append(line)
        i += 1
    _flush()
    return blocks


def _classify_blocks(blocks: List[Tuple[str, str]]) -> Optional[List[dict]]:
    """Single Sonnet call to classify every block. Returns parsed JSON array or None on failure."""
    if not blocks:
        return []

    # Build the user message: numbered blocks for the model to walk through.
    user_lines = [
        f"Classify each of the following {len(blocks)} blocks from "
        f"/system/learnings.md. Return a JSON array with one entry per "
        f"block, in the same order. The block_id field MUST match the "
        f"heading I provide for each block.\n\n"
    ]
    for block_id, block_text in blocks:
        user_lines.append(f"--- BLOCK ID: {block_id} ---")
        user_lines.append(block_text)
        user_lines.append("")
    user_lines.append("Return ONLY the JSON array — no preamble, no markdown fence.")
    user_msg = "\n".join(user_lines)

    try:
        response = client.messages.create(
            model=_CLASSIFIER_MODEL,
            max_tokens=4000,
            system=[
                {
                    "type": "text",
                    "text": _CLASSIFIER_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception:
        log.exception("codefix_issue_creator: Sonnet classification call failed")
        return None

    try:
        log_messages_usage("codefix_issue_creator", _CLASSIFIER_MODEL, response.usage)
    except Exception:
        log.exception("codefix_issue_creator: usage logging failed (non-fatal)")

    text = next((b.text for b in response.content if b.type == "text"), "").strip()
    if not text:
        log.warning("codefix_issue_creator: Sonnet returned empty text")
        return None

    # Strip a leading code fence if Sonnet ignored the contract.
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)

    # Extract the first ``[ ... ]`` array.
    match = re.search(r"\[[\s\S]*\]", text)
    if not match:
        log.warning("codefix_issue_creator: no JSON array in Sonnet response")
        return None

    try:
        parsed = json.loads(match.group())
    except json.JSONDecodeError:
        log.exception("codefix_issue_creator: JSON parse failed on classifier output")
        return None

    if not isinstance(parsed, list):
        log.warning("codefix_issue_creator: classifier returned non-list JSON")
        return None
    return parsed


def _read_applied_fingerprints(content: Optional[str]) -> set[str]:
    """Parse the applied ledger and return the set of fingerprints already processed.

    The ledger is a markdown table; we tolerate any line starting with a
    64-char hex fingerprint (column 1). Defensive against future schema
    tweaks.
    """
    if not content:
        return set()
    fingerprints: set[str] = set()
    for line in content.splitlines():
        # Match a line that starts with ``| <64-hex> |`` or just ``<64-hex>``
        m = re.match(r"\s*\|?\s*([0-9a-f]{64})\b", line)
        if m:
            fingerprints.add(m.group(1))
    return fingerprints


def _build_issue_title(error_pattern: str) -> str:
    """Build a concise GitHub issue title from the error pattern."""
    title = error_pattern.strip()
    # Capitalize for readability; cap at 72 chars to play nice with terminals.
    title = title[:72].rstrip()
    return f"[auto] code_fix: {title}"


def _build_issue_body(block_id: str, block_text: str, fingerprint: str) -> str:
    """Build the GitHub issue body. The source learning block is included verbatim."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = (
        "Auto-created from self_heal session learnings. "
        "Do not close without addressing the underlying observation — "
        "see fingerprint dedup below.\n\n"
        "## Source learning block\n\n"
        f"_From `{_LEARNINGS_SOURCE_PATH}`, block `{block_id}`._\n\n"
        "```markdown\n"
        f"{block_text}\n"
        "```\n\n"
        "## Fingerprint\n\n"
        f"`{fingerprint}`\n\n"
        "If a duplicate observation arrives in a future session, this "
        "fingerprint prevents a duplicate issue. To force a re-open after "
        "you close this one, edit "
        f"`{_APPLIED_LEDGER_PATH}` in the health memory store and remove "
        "the row.\n\n"
        f"_Created: {timestamp}_\n"
        f"_Labels: `{_ISSUE_LABELS}`_\n"
    )
    return body


def _create_gh_issue(title: str, body: str) -> Optional[str]:
    """Shell out to ``gh issue create``. Returns the issue URL or None on failure.

    Thin wrapper around ``github_issue_helper.create_gh_issue`` — kept under
    its original module-private name so the existing test suite (which
    monkeypatches ``_create_gh_issue``) keeps working untouched. Task #24
    extracted the subprocess plumbing into the shared helper so the
    self_improve doc-drift auto-issue path can reuse it.
    """
    return create_gh_issue(title, body, _ISSUE_LABELS)


def _append_to_applied_ledger(
    existing: Optional[str], new_rows: List[Tuple[str, str]]
) -> str:
    """Append ``(fingerprint, issue_url)`` rows to the applied ledger markdown."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not existing or not existing.strip():
        header = (
            "# code_fix Issues Created\n\n"
            "Tracks fingerprints of self_heal learnings that have been "
            "promoted to GitHub issues. The cron in "
            "`orchestrator/codefix_issue_creator.py` reads this file before "
            "each run and skips any block whose fingerprint already appears.\n\n"
            "| fingerprint | issue_url | created_at |\n"
            "| --- | --- | --- |\n"
        )
        lines = [header.rstrip()]
    else:
        lines = [existing.rstrip()]
    for fingerprint, url in new_rows:
        lines.append(f"| {fingerprint} | {url} | {timestamp} |")
    return "\n".join(lines) + "\n"


def _admin_dm(message: str) -> None:
    """Best-effort admin Slack DM. Swallowed so it can't break the cron."""
    try:
        from slack_bot import send_notification

        send_notification("watch", message, admin_only=True)
    except Exception:
        log.debug("codefix_issue_creator: admin-DM failed (non-fatal)")


def create_issues_from_learnings() -> Tuple[int, int, List[str], bool]:
    """Promote code_fix observations in /system/learnings.md to GitHub issues.

    Returns ``(blocks_seen, issues_created, [issue_urls], success)``.

    Success semantics:
    - ``True`` when the source read succeeded AND classification succeeded.
      Per-issue ``gh issue create`` failures are tolerated as partial
      success — ledger is updated only for the fingerprints that landed,
      and admin is DMed about the failures.
    - ``False`` when the source read failed (transient memory store error)
      OR Sonnet classification failed. The ledger is NOT updated in either
      case so the next run gets a fresh shot.
    - Empty source → no-op success (no blocks, no issues, no DM).

    Never raises.
    """
    source = _read_memory_file(_LEARNINGS_SOURCE_PATH)
    if source is None:
        log.warning(
            "codefix_issue_creator: source read failed — preserving ledger "
            "and returning success=False so next run retries"
        )
        _admin_dm(
            "Code-fix issue creator failed to read /system/learnings.md — "
            "no issues created this run; next Saturday will retry."
        )
        return (0, 0, [], False)

    if not source.strip():
        log.info("codefix_issue_creator: source empty — nothing to promote")
        return (0, 0, [], True)

    blocks = _split_blocks(source)
    if not blocks:
        log.info("codefix_issue_creator: no parseable blocks in source")
        return (0, 0, [], True)

    log.info("codefix_issue_creator: classifying %d blocks", len(blocks))
    classifications = _classify_blocks(blocks)
    if classifications is None:
        _admin_dm(
            "Code-fix issue creator: Sonnet classification call failed — "
            "no issues created this run; ledger preserved."
        )
        return (len(blocks), 0, [], False)

    # Map block_id → block_text for verbatim issue body assembly.
    block_text_by_id = {bid: btxt for bid, btxt in blocks}

    applied_ledger_content = _read_memory_file(_APPLIED_LEDGER_PATH)
    if applied_ledger_content is None:
        # Transient read failure on the ledger means we don't know what's
        # already created. Bail rather than risk duplicating every issue.
        _admin_dm(
            "Code-fix issue creator: failed to read applied ledger — "
            "skipping issue creation this run to avoid duplicates."
        )
        return (len(blocks), 0, [], False)

    seen_fingerprints = _read_applied_fingerprints(applied_ledger_content)

    new_rows: List[Tuple[str, str]] = []
    issue_urls: List[str] = []
    gh_failures: List[str] = []

    # Track fingerprints we mint within THIS run too, so two identical
    # observations in the same source don't both create issues.
    minted_this_run: set[str] = set()

    # Dedup against issues ALREADY OPEN on GitHub (#303/#305, #328/#330). The
    # ledger only remembers what THIS pipeline created; an issue opened on a
    # prior week (or closed-then-reopened, or hand-filed) was invisible, so the
    # same bug got re-filed every Saturday. Fetch open titles once and skip any
    # match. Returns [] on gh failure — a transient error must not block the
    # cron, just degrade dedup to ledger-only for this run.
    open_issue_titles = {
        t.strip().lower() for t in list_open_issues_with_label(_ISSUE_LABELS)
    }

    for entry in classifications:
        if not isinstance(entry, dict):
            continue
        if entry.get("kind") != "code_fix":
            continue
        terms = entry.get("fingerprint_terms") or {}
        error_pattern = (terms.get("error_pattern") or "").strip()
        file_path = (terms.get("file_path") or "").strip()
        if not (error_pattern and file_path):
            log.info(
                "codefix_issue_creator: skipping code_fix block %r with "
                "incomplete fingerprint terms",
                entry.get("block_id"),
            )
            continue

        fingerprint = _fingerprint(error_pattern, file_path)
        if fingerprint in seen_fingerprints or fingerprint in minted_this_run:
            log.info(
                "codefix_issue_creator: fingerprint %s already created — skipping",
                fingerprint[:16],
            )
            continue

        title = _build_issue_title(error_pattern)
        if title.strip().lower() in open_issue_titles:
            log.info(
                "codefix_issue_creator: issue %r already open on GitHub — skipping",
                title,
            )
            continue

        block_id = entry.get("block_id") or "unknown"
        block_text = block_text_by_id.get(block_id, "")
        if not block_text:
            log.warning(
                "codefix_issue_creator: classifier referenced unknown "
                "block_id %r — skipping",
                block_id,
            )
            continue

        body = _build_issue_body(block_id, block_text, fingerprint)
        url = _create_gh_issue(title, body)
        if url is None:
            gh_failures.append(block_id)
            continue

        minted_this_run.add(fingerprint)
        new_rows.append((fingerprint, url))
        issue_urls.append(url)
        log.info(
            "codefix_issue_creator: created issue %s for fingerprint %s",
            url,
            fingerprint[:16],
        )

    if new_rows:
        updated_ledger = _append_to_applied_ledger(applied_ledger_content, new_rows)
        try:
            _upsert_memory_file(_APPLIED_LEDGER_PATH, updated_ledger)
        except Exception:
            log.exception(
                "codefix_issue_creator: failed to write applied ledger — "
                "issues were created but dedup is degraded until next run"
            )
            _admin_dm(
                f"Code-fix issue creator created {len(new_rows)} issues but "
                f"failed to update the applied ledger. Next run may create "
                f"duplicates. Manually check /system/codefix_issues_created.md."
            )
            # Treat the run as partial-success — issues exist, ledger lags.
            return (len(blocks), len(new_rows), issue_urls, False)

    if gh_failures:
        _admin_dm(
            f"Code-fix issue creator: gh issue create failed for "
            f"{len(gh_failures)} block(s); succeeded for {len(new_rows)}. "
            f"Failed blocks: {', '.join(gh_failures[:5])}"
        )
        # Partial success — return False so the run is visible as degraded.
        return (len(blocks), len(new_rows), issue_urls, False)

    return (len(blocks), len(new_rows), issue_urls, True)
