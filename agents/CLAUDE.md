# agents/ — Managed Agent definitions, provisioning, prompt deploys

Module-specific context for the `agents/` directory. The root `CLAUDE.md` has the high-level architecture; this file covers provisioning, prompt-deploy plumbing, rollback, and multi-agent runtime mechanics.

## Twelve agents across four tiers

Defined in `setup_agents.py`; prompts updated in production via `update_prompts.py`.

**Tier 1 — Entry** (Slack → orchestrator):
1. **Prompt Engineer** (Sonnet 4.6) — preprocesses Slack questions BEFORE the Coordinator session. Reads `/{portco}/instructions.md`, injects standing data rules, corrects field names, emits JSON (`improved_prompt`, `summary`, `plan_steps`, `expected_output`, `risk_flags`). Orchestrator turns this into a rich Slack ack. Single-turn, no MCP. ID: `PROMPT_ENGINEER_ID`. Provision: `python agents/provision_prompt_engineer.py`.
2. **Quick Answer** (Sonnet 4.6) — single-fact Slack lookups that skip the full investigation pipeline.

**Tier 2 — Orchestration**:
3. **Coordinator** (Opus 4.8) — orchestrates sub-agents, runs validation pipeline, delegates to the Writing Agent (multiagent) before `post_report`. Does not query SF directly.
4. **Dream Agent** (Sonnet 4.6) — nightly hypothesis generation.

**Tier 3 — Data + reasoning specialists**:
5. **Three Specialists** (Sonnet 4.6) — Pipeline Monitor, Sales Process Monitor, Post-Sales Monitor. Materialize SF reads via `dump_sf_query` (Parquet).
6. **Statistician** (Opus 4.8) — CIs, p-values, regression, survival analysis.
7. **Chart Designer** (Sonnet 4.6) — QuickChart visualization.
8. **Adversarial Reviewer** (Opus 4.8) — five-check challenge process.
9. **Cross-Domain Synthesizer** (Opus 4.8) — connects signals into named patterns.

**Tier 4 — Output composition**:
10. **Writing Agent** (Haiku 4.5) — primary prose composer. In the Coordinator's multiagent roster since 2026-05-27 — the Coordinator delegates by addressing the Writing Agent in its session thread (persistent within parent session). Returns finished prose grounded in Strunk's *Elements of Style*. No MCP, no memory store. Prompt source-of-truth: `orchestrator/writing_agent.py:build_system_prompt()`. The legacy Report Writer is deprecated; the prior `write_prose` custom tool was retired 2026-05-27. Created by `setup_agents.py` (`provision_writing_agent.py` remains for rotation/recovery). ID: `WRITING_AGENT_ID`.

**Out-of-roster agents** (separate sessions, not Coordinator-routed):
- **RFP Responder** (Opus 4.8) — drafts inbound RFPs. Provision: `provision_rfp_agent.py`. ID: `RFP_RESPONDER_ID`. See `orchestrator/CLAUDE.md` for the runtime flow.
- **RFP Reviewer** (Opus 4.8) — quality-gate between RFP draft and Slack post. Standalone agent, separate session per review, dispatched via the `review_rfp_draft` custom tool. Provision: `provision_rfp_reviewer_agent.py`. ID: `RFP_REVIEWER_ID`.
- **❌-Watcher** — picks up terminally-failed investigations, writes draft fix PRs. Provision: `provision_watcher_agent.py`. ID: `WATCHER_AGENT_ID`. See `orchestrator/CLAUDE.md` for runtime, queue, kill switch.

## Memory stores

Two Anthropic memory stores attached to every session:
- **Methodology store** (read-only) — `METHODOLOGY_STORE_ID`. GTM methodology, benchmarks, SOQL patterns. Content lives in `skills/gtm-methodology.md`.
- **Health store** (read-write) — `HEALTH_STORE_ID`. Per-portco operational state: `/{portco}/metrics.md`, `open_questions.md`, `findings.md`, `resolved.md`, `schema_cache.md`. System-level: `/system/learnings.md`, `session_log.md`, `prompt_patches.md`.

## Prompt deploys

`.github/workflows/deploy-prompts.yml` auto-runs `update_prompts.py` whenever a merge to main touches `setup_agents.py`, `update_prompts.py`, `orchestrator/writing_agent.py`, or `update_subagent_tools.py`. Required GH secrets: `ANTHROPIC_API_KEY` + every `*_ID` secret (one per agent in the roster). The legacy `REPORT_WRITER_ID` secret was dropped 2026-05-11.

If secrets are missing the workflow fails loud — no silent skip. Closes the deploy gap from 2026-05-11 where PR #37 shipped a Coordinator prompt change at 14:16 PT and a 14:44 PT session picked up the stale v20 prompt ($47 wasted).

