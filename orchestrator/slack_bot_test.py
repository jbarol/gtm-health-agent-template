"""Tests for slack_bot._md_to_slack — the Markdown → Slack mrkdwn converter.

These cover the cases the Managed Agents docs-diff DM (self_improve._notify_user)
relies on. Slack uses mrkdwn, not Markdown, so common GitHub/CommonMark
constructs need translation before the message is posted.

Run:
    cd orchestrator && python3 -m pytest slack_bot_test.py -q
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

# Required env vars for config.py to import without raising. setdefault means
# a real .env wins.
for _key, _value in {
    "ANTHROPIC_API_KEY": "sk-ant-test",
    "SLACK_BOT_TOKEN": "xoxb-test",
    "SLACK_APP_TOKEN": "xapp-test",
    "SLACK_CHANNEL_ID": "C0TEST",
    "ENVIRONMENT_ID": "env_test",
    "DREAM_AGENT_ID": "agent_test_dream",
    "COORDINATOR_ID": "agent_test_coord",
    "QUICK_AGENT_ID": "agent_test_quick",
    "METHODOLOGY_STORE_ID": "memstore_test_m",
    "HEALTH_STORE_ID": "memstore_test_h",
}.items():
    os.environ.setdefault(_key, _value)


# ── Bullet conversion ──────────────────────────────────────────────────────


def test_dash_bullets_become_unicode_bullets():
    """``- foo`` is the most common Managed-Agents-DM bullet style and is the
    primary thing the rename PR fixes. Slack renders the literal ``-`` as a
    dash, not a bullet."""
    from slack_bot import _md_to_slack

    out = _md_to_slack("- alpha\n- beta\n- gamma")
    assert out == "• alpha\n• beta\n• gamma"


def test_asterisk_bullets_become_unicode_bullets():
    """CommonMark allows ``*`` as a bullet marker too. The space gate keeps it
    from mangling ``**bold**`` runs."""
    from slack_bot import _md_to_slack

    out = _md_to_slack("* first\n* second")
    assert out == "• first\n• second"


def test_bullet_indentation_preserved():
    """Nested lists keep their indent so visual hierarchy survives."""
    from slack_bot import _md_to_slack

    src = "- top\n  - nested\n    - deeply nested"
    out = _md_to_slack(src)
    assert "• top" in out
    assert "  • nested" in out
    assert "    • deeply nested" in out


def test_inline_dash_not_converted():
    """A dash inside a sentence must not turn into a bullet — only the leading
    list marker should match."""
    from slack_bot import _md_to_slack

    out = _md_to_slack("This is a sentence - with a dash.")
    assert "• " not in out
    assert "-" in out


def test_bold_with_asterisk_not_mangled():
    """``**bold**`` must collapse to ``*bold*`` cleanly without the bullet
    rule eating the leading asterisks."""
    from slack_bot import _md_to_slack

    out = _md_to_slack("**important**: read this")
    assert "*important*" in out
    assert "•" not in out


def test_slack_bold_passes_through():
    """Existing ``*bold*`` markers must not be touched by the bullet rule."""
    from slack_bot import _md_to_slack

    out = _md_to_slack("*already bold*")
    assert out == "*already bold*"


# ── Header conversion ──────────────────────────────────────────────────────


def test_h1_becomes_bold():
    """``# Heading`` → bold (Slack doesn't render literal ``#``)."""
    from slack_bot import _md_to_slack

    out = _md_to_slack("# Hello")
    assert "*Hello*" in out
    assert "#" not in out


def test_h2_becomes_bold():
    from slack_bot import _md_to_slack

    out = _md_to_slack("## Sub")
    assert "*Sub*" in out
    assert "##" not in out


def test_h3_becomes_bold():
    from slack_bot import _md_to_slack

    out = _md_to_slack("### Sub-sub")
    assert "*Sub-sub*" in out
    assert "###" not in out


# ── Bold and code spans ────────────────────────────────────────────────────


def test_double_asterisk_bold_normalized():
    """``**x**`` → ``*x*`` (Slack mrkdwn bold)."""
    from slack_bot import _md_to_slack

    out = _md_to_slack("**bold**")
    assert out == "*bold*"


def test_backtick_code_preserved():
    """``code`` stays as-is — Slack renders backtick spans natively."""
    from slack_bot import _md_to_slack

    out = _md_to_slack("Use `soqlQuery` to query SF.")
    assert "`soqlQuery`" in out


def test_numbered_list_preserved():
    """``1. foo`` numbered lists render natively in Slack — must NOT be
    converted to bullets."""
    from slack_bot import _md_to_slack

    out = _md_to_slack("1. first\n2. second\n3. third")
    assert "1. first" in out
    assert "2. second" in out
    assert "3. third" in out
    assert "•" not in out


