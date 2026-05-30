"""Assert that source-of-truth sub-agents are wired to SUB_AGENT_DATA_TOOLS.

setup_agents.py is structured so the ``client.beta.agents.create(...)``
calls live inside ``def main():`` — importing the module does NOT mint
agents. That lets us inspect the module's AST and assert that each
sub-agent (Pipeline Monitor, Sales Monitor, Post-Sales Monitor, Writing
Agent) passes ``tools=SUB_AGENT_DATA_TOOLS`` rather than an inline list.

This guards against accidental regression — e.g. a future PR replaces
``tools=SUB_AGENT_DATA_TOOLS`` with an inline ``[{"type":
"agent_toolset_20260401"}]`` and silently drops db_query /
dump_sf_query / query_artifact access for that sub-agent.

Track I of Iteration 2 in plan ``misty-squishing-badger``.
"""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO_ROOT / "agents"
ORCH_DIR = REPO_ROOT / "orchestrator"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))
if str(ORCH_DIR) not in sys.path:
    sys.path.insert(0, str(ORCH_DIR))


def _reset_setup_agents_cache():
    """Drop cached setup_agents + sf_mcp_builder so SUB_AGENT_DATA_TOOLS
    is rebuilt the next time the module is imported.

    SUB_AGENT_DATA_TOOLS is computed at module scope via
    ``sf_data_tools_list(vault_path=os.environ.get('SF_MCP_VIA_VAULT'))``.
    The flag is captured at import time and never re-read — so a test
    that flips ``SF_MCP_VIA_VAULT`` must invalidate both module caches
    so the next ``import setup_agents`` rebuilds the constant against
    the new flag value.
    """
    for name in ("setup_agents", "sf_mcp_builder"):
        sys.modules.pop(name, None)


def _import_setup_agents():
    """Force-import setup_agents after a cache reset."""
    _reset_setup_agents_cache()
    return importlib.import_module("setup_agents")


def _setup_agents_source() -> str:
    return (AGENTS_DIR / "setup_agents.py").read_text()


def _setup_agents_module_ast() -> ast.Module:
    """Return the parsed setup_agents.py AST.

    Used by tests that need to walk module-level assigns (e.g. resolve a
    ``tools=NAME`` reference inside ``client.beta.agents.create(...)`` back
    to the list literal where ``NAME`` is defined). Re-parses each call —
    cheap (<1 KB file, microseconds) and keeps the per-test isolation that
    the AST tests rely on.
    """
    return ast.parse(_setup_agents_source())


def _find_agent_create_calls() -> dict[str, ast.Call]:
    """Walk the AST and find each ``client.beta.agents.create(...)`` call,
    keyed by the ``name=`` keyword arg.
    """
    tree = ast.parse(_setup_agents_source())
    out: dict[str, ast.Call] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Match ``client.beta.agents.create(...)``.
        if not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "create":
            continue
        # Walk back: agents.create on something whose .attr == "agents".
        parent = node.func.value
        if not isinstance(parent, ast.Attribute) or parent.attr != "agents":
            continue
        # Find name kwarg.
        name_val = None
        for kw in node.keywords:
            if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                name_val = kw.value.value
        if name_val:
            out[name_val] = node
    return out


def _tools_kwarg_is_sub_agent_data_tools(call: ast.Call) -> bool:
    """Return True iff the call passes a data-tools roster.

    Accepts ``SUB_AGENT_DATA_TOOLS`` (base) or ``SUB_AGENT_DATA_TOOLS_WITH_KAPA``
    (base + Kapa MCP toolset, used by Post-Sales Monitor after the Kapa
    integration). Also supports ``tools=[*SUB_AGENT_DATA_TOOLS, ...]`` for
    future forward-compat shapes.
    """
    data_roster_names = {
        "SUB_AGENT_DATA_TOOLS",
        "SUB_AGENT_DATA_TOOLS_WITH_KAPA",
    }
    for kw in call.keywords:
        if kw.arg != "tools":
            continue
        if isinstance(kw.value, ast.Name) and kw.value.id in data_roster_names:
            return True
        # Also allow ``tools=[*SUB_AGENT_DATA_TOOLS, ...]`` (forward-compat).
        if isinstance(kw.value, ast.List):
            for elt in kw.value.elts:
                if (
                    isinstance(elt, ast.Starred)
                    and isinstance(elt.value, ast.Name)
                    and elt.value.id in data_roster_names
                ):
                    return True
        return False
    return False


