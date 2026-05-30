# Runbook — Pre-deploy smoke probe

Plan #42 PR2; Plan #44 Task #20 (multiagent extension). One-page operator
reference. Bookmark in your Slack admin DM.

## What it checks

The smoke probe runs **before** the orchestrator binds the `/ready` health
route. Until it passes, Railway holds the previous image. Up to four checks,
90-second total budget at `full` level (60s at `quick`):

| Check | Module | Why it can fail | Level |
|---|---|---|---|
| A — `build_commit` | `os.environ["BUILD_COMMIT"]` vs `agents/active_versions.json` | Dockerfile `ARG BUILD_COMMIT` not threaded through Railway; stale binary serving while the deploy claims green. | quick + full |
| B — `dump_sf_query` | `orchestrator/sf_dump_tool.dump_sf_query` with `SELECT Id FROM Account LIMIT 1` | MCP vault unreachable, SF credentials expired, `SESSION_OUTPUT_DIR` not writable. | quick + full |
| C — `quick_answer` | Fresh single-turn session against the Quick Answer agent | Anthropic API down, `QUICK_AGENT_ID` rotated and not re-pinned, env mismatch. | quick + full |
| D — `coordinator_multiagent` | Fresh single-turn session against the Coordinator; asks it to confirm specialist access | Coordinator multiagent pin gone stale, sub-agent prompt regression, `COORDINATOR_ID` rotated and not re-pinned. | **full only** |

