"""Unit tests for session_evaluate.

Mocks ``client.beta.sessions.retrieve`` and ``client.beta.sessions.events.list``
with synthetic payloads that exercise each verdict path. Also pins the cost
estimate against a manual calculation and confirms the heuristic constants
stay in lock-step with ``session_runner.MODEL_COSTS_PER_MTOK``.

Run:
    cd orchestrator && python3 -m pytest session_evaluate_test.py -v
    # or from repo root:
    python3 -m pytest orchestrator/session_evaluate_test.py -v
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import List
from unittest.mock import MagicMock, patch

import pytest

# Required env vars for config imports anywhere downstream. setdefault means a
# real .env (loaded by conftest.py) wins. Matches the pattern in
# self_heal_compresr_poc_test.py.
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

from session_evaluate import (  # noqa: E402
    CONTEXT_BLOAT_INPUT_THRESHOLD,
    MCP_PAYLOAD_BYTES_THRESHOLD,
    MODEL_COSTS_PER_MTOK,
    _estimate_cost,
    _extract_model,
    _format_report,
    evaluate_session,
    main,
)


# -----------------------------------------------------------------------------
# Fixture builders
# -----------------------------------------------------------------------------


def _usage(
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read: int = 0,
    cache_5m: int = 0,
    cache_1h: int = 0,
) -> SimpleNamespace:
    """Build a SimpleNamespace shaped like session.usage."""
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation=SimpleNamespace(
            ephemeral_5m_input_tokens=cache_5m,
            ephemeral_1h_input_tokens=cache_1h,
        ),
    )


def _session(
    *,
    session_id: str = "sesn_EXAMPLE",
    model: str = "claude-opus-4-8",
    usage: SimpleNamespace = None,
    archived: bool = False,
    age_minutes: float = 5.0,
    model_shape: str = "agent_config",
) -> SimpleNamespace:
    """Build a SimpleNamespace shaped like a Managed Agents session.

    ``model_shape`` controls how the model is surfaced — the API has shipped
    three variants over time:
      ``flat_string``    — session.model is a plain string (historical shape).
      ``agent_string``   — session.agent.model is a plain string.
      ``agent_config``   — session.agent.model is an object with ``.id``
                           (the production shape as of 2026-05-11).
    """
    created = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)
    updated = created + timedelta(minutes=age_minutes)

    if model_shape == "flat_string":
        agent = SimpleNamespace(id="agent_x", model=None)
        flat = model
    elif model_shape == "agent_string":
        agent = SimpleNamespace(id="agent_x", model=model)
        flat = None
    else:  # agent_config (production shape)
        agent = SimpleNamespace(
            id="agent_x",
            model=SimpleNamespace(id=model, speed="standard"),
        )
        flat = None

    return SimpleNamespace(
        id=session_id,
        model=flat,
        agent=agent,
        usage=usage if usage is not None else _usage(),
        created_at=created,
        updated_at=updated,
        archived_at=updated if archived else None,
    )


def _event(etype: str, **kwargs) -> SimpleNamespace:
    """Build a SimpleNamespace shaped like a session event."""
    ns = SimpleNamespace(type=etype)
    for k, v in kwargs.items():
        setattr(ns, k, v)
    return ns


def _content_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(text=text)


def _patched_client(session_obj, events: List[SimpleNamespace]) -> MagicMock:
    """Build a MagicMock client whose beta.sessions methods return the inputs."""
    client = MagicMock()
    client.beta.sessions.retrieve.return_value = session_obj
    client.beta.sessions.events.list.return_value = SimpleNamespace(data=events)
    return client


# -----------------------------------------------------------------------------
# Verdict paths
# -----------------------------------------------------------------------------


def test_healthy_session_returns_only_healthy_verdict():
    """Small session, no probes, no large MCP → ``healthy``."""
    session = _session(usage=_usage(input_tokens=1000, output_tokens=200))
    events = [
        _event(
            "agent.custom_tool_use",
            name="send_slack_notification",
            input={"severity": "info", "summary": "hello"},
        ),
        _event(
            "agent.mcp_tool_result",
            content=[_content_block('{ "records": [] }')],
        ),
    ]
    client = _patched_client(session, events)
    report = evaluate_session("sesn_EXAMPLE_healthy", client=client)

    assert report["verdicts"] == ["healthy"]
    assert report["slack_posts"] == 1
    assert report["sub_agent_dispatches"] == 0
    assert report["mcp_payload_bytes"] > 0  # one block was counted
    assert report["model"] == "claude-opus-4-8"


def test_context_bloat_risk_fires_on_large_input_side():
    """total_input_side > 750k → ``context-bloat-risk``."""
    usage = _usage(
        input_tokens=800_000,  # crosses the 750k boundary on its own
        output_tokens=10_000,
        cache_read=50_000,
    )
    session = _session(usage=usage)
    client = _patched_client(session, [])
    report = evaluate_session("sesn_EXAMPLE", client=client)

    assert "context-bloat-risk" in report["verdicts"]
    assert report["usage"]["total_input_side"] == 850_000


def test_context_bloat_risk_fires_on_large_mcp_payload():
    """mcp_payload_bytes > 300k → ``context-bloat-risk`` (no input bloat needed)."""
    session = _session(usage=_usage(input_tokens=1000))
    # 4 blocks of 100k chars each = 400k bytes total (one block > 300k too).
    big_block = "x" * 100_000
    events = [
        _event("agent.mcp_tool_result", content=[_content_block(big_block)]),
        _event("agent.mcp_tool_result", content=[_content_block(big_block)]),
        _event("agent.mcp_tool_result", content=[_content_block(big_block)]),
        _event("agent.mcp_tool_result", content=[_content_block(big_block)]),
    ]
    client = _patched_client(session, events)
    report = evaluate_session("sesn_EXAMPLE_bloat", client=client)

    assert "context-bloat-risk" in report["verdicts"]
    assert report["mcp_payload_bytes"] == 400_000
    assert report["largest_mcp_response_bytes"] == 100_000


def test_mcp_probe_hallucination_fires_on_filesystem_probes():
    """Bash probes for sfdx / SF env / /var/run/ → ``mcp-probe-hallucination``."""
    session = _session(usage=_usage(input_tokens=500))
    events = [
        _event(
            "agent.tool_use",
            name="bash",
            input={"command": "which sfdx"},
        ),
        _event(
            "agent.tool_use",
            name="bash",
            input={"command": 'find / -name "*.sf" 2>/dev/null | head'},
        ),
        _event(
            "agent.tool_use",
            name="bash",
            input={"command": "ls /var/run/"},
        ),
        _event(
            "agent.tool_use",
            name="bash",
            input={"command": "env | grep -i SF"},
        ),
    ]
    client = _patched_client(session, events)
    report = evaluate_session("sesn_EXAMPLE", client=client)

    assert "mcp-probe-hallucination" in report["verdicts"]


def test_streaming_pattern_violated_fires_without_streaming_evidence():
    """Large MCP payload + no openpyxl write + no nextRecordsUrl → violation."""
    session = _session(usage=_usage(input_tokens=1000))
    big_block = "y" * 320_000  # one shot crosses the 300k threshold
    events = [
        _event("agent.mcp_tool_result", content=[_content_block(big_block)]),
        # A non-streaming bash command — must NOT count as evidence.
        _event(
            "agent.tool_use",
            name="bash",
            input={"command": "echo hello"},
        ),
    ]
    client = _patched_client(session, events)
    report = evaluate_session("sesn_EXAMPLE_violation", client=client)

    assert "streaming-pattern-violated" in report["verdicts"]
    # Bloat risk also fires because 320k > 300k MCP threshold.
    assert "context-bloat-risk" in report["verdicts"]


def test_streaming_pattern_ok_when_openpyxl_or_nextrecordsurl_seen():
    """Large MCP payload with openpyxl write OR nextRecordsUrl → NOT violated."""
    session = _session(usage=_usage(input_tokens=1000))
    big_block = "z" * 320_000
    events = [
        _event("agent.mcp_tool_result", content=[_content_block(big_block)]),
        _event(
            "agent.tool_use",
            name="bash",
            input={
                "command": (
                    "python3 -c 'import openpyxl; "
                    'openpyxl.Workbook().save("/tmp/out.xlsx")\''
                )
            },
        ),
        _event(
            "agent.tool_use",
            name="bash",
            input={"command": "echo nextRecordsUrl seen"},
        ),
    ]
    client = _patched_client(session, events)
    report = evaluate_session("sesn_EXAMPLE_ok", client=client)

    assert "streaming-pattern-violated" not in report["verdicts"]
    # Bloat risk still fires (separate heuristic).
    assert "context-bloat-risk" in report["verdicts"]


def test_sub_agent_dispatch_broken_fires_on_failure_phrase():
    """Sub-agent dispatch followed by 'lacks Salesforce MCP' → broken.

    Uses the production event name ``agent.thread_message_sent`` — what the
    Managed Agents API actually emits when the Coordinator hands work to a
    specialist (verified against sesn_EXAMPLE on 2026-05-11).
    """
    session = _session(usage=_usage(input_tokens=1000))
    events = [
        _event(
            "agent.thread_message_sent",
            to_agent_name="Pipeline Monitor",
        ),
        _event(
            "agent.message",
            content=[
                _content_block("The subagent lacks Salesforce MCP access; rerouting.")
            ],
        ),
    ]
    client = _patched_client(session, events)
    report = evaluate_session("sesn_EXAMPLE_broken", client=client)

    assert "sub-agent-dispatch-broken" in report["verdicts"]
    assert report["sub_agent_dispatches"] == 1


def test_sub_agent_dispatch_via_legacy_event_name():
    """``agent.sub_agent_use`` is honored as a forward-compat alias."""
    session = _session(usage=_usage(input_tokens=1000))
    events = [
        _event("agent.sub_agent_use", name="pipeline_monitor"),
        _event(
            "agent.message",
            content=[_content_block("no sf mcp access")],
        ),
    ]
    client = _patched_client(session, events)
    report = evaluate_session("sesn_EXAMPLE_dispatch", client=client)

    assert "sub-agent-dispatch-broken" in report["verdicts"]
    assert report["sub_agent_dispatches"] == 1


def test_sub_agent_dispatch_ok_without_failure_phrase():
    """Sub-agent dispatch with normal follow-up text → NOT broken."""
    session = _session(usage=_usage(input_tokens=1000))
    events = [
        _event("agent.thread_message_sent", to_agent_name="Pipeline Monitor"),
        _event(
            "agent.message",
            content=[_content_block("Pipeline summary: 12 deals at risk.")],
        ),
    ]
    client = _patched_client(session, events)
    report = evaluate_session("sesn_EXAMPLE_ok", client=client)

    assert "sub-agent-dispatch-broken" not in report["verdicts"]
    assert "healthy" in report["verdicts"]


def test_multiple_verdicts_fire_simultaneously():
    """A pathological session can hit several verdicts at once."""
    usage = _usage(input_tokens=800_000, output_tokens=10_000)
    session = _session(usage=usage)
    big_block = "q" * 320_000
    events = [
        _event("agent.mcp_tool_result", content=[_content_block(big_block)]),
        _event(
            "agent.tool_use",
            name="bash",
            input={"command": "which sfdx"},
        ),
        _event("agent.thread_message_sent", to_agent_name="x"),
        _event(
            "agent.message",
            content=[_content_block("no sf mcp; aborting")],
        ),
    ]
    client = _patched_client(session, events)
    report = evaluate_session("sesn_EXAMPLE_bad", client=client)

    assert set(report["verdicts"]) >= {
        "context-bloat-risk",
        "mcp-probe-hallucination",
        "streaming-pattern-violated",
        "sub-agent-dispatch-broken",
    }
    assert "healthy" not in report["verdicts"]


@pytest.mark.parametrize(
    "shape",
    ["flat_string", "agent_string", "agent_config"],
)
def test_extract_model_handles_all_shapes(shape):
    """_extract_model normalizes every observed session shape to a string."""
    session = _session(model="claude-opus-4-8", model_shape=shape)
    assert _extract_model(session) == "claude-opus-4-8"


def test_extract_model_returns_none_when_missing():
    """No model anywhere → None (cost resolver tolerates this)."""
    session = SimpleNamespace(agent=None, model=None)
    assert _extract_model(session) is None


# -----------------------------------------------------------------------------
# Cost estimate
# -----------------------------------------------------------------------------


def test_cost_estimate_matches_manual_opus_calc_within_one_cent():
    """Manual Opus 4.8 calc vs _estimate_cost — must match within $0.01.

    Inputs:
        input_tokens     = 100,000        × $ 5.00 / MTok  = $0.50
        output_tokens    =  20,000        × $25.00 / MTok  = $0.50
        cache_read       =  50,000        × $ 0.50 / MTok  = $0.025
        cache_write_5m   =  10,000        × $ 6.25 / MTok  = $0.0625
        cache_write_1h   =   5,000        × $10.00 / MTok  = $0.05
        ----------------------------------------------------------
        total                                                $1.1375
    """
    usage = _usage(
        input_tokens=100_000,
        output_tokens=20_000,
        cache_read=50_000,
        cache_5m=10_000,
        cache_1h=5_000,
    )
    cost = _estimate_cost(usage, "claude-opus-4-8")
    assert cost == pytest.approx(1.1375, abs=0.01)


def test_cost_estimate_matches_manual_sonnet_calc_within_one_cent():
    """Manual Sonnet 4.6 calc vs _estimate_cost.

    100k × 3 + 20k × 15 + 50k × 0.3 + 10k × 3.75 + 5k × 6 = 0.30 + 0.30 +
    0.015 + 0.0375 + 0.03 = $0.6825.
    """
    usage = _usage(
        input_tokens=100_000,
        output_tokens=20_000,
        cache_read=50_000,
        cache_5m=10_000,
        cache_1h=5_000,
    )
    cost = _estimate_cost(usage, "claude-sonnet-4-6")
    assert cost == pytest.approx(0.6825, abs=0.01)


def test_cost_estimate_inside_evaluate_session_report():
    """The end-to-end report's estimated_cost_usd matches the manual Opus calc."""
    usage = _usage(
        input_tokens=100_000,
        output_tokens=20_000,
        cache_read=50_000,
        cache_5m=10_000,
        cache_1h=5_000,
    )
    session = _session(model="claude-opus-4-8", usage=usage)
    client = _patched_client(session, [])
    report = evaluate_session("sesn_EXAMPLE", client=client)

    assert report["estimated_cost_usd"] == pytest.approx(1.1375, abs=0.01)