def test_pipeline_monitor_uses_sub_agent_data_tools():
    calls = _find_agent_create_calls()
    assert "Pipeline Monitor" in calls
    assert _tools_kwarg_is_sub_agent_data_tools(calls["Pipeline Monitor"]), (
        "Pipeline Monitor must pass tools=SUB_AGENT_DATA_TOOLS (Track I)"
    )


def test_sales_monitor_uses_sub_agent_data_tools():
    calls = _find_agent_create_calls()
    assert "Sales Process Monitor" in calls
    assert _tools_kwarg_is_sub_agent_data_tools(calls["Sales Process Monitor"]), (
        "Sales Process Monitor must pass tools=SUB_AGENT_DATA_TOOLS (Track I)"
    )


def test_postsales_monitor_uses_sub_agent_data_tools():
    calls = _find_agent_create_calls()
    assert "Post-Sales Monitor" in calls
    assert _tools_kwarg_is_sub_agent_data_tools(calls["Post-Sales Monitor"]), (
        "Post-Sales Monitor must pass tools=SUB_AGENT_DATA_TOOLS (Track I)"
    )


def test_writing_agent_carries_reasoning_summary_tool():
    """The Writing Agent's tools[] in setup_agents.main() must contain
    ``REASONING_SUMMARY_TOOL`` alongside the built-in toolset.

    PR 11 (2026-05-14) reshaped the Writing Agent roster — setup_agents.py
    and provision_writing_agent.py both pass
    ``[agent_toolset_20260401, REASONING_SUMMARY_TOOL]``.

    2026-05-27: the Writing Agent joined the Coordinator's multiagent
    roster, so the tools list was extracted into the module-level
    ``WRITING_AGENT_TOOLS`` constant (imported and reused by
    ``agents/update_subagent_tools.py:PRE_PROVISIONED_AGENTS``). This
    test now accepts BOTH shapes — the inline literal list (historical)
    and the ``tools=WRITING_AGENT_TOOLS`` constant reference. For the
    constant-reference case it walks back to the constant's definition
    and asserts ``REASONING_SUMMARY_TOOL`` appears there.
    """
    calls = _find_agent_create_calls()
    assert "GTM Writing Agent" in calls
    create_call = calls["GTM Writing Agent"]
    found_reasoning_summary = False
    for kw in create_call.keywords:
        if kw.arg != "tools":
            continue
        # Shape A — inline list: tools=[..., REASONING_SUMMARY_TOOL].
        if isinstance(kw.value, ast.List):
            for elt in kw.value.elts:
                if isinstance(elt, ast.Name) and elt.id == "REASONING_SUMMARY_TOOL":
                    found_reasoning_summary = True
                    break
        # Shape B — constant reference: tools=WRITING_AGENT_TOOLS. Resolve
        # the constant by walking the module AST for an assign of the same
        # name to a list literal.
        elif isinstance(kw.value, ast.Name) and kw.value.id == "WRITING_AGENT_TOOLS":
            module = _setup_agents_module_ast()
            for node in module.body:
                if (
                    isinstance(node, ast.Assign)
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == "WRITING_AGENT_TOOLS"
                    and isinstance(node.value, ast.List)
                ):
                    for elt in node.value.elts:
                        if (
                            isinstance(elt, ast.Name)
                            and elt.id == "REASONING_SUMMARY_TOOL"
                        ):
                            found_reasoning_summary = True
                            break
                    break
        break
    assert found_reasoning_summary, (
        "GTM Writing Agent must include REASONING_SUMMARY_TOOL in its "
        "tools[] (PR 11) — either inline in the create-call or in the "
        "WRITING_AGENT_TOOLS module constant referenced by that call. "
        "Without it the agent cannot stamp a pre-final-response recap to "
        "the post-mortem log."
    )


