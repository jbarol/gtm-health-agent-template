"""Golden-file tests for response_renderer.

Run:
    cd orchestrator && pytest response_renderer_test.py

Each test renders a sample payload and compares against a golden file under
orchestrator/test_fixtures/. When you intentionally change a renderer template,
update the golden file (delete it and re-run; the test will fail with the new
output, which you copy into the file).

Tests double as documentation of what each response type's output looks like.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from response_renderer import render
from response_schemas import parse_payload

FIXTURES_DIR = Path(__file__).parent / "test_fixtures"


def _load_golden(name: str, actual: str) -> str:
    """Load golden output from disk; fail loudly if the fixture is missing.

    Previously this function auto-wrote missing fixtures on first run. That
    hid two real bugs: (1) a deleted fixture passed silently with whatever the
    current renderer happened to emit, and (2) a new test added without a
    fixture would also auto-create rather than force the author to inspect the
    output. The fail-loud version surfaces both cases.

    To accept a new golden, run the renderer once and copy the output into the
    indicated path, then re-run.
    """
    path = FIXTURES_DIR / name
    if not path.exists():
        raise FileNotFoundError(
            f"Golden fixture missing: {path}\n"
            f"To accept this output as the golden, write the following to "
            f"that path:\n\n--- BEGIN OUTPUT ---\n{actual}\n--- END OUTPUT ---"
        )
    return path.read_text()


# ---------------------------------------------------------------------------
# Sample payloads
# ---------------------------------------------------------------------------

QUICK_ANSWER_PAYLOAD = {
    "metric": "Win rate, Q1 2026 new business",
    "value": "23.4% (n=148)",
    "as_of": "as of 2026-05-11 09:00 PT",
    "source": "Salesforce MCP, RecordType.Name=New Business",
}

ANOMALY_ALERT_PAYLOAD = {
    "headline": "Partner-channel win rate dropped to 8%",
    "metric": "Win rate (partner)",
    "current_value": "8% (n=42)",
    "prior_value": "24% (n=58)",
    "benchmark": "20-30%",
    "severity": "critical",
    "evidence_summary": "3 new partners onboarded Q1 with no sales training. 12 of 14 partner losses cite champion-left.",
    "recommended_action": "Pause partner intake until training resumes; brief Sarah at Acme.",
}

AD_HOC_PAYLOAD = {
    "headline": "Win rate down 4.2pp this quarter",
    "key_metrics": [
        {
            "name": "Win rate",
            "current": "23.4%",
            "prior": "27.6%",
            "benchmark": "20-30%",
            "trend": "down",
        },
        {
            "name": "Pipeline coverage",
            "current": "3.1x",
            "prior": "3.8x",
            "benchmark": "3-4x",
            "trend": "down",
        },
    ],
    "findings": [
        {
            "headline": "Partner channel win rate collapsed",
            "value": "8% (n=42)",
            "confidence": "HIGH",
            "severity": "critical",
            "evidence_query": "SELECT Win/Loss FROM Opportunity WHERE LeadSource=Partner",
        },
        {
            "headline": "Cycle time elongating in negotiation stage",
            "value": "47d (P75)",
            "confidence": "MEDIUM",
            "severity": "watch",
            "reviewer_caveat": "n=23 may be small",
        },
    ],
    "cross_domain_pattern": "Partner onboarding gap leads to lower win rate AND higher churn risk",
    "open_questions": [
        "Why did partner training stop?",
        "Are the 3 new partners net-new logos or referrals?",
    ],
    "methodology_note": "Win rate = CW/(CW+CL), New Business RecordType only, CloseDate within Q1 2026",
}

NIGHTLY_DIGEST_PAYLOAD = {
    "headline": "Overnight: 2 portcos need attention",
    "portcos_with_action": ["Acme", "Acme"],
    "changes_overnight": [
        {
            "headline": "GRR dropped 1.2pp at Acme",
            "value": "84.3% (was 85.5%)",
            "confidence": "HIGH",
            "severity": "watch",
        },
        {
            "headline": "Acme new $250K opp from outbound",
            "value": "$250K",
            "confidence": "HIGH",
            "severity": "info",
        },
    ],
    "link_to_full_report": "https://outputs.example/report-2026-05-11",
}

WEEKLY_STATUS_PAYLOAD = {
    "headline": "Weekly trajectory: 4 portcos green, 2 yellow, 0 red",
    "portco_lines": [
        {
            "portco": "Acme",
            "headline": "Pipeline coverage 3.1x, below target 4x",
            "severity": "watch",
        },
        {
            "portco": "Acme",
            "headline": "Win rate steady 26%, expansion ARR +12%",
            "severity": "info",
        },
        {
            "portco": "Beta",
            "headline": "GRR 78% — below $5-20M benchmark",
            "severity": "critical",
        },
    ],
    "trajectory": "Pipeline coverage softening across portfolio. Partner-channel issues at Acme spreading to Acme.",
}

# ---------------------------------------------------------------------------
# Parametrized golden-file tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "response_type,payload,mode,fixture_name",
    [
        ("quick_answer", QUICK_ANSWER_PAYLOAD, "summary", "quick_answer_summary.txt"),
        ("quick_answer", QUICK_ANSWER_PAYLOAD, "expanded", "quick_answer_expanded.txt"),
        (
            "anomaly_alert",
            ANOMALY_ALERT_PAYLOAD,
            "summary",
            "anomaly_alert_summary.txt",
        ),
        (
            "anomaly_alert",
            ANOMALY_ALERT_PAYLOAD,
            "expanded",
            "anomaly_alert_expanded.txt",
        ),
        (
            "ad_hoc_investigation_result",
            AD_HOC_PAYLOAD,
            "summary",
            "ad_hoc_summary.txt",
        ),
        (
            "ad_hoc_investigation_result",
            AD_HOC_PAYLOAD,
            "expanded",
            "ad_hoc_expanded.txt",
        ),
        (
            "nightly_digest",
            NIGHTLY_DIGEST_PAYLOAD,
            "summary",
            "nightly_digest_summary.txt",
        ),
        (
            "nightly_digest",
            NIGHTLY_DIGEST_PAYLOAD,
            "expanded",
            "nightly_digest_expanded.txt",
        ),
        (
            "weekly_status",
            WEEKLY_STATUS_PAYLOAD,
            "summary",
            "weekly_status_summary.txt",
        ),
        (
            "weekly_status",
            WEEKLY_STATUS_PAYLOAD,
            "expanded",
            "weekly_status_expanded.txt",
        ),
    ],
)
def test_slack_render_matches_golden(response_type, payload, mode, fixture_name):
    response = parse_payload(response_type, payload)
    actual = render(response, mode=mode, target="slack")
    expected = _load_golden(fixture_name, actual)
    assert actual == expected, (
        f"\n--- expected ({fixture_name}) ---\n{expected}\n"
        f"--- actual ---\n{actual}\n"
        f"To accept the new output, overwrite {FIXTURES_DIR / fixture_name} with the actual output and re-run."
    )


# ---------------------------------------------------------------------------
# Length-budget sanity checks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "response_type,payload,budget",
    [
        ("quick_answer", QUICK_ANSWER_PAYLOAD, 300),
        ("anomaly_alert", ANOMALY_ALERT_PAYLOAD, 500),
        ("ad_hoc_investigation_result", AD_HOC_PAYLOAD, 1000),
        ("nightly_digest", NIGHTLY_DIGEST_PAYLOAD, 1000),
        ("weekly_status", WEEKLY_STATUS_PAYLOAD, 1000),
    ],
)
def test_summary_under_budget(response_type, payload, budget):
    """Summary mode must stay under the per-type character budget."""
    response = parse_payload(response_type, payload)
    out = render(response, mode="summary", target="slack")
    assert len(out) < budget, (
        f"Summary for {response_type} is {len(out)} chars, exceeds budget {budget}.\n"
        f"Output:\n{out}"
    )


def test_summary_includes_expand_footer():
    """Summary mode must always include the expand: footer for non-trivial types."""
    response = parse_payload("ad_hoc_investigation_result", AD_HOC_PAYLOAD)
    summary = render(response, mode="summary", target="slack")
    assert "`expand:`" in summary


def test_expanded_omits_expand_footer():
    """Expanded mode should not show the expand: footer (would be confusing)."""
    response = parse_payload("ad_hoc_investigation_result", AD_HOC_PAYLOAD)
    expanded = render(response, mode="expanded", target="slack")
    assert "`expand:`" not in expanded


def test_unknown_target_raises():
    response = parse_payload("quick_answer", QUICK_ANSWER_PAYLOAD)
    with pytest.raises(NotImplementedError):
        render(response, mode="summary", target="docx")
    with pytest.raises(NotImplementedError):
        render(response, mode="summary", target="xlsx")


# ---------------------------------------------------------------------------
# Mrkdwn injection defense (E1)
# ---------------------------------------------------------------------------


def test_escape_slack_neutralizes_broadcast_pings():
    """Agent strings containing <!channel> etc. must not ping the whole channel."""
    from response_renderer import escape_slack

    assert "<!channel>" not in escape_slack("<!channel> URGENT")
    assert "<!here>" not in escape_slack("<!here> alert")
    assert "<!everyone>" not in escape_slack("<!everyone> ping")
    # The keyword is preserved typographically so the message is still readable.
    assert "channel" in escape_slack("<!channel> URGENT")


def test_escape_slack_neutralizes_user_and_channel_mentions():
    from response_renderer import escape_slack

    out = escape_slack("hello <@U12345> and <#C67890|general>")
    assert "<@U12345>" not in out
    assert "<#C67890" not in out


def test_escape_slack_escapes_backticks():
    """Backticks in evidence_query would break out of inline code spans."""
    from response_renderer import escape_slack

    out = escape_slack("SELECT * FROM t WHERE x = `evil`")
    assert "`" not in out


def test_renderer_escapes_injected_headline():
    """End-to-end: poisoned CRM data in a headline doesn't ping the channel."""
    payload = dict(QUICK_ANSWER_PAYLOAD, metric="<!channel> Pipeline metric")
    response = parse_payload("quick_answer", payload)
    rendered = render(response, mode="summary", target="slack")
    assert "<!channel>" not in rendered
    assert "·channel" in rendered  # neutralized form


