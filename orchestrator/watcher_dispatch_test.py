"""Tests for ``orchestrator/watcher_dispatch.py``.

Covers:
    - is_path_editable accepts allowlisted paths and rejects everything else
    - branch-prefix validation rejects bad names + lets watcher/ through
    - watcher_create_branch happy path + 422 (exists) + 5xx + missing token
    - watcher_write_file blocks non-allowlisted paths BEFORE GH call
    - watcher_write_file 200 update path uses sha; 404 create path omits sha
    - watcher_create_pr enforces watcher/ branch, runs conflict check
    - _conflict_check returns conflict envelope for overlapping open PR
    - watcher_add_comment happy path + envelope shape
    - Active branch state: set on create_branch, cleared on create_pr
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "orchestrator"))

for _key, _value in {
    "WATCHER_GH_TOKEN": "ghp_test_token",
}.items():
    os.environ.setdefault(_key, _value)


import watcher_dispatch as wd  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_active_branches():
    wd._ACTIVE_BRANCHES.clear()
    yield
    wd._ACTIVE_BRANCHES.clear()


# ───────────────────────────────────────────────────────────────────────
# Path allowlist
# ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path,expected",
    [
        ("orchestrator/lifecycle.py", True),
        ("orchestrator/session_runner.py", True),
        ("orchestrator/migrations/00ZZ_new.sql", True),
        ("tests/unit/test_foo.py", True),
        ("tests/integration/foo_test.py", True),
    ],
)
def test_is_path_editable_allows_allowlist(path, expected):
    allowed, _ = wd.is_path_editable(path)
    assert allowed is expected


@pytest.mark.parametrize(
    "path",
    [
        "agents/setup_agents.py",
        "agents/provision_watcher_agent.py",
        ".github/workflows/deploy-prod.yml",
        ".github/workflows/codex-review.yml",
        "Dockerfile",
        "railway.toml",
        "bin/deploy.sh",
        ".env",
        ".env.example",
        "portco_config.json",
        "orchestrator/writing_agent.py",
        "orchestrator/main.py",  # never-edit
        "/etc/passwd",
        "../outside",
        "",
    ],
)
def test_is_path_editable_blocks_dangerous_paths(path):
    allowed, _ = wd.is_path_editable(path)
    assert allowed is False


def test_is_path_editable_blocks_unknown_paths():
    """Outside-allowlist paths are denied by default."""
    allowed, reason = wd.is_path_editable("README.md")
    assert allowed is False
    assert "not_in_allowlist" in reason


# ───────────────────────────────────────────────────────────────────────
# Branch validation
# ───────────────────────────────────────────────────────────────────────


def test_branch_prefix_required():
    ok, _ = wd._validate_branch_name("feat/something", inv_id=42)
    assert ok is False
    ok, _ = wd._validate_branch_name("watcher/42-fix-foo", inv_id=42)
    assert ok is True


def test_branch_rejects_illegal_chars():
    ok, reason = wd._validate_branch_name("watcher/bad name", inv_id=1)
    assert ok is False
    assert "illegal" in reason or "malformed" in reason


def test_branch_inv_id_prefix_is_soft(caplog):
    """Branch without inv_id prefix is allowed but logs a warning."""
    ok, _ = wd._validate_branch_name("watcher/manual-rename", inv_id=42)
    assert ok is True


# ───────────────────────────────────────────────────────────────────────
# watcher_create_branch
# ───────────────────────────────────────────────────────────────────────


def _mock_client():
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=None)
    return client


def _resp(status: int, json_body=None, text: str = ""):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = json_body if json_body is not None else {}
    r.text = text
    return r


def test_create_branch_happy_path():
    client = _mock_client()
    client.get.return_value = _resp(200, {"object": {"sha": "deadbeef"}})
    client.post.return_value = _resp(201, {"ref": "refs/heads/watcher/1-fix"})

    with patch.object(wd, "_gh_session", return_value=client):
        result = wd.watcher_create_branch(
            branch_name="watcher/1-fix-foo", inv_id=1, session_id="sess1"
        )
    assert result["ok"] is True
    assert result["branch_name"] == "watcher/1-fix-foo"
    assert result["base_sha"] == "deadbeef"
    # Active branch should be tracked
    assert wd.get_active_branch("sess1") == "watcher/1-fix-foo"


def test_create_branch_handles_422_exists():
    client = _mock_client()
    client.get.return_value = _resp(200, {"object": {"sha": "deadbeef"}})
    client.post.return_value = _resp(422, text="Reference already exists")
    with patch.object(wd, "_gh_session", return_value=client):
        result = wd.watcher_create_branch(
            branch_name="watcher/1-fix-foo", inv_id=1
        )
    assert result["ok"] is False
    assert result["reason"] == "branch_exists"


def test_create_branch_5xx_is_retryable():
    client = _mock_client()
    client.get.return_value = _resp(503, text="Service Unavailable")
    with patch.object(wd, "_gh_session", return_value=client):
        result = wd.watcher_create_branch(
            branch_name="watcher/1-fix", inv_id=1
        )
    assert result["ok"] is False
    assert result["reason"] == "github_5xx"
    assert result["retryable"] is True


def test_create_branch_missing_token():
    with patch.dict(os.environ, {"WATCHER_GH_TOKEN": ""}, clear=False):
        result = wd.watcher_create_branch(branch_name="watcher/1-fix", inv_id=1)
    assert result["ok"] is False
    assert result["reason"] == "no_token"


# ───────────────────────────────────────────────────────────────────────
# watcher_write_file
# ───────────────────────────────────────────────────────────────────────


def test_write_file_rejects_blocked_path():
    """Path enforcement runs BEFORE any GH call."""
    with patch.object(wd, "_gh_session") as fake_session:
        result = wd.watcher_write_file(
            path="agents/setup_agents.py",
            content="x",
            commit_message="msg",
            branch="watcher/1-fix",
        )
    assert result["ok"] is False
    assert result["reason"] == "path_not_editable"
    fake_session.assert_not_called()


def test_write_file_rejects_empty_commit_message():
    with patch.object(wd, "_gh_session") as fake_session:
        result = wd.watcher_write_file(
            path="orchestrator/foo.py",
            content="x",
            commit_message="  ",
            branch="watcher/1-fix",
        )
    assert result["ok"] is False
    assert result["reason"] == "empty_commit_message"
    fake_session.assert_not_called()


def test_write_file_new_file_omits_sha():
    client = _mock_client()
    client.get.return_value = _resp(404, text="not found")
    client.put.return_value = _resp(
        201, {"commit": {"sha": "abc123"}}
    )
    with patch.object(wd, "_gh_session", return_value=client):
        result = wd.watcher_write_file(
            path="orchestrator/new_module.py",
            content="def foo(): pass",
            commit_message="feat: add foo",
            branch="watcher/1-fix",
        )
    assert result["ok"] is True
    assert result["commit_sha"] == "abc123"
    # PUT body must not include sha on a new file
    put_body = client.put.call_args.kwargs["json"]
    assert "sha" not in put_body


def test_write_file_existing_file_includes_sha():
    client = _mock_client()
    client.get.return_value = _resp(200, {"sha": "existing_sha"})
    client.put.return_value = _resp(
        200, {"commit": {"sha": "new_sha"}}
    )
    with patch.object(wd, "_gh_session", return_value=client):
        wd.watcher_write_file(
            path="orchestrator/lifecycle.py",
            content="x",
            commit_message="fix: update",
            branch="watcher/1-fix",
        )
    put_body = client.put.call_args.kwargs["json"]
    assert put_body.get("sha") == "existing_sha"


# ───────────────────────────────────────────────────────────────────────
# watcher_create_pr
# ───────────────────────────────────────────────────────────────────────


def test_create_pr_rejects_non_watcher_branch():
    with patch.object(wd, "_gh_session") as fake_session:
        result = wd.watcher_create_pr(
            title="t", body="b", branch="feat/manual-edit", skip_conflict_check=True
        )
    assert result["ok"] is False
    assert result["reason"] == "branch_not_watcher_owned"
    fake_session.assert_not_called()


def test_create_pr_rejects_empty_title():
    with patch.object(wd, "_gh_session") as fake_session:
        result = wd.watcher_create_pr(
            title="  ", body="b", branch="watcher/1-fix", skip_conflict_check=True
        )
    assert result["ok"] is False
    assert result["reason"] == "empty_title_or_body"


def test_create_pr_happy_path_clears_active_branch():
    """Active-branch tracking: cleared after a successful PR open."""
    wd._ACTIVE_BRANCHES["sess1"] = "watcher/1-fix"

    client = _mock_client()
    client.post.return_value = _resp(
        201, {"number": 999, "html_url": "https://github.com/x/y/pull/999"}
    )
    with patch.object(wd, "_gh_session", return_value=client):
        result = wd.watcher_create_pr(
            title="fix: foo",
            body="body",
            branch="watcher/1-fix",
            session_id="sess1",
            skip_conflict_check=True,
        )
    assert result["ok"] is True
    assert result["pr_number"] == 999
    assert wd.get_active_branch("sess1") is None  # cleared


# ───────────────────────────────────────────────────────────────────────
# _conflict_check
# ───────────────────────────────────────────────────────────────────────


def test_conflict_check_detects_overlapping_open_pr():
    client = _mock_client()

    def _get_side_effect(url, **_kwargs):
        if "/compare/" in url:
            return _resp(
                200, {"files": [{"filename": "orchestrator/lifecycle.py"}]}
            )
        if url.endswith("/pulls") or "/pulls?" in url:
            return _resp(
                200,
                [
                    {
                        "number": 500,
                        "head": {"ref": "feat/other"},
                        "html_url": "https://github.com/x/y/pull/500",
                    }
                ],
            )
        if url.endswith("/pulls/500/files"):
            return _resp(
                200, [{"filename": "orchestrator/lifecycle.py"}]
            )
        if "/search/issues" in url:
            return _resp(200, {"items": []})
        return _resp(404, text="unhandled")

    client.get.side_effect = _get_side_effect
    with patch.object(wd, "_gh_session", return_value=client):
        result = wd._conflict_check(branch="watcher/1-fix")
    assert result is not None
    assert result["reason"] == "conflict_open_pr"
    assert result["details"]["pr_number"] == 500


def test_conflict_check_returns_none_on_no_overlap():
    client = _mock_client()

    def _get_side_effect(url, **_kwargs):
        if "/compare/" in url:
            return _resp(200, {"files": [{"filename": "orchestrator/foo.py"}]})
        if "/pulls" in url and "files" not in url:
            return _resp(200, [])
        if "/search/issues" in url:
            return _resp(200, {"items": []})
        return _resp(404, text="unhandled")

    client.get.side_effect = _get_side_effect
    with patch.object(wd, "_gh_session", return_value=client):
        result = wd._conflict_check(branch="watcher/1-fix")
    assert result is None


# ───────────────────────────────────────────────────────────────────────
# watcher_add_comment
# ───────────────────────────────────────────────────────────────────────


def test_add_comment_happy_path():
    client = _mock_client()
    client.post.return_value = _resp(
        201,
        {"id": 12345, "html_url": "https://github.com/x/y/pull/1#issuecomment-12345"},
    )
    with patch.object(wd, "_gh_session", return_value=client):
        result = wd.watcher_add_comment(pr_number=1, body="LGTM, see checklist")
    assert result["ok"] is True
    assert result["comment_id"] == 12345


def test_add_comment_rejects_invalid_pr_number():
    result = wd.watcher_add_comment(pr_number=0, body="x")
    assert result["ok"] is False
    assert result["reason"] == "invalid_pr_number"


def test_add_comment_rejects_empty_body():
    result = wd.watcher_add_comment(pr_number=1, body="   ")
    assert result["ok"] is False
    assert result["reason"] == "empty_body"