# ── Realistic Managed Agents docs-diff payload ─────────────────────────────


def test_managed_agents_docs_diff_payload_renders_cleanly():
    """End-to-end smoke: a representative LLM-generated docs-diff summary
    should have every Markdown artifact resolved to Slack-native mrkdwn after
    one round-trip through ``_md_to_slack``."""
    from slack_bot import _md_to_slack

    src = (
        "## What's new\n"
        "- Native structured outputs landed\n"
        "- New `response_format` field on agent definitions\n"
        "- **Breaking**: old `output_schema` is deprecated\n\n"
        "## Impact on us\n"
        "1. Coordinator agent\n"
        "2. Quick Answer agent\n"
    )
    out = _md_to_slack(src)

    # Headers gone, bold versions present
    assert "## " not in out
    assert "*What's new*" in out
    assert "*Impact on us*" in out

    # Bullets converted
    assert "• Native structured outputs landed" in out
    assert "• New `response_format` field on agent definitions" in out

    # ** → *
    assert "**Breaking**" not in out
    assert "*Breaking*" in out

    # Code span preserved
    assert "`response_format`" in out
    assert "`output_schema`" in out

    # Numbered list preserved verbatim
    assert "1. Coordinator agent" in out
    assert "2. Quick Answer agent" in out


# ── Plan #33 F9 — surface triggers ────────────────────────────────────────


def _make_history_with_bot_message(bot_user_id="UBOTTEST"):
    """Return a fake ``conversations_history`` result with one bot message."""
    return {
        "messages": [
            {
                "ts": "1700000000.000100",
                "user": bot_user_id,
                "bot_id": "B0BOT",
                "text": "Finding: pipeline coverage at 1.8x",
                "thread_ts": "1700000000.000100",
            }
        ]
    }


def test_reaction_added_fires_surface_push_after_feedback_recorded(monkeypatch):
    """Plan #33 F9 — after the feedback row is captured we kick off a
    push_to_canvas on a daemon thread.

    The push call is *not* awaited (it's daemon-threaded), so the test
    verifies ``threading.Thread`` was started with the right target +
    portco_key argument rather than asserting on ``push_to_canvas``
    return value.
    """
    import importlib

    import slack_bot

    importlib.reload(slack_bot)
    slack_bot._bot_user_id = "UBOTTEST"

    # Stub the Slack client so the history lookup succeeds.
    fake_client = MagicMock()
    fake_client.conversations_history.return_value = _make_history_with_bot_message(
        "UBOTTEST"
    )

    # Stub feedback_capture + portco_registry so the handler reaches the push.
    import sys

    fake_feedback = MagicMock()
    fake_registry = MagicMock()
    fake_registry.get_portco_by_channel.return_value = {"key": "acme"}
    monkeypatch.setitem(sys.modules, "feedback_capture", fake_feedback)
    monkeypatch.setitem(sys.modules, "portco_registry", fake_registry)

    # Stub surface_pusher (lazy-imported inside the handler). We don't want
    # the test to depend on F6 being on main yet.
    fake_pusher = MagicMock()
    monkeypatch.setitem(sys.modules, "surface_pusher", fake_pusher)

    # Replace threading.Thread inside slack_bot so we can assert the daemon
    # thread was constructed with the right target + args, without actually
    # launching a thread (which would race with the test exit).
    fake_thread_cls = MagicMock()
    fake_thread = MagicMock()
    fake_thread_cls.return_value = fake_thread
    monkeypatch.setattr("threading.Thread", fake_thread_cls)

    event = {
        "type": "reaction_added",
        "user": "UHUMAN",
        "reaction": "+1",
        "item": {
            "type": "message",
            "channel": "C0CHAN",
            "ts": "1700000000.000100",
        },
    }
    slack_bot.handle_reaction_added(event, client=fake_client)

    # 1. feedback row must be written first.
    assert fake_feedback.record_feedback.called, (
        "expected feedback_capture.record_feedback to be called for a +1 on a bot message"
    )
    rf_kwargs = fake_feedback.record_feedback.call_args.kwargs
    assert rf_kwargs["portco_key"] == "acme"
    assert rf_kwargs["signal"] == "positive"
    assert rf_kwargs["source"] == "emoji"

    # 2. Then a daemon Thread is started with push_to_canvas as the target.
    assert fake_thread_cls.called, "expected threading.Thread to be invoked"
    thread_kwargs = fake_thread_cls.call_args.kwargs
    assert thread_kwargs["target"] is fake_pusher.push_to_canvas
    assert thread_kwargs["args"] == ("acme",)
    assert thread_kwargs["daemon"] is True
    fake_thread.start.assert_called_once()


