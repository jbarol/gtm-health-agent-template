"""Stress Test 1: Q1'25 vs Q1'26 dimensional analysis.

Sends a complex multi-domain query to the Coordinator, forcing specialist
dispatch, cross-domain synthesis, chart generation, and adversarial review.
"""

import json
import os
import sys
import time
from pathlib import Path

dotenv = Path(__file__).parent.parent / ".env"
if dotenv.exists():
    for line in dotenv.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

sys.path.insert(0, str(Path(__file__).parent))
import anthropic
from slack_bot import send_notification, post_analysis, post_chart_file

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

ENVIRONMENT_ID = os.environ["ENVIRONMENT_ID"]
COORDINATOR_ID = os.environ["COORDINATOR_ID"]
ACME_VAULT_ID = os.environ.get("ACME_VAULT_ID", "")
METHODOLOGY_STORE_ID = os.environ["METHODOLOGY_STORE_ID"]
HEALTH_STORE_ID = os.environ["HEALTH_STORE_ID"]

vault_ids = [v for v in [ACME_VAULT_ID] if v]

STRESS_PROMPT = """\
Compare Q1 2025 vs Q1 2026 across every dimension:
- Win rate by rep, by source, by product (RecordType), by deal size band ($0-10K, $10-50K, $50-100K, $100K+)
- Show which reps improved and which declined
- For the declining reps, investigate whether it's a pipeline quality problem, execution problem, or market problem
- Cross-reference worst-performing reps' closed-lost accounts against renewal book
- Run survival analysis on accounts by top-3 vs bottom-3 reps

Use the Statistician for statistical analysis (R-squared values, confidence intervals).
Use the Chart Designer for charts on every comparison.

The Adversarial Reviewer must challenge at least one finding.

Send all findings to Slack as you discover them. Write the full report to /mnt/session/outputs/stress_test_1_report.md.

SOQL RULES:
- CloseDate = DATE only (2024-01-01, no T/Z)
- CreatedDate = DATETIME (2024-01-01T00:00:00Z)
- No CASE, COALESCE, FLOOR, or subqueries in SELECT
- No column aliases in ORDER BY — use the aggregate function
- Use CALENDAR_YEAR()/CALENDAR_QUARTER() for time grouping
- RecordType.Name = 'New Business' for new business opps
- Filter CreatedDate >= 2024-01-01T00:00:00Z — data before 2024 is unreliable

SLACK FORMATTING:
- Use *bold* (single asterisks) for emphasis
- Use bullet points (- ) for lists
- No markdown tables with pipes — use inline format
- Numbers with commas, percentages with 1 decimal
"""

print("=" * 60)
print("STRESS TEST 1: Q1'25 vs Q1'26 Dimensional Analysis")
print("=" * 60)
print(f"Coordinator: {COORDINATOR_ID}")
print(f"Environment: {ENVIRONMENT_ID}")
print()

session = client.beta.sessions.create(
    agent=COORDINATOR_ID,
    environment_id=ENVIRONMENT_ID,
    title="Stress Test 1: Q1 YoY Dimensional Analysis",
    vault_ids=vault_ids,
    resources=[
        {
            "type": "memory_store",
            "memory_store_id": METHODOLOGY_STORE_ID,
            "access": "read_only",
            "instructions": "GTM methodology reference.",
        },
        {
            "type": "memory_store",
            "memory_store_id": HEALTH_STORE_ID,
            "access": "read_write",
            "instructions": "Persistent GTM health memory.",
        },
    ],
)
print(f"Session: {session.id}")
start_time = time.time()

seen_ids = set()
pending_tools = {}
agent_text_parts = []
custom_tool_calls = 0
mcp_tool_calls = 0
slack_posts = 0
errors = []
soql_queries = []


def dispatch_tool(name, inp):
    if name == "send_slack_notification":
        ts = send_notification(
            severity=inp["severity"],
            summary=inp["summary"],
            detail=inp.get("detail", ""),
            reply_to=inp.get("reply_to"),
        )
        return json.dumps({"ok": True, "message_ts": ts})
    elif name == "generate_chart":
        from session_runner import _render_chart_bytes

        chart_bytes = _render_chart_bytes(inp)
        ts = post_chart_file(
            title=inp["title"], chart_bytes=chart_bytes, reply_to=inp.get("reply_to")
        )
        return json.dumps({"ok": True, "message_ts": ts})
    elif name == "db_query":
        import db_adapter

        if not db_adapter.is_db_available():
            return json.dumps({"error": "Database not available"})
        result = db_adapter.query(inp["sql"])
        return json.dumps(result, default=str)
    return json.dumps({"error": f"Unknown tool: {name}"})


