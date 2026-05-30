"""Tests for ``roi_signal`` (Plan #30 D3).

Covers:

  * Pure-math edge cases for ``_roi_cells``: no feedback, all-negative,
    no positives, no cost, mixed feedback, and no events.

  * ``compute_roi`` end-to-end with mocked DB rows:
      - Cost-only portco (spend, zero feedback) → ROI cells are None.
      - Mixed feedback portco → cost_per_positive / cost_per_useful /
        useful_rate match the documented formulas.
      - All-negative portco → ``cost_per_useful == float('inf')``,
        ``useful_rate == 0.0``.
      - Feedback-only portco (no cost in window) → useful_rate present,
        cost-per-X cells are None.
      - Window math: 7d vs 90d windows pass the right (start, end)
        params to the DB fetchers.

  * Watch line + ROI block rendering in
    ``cost_digest.build_digest_message``:
      - ``useful_rate < 50%`` → leading ``:warning: Watch`` banner.
      - All-positive day → no watch banner, no leading whitespace,
        but the block is still emitted.
      - Empty ROI dict → no block at all (parity with the compresr block).

DB writes are mocked. No live Slack, no live psycopg2.

Run:
    cd orchestrator && python3 -m pytest roi_signal_test.py -q
"""

from __future__ import annotations

import math
import os
import sys
from datetime import date, timedelta
from unittest.mock import MagicMock


# Same defensive .env stubbing pattern as feedback_capture_test / cost_digest_test —
# keep imports clean when the worktree has no .env file.
for _k in (
    "ANTHROPIC_API_KEY",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "SLACK_CHANNEL_ID",
    "ENVIRONMENT_ID",
    "DREAM_AGENT_ID",
    "COORDINATOR_ID",
    "QUICK_AGENT_ID",
    "METHODOLOGY_STORE_ID",
    "HEALTH_STORE_ID",
):
    os.environ.setdefault(_k, "test-stub")

sys.modules.pop("config", None)
sys.modules.pop("roi_signal", None)
sys.modules.pop("cost_digest", None)


# ──────────────────────────────────────────────────────────────────────────
# _roi_cells — pure math
# ──────────────────────────────────────────────────────────────────────────


def test_roi_cells_no_feedback_all_none():
    """Zero total_events → all three cells None regardless of cost."""
    import roi_signal

    cells = roi_signal._roi_cells(
        total_cost_usd=12.40,
        positive_count=0,
        negative_count=0,
        neutral_count=0,
    )
    assert cells == {
        "cost_per_positive": None,
        "cost_per_useful": None,
        "useful_rate": None,
    }


def test_roi_cells_mixed_signals_correct_math():
    """4 pos / 1 neg / 2 neu, $12.40 → matches the docstring example exactly."""
    import roi_signal

    cells = roi_signal._roi_cells(
        total_cost_usd=12.40,
        positive_count=4,
        negative_count=1,
        neutral_count=2,
    )
    # cost_per_positive = 12.40 / 4 = 3.10
    assert cells["cost_per_positive"] == 3.10
    # cost_per_useful = 12.40 / (4 + 2) = 2.0666…
    assert cells["cost_per_useful"] == 12.40 / 6
    # useful_rate = (4 + 2) / (4 + 1 + 2) = 6/7
    assert cells["useful_rate"] == 6 / 7


def test_roi_cells_all_negative_inf_sentinel():
    """All-negative portco → cost_per_useful = inf, useful_rate = 0.0."""
    import roi_signal

    cells = roi_signal._roi_cells(
        total_cost_usd=3.20,
        positive_count=0,
        negative_count=2,
        neutral_count=0,
    )
    assert cells["cost_per_positive"] is None  # 0 positives
    assert cells["cost_per_useful"] == float("inf")
    assert cells["useful_rate"] == 0.0


def test_roi_cells_zero_positives_but_neutral_present():
    """0 pos / 1 neg / 1 neu → cost_per_positive None, cost_per_useful = cost / 1."""
    import roi_signal

    cells = roi_signal._roi_cells(
        total_cost_usd=4.00,
        positive_count=0,
        negative_count=1,
        neutral_count=1,
    )
    assert cells["cost_per_positive"] is None
    assert cells["cost_per_useful"] == 4.00
    assert cells["useful_rate"] == 0.5


