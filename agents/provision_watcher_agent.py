"""Provision the autonomous ❌-Watcher Managed Agent ONCE.

Phase 1, PR 4 of the autonomous ❌-Watcher rollout. Run this once per
deployment to mint the new Anthropic Managed Agent. After running, the
printed ``WATCHER_AGENT_ID`` goes into .env locally + Railway env vars,
then redeploy.

The agent is invoked by ``orchestrator/watcher_worker.py:_run_watcher_job``
(currently a stub in PR 3 — PR 4 wires the real dispatch in a follow-up
that drops in here once the tool dispatcher in PR 5 lands).

DESIGN DEVIATION FROM watcher-design-20260521-210800.md
=========================================================

The design originally specified a GitHub MCP server with PAT bearer
auth pinned at agent provisioning. Reality on inspection of the
``anthropic-sdk-python`` 0.92+ types:

    - ``BetaManagedAgentsURLMCPServerParams`` exposes ONLY ``name``,
      ``type``, ``url`` — no ``authorization_token`` field.
    - ``BetaRequestMCPServerURLDefinitionParam`` does expose
      ``authorization_token`` BUT it is for direct Messages API calls,
      not Managed Agents.
    - ``sessions.create`` accepts ``resources`` (file/github_repo/
      memory_store) but not per-MCP-server auth.

Conclusion: the SDK does not currently support secret-bearing MCP
servers at the Managed Agents layer. To preserve the design's
TIGHT 4-TOOL ALLOWLIST while staying inside what the SDK supports, this
file registers **custom tools** instead of MCP toolsets. The
orchestrator's ``_dispatch_tool`` (PR 5) handles each by calling the
GitHub REST API with the orchestrator-side PAT. Same 4-tool allowlist,
same intent, but the PAT lives in the orchestrator instead of the
agent server-side config.

Trade-offs vs the original design:

    + PAT never reaches Anthropic's infrastructure — strictly better
      from a credential-isolation perspective.
    + Allowlist is enforced by construction (only 4 tools exist), not
      by a deny-by-default filter in ``_dispatch_tool``.
    + No "Anthropic-stored MCP URL + header" — PAT rotation is a
      simple Railway env var flip, no agent re-provisioning needed.
    - Loses 46 other GH MCP tools the agent could theoretically use
      (e.g. label management, project boards). Out of scope for v1
      anyway.

Idempotency: this script does NOT check whether a watcher agent already
exists. Running it twice creates two agents — intentional. If you need
a clean slate, archive the old agent via the Anthropic Console first.

Run:
    python agents/provision_watcher_agent.py           # mint a new agent
    python agents/provision_watcher_agent.py --dry-run # print the spec
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

dotenv = Path(__file__).parent.parent / ".env"
if dotenv.exists():
    for line in dotenv.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


WATCHER_MODEL_INVESTIGATE = "claude-opus-4-8"  # reasoning-heavy diagnosis
WATCHER_MODEL_FIX = "claude-sonnet-4-6"  # code-friendly, cache-friendly
# Provisioned with the investigate model. The fix-writing pass is a
# follow-up session using a separate agent (PR 4 follow-up will mint a
# ``WATCHER_FIX_AGENT_ID`` if the cost/latency split proves worthwhile).
# For v1 we run both passes through one Opus 4.8 agent and revisit.
WATCHER_MODEL = WATCHER_MODEL_INVESTIGATE


# ───────────────────────────────────────────────────────────────────────
# Custom-tool schemas
# ───────────────────────────────────────────────────────────────────────
#
# Four tools, no more, no less. Every name here MUST match the dispatch
# branch in ``orchestrator/session_runner._dispatch_tool`` that PR 5
# adds. Output envelopes follow the same ``{ok, ...}`` shape as the
# existing custom tools.

WATCHER_CREATE_BRANCH_TOOL = {
    "type": "custom",
    "name": "watcher_create_branch",
    "description": (
        "Create a new branch in the gtm-health-agent repo for your fix. "
        "The branch is forked from main HEAD. Pick a descriptive kebab-"
        "case name prefixed with ``watcher/<inv_id>-`` so operators can "
        "trace it back to the source failure. Returns the branch name and "
        "the SHA it was forked from."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "branch_name": {
                "type": "string",
                "description": (
                    "kebab-case branch name. Prefix ``watcher/<inv_id>-`` "
                    "is enforced by the orchestrator — calls with other "
                    "prefixes are rejected."
                ),
            },
        },
        "required": ["branch_name"],
    },
}

WATCHER_WRITE_FILE_TOOL = {
    "type": "custom",
    "name": "watcher_write_file",
    "description": (
        "Replace the entire contents of ``path`` on the current branch "
        "with ``content``. The orchestrator validates ``path`` against "
        "the editable-path allowlist and rejects writes outside it (see "
        "``EDITABLE_ALLOWLIST`` in session_runner). Returns the resulting "
        "commit SHA. Use this for incremental edits AND new files. "
        "Multiple writes in one session land as separate commits."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Repo-relative path. Allowlisted: "
                    "``orchestrator/*.py`` (except main.py:_HealthHandler), "
                    "``orchestrator/migrations/*.sql``, ``tests/**/*.py``. "
                    "Blocked: ``agents/*.py``, ``orchestrator/writing_agent.py``, "
                    "``.github/workflows/*``, ``Dockerfile``, ``railway.toml``, "
                    "``bin/deploy.sh``, anything matching ``.env*`` or "
                    "``portco_config.json``."
                ),
            },
            "content": {
                "type": "string",
                "description": "Full file contents. UTF-8. No length cap.",
            },
            "commit_message": {
                "type": "string",
                "description": (
                    "Conventional-commit prefix + concise description. "
                    "Watcher commits use ``fix(watcher): <description>``."
                ),
            },
        },
        "required": ["path", "content", "commit_message"],
    },
}

WATCHER_CREATE_PR_TOOL = {
    "type": "custom",
    "name": "watcher_create_pr",
    "description": (
        "Open a DRAFT pull request from the current branch into main. "
        "The orchestrator runs the conflict check (open PRs in the same "
        "area / recently-merged human PRs in the same area within 24h) "
        "BEFORE creating the PR — if a conflict is detected, returns "
        '``{ok: false, reason: "conflict", details: ...}`` and you '
        "should escalate to diagnose-only mode. Returns the PR URL on "
        "success."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": (
                    "Conventional-commit-style PR title, e.g. "
                    "``fix(session): handle None thread_ts in <call site>``."
                ),
            },
            "body": {
                "type": "string",
                "description": (
                    "PR description. Must include: (1) the original "
                    "error message + inv_id, (2) the root cause as you "
                    "diagnosed it, (3) why this fix is correct, (4) the "
                    "test you added or the reason no test is needed."
                ),
            },
        },
        "required": ["title", "body"],
    },
}

WATCHER_ADD_COMMENT_TOOL = {
    "type": "custom",
    "name": "watcher_add_comment",
    "description": (
        "Post a comment on a pull request (the one you just created, or "
        "the original investigation's tracking issue). Use this to leave "
        "a diagnose-only summary when the fix area is outside the "
        "allowlist, OR to add a self-review checklist on your draft PR "
        "before exiting."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "pr_number": {
                "type": "integer",
                "description": "PR number returned by ``watcher_create_pr``.",
            },
            "body": {
                "type": "string",
                "description": "Markdown comment body.",
            },
        },
        "required": ["pr_number", "body"],
    },
}


WATCHER_SYSTEM_PROMPT = """\
<role>
You are the ❌-Watcher for the GTM Health Agent. A production
investigation has just terminalized as ❌ — you are running because the
orchestrator's ``terminalize_lifecycle`` hook enqueued a
``watcher_pending`` row for that failure. Your job is two-pass:

