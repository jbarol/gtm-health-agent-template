# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Autonomous GTM operations analyst for a PE firm. Monitors pipeline health, sales process, and retention across portfolio companies via Slack. Built on Anthropic's Managed Agents API with a Python orchestrator that bridges Slack, Salesforce (via MCP vaults), and Claude. Deployed on Railway.

## Running

```bash
# Install deps (from repo root)
pip install -r orchestrator/requirements.txt

# Run the orchestrator (starts Slack bot + cron scheduler + investigation worker)
cd orchestrator && python main.py

# One-time agent setup (creates agents, environment, memory stores — save output IDs to .env)
python agents/setup_agents.py

# Docker (Railway deployment)
docker build -t gtm-health-agent .
docker run --env-file .env gtm-health-agent
```

Salesforce access is via MCP vaults (Acme vault), not sf CLI. The sf CLI dependency was eliminated — all queries go through soqlQuery/describeSObject MCP tools.

`AGENTS.md` at the repo root is auto-generated from this file by `~/.claude/hooks/agents_md_sync.py` on SessionStart — edit CLAUDE.md, never AGENTS.md.

## Testing

There is no Makefile or `pyproject.toml`. Tests are discovered by file suffix `*_test.py` (not the pytest default `test_*.py`), so pytest needs an explicit pattern. `orchestrator/conftest.py` loads the real `.env` and stubs `slack_bolt.App` at collection time so module imports don't hit Slack.

```bash
# Full suite (orchestrator + bin + agents)
pytest -p no:cacheprovider -o python_files='*_test.py' orchestrator agents bin

# Single file
pytest -p no:cacheprovider -o python_files='*_test.py' orchestrator/session_runner_test.py

# Single test
pytest -p no:cacheprovider -o python_files='*_test.py' orchestrator/session_runner_test.py::test_something

# Type-check (pyrightconfig.json pins Python 3.12, includes orchestrator/agents/bin/scripts)
pyright

# Lint (ruff is the only configured linter — no ruff.toml, uses defaults)
ruff check orchestrator agents bin scripts
```

The `*_smoke_test.py` files (e.g. `orchestrator/batch_smoke_test.py`) hit live APIs using the loaded `.env`; run them deliberately, not as part of routine unit testing.

## Architecture

**Twelve agents across four tiers**, defined in `agents/setup_agents.py` and updated via `agents/update_prompts.py`. Tier 1 = entry; Tier 2 = orchestration; Tier 3 = data + reasoning sub-agents; Tier 4 = output composition.

Tier 1 — Entry (Slack → orchestrator):
1. **Prompt Engineer** (Sonnet 4.6) — preprocesses Slack questions BEFORE the Coordinator session. Reads `/{portco}/instructions.md`, injects standing data rules, corrects field names, and emits a JSON object with improved_prompt, summary, plan_steps, expected_output, risk_flags. The orchestrator turns this into a rich Slack acknowledgment with plan + expected output. Single-turn, no MCP. ID lives in `PROMPT_ENGINEER_ID` (env). Provision once with `python agents/provision_prompt_engineer.py`.
2. **Quick Answer** (Sonnet 4.6) — simple single-fact Slack lookups that skip the full investigation pipeline.

Tier 2 — Orchestration:
3. **Coordinator** (Opus 4.8) — orchestrates sub-agents, runs validation pipeline, delegates to the Writing Agent (via the multiagent runtime) before `post_report`. Does not query SF directly.
4. **Dream Agent** (Sonnet 4.6) — nightly hypothesis generation, writes investigation plans for the Coordinator.

Tier 3 — Data + reasoning specialists:
5. **Three Specialists** (Sonnet 4.6) — Pipeline Monitor, Sales Process Monitor, Post-Sales Monitor. Materialize SF reads via `dump_sf_query` (Parquet), report findings with confidence tags.
6. **Statistician** (Opus 4.8) — PhD-level quantitative validation: CIs, p-values, regression, survival analysis.
7. **Chart Designer** (Sonnet 4.6) — data visualization via QuickChart.
8. **Adversarial Reviewer** (Opus 4.8) — five-check challenge process on every finding before it reaches Slack.
9. **Cross-Domain Synthesizer** (Opus 4.8) — connects signals across pipeline/sales/post-sales into named patterns.

Tier 4 — Output composition:
10. **Writing Agent** (Haiku 4.5) — primary prose composer. In the Coordinator's multiagent roster since 2026-05-27 — the Coordinator delegates prose composition by addressing the Writing Agent in its session thread (persistent across delegations within the parent session). Agent returns finished prose grounded in Strunk's *Elements of Style*. No MCP, no memory store, single composition turn per delegation. The legacy Report Writer (Sonnet 4.6) is deprecated and unused; the prior `write_prose` custom tool was retired 2026-05-27 in favor of the multiagent dispatch path.

**Orchestrator** (`orchestrator/`) is the glue — not an agent itself:

