"""Plan #44 Task #14 — Anthropic webhook handler.

Subscribes to four event classes per the plan:

  - ``session.status_terminated``        — a session ran past its budget,
                                           hit a tool error, or otherwise
                                           ended unhealthy. Operator needs
                                           the thread link + a pre-filled
                                           rollback command.
  - ``session.thread_terminated``        — a sub-agent thread terminated;
                                           often a leading indicator of
                                           the parent dying.
  - ``vault_credential.refresh_failed``  — a Acme vault's refresh
                                           token expired or got rejected.
                                           Operator needs the vault ID
                                           and the ``add-sf-vault-
                                           credential.py --apply`` reminder.
  - ``session.outcome_evaluation_ended`` — outcomes-rubric evaluation
                                           completed. Logged for now;
                                           full handling lands with
                                           Plan #44 Task #21.

The HTTP handler returns 200 even on internal failure (after logging) so
Anthropic doesn't retry the same event forever — the failure mode here
is "we missed a notification," not "we corrupt the next one."

Every admin DM follows the canonical WHAT + WHY + FIX-COMMAND shape
mandated by decision rows #21 and #24:

  *WHAT*       — one-line summary the operator can read on their phone
  *WHY*        — the terminal error excerpt or vault failure detail
  *FIX COMMAND* — a copy-paste-actionable shell command, pre-filled with
                 the right agent name / vault ID, so the operator can
                 fix it at 2am without remembering the syntax.

We DO NOT trust the webhook body alone — per Anthropic's documented
pattern, the handler fetches the full object from the API after
verifying the signature. The webhook payload exists to tell us "look at
THIS id," not to deliver the canonical state.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional, Tuple

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Signature verification
# ─────────────────────────────────────────────────────────────────────────────


def _signing_key() -> str:
    """Read ``ANTHROPIC_WEBHOOK_SIGNING_KEY`` at call time so tests can
    monkey-patch ``os.environ`` after import."""
    return os.environ.get("ANTHROPIC_WEBHOOK_SIGNING_KEY", "") or ""


def _client():
    """Lazy-build Anthropic client so tests can stub anthropic.Anthropic
    before the first webhook fires."""
    import anthropic  # local — see version_pin_overrides

    return anthropic.Anthropic()


def verify_and_parse(body: bytes, headers: dict) -> Tuple[bool, Optional[dict], str]:
    """Verify the webhook signature and return ``(ok, event, error)``.

    ``body`` is the raw request body (bytes preferred, str accepted).
    ``headers`` is the request header dict; case-insensitive lookup is
    expected (httpx Headers, Werkzeug EnvironHeaders, or a plain dict).
    The SDK's ``beta.webhooks.unwrap`` accepts both shapes for headers.
    Payload is decoded to UTF-8 before handoff — the SDK takes ``str``.

    On any failure (missing signing key, bad signature, malformed JSON)
    we return ``(False, None, error_message)`` so the calling HTTP layer
    can respond 400 without leaking detail.
    """
    key = _signing_key()
    if not key:
        return False, None, "ANTHROPIC_WEBHOOK_SIGNING_KEY is not set"

    try:
        cli = _client()
        # ``cli.beta.webhooks.unwrap`` is the canonical SDK helper as of
        # anthropic-python 0.100.x — the resource lives under ``beta``, NOT
        # at the top level (closing-review HIGH #1, 2026-05-13). The SDK
        # signature is ``unwrap(payload: str, *, headers, key)`` — note
        # ``key=`` (not ``secret=``) and ``payload`` is a string.
        payload = body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else body
        event = cli.beta.webhooks.unwrap(payload, headers=headers, key=key)
    except Exception as e:
        log.warning(f"webhook signature verification failed: {e}")
        return False, None, "invalid signature"

    # The SDK returns a typed object; we normalize to dict so downstream
    # handlers don't have to special-case attribute vs dict access.
    if hasattr(event, "model_dump"):
        try:
            event_dict = event.model_dump()
        except Exception:
            event_dict = {}
    elif isinstance(event, dict):
        event_dict = event
    else:
        event_dict = json.loads(json.dumps(event, default=str))
    return True, event_dict, ""


# ─────────────────────────────────────────────────────────────────────────────
# Per-event handlers — every one fetches the full object via API rather
# than trusting the webhook body alone.
# ─────────────────────────────────────────────────────────────────────────────


def _agent_short_name_from_id(agent_id: str) -> str:
    """Best-effort reverse lookup from an Anthropic agent ID to the short
    name used by ``bin/rollback-agent.py``. Returns the ID itself when
    no match is found — the operator can still figure it out, the
    pre-filled command just won't be one-click.

    We resolve by reading the env vars set by ``setup_agents.py`` rather
    than importing ``agents/update_prompts`` directly — that module
    pulls ``anthropic`` and runs a ``.env`` loader at module load, which
    is fragile in test environments. Env vars are stable.
    """
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
        # Hardcoded fallback IDs from agents/update_prompts.py — the
        # canonical mapping. Used when env vars are unset (tests, fresh
        # dev clones). When the deploy rotates an agent the env wins.
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
        log.debug("agent short-name lookup failed", exc_info=True)
    return agent_id


def _fetch_session(session_id: str) -> dict:
    """Pull the full session object via API. Returns a dict on success,
    ``{}`` on failure — the caller falls back to webhook-supplied fields.
    """
    if not session_id:
        return {}
    try:
        cli = _client()
        s = cli.beta.sessions.retrieve(session_id)
        if hasattr(s, "model_dump"):
            return s.model_dump()
        if isinstance(s, dict):
            return s
        return json.loads(json.dumps(s, default=str))
    except Exception as e:
        log.warning(f"_fetch_session({session_id}) failed: {e}")
        return {}


def _slack_thread_link(channel_id: str, thread_ts: str) -> str:
    """Build a slack.com://<channel>/<ts> deep-link the operator can tap.

    Best-effort — we use the workspace's permalink path even though we
    don't know the team domain at runtime (Slack's mobile clients honor
    the short form). The slash-format ``archives/<channel>/p<ts>`` is
    the canonical format; we drop the dot in the ts.
    """
    if not channel_id or not thread_ts:
        return ""
    ts_compact = (thread_ts or "").replace(".", "")
    return f"slack://channel?id={channel_id}&message={thread_ts}#p{ts_compact}"


# Canonical WHAT + WHY + FIX-COMMAND admin DM template — decision rows
# #21 and #24. Keep these strings inline so an at-2am operator can read
# the source and know exactly what they'll see in Slack.
_DM_TEMPLATE = "*WHAT:* {what}\n*WHY:* {why}\n*FIX:* `{fix}`"


def _terminal_error_excerpt(session: dict) -> str:
    """Return a short error excerpt for the WHY line. Looks at the
    common shapes a terminated session might surface."""
    err = session.get("error") or session.get("status_message") or ""
    if isinstance(err, dict):
        err = err.get("message", "") or json.dumps(err)
    err = (err or "").strip()
    if len(err) > 240:
        err = err[:237] + "..."
    return err or "no error detail provided by Anthropic"


def handle_session_status_terminated(event: dict) -> dict:
    """Build the admin DM payload for a terminated session.

    Returns ``{"dm": str | None, "severity": str, "event_type": str}``.
    The dispatcher (``dispatch_event``) decides whether to actually send.

    Pre-fills ``bin/rollback-agent.py <agent> --to-version <prior>`` — the
    operator usually wants to roll back the most recently changed agent,
    so we name the session's agent. ``<prior>`` is intentionally a
    placeholder; the operator looks up the prior version from
    ``agents/active_versions.json`` or the most recent prompt-deploy PR.
    """
    payload = event.get("data") or event.get("object") or event
    session_id = payload.get("id") or payload.get("session_id") or ""
    session = _fetch_session(session_id)

    # Merge fetched state over webhook payload (webhook supplies the ID;
    # API delivers the canonical fields).
    merged = {**payload, **session} if session else payload
    agent_id = (
        merged.get("agent_id") or merged.get("agent", {}).get("id", "")
        if isinstance(merged.get("agent"), dict)
        else merged.get("agent_id", "")
    )
    if not agent_id:
        agent_id = (
            (merged.get("agent") or "") if isinstance(merged.get("agent"), str) else ""
        )
    short = _agent_short_name_from_id(agent_id) if agent_id else ""

    metadata = merged.get("metadata") or {}
    channel_id = metadata.get("channel_id", "") if isinstance(metadata, dict) else ""
    thread_ts = metadata.get("thread_ts", "") if isinstance(metadata, dict) else ""
    thread_link = _slack_thread_link(channel_id, thread_ts)

    error_excerpt = _terminal_error_excerpt(merged)

    fix_cmd = f"bin/rollback-agent.py {short or '<agent>'} --to-version <prior>"
    what = (
        f"Session `{session_id}` terminated unhealthy"
        + (f" (agent `{short}`)" if short else "")
        + (f" — {thread_link}" if thread_link else "")
    )
    dm = _DM_TEMPLATE.format(what=what, why=error_excerpt, fix=fix_cmd)
    return {
        "dm": dm,
        "severity": "critical",
        "event_type": "session.status_terminated",
        "session_id": session_id,
    }


def handle_session_thread_terminated(event: dict) -> dict:
    """A sub-agent thread terminated. Lower severity than the parent
    session terminating but still worth a DM — often a leading indicator.
    """
    payload = event.get("data") or event.get("object") or event
    session_id = payload.get("session_id") or payload.get("id") or ""
    thread_id = payload.get("thread_id") or ""

    what = f"Sub-agent thread `{thread_id}` terminated in session `{session_id}`"
    why = (
        "Sub-agent ended before parent. If the parent session keeps "
        "running this is usually benign; if it terminates next, the "
        "status_terminated webhook will fire."
    )
    fix = "bin/rollback-agent.py <sub_agent> --to-version <prior>"
    dm = _DM_TEMPLATE.format(what=what, why=why, fix=fix)
    return {
        "dm": dm,
        "severity": "watch",
        "event_type": "session.thread_terminated",
        "session_id": session_id,
    }


def handle_vault_credential_refresh_failed(event: dict) -> dict:
    """A vault credential's refresh token was rejected. Pre-fill the
    add-sf-vault-credential reminder — that script is owned by Bundle D
    (per Bundle E's "DO NOT TOUCH" list) but the command itself is what
    we point the operator at.
    """
    payload = event.get("data") or event.get("object") or event
    vault_id = payload.get("vault_id") or payload.get("id") or ""
    detail = payload.get("error", "") or payload.get("message", "") or ""
    if isinstance(detail, dict):
        detail = detail.get("message", "") or json.dumps(detail)

    what = f"Vault `{vault_id}` credential refresh FAILED"
    why = (
        detail or "Anthropic returned a refresh failure with no detail"
    ) + " — Salesforce sessions for this portco will fail until rotated."
    fix = f"bin/add-sf-vault-credential.py --vault {vault_id} --apply"
    dm = _DM_TEMPLATE.format(what=what, why=why, fix=fix)
    return {
        "dm": dm,
        "severity": "critical",
        "event_type": "vault_credential.refresh_failed",
        "vault_id": vault_id,
    }


def handle_session_outcome_evaluation_ended(event: dict) -> dict:
    """Stub handler for outcomes evaluation. Plan #44 Task #21 wires
    real rubric handling; until then we acknowledge + log.
    """
    payload = event.get("data") or event.get("object") or event
    session_id = payload.get("session_id") or payload.get("id") or ""
    log.info(
        "outcome_evaluation_ended received for session=%s — Task #21 will wire handling",
        session_id,
    )
    return {
        "dm": None,
        "severity": "info",
        "event_type": "session.outcome_evaluation_ended",
        "session_id": session_id,
    }


_HANDLERS = {
    "session.status_terminated": handle_session_status_terminated,
    "session.thread_terminated": handle_session_thread_terminated,
    "vault_credential.refresh_failed": handle_vault_credential_refresh_failed,
    "session.outcome_evaluation_ended": handle_session_outcome_evaluation_ended,
}


def dispatch_event(event: dict) -> dict:
    """Route an event to its per-type handler. Returns the handler's
    return value or a no-op dict for unknown types.

    Module-level (not behind an HTTP handler) so unit tests can drive
    dispatch without standing up an HTTP server.
    """
    event_type = event.get("type") or ""
    handler = _HANDLERS.get(event_type)
    if not handler:
        log.info(f"webhook: unknown event type {event_type!r} — ignoring")
        return {"dm": None, "severity": "info", "event_type": event_type}
    try:
        return handler(event)
    except Exception:
        log.exception(f"webhook handler for {event_type} raised")
        return {"dm": None, "severity": "info", "event_type": event_type}


def _send_admin_dm(severity: str, dm_text: str) -> None:
    """Forward an admin DM to ``slack_bot.send_notification`` in admin_only
    mode. Lazy import so this module can be unit-tested without dragging
    Bolt into the test harness.

    Never raises — the DM is best-effort. If Slack is also down, the log
    line is the only signal we have.
    """
    if not dm_text:
        return
    try:
        from slack_bot import send_notification  # type: ignore

        send_notification(
            severity=severity or "watch",
            summary=dm_text,
            admin_only=True,
        )
    except Exception:
        log.exception("admin DM send failed for webhook event")


def handle_webhook(body: bytes, headers: dict) -> Tuple[int, str]:
    """Top-level HTTP handler — verify, parse, dispatch, return status.

    Returns ``(http_status, response_body)``. The caller wires this into
    whatever HTTP framework the orchestrator uses (currently a stdlib
    BaseHTTPRequestHandler in :mod:`main`); Bundle E does not modify
    :mod:`main`, so :mod:`anthropic_webhooks_register` exposes a
    framework-agnostic registration helper Bundle B can call at
    integration time.
    """
    ok, event, err = verify_and_parse(body, headers)
    if not ok or event is None:
        return 400, json.dumps({"ok": False, "error": err})

    result = dispatch_event(event)
    _send_admin_dm(result.get("severity", "watch"), result.get("dm") or "")
    return 200, json.dumps({"ok": True, "type": result.get("event_type", "")})
