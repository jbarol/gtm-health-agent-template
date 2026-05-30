"""Idempotent updater for the nine reconciled agents' tools[].

Every agent in ``PRE_PROVISIONED_AGENTS`` (Pipeline / Sales / Postsales
Monitors, Statistician, Adversarial Reviewer, Cross-Domain Synthesizer,
Chart Designer, Quick Answer, Dream) is reconciled against a per-agent
target roster on every deploy. This script retrieves each agent's
current state and calls ``beta.agents.update()`` when the live
``tools[]`` diverges from the target.

Iteration 3 split the single ``SUB_AGENT_DATA_TOOLS`` roster into two:

* Query sub-agents (Statistician + Pipeline / Sales / Postsales
  Monitors) → ``SUB_AGENT_DATA_TOOLS`` (no SF_MCP_TOOLSET — every SF
  read routes through ``dump_sf_query`` to Parquet).
* Reasoning sub-agents (Adversarial Reviewer, Cross-Domain Synthesizer,
  Chart Designer) → ``SUB_AGENT_REASONING_TOOLS`` (no SF tools at all
  — they consume findings, not raw rows). Removing the SF MCP toolset
  also resolves the prior ``"mcp_toolset references [salesforce] but
  no matching entry in mcp_servers"`` failure on these three agents.

Iteration 3 originally only listed the four "pre-provisioned" agents
(Statistician + the three reasoning agents) because they were the only
ones that couldn't be re-minted by ``setup_agents.py``. The three
Monitors were created with the SF mcp_toolset in their initial roster
and were never updated again — production held only because the
Coordinator's v31 prompt steers them through ``dump_sf_query``. Adding
the Monitors here (2026-05-12) closes that prompt-only enforcement
hole with a tool-level guarantee. Quick Answer joined the roster with
the Kapa integration. Dream Agent was added 2026-05-14 after a WATCH
alert showed it still carrying a stale Salesforce mcp_server entry
from a prior create call (sesn_EXAMPLE).

Idempotent — safe to re-run. Compares the live ``tools[]`` against the
per-agent target and skips the update when they already match (so a
no-op deploy does not bump the agent's version pointlessly).

The script reads env vars set in .env / GitHub Secrets / Railway:

    STATISTICIAN_ID
    PIPELINE_MONITOR_ID
    SALES_MONITOR_ID
    POSTSALES_MONITOR_ID
    ADVERSARIAL_REVIEWER_ID
    CROSS_DOMAIN_SYNTHESIZER_ID
    CHART_DESIGNER_ID
    QUICK_ANSWER_ID
    DREAM_AGENT_ID

Missing IDs are skipped with a [SKIP] log line — the script never
fails for a missing ID. Wired into ``.github/workflows/deploy-prompts.yml``
so it runs on every merge to main right after ``update_prompts.py``.

Iteration 3 of plan ``misty-squishing-badger`` (Monitor coverage
follow-up shipped 2026-05-12; Dream follow-up shipped 2026-05-14).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Bootstrap .env (same parser as update_prompts.py / add_post_report_tool.py).
_dotenv = Path(__file__).parent.parent / ".env"
if _dotenv.exists():
    for _line in _dotenv.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# Make ``agents/`` importable so we can pull tool roster constants from the
# source of truth (no DRY violation). setup_agents.py is structured so the
# constants live at module scope and the network-issuing calls live under
# ``if __name__ == "__main__": main()`` — importing this module is safe.
sys.path.insert(0, str(Path(__file__).parent))

import anthropic  # noqa: E402

from build_post_report_schema import flatten_refs  # noqa: E402
from setup_agents import (  # noqa: E402
    CHART_DESIGNER_TOOLS,
    DREAM_KAPA_TOOLS,
    FILE_MATERIALIZING_SKILLS,
    KAPA_ACME_MCP_TOOLSET,
    QUICK_ANSWER_KAPA_TOOLS,
    SF_MCP_SERVER,
    SUB_AGENT_DATA_TOOLS,
    SUB_AGENT_DATA_TOOLS_WITH_KAPA,
    SUB_AGENT_REASONING_TOOLS,
    SUB_AGENT_REASONING_TOOLS_WITH_KAPA,
    STATISTICIAN_TOOLS,
    WRITING_AGENT_TOOLS,
)

# Registry of MCP server configs keyed by ``name``. Anything that may appear
# as an ``mcp_toolset`` entry in a sub-agent's tools[] must have its server
# config registered here so ``_required_mcp_servers`` can re-attach the
# correct server registration on update. Adding a new MCP-backed integration
# = add the toolset to the relevant *_TOOLS roster in ``setup_agents.py`` and
# register its server config here.
#
# Kapa was an MCP server in the 2026-05-13 morning shipment but pivoted
# to a custom REST tool the same day (the Kapa hosted MCP rejects
# static_bearer auth, while the REST API works fine). No Kapa entry
# here as a result.
MCP_SERVERS_BY_NAME: dict[str, dict] = {
    SF_MCP_SERVER["name"]: SF_MCP_SERVER,
}

# Pin-file helpers are duplicated here (rather than imported from
# update_prompts) because update_prompts.py runs heavy import-time
# side effects: it reads .env, instantiates the Anthropic client, and
# builds the prompt schema block. The two-line read/write here keeps
# the script test-friendly with no .env dependency. The format must
# stay byte-identical to update_prompts.write_active_versions so the
# two writers don't fight over trailing newlines / key ordering.
ACTIVE_VERSIONS_PATH = Path(__file__).resolve().parent / "active_versions.json"


def read_active_versions() -> dict[str, int]:
    """Load the on-disk pin file. Returns ``{}`` when missing."""
    if not ACTIVE_VERSIONS_PATH.exists():
        return {}
    try:
        return json.loads(ACTIVE_VERSIONS_PATH.read_text())
    except Exception:
        return {}


def write_active_versions(versions: dict[str, int]) -> None:
    """Persist the pin file. Sorted keys + pretty-printed for clean diffs."""
    ACTIVE_VERSIONS_PATH.write_text(
        json.dumps(versions, indent=2, sort_keys=True) + "\n"
    )


# Per-agent target roster + skills. Three groups:
#
# * Data sub-agents (Pipeline / Sales / Postsales Monitors + Statistician)
#   → ``SUB_AGENT_DATA_TOOLS`` + xlsx skill. No mcp_toolset for Salesforce;
#   every SF read materializes through ``dump_sf_query`` to Parquet so raw
#   rows never bloat the Coordinator context. Adding the three Monitors here
#   (2026-05-12) closes the gap from Iter3: they were created by
#   ``setup_agents.py`` with the original SUB_AGENT_DATA_TOOLS roster
#   that *included* the SF mcp_toolset, and CI never overwrote them.
#   Production behavior held only because the Coordinator's v31 prompt
#   tells them to route through dump_sf_query — a Coordinator regression
#   would re-open the row-leak path. Tool-level enforcement is the
#   architectural defense; prompt enforcement is the soft layer on top.
#
# * Reasoning sub-agents (Adversarial Reviewer, Cross-Domain Synthesizer)
#   → ``SUB_AGENT_REASONING_TOOLS``, no skills. No SF tools at all — they
#   reason over findings, not raw rows. Removing the SF mcp_toolset also
#   resolves the prior ``mcp_toolset references [salesforce] but no
#   matching entry in mcp_servers`` failure on these two agents. They
#   don't materialize files, so xlsx is not attached — keeps the
#   session-wide 20-skill budget under 6/20 with room to spare.
#
# * Chart Designer → ``CHART_DESIGNER_TOOLS`` + xlsx skill. Same tool
#   shape as REASONING but with generate_chart appended — rendering charts
#   is its primary job. xlsx is attached because the Chart Designer also
#   ships .xlsx attachments for chart data on data-pull responses.
#
# Plan #44 Task #12 — `skills[]` reconciliation. The xlsx skill is
# attached ONLY to file-materializing agents (Coordinator + 3 Monitors
# + Statistician + Chart Designer = 6/20 budget consumed). Pure
# reasoning agents stay clean. Reconciler treats missing/empty as no-op
# and tolerates the live agent carrying extra skills the script didn't
# author (subset semantics, same as tools[]).
#
# Kapa MCP integration overlay (Post-Sales Monitor + Cross-Domain
# Synthesizer + Quick Answer): Post-Sales swaps SUB_AGENT_DATA_TOOLS for
# SUB_AGENT_DATA_TOOLS_WITH_KAPA (adds the Kapa toolset alongside SF data
# tools), Cross-Domain Synthesizer swaps SUB_AGENT_REASONING_TOOLS for
# SUB_AGENT_REASONING_TOOLS_WITH_KAPA, and Quick Answer joins the roster
# with QUICK_ANSWER_KAPA_TOOLS. Discovery in
# docs/research/kapa-acme-index.md drove the per-agent scope.
#
# Tuple shape: (name, env_var, target_tools, target_skills,
# in_multiagent_roster). The 5th element gates participation in the
# Coordinator's multiagent.agents block repinned by
# ``republish_coordinator_multiagent``. Default ``True`` (drop the flag
# or pass a 4-tuple). Dream Agent is set to ``False`` because it runs
# stand-alone — the orchestrator dispatches Dream directly via the
# nightly scheduler, never through the Coordinator's multiagent
# runtime, so re-pinning it into the roster would break that
# separation (and would add a sub-agent the Coordinator's prompt
# doesn't address). The same applies to any future stand-alone agent
# that needs tools[] reconciliation without joining the multiagent
# roster — pass ``False`` here.
PRE_PROVISIONED_AGENTS: list[tuple] = [
    (
        "statistician",
        "STATISTICIAN_ID",
        STATISTICIAN_TOOLS,
        FILE_MATERIALIZING_SKILLS,
    ),
    (
        "pipeline_monitor",
        "PIPELINE_MONITOR_ID",
        SUB_AGENT_DATA_TOOLS,
        FILE_MATERIALIZING_SKILLS,
    ),
    (
        "sales_monitor",
        "SALES_MONITOR_ID",
        SUB_AGENT_DATA_TOOLS,
        FILE_MATERIALIZING_SKILLS,
    ),
    # Post-Sales Monitor gets Kapa as of the Kapa MCP integration. Discovery
    # showed the Acme Kapa index covers internal Confluence (engineering,
    # product, Commerce GTM, "After-hours Work Updates") plus public help
    # docs — exactly the change-context that explains retention shifts. See
    # docs/research/kapa-acme-index.md for the index map.
    (
        "postsales_monitor",
        "POSTSALES_MONITOR_ID",
        SUB_AGENT_DATA_TOOLS_WITH_KAPA,
        FILE_MATERIALIZING_SKILLS,
    ),
    ("adversarial_reviewer", "ADVERSARIAL_REVIEWER_ID", SUB_AGENT_REASONING_TOOLS, []),
    # Cross-Domain Synthesizer gets Kapa so it can correlate product-side
    # events (releases, infra changes, deprecations) with revenue-side
    # patterns surfaced by the Monitors — that connection is exactly its job.
    (
        "cross_domain_synthesizer",
        "CROSS_DOMAIN_SYNTHESIZER_ID",
        SUB_AGENT_REASONING_TOOLS_WITH_KAPA,
        [],
    ),
    (
        "chart_designer",
        "CHART_DESIGNER_ID",
        CHART_DESIGNER_TOOLS,
        FILE_MATERIALIZING_SKILLS,
    ),
    # Writing Agent — Haiku 4.5, prose composer. Joined the Coordinator's
    # multiagent roster on 2026-05-27 when the ``write_prose`` custom tool
    # was retired. The Coordinator delegates prose composition by
    # addressing the Writing Agent in its session thread (persistent
    # within the parent session). Tools = WRITING_AGENT_TOOLS
    # (agent_toolset_20260401 + reasoning_summary, no SF data tools, no
    # Kapa, no chart). No skills attached — the Writing Agent never
    # materializes files; the xlsx attached to post_report is produced
    # by the Coordinator from data the Monitors materialized.
    # in_multiagent_roster=True (default) — the Writing Agent MUST be in
    # the canonical republish roster so ``republish_coordinator_multiagent``
    # preserves its pin when it re-snapshots the Coordinator. Without this
    # entry the next CI republish would strip the Writing Agent from the
    # Coordinator and the entire prose-composition path would dead-end.
    (
        "writing_agent",
        "WRITING_AGENT_ID",
        WRITING_AGENT_TOOLS,
        [],
    ),
    # Quick Answer is the single-fact Slack-lookup specialist. With Kapa it
    # can resolve Acme-specific term lookups ("what is FATI?", "who runs
    # Commerce GTM?", "what integrates with Acme?") directly. Single-turn
    # agent — no skills attached, doesn't materialize files. Excluded from
    # the Coordinator multiagent roster: the orchestrator dispatches Quick
    # Answer directly from the Slack handler (single-fact path), never
    # through the Coordinator's task delegation. Including it in the roster
    # bloated the Coordinator's pinned snapshot without changing routing.
    (
        "quick_answer",
        "QUICK_ANSWER_ID",
        QUICK_ANSWER_KAPA_TOOLS,
        [],
        False,  # in_multiagent_roster = False
    ),
    # Dream Agent — Sonnet 4.6, nightly hypothesis generator. Stands
    # alone from the Coordinator's multiagent roster (the orchestrator
    # dispatches Dream directly via scheduled_dream, never through the
    # Coordinator), so the 5th element ``False`` keeps it out of the
    # ``republish_coordinator_multiagent`` re-pin pass.
    #
    # Target tools = DREAM_KAPA_TOOLS:
    #   agent_toolset_20260401, db_query, query_artifact, and the Kapa
    #   custom tool (search_knowledge_base). No
    #   mcp_toolset entries — Kapa pivoted from MCP to a custom REST
    #   tool on 2026-05-13, and Dream never had Salesforce access.
    # Target mcp_servers = [] derived by ``_required_mcp_servers``.
    #
    # Live agent (agent_EXAMPLE_dream) historically had a
    # stale ``salesforce`` entry in mcp_servers from a prior create
    # call, which caused a WATCH alert on 2026-05-14 at ~21:49 UTC
    # (sesn_EXAMPLE): Dream tried ``soqlQuery`` and
    # Plan #44's empty MCP_AUTO_APPROVE_ALLOWLIST denied the call.
    # Reconciliation here clears that stale registration on the next
    # CI deploy.
    (
        "dream",
        "DREAM_AGENT_ID",
        DREAM_KAPA_TOOLS,
        [],
        False,  # in_multiagent_roster = False
    ),
]


def _unpack_entry(entry: tuple) -> tuple[str, str, list, list, bool]:
    """Unpack a PRE_PROVISIONED_AGENTS tuple into 5 named slots.

    Back-compat shim that tolerates the historical 3-tuple
    ``(name, env_var, tools)``, the post-Plan-#44 4-tuple
    ``(name, env_var, tools, skills)``, and the new 5-tuple
    ``(name, env_var, tools, skills, in_multiagent_roster)``. Missing
    elements default to ``[]`` (skills) and ``True``
    (in_multiagent_roster).

    Defaulting in_multiagent_roster to True preserves every existing
    sub-agent's pre-existing routing — only Dream (which is explicit
    about not being in the roster) opts out today.
    """
    if len(entry) == 5:
        return entry  # type: ignore[return-value]
    if len(entry) == 4:
        name, env_var, tools, skills = entry
        return (name, env_var, tools, skills, True)
    name, env_var, tools = entry  # type: ignore[misc]
    return (name, env_var, tools, [], True)


def build_tools_for_agent(target_tools: list) -> list:
    """Return the deploy-ready tools[] for an agent.

    Walks every tool with an ``input_schema`` and runs ``flatten_refs``
    over the schema so any embedded ``$ref`` / ``$defs`` are inlined
    before reaching ``agents.update()``. Pydantic-derived schemas
    (POST_REPORT_TOOL) emit refs that Anthropic's custom-tool API
    rejects on update — Iteration 3 closes that gap. Hand-written
    schemas (DUMP_SF_QUERY_TOOL, QUERY_ARTIFACT_TOOL, DB_QUERY_TOOL)
    don't contain refs, so this is a no-op for them today; the helper
    runs defensively so any future Pydantic-derived tool added to a
    sub-agent roster ships flat without the caller having to remember.
    """
    out = []
    for tool in target_tools:
        if isinstance(tool, dict) and isinstance(tool.get("input_schema"), dict):
            schema_text = json.dumps(tool["input_schema"])
            if "$ref" in schema_text or "$defs" in schema_text:
                flattened = dict(tool)
                flattened["input_schema"] = flatten_refs(tool["input_schema"])
                out.append(flattened)
                continue
        out.append(tool)
    return out


def _tool_to_dict(tool):
    """Convert a tool object from the API back into a dict for comparison.

    The Anthropic SDK returns Pydantic objects (or the SDK's TypedDicts);
    we need plain dicts to diff against the target and to feed back
    into ``agents.update()``.
    """
    if hasattr(tool, "model_dump"):
        return tool.model_dump(exclude_none=True)
    if isinstance(tool, dict):
        return tool
    return {
        k: v for k, v in vars(tool).items() if not k.startswith("_") and v is not None
    }


def _tools_match(live, target) -> bool:
    """Return True if the live tools[] already matches the target.

    The comparison is order-insensitive on the outer list (Anthropic does
    not promise ordering on retrieve) and applies SUBSET semantics on
    each tool dict: every key/value in the target tool must also appear
    in the live tool, but the live tool may have additional API-side
    default keys (``configs``, ``default_config``, ``permission_policy``,
    etc.) that we don't author. Without subset semantics, the script is
    non-idempotent — every re-run would issue a no-op update and bump
    the version, because the live tool returned by ``agents.retrieve``
    always has more keys than the spec we author.

    Recursion on nested dicts uses the same rule. Lists are compared by
    value equality (the only nested list we author today is
    ``input_schema.properties.<x>.enum`` and similar, where order matters).
    Two tools with the same ``name`` but differing ``input_schema``
    do NOT match — the schema must be a subset, not equal — but in
    practice every key in the target's input_schema is required to be
    present in live.
    """

    def _subset(target, live):
        if isinstance(target, dict):
            if not isinstance(live, dict):
                return False
            return all(k in live and _subset(v, live[k]) for k, v in target.items())
        return target == live

    live_norm = sorted((str(t.get("name") or t.get("type"))) for t in live)
    target_norm = sorted((str(t.get("name") or t.get("type"))) for t in target)
    if live_norm != target_norm:
        return False
    by_name_live = {t.get("name") or t.get("type"): t for t in live}
    by_name_target = {t.get("name") or t.get("type"): t for t in target}
    for k, v in by_name_target.items():
        if not _subset(v, by_name_live.get(k)):
            return False
    return True


def _target_has_mcp_toolset(target_tools: list) -> bool:
    """True if the target tools[] contains any mcp_toolset entry."""
    return any(
        isinstance(t, dict) and t.get("type") == "mcp_toolset" for t in target_tools
    )


def _required_mcp_servers(target_tools: list) -> list[dict]:
    """Return the mcp_server configs matching every mcp_toolset entry in tools[].

    Iteration-3 originally only cleared ``mcp_servers`` when no mcp_toolset
    was present, relying on the live state to already have the right
    registrations when toolsets WERE present. That worked for the
    SF-removal pass but is silently wrong on additions: a new mcp_toolset
    entry requires the matching mcp_server registration on the same
    ``agents.update`` call or the API responds with ``mcp_toolset
    references [<name>] but no matching entry in mcp_servers``. The
    Kapa MCP addition forced this gap — Post-Sales Monitor gains the
    Kapa toolset but its live ``mcp_servers`` only had SF. Fix: derive
    the required server list from the target tools and always set
    ``mcp_servers`` on update.

    Generalized for multi-server agents: a single tools[] roster may
    carry more than one mcp_toolset (e.g. Post-Sales has BOTH the SF
    toolset under SF_MCP_VIA_VAULT=true AND the Kapa toolset). Each
    mcp_toolset entry resolves to its server config via
    ``MCP_SERVERS_BY_NAME``; the returned list preserves toolset order.

    Falls through silently if a name has no registered config — keeps
    the script open to externally-managed MCP servers without forcing
    every name to be redeclared here.
    """
    names = [
        t["mcp_server_name"]
        for t in target_tools
        if isinstance(t, dict)
        and t.get("type") == "mcp_toolset"
        and "mcp_server_name" in t
    ]
    return [MCP_SERVERS_BY_NAME[n] for n in names if n in MCP_SERVERS_BY_NAME]


def _skill_key(skill) -> str:
    """Return a comparable identity for a skill entry.

    Skills are dicts like ``{"type": "anthropic", "skill_id": "xlsx"}``.
    For comparison we key on (type, skill_id) which is the unique
    identity the API enforces.
    """
    if hasattr(skill, "model_dump"):
        skill = skill.model_dump(exclude_none=True)
    elif not isinstance(skill, dict):
        skill = {
            k: v
            for k, v in vars(skill).items()
            if not k.startswith("_") and v is not None
        }
    return f"{skill.get('type', '')}:{skill.get('skill_id', '')}"


def _skills_match(live, target) -> bool:
    """Return True if the live skills[] already matches the target.

    Subset semantics: every target skill must appear in live, but live
    may have additional skills (API defaults, manually-attached ones).
    Without subset semantics, every re-run would issue a no-op update
    and bump the version — the same pattern _tools_match implements
    for tools[].
    """
    live_keys = {_skill_key(s) for s in (live or [])}
    target_keys = {_skill_key(s) for s in (target or [])}
    return target_keys.issubset(live_keys)


# ---------------------------------------------------------------------------
# MCP server reconciliation. Plan #44 Task #17 originally introduced
# ``_target_mcp_servers`` to gate-rebuild the SF mcp_servers list from
# the ``SF_MCP_VIA_VAULT`` flag. The Kapa MCP integration generalized
# this: any tools[] roster that carries ``mcp_toolset`` entries needs the
# matching ``mcp_server`` registrations on update, regardless of which
# integration owns them.  ``_required_mcp_servers`` (above) is the new
# canonical helper, driven by the ``MCP_SERVERS_BY_NAME`` registry; it
# handles SF, Kapa, and any future MCP-backed integration the same way.
#
# ``_target_mcp_servers`` is preserved as a thin backwards-compatible
# alias so older tests (Plan #44 Task #17 vault path) and any external
# caller keep working without code-level fan-out. New code should call
# ``_required_mcp_servers`` directly.
# ---------------------------------------------------------------------------
def _target_mcp_servers(target_tools: list) -> list:
    """Backwards-compat alias for :func:`_required_mcp_servers`.

    Returns the mcp_servers list that must accompany this tools[] roster,
    derived by mapping every ``mcp_toolset`` entry's ``mcp_server_name``
    through ``MCP_SERVERS_BY_NAME``. Empty list when no mcp_toolset
    references exist; one entry per registered toolset otherwise.
    """
    return _required_mcp_servers(target_tools)


def _mcp_server_to_dict(server):
    """Normalize a live mcp_servers entry (SDK object or dict) to a dict."""
    if hasattr(server, "model_dump"):
        return server.model_dump(exclude_none=True)
    if isinstance(server, dict):
        return server
    return {
        k: v for k, v in vars(server).items() if not k.startswith("_") and v is not None
    }


def _mcp_servers_match(live: list, target: list) -> bool:
    """Subset-style equality on mcp_servers, matching the tools[] comparator.

    Compared by name (the unique key the API enforces): if the names
    differ, no match. When names match, every authored key in the target
    must appear in the live entry (the live entry may carry server-side
    default fields we don't author, mirroring ``_tools_match`` semantics).
    """
    live_norm = sorted((str(_mcp_server_to_dict(s).get("name") or "")) for s in live)
    target_norm = sorted((str(s.get("name") or "") for s in target))
    if live_norm != target_norm:
        return False
    by_name_live = {
        _mcp_server_to_dict(s).get("name"): _mcp_server_to_dict(s) for s in live
    }
    for t in target:
        live_entry = by_name_live.get(t.get("name"))
        if live_entry is None:
            return False
        for k, v in t.items():
            if k not in live_entry or live_entry[k] != v:
                return False
    return True


def update_one(
    client,
    name: str,
    agent_id: str,
    target_tools: list,
    target_skills: list | None = None,
) -> tuple[str, int | None]:
    """Update one agent.

    Returns ``(status, new_version)`` where ``status`` is one of
    ``'updated'``, ``'unchanged'``, ``'failed'`` and ``new_version`` is
    the post-call live version: the bumped version on ``'updated'``, the
    unchanged live version on ``'unchanged'``, or ``None`` on
    ``'failed'``. The caller uses ``new_version`` to refresh
    ``agents/active_versions.json`` so the post-deploy verify gate
    compares the pin against actual live state.

    Always passes ``mcp_servers`` derived from ``target_tools`` so the
    server registrations stay in lockstep with the mcp_toolset entries.
    The Iter3 ``only-clear-on-removal`` pattern is replaced by
    ``always-set-from-target`` to handle additions correctly — see
    ``_required_mcp_servers`` for the failure mode that drove the change.
    Two scenarios this covers:

    1. **Removal** (Iter3, default path) — target has no mcp_toolset,
       live had a stale SF entry. ``required_servers`` is ``[]`` so the
       update payload carries ``mcp_servers=[]`` and the API drops the
       stale registration.

    2. **Addition** (Kapa, Plan #44 Task #17 vault flag) — target gains
       a new mcp_toolset (Kapa for Post-Sales / Cross-Domain, or SF
       under SF_MCP_VIA_VAULT=true). ``required_servers`` returns the
       matching server config(s) from ``MCP_SERVERS_BY_NAME`` so the
       update lands the toolset reference and its server declaration
       atomically.

    Plan #44 Task #12 — also reconciles the ``skills[]`` field. When
    ``target_skills`` is non-empty, the script computes the union of
    live + target so we never strip skills another path attached. When
    ``target_skills`` is empty/None, skills[] is left alone (the API
    does not require it). The SF_MCP_VIA_VAULT flag is read once at
    SUB_AGENT_DATA_TOOLS module-load time in setup_agents.py; flipping
    the env var at runtime does not change live agent shape — re-run
    this script after the flip.
    """
    deploy_tools = build_tools_for_agent(target_tools)
    target_skills = list(target_skills or [])
    required_servers = _required_mcp_servers(deploy_tools)
    try:
        current = client.beta.agents.retrieve(agent_id)
        live_tools = [_tool_to_dict(t) for t in (current.tools or [])]
        live_mcp_servers = list(current.mcp_servers or [])
        live_skills_raw = list(getattr(current, "skills", None) or [])
        # Normalize live skills into dicts for comparison + update.
        live_skills = [_tool_to_dict(s) for s in live_skills_raw]
        tools_match = _tools_match(live_tools, deploy_tools)
        skills_match = _skills_match(live_skills, target_skills)
        servers_match = _mcp_servers_match(live_mcp_servers, required_servers)
        if tools_match and skills_match and servers_match:
            print(
                f"[SKIP] {name} ({agent_id}): tools[] / skills[] / mcp_servers "
                f"already match target ({len(deploy_tools)} tools, "
                f"{len(target_skills)} skill(s), "
                f"{len(required_servers)} servers, v{current.version})"
            )
            return "unchanged", current.version
        update_kwargs: dict = {
            "version": current.version,
            "tools": deploy_tools,
            "mcp_servers": required_servers,
        }
        if not skills_match and target_skills:
            # Union live + target so we never strip skills attached by
            # other paths (e.g. a future provisioner adding pptx).
            live_keys = {_skill_key(s) for s in live_skills}
            merged: list = list(live_skills)
            for s in target_skills:
                if _skill_key(s) not in live_keys:
                    merged.append(s)
            update_kwargs["skills"] = merged
        updated = client.beta.agents.update(agent_id, **update_kwargs)
        skills_note = (
            f" | skills[]={len(update_kwargs['skills'])}"
            if "skills" in update_kwargs
            else ""
        )
        server_names = ", ".join(s["name"] for s in required_servers) or "(none)"
        print(
            f"[OK] {name} ({agent_id}): v{current.version} -> "
            f"v{updated.version} | tools[]={len(deploy_tools)} "
            f"({', '.join(t.get('name') or t.get('type') for t in deploy_tools)})"
            f"{skills_note} | mcp_servers=[{server_names}]"
        )
        return "updated", updated.version
    except Exception as e:
        print(f"[FAIL] {name} ({agent_id}): {e}")
        return "failed", None


def reconcile_coordinator_tools_and_skills(client) -> tuple[str, int | None]:
    """Reconcile the Coordinator's tools[] + skills[] alongside sub-agents.

    Plan #44 Task #4 + Task #12 + decision row #6. Until this function
    landed, the reconciler only touched the 7 pre-provisioned sub-agents.
    The Coordinator's tools[] could drift between source-of-truth
    (setup_agents.py:Coordinator create-call) and the live agent —
    e.g. if `POST_REPORT_TOOL.description` is beefed up to ≥3 sentences
    but no Coordinator prompt change ships, the deploy-prompts workflow
    is a no-op for that drift. Same for skills[]: attaching the xlsx
    skill ONLY to the Coordinator's setup_agents.create() does not
    propagate to the existing live agent because the create-call only
    fires on fresh provisioning.

    Target shapes (matching setup_agents.py Coordinator create-call):
      tools  = [agent_toolset_20260401, SLACK_TOOL, POST_REPORT_TOOL,
                MATERIALIZE_XLSX_TOOL, REASONING_SUMMARY_TOOL]
      skills = FILE_MATERIALIZING_SKILLS  (the xlsx skill)

    Note: WRITE_PROSE_TOOL was removed 2026-05-27 when the Writing Agent
    moved into the Coordinator's multiagent roster — the Coordinator now
    delegates prose composition via the multiagent runtime, not a custom
    tool.

    Idempotent — same subset semantics as ``update_one``. The
    Coordinator's multiagent block is left untouched here; that's
    handled by ``republish_coordinator_multiagent`` below.

    Returns ``(status, new_version)`` matching ``update_one``.
    """
    coordinator_id = os.environ.get("COORDINATOR_ID", "").strip()
    if not coordinator_id:
        print("[SKIP] coordinator tools/skills: no COORDINATOR_ID in environment")
        return "skipped", None

    # Import lazily so the module's import graph stays clean — setup_agents
    # already exports the constants we need, but we want to defer the
    # POST_REPORT_TOOL build until the function runs (the schema flatten
    # path is exercised the same way as for sub-agents).
    from setup_agents import (  # noqa: WPS433
        MATERIALIZE_XLSX_TOOL,
        POST_REPORT_TOOL,
        REASONING_SUMMARY_TOOL,
        SLACK_TOOL,
    )

    target_tools = [
        {"type": "agent_toolset_20260401"},
        SLACK_TOOL,
        POST_REPORT_TOOL,
        MATERIALIZE_XLSX_TOOL,
        REASONING_SUMMARY_TOOL,
        # The Coordinator has Kapa (search_knowledge_base) per the capability
        # map and its setup_agents tools[]; the reconciler must keep it or a
        # tool-refresh run would strip the Coordinator's knowledge-base access.
        KAPA_ACME_MCP_TOOLSET,
    ]
    target_skills = list(FILE_MATERIALIZING_SKILLS)
    deploy_tools = build_tools_for_agent(target_tools)
    required_servers = _required_mcp_servers(deploy_tools)

    try:
        current = client.beta.agents.retrieve(coordinator_id)
        live_tools = [_tool_to_dict(t) for t in (current.tools or [])]
        live_mcp_servers = list(current.mcp_servers or [])
        live_skills_raw = list(getattr(current, "skills", None) or [])
        live_skills = [_tool_to_dict(s) for s in live_skills_raw]
        tools_match = _tools_match(live_tools, deploy_tools)
        skills_match = _skills_match(live_skills, target_skills)
        servers_match = _mcp_servers_match(live_mcp_servers, required_servers)
        if tools_match and skills_match and servers_match:
            print(
                f"[SKIP] coordinator tools/skills ({coordinator_id}): "
                f"already matches target "
                f"({len(deploy_tools)} tool(s), "
                f"{len(target_skills)} skill(s), "
                f"{len(required_servers)} servers, v{current.version})"
            )
            return "unchanged", current.version
        # Always set mcp_servers when sending the update so a stale
        # ``salesforce`` registration on the live Coordinator (from a
        # pre-Iter3 deploy) is cleared in lockstep with the tools[] update.
        # Without this, the API rejects: ``mcp_servers [salesforce]
        # declared but no mcp_toolset in tools references them``.
        update_kwargs: dict = {
            "version": current.version,
            "tools": deploy_tools,
            "mcp_servers": required_servers,
        }
        if not skills_match and target_skills:
            live_keys = {_skill_key(s) for s in live_skills}
            merged: list = list(live_skills)
            for s in target_skills:
                if _skill_key(s) not in live_keys:
                    merged.append(s)
            update_kwargs["skills"] = merged
        updated = client.beta.agents.update(coordinator_id, **update_kwargs)
        notes = [
            f"tools[]={len(update_kwargs['tools'])}",
            f"mcp_servers=[{', '.join(s['name'] for s in required_servers) or '(none)'}]",
        ]
        if "skills" in update_kwargs:
            notes.append(f"skills[]={len(update_kwargs['skills'])}")
        print(
            f"[OK] coordinator tools/skills ({coordinator_id}): "
            f"v{current.version} -> v{updated.version} | {', '.join(notes)}"
        )
        return "updated", updated.version
    except Exception as e:
        print(f"[FAIL] coordinator tools/skills ({coordinator_id}): {e}")
        return "failed", None


# NOTE on the Quick Answer dual-naming: setup_agents.py prints BOTH
# QUICK_AGENT_ID and QUICK_ANSWER_ID for the same agent, so a forker copying
# its output sets both and this reconciler (which keys on QUICK_ANSWER_ID)
# resolves cleanly. config.py additionally reads either name at runtime.

# Canonical sub-agent ID env-vars allowed in the Coordinator multiagent
# roster. Derived from PRE_PROVISIONED_AGENTS at module load — the source
# of truth is the per-entry ``in_multiagent_roster`` flag. Any agent ID
# the live Coordinator carries that is NOT one of these env-var values
# gets STRIPPED on republish. This is what prevents roster drift when an
# old script run pinned an agent that shouldn't be in the roster
# (Writing Agent, Prompt Engineer, Quick Answer pre-2026-05-15).
def _canonical_roster_env_vars() -> list[str]:
    """Return the env-var names whose IDs may appear in the multiagent roster."""
    out: list[str] = []
    for entry in PRE_PROVISIONED_AGENTS:
        _name, env_var, _t, _s, in_roster = _unpack_entry(entry)
        if in_roster:
            out.append(env_var)
    return out


def _canonical_roster_ids() -> set[str]:
    """Resolve env-var names to the live agent IDs in the canonical roster."""
    return {
        agent_id
        for env_var in _canonical_roster_env_vars()
        if (agent_id := os.environ.get(env_var, "").strip())
    }


def _canonical_roster_complete() -> tuple[bool, list[str]]:
    """Return whether EVERY canonical env var is set (with values list of missing).

    The strip-enforcement in ``republish_coordinator_multiagent`` deletes any
    Coordinator-pinned ID that isn't in the canonical_ids set. When an env
    var is unset, the canonical_ids set is missing that entry — so a previously
    pinned Writing Agent (for example) would get stripped on a deploy run that
    didn't have ``WRITING_AGENT_ID`` set. Worse: the deploy would succeed with
    the broken pin in place, and the next session would have no Writing Agent
    delegation path.

    Defensive policy: when ANY canonical env var is missing, the republisher
    refuses to strip pins (it still adds new IDs to the union, just doesn't
    remove anything). The complete-roster check is what flips the strip mode
    on. Codex P2, 2026-05-27.
    """
    missing: list[str] = [
        env_var
        for env_var in _canonical_roster_env_vars()
        if not os.environ.get(env_var, "").strip()
    ]
    return (len(missing) == 0, missing)


def republish_coordinator_multiagent(
    client, sub_agent_ids: list[str]
) -> tuple[str, int | None]:
    """Re-update the Coordinator's multiagent.agents so sub-agent versions repin.

    When ``beta.agents.create()`` or ``beta.agents.update()`` is given a
    ``multiagent={"agents": [{"id": ...}]}`` block, Anthropic snapshots
    each sub-agent at its current ``version`` and stores that version in
    the parent's stored ``multiagent.agents[].version``. Sub-agent
    updates after that point DO NOT propagate — every session against
    the parent continues to dispatch to the snapshotted versions until
    the parent is updated again with a fresh multiagent block.

    Iter3 shipped seven sub-agent updates that never reached production
    traffic because no one was re-updating the Coordinator. Sessions
    dispatched to Pipeline Monitor v6 (with the SF mcp_toolset + the
    old prompt referencing soqlQuery directly) even though the live
    standalone Pipeline Monitor agent was at v11.

    The fix: after every sub-agent reconciliation, re-issue an
    ``agents.update(coordinator_id, multiagent={...})`` with the union
    of (Coordinator's current pinned roster) ∪ (sub-agents updated in
    this run). The API re-snapshots each sub-agent's current version,
    so the next session created against the Coordinator picks up the
    new sub-agent state. This bumps the Coordinator's own version by 1.

    Plan #44 review concern HIGH/MEDIUM #3 (closing review). If a
    sub-agent is skipped in this run because its ID env var is missing,
    it never reaches ``sub_agent_ids``. If we passed ONLY the updated
    list, the API would CLEAR the missing agents from the pinned
    roster — and the next Coordinator session would have no
    Statistician (or whichever agent was skipped) in its multiagent
    block. The union below prevents that: skipped-due-to-missing-env
    agents stay pinned at whatever version the Coordinator already
    has, and the updated agents repin to their latest version. Order:
    current-pin order first, then any new IDs at the end, so the
    Coordinator's existing routing semantics stay stable.

    Returns ``(status, new_version)`` matching the shape used by
    ``update_one``. Status is ``'updated'`` if the API call happened,
    ``'unchanged'`` if every pinned version already matched live and
    no new IDs need to be added, or ``'failed'``.
    """
    coordinator_id = os.environ.get("COORDINATOR_ID", "").strip()
    if not coordinator_id:
        print("[SKIP] coordinator multiagent: no COORDINATOR_ID in environment")
        return "skipped", None

    canonical_ids = _canonical_roster_ids()
    roster_complete, missing_env_vars = _canonical_roster_complete()
    if not roster_complete:
        print(
            f"[INFO] coordinator multiagent ({coordinator_id}): canonical "
            f"roster has unset env vars ({', '.join(missing_env_vars)}) — "
            f"deferring strip enforcement so existing pins are preserved. "
            f"New IDs from this run will still be added. Set the missing "
            f"env vars and re-run to enforce."
        )

    try:
        coord = client.beta.agents.retrieve(coordinator_id)
        coord_multiagent = getattr(coord, "multiagent", None)
        coord_agents = (
            getattr(coord_multiagent, "agents", None) if coord_multiagent else None
        )
        current_pins: dict[str, int] = {}
        # Preserve the order the Coordinator currently has so the union
        # below keeps the existing routing semantics stable (additions
        # only at the end).
        current_pin_order: list[str] = []
        stripped_ids: list[str] = []
        for entry in coord_agents or []:
            entry_id = getattr(entry, "id", None) or (
                entry.get("id") if isinstance(entry, dict) else None
            )
            entry_version = getattr(entry, "version", None) or (
                entry.get("version") if isinstance(entry, dict) else None
            )
            if entry_id is None or entry_version is None:
                continue
            # ENFORCE the canonical roster: any pin not on the allowlist
            # gets stripped. Prevents roster bloat from stale snapshots
            # (Prompt Engineer, Quick Answer, Dream — all of which are
            # dispatched directly, not via Coordinator). Skip enforcement
            # when ``roster_complete`` is False — that means we couldn't
            # resolve every canonical env var, and we'd otherwise strip a
            # legitimate canonical pin (codex P2, 2026-05-27).
            if roster_complete and canonical_ids and entry_id not in canonical_ids:
                stripped_ids.append(entry_id)
                continue
            current_pins[entry_id] = entry_version
            current_pin_order.append(entry_id)

        # Union: every canonical agent currently pinned, plus every
        # canonical agent updated in this run. Off-roster IDs from
        # ``sub_agent_ids`` are filtered out the same way.
        union_ids: list[str] = list(current_pin_order)
        for sub_id in sub_agent_ids:
            if canonical_ids and sub_id not in canonical_ids:
                # Caller passed an off-roster ID — quietly drop. The
                # ``in_multiagent_roster`` gate in main() already filters
                # most of these out; this is a belt-and-suspenders check.
                continue
            if sub_id not in current_pins:
                union_ids.append(sub_id)

        live_versions: dict[str, int] = {}
        for sub_id in sub_agent_ids:
            if canonical_ids and sub_id not in canonical_ids:
                continue
            sub = client.beta.agents.retrieve(sub_id)
            live_versions[sub_id] = sub.version

        drift = {
            sub_id: (current_pins.get(sub_id), live_versions[sub_id])
            for sub_id in live_versions
            if current_pins.get(sub_id) != live_versions[sub_id]
        }
        added_ids = [sid for sid in union_ids if sid not in current_pins]
        if not drift and not added_ids and not stripped_ids:
            print(
                f"[SKIP] coordinator multiagent ({coordinator_id}): all "
                f"{len(union_ids)} sub-agent pins already match live, no "
                f"new IDs to add, no off-roster pins to strip "
                f"(coord v{coord.version})"
            )
            return "unchanged", coord.version

        updated = client.beta.agents.update(
            coordinator_id,
            version=coord.version,
            multiagent={
                "type": "coordinator",
                "agents": [{"type": "agent", "id": sub_id} for sub_id in union_ids],
            },
        )
        drift_summary = ", ".join(
            f"{sub_id[-8:]}: v{old}->v{new}" for sub_id, (old, new) in drift.items()
        )
        added_note = f"; added {len(added_ids)} new pin(s)" if added_ids else ""
        stripped_note = (
            f"; stripped {len(stripped_ids)} off-roster pin(s) "
            f"({', '.join(sid[-8:] for sid in stripped_ids)})"
            if stripped_ids
            else ""
        )
        added_note = added_note + stripped_note
        print(
            f"[OK] coordinator multiagent ({coordinator_id}): v{coord.version} -> "
            f"v{updated.version} | repinned {len(drift)} sub-agent(s) "
            f"({drift_summary}){added_note} | "
            f"{len(union_ids)} total in roster"
        )
        return "updated", updated.version
    except Exception as e:
        print(f"[FAIL] coordinator multiagent ({coordinator_id}): {e}")
        return "failed", None


def main() -> int:
    client = anthropic.Anthropic()
    results: dict[str, list[str]] = {
        "updated": [],
        "unchanged": [],
        "failed": [],
        "skipped": [],
    }
    new_versions: dict[str, int] = {}
    sub_agent_ids_for_multiagent: list[str] = []
    for entry in PRE_PROVISIONED_AGENTS:
        # Back-compat: tuple may be (name, env_var, tools),
        # (name, env_var, tools, skills), or the new 5-tuple
        # (name, env_var, tools, skills, in_multiagent_roster). See
        # ``_unpack_entry``. Dream Agent (added 2026-05-14) is the
        # first 5-tuple entry — it opts OUT of the Coordinator
        # multiagent re-pin because the orchestrator dispatches Dream
        # directly via scheduled_dream, not through the Coordinator.
        name, env_var, target_tools, target_skills, in_multiagent_roster = (
            _unpack_entry(entry)
        )
        agent_id = os.environ.get(env_var, "").strip()
        if not agent_id:
            print(f"[SKIP] {name}: no {env_var} in environment")
            results["skipped"].append(name)
            continue
        outcome, new_version = update_one(
            client, name, agent_id, target_tools, target_skills
        )
        results[outcome].append(name)
        if new_version is not None:
            new_versions[name] = new_version
            if in_multiagent_roster:
                sub_agent_ids_for_multiagent.append(agent_id)

    # Plan #44 Task #4 + #12 + decision row #6 — reconcile the
    # Coordinator's own tools[] and skills[] (description tweaks +
    # xlsx skill). Runs BEFORE the multiagent re-publish so a single
    # Coordinator update can fold the tools/skills change into the same
    # version bump as the multiagent re-snapshot when both drift.
    coord_tools_status, coord_tools_version = reconcile_coordinator_tools_and_skills(
        client
    )
    if coord_tools_status == "updated" and coord_tools_version is not None:
        new_versions["coordinator"] = coord_tools_version

    # Re-publish the Coordinator's multiagent.agents so its pinned
    # sub-agent versions advance to whatever each sub-agent is at now.
    # Without this step, every sub-agent tools[]/prompt update above is
    # dead-letter for production traffic — sessions created against the
    # Coordinator dispatch to whatever version was pinned at the
    # Coordinator's last update, NOT the latest sub-agent state. See
    # republish_coordinator_multiagent's docstring for the full mechanic.
    #
    # PR 3 (floating-prancing-trinket plan, 2026-05-14): the explicit
    # "any sub-agent updated this run" gate is the closing step. The
    # republish itself also runs on drift-correction (no updates this
    # run but a pin drifted from live for some other reason), so this
    # log line distinguishes the two cases for operators reading CI logs.
    any_subagent_updated = any(
        name in results["updated"]
        for (name, *_) in (_unpack_entry(e) for e in PRE_PROVISIONED_AGENTS)
    )
    coord_status: str | None = None
    coord_new_version: int | None = None
    if sub_agent_ids_for_multiagent:
        if any_subagent_updated:
            print(
                f"[INFO] Sub-agents updated this run ({len(results['updated'])}): "
                f"{', '.join(results['updated'])}. "
                f"Re-publishing Coordinator multiagent.agents to repin."
            )
        coord_status, coord_new_version = republish_coordinator_multiagent(
            client, sub_agent_ids_for_multiagent
        )

    # Refresh agents/active_versions.json with the post-call versions.
    # update_prompts.py writes the pin BEFORE this script runs in
    # .github/workflows/deploy-prompts.yml, so any bump caused by a tools
    # update would otherwise be missing from the pin and the inline
    # verify gate would fail comparing live > pinned. Merge with the
    # existing pin so we never drop entries that this script doesn't
    # manage (e.g. coordinator, quick_answer, dream, monitors,
    # writing_agent — all updated by update_prompts.py).
    if coord_status == "updated" and coord_new_version is not None:
        new_versions["coordinator"] = coord_new_version
    if new_versions:
        merged = read_active_versions()
        changed = {k: v for k, v in new_versions.items() if merged.get(k) != v}
        if changed:
            merged.update(new_versions)
            write_active_versions(merged)
            print(
                f"Refreshed active_versions.json: "
                f"{', '.join(f'{k}=v{v}' for k, v in sorted(changed.items()))}"
            )
        else:
            print("active_versions.json already in sync with live state.")

    print(
        f"\nSummary: {len(results['updated'])} updated, "
        f"{len(results['unchanged'])} unchanged, "
        f"{len(results['failed'])} failed, "
        f"{len(results['skipped'])} skipped."
    )
    # Non-zero exit only on explicit API failure — missing IDs are a
    # config issue, not a deploy failure (matches update_prompts.py).
    return 1 if results["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
