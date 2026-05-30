"""Tests for Track F — in-thread meta-intent routing in ``slack_bot``.

Validates that ``_handle_incoming``:

  * Routes ``status`` / ``cancel`` / ``pause`` messages to the meta-intent
    pathway when an investigation row is already attached to the thread.
  * Does NOT spawn a Coordinator session (no ``_on_question_callback``).
  * Does NOT post the canned kickoff ack template for these messages.
  * Falls through to the normal question pipeline when there is no
    existing investigation, even if the text matches a meta-intent
    pattern.
  * Falls through to the normal question pipeline when the message is
    too long (the meta-intent is not the dominant content).

Run:
    cd orchestrator && python3 -m pytest slack_bot_meta_intent_test.py -v
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

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


import slack_bot  # noqa: E402


# ─── classify_meta_intent: pure classifier ──────────────────────────────────


@pytest.mark.parametrize(
    "text,expected",
    [
        # status
        ("status?", "status"),
        ("give me a status update", "status"),
        ("any update?", "status"),
        ("what's the status", "status"),
        ("how's it going?", "status"),
        ("progress?", "status"),
        # cancel
        ("cancel", "cancel"),
        ("stop", "cancel"),
        ("abort", "cancel"),
        ("kill it", "cancel"),
        ("nevermind", "cancel"),
        ("never mind", "cancel"),
        # pause
        ("pause", "pause"),
        ("hold on", "pause"),
        ("wait", "pause"),
    ],
)
def test_classify_meta_intent_matches_short_phrases(text, expected):
    assert slack_bot.classify_meta_intent(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        # Long messages — meta-intent isn't dominant
        "After the status update, also tell me X about Y",
        "I have a question about progress in the Q3 pipeline review",
        "could you wait for the database snapshot and then re-query",
        # No meta-intent keyword
        "what is the win rate this quarter",
        "how many opportunities are in stage Negotiation",
        # Empty / whitespace
        "",
        "   ",
        None,
    ],
)
def test_classify_meta_intent_returns_none(text):
    assert slack_bot.classify_meta_intent(text) is None


def test_classify_meta_intent_case_insensitive():
    assert slack_bot.classify_meta_intent("STATUS") == "status"
    assert slack_bot.classify_meta_intent("Cancel") == "cancel"
    assert slack_bot.classify_meta_intent("Hold ON") == "pause"


def test_classify_meta_intent_cancel_beats_status():
    """When both patterns could match, cancel (more destructive) wins."""
    assert slack_bot.classify_meta_intent("cancel status") == "cancel"


def test_classify_meta_intent_does_not_swallow_updates_word():
    """ "updates" alone isn't a status intent — only "update" forms we list."""
    # "updates" the noun appears in many real questions; we don't want to
    # trip on it. The regex matches "update" as a word — "updates" matches
    # because ``\b`` allows it, but the length heuristic should still
    # protect anything longer than 6 tokens.
    out = slack_bot.classify_meta_intent(
        "give me the latest customer updates from the team"
    )
    assert out is None  # 9 tokens — falls through


# ─── _handle_incoming dispatch under meta-intent ────────────────────────────


@pytest.fixture(autouse=True)
def reset_handlers(monkeypatch):
    monkeypatch.setattr(slack_bot, "_seen_events", {})
    monkeypatch.setattr(slack_bot, "_on_question_callback", None)
    monkeypatch.setattr(slack_bot, "_on_feedback_callback", None)
    yield


def _stub_thread_investigation(monkeypatch, row):
    """Patch the thread lookup to return ``row`` regardless of thread_ts."""
    monkeypatch.setattr(
        slack_bot,
        "_lookup_thread_investigation",
        lambda _ts: row,  # pyright: ignore[reportUnusedParameter]
    )


