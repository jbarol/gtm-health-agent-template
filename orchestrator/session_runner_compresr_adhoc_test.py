"""Tests for the Compresr adhoc_kickoff wiring in session_runner.

CLAUDE.md "Prompt compression" section, Tier B opt-in:
    session_runner.run_adhoc_mcp_session kickoff text, gated by
    COMPRESS_ADHOC_KICKOFF (default false). Only compresses if the
    kickoff exceeds 4 KB. Latte_v1 with the user question as the query.

These tests verify:
  1. Flag off → kickoff text untouched, compress_prompt NOT called.
  2. Flag on, kickoff < 4 KB → compress_prompt NOT called.
  3. Flag on, kickoff > 4 KB → compress_prompt called with the right
     model / query / call_site / min_chars.
  4. compress_prompt returns shorter text → that's what gets sent in
     the first user.message of the fresh session.
  5. compress_prompt raises → original kickoff is still sent (graceful
     fallback; mirrors self_heal._analyze_session's defensive pattern).
  6. compress_prompt returns text >= original length → original is
     used (avoid sending a worse prompt than what we built).

Run:
    cd orchestrator && python3 -m pytest session_runner_compresr_adhoc_test.py -v
"""

from __future__ import annotations

import contextlib
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# Required env vars for config.py to import without raising. Mirrors
# the pattern in self_heal_compresr_poc_test.py and compresr_client_test.py.
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


def _captured_kickoff_text(send_events) -> str:
    """Extract the first user.message text from a send_events list."""
    assert send_events, "send_events must not be empty"
    first = send_events[0]
    assert first["type"] == "user.message"
    return first["content"][0]["text"]


def _make_session_obj(session_id: str = "sesn_EXAMPLE_kickoff"):
    return SimpleNamespace(id=session_id)


