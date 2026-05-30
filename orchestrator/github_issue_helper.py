"""Shared GitHub issue-creation helper used by self_heal → codefix_issue_creator
and self_improve → doc-drift auto-issues (Task #24).

Extracts the ``gh issue create`` subprocess wrapper that previously lived
inside ``codefix_issue_creator._create_gh_issue`` so the doc-drift auto-issue
path in ``self_improve.create_doc_drift_issue`` doesn't have to duplicate
the subprocess plumbing, the URL parsing, or the timeout/retry handling.

The helpers also expose a tiny ``list_open_issues_with_label`` dedupe wrapper
around ``gh issue list`` so callers can avoid re-opening the same drift
issue on every cron run.

Design notes:

- All shell-outs use ``subprocess.run`` with ``check=False`` and an explicit
  timeout. Failures never raise — they return ``None`` (create) or ``[]``
  (list) and log via the module logger. Cron pipelines are noisy enough
  without a stray gh CLI hiccup taking out the whole nightly run.
- The label set is passed through verbatim. Callers pick their own labels
  (codefix uses ``auto,from-self-heal``; self_improve doc-drift uses
  ``auto-doc-drift``). The dedup helper accepts a single label string so the
  two callers stay aligned with how ``gh issue list --label`` works.
- The module has zero Anthropic SDK or memory-store imports so it stays
  cheap to import from anywhere in the orchestrator.
"""

from __future__ import annotations

import logging
import subprocess
from typing import List, Optional

log = logging.getLogger(__name__)


def create_gh_issue(
    title: str,
    body: str,
    labels: str,
    timeout: int = 30,
) -> Optional[str]:
    """Create a GitHub issue via the ``gh`` CLI.

    Args:
        title: Issue title.
        body: Issue body (markdown OK).
        labels: Comma-separated label string passed straight to
            ``gh issue create --label``.
        timeout: subprocess timeout in seconds. Default 30s — gh is usually
            sub-second; the wide budget guards against a network blip.

    Returns the issue URL on success, ``None`` on any failure (subprocess
    error, non-zero exit, stdout without a URL). Logs every failure mode at
    WARNING+ so the cron handler can pick the right user-facing
    notification path.
    """
    try:
        result = subprocess.run(
            [
                "gh",
                "issue",
                "create",
                "--title",
                title,
                "--body",
                body,
                "--label",
                labels,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        log.exception("github_issue_helper: gh issue create subprocess failed")
        return None

    if result.returncode != 0:
        log.warning(
            "github_issue_helper: gh issue create exited %d — stderr: %s",
            result.returncode,
            (result.stderr or "")[:500],
        )
        return None

    # gh prints the issue URL on stdout on success.
    url = (result.stdout or "").strip().splitlines()[-1] if result.stdout else ""
    if not url.startswith("http"):
        log.warning(
            "github_issue_helper: gh stdout did not contain a URL: %r",
            (result.stdout or "")[:200],
        )
        return None
    return url


def list_open_issues_with_label(
    label: str,
    timeout: int = 30,
) -> List[str]:
    """List open issue titles labeled ``label``.

    Used by callers that want to dedupe before creating: if the same drift
    is already tracked by an open issue with the auto-label, skip the
    create. We return titles (not URLs) because the doc-drift caller wants
    to match on the title's embedded ``hot_file`` token — that's the dedup
    key for a doc-page → local-file pair.

    Returns ``[]`` on any failure (no gh CLI, network error, malformed
    output). The caller treats an empty list as "no prior issues" which is
    the right default: a transient gh failure shouldn't surface a duplicate
    issue, but it also shouldn't take out the whole cron — the dedup is a
    nice-to-have, not load-bearing.
    """
    try:
        result = subprocess.run(
            [
                "gh",
                "issue",
                "list",
                "--label",
                label,
                "--state",
                "open",
                "--limit",
                "100",
                "--json",
                "title",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        log.exception("github_issue_helper: gh issue list subprocess failed")
        return []

    if result.returncode != 0:
        log.warning(
            "github_issue_helper: gh issue list exited %d — stderr: %s",
            result.returncode,
            (result.stderr or "")[:500],
        )
        return []

    try:
        import json

        rows = json.loads(result.stdout or "[]")
    except (ValueError, TypeError):
        log.warning(
            "github_issue_helper: gh issue list returned non-JSON: %r",
            (result.stdout or "")[:200],
        )
        return []

    return [
        row.get("title", "")
        for row in rows
        if isinstance(row, dict) and row.get("title")
    ]
