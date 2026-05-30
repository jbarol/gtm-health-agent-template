"""Pre-validation editor pass: audit and reduce text before printing.

Why this exists
---------------
The Coordinator (and other agents) emit structured payloads bound for
``post_report``. Pydantic schemas in ``response_schemas.py`` enforce hard
``max_length`` caps on every string field. When an agent overruns a cap by
one or two characters, the entire post fails validation and the user sees
``[POST_REPORT_FAILED] schema_validation_failed`` instead of an answer.

The reactive fix has been to bump the cap. That's a safety net. The real
fix is an editor pass that audits the proposed message BEFORE validation
and trims/rewrites it down to a tight, audience-appropriate shape.

This module is the deterministic first pass. It runs synchronously in
``_dispatch_post_report`` before ``response_schemas.parse_payload`` —
no LLM call, no network, no shared state. It edits in-place on a deep
copy and returns the edited payload plus a list of edit-log entries the
caller logs at INFO level so the operator can see what the editor did.

Design rules
------------
1. Deterministic. No LLM call. Pure string ops + glossary lookup.
2. Audience-aware targets. Targets are tight values designed for scannable
   Slack output (headline 100, value 120, etc.) — they pull content toward a
   phone-readable size BEFORE Pydantic ever sees it. PR #87 removed the
   ``max_length`` caps from user-facing prose fields, so ``_schema_cap_for``
   now returns ``None`` for those fields and the EFFECTIVE target IS the
   editor target. Identifier-like fields (portco, TableBlock cells/headers)
   still have schema caps; for those the effective target is the smaller
   of the editor target and the schema cap. Schema's job is structure
   (extra=forbid, enum types, list counts), not text policing.
3. Conservative. Never invent content. If a field cannot be trimmed
   below its effective target without losing meaning, REMOVE it (drop
   optional fields entirely) rather than truncate mid-sentence.
4. Observability. Every edit is logged with the field path, original
   length, and final length so the operator can audit what shrank.
5. Editor's NON-job: don't fact-check, don't add findings, don't add
   a recommendation to a decision_options list that lacks one. Just
   trim. Upstream agents own correctness.

Public API
----------
    edit_payload(response_type, payload) -> (edited_payload, edit_log)
        Returns a deep-copied, edited payload and a list of human-readable
        edit-log strings (e.g. "dropped methodology_note (350 chars, target 180)").
"""

from __future__ import annotations

import copy
import logging
import re
from typing import Any

__all__ = [
    "edit_payload",
    "EDITOR_TARGETS",
    "effective_target",
]

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema cap lookup — keeps the editor target <= the schema's max_length so
# the editor never accidentally allows a field to slip above its cap. Done
# lazily to avoid a hard import-time dependency on response_schemas (which
# itself depends on pydantic).
# ---------------------------------------------------------------------------


def _schema_cap_for(response_type: str, field_path: str) -> int | None:
    """Return the schema's max_length for response_type.field_path, or None.

    Supports paths like ``headline``, ``methodology_note``,
    ``findings[N].value`` (the [N] index is treated as the Finding class).
    Returns None if the schema doesn't cap the field (or if response_schemas
    can't be imported — e.g. during a standalone unit test).
    """
    try:
        import response_schemas as rs
    except Exception:
        return None

    schema_cls = rs.RESPONSE_TYPES.get(response_type)
    if schema_cls is None:
        return None

    # Resolve nested paths like "findings[0].value" by walking into the
    # finding/cell sub-schema.
    sub_match = re.match(r"^(\w+)\[\d+\]\.(\w+)$", field_path)
    if sub_match:
        container_field = sub_match.group(1)
        sub_field = sub_match.group(2)
        container_info = schema_cls.model_fields.get(container_field)
        if container_info is None:
            return None
        # Pull the inner list element type. For list[Finding], that's Finding.
        item_type = None
        anno = container_info.annotation
        if hasattr(anno, "__args__") and anno.__args__:
            item_type = anno.__args__[0]
        if item_type is None or not hasattr(item_type, "model_fields"):
            return None
        info = item_type.model_fields.get(sub_field)
    else:
        info = schema_cls.model_fields.get(field_path)

    if info is None:
        return None
    for meta in info.metadata or ():
        if hasattr(meta, "max_length") and isinstance(meta.max_length, int):
            return meta.max_length
    return None


def effective_target(response_type: str, field_path: str, editor_target: int) -> int:
    """Return min(editor_target, schema_cap_for(field_path)).

    Ensures the editor never lets a field exceed its schema cap, while
    preserving the tighter editor target when the schema cap is looser
    (the common case after PR #85's cap bumps).
    """
    cap = _schema_cap_for(response_type, field_path)
    if cap is None:
        return editor_target
    return min(editor_target, cap)


