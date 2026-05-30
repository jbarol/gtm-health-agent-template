"""Typed response schemas for the GTM Health Agent.

Every user-facing Slack post from the Coordinator or Quick Answer agent flows
through the `post_report` custom tool, whose payload must validate against
one of the schemas below. The renderer (response_renderer.py) converts a
validated schema instance to Slack mrkdwn, .docx, or .xlsx.

Schemas enforce STRUCTURE — extra=forbid, enum types, list-count caps,
field-shape validators. They do NOT enforce text length on user-facing
prose fields. Earlier versions used Field(max_length=N) as a "safety net";
in practice that fought LLM output variance and caused repeat production
[POST_REPORT_FAILED] incidents (2026-05-11). Text sizing lives in the
editor pass (orchestrator/editor.py) which runs before validation. List
counts, identifier lengths (portco), and platform-imposed limits
(TableBlock cell/row/header sizes) keep their caps.

Five response types map 1:1 to user-facing moments:

    quick_answer                 → Quick Answer agent, simple lookups
    ad_hoc_investigation_result  → Coordinator, Slack @bot questions
    anomaly_alert                → Coordinator, threshold breaches in cron
    nightly_digest               → Coordinator, 5am dream → investigation
    weekly_status                → Coordinator, Friday cross-portco trajectory

Adding a new type: append to RESPONSE_TYPES, add a class here, add a render
function in response_renderer.py, golden-file test it, update the post_report
tool definition in setup_agents.py.
"""

from typing import Annotated, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

# All schemas reject extra fields. Prevents stale-payload acceptance and gives
# self_heal a clean signal when prompts emit superseded shapes.
_STRICT = ConfigDict(extra="forbid")

# ---------------------------------------------------------------------------
# Shared types
# ---------------------------------------------------------------------------

Confidence = Literal["HIGH", "MEDIUM", "LOW", "DATA_GAP"]
Severity = Literal["critical", "watch", "info"]
Trend = Literal["up", "down", "flat", "unknown"]

# Decision-grade fields (Plan #29 / Task #15). Phase 1: ship as optional with
# sensible defaults — existing agent payloads keep validating. Phase 2 (v1.2)
# flips defaults off once adoption crosses 80%.
Priority = Literal["P0", "P1", "P2", "P3"]
Urgency = Literal["immediate", "this_week", "this_quarter", "monitor"]
Intervention = Literal[
    "data_fix", "process_change", "coaching", "strategic", "investigation"
]
ChangeStatus = Literal["new", "worsened", "improved", "unchanged", "resolved"]

# Per-item decision_options entry. No length cap — the editor pass handles
# sizing for prose quality (the cap-as-safety-net pattern fought LLM variance
# and caused repeat production failures, see incident 2026-05-11). Schema's
# job is structure (extra=forbid, enum types, list counts), not text policing.
_DecisionOption = Annotated[str, Field()]

# Slack Block Kit table block — see https://docs.slack.dev/reference/block-kit/blocks/table-block/
# Native support shipped 2025-08-14. The platform caps a single table at 100 rows
# × 20 columns and forbids more than one table per message; we cap tighter here
# (30 rows × 6 cols) so the inline message stays scannable on a phone. Above the
# 30-row threshold, the Coordinator is instructed to emit a 5-row preview table
# plus a streamed-xlsx attachment via the existing >500-row list-pull path.
#
# Per-cell cap: Slack's table block has no documented per-cell character limit —
# long cells wrap via `column_settings.is_wrapped=True` (set in the renderer).
# We cap at 500 chars as a structural safety net, not for ergonomics: the
# Coordinator prompt still aims for ≤200 chars/cell for mobile scannability,
# but product-definition prose and methodology notes routinely overrun 200.
# Cap-as-safety-net at the schema layer fought LLM variance and tripped a live
# session 2026-05-13 18:48 PT (string_too_long on natural ~210-char cells);
# 500 leaves room for that natural overrun without policing prose.
TableAlignment = Literal["left", "center", "right"]
_TableCell = Annotated[str, Field(max_length=500)]
_TableHeader = Annotated[str, Field(max_length=40)]