def test_roi_cells_no_cost_keeps_useful_rate():
    """No cost but feedback exists → cost-per-X None, useful_rate present."""
    import roi_signal

    cells = roi_signal._roi_cells(
        total_cost_usd=None,
        positive_count=2,
        negative_count=1,
        neutral_count=0,
    )
    assert cells["cost_per_positive"] is None
    assert cells["cost_per_useful"] is None
    assert cells["useful_rate"] == 2 / 3


def test_roi_cells_zero_cost_treated_like_no_cost():
    """total_cost_usd == 0 also collapses cost-per-X to None (not 0)."""
    import roi_signal

    cells = roi_signal._roi_cells(
        total_cost_usd=0.0,
        positive_count=1,
        negative_count=0,
        neutral_count=0,
    )
    assert cells["cost_per_positive"] is None
    assert cells["cost_per_useful"] is None
    assert cells["useful_rate"] == 1.0


# ──────────────────────────────────────────────────────────────────────────
# compute_roi — end-to-end with mocked DB fetchers
# ──────────────────────────────────────────────────────────────────────────


def _stub_fetchers(monkeypatch, *, cost_rows, feedback_rows):
    """Patch the two DB-touching fetchers and capture their call args.

    Returns ``recorded_args`` — a dict updated in-place with the start/end
    dates each fetcher was invoked with. Lets a single test assert both the
    output shape AND the window math.
    """
    import roi_signal

    recorded_args: dict = {}

    def fake_cost(start, end):
        recorded_args["cost"] = (start, end)
        return cost_rows

    def fake_feedback(start, end):
        recorded_args["feedback"] = (start, end)
        return feedback_rows

    monkeypatch.setattr(roi_signal, "fetch_cost_per_portco", fake_cost)
    monkeypatch.setattr(roi_signal, "fetch_feedback_counts_per_portco", fake_feedback)
    return recorded_args


def test_compute_roi_cost_only_portco_yields_none_cells(monkeypatch):
    """A portco with spend but zero feedback → ROI cells are None."""
    import roi_signal

    _stub_fetchers(
        monkeypatch,
        cost_rows=[
            {"portco_key": "acme", "total_cost_usd": 8.40, "sessions": 12},
        ],
        feedback_rows=[],  # no feedback at all
    )

    result = roi_signal.compute_roi(window_days=7)
    assert result["window_days"] == 7
    assert len(result["by_portco"]) == 1
    row = result["by_portco"][0]
    assert row["portco_key"] == "acme"
    assert row["total_cost_usd"] == 8.40
    assert row["positive_count"] == 0
    assert row["negative_count"] == 0
    assert row["neutral_count"] == 0
    assert row["cost_per_positive"] is None
    assert row["cost_per_useful"] is None
    assert row["useful_rate"] is None


def test_compute_roi_mixed_feedback_correct_math(monkeypatch):
    """4/1/2 + $12.40 → matches the documented numbers exactly."""
    import roi_signal

    _stub_fetchers(
        monkeypatch,
        cost_rows=[
            {"portco_key": "acme", "total_cost_usd": 12.40, "sessions": 20},
        ],
        feedback_rows=[
            {
                "portco_key": "acme",
                "positive_count": 4,
                "negative_count": 1,
                "neutral_count": 2,
            }
        ],
    )

    result = roi_signal.compute_roi(window_days=7)
    row = result["by_portco"][0]
    assert row["positive_count"] == 4
    assert row["negative_count"] == 1
    assert row["neutral_count"] == 2
    assert row["cost_per_positive"] == 12.40 / 4
    assert row["cost_per_useful"] == 12.40 / 6
    assert row["useful_rate"] == 6 / 7

    # Overall mirrors the single-portco numbers when only one portco exists.
    overall = result["overall"]
    assert overall["total_cost_usd"] == 12.40
    assert overall["positive_count"] == 4
    assert overall["cost_per_useful"] == 12.40 / 6


