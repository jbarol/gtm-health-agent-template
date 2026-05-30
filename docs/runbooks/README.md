---
title: Runbook index
plan_ref: docs/plans/42-safe-deploy-pipeline.md
audience: operator
---

# Runbook index

Symptom → runbook decision tree. Open this file first; every entry
links to a focused runbook with the exact commands, expected output,
and a "Done when" checklist.

## Quick lookup

| Symptom | Runbook |
|---|---|
| "The Coordinator (or any agent) is on a bad prompt — roll it back NOW." | [Rollback an agent prompt](#rollback-an-agent-prompt) |
| "An agent's `tools[]` has drifted from `setup_agents.py`." | [Managed Agents conformance](managed-agents-conformance.md) |
| "Sub-agents are running filesystem diagnostics instead of calling MCP." | [Managed Agents conformance — MCP probe gotcha](managed-agents-conformance.md) |
| "Production smoke probe failed — what now?" | [`runbook-smoke-probe.md`](../runbook-smoke-probe.md) |
| "Prompt deploy went out without the verification label." | [`runbook-prompt-rollback.md`](../runbook-prompt-rollback.md) |
| "Slack scopes need to change." | [Slack scopes changelog](../slack/scopes-changelog.md) |

## Index

| Runbook | Subject | Owner |
|---|---|---|
| [`managed-agents-conformance.md`](managed-agents-conformance.md) | Orphan MCP toolsets, toolset drift canary, MCP probe-by-call discipline. | Plan #44 |

Deploy-safety runbooks:

| Runbook | Subject | Status |
|---|---|---|
| [`runbook-smoke-probe.md`](../runbook-smoke-probe.md) | What the pre-deploy `/ready` smoke probe checks, how to read the admin DM, how to disable in an incident, local repro. | available |
| [`runbook-prompt-rollback.md`](../runbook-prompt-rollback.md) | When and how to use `bin/rollback-deploy.py --artifact-run <gh_run_id>`. | available |

## Rollback an agent prompt

For an immediate single-agent rollback (e.g. Coordinator):

```bash
# Look up the prior version on Anthropic, then:
python bin/rollback-agent.py coordinator --to-version <N>
```

The script writes the old prompt forward as a new active version,
updates `agents/active_versions.json`, opens a pin-file PR, and DMs
admins. See the docstring in `bin/rollback-agent.py` for the full SDK
contract.

For a whole-deploy rollback (multiple agents touched at once), use the
`bin/rollback-deploy.py` wrapper.

## When in doubt

- Read the matching runbook end-to-end before running the commands.
- Every runbook has a "Done when" section — verify against it.
- If the symptom does not match anything here, file an issue
  describing what you tried. The runbook index grows out of incidents.

## Conventions

Each runbook follows the same shape (codified per Plan #42 DX review
finding #27):

1. **Decision tree**: when to use this runbook.
2. **Commands**: the exact CLI invocations.
3. **Expected output**: what success and the common failure modes
   look like.
4. **"Done when"**: a checklist that defines the green-light state.
5. **Links**: plan, related files, and the runbook index.
