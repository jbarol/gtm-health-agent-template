#!/usr/bin/env python3
"""Scan recent session events and surface every outbound hostname.

Plan #44, Task #11 — negative-test trace (decision row #8). MUST be run BEFORE
``bin/provision-limited-env.py --apply`` so the allowlist is grounded in
observed traffic rather than wishful thinking.

Pipeline:
    1. Read every session_id from ``session_costs`` within the last N days
       (default 7).
    2. For each session, fetch ``client.beta.sessions.events.list(session_id)``
       and walk every event looking for outbound hostnames in:
         - ``agent.custom_tool_use`` input fields (URLs, instance_url, host)
         - ``user.custom_tool_result`` content (URL-shaped strings)
         - ``session.error`` messages (blocked-host errors name the host)
    3. Compare the observed set against the static expected allowlist (copied
       from ``bin/provision-limited-env.py:ALLOWED_HOSTS``).
    4. Print three buckets:
         - observed AND on allowlist (OK)
         - observed but NOT on allowlist (WARNING — would break under limited)
         - on allowlist but NOT observed (potentially droppable)

Exit codes:
    0 — all observed hosts are on the allowlist
    1 — at least one observed host is missing from the allowlist
    2 — could not run (no DATABASE_URL, no ANTHROPIC_API_KEY, etc.)

See ``docs/runbooks/managed-agents-conformance.md`` for the full bake-and-flip
playbook.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
ORCH_DIR = REPO_ROOT / "orchestrator"

# Make orchestrator/ importable so we can re-use db_adapter without copying.
if str(ORCH_DIR) not in sys.path:
    sys.path.insert(0, str(ORCH_DIR))

# Static allowlist — keep in sync with ``bin/provision-limited-env.py``.
# Stored as host-only (no scheme, lowercase) so glob matching against
# observed-from-URL hostnames is exact.
EXPECTED_HOSTS: list[str] = [
    "api.anthropic.com",
    "files.api.anthropic.com",
    "*.slack.com",
    "wss-primary.slack.com",
    # ``login.salesforce.com`` is the SOAP-auth default host for
    # ``simple_salesforce.Salesforce(username=..., password=..., security_token=...)``
    # when no consumer_key/instance_url is configured. Required for non-OAuth
    # portco auth under limited networking.
    "login.salesforce.com",
    "*.salesforce.com",
    "*.my.salesforce.com",
    "quickchart.io",
    "api.compresr.com",
    "api.github.com",
    # NOTE: pypi.org and files.pythonhosted.org intentionally OMITTED — must
    # match ``bin/provision-limited-env.py:ALLOWED_HOSTS``. The provision
    # script's ``allow_package_managers=false`` blocks pip at the sandbox
    # layer; allowlisting the wheel CDN would be dead weight.
]

# Regex to grab URL-shaped strings from arbitrary tool-call content.
URL_PATTERN = re.compile(r"https?://[A-Za-z0-9._\-]+(?::\d+)?(?:/[^\s\"'<>]*)?")


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
    import anthropic  # noqa: WPS433

    return anthropic.Anthropic()


def _matches_pattern(host: str, pattern: str) -> bool:
    """Match a literal hostname against an allowlist pattern.

    Supports the wildcard form ``*.example.com``. ``api.example.com``
    matches ``*.example.com`` but ``example.com`` does not (the wildcard
    requires at least one label).
    """
    host = host.lower()
    pattern = pattern.lower()
    if pattern.startswith("*."):
        suffix = pattern[2:]
        return host.endswith("." + suffix)
    return host == pattern


def host_is_allowed(host: str, allowlist: Iterable[str]) -> bool:
    """Return True iff ``host`` matches any pattern in ``allowlist``."""
    return any(_matches_pattern(host, p) for p in allowlist)


def extract_hosts_from_text(text: str) -> set[str]:
    """Pull every URL hostname out of an arbitrary text blob."""
    out: set[str] = set()
    if not text:
        return out
    for match in URL_PATTERN.findall(text):
        try:
            parsed = urlparse(match)
            if parsed.hostname:
                out.add(parsed.hostname.lower())
        except ValueError:
            continue
    return out


def extract_hosts_from_event(event) -> set[str]:
    """Walk one session event and return every URL-shaped hostname found.

    The event shape mirrors the Anthropic SDK:
      - ``agent.custom_tool_use``: ``.input`` is a dict — flatten + scan.
      - ``user.custom_tool_result``: ``.content`` is a list of content blocks
        (each with optional ``.text``).
      - ``session.error``: ``.error.message`` may name the blocked host.
    """
    found: set[str] = set()
    etype = getattr(event, "type", "")

    if etype == "agent.custom_tool_use":
        input_data = getattr(event, "input", None) or {}
        # Stringify the whole input dict; URL regex picks up everything.
        found |= extract_hosts_from_text(str(input_data))
        # Also surface known instance_url / host / url fields directly.
        for key in ("url", "host", "instance_url", "endpoint"):
            val = input_data.get(key) if isinstance(input_data, dict) else None
            if isinstance(val, str):
                found |= extract_hosts_from_text(val)
                # Bare host (no scheme) — try parsing as URL with https://.
                if "://" not in val and "." in val:
                    found.add(val.lower())

    elif etype == "user.custom_tool_result":
        content = getattr(event, "content", None) or []
        for block in content:
            text = getattr(block, "text", "") if hasattr(block, "text") else ""
            found |= extract_hosts_from_text(text)

    elif etype == "session.error":
        error = getattr(event, "error", None)
        if error is not None:
            msg = getattr(error, "message", "") or ""
            found |= extract_hosts_from_text(msg)
            # Blocked-host errors often have the form "blocked: <host>".
            for match in re.findall(r"\b([a-z0-9._-]+\.[a-z]{2,})\b", msg.lower()):
                found.add(match)

    return found


def fetch_session_ids(days: int) -> list[str]:
    """Pull distinct session_ids from ``session_costs`` within the window.

    Returns an empty list when DATABASE_URL is unset — the caller will fail
    with exit code 2 and a clear message.
    """
    try:
        import db_adapter  # type: ignore
    except Exception as exc:
        print(f"[FATAL] could not import db_adapter: {exc}", file=sys.stderr)
        return []
    if not getattr(db_adapter, "DATABASE_URL", ""):
        print(
            "[FATAL] DATABASE_URL not set — cannot scan session_costs", file=sys.stderr
        )
        return []
    try:
        conn = db_adapter._connect()  # type: ignore[attr-defined]
    except Exception as exc:
        print(f"[FATAL] DB connect failed: {exc}", file=sys.stderr)
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT session_id FROM session_costs "
                "WHERE recorded_at >= NOW() - INTERVAL %s "
                "ORDER BY session_id",
                (f"{int(days)} days",),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows if r and r[0]]


def trace_session_hosts(session_ids: Iterable[str], client) -> set[str]:
    """Fetch events for each session and collect every observed hostname."""
    observed: set[str] = set()
    for sid in session_ids:
        try:
            events = client.beta.sessions.events.list(session_id=sid)
        except Exception as exc:
            print(f"[WARN] events.list({sid}) failed: {exc}", file=sys.stderr)
            continue
        for ev in getattr(events, "data", []):
            observed |= extract_hosts_from_event(ev)
    return observed


def diff_against_allowlist(
    observed: set[str], allowlist: list[str]
) -> tuple[list[str], list[str], list[str]]:
    """Return three sorted buckets:
        (observed_and_allowed, observed_but_not_allowed, allowed_but_not_observed)

    ``allowed_but_not_observed`` matches patterns to literal hostnames in
    ``observed``; a wildcard counts as "observed" iff any literal in
    ``observed`` matches it.
    """
    obs_allowed: list[str] = []
    obs_blocked: list[str] = []
    for host in sorted(observed):
        if host_is_allowed(host, allowlist):
            obs_allowed.append(host)
        else:
            obs_blocked.append(host)

    droppable: list[str] = []
    for pattern in allowlist:
        if not any(_matches_pattern(host, pattern) for host in observed):
            droppable.append(pattern)

    return obs_allowed, obs_blocked, droppable


def write_report(out_path: Path, observed: set[str], allowlist: list[str]) -> int:
    """Render the report file and return the exit code (0 = all clean, 1 = blocked)."""
    obs_allowed, obs_blocked, droppable = diff_against_allowlist(observed, allowlist)

    lines: list[str] = []
    lines.append("# Outbound-host trace report")
    lines.append("")
    lines.append(f"Observed distinct hostnames: {len(observed)}")
    lines.append(f"Allowlist patterns:          {len(allowlist)}")
    lines.append("")

    lines.append("## On allowlist (OK)")
    if obs_allowed:
        for h in obs_allowed:
            lines.append(f"  [OK]   {h}")
    else:
        lines.append("  (none)")
    lines.append("")

    lines.append("## NOT on allowlist (WARN — would break under limited networking)")
    if obs_blocked:
        for h in obs_blocked:
            lines.append(f"  [WARN] {h}")
    else:
        lines.append("  (none)")
    lines.append("")

    lines.append("## On allowlist but not observed (potentially droppable)")
    if droppable:
        for p in droppable:
            lines.append(f"  [DROP?] {p}")
    else:
        lines.append("  (none — every allowlist entry was hit)")
    lines.append("")

    out_path.write_text("\n".join(lines))
    print(f"[REPORT] wrote {out_path}")
    print()
    print("\n".join(lines))

    return 1 if obs_blocked else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Trace outbound hostnames from recent session events. Run BEFORE "
            "provision-limited-env.py to ground the allowlist in observed "
            "traffic. See docs/runbooks/managed-agents-conformance.md."
        )
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="How many days of session history to scan (default: 7).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=REPO_ROOT / "outbound-hosts-trace.txt",
        help="Where to write the report (default: ./outbound-hosts-trace.txt).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip the DB + API calls and print the static allowlist (CI smoke).",
    )
    args = parser.parse_args(argv)

    _load_env()

    if args.dry_run:
        print("[DRY-RUN] Static allowlist:")
        for h in EXPECTED_HOSTS:
            print(f"  {h}")
        return 0

    session_ids = fetch_session_ids(args.days)
    if not session_ids:
        print(
            "[FATAL] no session_ids found in session_costs for the window — "
            "either the DB is empty or DATABASE_URL is unset",
            file=sys.stderr,
        )
        return 2

    print(f"[SCAN] {len(session_ids)} session(s) in the last {args.days} day(s)")

    try:
        client = _build_client()
    except Exception as exc:
        print(f"[FATAL] could not build Anthropic client: {exc}", file=sys.stderr)
        return 2

    observed = trace_session_hosts(session_ids, client)
    return write_report(args.out, observed, EXPECTED_HOSTS)


if __name__ == "__main__":
    sys.exit(main())
