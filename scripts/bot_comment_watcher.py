#!/usr/bin/env python3
"""Bot Comment Watcher.

Runs every 5 min via GitHub Actions cron. Processes unresolved bot comments
on open PRs:

  1. List open PRs in this repo.
  2. For each PR, fetch issue + review comments authored by bots.
  3. Compare against state — new comments since the last loop?
  4. If new: spawn an LLM call (Claude Sonnet 4.6) that returns structured
     edits. Apply edits in an isolated worktree, commit, push to the PR
     branch, reply to each bot comment, and resolve the conversation.
  5. If no new comments AND the watcher has touched the PR before,
     increment quiet_loops. At quiet_loops >= 2, merge the PR.
  6. Persist state for the next loop.

State schema (in $STATE_FILE):

    {
      "pr_state": {
        "<pr_number>": {
          "last_seen_comment_ids": [int, ...],
          "quiet_loops": int,
          "watcher_touched": bool,
          "first_seen_iso": str,
          "last_updated_iso": str,
          "fixes_today": int,
          "fixes_today_date": str  # YYYY-MM-DD
        },
        ...
      },
      "global": {"last_full_scan_iso": str | null}
    }

Required env:
  GH_TOKEN          - GitHub token with contents:write, pull-requests:write
  ANTHROPIC_API_KEY - sk-ant-* key
  GITHUB_REPOSITORY - owner/repo
  STATE_FILE        - path to state.json
  WATCHER_MODE      - normal | first-run | dry-run

Optional env:
  COMPRESR_API_KEY                       - cmp_* key. When set + flag on,
                                           the fix-request user message is
                                           routed through Compresr SDK
                                           (latte_v1, query=instruction)
                                           before reaching Anthropic. Cuts
                                           input-token spend on large
                                           diff+file payloads. Silent
                                           fallback on any error.
  COMPRESS_BOT_COMMENT_WATCHER_ENABLED   - 1/true/yes to enable. Defaults
                                           false so a fresh deploy stays
                                           untouched until explicitly opted
                                           in (mirrors the per-site flag
                                           pattern in orchestrator/config.py).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anthropic
import requests

# ---------- Constants ----------

LOG = logging.getLogger("bot-watcher")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

KNOWN_BOT_LOGINS = {
    "coderabbitai[bot]",
    "copilot[bot]",
    "dependabot[bot]",
    "claude[bot]",
    "claude-code[bot]",
    "renovate[bot]",
    "snyk-bot",
    "github-actions[bot]",
}

# When github-actions[bot] posts, distinguish by body marker. Maps marker
# substring -> friendly bot name for the LLM prompt.
GITHUB_ACTIONS_BODY_MARKERS = {
    "## Codex Review": "codex-review",
}

MERGE_QUIET_LOOPS = 2
MAX_FIXES_PER_PR_PER_DAY = 5
MODEL = "claude-sonnet-4-6"
MAX_TOKENS_PER_FIX_CALL = 8000

# ---------- Data types ----------


@dataclass
class BotComment:
    id: int
    pr_number: int
    body: str
    author: str
    author_type: str  # "Bot" or "User"
    bot_label: str  # friendly bot identifier
    is_review_comment: bool
    review_thread_id: str | None  # GraphQL node id for review threads
    path: str | None
    line: int | None
    created_at: str
    html_url: str


@dataclass
class PRState:
    last_seen_comment_ids: list[int] = field(default_factory=list)
    quiet_loops: int = 0
    watcher_touched: bool = False
    first_seen_iso: str = ""
    last_updated_iso: str = ""
    fixes_today: int = 0
    fixes_today_date: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PRState":
        return cls(
            last_seen_comment_ids=list(d.get("last_seen_comment_ids", [])),
            quiet_loops=int(d.get("quiet_loops", 0)),
            watcher_touched=bool(d.get("watcher_touched", False)),
            first_seen_iso=str(d.get("first_seen_iso", "")),
            last_updated_iso=str(d.get("last_updated_iso", "")),
            fixes_today=int(d.get("fixes_today", 0)),
            fixes_today_date=str(d.get("fixes_today_date", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_seen_comment_ids": self.last_seen_comment_ids,
            "quiet_loops": self.quiet_loops,
            "watcher_touched": self.watcher_touched,
            "first_seen_iso": self.first_seen_iso,
            "last_updated_iso": self.last_updated_iso,
            "fixes_today": self.fixes_today,
            "fixes_today_date": self.fixes_today_date,
        }


# ---------- GitHub API helpers ----------


class GHClient:
    def __init__(self, token: str, repo: str) -> None:
        self.token = token
        self.repo = repo
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )

    def _get(self, path: str, params: dict | None = None) -> Any:
        url = f"https://api.github.com{path}"
        r = self.session.get(url, params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict) -> Any:
        url = f"https://api.github.com{path}"
        r = self.session.post(url, json=body, timeout=30)
        r.raise_for_status()
        return r.json()

    def _graphql(self, query: str, variables: dict) -> Any:
        url = "https://api.github.com/graphql"
        r = self.session.post(
            url,
            json={"query": query, "variables": variables},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        if "errors" in data:
            raise RuntimeError(f"GraphQL errors: {data['errors']}")
        return data["data"]

    def list_open_prs(self) -> list[dict]:
        prs: list[dict] = []
        page = 1
        while True:
            batch = self._get(
                f"/repos/{self.repo}/pulls",
                params={"state": "open", "per_page": 100, "page": page},
            )
            prs.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return prs

    def list_pr_issue_comments(self, pr_number: int) -> list[dict]:
        return self._get(f"/repos/{self.repo}/issues/{pr_number}/comments")

    def list_pr_review_comments(self, pr_number: int) -> list[dict]:
        return self._get(f"/repos/{self.repo}/pulls/{pr_number}/comments")

    def list_unresolved_review_threads(self, pr_number: int) -> dict[int, str]:
        """Return {review_comment_id: review_thread_node_id} for unresolved threads."""
        query = """
        query($owner:String!, $repo:String!, $num:Int!) {
          repository(owner:$owner, name:$repo) {
            pullRequest(number:$num) {
              reviewThreads(first: 100) {
                nodes {
                  id
                  isResolved
                  comments(first: 100) { nodes { databaseId } }
                }
              }
            }
          }
        }
        """
        owner, name = self.repo.split("/", 1)
        data = self._graphql(query, {"owner": owner, "repo": name, "num": pr_number})
        threads = (
            data.get("repository", {})
            .get("pullRequest", {})
            .get("reviewThreads", {})
            .get("nodes", [])
        )
        out: dict[int, str] = {}
        for t in threads:
            if t.get("isResolved"):
                continue
            thread_id = t.get("id")
            for c in t.get("comments", {}).get("nodes", []):
                cid = c.get("databaseId")
                if cid is not None:
                    out[int(cid)] = thread_id
        return out

    def resolve_review_thread(self, thread_id: str) -> None:
        mutation = """
        mutation($thread_id: ID!) {
          resolveReviewThread(input: {threadId: $thread_id}) {
            thread { isResolved }
          }
        }
        """
        self._graphql(mutation, {"thread_id": thread_id})

    def reply_to_review_comment(
        self, pr_number: int, in_reply_to: int, body: str
    ) -> dict:
        return self._post(
            f"/repos/{self.repo}/pulls/{pr_number}/comments",
            {"body": body, "in_reply_to": in_reply_to},
        )

    def post_issue_comment(self, pr_number: int, body: str) -> dict:
        return self._post(
            f"/repos/{self.repo}/issues/{pr_number}/comments",
            {"body": body},
        )

    def get_pr_diff(self, pr_number: int) -> str:
        url = f"https://api.github.com/repos/{self.repo}/pulls/{pr_number}"
        r = self.session.get(
            url,
            headers={"Accept": "application/vnd.github.v3.diff"},
            timeout=30,
        )
        r.raise_for_status()
        return r.text

    def get_pr(self, pr_number: int) -> dict:
        return self._get(f"/repos/{self.repo}/pulls/{pr_number}")

    def merge_pr(self, pr_number: int) -> dict:
        return self.session.put(
            f"https://api.github.com/repos/{self.repo}/pulls/{pr_number}/merge",
            json={"merge_method": "squash"},
            timeout=30,
        ).json()


# ---------- Bot comment classification ----------


def is_bot_comment(c: dict) -> tuple[bool, str]:
    """Return (is_bot, friendly_label)."""
    user = c.get("user") or {}
    login = user.get("login", "")
    utype = user.get("type", "")

    if utype != "Bot":
        return False, ""

    if login in KNOWN_BOT_LOGINS and login != "github-actions[bot]":
        return True, login

    if login == "github-actions[bot]":
        body = c.get("body", "") or ""
        for marker, label in GITHUB_ACTIONS_BODY_MARKERS.items():
            if marker in body:
                return True, label
        return False, ""

    return True, login or "unknown-bot"


def is_resolved_message(body: str) -> bool:
    """Skip bot comments that are themselves resolution acks or self-replies."""
    markers = (
        "Resolved by bot-comment-watcher",
        "<!-- bot-watcher:resolved -->",
        "<!-- bot-watcher:reply -->",
    )
    return any(m in body for m in markers)


# ---------- Watcher core ----------


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def load_state(path: Path) -> dict:
    """Load state and ensure required top-level keys exist.

    State files written by older runs (or hand-bootstrapped) may be `{}` or
    missing keys. Normalize before returning so callers can assume keys are
    present.
    """
    if not path.exists():
        raw: dict = {}
    else:
        try:
            raw = json.loads(path.read_text())
        except json.JSONDecodeError:
            LOG.warning("state file unreadable; resetting")
            raw = {}
    raw.setdefault("pr_state", {})
    raw.setdefault("global", {})
    raw["global"].setdefault("last_full_scan_iso", None)
    return raw


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True))


def collect_bot_comments(gh: GHClient, pr_number: int) -> list[BotComment]:
    issue_comments = gh.list_pr_issue_comments(pr_number)
    review_comments = gh.list_pr_review_comments(pr_number)
    unresolved_threads = gh.list_unresolved_review_threads(pr_number)

    out: list[BotComment] = []

    for c in issue_comments:
        is_bot, label = is_bot_comment(c)
        if not is_bot:
            continue
        body = c.get("body") or ""
        if is_resolved_message(body):
            continue
        out.append(
            BotComment(
                id=int(c["id"]),
                pr_number=pr_number,
                body=body,
                author=c.get("user", {}).get("login", ""),
                author_type="Bot",
                bot_label=label,
                is_review_comment=False,
                review_thread_id=None,
                path=None,
                line=None,
                created_at=c.get("created_at", ""),
                html_url=c.get("html_url", ""),
            )
        )

    for c in review_comments:
        is_bot, label = is_bot_comment(c)
        if not is_bot:
            continue
        body = c.get("body") or ""
        if is_resolved_message(body):
            continue
        cid = int(c["id"])
        if cid not in unresolved_threads:
            continue
        out.append(
            BotComment(
                id=cid,
                pr_number=pr_number,
                body=body,
                author=c.get("user", {}).get("login", ""),
                author_type="Bot",
                bot_label=label,
                is_review_comment=True,
                review_thread_id=unresolved_threads[cid],
                path=c.get("path"),
                line=c.get("line"),
                created_at=c.get("created_at", ""),
                html_url=c.get("html_url", ""),
            )
        )

    return out


# ---------- LLM-driven fix application ----------


FIX_SYSTEM_PROMPT = """You are a code-fix agent for a Python project.

