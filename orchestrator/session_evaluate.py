"""On-demand evaluator for Managed Agents sessions.

Reusable CLI used during incidents (and as a self-heal complement) to produce
a structured evaluation report for any session ID: tool-call timeline, MCP
payload sizes, sub-agent dispatches, cost estimate, and a verdict.

Unlike ``self_heal.py``, this module never writes to the memory store, never
posts to Slack, and never mutates state — it is pure read-only forensics.

Usage:
    python -m orchestrator.session_evaluate <session_id>
    python -m orchestrator.session_evaluate <session_id> --json
    python -m orchestrator.session_evaluate <session_id> --events 500

Heuristic verdicts (see ``_compute_verdicts``):
    - ``context-bloat-risk``        — input side > 750k tokens or MCP > 300 KB.
    - ``mcp-probe-hallucination``   — bash probes (which sfdx, find /, ls
                                      /var/run/, env | grep sf) indicate the
                                      agent tried to verify MCP access via the
                                      filesystem instead of a trivial call.
    - ``streaming-pattern-violated`` — large MCP payload with no openpyxl write
                                       and no nextRecordsUrl streaming.
    - ``sub-agent-dispatch-broken`` — sub-agent dispatched, but a later
                                      agent.message says the subagent "lacks
                                      Salesforce MCP" / "no SF MCP" /
                                      "subagent lacks ..."
    - ``healthy``                   — none of the above fired.

Cost estimate uses ``MODEL_COSTS_PER_MTOK`` from ``session_runner`` so the
ledger and the evaluator never drift.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import anthropic

# Load the .env file before importing config-dependent modules. Same loader
# as orchestrator/config.py and orchestrator/conftest.py — keep in sync.
from pathlib import Path as _Path  # noqa: E402

import os as _os  # noqa: E402

_dotenv_path = _Path(__file__).parent.parent / ".env"
if _dotenv_path.exists():
    for _line in _dotenv_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            _os.environ.setdefault(_k.strip(), _v.strip())

# MODEL_COSTS_PER_MTOK lives in session_runner. Importing session_runner here
# would drag in slack_bolt, MCP vault wiring, and DB adapters — heavy and
# unnecessary for forensics. Inline a small copy of the rate table and resolver
# instead. If session_runner.MODEL_COSTS_PER_MTOK changes, update both. The
# unit test imports both and pins the rates to detect drift.
MODEL_COSTS_PER_MTOK = {
    # Opus 4.5–4.8 share $5/$25 list pricing (verified 2026-05-29 vs
    # platform.claude.com). opus-4-7 corrected from stale $15/$75 (Opus-4/4.1).
    "claude-opus-4-8": {
        "input": 5.0,
        "output": 25.0,
        "cache_write_5m": 6.25,
        "cache_write_1h": 10.0,
        "cache_read": 0.5,
    },
    "claude-opus-4-7": {
        "input": 5.0,
        "output": 25.0,
        "cache_write_5m": 6.25,
        "cache_write_1h": 10.0,
        "cache_read": 0.5,
    },
    "claude-sonnet-4-6": {
        "input": 3.0,
        "output": 15.0,
        "cache_write_5m": 3.75,
        "cache_write_1h": 6.0,
        "cache_read": 0.3,
    },
    "claude-haiku-4-5": {
        "input": 0.80,
        "output": 4.0,
        "cache_write_5m": 1.0,
        "cache_write_1h": 1.6,
        "cache_read": 0.08,
    },
}

# Heuristic thresholds — exposed as module constants so the unit test can
# reason about boundary conditions without hard-coding numbers.
CONTEXT_BLOAT_INPUT_THRESHOLD = 750_000
MCP_PAYLOAD_BYTES_THRESHOLD = 300_000

# Bash probe regex patterns. Each indicates an agent tried to verify MCP
# access by inspecting the filesystem rather than calling soqlQuery — see the
# MCP diagnostic hallucination incident in CLAUDE.md (sesn_EXAMPLE).
_MCP_PROBE_PATTERNS: Tuple[re.Pattern[str], ...] = (
    re.compile(r"which\s+sfdx", re.IGNORECASE),
    re.compile(r"find\s+/.*-name\s+['\"]?\*\.sf", re.IGNORECASE),
    re.compile(r"ls\s+/var/run/?", re.IGNORECASE),
    re.compile(r"env\s*\|\s*grep\s+-i\s+sf", re.IGNORECASE),
)

# Sub-agent failure phrases. Lowercased before matching against agent.message
# text. "lacks salesforce mcp" is what the Coordinator says when it incorrectly
# concludes a specialist can't reach SF; "no sf mcp" and "subagent lacks" are
# common variants seen in production transcripts.
_SUBAGENT_FAILURE_PHRASES: Tuple[str, ...] = (
    "lacks salesforce mcp",
    "no sf mcp",
    "subagent lacks",
)

# nextRecordsUrl substring — appears when an agent properly paginates a large
# SOQL result via Salesforce's REST cursor. Detected in bash tool inputs so
# we can tell streaming-pattern violations from legitimate large-result runs.
_STREAMING_TOKENS = ("nextRecordsUrl", "openpyxl")

log = logging.getLogger(__name__)


def _client() -> anthropic.Anthropic:
    """Build an Anthropic client. Separated for unit-test patching."""
    api_key = _os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Add it to .env or the environment."
        )
    return anthropic.Anthropic(api_key=api_key)


def _extract_model(session: Any) -> Optional[str]:
    """Pull the canonical model ID off a session, tolerating shape variation.

    The Managed Agents API exposes model info at ``session.agent.model``,
    which (as of 2026-05-11) is a ``BetaManagedAgentsModelConfig`` with an
    ``.id`` field (e.g. ``"claude-opus-4-8"``). Older or future shapes might
    use a plain string at ``session.model`` or ``session.agent.model``. Return
    ``None`` if nothing parseable is found — the cost resolver tolerates that.
    """
    direct = getattr(session, "model", None)
    if isinstance(direct, str) and direct:
        return direct
    if direct is not None:
        # Could be a config object even on session.model in some shapes.
        sub = getattr(direct, "id", None)
        if isinstance(sub, str) and sub:
            return sub
    agent_obj = getattr(session, "agent", None)
    if agent_obj is None:
        return None
    agent_model = getattr(agent_obj, "model", None)
    if isinstance(agent_model, str) and agent_model:
        return agent_model
    if agent_model is not None:
        sub = getattr(agent_model, "id", None)
        if isinstance(sub, str) and sub:
            return sub
    return None


def _resolve_model_rates(model_hint: Optional[str]) -> Dict[str, float]:
    """Look up cost rates with longest-prefix matching for dated model IDs.

    Mirrors ``session_runner._resolve_model_rates``. Pulled in here so this
    CLI has no hard dependency on session_runner's import graph. Falls back to
    Opus rates by default because the Coordinator is on Opus and is the most
    common subject of on-demand audits — a more conservative default for
    incident triage than Sonnet.
    """
    default = MODEL_COSTS_PER_MTOK["claude-opus-4-8"]
    if not model_hint:
        return default
    if model_hint in MODEL_COSTS_PER_MTOK:
        return MODEL_COSTS_PER_MTOK[model_hint]
    candidates = sorted(
        (k for k in MODEL_COSTS_PER_MTOK if model_hint.startswith(k)),
        key=len,
        reverse=True,
    )
    if candidates:
        return MODEL_COSTS_PER_MTOK[candidates[0]]
    return default


def _extract_usage_parts(usage: Any) -> Dict[str, int]:
    """Pull the five token categories out of a session usage object.

    Mirrors ``session_runner._extract_usage_parts``. Tolerates ``None`` so
    the CLI can run against in-flight sessions whose usage hasn't materialized.
    """
    if usage is None:
        return {
            "input": 0,
            "output": 0,
            "cache_read": 0,
            "cache_write_5m": 0,
            "cache_write_1h": 0,
        }
    input_tok = getattr(usage, "input_tokens", 0) or 0
    output_tok = getattr(usage, "output_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cw5, cw1 = 0, 0
    cc = getattr(usage, "cache_creation", None)
    if cc is not None:
        cw5 = getattr(cc, "ephemeral_5m_input_tokens", 0) or 0
        cw1 = getattr(cc, "ephemeral_1h_input_tokens", 0) or 0
    return {
        "input": input_tok,
        "output": output_tok,
        "cache_read": cache_read,
        "cache_write_5m": cw5,
        "cache_write_1h": cw1,
    }


def _estimate_cost(usage: Any, model_hint: Optional[str]) -> float:
    """Estimate cost in dollars. Mirrors session_runner._estimate_cost."""
    rates = _resolve_model_rates(model_hint)
    u = _extract_usage_parts(usage)
    return (
        u["input"] * rates["input"] / 1_000_000
        + u["output"] * rates["output"] / 1_000_000
        + u["cache_read"] * rates["cache_read"] / 1_000_000
        + u["cache_write_5m"] * rates["cache_write_5m"] / 1_000_000
        + u["cache_write_1h"] * rates["cache_write_1h"] / 1_000_000
    )


def _parse_dt(value: Any) -> Optional[datetime]:
    """Coerce an Anthropic API datetime field into a timezone-aware datetime.

    The SDK returns timestamps as datetime objects already; raw HTTP responses
    sometimes give ISO strings. Both paths normalize to UTC. Returns ``None``
    when the value is missing or unparseable — callers must tolerate that.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            # Accept both "Z" and "+00:00" suffixes.
            normalized = value.replace("Z", "+00:00")
            return datetime.fromisoformat(normalized)
        except ValueError:
            return None
    return None


