"""Plain-English polish pass for user-facing analyst prose.

Why this exists
---------------
The Report Writer agent emits analyst-style prose loaded with GTM acronyms
(NB, MTD, ARR, CFL, DTC, PDDR), Monte-Carlo / statistical jargon (PI, MC,
MAPE, OOS, β, R²), and academic-paper formula snippets ("Wilcoxon p=0.001",
"β = -$71K/qtr (p=0.012, R²=0.62)"). PE operating partners read these on a
phone — they don't parse "Mann phantom $77-92K" or "0.052x coverage".

The structured-response renderer in ``response_renderer.py`` is safe by
design — fixed templates, no free-form prose. But the legacy markdown path
(``post_analysis`` → ``_md_to_slack`` → Slack) and any ``send_notification``
call carry agent-authored prose verbatim. This module is the deterministic
plain-English layer that wraps that prose before Slack ever sees it.

Public API
----------
    polish(text: str) -> str
        Apply every cleanup pass to ``text`` and return the polished form.
        Safe to call on already-polished text (idempotent for the things it
        recognizes; passes through anything else unchanged).

Design rules
------------
1. Deterministic. Pure regex / dict — no LLM call, no network.
2. Idempotent. Polishing a polished string returns the same string.
3. Conservative. Only rewrites patterns we have explicit fixtures for.
   When in doubt, leave the text alone. A literal pass-through is always
   safer than a confidently wrong rewrite.
4. First-use glossing. Each acronym is glossed once per document. After
   that the bare form is fine — the reader has the key now.
5. Caveats consolidated. Inline ``Caveat:`` lines get hoisted into a single
   ``Caveats`` block at the end of the section they belong to.

Performance: O(N) over input length per pass, ~10 passes per call. The
typical 2KB report polishes in well under 1ms.
"""

from __future__ import annotations

import re
from typing import Iterable

__all__ = ["polish", "ACRONYM_GLOSS", "STAT_REWRITES"]


# ---------------------------------------------------------------------------
# Acronym glossary
# ---------------------------------------------------------------------------
#
# Format: bare acronym → plain-English expansion (with the acronym kept in
# parens for traceability — "new business (NB)" not just "new business").
# Order matters: longer phrases first so "Q4'26 NB" matches before "NB"
# alone, "Q2'26 NB Open ARR" stays intact, etc.
#
# Add a new entry only if the term shows up in user-facing copy AND
# operating partners regularly ask "what does that mean?".
ACRONYM_GLOSS: dict[str, str] = {
    # Pipeline / sales (multi-token phrases first so they win the match race)
    "NB CW MTD": "new-business closed-won, month-to-date",
    "NB CW": "new-business closed-won",
    "NB Open ARR": "new-business open pipeline (annualized)",
    "Open ARR": "open pipeline (annualized)",
    "NB": "new business (NB)",
    "CW": "closed-won (CW)",
    "CL": "closed-lost (CL)",
    "MTD": "month-to-date (MTD)",
    "ARR": "annual recurring revenue (ARR)",
    "TCV": "total contract value (TCV)",
    "DTC": "days-to-close (DTC)",
    "CFL": "close-forecast-late flag (CFL)",
    "PDDR": "Proposal/Decision/Demo/Review stage (PDDR)",
    # Lead funnel
    "MQL": "marketing-qualified lead (MQL)",
    "SQL": "sales-qualified lead (SQL)",
    "SDR": "sales development rep (SDR)",
    "ICP": "ideal customer profile (ICP)",
    # Post-sales
    "GRR": "gross retention rate (GRR)",
    "NRR": "net retention rate (NRR)",
    "CSM": "customer success manager (CSM)",
    "AM": "account manager (AM)",
    # Statistics / forecasting
    "Hybrid MC Forecast": "hybrid Monte Carlo forecast",
    "Hybrid MC": "hybrid Monte Carlo (MC)",
    "MC": "Monte Carlo (MC)",
    "80% PI": "80% prediction interval (PI)",
    "95% PI": "95% prediction interval (PI)",
    "PI": "prediction interval (PI)",
    "MAPE": "forecast error rate (MAPE)",
    "OOS": "out-of-sample (OOS)",
    "$-WR": "dollar-weighted win rate",
    "YoY": "year-over-year (YoY)",
}