def test_status_intent_calls_status_snippet_and_does_not_spawn(monkeypatch):
    """Existing investigation + 'status update' → status snippet, no spawn."""
    inv = {"id": 7, "session_id": "sesn_EXAMPLE", "status": "running"}
    _stub_thread_investigation(monkeypatch, inv)

    # Spy on status_responder.status_snippet.
    import status_responder

    monkeypatch.setattr(
        status_responder,
        "status_snippet",
        lambda inv_id: f"[snippet for {inv_id}]",
    )

    # Question callback must NOT be invoked.
    cb_called = {"count": 0}

    def cb(*_a, **_kw):
        cb_called["count"] += 1

    monkeypatch.setattr(slack_bot, "_on_question_callback", cb)

    posts: list[str] = []

    def fake_say(msg, thread_ts=None):
        # ``thread_ts`` name is preserved (not renamed to ``_thread_ts``)
        # because production calls pass it as a keyword argument.
        del thread_ts
        posts.append(msg)

    slack_bot._handle_incoming(
        text="give me a status update",
        user="U1",
        thread_ts="T1",
        say=fake_say,
        event_ts="evt-status-1",
        channel_id="C1",
    )

    assert cb_called["count"] == 0, "must not spawn Coordinator session"
    assert posts == ["[snippet for 7]"], (
        f"expected only the status snippet, got: {posts!r}"
    )
    # Critically — no kickoff ack template
    for p in posts:
        assert "On it" not in p
        assert "investigating now" not in p


def test_cancel_intent_archives_session_and_updates_db(monkeypatch):
    inv = {"id": 11, "session_id": "sesn_EXAMPLE", "status": "running"}
    _stub_thread_investigation(monkeypatch, inv)

    # Spy on the Anthropic SDK archive call.
    fake_client = MagicMock()
    monkeypatch.setattr("session_runner.client", fake_client)

    # Spy on db_adapter.cancel_investigation.
    import db_adapter

    cancel_calls: list[tuple[int, str]] = []
    monkeypatch.setattr(
        db_adapter,
        "cancel_investigation",
        lambda inv_id, reason="user cancelled": (
            cancel_calls.append((inv_id, reason)) or True
        ),
    )

    cb_called = {"count": 0}

    def cb(*_a, **_kw):
        cb_called["count"] += 1

    monkeypatch.setattr(slack_bot, "_on_question_callback", cb)

    posts: list[str] = []

    def fake_say(msg, thread_ts=None):
        # ``thread_ts`` name is preserved (not renamed to ``_thread_ts``)
        # because production calls pass it as a keyword argument.
        del thread_ts
        posts.append(msg)

    slack_bot._handle_incoming(
        text="cancel",
        user="U1",
        thread_ts="T1",
        say=fake_say,
        event_ts="evt-cancel-1",
        channel_id="C1",
    )

    fake_client.beta.sessions.archive.assert_called_once_with("sesn_EXAMPLE")
    assert len(cancel_calls) == 1 and cancel_calls[0][0] == 11
    assert posts == ["Stopped. Session archived."]
    assert cb_called["count"] == 0


def test_cancel_intent_when_session_archive_fails_still_updates_db(monkeypatch):
    """Archive call blowing up shouldn't prevent the DB update or the post."""
    inv = {"id": 12, "session_id": "sesn_EXAMPLE", "status": "running"}
    _stub_thread_investigation(monkeypatch, inv)

    fake_client = MagicMock()
    fake_client.beta.sessions.archive.side_effect = RuntimeError("404")
    monkeypatch.setattr("session_runner.client", fake_client)

    import db_adapter

    cancel_calls: list[int] = []
    monkeypatch.setattr(
        db_adapter,
        "cancel_investigation",
        lambda inv_id, reason="user cancelled": cancel_calls.append(inv_id) or True,  # pyright: ignore[reportUnusedParameter]
    )

    posts: list[str] = []
    slack_bot._handle_incoming(
        text="stop",
        user="U2",
        thread_ts="T2",
        say=lambda msg, thread_ts=None: posts.append(msg),  # pyright: ignore[reportUnusedParameter]
        event_ts="evt-cancel-2",
        channel_id="C1",
    )

    assert cancel_calls == [12]
    assert posts == ["Stopped. Session archived."]


