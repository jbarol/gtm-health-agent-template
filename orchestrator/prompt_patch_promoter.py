"""Weekly cron: promote un-applied prompt_patch entries into a draft GitHub PR.

Background
----------
``self_heal._apply_code_fixes`` and (rarely) ``self_improve`` write proposed
system-prompt changes to ``/system/prompt_patches.md`` in the Anthropic
memory store. A 2026-05-14 audit found 37 patches accumulated over a week
with ZERO landed in ``agents/setup_agents.py`` — patches sit unread in
memory. Task #18 (PR #185) closes the "agents READ their own learnings"
gap. This module (Task #19) closes the "patches get applied to source"
gap by:

  1. Reading ``/system/prompt_patches.md`` from the health memory store.
  2. Parsing it into discrete blocks separated by ``## Patch — <date>`` headers.
  3. Fingerprinting each block (sha256 of normalized content).
  4. Filtering out fingerprints already in ``/system/prompt_patches_applied.md``.
  5. Asking Sonnet 4.6 for a set of search/replace edits (JSON
     ``{old_string, new_string}``) against ``agents/setup_agents.py``
     covering all un-applied patches. (Was a unified diff until 2026-06-24
     — LLM-authored diffs with miscounted ``@@`` hunk headers failed
     ``git apply`` every weekly run; search/replace applies deterministically
     in Python and cannot be corrupted by line-count drift.)
  6. Applying the edits to the file in-process (each old_string must match
     exactly once, else abort).
  7. Opening a draft PR via ``gh pr create``.
  8. Marking the batch's fingerprints in the applied ledger.

Constraints
-----------
- DRAFT PR only. The existing ``deploy-prompts.yml`` workflow + the
  ``prompt-author-verified`` label gate (Plan #42 PR3) still apply on merge.
- Never modifies ``agents/setup_agents.py`` directly. The PR is the only
  surface that touches source.
- Patches promoted but REJECTED (PR closed unmerged) stay in the applied
  ledger to avoid re-opening the same PR forever. Operator clears manually.
- ``promote_prompt_patches`` never raises. Every failure path returns
  ``success=False`` + admin DM.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
from datetime import date
from pathlib import Path
from typing import Optional, Tuple

import anthropic

from _messages_usage import log_messages_usage
import cost_collector
from config import ANTHROPIC_API_KEY, HEALTH_STORE_ID

log = logging.getLogger(__name__)

_MODEL = "claude-sonnet-4-6"

# Memory paths.
PATCHES_PATH = "/system/prompt_patches.md"
APPLIED_PATH = "/system/prompt_patches_applied.md"

# Repo paths used by the gh CLI workflow. Resolved at call time so tests can
# monkeypatch them without import-time side effects.
ORCHESTRATOR_DIR = Path(__file__).parent
REPO_ROOT = ORCHESTRATOR_DIR.parent
SETUP_AGENTS_REL = "agents/setup_agents.py"

# Branch + PR title format. Title carries the patch count so operators can
# triage the queue at a glance.
_BRANCH_PREFIX = "auto/prompt-patches"


client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def promote_prompt_patches() -> Tuple[int, int, Optional[str], bool]:
    """Promote un-applied prompt_patch entries into a draft GitHub PR.

    Returns:
        (patches_seen, patches_applied, pr_url, success)

        - ``patches_seen``: total blocks parsed from ``prompt_patches.md``.
        - ``patches_applied``: number of un-applied blocks promoted in this
          run (0 if nothing to do, 0 on every failure path).
        - ``pr_url``: HTTPS URL of the draft PR, or None.
        - ``success``: True iff the run reached a terminal good state —
          either "nothing to do" or "PR opened". Any error (Sonnet call,
          diff apply, gh CLI, etc.) returns False.

    Never raises. Catastrophic failures DM admins via
    ``slack_bot.send_notification(admin_only=True)``.
    """
    log.info("Prompt-patch promoter: starting weekly run")

    try:
        patches_md = _read_memory(PATCHES_PATH)
    except Exception:
        log.exception("Failed to read prompt_patches.md from memory store")
        _admin_dm(
            "prompt-patch promoter: failed to read /system/prompt_patches.md "
            "from memory store. See Railway logs."
        )
        return (0, 0, None, False)

    if not patches_md or not patches_md.strip():
        log.info("Prompt-patch promoter: no patches in memory; nothing to do")
        return (0, 0, None, True)

    blocks = _parse_patch_blocks(patches_md)
    if not blocks:
        log.info("Prompt-patch promoter: file present but no parseable blocks")
        return (0, 0, None, True)

    try:
        applied_md = _read_memory(APPLIED_PATH) or ""
    except Exception:
        log.exception("Failed to read prompt_patches_applied.md from memory store")
        _admin_dm(
            "prompt-patch promoter: failed to read applied-ledger from "
            "memory store. See Railway logs."
        )
        return (len(blocks), 0, None, False)

    already_applied = _parse_applied_fingerprints(applied_md)

    pending = [b for b in blocks if b["fingerprint"] not in already_applied]
    log.info(
        "Prompt-patch promoter: %d total, %d already applied, %d pending",
        len(blocks),
        len(blocks) - len(pending),
        len(pending),
    )

    if not pending:
        return (len(blocks), 0, None, True)

    try:
        edits = _ask_sonnet_for_edits(pending)
    except Exception:
        log.exception("Sonnet edit request failed")
        _admin_dm(
            f"prompt-patch promoter: Sonnet edit request failed for "
            f"{len(pending)} pending patches. See Railway logs."
        )
        return (len(blocks), 0, None, False)

    if not edits:
        log.warning("Sonnet returned no usable edits; nothing to do")
        _admin_dm(
            f"prompt-patch promoter: Sonnet returned no usable edits for "
            f"{len(pending)} pending patches. Applied ledger NOT updated "
            f"— next run will retry."
        )
        return (len(blocks), 0, None, False)

    # Apply the edits to setup_agents.py + open the draft PR.
    try:
        pr_url = _apply_edits_and_open_pr(edits, pending)
    except _PromoterError as e:
        log.warning("Promoter aborted: %s", e)
        _admin_dm(f"prompt-patch promoter: {e}. See Railway logs.")
        return (len(blocks), 0, None, False)
    except Exception:
        log.exception("Unexpected error opening PR")
        _admin_dm(
            "prompt-patch promoter: unexpected error opening PR. See Railway logs."
        )
        return (len(blocks), 0, None, False)

    if not pr_url:
        # _apply_edits_and_open_pr should have raised; defensive guard.
        return (len(blocks), 0, None, False)

    try:
        _mark_applied(applied_md, pending, pr_url)
    except Exception:
        log.exception("Failed to update applied ledger; PR is open at %s", pr_url)
        _admin_dm(
            f"prompt-patch promoter: PR opened at {pr_url} but applied "
            f"ledger update FAILED — re-running may re-open the same PR. "
            f"Manually append fingerprints to {APPLIED_PATH}."
        )
        return (len(blocks), len(pending), pr_url, False)

    _admin_dm(
        f"prompt-patch promoter: opened draft PR with {len(pending)} patches.\n{pr_url}"
    )
    log.info("Prompt-patch promoter: PR opened at %s", pr_url)
    return (len(blocks), len(pending), pr_url, True)


# ---------------------------------------------------------------------------
# Memory I/O
# ---------------------------------------------------------------------------


def _read_memory(path: str) -> Optional[str]:
    """Read a memory file by exact path. Returns None if absent."""
    try:
        memories = client.beta.memory_stores.memories.list(
            HEALTH_STORE_ID,
            path_prefix=path.rsplit(".md", 1)[0],
        )
    except Exception:
        log.exception("memory_stores.memories.list failed for %s", path)
        raise

    for m in memories.data:
        if m.path == path:
            retrieved = client.beta.memory_stores.memories.retrieve(
                m.id,
                memory_store_id=HEALTH_STORE_ID,
            )
            return retrieved.content
    return None


def _write_memory(path: str, content: str) -> None:
    """Upsert a memory file by exact path."""
    existing = None
    try:
        memories = client.beta.memory_stores.memories.list(
            HEALTH_STORE_ID,
            path_prefix=path.rsplit(".md", 1)[0],
        )
        for m in memories.data:
            if m.path == path:
                existing = m
                break
    except Exception:
        log.exception("memory_stores.memories.list failed for %s", path)

    if existing:
        client.beta.memory_stores.memories.update(
            existing.id,
            memory_store_id=HEALTH_STORE_ID,
            content=content,
        )
    else:
        client.beta.memory_stores.memories.create(
            HEALTH_STORE_ID,
            path=path,
            content=content,
        )


# ---------------------------------------------------------------------------
# Patch parsing + fingerprinting
# ---------------------------------------------------------------------------


# self_heal._apply_code_fixes writes blocks shaped like:
#   ## Patch — 2026-05-12
#   **Issue:** ...
#   **Fix:** ...
_PATCH_HEADER_RE = re.compile(r"^##\s+Patch\s+—.*$", re.MULTILINE)


def _parse_patch_blocks(md: str) -> list[dict]:
    """Split the patches file into discrete blocks.

    Each block is the text between ``## Patch — <date>`` headers
    (header included). Returns a list of dicts:
        {"fingerprint": "<sha256>", "content": "<block text>"}

    Blocks above the first header (e.g. the file's title) are skipped.
    """
    matches = list(_PATCH_HEADER_RE.finditer(md))
    if not matches:
        return []

    blocks: list[dict] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(md)
        text = md[start:end].rstrip()
        if not text.strip():
            continue
        fp = _fingerprint(text)
        blocks.append({"fingerprint": fp, "content": text})
    return blocks


def _fingerprint(text: str) -> str:
    """Stable sha256 of the patch block.

    Whitespace is normalized so a re-render with different line breaks
    still dedupes against the prior fingerprint.
    """
    normalized = "\n".join(line.rstrip() for line in text.strip().splitlines())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# Applied-ledger lines look like:
#   - <fp> | <iso date> | <pr_url>
_APPLIED_LINE_RE = re.compile(r"^-\s+([0-9a-f]{64})\b", re.MULTILINE)


def _parse_applied_fingerprints(md: str) -> set[str]:
    return set(_APPLIED_LINE_RE.findall(md or ""))


def _mark_applied(existing_applied_md: str, pending: list[dict], pr_url: str) -> None:
    """Append fingerprints of promoted patches to the applied ledger."""
    today = date.today().isoformat()
    lines = []
    if not existing_applied_md or not existing_applied_md.strip():
        lines.append(
            "# Prompt Patches Applied\n\n"
            "Fingerprints of prompt-patch blocks that have been promoted "
            "to a draft PR. Operators clear manually to allow a retry.\n"
        )
        lines.append(existing_applied_md or "")
    else:
        lines.append(existing_applied_md.rstrip())
        lines.append("")

    for b in pending:
        lines.append(f"- {b['fingerprint']} | {today} | {pr_url}")
    lines.append("")

    _write_memory(APPLIED_PATH, "\n".join(lines))


# ---------------------------------------------------------------------------
# Sonnet diff request
# ---------------------------------------------------------------------------


# >1024 tokens so the ephemeral cache marker actually takes effect on Sonnet.
# Mirrors the size discipline documented in self_heal._SELF_HEAL_SYSTEM_PROMPT
# and self_improve._SELF_IMPROVE_SYSTEM_PROMPT.
_PROMOTER_SYSTEM_PROMPT = (
    "You are a senior platform engineer reviewing accumulated system-prompt "
    'improvement notes ("prompt patches") and producing ONE coherent '
    "unified diff against ``agents/setup_agents.py`` that lands the "
    "improvements as system-prompt edits in the live source.\n\n"
    "Context — what ``agents/setup_agents.py`` is and how it's structured:\n"
    "The file defines 8 production agents on the Anthropic Managed Agents "
    'API via ``client.beta.agents.create(name=..., model=..., system="""\\\n'
    '    ...multi-line prompt...\\n    """, tools=..., mcp_servers=...)``. '
    "The 8 agents:\n"
    "  1. Dream Agent (claude-sonnet-4-6) — nightly hypothesis planner. "
    "     ``dream_agent = client.beta.agents.create(...)``.\n"
    "  2. Pipeline Monitor (claude-sonnet-4-6) — Lead/MQL/SQL specialist. "
    "     ``pipeline_monitor = client.beta.agents.create(...)``.\n"
    "  3. Sales Process Monitor (claude-sonnet-4-6) — Opp-flow specialist. "
    "     ``sales_monitor = client.beta.agents.create(...)``.\n"
    "  4. Post-Sales Monitor (claude-sonnet-4-6) — Retention/renewal "
    "     specialist. ``postsales_monitor = client.beta.agents.create(...)``.\n"
    "  5. Coordinator (claude-opus-4-8) — orchestrates sub-agents, runs "
    "     validation, delegates to the Writing Agent (via the multiagent "
    "     runtime) before post_report. "
    "     ``coordinator = client.beta.agents.create(...)``.\n"
    "  6. Statistician (claude-opus-4-8) — PhD-level quantitative "
    "     validation. ``statistician = client.beta.agents.create(...)``.\n"
    "  7. Adversarial Reviewer (claude-opus-4-8) — five-check rebutter. "
    "     ``adversarial_reviewer = client.beta.agents.create(...)``.\n"
    "  8. Cross-Domain Synthesizer (claude-opus-4-8) — pattern namer. "
    "     ``cross_domain = client.beta.agents.create(...)``.\n"
    "Plus: Chart Designer, Writing Agent, Quick Answer, Prompt Engineer — "
    "but those are typically owned by sibling provision_*.py scripts and "
    "are NOT the primary target. Default to editing one of the 8 above.\n\n"
    "Each system prompt is a triple-quoted Python string literal with "
    "leading whitespace per line (the file is indented inside a "
    "``def`` block in some places, top-level in others).\n\n"
    "Patch format you'll receive (concatenated from "
    "``/system/prompt_patches.md`` in the Anthropic memory store):\n"
    "  ## Patch — <YYYY-MM-DD>\n"
    "  **Issue:** <one-line problem>\n"
    "  **Fix:** <one-paragraph proposed change>\n\n"
    "Your job is to:\n"
    "  1. Read all pending patches.\n"
    "  2. De-duplicate near-identical fixes (e.g. three patches that all "
    '     say "verify MCP access by calling soqlQuery, not by filesystem '
    '     inspection" — collapse to one prompt edit).\n'
    "  3. Identify the target agent for each surviving fix. If the patch "
    "     names an agent (Pipeline Monitor, Coordinator, etc.), use that. "
    "     If it doesn't, infer from the Issue text — pipeline-related "
    "     issues go to Pipeline Monitor, opp-flow to Sales Process "
    "     Monitor, retention to Post-Sales Monitor, orchestration to "
    "     Coordinator, statistics to Statistician.\n"
    "  4. Produce a set of search/replace edits against "
    "     ``agents/setup_agents.py``.\n\n"
    "Output format — return ONLY a JSON array, no preamble, no markdown "
    "fence, no trailing commentary:\n"
    '  [{"old_string": "<exact text to find>", "new_string": '
    '"<replacement>"}, ...]\n\n'
    "Rules for each edit (these REPLACE the old unified-diff contract — "
    "an LLM-authored diff with miscounted ``@@`` hunk headers was failing "
    "``git apply`` every run; search/replace is applied deterministically "
    "in Python and cannot be corrupted by line-count drift):\n"
    "  - ``old_string`` MUST be copied VERBATIM from the file, including "
    "    every leading space of indentation. Copy enough surrounding "
    "    context that ``old_string`` appears EXACTLY ONCE in the file — "
    "    a non-unique or not-found ``old_string`` is rejected and the "
    "    whole run aborts.\n"
    "  - ``new_string`` is the full replacement for that exact span, with "
    "    the same indentation style.\n"
    '  - Edit ONLY the ``system="""\\n..."""`` prompt strings — never the '
    "    ``tools=``, ``mcp_servers=``, ``model=``, or ``name=`` arguments.\n"
    "  - Additive edits only when possible: keep ``old_string`` small and "
    "    fold the new rule into the existing text in ``new_string``. Never "
    "    delete a rule no pending patch mentions.\n"
    "  - Group related additions under existing section headers in the "
    "    prompt (e.g. ``## Verifying tool access``) rather than appending "
    "    at the bottom.\n"
    "  - Do NOT add a new agent (``client.beta.agents.create(...)`` block).\n\n"
    "When in doubt, produce fewer, smaller edits. Two well-grounded edits "
    "that apply cleanly beat ten that miss their anchor and abort the run."
)


