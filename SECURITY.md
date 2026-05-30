# Security

This template runs Claude agents against a CRM and Slack, holds API keys, and (optionally)
opens pull requests against your GitHub. The threat model is real. Read this before you
provision anything, and read it again before you flip a fork public.

## Reporting a vulnerability

Report security issues privately to **security@your-org.example.com**.
<!-- TODO (forker): replace the address above with your real security contact before you flip this fork public. -->>

Do **not** open a public GitHub issue, discussion, or pull request for anything that could
expose a credential, leak portfolio-company data, or let an agent reach a system it should
not. Public issues are world-readable the moment they are filed, which turns a report into
an exposure.

Include enough to reproduce: affected file or env var, the conditions that trigger it, and
the impact you observed. We aim to acknowledge within **3 business days** and to ship or
describe a fix within **30 days**, sooner for anything actively exploitable. Please give us
that window before disclosing publicly.

## Secret model

**Nothing secret is committed.** The repository tracks only `*.example` files:

- `.env` is gitignored; only `.env.example` is tracked.
- `portco_config.json` is gitignored; only `portco_config.example.json` (the synthetic
  `acme` portco) is tracked.
- `bin/scrub-portco-names.yml` is gitignored; only `bin/scrub-portco-names.yml.example` is
  tracked.

Copy each `*.example` file to its real name locally, fill it in, and never commit the
result. The `.gitignore` already excludes all three.

**CRM and Slack OAuth credentials never enter agent-container code.** Salesforce (or your
chosen CRM) is reached through Anthropic MCP vaults. The per-portco `SF_*` env vars
(`SF_CONSUMER_KEY_<PORTCO>`, `SF_CONSUMER_SECRET_<PORTCO>`, `SF_DOMAIN_<PORTCO>`, or the SOAP
trio) configure the vault, and Anthropic-side proxies inject the live token into outbound
requests **after** they leave the sandbox. Code running inside an agent container cannot
read the vault secret, only call the tool. The same boundary holds for any other vaulted
credential. This is the single most important property to preserve when you extend the
system: route new credentialed integrations through a vault or an orchestrator-side custom
tool, never by handing the secret to an agent.

**Host-side-only secrets stay on the orchestrator.** The orchestrator process holds
`ANTHROPIC_API_KEY` (required), `ANTHROPIC_ADMIN_KEY` (optional, daily cost reconciliation),
and `WATCHER_GH_TOKEN` (optional, the Watcher's GitHub PAT for opening draft fix-PRs). None
of these are ever passed into an agent session, placed in a system prompt, or written to a
memory store. The Watcher PAT in particular should be a fine-grained token scoped to the
single fork repo with the minimum permissions to open a draft PR. Nothing else.

**Never put a key in a prompt or a memory store.** The two Anthropic memory stores
(Methodology, read-only; Health, read-write) persist their contents in session history.
Anything written there is durable and replayed into future sessions. Treat both as
permanent and non-secret. The same rule covers `portco_config.json`: it names env vars, it
does not contain their values.

If a secret is exposed, rotate it at the source first (Anthropic console, Slack app admin,
Salesforce Connected App, GitHub PAT settings), then update the Railway variable. Rotation
beats cleanup. Git history is hard to scrub and proxies cache.

## Agent sandbox networking

`agents/setup_agents.py` creates the Anthropic cloud environment with **unrestricted
egress** by default:

```python
environment = client.beta.environments.create(
    ...,
    config={"networking": {"type": "unrestricted"}},
)
```

Unrestricted egress is convenient for development: agents can `web_search`, `web_fetch`, and
reach any HTTP endpoint a tool needs. It is also the widest blast radius. An agent that is
prompt-injected through CRM free-text (a loss-reason note, an opportunity description) can,
in principle, attempt outbound calls. The vault boundary above keeps it from reading your
secrets, but unrestricted egress means it can still try to exfiltrate whatever it has in
context.

To narrow this, opt into limited networking when you create the environment. Switch the
`networking` block to an allowlist that names only the hosts your tools actually need (your
Anthropic API host, your QuickChart or Kapa endpoint if used, and your CRM/MCP host).
Consult the current Managed Agents environment docs at console.anthropic.com for the exact
allowlist schema, set it in `setup_agents.py`, and re-run setup (or recreate the
environment) so the new `ENVIRONMENT_ID` carries the tighter policy. Production deployments
handling real portfolio-company data should run an allowlisted environment, not the default.

## Before you open-source your own fork

Forking from a populated deployment is where most leaks happen: real portco names, Slack
channel IDs, vault and memory-store IDs, deployment URLs, and incident references end up in
code comments, runbooks, and committed artifacts. Two gates stand between your working tree
and a public repo. Run **both** before you flip the visibility switch.

**1. The portco-leakage scrub (`bin/scrub-portco.py`).** This is the leak gate. It reads
`bin/scrub-portco-patterns.yml` plus your private `bin/scrub-portco-names.yml` (copied from
`bin/scrub-portco-names.yml.example`) and reports every match across three severity tiers:

- **HIGH** — blocks the public flip (Slack/CRM/Anthropic IDs, portco and vendor names,
  emails, deployment URLs).
- **MEDIUM** — review before flip (incident references, internal PR numbers).
- **LOW** — informational.

```bash
python bin/scrub-portco.py                 # scan repo, human-readable
python bin/scrub-portco.py --severity HIGH # HIGH findings only
python bin/scrub-portco.py --json          # machine-readable for CI
```

It exits `1` if any HIGH finding survives the allowlist, `0` otherwise. Wire it as a
CI gate so the fork cannot regress after the flip.

**2. An independent secret scan.** The scrub catches portco identifiers; it is not a
credential scanner. Run a dedicated tool over the full history before going public:

```bash
gitleaks detect --source . --redact
# or
trufflehog git file://. --only-verified
```

A clean working tree is not enough: secrets hide in deleted files that still live in git
history. If either scanner flags a real credential, rotate it at the source (it is already
compromised the moment it touched a clean-room or a shared machine), then rewrite history or
start the public fork from a fresh, squashed root commit. Only flip the repo public after
`scrub-portco.py` exits `0` on HIGH and your secret scanner comes back clean.