def test_pause_intent_posts_helper_message(monkeypatch):
    inv = {"id": 8, "session_id": "sesn_EXAMPLE", "status": "running"}
    _stub_thread_investigation(monkeypatch, inv)

    cb_called = {"count": 0}

    def cb(*_a, **_kw):
        cb_called["count"] += 1

    monkeypatch.setattr(slack_bot, "_on_question_callback", cb)

    posts: list[str] = []
    slack_bot._handle_incoming(
        text="pause",
        user="U1",
        thread_ts="T1",
        say=lambda msg, thread_ts=None: posts.append(msg),  # pyright: ignore[reportUnusedParameter]
        event_ts="evt-pause-1",
        channel_id="C1",
    )

    assert cb_called["count"] == 0
    assert len(posts) == 1
    assert "can't pause" in posts[0]
    assert "cancel" in posts[0]


# ─── Fall-through behavior ──────────────────────────────────────────────────


def test_status_intent_without_investigation_falls_through(monkeypatch):
    """No investigation row → meta-intent path skipped, question pipeline runs."""
    _stub_thread_investigation(monkeypatch, None)

    cb_args: list[tuple] = []

    def cb(
        user, text, thread_ts, channel_id, ack_fn, verbosity="normal", event_ts=None
    ):
        cb_args.append((user, text, thread_ts, channel_id, verbosity))

    monkeypatch.setattr(slack_bot, "_on_question_callback", cb)

    posts: list[str] = []
    slack_bot._handle_incoming(
        text="give me a status update",
        user="U1",
        thread_ts="T_new",
        say=lambda msg, thread_ts=None: posts.append(msg),  # pyright: ignore[reportUnusedParameter]
        event_ts="evt-status-2",
        channel_id="C1",
    )

    assert len(cb_args) == 1, "fresh question — question callback must be invoked"
    # The callback didn't call ack_fn in our stub, so the default ack fires.
    assert any("On it" in p for p in posts)


def test_long_message_with_status_word_falls_through(monkeypatch):
    """ "After the status update, also tell me X" → question pipeline."""
    inv = {"id": 9, "session_id": "sesn_EXAMPLE", "status": "running"}
    _stub_thread_investigation(monkeypatch, inv)

    cb_args: list[tuple] = []

    def cb(
        user, text, thread_ts, channel_id, ack_fn, verbosity="normal", event_ts=None
    ):
        cb_args.append((user, text, thread_ts, channel_id, verbosity))

    monkeypatch.setattr(slack_bot, "_on_question_callback", cb)

    posts: list[str] = []
    slack_bot._handle_incoming(
        text="After the status update, also tell me what the win rate is",
        user="U1",
        thread_ts="T1",
        say=lambda msg, thread_ts=None: posts.append(msg),  # pyright: ignore[reportUnusedParameter]
        event_ts="evt-long-1",
        channel_id="C1",
    )

    assert len(cb_args) == 1, "long message must route to question pipeline"


def test_empty_message_after_mention_strip_falls_through(monkeypatch):
    """Bare @mention (e.g. "<@U_BOT>") → prompt message, not meta-intent."""
    inv = {"id": 10, "session_id": "sesn_EXAMPLE", "status": "running"}
    _stub_thread_investigation(monkeypatch, inv)

    cb_called = {"count": 0}

    def cb(*_a, **_kw):
        cb_called["count"] += 1

    monkeypatch.setattr(slack_bot, "_on_question_callback", cb)

    posts: list[str] = []
    slack_bot._handle_incoming(
        text="<@U_BOT>",  # strips to ""
        user="U1",
        thread_ts="T1",
        say=lambda msg, thread_ts=None: posts.append(msg),  # pyright: ignore[reportUnusedParameter]
        event_ts="evt-empty-1",
        channel_id="C1",
    )

    # Empty-body branch posts a help message and returns — no meta-intent,
    # no question spawn.
    assert cb_called["count"] == 0
    assert any("Ask me a question" in p for p in posts)


