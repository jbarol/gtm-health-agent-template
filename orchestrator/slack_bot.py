"""Slack bot: receives questions/feedback from channel/@mentions/DMs, sends notifications."""

import logging
import re
from typing import Literal, Optional

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from config import (
    SLACK_BOT_TOKEN,
    SLACK_APP_TOKEN,
    SLACK_CHANNEL_ID,
    SLACK_NOTIFY_USER_IDS,
)
from feedback_capture import Signal
from prose_polish import polish as _plain_english
from slack_redact import redact_paths

log = logging.getLogger(__name__)

app = App(token=SLACK_BOT_TOKEN)

_on_question_callback = None
_on_feedback_callback = None
_seen_events = {}
_DEDUP_MAX = 200
_bot_user_id = None

FEEDBACK_PREFIXES = [
    "remember ",
    "always ",
    "never ",
    "from now on ",
    "going forward ",
    "note that ",
    "keep in mind ",
    "fyi ",
    "correction:",
    "update:",
    "feedback:",
    "instruction:",
]

# ─────────────────────────────────────────────────────────────────────────────
# Plan #31 E2 — Verbosity prefix detection + /verbosity slash command.
#
# Resolution order (highest precedence first):
#   1. Explicit prefix on the message (e.g. ``terse: why?``, ``normal: …``,
#      or any of the ``EXPAND_PREFIXES`` → verbose).
#   2. Channel-level stored default from ``channel_verbosity_preferences``.
#   3. Module-level default ``"normal"``.
#
# The legacy ``EXPAND_PREFIXES`` constant is retained for backward compat with
# the v1 ``expand:`` family of prefixes — they all map to ``"verbose"`` under
# the renamed 3-tier model (Plan #31 E1: terse | normal | verbose).
# ─────────────────────────────────────────────────────────────────────────────

# Backward-compat: every prefix in this list maps to verbosity "verbose".
# Detected on the incoming user message; the prefix is stripped before the
# question is passed to the investigation handler.
EXPAND_PREFIXES = [
    "expand:",
    "long:",
    "details:",
    "full version:",
    "full:",
    "verbose:",
]

# Default verbosity when no prefix is present and no channel pref is stored.
DEFAULT_VERBOSITY = "normal"

# Allowed canonical verbosity values (Plan #31 E1 renamed model).
VALID_VERBOSITIES = ("terse", "normal", "verbose")

# Map of explicit verbosity prefixes → canonical verbosity. The explicit
# ``normal:`` form lets a user override a channel default that was set to
# something else without having to clear the pref entirely. Lookups are
# case-insensitive — see ``_resolve_verbosity``.
EXPLICIT_VERBOSITY_PREFIXES = {
    "terse:": "terse",
    "normal:": "normal",
    "verbose:": "verbose",
}

# Plan #30 D1 — Slack emoji → feedback signal mapping. Only reactions on
# bot-authored messages produce rows in feedback_events; anything else is a
# silent skip. The taxonomy here is intentionally narrow (the canonical
# positive/negative set Slack users default to); broader emoji like ``eyes``
# or ``warning`` are reserved for D2's expanded action taxonomy.
EMOJI_TO_SIGNAL: dict[str, Signal] = {
    # positive
    "+1": "positive",
    "thumbsup": "positive",
    "heavy_check_mark": "positive",
    "white_check_mark": "positive",
    "tada": "positive",
    "100": "positive",
    "fire": "positive",
    "clap": "positive",
    "bow": "positive",
    # negative
    "-1": "negative",
    "thumbsdown": "negative",
    "x": "negative",
    "no_entry": "negative",
    "confused": "negative",
    "disappointed": "negative",
    "cry": "negative",
    "rage": "negative",
}


def set_question_handler(callback):
    """callback(user_id, text, thread_ts, channel_id, ack_fn, verbosity, event_ts) -> None

    ack_fn(text: str) posts a message in the thread. The callback should call
    ack_fn with the acknowledgment text before starting the investigation.
    If the callback never calls ack_fn, _handle_incoming posts a default ack.

    verbosity is one of :data:`VALID_VERBOSITIES` (``"terse" | "normal" |
    "verbose"``, Plan #31 E1). Resolved in :func:`_resolve_verbosity` from
    the user's message prefix → channel-level stored default → module default
    (``"normal"``). Legacy ``expand:`` family prefixes still work and map to
    ``"verbose"``. The renderer (post-E1) accepts both canonical 3-tier and
    legacy ``summary | expanded`` aliases, so existing callbacks that ignore
    this kwarg continue to function.

    event_ts is the timestamp of the user's original Slack message. The
    callback threads it through to ``session_runner`` so the lifecycle
    reaction (👁 → ⏰ → ✅/❌) can be flipped on the right message as the
    investigation moves through its phases.
    """
    global _on_question_callback
    _on_question_callback = callback


def set_feedback_handler(callback):
    global _on_feedback_callback
    _on_feedback_callback = callback


def _is_feedback(text: str) -> bool:
    lower = text.lower()
    return any(lower.startswith(p) for p in FEEDBACK_PREFIXES)


# Plan: Design I (2026-05-15). Catch the placeholder-leak pattern.
# Live failure 2026-05-15: a Slack message arrived in L9xZx's thread with
# the literal string "[an unspecified metric]" — an upstream renderer or
# scheduled-trigger template that didn't get its variable filled in. The
# Coordinator wisely refused to invent a default, but the message
# shouldn't have been sent in that state.
#
# Pattern: `[Cap-or-letter ...]` with 3–80 chars inside, but NOT:
#   - Slack mrkdwn links `<url|label>` (uses angle brackets, not square)
#   - Footnote markers `[1]`, `[2]`, `[ab]` (1–2 chars inside)
#   - Code-fenced or backtick-wrapped content (we don't try to parse those;
#     the guard accepts a small false-positive risk inside code blocks
#     in exchange for catching the actual leak)
#
# Kill switch: PLACEHOLDER_GUARD_ENABLED=false disables.
# Negative lookahead ``(?!\()`` skips markdown link labels — ``[label](url)``
# has a ``]`` immediately followed by ``(``. Without this the guard fires on
# any ``[Click here](https://...)`` link that slipped past _md_to_slack
# conversion. Slack mrkdwn ``<url|label>`` uses angle brackets, so it's
# already not matched. (Self-review fix, 2026-05-15.)
_PLACEHOLDER_PATTERN = re.compile(r"\[[A-Za-z][^\]\n]{3,80}\](?!\()")


def _placeholder_guard_enabled() -> bool:
    import os

    return os.environ.get("PLACEHOLDER_GUARD_ENABLED", "true").lower() != "false"


def _check_for_unfilled_placeholders(text: str) -> Optional[str]:
    """Return the first suspicious placeholder span if the text has one, else None.

    Caller decides what to do (replace + admin DM + WARN). Returns the
    matched string verbatim so the log line is forensically useful.
    """
    if not text:
        return None
    for m in _PLACEHOLDER_PATTERN.finditer(text):
        candidate = m.group(0)
        # Skip the Slack rich-link case `<url|label>` — uses angle brackets.
        # Skip pure ID-like patterns and known false positives the agents use:
        # `[a]`, `[12]`, `[ab]` (handled by the 3-char min in the regex).
        # `[year]`-style placeholders ARE caught — those are legit leaks.
        inner = candidate[1:-1].strip()
        if not inner:
            continue
        # Common-words allowlist: short noun phrases that legitimately appear
        # in brackets in analyses (e.g. `[draft]`, `[redacted]`, `[urgent]`).
        # If anyone wants more entries here they can add them; the guard is
        # intentionally conservative because a false negative is worse than
        # a false positive (it costs a DM, not a dropped message).
        if inner.lower() in {"draft", "redacted", "urgent", "tba", "tbd", "wip"}:
            continue
        return candidate
    return None


_RECENT_INVESTIGATION_WINDOW_SECONDS = 30 * 60  # 30 minutes


def _is_active_or_recent_investigation(row) -> bool:
    """True when a thread has a live or recently-finished investigation.

    Design B (2026-05-15): used to decide whether a feedback-prefixed message
    in a thread should bypass the memory-store short-circuit and be routed
    to the question pipeline instead. ``row`` is the dict returned by
    ``_lookup_thread_investigation`` or ``None``.

    Active: status in ('queued', 'running').
    Recent: any status whose started_at is within the last 30 minutes.
    """
    if not row:
        return False
    status = (row.get("status") or "").lower()
    if status in ("queued", "running"):
        return True
    started_at = row.get("started_at")
    if not started_at:
        return False
    from datetime import datetime, timezone

    try:
        ts = (
            started_at
            if hasattr(started_at, "tzinfo")
            else datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        )
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        return 0 <= age < _RECENT_INVESTIGATION_WINDOW_SECONDS
    except Exception:
        return False


def _detect_expand_prefix(text: str) -> tuple[bool, str]:  # pyright: ignore[reportUnusedFunction]
    """Return (is_expand, stripped_text).

    If the message starts with one of EXPAND_PREFIXES (case-insensitive),
    strip the prefix and return True so the caller can route to expanded
    verbosity. Otherwise return False and the original text.

    Retained for backward compat with the v1 expand-prefix tests. New code
    should call :func:`_resolve_verbosity` which returns the canonical
    3-tier verbosity (``terse | normal | verbose``).
    """
    lower = text.lower()
    for prefix in EXPAND_PREFIXES:
        if lower.startswith(prefix):
            return True, text[len(prefix) :].lstrip()
    return False, text


def _resolve_verbosity(text: str, channel_id: Optional[str]) -> tuple[str, str]:
    """Resolve verbosity for an incoming Slack message.

    Resolution order:

      1. Explicit prefix on the message
         (``terse:`` / ``normal:`` / ``verbose:`` / any legacy ``EXPAND_PREFIXES``
         → ``verbose``). Strips the prefix and returns the tier.
      2. ``channel_verbosity_preferences`` row for ``channel_id`` (if any).
      3. Module-level :data:`DEFAULT_VERBOSITY` (``"normal"``).

    Returns:
        ``(stripped_text, verbosity)`` where ``verbosity`` is one of
        :data:`VALID_VERBOSITIES`. The text only changes when a prefix was
        present; in steps 2 and 3 the original text is returned verbatim.

    The DB lookup degrades gracefully — if ``db_adapter`` raises (DB down,
    missing table, etc.) we silently fall through to the default. Verbosity
    resolution must never break the message handler loop.
    """
    if not text:
        return text, DEFAULT_VERBOSITY

    lower = text.lower()

    # Step 1a: explicit tier prefixes (terse | normal | verbose).
    for prefix, tier in EXPLICIT_VERBOSITY_PREFIXES.items():
        if lower.startswith(prefix):
            return text[len(prefix) :].lstrip(), tier

    # Step 1b: legacy expand: family (all map to verbose).
    for prefix in EXPAND_PREFIXES:
        if lower.startswith(prefix):
            return text[len(prefix) :].lstrip(), "verbose"

    # Step 2: channel-level stored default.
    if channel_id:
        try:
            import db_adapter

            stored = db_adapter.get_channel_verbosity(channel_id)
            if stored in VALID_VERBOSITIES:
                return text, stored
        except Exception as e:
            log.debug(f"channel verbosity lookup failed for {channel_id}: {e}")

    # Step 3: default.
    return text, DEFAULT_VERBOSITY


def _get_thread_context(
    channel_id: Optional[str], thread_ts: Optional[str], current_ts: str
) -> str:
    """Fetch earlier messages in a thread to provide context for follow-ups."""
    if not channel_id or not thread_ts or thread_ts == current_ts:
        return ""
    try:
        result = app.client.conversations_replies(
            channel=channel_id,
            ts=thread_ts,
            limit=20,
        )
        messages = result.get("messages", [])
        context_parts = []
        for msg in messages:
            if msg.get("ts") == current_ts:
                break
            msg_text = msg.get("text", "")
            if not msg_text:
                continue
            is_bot = bool(msg.get("bot_id"))
            if not is_bot and "<@" in msg_text:
                msg_text = msg_text.split(">", 1)[-1].strip()
            if not is_bot and msg_text:
                context_parts.append(msg_text)
        if context_parts:
            return (
                "Thread context (earlier messages):\n"
                + "\n---\n".join(context_parts)
                + "\n\nLatest follow-up: "
            )
    except Exception as e:
        log.debug(f"Failed to fetch thread context: {e}")
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# Track F — In-thread meta-intent routing (status / cancel / pause).
#
# Problem: ANY in-thread @mention used to re-fire the kickoff template
# ("On it — investigating now…"), ignoring whether the user was asking for
# a status update on the running investigation. The bot felt deaf.
#
# Fix: classify a small set of meta-intents BEFORE the question pipeline. The
# classifier is pure regex / keyword matching (no LLM call — latency budget
# <100ms) and only fires for short, in-thread messages where an existing
# investigation row is attached to the thread. Anything longer or more
# ambiguous falls through to the unchanged question pipeline.
#
# Intent classes (case-insensitive):
#   - status: "status", "progress", "update" / "any update", "where are you",
#             "what's happening", "any progress"
#   - cancel: "cancel", "stop", "abort", "kill it", "nevermind"
#   - pause:  "pause", "hold on", "wait"
#
# Length heuristic: the dominant intent must occupy the message. We require
# the message body (after stripping the @mention and trailing punctuation)
# to be at most :data:`META_INTENT_MAX_TOKENS` words. "After the status
# update, also tell me X" is 8+ tokens and falls through cleanly.
# ─────────────────────────────────────────────────────────────────────────────

