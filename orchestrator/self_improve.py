"""Self-improvement module: crawls Managed Agents docs for changes, updates configs, DMs release notes."""

import hashlib
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from typing import Iterable, Optional

import anthropic
import cost_collector
import httpx
from _messages_usage import log_messages_usage
import batch_runner
from batch_runner import BATCH_CACHE_TTL
from compresr_client import compress_prompt
from config import ANTHROPIC_API_KEY, HEALTH_STORE_ID, SLACK_NOTIFY_USER_IDS
from github_issue_helper import create_gh_issue, list_open_issues_with_label
from slack_bot import send_dm

log = logging.getLogger(__name__)

_SELF_IMPROVE_MODEL = "claude-sonnet-4-6"

# Callback name registered with batch_runner. When a batch poll finds an
# ended self_improve batch, batch_runner._dispatch_results looks up this
# key in the callback registry the poller passes in. Matches the call_site
# used in submit_batch so the default ``callback_name`` resolves correctly
# in batch_runner.
BATCH_CALLBACK_NAME = "self_improve"

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

DOCS_BASE = "https://platform.claude.com/docs/en/managed-agents"
DOCS_PAGES = [
    "overview",
    "quickstart",
    "agent-setup",
    "sessions",
    "environments",
    "events-and-streaming",
    "tools",
    "files",
    "vaults",
    "memory",
    "define-outcomes",
    "multi-agent",
    "mcp-connector",
    "skills",
    "permission-policies",
    "observability",
    "github",
    "cloud-containers",
    "migration",
    "onboarding",
    # Candidate slugs for pending-access features. These currently 404; the
    # fetcher handles that gracefully. When they transition from 404 → 200,
    # TRIGGER_PAGES below routes a special notification.
    "structured-outputs",
    "response-format",
    "json-mode",
]

# F5 trigger registry (autoplan Failure Mode F5, implements Plan #34 monitoring).
#
# When a page in this map transitions from absent (404 / empty content) to
# populated (200 + content), self-improve fires a special trigger DM in addition
# to the regular doc-change summary. The trigger points the operator at the
# matching project plan so the migration window doesn't get missed by being
# folded into a generic "doc changed" notification.
#
# Add to this map when a new pending-access feature lands a plan in docs/plans/.
TRIGGER_PAGES = {
    "structured-outputs": {
        "plan_id": 34,
        "title": "Native structured outputs",
        "action": (
            "Anthropic has likely shipped native structured outputs in "
            "Managed Agents. Plan #34 "
            "(docs/plans/34-native-structured-outputs-migration.md) "
            "describes the migration path: keep response_schemas.py + "
            "renderer, swap the post_report tool definition for the new "
            "response_format. Start with Quick Answer, parallel-run, then "
            "Coordinator."
        ),
    },
    "response-format": {
        "plan_id": 34,
        "title": "Native structured outputs (response_format slug)",
        "action": ("Alternative slug for native structured outputs. See Plan #34."),
    },
    "json-mode": {
        "plan_id": 34,
        "title": "Native structured outputs (json-mode slug)",
        "action": ("Alternative slug for native structured outputs. See Plan #34."),
    },
}

# Plan #44 Task #5 / decision row #9 — ``STATE_DIR`` is the LOCAL orchestrator
# container disk path that holds the rolling Anthropic-docs hash snapshot
# (``doc_hashes.json``). This is the host disk used by self_improve to detect
# changes between nightly crawls. It is NOT the Anthropic session disk — no
# agent ever reads from here.
STATE_DIR = Path("/tmp/gtm-health-agent/self-improve")
STATE_FILE = STATE_DIR / "doc_hashes.json"

# Task #24: hot files where a doc-page change should auto-open a GitHub
# issue (so the operator can't silently miss a relevant Anthropic API
# tweak landing in a file the prompts depend on). The mapping is from
# doc-page slug → set of local files whose maintainer should be paged.
#
# Keep this set SMALL and EXPLICIT. The goal is not "page on every doc
# change" — that path drowns in noise. The goal is "page on doc changes
# that affect a load-bearing local touchpoint." Each entry below was
# picked because the local file makes a direct contract assumption
# against the matching Anthropic surface.
#
# Add to this set only when a new local file takes a hard dependency on
# a specific Anthropic doc page.
HOT_FILES = {
    "agents/setup_agents.py",
    "orchestrator/kapa_rest_tool.py",
    "orchestrator/session_runner.py",
    "orchestrator/db_adapter.py",
    "Dockerfile",
}

# Doc-page → local-file mapping used by ``create_doc_drift_issue``. The
# lookup is a coarse routing table — when a doc page in this dict changes
# AND the doc-update entry survives the cron, we open an issue against
# every listed hot file. Files outside HOT_FILES are filtered out at
# call time, so adding a slug here that points at a non-hot file is a
# no-op (the issue won't open). The mapping is duplicated rather than
# computed because the rationale for the link belongs in the diff
# review, not behind a layer of indirection.
#
# Codex review (PR #196): db_adapter.py owns the persisted Managed Agents
# doc snapshot table plus the cost/messages/session ledger surfaces, so
# ``observability`` and ``memory`` doc changes route to it as well as the
# session_runner. The ``observability`` slug used to be unmapped, which
# silently dropped routing for any change to that page.
HOT_FILE_BY_DOC_PAGE = {
    "sessions": {"orchestrator/session_runner.py", "orchestrator/db_adapter.py"},
    "events-and-streaming": {"orchestrator/session_runner.py"},
    "tools": {"orchestrator/session_runner.py", "agents/setup_agents.py"},
    "mcp-connector": {"orchestrator/kapa_rest_tool.py", "agents/setup_agents.py"},
    "vaults": {"orchestrator/kapa_rest_tool.py"},
    "multi-agent": {"agents/setup_agents.py"},
    "agent-setup": {"agents/setup_agents.py"},
    "permission-policies": {"agents/setup_agents.py"},
    "memory": {"agents/setup_agents.py", "orchestrator/db_adapter.py"},
    "files": {"orchestrator/session_runner.py"},
    "cloud-containers": {"Dockerfile"},
    "skills": {"agents/setup_agents.py"},
    "environments": {"agents/setup_agents.py"},
    "observability": {"orchestrator/db_adapter.py", "orchestrator/session_runner.py"},
}

