"""Tests for ``bin/install-hooks.py`` (Plan #44 — Task #7).

The script's filename has a hyphen, so we load it by path via
``importlib.util``. Tests redirect HOOK_SRC / HOOK_DST to a tmp_path
so install/uninstall never touch the real ``.git/hooks/``.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "bin" / "install-hooks.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("install_hooks", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def installer():
    return _load_module()


@pytest.fixture()
def fake_layout(tmp_path, monkeypatch, installer):
    """Build a sandboxed repo: .git/hooks/ + scripts/hooks/pre-commit."""
    repo = tmp_path / "repo"
    (repo / "scripts" / "hooks").mkdir(parents=True)
    (repo / ".git" / "hooks").mkdir(parents=True)
    src = repo / "scripts" / "hooks" / "pre-commit"
    src.write_text("#!/usr/bin/env bash\necho hook fired\n")
    src.chmod(0o755)
    dst = repo / ".git" / "hooks" / "pre-commit"

    monkeypatch.setattr(installer, "REPO_ROOT", repo)
    monkeypatch.setattr(installer, "HOOK_SRC", src)
    monkeypatch.setattr(installer, "HOOK_DST", dst)
    return repo, src, dst


def test_install_creates_symlink_when_supported(installer, fake_layout, capsys):
    repo, src, dst = fake_layout
    rc = installer._install()
    out = capsys.readouterr().out
    assert rc == 0
    assert dst.exists()
    assert dst.is_symlink()
    # Symlink target should be a relative path resolving back to src.
    resolved = (dst.parent / os.readlink(dst)).resolve()
    assert resolved == src.resolve()
    assert "before:" in out
    assert "after:" in out
    assert "Next steps:" in out
    assert "ANTHROPIC_API_KEY" in out


def test_install_falls_back_to_copy_when_symlink_unsupported(
    installer, fake_layout, capsys
):
    repo, src, dst = fake_layout
    with patch("os.symlink", side_effect=OSError("symlink not permitted")):
        rc = installer._install()
    out = capsys.readouterr().out
    assert rc == 0
    assert dst.exists()
    assert not dst.is_symlink()
    # Copy preserves content.
    assert dst.read_text() == src.read_text()
    assert "(copy of" in out


def test_install_is_idempotent(installer, fake_layout):
    """Running install twice should not error; second run replaces the link."""
    repo, src, dst = fake_layout
    assert installer._install() == 0
    assert installer._install() == 0
    assert dst.exists()


def test_install_dry_run_does_not_touch_filesystem(installer, fake_layout, capsys):
    repo, src, dst = fake_layout
    rc = installer._install(dry_run=True)
    out = capsys.readouterr().out
    assert rc == 0
    assert not dst.exists()
    assert "dry-run" in out


def test_install_fails_when_source_missing(installer, fake_layout, capsys):
    repo, src, dst = fake_layout
    src.unlink()
    rc = installer._install()
    assert rc == 1


def test_install_fails_when_git_hooks_dir_missing(installer, fake_layout, capsys):
    repo, src, dst = fake_layout
    # Remove .git/hooks entirely.
    import shutil

    shutil.rmtree(repo / ".git" / "hooks")
    rc = installer._install()
    assert rc == 1


def test_uninstall_removes_symlink(installer, fake_layout, capsys):
    repo, src, dst = fake_layout
    installer._install()  # arrange
    assert dst.exists()
    rc = installer._uninstall()
    out = capsys.readouterr().out
    assert rc == 0
    assert not dst.exists()
    assert "removed" in out
    assert "Next steps:" in out


def test_uninstall_removes_copy(installer, fake_layout):
    repo, src, dst = fake_layout
    with patch("os.symlink", side_effect=OSError("nope")):
        installer._install()
    assert dst.exists()
    assert not dst.is_symlink()
    rc = installer._uninstall()
    assert rc == 0
    assert not dst.exists()


def test_uninstall_noop_when_not_installed(installer, fake_layout, capsys):
    repo, src, dst = fake_layout
    rc = installer._uninstall()
    out = capsys.readouterr().out
    assert rc == 0
    assert "nothing to do" in out


def test_uninstall_dry_run_does_not_touch_filesystem(installer, fake_layout, capsys):
    repo, src, dst = fake_layout
    installer._install()
    rc = installer._uninstall(dry_run=True)
    out = capsys.readouterr().out
    assert rc == 0
    assert dst.exists()
    assert "dry-run" in out


def test_main_defaults_to_install(installer, fake_layout):
    repo, src, dst = fake_layout
    rc = installer.main([])
    assert rc == 0
    assert dst.exists()


def test_main_uninstall_flag(installer, fake_layout):
    repo, src, dst = fake_layout
    installer.main([])  # install
    rc = installer.main(["--uninstall"])
    assert rc == 0
    assert not dst.exists()


def test_main_install_and_uninstall_are_mutually_exclusive(installer, fake_layout):
    with pytest.raises(SystemExit) as exc:
        installer.main(["--install", "--uninstall"])
    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# Worktree resolution (Plan #44 review concern #1)
# ---------------------------------------------------------------------------
#
# In a real ``git worktree`` checkout, ``.git`` is a FILE with the line
# ``gitdir: /abs/path/to/main/.git/worktrees/<name>``. The canonical
# hooks dir lives on the MAIN repo at ``<main>/.git/hooks/``. Previously
# the installer hardcoded ``REPO_ROOT/.git/hooks`` and exited 1 on every
# worktree clone. ``_git_hooks_dir()`` must resolve to the main hooks
# directory via ``git rev-parse --git-common-dir``.


def _init_main_repo_with_worktree(tmp_path: Path):
    """Create a real ``main`` repo + ``worktree`` checkout under tmp_path.

    Returns ``(main_repo, worktree, expected_hooks_dir)``.
    """
    main_repo = tmp_path / "main"
    main_repo.mkdir()
    # init + a single commit so we have a branch to base the worktree on
    subprocess.run(
        ["git", "init", "-q", "-b", "trunk", str(main_repo)],
        check=True,
        capture_output=True,
    )
    # Required for `git commit` in CI sandboxes where no global identity
    # is configured.
    subprocess.run(
        ["git", "-C", str(main_repo), "config", "user.email", "t@example.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(main_repo), "config", "user.name", "t"],
        check=True,
        capture_output=True,
    )
    (main_repo / "README").write_text("hi\n")
    subprocess.run(
        ["git", "-C", str(main_repo), "add", "README"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(main_repo), "commit", "-q", "-m", "init"],
        check=True,
        capture_output=True,
    )
    # Spin up a worktree off a fresh branch
    worktree = tmp_path / "wt"
    subprocess.run(
        [
            "git",
            "-C",
            str(main_repo),
            "worktree",
            "add",
            "-b",
            "feat",
            str(worktree),
        ],
        check=True,
        capture_output=True,
    )
    expected_hooks = main_repo / ".git" / "hooks"
    return main_repo, worktree, expected_hooks


def test_git_hooks_dir_resolves_to_main_repo_in_worktree(
    tmp_path, installer, monkeypatch
):
    """Worktree checkouts must point hooks at the MAIN repo, not ``.git/hooks/`` under the worktree."""
    if not shutil_which("git"):
        pytest.skip("git CLI not available in test environment")

    main_repo, worktree, expected_hooks = _init_main_repo_with_worktree(tmp_path)

    # In a real worktree, .git is a FILE, not a directory.
    git_pointer = worktree / ".git"
    assert git_pointer.is_file(), "worktree .git should be a file containing gitdir:"

    # Point REPO_ROOT at the worktree; clear any monkeypatched HOOK_DST
    # so the resolver falls through to ``git rev-parse --git-common-dir``.
    monkeypatch.setattr(installer, "REPO_ROOT", worktree)
    monkeypatch.setattr(installer, "HOOK_DST", None)

    resolved = installer._git_hooks_dir()
    assert resolved is not None, (
        "git rev-parse --git-common-dir must succeed in a worktree"
    )
    assert resolved.resolve() == expected_hooks.resolve(), (
        f"hooks dir resolved to {resolved}, expected {expected_hooks}"
    )


def test_install_in_worktree_writes_to_main_repo_hooks(
    tmp_path, installer, monkeypatch
):
    """End-to-end: invoking _install() from a worktree writes the hook into the main repo's hooks dir."""
    if not shutil_which("git"):
        pytest.skip("git CLI not available in test environment")

    main_repo, worktree, expected_hooks = _init_main_repo_with_worktree(tmp_path)

    # Stage the hook source inside the worktree (matches REPO_ROOT/scripts/hooks/pre-commit)
    src_dir = worktree / "scripts" / "hooks"
    src_dir.mkdir(parents=True)
    src = src_dir / "pre-commit"
    src.write_text("#!/usr/bin/env bash\necho worktree hook\n")
    src.chmod(0o755)

    monkeypatch.setattr(installer, "REPO_ROOT", worktree)
    monkeypatch.setattr(installer, "HOOK_SRC", src)
    monkeypatch.setattr(installer, "HOOK_DST", None)  # force re-resolution

    rc = installer._install()
    assert rc == 0

    expected_hook = expected_hooks / "pre-commit"
    assert expected_hook.exists(), (
        f"hook should land in the MAIN repo at {expected_hook}, "
        f"not in the worktree's .git pointer-file path."
    )
    # The worktree directory itself must NOT have a .git/hooks/ subdir
    # — that would mean we wrote into the wrong place.
    assert (
        not (worktree / ".git" / "hooks").exists() or not (worktree / ".git").is_dir()
    )


