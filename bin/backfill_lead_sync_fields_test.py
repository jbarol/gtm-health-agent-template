"""Tests for ``bin/backfill_lead_sync_fields.py``.

Covers the Codex review PR #96, P2 fix: ``main()`` must return a nonzero
exit code when any portco fails. Pre-fix the loop swallowed exceptions
and always returned 0, which left historical Lead columns unfilled with
no machine-detectable error state.

Fully mocked — no real SF, no real Postgres.

Run:
    cd bin && python3 -m pytest backfill_lead_sync_fields_test.py -q
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Config.py raises if these are missing. setdefault means a real .env
# (when present) wins — mirrors orchestrator/db_sync_test bootstrap.
for _key, _value in {
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
    "DATABASE_URL": "postgres://test/db",
}.items():
    os.environ.setdefault(_key, _value)

# slack_bot module would call into Slack at import time without this stub.
# orchestrator/conftest.py does the same dance for orchestrator/ tests; we
# replicate it here because bin/ has no conftest.
if "slack_bolt" not in sys.modules:
    from unittest.mock import MagicMock

    _fake_app = MagicMock(name="slack_bolt.App")
    _fake_app.client = MagicMock()
    _slack_bolt = MagicMock(name="slack_bolt")
    _slack_bolt.App = MagicMock(return_value=_fake_app)
    sys.modules["slack_bolt"] = _slack_bolt
    _socket_mode = MagicMock(name="slack_bolt.adapter.socket_mode")
    _socket_mode.SocketModeHandler = MagicMock()
    sys.modules["slack_bolt.adapter"] = MagicMock(socket_mode=_socket_mode)
    sys.modules["slack_bolt.adapter.socket_mode"] = _socket_mode

# Make orchestrator/ importable — backfill_lead_sync_fields.py does the
# same insert at import time, so we mirror it for the test.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "orchestrator"))
sys.path.insert(0, str(_REPO_ROOT / "bin"))


@pytest.fixture
def backfill_module():
    """Import the backfill script as a module, isolated per test."""
    if "backfill_lead_sync_fields" in sys.modules:
        del sys.modules["backfill_lead_sync_fields"]
    mod = importlib.import_module("backfill_lead_sync_fields")
    yield mod


def _fake_portco(key: str) -> dict:
    return {"key": key}


# ---------------------------------------------------------------------------
# main() exit code semantics
# ---------------------------------------------------------------------------


def test_main_returns_zero_when_all_portcos_succeed(backfill_module, capsys):
    """Happy path — every portco returns from _backfill_portco cleanly."""
    portcos = [_fake_portco("acme"), _fake_portco("delta")]
    success_calls: list[str] = []

    def fake_backfill(key: str):
        success_calls.append(key)
        return (3209, 0) if key == "acme" else (1500, 2)

    with (
        patch.object(backfill_module, "get_all_portcos", return_value=portcos),
        patch.object(backfill_module, "_backfill_portco", side_effect=fake_backfill),
        patch.object(sys, "argv", ["backfill_lead_sync_fields.py"]),
    ):
        rc = backfill_module.main()

    assert rc == 0
    assert success_calls == ["acme", "delta"]


def test_main_returns_nonzero_when_any_portco_fails(backfill_module):
    """Codex PR #96 P2: a single portco failure must surface as a nonzero
    exit code so operators / cron / CI can detect partial failures."""
    portcos = [_fake_portco("acme"), _fake_portco("delta")]

    def fake_backfill(key: str):
        if key == "acme":
            raise RuntimeError("SOQL error: No such column")
        return (3209, 0)

    with (
        patch.object(backfill_module, "get_all_portcos", return_value=portcos),
        patch.object(backfill_module, "_backfill_portco", side_effect=fake_backfill),
        patch.object(sys, "argv", ["backfill_lead_sync_fields.py"]),
    ):
        rc = backfill_module.main()

    assert rc != 0, (
        "main() must return nonzero when any portco fails — "
        "swallowing the exception and returning 0 hides partial failures."
    )


def test_main_single_target_failure_returns_nonzero(backfill_module):
    """--portco <key> runs are the highest-risk case for the bug: if the
    one requested portco fails and main() returns 0, the operator has no
    machine-detectable signal that the backfill did not happen."""

    def fake_backfill(key: str):
        raise RuntimeError("SF auth expired")

    with (
        patch.object(backfill_module, "_backfill_portco", side_effect=fake_backfill),
        patch.object(
            sys,
            "argv",
            ["backfill_lead_sync_fields.py", "--portco", "acme"],
        ),
    ):
        rc = backfill_module.main()

    assert rc != 0


def test_main_logs_per_portco_status_summary(backfill_module, caplog):
    """Operators expect a per-portco SUCCESS/FAILED line in the summary so
    they can see which portco needs attention, not just a total count."""
    portcos = [_fake_portco("acme"), _fake_portco("delta")]

    def fake_backfill(key: str):
        if key == "delta":
            raise RuntimeError("SOQL error: No such column")
        return (3209, 0)

    with (
        patch.object(backfill_module, "get_all_portcos", return_value=portcos),
        patch.object(backfill_module, "_backfill_portco", side_effect=fake_backfill),
        patch.object(sys, "argv", ["backfill_lead_sync_fields.py"]),
        caplog.at_level("INFO", logger="backfill_lead_sync_fields"),
    ):
        backfill_module.main()

    text = caplog.text
    assert "acme" in text and "SUCCESS" in text
    assert "delta" in text and "FAILED" in text
    assert "2 portcos processed, 1 failed" in text


def test_main_no_database_url_returns_two(backfill_module, monkeypatch):
    """Pre-flight DATABASE_URL check still returns 2 (unchanged behavior)."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with patch.object(sys, "argv", ["backfill_lead_sync_fields.py"]):
        rc = backfill_module.main()
    assert rc == 2
