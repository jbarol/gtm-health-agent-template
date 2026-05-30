"""Tests for ``bin/trace-outbound-hosts.py`` (Plan #44 Task #11).

The script's filename has a hyphen, so we load it by path via ``importlib.util``.
Tests stub the Anthropic SDK + DB layer; pure parse/diff logic is exercised
against fixture events.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "bin" / "trace-outbound-hosts.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("trace_outbound_hosts", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod():
    return _load_module()


def _make_tool_use(input_dict):
    return SimpleNamespace(type="agent.custom_tool_use", input=input_dict)


def _make_tool_result(text):
    return SimpleNamespace(
        type="user.custom_tool_result",
        content=[SimpleNamespace(text=text)],
    )


def _make_session_error(msg):
    return SimpleNamespace(
        type="session.error", error=SimpleNamespace(message=msg, type="blocked_host")
    )


# ---------------------------------------------------------------------------
# Wildcard matching
# ---------------------------------------------------------------------------


def test_wildcard_pattern_matches_subdomain(mod):
    assert mod._matches_pattern("api.slack.com", "*.slack.com")
    assert mod._matches_pattern("hooks.slack.com", "*.slack.com")


def test_wildcard_does_not_match_bare_domain(mod):
    # *.slack.com requires at least one label before .slack.com.
    assert not mod._matches_pattern("slack.com", "*.slack.com")


def test_literal_pattern_requires_exact_match(mod):
    assert mod._matches_pattern("api.anthropic.com", "api.anthropic.com")
    assert not mod._matches_pattern("evil.anthropic.com", "api.anthropic.com")


def test_host_is_allowed_iterates_allowlist(mod):
    allow = ["api.anthropic.com", "*.salesforce.com"]
    assert mod.host_is_allowed("api.anthropic.com", allow)
    assert mod.host_is_allowed("your-org.my.salesforce.com", allow)
    assert not mod.host_is_allowed("evil.example.com", allow)


# ---------------------------------------------------------------------------
# URL extraction
# ---------------------------------------------------------------------------


def test_extract_hosts_from_text_picks_up_https_urls(mod):
    text = (
        "Tried https://api.anthropic.com/v1/messages then https://quickchart.io/chart"
    )
    hosts = mod.extract_hosts_from_text(text)
    assert hosts == {"api.anthropic.com", "quickchart.io"}


def test_extract_hosts_from_text_handles_empty_input(mod):
    assert mod.extract_hosts_from_text("") == set()
    assert mod.extract_hosts_from_text(None) == set()


def test_extract_hosts_from_event_walks_tool_use_input(mod):
    ev = _make_tool_use({"url": "https://your-org.my.salesforce.com/services/data"})
    assert mod.extract_hosts_from_event(ev) == {"your-org.my.salesforce.com"}


def test_extract_hosts_from_event_walks_tool_result_text(mod):
    ev = _make_tool_result(
        'Response: {"records": [{"Id": "001"}], "next": "https://files.api.anthropic.com/x"}'
    )
    assert mod.extract_hosts_from_event(ev) == {"files.api.anthropic.com"}


def test_extract_hosts_from_event_walks_session_error_message(mod):
    ev = _make_session_error("blocked: evil.example.com is not in allowed_hosts")
    hosts = mod.extract_hosts_from_event(ev)
    assert "evil.example.com" in hosts


def test_extract_hosts_from_event_unknown_type_is_empty(mod):
    ev = SimpleNamespace(type="agent.message", content=[])
    assert mod.extract_hosts_from_event(ev) == set()


# ---------------------------------------------------------------------------
# Diff logic
# ---------------------------------------------------------------------------


def test_diff_buckets_clean(mod):
    """All observed hosts are on the allowlist → no warnings, exit 0 path."""
    observed = {"api.anthropic.com", "api.slack.com", "your-org.my.salesforce.com"}
    allow = [
        "api.anthropic.com",
        "*.slack.com",
        "*.my.salesforce.com",
        "quickchart.io",  # unobserved → droppable
    ]
    ok, blocked, droppable = mod.diff_against_allowlist(observed, allow)

    assert ok == ["api.anthropic.com", "api.slack.com", "your-org.my.salesforce.com"]
    assert blocked == []
    assert droppable == ["quickchart.io"]


def test_diff_buckets_finds_blocked_host(mod):
    """A host outside the allowlist surfaces in the blocked bucket."""
    observed = {"api.anthropic.com", "evil.example.com"}
    allow = ["api.anthropic.com"]
    ok, blocked, droppable = mod.diff_against_allowlist(observed, allow)

    assert ok == ["api.anthropic.com"]
    assert blocked == ["evil.example.com"]
    assert droppable == []


def test_diff_buckets_wildcard_counts_as_observed_when_literal_matches(mod):
    """A wildcard pattern is NOT 'droppable' if any observed literal matches it."""
    observed = {"hooks.slack.com"}
    allow = ["*.slack.com"]
    ok, blocked, droppable = mod.diff_against_allowlist(observed, allow)

    assert ok == ["hooks.slack.com"]
    assert blocked == []
    assert droppable == []  # wildcard was satisfied


# ---------------------------------------------------------------------------
# End-to-end trace_session_hosts
# ---------------------------------------------------------------------------


def test_trace_session_hosts_aggregates_across_sessions(mod):
    """``trace_session_hosts`` walks every event in every session and unions
    the result."""
    events_a = SimpleNamespace(
        data=[
            _make_tool_use({"url": "https://api.anthropic.com/v1/messages"}),
            _make_tool_result('{"next": "https://quickchart.io/c?data=..."}'),
        ]
    )
    events_b = SimpleNamespace(
        data=[
            _make_tool_use({"url": "https://api.compresr.com/v1/compress"}),
        ]
    )

    client = MagicMock()
    client.beta.sessions.events.list.side_effect = [events_a, events_b]

    observed = mod.trace_session_hosts(["sesn_EXAMPLE", "sesn_EXAMPLE"], client)

    assert observed == {"api.anthropic.com", "quickchart.io", "api.compresr.com"}
    assert client.beta.sessions.events.list.call_count == 2


def test_trace_session_hosts_skips_failed_session(mod, capsys):
    """A failed ``events.list`` call doesn't kill the run — it logs + continues."""
    client = MagicMock()
    client.beta.sessions.events.list.side_effect = [
        RuntimeError("404 session expired"),
        SimpleNamespace(data=[_make_tool_use({"url": "https://api.anthropic.com/x"})]),
    ]

    observed = mod.trace_session_hosts(["sesn_EXAMPLE", "sesn_EXAMPLE"], client)

    assert observed == {"api.anthropic.com"}
    err = capsys.readouterr().err
    assert "events.list(sesn_EXAMPLE) failed" in err


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def test_write_report_clean_returns_zero(mod, tmp_path, capsys):
    """All observed hosts allowlisted → file written, exit code 0."""
    out = tmp_path / "report.txt"
    rc = mod.write_report(
        out, {"api.anthropic.com"}, ["api.anthropic.com", "quickchart.io"]
    )

    assert rc == 0
    text = out.read_text()
    assert "[OK]   api.anthropic.com" in text
    assert "[DROP?] quickchart.io" in text
    # No blocked entries in this scenario.
    assert "[WARN]" not in text