def test_reaction_added_skips_surface_push_when_portco_unknown(monkeypatch):
    """If we can't resolve a portco (non-portco channel), don't fire the push.

    The feedback row is still written — the channel_id is enough attribution
    for the fallback path — but firing push_to_canvas with portco_key=""
    would be ambiguous, so we skip it.
    """
    import importlib

    import slack_bot

    importlib.reload(slack_bot)
    slack_bot._bot_user_id = "UBOTTEST"

    fake_client = MagicMock()
    fake_client.conversations_history.return_value = _make_history_with_bot_message(
        "UBOTTEST"
    )

    import sys

    fake_feedback = MagicMock()
    fake_registry = MagicMock()
    fake_registry.get_portco_by_channel.return_value = None  # no portco
    monkeypatch.setitem(sys.modules, "feedback_capture", fake_feedback)
    monkeypatch.setitem(sys.modules, "portco_registry", fake_registry)

    fake_pusher = MagicMock()
    monkeypatch.setitem(sys.modules, "surface_pusher", fake_pusher)

    fake_thread_cls = MagicMock()
    monkeypatch.setattr("threading.Thread", fake_thread_cls)

    event = {
        "type": "reaction_added",
        "user": "UHUMAN",
        "reaction": "thumbsup",
        "item": {
            "type": "message",
            "channel": "C0CHAN",
            "ts": "1700000000.000100",
        },
    }
    slack_bot.handle_reaction_added(event, client=fake_client)

    assert fake_feedback.record_feedback.called
    assert not fake_thread_cls.called, (
        "expected no surface push when portco_key is empty"
    )


def test_refresh_surface_rejects_non_admin_user(monkeypatch):
    """A user not in SLACK_ADMIN_USER_IDS gets an ephemeral denial."""
    import slack_bot

    monkeypatch.setenv("SLACK_ADMIN_USER_IDS", "UADMIN1,UADMIN2")

    ack = MagicMock()
    respond = MagicMock()
    command = {
        "user_id": "UNOTANADMIN",
        "channel_id": "C0CHAN",
        "text": "",
    }

    import sys

    fake_pusher = MagicMock()
    monkeypatch.setitem(sys.modules, "surface_pusher", fake_pusher)

    slack_bot.on_refresh_surface_command(ack, command, respond)

    ack.assert_called_once()
    assert not fake_pusher.push_to_canvas.called, (
        "expected push_to_canvas NOT to fire for a non-admin caller"
    )
    respond.assert_called_once()
    respond_kwargs = respond.call_args.kwargs
    assert respond_kwargs.get("response_type") == "ephemeral"
    assert "admin-only" in respond_kwargs.get("text", "").lower()


def test_refresh_surface_rejects_when_admin_list_empty(monkeypatch):
    """Empty / unset SLACK_ADMIN_USER_IDS rejects everyone (safe default)."""
    import slack_bot

    monkeypatch.delenv("SLACK_ADMIN_USER_IDS", raising=False)

    ack = MagicMock()
    respond = MagicMock()
    command = {
        "user_id": "UANYONE",
        "channel_id": "C0CHAN",
        "text": "",
    }

    import sys

    fake_pusher = MagicMock()
    monkeypatch.setitem(sys.modules, "surface_pusher", fake_pusher)

    slack_bot.on_refresh_surface_command(ack, command, respond)
    assert not fake_pusher.push_to_canvas.called
    respond.assert_called_once()
    assert respond.call_args.kwargs.get("response_type") == "ephemeral"


def test_refresh_surface_admin_calls_push_to_canvas(monkeypatch):
    """Admin user → synchronous push_to_canvas + success message."""
    import slack_bot

    monkeypatch.setenv("SLACK_ADMIN_USER_IDS", "UADMIN1,UADMIN2")

    ack = MagicMock()
    respond = MagicMock()
    command = {
        "user_id": "UADMIN1",
        "channel_id": "C0CHAN",
        "text": "",
    }

    import sys

    fake_pusher = MagicMock()
    fake_pusher.push_to_canvas = MagicMock()
    monkeypatch.setitem(sys.modules, "surface_pusher", fake_pusher)

    fake_desc = MagicMock()
    monkeypatch.setitem(sys.modules, "channel_descriptions", fake_desc)

    fake_registry = MagicMock()
    fake_registry.get_portco_by_channel.return_value = {"key": "acme"}
    monkeypatch.setitem(sys.modules, "portco_registry", fake_registry)

    slack_bot.on_refresh_surface_command(ack, command, respond)

    ack.assert_called_once()
    fake_pusher.push_to_canvas.assert_called_once_with("acme")
    # Plan #49 — the description push fires alongside the Canvas push.
    fake_desc.push_channel_description.assert_called_once_with("C0CHAN")
    respond.assert_called_once()
    rk = respond.call_args.kwargs
    assert rk.get("response_type") == "ephemeral"
    assert "acme" in rk.get("text", "")
    assert "refreshed" in rk.get("text", "").lower()


