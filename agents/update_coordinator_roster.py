"""
Update the live Coordinator's multiagent roster without re-running
setup_agents.py (which would re-mint other agents and memory stores).

History:
  - 2026-05-11: First wired the 4 reasoning/synthesis/chart sub-agents
    (Statistician, Adversarial Reviewer, Cross-Domain Synthesizer, Chart
    Designer) alongside the 3 specialist Monitors → roster of 7.
  - 2026-05-27: Added Writing Agent to the roster → roster of 8. The
    Coordinator now delegates prose composition via the multiagent runtime
    instead of the prior ``write_prose`` custom tool. See CLAUDE.md
    "Writing pass" section for the new dispatch contract.

Run: python agents/update_coordinator_roster.py
"""

import os
from pathlib import Path

# Load .env (same manual parser used by update_prompts.py).
dotenv = Path(__file__).parent.parent / ".env"
for line in dotenv.read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

import anthropic

client = anthropic.Anthropic()

COORDINATOR_ID = os.environ["COORDINATOR_ID"]

# Sub-agent IDs — source of truth lives in agents/update_prompts.py:AGENTS.
PIPELINE_MONITOR_ID = os.environ.get(
    "PIPELINE_MONITOR_ID", "agent_EXAMPLE_pipeline_monitor"
)
SALES_MONITOR_ID = os.environ.get("SALES_MONITOR_ID", "agent_EXAMPLE_sales_monitor")
POSTSALES_MONITOR_ID = os.environ.get(
    "POSTSALES_MONITOR_ID", "agent_EXAMPLE_postsales_monitor"
)
STATISTICIAN_ID = os.environ.get("STATISTICIAN_ID", "agent_EXAMPLE_statistician")
ADVERSARIAL_REVIEWER_ID = os.environ.get(
    "ADVERSARIAL_REVIEWER_ID", "agent_EXAMPLE_adversarial_reviewer"
)
CROSS_DOMAIN_SYNTHESIZER_ID = os.environ.get(
    "CROSS_DOMAIN_SYNTHESIZER_ID", "agent_EXAMPLE_cross_domain_synthesizer"
)
CHART_DESIGNER_ID = os.environ.get(
    "CHART_DESIGNER_ID", "agent_EXAMPLE_chart_designer"
)
WRITING_AGENT_ID = os.environ.get("WRITING_AGENT_ID", "agent_EXAMPLE_legacy")

ROSTER = [
    {"type": "agent", "id": PIPELINE_MONITOR_ID},
    {"type": "agent", "id": SALES_MONITOR_ID},
    {"type": "agent", "id": POSTSALES_MONITOR_ID},
    {"type": "agent", "id": STATISTICIAN_ID},
    {"type": "agent", "id": ADVERSARIAL_REVIEWER_ID},
    {"type": "agent", "id": CROSS_DOMAIN_SYNTHESIZER_ID},
    {"type": "agent", "id": CHART_DESIGNER_ID},
    {"type": "agent", "id": WRITING_AGENT_ID},
]

current = client.beta.agents.retrieve(COORDINATOR_ID)
updated = client.beta.agents.update(
    COORDINATOR_ID,
    version=current.version,
    multiagent={"type": "coordinator", "agents": ROSTER},
)
print(f"COORDINATOR_ID={updated.id}")
print(f"COORDINATOR_VERSION={updated.version}")
print(f"Roster size: {len(ROSTER)}")
for entry in ROSTER:
    print(f"  - {entry['id']}")