def test_compute_roi_all_negative_portco_inf_sentinel(monkeypatch):
    """All-negative portco → cost_per_useful is inf, useful_rate is 0.0."""
    import roi_signal

    _stub_fetchers(
        monkeypatch,
        cost_rows=[
            {"portco_key": "acme", "total_cost_usd": 3.20, "sessions": 4},
        ],
        feedback_rows=[
            {
                "portco_key": "acme",
                "positive_count": 0,
                "negative_count": 2,
                "neutral_count": 0,
            }
        ],
    )

    result = roi_signal.compute_roi(window_days=7)
    row = result["by_portco"][0]
    assert row["cost_per_positive"] is None
    assert math.isinf(row["cost_per_useful"]) and row["cost_per_useful"] > 0
    assert row["useful_rate"] == 0.0


def test_compute_roi_feedback_only_portco_no_cost(monkeypatch):
    """Feedback exists for a portco with no spend in window → cost cells None,
    useful_rate present (so the operator still sees the signal)."""
    import roi_signal

    _stub_fetchers(
        monkeypatch,
        cost_rows=[
            {"portco_key": "acme", "total_cost_usd": 5.00, "sessions": 3},
        ],
        feedback_rows=[
            {
                "portco_key": "acme",
                "positive_count": 1,
                "negative_count": 0,
                "neutral_count": 0,
            },
            {
                "portco_key": "lateral",  # cost not in window, feedback is
                "positive_count": 2,
                "negative_count": 0,
                "neutral_count": 1,
            },
        ],
    )

    result = roi_signal.compute_roi(window_days=7)
    rows = {r["portco_key"]: r for r in result["by_portco"]}
    assert "lateral" in rows
    lateral = rows["lateral"]
    assert lateral["total_cost_usd"] == 0.0
    assert lateral["cost_per_positive"] is None
    assert lateral["cost_per_useful"] is None
    assert lateral["useful_rate"] == 1.0


def test_compute_roi_sorted_by_cost_descending(monkeypatch):
    """by_portco ordering matches the digest's other rollups (highest spend first)."""
    import roi_signal

    _stub_fetchers(
        monkeypatch,
        cost_rows=[
            {"portco_key": "small", "total_cost_usd": 1.00, "sessions": 1},
            {"portco_key": "big", "total_cost_usd": 50.00, "sessions": 30},
            {"portco_key": "mid", "total_cost_usd": 10.00, "sessions": 8},
        ],
        feedback_rows=[],
    )

    result = roi_signal.compute_roi(window_days=7)
    ordered = [r["portco_key"] for r in result["by_portco"]]
    assert ordered == ["big", "mid", "small"]


def test_compute_roi_window_math_7_vs_90_days(monkeypatch):
    """window_days=7 → 7-day inclusive window; window_days=90 → 90-day window.

    Both queries see the same end_date (today) but different start_dates.
    """
    import roi_signal

    today = date.today()

    args_7 = _stub_fetchers(monkeypatch, cost_rows=[], feedback_rows=[])
    roi_signal.compute_roi(window_days=7)
    assert args_7["cost"][1] == today
    assert args_7["cost"][0] == today - timedelta(days=6)
    assert args_7["feedback"] == args_7["cost"]

    args_90 = _stub_fetchers(monkeypatch, cost_rows=[], feedback_rows=[])
    roi_signal.compute_roi(window_days=90)
    assert args_90["cost"][1] == today
    assert args_90["cost"][0] == today - timedelta(days=89)
    assert args_90["feedback"] == args_90["cost"]


def test_compute_roi_rejects_non_positive_window():
    """window_days must be positive — guards against accidental 0/negative inputs."""
    import pytest

    import roi_signal

    with pytest.raises(ValueError):
        roi_signal.compute_roi(window_days=0)
    with pytest.raises(ValueError):
        roi_signal.compute_roi(window_days=-1)


def test_compute_roi_no_database_url_empty_result(monkeypatch):
    """DATABASE_URL unset → both fetchers return [], result is empty rollups."""
    import db_adapter
    import roi_signal

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "")

    result = roi_signal.compute_roi(window_days=7)
    assert result["window_days"] == 7
    assert result["by_portco"] == []
    overall = result["overall"]
    assert overall["total_cost_usd"] == 0
    assert overall["positive_count"] == 0
    assert overall["cost_per_useful"] is None