def _age_minutes(created_at: Any, updated_at: Any) -> Optional[float]:
    """Compute updated_at - created_at in minutes."""
    c = _parse_dt(created_at)
    u = _parse_dt(updated_at)
    if c is None or u is None:
        return None
    return round((u - c).total_seconds() / 60.0, 2)


def _block_text(content_blocks: Iterable[Any]) -> str:
    """Concatenate the .text fields of an event's content blocks."""
    parts: List[str] = []
    for b in content_blocks or []:
        text = getattr(b, "text", None)
        if text:
            parts.append(text)
    return "".join(parts)


def _tool_kind(event_type: str) -> str:
    """Classify an event_type into a tool kind. ``builtin`` covers bash/file/etc.

    ``agent.custom_tool_use``   → custom (orchestrator-defined: send_slack_*,
                                   generate_chart, db_query, save_snapshot_batch).
    ``agent.mcp_tool_use``      → mcp    (soqlQuery, describeSObject, etc.).
    Everything else (e.g. ``agent.tool_use`` for bash) → builtin.
    """
    if event_type == "agent.custom_tool_use":
        return "custom"
    if event_type == "agent.mcp_tool_use":
        return "mcp"
    return "builtin"


def _input_preview(raw_input: Any, limit: int = 200) -> str:
    """Render a tool's input as a short preview string for the timeline.

    JSON-serializes dicts/lists so the preview captures structure, not just
    type. Strings pass through. Anything else gets ``repr``'d. Always truncated
    to ``limit`` chars.
    """
    if raw_input is None:
        return ""
    if isinstance(raw_input, str):
        text = raw_input
    elif isinstance(raw_input, (dict, list)):
        try:
            text = json.dumps(raw_input, default=str)
        except (TypeError, ValueError):
            text = repr(raw_input)
    else:
        text = repr(raw_input)
    return text[:limit]