## Prompt-deploy gate

Plan #42 PR3 added a two-part safety net:

**Label gate.** Every push to `main` touching a prompt source file must originate from a merged PR carrying the `prompt-author-verified` label. The workflow looks up the PR via `gh pr list --search "$GITHUB_SHA" --state merged` and fails loud if absent. `workflow_dispatch` runs skip the gate. This is a tripwire for forgetfulness, NOT a security control — the same dev who sets the label is the dev who merges the PR.

**Artifact bracket.** The workflow uploads `active_versions.json` as workflow artifacts `pre_deploy_versions` (before `update_prompts.py`) and `post_deploy_versions` (after). The pair is the source-of-truth rollback target — `HEAD~1` is unreliable because the workflow auto-commits pin updates. Artifacts retain 90 days.

## Rollback

- **Single agent**: `python bin/rollback-agent.py <agent_short_name> --to-version <N>`.
- **Whole deploy**: `python bin/rollback-deploy.py --artifact-run <gh_run_id> --apply`. The wrapper downloads `pre_deploy_versions` from the named workflow run, diffs against current pin file, invokes `rollback-agent.py` per changed agent. Dry-run is the default; `--apply` is the explicit go. Recovery ~30s/agent. Full procedure: `docs/runbook-prompt-rollback.md`.

## Multi-agent orchestration

Multi-agent is enabled (beta `managed-agents-2026-04-01`). The Coordinator's `multiagent.agents` roster has 8 sub-agents in production: Pipeline Monitor, Sales Process Monitor, Post-Sales Monitor, Statistician, Adversarial Reviewer, Cross-Domain Synthesizer, Chart Designer, Writing Agent. Prompt Engineer is NOT in the roster — it preprocesses BEFORE the Coordinator session exists. RFP Responder, RFP Reviewer, Watcher Agent, Quick Answer, Dream Agent are also out — see `update_coordinator_roster.py` for the live `ROSTER` constant and the audit rationale committed 2026-05-27.

The 4 validation/synthesis/chart agents were activated 2026-05-11 in PR `chore/dead-agent-cleanup` — they had been provisioned but never wired into the roster, so the validation pipeline the Coordinator prompt describes never ran end-to-end before that date. The Writing Agent was added 2026-05-27, replacing the `write_prose` custom-tool dispatch path with multiagent delegation.

Each sub-agent owns its own configuration — `tools`, `mcp_servers`, `system` prompt. Per the docs: *"Each agent uses its own configuration (model, system prompt, tools, MCP servers, and skills) as defined when that agent was created. Tools and context are not shared."*

**Known runtime pitfall** (observed in production 2026-05-11): a sub-agent ran 15 filesystem diagnostics (`which sfdx`, `find / -name "*salesforce*"`, `ls /var/run/`, etc.) trying to verify MCP access and concluded BLOCKED, even though its agent definition correctly listed the Salesforce `mcp_toolset`. MCP tools live in the agent's tool registry — NOT as local binaries, sockets, or daemons. Specialist prompts must instruct verification by **attempting a trivial call** (e.g. `soqlQuery({"q": "SELECT Id FROM Account LIMIT 1"})`), not by inspecting the filesystem.

## Toolset versioning — `agent_toolset_20260401`

Every agent's `tools[]` includes the built-in `agent_toolset_20260401` entry (Python + files + bash). The date suffix matches the beta header — it is the contract version, NOT a per-agent snapshot. Tools behind the entry can FLOAT within the dated contract; breaking changes ship as a new dated ID.

- `setup_agents.py` and `update_subagent_tools.py` pin `agent_toolset_20260401` once.
- Drift IS possible within the contract. `.github/workflows/toolset-drift-canary.yml` runs weekly: snapshots every agent's `tools[*]` payload via `GET /v1/agents/{id}`, diffs against the most recent snapshot under `agents/toolset-snapshots/`, fails + admin-DMs on drift without a corresponding update.
- On-demand check: `bin/audit-toolset-drift.py`.

## Conformance audits — orphan MCP toolsets

- `bin/audit-mcp-toolsets.py` — flags any agent whose `tools[]` still includes an orphan `mcp_toolset` entry. Iteration 3 removed Salesforce MCP from every sub-agent, but `session_runner.py` auto-approve still tolerates such entries. Re-run after any sub-agent provisioning.
- See `docs/runbooks/managed-agents-conformance.md` for the operator workflow on each failure mode.

## Outcomes / rubric-based grading

Outcomes (rubric-based grading) requested 2026-05-06, not yet enabled. Rubrics under `rubrics/` are reference-only until then.