META_INTENT_MAX_TOKENS = 6

# Word-boundary regex for each intent. Ordered by class precedence in
# :func:`classify_meta_intent`. Multi-word phrases (``"kill it"``,
# ``"hold on"``, ``"what's the status"``) match as a literal substring; single
# word triggers use ``\b`` so we don't fire on ``"updates"`` in passing.
_META_INTENT_PATTERNS: dict[str, list[re.Pattern]] = {
    "cancel": [
        re.compile(r"\bcancel\b", re.IGNORECASE),
        re.compile(r"\bstop\b", re.IGNORECASE),
        re.compile(r"\babort\b", re.IGNORECASE),
        re.compile(r"\bkill it\b", re.IGNORECASE),
        re.compile(r"\bnevermind\b", re.IGNORECASE),
        re.compile(r"\bnever mind\b", re.IGNORECASE),
    ],
    "pause": [
        re.compile(r"\bpause\b", re.IGNORECASE),
        re.compile(r"\bhold on\b", re.IGNORECASE),
        re.compile(r"^wait[\s\.\?!]*$", re.IGNORECASE),  # "wait" alone
        re.compile(r"\bplease wait\b", re.IGNORECASE),
    ],
    "status": [
        re.compile(r"\bstatus\b", re.IGNORECASE),
        re.compile(r"\bprogress\b", re.IGNORECASE),
        re.compile(r"\bany update", re.IGNORECASE),
        re.compile(r"\bgive me an update", re.IGNORECASE),
        re.compile(r"\bstatus update\b", re.IGNORECASE),
        re.compile(r"\bwhere are you\b", re.IGNORECASE),
        re.compile(r"what'?s? happening", re.IGNORECASE),
        re.compile(r"what'?s? the status", re.IGNORECASE),
        re.compile(r"\bhow'?s it going\b", re.IGNORECASE),
        re.compile(r"\bare you done\b", re.IGNORECASE),
    ],
    "resume": [
        re.compile(r"\bcontinue\b", re.IGNORECASE),
        re.compile(r"\bresume\b", re.IGNORECASE),
        re.compile(r"\bgo ahead\b", re.IGNORECASE),
        re.compile(r"\bkeep going\b", re.IGNORECASE),
        re.compile(r"\bproceed\b", re.IGNORECASE),
    ],
}

# Action vs. non-action split. ACTION meta-intents terminate or interrupt the
# running session (cancel kills it, pause is a no-op helper that nonetheless
# reads as a control directive). NON-ACTION meta-intents are read-only queries
# or behavioral adjustments that don't end the session — ``status`` is a pure
# read.
#
# Used by ``_handle_incoming`` to decide which way the feedback-prefix vs
# meta-intent precedence breaks in an active thread:
#   - feedback prefix + ACTION  → save the rule, skip the action
#     (e.g. ``"never stop after one error"`` saves to memory, not a cancel)
#   - feedback prefix + NON-ACTION → let the meta-intent fire (Design B —
#     mid-flight continuation directives still reach the live session)
#
# Any future meta-intent that adjusts in-flight rendering (e.g. ``"include a
# chart of X"``, ``"format as a table"``) should be added to
# :data:`_META_INTENT_PATTERNS` and left OUT of this set.
ACTION_META_INTENTS: frozenset[str] = frozenset({"cancel", "pause"})


def classify_meta_intent(
    text: str,
) -> Optional[Literal["status", "cancel", "pause", "resume"]]:
    """Classify ``text`` into a meta-intent class, or return None.

    Returns one of ``"status" | "cancel" | "pause" | "resume"`` when the
    message matches a meta-intent pattern AND the body is short enough
    that the intent is the dominant content (see
    :data:`META_INTENT_MAX_TOKENS`). Returns None otherwise — the caller
    should fall through to the question pipeline.

    Length heuristic: messages with more than :data:`META_INTENT_MAX_TOKENS`
    word tokens are presumed to carry a real question, not a meta-intent.
    "After the status update, also tell me X" (10 tokens) → None.

    Empty / whitespace-only / bare-mention input → None.

    Class precedence: ``cancel`` > ``pause`` > ``status`` > ``resume``. A
    message that matches multiple patterns ("cancel and pause") routes to
    cancel — the stronger, more destructive intent wins.
    """
    if not text:
        return None
    body = text.strip()
    if not body:
        return None

    # Token count guard. Punctuation glued to a word counts as part of that
    # word — "stop." is one token, "stop the run" is three.
    tokens = body.split()
    if len(tokens) > META_INTENT_MAX_TOKENS:
        return None

    for intent, patterns in _META_INTENT_PATTERNS.items():
        for pat in patterns:
            if pat.search(body):
                return intent  # type: ignore[return-value]

    return None


def _handle_meta_intent(
    intent: Literal["status", "cancel", "pause", "resume"],
    investigation: dict,
    user: str,
    thread_ts: str,
    say,
) -> None:
    """Dispatch a classified meta-intent. Never raises.

    Critically: this routine MUST NOT spawn a Coordinator session and MUST
    NOT post the kickoff ack. The caller (``_handle_incoming``) has already
    decided this message bypasses the question pipeline.
    """
    inv_id = investigation.get("id")
    session_id = investigation.get("session_id")

    # The dict came from a SERIAL primary-key row, so ``id`` should always
    # be a non-None int — but ``investigation.get("id")`` is typed
    # ``Optional[Any]`` and the downstream callees (``status_snippet``,
    # ``cancel_investigation``) declare ``int``. Guard early; if the row
    # somehow lacks an id, there is nothing meaningful to act on.
    if not isinstance(inv_id, int):
        log.warning(
            f"meta_intent[{intent}]: investigation row missing integer id "
            f"(got {inv_id!r}); dropping intent for thread={thread_ts}"
        )
        return

    try:
        if intent == "status":
            try:
                import status_responder

                snippet = status_responder.status_snippet(inv_id)
            except Exception:
                log.exception(
                    f"meta_intent[status]: status_responder failed for inv={inv_id}"
                )
                snippet = (
                    ":mag: Couldn't load status — investigation `"
                    f"{inv_id}` is still tracked, but the status responder "
                    "errored. Check orchestrator logs."
                )
            log.info(
                f"meta_intent=status from <@{user}> in thread={thread_ts} "
                f"(inv={inv_id})"
            )
            say(snippet, thread_ts=thread_ts)
            return

        if intent == "cancel":
            # DB write goes first — it is the source of truth for whether the
            # investigation was actually in flight. Only if the row moved from
            # queued/running to cancelled do we archive the Anthropic session
            # and post the "Stopped" copy. Terminal rows get the no-op message
            # instead (codex review PR #97, comment 3223872886).
            cancelled = False
            try:
                import db_adapter

                cancelled = db_adapter.cancel_investigation(
                    inv_id, reason=f"cancelled by <@{user}> via in-thread intent"
                )
            except Exception:
                log.exception(
                    f"meta_intent[cancel]: db_adapter.cancel_investigation "
                    f"failed for inv={inv_id}"
                )

            if not cancelled:
                log.info(
                    f"meta_intent=cancel from <@{user}> in thread={thread_ts} "
                    f"(inv={inv_id}, already_terminal=True)"
                )
                say(
                    "This investigation has already finished. Nothing to cancel.",
                    thread_ts=thread_ts,
                )
                return

            archived = False
            if session_id:
                try:
                    # Lazy import — same pattern as session_runner consumers.
                    from session_runner import client as _client

                    _client.beta.sessions.archive(session_id)
                    archived = True
                except Exception:
                    log.exception(
                        f"meta_intent[cancel]: sessions.archive failed for {session_id}"
                    )
            log.info(
                f"meta_intent=cancel from <@{user}> in thread={thread_ts} "
                f"(inv={inv_id}, session_archived={archived})"
            )
            say("Stopped. Session archived.", thread_ts=thread_ts)
            return

        if intent == "pause":
            log.info(
                f"meta_intent=pause from <@{user}> in thread={thread_ts} (inv={inv_id})"
            )
            say(
                "I can't pause mid-session. Use 'cancel' to stop, then "
                "re-ask later when you want to resume.",
                thread_ts=thread_ts,
            )
            return

        if intent == "resume":
            log.info(
                f"meta_intent=resume from <@{user}> in thread={thread_ts} (inv={inv_id})"
            )
            # No active session → nothing to wake. Live repro 2026-05-19:
            # Jared replied "continue" in a thread where the session had
            # already terminalized; without this guard the events.send call
            # below would 400 and surface as a stack trace.
            if not session_id:
                say("No active session to resume.", thread_ts=thread_ts)
                return

            # Already-finished investigations get a polite redirect instead
            # of a wakeup nudge — the session may still be archivable but
            # the user's expectation is that the work is done.
            status = investigation.get("status")
            if status in ("completed", "failed", "cancelled"):
                say(
                    "The investigation already finished. Ask a new question "
                    "to start another.",
                    thread_ts=thread_ts,
                )
                return

            # Send a user.message nudge to wake the existing session.
            # Lazy import — same pattern as the cancel branch.
            try:
                from session_runner import client as _client

                _client.beta.sessions.events.send(
                    session_id=session_id,
                    events=[
                        {
                            "type": "user.message",
                            "content": [
                                {
                                    "type": "text",
                                    "text": (
                                        "[user] Please proceed with the "
                                        "results you have and call "
                                        "post_report."
                                    ),
                                }
                            ],
                        }
                    ],
                )
            except Exception:
                log.exception(
                    f"meta_intent[resume]: events.send failed for {session_id}"
                )
                say(
                    "Couldn't reach the session — try again in 30s.",
                    thread_ts=thread_ts,
                )
                return

            say(
                ":arrows_counterclockwise: Nudged the agent to proceed.",
                thread_ts=thread_ts,
            )
            return
    except Exception:
        # Belt-and-braces: meta-intent handlers must never crash the Socket
        # Mode loop. The individual branches already swallow errors but a
        # bug in ``say`` itself shouldn't bubble.
        log.exception(f"meta_intent handler crashed (intent={intent}, inv={inv_id})")


def _lookup_thread_investigation(thread_ts: Optional[str]) -> Optional[dict]:
    """Return the investigation row attached to a thread, or None.

    Thin wrapper around ``db_adapter.get_investigation_for_thread`` that
    swallows every error so the in-thread router degrades to "no row =
    fall through to question pipeline" on any DB hiccup.
    """
    if not thread_ts:
        return None
    try:
        import db_adapter

        return db_adapter.get_investigation_for_thread(thread_ts)
    except Exception as e:
        log.debug(f"_lookup_thread_investigation failed: {e}")
        return None


