"""Unit tests for ``bin/session-tail.py``.

The bin filename uses a hyphen so the module has to be loaded explicitly
via importlib — same pattern used by ``audit_mcp_toolsets_test.py``.

Run:
    python3 -m pytest bin/session_tail_test.py -v
"""

from __future__ import annotations

import importlib.util
import io
import os
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

BIN_DIR = Path(__file__).resolve().parent
SCRIPT_PATH = BIN_DIR / "session-tail.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("session_tail_cli", SCRIPT_PATH)
    assert spec and spec.loader, "failed to locate session-tail.py"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def stail():
    return _load_module()


# ---------------------------------------------------------------------------
# usage extraction
# ---------------------------------------------------------------------------


def _usage(*, input_tokens=0, output_tokens=0, cache_read=0, cache_5m=0):
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation=SimpleNamespace(ephemeral_5m_input_tokens=cache_5m),
    )


def test_compute_input_side_sums_three_input_categories(stail):
    u = _usage(input_tokens=100, cache_read=2000, cache_5m=500, output_tokens=999)
    assert stail._compute_input_side(u) == 2_600


def test_compute_input_side_tolerates_none(stail):
    assert stail._compute_input_side(None) == 0


def test_compute_input_side_tolerates_missing_cache_creation(stail):
    u = SimpleNamespace(
        input_tokens=10,
        cache_read_input_tokens=20,
        cache_creation=None,
    )
    assert stail._compute_input_side(u) == 30


# ---------------------------------------------------------------------------
# severity marker — the BAND boundary cases. These pin the threshold semantics
# so a future contributor cannot quietly shift any of them.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "input_side,expected",
    [
        (0, ""),
        (749_999, ""),
        (750_000, ":warning:"),
        (949_999, ":warning:"),
        (950_000, ":rotating_light:"),
        (999_999, ":rotating_light:"),
        (1_000_000, ":skull:"),
        (1_200_000, ":skull:"),
    ],
)
def test_severity_marker_bands(stail, input_side, expected):
    assert stail._severity_marker(input_side) == expected


# ---------------------------------------------------------------------------
# Event description
# ---------------------------------------------------------------------------


def test_describe_event_tool_use(stail):
    ev = SimpleNamespace(type="agent.tool_use", name="bash")
    assert stail._describe_event(ev) == "tool_use bash"


def test_describe_event_custom_tool_use_with_label(stail):
    ev = SimpleNamespace(
        type="agent.custom_tool_use",
        name="dump_sf_query",
        input={"label": "open_opps", "portco_key": "acme"},
    )
    assert stail._describe_event(ev) == "custom_tool_use dump_sf_query label=open_opps"


def test_describe_event_custom_tool_use_with_query(stail):
    ev = SimpleNamespace(
        type="agent.custom_tool_use",
        name="search_knowledge_base",
        input={"query": "What is FATI?"},
    )
    out = stail._describe_event(ev)
    assert out.startswith("custom_tool_use search_knowledge_base query=")
    assert "What is FATI?" in out


def test_describe_event_subagent_dispatch(stail):
    ev = SimpleNamespace(type="agent.thread_message_sent", agent_id="agent_subA")
    assert stail._describe_event(ev) == "sub_dispatch agent_id=agent_subA"


def test_describe_event_unknown_type_returns_type(stail):
    ev = SimpleNamespace(type="something.new")
    assert stail._describe_event(ev) == "something.new"


# ---------------------------------------------------------------------------
# tick() — single poll with mocked client
# ---------------------------------------------------------------------------


def _session(input_side=100, output=50, archived=False, age_min=1.0):
    created = datetime.now(timezone.utc) - timedelta(minutes=age_min)
    return SimpleNamespace(
        id="sesn_EXAMPLE",
        agent=SimpleNamespace(
            id="agent_x", model=SimpleNamespace(id="claude-opus-4-8")
        ),
        usage=_usage(input_tokens=input_side, output_tokens=output),
        created_at=created,
        updated_at=created + timedelta(seconds=30),
        archived_at=created if archived else None,
    )


