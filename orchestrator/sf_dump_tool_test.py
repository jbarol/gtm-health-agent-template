"""Tests for ``orchestrator.sf_dump_tool``.

Covers:
    - 100-row happy path: parquet file written, handle keys correct, count
      matches, file readable back into the same rows.
    - 10K-row streaming: ParquetWriter is opened exactly once and rows are
      flushed in batches — no full materialization in memory.
    - Auth failure: ``_get_sf_client`` raises → returned handle has ``error``,
      ``file_path is None``, no exception escapes.
    - Disk failure: ``os.makedirs`` raises PermissionError on the primary
      dir → handler either falls back to /tmp/gtm_outputs or returns a
      structured error. No exception escapes.
    - Summary stats over mixed-type columns (numeric + string + null +
      bool) cover every column the model will see.

Run::

    cd orchestrator && python3 -m pytest sf_dump_tool_test.py -v
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pyarrow.parquet as pq

import sf_dump_tool


# ──────────────────────────────────────────────────────────────────────────
# Fakes
# ──────────────────────────────────────────────────────────────────────────


class _FakeSfClient:
    """Stands in for ``simple_salesforce.Salesforce`` in tests.

    Records the SOQL it received and yields the configured rows from
    ``query_all_iter``. Each row includes the synthetic ``attributes`` dict
    that real SF returns so we can prove the handler strips it.
    """

    def __init__(self, rows: list[dict], *, raise_on_iter: Exception | None = None):
        self._rows = rows
        self._raise_on_iter = raise_on_iter
        self.last_soql: str | None = None

    def query_all_iter(self, soql: str):
        self.last_soql = soql
        if self._raise_on_iter is not None:
            raise self._raise_on_iter
        for r in self._rows:
            yield {"attributes": {"type": "Lead", "url": "/lead/x"}, **r}


# ──────────────────────────────────────────────────────────────────────────
# Happy path
# ──────────────────────────────────────────────────────────────────────────


def test_dump_sf_query_writes_parquet_and_returns_handle(tmp_path: Path):
    """100-row happy path: parquet written, handle has all expected keys, file
    readable back into the same rows."""
    rows = [
        {
            "Id": f"00Q{i:06}",
            "Name": f"Lead {i}",
            "Status": "Open" if i % 2 else "Closed",
            "Score": i * 1.25,
        }
        for i in range(100)
    ]
    fake_sf = _FakeSfClient(rows)

    with patch.object(
        sf_dump_tool, "_iter_records", side_effect=lambda c, q: iter(rows)
    ):
        with patch("session_runner._get_sf_client", return_value=fake_sf):
            result = sf_dump_tool.dump_sf_query(
                soql="SELECT Id, Name, Status, Score FROM Lead",
                portco_key="acme",
                label="leads_test",
                output_dir=str(tmp_path),
            )

    # Handle shape (PR 9 default — preview_3, not preview_10).
    for key in (
        "file_path",
        "count",
        "schema",
        "summary_stats",
        "preview_3",
        "summary_text",
    ):
        assert key in result, f"missing key {key!r} in handle"

    assert result["count"] == 100
    assert result["file_path"] and os.path.exists(result["file_path"])
    assert result["file_path"].endswith(".parquet")
    assert "sf_leads_test_" in os.path.basename(result["file_path"])
    # PR 9 default: preview drops from 10 to 3 rows.
    assert len(result["preview_3"]) == 3
    # Preview rows must NOT contain the SF ``attributes`` field.
    assert all("attributes" not in row for row in result["preview_3"])
    # summary_text is short and mentions row count.
    assert len(result["summary_text"]) <= 500
    assert "100" in result["summary_text"]
    # Schema covers all four columns.
    assert set(result["schema"].keys()) == {"Id", "Name", "Status", "Score"}
    assert result["schema"]["Score"] in ("float", "int")
    # File round-trips.
    table = pq.read_table(result["file_path"])
    df = table.to_pydict()
    assert len(df["Id"]) == 100
    assert df["Id"][0] == "00Q000000"


# ──────────────────────────────────────────────────────────────────────────
# Large-result streaming
# ──────────────────────────────────────────────────────────────────────────


def test_dump_sf_query_streams_large_result_without_memory_blowup(tmp_path: Path):
    """10K-row pull writes via ParquetWriter in batches.

    We don't measure RSS directly (flaky across CI machines); instead we
    assert the streaming structure: ParquetWriter is opened exactly once and
    multiple ``write_table`` calls fire. That proves we didn't accumulate all
    rows in memory and call write_table once at the end.
    """
    rows = [
        {"Id": f"00Q{i:08}", "Status": "Open" if i % 2 else "Closed", "Score": i}
        for i in range(10_000)
    ]
    fake_sf = _FakeSfClient(rows)

    write_count = {"n": 0}
    instance_count = {"n": 0}

    real_pw = pq.ParquetWriter

    class _CountingWriter(real_pw):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            instance_count["n"] += 1

        def write_table(self, table, *a, **kw):
            write_count["n"] += 1
            return super().write_table(table, *a, **kw)

    with patch.object(
        sf_dump_tool, "_iter_records", side_effect=lambda c, q: iter(rows)
    ):
        with patch("session_runner._get_sf_client", return_value=fake_sf):
            with patch("pyarrow.parquet.ParquetWriter", _CountingWriter):
                result = sf_dump_tool.dump_sf_query(
                    soql="SELECT Id FROM Lead",
                    portco_key="acme",
                    label="big_pull",
                    output_dir=str(tmp_path),
                )

    assert result["count"] == 10_000
    # Exactly one writer was opened for the file.
    assert instance_count["n"] == 1, (
        f"expected one ParquetWriter for the file, got {instance_count['n']} — "
        "streaming pattern broken"
    )
    # Multiple batches were flushed (batch size 1000 → expect ~10 writes).
    assert write_count["n"] >= 5, (
        f"expected >=5 write_table calls for a 10K-row pull (1000-row batches); "
        f"got {write_count['n']} — likely accumulated everything in memory"
    )

    # File round-trips with the right row count.
    table = pq.read_table(result["file_path"])
    assert table.num_rows == 10_000


# ──────────────────────────────────────────────────────────────────────────
# Error paths — never raise
# ──────────────────────────────────────────────────────────────────────────


def test_dump_sf_query_handles_auth_failure_gracefully(tmp_path: Path):
    """``_get_sf_client`` raises → handle has ``error``, no exception escapes."""
    try:
        from simple_salesforce.exceptions import SalesforceAuthenticationFailed

        auth_err = SalesforceAuthenticationFailed(401, "invalid credentials")
    except ImportError:
        auth_err = RuntimeError("invalid credentials")

    with patch("session_runner._get_sf_client", side_effect=auth_err):
        result = sf_dump_tool.dump_sf_query(
            soql="SELECT Id FROM Lead",
            portco_key="acme",
            label="leads",
            output_dir=str(tmp_path),
        )

    assert result["file_path"] is None
    assert result["count"] == 0
    assert "error" in result
    assert "sf_auth_failed" in result["error"]
    # PR 9: default error shape uses preview_3, not preview_10.
    assert result["preview_3"] == []
    assert result["schema"] == {}
    # No parquet file was created.
    assert not any(tmp_path.iterdir())


def test_dump_sf_query_handles_disk_failure_gracefully(tmp_path: Path, monkeypatch):
    """``os.makedirs`` failure returns a structured error.

    No fallback path: any path outside ``SESSION_OUTPUT_DIR`` is silently
    rejected by Track B's ``_dispatch_post_report`` whitelist. Returning the
    error surfaces the real failure to the agent/operator instead of producing
    an undeliverable file_path.
    """

    def _always_fail(*a, **kw):
        raise PermissionError("read-only filesystem")

    monkeypatch.setattr(sf_dump_tool.os, "makedirs", _always_fail)

    # We don't even need an SF client — the directory check happens first.
    result = sf_dump_tool.dump_sf_query(
        soql="SELECT Id FROM Lead",
        portco_key="acme",
        label="leads",
        output_dir=str(tmp_path / "nope"),
    )

    assert result["file_path"] is None
    assert result["count"] == 0
    assert result["schema"] == {}
    assert result["summary_stats"] == {}
    # PR 9: default error shape uses preview_3, not preview_10.
    assert result["preview_3"] == []
    assert "error" in result
    # Structured error mentions the failed dir and how to fix it.
    assert "Could not create output directory" in result["error"]
    assert "SESSION_OUTPUT_DIR" in result["error"]
    assert "read-only filesystem" in result["error"]


def test_dump_sf_query_does_not_fall_back_outside_session_output_dir(
    tmp_path: Path, monkeypatch
):
    """When the primary dir fails, the tool MUST NOT fall back to /tmp/gtm_outputs.

    Track B's ``_is_safe_attachment_path`` only permits files under
    ``SESSION_OUTPUT_DIR``. A fallback under /tmp produces an attachment path
    the dispatcher silently drops — the agent thinks the dump succeeded but
    the user sees nothing in Slack. Better to surface the error.
    """
    primary = str(tmp_path / "primary")
    real_makedirs = os.makedirs
    makedirs_paths: list[str] = []

    def _selective_fail(path, *a, **kw):
        makedirs_paths.append(path)
        if path == primary:
            raise PermissionError("read-only filesystem")
        return real_makedirs(path, *a, **kw)

    monkeypatch.setattr(sf_dump_tool.os, "makedirs", _selective_fail)

    rows = [{"Id": "x"}]
    fake_sf = _FakeSfClient(rows)

    with patch.object(
        sf_dump_tool, "_iter_records", side_effect=lambda c, q: iter(rows)
    ):
        with patch("session_runner._get_sf_client", return_value=fake_sf):
            result = sf_dump_tool.dump_sf_query(
                soql="SELECT Id FROM Lead",
                portco_key="acme",
                label="leads",
                output_dir=primary,
            )

    # Structured error, no fallback attempt.
    assert result["file_path"] is None
    assert result["count"] == 0
    assert "error" in result
    assert "Could not create output directory" in result["error"]
    # No /tmp fallback was ever attempted.
    assert "/tmp/gtm_outputs" not in makedirs_paths


def test_dump_sf_query_handles_mid_query_failure(tmp_path: Path):
    """SF raises during iteration → handler closes any partial file and returns
    a structured error."""
    fake_sf = _FakeSfClient([], raise_on_iter=RuntimeError("SF 500"))

    with patch("session_runner._get_sf_client", return_value=fake_sf):
        with patch.object(
            sf_dump_tool,
            "_iter_records",
            side_effect=RuntimeError("SF 500"),
        ):
            result = sf_dump_tool.dump_sf_query(
                soql="SELECT Id FROM Lead",
                portco_key="acme",
                label="broken",
                output_dir=str(tmp_path),
            )

    assert result["file_path"] is None
    assert "error" in result
    assert "sf_query_failed" in result["error"]


def test_dump_sf_query_zero_rows_returns_empty_handle(tmp_path: Path):
    """SF returns no rows → handle has count=0 and file_path=None. No file."""
    fake_sf = _FakeSfClient([])

    with patch.object(sf_dump_tool, "_iter_records", side_effect=lambda c, q: iter([])):
        with patch("session_runner._get_sf_client", return_value=fake_sf):
            result = sf_dump_tool.dump_sf_query(
                soql="SELECT Id FROM Lead WHERE Id = 'nope'",
                portco_key="acme",
                label="empty",
                output_dir=str(tmp_path),
            )

    assert result["file_path"] is None
    assert result["count"] == 0
    # PR 9: default empty handle uses preview_3, not preview_10.
    assert result["preview_3"] == []
    assert "0 rows" in result["summary_text"]
    # No parquet file written.
    assert not any(tmp_path.glob("*.parquet"))


# ──────────────────────────────────────────────────────────────────────────
# Summary stats — mixed types
# ──────────────────────────────────────────────────────────────────────────


def test_dump_sf_query_summary_stats_for_mixed_types(tmp_path: Path):
    """Rows with numeric + string + null + bool columns produce summary_stats
    covering every column the model needs to see."""
    rows = [
        {
            "Id": f"00Q{i}",
            "Score": i,
            "Status": "Open" if i % 2 else "Closed",
            "Active": (i % 3 == 0),
            "Notes": None,
        }
        for i in range(20)
    ]
    fake_sf = _FakeSfClient(rows)

    with patch.object(
        sf_dump_tool, "_iter_records", side_effect=lambda c, q: iter(rows)
    ):
        with patch("session_runner._get_sf_client", return_value=fake_sf):
            result = sf_dump_tool.dump_sf_query(
                soql="SELECT Id, Score, Status, Active, Notes FROM Lead",
                portco_key="acme",
                label="mixed",
                output_dir=str(tmp_path),
            )

    stats = result["summary_stats"]
    # Score: numeric — has min/max/mean.
    assert "Score" in stats
    assert stats["Score"]["dtype"] in ("int", "float")
    assert "min" in stats["Score"] and stats["Score"]["min"] == 0
    assert "max" in stats["Score"] and stats["Score"]["max"] == 19
    # Status: low-cardinality str — has top_values.
    assert stats["Status"]["dtype"] == "str"
    assert "top_values" in stats["Status"]
    # Active: bool — has true_count + false_count.
    assert stats["Active"]["dtype"] == "bool"
    assert "true_count" in stats["Active"]
    assert "false_count" in stats["Active"]
    # Notes: all None — dtype is null and null_count == 20.
    assert stats["Notes"]["dtype"] == "null"
    assert stats["Notes"]["null_count"] == 20


def test_dump_sf_query_summary_text_scales_top_value_count_when_sampled(
    tmp_path: Path, monkeypatch
):
    """When count > _STATS_SAMPLE_LIMIT, summary_text's top-value count is
    scaled from the sample to the full population — NOT a raw sample count
    divided by total (which underreports by sample_n/total).

    With a sample limit of 10 and 100 rows where every row has Status='Open',
    the sample sees 10 'Open' rows out of a 10-row sample. The naive (buggy)
    text would say '10 / 10.0%' (sample count / total). The correct scaled
    text says '~100 / 100.0%' — scaled to the full population and labelled.

    Id is given >20 distinct sample values so it becomes high-cardinality and
    gets skipped by the top_values picker, leaving Status as the picked column.
    """
    monkeypatch.setattr(sf_dump_tool, "_STATS_SAMPLE_LIMIT", 10)

    # Id is unique across all 100 rows → 10 unique in sample → low cardinality.
    # To force Status to be the picked column, we make Id explicitly the first
    # key but observably high-cardinality from the sample's perspective by
    # crossing the LOW_CARDINALITY_MAX (20) threshold. The sample limit is 10
    # though, so with 10 rows Id has 10 distinct values (still ≤ 20). We patch
    # the cardinality threshold so Id falls into the high-cardinality bucket
    # and gets unique_count instead of top_values, leaving Status for picking.
    from result_virtualize import LOW_CARDINALITY_MAX as _real_max  # noqa: F401
    import result_virtualize as _rv

    monkeypatch.setattr(_rv, "LOW_CARDINALITY_MAX", 5)

    rows = [{"Id": f"00Q{i:04}", "Status": "Open"} for i in range(100)]
    fake_sf = _FakeSfClient(rows)

    with patch.object(
        sf_dump_tool, "_iter_records", side_effect=lambda c, q: iter(rows)
    ):
        with patch("session_runner._get_sf_client", return_value=fake_sf):
            result = sf_dump_tool.dump_sf_query(
                soql="SELECT Id, Status FROM Lead",
                portco_key="acme",
                label="sampled",
                output_dir=str(tmp_path),
            )

    assert result["count"] == 100
    text = result["summary_text"]
    # The picked column should be Status (only one with top_values now).
    assert "Top Status" in text, f"expected 'Top Status' in summary_text, got: {text!r}"
    # Scaled count + percentage reflect the FULL population, not the sample.
    assert "~100" in text, (
        f"expected scaled count '~100' in summary_text, got: {text!r}"
    )
    assert "100.0%" in text, f"expected 100.0% in summary_text, got: {text!r}"
    # And the sample-size label is present so the model knows it's an estimate.
    assert "10-row sample" in text, (
        f"expected '10-row sample' label in summary_text, got: {text!r}"
    )
    # The naive raw "10 / 10.0%" must NOT appear — that's the bug we're fixing.
    assert "10 / 10.0%" not in text, (
        f"buggy raw sample percentage leaked into summary_text: {text!r}"
    )


def test_dump_sf_query_summary_text_unscaled_when_not_sampled(tmp_path: Path):
    """When count <= _STATS_SAMPLE_LIMIT, the sample IS the population, so the
    top-value count and percentage are reported as exact (no '~', no
    'estimated from' label).

    We use 20 rows with two Status values so Status is unambiguously the
    low-cardinality picked column, with exactly 10 'Open' rows out of 20.
    """
    rows = [
        {"Id": f"00Q{i:03}", "Status": "Open" if i % 2 else "Closed"} for i in range(20)
    ]
    fake_sf = _FakeSfClient(rows)

    with patch.object(
        sf_dump_tool, "_iter_records", side_effect=lambda c, q: iter(rows)
    ):
        with patch("session_runner._get_sf_client", return_value=fake_sf):
            result = sf_dump_tool.dump_sf_query(
                soql="SELECT Id, Status FROM Lead",
                portco_key="acme",
                label="full",
                output_dir=str(tmp_path),
            )

    text = result["summary_text"]
    # Whichever column is picked, the formatting must be exact (no '~', no
    # 'estimated from' label) because the sample IS the full population.
    assert "estimated from" not in text, (
        f"unsampled run should NOT have 'estimated from' label, got: {text!r}"
    )
    # Exact, non-tilde form: e.g. "(10 / 50.0%)" or "(1 / 5.0%)" — but never "(~".
    assert "(~" not in text, f"unsampled run should NOT use '~' prefix, got: {text!r}"


def test_dump_sf_query_strips_sf_attributes_field(tmp_path: Path):
    """The synthetic ``attributes`` dict every SF row carries is dropped from
    the schema, preview, and Parquet file."""
    rows = [{"Id": "00Q1", "Name": "alpha"}]
    fake_sf = _FakeSfClient(rows)

    with patch.object(
        sf_dump_tool,
        "_iter_records",
        side_effect=lambda c, q: iter(
            [{"attributes": {"type": "Lead"}, **r} for r in rows]
        ),
    ):
        with patch("session_runner._get_sf_client", return_value=fake_sf):
            result = sf_dump_tool.dump_sf_query(
                soql="SELECT Id, Name FROM Lead",
                portco_key="acme",
                label="attrs",
                output_dir=str(tmp_path),
            )

    assert "attributes" not in result["schema"]
    # PR 9: default uses preview_3, not preview_10.
    assert all("attributes" not in row for row in result["preview_3"])
    table = pq.read_table(result["file_path"])
    assert "attributes" not in table.column_names


# ──────────────────────────────────────────────────────────────────────────
# Relationship flattening (Owner, RecordType, etc.) — Task #6, 2026-05-14
# ──────────────────────────────────────────────────────────────────────────


def test_flatten_sf_relationships_promotes_subfields_to_top_level():
    """Owner / RecordType nested dicts become Owner_Id / Owner_Name / etc."""
    record = {
        "Id": "006xx",
        "Name": "Acme - Renewal",
        "Owner": {
            "attributes": {"type": "User"},
            "Id": "005xx",
            "Name": "Jane Smith",
        },
        "RecordType": {
            "attributes": {"type": "RecordType"},
            "Name": "New Business",
        },
        "StageName": "Closed Won",
    }

    flat = sf_dump_tool._flatten_sf_relationships(record)

    # Scalar fields untouched
    assert flat["Id"] == "006xx"
    assert flat["Name"] == "Acme - Renewal"
    assert flat["StageName"] == "Closed Won"
    # Relationship sub-fields promoted
    assert flat["Owner_Id"] == "005xx"
    assert flat["Owner_Name"] == "Jane Smith"
    assert flat["RecordType_Name"] == "New Business"
    # Nested ``attributes`` dropped at the relationship level too
    assert "Owner_attributes" not in flat
    assert "RecordType_attributes" not in flat
    # Original nested dict removed
    assert "Owner" not in flat
    assert "RecordType" not in flat


def test_flatten_sf_relationships_no_op_on_flat_record():
    """A record with no dict values passes through unchanged."""
    record = {"Id": "006xx", "Name": "Acme", "Amount": 5000.0}
    assert sf_dump_tool._flatten_sf_relationships(record) == record


def test_flatten_sf_relationships_preserves_null_relationship_without_expectation():
    """Without an ``expected_relationships`` hint, a null parent is left as-is.

    This is the safe fallback path — if the SOQL parser didn't catch the
    relationship, we treat ``Owner: None`` as a scalar rather than fabricating
    sub-field columns we don't know about. The dict case still flattens.
    """
    record = {"Id": "006xx", "Owner": None, "RecordType": {"Name": "Default"}}
    flat = sf_dump_tool._flatten_sf_relationships(record)
    assert flat["Owner"] is None
    assert flat["RecordType_Name"] == "Default"
    assert "RecordType" not in flat


def test_flatten_sf_relationships_expands_null_when_expected():
    """When the SOQL declares ``Owner.Id, Owner.Name`` but the row's Owner is
    null, the flattener emits ``Owner_Id: None, Owner_Name: None`` so the
    Parquet schema stays stable across null-first batches."""
    record = {"Id": "006xx", "Owner": None}
    flat = sf_dump_tool._flatten_sf_relationships(record, {"Owner": ["Id", "Name"]})
    assert flat["Owner_Id"] is None
    assert flat["Owner_Name"] is None
    assert "Owner" not in flat


def test_parse_soql_relationship_fields_extracts_dotted_paths():
    """The SOQL parser pulls ``Parent.Child`` pairs from the SELECT clause."""
    soql = (
        "SELECT Id, Name, Owner.Id, Owner.Name, RecordType.Name, "
        "Account.Industry FROM Opportunity WHERE IsClosed = FALSE"
    )
    rel = sf_dump_tool._parse_soql_relationship_fields(soql)
    assert rel == {
        "Owner": ["Id", "Name"],
        "RecordType": ["Name"],
        "Account": ["Industry"],
    }


def test_parse_soql_relationship_fields_preserves_depth2_paths():
    """Design K (2026-05-15): ``Owner.UserRole.Name`` must be preserved as a
    dotted child so the flattener can traverse one more level. Pre-fix, the
    parser dropped everything after the second dot, leaving Owner_UserRole
    serialized as an OrderedDict string."""
    rel = sf_dump_tool._parse_soql_relationship_fields(
        "SELECT Id, Owner.Id, Owner.UserRole.Name, Account.Owner.Name FROM Opportunity"
    )
    assert rel == {
        "Owner": ["Id", "UserRole.Name"],
        "Account": ["Owner.Name"],
    }


def test_flatten_sf_relationships_handles_depth2_path():
    """Design K (2026-05-15): a declared depth-2 path traverses the nested
    dict so Owner.UserRole.Name lands as Owner_UserRole_Name (not as a
    serialized OrderedDict on the parent Owner_UserRole column)."""
    record = {
        "Id": "006X",
        "Owner": {
            "Id": "005Y",
            "Name": "Jane",
            "UserRole": {"Name": "Account Manager"},
            "attributes": {"type": "User"},
        },
    }
    flat = sf_dump_tool._flatten_sf_relationships(
        record, expected_relationships={"Owner": ["Id", "Name", "UserRole.Name"]}
    )
    assert flat["Owner_Id"] == "005Y"
    assert flat["Owner_Name"] == "Jane"
    assert flat["Owner_UserRole_Name"] == "Account Manager"


def test_flatten_sf_relationships_depth2_null_intermediate():
    """When the intermediate relationship is null (e.g. Owner.UserRole is
    None for unassigned users), the leaf column lands as None — no exception."""
    record = {"Id": "006X", "Owner": {"Id": "005Y", "Name": "Jane", "UserRole": None}}
    flat = sf_dump_tool._flatten_sf_relationships(
        record, expected_relationships={"Owner": ["UserRole.Name"]}
    )
    assert flat["Owner_UserRole_Name"] is None


def test_flatten_sf_relationships_depth1_unchanged():
    """Regression guard: depth-1 declarations still produce simple
    Parent_Child columns. Design K must not break the common case."""
    record = {"Id": "006X", "Owner": {"Id": "005Y", "Name": "Jane"}}
    flat = sf_dump_tool._flatten_sf_relationships(
        record, expected_relationships={"Owner": ["Id", "Name"]}
    )
    assert flat == {"Id": "006X", "Owner_Id": "005Y", "Owner_Name": "Jane"}


# --- Plan: Design J (2026-05-15) — date column coercion ------------------


def test_maybe_promote_to_date_recognizes_lastactivitydate():
    """The exact column from sesn_EXAMPLE that arrived as VARCHAR."""
    rows = [{"LastActivityDate": "2026-05-12"}, {"LastActivityDate": "2026-05-13"}]
    assert sf_dump_tool._maybe_promote_to_date("LastActivityDate", rows) == "date"


def test_maybe_promote_to_date_recognizes_createddate_as_datetime():
    rows = [{"CreatedDate": "2026-05-15T14:32:00.000+0000"}]
    assert sf_dump_tool._maybe_promote_to_date("CreatedDate", rows) == "datetime"


def test_maybe_promote_to_date_custom_field_suffix():
    """Custom fields ending in _Date_Time__c (real Acme pattern) get
    detected. Fields without a date-hinting name segment (e.g.
    Discovery_Call_Booked__c) intentionally stay as string — value-only
    detection is too aggressive and would catch free-text notes with
    ISO-looking timestamps inside."""
    rows = [{"MQL_SDR_Accepted_Date_Time__c": "2026-05-15T20:13:00.000Z"}]
    assert (
        sf_dump_tool._maybe_promote_to_date("MQL_SDR_Accepted_Date_Time__c", rows)
        == "datetime"
    )


def test_maybe_promote_to_date_requires_value_evidence():
    """A field NAMED like a date but containing free text → don't promote.
    Protects the rare case of a notes column with 'date' in its name."""
    rows = [{"DateNotes__c": "Discovery was scheduled for Tuesday"}]
    assert sf_dump_tool._maybe_promote_to_date("DateNotes__c", rows) is None


def test_maybe_promote_to_date_requires_name_hint():
    """A field with ISO date values but a non-date name doesn't get promoted.
    Avoids false positives on string columns that happen to contain dates."""
    rows = [{"Notes": "2026-05-15"}]
    assert sf_dump_tool._maybe_promote_to_date("Notes", rows) is None


def test_coerce_iso_date_parses_string():
    from datetime import date

    assert sf_dump_tool._coerce_iso_date("2026-05-15") == date(2026, 5, 15)


def test_coerce_iso_date_fallback_to_string_on_bad_input():
    out = sf_dump_tool._coerce_iso_date("not-a-date")
    assert out == "not-a-date"


def test_coerce_iso_datetime_parses_zulu():
    from datetime import datetime

    assert sf_dump_tool._coerce_iso_datetime("2026-05-15T14:32:00.000Z") == datetime(
        2026, 5, 15, 14, 32, 0
    )


def test_coerce_iso_datetime_fallback_to_string_on_bad_input():
    out = sf_dump_tool._coerce_iso_datetime("not-a-datetime")
    assert out == "not-a-datetime"


def test_parse_soql_relationship_fields_handles_no_relationships():
    """A SELECT with only scalar columns returns an empty map."""
    assert (
        sf_dump_tool._parse_soql_relationship_fields(
            "SELECT Id, Name, Amount FROM Opportunity"
        )
        == {}
    )


def test_parse_soql_relationship_fields_ignores_subselects():
    """``(SELECT ... FROM ...)`` sub-selects are skipped — they yield lists,
    not dicts, and the flattener leaves them alone anyway.

    Critical: relationship fields appearing AFTER a sub-select must still be
    detected. Codex review (2026-05-14) caught a regression where the parser
    short-circuited on the sub-select's ``FROM`` and never saw later
    ``Parent.Child`` paths, reintroducing the null-first silent-drop bug.
    """
    # Relationship before the sub-select: trivial case.
    rel = sf_dump_tool._parse_soql_relationship_fields(
        "SELECT Id, Owner.Name, (SELECT Id, Name FROM Contacts) FROM Account"
    )
    assert rel == {"Owner": ["Name"]}

    # Relationship AFTER the sub-select: the regression case.
    rel_after = sf_dump_tool._parse_soql_relationship_fields(
        "SELECT Id, (SELECT Id FROM Contacts), Owner.Name FROM Account"
    )
    assert rel_after == {"Owner": ["Name"]}

    # Two relationships bracketing a sub-select.
    rel_both = sf_dump_tool._parse_soql_relationship_fields(
        "SELECT RecordType.Name, (SELECT Id FROM Contacts), Owner.Name FROM Account"
    )
    assert rel_both == {"RecordType": ["Name"], "Owner": ["Name"]}


def test_dump_sf_query_keeps_relationship_columns_when_first_batch_is_null(
    tmp_path: Path,
):
    """Regression for codex review (2026-05-14): when the first 1,000 rows
    have ``Owner: None`` but later rows have populated ``Owner`` dicts, the
    populated ``Owner_Id`` / ``Owner_Name`` values MUST land in Parquet.

    Before the fix, the schema was locked on the first batch's scalar
    ``Owner`` column, and the dict-shaped ``Owner`` in later rows produced
    new keys (``Owner_Name``) that ``_flush`` silently dropped because they
    weren't in the locked schema.
    """
    # 1,001 null-owner rows followed by 50 populated-owner rows. The first
    # batch (1,000) is entirely null; the second batch contains the dicts.
    null_rows = [{"Id": f"006N{i:04}", "Owner": None} for i in range(1_001)]
    populated_rows = [
        {
            "Id": f"006P{i:04}",
            "Owner": {
                "attributes": {"type": "User"},
                "Id": f"005U{i:04}",
                "Name": f"Owner {i}",
            },
        }
        for i in range(50)
    ]
    rows = null_rows + populated_rows
    fake_sf = _FakeSfClient(rows)

    with patch.object(
        sf_dump_tool,
        "_iter_records",
        side_effect=lambda c, q: iter(
            [{"attributes": {"type": "Opportunity"}, **r} for r in rows]
        ),
    ):
        with patch("session_runner._get_sf_client", return_value=fake_sf):
            result = sf_dump_tool.dump_sf_query(
                soql="SELECT Id, Owner.Id, Owner.Name FROM Opportunity",
                portco_key="acme",
                label="null_first_owner",
                output_dir=str(tmp_path),
            )

    # Schema has the expected flattened columns from the start (because we
    # seeded them from the SOQL, not from the first observed dict).
    assert "Owner_Id" in result["schema"]
    assert "Owner_Name" in result["schema"]
    assert "Owner" not in result["schema"]

    # The 50 populated rows survived the round-trip — their values are not
    # silently lost to the locked-schema mismatch.
    table = pq.read_table(result["file_path"])
    py = table.to_pydict()
    populated_names = [n for n in py["Owner_Name"] if n is not None]
    assert len(populated_names) == 50, (
        f"expected 50 populated Owner_Name values after 1,001 null-first rows; "
        f"got {len(populated_names)} — null-first batch locked schema and "
        "dropped later relationship values"
    )
    assert populated_names[0] == "Owner 0"
    assert populated_names[-1] == "Owner 49"


def test_dump_sf_query_flattens_relationship_dicts_into_parquet_columns(
    tmp_path: Path,
):
    """End-to-end: an SF row with nested Owner / RecordType lands in Parquet
    as Owner_Id / Owner_Name / RecordType_Name columns, NOT as stringified
    Python dicts. Closes the gap PR #170's JSON-string coercion papered over."""
    rows = [
        {
            "Id": "006A",
            "Name": "Acme - Renewal",
            "Owner": {
                "attributes": {"type": "User"},
                "Id": "005A",
                "Name": "Jane",
            },
            "RecordType": {
                "attributes": {"type": "RecordType"},
                "Name": "New Business",
            },
        },
        {
            "Id": "006B",
            "Name": "Beta - New",
            "Owner": {
                "attributes": {"type": "User"},
                "Id": "005B",
                "Name": "John",
            },
            "RecordType": {
                "attributes": {"type": "RecordType"},
                "Name": "Renewal",
            },
        },
    ]
    fake_sf = _FakeSfClient(rows)

    with patch.object(
        sf_dump_tool,
        "_iter_records",
        side_effect=lambda c, q: iter(
            [{"attributes": {"type": "Opportunity"}, **r} for r in rows]
        ),
    ):
        with patch("session_runner._get_sf_client", return_value=fake_sf):
            result = sf_dump_tool.dump_sf_query(
                soql="SELECT Id, Name, Owner.Id, Owner.Name, RecordType.Name FROM Opportunity",
                portco_key="acme",
                label="opps_flat",
                output_dir=str(tmp_path),
            )

    # Schema carries the flattened column names
    assert "Owner_Id" in result["schema"]
    assert "Owner_Name" in result["schema"]
    assert "RecordType_Name" in result["schema"]
    # Original relationship dicts are gone — agent can't accidentally read them
    assert "Owner" not in result["schema"]
    assert "RecordType" not in result["schema"]

    table = pq.read_table(result["file_path"])
    assert "Owner_Id" in table.column_names
    assert "Owner_Name" in table.column_names
    assert "RecordType_Name" in table.column_names
    # Real values, not stringified dicts
    py = table.to_pydict()
    assert py["Owner_Name"] == ["Jane", "John"]
    assert py["RecordType_Name"] == ["New Business", "Renewal"]
    assert py["Owner_Id"] == ["005A", "005B"]


