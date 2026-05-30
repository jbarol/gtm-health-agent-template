"""Streaming result virtualization for large tool outputs.

Why this exists
---------------
A Coordinator that pulls 3,209 Leads through MCP and tries to reason over the
full JSON in context will burn through the 1M input-token-per-turn cap before
posting any answer (see incident `sesn_EXAMPLE`, 2026-05-12 —
5.9M cached tokens, $13.87 for one session, zero deliverable).

The fix is to keep big results out of the model's context entirely. The
orchestrator wraps every list-shaped tool result above a threshold:

  - The full rows stream to disk as an .xlsx via openpyxl's write-only mode.
  - The model receives a compact handle: row count, schema, the first 10
    rows as a preview, per-column summary stats, and the file path.
  - The model reasons about the preview + stats. For per-row work it uses
    the Python tool against ``file_path``. To deliver the data to the user
    it attaches ``file_path`` to ``post_report.payload.attachments``.

Design rules
------------
1. Pure module. No Slack, no Anthropic SDK, no logging side effects. The
   caller decides what to log.
2. Streaming writes. Use openpyxl write-only mode so memory usage stays
   bounded for arbitrarily large lists. We must support 50K+ row pulls.
3. Stdlib stats only. No numpy / pandas — the orchestrator already has a
   bloated dependency surface and per-column summaries don't need either.
4. Deterministic shape. Every virtualized result follows the same dict
   shape (row_count, preview, summary_stats, file_path, schema, next_steps)
   so the Coordinator prompt has a single contract to encode.
5. Zero-row safety. An empty list returns a complete shape with no file
   write — never spawn an empty .xlsx that confuses the renderer.
"""

from __future__ import annotations

import json
import os
import statistics
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any


__all__ = [
    "compute_summary_stats",
    "virtualize_result",
    "write_xlsx_streaming",
]


# How many preview rows the model sees in-context. 10 is the documented
# contract — anything past row 10 lives on disk. Kept as a constant so the
# Coordinator prompt and this module stay in lock-step.
PREVIEW_ROW_COUNT = 10

# Top-N most frequent values reported for low-cardinality string columns.
TOP_VALUES_LIMIT = 5

# A column is "low cardinality" iff its distinct value count is at or below
# this number. 20 is a heuristic; it covers Stage, Type, Status, and most
# Salesforce picklists while excluding identifier-like columns (Id, Email).
LOW_CARDINALITY_MAX = 20


def write_xlsx_streaming(
    rows: list[dict],
    output_path: str,
    sheet_name: str = "Results",
) -> None:
    """Stream a list of row dicts to an .xlsx file using openpyxl write-only mode.

    Headers come from the FIRST row's keys, preserving insertion order. Rows
    with extra keys are written column-positionally against that header set —
    extra keys are dropped, missing keys become blank cells. This matches how
    a SOQL result with optional columns gets serialized.

    Use write-only mode so memory usage stays O(1) in row count: each row
    is flushed to disk after writing instead of accumulating in a Worksheet
    object. Tested up to 10K rows in <5 seconds with negligible RSS.

    No return value — the file at ``output_path`` is the side effect.
    """
    # Lazy import — openpyxl is heavy and not every code path needs it.
    # Importing here keeps module import-time light for tests that only
    # exercise compute_summary_stats.
    from openpyxl import Workbook

    wb = Workbook(write_only=True)
    ws = wb.create_sheet(title=sheet_name[:31])  # Excel sheet name cap is 31

    if not rows:
        # Caller should not invoke this with [] — virtualize_result short-
        # circuits before us — but make it safe anyway. Write an empty sheet
        # so the file is at least valid.
        wb.save(output_path)
        return

    headers = list(rows[0].keys())
    ws.append(headers)

    for row in rows:
        ws.append([row.get(h, "") for h in headers])

    wb.save(output_path)