- `main.py` — entry point. Starts Slack Socket Mode, APScheduler cron, investigation worker thread.
- `session_runner.py` — creates Managed Agent sessions, handles the `requires_action` custom tool lifecycle. All SF queries via MCP vaults. Includes per-session cost tracking ($input/$output/$cache). Dispatches `post_report` to the renderer + editor + Slack. The retired `_dispatch_write_prose` adapter was removed 2026-05-27 — prose composition is now a multiagent thread delegation owned by Anthropic's runtime, not a custom tool dispatched by the orchestrator.
- `writing_agent.py` — Writing Agent prompt source-of-truth. Holds the Strunk-grounded system prompt that `agents/update_prompts.py` deploys to the Anthropic side. The orchestrator-side `write_prose()` function was removed 2026-05-27 when the Writing Agent moved into the Coordinator's multiagent roster — the multiagent runtime now owns the Writing Agent's session thread within the parent session. `WritingAgentResult` dataclass remains for shape documentation (the result-payload shape the duplicate-retry-cache helper in `session_runner.py` references).
- `slack_bot.py` — Socket Mode event handler. Classifies messages as questions or feedback (by prefix detection). Converts markdown to Slack mrkdwn, splits blocks at 2900 chars.
- `data_sources.py` — adapter pattern for multi-CRM support. `SalesforceCliAdapter`, `HubSpotAdapter` (REST), `ZohoAdapter` (COQL). Registry maps type strings to classes.
- `portco_registry.py` — loads `portco_config.json`, maps channels to companies, resolves data sources.
- `db_adapter.py` — Railway Postgres for historical queries (24h-stale OK) and thread-to-session persistence (survives container restarts). Detects same-day keywords for live MCP fallback.
- `self_heal.py` — post-session review. Fetches session event history, identifies SOQL errors and inefficiencies, saves learnings to memory store, writes prompt patches.
- `self_improve.py` — daily doc crawler. Hashes 20 Managed Agents doc pages, diffs against prior state, analyzes changes via Sonnet, DMs release notes.
- `config.py` — loads `.env` with manual dotenv parsing (no python-dotenv dependency).

**Two Anthropic memory stores** attached to every session:
- Methodology store (read-only) — GTM audit methodology, benchmarks, SOQL patterns. Content lives in `skills/gtm-methodology.md`.
- Health store (read-write) — per-portco operational state: `/{portco}/metrics.md`, `open_questions.md`, `findings.md`, `resolved.md`, `schema_cache.md`. System-level: `/system/learnings.md`, `session_log.md`, `prompt_patches.md`.

## Key Patterns

**Ad-hoc query flow**: Slack question → Prompt Engineer preprocesses (injects data rules, refines prompt, generates ack with plan) → Coordinator session with MCP vault → agents query SF via soqlQuery/describeSObject → adversarial review + statistical validation → Coordinator delegates to the Writing Agent (via the multiagent runtime) → Coordinator quality-check rubric (max 2 thread follow-ups) → `post_report` → editor pass → render → Slack. The `already_preprocessed` flag prevents double-preprocessing.