def test_cost_resolver_handles_dated_model_id():
    """Dated model suffixes resolve via longest-prefix (Opus, not Sonnet default)."""
    usage = _usage(input_tokens=1_000_000)
    cost = _estimate_cost(usage, "claude-opus-4-8-20260101")
    # 1M × $5 / 1M = $5.00 exactly.
    assert cost == pytest.approx(5.00, abs=0.01)


# -----------------------------------------------------------------------------
# Schema + shape
# -----------------------------------------------------------------------------


def test_report_includes_all_required_keys():
    """The report shape is the contract — keys must not drift silently."""
    session = _session(usage=_usage(input_tokens=10))
    client = _patched_client(session, [])
    report = evaluate_session("sesn_EXAMPLE", client=client)

    expected = {
        "session_id",
        "created_at",
        "updated_at",
        "age_minutes",
        "archived",
        "model",
        "usage",
        "estimated_cost_usd",
        "event_summary",
        "tool_calls",
        "mcp_payload_bytes",
        "largest_mcp_response_bytes",
        "sub_agent_dispatches",
        "slack_posts",
        "verdicts",
    }
    assert expected <= set(report.keys())
    assert set(report["usage"].keys()) == {
        "cache_creation_5m",
        "cache_creation_1h",
        "cache_read",
        "input",
        "output",
        "total_input_side",
    }


