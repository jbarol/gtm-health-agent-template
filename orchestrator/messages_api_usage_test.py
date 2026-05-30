"""Tests for Messages API cache-aware usage extraction, cost math, and logging.

Covers the helper module ``_messages_usage`` plus the integration points in
``self_heal._analyze_session`` and ``self_improve._analyze_changes``. All
Anthropic client calls are mocked so tests run offline.

Run:
    cd orchestrator && python3 -m pytest messages_api_usage_test.py
"""

from __future__ import annotations

import logging
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# Stub the env vars ``config.py`` requires BEFORE any of the integration
# tests below trigger an ``import self_heal`` / ``import self_improve``. Those
# modules import ``config`` at module load. Without these stubs the worktree
# checkout (no .env) raises at collection time. setdefault means a real .env
# (when present) still wins. Mirrors the same pattern in
# self_heal_compresr_poc_test.py and cost_collector_test.py.
for _k, _v in {
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
    os.environ.setdefault(_k, _v)


# ---------- Fake usage objects ---------------------------------------------


def _fake_usage(
    input_tokens=0,
    output_tokens=0,
    cache_read_input_tokens=None,
    cache_creation_input_tokens=None,
):
    """Build a SimpleNamespace shaped like anthropic.types.Usage for Messages API.

    Using SimpleNamespace mirrors how getattr() reads attributes off the real
    SDK Pydantic models; we don't import anthropic at all so tests stay fully
    offline.
    """
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
    )


def _fake_response(usage=None, text="ok"):
    """Build a response object shaped like anthropic.types.Message."""
    block = SimpleNamespace(type="text", text=text)
    return SimpleNamespace(content=[block], usage=usage)


# ---------- extract_messages_usage -----------------------------------------


def test_extract_full_usage():
    from _messages_usage import extract_messages_usage

    u = _fake_usage(
        input_tokens=1234,
        output_tokens=567,
        cache_read_input_tokens=8900,
        cache_creation_input_tokens=2500,
    )
    out = extract_messages_usage(u)
    assert out == {
        "input": 1234,
        "output": 567,
        "cache_read": 8900,
        "cache_write": 2500,
    }


def test_extract_handles_none_usage():
    """If response.usage is None somehow, return zeros (don't crash)."""
    from _messages_usage import extract_messages_usage

    assert extract_messages_usage(None) == {
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_write": 0,
    }


def test_extract_treats_missing_cache_fields_as_zero():
    """When no prompt caching is configured, Anthropic omits the cache fields
    (or sets them to None). Both must coerce to 0 — never crash, never sum
    None into the math."""
    from _messages_usage import extract_messages_usage

    u = _fake_usage(
        input_tokens=100,
        output_tokens=50,
        cache_read_input_tokens=None,
        cache_creation_input_tokens=None,
    )
    out = extract_messages_usage(u)
    assert out["cache_read"] == 0
    assert out["cache_write"] == 0
    assert out["input"] == 100
    assert out["output"] == 50


def test_extract_handles_partial_cache_fields():
    """cache_read present, cache_creation missing — common in steady-state
    after a long-running cache has settled (no new writes, only reads)."""
    from _messages_usage import extract_messages_usage

    u = _fake_usage(
        input_tokens=10,
        output_tokens=20,
        cache_read_input_tokens=5000,
        cache_creation_input_tokens=None,
    )
    out = extract_messages_usage(u)
    assert out["cache_read"] == 5000
    assert out["cache_write"] == 0


# ---------- estimate_messages_cost -----------------------------------------


def test_cost_sonnet_with_full_cache_breakdown():
    """Verify the dollar math against MODEL_COSTS_PER_MTOK for sonnet-4-6.

    Rates per MTOK: input=$3.0, output=$15.0, cache_read=$0.30,
    cache_write_5m=$3.75.

    Inputs: 1000 input, 500 output, 4000 cache_read, 2000 cache_write
    Expected: (1000 * 3 + 500 * 15 + 4000 * 0.30 + 2000 * 3.75) / 1_000_000
            = (3000 + 7500 + 1200 + 7500) / 1_000_000
            = 19200 / 1_000_000 = $0.0192
    """
    from _messages_usage import estimate_messages_cost

    u = _fake_usage(
        input_tokens=1000,
        output_tokens=500,
        cache_read_input_tokens=4000,
        cache_creation_input_tokens=2000,
    )
    cost = estimate_messages_cost(u, "claude-sonnet-4-6")
    assert abs(cost - 0.0192) < 1e-9


def test_cost_opus_pricing():
    """Opus 4.8 rates: input=$5, output=$25, cache_read=$0.50, cache_write_5m=$6.25.

    Inputs: 1000 input, 500 output, 0 cache => $5/M*1000 + $25/M*500
          = 0.005 + 0.0125 = $0.0175
    """
    from _messages_usage import estimate_messages_cost

    u = _fake_usage(input_tokens=1000, output_tokens=500)
    cost = estimate_messages_cost(u, "claude-opus-4-8")
    assert abs(cost - 0.0175) < 1e-9