# Task #24: 7-day TTL on /system/doc_updates entries. After this window the
# nightly ``prune_stale_doc_updates`` sweep drops the entry. The window is
# chosen so an operator who skips a weekend still sees the prior week's
# notes, but a stale "two months ago" entry doesn't accumulate.
DOC_UPDATE_TTL_DAYS = 7

# Task #24: label every auto-created doc-drift issue with this so the
# dedupe lookup can find prior open issues without scanning every issue
# in the repo.
_DOC_DRIFT_ISSUE_LABEL = "auto-doc-drift"


def _fetch_page(page: str) -> str:
    url = f"{DOCS_BASE}/{page}.md"
    try:
        resp = httpx.get(url, timeout=30, follow_redirects=True)
        if resp.status_code == 200:
            return resp.text
    except Exception as e:
        log.warning(f"Failed to fetch {url}: {e}")
    return ""


def _hash_content(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def _load_state() -> dict:
    """Load prior doc-page hashes for diffing.

    Reads from Postgres (``managed_agents_doc_snapshots``) when DATABASE_URL is
    set. Falls back to the on-disk JSON cache for dev environments without a
    database. The DB-backed path is the one that matters in production: Railway
    wipes ``/tmp`` on every deploy, and a wiped baseline made the previous
    implementation re-flag every tracked page as "new" on the first nightly
    run after a deploy (a v0-baseline bug — see Investigation in PR
    fix/self-improve-rename-and-diff).
    """
    try:
        import db_adapter

        if getattr(db_adapter, "DATABASE_URL", ""):
            hashes = db_adapter.load_managed_agents_doc_snapshots()
            last_run = None
            # Treat *any* persisted row as evidence of a prior run, even if its
            # content hash is the empty string (404 marker). That way the F5
            # ``newly_published`` branch fires when a 404 page later returns
            # 200, instead of being misclassified as a brand-new page.
            if hashes:
                last_run = "persisted"
            return {"hashes": dict(hashes), "last_run": last_run}
    except Exception:
        log.exception("DB-backed snapshot load failed; falling back to /tmp")

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"hashes": {}, "last_run": None}


def _save_state(state: dict):
    """Persist doc-page hashes for the next nightly diff.

    Writes to Postgres when available; otherwise falls back to the on-disk
    cache. We always also write the on-disk cache as a belt-and-suspenders
    backup so a transient DB outage doesn't blow away the baseline.
    """
    hashes = state.get("hashes") or {}

    try:
        import db_adapter

        if getattr(db_adapter, "DATABASE_URL", ""):
            db_adapter.save_managed_agents_doc_snapshots(hashes)
    except Exception:
        log.exception("DB-backed snapshot save failed; falling back to /tmp")

    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception:
        log.exception("Filesystem snapshot save failed (non-fatal)")