def _ask_sonnet_for_edits(pending: list[dict]) -> list[dict]:
    """Ask Sonnet 4.6 for search/replace edits. Returns a list of
    ``{"old_string", "new_string"}`` dicts (empty list if none usable)."""
    user_message = (
        f"Pending patches ({len(pending)} total). Produce search/replace "
        f"edits against ``agents/setup_agents.py`` as a JSON array:\n\n"
        + "\n\n".join(b["content"] for b in pending)
    )

    response = client.messages.create(
        model=_MODEL,
        max_tokens=8000,
        system=[
            {
                "type": "text",
                "text": _PROMOTER_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_message}],
    )

    try:
        log_messages_usage("Prompt-patch promoter", _MODEL, response.usage)
    except Exception:
        log.exception("Failed to log promoter usage (non-fatal)")
    try:
        cost_collector.track_messages_call(
            call_site="prompt_patch_promoter._ask_sonnet_for_edits",
            model=_MODEL,
            usage=response.usage,
        )
    except Exception:
        log.exception("Failed to persist promoter cost row (non-fatal)")

    text = next((b.text for b in response.content if b.type == "text"), "")
    return _parse_edits(text)


def _strip_fences(text: str) -> str:
    """Strip a leading ``` fence if Sonnet ignores the no-fence instruction."""
    s = text.strip()
    if s.startswith("```"):
        # Drop the first line (```json or ```), and the trailing ```.
        first_nl = s.find("\n")
        if first_nl >= 0:
            s = s[first_nl + 1 :]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3].rstrip()
    return s


