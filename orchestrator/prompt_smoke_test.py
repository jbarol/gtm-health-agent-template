"""Smoke checks against the agent system prompts.

These tests load the prompt strings from ``agents/`` and assert that
known classification shapes and validation rules are present in the
exact prompt string that the deploy workflow will push to Anthropic.

The intent is to catch regressions where the response_shape taxonomy
drifts between the Prompt Engineer, the Coordinator, and the Writing
Agent (the prior write_prose tool was retired 2026-05-27 when the
Writing Agent moved into the Coordinator's multiagent roster) — the
failure mode the demo question ("Show opps closing this quarter.
Propensity + reference customers + product updates + rep trends. Word
+ Excel.") hit when it was misclassified as ``data_pull`` and skipped
the validation pipeline.

The tests are pure string contains — no network, no Anthropic calls.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make ``agents/`` importable. ``agents/update_prompts.py`` populates the
# PROMPTS dict at module load time (everything except writing_agent,
# which is lazily loaded by main() at deploy time and not needed here).
_AGENTS_DIR = Path(__file__).resolve().parent.parent / "agents"
if str(_AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENTS_DIR))


def _load_prompt_engineer_prompt() -> str:
    """Return the live Prompt Engineer system prompt string."""
    import update_prompts as up  # type: ignore

    return up.PROMPTS["prompt_engineer"]


def _load_coordinator_prompt_via_setup_agents() -> str:
    """Return the Coordinator system prompt as written in setup_agents.py.

    The Coordinator system prompt lives inside an ``agents.create(...)``
    call in setup_agents.py — we read the file as text and assert
    against the raw source so the test catches prompt drift in the
    exact bytes that will be pushed to Anthropic.

    update_prompts.PROMPTS["coordinator"] holds the deploy-path
    version of the same prompt; we check both to cover provisioning
    AND prompt-deploy code paths.
    """
    setup_agents_py = _AGENTS_DIR / "setup_agents.py"
    return setup_agents_py.read_text()


def _load_coordinator_prompt_via_update_prompts() -> str:
    """Return the Coordinator system prompt from update_prompts.PROMPTS."""
    import update_prompts as up  # type: ignore

    return up.PROMPTS["coordinator"]


# ---------------------------------------------------------------------------
# Prompt Engineer — hybrid_data_synthesis must be in the taxonomy
# ---------------------------------------------------------------------------


def test_prompt_engineer_lists_hybrid_data_synthesis_shape():
    """The Prompt Engineer prompt must define hybrid_data_synthesis.

    Without this, the agent can never emit the shape — every mixed-intent
    question falls through to data_pull and skips Adversarial Reviewer +
    Statistician + Writing Agent delegation. That is the failure mode
    this PR exists to fix.
    """
    prompt = _load_prompt_engineer_prompt()
    assert "hybrid_data_synthesis" in prompt, (
        "Prompt Engineer must define hybrid_data_synthesis as a "
        "response_shape — otherwise every mixed-intent question "
        "(data pull + analysis + prose) gets misclassified as "
        "data_pull and skips the validation pipeline."
    )


def test_prompt_engineer_response_shape_is_in_json_output_schema():
    """The Prompt Engineer must emit response_shape in its JSON output."""
    prompt = _load_prompt_engineer_prompt()
    assert "`response_shape`" in prompt, (
        "Prompt Engineer JSON output schema must declare "
        "`response_shape` so the orchestrator can forward it to the "
        "Coordinator."
    )
    # And the enum must list every shape — guard against silent drops.
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
        assert shape in prompt, (
            f"response_shape '{shape}' missing from Prompt Engineer prompt"
        )


def test_prompt_engineer_biases_toward_hybrid_on_ambiguous():
    """Prompt Engineer must err on the side of hybrid_data_synthesis.

    Misclassifying as data_pull silently ships unvalidated numbers;
    misclassifying as hybrid_data_synthesis only adds Adversarial
    Reviewer + Statistician latency. The classifier MUST be biased
    toward the safer shape on 50/50 calls.
    """
    prompt = _load_prompt_engineer_prompt()
    assert "err on the side of `hybrid_data_synthesis`" in prompt or (
        "pick `hybrid_data_synthesis`" in prompt
    ), (
        "Prompt Engineer prompt must explicitly bias the classifier "
        "toward hybrid_data_synthesis on ambiguous mixed-intent "
        "questions — otherwise the heuristic skews toward the "
        "shortcut path."
    )


# ---------------------------------------------------------------------------
# Coordinator — hybrid_data_synthesis must mandate the full pipeline
# ---------------------------------------------------------------------------


def test_coordinator_prompt_in_setup_agents_handles_hybrid_data_synthesis():
    """Coordinator prompt (setup_agents.py) must mandate validation on hybrid."""
    src = _load_coordinator_prompt_via_setup_agents()
    assert "hybrid_data_synthesis" in src, (
        "Coordinator prompt in setup_agents.py must mention "
        "hybrid_data_synthesis — without this branch the Coordinator "
        "applies the data-pull-only shortcut to mixed-intent "
        "questions and skips Adversarial Reviewer + Statistician."
    )
    # The mandatory validation rule must be spelled out — not just a
    # passing reference to the shape name.
    assert (
        "Adversarial Reviewer review of every finding" in src
        and "Statistician validation of every quantitative claim" in src
        and "Writing Agent delegation for the user-facing narrative" in src
    ), (
        "Coordinator prompt must spell out the mandatory steps for "
        "hybrid_data_synthesis: Adversarial Reviewer + Statistician + "
        "Writing Agent delegation. Generic mentions of validation don't "
        "fix the shortcut bug. (Writing Agent delegation supersedes the "
        "retired write_prose custom tool — see CLAUDE.md Writing pass.)"
    )


def test_coordinator_prompt_in_update_prompts_handles_hybrid_data_synthesis():
    """Coordinator prompt (update_prompts.PROMPTS) lists the new shape."""
    prompt = _load_coordinator_prompt_via_update_prompts()
    # update_prompts.PROMPTS["coordinator"] is what the deploy workflow
    # pushes. setup_agents.py is the provisioning-time source. Both
    # must agree on the response_shape taxonomy.
    assert "hybrid_data_synthesis" in prompt, (
        "update_prompts.PROMPTS['coordinator'] must mention "
        "hybrid_data_synthesis — this is the prompt the CI deploy "
        "workflow pushes to Anthropic on every merge to main."
    )


# ---------------------------------------------------------------------------
# Writing Agent roster wiring — the Coordinator delegates prose to the
# Writing Agent via the multiagent runtime (since 2026-05-27, the prior
# WRITE_PROSE_TOOL custom-tool path was retired). The shape-enum guard
# now lives in the Coordinator prompt + the Writing Agent prompt, not
# in a tool schema.
# ---------------------------------------------------------------------------


def test_coordinator_prompt_delegates_to_writing_agent_not_tool():
    """The Coordinator prompt must instruct delegation, not a tool call.

    Regression — a stale prompt that still says "call write_prose"
    will produce ``agent.custom_tool_use`` events the orchestrator no
    longer dispatches, stranding the session.
    """
    import update_prompts

    coord_prompt = update_prompts.PROMPTS["coordinator"]
    assert "Writing Agent" in coord_prompt, (
        "Coordinator prompt must name the Writing Agent — that's the "
        "sub-agent it delegates prose composition to."
    )
    assert "Delegate" in coord_prompt or "delegate" in coord_prompt, (
        "Coordinator prompt must instruct delegation to the Writing Agent."
    )
    # The retired ``write_prose`` tool name should NOT appear as a tool
    # call instruction. (Historical mentions in scope-of-work comments
    # — e.g. inside diff annotations — are still allowed; we check the
    # active instruction surface area.)
    assert "Call the `write_prose` tool" not in coord_prompt, (
        "Stale instruction — the write_prose custom tool was retired "
        "2026-05-27 when the Writing Agent moved into the Coordinator's "
        "multiagent roster."
    )


# ---------------------------------------------------------------------------
# Writing Agent prompt — response_shapes block must list hybrid_data_synthesis
# ---------------------------------------------------------------------------


def test_writing_agent_response_shapes_block_includes_hybrid_data_synthesis():
    """Writing Agent system prompt must teach hybrid_data_synthesis.

    The Coordinator passes response_shape through unchanged when it
    delegates to the Writing Agent via the multiagent runtime. If the
    Writing Agent's <response_shapes> block does not name
    `hybrid_data_synthesis`, the agent has no shape-specific sizing
    rule for hybrid questions and the prose silently degrades to
    undefined behavior (codex P2 review on PR #195).
    """
    writing_agent_py = (
        Path(__file__).resolve().parent / "writing_agent.py"
    ).read_text()
    # The block is named in the system prompt source — find it and
    # assert the new shape sits inside.
    block_start = writing_agent_py.find("<response_shapes>")
    block_end = writing_agent_py.find("</response_shapes>", block_start)
    assert block_start != -1 and block_end != -1, (
        "Writing Agent <response_shapes> block not found — the prompt "
        "structure changed and this assertion needs an update."
    )
    block = writing_agent_py[block_start:block_end]
    assert "hybrid_data_synthesis" in block, (
        "Writing Agent <response_shapes> block must list "
        "`hybrid_data_synthesis` — without an explicit sizing rule the "
        "agent receives the new shape value but has no instruction on "
        "how to compose the prose."
    )


# ---------------------------------------------------------------------------
# Orchestrator wiring — response_shape must flow PE → Coordinator
# ---------------------------------------------------------------------------


def test_main_preprocess_prompt_requests_response_shape_in_json_schema():
    """main._preprocess_prompt kickoff must ask the Prompt Engineer for response_shape.

    Without this, the PE may classify the shape but the runtime kickoff
    JSON schema doesn't request the field, so the response is dropped
    on the floor before it can reach the Coordinator (codex P2 review).
    """
    main_py = (Path(__file__).resolve().parent / "main.py").read_text()
    # The preprocess prompt is a multi-line f-string. The marker we
    # search for is the field name in quotes — that's what the model
    # parses against.
    assert '"response_shape"' in main_py, (
        "main._preprocess_prompt must request response_shape in the "
        "kickoff JSON schema — otherwise the Prompt Engineer's "
        "classification is silently dropped."
    )


def test_session_runner_build_adhoc_prompt_handles_response_shape():
    """_build_adhoc_prompt must accept response_shape and inject the hint.

    The Coordinator system prompt mandates Adversarial + Statistician +
    Writing-Agent-delegation on hybrid questions, but the binding has
    to be visible in the first user turn to reliably land before the
    Coordinator re-derives the shape from prose (codex P2 review).
    """
    sr_py = (Path(__file__).resolve().parent / "session_runner.py").read_text()
    assert "response_shape: str = None" in sr_py, (
        "_build_adhoc_prompt must accept a response_shape kwarg."
    )
    # The kickoff must mention the mandatory pipeline when the shape
    # is hybrid_data_synthesis.
    assert (
        'response_shape == "hybrid_data_synthesis"' in sr_py
        or "hybrid_data_synthesis" in sr_py
    ), (
        "_build_adhoc_prompt must inject the mandatory-validation hint "
        "when response_shape == 'hybrid_data_synthesis'."
    )
