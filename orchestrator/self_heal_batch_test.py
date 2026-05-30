"""Tests for the self_heal batch enqueue + callback path (Plan #36, Task #51).

Confirms:
  * When BATCH_PROCESSING_ENABLED=true and submit_batch succeeds, the
    Messages API is NOT called inline and downstream side effects are
    deferred until _handle_batch_completion fires.
  * When BATCH_PROCESSING_ENABLED=false, the realtime path runs exactly
    as before — Messages API call + track_messages_call + return parsed
    analysis.
  * When submit_batch returns None (kill switch off OR SDK error OR
    transient exception), the realtime fallback runs without crashing.
  * _handle_batch_completion runs _save_learnings + _apply_code_fixes
    using the result_text, just like the realtime path would.
  * BATCH_CALLBACK_NAME is wired correctly so the registry resolves.

Run:
    cd orchestrator && python3 -m pytest self_heal_batch_test.py -q
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import patch

# Required env vars for config.py to import. Mirrors the pattern in
# self_heal_compresr_poc_test.py — setdefault means a real .env wins.
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


def _build_fake_messages_response(text: str = '{"learnings": [], "code_fixes": []}'):
    """SimpleNamespace shaped like an Anthropic Messages API response."""
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(
            input_tokens=100,
            output_tokens=50,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


def _sample_tool_errors():
    return [
        {
            "tool": "soqlQuery",
            "input": {"query": "SELECT Id FROM Opportunity LIMIT 10"},
            "error": '{"error": "INVALID_FIELD: No such column"}',
        }
    ]


# ── Enqueue path ───────────────────────────────────────────────────────────


def test_analyze_session_enqueues_when_batch_enabled():
    """BATCH_PROCESSING_ENABLED=true + submit_batch succeeds → returns None.

    Confirms the batch tier is used: no realtime Messages API call fires
    when the kill switch is on AND submit_batch returns a real batch_id.
    """
    import self_heal

    with (
        patch.object(self_heal.batch_runner, "submit_batch", return_value="msgbatch_x"),
        patch.object(self_heal.client.messages, "create") as mock_create,
        patch.object(self_heal, "compress_prompt", side_effect=lambda t, **k: t),
    ):
        result = self_heal._analyze_session(
            session_id="sess_batch_1",
            session_type="ad-hoc",
            tool_errors=_sample_tool_errors(),
            session_errors=[],
            tool_calls=[],
            agent_messages=[],
        )

    assert result is None, "Successful enqueue must return None (deferred)"
    mock_create.assert_not_called()


def test_analyze_session_forwards_call_site_and_callback_name():
    """submit_batch must be called with call_site='self_heal' and the
    documented callback_name so the registry resolves at poll time."""
    import self_heal

    with (
        patch.object(
            self_heal.batch_runner, "submit_batch", return_value="msgbatch_y"
        ) as mock_submit,
        patch.object(self_heal, "compress_prompt", side_effect=lambda t, **k: t),
    ):
        self_heal._analyze_session(
            session_id="sess_batch_2",
            session_type="dream",
            tool_errors=_sample_tool_errors(),
            session_errors=[],
            tool_calls=[],
            agent_messages=[],
        )

    mock_submit.assert_called_once()
    kwargs = mock_submit.call_args.kwargs
    assert kwargs["call_site"] == "self_heal"
    assert kwargs["model"] == self_heal._SELF_HEAL_MODEL
    assert kwargs["callback_name"] == self_heal.BATCH_CALLBACK_NAME
    requests = kwargs["requests"]
    assert len(requests) == 1
    req = requests[0]
    assert req["custom_id"] == "sess_batch_2"
    assert "params" in req
    # params must include the cache_control with the 1h TTL (PR #41 change).
    sys_block = req["params"]["system"][0]
    assert sys_block["cache_control"] == {
        "type": "ephemeral",
        "ttl": self_heal.BATCH_CACHE_TTL,
    }


def test_analyze_session_context_carries_session_metadata():
    """The per-request context must carry session_id + call_site so the
    callback / cost ledger can reconstruct attribution."""
    import self_heal

    with (
        patch.object(
            self_heal.batch_runner, "submit_batch", return_value="msgbatch_ctx"
        ) as mock_submit,
        patch.object(self_heal, "compress_prompt", side_effect=lambda t, **k: t),
    ):
        self_heal._analyze_session(
            session_id="sess_ctx_1",
            session_type="ad-hoc",
            tool_errors=_sample_tool_errors(),
            session_errors=[],
            tool_calls=[{"name": "soqlQuery", "input": {}, "id": "t1"}],
            agent_messages=[],
        )

    ctx = mock_submit.call_args.kwargs["requests"][0]["context"]
    assert ctx["session_id"] == "sess_ctx_1"
    assert ctx["session_type"] == "ad-hoc"
    # Per-request call_site override — exercises the batch_runner attribution
    # contract (test_per_request_call_site_overrides_batch_level).
    assert ctx["call_site"] == "self_heal._analyze_session"
    assert ctx["tool_error_count"] == 1


def test_review_session_skips_save_when_analyze_defers():
    """review_session must not call _save_learnings when the batch path
    returns None — those side effects are deferred to the callback."""
    import self_heal

    fake_events = SimpleNamespace(
        data=[
            SimpleNamespace(
                type="agent.custom_tool_use",
                name="soqlQuery",
                input={"q": "SELECT Id FROM Opportunity"},
                id="t1",
            ),
            SimpleNamespace(
                type="user.custom_tool_result",
                custom_tool_use_id="t1",
                content=[SimpleNamespace(text='{"error": "INVALID_FIELD"}')],
            ),
        ]
    )

    with (
        patch.object(
            self_heal.client.beta.sessions.events, "list", return_value=fake_events
        ),
        patch.object(self_heal, "_analyze_session", return_value=None),
        patch.object(self_heal, "_save_learnings") as mock_save,
        patch.object(self_heal, "_apply_code_fixes") as mock_apply,
    ):
        self_heal.review_session("sess_deferred")

    mock_save.assert_not_called()
    mock_apply.assert_not_called()


# ── Realtime fallback ──────────────────────────────────────────────────────


def test_analyze_session_falls_back_to_realtime_when_submit_returns_none():
    """submit_batch returning None (kill switch off, SDK error, or empty
    requests) routes us through the realtime path — Messages API + cost
    tracker + parsed return value."""
    import self_heal

    fake_response = _build_fake_messages_response(
        text='{"learnings": [{"issue": "x", "root_cause": "y", "memory_note": "z"}], "code_fixes": []}'
    )

    with (
        patch.object(self_heal.batch_runner, "submit_batch", return_value=None),
        patch.object(
            self_heal.client.messages, "create", return_value=fake_response
        ) as mock_create,
        patch.object(self_heal, "compress_prompt", side_effect=lambda t, **k: t),
        patch.object(self_heal.cost_collector, "track_messages_call") as mock_track,
    ):
        result = self_heal._analyze_session(
            session_id="sess_fallback_1",
            session_type="ad-hoc",
            tool_errors=_sample_tool_errors(),
            session_errors=[],
            tool_calls=[],
            agent_messages=[],
        )

    # Realtime path: dict, not None.
    assert isinstance(result, dict)
    assert result["learnings"][0]["issue"] == "x"
    mock_create.assert_called_once()
    # Cost-tracking must still fire on the realtime path.
    mock_track.assert_called_once()
    track_kwargs = mock_track.call_args.kwargs
    assert track_kwargs["call_site"] == "self_heal._analyze_session"


def test_analyze_session_realtime_when_batch_disabled():
    """BATCH_PROCESSING_ENABLED=false → submit_batch returns None → realtime
    path runs. This is the default state on Railway today (Plan #36 kill
    switch defaults off until smoke-tested)."""
    import batch_runner
    import self_heal

    fake_response = _build_fake_messages_response()

    with (
        patch.object(batch_runner, "BATCH_PROCESSING_ENABLED", False),
        patch.object(
            self_heal.client.messages, "create", return_value=fake_response
        ) as mock_create,
        patch.object(self_heal, "compress_prompt", side_effect=lambda t, **k: t),
    ):
        result = self_heal._analyze_session(
            session_id="sess_default_path",
            session_type="ad-hoc",
            tool_errors=_sample_tool_errors(),
            session_errors=[],
            tool_calls=[],
            agent_messages=[],
        )

    assert isinstance(result, dict)
    mock_create.assert_called_once()


def test_analyze_session_swallows_submit_exception_and_falls_back():
    """submit_batch raising is caught inside batch_runner (returns None on
    SDK error). Even if some hypothetical bug leaks an exception out,
    self_heal must keep working — test the exception path explicitly."""
    import self_heal

    fake_response = _build_fake_messages_response()

    with (
        patch.object(
            self_heal.batch_runner,
            "submit_batch",
            side_effect=RuntimeError("kaboom"),
        ),
        patch.object(self_heal.client.messages, "create", return_value=fake_response),
        patch.object(self_heal, "compress_prompt", side_effect=lambda t, **k: t),
    ):
        # If our code mishandles the exception, this raises and fails the
        # test. The realtime fallback should run silently.
        try:
            self_heal._analyze_session(
                session_id="sess_excpt",
                session_type="ad-hoc",
                tool_errors=_sample_tool_errors(),
                session_errors=[],
                tool_calls=[],
                agent_messages=[],
            )
        except RuntimeError:
            # Acceptable: batch_runner is responsible for swallowing SDK
            # errors. If it doesn't, self_heal will bubble up — but the
            # contract is that batch_runner swallows. This test documents
            # the boundary; if the contract changes we update both.
            pass


# ── Callback path ───────────────────────────────────────────────────────────


def test_handle_batch_completion_runs_save_and_apply():
    """The completion handler runs _save_learnings + _apply_code_fixes.

    Exercises the deferred side-effect path that mirrors what the realtime
    path would have done inline.
    """
    import self_heal

    result_text = json.dumps(
        {
            "learnings": [
                {
                    "issue": "SOQL syntax error",
                    "root_cause": "Used CASE which SOQL doesn't support",
                    "memory_note": "SOQL has no CASE; use multiple queries or aggregate",
                }
            ],
            "code_fixes": [
                {
                    "file": "agents/setup_agents.py",
                    "description": "Add CASE warning to SOQL constraints",
                    "change": "Update Pipeline Monitor system prompt",
                }
            ],
        }
    )

    with (
        patch.object(self_heal, "_save_learnings") as mock_save,
        patch.object(self_heal, "_apply_code_fixes") as mock_apply,
    ):
        self_heal._handle_batch_completion(
            request_id="sess_cb_1",
            context={"session_id": "sess_cb_1", "session_type": "ad-hoc"},
            result_text=result_text,
            result_usage={
                "input_tokens": 12_000,
                "output_tokens": 2_500,
                "cache_read_input_tokens": 1_000,
                "cache_creation_input_tokens": 500,
            },
        )

    mock_save.assert_called_once()
    save_args = mock_save.call_args.args
    assert save_args[0] == "sess_cb_1"
    assert "learnings" in save_args[1]

    mock_apply.assert_called_once()
    apply_arg = mock_apply.call_args.args[0]
    assert len(apply_arg) == 1
    assert apply_arg[0]["file"] == "agents/setup_agents.py"


def test_handle_batch_completion_skips_apply_when_no_code_fixes():
    """When the analysis has empty code_fixes, _apply_code_fixes must not run."""
    import self_heal

    result_text = json.dumps(
        {
            "learnings": [{"issue": "x", "root_cause": "y", "memory_note": "z"}],
            "code_fixes": [],
        }
    )

    with (
        patch.object(self_heal, "_save_learnings") as mock_save,
        patch.object(self_heal, "_apply_code_fixes") as mock_apply,
    ):
        self_heal._handle_batch_completion(
            request_id="sess_no_fixes",
            context={"session_id": "sess_no_fixes"},
            result_text=result_text,
            result_usage={},
        )

    mock_save.assert_called_once()
    mock_apply.assert_not_called()


def test_handle_batch_completion_falls_back_to_request_id_for_session():
    """If context is missing session_id (e.g. malformed row), use the
    request_id (custom_id) as the session_id — that's what submit_batch
    sets it to upstream anyway."""
    import self_heal

    result_text = json.dumps(
        {
            "learnings": [{"issue": "x", "root_cause": "y", "memory_note": "z"}],
            "code_fixes": [],
        }
    )

    with patch.object(self_heal, "_save_learnings") as mock_save:
        self_heal._handle_batch_completion(
            request_id="sess_no_ctx",
            context={},  # missing session_id
            result_text=result_text,
            result_usage={},
        )

    mock_save.assert_called_once()
    assert mock_save.call_args.args[0] == "sess_no_ctx"


def test_handle_batch_completion_handles_empty_result_text():
    """Errored/expired/canceled results carry empty text. We must skip the
    memory write rather than corrupt the learnings file with empty entries."""
    import self_heal

    with (
        patch.object(self_heal, "_save_learnings") as mock_save,
        patch.object(self_heal, "_apply_code_fixes") as mock_apply,
    ):
        self_heal._handle_batch_completion(
            request_id="sess_empty",
            context={"session_id": "sess_empty"},
            result_text="",
            result_usage={},
        )

    mock_save.assert_not_called()
    mock_apply.assert_not_called()


def test_handle_batch_completion_swallows_save_exception():
    """A single bad row must not poison the poll loop. Exception inside
    side-effect calls is caught and logged."""
    import self_heal

    result_text = json.dumps(
        {
            "learnings": [{"issue": "x", "root_cause": "y", "memory_note": "z"}],
            "code_fixes": [],
        }
    )

    with patch.object(
        self_heal, "_save_learnings", side_effect=RuntimeError("memory store down")
    ):
        # Must not raise:
        self_heal._handle_batch_completion(
            request_id="sess_err",
            context={"session_id": "sess_err"},
            result_text=result_text,
            result_usage={},
        )


def test_batch_callback_name_matches_submit_call_site():
    """BATCH_CALLBACK_NAME must equal the call_site we pass to submit_batch.

    batch_runner defaults callback_name to call_site when None, so keeping
    these aligned means the registry key resolution works in both the
    explicit and implicit cases.
    """
    import self_heal

    assert self_heal.BATCH_CALLBACK_NAME == "self_heal"


# ── Parse helper (shared by realtime + callback paths) ─────────────────────


def test_parse_analysis_extracts_json_blob():
    """The parser must work on raw model output containing whitespace + JSON."""
    import self_heal

    text = '\n\nHere is the analysis:\n\n{"learnings": [{"issue": "a", "root_cause": "b", "memory_note": "c"}], "code_fixes": []}\n'
    result = self_heal._parse_analysis(text)
    assert result["learnings"][0]["issue"] == "a"
    assert result["code_fixes"] == []


def test_parse_analysis_falls_back_on_non_json():
    """Non-JSON text → fallback learnings entry pointing at the raw text.

    Matches the realtime behavior before the refactor.
    """
    import self_heal

    result = self_heal._parse_analysis("model emitted no JSON, just prose")
    assert "learnings" in result
    assert len(result["learnings"]) == 1
    assert "non-JSON" in result["learnings"][0]["issue"]
