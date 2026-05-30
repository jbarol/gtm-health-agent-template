"""Canonical resolver for session-artifact paths.

Theme B (2026-05-16) decoupled the orchestrator's on-disk artifact store
from the agent-facing path string. Pre-fix, both the orchestrator and the
agent assumed ``/mnt/session/outputs/<file>`` was a shared mount; in
practice the agent's sandbox had a ~18-minute TTL on that mount while the
orchestrator's filesystem did not, so files written by one sub-agent were
gone before the Coordinator (or a later sub-agent) tried to read them.

Post-fix, ``SESSION_OUTPUT_DIR`` (Railway Volume in prod, e.g.
``/data/session_outputs``) is the canonical store. ``virtualize_result``,
``materialize_xlsx``, and the orchestrator-side ``query_artifact`` all
write/read against it. The agent-facing string in tool result envelopes
may still appear as ``/mnt/session/outputs/<file>`` (older agents and
prompts assume that prefix); ``resolve_artifact_path`` translates by
basename so both spellings refer to the same on-disk file.

Live incident this addresses: 2026-05-16 sesn_EXAMPLE —
Statistician sub-agent hit ``artifact file not found`` repeatedly on
``quarterly_taxonomy_v2_20260516.parquet`` despite the Coordinator just
having materialized it.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

# The legacy mount path agents and prompts assume. We translate any
# incoming path under this prefix to the canonical SESSION_OUTPUT_DIR.
_LEGACY_AGENT_PREFIX = "/mnt/session/outputs"


def session_output_dir() -> str:
    """Canonical directory for session artifacts (Parquets, xlsx).

    Resolves from ``SESSION_OUTPUT_DIR`` env at call time; falls back to
    ``/mnt/session/outputs`` for back-compat when the env is unset (which
    matches the pre-Theme-B behavior). Always returns a real (canonical)
    path so symlink confusion is impossible.
    """
    raw = os.environ.get("SESSION_OUTPUT_DIR") or _LEGACY_AGENT_PREFIX
    return os.path.realpath(raw)


def resolve_artifact_path(path: str) -> str:
    """Translate an agent-facing artifact path to the orchestrator's canonical
    on-disk location.

    Rules:
      - Input already under ``session_output_dir()`` → return realpath unchanged.
      - Input under the legacy ``/mnt/session/outputs/`` prefix but
        ``SESSION_OUTPUT_DIR`` points elsewhere → rebase by basename.
      - Anything else (absolute path outside both prefixes, traversal
        attempts, non-string) → return realpath of original, let the
        downstream safety check reject.

    Callers must run ``_is_safe_artifact_path`` on the result before opening
    the file — this helper does path translation, not safety enforcement.
    """
    if not path or not isinstance(path, str):
        return path or ""

    canonical_root = session_output_dir()
    abs_input = os.path.abspath(path)

    # Fast path: already under the canonical root.
    try:
        if os.path.commonpath([os.path.realpath(abs_input), canonical_root]) == canonical_root:
            return os.path.realpath(abs_input)
    except ValueError:
        # commonpath raises on different drives (Windows); irrelevant on
        # Railway/Linux but defensive.
        pass

    # Legacy-prefix translation. Only rebase if the canonical root has
    # actually been redirected — otherwise the prefix IS the canonical root
    # and the path is fine as-is.
    if canonical_root != os.path.realpath(_LEGACY_AGENT_PREFIX):
        legacy_root_real = os.path.realpath(_LEGACY_AGENT_PREFIX)
        try:
            if os.path.commonpath([abs_input, legacy_root_real]) == legacy_root_real:
                basename = os.path.basename(abs_input)
                rebased = os.path.join(canonical_root, basename)
                log.debug(
                    "artifact_paths: rebased %s → %s (legacy prefix → canonical)",
                    abs_input,
                    rebased,
                )
                return rebased
        except ValueError:
            pass

    return os.path.realpath(abs_input)


def is_under_session_output_dir(path: str) -> bool:
    """True iff ``path`` (after canonical realpath) lives under the configured
    session output directory. Use this AFTER ``resolve_artifact_path`` to
    enforce the safety boundary that prevents traversal outside the store.
    """
    if not path or not isinstance(path, str):
        return False
    safe_root = session_output_dir()
    canonical = os.path.realpath(path)
    try:
        return os.path.commonpath([canonical, safe_root]) == safe_root
    except ValueError:
        return False


def sweep_session_artifacts(max_age_days: int = 14) -> dict:
    """Delete .parquet and .xlsx artifacts older than ``max_age_days``.

    Returns a stats dict ``{scanned, deleted, freed_bytes, error_count}``.
    Best-effort: per-file failures (permission, race) are logged and counted
    but do not stop the sweep. Designed to be called from the APScheduler
    daily cron in ``main.py``.

    Aggressive cleanup is unnecessary — artifacts are typically read within
    the same session (minutes). The 14-day default is generous enough that
    a Slack thread re-opened a week later still resolves cleanly, and short
    enough that a Railway Volume on a small SSD doesn't fill up over the
    long tail.
    """
    root = session_output_dir()
    cutoff = time.time() - (max_age_days * 86400)
    stats = {"scanned": 0, "deleted": 0, "freed_bytes": 0, "error_count": 0}
    try:
        root_path = Path(root)
        if not root_path.is_dir():
            log.info("artifact_paths: sweep skipped — %s not a directory", root)
            return stats
        for entry in root_path.iterdir():
            if not entry.is_file():
                continue
            if entry.suffix not in (".parquet", ".xlsx", ".csv"):
                continue
            stats["scanned"] += 1
            try:
                mtime = entry.stat().st_mtime
            except OSError as exc:
                log.warning("artifact_paths: stat failed on %s: %s", entry, exc)
                stats["error_count"] += 1
                continue
            if mtime >= cutoff:
                continue
            try:
                size = entry.stat().st_size
                entry.unlink()
                stats["deleted"] += 1
                stats["freed_bytes"] += size
            except OSError as exc:
                log.warning("artifact_paths: unlink failed on %s: %s", entry, exc)
                stats["error_count"] += 1
    except Exception:
        log.exception("artifact_paths: sweep failed unexpectedly")
    log.info(
        "artifact_paths: sweep done scanned=%d deleted=%d freed_mb=%.2f errors=%d",
        stats["scanned"],
        stats["deleted"],
        stats["freed_bytes"] / 1_048_576.0,
        stats["error_count"],
    )
    return stats
