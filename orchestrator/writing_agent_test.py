"""Unit tests for the Writing Agent prompt source-of-truth.

The Writing Agent moved into the Coordinator's multiagent roster on
2026-05-27. The orchestrator-side ``write_prose()`` dispatcher and its
``_dispatch_write_prose`` adapter are gone — the multiagent runtime now
owns the Writing Agent's session thread within the parent session.

What's left to test here:

  1. The system prompt still contains Strunk's *Elements of Style*
     grounding (rules + banned habit-words + Report-Writer-salvage
     anti-patterns + acronym glossary). The Coordinator's rejection
     rubric assumes these are present.
  2. The system prompt names the new delegation contract — Writing Agent
     is a sub-agent, its thread persists across delegations, it must not
     narrate prior attempts on rewrites.
  3. ``build_user_message`` emits the canonical ``{response_shape,
     payload, feedback?}`` JSON the Coordinator's prompt instructs it
     to send. Tests assert the shape so doc + code stay in sync.

Run:
    cd orchestrator && python3 -m pytest writing_agent_test.py -q
"""

from __future__ import annotations

import json


# ---------------------------------------------------------------------------
# build_system_prompt — Strunk grounding
# ---------------------------------------------------------------------------


def test_system_prompt_contains_strunk_rules():
    """The Writing Agent must be grounded in Strunk's classics."""
    from writing_agent import build_system_prompt

    prompt = build_system_prompt()

    # Strunk's classics — at least the rule numbers must be present so we
    # know the grounding made it into the final prompt.
    assert "Rule 10" in prompt, "active voice rule missing"
    assert "active voice" in prompt.lower()
    assert "Rule 11" in prompt, "positive form rule missing"
    assert "positive form" in prompt.lower()
    assert "Rule 12" in prompt, "definite specific concrete rule missing"
    assert "definite" in prompt.lower() and "specific" in prompt.lower()
    assert "Rule 13" in prompt, "omit needless words rule missing"
    assert "omit needless words" in prompt.lower() or "needless words" in prompt.lower()
    assert "Rule 18" in prompt, "emphatic-words-at-end rule missing"


def test_system_prompt_lists_response_shapes():
    """The agent must know the response-shape vocabulary."""
    from writing_agent import build_system_prompt

    prompt = build_system_prompt()
    for shape in (
        "one_fact",
        "comparative",
        "why",
        "briefing",
        "table",
        "methodology",
        "data_pull",
        "hybrid_data_synthesis",
    ):
        assert shape in prompt, f"response_shape '{shape}' missing from prompt"


def test_system_prompt_bans_stats_notation():
    """Stats notation must be explicitly banned in the prompt."""
    from writing_agent import build_system_prompt

    prompt = build_system_prompt()
    for banned in ("p=", "Wilcoxon", "R²="):
        assert banned in prompt, f"banned token '{banned}' not warned against"


def test_system_prompt_requires_acronym_glossing():
    """The agent must be told to gloss acronyms at first use."""
    from writing_agent import build_system_prompt

    prompt = build_system_prompt()
    assert "acronym" in prompt.lower(), "acronym-glossing rule missing"
    assert "first use" in prompt.lower()


# ---------------------------------------------------------------------------
# Merged-prompt assertions — verify the salvage from the retired Report
# Writer survived the merge. These cover the union of Strunk rules and
# anti-pattern coverage from both originals.
# ---------------------------------------------------------------------------


def test_merged_prompt_carries_strunk_rule_union():
    """The merged prompt must contain every Strunk rule from BOTH originals.

    Writing Agent original had Rules 10, 11, 12, 13, 14, 16, 18.  Report
    Writer didn't quote Strunk by rule number but enforced the same body
    of rules through anti-patterns.  After the merge the explicit rule
    numbers must remain so the agent has the grounding intact.
    """
    from writing_agent import build_system_prompt

    prompt = build_system_prompt()
    for rule in (
        "Rule 10",
        "Rule 11",
        "Rule 12",
        "Rule 13",
        "Rule 14",
        "Rule 16",
        "Rule 18",
    ):
        assert rule in prompt, f"Strunk {rule} dropped from merged prompt"
    # Banned habit-words list (Strunk) — salvaged into the merged prompt.
    for word in (
        "case",
        "character",
        "factor",
        "feature",
        "interesting",
        "nature",
        "system",
    ):
        assert word in prompt, f"banned habit-word '{word}' missing"


def test_merged_prompt_carries_report_writer_anti_patterns():
    """Anti-patterns salvaged from the retired Report Writer prompt."""
    from writing_agent import build_system_prompt

    prompt = build_system_prompt()
    # PE-firm audience framing — pulled forward from the Report Writer.
    assert "PE" in prompt or "private equity" in prompt
    assert "phone" in prompt.lower(), "phone-reader framing missing"
    # Strunk Rule 14 example salvaged: comma-stacked dump is BAD.
    assert "two sentences" in prompt.lower()
    # Number discipline — verbatim numbers, K/M/B suffix, 1 decimal pct.
    assert "$1.4M" in prompt or "K/M/B" in prompt
    # CHALLENGED-finding handling salvaged from Report Writer.
    assert "still being investigated" in prompt.lower()
    # All-clear reports are valuable — salvaged.
    assert "all-clear" in prompt.lower() or "no critical findings" in prompt.lower()
    # Adversarial verdict-token ban salvaged from Report Writer's rule 7.
    for token in ("PASS WITH CAVEATS", "REVISE", "CHALLENGE"):
        assert token in prompt, f"verdict token '{token}' not warned against"
    # Decision-options must end with a recommendation — salvaged Rule 4.
    assert "Recommended:" in prompt


