#!/usr/bin/env python3
"""Print the current ❌-Watcher metrics dashboard to stdout.

Phase 1, PR 7 operator-facing dashboard. Run on demand:

    python bin/watcher-metrics.py             # plain text (default)
    python bin/watcher-metrics.py --json      # machine-readable JSON
    python bin/watcher-metrics.py --window 7  # 7-day window stats (when supported)

Surfaces:

  - watcher_pending status breakdown for the last 24h
  - auto-PR rate
  - kill switch count + threshold + tripped flag

The compute_metrics function in watcher_kill_switch is the source of
truth. This script is a thin presentation layer.

Operator use:
  - Daily check: did the watcher do anything new? merge rate trending?
  - Incident triage: kill switch tripped? confirm backlog drain.
  - Pre-deploy: nothing weird in flight?
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ORCH_DIR = REPO_ROOT / "orchestrator"
sys.path.insert(0, str(ORCH_DIR))


log = logging.getLogger("watcher_metrics")


def _load_env() -> None:
    dotenv = REPO_ROOT / ".env"
    if not dotenv.exists():
        return
    for line in dotenv.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def _format_text(metrics: dict) -> str:
    lines = [
        "❌-Watcher Metrics",
        "=" * 50,
        f"Lookback window: {metrics['lookback_hours']}h",
        "",
        "Status counts (last 24h):",
    ]
    counts = metrics.get("status_counts_24h", {})
    if counts:
        for status, count in sorted(counts.items()):
            lines.append(f"  {status:<20s} {count:>5d}")
    else:
        lines.append("  (none)")
    lines.extend([
        "",
        f"Auto-PR count (last 24h):     {metrics['auto_pr_count_24h']}",
        f"Kill switch count:            {metrics['kill_switch_count']}",
        f"Kill switch threshold:        {metrics['kill_switch_threshold']}",
        f"Kill switch tripped:          {metrics['kill_switch_tripped']}",
    ])
    if metrics["kill_switch_tripped"]:
        lines.append("")
        lines.append("WARNING: kill switch is TRIPPED. Triage the backlog:")
        lines.append("  1. Review the open watcher_* PRs.")
        lines.append("  2. Merge or close each (review counts as 'sponged').")
        lines.append("  3. Next watcher tick will resume when count drops.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json", action="store_true", help="Machine-readable JSON output"
    )
    args = parser.parse_args(argv)

    _load_env()

    try:
        from watcher_kill_switch import compute_metrics
    except Exception as exc:
        print(f"ERROR: cannot import watcher_kill_switch ({exc})", file=sys.stderr)
        return 2

    try:
        metrics = compute_metrics()
    except Exception as exc:
        print(f"ERROR: compute_metrics failed ({exc})", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(metrics, indent=2, default=str))
    else:
        print(_format_text(metrics))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
