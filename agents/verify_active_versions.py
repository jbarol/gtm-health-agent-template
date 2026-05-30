"""Verify each agent's live active version matches ``agents/active_versions.json``.

Plan #41 — CI guardrail. Runs on every PR and every push to main via
``.github/workflows/verify-agent-versions.yml``. Exits 0 when every
agent in the pin file matches the server's current active version;
exits 1 with a clear ``agent X: live N, pin M`` listing on any
mismatch.

Why: the 2026-05-11 prompt-deploy-gap incident shipped a Coordinator
change but the live agent stayed on v20 for hours. CI verifying the
pin vs. live catches this before the next session burns spend on a
stale prompt.

Usage:
    python agents/verify_active_versions.py

Environment:
    ANTHROPIC_API_KEY — required. Standard Managed Agents read scope.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Make the same dotenv loader update_prompts.py uses available so the
# script works locally without an explicit `source .env`.
_REPO = Path(__file__).resolve().parent.parent
_DOTENV = _REPO / ".env"
if _DOTENV.exists():
    for _line in _DOTENV.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# Make agents/ importable when invoked as `python agents/verify_active_versions.py`
# from the repo root (so the import of update_prompts resolves).
_AGENTS_DIR = Path(__file__).resolve().parent
if str(_AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENTS_DIR))


def verify(client=None) -> list[str]:
    """Return a list of mismatch lines. Empty list = everything OK.

    Plan #44 Task #6 — after the per-agent live-vs-pin check, this also
    asserts ``coordinator.multiagent.agents[i].version`` matches each
    sub-agent's live ``.version``. The single-agent retrieve loop above
    cannot catch staleness inside the Coordinator's pinned roster — a
    sub-agent prompt can land cleanly on Anthropic while the Coordinator
    keeps dispatching to the old version because its multiagent snapshot
    was never refreshed. The 2026-05-11 $47 incident is the canonical
    instance of that failure.
    """
    from update_prompts import AGENTS, read_active_versions  # type: ignore

    # Read pins BEFORE constructing the client: the empty-pin "latest mode"
    # path below needs neither an API key nor a network round-trip, so a fork
    # shipping {} can run the verifier locally with no ANTHROPIC_API_KEY set.
    pins = read_active_versions()
    if not pins:
        # An empty pin map is the intentional FORK DEFAULT, not drift: no pins
        # means every session resolves to each agent's LATEST version, so there
        # is nothing to verify (no pin can disagree with live). This is
        # "latest-version mode". A fork starts pinning by running
        # `python agents/update_prompts.py` after its first prompt change.
        # Returning [] keeps the CI guardrail (verify-agent-versions.yml) green
        # for a freshly-forked repo that ships agents/active_versions.json = {}.
        print(
            "active_versions.json is empty ({}) — latest-version mode: sessions "
            "resolve to each agent's latest version, so there are no pins to "
            "verify. This is the expected default for a fresh fork; run "
            "`python agents/update_prompts.py` after your first prompt change to "
            "begin pinning."
        )
        return []

    # Non-empty pins: now we need a live client to compare each pin against
    # the server's current version. Lazy-construct it here (after the empty
    # short-circuit) so tests can still inject a stub.
    if client is None:
        import anthropic  # noqa: WPS433 — lazy so tests can stub

        client = anthropic.Anthropic()

    mismatches: list[str] = []
    # Cache retrieve() results so the multiagent parity check below can
    # reuse the live sub-agent versions we already fetched, instead of
    # firing a second round-trip per sub-agent.
    live_by_id: dict[str, int] = {}
    coord_obj = None
    for name, cfg in sorted(AGENTS.items()):
        agent_id = cfg.get("id") or ""
        pinned = pins.get(name)
        if not agent_id:
            # An agent without an ID would otherwise be silently skipped.
            # If it has a pin entry, that's a CI mis-configuration: the pin
            # says we're tracking this agent's version, but the workflow
            # didn't thread the *_AGENT_ID secret through, so we cannot
            # actually verify it. Fail loud — silent skip was the failure
            # mode codex flagged in PR #98 review.
            if pinned is not None:
                env_var = f"{name.upper()}_ID"
                # Special case: WRITING_AGENT_ID (not WRITING_AGENT_ID prefix
                # matches the var name) and DREAM_AGENT_ID (env var keeps
                # the _AGENT suffix). Map agent short-name -> env var name.
                env_var_map = {
                    "coordinator": "COORDINATOR_ID",
                    "quick_answer": "QUICK_ANSWER_ID",
                    "dream": "DREAM_AGENT_ID",
                    "pipeline_monitor": "PIPELINE_MONITOR_ID",
                    "sales_monitor": "SALES_MONITOR_ID",
                    "postsales_monitor": "POSTSALES_MONITOR_ID",
                    "statistician": "STATISTICIAN_ID",
                    "chart_designer": "CHART_DESIGNER_ID",
                    "adversarial_reviewer": "ADVERSARIAL_REVIEWER_ID",
                    "cross_domain_synthesizer": "CROSS_DOMAIN_SYNTHESIZER_ID",
                    "writing_agent": "WRITING_AGENT_ID",
                    "prompt_engineer": "PROMPT_ENGINEER_ID",
                    # Plan #52 PR-F
                    "rfp_reviewer": "RFP_REVIEWER_ID",
                    "rfp_responder": "RFP_RESPONDER_ID",
                }
                env_var = env_var_map.get(name, f"{name.upper()}_ID")
                mismatches.append(
                    f"agent {name}: pinned at version {pinned} but no ID "
                    f"is set — verification cannot run. Ensure {env_var} "
                    f"is threaded through .github/workflows/"
                    f"verify-agent-versions.yml (it's already a GitHub "
                    f"repo secret)."
                )
            # else: not provisioned and not pinned — genuinely not yet
            # tracked, safe to ignore.
            continue
        if pinned is None:
            mismatches.append(
                f"agent {name}: live ? , pin missing — add an entry to "
                "agents/active_versions.json or re-run update_prompts.py"
            )
            continue
        try:
            agent = client.beta.agents.retrieve(agent_id)
            live_version = int(agent.version)
            live_by_id[agent_id] = live_version
            if name == "coordinator":
                coord_obj = agent
        except Exception as exc:
            mismatches.append(f"agent {name} ({agent_id}): retrieve failed: {exc}")
            continue
        if live_version != int(pinned):
            mismatches.append(f"agent {name}: live {live_version}, pin {pinned}")

    # Plan #44 Task #6 — Coordinator multiagent pin parity.
    #
    # The above loop catches per-agent live-vs-pin drift, but it cannot
    # see staleness inside the Coordinator's multiagent.agents[] block.
    # Anthropic snapshots each sub-agent's version into the parent at
    # parent-update time and never auto-advances — a sub-agent prompt
    # change can land cleanly on Anthropic while the Coordinator keeps
    # dispatching to the old version because its snapshot was never
    # refreshed. The verifier must catch that drift before the next
    # session burns spend on a stale sub-agent.
    mismatches.extend(_verify_multiagent_pin_parity(client, coord_obj, live_by_id))
    return mismatches


def _verify_multiagent_pin_parity(
    client, coord_obj, live_by_id: dict[str, int]
) -> list[str]:
    """Return drift lines for the Coordinator's multiagent.agents[] block.

    Reuses the cached Coordinator object retrieved by the per-agent loop
    so we don't fire a second round-trip. Compares each entry's
    ``version`` against the live sub-agent ``.version``; re-retrieves
    sub-agents the per-agent loop didn't touch (e.g. an entry the
    Coordinator references that's no longer listed in ``AGENTS``).
    Empty list = parity OK.
    """
    if coord_obj is None:
        # Coordinator was skipped or failed earlier — the per-agent loop
        # already produced the right error line; don't duplicate it.
        return []

    coord_multiagent = getattr(coord_obj, "multiagent", None)
    coord_agents = (
        getattr(coord_multiagent, "agents", None) if coord_multiagent else None
    )
    if not coord_agents:
        # No multiagent block (e.g. the Coordinator was provisioned
        # standalone). Nothing to verify — silent OK.
        return []

    drift: list[str] = []
    for entry in coord_agents:
        entry_id = getattr(entry, "id", None) or (
            entry.get("id") if isinstance(entry, dict) else None
        )
        pinned_version = getattr(entry, "version", None) or (
            entry.get("version") if isinstance(entry, dict) else None
        )
        if entry_id is None or pinned_version is None:
            drift.append(
                "coordinator multiagent pin drift: entry missing id or version "
                f"({entry!r})"
            )
            continue
        live_version = live_by_id.get(entry_id)
        if live_version is None:
            # The entry references a sub-agent we didn't retrieve in
            # the per-agent loop (likely an agent not in AGENTS — old
            # entries can linger). Fetch it now so the parity check is
            # complete; a stale entry that resolves to the live version
            # is still a parity match.
            try:
                sub = client.beta.agents.retrieve(entry_id)
                live_version = int(sub.version)
            except Exception as exc:
                drift.append(
                    f"coordinator multiagent pin drift: sub-agent "
                    f"{entry_id} retrieve failed: {exc}"
                )
                continue
        if int(pinned_version) != int(live_version):
            drift.append(
                f"coordinator multiagent pin drift: sub-agent {entry_id}: "
                f"coordinator pin v{pinned_version}, live v{live_version}. "
                "Run `python agents/update_subagent_tools.py` or re-run "
                "`python agents/update_prompts.py` to re-publish the "
                "Coordinator's multiagent block."
            )
    return drift


def main() -> int:
    mismatches = verify()
    if mismatches:
        print("Active-version drift detected:", file=sys.stderr)
        for line in mismatches:
            print(f"  {line}", file=sys.stderr)
        print(
            "\nResolve by either (a) running `python agents/update_prompts.py` "
            "to redeploy + re-pin, (b) running `python bin/rollback-agent.py "
            "<agent> --to-version <pinned>` to restore the pinned version, or "
            "(c) editing agents/active_versions.json if the live state is "
            "correct.",
            file=sys.stderr,
        )
        return 1
    print("All agents match the pin file.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
