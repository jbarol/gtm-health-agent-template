"""Tests for ``agents/preview_prompt.py`` (Plan #44 — Task #7).

Covers:

* AST-based prompt extraction for both source-file shapes.
* The BEFORE-vs-AFTER ``changed_prompts`` differ.
* The Anthropic SDK call is mocked — no network.
* The ANTHROPIC_API_KEY-missing branch exits 0 with the expected message.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Make ``agents.preview_prompt`` importable as a module under test.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agents import preview_prompt as pp  # noqa: E402


# ---------------------------------------------------------------------------
# Extraction tests
# ---------------------------------------------------------------------------


def test_extract_prompts_handles_prompts_dict_assignment():
    src = '''
PROMPTS = {}
PROMPTS["coordinator"] = """you are the coordinator."""
PROMPTS["quick_answer"] = "you give quick answers."
'''
    out = pp.extract_prompts(src)
    assert out == {
        "coordinator": "you are the coordinator.",
        "quick_answer": "you give quick answers.",
    }


def test_extract_prompts_handles_client_create_pattern():
    src = """
client.beta.agents.create(
    name="Pipeline Monitor",
    model="claude-sonnet-4-6",
    system="you watch pipelines.",
    tools=[],
)
"""
    out = pp.extract_prompts(src)
    assert out == {"pipeline_monitor": "you watch pipelines."}


def test_extract_prompts_handles_client_update_pattern():
    src = """
client.beta.agents.update(
    "agent_xyz",
    name="Statistician",
    system="you do statistics.",
)
"""
    out = pp.extract_prompts(src)
    assert out == {"statistician": "you do statistics."}


def test_extract_prompts_skips_dynamic_strings():
    """Dynamic system values (function calls, name refs) are skipped."""
    src = """
def _build():
    return "dynamic"

client.beta.agents.create(
    name="Dynamic Agent",
    system=_build(),
)
PROMPTS["dynamic_via_var"] = some_variable
"""
    out = pp.extract_prompts(src)
    assert out == {}


def test_extract_prompts_returns_empty_on_syntax_error():
    """Malformed source (mid-rebase) is treated as no prompts."""
    out = pp.extract_prompts("this is :: not valid python")
    assert out == {}


def test_extract_prompts_does_not_clobber_dict_entries_with_create():
    """If both a PROMPTS dict entry and a .create() call share a name,
    the dict entry wins because dict-style is the source of truth in
    ``update_prompts.py`` (per Plan #41)."""
    src = """
PROMPTS = {}
PROMPTS["coordinator"] = "from prompts dict"
client.beta.agents.create(
    name="Coordinator",
    system="from create call",
)
"""
    # Whichever the walker hits first must persist; ``setdefault`` in
    # the create branch guarantees the dict-style assignment wins
    # because it executes first in the walk (it has no nested call).
    out = pp.extract_prompts(src)
    assert out["coordinator"] == "from prompts dict"


# ---------------------------------------------------------------------------
# Differ tests (changed_prompts) — read source via git show
# ---------------------------------------------------------------------------


def test_changed_prompts_detects_only_modified_strings(monkeypatch):
    """Two prompts: one identical, one changed → only the changed one returns."""

    def fake_show(rel_path, ref):
        assert ref == "HEAD"
        if rel_path == "agents/update_prompts.py":
            return (
                "PROMPTS = {}\n"
                'PROMPTS["coordinator"] = "old coordinator text"\n'
                'PROMPTS["quick_answer"] = "shared answer text"\n'
            )
        return ""

    def fake_read(rel_path):
        if rel_path == "agents/update_prompts.py":
            return (
                "PROMPTS = {}\n"
                'PROMPTS["coordinator"] = "new coordinator text"\n'
                'PROMPTS["quick_answer"] = "shared answer text"\n'
            )
        return ""

    monkeypatch.setattr(pp, "_read_file_at_ref", fake_show)
    monkeypatch.setattr(pp, "_read_working_tree", fake_read)

    out = pp.changed_prompts(ref="HEAD")
    assert set(out.keys()) == {"coordinator"}
    before, after = out["coordinator"]
    assert before == "old coordinator text"
    assert after == "new coordinator text"


def test_changed_prompts_treats_new_prompt_as_changed(monkeypatch):
    """A prompt that did not exist in BEFORE is reported as changed."""

    monkeypatch.setattr(
        pp,
        "_read_file_at_ref",
        lambda rel_path, ref: "PROMPTS = {}\n",
    )
    monkeypatch.setattr(
        pp,
        "_read_working_tree",
        lambda rel_path: (
            'PROMPTS = {}\nPROMPTS["new_agent"] = "brand new prompt"\n'
            if rel_path == "agents/update_prompts.py"
            else ""
        ),
    )

    out = pp.changed_prompts(ref="HEAD")
    assert "new_agent" in out
    before, after = out["new_agent"]
    assert before == ""
    assert after == "brand new prompt"


# ---------------------------------------------------------------------------
# Smoke-check tests (Anthropic SDK mocked)
# ---------------------------------------------------------------------------


def _client_returning(text: str):
    """Build a MagicMock client whose messages.create returns a TextBlock."""
    client = MagicMock(name="anthropic.Anthropic")
    response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
    )
    client.messages.create.return_value = response
    return client


