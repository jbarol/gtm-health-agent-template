"""Render validated response schemas to Slack mrkdwn, .docx, or .xlsx.

Public API:
    render(response, verbosity, target) -> str | bytes

The renderer is the structural layer that makes every response of a given type
look identical. It's invoked by _dispatch_tool in session_runner.py when an
agent calls the post_report custom tool.

Three verbosity tiers (Plan #31 E1):
    terse    — headline + at most one supporting fact. No footer.
    normal   — current `summary` shape: headline, top finding/metric, expand
               footer. The default.
    verbose  — current `expanded` shape: every populated schema field, no
               footer.

Backward-compat aliases for the legacy two-tier model:
    summary  → normal
    expanded → verbose

Callers may pass `verbosity=` (preferred) or `mode=` (legacy keyword). Values
are normalized through `_normalize_verbosity`.

Three targets:
    slack — Slack mrkdwn (single-asterisk *bold*, no pipe tables). Default.
    docx  — python-docx bytes for file uploads.
    xlsx  — openpyxl bytes for data exports.

For v1, only the Slack target is implemented. docx/xlsx raise NotImplementedError;
add them when a use case requires file output (e.g., weekly_status to docx).
"""

from typing import Literal, Optional, Union

from response_schemas import (
    AdHocInvestigationResponse,
    AnomalyAlertResponse,
    Finding,
    KeyMetric,
    NightlyDigestResponse,
    PortcoLine,
    QuickAnswerResponse,
    TableBlock,
    WeeklyStatusResponse,
)

# Canonical 3-tier verbosity (Plan #31).
Verbosity = Literal["terse", "normal", "verbose"]

# Legacy 2-tier mode retained for backward compat with existing callers and
# tests. Renderer code paths only use canonical Verbosity internally — Mode
# is kept around so external type annotations elsewhere don't break.
Mode = Literal["summary", "expanded"]

Target = Literal["slack", "docx", "xlsx"]

ResponseSchema = Union[
    QuickAnswerResponse,
    AnomalyAlertResponse,
    AdHocInvestigationResponse,
    NightlyDigestResponse,
    WeeklyStatusResponse,
]

EXPAND_FOOTER = "_Reply `expand:` in this thread for the full analysis._"

SEVERITY_EMOJI = {
    "critical": ":rotating_light:",
    "watch": ":eyes:",
    "info": ":information_source:",
}

TREND_ARROW = {
    "up": "⬆",
    "down": "⬇",
    "flat": "→",
    "unknown": "",
}


# ---------------------------------------------------------------------------
# Verbosity normalization and section selection (Plan #31 E1)
# ---------------------------------------------------------------------------


# Mapping from legacy mode strings to canonical verbosity. Anything in this
# table is silently normalized; anything else raises ValueError so callers
# notice typos quickly.
_VERBOSITY_ALIASES: dict[str, Verbosity] = {
    "terse": "terse",
    "normal": "normal",
    "verbose": "verbose",
    "summary": "normal",
    "expanded": "verbose",
}


def _normalize_verbosity(verbosity: Optional[str]) -> Verbosity:
    """Normalize any accepted verbosity/mode string to the canonical 3-tier.

    Accepts:
        - canonical: "terse", "normal", "verbose"
        - legacy:    "summary" (→ "normal"), "expanded" (→ "verbose")
        - None       (→ "normal" default)

    Raises ValueError for anything else so typos at call sites fail loud.
    """
    if verbosity is None:
        return "normal"
    canonical = _VERBOSITY_ALIASES.get(verbosity)
    if canonical is None:
        raise ValueError(
            f"Unknown verbosity {verbosity!r}; expected one of "
            f"{sorted(_VERBOSITY_ALIASES.keys())}"
        )
    return canonical