def test_write_report_with_blocked_returns_one(mod, tmp_path):
    """Any observed-but-not-allowed host → exit code 1."""
    out = tmp_path / "report.txt"
    rc = mod.write_report(
        out, {"api.anthropic.com", "evil.example.com"}, ["api.anthropic.com"]
    )
    assert rc == 1
    text = out.read_text()
    assert "[WARN] evil.example.com" in text


# ---------------------------------------------------------------------------
# CLI smoke
# ---------------------------------------------------------------------------


def test_dry_run_prints_static_allowlist(mod, capsys, monkeypatch):
    """``--dry-run`` lists EXPECTED_HOSTS without hitting DB or API."""
    monkeypatch.setattr(mod, "_load_env", lambda: None)
    rc = mod.main(["--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "api.anthropic.com" in out
    assert "api.compresr.com" in out


def test_expected_hosts_match_provision_allowlist(mod):
    """``EXPECTED_HOSTS`` must mirror ``provision-limited-env.py:ALLOWED_HOSTS``
    (with the ``https://`` prefix stripped). The trace script's job is to
    diff observed traffic against what the provision script will configure
    — drift between the two would create false positives/negatives in the
    bake-and-flip workflow.
    """
    # login.salesforce.com is the SOAP-auth default host.
    assert "login.salesforce.com" in mod.EXPECTED_HOSTS
    # pypi/wheel CDN intentionally omitted (matches provision script's
    # allow_package_managers=false reasoning).
    assert "files.pythonhosted.org" not in mod.EXPECTED_HOSTS
    assert "pypi.org" not in mod.EXPECTED_HOSTS


def test_main_no_session_ids_returns_two(mod, monkeypatch):
    """If the DB query returns nothing, exit code is 2 (could-not-run)."""
    monkeypatch.setattr(mod, "_load_env", lambda: None)
    monkeypatch.setattr(mod, "fetch_session_ids", lambda days: [])

    rc = mod.main(["--days", "7"])
    assert rc == 2


def test_main_happy_path_invokes_trace_and_writes_report(mod, monkeypatch, tmp_path):
    """End-to-end through main(): session ids fetched, trace called, report written."""
    monkeypatch.setattr(mod, "_load_env", lambda: None)
    monkeypatch.setattr(mod, "fetch_session_ids", lambda days: ["sesn_EXAMPLE"])

    fake_client = MagicMock()
    fake_client.beta.sessions.events.list.return_value = SimpleNamespace(
        data=[_make_tool_use({"url": "https://api.anthropic.com/v1/messages"})]
    )
    monkeypatch.setattr(mod, "_build_client", lambda: fake_client)

    out_path = tmp_path / "trace.txt"
    rc = mod.main(["--days", "3", "--out", str(out_path)])

    assert rc == 0
    assert out_path.exists()
    text = out_path.read_text()
    assert "api.anthropic.com" in text
