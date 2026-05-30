"""Postgres-backed pin override table for Plan #44 Task #10.

Decision row #20 from Plan #44 mandates this be Postgres-backed (NOT a
local file) so a pin set via Slack at 2am survives the next Railway
redeploy. The override layer sits **above** the file pin
(``agents/active_versions.json``) and **below** the SDK default of "latest":

    effective_pin = DB override (this module) > file pin > None (=latest)

Session create reads :func:`effective_pin` with the file value already
loaded; this module owns only the DB override slot. Bundle B's
``session_runner.py`` is expected to import :func:`effective_pin` and
pass the file pin as the second argument — we do not modify that file
here to keep merge isolation.

All read/write helpers degrade gracefully:

- ``DATABASE_URL`` unset → :func:`get_override` returns ``None`` and
  :func:`set_override` returns ``False`` (no exception).
- Postgres outage / missing table → same fall-through. The table is
  created by ``orchestrator/migrations/00AF_session_pin_overrides.sql``
  on apply.

Errors are logged at DEBUG to avoid noise; the slash command surface
in ``orchestrator/slack_bot.py`` is responsible for telling the user the
write failed.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

log = logging.getLogger(__name__)


def _database_url() -> str:
    """Read DATABASE_URL at call time so tests can monkey-patch ``os.environ``.

    Reading at import time would freeze the value before pytest's
    ``monkeypatch`` fixture has a chance to override it.
    """
    return os.environ.get("DATABASE_URL", "") or ""


def _connect():
    """Open a fresh psycopg2 connection. Lazy import keeps test isolation
    cheap — pytest can patch psycopg2 globally without dragging it in here.
    """
    import psycopg2  # local — see docstring

    return psycopg2.connect(_database_url())


def get_override(agent_name: str) -> Optional[int]:
    """Return the integer version pinned to ``agent_name`` in the
    ``session_pin_overrides`` table, or ``None`` when no override is set
    OR the DB is unavailable.

    The caller is responsible for falling back to the file pin (and then
    to the SDK default of "latest"). See :func:`effective_pin` for the
    merged read.
    """
    if not _database_url() or not agent_name:
        return None
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT version FROM session_pin_overrides WHERE agent_name = %s",
                    (agent_name,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                # Defensive cast — Postgres INT comes back as int, but
                # callers may have set the column via a non-numeric path.
                return int(row[0])
        finally:
            conn.close()
    except Exception as e:
        log.debug(f"get_override({agent_name}) failed: {e}")
        return None


def set_override(agent_name: str, version: int, actor: str) -> bool:
    """Upsert an override row. Returns True on success, False on every
    failure mode (no DATABASE_URL, missing table, write error).

    The slash command handler in ``orchestrator/slack_bot.py`` reads the
    return value to decide whether to tell the operator "pinned" or
    "couldn't save."
    """
    if not _database_url() or not agent_name:
        return False
    if not isinstance(version, int) or version <= 0:
        # Defense in depth — the slash command already validates this.
        log.debug(f"set_override({agent_name}): invalid version {version!r}")
        return False
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO session_pin_overrides "
                    "(agent_name, version, actor, ts) "
                    "VALUES (%s, %s, %s, NOW()) "
                    "ON CONFLICT (agent_name) DO UPDATE SET "
                    "version = EXCLUDED.version, "
                    "actor = EXCLUDED.actor, "
                    "ts = NOW()",
                    (agent_name, version, actor or "unknown"),
                )
                conn.commit()
                return True
        finally:
            conn.close()
    except Exception as e:
        log.debug(f"set_override({agent_name}, {version}, {actor}) failed: {e}")
        return False


def all_overrides() -> dict[str, dict]:
    """Return a ``{agent_name: {version, actor, ts}}`` dict of every
    override currently set.

    Used by the ``/health`` endpoint (Plan #44 decision row #20: "/health
    surfaces effective pin AND source AND actor + timestamp"). Returns an
    empty dict on any failure — the health endpoint must never crash on a
    DB hiccup.

    ``ts`` is returned as an ISO-8601 string so the health JSON payload
    serializes cleanly without a custom encoder.
    """
    if not _database_url():
        return {}
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT agent_name, version, actor, ts FROM session_pin_overrides"
                )
                rows = cur.fetchall()
                out: dict[str, dict] = {}
                for r in rows:
                    name, version, actor, ts = r
                    out[name] = {
                        "version": int(version),
                        "actor": actor or "",
                        "ts": ts.isoformat() if ts is not None else "",
                    }
                return out
        finally:
            conn.close()
    except Exception as e:
        log.debug(f"all_overrides() failed: {e}")
        return {}


def effective_pin(agent_name: str, file_pin: Optional[int]) -> Optional[int]:
    """Return the effective version pin to apply at session create.

    Resolution order (highest precedence first):
      1. DB override (this module's table)
      2. ``file_pin`` argument — Bundle B's session_runner reads
         ``agents/active_versions.json`` and passes the value in.
      3. ``None`` — caller treats this as "let the SDK pick the latest
         active version."

    Bundle B is expected to call this single helper at session create so
    a Slack `/pin` write hot-applies on the very next session.create
    without an orchestrator restart. We expose it as a standalone
    function (not a method) so the import surface from
    ``session_runner.py`` is one line:

        from version_pin_overrides import effective_pin

    The function never raises — every failure mode in :func:`get_override`
    returns ``None``, which collapses to the file pin via the ``or`` chain.
    """
    override = get_override(agent_name)
    if override is not None:
        return override
    return file_pin