# ──────────────────────────────────────────────────────────────────────────
# Filename collision defense
# ──────────────────────────────────────────────────────────────────────────


def test_dump_sf_query_two_rapid_calls_produce_distinct_files(tmp_path: Path):
    """Two rapid dump calls with the same label must not collide on filename."""
    rows_a = [{"Id": "A1"}]
    rows_b = [{"Id": "B1"}]

    with patch.object(
        sf_dump_tool, "_iter_records", side_effect=[iter(rows_a), iter(rows_b)]
    ):
        with patch("session_runner._get_sf_client", return_value=_FakeSfClient([])):
            r_a = sf_dump_tool.dump_sf_query(
                soql="x", portco_key="acme", label="same", output_dir=str(tmp_path)
            )
            r_b = sf_dump_tool.dump_sf_query(
                soql="x", portco_key="acme", label="same", output_dir=str(tmp_path)
            )

    assert r_a["file_path"] != r_b["file_path"], (
        f"same-label dumps collided: {r_a['file_path']!r}"
    )
    assert os.path.exists(r_a["file_path"])
    assert os.path.exists(r_b["file_path"])


# ──────────────────────────────────────────────────────────────────────────
# PR 9 — handle size caps (2026-05-14, perf/shrink-sf-handle-response)
# ──────────────────────────────────────────────────────────────────────────


