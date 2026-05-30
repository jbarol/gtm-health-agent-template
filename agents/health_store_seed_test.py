"""Tests for the per-portco instructions.md seed helper.

The bug being prevented: every fresh Managed Agent session was emitting

    awk: cannot open "/mnt/memory/<portco>/instructions.md" (No such file or directory)

because the agent prompts (in ``agents/update_prompts.py`` and
``orchestrator/session_runner.py``) all instruct the agent to read
``/{portco}/instructions.md`` FIRST, but that file did not exist until a user
sent feedback via the Slack "remember"/"always"/"never" loop. ``setup_agents.py``
now seeds an empty placeholder during environment provisioning so the file
always exists from the start.

These tests pin three things:

1. The seed path matches what the agent prompts actually read.
2. The seed header matches what ``orchestrator.main.on_slack_feedback`` writes
   on first use, so subsequent appends are byte-compatible.
3. ``setup_agents.py`` actually calls the seed helper for the active portco
   (caught with a string-grep so we don't have to import the module — which
   would force ``anthropic.Anthropic()`` to run with credentials present).

Run:
    python3 -m pytest agents/health_store_seed_test.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Self-contained sys.path setup so this test runs from either repo root or
# the agents/ directory.
_AGENTS_DIR = Path(__file__).parent
if str(_AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENTS_DIR))

from health_store_seed import (  # noqa: E402
    instructions_md_path,
    instructions_md_seed,
    instructions_md_seed_content,
)


# ---------------------------------------------------------------------------
# Path + content shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "portco_key,expected_path",
    [
        ("acme", "/acme/instructions.md"),
        ("delta", "/delta/instructions.md"),
        ("acme", "/acme/instructions.md"),
    ],
)
def test_instructions_md_path_matches_prompt_reads(portco_key, expected_path):
    """The seed path must match the path the prompts and runtime feedback use.

    If this diverges, agents will read one path and feedback will write to
    another, splitting the standing-instructions surface in half.
    """
    assert instructions_md_path(portco_key) == expected_path


def test_seed_returns_path_and_content_tuple():
    path, content = instructions_md_seed("acme")
    assert path == "/acme/instructions.md"
    assert isinstance(content, str)
    assert content == instructions_md_seed_content("acme")


def test_seed_content_has_standing_instructions_header():
    """The seeded body must use the same H1 header on_slack_feedback writes.

    on_slack_feedback creates the file (when none exists) with:
        f"# Standing Instructions — {portco_key.title()}\\n\\n"
    Since the seed path makes that "no file" branch unreachable for new
    portcos, the on_slack_feedback flow will always hit the "file exists ->
    append" branch instead. Keeping the same H1 line means agents reading
    the file see exactly one canonical header regardless of whether the
    file was seeded or runtime-created.
    """
    content = instructions_md_seed_content("acme")
    assert content.startswith("# Standing Instructions — Acme\n\n")


def test_seed_content_mentions_remember_keywords():
    """The placeholder body should hint at HOW the file gets populated.

    Operators reading the seed straight from the memory store should not
    have to grep code to find out the file fills via Slack messages.
    """
    content = instructions_md_seed_content("acme")
    assert "remember" in content
    assert "always" in content
    assert "never" in content


def test_seed_content_ends_with_newline():
    """Memory-store entries should end with a trailing newline so future
    bullet appends from on_slack_feedback don't collide with the header.
    """
    content = instructions_md_seed_content("acme")
    assert content.endswith("\n")


# ---------------------------------------------------------------------------
# Integration: setup_agents.py wires the helper in
# ---------------------------------------------------------------------------


def test_setup_agents_calls_instructions_md_seed():
    """setup_agents.py must invoke instructions_md_seed for at least one portco.

    We grep the source rather than importing it because importing
    setup_agents.py runs ``anthropic.Anthropic()`` at module load, which
    requires credentials and makes a network call.
    """
    setup_src = (_AGENTS_DIR / "setup_agents.py").read_text()
    assert "from health_store_seed import instructions_md_seed" in setup_src, (
        "setup_agents.py must import instructions_md_seed from "
        "health_store_seed — otherwise the instructions.md placeholder will "
        "not be created during environment provisioning and every fresh "
        "session will surface the awk error again."
    )
    assert "instructions_md_seed(_key)" in setup_src, (
        "setup_agents.py must call instructions_md_seed for each configured "
        "portco at provisioning time so /<key>/instructions.md exists "
        "before the first Managed Agent session reads it."
    )