def test_renderer_escapes_injected_finding():
    """Findings with injected mrkdwn don't escape their formatting."""
    payload = dict(AD_HOC_PAYLOAD)
    payload["findings"] = [
        {
            "headline": "<@U999999> Win rate <!here>",
            "value": "8%",
            "confidence": "HIGH",
            "severity": "critical",
        }
    ]
    response = parse_payload("ad_hoc_investigation_result", payload)
    rendered = render(response, mode="summary", target="slack")
    assert "<@U999999>" not in rendered
    assert "<!here>" not in rendered
    assert "·here" in rendered


# ---------------------------------------------------------------------------
# Strict schema rejection (E6)
# ---------------------------------------------------------------------------


def test_schema_rejects_extra_fields():
    """extra='forbid' rejects stale/malformed payloads at validation time."""
    from pydantic import ValidationError

    bad = dict(QUICK_ANSWER_PAYLOAD, undeclared_field="should fail")
    with pytest.raises(ValidationError):
        parse_payload("quick_answer", bad)


def test_schema_accepts_variable_length_open_questions():
    """Per-item length caps removed 2026-05-11. The editor pass handles
    sizing for quality; schema only enforces structure. Verify a long
    open_question now passes validation (was capped at 140; now uncapped)."""
    payload = dict(AD_HOC_PAYLOAD, open_questions=["x" * 500])
    response = parse_payload("ad_hoc_investigation_result", payload)
    assert len(response.open_questions[0]) == 500