def _parse_edits(text: str) -> list[dict]:
    """Parse a JSON array of ``{old_string, new_string}`` edits from the
    model's text. Tolerant of fences and surrounding prose: extracts the
    outermost ``[...]`` span. Returns [] if nothing parseable, and drops any
    entry missing either key."""
    s = _strip_fences(text)
    start = s.find("[")
    end = s.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(s[start : end + 1])
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    edits: list[dict] = []
    for item in data:
        if (
            isinstance(item, dict)
            and isinstance(item.get("old_string"), str)
            and isinstance(item.get("new_string"), str)
        ):
            edits.append(
                {"old_string": item["old_string"], "new_string": item["new_string"]}
            )
    return edits


# ---------------------------------------------------------------------------
# Diff application + gh PR creation
# ---------------------------------------------------------------------------


class _PromoterError(Exception):
    """User-friendly failure mode that maps to an admin DM."""


def _read_setup_agents() -> str:
    """Read agents/setup_agents.py. Thin wrapper so tests can monkeypatch."""
    return (REPO_ROOT / SETUP_AGENTS_REL).read_text(encoding="utf-8")


def _write_setup_agents(text: str) -> None:
    """Write agents/setup_agents.py. Thin wrapper so tests can monkeypatch."""
    (REPO_ROOT / SETUP_AGENTS_REL).write_text(text, encoding="utf-8")


