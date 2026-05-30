"""Tests for ``agents/provision_rfp_reviewer_agent.py``.

Pins prompt-content invariants on the RFP Reviewer system prompt so a
silent regression cannot reintroduce a known failure mode without
tripping CI. The codex P1 on PR #253 (2026-05-20) is the canonical
example: a diff flipped the Kapa fact-verification block from "pace
your calls" to "Submit all Kapa fact-verification queries at once
using parallel tool calls", which bursts the 20 RPM cap on RFPs with
more than 20 product/both questions.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO_ROOT / "agents"

if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))


def _prompt() -> str:
    from provision_rfp_reviewer_agent import (  # type: ignore[import-not-found]
        RFP_REVIEWER_PROMPT,
    )

    return RFP_REVIEWER_PROMPT


def test_kapa_block_uses_bounded_batching_not_unbounded_parallel():
    """The Kapa fact-verification block must instruct bounded batching.

    Kapa's 20 RPM limit is requests-per-minute, not concurrent slots —
    "submit all queries at once in parallel" bursts the cap on any RFP
    with more than 20 product/both questions and forces the retry/error
    path onto a significant share of checks, degrading the quality gate
    precisely on the large RFPs that need it most.

    The shipped design is **3 queries in parallel, then ``sleep 15`` in
    bash before the next batch** — ~12 req/min steady-state, ~40%
    headroom under the cap for clock skew, the retry path, and
    concurrent Kapa traffic from Coordinator / Quick Answer / Dream /
    Post-Sales / Cross-Domain agents that share the same API key.
    """
    prompt = _prompt()
    # Three phrasings must be present so the intent is unambiguous to
    # the reviewer agent: bounded batches, the explicit batch size of 3,
    # and the explicit ``sleep 15`` pacing instruction.
    assert "bounded batches" in prompt, (
        "Expected the Kapa block to use the phrase 'bounded batches' so "
        "the reviewer paces queries against the 20 RPM cap. If you "
        "changed the wording, update this assertion AND verify the new "
        "wording still prevents unbounded parallel fan-out."
    )
    assert "3 queries in parallel" in prompt or "batches of 3" in prompt, (
        "Expected the Kapa block to name a concrete batch size of 3. "
        "A vague 'a few in parallel' invites drift; 3 is the pinned "
        "design (~12 req/min, ~40% headroom under the 20 RPM cap — "
        "5+15s was at the cap with no headroom and codex P1'd it)."
    )
    assert "sleep 15" in prompt, (
        "Expected the Kapa block to name the exact bash command "
        "``sleep 15`` so the reviewer agent knows the pacing mechanic, "
        "not just the intent. 15s between 3-query batches yields the "
        "designed ~12 req/min throughput."
    )
    assert "20 RPM" in prompt or "20 rpm" in prompt, (
        "Expected the Kapa block to name the 20 RPM cap explicitly so "
        "the reviewer understands WHY the batching constraint exists."
    )


def test_kapa_block_does_not_use_15_plus_60s_design():
    """Regression pin: prior fix attempts used 15 parallel + 60s sleep AND
    5 parallel + 15s sleep — both got codex P1s.

    15+60s over-corrected — bursty 15-query spikes still hit the cap and
    a 60s sleep wastes wall-clock on big RFPs. 5+15s was mathematically
    AT the 20 RPM cap (5 * 4 batches/min = 20) with zero headroom for
    clock skew, retries, or concurrent Kapa traffic from other agents.
    The shipped design is 3+15s — ~12 req/min, ~40% headroom.
    """
    prompt = _prompt()
    banned_size_15 = "15 parallel tool calls"
    banned_sleep_60 = "wait 60 seconds"
    banned_size_5 = "5 queries in parallel"
    assert banned_size_15 not in prompt, (
        f"The Reviewer prompt contains the banned phrase {banned_size_15!r}. "
        "That phrasing was the first over-corrected fix; the shipped "
        "design is 3 queries in parallel, not 15."
    )
    assert banned_sleep_60 not in prompt, (
        f"The Reviewer prompt contains the banned phrase {banned_sleep_60!r}. "
        "That phrasing was the first over-corrected fix; the shipped "
        "design uses ``sleep 15`` between 3-query batches, not 60s "
        "between 15-query batches."
    )
    assert banned_size_5 not in prompt, (
        f"The Reviewer prompt contains the banned phrase {banned_size_5!r}. "
        "That phrasing was the second over-corrected fix — 5*4 batches "
        "= 20 req/min was exactly at the cap with no headroom. The "
        "shipped design is 3 queries in parallel, not 5."
    )


def test_kapa_block_does_not_say_submit_all_at_once():
    """Regression pin for codex P1 on PR #253.

    The diff in commit ac801f5 flipped the Reviewer prompt from "pace
    your calls" to "Submit all Kapa fact-verification queries at once
    using parallel tool calls". That phrasing is the bug: it bursts the
    20 RPM Chat-endpoint cap on RFPs with more than 20 product/both
    questions, forcing the overflow into the retry path and ultimately
    marking those questions "verification unavailable" instead of
    actually verified — exactly degrading the quality gate on the
    large RFPs.
    """
    prompt = _prompt()
    banned = "Submit all Kapa fact-verification queries at once"
    assert banned not in prompt, (
        f"The Reviewer prompt contains the banned phrase {banned!r}. "
        "That phrasing instructs unbounded parallel fan-out against "
        "Kapa's 20 RPM cap and reintroduces the codex P1 from PR #253."
    )


def test_kapa_block_forbids_skipping_questions_to_stay_under_cap():
    """The fix must explicitly tell the reviewer to batch, NOT drop.

    The whole point of full-coverage review (the rubric's "no sampling")
    is that EVERY product/both answer gets a real Kapa check. The
    bounded-batching language must make clear that pacing is the right
    response to the rate limit, not skipping questions.
    """
    prompt = _prompt()
    assert "Do NOT skip" in prompt or "do not skip" in prompt, (
        "Expected an explicit prohibition on skipping questions to "
        "stay under the rate limit. Without it, an agent under time "
        "pressure could rationalize sampling — which violates the "
        "no-sampling rule the Reviewer was built to enforce."
    )