import json as _json  # noqa: E402 — keep section locality with the PR 9 block


def _wide_synthetic_rows(*, n_rows: int, n_cols: int) -> list[dict]:
    """Build ``n_rows`` rows x ``n_cols`` columns of plausibly-sized SF data.

    Each row carries a sprinkle of allowlist columns so the test exercises
    BOTH the head-by-ordinal kept set AND the allowlist kept set. Status
    is forced low-cardinality so it draws a multi-entry top_values array;
    Id and a couple of UUID-like columns are forced high-cardinality so
    they exercise the unique_count -> unique_pct collapse.
    """
    rows: list[dict] = []
    statuses = ["Open", "Closed Won", "Closed Lost", "Negotiation", "Discovery"]
    types = ["New Business", "Renewal", "Upsell"]
    stages = ["Qualification", "Proposal", "Closed Won", "Closed Lost"]
    for i in range(n_rows):
        row: dict = {}
        # Position 0-4 are the kept-by-ordinal head columns.
        row["Id"] = f"00Q{i:08}"
        row["Name"] = f"Lead {i}"
        row["Status"] = statuses[i % len(statuses)]
        row["Score"] = i * 1.25
        row["Email"] = f"lead{i}@example.com"
        # Allowlist columns past ordinal 5 — exercise the allowlist branch.
        row["StageName"] = stages[i % len(stages)]
        row["Amount"] = float(1000 + (i * 37) % 50000)
        row["RecordType_Name"] = "Customer" if i % 3 else "Prospect"
        row["Type"] = types[i % len(types)]
        row["OwnerId"] = f"005{i % 17:08}"
        row["CloseDate"] = f"2026-{(i % 12) + 1:02}-{(i % 28) + 1:02}"
        row["CreatedDate"] = f"2026-01-{(i % 28) + 1:02}T00:00:00Z"
        row["LastModifiedDate"] = f"2026-05-{(i % 14) + 1:02}T00:00:00Z"
        row["IsClosed"] = i % 2 == 0
        row["IsWon"] = i % 3 == 0
        # Remaining filler columns past head + allowlist. These exist solely
        # to verify they are EXCLUDED from summary_stats by default.
        for j in range(15, n_cols):
            row[f"FillerCol_{j:02}"] = f"v{i}-{j}"
        rows.append(row)
    return rows


