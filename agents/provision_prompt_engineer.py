"""Provision the Prompt Engineer (Sonnet 4.6) ONCE.

Run this once after merging the Plan #44 Bundle A PR to mint the new
Anthropic managed agent. The setup_agents.py top-level script does NOT
create the Prompt Engineer (it pre-dates the agent); existing deployments
run update_prompts.py for prompt-only updates. This script is the
forward-fix for both paths — it mints the agent once and then
update_prompts.py keeps the system prompt in sync on every subsequent
deploy.

After running:
    1. Copy the printed PROMPT_ENGINEER_ID into .env locally
    2. Add PROMPT_ENGINEER_ID to Railway environment variables
    3. railway redeploy --service "GTM Health Agent" -y

The script reads the same prompt that update_prompts.py would push so
this script and the CI deploy path stay in lock-step.

Idempotency: the script does NOT check whether a Prompt Engineer
already exists. Running it twice creates two agents — that's
intentional. If you need a clean slate, retire the old agent via the
Anthropic Console before running again.

Run: python agents/provision_prompt_engineer.py
"""

import os
import sys
from pathlib import Path

# Load .env so we have ANTHROPIC_API_KEY
dotenv = Path(__file__).parent.parent / ".env"
if dotenv.exists():
    for line in dotenv.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

import anthropic  # noqa: E402

# Import the same prompt that ships in update_prompts.py so this script
# and update_prompts.py both push identical bytes. The PROMPTS dict is
# populated at module-load time for the Prompt Engineer (unlike the
# Writing Agent, whose prompt is loaded lazily), so a plain import is
# sufficient.
sys.path.insert(0, str(Path(__file__).parent))
from update_prompts import PROMPTS  # noqa: E402


PROMPT_ENGINEER_MODEL = "claude-sonnet-4-6"


def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        sys.exit("ANTHROPIC_API_KEY not set — check .env")

    client = anthropic.Anthropic(api_key=api_key)

    prompt = PROMPTS.get("prompt_engineer")
    if not prompt:
        sys.exit("PROMPTS['prompt_engineer'] is empty — fix update_prompts.py")

    print(f"Creating Prompt Engineer (model={PROMPT_ENGINEER_MODEL})...")
    agent = client.beta.agents.create(
        name="GTM Prompt Engineer",
        model=PROMPT_ENGINEER_MODEL,
        description=(
            "Preprocesses Slack questions before they reach the Coordinator. "
            "Reads /{portco}/instructions.md, injects standing data rules, "
            "and emits a JSON object with improved_prompt, plan, expected "
            "output, and risk flags. Single-turn, no MCP."
        ),
        system=prompt,
        tools=[{"type": "agent_toolset_20260401"}],
    )

    print()
    print(f"PROMPT_ENGINEER_ID={agent.id}")
    print(f"PROMPT_ENGINEER_VERSION={agent.version}")
    print()
    print("Next steps:")
    print(f"  1. Add PROMPT_ENGINEER_ID={agent.id} to .env")
    print(f"  2. Add PROMPT_ENGINEER_ID={agent.id} to Railway env vars")
    print('  3. railway redeploy --service "GTM Health Agent" -y')


if __name__ == "__main__":
    main()
