"""Tests for ``github_issue_helper`` — the shared ``gh issue create``/``list``
subprocess wrapper extracted in PR #196 (Task #24).

Codex review (PR #196, P2): the extraction left the new module without
direct tests. The existing ``codefix_issue_creator`` tests monkeypatch the
``_create_gh_issue`` wrapper above this layer, so the subprocess plumbing,
return-code branches, stdout URL parsing, and JSON parsing paths were all
uncovered. These tests close the gap.

Run:
    cd orchestrator && python3 -m pytest github_issue_helper_test.py
"""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import github_issue_helper as gih


# ─── create_gh_issue ────────────────────────────────────────────────────────


def _fake_completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> SimpleNamespace:
    """Build the shape ``subprocess.run`` returns when ``check=False``."""
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_create_gh_issue_returns_url_on_success():
    expected_url = "https://github.com/example/repo/issues/42"
    fake_run = _fake_completed(returncode=0, stdout=f"{expected_url}\n")

    with patch.object(subprocess, "run", return_value=fake_run) as mock_run:
        url = gih.create_gh_issue("title", "body", "auto-doc-drift")

    assert url == expected_url
    # gh CLI invoked with the exact flag shape the wrapper documents.
    args, kwargs = mock_run.call_args
    cmd = args[0]
    assert cmd[0:3] == ["gh", "issue", "create"]
    assert "--title" in cmd and cmd[cmd.index("--title") + 1] == "title"
    assert "--body" in cmd and cmd[cmd.index("--body") + 1] == "body"
    assert "--label" in cmd
    assert cmd[cmd.index("--label") + 1] == "auto-doc-drift"
    # Subprocess wrapper must never raise — check=False is load-bearing.
    assert kwargs["check"] is False
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True


def test_create_gh_issue_returns_none_on_nonzero_exit():
    fake_run = _fake_completed(returncode=1, stdout="", stderr="boom")

    with patch.object(subprocess, "run", return_value=fake_run):
        url = gih.create_gh_issue("t", "b", "lbl")

    assert url is None


def test_create_gh_issue_returns_none_when_stdout_lacks_url():
    """gh sometimes prints diagnostics on stdout. If the last line doesn't
    look like a URL the helper must reject it rather than feed garbage back."""
    fake_run = _fake_completed(returncode=0, stdout="some non-url message\n")

    with patch.object(subprocess, "run", return_value=fake_run):
        url = gih.create_gh_issue("t", "b", "lbl")

    assert url is None


def test_create_gh_issue_handles_oserror():
    """The gh CLI being absent on PATH must NOT raise — the cron handler
    relies on a clean ``None`` return to fall back."""
    with patch.object(subprocess, "run", side_effect=OSError("not found")):
        url = gih.create_gh_issue("t", "b", "lbl")

    assert url is None


def test_create_gh_issue_handles_timeout():
    """The gh CLI hanging must NOT raise — return None instead."""
    with patch.object(
        subprocess, "run", side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=30)
    ):
        url = gih.create_gh_issue("t", "b", "lbl")

    assert url is None


def test_create_gh_issue_returns_last_stdout_line():
    """gh prints the URL on the last line of stdout — leading log lines
    must not confuse the parser."""
    fake_run = _fake_completed(
        returncode=0,
        stdout=(
            "Creating issue in your-org/gtm-health-agent\n"
            "https://github.com/your-org/gtm-health-agent/issues/123\n"
        ),
    )
    with patch.object(subprocess, "run", return_value=fake_run):
        url = gih.create_gh_issue("t", "b", "lbl")

    assert url == "https://github.com/your-org/gtm-health-agent/issues/123"


# ─── list_open_issues_with_label ───────────────────────────────────────────


def test_list_open_issues_with_label_parses_json_titles():
    rows = [{"title": "alpha"}, {"title": "beta"}, {"title": ""}]
    fake_run = _fake_completed(returncode=0, stdout=json.dumps(rows))

    with patch.object(subprocess, "run", return_value=fake_run) as mock_run:
        titles = gih.list_open_issues_with_label("auto-doc-drift")

    # Empty-title rows are dropped; the rest survive in order.
    assert titles == ["alpha", "beta"]
    cmd = mock_run.call_args.args[0]
    assert cmd[0:3] == ["gh", "issue", "list"]
    assert "--state" in cmd and cmd[cmd.index("--state") + 1] == "open"
    assert "--label" in cmd and cmd[cmd.index("--label") + 1] == "auto-doc-drift"
    assert "--json" in cmd and cmd[cmd.index("--json") + 1] == "title"


def test_list_open_issues_with_label_empty_on_nonzero_exit():
    fake_run = _fake_completed(returncode=2, stdout="", stderr="oops")

    with patch.object(subprocess, "run", return_value=fake_run):
        titles = gih.list_open_issues_with_label("lbl")

    # Dedup callers treat ``[]`` as "no prior issues"; this is the deliberate
    # graceful degradation path documented in the module docstring.
    assert titles == []


def test_list_open_issues_with_label_empty_on_invalid_json():
    fake_run = _fake_completed(returncode=0, stdout="not json at all")

    with patch.object(subprocess, "run", return_value=fake_run):
        titles = gih.list_open_issues_with_label("lbl")

    assert titles == []


def test_list_open_issues_with_label_handles_subprocess_error():
    with patch.object(subprocess, "run", side_effect=OSError("no gh")):
        titles = gih.list_open_issues_with_label("lbl")

    assert titles == []


def test_list_open_issues_with_label_ignores_non_dict_entries():
    """Defensive: if gh ever returns a weird shape, skip it rather than crash."""
    fake_run = _fake_completed(
        returncode=0, stdout=json.dumps(["string-row", {"title": "real"}, None])
    )

    with patch.object(subprocess, "run", return_value=fake_run):
        titles = gih.list_open_issues_with_label("lbl")

    assert titles == ["real"]