# ---------------------------------------------------------------------------
# Autoplan test gap closures (Tasks #8, #9, #11)
# ---------------------------------------------------------------------------

# Task #8 — List-cap rejection (E6 coverage)
# Pydantic's list-level max_length caps the NUMBER of items; per-item caps
# (via Annotated[str, Field(max_length=N)]) prevent oversized items. These
# tests pin both layers.


@pytest.mark.parametrize(
    "response_type,payload_factory,assert_message",
    [
        (
            "ad_hoc_investigation_result",
            lambda: dict(
                AD_HOC_PAYLOAD,
                key_metrics=[
                    dict(name=f"M{i}", current="1", trend="up") for i in range(6)
                ],
            ),
            "key_metrics caps at 5 items",
        ),
        (
            "ad_hoc_investigation_result",
            lambda: dict(
                AD_HOC_PAYLOAD,
                findings=[
                    dict(
                        headline=f"H{i}",
                        value="1",
                        confidence="HIGH",
                        severity="info",
                    )
                    for i in range(5)
                ],
            ),
            "findings caps at 4 items",
        ),
        (
            "ad_hoc_investigation_result",
            lambda: dict(AD_HOC_PAYLOAD, open_questions=["q1", "q2", "q3", "q4"]),
            "open_questions caps at 3 items",
        ),
        (
            "nightly_digest",
            lambda: dict(
                NIGHTLY_DIGEST_PAYLOAD,
                portcos_with_action=["A", "B", "C", "D", "E", "F"],
            ),
            "portcos_with_action caps at 5 items",
        ),
        (
            "nightly_digest",
            lambda: dict(
                NIGHTLY_DIGEST_PAYLOAD,
                changes_overnight=[
                    dict(
                        headline=f"H{i}",
                        value="1",
                        confidence="HIGH",
                        severity="info",
                    )
                    for i in range(6)
                ],
            ),
            "changes_overnight caps at 5 items",
        ),
        (
            "weekly_status",
            lambda: dict(
                WEEKLY_STATUS_PAYLOAD,
                portco_lines=[
                    dict(portco=f"P{i}", headline=f"h{i}", severity="info")
                    for i in range(11)
                ],
            ),
            "portco_lines caps at 10 items",
        ),
    ],
)
def test_schema_rejects_oversize_lists(response_type, payload_factory, assert_message):
    """E6: list-level max_length caps reject oversized lists per schema."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        parse_payload(response_type, payload_factory())


# Task #9 — Renderer expanded mode includes all populated fields
# Counter-balances the summary-mode budget tests: expanded must NOT silently
# drop fields. Assertion is keyed off the populated payload values, not the
# schema reflection, so renames keep the test honest.


@pytest.mark.parametrize(
    "response_type,payload,expected_substrings",
    [
        (
            "quick_answer",
            QUICK_ANSWER_PAYLOAD,
            [
                QUICK_ANSWER_PAYLOAD["metric"],
                QUICK_ANSWER_PAYLOAD["value"],
                QUICK_ANSWER_PAYLOAD["as_of"],
                QUICK_ANSWER_PAYLOAD["source"],  # expanded-only field
            ],
        ),
        (
            "anomaly_alert",
            ANOMALY_ALERT_PAYLOAD,
            [
                ANOMALY_ALERT_PAYLOAD["headline"],
                ANOMALY_ALERT_PAYLOAD["benchmark"],
                ANOMALY_ALERT_PAYLOAD["current_value"],
                ANOMALY_ALERT_PAYLOAD["prior_value"],
                # evidence_summary may be truncated; assert a prefix substring
                ANOMALY_ALERT_PAYLOAD["evidence_summary"][:40],
                ANOMALY_ALERT_PAYLOAD["recommended_action"][:40],
            ],
        ),
        (
            "ad_hoc_investigation_result",
            AD_HOC_PAYLOAD,
            [
                AD_HOC_PAYLOAD["headline"],
                AD_HOC_PAYLOAD["key_metrics"][0]["name"],
                AD_HOC_PAYLOAD["key_metrics"][1]["name"],  # both metrics
                AD_HOC_PAYLOAD["findings"][0]["headline"],
                AD_HOC_PAYLOAD["findings"][1]["headline"],  # both findings
                AD_HOC_PAYLOAD["cross_domain_pattern"][:40],
                AD_HOC_PAYLOAD["open_questions"][0],
                AD_HOC_PAYLOAD["methodology_note"][:40],
            ],
        ),
        (
            "nightly_digest",
            NIGHTLY_DIGEST_PAYLOAD,
            [
                NIGHTLY_DIGEST_PAYLOAD["headline"],
                NIGHTLY_DIGEST_PAYLOAD["portcos_with_action"][0],
                NIGHTLY_DIGEST_PAYLOAD["portcos_with_action"][1],
                NIGHTLY_DIGEST_PAYLOAD["changes_overnight"][0]["headline"],
                NIGHTLY_DIGEST_PAYLOAD["changes_overnight"][1]["headline"],
            ],
        ),
        (
            "weekly_status",
            WEEKLY_STATUS_PAYLOAD,
            [
                WEEKLY_STATUS_PAYLOAD["headline"],
                WEEKLY_STATUS_PAYLOAD["portco_lines"][0]["portco"],
                WEEKLY_STATUS_PAYLOAD["portco_lines"][1]["portco"],
                WEEKLY_STATUS_PAYLOAD["portco_lines"][2]["portco"],
                WEEKLY_STATUS_PAYLOAD["trajectory"][:40],
            ],
        ),
    ],
)
def test_expanded_mode_includes_all_populated_fields(
    response_type, payload, expected_substrings
):
    """Expanded mode must surface every populated field. No silent drops."""
    response = parse_payload(response_type, payload)
    out = render(response, mode="expanded", target="slack")
    missing = [s for s in expected_substrings if s not in out]
    assert not missing, (
        f"Expanded {response_type} dropped: {missing}\n--- rendered output ---\n{out}"
    )


# Task #11 — Mrkdwn injection through every agent-controlled field (E1 coverage)
# Existing tests cover headline and one finding. Extend to verify each
# field neutralizes <!channel> and <@U...> tokens before rendering.


_INJECTIONS = [
    ("<!channel>", "·channel"),
    ("<!here>", "·here"),
    ("<!everyone>", "·everyone"),
    ("<@U99999>", "&lt;@U99999&gt;"),
    ("<#C12345|general>", "&lt;#C12345"),
]


@pytest.mark.parametrize("inject,expect_neutral", _INJECTIONS)
def test_injection_neutralized_in_quick_answer_fields(inject, expect_neutral):
    for field in ("metric", "value", "as_of", "source"):
        payload = dict(QUICK_ANSWER_PAYLOAD, **{field: f"{inject} text"})
        response = parse_payload("quick_answer", payload)
        rendered = render(response, mode="expanded", target="slack")
        assert inject not in rendered, f"{field}: {inject} leaked"
        assert expect_neutral in rendered, f"{field}: {expect_neutral} not in output"


@pytest.mark.parametrize("inject,expect_neutral", _INJECTIONS)
def test_injection_neutralized_in_anomaly_alert_fields(inject, expect_neutral):
    for field in (
        "headline",
        "metric",
        "current_value",
        "prior_value",
        "benchmark",
        "evidence_summary",
        "recommended_action",
    ):
        payload = dict(ANOMALY_ALERT_PAYLOAD, **{field: f"{inject} something"})
        response = parse_payload("anomaly_alert", payload)
        rendered = render(response, mode="expanded", target="slack")
        assert inject not in rendered, f"{field}: {inject} leaked"


@pytest.mark.parametrize("inject,_", _INJECTIONS)
def test_injection_neutralized_in_ad_hoc_finding_fields(inject, _):
    for field in ("headline", "value", "evidence_query", "reviewer_caveat"):
        payload = dict(AD_HOC_PAYLOAD)
        injected_finding = dict(
            AD_HOC_PAYLOAD["findings"][0],
            **{field: f"{inject} something"},
        )
        payload["findings"] = [injected_finding] + AD_HOC_PAYLOAD["findings"][1:]
        response = parse_payload("ad_hoc_investigation_result", payload)
        rendered = render(response, mode="expanded", target="slack")
        assert inject not in rendered, f"finding.{field}: {inject} leaked"


def test_injection_in_backtick_evidence_query_neutralized():
    """Specifically: backticks inside evidence_query don't break out of the code span."""
    payload = dict(AD_HOC_PAYLOAD)
    payload["findings"] = [
        dict(
            AD_HOC_PAYLOAD["findings"][0],
            evidence_query="SELECT * FROM t WHERE x = `evil`",
        )
    ]
    response = parse_payload("ad_hoc_investigation_result", payload)
    rendered = render(response, mode="expanded", target="slack")
    assert "`evil`" not in rendered  # original backticks gone
    assert "ʼevilʼ" in rendered or "evil" in rendered  # neutralized form present