Anthropic 429 (rate limit) or 5xx during Check C **or Check D** flips the
run to **INCONCLUSIVE-PASS**: the probe still returns `passed=True` and the
deploy proceeds because a real Anthropic outage degrades both the previous
and the new image — blocking the deploy would prevent the fix from landing
during the outage. The admin DM is loud about the degraded state. (Plan
#42 decision D7.)

## Level (Plan #44 Task #20)

`SMOKE_PROBE_LEVEL` controls how thoroughly the probe exercises the live
dependencies. Allowed values:

- `off` — probe skipped entirely; `/ready` returns 200 immediately. Use
  during incident debugging when the probe itself is suspect. Still writes
  one row to `smoke_probe_runs` (`reason=probe_disabled_via_level`) so the
  flip is auditable. Distinct from `SMOKE_PROBE_ENABLED=false`, which
  short-circuits in `main.py` before this module loads.
- `quick` (default) — Checks A+B+C, the original Plan #42 PR2 probe.
  ~$0.01/deploy.
- `full` — Checks A+B+C+D. Use when you suspect Coordinator routing
  regression or multiagent pin staleness (a sub-agent prompt deploy that
  forgot the `update_prompts.py` Coordinator re-snapshot). ~$0.05/deploy.

### Flipping the level

From Slack (preferred — works from your phone at 2am):

    /flag SMOKE_PROBE_LEVEL full
    /flag SMOKE_PROBE_LEVEL quick      # back to default
    /flag SMOKE_PROBE_LEVEL off        # disable for incident

Bundle E (Plan #44 Task #24) stores this in Postgres so it survives the
next redeploy — exactly the case where the operator needs the flip to
stick. From Railway: set `SMOKE_PROBE_LEVEL=full` as a service variable;
the Postgres override still wins if both are set.

## How to read the admin DM

Four templates. Every state names the failing check, the customer impact,
and the next concrete action.

### `[SMOKE PROBE OK]`

Deploy succeeded; new image promoted. The streak counter (7-day pass rate)
prints alongside so you spot a regression building over time.

### `[SMOKE PROBE FAILED]`

At least one check failed. Customer impact: NONE — Railway held the
previous image, so Slack traffic still works. Action: rerun the failing
check locally to reproduce, then either fix and redeploy or flip the kill
switch (below).

### `[SMOKE PROBE INCONCLUSIVE — DEPLOY ALLOWED]`

Anthropic returned 429 or 5xx during Check C. Deploy proceeded because the
probe could not distinguish an outage from a real failure of the new build.
Watch `https://status.anthropic.com/`; if the outage clears and the new
build still misbehaves, rollback via:
`python bin/rollback-deploy.py --artifact-run <gh_run_id>`.

### `[WARN] SMOKE_PROBE_ENABLED=false`

Operator disabled the gate. The deploy was NOT validated. Re-enable as
soon as the incident clears (Railway → Variables → prod →
`SMOKE_PROBE_ENABLED=true`).

## Diagnosing a failing probe

Each check logs its own line during the probe run. Search Railway logs for
`smoke_probe:`.

### Check A — build_commit mismatch

`BUILD_COMMIT env var unset` → Railway build args lost. Verify
`Variables → Build → BUILD_COMMIT` is set on the service.

`build_commit mismatch: env=abc12345 file=def67890` → the pin file
recorded a different SHA than the container booted with. Confirm
`agents/active_versions.json` was updated by the prompt-deploy workflow
after the merge to main.

### Check B — dump_sf_query failed

`sf_query exception: SalesforceAuthenticationFailed` → SF Connected App
credentials wrong / refresh token expired. Re-mint via the OAuth flow,
update Railway env, redeploy.

`sf_query returned error: sf_auth_failed: ...` → same class of failure;
the structured error in `sf_dump_tool` caught it before raising.

`Could not create output directory ...` → `SESSION_OUTPUT_DIR` is not
writable. Check the Railway volume mount.

### Check C — Quick Answer agent failed

`QUICK_AGENT_ID unset` → the env var wasn't propagated through Railway.
Both `QUICK_AGENT_ID` and the legacy `QUICK_ANSWER_ID` are accepted.

`Quick Answer timeout after 25s` → either the Quick Answer agent prompt
is stuck or the session API is slow. Compare against
`https://status.anthropic.com/`.

`Quick Answer response missing sentinel (got: '...')` → the agent
returned a non-empty response that did not contain `smoke-probe-ok`. This
usually means the agent's system prompt was overhauled and now refuses to
echo verbatim — fix the prompt or update the sentinel.

### Check D — Coordinator multiagent failed (`full` level only)

`COORDINATOR_ID unset` → the Coordinator agent ID was not propagated.
Re-run `setup_agents.py` or set `COORDINATOR_ID` in Railway variables.

`Coordinator timeout after 30s` → the Coordinator boot path is slow or
its multiagent block is large enough that planning takes too long. Compare
against `https://status.anthropic.com/`. If the API is healthy, the
Coordinator prompt may have regressed into a long planning rumination
on a trivial input.

`Coordinator response missing multiagent sentinel (got: '...')` → the
Coordinator returned prose but did not echo `multiagent-ok`. The likely
cause is a Coordinator prompt update that broke direct-answer mode; check
the most recent `agents/setup_agents.py` change and the
`agents/active_versions.json` pin for the Coordinator. The CLAUDE.md memory
`feedback_multiagent_pinning` covers the snapshotted-not-live nuance: a
sub-agent prompt deploy without a Coordinator re-update leaves the
Coordinator delegating to stale versions, and that can also surface as a
weird Check D failure.

## How to disable for an incident

Flip the kill switch in Railway:

1. Railway → service → Variables → `SMOKE_PROBE_ENABLED=false`.
2. Redeploy (Railway auto-redeploys on env-var changes).
3. The next boot skips the probe, `/ready` returns 200 immediately, and an
   admin DM warns the deploy was not validated.
4. Re-enable as soon as the incident clears.

No code change required.

## How to run locally

```bash
source .env.smoke          # copy from .env.smoke.example
python -m orchestrator.smoke_probe --local --check all
```

Flags:

- `--local` — missing `BUILD_COMMIT` is a WARN, not a FAIL.
- `--check {build,sf,agent,coord,all}` — run a subset for fast iteration.
  `coord` runs Check D in isolation, `all` honors `--level`.
- `--level {off,quick,full}` — override `SMOKE_PROBE_LEVEL` for this run
  without writing to the DB. Defaults to the resolved env/DB value.
- `--no-persist` — skip the `smoke_probe_runs` INSERT.
- `--no-dm` — skip the admin DM (recommended for local debugging).

Exit code: `0` on PASS (including INCONCLUSIVE-PASS), `1` on FAIL.

## Rollback (deploy already promoted)

If the probe passed but the new build is broken in production:

```bash
python bin/rollback-deploy.py --artifact-run <gh_run_id>
```

The `gh_run_id` is in the success admin DM. The rollback restores the
prior agent versions to Anthropic and the prior container to Railway.
