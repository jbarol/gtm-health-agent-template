"""Tests for slack_bot._handle_incoming verbosity plumbing (E9 — autoplan).

The E9 fix removed the try/except TypeError shim that swallowed verbosity
when callbacks used **kwargs. These tests pin the new contract:

  - Callback receives the verbosity kwarg correctly when it declares it.
  - **kwargs callbacks receive it via kwargs, NOT silently dropped.
  - Callbacks missing the verbosity parameter fail LOUD (TypeError),
    which is the right signal to update the callback.

Plan #31 E2 renamed the verbosity values from ``summary | expanded`` to
the canonical 3-tier ``terse | normal | verbose``. Legacy ``expand:``-family
prefixes still work and now resolve to ``verbose`` (was ``expanded``);
no-prefix messages resolve to ``normal`` (was ``summary``).

Run:
    cd orchestrator && python3 -m pytest handle_incoming_test.py
"""

from __future__ import annotations

import pytest

import slack_bot


@pytest.fixture
def captured(monkeypatch):
    """Capture every (callback, kwargs) the handler dispatches."""
    captured: list[dict] = []

    def fake_say(msg, thread_ts=None):
        captured.append({"_say": msg, "_thread_ts": thread_ts})

    monkeypatch.setattr(slack_bot, "_seen_events", {})
    return captured, fake_say


def test_handle_incoming_passes_verbosity_to_explicit_kwarg_callback(
    captured, monkeypatch
):
    """Callback declares `verbosity` — it must receive the value."""
    received: dict = {}

    def cb(
        user, text, thread_ts, channel_id, ack_fn, verbosity="normal", event_ts=None
    ):
        received["verbosity"] = verbosity
        received["text"] = text

    monkeypatch.setattr(slack_bot, "_on_question_callback", cb)
    monkeypatch.setattr(slack_bot, "_on_feedback_callback", None)
    _, fake_say = captured

    slack_bot._handle_incoming(
        text="expand: why is win rate down?",
        user="U123",
        thread_ts="T1",
        say=fake_say,
        event_ts="evt-1",
        channel_id="C1",
    )

    # Plan #31 E2: ``expand:`` (and the rest of EXPAND_PREFIXES) now map
    # to the canonical ``verbose`` tier instead of the legacy ``expanded``.
    assert received["verbosity"] == "verbose"
    # Prefix stripped from text
    assert received["text"].endswith("why is win rate down?")
    assert not received["text"].lower().startswith("expand:")


def test_handle_incoming_passes_normal_when_no_prefix(captured, monkeypatch):
    received: dict = {}

    def cb(
        user, text, thread_ts, channel_id, ack_fn, verbosity="normal", event_ts=None
    ):
        received["verbosity"] = verbosity

    monkeypatch.setattr(slack_bot, "_on_question_callback", cb)
    monkeypatch.setattr(slack_bot, "_on_feedback_callback", None)
    _, fake_say = captured

    slack_bot._handle_incoming(
        text="why is win rate down?",
        user="U123",
        thread_ts="T1",
        say=fake_say,
        event_ts="evt-2",
        channel_id="C1",
    )

    # Default is ``normal`` (Plan #31 E2). No DB available in tests, so the
    # channel-pref lookup falls through gracefully.
    assert received["verbosity"] == "normal"


def test_kwargs_callback_receives_verbosity_via_kwargs(captured, monkeypatch):
    """E9 regression guard: a **kwargs callback must SEE the verbosity kwarg,
    not silently drop it. Before the fix, the try/except shim's behavior
    was inconsistent — a **kwargs callback got the kwarg but if it ignored
    its kwargs the value was effectively lost without error. We now pass
    verbosity unambiguously.
    """
    received: dict = {}

    def cb(*args, **kwargs):
        # Confirm verbosity is in kwargs explicitly — proves the call site
        # didn't fall back to a 5-arg invocation.
        received["kwargs"] = dict(kwargs)
        received["args_len"] = len(args)

    monkeypatch.setattr(slack_bot, "_on_question_callback", cb)
    monkeypatch.setattr(slack_bot, "_on_feedback_callback", None)
    _, fake_say = captured

    slack_bot._handle_incoming(
        text="long: tell me everything",
        user="U123",
        thread_ts="T1",
        say=fake_say,
        event_ts="evt-3",
        channel_id="C1",
    )

    assert "verbosity" in received["kwargs"], (
        "verbosity must be passed as a kwarg so **kwargs callbacks see it"
    )
    # ``long:`` is a member of EXPAND_PREFIXES → canonical ``verbose`` tier.
    assert received["kwargs"]["verbosity"] == "verbose"