def _apply_edits_to_text(source: str, edits: list[dict]) -> str:
    """Apply search/replace ``edits`` to ``source`` deterministically.

    Each ``old_string`` must occur EXACTLY ONCE — zero matches means the
    prompt text drifted, more than one means the anchor is ambiguous. Either
    way we raise ``_PromoterError`` rather than guess, so a bad edit aborts the
    run cleanly (and the applied ledger is left untouched for a retry). This is
    the whole point of moving off unified diffs: the apply step can no longer
    be corrupted by an LLM miscounting hunk line numbers.
    """
    updated = source
    for i, edit in enumerate(edits, start=1):
        old = edit.get("old_string", "")
        new = edit.get("new_string", "")
        if not old:
            raise _PromoterError(
                f"edit #{i} has an empty old_string — no anchor to locate. "
                f"Applied ledger NOT updated."
            )
        count = updated.count(old)
        if count == 0:
            raise _PromoterError(
                f"edit #{i} old_string not found in setup_agents.py (prompt "
                f"text drifted). Applied ledger NOT updated — next run retries."
            )
        if count > 1:
            raise _PromoterError(
                f"edit #{i} old_string matches {count} locations — ambiguous, "
                f"refusing to guess. Applied ledger NOT updated."
            )
        updated = updated.replace(old, new, 1)
    return updated


