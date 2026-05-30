"""Tests for ``orchestrator.artifact_query_tool``.

Covers the Track H contract (Iteration 2 of misty-squishing-badger):

    - Inline path: ≤50-row result returns rows inline.
    - Virtualized path: >50-row result writes a new Parquet file and the
      response is a handle.
    - Multi-file JOIN: two Parquet files joined via ``t0`` / ``t1``.
    - Aggregate query: GROUP BY + COUNT(*) returns expected aggregate rows.
    - Path-whitelist: paths outside ``SESSION_OUTPUT_DIR`` are rejected.
    - Symlink attack: a symlink inside the session dir pointing OUT is
      rejected.
    - Malformed SQL: returns an error dict instead of raising.
    - Missing file: returns an error dict instead of raising.
    - CSV input: a small CSV is queryable via the ``t`` alias.

Run::

    cd orchestrator && python3 -m pytest artifact_query_tool_test.py -v
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import pytest

from artifact_query_tool import query_artifact


# ──────────────────────────────────────────────────────────────────────────
# Test helpers
# ──────────────────────────────────────────────────────────────────────────


def _write_parquet(path: Path, rows: list[dict]) -> str:
    """Write a list of row dicts as a Parquet file. Returns the path."""
    df = pd.DataFrame(rows)
    df.to_parquet(path, engine="pyarrow", index=False)
    return str(path)


def _write_csv(path: Path, rows: list[dict]) -> str:
    """Write a list of row dicts as a CSV file. Returns the path."""
    df = pd.DataFrame(rows)
    df.to_csv(path, index=False)
    return str(path)


@pytest.fixture
def session_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create an isolated session-output directory and point env at it."""
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    monkeypatch.setenv("SESSION_OUTPUT_DIR", str(outputs))
    return outputs


# ──────────────────────────────────────────────────────────────────────────
# Inline result path (≤50 rows)
# ──────────────────────────────────────────────────────────────────────────


def test_query_artifact_single_file_inline_result(session_outputs: Path):
    """A 20-row Parquet queried with SELECT * returns inline rows."""
    rows = [{"id": i, "stage": "Open", "amount": i * 100.0} for i in range(20)]
    fpath = _write_parquet(session_outputs / "small.parquet", rows)

    result = query_artifact(file_paths=[fpath], sql="SELECT * FROM t ORDER BY id")

    assert result["inline"] is True
    assert result["row_count"] == 20
    assert len(result["rows"]) == 20
    assert result["rows"][0] == {"id": 0, "stage": "Open", "amount": 0.0}
    assert "id" in result["schema"]
    assert "stage" in result["schema"]


# ──────────────────────────────────────────────────────────────────────────
# Virtualized result path (>50 rows)
# ──────────────────────────────────────────────────────────────────────────


def test_query_artifact_single_file_virtualized_result(session_outputs: Path):
    """A 100-row SELECT * returns a virtualized handle, not inline rows."""
    rows = [{"id": i, "label": f"row-{i}"} for i in range(100)]
    fpath = _write_parquet(session_outputs / "big.parquet", rows)

    result = query_artifact(file_paths=[fpath], sql="SELECT * FROM t")

    assert result["inline"] is False
    assert result["row_count"] == 100
    assert "rows" not in result, "virtualized result must not include inline rows"
    assert result["file_path"].endswith(".parquet")
    assert os.path.isfile(result["file_path"])
    # The virtualized file must be readable and contain the full result.
    df = pd.read_parquet(result["file_path"])
    assert len(df) == 100
    # Preview shape matches the contract.
    assert len(result["preview_10"]) == 10
    assert result["preview_10"][0]["id"] == 0
    # Schema present.
    assert set(result["schema"].keys()) == {"id", "label"}
    # Summary stats present (shape varies but must be a dict).
    assert isinstance(result["summary_stats"], dict)
    assert result["summary_stats"]  # non-empty


# ──────────────────────────────────────────────────────────────────────────
# Multi-file JOIN
# ──────────────────────────────────────────────────────────────────────────


