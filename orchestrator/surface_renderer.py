"""Canvas markdown renderer for the persistent state surface (Plan #33 F5).

Pure f-string rendering (no Jinja or other templating). Given a
`SurfaceState` (the F2 schema), produce a Canvas-compatible markdown
document. The fully-populated rendering matches the golden fixture at
`docs/surface/example.md`, which in turn renders the F2 JSON fixture at
`docs/surface/example-surface.json`.

Sections are emitted in this fixed order, each skipped if empty:

    1. Heading + last-updated timestamp (always emitted)
    2. ## Key Metrics             (skipped if no rows)
    3. ## Open Findings (N)       (skipped if no rows)
    4. ## Recent Decisions (...)  (skipped if no rows)
    5. ## Operating Cost          (skipped when cost_block is None)
    6. ## Trajectory              (skipped if all three lists empty)
    7. ## Open Questions (N)      (skipped if no rows)

This module is import-light by design: only stdlib + the F2 schema
module. No I/O, no network, no Slack client — that lives in F6
(`surface_pusher.py`).

The renderer is timezone-aware about the "PT" suffix in the heading:
`SurfaceState.generated_at` is an ISO 8601 string that may or may not
carry an offset. We convert to Pacific by parsing and adjusting; if the
offset is absent we assume the timestamp is already in Pacific and emit
it as-is.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from surface_schemas import (
    CostBlock,
    DecisionRow,
    FindingRow,
    KeyMetricRow,
    OpenQuestionRow,
    SurfaceState,
    TrajectoryBlock,
)

# Priority → badge mapping for Open Findings headlines.
_PRIORITY_BADGE = {
    "P0": ":rotating_light:",
    "P1": ":rotating_light:",
    "P2": ":eyes:",
    "P3": ":information_source:",
}

# Decision action → (emoji prefix, verb phrase) for Recent Decisions bullets.
# "Acted on" takes a preposition because "act on a finding" is the natural
# phrasing; "Ignored"/"Corrected" take the finding as a direct object so no
# preposition is needed.
_ACTION_GLYPH = {
    "acted": ("✅", "Acted on"),
    "ignored": ("❌", "Ignored"),
    "corrected": ("⚠️", "Corrected"),
}

# Status → display string for the Key Metrics table and Open Findings tail.
_STATUS_LABEL = {
    "open": "Open",
    "investigating": "Investigating",
    "blocked": "Blocked",
    "resolved": "Resolved",
    "monitor": "Monitor",
}

# How many open questions to show inline before truncating with "- ...".
_OPEN_QUESTIONS_MAX_DISPLAY = 3

# Pacific Time offset for the "Last updated" stamp. The system runs in PT
# already (Railway timezone is set), so the simple-case path is to format
# the datetime verbatim. The branch below handles incoming timestamps that
# carry a UTC offset.
_PACIFIC_OFFSET = timedelta(hours=-7)  # PDT; renderer is daylight-agnostic
_PACIFIC_TZ = timezone(_PACIFIC_OFFSET)


def render(state: SurfaceState) -> str:
    """Convert a SurfaceState into Canvas markdown.

    Returns a single string ending in a trailing newline so the output
    can be byte-compared to a golden file without surprise whitespace
    diffs.
    """
    parts: list[str] = []
    parts.append(_render_heading(state))

    metrics_block = _render_key_metrics(state.key_metrics)
    if metrics_block:
        parts.append(metrics_block)

    findings_block = _render_open_findings(state.open_findings)
    if findings_block:
        parts.append(findings_block)

    decisions_block = _render_recent_decisions(state.recent_decisions)
    if decisions_block:
        parts.append(decisions_block)

    cost_section = _render_cost_block(state.cost_block)
    if cost_section:
        parts.append(cost_section)

    trajectory_block = _render_trajectory(state.trajectory)
    if trajectory_block:
        parts.append(trajectory_block)

    questions_block = _render_open_questions(state.open_questions)
    if questions_block:
        parts.append(questions_block)

    # Each block terminates with a single "\n". Join with another "\n" so
    # blocks are separated by a blank line. Do NOT append a trailing
    # newline — that would give us two at EOF.
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Section renderers
# ---------------------------------------------------------------------------


def _render_heading(state: SurfaceState) -> str:
    stamp = _format_pacific_stamp(state.generated_at)
    return f"# {state.portco} — GTM Health\n_Last updated {stamp} PT_\n"


def _format_pacific_stamp(generated_at: str) -> str:
    """Format an ISO 8601 string as 'YYYY-MM-DD HH:MM' in Pacific time.

    Accepts either an offset-aware string (e.g. "...-07:00") or an
    offset-naive string. Naive strings are assumed to already be in
    Pacific; aware strings are converted.
    """
    try:
        dt = datetime.fromisoformat(generated_at)
    except ValueError:
        # If we can't parse, fall back to the raw string truncated to
        # minute precision. Better to render something than crash.
        return generated_at[:16].replace("T", " ")
    if dt.tzinfo is not None:
        dt = dt.astimezone(_PACIFIC_TZ)
    return dt.strftime("%Y-%m-%d %H:%M")


def _render_key_metrics(rows: list[KeyMetricRow]) -> str:
    if not rows:
        return ""
    lines: list[str] = [
        "## Key Metrics",
        "| Metric | Value | Δ vs Prior | Status | As of |",
        "|---|---|---|---|---|",
    ]
    for row in rows:
        status = _STATUS_LABEL.get(row.status, row.status.title())
        lines.append(
            f"| {row.name} | {row.value} | {row.delta_vs_prior} | {status} | {row.as_of} |"
        )
    return "\n".join(lines) + "\n"


def _render_open_findings(rows: list[FindingRow]) -> str:
    if not rows:
        return ""
    lines: list[str] = [f"## Open Findings ({len(rows)})", ""]
    for row in rows:
        lines.append(f"### {_render_priority_badge(row.priority)} — {row.title}")
        if row.decision_required and row.decision_options:
            tail = _render_decision_inline(row)
        else:
            tail = f"Status: {_STATUS_LABEL.get(row.status, row.status.title())}"
        lines.append(f"First seen {row.first_seen} · {tail}")
        if row.evidence:
            lines.append(f"Evidence: {row.evidence} · Confidence: {row.confidence}")
        lines.append("")
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"


def _render_priority_badge(priority: str) -> str:
    """Return the badge prefix used in `### <badge> — <title>` lines.

    Pure-function helper extracted from `_render_open_findings` (F10). The
    return value pairs the priority's emoji with the priority label so
    callers can interpolate it directly. Unknown priorities fall through
    to the `:information_source:` glyph so renderer output never crashes
    on a forward-compatible Priority value.
    """
    emoji = _PRIORITY_BADGE.get(priority, ":information_source:")
    return f"{emoji} {priority}"


def _render_decision_inline(finding: FindingRow) -> str:
    """Render the inline "Decision needed" tail for a finding row.

    Pure-function helper extracted from `_render_open_findings` (F10).
    Returns the empty string when no decision is required so the caller
    can use the result as a section tail without branching for the empty
    case. When a decision is required but no options are provided, the
    helper still emits the `_Decision needed: _` shell — that signals an
    upstream data issue rather than swallowing it silently.
    """
    if not finding.decision_required:
        return ""
    decision_str = " / ".join(finding.decision_options)
    return f"_Decision needed: {decision_str}_"


def _render_recent_decisions(rows: list[DecisionRow]) -> str:
    if not rows:
        return ""
    lines: list[str] = ["## Recent Decisions (last 14 days)"]
    for row in rows:
        emoji, verb_phrase = _ACTION_GLYPH.get(row.decision, ("", row.decision.title()))
        if row.portco_response:
            tail = f" ({row.decided_at}) — {row.portco_response}"
        else:
            tail = f" ({row.decided_at})"
        lines.append(f'- {emoji} {verb_phrase} "{row.title}"{tail}')
    return "\n".join(lines) + "\n"


def _render_cost_block(block: CostBlock | None) -> str:
    """Render the Plan #35 operating-cost block.

    Three lines:
      * "7-day spend: $X (Y% vs prior 30-day baseline)"
      * "Top task: <name> ($X)"
      * "Cache hit rate: X.X%"

    Returns the empty string when `block` is None so the caller can
    omit the section without branching for the None case. Top-task and
    cache-hit lines are skipped individually when their underlying
    value is unavailable (empty string / zero), but the section header
    + 7-day line always emit when the block is present — that's the
    minimum useful signal.
    """
    if block is None:
        return ""
    lines: list[str] = ["## Operating Cost"]
    lines.append(
        f"7-day spend: ${block.trailing_7d_usd:,.2f} "
        f"({block.trend_pct:+.1f}% vs prior 30-day baseline)"
    )
    if block.top_task:
        lines.append(f"Top task: {block.top_task}")
    lines.append(f"Cache hit rate: {block.cache_hit_pct:.1f}%")
    return "\n".join(lines) + "\n"


def _render_trajectory(block: TrajectoryBlock) -> str:
    if not (block.improving or block.worsening or block.new_this_week):
        return ""
    lines: list[str] = ["## Trajectory"]
    if block.improving:
        lines.append(f"**Improving:** {', '.join(block.improving)}.")
    if block.worsening:
        lines.append(f"**Worsening:** {', '.join(block.worsening)}.")
    if block.new_this_week:
        lines.append(f"**New this week:** {'; '.join(block.new_this_week)}.")
    return "\n".join(lines) + "\n"


def _render_open_questions(rows: list[OpenQuestionRow]) -> str:
    if not rows:
        return ""
    lines: list[str] = [f"## Open Questions ({len(rows)})"]
    displayed = rows[:_OPEN_QUESTIONS_MAX_DISPLAY]
    for row in displayed:
        if row.context:
            lines.append(f"- {row.question} — {row.context}")
        else:
            lines.append(f"- {row.question}")
    if len(rows) > len(displayed):
        lines.append("- ...")
    return "\n".join(lines) + "\n"