def _apply_edits_and_open_pr(edits: list[dict], pending: list[dict]) -> str:
    """Apply the search/replace edits in a fresh branch and open a draft PR.

    Workflow:
        edits applied to setup_agents.py in-process (deterministic)
        git checkout -b <branch>
        write setup_agents.py
        git add agents/setup_agents.py
        git commit -m "[auto] ..."
        git push -u origin <branch>
        gh pr create --draft ...

    Raises ``_PromoterError`` on any failure with operator-friendly text.
    """
    branch = _branch_name()
    body = _pr_body(pending)
    title = _pr_title(pending)

    source = _read_setup_agents()
    updated = _apply_edits_to_text(source, edits)  # raises on miss/ambiguity
    if updated == source:
        raise _PromoterError(
            "edits produced no change to setup_agents.py. Applied ledger NOT updated."
        )

    # 1. Branch off main, then write the mutated file.
    _run_git(["checkout", "-b", branch])
    _write_setup_agents(updated)

    # 2. Stage + commit.
    _run_git(["add", SETUP_AGENTS_REL])
    _run_git(["commit", "-m", title])

    # 3. Push.
    _run_git(["push", "-u", "origin", branch])

    # 4. Open draft PR via gh.
    return _gh_pr_create(branch, title, body)


def _branch_name() -> str:
    today = date.today().isoformat()
    # Include a short hash of the current time so two same-day runs don't
    # collide on the branch name (rare but observed when RUN_NIGHTLY_NOW
    # fires twice across a container restart).
    suffix = hashlib.sha256(str(date.today()).encode()).hexdigest()[:6]
    return f"{_BRANCH_PREFIX}/{today}-{suffix}"