def test_cost_unknown_model_returns_zero():
    """Future model name we don't have rates for: log a warning, return 0,
    don't crash."""
    from _messages_usage import estimate_messages_cost

    u = _fake_usage(input_tokens=1000, output_tokens=500)
    cost = estimate_messages_cost(u, "claude-future-model-9-9")
    assert cost == 0.0


def test_cost_zero_usage():
    from _messages_usage import estimate_messages_cost

    assert estimate_messages_cost(None, "claude-sonnet-4-6") == 0.0
    assert estimate_messages_cost(_fake_usage(), "claude-sonnet-4-6") == 0.0


def test_cost_uses_5m_rate_for_cache_writes():
    """The Messages API single cache_creation field maps to the 5-minute rate,
    not 1-hour. If anyone refactors to use the 1h rate by accident, this
    catches it.

    1000 cache_write tokens at sonnet 5m rate ($3.75/MTOK) = $0.00375.
    Same tokens at 1h rate ($6.0/MTOK) would be $0.006.
    """
    from _messages_usage import estimate_messages_cost

    u = _fake_usage(cache_creation_input_tokens=1000)
    cost = estimate_messages_cost(u, "claude-sonnet-4-6")
    assert abs(cost - 0.00375) < 1e-9


# ---------- cache_hit_pct --------------------------------------------------


def test_cache_hit_pct_basic():
    """cache_read / (input + cache_read + cache_write)
    = 800 / (200 + 800 + 0) = 80%
    """
    from _messages_usage import cache_hit_pct

    u = _fake_usage(
        input_tokens=200,
        cache_read_input_tokens=800,
        cache_creation_input_tokens=0,
    )
    assert cache_hit_pct(u) == 80.0


def test_cache_hit_pct_with_writes():
    """Writes count in the denominator (they're tokens charged at input-ish
    rates). cache_read / (input + cache_read + cache_write)
    = 100 / (50 + 100 + 50) = 50%
    """
    from _messages_usage import cache_hit_pct

    u = _fake_usage(
        input_tokens=50,
        cache_read_input_tokens=100,
        cache_creation_input_tokens=50,
    )
    assert cache_hit_pct(u) == 50.0


def test_cache_hit_pct_zero_when_no_tokens():
    from _messages_usage import cache_hit_pct

    assert cache_hit_pct(None) == 0.0
    assert cache_hit_pct(_fake_usage()) == 0.0


# ---------- log_messages_usage ---------------------------------------------


def test_log_messages_usage_emits_expected_format(caplog):
    """Verify the one-line log format matches the spec:
    '<caller> call (<model>): input=X, output=Y, cache_read=Z, cache_write=W, cost=$D.DDDD, cache_hit_pct=Q%'
    """
    from _messages_usage import log_messages_usage

    u = _fake_usage(
        input_tokens=100,
        output_tokens=50,
        cache_read_input_tokens=400,
        cache_creation_input_tokens=200,
    )

    with caplog.at_level(logging.INFO, logger="_messages_usage"):
        result = log_messages_usage("Self-heal", "claude-sonnet-4-6", u)

    # Returned dict has all six fields
    assert result["input"] == 100
    assert result["output"] == 50
    assert result["cache_read"] == 400
    assert result["cache_write"] == 200
    assert result["cost"] > 0
    assert result["cache_hit_pct"] > 0

    # Log line shape (single record, INFO level)
    assert any(
        "Self-heal call (claude-sonnet-4-6):" in rec.message
        and "input=100" in rec.message
        and "output=50" in rec.message
        and "cache_read=400" in rec.message
        and "cache_write=200" in rec.message
        and "cost=$" in rec.message
        and "cache_hit_pct=" in rec.message
        and "%" in rec.message
        for rec in caplog.records
    )


def test_log_messages_usage_ascii_only(caplog):
    """Railway log viewer should render the line cleanly — no Unicode."""
    from _messages_usage import log_messages_usage

    u = _fake_usage(input_tokens=10, output_tokens=10)
    with caplog.at_level(logging.INFO, logger="_messages_usage"):
        log_messages_usage("Self-improve", "claude-sonnet-4-6", u)

    msg = caplog.records[-1].message
    msg.encode("ascii")  # raises UnicodeEncodeError on non-ASCII


def test_log_messages_usage_zero_cache(caplog):
    """No cache fields → log line still emits, with cache_read=0 cache_write=0
    and cache_hit_pct=0.0%."""
    from _messages_usage import log_messages_usage

    u = _fake_usage(input_tokens=500, output_tokens=200)
    with caplog.at_level(logging.INFO, logger="_messages_usage"):
        result = log_messages_usage("Self-improve", "claude-sonnet-4-6", u)

    assert result["cache_read"] == 0
    assert result["cache_write"] == 0
    assert result["cache_hit_pct"] == 0.0


