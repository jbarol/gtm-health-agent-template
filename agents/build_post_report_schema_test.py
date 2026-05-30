"""Tests for build_post_report_schema helpers.

Two goals:

1. Catch future schema additions that the dump function doesn't pick up
   (parametrized validation roundtrip per response type).
2. Catch accidental removal of decision-support tokens from the prompt
   block — agents need to see all five response_type names plus the
   per-class Pydantic titles so they can pick the right shape.

Run:
    python3 -m pytest agents/build_post_report_schema_test.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Self-contained sys.path setup so this test runs from either repo root or
# the agents/ directory.
_AGENTS_DIR = Path(__file__).parent
_ORCHESTRATOR_DIR = _AGENTS_DIR.parent / "orchestrator"
for _p in (_AGENTS_DIR, _ORCHESTRATOR_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from build_post_report_schema import (  # noqa: E402
    build_post_report_input_schema,
    build_schema_prompt_block,
    flatten_refs,
)
from response_schemas import RESPONSE_TYPES, parse_payload  # noqa: E402

# ---------------------------------------------------------------------------
# Sample payloads — one per response type. Keep these aligned with the
# golden fixtures in orchestrator/response_renderer_test.py so a change
# that breaks one breaks both.
# ---------------------------------------------------------------------------

_SAMPLE_PAYLOADS = {
    "quick_answer": {
        "metric": "Win rate, Q1 2026 new business",
        "value": "23.4% (n=148)",
        "as_of": "as of 2026-05-11 09:00 PT",
        "source": "Salesforce MCP, RecordType.Name=New Business",
    },
    "anomaly_alert": {
        "headline": "Partner-channel win rate dropped to 8%",
        "metric": "Win rate (partner)",
        "current_value": "8% (n=42)",
        "prior_value": "24% (n=58)",
        "benchmark": "20-30%",
        "severity": "critical",
        "evidence_summary": "3 new partners onboarded Q1 with no sales training.",
        "recommended_action": "Pause partner intake until training resumes.",
    },
    "ad_hoc_investigation_result": {
        "headline": "Win rate down 4.2pp this quarter",
        "key_metrics": [
            {
                "name": "Win rate",
                "current": "23.4%",
                "prior": "27.6%",
                "benchmark": "20-30%",
                "trend": "down",
            }
        ],
        "findings": [
            {
                "headline": "Partner channel win rate collapsed",
                "value": "8% (n=42)",
                "confidence": "HIGH",
                "severity": "critical",
            }
        ],
        "open_questions": ["Why did partner training stop?"],
    },
    "nightly_digest": {
        "headline": "Overnight: 2 portcos need attention",
        "portcos_with_action": ["Acme", "Acme"],
        "changes_overnight": [
            {
                "headline": "GRR dropped 1.2pp at Acme",
                "value": "84.3% (was 85.5%)",
                "confidence": "HIGH",
                "severity": "watch",
            }
        ],
    },
    "weekly_status": {
        "headline": "Weekly trajectory: 4 green, 2 yellow",
        "portco_lines": [
            {
                "portco": "Acme",
                "headline": "Pipeline coverage 3.1x, below target 4x",
                "severity": "watch",
            }
        ],
        "trajectory": "Pipeline coverage softening across portfolio.",
    },
}


# ---------------------------------------------------------------------------
# Schema integrity
# ---------------------------------------------------------------------------


def test_input_schema_shape():
    """The helper produces an object schema with the expected top-level shape."""
    schema = build_post_report_input_schema()

    assert schema["type"] == "object"
    assert set(schema["required"]) == {"response_type", "payload"}
    # 2026-05-14: schema gained ``theme`` + ``nightly_run_id`` for Task #11
    # (thread-per-theme nightly output consolidation). Both are optional —
    # ``required`` stays at ``response_type`` + ``payload``.
    assert set(schema["properties"].keys()) == {
        "response_type",
        "payload",
        "reply_to",
        "theme",
        "nightly_run_id",
    }

    enum = schema["properties"]["response_type"]["enum"]
    assert set(enum) == set(RESPONSE_TYPES.keys()), (
        "response_type enum must mirror RESPONSE_TYPES exactly — otherwise "
        "the deployed tool definition will reject valid payloads (or accept "
        "invalid ones)."
    )

    one_of = schema["properties"]["payload"]["oneOf"]
    assert len(one_of) == len(RESPONSE_TYPES), (
        "payload.oneOf must include every response type so the API can "
        "discriminate the payload shape per response_type."
    )


@pytest.mark.parametrize("response_type", list(RESPONSE_TYPES.keys()))
def test_sample_payload_validates_against_pydantic(response_type):
    """Every response_type has a sample payload that Pydantic accepts.

    Round-tripping through `parse_payload` is the same validation the
    orchestrator runs in `_dispatch_post_report`. If this fails for a new
    response type, the sample dict above is stale relative to the model.
    """
    payload = _SAMPLE_PAYLOADS[response_type]
    parsed = parse_payload(response_type, payload)
    assert parsed.response_type == response_type


@pytest.mark.parametrize("response_type", list(RESPONSE_TYPES.keys()))
def test_sample_payload_present_in_oneof_schema(response_type):
    """The sample payload's required fields all appear in the matching oneOf entry.

    Sanity check that `model_json_schema()` emits the field set we expect —
    catches accidental schema regressions (e.g. someone marks a required
    field Optional and the payload becomes ambiguous).
    """
    schema = build_post_report_input_schema()
    one_of = schema["properties"]["payload"]["oneOf"]

    # Match by title (model_json_schema sets title to the class name).
    cls = RESPONSE_TYPES[response_type]
    matching = [s for s in one_of if s.get("title") == cls.__name__]
    assert len(matching) == 1, (
        f"Expected exactly one oneOf entry titled {cls.__name__}; got {len(matching)}."
    )

    entry = matching[0]
    sample_keys = set(_SAMPLE_PAYLOADS[response_type].keys())
    schema_props = set(entry.get("properties", {}).keys())
    missing = sample_keys - schema_props
    assert not missing, (
        f"Sample payload uses fields not in the JSON Schema for {cls.__name__}: {missing}"
    )


# ---------------------------------------------------------------------------
# Prompt block
# ---------------------------------------------------------------------------


def test_prompt_block_contains_every_response_type_name():
    """The prompt block names every response type at least once."""
    block = build_schema_prompt_block()
    for name in RESPONSE_TYPES:
        assert name in block, f"Prompt block missing response_type name {name!r}"


def test_prompt_block_contains_every_pydantic_class_title():
    """Each Pydantic class title appears in the block (one per model).

    The model_json_schema() output stamps the class name into the `title`
    field; agents see these titles next to the field shapes and can use
    them to disambiguate payloads.
    """
    block = build_schema_prompt_block()
    for cls in RESPONSE_TYPES.values():
        assert cls.__name__ in block, f"Prompt block missing class title {cls.__name__}"


def test_prompt_block_json_dump_round_trips():
    """The fenced JSON inside the prompt block is parseable as JSON.

    Catches accidental escaping bugs — if the block ever stops being valid
    JSON, agents will hallucinate around the malformed payload.
    """
    block = build_schema_prompt_block()
    fence_start = block.find("```json")
    fence_end = block.rfind("```")
    assert fence_start != -1 and fence_end > fence_start, (
        "Prompt block must contain a ```json fenced section."
    )
    json_text = block[fence_start + len("```json") : fence_end].strip()
    parsed = json.loads(json_text)

    assert set(parsed.keys()) == set(RESPONSE_TYPES.keys())
    # Each entry is itself a JSON Schema object with at least a "properties" map.
    for name, entry in parsed.items():
        assert "properties" in entry, f"Schema for {name} missing properties"


# ---------------------------------------------------------------------------
# $ref flattener — Iteration 3 gating change. The Managed Agents custom-tool
# input_schema rejects $ref / $defs entries; Pydantic emits them for any
# nested BaseModel. flatten_refs() inlines refs against the root $defs and
# strips the $defs key so agents.update(..., tools=[POST_REPORT_TOOL]) stops
# 4xx-ing.
# ---------------------------------------------------------------------------


def test_flatten_refs_one_level_ref_is_inlined_and_defs_stripped():
    schema = {
        "type": "object",
        "properties": {
            "metric": {"$ref": "#/$defs/Metric"},
        },
        "$defs": {
            "Metric": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
            }
        },
    }
    out = flatten_refs(schema)
    assert "$defs" not in out
    assert out["properties"]["metric"] == {
        "type": "object",
        "properties": {"name": {"type": "string"}},
    }
    assert "$ref" not in json.dumps(out)


def test_flatten_refs_two_level_nested_refs_resolve_fully():
    """Mirrors the Finding → KeyMetric shape Pydantic emits in our schemas."""
    schema = {
        "type": "object",
        "properties": {
            "findings": {
                "type": "array",
                "items": {"$ref": "#/$defs/Finding"},
            }
        },
        "$defs": {
            "Finding": {
                "type": "object",
                "properties": {
                    "headline": {"type": "string"},
                    "metric": {"$ref": "#/$defs/KeyMetric"},
                },
            },
            "KeyMetric": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
            },
        },
    }
    out = flatten_refs(schema)
    serialized = json.dumps(out)
    assert "$ref" not in serialized
    assert "$defs" not in serialized
    finding = out["properties"]["findings"]["items"]
    assert finding["properties"]["metric"]["properties"] == {"name": {"type": "string"}}


def test_flatten_refs_cycle_raises_value_error():
    schema = {
        "$defs": {
            "A": {
                "type": "object",
                "properties": {"next": {"$ref": "#/$defs/A"}},
            }
        },
        "$ref": "#/$defs/A",
    }
    with pytest.raises(ValueError, match="cycle"):
        flatten_refs(schema)


def test_flatten_refs_accepts_legacy_definitions_form():
    """Pydantic v1 and some custom schemas use #/definitions/ instead of #/$defs/."""
    schema = {
        "type": "object",
        "properties": {"m": {"$ref": "#/definitions/Metric"}},
        "definitions": {"Metric": {"type": "string"}},
    }
    out = flatten_refs(schema)
    assert "$ref" not in json.dumps(out)
    assert "definitions" not in out
    assert out["properties"]["m"] == {"type": "string"}