def _pr_title(pending: list[dict]) -> str:
    """``[auto] prompt patches: <agent_short_names> (N patches)``"""
    agents = _infer_agent_short_names(pending)
    agent_str = ",".join(agents) if agents else "various"
    return f"[auto] prompt patches: {agent_str} ({len(pending)} patches)"


_AGENT_NAME_PATTERNS = [
    (re.compile(r"\bcoordinator\b", re.I), "coordinator"),
    (re.compile(r"\bpipeline\s*monitor\b", re.I), "pipeline"),
    (re.compile(r"\bsales\s*(process\s*)?monitor\b", re.I), "sales"),
    (re.compile(r"\bpost[-\s]*sales\s*monitor\b", re.I), "postsales"),
    (re.compile(r"\bstatistician\b", re.I), "statistician"),
    (re.compile(r"\badversarial\s*reviewer\b", re.I), "adversarial"),
    (re.compile(r"\bcross[-\s]*domain\b", re.I), "cross-domain"),
    (re.compile(r"\bdream\s*(agent)?\b", re.I), "dream"),
]


def _infer_agent_short_names(pending: list[dict]) -> list[str]:
    """Pull agent short names from the patch content for the PR title."""
    seen: list[str] = []
    combined = "\n".join(b["content"] for b in pending)
    for pat, short in _AGENT_NAME_PATTERNS:
        if pat.search(combined) and short not in seen:
            seen.append(short)
    return seen[:4]  # title cap