1. **Diagnose** — read the error context, locate the call site, identify
   the root cause.
2. **Draft fix** — write a minimal code change on a new branch and open
   a DRAFT pull request so a human operator can review + merge.

You are NOT auto-merging. You are NOT auto-deploying. You produce a
draft PR; a human (or a future opt-in flag) does the final merge.
</role>

<inputs>
At session start, the orchestrator injects the failure context:

    - ``inv_id`` — the investigations row that terminalized
    - ``error_message_hash`` — sha1[:16] of the normalized error
    - ``error_category`` — coarse classification if known (may be null
      on first occurrence; you set this with ``watcher_add_comment``
      after diagnosis)
    - ``error_message`` — first line of the raw error string
    - ``repeat_count`` — how many ❌s have collapsed onto this hash
      since first_seen_at (1 = first occurrence; 3 = recurring)
    - ``catch_up`` — true if this was back-filled by the startup sweep
      (deploy gap recovery), false if it was a fresh terminalization

The repo is mounted read-only at ``/workspace/gtm-health-agent``. You
have ``read``, ``grep``, ``glob``, ``bash`` (read-only) tools for
investigation. You have FOUR custom tools for the fix pass:

    - ``watcher_create_branch``
    - ``watcher_write_file``
    - ``watcher_create_pr``
    - ``watcher_add_comment``

