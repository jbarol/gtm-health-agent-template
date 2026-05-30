"""Tests for the self_improve batch enqueue + callback path (Plan #36, Task #52).

Confirms:
  * When BATCH_PROCESSING_ENABLED=true and submit_batch succeeds, the
    Messages API is NOT called inline and _save_to_memory + _notify_user
    are NOT invoked from check_for_updates — they fire in the callback.
  * When BATCH_PROCESSING_ENABLED=false (or submit_batch returns None),
    the realtime path runs as before: Messages API + cost tracker +
    inline save+notify.
  * _handle_batch_completion runs _save_to_memory + _notify_user using
    the result_text + context dict (page lists + triggered registry).
  * The context dict round-trips page lists + the F5 triggered registry
    so the deferred DM body matches what the realtime path would emit.
  * Cache_control sets the 1h TTL (PR #41 wiring).

Run:
    cd orchestrator && python3 -m pytest self_improve_batch_test.py -q
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

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


def _build_fake_messages_response(text: str = "Realtime analysis summary."):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(
            input_tokens=1000,
            output_tokens=500,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


# ── Enqueue path ───────────────────────────────────────────────────────────


def test_analyze_changes_enqueues_when_batch_enabled():
    """submit_batch succeeds → _analyze_changes returns None (deferred)."""
    import self_improve

    with (
        patch.object(self_improve, "_fetch_page", return_value="# overview\nContent\n"),
        patch.object(
            self_improve.batch_runner, "submit_batch", return_value="msgbatch_si_1"
        ),
        patch.object(self_improve.client.messages, "create") as mock_create,
    ):
        result = self_improve._analyze_changes(["overview"], [])

    assert result is None, "Successful enqueue must return None (deferred)"
    mock_create.assert_not_called()


def test_analyze_changes_forwards_call_site_and_callback_name():
    """submit_batch wiring: call_site, model, callback_name aligned."""
    import self_improve

    with (
        patch.object(self_improve, "_fetch_page", return_value="content\n"),
        patch.object(
            self_improve.batch_runner, "submit_batch", return_value="msgbatch_si_2"
        ) as mock_submit,
    ):
        self_improve._analyze_changes(["sessions"], ["new-page"])

    mock_submit.assert_called_once()
    kwargs = mock_submit.call_args.kwargs
    assert kwargs["call_site"] == "self_improve"
    assert kwargs["model"] == self_improve._SELF_IMPROVE_MODEL
    assert kwargs["callback_name"] == self_improve.BATCH_CALLBACK_NAME
    requests = kwargs["requests"]
    assert len(requests) == 1
    req = requests[0]
    # custom_id is the daily marker (not session-keyed) — only one request
    # goes out per nightly run.
    assert req["custom_id"].startswith("self_improve_")
    # cache_control must use the 1h TTL.
    sys_block = req["params"]["system"][0]
    assert sys_block["cache_control"] == {
        "type": "ephemeral",
        "ttl": self_improve.BATCH_CACHE_TTL,
    }


def test_analyze_changes_context_carries_page_lists_and_triggers():
    """Context must round-trip everything the callback needs to build the DM."""
    import self_improve

    triggered = [
        ("structured-outputs", self_improve.TRIGGER_PAGES["structured-outputs"]),
    ]
    with (
        patch.object(self_improve, "_fetch_page", return_value="content\n"),
        patch.object(
            self_improve.batch_runner, "submit_batch", return_value="msgbatch_ctx"
        ) as mock_submit,
    ):
        self_improve._analyze_changes(
            ["sessions", "structured-outputs"], ["new-page"], triggered=triggered
        )

    ctx = mock_submit.call_args.kwargs["requests"][0]["context"]
    assert ctx["changed_pages"] == ["sessions", "structured-outputs"]
    assert ctx["new_pages"] == ["new-page"]
    # triggered is flattened to [[page, dict], ...] for JSON round-trip.
    assert len(ctx["triggered"]) == 1
    assert ctx["triggered"][0][0] == "structured-outputs"
    assert ctx["triggered"][0][1]["plan_id"] == 34
    # Per-request call_site override for cost attribution.
    assert ctx["call_site"] == "self_improve._analyze_changes"


def test_check_for_updates_skips_save_and_notify_when_analyze_defers():
    """check_for_updates must NOT call _save_to_memory or _notify_user when
    _analyze_changes returns None — those side effects fire in the callback."""
    import self_improve

    fake_state = {"hashes": {"overview": "old_hash"}, "last_run": "2026-05-10"}
    with (
        patch.object(self_improve, "_load_state", return_value=fake_state),
        patch.object(self_improve, "_save_state") as mock_save_state,
        patch.object(self_improve, "_fetch_page", return_value="new doc content\n"),
        patch.object(self_improve, "_hash_content", return_value="new_hash"),
        patch.object(self_improve, "_analyze_changes", return_value=None),
        patch.object(self_improve, "_save_to_memory") as mock_save_mem,
        patch.object(self_improve, "_notify_user") as mock_notify,
    ):
        self_improve.check_for_updates()

    # State hashes are still persisted (so we don't re-process tomorrow).
    mock_save_state.assert_called_once()
    # But the side effects are deferred — must NOT have fired.
    mock_save_mem.assert_not_called()
    mock_notify.assert_not_called()


def test_check_for_updates_state_persists_after_deferred_analysis():
    """After a deferred (batch) analysis, the new hashes still land on disk."""
    import self_improve

    fake_state = {"hashes": {"overview": "old"}, "last_run": "2026-05-10"}
    persisted = {}

    def _capture(state):
        persisted.update(state)

    with (
        patch.object(self_improve, "_load_state", return_value=fake_state),
        patch.object(self_improve, "_save_state", side_effect=_capture),
        patch.object(self_improve, "_fetch_page", return_value="new content\n"),
        patch.object(self_improve, "_hash_content", return_value="new_hash"),
        patch.object(self_improve, "_analyze_changes", return_value=None),
        patch.object(self_improve, "_save_to_memory"),
        patch.object(self_improve, "_notify_user"),
    ):
        self_improve.check_for_updates()

    assert persisted.get("last_run") is not None
    assert "hashes" in persisted


# ── Realtime fallback ──────────────────────────────────────────────────────


def test_analyze_changes_falls_back_to_realtime_when_submit_returns_none():
    """submit_batch=None → realtime Messages API call + cost track + str return."""
    import self_improve

    fake_response = _build_fake_messages_response("summary text here")

    with (
        patch.object(self_improve, "_fetch_page", return_value="content"),
        patch.object(self_improve.batch_runner, "submit_batch", return_value=None),
        patch.object(
            self_improve.client.messages, "create", return_value=fake_response
        ) as mock_create,
        patch.object(self_improve.cost_collector, "track_messages_call") as mock_track,
    ):
        result = self_improve._analyze_changes(["sessions"], [])

    # Realtime path returns a string summary.
    assert result == "summary text here"
    mock_create.assert_called_once()
    mock_track.assert_called_once()
    track_kwargs = mock_track.call_args.kwargs
    assert track_kwargs["call_site"] == "self_improve._analyze_changes"


def test_analyze_changes_realtime_when_batch_disabled():
    """BATCH_PROCESSING_ENABLED=false → submit_batch returns None → realtime."""
    import batch_runner
    import self_improve

    fake_response = _build_fake_messages_response("offline summary")

    with (
        patch.object(batch_runner, "BATCH_PROCESSING_ENABLED", False),
        patch.object(self_improve, "_fetch_page", return_value="content"),
        patch.object(
            self_improve.client.messages, "create", return_value=fake_response
        ) as mock_create,
    ):
        result = self_improve._analyze_changes(["sessions"], [])

    assert result == "offline summary"
    mock_create.assert_called_once()


def test_analyze_changes_returns_short_circuit_when_no_content():
    """When no doc content fetched, no batch enqueued, no realtime call."""
    import self_improve

    with (
        patch.object(self_improve, "_fetch_page", return_value=""),
        patch.object(self_improve.batch_runner, "submit_batch") as mock_submit,
        patch.object(self_improve.client.messages, "create") as mock_create,
    ):
        result = self_improve._analyze_changes(["overview"], [])

    # Short-circuit message — must NOT trigger any API call.
    assert isinstance(result, str)
    assert "could not be fetched" in result
    mock_submit.assert_not_called()
    mock_create.assert_not_called()


# ── Callback path ───────────────────────────────────────────────────────────


def test_handle_batch_completion_runs_save_and_notify():
    """Completion handler runs _save_to_memory + _notify_user with the
    summary text and the context-decoded page lists + triggered registry."""
    import self_improve

    context = {
        "changed_pages": ["sessions", "structured-outputs"],
        "new_pages": ["onboarding"],
        "triggered": [
            ["structured-outputs", self_improve.TRIGGER_PAGES["structured-outputs"]]
        ],
        "call_site": "self_improve._analyze_changes",
    }

    with (
        patch.object(self_improve, "_save_to_memory") as mock_save,
        patch.object(self_improve, "_notify_user") as mock_notify,
    ):
        self_improve._handle_batch_completion(
            request_id="self_improve_2026-05-11",
            context=context,
            result_text="analysis summary body",
            result_usage={
                "input_tokens": 100_000,
                "output_tokens": 3_000,
                "cache_read_input_tokens": 50_000,
                "cache_creation_input_tokens": 0,
            },
        )

    mock_save.assert_called_once_with("analysis summary body")

    mock_notify.assert_called_once()
    notify_args = mock_notify.call_args
    pos_args = notify_args.args
    kw = notify_args.kwargs
    # _notify_user(summary, changed_pages, new_pages, triggered=...).
    assert pos_args[0] == "analysis summary body"
    assert pos_args[1] == ["sessions", "structured-outputs"]
    assert pos_args[2] == ["onboarding"]
    # triggered re-hydrated to list of tuples.
    triggered = kw["triggered"]
    assert len(triggered) == 1
    assert triggered[0][0] == "structured-outputs"
    assert triggered[0][1]["plan_id"] == 34


def test_handle_batch_completion_skips_when_result_text_empty():
    """Empty result text (errored/expired/canceled) → no DM, no memory write."""
    import self_improve

    with (
        patch.object(self_improve, "_save_to_memory") as mock_save,
        patch.object(self_improve, "_notify_user") as mock_notify,
    ):
        self_improve._handle_batch_completion(
            request_id="self_improve_empty",
            context={"changed_pages": [], "new_pages": [], "triggered": []},
            result_text="",
            result_usage={},
        )

    mock_save.assert_not_called()
    mock_notify.assert_not_called()


def test_handle_batch_completion_handles_missing_context_keys():
    """Missing keys in context default to empty lists. _notify_user still fires."""
    import self_improve

    with (
        patch.object(self_improve, "_save_to_memory") as mock_save,
        patch.object(self_improve, "_notify_user") as mock_notify,
    ):
        self_improve._handle_batch_completion(
            request_id="self_improve_minimal",
            context={},  # no keys
            result_text="ok",
            result_usage={},
        )

    mock_save.assert_called_once_with("ok")
    mock_notify.assert_called_once()
    pos_args = mock_notify.call_args.args
    assert pos_args[1] == []  # changed_pages
    assert pos_args[2] == []  # new_pages
    assert mock_notify.call_args.kwargs["triggered"] == []


def test_handle_batch_completion_swallows_save_exception():
    """A side-effect exception must not poison the poll loop."""
    import self_improve

    with patch.object(
        self_improve, "_save_to_memory", side_effect=RuntimeError("memory store down")
    ):
        # Must not raise.
        self_improve._handle_batch_completion(
            request_id="r1",
            context={"changed_pages": [], "new_pages": [], "triggered": []},
            result_text="text",
            result_usage={},
        )


def test_batch_callback_name_matches_submit_call_site():
    """BATCH_CALLBACK_NAME aligned with call_site so registry resolution works."""
    import self_improve

    assert self_improve.BATCH_CALLBACK_NAME == "self_improve"