def test_compute_roi_overall_sums_across_portcos(monkeypatch):
    """Overall block sums cost+feedback across every portco before computing ROI."""
    import roi_signal

    _stub_fetchers(
        monkeypatch,
        cost_rows=[
            {"portco_key": "delta", "total_cost_usd": 10.00, "sessions": 5},
            {"portco_key": "acme", "total_cost_usd": 4.00, "sessions": 2},
        ],
        feedback_rows=[
            {
                "portco_key": "delta",
                "positive_count": 4,
                "negative_count": 0,
                "neutral_count": 0,
            },
            {
                "portco_key": "acme",
                "positive_count": 0,
                "negative_count": 2,
                "neutral_count": 0,
            },
        ],
    )

    result = roi_signal.compute_roi(window_days=7)
    overall = result["overall"]
    assert overall["total_cost_usd"] == 14.00
    assert overall["positive_count"] == 4
    assert overall["negative_count"] == 2
    assert overall["neutral_count"] == 0
    # cost_per_positive = 14 / 4 = 3.50
    assert overall["cost_per_positive"] == 14.00 / 4
    # cost_per_useful = 14 / (4 + 0) = 3.50 (neutral count is 0)
    assert overall["cost_per_useful"] == 14.00 / 4
    # useful_rate = 4 / 6 = 0.6666…
    assert overall["useful_rate"] == 4 / 6


# ──────────────────────────────────────────────────────────────────────────
# render_digest_block — pure rendering
# ──────────────────────────────────────────────────────────────────────────


def test_render_digest_block_empty_returns_empty_string():
    """No by_portco data → empty string (caller appends nothing)."""
    import roi_signal

    assert roi_signal.render_digest_block({}) == ""
    assert roi_signal.render_digest_block({"by_portco": []}) == ""


def test_render_digest_block_no_watch_when_all_useful(monkeypatch):
    """useful_rate >= threshold for every portco → no watch banner."""
    import roi_signal

    roi = {
        "window_days": 7,
        "by_portco": [
            {
                "portco_key": "acme",
                "total_cost_usd": 12.40,
                "positive_count": 4,
                "negative_count": 1,
                "neutral_count": 2,
                "cost_per_positive": 3.10,
                "cost_per_useful": 12.40 / 6,
                "useful_rate": 6 / 7,
            }
        ],
    }
    block = roi_signal.render_digest_block(roi)
    assert ":warning: Watch" not in block
    assert "Cost per useful answer (last 7d)" in block
    assert "acme" in block
    assert "$12.40" in block
    assert "4/1/2" in block
    # 86% useful — formatted with no decimal places.
    assert "86%" in block


def test_render_digest_block_watch_line_when_useful_below_50pct():
    """Any portco with useful_rate < 0.50 triggers a leading watch banner."""
    import roi_signal

    roi = {
        "window_days": 7,
        "by_portco": [
            {
                "portco_key": "acme",
                "total_cost_usd": 3.20,
                "positive_count": 0,
                "negative_count": 2,
                "neutral_count": 0,
                "cost_per_positive": None,
                "cost_per_useful": float("inf"),
                "useful_rate": 0.0,
            }
        ],
    }
    block = roi_signal.render_digest_block(roi)
    first_line = block.split("\n", 1)[0]
    assert ":warning: Watch" in first_line
    assert "acme" in first_line
    assert "0%" in first_line
    # Inf sentinel renders as a clear visual marker, not a literal "inf".
    assert "∞" in block


def test_render_digest_block_no_watch_for_portco_without_feedback():
    """A portco with useful_rate=None (no feedback) must NOT trigger the watch.

    None means "ungraded" — we can't say it's regressing.
    """
    import roi_signal

    roi = {
        "window_days": 7,
        "by_portco": [
            {
                "portco_key": "newco",
                "total_cost_usd": 1.00,
                "positive_count": 0,
                "negative_count": 0,
                "neutral_count": 0,
                "cost_per_positive": None,
                "cost_per_useful": None,
                "useful_rate": None,
            }
        ],
    }
    block = roi_signal.render_digest_block(roi)
    assert ":warning: Watch" not in block
    assert "n/a" in block  # both $/useful and useful% render as n/a


