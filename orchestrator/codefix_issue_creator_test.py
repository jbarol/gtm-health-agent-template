"""Tests for ``codefix_issue_creator.create_issues_from_learnings``.

Covers the six scenarios spec'd in Plan #20:

1. Happy path — 3 blocks classified as (code_fix, prompt_patch, prompt_patch);
   only the code_fix lands a gh issue; ledger gets 1 fingerprint row.
2. Same fingerprint twice in the source — only one issue created.
3. ``gh issue create`` fails for one block — ledger NOT updated for that
   block, admin DM, partial success path returns ``success=False``.
4. Sonnet classification fails — no issues created, ``success=False``.
5. Empty learnings — no-op, ``success=True``, no DM.
6. Fingerprint dedup across runs — second run with same source finds
   the prior fingerprint in the ledger and skips.
"""

from __future__ import annotations

import json
from typing import List, Optional, Tuple
from unittest.mock import MagicMock

import pytest

import codefix_issue_creator as cic


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures — fake memory store + fake Anthropic client
# ─────────────────────────────────────────────────────────────────────────────

# Three blocks: two SOQL CASE rejects (same fingerprint) and one prompt patch.
_SOURCE_THREE_BLOCKS = """# Session Learnings — sesn_EXAMPLE (2026-05-10)

## SOQL CASE rejected by Salesforce
- **Root cause:** Pipeline Monitor inlined ``CASE WHEN stage = 'Closed Won'``; SF rejects.
- **Memory note:** SOQL does not support CASE. Compute conditionals in Python.
- **Code fix:** Add a runtime SOQL validator in orchestrator/session_runner.py that rejects CASE/COALESCE/FLOOR before the MCP call.

---

# Session Learnings — sesn_EXAMPLE (2026-05-11)

## SOQL CASE rejected by Salesforce
- **Root cause:** Sales Process Monitor repeated the bug from sesn_EXAMPLE.
- **Memory note:** Same pitfall — CASE in SOQL.
- **Code fix:** Same as before — runtime validator for unsupported SOQL keywords in orchestrator/session_runner.py.

---

# Session Learnings — sesn_EXAMPLE (2026-05-12)

## Coordinator skipped write_prose
- **Root cause:** Coordinator called post_report without the prose pass.
- **Memory note:** Always call write_prose before post_report.
- **Code fix:** Update the Coordinator system prompt in agents/setup_agents.py to enforce the write_prose ordering rule.
"""

# Empty ledger — first-ever run.
_EMPTY_LEDGER = ""

# Ledger with a prior fingerprint already created (for the dedup-across-runs test).
# Fingerprint must match what _fingerprint("soql case rejected", "orchestrator/session_runner.py", "add runtime soql validator") produces.


@pytest.fixture
def mock_memory_store(monkeypatch):
    """Fake the two memory I/O helpers so tests don't hit the Anthropic API."""
    state = {
        cic._LEARNINGS_SOURCE_PATH: _SOURCE_THREE_BLOCKS,
        cic._APPLIED_LEDGER_PATH: _EMPTY_LEDGER,
    }

    def fake_read(path: str) -> Optional[str]:
        return state.get(path, "")

    def fake_upsert(path: str, content: str) -> None:
        state[path] = content

    monkeypatch.setattr(cic, "_read_memory_file", fake_read)
    monkeypatch.setattr(cic, "_upsert_memory_file", fake_upsert)
    # Stub _admin_dm so tests don't try to import slack_bot — and so we can
    # assert how many times it fires.
    dm_log: List[str] = []
    monkeypatch.setattr(cic, "_admin_dm", lambda msg: dm_log.append(msg))
    return state, dm_log