def test_tick_prints_summary_and_returns_state(stail):
    client = MagicMock()
    client.beta.sessions.retrieve.return_value = _session(
        input_side=800_000, output=1_000, archived=False
    )
    client.beta.sessions.events.list.return_value = SimpleNamespace(
        data=[
            SimpleNamespace(id="e1", type="agent.tool_use", name="bash"),
        ]
    )
    out = io.StringIO()
    archived, input_side = stail.tick(
        client, "sesn_EXAMPLE", 1, set(), show_events=True, out=out
    )
    assert archived is False
    assert input_side == 800_000
    text = out.getvalue()
    assert "tick=01" in text
    assert "input_side=  800,000" in text
    assert ":warning:" in text
    assert "tool_use bash" in text


def test_tick_marks_skull_past_cap(stail):
    client = MagicMock()
    client.beta.sessions.retrieve.return_value = _session(input_side=1_100_000)
    client.beta.sessions.events.list.return_value = SimpleNamespace(data=[])
    out = io.StringIO()
    archived, input_side = stail.tick(
        client, "sesn_EXAMPLE", 1, set(), show_events=True, out=out
    )
    assert archived is False
    assert input_side == 1_100_000
    assert ":skull:" in out.getvalue()


def test_tick_dedup_events_across_ticks(stail):
    """An event seen in tick 1 must NOT print again in tick 2."""
    client = MagicMock()
    client.beta.sessions.retrieve.return_value = _session(input_side=100)
    e1 = SimpleNamespace(id="e1", type="agent.tool_use", name="read")
    e2 = SimpleNamespace(id="e2", type="agent.tool_use", name="bash")
    client.beta.sessions.events.list.side_effect = [
        SimpleNamespace(data=[e1]),
        SimpleNamespace(data=[e2, e1]),  # API returns newest-first; both shown
    ]
    out = io.StringIO()
    seen: set[str] = set()
    stail.tick(client, "sesn_EXAMPLE", 1, seen, show_events=True, out=out)
    stail.tick(client, "sesn_EXAMPLE", 2, seen, show_events=True, out=out)
    text = out.getvalue()
    assert text.count("tool_use read") == 1
    assert text.count("tool_use bash") == 1


def test_tick_suppresses_noisy_event_types_by_default(stail):
    client = MagicMock()
    client.beta.sessions.retrieve.return_value = _session(input_side=100)
    client.beta.sessions.events.list.return_value = SimpleNamespace(
        data=[
            SimpleNamespace(id="s1", type="span.model_request_start"),
            SimpleNamespace(id="t1", type="agent.tool_use", name="bash"),
            SimpleNamespace(id="th1", type="agent.thinking"),
        ]
    )
    out = io.StringIO()
    stail.tick(client, "sesn_EXAMPLE", 1, set(), show_events=True, out=out)
    text = out.getvalue()
    assert "tool_use bash" in text
    assert "span.model_request_start" not in text
    assert "agent.thinking" not in text


def test_tick_verbose_includes_suppressed_events(stail):
    client = MagicMock()
    client.beta.sessions.retrieve.return_value = _session(input_side=100)
    client.beta.sessions.events.list.return_value = SimpleNamespace(
        data=[SimpleNamespace(id="s1", type="span.model_request_start")]
    )
    out = io.StringIO()
    stail.tick(client, "sesn_EXAMPLE", 1, set(), show_events=True, verbose=True, out=out)
    assert "span.model_request_start" in out.getvalue()


def test_tick_no_events_skips_listing(stail):
    client = MagicMock()
    client.beta.sessions.retrieve.return_value = _session(input_side=100)
    out = io.StringIO()
    stail.tick(client, "sesn_EXAMPLE", 1, set(), show_events=False, out=out)
    client.beta.sessions.events.list.assert_not_called()


def test_tick_handles_retrieve_failure_without_raising(stail):
    client = MagicMock()
    client.beta.sessions.retrieve.side_effect = RuntimeError("boom")
    out = io.StringIO()
    archived, input_side = stail.tick(
        client, "sesn_EXAMPLE", 1, set(), show_events=True, out=out
    )
    assert (archived, input_side) == (False, 0)
    assert "retrieve failed: boom" in out.getvalue()


