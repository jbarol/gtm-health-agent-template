#!/usr/bin/env python3
"""Live probe of the Kapa REST custom tool.

Why this exists
---------------
A 2026-05-14 production demo failed because a sub-agent cited a stale memory
note ("Kapa down with HTTP 406") instead of probing the live endpoint. The
note recorded the symptom of an ``Accept: text/event-stream`` request that
the streaming endpoint rejected; PR #167 already migrated the runtime to
``Accept: application/json``. The memory file was never invalidated, so
agents kept citing it.

This script is the live probe that proves the current contract works (or
captures the new contract break if it doesn't). It MUST exercise the same
code path the orchestrator runs at session time — that is the whole point.
We import ``search_kapa`` from ``orchestrator/kapa_rest_tool.py`` so any
divergence between probe and runtime is impossible by construction.

Usage
-----
    # Local dev — reads .env automatically (via the same dotenv parser
    # the orchestrator config.py uses):
    python bin/probe_kapa.py

    # Custom query:
    python bin/probe_kapa.py --query "What integrates with Acme?"

    # CI / container:
    KAPA_ACME_API_KEY=... KAPA_ACME_PROJECT_ID=... \
        python bin/probe_kapa.py

Exit codes
----------
    0 — PASS. Stream returned non-empty content; first 200 chars printed.
    1 — FAIL. Either pre-stream HTTP error, network failure, or the stream
        completed with empty content. Status code and body printed.
    2 — Misconfiguration (missing API key or project id).

Output is intentionally human-readable. The companion test
``orchestrator/kapa_rest_tool_live_probe_test.py`` (env-gated) re-uses
``run_probe`` so the same logic exists in exactly one place.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Make ``orchestrator/`` importable so we can reuse the production code path
# without duplicating the streaming consumer. Mirrors what the orchestrator
# does at runtime (it runs from inside that directory).
sys.path.insert(0, str(REPO_ROOT / "orchestrator"))


def _find_dotenv() -> Path | None:
    """Locate the project ``.env`` file.

    Walks up from this script's parent looking for ``.env``. This handles
    the common worktree case where the probe sits in
    ``<repo>/.claude/worktrees/<id>/bin/probe_kapa.py`` but the canonical
    ``.env`` lives at the main checkout ``<repo>/.env``. Walks at most 6
    levels — beyond that we'd be poking at the filesystem root.
    """
    # First, try the obvious case: ``<REPO_ROOT>/.env`` next to ``orchestrator/``.
    here = REPO_ROOT / ".env"
    if here.exists():
        return here
    # Walk up looking for either a ``.env`` file directly, or the marker
    # of a sibling checkout (``orchestrator/`` directory + ``.env`` peer).
    cur = REPO_ROOT
    for _ in range(6):
        candidate = cur / ".env"
        if candidate.exists() and (cur / "orchestrator").is_dir():
            return candidate
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def _load_dotenv() -> None:
    """Match the manual dotenv parsing in ``orchestrator/config.py``.

    Importing config.py directly would crash with a RuntimeError when
    optional vars (SLACK_BOT_TOKEN, ENVIRONMENT_ID, etc.) are unset on
    the local machine — the probe doesn't need those. So we replicate
    just the .env-loading slice here.
    """
    dotenv_path = _find_dotenv()
    if dotenv_path is None:
        return
    for line in dotenv_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


@dataclass
class ProbeResult:
    """Structured outcome the test can assert against."""

    ok: bool
    content: str
    error: str
    detail: str
    elapsed_s: float
    source_count: int
    http_status: int


def run_probe(query: str = "What is FATI?") -> ProbeResult:
    """Execute a single Kapa round-trip via the production code path.

    Loads .env, then calls ``kapa_rest_tool.search_kapa`` with the same
    headers, project id, and timeout the orchestrator uses. Returns a
    ``ProbeResult`` regardless of outcome; never raises.

    The query defaults to "What is FATI?" — a Acme term Kapa's
    index has dense coverage on, so a clean PASS should return real
    content rather than an "I don't know" answer.
    """
    _load_dotenv()

    api_key = os.environ.get("KAPA_ACME_API_KEY", "").strip()
    project_id = os.environ.get("KAPA_ACME_PROJECT_ID", "").strip()

    if not api_key:
        return ProbeResult(
            ok=False,
            content="",
            error="missing_api_key",
            detail="KAPA_ACME_API_KEY env var unset (also checked .env)",
            elapsed_s=0.0,
            source_count=0,
            http_status=0,
        )
    if not project_id:
        return ProbeResult(
            ok=False,
            content="",
            error="missing_project_id",
            detail="KAPA_ACME_PROJECT_ID env var unset (also checked .env)",
            elapsed_s=0.0,
            source_count=0,
            http_status=0,
        )

    # Import here, AFTER sys.path is set. Re-using the production
    # streaming consumer guarantees probe == runtime.
    from kapa_rest_tool import search_kapa

    raw = search_kapa(query=query, api_key=api_key, project_id=project_id)
    return ProbeResult(
        ok=bool(raw.get("ok")),
        content=str(raw.get("content", "")),
        error=str(raw.get("error", "")),
        detail=str(raw.get("detail", "")),
        elapsed_s=float(raw.get("elapsed_s", 0.0)),
        source_count=int(raw.get("source_count", 0)),
        http_status=int(raw.get("http_status", 0)),
    )


def _format_pass(result: ProbeResult) -> str:
    preview = result.content[:200].replace("\n", " ")
    if len(result.content) > 200:
        preview += "..."
    return (
        "PASS — Kapa /chat/stream/ operational\n"
        f"  elapsed_s:     {result.elapsed_s}\n"
        f"  http_status:   {result.http_status}\n"
        f"  source_count:  {result.source_count}\n"
        f"  content_chars: {len(result.content)}\n"
        f"  preview:       {preview}\n"
    )


def _format_fail(result: ProbeResult) -> str:
    return (
        "FAIL — Kapa probe returned non-OK result\n"
        f"  error:       {result.error}\n"
        f"  detail:      {result.detail[:500]}\n"
        f"  elapsed_s:   {result.elapsed_s}\n"
        f"  http_status: {result.http_status}\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--query",
        default="What is FATI?",
        help="Natural-language question to send to Kapa (default: 'What is FATI?')",
    )
    args = parser.parse_args(argv)

    result = run_probe(query=args.query)

    if result.error in ("missing_api_key", "missing_project_id"):
        sys.stderr.write(_format_fail(result))
        return 2

    if result.ok and result.content.strip():
        sys.stdout.write(_format_pass(result))
        return 0

    sys.stderr.write(_format_fail(result))
    return 1


if __name__ == "__main__":
    sys.exit(main())
