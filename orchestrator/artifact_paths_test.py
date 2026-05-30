"""Tests for ``artifact_paths`` — canonical resolver for session artifacts.

Verifies the contract documented in artifact_paths.py:
    - session_output_dir() honors SESSION_OUTPUT_DIR env at call time
    - resolve_artifact_path() rebases legacy /mnt/session/outputs paths
      to the canonical SESSION_OUTPUT_DIR by basename
    - sweep_session_artifacts respects the age cutoff and only touches
      .parquet/.xlsx/.csv files
"""

from __future__ import annotations

import os
import time

from artifact_paths import (  # pyright: ignore[reportMissingImports]
    is_under_session_output_dir,
    resolve_artifact_path,
    session_output_dir,
    sweep_session_artifacts,
)


def test_session_output_dir_honors_env(monkeypatch, tmp_path):
    """When SESSION_OUTPUT_DIR is set, it (canonicalized) wins."""
    target = tmp_path / "vol"
    target.mkdir()
    monkeypatch.setenv("SESSION_OUTPUT_DIR", str(target))
    assert session_output_dir() == os.path.realpath(str(target))


def test_session_output_dir_default_when_env_unset(monkeypatch):
    """Unset env → legacy /mnt/session/outputs default (realpath'd)."""
    monkeypatch.delenv("SESSION_OUTPUT_DIR", raising=False)
    assert session_output_dir() == os.path.realpath("/mnt/session/outputs")


def test_resolve_legacy_prefix_rebases_to_canonical(monkeypatch, tmp_path):
    """A path under /mnt/session/outputs/ is rebased onto SESSION_OUTPUT_DIR
    by basename when the env redirects the canonical root.
    """
    canonical = tmp_path / "vol"
    canonical.mkdir()
    monkeypatch.setenv("SESSION_OUTPUT_DIR", str(canonical))

    legacy = "/mnt/session/outputs/sf_foo_20260516T120000000000_abcd.parquet"
    resolved = resolve_artifact_path(legacy)
    assert resolved == os.path.join(
        os.path.realpath(str(canonical)),
        "sf_foo_20260516T120000000000_abcd.parquet",
    )


def test_resolve_already_canonical_unchanged(monkeypatch, tmp_path):
    """A path already under SESSION_OUTPUT_DIR is returned as realpath
    without rebasing (the basename strategy could otherwise lose a
    sub-directory that may exist in the future).
    """
    canonical = tmp_path / "vol"
    canonical.mkdir()
    monkeypatch.setenv("SESSION_OUTPUT_DIR", str(canonical))

    direct = canonical / "x.parquet"
    direct.write_bytes(b"")
    assert resolve_artifact_path(str(direct)) == os.path.realpath(str(direct))


def test_resolve_no_rebase_when_canonical_matches_legacy(monkeypatch):
    """When env is unset (or set to /mnt/session/outputs), there is no
    rebase: the legacy prefix IS the canonical root.
    """
    monkeypatch.delenv("SESSION_OUTPUT_DIR", raising=False)
    legacy = "/mnt/session/outputs/foo.parquet"
    # realpath is idempotent on a non-existent path
    assert resolve_artifact_path(legacy) == os.path.realpath(legacy)


def test_resolve_handles_empty_and_non_string():
    """Empty/None/non-string inputs return safely instead of crashing."""
    assert resolve_artifact_path("") == ""
    assert resolve_artifact_path(None) == ""  # type: ignore[arg-type]


def test_is_under_session_output_dir_true_for_canonical(monkeypatch, tmp_path):
    canonical = tmp_path / "vol"
    canonical.mkdir()
    monkeypatch.setenv("SESSION_OUTPUT_DIR", str(canonical))
    child = canonical / "x.parquet"
    child.write_bytes(b"")
    assert is_under_session_output_dir(str(child))


def test_is_under_session_output_dir_false_for_outside(monkeypatch, tmp_path):
    canonical = tmp_path / "vol"
    canonical.mkdir()
    monkeypatch.setenv("SESSION_OUTPUT_DIR", str(canonical))
    outside = tmp_path / "other.parquet"
    outside.write_bytes(b"")
    assert not is_under_session_output_dir(str(outside))


def test_sweep_deletes_only_old_files(monkeypatch, tmp_path):
    """Files older than max_age_days are deleted; younger files stay.
    Only .parquet / .xlsx / .csv are touched; unrelated extensions ignored.
    """
    monkeypatch.setenv("SESSION_OUTPUT_DIR", str(tmp_path))

    old_age = time.time() - 30 * 86400  # 30 days
    fresh_age = time.time() - 1 * 86400  # 1 day

    old_parquet = tmp_path / "old.parquet"
    old_parquet.write_bytes(b"x")
    os.utime(old_parquet, (old_age, old_age))

    old_xlsx = tmp_path / "old.xlsx"
    old_xlsx.write_bytes(b"x")
    os.utime(old_xlsx, (old_age, old_age))

    old_unrelated = tmp_path / "old.md"
    old_unrelated.write_bytes(b"x")
    os.utime(old_unrelated, (old_age, old_age))

    fresh_parquet = tmp_path / "fresh.parquet"
    fresh_parquet.write_bytes(b"x")
    os.utime(fresh_parquet, (fresh_age, fresh_age))

    stats = sweep_session_artifacts(max_age_days=14)
    assert stats["deleted"] == 2  # only the two old artifacts
    assert not old_parquet.exists()
    assert not old_xlsx.exists()
    assert old_unrelated.exists(), "non-artifact extension must not be swept"
    assert fresh_parquet.exists(), "fresh artifact must not be swept"


def test_sweep_skipped_when_root_missing(monkeypatch, tmp_path):
    """If SESSION_OUTPUT_DIR points at a non-existent directory, sweep
    returns zeros rather than crashing — the boot path must never raise
    over a missing volume.
    """
    monkeypatch.setenv("SESSION_OUTPUT_DIR", str(tmp_path / "does_not_exist"))
    stats = sweep_session_artifacts(max_age_days=14)
    assert stats == {"scanned": 0, "deleted": 0, "freed_bytes": 0, "error_count": 0}