def test_dump_sf_query_handle_under_8kb_for_40_column_dataset(tmp_path: Path):
    """PR 9 contract: a 40-column dataset's default handle is <= 8 KB.

    Pre-PR-9 the handle ran ~26 KB on real Acme pulls (preview_10 x
    full row width, summary_stats over every column with raw unique_count
    integers, unbounded top_values arrays). The caps land it under 8192
    bytes when JSON-encoded.
    """
    rows = _wide_synthetic_rows(n_rows=100, n_cols=40)
    fake_sf = _FakeSfClient(rows)

    with patch.object(
        sf_dump_tool, "_iter_records", side_effect=lambda c, q: iter(rows)
    ):
        with patch("session_runner._get_sf_client", return_value=fake_sf):
            result = sf_dump_tool.dump_sf_query(
                soql="SELECT Id, Name, Status FROM Lead",
                portco_key="acme",
                label="size_cap",
                output_dir=str(tmp_path),
            )

    # Default shape (no expand): preview_3 instead of preview_10.
    assert "preview_3" in result
    assert "preview_10" not in result
    assert len(result["preview_3"]) == 3
    # The handle is what the model actually receives — measure its serialized
    # JSON byte size to mirror what enters context as cache_read.
    serialized = _json.dumps(result, default=str)
    size_bytes = len(serialized.encode("utf-8"))
    assert size_bytes <= 8192, (
        f"PR 9 contract violated: handle size {size_bytes} bytes > 8192. "
        f"Top-level keys: {list(result.keys())}. "
        f"summary_stats columns: {list(result['summary_stats'].keys())}."
    )