def test_refresh_surface_admin_with_explicit_portco_arg(monkeypatch):
    """``/refresh-surface <portco>`` overrides the channel-derived portco."""
    import slack_bot

    monkeypatch.setenv("SLACK_ADMIN_USER_IDS", "UADMIN1")

    ack = MagicMock()
    respond = MagicMock()
    command = {
        "user_id": "UADMIN1",
        "channel_id": "C0OTHER",
        "text": "acme",
    }

    import sys

    fake_pusher = MagicMock()
    fake_pusher.push_to_canvas = MagicMock()
    monkeypatch.setitem(sys.modules, "surface_pusher", fake_pusher)

    fake_desc = MagicMock()
    monkeypatch.setitem(sys.modules, "channel_descriptions", fake_desc)

    fake_registry = MagicMock()
    monkeypatch.setitem(sys.modules, "portco_registry", fake_registry)

    slack_bot.on_refresh_surface_command(ack, command, respond)

    # Channel-derived lookup is bypassed when text is non-empty.
    fake_registry.get_portco_by_channel.assert_not_called()
    fake_pusher.push_to_canvas.assert_called_once_with("acme")
    # Plan #49 — the description push fires for the invoking channel.
    fake_desc.push_channel_description.assert_called_once_with("C0OTHER")


def test_refresh_surface_admin_push_failure_reports_cleanly(monkeypatch):
    """When push_to_canvas raises, the user gets a clean ephemeral failure
    message — never a Bolt traceback."""
    import slack_bot

    monkeypatch.setenv("SLACK_ADMIN_USER_IDS", "UADMIN1")

    ack = MagicMock()
    respond = MagicMock()
    command = {
        "user_id": "UADMIN1",
        "channel_id": "C0CHAN",
        "text": "acme",
    }

    import sys

    fake_pusher = MagicMock()
    fake_pusher.push_to_canvas.side_effect = RuntimeError("canvas API 500")
    monkeypatch.setitem(sys.modules, "surface_pusher", fake_pusher)

    # The handler MUST NOT raise.
    slack_bot.on_refresh_surface_command(ack, command, respond)

    fake_pusher.push_to_canvas.assert_called_once_with("acme")
    respond.assert_called_once()
    rk = respond.call_args.kwargs
    assert rk.get("response_type") == "ephemeral"
    assert "failed" in rk.get("text", "").lower()


def test_refresh_surface_admin_channel_desc_failure_swallowed(monkeypatch):
    """Plan #49 — when push_channel_description raises after a successful
    Canvas push, the user still sees the Canvas success response. The
    description failure is logged but never bubbles up to override the
    Canvas success.
    """
    import slack_bot

    monkeypatch.setenv("SLACK_ADMIN_USER_IDS", "UADMIN1")

    ack = MagicMock()
    respond = MagicMock()
    command = {
        "user_id": "UADMIN1",
        "channel_id": "C0CHAN",
        "text": "acme",
    }

    import sys

    fake_pusher = MagicMock()
    fake_pusher.push_to_canvas = MagicMock()
    monkeypatch.setitem(sys.modules, "surface_pusher", fake_pusher)

    fake_desc = MagicMock()
    fake_desc.push_channel_description.side_effect = RuntimeError("setPurpose API 500")
    monkeypatch.setitem(sys.modules, "channel_descriptions", fake_desc)

    slack_bot.on_refresh_surface_command(ack, command, respond)

    fake_pusher.push_to_canvas.assert_called_once_with("acme")
    fake_desc.push_channel_description.assert_called_once_with("C0CHAN")
    # User still sees the Canvas success message — description failure
    # is swallowed.
    respond.assert_called_once()
    rk = respond.call_args.kwargs
    assert "refreshed" in rk.get("text", "").lower()
    assert "failed" not in rk.get("text", "").lower()


