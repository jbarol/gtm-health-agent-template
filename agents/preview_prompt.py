"""Preview changed Managed-Agent prompts with a live Anthropic smoke test.

Plan #44 — Task #7 (decision row #23). Make ``prompt-author-verified`` a
load-bearing check instead of "tripwire for forgetfulness." The actual
gate is the matching CI workflow ``.github/workflows/ci-prompt-preview.yml``
— this script is the engine both the (opt-in) local pre-commit hook AND
that CI workflow run.

Behaviour:

* Parse ``agents/setup_agents.py`` and ``agents/update_prompts.py`` with
  the stdlib ``ast`` module to extract every prompt string. Two patterns
  are recognised:

    1. ``PROMPTS["<short_name>"] = "..."``      (``update_prompts.py``)
    2. ``client.beta.agents.create(... system="...", ...)``
        and ``...update(... system="...", ...)``  (``setup_agents.py``)

* Diff against the same parse of the BEFORE version. The BEFORE source
  is fetched from ``git show HEAD:<path>`` for ``--diff`` and from
  ``git show <ref>:<path>`` when ``--ref`` is provided (used by CI to
  diff against the merge base on a PR).

* For each prompt that changed, run a smoke check via the Anthropic
  Messages API. We hit Sonnet 4.6 (cheap) with a fixed probe message —
  "Hello, please confirm you're operational and describe your role in
  one sentence" — and accept any non-empty text reply.

Soft / hard exit logic — this is deliberately asymmetric:

* ``ANTHROPIC_API_KEY`` missing in env -> exit 0 with the message
  ``"ANTHROPIC_API_KEY missing — preview skipped (CI will gate)"``.
  The local hook is convenience only; CI is authoritative.
* Otherwise: exit 0 if every changed prompt smokes ok, exit 1 with
  per-prompt ``FAILED for <name>: <reason>`` lines otherwise.

Usage:

    python -m agents.preview_prompt --diff
    python -m agents.preview_prompt --file agents/update_prompts.py
    python -m agents.preview_prompt --diff --ref origin/main

See ``docs/runbooks/managed-agents-conformance.md`` for the runbook.
"""

from __future__ import annotations

import argparse
import ast
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPT_SOURCES = ("agents/setup_agents.py", "agents/update_prompts.py")

# Anthropic smoke-check probe — short, deterministic, cheap. Any non-empty
# coherent reply is enough; we are checking that the new prompt does not
# cause the model to refuse, error out, or return empty.
SMOKE_PROBE = (
    "Hello, please confirm you're operational and describe your role in one sentence."
)
SMOKE_MODEL = "claude-sonnet-4-6"
SMOKE_MAX_TOKENS = 256

RUNBOOK_REF = "docs/runbooks/managed-agents-conformance.md"


# ---------------------------------------------------------------------------
# Prompt extraction
# ---------------------------------------------------------------------------


