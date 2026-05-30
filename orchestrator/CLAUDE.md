# orchestrator/ — Runtime glue between Slack, Anthropic, Salesforce, Postgres

Module-specific context for `orchestrator/`. Root `CLAUDE.md` has high-level architecture; `agents/CLAUDE.md` covers agent definitions and prompt deploys. This file covers runtime mechanics.

## Module map

- `main.py` — entry point. Starts Slack Socket Mode, APScheduler cron, investigation worker thread, watcher scheduler tick.
- `session_runner.py` — creates Managed Agent sessions, handles the `requires_action` custom-tool lifecycle. All SF queries via MCP vaults. Per-session cost tracking. Dispatches `post_report` to renderer + editor + Slack. The retired `_dispatch_write_prose` adapter was removed 2026-05-27 — prose composition is now multiagent thread delegation, not a custom tool.
- `writing_agent.py` — Writing Agent prompt source-of-truth (`build_system_prompt()`). `agents/update_prompts.py` deploys it to Anthropic. `WritingAgentResult` dataclass remains as shape documentation for the duplicate-retry-cache helper in `session_runner.py`. The orchestrator-side `write_prose()` function was removed 2026-05-27.
- `slack_bot.py` — Socket Mode event handler. Classifies messages as questions or feedback by prefix. Converts markdown → Slack mrkdwn, splits blocks at 2900 chars.
- `data_sources.py` — adapter pattern for multi-CRM. `SalesforceCliAdapter`, `HubSpotAdapter` (REST), `ZohoAdapter` (COQL). Registry maps type strings to classes.
- `portco_registry.py` — loads `portco_config.json`, maps channels to companies, resolves data sources.
- `db_adapter.py` — Railway Postgres for historical queries (24h-stale OK) and thread-to-session persistence (survives container restarts). Same-day keyword detection triggers live MCP fallback.
- `self_heal.py` — post-session review. Fetches session events, identifies SOQL errors/inefficiencies, saves learnings to memory store, writes prompt patches.
- `self_improve.py` — daily doc crawler. Hashes 20 Managed Agents doc pages, diffs against prior state, analyzes via Sonnet, DMs release notes.
- `config.py` — loads `.env` with manual dotenv parsing (no python-dotenv dependency).
- `lifecycle.py`, `watcher_*.py`, `kapa_rest_tool.py`, `kapa_rate_limiter.py` — see below.

## Key Patterns

**Ad-hoc query flow**: Slack question → Prompt Engineer preprocesses (data rules, refines prompt, plan) → Coordinator session with MCP vault → sub-agents query SF via soqlQuery/describeSObject → adversarial review + statistical validation → Coordinator delegates to Writing Agent (multiagent) → quality-check rubric (max 2 thread follow-ups) → `post_report` → editor pass → render → Slack. The `already_preprocessed` flag prevents double-preprocessing.

**Writing pass**: Coordinator never writes user-facing prose itself. After validation it delegates to the Writing Agent (Haiku 4.5, in the multiagent roster as of 2026-05-27) by addressing it in its session thread with a structured payload + `response_shape` hint (`one_fact`, `comparative`, `why`, `briefing`, `table`, `methodology`, `data_pull`, `hybrid_data_synthesis`). The Writing Agent's thread is persistent within the parent session — a rewrite request returns to the same thread and the agent sees its prior draft. Returns finished prose grounded in Strunk as a JSON object in `agent.message`. Coordinator inspects against a five-check rubric (stats notation, unglossed acronyms, sentence-level bloat, inline caveats, decision-recommendation closing); failures get re-asked up to 2× in the same thread. Persistent failure falls through to direct `post_report` with `[WRITING_AGENT_FALLTHROUGH]`. The deterministic editor pass (`editor.py`) and `prose_polish.py` run AFTER as last-mile safety nets — they trim length and gloss missed acronyms but never compose prose.

**Custom tool lifecycle**: Agent emits `agent.custom_tool_use` → orchestrator buffers it → session goes idle with `stop_reason.type == "requires_action"` → orchestrator dispatches tool, sends `user.custom_tool_result` → session resumes. MCP tools with `evaluated_permission == "ask"` get auto-approved via `user.tool_confirmation`.