def test_refresh_surface_admin_no_portco_resolution(monkeypatch):
    """No portco arg, no channel match → ephemeral warning, no push."""
    import slack_bot

    monkeypatch.setenv("SLACK_ADMIN_USER_IDS", "UADMIN1")

    ack = MagicMock()
    respond = MagicMock()
    command = {
        "user_id": "UADMIN1",
        "channel_id": "C0NOTAPORTCO",
        "text": "",
    }

    import sys

    fake_pusher = MagicMock()
    monkeypatch.setitem(sys.modules, "surface_pusher", fake_pusher)

    fake_registry = MagicMock()
    fake_registry.get_portco_by_channel.return_value = None
    monkeypatch.setitem(sys.modules, "portco_registry", fake_registry)

    slack_bot.on_refresh_surface_command(ack, command, respond)

    assert not fake_pusher.push_to_canvas.called
    respond.assert_called_once()
    rk = respond.call_args.kwargs
    assert rk.get("response_type") == "ephemeral"
    assert "couldn't resolve" in rk.get("text", "").lower()


# ---------------------------------------------------------------------------
# Plain-English polish integration — see prose_polish.py.
# These tests guarantee that _md_to_slack runs the polish pass before any
# structural conversion, so the bad-forecast-report regression cannot
# reach Slack even if the agent prompts drift back to jargon.
# ---------------------------------------------------------------------------


def test_md_to_slack_runs_prose_polish_first():
    """Markdown analyst prose with acronyms + stats is plain-English'd."""
    from slack_bot import _md_to_slack

    src = (
        "NB pipeline trending β = -$71K/qtr (p=0.001, R²=0.62). "
        "Hybrid MC forecast widening. Full report: "
        "/mnt/session/outputs/forecast_report.md (533 lines)."
    )
    out = _md_to_slack(src)

    # Acronyms glossed at first use.
    assert "new business (NB)" in out
    # Statistics rewritten to plain English.
    assert "p=" not in out
    assert "R²" not in out
    assert "β =" not in out
    assert "trending down by ~$71K per quarter" in out
    assert "of the variation explained" in out
    # Internal path stripped.
    assert "/mnt/session/outputs" not in out


def test_md_to_slack_preserves_dollar_amounts():
    """Polish step must never drop a number the analyst put in."""
    from slack_bot import _md_to_slack

    out = _md_to_slack(
        "Q2 NB Open ARR: $5,762K (vs $5,761K 48h prior). MC forecast $1,658K."
    )
    assert "$5,762K" in out
    assert "$5,761K" in out
    assert "$1,658K" in out


# ──────────────────────────────────────────────────────────────────────────
# B8 — send_notification(admin_only=True) DMs every admin, never the channel
# ──────────────────────────────────────────────────────────────────────────


def test_send_notification_admin_only_dms_admin_users(monkeypatch):
    """admin_only=True ignores channel/reply_to and DMs every SLACK_ADMIN_USER_IDS user.

    The public channel must NEVER receive operational telemetry. This is the
    contract behind the 2026-05-12 self-heal: catastrophic alerts go to admin
    DMs only; recoverable in-band failures stay invisible (B7 retry loop).
    """
    from unittest.mock import patch
    import slack_bot

    monkeypatch.setenv("SLACK_ADMIN_USER_IDS", "U0000000000,U0OTHER")

    # Capture conversations_open + chat_postMessage calls.
    fake_client = MagicMock()
    fake_client.conversations_open = MagicMock(
        side_effect=lambda users: {"channel": {"id": f"D_{users[0]}"}}
    )
    fake_client.chat_postMessage = MagicMock(return_value={"ts": "ts-dm"})

    with patch.object(slack_bot.app, "client", fake_client):
        slack_bot.send_notification(
            severity="critical",
            summary="Catastrophic failure: db connection lost",
            channel="C_PUBLIC_DO_NOT_USE",  # MUST be ignored
            reply_to="thread-ignore-me",  # MUST be ignored
            admin_only=True,
        )

    # Both admins were DMed.
    assert fake_client.conversations_open.call_count == 2
    opened_users = [
        c.kwargs.get("users") or c.args[0]
        for c in fake_client.conversations_open.call_args_list
    ]
    flat_users = [u[0] if isinstance(u, list) else u for u in opened_users]
    assert "U0000000000" in flat_users
    assert "U0OTHER" in flat_users

    # All postMessage calls went to DM channels — never to the public channel.
    for call in fake_client.chat_postMessage.call_args_list:
        ch = call.kwargs.get("channel") or (call.args[0] if call.args else None)
        assert ch != "C_PUBLIC_DO_NOT_USE", (
            f"admin_only=True leaked a message to public channel: {ch}"
        )
        assert ch is not None, "postMessage called without a channel"
        assert ch.startswith("D_"), f"expected DM channel, got {ch}"


