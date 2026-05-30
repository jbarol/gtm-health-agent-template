"""Write a sibling .xlsx next to a freshly-materialized Parquet file.

The orchestrator's two materialization paths (``sf_dump_tool.dump_sf_query``
and ``artifact_query_tool._virtualize_query_result``) write Parquet because
DuckDB queries it much faster than xlsx and the model only ever sees a
compact handle. Slack users need xlsx — Parquet is unreadable without
pyarrow/DuckDB tooling.

Plan #37 follow-up (2026-05-13): write the xlsx sibling at materialization
time so the post_report attachment layer can swap Parquet → xlsx as the
last step before Slack upload. No model-prompt changes needed; the agent
keeps reasoning about Parquet paths, the orchestrator quietly delivers the
xlsx the human can actually open.

Design notes:

  * **Streaming write via ``openpyxl write_only``.** A 191K-row Lead dump
    is ~200 MB in memory if loaded fully via DataFrame, but only ~30 MB
    streamed cell-by-cell from pyarrow. Memory headroom matters because
    Railway containers cap at 512 MB and a single Coordinator session
    can hold multiple in-flight artifacts.

  * **Column ordering preserved.** The xlsx column order matches the
    Parquet schema order so an operator reading the file expects the
    same shape as the model's preview.

  * **Datetime cells written as Python datetime, not pyarrow Timestamp.**
    openpyxl auto-formats Python datetimes as Excel datetime cells but
    chokes on pyarrow Timestamp objects.

  * **No date conversion on date-only columns.** SF returns these as
    ISO strings already; reformatting risks losing timezone semantics.

  * **Silent fallback on any error.** xlsx delivery is a UX nice-to-have;
    failure must not break the Parquet path the model depends on.

  * **No header row beyond the schema columns.** Single-sheet, no
    formatting, no formulas. Excel still renders 1M cells without
    complaint.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

log = logging.getLogger(__name__)


def parquet_to_xlsx_sibling(parquet_path: str) -> Optional[str]:
    """Write a ``.xlsx`` next to ``parquet_path`` and return its path.

    Returns ``None`` on any failure — caller must treat xlsx delivery
    as best-effort. The Parquet file remains unchanged.

    The output path is ``<basename>.xlsx`` in the same directory, so the
    pairing is discoverable by a simple ``os.path.splitext + ".xlsx"``.
    """
    if not parquet_path or not parquet_path.endswith(".parquet"):
        return None
    if not os.path.exists(parquet_path):
        return None

    xlsx_path = os.path.splitext(parquet_path)[0] + ".xlsx"
    try:
        import pyarrow.parquet as pq
        from openpyxl import Workbook
        from openpyxl.cell import WriteOnlyCell  # noqa: F401 — verifies write-only support

        # Stream Parquet → xlsx without loading the full table into memory.
        # ParquetFile.iter_batches yields RecordBatches; converting each
        # batch to pylist keeps the working set small.
        pf = pq.ParquetFile(parquet_path)
        wb = Workbook(write_only=True)
        ws = wb.create_sheet(title="data")

        column_names = pf.schema.names
        ws.append(column_names)  # header row

        row_count = 0
        for batch in pf.iter_batches(batch_size=2048):
            for record in batch.to_pylist():
                # Preserve column order; openpyxl writes the row left-to-right.
                # Coerce pyarrow Timestamps to Python datetimes — openpyxl
                # rejects pyarrow objects but auto-formats Python datetimes.
                row = []
                for col in column_names:
                    val = record.get(col)
                    if val is None:
                        row.append(None)
                    elif hasattr(val, "to_pydatetime"):
                        # pyarrow Timestamp → naive datetime. Excel doesn't
                        # support tz-aware datetimes — openpyxl raises
                        # ``ValueError: Excel does not support timezones in
                        # datetimes``. Salesforce returns UTC across the
                        # board, so dropping the tzinfo is lossless for our
                        # use case as long as the operator knows columns
                        # like CreatedDate are UTC.
                        dt = val.to_pydatetime()
                        if dt.tzinfo is not None:
                            dt = dt.replace(tzinfo=None)
                        row.append(dt)
                    elif hasattr(val, "tzinfo") and val.tzinfo is not None:
                        # Plain Python datetime with tzinfo — same fix.
                        row.append(val.replace(tzinfo=None))
                    else:
                        row.append(val)
                ws.append(row)
                row_count += 1

        wb.save(xlsx_path)
        log.info(
            "[XLSX_EXPORT] wrote %s (%d rows) alongside %s",
            os.path.basename(xlsx_path),
            row_count,
            os.path.basename(parquet_path),
        )
        return xlsx_path
    except Exception as e:
        log.warning(
            "[XLSX_EXPORT_FAILED] %s → silent fallback (Parquet path intact): %s",
            os.path.basename(parquet_path),
            e,
        )
        # Best-effort cleanup so a partial xlsx doesn't confuse the attach
        # path's sibling-lookup. Failure to remove is itself fine.
        try:
            if os.path.exists(xlsx_path):
                os.remove(xlsx_path)
        except Exception:
            pass
        return None