# ---------- Integration with self_heal._analyze_session --------------------


def test_self_heal_logs_usage_after_messages_call(caplog):
    """The integration point: _analyze_session must call log_messages_usage
    after client.messages.create returns. We mock the Anthropic client and
    verify the helper is called with the response.usage object."""
    import self_heal

    fake_usage_obj = _fake_usage(
        input_tokens=1200,
        output_tokens=400,
        cache_read_input_tokens=5000,
        cache_creation_input_tokens=1000,
    )
    fake_resp = _fake_response(
        usage=fake_usage_obj, text='{"learnings": [], "code_fixes": []}'
    )

    with patch.object(self_heal.client, "messages") as mock_msgs:
        mock_msgs.create = MagicMock(return_value=fake_resp)
        with caplog.at_level(logging.INFO, logger="_messages_usage"):
            self_heal._analyze_session(
                session_id="sess-test",
                session_type="ad-hoc",
                tool_errors=[{"tool": "soqlQuery", "error": "bad query"}],
                session_errors=[],
                tool_calls=[],
                agent_messages=[],
            )

    # The Messages API was invoked exactly once
    mock_msgs.create.assert_called_once()

    # And the log helper fired with the right caller label
    self_heal_log = [r for r in caplog.records if "Self-heal call" in r.message]
    assert len(self_heal_log) == 1
    line = self_heal_log[0].message
    assert "input=1200" in line
    assert "output=400" in line
    assert "cache_read=5000" in line
    assert "cache_write=1000" in line
    assert "cost=$" in line


def test_self_heal_logging_does_not_break_on_missing_usage():
    """If the SDK returns a response with usage=None (shouldn't happen, but
    belt-and-suspenders), _analyze_session must still finish and return a
    learnings dict — even though log_messages_usage will log zeros."""
    import self_heal

    fake_resp = _fake_response(usage=None, text='{"learnings": [], "code_fixes": []}')

    with patch.object(self_heal.client, "messages") as mock_msgs:
        mock_msgs.create = MagicMock(return_value=fake_resp)
        result = self_heal._analyze_session(
            session_id="sess-test",
            session_type="ad-hoc",
            tool_errors=[],
            session_errors=[{"type": "x", "message": "y"}],
            tool_calls=[],
            agent_messages=[],
        )

    # Returned the parsed JSON (or fallback) without raising
    assert "learnings" in result


# ---------- Integration with self_improve._analyze_changes -----------------