**Writing pass**: The Coordinator never writes user-facing prose itself. Once validation passes, it delegates to the Writing Agent (Haiku 4.5, in the Coordinator's `multiagent.agents` roster as of 2026-05-27) by addressing the Writing Agent in its session thread with a structured payload + `response_shape` hint (`one_fact`, `comparative`, `why`, `briefing`, `table`, `methodology`, `data_pull`, `hybrid_data_synthesis`). The Writing Agent's thread is persistent within the parent session, so a rewrite request returns to the same thread and the agent sees its prior draft. The agent returns finished prose grounded in Strunk as a JSON object in its `agent.message`. The Coordinator inspects the result against a five-check rubric (stats notation, unglossed acronyms, sentence-level bloat, inline caveats, decision-recommendation closing); failures get re-asked with specific feedback up to 2 times in the same thread. Persistent failure falls through to direct `post_report` with `[WRITING_AGENT_FALLTHROUGH]` in the audit trail. The deterministic editor pass (`orchestrator/editor.py`) and `prose_polish.py` run AFTER the Writing Agent as the last-mile safety nets — they trim length and gloss missed acronyms but never compose prose. The prompt source-of-truth lives in `orchestrator/writing_agent.py:build_system_prompt()` and is deployed via `agents/update_prompts.py`. Provisioning: `python agents/provision_writing_agent.py` once after PR merge; ID lives in `WRITING_AGENT_ID` (env), rotate without code change. The retired `write_prose` custom tool and its orchestrator-side dispatch (`session_runner._dispatch_write_prose`) were removed in the same migration — any reference in older PRs / runbooks describing a "fresh single-turn Haiku session per call" is now historical.

**Custom tool lifecycle**: Agent emits `agent.custom_tool_use` → orchestrator buffers it → session goes idle with `stop_reason.type == "requires_action"` → orchestrator dispatches tool, sends `user.custom_tool_result` → session resumes. MCP tools with `evaluated_permission == "ask"` get auto-approved via `user.tool_confirmation`.

**Event-driven canvas sync**: every successful `post_report` dispatch fires `surface_pusher.push_to_canvas(portco)` asynchronously on a daemon thread; failures are logged as `[SURFACE_PUSH_FAILED]` and the daily 08:00 PT cron catches up (Plan #33 failure-mode table).

**Thread persistence**: Thread-to-session map stored in both memory (fast) and Postgres (survives restarts). Follow-up messages in a Slack thread reuse the existing session. DB-backed lookup restores sessions after container deploys. No session-level timeouts — sessions can run 55+ minutes.

**Investigation recovery**: Every ad-hoc investigation is tracked in the `investigations` table (queued→running→completed/failed). On container restart, `recover_interrupted_investigations()` finds rows still marked 'running' from a previous container, tries to resume the existing Anthropic session (if still alive), or starts a fresh session with the original question. Posts a Slack message in the thread explaining the restart. Max 2 recovery attempts per investigation. Uses `RAILWAY_DEPLOYMENT_ID` (or random UUID) as container_id to distinguish old vs. current container.

**Feedback loop**: Slack messages starting with "remember", "always", "never", etc. are written to `/instructions.md` in the health memory store. All future agent sessions read and apply these standing instructions.

**Prompt caching**: Messages API calls (self_heal, self_improve) use `cache_control: {"type": "ephemeral"}` with system prompts large enough to hit the 1024-token minimum for Sonnet. Managed Agent sessions cache internally across turns.

**Cost tracking**: Per-session cost estimates logged after every session based on model-specific pricing (Opus/Sonnet/Haiku input/output/cache rates). Cache hit percentage tracked. See "Cost tracking" section below for the full two-ledger architecture and reconciliation logic.

## RFP Responder

Standalone Managed Agent that drafts responses to inbound RFPs (~60/year, ~1/week). NOT part of the Coordinator's multi-agent roster — runs in its own session per upload, with its own `agent_id` and version pin. Triggered when a Acme employee uploads an .xlsx / .docx / .pdf file to the dedicated Slack channel identified by `RFP_CHANNEL_ID`.

**Flow** (all in `orchestrator/rfp_runner.py`):
1. Slack `message` event with `subtype="file_share"` lands in the RFP channel. `slack_bot.handle_message` checks `rfp_runner.is_rfp_channel(channel_id)` BEFORE the generic subtype short-circuit and dispatches in-thread.
2. Runner posts an immediate ack, then spawns a daemon thread so the Bolt handler returns under the 3s budget. No queue, no DB persistence of in-flight RFPs — if the container restarts mid-draft, the user re-uploads.
3. Worker downloads the file from Slack (`url_private_download` + bot token), uploads to Anthropic Files API, creates a fresh session against `RFP_RESPONDER_ID` with the file mounted at `/workspace/rfp_input.<ext>`.
4. The agent (Opus 4.8, model `claude-opus-4-8`) classifies each question — product → Kapa (`search_knowledge_base`, streaming REST per `kapa_rest_tool.py`), market/customer → SF (`db_query` snapshot for historical, `dump_sf_query` for live), company facts → `web_search` scoped to `site:acme.example.com`. Every product answer carries a Kapa source URL; every market answer carries a "Basis:" line naming the SF object + filter. Unanswerable questions are flagged `[NEEDS HUMAN INPUT]`.
5. Output: `/mnt/session/outputs/rfp_response.<ext>` (matches input shape via the `xlsx`/`docx`/`pdf` skills) plus a JSON sidecar `rfp_qa_index.json`.
6. Runner posts the agent's final `agent.message` (phone-readable summary: count answered, count flagged, top-3 flagged questions, data sources used) to the Slack thread, then uploads each output file via `_download_session_files`.
7. `_log_session_usage(trigger="slack-rfp", portco_key="acme", agent_id=RFP_RESPONDER_ID)` lands a cost row; `_archive_session` releases the container.

**Tools** (set in `agents/provision_rfp_agent.py`): `agent_toolset_20260401` (bash/read/write/edit/glob/grep/web_search/web_fetch), `search_knowledge_base` (Kapa REST streaming), `db_query`, `dump_sf_query`, `query_artifact`, `materialize_xlsx`, `reasoning_summary`. Skills: `xlsx`, `docx`, `pdf`.

**Provisioning**: `python agents/provision_rfp_agent.py` once; set `RFP_RESPONDER_ID` and `RFP_CHANNEL_ID` in `.env` and Railway. The system prompt currently lives inline in the provisioner — port to `agents/update_prompts.PROMPTS` once `RFP_RESPONDER_ID` is added as a GH secret so subsequent prompt updates flow through `.github/workflows/deploy-prompts.yml`.

**Why standalone vs. extending the Coordinator's roster**: RFP drafting is single-shot factual lookup, not iterative analytical validation — the Adversarial Reviewer + Statistician + Writing-Agent-delegation chain adds latency and cost without changing answer quality. A bad RFP prompt change cannot break the nightly Pipeline Monitor. Slack infra, vault credentials, cost tracking, and the `_stream_and_handle` custom-tool loop are reused; only the agent ID and the channel route are new.

## Cost tracking

Two-ledger architecture (see `docs/plans/35-cost-tracking-and-reporting.md`):

- **Local session-level estimator (attribution layer)**: `session_runner.py:_log_session_usage` extracts the five token categories from the session usage object, multiplies by `MODEL_COSTS_PER_MTOK`, and writes one row per session to Postgres. This is the only place that knows portco / thread / user / trigger / verbosity / agent attribution. Covers every Managed Agent session (dream, investigation, forecast, adhoc, quick_answer, recovery).
- **Anthropic Admin Usage & Cost API (ground-truth layer)**: a 06:00 Pacific cron in `cost_collector.py:pull_anthropic_daily_costs` pulls yesterday's tokens (`/v1/organizations/usage_report/messages`) and USD costs (`/v1/organizations/cost_report`) per model per workspace per service tier. Idempotent upsert by `(bucket_date, model, workspace_id, service_tier)`. Defaults to a 3-day lookback so re-runs catch late-arriving data. The Admin API cannot break down by portco/task/thread — that attribution lives only locally.

**DB tables** (in `orchestrator/migrations/00Z_cost_tracking.sql`):
- `session_costs` — per-session ledger with full attribution (`portco_key`, `channel_id`, `thread_ts`, `user_id`, `trigger`, `verbosity`, `agent_id`), token breakdown, `estimated_cost_usd`, and `raw_usage_json` for forensics. Unique index on `session_id`.
- `anthropic_daily_costs` — daily ground-truth rollup keyed by `(bucket_date, model, workspace_id, service_tier)`.
- `messages_api_calls` — parallel ledger for non-session traffic (`self_heal`, `self_improve`). Wrapped via `cost_collector.track_messages_call(caller, response, model, portco_key)` around every `client.messages.create()`.
- `cost_rollup_daily` view — per-day, per-portco, per-task-type aggregation used by `/cost` and the digest.

**Reconciliation**: daily job compares `SUM(estimated_cost_usd)` from `session_costs` to `anthropic_daily_costs.cost_usd` for the same day. `drift_pct = (actual - estimated) / actual`. If `|drift_pct| > 10%`, post a Slack watch notice (deduped once/day). If `> 25%`, log a recommended `MODEL_COSTS_PER_MTOK` refresh — pricing-table drift is the most likely cause.

**Reporting surfaces**:
- `/cost` Slack slash command — `/cost [scope] [window]`. Supports `today | week | month [portco]` and `/cost reconcile` for the local-vs-actual drift view. Renders a table in the channel within 3s for windows up to 30 days. Requires Slack app `commands` scope.
- Daily DM digest — 08:00 Pacific cron in `cost_digest.send_daily_cost_digest()` posts to each admin user with yesterday's total, by-portco split, by-task split, cache hit rate, drift line, and top-5 sessions. Leads with the drift watch line if `|drift_pct| > 10%`.
- **Persistent surface integration**: `SurfaceState.cost_block` renders trailing 7d/30d totals, trend, top task, and cache hit % in the Canvas under "Operating Cost." Reads from `cost_rollup_daily` view (Plan #35).

**Env var**: `ANTHROPIC_ADMIN_KEY` on Railway (Admin API keys can only be minted by org admins, separate from `ANTHROPIC_API_KEY`). Without it the local ledger and `/cost` still work; only reconciliation degrades gracefully.

## Prompt compression

Compresr (YC W26) SDK integration cuts input-token spend on the two Messages-API call sites that carry large compressible payloads (see `docs/plans/37-compresr-integration.md`).

**Call sites**:
- `self_improve._analyze_changes` — uses `espresso_v1` (general-purpose, no query) on the concatenated doc-page payload (~15K chars after the 2026-05-14 Anthropic docs reshuffle; was ~100 KB at Plan #37 sizing). `min_chars=12000` (lowered from 20000 on 2026-05-14 because the original threshold always tripped the `below_min_chars` fallback).
- `self_heal._analyze_session` — uses `latte_v1` (query-aware) on the session-summary JSON, with `query=f"Review session {session_id} for tool errors and code fixes"`. Query-aware compression preserves error fields. `min_chars=8000`.
- Tier B opt-in: `session_runner.run_adhoc_mcp_session` kickoff text, gated by `COMPRESS_ADHOC_KICKOFF` (default `false`). Only compresses if the kickoff exceeds 4 KB (e.g. user pasted a CSV or report). Latte_v1 with the user question as the query.

Managed Agents server-side system prompts and inter-turn agent reasoning are **not addressable** — those live on Anthropic's servers after `setup_agents.py` provisions them.

**Caching**: `compresr_cache` table keyed by `sha256(text || model || (query or ''))` with a 7-day TTL. `_analyze_changes` runs nightly on near-identical doc pages, so cache-hit rate is expected >50% week-over-week.

**Fallback**: `compress_prompt` silently returns the original text on any failure (missing key, timeout, 4xx/5xx, circuit-breaker tripped). Compression failure never breaks the calling code. If fallback rate exceeds 25% over 24h, the daily digest emits a watch notice.

**Per-site kill switch**: a quality-regression guard tracks downstream JSON parse failure rate from `_analyze_session` and `_analyze_changes`. If the rate exceeds 2x the prior 14-day baseline, compression auto-disables for that call site and Slack-notifies admins. Global kill via `COMPRESSION_ENABLED=false` (read at call time, no restart).

**Env var**: `COMPRESR_API_KEY` on Railway (NOT local). Format `cmp_*`. Rotate the key after any local dev use — local exposure should not persist into the Railway secret. Loaded by the existing manual dotenv parser in `config.py`.

## Kapa — Acme internal knowledge base

Kapa exposes a streaming REST API at `https://api.kapa.ai/query/v1/projects/<project_id>/chat/stream/`, consumed via the `search_knowledge_base` custom tool implemented in `orchestrator/kapa_rest_tool.py`. The agent-facing tool name is preserved from the retired MCP integration so existing prompts work unchanged. The Acme index covers:

- **Internal Confluence wiki** (`acme.atlassian.example/wiki`) — DEVOPS, PE, DPD, AGILE, AF, CS spaces. Release notes, Commerce GTM meeting notes, PM handover docs, DevOps onboarding, GitLab repository standards, glossaries, "After-hours Work Updates."
- **Jira issues** (`acme.atlassian.example/browse/...`) — ENG (Engineering) and SE (Support Escalations) projects. Issue bodies, comments, status, resolution. Jira issues often embed the originating Salesforce Case URL.
- **Public help docs** (`help.acme.example.com/advanced`) — customer-facing FAQs, feature articles, integration catalog.
- **Slack archive** (`acme.slack.example`) — limited threads.
- **Integration partner docs** (`docs.partnera.ai`, `support.partnerb.com`).

See `docs/research/kapa-acme-index.md` for the full index map.

### Which agents have Kapa access

| Agent | Access | Why |
|---|---|---|
| **Coordinator** | yes | Synthesis layer for product/initiative context in reports |
| **Quick Answer** | yes | Single-fact Slack lookups ("what is FATI?", "what integrates with Acme?") |
| **Dream Agent** | yes | Hypothesis generation seeded by recent product/GTM changes |
| **Post-Sales Monitor** | yes | Investigating retention shifts; cross-references product/Jira context |
| **Cross-Domain Synthesizer** | yes | Connects revenue-side patterns with product-side events (the SFDC↔Jira pattern) |
| Pipeline Monitor | no | Lead-flow domain; Kapa adds little signal here |
| Sales Process Monitor | no | Opp-flow domain; Kapa adds little signal here |
| Statistician | no | Pure math layer |
| Adversarial Reviewer | no | Challenges numbers; product context off the critical path |
| Chart Designer | no | Renderer |
| Writing Agent | no | Prose composition |

### Auth model — API key on X-API-KEY header

`KAPA_ACME_API_KEY` env var holds the Bearer token minted from the Kapa Acme tenant after browser SSO. `orchestrator/kapa_rest_tool.py` puts it in the `X-API-KEY` header on every request. No vault, no MCP server setup — Kapa's hosted MCP server requires OAuth with dynamic client registration, and Kapa support (2026-05-14) confirmed they will not provide machine-to-machine OAuth client credentials, so the MCP path is permanently closed for our headless runtime.

### Rate limit, behavior, scope

- **20 requests/minute** per API key for the Chat endpoint (Kapa server-side cap). 60 req/min is the Retrieval endpoint — a common doc-reading slip we corrected 2026-05-14 after Kapa Support confirmed in case-7326. Agent prompts say "do not loop calls."
- Queries must be complete natural-language sentences, not keyword lists.
- Returns markdown chunks (≤35K chars per call default; configurable via `_meta.max_chars`).
- The custom tool dispatcher auto-approves at the orchestrator level — read-only knowledge search doesn't sit on the `requires_action` confirmation loop.

**Orchestrator-side rate limiter (REST path only)**: `orchestrator/kapa_rate_limiter.py` enforces a 16 req/min token bucket on REST-path calls via `kapa_rest_tool.py`. Env vars: `KAPA_RATE_LIMIT_TOKENS_PER_MIN` (default 16), `KAPA_RATE_LIMIT_ENABLED` (default true). The MCP-path agents (Coordinator, Quick Answer, Dream Agent, Post-Sales Monitor, Cross-Domain Synthesizer) still go direct — Layer 3 (planned in a separate doc) will catch them via an MCP proxy.

### SFDC ↔ Jira synthesis pattern

Acme's support pipeline is Salesforce Case → JSM (Jira Service Management) → Jira (project keys `ENG` and `SE`). Cases link to Jira issues; Jira issues often embed the SF Case URL in the body. Because Kapa indexes Jira directly, Post-Sales Monitor and Cross-Domain Synthesizer can pull the engineering-side disposition for a customer issue with a single Kapa query — no separate Jira MCP needed today. Worked example lives in `_KAPA_KNOWLEDGE_BLOCK` in `agents/update_prompts.py`.

## SOQL Constraints

SOQL does not support: CASE, COALESCE, FLOOR, subqueries in SELECT. No column aliases in ORDER BY — must use the aggregate function. These are encoded in the query planner system prompt and in `_fix_soql`.

**Long-text-area, Rich-text-area, and Text-area(>255) fields** cannot be:
- Used in `WHERE` filters — neither `field != null` nor `field LIKE '%x%'`. Error: `INVALID_FIELD: <field> can not be filtered in a query call`.
- Used in aggregate functions — `COUNT(<field>)` fails with `MALFORMED_QUERY: field <field> does not support aggregate operator COUNT`.
- Workaround for free-text search: SELECT the field unfiltered into a Parquet (scoped by indexed columns like `Id`, `CreatedDate`, `StageName`), then use `query_artifact` with DuckDB `regexp_matches` for keyword scans, or — for small universes (<5,000 rows) — dispatch per-row LLM categorization. Live incident 2026-05-16 (inv 49): SOQL rejected `Closed_Lost_Notes__c LIKE '%ui%'`; sub-agent had to pivot to SELECT-then-DuckDB pattern.

## Environment Variables

All required vars are in `.env.example`. Key IDs come from running `setup_agents.py`: `ENVIRONMENT_ID`, `DREAM_AGENT_ID`, `COORDINATOR_ID`, `METHODOLOGY_STORE_ID`, `HEALTH_STORE_ID`. Slack needs `SLACK_BOT_TOKEN` (xoxb), `SLACK_APP_TOKEN` (xapp), `SLACK_CHANNEL_ID`.

Self-heal pipeline env vars (B-track, 2026-05-12):
- `RECOVERY_FRESH_THRESHOLD` — input-side tokens above which interrupted-investigation recovery archives the old session and starts fresh instead of resuming. Default 500_000. Lower it to be more aggressive about discarding bloated context.
- `RESULT_VIRTUALIZE_THRESHOLD` — list-shaped tool result rows above which the orchestrator streams to .xlsx and hands the model a compact handle instead of the raw rows. Default 50.
- `SLACK_ADMIN_USER_IDS` — comma-separated Slack user IDs. Catastrophic-failure messages (`send_notification(admin_only=True)`) DM these users instead of polluting the public channel. Set to `U0000000000` on Railway.

Kapa env vars (Kapa integration, 2026-05-13):
- `KAPA_ACME_API_KEY` — Bearer token minted from the Kapa Acme tenant after browser SSO. Read at session runtime by `orchestrator/kapa_rest_tool.py` (called from `_dispatch_tool` at `session_runner.py:1364`); attached as the `X-API-KEY` header on every request. When unset, the dispatcher returns a structured error and agent prompts treat it as "knowledge base unavailable, proceed without"; the SF data path is unaffected.
- `KAPA_ACME_PROJECT_ID` — Kapa project UUID for the Acme Internal tenant. Combined with the API key by `orchestrator/kapa_rest_tool.py` to build the REST URL.

## /health endpoint and BUILD_COMMIT

The orchestrator exposes `GET /health` on `PORT` (defaults to 8080). Body:

```json
{
  "build_commit": "<value of BUILD_COMMIT env at container start>",
  "deploy_started_at": "<ISO8601 of process start>",
  "active_versions": {<contents of agents/active_versions.json>},
  "status": "ok"
}
```

`BUILD_COMMIT` is injected at Docker build time via `ARG BUILD_COMMIT` in the Dockerfile. To set it correctly per environment:

- **Local docker build**: `docker build --build-arg BUILD_COMMIT=$(git rev-parse HEAD) -t gtm-health-agent .`
- **GitHub Actions** (any workflow that builds the image): pass `--build-arg BUILD_COMMIT=${{ github.sha }}` to the build step.
- **Railway**: Railway's Docker builder does not pass git SHA automatically. Set `BUILD_COMMIT` as a *build-time* variable in the Railway service settings (Variables → Build), or modify `railway.toml` to add `[build] buildArgs = { BUILD_COMMIT = "${{ ci.git_sha }}" }`. The Dockerfile also defaults `BUILD_COMMIT` from `RAILWAY_GIT_COMMIT_SHA` via BuildKit ARG-from-ARG so Railway builds get the right SHA with zero additional plumbing. If neither is wired the live container reports `build_commit: "unknown"`, which is the correct loud-fail signal that the verification step still needs to be set up.

Z2 deploy verification (Track Z of the misty-squishing-badger plan) curls `/health` and asserts `build_commit` matches `git rev-parse main` before declaring the deploy green.

Verified on 2026-05-14: `/health` `build_commit` plumbing works end-to-end on prod (`https://your-app.up.railway.app/health` returned a real SHA — `8ddb97b...` — not `unknown`). On that day the SHA also lagged `origin/main` because auto-deploy is OFF per `railway.toml` D3 — the deploy workflow had not yet shipped the most recent merge. This does NOT mean future mismatches are safe to dismiss: Z2 verification compares against the SHA the deploy step just shipped (`gh run view` of the deploy workflow), not raw `origin/main`. A `build_commit` that disagrees with the just-shipped SHA — or is `unknown` — is still a hard fail and must block green status.

## Multi-Portco

`portco_config.json` maps each company to data sources, Slack channel, and metadata. Currently only Acme is active. Portco isolation: each company gets its own Slack channel and memory store subdirectory. Channel-to-portco lookup in `portco_registry.py`. Platform priority ranking governs cross-source precedence (Salesforce 100, Zoho 90, HubSpot 80, etc.).

**SF custom-field expectations**: any portco wiring Salesforce as its CRM source needs the following four custom fields on the `Lead` object so the nightly Postgres sync writes complete rows (introduced by `fix/lead-sync-schema`):
- `Discovery_Call_Booked__c` (TIMESTAMPTZ in Postgres; flip to BOOLEAN in `00AA_lead_discovery_call_booked.sql` if the org defines it as a checkbox)
- `Funnel_Stage__c`
- `MQL_SDR_Accepted_Date_Time__c`
- `SDR_Qualified_Date_Time__c`

If a portco's SF org lacks any of the four, `r.get(...)` returns `None` and the sync writes NULL for that column rather than crashing — the Lead row still lands. The operator handling: leave the field missing if the portco's GTM motion has no equivalent stage (queries that filter on the column will simply return zero rows for that portco), or land the field in SF and re-run `bin/backfill_lead_sync_fields.py --portco <key>`.

On the `Opportunity` object, the nightly sync also picks up one optional custom field (introduced by `feat/sync-product-line`, 2026-05-19):
- `Product_Line__c` — single-string product line per opp. Lands in `opportunities.product_line` so cross-cuts like `Industry × Product Line` run from Postgres instead of paying a live SF MCP query per analysis. Multi-line-item disambiguation rule: **none** — we deliberately use the flat single-string Opportunity field rather than joining `OpportunityLineItem.ProductFamily__c`, to keep the sync one row per opp. A portco that needs line-item-level granularity should ship a separate `opportunity_line_items` table, not overload this column. Backfill date: snapshot #15 (next nightly run after migration `00AQ_opp_product_line.sql` lands); snapshot #14 and earlier stay NULL. Operators who need historical fill run `bin/backfill_opportunity_product_line.py --portco <key>`. Filtered through `_build_select_clause` against describeSObject, so portcos without the field write NULL instead of failing the whole Opportunity sync (same pattern as the four Lead custom fields above).

The Slack app's OAuth scopes are now declarative in `manifest.yaml` at the repo root. Canvas surface scopes (`canvases:read`, `canvases:write`, `channels:manage`) granted on the live bot token by PR #59 are permanent in source as of Plan #33 F3, alongside `pins:write` (pre-granted for the optional pinned-headline tier — currently unused but reserved to avoid a second reinstall round). See `docs/slack/scopes-changelog.md` for the per-scope rationale and the reinstall workflow that runs after any manifest scope change.

## Nightly Pipeline

Scheduled via APScheduler cron (all times Pacific). All user-facing daily
Slack-posting crons were retired 2026-05-14 pending JTBD redefinition —
the underlying functions remain importable so a future alert-based or
on-demand replacement can call them without re-implementation.

Active crons (silent unless something is wrong):
- 1am: DB sync (SF snapshot to Postgres) — silent on success
- 4am: compresr cache expiry — 7-day TTL sweep
- 6am: Anthropic Admin API daily cost pull (when ANTHROPIC_ADMIN_KEY set)
- 6am: session_thread_events 30-day TTL purge
- 6:15am: hot-window raw-row purge — snapshot retention Tier 3, gated on rollup + archive (incident 2026-06-16)
- 8am: surface refresh — Canvas push for every active portco
- every 15min: batch poll (when BATCH_PROCESSING_ENABLED)
- every 30s: session-size canary (log-only, no Slack output)
- hourly: batch flush / orphan recovery (when BATCH_PROCESSING_ENABLED)

Retired 2026-05-14 (functions still defined; no scheduler registration):
- Midnight self-improvement (`scheduled_self_improve`)
- 3am forecast analysis (`scheduled_forecast`)
- 5am dream → investigation (`scheduled_dream`)
- 7am cost reconciliation (`scheduled_reconcile_costs`)
- 8am cost-digest DM (`scheduled_cost_digest`)

`RUN_NIGHTLY_NOW` env var still triggers `run_full_nightly_pipeline` 2 minutes
after startup (self-improve → DB sync → forecast → dream → investigation),
then auto-clears. This is the on-demand path for testing whatever the JTBD
discussion decides should come back. All scheduler job failures still log
+ post a watch notice; APScheduler event listener catches missed jobs.

## Production Deploys / Break Glass

<!-- Plan #42 PR1 (trimmed) — measurement + runbook index landed here.
     Business-hours deploy freeze + delayed-prod-deploy cron were
     intentionally cut from PR1; can be re-introduced later if the
     measurement loop shows a workday-incident correlation. -->

- **Bad prompt landed?** `python bin/rollback-agent.py <agent_short_name> --to-version <N>` rolls back a single agent. The full deploy-rollback wrapper ships in Plan #42 PR3.
- **Runbook index**: [`docs/runbooks/README.md`](docs/runbooks/README.md) — single discovery point at 2 AM. Decision tree by symptom, links to every focused runbook.
- **Measurement loop**: `bin/measure-deploy-risk.py` runs monthly on the 1st at 09:00 PT (`.github/workflows/measure-deploy-risk.yml`). Output is a 3-sheet .xlsx (sessions by hour, error rate by hour, deploys-vs-incidents) DMed to admins. The `session_costs.outcome` column (added by `00AB_session_costs_outcome.sql`) is the data source for the error-rate tab. Re-run this monthly; if a workday error-rate cluster appears, this is the evidence to re-introduce the business-hours freeze (PR1 v1).
- **Manual deploy**: `bin/deploy.sh` from a fresh checkout on `main`. The script (a) refuses dirty trees, (b) refuses non-main branches without `--allow-non-main`, (c) sets `BUILD_COMMIT=$(git rev-parse HEAD)` as a Railway service variable via `railway variables --set ... --skip-deploys` BEFORE running `railway up`, so the live container's `/health` reports the actual built SHA. **Do NOT run `railway up` directly** — Railway's auto-injected `RAILWAY_GIT_COMMIT_SHA` only populates on GitHub-triggered builds (which we don't use; auto-deploy is OFF), so bare `railway up` ships with whatever stale SHA Railway last saw. Observed 2026-05-15 on the SSE-auto-reconnect deploy: `railway up` succeeded but `/health` reported the prior pin-deploy SHA, defeating Z2 verification. Smoke probe (PR2) gates promotion regardless of trigger.

## Prompt deploys

A `.github/workflows/deploy-prompts.yml` workflow auto-runs `update_prompts.py` whenever a merge to main touches `agents/setup_agents.py` or `agents/update_prompts.py`. Required GH secrets: `ANTHROPIC_API_KEY` + `COORDINATOR_ID` / `QUICK_ANSWER_ID` / `DREAM_AGENT_ID` / `PIPELINE_MONITOR_ID` / `SALES_MONITOR_ID` / `POSTSALES_MONITOR_ID` / `STATISTICIAN_ID` / `CHART_DESIGNER_ID` / `ADVERSARIAL_REVIEWER_ID` / `CROSS_DOMAIN_SYNTHESIZER_ID` / `WRITING_AGENT_ID`. (The legacy `REPORT_WRITER_ID` secret was dropped 2026-05-11 — agent superseded by Writing Agent.) If the secrets are missing the workflow fails loud — no silent skip. Closes the deploy gap from 2026-05-11 where PR #37 shipped a Coordinator prompt change at 14:16 PT and a 14:44 PT session picked up the stale v20 prompt ($47 wasted on a reproduced context blowup).

## Prompt-deploy gate

Plan #42 PR3 adds a two-part safety net around the prompt-deploy workflow above:

**Label gate.** Every push to `main` that touches a prompt source file (`agents/setup_agents.py`, `agents/update_prompts.py`, `orchestrator/writing_agent.py`, `agents/update_subagent_tools.py`) must originate from a merged PR carrying the `prompt-author-verified` label. The deploy workflow looks up the merged PR via `gh pr list --search "$GITHUB_SHA" --state merged --json labels,author --limit 1` and fails loud if the label is absent. `workflow_dispatch` runs (manual operator triggers) skip the gate.

Honest framing: this is a tripwire for forgetfulness, NOT a security control. The same dev who sets the label is the dev who merges the PR — there is no second party. The label exists so the click-through cost makes the author re-read the prompt diff one more time before it hits Anthropic. Renamed from `prompt-verified` (D10) to make the limit explicit in the name.

**Artifact bracket.** The workflow uploads `agents/active_versions.json` as a workflow artifact named `pre_deploy_versions` BEFORE calling `update_prompts.py`, and again as `post_deploy_versions` AFTER. The pair forms the source-of-truth rollback target — reading `HEAD~1` is unreliable because the workflow auto-commits pin updates and other commits can sit between deploys (D9). Artifacts retain for 90 days.

**Rollback.** If a fresh deploy regresses production behavior:

```bash
python bin/rollback-deploy.py --artifact-run <gh_run_id> --apply
```

The wrapper downloads `pre_deploy_versions` from the named workflow run, diffs against the current pin file, and invokes `bin/rollback-agent.py` once per changed agent (D8: reuse the existing per-agent script, don't duplicate the SDK dance). Dry-run is the default; `--apply` is the explicit go. Recovery is ~30s per agent. Full procedure in `docs/runbook-prompt-rollback.md`.

## Multi-agent orchestration

Multi-agent is enabled (Anthropic Managed Agents API, beta `managed-agents-2026-04-01`). The Coordinator's `multiagent.agents` roster is populated with 8 sub-agents in production: Pipeline Monitor, Sales Process Monitor, Post-Sales Monitor, Statistician, Adversarial Reviewer, Cross-Domain Synthesizer, Chart Designer, Writing Agent. (Prompt Engineer is NOT in the roster — it preprocesses Slack questions BEFORE the Coordinator session exists, so the Coordinator can't delegate to it. RFP Responder, RFP Reviewer, Watcher Agent, Quick Answer, and Dream Agent are also out of the roster — see `agents/update_coordinator_roster.py` for the live ROSTER constant and the audit rationale committed 2026-05-27.) The 4 validation/synthesis/chart agents were activated 2026-05-11 in PR `chore/dead-agent-cleanup` — they had been provisioned on Anthropic but never wired into the roster, so the validation pipeline the Coordinator prompt describes never actually ran end-to-end before that date. The Writing Agent was added 2026-05-27, replacing the `write_prose` custom-tool dispatch path with multiagent delegation. Each sub-agent owns its own configuration — `tools`, `mcp_servers`, and `system` prompt — per the docs: *"Each agent uses its own configuration (model, system prompt, tools, MCP servers, and skills) as defined when that agent was created. Tools and context are not shared."*

Known runtime pitfall (session `sesn_EXAMPLE`, 2026-05-11): a sub-agent ran 15 filesystem diagnostics (`which sfdx`, `find / -name "*salesforce*"`, `ls /var/run/`, etc.) trying to verify MCP access and concluded BLOCKED, even though its agent definition correctly listed the Salesforce `mcp_toolset`. MCP tools are exposed via the agent's tool registry — not as local binaries, sockets, or daemons. Specialist prompts must instruct the agent to verify access by **attempting a trivial call** (e.g. `soqlQuery({"q": "SELECT Id FROM Account LIMIT 1"})`), not by inspecting the filesystem.

## Outcomes / rubric-based grading

Outcomes (rubric-based grading) requested 2026-05-06, not yet enabled. Until then, rubrics are reference-only.

## Toolset versioning — `agent_toolset_20260401`

Every agent's tools[] includes the built-in `agent_toolset_20260401` entry (Python + files + bash). The date suffix matches the beta header `managed-agents-2026-04-01`: it is the contract version, NOT a per-agent snapshot. The tools available behind this entry can FLOAT within the dated contract — Anthropic may add fields, extend descriptions, or relax constraints without changing the ID. Breaking changes ship as a NEW dated toolset ID (e.g. `agent_toolset_2026XXXX`); we then bump our agent definitions to point at the new ID after vetting.

**What this means operationally**:
- Our `setup_agents.py` and `update_subagent_tools.py` pin `agent_toolset_20260401` once and rely on the contract not breaking under us.
- Tool schema/description drift IS possible within the contract. The `agent_toolset_drift_canary` workflow (weekly, see `.github/workflows/toolset-drift-canary.yml`) catches it: snapshots every agent's `tools[*]` payload via `GET /v1/agents/{id}`, diffs against the most recent prior snapshot under `agents/toolset-snapshots/`, and fails the run + admin-DMs on any drift without a corresponding agent update.
- On a drift alert: review the diff, decide whether the new shape is safe to inherit (usually yes) or needs prompt updates to match (rare). The script `bin/audit-toolset-drift.py` is the same logic available on-demand for ad-hoc checks.
- See `docs/runbooks/managed-agents-conformance.md` for the operator workflow on each failure mode.

## Conformance audits — orphan MCP toolsets

Two read-only audit scripts run alongside the deploy pipeline:

- `bin/audit-mcp-toolsets.py` — flags any agent whose `tools[]` still includes an orphan `mcp_toolset` entry. Iteration 3 removed the Salesforce MCP toolset from every sub-agent, but the auto-approve path in `session_runner.py` still tolerates such entries. Re-run after any sub-agent provisioning round. See `docs/runbooks/managed-agents-conformance.md`.
- `bin/audit-toolset-drift.py` — the drift canary referenced above. Designed to run weekly via GitHub Actions or cron.

Both exit 0 on clean state, 1 on findings; both write a clear "Next steps" block to stdout.

## Portco-identifier scrub (pre-public-flip gate)

`bin/scrub-portco.py` scans the repo for portco-specific identifiers — Slack IDs, Anthropic agent/session/vault IDs, vendor names, deployment URLs, etc. — that must not ship in a public OSS distribution. Patterns live in `bin/scrub-portco-patterns.yml` with three severity tiers:

- `HIGH` — blocks the public flip (Slack/SF/Anthropic IDs, portco names, vendor relationships, emails, deployment URLs).
- `MEDIUM` — review before flip (incident references, internal PR numbers).
- `LOW` — informational only.

```bash
python bin/scrub-portco.py                       # scan repo root, human-readable
python bin/scrub-portco.py --json                # JSON for tooling
python bin/scrub-portco.py --severity HIGH       # HIGH only
python bin/scrub-portco.py --root path/to/dir    # scan a subdir
```

Exit code is `1` if any HIGH finding remains after the allowlist, `0` otherwise. When adding a pattern, drop a fixture into `bin/scrub-portco-fixtures/` that exercises it and re-run against that subtree to verify the pattern fires. Designed to run pre-public-flip and as a CI gate after flip.

## SSE reconnect budget vs watchdog tier timing

The orchestrator's SSE event stream and the session watchdog race when the Coordinator stalls mid-flow (e.g. after dispatching a sub-agent and going quiet). Constants in `orchestrator/session_runner.py` and `orchestrator/session_watchdog.py` are sized so the watchdog wins:

- **Watchdog Tier 1** (gentle nudge — inject a user.message asking Coordinator to ship results or re-dispatch) fires at `STALL_THRESHOLD_SECONDS` (600s) + `WATCHDOG_POLL_SECONDS` (60s) ≈ **11 min**.
- **Watchdog Tier 2** (interrupt non-primary sub-threads via `user.interrupt`) fires at Tier 1 + `WATCHDOG_TIER_ESCALATION_SECONDS` (120s) ≈ **13 min**.
- **Watchdog Tier 3** (mark investigation failed + archive session + admin DM) fires at Tier 2 + 120s ≈ **15 min**.
- **SSE budget exhaustion** (gives up streaming, raises `ReadTimeout` to lifecycle guard) fires at `SSE_MAX_RECONNECT_ATTEMPTS` (7) × `SSE_READ_TIMEOUT_S` (120s) + backoff (~90s) ≈ **15.5 min**.

Before the 2026-05-19 bump, `SSE_MAX_RECONNECT_ATTEMPTS` was 5 and the budget was ~11 min — the watchdog tied or lost the race, and stranded Coordinator sessions terminalized as ❌ before any tier ran. Live repro: sub3 inv 58 (`sesn_EXAMPLE`) on 2026-05-19 04:48:41 UTC. If you ever need to lower SSE_MAX_RECONNECT_ATTEMPTS below 6, you re-introduce the race; if you raise STALL_THRESHOLD_SECONDS above 700s, same problem. The assertion lives in `plan_47_workstream_a_test.py::test_sse_max_reconnect_attempts_sized_above_watchdog_tier_3`.

The 400-on-follow-up sentinel (`_FollowupBlocked`) is independent of this race and covers BOTH `events.send()` AND `events.stream()` context-manager entry (Plan #47 Workstream A + A.2). A 400 from either site in a thread-follow-up is converted to a polite Slack reply instead of ❌, as long as `_is_requires_action_400()` matches the error body.