def test_dump_sf_query_expand_true_restores_full_payload(tmp_path: Path):
    """When ``expand=True``, the handle reverts to the pre-PR-9 shape:
    every schema column appears in summary_stats and preview_10 (not
    preview_3) carries the rows."""
    rows = _wide_synthetic_rows(n_rows=50, n_cols=40)
    fake_sf = _FakeSfClient(rows)

    with patch.object(
        sf_dump_tool, "_iter_records", side_effect=lambda c, q: iter(rows)
    ):
        with patch("session_runner._get_sf_client", return_value=fake_sf):
            result = sf_dump_tool.dump_sf_query(
                soql="SELECT * FROM Lead",
                portco_key="acme",
                label="expanded",
                output_dir=str(tmp_path),
                expand=True,
            )

    # Expanded shape: preview_10 present and carrying 10 rows; preview_3
    # absent (we don't double-emit).
    assert "preview_10" in result, "expand=True must surface preview_10"
    assert "preview_3" not in result
    assert len(result["preview_10"]) == 10
    # Every schema column is represented in summary_stats — no caps applied.
    schema_cols = set(result["schema"].keys())
    stat_cols = set(result["summary_stats"].keys())
    assert schema_cols == stat_cols, (
        f"expand=True must keep every schema column in summary_stats; "
        f"missing: {schema_cols - stat_cols}"
    )