def test_send_notification_admin_only_no_admins_no_post(monkeypatch):
    """Empty SLACK_ADMIN_USER_IDS → log warning, no Slack call. Graceful degrade."""
    from unittest.mock import patch
    import slack_bot

    monkeypatch.setenv("SLACK_ADMIN_USER_IDS", "")

    fake_client = MagicMock()
    with patch.object(slack_bot.app, "client", fake_client):
        result = slack_bot.send_notification(
            severity="critical",
            summary="Drop me — no admins configured",
            admin_only=True,
        )

    assert result == ""
    fake_client.chat_postMessage.assert_not_called()
    fake_client.conversations_open.assert_not_called()


# ──────────────────────────────────────────────────────────────────────────
# Mention-stripping regression (2026-05-13 live incident)
#
# The earlier ``text.split(">", 1)[-1]`` only stripped the leading
# ``<@USERID>`` mention. A message with the mention at the END left an
# empty string after stripping, which triggered the canned
# "Ask me a question…" fallback instead of dispatching an investigation.
# Anthropic console showed zero sessions created for the dropped message.
# ──────────────────────────────────────────────────────────────────────────


def _mention_strip(text: str) -> str:
    """Mirror the production stripping behavior (slack_bot._handle_incoming)
    so the test is decoupled from the rest of that function's wiring."""
    import re

    return re.sub(r"<@[A-Z0-9]+>", "", text).strip()


def test_mention_strip_handles_leading_mention():
    assert (
        _mention_strip("<@U0123ABC> what is the win rate?") == "what is the win rate?"
    )


def test_mention_strip_handles_trailing_mention():
    """The 2026-05-13 repro: mention at the END must NOT erase the question."""
    msg = "Give me a breakdown of customers by product <@U0000000002>"
    assert _mention_strip(msg) == "Give me a breakdown of customers by product"


def test_mention_strip_handles_inline_mention():
    msg = "hey <@U0123ABC> can you check the pipeline?"
    assert (
        _mention_strip(msg)
        == "hey  can you check the pipeline?".replace("  ", " ").strip()
        or _mention_strip(msg) == "hey  can you check the pipeline?"
    )
    # Either single-space or double-space is acceptable — the strip+split
    # downstream in _handle_incoming tolerates extra whitespace.


def test_mention_strip_handles_multiple_mentions():
    msg = "<@U0123ABC> ping <@U0456DEF> about retention"
    assert (
        _mention_strip(msg) == "ping  about retention".replace("  ", " ")
        or _mention_strip(msg) == "ping  about retention"
    )


def test_mention_strip_no_mention_passes_through():
    msg = "what changed in the deal flow last week?"
    assert _mention_strip(msg) == msg


def test_mention_strip_only_mention_returns_empty():
    """A bare mention with no question still falls through to the canned reply."""
    assert _mention_strip("<@U0000000002>") == ""


# ──────────────────────────────────────────────────────────────────────────
# Lifecycle reaction helpers — 👁 / ⏰ / ✅ / ❌
#
# Reaction calls are status-indicator hygiene. They MUST swallow exceptions
# (Slack already_reacted / no_reaction / rate-limit / missing scope) and
# MUST early-return without calling Slack when channel/ts/emoji is missing.
# These three tests lock the contract in.
# ──────────────────────────────────────────────────────────────────────────


def test_add_reaction_success_calls_reactions_add():
    """Happy path: add_reaction returns True and invokes reactions_add once."""
    from unittest.mock import MagicMock, patch
    import slack_bot

    fake_client = MagicMock()
    fake_client.reactions_add = MagicMock(return_value={"ok": True})
    with patch.object(slack_bot.app, "client", fake_client):
        result = slack_bot.add_reaction("C0CHAN", "1700000000.000100", "eye")

    assert result is True
    fake_client.reactions_add.assert_called_once_with(
        channel="C0CHAN", timestamp="1700000000.000100", name="eye"
    )


def test_add_reaction_already_reacted_is_swallowed():
    """``already_reacted`` is the idempotency case — return True, do not raise."""
    from unittest.mock import MagicMock, patch
    import slack_bot

    fake_client = MagicMock()
    fake_client.reactions_add = MagicMock(
        side_effect=Exception("already_reacted: bot has this reaction")
    )
    with patch.object(slack_bot.app, "client", fake_client):
        result = slack_bot.add_reaction("C0CHAN", "1700000000.000100", "eye")

    assert result is True