def test_sub_agent_data_tools_contents_default(monkeypatch):
    """SUB_AGENT_DATA_TOOLS contains the five default entries when the
    vault flag is OFF.

    Iteration 3 dropped the Salesforce MCP toolset from this roster —
    sub-agents must materialize every SF read via dump_sf_query so the
    Parquet handle (not the raw rows) lands in context. A regression
    that re-adds the SF MCP toolset under the default flag state would
    re-enable the 1.07M-token context-blowup failure mode.

    PR 11 (2026-05-14) added ``reasoning_summary`` to every agent so the
    expected count is now 5 (was 4 before).
    """
    monkeypatch.delenv("SF_MCP_VIA_VAULT", raising=False)
    try:
        setup_agents = _import_setup_agents()

        tools = setup_agents.SUB_AGENT_DATA_TOOLS
        assert isinstance(tools, list)
        assert len(tools) == 5
        names = {t.get("name") for t in tools if t.get("name")}
        types = {t.get("type") for t in tools}
        assert "db_query" in names
        assert "dump_sf_query" in names
        assert "query_artifact" in names
        assert "reasoning_summary" in names
        assert "agent_toolset_20260401" in types
        # The Salesforce MCP toolset must NOT be in the default roster —
        # sub-agents route every SF read through dump_sf_query.
        assert "mcp_toolset" not in types, (
            "SUB_AGENT_DATA_TOOLS with SF_MCP_VIA_VAULT=false must not "
            "include mcp_toolset — Iteration 3 removed it so sub-agents "
            "can't return raw soqlQuery payloads."
        )
    finally:
        # Restore the default state so unrelated tests don't inherit a
        # cached module loaded under a non-default env.
        _reset_setup_agents_cache()


def test_sub_agent_data_tools_contents_vault_path(monkeypatch):
    """SUB_AGENT_DATA_TOOLS exposes the SF mcp_toolset when the vault
    flag is ON.

    Plan #44 Task #17 wired the vault-backed path: when
    ``SF_MCP_VIA_VAULT=true`` is set at deploy time, sub-agents see the
    SF MCP toolset alongside dump_sf_query (the latter stays as a
    fallback). The mcp_toolset/mcp_servers pairing must match — the
    Anthropic API rejects an agent create call where tools[] references
    an mcp_toolset with no matching entry in mcp_servers and vice
    versa.
    """
    monkeypatch.setenv("SF_MCP_VIA_VAULT", "true")
    try:
        setup_agents = _import_setup_agents()

        tools = setup_agents.SUB_AGENT_DATA_TOOLS
        assert isinstance(tools, list)
        # Vault path adds the SF mcp_toolset → one more entry than default.
        # PR 11 (2026-05-14) added reasoning_summary on top → 6 (was 5).
        assert len(tools) == 6
        names = {t.get("name") for t in tools if t.get("name")}
        types = {t.get("type") for t in tools}
        assert "db_query" in names
        assert "dump_sf_query" in names, (
            "dump_sf_query must remain on the roster even when the vault "
            "path is active — it stays as the Parquet-materializing "
            "fallback for the 1M-context guard."
        )
        assert "query_artifact" in names
        assert "reasoning_summary" in names
        assert "agent_toolset_20260401" in types
        assert "mcp_toolset" in types, (
            "SUB_AGENT_DATA_TOOLS with SF_MCP_VIA_VAULT=true must include "
            "the SF mcp_toolset — Plan #44 Task #17 wired the vault path."
        )
    finally:
        _reset_setup_agents_cache()


def test_chart_designer_tools_contents():
    """CHART_DESIGNER_TOOLS = reasoning-roster shape + generate_chart.

    Without this variant, ``update_subagent_tools.py`` would overwrite Chart
    Designer's live tools[] with SUB_AGENT_REASONING_TOOLS — dropping the
    generate_chart custom tool that's its primary output mechanism.

    PR 11 (2026-05-14) added ``reasoning_summary`` to every agent so the
    expected count is now 5 (was 4).
    """
    import setup_agents  # type: ignore

    tools = setup_agents.CHART_DESIGNER_TOOLS
    assert isinstance(tools, list)
    assert len(tools) == 5
    names = {t.get("name") for t in tools if t.get("name")}
    types = {t.get("type") for t in tools}
    assert "generate_chart" in names, (
        "CHART_DESIGNER_TOOLS must include generate_chart — Chart Designer's "
        "primary tool. Dropping it would silently break chart rendering."
    )
    assert "db_query" in names
    assert "query_artifact" in names
    assert "reasoning_summary" in names
    assert "agent_toolset_20260401" in types
    # Chart Designer doesn't query SF directly.
    assert "dump_sf_query" not in names
    assert "mcp_toolset" not in types