def _classify_dtype(values: list[Any]) -> str:
    """Best-effort dtype label for a column. Cheap, no numpy.

    Returns one of: "int", "float", "bool", "str", "null". A column whose
    non-null values are all numbers labels "int" or "float"; mixed types
    fall back to "str". An all-null column is "null".
    """
    non_null = [v for v in values if v is not None and v != ""]
    if not non_null:
        return "null"
    if all(isinstance(v, bool) for v in non_null):
        return "bool"
    # bool is a subclass of int, so check bool first.
    if all(isinstance(v, int) and not isinstance(v, bool) for v in non_null):
        return "int"
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in non_null):
        return "float"
    return "str"


def _numeric_summary(values: list[Any]) -> dict:
    """min / max / mean / count for a numeric column. None-values dropped."""
    nums = [
        v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)
    ]
    if not nums:
        return {"min": None, "max": None, "mean": None, "count": 0}
    return {
        "min": min(nums),
        "max": max(nums),
        # statistics.fmean is faster than mean and returns a float reliably.
        "mean": round(statistics.fmean(nums), 4),
        "count": len(nums),
    }


def _hashable_summary_value(v: Any) -> Any:
    """Coerce nested dicts / lists into a hashable JSON string for stats.

    Salesforce relationship fields like ``Owner.Name`` or ``RecordType.Name``
    arrive as nested ``OrderedDict``s in the raw REST response (simple_salesforce
    + the SF MCP both surface them this way). The stats layer uses dict keys
    and set membership for top-values / cardinality counts; both require
    hashable elements. Without coercion every Opportunity dump crashed with
    ``unhashable type: 'collections.OrderedDict'``. Live repro 2026-05-14
    19:43 PT (session sesn_EXAMPLE): 3+ failures on
    Opportunity rows whose ``Owner`` / ``RecordType`` columns were nested.

    Returns a deterministic JSON string for any Mapping / list / tuple / set
    so identical nested values still collapse to one bucket. Scalars pass
    through untouched. ``default=str`` covers ``Decimal``, ``date``, and other
    non-JSON-native scalars that occasionally appear in SF responses.
    """
    if isinstance(v, Mapping):
        return json.dumps(v, sort_keys=True, default=str)
    if isinstance(v, (list, tuple, set)):
        return json.dumps(list(v), sort_keys=True, default=str)
    return v


def _top_values(values: list[Any], limit: int = TOP_VALUES_LIMIT) -> list[dict]:
    """Top-N most frequent non-null values for a low-cardinality string column."""
    counts: dict[Any, int] = {}
    for v in values:
        if v is None or v == "":
            continue
        v = _hashable_summary_value(v)
        counts[v] = counts.get(v, 0) + 1
    # Sort by count desc, then by value asc for deterministic output.
    items = sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0])))[:limit]
    return [{"value": k, "count": c} for k, c in items]


def compute_summary_stats(rows: list[dict]) -> dict:
    """Per-column summary stats for a list of row dicts.

    For each column the model gets:
      - ``dtype``: int | float | bool | str | null
      - ``null_count``: number of None / empty-string cells

    Numeric columns (int / float) add ``min``, ``max``, ``mean``, ``count``.
    Low-cardinality string columns (<= LOW_CARDINALITY_MAX distinct) add
    ``top_values`` (a list of {value, count} for the top 5).
    High-cardinality string columns just get ``null_count`` + ``unique_count`` —
    enough for the model to know "this is an id-like column, don't summarize."

    Empty input returns ``{}`` so callers can pass [] without a guard.
    """
    if not rows:
        return {}

    # Materialize the column → values map once. Each column gets the cell from
    # every row even when the row dict is missing the key (treated as None).
    columns = list(rows[0].keys())
    by_col: dict[str, list[Any]] = {c: [] for c in columns}
    for row in rows:
        for c in columns:
            by_col[c].append(row.get(c))

    out: dict[str, dict] = {}
    for col, vals in by_col.items():
        dtype = _classify_dtype(vals)
        null_count = sum(1 for v in vals if v is None or v == "")
        col_stats: dict[str, Any] = {"dtype": dtype, "null_count": null_count}

        if dtype in ("int", "float"):
            col_stats.update(_numeric_summary(vals))
        elif dtype == "str":
            # Distinct count drives the cardinality split. Coerce nested SF
            # dicts (Owner, RecordType) into JSON strings so set membership
            # works — see ``_hashable_summary_value``.
            distinct = {
                _hashable_summary_value(v) for v in vals if v is not None and v != ""
            }
            unique_count = len(distinct)
            if unique_count <= LOW_CARDINALITY_MAX:
                col_stats["top_values"] = _top_values(vals)
            else:
                col_stats["unique_count"] = unique_count
        elif dtype == "bool":
            true_count = sum(1 for v in vals if v is True)
            false_count = sum(1 for v in vals if v is False)
            col_stats["true_count"] = true_count
            col_stats["false_count"] = false_count

        out[col] = col_stats

    return out


