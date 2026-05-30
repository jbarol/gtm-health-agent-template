# Getting Started: Cold Setup

This is the step-by-step guide for standing up your own GTM Health Agent
instance from nothing. It assumes you have forked or cloned the template
and have shell access to a machine with Python 3.12 and `git`.

GTM Health Agent is an autonomous go-to-market operations analyst. A Python
orchestrator (`orchestrator/`) bridges Slack, a CRM (Salesforce by default,
via Anthropic MCP vaults), and Claude through Anthropic's Managed Agents API.
It runs on Railway with a Postgres add-on that mirrors your CRM nightly. The
agents monitor pipeline health, sales process, and retention, and answer
ad-hoc questions in Slack.

Budget about two to three hours for a first run. Most of that is waiting on
external consoles (Salesforce Connected App approval, Slack install, Railway
build), not on the code.

Work through the sections in order. Each one ends with a concrete artifact
(an ID, a token, a green check) that the next section needs.

---

## 1. Prerequisites: accounts to create first

Create these accounts before you touch the code. The first one is a hard
external dependency: without Managed Agents beta access the agent cannot run
at all.

| Account | Why | Where | Notes |
|---|---|---|---|
| Anthropic org with Managed Agents beta | The agents run on Anthropic's Managed Agents API | https://console.anthropic.com | Beta-gated. Request access and confirm the `managed-agents-2026-04-01` beta is enabled for your org before continuing. This is the blocker. |
| Slack workspace you admin | The agent talks to users in Slack channels and runs in Socket Mode | https://api.slack.com/apps | You need admin rights to install an app and approve OAuth scopes. |
| Salesforce org (or another CRM) | Source of pipeline / sales-process / retention data | https://login.salesforce.com | You will create a Connected App with OAuth. A Developer Edition org works for testing. |
| Railway account | Hosts the orchestrator container plus a Postgres add-on | https://railway.app | Free tier is enough to boot; production load wants a paid plan. |

Optional services you can skip on the first pass and wire later:

- **Anthropic Admin API key** for daily cost reconciliation.
- **Kapa.ai** for an internal knowledge base. Forks without a KB run fine.
- **Compresr** for prompt compression.
- **GitHub fine-grained PAT** so the Watcher can open draft fix-PRs.
- **QuickChart** for chart rendering (no key needed for basic use).

---

## 2. Clone, Python environment, install

```bash
git clone https://github.com/your-org/gtm-health-agent.git
cd gtm-health-agent

python3.12 -m venv .venv
source .venv/bin/activate

pip install -r orchestrator/requirements.txt
```

Confirm the test suite collects. Tests use the `*_test.py` suffix (not the
pytest default), so you must pass the pattern explicitly:

```bash
pytest -p no:cacheprovider -o python_files='*_test.py' orchestrator agents bin
```

Type-check and lint while you are here:

```bash
pyright
ruff check orchestrator agents bin scripts
```

Now create your `.env` from the template. You will fill it in over the next
several sections:

```bash
cp .env.example .env
```

Keep `.env` open in an editor. Every ID and token you mint below gets pasted
into it.

---

## 3. Anthropic: API key, beta, and agent provisioning

### 3a. Mint the API key

In https://console.anthropic.com go to **Settings -> API Keys** and create a
standard key. It starts with `sk-ant-`. Paste it into `.env`:

```
ANTHROPIC_API_KEY=sk-ant-your-key-here
```

Confirm your org has the Managed Agents beta enabled. The SDK sends the
`managed-agents-2026-04-01` beta header on every call; if the beta is not
enabled for your org, `setup_agents.py` fails immediately with a 403.

### 3b. Create the environment, agents, and memory stores

Run the one-time setup script. It creates the cloud environment, **all 11
roster agents**, and the two Anthropic memory stores, then prints every ID you
need:

```bash
python agents/setup_agents.py
```

It prints a block at the end labelled `# --- Copy to .env ---`. Capture every
line. `setup_agents.py` creates and prints all 11 roster agent IDs plus the
environment and both memory stores. The IDs you must paste into `.env`:

- `ENVIRONMENT_ID`
- `COORDINATOR_ID`
- `DREAM_AGENT_ID`
- `QUICK_AGENT_ID`
- `PIPELINE_MONITOR_ID`, `SALES_MONITOR_ID`, `POSTSALES_MONITOR_ID`
- the other Tier-3 specialist IDs (`STATISTICIAN_ID`, `ADVERSARIAL_REVIEWER_ID`,
  `CROSS_DOMAIN_SYNTHESIZER_ID`, `CHART_DESIGNER_ID`)
