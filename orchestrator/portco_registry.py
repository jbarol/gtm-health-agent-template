"""Portco registry: maps companies to data sources, credentials, channels, and team members.

Based on the GTM audit plugin's modular source architecture:
- Each portco has its own data source config (CRM type, marketing, billing)
- Each data source maps to an adapter (Salesforce, HubSpot, Zoho, etc.)
- Extraction hierarchy: MCP > REST API > CLI > manual
- Cross-source precedence when multiple sources provide the same metric
"""

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

REGISTRY_FILE = Path(__file__).parent.parent / "portco_config.json"

PLATFORM_PRIORITY = {
    "salesforce": 100,
    "zoho": 90,
    "hubspot": 80,
    "domo": 70,
    "snowflake": 70,
    "marketo": 60,
    "mailchimp": 60,
    "excel": 50,
}

DEFAULT_REGISTRY = {
    "portcos": {
        "acme": {
            "name": "Acme",
            "status": "active",
            "data_sources": {
                "crm": {
                    "type": "salesforce",
                    "extraction": "mcp",
                    "mcp_server_url": "https://api.salesforce.com/platform/mcp/v1/platform/sobject-reads",
                    "vault_id": None,
                },
            },
            "slack_channel": "C0000000000",
            "team_members": [],
            "metadata": {
                "arr_tier": "unknown",
                "arr_basis": "unknown",
                "gtm_motion": "unknown",
            },
        },
    },
    "master_channel": "C0000000000",
    "admin_user_ids": ["U0000000000"],
}


def _load_registry() -> dict:
    # Deploy-friendly load order:
    #   1. PORTCO_CONFIG_JSON env var — raw JSON or base64-encoded JSON. This is
    #      how a Railway/Docker deploy (which builds from git, where the real
    #      portco_config.json is gitignored) supplies live config WITHOUT
    #      committing portco data: set it as a Railway service variable.
    #   2. portco_config.json on disk (local dev, or a config mounted at runtime).
    #   3. DEFAULT_REGISTRY — the built-in acme example, degraded fallback.
    raw = os.environ.get("PORTCO_CONFIG_JSON", "").strip()
    if raw:
        try:
            if not raw.lstrip().startswith("{"):
                import base64

                raw = base64.b64decode(raw).decode("utf-8")
            return json.loads(raw)
        except Exception:
            log.warning("PORTCO_CONFIG_JSON set but unparseable; falling back")
    if REGISTRY_FILE.exists():
        return json.loads(REGISTRY_FILE.read_text())
    return DEFAULT_REGISTRY


def _save_registry(registry: dict):
    REGISTRY_FILE.write_text(json.dumps(registry, indent=2))


def get_registry() -> dict:
    return _load_registry()


def get_portco(name: str) -> dict:
    registry = _load_registry()
    key = name.lower().replace(" ", "_")
    for k, v in registry["portcos"].items():
        if k == key or v["name"].lower() == name.lower():
            return {**v, "key": k}
    return None


def get_portco_by_channel(channel_id: str) -> dict:
    """Look up which portco a Slack channel belongs to."""
    registry = _load_registry()
    for key, portco in registry["portcos"].items():
        if portco.get("slack_channel") == channel_id:
            return {**portco, "key": key}
    return None


def is_master_channel(channel_id: str) -> bool:
    registry = _load_registry()
    return channel_id == registry.get("master_channel")


def get_all_portcos() -> list:
    registry = _load_registry()
    return [
        {**v, "key": k}
        for k, v in registry["portcos"].items()
        if v.get("status") == "active"
    ]


def get_admin_user_ids() -> list[str]:
    """Return the Slack user IDs that should receive operator-level DMs.

    Plan #35 task #41 — the daily cost digest is DMed to these users at 08:00 PT.
    Sourced from the top-level ``admin_user_ids`` key in ``portco_config.json``
    (mirrored in ``DEFAULT_REGISTRY`` above). Empty list when the key is missing
    so callers can skip work cleanly in degraded mode.
    """
    registry = _load_registry()
    return [uid for uid in (registry.get("admin_user_ids") or []) if uid]


def get_data_source(portco_key: str, domain: str) -> dict:
    """Get data source config for a portco + domain (crm, marketing, billing, etc.)."""
    registry = _load_registry()
    portco = registry["portcos"].get(portco_key)
    if not portco:
        return None
    return portco.get("data_sources", {}).get(domain)


def add_portco(
    key: str,
    name: str,
    data_sources: dict,
    slack_channel: str = None,
    team_members: list = None,
    metadata: dict = None,
):
    """Add a new portco to the registry."""
    registry = _load_registry()
    registry["portcos"][key] = {
        "name": name,
        "status": "active",
        "data_sources": data_sources,
        "slack_channel": slack_channel,
        "team_members": team_members or [],
        "metadata": metadata or {},
    }
    _save_registry(registry)
    log.info(f"Added portco: {key} ({name})")


def get_portco_config(portco_key: str) -> dict:
    """Get raw config dict for a portco by key."""
    registry = _load_registry()
    return registry["portcos"].get(portco_key)


def extract_portco_from_question(question: str) -> str:
    """Try to extract a portco name from a question text."""
    registry = _load_registry()
    q_lower = question.lower()
    for key, portco in registry["portcos"].items():
        if key in q_lower or portco["name"].lower() in q_lower:
            return key
        for alias in portco.get("metadata", {}).get("aliases", []):
            if alias.lower() in q_lower:
                return key
    return None
