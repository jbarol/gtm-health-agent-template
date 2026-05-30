"""Tests for ``orchestrator.materialize_xlsx_tool``.

Covers the contract added to close the call-prep deliverable gap
(2026-05-13 incident — session ``sesn_EXAMPLE`` went idle
after ``COPY (SELECT ...) TO 'foo.xlsx'`` was rejected by query_artifact):

    - Passthrough mode: a single Parquet → single-sheet .xlsx with no SQL.
    - SQL mode: filter / reshape a Parquet via DuckDB before writing.
    - Multi-sheet mode: one .xlsx with several named sheets from different
      sources.
    - Streaming path: rows > PANDAS_BUFFER_ROW_LIMIT use openpyxl
      write_only's streaming write instead of buffering through pandas.
    - Security rejections:
        * output_name with ".." / slashes
        * output_name with non-.xlsx extension
        * input path outside SESSION_OUTPUT_DIR
        * symlink in input path
        * COPY / PRAGMA / SET in SQL
    - Validation errors return ok=False, never raise.

Run::

    cd orchestrator && python3 -m pytest materialize_xlsx_tool_test.py -v
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import pytest

from materialize_xlsx_tool import (  # pyright: ignore[reportMissingImports]
    PANDAS_BUFFER_ROW_LIMIT,
    materialize_xlsx,
)


# ──────────────────────────────────────────────────────────────────────────
# Test helpers
# ──────────────────────────────────────────────────────────────────────────


def _write_parquet(path: Path, rows: list[dict]) -> str:
    """Write a list of row dicts as a Parquet file. Returns the path."""
    df = pd.DataFrame(rows)
    df.to_parquet(path, engine="pyarrow", index=False)
    return str(path)


def _read_xlsx_sheets(path: str) -> dict[str, list[dict]]:
    """Return ``{sheet_name: [row_dict, ...]}`` for verification."""
    out: dict[str, list[dict]] = {}
    xl = pd.ExcelFile(path)
    for name in xl.sheet_names:
        # parse(sheet_name=str) returns a single DataFrame.
        df: pd.DataFrame = xl.parse(name)  # type: ignore[assignment]
        out[str(name)] = df.to_dict("records")
    return out


@pytest.fixture
def session_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a session-output dir and point ``SESSION_OUTPUT_DIR`` at it."""
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    monkeypatch.setenv("SESSION_OUTPUT_DIR", str(outputs))
    return outputs


# ──────────────────────────────────────────────────────────────────────────
# Happy paths
# ──────────────────────────────────────────────────────────────────────────


def test_passthrough_single_sheet(session_outputs: Path):
    """Single Parquet, no SQL: every row appears in one sheet named 'data'."""
    rows = [{"id": i, "subject": f"row-{i}"} for i in range(10)]
    fpath = _write_parquet(session_outputs / "events.parquet", rows)

    result = materialize_xlsx(
        output_name="passthrough.xlsx",
        file_paths=[fpath],
    )

    assert result["ok"] is True, result
    assert result["total_rows"] == 10
    assert len(result["sheets"]) == 1
    assert result["sheets"][0]["sheet_name"] == "data"
    assert result["sheets"][0]["row_count"] == 10
    out_path = result["file_path"]
    assert out_path.endswith("/passthrough.xlsx")
    assert os.path.isfile(out_path)

    written = _read_xlsx_sheets(out_path)
    assert list(written.keys()) == ["data"]
    assert len(written["data"]) == 10
    assert written["data"][0]["id"] == 0
    assert written["data"][0]["subject"] == "row-0"


def test_sql_mode_filters_and_projects(session_outputs: Path):
    """SQL mode runs DuckDB against the Parquet and writes the filtered result."""
    rows = [
        {"id": 1, "type": "Event", "subject": "demo"},
        {"id": 2, "type": "Task", "subject": "call"},
        {"id": 3, "type": "Event", "subject": "meeting"},
    ]
    fpath = _write_parquet(session_outputs / "all.parquet", rows)

    result = materialize_xlsx(
        output_name="events_only.xlsx",
        file_paths=[fpath],
        sql="SELECT id, subject FROM t WHERE type='Event' ORDER BY id",
        sheet_name="Events",
    )

    assert result["ok"] is True, result
    assert result["total_rows"] == 2
    assert result["sheets"][0]["sheet_name"] == "Events"
    written = _read_xlsx_sheets(result["file_path"])
    assert list(written.keys()) == ["Events"]
    assert [r["id"] for r in written["Events"]] == [1, 3]
    assert "type" not in written["Events"][0]  # SQL projected it away


