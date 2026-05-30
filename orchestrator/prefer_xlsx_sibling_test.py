"""Tests for session_runner._prefer_xlsx_sibling.

Covers the swap-on-upload logic that makes the Parquet → xlsx UX transform
invisible to the agent:

  - Parquet path with xlsx sibling → swap to xlsx
  - Parquet path with no xlsx sibling → return Parquet (fallback)
  - Non-Parquet path → return unchanged
  - xlsx outside the safe attachment root → don't swap
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ORCH = Path(__file__).resolve().parent
if str(ORCH) not in sys.path:
    sys.path.insert(0, str(ORCH))


def _seed_required_env(monkeypatch):
    for k in (
        "ANTHROPIC_API_KEY",
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
        "SLACK_CHANNEL_ID",
        "ENVIRONMENT_ID",
        "DREAM_AGENT_ID",
        "COORDINATOR_ID",
        "QUICK_AGENT_ID",
        "METHODOLOGY_STORE_ID",
        "HEALTH_STORE_ID",
        "WRITING_AGENT_ID",
    ):
        monkeypatch.setenv(k, "x")


def test_swaps_parquet_for_xlsx_when_sibling_exists(monkeypatch, tmp_path):
    _seed_required_env(monkeypatch)
    import session_runner  # type: ignore

    parquet = tmp_path / "leads.parquet"
    parquet.write_bytes(b"\x00")
    xlsx = tmp_path / "leads.xlsx"
    xlsx.write_bytes(b"\x00")

    with patch.object(session_runner, "_is_safe_attachment_path", return_value=True):
        result = session_runner._prefer_xlsx_sibling(str(parquet))

    assert result == str(xlsx), "should swap to the .xlsx sibling"


def test_returns_parquet_when_no_xlsx_sibling(monkeypatch, tmp_path):
    """If xlsx materialization failed, fall back to Parquet — never lose the artifact."""
    _seed_required_env(monkeypatch)
    import session_runner  # type: ignore

    parquet = tmp_path / "leads.parquet"
    parquet.write_bytes(b"\x00")
    # No sibling xlsx file.

    with patch.object(session_runner, "_is_safe_attachment_path", return_value=True):
        result = session_runner._prefer_xlsx_sibling(str(parquet))

    assert result == str(parquet), "fallback to Parquet when xlsx sibling missing"


def test_non_parquet_path_returns_unchanged(monkeypatch, tmp_path):
    _seed_required_env(monkeypatch)
    import session_runner  # type: ignore

    for ext in (".csv", ".docx", ".png", ".pdf", ""):
        path = str(tmp_path / f"data{ext}")
        assert session_runner._prefer_xlsx_sibling(path) == path, (
            f"non-Parquet path with extension {ext!r} should pass through unchanged"
        )


def test_empty_or_none_path_returns_unchanged(monkeypatch):
    _seed_required_env(monkeypatch)
    import session_runner  # type: ignore

    assert session_runner._prefer_xlsx_sibling("") == ""
    assert session_runner._prefer_xlsx_sibling(None) is None  # type: ignore[arg-type]


def test_does_not_swap_when_xlsx_sibling_outside_safe_root(monkeypatch, tmp_path):
    """A sibling that fails the safe-path check is treated as missing.

    Defensive: if a Parquet lives inside the session output dir but the
    xlsx sibling somehow doesn't (e.g. symlink games, future code that
    writes xlsx to /tmp), the attach step keeps using Parquet rather than
    uploading a file from an untrusted location.
    """
    _seed_required_env(monkeypatch)
    import session_runner  # type: ignore

    parquet = tmp_path / "leads.parquet"
    parquet.write_bytes(b"\x00")
    xlsx = tmp_path / "leads.xlsx"
    xlsx.write_bytes(b"\x00")

    # Pretend the safe-path check rejects the xlsx (e.g. it's outside SESSION_OUTPUT_DIR).
    def fake_safe(path: str) -> bool:
        return not path.endswith(".xlsx")

    with patch.object(
        session_runner, "_is_safe_attachment_path", side_effect=fake_safe
    ):
        result = session_runner._prefer_xlsx_sibling(str(parquet))

    assert result == str(parquet), "unsafe xlsx sibling should not be swapped"