def test_render_digest_block_window_days_label_uses_input():
    """The block header reflects the window the caller passed (7d vs 90d)."""
    import roi_signal

    roi = {
        "window_days": 90,
        "by_portco": [
            {
                "portco_key": "acme",
                "total_cost_usd": 100.00,
                "positive_count": 30,
                "negative_count": 5,
                "neutral_count": 5,
                "cost_per_positive": 100.00 / 30,
                "cost_per_useful": 100.00 / 35,
                "useful_rate": 35 / 40,
            }
        ],
    }
    block = roi_signal.render_digest_block(roi)
    assert "Cost per useful answer (last 90d)" in block


# ──────────────────────────────────────────────────────────────────────────
# Digest integration — build_digest_message wires the ROI block
# ──────────────────────────────────────────────────────────────────────────


def _recon_clean() -> dict:
    """Drift-free recon dict so the digest doesn't lead with its own watch line."""
    return {
        "date": "2026-05-10",
        "local_total_usd": 1.0,
        "anthropic_total_usd": 1.0,
        "drift_usd": 0.0,
        "drift_pct": 0.0,
    }


def test_build_digest_includes_roi_block_when_roi_present():
    """``roi`` arg with by_portco data → block appears in the digest body."""
    import cost_digest

    roi = {
        "window_days": 7,
        "by_portco": [
            {
                "portco_key": "acme",
                "total_cost_usd": 12.40,
                "positive_count": 4,
                "negative_count": 1,
                "neutral_count": 2,
                "cost_per_positive": 3.10,
                "cost_per_useful": 12.40 / 6,
                "useful_rate": 6 / 7,
            }
        ],
    }

    msg = cost_digest.build_digest_message(
        date(2026, 5, 10),
        total=1.0,
        portco_rows=[{"portco": "acme", "cost_usd": 1.0, "sessions": 1}],
        trigger_rows=[{"trigger": "cron", "cost_usd": 1.0, "sessions": 1}],
        cache_pct=70.0,
        recon=_recon_clean(),
        top_sessions=[],
        roi=roi,
    )
    assert "Cost per useful answer (last 7d)" in msg
    assert "acme" in msg
    assert "4/1/2" in msg
    assert ":warning: Watch — useful_rate" not in msg  # 86% is above threshold


def test_build_digest_no_roi_block_when_roi_none():
    """``roi=None`` → no block (matches the compresr None gating)."""
    import cost_digest

    msg = cost_digest.build_digest_message(
        date(2026, 5, 10),
        total=1.0,
        portco_rows=[{"portco": "acme", "cost_usd": 1.0, "sessions": 1}],
        trigger_rows=[{"trigger": "cron", "cost_usd": 1.0, "sessions": 1}],
        cache_pct=70.0,
        recon=_recon_clean(),
        top_sessions=[],
        roi=None,
    )
    assert "Cost per useful answer" not in msg


def test_build_digest_no_roi_block_when_by_portco_empty():
    """``roi`` dict with empty by_portco → no block (renderer returns "")."""
    import cost_digest

    msg = cost_digest.build_digest_message(
        date(2026, 5, 10),
        total=1.0,
        portco_rows=[],
        trigger_rows=[],
        cache_pct=None,
        recon=_recon_clean(),
        top_sessions=[],
        roi={"window_days": 7, "by_portco": [], "overall": {}},
    )
    assert "Cost per useful answer" not in msg


def test_build_digest_watch_line_appears_when_useful_below_threshold():
    """When ROI has a portco below threshold, the watch banner appears in the body."""
    import cost_digest

    roi = {
        "window_days": 7,
        "by_portco": [
            {
                "portco_key": "acme",
                "total_cost_usd": 3.20,
                "positive_count": 0,
                "negative_count": 2,
                "neutral_count": 0,
                "cost_per_positive": None,
                "cost_per_useful": float("inf"),
                "useful_rate": 0.0,
            }
        ],
    }
    msg = cost_digest.build_digest_message(
        date(2026, 5, 10),
        total=1.0,
        portco_rows=[{"portco": "acme", "cost_usd": 1.0, "sessions": 1}],
        trigger_rows=[{"trigger": "cron", "cost_usd": 1.0, "sessions": 1}],
        cache_pct=60.0,
        recon=_recon_clean(),
        top_sessions=[],
        roi=roi,
    )
    assert "useful_rate < 50%" in msg
    assert "acme (0%)" in msg
    # Drift-clean recon → no drift-watch banner, just the ROI one.
    assert msg.count(":warning: Watch") == 1