def test_multi_sheet_with_different_sources(session_outputs: Path):
    """Multi-sheet: each spec contributes a named sheet to the same workbook."""
    events_rows = [{"id": i, "subject": f"e{i}"} for i in range(3)]
    tasks_rows = [{"id": i, "subject": f"t{i}", "status": "Open"} for i in range(5)]
    events_path = _write_parquet(session_outputs / "events.parquet", events_rows)
    tasks_path = _write_parquet(session_outputs / "tasks.parquet", tasks_rows)

    result = materialize_xlsx(
        output_name="call_prep_brief.xlsx",
        sheets=[
            {"sheet_name": "Events", "file_paths": [events_path]},
            {"sheet_name": "Tasks", "file_paths": [tasks_path]},
        ],
    )

    assert result["ok"] is True, result
    assert result["total_rows"] == 8
    written = _read_xlsx_sheets(result["file_path"])
    assert list(written.keys()) == ["Events", "Tasks"]
    assert len(written["Events"]) == 3
    assert len(written["Tasks"]) == 5


def test_multi_sheet_with_per_sheet_sql(session_outputs: Path):
    """Each sheet can carry its own SQL filter."""
    rows = [{"id": i, "type": "Event" if i % 2 == 0 else "Task"} for i in range(10)]
    fpath = _write_parquet(session_outputs / "all.parquet", rows)

    result = materialize_xlsx(
        output_name="split.xlsx",
        sheets=[
            {
                "sheet_name": "Events",
                "file_paths": [fpath],
                "sql": "SELECT id FROM t WHERE type='Event' ORDER BY id",
            },
            {
                "sheet_name": "Tasks",
                "file_paths": [fpath],
                "sql": "SELECT id FROM t WHERE type='Task' ORDER BY id",
            },
        ],
    )

    assert result["ok"] is True, result
    written = _read_xlsx_sheets(result["file_path"])
    assert [r["id"] for r in written["Events"]] == [0, 2, 4, 6, 8]
    assert [r["id"] for r in written["Tasks"]] == [1, 3, 5, 7, 9]


def test_streaming_path_for_large_input(session_outputs: Path):
    """Inputs above PANDAS_BUFFER_ROW_LIMIT use the streaming writer."""
    rows = [{"id": i, "label": f"row-{i}"} for i in range(PANDAS_BUFFER_ROW_LIMIT + 50)]
    fpath = _write_parquet(session_outputs / "big.parquet", rows)

    result = materialize_xlsx(
        output_name="big.xlsx",
        file_paths=[fpath],
    )

    assert result["ok"] is True, result
    assert result["total_rows"] == PANDAS_BUFFER_ROW_LIMIT + 50
    written = _read_xlsx_sheets(result["file_path"])
    assert len(written["data"]) == PANDAS_BUFFER_ROW_LIMIT + 50
    assert written["data"][0]["id"] == 0
    assert written["data"][-1]["id"] == PANDAS_BUFFER_ROW_LIMIT + 49


def test_extension_auto_appended(session_outputs: Path):
    """If the caller omits the .xlsx extension, the tool adds it."""
    rows = [{"id": 1}]
    fpath = _write_parquet(session_outputs / "tiny.parquet", rows)

    result = materialize_xlsx(
        output_name="report",
        file_paths=[fpath],
    )

    assert result["ok"] is True, result
    assert result["file_path"].endswith("/report.xlsx")


# ──────────────────────────────────────────────────────────────────────────
# Security: output_name guards
# ──────────────────────────────────────────────────────────────────────────


def test_rejects_output_name_with_path_separator(session_outputs: Path):
    rows = [{"id": 1}]
    fpath = _write_parquet(session_outputs / "x.parquet", rows)
    result = materialize_xlsx(
        output_name="../etc/evil.xlsx",
        file_paths=[fpath],
    )
    assert result["ok"] is False
    assert "path separators" in result["error"] or ".." in result["error"]


def test_rejects_output_name_leading_dot(session_outputs: Path):
    rows = [{"id": 1}]
    fpath = _write_parquet(session_outputs / "x.parquet", rows)
    result = materialize_xlsx(
        output_name=".hidden.xlsx",
        file_paths=[fpath],
    )
    assert result["ok"] is False
    assert "start with '.'" in result["error"]


def test_rejects_output_name_non_xlsx_extension(session_outputs: Path):
    rows = [{"id": 1}]
    fpath = _write_parquet(session_outputs / "x.parquet", rows)
    result = materialize_xlsx(
        output_name="data.csv",
        file_paths=[fpath],
    )
    assert result["ok"] is False
    assert "'.xlsx'" in result["error"]


def test_rejects_output_name_with_shell_meta(session_outputs: Path):
    rows = [{"id": 1}]
    fpath = _write_parquet(session_outputs / "x.parquet", rows)
    result = materialize_xlsx(
        output_name="bad;name.xlsx",
        file_paths=[fpath],
    )
    assert result["ok"] is False