def _is_mcp_probe(bash_command: str) -> bool:
    """True if a bash command matches any MCP-diagnostic-hallucination pattern."""
    if not bash_command:
        return False
    return any(p.search(bash_command) for p in _MCP_PROBE_PATTERNS)


def _is_streaming_evidence(bash_command: str) -> bool:
    """True if a bash command references openpyxl writes or nextRecordsUrl."""
    if not bash_command:
        return False
    return any(token in bash_command for token in _STREAMING_TOKENS)


def _looks_like_bash(event: Any) -> Tuple[bool, str]:
    """Detect a bash tool call and return (is_bash, command_string).

    Managed Agents emits builtin tool calls as ``agent.tool_use`` with
    ``name == "bash"`` (or sometimes ``"shell"``). The command lives at
    ``event.input.command`` or directly at ``event.input`` if the SDK shape
    has shifted. Treat both gracefully.
    """
    name = (getattr(event, "name", "") or "").lower()
    if name not in ("bash", "shell"):
        return False, ""
    raw = getattr(event, "input", None)
    if isinstance(raw, dict):
        cmd = raw.get("command") or raw.get("cmd") or ""
        return True, str(cmd)
    if isinstance(raw, str):
        return True, raw
    return True, ""


def _compute_verdicts(
    *,
    total_input_side: int,
    mcp_payload_bytes: int,
    bash_commands: List[str],
    sub_agent_dispatches: int,
    agent_messages_after_dispatch: List[str],
) -> List[str]:
    """Apply the four heuristic checks and return matched verdicts.

    Returns ``["healthy"]`` when no heuristic fires. The four checks are
    independent — a session can hit multiple verdicts simultaneously (e.g. a
    context-bloat run that also has an MCP probe). Order is stable for
    test/snapshot determinism: bloat → probe → streaming → dispatch.
    """
    verdicts: List[str] = []

    if (
        total_input_side > CONTEXT_BLOAT_INPUT_THRESHOLD
        or mcp_payload_bytes > MCP_PAYLOAD_BYTES_THRESHOLD
    ):
        verdicts.append("context-bloat-risk")

    if any(_is_mcp_probe(cmd) for cmd in bash_commands):
        verdicts.append("mcp-probe-hallucination")

    if mcp_payload_bytes > MCP_PAYLOAD_BYTES_THRESHOLD:
        any_streaming_evidence = any(
            _is_streaming_evidence(cmd) for cmd in bash_commands
        )
        if not any_streaming_evidence:
            verdicts.append("streaming-pattern-violated")

    if sub_agent_dispatches > 0:
        joined = " ".join(m.lower() for m in agent_messages_after_dispatch)
        if any(phrase in joined for phrase in _SUBAGENT_FAILURE_PHRASES):
            verdicts.append("sub-agent-dispatch-broken")

    if not verdicts:
        verdicts.append("healthy")
    return verdicts