def _handle_incoming(text, user, thread_ts, say, event_ts=None, channel_id=None):
    # Deduplicate — Slack can send the same event twice
    dedup_key = event_ts or f"{user}:{text[:50]}:{thread_ts}"
    if dedup_key in _seen_events:
        return
    _seen_events[dedup_key] = None
    while len(_seen_events) > _DEDUP_MAX:
        _seen_events.pop(next(iter(_seen_events)))

    current_ts = event_ts or ""

    # Strip Slack ``<@USERID>`` mentions from anywhere in the text — not
    # just a leading mention. Earlier logic did ``text.split(">", 1)[-1]``
    # which only worked when the mention sat at the start of the message
    # (``@bot what's the win rate?``). When users put the mention at the
    # end (``what's the win rate? @bot``) the split discarded the entire
    # question and left an empty string, which triggered the
    # "Ask me a question…" fallback instead of dispatching an
    # investigation. Live repro 2026-05-13 17:20 PT in the Acme
    # channel — Anthropic console showed zero sessions created for the
    # message because the routing dropped it before reaching the
    # orchestrator.
    text = re.sub(r"<@\w+>", "", text).strip()

    if not text:
        say(
            "Ask me a question about portco GTM health and I'll investigate.",
            thread_ts=thread_ts,
        )
        return

    # Lifecycle reaction — 👁 on receipt. Marks "the bot saw your message"
    # the instant we have non-empty text, before any classification or
    # downstream dispatch. transition_reaction below in session_runner
    # flips this to ⏰ once the agent session is live, ✅ on successful
    # post_report, or ❌ on catastrophic failure. Reactions are best-effort
    # status indicators — never block the user workflow on a failed add.
    if channel_id and current_ts:
        add_reaction(channel_id, current_ts, REACTION_RECEIVED)

    # Feedback prefix vs meta-intent precedence. Standing instructions like
    # ``"always include status updates"`` or ``"never stop after one error"``
    # contain meta-intent keywords (``status``, ``stop``) but are memory writes,
    # not control signals — routing them to ``_handle_meta_intent`` would skip
    # the memory store and, worse, cancel the active investigation in the
    # ``"never stop…"`` case (codex review PR #97, comment 3223872884).
    #
    # Design A (PR #97): feedback prefix always wins over meta-intent.
    #
    # Design B (PR #235, commit d82107a, 2026-05-15): in an ACTIVE thread (live
    # investigation, or one finished in the last 30 min), a feedback-prefixed
    # message is often a continuation request — the user is telling the agent
    # how to render its next response, not setting standing policy. Bypass the
    # feedback short-circuit so the live session sees it. Live repro:
    # sesn_EXAMPLE on 2026-05-15 — user replied in-thread
    # asking for charts, the message matched the feedback prefix list and
    # went silently to memory; the session sat idle.
    #
    # Design B's collateral damage: ``"never stop after one error"`` in an
    # active thread CANCELS the investigation because ``\bstop\b`` is one of
    # the cancel meta-intent patterns. The user clearly meant a rule, not a
    # command.
    #
    # PR #239 split: classify the matched meta-intent into ACTION (cancel,
    # pause — terminate or interrupt) vs NON-ACTION (status — read-only) and
    # mix the two designs:
    #
    #   - outside active thread       → Design A (feedback always wins)
    #   - active thread + NON-ACTION  → Design B (let meta-intent fire so the
    #     live session adopts the directive)
    #   - active thread + ACTION      → Design A restored (save the rule, do
    #     NOT terminate the session)
    if _is_feedback(text):
        active_or_recent = _is_active_or_recent_investigation(
            _lookup_thread_investigation(thread_ts)
        )
        route_to_feedback = not active_or_recent
        if active_or_recent:
            # In an active thread, the precedence depends on what kind of
            # meta-intent the message would trigger. Re-use the same classifier
            # the meta-intent gate uses below so the two stay in lockstep.
            classified = classify_meta_intent(text)
            if classified is not None and classified in ACTION_META_INTENTS:
                # Action meta-intent on a feedback-prefixed message — the user
                # is setting a rule, not asking us to terminate. Restore PR #97
                # behavior so we save the rule.
                route_to_feedback = True
                log.info(
                    f"Feedback prefix in active thread + ACTION meta-intent "
                    f"({classified!r}) — routing to memory (PR #239 restore "
                    f"of PR #97 safety): user=<@{user}> thread={thread_ts}: "
                    f"{text[:120]}"
                )
            else:
                log.info(
                    f"Feedback prefix in active thread — routing to question "
                    f"pipeline instead of memory (Design B, classified="
                    f"{classified!r}): user=<@{user}> thread={thread_ts}: "
                    f"{text[:120]}"
                )
        if route_to_feedback:
            matched_prefix = next(
                (p for p in FEEDBACK_PREFIXES if text.lower().startswith(p)), "?"
            )
            log.info(
                f"Feedback from <@{user}> (matched prefix={matched_prefix!r} "
                f"channel={channel_id} thread={thread_ts}): {text}"
            )
            say(
                ":memo: Saved to standing instructions. Note: this was treated as "
                f'feedback (matched prefix "{matched_prefix.strip()}"), not a question. '
                "If you wanted to ask the agent something, repost without the prefix.",
                thread_ts=thread_ts,
            )
            if _on_feedback_callback:
                _on_feedback_callback(user, text, thread_ts, channel_id)
            return

    # Track F — In-thread meta-intent routing. Runs AFTER feedback detection
    # (so "always/never …" memory writes win for the cases the feedback branch
    # decided to route to memory) but BEFORE the question pipeline so
    # status/cancel/pause win over the canned kickoff ack. The classifier
    # returns None for any message that's too long or doesn't match a meta
    # pattern; pair that with the "must have an existing investigation on
    # this thread" gate so fresh questions are never short-circuited.
    intent = classify_meta_intent(text)
    if intent is not None:
        investigation = _lookup_thread_investigation(thread_ts)
        # Only fire meta-intent handling when there's an in-flight or
        # recently-finished investigation on this thread. Without one, the
        # message is either a fresh question ("status of the pipeline?")
        # or chatter — fall through to the question pipeline either way.
        if investigation is not None:
            _handle_meta_intent(
                intent=intent,
                investigation=investigation,
                user=user,
                thread_ts=thread_ts,
                say=say,
            )
            return

    # Resolve verbosity (Plan #31 E2). Order: explicit prefix > channel pref >
    # default ("normal"). The text is returned with any matching prefix stripped.
    text, verbosity = _resolve_verbosity(text, channel_id)
    if verbosity != DEFAULT_VERBOSITY:
        log.info(
            f"Verbosity resolved to {verbosity!r} for question in channel={channel_id}"
        )

    thread_context = _get_thread_context(channel_id, thread_ts, current_ts)
    if thread_context:
        text = thread_context + text
        log.info(f"Thread follow-up from <@{user}> with context")
    else:
        log.info(f"Question from <@{user}>: {text}")

    # Build an ack function the callback can invoke with custom text.
    # If the callback never calls it, we post a default ack before returning.
    ack_posted = {"done": False}

    def ack_fn(msg: str):
        if not ack_posted["done"]:
            say(msg, thread_ts=thread_ts)
            ack_posted["done"] = True

    if _on_question_callback:
        # The current signature is `callback(user, text, thread_ts, channel_id,
        # ack_fn, verbosity, event_ts)` — see set_question_handler docstring.
        # Pre-rewrite this had a try/except TypeError shim for legacy callbacks;
        # that was silently broken for **kwargs callbacks (no TypeError,
        # verbosity dropped). New callbacks that don't accept verbosity will
        # fail loud — the right signal to update the callback rather than
        # auto-degrade. event_ts threads the user's message ts down to
        # session_runner so the lifecycle reaction can be flipped at each
        # phase boundary.
        _on_question_callback(
            user,
            text,
            thread_ts,
            channel_id,
            ack_fn,
            verbosity=verbosity,
            event_ts=current_ts or None,
        )

    # Fallback: if callback didn't ack (e.g. old handler, or error before ack)
    if not ack_posted["done"]:
        say(
            "On it — investigating now. I'll post findings in this thread when done, including any charts and data files.",
            thread_ts=thread_ts,
        )


def on_cost_command(ack, command, respond):
    """``/cost`` slash command — render the cost ledger as Slack mrkdwn.

    Surface (per Plan #35):
        /cost              today's spend, all portcos
        /cost today        same
        /cost week         last 7 days
        /cost month        last 30 days
        /cost <portco>     today, scoped to one portco
        /cost <portco> week / month — etc.
        /cost reconcile    drift between local estimate and Anthropic billing
                           for yesterday

    The handler ack()s immediately (Slack's 3-second budget), then queries
    the ledger and posts the formatted breakdown via ``respond()``. All errors
    are caught so a DB outage or missing column never causes Slack to retry
    the command, which would result in duplicate output.

    Registered with the Bolt app via ``app.command("/cost")(on_cost_command)``
    below — keeping the function module-level (not wrapped in the decorator)
    so unit tests can call it directly without going through Bolt's adapter.
    """
    try:
        ack()
    except Exception:
        log.exception("/cost: ack() failed")

    text = ""
    try:
        text = (command or {}).get("text", "") or ""
    except Exception:
        pass

    try:
        import cost_queries

        message = cost_queries.handle_cost_command(text)
    except Exception:
        log.exception("/cost: handler crashed (text=%r)", text)
        message = "_/cost: query failed. Check orchestrator logs._"

    try:
        respond(text=message, response_type="in_channel")
    except Exception:
        log.exception("/cost: respond() failed")


# Register the slash command after the function is defined. app.command() is
# a decorator factory — calling it returns a decorator we apply manually.
# Renamed from ``/cost`` to ``/token-cost`` 2026-05-19: Slack's manifest validator
# rejected ``/cost`` as "invalid name" (likely reserved as too generic). The
# token-cost prefix is unambiguous (these are Anthropic token costs) and
# validates cleanly.
app.command("/bot-token-cost")(on_cost_command)


def on_feedback_command(ack, command, respond):
    """``/feedback`` slash command — render feedback rollups as Slack mrkdwn.

    Surface (Plan #30 D2):
        /feedback              7-day aggregate, by portco (default view)
        /feedback agent        by-agent rollup
        /feedback trigger      by-trigger rollup
        /feedback negative     top-5 most-recent negative-signal drill-down
        /feedback 30           same as default but with a 30-day window
        /feedback agent 30     combine view + window — order doesn't matter

    Same ack-fast / respond-with-mrkdwn pattern as ``on_cost_command``. The
    handler ack()s immediately (Slack's 3-second budget), then delegates to
    ``feedback_aggregate.handle_feedback_command`` which is the entire flow
    minus the Bolt adapter.

    All exceptions are caught so a DB outage never causes Slack to retry the
    command (which would produce duplicate output).

    Module-level (not wrapped in the decorator) so unit tests can invoke it
    directly without going through Bolt's adapter.
    """
    try:
        ack()
    except Exception:
        log.exception("/feedback: ack() failed")

    text = ""
    try:
        text = (command or {}).get("text", "") or ""
    except Exception:
        pass

    try:
        import feedback_aggregate

        message = feedback_aggregate.handle_feedback_command(text)
    except Exception:
        log.exception("/feedback: handler crashed (text=%r)", text)
        message = "_/feedback: query failed. Check orchestrator logs._"

    try:
        respond(text=message, response_type="in_channel")
    except Exception:
        log.exception("/feedback: respond() failed")


app.command("/bot-feedback")(on_feedback_command)


# ─────────────────────────────────────────────────────────────────────────────
# Plan #31 E2 — /verbosity slash command.
#
# Slack manifest note: add ``commands: ["/verbosity"]`` alongside the existing
# ``/cost`` entry so Slack registers the command for the workspace. Without
# the manifest entry Slack returns ``dispatch_failed`` before the handler
# ever fires.
# ─────────────────────────────────────────────────────────────────────────────


def on_verbosity_command(ack, command, respond):
    """``/verbosity [terse|normal|verbose]`` — manage channel verbosity default.

    Usage:
        /verbosity              show the channel's current setting
        /verbosity terse        store ``terse`` as the channel default
        /verbosity normal       store ``normal`` as the channel default
        /verbosity verbose      store ``verbose`` as the channel default

    Anything else returns a usage hint. The stored default applies to every
    question in this channel that doesn't carry an explicit prefix (Plan #31
    E2 resolution order: prefix > channel pref > module default).

    Handler ack()s immediately (Slack's 3-second budget), then performs the
    DB read/write and responds ephemerally so we don't spam the channel.
    The pure handler body (:func:`_handle_verbosity_command`) catches DB
    outages and returns a user-visible warning string, so Slack receives a
    final response and won't redeliver the command. Bolt-side ``ack`` and
    ``respond`` calls have their own try/except so transport failures never
    crash the Socket Mode loop.

    Registered with the Bolt app via
    ``app.command("/verbosity")(on_verbosity_command)`` below — module-level
    function so unit tests can call it directly without going through Bolt's
    adapter (which under MagicMock would replace it with a Mock).
    """
    try:
        ack()
    except Exception:
        log.exception("/verbosity: ack() failed")

    raw_text = ""
    channel_id = ""
    user_id = ""
    try:
        cmd = command or {}
        raw_text = (cmd.get("text", "") or "").strip().lower()
        channel_id = cmd.get("channel_id", "") or ""
        user_id = cmd.get("user_id", "") or ""
    except Exception:
        log.exception("/verbosity: failed to parse command payload")

    message = _handle_verbosity_command(raw_text, channel_id, user_id)

    try:
        # Ephemeral response — preferences are per-channel admin, not a
        # public announcement.
        respond(text=message, response_type="ephemeral")
    except Exception:
        log.exception("/verbosity: respond() failed")


def _handle_verbosity_command(raw_text: str, channel_id: str, user_id: str) -> str:
    """Pure handler body — parses args, performs DB ops, returns mrkdwn.

    Split out so unit tests don't need to mock Bolt's ``ack``/``respond``.
    """
    if not channel_id:
        return (
            ":warning: `/verbosity` must be run inside a channel — the channel "
            "id was missing from the payload."
        )

    # No args → show current setting (channel pref or module default).
    if not raw_text:
        current = None
        try:
            import db_adapter

            current = db_adapter.get_channel_verbosity(channel_id)
        except Exception as e:
            log.debug(f"/verbosity: get_channel_verbosity failed: {e}")
        if current in VALID_VERBOSITIES:
            return (
                f"Channel verbosity is *{current}*. "
                f"Change it with `/verbosity terse|normal|verbose`."
            )
        return (
            f"No channel verbosity set — using the default *{DEFAULT_VERBOSITY}*. "
            f"Set one with `/verbosity terse|normal|verbose`."
        )

    # First token wins. Slack passes args separated by whitespace.
    arg = raw_text.split()[0] if raw_text else ""
    if arg not in VALID_VERBOSITIES:
        return (
            f":warning: Unknown verbosity `{arg}`. "
            f"Allowed: `{', '.join(VALID_VERBOSITIES)}`."
        )

    try:
        import db_adapter

        db_adapter.set_channel_verbosity(channel_id, arg, user_id or None)
    except Exception:
        log.exception("/verbosity: set_channel_verbosity failed")
        return (
            ":warning: Couldn't save verbosity — DB write failed. "
            "Check orchestrator logs."
        )

    return f":white_check_mark: Channel verbosity set to *{arg}*."