# Plain-English rewrites for academic statistics phrasing.
#
# Each entry is (compiled regex, replacement). The replacement is either a
# literal string or a callable receiving the match. Callables are used for
# (p=N) → "chance random NN%" rewrites because the human-friendly value
# depends on the p-value's magnitude.
STAT_REWRITES: list[tuple[re.Pattern, object]] = []


def _p_value_to_prose(match: re.Match) -> str:
    """Turn ``p=0.001`` / ``p=0.75`` into a plain-English chance phrase.

    Mapping:
        p <= 0.001 → "chance this is random noise: under 0.1%"
        p <= 0.01  → "chance this is random noise: under 1%"
        p <  0.05  → "statistically meaningful"
        p <  0.10  → "weak signal, could still be random"
        else       → "likely random noise"

    Returns a parenthesized phrase so it drops into existing sentences
    without breaking grammar. The original ``p=...`` token is gone — readers
    don't read p-values, they read confidence.
    """
    try:
        p = float(match.group("p"))
    except (TypeError, ValueError):
        return match.group(0)
    if p <= 0.001:
        phrase = "chance this is random noise: under 0.1%"
    elif p <= 0.01:
        phrase = "chance this is random noise: under 1%"
    elif p < 0.05:
        phrase = "statistically meaningful"
    elif p < 0.10:
        phrase = "weak signal, could still be random"
    else:
        phrase = "likely random noise"
    return f"({phrase})"


def _r_squared_to_prose(match: re.Match) -> str:
    """Turn ``R²=0.62`` into ``~62% of the variation explained``.

    Slack mrkdwn doesn't render ``²`` cleanly on every client — and the bare
    decimal is meaningless to operating partners. We keep the magnitude but
    swap in a familiar phrase.
    """
    try:
        r2 = float(match.group("r2"))
    except (TypeError, ValueError):
        return match.group(0)
    pct = int(round(r2 * 100))
    return f"~{pct}% of the variation explained"


def _beta_trend_to_prose(match: re.Match) -> str:
    """Turn ``β = -$71K/qtr`` into ``trending down by ~$71K per quarter``.

    Only handles the common GTM forecast shape. Bare ``β`` (no per-unit
    suffix) is left alone — we don't know what to call it without more
    context. Accepts either ASCII hyphen ``-`` or Unicode minus ``−``
    (U+2212), since copy-paste from analysis tools mixes them.
    """
    sign = match.group("sign") or ""
    amount = match.group("amount")
    unit = match.group("unit").lower()
    direction = "down" if sign in ("-", "−") else "up"
    unit_plain = {"qtr": "quarter", "mo": "month", "wk": "week", "yr": "year"}.get(
        unit, unit
    )
    return f"trending {direction} by ~${amount} per {unit_plain}"


# Wilcoxon / Mann-Whitney / Kolmogorov-Smirnov etc. — name the test in
# English. The reader doesn't care which non-parametric test the analyst
# picked. The optional " test" suffix is consumed; anything after that
# (e.g. "vs 3x") is left alone so the trailing p-value match still fires.
STAT_REWRITES.append(
    (
        re.compile(
            r"\b(?:Wilcoxon|Mann-Whitney|Mann–Whitney|Kolmogorov-Smirnov|Shapiro-Wilk|Kruskal-Wallis|chi-square|chi-squared|t-test|z-test|F-test)"
            r"(?:\s+test)?",
            re.IGNORECASE,
        ),
        "comparison test",
    )
)

