"""Run DuckDB SQL against previously-materialized artifact files.

Why this exists
---------------
Track G's ``dump_sf_query`` moves Salesforce data to disk as Parquet without
ever holding rows in agent context. Track H — this module — gives sub-agents
the matching analytical capability: SQL against those materialized files, and
against any other Parquet/CSV produced by ``db_query`` virtualization or a
prior ``query_artifact`` call.

Together they form the data-movement contract laid out in Iteration 2 of
the misty-squishing-badger plan: every byte of raw or aggregated data lives
on Railway disk; the agents only ever see handles (file_path + summary).

Design rules
------------
1. Path-whitelist FIRST. Every input path must resolve inside the canonical
   ``SESSION_OUTPUT_DIR`` (default ``/mnt/session/outputs``). Symlinks and
   ``..`` escape attempts are rejected. The check mirrors Track B's
   ``_is_safe_attachment_path`` from ``session_runner`` — we re-implement
   locally to keep the module import-cycle-free and unit-testable in
   isolation.
2. SQL is read-only and sandboxed. The path whitelist on ``file_paths`` is
   not enough on its own: DuckDB SQL can call ``read_csv_auto`` /
   ``read_parquet`` / ``read_text`` / ``read_blob`` on arbitrary host paths
   directly from the query body. To close that exfiltration vector we
   (a) materialize whitelisted files into in-memory tables via
   ``CREATE TABLE AS SELECT *``, then (b) flip
   ``SET enable_external_access = false`` on the connection before
   running user SQL, and (c) reject non-SELECT/EXPLAIN statements
   (and ``PRAGMA`` / ``SET`` / ``COPY`` / ``ATTACH`` / ``LOAD`` / etc.)
   in a parse guard. The order matters: register first, lock down second,
   run user SQL third. See ``_validate_sql_is_read_only`` and the lockdown
   block in ``query_artifact``. P1 security fix per PR #101 codex review.
3. Never raise. Bad SQL, bad paths, missing files all return an error dict.
   The agent reads the dict and reacts; an unhandled exception would force
   the orchestrator into the catastrophic-failure path.
4. Inline-or-virtualize. Results ≤ ``inline_threshold`` (50 by default)
   come back as rows inside the response. Bigger results stream to a new
   Parquet file under the session output dir and the response carries a
   file handle + preview + summary stats — same contract as
   ``result_virtualize.virtualize_result``.
5. Single-file queries reference the table as ``t``. Multi-file queries
   reference tables in array order as ``t0``, ``t1``, ``t2``, etc. The
   Coordinator prompt instructs sub-agents on this convention.
6. XLSX inputs are not supported at this layer. DuckDB doesn't natively
   read xlsx; loading via openpyxl + DataFrame defeats the
   keep-data-out-of-context principle. The tool returns a clear error
   suggesting the caller materialize to Parquet via ``dump_sf_query`` or
   convert manually.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any


__all__ = ["query_artifact"]


log = logging.getLogger(__name__)


# Default inline-vs-virtualize boundary. The Coordinator prompt's data
# contract documents the 50-row threshold; keep it in lock-step.
DEFAULT_INLINE_THRESHOLD = 50

# Preview rows attached to a virtualized response — mirrors
# ``result_virtualize.PREVIEW_ROW_COUNT`` so the model sees the same shape
# regardless of which path produced the handle.
PREVIEW_ROW_COUNT = 10

# Extensions the tool can read directly. xlsx is intentionally rejected.
SUPPORTED_INPUT_EXTS = {".parquet", ".csv"}

# SQL statement types we permit. DuckDB normalizes ``WITH ... SELECT`` to
# ``SELECT``, so the allowlist only needs the two read-only kinds. Everything
# else — INSERT, UPDATE, DELETE, DROP, CREATE, ALTER, COPY, ATTACH, LOAD,
# PRAGMA, SET, INSTALL — is rejected before we ever touch the connection.
_ALLOWED_STATEMENT_TYPE_NAMES = {"SELECT", "EXPLAIN"}

# Leading-keyword denylist. DuckDB classifies some statements (notably
# ``PRAGMA show_tables``) as SELECT, so we belt-and-brace against keywords
# that can mutate state or trigger filesystem access regardless of how the
# parser labels them.
_FORBIDDEN_LEADING_KEYWORDS = (
    "PRAGMA",
    "ATTACH",
    "DETACH",
    "SET",
    "COPY",
    "LOAD",
    "INSTALL",
    "IMPORT",
    "EXPORT",
    "CALL",
)
_LEADING_KEYWORD_RE = re.compile(r"^\s*([A-Za-z_]+)")


def _session_output_dir() -> str:
    """Resolve the canonical session output directory.

    Mirrors ``session_runner._session_output_dir`` but re-implemented here
    so this module has no dependency on ``session_runner`` (avoids a circular
    import when ``session_runner._dispatch_tool`` later imports this module
    to dispatch the custom tool).
    """
    raw = os.environ.get("SESSION_OUTPUT_DIR") or "/mnt/session/outputs"
    return os.path.realpath(raw)


def _is_safe_artifact_path(path: str) -> bool:
    """Return True iff ``path`` is safe to read as an artifact.

    Logic mirrors ``session_runner._is_safe_attachment_path``: resolve to a
    canonical absolute path, refuse if any segment is a symlink, and require
    the canonical path to live under the session output directory.

    The model controls these paths (they come back from a previous
    ``dump_sf_query`` or ``query_artifact`` call), so a prompt-injection
    could in theory induce it to feed ``/etc/passwd``. Whitelisting to the
    output mount blocks that vector at the orchestrator layer.
    """
    if not path or not isinstance(path, str):
        return False

    safe_root = _session_output_dir()
    abs_input = os.path.abspath(path)

    # Walk every parent segment; reject if any link points away.
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


def _error(message: str) -> dict:
    """Build the standard error response. Never raise — return this instead."""
    return {
        "error": message,
        "row_count": 0,
        "rows": [],
        "inline": True,
    }


def _quote_sql_path(path: str) -> str:
    """Single-quote a path for DuckDB's ``read_parquet`` / ``read_csv_auto``.

    DuckDB SQL strings use a single quote with backslash escapes; we double
    any embedded single quote to be safe. Paths from the session output dir
    should never contain quotes, but defense in depth is cheap.
    """
    return "'" + path.replace("'", "''") + "'"


def _schema_from_df(df) -> dict:
    """Return ``{col: dtype_str}`` for a pandas DataFrame.

    Cast each dtype to ``str`` so the response is JSON-serializable. Dtypes
    like ``datetime64[ns, UTC]`` come back as strings, which is what the
    agent needs anyway.
    """
    return {col: str(dtype) for col, dtype in df.dtypes.items()}


def _compute_summary(df) -> dict:
    """Compute a compact per-column summary for the response handle.

    Prefer ``result_virtualize.compute_summary_stats`` if importable so the
    handle shape matches the one ``db_query`` and ``dump_sf_query`` emit.
    Fall back to a pandas-based summary if the import fails — in either case
    callers should not depend on the exact key set beyond ``dtype`` /
    ``null_count``.
    """
    rows = (
        df.to_dict("records") if len(df) <= 5000 else df.head(5000).to_dict("records")
    )
    try:
        from result_virtualize import compute_summary_stats

        return compute_summary_stats(rows)
    except Exception:  # pragma: no cover — defensive fallback
        out: dict[str, Any] = {}
        for col in df.columns:
            series = df[col]
            entry: dict[str, Any] = {
                "dtype": str(series.dtype),
                "null_count": int(series.isna().sum()),
            }
            out[col] = entry
        return out


def _register_table(conn, alias: str, path: str) -> None:
    """Materialize ``path`` into an in-memory DuckDB table at ``alias``.

    Sniffs the extension and dispatches to the right DuckDB reader. The
    function never raises — caller is expected to wrap the SQL in try/except
    too — but a syntactically bad path will surface as a DuckDB error and
    bubble up to the calling try/except.

    Why ``CREATE TABLE AS SELECT *`` and not ``CREATE OR REPLACE VIEW``:
    after registration we flip ``enable_external_access = false`` to block
    file-reading table functions in the user-supplied SQL (P1 security fix,
    PR #101 codex review). Views hold a lazy reference to ``read_parquet`` /
    ``read_csv_auto``, so a locked-down session re-executing the view would
    raise PermissionException at query time. Materializing the rows into
    an in-memory table at registration sidesteps that — the table is just
    in-memory pages once the SET fires, with no further filesystem touch.
    """
    ext = os.path.splitext(path)[1].lower()
    quoted = _quote_sql_path(path)
    if ext == ".parquet":
        conn.execute(f"CREATE TABLE {alias} AS SELECT * FROM read_parquet({quoted})")
    elif ext == ".csv":
        conn.execute(f"CREATE TABLE {alias} AS SELECT * FROM read_csv_auto({quoted})")
    else:
        # Should have been screened by the caller, but raise so the
        # surrounding try/except returns a clean error dict.
        raise ValueError(
            f"Unsupported artifact extension {ext!r}; supported: {sorted(SUPPORTED_INPUT_EXTS)}"
        )


def _validate_sql_is_read_only(sql: str, duckdb_mod) -> str | None:
    """Return ``None`` if ``sql`` is read-only, else an error message.

    Two-layer defense against SQL-body exfiltration (P1 security fix,
    PR #101 codex review):

    1. **Leading-keyword denylist.** Reject any statement that starts with
       ``PRAGMA`` / ``ATTACH`` / ``SET`` / ``COPY`` / ``LOAD`` / ``INSTALL``
       / ``IMPORT`` / ``EXPORT`` / ``CALL``. DuckDB classifies some of these
       (notably ``PRAGMA show_tables``) as ``StatementType.SELECT``, so the
       type allowlist alone is insufficient.
    2. **Type allowlist via ``extract_statements``.** Every parsed statement
       must be ``SELECT`` or ``EXPLAIN``. ``WITH ... SELECT`` normalizes to
       ``SELECT``. INSERT / UPDATE / DELETE / DROP / CREATE / ALTER / COPY
       / ATTACH all surface their own types and are rejected.

    Parse failures here also short-circuit with a clear error before the
    SQL reaches the connection. The real load-bearing defense is
    ``SET enable_external_access = false`` applied to the connection AFTER
    table registration — see ``query_artifact`` — but this guard catches
    obvious abuse with a friendlier message.
    """
    if not isinstance(sql, str):
        return "sql must be a string"

    # Leading-keyword guard runs first because it doesn't need the parser.
    # It catches statements DuckDB parses as SELECT but that still touch
    # configuration or trigger filesystem access (PRAGMA, SET, COPY, …).
    for raw_stmt in sql.split(";"):
        stripped = raw_stmt.strip()
        if not stripped:
            continue
        m = _LEADING_KEYWORD_RE.match(stripped)
        if not m:
            continue
        leading = m.group(1).upper()
        if leading in _FORBIDDEN_LEADING_KEYWORDS:
            return (
                f"only SELECT/WITH/EXPLAIN allowed; got {leading} "
                f"(read-only queries against pre-registered tables only)"
            )

    # Type allowlist via the parser.
    try:
        statements = duckdb_mod.extract_statements(sql)
    except Exception as e:  # noqa: BLE001 — parse error surface
        return f"SQL parse error: {e}"

    if not statements:
        return "sql produced no statements"

    for stmt in statements:
        stmt_type = getattr(stmt, "type", None)
        # ``stmt.type`` is a ``duckdb.StatementType`` enum; its ``.name`` is
        # the bare token (e.g. ``"SELECT"``). Fall back to ``str(stmt_type)``
        # if ``.name`` is unavailable on some future SDK build.
        type_name = (
            getattr(stmt_type, "name", None) or str(stmt_type).rsplit(".", 1)[-1]
        )
        if type_name not in _ALLOWED_STATEMENT_TYPE_NAMES:
            return (
                f"only SELECT/WITH/EXPLAIN allowed; got {type_name} "
                f"(read-only queries against pre-registered tables only)"
            )

    return None


def _safe_output_name(raw: str) -> str | None:
    """Sanitize a caller-supplied output_name. Returns None if rejected.

    Design #16 (2026-05-15). Allow ``[A-Za-z0-9._-]+`` only — no path
    separators, no leading dots, max 80 chars. Strip a trailing
    ``.parquet`` so the caller can pass either form. Reject anything
    that would let a sub-agent write outside the output dir.
    """
    if not isinstance(raw, str) or not raw:
        return None
    raw = raw.strip()
    if raw.endswith(".parquet"):
        raw = raw[: -len(".parquet")]
    if not raw or raw.startswith(".") or len(raw) > 80:
        return None
    import re

    if not re.fullmatch(r"[A-Za-z0-9._-]+", raw):
        return None
    return raw + ".parquet"


def _virtualize_query_result(
    df, output_dir: str, output_name: str | None = None
) -> dict:
    """Stream a >threshold result to Parquet and return a compact handle.

    Naming mirrors Track B's convention so an operator scanning
    ``/mnt/session/outputs`` can tell at a glance which tool produced which
    file: ``qa_<utc-iso-compact>_<uuid4[:4]>.parquet``.

    Design #16 (2026-05-15): when ``output_name`` is supplied and passes
    sanitization, use it as the basename instead of the auto-generated
    qa_<ts>_<uuid> name. The Coordinator now controls semantic naming so
    downstream consumers can find the file by purpose rather than tracking
    random tool handles. Failed sanitization falls back to the auto name.
    """
    os.makedirs(output_dir, exist_ok=True)
    safe_name = _safe_output_name(output_name) if output_name else None
    if safe_name:
        fname = safe_name
    else:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        suffix = uuid.uuid4().hex[:4]
        fname = f"qa_{ts}_{suffix}.parquet"
    file_path = os.path.join(output_dir, fname)

    # pyarrow engine keeps the dependency surface aligned with Track G,
    # which writes Parquet via pyarrow directly. We accept the default
    # compression (snappy) so files stay readable by any pyarrow consumer.
    df.to_parquet(file_path, engine="pyarrow", index=False)

    # Write a sibling .xlsx so the post_report attachment layer can swap
    # Parquet → xlsx before Slack upload. The model keeps reasoning about
    # the Parquet handle; the human gets a file they can actually open in
    # Excel. Silent fallback on failure — Parquet path must stay intact.
    try:
        from xlsx_export import parquet_to_xlsx_sibling

        parquet_to_xlsx_sibling(file_path)
    except Exception:
        log.debug("xlsx export side-effect skipped (non-fatal)")

    return {
        "row_count": int(len(df)),
        "file_path": file_path,
        "schema": _schema_from_df(df),
        "preview_10": df.head(PREVIEW_ROW_COUNT).to_dict("records"),
        "summary_stats": _compute_summary(df),
        "inline": False,
    }


def query_artifact(
    file_paths: list[str],
    sql: str,
    output_dir: str | None = None,
    inline_threshold: int = DEFAULT_INLINE_THRESHOLD,
    output_name: str | None = None,
) -> dict:
    """Run a DuckDB SQL query against one or more materialized artifact files.

    Args:
        file_paths: absolute paths to Parquet/CSV files inside the session
            output directory. Single-file queries reference the table as
            ``t``; multi-file queries use ``t0``, ``t1``, ... in array order.
        sql: the DuckDB SQL to run. Standard DuckDB dialect; the registered
            views are read-only.
        output_dir: target directory for virtualized result files. Defaults
            to ``SESSION_OUTPUT_DIR`` (env) then ``/mnt/session/outputs``.
        inline_threshold: result row counts at or below this return inline;
            larger results are virtualized to a new Parquet file.

    Returns:
        Inline shape (row_count ≤ threshold)::

            {
              "row_count": N,
              "rows": [{...}, ...],
              "schema": {col: dtype},
              "inline": True,
            }

        Virtualized shape (row_count > threshold)::

            {
              "row_count": N,
              "file_path": "/mnt/session/outputs/qa_<ts>_<uuid>.parquet",
              "schema": {col: dtype},
              "preview_10": [{...}, ...],
              "summary_stats": {...},
              "inline": False,
            }

        Error shape (any failure)::

            {
              "error": "<message>",
              "row_count": 0,
              "rows": [],
              "inline": True,
            }
    """
    # ── Pre-flight validation ────────────────────────────────────────────
    if not isinstance(file_paths, list) or not file_paths:
        return _error("file_paths must be a non-empty list of absolute paths")
    if not isinstance(sql, str) or not sql.strip():
        return _error("sql must be a non-empty string")

    # Path-whitelist BEFORE any I/O. Reject xlsx explicitly with a hint —
    # the model can re-pull as Parquet via dump_sf_query.
    # Theme B (2026-05-16): translate legacy ``/mnt/session/outputs/...``
    # paths to the canonical SESSION_OUTPUT_DIR before the safety check.
    # See artifact_paths.py for the rationale.
    from artifact_paths import resolve_artifact_path

    resolved_paths: list[str] = []
    for raw_path in file_paths:
        if not isinstance(raw_path, str) or not raw_path:
            return _error("every file_paths entry must be a non-empty string")
        translated = resolve_artifact_path(raw_path)
        if not _is_safe_artifact_path(translated):
            log.warning(
                "[QUERY_ARTIFACT_PATH_REJECTED] path=%s resolved=%s reason=outside_session_outputs",
                raw_path,
                translated,
            )
            return _error(
                f"path {raw_path!r} is outside the session output directory "
                f"or traverses a symlink; refusing to read"
            )
        ext = os.path.splitext(translated)[1].lower()
        if ext == ".xlsx":
            return _error(
                f"xlsx input is not supported. Re-materialize {raw_path!r} as "
                f".parquet (via dump_sf_query) or convert before querying."
            )
        if ext not in SUPPORTED_INPUT_EXTS:
            return _error(
                f"unsupported artifact extension {ext!r} for {raw_path!r}; "
                f"supported: {sorted(SUPPORTED_INPUT_EXTS)}"
            )
        if not os.path.isfile(translated):
            return _error(f"artifact file not found: {raw_path!r}")
        resolved_paths.append(translated)
    # From this point on use the resolved (canonical) paths for I/O so
    # DuckDB sees the actual file location, not the agent-facing alias.
    file_paths = resolved_paths

    if output_dir is None:
        output_dir = _session_output_dir()

    # ── Lazy import the heavy deps ───────────────────────────────────────
    try:
        import duckdb
    except ImportError:  # pragma: no cover — install-time issue
        return _error("duckdb is not installed; add duckdb>=0.10.0 to requirements")

    # ── SQL parse guard ─────────────────────────────────────────────────
    # Reject non-read-only statements (INSERT/UPDATE/DELETE/DROP/PRAGMA/SET
    # /COPY/ATTACH/LOAD/...) before opening the connection. This is layer 2
    # of the security model — layer 1 is ``SET enable_external_access =
    # false`` below — and exists to give the agent a clear error message
    # rather than a generic DuckDB exception.
    parse_err = _validate_sql_is_read_only(sql, duckdb)
    if parse_err is not None:
        log.warning(
            "[QUERY_ARTIFACT_SQL_REJECTED] sql=%r error=%s", sql[:200], parse_err
        )
        return _error(parse_err)

    # ── Run the SQL ──────────────────────────────────────────────────────
    conn = None
    try:
        conn = duckdb.connect(database=":memory:")

        # Step 1: register whitelisted file_paths as IN-MEMORY tables. This
        # MUST happen before the lockdown — once external access is
        # disabled, even our own ``read_parquet`` / ``read_csv_auto`` calls
        # are blocked.
        if len(file_paths) == 1:
            _register_table(conn, "t", file_paths[0])
        else:
            for idx, path in enumerate(file_paths):
                _register_table(conn, f"t{idx}", path)

        # Step 2: lock the connection down — this is the load-bearing
        # security control. The path whitelist on ``file_paths`` is
        # insufficient on its own because DuckDB SQL can call
        # ``read_csv_auto`` / ``read_parquet`` / ``read_text`` /
        # ``read_blob`` on arbitrary host paths directly from the query
        # body, bypassing every Python-side check. Setting
        # ``enable_external_access = false`` here makes those table
        # functions raise PermissionException at execution time, regardless
        # of the model's input. The pre-registered ``t`` / ``t0`` / ... in-
        # memory tables remain queryable because the data already lives in
        # the in-memory DB. ``SET disabled_filesystems = 'LocalFileSystem'``
        # is the older API; we attempt it first as belt-and-brace, then
        # apply the modern flag.
        try:
            conn.execute("SET disabled_filesystems = 'LocalFileSystem'")
        except Exception:  # pragma: no cover — older DuckDB lacks this
            pass
        try:
            conn.execute("SET enable_external_access = false")
        except Exception as e:  # pragma: no cover — very old DuckDB
            # If neither lockdown setting is available, fall through to
            # layer 2 (parse guard) only. Log loudly so we notice.
            log.warning(
                "[QUERY_ARTIFACT_LOCKDOWN_UNAVAILABLE] error=%s "
                "(running with parse guard only — upgrade duckdb>=0.10.0)",
                e,
            )

        # Step 3: run the user SQL. External file reads now fail with a
        # PermissionException; the surrounding try/except converts that
        # into an error dict, matching the malformed-SQL contract.
        result_df = conn.execute(sql).df()
    except Exception as e:  # noqa: BLE001 — DuckDB raises a wide variety
        log.warning("[QUERY_ARTIFACT_SQL_FAILED] sql=%r error=%s", sql[:200], e)
        return _error(f"query failed: {e}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    # ── Branch on size ───────────────────────────────────────────────────
    row_count = int(len(result_df))
    if row_count <= inline_threshold:
        return {
            "row_count": row_count,
            "rows": result_df.to_dict("records"),
            "schema": _schema_from_df(result_df),
            "inline": True,
        }

    try:
        return _virtualize_query_result(result_df, output_dir, output_name=output_name)
    except Exception as e:  # noqa: BLE001 — write failure should not crash the agent
        log.warning("[QUERY_ARTIFACT_VIRTUALIZE_FAILED] error=%s", e)
        return _error(f"result virtualization failed: {e}")