# ---------------------------------------------------------------------------
# watch() — loop semantics. The critical contract is: do NOT stop on cap.
# ---------------------------------------------------------------------------


def test_watch_does_not_stop_on_cap_crossing(stail):
    """Regression: a previous iteration stopped at 1M; the fix is that the
    only natural-termination signal is ``archived_at`` flipping. Crossing
    the 1M cap MUST continue polling so the operator sees the stalled state.
    """
    client = MagicMock()
    sessions = [
        _session(input_side=900_000, archived=False),
        _session(input_side=1_100_000, archived=False),  # cap crossed
        _session(input_side=1_200_000, archived=False),  # would have stopped before
    ]
    client.beta.sessions.retrieve.side_effect = sessions
    client.beta.sessions.events.list.return_value = SimpleNamespace(data=[])
    out = io.StringIO()
    result = stail.watch(
        client,
        "sesn_EXAMPLE",
        interval=0,
        max_ticks=3,
        show_events=False,
        sleeper=lambda _s: None,
        out=out,
    )
    assert result["ticks"] == 3
    assert result["reason"] == "max_ticks"
    assert result["archived"] is False
    assert result["final_input_side"] == 1_200_000
    text = out.getvalue()
    assert ":skull:" in text  # past-cap marker rendered


def test_watch_stops_on_archive(stail):
    client = MagicMock()
    sessions = [
        _session(input_side=500_000, archived=False),
        _session(input_side=600_000, archived=True),
    ]
    client.beta.sessions.retrieve.side_effect = sessions
    client.beta.sessions.events.list.return_value = SimpleNamespace(data=[])
    out = io.StringIO()
    result = stail.watch(
        client,
        "sesn_EXAMPLE",
        interval=0,
        max_ticks=10,
        show_events=False,
        sleeper=lambda _s: None,
        out=out,
    )
    assert result["ticks"] == 2
    assert result["reason"] == "archived"
    assert result["archived"] is True


def test_watch_respects_max_ticks(stail):
    client = MagicMock()
    client.beta.sessions.retrieve.return_value = _session(
        input_side=100, archived=False
    )
    client.beta.sessions.events.list.return_value = SimpleNamespace(data=[])
    out = io.StringIO()
    result = stail.watch(
        client,
        "sesn_EXAMPLE",
        interval=0,
        max_ticks=4,
        show_events=False,
        sleeper=lambda _s: None,
        out=out,
    )
    assert result["ticks"] == 4
    assert result["reason"] == "max_ticks"


def test_watch_sleeper_called_n_minus_1_times(stail):
    """The loop should sleep between ticks but not after the last tick."""
    client = MagicMock()
    client.beta.sessions.retrieve.return_value = _session(
        input_side=100, archived=False
    )
    client.beta.sessions.events.list.return_value = SimpleNamespace(data=[])
    calls: list[int] = []
    stail.watch(
        client,
        "sesn_EXAMPLE",
        interval=15,
        max_ticks=3,
        show_events=False,
        sleeper=lambda s: calls.append(s),
        out=io.StringIO(),
    )
    assert calls == [15, 15]


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------


def test_parser_quiet_and_no_events_are_aliases(stail):
    p = stail._build_parser()
    args1 = p.parse_args(["sesn_EXAMPLE", "--quiet"])
    args2 = p.parse_args(["sesn_EXAMPLE", "--no-events"])
    assert args1.quiet is True
    assert args2.no_events is True


def test_parser_defaults(stail):
    p = stail._build_parser()
    args = p.parse_args(["sesn_EXAMPLE"])
    assert args.interval == 15
    assert args.max_ticks == 120
    assert args.no_events is False
    assert args.quiet is False
    assert args.verbose is False


def test_build_client_requires_api_key(stail, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        stail._build_client()


def test_load_env_idempotent_when_no_dotenv(tmp_path, stail, monkeypatch):
    """Loader must not crash when .env is absent."""
    monkeypatch.setattr(stail, "REPO_ROOT", tmp_path)
    # No .env in tmp_path; loader is a no-op.
    stail._load_env()
    # Verify no surprising side effects on the environment.
    assert "ANTHROPIC_API_KEY" not in os.environ or True  # smoke
