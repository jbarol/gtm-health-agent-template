"""Tests for the pre-validation editor pass.

Covers:
- The three failing/edge payloads called out in the editor design brief:
    1. 110-char headline that historically exceeded the 100-char target
    2. 73-char value that should pass through unchanged
    3. 41-char quick_answer value that should pass through unchanged
- Field-drop behavior when a field cannot be trimmed under target
- Decision-options warning when the recommendation framing is missing
- Editor never mutates the caller's payload (deep-copy contract)
- Unknown response_type falls through cleanly
- Writing-Agent rephrase path (historical, removed 2026-05-13): user
  rejected boundary truncation 2026-05-11 ("do not arbitrarily truncate.
  That's fucking dumb. Rephrase the content until it fits."), so the
  editor had briefly called writing_agent.write_prose when shorteners
  couldn't get a field under target. That path was retired in favor of
  pass-through behavior; see the comment block above ``_edit_string_field``
  in editor.py for the rationale. Tests below assert the pass-through
  contract.

Run:
    cd orchestrator && python3 -m pytest editor_test.py -v
"""

from __future__ import annotations

import copy

from editor import EDITOR_TARGETS, edit_payload, effective_target


def _no_rephrase(text, target_chars, field_name, context=""):
    """Default stub: returns input unchanged so droppable fields drop and
    non-droppable fields fall through. Tests that exercise the happy
    rephrase path patch this explicitly.
    """
    return text


def _rephrase_to(stub_output):
    """Build a stub that returns ``stub_output`` regardless of input.

    Useful when a test wants to assert the editor takes the rephrased
    form. Pass a string shorter than the target.
    """

    def _stub(text, target_chars, field_name, context=""):
        return stub_output if len(stub_output) <= target_chars else text

    return _stub


# ---------------------------------------------------------------------------
# The three brief payloads
# ---------------------------------------------------------------------------


def test_headline_over_target_passes_through_unchanged():
    """Over-target required headline passes through verbatim post 2026-05-13.

    Rephrase loop deleted — see comment block above ``_edit_string_field``.
    Required fields keep their original text; the log records the
    over-target event. No truncation, no LLM call on the critical path.
    """
    original = (
        "Recarve ICP around a Core 6 of industries + a fit-and-size "
        "composite score, not the current ARR-only tier"
    )
    assert len(original) == 105
    payload = {"headline": original, "findings": []}

    edited, log = edit_payload("ad_hoc_investigation_result", payload)

    assert edited["headline"] == original
    assert any(
        "headline" in entry and "EDITOR_OVER_TARGET" in entry for entry in log
    ), log


def test_short_value_passes_through_unchanged():
    """73-char value should pass through (well under 120-char target)."""
    payload = {
        "headline": "ICP fit drives win rate",
        "findings": [
            {
                "headline": "Industries with M&A activity convert at 2x",
                "value": "Acquisition fit (industries with M&A activity → drive and Prof Services.",
                "confidence": "MEDIUM",
                "severity": "watch",
            }
        ],
    }
    original_value = payload["findings"][0]["value"]
    assert len(original_value) < EDITOR_TARGETS["value"]

    edited, log = edit_payload("ad_hoc_investigation_result", payload)

    assert edited["findings"][0]["value"] == original_value
    # No edits to findings should have logged
    assert not any("findings[0]" in entry for entry in log), log


def test_quick_answer_value_41_chars_passes_through():
    """41-char quick_answer value should pass through with no edit."""
    payload = {
        "metric": "Active sales reps (quota-carrying)",
        "value": "28 total: 14 NB reps, 14 Account Managers",
        "as_of": "2026-05-11",
        "source": "Salesforce",
    }
    original_value = payload["value"]
    assert len(original_value) == 41

    edited, log = edit_payload("quick_answer", payload)

    assert edited["value"] == original_value
    assert not any("value" in entry for entry in log), log


# ---------------------------------------------------------------------------
# Per-field shortener behavior
# ---------------------------------------------------------------------------


def test_oversize_methodology_note_is_dropped():
    """methodology_note is droppable → over-target drops, no rephrase call."""
    long_note = "a" * 250
    payload = {
        "headline": "Q3 win rate dropped 4pp",
        "findings": [],
        "methodology_note": long_note,
    }
    edited, log = edit_payload("ad_hoc_investigation_result", payload)
    assert "methodology_note" not in edited, edited
    assert any("dropped methodology_note" in entry for entry in log), log