def check_for_updates():
    """Crawl managed agents docs, detect changes, analyze and notify.

    Three transition types are tracked:
      - new_pages: page absent from old_hashes entirely (truly new in this run).
      - changed_pages: page existed before with content; content hash differs.
      - newly_published: page was tracked with empty hash (was 404 last run)
        and now returns content. This is the F5 trigger surface — used by
        TRIGGER_PAGES to fire migration alerts when pending-access features
        ship docs.

    When ``BATCH_PROCESSING_ENABLED=true`` and the analysis enqueues
    successfully, the downstream side effects (memory write + DM) are
    deferred to ``_handle_batch_completion`` which fires when the batch
    poll loop finds the result. The state hashes are still saved before
    return so we don't re-scan the same docs on the next nightly run. If
    the batch never completes (rare; ~24h hard expiry), one nightly DM is
    lost — acceptable per Plan #36.
    """
    log.info("Self-improvement: checking for doc updates")

    # Task #24 — sweep stale /system/doc_updates entries before the crawl so
    # the daily run is the single nightly maintenance point for this corpus.
    # Best-effort: pruning failures must not block the doc crawl.
    try:
        prune_stale_doc_updates()
    except Exception:
        log.exception("self_improve: prune_stale_doc_updates failed (non-fatal)")

    state = _load_state()
    old_hashes = state.get("hashes", {})
    is_first_run = state.get("last_run") is None
    new_hashes = {}
    changed_pages = []
    new_pages = []
    newly_published = []

    for page in DOCS_PAGES:
        content = _fetch_page(page)
        old_h = old_hashes.get(page, "")
        if not content:
            new_hashes[page] = old_h
            continue

        h = _hash_content(content)
        new_hashes[page] = h

        if page not in old_hashes:
            new_pages.append(page)
            log.info(f"New doc page: {page}")
        elif old_h == "" and h:
            # Page was 404 last run; now published. The biggest signal.
            newly_published.append(page)
            log.info(f"Newly published doc page: {page}")
        elif old_h != h:
            changed_pages.append(page)
            log.info(f"Changed doc page: {page}")

    triggered = []
    if not is_first_run:
        for page in newly_published + changed_pages:
            if page in TRIGGER_PAGES:
                triggered.append((page, TRIGGER_PAGES[page]))
                log.info(f"F5 TRIGGER: {page} → plan #{TRIGGER_PAGES[page]['plan_id']}")

    if not changed_pages and not new_pages and not newly_published:
        log.info("No doc changes detected")
        state["hashes"] = new_hashes
        state["last_run"] = date.today().isoformat()
        _save_state(state)
        return

    # newly_published pages get analyzed alongside changed/new pages
    pages_to_analyze_changed = changed_pages + newly_published
    changes_summary = _analyze_changes(
        pages_to_analyze_changed,
        new_pages,
        triggered=triggered,
    )

    if changes_summary is None:
        # Batch enqueue succeeded — side effects (memory write + DM) deferred
        # to _handle_batch_completion. Still persist state so we don't
        # re-scan the same pages tomorrow if the batch hasn't completed by
        # then. Worst case: one nightly DM missed if the batch expires
        # without completing (24h hard cap). That's acceptable per Plan #36.
        log.info(
            f"Self-improvement queued for batch analysis: "
            f"{len(changed_pages)} changed, {len(new_pages)} new, "
            f"{len(newly_published)} newly published, "
            f"{len(triggered)} F5 triggers — DM will fire when batch completes"
        )
        state["hashes"] = new_hashes
        state["last_run"] = date.today().isoformat()
        _save_state(state)
        return

    _save_to_memory(changes_summary)

    # Task #24 — open a GitHub issue for any doc-page change that touches a
    # hot file. New pages and newly-published pages count too because the
    # 404 → 200 transition is the loudest signal we get. Best-effort; gh
    # CLI failures inside the helper are non-fatal.
    try:
        open_doc_drift_issues_for_pages(
            list(pages_to_analyze_changed) + list(new_pages),
            changes_summary,
        )
    except Exception:
        log.exception("self_improve: doc-drift issue creation failed (non-fatal)")

    _notify_user(
        changes_summary, pages_to_analyze_changed, new_pages, triggered=triggered
    )

    state["hashes"] = new_hashes
    state["last_run"] = date.today().isoformat()
    _save_state(state)

    log.info(
        f"Self-improvement complete: {len(changed_pages)} changed, {len(new_pages)} new"
    )


