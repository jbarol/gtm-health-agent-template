"""Tests for Plan #31 E2 — Slack bot verbosity detection + /verbosity command.

Covers:

  - :func:`slack_bot._resolve_verbosity` resolution order
      1. Explicit prefix (``terse:`` / ``normal:`` / ``verbose:``, plus the
         legacy ``EXPAND_PREFIXES`` family).
      2. Stored ``channel_verbosity_preferences`` row for the channel.
      3. Module-level default (``"normal"``).
  - ``/verbosity`` slash command: set, get, unknown-arg, no-channel.
  - DB graceful fall-through: when ``db_adapter`` raises, the resolver
    silently returns the default rather than breaking the message handler.

Run:
    cd orchestrator && python3 -m pytest slack_bot_verbosity_test.py
"""

from __future__ import annotations

import pytest

import slack_bot


# ─────────────────────────────────────────────────────────────────────────────
# _resolve_verbosity — prefix detection
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "input_text,expected_verbosity,expected_stripped",
    [
        # Explicit canonical tiers
        ("terse: win rate?", "terse", "win rate?"),
        ("normal: what changed?", "normal", "what changed?"),
        ("verbose: full breakdown please", "verbose", "full breakdown please"),
        # Case-insensitive
        ("TERSE: win rate?", "terse", "win rate?"),
        ("Normal: what?", "normal", "what?"),
        ("VERBOSE: details", "verbose", "details"),
        # Legacy expand: family — all map to verbose
        ("expand: tell me everything", "verbose", "tell me everything"),
        ("long: full story", "verbose", "full story"),
        ("details: every field", "verbose", "every field"),
        ("full version: kitchen sink", "verbose", "kitchen sink"),
        ("full: everything", "verbose", "everything"),
        # The literal verbose: prefix wins via EXPLICIT_VERBOSITY_PREFIXES
        # (checked first), but the result is identical to the legacy alias.
        ("verbose: redundant but valid", "verbose", "redundant but valid"),
        # Whitespace after the colon is stripped
        ("terse:   leading spaces", "terse", "leading spaces"),
        # Prefix-only message
        ("terse:", "terse", ""),
    ],
)
def test_resolve_verbosity_explicit_prefix(
    monkeypatch, input_text, expected_verbosity, expected_stripped
):
    """Explicit prefixes always win, regardless of channel pref or default."""

    # Force the channel-pref lookup to raise — proves prefix wins even when
    # the DB layer would otherwise return a different value.
    def raising(*_args, **_kwargs):
        raise RuntimeError("DB lookup should not be reached when prefix is present")

    monkeypatch.setattr("db_adapter.get_channel_verbosity", raising)

    stripped, verbosity = slack_bot._resolve_verbosity(input_text, "C123")
    assert verbosity == expected_verbosity
    assert stripped == expected_stripped


def test_resolve_verbosity_no_prefix_no_channel_pref_returns_default(monkeypatch):
    """No prefix + no stored channel pref → module default ``"normal"``."""
    monkeypatch.setattr("db_adapter.get_channel_verbosity", lambda _ch: None)

    stripped, verbosity = slack_bot._resolve_verbosity("why is win rate down?", "C999")

    assert verbosity == slack_bot.DEFAULT_VERBOSITY == "normal"
    # Text unchanged (no prefix to strip).
    assert stripped == "why is win rate down?"


def test_resolve_verbosity_channel_pref_used_when_no_prefix(monkeypatch):
    """Channel-level stored default applies when no prefix is on the message."""
    monkeypatch.setattr("db_adapter.get_channel_verbosity", lambda ch: "verbose")

    stripped, verbosity = slack_bot._resolve_verbosity("what changed?", "C42")

    assert verbosity == "verbose"
    assert stripped == "what changed?"


def test_resolve_verbosity_prefix_overrides_channel_pref(monkeypatch):
    """A user prefix beats whatever the channel pref says."""
    monkeypatch.setattr("db_adapter.get_channel_verbosity", lambda _ch: "verbose")

    stripped, verbosity = slack_bot._resolve_verbosity("terse: just the number", "C42")

    assert verbosity == "terse"
    assert stripped == "just the number"


def test_resolve_verbosity_db_failure_falls_through_silently(monkeypatch):
    """DB unavailable → resolver returns default instead of raising."""

    def boom(_channel):
        raise RuntimeError("postgres is down")

    monkeypatch.setattr("db_adapter.get_channel_verbosity", boom)

    # Should not raise — the handler loop must never break on DB failure.
    stripped, verbosity = slack_bot._resolve_verbosity("status?", "C42")

    assert verbosity == "normal"
    assert stripped == "status?"


def test_resolve_verbosity_no_channel_id_skips_lookup(monkeypatch):
    """When channel_id is None/empty, skip the DB lookup entirely."""
    calls = []

    def tracked(_channel):
        calls.append(_channel)
        return "verbose"

    monkeypatch.setattr("db_adapter.get_channel_verbosity", tracked)

    stripped, verbosity = slack_bot._resolve_verbosity("status?", None)

    assert verbosity == "normal"
    assert stripped == "status?"
    assert calls == [], "DB should not be queried when channel_id is None"


def test_resolve_verbosity_invalid_stored_value_falls_back_to_default(monkeypatch):
    """A bogus value in the DB (e.g. legacy ``"summary"``) → default ``"normal"``.

    This guards against drift if someone hand-edits the row to an unsupported
    value. The resolver only honors strings in ``VALID_VERBOSITIES``.
    """
    monkeypatch.setattr("db_adapter.get_channel_verbosity", lambda _ch: "summary")

    _stripped, verbosity = slack_bot._resolve_verbosity("status?", "C42")
    assert verbosity == "normal"


