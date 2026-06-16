"""Tests for the Parquet cold archive (incident 2026-06-16).

boto3 is patched out (``_s3_client``) so these run without the dependency;
pyarrow round-trips are real.
"""

from datetime import date, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pyarrow.parquet as pq

import snapshot_archive


def test_archive_enabled_false_when_unconfigured():
    with (
        patch.object(snapshot_archive, "ARCHIVE_BUCKET_ENABLED", False),
    ):
        assert snapshot_archive.archive_enabled() is False


def test_archive_enabled_true_only_when_fully_configured():
    with (
        patch.object(snapshot_archive, "ARCHIVE_BUCKET_ENABLED", True),
        patch.object(snapshot_archive, "ARCHIVE_S3_ENDPOINT", "https://x"),
        patch.object(snapshot_archive, "ARCHIVE_S3_BUCKET", "b"),
        patch.object(snapshot_archive, "ARCHIVE_S3_ACCESS_KEY_ID", "k"),
        patch.object(snapshot_archive, "ARCHIVE_S3_SECRET_ACCESS_KEY", "s"),
    ):
        assert snapshot_archive.archive_enabled() is True
    # missing one cred -> not enabled
    with (
        patch.object(snapshot_archive, "ARCHIVE_BUCKET_ENABLED", True),
        patch.object(snapshot_archive, "ARCHIVE_S3_ENDPOINT", "https://x"),
        patch.object(snapshot_archive, "ARCHIVE_S3_BUCKET", ""),
        patch.object(snapshot_archive, "ARCHIVE_S3_ACCESS_KEY_ID", "k"),
        patch.object(snapshot_archive, "ARCHIVE_S3_SECRET_ACCESS_KEY", "s"),
    ):
        assert snapshot_archive.archive_enabled() is False


def test_archive_snapshot_disabled_is_noop():
    with patch.object(snapshot_archive, "ARCHIVE_BUCKET_ENABLED", False):
        # _s3_client must never be reached when disabled
        with patch.object(snapshot_archive, "_s3_client") as s3:
            out = snapshot_archive.archive_snapshot(1, "acme", date(2026, 6, 16))
    assert out is None
    s3.assert_not_called()


def test_write_parquet_roundtrips_decimal_and_dates(tmp_path):
    rows = [
        {
            "sf_id": "006A",
            "amount": Decimal("1234.50"),
            "close_date": date(2026, 6, 1),
            "created_date": datetime(2026, 5, 1, 12, 0, 0),
            "is_won": True,
            "name": None,
        }
    ]
    path = tmp_path / "opportunities.parquet"
    assert snapshot_archive._write_parquet(rows, path) is True
    back = pq.read_table(str(path)).to_pylist()
    assert back[0]["sf_id"] == "006A"
    assert back[0]["amount"] == 1234.5  # Decimal normalized to float
    assert back[0]["name"] is None


def test_write_parquet_handles_empty_rows(tmp_path):
    path = tmp_path / "leads.parquet"
    assert snapshot_archive._write_parquet([], path) is True
    assert pq.read_table(str(path)).num_rows == 0


def test_archive_snapshot_enabled_uploads_all_tables_and_marks(tmp_path):
    fake_s3 = MagicMock()
    rows_by_table = {
        "opportunities": [{"sf_id": "006", "amount": Decimal("10")}],
        "leads": [{"sf_id": "00Q"}],
        "contacts": [],
        "accounts": [{"sf_id": "001", "arr": Decimal("5")}],
    }
    with (
        patch.object(snapshot_archive, "ARCHIVE_BUCKET_ENABLED", True),
        patch.object(snapshot_archive, "ARCHIVE_S3_ENDPOINT", "https://x"),
        patch.object(snapshot_archive, "ARCHIVE_S3_BUCKET", "gtm-bucket"),
        patch.object(snapshot_archive, "ARCHIVE_S3_ACCESS_KEY_ID", "k"),
        patch.object(snapshot_archive, "ARCHIVE_S3_SECRET_ACCESS_KEY", "s"),
        patch.object(snapshot_archive, "ARCHIVE_S3_PREFIX", "gtm-archive"),
        patch.object(snapshot_archive, "_s3_client", return_value=fake_s3),
        patch.object(
            snapshot_archive.db_adapter,
            "fetch_snapshot_rows",
            side_effect=lambda sid, table: rows_by_table[table],
        ),
        patch.object(
            snapshot_archive.db_adapter, "mark_snapshot_archived"
        ) as mark,
    ):
        uri = snapshot_archive.archive_snapshot(42, "acme", date(2026, 6, 16))

    assert uri == "s3://gtm-bucket/gtm-archive/acme/2026-06-16"
    # one upload per child table
    assert fake_s3.upload_file.call_count == 4
    keys = sorted(c[0][2] for c in fake_s3.upload_file.call_args_list)
    assert keys == [
        "gtm-archive/acme/2026-06-16/accounts.parquet",
        "gtm-archive/acme/2026-06-16/contacts.parquet",
        "gtm-archive/acme/2026-06-16/leads.parquet",
        "gtm-archive/acme/2026-06-16/opportunities.parquet",
    ]
    mark.assert_called_once_with(42, uri)


def test_archive_snapshot_upload_failure_returns_none_and_skips_mark():
    fake_s3 = MagicMock()
    fake_s3.upload_file.side_effect = RuntimeError("network down")
    with (
        patch.object(snapshot_archive, "ARCHIVE_BUCKET_ENABLED", True),
        patch.object(snapshot_archive, "ARCHIVE_S3_ENDPOINT", "https://x"),
        patch.object(snapshot_archive, "ARCHIVE_S3_BUCKET", "b"),
        patch.object(snapshot_archive, "ARCHIVE_S3_ACCESS_KEY_ID", "k"),
        patch.object(snapshot_archive, "ARCHIVE_S3_SECRET_ACCESS_KEY", "s"),
        patch.object(snapshot_archive, "_s3_client", return_value=fake_s3),
        patch.object(
            snapshot_archive.db_adapter, "fetch_snapshot_rows", return_value=[{"x": 1}]
        ),
        patch.object(
            snapshot_archive.db_adapter, "mark_snapshot_archived"
        ) as mark,
    ):
        out = snapshot_archive.archive_snapshot(1, "acme", date(2026, 6, 16))
    assert out is None
    mark.assert_not_called()  # not marked archived -> purge keeps hot rows
