---
title: Managed Agents conformance — runbook
plan_ref: docs/plans/44-managed-agents-docs-conformance.md
audience: operator
---

# Managed Agents conformance runbook

Operator-facing instructions for the two conformance audit scripts
shipped by Plan #44. Both are read-only. Both exit 0 on clean state
and 1 on findings. Both write a "Next steps" block to stdout that
points back to this file.

The runbook is self-sufficient — you should never need to open the
plan to act on a finding. The plan is the design rationale; this file
is the action checklist.

## Script 1 — `bin/audit-mcp-toolsets.py`

**Purpose**: flag any agent whose `tools[]` still carries an orphan
`mcp_toolset` entry. Iteration 3 removed the Salesforce MCP toolset
from every sub-agent (every SF read now routes through
`dump_sf_query`). The auto-approve path in `session_runner.py:1282-1287`
still tolerates `mcp_toolset` entries, so this audit catches drift
between what we authored in `agents/setup_agents.py` and what's on
the live Anthropic agent.

**When to run**:
- After every sub-agent provisioning round
  (`python agents/setup_agents.py` or a `provision_*.py` script).
- After merging any PR that touches `agents/setup_agents.py` (the
  source of truth for which agents carry MCP toolsets).
- Quarterly, as a manual sanity check.

**How to run**:

```bash
cd /path/to/gtm-health-agent
python bin/audit-mcp-toolsets.py
# Add --verbose to print every agent's tools[] summary, not just orphans.
```

**Failure modes and remediation**:

1. **Exit 1, finding "agent X: orphan mcp_toolset(s): mcp_toolset:salesforce"**
   - Confirm the orphan is unintended. Check
     `agents/setup_agents.py:SUB_AGENT_DATA_TOOLS` and
     `SUB_AGENT_REASONING_TOOLS` — neither should contain
     `SF_MCP_TOOLSET` after Iter3.
   - If unintended: run
     `python agents/update_subagent_tools.py`. The reconciler passes
     `mcp_servers=[]` whenever the target tools[] contains no
     `mcp_toolset`, which clears the orphan in one update call.
   - If intended (rare): document the exception in
     `agents/setup_agents.py` and in the plan that owns it, then
     re-run the audit to confirm only the documented agent is
     flagged.

2. **Exit 1, finding "agent X (agent_...): retrieve failed: <error>"**
   - The agent ID may be stale (rotated without updating
     `agents/update_prompts.py:AGENTS`). Cross-check against the
     Anthropic Console. If rotated, update the env var and the
     hardcoded fallback in AGENTS, then re-run.
   - 503/timeout: re-run after a few minutes. Anthropic Managed
     Agents API has occasional read latency spikes.

3. **No findings** (exit 0): no action required. Re-run after the
   next provisioning round.

## Script 2 — `bin/audit-toolset-drift.py`

**Purpose**: snapshot each agent's full `tools[]` payload weekly and
diff against the most recent prior snapshot. Catches drift WITHIN the
`agent_toolset_20260401` dated contract — Anthropic may extend tool
descriptions / argument shapes without changing the toolset ID.

**When to run**:
- Automatically: every Monday 06:00 PT via
  `.github/workflows/toolset-drift-canary.yml`.
- On demand: when you suspect a Managed Agents release changed
  behavior (e.g. tool calls returning slightly different shapes than
  expected).

**How to run**:

```bash
cd /path/to/gtm-health-agent
python bin/audit-toolset-drift.py
# Add --no-write for a diff-only run (does not write a new snapshot).
# Add --verbose to print the full new-vs-old payload per drifted agent.
```

**Failure modes and remediation**:

1. **Exit 1, drift detected** (one or more agents flagged):
   - Inspect the diff in the workflow logs (or re-run locally with
     `--verbose`).
   - Cross-reference against the most recent
     https://docs.anthropic.com release notes for Managed Agents.
   - **Most floating changes within `agent_toolset_20260401` are
     safe to inherit**: extended descriptions, new optional arguments
     with sensible defaults, additional response fields. No action
     needed — the snapshot has been committed back to main, and the
     next weekly run will use the new baseline.
   - **If the change is unsafe** (e.g. a tool argument renamed, an
     existing field removed): update `agents/setup_agents.py` to
     match the new shape, then run
     `python agents/update_subagent_tools.py` to re-publish the
     pre-provisioned agents. Run `python agents/update_prompts.py` if
     prompts also need adjusting.
   - **If the toolset ID itself changes** (e.g.
     `agent_toolset_2026XXXX`): update `setup_agents.py` to point at
     the new ID after a vetting cycle. The diff will show the
     toolset entry replaced.

2. **Exit 0 on first run** ("No prior snapshot found"): expected.
   The script writes the initial snapshot; the next weekly run
   establishes drift detection. No action needed.

