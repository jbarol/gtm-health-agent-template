"""Postgres-backed feature-flag override table for Plan #44 Task #24.

Decision row #25 from Plan #44 mandates this be Postgres-backed (NOT a
process-local override) so an operator-set flag survives the next Railway
redeploy — the redeploy itself is often what surfaces the bug that
triggered the flag flip.

Resolution order (per :func:`get_flag`):
  1. DB override (this module's table)
  2. ``os.environ.get(name, env_default)`` — the regular env-var path
  3. ``env_default`` argument — the in-code default if both miss.

Bundle B's ``orchestrator/config.py`` is expected to call
:func:`get_flag` at read-time (not import-time) so a Slack `/flag` write
hot-applies on the very next config lookup without an orchestrator
restart. We expose helpers as module-level functions — same import shape
as ``version_pin_overrides`` — so Bundle B can wire this in with a single
import line.

All helpers degrade gracefully:

- ``DATABASE_URL`` unset → :func:`get_flag` falls through to env;
  :func:`set_flag` returns ``False``.
- Postgres outage / missing table → same fall-through. Errors are
  logged at DEBUG.

The whitelist of allowed flag names lives in
``orchestrator/slack_bot.py`` (the slash command layer). The DB table
itself accepts any (name, value) pair so a future flag doesn't need a
migration — only the slash-command whitelist needs to grow.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

log = logging.getLogger(__name__)


def _database_url() -> str:
    """Read DATABASE_URL at call time so tests can monkey-patch
    ``os.environ`` after import."""
    return os.environ.get("DATABASE_URL", "") or ""


def _connect():
    """Open a fresh psycopg2 connection. Lazy import for test isolation."""
    import psycopg2  # local — see version_pin_overrides

    return psycopg2.connect(_database_url())


def _read_override(name: str) -> Optional[str]:
    """Return the stored value for ``name`` or ``None``. Pure helper; the
    user-facing entry point is :func:`get_flag`."""
    if not _database_url() or not name:
        return None
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT flag_value FROM flag_overrides WHERE flag_name = %s",
                    (name,),
                )
                row = cur.fetchone()
                return row[0] if row else None
        finally:
            conn.close()
    except Exception as e:
        log.debug(f"_read_override({name}) failed: {e}")
        return None


def get_flag(name: str, env_default: str) -> str:
    """Return the effective value for ``name``.

    Resolution order:
      1. DB override row (if present)
      2. ``os.environ.get(name, env_default)``

    The DB layer is checked FIRST — by design an operator-set override
    in Slack should win against the deployed env value. The env value is
    what the operator was trying to escape from.

    ``env_default`` is the in-code fallback if neither DB nor env has a
    value. Callers should pass the same default they'd use with
    ``os.environ.get`` so removing this layer is a one-line diff.
    """
    override = _read_override(name)
    if override is not None:
        return override
    return os.environ.get(name, env_default)


def set_flag(name: str, value: str, actor: str) -> bool:
    """Upsert a flag override. Returns True on success, False on every
    failure mode (no DATABASE_URL, missing table, write error).

    The slash command handler reads the return value to decide whether to
    confirm or warn.
    """
    if not _database_url() or not name:
        return False
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO flag_overrides "
                    "(flag_name, flag_value, actor, ts) "
                    "VALUES (%s, %s, %s, NOW()) "
                    "ON CONFLICT (flag_name) DO UPDATE SET "
                    "flag_value = EXCLUDED.flag_value, "
                    "actor = EXCLUDED.actor, "
                    "ts = NOW()",
                    (name, value, actor or "unknown"),
                )
                conn.commit()
                return True
        finally:
            conn.close()
    except Exception as e:
        log.debug(f"set_flag({name}, {value}, {actor}) failed: {e}")
        return False


def all_flags() -> dict[str, dict]:
    """Return ``{flag_name: {value, actor, ts}}`` for every override row.

    Same shape as :func:`version_pin_overrides.all_overrides` so the
    ``/health`` endpoint can render both with a single pattern. Empty
    dict on any failure.
    """
    if not _database_url():
        return {}
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT flag_name, flag_value, actor, ts FROM flag_overrides"
                )
                rows = cur.fetchall()
                out: dict[str, dict] = {}
                for r in rows:
                    name, value, actor, ts = r
                    out[name] = {
                        "value": value or "",
                        "actor": actor or "",
                        "ts": ts.isoformat() if ts is not None else "",
                    }
                return out
        finally:
            conn.close()
    except Exception as e:
        log.debug(f"all_flags() failed: {e}")
        return {}