def test_callback_without_verbosity_param_raises_loud(captured, monkeypatch):
    """E9 fix: removed the try/except shim. Callbacks that don't accept
    verbosity now raise TypeError — the right signal to update the callback.
    """

    def cb(user, text, thread_ts, channel_id, ack_fn):
        pytest.fail("should not be invoked — TypeError should fire first")

    monkeypatch.setattr(slack_bot, "_on_question_callback", cb)
    monkeypatch.setattr(slack_bot, "_on_feedback_callback", None)
    _, fake_say = captured

    with pytest.raises(TypeError, match="verbosity"):
        slack_bot._handle_incoming(
            text="why is win rate down?",
            user="U123",
            thread_ts="T1",
            say=fake_say,
            event_ts="evt-4",
            channel_id="C1",
        )


def test_feedback_prefix_in_active_thread_routes_to_question_pipeline(
    captured, monkeypatch
):
    """Design B: a feedback-prefixed message in a thread with an active
    investigation must NOT short-circuit to the memory store. It should
    reach the question pipeline so the running session can process it.

    Reproduces the L9xZx chart-request loss (2026-05-15): user replied
    in a Slack thread with "always include a chart of ..." which matched
    the FEEDBACK_PREFIXES list and got silently routed to memory, never
    reaching the active session.
    """
    received: dict = {}

    def question_cb(
        user, text, thread_ts, channel_id, ack_fn, verbosity="normal", event_ts=None
    ):
        received["text"] = text

    def feedback_cb(*args, **kwargs):
        received["feedback_called"] = True

    monkeypatch.setattr(slack_bot, "_on_question_callback", question_cb)
    monkeypatch.setattr(slack_bot, "_on_feedback_callback", feedback_cb)
    monkeypatch.setattr(
        slack_bot,
        "_lookup_thread_investigation",
        lambda thread_ts: {"id": 999, "status": "running", "thread_ts": thread_ts},
    )
    _, fake_say = captured

    slack_bot._handle_incoming(
        text="always include a chart of win rate by month",
        user="U123",
        thread_ts="T1",
        say=fake_say,
        event_ts="evt-5",
        channel_id="C1",
    )

    assert "text" in received, (
        "active investigation thread + feedback-prefix → must route to question "
        "pipeline, not memory store"
    )
    assert "feedback_called" not in received
    assert received["text"].startswith("always include a chart")


def test_feedback_prefix_with_no_thread_investigation_still_routes_to_memory(
    captured, monkeypatch
):
    """Negative case: when there is no active or recent investigation on the
    thread (or no thread at all), a feedback-prefixed message MUST still go
    to the memory store. The guard added for Design B narrows the bypass
    to active threads only — main-channel feedback is unaffected.
    """
    received: dict = {}

    def question_cb(*args, **kwargs):
        received["question_called"] = True

    def feedback_cb(user, text, thread_ts, channel_id):
        received["feedback_text"] = text

    monkeypatch.setattr(slack_bot, "_on_question_callback", question_cb)
    monkeypatch.setattr(slack_bot, "_on_feedback_callback", feedback_cb)
    monkeypatch.setattr(
        slack_bot, "_lookup_thread_investigation", lambda thread_ts: None
    )
    _, fake_say = captured

    slack_bot._handle_incoming(
        text="always include a chart of win rate by month",
        user="U123",
        thread_ts="T1",
        say=fake_say,
        event_ts="evt-6",
        channel_id="C1",
    )

    assert received.get("feedback_text", "").startswith("always include")
    assert "question_called" not in received


def test_feedback_prefix_with_terminal_investigation_older_than_30min_routes_to_memory(
    captured, monkeypatch
):
    """Negative case: a thread whose only investigation finished >30 min ago
    is no longer "active or recent" — feedback prefix should still route to
    memory. Prevents stale threads from forever swallowing standing instructions.
    """
    from datetime import datetime, timedelta, timezone

    received: dict = {}

    def question_cb(*args, **kwargs):
        received["question_called"] = True

    def feedback_cb(user, text, thread_ts, channel_id):
        received["feedback_text"] = text

    monkeypatch.setattr(slack_bot, "_on_question_callback", question_cb)
    monkeypatch.setattr(slack_bot, "_on_feedback_callback", feedback_cb)
    monkeypatch.setattr(
        slack_bot,
        "_lookup_thread_investigation",
        lambda thread_ts: {
            "id": 998,
            "status": "completed",
            "thread_ts": thread_ts,
            "started_at": datetime.now(timezone.utc) - timedelta(hours=2),
        },
    )
    _, fake_say = captured

    slack_bot._handle_incoming(
        text="remember to always run statistician on >n=30 cohorts",
        user="U123",
        thread_ts="T1",
        say=fake_say,
        event_ts="evt-7",
        channel_id="C1",
    )

    assert received.get("feedback_text", "").startswith("remember to")
    assert "question_called" not in received