3. **Snapshot commit blocked by branch protection**: the workflow's
   `Commit new snapshot` step prints "push blocked — snapshot left in
   working tree". The drift is still reported (workflow fails), but
   the snapshot doesn't land. Action: re-run with a PAT-authenticated
   workflow or manually run the script locally and open a PR.

## Both scripts — log location

- Workflow runs: GitHub Actions UI, repo Actions tab.
- Local runs: stdout. The "Next steps" block at the bottom is the
  same content as this runbook.

## Related plans

- Full design rationale: `docs/plans/44-managed-agents-docs-conformance.md`.
- Rollback mechanism: `docs/plans/41-agent-versioning-and-rollback.md`.
- Multi-agent multiagent re-pin: `agents/update_subagent_tools.py:republish_coordinator_multiagent`.
## Limited networking shadow rollout

The live env (`ENVIRONMENT_ID`) was created with
`networking.type = "unrestricted"`. The Anthropic Managed-Agents docs
explicitly recommend production environments flip to `limited` with a
written `allowed_hosts` allowlist. Plan #44 decision row #8 picks the
**two-env shadow rollout** path — both envs stay active during a 48h bake,
then `ENVIRONMENT_ID` flips when the bake is clean.

### Why a shadow env (and not an in-place flip)

A blind in-place flip is the single fastest way to take the system down:
any host the operator forgot will silently block sessions until somebody
notices a Slack-message backlog. Two envs let the operator route a small
percentage of traffic at the new constraint, watch for blocked hosts,
expand the allowlist, and **only then** cut over.

### Step 1 — Trace observed hosts (negative test)

Before provisioning anything, surface every hostname the system has
actually reached in the last week. The trace script reads `session_costs`
to enumerate session ids, then walks every event via the Anthropic SDK
and extracts URL-shaped hostnames from tool-call inputs, tool-result
content, and `session.error` messages.

```sh
python bin/trace-outbound-hosts.py --days 7
```

The report writes to `outbound-hosts-trace.txt`. Three buckets:

- **`[OK]`** — observed and on the allowlist. No action.
- **`[WARN]`** — observed but NOT on the allowlist. These would break
  under limited networking — add them to `ALLOWED_HOSTS` in
  `bin/provision-limited-env.py` before the next step.
- **`[DROP?]`** — on the allowlist but not observed. Probably safe to
  remove, but keep them for the first bake cycle and trim in a follow-up.

Exit code is 0 if every observed host is allowlisted, 1 if any host
would block, 2 if the script could not run (no `DATABASE_URL`, no
`ANTHROPIC_API_KEY`, etc.).

### Step 2 — Update the allowlist if needed

`bin/provision-limited-env.py` carries the static allowlist (top of file,
`ALLOWED_HOSTS`). Each entry is HTTPS-prefixed per docs. If the trace
surfaced anything in the `[WARN]` bucket, add those hosts here. Keep the
list small and explicit — sandbox attack-surface reduction is the
point.

The trace script's `EXPECTED_HOSTS` (host-only form, no scheme) MUST be
updated in parallel — the two lists are deliberately duplicated rather
than imported so each script can run standalone.

### Step 3 — Provision the shadow env

```sh
# Dry-run first (default behavior — no API call):
python bin/provision-limited-env.py

# Once the printed payload looks right:
python bin/provision-limited-env.py --apply
```

The script prints an `ENVIRONMENT_ID_LIMITED=env_...` line at the end.
Copy that value.

### Step 4 — Route a slice of traffic to the shadow env

In Railway → Service → Variables, set both:

```
ENVIRONMENT_ID_LIMITED=env_<from step 3>
LIMITED_NETWORKING_SHADOW_PCT=10
```

This routes 10% of new sessions through the limited env while the other
90% continue on the existing unrestricted env. Slack channel posts the
shadow-env tag on each routed session so the operator can map a session
back to its env at incident time.

> NOTE: The router (Bundle B) reads `LIMITED_NETWORKING_SHADOW_PCT` and
> `ENVIRONMENT_ID_LIMITED` at session-create time. Until Bundle B lands
> the router code, this variable is a no-op — the unrestricted env stays
> at 100%. The provisioning step above is still valuable on its own
> because the shadow env exists and can be hand-attached to a single
> session for verification.

### Step 5 — Bake for 48 hours

Watch admin DMs (`SLACK_ADMIN_USER_IDS`) for blocked-host errors. Any
session that hits a missing host will surface a `session.error` event;
self-heal already classifies those.

After the bake window, re-run the trace on the last two days
(`bin/trace-outbound-hosts.py --days 2`) to confirm the allowlist is
clean. If the report is exit-code 0 with no `[WARN]` entries, proceed
to step 6. If anything new shows up, add it to both lists (step 2),
re-provision (step 3), update Railway (step 4), and extend the bake.