def _schema_from_summary(summary: dict) -> dict:
    """Flatten ``{col: {dtype, ...}}`` to ``{col: dtype}`` for the schema field."""
    return {col: stats.get("dtype", "str") for col, stats in summary.items()}


def _safe_filename_component(s: str) -> str:
    """Sanitize a tool name so it's safe to use as a path component.

    Strips anything that isn't alphanumeric, underscore, or hyphen. Keeps the
    output short — long tool names produce unwieldy file names.
    """
    keep = []
    for ch in s:
        if ch.isalnum() or ch in ("_", "-"):
            keep.append(ch)
    out = "".join(keep) or "result"
    return out[:50]


def virtualize_result(
    rows: list[dict],
    tool_name: str,
    output_dir: str = "",
) -> dict:
    """Stream rows to disk and return a compact handle the model can reason about.

    The returned shape is documented in the Coordinator's virtualization
    contract — the model is told exactly what these keys mean and what to do
    with each one.

    Zero-row input returns a complete shape with ``row_count=0`` and
    ``file_path=None`` — no file write, no openpyxl import. The model gets
    a clear "nothing to deliver" signal.

    Args:
        rows: list of row dicts (e.g. from ``soqlQuery``'s ``records`` field).
        tool_name: the tool that produced these rows. Used in the file name.
        output_dir: directory for the .xlsx file. Defaults to the Managed
            Agents session output mount (``/mnt/session/outputs``) so the
            file is reachable by both the orchestrator and the Python tool.
            Created on demand if missing.
    """
    if not rows:
        return {
            "row_count": 0,
            "preview": [],
            "summary_stats": {},
            "file_path": None,
            "schema": {},
            "next_steps": (
                "Result virtualized but contained zero rows. There is nothing "
                "to deliver. Report the empty result back to the user in plain "
                "language."
            ),
        }

    if not output_dir:
        # Theme B (2026-05-16): honor SESSION_OUTPUT_DIR (Railway Volume in
        # prod) so the canonical store survives Anthropic's sandbox TTL on
        # /mnt/session/outputs. Lazy import to keep result_virtualize free
        # of cross-module dependencies at import time.
        from artifact_paths import session_output_dir

        output_dir = session_output_dir()
    os.makedirs(output_dir, exist_ok=True)
    # Microsecond-precision UTC timestamp plus a 4-char random hex suffix.
    # Two large queries from the same tool within the same second used to
    # collide on a whole-second timestamp and silently overwrite each other
    # (codex review PR #99, comment 3223912577). Microseconds reduce the
    # collision probability to near-zero; the random suffix makes it
    # astronomically unlikely even under adversarial clock skew or
    # virtualized-clock test fixtures.
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    suffix = uuid.uuid4().hex[:4]
    fname = f"{_safe_filename_component(tool_name)}_{ts}_{suffix}.xlsx"
    file_path = os.path.join(output_dir, fname)

    write_xlsx_streaming(
        rows, file_path, sheet_name=_safe_filename_component(tool_name)
    )

    summary_stats = compute_summary_stats(rows)
    schema = _schema_from_summary(summary_stats)
    preview = rows[:PREVIEW_ROW_COUNT]

    return {
        "row_count": len(rows),
        "preview": preview,
        "summary_stats": summary_stats,
        "file_path": file_path,
        "schema": schema,
        "next_steps": (
            "Result virtualized. Reason about preview + stats; use the Python "
            "tool against file_path for per-row work; attach file_path to "
            "post_report when the user needs the data."
        ),
    }