# Register the /verbosity slash command. Same module-level pattern as
# on_cost_command above.
app.command("/bot-verbosity")(on_verbosity_command)


# ─────────────────────────────────────────────────────────────────────────────
# Plan #33 F9 — /refresh-surface slash command (admin-only manual sync).
# ─────────────────────────────────────────────────────────────────────────────


def _parse_admin_user_ids() -> list[str]:
    """Parse ``SLACK_ADMIN_USER_IDS`` env var (comma-separated Slack user IDs).

    Read at call time (not import time) so tests can monkey-patch
    ``os.environ`` without re-importing the module. Empty / unset means
    nobody is admin — every ``/refresh-surface`` invocation will be denied,
    which is the right safe default.
    """
    import os

    raw = os.environ.get("SLACK_ADMIN_USER_IDS", "") or ""
    return [uid.strip() for uid in raw.split(",") if uid.strip()]


def on_refresh_surface_command(ack, command, respond):
    """``/refresh-surface`` slash command — manual Canvas refresh trigger.

    Plan #33 F9 — admin-only surface trigger. Plays the same role as a
    repair / debug button when the daily cron or reaction-driven sync
    hasn't fired (e.g. the operator wants to see the new state right now).

    Surface:
        /refresh-surface              # current channel's portco
        /refresh-surface <portco_key> # explicit portco override

    Authorization:
        Caller must appear in ``SLACK_ADMIN_USER_IDS`` (comma-separated
        Slack user IDs, same idiom as the cost digest's admin DMs).
        Non-admin users get an ephemeral warning and the command no-ops.

    Execution is synchronous (NOT off-thread like the reaction trigger):
    the user is waiting on the ephemeral response, so blocking until
    push_to_canvas returns gives them an immediate success/failure
    signal. The push is wrapped in try/except so a Canvas API failure
    surfaces as a clean error message instead of a Bolt traceback.

    Module-level (not wrapped in the decorator) so unit tests can call
    this directly without going through Bolt's adapter (which under
    MagicMock would replace the function with a Mock).
    """
    try:
        ack()
    except Exception:
        log.exception("/refresh-surface: ack() failed")

    cmd = command or {}
    user_id = cmd.get("user_id", "") or ""
    channel_id = cmd.get("channel_id", "") or ""
    text = (cmd.get("text", "") or "").strip()

    # Auth check — admin gate.
    admins = _parse_admin_user_ids()
    if not user_id or user_id not in admins:
        log.warning(
            f"/refresh-surface: rejecting non-admin user {user_id!r} "
            f"(channel={channel_id!r})"
        )
        try:
            respond(
                text=(
                    ":no_entry: `/refresh-surface` is admin-only. "
                    "Ask an operator to add your user ID to "
                    "`SLACK_ADMIN_USER_IDS`."
                ),
                response_type="ephemeral",
            )
        except Exception:
            log.exception("/refresh-surface: respond() failed (denial path)")
        return

    # Resolve portco — explicit arg wins over channel lookup.
    portco_key = ""
    if text:
        portco_key = text.split()[0].strip().lower()
    else:
        try:
            import portco_registry

            pc = portco_registry.get_portco_by_channel(channel_id)
            if pc:
                portco_key = pc.get("key", "") or ""
        except Exception:
            log.exception(
                f"/refresh-surface: portco resolve failed for channel {channel_id!r}"
            )

    if not portco_key:
        try:
            respond(
                text=(
                    ":warning: Couldn't resolve a portco for this channel. "
                    "Pass an explicit key: `/refresh-surface <portco_key>`."
                ),
                response_type="ephemeral",
            )
        except Exception:
            log.exception("/refresh-surface: respond() failed (no-portco path)")
        return

    # Synchronous push — user is waiting on the response. Lazy-imported
    # because F6 may not be on main yet; absence is a clean failure rather
    # than an import-time crash.
    try:
        from surface_pusher import push_to_canvas

        push_to_canvas(portco_key)
        log.info(f"/refresh-surface: pushed portco={portco_key} (user={user_id})")

        # Plan #49 — also refresh the channel description in the same
        # command. The push is synchronous (user is waiting), but its
        # failure is swallowed so a setPurpose error doesn't override
        # the successful Canvas push response.
        try:
            from channel_descriptions import push_channel_description

            push_channel_description(channel_id)
            log.info(
                "/refresh-surface: also pushed channel desc for %s",
                channel_id,
            )
        except Exception:
            log.exception(
                "[CHANNEL_DESC_FAILED] /refresh-surface description push for "
                "channel=%s (user=%s) — Canvas push succeeded, continuing",
                channel_id,
                user_id,
            )

        respond(
            text=(
                f":white_check_mark: Surface refreshed for `{portco_key}`. "
                "Canvas should show the new state momentarily."
            ),
            response_type="ephemeral",
        )
    except Exception:
        log.exception(
            f"[SURFACE_PUSH_FAILED] /refresh-surface for portco={portco_key} "
            f"(user={user_id})"
        )
        try:
            respond(
                text=(
                    f":x: `/refresh-surface` failed for `{portco_key}`. "
                    "Check the orchestrator logs for the traceback."
                ),
                response_type="ephemeral",
            )
        except Exception:
            log.exception("/refresh-surface: respond() failed (error path)")


app.command("/bot-refresh-surface")(on_refresh_surface_command)


# ─────────────────────────────────────────────────────────────────────────────
# Plan #44 Task #10 — /pin admin slash command (decision row #20).
#
# Pins a specific agent to a specific version on the next session.create.
# Stored in Postgres (``session_pin_overrides`` table) so the override
# survives Railway redeploys — see ``orchestrator/version_pin_overrides.py``
# and ``orchestrator/migrations/00AF_session_pin_overrides.sql`` for the
# full rationale.
#
# Bundle B's ``session_runner.py`` is expected to import
# ``version_pin_overrides.effective_pin`` at session create. Until that
# wire-up lands the override row will be written but ignored by the SDK
# call (next session uses the file pin). The Slack command and DB write
# work today; the consumer side ships with Bundle B.
# ─────────────────────────────────────────────────────────────────────────────


# Whitelist of agent short names ``/pin`` will accept. Kept in sync with
# ``agents/update_prompts.py:AGENTS`` (the rollback-agent script's source
# of truth). Listed verbatim here so /pin doesn't import agents/ at slash
# command resolve time — the import surface is heavier than we want
# inside a 3-second Slack ack budget.
PIN_ALLOWED_AGENTS = (
    "coordinator",
    "quick_answer",
    "dream",
    "pipeline_monitor",
    "sales_monitor",
    "postsales_monitor",
    "statistician",
    "chart_designer",
    "adversarial_reviewer",
    "cross_domain_synthesizer",
    "writing_agent",
)


def on_pin_command(ack, command, respond):
    """``/pin <agent> <version>`` — admin-only hot pin override.

    Surface:
        /pin coordinator 35     # pin coordinator to v35 on next session
        /pin                    # show usage hint

    Validation:
        - caller must be in ``SLACK_ADMIN_USER_IDS``
        - ``<agent>`` must be in :data:`PIN_ALLOWED_AGENTS`
        - ``<version>`` must be a positive integer

    On success: upserts a row into ``session_pin_overrides`` via
    :func:`version_pin_overrides.set_override`. The very next session
    create on that agent picks up the override (assuming Bundle B's
    session_runner wire-up has landed).

    Replies ephemerally (not in-channel) — pin overrides are operator
    state, not user-facing announcements.
    """
    try:
        ack()
    except Exception:
        log.exception("/pin: ack() failed")

    cmd = command or {}
    user_id = (cmd.get("user_id") or "").strip()
    text = (cmd.get("text") or "").strip()

    # Admin gate — same idiom as /refresh-surface.
    admins = _parse_admin_user_ids()
    if not user_id or user_id not in admins:
        log.warning(f"/pin: rejecting non-admin user {user_id!r} (text={text!r})")
        try:
            respond(
                text=(
                    ":no_entry: `/pin` is admin-only. Ask an operator to "
                    "add your user ID to `SLACK_ADMIN_USER_IDS`."
                ),
                response_type="ephemeral",
            )
        except Exception:
            log.exception("/pin: respond() failed (denial)")
        return

    message = _handle_pin_command(text, user_id)
    try:
        respond(text=message, response_type="ephemeral")
    except Exception:
        log.exception("/pin: respond() failed")


def _handle_pin_command(raw_text: str, user_id: str) -> str:
    """Pure handler body — parse + validate + DB write. Unit-tested
    directly without going through Bolt's ack/respond plumbing."""
    parts = (raw_text or "").split()
    if len(parts) < 2:
        return (
            "Usage: `/pin <agent> <version>`.\n"
            f"Allowed agents: `{', '.join(PIN_ALLOWED_AGENTS)}`."
        )

    agent = parts[0].strip().lower()
    version_raw = parts[1].strip()

    if agent not in PIN_ALLOWED_AGENTS:
        return (
            f":warning: Unknown agent `{agent}`. "
            f"Allowed: `{', '.join(PIN_ALLOWED_AGENTS)}`."
        )

    try:
        version = int(version_raw)
    except ValueError:
        return (
            f":warning: Version `{version_raw}` is not an integer. "
            "Use a positive int like `35`."
        )

    if version <= 0:
        return f":warning: Version must be a positive integer (got `{version}`)."

    try:
        import version_pin_overrides

        ok = version_pin_overrides.set_override(agent, version, user_id)
    except Exception:
        log.exception("/pin: set_override raised")
        ok = False

    if not ok:
        return (
            ":warning: Couldn't save pin override — DB write failed. "
            "Check orchestrator logs."
        )

    # Plan #44 decision row #4 — invalidate the thread-session cache so the
    # next follow-up in any thread targeting this agent starts a fresh
    # session pinned at the new version. Without this, in-flight threads
    # would keep responding from the OLD pinned version until they expire.
    try:
        from session_runner import invalidate_thread_session_cache_for_agent

        invalidate_thread_session_cache_for_agent(agent)
    except Exception:
        log.exception(
            "/pin: thread-session cache invalidation raised (override still applied)"
        )

    return (
        f":white_check_mark: Pinned `{agent}` → v{version}; next session.create "
        f"uses this. Override stored in `session_pin_overrides`. "
        f"Thread-session cache invalidated."
    )


app.command("/bot-pin")(on_pin_command)


# ─────────────────────────────────────────────────────────────────────────────
# Plan #44 Task #15 — /stop admin slash command (decision row #24).
#
# Emits ``user.interrupt`` to halt a running session in-thread. Reply is
# the forensics-ready WHAT+WHY+FIX template: session ID, tokens burned,
# estimated cost, and a pre-filled rollback command in case the runaway
# prompt was the cause.
#
# Bundle E ships the slash command + helper module
# (``session_interrupt.py``). Bundle B's ``session_runner.py`` already
# emits ``user.interrupt`` for cancellation via the meta-intent router;
# the explicit /stop adds an admin-driven path that doesn't require a
# matching meta-intent keyword.
# ─────────────────────────────────────────────────────────────────────────────


def on_stop_command(ack, command, respond):
    """``/stop [thread_ts]`` — admin OR thread-author session interrupt.

    Surface:
        /stop                       # stop session attached to current thread
        /stop 1737654321.000100     # stop session attached to that thread_ts

    Authorization:
        - admin (``SLACK_ADMIN_USER_IDS``), OR
        - the original author of the investigation row attached to the
          thread (``investigations.user_id`` match).

    Reply (ephemeral):
        ":octagonal_sign: Stopped session `<id>`; <N> tokens burned
         ($<C>). If a runaway prompt caused this, roll back with:
         `bin/rollback-agent.py <agent> --to-version <prior>`"

    Gracefully degrades when ``STOP_COMMAND_ENABLED`` is "false" (env or
    flag-override): replies with a hint pointing the operator at /flag.
    """
    try:
        ack()
    except Exception:
        log.exception("/stop: ack() failed")

    cmd = command or {}
    user_id = (cmd.get("user_id") or "").strip()
    channel_id = (cmd.get("channel_id") or "").strip()
    text = (cmd.get("text") or "").strip()
    # Slack also passes the thread_ts in the command payload when the
    # user runs /stop INSIDE a thread; fall through to argv otherwise.
    current_thread_ts = (cmd.get("thread_ts") or "").strip()

    message = _handle_stop_command(text, user_id, channel_id, current_thread_ts)
    try:
        respond(text=message, response_type="ephemeral")
    except Exception:
        log.exception("/stop: respond() failed")


