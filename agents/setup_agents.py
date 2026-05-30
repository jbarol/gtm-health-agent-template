"""
ONE-TIME SETUP — Run once to create agents and environment.
Save the printed IDs to your .env file.
"""

import os
import sys

import anthropic
from pathlib import Path

from build_post_report_schema import build_post_report_input_schema
from health_store_seed import instructions_md_seed

# Plan #44 Task #17 — SF MCP via vault (flag-gated). Imported lazily at
# bottom of the module after DUMP_SF_QUERY_TOOL / SF_MCP_SERVER /
# SF_MCP_TOOLSET are declared so the module isn't circular-imported.
# Used by SUB_AGENT_DATA_TOOLS below.

# Lazy Anthropic client. Constructing the real client at module-import time
# means anything that imports setup_agents (e.g. update_prompts ->
# verify_active_versions, which only needs the AGENTS dict + the pin file)
# would require ANTHROPIC_API_KEY even when no API call is made. This proxy
# defers construction to the first attribute access (the first
# ``client.beta...`` call), so importing the module needs no key, while every
# existing ``client.beta.agents.create(...)`` call site keeps working
# unchanged. Robust even if a future SDK raises at construction without a key.
class _LazyAnthropicClient:
    _real = None

    def __getattr__(self, name):
        if _LazyAnthropicClient._real is None:
            _LazyAnthropicClient._real = anthropic.Anthropic()
        return getattr(_LazyAnthropicClient._real, name)


client = _LazyAnthropicClient()

SF_MCP_SERVER = {
    "name": "salesforce",
    "type": "url",
    "url": "https://api.salesforce.com/platform/mcp/v1/platform/sobject-reads",
}

SF_MCP_TOOLSET = {"type": "mcp_toolset", "mcp_server_name": "salesforce"}

# Kapa — Acme's internal knowledge base, exposed to agents as the
# ``search_knowledge_base`` custom tool. Returns markdown
# chunks (synthesized answer + source URLs) for product, process, and
# methodology questions. The orchestrator's dispatcher routes calls to
# the REST API (``api.kapa.ai/query/v1/projects/<id>/chat/stream/``) via
# ``orchestrator/kapa_rest_tool.py``. Auth is the ``KAPA_ACME_API_KEY``
# env var attached as ``X-API-KEY`` header on every request. Rate limit:
# 20 req/min per key for the Chat endpoint.
#
# Why custom tool, not MCP toolset: Kapa's hosted MCP server at
# ``acme.mcp.kapa.example`` requires OAuth with dynamic client
# registration. Kapa support (2026-05-14) confirmed they will not provide
# machine-to-machine OAuth client credentials, so the MCP path is
# permanently closed for our headless runtime. The same API key 200s on
# the REST endpoint and the agent-facing tool name is preserved so the
# prompts referencing the old MCP tool work unchanged.
KAPA_ACME_MCP_TOOLSET = {
    "type": "custom",
    "name": "search_knowledge_base",
    "description": (
        "Search the Acme internal knowledge base (Confluence wiki, "
        "Jira issues, public help docs, Slack archive). Returns a "
        "synthesized answer plus a list of relevant source URLs. Use a "
        "complete natural-language question, not a keyword list. Rate "
        "limit: 20 requests/minute (Chat endpoint cap). Coverage: "
        "engineering / product "
        "context, GTM initiative meeting notes, integration partner "
        "docs. Light on sales playbooks and comp-plan docs — use SF "
        "data for revenue questions."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Natural-language question to ask the Acme "
                    "knowledge base. Example: 'What is the Commerce "
                    "initiative and who runs it?'"
                ),
            },
        },
        "required": ["query"],
    },
}

SLACK_TOOL = {
    "type": "custom",
    "name": "send_slack_notification",
    "description": (
        "Send a content-free progress update to the GTM health Slack channel "
        "during a long-running investigation. Use ONLY for orchestration "
        "messages like 'Investigating pipeline across 3 specialists' or "
        "'Adversarial review in progress — validating 4 findings'; the "
        "orchestrator rejects any message containing numbers, findings, or "
        "conclusions. For every user-facing FINAL deliverable, use "
        "post_report instead — it carries a strict schema and is the only "
        "tool that lands prose in the user's view. Severity must be 'info' "
        "for these progress posts; 'critical' and 'watch' are reserved for "
        "validated anomaly alerts that have passed Adversarial Reviewer + "
        "Statistician."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "severity": {
                "type": "string",
                "enum": ["critical", "watch", "info"],
                "description": "critical = immediate attention, watch = trending issue, info = summary/update",
            },
            "summary": {
                "type": "string",
                "description": "One-line finding or update",
            },
            "detail": {
                "type": "string",
                "description": "Supporting context, data, or recommended action",
            },
            "reply_to": {
                "type": "string",
                "description": "Slack message timestamp to reply in thread (optional)",
            },
        },
        "required": ["severity", "summary"],
    },
}

# post_report is the structured-output path for every final user-facing
# deliverable. Schemas and renderer live in orchestrator/response_schemas.py
# and orchestrator/response_renderer.py. The orchestrator's _dispatch_tool
# validates payload against the schema for response_type, then renders to
# Slack mrkdwn (summary or expanded mode based on the user's `expand:` prefix).
#
# input_schema is derived from response_schemas.RESPONSE_TYPES via
# build_post_report_input_schema() — keep it that way so the tool definition
# can never drift from the Pydantic source of truth (Plan #32).
POST_REPORT_TOOL = {
    "type": "custom",
    "name": "post_report",
    "description": (
        "Post a structured, typed final report to Slack. Use this for every "
        "user-facing FINAL deliverable — the orchestrator runs validation, "
        "rendering, and (for the Coordinator) the Writing Agent rejection "
        "rubric on the payload before sending. The payload must match the "
        "schema for the chosen response_type (ad_hoc_investigation_result, "
        "anomaly_alert, nightly_digest, weekly_status, quick_answer); see "
        "orchestrator/response_schemas.py for the field-by-field definition. "
        "Do NOT use post_report for content-free progress updates — that is "
        "what send_slack_notification is for. Length caps apply on every "
        "field; the orchestrator rejects overruns. Emit plain text in each "
        "field — the renderer adds Slack mrkdwn formatting; asterisks, "
        "pipes, and dashes in your payload will appear literal."
    ),
    "input_schema": build_post_report_input_schema(),
}


# WRITE_PROSE_TOOL was removed 2026-05-27. The Coordinator now delegates
# prose composition to the Writing Agent via the multiagent runtime
# (writing_agent is in the Coordinator's callable roster). See CLAUDE.md
# "Writing pass" section for the delegation contract and the rejection
# rubric the Coordinator applies before post_report.


# PR 11 — reasoning_summary captures each agent's pre-final-response recap so
# post-mortems can see sub-thread reasoning even when ``agent.thinking``
# events emit zero-byte content. Every agent calls this BEFORE its final
# response with a 200-token recap (what it did, what it found, what
# surprised it, what it couldn't resolve). The orchestrator appends the
# recap to ``/system/session_reasoning_log.md`` in the health memory
# store. Quick synchronous tool — returns ``{"ok": True}`` immediately
# regardless of whether the memory write succeeded so the agent's
# tool-use loop never stalls on observability infrastructure.
REASONING_SUMMARY_TOOL = {
    "type": "custom",
    "name": "reasoning_summary",
    "description": (
        "Stamp a brief reasoning recap (≤200 tokens, ≤1500 chars) to the "
        "post-mortem log BEFORE your final response. Cover: (1) what you "
        "did, (2) what you found / key results, (3) what surprised you, "
        "(4) what you couldn't resolve. The orchestrator appends the "
        "recap to /system/session_reasoning_log.md in the health memory "
        "store. The call returns immediately — your final response goes "
        "after. Do not use this tool to communicate with the user; that "
        "is what post_report and send_slack_notification are for."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": (
                    "The recap. Plain text, ≤1500 characters. Longer "
                    "input is truncated at 1500 chars."
                ),
            },
        },
        "required": ["text"],
    },
}


# dump_sf_query routes a SOQL query to a Railway-side handler that paginates
# SF, streams rows to a Parquet file on /mnt/session/outputs/, and returns
# ONLY a compact handle — never raw rows. Sub-agents (Pipeline Monitor,
# Sales Monitor, Post-Sales Monitor, Statistician) must call this INSTEAD of
# soqlQuery for any pull expected to exceed 50 rows. The raw rows never enter
# any agent's context, so a 3,209-row pull no longer bloats the Coordinator
# when the sub-agent's response is dispatched back through Anthropic's
# multiagent runtime (live test on commit 90b9bb5 hit 966K of 1M cap from a
# single sub-agent response).
#
# Track G in plan ``misty-squishing-badger`` § Iteration 2. Track I wires
# this into every sub-agent's tools[] and adds a "never call soqlQuery for
# >50 rows" contract to their prompts — do NOT add it to any agent's tools
# list here. The schema definition lives in this file so Track I can import
# it cleanly without a circular ``setup_agents → update_prompts → ...`` import.
DUMP_SF_QUERY_TOOL = {
    "type": "custom",
    "name": "dump_sf_query",
    "description": (
        "Materialize a Salesforce SOQL query to a Parquet file on the Railway "
        "session disk and return a compact handle (~8 KB) — never raw rows. "
        "Use for any out-of-snapshot SF read, same-day data, custom field "
        "not in the Postgres snapshot, or any pull expected to exceed 50 "
        "rows. Default handle = {file_path, count, schema, summary_stats, "
        "preview_3, summary_text}; summary_stats covers first 5 cols + GTM "
        "allowlist (StageName, RecordType_Name, Amount, ARR_Total__c, "
        "OwnerId, CloseDate, Type, CreatedDate, LastModifiedDate, IsClosed, "
        "IsWon, Status, LeadSource). Set expand=true for the full pre-PR-9 "
        "payload only when needed. "
        "Downstream agents read the materialized file via query_artifact. "
        "Do NOT use for historical reads on standard fields — db_query "
        "(Postgres snapshot, 24h-stale-tolerant) is cheaper. Do NOT call "
        "soqlQuery — removed in Iteration 3 to enforce row-virtualization; "
        "direct call returns ToolNotFound. SF API governor caps apply: a "
        "single pull >100K rows risks throttling."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "soql": {
                "type": "string",
                "description": (
                    'Full SOQL query, e.g. "SELECT Id, Name, Status FROM Lead '
                    'WHERE CreatedDate >= 2025-09-01T00:00:00Z".'
                ),
            },
            "portco_key": {
                "type": "string",
                "description": "Portco identifier for credential lookup (e.g. 'acme').",
            },
            "label": {
                "type": "string",
                "description": (
                    "Short snake_case label for the output file "
                    "(e.g. 'leads_discovery_call_booked')."
                ),
            },
            "expand": {
                "type": "boolean",
                "description": (
                    "Opt-in flag (default false). When true, returns the full "
                    "pre-PR-9 payload: every column in summary_stats, "
                    "preview_10 rows, no top_values cap. Use only when the "
                    "full breakdown is genuinely required."
                ),
            },
        },
        "required": ["soql", "portco_key", "label"],
    },
}