def test_dump_sf_query_allowlist_column_kept_past_ordinal_5(tmp_path: Path):
    """A column from the allowlist (StageName at ordinal 5+) must appear
    in summary_stats even though its position is past the head cut.

    Schema ordering: Id, Name, Status, Score, Email (first 5 — head),
    then StageName at position 5 (an allowlist column past the head),
    then more filler columns. Default-shrunk summary_stats must include
    StageName.
    """
    rows = _wide_synthetic_rows(n_rows=30, n_cols=20)
    fake_sf = _FakeSfClient(rows)

    with patch.object(
        sf_dump_tool, "_iter_records", side_effect=lambda c, q: iter(rows)
    ):
        with patch("session_runner._get_sf_client", return_value=fake_sf):
            result = sf_dump_tool.dump_sf_query(
                soql="SELECT Id FROM Lead",
                portco_key="acme",
                label="allowlist",
                output_dir=str(tmp_path),
            )

    schema_keys = list(result["schema"].keys())
    # Confirm StageName really is past ordinal 5 in this synthetic dataset
    # — otherwise the test would be vacuous.
    assert schema_keys.index("StageName") >= 5, (
        f"test precondition: StageName must be past ordinal 5 in schema; "
        f"got index {schema_keys.index('StageName')}"
    )
    # The allowlist column survives the head-only cut.
    assert "StageName" in result["summary_stats"], (
        f"PR 9 allowlist regression: StageName must appear in default "
        f"summary_stats. Got cols: {list(result['summary_stats'].keys())}"
    )
    # A filler column past the head AND not in the allowlist is excluded.
    assert "FillerCol_18" not in result["summary_stats"], (
        "non-allowlist columns past ordinal 5 must be excluded by default"
    )


