"""Tests for the Canvas markdown renderer (Plan #33 F5).

Golden-file test: load the F2 JSON fixture at
`docs/surface/example-surface.json`, hydrate a `SurfaceState`, and
compare `render(state)` byte-for-byte to `docs/surface/example.md`.

Empty-section tests cover the MVP path (F4 ships with only key
metrics) and the all-empty path (renderer must not crash and must not
emit any "## ..." headers).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from surface_renderer import (
    _render_cost_block,
    _render_decision_inline,
    _render_priority_badge,
    _render_trajectory,
    render,
)
from surface_schemas import (
    CostBlock,
    FindingRow,
    KeyMetricRow,
    SurfaceState,
    TrajectoryBlock,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
GOLDEN_MD = REPO_ROOT / "docs" / "surface" / "example.md"
GOLDEN_JSON = REPO_ROOT / "docs" / "surface" / "example-surface.json"


def _fully_populated_state() -> SurfaceState:
    """Build a SurfaceState from the F2 JSON fixture."""
    return SurfaceState.model_validate_json(GOLDEN_JSON.read_text())


def test_render_matches_golden_file():
    state = _fully_populated_state()
    expected = GOLDEN_MD.read_text()
    actual = render(state)
    if actual != expected:
        import difflib

        diff = "\n".join(
            difflib.unified_diff(
                expected.splitlines(),
                actual.splitlines(),
                fromfile="example.md (expected)",
                tofile="render() output",
                lineterm="",
            )
        )
        pytest.fail(f"Rendered markdown drifted from golden file:\n{diff}")


def test_render_empty_state_just_heading():
    state = SurfaceState(
        portco="Acme",
        generated_at="2026-05-11T09:00:00-07:00",
    )
    out = render(state)
    assert out.startswith("# Acme — GTM Health\n")
    assert "_Last updated 2026-05-11 09:00 PT_" in out
    for header in (
        "## Key Metrics",
        "## Open Findings",
        "## Recent Decisions",
        "## Trajectory",
        "## Open Questions",
    ):
        assert header not in out, f"{header} should be skipped on empty state"


def test_render_trajectory_empty_block_returns_empty_string():
    assert _render_trajectory(TrajectoryBlock()) == ""


def test_render_trajectory_populated_block_emits_three_lines():
    block = TrajectoryBlock(
        improving=["MQL to SQL conversion (+2pp over 4 weeks)"],
        worsening=[
            "Win rate (partner channel)",
            "GRR (regional concentration in West)",
        ],
        new_this_week=[
            "Q1 vertical mix shift detected",
            "Stage-3 cycle elongation",
        ],
    )
    out = _render_trajectory(block)
    # Header + three populated lines, each terminated with "\n".
    lines = out.splitlines()
    assert lines[0] == "## Trajectory"
    assert lines[1] == "**Improving:** MQL to SQL conversion (+2pp over 4 weeks)."
    assert (
        lines[2]
        == "**Worsening:** Win rate (partner channel), GRR (regional concentration in West)."
    )
    assert (
        lines[3]
        == "**New this week:** Q1 vertical mix shift detected; Stage-3 cycle elongation."
    )
    assert out.endswith("\n")


def test_render_trajectory_partial_block_only_emits_populated_lines():
    block = TrajectoryBlock(improving=["MQL to SQL"])
    out = _render_trajectory(block)
    assert "## Trajectory" in out
    assert "**Improving:** MQL to SQL." in out
    assert "**Worsening:**" not in out
    assert "**New this week:**" not in out


def test_render_priority_badge_known_priorities():
    assert _render_priority_badge("P0") == ":rotating_light: P0"
    assert _render_priority_badge("P1") == ":rotating_light: P1"
    assert _render_priority_badge("P2") == ":eyes: P2"
    assert _render_priority_badge("P3") == ":information_source: P3"


def test_render_priority_badge_unknown_priority_falls_back():
    # Forward-compatibility: unrecognized priority should not raise.
    assert _render_priority_badge("P9") == ":information_source: P9"


def _finding(**overrides) -> FindingRow:
    base = dict(
        title="Win rate fell 8pp in partner channel",
        priority="P1",
        urgency="this_week",
        status="open",
        first_seen="2026-05-08",
        decision_required=False,
        decision_options=[],
        evidence="",
        confidence="HIGH",
    )
    base.update(overrides)
    return FindingRow(**base)


def test_render_decision_inline_no_decision_required_returns_empty():
    finding = _finding(decision_required=False)
    assert _render_decision_inline(finding) == ""


def test_render_decision_inline_with_options_renders_separator():
    finding = _finding(
        decision_required=True,
        decision_options=["Coach AE-East", "Pause partner channel"],
    )
    assert (
        _render_decision_inline(finding)
        == "_Decision needed: Coach AE-East / Pause partner channel_"
    )


def test_render_cost_block_none_returns_empty_string():
    """When cost_block is None the helper short-circuits to empty."""
    assert _render_cost_block(None) == ""


def test_render_cost_block_populated_emits_three_lines():
    block = CostBlock(
        trailing_7d_usd=42.5,
        trailing_30d_usd=180.0,
        trend_pct=25.0,
        top_task="ad_hoc_investigation: $30.00",
        cache_hit_pct=80.0,
        updated_at="2026-05-11T14:00:00",
    )
    out = _render_cost_block(block)
    lines = out.splitlines()
    assert lines[0] == "## Operating Cost"
    assert lines[1] == "7-day spend: $42.50 (+25.0% vs prior 30-day baseline)"
    assert lines[2] == "Top task: ad_hoc_investigation: $30.00"
    assert lines[3] == "Cache hit rate: 80.0%"
    assert out.endswith("\n")


def test_render_cost_block_negative_trend_renders_minus_sign():
    block = CostBlock(
        trailing_7d_usd=10.0,
        trailing_30d_usd=80.0,
        trend_pct=-12.5,
        top_task="cron: $5.00",
        cache_hit_pct=65.0,
        updated_at="2026-05-11T14:00:00",
    )
    out = _render_cost_block(block)
    assert "(-12.5% vs prior 30-day baseline)" in out


def test_render_cost_block_no_top_task_omits_line():
    block = CostBlock(
        trailing_7d_usd=10.0,
        trailing_30d_usd=80.0,
        trend_pct=-12.5,
        top_task="",
        cache_hit_pct=65.0,
        updated_at="2026-05-11T14:00:00",
    )
    out = _render_cost_block(block)
    assert "Top task:" not in out
    # Cache line still emitted.
    assert "Cache hit rate: 65.0%" in out


def test_render_includes_operating_cost_section_when_present():
    """Full SurfaceState with a cost_block emits the Operating Cost block."""
    state = SurfaceState(
        portco="Acme",
        generated_at="2026-05-11T09:00:00-07:00",
        cost_block=CostBlock(
            trailing_7d_usd=42.5,
            trailing_30d_usd=180.0,
            trend_pct=25.0,
            top_task="ad_hoc_investigation: $30.00",
            cache_hit_pct=80.0,
            updated_at="2026-05-11T14:00:00",
        ),
    )
    out = render(state)
    assert "## Operating Cost" in out
    assert "7-day spend: $42.50" in out
    assert "Top task: ad_hoc_investigation: $30.00" in out
    assert "Cache hit rate: 80.0%" in out


def test_render_without_cost_block_omits_operating_cost_section():
    """SurfaceState with cost_block=None must not emit the section."""
    state = SurfaceState(
        portco="Acme",
        generated_at="2026-05-11T09:00:00-07:00",
        cost_block=None,
    )
    out = render(state)
    assert "## Operating Cost" not in out


def test_render_cost_block_positioned_between_decisions_and_trajectory():
    """Operating Cost section sits after Recent Decisions, before Trajectory."""
    from surface_schemas import DecisionRow

    state = SurfaceState(
        portco="Acme",
        generated_at="2026-05-11T09:00:00-07:00",
        recent_decisions=[
            DecisionRow(
                title="A decision",
                decided_at="2026-05-10",
                decision="acted",
            )
        ],
        trajectory=TrajectoryBlock(improving=["X"]),
        cost_block=CostBlock(
            trailing_7d_usd=5.0,
            trailing_30d_usd=20.0,
            trend_pct=0.0,
            top_task="cron: $5.00",
            cache_hit_pct=50.0,
            updated_at="2026-05-11T14:00:00",
        ),
    )
    out = render(state)
    decisions_idx = out.index("## Recent Decisions")
    cost_idx = out.index("## Operating Cost")
    trajectory_idx = out.index("## Trajectory")
    assert decisions_idx < cost_idx < trajectory_idx


def test_render_only_key_metrics_skips_other_sections():
    state = SurfaceState(
        portco="Acme",
        generated_at="2026-05-11T09:00:00-07:00",
        key_metrics=[
            KeyMetricRow(
                name="Win rate",
                value="20%",
                delta_vs_prior="-5pp",
                status="investigating",
                as_of="2026-05-11",
            )
        ],
        trajectory=TrajectoryBlock(),
    )
    out = render(state)
    assert "## Key Metrics" in out
    assert "Win rate" in out
    for header in (
        "## Open Findings",
        "## Recent Decisions",
        "## Trajectory",
        "## Open Questions",
    ):
        assert header not in out, f"{header} should be skipped"