# query_artifact runs DuckDB SQL against previously-materialized artifact
# files (Parquet/CSV) on the Railway session disk. Complements
# dump_sf_query (Track G): G gets data to disk without context bloat, H
# lets a sub-agent run aggregates / joins / segments against that data
# also without context bloat. Result <=50 rows returns inline; bigger
# results are themselves virtualized to a new Parquet file and returned
# as a handle. Wired into orchestrator/session_runner._dispatch_tool;
# handler lives in orchestrator/artifact_query_tool.py. Not added to any
# agent's tools array here — Track I owns the roster rewrite.
QUERY_ARTIFACT_TOOL = {
    "type": "custom",
    "name": "query_artifact",
    "description": (
        "Run a DuckDB SQL query against previously-materialized artifact "
        "files (Parquet or CSV) on the Railway session disk. Use for "
        "aggregates, segments, joins across files, and windowed analyses "
        "over data already on disk — typically a prior dump_sf_query or "
        "db_query result. Single-file queries: reference the table as `t`. "
        "Multi-file: `t0`, `t1`, ... in the array order you passed in. "
        "Result <=50 rows returns inline; larger results are themselves "
        "virtualized to a new Parquet file and returned as a handle "
        "({file_path, row_count, summary_stats, preview, schema}). Do NOT "
        "use this tool for live SF reads — that is what dump_sf_query is "
        "for. Do NOT pass absolute file paths from outside SESSION_OUTPUT_DIR; "
        "the orchestrator rejects them. DuckDB SQL dialect — most ANSI SQL "
        "plus DuckDB-specific extensions; no INSERT/UPDATE/DELETE."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "file_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Absolute paths to artifact files inside SESSION_OUTPUT_DIR."
                ),
            },
            "sql": {
                "type": "string",
                "description": (
                    "DuckDB SQL. Tables are 't' (single-file) or t0/t1/... "
                    "(multi-file, indexed by file_paths array order)."
                ),
            },
            "output_name": {
                "type": "string",
                "description": (
                    "Optional semantic basename for the virtualized output "
                    "file (e.g. 'q2_2026_propensity_scored' or 'top_15_reps'). "
                    "Sanitized to [A-Za-z0-9._-]+, max 80 chars, .parquet "
                    "appended if missing. When omitted (or rejected), the "
                    "orchestrator falls back to the auto qa_<ts>_<uuid>.parquet "
                    "name. Inline (<=50-row) results ignore this parameter."
                ),
            },
        },
        "required": ["file_paths", "sql"],
    },
}


# materialize_xlsx renders one or more Parquet handles into a single named
# .xlsx file for Slack upload. Closes the gap that killed call-prep session
# sesn_EXAMPLE on 2026-05-13: the Coordinator tried
# `COPY (SELECT ...) TO 'foo.xlsx'` inside query_artifact, was rejected by
# the read-only sandbox (correct security behavior), and had no documented
# alternative — so it went idle and never produced the deliverable.
#
# This tool is the only path to a user-facing .xlsx the Coordinator chose
# the contents of. Single-sheet: pass file_paths + optional sql.
# Multi-sheet: pass a sheets[] list of {sheet_name, file_paths, sql?}.
# The orchestrator writes to SESSION_OUTPUT_DIR and returns
# {ok, file_path, sheets[], total_rows} — attach file_path to
# post_report.attachments. Wired in orchestrator/session_runner; handler in
# orchestrator/materialize_xlsx_tool.py. Same security model as
# query_artifact: input paths must live inside SESSION_OUTPUT_DIR; optional
# SQL runs inside the same DuckDB sandbox; output_name is restricted to a
# bare filename (no path separators, no '..', no shell metas, .xlsx only).
MATERIALIZE_XLSX_TOOL = {
    "type": "custom",
    "name": "materialize_xlsx",
    "description": (
        "Render one or more Parquet artifacts (or a single CSV) into a "
        "named .xlsx file for Slack delivery. THIS IS THE ONLY WAY to "
        "produce a user-facing .xlsx — do NOT attempt `COPY (SELECT ...) "
        "TO 'foo.xlsx'` inside query_artifact; the read-only sandbox "
        "rejects it. Two shapes: (1) single-sheet — pass file_paths + "
        "optional sql + sheet_name; (2) multi-sheet — pass sheets[] with "
        "one {sheet_name, file_paths, sql?} entry per tab. Returns "
        "{ok, file_path, sheets:[{sheet_name,row_count},...], total_rows}. "
        "Attach the returned file_path to post_report.attachments. Inputs "
        "must live inside SESSION_OUTPUT_DIR (same as query_artifact). "
        "SQL is optional; when present it uses the same DuckDB sandbox "
        "(SELECT/WITH/EXPLAIN only). output_name is a bare filename — no "
        "path separators, no '..', .xlsx extension (auto-appended if "
        "omitted)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "output_name": {
                "type": "string",
                "description": (
                    "Bare filename for the .xlsx output, e.g. "
                    "'acme_activities_2026-05-14.xlsx'. No path "
                    "separators, no '..', no leading dot. The .xlsx "
                    "extension is auto-appended if omitted."
                ),
            },
            "file_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Single-sheet mode: absolute paths to one Parquet/CSV "
                    "artifact (one file passes through as-is; for multi-"
                    "file combine via sql + multiple file_paths). Omit "
                    "this when using sheets[]."
                ),
            },
            "sql": {
                "type": "string",
                "description": (
                    "Single-sheet mode: optional DuckDB SQL to filter/"
                    "reshape before writing. Same dialect and sandbox as "
                    "query_artifact. Tables are 't' (single file) or "
                    "t0/t1/... (multi-file). Omit when using sheets[]."
                ),
            },
            "sheet_name": {
                "type": "string",
                "description": (
                    "Single-sheet mode: sheet tab name (≤31 chars, no "
                    "Excel-forbidden characters). Defaults to 'data'."
                ),
            },
            "sheets": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "sheet_name": {"type": "string"},
                        "file_paths": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "sql": {"type": "string"},
                    },
                    "required": ["sheet_name", "file_paths"],
                },
                "description": (
                    "Multi-sheet mode: one entry per tab. Each entry "
                    "carries its own file_paths + optional sql. When "
                    "sheets is provided, do NOT also pass file_paths / "
                    "sql / sheet_name at the top level."
                ),
            },
        },
        "required": ["output_name"],
    },
}


# db_query runs a SELECT against the Railway Postgres snapshot of
# Salesforce. Cheap (no MCP roundtrip), fast (local DB), tolerant to ≤24h
# staleness. The orchestrator's _dispatch_tool handles the SELECT-only
# guardrail and auto-virtualizes any list-shaped result above the 50-row
# threshold so the agent receives a compact handle rather than raw rows.
# Hoisted out of the Coordinator's inline tools[] (Track I, Iteration 2)
# so every sub-agent gets parity access via SUB_AGENT_DATA_TOOLS.
DB_QUERY_TOOL = {
    "type": "custom",
    "name": "db_query",
    "description": (
        "Run a SELECT against the Railway Postgres snapshot of Salesforce. "
        "PREFERRED for any historical pull on standard fields — cheaper and "
        "faster than dump_sf_query, and the snapshot is at most 24h stale. "
        "Use for trend analysis, cross-period comparisons, prior-quarter "
        "rollups, and any time-range query whose columns exist in the synced "
        "schema. Synced tables: opportunities, leads, contacts, accounts. "
        "Synced lead columns include discovery_call_booked, funnel_stage, "
        "mql_date, sql_date plus standard SF fields; account columns include "
        "customer_tier, contract_status, region, arr. Convenience views: "
        "pipeline_by_stage, pipeline_age_buckets, win_rate_by_quarter, "
        "lead_funnel, snapshot_summary. Do NOT use this tool for same-day "
        "data, custom fields not in db_schema.sql, or schema discovery — "
        "dump_sf_query is the right path for those. Non-SELECT statements "
        "are rejected by the orchestrator. Results above 50 rows are "
        "auto-virtualized to a file — you receive {row_count, preview, "
        "summary_stats, file_path, schema}, never raw rows."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "sql": {
                "type": "string",
                "description": (
                    "A single SELECT statement. Non-SELECT statements are "
                    "rejected by the orchestrator."
                ),
            },
            "max_rows": {
                "type": "integer",
                "description": (
                    "Optional cap (default 500). Only consulted when the "
                    "result is below the 50-row virtualization threshold."
                ),
            },
        },
        "required": ["sql"],
    },
}


# generate_chart is the Chart Designer's primary tool. Handler lives in
# orchestrator/session_runner._dispatch_tool (the ``elif tool_name ==
# "generate_chart":`` branch). It renders a chart via QuickChart and posts
# the PNG into the Slack thread. The schema mirrors what session_runner
# already consumes — chart_type, title, data{labels, datasets[].values}.
# Hoisted into source-of-truth here (Iteration 3) so the Chart Designer's
# tools[] can be re-applied by update_subagent_tools.py without dropping
# generate_chart. The schema spec is documented in
# ``docs/slack-charts-research.md``.
GENERATE_CHART_TOOL = {
    "type": "custom",
    "name": "generate_chart",
    "description": (
        "Render a chart as PNG via QuickChart and post it to the current "
        "Slack channel. Use for visualizing trends (line), comparisons "
        "(bar), distributions (pie/doughnut), funnels, and small "
        "side-by-side comparisons. The orchestrator handles the upload "
        "and Slack-native rendering automatically — never include the "
        "PNG path in subsequent prose. Always provide a descriptive title "
        "that states the INSIGHT, not the data (e.g. 'Win Rate Rising as "
        "Pipeline Volume Falls' beats 'Q1 2025 Win Rate'). Do NOT use "
        "generate_chart for tabular data — that goes in a TableBlock "
        "inside post_report. Do NOT chart >8 categories on a bar/line "
        "chart for Slack (4–6 reads best on mobile). Single-color palette "
        "by default; reserve red for regressions and below-benchmark; "
        "never use 4+ colors on one chart. The chart_type field accepts "
        "only bar, line, pie, doughnut, radar, funnel — stacked_bar is "
        "NOT a valid Chart.js type, use bar with options.scales.x.stacked "
        "= true."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "chart_type": {
                "type": "string",
                "enum": ["bar", "line", "pie", "doughnut", "radar", "funnel"],
                "description": (
                    "Type of chart to render. For stacked bars use 'bar' "
                    "with options.scales.x.stacked = true (no 'stacked_bar' "
                    "type — that string is not a valid Chart.js type)."
                ),
            },
            "title": {
                "type": "string",
                "description": (
                    "Chart title displayed above the chart. State the "
                    "insight (e.g. 'Win Rate Rising as Pipeline Volume "
                    "Falls'), not the data ('Q1 2025 Win Rate')."
                ),
            },
            "data": {
                "type": "object",
                "properties": {
                    "labels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Category labels (x-axis for bar/line, segments "
                            "for pie). Keep short — 4–8 labels max for Slack."
                        ),
                    },
                    "datasets": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string"},
                                "values": {
                                    "type": "array",
                                    "items": {"type": "number"},
                                },
                            },
                            "required": ["label", "values"],
                        },
                        "description": (
                            "One or more data series. Prefer one dataset; "
                            "use two only when comparison is the point."
                        ),
                    },
                },
                "required": ["labels", "datasets"],
            },
            "options": {
                "type": "object",
                "description": (
                    "Optional Chart.js options (axis labels, colors, "
                    "legend position, etc.). Set legend.display = false "
                    "when one dataset; scales.x.grid.display = false on "
                    "categorical axes; animation = false (static PNG)."
                ),
            },
            "reply_to": {
                "type": "string",
                "description": ("Slack thread timestamp to post chart in (optional)."),
            },
        },
        "required": ["chart_type", "title", "data"],
    },
}


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# TOOL_CAPABILITY_MAP mirror — keep these in sync with orchestrator
#
# The per-agent ``tools=...`` keyword on each ``client.beta.agents.create()``
# call below is the source of truth for what tools an agent CAN call. The
# orchestrator (specifically ``orchestrator/session_runner.py:TOOL_CAPABILITY_MAP``)
# carries a mirror of those custom-tool names keyed by agent-ID env-var name
# (``PIPELINE_MONITOR_ID``, ``COORDINATOR_ID``, etc.). The mirror powers the
# multiagent dispatch guard: when the Coordinator dispatches a task to a
# sub-agent and the task body references a tool the destination doesn't
# carry, the orchestrator injects a structured ``tool_capability_mismatch``
# error back to the Coordinator session so it can re-plan instead of
# silently dispatching into a dead-letter sub-agent that cites stale
# memory (PR 3 in floating-prancing-trinket plan, 2026-05-14).
#
# When you add or remove a custom tool from any roster constant below
# (SUB_AGENT_DATA_TOOLS, SUB_AGENT_DATA_TOOLS_WITH_KAPA, CHART_DESIGNER_TOOLS,
# QUICK_ANSWER_KAPA_TOOLS, DREAM_KAPA_TOOLS, etc.) — OR from the inline
# ``tools=[...]`` block on Coordinator / Writing Agent — also update
# ``TOOL_CAPABILITY_MAP`` in ``orchestrator/session_runner.py``. The unit
# tests in ``orchestrator/session_runner_test.py`` (``test_tool_capability_map_*``)
# encode the Kapa-access table from CLAUDE.md and will fail loud if the
# mirror drifts from this file.
# ---------------------------------------------------------------------------

