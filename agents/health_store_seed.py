"""Pure data for seeding the per-portco ``instructions.md`` placeholder.

Why this exists
---------------

The health memory store is mounted at ``/mnt/memory/<portco>/`` inside every
Managed Agent session. The agent prompts (see ``agents/update_prompts.py`` and
``orchestrator/session_runner.py``) instruct agents to read
``/{portco}/instructions.md`` FIRST on every run — it carries mandatory data
rules (which fields to use, what to exclude, how to segment).

If the file does not exist yet, Anthropic's underlying memory-tool
implementation runs ``awk`` against the path and surfaces a raw shell error
back to the agent::

    awk: cannot open "/mnt/memory/<portco>/instructions.md" (No such file or directory)

That error appears as a failed tool call in every fresh session until a user
writes feedback via the Slack "remember"/"always"/"never" loop. We can't
suppress the error on Anthropic's side, so we seed an empty-but-valid
placeholder in this module and call it from ``setup_agents.py``.

The placeholder header matches the H1 line produced at runtime by
``orchestrator.main.on_slack_feedback`` so subsequent appends (the
file-exists branch of that function) produce the same canonical document
shape we would have seen if a user had triggered the first-write branch.

Keep this module pure (no SDK calls, no I/O). Tests import the data directly.
"""

from __future__ import annotations


def instructions_md_path(portco_key: str) -> str:
    """Return the memory-store path for a portco's ``instructions.md``."""
    return f"/{portco_key}/instructions.md"


def instructions_md_seed_content(portco_key: str) -> str:
    """Return the seed body for a portco's ``instructions.md``.

    The H1 header matches what ``on_slack_feedback`` writes on its
    first-write path, so the resulting document has the same canonical
    shape whether the file was seeded here or created lazily on first
    feedback. ``on_slack_feedback`` will see the seeded file via its
    "file exists -> append" branch and concatenate new bullets normally.
    """
    return (
        f"# Standing Instructions — {portco_key.title()}\n\n"
        "Portco-specific instructions for all investigations. Populated via "
        'Slack messages starting with "remember", "always", or "never".\n'
    )


def instructions_md_seed(portco_key: str) -> tuple[str, str]:
    """Return ``(path, content)`` for the per-portco ``instructions.md`` seed.

    Caller is responsible for invoking ``memory_stores.memories.create`` on
    the tuple.
    """
    return instructions_md_path(portco_key), instructions_md_seed_content(portco_key)
