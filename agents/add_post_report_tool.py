"""One-shot migration: add post_report custom tool to Coordinator and Quick Answer.

Run: python agents/add_post_report_tool.py

Idempotent — if post_report is already present, the script leaves the agent
unchanged. Run it again after redeploying setup_agents.py to a fresh account
(those agents already include post_report at creation, so this is a no-op).

After this script succeeds:
- Coordinator (and Quick Answer) can call post_report alongside their existing
  custom tools (send_slack_notification, generate_chart, db_query, etc.).
- The orchestrator's _dispatch_tool routes post_report through response_renderer.

Until the orchestrator wires up the post_report handler (Phase 2.2), the
agents have access to the tool but the orchestrator will return an
"Unknown tool" error if they call it. Run this script BEFORE the prompts are
updated (Phase 3) to avoid that error.
"""

import os
from pathlib import Path

dotenv = Path(__file__).parent.parent / ".env"
for line in dotenv.read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

import anthropic

from build_post_report_schema import build_post_report_input_schema

# Keep this in sync with POST_REPORT_TOOL in setup_agents.py.
# input_schema is derived from response_schemas.RESPONSE_TYPES via
# build_post_report_input_schema() — single source of truth (Plan #32).
POST_REPORT_TOOL = {
    "type": "custom",
    "name": "post_report",
    "description": (
        "Post a structured, typed final report to Slack. Use this for every "
        "user-facing FINAL deliverable. The payload must match the schema for "
        "the chosen response_type (see orchestrator/response_schemas.py). "
        "Length caps on every field; the orchestrator will reject overruns. "
        "Emit plain text in each field — the renderer adds Slack formatting."
    ),
    "input_schema": build_post_report_input_schema(),
}

AGENTS_TO_UPDATE = [
    ("coordinator", os.environ.get("COORDINATOR_ID")),
    ("quick_answer", os.environ.get("QUICK_AGENT_ID")),
]


def _tool_to_dict(tool):
    """Convert a tool object from the API back into a dict for re-sending.

    The API returns Pydantic objects; we need plain dicts to pass back to
    agents.update().
    """
    if hasattr(tool, "model_dump"):
        return tool.model_dump(exclude_none=True)
    if isinstance(tool, dict):
        return tool
    # Last-resort: pull fields manually
    return {
        k: v for k, v in vars(tool).items() if not k.startswith("_") and v is not None
    }


def main():
    client = anthropic.Anthropic()

    for name, agent_id in AGENTS_TO_UPDATE:
        if not agent_id:
            print(f"[SKIP] {name}: no agent_id in .env")
            continue

        try:
            agent = client.beta.agents.retrieve(agent_id)
            current_tools = [_tool_to_dict(t) for t in (agent.tools or [])]
            current_tool_names = {
                t.get("name") for t in current_tools if t.get("type") == "custom"
            }

            if "post_report" in current_tool_names:
                print(f"[SKIP] {name}: post_report already present (v{agent.version})")
                continue

            new_tools = current_tools + [POST_REPORT_TOOL]
            updated = client.beta.agents.update(
                agent_id,
                version=agent.version,
                tools=new_tools,
            )
            print(
                f"[OK] {name}: v{agent.version} -> v{updated.version} | "
                f"tools {len(current_tools)} -> {len(new_tools)}"
            )
        except Exception as e:
            print(f"[FAIL] {name} ({agent_id}): {e}")


if __name__ == "__main__":
    main()