def test_resolve_hook_dst_returns_none_outside_git_checkout(
    tmp_path, installer, monkeypatch
):
    """When REPO_ROOT is not a git checkout, _resolve_hook_dst returns None and _install errors helpfully."""
    if not shutil_which("git"):
        pytest.skip("git CLI not available in test environment")

    plain_dir = tmp_path / "not-a-repo"
    plain_dir.mkdir()
    (plain_dir / "scripts" / "hooks").mkdir(parents=True)
    src = plain_dir / "scripts" / "hooks" / "pre-commit"
    src.write_text("#!/usr/bin/env bash\necho hook\n")
    src.chmod(0o755)

    monkeypatch.setattr(installer, "REPO_ROOT", plain_dir)
    monkeypatch.setattr(installer, "HOOK_SRC", src)
    monkeypatch.setattr(installer, "HOOK_DST", None)

    assert installer._resolve_hook_dst() is None

    # The user-facing error should explain the failure clearly.
    import io
    import contextlib

    err_buf = io.StringIO()
    with contextlib.redirect_stderr(err_buf):
        rc = installer._install()
    assert rc == 1
    err = err_buf.getvalue()
    assert "git rev-parse --git-common-dir" in err
    assert "is this a git checkout?" in err


def shutil_which(name: str) -> bool:
    """Tiny shim — pytest.skip when ``git`` is unavailable."""
    import shutil as _shutil

    return _shutil.which(name) is not None
