# bin/ — Operations scripts

Module-specific context. Each script here exits 0 on clean state, 1 on findings or failure, and writes a "Next steps" block to stdout.

## Manual deploy

`bin/deploy.sh` from a fresh checkout on `main`. The script:
1. Refuses dirty trees.
2. Refuses non-main branches without `--allow-non-main`.
3. Sets `BUILD_COMMIT=$(git rev-parse HEAD)` as a Railway service variable via `railway variables --set ... --skip-deploys` BEFORE running `railway up`, so the live container's `/health` reports the actual built SHA.

**Do NOT run `railway up` directly.** Railway's auto-injected `RAILWAY_GIT_COMMIT_SHA` only populates on GitHub-triggered builds (which we don't use — auto-deploy is OFF). Bare `railway up` ships with whatever stale SHA Railway last saw. Observed 2026-05-15 on the SSE-auto-reconnect deploy: `railway up` succeeded but `/health` reported the prior pin-deploy SHA, defeating Z2 verification. Smoke probe (PR2) gates promotion regardless of trigger.

Pre-existing pre-commit hook drift on the working tree (recurring `M` on `bin/backfill_opportunity_product_line.py`, `orchestrator/*.py`, `AGENTS.md`) is a standing condition — `bin/deploy.sh` refuses dirty trees, so stash before running and restore after.

## Rollback

- `bin/rollback-agent.py <agent_short_name> --to-version <N>` — single-agent rollback. Calls the Anthropic SDK to swap the prompt pin.
- `bin/rollback-deploy.py --artifact-run <gh_run_id> --apply` — whole-deploy rollback. Downloads `pre_deploy_versions` artifact from the named workflow run, diffs against current pin file, invokes `rollback-agent.py` per changed agent. Dry-run is default; `--apply` is the explicit go. ~30s/agent.

Full procedure: `docs/runbook-prompt-rollback.md`.

## Portco-identifier scrub (pre-public-flip gate)

`bin/scrub-portco.py` scans the repo for portco-specific identifiers — Slack IDs, Anthropic agent/session/vault IDs, vendor names, deployment URLs — that must not ship in a public OSS distribution. Patterns in `bin/scrub-portco-patterns.yml` with three severity tiers:

- `HIGH` — blocks the public flip (Slack/SF/Anthropic IDs, portco names, vendor relationships, emails, deployment URLs).
- `MEDIUM` — review before flip (incident references, internal PR numbers).
- `LOW` — informational only.

```bash
python bin/scrub-portco.py                       # scan repo root, human-readable
python bin/scrub-portco.py --json                # JSON for tooling
python bin/scrub-portco.py --severity HIGH       # HIGH only
python bin/scrub-portco.py --root path/to/dir    # scan a subdir
```

Exit code is `1` if any HIGH finding remains after the allowlist, `0` otherwise. When adding a pattern, drop a fixture into `bin/scrub-portco-fixtures/` that exercises it and re-run against that subtree. Designed to run pre-public-flip and as a CI gate after flip.

## Measurement & audits

- `bin/measure-deploy-risk.py` — monthly on the 1st at 09:00 PT (`.github/workflows/measure-deploy-risk.yml`). Output: 3-sheet .xlsx (sessions by hour, error rate by hour, deploys-vs-incidents) DMed to admins. Data source: `session_costs.outcome` (added by `00AB_session_costs_outcome.sql`). If a workday error-rate cluster appears, this is the evidence to re-introduce the business-hours freeze (Plan #42 PR1 v1).
- `bin/audit-error-categories.py` — Phase 0 retrospective for the ❌-Watcher. Classifies historical ❌ outcomes to size the watcher's addressable error universe before each phase.
- `bin/audit-mcp-toolsets.py` — flags any agent whose `tools[]` still has an orphan `mcp_toolset` entry. Re-run after any sub-agent provisioning. See `docs/runbooks/managed-agents-conformance.md`.
- `bin/audit-toolset-drift.py` — on-demand version of the weekly `agent_toolset_drift_canary` workflow.

## Watcher metrics

`bin/watcher-metrics.py` — dashboard for the ❌-Watcher: queue depth (`watcher_pending`), success rate, latency. Run on-demand. Pairs with the orchestrator-side `WATCHER_ENABLED` kill switch.

## Backfills

- `bin/backfill_lead_sync_fields.py --portco <key>` — re-runs the nightly Lead sync to fill the four custom fields (`Discovery_Call_Booked__c`, `Funnel_Stage__c`, `MQL_SDR_Accepted_Date_Time__c`, `SDR_Qualified_Date_Time__c`) after they land in a portco's SF org. Required by `fix/lead-sync-schema`.
- `bin/backfill_opportunity_product_line.py --portco <key>` — historical fill of `opportunities.product_line` from `Opportunity.Product_Line__c` (migration `00AQ_opp_product_line.sql`). Snapshot #14 and earlier stay NULL otherwise.
