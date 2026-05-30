"""Fixture-driven tests for `surface_compute` (Plan #33 F4 + F7).

Each test writes a tmp memory-store file in the YAML-front-matter format
documented at `docs/surface/memory-store-format.md`, monkeypatches
`MEMORY_STORE_ROOT`, and asserts the corresponding reader returns the
expected rows. Missing-file + malformed-YAML cases are exercised so the
surface degrades to `[]` rather than raising.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from textwrap import dedent

import pytest

import surface_compute
from surface_compute import (
    _read_key_metrics,
    compute_surface,
    compute_trajectory,
    read_cost_block,
    read_open_questions,
    read_recent_decisions,
    read_unresolved_findings,
)
from surface_schemas import (
    CostBlock,
    DecisionRow,
    FindingRow,
    OpenQuestionRow,
    SurfaceState,
    TrajectoryBlock,
)


@pytest.fixture()
def memory_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the memory-store root to a tmp dir for the test session."""
    monkeypatch.setattr(surface_compute, "MEMORY_STORE_ROOT", tmp_path)
    return tmp_path


def _write(root: Path, portco: str, filename: str, body: str) -> None:
    portco_dir = root / portco
    portco_dir.mkdir(parents=True, exist_ok=True)
    (portco_dir / filename).write_text(dedent(body).lstrip("\n"))


def _today_iso(offset_days: int = 0) -> str:
    return (datetime.utcnow() + timedelta(days=offset_days)).date().isoformat()


# ---------------------------------------------------------------------------
# metrics.md
# ---------------------------------------------------------------------------


def test_compute_surface_parses_metrics(memory_root: Path) -> None:
    _write(
        memory_root,
        "test",
        "metrics.md",
        """
        # Metrics for test

        ---
        name: ARR
        value: $1.2M
        delta_vs_prior: +5%
        status: monitor
        as_of: 2026-05-01
        ---
        ARR within target band.

        ---
        name: Win rate
        value: 24%
        delta_vs_prior: -8pp
        status: investigating
        as_of: 2026-05-08
        ---
        Partner channel dragging.

        ---
        name: GRR
        value: 87%
        delta_vs_prior: flat
        status: monitor
        as_of: 2026-05-05
        ---
        Stable.
        """,
    )

    state = compute_surface("test")

    assert isinstance(state, SurfaceState)
    assert state.portco == "test"
    assert state.generated_at is not None
    assert len(state.key_metrics) == 3
    names = [m.name for m in state.key_metrics]
    assert names == ["ARR", "Win rate", "GRR"]
    assert state.key_metrics[0].value == "$1.2M"
    assert state.key_metrics[1].status == "investigating"


def test_compute_surface_missing_metrics_file_returns_empty(
    memory_root: Path,
) -> None:
    state = compute_surface("noportco")
    assert isinstance(state, SurfaceState)
    assert state.portco == "noportco"
    assert state.key_metrics == []
    assert state.open_findings == []
    assert state.recent_decisions == []
    assert state.open_questions == []
    assert isinstance(state.trajectory, TrajectoryBlock)


def test_compute_surface_loose_markdown_yields_empty(memory_root: Path) -> None:
    """Pre-Plan-#33 loose markdown bullets parse to []. Migration story."""
    _write(
        memory_root,
        "legacy",
        "metrics.md",
        """
        - ARR: $1.2M (+5%) [HIGH] @ 2026-05-01
        - Win rate: 24% (-8pp) [MEDIUM] @ 2026-05-08
        """,
    )
    state = compute_surface("legacy")
    assert state.key_metrics == []


def test_read_key_metrics_direct(memory_root: Path) -> None:
    _write(
        memory_root,
        "direct",
        "metrics.md",
        """
        ---
        name: Foo
        value: '42'
        delta_vs_prior: +1
        status: open
        as_of: 2026-05-01
        ---
        Body text.
        """,
    )
    rows = _read_key_metrics("direct")
    assert len(rows) == 1
    assert rows[0].name == "Foo"
    assert rows[0].value == "42"
    assert rows[0].status == "open"


