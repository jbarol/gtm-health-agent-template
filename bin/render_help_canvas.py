#!/usr/bin/env python3
"""Render and push the "How to use the GTM Health Agent" canvas to Slack.

Single source of truth for the help canvas. Pulls the live agent roster
from ``agents/active_versions.json``, the slash-command inventory from
``orchestrator/slack_bot.py``, the quick-answer trigger patterns from
``orchestrator/session_runner.py``, and the feedback prefixes from the
same module, then renders a markdown document and upserts it to the
Slack channel canvas via ``canvases.edit`` / ``conversations.canvases.create``.

Runs from:
  * ``.github/workflows/update-help-canvas.yml`` — auto-fires on any change
    to the agent / slash-command source files.
  * ``python bin/render_help_canvas.py`` — local / manual invocation.
  * ``python bin/render_help_canvas.py --dry-run`` — print the rendered
    markdown to stdout without touching Slack.

Required env (live mode):
  ``SLACK_BOT_TOKEN``           — bot token with ``canvases:write``,
                                  ``conversations.canvases:write``, and
                                  ``channels:manage`` scopes (already
                                  granted by PR #59).
  ``SLACK_CHANNEL_ID``          — channel to host the canvas. Single-portco
                                  today (Acme); per-portco loop is
                                  Phase 2.

Optional env:
  ``HELP_CANVAS_ID``            — when set, ``canvases.edit`` reuses this
                                  canvas. When unset, the first run
                                  creates a fresh canvas and prints the
                                  ID so it can be added to env / Railway
                                  secrets for the next run.

Exit codes:
  0 — success (canvas updated, no-op when content unchanged, or dry-run)
  1 — any failure path (network, schema, missing env)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger("render_help_canvas")

REPO_ROOT = Path(__file__).resolve().parent.parent
ACTIVE_VERSIONS = REPO_ROOT / "agents" / "active_versions.json"
SLACK_BOT_PY = REPO_ROOT / "orchestrator" / "slack_bot.py"
SESSION_RUNNER_PY = REPO_ROOT / "orchestrator" / "session_runner.py"

CANVAS_TITLE = "GTM Health Agent — How to Use"

# Static descriptions of each agent. Pulled from CLAUDE.md so the source
# of truth stays in one place — the GitHub Action invalidates this canvas
# whenever CLAUDE.md / setup_agents.py / update_prompts.py changes, so a
# refresh here is the contract.
AGENT_DESCRIPTIONS: dict[str, str] = {
    "prompt_engineer": (
        "Preprocesses your question — injects portco data rules, corrects "
        "field names, drafts a plan + expected output. Runs before every "
        "Coordinator session and renders the rich ack you see in-thread."
    ),
    "quick_answer": (
        "Single-fact lookups that skip the full investigation pipeline. "
        '"How many opps closed last week?" / "What is FATI at Acme?". '
        "Has Salesforce + Kapa (Confluence/Jira) access."
    ),
    "coordinator": (
        "Orchestrates sub-agents, runs validation pipeline (stat + "
        "adversarial review), calls the Writing Agent for prose. Never "
        "queries Salesforce directly — delegates to specialists."
    ),
    "dream": (
        "Nightly hypothesis generation. Writes investigation plans the "
        "Coordinator runs at 05:00 PT to surface tomorrow's risks before "
        "anyone asks."
    ),
    "pipeline_monitor": (
        "Lead-flow specialist — materializes Salesforce reads to Parquet "
        "via dump_sf_query, reports findings with confidence tags."
    ),
    "sales_monitor": (
        "Opportunity / stage-flow specialist — same data pattern as the "
        "Pipeline Monitor, focused on the sales cycle."
    ),
    "postsales_monitor": (
        "Retention specialist. Has Kapa access to cross-reference customer "
        "issues against engineering / Jira context — investigate churn "
        "shifts with product-side awareness."
    ),
    "statistician": (
        "PhD-level quantitative validation: CIs, p-values, regression, "
        "survival analysis. Every numeric finding gets stat-checked before "
        "it reaches Slack."
    ),
    "adversarial_reviewer": (
        "Five-check challenge process on every finding. Looks for "
        "cherry-picked aggregations, missing baselines, confounders. "
        "Output blocks publish if it rejects."
    ),
    "cross_domain_synthesizer": (
        "Connects signals across pipeline / sales / post-sales into named "
        "patterns. Pulls Kapa for product-side correlations."
    ),
    "chart_designer": (
        "Renders charts via QuickChart when the response payload calls for "
        "visualization."
    ),
    "writing_agent": (
        "Prose composer (Haiku 4.5). The Coordinator delegates to it via the "
        "multiagent runtime with a structured payload + response_shape hint; "
        "this agent returns the finished Slack-ready prose grounded in "
        "Strunk's *Elements of Style*."
    ),
}


# ─── Source-of-truth extractors ──────────────────────────────────────────


def load_active_versions() -> dict[str, int]:
    """Return the agent → version mapping from active_versions.json."""
    if not ACTIVE_VERSIONS.exists():
        return {}
    data = json.loads(ACTIVE_VERSIONS.read_text())
    if isinstance(data, dict) and "agents" in data:
        return data["agents"]
    if isinstance(data, dict):
        return {k: v for k, v in data.items() if isinstance(v, int)}
    return {}


def extract_slash_commands() -> list[tuple[str, str]]:
    """Parse slash-command registrations + their first-line docstring from
    slack_bot.py. Returns ``[(name, short_desc), ...]``.

    Pattern: ``app.command("/foo")(handler)`` followed by the handler's
    docstring earlier in the file. Falls back to a static dict for
    handlers without docstrings.
    """
    if not SLACK_BOT_PY.exists():
        return []
    src = SLACK_BOT_PY.read_text()
    cmds = re.findall(r'app\.command\("(/[a-z-]+)"\)\(([a-z_]+)\)', src)
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    static_docs = {
        "/bot-pin": "Pin an agent to a specific version — `/bot-pin <agent> <version>`. Admin only.",
        "/bot-flag": "Toggle runtime flags — `/bot-flag SMOKE_PROBE_LEVEL off`. Admin only.",
        "/bot-stop": "Kill a runaway session — `/bot-stop [thread_ts]`. Admin only.",
    }
    for cmd, handler in cmds:
        if cmd in seen:
            continue
        seen.add(cmd)
        # Try to find the handler's docstring.
        m = re.search(
            rf'def {re.escape(handler)}\b[^:]*:\s*"""([^"\n]+)',
            src,
        )
        desc = (m.group(1).strip() if m else "") or static_docs.get(cmd, "")
        out.append((cmd, desc or "(no description)"))
    return out


def extract_quick_answer_patterns() -> list[str]:
    """Pull QUICK_ANSWER_PATTERNS from session_runner.py."""
    if not SESSION_RUNNER_PY.exists():
        return []
    src = SESSION_RUNNER_PY.read_text()
    m = re.search(r"QUICK_ANSWER_PATTERNS\s*=\s*\[([^\]]+)\]", src, re.S)
    if not m:
        return []
    inner = m.group(1)
    return [s.strip().strip('"').strip("'") for s in inner.split(",") if s.strip()]


def extract_feedback_prefixes() -> list[str]:
    """Pull FEEDBACK_PREFIXES from slack_bot.py."""
    if not SLACK_BOT_PY.exists():
        return []
    src = SLACK_BOT_PY.read_text()
    m = re.search(r"FEEDBACK_PREFIXES\s*=\s*\[([^\]]+)\]", src, re.S)
    if not m:
        return []
    inner = m.group(1)
    return [
        s.strip().strip('"').strip("'").strip() for s in inner.split(",") if s.strip()
    ]


# ─── Renderer ────────────────────────────────────────────────────────────


def render_markdown() -> str:
    """Build the full markdown body of the help canvas."""
    versions = load_active_versions()
    commands = extract_slash_commands()
    quick_patterns = extract_quick_answer_patterns()
    feedback_prefixes = extract_feedback_prefixes()

    lines: list[str] = []
    lines.append(f"# {CANVAS_TITLE}")
    lines.append("")
    lines.append(
        "Autonomous GTM operations analyst for Acme. Ask questions in "
        "this channel, get statistically-validated, adversarially-reviewed "
        "answers about pipeline, sales process, and retention. This page "
        "auto-updates when the agent roster or commands change."
    )
    lines.append("")

    lines.append("## Quick start")
    lines.append("")
    lines.append("Just talk to the bot — no slash command required:")
    lines.append("")
    lines.append("- `@gtm-health how many SQLs did we generate last week?`")
    lines.append("- `@gtm-health what's the win rate for opps over $50K?`")
    lines.append("- `@gtm-health which accounts churned last month and why?`")
    lines.append("- `@gtm-health what is FATI at Acme?` (knowledge base)")
    lines.append("")
    lines.append(
        "Questions that match a single-fact pattern route to **Quick Answer** "
        "(< 30s, single fact). Anything else goes through the full pipeline: "
        "Coordinator → specialists → Statistician → Adversarial Reviewer → "
        "Writing Agent → Slack."
    )
    lines.append("")

    if quick_patterns:
        lines.append("### Single-fact triggers (routes to Quick Answer)")
        lines.append("")
        lines.append(
            "Questions starting with any of these patterns skip the full "
            "investigation pipeline:"
        )
        lines.append("")
        for p in quick_patterns:
            lines.append(f"- `{p}…`")
        lines.append("")

    lines.append("## Slash commands")
    lines.append("")
    if commands:
        for cmd, desc in sorted(commands):
            lines.append(f"- **`{cmd}`** — {desc}")
        lines.append("")
    else:
        lines.append("_(none registered)_")
        lines.append("")

    lines.append("## The agent roster")
    lines.append("")
    lines.append(
        "Twelve agents across four tiers. Live versions are pinned in "
        "`agents/active_versions.json`; a CI guardrail enforces parity."
    )
    lines.append("")
    tiers = [
        ("Tier 1 — entry", ["prompt_engineer", "quick_answer"]),
        ("Tier 2 — orchestration", ["coordinator", "dream"]),
        (
            "Tier 3 — data + reasoning",
            [
                "pipeline_monitor",
                "sales_monitor",
                "postsales_monitor",
                "statistician",
                "adversarial_reviewer",
                "cross_domain_synthesizer",
                "chart_designer",
            ],
        ),
        ("Tier 4 — output", ["writing_agent"]),
    ]
    for tier_name, agents in tiers:
        lines.append(f"### {tier_name}")
        lines.append("")
        for a in agents:
            v = versions.get(a)
            ver_suffix = f" _(v{v})_" if v is not None else ""
            desc = AGENT_DESCRIPTIONS.get(a, "_(no description)_")
            lines.append(f"- **{a}**{ver_suffix} — {desc}")
        lines.append("")

    lines.append("## Standing instructions (feedback loop)")
    lines.append("")
    lines.append(
        "Messages starting with any of these prefixes are saved to "
        "`/instructions.md` in the health memory store and applied to every "
        "future agent session — no code change required:"
    )
    lines.append("")
    for p in feedback_prefixes:
        lines.append(f"- `{p}…`")
    lines.append("")
    lines.append("Examples:")
    lines.append("- `remember to use ARR not MRR for Acme reporting`")
    lines.append("- `never include opps in Discovery stage in pipeline numbers`")
    lines.append("- `always group by Account.Industry when ranking accounts`")
    lines.append("")

    lines.append("## Response shapes")
    lines.append("")
    lines.append(
        "The Coordinator picks a response shape based on the question. Each "
        "shape has a different Slack render:"
    )
    lines.append("")
    lines.append("- **one_fact** — single number / short statement. From Quick Answer.")
    lines.append(
        "- **comparative** — A vs B with deltas. Side-by-side or table format."
    )
    lines.append(
        "- **why** — root-cause analysis. Structured: signal → mechanism → "
        "implication → recommendation."
    )
    lines.append("- **briefing** — executive summary of an account / segment / motion.")
    lines.append("- **table** — `.xlsx` attachment + Block Kit preview.")
    lines.append(
        "- **methodology** — how a metric is computed (no numbers, just definition)."
    )
    lines.append("- **data_pull** — full rows on demand. Streams to `.xlsx`.")
    lines.append("")

    lines.append("## Operational notes")
    lines.append("")
    lines.append(
        "- **No timeouts.** Sessions run 55+ min. Long investigations are "
        "expected — the bot will reply in the thread when done."
    )
    lines.append(
        "- **Thread persistence.** Follow-up messages in the same thread "
        "reuse the existing session — context survives container restarts."
    )
    lines.append(
        "- **Statistical validation.** Every numeric finding is checked by "
        "the Statistician + Adversarial Reviewer before posting. Findings "
        "without validation never reach the channel."
    )
    lines.append(
        "- **Cost transparency.** `/cost today` shows what you've spent. The "
        "Operating Cost block on the persistent surface canvas trends 7d / 30d."
    )
    lines.append("")
    lines.append("---")
    lines.append(
        "_Auto-generated by `bin/render_help_canvas.py` — "
        "updates on every merge to main that touches the agent roster, "
        "slash commands, or this template._"
    )
    return "\n".join(lines).rstrip() + "\n"


# ─── Slack canvas writer ─────────────────────────────────────────────────


def _document_content(markdown: str) -> dict[str, Any]:
    """Wrap markdown in the document_content envelope Slack expects."""
    return {"type": "markdown", "markdown": markdown}


def push_to_slack(markdown: str) -> str:
    """Create or edit the help canvas; return the canvas_id.

    Returns the canvas_id of the created/edited canvas. Raises on failure.
    """
    from slack_sdk import WebClient  # imported here so --dry-run doesn't need it

    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        raise RuntimeError("SLACK_BOT_TOKEN unset")
    channel_id = os.environ.get("SLACK_CHANNEL_ID")
    canvas_id = os.environ.get("HELP_CANVAS_ID", "").strip()
    client = WebClient(token=token)
    doc = _document_content(markdown)

    if canvas_id:
        resp = client.canvases_edit(
            canvas_id=canvas_id,
            changes=[{"operation": "replace", "document_content": doc}],
        )
        if not resp.get("ok"):
            raise RuntimeError(f"canvases.edit failed: {resp}")
        log.info("Edited canvas %s in channel %s", canvas_id, channel_id)
        return canvas_id

    if not channel_id:
        raise RuntimeError(
            "SLACK_CHANNEL_ID unset — required for first-time canvas creation"
        )
    resp = client.conversations_canvases_create(
        channel_id=channel_id,
        title=CANVAS_TITLE,
        document_content=doc,
    )
    if not resp.get("ok"):
        raise RuntimeError(f"conversations.canvases.create failed: {resp}")
    canvas_obj = resp.get("canvas") or {}
    new_id = (
        resp.get("canvas_id")
        or (canvas_obj.get("id") if isinstance(canvas_obj, dict) else "")
        or ""
    )
    if not new_id:
        raise RuntimeError(f"canvases.create returned no canvas_id — resp: {resp!r}")
    log.info(
        "Created canvas %s in channel %s — ADD `HELP_CANVAS_ID=%s` to env",
        new_id,
        channel_id,
        new_id,
    )
    print(f"HELP_CANVAS_ID={new_id}")
    return new_id


def _content_hash(markdown: str) -> str:
    return hashlib.sha256(markdown.encode()).hexdigest()[:12]


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    parser = argparse.ArgumentParser(
        prog="render_help_canvas.py",
        description=(
            "Render the 'How to use the GTM Health Agent' canvas and "
            "(by default) push it to Slack."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the rendered markdown to stdout; do not touch Slack.",
    )
    parser.add_argument(
        "--out",
        type=str,
        default=None,
        help="Optional file path — write the rendered markdown here too.",
    )
    args = parser.parse_args(argv)

    md = render_markdown()

    if args.out:
        Path(args.out).write_text(md)
        log.info("Wrote rendered markdown to %s", args.out)

    if args.dry_run:
        print(md)
        log.info(
            "Dry run — content hash=%s, length=%d chars",
            _content_hash(md),
            len(md),
        )
        return 0

    try:
        push_to_slack(md)
    except Exception:
        log.exception("Failed to push canvas")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