# Sections each tier exposes. The section names are response-type-agnostic
# labels the per-type renderers consult to decide what to include. A response
# type that doesn't have a given section simply ignores its presence.
#
#   headline           — always included; the top-line *bold* title.
#   top_metric         — the single highest-priority KeyMetric (ad_hoc only).
#   top_finding        — the single most-severe finding (ad_hoc, nightly).
#   key_metrics        — full key_metrics list (ad_hoc).
#   findings           — full findings list (ad_hoc, nightly).
#   current_prior      — current/prior numbers (anomaly_alert).
#   evidence_summary   — evidence_summary text (anomaly_alert).
#   recommended_action — recommended_action line (anomaly_alert).
#   decision_block     — decision_required + options + intervention + changed.
#   cross_domain       — cross_domain_pattern block (ad_hoc).
#   open_questions     — open_questions list (ad_hoc).
#   methodology        — methodology_note italic (ad_hoc).
#   source             — source line (quick_answer).
#   portcos_with_action— portcos_with_action list (nightly).
#   link_to_report     — link_to_full_report (nightly).
#   portco_lines       — full portco_lines list (weekly).
#   portco_lines_flagged— only critical/watch portcos (weekly).
#   trajectory         — trajectory line (weekly).
#   expand_footer      — the "Reply `expand:`" hint at the bottom.
_TERSE_SECTIONS = [
    "headline",
    "top_metric",
    "top_finding",
    "trajectory",
]
# Normal mirrors the legacy `summary` mode exactly — backward-compat golden
# files in test_fixtures/ pin every line, so each entry below is load-bearing.
# Do not add fields to this list without also updating the goldens.
_NORMAL_SECTIONS = [
    "headline",
    "top_metric",
    "top_finding",
    "recommended_action",
    "portcos_with_action",
    "link_to_report",
    "portco_lines_flagged",
    "trajectory",
    "expand_footer",
]
_VERBOSE_SECTIONS = [
    "headline",
    "source",
    "current_prior",
    "evidence_summary",
    "recommended_action",
    "key_metrics",
    "findings",
    "decision_block",
    "cross_domain",
    "open_questions",
    "methodology",
    "portcos_with_action",
    "portco_lines",
    "trajectory",
    "link_to_report",
]


def _select_sections(verbosity: str) -> list[str]:
    """Return the list of section labels included for a given tier.

    This is the single source of truth for "what's in each tier." Per-type
    renderers consult the returned list (typically by `"x" in sections`) to
    decide which fields to surface. The labels are documented inline above.

    Accepts both canonical verbosity strings and legacy mode aliases.
    """
    canonical = _normalize_verbosity(verbosity)
    if canonical == "terse":
        return list(_TERSE_SECTIONS)
    if canonical == "verbose":
        return list(_VERBOSE_SECTIONS)
    return list(_NORMAL_SECTIONS)


def escape_slack(s: str) -> str:
    """Neutralize Slack mrkdwn injection in agent-controlled strings.

    Agent payloads can carry prompt-injected content from poisoned CRM data
    (e.g., a Lead's Description field containing `<!channel> URGENT`). Length
    caps in response_schemas don't stop this — they cap size, not contents.
    Every agent-controlled string MUST flow through this before entering
    rendered Slack output.

    Defuses:
    - `<!channel>`, `<!here>`, `<!everyone>` broadcast pings
    - `<@U123>` user mentions and `<#C456>` channel links
    - `<https://evil>` link autolinks
    - Backticks (would break out of code spans)
    - Underscores/asterisks at field boundaries (would create stray formatting)
    """
    if not s:
        return ""
    # HTML-style escape neutralizes < > and angle-bracket Slack tokens.
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # Belt-and-suspenders: even without angle brackets, the keywords
    # `channel`/`here`/`everyone` after `!` can still ping if rendered raw.
    # Replace `!channel` → `·channel` (typographic bullet, non-functional).
    for token in ("!channel", "!here", "!everyone"):
        s = s.replace(token, token.replace("!", "·"))
    # Backticks would break out of inline code spans (e.g., in evidence_query).
    s = s.replace("`", "ʼ")
    return s


