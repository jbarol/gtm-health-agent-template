#!/usr/bin/env python3
"""Roll back an entire prompt-deploy workflow run, agent-by-agent.

Plan #42 PR3 — thin wrapper over ``bin/rollback-agent.py``. Closes the
2026-05-11 $47 prompt-regression gap by giving the operator one
copy-paste command for the common case ("the deploy that just landed
broke something — get me back to where I was before it").

How it works:

    1. Download ``pre_deploy_versions.json`` from the named workflow run
       via ``gh run download <run_id> -n pre_deploy_versions``. This is
       the artifact ``.github/workflows/deploy-prompts.yml`` uploads BEFORE
       it calls ``update_prompts.py`` — it's the source-of-truth rollback
       target. Reading ``HEAD~1`` of ``active_versions.json`` is
       unreliable because the deploy workflow auto-commits pin updates
       and other commits can land between deploys (D9).
    2. Diff the pre-deploy versions against the current
       ``agents/active_versions.json``. Any agent whose version number
       differs is a rollback candidate.
    3. For each changed agent, invoke ``bin/rollback-agent.py
       <short_name> --to-version <pre_deploy_version>``. The existing
       script already handles the SDK reality correctly (D8: SDK has no
       ``set_active`` endpoint; rolling back means writing the old
       version's body forward as a new active version).
    4. Print a Next-steps block to stdout: which agents were rolled
       back, previous → current version numbers, recovery time.
    5. DM admins via ``slack_bot.send_dm`` with the same summary.

Usage:

    python bin/rollback-deploy.py --artifact-run <gh_run_id>
    python bin/rollback-deploy.py --artifact-run 7891234567 --apply
    python bin/rollback-deploy.py --artifact-run 7891234567       # dry-run

The dry-run is the default — operators see the plan before anything
mutates Anthropic. ``--apply`` is the explicit go.

Exit codes:
    0 = clean rollback (or clean dry-run)
    1 = partial rollback (≥1 agent failed; the rest landed)
    2 = setup failure (artifact download / version-file parse) — no
        Anthropic mutations attempted

Runbook: docs/runbook-prompt-rollback.md
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO_ROOT / "agents"
ORCH_DIR = REPO_ROOT / "orchestrator"
ROLLBACK_AGENT_SCRIPT = REPO_ROOT / "bin" / "rollback-agent.py"

# Make agents/ and orchestrator/ importable when this script is invoked
# directly so we can re-use the AGENTS registry + slack_bot helpers.
for _p in (AGENTS_DIR, ORCH_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _load_env() -> None:
    """Manual dotenv loader matching ``orchestrator/config.py``.

    Mirrors ``bin/rollback-agent.py:_load_env`` exactly so the two scripts
    behave identically when invoked side-by-side from a shell.
    """
    dotenv = REPO_ROOT / ".env"
    if not dotenv.exists():
        return
    for line in dotenv.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def _download_pre_deploy_versions(run_id: str, dest_dir: Path) -> Path:
    """Run ``gh run download`` to fetch the pre-deploy pin artifact.

    Returns the path to ``pre_deploy_versions.json`` inside ``dest_dir``.
    Raises ``SystemExit(2)`` with a clear message on any failure.
    """
    cmd = [
        "gh",
        "run",
        "download",
        run_id,
        "-n",
        "pre_deploy_versions",
        "-D",
        str(dest_dir),
    ]
    print(f"[DOWNLOAD] $ {' '.join(cmd)}")
    try:
        subprocess.run(cmd, check=True, cwd=str(REPO_ROOT))
    except FileNotFoundError as exc:
        raise SystemExit(
            f"`gh` CLI not installed on this host. Install gh "
            f"(https://cli.github.com/) and re-run. Error: {exc}"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            f"Failed to download `pre_deploy_versions` artifact from run "
            f"{run_id}: {exc}. Verify the run ID via `gh run list "
            f"--workflow=deploy-prompts.yml --limit 5`."
        ) from exc

    artifact_path = dest_dir / "active_versions.json"
    if not artifact_path.exists():
        # Fallback: gh may have downloaded the file under its own name.
        for candidate in dest_dir.rglob("*.json"):
            artifact_path = candidate
            break
    if not artifact_path.exists():
        raise SystemExit(
            f"`pre_deploy_versions` artifact downloaded to {dest_dir} but "
            f"no JSON file found inside. Run `gh run download {run_id} -n "
            f"pre_deploy_versions` manually to inspect."
        )
    return artifact_path


def _load_versions(path: Path) -> dict[str, int]:
    """Load a versions JSON file. ``SystemExit(2)`` on parse failure."""
    try:
        raw = json.loads(path.read_text())
    except Exception as exc:
        raise SystemExit(
            f"Failed to parse versions file {path}: {exc}. Expected "
            f'`{{"<agent>": <int>, ...}}` JSON.'
        ) from exc
    if not isinstance(raw, dict):
        raise SystemExit(f"Versions file {path} did not parse to a JSON object.")
    out: dict[str, int] = {}
    for k, v in raw.items():
        try:
            out[str(k)] = int(v)
        except (TypeError, ValueError):
            raise SystemExit(
                f"Versions file {path} has non-integer value for `{k}`: {v!r}."
            )
    return out


def _compute_diff(
    pre: dict[str, int], current: dict[str, int]
) -> list[tuple[str, int, int]]:
    """Return ``[(short_name, pre_version, current_version), ...]``.

    Only agents whose version numbers differ end up in the list. Agents
    that exist in one map but not the other are reported so the operator
    knows the deploy added/removed an agent.
    """
    diffs: list[tuple[str, int, int]] = []
    all_keys = set(pre) | set(current)
    for name in sorted(all_keys):
        pv = pre.get(name)
        cv = current.get(name)
        if pv is None:
            print(
                f"[WARN] agent {name!r} is in current pin file (v{cv}) but "
                f"NOT in pre-deploy artifact — was added by this deploy. "
                f"Cannot roll back."
            )
            continue
        if cv is None:
            print(
                f"[WARN] agent {name!r} was in pre-deploy artifact (v{pv}) "
                f"but is NOT in current pin file — was removed by this "
                f"deploy. Cannot roll back."
            )
            continue
        if pv != cv:
            diffs.append((name, pv, cv))
    return diffs


def _invoke_rollback_agent(
    short_name: str,
    target_version: int,
    *,
    apply: bool,
    runner=None,
) -> tuple[bool, str]:
    """Shell out to ``bin/rollback-agent.py`` for one agent.

    ``runner`` lets tests inject a fake subprocess runner. Returns
    ``(ok, message)`` so the caller can aggregate.
    """
    cmd = [
        sys.executable,
        str(ROLLBACK_AGENT_SCRIPT),
        short_name,
        "--to-version",
        str(target_version),
    ]
    if not apply:
        # Dry-run: skip the PR + DM side effects from rollback-agent.py
        # AND don't actually mutate. We achieve "don't mutate" by short
        # circuiting before invoking the script at all.
        return True, f"[DRY-RUN] would invoke: {' '.join(cmd)}"

    runner = runner or subprocess.run
    print(f"[ROLLBACK] $ {' '.join(cmd)}")
    try:
        runner(cmd, check=True, cwd=str(REPO_ROOT))
    except FileNotFoundError as exc:
        return False, f"rollback-agent.py not found: {exc}"
    except subprocess.CalledProcessError as exc:
        return False, f"rollback-agent.py failed (exit {exc.returncode})"
    return True, f"rolled back to v{target_version}"


def _send_dm_safe(text: str) -> None:
    """DM admins. Never raise — rollback must succeed even if Slack is down.

    Mirrors ``bin/rollback-agent.py:_send_dm_safe`` exactly.
    """
    try:
        from cost_digest import _resolve_admin_ids  # type: ignore
        from slack_bot import send_dm  # type: ignore
    except Exception as exc:
        print(f"[DM-SKIP] slack_bot import failed: {exc}")
        return
    try:
        admins = _resolve_admin_ids()
    except Exception as exc:
        print(f"[DM-SKIP] admin id resolution failed: {exc}")
        return
    if not admins:
        print("[DM-SKIP] no admins configured (SLACK_ADMIN_USER_IDS unset)")
        return
    for uid in admins:
        try:
            send_dm(uid, text)
            print(f"[DM-OK] sent to {uid}")
        except Exception as exc:
            print(f"[DM-FAIL] {uid}: {exc}")


def _format_summary(
    *,
    run_id: str,
    diffs: list[tuple[str, int, int]],
    results: list[tuple[str, bool, str, float]],
    total_elapsed_s: float,
    apply: bool,
) -> str:
    """Build the Next-steps block printed to stdout AND sent as Slack DM."""
    if not diffs:
        return (
            f"[ROLLBACK-DEPLOY] artifact-run {run_id}: no version changes "
            f"detected vs current active_versions.json. Nothing to roll "
            f"back."
        )

    mode = "APPLIED" if apply else "DRY-RUN"
    head = f"[ROLLBACK-DEPLOY {mode}] artifact-run {run_id}"
    lines = [head, ""]
    lines.append(f"  Agents to roll back: {len(diffs)}")
    for name, pv, cv in diffs:
        lines.append(f"    - {name:30s} v{cv} -> v{pv}")

    if apply:
        lines.append("")
        lines.append("  Per-agent results:")
        for name, ok, msg, elapsed_s in results:
            status = "OK" if ok else "FAIL"
            lines.append(f"    [{status}] {name:30s} {msg}  ({elapsed_s:.1f}s)")
        lines.append("")
        lines.append(f"  Total recovery time: {total_elapsed_s:.1f}s")
        lines.append(
            f"  Per-agent average: ~{total_elapsed_s / max(len(diffs), 1):.1f}s "
            f"(expect ~30s per agent on a healthy day)"
        )

    lines.append("")
    lines.append("  Next steps:")
    if apply:
        lines.append("    1. Verify in Anthropic console that each rolled-back agent")
        lines.append("       now serves the prior prompt.")
        lines.append("    2. Confirm Slack behavior in #acme-gtm.")
        lines.append("    3. If the rollback was triggered by a prompt regression,")
        lines.append("       open a follow-up PR with the fix (do NOT rely on the")
        lines.append("       same merge to main re-deploying — re-run the workflow).")
    else:
        lines.append("    Re-run with --apply to perform the rollback:")
        lines.append(
            f"      python bin/rollback-deploy.py --artifact-run {run_id} --apply"
        )

    lines.append("")
    lines.append("  Runbook: docs/runbook-prompt-rollback.md")
    return "\n".join(lines)


def rollback_deploy(
    artifact_run: str,
    *,
    apply: bool = False,
    workdir: Path | None = None,
    pin_path: Path | None = None,
    download_fn=None,
    rollback_fn=None,
    dm_fn=None,
) -> int:
    """Public entry point. Returns process exit code.

    Tests inject ``download_fn`` (no real ``gh`` call), ``rollback_fn``
    (no real subprocess) and ``dm_fn`` (no real Slack).
    """
    pin_path = pin_path or (AGENTS_DIR / "active_versions.json")
    if not pin_path.exists():
        print(
            f"[FATAL] {pin_path} does not exist. Run "
            f"`python agents/update_prompts.py` once to bootstrap, then "
            f"re-run this script."
        )
        return 2

    if download_fn is None:
        # Real download path: create a temp dir; default subprocess.
        if workdir is None:
            workdir = Path(tempfile.mkdtemp(prefix="rollback-deploy-"))
        try:
            artifact_path = _download_pre_deploy_versions(artifact_run, workdir)
        except SystemExit as exc:
            print(f"[FATAL] {exc}")
            return 2
    else:
        # Test path: caller supplies the artifact contents directly.
        try:
            artifact_path = download_fn(artifact_run, workdir)
        except SystemExit as exc:
            print(f"[FATAL] {exc}")
            return 2

    try:
        pre = _load_versions(artifact_path)
        current = _load_versions(pin_path)
    except SystemExit as exc:
        print(f"[FATAL] {exc}")
        return 2

    diffs = _compute_diff(pre, current)
    if not diffs:
        summary = _format_summary(
            run_id=artifact_run,
            diffs=diffs,
            results=[],
            total_elapsed_s=0.0,
            apply=apply,
        )
        print(summary)
        return 0

    results: list[tuple[str, bool, str, float]] = []
    total_t0 = time.monotonic()
    any_fail = False
    for name, pv, _cv in diffs:
        t0 = time.monotonic()
        invoke = rollback_fn or _invoke_rollback_agent
        ok, msg = invoke(name, pv, apply=apply)
        elapsed_s = time.monotonic() - t0
        results.append((name, ok, msg, elapsed_s))
        if not ok:
            any_fail = True
    total_elapsed_s = time.monotonic() - total_t0

    summary = _format_summary(
        run_id=artifact_run,
        diffs=diffs,
        results=results,
        total_elapsed_s=total_elapsed_s,
        apply=apply,
    )
    print(summary)

    if apply:
        sender = dm_fn or _send_dm_safe
        sender(summary)

    if any_fail:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Roll back an entire prompt-deploy workflow run, agent-by-agent. "
            "See docs/runbook-prompt-rollback.md for the full procedure."
        ),
    )
    parser.add_argument(
        "--artifact-run",
        required=True,
        help=(
            "GitHub Actions run ID for the deploy-prompts.yml run to roll "
            "back. Find it via `gh run list --workflow=deploy-prompts.yml "
            "--limit 5` or the link in the deploy success DM."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Perform the rollback. Without this flag the script runs in "
            "dry-run mode and only prints the planned changes."
        ),
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Default. Print the planned rollback without mutating anything.",
    )
    args = parser.parse_args(argv)

    _load_env()
    return rollback_deploy(args.artifact_run, apply=args.apply)


if __name__ == "__main__":
    sys.exit(main())