def test_generate_chart_tool_schema_shape():
    """generate_chart's input_schema matches what session_runner expects.

    The dispatcher in orchestrator/session_runner.py:294 reads
    tool_input["title"] and tool_input["data"]; the tool's required fields
    must include those. Future schema drift on either side breaks the
    Chart Designer silently — this asserts the contract.
    """
    import setup_agents  # type: ignore

    tool = setup_agents.GENERATE_CHART_TOOL
    assert tool["name"] == "generate_chart"
    assert tool["type"] == "custom"
    schema = tool["input_schema"]
    assert "chart_type" in schema["required"]
    assert "title" in schema["required"]
    assert "data" in schema["required"]
    # Data shape: labels + datasets, each dataset has label + values.
    data_props = schema["properties"]["data"]["properties"]
    assert "labels" in data_props
    assert "datasets" in data_props
    assert {"label", "values"} <= set(data_props["datasets"]["items"]["required"])


def test_sub_agent_reasoning_tools_contents():
    """SUB_AGENT_REASONING_TOOLS contains the four reasoning-safe entries.

    Reasoning sub-agents (Adversarial Reviewer, Cross-Domain Synthesizer,
    Chart Designer) consume validated findings, never raw SF rows. They keep
    db_query (small Postgres reads) and query_artifact (DuckDB over already-
    materialized files) but lose every direct SF path.

    PR 11 (2026-05-14) added ``reasoning_summary`` to every agent so the
    expected count is now 4 (was 3).
    """
    import setup_agents  # type: ignore

    tools = setup_agents.SUB_AGENT_REASONING_TOOLS
    assert isinstance(tools, list)
    assert len(tools) == 4
    names = {t.get("name") for t in tools if t.get("name")}
    types = {t.get("type") for t in tools}
    assert "db_query" in names
    assert "query_artifact" in names
    assert "reasoning_summary" in names
    assert "agent_toolset_20260401" in types
    assert "dump_sf_query" not in names, (
        "Reasoning agents must not have dump_sf_query — they reason over "
        "findings, not raw SF queries."
    )
    assert "mcp_toolset" not in types


def _system_kwarg(call: ast.Call) -> str:
    """Return the value of the ``system=`` kwarg from a create() call.

    Returns empty string if the kwarg is missing or non-literal.
    """
    for kw in call.keywords:
        if kw.arg == "system" and isinstance(kw.value, ast.Constant):
            return kw.value.value or ""
    return ""