def _run_with_mocks(
    *,
    question: str,
    flag_on: bool,
    compress_return,
    compress_side_effect=None,
    prompt_padding_chars: int = 0,
):
    """Run run_adhoc_mcp_session under heavy mocks; return (captured_kickoff_text,
    mock_compress).

    - ``question`` is the user-facing question text.
    - ``flag_on`` toggles config.COMPRESS_ADHOC_KICKOFF.
    - ``compress_return`` is the return value for compress_prompt when invoked.
      Ignored if ``compress_side_effect`` is provided.
    - ``compress_side_effect`` sets a side_effect on compress_prompt (e.g. an
      Exception class to verify the defensive fallback path).
    - ``prompt_padding_chars`` lets a caller force the built kickoff prompt
      above the 4 KB threshold by inflating ``_build_adhoc_prompt``'s return
      with a unique sentinel block.
    """
    import session_runner

    # We need a prompt large enough to exceed 4096 chars for the
    # "long kickoff" paths, but the real ``_build_adhoc_prompt`` builds
    # a portco-specific multi-section template. Patch it to a short
    # baseline + padding so test inputs are predictable.
    padded_body = "QUESTION_BODY:" + ("X" * prompt_padding_chars)

    def _fake_build_adhoc_prompt(q, portco_key, response_shape=None):
        # response_shape added by PR 5 (hybrid_data_synthesis routing).
        # This fake ignores it — compression tests don't exercise the
        # shape-routing branch.
        return f"{padded_body}\n\nUSER_QUESTION:{q}"

    def _fake_prepend_session_instructions(p, *, portco_key=None, channel_id=None):
        # Append a small fixed prefix so we know the prepend path ran but
        # we don't blow the prompt past arbitrary thresholds. Real func
        # adds standing rules; that's not relevant to the kickoff
        # compression behavior being tested here.
        return f"STANDING_RULES_PREFIX::{p}"

    captured = {"send_events": None}

    # A delivery_state object with is_delivered()->False routes the
    # caller's post-call branch to the empty-text "no output" path
    # (send_notification + terminalize). We mock send_notification +
    # terminalize so the path is harmless in tests.
    fake_delivery_state = SimpleNamespace(is_delivered=lambda: False, value="no_output")

    def _fake_guarded(*args, **kwargs):
        # Capture the send_events that would have gone to _stream_and_handle.
        # Real signature: _run_investigation_guarded(inv_id, event_ts,
        # channel_id, fn, *args, **kwargs) — but session_runner passes
        # event_ts and channel_id BOTH positionally (3rd/4th args) and as
        # kwargs in the same call, so a strict-signature fake fails with
        # TypeError on the duplicate. Accepting *args/**kwargs and pulling
        # send_events from kwargs sidesteps that — the only thing this
        # test cares about is what got into the first user.message.
        captured["send_events"] = kwargs.get("send_events")
        # Return a benign 4-tuple matching _stream_and_handle's signature.
        # text_parts=[], delivery_state.is_delivered()=False routes the
        # post-call code to the no-output branch (send_notification +
        # terminalize_lifecycle, both mocked below).
        return ([], fake_delivery_state, None, [])

    fake_session = _make_session_obj()
    mock_compress = MagicMock(
        side_effect=compress_side_effect, return_value=compress_return
    )
    if compress_side_effect is not None:
        # MagicMock honors side_effect even when return_value is set, but the
        # tests are clearer if we drop return_value when a side_effect runs.
        mock_compress = MagicMock(side_effect=compress_side_effect)

    # Python 3.9 caps statically nested ``with`` blocks at 20 — drop into
    # ExitStack so we can register N context managers without hitting the
    # SyntaxError "too many statically nested blocks".
    patches = [
        patch.object(session_runner, "compress_prompt", mock_compress),
        patch.object(session_runner._config, "COMPRESS_ADHOC_KICKOFF", flag_on),
        patch.object(
            session_runner, "_build_adhoc_prompt", side_effect=_fake_build_adhoc_prompt
        ),
        patch.object(
            session_runner,
            "_prepend_session_instructions",
            side_effect=_fake_prepend_session_instructions,
        ),
        patch.object(session_runner, "_is_simple_lookup", return_value=False),
        patch.object(session_runner, "_resolve_portco", return_value="testco"),
        patch.object(session_runner, "_resolve_agent_param", return_value="agent_x"),
        patch.object(
            session_runner.client.beta.sessions, "create", return_value=fake_session
        ),
        patch.object(
            session_runner.db_adapter, "create_investigation", return_value=12345
        ),
        patch.object(
            session_runner.db_adapter, "update_investigation", return_value=None
        ),
        patch.object(
            session_runner.db_adapter, "save_thread_session", return_value=None
        ),
        patch.object(
            session_runner.db_adapter, "get_thread_session", return_value=None
        ),
        patch.object(
            session_runner.db_adapter,
            "transition_queued_to_running",
            return_value=True,
        ),
        patch.object(session_runner, "transition_reaction"),
        # Investigation guarded runner — capture send_events instead of
        # running the real Anthropic stream.
        patch("lifecycle._run_investigation_guarded", side_effect=_fake_guarded),
        # Lifecycle terminalization path expects an enum + DB writes;
        # stub both so the test doesn't have to mock the full lifecycle
        # module.
        patch("lifecycle.terminalize_lifecycle", return_value=None),
        patch(
            "lifecycle.DeliveryState",
            new=SimpleNamespace(
                TERMINAL_FAILURE="terminal_failure",
                ALREADY_DELIVERED="already_delivered",
                NEEDS_FALLBACK="needs_fallback",
                NO_OUTPUT="no_output",
                DELIVERED_VIA_POST_ANALYSIS="delivered_via_post_analysis",
            ),
        ),
        # Post-call lifecycle path: no-output branch fires
        # send_notification + _download_session_files + _log_session_usage
        # + review_session. All harmless to mock — none are on the
        # kickoff path under test.
        patch.object(session_runner, "send_notification"),
        patch.object(session_runner, "_download_session_files"),
        patch.object(session_runner, "_log_session_usage"),
        patch.object(session_runner, "review_session"),
    ]
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        session_runner.run_adhoc_mcp_session(
            question=question,
            user_id="U_TEST",
            thread_ts=None,
            channel_id=None,
            already_preprocessed=True,  # skip Prompt Engineer for test simplicity
            verbosity="summary",
        )

    return _captured_kickoff_text(captured["send_events"]), mock_compress


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_flag_off_kickoff_text_untouched_and_no_compress_call():
    """COMPRESS_ADHOC_KICKOFF=False → compress_prompt is never called."""
    kickoff, mock_compress = _run_with_mocks(
        question="What is the win rate?",
        flag_on=False,
        compress_return="UNUSED",
        prompt_padding_chars=10_000,  # well past 4 KB
    )
    # The flag is off, so compression must NOT run regardless of prompt size.
    assert mock_compress.call_count == 0
    # And the kickoff carries the original (uncompressed) text.
    assert "QUESTION_BODY:" in kickoff
    assert "STANDING_RULES_PREFIX::" in kickoff


