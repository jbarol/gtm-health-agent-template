"""Normalize and hash error messages for the ❌-watcher dedup key.

Shared between:
    bin/audit-error-categories.py     — Phase 0 audit (gating)
    orchestrator/lifecycle.py         — enqueue hook (Phase 1 PR 2)
    orchestrator/watcher_pending_db.py — catch-up sweep (Phase 1 PR 1)

The function ``compute(message)`` returns the 16-hex-char sha1 prefix
of the volatile-ID-stripped first line. The full normalization regex
set lives in ``_NORMALIZATION_PATTERNS`` so the auditor can spot-check
underwetted buckets (Phase 0.5 pre-check).

Adding a new strip rule: add to the tuple, update the docstring, and
re-run ``bin/audit-error-categories.py --window-days 60`` to confirm
distinct hashes per category stays ≤ 5.
"""

from __future__ import annotations

import hashlib
import re


# Order matters — longer / more specific patterns first so a generic
# ``[0-9a-f]{7,40}`` SHA strip does not eat a labeled-ID prefix like
# ``session_abc1234567``.
_NORMALIZATION_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    # Labeled IDs (Anthropic SDK shapes).
    # ``sesn_`` is the real Managed Agents session ID prefix used in this
    # repo (see orchestrator/ref_footer_test.py); ``session_`` covers
    # the legacy Anthropic SDK shape that still appears in older log lines.
    (re.compile(r"sesn_[A-Za-z0-9]+"), "sesn_<ID>"),
    (re.compile(r"session_[A-Za-z0-9]+"), "session_<ID>"),
    (re.compile(r"msg_[A-Za-z0-9]+"), "msg_<ID>"),
    (re.compile(r"event_[A-Za-z0-9]+"), "event_<ID>"),
    (re.compile(r"req_[A-Za-z0-9]+"), "req_<ID>"),
    (re.compile(r"memstore_[A-Za-z0-9]+"), "memstore_<ID>"),
    (re.compile(r"agent_[A-Za-z0-9]+"), "agent_<ID>"),
    (re.compile(r"toolu_[A-Za-z0-9]+"), "toolu_<ID>"),
    # Investigation IDs in error messages
    (re.compile(r"inv_id=\d+"), "inv_id=<N>"),
    (re.compile(r"\binv \d+\b"), "inv <N>"),
    # Slack channel + ts shapes
    (re.compile(r"\bC[A-Z0-9]{8,}\b"), "C<CHANNEL>"),
    (re.compile(r"\b\d{10}\.\d{6}\b"), "<SLACK_TS>"),
    # Filename timestamps: 20260521-153045 or 2026-05-21T15:30:45
    (re.compile(r"\d{8}-\d{6}"), "<FNTS>"),
    (
        re.compile(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Z+-]\S*)?"
        ),
        "<ISO_TS>",
    ),
    # File paths (POSIX-style with extension)
    (re.compile(r"(?:/[\w.-]+)+\.[a-zA-Z0-9]{1,6}"), "<PATH>"),
    # UUIDs — MUST come before the bare-SHA strip below; otherwise the
    # \b[0-9a-f]{7,40}\b SHA regex eats each hex group of a UUID
    # individually and the UUID pattern never matches what is left.
    (
        re.compile(
            r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
        ),
        "<UUID>",
    ),
    # Git commit SHAs — placed after labeled IDs and UUIDs so it does
    # not eat their prefixes.
    (re.compile(r"\b[0-9a-f]{7,40}\b"), "<SHA>"),
    # Bare integers >= 4 digits (line numbers, sizes, counts)
    (re.compile(r"\b\d{4,}\b"), "<N>"),
    # Squeeze runs of whitespace to single space
    (re.compile(r"\s+"), " "),
)


def first_line(text: str | None) -> str:
    """Return the first line of ``text``, trimmed and capped at 500 chars."""
    if not text:
        return ""
    line = text.strip().split("\n", 1)[0]
    return line[:500]


def normalize(message: str | None) -> str:
    """Apply the volatile-identifier strip set to a raw error message."""
    out = first_line(message)
    for pattern, replacement in _NORMALIZATION_PATTERNS:
        out = pattern.sub(replacement, out)
    return out.strip()


def compute(message: str | None) -> str:
    """Return the 16-hex-char dedup hash for an error message."""
    return hashlib.sha1(normalize(message).encode("utf-8")).hexdigest()[:16]