def test_flatten_refs_unresolved_ref_raises():
    schema = {"properties": {"x": {"$ref": "#/$defs/Missing"}}}
    with pytest.raises(ValueError, match="not in"):
        flatten_refs(schema)


# ---------------------------------------------------------------------------
# End-to-end: the POST_REPORT input_schema we actually ship must be free of
# $ref and $defs — the API rejection criterion. This test is the contract
# the Managed Agents API enforces; if it fails, the deploy will 4xx.
# ---------------------------------------------------------------------------


def test_post_report_input_schema_has_no_refs_or_defs():
    schema = build_post_report_input_schema()
    serialized = json.dumps(schema)
    assert "$ref" not in serialized, (
        "POST_REPORT input_schema still contains $ref — Anthropic's custom-tool "
        "API will reject this on agents.update(). Run flatten_refs over every "
        "model_json_schema() call before assembling the tool definition."
    )
    assert "$defs" not in serialized, (
        "POST_REPORT input_schema still contains $defs — flatten_refs should "
        "strip the definitions block once refs are inlined."
    )


def test_prompt_block_has_no_refs_or_defs():
    """Prompt-side schemas should match the tool-side schemas exactly.

    Otherwise the model sees a $ref shape in its prompt that the tool no
    longer accepts — easy way to ship a self-contradicting contract.
    """
    block = build_schema_prompt_block()
    assert "$ref" not in block
    assert "$defs" not in block
