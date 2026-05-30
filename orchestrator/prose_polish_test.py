"""Tests for prose_polish.

The headline test (``test_real_forecast_report_round_trip``) feeds the
exact production report a user complained was unreadable through the
polisher and asserts the polished version no longer contains the
domain-internal acronyms or academic statistics phrasing that broke
readability.
"""

from __future__ import annotations

import re

import pytest

from prose_polish import (
    ACRONYM_GLOSS,
    polish,
    _consolidate_caveats,
    _gloss_acronyms,
)


# The verbatim production report the user said was unreadable. This is
# the regression bait — any future change to polish() that re-introduces
# academic stats or un-glossed acronyms fails this test.
_BAD_REPORT = """\
Q4'26 renewal pipeline-account mismatch 913/916 (99.7%) and Q3'26 NB coverage at 0.052x are both critical. Q2 pipeline is flat 48h ($5,762K ARR open) but quality is degrading; weighted-pipe forecast $1,658K exceeds the deterministic ceiling from existing pipe ($1,292K), assuming $370K of fresh creates against lead supply −49% YoY.

KEY METRICS (ARR_Total__c basis)
• Q2'26 NB Open ARR: $5,762K (vs $5,761K 48h prior — flat, in noise band)
• Q2'26 NB CW MTD: $588K, 58 opps (day 37/91)
• Q2'26 Hybrid MC Forecast: $1,658K — 80% PI [$1,210K, $2,190K]; vs Q2'25 $1,690K = −2.1% YoY; P(beat Q2'25)=39.6%
• Q3'26 NB Open ARR: ~$190K → coverage 0.052x team-wide (3x bench, 1x critical)

TOP FINDINGS

1. CRITICAL — Q3'26 NB coverage collapse
   0.052x team-wide; 11 of 11 reps below 1x quota; Wilcoxon vs 3x p=0.001; T-51 days to quarter start.
   Caveat: quota assumes FY25 $18.72M is ARR; if Amount, ratio ×2.52 to 0.13x.
   Intervention: marketing demand-gen + SDR outbound sprint this week.

2. WATCH — Q2 forecast above pipe ceiling
   Hybrid MC $1,658K; deterministic ceiling from existing pipe = $1,292K.
   Late-stage + MTD hard floor = $999K. PI widening across runs ($615K → $975K) — anchor-locked, NOT stable.
   Trend β = −$71K/qtr (p=0.012, R²=0.62, full series); FY25+ subset NS. CW DTC compressed 37.7→26.6→21.3d Mar/Apr/May (n=3, selection caveat). Forecast accuracy: naive baseline MAPE 13.6% only — Hybrid MC OOS untested.

Full report: /mnt/session/outputs/forecast_report.md (533 lines).
"""


# ---------------------------------------------------------------------------
# Acronym glossing
# ---------------------------------------------------------------------------


def test_first_use_glosses_acronym() -> None:
    """First NB → "new business (NB)"; the bare acronym is fine after that."""
    text = "NB pipeline grew. NB win rate also up."
    out = _gloss_acronyms(text, {"NB": "new business (NB)"})
    assert out.startswith("new business (NB) pipeline grew.")
    # Second occurrence preserved bare.
    assert out.count("NB") >= 2


def test_existing_gloss_is_idempotent() -> None:
    """If the expansion is already present, do not re-gloss.

    This makes the polisher safe to call repeatedly — the second call is
    a no-op for already-glossed text.
    """
    text = "new business (NB) is up. NB is the focus."
    out = _gloss_acronyms(text, {"NB": "new business (NB)"})
    assert out == text


def test_longer_keys_win_over_shorter() -> None:
    """'NB CW MTD' is matched as a unit, not 'NB' + 'CW' + 'MTD'."""
    text = "Q2'26 NB CW MTD: $588K"
    out = _gloss_acronyms(
        text,
        {
            "NB CW MTD": "new-business closed-won, month-to-date",
            "NB": "new business (NB)",
            "CW": "closed-won (CW)",
            "MTD": "month-to-date (MTD)",
        },
    )
    assert "new-business closed-won, month-to-date" in out
    # The "NB" inside the just-replaced phrase should not have re-glossed.
    assert out.count("new business (NB)") == 0


def test_acronym_not_glossed_inside_word() -> None:
    """``NBA`` should not get glossed as ``new business (NB)A``."""
    text = "NBA championships matter to nobody here."
    out = _gloss_acronyms(text, {"NB": "new business (NB)"})
    assert out == text


# ---------------------------------------------------------------------------
# Statistics rewrites
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,phrase",
    [
        ("Wilcoxon vs 3x (p=0.001)", "under 0.1%"),
        ("vs prior (p=0.01)", "under 1%"),
        ("trend (p=0.04)", "statistically meaningful"),
        ("trend (p=0.07)", "weak signal"),
        ("trend (p=0.75)", "likely random noise"),
    ],
)
def test_p_value_rewrites_to_prose(raw: str, phrase: str) -> None:
    out = polish(raw)
    assert phrase in out
    # Bare p= form should not survive.
    assert "p=" not in out