def test_metrics_malformed_yaml_skipped(memory_root: Path) -> None:
    """A broken YAML block is skipped; good entries still return."""
    _write(
        memory_root,
        "mixed",
        "metrics.md",
        """
        ---
        name: Good
        value: '100'
        delta_vs_prior: +1
        status: monitor
        as_of: 2026-05-01
        ---
        ok

        ---
        name: Bad
          value: missing-quote: this is broken yaml :  : :
        delta_vs_prior: ???
        ---
        bad

        ---
        name: AlsoGood
        value: '200'
        delta_vs_prior: -1
        status: monitor
        as_of: 2026-05-02
        ---
        also ok
        """,
    )
    rows = _read_key_metrics("mixed")
    assert [r.name for r in rows] == ["Good", "AlsoGood"]


# ---------------------------------------------------------------------------
# findings.md
# ---------------------------------------------------------------------------


def test_read_unresolved_findings_filters_resolved(memory_root: Path) -> None:
    _write(
        memory_root,
        "fb",
        "findings.md",
        """
        ---
        priority: P1
        urgency: this_week
        status: open
        first_seen: 2026-05-08
        decision_required: true
        decision_options:
          - Coach AE-East
          - Pause partner channel
        evidence: Partner-sourced opps closed 24.1% vs 32.5% (n=47).
        confidence: HIGH
        ---
        Win rate fell 8pp in partner channel.

        Concentrated in AE-East cohort.

        ---
        priority: P2
        urgency: this_quarter
        status: investigating
        first_seen: 2026-05-05
        decision_required: false
        decision_options: []
        evidence: Stage-3 duration rose 18d -> 30d.
        confidence: MEDIUM
        ---
        Stage 3 cycle time +12 days.

        ---
        priority: P3
        urgency: monitor
        status: resolved
        first_seen: 2026-04-15
        decision_required: false
        decision_options: []
        evidence: Q1 pipeline gap; refilled in Q2.
        confidence: HIGH
        ---
        Q1 pipeline coverage shortfall (resolved).
        """,
    )

    rows = read_unresolved_findings("fb")
    assert len(rows) == 2
    titles = [r.title for r in rows]
    assert "Win rate fell 8pp in partner channel." in titles
    assert "Stage 3 cycle time +12 days." in titles
    # No resolved entry leaks through.
    assert all(r.status != "resolved" for r in rows)
    # decision_options is a real list, not a string.
    first = rows[0]
    assert isinstance(first, FindingRow)
    assert "Coach AE-East" in first.decision_options


def test_read_unresolved_findings_missing_file(memory_root: Path) -> None:
    assert read_unresolved_findings("ghost") == []


def test_read_unresolved_findings_skips_malformed(memory_root: Path) -> None:
    _write(
        memory_root,
        "fb2",
        "findings.md",
        """
        ---
        priority: P1
        urgency: this_week
        status: open
        first_seen: 2026-05-08
        decision_required: true
        confidence: HIGH
        ---
        Good finding.

        ---
        priority: NOT_A_VALID_PRIORITY
        urgency: this_week
        status: open
        first_seen: 2026-05-08
        decision_required: true
        confidence: HIGH
        ---
        Bad finding (invalid priority).
        """,
    )
    rows = read_unresolved_findings("fb2")
    assert len(rows) == 1
    assert rows[0].title == "Good finding."


# ---------------------------------------------------------------------------
# decisions.md
# ---------------------------------------------------------------------------


def test_read_recent_decisions_windows_to_days(memory_root: Path) -> None:
    today = _today_iso(0)
    eight_days_ago = _today_iso(-8)
    thirty_days_ago = _today_iso(-30)

    _write(
        memory_root,
        "dec",
        "decisions.md",
        f"""
        ---
        title: Recent decision
        decided_at: {today}
        decision: acted
        portco_response: Done.
        ---
        Recent body.

        ---
        title: Eight-days-ago decision
        decided_at: {eight_days_ago}
        decision: corrected
        ---
        Eight days ago.

        ---
        title: Thirty-days-ago decision
        decided_at: {thirty_days_ago}
        decision: ignored
        ---
        Old.
        """,
    )

    rows = read_recent_decisions("dec", days=14)
    titles = [r.title for r in rows]
    assert "Recent decision" in titles
    assert "Eight-days-ago decision" in titles
    assert "Thirty-days-ago decision" not in titles
    assert all(isinstance(r, DecisionRow) for r in rows)


def test_read_recent_decisions_missing_file(memory_root: Path) -> None:
    assert read_recent_decisions("ghost") == []


