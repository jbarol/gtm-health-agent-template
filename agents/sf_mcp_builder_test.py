"""Tests for ``agents/sf_mcp_builder.py`` (Plan #44 Task #17).

Two shapes, one switch. The flag determines whether sub-agents see
``dump_sf_query`` alone (default) or ``dump_sf_query`` plus the SF MCP
toolset (vault path).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO_ROOT / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))


def _names(tools):
    """Helper — extract the (name or type) tag of each tool entry."""
    return [t.get("name") or t.get("type") for t in tools]


# ---------------------------------------------------------------------------
# build_sf_data_tools — default (Railway OAuth) path
# ---------------------------------------------------------------------------


def test_default_path_returns_only_dump_sf_query_in_tools():
    """vault_path=False → tools=[DUMP_SF_QUERY_TOOL]; no mcp_servers key.

    The default path is the proven one. Sub-agents see one SF-shaped
    tool (``dump_sf_query``) and route every SF read through the
    Railway-resident OAuth Client Credentials flow. The Anthropic agent
    config must NOT declare ``mcp_servers`` in this path — otherwise
    the API rejects the update with "mcp_servers declared but no
    mcp_toolset references them" once a Statistician-style stale entry
    is left behind.
    """
    from sf_mcp_builder import build_sf_data_tools

    out = build_sf_data_tools(vault_path=False)
    assert "tools" in out
    assert _names(out["tools"]) == ["dump_sf_query"]
    assert "mcp_servers" not in out, (
        "Default path must not declare mcp_servers — the dump_sf_query "
        "tool brokers SF auth via Railway env, not via an Anthropic vault."
    )


def test_default_path_tool_is_the_canonical_dump_sf_query():
    """The returned tool is the canonical DUMP_SF_QUERY_TOOL constant.

    Identity matters — ``update_subagent_tools.py`` walks the tools list
    and runs ``flatten_refs`` defensively. If a fresh dict were returned,
    a future Pydantic-derived schema change would not propagate.
    """
    from setup_agents import DUMP_SF_QUERY_TOOL
    from sf_mcp_builder import build_sf_data_tools

    out = build_sf_data_tools(vault_path=False)
    assert out["tools"][0] is DUMP_SF_QUERY_TOOL


# ---------------------------------------------------------------------------
# build_sf_data_tools — vault path
# ---------------------------------------------------------------------------


def test_vault_path_returns_dump_sf_query_plus_mcp_toolset():
    """vault_path=True → tools include both DUMP_SF_QUERY_TOOL and SF mcp_toolset.

    The vault path is additive, not replacing. ``dump_sf_query`` remains
    on the roster because the Coordinator's "use dump_sf_query for >50
    rows" contract still has a tool to call — the flag flip changes how
    SF auth is brokered, not how SOQL pulls are materialized.
    """
    from sf_mcp_builder import build_sf_data_tools

    out = build_sf_data_tools(vault_path=True)
    names = _names(out["tools"])
    assert "dump_sf_query" in names
    assert "mcp_toolset" in names
    types = {t.get("type") for t in out["tools"]}
    assert "mcp_toolset" in types


def test_vault_path_includes_mcp_servers_declaration():
    """vault_path=True → mcp_servers contains the SF_MCP_SERVER entry.

    The API requires every mcp_toolset reference to be matched by an
    mcp_servers entry of the same name. Without this, ``agents.update``
    rejects with "mcp_toolset references [salesforce] but no matching
    entry in mcp_servers".
    """
    from setup_agents import SF_MCP_SERVER
    from sf_mcp_builder import build_sf_data_tools

    out = build_sf_data_tools(vault_path=True)
    assert "mcp_servers" in out
    assert out["mcp_servers"] == [SF_MCP_SERVER]


def test_vault_path_toolset_references_salesforce_server_name():
    """The mcp_toolset entry's mcp_server_name matches the server's name."""
    from sf_mcp_builder import build_sf_data_tools

    out = build_sf_data_tools(vault_path=True)
    toolset = next(t for t in out["tools"] if t.get("type") == "mcp_toolset")
    server = out["mcp_servers"][0]
    assert toolset["mcp_server_name"] == server["name"] == "salesforce"


# ---------------------------------------------------------------------------
# sf_data_tools_list — convenience wrapper for in-place splice
# ---------------------------------------------------------------------------


def test_sf_data_tools_list_default_path():
    """Default: returns just [DUMP_SF_QUERY_TOOL]."""
    from sf_mcp_builder import sf_data_tools_list

    out = sf_data_tools_list(vault_path=False)
    assert _names(out) == ["dump_sf_query"]


def test_sf_data_tools_list_vault_path():
    """Vault path: returns [DUMP_SF_QUERY_TOOL, SF_MCP_TOOLSET]."""
    from sf_mcp_builder import sf_data_tools_list

    out = sf_data_tools_list(vault_path=True)
    names = _names(out)
    assert names.count("dump_sf_query") == 1
    assert names.count("mcp_toolset") == 1


# ---------------------------------------------------------------------------
# sf_mcp_servers — companion accessor used by update_subagent_tools
# ---------------------------------------------------------------------------


def test_sf_mcp_servers_default_path_returns_empty_list():
    """Default: no mcp_servers — sub-agents have no SF declaration to manage."""
    from sf_mcp_builder import sf_mcp_servers

    assert sf_mcp_servers(vault_path=False) == []


def test_sf_mcp_servers_vault_path_returns_sf_server_singleton():
    """Vault: returns [SF_MCP_SERVER]. Identity preserved so URL / type / name
    stay centralized in setup_agents.py.
    """
    from setup_agents import SF_MCP_SERVER
    from sf_mcp_builder import sf_mcp_servers

    out = sf_mcp_servers(vault_path=True)
    assert out == [SF_MCP_SERVER]
    assert out[0] is SF_MCP_SERVER


# ---------------------------------------------------------------------------
# Setup_agents wiring — SUB_AGENT_DATA_TOOLS reads the flag at import time
# ---------------------------------------------------------------------------


def _reset_setup_agents_cache():
    """Drop cached setup_agents / sf_mcp_builder so a future import sees env changes.

    SUB_AGENT_DATA_TOOLS is a module-level constant computed at import
    time from os.environ.get('SF_MCP_VIA_VAULT'). Tests that flip the
    flag must drop the module cache; tests that depend on the default
    state must ALSO drop the cache so they don't inherit a vault-shaped
    constant from a sibling test that ran first.
    """
    for mod in ("setup_agents", "sf_mcp_builder"):
        sys.modules.pop(mod, None)


def test_setup_agents_sub_agent_data_tools_default_excludes_mcp_toolset(
    monkeypatch,
):
    """SUB_AGENT_DATA_TOOLS without the flag set contains no mcp_toolset.

    The flag is read at module-import time via
    ``os.environ.get('SF_MCP_VIA_VAULT', 'false')``. With the default
    'false', sub-agents must see no MCP path. The Iter3 contract
    (Parquet handle via dump_sf_query) stays intact.
    """
    monkeypatch.delenv("SF_MCP_VIA_VAULT", raising=False)
    _reset_setup_agents_cache()
    try:
        import setup_agents

        types = {t.get("type") for t in setup_agents.SUB_AGENT_DATA_TOOLS}
        assert "mcp_toolset" not in types
    finally:
        _reset_setup_agents_cache()


def test_setup_agents_sub_agent_data_tools_vault_includes_mcp_toolset(
    monkeypatch,
):
    """SUB_AGENT_DATA_TOOLS with the flag set contains the SF mcp_toolset.

    The flag must be a deploy-time decision — flipping it after import
    does nothing because the constant is captured at module load. Tests
    must drop the cached module first AND restore the default state on
    exit so unrelated tests don't inherit a vault-shaped constant.
    """
    monkeypatch.setenv("SF_MCP_VIA_VAULT", "true")
    _reset_setup_agents_cache()
    try:
        import setup_agents

        types = {t.get("type") for t in setup_agents.SUB_AGENT_DATA_TOOLS}
        assert "mcp_toolset" in types
        names = {t.get("name") for t in setup_agents.SUB_AGENT_DATA_TOOLS}
        assert "dump_sf_query" in names, (
            "dump_sf_query must remain on the roster even when the vault "
            "MCP toolset is added — the Coordinator's >50-row contract "
            "depends on it."
        )
    finally:
        # Reset env (monkeypatch handles env restoration) AND drop the
        # vault-shaped module so later tests see the default again.
        _reset_setup_agents_cache()