def test_smoke_check_pass_with_text_block():
    client = _client_returning("Hello, I am operational.")
    ok, detail = pp.smoke_check(
        "coordinator", "you are the coordinator.", client=client
    )
    assert ok is True
    assert detail.startswith("ok ")
    # SDK called with the right system + probe
    client.messages.create.assert_called_once()
    kwargs = client.messages.create.call_args.kwargs
    assert kwargs["system"] == "you are the coordinator."
    assert kwargs["messages"][0]["content"] == pp.SMOKE_PROBE
    assert kwargs["model"] == pp.SMOKE_MODEL


def test_smoke_check_pass_with_dict_block():
    """API may return dicts instead of TextBlock objects depending on SDK version."""
    client = MagicMock(name="anthropic.Anthropic")
    client.messages.create.return_value = SimpleNamespace(
        content=[{"type": "text", "text": "ok response"}]
    )
    ok, detail = pp.smoke_check("coordinator", "...", client=client)
    assert ok is True
    assert "ok response" in detail


def test_smoke_check_fail_on_sdk_error():
    client = MagicMock(name="anthropic.Anthropic")
    client.messages.create.side_effect = RuntimeError("rate-limit")
    ok, detail = pp.smoke_check("coordinator", "...", client=client)
    assert ok is False
    assert "rate-limit" in detail
    assert detail.startswith("SDK error:")


def test_smoke_check_fail_on_empty_text():
    client = MagicMock(name="anthropic.Anthropic")
    client.messages.create.return_value = SimpleNamespace(content=[])
    ok, detail = pp.smoke_check("coordinator", "...", client=client)
    assert ok is False
    assert "empty response" in detail


def test_smoke_check_fail_when_no_text_block():
    """Non-text blocks should not count toward 'ok'."""
    client = MagicMock(name="anthropic.Anthropic")
    client.messages.create.return_value = SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", text=None)]
    )
    ok, detail = pp.smoke_check("coordinator", "...", client=client)
    assert ok is False


# ---------------------------------------------------------------------------
# CLI behaviour — the ANTHROPIC_API_KEY-missing soft exit
# ---------------------------------------------------------------------------