class TableBlock(BaseModel):
    model_config = _STRICT

    """One inline tabular breakdown. Rendered as a native Slack table block.

    Use a TableBlock when the answer is naturally rows of structured data
    (per-rep, per-account, per-stage) — exactly the shape that loses all
    structure when pipe-packed into a Finding.value string. Slack's native
    table block aligns columns, supports wrapping, and renders identically
    on mobile and desktop.

    Limits below are intentionally tighter than Slack's 100×20 platform cap
    so the table is scannable inline rather than scroll-of-doom. If the
    underlying data needs more rows, post a 5-row preview TableBlock AND a
    .xlsx via the streaming list-pull path (the >500-row rule in the prompt).
    """

    title: str = Field(max_length=120)
    headers: list[_TableHeader] = Field(min_length=1, max_length=6)
    rows: list[list[_TableCell]] = Field(default_factory=list, max_length=30)
    column_alignment: Optional[list[TableAlignment]] = None
    # footnote cap: 400 chars. Sample-size methodology lines like
    # "n=148 opps, Closed Won 2026-Q1, excludes the 12 reps with <5 opps"
    # routinely run 250-350 chars; the prior 200 cap tripped real outputs.
    footnote: Optional[str] = Field(default=None, max_length=400)

    @model_validator(mode="after")
    def _check_row_widths(self) -> "TableBlock":
        n = len(self.headers)
        for i, row in enumerate(self.rows):
            if len(row) != n:
                raise ValueError(
                    f"row {i} has {len(row)} cells but headers has {n}; "
                    f"every row must match the header count"
                )
        if self.column_alignment is not None and len(self.column_alignment) != n:
            raise ValueError(
                f"column_alignment has {len(self.column_alignment)} entries but "
                f"headers has {n}; provide one alignment per column or omit entirely"
            )
        return self


class KeyMetric(BaseModel):
    model_config = _STRICT

    """One headline number with context. Used in ad-hoc results and digests."""

    name: str
    current: str
    prior: Optional[str] = None
    benchmark: Optional[str] = None
    trend: Trend = "unknown"


class Finding(BaseModel):
    model_config = _STRICT

    """One validated finding. Used in ad-hoc results and nightly digests."""

    headline: str
    value: str
    confidence: Confidence
    severity: Severity
    evidence_query: Optional[str] = None
    reviewer_caveat: Optional[str] = None
    # Decision-grade fields (Plan #29). Phase 1: defaults applied when omitted.
    priority: Priority = "P2"
    urgency: Urgency = "monitor"
    decision_required: bool = False
    decision_options: list[_DecisionOption] = Field(default_factory=list, max_length=3)
    changed_since_last: Optional[ChangeStatus] = None
    recommended_intervention: Optional[Intervention] = None

    @model_validator(mode="after")
    def _check_decision_options(self) -> "Finding":
        if self.decision_required and not self.decision_options:
            raise ValueError("decision_options required when decision_required=True")
        return self


class PortcoLine(BaseModel):
    model_config = _STRICT

    """One portco's status in a weekly cross-portco summary."""

    portco: str = Field(max_length=80)  # identifier — kept capped
    headline: str
    severity: Severity
    # Decision-grade fields (Plan #29). PortcoLine carries priority/urgency but
    # not decision_required — weekly_status is informational, not decisional.
    priority: Priority = "P2"
    urgency: Urgency = "monitor"
    changed_since_last: Optional[ChangeStatus] = None
    recommended_intervention: Optional[Intervention] = None


# ---------------------------------------------------------------------------
# Response types
# ---------------------------------------------------------------------------


# List item types. Per-item length caps removed 2026-05-11 — see _DecisionOption
# comment. Editor pass handles sizing; schema only enforces structure.
# _PortcoName keeps a cap because it's an identifier (lookup key), not prose.
_OpenQuestion = Annotated[str, Field()]
_PortcoName = Annotated[str, Field(max_length=80)]


class QuickAnswerResponse(BaseModel):
    model_config = _STRICT

    """Single-fact lookup. Quick Answer agent only.

    Caps bumped 2026-05-11 after production failure:
    'Active sales reps (quota-carrying)' / value='28 total: 14 NB reps,
    14 Account Managers' (41 chars) hit the prior 40-char cap on value,
    and Salesforce field-list sources routinely exceed 80 chars.
    New caps accommodate compound facts (e.g. "N total: X type-A, Y
    type-B") and SOQL source descriptions with 3-4 fields.
    """

    response_type: Literal["quick_answer"] = "quick_answer"
    metric: str
    value: str
    as_of: str
    source: str


