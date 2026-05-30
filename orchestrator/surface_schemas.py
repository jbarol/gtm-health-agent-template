"""Pydantic schemas for the per-portco persistent state surface (Plan #33).

The surface is the system-of-record view per portco. Slack posts are deltas
against this state. `SurfaceState` is the JSONB blob persisted in the
`surface_state` Postgres table (`state_json` column) and rendered to Slack
Canvas markdown by `surface_renderer.py` (F4).

Pure schemas: no I/O, no DB, no helpers. The compute layer (F5) builds
`SurfaceState` from the memory store + response_telemetry; the renderer
(F4) converts it to Canvas markdown; the pusher (F6) syncs to Slack.

Reuses `Confidence`, `Priority`, `Urgency` from `response_schemas.py` so the
surface and the per-post schemas speak the same vocabulary. Defines a local
`Status` enum since the surface tracks investigation/decision lifecycle
states that don't exist in the per-post schemas.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from response_schemas import Confidence, Priority, Urgency

# All schemas reject extra fields. Same posture as response_schemas — stale
# payload shapes surface as ValidationError, not silent drift.
_STRICT = ConfigDict(extra="forbid")

# Lifecycle state for findings and metrics. "monitor" covers passively-tracked
# items that aren't active investigations; "blocked" surfaces dependencies
# that need partner input before the agent can progress.
Status = Literal["open", "investigating", "blocked", "resolved", "monitor"]


class KeyMetricRow(BaseModel):
    model_config = _STRICT

    name: str
    value: str
    delta_vs_prior: str
    status: Status
    as_of: str  # ISO date, e.g. "2026-05-11"


class FindingRow(BaseModel):
    model_config = _STRICT

    title: str
    priority: Priority
    urgency: Urgency
    status: Status
    first_seen: str  # ISO date
    decision_required: bool
    decision_options: list[str] = Field(default_factory=list)
    evidence: str = ""
    confidence: Confidence


class DecisionRow(BaseModel):
    model_config = _STRICT

    title: str
    decided_at: str  # ISO date
    decision: str
    portco_response: str = ""


class TrajectoryBlock(BaseModel):
    model_config = _STRICT

    improving: list[str] = Field(default_factory=list)
    worsening: list[str] = Field(default_factory=list)
    new_this_week: list[str] = Field(default_factory=list)


class OpenQuestionRow(BaseModel):
    model_config = _STRICT

    question: str
    asked_at: str  # ISO date
    context: str = ""


class CostBlock(BaseModel):
    """Trailing operating-cost summary per portco, sourced from Plan #35.

    Populated by `surface_compute.read_cost_block`, rendered between
    "Recent Decisions" and "Trajectory" by `surface_renderer`. None on
    the parent `SurfaceState` when the cost ledger is unavailable
    (DATABASE_URL unset or query failure) — the renderer then skips the
    section entirely rather than emitting a placeholder.

    `trend_pct` is the 7-day window vs the prior 7-day baseline (so
    "the last week vs the week before"), expressed as percentage points
    and capped at +/-999 to keep the rendered string compact.
    """

    model_config = _STRICT

    trailing_7d_usd: float = 0.0
    trailing_30d_usd: float = 0.0
    trend_pct: float = 0.0  # 7d vs prior-7d %, capped at +/-999
    top_task: str = ""  # e.g. "ad_hoc_investigation: $X"
    cache_hit_pct: float = 0.0
    updated_at: str = ""  # ISO timestamp


class SurfaceState(BaseModel):
    model_config = _STRICT

    portco: str
    generated_at: str  # ISO datetime, e.g. "2026-05-11T14:23:00-07:00"
    key_metrics: list[KeyMetricRow] = Field(default_factory=list)
    open_findings: list[FindingRow] = Field(default_factory=list)
    recent_decisions: list[DecisionRow] = Field(default_factory=list)
    trajectory: TrajectoryBlock = Field(default_factory=TrajectoryBlock)
    open_questions: list[OpenQuestionRow] = Field(default_factory=list)
    cost_block: Optional[CostBlock] = None
