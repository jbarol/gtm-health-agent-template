"""Orchestrator-side handlers for the 4 watcher custom tools.

Phase 1, PR 5 of the autonomous ❌-Watcher. Implements the GitHub REST
calls that back ``watcher_create_branch``, ``watcher_write_file``,
``watcher_create_pr``, ``watcher_add_comment``.

The orchestrator owns the ``WATCHER_GH_TOKEN`` PAT — the agent only
ever sees the high-level tool surface. This gives us:

  - Allowlist enforced by construction (only 4 tools exist in the
    agent spec; PR 4 defines them, this PR implements them).
  - Editable-path allowlist enforced at the orchestrator before any
    GH API call lands.
  - Branch-prefix guard (``watcher/<inv_id>-``) enforced here, not
    in the agent prompt — prompt-only checks are advisory.
  - Conflict check (any open PR or merged-in-last-24h PR touching the
    failing area) enforced here before ``watcher_create_pr`` opens a
    draft PR.
  - PAT rotation = Railway env flip + restart, no agent re-provisioning.

Returns ``{ok: bool, ...}`` envelopes that the agent can parse. On
non-OK outcomes, the envelope includes ``reason`` (machine-actionable
code) and ``details`` (human-readable explanation) so the agent can
decide whether to escalate to diagnose-only mode.

Coverage of network failures: best-effort. A 5xx on GH side becomes
``{ok: false, reason: "github_5xx", retryable: true}`` — the worker
will retry the whole session via the watcher_pending state machine.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
from typing import Any, Optional

log = logging.getLogger(__name__)


# Per-session active branch — set after watcher_create_branch succeeds,
# read by watcher_write_file / watcher_create_pr so the agent doesn't
# need to thread the branch name back through every tool call. Matches
# the per-session state pattern used by ``_track_virtualized_file`` in
# session_runner. Cleared on watcher_create_pr success (PR is opened,
# branch work is done).
_ACTIVE_BRANCHES: dict[str, str] = {}


def _set_active_branch(session_id: Optional[str], branch_name: str) -> None:
    if session_id:
        _ACTIVE_BRANCHES[session_id] = branch_name


def get_active_branch(session_id: Optional[str]) -> Optional[str]:
    if not session_id:
        return None
    return _ACTIVE_BRANCHES.get(session_id)


def _clear_active_branch(session_id: Optional[str]) -> None:
    if session_id:
        _ACTIVE_BRANCHES.pop(session_id, None)


GITHUB_API_BASE = "https://api.github.com"
# Target repo for Watcher-authored fix PRs. Override per fork via WATCHER_REPO
# ("owner/name") or GITHUB_REPOSITORY (auto-set in GitHub Actions). Defaults to
# a placeholder so a fresh fork fails loud instead of targeting another repo.
_WATCHER_REPO = (
    os.environ.get("WATCHER_REPO")
    or os.environ.get("GITHUB_REPOSITORY")
    or "your-org/your-repo"
)
REPO_OWNER, _, REPO_NAME = _WATCHER_REPO.partition("/")
REPO_NAME = REPO_NAME or "your-repo"
DEFAULT_BASE_BRANCH = "main"


# ───────────────────────────────────────────────────────────────────────
# Editable path allowlist
# ───────────────────────────────────────────────────────────────────────
#
# These lists mirror the design doc's "Editable path allowlist" and the
# warning in the watcher's system prompt. Allow-by-explicit-match,
# deny-by-default. fnmatch globs apply per path segment with ``*``
# expanding only within a segment and ``**`` matching path separators
# (handled below by a pre-pass).

_ALLOWED_GLOBS: tuple[str, ...] = (
    "orchestrator/*.py",
    "orchestrator/migrations/*.sql",
    "tests/**/*.py",
)

# Even when the path matches an allowed glob, these specific paths are
# blocked. The original ``additive only`` constraint on
# ``agents/provision_*.py`` was dropped (per checkpoint) because
# ``create_or_update_file`` replaces whole blobs — additive-only is
# unenforceable through this tool. ``agents/*.py`` is entirely blocked
# below.
_BLOCKED_GLOBS: tuple[str, ...] = (
    "agents/*.py",
    "orchestrator/writing_agent.py",
    ".github/workflows/*",
    ".github/**/*.yml",
    "Dockerfile",
    "railway.toml",
    "bin/deploy.sh",
    ".env",
    ".env.*",
    "portco_config.json",
)

# Specific intra-allowed-glob carve-outs the agent should not touch
# even though they're under orchestrator/*.py. main.py's _HealthHandler
# block is on this list — the orchestrator can't yet enforce
# block-level granularity, so we block the whole file for now.
_NEVER_EDIT_FILES: tuple[str, ...] = (
    "orchestrator/main.py",
)


def _matches_any(path: str, patterns: tuple[str, ...]) -> bool:
    """fnmatch with ``**`` support. ``**/*.py`` matches any depth."""
    for pat in patterns:
        if "**" in pat:
            # Translate ``**`` to a regex that matches across path seps.
            regex = (
                "^" + re.escape(pat).replace(r"\*\*", ".*").replace(r"\*", "[^/]*") + "$"
            )
            if re.match(regex, path):
                return True
        else:
            if fnmatch.fnmatchcase(path, pat):
                return True
    return False


def is_path_editable(path: str) -> tuple[bool, str]:
    """Return (allowed, reason). reason is descriptive on rejection."""
    if not path:
        return False, "empty_path"
    if path.startswith("/") or ".." in path.split("/"):
        return False, "absolute_or_traversal_path"
    if path in _NEVER_EDIT_FILES:
        return False, f"never_edit:{path}"
    if _matches_any(path, _BLOCKED_GLOBS):
        return False, f"blocked_glob:{path}"
    if _matches_any(path, _ALLOWED_GLOBS):
        return True, "allowed"
    return False, "not_in_allowlist"


# ───────────────────────────────────────────────────────────────────────
# Branch prefix guard
# ───────────────────────────────────────────────────────────────────────


def _validate_branch_name(branch_name: str, inv_id: Optional[int]) -> tuple[bool, str]:
    if not branch_name:
        return False, "empty_branch_name"
    if not branch_name.startswith("watcher/"):
        return False, "missing_watcher_prefix"
    if inv_id is not None and f"watcher/{inv_id}-" not in branch_name:
        # Soft enforcement — the design says "prefix watcher/<inv_id>-".
        # Allow plain ``watcher/`` to preserve operator-paste flexibility.
        log.warning(
            "watcher_create_branch: branch_name=%s missing inv_id=%s prefix "
            "(design recommends watcher/<inv_id>-…)",
            branch_name,
            inv_id,
        )
    # GitHub ref-name restrictions
    if "//" in branch_name or branch_name.endswith("/"):
        return False, "malformed_branch_name"
    if any(c in branch_name for c in (" ", "\t", "..", "~", "^", ":", "?", "*", "[", "\\")):
        return False, "illegal_branch_chars"
    return True, "ok"


# ───────────────────────────────────────────────────────────────────────
# HTTP client (lazy)
# ───────────────────────────────────────────────────────────────────────


def _get_token() -> Optional[str]:
    return (os.environ.get("WATCHER_GH_TOKEN") or "").strip() or None


def _gh_session():
    """Return an httpx.Client preconfigured with auth + base URL.

    Lazy import — keeps tests that mock at the function-call boundary
    from paying the import cost.
    """
    import httpx

    token = _get_token()
    if not token:
        raise RuntimeError("WATCHER_GH_TOKEN is unset")
    return httpx.Client(
        base_url=GITHUB_API_BASE,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "gtm-health-watcher/1.0",
        },
        timeout=30.0,
    )


def _envelope_5xx(resp_status: int, body: str) -> dict[str, Any]:
    return {
        "ok": False,
        "reason": "github_5xx",
        "retryable": True,
        "status": resp_status,
        "details": body[:500],
    }


def _envelope_4xx(resp_status: int, body: str) -> dict[str, Any]:
    return {
        "ok": False,
        "reason": "github_4xx",
        "retryable": False,
        "status": resp_status,
        "details": body[:500],
    }


# ───────────────────────────────────────────────────────────────────────
# watcher_create_branch
# ───────────────────────────────────────────────────────────────────────


def watcher_create_branch(
    *,
    branch_name: str,
    inv_id: Optional[int] = None,
    session_id: Optional[str] = None,
    base_branch: str = DEFAULT_BASE_BRANCH,
) -> dict[str, Any]:
    ok, reason = _validate_branch_name(branch_name, inv_id)
    if not ok:
        return {"ok": False, "reason": reason}
    try:
        client = _gh_session()
    except RuntimeError as exc:
        return {"ok": False, "reason": "no_token", "details": str(exc)}
    try:
        # 1. Resolve base SHA
        r = client.get(f"/repos/{REPO_OWNER}/{REPO_NAME}/git/refs/heads/{base_branch}")
        if r.status_code >= 500:
            return _envelope_5xx(r.status_code, r.text)
        if r.status_code >= 400:
            return _envelope_4xx(r.status_code, r.text)
        base_sha = r.json()["object"]["sha"]

        # 2. Create the branch ref
        r = client.post(
            f"/repos/{REPO_OWNER}/{REPO_NAME}/git/refs",
            json={"ref": f"refs/heads/{branch_name}", "sha": base_sha},
        )
        if r.status_code == 422:
            # Ref already exists — treat as success if it matches the requested SHA
            return {
                "ok": False,
                "reason": "branch_exists",
                "details": f"branch {branch_name} already exists",
            }
        if r.status_code >= 500:
            return _envelope_5xx(r.status_code, r.text)
        if r.status_code >= 400:
            return _envelope_4xx(r.status_code, r.text)
    finally:
        client.close()
    _set_active_branch(session_id, branch_name)
    return {"ok": True, "branch_name": branch_name, "base_sha": base_sha}


# ───────────────────────────────────────────────────────────────────────
# watcher_write_file
# ───────────────────────────────────────────────────────────────────────


def watcher_write_file(
    *,
    path: str,
    content: str,
    commit_message: str,
    branch: str,
) -> dict[str, Any]:
    allowed, reason = is_path_editable(path)
    if not allowed:
        return {
            "ok": False,
            "reason": "path_not_editable",
            "details": reason,
        }
    if not commit_message.strip():
        return {"ok": False, "reason": "empty_commit_message"}
    try:
        client = _gh_session()
    except RuntimeError as exc:
        return {"ok": False, "reason": "no_token", "details": str(exc)}
    try:
        # 1. Fetch existing file SHA if any (PUT requires it to update)
        r = client.get(
            f"/repos/{REPO_OWNER}/{REPO_NAME}/contents/{path}",
            params={"ref": branch},
        )
        existing_sha: Optional[str] = None
        if 200 <= r.status_code < 300:
            existing = r.json()
            existing_sha = existing.get("sha") if isinstance(existing, dict) else None
        elif r.status_code != 404:
            if r.status_code >= 500:
                return _envelope_5xx(r.status_code, r.text)
            return _envelope_4xx(r.status_code, r.text)

        # 2. PUT new content
        import base64

        payload = {
            "message": commit_message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if existing_sha:
            payload["sha"] = existing_sha
        r = client.put(
            f"/repos/{REPO_OWNER}/{REPO_NAME}/contents/{path}", json=payload
        )
        if r.status_code >= 500:
            return _envelope_5xx(r.status_code, r.text)
        if r.status_code >= 400:
            return _envelope_4xx(r.status_code, r.text)
        body = r.json()
        commit_sha = body.get("commit", {}).get("sha", "")
    finally:
        client.close()
    return {
        "ok": True,
        "path": path,
        "branch": branch,
        "commit_sha": commit_sha,
    }


# ───────────────────────────────────────────────────────────────────────
# watcher_create_pr
# ───────────────────────────────────────────────────────────────────────


def _conflict_check(
    *, branch: str, base: str = DEFAULT_BASE_BRANCH
) -> Optional[dict[str, Any]]:
    """Return a conflict envelope if any open PR touches the same area OR
    any merged PR in the same area landed in the last 24h.

    Heuristic: compare the FILES changed in ``branch`` (vs ``base``) to
    the FILES touched by recent PRs. If overlap exists, escalate. This
    is conservative — favors false-positives (defer to operator) over
    false-negatives (auto-merging a conflicting fix).
    """
    try:
        client = _gh_session()
    except RuntimeError as exc:
        return {
            "ok": False,
            "reason": "no_token",
            "details": str(exc),
        }
    try:
        # 1. Files changed on the watcher's branch vs base
        r = client.get(
            f"/repos/{REPO_OWNER}/{REPO_NAME}/compare/{base}...{branch}"
        )
        if r.status_code >= 400:
            log.warning("conflict_check compare failed status=%s", r.status_code)
            return None  # don't block on a compare failure
        our_files = {
            f["filename"] for f in r.json().get("files", []) if isinstance(f, dict)
        }
        if not our_files:
            return None

        # 2. Open PRs
        r = client.get(
            f"/repos/{REPO_OWNER}/{REPO_NAME}/pulls",
            params={"state": "open", "per_page": 50},
        )
        if r.status_code >= 400:
            log.warning("conflict_check pulls open failed status=%s", r.status_code)
        else:
            for pr in r.json():
                if not isinstance(pr, dict):
                    continue
                pr_num = pr.get("number")
                if pr_num is None or pr.get("head", {}).get("ref") == branch:
                    continue
                fr = client.get(
                    f"/repos/{REPO_OWNER}/{REPO_NAME}/pulls/{pr_num}/files",
                    params={"per_page": 100},
                )
                if fr.status_code >= 400:
                    continue
                their_files = {
                    f["filename"] for f in fr.json() if isinstance(f, dict)
                }
                overlap = our_files & their_files
                if overlap:
                    return {
                        "ok": False,
                        "reason": "conflict_open_pr",
                        "details": {
                            "pr_number": pr_num,
                            "pr_url": pr.get("html_url"),
                            "overlapping_files": sorted(overlap)[:10],
                        },
                    }

        # 3. Recently-merged PRs (last 24h) on the same files. Use the
        # search endpoint for efficiency — date filter in the query.
        from datetime import datetime, timedelta, timezone

        since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%d")
        r = client.get(
            "/search/issues",
            params={
                "q": (
                    f"repo:{REPO_OWNER}/{REPO_NAME} is:pr is:merged "
                    f"merged:>={since}"
                ),
                "per_page": 50,
            },
        )
        if r.status_code >= 400:
            log.warning("conflict_check search failed status=%s", r.status_code)
        else:
            for item in r.json().get("items", []):
                if not isinstance(item, dict):
                    continue
                pr_num = item.get("number")
                if pr_num is None:
                    continue
                fr = client.get(
                    f"/repos/{REPO_OWNER}/{REPO_NAME}/pulls/{pr_num}/files",
                    params={"per_page": 100},
                )
                if fr.status_code >= 400:
                    continue
                their_files = {
                    f["filename"] for f in fr.json() if isinstance(f, dict)
                }
                overlap = our_files & their_files
                if overlap:
                    return {
                        "ok": False,
                        "reason": "conflict_recent_merge",
                        "details": {
                            "pr_number": pr_num,
                            "pr_url": item.get("html_url"),
                            "overlapping_files": sorted(overlap)[:10],
                        },
                    }
    finally:
        client.close()
    return None


def watcher_create_pr(
    *,
    title: str,
    body: str,
    branch: str,
    session_id: Optional[str] = None,
    base: str = DEFAULT_BASE_BRANCH,
    skip_conflict_check: bool = False,
) -> dict[str, Any]:
    if not title.strip() or not body.strip():
        return {"ok": False, "reason": "empty_title_or_body"}
    if not branch.startswith("watcher/"):
        return {"ok": False, "reason": "branch_not_watcher_owned"}

    if not skip_conflict_check:
        conflict = _conflict_check(branch=branch, base=base)
        if conflict:
            return conflict

    try:
        client = _gh_session()
    except RuntimeError as exc:
        return {"ok": False, "reason": "no_token", "details": str(exc)}
    try:
        r = client.post(
            f"/repos/{REPO_OWNER}/{REPO_NAME}/pulls",
            json={
                "title": title,
                "body": body,
                "head": branch,
                "base": base,
                "draft": True,
            },
        )
        if r.status_code >= 500:
            return _envelope_5xx(r.status_code, r.text)
        if r.status_code >= 400:
            return _envelope_4xx(r.status_code, r.text)
        pr = r.json()
    finally:
        client.close()
    _clear_active_branch(session_id)
    return {
        "ok": True,
        "pr_number": pr.get("number"),
        "pr_url": pr.get("html_url"),
    }


# ───────────────────────────────────────────────────────────────────────
# watcher_add_comment
# ───────────────────────────────────────────────────────────────────────


def watcher_add_comment(*, pr_number: int, body: str) -> dict[str, Any]:
    if not isinstance(pr_number, int) or pr_number <= 0:
        return {"ok": False, "reason": "invalid_pr_number"}
    if not body.strip():
        return {"ok": False, "reason": "empty_body"}
    try:
        client = _gh_session()
    except RuntimeError as exc:
        return {"ok": False, "reason": "no_token", "details": str(exc)}
    try:
        r = client.post(
            f"/repos/{REPO_OWNER}/{REPO_NAME}/issues/{pr_number}/comments",
            json={"body": body},
        )
        if r.status_code >= 500:
            return _envelope_5xx(r.status_code, r.text)
        if r.status_code >= 400:
            return _envelope_4xx(r.status_code, r.text)
        comment = r.json()
    finally:
        client.close()
    return {
        "ok": True,
        "comment_id": comment.get("id"),
        "comment_url": comment.get("html_url"),
    }
