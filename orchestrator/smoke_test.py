"""Smoke test suite for GTM Health Agent.

Run after deploys to verify all APIs are working:
    python orchestrator/smoke_test.py
"""

import os
import sys
from pathlib import Path

dotenv = Path(__file__).parent.parent / ".env"
if dotenv.exists():
    for line in dotenv.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

import anthropic
from slack_sdk import WebClient

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
slack = WebClient(token=os.environ["SLACK_BOT_TOKEN"])

results = {}


# Renamed from `test` to `_check` so pytest's default test-discovery
# (functions matching `test_*` / `test`) does not pick this helper up and
# try to inject `name`/`fn` as fixtures. This file is designed to run as
# `python orchestrator/smoke_test.py`, not via pytest.
def _check(name, fn):
    print(f"{name}...", end=" ", flush=True)
    try:
        result = fn()
        print(f"OK ({result})")
        results[name] = "PASS"
    except Exception as e:
        print(f"FAIL: {e}")
        results[name] = f"FAIL: {e}"


def test_messages_sonnet():
    r = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=10,
        messages=[{"role": "user", "content": "Say OK"}],
    )
    return r.content[0].text


def test_messages_opus():
    r = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=100,
        thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": "Say OK"}],
    )
    return next((b.text for b in r.content if b.type == "text"), "")


def test_list_agents():
    agents = client.beta.agents.list()
    return f"{len(agents.data)} agents"


def test_environment():
    env = client.beta.environments.retrieve(os.environ["ENVIRONMENT_ID"])
    return env.id


def test_memory_methodology():
    ms = client.beta.memory_stores.retrieve(os.environ["METHODOLOGY_STORE_ID"])
    return ms.name


def test_memory_health():
    ms = client.beta.memory_stores.retrieve(os.environ["HEALTH_STORE_ID"])
    return ms.name


def test_vault():
    vault_id = os.environ.get("ACME_VAULT_ID", "")
    if not vault_id:
        return "SKIP — ACME_VAULT_ID not set (SF_MCP_VIA_VAULT=false)"
    v = client.beta.vaults.retrieve(vault_id)
    return v.display_name


def test_quick_answer_session():
    vault_ids = [v for v in [os.environ.get("ACME_VAULT_ID", "")] if v]
    s = client.beta.sessions.create(
        agent=os.environ["QUICK_AGENT_ID"],
        environment_id=os.environ["ENVIRONMENT_ID"],
        title="Smoke test: Quick Answer",
        vault_ids=vault_ids,
    )
    client.beta.sessions.events.send(
        session_id=s.id,
        events=[
            {
                "type": "user.message",
                "content": [
                    {"type": "text", "text": "How many open opps? Just the number."}
                ],
            }
        ],
    )
    with client.beta.sessions.events.stream(session_id=s.id) as stream:
        for event in stream:
            if event.type == "agent.message":
                for block in event.content:
                    if hasattr(block, "text") and block.text:
                        return block.text[:100]
            elif event.type == "session.error":
                ed = event.error if hasattr(event, "error") else None
                raise RuntimeError(getattr(ed, "message", "unknown error"))
            elif event.type == "session.status_idle":
                sr = getattr(event, "stop_reason", None)
                if getattr(sr, "type", None) != "requires_action":
                    return "idle"
            elif event.type == "session.status_terminated":
                return "terminated"


def test_coordinator_session():
    vault_ids = [v for v in [os.environ.get("ACME_VAULT_ID", "")] if v]
    s = client.beta.sessions.create(
        agent=os.environ["COORDINATOR_ID"],
        environment_id=os.environ["ENVIRONMENT_ID"],
        title="Smoke test: Coordinator",
        vault_ids=vault_ids,
    )
    client.beta.sessions.events.send(
        session_id=s.id,
        events=[
            {
                "type": "user.message",
                "content": [
                    {
                        "type": "text",
                        "text": "SELECT COUNT(Id) FROM Opportunity WHERE IsClosed = true AND CreatedDate >= 2025-01-01T00:00:00Z. Just the number.",
                    }
                ],
            }
        ],
    )
    with client.beta.sessions.events.stream(session_id=s.id) as stream:
        for event in stream:
            if event.type == "agent.message":
                for block in event.content:
                    if hasattr(block, "text") and block.text:
                        return block.text[:100]
            elif event.type == "session.error":
                ed = event.error if hasattr(event, "error") else None
                raise RuntimeError(getattr(ed, "message", "unknown error"))
            elif event.type == "session.status_idle":
                sr = getattr(event, "stop_reason", None)
                if getattr(sr, "type", None) != "requires_action":
                    return "idle"
            elif event.type == "session.status_terminated":
                return "terminated"


def test_slack_post():
    r = slack.chat_postMessage(
        channel=os.environ["SLACK_CHANNEL_ID"],
        text=":white_check_mark: Smoke test passed.",
    )
    return f"ts={r['ts']}"


def test_slack_read():
    history = slack.conversations_history(
        channel=os.environ["SLACK_CHANNEL_ID"],
        limit=1,
    )
    return f"{len(history['messages'])} messages"


def test_postgres():
    import psycopg2

    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        return "SKIP — DATABASE_URL not set"
    conn = psycopg2.connect(db_url)
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public'"
    )
    tables = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM snapshots")
    snapshots = cur.fetchone()[0]
    conn.close()
    return f"{tables} tables, {snapshots} snapshots"


if __name__ == "__main__":
    _check("1. Messages API (Sonnet)", test_messages_sonnet)
    _check("2. Messages API (Opus 4.8)", test_messages_opus)
    _check("3. List agents", test_list_agents)
    _check("4. Environment", test_environment)
    _check("5. Memory store (methodology)", test_memory_methodology)
    _check("6. Memory store (health)", test_memory_health)
    _check("7. Vault (Acme)", test_vault)
    _check("8. Quick Answer + MCP", test_quick_answer_session)
    _check("9. Coordinator + MCP", test_coordinator_session)
    _check("10. Slack post", test_slack_post)
    _check("11. Slack read", test_slack_read)
    _check("12. Railway Postgres", test_postgres)

    print("\n" + "=" * 60)
    print("SMOKE TEST SUMMARY")
    print("=" * 60)
    passed = sum(1 for v in results.values() if v == "PASS")
    failed = sum(1 for v in results.values() if v != "PASS")
    for name, result in results.items():
        icon = "PASS" if result == "PASS" else "FAIL"
        print(f"  [{icon}] {name}")
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if failed == 0 else 1)
