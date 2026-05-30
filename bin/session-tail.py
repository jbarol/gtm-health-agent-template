#!/usr/bin/env python3
"""Tail a live Managed Agents session — poll usage + events on an interval.

When to use:
    During incidents, while watching an investigation the orchestrator
    spawned for a Slack thread, or whenever a session ID needs eyes-on
    visibility outside the in-process ``session_watch`` canary. Complements
    ``session_evaluate.py`` (which is one-shot forensics): this script keeps
    polling and prints a one-line summary per tick plus any newly observed
    events since the previous tick.

    The default stop condition is ONLY ``archived_at`` flipping or the operator
    sending SIGINT. The script intentionally does NOT auto-stop on the 1M
    input-token cap — past that boundary the session can sit in a stalled
    state for minutes (model output frozen, custom-tool result undispatchable,
    no archive yet), and that stalled state is exactly what the operator needs
    to see. Cap-crossings are flagged in the output but never terminate the
    watch.

Usage:
    python bin/session-tail.py <session_id>
    python bin/session-tail.py <session_id> --interval 10 --max-ticks 200
    python bin/session-tail.py <session_id> --quiet         # tick-only, no events
    python bin/session-tail.py <session_id> --no-events     # same; --quiet alias

Read-only. No Slack side effects, no state mutation, no archive. Mirrors the
shape of ``orchestrator/session_evaluate.py`` so the two tools share mental
model and helpers.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_env() -> None:
    """Manual dotenv loader matching ``orchestrator/config.py``."""
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
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Add it to .env or the environment."
        )
    return anthropic.Anthropic(api_key=api_key)


WATCH_THRESHOLD = 750_000
IMMINENT_THRESHOLD = 950_000
CAP = 1_000_000

# Event types that are far too noisy to print one-per-line during a tail.
# session_runner emits one span pair per model call; ``agent.thinking`` /
# ``agent.tool_result`` events come in pairs with the ``agent.tool_use`` we
# already render. Hide them by default; ``--verbose`` flips them back on.
_DEFAULT_SUPPRESSED = frozenset(
    {
        "span.model_request_start",
        "span.model_request_end",
        "session.status_running",
        "session.thread_status_running",
        "session.thread_status_idle",
        "agent.tool_result",
        "user.custom_tool_result",
        "agent.thinking",
    }
)


def _now_local() -> str:
    return datetime.now().astimezone().strftime("%H:%M:%S")


def _compute_input_side(usage: Any) -> int:
    """Mirror ``session_watch._compute_input_side``."""
    if usage is None:
        return 0
    try:
        input_tok = getattr(usage, "input_tokens", 0) or 0
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cc = getattr(usage, "cache_creation", None)
        cache_write_5m = 0
        if cc is not None:
            cache_write_5m = getattr(cc, "ephemeral_5m_input_tokens", 0) or 0
        return int(input_tok) + int(cache_read) + int(cache_write_5m)
    except Exception:
        return 0


def _output_tokens(usage: Any) -> int:
    if usage is None:
        return 0
    try:
        return int(getattr(usage, "output_tokens", 0) or 0)
    except Exception:
        return 0


def _severity_marker(input_side: int) -> str:
    """Return a single-token marker indicating threshold band."""
    if input_side >= CAP:
        return ":skull:"
    if input_side >= IMMINENT_THRESHOLD:
        return ":rotating_light:"
    if input_side >= WATCH_THRESHOLD:
        return ":warning:"
    return ""


def _describe_event(ev: Any) -> str:
    """One-line human description of an event. Tolerates missing fields."""
    t = getattr(ev, "type", "?")
    if t == "agent.tool_use":
        return f"tool_use {getattr(ev, 'name', '?')}"
    if t == "agent.custom_tool_use":
        nm = getattr(ev, "name", "?")
        ti = getattr(ev, "input", {}) or {}
        if not isinstance(ti, dict):
            ti = {}
        lbl = ti.get("label")
        q = ti.get("query")
        extra = ""
        if lbl:
            extra = f" label={lbl}"
        elif q:
            extra = f" query={str(q)[:60]!r}"
        return f"custom_tool_use {nm}{extra}"
    if t == "agent.thread_message_sent":
        return f"sub_dispatch agent_id={getattr(ev, 'agent_id', None)}"
    if t == "agent.message":
        cont = getattr(ev, "content", None) or []
        if cont and hasattr(cont[0], "text"):
            return f"message {cont[0].text[:160]!r}"
        return "message"
    if t == "session.status_idle":
        return f"idle stop={getattr(ev, 'stop_reason', None)}"
    return t


def _event_id(ev: Any) -> str:
    """Stable de-dup key. Prefer ``ev.id``; fall back to type + created_at."""
    eid = getattr(ev, "id", None)
    if eid:
        return str(eid)
    return f"{getattr(ev, 'type', '')}-{getattr(ev, 'created_at', '')}"


def tick(
    client: Any,
    session_id: str,
    i: int,
    seen_event_ids: set[str],
    *,
    show_events: bool = True,
    verbose: bool = False,
    suppressed: frozenset[str] = _DEFAULT_SUPPRESSED,
    out=sys.stdout,
) -> tuple[bool, int]:
    """Run one polling tick. Returns ``(archived, input_side)``.

    Never raises — exceptions are caught, logged to ``out``, and the call
    returns ``(False, 0)``. The loop is non-critical: a transient HTTP error
    or schema drift on the SDK side should produce a noisy line, not crash.
    """
    try:
        s = client.beta.sessions.retrieve(session_id)
    except Exception as e:
        print(
            f"[{_now_local()}] tick={i:02d} retrieve failed: {e}", file=out, flush=True
        )
        return False, 0

    archived = getattr(s, "archived_at", None) is not None
    created_at = getattr(s, "created_at", None)
    if created_at is not None:
        try:
            age_min = (datetime.now(timezone.utc) - created_at).total_seconds() / 60.0
        except Exception:
            age_min = 0.0
    else:
        age_min = 0.0
    input_side = _compute_input_side(getattr(s, "usage", None))
    output = _output_tokens(getattr(s, "usage", None))
    sev = _severity_marker(input_side)
    sev_str = f" {sev}" if sev else ""
    pct = input_side / 10_000  # one-decimal percent of 1M
    print(
        f"[{_now_local()}] tick={i:02d} age={age_min:5.2f}m "
        f"input_side={input_side:>9,} ({pct:.1f}%) "
        f"output={output:>6,} archived={archived}{sev_str}",
        file=out,
        flush=True,
    )

    if show_events:
        try:
            evs = client.beta.sessions.events.list(session_id=session_id, limit=50)
            new = []
            for e in getattr(evs, "data", None) or []:
                eid = _event_id(e)
                if eid not in seen_event_ids:
                    seen_event_ids.add(eid)
                    new.append(e)
            # API returns newest-first; flip to oldest-first for human reading.
            for e in reversed(new):
                t = getattr(e, "type", "?")
                if not verbose and t in suppressed:
                    continue
                print(f"           ev: {_describe_event(e)}", file=out, flush=True)
        except Exception as e:
            print(f"           events.list failed: {e}", file=out, flush=True)

    return archived, input_side


def watch(
    client: Any,
    session_id: str,
    *,
    interval: int = 15,
    max_ticks: int = 120,
    show_events: bool = True,
    verbose: bool = False,
    sleeper=time.sleep,
    out=sys.stdout,
) -> dict:
    """Poll loop. Returns a small summary dict for callers / tests."""
    seen: set[str] = set()
    print(
        f"[{_now_local()}] starting watch on {session_id} "
        f"(interval={interval}s, max_ticks={max_ticks})",
        file=out,
        flush=True,
    )
    ticks_done = 0
    final_input_side = 0
    archived = False
    for i in range(1, max_ticks + 1):
        ticks_done = i
        try:
            archived, final_input_side = tick(
                client,
                session_id,
                i,
                seen,
                show_events=show_events,
                verbose=verbose,
                out=out,
            )
        except Exception:
            traceback.print_exc(file=out)
        if archived:
            print(
                f"[{_now_local()}] session archived — stopping watch",
                file=out,
                flush=True,
            )
            return {
                "ticks": ticks_done,
                "archived": True,
                "final_input_side": final_input_side,
                "reason": "archived",
            }
        # NOTE: we deliberately do NOT stop on input_side >= 1M. Past the cap
        # a session frequently stalls (no archive, no further model output)
        # and that stalled state is exactly what the operator wants to see.
        # The :skull: marker in the tick line is the visible signal.
        if i < max_ticks:
            sleeper(interval)
    print(f"[{_now_local()}] max_ticks reached — stopping watch", file=out, flush=True)
    return {
        "ticks": ticks_done,
        "archived": False,
        "final_input_side": final_input_side,
        "reason": "max_ticks",
    }


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Tail a live Managed Agents session. Read-only; prints usage + "
            "events on a polling interval. Stops only on archive, SIGINT, "
            "or --max-ticks."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("session_id", help="Anthropic session ID (sesn_...)")
    p.add_argument(
        "--interval",
        type=int,
        default=15,
        help="Seconds between polls.",
    )
    p.add_argument(
        "--max-ticks",
        type=int,
        default=120,
        help="Stop after this many ticks (default ≈ 30 min at 15s interval).",
    )
    p.add_argument(
        "--no-events",
        action="store_true",
        help="Skip the per-event listing. Only print the tick summary line.",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="Alias for --no-events.",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Include normally-suppressed events (spans, status, thinking).",
    )
    return p


def _install_sigint_handler() -> None:
    """Make Ctrl-C exit cleanly with a final newline so the prompt isn't clobbered."""

    def _handler(*_args):  # (signum, frame) — required by signal module
        print(f"\n[{_now_local()}] SIGINT — stopping watch", flush=True)
        sys.exit(130)

    try:
        signal.signal(signal.SIGINT, _handler)
    except Exception:
        # Signal handling not available in some environments (e.g. embedded);
        # the default KeyboardInterrupt traceback is acceptable there.
        pass


def main(argv: Optional[list[str]] = None) -> int:
    _load_env()
    args = _build_parser().parse_args(argv)
    _install_sigint_handler()
    client = _build_client()
    show_events = not (args.no_events or args.quiet)
    result = watch(
        client,
        args.session_id,
        interval=args.interval,
        max_ticks=args.max_ticks,
        show_events=show_events,
        verbose=args.verbose,
    )
    return 0 if result.get("archived") else 0


if __name__ == "__main__":
    raise SystemExit(main())
