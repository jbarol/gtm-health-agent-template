"""Channel description (purpose) push layer (Plan #49).

Classify each Slack channel the bot is in and set the channel's
``purpose`` text to a type-specific, bot-owned blurb explaining how to
use the GTM Health Agent there. The text is fronted by a sentinel
prefix (``[GTM Health Agent] ``) so the bot never overwrites human-set
purpose copy.

Public entry point: ``push_channel_description(channel_id) -> bool``.
The function is intentionally idempotent — running it daily from the
08:00 PT cron is the safety net for any drift caused by Slack admin
edits or template updates.

Decision tree inside ``push_channel_description``:

  1. Read the current purpose via ``conversations.info``.
  2. If the current purpose does NOT start with ``SENTINEL`` → skip
     (human-owned). Returns True (deliberate no-op).
  3. If the current purpose IS sentinel-prefixed and byte-identical
     to the rendered target → skip. Returns True (no drift).
  4. Otherwise call ``conversations.setPurpose`` with the rendered
     text. Returns True/False on Slack API success/failure.

The classifier resolves channels in this order: RFP (hard-coded ID),
portco_analyst (via ``portco_registry.get_portco_by_channel``), or
unknown. Templates live as module-level constants because they are
tightly coupled to slash-command names in ``manifest.yaml`` — config
files would add hot-reload complexity for three short strings.

Failure semantics mirror ``surface_pusher.py``:

  * Never raise. Any exception path returns False and logs
    ``[CHANNEL_DESC_FAILED]``.
  * After 3 failures within a 60-minute rolling window per channel,
    DM admin user IDs once per UTC date (deduped, in-process).
  * The Slack client is fetched lazily (``_slack_client``) so importing
    this module never triggers a Slack auth round-trip.

Kill switch: set ``CHANNEL_DESC_PUSH_ENABLED=false`` on Railway. The
flag is read at call time, so the toggle takes effect on the next call
with no restart.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Deque, Dict, Literal, Tuple

from slack_sdk.errors import SlackApiError

import portco_registry

log = logging.getLogger(__name__)


# Hard-coded RFP channel. There is exactly one today.
# FIXME(plan-49 OQ-4): when a second RFP channel is created, add a
# ``"channel_type": "rfp"`` key to the relevant ``portco_config.json``
# entry, update ``classify_channel`` to check that key before this
# constant, and remove the hard-coded ID.
RFP_CHANNEL_ID = "C0000000001"

# Sentinel prefix. Anything starting with this string is bot-owned and
# may be overwritten by the bot. Anything not starting with this is
# treated as human copy and never touched.
SENTINEL = "[GTM Health Agent] "

# Slack ``purpose`` field hard limit.
PURPOSE_MAX_CHARS = 250

# Retry policy (mirrors surface_pusher).
MAX_RETRIES = 3
DEFAULT_RETRY_AFTER_SECONDS = 1.0

# Watch-notice trigger: 3 failures in 60 min → admin DM (deduped per
# UTC date per channel). Same shape as the surface_pusher cadence.
FAILURE_WINDOW_SECONDS = 60 * 60
FAILURE_THRESHOLD = 3


ChannelType = Literal["rfp", "portco_analyst", "unknown"]


# ─── Templates ──────────────────────────────────────────────────────────
# Plain text with Slack emoji shortcodes. No markdown headers — the
# ``purpose`` field is rendered as plain text in the channel-info pane.
# Each template body is concatenated with SENTINEL by ``render_description``.
# Keep all final strings ≤ PURPOSE_MAX_CHARS (Slack's hard limit).

_TEMPLATE_RFP = (
    "Drop an RFP file (.xlsx, .docx, or .pdf) here — no @mention needed. "
    "The bot returns an ack, summary, and drafted response in-thread. "
    "Questions go to your portco channel."
)

_TEMPLATE_PORTCO_ANALYST = (
    "@mention GTM Health Agent with pipeline, sales, or retention "
    "questions for {portco_name}. Threaded follow-ups reuse the same "
    "session. Prefix \"remember\"/\"always\"/\"never\" to save standing "
    "rules. /bot-verbosity sets depth."
)

_TEMPLATE_UNKNOWN = (
    "I'm here but this channel isn't configured. Ask your operator to "
    "map it in portco_config.json."
)


# ─── Watch-notice dedup state (in-process) ──────────────────────────────
# Module-level dicts gated on UTC date stamp. Mirrors surface_pusher's
# rolling-window approach: simple, accurate for clustered failures, and
# at worst surfaces one duplicate DM across a container restart.
_state_lock = threading.Lock()
_recent_failures: Dict[str, Deque[float]] = {}
_alerted_today: Dict[Tuple[str, str], bool] = {}


# ─── Slack helpers ──────────────────────────────────────────────────────


def _slack_client():
    """Lazy import to avoid a Slack auth round-trip at module load."""
    from slack_bot import app  # noqa: WPS433 — local import is intentional

    return app.client


def _retry_after_seconds(exc: SlackApiError, attempt: int) -> float:
    """Extract ``Retry-After`` from a Slack 429, with jittered fallback."""
    headers = {}
    response = getattr(exc, "response", None)
    if response is not None:
        headers = getattr(response, "headers", None) or {}
        if not headers and isinstance(response, dict):
            headers = response.get("headers", {}) or {}
    raw = headers.get("Retry-After") or headers.get("retry-after")
    jitter = random.uniform(0, 1)
    if raw is None:
        return DEFAULT_RETRY_AFTER_SECONDS * (2**attempt) + jitter
    try:
        return max(float(raw), DEFAULT_RETRY_AFTER_SECONDS) + jitter
    except (TypeError, ValueError):
        return DEFAULT_RETRY_AFTER_SECONDS * (2**attempt) + jitter


def _call_with_retry(callable_fn, *, op: str, channel_id: str):
    """Invoke a Slack API callable, retrying on ``ratelimited`` only.

    Non-``ratelimited`` ``SlackApiError`` codes bubble up; the caller
    logs ``[CHANNEL_DESC_FAILED]`` and returns False.
    """
    attempt = 0
    while True:
        try:
            return callable_fn()
        except SlackApiError as exc:
            err_code = ""
            response = getattr(exc, "response", None)
            if response is not None:
                if hasattr(response, "get"):
                    err_code = response.get("error", "") or ""
                elif isinstance(response, dict):
                    err_code = response.get("error", "") or ""
            if err_code != "ratelimited" or attempt >= MAX_RETRIES:
                raise
            delay = _retry_after_seconds(exc, attempt)
            log.warning(
                "Slack ratelimited on %s for channel=%s; "
                "retrying in %.2fs (attempt %d/%d)",
                op,
                channel_id,
                delay,
                attempt + 1,
                MAX_RETRIES,
            )
            time.sleep(delay)
            attempt += 1


# ─── Public classifier + renderer ───────────────────────────────────────


def classify_channel(channel_id: str) -> ChannelType:
    """Three-way lookup: rfp / portco_analyst / unknown.

    Resolution order:
      1. ``channel_id == RFP_CHANNEL_ID`` → ``"rfp"``.
      2. ``portco_registry.get_portco_by_channel`` returns a non-None
         result → ``"portco_analyst"``. Master channel sits inside the
         portco registry and matches naturally.
      3. Anything else → ``"unknown"``.

    Failures in the registry lookup degrade gracefully to ``"unknown"``
    — never raise.
    """
    if not channel_id:
        return "unknown"
    if channel_id == RFP_CHANNEL_ID:
        return "rfp"
    try:
        portco = portco_registry.get_portco_by_channel(channel_id)
    except Exception:
        log.exception(
            "classify_channel: portco_registry lookup failed for %s",
            channel_id,
        )
        return "unknown"
    if portco:
        return "portco_analyst"
    return "unknown"


def render_description(channel_type: ChannelType, **ctx) -> str:
    """Render the channel-type-specific purpose text with sentinel prefix.

    Substitutes:
      * ``portco_name`` (portco_analyst only) — e.g. ``"Acme"``.

    Length guard: the final string is asserted ≤ ``PURPOSE_MAX_CHARS``.
    If it exceeds the limit (unexpected given template sizes), it is
    silently truncated to 247 chars + ``"..."``. Tests assert the
    template constants stay under the limit so this branch is for
    defense in depth, not regular operation.
    """
    if channel_type == "rfp":
        body = _TEMPLATE_RFP
    elif channel_type == "portco_analyst":
        portco_name = ctx.get("portco_name") or ""
        body = _TEMPLATE_PORTCO_ANALYST.format(portco_name=portco_name)
    else:
        body = _TEMPLATE_UNKNOWN
    full = SENTINEL + body
    if len(full) > PURPOSE_MAX_CHARS:
        log.warning(
            "render_description: %s template exceeds %d chars (%d) — truncating",
            channel_type,
            PURPOSE_MAX_CHARS,
            len(full),
        )
        full = full[: PURPOSE_MAX_CHARS - 3] + "..."
    return full


def get_current_purpose(client, channel_id: str) -> str:
    """Fetch the current channel purpose via ``conversations.info``.

    Returns the raw string. Returns ``""`` on any error (logs but never
    raises). A ``None`` value in ``purpose.value`` (rare, but possible
    for freshly created channels) also surfaces as ``""``.

    On a ``SlackApiError`` (``channel_not_found``, ``not_in_channel``,
    etc.) we log and return ``""``. The outer ``push_channel_description``
    treats an empty current purpose as "first-time write" and will then
    attempt ``conversations.setPurpose``, which will fail with the same
    underlying error and be caught by the outer handler — net behavior
    is still ``False`` + ``[CHANNEL_DESC_FAILED]`` log, but the docstring
    contract holds for any other caller.
    """
    try:
        resp = client.conversations_info(channel=channel_id)
    except Exception:
        log.exception(
            "[CHANNEL_DESC_FAILED] conversations.info raised for channel=%s",
            channel_id,
        )
        return ""
    channel = None
    if isinstance(resp, dict):
        channel = resp.get("channel")
    elif hasattr(resp, "get"):
        channel = resp.get("channel")
    if not isinstance(channel, dict):
        return ""
    purpose = channel.get("purpose")
    if not isinstance(purpose, dict):
        return ""
    value = purpose.get("value")
    return value or ""


# ─── Failure tracking + admin watch notice ──────────────────────────────


def _utc_date_str() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _record_failure_and_maybe_notify(channel_id: str, error: Exception) -> None:
    """Track a failure and emit a deduped admin DM at threshold.

    Always called from the ``except`` block in ``push_channel_description``.
    Must never raise.
    """
    try:
        now = time.time()
        with _state_lock:
            window = _recent_failures.setdefault(channel_id, deque())
            window.append(now)
            cutoff = now - FAILURE_WINDOW_SECONDS
            while window and window[0] < cutoff:
                window.popleft()
            should_notify = len(window) >= FAILURE_THRESHOLD
            day_key = (channel_id, _utc_date_str())
            if should_notify and _alerted_today.get(day_key):
                log.info(
                    "channel_descriptions: %d failures in 60min for channel=%s, "
                    "but admins already notified today — suppressing",
                    len(window),
                    channel_id,
                )
                return
            if not should_notify:
                return
            _alerted_today[day_key] = True
            failure_count = len(window)

        _send_admin_watch_notice(channel_id, failure_count, error)
    except Exception:  # noqa: BLE001 — must never raise
        log.exception(
            "channel_descriptions: failed to evaluate/dispatch admin watch "
            "notice for channel=%s",
            channel_id,
        )


def _send_admin_watch_notice(
    channel_id: str, failure_count: int, error: Exception
) -> None:
    """DM every admin user a one-line description-push failure notice."""
    try:
        from portco_registry import get_admin_user_ids
    except Exception:
        log.exception("channel_descriptions: admin user lookup import failed")
        return
    admins = get_admin_user_ids() or []
    if not admins:
        log.warning(
            "channel_descriptions: no admin_user_ids configured — would have "
            "notified for channel=%s after %d failures",
            channel_id,
            failure_count,
        )
        return

    try:
        from slack_bot import send_dm
    except Exception:
        log.exception("channel_descriptions: send_dm import failed")
        return

    msg = (
        f":large_yellow_circle: *WATCH* — channel description push for "
        f"`{channel_id}` has failed {failure_count} times in the last hour. "
        f"Latest error: `{type(error).__name__}: {error}`. "
        f"The next push will retry. See `[CHANNEL_DESC_FAILED]` logs for detail."
    )
    sent = 0
    for uid in admins:
        if not uid:
            continue
        try:
            send_dm(uid, msg)
            sent += 1
        except Exception:
            log.exception(
                "channel_descriptions: failed to DM admin %s for channel=%s",
                uid,
                channel_id,
            )
    log.warning(
        "channel_descriptions: posted [CHANNEL_DESC_FAILED] watch notice to "
        "%d/%d admins for channel=%s (failure_count=%d)",
        sent,
        len(admins),
        channel_id,
        failure_count,
    )


def _reset_failure_state() -> None:  # pyright: ignore[reportUnusedFunction]
    """Clear in-process failure state. Test-only.

    Pyright doesn't see the cross-module reference from
    ``channel_descriptions_test.py``; the ignore comment suppresses
    the spurious unused-function hint.
    """
    with _state_lock:
        _recent_failures.clear()
        _alerted_today.clear()


# ─── Public entry point ─────────────────────────────────────────────────


def push_channel_description(channel_id: str) -> bool:
    """Set ``purpose`` for ``channel_id`` to the bot-owned template.

    Decision tree:
      1. ``CHANNEL_DESC_PUSH_ENABLED`` false → return True, no-op.
      2. Read current purpose via ``conversations.info``.
      3. If purpose does NOT start with ``SENTINEL`` → return True
         (human-owned; deliberate skip).
      4. If purpose equals the rendered target → return True (no drift).
      5. Call ``conversations.setPurpose``. Return True on success,
         False on failure.

    Failures:
      * Any exception path returns False and logs ``[CHANNEL_DESC_FAILED]``.
      * After 3 failures in 60 min, DMs admins (deduped per UTC date).
    """
    # Kill switch — read at call time so the toggle takes effect without
    # a restart. Import inside the function so test environments can flip
    # the env var between calls and pick up the new value via a fresh
    # ``parse_bool`` evaluation.
    try:
        from config import parse_bool

        if not parse_bool("CHANNEL_DESC_PUSH_ENABLED", default=True):
            log.info(
                "channel_descriptions: skipping push for %s — "
                "CHANNEL_DESC_PUSH_ENABLED=false",
                channel_id,
            )
            return True
    except Exception:
        log.exception(
            "channel_descriptions: kill-switch lookup failed (treating as enabled) "
            "for channel=%s",
            channel_id,
        )

    if not channel_id:
        log.warning("channel_descriptions: empty channel_id, skipping")
        return False

    try:
        client = _slack_client()

        # Build the target text first so we can compare against current.
        channel_type = classify_channel(channel_id)
        portco_name = ""
        if channel_type == "portco_analyst":
            try:
                portco = portco_registry.get_portco_by_channel(channel_id)
                if portco:
                    portco_name = portco.get("name", "") or ""
            except Exception:
                log.exception(
                    "channel_descriptions: portco name lookup failed for %s",
                    channel_id,
                )
        target = render_description(channel_type, portco_name=portco_name)

        current = get_current_purpose(client, channel_id)

        # Sentinel guard: only ever overwrite text we previously wrote.
        if current and not current.startswith(SENTINEL):
            log.info(
                "channel_descriptions: skipping %s — purpose is human-owned",
                channel_id,
            )
            return True

        if current == target:
            log.info(
                "channel_descriptions: no-op for %s (purpose unchanged)",
                channel_id,
            )
            return True

        _call_with_retry(
            lambda: client.conversations_setPurpose(
                channel=channel_id, purpose=target
            ),
            op="conversations.setPurpose",
            channel_id=channel_id,
        )
        log.info(
            "channel_descriptions: set purpose for %s (type=%s, %d chars)",
            channel_id,
            channel_type,
            len(target),
        )
        return True
    except Exception as exc:  # noqa: BLE001 — broad on purpose
        log.error(
            "[CHANNEL_DESC_FAILED] channel=%s error=%s",
            channel_id,
            exc,
        )
        _record_failure_and_maybe_notify(channel_id, exc)
        return False