# ──────────────────────────────────────────────────────────────────────────
# Security: input path guards
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("session_outputs")
def test_rejects_input_outside_session_dir(tmp_path: Path):
    """Input outside SESSION_OUTPUT_DIR is rejected with a clear error."""
    outside = tmp_path / "outside.parquet"
    _write_parquet(outside, [{"id": 1}])
    result = materialize_xlsx(
        output_name="out.xlsx",
        file_paths=[str(outside)],
    )
    assert result["ok"] is False
    assert "outside the session output directory" in result["error"]


def test_rejects_symlink_input(tmp_path: Path, session_outputs: Path):
    """A symlink inside the session dir pointing OUT is rejected."""
    outside = tmp_path / "secret.parquet"
    _write_parquet(outside, [{"id": 1}])
    sym = session_outputs / "innocuous.parquet"
    os.symlink(outside, sym)
    result = materialize_xlsx(
        output_name="out.xlsx",
        file_paths=[str(sym)],
    )
    assert result["ok"] is False
    assert "symlink" in result["error"] or "outside" in result["error"]


# ──────────────────────────────────────────────────────────────────────────
# Security: SQL guards
# ──────────────────────────────────────────────────────────────────────────


def test_rejects_copy_statement(session_outputs: Path):
    """The bug that broke sesn_EXAMPLE: COPY in SQL.

    Confirms the tool surfaces a clear, actionable error rather than
    silently failing.
    """
    rows = [{"id": 1}]
    fpath = _write_parquet(session_outputs / "x.parquet", rows)
    result = materialize_xlsx(
        output_name="copy.xlsx",
        file_paths=[fpath],
        sql="COPY (SELECT * FROM t) TO 'foo.xlsx'",
    )
    assert result["ok"] is False
    assert "COPY" in result["error"]


def test_rejects_pragma_statement(session_outputs: Path):
    rows = [{"id": 1}]
    fpath = _write_parquet(session_outputs / "x.parquet", rows)
    result = materialize_xlsx(
        output_name="x.xlsx",
        file_paths=[fpath],
        sql="PRAGMA show_tables",
    )
    assert result["ok"] is False
    assert "PRAGMA" in result["error"]


def test_rejects_set_statement(session_outputs: Path):
    rows = [{"id": 1}]
    fpath = _write_parquet(session_outputs / "x.parquet", rows)
    result = materialize_xlsx(
        output_name="x.xlsx",
        file_paths=[fpath],
        sql="SET enable_external_access = true",
    )
    assert result["ok"] is False
    assert "SET" in result["error"]


# ──────────────────────────────────────────────────────────────────────────
# Validation errors
# ──────────────────────────────────────────────────────────────────────────


def test_rejects_both_single_and_multi_sheet(session_outputs: Path):
    rows = [{"id": 1}]
    fpath = _write_parquet(session_outputs / "x.parquet", rows)
    result = materialize_xlsx(
        output_name="x.xlsx",
        file_paths=[fpath],
        sheets=[{"sheet_name": "S", "file_paths": [fpath]}],
    )
    assert result["ok"] is False
    assert "either" in result["error"].lower() or "both" in result["error"].lower()


def test_rejects_passthrough_with_multiple_files(session_outputs: Path):
    """Passthrough mode requires exactly one file; multi needs sql."""
    rows = [{"id": 1}]
    fp1 = _write_parquet(session_outputs / "a.parquet", rows)
    fp2 = _write_parquet(session_outputs / "b.parquet", rows)
    result = materialize_xlsx(
        output_name="x.xlsx",
        file_paths=[fp1, fp2],
    )
    assert result["ok"] is False
    assert "one file" in result["error"] or "single" in result["error"]


def test_rejects_missing_file(session_outputs: Path):  # noqa: ARG001 — fixture sets env var
    result = materialize_xlsx(
        output_name="x.xlsx",
        file_paths=[str(session_outputs / "does_not_exist.parquet")],
    )
    assert result["ok"] is False
    assert "not found" in result["error"]


def test_rejects_unsupported_extension(session_outputs: Path):
    """Only .parquet and .csv are supported as inputs."""
    bad = session_outputs / "data.json"
    bad.write_text("{}")
    result = materialize_xlsx(
        output_name="x.xlsx",
        file_paths=[str(bad)],
    )
    assert result["ok"] is False


def test_rejects_invalid_sheet_name(session_outputs: Path):
    """Excel-forbidden chars in sheet names rejected before write."""
    rows = [{"id": 1}]
    fpath = _write_parquet(session_outputs / "x.parquet", rows)
    result = materialize_xlsx(
        output_name="x.xlsx",
        file_paths=[fpath],
        sheet_name="bad:name",
    )
    assert result["ok"] is False


def test_rejects_long_sheet_name(session_outputs: Path):
    """Excel sheet names are capped at 31 chars."""
    rows = [{"id": 1}]
    fpath = _write_parquet(session_outputs / "x.parquet", rows)
    result = materialize_xlsx(
        output_name="x.xlsx",
        file_paths=[fpath],
        sheet_name="x" * 32,
    )
    assert result["ok"] is False
    assert "31" in result["error"]
