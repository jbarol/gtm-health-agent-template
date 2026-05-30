"""Tests for ``orchestrator/version_pin_overrides.py`` (Plan #44 Task #10).

Tests cover the four public helpers and verify:

  - DB outage / unset DATABASE_URL → graceful None / False / {} returns
    (no exception bubbles up).
  - :func:`effective_pin` resolution order: override > file pin > None.
  - Write path validates positive integer.
  - `all_overrides` shape matches what /health expects (Plan #44
    decision row #20).

All DB calls are mocked — no Postgres required.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _reset_db_url(monkeypatch):
    """Default every test to DATABASE_URL=postgres://test. Individual
    tests can blank it out via monkeypatch to exercise the no-DB path.
    """
    monkeypatch.setenv("DATABASE_URL", "postgres://test")
    yield


@pytest.fixture()
def fake_psycopg2(monkeypatch):
    """Inject a fake psycopg2 module that returns a MagicMock connection."""
    fake_module = MagicMock(name="psycopg2")
    conn = MagicMock(name="connection")
    cur = MagicMock(name="cursor")
    conn.cursor.return_value.__enter__.return_value = cur
    fake_module.connect.return_value = conn
    monkeypatch.setitem(__import__("sys").modules, "psycopg2", fake_module)
    return {"module": fake_module, "conn": conn, "cur": cur}


# ─────────────────────────────────────────────────────────────────────────────
# get_override
# ─────────────────────────────────────────────────────────────────────────────


def test_get_override_returns_int_on_hit(fake_psycopg2, monkeypatch):
    import importlib

    import version_pin_overrides

    importlib.reload(version_pin_overrides)
    fake_psycopg2["cur"].fetchone.return_value = (35,)

    out = version_pin_overrides.get_override("coordinator")

    assert out == 35
    fake_psycopg2["cur"].execute.assert_called_once()
    args, _ = fake_psycopg2["cur"].execute.call_args
    assert "session_pin_overrides" in args[0]
    assert args[1] == ("coordinator",)


def test_get_override_returns_none_on_miss(fake_psycopg2):
    import version_pin_overrides

    fake_psycopg2["cur"].fetchone.return_value = None

    assert version_pin_overrides.get_override("coordinator") is None


def test_get_override_returns_none_when_db_url_unset(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    import version_pin_overrides

    assert version_pin_overrides.get_override("coordinator") is None


def test_get_override_swallows_db_errors(monkeypatch):
    import sys

    fake_module = MagicMock(name="psycopg2")
    fake_module.connect.side_effect = RuntimeError("postgres is down")
    monkeypatch.setitem(sys.modules, "psycopg2", fake_module)

    import version_pin_overrides

    # Must not raise.
    assert version_pin_overrides.get_override("coordinator") is None


def test_get_override_empty_agent_name(fake_psycopg2):
    import version_pin_overrides

    assert version_pin_overrides.get_override("") is None
    fake_psycopg2["cur"].execute.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# set_override
# ─────────────────────────────────────────────────────────────────────────────


def test_set_override_upserts_and_returns_true(fake_psycopg2):
    import version_pin_overrides

    ok = version_pin_overrides.set_override("coordinator", 35, "U7")

    assert ok is True
    fake_psycopg2["cur"].execute.assert_called_once()
    args, _ = fake_psycopg2["cur"].execute.call_args
    sql = args[0]
    assert "INSERT INTO session_pin_overrides" in sql
    assert "ON CONFLICT" in sql
    assert args[1] == ("coordinator", 35, "U7")
    fake_psycopg2["conn"].commit.assert_called_once()


def test_set_override_rejects_non_positive_version(fake_psycopg2):
    import version_pin_overrides

    assert version_pin_overrides.set_override("coordinator", 0, "U7") is False
    assert version_pin_overrides.set_override("coordinator", -3, "U7") is False
    fake_psycopg2["cur"].execute.assert_not_called()


def test_set_override_rejects_non_int_version(fake_psycopg2):
    import version_pin_overrides

    assert version_pin_overrides.set_override("coordinator", "35", "U7") is False
    fake_psycopg2["cur"].execute.assert_not_called()


def test_set_override_returns_false_when_db_unset(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    import version_pin_overrides

    assert version_pin_overrides.set_override("coordinator", 35, "U7") is False


def test_set_override_swallows_db_errors(monkeypatch):
    import sys

    fake_module = MagicMock(name="psycopg2")
    fake_module.connect.side_effect = RuntimeError("postgres is down")
    monkeypatch.setitem(sys.modules, "psycopg2", fake_module)

    import version_pin_overrides

    assert version_pin_overrides.set_override("coordinator", 35, "U7") is False


def test_set_override_defaults_actor_when_empty(fake_psycopg2):
    import version_pin_overrides

    version_pin_overrides.set_override("coordinator", 35, "")

    _, _ = fake_psycopg2["cur"].execute.call_args
    args = fake_psycopg2["cur"].execute.call_args.args
    # Third bind param should be the fallback "unknown" string.
    assert args[1][2] == "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# all_overrides
# ─────────────────────────────────────────────────────────────────────────────


def test_all_overrides_shape(fake_psycopg2):
    import version_pin_overrides

    fixed_ts = dt.datetime(2026, 5, 13, 10, 30, 0, tzinfo=dt.timezone.utc)
    fake_psycopg2["cur"].fetchall.return_value = [
        ("coordinator", 35, "U7", fixed_ts),
        ("writing_agent", 3, "U8", fixed_ts),
    ]

    out = version_pin_overrides.all_overrides()

    assert "coordinator" in out and "writing_agent" in out
    assert out["coordinator"] == {
        "version": 35,
        "actor": "U7",
        "ts": fixed_ts.isoformat(),
    }


def test_all_overrides_empty_dict_when_db_unset(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    import version_pin_overrides

    assert version_pin_overrides.all_overrides() == {}


def test_all_overrides_empty_on_db_error(monkeypatch):
    import sys

    fake_module = MagicMock(name="psycopg2")
    fake_module.connect.side_effect = RuntimeError("postgres down")
    monkeypatch.setitem(sys.modules, "psycopg2", fake_module)

    import version_pin_overrides

    assert version_pin_overrides.all_overrides() == {}


# ─────────────────────────────────────────────────────────────────────────────
# effective_pin — the merged read Bundle B's session_runner will call
# ─────────────────────────────────────────────────────────────────────────────


def test_effective_pin_override_beats_file(fake_psycopg2, monkeypatch):
    import version_pin_overrides

    fake_psycopg2["cur"].fetchone.return_value = (99,)

    assert version_pin_overrides.effective_pin("coordinator", 35) == 99


def test_effective_pin_file_used_when_no_override(fake_psycopg2):
    import version_pin_overrides

    fake_psycopg2["cur"].fetchone.return_value = None

    assert version_pin_overrides.effective_pin("coordinator", 35) == 35


def test_effective_pin_returns_none_when_no_override_no_file(fake_psycopg2):
    import version_pin_overrides

    fake_psycopg2["cur"].fetchone.return_value = None

    assert version_pin_overrides.effective_pin("coordinator", None) is None


def test_effective_pin_never_raises(monkeypatch):
    """Even when the DB is completely unavailable, effective_pin returns
    the file pin without raising."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    import version_pin_overrides

    assert version_pin_overrides.effective_pin("coordinator", 35) == 35