def test_injection_in_weekly_status_portco_fields():
    """The same defenses apply on weekly_status portco_lines."""
    payload = dict(WEEKLY_STATUS_PAYLOAD)
    payload["portco_lines"] = [
        dict(
            portco="<!channel> Acme",
            headline="<@U99999> Pipeline issue",
            severity="critical",
        )
    ]
    response = parse_payload("weekly_status", payload)
    rendered = render(response, mode="expanded", target="slack")
    assert "<!channel>" not in rendered
    assert "<@U99999>" not in rendered


# ---------------------------------------------------------------------------
# Plan #31 E1 — three-tier verbosity flag plumbing
# ---------------------------------------------------------------------------

from response_renderer import (  # noqa: E402  (deliberate late import)
    _NORMAL_SECTIONS,
    _TERSE_SECTIONS,
    _VERBOSE_SECTIONS,
    _normalize_verbosity,
    _select_sections,
)


def test_normalize_verbosity_canonical():
    """Canonical tier names pass through unchanged."""
    assert _normalize_verbosity("terse") == "terse"
    assert _normalize_verbosity("normal") == "normal"
    assert _normalize_verbosity("verbose") == "verbose"


def test_normalize_verbosity_legacy_aliases():
    """Legacy mode aliases map to the canonical 3-tier model."""
    assert _normalize_verbosity("summary") == "normal"
    assert _normalize_verbosity("expanded") == "verbose"


