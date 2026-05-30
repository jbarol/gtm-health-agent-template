"""Ref-footer helper for Slack messages — Plan #46 Commit 1.

Builds a compact, copy-pasteable identifier that an operator can grep for
in Railway logs. The session-id slice matches the prefix of the same
session_id that appears verbatim in ``session_runner`` log lines, so a
copy-paste round-trip from Slack to log search resolves in seconds.

Two public functions:

- ``format_ref_footer`` returns the mrkdwn-italic ref string (or ``None``).
- ``ref_context_block`` wraps it in a Slack Block Kit context block dict.

Helpers are intentionally callable with any combination of ``session_id``,
``inv_id``, ``context``. Callers that have nothing to identify pass nothing
and get ``None`` back (no spurious empty footer).
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# 8 chars of ULID after the ``sesn_`` prefix is collision-resistant at our
# scale and still readable at a glance. The fallback below handles a future
# prefix change without crashing.
_SESN_PREFIX = "sesn_"
_SESN_ABBREV_END = 13  # indices 5..12 inclusive — 8 chars after sesn_
_FALLBACK_ABBREV = 12


def _abbreviate_session_id(session_id: str) -> str:
    if session_id.startswith(_SESN_PREFIX):
        return session_id[:_SESN_ABBREV_END]
    # Contract drift signal — Anthropic changed the prefix format.
    log.debug("[REF_FOOTER_FALLBACK_PREFIX] session_id=%r", session_id)
    return session_id[:_FALLBACK_ABBREV]


def format_ref_footer(
    session_id: str | None = None,
    inv_id: int | None = None,
    context: str | None = None,
) -> str | None:
    """Return the mrkdwn ref string, or ``None`` when nothing identifies the message."""
    sesn = _abbreviate_session_id(session_id) if session_id else None

    if sesn and inv_id is not None:
        body = f"{sesn} · inv {inv_id}"
    elif sesn:
        body = sesn
    elif inv_id is not None:
        body = f"inv {inv_id}"
    elif context:
        body = context
    else:
        return None

    return f"_ref: {body}_"


def ref_context_block(
    session_id: str | None = None,
    inv_id: int | None = None,
    context: str | None = None,
) -> dict | None:
    """Wrap ``format_ref_footer`` output in a Slack context block dict."""
    text = format_ref_footer(session_id=session_id, inv_id=inv_id, context=context)
    if text is None:
        return None
    return {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": text}],
    }