def _string_value(node: ast.AST) -> Optional[str]:
    """Return the literal string for ``node`` or ``None`` if not a literal.

    Handles bare ``ast.Constant`` strings and ``ast.JoinedStr`` (f-string)
    nodes whose values are all plain constants. Anything dynamic (a
    name reference, a function call, .format()) is skipped on purpose —
    we cannot smoke-check a value we cannot resolve statically.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: List[str] = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            else:
                return None
        return "".join(parts)
    return None


def _is_dynamic_string_node(node: ast.AST) -> bool:
    """Return True if ``node`` looks like a string-typed expression that
    is not a pure constant — i.e. an f-string with non-constant pieces,
    a ``str.format()`` call, a ``"%s" % x`` expression, or a binary
    string concatenation containing a non-constant value.

    We use this to distinguish "this was a dynamic prompt that needs a
    [SKIP] warning" from "this kwarg wasn't a string at all" (in which
    case we silently skip — it's e.g. ``system=None`` on a placeholder).
    """
    # f-string with at least one non-constant interpolation
    if isinstance(node, ast.JoinedStr):
        for v in node.values:
            if isinstance(v, ast.FormattedValue):
                return True
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                continue
            # any other JoinedStr child is also "dynamic"
            return True
        return False
    # `"foo {}".format(x)` or `prompt.format(...)`
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr == "format":
            return True
    # `"foo %s" % bar`
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        if isinstance(node.left, ast.Constant) and isinstance(node.left.value, str):
            return True
    # `"foo" + bar` where one side is a string literal
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        for side in (node.left, node.right):
            if isinstance(side, ast.Constant) and isinstance(side.value, str):
                return True
    return False


def extract_prompts(source: str, *, on_skip=None) -> Dict[str, str]:
    """Walk ``source`` and return ``{short_name: prompt_string}``.

    Recognises two shapes:

    * ``PROMPTS["coordinator"] = "..."`` — used in ``update_prompts.py``.
    * ``client.beta.agents.create(name=..., system="...", ...)`` and
      ``.update(... system=..., ...)`` — used in ``setup_agents.py``.
      The short name is the agent's ``name=`` kwarg lower-cased with
      whitespace turned to underscores.

    When a prompt is identified by name (``PROMPTS["x"]`` key, or
    ``name=`` kwarg on a ``create``/``update`` call) but the system /
    value expression is a dynamic string (f-string with interpolation,
    ``.format()``, ``%`` formatting, or string concatenation), the
    prompt cannot be statically smoke-checked. We invoke ``on_skip(name,
    lineno)`` — defaults to printing
    ``[SKIP] {name}: dynamic string at line {lineno} — cannot smoke-check``
    to stderr — and omit the entry. This is the Plan #44 review #2
    fix: silent skips let a developer refactor a prompt into an
    f-string by accident and have CI green-light it. ``--strict`` mode
    in the CLI converts these into an exit-1 failure (the caller
    inspects the ``on_skip`` callback to count occurrences).
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # On a malformed BEFORE/AFTER snapshot (mid-rebase, partial edit)
        # we treat the file as having no prompts. The caller will then
        # see "no changes" and exit cleanly. That is the safe failure
        # mode for a pre-commit hook.
        return {}

    def _default_on_skip(name: str, lineno: int) -> None:
        print(
            f"[SKIP] {name}: dynamic string at line {lineno} — cannot smoke-check",
            file=sys.stderr,
        )

    skip_cb = on_skip if on_skip is not None else _default_on_skip

    prompts: Dict[str, str] = {}

    for node in ast.walk(tree):
        # Pattern 1: PROMPTS["short_name"] = "..."
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Subscript)
            and isinstance(node.targets[0].value, ast.Name)
            and node.targets[0].value.id == "PROMPTS"
        ):
            key_node = node.targets[0].slice
            key = _string_value(key_node)
            value = _string_value(node.value)
            if key and value is not None:
                prompts[key] = value
            elif key and _is_dynamic_string_node(node.value):
                # Identified by name but the RHS is a dynamic string —
                # smoke check is impossible. Emit a clear [SKIP] line so
                # the developer sees the omission instead of getting a
                # silent green CI run.
                skip_cb(key, getattr(node.value, "lineno", node.lineno))

        # Pattern 2: client.beta.agents.create(..., system="...", name="...")
        #     or    .update(..., system="...", name="...")
        if isinstance(node, ast.Call):
            attr_name = ""
            if isinstance(node.func, ast.Attribute):
                attr_name = node.func.attr
            if attr_name not in ("create", "update"):
                continue
            system_kw: Optional[ast.keyword] = None
            name_kw: Optional[ast.keyword] = None
            for kw in node.keywords:
                if kw.arg == "system":
                    system_kw = kw
                elif kw.arg == "name":
                    name_kw = kw
            if system_kw is None or name_kw is None:
                continue
            agent_name = _string_value(name_kw.value)
            if agent_name is None:
                # ``name=`` itself is dynamic — we don't know what to call
                # this prompt in [SKIP] output. Silently skip; this is
                # not the failure mode the review concern targets.
                continue
            short = agent_name.strip().lower().replace(" ", "_")
            system_value = _string_value(system_kw.value)
            if system_value is not None:
                # Avoid clobbering a real PROMPTS["coordinator"] entry with
                # an unrelated update() call that happens to share a name.
                # The two source files don't actually collide today, but
                # this keeps the merge stable as patterns grow.
                prompts.setdefault(short, system_value)
            elif _is_dynamic_string_node(system_kw.value):
                if short not in prompts:
                    skip_cb(
                        short,
                        getattr(system_kw.value, "lineno", node.lineno),
                    )

    return prompts


# ---------------------------------------------------------------------------
# Diff helpers — drive the BEFORE-vs-AFTER comparison
# ---------------------------------------------------------------------------


def _read_file_at_ref(rel_path: str, ref: str) -> str:
    """Return file content at ``ref`` or ``""`` if the path didn't exist."""
    try:
        out = subprocess.run(
            ["git", "show", f"{ref}:{rel_path}"],
            cwd=str(REPO_ROOT),
            check=True,
            capture_output=True,
            text=True,
        )
        return out.stdout
    except subprocess.CalledProcessError:
        # File didn't exist at that ref — treat as empty.
        return ""
    except FileNotFoundError:
        # git not installed (rare — CI installs it; local hook needs it).
        return ""