def test_query_artifact_multi_file_join(session_outputs: Path):
    """JOIN two Parquet files via t0/t1, assert correct join semantics."""
    accounts = [
        {"account_id": 1, "name": "Acme"},
        {"account_id": 2, "name": "Beta"},
        {"account_id": 3, "name": "Gamma"},
    ]
    opps = [
        {"opp_id": "a", "account_id": 1, "amount": 100},
        {"opp_id": "b", "account_id": 1, "amount": 200},
        {"opp_id": "c", "account_id": 2, "amount": 300},
    ]
    accounts_path = _write_parquet(session_outputs / "accounts.parquet", accounts)
    opps_path = _write_parquet(session_outputs / "opps.parquet", opps)

    result = query_artifact(
        file_paths=[accounts_path, opps_path],
        sql=(
            "SELECT t1.opp_id, t0.name, t1.amount "
            "FROM t0 JOIN t1 ON t0.account_id = t1.account_id "
            "ORDER BY t1.opp_id"
        ),
    )

    assert result["inline"] is True
    assert result["row_count"] == 3
    rows = result["rows"]
    assert rows[0]["opp_id"] == "a"
    assert rows[0]["name"] == "Acme"
    assert rows[0]["amount"] == 100
    assert rows[2]["opp_id"] == "c"
    assert rows[2]["name"] == "Beta"


# ──────────────────────────────────────────────────────────────────────────
# Aggregate (GROUP BY + COUNT)
# ──────────────────────────────────────────────────────────────────────────


def test_query_artifact_aggregate_query(session_outputs: Path):
    """GROUP BY stage with COUNT(*) returns expected per-stage rows."""
    rows = (
        [{"id": i, "stage": "Open"} for i in range(7)]
        + [{"id": i, "stage": "Won"} for i in range(5)]
        + [{"id": i, "stage": "Lost"} for i in range(3)]
    )
    fpath = _write_parquet(session_outputs / "stages.parquet", rows)

    result = query_artifact(
        file_paths=[fpath],
        sql="SELECT stage, COUNT(*) AS n FROM t GROUP BY stage ORDER BY n DESC",
    )

    assert result["inline"] is True
    assert result["row_count"] == 3
    # DuckDB returns integer counts as numpy/python ints; cast for compare.
    rows_out = [
        {k: int(v) if k == "n" else v for k, v in r.items()} for r in result["rows"]
    ]
    assert rows_out == [
        {"stage": "Open", "n": 7},
        {"stage": "Won", "n": 5},
        {"stage": "Lost", "n": 3},
    ]


# ──────────────────────────────────────────────────────────────────────────
# Security: path-whitelist rejection
# ──────────────────────────────────────────────────────────────────────────


def test_query_artifact_rejects_path_outside_session_dir(session_outputs: Path):
    """A path completely outside SESSION_OUTPUT_DIR returns an error dict."""
    result = query_artifact(
        file_paths=["/etc/passwd"],
        sql="SELECT * FROM t",
    )

    assert "error" in result
    assert result["inline"] is True
    assert result["rows"] == []
    assert result["row_count"] == 0
    # Error message must mention path rejection, not just generic failure.
    assert (
        "session output" in result["error"].lower()
        or "symlink" in result["error"].lower()
    )


def test_query_artifact_rejects_symlink_attack(tmp_path: Path, session_outputs: Path):
    """A symlink INSIDE the session dir pointing OUT is rejected.

    Mirrors the attack vector covered by Track B's
    ``_is_safe_attachment_path`` test: an attacker (or prompt-injected
    agent) could materialize a file outside the safe dir and place a
    symlink to it inside. ``realpath`` would land outside; the symlink
    check intercepts before that.
    """
    # Create a fake parquet outside the safe dir.
    outside = tmp_path / "secret.parquet"
    df = pd.DataFrame([{"secret": "data"}])
    df.to_parquet(outside, engine="pyarrow", index=False)

    # Place a symlink inside the safe dir pointing at it.
    sym = session_outputs / "innocuous.parquet"
    sym.symlink_to(outside)
    assert sym.is_symlink()

    result = query_artifact(
        file_paths=[str(sym)],
        sql="SELECT * FROM t",
    )

    assert "error" in result
    assert result["row_count"] == 0
    assert result["rows"] == []


# ──────────────────────────────────────────────────────────────────────────
# Failure modes: bad SQL, missing file
# ──────────────────────────────────────────────────────────────────────────


def test_query_artifact_handles_malformed_sql(session_outputs: Path):
    """A SQL parse error returns an error dict — no exception."""
    rows = [{"id": 1, "x": "a"}]
    fpath = _write_parquet(session_outputs / "tiny.parquet", rows)

    result = query_artifact(
        file_paths=[fpath],
        sql="SELECT ZZZZ FROM t WHERE ??? ",
    )

    assert "error" in result
    assert result["inline"] is True
    assert result["row_count"] == 0
    assert (
        "query failed" in result["error"].lower() or "parser" in result["error"].lower()
    )


