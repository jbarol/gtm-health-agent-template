"""Tests for decision-grade schema fields (Plan #29 / Task #15).

Covers:
- Defaults applied when fields omitted
- Validator: decision_required=True with empty decision_options raises
- Validator: decision_required=False is valid with empty decision_options
- Renderer: priority tag appears in summary mode
- Renderer: decision options appear in expanded mode when decision_required
- Renderer: intervention line appears in expanded mode when set
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from response_renderer import render
from response_schemas import (
    AdHocInvestigationResponse,
    AnomalyAlertResponse,
    Finding,
    PortcoLine,
    TableBlock,
    parse_payload,
)


# ---------------------------------------------------------------------------
# Defaults applied when fields omitted
# ---------------------------------------------------------------------------


def test_finding_defaults_applied_when_omitted():
    """Finding emitted without decision-grade fields gets sensible defaults."""
    f = Finding(
        headline="Win rate dropped",
        value="23%",
        confidence="HIGH",
        severity="watch",
    )
    assert f.priority == "P2"
    assert f.urgency == "monitor"
    assert f.decision_required is False
    assert f.decision_options == []
    assert f.changed_since_last is None
    assert f.recommended_intervention is None


def test_anomaly_alert_defaults_applied_when_omitted():
    """AnomalyAlert without new fields gets defaults."""
    a = AnomalyAlertResponse(
        headline="Win rate drop",
        metric="win_rate",
        current_value="8%",
        prior_value="24%",
        benchmark="20-30%",
        severity="critical",
        evidence_summary="Partner channel collapse",
        recommended_action="Pause partner intake",
    )
    assert a.priority == "P2"
    assert a.urgency == "monitor"
    assert a.decision_required is False
    assert a.decision_options == []
    assert a.changed_since_last is None
    assert a.recommended_intervention is None


def test_portco_line_defaults_applied_when_omitted():
    """PortcoLine without new fields gets defaults."""
    p = PortcoLine(portco="Acme", headline="Pipeline soft", severity="watch")
    assert p.priority == "P2"
    assert p.urgency == "monitor"
    assert p.changed_since_last is None
    assert p.recommended_intervention is None


# ---------------------------------------------------------------------------
# Validator: decision_required ↔ decision_options consistency
# ---------------------------------------------------------------------------


def test_finding_decision_required_without_options_raises():
    """decision_required=True with empty decision_options must raise."""
    with pytest.raises(ValidationError) as exc:
        Finding(
            headline="Pick a path",
            value="—",
            confidence="HIGH",
            severity="watch",
            decision_required=True,
            decision_options=[],
        )
    assert "decision_options required" in str(exc.value)


def test_finding_decision_required_false_with_empty_options_valid():
    """decision_required=False with empty decision_options is valid."""
    f = Finding(
        headline="Just informational",
        value="—",
        confidence="HIGH",
        severity="info",
        decision_required=False,
        decision_options=[],
    )
    assert f.decision_required is False
    assert f.decision_options == []


def test_anomaly_alert_decision_required_without_options_raises():
    """AnomalyAlert validator parallels Finding."""
    with pytest.raises(ValidationError) as exc:
        AnomalyAlertResponse(
            headline="Anomaly",
            metric="win_rate",
            current_value="8%",
            prior_value="24%",
            benchmark="20-30%",
            severity="critical",
            evidence_summary="x",
            recommended_action="y",
            decision_required=True,
            decision_options=[],
        )
    assert "decision_options required" in str(exc.value)


def test_finding_decision_required_with_options_valid():
    """decision_required=True with non-empty options validates."""
    f = Finding(
        headline="Choose",
        value="—",
        confidence="HIGH",
        severity="critical",
        decision_required=True,
        decision_options=["Pause partner intake", "Add coaching", "Investigate root"],
    )
    assert f.decision_required is True
    assert len(f.decision_options) == 3


# ---------------------------------------------------------------------------
# Renderer: priority tag in summary mode
# ---------------------------------------------------------------------------


def test_priority_tag_in_ad_hoc_summary():
    """Summary mode prepends *[P1]* to top finding headline."""
    payload = {
        "headline": "Win rate down",
        "findings": [
            {
                "headline": "Partner channel collapse",
                "value": "8%",
                "confidence": "HIGH",
                "severity": "critical",
                "priority": "P1",
                "urgency": "this_week",
            }
        ],
    }
    response = parse_payload("ad_hoc_investigation_result", payload)
    out = render(response, mode="summary", target="slack")
    assert "*[P1]*" in out
    assert "Partner channel collapse" in out


def test_priority_tag_in_anomaly_alert_summary():
    """AnomalyAlert summary includes priority tag in headline."""
    payload = {
        "headline": "Partner-channel win rate dropped",
        "metric": "win_rate",
        "current_value": "8%",
        "prior_value": "24%",
        "benchmark": "20-30%",
        "severity": "critical",
        "evidence_summary": "Onboarding gap",
        "recommended_action": "Pause partner intake",
        "priority": "P0",
        "urgency": "immediate",
    }
    response = parse_payload("anomaly_alert", payload)
    out = render(response, mode="summary", target="slack")
    assert "*[P0]*" in out


def test_priority_tag_in_weekly_status_summary():
    """Weekly status surfaces priority tag on flagged portco lines."""
    payload = {
        "headline": "Weekly trajectory",
        "portco_lines": [
            {
                "portco": "Acme",
                "headline": "Pipeline soft",
                "severity": "watch",
                "priority": "P1",
                "urgency": "this_week",
            }
        ],
        "trajectory": "Stable",
    }
    response = parse_payload("weekly_status", payload)
    out = render(response, mode="summary", target="slack")
    assert "*[P1]*" in out


# ---------------------------------------------------------------------------
# Renderer: decision options + intervention + changed_since_last in expanded
# ---------------------------------------------------------------------------


def test_decision_options_in_ad_hoc_expanded():
    """Expanded mode shows *Decision:* line when decision_required=True."""
    payload = {
        "headline": "Decide partner intake",
        "findings": [
            {
                "headline": "Partner channel collapse",
                "value": "8%",
                "confidence": "HIGH",
                "severity": "critical",
                "priority": "P1",
                "urgency": "this_week",
                "decision_required": True,
                "decision_options": [
                    "Pause partner intake",
                    "Add coaching sprint",
                    "Investigate root cause",
                ],
            }
        ],
    }
    response = parse_payload("ad_hoc_investigation_result", payload)
    out = render(response, mode="expanded", target="slack")
    assert "*Decision:*" in out
    assert "Pause partner intake" in out
    assert "Add coaching sprint" in out
    assert "Investigate root cause" in out


def test_intervention_line_in_ad_hoc_expanded():
    """Expanded mode shows *Intervention:* line when set."""
    payload = {
        "headline": "Need coaching",
        "findings": [
            {
                "headline": "Rep skill gap",
                "value": "—",
                "confidence": "MEDIUM",
                "severity": "watch",
                "priority": "P2",
                "urgency": "this_quarter",
                "recommended_intervention": "coaching",
            }
        ],
    }
    response = parse_payload("ad_hoc_investigation_result", payload)
    out = render(response, mode="expanded", target="slack")
    assert "*Intervention:*" in out
    assert "coaching" in out


def test_changed_since_last_in_ad_hoc_expanded():
    """Expanded mode shows _(worsened)_ italic when changed_since_last set."""
    payload = {
        "headline": "Worsening pattern",
        "findings": [
            {
                "headline": "Win rate down again",
                "value": "21%",
                "confidence": "HIGH",
                "severity": "watch",
                "priority": "P1",
                "urgency": "this_week",
                "changed_since_last": "worsened",
            }
        ],
    }
    response = parse_payload("ad_hoc_investigation_result", payload)
    out = render(response, mode="expanded", target="slack")
    assert "_(worsened)_" in out


def test_intervention_in_anomaly_alert_expanded():
    """AnomalyAlert expanded mode shows intervention/decision lines."""
    payload = {
        "headline": "Partner anomaly",
        "metric": "win_rate",
        "current_value": "8%",
        "prior_value": "24%",
        "benchmark": "20-30%",
        "severity": "critical",
        "evidence_summary": "Onboarding gap",
        "recommended_action": "Pause partner intake",
        "priority": "P0",
        "urgency": "immediate",
        "decision_required": True,
        "decision_options": ["Pause now", "Pause after Q-end"],
        "recommended_intervention": "process_change",
        "changed_since_last": "new",
    }
    response = parse_payload("anomaly_alert", payload)
    out = render(response, mode="expanded", target="slack")
    assert "*Decision:*" in out
    assert "Pause now" in out
    assert "Pause after Q-end" in out
    assert "*Intervention:*" in out
    assert "process_change" in out
    assert "_(new)_" in out


# ---------------------------------------------------------------------------
# decision_options list cap and per-item cap
# ---------------------------------------------------------------------------


def test_decision_options_cap_at_three_items():
    """decision_options list cap is 3."""
    with pytest.raises(ValidationError):
        Finding(
            headline="x",
            value="x",
            confidence="HIGH",
            severity="watch",
            decision_required=True,
            decision_options=["a", "b", "c", "d"],
        )


def test_decision_options_accept_variable_length():
    """Per-item length caps removed 2026-05-11. Decision options of any
    length validate; sizing is the editor pass's job, not the schema's."""
    f = Finding(
        headline="x",
        value="x",
        confidence="HIGH",
        severity="watch",
        decision_required=True,
        decision_options=["x" * 300, "Bulk-create renewal opps via Apex"],
    )
    assert len(f.decision_options[0]) == 300


# ---------------------------------------------------------------------------
# TableBlock schema (Slack native Block Kit table support, 2026-05-11)
# ---------------------------------------------------------------------------


def test_table_block_validates_5_row_3_col():
    """A 5-row × 3-col TableBlock validates with all optional fields populated."""
    t = TableBlock(
        title="Q1 win rate by rep",
        headers=["Rep", "Win%", "n"],
        rows=[
            ["Alice", "24.1%", "42"],
            ["Bob", "19.7%", "38"],
            ["Carol", "31.2%", "44"],
            ["Dan", "12.4%", "29"],
            ["Eve", "22.8%", "37"],
        ],
        column_alignment=["left", "right", "right"],
        footnote="Excludes terminated reps.",
    )
    assert len(t.rows) == 5
    assert len(t.headers) == 3
    assert t.column_alignment == ["left", "right", "right"]


def test_table_block_minimal_validates():
    """A TableBlock with only required fields (title, headers, rows) validates."""
    t = TableBlock(
        title="By stage",
        headers=["Stage", "Count"],
        rows=[["Discovery", "12"], ["Demo", "8"]],
    )
    assert t.column_alignment is None
    assert t.footnote is None


def test_table_block_rejects_mismatched_row_width():
    """A row whose length differs from len(headers) is rejected."""
    with pytest.raises(ValidationError) as exc:
        TableBlock(
            title="bad",
            headers=["A", "B", "C"],
            rows=[["1", "2", "3"], ["1", "2"]],  # second row only 2 cells
        )
    assert "every row must match the header count" in str(exc.value)


def test_table_block_rejects_more_than_30_rows():
    """rows max_length=30 is enforced (above this, switch to xlsx)."""
    with pytest.raises(ValidationError):
        TableBlock(
            title="too many",
            headers=["A"],
            rows=[[str(i)] for i in range(31)],
        )


def test_table_block_rejects_more_than_6_cols():
    """headers max_length=6 is enforced."""
    with pytest.raises(ValidationError):
        TableBlock(
            title="too wide",
            headers=["A", "B", "C", "D", "E", "F", "G"],
            rows=[],
        )


def test_table_block_rejects_empty_headers():
    """headers must have at least one column."""
    with pytest.raises(ValidationError):
        TableBlock(title="x", headers=[], rows=[])


def test_table_block_rejects_misaligned_column_alignment():
    """column_alignment length must match headers length when provided."""
    with pytest.raises(ValidationError) as exc:
        TableBlock(
            title="x",
            headers=["A", "B"],
            rows=[["1", "2"]],
            column_alignment=["left"],
        )
    assert "column_alignment" in str(exc.value)


def test_ad_hoc_response_with_one_table_validates():
    """AdHocInvestigationResponse accepts a tables list with one TableBlock."""
    r = AdHocInvestigationResponse(
        headline="Q1 by rep",
        tables=[
            TableBlock(
                title="By rep",
                headers=["Rep", "Win%"],
                rows=[["Alice", "24.1%"], ["Bob", "19.7%"]],
            )
        ],
    )
    assert len(r.tables) == 1
    assert r.tables[0].title == "By rep"


def test_ad_hoc_response_tables_defaults_to_empty_list():
    """tables field defaults to [] — does NOT break existing payloads."""
    r = AdHocInvestigationResponse(headline="No table here")
    assert r.tables == []


def test_ad_hoc_response_rejects_more_than_3_tables():
    """tables max_length=3 — keeps the post scannable."""
    tb = TableBlock(title="t", headers=["A"], rows=[["1"]])
    with pytest.raises(ValidationError):
        AdHocInvestigationResponse(headline="too many", tables=[tb, tb, tb, tb])


def test_table_block_cell_length_cap_500():
    """Per-cell length cap of 500 chars is enforced.

    Raised from 200 to 500 after a live session 2026-05-13 18:48 PT hit
    `string_too_long` on natural product-definition cells ~210 chars long.
    500 leaves headroom for product/process prose while still acting as a
    structural safety net against runaway output.
    """
    # 500 chars is allowed.
    TableBlock(
        title="x",
        headers=["A"],
        rows=[["x" * 500]],
    )
    # 501 chars trips the cap.
    with pytest.raises(ValidationError):
        TableBlock(
            title="x",
            headers=["A"],
            rows=[["x" * 501]],
        )


# ---------------------------------------------------------------------------
# Renderer: TableBlock → Slack Block Kit table block dict
# ---------------------------------------------------------------------------


def test_renderer_emits_block_kit_table_for_ad_hoc_with_table():
    """render_payload returns text + a list of Block Kit blocks for a TableBlock."""
    from response_renderer import render_payload

    r = AdHocInvestigationResponse(
        headline="Q1 by rep",
        tables=[
            TableBlock(
                title="Q1 win rate by rep",
                headers=["Rep", "Win%", "n"],
                rows=[["Alice", "24.1%", "42"], ["Bob", "19.7%", "38"]],
                column_alignment=["left", "right", "right"],
                footnote="Excludes terminated reps.",
            )
        ],
    )
    text, blocks = render_payload(r, verbosity="normal")

    # Text path still works
    assert "Q1 by rep" in text

    # Three blocks: title section, table, footnote context
    assert len(blocks) == 3

    title_block, table_block, footnote_block = blocks
    assert title_block["type"] == "section"
    assert "Q1 win rate by rep" in title_block["text"]["text"]

    assert table_block["type"] == "table"
    # First row is the header row
    assert table_block["rows"][0][0] == {"type": "raw_text", "text": "Rep"}
    # Then 2 data rows
    assert len(table_block["rows"]) == 3  # header + 2 data
    # column_settings: 3 entries, each with is_wrapped and align
    assert len(table_block["column_settings"]) == 3
    assert table_block["column_settings"][1]["align"] == "right"
    assert table_block["column_settings"][0]["is_wrapped"] is True

    assert footnote_block["type"] == "context"
    assert "Excludes terminated reps" in footnote_block["elements"][0]["text"]


def test_renderer_no_blocks_when_tables_empty():
    """render_payload returns empty extra_blocks list when there are no tables."""
    from response_renderer import render_payload

    r = AdHocInvestigationResponse(headline="No table")
    text, blocks = render_payload(r, verbosity="normal")
    assert text  # still has text
    assert blocks == []


def test_renderer_defaults_alignment_to_left_when_omitted():
    """When column_alignment is omitted, every column defaults to left."""
    from response_renderer import render_payload

    r = AdHocInvestigationResponse(
        headline="x",
        tables=[
            TableBlock(
                title="t",
                headers=["A", "B"],
                rows=[["1", "2"]],
            )
        ],
    )
    _text, blocks = render_payload(r, verbosity="normal")
    # blocks[1] is the table (blocks[0] is the title section)
    table = blocks[1]
    assert all(s["align"] == "left" for s in table["column_settings"])


def test_renderer_escapes_table_cell_injection():
    """Agent-controlled cell text is escaped to neutralize Slack mrkdwn injection."""
    from response_renderer import render_payload

    r = AdHocInvestigationResponse(
        headline="x",
        tables=[
            TableBlock(
                title="t",
                headers=["Rep"],
                rows=[["<!channel> URGENT"]],
            )
        ],
    )
    _text, blocks = render_payload(r, verbosity="normal")
    table = blocks[1]
    cell_text = table["rows"][1][0]["text"]
    # !channel is replaced with ·channel; angle brackets html-escaped
    assert "!channel" not in cell_text
    assert "·channel" in cell_text


def test_renderer_drops_extra_tables_and_notes_in_text():
    """Slack allows one table per message; extras are dropped with a text note."""
    from response_renderer import render_payload

    tb = TableBlock(title="t", headers=["A"], rows=[["1"]])
    r = AdHocInvestigationResponse(headline="x", tables=[tb, tb])
    text, blocks = render_payload(r, verbosity="verbose")
    # Only one table block emitted
    table_blocks = [b for b in blocks if b["type"] == "table"]
    assert len(table_blocks) == 1
    assert "additional table" in text