# (p=0.001), (p < 0.05), p=0.75 — rewrite via callable.
#
# Three forms to cover:
#   1. Lone parenthesized:  ``(p=0.001)`` → ``(chance ... 0.1%)``
#   2. Inside a multi-stat clause:
#        ``(p=0.012, R²=0.62, full series)`` → ``(... 1%, R²=0.62, full series)``
#      Here we don't want to swallow the closing paren — only the p-token.
#   3. Bare prose form:     ``..., p=0.001`` → ``..., chance ... 0.1%``
#
# Order matters: handle the lone form first (most specific), then the
# inline-clause form, then the prose form. The negative lookbehind
# ``(?<![A-Za-z(])`` on form 1 prevents matching ``P(beat Q2'25)=39.6%``
# (a probability statement, not a p-value).
STAT_REWRITES.append(
    (
        re.compile(
            r"(?<![A-Za-z(])\(\s*p\s*[=<≤]\s*(?P<p>\d*\.?\d+)\s*\)",
            re.IGNORECASE,
        ),
        _p_value_to_prose,
    )
)
STAT_REWRITES.append(
    (
        re.compile(
            r"(?<=[(,;\s])p\s*[=<≤]\s*(?P<p>\d*\.?\d+)(?=[\s,;)])",
            re.IGNORECASE,
        ),
        lambda m: _p_value_to_prose(m).strip("()"),
    )
)
STAT_REWRITES.append(
    (
        re.compile(
            r"(?:;\s*|,\s*|^)p\s*[=<≤]\s*(?P<p>\d*\.?\d+)\b",
            re.IGNORECASE | re.MULTILINE,
        ),
        lambda m: f"; {_p_value_to_prose(m).strip('()')}",
    )
)

# R²=0.62, R^2=0.62, R-squared=0.62
STAT_REWRITES.append(
    (
        re.compile(
            r"R(?:²|\^2|-squared)\s*=\s*(?P<r2>\d*\.?\d+)",
            re.IGNORECASE,
        ),
        _r_squared_to_prose,
    )
)

# β = -$71K/qtr — trend coefficient. Accept ASCII hyphen and U+2212.
STAT_REWRITES.append(
    (
        re.compile(
            r"(?:β|beta|trend slope)\s*=\s*(?P<sign>[-−]?)\s*\$?(?P<amount>[\d,.]+[KMB]?)\s*/\s*(?P<unit>qtr|mo|wk|yr)",
            re.IGNORECASE,
        ),
        _beta_trend_to_prose,
    )
)

# "n=N small/may be small" / "NS" (not significant) — bare academic shorthand
STAT_REWRITES.append(
    (
        re.compile(r"\bn\s*=\s*(\d+)\s*(?:may be small|small)\b", re.IGNORECASE),
        lambda m: f"small sample (n={m.group(1)})",
    )
)
STAT_REWRITES.append(
    (
        re.compile(r"\bNS\b(?!\w)"),
        "not statistically meaningful",
    )
)

# Domain-internal jargon that doesn't translate cleanly. We don't try to
# rewrite "Mann phantom" in place (meaning is context-dependent), but
# "anchor-locked" has a stable substitution.
STAT_REWRITES.append(
    (
        re.compile(r"\banchor-locked\b", re.IGNORECASE),
        "sensitive to one historical anchor",
    )
)


# Paths and references to internal artifacts that shouldn't reach the
# Slack reader. These are noise — the orchestrator handles file uploads.
# Also consume an optional trailing line-count parenthetical so we don't
# leave orphan ``(533 lines).`` debris on its own line.
_PATH_NOISE = re.compile(
    r"(?:Full report:\s*)?/mnt/session/outputs/\S+\.(?:md|docx|xlsx|pdf|json|csv)"
    r"(?:\s*\(\d+\s+lines?\))?\.?",
    re.IGNORECASE,
)

