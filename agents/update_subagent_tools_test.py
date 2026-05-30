"""Tests for ``agents/update_subagent_tools.py``.

Idempotent updater for the four pre-provisioned sub-agents'
``tools[]``. Iteration 3 split the single SUB_AGENT_DATA_TOOLS roster
into two — Statistician + the three Monitors keep data tools (no SF
MCP toolset, every SF read routes through dump_sf_query), and the three
reasoning agents (Adversarial, Cross-Domain, Chart) drop SF tools
entirely. Tests use ``unittest.mock.MagicMock`` to simulate the
Anthropic client — no network.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO_ROOT / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))


@pytest.fixture(autouse=True)
def _isolate_active_versions(monkeypatch, tmp_path):
    """Redirect ACTIVE_VERSIONS_PATH to a tmp file for every test.

    main() now refreshes the pin file in-place after tool updates (the
    deploy-prompts.yml ordering fix). Without this fixture, every test
    whose fake_retrieve returns versions that differ from the live pin
    would scribble those test versions onto agents/active_versions.json
    on disk. Each test gets its own tmp pin so they can't see each
    other's writes either.
    """
    import update_subagent_tools as ust  # type: ignore

    monkeypatch.setattr(ust, "ACTIVE_VERSIONS_PATH", tmp_path / "active_versions.json")


def _set_ids(monkeypatch):
    """Set every sub-agent ID env var to a test fixture.

    Deliberately clears COORDINATOR_ID so ``republish_coordinator_multiagent``
    short-circuits via its "no COORDINATOR_ID in environment" branch.
    The dotenv loader at the top of update_subagent_tools.py reads the
    real .env at import time, which would otherwise leak the real
    coordinator ID into tests and break them when the fake retrieve
    side_effect doesn't know how to handle it. Tests that explicitly
    exercise the multiagent re-publish path set COORDINATOR_ID
    themselves.
    """
    monkeypatch.delenv("COORDINATOR_ID", raising=False)
    monkeypatch.setenv("STATISTICIAN_ID", "agent_stat_test")
    monkeypatch.setenv("PIPELINE_MONITOR_ID", "agent_pipe_test")
    monkeypatch.setenv("SALES_MONITOR_ID", "agent_sales_test")
    monkeypatch.setenv("POSTSALES_MONITOR_ID", "agent_post_test")
    monkeypatch.setenv("ADVERSARIAL_REVIEWER_ID", "agent_adv_test")
    monkeypatch.setenv("CROSS_DOMAIN_SYNTHESIZER_ID", "agent_cds_test")
    monkeypatch.setenv("CHART_DESIGNER_ID", "agent_chart_test")
    monkeypatch.setenv("DREAM_AGENT_ID", "agent_dream_test")
    # Writing Agent joined the Coordinator's multiagent roster on
    # 2026-05-27 (PRE_PROVISIONED_AGENTS entry added in the same PR).
    # Without this, the writing_agent reconciliation gets the
    # "no WRITING_AGENT_ID in environment" skip branch and the multiagent
    # republish path drops it from the Coordinator's roster — the exact
    # P1 codex finding we're guarding against.
    monkeypatch.setenv("WRITING_AGENT_ID", "agent_writing_test")


def _make_fake_agent(
    version: int,
    tools: list,
    mcp_servers: list | None = None,
    multiagent: SimpleNamespace | None = None,
    skills: list | None = None,
):
    """Build a SimpleNamespace that mimics ``client.beta.agents.retrieve`` output.

    ``multiagent`` is None for sub-agents and a populated namespace for
    the Coordinator. ``skills`` defaults to empty so pre-Plan-#44 tests
    keep passing without modification.
    """
    return SimpleNamespace(
        version=version,
        tools=list(tools),
        mcp_servers=list(mcp_servers) if mcp_servers else [],
        multiagent=multiagent,
        skills=list(skills) if skills else [],
    )


def _make_fake_coordinator(version: int, pinned_subagents: dict[str, int]):
    """Build a Coordinator-shaped fake whose multiagent.agents pins ids→versions."""
    return SimpleNamespace(
        version=version,
        tools=[],
        mcp_servers=[],
        multiagent=SimpleNamespace(
            type="coordinator",
            agents=[
                SimpleNamespace(id=sub_id, type="agent", version=ver)
                for sub_id, ver in pinned_subagents.items()
            ],
        ),
    )


_AGENT_ID_TO_NAME = {
    "agent_stat_test": "statistician",
    "agent_pipe_test": "pipeline_monitor",
    "agent_sales_test": "sales_monitor",
    "agent_post_test": "postsales_monitor",
    "agent_adv_test": "adversarial_reviewer",
    "agent_cds_test": "cross_domain_synthesizer",
    "agent_chart_test": "chart_designer",
    "agent_writing_test": "writing_agent",
    "agent_quick_test": "quick_answer",
    "agent_dream_test": "dream",
}


def test_update_subagent_tools_routes_per_agent_targets(monkeypatch):
    """Each agent gets the right per-role roster.

    Iteration 3 contract: query agents (Statistician + 3 Monitors) get a
    data roster routed through dump_sf_query, never direct soqlQuery.
    Reasoning agents (Adversarial Reviewer + Cross-Domain Synthesizer)
    get the reasoning roster — no SF tools at all. Chart Designer keeps
    a variant of the reasoning roster that adds generate_chart.

    Kapa MCP addition (this PR): Post-Sales Monitor gains the Kapa
    toolset (for product/Jira change-context relevant to retention
    investigations) and Cross-Domain Synthesizer also gains it (to
    correlate product-side events with revenue patterns). Pipeline /
    Sales Monitors and Statistician stay Kapa-free — discovery showed
    the Acme Kapa index is engineering/product-heavy and adds
    little signal to revenue-flow investigations.
    """
    _set_ids(monkeypatch)
    monkeypatch.setenv("QUICK_ANSWER_ID", "agent_quick_test")
    import update_subagent_tools as ust  # type: ignore

    stub_tools = [{"type": "agent_toolset_20260401"}]
    update_calls: list[tuple[str, list]] = []

    def fake_retrieve(agent_id):
        return _make_fake_agent(version=3, tools=stub_tools)

    def fake_update(agent_id, **kwargs):
        update_calls.append((agent_id, kwargs["tools"]))
        return _make_fake_agent(
            version=kwargs["version"] + 1,
            tools=kwargs["tools"],
            mcp_servers=kwargs.get("mcp_servers", []),
            skills=kwargs.get("skills", []),
        )

    fake_client = MagicMock()
    fake_client.beta.agents.retrieve.side_effect = fake_retrieve
    fake_client.beta.agents.update.side_effect = fake_update
    monkeypatch.setattr(ust.anthropic, "Anthropic", lambda: fake_client)

    rc = ust.main()
    assert rc == 0
    # 10 entries in PRE_PROVISIONED_AGENTS now (7 multiagent-roster sub-agents
    # + writing_agent (joined roster 2026-05-27) + quick_answer + dream).
    assert len(update_calls) == 10

    by_agent = {aid: tools for aid, tools in update_calls}
    # Kapa-free data agents — Statistician + Pipeline / Sales Monitors get
    # the base data roster (no mcp_toolset).
    for data_aid in ("agent_stat_test", "agent_pipe_test", "agent_sales_test"):
        names = {t.get("name") or t.get("type") for t in by_agent[data_aid]}
        assert "dump_sf_query" in names, (
            f"{_AGENT_ID_TO_NAME[data_aid]} must have dump_sf_query — it's "
            "the only path to Salesforce after Iter3."
        )
        assert "query_artifact" in names
        assert "db_query" in names
        assert "agent_toolset_20260401" in names
        types = {t.get("type") for t in by_agent[data_aid]}
        assert "mcp_toolset" not in types, (
            f"{_AGENT_ID_TO_NAME[data_aid]} must not have any mcp_toolset "
            "— SF reads must route through dump_sf_query and Kapa is "
            "deliberately scoped away from this agent."
        )
    # Post-Sales Monitor — Kapa-aware data roster (dump_sf_query for SF +
    # search_knowledge_base custom tool for product/Jira change
    # context). Kapa was an mcp_toolset until 2026-05-13; after the pivot
    # to a custom-tool dispatch it lives in tools[] as type=custom.
    post_names = {t.get("name") or t.get("type") for t in by_agent["agent_post_test"]}
    assert "dump_sf_query" in post_names
    assert "search_knowledge_base" in post_names, (
        "Post-Sales Monitor must carry the Kapa custom tool — that's what "
        "lets it pull product/Jira change-context when investigating churn."
    )
    # Adversarial Reviewer — pure reasoning, no SF at all, no generate_chart, no Kapa.
    adv_names = {t.get("name") or t.get("type") for t in by_agent["agent_adv_test"]}
    adv_types = {t.get("type") for t in by_agent["agent_adv_test"]}
    assert "dump_sf_query" not in adv_names
    assert "generate_chart" not in adv_names
    assert "mcp_toolset" not in adv_types, (
        "Adversarial Reviewer challenges numbers — Kapa product context is "
        "not on its critical path."
    )
    # Cross-Domain Synthesizer — reasoning roster + Kapa (it correlates
    # revenue-side patterns with product-side events).
    cds_names = {t.get("name") or t.get("type") for t in by_agent["agent_cds_test"]}
    cds_types = {t.get("type") for t in by_agent["agent_cds_test"]}
    assert "dump_sf_query" not in cds_names, (
        "Cross-Domain Synthesizer must not see dump_sf_query — it reasons "
        "about findings, not raw rows."
    )
    assert "generate_chart" not in cds_names
    # Kapa was an mcp_toolset until 2026-05-13; now a custom tool.
    assert "search_knowledge_base" in cds_names, (
        "Cross-Domain Synthesizer must have Kapa — connecting product "
        "events with revenue patterns is exactly its job."
    )
    # Chart Designer — reasoning-shaped roster PLUS generate_chart.
    chart_names = {t.get("name") or t.get("type") for t in by_agent["agent_chart_test"]}
    assert "generate_chart" in chart_names, (
        "Chart Designer must keep generate_chart — otherwise the agent "
        "loses its primary tool and can't produce any chart output."
    )
    assert "dump_sf_query" not in chart_names
    assert "search_knowledge_base" not in chart_names
    # Quick Answer — agent_toolset + Kapa custom tool. Slack single-fact lookups
    # like "what is FATI?" or "what integrates with Acme?" resolve
    # directly via Kapa.
    qa_names = {t.get("name") or t.get("type") for t in by_agent["agent_quick_test"]}
    qa_types = {t.get("type") for t in by_agent["agent_quick_test"]}
    assert "agent_toolset_20260401" in qa_types
    assert "search_knowledge_base" in qa_names


def test_update_payload_contains_no_dollar_refs(monkeypatch):
    """No tool's ``input_schema`` may contain $ref or $defs.

    The Managed Agents custom-tool API rejects ``$ref`` on update.
    Pydantic-derived schemas emit them; ``build_tools_for_agent`` runs
    ``flatten_refs`` so they never reach the wire. Today the sub-agent
    rosters have only hand-written schemas (no refs), but future
    additions must inherit this guard automatically.
    """
    _set_ids(monkeypatch)
    import update_subagent_tools as ust  # type: ignore

    stub_tools = [{"type": "agent_toolset_20260401"}]
    update_calls: list[tuple[str, list]] = []

    def fake_retrieve(agent_id):
        return _make_fake_agent(version=3, tools=stub_tools)

    def fake_update(agent_id, *, version, tools, skills=None, mcp_servers=None):
        update_calls.append((agent_id, tools))
        return _make_fake_agent(version=version + 1, tools=tools)

    fake_client = MagicMock()
    fake_client.beta.agents.retrieve.side_effect = fake_retrieve
    fake_client.beta.agents.update.side_effect = fake_update
    monkeypatch.setattr(ust.anthropic, "Anthropic", lambda: fake_client)

    ust.main()
    for _aid, tools in update_calls:
        serialized = json.dumps(tools)
        assert "$ref" not in serialized
        assert "$defs" not in serialized


def test_update_subagent_tools_skips_when_id_missing(monkeypatch):
    """A missing env var → that agent is skipped, others still updated.

    Quick Answer is deliberately not set here — exercises the "skip when
    ID missing" path for the second sub-agent simultaneously with the
    same code path being hit for Statistician.
    """
    _set_ids(monkeypatch)
    monkeypatch.delenv("STATISTICIAN_ID", raising=False)
    monkeypatch.delenv("QUICK_ANSWER_ID", raising=False)

    import update_subagent_tools as ust  # type: ignore

    stub_tools = [{"type": "agent_toolset_20260401"}]
    retrieve_calls: list[str] = []
    update_calls: list[str] = []

    def fake_retrieve(agent_id):
        retrieve_calls.append(agent_id)
        return _make_fake_agent(version=5, tools=stub_tools)

    def fake_update(agent_id, **kwargs):
        update_calls.append(agent_id)
        return _make_fake_agent(
            version=kwargs["version"] + 1,
            tools=kwargs["tools"],
            mcp_servers=kwargs.get("mcp_servers", []),
            skills=kwargs.get("skills", []),
        )

    fake_client = MagicMock()
    fake_client.beta.agents.retrieve.side_effect = fake_retrieve
    fake_client.beta.agents.update.side_effect = fake_update
    monkeypatch.setattr(ust.anthropic, "Anthropic", lambda: fake_client)

    rc = ust.main()
    assert rc == 0

    expected_others = sorted(
        [
            "agent_pipe_test",
            "agent_sales_test",
            "agent_post_test",
            "agent_adv_test",
            "agent_cds_test",
            "agent_chart_test",
            "agent_writing_test",
            "agent_dream_test",
        ]
    )
    assert "agent_stat_test" not in retrieve_calls
    assert sorted(retrieve_calls) == expected_others
    assert sorted(update_calls) == expected_others


def test_update_subagent_tools_idempotent_when_tools_match(monkeypatch):
    """When live tools[] and mcp_servers already match the per-agent target,
    no update is issued. Live state must include the mcp_servers the target
    derives — for Post-Sales + Cross-Domain Synthesizer that's the Kapa
    server config; the "always set from target" path triggers an update
    if the live mcp_servers list disagrees with what the toolset requires.
    """
    _set_ids(monkeypatch)
    import update_subagent_tools as ust  # type: ignore

    update_calls: list[str] = []

    def fake_retrieve(agent_id):
        # Hand each agent its already-correct deploy roster + skills + mcp_servers.
        # All three must match to short-circuit the update — Bundle A added the
        # skills check, Kapa added the always-set mcp_servers check.
        entry = next(
            e for e in ust.PRE_PROVISIONED_AGENTS if _AGENT_ID_TO_NAME[agent_id] == e[0]
        )
        target_tools = entry[2]
        target_skills = entry[3] if len(entry) >= 4 else []
        deploy = ust.build_tools_for_agent(target_tools)
        return _make_fake_agent(
            version=9,
            tools=deploy,
            skills=target_skills,
            mcp_servers=ust._required_mcp_servers(deploy),
        )

    def fake_update(agent_id, **kwargs):
        update_calls.append(agent_id)
        return _make_fake_agent(
            version=kwargs["version"] + 1,
            tools=kwargs["tools"],
            mcp_servers=kwargs.get("mcp_servers", []),
            skills=kwargs.get("skills", []),
        )

    fake_client = MagicMock()
    fake_client.beta.agents.retrieve.side_effect = fake_retrieve
    fake_client.beta.agents.update.side_effect = fake_update
    monkeypatch.setattr(ust.anthropic, "Anthropic", lambda: fake_client)

    rc = ust.main()
    assert rc == 0
    assert update_calls == []


def test_update_subagent_tools_returns_nonzero_on_failure(monkeypatch):
    """If retrieve raises for one agent, main() returns 1 but still tries the others."""
    _set_ids(monkeypatch)
    monkeypatch.delenv("QUICK_ANSWER_ID", raising=False)
    import update_subagent_tools as ust  # type: ignore

    def fake_retrieve(agent_id):
        if agent_id == "agent_stat_test":
            raise RuntimeError("simulated API failure")
        return _make_fake_agent(version=1, tools=[{"type": "agent_toolset_20260401"}])

    update_calls: list[str] = []

    def fake_update(agent_id, **kwargs):
        update_calls.append(agent_id)
        return _make_fake_agent(
            version=kwargs["version"] + 1,
            tools=kwargs["tools"],
            mcp_servers=kwargs.get("mcp_servers", []),
            skills=kwargs.get("skills", []),
        )

    fake_client = MagicMock()
    fake_client.beta.agents.retrieve.side_effect = fake_retrieve
    fake_client.beta.agents.update.side_effect = fake_update
    monkeypatch.setattr(ust.anthropic, "Anthropic", lambda: fake_client)

    rc = ust.main()
    assert rc == 1

    # The other eight agents were still updated (six specialist sub-agents +
    # writing_agent (joined roster 2026-05-27) + Dream).
    assert sorted(update_calls) == sorted(
        [
            "agent_pipe_test",
            "agent_sales_test",
            "agent_post_test",
            "agent_adv_test",
            "agent_cds_test",
            "agent_chart_test",
            "agent_writing_test",
            "agent_dream_test",
        ]
    )


# ---------------------------------------------------------------------------
# Roster-shape assertions
# ---------------------------------------------------------------------------


def test_sub_agent_data_tools_has_no_sf_mcp_toolset():
    """Iteration 3 removes the Salesforce MCP toolset from the data roster.

    Sub-agents must route every SF read through dump_sf_query so the
    Parquet handle (not raw rows) lands in context. A regression that
    re-adds the SF MCP toolset would re-enable the 1.07M-token failure
    mode (sub-agent → Coordinator return surfaces raw rows).

    PR 11 (2026-05-14) added ``reasoning_summary`` to every agent so the
    expected count is now 5 (was 4 before).
    """
    import update_subagent_tools as ust  # type: ignore

    assert isinstance(ust.SUB_AGENT_DATA_TOOLS, list)
    assert len(ust.SUB_AGENT_DATA_TOOLS) == 5
    types = {t.get("type") for t in ust.SUB_AGENT_DATA_TOOLS}
    names = {t.get("name") for t in ust.SUB_AGENT_DATA_TOOLS if t.get("name")}
    assert "mcp_toolset" not in types, (
        "SUB_AGENT_DATA_TOOLS must not include any mcp_toolset entry — "
        "Iteration 3 routes all SF reads through dump_sf_query."
    )
    assert "dump_sf_query" in names
    assert "query_artifact" in names
    assert "db_query" in names
    assert "reasoning_summary" in names
    assert "agent_toolset_20260401" in types


def test_sub_agent_reasoning_tools_has_no_sf_tools_at_all():
    """Reasoning agents see neither the SF MCP toolset nor dump_sf_query.

    PR 11 (2026-05-14) added ``reasoning_summary`` to every agent so the
    expected count is now 4 (was 3 before).
    """
    import update_subagent_tools as ust  # type: ignore

    assert isinstance(ust.SUB_AGENT_REASONING_TOOLS, list)
    assert len(ust.SUB_AGENT_REASONING_TOOLS) == 4
    types = {t.get("type") for t in ust.SUB_AGENT_REASONING_TOOLS}
    names = {t.get("name") for t in ust.SUB_AGENT_REASONING_TOOLS if t.get("name")}
    assert "mcp_toolset" not in types
    assert "dump_sf_query" not in names, (
        "Reasoning agents (Adversarial / Cross-Domain / Chart) consume "
        "findings — they should not have a path to raw SF data."
    )
    assert "query_artifact" in names
    assert "db_query" in names
    assert "reasoning_summary" in names
    assert "agent_toolset_20260401" in types


def test_dream_agent_target_is_kapa_tools_no_salesforce_server(monkeypatch):
    """Dream's update payload carries DREAM_KAPA_TOOLS and no SF mcp_server.

    Live state (agent_EXAMPLE_dream observed 2026-05-14)
    carries a stale ``salesforce`` entry in ``mcp_servers[]`` from a
    prior create call. That stale registration caused a WATCH alert
    (sesn_EXAMPLE) when Dream tried ``soqlQuery`` and
    Plan #44's empty MCP_AUTO_APPROVE_ALLOWLIST denied the call. The
    reconciler must:

    * Hand the API the four tools in DREAM_KAPA_TOOLS:
      agent_toolset_20260401, db_query, query_artifact, and the Kapa
      custom tool (search_knowledge_base). No mcp_toolset
      entries — Kapa pivoted from MCP to a custom REST tool on
      2026-05-13, and Dream never had Salesforce access.
    * Pass mcp_servers=[] so the API drops the stale ``salesforce``
      registration on the next update.
    """
    _set_ids(monkeypatch)
    monkeypatch.delenv("QUICK_ANSWER_ID", raising=False)
    import setup_agents  # type: ignore
    import update_subagent_tools as ust  # type: ignore

    update_calls: list[dict] = []

    def fake_retrieve(agent_id):
        # Pretend every sub-agent has the live shape that motivated this
        # PR: agent_toolset placeholder + a stale Salesforce mcp_server.
        return _make_fake_agent(
            version=8,
            tools=[{"type": "agent_toolset_20260401"}],
            mcp_servers=[_fake_mcp_server("salesforce")],
        )

    def fake_update(agent_id, **kwargs):
        update_calls.append({"agent_id": agent_id, **kwargs})
        return _make_fake_agent(
            version=kwargs["version"] + 1,
            tools=kwargs["tools"],
            mcp_servers=kwargs.get("mcp_servers", []),
        )

    fake_client = MagicMock()
    fake_client.beta.agents.retrieve.side_effect = fake_retrieve
    fake_client.beta.agents.update.side_effect = fake_update
    monkeypatch.setattr(ust.anthropic, "Anthropic", lambda: fake_client)

    rc = ust.main()
    assert rc == 0

    dream_call = next(c for c in update_calls if c["agent_id"] == "agent_dream_test")
    # Tools[] must be byte-identical to DREAM_KAPA_TOOLS from setup_agents.
    assert dream_call["tools"] == setup_agents.DREAM_KAPA_TOOLS, (
        "Dream's update payload must carry DREAM_KAPA_TOOLS verbatim. "
        f"Got {dream_call['tools']!r}."
    )
    # mcp_servers must be empty — no SF MCP server entry, no Kapa server
    # entry (Kapa is now a custom tool, not an mcp_toolset). The stale
    # ``salesforce`` registration that triggered the WATCH alert must be
    # cleared.
    assert dream_call["mcp_servers"] == [], (
        "Dream's update payload must clear the stale Salesforce "
        f"mcp_server registration. Got {dream_call['mcp_servers']!r}."
    )
    # The deploy tool list must not contain an mcp_toolset of any kind —
    # Dream's Kapa access is via the custom tool, not an MCP server.
    types = {t.get("type") for t in dream_call["tools"]}
    assert "mcp_toolset" not in types, (
        "Dream's tools[] must contain no mcp_toolset entry. Kapa pivoted "
        "to a custom REST tool 2026-05-13. Got types={types!r}."
    )
    names = {t.get("name") or t.get("type") for t in dream_call["tools"]}
    assert names == {
        "agent_toolset_20260401",
        "db_query",
        "query_artifact",
        "reasoning_summary",
        "search_knowledge_base",
    }, f"Dream's tools[] names must match DREAM_KAPA_TOOLS. Got {names!r}."


def test_dream_not_added_to_coordinator_multiagent_roster(monkeypatch):
    """Updating Dream must NOT add it to the Coordinator's multiagent block.

    Dream runs stand-alone — the orchestrator dispatches it directly via
    ``scheduled_dream`` (see orchestrator/session_runner.py), never
    through the Coordinator's multiagent runtime. If the reconciler
    accidentally re-pinned Dream into the Coordinator's
    multiagent.agents block, two regressions follow:

    1. The Coordinator's prompt has no instructions for routing to
       Dream as a sub-agent, so dispatch behavior is undefined.
    2. Coordinator sessions would carry Dream's tools[] in their
       multiagent overhead, inflating token cost.

    Mechanism: ``main()`` only appends to
    ``sub_agent_ids_for_multiagent`` when the entry's
    ``in_multiagent_roster`` flag is True. Dream's entry has it set
    to False.
    """
    _set_ids(monkeypatch)
    monkeypatch.setenv("COORDINATOR_ID", "agent_coord_test")
    monkeypatch.delenv("QUICK_ANSWER_ID", raising=False)
    import update_subagent_tools as ust  # type: ignore

    # The Coordinator starts with the 8 in-roster sub-agents pinned (7
    # specialists + Writing Agent — Writing Agent joined the roster
    # 2026-05-27), NO dream pin. After main(), the multiagent re-pin
    # must still be those 8 — Dream's agent_id must not appear.
    coord_pinned_subagents = {
        "agent_stat_test": 8,
        "agent_pipe_test": 8,
        "agent_sales_test": 8,
        "agent_post_test": 8,
        "agent_adv_test": 8,
        "agent_cds_test": 8,
        "agent_chart_test": 8,
        "agent_writing_test": 8,
    }
    coord_state = {"version": 31}

    def fake_retrieve(agent_id):
        if agent_id == "agent_coord_test":
            return _make_fake_coordinator(
                version=coord_state["version"],
                pinned_subagents=coord_pinned_subagents,
            )
        # Every sub-agent (including Dream) has a tools[] mismatch so
        # update_one() runs and the agent_id lands in this-run updates.
        return _make_fake_agent(
            version=10,
            tools=[{"type": "agent_toolset_20260401"}],
        )

    multiagent_payloads: list[dict] = []

    def fake_update(agent_id, **kwargs):
        if agent_id == "agent_coord_test":
            coord_state["version"] += 1
            if "multiagent" in kwargs:
                multiagent_payloads.append(kwargs["multiagent"])
            return _make_fake_agent(
                version=coord_state["version"],
                tools=kwargs.get("tools", []),
            )
        return _make_fake_agent(
            version=kwargs["version"] + 1,
            tools=kwargs.get("tools", []),
            mcp_servers=kwargs.get("mcp_servers", []),
        )

    fake_client = MagicMock()
    fake_client.beta.agents.retrieve.side_effect = fake_retrieve
    fake_client.beta.agents.update.side_effect = fake_update
    monkeypatch.setattr(ust.anthropic, "Anthropic", lambda: fake_client)

    rc = ust.main()
    assert rc == 0

    # Exactly one multiagent re-publish, and Dream's id is NOT in it.
    assert len(multiagent_payloads) == 1
    submitted_ids = {entry["id"] for entry in multiagent_payloads[0]["agents"]}
    assert "agent_dream_test" not in submitted_ids, (
        "Dream's agent_id must NOT land in the Coordinator's multiagent "
        f"roster. Got {submitted_ids!r}."
    )
    # The 7 normal sub-agents remain in the roster.
    assert submitted_ids == set(coord_pinned_subagents.keys())


def test_dream_entry_flagged_out_of_multiagent_roster():
    """Dream's PRE_PROVISIONED_AGENTS entry sets in_multiagent_roster=False.

    Architectural assertion: the 5-tuple's last element is the gate
    that keeps Dream out of the Coordinator multiagent re-pin. If a
    future refactor drops the flag, this test fails and the breaker
    has to read the docstring on ``_unpack_entry`` before silencing.
    """
    import update_subagent_tools as ust  # type: ignore

    dream_entry = next(e for e in ust.PRE_PROVISIONED_AGENTS if e[0] == "dream")
    name, env_var, _tools, _skills, in_multiagent_roster = ust._unpack_entry(
        dream_entry
    )
    assert name == "dream"
    assert env_var == "DREAM_AGENT_ID"
    assert in_multiagent_roster is False, (
        "Dream is dispatched stand-alone via scheduled_dream — it must "
        "NOT join the Coordinator's multiagent.agents roster. See the "
        "PRE_PROVISIONED_AGENTS comment for the routing rationale."
    )

    # And the back-compat shim treats 4-tuples as in_multiagent_roster=True.
    legacy_entry = ("statistician", "STATISTICIAN_ID", [], [])
    _, _, _, _, in_roster_default = ust._unpack_entry(legacy_entry)
    assert in_roster_default is True


def test_pre_provisioned_agents_maps_targets_per_role():
    """Each sub-agent is mapped to the correct per-role roster.

    Three groups after the Kapa integration:

    * **Data role, Kapa-free**: Statistician, Pipeline Monitor, Sales
      Monitor → ``SUB_AGENT_DATA_TOOLS`` (dump_sf_query for SF, no
      mcp_toolset of any kind). Kapa was scoped away from these agents
      after discovery showed the Acme Kapa index is engineering /
      product-heavy and adds little signal to revenue-flow work.

    * **Data role + Kapa**: Post-Sales Monitor →
      ``SUB_AGENT_DATA_TOOLS_WITH_KAPA``. The Kapa toolset surfaces
      product/Jira change-context relevant to retention investigations.

    * **Reasoning role, Kapa-free**: Adversarial Reviewer →
      ``SUB_AGENT_REASONING_TOOLS``. It challenges numbers; product
      context is not on its critical path.

    * **Reasoning role + Kapa**: Cross-Domain Synthesizer →
      ``SUB_AGENT_REASONING_TOOLS_WITH_KAPA``. Connecting product-side
      events with revenue-side patterns is its core function.

    * **Chart role**: Chart Designer → ``CHART_DESIGNER_TOOLS``
      (reasoning shape + generate_chart).

    * **Quick Answer**: ``QUICK_ANSWER_KAPA_TOOLS`` (agent toolset +
      Kapa). Single-fact Slack lookups like "what is FATI?" resolve
      directly via Kapa.

    This test guards against a regression in scope assignments — the
    Kapa index map (docs/research/kapa-acme-index.md) drove these
    choices.
    """
    import setup_agents  # type: ignore
    import update_subagent_tools as ust  # type: ignore

    # Plan #44 changed the tuple shape to (name, env, tools, skills); be
    # tolerant of both shapes so any future re-extension lands cleanly.
    # Kapa MCP integration narrowed the Kapa-free data agents down to three
    # — Post-Sales Monitor moved onto SUB_AGENT_DATA_TOOLS_WITH_KAPA.
    targets = {entry[0]: entry[2] for entry in ust.PRE_PROVISIONED_AGENTS}
    for data_agent in ("pipeline_monitor", "sales_monitor"):
        assert targets[data_agent] is ust.SUB_AGENT_DATA_TOOLS, (
            f"{data_agent} must be on SUB_AGENT_DATA_TOOLS — it queries "
            "Salesforce and must route through dump_sf_query. Kapa was "
            "deliberately scoped away from this agent."
        )
    # Statistician uses STATISTICIAN_TOOLS — the same data tools but pinned to
    # vault_path=False, so it NEVER gets the SF MCP toolset (every SF read goes
    # through the dump_sf_query custom tool, matching its system prompt). This
    # is the vault-flag regression guard: a reconciler run with
    # SF_MCP_VIA_VAULT=true must not re-add the Salesforce MCP toolset here.
    assert targets["statistician"] is ust.STATISTICIAN_TOOLS, (
        "Statistician must be on STATISTICIAN_TOOLS (vault_path=False, no SF "
        "MCP toolset) — it reads SF only through dump_sf_query."
    )
    assert (
        targets["postsales_monitor"] is setup_agents.SUB_AGENT_DATA_TOOLS_WITH_KAPA
    ), (
        "Post-Sales Monitor must use SUB_AGENT_DATA_TOOLS_WITH_KAPA — "
        "investigating retention benefits from Kapa product/Jira context."
    )
    assert targets["adversarial_reviewer"] is ust.SUB_AGENT_REASONING_TOOLS, (
        "Adversarial Reviewer must use SUB_AGENT_REASONING_TOOLS — it "
        "reasons over findings, not raw SF rows, and Kapa is not on its "
        "critical path."
    )
    assert (
        targets["cross_domain_synthesizer"]
        is setup_agents.SUB_AGENT_REASONING_TOOLS_WITH_KAPA
    ), (
        "Cross-Domain Synthesizer must use "
        "SUB_AGENT_REASONING_TOOLS_WITH_KAPA — connecting product "
        "events with revenue patterns is its job."
    )
    assert targets["chart_designer"] is setup_agents.CHART_DESIGNER_TOOLS, (
        "Chart Designer needs the chart-specific roster (REASONING shape + "
        "generate_chart). The plain reasoning roster would strip its primary tool."
    )
    assert targets["quick_answer"] is setup_agents.QUICK_ANSWER_KAPA_TOOLS, (
        "Quick Answer must use QUICK_ANSWER_KAPA_TOOLS — its primary job is "
        "single-fact Slack lookups, which Kapa serves directly."
    )


def test_pre_provisioned_agents_includes_all_reconciled_agents():
    """All reconciled agents must appear in PRE_PROVISIONED_AGENTS.

    Any agent missing from PRE_PROVISIONED_AGENTS is invisible to CI
    — its tools[] is whatever setup_agents.py installed at provisioning
    time, frozen forever. The original Iter3 gap was that the three
    Monitors were provisioned with the old SF-MCP roster and silently
    kept it for weeks. Quick Answer was added later — it had been
    pre-provisioned but never reconciled by CI, leaving its Kapa toolset
    addition dependent on a manual `setup_agents.py` re-run. Dream
    Agent was added 2026-05-14 after a WATCH alert showed it carrying
    a stale Salesforce mcp_server registration from a prior create call
    (sesn_EXAMPLE). Catch any future agent added to
    setup_agents.py but forgotten here.
    """
    import update_subagent_tools as ust  # type: ignore

    names = {entry[0] for entry in ust.PRE_PROVISIONED_AGENTS}
    expected = {
        "statistician",
        "pipeline_monitor",
        "sales_monitor",
        "postsales_monitor",
        "adversarial_reviewer",
        "cross_domain_synthesizer",
        "chart_designer",
        "writing_agent",
        "quick_answer",
        "dream",
    }
    assert names == expected, (
        f"PRE_PROVISIONED_AGENTS missing {expected - names} or has unexpected "
        f"{names - expected}. Every reconciled agent in setup_agents.py must "
        "be here or its tools[] will drift from source on every deploy."
    )


def test_pre_provisioned_agents_env_var_names_match_update_prompts_pattern():
    """Each entry uses the same ID env var name update_prompts.AGENTS reads.

    setup_agents.py prints the IDs as ``{NAME}_ID=...``. update_prompts.AGENTS
    hardcodes those IDs by name. The env var used in PRE_PROVISIONED_AGENTS
    must match the convention so a rotation that switches an agent ID over
    to env-resolved (the WRITING_AGENT_ID pattern) is read correctly here too.
    """
    import update_subagent_tools as ust  # type: ignore

    expected_env_vars = {
        "statistician": "STATISTICIAN_ID",
        "pipeline_monitor": "PIPELINE_MONITOR_ID",
        "sales_monitor": "SALES_MONITOR_ID",
        "postsales_monitor": "POSTSALES_MONITOR_ID",
        "adversarial_reviewer": "ADVERSARIAL_REVIEWER_ID",
        "cross_domain_synthesizer": "CROSS_DOMAIN_SYNTHESIZER_ID",
        "chart_designer": "CHART_DESIGNER_ID",
        "writing_agent": "WRITING_AGENT_ID",
        "quick_answer": "QUICK_ANSWER_ID",
        "dream": "DREAM_AGENT_ID",
    }
    actual_env_vars = {entry[0]: entry[1] for entry in ust.PRE_PROVISIONED_AGENTS}
    assert actual_env_vars == expected_env_vars


def test_build_tools_for_agent_flattens_dollar_refs():
    """A tool whose input_schema contains $ref is flattened before deploy."""
    import update_subagent_tools as ust  # type: ignore

    tool_with_ref = {
        "type": "custom",
        "name": "synthetic",
        "input_schema": {
            "type": "object",
            "properties": {"item": {"$ref": "#/$defs/Thing"}},
            "$defs": {"Thing": {"type": "string"}},
        },
    }
    out = ust.build_tools_for_agent([tool_with_ref])
    serialized = json.dumps(out)
    assert "$ref" not in serialized
    assert "$defs" not in serialized
    assert out[0]["input_schema"]["properties"]["item"] == {"type": "string"}


def test_build_tools_for_agent_passes_through_tools_without_refs():
    """Hand-written schemas (no refs) pass through identically."""
    import update_subagent_tools as ust  # type: ignore

    out = ust.build_tools_for_agent(ust.SUB_AGENT_DATA_TOOLS)
    # build_tools_for_agent rebuilds dicts only for tools it flattens; the
    # rest must be the original objects (identity preserved).
    for original, deployed in zip(ust.SUB_AGENT_DATA_TOOLS, out):
        assert deployed is original


# ---------------------------------------------------------------------------
# mcp_servers auto-clear — when the target roster has no mcp_toolset but
# the live agent still carries an mcp_servers entry, the API rejects the
# update with "mcp_servers [X] declared but no mcp_toolset in tools
# references them". update_one() detects this and passes mcp_servers=[]
# alongside the tools update so the deploy self-heals.
# ---------------------------------------------------------------------------


def _fake_mcp_server(name: str):
    """Stand-in for BetaManagedAgentsMCPServerURLDefinition."""
    return SimpleNamespace(name=name, type="url", url=f"https://example/{name}")


def test_update_sets_mcp_servers_from_target_kapa_added(monkeypatch):
    """update_one always clears stale mcp_servers when target has no mcp_toolset.

    Iter3's original behavior was "only CLEAR mcp_servers when target has
    no mcp_toolset," which silently broke ADDITIONS — a new mcp_toolset
    entry was deployed without the matching mcp_server, and the API
    rejected the update. The fix: derive required mcp_servers from the
    target tools and set them on every update.

    Post-2026-05-13, Kapa pivoted from an mcp_toolset to a custom tool, so
    no sub-agent currently has any mcp_toolset entries — every target
    therefore produces mcp_servers=[] and the update clears any stale SF
    registration in lockstep with the tools[] update.
    """
    _set_ids(monkeypatch)
    monkeypatch.delenv("QUICK_ANSWER_ID", raising=False)
    import update_subagent_tools as ust  # type: ignore

    update_calls: list[dict] = []

    def fake_retrieve(agent_id):
        # Pretend each agent has only agent_toolset (mismatch on tools) AND
        # a stale Salesforce mcp_server registry entry.
        return _make_fake_agent(
            version=5,
            tools=[{"type": "agent_toolset_20260401"}],
            mcp_servers=[_fake_mcp_server("salesforce")],
        )

    def fake_update(agent_id, **kwargs):
        update_calls.append({"agent_id": agent_id, **kwargs})
        return _make_fake_agent(
            version=kwargs["version"] + 1,
            tools=kwargs["tools"],
            mcp_servers=kwargs.get("mcp_servers", []),
        )

    fake_client = MagicMock()
    fake_client.beta.agents.retrieve.side_effect = fake_retrieve
    fake_client.beta.agents.update.side_effect = fake_update
    monkeypatch.setattr(ust.anthropic, "Anthropic", lambda: fake_client)

    rc = ust.main()
    assert rc == 0
    by_aid = {c["agent_id"]: c for c in update_calls}
    # Every sub-agent: target has no mcp_toolset (Kapa is a custom tool
    # after the 2026-05-13 pivot), so update sets mcp_servers=[] and the
    # stale Salesforce registration is cleared.
    for aid in (
        "agent_stat_test",
        "agent_pipe_test",
        "agent_sales_test",
        "agent_adv_test",
        "agent_chart_test",
        "agent_post_test",
        "agent_cds_test",
        "agent_dream_test",
    ):
        assert by_aid[aid]["mcp_servers"] == [], (
            f"{aid} target has no mcp_toolset → update must set mcp_servers=[]"
        )


def test_update_sets_mcp_servers_to_empty_when_already_empty(monkeypatch):
    """If live mcp_servers is empty and target has no mcp_toolset, set [] on update.

    The new "always set mcp_servers from target" path passes the derived
    list (possibly empty) on every update. This documents that behavior —
    the update payload always carries an explicit mcp_servers field, even
    when both sides agree it should be empty.
    """
    _set_ids(monkeypatch)
    monkeypatch.delenv("QUICK_ANSWER_ID", raising=False)
    import update_subagent_tools as ust  # type: ignore

    update_calls: list[dict] = []

    def fake_retrieve(agent_id):
        # Live state: mismatched tools, but mcp_servers already clear.
        return _make_fake_agent(
            version=5,
            tools=[{"type": "agent_toolset_20260401"}],
            mcp_servers=[],
        )

    def fake_update(agent_id, **kwargs):
        update_calls.append({"agent_id": agent_id, **kwargs})
        return _make_fake_agent(
            version=kwargs["version"] + 1,
            tools=kwargs["tools"],
            mcp_servers=kwargs.get("mcp_servers", []),
        )

    fake_client = MagicMock()
    fake_client.beta.agents.retrieve.side_effect = fake_retrieve
    fake_client.beta.agents.update.side_effect = fake_update
    monkeypatch.setattr(ust.anthropic, "Anthropic", lambda: fake_client)

    rc = ust.main()
    assert rc == 0
    # Every update call carries an explicit mcp_servers field — the new
    # "always set from target" behavior. For Kapa-free agents the list
    # is empty; for Kapa-enabled agents it has the Kapa server.
    for call in update_calls:
        assert "mcp_servers" in call, (
            f"Expected explicit mcp_servers in update for {call['agent_id']} — "
            "update_one() always derives the required list from target tools."
        )


def test_required_mcp_servers_helper():
    """_required_mcp_servers returns the right server list for each target.

    Replaces the old _target_has_mcp_toolset helper. The new helper returns
    the actual server config list (not just a boolean) so update_one() can
    pass it directly to agents.update(mcp_servers=...).

    Post-2026-05-13, Kapa pivoted from mcp_toolset to custom tool. None of
    the in-use SUB_AGENT_* tool sets carry an mcp_toolset entry today, so
    every helper call returns an empty server list. This test pins the
    contract for whatever the live tool sets are (cleanly empty today;
    will document any future mcp_toolset addition the moment one lands).
    """
    import setup_agents  # type: ignore
    import update_subagent_tools as ust  # type: ignore

    # No mcp_toolset in any of the standard reasoning/data targets after
    # the Kapa custom-tool pivot.
    assert ust._required_mcp_servers(setup_agents.SUB_AGENT_DATA_TOOLS) == []
    assert ust._required_mcp_servers(setup_agents.SUB_AGENT_REASONING_TOOLS) == []
    # Hand-crafted mcp_toolset entry pointing at the only registered server
    # in MCP_SERVERS_BY_NAME (Salesforce) — proves the helper still wires
    # up registered toolsets correctly.
    fake_target_with_toolset = [
        {"type": "agent_toolset_20260401"},
        {"type": "mcp_toolset", "mcp_server_name": setup_agents.SF_MCP_SERVER["name"]},
    ]
    servers = ust._required_mcp_servers(fake_target_with_toolset)
    assert len(servers) == 1
    assert servers[0]["name"] == setup_agents.SF_MCP_SERVER["name"]
    # Unknown server names are filtered out — the helper only returns
    # entries registered in MCP_SERVERS_BY_NAME.
    fake_unknown = [
        {"type": "mcp_toolset", "mcp_server_name": "some_future_server"},
    ]
    assert ust._required_mcp_servers(fake_unknown) == []


# ---------------------------------------------------------------------------
# Pin-file refresh — closes the deploy-prompts.yml ordering hole. The
# workflow runs update_prompts.py (which writes active_versions.json
# from the post-prompt-update live state) FIRST, then update_subagent_tools.py.
# Tool updates can bump live versions further. Without the refresh below
# the pin file would be stale and the workflow's inline verify gate
# (verify_active_versions.py) would 4xx comparing live > pinned. The
# refresh merges the new tool-update versions back into the pin so the
# subsequent commit + verify see the actual live state.
# ---------------------------------------------------------------------------


def test_main_refreshes_pin_file_after_tool_updates(monkeypatch, tmp_path):
    """When tools[] updates bump live versions, the pin file is refreshed."""
    _set_ids(monkeypatch)
    monkeypatch.delenv("QUICK_ANSWER_ID", raising=False)
    import update_subagent_tools as ust  # type: ignore

    pin_file = tmp_path / "active_versions.json"
    pin_file.write_text(
        json.dumps(
            {
                # Other agents the pin tracks but this script doesn't touch.
                "coordinator": 31,
                "quick_answer": 13,
                # Pre-bump versions for the 9 in scope (7 specialist sub-agents +
                # writing_agent (joined roster 2026-05-27) + dream).
                "statistician": 13,
                "pipeline_monitor": 9,
                "sales_monitor": 9,
                "postsales_monitor": 9,
                "adversarial_reviewer": 7,
                "cross_domain_synthesizer": 4,
                "chart_designer": 7,
                "writing_agent": 6,
                "dream": 8,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    monkeypatch.setattr(ust, "ACTIVE_VERSIONS_PATH", pin_file)

    # Live state: tools mismatch → forces update. Each update bumps the
    # version by 1, so the pin file should advance by +1 for all 9.
    def fake_retrieve(agent_id):
        return _make_fake_agent(
            version={
                "agent_stat_test": 13,
                "agent_pipe_test": 9,
                "agent_sales_test": 9,
                "agent_post_test": 9,
                "agent_adv_test": 7,
                "agent_cds_test": 4,
                "agent_chart_test": 7,
                "agent_writing_test": 6,
                "agent_dream_test": 8,
            }[agent_id],
            tools=[{"type": "agent_toolset_20260401"}],
        )

    def fake_update(agent_id, **kwargs):
        return _make_fake_agent(
            version=kwargs["version"] + 1,
            tools=kwargs["tools"],
        )

    fake_client = MagicMock()
    fake_client.beta.agents.retrieve.side_effect = fake_retrieve
    fake_client.beta.agents.update.side_effect = fake_update
    monkeypatch.setattr(ust.anthropic, "Anthropic", lambda: fake_client)

    rc = ust.main()
    assert rc == 0

    refreshed = json.loads(pin_file.read_text())
    # The nine reconciled agents (7 specialist sub-agents + writing_agent +
    # dream) advanced by 1. writing_agent joined the canonical roster
    # 2026-05-27 with the multiagent migration.
    assert refreshed["statistician"] == 14
    assert refreshed["pipeline_monitor"] == 10
    assert refreshed["sales_monitor"] == 10
    assert refreshed["postsales_monitor"] == 10
    assert refreshed["adversarial_reviewer"] == 8
    assert refreshed["cross_domain_synthesizer"] == 5
    assert refreshed["chart_designer"] == 8
    assert refreshed["writing_agent"] == 7
    assert refreshed["dream"] == 9
    # Entries the script doesn't touch must be preserved byte-for-byte.
    assert refreshed["coordinator"] == 31
    assert refreshed["quick_answer"] == 13


def test_main_no_pin_rewrite_when_all_unchanged(monkeypatch, tmp_path):
    """If every agent is already at target, the pin file is not rewritten.

    Idempotency — re-running the deploy on an already-in-sync repo
    should not produce a phantom commit. The previous (pre-fix) behavior
    would have made the workflow's auto-commit step think the file was
    dirty even when nothing actually changed.
    """
    _set_ids(monkeypatch)
    monkeypatch.delenv("QUICK_ANSWER_ID", raising=False)
    import update_subagent_tools as ust  # type: ignore

    pin_file = tmp_path / "active_versions.json"
    initial_payload = (
        json.dumps(
            {
                "statistician": 14,
                "pipeline_monitor": 10,
                "sales_monitor": 10,
                "postsales_monitor": 10,
                "adversarial_reviewer": 8,
                "cross_domain_synthesizer": 5,
                "chart_designer": 8,
                "writing_agent": 7,
                "dream": 9,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    pin_file.write_text(initial_payload)
    pre_mtime = pin_file.stat().st_mtime_ns
    monkeypatch.setattr(ust, "ACTIVE_VERSIONS_PATH", pin_file)

    def fake_retrieve(agent_id):
        entry = next(
            e for e in ust.PRE_PROVISIONED_AGENTS if _AGENT_ID_TO_NAME[agent_id] == e[0]
        )
        target = entry[2]
        target_skills = entry[3] if len(entry) >= 4 else []
        deploy = ust.build_tools_for_agent(target)
        # Live state must also match the target's mcp_servers — otherwise the
        # new "always set from target" logic would trigger an update. For
        # Kapa-enabled agents (Post-Sales + Cross-Domain), that's the Kapa
        # server config; for Kapa-free agents it's an empty list.
        target_servers = ust._required_mcp_servers(deploy)
        version_for = {
            "agent_stat_test": 14,
            "agent_pipe_test": 10,
            "agent_sales_test": 10,
            "agent_post_test": 10,
            "agent_adv_test": 8,
            "agent_cds_test": 5,
            "agent_chart_test": 8,
            "agent_writing_test": 7,
            "agent_dream_test": 9,
        }[agent_id]
        return _make_fake_agent(
            version=version_for,
            tools=deploy,
            skills=target_skills,
            mcp_servers=target_servers,
        )

    fake_client = MagicMock()
    fake_client.beta.agents.retrieve.side_effect = fake_retrieve

    def _fail_on_update(*a, **k):
        raise AssertionError(
            "update_one should not call .update() when tools already match"
        )

    fake_client.beta.agents.update.side_effect = _fail_on_update
    monkeypatch.setattr(ust.anthropic, "Anthropic", lambda: fake_client)

    rc = ust.main()
    assert rc == 0
    assert pin_file.read_text() == initial_payload
    # Mtime check — write_text rewrites even when content is identical,
    # so a missing rewrite is also a missing mtime advance.
    assert pin_file.stat().st_mtime_ns == pre_mtime


def test_main_skips_pin_refresh_when_only_failures(monkeypatch, tmp_path):
    """If every agent fails, the pin file is left alone.

    A failed update returns ``new_version=None`` and is excluded from the
    refresh map, so the pin file never sees a write at all. This keeps
    a half-deploy from poisoning the pin with arbitrary versions.
    """
    _set_ids(monkeypatch)
    import update_subagent_tools as ust  # type: ignore

    pin_file = tmp_path / "active_versions.json"
    pin_file.write_text(
        json.dumps({"statistician": 14}, indent=2, sort_keys=True) + "\n"
    )
    pre_mtime = pin_file.stat().st_mtime_ns
    monkeypatch.setattr(ust, "ACTIVE_VERSIONS_PATH", pin_file)

    def fake_retrieve(agent_id):
        raise RuntimeError("simulated API outage")

    fake_client = MagicMock()
    fake_client.beta.agents.retrieve.side_effect = fake_retrieve
    monkeypatch.setattr(ust.anthropic, "Anthropic", lambda: fake_client)

    rc = ust.main()
    # Failures should still cause a non-zero exit.
    assert rc == 1
    # Pin file untouched.
    assert pin_file.stat().st_mtime_ns == pre_mtime


# ---------------------------------------------------------------------------
# Idempotency — _tools_match must accept the API's extra default fields.
# ---------------------------------------------------------------------------


def test_tools_match_treats_target_as_subset_of_live():
    """Live tools[] from the API has API-side defaults the target doesn't.

    Before the subset fix, ``_tools_match`` did dict-equality on each
    tool body, so a target like ``{"type": "agent_toolset_20260401"}``
    did NOT match a live tool that came back as ``{"type": "agent_toolset_20260401",
    "configs": [], "default_config": {"enabled": True, "permission_policy": {...}}}``.
    Every script run issued a no-op update and bumped the version,
    which is what caused the Coordinator's multiagent pin to drift so
    far past live in the first place (every CI run was pushing new
    sub-agent versions that the Coordinator's pin never advanced to).
    """
    import update_subagent_tools as ust  # type: ignore

    target = [
        {"type": "agent_toolset_20260401"},
        {
            "type": "custom",
            "name": "db_query",
            "input_schema": {
                "type": "object",
                "properties": {"sql": {"type": "string"}},
            },
        },
    ]
    live = [
        {
            "type": "agent_toolset_20260401",
            "configs": [],
            "default_config": {
                "enabled": True,
                "permission_policy": {"type": "always_allow"},
            },
        },
        {
            "type": "custom",
            "name": "db_query",
            "input_schema": {
                "type": "object",
                "properties": {"sql": {"type": "string"}},
                "additionalProperties": False,  # API-side default.
            },
            "configs": [],
            "default_config": {"enabled": True},
        },
    ]
    assert ust._tools_match(live, target), (
        "Live tool dict with API-side default fields should match a target "
        "tool that only specifies the spec keys."
    )


def test_tools_match_rejects_missing_target_key_in_live():
    """If the target says ``"name": "X"`` but live has ``"name": "Y"``, no match."""
    import update_subagent_tools as ust  # type: ignore

    target = [{"type": "custom", "name": "want", "input_schema": {"type": "object"}}]
    live = [{"type": "custom", "name": "got", "input_schema": {"type": "object"}}]
    assert not ust._tools_match(live, target)


def test_tools_match_rejects_diverging_input_schema():
    """A target input_schema must be a subset of live; a diverging key fails."""
    import update_subagent_tools as ust  # type: ignore

    target = [
        {
            "type": "custom",
            "name": "x",
            "input_schema": {
                "type": "object",
                "properties": {"sql": {"type": "integer"}},
            },
        }
    ]
    live = [
        {
            "type": "custom",
            "name": "x",
            "input_schema": {
                "type": "object",
                "properties": {"sql": {"type": "string"}},
            },
        }
    ]
    # The target's sql property says type=integer; live says type=string.
    # _subset({"type":"integer"}, {"type":"string"}) → live["type"]="string" != target["type"]="integer"
    # so the input_schema check fails.
    assert not ust._tools_match(live, target)


# ---------------------------------------------------------------------------
# Coordinator multiagent re-publish — closes the Iter3 dead-letter hole.
# Sub-agent updates were never reaching production traffic because the
# Coordinator's multiagent.agents block pins sub-agent versions at
# parent-update time and never auto-advances. This block re-issues an
# agents.update(coord_id, multiagent={...}) so the API re-snapshots
# each sub-agent at its current version.
# ---------------------------------------------------------------------------


def test_republish_coordinator_skips_when_no_coordinator_id(monkeypatch):
    """No COORDINATOR_ID in environment → skip cleanly, never call retrieve."""
    _set_ids(monkeypatch)  # already deletes COORDINATOR_ID
    import update_subagent_tools as ust  # type: ignore

    fake_client = MagicMock()
    status, version = ust.republish_coordinator_multiagent(
        fake_client, ["agent_stat_test", "agent_pipe_test"]
    )
    assert status == "skipped"
    assert version is None
    fake_client.beta.agents.retrieve.assert_not_called()
    fake_client.beta.agents.update.assert_not_called()


def test_republish_coordinator_repins_drift(monkeypatch):
    """When live sub-agent versions diverge from pinned, repin via agents.update."""
    _set_ids(monkeypatch)  # populate canonical-roster env vars for the new gate
    monkeypatch.setenv("COORDINATOR_ID", "agent_coord_test")
    import update_subagent_tools as ust  # type: ignore

    # Coord's multiagent pins stat at v8, pipe at v6 (the Iter3 dead-letter state).
    # Live: stat at v18, pipe at v12.
    coord_fake = _make_fake_coordinator(
        version=31,
        pinned_subagents={"agent_stat_test": 8, "agent_pipe_test": 6},
    )
    live_versions = {"agent_stat_test": 18, "agent_pipe_test": 12}

    def fake_retrieve(agent_id):
        if agent_id == "agent_coord_test":
            return coord_fake
        return _make_fake_agent(version=live_versions[agent_id], tools=[])

    update_calls: list[dict] = []

    def fake_update(agent_id, **kwargs):
        update_calls.append({"agent_id": agent_id, **kwargs})
        return _make_fake_agent(
            version=kwargs["version"] + 1, tools=[], multiagent=coord_fake.multiagent
        )

    fake_client = MagicMock()
    fake_client.beta.agents.retrieve.side_effect = fake_retrieve
    fake_client.beta.agents.update.side_effect = fake_update

    status, version = ust.republish_coordinator_multiagent(
        fake_client, ["agent_stat_test", "agent_pipe_test"]
    )
    assert status == "updated"
    assert version == 32
    assert len(update_calls) == 1
    payload = update_calls[0]
    assert payload["agent_id"] == "agent_coord_test"
    assert payload["version"] == 31
    multiagent = payload["multiagent"]
    assert multiagent["type"] == "coordinator"
    assert multiagent["agents"] == [
        {"type": "agent", "id": "agent_stat_test"},
        {"type": "agent", "id": "agent_pipe_test"},
    ]


def test_republish_coordinator_skips_when_pins_match_live(monkeypatch):
    """When every pinned sub-agent already matches live, no update is issued."""
    _set_ids(monkeypatch)
    monkeypatch.setenv("COORDINATOR_ID", "agent_coord_test")
    import update_subagent_tools as ust  # type: ignore

    coord_fake = _make_fake_coordinator(
        version=31,
        pinned_subagents={"agent_stat_test": 18, "agent_pipe_test": 12},
    )

    def fake_retrieve(agent_id):
        if agent_id == "agent_coord_test":
            return coord_fake
        return _make_fake_agent(
            version={"agent_stat_test": 18, "agent_pipe_test": 12}[agent_id], tools=[]
        )

    fake_client = MagicMock()
    fake_client.beta.agents.retrieve.side_effect = fake_retrieve

    def _fail_on_update(*a, **k):
        raise AssertionError(
            "republish must not issue agents.update when every pin matches live"
        )

    fake_client.beta.agents.update.side_effect = _fail_on_update

    status, version = ust.republish_coordinator_multiagent(
        fake_client, ["agent_stat_test", "agent_pipe_test"]
    )
    assert status == "unchanged"
    assert version == 31


def test_republish_coordinator_returns_failed_on_api_error(monkeypatch):
    """Coord retrieve raises → 'failed' status, never tries to update."""
    monkeypatch.setenv("COORDINATOR_ID", "agent_coord_test")
    import update_subagent_tools as ust  # type: ignore

    def fake_retrieve(agent_id):
        raise RuntimeError("simulated API outage")

    fake_client = MagicMock()
    fake_client.beta.agents.retrieve.side_effect = fake_retrieve

    status, version = ust.republish_coordinator_multiagent(
        fake_client, ["agent_stat_test"]
    )
    assert status == "failed"
    assert version is None
    fake_client.beta.agents.update.assert_not_called()


def test_republish_coordinator_preserves_skipped_agents_in_pin_roster(monkeypatch):
    """Skipped-due-to-missing-env agents stay pinned in the Coordinator's roster.

    Plan #44 review concern MEDIUM #3. The closing review caught this:
    when a sub-agent is skipped in this run because its ID env var is
    missing, it never lands in ``sub_agent_ids_for_multiagent``. Before
    the fix the republish call would pass ONLY the updated list to the
    API, and the API CLEARS missing agents from the pinned roster — so
    the next Coordinator session has no Statistician (or whichever
    agent was skipped) in multiagent.agents and dispatch fails.

    Expected behavior: union the current pinned roster with the updated
    IDs, so skipped agents keep their existing pin and updated agents
    get repinned to their latest version.
    """
    _set_ids(monkeypatch)
    monkeypatch.setenv("COORDINATOR_ID", "agent_coord_test")
    import update_subagent_tools as ust  # type: ignore

    # Coord currently has 7 sub-agents pinned. Only 3 will be updated
    # in this run; the other 4 are "skipped" (simulating a missing env
    # var) and must STAY in the union.
    coord_fake = _make_fake_coordinator(
        version=31,
        pinned_subagents={
            "agent_stat_test": 17,
            "agent_pipe_test": 11,
            "agent_sales_test": 11,
            "agent_post_test": 11,
            "agent_adv_test": 11,  # not updated this run
            "agent_cds_test": 8,  # not updated this run
            "agent_chart_test": 11,  # not updated this run
        },
    )
    # Only the three Monitors are passed to republish — Statistician /
    # Adversarial / Cross-Domain / Chart are "skipped" by main() (no
    # IDs in env). The union must keep them in the final roster.
    updated_in_this_run = [
        "agent_pipe_test",
        "agent_sales_test",
        "agent_post_test",
    ]
    live_versions = {
        "agent_pipe_test": 12,
        "agent_sales_test": 12,
        "agent_post_test": 12,
    }

    def fake_retrieve(agent_id):
        if agent_id == "agent_coord_test":
            return coord_fake
        return _make_fake_agent(version=live_versions[agent_id], tools=[])

    update_calls: list[dict] = []

    def fake_update(agent_id, **kwargs):
        update_calls.append({"agent_id": agent_id, **kwargs})
        return _make_fake_agent(
            version=kwargs["version"] + 1, tools=[], multiagent=coord_fake.multiagent
        )

    fake_client = MagicMock()
    fake_client.beta.agents.retrieve.side_effect = fake_retrieve
    fake_client.beta.agents.update.side_effect = fake_update

    status, version = ust.republish_coordinator_multiagent(
        fake_client, updated_in_this_run
    )
    assert status == "updated"
    assert version == 32

    # Exactly one update call to the Coordinator.
    assert len(update_calls) == 1
    payload = update_calls[0]
    assert payload["agent_id"] == "agent_coord_test"

    # The republish must pass the UNION of (current pinned) ∪ (this-run updates)
    # — not just the 3 IDs in updated_in_this_run. If only 3 went in,
    # the API would clear Adversarial / Cross-Domain / Chart Designer
    # from the Coordinator's roster.
    multiagent = payload["multiagent"]
    submitted_ids = {entry["id"] for entry in multiagent["agents"]}
    expected_union = {
        # Updated this run.
        "agent_pipe_test",
        "agent_sales_test",
        "agent_post_test",
        # Skipped — must stay pinned.
        "agent_stat_test",
        "agent_adv_test",
        "agent_cds_test",
        "agent_chart_test",
    }
    assert submitted_ids == expected_union, (
        f"Republish must union skipped agents into the pinned roster. "
        f"Got {submitted_ids}, expected {expected_union}. Missing: "
        f"{expected_union - submitted_ids}"
    )


def test_republish_coordinator_union_preserves_current_pin_order(monkeypatch):
    """Current pin order is preserved; new canonical IDs appended at the end.

    Multi-agent routing semantics depend on the order of the agents in
    the roster — additions only at the end keep behavior stable. The
    canonical-roster gate added 2026-05-15 strips off-roster IDs entirely,
    so this test uses ``agent_post_test`` (a canonical sub-agent NOT yet
    pinned) as the "new" ID rather than the pre-gate ``agent_new_test``.
    """
    _set_ids(monkeypatch)
    monkeypatch.setenv("COORDINATOR_ID", "agent_coord_test")
    import update_subagent_tools as ust  # type: ignore

    coord_fake = _make_fake_coordinator(
        version=31,
        pinned_subagents={
            "agent_stat_test": 17,
            "agent_pipe_test": 11,
        },
    )

    # Run updates only on the existing pinned subset PLUS one new canonical ID
    # (Post-Sales Monitor). The new ID must land at the end of the roster.
    updated_in_this_run = ["agent_pipe_test", "agent_post_test"]
    live_versions = {"agent_pipe_test": 12, "agent_post_test": 1}

    def fake_retrieve(agent_id):
        if agent_id == "agent_coord_test":
            return coord_fake
        return _make_fake_agent(version=live_versions[agent_id], tools=[])

    update_calls: list[dict] = []

    def fake_update(agent_id, **kwargs):
        update_calls.append({"agent_id": agent_id, **kwargs})
        return _make_fake_agent(
            version=kwargs["version"] + 1, tools=[], multiagent=coord_fake.multiagent
        )

    fake_client = MagicMock()
    fake_client.beta.agents.retrieve.side_effect = fake_retrieve
    fake_client.beta.agents.update.side_effect = fake_update

    status, _ = ust.republish_coordinator_multiagent(fake_client, updated_in_this_run)
    assert status == "updated"

    multiagent = update_calls[0]["multiagent"]
    submitted_ids = [entry["id"] for entry in multiagent["agents"]]
    # Current pin order first, new canonical ID last.
    assert submitted_ids == [
        "agent_stat_test",
        "agent_pipe_test",
        "agent_post_test",
    ]


def test_republish_coordinator_strips_off_roster_pins(monkeypatch):
    """Live Coordinator roster carrying off-roster IDs gets cleaned on republish.

    2026-05-15: smoke test caught the live Coordinator pinning 11 sub-agents
    (Writing Agent, Prompt Engineer, Quick Answer, Dream got mistakenly
    pinned at some point). The canonical-roster gate strips any pin not
    on the allowlist — re-running republish converges to the 7-agent roster.
    """
    _set_ids(monkeypatch)
    monkeypatch.setenv("COORDINATOR_ID", "agent_coord_test")
    import update_subagent_tools as ust  # type: ignore

    # Live coord pins 3 canonical + 2 off-roster agents.
    coord_fake = _make_fake_coordinator(
        version=42,
        pinned_subagents={
            "agent_stat_test": 17,
            "agent_pipe_test": 11,
            "agent_post_test": 22,
            "agent_writer_offroster": 4,  # off-roster: Writing Agent
            "agent_prompt_engineer_off": 5,  # off-roster: Prompt Engineer
        },
    )
    live_versions = {
        "agent_stat_test": 17,
        "agent_pipe_test": 11,
        "agent_post_test": 22,
    }

    def fake_retrieve(agent_id):
        if agent_id == "agent_coord_test":
            return coord_fake
        return _make_fake_agent(version=live_versions[agent_id], tools=[])

    update_calls: list[dict] = []

    def fake_update(agent_id, **kwargs):
        update_calls.append({"agent_id": agent_id, **kwargs})
        return _make_fake_agent(
            version=kwargs["version"] + 1, tools=[], multiagent=coord_fake.multiagent
        )

    fake_client = MagicMock()
    fake_client.beta.agents.retrieve.side_effect = fake_retrieve
    fake_client.beta.agents.update.side_effect = fake_update

    status, _ = ust.republish_coordinator_multiagent(
        fake_client, ["agent_stat_test", "agent_pipe_test", "agent_post_test"]
    )
    # Off-roster pins forced an update even though no canonical sub-agent
    # drifted — the strip path is the trigger.
    assert status == "updated"
    submitted_ids = [entry["id"] for entry in update_calls[0]["multiagent"]["agents"]]
    assert submitted_ids == [
        "agent_stat_test",
        "agent_pipe_test",
        "agent_post_test",
    ], submitted_ids
    assert "agent_writer_offroster" not in submitted_ids
    assert "agent_prompt_engineer_off" not in submitted_ids


def test_republish_coordinator_skips_when_only_new_ids_match_and_no_drift(monkeypatch):
    """No drift and no new IDs to add → 'unchanged' status, no API call.

    When the union equals the current pinned roster AND every pinned
    sub-agent is already at its live version, the republish is a no-op.
    """
    _set_ids(monkeypatch)
    monkeypatch.setenv("COORDINATOR_ID", "agent_coord_test")
    import update_subagent_tools as ust  # type: ignore

    coord_fake = _make_fake_coordinator(
        version=31,
        pinned_subagents={
            "agent_stat_test": 18,
            "agent_pipe_test": 12,
        },
    )

    def fake_retrieve(agent_id):
        if agent_id == "agent_coord_test":
            return coord_fake
        return _make_fake_agent(
            version={"agent_stat_test": 18, "agent_pipe_test": 12}[agent_id],
            tools=[],
        )

    fake_client = MagicMock()
    fake_client.beta.agents.retrieve.side_effect = fake_retrieve

    def _fail_on_update(*a, **k):
        raise AssertionError(
            "republish must not call .update() when union matches current "
            "pin and no version drift"
        )

    fake_client.beta.agents.update.side_effect = _fail_on_update

    status, version = ust.republish_coordinator_multiagent(
        fake_client, ["agent_stat_test", "agent_pipe_test"]
    )
    assert status == "unchanged"
    assert version == 31


def test_main_writes_coordinator_to_pin_file_after_republish(monkeypatch, tmp_path):
    """The pin file refresh includes the Coordinator version after repin."""
    _set_ids(monkeypatch)
    monkeypatch.setenv("COORDINATOR_ID", "agent_coord_test")
    import update_subagent_tools as ust  # type: ignore

    pin_file = tmp_path / "active_versions.json"
    pin_file.write_text(
        json.dumps(
            {
                "coordinator": 31,  # Pre-bump.
                "statistician": 17,
                "pipeline_monitor": 11,
                "sales_monitor": 11,
                "postsales_monitor": 11,
                "adversarial_reviewer": 11,
                "cross_domain_synthesizer": 8,
                "chart_designer": 11,
                "writing_agent": 6,
                "dream": 8,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    monkeypatch.setattr(ust, "ACTIVE_VERSIONS_PATH", pin_file)

    # Build a coord fake whose pinned sub-agent versions are STALE so the
    # republish path triggers an update. Pin sub-agents at v1 across the
    # board → drift on all 8 → repin → coord version bumps from 31→32.
    # Note: Dream is NOT in the Coordinator's multiagent roster — the
    # orchestrator dispatches it directly — so the coord pin and the
    # repin set both omit Dream by design. Writing Agent IS in the roster
    # (joined 2026-05-27), so it appears in both coord_pinned_at_v1 and
    # live_sub_versions.
    coord_state = {"version": 31}
    coord_pinned_at_v1 = {
        "agent_stat_test": 1,
        "agent_pipe_test": 1,
        "agent_sales_test": 1,
        "agent_post_test": 1,
        "agent_adv_test": 1,
        "agent_cds_test": 1,
        "agent_chart_test": 1,
        "agent_writing_test": 1,
    }
    live_sub_versions = {
        "agent_stat_test": 18,
        "agent_pipe_test": 12,
        "agent_sales_test": 12,
        "agent_post_test": 12,
        "agent_adv_test": 12,
        "agent_cds_test": 9,
        "agent_chart_test": 12,
        "agent_writing_test": 7,
        "agent_dream_test": 9,
    }

    def fake_retrieve(agent_id):
        if agent_id == "agent_coord_test":
            return _make_fake_coordinator(
                version=coord_state["version"], pinned_subagents=coord_pinned_at_v1
            )
        return _make_fake_agent(
            version=live_sub_versions[agent_id] - 1,  # pre-update
            tools=[{"type": "agent_toolset_20260401"}],
        )

    def fake_update(agent_id, **kwargs):
        if agent_id == "agent_coord_test":
            coord_state["version"] += 1
            return _make_fake_agent(
                version=coord_state["version"],
                tools=[],
                multiagent=SimpleNamespace(
                    type="coordinator",
                    agents=[
                        SimpleNamespace(
                            id=sid, type="agent", version=live_sub_versions[sid]
                        )
                        for sid in live_sub_versions
                    ],
                ),
            )
        return _make_fake_agent(version=kwargs["version"] + 1, tools=kwargs["tools"])

    fake_client = MagicMock()
    fake_client.beta.agents.retrieve.side_effect = fake_retrieve
    fake_client.beta.agents.update.side_effect = fake_update
    monkeypatch.setattr(ust.anthropic, "Anthropic", lambda: fake_client)

    rc = ust.main()
    assert rc == 0

    refreshed = json.loads(pin_file.read_text())
    # Plan #44 Task #4/#12 added Coordinator tools/skills reconciliation
    # BEFORE the multiagent republish. The test fixture's coordinator
    # starts at v31 with empty tools[] (the fake_retrieve returns that
    # shape), so reconcile_coordinator_tools_and_skills bumps 31→32, then
    # the multiagent republish bumps 32→33.
    assert refreshed["coordinator"] == 33
    # Each sub-agent advanced by 1 (pre-update + update = live - 1 + 1 = live).
    assert refreshed["statistician"] == 18
    assert refreshed["pipeline_monitor"] == 12
    assert refreshed["sales_monitor"] == 12
    assert refreshed["postsales_monitor"] == 12
    assert refreshed["adversarial_reviewer"] == 12
    assert refreshed["cross_domain_synthesizer"] == 9
    assert refreshed["chart_designer"] == 12
    # Writing Agent — joined the multiagent roster 2026-05-27. The Coordinator's
    # republish keeps it pinned. Its pin advances alongside the other reconciled
    # agents.
    assert refreshed["writing_agent"] == 7
    # Dream's pin advances alongside the other reconciled agents, even
    # though Dream is NOT in the Coordinator's multiagent roster.
    assert refreshed["dream"] == 9


# ---------------------------------------------------------------------------
# Plan #44 Task #17 — SF MCP toolset reconciliation. When the target tools[]
# carries an SF mcp_toolset (vault path enabled), the update payload must
# also pass mcp_servers=[SF_MCP_SERVER] so the API accepts the toolset
# reference. When the target has no mcp_toolset (default path), the
# pre-existing clearing branch passes mcp_servers=[]. Both branches must
# stay idempotent — a live agent already in the right shape produces no
# update at all.
# ---------------------------------------------------------------------------


def _reset_setup_agents_cache():
    """Drop cached setup_agents / sf_mcp_builder so the flag re-evaluates.

    Caller MUST re-set ACTIVE_VERSIONS_PATH on the freshly-imported
    update_subagent_tools module — the autouse fixture in this file
    monkey-patches the OLD module instance; dropping the cache breaks
    that link and reading writes would otherwise scribble onto the
    real ``agents/active_versions.json`` pin file.
    """
    for mod in ("setup_agents", "sf_mcp_builder", "update_subagent_tools"):
        sys.modules.pop(mod, None)


def _reimport_with_isolated_pin(tmp_path):
    """Re-import update_subagent_tools and point ACTIVE_VERSIONS_PATH at tmp.

    Companion to _reset_setup_agents_cache. After flag manipulation
    tests reset the module cache, they must re-isolate the pin file
    so the test's main() run doesn't write the real on-disk pin.
    """
    import update_subagent_tools as ust  # type: ignore

    ust.ACTIVE_VERSIONS_PATH = tmp_path / "active_versions.json"
    return ust


def test_vault_path_target_passes_sf_mcp_server_to_update(monkeypatch, tmp_path):
    """When SF_MCP_VIA_VAULT=true, update_one passes mcp_servers=[SF_MCP_SERVER].

    Today's default-path agent has no SF mcp_toolset and no SF
    mcp_servers entry. Flipping the flag adds the toolset to the target
    roster; the update must declare the matching mcp_servers entry in
    the same call or the API rejects it. Verifies the new
    ``_target_mcp_servers`` helper is consumed by ``update_one``.
    """
    _set_ids(monkeypatch)
    monkeypatch.setenv("SF_MCP_VIA_VAULT", "true")
    _reset_setup_agents_cache()
    try:
        ust = _reimport_with_isolated_pin(tmp_path)

        update_calls: list[dict] = []

        def fake_retrieve(agent_id):
            return _make_fake_agent(
                version=5,
                tools=[{"type": "agent_toolset_20260401"}],
                mcp_servers=[],
            )

        def fake_update(agent_id, **kwargs):
            update_calls.append({"agent_id": agent_id, **kwargs})
            return _make_fake_agent(
                version=kwargs["version"] + 1,
                tools=kwargs.get("tools", []),
                mcp_servers=kwargs.get("mcp_servers", []),
            )

        fake_client = MagicMock()
        fake_client.beta.agents.retrieve.side_effect = fake_retrieve
        fake_client.beta.agents.update.side_effect = fake_update
        monkeypatch.setattr(ust.anthropic, "Anthropic", lambda: fake_client)

        rc = ust.main()
        assert rc == 0

        # Data agents (Statistician + 3 Monitors) get the SF mcp_toolset
        # AND the matching mcp_servers entry. Reasoning agents don't —
        # their target roster has no mcp_toolset even in the vault path.
        # Post-2026-05-13, Kapa pivoted from mcp_toolset to custom tool, so
        # only the SF mcp_toolset remains in vault mode → mcp_servers carries
        # exactly one entry (salesforce) for every data agent in this path.
        # Statistician (agent_stat_test) is intentionally excluded: it uses
        # STATISTICIAN_TOOLS (vault_path=False), so it never gets the SF MCP
        # toolset/server even with SF_MCP_VIA_VAULT=true. Only the 3 Monitors
        # are vault-path data agents.
        data_ids = {
            "agent_pipe_test",
            "agent_sales_test",
            "agent_post_test",
        }
        seen_data_ids = set()
        for call in update_calls:
            if call["agent_id"] in data_ids:
                seen_data_ids.add(call["agent_id"])
                assert "mcp_servers" in call, (
                    f"Vault path: {call['agent_id']} must declare mcp_servers "
                    "so the SF mcp_toolset in tools[] has a matching server."
                )
                servers = call["mcp_servers"]
                server_names = {s["name"] for s in servers}
                assert "salesforce" in server_names, (
                    f"Vault path: {call['agent_id']} mcp_servers must "
                    f"contain salesforce. Got {servers!r}."
                )
                # Tools[] must also contain the SF mcp_toolset entry.
                types = {t.get("type") for t in call["tools"]}
                assert "mcp_toolset" in types
        assert seen_data_ids == data_ids, (
            f"Missing updates for data agents: {data_ids - seen_data_ids}"
        )
    finally:
        _reset_setup_agents_cache()


def test_vault_path_idempotent_when_servers_already_correct(monkeypatch, tmp_path):
    """Live state already has SF mcp_toolset + mcp_servers → no update call.

    Idempotency is critical so a no-op deploy doesn't bump versions.
    With the flag set AND the live tools/servers matching the target,
    update_one must short-circuit before calling .update().
    """
    _set_ids(monkeypatch)
    monkeypatch.setenv("SF_MCP_VIA_VAULT", "true")
    _reset_setup_agents_cache()
    try:
        ust = _reimport_with_isolated_pin(tmp_path)
        import setup_agents  # type: ignore  # noqa: F401

        update_calls: list[str] = []

        def fake_retrieve(agent_id):
            entry = next(
                e
                for e in ust.PRE_PROVISIONED_AGENTS
                if _AGENT_ID_TO_NAME[agent_id] == e[0]
            )
            target = entry[2]
            target_skills = entry[3] if len(entry) >= 4 else []
            deploy = ust.build_tools_for_agent(target)
            # Live mcp_servers mirrors what the target declares for data
            # agents (SF_MCP_SERVER); empty for the 3 reasoning agents.
            # Live skills mirrors the target so the idempotency check
            # holds for both data agents (xlsx skill) and reasoning
            # agents (no skills) — Plan #44 Task #12 added skills[] to
            # the reconciler, so the fake must populate it too.
            target_servers = ust._target_mcp_servers(deploy)
            return _make_fake_agent(
                version=9,
                tools=deploy,
                mcp_servers=target_servers,
                skills=list(target_skills),
            )

        def fake_update(agent_id, **kwargs):
            update_calls.append(agent_id)
            return _make_fake_agent(
                version=kwargs["version"] + 1,
                tools=kwargs["tools"],
                mcp_servers=kwargs.get("mcp_servers", []),
            )

        fake_client = MagicMock()
        fake_client.beta.agents.retrieve.side_effect = fake_retrieve
        fake_client.beta.agents.update.side_effect = fake_update
        monkeypatch.setattr(ust.anthropic, "Anthropic", lambda: fake_client)

        rc = ust.main()
        assert rc == 0
        assert update_calls == [], (
            f"Idempotency violation: update was called for {update_calls} "
            "even though live tools[] + mcp_servers already match target."
        )
    finally:
        _reset_setup_agents_cache()


def test_default_path_still_clears_stale_mcp_servers(monkeypatch, tmp_path):
    """Regression guard: SF_MCP_VIA_VAULT=false keeps the original clearing branch.

    The new vault-path code path must not break the existing
    mcp_servers=[] clearing behavior. With the flag absent (default),
    a live agent carrying a stale SF mcp_servers entry should still get
    mcp_servers=[] in the update kwargs.
    """
    _set_ids(monkeypatch)
    monkeypatch.delenv("SF_MCP_VIA_VAULT", raising=False)
    _reset_setup_agents_cache()
    try:
        ust = _reimport_with_isolated_pin(tmp_path)

        update_calls: list[dict] = []
        sub_agent_ids = set(_AGENT_ID_TO_NAME.keys())

        def fake_retrieve(agent_id):
            return _make_fake_agent(
                version=5,
                tools=[{"type": "agent_toolset_20260401"}],
                mcp_servers=[_fake_mcp_server("salesforce")],
            )

        def fake_update(agent_id, **kwargs):
            update_calls.append({"agent_id": agent_id, **kwargs})
            return _make_fake_agent(
                version=kwargs["version"] + 1,
                tools=kwargs.get("tools", []),
                mcp_servers=kwargs.get("mcp_servers", []),
            )

        fake_client = MagicMock()
        fake_client.beta.agents.retrieve.side_effect = fake_retrieve
        fake_client.beta.agents.update.side_effect = fake_update
        monkeypatch.setattr(ust.anthropic, "Anthropic", lambda: fake_client)

        rc = ust.main()
        assert rc == 0
        # Only assert on sub-agent calls — the Coordinator's multiagent
        # republish step is a separate update with a different payload
        # shape and is exercised in its own dedicated tests.
        sub_agent_calls = [c for c in update_calls if c["agent_id"] in sub_agent_ids]
        assert sub_agent_calls, "expected at least one sub-agent update call"
        # Post-2026-05-13, Kapa pivoted from mcp_toolset to custom tool, so
        # NO sub-agent currently has any mcp_toolset entry. Every sub-agent
        # update MUST set mcp_servers=[] to clear the stale Salesforce
        # registration in lockstep with the tools[] update.
        for call in sub_agent_calls:
            aid = call["agent_id"]
            servers = call.get("mcp_servers", [])
            assert servers == [], (
                f"Default path: {aid} should clear stale mcp_servers because "
                "no sub-agent target carries an mcp_toolset entry after the "
                f"Kapa custom-tool pivot. Got {servers!r}."
            )
    finally:
        _reset_setup_agents_cache()


def test_target_mcp_servers_helper():
    """Direct check on _target_mcp_servers (the SF MCP reconciliation helper)."""
    import update_subagent_tools as ust  # type: ignore

    # Target with no mcp_toolset → empty list (default path).
    assert (
        ust._target_mcp_servers(
            [{"type": "agent_toolset_20260401"}, {"type": "custom", "name": "x"}]
        )
        == []
    )

    # Target with SF mcp_toolset → one SF server entry (vault path).
    out = ust._target_mcp_servers(
        [
            {"type": "agent_toolset_20260401"},
            {"type": "mcp_toolset", "mcp_server_name": "salesforce"},
        ]
    )
    assert len(out) == 1
    assert out[0]["name"] == "salesforce"


def test_mcp_servers_match_helper_subset_semantics():
    """_mcp_servers_match handles API-side default fields like _tools_match does.

    The live mcp_servers entry from the SDK carries server-side default
    fields that aren't in the authored target. The matcher must apply
    subset semantics on the name + every authored key, ignoring extras.
    """
    import update_subagent_tools as ust  # type: ignore

    target = [{"name": "salesforce", "type": "url", "url": "https://example/x"}]
    live = [
        SimpleNamespace(
            name="salesforce",
            type="url",
            url="https://example/x",
            # API-side defaults that target doesn't author:
            permission_policy={"type": "always_allow"},
            created_at="2026-05-13T00:00:00Z",
        )
    ]
    assert ust._mcp_servers_match(live, target), (
        "Live mcp_servers with API-side defaults must match a target with "
        "only the authored keys (name/type/url)."
    )

    # Different URL → no match.
    different = [{"name": "salesforce", "type": "url", "url": "https://other"}]
    assert not ust._mcp_servers_match(live, different)

    # Different name → no match.
    other_name = [{"name": "slack", "type": "url", "url": "https://example/x"}]
    assert not ust._mcp_servers_match(live, other_name)