# Internal alias used inside this module.
_esc = escape_slack


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def render_payload(
    response: ResponseSchema,
    verbosity: Optional[str] = None,
    *,
    mode: Optional[str] = None,
) -> tuple[str, list[dict]]:
    """Render to Slack, returning both the mrkdwn text and any extra Block Kit blocks.

    The string portion is identical to what `render(response, target="slack")`
    returns — every existing caller and golden file continues to pass. The
    second tuple element is a list of Block Kit block dicts (currently only
    `table` blocks) that the dispatcher appends to the chat.postMessage
    `blocks=` array AFTER the mrkdwn section and BEFORE the expand footer.

    Returns ([], []) safe defaults — never raises on missing optional fields.
    """
    text = render(response, verbosity=verbosity, mode=mode, target="slack")
    extra_blocks: list[dict] = []
    # Only AdHocInvestigationResponse carries TableBlocks today. Other schemas
    # may add a tables field in the future; gate on attribute presence so
    # adding it elsewhere doesn't require touching this code.
    tables = getattr(response, "tables", None) or []
    if tables:
        # Slack platform constraint: one table block per chat.postMessage call.
        # If the agent emits more than one, render the first inline and surface
        # a footer note in text that the rest were dropped.
        first = tables[0]
        extra_blocks.append(_format_table_title_block(first))
        extra_blocks.append(_format_table_block(first))
        footnote_block = _format_table_footnote_block(first)
        if footnote_block is not None:
            extra_blocks.append(footnote_block)
        if len(tables) > 1:
            dropped = len(tables) - 1
            text = text + (
                f"\n_+{dropped} additional table"
                f"{'s' if dropped > 1 else ''} omitted "
                f"(Slack allows one inline table per message)._"
            )
    return text, extra_blocks


def render(
    response: ResponseSchema,
    verbosity: Optional[str] = None,
    target: Target = "slack",
    *,
    mode: Optional[str] = None,
) -> Union[str, bytes]:
    """Render a validated response schema to the requested target+verbosity.

    Args:
        response: validated Pydantic response schema.
        verbosity: one of "terse", "normal", "verbose". Legacy values
            "summary" (→ "normal") and "expanded" (→ "verbose") are accepted
            as backward-compat aliases. Defaults to "normal".
        target: output format. Only "slack" is implemented.
        mode: DEPRECATED. Legacy keyword for `verbosity`. If both are passed,
            `verbosity` wins. Kept so existing callers don't break while we
            migrate Slack bot / session_runner to the new naming (E2/E3).

    Raises:
        NotImplementedError: target is docx or xlsx (not yet implemented).
        ValueError: response.response_type is not registered in the dispatch
            table, or `verbosity`/`mode` is an unknown string.
    """
    if target != "slack":
        raise NotImplementedError(
            f"render target={target!r} not yet implemented; v1 only supports slack"
        )

    # Resolve verbosity from either kwarg. `verbosity` is the preferred name;
    # `mode` is the legacy keyword some callers (and existing tests) still use.
    chosen = verbosity if verbosity is not None else mode
    canonical = _normalize_verbosity(chosen)

    renderer = _SLACK_RENDERERS.get(response.response_type)
    if renderer is None:
        raise ValueError(f"No renderer for response_type={response.response_type!r}")
    return renderer(response, canonical)


# ---------------------------------------------------------------------------
# Slack renderers (one per response type)
# ---------------------------------------------------------------------------


def _render_quick_answer_slack(r: QuickAnswerResponse, verbosity: Verbosity) -> str:
    """QuickAnswer — already terse by design.

    terse / normal: metric + value + as_of.
    verbose:        adds the source line.
    """
    sections = _select_sections(verbosity)
    lines = [
        f"*{_esc(r.metric)}* — {_esc(r.value)}",
        f"_{_esc(r.as_of)}_",
    ]
    if "source" in sections:
        lines.append(f"Source: {_esc(r.source)}")
    return "\n".join(lines)


def _render_anomaly_alert_slack(r: AnomalyAlertResponse, verbosity: Verbosity) -> str:
    """AnomalyAlert — short by definition.

    terse:   headline + benchmark only.
    normal:  + recommended_action + expand footer (matches legacy summary).
    verbose: + current/prior + evidence_summary + decision_block.
    """
    sections = _select_sections(verbosity)
    emoji = SEVERITY_EMOJI.get(r.severity, "")
    headline_with_priority = f"*[{r.priority}]* {_esc(r.headline)}"
    lines = [
        f"{emoji} {headline_with_priority} (benchmark: {_esc(r.benchmark)})".strip(),
    ]
    if "current_prior" in sections:
        lines.append(f"Current: {_esc(r.current_value)} | Prior: {_esc(r.prior_value)}")
    if "evidence_summary" in sections:
        lines.append(f"Evidence: {_esc(r.evidence_summary)}")
    if "recommended_action" in sections:
        lines.append(f"*Recommended:* {_esc(r.recommended_action)}")
    if "decision_block" in sections:
        _append_decision_lines(
            lines,
            decision_required=r.decision_required,
            decision_options=r.decision_options,
            recommended_intervention=r.recommended_intervention,
            changed_since_last=r.changed_since_last,
        )
    if "expand_footer" in sections:
        lines.append("")
        lines.append(EXPAND_FOOTER)
    return "\n".join(lines)


