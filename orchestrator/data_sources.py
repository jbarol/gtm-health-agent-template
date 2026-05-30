"""Data source adapters for querying CRM/marketing/billing systems.

Mirrors the GTM audit plugin's modular source architecture:
- Base adapter interface with query/describe methods
- Registry maps type strings to adapter classes
- Each portco's config determines which adapter runs
- Graceful degradation: adapters return error dicts, never crash
"""

import json
import logging
from abc import ABC, abstractmethod

log = logging.getLogger(__name__)


class DataSourceAdapter(ABC):
    """Base class for all data source adapters."""

    SOURCE_TYPE = "unknown"
    PROVIDES = []
    DOES_NOT_PROVIDE = []

    @abstractmethod
    def query(self, query_str: str) -> dict:
        """Execute a query and return results."""

    @abstractmethod
    def describe(self, object_name: str) -> dict:
        """Describe an object's schema."""

    def query_next(self, cursor: str) -> dict:
        """Fetch next page of results. Override if pagination supported."""
        return {"error": f"{self.SOURCE_TYPE} does not support pagination"}


class SalesforceMcpAdapter(DataSourceAdapter):
    """Salesforce via MCP (managed agent handles natively — this is a placeholder for routing)."""

    SOURCE_TYPE = "salesforce_mcp"
    PROVIDES = ["crm", "pipeline", "retention", "funnel", "performance"]

    def __init__(self, config: dict):
        self.mcp_server_url = config.get("mcp_server_url")
        self.vault_id = config.get("vault_id")

    def query(self, query_str: str) -> dict:
        return {
            "error": "MCP queries are handled natively by the managed agent, not by the orchestrator"
        }

    def describe(self, object_name: str) -> dict:
        return {"error": "MCP describe is handled natively by the managed agent"}


class HubSpotAdapter(DataSourceAdapter):
    """HubSpot via REST API."""

    SOURCE_TYPE = "hubspot"
    PROVIDES = ["crm", "marketing", "funnel"]
    DOES_NOT_PROVIDE = ["retention"]

    def __init__(self, config: dict):
        self.api_key = config.get("api_key", "")
        self.base_url = "https://api.hubapi.com"

    def query(self, query_str: str) -> dict:
        import httpx

        # HubSpot uses a search API, not SOQL
        # The query_str is treated as a JSON search request body
        try:
            body = json.loads(query_str)
        except json.JSONDecodeError:
            return {
                "error": f"HubSpot queries must be JSON search bodies, not SOQL. Got: {query_str[:200]}"
            }

        object_type = body.pop("objectType", "contacts")
        resp = httpx.post(
            f"{self.base_url}/crm/v3/objects/{object_type}/search",
            json=body,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=60,
        )
        if resp.status_code != 200:
            return {"error": f"HubSpot API error {resp.status_code}: {resp.text[:300]}"}
        return resp.json()

    def describe(self, object_name: str) -> dict:
        import httpx

        resp = httpx.get(
            f"{self.base_url}/crm/v3/properties/{object_name}",
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=60,
        )
        if resp.status_code != 200:
            return {"error": f"HubSpot API error {resp.status_code}: {resp.text[:300]}"}
        data = resp.json()
        return {
            "name": object_name,
            "field_count": len(data.get("results", [])),
            "fields": [
                {
                    "name": f["name"],
                    "label": f["label"],
                    "type": f["type"],
                    "options": f.get("options", []),
                }
                for f in data.get("results", [])
            ],
        }


class ZohoAdapter(DataSourceAdapter):
    """Zoho CRM via REST API."""

    SOURCE_TYPE = "zoho"
    PROVIDES = ["crm", "pipeline", "performance"]
    DOES_NOT_PROVIDE = ["retention", "marketing"]

    def __init__(self, config: dict):
        self.access_token = config.get("access_token", "")
        self.base_url = config.get("api_domain", "https://www.zohoapis.com")

    def query(self, query_str: str) -> dict:
        import httpx

        # Zoho uses COQL (Criteria based Object Query Language)
        resp = httpx.post(
            f"{self.base_url}/crm/v7/coql",
            json={"select_query": query_str},
            headers={"Authorization": f"Zoho-oauthtoken {self.access_token}"},
            timeout=60,
        )
        if resp.status_code != 200:
            return {"error": f"Zoho API error {resp.status_code}: {resp.text[:300]}"}
        return resp.json()

    def describe(self, object_name: str) -> dict:
        import httpx

        resp = httpx.get(
            f"{self.base_url}/crm/v7/settings/fields",
            params={"module": object_name},
            headers={"Authorization": f"Zoho-oauthtoken {self.access_token}"},
            timeout=60,
        )
        if resp.status_code != 200:
            return {"error": f"Zoho API error {resp.status_code}: {resp.text[:300]}"}
        data = resp.json()
        return {
            "name": object_name,
            "field_count": len(data.get("fields", [])),
            "fields": [
                {
                    "name": f["api_name"],
                    "label": f["display_label"],
                    "type": f["data_type"],
                    "pick_list_values": f.get("pick_list_values", []),
                }
                for f in data.get("fields", [])
            ],
        }


# --- Adapter Registry ---

ADAPTER_REGISTRY = {
    "salesforce": SalesforceMcpAdapter,
    "salesforce_mcp": SalesforceMcpAdapter,
    "hubspot": HubSpotAdapter,
    "zoho": ZohoAdapter,
}


def get_adapter(source_config: dict) -> DataSourceAdapter:
    """Instantiate the correct adapter for a data source config."""
    source_type = source_config.get("type", "")

    # Prefer MCP if configured
    if source_config.get("mcp_server_url") and source_type == "salesforce":
        source_type = "salesforce_mcp"

    cls = ADAPTER_REGISTRY.get(source_type)
    if not cls:
        raise ValueError(
            f"Unknown data source type: {source_type}. Available: {list(ADAPTER_REGISTRY)}"
        )

    return cls(source_config)


def dispatch_query(portco_key: str, domain: str, query_str: str) -> dict:
    """Route a query to the correct adapter for a portco + domain."""
    from portco_registry import get_data_source

    source_config = get_data_source(portco_key, domain)
    if not source_config:
        return {"error": f"No {domain} data source configured for portco: {portco_key}"}

    try:
        adapter = get_adapter(source_config)
        return adapter.query(query_str)
    except Exception as e:
        log.error(f"Query failed for {portco_key}/{domain}: {e}")
        return {
            "error": str(e),
            "hint": "Check data source configuration and credentials.",
        }


def dispatch_describe(portco_key: str, domain: str, object_name: str) -> dict:
    """Route a describe to the correct adapter for a portco + domain."""
    from portco_registry import get_data_source

    source_config = get_data_source(portco_key, domain)
    if not source_config:
        return {"error": f"No {domain} data source configured for portco: {portco_key}"}

    try:
        adapter = get_adapter(source_config)
        return adapter.describe(object_name)
    except Exception as e:
        log.error(f"Describe failed for {portco_key}/{domain}: {e}")
        return {"error": str(e)}