def test_merged_prompt_carries_acronym_glossary():
    """Standard acronym expansions list must survive the merge."""
    from writing_agent import build_system_prompt

    prompt = build_system_prompt()
    # A representative sample of the salvaged glossary from the Report Writer.
    for acronym in ("NB", "ARR", "GRR", "NRR", "MQL", "ICP", "CSM", "DTC"):
        assert acronym in prompt, f"acronym '{acronym}' dropped from glossary"


# ---------------------------------------------------------------------------
# Multiagent delegation contract — new section added 2026-05-27 when the
# Writing Agent moved into the Coordinator's multiagent roster.
# ---------------------------------------------------------------------------


def test_system_prompt_names_multiagent_delegation_contract():
    """The agent must know it's a sub-agent of the Coordinator's roster.

    Without this section the agent could mistake itself for a top-level
    composer or wait for a tool-call shape it will never receive.
    """
    from writing_agent import build_system_prompt

    prompt = build_system_prompt()
    lowered = prompt.lower()
    assert "multiagent" in lowered, "multiagent dispatch contract missing"
    assert "coordinator" in lowered, "Coordinator-relationship framing missing"
    assert "sub-agent" in lowered or "sub_agent" in lowered or "roster" in lowered, (
        "the agent must know it is the Coordinator's sub-agent"
    )


def test_system_prompt_names_persistent_thread_behavior():
    """The agent must know its thread persists across delegations.

    On a rewrite, the agent will see its prior draft + the Coordinator's
    rejection feedback in its context. The prompt must instruct it to
    NOT narrate the prior attempt — just return a fresh JSON object that
    addresses the feedback. Without this guidance, the agent will pad
    rewrites with "Here is the revised draft" preambles that pollute
    the post_report prose.
    """
    from writing_agent import build_system_prompt

    prompt = build_system_prompt()
    lowered = prompt.lower()
    assert "persistent" in lowered, "thread-persistence note missing"
    # The exact phrasing of "no preamble on rewrites" varies; check for
    # the explicit ban on "Here is the revised draft"-style language.
    assert (
        "revised draft" in lowered
        or "prior draft" in lowered
        or "do not narrate" in lowered
    ), "rewrite-narration ban missing — agent will pad rewrites with preambles"


def test_system_prompt_expects_json_input_payload():
    """The prompt must describe the input payload shape the Coordinator sends.

    The Coordinator's prompt instructs it to paste
    ``{"response_shape": ..., "payload": ...}`` JSON into the message.
    The Writing Agent must know to parse that shape — otherwise it will
    treat the JSON as prose and emit malformed output.
    """
    from writing_agent import build_system_prompt

    prompt = build_system_prompt()
    assert '"response_shape"' in prompt, "response_shape input key not documented"
    assert '"payload"' in prompt, "payload input key not documented"


# ---------------------------------------------------------------------------
# build_user_message — canonical delegation payload shape
# ---------------------------------------------------------------------------


def test_user_message_emits_response_shape_and_payload():
    """The canonical delegation payload nests payload under a top-level key.

    The Coordinator's prompt instructs it to send
    ``{"response_shape": ..., "payload": {...}}`` — this helper documents
    that shape so test fixtures and the prompt stay aligned.
    """
    from writing_agent import build_user_message

    payload = {
        "response_type": "ad_hoc_investigation_result",
        "headline": "Q1 win rate at 23.4 percent",
        "findings": [
            {"headline": "Q1 win rate dropped 5 points", "value": "23.4 percent"}
        ],
    }
    msg = build_user_message(payload, "briefing")
    parsed = json.loads(msg)

    assert parsed["response_shape"] == "briefing"
    assert parsed["payload"] == payload
    # Should NOT carry a feedback key on the first turn.
    assert "feedback" not in parsed


def test_user_message_includes_feedback_on_retry():
    """A rewrite delegation appends a feedback string."""
    from writing_agent import build_user_message

    payload = {"headline": "x"}
    feedback = (
        "rewrite without stats notation; translate Wilcoxon p=0.001 to plain English"
    )
    msg = build_user_message(payload, "briefing", feedback=feedback)
    parsed = json.loads(msg)

    assert parsed["feedback"] == feedback
    # The payload still has to be there — agents need both for a rewrite.
    assert parsed["payload"] == payload


def test_user_message_first_turn_has_no_retry_flavor():
    """First-turn messages must not carry rewrite-flavor language.

    Without this guard, the Coordinator's rejection logging could end up
    in the first-turn body and confuse the model.
    """
    from writing_agent import build_user_message

    msg = build_user_message({"headline": "x"}, "briefing")
    parsed = json.loads(msg)
    assert "feedback" not in parsed
    # And nothing in the serialized text should hint at "rewrite" / "rejected".
    assert "rewrite" not in msg.lower()
    assert "rejected" not in msg.lower()


# ---------------------------------------------------------------------------
# WritingAgentResult — the historical shape is still consumed by the
# duplicate-retry-cache logic in session_runner.py. Keep one round-trip
# test so the dataclass shape doesn't drift away from what that logic
# documents.
# ---------------------------------------------------------------------------


def test_writing_agent_result_to_dict_roundtrip():
    """The dataclass shape that the cache-logic comment documents."""
    from writing_agent import WritingAgentResult

    success = WritingAgentResult(ok=True, prose="A line of prose.", error="")
    payload = success.to_dict()
    assert payload["ok"] is True
    assert payload["prose"] == "A line of prose."
    # Every WritingAgentResult ALWAYS includes an ``error`` key — the
    # duplicate-retry cache in session_runner.py relies on this when
    # interpreting tool-result payloads.
    assert "error" in payload
    assert payload["error"] == ""

    failure = WritingAgentResult(ok=False, error="timeout")
    fp = failure.to_dict()
    assert fp["ok"] is False
    assert fp["error"] == "timeout"
    assert fp["prose"] == ""