# System prompt as a module-level constant so the realtime path, the batch
# builder, and any future call sites share the exact same bytes — keeping
# the ephemeral cache prefix stable across tiers.
#
# Size note (PR fix/caching-and-compresr, 2026-05-11): this prompt is
# deliberately >1024 effective system tokens. Sonnet's prompt-caching
# minimum is 1024 tokens — any cached block below that silently no-ops at
# the API edge. Earlier versions of this constant were ~330 tokens, which
# made the ``cache_control: ephemeral 1h`` marker decorative. Do NOT trim
# this back below ~1100 tokens without verifying via
# ``client.messages.count_tokens`` that the cached block still clears the
# floor. The audit that found the bug lives at
# ``docs/proposals/cache-audit-2026-05-11.md``.
_SELF_IMPROVE_SYSTEM_PROMPT = (
    "You analyze Anthropic Managed Agents documentation changes and summarize "
    "their impact on a production deployment of the GTM Health Agent. The "
    "deliverable is a short DM that helps the operator decide whether to "
    "schedule migration work, adopt a new feature, or ignore the diff. Be "
    'concrete and operational — vague "this may be relevant" prose is '
    "actively worse than no message at all.\n\n"
    "Current GTM Health Agent architecture (the system you're advising about):\n"
    "- 11 agents in four tiers: Coordinator (Opus 4.8) orchestrates and posts "
    "  to Slack; Dream (Sonnet 4.6) nightly hypothesis generation; Quick Answer "
    "  (Sonnet 4.6) single-fact lookups; three Specialists (Pipeline Monitor, "
    "  Sales Process Monitor, Post-Sales Monitor on Sonnet 4.6) query "
    "  Salesforce via MCP; Statistician (Opus 4.8) runs CIs / regression / "
    "  survival; Chart Designer (Sonnet 4.6) renders QuickChart visualizations; "
    "  Adversarial Reviewer (Opus 4.8) runs a five-check challenge; "
    "  Cross-Domain Synthesizer (Opus 4.8) names cross-signal patterns; "
    "  Report Writer (Sonnet 4.6) assembles findings into Slack + .docx/.xlsx "
    "  reports; Prompt Engineer (Sonnet 4.6) preprocesses Slack questions.\n"
    "- Python orchestrator (one process, deployed on Railway via Docker) with "
    "  Slack Socket Mode, APScheduler cron, ThreadPoolExecutor investigation "
    "  worker, Postgres for cost ledger + thread-to-session persistence + "
    "  managed-agents-doc snapshot baseline.\n"
    "- Salesforce data via Anthropic MCP vault (Acme is the only active "
    "  portco today). Tools exposed by the vault: ``soqlQuery`` (free-form "
    "  SOQL) and ``describeSObject`` (schema introspection). No sf CLI "
    "  dependency — MCP-only.\n"
    "- Two memory stores attached to every session: methodology (read-only, "
    "  holds GTM audit methodology, benchmarks, SOQL patterns) and health "
    "  (read-write, per-portco metrics / open questions / findings / resolved "
    "  / schema cache, plus system-level learnings / session log / prompt "
    "  patches).\n"
    "- Custom tools registered with sessions: ``send_slack_notification`` (text "
    "  + severity), ``generate_chart`` (QuickChart wrapper), ``db_query`` "
    "  (read-only Postgres for snapshots), ``save_snapshot_batch`` (writes "
    "  daily rollups), ``post_report`` (structured-output variant that "
    '  signals "final answer delivered").\n'
    "- Sessions use streaming with the custom-tool lifecycle (``requires_action`` "
    "  pattern). MCP tools with ``evaluated_permission='ask'`` get "
    "  auto-approved via ``user.tool_confirmation``.\n"
    "- Two-ledger cost tracking: per-session local estimator writes "
    "  ``session_costs`` rows with full attribution; a 06:00 Pacific cron "
    "  pulls the Anthropic Admin Usage & Cost API into ``anthropic_daily_costs`` "
    "  for ground-truth reconciliation. Drift > 10% posts a Slack watch notice; "
    "  > 25% suggests a model-pricing-table refresh.\n"
    "- Compresr (YC W26) SDK wraps the user-message payload at two Messages-API "
    "  call sites (self_heal + self_improve). Espresso_v1 for general "
    "  compression, latte_v1 for query-aware. Per-site kill switch, "
    "  regression-guard auto-disable, 7-day cache TTL.\n"
    "- Nightly APScheduler pipeline (all Pacific): 00:00 self-improve doc "
    "  crawler, 00:00 batch flush (when batch enabled), 01:00 DB sync (SF "
    "  snapshot to Postgres), 03:00 forecast, 04:00 expire compresr cache, "
    "  05:00 dream → investigation, 06:00 Anthropic Admin API daily cost pull, "
    "  07:00 cost reconciliation, 08:00 daily cost digest DM.\n"
    "- Multi-agent orchestration IS enabled (beta ``managed-agents-2026-04-01``). "
    "  The Coordinator's ``multiagent.agents`` roster has 8 sub-agents in "
    "  prod. Each sub-agent owns its own ``tools``, ``mcp_servers``, and "
    "  ``system`` prompt per the docs.\n\n"
    "Managed Agents features by status (use this to decide what's relevant):\n"
    "- IN USE: sessions, environments, events/streaming, custom tools, MCP "
    "  connector (vaults), memory stores, files, permission policies, "
    "  multi-agent orchestration, prompt caching (system blocks with "
    "  ``cache_control: ephemeral`` at the 1h TTL — note that the cached "
    "  block must be >=1024 tokens for Sonnet; below that the marker silently "
    "  no-ops at the API edge).\n"
    "- WANT, NOT YET ACCESSED: outcomes / rubric-based grading (requested "
    "  2026-05-06; rubrics are reference-only until enabled), native "
    "  structured outputs (Plan #34 covers the migration — the "
    "  ``structured-outputs`` / ``response-format`` / ``json-mode`` doc slugs "
    "  are the F5 triggers).\n"
    "- ACTIVELY MONITORED: observability (token usage + session events + "
    "  cost attribution), skills, batch API (Plan #36, opt-in via "
    "  ``BATCH_PROCESSING_ENABLED``).\n\n"
    "When analyzing doc changes, focus on these four impact buckets and "
    "answer them in order:\n"
    "1. Breaking changes that affect our session lifecycle, tool handling, "
    "   or MCP connector behavior. If a breaking change is hidden in the "
    "   diff, this is the headline — operators must know before the next "
    "   nightly run.\n"
    "2. New features that could improve agent quality, reduce token spend, "
    "   or unlock a Plan-gated migration (e.g. native structured outputs "
    "   → Plan #34, rubric grading → outcomes work).\n"
    "3. Deprecations or migration requirements with a hard deadline.\n"
    "4. Performance improvements or new best practices we should fold in.\n\n"
    "Output contract — your response goes directly into the DM body, so:\n"
    "- Keep it under 800 chars in the body proper. The DM template adds "
    "  headers and the page list; you provide the analysis only.\n"
    "- Lead with the most operationally consequential finding.\n"
    "- Bullets over paragraphs. Slack mrkdwn renders ``-`` bullets as "
    "  ``•`` glyphs (the ``_md_to_slack`` post-processor handles the "
    "  rewrite — don't pre-rewrite them yourself).\n"
    "- Cite the specific page slug each finding came from in parentheses, "
    "  e.g. ``(sessions.md)``.\n"
    '- If first_run is signaled (every page is "new"), don\'t claim the '
    "  architecture is different from what we actually use — just surface "
    "  the features we should adopt and skip the rest.\n\n"
    "Good output vs bad output — examples:\n"
    "GOOD: ``Breaking: ``session.create`` now requires "
    "``environment_id`` even for stateless agents (sessions.md). Action: "
    "``orchestrator/session_runner.py`` already passes "
    "``ENVIRONMENT_ID`` on every create call — no migration needed. ``\n"
    "GOOD: ``New: outcomes/rubrics shipped — referenced 2026-05-06, now "
    "GA (define-outcomes.md). Action: open Plan #X to enable "
    "rubric-based grading; rubrics in docs/rubrics/*.md become "
    "load-bearing.``\n"
    "GOOD: ``The structured-outputs page transitioned 404 → 200 — Plan #34 "
    "migration window has opened. response_schemas.py + renderer keep, "
    "swap post_report tool definition for the new response_format.``\n"
    "BAD: ``The documentation contains information about sessions and "
    "tools.`` — generic, non-actionable, wastes the operator's time.\n"
    "BAD: ``You should consider whether multi-agent makes sense for your "
    "use case.`` — we already use multi-agent; the model doesn't know "
    "that without reading this prompt. Always check the architecture "
    "block above before making a recommendation.\n"
    "BAD: claiming a feature is missing when it's listed under IN USE "
    "above. The doc diff describes Anthropic-side; the architecture "
    "block describes us. Reconcile before suggesting changes.\n\n"
    "Anti-patterns you must avoid in your output:\n"
    "- Don't recommend disabling features we depend on (memory stores, "
    "  MCP connector, custom tools) without an explicit replacement path.\n"
    "- Don't recommend rewriting the orchestrator to use a different SDK "
    '  surface (e.g. "switch from Managed Agents back to raw Messages '
    "  API\") — that's a major migration and a doc-diff DM is the wrong "
    "  surface to propose it.\n"
    "- Don't recommend mocking the API or shimming around the change; "
    "  operators want the real migration plan.\n"
    "- Don't recommend a change that breaks prompt caching (e.g. inlining "
    "  dynamic content into the system block above the cache marker).\n"
    "- Don't fabricate version numbers, dates, or feature names that "
    "  aren't in the provided doc content. If the diff is ambiguous, say "
    "  so explicitly.\n"
    "- Don't recommend opening a plan without saying which plan number; "
    "  if you don't know the next number, write ``(next plan)``."
)


