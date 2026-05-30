"""Memory entry TTL and commit-based invalidation for transient_infra entries.

Plan #46 / floating-prancing-trinket PR 4. Memory files in the health store
can become stale when underlying infrastructure changes (e.g. Kapa endpoint
contract switch on PR #167). Time-based TTL is brittle — it expires entries
that are still correct. Commit-stamped invalidation ties an entry's validity
to a specific build commit; when the live ``BUILD_COMMIT`` differs, the
entry is treated as stale and the agent must re-verify by attempting the
underlying tool call.

Frontmatter schema (only YAML-like ``key: value`` lines between ``---``
markers — no nested structures, no lists, no quoted values):

    ---
    kind: transient_infra
    valid_through_commit: <sha>
    last_verified_at: <iso-ts>
    status: operational
    ---

Stamps appended after invalidation:

    superseded_at_commit: <sha>

Importable from both ``orchestrator/`` (in-process) and ``bin/`` scripts
that adjust ``sys.path`` before importing. Pure-Python, no YAML dependency.

The 2026-05-14 demo incident drove this design: a sub-agent cited a
stale "Kapa down with 406" memory note long after PR #167 had migrated
the runtime to ``Accept: application/json``. Re-verification by direct
tool call (NOT filesystem inspection — see CLAUDE.md "MCP diagnostic
hallucination") is the only correct path to refresh transient state.
"""

from __future__ import annotations

import os
from typing import Dict, Optional


_FRONTMATTER_DELIMITER = "---"


# ─────────────────────────────────────────────────────────────────────────────
# Frontmatter parsing — minimal YAML-subset (key: value lines only)
# ─────────────────────────────────────────────────────────────────────────────


def parse_frontmatter(memory_text: str) -> Dict[str, str]:
    """Extract the ``key: value`` lines between the leading ``---`` markers.

    Returns an empty dict if the file does not start with ``---`` or if no
    closing ``---`` is found. Values are returned as plain strings — no
    type coercion, no quoting handling, no nested structures. This is
    deliberate: the frontmatter schema is flat and we want zero deps.

    Lines with no colon are skipped. Whitespace around keys/values is
    trimmed. Empty values are kept as empty strings.
    """
    if not memory_text:
        return {}
    lines = memory_text.splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_DELIMITER:
        return {}
    fields: Dict[str, str] = {}
    for line in lines[1:]:
        stripped = line.strip()
        if stripped == _FRONTMATTER_DELIMITER:
            return fields
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        fields[key.strip()] = value.strip()
    # No closing delimiter found — treat as malformed.
    return {}


def _split_frontmatter(memory_text: str) -> Optional[tuple]:
    """Return ``(frontmatter_text, body_text)`` or ``None`` if no frontmatter.

    ``frontmatter_text`` does NOT include the surrounding ``---`` markers.
    ``body_text`` is everything after the closing ``---`` (including the
    trailing newline, if any).
    """
    if not memory_text:
        return None
    lines = memory_text.splitlines(keepends=True)
    if not lines or lines[0].strip() != _FRONTMATTER_DELIMITER:
        return None
    closing_idx: Optional[int] = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == _FRONTMATTER_DELIMITER:
            closing_idx = idx
            break
    if closing_idx is None:
        return None
    frontmatter = "".join(lines[1:closing_idx])
    body = "".join(lines[closing_idx + 1 :])
    return frontmatter, body


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────


def is_stale(memory_text: str, current_build_commit: Optional[str]) -> bool:
    """Return True if a ``transient_infra`` entry is stale.

    An entry is stale when:
    - ``kind`` is ``transient_infra``, AND
    - either ``superseded_at_commit`` is set (explicit hard invalidation),
      OR ``valid_through_commit`` differs from ``current_build_commit``.

    Non-``transient_infra`` entries are never stale by this function — they
    have their own lifecycle.

    If ``current_build_commit`` is ``None`` or empty, the entry is treated
    as stale (we don't know which build we're on, so we can't trust a
    commit-stamped guarantee). This is the conservative behavior — a
    missing BUILD_COMMIT is the loud-fail signal that deploy plumbing
    isn't wired yet (see CLAUDE.md ``/health`` section).
    """
    fields = parse_frontmatter(memory_text)
    if fields.get("kind") != "transient_infra":
        return False
    if fields.get("superseded_at_commit"):
        return True
    valid_through = fields.get("valid_through_commit")
    if not valid_through:
        # Tagged transient_infra but no commit stamp — schema violation.
        # Treat as stale so an agent re-verifies rather than silently
        # trusts an unbounded entry.
        return True
    if not current_build_commit:
        return True
    return valid_through != current_build_commit


def mark_superseded(memory_text: str, superseded_at_commit: str) -> str:
    """Append ``superseded_at_commit: <sha>`` to the frontmatter.

    For any agent reading the file later, this is a clear "stale" signal
    that survives even when ``BUILD_COMMIT`` is unset. Idempotent — if
    the same value is already stamped, the original text is returned
    unchanged. If a DIFFERENT ``superseded_at_commit`` is already there,
    the value is updated (last-write-wins; the most recent invalidating
    deploy is the one that matters).

    If the file has no frontmatter, this function creates a minimal one
    containing only the supersede stamp — the body becomes whatever the
    original text was. Callers building NEW memory entries should write
    the full frontmatter directly; this function is the
    post-hoc-invalidation path.
    """
    if not superseded_at_commit:
        raise ValueError("superseded_at_commit must be a non-empty sha")

    split = _split_frontmatter(memory_text)
    if split is None:
        # No frontmatter — synthesize one. Preserve the body verbatim.
        body = memory_text if memory_text else ""
        # Insert a blank line between frontmatter and body when body
        # doesn't already start with one.
        sep = "" if body.startswith("\n") or not body else "\n"
        return f"---\nsuperseded_at_commit: {superseded_at_commit}\n---\n{sep}{body}"

    frontmatter_text, body = split
    # Walk the frontmatter lines, replacing or appending the stamp.
    lines = frontmatter_text.splitlines(keepends=True)
    target_key = "superseded_at_commit"
    replaced = False
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{target_key}:"):
            existing_value = stripped.split(":", 1)[1].strip()
            if existing_value == superseded_at_commit:
                # Already stamped with this exact sha — return original.
                return memory_text
            # Update the stamp in place; keep original line ending.
            line_ending = ""
            for end in ("\r\n", "\n"):
                if line.endswith(end):
                    line_ending = end
                    break
            out.append(f"{target_key}: {superseded_at_commit}{line_ending}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        # Ensure the previous block ended with a newline before our append.
        if out and not out[-1].endswith("\n"):
            out[-1] = out[-1] + "\n"
        out.append(f"{target_key}: {superseded_at_commit}\n")

    new_frontmatter = "".join(out)
    return f"---\n{new_frontmatter}---\n{body}"


def current_build_commit() -> Optional[str]:
    """Return the current build commit SHA, or ``None`` if unset.

    Reads ``BUILD_COMMIT`` first (canonical name used by the Dockerfile
    ARG and ``/health``), then ``RAILWAY_GIT_COMMIT_SHA`` as a fallback
    for Railway environments that haven't been migrated to the
    explicit build arg yet. Empty strings count as unset.
    """
    for env_var in ("BUILD_COMMIT", "RAILWAY_GIT_COMMIT_SHA"):
        value = os.environ.get(env_var)
        if value and value.strip():
            return value.strip()
    return None