def test_normalize_verbosity_default():
    """None defaults to normal — matches the new render() default."""
    assert _normalize_verbosity(None) == "normal"


def test_normalize_verbosity_rejects_unknown():
    """Unknown values raise loudly so call-site typos are caught."""
    with pytest.raises(ValueError):
        _normalize_verbosity("brief")
    with pytest.raises(ValueError):
        _normalize_verbosity("EXPANDED")  # case-sensitive on purpose


def test_select_sections_terse_is_subset_of_normal():
    """Terse sections are a strict subset of normal."""
    terse = set(_select_sections("terse"))
    normal = set(_select_sections("normal"))
    assert terse.issubset(normal), terse - normal


def test_select_sections_normal_contains_expand_footer():
    """Normal tier (the default summary shape) must keep the expand footer."""
    assert "expand_footer" in _select_sections("normal")


def test_select_sections_terse_omits_expand_footer():
    """Terse drops the footer — 1-sentence answer + 1 number, nothing else."""
    assert "expand_footer" not in _select_sections("terse")


def test_select_sections_verbose_omits_expand_footer():
    """Verbose mode has the full breakdown; the footer would be misleading."""
    assert "expand_footer" not in _select_sections("verbose")


def test_select_sections_verbose_includes_full_detail():
    """Verbose must surface every drillable section so nothing is hidden."""
    sections = set(_select_sections("verbose"))
    for required in (
        "key_metrics",
        "findings",
        "decision_block",
        "cross_domain",
        "open_questions",
        "methodology",
        "portco_lines",
        "source",
    ):
        assert required in sections, f"verbose missing {required}"