def _mcp_servers_kwarg_is_present(call: ast.Call) -> bool:
    """Return True iff the call passes a non-empty ``mcp_servers=`` kwarg.

    Iter3 sub-agents have no SF MCP toolset in their tools[], so the
    create() call MUST NOT pass an unconditional ``mcp_servers=[SF_MCP_SERVER]``
    (the update reconciler would otherwise have to scrub it on every deploy
    and the live API rejects "mcp_servers declared but no mcp_toolset
    references them" on subsequent updates).

    Plan #44 Task #17 — the flag-gated `_monitor_mcp_servers` identifier
    is allowed. It resolves to `[SF_MCP_SERVER]` only when
    `SF_MCP_VIA_VAULT=true` (and `SUB_AGENT_DATA_TOOLS` then carries the
    SF mcp_toolset in lockstep) and `[]` otherwise. Treat it as the
    "empty by default" shape this guard was authored to allow.

    Generic MCP integration augmentation — a create call may extend the
    flag-gated identifier via ``_monitor_mcp_servers + [SOME_SERVER]``.
    That shape is structurally safe so long as the additional server
    pairs with a corresponding mcp_toolset entry in tools[]. We accept
    any BinOp whose left operand is the flag-gated identifier.
    """
    for kw in call.keywords:
        if kw.arg == "mcp_servers":
            if isinstance(kw.value, ast.List) and len(kw.value.elts) == 0:
                return False
            # Flag-gated identifier: `_monitor_mcp_servers` is empty in
            # the default deploy shape (SF_MCP_VIA_VAULT unset/false).
            if isinstance(kw.value, ast.Name) and kw.value.id == "_monitor_mcp_servers":
                return False
            # Generic augmentation: `_monitor_mcp_servers + [<server>]`
            # (or any other BinOp(+, _monitor_mcp_servers, ...) shape) keeps
            # the flag-gated SF entry and adds an integration server that
            # pairs with its mcp_toolset entry in tools[]. Allowed.
            if (
                isinstance(kw.value, ast.BinOp)
                and isinstance(kw.value.op, ast.Add)
                and isinstance(kw.value.left, ast.Name)
                and kw.value.left.id == "_monitor_mcp_servers"
            ):
                return False
            return True
    return False


# Plan #44 review concern HIGH #2 — Sales + Post-Sales Monitor creation
# prompts in setup_agents.py used the Iter2 vocabulary (soqlQuery,
# describeSObject) which was scrubbed from update_prompts.py during
# Iter3 but missed in the creation prompts. A fresh portco that runs
# setup_agents.py to mint agents would have inherited a stale prompt
# that points at tools no longer in the registry. These tests guard
# the alignment.


def _prompt_uses_iter2_call_syntax(prompt: str) -> bool:
    """True iff the prompt contains an Iter2-style tool-call example.

    The Iter3 prompts may still mention the names ``soqlQuery`` /
    ``describeSObject`` inside a deprecation notice ("soqlQuery is NO
    LONGER in your registry"). That's fine — we only fail when the
    prompt INSTRUCTS the agent to call them, i.e. it shows an example
    invocation like ``soqlQuery({...})`` or ``describeSObject(...)``.
    Iter3 examples use ``db_query({"sql": ...})`` or
    ``dump_sf_query(...)`` exclusively.
    """
    return "soqlQuery({" in prompt or "describeSObject(" in prompt


def _prompt_uses_iter2_probe_example(prompt: str) -> bool:
    """True iff the "verify tool access" probe still uses the Iter2 call.

    The probe block must instruct the agent to call ``db_query({"sql":
    "SELECT 1"})`` — the Iter3 trivial-probe pattern. A leftover
    ``soqlQuery({"q": "SELECT Id FROM Account LIMIT 1"})`` block means
    the create() prompt was not migrated.
    """
    return 'soqlQuery({"q":' in prompt or 'soqlQuery({"q":' in prompt


def test_pipeline_monitor_creation_prompt_uses_iter3_vocab():
    """Pipeline Monitor create() prompt must invoke Iter3 tools (db_query / dump_sf_query).

    Iter3 dropped the SF MCP tools — the prompt must show example
    invocations of db_query / dump_sf_query, not soqlQuery /
    describeSObject. Deprecation notices that name the removed tools
    are fine; example-syntax calls are not. Closes Plan #44 review
    concern HIGH #2 (decision row #18 + #2).
    """
    calls = _find_agent_create_calls()
    prompt = _system_kwarg(calls["Pipeline Monitor"])
    assert prompt, "Pipeline Monitor must have a system prompt"
    assert not _prompt_uses_iter2_call_syntax(prompt), (
        "Pipeline Monitor create() prompt still has an Iter2-style "
        "tool-call example (soqlQuery({...}) or describeSObject(...)). "
        "Iter3 instructs every agent to call db_query / dump_sf_query "
        "instead."
    )
    assert not _prompt_uses_iter2_probe_example(prompt), (
        "Pipeline Monitor create() prompt's tool-access probe still uses "
        'the Iter2 soqlQuery({"q": ...}) form. Iter3 probes via '
        'db_query({"sql": "SELECT 1"}).'
    )
    assert "db_query" in prompt, (
        "Pipeline Monitor create() prompt must mention db_query — it's "
        "the Postgres-snapshot path now."
    )
    assert "dump_sf_query" in prompt, (
        "Pipeline Monitor create() prompt must mention dump_sf_query — "
        "the Iter3-required path for live SF reads."
    )