def _build_messages_request(
    combined: str, changed_pages: list, new_pages: list
) -> dict:
    """Build the kwargs dict for ``client.messages.create`` / batch params.

    Shared by the realtime path and the batch path so the prompt bytes are
    identical — required for cache hits and for the batch result to be a
    drop-in replacement for the realtime response.
    """
    return {
        "model": _SELF_IMPROVE_MODEL,
        "max_tokens": 4000,
        "system": [
            {
                "type": "text",
                "text": _SELF_IMPROVE_SYSTEM_PROMPT,
                # 1h TTL avoids cache expiry mid-batch. The nightly doc
                # crawler hits near-identical pages run-over-run, so the
                # extended TTL turns the second-and-later calls into cache
                # reads instead of fresh input. See BATCH_CACHE_TTL in
                # batch_runner.py for the full rationale (Task #54, Plan #36).
                "cache_control": {"type": "ephemeral", "ttl": BATCH_CACHE_TTL},
            }
        ],
        "messages": [
            {
                "role": "user",
                "content": (
                    f"The following Managed Agents documentation pages have changed or are new:\n"
                    f"Changed: {changed_pages}\n"
                    f"New: {new_pages}\n\n"
                    f"{'NOTE: If ALL pages are listed as new, this is a first-run baseline scan — the system has no prior hashes. Focus on features we should adopt, not on describing what the docs contain. Do NOT claim the architecture is different from what we already use.' if not changed_pages else ''}\n\n"
                    f"Content:\n{combined}\n\n"
                    f"Analyze these changes and produce a brief summary:\n"
                    f"1. What's new or changed (2-3 bullet points)\n"
                    f"2. Does this affect our GTM Health Agent setup? If so, what specific changes should we make?\n"
                    f"3. Any new features we should adopt?\n\n"
                    f"Be concise — this goes in a DM notification."
                ),
            }
        ],
    }


def _analyze_changes(
    changed_pages: list, new_pages: list, triggered: Optional[list] = None
) -> Optional[str]:
    """Use Claude to analyze what changed and what actions to take.

    Returns:
      * A ``str`` summary when the call ran realtime (default).
      * ``None`` when ``BATCH_PROCESSING_ENABLED=true`` AND the request was
        successfully enqueued. Caller (``check_for_updates``) treats ``None``
        as "deferred" and skips the side effects; ``_handle_batch_completion``
        runs ``_save_to_memory`` + ``_notify_user`` later.

    Batch enqueue failures (kill switch flipped, network error, empty
    response) fall through to the realtime path so a Batches API outage
    never blocks the nightly DM.
    """
    pages_content = []
    for page in changed_pages + new_pages:
        content = _fetch_page(page)
        if content:
            pages_content.append(f"## {page}.md\n{content[:5000]}")

    if not pages_content:
        return "Doc pages changed but content could not be fetched."

    combined = "\n\n---\n\n".join(pages_content)

    # Compresr integration (Plan #37 + audit fix 2026-05-11).
    #
    # espresso_v1 is the right model — general-purpose, no query needed —
    # because the analysis prompt asks for a holistic summary rather than a
    # specific question. compress_prompt() silently falls back to the
    # original text when:
    #   - COMPRESR_API_KEY is unset (dev environments),
    #   - the per-site flag is off,
    #   - the regression guard tripped the kill switch,
    #   - len(combined) < min_chars,
    #   - the SDK errors,
    # so this is always safe to call. Audit reference:
    # docs/proposals/compresr-audit-2026-05-11.md §1.
    #
    # 12000 chosen 2026-05-14: actual doc-payload sizes today are ~15K
    # chars after the Anthropic Managed Agents docs reshuffle. Original
    # 20000 (Plan #37) assumed a 100K payload. Re-tune if the doc set
    # grows materially again.
    combined = compress_prompt(
        combined,
        model="espresso_v1",
        call_site="self_improve",
        min_chars=12000,
    )
    request_params = _build_messages_request(combined, changed_pages, new_pages)

    # Batch path: enqueue and defer. The custom_id is just the run date —
    # only one self_improve request goes out per nightly run, so this is
    # plenty unique to disambiguate when polling.
    today = date.today().isoformat()
    custom_id = f"self_improve_{today}"
    batch_id = batch_runner.submit_batch(
        call_site="self_improve",
        model=_SELF_IMPROVE_MODEL,
        requests=[
            {
                "custom_id": custom_id,
                "params": request_params,
                # Round-trip the page lists + triggered registry to the
                # completion callback so it can re-build the DM body without
                # re-fetching state. ``triggered`` carries dicts (page,
                # trigger_dict) — flatten to a JSON-serializable list of
                # [page, dict] pairs so context_json round-trips cleanly.
                "context": {
                    "changed_pages": changed_pages,
                    "new_pages": new_pages,
                    "triggered": [[page, trig] for page, trig in (triggered or [])],
                    # Per-request call_site overrides the batch-level value
                    # at cost-logging time (see batch_runner._dispatch_results).
                    "call_site": "self_improve._analyze_changes",
                },
            }
        ],
        callback_name=BATCH_CALLBACK_NAME,
    )
    if batch_id:
        log.info(
            f"Self-improve: enqueued nightly analysis into batch {batch_id} "
            f"(deferred DM)"
        )
        return None

    # Realtime fallback path.
    response = client.messages.create(**request_params)

    # Log token usage + cost for this Messages API call. The log line is the
    # human-readable record; track_messages_call persists the row to the
    # ``messages_api_calls`` ledger (Plan #35, Task #39) for cost rollups +
    # reconciliation against the Anthropic Admin API ground truth. Both calls
    # swallow their own exceptions — cost tracking is observability, not
    # load-bearing.
    try:
        log_messages_usage("Self-improve", _SELF_IMPROVE_MODEL, response.usage)
    except Exception:
        log.exception("Failed to log self-improve usage (non-fatal)")
    try:
        cost_collector.track_messages_call(
            call_site="self_improve._analyze_changes",
            model=_SELF_IMPROVE_MODEL,
            usage=response.usage,
        )
    except Exception:
        log.exception("Failed to persist self-improve cost row (non-fatal)")

    analysis_text = next((b.text for b in response.content if b.type == "text"), "")

    # Record the downstream parse outcome for the compresr regression guard
    # (Plan #37 task #67). ``_analyze_changes`` doesn't ask for JSON, but we
    # apply the same gate: a non-empty, non-placeholder response means
    # compression didn't destroy downstream reasoning. Empty content
    # post-compression is the most likely regression failure mode (espresso_v1
    # deleting structural anchors), so we treat that as a parse failure for
    # guard purposes. Best-effort — never raises.
    try:
        from compresr_regression_guard import record_parse_outcome

        parsed_ok = bool(analysis_text and analysis_text.strip())
        record_parse_outcome("self_improve", parsed_ok=parsed_ok)
    except Exception:
        log.debug("self_improve: record_parse_outcome failed (non-fatal)")

    return analysis_text or "No analysis available"