def test_tool_calls_classification_and_preview():
    """tool_calls entries get the right kind and a truncated input_preview."""
    session = _session(usage=_usage(input_tokens=10))
    long_input = "SELECT Id FROM Opportunity WHERE " + "X" * 500
    events = [
        _event(
            "agent.mcp_tool_use",
            name="soqlQuery",
            input={"q": long_input},
        ),
        _event(
            "agent.custom_tool_use",
            name="post_report",
            input={"summary": "hi"},
        ),
        _event(
            "agent.tool_use",
            name="bash",
            input={"command": "ls"},
        ),
    ]
    client = _patched_client(session, events)
    report = evaluate_session("sesn_EXAMPLE", client=client)

    kinds = {tc["name"]: tc["kind"] for tc in report["tool_calls"]}
    assert kinds["soqlQuery"] == "mcp"
    assert kinds["post_report"] == "custom"
    assert kinds["bash"] == "builtin"
    soql_preview = next(
        tc["input_preview"] for tc in report["tool_calls"] if tc["name"] == "soqlQuery"
    )
    assert len(soql_preview) <= 200


def test_event_summary_counts_match():
    """event_summary is a faithful count of every event type encountered."""
    session = _session(usage=_usage(input_tokens=10))
    events = [
        _event("agent.message", content=[_content_block("hi")]),
        _event("agent.message", content=[_content_block("again")]),
        _event(
            "agent.mcp_tool_use",
            name="soqlQuery",
            input={"q": "SELECT Id FROM Account LIMIT 1"},
        ),
        _event("session.status_idle"),
    ]
    client = _patched_client(session, events)
    report = evaluate_session("sesn_EXAMPLE", client=client)

    assert report["event_summary"]["agent.message"] == 2
    assert report["event_summary"]["agent.mcp_tool_use"] == 1
    assert report["event_summary"]["session.status_idle"] == 1