class AnomalyAlertResponse(BaseModel):
    model_config = _STRICT

    """Threshold breach during nightly or forecast cron. Coordinator emits."""

    response_type: Literal["anomaly_alert"] = "anomaly_alert"
    headline: str
    metric: str
    current_value: str
    prior_value: str
    benchmark: str
    severity: Literal["critical", "watch"]
    evidence_summary: str
    recommended_action: str
    # Decision-grade fields (Plan #29). Same shape as Finding so partners get
    # a consistent decision frame across anomalies and ad-hoc results.
    priority: Priority = "P2"
    urgency: Urgency = "monitor"
    decision_required: bool = False
    decision_options: list[_DecisionOption] = Field(default_factory=list, max_length=3)
    changed_since_last: Optional[ChangeStatus] = None
    recommended_intervention: Optional[Intervention] = None

    @model_validator(mode="after")
    def _check_decision_options(self) -> "AnomalyAlertResponse":
        if self.decision_required and not self.decision_options:
            raise ValueError("decision_options required when decision_required=True")
        return self


class AdHocInvestigationResponse(BaseModel):
    model_config = _STRICT

    """Slack @bot investigation result. Coordinator emits after validation."""

    response_type: Literal["ad_hoc_investigation_result"] = (
        "ad_hoc_investigation_result"
    )
    headline: str
    key_metrics: list[KeyMetric] = Field(default_factory=list, max_length=5)
    findings: list[Finding] = Field(default_factory=list, max_length=4)
    # `tables` is the structural answer to "by rep" / "per account" / "broken
    # out by X" questions. The Coordinator's VP-of-RevOps response_shape block
    # in setup_agents.py routes any such question to a TableBlock here. Slack
    # only allows one table block per message, but we accept up to 3 in the
    # schema so the renderer can pick the most informative one (or emit each
    # as a follow-up post in a later iteration). Default empty preserves
    # backward-compat with every existing payload.
    tables: list[TableBlock] = Field(default_factory=list, max_length=3)
    cross_domain_pattern: Optional[str] = None
    open_questions: list[_OpenQuestion] = Field(default_factory=list, max_length=3)
    methodology_note: Optional[str] = None
    # File paths to attach in-thread on the Slack post. Populated by the
    # Coordinator when a virtualized result (>50-row tool output) carries
    # data the user needs to receive — typically the ``.xlsx`` written by
    # ``result_virtualize.write_xlsx_streaming``. The renderer/dispatcher
    # uploads each path via ``files.upload_v2`` AFTER the main message
    # lands. No max_length on item strings (paths can be deep); list cap
    # of 5 prevents the agent from stapling every intermediate scratch file
    # onto one post.
    attachments: list[str] = Field(default_factory=list, max_length=5)


class NightlyDigestResponse(BaseModel):
    model_config = _STRICT

    """5am dream → investigation summary. Coordinator emits."""

    response_type: Literal["nightly_digest"] = "nightly_digest"
    headline: str
    portcos_with_action: list[_PortcoName] = Field(default_factory=list, max_length=5)
    changes_overnight: list[Finding] = Field(default_factory=list, max_length=5)
    link_to_full_report: Optional[str] = None


class WeeklyStatusResponse(BaseModel):
    model_config = _STRICT

    """Friday cross-portco trajectory readout. Coordinator emits."""

    response_type: Literal["weekly_status"] = "weekly_status"
    headline: str
    portco_lines: list[PortcoLine] = Field(default_factory=list, max_length=10)
    trajectory: str


# ---------------------------------------------------------------------------
# Registry — keep in sync with post_report tool's response_type enum in
# agents/setup_agents.py
# ---------------------------------------------------------------------------

RESPONSE_TYPES: dict[str, type[BaseModel]] = {
    "quick_answer": QuickAnswerResponse,
    "anomaly_alert": AnomalyAlertResponse,
    "ad_hoc_investigation_result": AdHocInvestigationResponse,
    "nightly_digest": NightlyDigestResponse,
    "weekly_status": WeeklyStatusResponse,
}


def parse_payload(response_type: str, payload: dict) -> BaseModel:
    """Validate a raw payload dict against the schema for response_type.

    Raises:
        KeyError: response_type is not registered.
        pydantic.ValidationError: payload does not match schema (shape, types,
            or length caps).
    """
    schema_cls = RESPONSE_TYPES[response_type]
    return schema_cls.model_validate(payload)