def _pr_body(pending: list[dict]) -> str:
    """Render a Markdown summary of every patch + the deploy reminder."""
    today = date.today().isoformat()
    lines = [
        "## Auto-promoted prompt patches",
        "",
        f"Generated by the weekly `prompt_patch_promoter` cron on {today}.",
        "Source: `/system/prompt_patches.md` in the health memory store.",
        f"Patch count: **{len(pending)}**",
        "",
        "### Deploy notes",
        "",
        "- This PR is opened as a **draft**. Review the diff carefully.",
        "- Merging this PR triggers the existing `deploy-prompts.yml` "
        "workflow, which requires the `prompt-author-verified` label.",
        "- Re-running the promoter cron will NOT re-open the same patches; "
        "their fingerprints are recorded in "
        "`/system/prompt_patches_applied.md`.",
        "- If you close this PR without merging, the patches stay in the "
        "applied ledger. Clear them manually from "
        "`prompt_patches_applied.md` to allow a retry.",
        "",
        "### Patches included",
        "",
    ]
    for i, b in enumerate(pending, start=1):
        lines.append(f"#### Patch {i}")
        lines.append("")
        lines.append(b["content"])
        lines.append("")
        lines.append(f"_Fingerprint: `{b['fingerprint'][:16]}...`_")
        lines.append("")
    return "\n".join(lines)


def _gh_pr_create(branch: str, title: str, body: str) -> str:
    """Run ``gh pr create --draft`` and return the URL on stdout."""
    proc = _run_subprocess(
        [
            "gh",
            "pr",
            "create",
            "--draft",
            "--title",
            title,
            "--body",
            body,
            "--head",
            branch,
            "--base",
            "main",
        ],
        cwd=str(REPO_ROOT),
        check=False,
    )
    if proc.returncode != 0:
        raise _PromoterError(
            f"``gh pr create`` failed (stderr: {proc.stderr.strip()[:300]}). "
            f"Applied ledger NOT updated — next run will retry."
        )
    url = proc.stdout.strip().splitlines()[-1].strip()
    if not url.startswith("http"):
        raise _PromoterError(
            f"``gh pr create`` returned unexpected stdout: {proc.stdout[:200]}"
        )
    return url


# ---------------------------------------------------------------------------
# subprocess helpers (own wrappers so tests can monkeypatch them)
# ---------------------------------------------------------------------------


def _run_git(args: list[str]) -> subprocess.CompletedProcess:
    """Run a git command rooted at REPO_ROOT. Raises on non-zero exit."""
    return _run_subprocess(["git"] + args, cwd=str(REPO_ROOT), check=True)


def _run_subprocess(
    cmd: list[str], cwd: str, check: bool
) -> subprocess.CompletedProcess:
    """Wrapper over subprocess.run that's easy to monkeypatch in tests."""
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=check)


# ---------------------------------------------------------------------------
# Admin DM
# ---------------------------------------------------------------------------


def _admin_dm(message: str) -> None:
    """DM admins via slack_bot.send_notification(admin_only=True). Best-effort."""
    try:
        from slack_bot import send_notification

        send_notification("watch", message, admin_only=True)
    except Exception:
        log.exception("Failed to admin-DM prompt-patch promoter status")