with client.beta.sessions.events.stream(session_id=session.id) as stream:
    client.beta.sessions.events.send(
        session_id=session.id,
        events=[
            {
                "type": "user.message",
                "content": [{"type": "text", "text": STRESS_PROMPT}],
            }
        ],
    )

    for event in stream:
        if hasattr(event, "id") and event.id:
            if event.id in seen_ids:
                continue
            seen_ids.add(event.id)

        if event.type == "agent.message":
            for block in event.content:
                if hasattr(block, "text") and block.text:
                    agent_text_parts.append(block.text)
                    preview = block.text[:120].replace("\n", " ")
                    elapsed = int(time.time() - start_time)
                    print(f"  [{elapsed}s] AGENT: {preview}")

        elif event.type == "agent.custom_tool_use":
            pending_tools[event.id] = event
            custom_tool_calls += 1
            if event.name == "send_slack_notification":
                severity = (
                    event.input.get("severity", "")
                    if isinstance(event.input, dict)
                    else ""
                )
                if severity in ("critical", "watch"):
                    slack_posts += 1
            elapsed = int(time.time() - start_time)
            print(f"  [{elapsed}s] CUSTOM_TOOL: {event.name}")

        elif event.type == "agent.mcp_tool_use":
            mcp_tool_calls += 1
            perm = getattr(event, "evaluated_permission", None)
            if perm == "ask":
                pending_tools[event.id] = event
            if event.name in ("soqlQuery", "describeSObject"):
                soql_queries.append({"tool": event.name, "input": event.input})
            elapsed = int(time.time() - start_time)
            print(f"  [{elapsed}s] MCP_TOOL: {event.name} (perm={perm})")

        elif event.type == "session.status_idle":
            stop_reason = getattr(event, "stop_reason", None)
            sr_type = getattr(stop_reason, "type", None) if stop_reason else None

            if sr_type == "requires_action":
                event_ids = getattr(stop_reason, "event_ids", [])
                results = []
                for eid in event_ids:
                    tool_event = pending_tools.get(eid)
                    if tool_event:
                        if tool_event.type == "agent.mcp_tool_use":
                            results.append(
                                {
                                    "type": "user.tool_confirmation",
                                    "tool_use_id": eid,
                                    "result": "allow",
                                }
                            )
                        else:
                            try:
                                result_text = dispatch_tool(
                                    tool_event.name, tool_event.input
                                )
                                results.append(
                                    {
                                        "type": "user.custom_tool_result",
                                        "custom_tool_use_id": eid,
                                        "content": [
                                            {"type": "text", "text": result_text}
                                        ],
                                    }
                                )
                            except Exception as e:
                                results.append(
                                    {
                                        "type": "user.custom_tool_result",
                                        "custom_tool_use_id": eid,
                                        "content": [
                                            {
                                                "type": "text",
                                                "text": json.dumps({"error": str(e)}),
                                            }
                                        ],
                                    }
                                )
                                errors.append(f"{tool_event.name}: {e}")
                        del pending_tools[eid]
                    else:
                        elapsed = int(time.time() - start_time)
                        print(f"  [{elapsed}s] SKIP: event {eid} (sub-agent internal)")

                if results:
                    try:
                        client.beta.sessions.events.send(
                            session_id=session.id,
                            events=results,
                        )
                    except anthropic.BadRequestError as e:
                        errors.append(f"Send failed: {e}")
                continue

            elif sr_type == "max_tokens":
                elapsed = int(time.time() - start_time)
                print(f"  [{elapsed}s] MAX_TOKENS — sending continuation")
                client.beta.sessions.events.send(
                    session_id=session.id,
                    events=[
                        {
                            "type": "user.message",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "Continue from where you stopped.",
                                }
                            ],
                        }
                    ],
                )
                continue

            else:
                elapsed = int(time.time() - start_time)
                print(f"  [{elapsed}s] SESSION_IDLE: {sr_type}")
                break

        elif event.type == "session.error":
            ed = event.error if hasattr(event, "error") else None
            msg = getattr(ed, "message", "unknown") if ed else "unknown"
            retry = getattr(ed, "retry_status", None)
            if retry and getattr(retry, "type", "") != "exhausted":
                elapsed = int(time.time() - start_time)
                print(f"  [{elapsed}s] RETRYING: {msg}")
                continue
            errors.append(msg)
            elapsed = int(time.time() - start_time)
            print(f"  [{elapsed}s] SESSION_ERROR: {msg}")
            break

        elif event.type == "session.status_rescheduled":
            elapsed = int(time.time() - start_time)
            print(f"  [{elapsed}s] RESCHEDULED (auto-retry)")

        elif event.type == "agent.thread_context_compacted":
            elapsed = int(time.time() - start_time)
            print(f"  [{elapsed}s] CONTEXT_COMPACTED")

        elif event.type == "session.status_terminated":
            elapsed = int(time.time() - start_time)
            print(f"  [{elapsed}s] TERMINATED")
            break