def test_feedback_with_status_word_in_active_thread_routes_to_meta_intent(
    monkeypatch,
):
    """ "always include status updates" in an active thread → status snippet.

    Status is a NON-ACTION meta-intent (read-only query) — it doesn't
    terminate or interrupt the session. Per the PR #239 split, NON-ACTION
    meta-intents in an active thread still fire (Design B behavior) so the
    live session can adopt continuation directives that read like "always
    do X going forward."

    The split:
      - ACTION meta-intents (cancel, pause) + feedback prefix → save rule
        (Design A restored — PR #97 codex-review safety).
      - NON-ACTION meta-intents (status) + feedback prefix → fire the
        intent (Design B preserved — mid-flight continuation).

    Net behavior here: feedback callback does NOT fire; status_snippet IS
    called.
    """
    inv = {"id": 42, "session_id": "sesn_EXAMPLE", "status": "running"}
    _stub_thread_investigation(monkeypatch, inv)

    # Spy on status_responder — Design B post-state: meta-intent fires.
    import status_responder

    status_called = {"count": 0}

    def fake_status(_inv_id):
        status_called["count"] += 1
        return f"[snippet for {_inv_id}]"

    monkeypatch.setattr(status_responder, "status_snippet", fake_status)

    # Feedback callback must NOT fire (Design B bypass).
    feedback_calls: list[tuple] = []

    def feedback_cb(user, text, thread_ts, channel_id):
        feedback_calls.append((user, text, thread_ts, channel_id))

    monkeypatch.setattr(slack_bot, "_on_feedback_callback", feedback_cb)

    # Question callback must NOT fire either — meta-intent short-circuits.
    cb_called = {"count": 0}

    def cb(*_a, **_kw):
        cb_called["count"] += 1

    monkeypatch.setattr(slack_bot, "_on_question_callback", cb)

    posts: list[str] = []
    slack_bot._handle_incoming(
        text="always include status updates",
        user="U1",
        thread_ts="T1",
        say=lambda msg, thread_ts=None: posts.append(msg),  # pyright: ignore[reportUnusedParameter]
        event_ts="evt-fb-status",
        channel_id="C1",
    )

    assert status_called["count"] == 1, "meta-intent must fire (Design B path)"
    assert feedback_calls == [], (
        "Design B: feedback prefix in active thread must NOT route to memory"
    )
    assert cb_called["count"] == 0, "question pipeline must not be invoked"
    assert posts == ["[snippet for 42]"]