def test_query_artifact_handles_missing_file(session_outputs: Path):
    """A path inside the safe dir but with no file returns an error dict."""
    missing = session_outputs / "does_not_exist.parquet"
    assert not missing.exists()

    result = query_artifact(
        file_paths=[str(missing)],
        sql="SELECT * FROM t",
    )

    assert "error" in result
    assert result["row_count"] == 0
    assert "not found" in result["error"].lower()


# ──────────────────────────────────────────────────────────────────────────
# CSV input
# ──────────────────────────────────────────────────────────────────────────


def test_query_artifact_csv_input(session_outputs: Path):
    """A CSV file can be queried via the ``t`` alias.

    DuckDB's ``read_csv_auto`` sniffs types from the header; we round-trip
    a small CSV and assert the response is well-shaped.
    """
    rows = [{"id": i, "name": f"row-{i}"} for i in range(10)]
    fpath = _write_csv(session_outputs / "small.csv", rows)

    result = query_artifact(
        file_paths=[fpath],
        sql="SELECT id, name FROM t WHERE id < 5 ORDER BY id",
    )

    assert result["inline"] is True
    assert result["row_count"] == 5
    assert result["rows"][0] == {"id": 0, "name": "row-0"}
    assert result["rows"][-1] == {"id": 4, "name": "row-4"}


# ──────────────────────────────────────────────────────────────────────────
# Extra: bad input shapes
# ──────────────────────────────────────────────────────────────────────────


def test_query_artifact_rejects_empty_file_paths(session_outputs: Path):
    """Empty file_paths list returns an error dict (never crashes DuckDB)."""
    result = query_artifact(file_paths=[], sql="SELECT 1")
    assert "error" in result
    assert "non-empty" in result["error"].lower()


def test_query_artifact_rejects_xlsx_input(session_outputs: Path):
    """xlsx is explicitly rejected with a hint to use Parquet."""
    # Touch a fake xlsx so the path-existence check doesn't fire first.
    fake = session_outputs / "data.xlsx"
    fake.write_bytes(b"PK\x03\x04 fake xlsx")
    result = query_artifact(
        file_paths=[str(fake)],
        sql="SELECT * FROM t",
    )
    assert "error" in result
    assert "xlsx" in result["error"].lower()


# ──────────────────────────────────────────────────────────────────────────
# Security: SQL-body exfiltration guard (P1 fix, PR #101 codex review)
# ──────────────────────────────────────────────────────────────────────────


def test_external_file_read_in_sql_is_rejected(session_outputs: Path):
    """SQL calling ``read_csv_auto`` on a non-whitelisted path is rejected.

    This is the load-bearing security test for the P1 codex finding: the
    ``file_paths`` whitelist was bypassable because DuckDB SQL can call
    ``read_csv_auto`` / ``read_parquet`` / ``read_text`` on arbitrary host
    paths directly from the query body. The fix disables external file
    access at the connection level after registering whitelisted paths as
    in-memory tables.
    """
    # Set up a legitimate Parquet artifact inside the session output dir.
    valid_rows = [{"id": i, "amount": i * 10.0} for i in range(5)]
    valid_path = _write_parquet(session_outputs / "valid.parquet", valid_rows)

    # Pick a path the agent should never be able to reach. We don't care
    # whether the file exists — the lockdown rejects the call before
    # filesystem stat. Use a path outside session_outputs.
    target = "/etc/passwd"

    result = query_artifact(
        file_paths=[valid_path],
        sql=f"SELECT * FROM read_csv_auto('{target}')",
    )

    assert "error" in result, "external read must be rejected"
    assert result["row_count"] == 0
    assert result["rows"] == []
    # Error should reflect the permission denial, not a phantom success.
    err = result["error"].lower()
    assert (
        "permission" in err
        or "disabled" in err
        or "external" in err
        or "query failed" in err
    ), f"unexpected error: {result['error']!r}"