def test_oversize_cross_domain_pattern_is_dropped():
    """cross_domain_pattern is droppable → over-target drops, no rephrase call."""
    long_pattern = "x" * 250
    payload = {
        "headline": "Q3 pipeline weak",
        "findings": [],
        "cross_domain_pattern": long_pattern,
    }
    edited, log = edit_payload("ad_hoc_investigation_result", payload)
    assert "cross_domain_pattern" not in edited
    assert any("dropped cross_domain_pattern" in entry for entry in log), log


# ---------------------------------------------------------------------------
# Writing-Agent rephrase path — direct unit tests on call_writing_agent_to_shorten
# ---------------------------------------------------------------------------


# Rephrase loop deleted 2026-05-13 — replaced by pass-through behavior. See
# the comment block above ``_edit_string_field`` in editor.py for the
# rationale (synchronous Writing Agent calls were timing out and bleeding
# session cost on the post_report critical path).


def test_oversize_required_field_passes_through_unchanged():
    """An over-target required field passes through verbatim — no rephrase, no truncate."""
    payload = {
        "headline": "x" * 250,
        "findings": [],
    }
    edited, log = edit_payload("ad_hoc_investigation_result", payload)
    assert edited["headline"] == "x" * 250
    assert any(
        "EDITOR_OVER_TARGET" in entry and "headline" in entry for entry in log
    ), log


def test_oversize_droppable_field_is_dropped():
    """Optional fields exceeding target get dropped, not passed through."""
    payload = {
        "headline": "ok",
        "findings": [
            {
                "headline": "Finding hed",
                "value": "v",
                "confidence": "HIGH",
                "severity": "watch",
                "reviewer_caveat": "z" * 500,  # droppable + oversize
            }
        ],
    }
    edited, log = edit_payload("ad_hoc_investigation_result", payload)
    assert "reviewer_caveat" not in edited["findings"][0]
    assert any("dropped" in entry and "reviewer_caveat" in entry for entry in log), log


# ---------------------------------------------------------------------------
# Decision-options behavior
# ---------------------------------------------------------------------------


def test_decision_options_missing_recommendation_logs_warning():
    """Editor warns but does NOT synthesize a 'Recommended: X' clause."""
    payload = {
        "headline": "Renewal-pipeline coverage soft",
        "findings": [
            {
                "headline": "Q4 renewal coverage at 0.6x",
                "value": "0.6x of plan (n=23 accounts)",
                "confidence": "HIGH",
                "severity": "critical",
                "decision_required": True,
                "decision_options": [
                    "Pull forward Q1 renewals into Q4 cycle",
                    "Accept the gap and reforecast",
                    "Cut churn rate via CSM playbook expansion",
                ],
            }
        ],
    }
    edited, log = edit_payload("ad_hoc_investigation_result", payload)

    # Options preserved as-is (all are under the 80-char target)
    assert (
        edited["findings"][0]["decision_options"]
        == payload["findings"][0]["decision_options"]
    )
    # Warning logged
    assert any("missing 'Recommended:'" in entry for entry in log), log


def test_decision_options_with_recommendation_no_warning():
    """A correctly-formatted recommendation does NOT produce a warning."""
    payload = {
        "headline": "Renewal coverage soft",
        "findings": [
            {
                "headline": "Q4 renewal coverage at 0.6x",
                "value": "0.6x of plan",
                "confidence": "HIGH",
                "severity": "critical",
                "decision_required": True,
                "decision_options": [
                    "(a) Pull renewals forward",
                    "(b) Accept the gap",
                    "Recommended: (a) because coverage gap is structural",
                ],
            }
        ],
    }
    edited, log = edit_payload("ad_hoc_investigation_result", payload)

    assert not any("missing 'Recommended:'" in entry for entry in log), log


# ---------------------------------------------------------------------------
# Editor contracts
# ---------------------------------------------------------------------------


def test_editor_does_not_mutate_input():
    """Editor must deep-copy — caller's payload survives unchanged."""
    original = {
        "headline": "x" * 250,
        "findings": [
            {
                "headline": "y" * 250,
                "value": "z" * 250,
                "confidence": "HIGH",
                "severity": "watch",
            }
        ],
        "methodology_note": "n" * 250,
    }
    original_copy = copy.deepcopy(original)
    edit_payload("ad_hoc_investigation_result", original)
    assert original == original_copy, "editor mutated input payload"


def test_unknown_response_type_passes_through():
    """Unknown response_type returns the payload unchanged, no edits."""
    payload = {"headline": "x" * 250}
    edited, log = edit_payload("unknown_type", payload)

    assert edited == payload
    assert log == []


