"""Materialize a user-facing .xlsx deliverable from one or more Parquet artifacts.

Why this exists
---------------
Track G's ``dump_sf_query`` writes Salesforce rows to Parquet without ever
holding them in agent context, and Track H's ``query_artifact`` runs DuckDB SQL
over those artifacts inside a locked-down sandbox. Both keep data out of the
model's tokens.

What was missing: a way for the Coordinator to produce a single named .xlsx
file the human user can open, with content the model chose (filtered, joined,
multi-sheet). Without this, the model attempted ``COPY (SELECT ...) TO
'foo.xlsx'`` inside ``query_artifact`` and ran into the read-only SQL
sandbox — the request is rejected by the parse guard, and the model had no
documented alternative. Live trace 2026-05-13 19:18 PT (call-prep session
``sesn_EXAMPLE``) shows the failure: the Coordinator went
idle after the rejection and never produced a deliverable.

This tool closes that gap. The agent passes one or more Parquet artifacts
(produced by ``dump_sf_query`` or ``query_artifact``) plus an output filename;
the orchestrator writes the .xlsx and returns a handle the model can attach
to ``post_report``. SQL is optional — when present, it runs in the same
locked-down DuckDB sandbox as ``query_artifact``; when absent, the tool
streams the Parquet rows straight through.

Design rules (mirror ``artifact_query_tool``)
---------------------------------------------
1. **Path-whitelist on inputs.** Every input path must canonicalize inside
   ``SESSION_OUTPUT_DIR``; symlink escapes are rejected.
2. **Filename whitelist on outputs.** ``output_name`` is a bare filename
   (no path separators, no leading dots, no ``..``). The orchestrator
   computes the final path. Mirrors the same defense-in-depth model.
3. **Read-only SQL.** Optional SQL runs inside DuckDB with
   ``enable_external_access = false``. The same leading-keyword denylist
   blocks PRAGMA / SET / COPY / ATTACH / LOAD / etc.
4. **Streaming write.** Inputs ≤ 5,000 rows go through pandas
   (header preservation is easier); larger inputs stream via
   ``openpyxl write_only`` so 191K-row Lead dumps don't blow the
   Railway container's 512 MB cap.
5. **Multi-sheet support.** The model can pass a single
   ``{file_paths, sql, sheet_name}`` triple, OR a ``sheets`` list to
   build a workbook with one sheet per source. Multi-sheet is the
   important shape — call-prep briefs typically need an "Events" sheet,
   a "Tasks" sheet, a "Summary" sheet.
6. **Never raise.** Every failure path returns a dict the agent reads
   and reacts to. An unhandled exception would push the orchestrator
   into the catastrophic-failure path.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

__all__ = ["materialize_xlsx"]


log = logging.getLogger(__name__)


SUPPORTED_INPUT_EXTS = {".parquet", ".csv"}


# Max rows per sheet that we'll buffer through pandas instead of streaming.
# Above this, switch to openpyxl write_only for memory safety. The 5,000-row
# threshold balances "header preservation is easy" against "Railway has
# 512 MB". A 5,000-row Salesforce Lead row is ~2.5 MB in pandas; 50,000 rows
# is ~25 MB — fine for buffered, but 191K rows is ~95 MB and warrants
# streaming.
PANDAS_BUFFER_ROW_LIMIT = 5_000


# Output filename safety. The agent supplies a bare filename; the orchestrator
# decides the directory. Rejects path traversal (..), separators, and shell
# metacharacters.
_VALID_FILENAME_RE = re.compile(r"^[A-Za-z0-9_\-. ]+$")


def _session_output_dir() -> str:
    """Resolve the canonical session output directory.

    Mirrors ``artifact_query_tool._session_output_dir`` so the two modules
    agree on what's safe to read. Re-implemented locally to keep the import
    graph cycle-free (``session_runner`` lazy-imports both).
    """
    raw = os.environ.get("SESSION_OUTPUT_DIR") or "/mnt/session/outputs"
    return os.path.realpath(raw)


def _is_safe_artifact_path(path: str) -> bool:
    """Return True iff ``path`` is safe to read as an artifact.

    Identical logic to ``artifact_query_tool._is_safe_artifact_path`` —
    canonical absolute path, no symlink-bearing segments, must live under
    the session output directory. We re-implement rather than import to
    keep the two tools' security contracts independent: a regression in
    one shouldn't silently widen the surface of the other.
    """
    if not path or not isinstance(path, str):
        return False

    safe_root = _session_output_dir()
    abs_input = os.path.abspath(path)

    segments: list[str] = []
    current = abs_input
    while True:
        parent, _ = os.path.split(current)
        if parent == current:
            segments.append(current)
            break
        segments.append(current)
        current = parent
    for seg in segments:
        try:
            if os.path.islink(seg):
                return False
        except OSError:
            return False

    canonical = os.path.realpath(abs_input)
    try:
        common = os.path.commonpath([canonical, safe_root])
    except ValueError:
        return False
    return common == safe_root


def _normalize_output_name(output_name: str) -> tuple[str | None, str | None]:
    """Validate and normalize the user-supplied output filename.

    Returns ``(safe_basename, None)`` on success, ``(None, error_message)``
    on rejection. The basename always ends in ``.xlsx``; if the caller
    omits the extension we append it. Path separators, ``..``, leading
    dots, and shell-meta characters are rejected so the agent can't
    coax the orchestrator into writing outside the output directory or
    overwriting a sibling artifact handle.
    """
    if not isinstance(output_name, str) or not output_name.strip():
        return None, "output_name must be a non-empty string"
    name = output_name.strip()
    # No path separators or parent-dir traversal.
    if "/" in name or "\\" in name or ".." in name:
        return None, "output_name must be a bare filename — no path separators or '..'"
    # No leading dot (avoids hidden files and ``./...`` styles).
    if name.startswith("."):
        return None, "output_name must not start with '.'"
    # Ensure extension.
    base, ext = os.path.splitext(name)
    if not base:
        return None, "output_name must include a basename before the extension"
    if ext.lower() not in {"", ".xlsx"}:
        return (
            None,
            f"output_name extension {ext!r} not supported; only '.xlsx' is allowed",
        )
    if ext.lower() != ".xlsx":
        name = name + ".xlsx"
    # Whitelist the resulting characters (basename pre-extension only).
    if not _VALID_FILENAME_RE.match(base):
        return (
            None,
            "output_name basename may only contain letters, digits, "
            "underscore, dash, period, and space",
        )
    return name, None


def _error(message: str) -> dict:
    """Build the standard error response. Never raise — return this instead."""
    return {"ok": False, "error": message}


def _validate_sheet_name(name: str) -> tuple[str | None, str | None]:
    """Excel sheet names: ≤31 chars, no ``: \\ / ? * [ ]``.

    Returns ``(safe_name, None)`` on success, ``(None, error)`` otherwise.
    """
    if not isinstance(name, str) or not name.strip():
        return None, "sheet_name must be a non-empty string"
    cleaned = name.strip()
    if len(cleaned) > 31:
        return None, f"sheet_name {cleaned!r} exceeds Excel's 31-char limit"
    if any(c in cleaned for c in r":\/?*[]"):
        return None, (
            f"sheet_name {cleaned!r} contains a character Excel forbids "
            "(: \\ / ? * [ ])"
        )
    return cleaned, None


def _validate_sheet_spec(spec: Any, idx: int) -> tuple[dict | None, str | None]:
    """Validate one sheet spec from the ``sheets`` list. Returns (normalized, err).

    ``spec`` is typed ``Any`` because it arrives via JSON deserialization and
    we can't trust the static type — the isinstance check is load-bearing.
    """
    if not isinstance(spec, dict):
        return None, f"sheets[{idx}] must be a dict"
    raw_paths = spec.get("file_paths")
    if not isinstance(raw_paths, list) or not raw_paths:
        return None, f"sheets[{idx}].file_paths must be a non-empty list"
    sheet_name = spec.get("sheet_name") or spec.get("name") or f"Sheet{idx + 1}"
    safe_name, name_err = _validate_sheet_name(sheet_name)
    if name_err is not None:
        return None, f"sheets[{idx}]: {name_err}"
    sql = spec.get("sql")
    if sql is not None and not isinstance(sql, str):
        return None, f"sheets[{idx}].sql must be a string when present"
    return (
        {"file_paths": raw_paths, "sheet_name": safe_name, "sql": sql},
        None,
    )


def _check_input_paths(file_paths: list[str]) -> str | None:
    """Path-whitelist + extension guard for a single sheet's inputs. None=ok.

    Theme B (2026-05-16): translates legacy ``/mnt/session/outputs/...`` paths
    to the canonical SESSION_OUTPUT_DIR in-place so downstream readers see
    the real on-disk location. Sub-agents may still return the legacy
    prefix in their tool results; this normalizes before the safety check.
    """
    if not isinstance(file_paths, list) or not file_paths:
        return "file_paths must be a non-empty list of absolute paths"
    from artifact_paths import resolve_artifact_path

    for i, raw_path in enumerate(file_paths):
        if not isinstance(raw_path, str) or not raw_path:
            return "every file_paths entry must be a non-empty string"
        translated = resolve_artifact_path(raw_path)
        if not _is_safe_artifact_path(translated):
            log.warning(
                "[MATERIALIZE_XLSX_PATH_REJECTED] path=%s resolved=%s "
                "reason=outside_session_outputs",
                raw_path,
                translated,
            )
            return (
                f"path {raw_path!r} is outside the session output directory "
                f"or traverses a symlink; refusing to read"
            )
        ext = os.path.splitext(translated)[1].lower()
        if ext not in SUPPORTED_INPUT_EXTS:
            return (
                f"unsupported artifact extension {ext!r} for {raw_path!r}; "
                f"supported: {sorted(SUPPORTED_INPUT_EXTS)}"
            )
        if not os.path.isfile(translated):
            return f"artifact file not found: {raw_path!r}"
        # Rewrite in place so the downstream pandas/duckdb read uses the
        # canonical path. Sub-agents who passed the legacy prefix still see
        # their original string echoed back in errors above.
        file_paths[i] = translated
    return None


def _resolve_sheet_dataframe(
    file_paths: list[str],
    sql: str | None,
) -> tuple[Any, int, str | None]:
    """Produce a pandas DataFrame for one sheet. Returns (df, row_count, err).

    No SQL: stream the single Parquet through pyarrow → pandas.
    SQL: open a locked-down DuckDB connection, register every input as an
    in-memory table (``t`` for single input, ``t0``/``t1``/... for multi),
    then execute the SQL. The lockdown / parse-guard pattern matches
    ``artifact_query_tool.query_artifact`` exactly.
    """
    try:
        if sql is None:
            # Direct passthrough: read the (single) artifact into a DataFrame.
            if len(file_paths) != 1:
                return (
                    None,
                    0,
                    "passthrough mode requires exactly one file_paths entry; "
                    "use sql= to combine multiple files",
                )
            path = file_paths[0]
            ext = os.path.splitext(path)[1].lower()
            if ext == ".parquet":
                import pandas as pd

                df = pd.read_parquet(path, engine="pyarrow")
            elif ext == ".csv":
                import pandas as pd

                df = pd.read_csv(path)
            else:  # pragma: no cover — pre-screened by _check_input_paths
                return None, 0, f"unsupported artifact extension {ext!r}"
            return df, int(len(df)), None

        # SQL mode: use DuckDB sandbox.
        # Importing the private helpers from artifact_query_tool keeps a single
        # source of truth for the security-critical SQL parse guard and the
        # table-registration sequence. Underscore-prefixed names are NOT in
        # artifact_query_tool.__all__ on purpose — they're shared with this
        # module only.
        from artifact_query_tool import (  # pyright: ignore[reportMissingImports,reportPrivateUsage]
            _register_table,
            _validate_sql_is_read_only,
        )

        try:
            import duckdb
        except ImportError:  # pragma: no cover — install-time issue
            return (
                None,
                0,
                "duckdb is not installed; add duckdb>=0.10.0 to requirements",
            )

        parse_err = _validate_sql_is_read_only(sql, duckdb)
        if parse_err is not None:
            log.warning(
                "[MATERIALIZE_XLSX_SQL_REJECTED] sql=%r error=%s",
                sql[:200],
                parse_err,
            )
            return None, 0, parse_err

        conn = None
        try:
            conn = duckdb.connect(database=":memory:")
            if len(file_paths) == 1:
                _register_table(conn, "t", file_paths[0])
            else:
                for idx, path in enumerate(file_paths):
                    _register_table(conn, f"t{idx}", path)
            try:
                conn.execute("SET disabled_filesystems = 'LocalFileSystem'")
            except Exception:  # pragma: no cover — older DuckDB lacks this
                pass
            try:
                conn.execute("SET enable_external_access = false")
            except Exception as e:  # pragma: no cover — very old DuckDB
                log.warning(
                    "[MATERIALIZE_XLSX_LOCKDOWN_UNAVAILABLE] error=%s "
                    "(running with parse guard only)",
                    e,
                )
            result_df = conn.execute(sql).df()
        except Exception as e:  # noqa: BLE001 — DuckDB raises a wide variety
            return None, 0, f"sql failed: {e}"
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        return result_df, int(len(result_df)), None
    except Exception as e:  # noqa: BLE001 — last-resort safety net
        log.warning("[MATERIALIZE_XLSX_RESOLVE_FAILED] error=%s", e)
        return None, 0, f"sheet resolution failed: {e}"


def _coerce_cell(val: Any) -> Any:
    """Make ``val`` openpyxl-safe. Strips tz info from datetimes.

    Excel doesn't support tz-aware datetimes (openpyxl raises
    ``ValueError: Excel does not support timezones in datetimes``).
    Salesforce returns UTC across the board, so dropping the tzinfo is
    lossless as long as the operator knows the columns are UTC.
    """
    if val is None:
        return None
    if hasattr(val, "to_pydatetime"):
        dt = val.to_pydatetime()
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        return dt
    if hasattr(val, "tzinfo") and val.tzinfo is not None:
        return val.replace(tzinfo=None)
    return val


def _write_sheet_buffered(ws, df) -> int:
    """Write a small DataFrame to ``ws`` using a buffered pass. Returns rows."""
    ws.append(list(df.columns))
    row_count = 0
    for record in df.to_dict("records"):
        ws.append([_coerce_cell(record.get(col)) for col in df.columns])
        row_count += 1
    return row_count


def _write_sheet_streaming(ws, df, batch_size: int = 2048) -> int:
    """Write a DataFrame in chunks. Returns rows written.

    Kept conservative: pandas DataFrames live in memory either way, but
    iterating ``itertuples`` skips the dict-overhead and keeps peak memory
    lower than ``to_dict('records')`` on wide tables.
    """
    columns = list(df.columns)
    ws.append(columns)
    row_count = 0
    chunk: list[list[Any]] = []
    for row in df.itertuples(index=False, name=None):
        chunk.append([_coerce_cell(v) for v in row])
        if len(chunk) >= batch_size:
            for r in chunk:
                ws.append(r)
            chunk.clear()
        row_count += 1
    for r in chunk:
        ws.append(r)
    return row_count


def materialize_xlsx(
    output_name: str,
    file_paths: list[str] | None = None,
    sql: str | None = None,
    sheet_name: str = "data",
    sheets: list[dict] | None = None,
    output_dir: str | None = None,
) -> dict:
    """Write an .xlsx deliverable from one or more Parquet/CSV artifacts.

    Two call shapes:

    Single-sheet::

        materialize_xlsx(
            output_name="acme_activities_2026-05-14.xlsx",
            file_paths=["/mnt/session/outputs/sf_events_flat_....parquet"],
            sql="SELECT ActivityType, Subject, ActivityDate, OwnerName FROM t",
            sheet_name="Events",
        )

    Multi-sheet::

        materialize_xlsx(
            output_name="call_prep_brief.xlsx",
            sheets=[
                {"sheet_name": "Events",  "file_paths": [...], "sql": "..."},
                {"sheet_name": "Tasks",   "file_paths": [...], "sql": "..."},
                {"sheet_name": "Summary", "file_paths": [...], "sql": "..."},
            ],
        )

    Returns
    -------
    Success::

        {
          "ok": True,
          "file_path": "/mnt/session/outputs/<output_name>",
          "sheets": [{"sheet_name": "Events", "row_count": 207}, ...],
          "total_rows": 1234,
        }

    Failure::

        {"ok": False, "error": "<message>"}
    """
    # ── Output filename guard ────────────────────────────────────────────
    safe_name, name_err = _normalize_output_name(output_name)
    if safe_name is None:
        return _error(name_err or "output_name validation failed")

    # ── Build the list of sheet specs ────────────────────────────────────
    sheet_specs: list[dict] = []
    if sheets is not None:
        if not isinstance(sheets, list) or not sheets:
            return _error("sheets must be a non-empty list when provided")
        if file_paths is not None or sql is not None:
            return _error(
                "pass either single-sheet (file_paths/sql/sheet_name) "
                "or multi-sheet (sheets), not both"
            )
        for idx, raw_spec in enumerate(sheets):
            normalized, err = _validate_sheet_spec(raw_spec, idx)
            if normalized is None:
                return _error(err or f"sheets[{idx}] validation failed")
            sheet_specs.append(normalized)
    else:
        if file_paths is None:
            return _error(
                "must provide either file_paths (single-sheet) or sheets (multi-sheet)"
            )
        safe_sheet, name_err = _validate_sheet_name(sheet_name)
        if safe_sheet is None:
            return _error(name_err or "sheet_name validation failed")
        sheet_specs.append(
            {"file_paths": file_paths, "sheet_name": safe_sheet, "sql": sql}
        )

    # ── Path-whitelist every input across all sheets ─────────────────────
    for idx, spec in enumerate(sheet_specs):
        err = _check_input_paths(spec["file_paths"])
        if err is not None:
            return _error(f"sheets[{idx}]: {err}" if sheets is not None else err)

    # Resolve each sheet's DataFrame.
    resolved: list[tuple[str, Any, int]] = []
    for idx, spec in enumerate(sheet_specs):
        df, row_count, err = _resolve_sheet_dataframe(
            spec["file_paths"], spec.get("sql")
        )
        if err is not None:
            return _error(
                f"sheets[{idx}] ({spec['sheet_name']}): {err}"
                if sheets is not None
                else err
            )
        resolved.append((spec["sheet_name"], df, row_count))

    # ── Determine destination path ───────────────────────────────────────
    if output_dir is None:
        output_dir = _session_output_dir()
    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(output_dir, safe_name)

    # ── Write the workbook ───────────────────────────────────────────────
    try:
        from openpyxl import Workbook
    except ImportError:  # pragma: no cover — install-time issue
        return _error("openpyxl is not installed; add openpyxl to requirements")

    wb = Workbook(write_only=True)
    summary: list[dict] = []
    total = 0
    try:
        for sheet_name, df, row_count in resolved:
            ws = wb.create_sheet(title=sheet_name)
            if row_count <= PANDAS_BUFFER_ROW_LIMIT:
                written = _write_sheet_buffered(ws, df)
            else:
                written = _write_sheet_streaming(ws, df)
            summary.append({"sheet_name": sheet_name, "row_count": written})
            total += written
        wb.save(out_path)
    except Exception as e:  # noqa: BLE001 — write_only is fragile on odd cell types
        log.warning("[MATERIALIZE_XLSX_WRITE_FAILED] out=%s error=%s", out_path, e)
        try:
            if os.path.exists(out_path):
                os.remove(out_path)
        except Exception:
            pass
        return _error(f"workbook write failed: {e}")

    log.info(
        "[MATERIALIZE_XLSX] wrote %s sheets=%d total_rows=%d path=%s",
        safe_name,
        len(summary),
        total,
        out_path,
    )
    return {
        "ok": True,
        "file_path": out_path,
        "sheets": summary,
        "total_rows": total,
    }
