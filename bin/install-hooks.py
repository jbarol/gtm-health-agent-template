#!/usr/bin/env python3
"""Install the prompt-preview pre-commit hook into ``.git/hooks/``.

Plan #44 — Task #7 (decision row #23). Cross-platform installer (Python,
not shell) so Windows clones can opt into the same local convenience
check. The local hook is bypassable; CI is the actual gate. See
``docs/runbooks/managed-agents-conformance.md``.

Usage:

    python bin/install-hooks.py              # install (default)
    python bin/install-hooks.py --install    # explicit install
    python bin/install-hooks.py --uninstall  # remove the installed hook
    python bin/install-hooks.py --dry-run    # print what would happen

The hook source lives at ``scripts/hooks/pre-commit``. On POSIX systems
we use a relative symlink so subsequent edits to the source propagate
without reinstalling. On Windows (where the standard user lacks symlink
privilege) we fall back to a file copy and tell the user they will
need to re-run this script after editing the source.
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_SRC = REPO_ROOT / "scripts" / "hooks" / "pre-commit"
# ``HOOK_DST`` is computed at call time via ``_resolve_hook_dst()`` so that
# ``git worktree`` checkouts (where ``.git`` is a file, not a directory)
# resolve to the canonical ``<main-repo>/.git/hooks/`` location. Tests
# monkeypatch this attribute to redirect installation into a tmp_path
# sandbox; the runtime helpers prefer the monkeypatched value when set.
HOOK_DST: Optional[Path] = None

RUNBOOK_REF = "docs/runbooks/managed-agents-conformance.md"


def _git_hooks_dir() -> Optional[Path]:
    """Resolve the real ``hooks/`` directory for the current checkout.

    In a normal clone this is ``<repo>/.git/hooks/``. In a ``git worktree``
    (which Plan #44 review #1 flagged) ``.git`` is a *file* containing a
    ``gitdir:`` pointer at ``<main-repo>/.git/worktrees/<name>/`` — and
    the canonical hooks directory still lives on the MAIN repo at
    ``<main-repo>/.git/hooks/``. ``git rev-parse --git-common-dir`` is
    the only portable way to find that path; ``--git-dir`` returns the
    per-worktree dir which has no ``hooks/`` of its own.

    Returns ``None`` if ``git`` is unavailable or this directory is not
    a git checkout. Callers print the user-facing error.
    """
    try:
        common = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=str(REPO_ROOT),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    if not common:
        return None
    common_path = Path(common)
    if not common_path.is_absolute():
        common_path = (REPO_ROOT / common_path).resolve()
    return (common_path / "hooks").resolve()


def _resolve_hook_dst() -> Optional[Path]:
    """Return the absolute target path for the installed hook, or ``None``.

    Honors a manually overridden module-level ``HOOK_DST`` (used by the
    test fixture to redirect into a tmp_path sandbox) before falling
    back to ``git rev-parse --git-common-dir``. ``None`` means the
    fallback could not locate a git checkout at ``REPO_ROOT``.
    """
    override = globals().get("HOOK_DST")
    if override is not None:
        return Path(override)
    hooks_dir = _git_hooks_dir()
    if hooks_dir is None:
        return None
    return hooks_dir / "pre-commit"


def _rel(path: Path) -> str:
    """Pretty-print a path relative to REPO_ROOT when possible."""
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _install(dry_run: bool = False) -> int:
    """Symlink (or copy on Windows) the hook into the resolved hooks dir."""
    if not HOOK_SRC.exists():
        print(
            f"FAILED: hook source missing at {_rel(HOOK_SRC)}.",
            file=sys.stderr,
        )
        return 1
    hook_dst = _resolve_hook_dst()
    if hook_dst is None or not hook_dst.parent.exists():
        print(
            "FAILED: could not locate git hooks directory "
            "(git rev-parse --git-common-dir) — is this a git checkout?",
            file=sys.stderr,
        )
        return 1

    # Report before-state.
    if hook_dst.exists() or hook_dst.is_symlink():
        if hook_dst.is_symlink():
            target = os.readlink(hook_dst)
            print(f"before: {_rel(hook_dst)} -> {target}")
        else:
            print(
                f"before: {_rel(hook_dst)} "
                f"(plain file, {hook_dst.stat().st_size} bytes)"
            )
    else:
        print(f"before: {_rel(hook_dst)} not present")

    if dry_run:
        print("dry-run: skipping install action.")
        return 0

    # Wipe any existing hook so the install is idempotent.
    if hook_dst.exists() or hook_dst.is_symlink():
        hook_dst.unlink()

    used_symlink = False
    try:
        rel_src = os.path.relpath(HOOK_SRC, hook_dst.parent)
        os.symlink(rel_src, hook_dst)
        used_symlink = True
    except (OSError, NotImplementedError):
        # Windows without developer mode lands here. Fall back to a copy.
        shutil.copy2(HOOK_SRC, hook_dst)

    # Ensure the executable bit. (No-op on Windows where it is implicit.)
    try:
        mode = hook_dst.stat().st_mode
        hook_dst.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass

    if used_symlink:
        print(f"after:  {_rel(hook_dst)} -> {os.readlink(hook_dst)} (symlink)")
    else:
        print(
            f"after:  {_rel(hook_dst)} "
            f"(copy of scripts/hooks/pre-commit — re-run install after edits)"
        )

    print()
    print("Next steps:")
    print(
        "  1. Make sure ANTHROPIC_API_KEY is set in your shell (or .env). "
        "Without it the hook will skip with a notice and CI will gate."
    )
    print(
        "  2. Stage a prompt change in agents/setup_agents.py or "
        "agents/update_prompts.py and run `git commit` — the hook fires."
    )
    print(f"  See {RUNBOOK_REF}#prompt-preview-pre-commit-hook")
    return 0


def _uninstall(dry_run: bool = False) -> int:
    """Remove the installed hook. Idempotent."""
    hook_dst = _resolve_hook_dst()
    if hook_dst is None:
        print(
            "FAILED: could not locate git hooks directory "
            "(git rev-parse --git-common-dir) — is this a git checkout?",
            file=sys.stderr,
        )
        return 1

    if not (hook_dst.exists() or hook_dst.is_symlink()):
        print(f"before: {_rel(hook_dst)} not present — nothing to do.")
        return 0

    if hook_dst.is_symlink():
        target = os.readlink(hook_dst)
        print(f"before: {_rel(hook_dst)} -> {target}")
    else:
        print(f"before: {_rel(hook_dst)} (plain file, {hook_dst.stat().st_size} bytes)")

    if dry_run:
        print("dry-run: skipping uninstall action.")
        return 0

    hook_dst.unlink()
    print(f"after:  {_rel(hook_dst)} removed.")
    print()
    print("Next steps:")
    print(
        "  - The prompt-preview hook is no longer wired locally. CI "
        "(ci-prompt-preview.yml) still gates every PR."
    )
    print(f"  See {RUNBOOK_REF}#prompt-preview-pre-commit-hook")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Install (or remove) the prompt-preview pre-commit hook. "
            f"See {RUNBOOK_REF}."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--install",
        action="store_true",
        help="Install the hook into .git/hooks/pre-commit (default).",
    )
    mode.add_argument(
        "--uninstall",
        action="store_true",
        help="Remove the installed hook.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would happen without changing the filesystem.",
    )
    args = parser.parse_args(argv)

    if args.uninstall:
        return _uninstall(dry_run=args.dry_run)
    # Default action is install.
    return _install(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
