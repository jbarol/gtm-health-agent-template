"""Tests for the Compresr POC integration in self_heal._analyze_session.

Plan #37, Task #63 — the FIRST Compresr integration call site. Shadow eval
(Task #64) must run for 1 week before broader rollout per Plan #37.

These tests verify:
  * compress_prompt is invoked with model="latte_v1", call_site="self_heal",
    query=<session_id>, and min_chars=1000.
  * The Messages API user-message content uses the value returned by
    compress_prompt (compressed-or-original, whichever the wrapper gives back).
  * When compress_prompt falls back to the original text, _analyze_session
    still completes successfully and returns the parsed analysis.

Run:
    cd orchestrator && python3 -m pytest self_heal_compresr_poc_test.py -q
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import patch

# Required env vars for config.py to import without raising. Mirrors the
# pattern in compresr_client_test.py — setdefault means a real .env wins.
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
    """Build a SimpleNamespace shaped like an Anthropic Messages API response."""
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


def test_compress_prompt_called_with_correct_args():
    """compress_prompt must be invoked with the expected model, call_site, query."""
    import self_heal

    session_id = "sess_abc123"
    fake_response = _build_fake_messages_response()

    with (
        patch.object(self_heal, "compress_prompt") as mock_compress,
        patch.object(self_heal.client.messages, "create", return_value=fake_response),
    ):
        mock_compress.return_value = "COMPRESSED-PAYLOAD"

        self_heal._analyze_session(
            session_id=session_id,
            session_type="ad-hoc",
            tool_errors=_sample_tool_errors(),
            session_errors=[],
            tool_calls=[],
            agent_messages=["agent says hello"],
        )

    assert mock_compress.call_count == 1
    args, kwargs = mock_compress.call_args
    # First positional arg is the JSON-stringified summary.
    assert args and isinstance(args[0], str)
    assert '"session_id": "sess_abc123"' in args[0]
    assert '"tool_errors"' in args[0]
    # Kwargs must match the POC contract.
    assert kwargs["model"] == "latte_v1"
    assert kwargs["call_site"] == "self_heal"
    assert kwargs["query"] == session_id
    assert kwargs["min_chars"] == 1000


def test_messages_api_uses_compressed_text():
    """The Messages API user content must contain the value compress_prompt returned."""
    import self_heal

    fake_response = _build_fake_messages_response()
    sentinel = "<<COMPRESSED-SENTINEL-12345>>"

    with (
        patch.object(self_heal, "compress_prompt", return_value=sentinel),
        patch.object(
            self_heal.client.messages, "create", return_value=fake_response
        ) as mock_create,
    ):
        self_heal._analyze_session(
            session_id="sess_xyz",
            session_type="dream",
            tool_errors=_sample_tool_errors(),
            session_errors=[],
            tool_calls=[],
            agent_messages=[],
        )

    assert mock_create.call_count == 1
    _, kwargs = mock_create.call_args
    user_msg = kwargs["messages"][0]
    assert user_msg["role"] == "user"
    assert sentinel in user_msg["content"]


def test_fallback_to_original_text_still_works():
    """When compress_prompt returns the original (fallback path), self_heal still works."""
    import self_heal

    fake_response = _build_fake_messages_response(
        text='{"learnings": [{"issue": "x", "root_cause": "y", "memory_note": "z"}], "code_fixes": []}'
    )

    # Mimic the wrapper's fallback contract: return the original text unchanged.
    def _passthrough(text, **kwargs):
        return text

    with (
        patch.object(self_heal, "compress_prompt", side_effect=_passthrough),
        patch.object(
            self_heal.client.messages, "create", return_value=fake_response
        ) as mock_create,
    ):
        result = self_heal._analyze_session(
            session_id="sess_fallback",
            session_type="ad-hoc",
            tool_errors=_sample_tool_errors(),
            session_errors=[],
            tool_calls=[],
            agent_messages=[],
        )

    # Result is the parsed JSON from the (mocked) Messages API response.
    assert isinstance(result, dict)
    assert "learnings" in result
    assert result["learnings"][0]["issue"] == "x"

    # The user-message content must contain the original JSON-stringified summary,
    # not some compressed sentinel — because the wrapper passed it through.
    _, kwargs = mock_create.call_args
    user_msg_content = kwargs["messages"][0]["content"]
    assert '"session_id": "sess_fallback"' in user_msg_content
    # The full summary dict is present (key fields).
    summary_dict = {
        "session_id": "sess_fallback",
        "type": "ad-hoc",
        "tool_call_count": 0,
        "tool_errors": _sample_tool_errors(),
        "session_errors": [],
        "agent_messages_sample": [],
    }
    expected_summary_text = json.dumps(summary_dict, indent=2, default=str)
    assert expected_summary_text in user_msg_content