def _stop_command_enabled() -> bool:
    """Read the STOP_COMMAND_ENABLED flag, honoring Postgres overrides
    written by `/flag`. Defaults to True per .env.example."""
    try:
        import flag_overrides

        value = flag_overrides.get_flag("STOP_COMMAND_ENABLED", "true")
    except Exception:
        import os

        value = os.environ.get("STOP_COMMAND_ENABLED", "true")
    return (value or "true").strip().lower() not in ("false", "0", "no", "off")


def _handle_stop_command(
    raw_text: str, user_id: str, _channel_id: str, current_thread_ts: str
) -> str:
    """Pure handler body for /stop. Unit-tested directly.

    ``_channel_id`` is leading-underscore: accepted for symmetry with the
    Bolt-side caller (which has ``channel_id`` available) but unused by the
    pure handler body. Reserved for future emoji-update plumbing.
    """
    if not _stop_command_enabled():
        return (
            ":warning: `/stop` is disabled via `STOP_COMMAND_ENABLED=false`. "
            "Re-enable with `/flag STOP_COMMAND_ENABLED true` (admin only)."
        )

    # Resolve target thread_ts. Explicit arg wins over the slash command's
    # implicit thread context.
    target_ts = (raw_text.split()[0] if raw_text else "") or current_thread_ts
    if not target_ts:
        return (
            "Usage: `/stop` (in a thread) or `/stop <thread_ts>`.\n"
            "The thread must have an active investigation."
        )

    # Auth ordering (closing-review MEDIUM #5, 2026-05-13):
    #
    #   1. Check admin FIRST — no DB read.
    #   2. If non-admin, THEN read the investigation row and check
    #      author match.
    #
    # The old ordering read the row first, then checked auth, which leaked
    # "this thread has an investigation" to non-authorized users via the
    # error-message divergence (warning-no-investigation vs no-entry-not-
    # authorized). The new ordering returns the same auth-denied message
    # regardless of thread state for non-admin non-author callers.
    admins = _parse_admin_user_ids()
    is_admin = bool(user_id and user_id in admins)

    investigation = None
    if is_admin:
        # Admin bypasses author check — still need the row for session_id
        # and status. Do the read here so admins get the same lookups.
        try:
            import db_adapter

            investigation = db_adapter.get_investigation_for_thread(target_ts)
        except Exception:
            log.exception(f"/stop: get_investigation_for_thread({target_ts}) failed")

        if not investigation:
            return (
                f":warning: No investigation found for thread `{target_ts}`. "
                "Already finished or never tracked."
            )
    else:
        # Non-admin path. Read the row, then enforce author match. If the
        # caller is neither admin nor author, we return the no-entry
        # message — same string whether the row exists or not, so the
        # error-message diff stops being an oracle for thread existence.
        try:
            import db_adapter

            investigation = db_adapter.get_investigation_for_thread(target_ts)
        except Exception:
            log.exception(f"/stop: get_investigation_for_thread({target_ts}) failed")

        inv_user = (investigation or {}).get("user_id") or ""
        is_author = bool(user_id and inv_user and user_id == inv_user)

        if not is_author:
            # Same response for "no investigation" and "not authorized"
            # when called by a non-admin — leak shut. Admins still see
            # the differentiated warning above so 2am incident triage
            # isn't impeded.
            return (
                ":no_entry: `/stop` requires admin (`SLACK_ADMIN_USER_IDS`) or "
                "the original investigation author."
            )

        # Author path: must still have a row to interrupt against.
        if not investigation:
            return (
                f":warning: No investigation found for thread `{target_ts}`. "
                "Already finished or never tracked."
            )

    session_id = investigation.get("session_id") or ""
    status = investigation.get("status") or ""

    if not session_id:
        return (
            f":warning: Investigation `{investigation.get('id')}` has no "
            "session_id — nothing to interrupt."
        )

    if status in ("completed", "failed", "cancelled"):
        return (
            f"Investigation `{investigation.get('id')}` is already "
            f"`{status}`. No interrupt sent."
        )

    # Send the interrupt.
    try:
        import session_interrupt

        result = session_interrupt.interrupt_session(session_id)
    except Exception:
        log.exception(f"/stop: interrupt_session({session_id}) raised")
        return (
            f":x: Failed to interrupt session `{session_id}`. Check orchestrator logs."
        )

    if not result.get("ok"):
        return (
            f":x: Interrupt failed for `{session_id}`: "
            f"{result.get('error') or 'unknown error'}"
        )

    # Lifecycle terminalization (2026-05-13). Pre-refactor /stop sent the
    # interrupt but never touched the user's lifecycle reaction emoji,
    # leaving it stuck on ⏰ even after the session was killed. Now flip
    # the user's message to ❌ and mark investigations.status='cancelled'
    # (distinct from 'failed' so /cost and recovery filters can exclude
    # user-initiated stops from failure-rate metrics).
    try:
        from lifecycle import DeliveryState, terminalize_lifecycle

        terminalize_lifecycle(
            DeliveryState.USER_CANCELLED,
            event_ts=investigation.get("event_ts"),
            channel_id=investigation.get("channel_id"),
            inv_id=investigation.get("id"),
            error_message=f"user_cancelled_via_stop:by_{user_id}",
        )
    except Exception:
        log.exception(
            f"/stop: lifecycle terminalization failed for inv_id={investigation.get('id')}; "
            "interrupt succeeded but emoji may still be stuck on ⏰"
        )

    tokens = int(result.get("tokens_burned", 0) or 0)
    cost = float(result.get("cost_usd", 0.0) or 0.0)

    # WHAT + WHY + FIX (decision row #24): pre-fill the rollback so the
    # operator can flip a runaway prompt back to its prior version.
    agent_id = investigation.get("agent_id") or ""
    agent_short = _agent_short_from_id(agent_id)
    rollback_cmd = (
        f"bin/rollback-agent.py {agent_short or '<agent>'} --to-version <prior>"
    )

    return (
        f":octagonal_sign: Stopped session `{session_id}`; "
        f"{tokens:,} tokens burned (~${cost:.4f}).\n"
        f"If a runaway prompt caused this, roll back with: `{rollback_cmd}`"
    )


def _agent_short_from_id(agent_id: str) -> str:
    """Best-effort reverse lookup of agent short name from Anthropic ID.

    Returns "" when the lookup fails — the slash command falls back to
    the literal placeholder ``<agent>`` so the operator still gets a
    valid template they can fix by hand.

    Reads env vars + an embedded hardcoded mapping rather than importing
    ``agents/update_prompts``; that module's import-time ``.env`` loader
    is fragile in test environments (and we don't need the model
    metadata, only the short-name reverse lookup).
    """
    import os

    if not agent_id:
        return ""
    try:
        env_map = {
            "coordinator": os.environ.get("COORDINATOR_ID", ""),
            "quick_answer": os.environ.get("QUICK_ANSWER_ID", "")
            or os.environ.get("QUICK_AGENT_ID", ""),
            "dream": os.environ.get("DREAM_AGENT_ID", ""),
            "pipeline_monitor": os.environ.get("PIPELINE_MONITOR_ID", ""),
            "sales_monitor": os.environ.get("SALES_MONITOR_ID", ""),
            "postsales_monitor": os.environ.get("POSTSALES_MONITOR_ID", ""),
            "statistician": os.environ.get("STATISTICIAN_ID", ""),
            "chart_designer": os.environ.get("CHART_DESIGNER_ID", ""),
            "adversarial_reviewer": os.environ.get("ADVERSARIAL_REVIEWER_ID", ""),
            "cross_domain_synthesizer": os.environ.get(
                "CROSS_DOMAIN_SYNTHESIZER_ID", ""
            ),
            "writing_agent": os.environ.get("WRITING_AGENT_ID", ""),
        }
        hardcoded = {
            "agent_EXAMPLE_coordinator": "coordinator",
            "agent_EXAMPLE_quick_answer": "quick_answer",
            "agent_EXAMPLE_dream": "dream",
            "agent_EXAMPLE_pipeline_monitor": "pipeline_monitor",
            "agent_EXAMPLE_sales_monitor": "sales_monitor",
            "agent_EXAMPLE_postsales_monitor": "postsales_monitor",
            "agent_EXAMPLE_statistician": "statistician",
            "agent_EXAMPLE_chart_designer": "chart_designer",
            "agent_EXAMPLE_adversarial_reviewer": "adversarial_reviewer",
            "agent_EXAMPLE_cross_domain_synthesizer": "cross_domain_synthesizer",
        }
        for short, env_id in env_map.items():
            if env_id and env_id == agent_id:
                return short
        if agent_id in hardcoded:
            return hardcoded[agent_id]
    except Exception:
        log.debug("/stop: agent short-name lookup failed", exc_info=True)
    return ""


app.command("/bot-stop")(on_stop_command)


# ─────────────────────────────────────────────────────────────────────────────
# Plan #44 Task #24 — /flag admin slash command (decision row #25).
#
# Flips in-process feature flags via Postgres overrides
# (``flag_overrides`` table). Operator can change behavior at 2am during
# an incident without reaching the Railway dashboard.
#
# Bundle B's ``config.py`` is expected to read each flag via
# :func:`flag_overrides.get_flag` so a Slack write hot-applies. Bundle E
# ships only the override table + Slack command; the consumer side is
# Bundle B.
# ─────────────────────────────────────────────────────────────────────────────


# Whitelist of flag names ``/flag`` accepts. Each value carries a
# normalizer so we reject obvious typos at the command layer rather than
# storing a useless value.
def _normalize_bool(v: str) -> tuple[bool, str]:
    """(ok, normalized). Accepts true/false/yes/no/on/off/1/0."""
    val = (v or "").strip().lower()
    if val in ("true", "1", "yes", "on"):
        return True, "true"
    if val in ("false", "0", "no", "off"):
        return True, "false"
    return False, v


def _normalize_int(v: str) -> tuple[bool, str]:
    """(ok, normalized). Stored as the integer's str form."""
    try:
        i = int((v or "").strip())
        return True, str(i)
    except (TypeError, ValueError):
        return False, v


def _normalize_pct(v: str) -> tuple[bool, str]:
    """Integer 0..100 inclusive."""
    ok, norm = _normalize_int(v)
    if not ok:
        return False, v
    try:
        i = int(norm)
        if 0 <= i <= 100:
            return True, str(i)
    except ValueError:
        pass
    return False, v


def _normalize_enum_smoke_probe(v: str) -> tuple[bool, str]:
    """Allowed levels for ``SMOKE_PROBE_LEVEL``.

    Per Plan #44 Task #20 (decision row, ``docs/plans/44-...:534``) the
    smoke probe has two real modes:

      ``quick`` — the current Plan #42 PR2 probe (boot / MCP /
                  Anthropic). Default.
      ``full``  — Plan #44 Task #20 extension that also fires a trivial
                  Coordinator turn to exercise the multiagent path.

    ``off`` is accepted as a synonym for "disabled" so the smoke probe
    can be skipped on a preview environment without removing the env var.
    Closing-review HIGH #2 (2026-05-13): the prior ``off|shallow|deep``
    vocabulary did not match the plan or the consumer in
    ``orchestrator/smoke_probe.py``.
    """
    val = (v or "").strip().lower()
    if val in ("off", "quick", "full"):
        return True, val
    return False, v


# Initial whitelist per decision row #25. Each entry maps flag name to
# (validator, human description). Future flags need only a row here.
FLAG_ALLOWED: dict[str, tuple] = {
    "SMOKE_PROBE_LEVEL": (
        _normalize_enum_smoke_probe,
        "off | quick | full",
    ),
    "SF_MCP_VIA_VAULT": (_normalize_bool, "true | false"),
    "STOP_COMMAND_ENABLED": (_normalize_bool, "true | false"),
    "LIMITED_NETWORKING_SHADOW_PCT": (_normalize_pct, "integer 0..100"),
    "COMPRESSION_ENABLED": (_normalize_bool, "true | false"),
}


