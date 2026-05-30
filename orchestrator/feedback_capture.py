"""Feedback capture (Plan #30 D1).

Single entry point ``record_feedback`` writes one row to ``feedback_events``
per signal a user expressed about a bot message. Currently two sources:

  - ``emoji`` — Slack reaction on a bot-authored message (wired in
    ``slack_bot.py``'s ``reaction_added`` handler).
  - ``text``  — text-mode feedback ("remember…", "always…", "never…") —
    reserved for Plan #30 D2.

Design notes:

  - Idempotent on ``(portco_key, agent_message_ts, user_id, signal, source)``
    via a partial unique index + ``ON CONFLICT DO NOTHING``. Re-firing the
    same emoji reaction (Slack occasionally redelivers ``reaction_added``)
    therefore never writes a duplicate row.

  - Best-effort writes only. The Slack handler that invokes this must never
    crash because Postgres is unavailable. We catch every exception and log
    it with the ``non-fatal`` marker so it shows up in log scans but doesn't
    propagate.

  - Schema lives in ``db_adapter.ensure_schema()`` alongside the rest of the
    core tables. Aggregation views and the ``/feedback`` slash command live
    in tasks D2/D3.
"""

from __future__ import annotations

import logging
from typing import Literal, Optional

import db_adapter

log = logging.getLogger(__name__)

Signal = Literal["positive", "negative", "neutral"]
Source = Literal["emoji", "text"]


def record_feedback(
    *,
    portco_key: str,
    channel_id: str,
    thread_ts: str,
    user_id: str,
    agent_message_ts: str,
    signal: Signal,
    source: Source,
    raw_text: Optional[str] = None,
) -> None:
    """Insert one feedback_events row. Idempotent on the dedup tuple.

    Args:
        portco_key:       Resolved portco (or "" if unknown — channel still logged).
        channel_id:       Slack channel where the reaction/text happened.
        thread_ts:        Parent thread ts of the agent message (or message ts
                          if the message was top-level).
        user_id:          Slack user who emitted the signal.
        agent_message_ts: The bot-authored message the user is reacting to.
                          This is the dedup anchor — re-firing the same
                          reaction on the same message by the same user
                          with the same signal+source is a no-op.
        signal:           One of "positive", "negative", "neutral".
        source:           "emoji" (D1) or "text" (D2, reserved).
        raw_text:         Optional original text (emoji name or user comment).

    Never raises. DB errors are logged as ``non-fatal`` and swallowed.
    """
    if not getattr(db_adapter, "DATABASE_URL", ""):
        # Local-dev mode (no DB) — log and bail. The Slack handler runs
        # the same code path locally; making this fatal would break dev.
        log.debug(
            "feedback_capture.record_feedback: no DATABASE_URL — non-fatal, skipping write"
        )
        return

    try:
        conn = db_adapter._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO feedback_events (
                        portco_key, channel_id, thread_ts,
                        user_id, agent_message_ts,
                        signal, source, raw_text
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (portco_key, agent_message_ts, user_id, signal, source)
                    DO NOTHING
                    """,
                    (
                        portco_key or "",
                        channel_id or "",
                        thread_ts or "",
                        user_id or "",
                        agent_message_ts or "",
                        signal,
                        source,
                        raw_text,
                    ),
                )
                conn.commit()
        finally:
            conn.close()
    except Exception as e:
        # Never propagate — the Slack handler must keep handling other events.
        log.warning(
            "feedback_capture.record_feedback: non-fatal DB write failure: %s", e
        )