def _read_working_tree(rel_path: str) -> str:
    p = REPO_ROOT / rel_path
    if not p.exists():
        return ""
    return p.read_text()


def changed_prompts(ref: str = "HEAD", *, on_skip=None) -> Dict[str, Tuple[str, str]]:
    """Return ``{short_name: (before, after)}`` for prompts whose string changed.

    ``ref`` is the BEFORE side — defaults to ``HEAD`` (covers the local
    pre-commit case where the diff is staged-vs-HEAD). CI passes
    ``origin/main`` (or the merge base ref) to compare against the
    branch's base.

    Dynamic-string [SKIP] notices fire only on the AFTER side — that's
    the side the developer just wrote. ``on_skip`` is plumbed through
    so the CLI can count skips for ``--strict`` mode.
    """
    diffs: Dict[str, Tuple[str, str]] = {}
    for rel_path in PROMPT_SOURCES:
        before_src = _read_file_at_ref(rel_path, ref)
        after_src = _read_working_tree(rel_path)
        # No skip messages for BEFORE — the developer can't fix a
        # historical commit from the current pre-commit hook anyway.
        before = extract_prompts(before_src, on_skip=lambda _n, _l: None)
        after = extract_prompts(after_src, on_skip=on_skip)
        for name, after_text in after.items():
            before_text = before.get(name, "")
            if before_text == after_text:
                continue
            diffs[name] = (before_text, after_text)
    return diffs


# ---------------------------------------------------------------------------
# Anthropic smoke check
# ---------------------------------------------------------------------------


def _build_anthropic_client():
    """Return an Anthropic client. Imported lazily so tests can stub."""
    import anthropic  # noqa: WPS433 — intentional local import

    return anthropic.Anthropic()


def smoke_check(
    name: str,
    system_prompt: str,
    *,
    client=None,
) -> Tuple[bool, str]:
    """Run one ``messages.create`` call. Return ``(ok, detail)``.

    ``ok=False`` if the SDK raises, the response is empty, or the
    response shape is unexpected. ``detail`` is a single line suitable
    for printing.
    """
    client = client or _build_anthropic_client()
    try:
        response = client.messages.create(
            model=SMOKE_MODEL,
            max_tokens=SMOKE_MAX_TOKENS,
            system=system_prompt,
            messages=[{"role": "user", "content": SMOKE_PROBE}],
        )
    except Exception as exc:  # noqa: BLE001 — any SDK failure is a fail
        return False, f"SDK error: {exc}"

    blocks = getattr(response, "content", None) or []
    text_parts: List[str] = []
    for block in blocks:
        # block may be a dict (raw API) or a TextBlock-like object.
        btype = getattr(block, "type", None) or (
            block.get("type") if isinstance(block, dict) else None
        )
        if btype != "text":
            continue
        text = getattr(block, "text", None)
        if text is None and isinstance(block, dict):
            text = block.get("text")
        if text:
            text_parts.append(text)
    text = "".join(text_parts).strip()
    if not text:
        return False, "empty response (no text blocks)"
    # Truncate for log readability — the smoke check only needs to know
    # that the model emitted SOMETHING coherent.
    snippet = text[:120].replace("\n", " ")
    return True, f"ok ({len(text)} chars): {snippet}"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_next_steps(failed: Iterable[str]) -> None:
    """Print the rollback-agent-style 'Next steps' block on failure."""
    failed_list = list(failed)
    if not failed_list:
        return
    print()
    print("Next steps:")
    print(
        "  1. Review the failing prompt(s) above. The model declined, "
        "errored, or returned empty text."
    )
    print(
        "  2. Re-edit agents/setup_agents.py or agents/update_prompts.py and re-stage:"
    )
    print("       git add agents/setup_agents.py agents/update_prompts.py")
    print("       git commit  # re-runs the preview")
    print("  3. To bypass the local hook (CI will still gate):  git commit --no-verify")
    print(f"  See {RUNBOOK_REF}#prompt-preview-pre-commit-hook")


def _make_skip_tracker():
    """Return ``(callback, get_count)`` — wraps the default stderr message
    plus a running counter used by ``--strict`` to convert skips into a
    non-zero exit code.
    """
    skipped: List[Tuple[str, int]] = []

    def cb(name: str, lineno: int) -> None:
        print(
            f"[SKIP] {name}: dynamic string at line {lineno} — cannot smoke-check",
            file=sys.stderr,
        )
        skipped.append((name, lineno))

    return cb, lambda: len(skipped), lambda: list(skipped)


