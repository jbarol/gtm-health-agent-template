"""Configuration loaded from environment variables (local mode) or .env file."""

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

dotenv_path = Path(__file__).parent.parent / ".env"
if dotenv_path.exists():
    for line in dotenv_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())


def require_env(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"Missing required environment variable: {key}")
    return val


def parse_bool(key: str, default: bool = False) -> bool:
    """Parse a boolean env var. Accepts true/false/yes/no/1/0, case-insensitive.

    Any unrecognized value falls back to `default`.
    """
    raw = os.environ.get(key)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in ("true", "yes", "1", "on"):
        return True
    if normalized in ("false", "no", "0", "off", ""):
        return False
    return default


def warn_if_enabled_without_key(
    key_name: str,
    key_value: str,
    flag_name: str,
    flag_value: bool,
) -> bool:
    """If `flag_value` is true but `key_value` is empty, log a warning and force-disable.

    Returns the effective flag value (False when the key is missing, otherwise unchanged).
    """
    if flag_value and not key_value:
        logger.warning(
            "%s is enabled but %s is unset — running in degraded mode (force-disabling %s).",
            flag_name,
            key_name,
            flag_name,
        )
        return False
    return flag_value


ANTHROPIC_API_KEY = require_env("ANTHROPIC_API_KEY")
SLACK_BOT_TOKEN = require_env("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = require_env("SLACK_APP_TOKEN")
SLACK_CHANNEL_ID = require_env("SLACK_CHANNEL_ID")

ENVIRONMENT_ID = require_env("ENVIRONMENT_ID")
DREAM_AGENT_ID = require_env("DREAM_AGENT_ID")
COORDINATOR_ID = require_env("COORDINATOR_ID")
# Quick Answer agent ID — historical naming clash. Plan #44 decision row
# #18: ``QUICK_AGENT_ID`` is the canonical env var name used by this
# module and ``setup_agents.py`` output. ``QUICK_ANSWER_ID`` is the
# legacy name used by the GitHub Actions deploy workflow's verify gate
# and (until this fix) parts of ``.github/workflows/deploy-prompts.yml``.
# Read both so a Railway env / .env that names the variable either way
# keeps working without surgery. Prefer ``QUICK_AGENT_ID`` when both are
# set; fall back to ``QUICK_ANSWER_ID`` if only that one is present.
_quick_agent_id = os.environ.get("QUICK_AGENT_ID") or os.environ.get("QUICK_ANSWER_ID")
if not _quick_agent_id:
    raise RuntimeError(
        "Missing required environment variable: QUICK_AGENT_ID "
        "(also checked QUICK_ANSWER_ID for backward compatibility)"
    )
QUICK_AGENT_ID = _quick_agent_id
METHODOLOGY_STORE_ID = require_env("METHODOLOGY_STORE_ID")
HEALTH_STORE_ID = require_env("HEALTH_STORE_ID")
ACME_VAULT_ID = os.environ.get("ACME_VAULT_ID", "")
SLACK_VAULT_ID = os.environ.get(
    "SLACK_VAULT_ID", ""
)  # disabled until Slack MCP connection issue resolved

# Kapa Acme integration — pivoted from MCP to REST custom tool on
# 2026-05-13 (the Kapa hosted MCP rejects static_bearer auth; the REST
# endpoint accepts the same key via X-API-KEY header). The orchestrator
# dispatches search_knowledge_base tool calls against
# https://api.kapa.ai/query/v1/projects/<project_id>/chat/.
#
# Required for Kapa-enabled agents (Coordinator, Quick Answer, Dream,
# Post-Sales Monitor, Cross-Domain Synthesizer) to return knowledge
# results. When unset, the dispatcher returns a structured error and
# the agent prompts treat it as "knowledge base unavailable, proceed
# without." SF data path is unaffected.
KAPA_ACME_API_KEY = os.environ.get("KAPA_ACME_API_KEY", "")
KAPA_ACME_PROJECT_ID = os.environ.get("KAPA_ACME_PROJECT_ID", "")

PROMPT_ENGINEER_ID = os.environ.get("PROMPT_ENGINEER_ID", "")

# Writing Agent (Haiku 4.5) — primary prose composer for user-facing copy.
# Provisioned by agents/provision_writing_agent.py and added to Railway.
# In the Coordinator's multiagent roster as of 2026-05-27; the Coordinator
# delegates prose composition via the multiagent runtime (formerly the
# write_prose custom tool). When unset, the Coordinator's roster entry
# silently fails the delegation, the rejection loop short-circuits, and
# the orchestrator falls through to direct post_report (renderer +
# prose_polish safety net). The system degrades gracefully but loses
# Haiku-quality prose.
WRITING_AGENT_ID = os.environ.get("WRITING_AGENT_ID", "")

SF_USERNAME = os.environ.get("SF_USERNAME", "")
SF_PASSWORD = os.environ.get("SF_PASSWORD", "")
SF_SECURITY_TOKEN = os.environ.get("SF_SECURITY_TOKEN", "")
SF_INSTANCE_URL = os.environ.get("SF_INSTANCE_URL", "")
SF_ACCESS_TOKEN = os.environ.get("SF_ACCESS_TOKEN", "")
SF_CLIENT_ID = os.environ.get("SF_CLIENT_ID", "")
SF_CLIENT_SECRET = os.environ.get("SF_CLIENT_SECRET", "")
SF_LOGIN_URL = os.environ.get("SF_LOGIN_URL", "https://login.salesforce.com")

SLACK_NOTIFY_USER_IDS = [
    uid for uid in os.environ.get("SLACK_NOTIFY_USER_IDS", "").split(",") if uid
]

# Schedule (cron expressions)
DREAM_SCHEDULE_CRON = os.environ.get(
    "DREAM_SCHEDULE_CRON", "0 20 * * 0"
)  # Sunday 20:00 (matches CONFIGURATION.md / .env.example)
INVESTIGATION_SCHEDULE_CRON = os.environ.get(
    "INVESTIGATION_SCHEDULE_CRON", "0 9 * * 1"
)  # Monday 9am
TIMEZONE = os.environ.get("TIMEZONE", "America/Los_Angeles")

# Plan #35 — Cost tracking and reporting (Anthropic Admin API)
# Admin API key for the daily cost pull. Format: sk-ant-admin... Only org
# admins can mint one. Must be set as a Railway env var (this app runs on
# Railway, not local). When unset, the cost reconciliation job runs in
# degraded mode (local estimates only, no ground-truth reconciliation).
ANTHROPIC_ADMIN_KEY = os.environ.get("ANTHROPIC_ADMIN_KEY", "")
if not ANTHROPIC_ADMIN_KEY:
    logger.warning(
        "ANTHROPIC_ADMIN_KEY is unset — Admin Usage & Cost API reconciliation "
        "(Plan #35) will run in degraded mode (local estimates only)."
    )

# Plan #36 — Batch processing kill-switch
# When true, self_heal and self_improve route through Anthropic's Batches API
# (50% off, async). When false (default), both call sites stay realtime.
BATCH_PROCESSING_ENABLED = parse_bool("BATCH_PROCESSING_ENABLED", default=False)

# Plan #49 — Channel description push kill switch.
# When false, ``channel_descriptions.push_channel_description`` is a no-op.
# Read at call time (NOT just at import) so the toggle takes effect on the
# next call without a restart. This module-level value is the boot-time
# snapshot for visibility; the runtime check reads the env var directly.
CHANNEL_DESC_PUSH_ENABLED = parse_bool("CHANNEL_DESC_PUSH_ENABLED", default=True)

# Plan #37 — Compresr prompt-compression integration
# COMPRESR_API_KEY format: cmp_... Must be set as a Railway env var. When unset
# but any COMPRESS_*_ENABLED flag is true, the affected site is force-disabled
# with a warning rather than crashing.
COMPRESR_API_KEY = os.environ.get("COMPRESR_API_KEY", "")
COMPRESS_SELF_HEAL_ENABLED = warn_if_enabled_without_key(
    "COMPRESR_API_KEY",
    COMPRESR_API_KEY,
    "COMPRESS_SELF_HEAL_ENABLED",
    parse_bool("COMPRESS_SELF_HEAL_ENABLED", default=False),
)
COMPRESS_SELF_IMPROVE_ENABLED = warn_if_enabled_without_key(
    "COMPRESR_API_KEY",
    COMPRESR_API_KEY,
    "COMPRESS_SELF_IMPROVE_ENABLED",
    parse_bool("COMPRESS_SELF_IMPROVE_ENABLED", default=False),
)
COMPRESS_ADHOC_KICKOFF = warn_if_enabled_without_key(
    "COMPRESR_API_KEY",
    COMPRESR_API_KEY,
    "COMPRESS_ADHOC_KICKOFF",
    parse_bool("COMPRESS_ADHOC_KICKOFF", default=False),
)

# Plan #44 Task #22 — MCP auto-approve allowlist.
# Today no Managed Agent has an mcp_toolset attached (Iter3 removed them and
# routed Salesforce reads through dump_sf_query). The dispatcher at
# session_runner.py:_stream_and_handle previously blanket-approved every MCP
# `ask` call. Per Plan #44 decision row #14 we now require an explicit
# allowlist — any non-allowlisted server with evaluated_permission="ask"
# falls through to a log + admin DM and is NOT auto-approved. Bundle D's
# vault SF work will append the Salesforce MCP server name here when/if
# SF MCP path is re-enabled.
MCP_AUTO_APPROVE_ALLOWLIST: set[str] = set(
    s.strip()
    for s in os.environ.get("MCP_AUTO_APPROVE_ALLOWLIST", "").split(",")
    if s.strip()
)


# Plan #44 Task #8 — Read agents/active_versions.json at orchestrator boot.
# This is the pin file written by CI when agent prompts/tools are deployed.
# Until Plan #41 ships every agent's pin, the file may be missing keys; we
# tolerate that and fall back to "no pin" (bare agent ID) at the session
# create call sites. A missing file produces an empty dict and the same
# fallback — see Task #9 for the resolution logic.
#
# The file is read AT IMPORT TIME by design. A redeploy is required to pick
# up a new pin file from disk. Bundle E's /pin slash command will provide a
# hot-override path on top of this baseline (Task #10).
_ACTIVE_VERSIONS_PATH = Path(__file__).parent.parent / "agents" / "active_versions.json"


def _load_agent_versions() -> dict:
    """Load the pin file into a dict, returning {} on any error.

    Tolerates: missing file, malformed JSON, non-dict shape, unreadable
    permissions. Never raises — the orchestrator must come up even if the
    pin file is broken. Production behavior in that case: every
    ``sessions.create`` resolves to latest agent version (the pre-pin
    default).
    """
    try:
        if _ACTIVE_VERSIONS_PATH.exists():
            with open(_ACTIVE_VERSIONS_PATH, "r") as fh:
                data = json.load(fh)
                if isinstance(data, dict):
                    # Coerce values to int where possible. JSON numbers come
                    # through as int already, but defensive: skip any non-int
                    # value rather than poisoning the dict with a string.
                    cleaned = {}
                    for k, v in data.items():
                        if isinstance(v, int):
                            cleaned[k] = v
                    return cleaned
    except Exception:
        logger.exception(
            "Failed to read %s — agent version pinning disabled",
            _ACTIVE_VERSIONS_PATH,
        )
    return {}


AGENT_VERSIONS: dict[str, int] = _load_agent_versions()
if AGENT_VERSIONS:
    logger.info(
        "Loaded agent version pins: %s",
        ", ".join(f"{k}=v{v}" for k, v in sorted(AGENT_VERSIONS.items())),
    )
else:
    logger.info(
        "No agent version pins loaded (file missing or empty) — sessions "
        "will resolve to latest agent version"
    )