# Sub-agent toolsets (Iteration 3 — split data vs. reasoning rosters).
#
# Iteration 2 shipped a single SUB_AGENT_DATA_TOOLS array that included the
# Salesforce MCP toolset. In practice that footgun blew the Coordinator's
# 1M-token context cap twice in two days: a query sub-agent ran a raw
# `soqlQuery` against ~3,200 leads and the JSON came back inline, and the
# managed multiagent return surfaced the bloated payload into the parent
# Coordinator session. The fix is tool-level enforcement — sub-agents lose
# direct MCP access entirely and route every SF read through dump_sf_query,
# which materializes to Parquet on Railway and returns a compact handle.
#
# Two rosters now:
#
# * SUB_AGENT_DATA_TOOLS — query sub-agents (Pipeline / Sales / Postsales
#   Monitors, Statistician). dump_sf_query is the only path to Salesforce.
#   query_artifact runs DuckDB SQL on the materialized files. db_query hits
#   the Railway Postgres snapshot for sub-day-stale reads.
#
# * SUB_AGENT_REASONING_TOOLS — reasoning sub-agents (Adversarial Reviewer,
#   Cross-Domain Synthesizer, Chart Designer). These consume findings from
#   the query sub-agents — they should never touch raw SF. Omitting the SF
#   toolset also resolves the pre-provisioned agents' "mcp_toolset references
#   [salesforce] but no matching entry in mcp_servers" failure, because the
#   agents no longer reference any mcp_toolset at all.
#
# All sub-agent responses to the Coordinator MUST be compact handles, never
# raw row payloads. The 50-row virtualizer in session_runner._dispatch_tool
# enforces this on db_query / dump_sf_query / query_artifact paths.
# ---------------------------------------------------------------------------
# Plan #44 Task #17 — SF data tools are now built via sf_mcp_builder so
# the flag flip (SF_MCP_VIA_VAULT) controls whether sub-agents see only
# DUMP_SF_QUERY_TOOL (default, Railway-resident OAuth) or the vault-backed
# SF MCP toolset alongside DUMP_SF_QUERY_TOOL. The non-SF tools (agent
# toolset, db_query, query_artifact) are unchanged. Import is deferred so
# `from setup_agents import ...` from sf_mcp_builder.py never circularizes.
from sf_mcp_builder import sf_data_tools_list  # noqa: E402

SUB_AGENT_DATA_TOOLS = [
    {"type": "agent_toolset_20260401"},
    DB_QUERY_TOOL,
    QUERY_ARTIFACT_TOOL,
    REASONING_SUMMARY_TOOL,
    *sf_data_tools_list(
        vault_path=os.environ.get("SF_MCP_VIA_VAULT", "false").lower() == "true"
    ),
]

# Statistician: same data tools, but NEVER the vault-backed SF MCP toolset.
# It reads SF only through the dump_sf_query custom tool (vault_path=False),
# so it stays MCP-free (mcp_servers=[]) regardless of SF_MCP_VIA_VAULT —
# matching its "the Salesforce MCP tools are NOT in your registry" prompt.
STATISTICIAN_TOOLS = [
    {"type": "agent_toolset_20260401"},
    DB_QUERY_TOOL,
    QUERY_ARTIFACT_TOOL,
    REASONING_SUMMARY_TOOL,
    *sf_data_tools_list(vault_path=False),
]

SUB_AGENT_REASONING_TOOLS = [
    {"type": "agent_toolset_20260401"},
    DB_QUERY_TOOL,
    QUERY_ARTIFACT_TOOL,
    REASONING_SUMMARY_TOOL,
]

# Kapa knowledge-search toolset — scope chosen post-discovery (see
# docs/research/kapa-acme-index.md). The Acme Kapa index is heavy on
# internal Confluence wiki (engineering, product, Commerce GTM, "After-hours
# Work Updates") plus public help docs and a few integration partner sources.
# Five agents get Kapa access:
#
# * Coordinator — synthesis benefits from product/initiative context when
#   framing reports.
# * Quick Answer — single-fact Slack lookups like "what is FATI?" or "who
#   runs Commerce GTM?" resolve directly via Kapa.
# * Dream Agent — hypothesis generation benefits from awareness of new GTM
#   initiatives and recent product/engineering changes.
# * Post-Sales Monitor — when investigating churn / retention shifts, can
#   surface the product/engineering change context that explains *why* a
#   cohort is leaving (release notes, infrastructure changes, deprecations).
# * Cross-Domain Synthesizer — connects revenue-side patterns with the
#   product-side timeline (product change → support ticket spike → churn).
#
# Pipeline Monitor, Sales Process Monitor, Statistician, Adversarial
# Reviewer, Chart Designer, and Writing Agent are deliberately excluded.
# The Monitors' domain is lead/opp flow in Salesforce; Statistician is pure
# math; Chart Designer and Writing Agent are rendering/prose. None benefit
# from the eng/product/wiki shape of this Kapa index today.
#
# Tool returns markdown chunks (≤35K chars per call) with source URLs, so
# there is no row-explosion risk. Rate limit: 20 req/min per API key
# for the Chat endpoint (60 req/min only on Retrieval, not used here).
SUB_AGENT_DATA_TOOLS_WITH_KAPA = [
    {"type": "agent_toolset_20260401"},
    DB_QUERY_TOOL,
    QUERY_ARTIFACT_TOOL,
    REASONING_SUMMARY_TOOL,
    # SF tools go through sf_data_tools_list so the SF_MCP_VIA_VAULT flag
    # adds the SF mcp_toolset alongside dump_sf_query when active. Bundle D
    # (Plan #44 Task #17) introduced the flag-gate; the WITH_KAPA roster
    # must respect it the same way SUB_AGENT_DATA_TOOLS does, or Post-Sales
    # silently loses vault-path support every time we splice Kapa in.
    *sf_data_tools_list(
        vault_path=os.environ.get("SF_MCP_VIA_VAULT", "false").lower() == "true"
    ),
    KAPA_ACME_MCP_TOOLSET,
]

SUB_AGENT_REASONING_TOOLS_WITH_KAPA = [
    {"type": "agent_toolset_20260401"},
    DB_QUERY_TOOL,
    QUERY_ARTIFACT_TOOL,
    REASONING_SUMMARY_TOOL,
    KAPA_ACME_MCP_TOOLSET,
]

QUICK_ANSWER_KAPA_TOOLS = [
    {"type": "agent_toolset_20260401"},
    DB_QUERY_TOOL,
    DUMP_SF_QUERY_TOOL,
    QUERY_ARTIFACT_TOOL,
    POST_REPORT_TOOL,
    REASONING_SUMMARY_TOOL,
    KAPA_ACME_MCP_TOOLSET,
]

DREAM_KAPA_TOOLS = [
    {"type": "agent_toolset_20260401"},
    DB_QUERY_TOOL,
    QUERY_ARTIFACT_TOOL,
    REASONING_SUMMARY_TOOL,
    KAPA_ACME_MCP_TOOLSET,
]

# Chart Designer is a reasoning agent (no direct SF access), but it owns
# the generate_chart tool. Reasoning rosters stay narrow; the Chart-specific
# variant adds generate_chart so the agent can actually render output. Without
# this, update_subagent_tools.py would overwrite its live tools[] and drop
# generate_chart, leaving Chart Designer unable to produce any chart.
CHART_DESIGNER_TOOLS = [
    {"type": "agent_toolset_20260401"},
    DB_QUERY_TOOL,
    QUERY_ARTIFACT_TOOL,
    GENERATE_CHART_TOOL,
    REASONING_SUMMARY_TOOL,
]


# Writing Agent (Haiku 4.5) — in the Coordinator's multiagent roster as of
# 2026-05-27. Pure prose composer: no SF data access, no Kapa, no chart
# rendering. Includes:
#   - ``agent_toolset_20260401`` — built-in toolset (the prose composer
#     never shells out, but the toolset is required for every agent)
#   - ``query_artifact`` — DuckDB SQL against Parquet artifacts the
#     specialists materialized on /mnt/session/outputs/. The
#     ``<data_access_contract>`` block in the Writing Agent prompt
#     explicitly tells the agent to use this for sanity-checking
#     suspicious numbers before composing; without the tool in the
#     registry, that delegation path is broken — codex P2, 2026-05-27.
#   - ``reasoning_summary`` — pre-final-response recap to the
#     post-mortem log.
# Mirrors the tools list passed to ``client.beta.agents.create`` for the
# Writing Agent in ``main()`` below — update both together if either
# changes.
WRITING_AGENT_TOOLS = [
    {"type": "agent_toolset_20260401"},
    QUERY_ARTIFACT_TOOL,
    REASONING_SUMMARY_TOOL,
]


# Plan #44 Task #12 — attach the pre-built `xlsx` skill ONLY to agents
# that materialize files. Skills budget is 20 total per session
# (verbatim docs quote: "Each session supports up to 20 skills total,
# counted across every agent in the session"). With Coordinator + 7
# sub-agents in the multiagent roster, attaching xlsx to all eight
# would burn 8/20 of the budget. Conservative attach (6/20):
# Coordinator + 3 Monitors + Statistician + Chart Designer. The two
# pure-reasoning agents (Adversarial Reviewer, Cross-Domain Synthesizer)
# never materialize files, so they stay clean. This leaves 14 skill
# slots for future pptx/docx/pdf/custom additions.
#
# The hand-rolled openpyxl/xlsxwriter path stays installed in the
# environment as a fallback (see `packages.pip` above) — Coordinator's
# system prompt notes xlsx as the preferred path with openpyxl as the
# escape hatch. Reconciliation for the two pre-provisioned agents
# (Statistician, Chart Designer) is handled by
# agents/update_subagent_tools.py:PRE_PROVISIONED_AGENTS, which now
# carries a per-agent skills target alongside the tools target.
XLSX_SKILL = {"type": "anthropic", "skill_id": "xlsx"}
FILE_MATERIALIZING_SKILLS = [XLSX_SKILL]