def _handle_batch_completion(
    request_id: str, context: dict, result_text: str, result_usage: dict
):
    """Batch completion handler for self_improve requests.

    Signature matches ``batch_runner._dispatch_results``'s callback contract:
    ``(custom_id, context, text, usage) -> None``. Cost row was already
    written by ``batch_runner._log_batch_cost`` before this fires (succeeded
    results only). We re-run the deferred side effects from
    ``check_for_updates``: persist the analysis to the memory store and DM
    the configured operators.

    Errors are swallowed so a single bad result can't poison the poll loop.
    """
    try:
        if not result_text:
            log.info(
                f"Self-improve batch result for {request_id} was empty "
                f"(likely errored/expired); skipping DM"
            )
            return

        if not isinstance(context, dict):
            context = {}

        changed_pages = context.get("changed_pages") or []
        new_pages = context.get("new_pages") or []
        # context_json stores triggered as a list of [page, dict] pairs.
        triggered_raw = context.get("triggered") or []
        triggered = [tuple(pair) for pair in triggered_raw if len(pair) == 2]

        _save_to_memory(result_text)

        # Task #24 — same doc-drift auto-issue path as the realtime branch,
        # since the batch result is a drop-in replacement for the realtime
        # analysis. Best-effort.
        try:
            open_doc_drift_issues_for_pages(
                list(changed_pages) + list(new_pages),
                result_text,
            )
        except Exception:
            log.exception(
                "self_improve batch: doc-drift issue creation failed (non-fatal)"
            )

        _notify_user(result_text, changed_pages, new_pages, triggered=triggered)

        log.info(
            f"Self-improve batch completion: DM sent "
            f"({len(changed_pages)} changed, {len(new_pages)} new, "
            f"{len(triggered)} F5 triggers)"
        )
    except Exception:
        log.exception(
            f"Self-improve batch completion handler raised for request {request_id}"
        )


def _save_to_memory(summary: str):
    """Save the analysis to the memory store for agents to reference.

    Upsert semantics: ``RUN_NIGHTLY_NOW`` or a second nightly run on the
    same day will collide with the existing ``/system/doc_updates/<date>.md``
    entry and the API will return 409 ``memory_path_conflict_error``. The
    Anthropic API does not expose an upsert endpoint, so we catch the
    ``ConflictError``, read ``conflicting_memory_id`` from the response
    body, and call ``memories.update`` with the same content. This makes
    the function idempotent within a calendar day.

    Task #24 — frontmatter now carries an ``expires_at`` ISO timestamp
    7 days out so ``prune_stale_doc_updates`` (called from the same cron
    after the crawl) can sweep stale entries. Operators who want to keep
    an entry forever can edit the frontmatter in place — the prune sweep
    is read-then-write per file, so a manual edit survives.
    """
    today = date.today().isoformat()
    path = f"/system/doc_updates/{today}.md"
    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=DOC_UPDATE_TTL_DAYS)
    ).isoformat()
    content = (
        "---\n"
        f"expires_at: {expires_at}\n"
        "---\n"
        f"# Managed Agents Doc Updates — {today}\n\n"
        f"{summary}"
    )
    try:
        try:
            client.beta.memory_stores.memories.create(
                HEALTH_STORE_ID,
                path=path,
                content=content,
            )
        except anthropic.ConflictError as e:
            memory_id = _conflicting_memory_id(e)
            if not memory_id:
                # Re-raise so the outer handler logs the full trace. Without
                # an id to update against there is no recovery path.
                raise
            client.beta.memory_stores.memories.update(
                memory_id,
                memory_store_id=HEALTH_STORE_ID,
                content=content,
            )
            log.info(
                f"Self-improve: doc update for {today} already existed; "
                f"updated {memory_id} in place."
            )
    except Exception:
        log.exception("Failed to save doc update to memory store")