# ---------------------------------------------------------------------------
# Per-field length targets (TIGHTER than the schema caps in response_schemas).
#
# Tightening below the schema cap means the editor pulls fields toward a
# scannable size BEFORE Pydantic ever sees them, so validation rejection on
# overrun becomes effectively impossible for the fields we control.
# ---------------------------------------------------------------------------

EDITOR_TARGETS: dict[str, int] = {
    "headline": 100,
    "value": 120,
    "cross_domain_pattern": 180,
    "methodology_note": 180,
    "decision_option": 80,
    "reviewer_caveat": 120,
    "recommended_action": 160,
}


# ---------------------------------------------------------------------------
# Deterministic shorteners REMOVED 2026-05-11 per user direction:
#     "I do not want any deterministic shortening."
#
# The previous implementation had a regex table that ran phrase substitutions
# (e.g. "in order to" -> "to", "composite score" -> "score") before falling
# through to the Writing Agent. The user rejected this entirely — every
# shortening decision now goes through the Writing Agent, which can make
# context-aware judgment calls instead of blind substitution.
#
# The Writing Agent is the SOLE shortening path. If it can't fit a field
# under target within its retry budget, the editor drops the field (if
# optional) or passes the original through unchanged (if required). No
# regex shortcuts, no character truncation.
# ---------------------------------------------------------------------------


def _apply_shorteners(text: str) -> str:
    """Deprecated. Retained only so old call sites don't crash during the
    deploy window; returns input unchanged. Remove after the editor PR is
    fully landed and tests are green."""
    # No-op per "no deterministic shortening" directive. Returns input
    # unchanged. Whitespace normalization moved into the Writing Agent's
    # rephrase output validation.
    return text


# ---------------------------------------------------------------------------
# Rephrase loop — REMOVED 2026-05-13.
#
# The editor used to call the Writing Agent in a 2-attempt × 10s loop to
# shorten any field that came back over its character target. In practice
# that loop fired up to a dozen times per post_report, every call timed
# out (Haiku creating a fresh session per call has too much overhead for
# the 10s wall-clock), and the resulting fallback was "pass the original
# text through unchanged" — exactly the same outcome we now get for free.
# Live impact on 2026-05-13 16:34 PT: 6 rephrase fields × 2 attempts × 10s
# = 2 minutes of dead session time + ~$1 of cache_read accrual per call,
# all to arrive at the same pass-through behavior.
#
# Length constraints belong in the Writing Agent's PRIMARY call (the
# write_prose dispatch from session_runner). The Coordinator's response
# schema already specifies field-level character targets that the model
# sees up-front. The editor's job is now reduced to: drop optional fields
# that overrun their target, and otherwise pass through. No additional
# LLM call on the post_report critical path.
#
# If we re-introduce a rephrase loop later, it MUST be (a) async, (b)
# pre-emptive (issued before post_report ships, not blocking it), or (c)
# moved into the Writing Agent's primary system prompt as a constraint.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Field-level edit primitive
# ---------------------------------------------------------------------------


def _edit_string_field(
    text: str,
    target: int,
    edit_log: list[str],
    field_path: str,
    *,
    droppable: bool,
    response_type: str = "",
) -> tuple[str | None, bool]:
    """Edit ``text`` to fit ``target`` chars.

    Returns ``(edited_text_or_None, was_edited)``.

    The contract:
    - If ``text`` already fits, return ``(text, False)`` — no edit needed.
    - Ask the Writing Agent (Haiku 4.5) to rephrase the field down to the
      target. User explicitly rejected deterministic shortening 2026-05-11:
          "I do not want any deterministic shortening."
          "do not arbitrarily truncate. That's fucking dumb.
           Rephrase the content until it fits."
      The Writing Agent is the SOLE shortening path.
    - If droppable and Writing Agent can't fit it, return ``(None, True)``
      — caller removes the field from the payload entirely.
    - If not droppable and Writing Agent can't fit it, log
      [EDITOR_REPHRASE_FAILED] and pass the original through unchanged.
      No truncation. Schema has no cap on prose fields (PR #87), so the
      original ships verbatim — the user sees the LLM's actual output
      rather than a regex-mangled version.
    """
    original_len = len(text)
    if original_len <= target:
        return text, False

    # The rephrase loop was deleted 2026-05-13. Length constraints belong in
    # the Writing Agent's PRIMARY composition turn (the Coordinator's
    # delegation in the multiagent runtime; was the write_prose custom tool
    # before 2026-05-27), not in a post-edit retry loop that blocks the
    # post_report critical path. See the comment block above
    # ``_edit_string_field`` for the live incident (6 fields × 2 attempts ×
    # 10s each = 2 min of dead session time + ~$1 cache_read bleed, with
    # the same pass-through outcome we now get for free).
    #
    # Drop optional fields that overrun; pass required fields through
    # unchanged. The renderer + prose_polish handle final Slack formatting
    # (block splits at 2900 chars).
    if droppable:
        edit_log.append(
            f"dropped {field_path} ({original_len} chars exceeds target {target})"
        )
        return None, True

    edit_log.append(
        f"[EDITOR_OVER_TARGET] {field_path} ({original_len} chars exceeds "
        f"target {target}; pass-through, no rephrase)"
    )
    return text, False