def test_select_sections_accepts_legacy_aliases():
    """`summary` and `expanded` resolve to the same section sets as normal/verbose."""
    assert _select_sections("summary") == _select_sections("normal")
    assert _select_sections("expanded") == _select_sections("verbose")


def test_select_sections_returns_a_fresh_list():
    """Mutating the returned list must not poison the module-level constants."""
    out = _select_sections("normal")
    out.append("frobnicate")
    assert "frobnicate" not in _NORMAL_SECTIONS
    assert "frobnicate" not in _select_sections("normal")


def test_select_sections_constants_match_canonical_tiers():
    """The internal section-set constants are what _select_sections returns."""
    assert _select_sections("terse") == list(_TERSE_SECTIONS)
    assert _select_sections("normal") == list(_NORMAL_SECTIONS)
    assert _select_sections("verbose") == list(_VERBOSE_SECTIONS)


# --- end-to-end render() with the new verbosity kwarg --------------------


def test_render_accepts_verbosity_kwarg_canonical():
    """The new public kwarg is `verbosity`. All three tiers render cleanly."""
    response = parse_payload("ad_hoc_investigation_result", AD_HOC_PAYLOAD)
    for v in ("terse", "normal", "verbose"):
        out = render(response, verbosity=v, target="slack")
        assert AD_HOC_PAYLOAD["headline"] in out, f"headline missing at {v}"


def test_render_default_verbosity_is_normal():
    """Calling render() with no verbosity matches the legacy `summary` shape."""
    response = parse_payload("ad_hoc_investigation_result", AD_HOC_PAYLOAD)
    default = render(response, target="slack")
    legacy_summary = render(response, mode="summary", target="slack")
    assert default == legacy_summary