def evaluate_session(
    session_id: str,
    *,
    client: Optional[anthropic.Anthropic] = None,
    events_limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Return a structured evaluation report for ``session_id``.

    Read-only. Pulls ``client.beta.sessions.retrieve(session_id)`` and
    ``client.beta.sessions.events.list(session_id=...)`` exactly once each.
    When ``events_limit`` is provided, only the first N events are walked
    (useful for very large sessions where the user wants a fast sample).

    See the module docstring for the full output schema.
    """
    c = client or _client()

    session = c.beta.sessions.retrieve(session_id)
    events_obj = c.beta.sessions.events.list(session_id=session_id)
    events_data = getattr(events_obj, "data", []) or []
    if events_limit is not None and events_limit > 0:
        events_data = list(events_data)[:events_limit]

    # Session-level fields.
    created_at = getattr(session, "created_at", None)
    updated_at = getattr(session, "updated_at", None)
    archived_at = getattr(session, "archived_at", None)
    usage = getattr(session, "usage", None)

    # Model extraction. The Managed Agents session shape does NOT expose a
    # top-level ``model`` field — it lives at session.agent.model (a
    # ``BetaManagedAgentsModelConfig`` with .id and .speed) or, in older shapes,
    # as a plain string. Handle all three shapes:
    #   1. session.model is a string  → use directly.
    #   2. session.agent.model is a BetaManagedAgentsModelConfig with .id.
    #   3. session.agent.model is itself a string.
    # The .id form is what we see in production (2026-05-11).
    model = _extract_model(session)

    usage_parts = _extract_usage_parts(usage)
    total_input_side = (
        usage_parts["input"]
        + usage_parts["cache_read"]
        + usage_parts["cache_write_5m"]
        + usage_parts["cache_write_1h"]
    )
    estimated_cost = _estimate_cost(usage, model)

    # Walk events once. Each branch updates one or more of the running
    # accumulators below — keep the walk single-pass so large sessions don't
    # blow out memory on repeated iteration.
    event_summary: Dict[str, int] = {}
    tool_calls: List[Dict[str, Any]] = []
    mcp_payload_bytes = 0
    largest_mcp_response = 0
    sub_agent_dispatches = 0
    slack_posts = 0
    bash_commands: List[str] = []
    sub_agent_seen = False
    agent_messages_after_dispatch: List[str] = []

    for e in events_data:
        etype = getattr(e, "type", "unknown")
        event_summary[etype] = event_summary.get(etype, 0) + 1

        if etype in ("agent.custom_tool_use", "agent.mcp_tool_use", "agent.tool_use"):
            name = getattr(e, "name", "") or ""
            raw_input = getattr(e, "input", None)
            tool_calls.append(
                {
                    "name": name,
                    "kind": _tool_kind(etype),
                    "input_preview": _input_preview(raw_input),
                }
            )
            if etype == "agent.custom_tool_use" and name == "send_slack_notification":
                slack_posts += 1
            is_bash, cmd = _looks_like_bash(e)
            if is_bash:
                bash_commands.append(cmd)

        elif etype == "agent.mcp_tool_result":
            text = _block_text(getattr(e, "content", None) or [])
            n = len(text)
            mcp_payload_bytes += n
            if n > largest_mcp_response:
                largest_mcp_response = n

        elif etype in ("agent.thread_message_sent", "agent.sub_agent_use"):
            # Sub-agent dispatch. In the Managed Agents API today (2026-05-11)
            # the Coordinator hands work to a specialist via
            # ``agent.thread_message_sent`` carrying a ``to_agent_name`` field;
            # ``agent.sub_agent_use`` is kept as a forward-compat alias in case
            # the SDK renames the event.
            sub_agent_dispatches += 1
            sub_agent_seen = True

        elif etype == "agent.message" and sub_agent_seen:
            text = _block_text(getattr(e, "content", None) or [])
            if text:
                agent_messages_after_dispatch.append(text)

    verdicts = _compute_verdicts(
        total_input_side=total_input_side,
        mcp_payload_bytes=mcp_payload_bytes,
        bash_commands=bash_commands,
        sub_agent_dispatches=sub_agent_dispatches,
        agent_messages_after_dispatch=agent_messages_after_dispatch,
    )

    return {
        "session_id": session_id,
        "created_at": _parse_dt(created_at).isoformat()
        if _parse_dt(created_at)
        else None,
        "updated_at": _parse_dt(updated_at).isoformat()
        if _parse_dt(updated_at)
        else None,
        "age_minutes": _age_minutes(created_at, updated_at),
        "archived": archived_at is not None,
        "model": model,
        "usage": {
            "cache_creation_5m": usage_parts["cache_write_5m"],
            "cache_creation_1h": usage_parts["cache_write_1h"],
            "cache_read": usage_parts["cache_read"],
            "input": usage_parts["input"],
            "output": usage_parts["output"],
            "total_input_side": total_input_side,
        },
        "estimated_cost_usd": round(estimated_cost, 6),
        "event_summary": event_summary,
        "tool_calls": tool_calls,
        "mcp_payload_bytes": mcp_payload_bytes,
        "largest_mcp_response_bytes": largest_mcp_response,
        "sub_agent_dispatches": sub_agent_dispatches,
        "slack_posts": slack_posts,
        "verdicts": verdicts,
    }


def _format_report(report: Dict[str, Any]) -> str:
    """Pretty-print a report for terminal output (non-JSON mode)."""
    lines: List[str] = []
    lines.append(f"Session:   {report['session_id']}")
    lines.append(f"Model:     {report.get('model') or 'unknown'}")
    lines.append(f"Created:   {report.get('created_at') or 'unknown'}")
    lines.append(f"Updated:   {report.get('updated_at') or 'unknown'}")
    age = report.get("age_minutes")
    age_str = f"{age:.2f} min" if age is not None else "unknown"
    lines.append(f"Age:       {age_str}")
    lines.append(f"Archived:  {report.get('archived')}")
    lines.append("")

    u = report["usage"]
    lines.append("Usage (tokens):")
    lines.append(f"  input              {u['input']:>14,}")
    lines.append(f"  output             {u['output']:>14,}")
    lines.append(f"  cache_read         {u['cache_read']:>14,}")
    lines.append(f"  cache_creation_5m  {u['cache_creation_5m']:>14,}")
    lines.append(f"  cache_creation_1h  {u['cache_creation_1h']:>14,}")
    lines.append(f"  total_input_side   {u['total_input_side']:>14,}")
    lines.append(f"Estimated cost: ${report['estimated_cost_usd']:.4f}")
    lines.append("")

    lines.append(f"MCP payload bytes:           {report['mcp_payload_bytes']:,}")
    lines.append(
        f"Largest MCP response bytes:  {report['largest_mcp_response_bytes']:,}"
    )
    lines.append(f"Sub-agent dispatches:        {report['sub_agent_dispatches']}")
    lines.append(f"Slack posts:                 {report['slack_posts']}")
    lines.append("")

    lines.append("Event summary:")
    if report["event_summary"]:
        for etype, count in sorted(
            report["event_summary"].items(), key=lambda kv: (-kv[1], kv[0])
        ):
            lines.append(f"  {etype:<40} {count:>6}")
    else:
        lines.append("  (no events)")
    lines.append("")

    lines.append(f"Tool calls ({len(report['tool_calls'])}):")
    if report["tool_calls"]:
        for tc in report["tool_calls"][:50]:
            preview = tc["input_preview"].replace("\n", " ")
            lines.append(f"  [{tc['kind']:<7}] {tc['name']:<28} {preview[:120]}")
        if len(report["tool_calls"]) > 50:
            lines.append(f"  ... (+{len(report['tool_calls']) - 50} more)")
    else:
        lines.append("  (none)")
    lines.append("")

    lines.append(f"Verdicts: {', '.join(report['verdicts'])}")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point. Returns a Unix exit code."""
    parser = argparse.ArgumentParser(
        prog="python -m orchestrator.session_evaluate",
        description=(
            "Evaluate a Managed Agents session and emit a structured "
            "report (tool timeline, MCP payload sizes, sub-agent dispatches, "
            "cost, verdicts)."
        ),
    )
    parser.add_argument("session_id", help="Session ID, e.g. sesn_EXAMPLE...")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the report as JSON to stdout (for piping to jq).",
    )
    parser.add_argument(
        "--events",
        type=int,
        default=None,
        help="Walk only the first N events (useful for very large sessions).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    try:
        report = evaluate_session(args.session_id, events_limit=args.events)
    except anthropic.APIError as e:
        print(f"Anthropic API error: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"Failed to evaluate session: {e}", file=sys.stderr)
        return 1

    if args.json:
        json.dump(report, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
    else:
        print(_format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