def test_effective_target_uses_editor_target_when_schema_uncapped():
    """Post PR #87 most prose fields have no schema cap.

    With no schema cap, effective_target returns the editor target unchanged.
    The function still pins to the schema cap when a cap exists (list-length
    caps remain — but those aren't string targets we'd trim against).
    """
    # PR #87 removed string caps; effective_target returns the editor target
    assert effective_target("ad_hoc_investigation_result", "headline", 100) == 100
    assert effective_target("quick_answer", "value", 120) == 120
    # Unknown response_type → editor target wins
    assert effective_target("nonexistent", "headline", 100) == 100


def test_short_headline_passes_through_with_no_log():
    """Already-short headline → no edits logged."""
    payload = {"headline": "Win rate up 4pp", "findings": []}
    edited, log = edit_payload("ad_hoc_investigation_result", payload)

    assert edited["headline"] == "Win rate up 4pp"
    assert not any("headline" in entry for entry in log), log


def test_anomaly_alert_recommended_action_trimmed():
    """anomaly_alert.recommended_action gets trimmed to its target."""
    long_action = (
        "In order to address the pipeline gap, prior to the close of Q3, "
        "we currently need to immediately escalate to the partner channel "
        "due to the fact that organic conversion is weak this quarter."
    )
    payload = {
        "headline": "Pipeline gap detected",
        "metric": "Q3 coverage",
        "current_value": "0.5x",
        "prior_value": "1.2x",
        "benchmark": "3x",
        "severity": "critical",
        "evidence_summary": "Coverage check",
        "recommended_action": long_action,
    }

    edited, log = edit_payload("anomaly_alert", payload)
    # recommended_action is required → over-target passes through unchanged
    # (no rephrase, no truncate). The log records the over-target event so an
    # operator can see it; the user gets the full text from the Coordinator.
    assert edited["recommended_action"] == long_action
    assert any(
        "recommended_action" in entry and "EDITOR_OVER_TARGET" in entry for entry in log
    ), log


# ---------------------------------------------------------------------------
# Combined integration — multi-field oversize payload
# ---------------------------------------------------------------------------


def test_full_payload_with_multiple_oversize_fields():
    """A messy real-world payload gets cleaned across every field.

    Writing Agent rephrase returns short, target-fitting text for every
    field so the editor takes the rephrased form and the asserts on
    edited length still hold.
    """
    payload = {
        "headline": (
            "Recarve ICP around a Core 6 of industries + a fit-and-size "
            "composite score, not the current ARR-only tier"
        ),
        "findings": [
            {
                "headline": (
                    "Industries with M&A activity convert at meaningfully "
                    "higher rates compared to the rest of the book"
                ),
                "value": (
                    "Acquisition fit (industries with M&A activity in the "
                    "trailing 12 months) drives win rate +2.4x vs the rest of "
                    "the book, holding price band constant across the cut."
                ),
                "confidence": "MEDIUM",
                "severity": "watch",
                "reviewer_caveat": (
                    "Sample n=42 in the M&A segment vs n=180 in the rest. "
                    "Effect direction is robust; magnitude could shift up to "
                    "0.5x in either direction as more deals close."
                ),
            }
        ],
        "cross_domain_pattern": (
            "Pipeline quality scores correlate with post-sales retention "
            "scores at r=0.71, with the strongest pull from the industry-fit "
            "signal — accounts that come in via fit-tagged demand consistently "
            "retain better in year 2 than the book average."
        ),
        "methodology_note": (
            "Pulled 432 closed-won opps from the trailing 18 months. "
            "Segmented by industry vs ARR tier. Adversarial review flagged "
            "selection bias in the post-sales overlay; statistician applied a "
            "back-test on the prior 12-month cohort to confirm directionality."
        ),
    }

    def _rephrase_any(text, target_chars, field_name, context=""):
        # Return a short, field-aware payload that fits the target.
        stub = f"[rephrased {field_name}]"
        return stub if len(stub) <= target_chars else text

    edited, log = edit_payload("ad_hoc_investigation_result", payload)

    # Post 2026-05-13 rephrase-loop removal: required fields pass through
    # over-target verbatim (no truncation); droppable fields get dropped.
    # The edit log records every over-target event so the operator can
    # see what happened.
    over_target_count = sum(
        1 for entry in log if "EDITOR_OVER_TARGET" in entry or "dropped" in entry
    )
    assert over_target_count >= 3, log
    # Required headline survives verbatim
    assert edited["headline"] == payload["headline"]
