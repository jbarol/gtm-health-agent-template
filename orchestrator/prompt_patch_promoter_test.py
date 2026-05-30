"""Tests for orchestrator/prompt_patch_promoter.py (Task #19).

Six scenarios per the plan:
  1. Happy path — 5 un-applied patches → Sonnet returns a diff → gh pr create
     called → applied ledger updated.
  2. All patches already applied (fingerprints in ledger) → no Sonnet call,
     no PR, success=True.
  3. Sonnet returns invalid diff → ``git apply --check`` fails → no PR,
     success=False, applied ledger NOT updated.
  4. ``gh pr create`` fails → applied ledger NOT updated, admin DM,
     success=False.
  5. Empty patches file → no-op, success=True.
  6. Fingerprint dedup — re-running with the same content does NOT
     re-process the same patches.
"""

from __future__ import annotations

import subprocess
import types

import pytest

import prompt_patch_promoter as ppp


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


SAMPLE_PATCHES = """# System Prompt Patches

Auto-generated improvements from session reviews.

## Patch — 2026-05-12
**Issue:** Pipeline Monitor declared MCP blocked via filesystem inspection
**Fix:** Add a rule to the Pipeline Monitor prompt: verify MCP by calling soqlQuery, not by running `which sfdx` or `ls /var/run/`.

## Patch — 2026-05-12
**Issue:** Coordinator forgot to call write_prose before post_report
**Fix:** Add to Coordinator prompt: "Every post_report MUST be preceded by a write_prose call. Failure to do so will be flagged."

## Patch — 2026-05-13
**Issue:** Sales Process Monitor used SOQL FLOOR()
**Fix:** Reinforce in the Sales Process Monitor prompt that FLOOR is not supported in SOQL.

## Patch — 2026-05-13
**Issue:** Statistician posted CI without sample size
**Fix:** Statistician prompt must require N in every CI report.

## Patch — 2026-05-14
**Issue:** Adversarial Reviewer rubber-stamped a finding
**Fix:** Add explicit five-check enforcement to the Adversarial Reviewer prompt.
"""


VALID_DIFF = """--- a/agents/setup_agents.py
+++ b/agents/setup_agents.py
@@ -1,3 +1,3 @@
-old line one
+new line one
 second line
 third line
"""


@pytest.fixture(autouse=True)
def isolate_module(monkeypatch):
    """Replace the global Anthropic client with a stub so no network calls
    happen in any test. Individual tests can monkeypatch deeper as needed.
    """
    monkeypatch.setattr(ppp, "client", _StubAnthropicClient())
    monkeypatch.setattr(ppp, "_admin_dm", lambda msg: None)
    monkeypatch.setattr(ppp, "log_messages_usage", lambda *a, **kw: None, raising=False)

    # cost_collector.track_messages_call → no-op so we don't hit the DB.
    import cost_collector

    monkeypatch.setattr(cost_collector, "track_messages_call", lambda *a, **kw: None)

    yield


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _StubMemoriesList:
    def __init__(self, data):
        self.data = data


class _StubMemoryItem:
    def __init__(self, id, path, content):
        self.id = id
        self.path = path
        self.content = content


class _StubAnthropicClient:
    """Mimics the surface ppp uses: ``client.beta.memory_stores.memories.*``
    and ``client.messages.create``.
    """

    def __init__(self):
        self._mem: dict[str, _StubMemoryItem] = {}
        self.messages_calls: list[dict] = []
        self.next_diff: str = VALID_DIFF
        self.beta = types.SimpleNamespace(
            memory_stores=types.SimpleNamespace(
                memories=types.SimpleNamespace(
                    list=self._list_memories,
                    retrieve=self._retrieve_memory,
                    update=self._update_memory,
                    create=self._create_memory,
                )
            )
        )
        self.messages = types.SimpleNamespace(create=self._create_message)

    # --- memory ----------------------------------------------------------

    def _list_memories(self, store_id, path_prefix=None):
        items = []
        for item in self._mem.values():
            if path_prefix is None or item.path.startswith(path_prefix):
                items.append(item)
        return _StubMemoriesList(items)

    def _retrieve_memory(self, mem_id, memory_store_id=None):
        for item in self._mem.values():
            if item.id == mem_id:
                return item
        raise KeyError(mem_id)

    def _update_memory(self, mem_id, memory_store_id=None, content=None):
        for item in self._mem.values():
            if item.id == mem_id:
                item.content = content
                return item
        raise KeyError(mem_id)

    def _create_memory(self, store_id, path, content):
        next_id = f"mem_{len(self._mem) + 1}"
        item = _StubMemoryItem(id=next_id, path=path, content=content)
        self._mem[path] = item
        return item

    def seed(self, path, content):
        self._create_memory(None, path, content)

    # --- messages --------------------------------------------------------

    def _create_message(self, **kwargs):
        self.messages_calls.append(kwargs)
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(type="text", text=self.next_diff)],
            usage=types.SimpleNamespace(
                input_tokens=100,
                output_tokens=50,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            ),
        )