def test_read_recent_decisions_custom_window(memory_root: Path) -> None:
    today = _today_iso(0)
    forty_days_ago = _today_iso(-40)
    _write(
        memory_root,
        "win",
        "decisions.md",
        f"""
        ---
        title: New
        decided_at: {today}
        decision: acted
        ---
        x

        ---
        title: Older
        decided_at: {forty_days_ago}
        decision: ignored
        ---
        y
        """,
    )
    # 60-day window includes both.
    rows60 = read_recent_decisions("win", days=60)
    assert {r.title for r in rows60} == {"New", "Older"}
    # 14-day window excludes the older.
    rows14 = read_recent_decisions("win", days=14)
    assert {r.title for r in rows14} == {"New"}


# ---------------------------------------------------------------------------
# open_questions.md
# ---------------------------------------------------------------------------


def test_read_open_questions_round_trip(memory_root: Path) -> None:
    _write(
        memory_root,
        "oq",
        "open_questions.md",
        """
        ---
        question: Why is stage 3 elongating?
        asked_at: 2026-05-09
        context: Needs Sales specialist deep-dive.
        ---
        Median stage-3 days rose from 18 to 30.

        ---
        question: Is Western GRR a single-team problem?
        asked_at: 2026-05-08
        ---
        Western churn 4pp above other regions.
        """,
    )
    rows = read_open_questions("oq")
    assert len(rows) == 2
    assert all(isinstance(r, OpenQuestionRow) for r in rows)
    assert rows[0].question == "Why is stage 3 elongating?"
    assert rows[0].context == "Needs Sales specialist deep-dive."
    # Missing context defaults to "".
    assert rows[1].context == ""


def test_read_open_questions_missing_file(memory_root: Path) -> None:
    assert read_open_questions("ghost") == []


# ---------------------------------------------------------------------------
# trajectory
# ---------------------------------------------------------------------------


def test_compute_trajectory_picks_up_new_findings(memory_root: Path) -> None:
    recent = _today_iso(-2)
    old = _today_iso(-30)
    _write(
        memory_root,
        "tr",
        "findings.md",
        f"""
        ---
        priority: P1
        urgency: this_week
        status: open
        first_seen: {recent}
        decision_required: false
        confidence: HIGH
        ---
        New thing this week.

        ---
        priority: P2
        urgency: this_quarter
        status: open
        first_seen: {old}
        decision_required: false
        confidence: MEDIUM
        ---
        Old thing.
        """,
    )

    traj = compute_trajectory("tr", days=7)
    assert isinstance(traj, TrajectoryBlock)
    assert "New thing this week." in traj.new_this_week
    assert "Old thing." not in traj.new_this_week


def test_compute_trajectory_empty_when_no_files(memory_root: Path) -> None:
    traj = compute_trajectory("ghost")
    assert isinstance(traj, TrajectoryBlock)
    assert traj.improving == []
    assert traj.worsening == []
    assert traj.new_this_week == []


def test_compute_trajectory_uses_resolved_md(memory_root: Path) -> None:
    _write(
        memory_root,
        "rs",
        "resolved.md",
        """
        ---
        priority: P2
        urgency: this_quarter
        status: resolved
        first_seen: 2026-04-15
        decision_required: false
        confidence: HIGH
        ---
        Q1 pipeline shortfall.
        """,
    )
    traj = compute_trajectory("rs")
    assert "Q1 pipeline shortfall." in traj.improving


# ---------------------------------------------------------------------------
# compute_surface integration
# ---------------------------------------------------------------------------


def test_compute_surface_wires_all_readers(memory_root: Path) -> None:
    today = _today_iso(0)
    _write(
        memory_root,
        "all",
        "metrics.md",
        """
        ---
        name: ARR
        value: $1M
        delta_vs_prior: +1%
        status: monitor
        as_of: 2026-05-01
        ---
        ok
        """,
    )
    _write(
        memory_root,
        "all",
        "findings.md",
        f"""
        ---
        priority: P1
        urgency: this_week
        status: open
        first_seen: {today}
        decision_required: false
        confidence: HIGH
        ---
        Brand new finding.
        """,
    )
    _write(
        memory_root,
        "all",
        "decisions.md",
        f"""
        ---
        title: A decision
        decided_at: {today}
        decision: acted
        ---
        body
        """,
    )
    _write(
        memory_root,
        "all",
        "open_questions.md",
        """
        ---
        question: Why?
        asked_at: 2026-05-09
        ---
        ctx
        """,
    )

    state = compute_surface("all")
    assert isinstance(state, SurfaceState)
    assert len(state.key_metrics) == 1
    assert len(state.open_findings) == 1
    assert len(state.recent_decisions) == 1
    assert len(state.open_questions) == 1
    assert "Brand new finding." in state.trajectory.new_this_week


