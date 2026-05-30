"""Build the post_report tool's JSON Schema and prompt-side schema block.

Derives both artifacts from the Pydantic models in
`orchestrator/response_schemas.py` so the tool definition and the
`<output_format>` block in each prompt can never drift from the schemas
themselves.

Two callers:

* `agents/setup_agents.py` and `agents/add_post_report_tool.py` use
  `build_post_report_input_schema()` to populate `POST_REPORT_TOOL.input_schema`.
* `agents/update_prompts.py` uses `build_schema_prompt_block()` to embed the
  literal JSON Schema in the Coordinator and Quick Answer prompts at
  module-load time. Re-deploying prompts re-syncs the schema dump.

Plan #32 (Option C): tool-level enforcement + prompt-level guidance, both
derived. See docs/plans/32-json-schema-delivery-dx-2.md.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Add orchestrator/ to sys.path so we can import response_schemas without
# requiring callers to set PYTHONPATH. setup_agents.py, update_prompts.py,
# and add_post_report_tool.py all live in agents/ — this insert keeps each
# call site dependency-free.
_ORCHESTRATOR_DIR = Path(__file__).parent.parent / "orchestrator"
if str(_ORCHESTRATOR_DIR) not in sys.path:
    sys.path.insert(0, str(_ORCHESTRATOR_DIR))

from response_schemas import RESPONSE_TYPES  # noqa: E402


def flatten_refs(schema: dict) -> dict:
    """Inline `$ref` entries from a Pydantic-generated JSON Schema.

    Anthropic's custom-tool `input_schema` rejects schemas containing `$ref`;
    Pydantic emits one per nested `BaseModel`. This walker resolves each
    `{"$ref": "#/$defs/X"}` against the root `$defs` (also accepts the
    `#/definitions/` form some Pydantic versions emit), strips `$defs` /
    `definitions` from every level, and returns a `$ref`-free schema. Resolved
    defs are themselves recursively flattened so chains collapse fully.

    Memoizes resolved defs to avoid quadratic blowup on diamond-shaped schemas
    (two refs pointing at the same def). Raises `ValueError` on cycles.
    """
    defs = schema.get("$defs") or schema.get("definitions") or {}
    resolved: dict[str, dict] = {}

    def _resolve(name: str, in_flight: set[str]) -> dict:
        if name in resolved:
            return resolved[name]
        if name in in_flight:
            raise ValueError(f"$ref cycle detected resolving '{name}'")
        if name not in defs:
            raise ValueError(f"Unresolved $ref '#/$defs/{name}' — not in $defs")
        in_flight.add(name)
        out = _walk(defs[name], in_flight)
        in_flight.discard(name)
        assert isinstance(out, dict), f"$defs/{name} did not resolve to a dict"
        resolved[name] = out
        return out

    def _walk(node, in_flight: set[str]):
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                if ref.startswith("#/$defs/"):
                    return _resolve(ref[len("#/$defs/") :], in_flight)
                if ref.startswith("#/definitions/"):
                    return _resolve(ref[len("#/definitions/") :], in_flight)
                raise ValueError(f"Unsupported $ref form: {ref!r}")
            return {
                k: _walk(v, in_flight)
                for k, v in node.items()
                if k not in ("$defs", "definitions")
            }
        if isinstance(node, list):
            return [_walk(item, in_flight) for item in node]
        return node

    result = _walk(schema, set())
    assert isinstance(result, dict), "top-level schema must walk to a dict"
    return result


def build_post_report_input_schema() -> dict:
    """JSON Schema for the post_report tool's `input_schema` field.

    The `payload` property is a `oneOf` over every Pydantic model in
    `RESPONSE_TYPES`. Each sub-schema is `flatten_refs`-ed because the
    Managed Agents custom-tool API rejects `$ref` entries — Pydantic emits
    these for any nested BaseModel (KeyMetric, Finding, TableBlock, ...). The
    orchestrator's `_dispatch_post_report` validates a second time via
    Pydantic before rendering, so the API's `oneOf` adherence is
    belt-and-suspenders rather than load-bearing.
    """
    return {
        "type": "object",
        "properties": {
            "response_type": {
                "type": "string",
                "enum": list(RESPONSE_TYPES.keys()),
                "description": "Which schema the payload conforms to.",
            },
            "payload": {
                "oneOf": [
                    flatten_refs(cls.model_json_schema())
                    for cls in RESPONSE_TYPES.values()
                ],
                "description": (
                    "Payload matching the schema for the given response_type."
                ),
            },
            "reply_to": {
                "type": "string",
                "description": (
                    "Slack message timestamp to reply in thread (optional)."
                ),
            },
            "theme": {
                "type": "string",
                "enum": [
                    "pipeline_review",
                    "forecast_analysis",
                    "dream_plan",
                    "investigation_finding",
                    "cost_report",
                ],
                "description": (
                    "Optional. When set on a CRON-driven post_report (nightly "
                    "dream / forecast / investigation / cost / pipeline review), "
                    "the orchestrator routes every artifact for the same "
                    "(nightly_run_id, theme, channel) tuple into ONE Slack "
                    "thread. The first post_report per theme creates a parent "
                    "message containing the agent's summary line + 'More details "
                    "in thread ↓'; subsequent calls reply in the parent's "
                    "thread. Leave unset for ad-hoc Slack-question flows — the "
                    "existing reply_to / thread_ts plumbing handles those."
                ),
            },
            "nightly_run_id": {
                "type": "string",
                "description": (
                    "Optional. Identifier for the cron run that owns this "
                    "post_report. Used together with `theme` to group every "
                    "artifact emitted by a single nightly run into one Slack "
                    "thread. Conventionally a date-keyed string like "
                    "`nightly-2026-05-14` set by the cron entry point and "
                    "threaded through every post_report it triggers. When "
                    "omitted, the orchestrator falls back to a UTC-date "
                    "default — fine for the single-run-per-day case but pass "
                    "an explicit value when two runs of the same cron can "
                    "overlap (e.g. a retried investigation)."
                ),
            },
        },
        "required": ["response_type", "payload"],
    }


def build_schema_prompt_block() -> str:
    """Human-readable JSON Schema block for embedding in agent system prompts.

    Decision-support prose ("Use quick_answer for single-fact lookups …")
    stays in the prompt around this block. The block itself is a literal
    JSON dump of every response type's schema — agents see the same shape the
    tool definition enforces. We also `flatten_refs` here so the prompt-side
    schemas match what the tool actually accepts (no surprise `$ref`s for the
    model to reason about).
    """
    schemas = {
        name: flatten_refs(cls.model_json_schema())
        for name, cls in RESPONSE_TYPES.items()
    }
    return (
        "Full JSON Schema for each response_type (the post_report tool will "
        "reject payloads that don't match):\n```json\n"
        + json.dumps(schemas, indent=2)
        + "\n```"
    )
