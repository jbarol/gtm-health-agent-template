# Configuration reference

Everything you set to run GTM Health Agent: environment variables, the
`portco_config.json` schema, the Salesforce custom fields the nightly sync
expects, and which Claude model each agent runs on.

The orchestrator reads configuration from two places:

1. **Environment variables** - secrets, agent IDs, feature flags, schedules.
   Loaded from `.env` locally and from Railway service variables in production.
   Copy `.env.example` to `.env` and fill it in.
2. **`portco_config.json`** - the per-tenant map (data sources, Slack channel,
   metadata). Copy `portco_config.example.json` to `portco_config.json` and
   edit. The real file is gitignored; only the `.example` ships.

Throughout this doc, `PORTCO` in an env var name is the **uppercased portco
key** from `portco_config.json` (for the example portco `acme`, the key is
`ACME`).

## 1. Environment variables

### Required

The orchestrator refuses to start, or silently degrades a core path, without
these.

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key (`sk-ant-...`). Authenticates every Managed Agents session. Mint at console.anthropic.com. |
| `ENVIRONMENT_ID` | Managed Agents cloud environment ID (`env_...`). Printed by `python agents/setup_agents.py`. Scopes the toolset, memory stores, and network policy. |
| `COORDINATOR_ID` | Agent ID (`agent_...`) for the Coordinator (Tier 2 orchestrator). Printed by `setup_agents.py`. |
| `QUICK_AGENT_ID` | Agent ID for Quick Answer (single-fact Slack lookups). The legacy name `QUICK_ANSWER_ID` is accepted as a fallback; set whichever your environment already uses. |
| `DREAM_AGENT_ID` | Agent ID for the Dream Agent (scheduled proactive analysis). |
| `PIPELINE_MONITOR_ID` | Agent ID for the Pipeline Monitor specialist. |
| `SALES_MONITOR_ID` | Agent ID for the Sales Process Monitor specialist. |
| `POSTSALES_MONITOR_ID` | Agent ID for the Post-Sales Monitor specialist. |
| `STATISTICIAN_ID` | Agent ID for the Statistician specialist. |
| `CHART_DESIGNER_ID` | Agent ID for the Chart Designer specialist. |
| `ADVERSARIAL_REVIEWER_ID` | Agent ID for the Adversarial Reviewer specialist. |
| `CROSS_DOMAIN_SYNTHESIZER_ID` | Agent ID for the Cross-Domain Synthesizer specialist. |
| `METHODOLOGY_STORE_ID` | Memory store ID (`memstore_...`) for the read-only Methodology store. Printed by `setup_agents.py`. |
| `HEALTH_STORE_ID` | Memory store ID for the read-write Health store (per-portco operational state). |
| `SLACK_BOT_TOKEN` | Slack bot token (`xoxb-...`). Posts messages, reads channels. From the installed Slack app. |
| `SLACK_APP_TOKEN` | Slack app-level token (`xapp-...`) for Socket Mode. The orchestrator opens a Socket Mode connection, not a public webhook. |
| `SLACK_CHANNEL_ID` | Default Slack channel ID (`C...`) the bot posts to when no portco channel resolves. |
| `DATABASE_URL` | PostgreSQL connection string (`postgresql://...`). Injected automatically by the Railway Postgres add-on. Schema auto-bootstraps at boot via `db_adapter.ensure_schema` plus migrations. |

The Writing Agent is created by `setup_agents.py` (printed as `WRITING_AGENT_ID`;
`agents/provision_writing_agent.py` remains for rotation/recovery). The Prompt
Engineer is minted by its own script (`agents/provision_prompt_engineer.py`) and
set via `PROMPT_ENGINEER_ID`. They are Optional below
because the system degrades gracefully when they are unset, but you want them
for production-quality output.

### Optional

Unset means the associated feature is off or runs in degraded mode. Nothing
here blocks startup.

