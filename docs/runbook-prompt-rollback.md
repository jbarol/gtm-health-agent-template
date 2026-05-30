# Runbook: Prompt Rollback

Plan #42 PR3 — operator procedure for reverting a Managed-Agent prompt
deploy that broke production behavior. The common case is the
2026-05-11 $47 context blowup: a Coordinator prompt change landed, the
next session burned 1.12 M tokens reproducing the regression. Recovery
should take under a minute.

## When to use

Open this runbook the moment any of these are true after a prompt deploy:

- A Coordinator or sub-agent session goes off the rails (unexpected
  loops, MCP-diagnostic spiral, wrong-format output, sudden cost spike).
- The deploy success DM lists agents you don't recognize having changed,
  AND a regression is observed.
- The pre-deploy "watch this" intuition fires — you'd rather be back at
  the prior version while you debug.

Do NOT use this runbook for:

- Salesforce MCP issues — those are environment-side, not prompt-side.
- Slack-channel routing or surface-pusher failures — those are
  orchestrator code, not the agent prompts.
- Production downtime — that's `docs/runbook-business-hours.md` or
  `docs/runbook-smoke-probe.md`.

## Done when

You're done with this runbook when ALL of these are true:

1. The Slack admin DM confirms a clean rollback (`[ROLLBACK-DEPLOY
   APPLIED]`) with per-agent OK lines for every diffed agent.
2. The Anthropic console shows the rolled-back agents at the prior
   version content.
3. `agents/active_versions.json` on `main` reflects the post-rollback
   state (a follow-up auto-commit fires from `bin/rollback-agent.py`).
4. A Slack-side spot check in `#acme-gtm` confirms the regression
   is gone.

## Decision tree

```
Symptom: prompt deploy regressed something
   │
   ├── Do you know which deploy was bad?
   │     │
   │     ├── YES → use that workflow run ID directly. Skip to "Step 2".
   │     │
   │     └── NO  → run `gh run list --workflow=deploy-prompts.yml \
   │                       --limit 5` and pick the most recent
   │                       successful run BEFORE the regression appeared.
   │
   └── Is the broken agent in agents/active_versions.json?
         │
         ├── YES → proceed.
         │
         └── NO  → check `agents/update_prompts.py:AGENTS`. If the agent
                  has no ID (env var unset) the prompt deploy could not
                  have touched it. Look elsewhere.
```

## Step 1 — find the right workflow run ID

The deploy-success DM you got when the bad deploy landed includes the
GitHub run ID and a pre-filled rollback command. That's the fast path —
scroll up in DMs, copy-paste.

If you no longer have the DM, list recent runs:

```bash
gh run list --workflow=deploy-prompts.yml --limit 5
```

Pick the run ID of the deploy whose changes you want to revert. The run
IDs are also visible at
`https://github.com/your-org/gtm-health-agent/actions/workflows/deploy-prompts.yml`.

## Step 2 — dry-run

Always dry-run first. It prints exactly what would change without
mutating Anthropic:

```bash
python bin/rollback-deploy.py --artifact-run <gh_run_id>
```

Expected output (truncated):

```
[DOWNLOAD] $ gh run download <gh_run_id> -n pre_deploy_versions -D /tmp/...
[ROLLBACK-DEPLOY DRY-RUN] artifact-run <gh_run_id>

  Agents to roll back: 2
    - coordinator                   v37 -> v36
    - quick_answer                  v14 -> v13

  Next steps:
    Re-run with --apply to perform the rollback:
      python bin/rollback-deploy.py --artifact-run <gh_run_id> --apply

  Runbook: docs/runbook-prompt-rollback.md
```

Sanity check: do the "v_current -> v_previous" arrows match the agents
you expected this deploy to touch? If a surprising agent is in the
list, stop and read the PR diff before applying.

If the dry-run says `no version changes detected`, the deploy you're
referencing didn't actually change any prompt versions. Pick a different
run ID.

## Step 3 — apply

```bash
python bin/rollback-deploy.py --artifact-run <gh_run_id> --apply
```

For each agent in the diff, the wrapper invokes
`python bin/rollback-agent.py <name> --to-version <prior>`. That script
already handles the SDK reality correctly: writes the prior version's
body forward as a new active version, updates `active_versions.json`,
DMs admins, and opens a one-line PR with the pin file change.

Expected runtime: roughly 30 seconds per agent. A two-agent rollback
should finish in under a minute. The output ends with a `Total
recovery time: Xs` line.

## Step 4 — verify

Check three places in this order:

1. **Slack DM** — Look for `[ROLLBACK-DEPLOY APPLIED]` with `[OK]`
   for every agent.
2. **Anthropic console** — Open each rolled-back agent and confirm
   the latest version's prompt body matches the prior version's body.
   The version number is new (rollback creates a forward version), but
   the content matches the target.
3. **`agents/active_versions.json` on main** — `bin/rollback-agent.py`
   opens a follow-up PR per agent. Merge those promptly so CI's
   `verify-agent-versions` job stops flagging drift.

## Common gotchas

- **`gh` CLI not installed.** The wrapper shells out to `gh run
  download`. Install via `brew install gh` and authenticate with `gh
  auth login`.
- **Wrong run ID.** If `gh run download` 404s, the run ID is wrong or
  the run is older than the 90-day artifact retention. Pick the next
  most recent run.
- **An agent was added/removed by the bad deploy.** The wrapper prints
  a `[WARN]` line listing the agent and skips it. You need to do that
  one by hand via `bin/rollback-agent.py` against a known-good prior
  version.
- **Partial failure (exit code 1).** One or more `rollback-agent.py`
  invocations failed mid-batch. Re-run with the same `--artifact-run`;
  the script will redo only the agents still showing a version delta.

## Slack DM examples

### Success

```
[ROLLBACK-DEPLOY APPLIED] artifact-run 7891234567

  Agents to roll back: 2
    - coordinator                   v37 -> v36
    - quick_answer                  v14 -> v13

  Per-agent results:
    [OK] coordinator                rolled back to v36  (28.4s)
    [OK] quick_answer               rolled back to v13  (26.9s)

  Total recovery time: 55.3s
  Per-agent average: ~27.6s (expect ~30s per agent on a healthy day)

  Next steps:
    1. Verify in Anthropic console that each rolled-back agent
       now serves the prior prompt.
    2. Confirm Slack behavior in #acme-gtm.
    3. If the rollback was triggered by a prompt regression,
       open a follow-up PR with the fix.

  Runbook: docs/runbook-prompt-rollback.md
```

### Partial failure

If one agent's rollback fails, the corresponding line in the DM reads
`[FAIL]` instead of `[OK]` and includes the underlying error. The
overall exit code is 1 so a calling script can detect the partial
state, and the wrapper still DMs the summary so you see exactly which
agents are inconsistent.

## 60-second walkthrough

The first time you run this end-to-end, budget 60 seconds:

- 10 s — open the deploy-success DM, copy the pre-filled command.
- 5 s — paste, edit `--apply`.
- 30 s — wait for the per-agent updates.
- 15 s — eyeball the Anthropic console + Slack to confirm.

If it takes longer than two minutes, something is wrong with the script
or the underlying SDK call. Capture the stdout and stop.
