"""Tests for ``feedback_capture`` (Plan #30 D1).

Covers:
  - emoji → signal mapping correctness (the public table the
    ``reaction_added`` handler consults)
  - dedup behavior: re-firing the same emoji reaction must produce
    exactly one persisted row even when ``record_feedback`` is invoked
    twice with identical arguments
  - missing-DB graceful fail: no ``DATABASE_URL`` → no exception, no
    DB call, the Slack handler stays alive

Run:
    cd orchestrator && python3 -m pytest feedback_capture_test.py -q
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

import pytest


# Mirror the env-stub pattern from cost_collector_test.py — without these,
# ``import config`` inside ``slack_bot`` raises at module load on a clean
# worktree checkout that has no .env file. setdefault means a real .env
# (when present) still wins.
for _k in (
    "ANTHROPIC_API_KEY",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "SLACK_CHANNEL_ID",
    "ENVIRONMENT_ID",
    "DREAM_AGENT_ID",
    "COORDINATOR_ID",
    "QUICK_AGENT_ID",
    "METHODOLOGY_STORE_ID",
    "HEALTH_STORE_ID",
):
    os.environ.setdefault(_k, "test-stub")

# Drop cached imports so a previously-broken config module doesn't bleed in.
sys.modules.pop("config", None)
sys.modules.pop("feedback_capture", None)


# ──────────────────────────────────────────────────────────────────────────
# Fake psycopg2 connection that emulates ON CONFLICT DO NOTHING
# ──────────────────────────────────────────────────────────────────────────


class _FakeCursor:
    """Minimal cursor that stores rows keyed by the dedup tuple to mimic
    Postgres's ``ON CONFLICT (...) DO NOTHING`` semantics. Only handles the
    one INSERT pattern ``record_feedback`` produces — anything else is a
    no-op execute (which is fine for unit testing).
    """

    def __init__(self, store: list):
        # ``store`` is a dict-like list of tuples we append to on insert.
        self._store = store
        # The dedup index is computed from a subset of the params; we mirror
        # the actual unique index on (portco_key, agent_message_ts, user_id,
        # signal, source).
        self._seen_keys: set = set()
        self.execute_calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql: str, params=None):
        self.execute_calls.append((sql, params))
        if params is None or "INSERT INTO feedback_events" not in sql:
            return
        # Params order matches the INSERT column list in record_feedback:
        # (portco_key, channel_id, thread_ts, user_id, agent_message_ts,
        #  signal, source, raw_text)
        (
            portco_key,
            channel_id,
            thread_ts,
            user_id,
            agent_message_ts,
            signal,
            source,
            raw_text,
        ) = params
        dedup_key = (portco_key, agent_message_ts, user_id, signal, source)
        if dedup_key in self._seen_keys:
            return  # ON CONFLICT DO NOTHING
        self._seen_keys.add(dedup_key)
        self._store.append(
            {
                "portco_key": portco_key,
                "channel_id": channel_id,
                "thread_ts": thread_ts,
                "user_id": user_id,
                "agent_message_ts": agent_message_ts,
                "signal": signal,
                "source": source,
                "raw_text": raw_text,
            }
        )


class _FakeConn:
    def __init__(self, store: list):
        self._cursor = _FakeCursor(store)
        self.commits = 0
        self.closed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


# ──────────────────────────────────────────────────────────────────────────
# record_feedback — DB writes
# ──────────────────────────────────────────────────────────────────────────


def test_record_feedback_writes_one_row(monkeypatch):
    """Happy path: a single call writes one row with the full attribution."""
    import db_adapter
    import feedback_capture

    store: list = []
    fake_conn = _FakeConn(store)
    monkeypatch.setattr(db_adapter, "DATABASE_URL", "postgres://test")
    monkeypatch.setattr(db_adapter, "_connect", lambda: fake_conn)

    feedback_capture.record_feedback(
        portco_key="acme",
        channel_id="C123",
        thread_ts="T456",
        user_id="U789",
        agent_message_ts="M999",
        signal="positive",
        source="emoji",
        raw_text="thumbsup",
    )

    assert len(store) == 1
    row = store[0]
    assert row["portco_key"] == "acme"
    assert row["agent_message_ts"] == "M999"
    assert row["signal"] == "positive"
    assert row["source"] == "emoji"
    assert row["raw_text"] == "thumbsup"
    assert fake_conn.commits == 1
    assert fake_conn.closed is True


def test_record_feedback_dedup_same_signal_twice(monkeypatch):
    """Re-firing the same emoji must collapse via ON CONFLICT — one row."""
    import db_adapter
    import feedback_capture

    store: list = []
    fake_conn = _FakeConn(store)
    monkeypatch.setattr(db_adapter, "DATABASE_URL", "postgres://test")
    monkeypatch.setattr(db_adapter, "_connect", lambda: fake_conn)

    args = dict(
        portco_key="acme",
        channel_id="C123",
        thread_ts="T456",
        user_id="U789",
        agent_message_ts="M999",
        signal="positive",
        source="emoji",
        raw_text="thumbsup",
    )
    feedback_capture.record_feedback(**args)
    feedback_capture.record_feedback(**args)

    assert len(store) == 1, "duplicate reaction must not produce a second row"


def test_record_feedback_distinct_signals_both_persist(monkeypatch):
    """Different signal on the same message → two rows (user changed mind)."""
    import db_adapter
    import feedback_capture

    store: list = []
    fake_conn = _FakeConn(store)
    monkeypatch.setattr(db_adapter, "DATABASE_URL", "postgres://test")
    monkeypatch.setattr(db_adapter, "_connect", lambda: fake_conn)

    base = dict(
        portco_key="acme",
        channel_id="C123",
        thread_ts="T456",
        user_id="U789",
        agent_message_ts="M999",
        source="emoji",
    )
    feedback_capture.record_feedback(**base, signal="positive", raw_text="thumbsup")
    feedback_capture.record_feedback(**base, signal="negative", raw_text="thumbsdown")

    signals = sorted(r["signal"] for r in store)
    assert signals == ["negative", "positive"]


def test_record_feedback_distinct_users_both_persist(monkeypatch):
    """Two users reacting with the same emoji → two rows (consensus signal)."""
    import db_adapter
    import feedback_capture

    store: list = []
    fake_conn = _FakeConn(store)
    monkeypatch.setattr(db_adapter, "DATABASE_URL", "postgres://test")
    monkeypatch.setattr(db_adapter, "_connect", lambda: fake_conn)

    base = dict(
        portco_key="acme",
        channel_id="C123",
        thread_ts="T456",
        agent_message_ts="M999",
        signal="positive",
        source="emoji",
        raw_text="thumbsup",
    )
    feedback_capture.record_feedback(**base, user_id="U_alice")
    feedback_capture.record_feedback(**base, user_id="U_bob")

    users = sorted(r["user_id"] for r in store)
    assert users == ["U_alice", "U_bob"]


# ──────────────────────────────────────────────────────────────────────────
# Missing-DB graceful fail
# ──────────────────────────────────────────────────────────────────────────


def test_record_feedback_no_database_url_is_noop(monkeypatch):
    """Empty DATABASE_URL → no exception, no _connect call, no DB row."""
    import db_adapter
    import feedback_capture

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "")
    connect_called = {"n": 0}

    def _boom():
        connect_called["n"] += 1
        raise AssertionError("_connect must not be called when DATABASE_URL is empty")

    monkeypatch.setattr(db_adapter, "_connect", _boom)

    # Must not raise
    feedback_capture.record_feedback(
        portco_key="acme",
        channel_id="C123",
        thread_ts="T456",
        user_id="U789",
        agent_message_ts="M999",
        signal="positive",
        source="emoji",
        raw_text="thumbsup",
    )

    assert connect_called["n"] == 0


def test_record_feedback_db_error_is_swallowed(monkeypatch, caplog):
    """A DB exception during INSERT must NOT propagate. Slack handler stays alive."""
    import db_adapter
    import feedback_capture

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "postgres://test")

    def _explode():
        raise RuntimeError("simulated DB outage")

    monkeypatch.setattr(db_adapter, "_connect", _explode)

    # Must not raise
    with caplog.at_level("WARNING"):
        feedback_capture.record_feedback(
            portco_key="acme",
            channel_id="C123",
            thread_ts="T456",
            user_id="U789",
            agent_message_ts="M999",
            signal="negative",
            source="emoji",
            raw_text="x",
        )

    # Confirms the "non-fatal" marker for log-search alerting.
    assert any("non-fatal" in r.message for r in caplog.records)


def test_record_feedback_commit_failure_swallowed(monkeypatch):
    """Commit raising mid-transaction must not propagate either."""
    import db_adapter
    import feedback_capture

    class _BoomConn:
        def __init__(self):
            self.closed = False

        def cursor(self):
            class _C:
                def __enter__(self_inner):
                    return self_inner

                def __exit__(self_inner, *a):
                    return False

                def execute(self_inner, sql, params=None):
                    pass

            return _C()

        def commit(self):
            raise RuntimeError("commit failed")

        def close(self):
            self.closed = True

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "postgres://test")
    monkeypatch.setattr(db_adapter, "_connect", lambda: _BoomConn())

    feedback_capture.record_feedback(
        portco_key="acme",
        channel_id="C",
        thread_ts="T",
        user_id="U",
        agent_message_ts="M",
        signal="neutral",
        source="emoji",
    )


# ──────────────────────────────────────────────────────────────────────────
# Emoji → signal mapping (the public table the reaction handler consults)
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "emoji,expected",
    [
        # positive
        ("+1", "positive"),
        ("thumbsup", "positive"),
        ("heavy_check_mark", "positive"),
        ("white_check_mark", "positive"),
        ("tada", "positive"),
        ("100", "positive"),
        ("fire", "positive"),
        ("clap", "positive"),
        ("bow", "positive"),
        # negative
        ("-1", "negative"),
        ("thumbsdown", "negative"),
        ("x", "negative"),
        ("no_entry", "negative"),
        ("confused", "negative"),
        ("disappointed", "negative"),
        ("cry", "negative"),
        ("rage", "negative"),
    ],
)
def test_emoji_to_signal_mapping(emoji, expected):
    """Pin the public mapping table — both directions of the contract."""
    from slack_bot import EMOJI_TO_SIGNAL

    assert EMOJI_TO_SIGNAL[emoji] == expected


@pytest.mark.parametrize(
    "emoji",
    ["eyes", "warning", "question", "smile", "wave", ""],
)
def test_emoji_outside_taxonomy_is_unmapped(emoji):
    """Untracked emoji must not appear in the mapping — silent skip in the handler."""
    from slack_bot import EMOJI_TO_SIGNAL

    assert emoji not in EMOJI_TO_SIGNAL


def test_emoji_mapping_partitions_into_two_signals():
    """Every value in the table is one of the documented signals.

    A future PR can add neutral signals — until then, the public taxonomy
    is strictly {positive, negative}. This test guards against typos
    that would route an emoji into a third class by accident.
    """
    from slack_bot import EMOJI_TO_SIGNAL

    values = set(EMOJI_TO_SIGNAL.values())
    assert values <= {"positive", "negative"}


# ──────────────────────────────────────────────────────────────────────────
# Slack reaction_added handler — emoji routing into record_feedback
# ──────────────────────────────────────────────────────────────────────────


def _make_event(emoji: str, channel: str = "C_FB", ts: str = "M1", user: str = "U1"):
    return {
        "type": "reaction_added",
        "reaction": emoji,
        "user": user,
        "item": {"type": "message", "channel": channel, "ts": ts},
    }


def _make_client_with_message(*, bot_user_id: str = "B_BOT", message_user: str = None):
    """Fake Slack WebClient with conversations_history returning one message."""
    cli = MagicMock()
    cli.conversations_history.return_value = {
        "messages": [
            {
                "ts": "M1",
                "user": message_user or bot_user_id,
                "bot_id": "BX" if message_user is None else None,
                "thread_ts": None,
            }
        ]
    }
    return cli


def test_handler_records_positive_for_bot_message(monkeypatch):
    """Bot-authored message + thumbsup → record_feedback called with positive."""
    import slack_bot

    calls = []

    def fake_record(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(slack_bot, "_bot_user_id", "B_BOT")
    cli = _make_client_with_message(bot_user_id="B_BOT")

    # Patch the lazy import inside the handler.
    import feedback_capture

    monkeypatch.setattr(feedback_capture, "record_feedback", fake_record)

    slack_bot.handle_reaction_added(_make_event("thumbsup"), client=cli)

    assert len(calls) == 1
    assert calls[0]["signal"] == "positive"
    assert calls[0]["source"] == "emoji"
    assert calls[0]["agent_message_ts"] == "M1"


def test_handler_skips_untracked_emoji(monkeypatch):
    """An emoji outside EMOJI_TO_SIGNAL produces no record_feedback call."""
    import feedback_capture
    import slack_bot

    calls = []
    monkeypatch.setattr(
        feedback_capture, "record_feedback", lambda **kw: calls.append(kw)
    )
    monkeypatch.setattr(slack_bot, "_bot_user_id", "B_BOT")
    cli = _make_client_with_message(bot_user_id="B_BOT")

    slack_bot.handle_reaction_added(_make_event("eyes"), client=cli)
    slack_bot.handle_reaction_added(_make_event("wave"), client=cli)

    assert calls == []


def test_handler_skips_non_bot_messages(monkeypatch):
    """Reaction on a human message → no record_feedback call (not feedback on us)."""
    import feedback_capture
    import slack_bot

    calls = []
    monkeypatch.setattr(
        feedback_capture, "record_feedback", lambda **kw: calls.append(kw)
    )
    monkeypatch.setattr(slack_bot, "_bot_user_id", "B_BOT")

    cli = MagicMock()
    cli.conversations_history.return_value = {
        "messages": [
            {
                "ts": "M1",
                "user": "U_HUMAN",
                "bot_id": None,
                "bot_profile": None,
                "thread_ts": None,
            }
        ]
    }

    slack_bot.handle_reaction_added(_make_event("thumbsup"), client=cli)
    assert calls == []


def test_handler_skips_file_reactions(monkeypatch):
    """item.type != "message" (file/file_comment) → no DB write."""
    import feedback_capture
    import slack_bot

    calls = []
    monkeypatch.setattr(
        feedback_capture, "record_feedback", lambda **kw: calls.append(kw)
    )
    monkeypatch.setattr(slack_bot, "_bot_user_id", "B_BOT")

    event = {
        "reaction": "thumbsup",
        "user": "U1",
        "item": {"type": "file", "file": "F123"},
    }
    slack_bot.handle_reaction_added(event)
    assert calls == []


def test_handler_records_negative_for_thumbsdown(monkeypatch):
    """The other end of the taxonomy — negative routing."""
    import feedback_capture
    import slack_bot

    calls = []
    monkeypatch.setattr(
        feedback_capture, "record_feedback", lambda **kw: calls.append(kw)
    )
    monkeypatch.setattr(slack_bot, "_bot_user_id", "B_BOT")
    cli = _make_client_with_message(bot_user_id="B_BOT")

    slack_bot.handle_reaction_added(_make_event("thumbsdown"), client=cli)

    assert len(calls) == 1
    assert calls[0]["signal"] == "negative"
    assert calls[0]["raw_text"] == "thumbsdown"