You are given:
1. The full diff of a pull request.
2. Unresolved bot comments (Codex Review, CodeRabbit, Copilot, etc.) on that PR.
3. The current contents of the files those comments reference.

Your job: produce precise, minimal file edits that address each bot comment.

CONSTRAINTS:
- Only edit files that already exist. Do not create new files.
- Each edit is a (file_path, old_string, new_string) triple. old_string MUST
  match the current file exactly (including whitespace) and be unique.
- Do not bundle multiple unrelated changes in one edit. Keep edits small.
- If a bot comment is wrong or out of scope, mark it as skipped with a
  short reason.
- Do not edit AI-tooling directories: .claude/, agents/, skills/.
- Focus on orchestrator/, scripts/, tests, docs.
- No emojis in code, prompts, or commit messages.

OUTPUT: a single JSON object matching this schema:
{
  "summary": "<one-paragraph summary of what was changed and why>",
  "edits": [
    {
      "file": "relative/path.py",
      "old": "exact string in file",
      "new": "replacement string",
      "addresses_comment_ids": [123, 456],
      "reasoning": "why this fix"
    }
  ],
  "skipped": [
    {
      "comment_id": 789,
      "reason": "why we couldn't or shouldn't act"
    }
  ]
}

Return ONLY valid JSON. No commentary, no markdown fences."""


def build_fix_user_message(
    pr: dict,
    diff: str,
    comments: list[BotComment],
    file_snapshots: dict[str, str],
) -> str:
    parts: list[str] = []
    parts.append(f"# PR #{pr['number']}: {pr['title']}")
    parts.append(f"Base: {pr['base']['ref']}  Head: {pr['head']['ref']}")
    parts.append("")
    parts.append("## Unresolved bot comments")
    for c in comments:
        loc = (
            f"{c.path}:{c.line}"
            if c.is_review_comment and c.path
            else "(general PR comment)"
        )
        parts.append(f"### Comment id={c.id} from {c.author} ({c.bot_label}) at {loc}")
        parts.append(c.body.strip())
        parts.append("")
    parts.append("## Diff")
    parts.append("```diff")
    parts.append(diff[:80000])  # cap at 80KB
    parts.append("```")
    parts.append("")
    parts.append("## Current file contents (post-edit baseline)")
    for path, content in file_snapshots.items():
        parts.append(f"### {path}")
        parts.append("```")
        parts.append(content[:40000])
        parts.append("```")
        parts.append("")
    return "\n".join(parts)


def collect_file_snapshots(workdir: Path, comments: list[BotComment]) -> dict[str, str]:
    paths = {c.path for c in comments if c.path}
    out: dict[str, str] = {}
    for p in paths:
        fp = workdir / p
        if fp.is_file():
            try:
                out[p] = fp.read_text()
            except UnicodeDecodeError:
                LOG.warning("skipping non-utf8 file %s", p)
    return out


_COMPRESS_MIN_CHARS = 8000  # Matches orchestrator/self_heal min_chars heuristic.


def _maybe_compress_user_msg(user_msg: str, instruction: str) -> str:
    """Route ``user_msg`` through Compresr if enabled; silently fall through on any error.

    Uses ``latte_v1`` with ``query=instruction`` so the SDK preserves diff
    lines and file fragments relevant to what we're asking the model to do.
    Skips compression entirely when:

      * COMPRESS_BOT_COMMENT_WATCHER_ENABLED is unset/false (default off
        so a fresh deploy doesn't silently change behavior).
      * COMPRESR_API_KEY is unset.
      * The payload is below ``_COMPRESS_MIN_CHARS`` (overhead > benefit).
      * The SDK isn't installed, the import fails, the API errors, or
        returns malformed data.

    Never raises — compression failure must not break the fix pipeline.
    Matches the silent-fallback contract from ``orchestrator/compresr_client.compress_prompt``.
    """
    if os.environ.get(
        "COMPRESS_BOT_COMMENT_WATCHER_ENABLED", ""
    ).strip().lower() not in (
        "1",
        "true",
        "yes",
    ):
        return user_msg
    api_key = os.environ.get("COMPRESR_API_KEY", "").strip()
    if not api_key:
        return user_msg
    if len(user_msg) < _COMPRESS_MIN_CHARS:
        return user_msg
    try:
        from compresr import CompressionClient  # type: ignore[import-not-found]

        client = CompressionClient(api_key=api_key, timeout=30)
        result = client.compress(
            context=user_msg,
            query=instruction,
            compression_model_name="latte_v1",
        )
        data = getattr(result, "data", None)
        compressed = getattr(data, "compressed_context", None) if data else None
        if not isinstance(compressed, str) or not compressed:
            return user_msg
        LOG.info(
            "compresr: bot_comment_watcher compressed %d → %d chars (saved %d)",
            len(user_msg),
            len(compressed),
            len(user_msg) - len(compressed),
        )
        return compressed
    except Exception as e:
        LOG.warning("compresr fallthrough (bot_comment_watcher): %s", e)
        return user_msg


def request_fixes(
    client: anthropic.Anthropic,
    pr: dict,
    diff: str,
    comments: list[BotComment],
    file_snapshots: dict[str, str],
) -> dict | None:
    user_msg = build_fix_user_message(pr, diff, comments, file_snapshots)
    # The "instruction" surface for latte_v1's query: a tight description of
    # what we're asking the model to do. Each comment body is appended so the
    # SDK keeps comment-related context. Truncated so the query stays compact.
    instruction = (
        "Fix the issues raised by the bot review comments listed. "
        + " | ".join(c.body[:200] for c in comments[:10])
    )[:2000]
    user_msg = _maybe_compress_user_msg(user_msg, instruction)
    resp = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS_PER_FIX_CALL,
        system=FIX_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = "".join(
        getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text"
    ).strip()
    if not text:
        LOG.warning("LLM returned empty body")
        return None
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        LOG.warning("LLM returned non-JSON: %s", e)
        LOG.warning("Body: %s", text[:1000])
        return None


def apply_edits(workdir: Path, edits: list[dict]) -> list[dict]:
    """Apply edits in order; return list of applied edits (with file diffs)."""
    applied: list[dict] = []
    for e in edits:
        path = workdir / e["file"]
        if not path.is_file():
            LOG.warning("edit references missing file: %s", e["file"])
            continue
        text = path.read_text()
        if e["old"] not in text:
            LOG.warning("edit old_string not found in %s", e["file"])
            continue
        if text.count(e["old"]) > 1:
            LOG.warning(
                "edit old_string ambiguous in %s (matches %d times)",
                e["file"],
                text.count(e["old"]),
            )
            continue
        new_text = text.replace(e["old"], e["new"], 1)
        path.write_text(new_text)
        applied.append(e)
        LOG.info("applied edit to %s", e["file"])
    return applied


# ---------- Worktree-isolated commit ----------


def run_in_worktree(
    workdir: Path,
    pr_number: int,
    pr_head_ref: str,
    pr_head_repo_full_name: str,
    repo_full_name: str,
    fixes: dict,
    gh_token: str,
) -> bool:
    """Apply fixes in an isolated worktree, commit, push. Return True if pushed."""
    edits = fixes.get("edits", [])
    if not edits:
        return False

    # Only operate on same-repo PRs (no forks).
    if pr_head_repo_full_name != repo_full_name:
        LOG.warning(
            "skipping PR #%d: head is from fork %s",
            pr_number,
            pr_head_repo_full_name,
        )
        return False

    wt_path = workdir.parent / f"watcher-worktree-pr-{pr_number}"
    if wt_path.exists():
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(wt_path)],
            cwd=workdir,
            check=False,
        )

    subprocess.run(
        ["git", "fetch", "origin", pr_head_ref],
        cwd=workdir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "worktree", "add", str(wt_path), f"origin/{pr_head_ref}"],
        cwd=workdir,
        check=True,
        capture_output=True,
    )

    try:
        applied = apply_edits(wt_path, edits)
        if not applied:
            LOG.info("no edits applied for PR #%d; not pushing", pr_number)
            return False

        # Stage only touched files
        files = sorted({e["file"] for e in applied})
        subprocess.run(
            ["git", "add", "--"] + files,
            cwd=wt_path,
            check=True,
            capture_output=True,
        )

        summary = fixes.get("summary", "address bot review comments")
        msg = (
            f"fix(bot-watcher): {summary[:72]}\n\n"
            f"Automated fixes by bot-comment-watcher addressing review feedback.\n"
        )
        result = subprocess.run(
            ["git", "commit", "-m", msg],
            cwd=wt_path,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            LOG.warning("commit failed: %s", result.stderr)
            return False

        push_url = f"https://x-access-token:{gh_token}@github.com/{repo_full_name}.git"
        subprocess.run(
            ["git", "push", push_url, f"HEAD:{pr_head_ref}"],
            cwd=wt_path,
            check=True,
            capture_output=True,
        )
        LOG.info("pushed fix commit to %s on PR #%d", pr_head_ref, pr_number)
        return True
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(wt_path)],
            cwd=workdir,
            check=False,
            capture_output=True,
        )


# ---------- Main loop ----------


def process_pr(
    gh: GHClient,
    pr: dict,
    state: dict,
    workdir: Path,
    client: anthropic.Anthropic | None,
    gh_token: str,
    repo_full_name: str,
    dry_run: bool,
) -> None:
    pr_num = pr["number"]
    pr_key = str(pr_num)
    pr_state_dict = state["pr_state"].get(pr_key)
    pr_state = PRState.from_dict(pr_state_dict) if pr_state_dict else PRState()

    # Reset daily counter if date rolled.
    today = today_str()
    if pr_state.fixes_today_date != today:
        pr_state.fixes_today_date = today
        pr_state.fixes_today = 0

    if not pr_state.first_seen_iso:
        pr_state.first_seen_iso = utcnow_iso()

    LOG.info(
        "PR #%d: state quiet_loops=%d watcher_touched=%s",
        pr_num,
        pr_state.quiet_loops,
        pr_state.watcher_touched,
    )

    comments = collect_bot_comments(gh, pr_num)
    current_ids = sorted(c.id for c in comments)
    new_ids = [cid for cid in current_ids if cid not in pr_state.last_seen_comment_ids]
    has_new = bool(new_ids)

    LOG.info(
        "PR #%d: %d unresolved bot comments (%d new since last loop)",
        pr_num,
        len(comments),
        len(new_ids),
    )

    if has_new and not dry_run and client is not None:
        if pr_state.fixes_today >= MAX_FIXES_PER_PR_PER_DAY:
            LOG.warning(
                "PR #%d: daily fix cap reached (%d); skipping",
                pr_num,
                pr_state.fixes_today,
            )
        else:
            diff = gh.get_pr_diff(pr_num)
            file_snapshots = collect_file_snapshots(workdir, comments)
            fixes = request_fixes(client, pr, diff, comments, file_snapshots)
            if fixes:
                pushed = run_in_worktree(
                    workdir=workdir,
                    pr_number=pr_num,
                    pr_head_ref=pr["head"]["ref"],
                    pr_head_repo_full_name=pr["head"]["repo"]["full_name"],
                    repo_full_name=repo_full_name,
                    fixes=fixes,
                    gh_token=gh_token,
                )
                if pushed:
                    pr_state.watcher_touched = True
                    pr_state.fixes_today += 1
                    addressed = {
                        cid
                        for e in fixes.get("edits", [])
                        for cid in e.get("addresses_comment_ids", [])
                    }
                    for c in comments:
                        if c.id not in addressed:
                            continue
                        reply_body = (
                            "<!-- bot-watcher:reply -->\n"
                            f"Addressed by bot-comment-watcher in the latest commit. "
                            f"Reasoning: {fixes.get('summary', '')[:280]}"
                        )
                        try:
                            if c.is_review_comment:
                                gh.reply_to_review_comment(pr_num, c.id, reply_body)
                                if c.review_thread_id:
                                    gh.resolve_review_thread(c.review_thread_id)
                            else:
                                gh.post_issue_comment(pr_num, reply_body)
                        except Exception as e:
                            LOG.warning(
                                "failed to reply/resolve comment %d: %s",
                                c.id,
                                e,
                            )
                    skipped = fixes.get("skipped", [])
                    if skipped:
                        skip_text = "\n".join(
                            f"- comment {s['comment_id']}: {s['reason']}"
                            for s in skipped
                        )
                        gh.post_issue_comment(
                            pr_num,
                            "<!-- bot-watcher:resolved -->\n"
                            f"bot-comment-watcher skipped these comments:\n{skip_text}",
                        )

    # Quiet-loop accounting: only count loops where there are NO unresolved bot
    # comments (after attempted fixes). We re-query to get fresh state.
    fresh_comments = collect_bot_comments(gh, pr_num)
    fresh_ids = sorted(c.id for c in fresh_comments)

    if fresh_comments:
        pr_state.quiet_loops = 0
    else:
        pr_state.quiet_loops += 1

    pr_state.last_seen_comment_ids = fresh_ids
    pr_state.last_updated_iso = utcnow_iso()

    # Auto-merge gate
    if (
        pr_state.watcher_touched
        and pr_state.quiet_loops >= MERGE_QUIET_LOOPS
        and not fresh_comments
        and not dry_run
    ):
        LOG.info(
            "PR #%d: quiet_loops=%d, watcher_touched, attempting merge",
            pr_num,
            pr_state.quiet_loops,
        )
        try:
            result = gh.merge_pr(pr_num)
            LOG.info("merge result: %s", result.get("message", result))
        except Exception as e:
            LOG.warning("merge failed: %s", e)

    state["pr_state"][pr_key] = pr_state.to_dict()


def main() -> int:
    gh_token = os.environ.get("GH_TOKEN")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    repo = os.environ.get("GITHUB_REPOSITORY")
    state_path = Path(os.environ.get("STATE_FILE", "./state.json"))
    mode = os.environ.get("WATCHER_MODE", "normal")

    if not gh_token or not repo:
        LOG.error("GH_TOKEN and GITHUB_REPOSITORY required")
        return 2
    if not anthropic_key:
        LOG.warning("ANTHROPIC_API_KEY not set; running in observe-only mode")
        observe_only = True
    else:
        observe_only = False

    workdir = Path.cwd()
    dry_run = mode == "dry-run" or observe_only

    gh = GHClient(gh_token, repo)
    client = anthropic.Anthropic(api_key=anthropic_key) if anthropic_key else None

    state = load_state(state_path)

    prs = gh.list_open_prs()
    LOG.info("found %d open PRs", len(prs))

    for pr in prs:
        try:
            process_pr(
                gh=gh,
                pr=pr,
                state=state,
                workdir=workdir,
                client=client,
                gh_token=gh_token,
                repo_full_name=repo,
                dry_run=dry_run,
            )
        except Exception as e:
            LOG.exception("error processing PR #%d: %s", pr["number"], e)

    state["global"]["last_full_scan_iso"] = utcnow_iso()
    save_state(state_path, state)
    LOG.info("state saved to %s", state_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