def test_sales_monitor_creation_prompt_uses_iter3_vocab():
    """Sales Process Monitor create() prompt must invoke Iter3 tools."""
    calls = _find_agent_create_calls()
    prompt = _system_kwarg(calls["Sales Process Monitor"])
    assert prompt, "Sales Process Monitor must have a system prompt"
    assert not _prompt_uses_iter2_call_syntax(prompt), (
        "Sales Process Monitor create() prompt still has an Iter2-style "
        "tool-call example. Iter3 instructs every agent to call "
        "db_query / dump_sf_query instead."
    )
    assert not _prompt_uses_iter2_probe_example(prompt), (
        "Sales Process Monitor create() prompt's tool-access probe still "
        "uses the Iter2 soqlQuery form."
    )
    assert "db_query" in prompt
    assert "dump_sf_query" in prompt


def test_postsales_monitor_creation_prompt_uses_iter3_vocab():
    """Post-Sales Monitor create() prompt must invoke Iter3 tools."""
    calls = _find_agent_create_calls()
    prompt = _system_kwarg(calls["Post-Sales Monitor"])
    assert prompt, "Post-Sales Monitor must have a system prompt"
    assert not _prompt_uses_iter2_call_syntax(prompt), (
        "Post-Sales Monitor create() prompt still has an Iter2-style "
        "tool-call example. Iter3 instructs every agent to call "
        "db_query / dump_sf_query instead."
    )
    assert not _prompt_uses_iter2_probe_example(prompt), (
        "Post-Sales Monitor create() prompt's tool-access probe still "
        "uses the Iter2 soqlQuery form."
    )
    assert "db_query" in prompt
    assert "dump_sf_query" in prompt


def test_three_monitor_creates_do_not_pass_sf_mcp_servers():
    """The 3 Monitor create() calls must not pass mcp_servers=[SF_MCP_SERVER].

    SUB_AGENT_DATA_TOOLS has no mcp_toolset entry; passing mcp_servers
    leaves an orphan registry entry that the API rejects on the next
    update_subagent_tools.py reconcile (the same "mcp_servers declared
    but no mcp_toolset references them" failure that bit the Statistician
    during Iter3). Closes Plan #44 review concern HIGH #2.
    """
    calls = _find_agent_create_calls()
    for monitor in ("Pipeline Monitor", "Sales Process Monitor", "Post-Sales Monitor"):
        assert not _mcp_servers_kwarg_is_present(calls[monitor]), (
            f"{monitor} create() must NOT pass mcp_servers=[...] — "
            "SUB_AGENT_DATA_TOOLS has no mcp_toolset entry and the live "
            "API rejects orphan mcp_servers registry entries on the next "
            "reconcile."
        )


def test_setup_agents_module_import_does_not_call_network():
    """Importing setup_agents must not call client.beta.agents.create.

    The ``def main()`` refactor is what makes update_subagent_tools.py
    safe to import SUB_AGENT_DATA_TOOLS from. If a future PR moves a
    .create() call back to module scope it would re-mint agents at
    import time.
    """
    source = _setup_agents_source()
    tree = ast.parse(source)
    # Collect every ``Call`` node that calls ``client.beta.<X>.create``.
    network_calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "create":
            continue
        # Walk up: ``something.something.create``.
        # Collect dotted-name and assert the parent FunctionDef.
        network_calls.append(node)

    # Find each call's nearest enclosing FunctionDef. If any are at
    # module scope we fail.
    parent_map: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parent_map[child] = parent

    def in_function(node):
        cur = parent_map.get(node)
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return True
            cur = parent_map.get(cur)
        return False

    module_scope_calls = [
        ast.unparse(n.func) for n in network_calls if not in_function(n)
    ]
    assert not module_scope_calls, (
        "setup_agents.py must guard all .create() calls inside "
        "def main(); these are at module scope: "
        f"{module_scope_calls}"
    )