def test_send_daily_cost_digest_calls_compute_roi(monkeypatch):
    """End-to-end: send_daily_cost_digest queries roi_signal and renders it.

    Stubs every DB-touching helper so the test pins exactly the orchestration
    behavior: that the digest fetches ROI alongside the spend rollups, and
    passes the result into build_digest_message.
    """
    import cost_digest
    import db_adapter
    import roi_signal

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "postgres://test")
    monkeypatch.setattr(cost_digest, "_total_cost", lambda d: 1.00)
    monkeypatch.setattr(
        cost_digest,
        "_portco_totals",
        lambda d: [{"portco": "acme", "cost_usd": 1.00, "sessions": 1}],
    )
    monkeypatch.setattr(
        cost_digest,
        "_trigger_totals",
        lambda d: [{"trigger": "cron", "cost_usd": 1.00, "sessions": 1}],
    )
    monkeypatch.setattr(cost_digest, "_cache_hit_pct", lambda d: 60.0)
    monkeypatch.setattr(cost_digest, "_top_sessions", lambda d, limit=5: [])
    monkeypatch.setattr(cost_digest, "compute_reconciliation", lambda d: _recon_clean())

    fake_roi = {
        "window_days": 7,
        "by_portco": [
            {
                "portco_key": "acme",
                "total_cost_usd": 8.00,
                "positive_count": 2,
                "negative_count": 0,
                "neutral_count": 2,
                "cost_per_positive": 4.00,
                "cost_per_useful": 2.00,
                "useful_rate": 1.0,
            }
        ],
        "overall": {
            "total_cost_usd": 8.00,
            "positive_count": 2,
            "negative_count": 0,
            "neutral_count": 2,
            "cost_per_positive": 4.00,
            "cost_per_useful": 2.00,
            "useful_rate": 1.0,
        },
    }
    captured = {}

    def fake_compute_roi(window_days=7):
        captured["window_days"] = window_days
        return fake_roi

    monkeypatch.setattr(roi_signal, "compute_roi", fake_compute_roi)

    sender = MagicMock()
    result = cost_digest.send_daily_cost_digest(
        date(2026, 5, 10), sender=sender, admin_ids=["U_TEST"]
    )

    assert captured["window_days"] == 7, (
        "send_daily_cost_digest must request the 7-day ROI window"
    )
    assert result["sent"] == 1
    msg = sender.call_args.args[1]
    assert "Cost per useful answer (last 7d)" in msg
    assert "acme" in msg
    assert "2/0/2" in msg
    assert "100%" in msg  # useful_rate


def test_send_daily_cost_digest_roi_lookup_failure_swallowed(monkeypatch):
    """compute_roi raising → digest still ships, ROI block omitted."""
    import cost_digest
    import db_adapter
    import roi_signal

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "postgres://test")
    monkeypatch.setattr(cost_digest, "_total_cost", lambda d: 1.00)
    monkeypatch.setattr(cost_digest, "_portco_totals", lambda d: [])
    monkeypatch.setattr(cost_digest, "_trigger_totals", lambda d: [])
    monkeypatch.setattr(cost_digest, "_cache_hit_pct", lambda d: None)
    monkeypatch.setattr(cost_digest, "_top_sessions", lambda d, limit=5: [])
    monkeypatch.setattr(cost_digest, "compute_reconciliation", lambda d: _recon_clean())

    def boom(window_days=7):
        raise RuntimeError("roi down")

    monkeypatch.setattr(roi_signal, "compute_roi", boom)

    sender = MagicMock()
    result = cost_digest.send_daily_cost_digest(
        date(2026, 5, 10), sender=sender, admin_ids=["U_TEST"]
    )
    assert result["sent"] == 1
    msg = sender.call_args.args[1]
    assert "Cost per useful answer" not in msg  # block omitted
