#!/usr/bin/env python3
"""Stamp ``superseded_at_commit`` on transient_infra memory entries.

Plan #46 / floating-prancing-trinket PR 4. Wired into the
``deploy-prompts.yml`` workflow as a post-deploy step: if the merged
commit touches a file whose underlying contract is encoded in a
``transient_infra`` memory entry (e.g. ``orchestrator/kapa_rest_tool.py``),
invalidate every related entry so the next session re-verifies rather
than trusting stale frontmatter.

The CI step passes the list of files touched by the merge via
``--touched-files``. This script:

1. Resolves the current commit SHA from ``GITHUB_SHA`` (set by Actions)
   or ``$(git rev-parse HEAD)``.
2. Reads every ``*.md`` file under ``memory_seed/system/`` (the canonical
   location for portco-agnostic ``/system/...`` seeds).
3. For each file that:
   - has frontmatter with ``kind: transient_infra``, AND
   - mentions any of the configured "topics" in its body that map to a
     touched file (e.g. ``kapa_rest_tool.py`` → "kapa" topic),
   appends ``superseded_at_commit: <sha>`` via
   ``orchestrator.memory_hygiene.mark_superseded``.

Usage:
    python bin/stamp_superseded_memory.py \\
        --touched-files orchestrator/kapa_rest_tool.py \\
        --touched-files agents/setup_agents.py

    # Or as a single comma-separated list:
    python bin/stamp_superseded_memory.py \\
        --touched-files orchestrator/kapa_rest_tool.py,bin/probe_kapa.py

    # Dry-run (default behavior is to write):
    python bin/stamp_superseded_memory.py --touched-files ... --dry-run

Topic map: each topic key maps to (path_substrings_that_trigger,
body_substrings_to_match). A memory entry is stamped if ANY touched
file path matches ANY path_substring AND the entry body contains ANY
body_substring (case-insensitive on the body side).

Read-only safe: if ``memory_seed/system/`` does not exist (e.g. the
PR 2 file hasn't merged yet), the script is a clean no-op.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
ORCHESTRATOR_DIR = REPO_ROOT / "orchestrator"
MEMORY_SEED_DIR = REPO_ROOT / "memory_seed" / "system"

# Make ``orchestrator/`` importable so we can use memory_hygiene directly
# rather than re-implementing frontmatter handling here.
if str(ORCHESTRATOR_DIR) not in sys.path:
    sys.path.insert(0, str(ORCHESTRATOR_DIR))

import memory_hygiene  # noqa: E402 — sys.path adjusted above


# Topic registry — maps a deployable-contract topic to:
# - path_substrings: any touched file containing one of these substrings
#   triggers this topic.
# - body_substrings: a memory entry whose body contains one of these
#   substrings (case-insensitive) belongs to this topic.
#
# Extend this dict when a NEW deployable contract gets a transient_infra
# memory entry. Today the only one is Kapa (PR 2 / PR 4).
TOPICS: dict[str, dict[str, tuple[str, ...]]] = {
    "kapa": {
        "path_substrings": (
            "orchestrator/kapa_rest_tool.py",
            "orchestrator/kapa_rest_tool_test.py",
            "bin/probe_kapa.py",
            "bin/verify_kapa_key.sh",
        ),
        "body_substrings": (
            "kapa",
            "acme_knowledge",
            "search_knowledge_base",
        ),
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# CLI helpers
# ─────────────────────────────────────────────────────────────────────────────


def _parse_args(argv: List[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stamp superseded_at_commit on transient_infra memory entries.",
    )
    parser.add_argument(
        "--touched-files",
        action="append",
        default=[],
        help=(
            "File path(s) touched by the merged commit. May be repeated or "
            "comma-separated."
        ),
    )
    parser.add_argument(
        "--commit",
        default=None,
        help=(
            "Override the supersede SHA. Defaults to ``$GITHUB_SHA`` or "
            "``git rev-parse HEAD`` in the working tree."
        ),
    )
    parser.add_argument(
        "--memory-dir",
        default=str(MEMORY_SEED_DIR),
        help=(
            "Directory to scan for *.md frontmatter entries. Defaults to "
            "``memory_seed/system/`` (canonical portco-agnostic location)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what WOULD be stamped without writing files.",
    )
    return parser.parse_args(argv)


def _flatten_paths(raw: Iterable[str]) -> List[str]:
    out: List[str] = []
    for item in raw:
        for piece in item.split(","):
            piece = piece.strip()
            if piece:
                out.append(piece)
    return out


def _resolve_commit(explicit: str | None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    env_sha = os.environ.get("GITHUB_SHA", "").strip()
    if env_sha:
        return env_sha
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(REPO_ROOT),
            stderr=subprocess.STDOUT,
            text=True,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise SystemExit(
            f"Cannot resolve commit SHA: pass --commit or set GITHUB_SHA "
            f"(git rev-parse failed: {exc})"
        )
    if not out:
        raise SystemExit("git rev-parse HEAD returned empty output")
    return out


def _matched_topics(touched_files: List[str]) -> List[str]:
    """Return the topics activated by any touched file path."""
    activated: List[str] = []
    for topic, cfg in TOPICS.items():
        for tf in touched_files:
            tf_norm = tf.strip()
            if not tf_norm:
                continue
            for needle in cfg["path_substrings"]:
                if needle in tf_norm:
                    activated.append(topic)
                    break
            if activated and activated[-1] == topic:
                break
    return activated


def _entry_belongs_to_topic(body: str, topic: str) -> bool:
    body_lower = body.lower()
    for needle in TOPICS[topic]["body_substrings"]:
        if needle.lower() in body_lower:
            return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Core
# ─────────────────────────────────────────────────────────────────────────────


def stamp(
    touched_files: List[str],
    commit_sha: str,
    memory_dir: Path,
    dry_run: bool = False,
) -> Tuple[List[Path], List[str]]:
    """Return (stamped_paths, activated_topics).

    ``stamped_paths`` is the list of memory files actually written (or
    that would be written under --dry-run). ``activated_topics`` is the
    list of topics that the touched files mapped to, for logging.
    """
    activated = _matched_topics(touched_files)
    if not activated:
        return [], []
    if not memory_dir.exists():
        print(
            f"memory directory does not exist: {memory_dir} — no-op.",
            file=sys.stderr,
        )
        return [], activated

    stamped: List[Path] = []
    for md_path in sorted(memory_dir.rglob("*.md")):
        text = md_path.read_text()
        fields = memory_hygiene.parse_frontmatter(text)
        if fields.get("kind") != "transient_infra":
            continue
        # If this entry already has the same supersede sha, skip — the
        # idempotency check inside mark_superseded also returns the
        # original text, but we'd still count it as "stamped". Be loud.
        if fields.get("superseded_at_commit") == commit_sha:
            continue
        # Match the entry's body to the activated topics. Use entire file
        # text for substring search — frontmatter mentions count too,
        # since e.g. the title hash may be in a header line below.
        if not any(_entry_belongs_to_topic(text, t) for t in activated):
            continue
        new_text = memory_hygiene.mark_superseded(text, commit_sha)
        if new_text == text:
            continue
        if not dry_run:
            md_path.write_text(new_text)
        stamped.append(md_path)
    return stamped, activated


def main(argv: List[str] | None = None) -> int:
    args = _parse_args(argv)
    touched = _flatten_paths(args.touched_files)
    if not touched:
        print("no --touched-files passed; nothing to do.", file=sys.stderr)
        return 0
    commit_sha = _resolve_commit(args.commit)
    memory_dir = Path(args.memory_dir).resolve()

    stamped, activated = stamp(
        touched_files=touched,
        commit_sha=commit_sha,
        memory_dir=memory_dir,
        dry_run=args.dry_run,
    )

    if not activated:
        print(
            "No topics matched the touched files — no transient_infra entries "
            "to invalidate."
        )
        return 0

    print(f"Activated topics: {', '.join(activated)}")
    print(f"Supersede SHA: {commit_sha}")
    print(f"Memory directory: {memory_dir}")
    if not stamped:
        print("No matching transient_infra entries found.")
        return 0
    label = "[dry-run] would stamp" if args.dry_run else "Stamped"
    print(f"{label} {len(stamped)} entries:")
    for p in stamped:
        try:
            rel = p.relative_to(REPO_ROOT)
            print(f"  {rel}")
        except ValueError:
            # Memory dir outside repo (e.g. in tests). Print absolute.
            print(f"  {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