# ---------------------------------------------------------------------------
# Per-response-type editors
# ---------------------------------------------------------------------------


def _edit_finding(
    finding: dict, edit_log: list[str], idx: int, response_type: str
) -> None:
    """Mutate one Finding dict in place.

    response_type is passed through so we can look up the Finding subschema's
    actual cap on each field — keeps the editor target <= the schema cap.
    """
    headline = finding.get("headline")
    if isinstance(headline, str):
        new_headline, _ = _edit_string_field(
            headline,
            effective_target(
                response_type,
                f"findings[{idx}].headline",
                EDITOR_TARGETS["headline"],
            ),
            edit_log,
            f"findings[{idx}].headline",
            droppable=False,  # headline is required
        )
        if new_headline is not None:
            finding["headline"] = new_headline

    value = finding.get("value")
    if isinstance(value, str):
        new_value, _ = _edit_string_field(
            value,
            effective_target(
                response_type,
                f"findings[{idx}].value",
                EDITOR_TARGETS["value"],
            ),
            edit_log,
            f"findings[{idx}].value",
            droppable=False,  # value is required
        )
        if new_value is not None:
            finding["value"] = new_value

    caveat = finding.get("reviewer_caveat")
    if isinstance(caveat, str):
        new_caveat, edited = _edit_string_field(
            caveat,
            effective_target(
                response_type,
                f"findings[{idx}].reviewer_caveat",
                EDITOR_TARGETS["reviewer_caveat"],
            ),
            edit_log,
            f"findings[{idx}].reviewer_caveat",
            droppable=True,
        )
        if edited and new_caveat is None:
            finding.pop("reviewer_caveat", None)
        elif new_caveat is not None:
            finding["reviewer_caveat"] = new_caveat

    options = finding.get("decision_options")
    if isinstance(options, list):
        new_options: list[str] = []
        for i, opt in enumerate(options):
            if not isinstance(opt, str):
                new_options.append(opt)
                continue
            edited_opt, _ = _edit_string_field(
                opt,
                EDITOR_TARGETS["decision_option"],
                edit_log,
                f"findings[{idx}].decision_options[{i}]",
                droppable=False,  # an option is part of a decision; never drop it
            )
            new_options.append(edited_opt if edited_opt is not None else opt)
        finding["decision_options"] = new_options

        # Editor's NON-job: warn but DO NOT add a recommendation if missing.
        # Upstream agents own the "Recommended: X because Y" framing.
        if finding.get("decision_required") and new_options:
            has_recommendation = any(
                "recommended:" in opt.lower() for opt in new_options
            )
            if not has_recommendation:
                edit_log.append(
                    f"WARN findings[{idx}].decision_options missing "
                    f"'Recommended:' clause (editor will not synthesize one)"
                )


def _edit_ad_hoc(payload: dict, edit_log: list[str]) -> dict:
    """Edit an ad_hoc_investigation_result payload in place. Returns payload."""
    rt = "ad_hoc_investigation_result"
    headline = payload.get("headline")
    if isinstance(headline, str):
        new_headline, _ = _edit_string_field(
            headline,
            effective_target(rt, "headline", EDITOR_TARGETS["headline"]),
            edit_log,
            "headline",
            droppable=False,
        )
        if new_headline is not None:
            payload["headline"] = new_headline

    findings = payload.get("findings")
    if isinstance(findings, list):
        for i, f in enumerate(findings):
            if isinstance(f, dict):
                _edit_finding(f, edit_log, i, rt)

    pattern = payload.get("cross_domain_pattern")
    if isinstance(pattern, str):
        new_pattern, edited = _edit_string_field(
            pattern,
            effective_target(
                rt, "cross_domain_pattern", EDITOR_TARGETS["cross_domain_pattern"]
            ),
            edit_log,
            "cross_domain_pattern",
            droppable=True,
        )
        if edited and new_pattern is None:
            payload.pop("cross_domain_pattern", None)
        elif new_pattern is not None:
            payload["cross_domain_pattern"] = new_pattern

    note = payload.get("methodology_note")
    if isinstance(note, str):
        new_note, edited = _edit_string_field(
            note,
            effective_target(
                rt, "methodology_note", EDITOR_TARGETS["methodology_note"]
            ),
            edit_log,
            "methodology_note",
            droppable=True,
        )
        if edited and new_note is None:
            payload.pop("methodology_note", None)
        elif new_note is not None:
            payload["methodology_note"] = new_note

    return payload