def test_remove_reaction_no_reaction_is_swallowed():
    """``no_reaction`` — nothing to remove — must return True without raising."""
    from unittest.mock import MagicMock, patch
    import slack_bot

    fake_client = MagicMock()
    fake_client.reactions_remove = MagicMock(
        side_effect=Exception("no_reaction on this message")
    )
    with patch.object(slack_bot.app, "client", fake_client):
        result = slack_bot.remove_reaction("C0CHAN", "1700000000.000100", "eye")

    assert result is True


def test_add_reaction_missing_args_returns_false_without_calling_slack():
    """Empty channel / ts / emoji must short-circuit before any API call."""
    from unittest.mock import MagicMock, patch
    import slack_bot

    fake_client = MagicMock()
    with patch.object(slack_bot.app, "client", fake_client):
        assert slack_bot.add_reaction("", "1700000000.000100", "eye") is False
        assert slack_bot.add_reaction("C0CHAN", "", "eye") is False
        assert slack_bot.add_reaction("C0CHAN", "1700000000.000100", "") is False

    fake_client.reactions_add.assert_not_called()


def test_transition_reaction_calls_remove_then_add():
    """transition_reaction runs remove first, then add. Order matters: the
    indicator briefly may show both rather than a stuck stale emoji."""
    from unittest.mock import MagicMock, patch
    import slack_bot

    fake_client = MagicMock()
    fake_client.reactions_remove = MagicMock(return_value={"ok": True})
    fake_client.reactions_add = MagicMock(return_value={"ok": True})

    with patch.object(slack_bot.app, "client", fake_client):
        slack_bot.transition_reaction(
            "C0CHAN", "1700000000.000100", remove="alarm_clock", add="white_check_mark"
        )

    # Remove fires before add — order matters for the lifecycle UX.
    fake_client.reactions_remove.assert_called_once_with(
        channel="C0CHAN", timestamp="1700000000.000100", name="alarm_clock"
    )
    fake_client.reactions_add.assert_called_once_with(
        channel="C0CHAN", timestamp="1700000000.000100", name="white_check_mark"
    )


# ── Plan #49 — member_joined_channel handler ──────────────────────────────


def test_handle_member_joined_bot_self(monkeypatch):
    """When the bot itself joins a channel, push_channel_description is
    dispatched on a daemon thread.

    The push is daemon-threaded so we assert ``threading.Thread`` was
    constructed with the right target + args rather than letting a real
    thread race with the test exit.
    """
    import importlib
    import sys

    import slack_bot

    importlib.reload(slack_bot)
    slack_bot._bot_user_id = "UBOTTEST"

    fake_module = MagicMock()
    monkeypatch.setitem(sys.modules, "channel_descriptions", fake_module)

    fake_thread_cls = MagicMock()
    fake_thread = MagicMock()
    fake_thread_cls.return_value = fake_thread
    monkeypatch.setattr("threading.Thread", fake_thread_cls)

    event = {
        "type": "member_joined_channel",
        "user": "UBOTTEST",
        "channel": "C0NEWCHAN",
        "channel_type": "C",
        "team": "T0TEAM",
    }
    slack_bot.handle_member_joined_channel(event)

    assert fake_thread_cls.called, "expected threading.Thread to be invoked"
    thread_kwargs = fake_thread_cls.call_args.kwargs
    assert thread_kwargs["target"] is fake_module.push_channel_description
    assert thread_kwargs["args"] == ("C0NEWCHAN",)
    assert thread_kwargs["daemon"] is True
    fake_thread.start.assert_called_once()


def test_handle_member_joined_other_user(monkeypatch):
    """When a human joins, NOT the bot, no description push fires."""
    import importlib
    import sys

    import slack_bot

    importlib.reload(slack_bot)
    slack_bot._bot_user_id = "UBOTTEST"

    fake_module = MagicMock()
    monkeypatch.setitem(sys.modules, "channel_descriptions", fake_module)

    fake_thread_cls = MagicMock()
    monkeypatch.setattr("threading.Thread", fake_thread_cls)

    event = {
        "type": "member_joined_channel",
        "user": "UHUMAN",
        "channel": "C0NEWCHAN",
        "channel_type": "C",
        "team": "T0TEAM",
    }
    slack_bot.handle_member_joined_channel(event)

    fake_thread_cls.assert_not_called()
    fake_module.push_channel_description.assert_not_called()


def test_handle_member_joined_no_bot_id(monkeypatch):
    """If _bot_user_id is None (startup race), no dispatch."""
    import importlib
    import sys

    import slack_bot

    importlib.reload(slack_bot)
    slack_bot._bot_user_id = None  # startup race

    fake_module = MagicMock()
    monkeypatch.setitem(sys.modules, "channel_descriptions", fake_module)

    fake_thread_cls = MagicMock()
    monkeypatch.setattr("threading.Thread", fake_thread_cls)

    event = {
        "type": "member_joined_channel",
        "user": "UBOTTEST",
        "channel": "C0NEWCHAN",
    }
    slack_bot.handle_member_joined_channel(event)

    fake_thread_cls.assert_not_called()
    fake_module.push_channel_description.assert_not_called()