class _SubprocessRecorder:
    """Records every subprocess.run call. Returns CompletedProcess per script."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self._script: dict[tuple[str, ...], subprocess.CompletedProcess] = {}
        self.default_pr_url = "https://github.com/your-org/gtm-health-agent/pull/9999"

    def set(self, cmd_prefix, returncode=0, stdout="", stderr=""):
        self._script[tuple(cmd_prefix)] = subprocess.CompletedProcess(
            args=cmd_prefix,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    def __call__(self, cmd, cwd, check):
        self.calls.append(list(cmd))
        # Match longest prefix.
        for k in sorted(self._script.keys(), key=lambda t: -len(t)):
            if tuple(cmd[: len(k)]) == k:
                proc = self._script[k]
                if check and proc.returncode != 0:
                    raise subprocess.CalledProcessError(
                        proc.returncode, cmd, proc.stdout, proc.stderr
                    )
                return proc
        # Default: gh pr create returns the canned URL; everything else 0.
        if cmd[:2] == ["gh", "pr"]:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=self.default_pr_url, stderr=""
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_happy_path(monkeypatch):
    """5 un-applied patches → diff → PR opens → ledger updated."""
    monkeypatch.setattr(ppp, "client", _StubAnthropicClient())
    ppp.client.seed(ppp.PATCHES_PATH, SAMPLE_PATCHES)

    recorder = _SubprocessRecorder()
    monkeypatch.setattr(ppp, "_run_subprocess", recorder)

    seen, applied, pr_url, ok = ppp.promote_prompt_patches()

    assert ok is True
    assert seen == 5
    assert applied == 5
    assert pr_url == recorder.default_pr_url

    # Sonnet was asked.
    assert len(ppp.client.messages_calls) == 1
    # gh pr create --draft was invoked.
    assert any(c[:3] == ["gh", "pr", "create"] for c in recorder.calls)
    assert any("--draft" in c for c in recorder.calls)
    # Applied ledger written and contains 5 fingerprints.
    applied_md = ppp.client._mem[ppp.APPLIED_PATH].content
    fps = ppp._parse_applied_fingerprints(applied_md)
    assert len(fps) == 5


def test_all_patches_already_applied(monkeypatch):
    """If every fingerprint is already in the ledger, no Sonnet call, no PR."""
    monkeypatch.setattr(ppp, "client", _StubAnthropicClient())
    ppp.client.seed(ppp.PATCHES_PATH, SAMPLE_PATCHES)

    # Pre-seed the applied ledger with every fingerprint from SAMPLE_PATCHES.
    blocks = ppp._parse_patch_blocks(SAMPLE_PATCHES)
    applied_md = "# Prompt Patches Applied\n\n" + "\n".join(
        f"- {b['fingerprint']} | 2026-05-13 | https://example.com/pr/1" for b in blocks
    )
    ppp.client.seed(ppp.APPLIED_PATH, applied_md)

    recorder = _SubprocessRecorder()
    monkeypatch.setattr(ppp, "_run_subprocess", recorder)

    seen, applied, pr_url, ok = ppp.promote_prompt_patches()

    assert ok is True
    assert seen == 5
    assert applied == 0
    assert pr_url is None
    # No Sonnet call.
    assert ppp.client.messages_calls == []
    # No subprocess calls (no git, no gh).
    assert recorder.calls == []


def test_invalid_diff_no_pr(monkeypatch):
    """Sonnet returns a diff that fails ``git apply --check``."""
    monkeypatch.setattr(ppp, "client", _StubAnthropicClient())
    ppp.client.seed(ppp.PATCHES_PATH, SAMPLE_PATCHES)
    ppp.client.next_diff = "this is not a valid diff"

    recorder = _SubprocessRecorder()
    # git apply --check returns 1.
    recorder.set(
        ["git", "apply", "--check"],
        returncode=1,
        stderr="error: patch does not apply",
    )
    monkeypatch.setattr(ppp, "_run_subprocess", recorder)

    seen, applied, pr_url, ok = ppp.promote_prompt_patches()

    assert ok is False
    assert seen == 5
    assert applied == 0
    assert pr_url is None
    # gh pr create NOT called.
    assert not any(c[:3] == ["gh", "pr", "create"] for c in recorder.calls)
    # Applied ledger NOT updated (no entry for APPLIED_PATH at all).
    assert ppp.APPLIED_PATH not in ppp.client._mem


def test_gh_pr_create_fails(monkeypatch):
    """gh CLI returns non-zero → ledger NOT updated, success=False."""
    monkeypatch.setattr(ppp, "client", _StubAnthropicClient())
    ppp.client.seed(ppp.PATCHES_PATH, SAMPLE_PATCHES)

    recorder = _SubprocessRecorder()
    recorder.set(["gh", "pr", "create"], returncode=1, stderr="auth error")
    monkeypatch.setattr(ppp, "_run_subprocess", recorder)

    seen, applied, pr_url, ok = ppp.promote_prompt_patches()

    assert ok is False
    assert applied == 0
    assert pr_url is None
    assert ppp.APPLIED_PATH not in ppp.client._mem


def test_empty_patches_file(monkeypatch):
    """File exists but is empty/whitespace → no-op, success=True."""
    monkeypatch.setattr(ppp, "client", _StubAnthropicClient())
    ppp.client.seed(ppp.PATCHES_PATH, "   \n\n   \n")

    recorder = _SubprocessRecorder()
    monkeypatch.setattr(ppp, "_run_subprocess", recorder)

    seen, applied, pr_url, ok = ppp.promote_prompt_patches()

    assert ok is True
    assert seen == 0
    assert applied == 0
    assert pr_url is None
    assert ppp.client.messages_calls == []
    assert recorder.calls == []


def test_fingerprint_dedup_across_runs(monkeypatch):
    """Re-running with identical content does not re-process the same patches."""
    monkeypatch.setattr(ppp, "client", _StubAnthropicClient())
    ppp.client.seed(ppp.PATCHES_PATH, SAMPLE_PATCHES)

    recorder = _SubprocessRecorder()
    monkeypatch.setattr(ppp, "_run_subprocess", recorder)

    # First run — promotes 5.
    seen1, applied1, _, ok1 = ppp.promote_prompt_patches()
    assert ok1 is True
    assert applied1 == 5

    # Second run — same content, nothing pending.
    seen2, applied2, pr_url2, ok2 = ppp.promote_prompt_patches()
    assert ok2 is True
    assert seen2 == 5
    assert applied2 == 0
    assert pr_url2 is None

    # Sonnet was called exactly once total.
    assert len(ppp.client.messages_calls) == 1


# ---------------------------------------------------------------------------
# Unit tests for the pure helpers
# ---------------------------------------------------------------------------


def test_parse_patch_blocks_handles_missing_header():
    assert ppp._parse_patch_blocks("") == []
    assert ppp._parse_patch_blocks("just some prose, no patches") == []


def test_parse_patch_blocks_extracts_each_block():
    blocks = ppp._parse_patch_blocks(SAMPLE_PATCHES)
    assert len(blocks) == 5
    assert all("**Issue:**" in b["content"] for b in blocks)
    # Fingerprints are unique per distinct content.
    fps = {b["fingerprint"] for b in blocks}
    assert len(fps) == 5


def test_fingerprint_stable_under_whitespace_changes():
    text_a = "## Patch — 2026\n**Issue:** x   \n**Fix:** y"
    text_b = "## Patch — 2026\n**Issue:** x\n**Fix:** y"
    assert ppp._fingerprint(text_a) == ppp._fingerprint(text_b)


def test_infer_agent_short_names_dedupes():
    pending = [
        {"content": "Pipeline Monitor did a thing", "fingerprint": "x"},
        {"content": "Coordinator did a thing", "fingerprint": "y"},
        {"content": "Pipeline Monitor again", "fingerprint": "z"},
    ]
    names = ppp._infer_agent_short_names(pending)
    assert names == ["coordinator", "pipeline"] or names == [
        "pipeline",
        "coordinator",
    ]


def test_pr_title_format():
    pending = [
        {"content": "Pipeline Monitor fix", "fingerprint": "a" * 64},
        {"content": "Coordinator fix", "fingerprint": "b" * 64},
    ]
    title = ppp._pr_title(pending)
    assert title.startswith("[auto] prompt patches:")
    assert "(2 patches)" in title


def test_strip_fences():
    diff = "```diff\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n```"
    stripped = ppp._strip_fences(diff)
    assert stripped.startswith("--- a/x")
    assert "```" not in stripped


def test_mark_applied_appends_without_clobbering(monkeypatch):
    stub = _StubAnthropicClient()
    monkeypatch.setattr(ppp, "client", stub)

    existing = (
        "# Prompt Patches Applied\n\nA prior run.\n"
        "- " + ("a" * 64) + " | 2026-05-01 | https://example.com/pr/1\n"
    )
    pending = [{"fingerprint": "b" * 64, "content": "..."}]
    ppp._mark_applied(existing, pending, "https://example.com/pr/2")
    stored = stub._mem[ppp.APPLIED_PATH].content
    assert "a" * 64 in stored
    assert "b" * 64 in stored
    assert "https://example.com/pr/2" in stored