def _edit_quick_answer(payload: dict, edit_log: list[str]) -> dict:
    """Edit a quick_answer payload in place. Returns payload."""
    rt = "quick_answer"
    value = payload.get("value")
    if isinstance(value, str):
        new_value, _ = _edit_string_field(
            value,
            effective_target(rt, "value", EDITOR_TARGETS["value"]),
            edit_log,
            "value",
            droppable=False,
        )
        if new_value is not None:
            payload["value"] = new_value
    return payload


def _edit_anomaly(payload: dict, edit_log: list[str]) -> dict:
    """Edit an anomaly_alert payload in place. Returns payload."""
    rt = "anomaly_alert"
    headline = payload.get("headline")
    if isinstance(headline, str):
        new_headline, _ = _edit_string_field(
            headline,
            effective_target(rt, "headline", EDITOR_TARGETS["headline"]),
            edit_log,
            "headline",
            droppable=False,
        )
        if new_headline is not None:
            payload["headline"] = new_headline

    action = payload.get("recommended_action")
    if isinstance(action, str):
        new_action, _ = _edit_string_field(
            action,
            effective_target(
                rt, "recommended_action", EDITOR_TARGETS["recommended_action"]
            ),
            edit_log,
            "recommended_action",
            droppable=False,
        )
        if new_action is not None:
            payload["recommended_action"] = new_action
    return payload


def _edit_nightly_digest(payload: dict, edit_log: list[str]) -> dict:
    """Edit a nightly_digest payload in place. Returns payload."""
    rt = "nightly_digest"
    headline = payload.get("headline")
    if isinstance(headline, str):
        new_headline, _ = _edit_string_field(
            headline,
            effective_target(rt, "headline", EDITOR_TARGETS["headline"]),
            edit_log,
            "headline",
            droppable=False,
        )
        if new_headline is not None:
            payload["headline"] = new_headline

    changes = payload.get("changes_overnight")
    if isinstance(changes, list):
        for i, f in enumerate(changes):
            if isinstance(f, dict):
                _edit_finding(f, edit_log, i, rt)
    return payload


def _edit_weekly_status(payload: dict, edit_log: list[str]) -> dict:
    """Edit a weekly_status payload in place. Returns payload."""
    rt = "weekly_status"
    headline = payload.get("headline")
    if isinstance(headline, str):
        new_headline, _ = _edit_string_field(
            headline,
            effective_target(rt, "headline", EDITOR_TARGETS["headline"]),
            edit_log,
            "headline",
            droppable=False,
        )
        if new_headline is not None:
            payload["headline"] = new_headline
    return payload


_EDITORS = {
    "ad_hoc_investigation_result": _edit_ad_hoc,
    "quick_answer": _edit_quick_answer,
    "anomaly_alert": _edit_anomaly,
    "nightly_digest": _edit_nightly_digest,
    "weekly_status": _edit_weekly_status,
}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def edit_payload(
    response_type: str, payload: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Audit and reduce ``payload`` to fit audience-appropriate targets.

    Runs BEFORE Pydantic validation in ``_dispatch_post_report``. The
    contract:
    - Returns a NEW dict (deep copy); never mutates the caller's input.
    - Returns the original payload (still a deep copy) if response_type
      is unknown — keeps the upstream contract that unknown types fall
      through to the schema-validation error path.
    - Logs every edit to the returned edit_log list. The caller is
      responsible for emitting these at INFO level.
    """
    if not isinstance(payload, dict):
        return payload, []

    edited = copy.deepcopy(payload)
    edit_log: list[str] = []

    editor_fn = _EDITORS.get(response_type)
    if editor_fn is None:
        return edited, edit_log

    editor_fn(edited, edit_log)
    return edited, edit_log