def _make_classification_response(entries: list[dict]) -> MagicMock:
    """Build a fake Anthropic ``messages.create`` response with JSON content."""
    response = MagicMock()
    response.content = [MagicMock(type="text", text=json.dumps(entries))]
    response.usage = MagicMock(
        input_tokens=100,
        output_tokens=50,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    return response


# ─────────────────────────────────────────────────────────────────────────────
# Test 1 — happy path
# ─────────────────────────────────────────────────────────────────────────────


def test_happy_path_one_code_fix_one_issue(mock_memory_store, monkeypatch):
    state, dm_log = mock_memory_store

    # Source has two ``SOQL CASE rejected`` blocks (same fingerprint) + one
    # prompt_patch block. Configure classifier to flag the first as code_fix
    # and the others as different kinds.
    classifications = [
        {
            "block_id": "SOQL CASE rejected by Salesforce",
            "kind": "code_fix",
            "fingerprint_terms": {
                "error_pattern": "soql case rejected",
                "file_path": "orchestrator/session_runner.py",
                "proposed_action": "add runtime soql validator",
            },
        },
        {
            "block_id": "SOQL CASE rejected by Salesforce #2",
            "kind": "code_fix",
            "fingerprint_terms": {
                "error_pattern": "soql case rejected",
                "file_path": "orchestrator/session_runner.py",
                "proposed_action": "add runtime soql validator",
            },
        },
        {
            "block_id": "Coordinator skipped write_prose",
            "kind": "prompt_patch",
            "fingerprint_terms": {},
        },
    ]
    fake_response = _make_classification_response(classifications)
    monkeypatch.setattr(
        cic.client.messages,
        "create",
        MagicMock(return_value=fake_response),
    )

    gh_calls: List[Tuple[str, str]] = []

    def fake_gh(title: str, body: str) -> str:
        gh_calls.append((title, body))
        return f"https://github.com/example/repo/issues/{len(gh_calls)}"

    monkeypatch.setattr(cic, "_create_gh_issue", fake_gh)

    blocks_seen, issues_created, urls, ok = cic.create_issues_from_learnings()

    assert ok is True
    # 3 ``##`` blocks total
    assert blocks_seen == 3
    # Two code_fix entries share a fingerprint, so only ONE issue.
    assert issues_created == 1
    assert len(urls) == 1
    assert len(gh_calls) == 1
    # Issue body must contain the verbatim block text.
    title, body = gh_calls[0]
    assert "soql case rejected" in title.lower()
    assert "Add a runtime SOQL validator" in body
    # Ledger updated with exactly one fingerprint row.
    ledger = state[cic._APPLIED_LEDGER_PATH]
    fingerprint_lines = [
        line for line in ledger.splitlines() if "https://github.com/" in line
    ]
    assert len(fingerprint_lines) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Test 2 — same fingerprint twice → only one issue
# ─────────────────────────────────────────────────────────────────────────────


def test_duplicate_fingerprint_in_source(mock_memory_store, monkeypatch):
    """Two separate blocks classified as code_fix with identical terms → 1 issue."""
    state, _ = mock_memory_store

    classifications = [
        {
            "block_id": "SOQL CASE rejected by Salesforce",
            "kind": "code_fix",
            "fingerprint_terms": {
                "error_pattern": "soql case rejected",
                "file_path": "orchestrator/session_runner.py",
                "proposed_action": "add runtime soql validator",
            },
        },
        {
            "block_id": "SOQL CASE rejected by Salesforce #2",
            "kind": "code_fix",
            "fingerprint_terms": {
                "error_pattern": "soql case rejected",
                "file_path": "orchestrator/session_runner.py",
                "proposed_action": "add runtime soql validator",
            },
        },
        {
            "block_id": "Coordinator skipped write_prose",
            "kind": "observation",
            "fingerprint_terms": {},
        },
    ]
    monkeypatch.setattr(
        cic.client.messages,
        "create",
        MagicMock(return_value=_make_classification_response(classifications)),
    )

    gh_calls: List[Tuple[str, str]] = []
    monkeypatch.setattr(
        cic,
        "_create_gh_issue",
        lambda title, body: (
            gh_calls.append((title, body)),
            f"https://github.com/example/repo/issues/{len(gh_calls)}",
        )[1],
    )

    _, issues_created, urls, ok = cic.create_issues_from_learnings()
    assert ok is True
    assert issues_created == 1
    assert len(urls) == 1
    assert len(gh_calls) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Test 3 — gh issue create fails for one block; others succeed
# ─────────────────────────────────────────────────────────────────────────────


def test_gh_issue_partial_failure(mock_memory_store, monkeypatch):
    """One ``gh issue create`` returns None; ledger only logs the success row;
    admin DM fires; overall success=False (partial)."""
    state, dm_log = mock_memory_store

    # Two DIFFERENT code_fix fingerprints + one prompt_patch.
    classifications = [
        {
            "block_id": "SOQL CASE rejected by Salesforce",
            "kind": "code_fix",
            "fingerprint_terms": {
                "error_pattern": "soql case rejected",
                "file_path": "orchestrator/session_runner.py",
                "proposed_action": "add runtime soql validator",
            },
        },
        {
            "block_id": "SOQL CASE rejected by Salesforce #2",
            "kind": "code_fix",
            "fingerprint_terms": {
                "error_pattern": "mcp result overflow",
                "file_path": "orchestrator/session_runner.py",
                "proposed_action": "cap mcp result rows",
            },
        },
        {
            "block_id": "Coordinator skipped write_prose",
            "kind": "prompt_patch",
            "fingerprint_terms": {},
        },
    ]
    monkeypatch.setattr(
        cic.client.messages,
        "create",
        MagicMock(return_value=_make_classification_response(classifications)),
    )

    # First gh call succeeds, second fails (None).
    call_count = {"n": 0}

    def fake_gh(title: str, body: str) -> Optional[str]:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return "https://github.com/example/repo/issues/1"
        return None

    monkeypatch.setattr(cic, "_create_gh_issue", fake_gh)

    _, issues_created, urls, ok = cic.create_issues_from_learnings()

    assert ok is False  # partial success → False
    assert issues_created == 1  # only the successful one
    assert urls == ["https://github.com/example/repo/issues/1"]
    # Ledger must contain ONLY the successful fingerprint.
    ledger = state[cic._APPLIED_LEDGER_PATH]
    fingerprint_lines = [
        line for line in ledger.splitlines() if "https://github.com/" in line
    ]
    assert len(fingerprint_lines) == 1
    assert "issues/1" in fingerprint_lines[0]
    # Admin DM fired for the failure.
    assert any("gh issue create failed" in m for m in dm_log)


# ─────────────────────────────────────────────────────────────────────────────
# Test 4 — Sonnet classification fails → no issues created, success=False
# ─────────────────────────────────────────────────────────────────────────────


def test_classification_failure(mock_memory_store, monkeypatch):
    state, dm_log = mock_memory_store

    def raise_boom(**kwargs):
        raise RuntimeError("Sonnet API down")

    monkeypatch.setattr(cic.client.messages, "create", raise_boom)
    gh_calls: List[Tuple[str, str]] = []
    monkeypatch.setattr(cic, "_create_gh_issue", lambda t, b: gh_calls.append((t, b)))

    blocks_seen, issues_created, urls, ok = cic.create_issues_from_learnings()

    assert ok is False
    assert issues_created == 0
    assert urls == []
    assert gh_calls == []
    assert blocks_seen == 3  # we got far enough to split blocks
    # Ledger untouched.
    assert state[cic._APPLIED_LEDGER_PATH] == _EMPTY_LEDGER
    # Admin DM fired.
    assert any("classification call failed" in m for m in dm_log)


# ─────────────────────────────────────────────────────────────────────────────
# Test 5 — empty learnings → no-op, success=True, no DM
# ─────────────────────────────────────────────────────────────────────────────


def test_empty_source(monkeypatch):
    monkeypatch.setattr(cic, "_read_memory_file", lambda path: "")
    dm_log: List[str] = []
    monkeypatch.setattr(cic, "_admin_dm", lambda m: dm_log.append(m))
    classify_called = {"yes": False}

    def should_not_call(**kwargs):
        classify_called["yes"] = True
        raise AssertionError("classifier should not be called on empty source")

    monkeypatch.setattr(cic.client.messages, "create", should_not_call)

    blocks_seen, issues_created, urls, ok = cic.create_issues_from_learnings()
    assert ok is True
    assert blocks_seen == 0
    assert issues_created == 0
    assert urls == []
    assert dm_log == []
    assert classify_called["yes"] is False


# ─────────────────────────────────────────────────────────────────────────────
# Test 6 — fingerprint dedup across multiple cron runs
# ─────────────────────────────────────────────────────────────────────────────


def test_dedup_across_runs(monkeypatch):
    """Second run sees the prior fingerprint in the ledger and skips."""
    # Pre-compute the fingerprint for the only block we'll send.
    fp = cic._fingerprint(
        "soql case rejected",
        "orchestrator/session_runner.py",
        "add runtime soql validator",
    )
    # Pre-seed the applied ledger with that row.
    prior_ledger = (
        "# code_fix Issues Created\n\n"
        "| fingerprint | issue_url | created_at |\n"
        "| --- | --- | --- |\n"
        f"| {fp} | https://github.com/example/repo/issues/42 | 2026-05-13T00:00:00Z |\n"
    )

    state = {
        cic._LEARNINGS_SOURCE_PATH: _SOURCE_THREE_BLOCKS,
        cic._APPLIED_LEDGER_PATH: prior_ledger,
    }
    monkeypatch.setattr(cic, "_read_memory_file", lambda path: state.get(path, ""))
    upsert_calls: List[Tuple[str, str]] = []
    monkeypatch.setattr(
        cic,
        "_upsert_memory_file",
        lambda p, c: upsert_calls.append((p, c)),
    )
    monkeypatch.setattr(cic, "_admin_dm", lambda m: None)

    classifications = [
        {
            "block_id": "SOQL CASE rejected by Salesforce",
            "kind": "code_fix",
            "fingerprint_terms": {
                "error_pattern": "soql case rejected",
                "file_path": "orchestrator/session_runner.py",
                "proposed_action": "add runtime soql validator",
            },
        },
        {
            "block_id": "SOQL CASE rejected by Salesforce #2",
            "kind": "code_fix",
            "fingerprint_terms": {
                "error_pattern": "soql case rejected",
                "file_path": "orchestrator/session_runner.py",
                "proposed_action": "add runtime soql validator",
            },
        },
        {
            "block_id": "Coordinator skipped write_prose",
            "kind": "prompt_patch",
            "fingerprint_terms": {},
        },
    ]
    monkeypatch.setattr(
        cic.client.messages,
        "create",
        MagicMock(return_value=_make_classification_response(classifications)),
    )

    gh_calls: List[Tuple[str, str]] = []
    monkeypatch.setattr(cic, "_create_gh_issue", lambda t, b: gh_calls.append((t, b)))

    _, issues_created, urls, ok = cic.create_issues_from_learnings()
    # The fingerprint already exists in the ledger — nothing new to create.
    assert ok is True
    assert issues_created == 0
    assert urls == []
    assert gh_calls == []
    # Ledger NOT touched (no new rows means no upsert).
    assert upsert_calls == []


# ─────────────────────────────────────────────────────────────────────────────
# Auxiliary — fingerprint stability + block split tests
# ─────────────────────────────────────────────────────────────────────────────


def test_fingerprint_normalization_collapses_whitespace_and_session_ids():
    """Two slightly different wordings that normalize to the same string collide."""
    a = cic._fingerprint(
        "SOQL CASE Rejected",
        "orchestrator/session_runner.py",
        "Add runtime SOQL validator",
    )
    b = cic._fingerprint(
        "soql   case  rejected\n",
        "orchestrator/session_runner.py",
        "  add runtime soql validator  ",
    )
    assert a == b
    # Session-id and date should not affect the fingerprint.
    c = cic._fingerprint(
        "soql case rejected sesn_EXAMPLE 2026-05-10",
        "orchestrator/session_runner.py",
        "add runtime soql validator",
    )
    assert a == c


def test_split_blocks_disambiguates_repeat_headings():
    """Two ``## SOQL CASE rejected`` headings get distinct block_ids."""
    blocks = cic._split_blocks(_SOURCE_THREE_BLOCKS)
    ids = [bid for bid, _ in blocks]
    assert ids[0] == "SOQL CASE rejected by Salesforce"
    assert ids[1] == "SOQL CASE rejected by Salesforce #2"
    assert ids[2] == "Coordinator skipped write_prose"