def test_self_improve_logs_usage_after_messages_call(caplog):
    import self_improve

    fake_usage_obj = _fake_usage(
        input_tokens=8000,
        output_tokens=2000,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    fake_resp = _fake_response(usage=fake_usage_obj, text="A summary of changes.")

    # _analyze_changes calls _fetch_page for each page; stub it so we don't
    # hit the network. Returning non-empty content ensures the function
    # reaches the messages.create call.
    with patch("self_improve._fetch_page", return_value="dummy doc content"):
        with patch.object(self_improve.client, "messages") as mock_msgs:
            mock_msgs.create = MagicMock(return_value=fake_resp)
            with caplog.at_level(logging.INFO, logger="_messages_usage"):
                summary = self_improve._analyze_changes(["overview"], [])

    mock_msgs.create.assert_called_once()
    assert summary == "A summary of changes."

    self_improve_log = [r for r in caplog.records if "Self-improve call" in r.message]
    assert len(self_improve_log) == 1
    line = self_improve_log[0].message
    assert "input=8000" in line
    assert "output=2000" in line
    assert "cache_read=0" in line
    assert "cache_write=0" in line
    assert "cost=$" in line


def test_self_improve_skips_log_when_no_content_to_analyze(caplog):
    """If no pages had fetchable content, _analyze_changes returns the early
    string and never calls messages.create — so no usage log either."""
    import self_improve

    with patch("self_improve._fetch_page", return_value=""):
        with patch.object(self_improve.client, "messages") as mock_msgs:
            mock_msgs.create = MagicMock()
            with caplog.at_level(logging.INFO, logger="_messages_usage"):
                summary = self_improve._analyze_changes(["overview"], [])

    mock_msgs.create.assert_not_called()
    assert not [r for r in caplog.records if "Self-improve call" in r.message]
    assert "could not be fetched" in summary


# ---------- Plan #35 Task #39: cost_collector.track_messages_call wiring ----
#
# These tests verify that every ``client.messages.create()`` call in
# ``self_heal._analyze_session`` and ``self_improve._analyze_changes`` is
# followed by a ``cost_collector.track_messages_call`` invocation that
# persists the call to the ``messages_api_calls`` ledger. The log line alone
# is not enough — Railway log retention is shallow and rollups need a DB row.


def test_self_heal_invokes_track_messages_call():
    """``_analyze_session`` must call cost_collector.track_messages_call
    exactly once with the response.usage object, the right caller label, and
    the model id (Plan #35, Task #39)."""
    import self_heal

    fake_usage_obj = _fake_usage(
        input_tokens=1500,
        output_tokens=400,
        cache_read_input_tokens=6000,
        cache_creation_input_tokens=500,
    )
    fake_resp = _fake_response(
        usage=fake_usage_obj, text='{"learnings": [], "code_fixes": []}'
    )

    with patch.object(self_heal.client, "messages") as mock_msgs:
        mock_msgs.create = MagicMock(return_value=fake_resp)
        with patch.object(self_heal.cost_collector, "track_messages_call") as mock_tc:
            self_heal._analyze_session(
                session_id="sess-track-test",
                session_type="ad-hoc",
                tool_errors=[{"tool": "soqlQuery", "error": "bad query"}],
                session_errors=[],
                tool_calls=[],
                agent_messages=[],
            )

    mock_tc.assert_called_once()
    _, kwargs = mock_tc.call_args
    assert kwargs["call_site"] == "self_heal._analyze_session"
    assert kwargs["model"] == self_heal._SELF_HEAL_MODEL
    assert kwargs["usage"] is fake_usage_obj


def test_self_improve_invokes_track_messages_call():
    """``_analyze_changes`` must call cost_collector.track_messages_call
    exactly once with the response.usage object, the right caller label, and
    the model id (Plan #35, Task #39)."""
    import self_improve

    fake_usage_obj = _fake_usage(
        input_tokens=9000,
        output_tokens=2500,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    fake_resp = _fake_response(usage=fake_usage_obj, text="Summary text.")

    with patch("self_improve._fetch_page", return_value="dummy doc content"):
        with patch.object(self_improve.client, "messages") as mock_msgs:
            mock_msgs.create = MagicMock(return_value=fake_resp)
            with patch.object(
                self_improve.cost_collector, "track_messages_call"
            ) as mock_tc:
                self_improve._analyze_changes(["overview"], [])

    mock_tc.assert_called_once()
    _, kwargs = mock_tc.call_args
    assert kwargs["call_site"] == "self_improve._analyze_changes"
    assert kwargs["model"] == self_improve._SELF_IMPROVE_MODEL
    assert kwargs["usage"] is fake_usage_obj


def test_self_heal_track_call_failure_is_non_fatal():
    """A DB / track_messages_call exception must not break _analyze_session.
    Cost tracking is observability, never load-bearing."""
    import self_heal

    fake_resp = _fake_response(
        usage=_fake_usage(input_tokens=10, output_tokens=10),
        text='{"learnings": [], "code_fixes": []}',
    )

    with patch.object(self_heal.client, "messages") as mock_msgs:
        mock_msgs.create = MagicMock(return_value=fake_resp)
        with patch.object(
            self_heal.cost_collector,
            "track_messages_call",
            side_effect=RuntimeError("db down"),
        ):
            result = self_heal._analyze_session(
                session_id="sess-fail",
                session_type="ad-hoc",
                tool_errors=[{"tool": "soqlQuery", "error": "x"}],
                session_errors=[],
                tool_calls=[],
                agent_messages=[],
            )

    # Function still returns the parsed analysis dict
    assert isinstance(result, dict)
    assert "learnings" in result


def test_self_improve_track_call_failure_is_non_fatal():
    """A DB / track_messages_call exception must not break _analyze_changes."""
    import self_improve

    fake_resp = _fake_response(
        usage=_fake_usage(input_tokens=10, output_tokens=10),
        text="Summary text.",
    )

    with patch("self_improve._fetch_page", return_value="dummy doc content"):
        with patch.object(self_improve.client, "messages") as mock_msgs:
            mock_msgs.create = MagicMock(return_value=fake_resp)
            with patch.object(
                self_improve.cost_collector,
                "track_messages_call",
                side_effect=RuntimeError("db down"),
            ):
                summary = self_improve._analyze_changes(["overview"], [])

    assert summary == "Summary text."


def test_self_improve_skips_track_call_when_no_content():
    """When no pages have fetchable content, _analyze_changes never reaches
    messages.create — so track_messages_call must not fire either."""
    import self_improve

    with patch("self_improve._fetch_page", return_value=""):
        with patch.object(self_improve.client, "messages") as mock_msgs:
            mock_msgs.create = MagicMock()
            with patch.object(
                self_improve.cost_collector, "track_messages_call"
            ) as mock_tc:
                self_improve._analyze_changes(["overview"], [])

    mock_msgs.create.assert_not_called()
    mock_tc.assert_not_called()