You do NOT have ``write`` or ``edit`` filesystem tools — all code
changes go through ``watcher_write_file`` so the orchestrator can
enforce the editable-path allowlist.
</inputs>

<workflow>
Follow these steps. Do not skip diagnosis.

1. **Read the error context.** Use ``grep`` against the repo for the
   error message (stripped of volatile IDs). Locate the throw site. If
   ``error_message`` is opaque (e.g. "unknown_terminalization"), search
   for the inv_id in recent logs via ``bash railway logs --filter inv_id=<N>``.

2. **Trace to the root cause.** Read the surrounding code, the call
   stack you can reconstruct from grep matches, and the relevant
   docstrings. State the root cause in one sentence before writing
   any code. If you cannot identify a root cause in 3 grep+read passes,
   escalate to diagnose-only mode (skip steps 4-6, do step 7 with the
   summary).

3. **Check the editable-path allowlist.** Before writing any fix, confirm
   the file you intend to change is in the allowlist:

       allowed: orchestrator/*.py (except main.py:_HealthHandler block),
                orchestrator/migrations/*.sql,
                tests/**/*.py
       blocked: agents/*.py, orchestrator/writing_agent.py,
                .github/workflows/*, Dockerfile, railway.toml,
                bin/deploy.sh, .env*, portco_config.json

   If the fix lives in a blocked path, escalate to diagnose-only mode.
   ``watcher_write_file`` will reject the write anyway, but checking
   upfront saves a wasted tool call.

4. **Create a branch.** ``watcher_create_branch(branch_name="watcher/<inv_id>-<short-desc>")``.

5. **Write the fix.** ``watcher_write_file(path=..., content=..., commit_message="fix(<scope>): <description>")``.
   Multiple writes are fine — each lands as a separate commit. Keep
   each write focused: one logical change per commit. Always include
   a test in the same PR; if the code area genuinely doesn't have
   tests, add a regression test.

6. **Open the draft PR.** ``watcher_create_pr(title=..., body=...)``.
   The body MUST include:
       - the original error message + inv_id (verbatim)
       - the root cause you identified
       - why your fix is correct
       - what test covers the fix (or why none is needed)

   The orchestrator runs the conflict check before creating the PR.
   If it returns ``{ok: false, reason: "conflict"}``, switch to
   diagnose-only mode and DM the operator with your diagnosis + the
   conflicting PR URL.

7. **Self-review checklist.** ``watcher_add_comment(pr_number=..., body=...)``
   with this checklist body, filled in:

       - [ ] Test exercises the failing path
       - [ ] Edit is in the allowlisted area
       - [ ] No env vars, agent prompts, or workflow files touched
       - [ ] No new dependencies added
       - [ ] Commit message follows ``fix(<scope>): ...`` convention

   This is what your draft PR carries when the operator opens it.
</workflow>

<diagnose-only-mode>
When you cannot write a fix (fix area outside allowlist, root cause
unclear after 3 passes, or conflict with in-flight human work):

    - Do NOT call ``watcher_create_branch`` or ``watcher_write_file``.
    - Write a clear diagnosis comment via ``watcher_add_comment`` on
      the conflicting PR (if there is one) OR on a fresh PR you create
      with title ``diag(<inv_id>): <error category>`` and an empty
      commit (just a docs change to docs/diagnostics/<inv_id>.md).
    - The orchestrator will mark your watcher_pending row as
      ``diagnose_only`` when you exit without ``watcher_create_pr``,
      and DM the admin with your final summary.
</diagnose-only-mode>

<constraints>
- ONE PR per ❌. If your fix needs to span multiple PRs, open the first
  one as your best-guess minimal fix and note the follow-up scope in
  the body.
- NEVER touch: agent prompts (``agents/*.py``), prompt-deploy workflows
  (``.github/workflows/deploy-prompts.yml`` etc.), env vars, build
  config, or anything that could break the orchestrator's own ability
  to terminalize sessions cleanly. Crashing the bot kills your ability
  to fix yourself.
- Default cost budget per session: $5. Investigate pass uses Opus 4.8
  (reasoning); the fix pass is also on Opus 4.8 in v1.
- When in doubt, escalate to diagnose-only mode and let a human ship
  the fix.
</constraints>
"""


def _build_spec() -> dict:
    """Construct the full ``client.beta.agents.create`` kwargs dict.

    Split out so ``--dry-run`` can print it without instantiating an
    Anthropic client.
    """
    return {
        "name": "GTM Health Agent — ❌ Watcher",
        "model": WATCHER_MODEL,
        "description": (
            "Autonomous diagnose-and-draft-PR agent for production ❌s on "
            "the GTM Health Agent. Triggered by lifecycle.terminalize_lifecycle "
            "via the watcher_pending queue. Reads error context, locates the "
            "call site, writes a minimal fix on a branch, opens a draft PR "
            "for human review. Bounded 4-tool surface; cannot touch agent "
            "prompts, workflows, env vars, or deploy infra."
        ),
        "system": WATCHER_SYSTEM_PROMPT,
        "tools": [
            WATCHER_CREATE_BRANCH_TOOL,
            WATCHER_WRITE_FILE_TOOL,
            WATCHER_CREATE_PR_TOOL,
            WATCHER_ADD_COMMENT_TOOL,
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the agent spec without calling the API.",
    )
    parser.add_argument(
        "--rotate",
        action="store_true",
        help=(
            "[reserved] Re-provision the agent. Documented in the design "
            "doc for PAT rotation, but with the custom-tool approach the "
            "PAT lives in the orchestrator and rotation is a Railway env "
            "flip — no re-provisioning needed. Kept as a no-op for "
            "back-compat with operator muscle memory."
        ),
    )
    args = parser.parse_args(argv)

    spec = _build_spec()

    if args.dry_run:
        import json

        print(
            json.dumps(
                {
                    "name": spec["name"],
                    "model": spec["model"],
                    "description": spec["description"],
                    "system_chars": len(spec["system"]),
                    "tools": [t["name"] for t in spec["tools"]],
                },
                indent=2,
            )
        )
        return 0

    if args.rotate:
        print(
            "--rotate is a no-op under the custom-tool design (PAT is "
            "orchestrator-side; flip WATCHER_GH_TOKEN on Railway and "
            "redeploy)."
        )
        return 0

    try:
        import anthropic  # local import so --dry-run doesn't need the SDK
    except ImportError:
        print(
            "ERROR: anthropic SDK not installed (`pip install anthropic`).",
            file=sys.stderr,
        )
        return 2

    client = anthropic.Anthropic()

    print(f"Creating ❌-Watcher (model={WATCHER_MODEL})...")
    agent = client.beta.agents.create(**spec)

    print()
    print(f"WATCHER_AGENT_ID={agent.id}")
    print()
    print("Next steps:")
    print(f"  1. Add WATCHER_AGENT_ID={agent.id} to .env")
    print(f"  2. Add WATCHER_AGENT_ID={agent.id} to Railway env vars")
    print("  3. Add WATCHER_GH_TOKEN=<PAT> to Railway env vars (repo scope only)")
    print("  4. Leave WATCHER_ENABLED=false on Day 1 — flip true on Day 2")
    print('  5. railway redeploy --service "GTM Health Agent" -y')
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