def test_events_limit_caps_walk():
    """``events_limit`` truncates the list before any work happens."""
    session = _session(usage=_usage(input_tokens=10))
    events = [_event("agent.message", content=[_content_block("hi")])] * 100
    client = _patched_client(session, events)
    report = evaluate_session("sesn_EXAMPLE", client=client, events_limit=5)

    assert sum(report["event_summary"].values()) == 5


def test_format_report_is_non_empty_string():
    """The pretty-print path produces a non-empty string with key labels."""
    session = _session(usage=_usage(input_tokens=10))
    client = _patched_client(session, [])
    report = evaluate_session("sesn_EXAMPLE", client=client)
    out = _format_report(report)

    assert "Session:" in out
    assert "Usage (tokens):" in out
    assert "Verdicts:" in out


# -----------------------------------------------------------------------------
# CLI surface
# -----------------------------------------------------------------------------


def test_cli_json_mode_dumps_full_report_to_stdout(capsys):
    """``--json`` writes the entire report to stdout as parseable JSON."""
    session = _session(usage=_usage(input_tokens=10))
    client = _patched_client(session, [])

    with patch("session_evaluate._client", return_value=client):
        rc = main(["sesn_EXAMPLE_json", "--json"])

    assert rc == 0
    out = capsys.readouterr().out
    import json as _json

    parsed = _json.loads(out)
    assert parsed["session_id"] == "sesn_EXAMPLE_json"
    assert "verdicts" in parsed