def _render_ad_hoc_slack(r: AdHocInvestigationResponse, verbosity: Verbosity) -> str:
    """AdHocInvestigationResult — primary user-facing response.

    terse:   headline + top metric.
    normal:  headline + top metric + top finding + expand footer (current
             summary shape).
    verbose: headline + all metrics + all findings + cross-domain pattern +
             open questions + methodology (current expanded shape).
    """
    sections = _select_sections(verbosity)
    lines: list[str] = [f"*{_esc(r.headline)}*"]

    if verbosity == "terse":
        # 1-sentence answer + 1 supporting number max: prefer the top metric;
        # fall back to the top finding if no metrics exist.
        if "top_metric" in sections and r.key_metrics:
            lines.append(_format_metric_line(r.key_metrics[0]))
        elif "top_finding" in sections:
            top_finding = _select_top_finding(r.findings)
            if top_finding is not None:
                lines.append(_format_finding_line(top_finding, include_priority=True))
        return "\n".join(lines)

    if verbosity == "normal":
        # Show only the highest-priority KeyMetric and the most severe Finding.
        if "top_metric" in sections and r.key_metrics:
            lines.append(_format_metric_line(r.key_metrics[0]))
        if "top_finding" in sections:
            top_finding = _select_top_finding(r.findings)
            if top_finding is not None:
                # Prepend priority tag on the top finding headline.
                lines.append(_format_finding_line(top_finding, include_priority=True))
        if "expand_footer" in sections:
            lines.append("")
            lines.append(EXPAND_FOOTER)
        return "\n".join(lines)

    # Verbose mode
    if "key_metrics" in sections and r.key_metrics:
        lines.append("")
        lines.append("*Key metrics:*")
        for m in r.key_metrics:
            lines.append(_format_metric_line(m))

    if "findings" in sections and r.findings:
        lines.append("")
        lines.append("*Findings:*")
        for f in _sort_findings_by_severity(r.findings):
            lines.append(_format_finding_line(f, include_priority=True))
            if f.evidence_query:
                # evidence_query is wrapped in inline code; backticks inside it
                # are already neutralized by escape_slack.
                lines.append(f"  _query: `{_esc(_truncate(f.evidence_query, 280))}`_")
            if "decision_block" in sections:
                _append_decision_lines(
                    lines,
                    decision_required=f.decision_required,
                    decision_options=f.decision_options,
                    recommended_intervention=f.recommended_intervention,
                    changed_since_last=f.changed_since_last,
                )

    if "cross_domain" in sections and r.cross_domain_pattern:
        lines.append("")
        lines.append(f"*Cross-domain pattern:* {_esc(r.cross_domain_pattern)}")

    if "open_questions" in sections and r.open_questions:
        lines.append("")
        lines.append("*Open questions:*")
        for q in r.open_questions:
            lines.append(f"- {_esc(q)}")

    if "methodology" in sections and r.methodology_note:
        lines.append("")
        lines.append(f"_Methodology: {_esc(r.methodology_note)}_")

    return "\n".join(lines)