# ---------------------------------------------------------------------------
# cost block (Plan #35 integration)
# ---------------------------------------------------------------------------


class _FakeCursor:
    """Minimal psycopg2-style cursor that replays scripted rows.

    Each call to `execute` consumes the next entry in `script` and stores its
    rows. `fetchone()` returns the first row (or None). `fetchall()` returns
    the full list. Supports the context-manager protocol so the production
    code's `with conn.cursor(...) as cur:` block works unchanged.
    """

    def __init__(self, script: list[list[dict] | None]) -> None:
        self._script = script
        self._idx = 0
        self._rows: list[dict] = []

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple) -> None:
        if self._idx >= len(self._script):
            self._rows = []
            return
        entry = self._script[self._idx]
        self._idx += 1
        self._rows = list(entry) if entry else []

    def fetchone(self) -> dict | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[dict]:
        return list(self._rows)


class _FakeConn:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def cursor(self, cursor_factory=None) -> _FakeCursor:
        return self._cursor

    def close(self) -> None:
        return None


def _install_fake_db(
    monkeypatch: pytest.MonkeyPatch, script: list[list[dict] | None]
) -> None:
    """Patch db_adapter so read_cost_block's queries run against fake rows."""
    import db_adapter

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "postgres://fake")
    monkeypatch.setattr(db_adapter, "_connect", lambda: _FakeConn(_FakeCursor(script)))


def test_read_cost_block_populated_from_mocked_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mocked totals + top-task rows produce a CostBlock with the right shape."""
    totals_row = {
        "spend_7d": 42.5,
        "spend_30d": 180.0,
        "spend_prior_7d": 34.0,  # 7d / prior_7d = 1.25 → +25%
        "cache_read_7d": 800_000,
        "input_side_total_7d": 1_000_000,  # 80% cache hit
    }
    top_task_row = {"trigger": "ad_hoc_investigation", "cost_usd": 30.0}

    _install_fake_db(monkeypatch, [[totals_row], [top_task_row]])

    block = read_cost_block("acme")
    assert isinstance(block, CostBlock)
    assert block.trailing_7d_usd == 42.5
    assert block.trailing_30d_usd == 180.0
    assert block.trend_pct == 25.0
    assert block.top_task == "ad_hoc_investigation: $30.00"
    assert block.cache_hit_pct == 80.0
    assert block.updated_at  # ISO timestamp populated


def test_read_cost_block_none_when_db_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DATABASE_URL unset → read_cost_block returns None."""
    import db_adapter

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "")
    assert read_cost_block("acme") is None


def test_read_cost_block_db_failure_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any exception during DB connect/query yields None — never raise."""
    import db_adapter

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "postgres://fake")

    def _boom() -> None:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(db_adapter, "_connect", _boom)
    assert read_cost_block("acme") is None


def test_read_cost_block_zero_prior_baseline_caps_trend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When prior 7d spend is zero but current 7d is positive, trend caps at +999."""
    totals_row = {
        "spend_7d": 10.0,
        "spend_30d": 10.0,
        "spend_prior_7d": 0.0,
        "cache_read_7d": 0,
        "input_side_total_7d": 0,
    }
    _install_fake_db(monkeypatch, [[totals_row], None])
    block = read_cost_block("newportco")
    assert block is not None
    assert block.trend_pct == 999.0
    assert block.top_task == ""
    assert block.cache_hit_pct == 0.0


def test_compute_surface_includes_cost_block_when_available(
    memory_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """compute_surface wires read_cost_block — SurfaceState.cost_block populated."""
    fake = CostBlock(
        trailing_7d_usd=12.5,
        trailing_30d_usd=50.0,
        trend_pct=10.0,
        top_task="cron: $8.00",
        cache_hit_pct=72.4,
        updated_at="2026-05-11T14:00:00",
    )
    monkeypatch.setattr(surface_compute, "read_cost_block", lambda portco: fake)
    state = compute_surface("withcost")
    assert state.cost_block == fake


def test_compute_surface_cost_block_none_when_unavailable(
    memory_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When read_cost_block returns None, SurfaceState.cost_block is None."""
    monkeypatch.setattr(surface_compute, "read_cost_block", lambda portco: None)
    state = compute_surface("nocost")
    assert state.cost_block is None
