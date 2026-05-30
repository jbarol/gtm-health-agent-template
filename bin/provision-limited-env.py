#!/usr/bin/env python3
"""Provision a Managed-Agents environment with `networking.type = "limited"`.

Plan #44, Task #11 — two-env shadow rollout (decision row #8).

The live env (`ENVIRONMENT_ID`) was created with
``networking.type = "unrestricted"`` (see ``agents/setup_agents.py:445-461``).
The Anthropic Managed-Agents docs explicitly recommend production environments
flip to ``limited`` with an explicit ``allowed_hosts`` allowlist. This script
provisions a SECOND environment alongside the live one (shadow), so the
operator can route a percentage of sessions through it during a bake window
before flipping ``ENVIRONMENT_ID`` system-wide.

Usage:
    # Print the call that would be made; no side effects.
    python bin/provision-limited-env.py

    # Actually create the environment on Anthropic.
    python bin/provision-limited-env.py --apply

    # Use a custom name (default: gtm-health-env-limited).
    python bin/provision-limited-env.py --apply --name gtm-health-env-limited-v2

See ``docs/runbooks/managed-agents-conformance.md`` for the full bake-and-flip
playbook (trace hosts → provision shadow env → set
``ENVIRONMENT_ID_LIMITED`` + ``LIMITED_NETWORKING_SHADOW_PCT`` → bake 48h →
flip ``ENVIRONMENT_ID``).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# HTTPS-prefixed entries per Anthropic Managed-Agents networking docs. Each
# host below is justified — keep this list explicit so the operator running
# the trace step (``bin/trace-outbound-hosts.py``) can diff observed-vs-allowed.
ALLOWED_HOSTS: list[str] = [
    # Anthropic Messages + Sessions API
    "https://api.anthropic.com",
    # Anthropic Files API (file uploads from the sandbox for renderer
    # downloads and the future xlsx skill output)
    "https://files.api.anthropic.com",
    # Slack Web API + Socket Mode WebSocket origin
    "https://*.slack.com",
    "https://wss-primary.slack.com",
    # Salesforce REST/Bulk API (each portco gets its own *.my.salesforce.com).
    # ``login.salesforce.com`` is the DEFAULT host that
    # ``simple_salesforce.Salesforce(username=..., password=..., security_token=...)``
    # uses when no instance_url is passed — see
    # ``orchestrator/session_runner.py`` SOAP fallback path (consumer_key
    # absent). Required for any non-OAuth portco auth under limited
    # networking; without it the auth call is a blocked-host event.
    "https://login.salesforce.com",
    "https://*.salesforce.com",
    "https://*.my.salesforce.com",
    # QuickChart for chart rendering
    "https://quickchart.io",
    # Compresr SDK endpoint (see orchestrator/compresr_client.py; SDK defaults
    # to api.compresr.com — confirmed 2026-05-13)
    "https://api.compresr.com",
    # GitHub API — used by any code-pull MCP path + CI artifact retrieval
    "https://api.github.com",
    # NOTE: pypi.org and files.pythonhosted.org are intentionally OMITTED.
    # ``allow_package_managers=false`` (below) blocks pip entirely at the
    # sandbox layer — the package manager cannot run regardless of network
    # reachability, so allowlisting the wheel CDN would be dead weight.
    # If a future change flips ``allow_package_managers=true``, add BOTH
    # ``https://pypi.org`` AND ``https://files.pythonhosted.org`` as a pair
    # (pip resolves metadata at pypi.org BEFORE fetching wheels from the
    # CDN — one without the other is incomplete).
]

# Pre-installed pip packages — copied from ``agents/setup_agents.py``
# (``config.packages.pip``). Kept in this script so this tool can run without
# importing setup_agents (which would trigger heavy SDK setup at import time).
PIP_PACKAGES: list[str] = [
    "pandas",
    "numpy",
    "openpyxl",
    "xlsxwriter",
    "python-docx",
    "python-pptx",
    "matplotlib",
    "seaborn",
]


def _load_env() -> None:
    """Manual dotenv loader (matches orchestrator/config.py)."""
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
    import anthropic  # noqa: WPS433

    return anthropic.Anthropic()


def _build_config() -> dict:
    """Return the environment ``config`` payload sent to Anthropic.

    Pure function — no I/O. Tests inspect the return value to assert the
    payload shape without exercising the network.
    """
    return {
        "type": "cloud",
        "networking": {
            "type": "limited",
            "allowed_hosts": list(ALLOWED_HOSTS),
        },
        # Keep allow_mcp_servers=True while Bundle D's vault SF MCP path is
        # still in flight — flipping false would break the vault decision
        # before it's locked. Revisit after Plan #44 Task #17 lands.
        "allow_mcp_servers": True,
        # We already pre-install the packages we need via ``config.packages``
        # — denying runtime ``pip install`` shrinks the sandbox attack
        # surface and matches the docs' production guidance.
        "allow_package_managers": False,
        "packages": {"pip": list(PIP_PACKAGES)},
    }


def _print_next_steps(env_id: str, name: str) -> None:
    """Print the operator's next steps after a successful --apply.

    Kept in a helper so tests can stub it out and inspect the env_id flow
    via a recorded call instead of stdout scraping.
    """
    print()
    print("=== Next steps ===")
    print("1. Set the Railway build-time variable:")
    print(f"     ENVIRONMENT_ID_LIMITED={env_id}")
    print("2. Start the shadow rollout by setting:")
    print("     LIMITED_NETWORKING_SHADOW_PCT=10")
    print("3. Smoke-test the new env with the trio:")
    print("     - dream-cycle (cron) trigger via RUN_NIGHTLY_NOW=1")
    print("     - adhoc Slack question (any portco channel)")
    print("     - list-pull (e.g. 'pull all open opps for Acme as xlsx')")
    print("4. Bake for 48h. Watch admin DMs for blocked-host errors.")
    print("   Hosts that block will surface as ``session.error`` events;")
    print("   bin/trace-outbound-hosts.py --days 2 after the bake will")
    print("   confirm or refute the allowlist.")
    print("5. KEEP THE OLD UNRESTRICTED ENV ACTIVE during bake. Only flip")
    print(f"   ENVIRONMENT_ID={env_id} after the bake passes.")
    print()
    print(f"Env name: {name}")
    print(f"Env id:   {env_id}")
    print()
    print("Runbook: docs/runbooks/managed-agents-conformance.md")


def provision(
    *,
    name: str = "gtm-health-env-limited",
    apply: bool = False,
    client=None,
) -> str | None:
    """Create the limited-networking environment.

    When ``apply=False`` (default), prints what would be sent and returns
    None — no network call. When ``apply=True``, calls
    ``client.beta.environments.create(...)`` and returns the new env id.
    """
    cfg = _build_config()

    print(f"[PROVISION] name={name!r}, apply={apply}")
    print("[PROVISION] networking.type=limited")
    print(
        f"[PROVISION] allowed_hosts={len(cfg['networking']['allowed_hosts'])} entries"
    )
    for host in cfg["networking"]["allowed_hosts"]:
        print(f"    - {host}")
    print(f"[PROVISION] allow_mcp_servers={cfg['allow_mcp_servers']}")
    print(f"[PROVISION] allow_package_managers={cfg['allow_package_managers']}")
    print(f"[PROVISION] packages.pip={cfg['packages']['pip']}")

    if not apply:
        print()
        print("[DRY-RUN] No API call made. Re-run with --apply to provision.")
        return None

    client = client or _build_client()
    env = client.beta.environments.create(name=name, config=cfg)
    env_id = env.id
    print(f"[PROVISION-OK] ENVIRONMENT_ID_LIMITED={env_id}")

    _print_next_steps(env_id, name)
    return env_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Provision a limited-networking shadow environment for the GTM "
            "Health Agent (Plan #44 task #11). See "
            "docs/runbooks/managed-agents-conformance.md for the bake-and-flip "
            "playbook."
        )
    )
    # ``--apply`` and ``--dry-run`` are mutually exclusive — passing both is a
    # hard argparse error, so an operator can't accidentally combine them and
    # have ``--apply`` silently win. Default (neither flag passed) is dry-run.
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Actually create the environment. Without this flag the script runs in dry-run mode (the default).",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be sent and exit without an API call (the default when neither flag is passed).",
    )
    parser.add_argument(
        "--name",
        default="gtm-health-env-limited",
        help="Environment name on Anthropic (default: gtm-health-env-limited).",
    )
    args = parser.parse_args(argv)

    _load_env()
    # ``--dry-run`` is implicit when ``--apply`` is absent; the mutually
    # exclusive group already rejects the both-set case at parse time.
    env_id = provision(name=args.name, apply=args.apply and not args.dry_run)
    return 0 if (env_id or not args.apply) else 1


if __name__ == "__main__":
    sys.exit(main())