def on_flag_command(ack, command, respond):
    """``/flag <NAME> <value>`` — admin-only Postgres-backed flag flip.

    Surface:
        /flag SMOKE_PROBE_LEVEL full
        /flag COMPRESSION_ENABLED false
        /flag                              # list flags + current values
        /flag SMOKE_PROBE_LEVEL            # show one flag's current value

    Validation per-flag — see :data:`FLAG_ALLOWED`. Anything outside the
    whitelist is rejected so a typo doesn't store a bogus value.
    """
    try:
        ack()
    except Exception:
        log.exception("/flag: ack() failed")

    cmd = command or {}
    user_id = (cmd.get("user_id") or "").strip()
    text = (cmd.get("text") or "").strip()

    admins = _parse_admin_user_ids()
    if not user_id or user_id not in admins:
        log.warning(f"/flag: rejecting non-admin user {user_id!r} (text={text!r})")
        try:
            respond(
                text=(
                    ":no_entry: `/flag` is admin-only. Ask an operator to "
                    "add your user ID to `SLACK_ADMIN_USER_IDS`."
                ),
                response_type="ephemeral",
            )
        except Exception:
            log.exception("/flag: respond() failed (denial)")
        return

    message = _handle_flag_command(text, user_id)
    try:
        respond(text=message, response_type="ephemeral")
    except Exception:
        log.exception("/flag: respond() failed")


def _handle_flag_command(raw_text: str, user_id: str) -> str:
    """Pure handler body for /flag. Unit-tested directly."""
    parts = (raw_text or "").split()

    # No args → list every whitelisted flag with its effective value.
    if not parts:
        lines = ["Flag overrides (DB > env > default):"]
        for name, (_v, desc) in FLAG_ALLOWED.items():  # pyright: ignore[reportUnusedVariable]
            current = _read_flag_for_display(name)
            lines.append(f"  • `{name}` = `{current}`  _({desc})_")
        lines.append("\nSet: `/flag <NAME> <value>` — see allowed values above.")
        return "\n".join(lines)

    name = parts[0].strip()
    if name not in FLAG_ALLOWED:
        return (
            f":warning: Flag `{name}` is not in the whitelist. "
            f"Allowed: `{', '.join(FLAG_ALLOWED.keys())}`."
        )

    # Single-arg form → show this flag's current value.
    if len(parts) == 1:
        current = _read_flag_for_display(name)
        _v, desc = FLAG_ALLOWED[name]  # pyright: ignore[reportUnusedVariable]
        return f"`{name}` = `{current}`  _({desc})_\nSet with `/flag {name} <value>`."

    value_raw = " ".join(parts[1:]).strip()
    validator, desc = FLAG_ALLOWED[name]
    ok, normalized = validator(value_raw)
    if not ok:
        return (
            f":warning: Value `{value_raw}` rejected for `{name}`. Allowed: `{desc}`."
        )

    try:
        import flag_overrides

        wrote = flag_overrides.set_flag(name, normalized, user_id)
    except Exception:
        log.exception("/flag: set_flag raised")
        wrote = False

    if not wrote:
        return (
            ":warning: Couldn't save flag override — DB write failed. "
            "Check orchestrator logs."
        )

    return (
        f":white_check_mark: `{name}` = `{normalized}` (was env / "
        "default). Next config read picks this up."
    )


def _read_flag_for_display(name: str) -> str:
    """Resolve the flag's current value for display purposes only.

    Mirrors :func:`flag_overrides.get_flag` but defaults to "unset" when
    nothing is configured so the listing is readable.
    """
    try:
        import flag_overrides

        return flag_overrides.get_flag(name, "<unset>")
    except Exception:
        import os

        return os.environ.get(name, "<unset>")


app.command("/bot-flag")(on_flag_command)


@app.event("app_mention")
def handle_mention(event, say):
    text = event.get("text", "").strip()
    user = event.get("user", "unknown")
    thread_ts = event.get("thread_ts") or event.get("ts")
    _handle_incoming(
        text,
        user,
        thread_ts,
        say,
        event_ts=event.get("event_ts") or event.get("ts"),
        channel_id=event.get("channel"),
    )


def _route_file_share_event(event: dict, say) -> bool:
    """Route a Slack ``file_share`` event to the RFP runner.

    Returns ``True`` when the event was claimed (RFP runner was
    dispatched or the event was deduped); ``False`` when the caller
    should fall through to the normal subtype-drop. The helper is
    extracted from ``handle_message`` so it can be unit-tested
    directly — the @app.event decorator in test mode is stubbed by
    a MagicMock, which would otherwise make the decorated handler
    uncallable from tests.

    Filters / gates applied in order:

    1. **Bot filter.** Skip events whose author surfaces as either
       ``bot_id`` or ``bot_profile``. Slack workflows / app
       integrations sometimes only set ``bot_profile`` (see the
       reaction handler below for the same dual-check pattern).
       Without the dual check, an integration that drops a file
       into the RFP channel would trigger a spurious RFP session.

    2. **Dedup.** Slack redelivers ``message`` events on its own
       retry cadence. The text path consults ``_seen_events`` in
       ``_handle_incoming``; the file_share path does its own
       dedup here so a redelivered upload doesn't spawn a second
       session (and a second ack + second cost row). Keyed on
       ``event_ts`` to match the text-path dedup key shape.

    3. **Channel gate.** Lazy-imports ``rfp_runner`` and consults
       ``is_rfp_channel`` — only file uploads in the configured
       ``RFP_CHANNEL_ID`` get routed. Empty env var degrades the
       whole feature to no-op.
    """
    if event.get("bot_id") or event.get("bot_profile"):
        return False

    event_ts = event.get("event_ts") or event.get("ts") or ""
    dedup_key = f"file_share:{event_ts}"
    if event_ts and dedup_key in _seen_events:
        return True

    try:
        # Lazy import: keeps slack_bot importable in test environments
        # where rfp_runner's httpx-based deps may not be installed.
        # ``type: ignore`` guards against Pyright cache lag on
        # newly-added sibling modules in ``orchestrator/``.
        import rfp_runner  # type: ignore[import-not-found]

        if not rfp_runner.is_rfp_channel(event.get("channel")):
            return False

        # Record dedup AFTER channel match so non-RFP-channel file
        # uploads don't pollute the cache.
        if event_ts:
            _seen_events[dedup_key] = None
            while len(_seen_events) > _DEDUP_MAX:
                _seen_events.pop(next(iter(_seen_events)))
        rfp_runner.handle_rfp_message(event, say)
        return True
    except Exception:
        log.exception("RFP routing failed — falling through")
        return False


def _has_active_meta_intent_followup(text: str, thread_ts: Optional[str]) -> bool:
    """Plan #52 PR-B (codex P2 fix): true when a short, no-mention thread
    reply should bypass the @mention gate and reach ``_handle_meta_intent``.

    Conditions ALL required:
    - ``thread_ts`` is set (we are in a thread)
    - text classifies as a meta-intent (status / cancel / pause / resume)
    - an in-flight or recently-finished investigation exists on this
      thread (``_lookup_thread_investigation`` returns non-None)

    Without all three, fall through to the original @mention gate. This
    keeps the broadening narrow — random short replies in unrelated
    portco-channel threads still get dropped.
    """
    if not thread_ts or not text:
        return False
    if classify_meta_intent(text) is None:
        return False
    investigation = _lookup_thread_investigation(thread_ts)
    return investigation is not None


@app.event("message")
def handle_message(event, say):
    subtype = event.get("subtype")

    # RFP intake: a ``file_share`` in the dedicated RFP channel goes to
    # the RFP runner instead of the question pipeline. Routed BEFORE
    # the generic subtype short-circuit below so file uploads aren't
    # silently dropped. The runner returns immediately after spawning
    # a daemon thread, keeping the Bolt handler well under the 3s
    # budget. Lazy-imported so a missing ``RFP_RESPONDER_ID`` /
    # ``RFP_CHANNEL_ID`` env var degrades gracefully — the import
    # succeeds, ``is_rfp_channel`` returns False, and we fall through.
    if subtype == "file_share":
        if _route_file_share_event(event, say):
            return

    if subtype:
        return

    channel_type = event.get("channel_type", "")
    text = event.get("text", "").strip()

    # DMs: handle all non-bot messages (existing behavior)
    if channel_type == "im":
        if event.get("bot_id"):
            return
        user = event.get("user", "unknown")
        thread_ts = event.get("ts")
        _handle_incoming(
            text,
            user,
            thread_ts,
            say,
            event_ts=event.get("event_ts") or event.get("ts"),
            channel_id=event.get("channel"),
        )
        return

    # Channel messages: normally we require an @mention to fire the
    # question pipeline (avoids the bot replying to every line in a
    # portco channel). Exception: the dedicated RFP intake channel
    # behaves like a DM — every message from a human is treated as
    # something the bot should answer, with no @mention required.
    # The bot_id check still applies in both cases to prevent loops.
    if channel_type == "channel" or channel_type == "group":
        if event.get("bot_id") or event.get("bot_profile"):
            return
        channel_id = event.get("channel")
        is_rfp = False
        try:
            import rfp_runner  # type: ignore[import-not-found]

            is_rfp = rfp_runner.is_rfp_channel(channel_id)
        except Exception:
            log.exception(
                "RFP channel resolution failed — falling back to @mention gate"
            )
        user = event.get("user", "unknown")
        thread_ts = event.get("thread_ts") or event.get("ts")
        if not is_rfp:
            if not _bot_user_id or f"<@{_bot_user_id}>" not in text:
                # Plan #52 PR-B (codex P2 fix, 2026-05-19): allow short
                # no-mention thread replies to reach the meta-intent
                # router when there's an active investigation on the
                # thread. Without this, plain "continue" / "status" /
                # "stop" in a portco channel thread is dropped before
                # the `resume` intent can wake the session. The gate
                # is intentionally narrow — token cap + existing-row
                # check + classifier match — so we don't accidentally
                # route random short replies into the question
                # pipeline.
                if not _has_active_meta_intent_followup(text, thread_ts):
                    return
        _handle_incoming(
            text,
            user,
            thread_ts,
            say,
            event_ts=event.get("event_ts") or event.get("ts"),
            channel_id=channel_id,
        )
        return


def handle_reaction_added(event, client=None):
    """Plan #30 D1 — capture emoji reactions on bot-authored messages.

    Logic:
        1. Skip non-message reactions (file/file_comment items).
        2. Map emoji → signal via ``EMOJI_TO_SIGNAL``. Untracked emoji exit
           silently — no DB write, no log spam.
        3. Resolve the source message via ``conversations.history`` and
           confirm the message author is the bot itself. Reactions on
           human-authored messages are not feedback on agent output.
        4. Resolve portco_key from the channel via ``portco_registry``.
           Empty string when the channel isn't a portco channel — we still
           log the row (channel_id is captured).
        5. Call ``feedback_capture.record_feedback`` with ``source="emoji"``.
           That function is idempotent + non-fatal — Slack occasionally
           redelivers ``reaction_added``, and a missing DB must never break
           the Slack handler loop.

    The ``client`` kwarg is Slack Bolt's WebClient. We accept it for
    testability; when missing (older Bolt versions) we fall back to the
    module-level ``app.client``.

    Registered with the Bolt app via ``app.event("reaction_added")
    (handle_reaction_added)`` below — keeping the function module-level
    (not wrapped in the decorator) so unit tests can call it directly
    without going through Bolt's adapter (which under MagicMock would
    replace the function with a Mock).
    """
    try:
        item = event.get("item") or {}
        if item.get("type") != "message":
            return
        emoji = (event.get("reaction") or "").strip()
        signal = EMOJI_TO_SIGNAL.get(emoji)
        if not signal:
            return  # untracked emoji — silent skip

        channel_id = item.get("channel", "")
        message_ts = item.get("ts", "")
        user_id = event.get("user", "")
        if not channel_id or not message_ts:
            return

        # Resolve the source message + author. We need conversations.history
        # rather than conversations.replies because the reaction's item.ts
        # may itself be a top-level message — replies require a thread_ts.
        cli = client or app.client
        bot_authored = False
        thread_ts = message_ts
        try:
            hist = cli.conversations_history(
                channel=channel_id,
                latest=message_ts,
                inclusive=True,
                limit=1,
            )
            messages = hist.get("messages", []) if isinstance(hist, dict) else []
            if not messages and hasattr(hist, "get"):
                messages = hist.get("messages", [])
            if messages:
                src = messages[0]
                src_bot_user = src.get("bot_id") or src.get("bot_profile")
                src_user = src.get("user")
                # The bot may post as a bot integration (bot_id set) or as
                # the bot's own user_id; either way it's "us".
                if _bot_user_id and src_user == _bot_user_id:
                    bot_authored = True
                elif src_bot_user:
                    bot_authored = True
                # Prefer the source message's thread anchor when present.
                thread_ts = src.get("thread_ts") or message_ts
        except Exception as e:
            log.debug(
                f"reaction_added: conversations_history failed for {channel_id}/{message_ts}: {e}"
            )
            return

        if not bot_authored:
            return  # reaction on a human message — not feedback on agent output

        # Resolve portco from channel. Empty string when unknown — we still
        # log the row, the channel_id is the fallback attribution.
        portco_key = ""
        try:
            import portco_registry

            pc = portco_registry.get_portco_by_channel(channel_id)
            if pc:
                portco_key = pc.get("key", "") or ""
        except Exception as e:
            log.debug(f"reaction_added: portco resolve failed for {channel_id}: {e}")

        import feedback_capture

        feedback_capture.record_feedback(
            portco_key=portco_key,
            channel_id=channel_id,
            thread_ts=thread_ts or message_ts,
            user_id=user_id,
            agent_message_ts=message_ts,
            signal=signal,
            source="emoji",
            raw_text=emoji,
        )

        # Plan #33 F9 — fire a surface refresh after the reaction is recorded
        # so the Canvas reflects the new decision state quickly. The push is
        # off-thread (daemon) because we don't want to block the Socket Mode
        # event loop on a Canvas API round-trip. Lazy-imported because F6 may
        # not be on main yet when F9 lands; the try/except keeps the handler
        # functional in that case. ``portco_key`` may be empty for non-portco
        # channels — the pusher is responsible for short-circuiting cleanly.
        if portco_key:
            try:
                from surface_pusher import push_to_canvas
                import threading

                threading.Thread(
                    target=push_to_canvas,
                    args=(portco_key,),
                    daemon=True,
                    name=f"surface-push-reaction-{portco_key}",
                ).start()
            except Exception:
                log.exception(
                    "[SURFACE_PUSH_FAILED] reaction-triggered surface push failed"
                )
    except Exception:
        # The handler must never crash the Socket Mode loop.
        log.exception("reaction_added handler failed (non-fatal)")