def test_flag_on_but_kickoff_below_threshold_skips_compression():
    """COMPRESS_ADHOC_KICKOFF=True but len(prompt) <= 4096 → no compress call."""
    kickoff, mock_compress = _run_with_mocks(
        question="What is the win rate?",
        flag_on=True,
        compress_return="UNUSED",
        prompt_padding_chars=0,  # short prompt, well under 4 KB
    )
    assert mock_compress.call_count == 0
    assert "QUESTION_BODY:" in kickoff


def test_flag_on_and_long_kickoff_invokes_compress_with_expected_args():
    """Flag on + len(prompt) > 4096 → compress_prompt called with contract args."""
    question = "How many opps closed last quarter?"
    kickoff, mock_compress = _run_with_mocks(
        question=question,
        flag_on=True,
        compress_return="COMPRESSED-SENTINEL",
        prompt_padding_chars=10_000,  # pushes prompt well past 4 KB
    )
    assert mock_compress.call_count == 1
    args, kwargs = mock_compress.call_args
    # First positional arg is the kickoff prompt itself.
    assert args, "compress_prompt should be invoked positionally with the prompt"
    assert isinstance(args[0], str)
    assert len(args[0]) > 4096
    # Contract per CLAUDE.md.
    assert kwargs["model"] == "latte_v1"
    assert kwargs["query"] == question
    assert kwargs["call_site"] == "adhoc_kickoff"
    assert kwargs["min_chars"] == 4096


def test_compressed_text_replaces_kickoff_when_shorter():
    """compress_prompt returns shorter text → that's what gets sent."""
    sentinel = "<<COMPRESSED-OK>>"
    kickoff, mock_compress = _run_with_mocks(
        question="why did pipeline stall?",
        flag_on=True,
        compress_return=sentinel,
        prompt_padding_chars=10_000,
    )
    assert mock_compress.call_count == 1
    assert kickoff == sentinel


def test_compress_prompt_raises_falls_back_to_original_prompt():
    """compress_prompt raising must NOT break the kickoff — original is sent."""
    kickoff, mock_compress = _run_with_mocks(
        question="give me a forecast",
        flag_on=True,
        compress_return=None,
        compress_side_effect=RuntimeError("simulated compresr blowup"),
        prompt_padding_chars=10_000,
    )
    assert mock_compress.call_count == 1
    # Original kickoff text survived the exception.
    assert "QUESTION_BODY:" in kickoff
    assert "STANDING_RULES_PREFIX::" in kickoff


def test_compressed_text_no_shorter_keeps_original():
    """If compress_prompt returns text >= original length, keep the original.

    compress_prompt's internal `no_compression_benefit` guard already
    returns the original in that case, but we duplicate the defense at
    the call site so the contract is obvious. This test exercises the
    belt-and-braces path.
    """
    # The mock will return a string that is at least as long as the
    # padded prompt — guarantee that by padding the return with extra
    # filler bytes.
    bloated = "BLOATED-RESULT" + ("Z" * 50_000)
    kickoff, mock_compress = _run_with_mocks(
        question="just a question",
        flag_on=True,
        compress_return=bloated,
        prompt_padding_chars=10_000,
    )
    assert mock_compress.call_count == 1
    # The bloated result is rejected; the original (uncompressed) prompt
    # is what actually lands in the kickoff.
    assert kickoff != bloated
    assert "QUESTION_BODY:" in kickoff
    assert "STANDING_RULES_PREFIX::" in kickoff