**Event-driven canvas sync**: every successful `post_report` dispatch fires `surface_pusher.push_to_canvas(portco)` on a daemon thread; failures log `[SURFACE_PUSH_FAILED]` and the daily 08:00 PT cron catches up (Plan #33 failure-mode table).

**Thread persistence**: Thread-to-session map in both memory (fast) and Postgres (survives restarts). Follow-up messages in a Slack thread reuse the existing session. DB-backed lookup restores sessions after container deploys. No session-level timeouts — sessions can run 55+ minutes.

**Investigation recovery**: Every ad-hoc investigation tracked in `investigations` (queued→running→completed/failed). On container restart, `recover_interrupted_investigations()` finds rows still 'running' from a previous container, tries to resume the Anthropic session (if alive), or starts fresh with the original question. Posts Slack message in thread explaining the restart. Max 2 recovery attempts. `RAILWAY_DEPLOYMENT_ID` (or random UUID) is the container_id.

**Feedback loop**: Slack messages starting with "remember", "always", "never", etc. are written to `/instructions.md` in the health memory store. All future sessions read and apply these.

**Prompt caching**: Messages API calls (self_heal, self_improve) use `cache_control: {"type": "ephemeral"}` with system prompts large enough to hit the 1024-token Sonnet minimum. Managed Agent sessions cache internally across turns.

## RFP Responder runtime

Standalone Managed Agent (NOT in the Coordinator's roster) that drafts responses to inbound RFPs (~60/year, ~1/week). Per-upload session.

**Flow** (all in `rfp_runner.py`):
1. Slack `message` with `subtype="file_share"` lands in the RFP channel. `slack_bot.handle_message` checks `rfp_runner.is_rfp_channel(channel_id)` BEFORE the generic subtype short-circuit and dispatches in-thread.
2. Runner posts an immediate ack, spawns a daemon thread so the Bolt handler returns under the 3s budget. No queue, no DB persistence of in-flight RFPs — if the container restarts mid-draft, the user re-uploads.
3. Worker downloads the file (`url_private_download` + bot token), uploads to Anthropic Files API, creates a fresh session against `RFP_RESPONDER_ID` with the file at `/workspace/rfp_input.<ext>`.
4. Agent classifies each question — product → Kapa (`search_knowledge_base`, streaming REST per `kapa_rest_tool.py`), market/customer → SF (`db_query` snapshot for historical, `dump_sf_query` for live), company facts → `web_search` scoped to `site:acme.example.com`. Every product answer carries a Kapa source URL; every market answer carries a "Basis:" line naming the SF object + filter. Unanswerable questions are `[NEEDS HUMAN INPUT]`. The RFP Responder calls `review_rfp_draft` (RFP Reviewer) BEFORE its final summary, revises up to 2× on REVISE.
5. Output: `/mnt/session/outputs/rfp_response.<ext>` (matches input shape via `xlsx`/`docx`/`pdf` skills) + JSON sidecar `rfp_qa_index.json`.
6. Runner posts the agent's final `agent.message` (phone-readable summary) to the Slack thread, uploads each output file via `_download_session_files`.
7. `_log_session_usage(trigger="slack-rfp", portco_key="acme", agent_id=RFP_RESPONDER_ID)` lands a cost row; `_archive_session` releases the container.

**Why standalone vs. extending the Coordinator's roster**: single-shot factual lookup, not iterative analytical validation. Adversarial Reviewer + Statistician + Writing Agent delegation adds latency and cost without changing answer quality. A bad RFP prompt change cannot break the nightly Pipeline Monitor. Slack infra, vault credentials, cost tracking, and `_stream_and_handle` are reused.

## ❌-Watcher runtime

Autonomous agent that picks up terminally-failed investigations (sessions that ended with ❌) and writes draft fix PRs. Phase 1 shipped across several PRs (last one added the kill switch + metrics dashboard). Design was reviewed and approved before build.

**Flow**:
1. `lifecycle.terminalize()` detects a ❌ outcome, enqueues a row into `watcher_pending` (`watcher_pending_db.py`). Recursion guard prevents watcher-on-watcher loops.
2. APScheduler tick every 30s (`watcher_worker.py:_tick`) drains the queue into a dedicated `WatcherThreadPoolExecutor` (`max_workers=5`, separate from main investigation pool so a slow watcher cannot starve user sessions). Startup `catch_up_sweep()` back-fills the last 30 min of failures.
3. `_run_watcher_job` spawns a Managed Agent session against `WATCHER_AGENT_ID`. Agent reads the failed session events, diagnoses, writes fix, opens draft PR.
4. Tool surface: **4 custom tools only** — `watcher_create_branch`, `watcher_write_file`, `watcher_create_pr`, `watcher_add_comment`. Dispatched by `watcher_dispatch.py`, which owns `WATCHER_GH_TOKEN` (the agent never sees it). Editable-path allowlist + branch-prefix guard (`watcher/<inv_id>-`) + conflict check (any open PR or recently-merged PR touching the failing area) enforced at the dispatcher, NOT in the prompt.
5. Codex verdict poll: after PR is opened, watcher polls the `codex-review.yml` workflow run and reports the verdict to the originating Slack thread.

**Why custom tools instead of a GitHub MCP server**: `anthropic-sdk-python` 0.92+ exposes `BetaManagedAgentsURLMCPServerParams` with no `authorization_token` field — the SDK currently does not support secret-bearing MCP at the Managed Agents layer. Custom tools keep the 4-tool allowlist intent while putting the PAT in the orchestrator (strictly better for credential isolation — PAT never reaches Anthropic infra). Loses the other 46 GH MCP tools (out of scope v1). Full deviation rationale in `agents/provision_watcher_agent.py` docstring.

**Kill switch & observability**:
- `watcher_kill_switch.py` — circuit breaker for runaway watcher behavior (cost / error-rate thresholds). Global kill: `WATCHER_ENABLED=false`.
- `bin/watcher-metrics.py` — queue depth, success rate, latency dashboard. Run on-demand.
- `bin/audit-error-categories.py` — Phase 0 retrospective: classifies historical ❌ outcomes to size the addressable error universe before each phase.

## Cost tracking

Two-ledger architecture (Plan #35).

- **Local session-level estimator (attribution)**: `session_runner.py:_log_session_usage` extracts the five token categories from session usage, multiplies by `MODEL_COSTS_PER_MTOK`, writes one row per session to Postgres. Only place that knows portco / thread / user / trigger / verbosity / agent attribution. Covers every Managed Agent session (dream, investigation, forecast, adhoc, quick_answer, recovery).
- **Anthropic Admin Usage & Cost API (ground truth)**: 06:00 PT cron in `cost_collector.py:pull_anthropic_daily_costs` pulls yesterday's tokens (`/v1/organizations/usage_report/messages`) and USD (`/v1/organizations/cost_report`) per model per workspace per service tier. Idempotent upsert by `(bucket_date, model, workspace_id, service_tier)`. 3-day lookback for late-arriving data. Cannot break down by portco/task/thread — that attribution lives only locally.

**DB tables** (`migrations/00Z_cost_tracking.sql`):
- `session_costs` — per-session ledger with full attribution + `estimated_cost_usd` + `raw_usage_json`. Unique index on `session_id`.
- `anthropic_daily_costs` — daily ground-truth rollup keyed by `(bucket_date, model, workspace_id, service_tier)`.
- `messages_api_calls` — parallel ledger for non-session traffic (`self_heal`, `self_improve`). Wrapped via `cost_collector.track_messages_call(caller, response, model, portco_key)` around every `client.messages.create()`.
- `cost_rollup_daily` view — per-day, per-portco, per-task-type aggregation used by `/cost` and the digest.

**Reconciliation**: daily job compares `SUM(estimated_cost_usd)` to `anthropic_daily_costs.cost_usd`. `drift_pct > 10%` → Slack watch notice (deduped once/day). `> 25%` → recommended `MODEL_COSTS_PER_MTOK` refresh.

**Reporting surfaces**:
- `/cost` Slack slash command — `/cost [scope] [window]`, supports `today | week | month [portco]` and `/cost reconcile`. Renders within 3s for windows up to 30 days. Requires `commands` scope.
- Daily DM digest — 08:00 PT cron in `cost_digest.send_daily_cost_digest()` posts each admin: yesterday's total, by-portco split, by-task split, cache hit rate, drift line, top-5 sessions. Leads with drift watch if `|drift_pct| > 10%`.
- **Surface integration**: `SurfaceState.cost_block` renders trailing 7d/30d totals, trend, top task, cache hit % in the Canvas under "Operating Cost." Reads from `cost_rollup_daily`.

**Env var**: `ANTHROPIC_ADMIN_KEY` on Railway (separate from `ANTHROPIC_API_KEY`). Without it, local ledger and `/cost` still work; reconciliation degrades gracefully.

## Prompt compression (Compresr)

Compresr (YC W26) SDK cuts input-token spend on the two Messages-API call sites with large compressible payloads (Plan #37).

**Call sites**:
- `self_improve._analyze_changes` — `espresso_v1` (general-purpose, no query) on the concatenated doc-page payload (~15K chars after the 2026-05-14 Anthropic docs reshuffle; was ~100 KB). `min_chars=12000`.
- `self_heal._analyze_session` — `latte_v1` (query-aware) on the session-summary JSON, with `query=f"Review session {session_id} for tool errors and code fixes"`. `min_chars=8000`.
- Tier B opt-in: `session_runner.run_adhoc_mcp_session` kickoff text, gated by `COMPRESS_ADHOC_KICKOFF` (default `false`). Only compresses if kickoff > 4 KB. Latte_v1 with the user question as query.

Managed Agents server-side system prompts and inter-turn reasoning are **not addressable**.

**Caching**: `compresr_cache` table keyed by `sha256(text || model || (query or ''))`, 7-day TTL. `_analyze_changes` runs nightly on near-identical doc pages — cache-hit >50% expected week-over-week.

**Fallback**: `compress_prompt` silently returns original text on any failure. If fallback rate exceeds 25% over 24h, the daily digest emits a watch notice.

**Per-site kill switch**: tracks downstream JSON parse failure rate from `_analyze_session` and `_analyze_changes`. Rate > 2× prior 14-day baseline → auto-disable for that site + Slack notify. Global kill: `COMPRESSION_ENABLED=false`.

**Env var**: `COMPRESR_API_KEY` on Railway (NOT local), format `cmp_*`. Loaded by `config.py`'s manual dotenv parser.

## xlsx consolidation

Enabled by default since #247 / #257. When the Coordinator's `post_report` dispatch produces multiple xlsx files, `_dispatch_post_report` consolidates them into one workbook (one sheet per source file) before upload. Reduces Slack clutter and gives recipients a single artifact.

- **Kill switch**: `XLSX_CONSOLIDATE_ENABLED=false` (emergency only).
- **Size cap**: `XLSX_CONSOLIDATE_SIZE_CAP_MB` (default 50). Above cap, consolidation skips and files upload individually.
- **User override**: the agent can request split files via the `_split=True` preference — `[XLSX_CONSOLIDATE_SKIPPED_USER_OVERRIDE]` logs the bypass.
- Failures log `[XLSX_CONSOLIDATE_FAILED]` and fall through to individual uploads.

Plan refs: #48 Phase 3, #52 PR-E + PR-H. Implementation: `xlsx_consolidate.py` + `session_runner.py:_dispatch_post_report`.

## Kapa — Acme internal knowledge base

Kapa exposes a streaming REST API at `https://api.kapa.ai/query/v1/projects/<project_id>/chat/stream/`, consumed via the `search_knowledge_base` custom tool (`kapa_rest_tool.py`). The agent-facing tool name is preserved from the retired MCP integration. Index covers:
- **Confluence wiki** (`your-org.atlassian.net/wiki`) — DEVOPS, PE, DPD, AGILE, AF, CS spaces.
- **Jira** (`your-org.atlassian.net/browse/...`) — ENG, SE projects. Issue bodies embed originating SF Case URLs.
- **Public help docs** (`help.acme.example.com/advanced`).
- **Slack archive** (`your-org.slack.com`) — limited.
- **Partner docs** (`docs.partnera.ai`, `support.partnerb.com`).

See the "Kapa" section in the root `CLAUDE.md` for the full index map.

**Which agents have Kapa access**: Coordinator, Quick Answer, Dream Agent, Post-Sales Monitor, Cross-Domain Synthesizer. Pipeline / Sales Process Monitor, Statistician, Adversarial Reviewer, Chart Designer, Writing Agent — no (off the critical path).

**Auth**: `KAPA_ACME_API_KEY` (Bearer token minted from Kapa Acme tenant after SSO) on the `X-API-KEY` header. No vault, no MCP setup — Kapa's hosted MCP requires OAuth with dynamic client registration; Kapa support (2026-05-14) confirmed they will not provide machine-to-machine OAuth client credentials, so the MCP path is permanently closed. `KAPA_ACME_PROJECT_ID` for the REST URL.

**Rate limit**: 20 req/min per API key on Chat endpoint (Kapa server-side). 60 req/min is the Retrieval endpoint — a common doc-reading slip we corrected 2026-05-14 (Kapa Support case-7326). Agent prompts say "do not loop calls."

**Orchestrator-side rate limiter** (REST path only): `kapa_rate_limiter.py` enforces a 16 req/min token bucket on calls via `kapa_rest_tool.py`. Env: `KAPA_RATE_LIMIT_TOKENS_PER_MIN` (default 16), `KAPA_RATE_LIMIT_ENABLED` (default true).

**SFDC ↔ Jira synthesis**: Acme support pipeline is SF Case → JSM → Jira (`ENG`, `SE`). Cases link to Jira issues; Jira often embeds SF Case URL. Post-Sales Monitor and Cross-Domain Synthesizer pull engineering disposition for a customer issue via a single Kapa query — no separate Jira MCP needed. Worked example in `_KAPA_KNOWLEDGE_BLOCK` in `agents/update_prompts.py`.

## /health endpoint and BUILD_COMMIT

The orchestrator exposes `GET /health` on `PORT` (default 8080):

```json
{
  "build_commit": "<value of BUILD_COMMIT at container start>",
  "deploy_started_at": "<ISO8601 of process start>",
  "active_versions": {<contents of agents/active_versions.json>},
  "status": "ok"
}
```

`BUILD_COMMIT` is injected at Docker build time via `ARG BUILD_COMMIT`. Set correctly per environment:
- **Local docker build**: `docker build --build-arg BUILD_COMMIT=$(git rev-parse HEAD) -t gtm-health-agent .`
- **GitHub Actions**: `--build-arg BUILD_COMMIT=${{ github.sha }}`.
- **Railway**: Railway's Docker builder does not pass git SHA automatically. Set `BUILD_COMMIT` as a build-time variable in the service settings (Variables → Build), or modify `railway.toml` to add `[build] buildArgs = { BUILD_COMMIT = "${{ ci.git_sha }}" }`. The Dockerfile defaults `BUILD_COMMIT` from `RAILWAY_GIT_COMMIT_SHA` via BuildKit ARG-from-ARG so Railway builds get the right SHA. If neither is wired the live container reports `build_commit: "unknown"` — the loud-fail signal that verification is still missing.

Z2 deploy verification curls `/health` and asserts `build_commit` matches the SHA the deploy step just shipped (`gh run view`), not raw `origin/main`. A `build_commit` that disagrees — or is `unknown` — is a hard fail and must block green status.

## SSE reconnect budget vs watchdog tier timing

The SSE event stream and the session watchdog race when the Coordinator stalls mid-flow (e.g. after dispatching a sub-agent and going quiet). Constants in `session_runner.py` and `session_watchdog.py` are sized so the watchdog wins:

- **Watchdog Tier 1** (gentle nudge — inject `user.message` asking Coordinator to ship results or re-dispatch) fires at `STALL_THRESHOLD_SECONDS` (600s) + `WATCHDOG_POLL_SECONDS` (60s) ≈ **11 min**.
- **Watchdog Tier 2** (interrupt non-primary sub-threads via `user.interrupt`) fires at Tier 1 + `WATCHDOG_TIER_ESCALATION_SECONDS` (120s) ≈ **13 min**.
- **Watchdog Tier 3** (mark investigation failed + archive session + admin DM) fires at Tier 2 + 120s ≈ **15 min**.
- **SSE budget exhaustion** (gives up streaming, raises `ReadTimeout` to lifecycle guard) fires at `SSE_MAX_RECONNECT_ATTEMPTS` (7) × `SSE_READ_TIMEOUT_S` (120s) + backoff (~90s) ≈ **15.5 min**.

Before the 2026-05-19 bump, `SSE_MAX_RECONNECT_ATTEMPTS` was 5 and the budget was ~11 min — the watchdog tied or lost the race, and stranded Coordinator sessions terminalized as ❌ before any tier ran. Live repro: sub3 inv 58 (`sesn_EXAMPLE`) on 2026-05-19 04:48:41 UTC. If you lower `SSE_MAX_RECONNECT_ATTEMPTS` below 6, you re-introduce the race; if you raise `STALL_THRESHOLD_SECONDS` above 700s, same problem. Asserted in `plan_47_workstream_a_test.py::test_sse_max_reconnect_attempts_sized_above_watchdog_tier_3`.

The 400-on-follow-up sentinel (`_FollowupBlocked`) is independent of this race and covers BOTH `events.send()` AND `events.stream()` context-manager entry (Plan #47 Workstream A + A.2). A 400 from either site in a thread-follow-up is converted to a polite Slack reply instead of ❌, as long as `_is_requires_action_400()` matches the error body.

## Nightly Pipeline

APScheduler cron (Pacific). All user-facing daily Slack-posting crons were retired 2026-05-14 pending JTBD redefinition — the underlying functions remain importable.

**Active** (silent unless something is wrong):
- 1am — DB sync (SF → Postgres)
- 4am — compresr cache expiry (7-day TTL sweep)
- 6am — Anthropic Admin API daily cost pull (when `ANTHROPIC_ADMIN_KEY` set)
- 6am — `session_thread_events` 30-day TTL purge
- 8am — surface refresh (Canvas push per active portco)
- every 15min — batch poll (when `BATCH_PROCESSING_ENABLED`)
- every 30s — session-size canary (log-only)
- hourly — batch flush / orphan recovery (when `BATCH_PROCESSING_ENABLED`)
- every 30s — watcher tick (drain `watcher_pending` queue)

**Retired** 2026-05-14 (defined; no scheduler registration):
- Midnight self-improvement (`scheduled_self_improve`)
- 3am forecast (`scheduled_forecast`)
- 5am dream → investigation (`scheduled_dream`)
- 7am cost reconciliation (`scheduled_reconcile_costs`)
- 8am cost-digest DM (`scheduled_cost_digest`)

`RUN_NIGHTLY_NOW` env triggers `run_full_nightly_pipeline` 2 min after startup (self-improve → DB sync → forecast → dream → investigation), then auto-clears. APScheduler event listener catches missed jobs.

## Environment Variables

All required vars in `.env.example`. Key IDs from `setup_agents.py`: `ENVIRONMENT_ID`, `DREAM_AGENT_ID`, `COORDINATOR_ID`, `METHODOLOGY_STORE_ID`, `HEALTH_STORE_ID`. Slack: `SLACK_BOT_TOKEN` (xoxb), `SLACK_APP_TOKEN` (xapp), `SLACK_CHANNEL_ID`.

**Self-heal pipeline** (B-track, 2026-05-12):
- `RECOVERY_FRESH_THRESHOLD` — input-side tokens above which interrupted-investigation recovery archives the old session and starts fresh. Default 500_000. Lower to be more aggressive about discarding bloated context.
- `RESULT_VIRTUALIZE_THRESHOLD` — list-shaped tool result rows above which the orchestrator streams to .xlsx and hands the model a compact handle. Default 50.
- `SLACK_ADMIN_USER_IDS` — comma-separated. Catastrophic-failure DMs (`send_notification(admin_only=True)`) target these instead of the public channel.

**Kapa** (2026-05-13):
- `KAPA_ACME_API_KEY` — Bearer token (X-API-KEY header). When unset, dispatcher returns structured error; agent prompts treat as "knowledge base unavailable, proceed without".
- `KAPA_ACME_PROJECT_ID` — UUID for the REST URL.
- `KAPA_RATE_LIMIT_TOKENS_PER_MIN`, `KAPA_RATE_LIMIT_ENABLED` — token bucket config.

**Watcher**:
- `WATCHER_AGENT_ID`, `WATCHER_GH_TOKEN` — agent ID and orchestrator-side GH PAT.
- `WATCHER_ENABLED` — global kill switch.

**xlsx consolidation**:
- `XLSX_CONSOLIDATE_ENABLED` (default true), `XLSX_CONSOLIDATE_SIZE_CAP_MB` (default 50).