# Redundant English doublings that emerge after rewrites. ``Trend β = ...``
# becomes ``Trend trending down by ~$71K ...`` once β is rewritten — strip
# the leading "Trend " label since "trending" already carries the meaning.
_DOUBLE_TREND = re.compile(r"\bTrend\s+(trending\b)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def polish(text: str) -> str:
    """Apply the full plain-English polish pipeline to user-facing prose.

    Pipeline order matters — later passes assume earlier ones ran:

      1. Strip internal artifact paths (``/mnt/session/outputs/...``).
      2. Rewrite statistics formulas to prose (p-values, R², β, comparison tests).
      3. Strip "Trend trending" redundancy left by the β rewrite.
      4. Gloss acronyms at first use, leave subsequent uses bare.
      5. Consolidate inline ``Caveat:`` lines (best-effort — only fires
         when there are 2+ caveats; one inline caveat is left alone).
      6. Trim excess whitespace, drop orphan ``(N lines).`` lines, collapse
         3+ newlines.

    Returns the polished text. Pure function — input is never mutated.
    """
    if not text or not text.strip():
        return text

    out = text

    # 1. Strip internal artifact paths (incl. trailing ``(533 lines)`` suffix).
    out = _PATH_NOISE.sub("", out)

    # 2. Rewrite statistics formulas.
    for pattern, replacement in STAT_REWRITES:
        if callable(replacement):
            out = pattern.sub(replacement, out)
        else:
            out = pattern.sub(replacement, out)

    # 3. Strip "Trend trending" redundancy left by the β rewrite.
    out = _DOUBLE_TREND.sub(r"\1", out)

    # 4. Gloss acronyms at first use.
    out = _gloss_acronyms(out, ACRONYM_GLOSS)

    # 5. Consolidate inline caveats (best-effort).
    out = _consolidate_caveats(out)

    # 6. Trim whitespace and orphan lines left after path/caveat removal.
    out = re.sub(r"\n{3,}", "\n\n", out)
    out = re.sub(r"^[ \t]*\(\d+\s+lines?\)\.?[ \t]*$", "", out, flags=re.MULTILINE)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()

    return out


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _gloss_acronyms(text: str, glossary: dict[str, str]) -> str:
    """Replace the first occurrence of each glossary key with its expansion.

    Subsequent occurrences are left bare — the reader has the key now,
    and re-expanding every mention would bloat the prose. Skips matches
    that are already glossed (idempotency: if the expansion is already
    in the text, don't re-gloss).

    Matches on word boundaries so "NB" doesn't match inside "NBA" or
    "newcomers". Longer keys are tried first so "NB CW MTD" wins over
    "NB" alone.
    """
    # Sort longest-first so multi-token acronyms get matched before their
    # single-token components. Stable sort keeps insertion order for ties.
    keys = sorted(glossary.keys(), key=lambda k: (-len(k), k))

    for key in keys:
        expansion = glossary[key]
        # Idempotency check: if the expansion text already appears anywhere
        # in the document, skip glossing this key — it's already been done.
        if expansion.lower() in text.lower():
            continue
        pattern = re.compile(
            r"(?<![A-Za-z0-9_$])" + re.escape(key) + r"(?![A-Za-z0-9_])"
        )
        match = pattern.search(text)
        if match is None:
            continue
        # Replace only the FIRST occurrence with the expansion.
        text = text[: match.start()] + expansion + text[match.end() :]

    return text


_CAVEAT_LINE = re.compile(
    r"^(\s*)(?:Caveat|caveat):\s*(.+?)\s*$",
    re.MULTILINE,
)


def _consolidate_caveats(text: str) -> str:
    """Hoist 2+ inline ``Caveat: ...`` lines into one ``Caveats:`` block.

    A single inline caveat is left alone — it's already concise. When two
    or more appear in a single document we collect them, strip the
    originals, and append a consolidated ``Caveats`` block at the bottom.
    """
    matches = list(_CAVEAT_LINE.finditer(text))
    if len(matches) < 2:
        return text

    items: list[str] = [m.group(2).strip() for m in matches]

    # Strip the inline caveats. Replace each with an empty string so the
    # following blank-line collapse pass in polish() cleans up.
    cleaned = _CAVEAT_LINE.sub("", text)

    # Append the consolidated block at the end.
    bullet_lines = "\n".join(f"- {item}" for item in items)
    cleaned = cleaned.rstrip() + "\n\n*Caveats:*\n" + bullet_lines + "\n"
    return cleaned


def _iter_first_match_positions(text: str, keys: Iterable[str]) -> dict[str, int]:
    """Debug helper — return the first-match index of each key in ``text``.

    Not used in production code paths. Handy when fixturing new acronyms.
    """
    positions: dict[str, int] = {}
    for key in keys:
        idx = text.find(key)
        if idx >= 0:
            positions[key] = idx
    return positions