def test_render_verbose_matches_legacy_expanded():
    """Backward-compat: `verbose` produces the same output as legacy `expanded`."""
    response = parse_payload("ad_hoc_investigation_result", AD_HOC_PAYLOAD)
    verbose = render(response, verbosity="verbose", target="slack")
    expanded = render(response, mode="expanded", target="slack")
    assert verbose == expanded


def test_render_normal_matches_legacy_summary():
    """Backward-compat: `normal` produces the same output as legacy `summary`."""
    response = parse_payload("ad_hoc_investigation_result", AD_HOC_PAYLOAD)
    normal = render(response, verbosity="normal", target="slack")
    summary = render(response, mode="summary", target="slack")
    assert normal == summary


def test_render_verbosity_overrides_mode_when_both_passed():
    """If a caller passes both, the new `verbosity` kwarg wins."""
    response = parse_payload("ad_hoc_investigation_result", AD_HOC_PAYLOAD)
    out = render(response, verbosity="verbose", mode="summary", target="slack")
    expanded = render(response, mode="expanded", target="slack")
    assert out == expanded


def test_render_terse_ad_hoc_keeps_headline_and_at_most_one_fact():
    """Terse: 1-sentence answer + at most one supporting number."""
    response = parse_payload("ad_hoc_investigation_result", AD_HOC_PAYLOAD)
    out = render(response, verbosity="terse", target="slack")
    # The headline is present and second-priority finding is not.
    assert AD_HOC_PAYLOAD["headline"] in out
    assert AD_HOC_PAYLOAD["findings"][1]["headline"] not in out
    # No expand footer at the terse tier.
    assert "`expand:`" not in out
    # Methodology / cross-domain blocks are stripped in terse.
    assert "Methodology" not in out
    assert "Cross-domain pattern" not in out


def test_render_terse_quick_answer_has_no_source():
    """QuickAnswer terse mirrors normal: no source line."""
    response = parse_payload("quick_answer", QUICK_ANSWER_PAYLOAD)
    out = render(response, verbosity="terse", target="slack")
    assert QUICK_ANSWER_PAYLOAD["source"] not in out


def test_render_terse_anomaly_alert_minimal():
    """Anomaly alert terse: headline + recommended action; no evidence text."""
    response = parse_payload("anomaly_alert", ANOMALY_ALERT_PAYLOAD)
    out = render(response, verbosity="terse", target="slack")
    assert ANOMALY_ALERT_PAYLOAD["headline"] in out
    # evidence_summary line is dropped at the terse tier.
    assert ANOMALY_ALERT_PAYLOAD["evidence_summary"][:30] not in out
    # No expand footer.
    assert "`expand:`" not in out


def test_render_terse_nightly_digest_only_top_change():
    """Nightly digest terse: headline + the single top change only."""
    response = parse_payload("nightly_digest", NIGHTLY_DIGEST_PAYLOAD)
    out = render(response, verbosity="terse", target="slack")
    # Top change is the critical/watch one (GRR drop). The info-severity
    # Acme line should not appear.
    assert "GRR" in out
    assert "Acme new $250K" not in out


def test_render_terse_weekly_status_only_trajectory():
    """Weekly status terse: headline + trajectory; no per-portco lines.

    Can't assert portco names are absent — trajectory free text often mentions
    them ("issues at Acme"). What we pin is that no portco-line block is
    rendered (no severity emoji, no `By portco:` header) and no expand footer.
    """
    response = parse_payload("weekly_status", WEEKLY_STATUS_PAYLOAD)
    out = render(response, verbosity="terse", target="slack")
    assert WEEKLY_STATUS_PAYLOAD["trajectory"][:30] in out
    # Per-portco lines suppressed at terse — no severity emoji in the body.
    assert ":eyes:" not in out
    assert ":rotating_light:" not in out
    # No `By portco:` section header (verbose-only).
    assert "By portco" not in out
    # No expand footer at terse.
    assert "`expand:`" not in out


def test_render_rejects_unknown_verbosity():
    """Unknown verbosity strings raise from the public render()."""
    response = parse_payload("quick_answer", QUICK_ANSWER_PAYLOAD)
    with pytest.raises(ValueError):
        render(response, verbosity="medium", target="slack")