duration = int(time.time() - start_time)

# Post combined analysis if agent didn't post to Slack itself
if not slack_posts and agent_text_parts:
    orchestration_keywords = [
        "dispatched",
        "awaiting",
        "ending turn",
        "specialists",
        "unblocked",
        "blocked on",
    ]
    analysis = "\n\n".join(agent_text_parts)
    orchestration_lines = sum(
        1
        for line in analysis.split("\n")
        if any(kw in line.lower() for kw in orchestration_keywords)
    )
    total_lines = max(len(analysis.split("\n")), 1)

    if orchestration_lines / total_lines <= 0.5:
        queries_list = [
            q["input"].get("soql", q["input"].get("query", ""))
            for q in soql_queries
            if q.get("input")
        ]
        post_analysis(
            title="Stress Test 1: Q1'25 vs Q1'26 Dimensional Analysis",
            analysis_text=analysis,
            queries=queries_list,
        )
        print(f"\nPosted analysis to Slack ({len(analysis)} chars)")
    else:
        print("\nAgent output was mostly orchestration chatter — not posting")

# Archive and log usage
try:
    s = client.beta.sessions.retrieve(session.id)
    u = s.usage
    print(
        f"\nUsage: output={u.output_tokens:,} input={u.input_tokens:,} cache_read={u.cache_read_input_tokens:,}"
    )
except Exception:
    pass

try:
    client.beta.sessions.archive(session.id)
except Exception:
    pass

# Save output — Plan #44 Task #5 / decision row #9: local orchestrator
# container disk path. Stress tests write their report to the same OUTPUTS_DIR
# that session_runner uses as the Files-API download cache so an operator can
# eyeball both side-by-side. Not the Anthropic session disk.
output_dir = Path("/tmp/gtm-health-agent/outputs")
output_dir.mkdir(parents=True, exist_ok=True)
report = {
    "test": "stress_test_1",
    "session_id": session.id,
    "duration_s": duration,
    "custom_tool_calls": custom_tool_calls,
    "mcp_tool_calls": mcp_tool_calls,
    "soql_queries": len(soql_queries),
    "slack_posts": slack_posts,
    "agent_messages": len(agent_text_parts),
    "errors": errors,
    "agent_output_chars": sum(len(t) for t in agent_text_parts),
}
(output_dir / "stress_test_1_results.json").write_text(json.dumps(report, indent=2))

print("\n" + "=" * 60)
print("STRESS TEST 1 RESULTS")
print("=" * 60)
print(f"  Duration:            {duration}s ({duration // 60}m {duration % 60}s)")
print(f"  Custom tool calls:   {custom_tool_calls}")
print(f"  MCP tool calls:      {mcp_tool_calls}")
print(f"  SOQL queries:        {len(soql_queries)}")
print(f"  Slack posts:         {slack_posts}")
print(f"  Agent messages:      {len(agent_text_parts)}")
print(f"  Agent output:        {sum(len(t) for t in agent_text_parts):,} chars")
print(f"  Errors:              {len(errors)}")
if errors:
    for e in errors:
        print(f"    - {e}")

verdict = (
    "PASS"
    if not errors and len(agent_text_parts) > 0
    else "NEEDS_REVIEW"
    if errors
    else "FAIL"
)
print(f"\n  VERDICT: {verdict}")
