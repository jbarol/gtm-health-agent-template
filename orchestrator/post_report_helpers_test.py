"""Tests for Theme D helpers in session_runner: validation retry detail
builder + chart-dispatch heuristic.

We import the helpers directly. Mocked at the lifecycle level so neither
test requires the anthropic SDK to be installed.
"""

from __future__ import annotations

import sys
from typing import List
from unittest.mock import MagicMock

from pydantic import BaseModel, Field, ValidationError


def _length_validation_error() -> ValidationError:
    """Trigger a real pydantic ValidationError for an over-long footnote.

    ``_build_validation_detail`` gates on ``isinstance(exc, ValidationError)``
    and falls back to ``str(exc)`` for anything else (e.g. a bare MagicMock),
    so the test must hand it a genuine ValidationError. The model mirrors the
    real post_report schema shape (``tables[i].footnote`` with a max_length
    cap) so ``loc`` == ("tables", 0, "footnote") and the pydantic v2 message
    is "String should have at most 400 characters" (contains "at most").
    """

    class _Table(BaseModel):
        footnote: str = Field(max_length=400)

    class _Outer(BaseModel):
        tables: List[_Table]

    try:
        _Outer(tables=[{"footnote": "x" * 412}])
    except ValidationError as exc:
        return exc
    raise AssertionError("expected ValidationError")


def _missing_required_error() -> ValidationError:
    """Trigger a real pydantic ValidationError for a missing required field.

    pydantic v2 emits "Field required" (the impl checks for "missing" or
    "required" in the message). ``loc`` == ("payload", "headline").
    """

    class _Payload(BaseModel):
        headline: str

    class _Outer(BaseModel):
        payload: _Payload

    try:
        _Outer(payload={})
    except ValidationError as exc:
        return exc
    raise AssertionError("expected ValidationError")


def _import_helpers():
    """Stub anthropic so session_runner import doesn't fail in CI."""
    if "anthropic" not in sys.modules:
        stub = MagicMock(name="anthropic_stub")
        stub.APIConnectionError = type("APIConnectionError", (Exception,), {})
        stub.APITimeoutError = type("APITimeoutError", (Exception,), {})
        stub.InternalServerError = type("InternalServerError", (Exception,), {})
        stub.RateLimitError = type("RateLimitError", (Exception,), {})
        stub.APIError = type("APIError", (Exception,), {})
        stub.BadRequestError = type("BadRequestError", (Exception,), {})
        sys.modules["anthropic"] = stub
    from session_runner import (  # pyright: ignore[reportMissingImports]
        _build_validation_detail,
        _should_dispatch_chart,
    )

    return _build_validation_detail, _should_dispatch_chart


# ── _build_validation_detail ────────────────────────────────────────


def test_validation_detail_reports_length_overage():
    _build, _ = _import_helpers()
    # A bare MagicMock is not an isinstance(exc, ValidationError), so the impl
    # correctly falls through to str(exc). Hand it a real pydantic
    # ValidationError so the field-level length-overage branch fires.
    err = _length_validation_error()
    payload = {
        "tables": [{"footnote": "x" * 412}],
    }
    out = _build(err, payload)
    assert "tables.0.footnote" in out
    assert "412 chars (max 400)" in out
    assert "Trim by 12 chars" in out


def test_validation_detail_handles_missing_required():
    _build, _ = _import_helpers()
    err = _missing_required_error()
    out = _build(err, {"payload": {}})
    assert "payload.headline" in out
    assert "required field is missing" in out


def test_validation_detail_falls_back_for_non_pydantic():
    _build, _ = _import_helpers()
    out = _build(ValueError("generic boom"), {})
    assert "generic boom" in out


def test_validation_detail_bounded_length():
    _build, _ = _import_helpers()
    fake_err = MagicMock()
    fake_err.errors.return_value = [{"loc": ("f",), "msg": "x" * 5000}]
    out = _build(fake_err, {})
    assert len(out) <= 1500


# ── _should_dispatch_chart ──────────────────────────────────────────


def test_chart_heuristic_fires_for_time_series():
    _, _chart = _import_helpers()
    payload = {
        "tables": [
            {
                "name": "quarterly_trend",
                "columns": ["quarter", "open_opps", "arr"],
                "rows": [
                    ["2024Q1", 480, 23],
                    ["2024Q2", 1500, 31],
                    ["2024Q3", 7284, 39],
                    ["2024Q4", 7553, 46],
                ],
            }
        ]
    }
    goal = _chart(payload)
    assert goal is not None
    assert "quarterly_trend" in goal
    assert "time series" in goal.lower()


def test_chart_heuristic_fires_for_categorical_distribution():
    _, _chart = _import_helpers()
    payload = {
        "tables": [
            {
                "name": "bucket_distribution",
                "columns": ["bucket_count", "opp_count"],
                "rows": [[1, 2], [2, 35], [3, 377], [4, 1620], [5, 4244]],
            }
        ]
    }
    goal = _chart(payload)
    assert goal is not None
    assert "bucket_distribution" in goal
    assert "bar chart" in goal.lower()


def test_chart_heuristic_skips_small_distributions():
    _, _chart = _import_helpers()
    payload = {
        "tables": [
            {
                "name": "tiny",
                "columns": ["a", "count"],
                "rows": [["x", 1], ["y", 2]],
            }
        ]
    }
    assert _chart(payload) is None


def test_chart_heuristic_skips_when_no_value_column():
    _, _chart = _import_helpers()
    payload = {
        "tables": [
            {
                "name": "id_list",
                "columns": ["id", "name"],
                "rows": [[i, f"x{i}"] for i in range(20)],
            }
        ]
    }
    assert _chart(payload) is None


def test_chart_heuristic_handles_missing_keys():
    _, _chart = _import_helpers()
    assert _chart(None) is None  # type: ignore[arg-type]
    assert _chart({}) is None
    assert _chart({"tables": "not_a_list"}) is None
