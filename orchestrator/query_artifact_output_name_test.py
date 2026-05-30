"""Tests for query_artifact's output_name parameter (Design #16, 2026-05-15).

Run:
    cd orchestrator && python3 -m pytest query_artifact_output_name_test.py -v
"""

from __future__ import annotations

import artifact_query_tool


def test_safe_output_name_accepts_plain_basename():
    assert (
        artifact_query_tool._safe_output_name("q2_2026_propensity_scored")
        == "q2_2026_propensity_scored.parquet"
    )


def test_safe_output_name_accepts_with_parquet_suffix():
    assert (
        artifact_query_tool._safe_output_name("top_15_reps.parquet")
        == "top_15_reps.parquet"
    )


def test_safe_output_name_rejects_path_separators():
    assert artifact_query_tool._safe_output_name("../etc/passwd") is None
    assert artifact_query_tool._safe_output_name("foo/bar") is None
    assert artifact_query_tool._safe_output_name("/abs/path") is None


def test_safe_output_name_rejects_leading_dot():
    assert artifact_query_tool._safe_output_name(".hidden") is None


def test_safe_output_name_rejects_empty_and_oversized():
    assert artifact_query_tool._safe_output_name("") is None
    assert artifact_query_tool._safe_output_name("   ") is None
    assert artifact_query_tool._safe_output_name("x" * 81) is None


def test_safe_output_name_rejects_special_chars():
    assert artifact_query_tool._safe_output_name("a b") is None
    assert artifact_query_tool._safe_output_name("a;rm -rf") is None
    assert artifact_query_tool._safe_output_name("résumé") is None


def test_safe_output_name_handles_non_string():
    assert artifact_query_tool._safe_output_name(None) is None  # type: ignore[arg-type]
    assert artifact_query_tool._safe_output_name(123) is None  # type: ignore[arg-type]


def test_virtualize_uses_output_name_when_supplied(tmp_path):
    """The semantic name lands as the basename instead of qa_<ts>_<uuid>."""
    import pandas as pd

    df = pd.DataFrame({"x": list(range(100))})
    result = artifact_query_tool._virtualize_query_result(
        df, str(tmp_path), output_name="my_report"
    )
    assert result["file_path"].endswith("/my_report.parquet")
    assert result["row_count"] == 100


def test_virtualize_falls_back_to_auto_when_name_unsafe(tmp_path):
    """Unsafe input doesn't crash — auto-name is used instead."""
    import pandas as pd

    df = pd.DataFrame({"x": [1, 2, 3]})
    result = artifact_query_tool._virtualize_query_result(
        df, str(tmp_path), output_name="../bad/name"
    )
    basename = result["file_path"].rsplit("/", 1)[-1]
    assert basename.startswith("qa_") and basename.endswith(".parquet")


def test_virtualize_falls_back_to_auto_when_no_name(tmp_path):
    import pandas as pd

    df = pd.DataFrame({"x": [1, 2]})
    result = artifact_query_tool._virtualize_query_result(df, str(tmp_path))
    basename = result["file_path"].rsplit("/", 1)[-1]
    assert basename.startswith("qa_") and basename.endswith(".parquet")