### Step 6 — Flip `ENVIRONMENT_ID` (full cut-over)

In Railway:

```
ENVIRONMENT_ID=<value of ENVIRONMENT_ID_LIMITED>
LIMITED_NETWORKING_SHADOW_PCT=100   # optional; matches the new reality
```

The old unrestricted env stays in the Anthropic console for one Railway
deploy cycle as a hot rollback. Delete it on the next deploy unless
something points back to it.

### Rollback

If anything goes sideways during step 5 or step 6: set
`LIMITED_NETWORKING_SHADOW_PCT=0` to immediately route 100% back to
the unrestricted env, then investigate. The old `ENVIRONMENT_ID` value
is the rollback target.
## Prompt preview pre-commit hook

**Owner: Bundle F (Task #7, decision row #23)**

The deploy workflow `.github/workflows/deploy-prompts.yml` auto-runs
`agents/update_prompts.py` on every merge that touches a prompt source.
Plan #42 added a `prompt-author-verified` label, but its own audit
called the label "tripwire for forgetfulness." This task makes the
check load-bearing by mirroring the same smoke call in two places:

1. A **local opt-in pre-commit hook**, for fast feedback before push.
2. A **required CI workflow**, which is the actual gate and cannot be
   bypassed.

### What the smoke check does

For every prompt string that changed between the BEFORE ref and the
working tree, the preview engine sends one Messages-API call to
Sonnet 4.6 with the changed system prompt and a fixed probe:

> Hello, please confirm you're operational and describe your role in
> one sentence.

Any non-empty coherent reply counts as a pass. The check catches the
two failure modes that ship from a typo or paste error: a model that
refuses outright, and a model that emits empty text. It does not — and
intentionally cannot — catch semantic regressions (those are caught by
the rubric review).

### Install the local hook (one-time)

```bash
python bin/install-hooks.py
```

On macOS / Linux this symlinks `.git/hooks/pre-commit` to the source
at `scripts/hooks/pre-commit`, so edits to the hook source propagate
without reinstalling. On Windows (where a standard user cannot create
symlinks) the script falls back to a file copy and asks you to re-run
after editing the source.

To remove the hook later:

```bash
python bin/install-hooks.py --uninstall
```

To preview the action without changing anything:

```bash
python bin/install-hooks.py --dry-run
```

### How the hook behaves

Once installed, `git commit` runs the hook automatically. The hook:

- Returns immediately (exit 0) if no staged file matches
  `agents/setup_agents.py` or `agents/update_prompts.py`.
- Otherwise invokes `python -m agents.preview_prompt --diff` to extract
  changed prompts and smoke-check each one.
- Exits 1 with `Prompt preview failed — fix the prompt or commit with
  --no-verify (CI will still gate)` if any smoke call fails.

If `ANTHROPIC_API_KEY` is not set in the local shell, the preview
script prints `ANTHROPIC_API_KEY missing — preview skipped (CI will
gate)` and exits 0. The hook is convenience only — CI runs the
authoritative check with the org-wide key.

### The CI gate

`.github/workflows/ci-prompt-preview.yml` runs on every PR that touches
`agents/setup_agents.py` or `agents/update_prompts.py`. It uses the
`ANTHROPIC_API_KEY` secret to run the same `preview_prompt.py` engine
with `--ref origin/<base_branch>`. A failed smoke check blocks the
merge.

**One-time branch protection setup:** after this workflow lands and
runs at least once (so the status check exists), mark
`CI — Prompt Preview / preview` as a required status check on `main`
in the GitHub branch-protection settings. Until that flip happens the
workflow runs but does not gate.

### Bypassing locally (CI still gates)

If you need to commit through a transient smoke failure (rate limit,
upstream outage), use:

```bash
git commit --no-verify
```

CI will still run the same check on push. The local bypass exists for
hot-fix ergonomics, not as a permanent escape hatch.

### Manual run

The preview engine is invocable directly for ad-hoc checks:

```bash
# Smoke-check whatever changed against HEAD (the default).
python -m agents.preview_prompt --diff

# Smoke-check against a different base ref (CI uses this form).
python -m agents.preview_prompt --diff --ref origin/main

# Smoke-check every prompt in a single file, regardless of diff state.
python -m agents.preview_prompt --file agents/update_prompts.py

# Strict mode: treat any [SKIP] notice (dynamic f-string, .format(),
# %-formatting, string concat) as a hard failure. Use in CI to keep
# prompt strings statically inspectable.
python -m agents.preview_prompt --diff --strict
```

Each invocation honours the same `ANTHROPIC_API_KEY` semantics: missing
key prints the skip notice and exits 0; failed smoke check prints
`FAILED for <name>: <reason>` per prompt and exits 1.

Dynamic strings (f-strings with interpolation, `.format()` calls,
`%` formatting, string concatenation) are not statically smoke-checkable
— they emit `[SKIP] <name>: dynamic string at line <N> — cannot
smoke-check` to stderr but, by default, still exit 0. Pass `--strict`
to convert any [SKIP] into exit 1 (Plan #44 review #2 fix).

### Worktree note

The installer resolves `.git/hooks/` via `git rev-parse --git-common-dir`
so it works in `git worktree` checkouts where `.git` is a *file*
pointing into the main repo's `.git/worktrees/<name>/` directory. The
hook lands on the main repo's `hooks/` dir and is picked up by every
worktree.

### Files

- `agents/preview_prompt.py` — extract + smoke engine.
- `scripts/hooks/pre-commit` — installable hook script.
- `bin/install-hooks.py` — cross-platform installer.
- `.github/workflows/ci-prompt-preview.yml` — the CI gate.
- `agents/preview_prompt_test.py` and `bin/install_hooks_test.py` —
  unit coverage (AST extraction, mocked SDK, install/uninstall).

## Vault SF MCP rollout (Plan #44 Task #17 — Bundle D)

Moves Salesforce MCP auth from Railway env vars into the Acme vault,
gated behind `SF_MCP_VIA_VAULT`. The flag controls the SHAPE of the
published agent configuration — flipping it is a DEPLOY-TIME operation,
not a runtime env-var flip. Tools and `mcp_servers` are server-side
agent state.

### Rollout steps

**Step 1 — Create the vault credential.**

```bash
# Inspect first (no network writes)
python bin/add-sf-vault-credential.py

# Apply once the redacted payload looks correct
python bin/add-sf-vault-credential.py --apply
```

The script prints the new `credential_id` plus the `mcp_oauth_validate`
diagnostic verbatim. If the diagnostic returns anything other than
`valid`, do not proceed — fix the OAuth payload (consumer key, refresh
token, scope) in `.env` / Railway and retry.

**Step 2 — Confirm `mcp_oauth_validate` returned `valid`.**

Eyeball the validation block printed in step 1. The Anthropic SDK
returns a `BetaManagedAgentsCredentialValidation` object whose status
field must read `valid`. Anything else is a credential health failure;
the new credential is in the vault but should be archived and re-issued
once the underlying credential is fixed.

**Step 3 — Set `SF_MCP_VIA_VAULT=true` in Railway (build var, not
runtime).**

Railway → service → Variables → Build → add `SF_MCP_VIA_VAULT=true`.
Run-time variables won't help because the flag is read by
`setup_agents.py` / `update_subagent_tools.py` at deploy time, not at
session-create time.

**Step 4 — Re-deploy.**

Trigger a fresh build so the `update_subagent_tools.py` step in
`.github/workflows/deploy-prompts.yml` sees the new flag value.

**Step 5 — Publish the new agent shapes.**

```bash
python agents/update_subagent_tools.py
```

The script picks up `SF_MCP_VIA_VAULT=true` via the
`SUB_AGENT_DATA_TOOLS` module-level computation in `setup_agents.py`,
adds the SF mcp_toolset to each Monitor + Statistician roster, and sets
`mcp_servers=[SF_MCP_SERVER]` in lockstep so the API accepts the
update. Every Monitor + Statistician version bumps by one.

**Step 6 — Re-publish the Coordinator multiagent roster.**

```bash
python agents/update_coordinator_roster.py
```

(or whatever entrypoint Bundle A standardizes on — Plan #44 decision
row #11.) The Coordinator pins each sub-agent at parent-update time, so
without this step every session continues dispatching to the old
sub-agent versions. The script bumps the Coordinator's own version by
one.

**Step 7 — Smoke-test.**

Run one dream cycle and one ad-hoc session in Slack. Confirm
`session_costs` for both reflects normal token totals (the
`SESSION_WATCH_THRESHOLD` 500K backstop catches the row-leak regression
from the 2026-05-11 1.07M-context incident). Confirm
`agent.mcp_tool_use` events appear in the session event stream for SF
reads — that proves the vault-backed path is actually engaging, not
silently falling through.

### Rollback

The rollback is the same shape as the rollout, in reverse:

1. Set `SF_MCP_VIA_VAULT=false` in Railway (build var).
2. Re-deploy.
3. `python agents/update_subagent_tools.py` — drops the SF mcp_toolset
   from each sub-agent, passes `mcp_servers=[]` to clear the SF server
   declaration. The `dump_sf_query` tool remains and serves every read
   via the Railway-resident OAuth Client Credentials flow (the proven
   path that survived Iter3 and the 1M-context incident).
4. `python agents/update_coordinator_roster.py` — re-snapshots the
   Coordinator's sub-agent pins onto the new versions.

The vault credential created in step 1 of the rollout does NOT need to
be archived. The flag flip is the only path control; the credential
sits idle until the flag flips back to `true`. Archive it only if
explicitly rotating credentials.