| Variable | Description |
|---|---|
| `WRITING_AGENT_ID` | Agent ID for the Writing Agent (Haiku 4.5 prose composer). When unset, the system falls through to a renderer plus prose-polish safety net with no Haiku-quality prose. |
| `PROMPT_ENGINEER_ID` | Agent ID for the Prompt Engineer (pre-flight question refinement). When unset, the Coordinator runs against the raw user question. |
| `RFP_RESPONDER_ID` | Agent ID for the RFP Responder (out-of-roster). When unset (or `RFP_CHANNEL_ID` unset), file uploads are ignored and the RFP path is disabled. |
| `RFP_REVIEWER_ID` | Agent ID for the RFP Reviewer quality gate. When unset, the review tool returns a soft pass and the draft still ships. |
| `RFP_CHANNEL_ID` | Slack channel ID for RFP intake. A `file_share` event in this channel routes to the RFP pipeline instead of the question pipeline. |
| `WATCHER_ENABLED` | Master switch for the Watcher subsystem (scans for error patterns, opens draft fix-PRs). Default off. |
| `WATCHER_AGENT_ID` | Agent ID for the Watcher (out-of-roster). Printed by `python agents/provision_watcher_agent.py`. Required alongside `WATCHER_ENABLED=true`; when unset the Watcher worker no-ops. |
| `WATCHER_GH_TOKEN` | GitHub fine-grained PAT scoped to your fork. The Watcher uses it to open draft PRs. Without it the Watcher can detect but not file. |
| `ANTHROPIC_ADMIN_KEY` | Anthropic Admin API key (`sk-ant-admin-...`). Powers the daily cost reconciliation pull. When unset, cost reporting falls back to local estimates. |
| `COMPRESR_API_KEY` | Compresr prompt-compression key (`cmp_...`). When unset but any `COMPRESS_*` flag is true, that site is force-disabled with a warning. |
| `COMPRESS_SELF_HEAL_ENABLED` | Compress the self-heal prompt. Default `false`. |
| `COMPRESS_SELF_IMPROVE_ENABLED` | Compress the self-improve prompt. Default `false`. |
| `COMPRESS_ADHOC_KICKOFF` | Compress the ad-hoc kickoff prompt. Default `false`. |
| `BATCH_PROCESSING_ENABLED` | Route self-heal / self-improve through the Anthropic Batches API (50% cheaper, async). Default `false` (realtime). |
| `CHANNEL_DESC_PUSH_ENABLED` | Allow event- and cron-driven Slack channel-purpose updates. Default `true`. Read at call time, so flipping it takes effect without a restart. |
| `STOP_COMMAND_ENABLED` | Kill switch for the `/stop` slash command. Default `true`. |
| `SMOKE_PROBE_ENABLED` | Block `/ready=200` until the pre-deploy smoke probe passes. Default `true`. Set `false` to bypass during an incident. |
| `SMOKE_PROBE_LEVEL` | How hard the probe exercises live dependencies: `off`, `quick` (default), or `full` (adds a Coordinator multiagent turn). |
| `MCP_AUTO_APPROVE_ALLOWLIST` | Comma-separated MCP server names allowed to auto-approve when an agent emits an `ask`-permission tool call. Empty by default. |
| `BUILD_COMMIT` | Git SHA baked at Docker build (`--build-arg BUILD_COMMIT=$(git rev-parse HEAD)`). Surfaced on `/health` and checked by the smoke probe. |
| `PORT` | HTTP port for the health server (`/health`, `/ready`, webhook handler). Railway injects this; default falls back if unset. |
| `SESSION_OUTPUT_DIR` | Filesystem path for per-session sandbox outputs (Parquet, charts). Treated as ephemeral. |
| `DREAM_SCHEDULE_CRON` | Cron for the Dream Agent proactive run. Default `0 20 * * 0` (Sunday 20:00). Interpreted in `TIMEZONE`. |
| `INVESTIGATION_SCHEDULE_CRON` | Cron for the scheduled investigation. Default `0 9 * * 1` (Monday 09:00). |
| `TIMEZONE` | IANA timezone for all cron expressions. Default `America/Los_Angeles`. |
| `SLACK_NOTIFY_USER_IDS` | Comma-separated Slack user IDs DM'd on admin alerts (degraded mode, watcher findings). |
| `SLACK_ADMIN_USER_IDS` | Comma-separated Slack user IDs treated as admins: they gate privileged slash commands and receive admin-only DMs (catastrophic-failure notices, cost digests, drift watch lines). Leave empty to disable admin DMs. |
| `ANTHROPIC_WEBHOOK_SIGNING_KEY` | HMAC signing key for the `/webhooks/anthropic` handler. When unset, the handler rejects every request with 400. |
| `ANTHROPIC_WEBHOOK_URL` | Public URL the Anthropic webhook subscription points at (your Railway URL plus `/webhooks/anthropic`). |
| `RAW_HOT_WINDOW_DAYS` | Days the bulky raw child rows of a snapshot stay hot in Postgres before the archive-gated purge (Tier 3) drops them. Default `60`. The `snapshots` metadata row and `daily_metrics` are kept forever regardless. |
| `ARCHIVE_BUCKET_ENABLED` | Master switch for the Parquet cold archive (snapshot retention Tier 2). Default `false`. When off (or any `ARCHIVE_S3_*` below is unset), archiving is a no-op and the purge falls back to a rollup-only guarantee. |
| `ARCHIVE_S3_ENDPOINT` | S3-compatible endpoint URL for the archive bucket (e.g. a Railway object-storage bucket). Required when `ARCHIVE_BUCKET_ENABLED=true`. |
| `ARCHIVE_S3_BUCKET` | Bucket name the dated Parquet files are uploaded to. |
| `ARCHIVE_S3_ACCESS_KEY_ID` | Access key ID for the archive bucket. Set as a Railway service variable in production, not local `.env`. |
| `ARCHIVE_S3_SECRET_ACCESS_KEY` | Secret access key for the archive bucket. Set as a Railway service variable in production, not local `.env`. |
| `ARCHIVE_S3_REGION` | Region for the archive bucket. Default `auto`. |
| `ARCHIVE_S3_PREFIX` | Key prefix under which archives are written (`{prefix}/{portco}/{date}/{table}.parquet`). Default `gtm-archive`. |

