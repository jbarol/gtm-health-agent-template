#!/usr/bin/env python3
"""Rollback an Anthropic Managed Agent to a previous version.

Plan #41 — emergency lever. Use when a freshly-deployed prompt regresses
and we need the prior version active inside seconds, before the next
session burns more spend on broken behavior.

Usage:
    python bin/rollback-agent.py <agent_short_name> --to-version <N>
    python bin/rollback-agent.py coordinator --to-version 26
    python bin/rollback-agent.py coordinator --to-version 26 --no-pr --no-dm

Steps:
    1. Resolve ``<agent_short_name>`` to an agent ID via
       ``agents/update_prompts.py:AGENTS``.
    2. Retrieve the current agent (latest active version) and the target
       version ``N``. Abort if ``--to-version`` matches current active.
       Abort if ``N`` is unknown (catches typos).
    3. Call ``client.beta.agents.update(agent_id, version=<current>,
       system=<v_N.system>, model=<v_N.model.id>, ...)`` to make
       version ``N``'s configuration the new active version. The SDK
       returns a new version number (``current + 1``) whose content
       mirrors ``N``.
    4. Update ``agents/active_versions.json`` so CI verification doesn't
       flag drift on the next run.
    5. DM admins via ``slack_bot.send_dm`` (skip with ``--no-dm``).
    6. Open a one-line PR on a new branch ``rollback/<agent>-to-v<N>``
       containing the pin-file change (skip with ``--no-pr``).

SDK reality (verified 2026-05-11, anthropic-py 0.100.0):
    The SDK has no ``active_version=`` kwarg and no ``set_active``
    endpoint. The only way to make an older version "active" is to
    write its content forward as a new version. That's the same pattern
    ``agents/update_coordinator_roster.py`` uses (PR #94). The pin file
    always tracks the latest (= currently active) version number.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO_ROOT / "agents"
ORCH_DIR = REPO_ROOT / "orchestrator"

# Make agents/ and orchestrator/ importable when this script is invoked
# directly (e.g. ``python bin/rollback-agent.py ...``) so we can re-use
# the AGENTS registry + active_versions helpers + slack_bot.send_dm.
for _p in (AGENTS_DIR, ORCH_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _load_env() -> None:
    """Manual dotenv loader matching ``orchestrator/config.py``.

    Run at function-call time (not module load) so test code can
    monkey-patch the environment without re-importing.
    """
    dotenv = REPO_ROOT / ".env"
    if not dotenv.exists():
        return
    for line in dotenv.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def _build_client():
    """Return an Anthropic client. Imported lazily so tests can stub."""
    import anthropic  # noqa: WPS433 — local import is intentional

    return anthropic.Anthropic()


def _resolve_agent(agent_short_name: str) -> tuple[str, str]:
    """Look up ``(agent_id, target_model)`` from the AGENTS registry."""
    from update_prompts import AGENTS  # type: ignore

    if agent_short_name not in AGENTS:
        known = ", ".join(sorted(AGENTS.keys()))
        raise SystemExit(f"Unknown agent '{agent_short_name}'. Known: {known}")
    cfg = AGENTS[agent_short_name]
    agent_id = cfg.get("id") or ""
    if not agent_id:
        raise SystemExit(
            f"Agent '{agent_short_name}' has no ID set (env var unset). "
            "Set the corresponding *_ID env var or update agents/update_prompts.py."
        )
    return agent_id, cfg["model"]


def _send_dm_safe(text: str) -> None:
    """DM admins. Never raise — rollback must succeed even if Slack is down."""
    try:
        from cost_digest import _resolve_admin_ids  # type: ignore
        from slack_bot import send_dm  # type: ignore
    except Exception as exc:
        print(f"[DM-SKIP] slack_bot import failed: {exc}")
        return
    try:
        admins = _resolve_admin_ids()
    except Exception as exc:
        print(f"[DM-SKIP] admin id resolution failed: {exc}")
        return
    if not admins:
        print("[DM-SKIP] no admins configured (SLACK_ADMIN_USER_IDS unset)")
        return
    for uid in admins:
        try:
            send_dm(uid, text)
            print(f"[DM-OK] sent to {uid}")
        except Exception as exc:
            print(f"[DM-FAIL] {uid}: {exc}")


def _open_pr(agent_short_name: str, new_version: int, target_version: int) -> None:
    """Open a one-line PR with the active_versions.json change."""
    branch = f"rollback/{agent_short_name}-to-v{target_version}"
    title = (
        f"chore(rollback): pin {agent_short_name} to v{target_version} "
        f"(now active as v{new_version})"
    )
    body = (
        f"Emergency rollback of `{agent_short_name}` to the configuration "
        f"of version {target_version}. The SDK created a new version "
        f"{new_version} carrying v{target_version}'s system prompt and "
        f"model — that is now the active version on Anthropic.\n\n"
        f"`agents/active_versions.json` updated to reflect the new state "
        f"so CI verification does not flag drift.\n\n"
        f"Posted by `bin/rollback-agent.py`."
    )
    commands = [
        ["git", "checkout", "-b", branch],
        ["git", "add", "agents/active_versions.json"],
        ["git", "commit", "-m", title],
        ["git", "push", "-u", "origin", branch],
        ["gh", "pr", "create", "--base", "main", "--title", title, "--body", body],
    ]
    for cmd in commands:
        print(f"[PR] $ {' '.join(cmd)}")
        try:
            subprocess.run(cmd, check=True, cwd=str(REPO_ROOT))
        except subprocess.CalledProcessError as exc:
            print(f"[PR-FAIL] {' '.join(cmd)}: {exc}")
            return
        except FileNotFoundError as exc:
            # ``gh`` may not be installed on the runner — log + continue.
            print(f"[PR-SKIP] command not available: {exc}")
            return


def rollback(
    agent_short_name: str,
    to_version: int,
    *,
    open_pr: bool = True,
    send_dm: bool = True,
    client=None,
) -> int:
    """Run a rollback. Returns the new active version on success.

    Public entry point — tests call this directly with a mocked client.
    """
    from update_prompts import (  # type: ignore
        read_active_versions,
        write_active_versions,
    )

    agent_id, _target_model = _resolve_agent(agent_short_name)
    client = client or _build_client()

    current = client.beta.agents.retrieve(agent_id)
    current_version = int(current.version)

    if to_version == current_version:
        raise SystemExit(
            f"No-op: '{agent_short_name}' is already at version "
            f"{current_version}. Pick a different --to-version."
        )

    # Verify the target version exists on the server before we try to
    # roll back. Catches typos ("--to-version 260" when v26 was intended).
    try:
        target = client.beta.agents.retrieve(agent_id, version=to_version)
    except Exception as exc:
        raise SystemExit(
            f"Unknown version: --to-version {to_version} is not retrievable "
            f"for '{agent_short_name}' ({agent_id}). Error: {exc}"
        )

    target_model = target.model.id if hasattr(target.model, "id") else str(target.model)
    target_system = target.system

    print(
        f"[ROLLBACK] {agent_short_name} ({agent_id}) "
        f"current v{current_version} -> target v{to_version}"
    )
    print(f"  target model:  {target_model}")
    print(f"  target system: {len(target_system or ''):,} chars")

    updated = client.beta.agents.update(
        agent_id,
        version=current_version,
        system=target_system,
        model=target_model,
    )
    new_version = int(updated.version)
    print(
        f"[ROLLBACK-OK] new active version: v{new_version} (content of v{to_version})"
    )

    pins = read_active_versions()
    pins[agent_short_name] = new_version
    write_active_versions(pins)
    print(f"[PIN] {agent_short_name} = v{new_version} in active_versions.json")

    if send_dm:
        _send_dm_safe(
            f":rewind: *Rollback*: `{agent_short_name}` rolled back to the "
            f"content of v{to_version}. New active version: v{new_version}. "
            f"Run by `bin/rollback-agent.py`."
        )

    if open_pr:
        _open_pr(agent_short_name, new_version, to_version)

    return new_version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rollback an Anthropic Managed Agent to a previous version."
    )
    parser.add_argument(
        "agent_short_name",
        help="Agent short name from agents/update_prompts.py:AGENTS (e.g. coordinator).",
    )
    parser.add_argument(
        "--to-version",
        type=int,
        required=True,
        help="Version number to roll back to (must exist on Anthropic).",
    )
    parser.add_argument(
        "--no-pr",
        action="store_true",
        help="Do not open a pin-file PR (useful for hot-fix dry-runs).",
    )
    parser.add_argument(
        "--no-dm",
        action="store_true",
        help="Do not DM admins.",
    )
    args = parser.parse_args(argv)

    _load_env()
    rollback(
        args.agent_short_name,
        args.to_version,
        open_pr=not args.no_pr,
        send_dm=not args.no_dm,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
