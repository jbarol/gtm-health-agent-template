"""Tests for send_notification's requester_id mention behavior.

Locks in the fix where investigation results @-mention the user who started
the Slack thread instead of the global ``SLACK_NOTIFY_USER_IDS`` admin list.

Precedence: ``requester_id`` (when ``reply_to`` is set) > ``SLACK_NOTIFY_USER_IDS``.

Run:
    cd orchestrator && python3 -m pytest mention_requester_test.py -q
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

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


def _flatten_blocks(call_args) -> str:
    """Return all mrkdwn / text strings from the posted blocks as a flat
    string we can substring-search for ``<@U…>`` mention tags. Handles both
    section and context block shapes.
    """
    blocks = call_args.kwargs.get("blocks") or []
    parts: list[str] = []
    for blk in blocks:
        # section blocks → text.text
        text = (blk.get("text") or {}).get("text")
        if text:
            parts.append(text)
        # context blocks → elements[].text
        for el in blk.get("elements") or []:
            t = el.get("text")
            if t:
                parts.append(t)
    # plus the top-level text field
    parts.append(call_args.kwargs.get("text") or "")
    return "\n".join(parts)


def test_requester_id_in_thread_pings_requester_only():
    """When ``requester_id`` is set AND we're posting into a thread,
    the mention is the requester — never the env-var admin list."""
    import slack_bot

    fake_client = MagicMock()
    fake_client.chat_postMessage = MagicMock(return_value={"ts": "ts-1"})

    # Make sure SLACK_NOTIFY_USER_IDS is non-empty so we'd catch a leak.
    with (
        patch.object(slack_bot, "SLACK_NOTIFY_USER_IDS", ["UADMIN1", "UADMIN2"]),
        patch.object(slack_bot.app, "client", fake_client),
    ):
        slack_bot.send_notification(
            severity="critical",
            summary="findings here",
            reply_to="thread-123",
            requester_id="UREQ999",
        )

    fake_client.chat_postMessage.assert_called_once()
    flat = _flatten_blocks(fake_client.chat_postMessage.call_args)
    assert "<@UREQ999>" in flat, f"requester not mentioned; flat={flat!r}"
    assert "<@UADMIN1>" not in flat, f"admin leaked into a thread reply; flat={flat!r}"
    assert "<@UADMIN2>" not in flat, f"admin leaked into a thread reply; flat={flat!r}"


def test_no_requester_id_falls_back_to_admins_in_thread():
    """No ``requester_id`` → legacy behavior: ping the admin list.
    Severity ``critical``/``watch`` triggers the mention context block."""
    import slack_bot

    fake_client = MagicMock()
    fake_client.chat_postMessage = MagicMock(return_value={"ts": "ts-2"})

    with (
        patch.object(slack_bot, "SLACK_NOTIFY_USER_IDS", ["UADMIN1", "UADMIN2"]),
        patch.object(slack_bot.app, "client", fake_client),
    ):
        slack_bot.send_notification(
            severity="critical",
            summary="findings here",
            reply_to="thread-123",
        )

    fake_client.chat_postMessage.assert_called_once()
    flat = _flatten_blocks(fake_client.chat_postMessage.call_args)
    assert "<@UADMIN1>" in flat, (
        f"admin should be pinged when requester_id is absent; flat={flat!r}"
    )
    assert "<@UADMIN2>" in flat, (
        f"admin should be pinged when requester_id is absent; flat={flat!r}"
    )


def test_no_reply_to_uses_admins_even_with_requester_id():
    """Out-of-band (cron-style) post with ``reply_to=None`` — even if
    ``requester_id`` is somehow set, we fall back to the admin list because
    there's no thread to scope the ping to."""
    import slack_bot

    fake_client = MagicMock()
    fake_client.chat_postMessage = MagicMock(return_value={"ts": "ts-3"})

    with (
        patch.object(slack_bot, "SLACK_NOTIFY_USER_IDS", ["UADMIN1"]),
        patch.object(slack_bot.app, "client", fake_client),
    ):
        slack_bot.send_notification(
            severity="critical",
            summary="cron alert",
            reply_to=None,
            requester_id="UREQ999",  # ignored when reply_to is None
        )

    fake_client.chat_postMessage.assert_called_once()
    flat = _flatten_blocks(fake_client.chat_postMessage.call_args)
    assert "<@UADMIN1>" in flat
    assert "<@UREQ999>" not in flat


def test_info_severity_suppresses_mention_block():
    """Mentions only render for ``critical`` and ``watch`` severities (the
    existing contract). ``info`` posts have no mention context block at
    all — verify the requester override doesn't accidentally force one in
    for routine info posts."""
    import slack_bot

    fake_client = MagicMock()
    fake_client.chat_postMessage = MagicMock(return_value={"ts": "ts-4"})

    with (
        patch.object(slack_bot, "SLACK_NOTIFY_USER_IDS", ["UADMIN1"]),
        patch.object(slack_bot.app, "client", fake_client),
    ):
        slack_bot.send_notification(
            severity="info",
            summary="x",
            reply_to="t1",
            requester_id="U123",
        )

    fake_client.chat_postMessage.assert_called_once()
    flat = _flatten_blocks(fake_client.chat_postMessage.call_args)
    # No mention rendered for info severity — neither the requester nor the
    # admin list. The summary text "x" should still be present.
    assert "<@U123>" not in flat
    assert "<@UADMIN1>" not in flat