def test_resolve_verbosity_empty_text():
    """Empty input → no prefix, return default with empty text."""
    stripped, verbosity = slack_bot._resolve_verbosity("", "C42")
    assert stripped == ""
    assert verbosity == "normal"


# ─────────────────────────────────────────────────────────────────────────────
# /verbosity slash command
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("verbosity", ["terse", "normal", "verbose"])
def test_handle_verbosity_command_sets_channel_pref(monkeypatch, verbosity):
    """``/verbosity <tier>`` writes through to db_adapter.set_channel_verbosity."""
    writes: list[tuple] = []

    def fake_set(channel_id, v, updated_by=None):
        writes.append((channel_id, v, updated_by))
        return True

    monkeypatch.setattr("db_adapter.set_channel_verbosity", fake_set)

    msg = slack_bot._handle_verbosity_command(verbosity, "C42", "U7")

    assert writes == [("C42", verbosity, "U7")]
    assert verbosity in msg
    assert ":white_check_mark:" in msg


def test_handle_verbosity_command_get_shows_current(monkeypatch):
    """``/verbosity`` with no args returns the stored pref."""
    monkeypatch.setattr("db_adapter.get_channel_verbosity", lambda ch: "verbose")

    msg = slack_bot._handle_verbosity_command("", "C42", "U7")

    assert "verbose" in msg
    # Hints at how to change it
    assert "/verbosity" in msg


def test_handle_verbosity_command_get_no_pref_shows_default(monkeypatch):
    """``/verbosity`` with no stored pref shows the module default."""
    monkeypatch.setattr("db_adapter.get_channel_verbosity", lambda _ch: None)

    msg = slack_bot._handle_verbosity_command("", "C42", "U7")

    assert slack_bot.DEFAULT_VERBOSITY in msg
    assert "default" in msg.lower()


def test_handle_verbosity_command_unknown_arg_rejects(monkeypatch):
    """Unknown tier → :warning: with allowed values, no DB write."""
    writes: list = []
    monkeypatch.setattr(
        "db_adapter.set_channel_verbosity",
        lambda *a, **kw: writes.append(a) or True,
    )

    msg = slack_bot._handle_verbosity_command("loud", "C42", "U7")

    assert writes == []
    assert ":warning:" in msg
    assert "loud" in msg
    assert "terse" in msg and "normal" in msg and "verbose" in msg


def test_handle_verbosity_command_no_channel_rejects():
    """Missing channel_id → :warning:, no DB call attempted."""
    msg = slack_bot._handle_verbosity_command("terse", "", "U7")
    assert ":warning:" in msg


def test_handle_verbosity_command_db_failure_returns_warning(monkeypatch):
    """DB write failure surfaces a user-visible warning."""

    def boom(*_args, **_kwargs):
        raise RuntimeError("postgres ate it")

    monkeypatch.setattr("db_adapter.set_channel_verbosity", boom)

    msg = slack_bot._handle_verbosity_command("terse", "C42", "U7")
    assert ":warning:" in msg
    assert "logs" in msg.lower() or "fail" in msg.lower()


# ─────────────────────────────────────────────────────────────────────────────
# on_verbosity_command (Bolt wrapper) — exercises ack/respond plumbing
# ─────────────────────────────────────────────────────────────────────────────


def test_on_verbosity_command_acks_and_responds(monkeypatch):
    """Bolt entrypoint must ack() immediately and respond() once."""
    monkeypatch.setattr("db_adapter.set_channel_verbosity", lambda *a, **kw: True)

    ack_calls: list = []
    respond_calls: list = []

    def fake_ack():
        ack_calls.append(True)

    def fake_respond(text, response_type=None):
        respond_calls.append({"text": text, "response_type": response_type})

    slack_bot.on_verbosity_command(
        fake_ack,
        {"text": "verbose", "channel_id": "C42", "user_id": "U7"},
        fake_respond,
    )

    assert len(ack_calls) == 1
    assert len(respond_calls) == 1
    assert respond_calls[0]["response_type"] == "ephemeral"
    assert "verbose" in respond_calls[0]["text"]


def test_on_verbosity_command_pure_handler_owns_db_errors(monkeypatch):
    """Failure semantics are owned by the pure handler, not the Bolt wrapper.

    The Bolt wrapper catches exceptions in ack() and respond() so the Socket
    Mode loop never dies, but expects ``_handle_verbosity_command`` itself
    to convert internal failures (e.g. DB outage) into user-visible mrkdwn.
    That's covered by :func:`test_handle_verbosity_command_db_failure_returns_warning`.

    This test pins the contract that if the pure handler raises (which
    shouldn't happen in production — it has its own try/except), the
    wrapper does NOT swallow the exception. That way an unhandled bug
    in the handler surfaces in logs rather than vanishing silently.
    """

    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated unhandled bug")

    monkeypatch.setattr(slack_bot, "_handle_verbosity_command", boom)

    ack_calls: list = []

    def fake_ack():
        ack_calls.append(True)

    def fake_respond(*_a, **_kw):
        return None

    with pytest.raises(RuntimeError):
        slack_bot.on_verbosity_command(
            fake_ack,
            {"text": "terse", "channel_id": "C42", "user_id": "U7"},
            fake_respond,
        )

    # ack() still fired before the handler raised — so Slack got its 3-second
    # ack and won't redeliver the command.
    assert len(ack_calls) == 1


# touch