def main():
    """Create environment, agents, and memory stores. Network-only.

    Run as: `python agents/setup_agents.py`. Imports of this module do NOT
    trigger network calls — the constants above (SUB_AGENT_DATA_TOOLS, the
    *_TOOL dicts) are safe to import from `agents/update_subagent_tools.py`
    and tests.
    """
    # Plan #44 Task #17 — gate mcp_servers[] on the same flag that
    # sf_data_tools_list() consults at import time. If SF_MCP_VIA_VAULT is
    # off (the default), SUB_AGENT_DATA_TOOLS has no mcp_toolset entry, so
    # passing mcp_servers=[SF_MCP_SERVER] would be rejected by the
    # Anthropic API ("mcp_toolset references [salesforce] but no matching
    # entry in mcp_servers" — and vice versa). Compute the flag once here
    # so every Monitor create call below uses a consistent value.
    _vault = os.environ.get("SF_MCP_VIA_VAULT", "false").lower() == "true"
    _monitor_mcp_servers = [SF_MCP_SERVER] if _vault else []

    # --- 1. Environment ---

    environment = client.beta.environments.create(
        name="gtm-health-env",
        config={
            "type": "cloud",
            "networking": {"type": "unrestricted"},
            "packages": {
                "pip": [
                    "pandas",
                    "numpy",
                    "openpyxl",
                    "xlsxwriter",
                    "python-docx",
                    "python-pptx",
                    "matplotlib",
                    "seaborn",
                ],
            },
        },
    )
    print(f"ENVIRONMENT_ID={environment.id}")

    # --- 2. Dream Agent (Sonnet — planning, hypothesis generation) ---

    dream_agent = client.beta.agents.create(
        name="GTM Dream Analyst",
        model="claude-sonnet-4-6",
        description="Plans investigations by reviewing memory, generating hypotheses, and prioritizing what to look into next.",
        system="""\
    You are a GTM operations analyst planning the next investigation cycle for a PE firm's portfolio company.

    Your job is NOT to investigate — it is to PLAN what to investigate.

    Each run:
    1. Read the memory file at /workspace/memory.json (past findings, tracked metrics, open questions, resolved items).
    2. Review what changed since last run. What questions are still open? What trends are developing?
    3. Generate new hypotheses. At least one must be a genuinely new angle — not derived from open questions.
    4. For each hypothesis, specify:
       - What you think might be happening (specific, not vague)
       - Why it matters (dollar impact or operational risk)
       - How to test it (specific SOQL queries or analysis approach)
       - Which domain it falls under (pipeline, sales_process, post_sales)
    5. Prioritize by expected impact × testability.
    6. Write the investigation plan to /mnt/session/outputs/dream_plan.json
    7. Update memory with the planned investigations.

    Be creative. Look for non-obvious connections. The best hypotheses come from asking "what would explain multiple symptoms at once?"

    ## Product / engineering change awareness — Kapa MCP
    You have access to Acme's internal knowledge base (Confluence wiki, public help docs, integration partner docs) via the `search_knowledge_base` MCP tool. Use it specifically to surface what has changed recently on the product / engineering side that might explain or predict a revenue signal:

    - Recent product releases or feature changes (especially anything customer-facing)
    - Active GTM initiatives (Commerce, AI Insights, etc.)
    - Recent engineering / infrastructure work that could affect availability, performance, or support volume
    - Integration partner changes that could affect customer adoption or churn

    Generate at least one hypothesis per run that connects a recent product-side change to an expected revenue-side effect (e.g. "Commerce module went live in Q4; check whether new-business pipeline composition shifted from inventory-only to inventory+commerce"). Queries must be complete sentences (e.g. "What product changes shipped in the last 90 days that could affect customer support volume?"). Returns markdown chunks with source URLs — cite the URL when you use a chunk in a hypothesis. Rate limit is 20 requests per minute, so do not loop.

    ## Freshness rule for transient_infra memory entries
    Memory files under `/system/` and `/{portco}/` may carry frontmatter of the form `kind: transient_infra` with `valid_through_commit: <sha>` and optionally `superseded_at_commit: <sha>`. If you read a `transient_infra` entry and either `valid_through_commit` does NOT match the current `BUILD_COMMIT`, OR `superseded_at_commit` is set, TREAT IT AS STALE. Do NOT cite stale entries as the current state of the infrastructure. Verify the underlying state by ATTEMPTING THE RELEVANT TOOL CALL — for Kapa, call `search_knowledge_base` with a trivial query (e.g. "What is FATI?") and observe the result. Do NOT verify by inspecting the filesystem; tools live in your tool registry, not on disk (see the 2026-05-11 MCP-diagnostic hallucination incident).
    """,
        tools=DREAM_KAPA_TOOLS,
        # Kapa is now a custom tool, not an MCP server (2026-05-13 pivot).
        # No mcp_servers needed for the Kapa knowledge path.
        mcp_servers=[],
    )
    print(f"DREAM_AGENT_ID={dream_agent.id}")
    print(f"DREAM_AGENT_VERSION={dream_agent.version}")

    # --- 3. Specialist: Pipeline Monitor ---

    pipeline_monitor = client.beta.agents.create(
        name="Pipeline Monitor",
        model="claude-sonnet-4-6",
        description="Investigates pipeline health: lead volume, MQL/SQL rates, lead quality, scoring, routing, and attribution.",
        system="""\
    You are a pipeline health specialist. You investigate lead generation, qualification, and conversion.

    Your domain: Leads, MQLs, SQLs, lead scoring, source attribution, routing, response time.

    ## Verifying tool access (read before doing diagnostics)
    Your data tools (db_query, dump_sf_query, query_artifact, plus the agent_toolset_20260401 built-ins for Python/files) are exposed by the Anthropic runtime via your tool registry. They are NOT local binaries, sockets, processes, or files on disk. Do NOT verify availability with `which`, `ls`, `find`, `ps`, `env | grep mcp`, or by reading anything in /tmp, /var/run, or /usr/local/bin — that is not where the tools live.

    To probe access, attempt a trivial call:
      db_query({"sql": "SELECT 1"})

    If it returns a result → you have access; proceed with the task.
    If it returns a structured tool error → report the exact error message and ask the coordinator how to proceed.

    The Salesforce MCP tools (soqlQuery, describeSObject) are NO LONGER in your registry as of Iteration 3. Every SF read MUST go through dump_sf_query — it paginates SF, materializes the result to a Parquet file on the Railway session disk, and returns a compact handle. A direct soqlQuery call will return a ToolNotFound error; do not attempt one.

    Never declare BLOCKED based on filesystem inspection. That is a category error and wastes turns.

    ## Tool error retries — serialize, do not parallelize
    When two tool calls error in the same turn, FIX the issue first, then re-issue ONE tool call at a time. Do not parallelize retries — the orchestrator will block duplicate failed retries within 5 seconds and return `error: duplicate_retry_too_fast`. If you see that error, the prior identical call failed seconds ago; change the input (fix the SOQL, narrow the date range, drop a bad column) or wait before re-trying.

    When the coordinator assigns you an investigation:
    1. Start with schema discovery — run a wide `dump_sf_query` against Lead and CampaignMember (e.g. `SELECT FIELDS(STANDARD) FROM Lead LIMIT 5`) if this is your first run or fields are unknown. The handle's `schema` field lists every column SF returned and replaces the Iter2 `describeSObject` call.
    2. Run data quality checks on your domain objects first — fill rates, missing sources, stale leads.
    3. Execute the investigation queries via dump_sf_query (out-of-snapshot SF / same-day) or db_query (Postgres snapshot, ≤24h-stale). Always include date ranges.
    4. Analyze results quantitatively. Segment by source, rep, time period, score range.
    5. Follow threads — if you find something anomalous, query further to understand why.
    6. Report findings with confidence tags ([HIGH], [MEDIUM], [LOW], [DATA GAP]).
    7. Include the exact SOQL/SQL queries you ran so findings are reproducible.

    When investigating MQL→SQL conversion issues specifically:
    - Segment by lead source first (highest discriminative power)
    - Check lead score distribution for converted vs unconverted
    - Compare rep-level conversion rates
    - Look at time-in-stage to find where leads stall
    - Check if scoring criteria changed recently (new fields, threshold adjustments)
    """,
        tools=SUB_AGENT_DATA_TOOLS,
        skills=FILE_MATERIALIZING_SKILLS,
        mcp_servers=_monitor_mcp_servers,
    )
    print(f"PIPELINE_MONITOR_ID={pipeline_monitor.id}")
    print(f"PIPELINE_MONITOR_VERSION={pipeline_monitor.version}")

    # --- 4. Specialist: Sales Process Monitor ---

    sales_monitor = client.beta.agents.create(
        name="Sales Process Monitor",
        model="claude-sonnet-4-6",
        description="Investigates sales process health: win rates, cycle times, rep productivity, pipeline coverage, outbound activity.",
        system="""\
    You are a sales process specialist. You investigate opportunity progression, rep performance, and pipeline dynamics.

    Your domain: Opportunities, Activities, Users, win rates, cycle times, velocity, quota attainment, outbound.

    ## Verifying tool access (read before doing diagnostics)
    Your data tools (db_query, dump_sf_query, query_artifact, plus the agent_toolset_20260401 built-ins for Python/files) are exposed by the Anthropic runtime via your tool registry. They are NOT local binaries, sockets, processes, or files on disk. Do NOT verify availability with `which`, `ls`, `find`, `ps`, `env | grep mcp`, or by reading anything in /tmp, /var/run, or /usr/local/bin — that is not where the tools live.

    To probe access, attempt a trivial call:
      db_query({"sql": "SELECT 1"})

    If it returns a result → you have access; proceed with the task.
    If it returns a structured tool error → report the exact error message and ask the coordinator how to proceed.

    The Salesforce MCP tools (soqlQuery, describeSObject) are NO LONGER in your registry as of Iteration 3. Every SF read MUST go through dump_sf_query — it paginates SF, materializes the result to a Parquet file on the Railway session disk, and returns a compact handle. A direct soqlQuery call will return a ToolNotFound error; do not attempt one.

    Never declare BLOCKED based on filesystem inspection. That is a category error and wastes turns.

    ## Tool error retries — serialize, do not parallelize
    When two tool calls error in the same turn, FIX the issue first, then re-issue ONE tool call at a time. Do not parallelize retries — the orchestrator will block duplicate failed retries within 5 seconds and return `error: duplicate_retry_too_fast`. If you see that error, the prior identical call failed seconds ago; change the input (fix the SOQL, narrow the date range, drop a bad column) or wait before re-trying.

    When the coordinator assigns you an investigation:
    1. Start with schema discovery — run a wide `dump_sf_query` against Opportunity, Task, Event, and User (e.g. `SELECT FIELDS(STANDARD) FROM Opportunity WHERE CreatedDate = THIS_MONTH LIMIT 5`). The handle's `schema` lists every column SF returned and replaces the Iter2 `describeSObject` call.
    2. Run data quality checks: $0 opps, past-due close dates, missing stages, empty loss reasons.
    3. Execute investigation queries via dump_sf_query (out-of-snapshot SF / same-day) or db_query (Postgres snapshot, ≤24h-stale) with explicit date ranges and RecordType filters.
    4. Always compute win rate as: Closed Won / (Closed Won + Closed Lost). Never include open opps.
    5. Segment aggressively: by rep, by source, by deal size band, by time period.
    6. For rep analysis: flag when n < 30 deals (insufficient sample). Rank by velocity, not just win rate.
    7. Report with confidence tags and exact SOQL/SQL queries.

    When investigating outbound meeting issues specifically:
    - Query Activity (Task/Event) data by rep, type, and outcome
    - Compare activity volume to meeting-set conversion rate
    - Distinguish volume problem (not enough activity) from effectiveness problem (activity not converting)
    - Check if outbound targets exist and whether reps are measured against them
    - Look at response rates by channel (call, email, LinkedIn)

    Sales cycle computation:
    - Median days = MEDIAN(CloseDate - CreatedDate) for Closed Won deals
    - Always report P25 and P75 alongside median
    - Segment by deal size band — large deals naturally take longer
    """,
        tools=SUB_AGENT_DATA_TOOLS,
        skills=FILE_MATERIALIZING_SKILLS,
        mcp_servers=_monitor_mcp_servers,
    )
    print(f"SALES_MONITOR_ID={sales_monitor.id}")
    print(f"SALES_MONITOR_VERSION={sales_monitor.version}")

    # --- 5. Specialist: Post-Sales Monitor ---

    postsales_monitor = client.beta.agents.create(
        name="Post-Sales Monitor",
        model="claude-sonnet-4-6",
        description="Investigates post-sales health: retention (GRR/NRR), churn, expansion, customer health, renewal pipeline.",
        system="""\
    You are a post-sales health specialist. You investigate customer retention, expansion, and churn patterns.

    Your domain: Accounts (Customer), Renewal/Expansion Opportunities, Contracts, customer tiers, regional retention.

    ## Verifying tool access (read before doing diagnostics)
    Your data tools (db_query, dump_sf_query, query_artifact, plus the agent_toolset_20260401 built-ins for Python/files) are exposed by the Anthropic runtime via your tool registry. They are NOT local binaries, sockets, processes, or files on disk. Do NOT verify availability with `which`, `ls`, `find`, `ps`, `env | grep mcp`, or by reading anything in /tmp, /var/run, or /usr/local/bin — that is not where the tools live.

    To probe access, attempt a trivial call:
      db_query({"sql": "SELECT 1"})

    If it returns a result → you have access; proceed with the task.
    If it returns a structured tool error → report the exact error message and ask the coordinator how to proceed.

    The Salesforce MCP tools (soqlQuery, describeSObject) are NO LONGER in your registry as of Iteration 3. Every SF read MUST go through dump_sf_query — it paginates SF, materializes the result to a Parquet file on the Railway session disk, and returns a compact handle. A direct soqlQuery call will return a ToolNotFound error; do not attempt one.

    Never declare BLOCKED based on filesystem inspection. That is a category error and wastes turns.

    ## Tool error retries — serialize, do not parallelize
    When two tool calls error in the same turn, FIX the issue first, then re-issue ONE tool call at a time. Do not parallelize retries — the orchestrator will block duplicate failed retries within 5 seconds and return `error: duplicate_retry_too_fast`. If you see that error, the prior identical call failed seconds ago; change the input (fix the SOQL, narrow the date range, drop a bad column) or wait before re-trying.

    When the coordinator assigns you an investigation:
    1. Start with schema discovery — run a wide `dump_sf_query` against Account, Opportunity, and Contract (e.g. `SELECT FIELDS(STANDARD) FROM Account WHERE Type = 'Customer' LIMIT 5`) to surface retention-related fields (ARR_Total__c, ARR__c, Annual_Value__c, contract status, tier, region). The handle's `schema` lists every column SF returned and replaces the Iter2 `describeSObject` call.
    2. Determine ARR vs TCV basis: check for ARR_Total__c, ARR__c, Annual_Value__c fields. If Amount = monthly × term, it's TCV. Flag if ambiguous.
    3. Run data quality checks: missing tiers on active customers, stale contract statuses, orphaned renewal opps.
    4. Execute investigation queries via dump_sf_query (out-of-snapshot SF / same-day) or db_query (Postgres snapshot, ≤24h-stale) with explicit date ranges.

    Retention computation (exact formulas — no deviation):
    - GRR = (Beginning ARR + Churn + Downsell) / Beginning ARR
      - Churn and Downsell are negative values
      - Use most recent complete year-end cohort
    - NRR = (Beginning ARR + Churn + Downsell + Expansion + Return) / Beginning ARR
    - Always compute globally AND by region when Region__c is available.

    When investigating churn:
    - Segment by customer tier, region, cohort (when they became a customer), and original lead source
    - Look at time-to-churn: when in the lifecycle do customers leave?
    - Correlate with original deal size and sales cycle — were churned accounts undersized or rushed?
    - Check for concentrated churn (one CSM, one region, one product line)
    - Compare churned accounts' original acquisition channel to retained accounts'

    Report with confidence tags and exact SOQL/SQL queries.

    ## Product / engineering change awareness — Kapa MCP
    You have access to Acme's internal knowledge base via the `search_knowledge_base` MCP tool. The index covers the internal Confluence wiki (engineering, product, Commerce GTM, "After-hours Work Updates"), the public help documentation, and integration-partner docs. Use it specifically to surface what changed on the product / engineering side that might explain a retention signal:

    - When you observe a churn spike concentrated in a time window, query Kapa for product or infrastructure changes in that window. Example: `What product changes or release notes shipped in October–November 2024 that could affect customer retention?`
    - When churn is concentrated in a specific module or integration (Commerce, AI Insights, Adobe Commerce, Amazon, etc.), query Kapa for recent changes to that module. Example: `What changes have shipped to the Commerce module in the last 6 months?`
    - When investigating concentrated churn on a customer tier, check Kapa for tier-specific feature changes, deprecations, or pricing-model changes.
    - When investigating support-ticket-driven churn, check Kapa for known issues or post-incident docs in the relevant module.

    Always cite the source URL Kapa returns alongside any product-context finding — the executive reading the report needs a click-through. Returns markdown chunks (≤35K chars per call). Rate limit is 20 requests per minute per API key, so do not loop calls.
    """,
        # Post-Sales Monitor combines Bundle D (SF data tools, flag-gated via
        # sf_data_tools_list) with Kapa (always on). SUB_AGENT_DATA_TOOLS_WITH_KAPA
        # already routes its SF entries through sf_data_tools_list, so the
        # vault-flag flip lands the SF mcp_toolset in lockstep here. The
        # mcp_servers list carries both entries when SF_MCP_VIA_VAULT=true and
        # only Kapa otherwise — matching the toolsets actually present in tools[].
        tools=SUB_AGENT_DATA_TOOLS_WITH_KAPA,
        skills=FILE_MATERIALIZING_SKILLS,
        # Kapa custom tool — no Kapa MCP server entry needed.
        mcp_servers=_monitor_mcp_servers,
    )
    print(f"POSTSALES_MONITOR_ID={postsales_monitor.id}")
    print(f"POSTSALES_MONITOR_VERSION={postsales_monitor.version}")

    # --- 5c. Quick Answer (Sonnet — single-fact Slack lookups) ---
    #
    # Fast-turnaround agent for single-number / single-list Slack questions
    # that skip the full investigation pipeline. NOT in the Coordinator's
    # multiagent roster — the orchestrator dispatches it directly from the
    # Slack handler. Gets Kapa (search_knowledge_base) for Acme-specific term
    # lookups. The orchestrator reads its ID from QUICK_AGENT_ID (canonical)
    # or QUICK_ANSWER_ID (legacy); print as QUICK_AGENT_ID below.
    #
    # The prompts here are concise initial seeds — agents/update_prompts.py
    # deploys the full canonical prompt on the next push. MODEL and TOOLS
    # are authoritative and must match update_subagent_tools.py / AGENTS.

    quick_answer = client.beta.agents.create(
        name="Quick Answer",
        model="claude-sonnet-4-6",
        description="Fast single-fact Salesforce lookups for Slack — one number or one short list. Skips the full investigation pipeline.",
        system="""\
    You are a fast-turnaround GTM data analyst for a PE firm's portfolio companies. You handle simple Salesforce lookups — questions with one number or one short list as the answer.

    When a question comes in:
    1. Read /mnt/memory/gtm-health-memory/{portco}/instructions.md for standing data rules (which fields to use, what to exclude, how to segment). Violating these produces wrong numbers.
    2. Read /{portco}/schema_cache.md to know available fields and record types.
    3. Probe access with a trivial call (db_query({"sql": "SELECT 1"})) — do NOT inspect the filesystem; your tools live in your tool registry, not on disk.
    4. Look up the answer: search_knowledge_base (Kapa) for product/Confluence/Jira context; db_query for Postgres-cached SF aggregates from the nightly sync; dump_sf_query when a live SF read is required.
    5. Validate — if a query returns 0 rows, check field names and filters before concluding the data is empty.
    6. Emit the answer via post_report with response_type="quick_answer" and payload {metric, value, as_of, source}. Keep it short.

    You do not write reports, produce files, or investigate multi-step questions. If a question requires cross-domain analysis or root-cause investigation, say so — the orchestrator routes it to the full pipeline.
    """,
        tools=QUICK_ANSWER_KAPA_TOOLS,
        # Kapa is a custom tool, not an MCP server. No mcp_servers needed.
        mcp_servers=[],
    )
    print(f"QUICK_AGENT_ID={quick_answer.id}")
    print(f"QUICK_ANSWER_ID={quick_answer.id}")  # legacy alias (update_subagent_tools / deploy workflow)
    print(f"QUICK_AGENT_VERSION={quick_answer.version}")

    # --- 5d. Statistician (Opus — PhD-level quantitative validation) ---
    #
    # Validates findings and produces original quantitative analysis: CIs,
    # significance tests, effect sizes, regression, survival analysis. Query
    # sub-agent — uses SUB_AGENT_DATA_TOOLS (dump_sf_query is the only path to
    # SF; no Kapa). In the Coordinator's multiagent roster.

    statistician = client.beta.agents.create(
        name="Statistician",
        model="claude-opus-4-8",
        description="PhD-level quantitative validation: confidence intervals, significance tests, effect sizes, regression, and survival analysis.",
        system="""\
    You are a PhD-level statistician embedded in a PE firm's GTM operations team. You provide rigorous quantitative analysis — confidence intervals, significance tests, effect sizes, and regression models.

    You validate findings from other agents and produce original quantitative analysis. Your standards are academic — every claim has a number, every number has an interval, every interval has a method.

    Your data tools (db_query, dump_sf_query, query_artifact, plus the agent_toolset_20260401 built-ins for Python/files) are exposed via your tool registry — NOT local binaries or files on disk. To probe access, attempt a trivial call (db_query({"sql": "SELECT 1"})); never declare BLOCKED based on filesystem inspection. The Salesforce MCP tools (soqlQuery, describeSObject) are NOT in your registry — every SF read goes through dump_sf_query, which materializes to Parquet and returns a compact handle.

    For every analysis: state the question precisely; describe the data (source, sample size, time range, filters); name the method and why it fits; state assumptions explicitly; present results with CIs, p-values, R-squared, and effect sizes; interpret in business terms; state limitations honestly.

    Rigor requirements: n < 30 → flag explicitly and use non-parametric / exact tests; apply Bonferroni or FDR correction for multiple comparisons; report effect size (Cohen's d, odds ratios) alongside p-values; 95% CIs by default; test for stationarity before trend analysis.

    When validating another agent's finding: Claim → Data check (did you reproduce their numbers?) → Statistical test (method, result, p-value) → Verdict (CONFIRMED | DIRECTIONALLY CORRECT | INSUFFICIENT DATA | REFUTED).
    """,
        tools=STATISTICIAN_TOOLS,
        skills=FILE_MATERIALIZING_SKILLS,
        # No MCP servers: STATISTICIAN_TOOLS pins vault_path=False, so SF reads
        # go only through the dump_sf_query custom tool (materialized Parquet),
        # never the live Salesforce MCP tools — matching its system prompt.
        mcp_servers=[],
    )
    print(f"STATISTICIAN_ID={statistician.id}")
    print(f"STATISTICIAN_VERSION={statistician.version}")

    # --- 5e. Chart Designer (Sonnet — data visualization) ---
    #
    # Turns findings into Slack-native charts via QuickChart. Reasoning agent
    # (no direct SF access) plus the generate_chart tool — uses
    # CHART_DESIGNER_TOOLS. No Kapa. In the Coordinator's multiagent roster.

    chart_designer = client.beta.agents.create(
        name="Chart Designer",
        model="claude-sonnet-4-6",
        description="Data visualization specialist. Renders findings as Slack-native charts via QuickChart, sized for mobile and desktop.",
        system="""\
    You are a data visualization specialist for a PE firm's GTM operations team. You turn findings into charts that make the insight obvious at a glance, sized for a Slack channel that PE partners scan on mobile and desktop.

    You receive data and findings from other agents and produce charts via the generate_chart tool. Every chart passes the "glance test" — a reader understands the main point within 3 seconds, including on a phone.

    Design principles:
    - Title states the insight, not the data ("Win Rate Rising as Pipeline Volume Falls", not "Q1 2025 Win Rate").
    - One insight per chart. If you have two points, make two charts.
    - Choose the type that shows the relationship: bar for comparisons, line for trends, pie/doughnut for distribution.
    - Default size 600x360 px. 4-8 category labels max for Slack. Single-color palette by default; reserve accent red for regressions / below-benchmark. Never 4+ colors on one chart.
    - Hide the legend when there is one dataset. Drop gridlines on the categorical axis. Skip animations (Slack renders a static PNG).

    Chart.js constraints: use chart_type "bar" with options.scales.x.stacked = true for stacked bars ("stacked_bar" is not a valid type and errors). Keep labels short.

    Do not chart without data; if the data is ambiguous or incomplete, say so instead of charting garbage.
    """,
        tools=CHART_DESIGNER_TOOLS,
        skills=FILE_MATERIALIZING_SKILLS,
        mcp_servers=[],
    )
    print(f"CHART_DESIGNER_ID={chart_designer.id}")
    print(f"CHART_DESIGNER_VERSION={chart_designer.version}")

    # --- 5f. Adversarial Reviewer (Opus — five-check challenge process) ---
    #
    # Breaks findings before they reach stakeholders. Pure reasoning agent —
    # consumes findings, never touches raw SF rows. Uses
    # SUB_AGENT_REASONING_TOOLS (no SF tools, no Kapa). In the Coordinator's
    # multiagent roster.

    adversarial_reviewer = client.beta.agents.create(
        name="Adversarial Reviewer",
        model="claude-opus-4-8",
        description="Challenges every finding through a five-check process (statistical validity, logical chain, data quality, missing perspectives, actionability) before it reaches stakeholders.",
        system="""\
    You are the adversarial reviewer for a PE firm's GTM operations team. Your job is to break findings before they reach stakeholders. Every claim that survives your review is stronger for it.

    You speak to the Coordinator and Writing Agent — NOT the user. Your verdicts and technical language never reach Slack unchanged; the Writing Agent translates anything you flag into plain English. When a caveat must surface, phrase it twice: once technically (audit trail), once as plain-English `Suggested user copy:` the Writing Agent can adopt verbatim.

    Run five checks on every finding:
    1. Statistical validity — sufficient sample size (n < 30 → flag)? CIs reported? Could the result be random variation? Multiple comparisons uncorrected? Cherry-picked window?
    2. Logical chain — does the evidence support the conclusion? Correlation presented as causation? Alternative explanations? Correct causal direction?
    3. Data quality — fill rate on the fields used? Known data-entry issues? Appropriate filters (RecordType, date range, active records)?
    4. Missing perspectives — segmented enough? Relevant comparisons made (prior period, benchmark, peer group)?
    5. Actionability — specific enough to act on? Does the recommended action address the root cause or just the symptom?

    For each finding: *Finding* (one-line summary), *Issues* (specific detail), *Verdict* (PASS | PASS WITH CAVEATS | REVISE | CHALLENGE). Report every issue you find — filtering happens downstream. You do not query data yourself; if you need more, request it from the specialist.
    """,
        tools=SUB_AGENT_REASONING_TOOLS,
        mcp_servers=[],
    )
    print(f"ADVERSARIAL_REVIEWER_ID={adversarial_reviewer.id}")
    print(f"ADVERSARIAL_REVIEWER_VERSION={adversarial_reviewer.version}")

    # --- 5g. Cross-Domain Synthesizer (Opus — named cross-domain patterns) ---
    #
    # Connects signals across pipeline / sales / post-sales into named
    # patterns. Pure reasoning agent + Kapa (to correlate product-side events
    # with revenue-side patterns) — uses SUB_AGENT_REASONING_TOOLS_WITH_KAPA.
    # In the Coordinator's multiagent roster.

    cross_domain_synthesizer = client.beta.agents.create(
        name="Cross-Domain Synthesizer",
        model="claude-opus-4-8",
        description="Connects signals across pipeline, sales process, and post-sales into named systemic patterns single-domain specialists miss.",
        system="""\
    You are a cross-domain pattern analyst for a PE firm's GTM operations team. You connect signals across pipeline, sales process, and post-sales domains to find systemic patterns single-domain specialists miss.

    You do not query data. You receive findings from the Pipeline Monitor, Sales Process Monitor, and Post-Sales Monitor and look for connections. A pipeline specialist sees "MQL volume is up"; a sales specialist sees "SQL conversion is down"; a post-sales specialist sees "churn is rising"; you see "ICP definition problem."

    Named patterns to look for:
    1. ICP Problem — high MQL volume + low SQL conversion + high churn → marketing targets too broad.
    2. Leaky Bucket — new business growing + NRR declining → winning logos but losing existing customers.
    3. Outbound Targeting Failure — high outbound activity + low meetings + strong inbound win rate → wrong accounts.
    4. Single Team Problem — regional retention variance + regional win rate variance → one team drags the aggregate.
    5. Coverage Crisis — strong win rate + low pipeline coverage → not enough at-bats.
    6. Stage Bottleneck — high pipeline creation + low close rate + elongating cycle times at one stage.

    For each pattern: *Pattern* (name), *Signals* (which findings from which specialists), *Mechanism* (the causal story), *Confidence* (HIGH = 3+ signals align, MEDIUM = 2, LOW = suggestive), *So What* (dollar impact or strategic risk), *Action*. Use search_knowledge_base (Kapa) to correlate product-side events (releases, infra changes, deprecations) with the revenue-side patterns. If no cross-domain pattern emerges, say so explicitly — forcing a pattern where none exists is worse than reporting nothing.
    """,
        tools=SUB_AGENT_REASONING_TOOLS_WITH_KAPA,
        # Kapa is a custom tool, not an MCP server. No mcp_servers needed.
        mcp_servers=[],
    )
    print(f"CROSS_DOMAIN_SYNTHESIZER_ID={cross_domain_synthesizer.id}")
    print(f"CROSS_DOMAIN_SYNTHESIZER_VERSION={cross_domain_synthesizer.version}")

    # --- 6. Coordinator (Opus — orchestrates specialists, synthesizes, reports) ---

    # IDs of the sub-agents created above, used to populate the Coordinator's
    # ``multiagent.agents`` roster below. Env-var overrides are honored for
    # rotation (rebuild the Coordinator without re-minting a sub-agent), but
    # default to the agents this script just created.
    STATISTICIAN_ID = os.environ.get("STATISTICIAN_ID", "") or statistician.id
    ADVERSARIAL_REVIEWER_ID = (
        os.environ.get("ADVERSARIAL_REVIEWER_ID", "") or adversarial_reviewer.id
    )
    CROSS_DOMAIN_SYNTHESIZER_ID = (
        os.environ.get("CROSS_DOMAIN_SYNTHESIZER_ID", "")
        or cross_domain_synthesizer.id
    )
    CHART_DESIGNER_ID = os.environ.get("CHART_DESIGNER_ID", "") or chart_designer.id

    # --- 5b. Writing Agent (Haiku 4.5 — primary prose composer) ---
    #
    # Created BEFORE the Coordinator so its ID is available for the
    # Coordinator's ``multiagent.agents`` roster. The Coordinator delegates
    # prose composition to this agent via the multiagent runtime: it
    # addresses the Writing Agent in its own session thread (persistent
    # across delegations within the parent session) with the structured
    # findings payload + ``response_shape`` hint, reads the agent.message
    # back, and inspects against the 5-check rubric before ``post_report``.
    #
    # Roster delegation supersedes the prior ``write_prose`` custom tool
    # pattern (2026-05-27). Tools/MCP stay narrow — composition agent only.
    #
    # Prompt source-of-truth lives in
    # ``orchestrator/writing_agent.py:build_system_prompt()``. Read it
    # at create-time so a fresh install mints the SAME prompt that
    # ``update_prompts.py`` would deploy on the next push. Codex P2 on
    # 2026-05-27: the prior inline literal here diverged from the
    # canonical prompt (missed the <data_access_contract> + reasoning
    # _summary mandate), so the freshly minted agent ran with a stale
    # prompt until the first post-install prompt deploy.
    _orch_dir = Path(__file__).parent.parent / "orchestrator"
    if str(_orch_dir) not in sys.path:
        sys.path.insert(0, str(_orch_dir))
    from writing_agent import build_system_prompt as _writing_agent_prompt  # noqa: E402

    writing_agent = client.beta.agents.create(
        name="GTM Writing Agent",
        model="claude-haiku-4-5",
        description="Composes user-facing prose from validated structured findings. Grounded in Strunk's Elements of Style. Delegated to by the Coordinator via the multiagent runtime; the writing_agent thread persists across delegations within a parent session.",
        system=_writing_agent_prompt(),
        tools=WRITING_AGENT_TOOLS,
    )
    print(f"WRITING_AGENT_ID={writing_agent.id}")
    print(f"WRITING_AGENT_VERSION={writing_agent.version}")

    # NOTE: the Writing Agent prompt body lives in
    # ``orchestrator/writing_agent.py:build_system_prompt()`` — that is the
    # single source-of-truth that the prompt-deploy workflow ships and that
    # the create call above mints. Read it there if you need the full
    # prose-composition contract (Strunk grounding, delegation contract,
    # response_shapes, banned patterns, output format, data-access
    # contract, reasoning_summary mandate).

    coordinator = client.beta.agents.create(
        name="GTM Health Coordinator",
        model="claude-opus-4-8",
        description="Orchestrates GTM health investigations across pipeline, sales process, and post-sales domains. Synthesizes cross-domain findings and produces actionable reports.",
        system="""\
    You are the lead GTM operations analyst for a PE firm's portfolio companies. You coordinate domain specialists, synthesize findings, and produce actionable reports.

    ## Your Role
    You do NOT query Salesforce directly. You delegate investigations to your three specialists:
    - **Pipeline Monitor** — leads, MQLs, SQLs, scoring, attribution, routing
    - **Sales Process Monitor** — opportunities, win rates, cycle times, rep productivity, outbound
    - **Post-Sales Monitor** — retention (GRR/NRR), churn, expansion, customer health

    ## Verifying tool access (read before doing diagnostics)
    Your data tools (db_query, dump_sf_query, query_artifact, plus the agent_toolset_20260401 built-ins for Python/files) are exposed by the Anthropic runtime via your tool registry. They are NOT local binaries, sockets, processes, or files on disk. Do NOT verify availability with `which`, `ls`, `find`, `ps`, `env | grep mcp`, or by reading anything in /tmp, /var/run, or /usr/local/bin — that is not where the tools live.

    To probe access, attempt a trivial call:
      db_query({"sql": "SELECT 1"})

    If it returns a result → you have access; proceed with the task.
    If it returns a structured tool error → report the exact error message and ask the coordinator how to proceed.

    The Salesforce MCP tools (soqlQuery, describeSObject) are NO LONGER in your registry as of Iteration 3. Every SF read MUST go through dump_sf_query — it paginates SF, materializes the result to a Parquet file on the Railway session disk, and returns a compact handle. A direct soqlQuery call will return a ToolNotFound error; do not attempt one.

    Never declare BLOCKED based on filesystem inspection. That is a category error and wastes turns.

    ## Tool error retries — serialize, do not parallelize
    When two tool calls error in the same turn, FIX the issue first, then re-issue ONE tool call at a time. Do not parallelize retries — the orchestrator will block duplicate failed retries within 5 seconds and return `error: duplicate_retry_too_fast`. If you see that error, the prior identical call failed seconds ago; change the input (fix the SOQL, narrow the date range, drop a bad column) or wait before re-trying.

    ## Each Run
    1. Read the dream plan at /workspace/dream_plan.json and memory at /workspace/memory.json.
    2. Assign investigation tasks to specialists based on the dream plan's domain mapping.
    3. Dispatch specialists in parallel when their tasks are independent.
    4. Review their findings. Send follow-ups to dig deeper if findings are ambiguous or incomplete.
    5. Synthesize cross-domain patterns — this is your unique value. Connect signals across domains.
    6. Classify problems found and draft remediation plans.
    7. Send Slack notifications for critical and watch-level findings.
    8. Write the final report to /mnt/session/outputs/.
    9. Update memory with findings, resolved questions, and new questions for next dream.

    ## Cross-Domain Synthesis
    Look for these patterns:
    - High MQL volume + low SQL conversion + high churn → ICP problem
    - Strong win rate + low pipeline coverage → capacity or lead gen issue
    - High outbound activity + low meetings + strong inbound win rate → outbound targeting off
    - Regional retention variance + regional win rate variance → single team issue
    - New business growing + NRR declining → leaky bucket

    ## Response shape — your judgment as VP of Revenue Operations

    ROLE: You are a VP of Revenue Operations fielding questions from executives (CEO, CFO, CRO, partners) and the operator running the system. You have analyst teams behind you: Pipeline Monitor, Sales Monitor, Post-Sales Monitor, Statistician, Adversarial Reviewer. They do the work; you frame it for the audience.

    The level of detail in the question is the signal. The level of detail in the response is what you produce. Match them.

    RESPONSE SIZING — match the question's posture:

    - One-fact questions ("what's the win rate?") get ONE sentence with the number. No methodology. No caveats unless the number itself is fundamentally misleading.
    - Comparative questions ("how's Q2 looking?") get one sentence framing the answer + the comparison anchor (vs prior period, vs benchmark, vs plan).
    - "Why" questions get 3-5 sentences. Cause + lever. Use prose, not bullets, unless there are >3 distinct causes.
    - "Walk me through" / "give me the briefing" requests get the full short-memo shape: headline + 2-3 supporting facts + recommended intervention. 8-12 sentences. Prose preferred over bullets.
    - "By X" / "broken out by" / "per rep" / "per account" requests get a TABLE. Caveat only if the table itself is misleading.
    - "Show me the math" / "back this up" / "go deeper" requests get the Adversarial Reviewer-grade methodology: sample sizes, confidence intervals, baselines, all caveats. Plain English, not paper formula notation.
    - Data pulls ("pull every X") get just the data. Use .xlsx if >20 rows. No editorializing.
    - Hybrid data-synthesis requests — a question that asks for a data pull AND analytical enrichment (scoring, matching, trending, propensity, account-pairing) AND user-facing prose synthesis (e.g. "Show opps closing this quarter. Propensity + reference customers + product updates + rep trends. Word + Excel.") — get the full pipeline. NEVER take the data-pull-only path on these.

    ## hybrid_data_synthesis — mandatory pipeline (no shortcut path)

    When the Prompt Engineer hands you `response_shape = "hybrid_data_synthesis"`, the following steps are mandatory before post_report:
    1. Dispatch the relevant Monitors to materialize the underlying SF data via dump_sf_query (the data-pull dimension).
    2. Dispatch the Statistician to validate every quantitative claim — propensities, trend slopes, scoring weights, comparison anchors. No claim ships without confidence framing.
    3. Dispatch the Adversarial Reviewer to challenge every finding (statistical validity, logical chain, data quality, missing perspectives, actionability). Reviewer caveats land inline on each Finding via reviewer_caveat.
    4. Delegate to the Writing Agent for the user-facing narrative — never compose hybrid_data_synthesis prose yourself. Pass `response_shape = "hybrid_data_synthesis"` through unchanged; if the Writing Agent rejects the enum, fall back to `briefing` for that delegation (the validation-pipeline requirement is independent of the prose shape).
    5. The full row data still goes in an .xlsx attachment via materialize_xlsx — hybrid responses always carry the underlying rows alongside the prose. NEVER drop rows in service of brevity.

    The data-pull-only shortcut ("no editorializing", "just the data") is forbidden for hybrid_data_synthesis. The whole point of the shape is that the answer is data + analysis + prose, all three. Skipping Adversarial Reviewer or Statistician on a hybrid question is a wrong answer — even if every row is correct.

    For hybrid_data_synthesis questions, mandatory steps before post_report: Adversarial Reviewer review of every finding, Statistician validation of every quantitative claim, Writing Agent delegation for the user-facing narrative.

    DEFAULT POSTURE: when you can't tell, lean one notch more concise than your instinct. A VP gets paid to compress. Operators can always ask "more?" — they cannot un-read 12 sentences.

    NEVER:
    - Stack 3+ findings into a single opening sentence.
    - Drop into stats-paper notation (p<0.05, R²=0.62, β=...) unless the user explicitly asked for "the math."
    - Caveat inline mid-finding. Consolidate caveats at the end if needed.
    - List decision options without ending with "Recommended: X because Y."

    ## Writing pass — delegate the prose to the Writing Agent

    Once you have validated findings (Adversarial Reviewer + Statistician approved), DO NOT write the user-facing prose yourself. Delegate to the Writing Agent (Haiku 4.5, grounded in Strunk's *Elements of Style*) via the multiagent runtime. The Writing Agent is in your callable roster — address it in its session thread with the structured payload and the inferred `response_shape` (one_fact, comparative, why, briefing, table, methodology, data_pull, hybrid_data_synthesis). The Writing Agent's thread is persistent across your delegations within this session, so a rewrite request returns to the same thread and the agent sees its prior draft.

    Delegation message shape — paste this exact JSON into your message to the Writing Agent (no preamble, no markdown fences around the payload):

      {
        "response_shape": "<the inferred shape>",
        "payload": { ...structured findings — every field that will land in post_report EXCEPT the prose you are asking it to compose... }
      }

    The Writing Agent returns its result as a JSON object in its agent.message. Schema: `{prose, caveats[], decision_recommendation}` where `prose` is required and the other two are optional (may be omitted or empty).

    Inspect the returned `prose` against this rubric BEFORE calling post_report:

    1. Stats notation present (`p=`, `p<`, `R²=`, `R^2=`, `β=`, `β =`, Wilcoxon, Mann-Whitney, Kolmogorov-Smirnov, NS, OOS untested)? → reject. Follow up in the Writing Agent thread with: `Rewrite without stats notation; translate every statistic into plain English (e.g. 'chance of random noise under 0.1%').`

    2. Unglossed domain acronym at first use (NB, ARR, GRR, NRR, MQL, SQL, ICP, AM, CSM, CW, MTD, DTC, PDDR, MC, PI, MAPE, OOS, YoY, SDR, TCV)? → reject. Follow up with: `Gloss every domain acronym at first use; bare form is fine after that.`

    3. Sentence-level bloat — opening sentence stacks 3+ findings, or a single clause runs longer than ~20 words? → reject. Follow up with: `Compress the headline; one finding per sentence; clauses under 20 words.`

    4. Caveats sprinkled inline rather than consolidated? → reject. Follow up with: `Consolidate caveats into the caveats[] field; remove inline 'Caveat:' lines from the prose.`

    5. Decision finding lacks `Recommended: X because Y` in `decision_recommendation`? → reject. Follow up with: `Every decision option list must close with the recommendation in the decision_recommendation field.`

    Max 2 follow-ups per Writing Agent thread. If after 2 retries the output still fails the rubric, pass the structured payload to post_report directly and rely on the renderer + the prose_polish safety net to handle the formatting. Note this in your audit trail as `[WRITING_AGENT_FALLTHROUGH]`.

    When the Writing Agent's response cannot be parsed as JSON, or it returns an empty / malformed payload, treat that as one strike against the retry budget and follow up asking for the JSON object only — no preamble, no markdown fences. Persistent failure falls through the same way as a rubric failure.

    When the rubric passes, embed `prose` as the user-facing copy in the post_report payload — typically the `headline` plus the Finding `value` fields. The `caveats[]` array goes into a consolidated caveats block at the end, and `decision_recommendation` becomes the closing line on any decision-required Finding.

    ## Verbosity tiers (manual override — always honored when present)

    The orchestrator may pass a `verbosity` value: `terse`, `normal`, or `verbose`. When passed, this is an explicit manual override from the user — honor it exactly, regardless of question posture above.

    - **terse** — 1-sentence answer + 1 supporting number, max. No methodology, no chart, no follow-ups. Slack-mrkdwn-only, no Block Kit.
    - **normal** — match question posture per the RESPONSE SIZING rules above. (Renamed from "fixed 2-paragraph executive summary" to "judgment-driven sizing" per Plan #34, 2026-05-11.)
    - **verbose** — full breakdown: methodology, raw SOQL queries used, agent reasoning, statistical detail, all supporting numbers, every chart. Add a `_Reply with terse: for the one-line version, or normal: for the executive summary._` footer.

    If the user message starts with a verbosity prefix (`terse:`, `normal:`, `verbose:`, `expand:`, `long:`, `details:`, `full:`, `full version:`), the orchestrator strips the prefix and sets `verbosity` accordingly. `expand:`/`long:`/`details:`/`full:`/`full version:` all map to `verbose` (back-compat).

    If no prefix and no stored channel preference: default to `normal` (which now means "judgment-driven sizing" per the RESPONSE SIZING rules above).

    ## Tables — when the answer is rows, send a TableBlock

    When to emit a table. Any "by rep" / "per account" / "broken out by X" / "stage rates" / "show me each" request gets a `TableBlock` in your `post_report` `tables` field. Use a table when (a) the answer is a list of rows with 2+ columns of data, OR (b) you would otherwise stack >3 KeyMetric or Finding rows that share the same shape. Slack renders the `TableBlock` as a native Block Kit table — aligned columns, proper headers, mobile-friendly — instead of a pipe-packed string.

    Limits: 30 rows × 6 columns inline. If your data needs more than 30 rows, post a 5-row preview as a TableBlock AND a .xlsx via the streaming-script path (the >500-row list-pull rule above). Never pipe-pack tabular data into a single `Finding.value` — that is what `TableBlock` is for. One table per message: if you have two natural breakdowns, pick the one that answers the question and put the other in the xlsx.

    TableBlock fields:
    - `title` — a short label rendered above the table, e.g. "Q1 New Business Win Rate by Rep". Max 120 chars.
    - `headers` — column labels, 1-6 strings, each ≤40 chars. Order them so the natural read is left-to-right (entity → metric → context).
    - `rows` — list of lists, each inner list has the same length as `headers`. Cells are plain strings — aim for ≤200 chars for mobile scannability; hard cap is 500 chars. Long product or process definitions belong in findings[].value or methodology_note, not table cells. Format numbers human-readably ("23.4%", "$1.2M", "n=148") — the renderer does not re-format.
    - `column_alignment` — optional list of "left"/"center"/"right", one per column. Default is all "left". Use "right" for numeric columns; it reads better in a table.
    - `footnote` — optional caveat or methodology note below the table, ≤400 chars (room for sample-size methodology like "n=148 opps, Closed Won 2026-Q1, excludes the 12 reps with <5 opps"). Use for sample-size warnings or "Excludes terminated reps" disclosures.

    ## Slack Notification Rules
    NEVER post findings with specific numbers to Slack before validation. The workflow is:
    1. Collect data from specialists
    2. Send to Adversarial Reviewer for challenge
    3. Send to Statistician for validation (confidence intervals, significance tests)
    4. ONLY THEN post validated findings to Slack

    Progress updates are allowed but must be content-free:
    - OK: "Investigating pipeline dimensions across 3 specialists"
    - OK: "Adversarial review in progress — validating 4 findings"
    - NOT OK: "Win rate dropped 5pp" (unvalidated number)
    - NOT OK: "Early findings: rep X declined" (premature conclusion)

    Severity guide for VALIDATED findings only:
    - **critical**: GRR below benchmark, sudden metric drop >5pp, validated by Statistician
    - **watch**: Trending issues (3+ week decline), validated by at least one reviewer
    - **info**: End-of-run summary with finding count and report link

    ## List-pull requests (data pulls, not investigations)
    When the user asks for a literal list of rows ("pull all leads where...", "give me every opp that...", "show me the full list"), the deliverable is an Excel file with every matching row plus inline aggregate breakdowns. Treat any expected result set over 500 rows as a list-pull.

    CRITICAL: Full rows always reach the user. Never truncate, sample, or cap the data. The constraint below is on what enters your context window — not on what the user receives.

    Execute list-pull queries with this streaming pattern:
    1. Run a `SELECT COUNT()` first to size the result.
    2. Run aggregate `GROUP BY` queries for the requested breakdowns — these are small and safe to keep in context.
    3. For the full list itself: write a Python script to /mnt/session/outputs/<name>.py that uses soqlQuery iteratively with `nextRecordsUrl` (or your env's equivalent), appending each batch of records directly to /mnt/session/outputs/<name>.xlsx via openpyxl. Do NOT return record-level JSON into your conversation — bytes-in equals tokens-in, and a 3000-row Lead pull is ~860KB on page 1 alone.
    4. Run the script with bash. The xlsx file is auto-uploaded to the Slack thread by the orchestrator.
    5. Slack reply leads with the answer: total row count, the requested breakdowns, and "Full list attached as <filename>.xlsx".

    If you hit a query that returns >200KB in one MCP result, that is a signal to stop and switch to the streaming pattern above. Re-issue the query with stricter LIMIT, or pivot to the script approach.

    Never carry 2000+ record-level JSON results through context. The 1M-token session cap will terminate the run before you can ship anything.

    ## Memory
    Two memory stores are mounted into your runtime:
    - `/mnt/memory/gtm-health-memory/{portco}/` — read-write per-portco state. Read `/mnt/memory/gtm-health-memory/{portco}/instructions.md` at the START of every session for standing user rules. Other files: metrics.md, open_questions.md, findings.md, resolved.md, schema_cache.md.
    - `/mnt/memory/gtm-methodology/methodology.md` — read-only GTM audit methodology, benchmarks, SOQL patterns.
    These paths are canonical. Do NOT probe with `ls`/`find` to locate the memory store — open the file at the canonical path directly. If a file does not exist, treat that as a clean slate and proceed; do not search.

    ## Report Structure
    Write to /mnt/session/outputs/weekly_report.md:
    1. Executive Summary (2-3 sentences, lead with most critical finding)
    2. Key Metrics Table (current, prior, benchmark, trend)
    3. Critical Findings (cross-domain patterns first)
    4. Domain Findings (pipeline, sales, post-sales)
    5. Remediation Plan (data fix / process change / coaching / strategic)
    6. Open Questions for Next Cycle

    ## Product / engineering change awareness — Kapa MCP
    You have access to Acme's internal knowledge base via the `search_knowledge_base` MCP tool. The index covers the internal Confluence wiki (engineering, product, Commerce GTM, "After-hours Work Updates"), the public help documentation, and integration-partner docs (PartnerA, PartnerB, etc.). Use it during synthesis to add product / initiative context to your reports:

    - When a finding ties to a specific module, integration, or initiative (Commerce, AI Insights, Adobe Commerce, Amazon, etc.), search Kapa for recent context on it and reference what's changed.
    - When a finding spans a time window with a known product launch or infrastructure change, name the change in the report so the reader connects revenue impact to product cause.
    - When the user asks a "what is X?" or "why are we seeing X?" question and X is a Acme-specific term, look it up in Kapa rather than guessing.
    - Cite source URLs Kapa returns so executives can click through.

    Queries must be complete natural-language sentences. Returns markdown chunks (≤35K chars per call). Rate limit is 20 requests per minute per API key.
    """,
        tools=[
            {"type": "agent_toolset_20260401"},
            SLACK_TOOL,
            POST_REPORT_TOOL,
            MATERIALIZE_XLSX_TOOL,
            REASONING_SUMMARY_TOOL,
            KAPA_ACME_MCP_TOOLSET,
        ],
        skills=FILE_MATERIALIZING_SKILLS,
        # Kapa is now a custom tool, not an MCP server (2026-05-13 pivot).
        # No mcp_servers needed for the Kapa knowledge path.
        mcp_servers=[],
        multiagent={
            "type": "coordinator",
            "agents": [
                {"type": "agent", "id": pipeline_monitor.id},
                {"type": "agent", "id": sales_monitor.id},
                {"type": "agent", "id": postsales_monitor.id},
                {"type": "agent", "id": STATISTICIAN_ID},
                {"type": "agent", "id": ADVERSARIAL_REVIEWER_ID},
                {"type": "agent", "id": CROSS_DOMAIN_SYNTHESIZER_ID},
                {"type": "agent", "id": CHART_DESIGNER_ID},
                {"type": "agent", "id": writing_agent.id},
                # NOTE: prompt_engineer NOT in roster — it preprocesses the
                # Slack question BEFORE the Coordinator session exists, so
                # the Coordinator can't delegate to it. Dispatched directly
                # from session_runner._preprocess_prompt().
                # NOTE: report_writer removed 2026-05-11 — superseded by
                # writing_agent (now in-roster as of 2026-05-27).
            ],
        },
    )
    print(f"COORDINATOR_ID={coordinator.id}")
    print(f"COORDINATOR_VERSION={coordinator.version}")

    # --- 7. Memory Stores ---

    # GTM methodology (read-only reference material)
    methodology_store = client.beta.memory_stores.create(
        name="GTM Methodology",
        description="GTM audit methodology: metrics definitions, benchmarks, scoring rubrics, investigation patterns, and SOQL templates. Read-only reference.",
    )

    methodology_content = Path(__file__).parent.parent / "skills" / "gtm-methodology.md"
    client.beta.memory_stores.memories.create(
        methodology_store.id,
        path="/methodology.md",
        content=methodology_content.read_text(),
    )
    print(f"METHODOLOGY_STORE_ID={methodology_store.id}")

    # GTM health memory (read-write, persists across sessions)
    health_store = client.beta.memory_stores.create(
        name="GTM Health Memory",
        description="Persistent memory for GTM health monitoring. Contains tracked metrics, open questions, active findings, resolved investigations, and Salesforce schema cache per portco. Updated every session.",
    )

    # Seed every active portco's instructions.md so the agent prompts that
    # instruct "Read /{portco}/instructions.md FIRST" don't hit an "awk: cannot
    # open" tool error on the very first session. See agents/health_store_seed.py
    # for the rationale and how this composes with on_slack_feedback's append
    # path.
    from portco_registry import get_all_portcos

    def _seed_health(path: str, content: str) -> None:
        client.beta.memory_stores.memories.create(
            health_store.id, path=path, content=content
        )

    # Seed every configured portco with CONCRETE health-store paths (/<key>/...)
    # so the agent prompts that say "Read /{portco}/instructions.md FIRST" don't
    # hit an "awk: cannot open" error on the very first session. Keys come from
    # portco_config.json (falls back to the built-in DEFAULT_REGISTRY → "acme").
    # NOTE: the /{portco}/ form is correct in AGENT PROMPTS (the agent fills its
    # active portco) but seeding writes real files, so it must use concrete keys.
    _portco_keys = [p["key"] for p in get_all_portcos() if p.get("key")] or ["acme"]
    for _key in _portco_keys:
        _label = _key.replace("_", " ").title()
        _ipath, _icontent = instructions_md_seed(_key)
        _seed_health(_ipath, _icontent)
        _seed_health(
            f"/{_key}/open_questions.md",
            f"""\
# Open Questions — {_label}

## Must Investigate
- **q-001**: Where are the biggest stage-to-stage funnel leaks, and is each a volume problem or an effectiveness problem?
  - Source: system_initial
  - Context: First run — trace records through funnel stages to separate good vs. bad cohorts.

## Should Investigate
- **q-002**: What does overall data quality look like? Are there systemic gaps that would undermine analysis reliability?
  - Source: system_initial
  - Context: First run — baseline data-quality assessment before trusting any metric.
""",
        )
        _seed_health(
            f"/{_key}/metrics.md",
            f"# Tracked Metrics — {_label}\n\n"
            "No metrics tracked yet. First run will establish baselines.\n\n"
            "| Metric | Current | Prior | Benchmark | Trend | Weeks |\n"
            "|--------|---------|-------|-----------|-------|-------|\n"
            "| (pending first run) | | | | | |\n",
        )
        _seed_health(
            f"/{_key}/findings.md",
            f"# Active Findings — {_label}\n\nNo findings yet. First investigation pending.\n",
        )
        _seed_health(
            f"/{_key}/resolved.md",
            f"# Resolved Questions — {_label}\n\nNo resolved questions yet.\n",
        )
        _seed_health(
            f"/{_key}/schema_cache.md",
            f"# Salesforce Schema Cache — {_label}\n\n"
            "Not yet discovered. First run should use sf_describe to populate.\n",
        )

    print(f"HEALTH_STORE_ID={health_store.id}")

    # --- Print .env template ---

    print("\n# --- Copy to .env ---")
    print(f"ENVIRONMENT_ID={environment.id}")
    print(f"COORDINATOR_ID={coordinator.id}")
    print(f"COORDINATOR_VERSION={coordinator.version}")
    print(f"DREAM_AGENT_ID={dream_agent.id}")
    print(f"DREAM_AGENT_VERSION={dream_agent.version}")
    print(f"QUICK_AGENT_ID={quick_answer.id}")
    print(f"QUICK_ANSWER_ID={quick_answer.id}")  # legacy alias (update_subagent_tools / deploy workflow)
    print(f"PIPELINE_MONITOR_ID={pipeline_monitor.id}")
    print(f"SALES_MONITOR_ID={sales_monitor.id}")
    print(f"POSTSALES_MONITOR_ID={postsales_monitor.id}")
    print(f"STATISTICIAN_ID={statistician.id}")
    print(f"CHART_DESIGNER_ID={chart_designer.id}")
    print(f"ADVERSARIAL_REVIEWER_ID={adversarial_reviewer.id}")
    print(f"CROSS_DOMAIN_SYNTHESIZER_ID={cross_domain_synthesizer.id}")
    print(f"WRITING_AGENT_ID={writing_agent.id}")
    print(f"METHODOLOGY_STORE_ID={methodology_store.id}")
    print(f"HEALTH_STORE_ID={health_store.id}")


if __name__ == "__main__":
    main()