def test_r_squared_rewrites_to_percent_explained() -> None:
    out = polish("Trend strong (R²=0.62, p=0.012)")
    assert "62% of the variation explained" in out
    assert "R²" not in out
    assert "R^2" not in out


def test_beta_trend_rewrites_to_prose() -> None:
    out = polish("Series β = -$71K/qtr stable.")
    assert "trending down by ~$71K per quarter" in out
    assert "β" not in out


def test_wilcoxon_collapses_to_comparison_test() -> None:
    out = polish("Wilcoxon vs 3x")
    assert "Wilcoxon" not in out
    assert "comparison test" in out


def test_anchor_locked_rewritten() -> None:
    out = polish("PI widening — anchor-locked.")
    assert "anchor-locked" not in out
    assert "sensitive to one historical anchor" in out


# ---------------------------------------------------------------------------
# Caveat consolidation
# ---------------------------------------------------------------------------


def test_single_caveat_left_alone() -> None:
    """One inline caveat is concise enough — don't reorganize."""
    text = "Finding A.\nCaveat: small sample.\nFinding B."
    out = _consolidate_caveats(text)
    assert out == text


def test_multiple_caveats_consolidated() -> None:
    text = (
        "Finding A.\nCaveat: small sample.\n"
        "Finding B.\nCaveat: prior-year baseline imperfect.\n"
        "Finding C."
    )
    out = _consolidate_caveats(text)
    # Original inline caveats stripped.
    assert "\nCaveat: small sample." not in out
    assert "\nCaveat: prior-year baseline imperfect." not in out
    # Consolidated block appended.
    assert "*Caveats:*" in out
    assert "- small sample." in out
    assert "- prior-year baseline imperfect." in out


# ---------------------------------------------------------------------------
# Internal artifact path stripping
# ---------------------------------------------------------------------------


def test_mnt_session_outputs_path_stripped() -> None:
    text = "Done. Full report: /mnt/session/outputs/forecast_report.md (533 lines)."
    out = polish(text)
    assert "/mnt/session/outputs" not in out
    # The trailing prose should still be present.
    assert "Done." in out


# ---------------------------------------------------------------------------
# Real-world end-to-end
# ---------------------------------------------------------------------------


def test_real_forecast_report_round_trip() -> None:
    """The headline regression test — polish the verbatim production report.

    Asserts the polished version:
      * Glosses the top-priority acronyms at first use.
      * Drops every raw ``p=...`` and ``R²=...`` token.
      * Strips the ``/mnt/session/outputs/...`` path leak.
      * Rewrites academic stat names (Wilcoxon, β) into plain English.
    """
    out = polish(_BAD_REPORT)

    # 1. Acronyms glossed at first use. Spot-check the big ones.
    assert "new business (NB)" in out or "new-business" in out.lower()
    assert "annual recurring revenue (ARR)" in out
    assert "Monte Carlo" in out
    assert "prediction interval" in out
    assert "month-to-date" in out

    # 2. Academic stats gone.
    assert "p=" not in out
    assert "p <" not in out
    assert "R²" not in out
    assert "R^2" not in out
    assert "β =" not in out
    assert "Wilcoxon" not in out

    # 3. Plain-English replacements present.
    assert "chance this is random noise" in out or "statistically meaningful" in out
    assert "of the variation explained" in out
    assert "trending down by ~$71K per quarter" in out
    assert "comparison test" in out

    # 4. Path leak stripped.
    assert "/mnt/session/outputs" not in out


def test_polish_is_idempotent() -> None:
    """Polishing already-polished text returns the same text."""
    once = polish(_BAD_REPORT)
    twice = polish(once)
    assert once == twice


def test_polish_passes_through_empty() -> None:
    assert polish("") == ""
    assert polish("   ") == "   "
    assert polish(None) is None  # type: ignore[arg-type]


def test_acronym_gloss_table_has_no_circular_definitions() -> None:
    """Sanity check: no acronym's expansion contains the same bare key.

    If "NB" expanded to "the NB metric (NB)", the first-occurrence
    skip in ``_gloss_acronyms`` would still fire (the expansion already
    contains the key), and we'd silently never gloss. Check the table
    statically so this can't sneak in.
    """
    for key, expansion in ACRONYM_GLOSS.items():
        # The expansion is allowed to mention the key in parens — that's
        # the standard "new business (NB)" form. What we don't want is a
        # bare key elsewhere in the expansion.
        bare = re.sub(rf"\(\s*{re.escape(key)}\s*\)", "", expansion)
        assert key not in bare.split(), (
            f"Acronym {key!r} appears bare in its own expansion {expansion!r}"
        )


def test_polish_keeps_dollar_amounts_intact() -> None:
    """Dollar figures are the load-bearing numbers — must survive verbatim."""
    out = polish(_BAD_REPORT)
    for amount in ("$5,762K", "$1,658K", "$1,292K", "$588K", "$370K"):
        assert amount in out, f"polish() lost dollar amount {amount}"
