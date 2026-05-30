"""Build the SF data-tools fragment for sub-agent rosters.

Plan #44 Task #17. Two shapes, one switch:

* ``vault_path=False`` (default) — sub-agents see ``dump_sf_query`` and
  hit Salesforce via the Railway-resident OAuth Client Credentials flow
  (the proven path that survived Iter3). No MCP toolset, no
  ``mcp_servers`` declaration. The Coordinator's row-leak defense
  (Parquet handle return) stays intact.

* ``vault_path=True`` — sub-agents see the SF MCP toolset and an
  ``mcp_servers=[SF_MCP_SERVER]`` declaration. Salesforce auth is brokered
  by Anthropic's Acme vault (an ``mcp_oauth`` credential created via
  ``bin/add-sf-vault-credential.py``), which surfaces the audit trail
  and ``mcp_oauth_validate`` diagnostic the Railway path can't provide.
  ``dump_sf_query`` stays on the roster so the Coordinator's "use
  dump_sf_query for >50 rows" contract still has a tool to call —
  flipping the flag does NOT abandon Parquet virtualization, it only
  changes how the underlying SOQL credentials are resolved.

The flag controls the SHAPE of the published agent configuration. It is
read at ``update_subagent_tools.py`` runtime, baked into the agent
config on Anthropic, and consumed by every subsequent session. Flipping
the flag at runtime does nothing — tools are server-side agent state.
Decision row #10 in plan #44 calls this out explicitly.

Bundle D isolation point: Bundle A is also modifying ``setup_agents.py``
(tool description rewrites, xlsx skill). Exposing the SF MCP shape via
a function call here keeps the two PRs' textual diffs disjoint.
"""

from __future__ import annotations

from typing import Any, Dict, List


def _get_sf_constants():
    """Pull DUMP_SF_QUERY_TOOL / SF_MCP_SERVER / SF_MCP_TOOLSET lazily.

    Imported inside the function body (not at module scope) because
    ``setup_agents.py`` calls back into this module at its own module-load
    time to compute ``SUB_AGENT_DATA_TOOLS``. A top-level import here
    would close the cycle and raise ``ImportError`` on first import.
    The lazy resolution path lets both modules finish initializing before
    either touches the other's constants.
    """
    from setup_agents import (  # noqa: WPS433 — local import is intentional
        DUMP_SF_QUERY_TOOL,
        SF_MCP_SERVER,
        SF_MCP_TOOLSET,
    )

    return DUMP_SF_QUERY_TOOL, SF_MCP_SERVER, SF_MCP_TOOLSET


def build_sf_data_tools(vault_path: bool) -> Dict[str, Any]:
    """Return the SF-data fragment to splat into a sub-agent's create kwargs.

    The result is a dict that callers spread into the existing
    ``tools=[...]`` and (when vault path is active) ``mcp_servers=[...]``
    keyword arguments of ``client.beta.agents.create()`` /
    ``.update()``. Two keys may appear in the returned dict:

    * ``tools`` — always present. List of tool entries. In the default
      path this is ``[DUMP_SF_QUERY_TOOL]``. In the vault path it is
      ``[DUMP_SF_QUERY_TOOL, SF_MCP_TOOLSET]`` — both stay on the
      roster so the Coordinator can still call ``dump_sf_query`` for
      >50-row pulls AND the MCP toolset is available for sub-50-row
      ad-hoc reads + write-protection coverage.

    * ``mcp_servers`` — only present in the vault path. List of
      ``mcp_servers`` declarations (currently just
      ``[SF_MCP_SERVER]``). Anthropic requires the declaration when
      ``tools[]`` references an ``mcp_toolset`` entry.

    The caller is expected to merge ``tools`` with the rest of its
    sub-agent roster (e.g. ``DB_QUERY_TOOL``, ``QUERY_ARTIFACT_TOOL``,
    ``agent_toolset_20260401``).
    """
    DUMP_SF_QUERY_TOOL, SF_MCP_SERVER, SF_MCP_TOOLSET = _get_sf_constants()

    if not vault_path:
        return {"tools": [DUMP_SF_QUERY_TOOL]}

    return {
        "mcp_servers": [SF_MCP_SERVER],
        "tools": [DUMP_SF_QUERY_TOOL, SF_MCP_TOOLSET],
    }


def sf_data_tools_list(vault_path: bool) -> List[Any]:
    """Return just the ``tools`` list — convenience for in-place splice.

    ``update_subagent_tools.py`` builds the data-tools roster by
    splicing the SF fragment into an existing tool array; it doesn't
    need the ``mcp_servers`` key (that's handled separately). This
    helper keeps the call site explicit.
    """
    return build_sf_data_tools(vault_path)["tools"]


def sf_mcp_servers(vault_path: bool) -> List[Any]:
    """Return the ``mcp_servers`` list for an agent.

    Empty list in the default path; ``[SF_MCP_SERVER]`` in the vault
    path. Wired into ``update_subagent_tools.update_one`` so the
    declaration is added / cleared in lockstep with the toolset.
    """
    return build_sf_data_tools(vault_path).get("mcp_servers", [])
