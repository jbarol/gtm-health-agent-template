"""Tests for the post_report synthetic-data guard (Plan: Design C, 2026-05-15).

Run:
    cd orchestrator && python3 -m pytest synthetic_data_guard_test.py -v
"""

from __future__ import annotations

import session_runner


def test_clean_payload_passes():
    payload = {
        "findings": [
            {
                "headline": "Win rate fell 8.4pp QoQ",
                "value": "From 18.6% in Q1 to 10.2% in Q2 across 1,247 opps.",
                "confidence": "high",
            }
        ]
    }
    assert session_runner._detect_fabricated_rows_in_payload(payload) is None


def test_synthetic_word_is_rejected():
    payload = {
        "findings": [
            {"value": "Top 25 reps by ARR (synthetic data — input parquet missing)."}
        ]
    }
    out = session_runner._detect_fabricated_rows_in_payload(payload)
    assert out is not None
    assert "fabricated-row marker" in out


def test_hardcoded_word_is_rejected():
    payload = {
        "value": "Past-due breakdown: numbers are hardcoded fallback values from spec."
    }
    out = session_runner._detect_fabricated_rows_in_payload(payload)
    assert out is not None


def test_spec_embedded_word_is_rejected():
    """Matches the exact phrase from the live 17YiJ failure."""
    payload = {
        "findings": [
            {
                "headline": "Top 25 reps",
                "value": (
                    "All computed tables (Sections 4-8, 10) use synthetic/"
                    "hardcoded data rather than actual parquet-computed "
                    "values."
                ),
            }
        ]
    }
    out = session_runner._detect_fabricated_rows_in_payload(payload)
    assert out is not None


def test_representative_alone_does_not_trip_the_guard():
    """The word 'representative' is too common to fire by itself. It must
    co-occur with a row-bearing term to count as a soft signal."""
    payload = {
        "headline": "Renewal cohort is representative of overall ARR.",
        "value": "Sample drawn from the full pool; no replacement.",
    }
    assert session_runner._detect_fabricated_rows_in_payload(payload) is None


def test_representative_with_row_bearing_term_trips_guard():
    """The live 17YiJ payload literally said 'the row-level tables ... are
    representative'. That MUST be caught."""
    payload = {
        "findings": [
            {
                "value": (
                    "The row-level tables (Top 25, past-due top 15, etc.) "
                    "are representative."
                )
            }
        ]
    }
    out = session_runner._detect_fabricated_rows_in_payload(payload)
    assert out is not None
    assert "soft-fabrication" in out


def test_guard_walks_nested_structure():
    payload = {
        "cross_domain_pattern": "Looks healthy on the surface.",
        "appendix": {
            "methodology": [{"note": "Spec-embedded values used for top 10 owners."}]
        },
    }
    out = session_runner._detect_fabricated_rows_in_payload(payload)
    assert out is not None


def test_guard_skips_structural_keys():
    """file_path / attachments / schema may contain words like 'table'
    without indicating fabrication. The guard should not fire on those."""
    payload = {
        "file_path": "/mnt/session/outputs/table_synthetic_fixtures.parquet",
        "findings": [{"value": "Win rate is 18.6%."}],
    }
    assert session_runner._detect_fabricated_rows_in_payload(payload) is None


def test_guard_tolerates_non_dict_input():
    assert session_runner._detect_fabricated_rows_in_payload("not a dict") is None
    assert session_runner._detect_fabricated_rows_in_payload([1, 2, 3]) is None
    assert session_runner._detect_fabricated_rows_in_payload(None) is None