# Register the reaction_added handler after the function is defined. Same
# pattern as ``on_cost_command`` above — keeps the function callable by
# unit tests without going through Bolt's decorator adapter.
app.event("reaction_added")(handle_reaction_added)


def handle_member_joined_channel(event, _say=None):
    """Plan #49 — set channel purpose when the bot joins a channel.

    Slack emits ``member_joined_channel`` for every user who joins a
    channel, including the bot itself. We act only when the joining
    member is the bot (``event["user"] == _bot_user_id``) — joins by
    humans are not our concern.

    Dispatch is off the Bolt event loop on a daemon thread, same pattern
    as ``handle_reaction_added``'s surface-push trigger. The Slack event
    ack returns immediately; the purpose write happens in the background
    so a slow ``conversations.info`` or ``conversations.setPurpose``
    round-trip does not stall the Socket Mode loop.

    Startup race guard: ``_bot_user_id`` is resolved in
    ``start_socket_mode()`` via ``auth_test``. If a join event arrives
    before that resolution lands (extremely unlikely in practice), we
    return immediately — the 08:00 PT cron will catch the channel
    within 24 hours regardless.

    The handler must never raise — that would crash the Socket Mode
    loop. All failures are swallowed and logged.
    """
    try:
        if not _bot_user_id or event.get("user") != _bot_user_id:
            return
        channel_id = event.get("channel", "")
        if not channel_id:
            return
        try:
            from channel_descriptions import push_channel_description
            import threading

            t = threading.Thread(
                target=push_channel_description,
                args=(channel_id,),
                daemon=True,
                name=f"channel-desc-{channel_id}",
            )
            t.start()
        except Exception:
            log.exception(
                "[CHANNEL_DESC_FAILED] handle_member_joined_channel: "
                "dispatch failed for %s",
                channel_id,
            )
    except Exception:
        # The handler must never crash the Socket Mode loop.
        log.exception("member_joined_channel handler failed (non-fatal)")


app.event("member_joined_channel")(handle_member_joined_channel)


def _md_to_slack(text: str) -> str:
    """Convert markdown to Slack mrkdwn format.

    Slack uses mrkdwn, not Markdown. Common GitHub/CommonMark constructs that
    don't render natively:

      * ATX headers (``# H``, ``## H``, ``### H``)   → ``*H*`` on its own line.
      * ``**bold**`` (double-asterisk)               → ``*bold*`` (single).
      * ``- bullet`` / ``* bullet`` list markers     → ``• bullet`` (Slack's
                                                       preferred bullet glyph).
      * Pipe tables                                  → fixed-width code blocks.
      * ``---`` horizontal rules                     → unicode divider.

    Constructs Slack handles natively are left alone: backtick code spans,
    fenced code blocks, numbered lists (``1. foo``), URLs, and emoji shortcodes.

    Before the structural conversion, the input runs through
    :func:`prose_polish.polish` to gloss GTM acronyms (NB, ARR, MC, PI, ...)
    at first use and rewrite academic statistics phrasing
    ("Wilcoxon p=0.001", "β = -$71K/qtr") into plain English. That step is
    idempotent and deterministic — see ``prose_polish.py``. This is the
    single chokepoint every free-form analyst report flows through before
    Slack, so the polish lives here rather than at each caller.
    """
    # 0. Plain-English polish pass — acronym gloss + stats-to-prose.
    text = _plain_english(text)

    # Convert markdown tables to code blocks (Slack doesn't render pipe tables)
    lines = text.split("\n")
    result = []
    in_table = False
    table_lines = []

    for line in lines:
        stripped = line.strip()
        is_table_row = (
            stripped.startswith("|")
            and stripped.endswith("|")
            and stripped.count("|") >= 3
        )
        is_separator = bool(re.match(r"^\|[\s\-:]+\|", stripped))

        if is_table_row or is_separator:
            if not in_table:
                in_table = True
                table_lines = []
            if not is_separator:
                table_lines.append(stripped)
        else:
            if in_table:
                result.append("```")
                for tl in table_lines:
                    cells = [c.strip() for c in tl.strip("|").split("|")]
                    result.append("  ".join(f"{c:<20}" for c in cells))
                result.append("```")
                in_table = False
                table_lines = []
            result.append(line)

    if in_table:
        result.append("```")
        for tl in table_lines:
            cells = [c.strip() for c in tl.strip("|").split("|")]
            result.append("  ".join(f"{c:<20}" for c in cells))
        result.append("```")

    text = "\n".join(result)

    # Headers → bold with emoji markers
    text = re.sub(r"^###\s+(.+)$", r"\n*\1*", text, flags=re.MULTILINE)
    text = re.sub(
        r"^##\s+(.+)$", r"\n━━━━━━━━━━━━━━━━━━━━\n*\1*\n", text, flags=re.MULTILINE
    )
    text = re.sub(r"^#\s+(.+)$", r"\n*\1*\n", text, flags=re.MULTILINE)

    # **bold** → *bold*
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)

    # Unordered list markers ``- foo`` / ``* foo`` → ``• foo`` (Slack's
    # preferred bullet glyph). Preserve the leading indentation so nested
    # bullets keep their hierarchy. ``*`` markers must NOT swallow ``**bold**``
    # text — the lookahead requires a whitespace character (not another
    # asterisk) after the marker. ``1. foo`` numbered lists are left alone;
    # Slack renders those natively.
    text = re.sub(
        r"^(?P<indent>[ \t]*)[-*](?=\s)\s+",
        lambda m: f"{m.group('indent')}• ",
        text,
        flags=re.MULTILINE,
    )

    # --- horizontal rules → divider-like text
    text = re.sub(r"^---+$", "━━━━━━━━━━━━━━━━━━━━", text, flags=re.MULTILINE)

    # Remove excessive blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def _build_blocks(text: str, mentions: str = "", severity: str = "info") -> list:
    """Build Slack blocks from analysis text, respecting the 3000 char block limit."""
    blocks = []

    # Split on double newlines to get logical sections
    sections = re.split(r"\n\n+", text)

    current_block = ""
    for section in sections:
        if len(current_block) + len(section) + 2 > 2900:
            if current_block.strip():
                blocks.append(
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": current_block.strip()},
                    }
                )
            current_block = section
        else:
            current_block += "\n\n" + section if current_block else section

    if current_block.strip():
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": current_block.strip()},
            }
        )

    if mentions and severity in ("critical", "watch"):
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": mentions}]},
        )

    return blocks


def post_rich_ack(
    *,
    prompt_plan: dict,
    question: str,
    thread_ts: Optional[str] = None,
    channel_id: Optional[str] = None,
) -> str:
    """Post the Prompt Engineer's plan back to Slack as a rich
    acknowledgment BEFORE the investigation begins.

    The message has four sections:
      1. A one-line restatement of the question (``summary``).
      2. A bulleted ``Plan:`` of 2-5 investigation steps the agent will
         run (``plan_steps``).
      3. A ``Expected output:`` line describing what the user will see
         when the run completes (``expected_output``).
      4. (optional) A ``Caveats:`` block listing any risk flags the
         Prompt Engineer surfaced.

    Falls through to the legacy short ack (``"I'll investigate."``)
    when the Prompt Engineer dict is empty, missing fields, or the
    Prompt Engineer call failed upstream. Never raises — the
    investigation must proceed even if the ack render fails.

    Returns the Slack message timestamp, or ``""`` on any failure.
    """
    try:
        summary = ((prompt_plan or {}).get("summary") or "").strip()
        plan_steps = (prompt_plan or {}).get("plan_steps") or []
        expected = ((prompt_plan or {}).get("expected_output") or "").strip()
        risks = (prompt_plan or {}).get("risk_flags") or []

        # Synthesize a graceful fallback if the dict is empty / missing
        # keys. The user gets something better than "I'll investigate."
        # even when the Prompt Engineer skipped or timed out.
        if not summary and question:
            short_q = (question[:140] + "…") if len(question) > 140 else question
            summary = f"Looking into: {short_q}"
        if not plan_steps:
            plan_steps = [
                "Identify the right data source (Postgres snapshot vs live SF MCP)",
                "Pull the rows the question needs",
                "Validate the answer (Statistician + Adversarial Reviewer)",
                "Reply in this thread with the result",
            ]
        if not expected:
            expected = "A Slack reply in this thread with the answer + any supporting tables/files."

        lines = []
        if summary:
            lines.append(f"_{summary}_")
            lines.append("")
        lines.append("*Plan*")
        for step in plan_steps:
            step_clean = (str(step) or "").strip().rstrip(".")
            if step_clean:
                lines.append(f"• {step_clean}")
        lines.append("")
        lines.append(f"*Expected output*: {expected}")
        if risks:
            lines.append("")
            lines.append("*Caveats*")
            for r in risks:
                r_clean = (str(r) or "").strip().rstrip(".")
                if r_clean:
                    lines.append(f"• {r_clean}")
        body = "\n".join(lines)

        target_channel = channel_id or SLACK_CHANNEL_ID
        if not target_channel:
            log.warning("post_rich_ack: no channel — dropping ack")
            return ""

        rendered = _md_to_slack(body)
        if _placeholder_guard_enabled():
            leak = _check_for_unfilled_placeholders(rendered)
            if leak is not None:
                log.warning(
                    "[PLACEHOLDER_LEAK_BLOCKED] post_rich_ack matched=%r "
                    "thread=%s channel=%s — dropping message",
                    leak,
                    thread_ts,
                    target_channel,
                )
                try:
                    _admin_dm_placeholder_leak("post_rich_ack", leak, body)
                except Exception:
                    log.exception("admin DM about placeholder leak failed")
                return ""

        # Call chat_postMessage with explicit kwargs rather than ``**kwargs``
        # unpacking a dict[str, str]. The unpacking path makes pyright unable
        # to bind the dict values to the typed parameter slots — every
        # ``bool | None`` parameter on ``chat_postMessage`` (as_user,
        # reply_broadcast, unfurl_links, unfurl_media, mrkdwn, link_names,
        # metadata) gets flagged as receiving an incompatible str.
        if thread_ts:
            resp = app.client.chat_postMessage(
                channel=target_channel,
                text=rendered,
                thread_ts=thread_ts,
            )
        else:
            resp = app.client.chat_postMessage(
                channel=target_channel,
                text=rendered,
            )
        return resp.get("ts", "") or ""
    except Exception:
        log.exception("post_rich_ack: failed to post — investigation continues")
        return ""


def _admin_dm_placeholder_leak(source: str, leak: str, body: str) -> None:
    """DM SLACK_ADMIN_USER_IDS about a blocked placeholder leak. Best-effort.

    Caller wraps in try/except; we don't re-raise on Slack failure.
    """
    admin_ids = _parse_admin_user_ids()
    if not admin_ids:
        log.warning(
            "[PLACEHOLDER_LEAK_NO_ADMINS] cannot DM — SLACK_ADMIN_USER_IDS is empty"
        )
        return
    preview = body[:500] + ("…" if len(body) > 500 else "")
    text = (
        f":warning: Blocked an outbound Slack message containing an "
        f"unfilled placeholder.\n\n"
        f"Source: `{source}`\nMatched span: `{leak}`\n\n"
        f"Body preview:\n```\n{preview}\n```"
    )
    for uid in admin_ids:
        try:
            dm = app.client.conversations_open(users=uid)
            channel = (dm.get("channel") or {}).get("id")
            if channel:
                app.client.chat_postMessage(channel=channel, text=text)
        except Exception:
            log.exception("[PLACEHOLDER_LEAK_DM_FAILED] uid=%s — continuing", uid)


