"""Tests for ``scripts/cost_backfill_30d.py`` — one-shot 30d Admin API backfill.

The script is a thin wrapper around ``cost_collector.pull_anthropic_daily_costs``.
We mock the pull function + DB so the test runs offline.

Run:
    cd orchestrator && python3 -m pytest cost_backfill_30d_test.py -q
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Same env-stubbing trick as cost_collector_test — config.py raises at import
# time if these are missing and there's no .env, which is the case for fresh
# worktrees and CI.
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
sys.modules.pop("cost_collector", None)
sys.modules.pop("cost_backfill_30d", None)

# Make scripts/ importable as a flat module path. The script lives at
# repo_root/scripts/cost_backfill_30d.py.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


@pytest.fixture
def script_module(monkeypatch):
    """Import cost_backfill_30d fresh with a non-empty ANTHROPIC_ADMIN_KEY.

    The script reads ``config.ANTHROPIC_ADMIN_KEY`` at call time (not import
    time), so we patch it on the imported config module.
    """
    sys.modules.pop("cost_backfill_30d", None)
    import cost_backfill_30d  # type: ignore[import-not-found]

    monkeypatch.setattr(
        cost_backfill_30d.config, "ANTHROPIC_ADMIN_KEY", "sk-admin-test"
    )
    monkeypatch.setattr(cost_backfill_30d.db_adapter, "DATABASE_URL", "postgres://test")
    return cost_backfill_30d


def test_expected_dates_returns_correct_window(script_module):
    """Window covers the trailing N days, today excluded, in descending order."""
    dates = script_module._expected_dates(5)
    assert len(dates) == 5
    # All ISO YYYY-MM-DD
    for d in dates:
        assert len(d) == 10 and d[4] == "-" and d[7] == "-"
    # Descending — most recent first
    assert dates == sorted(dates, reverse=True)


def test_main_dry_run_skips_pull(script_module, monkeypatch, caplog):
    """--dry-run logs the window and exits 0 without calling the pull function."""
    caplog.set_level(logging.INFO, logger="cost_backfill_30d")
    pull_mock = MagicMock()
    monkeypatch.setattr(
        script_module.cost_collector, "pull_anthropic_daily_costs", pull_mock
    )
    monkeypatch.setattr(sys, "argv", ["cost_backfill_30d.py", "--dry-run"])
    rc = script_module.main()
    assert rc == 0
    pull_mock.assert_not_called()
    assert any("dry-run" in r.message.lower() for r in caplog.records)


def test_main_default_calls_pull_with_30_days(script_module, monkeypatch):
    """Default invocation calls pull_anthropic_daily_costs(days_back=30) once."""
    pull_mock = MagicMock(return_value=7)
    monkeypatch.setattr(
        script_module.cost_collector, "pull_anthropic_daily_costs", pull_mock
    )
    # Skip the post-run DB summary query — no real DB available.
    monkeypatch.setattr(script_module, "_query_rows_per_date", lambda _d: {})
    monkeypatch.setattr(sys, "argv", ["cost_backfill_30d.py"])
    rc = script_module.main()
    assert rc == 0
    pull_mock.assert_called_once_with(days_back=30)


def test_main_custom_days_forwarded(script_module, monkeypatch):
    """--days N is forwarded to pull_anthropic_daily_costs(days_back=N)."""
    pull_mock = MagicMock(return_value=3)
    monkeypatch.setattr(
        script_module.cost_collector, "pull_anthropic_daily_costs", pull_mock
    )
    monkeypatch.setattr(script_module, "_query_rows_per_date", lambda _d: {})
    monkeypatch.setattr(sys, "argv", ["cost_backfill_30d.py", "--days", "7"])
    rc = script_module.main()
    assert rc == 0
    pull_mock.assert_called_once_with(days_back=7)


def test_main_zero_days_rejected(script_module, monkeypatch, caplog):
    """--days 0 (or negative) exits 1 with a clear error and does not call pull."""
    caplog.set_level(logging.INFO, logger="cost_backfill_30d")
    pull_mock = MagicMock()
    monkeypatch.setattr(
        script_module.cost_collector, "pull_anthropic_daily_costs", pull_mock
    )
    monkeypatch.setattr(sys, "argv", ["cost_backfill_30d.py", "--days", "0"])
    rc = script_module.main()
    assert rc == 1
    pull_mock.assert_not_called()
    assert any("positive" in r.message.lower() for r in caplog.records)


def test_main_missing_admin_key_fails(script_module, monkeypatch, caplog):
    """Without ANTHROPIC_ADMIN_KEY, the script exits 1 and skips the pull."""
    caplog.set_level(logging.ERROR, logger="cost_backfill_30d")
    monkeypatch.setattr(script_module.config, "ANTHROPIC_ADMIN_KEY", "")
    pull_mock = MagicMock()
    monkeypatch.setattr(
        script_module.cost_collector, "pull_anthropic_daily_costs", pull_mock
    )
    monkeypatch.setattr(sys, "argv", ["cost_backfill_30d.py"])
    rc = script_module.main()
    assert rc == 1
    pull_mock.assert_not_called()
    assert any("ANTHROPIC_ADMIN_KEY" in r.message for r in caplog.records)


def test_main_missing_db_url_fails(script_module, monkeypatch, caplog):
    """Without DATABASE_URL, the script exits 1 and skips the pull."""
    caplog.set_level(logging.ERROR, logger="cost_backfill_30d")
    monkeypatch.setattr(script_module.db_adapter, "DATABASE_URL", "")
    pull_mock = MagicMock()
    monkeypatch.setattr(
        script_module.cost_collector, "pull_anthropic_daily_costs", pull_mock
    )
    monkeypatch.setattr(sys, "argv", ["cost_backfill_30d.py"])
    rc = script_module.main()
    assert rc == 1
    pull_mock.assert_not_called()
    assert any("DATABASE_URL" in r.message for r in caplog.records)


def test_main_summary_uses_db_breakdown(script_module, monkeypatch, caplog):
    """The end-of-run summary reports dates-with-data vs. dates-with-no-data."""
    caplog.set_level(logging.INFO, logger="cost_backfill_30d")
    monkeypatch.setattr(
        script_module.cost_collector,
        "pull_anthropic_daily_costs",
        MagicMock(return_value=2),
    )
    monkeypatch.setattr(
        script_module,
        "_query_rows_per_date",
        lambda dates: {dates[0]: 3, dates[1]: 0, dates[2]: 5},
    )
    monkeypatch.setattr(sys, "argv", ["cost_backfill_30d.py", "--days", "3"])
    rc = script_module.main()
    assert rc == 0
    summary = "\n".join(r.message for r in caplog.records if r.levelname == "INFO")
    assert "Backfill summary" in summary
    assert "Dates with data:  2" in summary
    assert "Dates with no data: 1" in summary