def test_shrink_summary_stats_collapses_high_cardinality_unique_count():
    """Pure-unit test for ``_shrink_summary_stats``: a column with
    ``unique_count`` at >0.9 distinct ratio loses the raw integer and
    gains ``unique_pct: "0.99"`` instead.

    Pure-unit so we can exercise the boundary cleanly without spinning
    up the full materialize pipeline.
    """
    # ratio 0.95 -> above threshold -> collapse
    high = {"dtype": "str", "null_count": 0, "unique_count": 95}
    # ratio 0.50 -> below threshold -> keep raw count
    low = {"dtype": "str", "null_count": 0, "unique_count": 50}
    # ratio exactly 0.9 -> NOT above threshold -> keep raw (we use strict >)
    edge = {"dtype": "str", "null_count": 0, "unique_count": 90}
    schema = {"HighCardCol": "str", "LowCardCol": "str", "EdgeCol": "str"}
    full = {"HighCardCol": high, "LowCardCol": low, "EdgeCol": edge}

    shrunk = sf_dump_tool._shrink_summary_stats(full, schema, total_count=100)

    # High cardinality: unique_count gone, unique_pct sentinel present.
    assert "unique_count" not in shrunk["HighCardCol"]
    assert shrunk["HighCardCol"]["unique_pct"] == "0.99"
    # Low cardinality: unique_count untouched.
    assert shrunk["LowCardCol"]["unique_count"] == 50
    assert "unique_pct" not in shrunk["LowCardCol"]
    # Edge case (== 0.9): kept as raw count because the threshold is strict.
    assert shrunk["EdgeCol"]["unique_count"] == 90
    assert "unique_pct" not in shrunk["EdgeCol"]


def test_shrink_summary_stats_caps_top_values_at_10():
    """A top_values array with >10 entries is truncated to the first 10."""
    long_top = [{"value": f"v{i}", "count": 100 - i} for i in range(25)]
    full = {"Status": {"dtype": "str", "null_count": 0, "top_values": long_top}}
    shrunk = sf_dump_tool._shrink_summary_stats(
        full, {"Status": "str"}, total_count=100
    )
    assert len(shrunk["Status"]["top_values"]) == 10
    # Order preserved — we take the first 10, which are the highest-count.
    assert shrunk["Status"]["top_values"][0]["value"] == "v0"
    assert shrunk["Status"]["top_values"][-1]["value"] == "v9"


def test_shrink_summary_stats_uses_sampled_from_denominator_for_high_cardinality():
    """When stats are sample-derived, the high-cardinality collapse uses the
    sample size as the denominator — not the full row count.

    Reproduces the codex finding on the initial PR 9 commit: a 100K-row
    dump with a 1000-row sample produces ``unique_count`` ≈ 1000 against
    the SAMPLE. Dividing by the full ``total_count`` of 100K gives
    distinct_ratio = 0.01 and the column escapes the shrink. The intent
    is to denote ``unique_pct: "0.99"`` for id-like columns — the
    denominator must match the population over which the unique_count
    was computed.
    """
    # Simulate the output of `_summary_stats_for_sample` for a 100K-row
    # dump where the 1000-row sample saw 950 distinct values in `Id` —
    # an id-like column. ``sampled_from`` and ``total_rows`` tags mirror
    # what the function actually attaches.
    id_col = {
        "dtype": "str",
        "null_count": 0,
        "unique_count": 950,
        "sampled_from": 1000,
        "total_rows": 100_000,
    }
    schema = {"Id": "str"}
    full = {"Id": id_col}

    shrunk = sf_dump_tool._shrink_summary_stats(full, schema, total_count=100_000)

    # Sample distinct_ratio = 950 / 1000 = 0.95 → above 0.9 → collapse.
    assert "unique_count" not in shrunk["Id"]
    assert shrunk["Id"]["unique_pct"] == "0.99"


