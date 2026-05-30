"""Compute layer for the persistent-state surface (Plan #33).

`compute_surface(portco)` returns a `SurfaceState` populated from the per-portco
memory store at `/mnt/memory/gtm-health-memory/{portco}/`. F7 fills in the
readers F4 stubbed: findings, recent decisions, open questions, trajectory.

Memory-store layout (post-F7):

    /mnt/memory/gtm-health-memory/{portco}/metrics.md
    /mnt/memory/gtm-health-memory/{portco}/findings.md
    /mnt/memory/gtm-health-memory/{portco}/resolved.md
    /mnt/memory/gtm-health-memory/{portco}/decisions.md
    /mnt/memory/gtm-health-memory/{portco}/open_questions.md

Each file is a sequence of YAML-front-matter blocks followed by a body
paragraph. See `docs/surface/memory-store-format.md` for the full spec,
example entries per file, and migration notes.

A missing file yields an empty list. A malformed YAML block is logged at
debug and skipped — never raises. Pre-front-matter loose markdown (the
historical format) parses to an empty list, which is the migration story:
no hard cutover, agents rewrite files entry-by-entry.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from surface_schemas import (
    CostBlock,
    DecisionRow,
    FindingRow,
    KeyMetricRow,
    OpenQuestionRow,
    SurfaceState,
    TrajectoryBlock,
)

logger = logging.getLogger(__name__)

# Module-level so tests can monkeypatch with a tmp dir.
MEMORY_STORE_ROOT = Path("/mnt/memory/gtm-health-memory")


# ---------------------------------------------------------------------------
# Block parser
# ---------------------------------------------------------------------------


def _read_file(path: Path) -> str | None:
    """Read a memory-store file, returning None on missing/unreadable."""
    try:
        return path.read_text()
    except (OSError, FileNotFoundError):
        return None


def _parse_blocks(text: str) -> list[tuple[dict[str, Any], str]]:
    """Split a memory-store file into (yaml_dict, body) pairs.

    Blocks are delimited by `---` lines. Returns one tuple per valid YAML
    block. Malformed YAML is logged at debug and dropped. Pre-front-matter
    preamble (anything before the first `---`) is ignored.
    """
    lines = text.splitlines()
    # Find every line that is exactly `---` (after strip).
    delim_indices = [i for i, line in enumerate(lines) if line.strip() == "---"]
    if len(delim_indices) < 2:
        return []

    # YAML blocks are between pairs of `---` lines: (open, close).
    # Bodies are between close[k] and open[k+1] (or EOF).
    blocks: list[tuple[dict[str, Any], str]] = []
    i = 0
    while i + 1 < len(delim_indices):
        open_idx = delim_indices[i]
        close_idx = delim_indices[i + 1]
        yaml_text = "\n".join(lines[open_idx + 1 : close_idx])
        try:
            parsed = yaml.safe_load(yaml_text)
        except yaml.YAMLError as exc:
            logger.debug("YAML parse error: %s", exc)
            i += 2
            continue
        if not isinstance(parsed, dict):
            # Empty block or scalar; skip.
            i += 2
            continue
        # Body is from close_idx+1 up to the next open delimiter (or EOF).
        next_open_idx = (
            delim_indices[i + 2] if i + 2 < len(delim_indices) else len(lines)
        )
        body_lines = lines[close_idx + 1 : next_open_idx]
        # Trim leading/trailing blanks.
        while body_lines and not body_lines[0].strip():
            body_lines.pop(0)
        while body_lines and not body_lines[-1].strip():
            body_lines.pop()
        body = "\n".join(body_lines)
        blocks.append((parsed, body))
        i += 2
    return blocks


def _body_title(body: str) -> str:
    """First non-empty line of the body is the title."""
    for line in body.splitlines():
        s = line.strip()
        if s:
            return s
    return ""


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------


def _read_blocks(portco: str, filename: str) -> list[tuple[dict[str, Any], str]]:
    """Read and parse one memory-store file. Empty list on missing/empty."""
    path = MEMORY_STORE_ROOT / portco / filename
    text = _read_file(path)
    if text is None:
        return []
    return _parse_blocks(text)


def _read_key_metrics(portco: str) -> list[KeyMetricRow]:
    """Parse `metrics.md` YAML-front-matter blocks into `KeyMetricRow` rows."""
    rows: list[KeyMetricRow] = []
    for data, _body in _read_blocks(portco, "metrics.md"):
        try:
            rows.append(
                KeyMetricRow(
                    name=str(data["name"]),
                    value=str(data["value"]),
                    delta_vs_prior=str(data["delta_vs_prior"]),
                    status=data["status"],
                    as_of=str(data["as_of"]),
                )
            )
        except (KeyError, ValidationError, TypeError) as exc:
            logger.debug("metrics.md row skipped: %s", exc)
            continue
    return rows


def _build_finding(data: dict[str, Any], body: str) -> FindingRow | None:
    """Build a FindingRow from one block. Returns None on validation failure."""
    title = data.get("title") or _body_title(body)
    if not title:
        logger.debug("findings: skipping block with no title")
        return None
    try:
        return FindingRow(
            title=str(title),
            priority=data["priority"],
            urgency=data["urgency"],
            status=data["status"],
            first_seen=str(data["first_seen"]),
            decision_required=bool(data["decision_required"]),
            decision_options=list(data.get("decision_options") or []),
            evidence=str(data.get("evidence") or ""),
            confidence=data["confidence"],
        )
    except (KeyError, ValidationError, TypeError) as exc:
        logger.debug("findings row skipped: %s", exc)
        return None


def read_unresolved_findings(portco: str) -> list[FindingRow]:
    """Return open findings — anything in `findings.md` where status != resolved."""
    rows: list[FindingRow] = []
    for data, body in _read_blocks(portco, "findings.md"):
        if str(data.get("status", "")).lower() == "resolved":
            continue
        row = _build_finding(data, body)
        if row is not None and row.status != "resolved":
            rows.append(row)
    return rows


def _parse_iso_date(value: Any) -> datetime | None:
    """Parse a YAML date/datetime/str value to a datetime, or None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    s = str(value).strip()
    if not s:
        return None
    try:
        # `date` from yaml.safe_load comes back as datetime.date in PyYAML's
        # SafeLoader for YYYY-MM-DD; convert via fromisoformat on the str form.
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def read_recent_decisions(portco: str, days: int = 14) -> list[DecisionRow]:
    """Return decisions from `decisions.md` within the trailing `days` window."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    rows: list[DecisionRow] = []
    for data, body in _read_blocks(portco, "decisions.md"):
        title = data.get("title") or _body_title(body)
        decided_at_raw = data.get("decided_at")
        decided_at_dt = _parse_iso_date(decided_at_raw)
        if decided_at_dt is None:
            logger.debug("decisions row skipped: bad decided_at=%r", decided_at_raw)
            continue
        if decided_at_dt < cutoff:
            continue
        try:
            rows.append(
                DecisionRow(
                    title=str(title),
                    decided_at=str(decided_at_raw),
                    decision=str(data["decision"]),
                    portco_response=str(data.get("portco_response") or ""),
                )
            )
        except (KeyError, ValidationError, TypeError) as exc:
            logger.debug("decisions row skipped: %s", exc)
            continue
    return rows


def read_open_questions(portco: str) -> list[OpenQuestionRow]:
    """Return open-question rows from `open_questions.md`."""
    rows: list[OpenQuestionRow] = []
    for data, _body in _read_blocks(portco, "open_questions.md"):
        try:
            rows.append(
                OpenQuestionRow(
                    question=str(data["question"]),
                    asked_at=str(data["asked_at"]),
                    context=str(data.get("context") or ""),
                )
            )
        except (KeyError, ValidationError, TypeError) as exc:
            logger.debug("open_questions row skipped: %s", exc)
            continue
    return rows


# ---------------------------------------------------------------------------
# Trajectory
# ---------------------------------------------------------------------------


def _read_resolved_findings(portco: str) -> list[FindingRow]:
    """Findings from `resolved.md`. Used by trajectory to detect resolutions."""
    rows: list[FindingRow] = []
    for data, body in _read_blocks(portco, "resolved.md"):
        # Coerce status to resolved if the resolved.md author forgot.
        data = {**data, "status": "resolved"}
        row = _build_finding(data, body)
        if row is not None:
            rows.append(row)
    return rows


def compute_trajectory(portco: str, days: int = 7) -> TrajectoryBlock:
    """Compare current findings to a snapshot from `days` ago.

    Without a historical snapshot store, this returns an empty
    TrajectoryBlock — F7 ships the structure; a follow-up plan wires the
    snapshot. `resolved.md` is consulted to detect resolutions within the
    window (they go to `improving`), and findings whose `first_seen` is
    within the window populate `new_this_week`.
    """
    cutoff = datetime.utcnow() - timedelta(days=days)

    new_this_week: list[str] = []
    for data, body in _read_blocks(portco, "findings.md"):
        first_seen_dt = _parse_iso_date(data.get("first_seen"))
        if first_seen_dt is None or first_seen_dt < cutoff:
            continue
        title = data.get("title") or _body_title(body)
        if title:
            new_this_week.append(str(title))

    improving: list[str] = []
    for resolved in _read_resolved_findings(portco):
        # Use first_seen as a proxy; without a "resolved_at" field we can't
        # filter by resolution date. Skip the date filter here and include all
        # resolved entries (the file is append-only audit trail; recent rows
        # dominate the bottom). A follow-up plan adds resolved_at to the spec.
        improving.append(resolved.title)

    return TrajectoryBlock(
        improving=improving,
        worsening=[],
        new_this_week=new_this_week,
    )


# ---------------------------------------------------------------------------
# Cost block (Plan #35 integration — closes the "blocked on Plan #33" item)
# ---------------------------------------------------------------------------


# Cap on trend_pct so "infinite" trends (prior baseline = 0) render compactly.
_TREND_PCT_CAP = 999.0


def _cap_trend(pct: float) -> float:
    """Clamp a trend percentage to +/-_TREND_PCT_CAP."""
    if pct > _TREND_PCT_CAP:
        return _TREND_PCT_CAP
    if pct < -_TREND_PCT_CAP:
        return -_TREND_PCT_CAP
    return pct


def read_cost_block(portco: str) -> CostBlock | None:
    """Read the trailing operating-cost summary for `portco`.

    Queries `session_costs` (the Plan #35 attribution ledger) for:
      * trailing_7d_usd  — sum(cost_usd) over the last 7 days
      * trailing_30d_usd — sum(cost_usd) over the last 30 days
      * trend_pct        — % change of the 7d window vs the prior 7d window
      * top_task         — top trigger by 7d spend, formatted "trigger: $X"
      * cache_hit_pct    — cache reads as % of total input-side tokens

    Returns None when the cost ledger is unreachable (no DATABASE_URL) or
    when the query fails for any reason. Empty data — no rows in the
    window — still returns a populated CostBlock with zeros so the
    renderer can decide whether to show it.
    """
    # Import locally so this module stays usable in test environments
    # that don't have psycopg2 (the compute tests monkeypatch
    # MEMORY_STORE_ROOT but never touch the DB).
    try:
        import db_adapter
    except Exception:
        return None

    if not getattr(db_adapter, "DATABASE_URL", None):
        return None

    try:
        import psycopg2.extras

        conn = db_adapter._connect()
    except Exception:
        logger.exception("read_cost_block: DB connect failed")
        return None

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # 7d, 30d, and prior-7d totals + cache breakdown — single query
            # using FILTER so we don't pay round-trip cost per window.
            cur.execute(
                """
                SELECT
                    COALESCE(SUM(cost_usd) FILTER (
                        WHERE recorded_at >= NOW() - INTERVAL '7 days'
                    ), 0)::float AS spend_7d,
                    COALESCE(SUM(cost_usd) FILTER (
                        WHERE recorded_at >= NOW() - INTERVAL '30 days'
                    ), 0)::float AS spend_30d,
                    COALESCE(SUM(cost_usd) FILTER (
                        WHERE recorded_at >= NOW() - INTERVAL '14 days'
                          AND recorded_at <  NOW() - INTERVAL '7 days'
                    ), 0)::float AS spend_prior_7d,
                    COALESCE(SUM(cache_read_tokens) FILTER (
                        WHERE recorded_at >= NOW() - INTERVAL '7 days'
                    ), 0)::bigint AS cache_read_7d,
                    COALESCE(SUM(input_tokens + cache_read_tokens
                                 + cache_write_5m_tokens + cache_write_1h_tokens)
                             FILTER (
                                 WHERE recorded_at >= NOW() - INTERVAL '7 days'
                             ), 0)::bigint AS input_side_total_7d
                FROM session_costs
                WHERE portco_key = %s
                """,
                (portco,),
            )
            totals = cur.fetchone() or {}

            cur.execute(
                """
                SELECT trigger,
                       SUM(cost_usd)::float AS cost_usd
                FROM session_costs
                WHERE portco_key = %s
                  AND recorded_at >= NOW() - INTERVAL '7 days'
                GROUP BY trigger
                ORDER BY SUM(cost_usd) DESC
                LIMIT 1
                """,
                (portco,),
            )
            top_row = cur.fetchone()
    except Exception:
        logger.exception("read_cost_block: DB query failed for portco=%s", portco)
        try:
            conn.close()
        except Exception:
            pass
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass

    spend_7d = float(totals.get("spend_7d") or 0.0)
    spend_30d = float(totals.get("spend_30d") or 0.0)
    spend_prior_7d = float(totals.get("spend_prior_7d") or 0.0)

    if spend_prior_7d > 0:
        trend = ((spend_7d / spend_prior_7d) - 1.0) * 100.0
    elif spend_7d > 0:
        # Prior baseline was zero — call that "infinite growth" pinned at cap.
        trend = _TREND_PCT_CAP
    else:
        trend = 0.0
    trend = _cap_trend(trend)

    cache_read = int(totals.get("cache_read_7d") or 0)
    input_side_total = int(totals.get("input_side_total_7d") or 0)
    if input_side_total > 0:
        cache_hit = 100.0 * cache_read / input_side_total
    else:
        cache_hit = 0.0

    if top_row and top_row.get("trigger"):
        top_task = f"{top_row['trigger']}: ${float(top_row['cost_usd']):.2f}"
    else:
        top_task = ""

    return CostBlock(
        trailing_7d_usd=round(spend_7d, 2),
        trailing_30d_usd=round(spend_30d, 2),
        trend_pct=round(trend, 1),
        top_task=top_task,
        cache_hit_pct=round(cache_hit, 1),
        updated_at=datetime.utcnow().isoformat(),
    )


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------


def compute_surface(portco: str) -> SurfaceState:
    """Build a fully-populated `SurfaceState` for `portco`."""
    return SurfaceState(
        portco=portco,
        key_metrics=_read_key_metrics(portco),
        open_findings=read_unresolved_findings(portco),
        recent_decisions=read_recent_decisions(portco),
        trajectory=compute_trajectory(portco),
        open_questions=read_open_questions(portco),
        cost_block=read_cost_block(portco),
        generated_at=datetime.utcnow().isoformat(),
    )
