"""Tests for surface_schemas (Plan #33 F2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from surface_schemas import (
    CostBlock,
    DecisionRow,
    FindingRow,
    KeyMetricRow,
    OpenQuestionRow,
    SurfaceState,
    TrajectoryBlock,
)


_GOLDEN_PATH = (
    Path(__file__).parent.parent / "docs" / "surface" / "example-surface.json"
)


def _golden_payload() -> dict:
    with _GOLDEN_PATH.open() as fh:
        return json.load(fh)


def test_golden_fixture_parses_clean() -> None:
    """Golden file from docs/surface/example-surface.json validates without error."""
    payload = _golden_payload()
    state = SurfaceState.model_validate(payload)
    assert state.portco == "Acme"
    assert len(state.key_metrics) == 5
    assert len(state.open_findings) == 3
    assert len(state.recent_decisions) == 3
    assert state.trajectory.improving
    assert state.trajectory.worsening
    assert state.trajectory.new_this_week
    assert len(state.open_questions) == 3


def test_json_round_trip() -> None:
    """parse -> model_dump -> parse again yields a deep-equal model."""
    payload = _golden_payload()
    first = SurfaceState.model_validate(payload)
    dumped = first.model_dump()
    second = SurfaceState.model_validate(dumped)
    assert first == second
    # model_dump should be JSON-serializable too.
    re_dumped = json.loads(json.dumps(dumped))
    third = SurfaceState.model_validate(re_dumped)
    assert first == third


def test_empty_defaults() -> None:
    """SurfaceState with only required fields gets empty lists + default trajectory."""
    state = SurfaceState(
        portco="Acme",
        generated_at="2026-05-11T00:00:00-07:00",
    )
    assert state.key_metrics == []
    assert state.open_findings == []
    assert state.recent_decisions == []
    assert state.open_questions == []
    assert isinstance(state.trajectory, TrajectoryBlock)
    assert state.trajectory.improving == []
    assert state.trajectory.worsening == []
    assert state.trajectory.new_this_week == []


def test_finding_row_defaults() -> None:
    """FindingRow accepts empty decision_options + empty evidence by default."""
    row = FindingRow(
        title="Coverage low",
        priority="P2",
        urgency="this_quarter",
        status="open",
        first_seen="2026-05-01",
        decision_required=False,
        confidence="MEDIUM",
    )
    assert row.decision_options == []
    assert row.evidence == ""


def test_decision_row_defaults() -> None:
    """DecisionRow accepts empty portco_response by default."""
    row = DecisionRow(
        title="ARR off by 4%",
        decided_at="2026-05-07",
        decision="corrected",
    )
    assert row.portco_response == ""


def test_open_question_row_defaults() -> None:
    """OpenQuestionRow accepts empty context by default."""
    row = OpenQuestionRow(question="Why?", asked_at="2026-05-09")
    assert row.context == ""


def test_extra_fields_rejected() -> None:
    """extra='forbid' surfaces stale payload shapes as ValidationError."""
    with pytest.raises(ValidationError):
        SurfaceState.model_validate(
            {
                "portco": "Acme",
                "generated_at": "2026-05-11T00:00:00-07:00",
                "unexpected_field": "should fail",
            }
        )


def test_cost_block_defaults() -> None:
    """CostBlock fields all default to their zero-values."""
    block = CostBlock()
    assert block.trailing_7d_usd == 0.0
    assert block.trailing_30d_usd == 0.0
    assert block.trend_pct == 0.0
    assert block.top_task == ""
    assert block.cache_hit_pct == 0.0
    assert block.updated_at == ""


def test_surface_state_cost_block_optional_none_default() -> None:
    """SurfaceState.cost_block defaults to None — golden parses cleanly."""
    state = SurfaceState(
        portco="Acme",
        generated_at="2026-05-11T00:00:00-07:00",
    )
    assert state.cost_block is None


def test_surface_state_accepts_cost_block() -> None:
    """SurfaceState round-trips a populated cost_block."""
    payload = {
        "portco": "Acme",
        "generated_at": "2026-05-11T00:00:00-07:00",
        "cost_block": {
            "trailing_7d_usd": 12.5,
            "trailing_30d_usd": 50.0,
            "trend_pct": 8.5,
            "top_task": "ad_hoc: $7.00",
            "cache_hit_pct": 81.3,
            "updated_at": "2026-05-11T14:00:00",
        },
    }
    state = SurfaceState.model_validate(payload)
    assert state.cost_block is not None
    assert state.cost_block.trailing_7d_usd == 12.5
    assert state.cost_block.top_task == "ad_hoc: $7.00"


def test_key_metric_row_requires_status() -> None:
    """KeyMetricRow rejects missing status (no default)."""
    with pytest.raises(ValidationError):
        KeyMetricRow(  # type: ignore[call-arg]
            name="GRR",
            value="87%",
            delta_vs_prior="-2pp",
            as_of="2026-05-11",
        )