- `WRITING_AGENT_ID` (Tier-4 prose composer, created here too)
- `METHODOLOGY_STORE_ID` (read-only Methodology store, seeded from
  `skills/gtm-methodology.md`)
- `HEALTH_STORE_ID` (read-write per-portco Health store)

The eleven agents created here: Coordinator, Dream Agent, Quick Answer,
Pipeline Monitor, Sales Process Monitor, Post-Sales Monitor, Statistician,
Chart Designer, Adversarial Reviewer, Cross-Domain Synthesizer, and the Writing
Agent. The twelfth agent, the Prompt Engineer, is provisioned separately in 3c
below (it preprocesses Slack questions before the Coordinator session exists, so
it is not in the Coordinator's roster).

> The `*_VERSION` lines that print alongside the IDs are informational. You do
> not need them in `.env` for a first boot.

### 3c. Provision the standalone agents

`setup_agents.py` creates the entire Coordinator roster (including the Writing
Agent). The remaining agents run in their own sessions, outside the roster, and
each has its own provisioning script. Run the ones you want; each prints an ID
line you paste into `.env`:

```bash
python agents/provision_prompt_engineer.py       # -> PROMPT_ENGINEER_ID
python agents/provision_rfp_agent.py             # -> RFP_RESPONDER_ID   (optional)
python agents/provision_rfp_reviewer_agent.py    # -> RFP_REVIEWER_ID    (optional)
python agents/provision_watcher_agent.py         # -> WATCHER_AGENT_ID   (optional)
```

When a standalone ID is left unset the system degrades rather than crashes: an
unset `PROMPT_ENGINEER_ID` skips question refinement, and the RFP and Watcher
paths simply stay off. The `WRITING_AGENT_ID` is set from 3b above; when it is
unset the Coordinator falls back to the direct renderer plus prose-polish.

---

## 4. Slack: app, Socket Mode, install

### 4a. Create the app from the manifest

Go to https://api.slack.com/apps and click **Create New App ->
From an app manifest**. Pick your workspace, then paste the contents of
`manifest.yaml` from the repo root. The manifest declares the bot user, the
slash commands, the event subscriptions, and the full set of OAuth scopes,
so you do not configure those by hand.

### 4b. Enable Socket Mode and mint the app token

The manifest sets `socket_mode_enabled: true`. The orchestrator connects over
Socket Mode, so there is no public webhook URL to expose. Under
**Settings -> Basic Information -> App-Level Tokens**, generate a token with
the `connections:write` scope. It starts with `xapp-`. Paste it:

```
SLACK_APP_TOKEN=xapp-your-app-level-token
```

### 4c. Install to the workspace and mint the bot token

Under **Settings -> Install App**, click **Install to Workspace** and approve
the scopes. Copy the **Bot User OAuth Token** (starts with `xoxb-`):

```
SLACK_BOT_TOKEN=xoxb-your-bot-token
```

### 4d. Set the channel ID

Create (or pick) the channel the agent posts into, invite the bot to it, and
copy the channel ID. In the Slack client, open the channel, click the channel
name, and the ID (starts with `C`) is at the bottom of the **About** tab.

```
SLACK_CHANNEL_ID=C0123456789
SLACK_NOTIFY_USER_IDS=U0123456789
```

`SLACK_NOTIFY_USER_IDS` is a comma-separated list of user IDs that receive
admin DMs (deploy outcomes, degraded-mode warnings).

> OAuth scopes are declarative in `manifest.yaml`. If you later change scopes,
> you reinstall the app to pick them up. The reinstall flow is documented in
> `docs/slack/scopes-changelog.md`.

---

## 5. Salesforce: Connected App and custom fields

The agent reads Salesforce through an MCP vault, and a nightly job mirrors it
into Postgres. You need a Connected App for OAuth and a handful of custom
fields for the sync to write complete rows.

### 5a. Create the Connected App

In Salesforce **Setup -> App Manager -> New Connected App**, enable OAuth
settings and select the **OAuth client-credentials** flow (preferred). Assign
a run-as integration user. Capture the consumer key, consumer secret, and your
org's My Domain login URL.

### 5b. Set per-portco SF env vars

Salesforce credentials are per-portco env vars. The variable names are
declared in `portco_config.json` (next section) and suffixed with the
uppercased portco key. For a portco keyed `acme`, the client-credentials path
uses:

```
SF_CONSUMER_KEY_ACME=your-consumer-key
SF_CONSUMER_SECRET_ACME=your-consumer-secret
SF_DOMAIN_ACME=your-org.my.salesforce.com
```

If you must use the older SOAP path instead, set
`SF_USERNAME_ACME` / `SF_PASSWORD_ACME` / `SF_TOKEN_ACME`. Client-credentials
is preferred; use SOAP only when the org cannot grant a Connected App.

### 5c. Add the custom Lead fields

The nightly Lead sync expects four custom fields on the `Lead` object. Add
them in **Setup -> Object Manager -> Lead -> Fields & Relationships**:

- `Discovery_Call_Booked__c`
- `Funnel_Stage__c`
- `MQL_SDR_Accepted_Date_Time__c`
- `SDR_Qualified_Date_Time__c`

If any field is missing, the sync writes `NULL` for it rather than crashing,
so the Lead row still lands; queries that filter on the missing field just
return zero rows. Either add the field in Salesforce or accept the empty
column.

Optional but recommended: add `Product_Line__c` (single-string product line)
on the `Opportunity` object. It lands in `opportunities.product_line` so
cross-cuts like `Industry x Product Line` run from Postgres instead of paying
live MCP per analysis.

### 5d. Create the Anthropic MCP vault for Salesforce

For live Salesforce reads, the agents reach Salesforce through an **Anthropic
MCP vault** — a credential store on Anthropic's side that brokers the OAuth
token so it never lands on Railway. Two places refer to the same vault:

- the per-portco `vault_id` (`vlt_...`) under
  `portcos.<key>.data_sources.crm.vault_id` (and `vault_ids.salesforce_<key>`)
  in `portco_config.json`, and
- the `ACME_VAULT_ID` env var in `.env` (uppercased portco key).

Create the vault and attach the SF credential:

1. **Create the vault.** Create an MCP vault via the Anthropic Console (or the
   API). It returns a `vlt_...` id. Paste that id into both `portco_config.json`
   (replacing the `vlt_REPLACE_WITH_YOUR_SALESFORCE_VAULT_ID` placeholders) and
   into `.env` as `ACME_VAULT_ID=vlt_...`.

2. **Attach the Salesforce credential.** Set the Connected App OAuth values in
   `.env` — `SF_ACME_CLIENT_ID`, `SF_ACME_CLIENT_SECRET`,
   `SF_ACME_REFRESH_TOKEN`, `SF_ACME_ACCESS_TOKEN` (plus optional
   `SF_ACME_TOKEN_ENDPOINT` / `SF_ACME_SCOPE`) — then run the helper. It defaults
   to a dry-run that prints the redacted payload; pass `--apply` to write:

   ```bash
   python bin/add-sf-vault-credential.py                # dry-run (default)
   python bin/add-sf-vault-credential.py --apply        # creates the credential
   ```

   The script reads `ACME_VAULT_ID` from `.env` (or pass `--vault-id vlt_...`),
   creates an `mcp_oauth` credential in the vault, then runs the
   `mcp_oauth_validate` diagnostic. Confirm it reports `valid` before
   proceeding.

3. **Engage the vault path (optional).** Live SF MCP reads through the vault are
   gated behind the `SF_MCP_VIA_VAULT` build-time flag (default `false`, which
   keeps the proven `dump_sf_query` path). Flipping it to `true` is a
   deploy-time operation — re-run `agents/update_subagent_tools.py` and
   re-publish the Coordinator roster afterward. The full rollout sequence is in
   `docs/runbooks/managed-agents-conformance.md` ("Vault SF MCP rollout"). With
   the flag at the default, the vault credential is still useful for a healthy
   end-to-end credential check, but the agents reach Salesforce via
   `dump_sf_query`.

---

## 6. Portco config

`portco_config.json` maps each company to its data sources, Slack channel, and
metadata. The real file is gitignored; the repo ships an example with a
synthetic `acme` portco.

```bash
cp portco_config.example.json portco_config.json
```

Edit `portco_config.json`:

- Rename the `acme` key to your own portco key (lowercase, no spaces). This key
  is the suffix on your `SF_*` env vars from section 5b.
- Set `name`, `fund`, and `status: "active"`.
- Point `slack_channel` at the channel ID from section 4d.
- Under `data_sources.crm`, set `vault_id` to your Salesforce MCP vault ID and
  confirm the `sf_credentials` env-var names match what you exported.
- Leave `data_sources.knowledge.enabled` as `false` unless you are wiring
  Kapa (section 8).

Channel-to-portco resolution happens in `orchestrator/portco_registry.py`: an
inbound Slack channel is matched against each portco's `slack_channel`, and
the matched portco's `data_sources` drive the query.

---

## 7. Railway and Postgres

### 7a. Create the project and service

In https://railway.app create a new project. Add a service from your repo's
**Dockerfile** (the repo ships a `Dockerfile` and `railway.toml` that point
Railway at it).

### 7b. Add the Postgres plugin

Add the **Postgres** plugin to the project. It injects `DATABASE_URL` into the
service automatically. You do not run any `psql` by hand: the schema
auto-bootstraps at boot via `db_adapter.ensure_schema()` plus migrations.

### 7c. Set the service variables

Copy every variable from your local `.env` into the Railway service
(**Variables** tab). At minimum the service needs `ANTHROPIC_API_KEY`, all the
agent IDs, the three Slack vars, the `SF_*` vars, and `DATABASE_URL` (already
injected by the plugin).

**Important — supply your portco config via env.** `portco_config.json` is
gitignored, so a GitHub/Railway build never contains it (the image would fall
back to the synthetic `acme` example). Provide your real config as a Railway
variable — `portco_registry` reads `PORTCO_CONFIG_JSON` (raw or base64 JSON)
before the on-disk file:

```bash
# raw JSON:
PORTCO_CONFIG_JSON=$(cat portco_config.json)
# or base64 (safer to paste into the Railway UI):
PORTCO_CONFIG_JSON=$(base64 < portco_config.json)
```

### 7d. Turn OFF Auto-Deploy

In **Settings -> Source**, set **Auto Deploy = OFF**. This is mandatory. With
auto-deploy on, a push to `main` and the gated deploy path race each other.
The sanctioned deploy path is the wrapper script, which injects `BUILD_COMMIT`
so `/health` reports the exact git SHA that built the running container.

### 7e. Deploy

From a clean checkout on `main`:

```bash
bin/deploy.sh
```

The script refuses to run on a dirty tree or off `main`, sets `BUILD_COMMIT`
as a Railway variable without triggering an early build, then runs the
detached deploy and prints the build-log URL. Railway's healthcheck points at
`/ready`, which returns 200 only after the pre-deploy smoke probe passes; a
failing probe holds the previous image.

---

## 8. First run and smoke test

### 8a. Hard-required variables

Before the first boot, confirm these are set (locally and on Railway):

- `ANTHROPIC_API_KEY`
- `ENVIRONMENT_ID`, `COORDINATOR_ID`, `QUICK_AGENT_ID`, `DREAM_AGENT_ID`, and
  the Tier-3 specialist IDs
- `METHODOLOGY_STORE_ID`, `HEALTH_STORE_ID`
- `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_CHANNEL_ID`
- the `SF_*` vars for your portco
- `DATABASE_URL` (Railway injects it; for a local run, point it at a local
  Postgres)

### 8b. Run locally

```bash
cd orchestrator && python main.py
```

On boot the orchestrator opens the Slack Socket Mode connection, starts the
APScheduler cron and the investigation worker thread, and runs
`ensure_schema()` against Postgres.

### 8c. Confirm `/health`

The orchestrator binds a small HTTP server on `PORT` (Railway sets it; default
`8080`). Hit the health endpoint and confirm it reports your build commit:

```bash
curl -s localhost:8080/health | python -m json.tool
```

You want `build_commit` to match `git rev-parse HEAD`. On Railway, hit the
same path on the service's public URL. `/ready` returns 200 only after the
smoke probe clears; `/health` always returns 200 once the process is up.

### 8d. Send a test Slack message

In the channel you set as `SLACK_CHANNEL_ID`, mention the bot with a simple
question, for example:

```
@GTM Health Agent how many open opportunities are there this quarter?
```

You should see the bot acknowledge in-thread and then post an answer. If it
acknowledges but never answers, check the orchestrator logs for the
investigation worker and the Salesforce MCP vault connection.

---

## 9. Adding another portco

Once the first portco works, adding a second is config plus credentials, no
code:

1. Add a new top-level key under `portcos` in `portco_config.json` with its own
   `slack_channel` and `data_sources`.
2. Export that portco's `SF_*` env vars, suffixed with its uppercased key (for
   a portco `beta`, that is `SF_CONSUMER_KEY_BETA`, etc.).
3. Create and invite the bot to the new Slack channel.
4. Redeploy with `bin/deploy.sh`.

Each portco is isolated: its own Slack channel and its own subdirectory in the
read-write Health memory store. Cross-source precedence (when a portco has more
than one data source) is governed by the platform priority ranking in the
config.