def test_external_read_parquet_in_sql_is_rejected(session_outputs: Path):
    """Same vector via ``read_parquet`` — the other common exfil function."""
    valid_rows = [{"id": 1, "x": "a"}]
    valid_path = _write_parquet(session_outputs / "valid.parquet", valid_rows)

    result = query_artifact(
        file_paths=[valid_path],
        sql="SELECT * FROM read_parquet('/etc/hostname')",
    )

    assert "error" in result
    assert result["row_count"] == 0
    assert result["rows"] == []


def test_external_read_text_in_sql_is_rejected(session_outputs: Path):
    """Same vector via ``read_text`` — used for raw file content exfil."""
    valid_rows = [{"id": 1}]
    valid_path = _write_parquet(session_outputs / "valid.parquet", valid_rows)

    result = query_artifact(
        file_paths=[valid_path],
        sql="SELECT * FROM read_text('/etc/passwd')",
    )

    assert "error" in result
    assert result["row_count"] == 0
    assert result["rows"] == []


@pytest.mark.parametrize(
    "bad_sql",
    [
        "INSERT INTO t VALUES (99, 'x', 0.0)",
        "UPDATE t SET stage = 'X'",
        "DELETE FROM t",
        "DROP TABLE t",
        "ALTER TABLE t ADD COLUMN y INT",
        "CREATE TABLE u AS SELECT 1",
        "PRAGMA database_list",
        "SET memory_limit = '1GB'",
        "COPY t TO '/tmp/leak.csv'",
        "ATTACH ':memory:' AS db2",
        "LOAD httpfs",
        "INSTALL httpfs",
    ],
)
def test_only_select_and_explain_allowed(session_outputs: Path, bad_sql: str):
    """DML / DDL / PRAGMA / SET / COPY / ATTACH / LOAD / INSTALL are rejected.

    The parse-guard layer (layer 2) catches these with a clear error before
    they reach the connection. Even if it didn't, layer 1 (external access
    disabled) would block the filesystem-touching ones.
    """
    rows = [{"id": i, "stage": "Open", "amount": float(i)} for i in range(3)]
    fpath = _write_parquet(session_outputs / "data.parquet", rows)

    result = query_artifact(file_paths=[fpath], sql=bad_sql)
    assert "error" in result, f"should reject: {bad_sql!r}"
    assert result["row_count"] == 0
    assert result["rows"] == []


def test_explain_is_allowed(session_outputs: Path):
    """``EXPLAIN`` is read-only and on the allowlist."""
    rows = [{"id": 1, "x": "a"}]
    fpath = _write_parquet(session_outputs / "data.parquet", rows)

    result = query_artifact(file_paths=[fpath], sql="EXPLAIN SELECT * FROM t")
    # EXPLAIN returns a plan; we just need to confirm no error path fired.
    assert "error" not in result, f"unexpected error: {result.get('error')!r}"


def test_with_cte_is_allowed(session_outputs: Path):
    """``WITH ... SELECT`` normalizes to SELECT and runs successfully."""
    rows = [{"id": i, "amount": i * 10} for i in range(10)]
    fpath = _write_parquet(session_outputs / "data.parquet", rows)

    result = query_artifact(
        file_paths=[fpath],
        sql="WITH big AS (SELECT * FROM t WHERE amount > 30) SELECT COUNT(*) AS n FROM big",
    )
    assert "error" not in result, f"unexpected error: {result.get('error')!r}"
    assert result["row_count"] == 1
    assert int(result["rows"][0]["n"]) == 6


def test_legitimate_select_still_works_after_lockdown(session_outputs: Path):
    """Happy path: pre-registered tables remain queryable after lockdown.

    The fix flips ``enable_external_access = false`` after registering the
    whitelisted Parquet into an in-memory table. The user SQL must still
    succeed against that table. This catches the regression where someone
    mistakenly registers as a VIEW (which holds a lazy ``read_parquet``
    reference) and the lockdown breaks legitimate queries.
    """
    rows = [{"id": i, "stage": "Open" if i % 2 == 0 else "Won"} for i in range(8)]
    fpath = _write_parquet(session_outputs / "data.parquet", rows)

    result = query_artifact(
        file_paths=[fpath],
        sql="SELECT stage, COUNT(*) AS n FROM t GROUP BY stage ORDER BY stage",
    )
    assert "error" not in result, f"unexpected error: {result.get('error')!r}"
    assert result["row_count"] == 2
    counts = {r["stage"]: int(r["n"]) for r in result["rows"]}
    assert counts == {"Open": 4, "Won": 4}
