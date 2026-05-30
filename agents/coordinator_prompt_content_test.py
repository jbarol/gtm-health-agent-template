"""Content tests for the Coordinator system prompt (Plan #52 PR-G Part A).

These tests assert that the Coordinator prompt carries the
``<watchdog_signals>`` directive — the rules the model follows when the
orchestrator's watchdog injects a Tier-1 ``[watchdog]`` nudge or a Tier-2
``user.interrupt``. The block is defined in
``agents/update_prompts.py`` between the closing ``</sub_agent_handles>``
tag and the closing triple-quoted string. Tests assert presence,
ordering, and the load-bearing literal strings the orchestrator injects
so a future refactor that "tightens" the wording can't silently drop the
guidance.

Why this lives in its own file: ``verify_active_versions_test.py`` checks
version-pin parity (semantic — does the local pin match the live
Anthropic version?), not prompt content. Content checks belong in their
own home so a prompt-text edit fails one clear test rather than tripping
unrelated version-parity assertions.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO_ROOT / "agents"

# Ensure agents/ is importable when pytest runs from repo root or agents/.
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))


def _coordinator_prompt() -> str:
    """Re-render the Coordinator prompt as a fresh string each call.

    Reading ``PROMPTS["coordinator"]`` returns the final string after all
    ``+=`` appends have run at import time, which is what the live agent
    sees.
    """
    import update_prompts  # type: ignore

    return update_prompts.PROMPTS["coordinator"]


def test_coordinator_prompt_contains_watchdog_signals_block():
    """The ``<watchdog_signals>`` block is present, opened and closed."""
    prompt = _coordinator_prompt()
    assert "<watchdog_signals>" in prompt
    assert "</watchdog_signals>" in prompt


def test_coordinator_prompt_names_tier1_tier2_tier3():
    """All three tier names appear so the directive maps to the watchdog ladder.

    The orchestrator's ``session_watchdog.py`` has exactly three tiers; the
    prompt must reference all of them so the Coordinator knows which
    signals it observes (Tier 1, Tier 2) and which one ends its session
    by definition (Tier 3, never observed by the model).
    """
    prompt = _coordinator_prompt()
    assert "Tier 1" in prompt
    assert "Tier 2" in prompt
    assert "Tier 3" in prompt


def test_coordinator_prompt_includes_literal_watchdog_token():
    """The literal ``[watchdog]`` token appears verbatim.

    ``orchestrator/session_watchdog.py:_send_tier1_wakeup`` prefixes every
    nudge with ``[watchdog] One of your dispatched sub-agents...``. If the
    prompt diverges from that exact token, the Coordinator can't match the
    nudge as a meta-signal and may treat it as a user message.
    """
    prompt = _coordinator_prompt()
    assert "[watchdog]" in prompt


def test_coordinator_prompt_forbids_redispatch_same_subagent():
    """The directive forbids re-dispatching the same sub-agent that timed out.

    This is the exact failure-mode the watchdog is trying to break — a
    Coordinator re-dispatching a stranded specialist burns the remaining
    window before Tier 3 archives the session. The substring match keeps
    a future prompt edit honest.
    """
    prompt = _coordinator_prompt()
    assert "Do NOT re-dispatch the same sub-agent" in prompt


def test_coordinator_prompt_forbids_synthetic_numbers():
    """The directive forbids inventing numbers when a sub-agent did not return.

    The ``_detect_fabricated_rows_in_payload`` guard rejects payloads with
    admitted-fabricated rows; the prompt rule exists so the Coordinator
    avoids the wasted turn in the first place.
    """
    prompt = _coordinator_prompt()
    assert "Do NOT synthesize numbers from nothing" in prompt


def test_coordinator_prompt_watchdog_block_after_subagent_handles():
    """The ``<watchdog_signals>`` block lands AFTER ``</sub_agent_handles>``.

    Ordering matters: the directive references sub-agent dispatch vocabulary
    (primary/non-primary threads, dispatch imbalance, tool capability map)
    introduced in ``<sub_agent_handles>``. Moving the directive earlier
    leaves those concepts undefined when the rules are read top-to-bottom.
    """
    prompt = _coordinator_prompt()
    sub_close = prompt.find("</sub_agent_handles>")
    watchdog_open = prompt.find("<watchdog_signals>")
    assert sub_close > 0, "sub_agent_handles close tag missing"
    assert watchdog_open > 0, "watchdog_signals open tag missing"
    assert watchdog_open > sub_close, (
        "watchdog_signals must appear AFTER </sub_agent_handles> in render order"
    )


def test_coordinator_prompt_token_budget():
    """Sanity-bound the Coordinator prompt size to catch future bloat.

    Today the rendered prompt is ~64K chars. The PR-G directive adds ~5K
    chars (~3% bump). A 100K cap is well above current size and well below
    the levels at which input-token spend becomes a concern. If a future
    edit pushes past this bound, the editor should justify it explicitly.
    """
    prompt = _coordinator_prompt()
    assert len(prompt) < 100_000, (
        f"Coordinator prompt is {len(prompt)} chars; budget is 100_000"
    )