def test_cli_pretty_mode_writes_to_stdout(capsys):
    """Without ``--json``, output is the pretty-printed report."""
    session = _session(usage=_usage(input_tokens=10))
    client = _patched_client(session, [])

    with patch("session_evaluate._client", return_value=client):
        rc = main(["sesn_EXAMPLE_pretty"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "Session:   sesn_EXAMPLE_pretty" in out
    assert "Verdicts:" in out


def test_cli_exit_code_on_api_error(capsys):
    """An anthropic.APIError surfaces as exit code 2 with stderr message."""
    import anthropic

    fake = MagicMock()
    fake.beta.sessions.retrieve.side_effect = anthropic.APIError(
        message="boom", request=MagicMock(), body=None
    )

    with patch("session_evaluate._client", return_value=fake):
        rc = main(["sesn_EXAMPLE"])

    assert rc == 2
    err = capsys.readouterr().err
    assert "Anthropic API error" in err


# -----------------------------------------------------------------------------
# Rate-table integrity (catches drift between this CLI and session_runner)
# -----------------------------------------------------------------------------


def test_rate_table_matches_session_runner():
    """If session_runner's rates change, this CLI's inline copy MUST change too.

    Tests both rate-table contents and the heuristic thresholds — if these
    drift, downstream cost reports stop matching the ground-truth ledger.
    """
    pytest.importorskip("anthropic")  # session_runner depends on anthropic
    try:
        import session_runner  # noqa: F401
    except Exception:
        pytest.skip("session_runner not importable (heavy dep chain); rate pin skipped")
    else:
        assert MODEL_COSTS_PER_MTOK == session_runner.MODEL_COSTS_PER_MTOK


def test_thresholds_are_documented_constants():
    """The verdict thresholds are exposed at module level for test/inspection."""
    assert CONTEXT_BLOAT_INPUT_THRESHOLD == 750_000
    assert MCP_PAYLOAD_BYTES_THRESHOLD == 300_000
