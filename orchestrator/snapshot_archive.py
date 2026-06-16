"""Tier 2 of the snapshot retention model: full-row Parquet cold archive.

After each nightly sync completes, the day's raw rows
(opportunities/leads/contacts/accounts) are written to dated Parquet files
and uploaded to an S3-compatible object-storage bucket (a Railway bucket in
prod). Object storage is ~50x cheaper per GB than the Postgres volume, so the
*complete* row-level history is kept FOREVER off-volume — while the hot copy
in Postgres ages out after RAW_HOT_WINDOW_DAYS (see
``db_adapter.purge_raw_rows_older_than``). Incident 2026-06-16.

Optional + gracefully degrading. If ``ARCHIVE_BUCKET_ENABLED`` is false or the
S3 creds are unset, :func:`archive_snapshot` is a logged no-op returning None,
and the purge falls back to a rollup-only guarantee (``archive_required=False``
in the cron) — metrics are still kept forever, only the raw-row drill-down
beyond the hot window is unavailable.

Layout in the bucket:
    {prefix}/{portco_key}/{snapshot_date}/{table}.parquet
e.g. gtm-archive/acme/2026-06-16/opportunities.parquet
"""

import logging
import os
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Optional

import pyarrow as pa
import pyarrow.parquet as pq

import db_adapter

log = logging.getLogger(__name__)

# --- Config (read directly from env, mirroring db_adapter's DATABASE_URL
# pattern, so the module imports cleanly in tests without config.require_env). ---
ARCHIVE_BUCKET_ENABLED = os.environ.get(
    "ARCHIVE_BUCKET_ENABLED", "false"
).strip().lower() in ("1", "true", "yes", "on")
ARCHIVE_S3_ENDPOINT = os.environ.get("ARCHIVE_S3_ENDPOINT", "").strip()
ARCHIVE_S3_BUCKET = os.environ.get("ARCHIVE_S3_BUCKET", "").strip()
ARCHIVE_S3_ACCESS_KEY_ID = os.environ.get("ARCHIVE_S3_ACCESS_KEY_ID", "").strip()
ARCHIVE_S3_SECRET_ACCESS_KEY = os.environ.get(
    "ARCHIVE_S3_SECRET_ACCESS_KEY", ""
).strip()
ARCHIVE_S3_REGION = os.environ.get("ARCHIVE_S3_REGION", "auto").strip() or "auto"
ARCHIVE_S3_PREFIX = os.environ.get("ARCHIVE_S3_PREFIX", "gtm-archive").strip("/")


def archive_enabled() -> bool:
    """True only when archiving is switched on AND fully configured.

    The cron uses this to decide whether the purge must wait for an archive
    (``archive_required``) before dropping hot rows.
    """
    return bool(
        ARCHIVE_BUCKET_ENABLED
        and ARCHIVE_S3_ENDPOINT
        and ARCHIVE_S3_BUCKET
        and ARCHIVE_S3_ACCESS_KEY_ID
        and ARCHIVE_S3_SECRET_ACCESS_KEY
    )


def _normalize_rows(rows: list[dict]) -> list[dict]:
    """Make rows Parquet-safe: Decimal -> float (dates/datetimes pass through).

    pyarrow infers types from the Python objects; Decimal columns with mixed
    NULLs are the only awkward case, so coerce them to float up front.
    """
    out = []
    for r in rows:
        out.append(
            {k: (float(v) if isinstance(v, Decimal) else v) for k, v in r.items()}
        )
    return out


def _write_parquet(rows: list[dict], path: Path) -> bool:
    """Write rows to a Parquet file via pyarrow. Empty table still written
    (so an empty day is faithfully archived, not silently skipped)."""
    table = pa.Table.from_pylist(_normalize_rows(rows))
    pq.write_table(table, str(path))
    return True


def _s3_client():
    """Build a boto3 S3 client for the configured (S3-compatible) endpoint.

    boto3 is imported lazily so the module — and disabled-mode callers — do
    not require the dependency at import time.
    """
    import boto3  # lazy

    return boto3.client(
        "s3",
        endpoint_url=ARCHIVE_S3_ENDPOINT,
        aws_access_key_id=ARCHIVE_S3_ACCESS_KEY_ID,
        aws_secret_access_key=ARCHIVE_S3_SECRET_ACCESS_KEY,
        region_name=ARCHIVE_S3_REGION,
    )


def archive_snapshot(
    snapshot_id: int, portco_key: str, snapshot_date
) -> Optional[str]:
    """Export one snapshot's raw rows to Parquet and upload to the bucket.

    Returns the bucket URI prefix (``s3://bucket/prefix/portco/date``) on
    success and stamps ``snapshots.archived_at`` via
    :func:`db_adapter.mark_snapshot_archived`. Returns None when archiving is
    disabled/unconfigured or on any failure — never raises, so a sync is
    never broken by an archive problem. A failed archive simply leaves
    ``archived_at`` unset, which holds the hot rows in Postgres until the
    next run succeeds (the purge is archive-gated).
    """
    if not archive_enabled():
        log.info(
            "snapshot_archive: archiving disabled/unconfigured; skipping "
            f"snapshot {snapshot_id} ({portco_key} {snapshot_date})"
        )
        return None

    date_str = str(snapshot_date)
    key_prefix = f"{ARCHIVE_S3_PREFIX}/{portco_key}/{date_str}"
    try:
        client = _s3_client()
        with tempfile.TemporaryDirectory() as tmp:
            for table in db_adapter._SNAPSHOT_CHILD_TABLES:
                rows = db_adapter.fetch_snapshot_rows(snapshot_id, table)
                local = Path(tmp) / f"{table}.parquet"
                _write_parquet(rows, local)
                client.upload_file(
                    str(local), ARCHIVE_S3_BUCKET, f"{key_prefix}/{table}.parquet"
                )
                log.info(
                    f"snapshot_archive: uploaded {table} ({len(rows)} rows) -> "
                    f"s3://{ARCHIVE_S3_BUCKET}/{key_prefix}/{table}.parquet"
                )
        uri = f"s3://{ARCHIVE_S3_BUCKET}/{key_prefix}"
        db_adapter.mark_snapshot_archived(snapshot_id, uri)
        log.info(f"snapshot_archive: snapshot {snapshot_id} archived to {uri}")
        return uri
    except Exception:
        log.exception(
            f"snapshot_archive: failed to archive snapshot {snapshot_id} "
            f"({portco_key} {snapshot_date}) — hot rows retained until retry"
        )
        return None