def test_run_diff_returns_0_when_key_missing(monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # changed_prompts MUST NOT be called when the key is missing — the
    # whole point of the soft branch is to skip work cheaply.
    monkeypatch.setattr(
        pp,
        "changed_prompts",
        lambda ref="HEAD", on_skip=None: pytest.fail(
            "should not be called when key is missing"
        ),
    )
    rc = pp._run_diff(ref="HEAD")
    out = capsys.readouterr().out
    assert rc == 0
    assert "ANTHROPIC_API_KEY missing" in out
    assert "CI will gate" in out


def test_run_diff_returns_0_when_no_changes(monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(pp, "changed_prompts", lambda ref="HEAD", on_skip=None: {})
    rc = pp._run_diff(ref="HEAD")
    out = capsys.readouterr().out
    assert rc == 0
    assert "No prompt-string changes detected" in out


def test_run_diff_returns_0_on_all_pass(monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(
        pp,
        "changed_prompts",
        lambda ref="HEAD", on_skip=None: {
            "coordinator": ("old", "new"),
            "quick_answer": ("old", "new2"),
        },
    )
    client = _client_returning("Hello, operational.")
    rc = pp._run_diff(ref="HEAD", client=client)
    out = capsys.readouterr().out
    assert rc == 0
    assert "[PASS] coordinator" in out
    assert "[PASS] quick_answer" in out
    assert client.messages.create.call_count == 2


def test_run_diff_returns_1_on_any_fail(monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(
        pp,
        "changed_prompts",
        lambda ref="HEAD", on_skip=None: {"coordinator": ("old", "new")},
    )
    # SDK error → fail
    failing = MagicMock(name="anthropic.Anthropic")
    failing.messages.create.side_effect = RuntimeError("boom")
    rc = pp._run_diff(ref="HEAD", client=failing)
    out = capsys.readouterr().out
    assert rc == 1
    assert "[FAIL] coordinator" in out
    assert "FAILED for coordinator" in out
    assert "Next steps:" in out
    assert "managed-agents-conformance" in out


def test_run_file_returns_0_when_key_missing(monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    rc = pp._run_file("agents/update_prompts.py")
    out = capsys.readouterr().out
    assert rc == 0
    assert "ANTHROPIC_API_KEY missing" in out


def test_main_dispatches_to_run_diff(monkeypatch):
    monkeypatch.setattr(pp, "_run_diff", lambda ref, strict=False: 0)
    rc = pp.main(["--diff"])
    assert rc == 0


def test_main_dispatches_to_run_file(monkeypatch):
    monkeypatch.setattr(pp, "_run_file", lambda path, strict=False: 0)
    rc = pp.main(["--file", "agents/update_prompts.py"])
    assert rc == 0


def test_main_requires_one_of_diff_or_file(capsys):
    with pytest.raises(SystemExit) as exc:
        pp.main([])
    # argparse exits 2 on usage error
    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# Dynamic-string [SKIP] behavior (Plan #44 review concern #2)
# ---------------------------------------------------------------------------
#
# Per ``decision row #23`` of Plan #44, the smoke-check must announce
# any prompt that can't be statically inspected — silent skips let a
# developer refactor a prompt into an f-string by accident and ship a
# green CI run. ``extract_prompts`` now emits ``[SKIP] {name}:
# dynamic string at line {N} — cannot smoke-check`` to stderr, and
# ``--strict`` (CLI flag) converts those skips into exit 1.


def test_extract_prompts_emits_skip_line_for_fstring_prompts_dict(capsys):
    """An f-string with non-constant interpolation on a PROMPTS dict
    assignment must trigger a [SKIP] notice."""
    src = (
        "version = 'v3'\n"
        "PROMPTS = {}\n"
        'PROMPTS["coordinator"] = f"you are coordinator {version}"\n'
    )
    out = pp.extract_prompts(src)
    err = capsys.readouterr().err
    # The prompt is omitted (can't smoke-check) — no entry in the dict.
    assert "coordinator" not in out
    # And the user sees a clear [SKIP] line with the right name + line.
    assert "[SKIP] coordinator:" in err
    assert "dynamic string at line" in err
    assert "cannot smoke-check" in err


def test_extract_prompts_emits_skip_line_for_fstring_create_call(capsys):
    """An f-string interpolation in a ``client.beta.agents.create(system=...)``
    call must also trigger a [SKIP] notice keyed by the agent name."""
    src = (
        "tag = '2026'\n"
        "client.beta.agents.create(\n"
        '    name="Pipeline Monitor",\n'
        '    system=f"you watch pipelines {tag}",\n'
        ")\n"
    )
    out = pp.extract_prompts(src)
    err = capsys.readouterr().err
    assert "pipeline_monitor" not in out
    assert "[SKIP] pipeline_monitor:" in err
    assert "dynamic string at line" in err


def test_extract_prompts_emits_skip_for_format_call(capsys):
    """``"foo".format(x)`` is also dynamic — must skip with notice."""
    src = 'PROMPTS = {}\nPROMPTS["agent_a"] = "you are {role}".format(role="boss")\n'
    out = pp.extract_prompts(src)
    err = capsys.readouterr().err
    assert "agent_a" not in out
    assert "[SKIP] agent_a:" in err


def test_extract_prompts_skip_callback_receives_name_and_line():
    """The ``on_skip`` callback is the integration point for ``--strict``."""
    src = 'tag = \'x\'\nPROMPTS = {}\nPROMPTS["coord"] = f"hi {tag}"\n'
    captured = []

    def cb(name, lineno):
        captured.append((name, lineno))

    pp.extract_prompts(src, on_skip=cb)
    assert len(captured) == 1
    name, lineno = captured[0]
    assert name == "coord"
    assert lineno >= 3  # the assignment is at line 3 of src


def test_extract_prompts_constant_fstring_does_not_skip(capsys):
    """An f-string with ONLY constant pieces is still a literal string
    and should be extracted, not skipped. (Defends the existing
    fast-path in ``_string_value``.)"""
    src = 'PROMPTS = {}\nPROMPTS["coord"] = f"hello world"\n'
    out = pp.extract_prompts(src)
    err = capsys.readouterr().err
    assert out == {"coord": "hello world"}
    assert "[SKIP]" not in err


def test_run_diff_strict_returns_1_when_skips_present(monkeypatch, capsys):
    """``--strict`` must convert [SKIP] notices into exit 1 even when no
    prompts changed and no SDK calls are made."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    def fake_changed_prompts(ref="HEAD", on_skip=None):
        if on_skip is not None:
            on_skip("coordinator", 42)
        return {}

    monkeypatch.setattr(pp, "changed_prompts", fake_changed_prompts)
    rc = pp._run_diff(ref="HEAD", strict=True)
    err = capsys.readouterr().err
    assert rc == 1
    assert "--strict" in err
    assert "1 prompt(s) skipped" in err
    assert "[SKIP] coordinator:" in err


def test_run_diff_non_strict_returns_0_on_skips(monkeypatch, capsys):
    """Without ``--strict`` the same input must still exit 0 — skips are
    informational only by default."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    def fake_changed_prompts(ref="HEAD", on_skip=None):
        if on_skip is not None:
            on_skip("coordinator", 42)
        return {}

    monkeypatch.setattr(pp, "changed_prompts", fake_changed_prompts)
    rc = pp._run_diff(ref="HEAD", strict=False)
    captured = capsys.readouterr()
    assert rc == 0
    assert "[SKIP] coordinator:" in captured.err


def test_run_file_strict_returns_1_when_skips_present(monkeypatch, tmp_path, capsys):
    """``--strict`` on the ``--file`` flow also converts [SKIP] into exit 1."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    # Build a fake source file under tmp_path; redirect REPO_ROOT so the
    # path resolution lands inside our sandbox.
    monkeypatch.setattr(pp, "REPO_ROOT", tmp_path)
    src_path = tmp_path / "agents" / "update_prompts.py"
    src_path.parent.mkdir(parents=True)
    src_path.write_text('tag = \'x\'\nPROMPTS = {}\nPROMPTS["coord"] = f"hi {tag}"\n')
    rc = pp._run_file("agents/update_prompts.py", strict=True)
    err = capsys.readouterr().err
    assert rc == 1
    assert "[SKIP] coord:" in err
    assert "--strict" in err


def test_main_threads_strict_to_run_diff(monkeypatch):
    """``--strict`` on the CLI must reach ``_run_diff(strict=True)``."""
    captured = {}

    def fake_run_diff(ref, strict=False):
        captured["ref"] = ref
        captured["strict"] = strict
        return 0

    monkeypatch.setattr(pp, "_run_diff", fake_run_diff)
    rc = pp.main(["--diff", "--strict"])
    assert rc == 0
    assert captured["strict"] is True


def test_main_threads_strict_to_run_file(monkeypatch):
    """``--strict`` on the CLI must reach ``_run_file(strict=True)``."""
    captured = {}

    def fake_run_file(path, strict=False):
        captured["path"] = path
        captured["strict"] = strict
        return 0

    monkeypatch.setattr(pp, "_run_file", fake_run_file)
    rc = pp.main(["--file", "agents/update_prompts.py", "--strict"])
    assert rc == 0
    assert captured["strict"] is True
