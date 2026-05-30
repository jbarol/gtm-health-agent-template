"""Tests for the surface_state CRUD helpers in db_adapter (Plan #33 F1).

Fully mocked — no real Postgres. Uses an in-memory dict keyed by portco
to satisfy the round-trip contract:

  upsert → get returns the same state_json / rendered_md / canvas_id;
  version starts at 1; second upsert bumps to 2.
  bump_surface_version increments without mutating state_json.
  get_canvas_id returns the stored value.
  All helpers degrade gracefully when DATABASE_URL is empty.

Run:
    cd orchestrator && python3 -m pytest surface_db_test.py -q
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from unittest.mock import patch

import pytest

# Config.py raises if these are missing. setdefault means a real .env
# (when present) takes precedence — mirrors compresr_regression_guard_test.
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
}.items():
    os.environ.setdefault(_key, _value)


# ---------------------------------------------------------------------------
# Fake DB plumbing — in-memory dict keyed by portco
# ---------------------------------------------------------------------------


class _FakeCursor:
    """Cursor that mimics the surface_state schema using an in-memory dict.

    Only recognizes the four statements the helpers issue. Anything else
    becomes a silent no-op (matches the best-effort contract — a future
    column addition must not crash the calling code)."""

    def __init__(self, store: dict):
        self._store = store  # {"rows": {portco: row_dict}}
        self._fetched = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql: str, params=None):
        sql_norm = " ".join(sql.split())
        sql_lower = sql_norm.lower()
        params = params or ()
        rows = self._store.setdefault("rows", {})

        # ── SELECT state_json, rendered_md, canvas_id, version, updated_at
        if (
            "select state_json, rendered_md, canvas_id, version, updated_at"
            in sql_lower
            and "from surface_state" in sql_lower
        ):
            portco = params[0]
            row = rows.get(portco)
            if not row:
                self._fetched = None
                return
            self._fetched = (
                row["state_json"],
                row["rendered_md"],
                row["canvas_id"],
                row["version"],
                row["updated_at"],
            )
            return

        # ── SELECT canvas_id ──
        if (
            "select canvas_id from surface_state" in sql_lower
            and "where portco" in sql_lower
        ):
            portco = params[0]
            row = rows.get(portco)
            self._fetched = (row["canvas_id"],) if row else None
            return

        # ── INSERT … ON CONFLICT DO UPDATE (upsert_surface_state) ──
        if "insert into surface_state" in sql_lower and "on conflict" in sql_lower:
            portco, state_json_str, rendered_md, canvas_id = params
            # state_json_str is the JSON-encoded dict (helper passes
            # json.dumps(state_json)). Decode so callers see a dict.
            try:
                state_json = json.loads(state_json_str)
            except Exception:
                state_json = {}
            existing = rows.get(portco)
            if existing:
                existing.update(
                    {
                        "state_json": state_json,
                        "rendered_md": rendered_md,
                        "canvas_id": canvas_id,
                        "updated_at": _dt.datetime.now(_dt.timezone.utc),
                        "version": existing["version"] + 1,
                    }
                )
            else:
                rows[portco] = {
                    "state_json": state_json,
                    "rendered_md": rendered_md,
                    "canvas_id": canvas_id,
                    "updated_at": _dt.datetime.now(_dt.timezone.utc),
                    "version": 1,
                }
            self._fetched = None
            return

        # ── UPDATE … SET version = version + 1 RETURNING version ──
        if (
            "update surface_state" in sql_lower
            and "version = version + 1" in sql_lower
            and "returning version" in sql_lower
        ):
            portco = params[0]
            row = rows.get(portco)
            if not row:
                self._fetched = None
                return
            row["version"] += 1
            row["updated_at"] = _dt.datetime.now(_dt.timezone.utc)
            self._fetched = (row["version"],)
            return

        # Unknown SQL → no-op.
        self._fetched = None

    def fetchone(self):
        return self._fetched


class _FakeConn:
    def __init__(self, store: dict):
        self._store = store

    def cursor(self, cursor_factory=None):
        return _FakeCursor(self._store)

    def commit(self):
        pass

    def close(self):
        pass


@pytest.fixture
def fake_db():
    store: dict = {"rows": {}}
    with (
        patch("db_adapter.DATABASE_URL", "postgres://test"),
        patch("db_adapter._connect", lambda: _FakeConn(store)),
    ):
        yield store


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_upsert_then_get_returns_same_payload(fake_db):
    """upsert → get must return the same state_json, rendered_md, canvas_id."""
    import db_adapter

    state = {"open_questions": ["q1", "q2"], "cost_block": {"trailing_7d": 12.3}}
    md = "## Operating state\n- q1\n- q2\n"
    canvas = "F0TESTCANVAS"

    ok = db_adapter.upsert_surface_state(
        "acme", state_json=state, rendered_md=md, canvas_id=canvas
    )
    assert ok is True

    out = db_adapter.get_surface_state("acme")
    assert out is not None
    assert out["state_json"] == state
    assert out["rendered_md"] == md
    assert out["canvas_id"] == canvas
    assert out["version"] == 1
    assert out["updated_at"] is not None


def test_second_upsert_bumps_version(fake_db):
    """The second upsert must yield version = 2 (ON CONFLICT path)."""
    import db_adapter

    db_adapter.upsert_surface_state(
        "acme", state_json={"a": 1}, rendered_md="v1", canvas_id="F1"
    )
    out1 = db_adapter.get_surface_state("acme")
    assert out1["version"] == 1

    db_adapter.upsert_surface_state(
        "acme", state_json={"a": 2}, rendered_md="v2", canvas_id="F1"
    )
    out2 = db_adapter.get_surface_state("acme")
    assert out2["version"] == 2
    assert out2["state_json"] == {"a": 2}
    assert out2["rendered_md"] == "v2"


def test_upsert_isolates_portcos(fake_db):
    """Distinct portcos write to distinct rows — no cross-contamination."""
    import db_adapter

    db_adapter.upsert_surface_state(
        "acme", state_json={"who": "fb"}, rendered_md="fb-md", canvas_id="F1"
    )
    db_adapter.upsert_surface_state(
        "acmeco", state_json={"who": "ac"}, rendered_md="ac-md", canvas_id="A1"
    )

    fb = db_adapter.get_surface_state("acme")
    ac = db_adapter.get_surface_state("acmeco")
    assert fb["state_json"] == {"who": "fb"}
    assert ac["state_json"] == {"who": "ac"}
    assert fb["canvas_id"] == "F1"
    assert ac["canvas_id"] == "A1"


# ---------------------------------------------------------------------------
# bump_surface_version
# ---------------------------------------------------------------------------


def test_bump_surface_version_increments(fake_db):
    """bump_surface_version returns the post-bump version and persists it."""
    import db_adapter

    db_adapter.upsert_surface_state(
        "acme", state_json={"a": 1}, rendered_md="v1", canvas_id="F1"
    )
    assert db_adapter.get_surface_state("acme")["version"] == 1

    new_v = db_adapter.bump_surface_version("acme")
    assert new_v == 2
    assert db_adapter.get_surface_state("acme")["version"] == 2

    new_v2 = db_adapter.bump_surface_version("acme")
    assert new_v2 == 3


def test_bump_surface_version_missing_row_returns_none(fake_db):
    """Bumping a portco that's never been upserted returns None — the
    UPDATE returns zero rows."""
    import db_adapter

    assert db_adapter.bump_surface_version("never-existed") is None


# ---------------------------------------------------------------------------
# get_canvas_id
# ---------------------------------------------------------------------------


def test_get_canvas_id_returns_stored_value(fake_db):
    """get_canvas_id returns the canvas_id stored by upsert."""
    import db_adapter

    db_adapter.upsert_surface_state(
        "acme", state_json={}, rendered_md="", canvas_id="F0CANVAS123"
    )
    assert db_adapter.get_canvas_id("acme") == "F0CANVAS123"


def test_get_canvas_id_missing_row_returns_none(fake_db):
    """No row → None."""
    import db_adapter

    assert db_adapter.get_canvas_id("nobody") is None


def test_get_canvas_id_null_value_returns_none(fake_db):
    """When canvas_id was upserted as None (Canvas not yet created)
    get_canvas_id should still return None, not an empty string."""
    import db_adapter

    db_adapter.upsert_surface_state(
        "acme", state_json={}, rendered_md="", canvas_id=None
    )
    assert db_adapter.get_canvas_id("acme") is None


# ---------------------------------------------------------------------------
# Graceful degradation without DATABASE_URL
# ---------------------------------------------------------------------------


def test_get_surface_state_returns_none_without_db(monkeypatch):
    import db_adapter

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "")
    assert db_adapter.get_surface_state("acme") is None


def test_upsert_surface_state_returns_false_without_db(monkeypatch):
    import db_adapter

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "")
    assert (
        db_adapter.upsert_surface_state(
            "acme", state_json={}, rendered_md="", canvas_id=None
        )
        is False
    )


def test_bump_surface_version_returns_none_without_db(monkeypatch):
    import db_adapter

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "")
    assert db_adapter.bump_surface_version("acme") is None


def test_get_canvas_id_returns_none_without_db(monkeypatch):
    import db_adapter

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "")
    assert db_adapter.get_canvas_id("acme") is None


# ---------------------------------------------------------------------------
# Empty-portco guard
# ---------------------------------------------------------------------------


def test_helpers_reject_empty_portco(fake_db):
    """All helpers must short-circuit on a falsy portco rather than
    issuing a SQL statement that would match the wrong row."""
    import db_adapter

    assert db_adapter.get_surface_state("") is None
    assert (
        db_adapter.upsert_surface_state(
            "", state_json={}, rendered_md="", canvas_id=None
        )
        is False
    )
    assert db_adapter.bump_surface_version("") is None
    assert db_adapter.get_canvas_id("") is None


# ---------------------------------------------------------------------------
# DB error fail-soft
# ---------------------------------------------------------------------------


def test_helpers_swallow_connect_errors(monkeypatch):
    """A raising _connect() must not propagate out of any helper."""
    import db_adapter

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "postgres://test")

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(db_adapter, "_connect", _boom)

    assert db_adapter.get_surface_state("acme") is None
    assert (
        db_adapter.upsert_surface_state(
            "acme", state_json={}, rendered_md="", canvas_id=None
        )
        is False
    )
    assert db_adapter.bump_surface_version("acme") is None
    assert db_adapter.get_canvas_id("acme") is None