def _conflicting_memory_id(err: anthropic.ConflictError) -> Optional[str]:
    """Extract ``conflicting_memory_id`` from a 409 response body.

    The Anthropic SDK puts the parsed JSON body on ``err.body``; the field
    we need lives under ``body["error"]["conflicting_memory_id"]``. Some
    transport paths surface the body as a raw string, so be defensive."""
    body = getattr(err, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            mid = error.get("conflicting_memory_id")
            if isinstance(mid, str) and mid:
                return mid
    return None


def _notify_user(
    summary: str,
    changed_pages: list,
    new_pages: list,
    triggered: Optional[list] = None,
):
    """DM the user with release notes. `triggered` is a list of (page, trigger)
    tuples that, when non-empty, get a prominent header so plan-gated work
    (e.g. Plan #34 native structured outputs migration) isn't lost in the
    regular doc-change summary."""
    today = date.today().isoformat()
    triggered = triggered or []

    if triggered:
        # Trigger header leads. Operators routinely skim doc DMs; F5-level
        # signals deserve a top-line that survives a 5-second skim.
        header = (
            f":rotating_light: *TRIGGER — Plan migration window opened ({today})*\n\n"
        )
        for page, trig in triggered:
            header += (
                f"• *{trig['title']}* — page `{page}` is now published.\n"
                f"  Plan: #{trig['plan_id']} (`docs/plans/{trig['plan_id']:02d}-*.md`)\n"
                f"  Action: {trig['action']}\n\n"
            )
        header += "---\n\n"
        header += f":sparkles: *Managed Agents Docs — Daily Diff ({today})*\n\n"
    else:
        header = f":sparkles: *Managed Agents Docs — Daily Diff ({today})*\n\n"

    if changed_pages:
        header += f"*Changed pages:* {', '.join(changed_pages)}\n"
    if new_pages:
        header += f"*New pages:* {', '.join(new_pages)}\n"

    from slack_bot import _md_to_slack

    header += f"\n{_md_to_slack(summary)}"

    for user_id in SLACK_NOTIFY_USER_IDS:
        if user_id:
            try:
                send_dm(user_id, header)
                log.info(f"Sent Managed Agents docs-diff DM to {user_id}")
            except Exception:
                log.exception(f"Failed to DM {user_id}")


# ─────────────────────────────────────────────────────────────────────────────
# Task #24 — TTL sweep + hot-file auto-issue
# ─────────────────────────────────────────────────────────────────────────────

# Frontmatter regex: a doc-update file starts with a ``---``-delimited YAML
# block; we only need ``expires_at: <iso>`` out of it. Tolerant of leading
# whitespace and a missing trailing newline.
_EXPIRES_AT_RE = re.compile(
    r"^---\s*\n(?:[^\n]*\n)*?expires_at:\s*([^\n]+?)\s*\n(?:[^\n]*\n)*?---",
    re.MULTILINE,
)


def _parse_expires_at(content: str) -> Optional[datetime]:
    """Pull the ``expires_at`` ISO timestamp out of frontmatter. Returns
    None if the file has no frontmatter or no parseable timestamp — those
    files are treated as "indefinite, no sweep" by the prune sweep.

    Codex review (PR #196, P2 fix): operators who hand-edit the frontmatter
    may drop the timezone suffix and leave a naive timestamp like
    ``2026-05-21T12:00:00``. ``prune_stale_doc_updates`` compares against
    ``datetime.now(timezone.utc)`` — comparing a naive datetime to an aware
    one raises ``TypeError`` and aborts the sweep mid-loop. We normalize
    naive timestamps to UTC at parse time so the prune sweep is monotonic.
    """
    if not content:
        return None
    m = _EXPIRES_AT_RE.search(content)
    if not m:
        return None
    raw = m.group(1).strip()
    try:
        # ``datetime.fromisoformat`` handles offset-aware timestamps in 3.11+.
        # Strip a trailing ``Z`` (older formatting) so 3.10 also parses.
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        log.warning("self_improve: unparseable expires_at frontmatter: %r", raw)
        return None
    # Coerce naive timestamps to UTC so the comparison against
    # ``datetime.now(timezone.utc)`` in ``prune_stale_doc_updates`` doesn't
    # raise TypeError on hand-edited frontmatter.
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _list_doc_update_memories():
    """Yield ``(memory_id, path, content)`` triples for every
    ``/system/doc_updates/*.md`` entry in the health store.

    Wrapped in a generator so the caller can short-circuit on the first
    fetch failure without loading every memory eagerly. Each ``retrieve``
    is a separate API call — the list endpoint only returns metadata."""
    try:
        memories = client.beta.memory_stores.memories.list(
            HEALTH_STORE_ID,
            path_prefix="/system/doc_updates",
        )
    except Exception:
        log.exception("self_improve: list /system/doc_updates failed")
        return
    for m in getattr(memories, "data", []) or []:
        path = getattr(m, "path", "") or ""
        if not path.startswith("/system/doc_updates/"):
            continue
        try:
            current = client.beta.memory_stores.memories.retrieve(
                m.id,
                memory_store_id=HEALTH_STORE_ID,
            )
        except Exception:
            log.exception("self_improve: retrieve %s failed", path)
            continue
        yield m.id, path, getattr(current, "content", "") or ""


def prune_stale_doc_updates(now: Optional[datetime] = None) -> int:
    """Drop ``/system/doc_updates/*.md`` entries whose ``expires_at`` < now.

    Called from the same cron entry point as ``check_for_updates`` so the
    sweep runs once per nightly invocation. Files without parseable
    frontmatter are left alone — this is the migration path for entries
    written before the TTL frontmatter was introduced. Operators who want
    those gone can remove them manually.

    Returns the number of entries dropped (useful for tests and the cron
    log). Never raises; per-file failures log and continue.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    dropped = 0
    for memory_id, path, content in _list_doc_update_memories():
        expires_at = _parse_expires_at(content)
        if expires_at is None:
            log.debug(
                "self_improve.prune: %s has no expires_at; leaving in place", path
            )
            continue
        if expires_at >= now:
            continue
        try:
            client.beta.memory_stores.memories.delete(
                memory_id,
                memory_store_id=HEALTH_STORE_ID,
            )
            dropped += 1
            log.info(
                "self_improve.prune: dropped expired doc update %s (expires_at=%s)",
                path,
                expires_at.isoformat(),
            )
        except Exception:
            log.exception("self_improve.prune: failed to delete %s — skipping", path)
    if dropped:
        log.info("self_improve.prune: removed %d stale doc update entries", dropped)
    return dropped


def _hot_files_for(doc_page: str) -> Iterable[str]:
    """Intersect ``HOT_FILE_BY_DOC_PAGE[doc_page]`` with ``HOT_FILES``.

    The intersection is deliberate — adding a slug to ``HOT_FILE_BY_DOC_PAGE``
    that lists a non-hot file should be a no-op, not a stealth expansion of
    the page-on-this-file set. The single source of truth is ``HOT_FILES``;
    the by-page map only routes."""
    return sorted(HOT_FILE_BY_DOC_PAGE.get(doc_page, set()) & HOT_FILES)


def _build_drift_issue_title(doc_page: str, hot_file: str) -> str:
    """Build the GitHub issue title. Dedup matches on this exact title."""
    return f"[auto-doc-drift] {doc_page}.md → {hot_file}"


def _build_drift_issue_body(doc_url: str, summary: str, hot_file: str) -> str:
    """Build the issue body — operator-facing checklist."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    summary_block = (summary or "").strip() or "(no summary available)"
    return (
        "Auto-created by `self_improve` because the Managed Agents documentation "
        "changed in a way that affects a hot file in this repo.\n\n"
        "## Changed doc page\n\n"
        f"{doc_url}\n\n"
        "## Affected local file\n\n"
        f"`{hot_file}`\n\n"
        "## Summary of the change\n\n"
        f"{summary_block}\n\n"
        "## Operator checklist\n\n"
        f"- [ ] Read the doc page and the current `{hot_file}`\n"
        "- [ ] Decide whether the change requires a code edit, a prompt edit, "
        "or no action\n"
        "- [ ] If a code edit: open a follow-up PR and link it here\n"
        "- [ ] If no action needed: close this issue with a one-line note "
        "explaining why\n\n"
        f"_Created: {timestamp}_  \n"
        f"_Label: `{_DOC_DRIFT_ISSUE_LABEL}`_\n"
    )


def create_doc_drift_issue(doc_url: str, summary: str, hot_file: str) -> Optional[str]:
    """Open a GitHub issue for a doc-page change that touches ``hot_file``.

    Dedupe: callers may invoke this for the same ``(doc_page, hot_file)`` pair
    on every nightly run. We check ``gh issue list --label auto-doc-drift``
    for an existing open issue with the same title and skip the create if
    one already exists. The dedupe is best-effort — a transient gh CLI
    failure during the list call returns an empty list, which means a
    duplicate may slip through. The alternative (failing the cron) is worse.

    Returns the issue URL on a fresh create, ``None`` if the dedupe hit or
    the create failed. Logs every branch.
    """
    # Reconstruct the doc page slug from the doc_url tail so the title and
    # dedupe key stay consistent regardless of how the caller spelled the URL.
    doc_page = doc_url.rsplit("/", 1)[-1].removesuffix(".md")
    title = _build_drift_issue_title(doc_page, hot_file)

    existing_titles = list_open_issues_with_label(_DOC_DRIFT_ISSUE_LABEL)
    if title in existing_titles:
        log.info(
            "self_improve: doc-drift issue already open for %s — skipping create",
            title,
        )
        return None

    body = _build_drift_issue_body(doc_url, summary, hot_file)
    url = create_gh_issue(title, body, _DOC_DRIFT_ISSUE_LABEL)
    if url:
        log.info(
            "self_improve: opened doc-drift issue %s for %s",
            url,
            title,
        )
    else:
        log.warning(
            "self_improve: failed to open doc-drift issue for %s",
            title,
        )
    return url


def open_doc_drift_issues_for_pages(pages: Iterable[str], summary: str) -> list[str]:
    """Iterate hot-file pages from a doc-change set and open one issue per
    ``(page, hot_file)`` pair. Returns the list of URLs that landed.

    Called from ``check_for_updates`` after the analysis summary is built
    so the issue body can quote the analysis verbatim. Pages outside
    ``HOT_FILE_BY_DOC_PAGE`` are filtered out here so the caller doesn't
    have to know the routing table.
    """
    urls: list[str] = []
    for page in pages or []:
        for hot_file in _hot_files_for(page):
            doc_url = f"{DOCS_BASE}/{page}.md"
            url = create_doc_drift_issue(doc_url, summary, hot_file)
            if url:
                urls.append(url)
    return urls