def test_shrink_summary_stats_falls_back_to_total_count_when_no_sample_tag():
    """When `sampled_from` is absent (small full-population dump), the
    denominator falls back to ``total_count`` — preserves the
    pre-fix behavior for the small-pull path."""
    # 100-row full-population dump with 95 distinct = id-like.
    col_stats = {"dtype": "str", "null_count": 0, "unique_count": 95}
    schema = {"Email": "str"}
    full = {"Email": col_stats}

    shrunk = sf_dump_tool._shrink_summary_stats(full, schema, total_count=100)

    # 95 / 100 = 0.95 → collapse.
    assert "unique_count" not in shrunk["Email"]
    assert shrunk["Email"]["unique_pct"] == "0.99"


def test_summary_interesting_columns_includes_lead_funnel_fields():
    """Allowlist must cover lead-side funnel fields, not just opp/account.

    Lead pulls typically `SELECT Id, Name, Email, Phone, CreatedDate
    FROM Lead WHERE ...`, which fills the 5-column head and pushes
    Status / LeadSource past the cap. Without the allowlist entry, the
    Specialists lose summary stats on the two fields they most often
    segment by. Added 2026-05-14 alongside the codex follow-up.
    """
    assert "Status" in sf_dump_tool._SUMMARY_INTERESTING_COLUMNS
    assert "LeadSource" in sf_dump_tool._SUMMARY_INTERESTING_COLUMNS


# ---------------------------------------------------------------------------
# Task 4 (F1): SF child/aggregate subqueries flatten to count + JSON column;
# no dict/list ever lands as a Python repr in the Parquet artifact.
# Issues #300, #303, #304, #305, #325, #328, #329, #330.
# ---------------------------------------------------------------------------


def test_child_subquery_flattens_to_json_and_count(tmp_path: Path):
    rows = [
        {
            "Id": "001A",
            "Name": "Acme",
            "Contacts": {
                "totalSize": 2,
                "done": True,
                "records": [
                    {"attributes": {"type": "Contact"}, "Id": "003A", "Name": "Ann"},
                    {"attributes": {"type": "Contact"}, "Id": "003B", "Name": "Bob"},
                ],
            },
        }
    ]
    with patch.object(
        sf_dump_tool,
        "_iter_records",
        side_effect=lambda c, q: iter(
            [{"attributes": {"type": "Account"}, **r} for r in rows]
        ),
    ):
        with patch("session_runner._get_sf_client", return_value=_FakeSfClient(rows)):
            res = sf_dump_tool.dump_sf_query(
                soql="SELECT Id, Name, (SELECT Id, Name FROM Contacts) FROM Account",
                portco_key="fishbowl",
                label="acct_children",
                output_dir=str(tmp_path),
            )
    assert "Contacts_count" in res["schema"]
    assert "Contacts_json" in res["schema"]
    table = pq.read_table(res["file_path"]).to_pydict()
    assert table["Contacts_count"] == [2]
    parsed = json.loads(table["Contacts_json"][0])
    assert parsed[0]["Name"] == "Ann"
    # The Python-repr leak must be gone.
    assert "OrderedDict" not in table["Contacts_json"][0]
    assert "attributes" not in table["Contacts_json"][0]


def test_coerce_for_parquet_json_encodes_dict():
    out = sf_dump_tool._coerce_for_parquet({"a": 1}, "str")
    assert out == '{"a": 1}'
    assert "OrderedDict" not in out


def test_coerce_for_parquet_json_encodes_list():
    out = sf_dump_tool._coerce_for_parquet([1, {"b": 2}], "str")
    assert json.loads(out) == [1, {"b": 2}]


# ---------------------------------------------------------------------------
# Task 8 (F4): quoted SOQL date literals are auto-unquoted before submission.
# Issues #296, #298, #321, #323.
# ---------------------------------------------------------------------------


def test_soql_quoted_date_literal_is_unquoted(tmp_path: Path):
    captured = {}

    def fake_iter(client, soql):
        captured["soql"] = soql
        return iter([])

    with patch.object(sf_dump_tool, "_iter_records", side_effect=fake_iter):
        with patch("session_runner._get_sf_client", return_value=_FakeSfClient([])):
            sf_dump_tool.dump_sf_query(
                soql="SELECT Id FROM Lead WHERE CreatedDate >= '2024-01-01T00:00:00Z'",
                portco_key="fishbowl",
                label="dl",
                output_dir=str(tmp_path),
            )
    assert "'2024-01-01T00:00:00Z'" not in captured["soql"]
    assert "2024-01-01T00:00:00Z" in captured["soql"]


def test_soql_quoted_plain_date_is_unquoted(tmp_path: Path):
    captured = {}

    def fake_iter(client, soql):
        captured["soql"] = soql
        return iter([])

    with patch.object(sf_dump_tool, "_iter_records", side_effect=fake_iter):
        with patch("session_runner._get_sf_client", return_value=_FakeSfClient([])):
            sf_dump_tool.dump_sf_query(
                soql="SELECT Id FROM Opportunity WHERE CloseDate <= '2024-12-31'",
                portco_key="fishbowl",
                label="dl2",
                output_dir=str(tmp_path),
            )
    assert "'2024-12-31'" not in captured["soql"]
    assert "2024-12-31" in captured["soql"]


def test_soql_string_literal_quotes_preserved(tmp_path: Path):
    """Non-date string literals must KEEP their quotes."""
    captured = {}

    def fake_iter(client, soql):
        captured["soql"] = soql
        return iter([])

    with patch.object(sf_dump_tool, "_iter_records", side_effect=fake_iter):
        with patch("session_runner._get_sf_client", return_value=_FakeSfClient([])):
            sf_dump_tool.dump_sf_query(
                soql="SELECT Id FROM Lead WHERE Status = 'Open'",
                portco_key="fishbowl",
                label="dl3",
                output_dir=str(tmp_path),
            )
    assert "'Open'" in captured["soql"]


def test_soql_date_like_text_equality_quotes_preserved(tmp_path: Path):
    """A date-LOOKING value compared with ``=`` (a text field) keeps its quotes
    — only relational comparisons (date ranges) get unquoted (codex review)."""
    captured = {}

    def fake_iter(client, soql):
        captured["soql"] = soql
        return iter([])

    with patch.object(sf_dump_tool, "_iter_records", side_effect=fake_iter):
        with patch("session_runner._get_sf_client", return_value=_FakeSfClient([])):
            sf_dump_tool.dump_sf_query(
                soql="SELECT Id FROM Campaign WHERE Campaign_Code__c = '2024-01-01'",
                portco_key="fishbowl",
                label="dl4",
                output_dir=str(tmp_path),
            )
    assert "'2024-01-01'" in captured["soql"]