def _render_nightly_digest_slack(r: NightlyDigestResponse, verbosity: Verbosity) -> str:
    """NightlyDigest — overnight changes across portcos.

    terse:   headline + top change only.
    normal:  headline + portcos_with_action + top change + link + footer.
    verbose: headline + portcos_with_action + every change + link.
    """
    sections = _select_sections(verbosity)
    lines: list[str] = [f"*{_esc(r.headline)}*"]

    if verbosity == "terse":
        top = _select_top_finding(r.changes_overnight)
        if top is not None:
            lines.append(_format_finding_line(top, include_priority=True))
        return "\n".join(lines)

    if verbosity == "normal":
        if "portcos_with_action" in sections and r.portcos_with_action:
            lines.append(
                f"Action needed: {', '.join(_esc(p) for p in r.portcos_with_action)}"
            )
        # Show the single most severe overnight change
        top = _select_top_finding(r.changes_overnight)
        if top is not None:
            lines.append(_format_finding_line(top, include_priority=True))
        if "link_to_report" in sections and r.link_to_full_report:
            lines.append(f"_Full report: {_esc(r.link_to_full_report)}_")
        if "expand_footer" in sections:
            lines.append("")
            lines.append(EXPAND_FOOTER)
        return "\n".join(lines)

    # Verbose
    if "portcos_with_action" in sections and r.portcos_with_action:
        lines.append("")
        lines.append(
            f"*Action needed:* {', '.join(_esc(p) for p in r.portcos_with_action)}"
        )

    if "findings" in sections and r.changes_overnight:
        lines.append("")
        lines.append("*Overnight changes:*")
        for f in _sort_findings_by_severity(r.changes_overnight):
            lines.append(_format_finding_line(f, include_priority=True))
            if "decision_block" in sections:
                _append_decision_lines(
                    lines,
                    decision_required=f.decision_required,
                    decision_options=f.decision_options,
                    recommended_intervention=f.recommended_intervention,
                    changed_since_last=f.changed_since_last,
                )

    if "link_to_report" in sections and r.link_to_full_report:
        lines.append("")
        lines.append(f"_Full report: {_esc(r.link_to_full_report)}_")

    return "\n".join(lines)