def post_analysis(
    title: str,
    analysis_text: str,
    queries: Optional[list] = None,
    reply_to: Optional[str] = None,
    severity: str = "info",
    requester_id: Optional[str] = None,
):
    """Post a full analysis to Slack as a single well-formatted thread."""
    emoji = {
        "critical": ":red_circle:",
        "watch": ":large_yellow_circle:",
        "info": ":large_blue_circle:",
    }.get(severity, ":white_circle:")

    mentions = " ".join(f"<@{uid}>" for uid in SLACK_NOTIFY_USER_IDS if uid)
    slack_text = _md_to_slack(redact_paths(analysis_text))

    # Main message with title
    header_block = {
        "type": "header",
        "text": {"type": "plain_text", "text": f"{title}", "emoji": True},
    }

    if requester_id:
        requester_block = {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"<@{requester_id}> here are your results:"}
            ],
        }
    else:
        requester_block = None

    content_blocks = _build_blocks(slack_text, mentions, severity)

    all_blocks = [header_block]
    if requester_block:
        all_blocks.append(requester_block)
    all_blocks.extend(content_blocks)

    # Slack has a 50 block limit per message
    if len(all_blocks) > 49:
        all_blocks = all_blocks[:49]

    kwargs = {
        "channel": SLACK_CHANNEL_ID,
        "blocks": all_blocks,
        "text": f"{emoji} {title}",
    }
    if reply_to:
        kwargs["thread_ts"] = reply_to

    result = app.client.chat_postMessage(**kwargs)
    main_ts = result["ts"]

    # Post queries as a separate threaded reply
    if queries:
        query_blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "SOQL Queries Run"},
            },
        ]
        query_text = ""
        for i, q in enumerate(queries, 1):
            entry = f"{i}. ```{q}```\n"
            if len(query_text) + len(entry) > 2900:
                query_blocks.append(
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": query_text.strip()},
                    }
                )
                query_text = entry
            else:
                query_text += entry

        if query_text.strip():
            query_blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": query_text.strip()},
                }
            )

        app.client.chat_postMessage(
            channel=SLACK_CHANNEL_ID,
            blocks=query_blocks,
            text=f"SOQL Queries ({len(queries)})",
            thread_ts=reply_to or main_ts,
        )

    return main_ts


def _send_admin_dm_notification(
    severity: str,
    summary: str,
    detail: str = "",
    extra_blocks=None,
) -> str:
    """DM the catastrophic-failure message to every admin in SLACK_ADMIN_USER_IDS.

    Admin-only path for ``send_notification(admin_only=True)``. The public
    channel must NEVER see operational telemetry — those messages are routed
    here. Empty admin list → log a warning and return "" so the calling code
    still gets a falsy timestamp and degrades gracefully.

    Returns the timestamp of the LAST successful DM, or "" if none landed.
    """
    admins = _parse_admin_user_ids()
    if not admins:
        log.warning(
            "send_notification(admin_only=True) called but SLACK_ADMIN_USER_IDS is "
            "empty — dropping message: %s",
            (summary or "")[:120],
        )
        return ""

    emoji = {
        "critical": ":red_circle:",
        "watch": ":large_yellow_circle:",
        "info": ":large_blue_circle:",
    }.get(severity, ":white_circle:")
    label = (severity or "info").upper()

    summary_md = _md_to_slack(summary)
    detail_md = _md_to_slack(detail) if detail else ""

    text_content = f"{emoji} *{label}* — {summary_md}"
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text_content}},
    ]
    if detail_md:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": detail_md}}
        )
    if extra_blocks:
        blocks.extend(extra_blocks)

    last_ts = ""
    for uid in admins:
        try:
            resp = app.client.conversations_open(users=[uid])
            # SlackResponse["channel"] is typed as Any|None; guard before
            # subscripting. Same pattern as the placeholder-leak DM at L2306.
            dm_channel = (resp.get("channel") or {}).get("id")
            if not dm_channel:
                log.warning(
                    "admin-only DM: conversations_open returned no channel id "
                    "for user_id=%s — skipping",
                    uid,
                )
                continue
            result = app.client.chat_postMessage(
                channel=dm_channel,
                blocks=blocks,
                text=f"{emoji} {label}: {summary_md}",
            )
            last_ts = result.get("ts", last_ts)
        except Exception:
            log.exception("admin-only DM failed for user_id=%s", uid)
    return last_ts


def send_notification(
    severity,
    summary,
    detail="",
    reply_to=None,
    channel=None,
    extra_blocks=None,
    admin_only: bool = False,
    requester_id: Optional[str] = None,
):
    """Send a short notification. For full analyses, use post_analysis instead.

    extra_blocks: optional list of Block Kit block dicts (e.g. a `table`
    block from response_renderer.render_payload). They are appended to the
    message after the summary/detail mrkdwn sections and before the
    mention-ping context block. Slack only allows one `table` block per
    message — the renderer enforces this.

    admin_only: when True, ignore ``channel`` / ``reply_to`` and DM every
    user id listed in ``SLACK_ADMIN_USER_IDS``. Reserved for catastrophic
    failures the in-band retry loop cannot recover from — public channels
    must NOT receive operational telemetry. In-band recoverable failures
    (e.g. post_report validation rejection) are handled by the
    dispatcher's retry loop in ``session_runner._dispatch_post_report``
    and never call this path.

    requester_id: Slack user ID of the person who started this thread (the
    original requester of the investigation). When set AND ``reply_to`` is
    non-None (i.e. we're posting into a Slack thread), the mention-ping
    context block @-mentions THIS user instead of the global
    ``SLACK_NOTIFY_USER_IDS`` admin list. Precedence:
    ``requester_id`` > ``SLACK_NOTIFY_USER_IDS``. Cron / non-Slack callers
    leave ``requester_id`` unset and fall back to the admin list so
    background alerts still notify operators.
    """
    if admin_only:
        return _send_admin_dm_notification(severity, summary, detail, extra_blocks)

    emoji = {
        "critical": ":red_circle:",
        "watch": ":large_yellow_circle:",
        "info": ":large_blue_circle:",
    }.get(severity, ":white_circle:")
    label = severity.upper()

    # Precedence: requester_id (thread-scoped, knows who asked) >
    # SLACK_NOTIFY_USER_IDS (env-var admin list, cron fallback). Only
    # apply the requester_id override when we're actually posting into a
    # thread — out-of-band notifications (no reply_to) belong to the
    # admin list.
    if requester_id and reply_to:
        mentions = f"<@{requester_id}>"
    else:
        mentions = " ".join(f"<@{uid}>" for uid in SLACK_NOTIFY_USER_IDS if uid)

    # Redact absolute container paths before they reach the channel (#293, #318).
    summary = _md_to_slack(redact_paths(summary))
    detail = _md_to_slack(redact_paths(detail)) if detail else ""

    text_content = f"{emoji} *{label}* — {summary}"
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text_content}},
    ]
    if detail:
        sections = re.split(r"\n\n+", detail)
        current_block = ""
        for section in sections:
            if len(current_block) + len(section) + 2 > 2900:
                if current_block.strip():
                    blocks.append(
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": current_block.strip()},
                        },
                    )
                current_block = section
            else:
                current_block += "\n\n" + section if current_block else section
        if current_block.strip():
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": current_block.strip()},
                },
            )

    # Pass-through Block Kit blocks (currently just the native `table` block
    # from response_renderer). Validated upstream by Pydantic; not modified
    # here. Slack rejects >1 table per message — the renderer enforces.
    if extra_blocks:
        blocks.extend(extra_blocks)

    if mentions and severity in ("critical", "watch"):
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": mentions}]},
        )

    if len(blocks) > 49:
        blocks = blocks[:49]

    kwargs = {
        "channel": channel or SLACK_CHANNEL_ID,
        "blocks": blocks,
        "text": f"{emoji} {label}: {summary}",
    }
    if reply_to:
        kwargs["thread_ts"] = reply_to

    result = app.client.chat_postMessage(**kwargs)
    return result["ts"]


# Lifecycle reaction helpers — never raise. Slack idempotently no-ops
# ``already_reacted`` and ``no_reaction``, so add/remove pairs that arrive
# in the wrong order (e.g. retry after a missed remove) settle correctly
# on the next transition.

# Lifecycle reaction names. Kept as module-level constants so callers
# stay consistent across slack_bot.py / main.py / session_runner.py.
REACTION_RECEIVED = "eye"  # 👁 — bot acknowledged receipt
REACTION_WORKING = "alarm_clock"  # ⏰ — investigation worker running
REACTION_DONE = "white_check_mark"  # ✅ — post_report dispatched
REACTION_FAILED = "x"  # ❌ — catastrophic failure


def add_reaction(channel: str, ts: str, emoji: str) -> bool:
    """Add an emoji reaction to a message. Never raises.

    Returns True on success or already_reacted (the bot's reaction is
    already there). Returns False on any other failure (missing scope,
    invalid channel/ts, network blip). Failures log at WARNING — they
    are status-indicator hygiene, never a reason to drop user work.
    """
    if not (channel and ts and emoji):
        return False
    try:
        app.client.reactions_add(channel=channel, timestamp=ts, name=emoji)
        return True
    except Exception as e:
        # ``already_reacted`` is the common no-op case — the bot already
        # put this emoji on this message. Not worth logging at INFO every
        # session.
        msg = str(e)
        if "already_reacted" in msg:
            return True
        log.warning(
            "reactions.add failed (channel=%s ts=%s emoji=%s): %s",
            channel,
            ts,
            emoji,
            msg,
        )
        return False


def remove_reaction(channel: str, ts: str, emoji: str) -> bool:
    """Remove the bot's emoji reaction from a message. Never raises.

    Returns True on success or ``no_reaction`` (nothing to remove).
    Returns False on any other failure. Same hygiene rule as
    ``add_reaction``: never block the user workflow on a remove
    failure.
    """
    if not (channel and ts and emoji):
        return False
    try:
        app.client.reactions_remove(channel=channel, timestamp=ts, name=emoji)
        return True
    except Exception as e:
        msg = str(e)
        if "no_reaction" in msg:
            return True
        log.warning(
            "reactions.remove failed (channel=%s ts=%s emoji=%s): %s",
            channel,
            ts,
            emoji,
            msg,
        )
        return False


def transition_reaction(
    channel: str, ts: str, *, remove: Optional[str], add: Optional[str]
) -> None:
    """Atomic-ish lifecycle transition: remove one emoji, add the next.

    Order matters: remove first, then add. If remove fails the add still
    runs, so the user sees forward progress (e.g. ⏰ + ✅ briefly) rather
    than a stuck ⏰. Both calls swallow exceptions internally.
    """
    if remove:
        remove_reaction(channel, ts, remove)
    if add:
        add_reaction(channel, ts, add)


def post_chart_file(
    title: str,
    chart_bytes: bytes,
    reply_to: Optional[str] = None,
    channel: Optional[str] = None,
):
    """Upload a chart PNG directly to Slack. No URL expiry."""
    safe_title = re.sub(r"[^\w\s\-]", "", title)[:80].strip().replace(" ", "_")
    result = app.client.files_upload_v2(
        channel=channel or SLACK_CHANNEL_ID,
        thread_ts=reply_to,
        content=chart_bytes,
        filename=f"{safe_title}.png",
        title=title[:150],
        initial_comment=f":bar_chart: {title}",
    )
    return result.get("file", {}).get("shares", {}).get("ts", "")


def post_file(
    file_path: str,
    title: Optional[str] = None,
    reply_to: Optional[str] = None,
    channel: Optional[str] = None,
    comment: Optional[str] = None,
):
    """Upload any file to Slack."""
    from pathlib import Path

    p = Path(file_path)
    display_title = title or p.stem.replace("_", " ").title()
    result = app.client.files_upload_v2(
        channel=channel or SLACK_CHANNEL_ID,
        thread_ts=reply_to,
        file=file_path,
        filename=p.name,
        title=display_title[:150],
        initial_comment=comment or f"Generated: {p.name}",
    )
    return result


def send_dm(user_id: str, text: str):
    """Send a direct message to a user."""
    resp = app.client.conversations_open(users=[user_id])
    # SlackResponse["channel"] is typed as Any|None; guard before subscripting.
    dm_channel = (resp.get("channel") or {}).get("id")
    if not dm_channel:
        log.warning(
            "send_dm: conversations_open returned no channel id for user_id=%s",
            user_id,
        )
        return
    app.client.chat_postMessage(channel=dm_channel, text=text)


_socket_handler = None


def start_socket_mode():
    global _socket_handler, _bot_user_id
    try:
        auth = app.client.auth_test()
        _bot_user_id = auth["user_id"]
        log.info(f"Bot user ID resolved: {_bot_user_id}")
    except Exception as e:
        log.warning(f"Failed to resolve bot user ID via auth_test: {e}")
    _socket_handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    _socket_handler.start()


def stop_socket_mode():
    """Stop the Socket Mode handler so no new events are received."""
    global _socket_handler
    if _socket_handler:
        _socket_handler.close()
        _socket_handler = None
