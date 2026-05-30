"""Provision the Writing Agent (Haiku 4.5) ONCE.

Run this once after merging the writing-agent PR to mint the new Anthropic
managed agent. The setup_agents.py top-level script also creates it on a
fresh setup, but existing deployments don't re-run setup_agents — they run
update_prompts.py, which only updates existing agents. This script is the
forward-fix for those.

After running:
    1. Copy the printed WRITING_AGENT_ID into .env locally
    2. Add WRITING_AGENT_ID to Railway environment variables
    3. railway redeploy --service "GTM Health Agent" -y

The script reads the same prompt + model that setup_agents.py would use,
so it stays in lock-step with new deployments.

Idempotency: the script does NOT check whether a Writing Agent already
exists. Running it twice creates two agents — that's intentional. If you
need a clean slate, retire the old agent via the Anthropic Console before
running again.

Run: python agents/provision_writing_agent.py
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

# Import the same prompt that ships in setup_agents.py so this script and
# update_prompts.py and setup_agents.py all agree on what the live agent
# should think. update_prompts.PROMPTS["writing_agent"] is the canonical
# source — both setup_agents.py and this script read from it indirectly via
# the mirrored string below. Update one, update all three; the test suite
# enforces alignment.
sys.path.insert(0, str(Path(__file__).parent))
from setup_agents import WRITING_AGENT_TOOLS  # noqa: E402
from update_prompts import PROMPTS, _load_writing_agent_prompt  # noqa: E402

# update_prompts no longer eagerly loads PROMPTS["writing_agent"] at module
# import time (so the verifier workflow can import without Slack env vars).
# This provisioning script DOES need the prompt — call the loader explicitly.
_load_writing_agent_prompt()


WRITING_AGENT_MODEL = "claude-haiku-4-5"


def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        sys.exit("ANTHROPIC_API_KEY not set — check .env")

    client = anthropic.Anthropic(api_key=api_key)

    prompt = PROMPTS.get("writing_agent")
    if not prompt:
        sys.exit("PROMPTS['writing_agent'] is empty — fix update_prompts.py")

    print(f"Creating Writing Agent (model={WRITING_AGENT_MODEL})...")
    agent = client.beta.agents.create(
        name="GTM Writing Agent",
        model=WRITING_AGENT_MODEL,
        description=(
            "Composes user-facing prose from validated structured findings. "
            "Grounded in Strunk's Elements of Style. Delegated to by the "
            "Coordinator via the multiagent runtime; the writing_agent "
            "thread persists across delegations within a parent session."
        ),
        system=prompt,
        # Share the canonical tools list with setup_agents.py and
        # update_subagent_tools.py — a rotation minted here picks up
        # query_artifact and reasoning_summary in the same shape the
        # canonical reconciler would.
        tools=WRITING_AGENT_TOOLS,
    )

    print()
    print(f"WRITING_AGENT_ID={agent.id}")
    print(f"WRITING_AGENT_VERSION={agent.version}")
    print()
    print("Next steps:")
    print(f"  1. Add WRITING_AGENT_ID={agent.id} to .env")
    print(f"  2. Add WRITING_AGENT_ID={agent.id} to Railway env vars")
    print('  3. railway redeploy --service "GTM Health Agent" -y')


if __name__ == "__main__":
    main()
