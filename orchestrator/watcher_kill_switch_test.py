"""Tests for ``orchestrator/watcher_kill_switch.py``.

Covers:
    - kill_switch_tripped True when count_unmerged_unreviewed_24h returns ≥ 5
    - kill_switch_tripped safe-default False on count exception
    - compute_metrics envelope shape always present
    - maybe_dm_admin_on_trip sends ONE DM per trip event (not every check)
    - DM state resets when count drops below threshold
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "orchestrator"))

for _key, _value in {
    "DATABASE_URL": "postgres://test/db",
    "WATCHER_GH_TOKEN": "ghp_test_token",
    "ANTHROPIC_API_KEY": "sk-ant-test",
    "SLACK_BOT_TOKEN": "xoxb-test",
    "SLACK_APP_TOKEN": "xapp-test",
    "SLACK_CHANNEL_ID": "C0TEST",
    "ENVIRONMENT_ID": "env_test",
    "DREAM_AGENT_ID": "agent_test_dream",
    "COORDINATOR_ID": "agent_test_coord",
    "QUICK_AGENT_ID": "agent_test_quick",
    "METHODOLOGY_STORE_ID": "memstore_test_m",
    "HEALTH_STORE_ID": "memstore_test_h",
}.items():
    os.environ.setdefault(_key, _value)


import watcher_kill_switch as wks  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_dm_state():
    wks._reset_dm_state_for_tests()
    yield
    wks._reset_dm_state_for_tests()


# ───────────────────────────────────────────────────────────────────────
# kill_switch_tripped
# ───────────────────────────────────────────────────────────────────────


def test_tripped_when_count_at_threshold():
    fake = MagicMock(return_value=5)
    with patch("watcher_pending_db.count_unmerged_unreviewed_24h", fake):
        assert wks.kill_switch_tripped() is True


def test_tripped_when_count_above_threshold():
    fake = MagicMock(return_value=10)
    with patch("watcher_pending_db.count_unmerged_unreviewed_24h", fake):
        assert wks.kill_switch_tripped() is True


def test_not_tripped_when_count_below_threshold():
    fake = MagicMock(return_value=4)
    with patch("watcher_pending_db.count_unmerged_unreviewed_24h", fake):
        assert wks.kill_switch_tripped() is False


def test_safe_default_when_count_raises():
    fake = MagicMock(side_effect=RuntimeError("DB down"))
    with patch("watcher_pending_db.count_unmerged_unreviewed_24h", fake):
        assert wks.kill_switch_tripped() is False


# ───────────────────────────────────────────────────────────────────────
# compute_metrics envelope
# ───────────────────────────────────────────────────────────────────────


def test_metrics_envelope_always_present():
    """Even on full DB failure, the dict shape is the contract."""
    with patch("watcher_pending_db.list_completed_24h", side_effect=RuntimeError), \
         patch(
             "watcher_pending_db.count_unmerged_unreviewed_24h",
             side_effect=RuntimeError,
         ):
        metrics = wks.compute_metrics()
    assert metrics["lookback_hours"] == wks.KILL_SWITCH_LOOKBACK_HOURS
    assert metrics["kill_switch_threshold"] == wks.KILL_SWITCH_THRESHOLD
    assert metrics["kill_switch_tripped"] is False
    assert metrics["auto_pr_count_24h"] == 0
    assert metrics["status_counts_24h"] == {}


def test_metrics_counts_status_breakdown():
    rows = [
        {"id": 1, "status": "completed"},
        {"id": 2, "status": "completed"},
        {"id": 3, "status": "diagnose_only"},
    ]
    with patch("watcher_pending_db.list_completed_24h", return_value=rows), \
         patch("watcher_pending_db.count_unmerged_unreviewed_24h", return_value=0):
        metrics = wks.compute_metrics()
    # list_completed_24h returns completed AND diagnose_only per PR 1 contract,
    # but kill-switch (count_unmerged_unreviewed_24h) excludes diagnose_only.
    # The metrics view shows the breakdown as-is.
    assert metrics["auto_pr_count_24h"] == 3
    assert metrics["status_counts_24h"]["completed"] == 2
    assert metrics["status_counts_24h"]["diagnose_only"] == 1


# ───────────────────────────────────────────────────────────────────────
# maybe_dm_admin_on_trip
# ───────────────────────────────────────────────────────────────────────


def test_dm_sent_once_per_trip():
    fake_send = MagicMock()
    with patch("watcher_pending_db.list_completed_24h", return_value=[]), \
         patch("watcher_pending_db.count_unmerged_unreviewed_24h", return_value=5), \
         patch("slack_bot.send_notification", fake_send):
        first = wks.maybe_dm_admin_on_trip()
        second = wks.maybe_dm_admin_on_trip()
    assert first is True
    assert second is False
    fake_send.assert_called_once()


def test_dm_resets_when_count_drops_below_threshold():
    fake_send = MagicMock()
    with patch("watcher_pending_db.list_completed_24h", return_value=[]):
        with patch(
            "watcher_pending_db.count_unmerged_unreviewed_24h", return_value=5
        ), patch("slack_bot.send_notification", fake_send):
            wks.maybe_dm_admin_on_trip()  # DM fires
        # Now count drops below threshold — state resets
        with patch(
            "watcher_pending_db.count_unmerged_unreviewed_24h", return_value=2
        ), patch("slack_bot.send_notification", fake_send):
            wks.maybe_dm_admin_on_trip()  # no DM, state resets
        # Climbs back above threshold — DM fires again
        with patch(
            "watcher_pending_db.count_unmerged_unreviewed_24h", return_value=6
        ), patch("slack_bot.send_notification", fake_send):
            wks.maybe_dm_admin_on_trip()  # DM fires again
    assert fake_send.call_count == 2


def test_dm_force_flag_bypasses_state():
    fake_send = MagicMock()
    with patch("watcher_pending_db.list_completed_24h", return_value=[]), \
         patch("watcher_pending_db.count_unmerged_unreviewed_24h", return_value=5), \
         patch("slack_bot.send_notification", fake_send):
        wks.maybe_dm_admin_on_trip()  # first DM
        wks.maybe_dm_admin_on_trip(force=True)  # forced second DM
    assert fake_send.call_count == 2