### Portco-specific

One set per portco. `PORTCO` is the uppercased portco key. The exact env-var
names are declared in `portco_config.json` under each portco's `sf_credentials`
and `knowledge` blocks, so these are conventions, not magic - change the names
in the config and the orchestrator follows.

Salesforce, OAuth client-credentials flow (preferred):

| Variable | Description |
|---|---|
| `SF_CONSUMER_KEY_PORTCO` | Connected App consumer key (OAuth client ID). |
| `SF_CONSUMER_SECRET_PORTCO` | Connected App consumer secret. |
| `SF_DOMAIN_PORTCO` | Salesforce My Domain host, e.g. `acme.my.salesforce.com`. |

Salesforce, SOAP login fallback (only if you cannot use OAuth):

| Variable | Description |
|---|---|
| `SF_USERNAME_PORTCO` | Salesforce username. |
| `SF_PASSWORD_PORTCO` | Salesforce password. |
| `SF_TOKEN_PORTCO` | Salesforce security token appended to the password. |

Internal knowledge base (Kapa.ai), only if `data_sources.knowledge.enabled` is
`true`:

| Variable | Description |
|---|---|
| `KAPA_PORTCO_API_KEY` | Kapa project API key. Sent as `X-API-KEY` to the Kapa REST chat endpoint. |
| `KAPA_PORTCO_PROJECT_ID` | Kapa project UUID. Forms the endpoint path. |

Forks with no internal KB leave `knowledge.enabled` at `false` and skip both
Kapa vars. The `search_knowledge_base` tool is then simply not offered to the
agents.

## 2. `portco_config.json` schema

Copy `portco_config.example.json` to `portco_config.json`. Top-level shape:

```json
{
  "portcos": { "<portco_key>": { ... } },
  "master_channel": "C0000000000",
  "admin_user_ids": ["U0000000000"],
  "vault_ids": { "salesforce_acme": "vlt_...", "slack": "vlt_..." }
}
```

| Field | Description |
|---|---|
| `portcos` | Map of portco key to portco object. The key (e.g. `acme`) is the canonical identifier and the source of the uppercased `PORTCO` env-var suffix. |
| `master_channel` | Slack channel ID for cross-portco / admin messages not tied to one company. |
| `admin_user_ids` | Slack user IDs treated as admins (privileged slash commands, alert DMs). |
| `vault_ids` | Named MCP vault IDs (`vlt_...`). `salesforce_<portco>` holds that portco's SF connection; `slack` holds the Slack connection. |

Each portco object:

| Field | Description |
|---|---|
| `name` | Human-readable display name. |
| `status` | `active` (served) or `pending_crm` (defined but not yet wired; skipped at runtime). |
| `fund` | Free-text fund label for grouping. Metadata only. |
| `data_sources` | Map of source type to config. Today: `crm` and optional `knowledge`. |
| `slack_channel` | Slack channel ID bound to this portco, or `null`. The reverse of this field drives channel-to-portco resolution. |
| `team_members` | List of people associated with the portco. Metadata only. |
| `metadata` | Free-form bag: `arr_tier`, `arr_basis`, `gtm_motion`, `sf_org_id`, `sf_username`, etc. |

`data_sources.crm`:

| Field | Description |
|---|---|
| `type` | `salesforce` (active path), or `unknown` for a pending portco. |
| `extraction` | `mcp` for live MCP-vault reads, or `pending`. |
| `sf_alias` | Display alias for this SF org. |
| `mcp_server_url` | Salesforce platform MCP endpoint for sObject reads. |
| `vault_id` | MCP vault ID holding the SF OAuth connection (`vlt_...`). |
| `sf_credentials` | Map naming the env vars that hold each credential (`consumer_key_env`, `consumer_secret_env`, `domain_env`, `username_env`, `password_env`, `token_env`). |

`data_sources.knowledge` (optional):

