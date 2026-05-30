"""Tests for ``orchestrator/flag_overrides.py`` (Plan #44 Task #24).

Covers:
  - DB resolution wins over env (the whole point of decision row #25)
  - env fallback when DB has no row
  - env_default fallback when neither DB nor env have a value
  - graceful no-DATABASE_URL path
  - set_flag upsert
  - all_flags shape for /health
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _reset_db_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://test")
    yield


@pytest.fixture()
def fake_psycopg2(monkeypatch):
    fake_module = MagicMock(name="psycopg2")
    conn = MagicMock(name="connection")
    cur = MagicMock(name="cursor")
    conn.cursor.return_value.__enter__.return_value = cur
    fake_module.connect.return_value = conn
    monkeypatch.setitem(__import__("sys").modules, "psycopg2", fake_module)
    return {"module": fake_module, "conn": conn, "cur": cur}


# ─────────────────────────────────────────────────────────────────────────────
# get_flag
# ─────────────────────────────────────────────────────────────────────────────


def test_get_flag_db_wins_over_env(fake_psycopg2, monkeypatch):
    """DB override beats whatever the env var says."""
    import flag_overrides

    fake_psycopg2["cur"].fetchone.return_value = ("false",)
    monkeypatch.setenv("COMPRESSION_ENABLED", "true")

    assert flag_overrides.get_flag("COMPRESSION_ENABLED", "true") == "false"


def test_get_flag_falls_back_to_env(fake_psycopg2, monkeypatch):
    """When the DB has no row, get_flag falls through to env."""
    import flag_overrides

    fake_psycopg2["cur"].fetchone.return_value = None
    monkeypatch.setenv("COMPRESSION_ENABLED", "true")

    assert flag_overrides.get_flag("COMPRESSION_ENABLED", "false") == "true"


def test_get_flag_falls_back_to_env_default(fake_psycopg2, monkeypatch):
    """Both DB and env miss → env_default is returned."""
    import flag_overrides

    fake_psycopg2["cur"].fetchone.return_value = None
    monkeypatch.delenv("COMPRESSION_ENABLED", raising=False)

    assert flag_overrides.get_flag("COMPRESSION_ENABLED", "true") == "true"


def test_get_flag_db_unset_uses_env(monkeypatch):
    """No DATABASE_URL → straight env read."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("COMPRESSION_ENABLED", "true")

    import flag_overrides

    assert flag_overrides.get_flag("COMPRESSION_ENABLED", "false") == "true"


def test_get_flag_db_error_falls_back_to_env(monkeypatch):
    """A psycopg2 connect failure must not raise — fall through to env."""
    import sys

    fake_module = MagicMock(name="psycopg2")
    fake_module.connect.side_effect = RuntimeError("db ate it")
    monkeypatch.setitem(sys.modules, "psycopg2", fake_module)
    monkeypatch.setenv("COMPRESSION_ENABLED", "true")

    import flag_overrides

    assert flag_overrides.get_flag("COMPRESSION_ENABLED", "false") == "true"


# ─────────────────────────────────────────────────────────────────────────────
# set_flag
# ─────────────────────────────────────────────────────────────────────────────


def test_set_flag_upserts_and_returns_true(fake_psycopg2):
    import flag_overrides

    ok = flag_overrides.set_flag("COMPRESSION_ENABLED", "false", "U7")

    assert ok is True
    fake_psycopg2["cur"].execute.assert_called_once()
    args = fake_psycopg2["cur"].execute.call_args.args
    assert "INSERT INTO flag_overrides" in args[0]
    assert "ON CONFLICT" in args[0]
    assert args[1] == ("COMPRESSION_ENABLED", "false", "U7")


def test_set_flag_returns_false_when_db_unset(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    import flag_overrides

    assert flag_overrides.set_flag("COMPRESSION_ENABLED", "false", "U7") is False


def test_set_flag_swallows_db_errors(monkeypatch):
    import sys

    fake_module = MagicMock(name="psycopg2")
    fake_module.connect.side_effect = RuntimeError("db down")
    monkeypatch.setitem(sys.modules, "psycopg2", fake_module)

    import flag_overrides

    assert flag_overrides.set_flag("COMPRESSION_ENABLED", "false", "U7") is False


# ─────────────────────────────────────────────────────────────────────────────
# all_flags
# ─────────────────────────────────────────────────────────────────────────────


def test_all_flags_shape(fake_psycopg2):
    import flag_overrides

    fixed_ts = dt.datetime(2026, 5, 13, 9, 0, 0, tzinfo=dt.timezone.utc)
    fake_psycopg2["cur"].fetchall.return_value = [
        ("COMPRESSION_ENABLED", "false", "U7", fixed_ts),
        ("SMOKE_PROBE_LEVEL", "full", "U7", fixed_ts),
    ]

    out = flag_overrides.all_flags()

    assert out["COMPRESSION_ENABLED"] == {
        "value": "false",
        "actor": "U7",
        "ts": fixed_ts.isoformat(),
    }
    assert out["SMOKE_PROBE_LEVEL"]["value"] == "full"


def test_all_flags_empty_when_db_unset(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    import flag_overrides

    assert flag_overrides.all_flags() == {}