def test_feedback_with_stop_word_in_active_thread_saves_rule_not_cancel(
    monkeypatch,
):
    """ "never stop after one error" in an active thread → save rule, no cancel.

    Cancel is an ACTION meta-intent — it terminates the running session. Per
    the PR #239 split, ACTION meta-intents on a feedback-prefixed message in
    an active thread route BACK to feedback (Design A restored). The user is
    telling us a rule ("never stop after one error"), not commanding us to
    cancel the live investigation.

    This restores the original codex review PR #97 comment 3223872884
    contract: standing instructions that contain action keywords like
    ``stop`` / ``cancel`` MUST be saved to memory, NOT executed as control
    commands. PR #235 had inverted this in the name of mid-flight
    continuation; PR #239 keeps the continuation behavior for NON-ACTION
    intents (status) while restoring the safety for ACTION intents (cancel,
    pause).
    """
    inv = {"id": 43, "session_id": "sesn_EXAMPLE", "status": "running"}
    _stub_thread_investigation(monkeypatch, inv)

    import db_adapter

    cancel_calls: list[int] = []
    monkeypatch.setattr(
        db_adapter,
        "cancel_investigation",
        lambda inv_id, reason="user cancelled": cancel_calls.append(inv_id) or True,  # pyright: ignore[reportUnusedParameter]
    )

    fake_client = MagicMock()
    monkeypatch.setattr("session_runner.client", fake_client)

    feedback_calls: list[tuple] = []

    def feedback_cb(user, text, thread_ts, channel_id):
        feedback_calls.append((user, text, thread_ts, channel_id))

    monkeypatch.setattr(slack_bot, "_on_feedback_callback", feedback_cb)

    cb_called = {"count": 0}

    def cb(*_a, **_kw):
        cb_called["count"] += 1

    monkeypatch.setattr(slack_bot, "_on_question_callback", cb)

    posts: list[str] = []
    slack_bot._handle_incoming(
        text="never stop after one error",
        user="U1",
        thread_ts="T1",
        say=lambda msg, thread_ts=None: posts.append(msg),  # pyright: ignore[reportUnusedParameter]
        event_ts="evt-fb-stop",
        channel_id="C1",
    )

    # Critical: NO cancel side effects.
    assert cancel_calls == [], (
        "ACTION meta-intent on feedback prefix in active thread must NOT "
        "trigger db_adapter.cancel_investigation (PR #97 / PR #239 contract)"
    )
    fake_client.beta.sessions.archive.assert_not_called()
    # Rule was saved.
    assert len(feedback_calls) == 1, "rule must be saved via feedback callback"
    assert feedback_calls[0][1] == "never stop after one error"
    # Question pipeline did not run.
    assert cb_called["count"] == 0, "question pipeline must not be invoked"
    # User saw the standing-instructions ack.
    assert len(posts) == 1
    assert "Saved to standing instructions" in posts[0]
    assert "Stopped" not in posts[0]


def test_feedback_with_no_meta_intent_in_active_thread_routes_to_question_pipeline(
    monkeypatch,
):
    """ "always include a chart" in an active thread → question pipeline.

    Design B retention case: a feedback-prefixed message in an active thread
    that DOES NOT match any meta-intent keyword (no status / cancel / pause
    word) must bypass the memory short-circuit and reach the live session.
    This is the original Design B intent — mid-flight continuation directives
    like ``"always include a chart of X"`` should reshape the in-flight
    response, not be saved as standing policy.

    Net behavior: feedback callback does NOT fire; question callback DOES
    fire (with the original feedback-prefixed text), no meta-intent handler
    runs.
    """
    inv = {"id": 50, "session_id": "sesn_EXAMPLE", "status": "running"}
    _stub_thread_investigation(monkeypatch, inv)

    feedback_calls: list[tuple] = []

    def feedback_cb(user, text, thread_ts, channel_id):
        feedback_calls.append((user, text, thread_ts, channel_id))

    monkeypatch.setattr(slack_bot, "_on_feedback_callback", feedback_cb)

    cb_args: list[tuple] = []

    def cb(
        user, text, thread_ts, channel_id, ack_fn, verbosity="normal", event_ts=None
    ):
        cb_args.append((user, text, thread_ts, channel_id, verbosity))

    monkeypatch.setattr(slack_bot, "_on_question_callback", cb)

    # status_snippet must not be called — no meta-intent match expected.
    import status_responder

    status_called = {"count": 0}

    def fake_status(_inv_id):
        status_called["count"] += 1
        return "[should not run]"

    monkeypatch.setattr(status_responder, "status_snippet", fake_status)

    posts: list[str] = []
    slack_bot._handle_incoming(
        text="always include a chart",
        user="U1",
        thread_ts="T1",
        say=lambda msg, thread_ts=None: posts.append(msg),  # pyright: ignore[reportUnusedParameter]
        event_ts="evt-fb-chart",
        channel_id="C1",
    )

    assert feedback_calls == [], (
        "Design B: feedback prefix in active thread with NO meta-intent "
        "match must NOT route to memory"
    )
    assert status_called["count"] == 0, "no meta-intent keyword → no status fire"
    assert len(cb_args) == 1, "question pipeline must run for the live session"
    assert cb_args[0][1] == "always include a chart"