def _run_diff(ref: str, *, client=None, strict: bool = False) -> int:
    """Drive the ``--diff`` flow. Returns the process exit code.

    ``strict=True`` converts any [SKIP] notice (dynamic f-string,
    ``.format()``, etc.) into a non-zero exit. See Plan #44 review #2.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY missing — preview skipped (CI will gate)")
        return 0

    skip_cb, skip_count, _ = _make_skip_tracker()
    diffs = changed_prompts(ref=ref, on_skip=skip_cb)
    if not diffs:
        if strict and skip_count() > 0:
            print(
                f"--strict: {skip_count()} prompt(s) skipped due to "
                "dynamic strings — see [SKIP] lines above.",
                file=sys.stderr,
            )
            return 1
        print("No prompt-string changes detected — nothing to preview.")
        return 0

    print(
        f"Previewing {len(diffs)} changed prompt(s) "
        f"(BEFORE ref: {ref}, AFTER: working tree)"
    )

    failed: List[str] = []
    for name, (_before, after) in sorted(diffs.items()):
        ok, detail = smoke_check(name, after, client=client)
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name}: {detail}")
        if not ok:
            failed.append(name)

    if failed:
        print()
        for name in failed:
            print(f"FAILED for {name}: see line above")
        _print_next_steps(failed)
        return 1

    if strict and skip_count() > 0:
        print()
        print(
            f"--strict: {skip_count()} prompt(s) skipped due to dynamic "
            "strings — see [SKIP] lines above.",
            file=sys.stderr,
        )
        return 1

    print()
    print("All prompt smoke checks passed.")
    return 0


def _run_file(rel_path: str, *, client=None, strict: bool = False) -> int:
    """Drive the ``--file <path>`` flow. Smoke-check every prompt in the file."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY missing — preview skipped (CI will gate)")
        return 0

    p = (REPO_ROOT / rel_path).resolve()
    if not p.exists():
        print(f"FAILED for {rel_path}: file does not exist", file=sys.stderr)
        return 1
    skip_cb, skip_count, _ = _make_skip_tracker()
    prompts = extract_prompts(p.read_text(), on_skip=skip_cb)
    if not prompts:
        if strict and skip_count() > 0:
            print(
                f"--strict: {skip_count()} prompt(s) skipped due to "
                "dynamic strings — see [SKIP] lines above.",
                file=sys.stderr,
            )
            return 1
        print(f"No prompts found in {rel_path} — nothing to preview.")
        return 0

    print(f"Previewing {len(prompts)} prompt(s) from {rel_path}")
    failed: List[str] = []
    for name, text in sorted(prompts.items()):
        ok, detail = smoke_check(name, text, client=client)
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name}: {detail}")
        if not ok:
            failed.append(name)

    if failed:
        print()
        for name in failed:
            print(f"FAILED for {name}: see line above")
        _print_next_steps(failed)
        return 1

    if strict and skip_count() > 0:
        print()
        print(
            f"--strict: {skip_count()} prompt(s) skipped due to dynamic "
            "strings — see [SKIP] lines above.",
            file=sys.stderr,
        )
        return 1

    print()
    print("All prompt smoke checks passed.")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Preview changed Managed-Agent prompts via an Anthropic smoke "
            "call. Used by the (opt-in) pre-commit hook and the matching "
            f"CI workflow. See {RUNBOOK_REF}."
        ),
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--diff",
        action="store_true",
        help=(
            "Smoke-check every prompt whose string changed between the "
            "BEFORE ref (default HEAD) and the working tree."
        ),
    )
    group.add_argument(
        "--file",
        metavar="PATH",
        help="Smoke-check every prompt extracted from PATH.",
    )
    parser.add_argument(
        "--ref",
        default="HEAD",
        help=(
            "BEFORE ref for --diff (default HEAD). CI passes "
            "origin/main to compare against the merge base."
        ),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Treat any [SKIP] notice (dynamic f-string, .format(), %% "
            "formatting, string concat) as a hard failure. Use in CI "
            "to keep prompt strings statically inspectable."
        ),
    )
    args = parser.parse_args(argv)

    if args.diff:
        return _run_diff(args.ref, strict=args.strict)
    return _run_file(args.file, strict=args.strict)


if __name__ == "__main__":
    sys.exit(main())