def _render_weekly_status_slack(r: WeeklyStatusResponse, verbosity: Verbosity) -> str:
    """WeeklyStatus — Friday cross-portco trajectory.

    terse:   headline + trajectory.
    normal:  headline + flagged portcos + trajectory + footer.
    verbose: headline + every portco + trajectory.
    """
    sections = _select_sections(verbosity)
    lines: list[str] = [f"*{_esc(r.headline)}*"]

    if verbosity == "terse":
        if r.trajectory:
            lines.append(f"_{_esc(r.trajectory)}_")
        return "\n".join(lines)

    if verbosity == "normal":
        if "portco_lines_flagged" in sections:
            flagged = [p for p in r.portco_lines if p.severity in ("critical", "watch")]
            for p in flagged:
                lines.append(_format_portco_line(p, include_priority=True))
        if "trajectory" in sections and r.trajectory:
            lines.append("")
            lines.append(f"*Trajectory:* {_esc(r.trajectory)}")
        if "expand_footer" in sections:
            lines.append("")
            lines.append(EXPAND_FOOTER)
        return "\n".join(lines)

    # Verbose: show all portcos
    if "portco_lines" in sections and r.portco_lines:
        lines.append("")
        lines.append("*By portco:*")
        for p in r.portco_lines:
            lines.append(_format_portco_line(p, include_priority=True))

    if "trajectory" in sections and r.trajectory:
        lines.append("")
        lines.append(f"*Trajectory:* {_esc(r.trajectory)}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

_SLACK_RENDERERS = {
    "quick_answer": _render_quick_answer_slack,
    "anomaly_alert": _render_anomaly_alert_slack,
    "ad_hoc_investigation_result": _render_ad_hoc_slack,
    "nightly_digest": _render_nightly_digest_slack,
    "weekly_status": _render_weekly_status_slack,
}


# ---------------------------------------------------------------------------
# Shared formatters
# ---------------------------------------------------------------------------


def _format_metric_line(m: KeyMetric) -> str:
    """Inline metric: *Win rate* — 23.4% (prior: 27.6%, benchmark: 20-30%, ⬇)"""
    parts = []
    if m.prior:
        parts.append(f"prior: {_esc(m.prior)}")
    if m.benchmark:
        parts.append(f"benchmark: {_esc(m.benchmark)}")
    arrow = TREND_ARROW.get(m.trend, "")
    if arrow:
        parts.append(arrow)
    suffix = f" ({', '.join(parts)})" if parts else ""
    return f"*{_esc(m.name)}* — {_esc(m.current)}{suffix}"


def _format_finding_line(f: Finding, include_priority: bool = False) -> str:
    """One-line finding: :emoji: [P1] headline — value [HIGH] (caveat if any)"""
    emoji = SEVERITY_EMOJI.get(f.severity, "")
    priority_tag = f"*[{f.priority}]* " if include_priority else ""
    line = (
        f"{emoji} {priority_tag}*{_esc(f.headline)}* — "
        f"{_esc(f.value)} `[{f.confidence}]`"
    ).strip()
    if f.reviewer_caveat:
        line += f" _({_esc(f.reviewer_caveat)})_"
    return line


def _format_portco_line(p: PortcoLine, include_priority: bool = False) -> str:
    emoji = SEVERITY_EMOJI.get(p.severity, "")
    priority_tag = f"*[{p.priority}]* " if include_priority else ""
    line = f"{emoji} {priority_tag}*{_esc(p.portco)}*: {_esc(p.headline)}".strip()
    if p.recommended_intervention:
        line += f" _intervention: {p.recommended_intervention}_"
    if p.changed_since_last:
        line += f" _({p.changed_since_last})_"
    return line


def _append_decision_lines(
    lines: list,
    *,
    decision_required: bool,
    decision_options: list,
    recommended_intervention,
    changed_since_last,
) -> None:
    """Append decision-grade lines for verbose mode. No-op when nothing set."""
    if decision_required and decision_options:
        opts = " | ".join(_esc(o) for o in decision_options)
        lines.append(f"  *Decision:* {opts}")
    if recommended_intervention:
        lines.append(f"  *Intervention:* {recommended_intervention}")
    if changed_since_last:
        lines.append(f"  _({changed_since_last})_")


_SEVERITY_RANK = {"critical": 0, "watch": 1, "info": 2}


def _sort_findings_by_severity(findings: list[Finding]) -> list[Finding]:
    """Critical first, then watch, then info. Preserves input order within rank."""
    return sorted(findings, key=lambda f: _SEVERITY_RANK.get(f.severity, 99))


def _select_top_finding(findings: list[Finding]):
    """Return the highest-severity finding, or None if list is empty."""
    if not findings:
        return None
    return _sort_findings_by_severity(findings)[0]


def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


def _format_table_block(block: TableBlock) -> dict:
    """Render a TableBlock as a Slack Block Kit `table` block dict.

    Shape per https://docs.slack.dev/reference/block-kit/blocks/table-block/ —
    `rows` is a list of rows, each row is a list of cell objects. We emit
    every cell as `raw_text` (plain text). The header row is the first entry
    in `rows`; the platform doesn't yet expose an `is_header` flag, so the
    header row is differentiated only by ordering. column_settings carries
    alignment and wrapping. Title and footnote are NOT part of the table
    block per Slack's spec — they're emitted as Block Kit `header` and
    `context` blocks above and below the table, respectively.

    Returns a list of block dicts (header + table + optional context) for
    the caller to splice into the message's `blocks=` array. The caller
    extends extra_blocks with this list.
    """
    # Column settings: one entry per column. align defaults to "left"; wrap
    # on by default so long cell text doesn't get clipped on narrow screens.
    n_cols = len(block.headers)
    alignment = block.column_alignment or ["left"] * n_cols
    column_settings = [
        {"is_wrapped": True, "align": alignment[i]} for i in range(n_cols)
    ]

    # The header row is the first row of the table. Cells are raw_text; the
    # text is pre-escaped to neutralize any Slack mrkdwn injection from
    # agent-controlled strings.
    header_row = [{"type": "raw_text", "text": _esc(h)} for h in block.headers]
    data_rows = [
        [{"type": "raw_text", "text": _esc(c)} for c in row] for row in block.rows
    ]

    return {
        "type": "table",
        "rows": [header_row, *data_rows],
        "column_settings": column_settings,
    }


def _format_table_title_block(block: TableBlock) -> dict:
    """Bold title header rendered above the table block."""
    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"*{_esc(block.title)}*"},
    }


def _format_table_footnote_block(block: TableBlock) -> Optional[dict]:
    """Italic context note rendered under the table when footnote is set."""
    if not block.footnote:
        return None
    return {
        "type": "context",
        "elements": [
            {"type": "mrkdwn", "text": f"_{_esc(block.footnote)}_"},
        ],
    }