def test_cancel_on_completed_investigation_is_a_no_op_with_user_message(monkeypatch):
    """Late /cancel on a finished investigation — DB unchanged, polite message.

    Codex review PR #97 comment 3223872886: previously this rewrote
    ``status`` and ``completed_at`` on a terminal row, corrupting analytics.
    Now the DB layer rejects the update and the user gets a clear message.
    """
    inv = {"id": 99, "session_id": "sesn_EXAMPLE", "status": "completed"}
    _stub_thread_investigation(monkeypatch, inv)

    import db_adapter

    # Real (well, stubbed) cancel_investigation that returns False when the
    # row is terminal — mirrors the SQL WHERE clause behavior.
    cancel_calls: list[int] = []

    def stub_cancel(inv_id, reason="user cancelled"):
        cancel_calls.append(inv_id)
        return False  # terminal row, nothing updated

    monkeypatch.setattr(db_adapter, "cancel_investigation", stub_cancel)

    fake_client = MagicMock()
    monkeypatch.setattr("session_runner.client", fake_client)

    posts: list[str] = []
    slack_bot._handle_incoming(
        text="cancel",
        user="U1",
        thread_ts="T1",
        say=lambda msg, thread_ts=None: posts.append(msg),  # pyright: ignore[reportUnusedParameter]
        event_ts="evt-cancel-terminal",
        channel_id="C1",
    )

    # DB layer was asked but reported no-op.
    assert cancel_calls == [99]
    # Anthropic session must NOT be archived on a no-op cancel.
    fake_client.beta.sessions.archive.assert_not_called()
    # User gets the "already finished" message, not "Stopped."
    assert len(posts) == 1
    assert "already finished" in posts[0].lower()
    assert "Stopped" not in posts[0]


def test_cancel_on_running_investigation_archives_and_marks(monkeypatch):
    """Happy path: running investigation → DB cancel, session archive, "Stopped"."""
    inv = {"id": 77, "session_id": "sesn_EXAMPLE", "status": "running"}
    _stub_thread_investigation(monkeypatch, inv)

    import db_adapter

    cancel_calls: list[tuple[int, str]] = []

    def stub_cancel(inv_id, reason="user cancelled"):
        cancel_calls.append((inv_id, reason))
        return True  # WHERE matched a running row

    monkeypatch.setattr(db_adapter, "cancel_investigation", stub_cancel)

    fake_client = MagicMock()
    monkeypatch.setattr("session_runner.client", fake_client)

    posts: list[str] = []
    slack_bot._handle_incoming(
        text="cancel",
        user="U2",
        thread_ts="T2",
        say=lambda msg, thread_ts=None: posts.append(msg),  # pyright: ignore[reportUnusedParameter]
        event_ts="evt-cancel-running",
        channel_id="C1",
    )

    assert len(cancel_calls) == 1 and cancel_calls[0][0] == 77
    fake_client.beta.sessions.archive.assert_called_once_with("sesn_EXAMPLE")
    assert posts == ["Stopped. Session archived."]


def test_status_intent_responder_error_still_posts_something(monkeypatch):
    """status_responder raising should be swallowed into a fallback message."""
    inv = {"id": 7, "session_id": "sesn_EXAMPLE", "status": "running"}
    _stub_thread_investigation(monkeypatch, inv)

    import status_responder

    def boom(_inv_id):
        raise RuntimeError("DB down")

    monkeypatch.setattr(status_responder, "status_snippet", boom)

    posts: list[str] = []
    slack_bot._handle_incoming(
        text="status?",
        user="U1",
        thread_ts="T1",
        say=lambda msg, thread_ts=None: posts.append(msg),  # pyright: ignore[reportUnusedParameter]
        event_ts="evt-status-err",
        channel_id="C1",
    )

    assert len(posts) == 1
    # Fallback string content
    assert "investigation" in posts[0].lower()
    # And no kickoff ack
    assert "On it" not in posts[0]