| Field | Description |
|---|---|
| `type` | `kapa`. |
| `extraction` | `rest`. |
| `enabled` | `true` activates the KB; `false` (default) runs with no KB and the agents degrade gracefully. |
| `tool_name` | Custom tool name exposed to agents. Use the generic `search_knowledge_base`. |
| `api_key_env` | Env var holding the Kapa API key (e.g. `KAPA_ACME_API_KEY`). |
| `project_id_env` | Env var holding the Kapa project UUID. |
| `indexed_sources_summary` | One-line description of what the KB indexes, shown to agents as context. |

### Channel-to-portco resolution

Every inbound Slack event carries a channel ID. `orchestrator/portco_registry.py`
inverts `portcos[*].slack_channel` into a channel-to-portco map at load, then
looks up the event's channel. The matched portco's `data_sources` and metadata
scope the whole session. If no portco claims the channel, the event is handled
in the default context (`SLACK_CHANNEL_ID`). A portco with `slack_channel: null`
or `status: pending_crm` is never resolved.

### Platform-priority precedence

When more than one CRM-class source could answer a question, the orchestrator
ranks platforms by a fixed priority so cross-source results stay deterministic:
Salesforce (100) > Zoho (90) > HubSpot (80) > others. The highest-priority
source that has the requested object wins; lower-priority sources fill gaps.
Today only the Salesforce path is wired, so this matters once a portco lists a
second CRM.

## 3. Salesforce custom fields

The nightly Postgres sync (Salesforce mirror) expects four custom fields on
`Lead`, plus one optional field on `Opportunity`. If a field is missing from
the SF org, the row reader's `r.get(...)` returns `None` and the sync writes
`NULL` for that column. **It does not crash** - the Lead or Opportunity row
still lands, queries filtering on the missing field just return zero rows.

`Lead`:

| Field | Notes |
|---|---|
| `Discovery_Call_Booked__c` | Timestamp the discovery call was booked. Stored as `TIMESTAMPTZ`. If your org defines it as a checkbox, flip the column to `BOOLEAN` in the lead-sync migration. |
| `Funnel_Stage__c` | Lead funnel stage label. |
| `MQL_SDR_Accepted_Date_Time__c` | When the SDR accepted the MQL. |
| `SDR_Qualified_Date_Time__c` | When the SDR marked the lead qualified. |

`Opportunity` (optional):

| Field | Notes |
|---|---|
| `Product_Line__c` | Single-string product line per opportunity. Lands in `opportunities.product_line` so cross-cuts like Industry x Product Line run from Postgres instead of paying live MCP per analysis. Deliberately flat (one row per opp), not a join on line items. |

To add a missing field: create it in Salesforce Setup, then re-run the field
backfill script for that portco. Leaving it missing is also fine; the column
just stays `NULL`.

## 4. Agent model assignments

Each agent's target model is declared in the `AGENTS` registry in
`agents/update_prompts.py` and pushed by the prompt-deploy pipeline. Three
models are in play: Opus 4.8 for heavy reasoning, Sonnet 4.6 for fast
specialist and entry work, Haiku 4.5 for prose composition.

| Tier | Agent | Model |
|---|---|---|
| 1 - Entry | Prompt Engineer | Sonnet 4.6 |
| 1 - Entry | Quick Answer | Sonnet 4.6 |
| 2 - Orchestration | Coordinator | Opus 4.8 |
| 2 - Orchestration | Dream Agent | Sonnet 4.6 |
| 3 - Specialist | Pipeline Monitor | Sonnet 4.6 |
| 3 - Specialist | Sales Process Monitor | Sonnet 4.6 |
| 3 - Specialist | Post-Sales Monitor | Sonnet 4.6 |
| 3 - Specialist | Chart Designer | Sonnet 4.6 |
| 3 - Specialist | Statistician | Opus 4.8 |
| 3 - Specialist | Adversarial Reviewer | Opus 4.8 |
| 3 - Specialist | Cross-Domain Synthesizer | Opus 4.8 |
| 4 - Output | Writing Agent | Haiku 4.5 |
| Out of roster | RFP Responder | Opus 4.8 |
| Out of roster | RFP Reviewer | Opus 4.8 |

Opus 4.8 goes to anything that has to reason hard or be adversarial:
orchestration (Coordinator), statistics, review, and cross-domain synthesis.
Sonnet 4.6 covers the entry agents and the read-and-summarize specialists where
latency matters more than depth. Haiku 4.5 writes the final user-facing prose.

To change a model, edit the `model` value for that agent in
`agents/update_prompts.py` and run the prompt-deploy pipeline. The Watcher and
RFP agents run in their own sessions outside the Coordinator's roster, but their
models are set the same way.