# ── Resume meta-intent (PR-B of Plan #52) ──────────────────────────────────
#
# Plan #52 PR-B adds a ``resume`` meta-intent class so that "continue" /
# "resume" / "go ahead" / "keep going" / "proceed" in an active Slack thread
# wakes the existing session via ``events.send`` instead of spawning a new
# investigation. Refs: docs/plans/52-cohesive-2026-05-19-incident-response.md
# §3 PR-B; docs/plans/50-stop-lifecycle-and-continue-routing.md §3.


def test_classify_meta_intent_continue_returns_resume():
    """``continue`` is the canonical wakeup trigger — must classify as resume."""
    from slack_bot import classify_meta_intent

    assert classify_meta_intent("continue") == "resume"


def test_classify_meta_intent_resume_returns_resume():
    """``resume the agent`` is within the 6-token cap and matches \\bresume\\b."""
    from slack_bot import classify_meta_intent

    assert classify_meta_intent("resume the agent") == "resume"


def test_handle_meta_intent_resume_sends_wakeup(monkeypatch):
    """Running investigation + resume intent → events.send with the nudge text."""
    import slack_bot

    fake_client = MagicMock()
    monkeypatch.setattr("session_runner.client", fake_client)

    posts: list[str] = []
    slack_bot._handle_meta_intent(
        intent="resume",
        investigation={
            "id": 101,
            "session_id": "sesn_EXAMPLE",
            "status": "running",
        },
        user="U1",
        thread_ts="T1",
        say=lambda msg, thread_ts=None: posts.append(msg),  # pyright: ignore[reportUnusedParameter]
    )

    fake_client.beta.sessions.events.send.assert_called_once()
    call_kwargs = fake_client.beta.sessions.events.send.call_args.kwargs
    assert call_kwargs["session_id"] == "sesn_EXAMPLE"
    events = call_kwargs["events"]
    assert len(events) == 1
    assert events[0]["type"] == "user.message"
    nudge_text = events[0]["content"][0]["text"]
    assert "proceed" in nudge_text.lower()
    assert "post_report" in nudge_text
    # Slack ack uses the :arrows_counterclockwise: emoji per the plan.
    assert len(posts) == 1
    assert ":arrows_counterclockwise:" in posts[0]
    assert "Nudged" in posts[0]


def test_handle_meta_intent_resume_no_session_id_says_no_active(monkeypatch):
    """Investigation row without a session_id → polite message, no events.send."""
    import slack_bot

    fake_client = MagicMock()
    monkeypatch.setattr("session_runner.client", fake_client)

    posts: list[str] = []
    slack_bot._handle_meta_intent(
        intent="resume",
        investigation={"id": 102, "session_id": None, "status": "running"},
        user="U1",
        thread_ts="T1",
        say=lambda msg, thread_ts=None: posts.append(msg),  # pyright: ignore[reportUnusedParameter]
    )

    fake_client.beta.sessions.events.send.assert_not_called()
    assert len(posts) == 1
    assert "No active session to resume." in posts[0]


def test_handle_meta_intent_resume_completed_investigation_says_finished(monkeypatch):
    """Completed investigation → "already finished" message, no events.send."""
    import slack_bot

    fake_client = MagicMock()
    monkeypatch.setattr("session_runner.client", fake_client)

    posts: list[str] = []
    slack_bot._handle_meta_intent(
        intent="resume",
        investigation={
            "id": 103,
            "session_id": "sesn_EXAMPLE",
            "status": "completed",
        },
        user="U1",
        thread_ts="T1",
        say=lambda msg, thread_ts=None: posts.append(msg),  # pyright: ignore[reportUnusedParameter]
    )

    fake_client.beta.sessions.events.send.assert_not_called()
    assert len(posts) == 1
    assert "already finished" in posts[0].lower()


def test_resume_not_in_action_meta_intents():
    """Resume is non-action — feedback-prefix Design B routing must apply.

    If ``"resume"`` were in ``ACTION_META_INTENTS``, the PR #239 safety would
    route ``"always resume after a stall"`` to memory instead of waking the
    session. Resume is a read-only wakeup nudge, not a terminate-or-interrupt
    action.
    """
    from slack_bot import ACTION_META_INTENTS

    assert "resume" not in ACTION_META_INTENTS
