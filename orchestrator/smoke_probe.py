"""Pre-deploy smoke probe (Plan #42 PR2; Plan #44 Task #20 multiagent ext).

Why this exists
---------------
``main.py`` runs this BEFORE binding the health server's ``/ready`` route, so
Railway's healthcheck holds the previous image when the new container can't
prove it is fundamentally healthy. The probe runs trivial checks against the
live dependencies (build commit, Salesforce MCP path, Quick Answer agent,
and — at ``full`` level — a Coordinator multiagent turn), inside a 90s
wall-clock budget. Total cost is ~$0.01 at ``quick`` / ~$0.05 at ``full``.

Probe levels (Plan #44 Task #20)
--------------------------------
``SMOKE_PROBE_LEVEL`` (env var, or Bundle E Postgres-backed ``/flag`` override):

  ``off``   — every check skipped; ``passed=True`` immediately. Used during
              incident debugging when the probe itself is suspect.
  ``quick`` — the original Plan #42 PR2 probe: Checks A, B, C. **Default**.
  ``full``  — A, B, C, AND Check D. Use when you suspect Coordinator
              multiagent pin staleness or sub-agent prompt regressions.

The Postgres-backed override (``flag_overrides.get_flag``) is consulted first
so an operator can flip the level from Slack at 2am without a Railway
redeploy. If ``DATABASE_URL`` is unset the lookup falls back to the env var.

Four checks
-----------
A. ``build_commit_match`` — confirm the running process is the build the
   pipeline thinks it is. Reads ``BUILD_COMMIT`` from the environment (set at
   container build time per ``Dockerfile``) and compares it to the active
   versions file. In ``--local`` mode missing ``BUILD_COMMIT`` is a WARN, not
   a FAIL — laptop runs do not carry the env var.

B. ``dump_sf_query`` — runs the canonical production SF code path
   (``orchestrator.sf_dump_tool.dump_sf_query``) against
   ``SELECT Id FROM Account LIMIT 1``. Failure means MCP vault unreachable,
   credentials wrong, or the SF org is down.

C. ``quick_answer_agent`` — opens a fresh single-turn Anthropic session
   against the Quick Answer agent and asks it to echo a sentinel token. Failure
   means the Anthropic session/agent pipeline can't even render one trivial
   response. A 429 or 5xx from Anthropic flips ``anthropic_status`` to
   ``rate_limited`` / ``unavailable`` and the probe returns ``passed=True`` —
   plan #42 decision D7: a real Anthropic outage degrades BOTH the previous
   image and the new image, so blocking the deploy would prevent the fix from
   landing during the outage.

D. ``coordinator_multiagent`` (``full`` level only) — opens a fresh
   single-turn session against the Coordinator and asks it to confirm
   specialist access. Validates that multiagent pins, sub-agent prompts, and
   the Coordinator's routing prompt all survived the deploy. 30s budget; same
   429/5xx inconclusive-PASS treatment as Check C.

Outcomes
--------
- PASS — every requested check returned ok. Admin DM goes out so operators
  see the streak counter from the daily digest.
- INCONCLUSIVE-PASS — Check C or D hit a 429/503; admin DM warns the deploy
  went through with a degraded Anthropic dependency.
- FAIL — at least one check failed. The orchestrator leaves ``_READY`` set to
  False; ``/ready`` returns 503; Railway's healthcheckTimeout fires and the
  deployment is marked failed, holding the previous image. Admin DM lists the
  exact failing check.
- DISABLED — emitted by ``main.py`` (NOT this module) when
  ``SMOKE_PROBE_ENABLED=false``. The DM template here is reused. Distinct
  from ``SMOKE_PROBE_LEVEL=off``, which still records a row (passed=True,
  ``reason=probe_disabled_via_level``).

CLI surface
-----------
``python -m orchestrator.smoke_probe [--local] [--check build|sf|agent|coord|all]``

In ``--local`` mode missing creds degrade gracefully: Check A allows a missing
``BUILD_COMMIT`` as a WARN, Checks B / C / D still need real creds (no
silent skip — the operator should see the same failure the prod probe sees).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional


log = logging.getLogger(__name__)


# Wall-clock budget for the whole probe. At ``quick`` level this is the 60s
# of Plan #42; at ``full`` it stretches to 90s so Check D (~30s) fits without
# crowding A/B/C. Each individual check also has its own bounded deadline so
# a single slow check can't starve the others.
TOTAL_BUDGET_SECONDS = 90.0

# Per-check budgets. The numbers add up to less than the total budget so
# overhead (DB INSERT, admin DM render) has headroom.
CHECK_B_BUDGET_SECONDS = 25.0
CHECK_C_BUDGET_SECONDS = 25.0
# Plan #44 Task #20: Check D (Coordinator multiagent) gets 30s — slightly
# more than C because a Coordinator turn can do extra routing work even when
# the prompt explicitly asks it not to delegate.
CHECK_D_BUDGET_SECONDS = 30.0

# Sentinel token Check C asks the Quick Answer agent to echo. Deliberately
# token-cheap and unambiguous — if it shows up verbatim we know the agent
# resolved a fresh session against our environment.
_SMOKE_SENTINEL = "smoke-probe-ok"

# Sentinel token Check D asks the Coordinator to echo. A different token from
# Check C so a misrouted Quick Answer response can't accidentally pass Check
# D's check.
_SMOKE_SENTINEL_MULTIAGENT = "multiagent-ok"

# Allowed values for ``SMOKE_PROBE_LEVEL``. Matches the whitelist in
# ``slack_bot.FLAG_ALLOWED`` so a ``/flag`` write and the smoke probe agree
# on the vocabulary.
_VALID_PROBE_LEVELS = ("off", "quick", "full")
_DEFAULT_PROBE_LEVEL = "quick"

# Workspace defaults — keep aligned with sf_dump_tool's "real-thing-or-error"
# stance. Check B always runs against the Acme portco; multi-portco
# support is out of scope for the smoke probe (each portco's MCP path is the
# same code).
_SMOKE_PORTCO_KEY = "acme"

# Repo-relative path to the pin file. Same logic as main.py / config.py.
_ACTIVE_VERSIONS_PATH = Path(__file__).parent.parent / "agents" / "active_versions.json"


# ──────────────────────────────────────────────────────────────────────────
# Result dataclass
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class SmokeResult:
    """Outcome of one probe invocation.

    ``passed`` is ``True`` for both PASS and INCONCLUSIVE-PASS — the
    distinction lives in ``anthropic_status``. The orchestrator uses
    ``passed`` directly to gate ``_READY``.

    ``check_d_ok`` is populated only when ``SMOKE_PROBE_LEVEL=full``
    (Plan #44 Task #20). At the ``quick`` and ``off`` levels it stays
    ``None`` so the smoke_probe_runs row reflects "Check D not requested".
    """

    passed: bool
    reason: str
    elapsed_s: float
    check_results: dict[str, dict]
    anthropic_status: Literal["ok", "rate_limited", "unavailable"] = "ok"
    deploy_sha: str = ""
    # Per-check OK booleans for the DB row. ``None`` means the check was
    # skipped (e.g. ``--check sf`` only runs Check B, or Check D only runs
    # at ``SMOKE_PROBE_LEVEL=full``).
    check_a_ok: Optional[bool] = None
    check_b_ok: Optional[bool] = None
    check_c_ok: Optional[bool] = None
    check_d_ok: Optional[bool] = None
    # Probe level that produced this run. Tracked separately from the
    # check booleans so the daily digest can group by level.
    probe_level: str = _DEFAULT_PROBE_LEVEL

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "reason": self.reason,
            "elapsed_s": round(self.elapsed_s, 2),
            "check_results": self.check_results,
            "anthropic_status": self.anthropic_status,
            "deploy_sha": self.deploy_sha,
            "check_a_ok": self.check_a_ok,
            "check_b_ok": self.check_b_ok,
            "check_c_ok": self.check_c_ok,
            "check_d_ok": self.check_d_ok,
            "probe_level": self.probe_level,
        }


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────


def _short_sha(sha: str) -> str:
    """Render an 8-char SHA prefix — long enough to be unambiguous in DMs."""
    return (sha or "unknown")[:8]


def _read_active_versions_commit() -> str:
    """Return the ``build_commit`` recorded in ``agents/active_versions.json``.

    The file is optional — some deploys (pre-Plan #41) don't have it. Return
    an empty string in that case so the caller decides what to do.
    """
    try:
        if _ACTIVE_VERSIONS_PATH.exists():
            with open(_ACTIVE_VERSIONS_PATH, "r") as fh:
                data = json.load(fh)
                if isinstance(data, dict):
                    return str(data.get("build_commit") or "")
    except Exception:
        log.exception("smoke_probe: failed to read active_versions.json")
    return ""


def _resolve_probe_level() -> str:
    """Resolve the active ``SMOKE_PROBE_LEVEL`` for this run.

    Resolution order (Plan #44 Task #20, decision row #25 / Bundle E):
      1. ``flag_overrides.get_flag('SMOKE_PROBE_LEVEL', 'quick')`` — Postgres
         override; the slash-command path writes here. Survives the next
         redeploy, which is exactly the case where the operator needs the
         flip to stick.
      2. Plain env var ``SMOKE_PROBE_LEVEL`` — fallback when
         ``DATABASE_URL`` is unset (local dev, CI).
      3. ``'quick'`` — the in-code default.

    Invalid values default to ``'quick'`` with a warning log. Same behaviour
    as the ``/flag`` validator (``slack_bot._normalize_enum_smoke_probe``)
    rejects unknown enums, so a value reaching this function should only be
    invalid if an operator wrote one out-of-band by ``psql`` or set the env
    var directly.
    """
    raw: Optional[str] = None
    try:
        # Lazy import — ``flag_overrides`` pulls ``psycopg2`` and we want
        # the local-dev path (no Postgres) to keep working even when the
        # module fails to import.
        from flag_overrides import get_flag

        raw = get_flag("SMOKE_PROBE_LEVEL", _DEFAULT_PROBE_LEVEL)
    except Exception:
        raw = os.environ.get("SMOKE_PROBE_LEVEL", _DEFAULT_PROBE_LEVEL)

    val = (raw or _DEFAULT_PROBE_LEVEL).strip().lower()
    if val in _VALID_PROBE_LEVELS:
        return val
    log.warning(
        "smoke_probe: invalid SMOKE_PROBE_LEVEL=%r — defaulting to %r",
        raw,
        _DEFAULT_PROBE_LEVEL,
    )
    return _DEFAULT_PROBE_LEVEL


def _classify_anthropic_exception(exc: BaseException) -> Optional[str]:
    """Return ``'rate_limited' | 'unavailable'`` for transient API failures.

    Inspects the exception type AND ``status_code`` attribute so a generic
    ``APIStatusError`` carrying 503 is classified the same way as a typed
    ``InternalServerError``. Returns ``None`` for everything else — the
    caller treats those as hard failures of Check C.
    """
    try:
        import anthropic

        if isinstance(exc, getattr(anthropic, "RateLimitError", ())):
            return "rate_limited"
        if isinstance(exc, getattr(anthropic, "InternalServerError", ())):
            return "unavailable"
    except Exception:
        pass

    status = getattr(exc, "status_code", None)
    if status == 429:
        return "rate_limited"
    if isinstance(status, int) and 500 <= status < 600:
        return "unavailable"
    return None


# ──────────────────────────────────────────────────────────────────────────
# Check A — build commit match
# ──────────────────────────────────────────────────────────────────────────


def _check_build_commit(*, local_mode: bool) -> dict:
    """Compare ``BUILD_COMMIT`` env var against ``agents/active_versions.json``.

    Returns ``{ok, detail, env_commit, file_commit}``. In ``--local`` mode a
    missing ``BUILD_COMMIT`` env var produces ``ok=True`` with a WARN detail
    — laptop debugging shouldn't fail Check A just because the env wasn't set.

    Production semantics:
      * No env var → FAIL (the Dockerfile sets one).
      * Env var present, no pin file → PASS (Plan #41 hasn't shipped pins for
        every agent yet; the pin file is optional today).
      * Env var present, pin file has no ``build_commit`` key → PASS for the
        same reason.
      * Both present and equal → PASS.
      * Both present and different → FAIL.
    """
    env_commit = os.environ.get("BUILD_COMMIT", "")
    file_commit = _read_active_versions_commit()

    if not env_commit:
        if local_mode:
            return {
                "ok": True,
                "detail": "WARN: BUILD_COMMIT unset (local mode)",
                "env_commit": "",
                "file_commit": file_commit,
            }
        return {
            "ok": False,
            "detail": "BUILD_COMMIT env var unset — Dockerfile ARG not propagated",
            "env_commit": "",
            "file_commit": file_commit,
        }

    if not file_commit:
        return {
            "ok": True,
            "detail": (
                f"{_short_sha(env_commit)} (pin file has no build_commit yet — "
                "Plan #41 work-in-progress)"
            ),
            "env_commit": env_commit,
            "file_commit": "",
        }

    if env_commit == file_commit:
        return {
            "ok": True,
            "detail": f"{_short_sha(env_commit)} == BUILD_COMMIT",
            "env_commit": env_commit,
            "file_commit": file_commit,
        }

    return {
        "ok": False,
        "detail": (
            f"build_commit mismatch: env={_short_sha(env_commit)} "
            f"file={_short_sha(file_commit)}"
        ),
        "env_commit": env_commit,
        "file_commit": file_commit,
    }


# ──────────────────────────────────────────────────────────────────────────
# Check B — dump_sf_query against a one-row SOQL
# ──────────────────────────────────────────────────────────────────────────


def _check_dump_sf_query(*, portco_key: str = _SMOKE_PORTCO_KEY) -> dict:
    """Run the canonical SF data-pull path with a trivial single-row SOQL.

    Failure modes covered:
      * SF auth (bad creds, expired token).
      * MCP vault unreachable.
      * Disk failure during Parquet write (the production path materializes
        results to ``SESSION_OUTPUT_DIR``).

    Returns ``{ok, detail, count, elapsed_s, error}``. Never raises — every
    failure path classifies into a returned dict so the outer ``run_smoke_probe``
    can format the admin DM.
    """
    started = time.monotonic()
    try:
        from sf_dump_tool import dump_sf_query

        # SESSION_OUTPUT_DIR may not be writable in dev; coerce to a temp
        # path the test harness controls. In prod Railway mounts a real
        # directory and the override is unset.
        result = dump_sf_query(
            "SELECT Id FROM Account LIMIT 1",
            portco_key=portco_key,
            label="smoke_probe_check_b",
        )
    except Exception as exc:
        elapsed = time.monotonic() - started
        return {
            "ok": False,
            "detail": f"sf_query exception: {exc}",
            "count": 0,
            "elapsed_s": round(elapsed, 2),
            "error": traceback.format_exc(limit=2).strip().splitlines()[-1]
            if traceback.format_exc()
            else str(exc),
        }

    elapsed = time.monotonic() - started

    # dump_sf_query never raises; success = ``file_path`` populated AND
    # ``error`` absent. Zero-row results from a healthy SF org are also
    # fine for Check B's purposes (the org might be empty in dev), but for
    # the canonical Account-LIMIT-1 query we expect ≥1 row. Treat 0 rows as
    # OK because the org may legitimately have no Accounts visible.
    err = (result or {}).get("error")
    if err:
        return {
            "ok": False,
            "detail": f"sf_query returned error: {err}",
            "count": 0,
            "elapsed_s": round(elapsed, 2),
            "error": err,
        }

    count = (result or {}).get("count", 0) or 0
    return {
        "ok": True,
        "detail": f"{count} Account row(s) in {elapsed:.1f}s",
        "count": count,
        "elapsed_s": round(elapsed, 2),
        "error": "",
    }


# ──────────────────────────────────────────────────────────────────────────
# Check C — Quick Answer single-turn session
# ──────────────────────────────────────────────────────────────────────────


def _check_quick_answer_agent(
    *,
    timeout_seconds: float = CHECK_C_BUDGET_SECONDS,
) -> dict:
    """Open a fresh single-turn session against the Quick Answer agent.

    Returns ``{ok, detail, response, anthropic_status, elapsed_s, error}``.
    ``anthropic_status`` is ``'ok' | 'rate_limited' | 'unavailable'`` — the
    outer probe lifts that into the SmokeResult for the inconclusive-PASS
    path (plan #42 decision D7).
    """
    started = time.monotonic()
    try:
        import anthropic

        import config as _config
    except Exception as exc:
        return {
            "ok": False,
            "detail": f"config import failed: {exc}",
            "response": "",
            "anthropic_status": "ok",
            "elapsed_s": round(time.monotonic() - started, 2),
            "error": str(exc),
        }

    quick_id = getattr(_config, "QUICK_AGENT_ID", "")
    if not quick_id:
        return {
            "ok": False,
            "detail": "QUICK_AGENT_ID unset — Quick Answer agent not provisioned",
            "response": "",
            "anthropic_status": "ok",
            "elapsed_s": round(time.monotonic() - started, 2),
            "error": "missing_quick_agent_id",
        }

    try:
        client = anthropic.Anthropic(api_key=_config.ANTHROPIC_API_KEY)
    except Exception as exc:
        return {
            "ok": False,
            "detail": f"Anthropic client init failed: {exc}",
            "response": "",
            "anthropic_status": "ok",
            "elapsed_s": round(time.monotonic() - started, 2),
            "error": str(exc),
        }

    # Vault IDs MUST be attached to the session — the Quick Answer agent's
    # Salesforce MCP toolset needs credentials from the vault to initialize.
    # Without vault_ids the session emits ``session.error`` of type
    # ``mcp_authentication_failed_error`` with the message "no credential is
    # stored for this server URL", the stream closes, ``text_parts`` stays
    # empty, and Check C reports the misleading ``sentinel_missing got: ''``.
    # Production ``session_runner.py`` passes ``vault_ids=VAULT_IDS`` on every
    # ``sessions.create``; the smoke probe shipped without this. Falls back
    # to ``[]`` for the test path that stubs ``session_runner`` to avoid the
    # psycopg2 import.
    try:
        from session_runner import VAULT_IDS as _VAULT_IDS

        vault_ids = list(_VAULT_IDS)
    except Exception:
        vault_ids = [
            v
            for v in (
                getattr(_config, "ACME_VAULT_ID", "") or "",
                getattr(_config, "SLACK_VAULT_ID", "") or "",
            )
            if v
        ]

    try:
        session = client.beta.sessions.create(
            agent=quick_id,
            environment_id=_config.ENVIRONMENT_ID,
            title="smoke-probe-check-c",
            vault_ids=vault_ids,
        )
    except Exception as exc:
        status = _classify_anthropic_exception(exc)
        if status:
            return {
                "ok": False,
                "detail": f"Anthropic session create failed ({status}): {exc}",
                "response": "",
                "anthropic_status": status,
                "elapsed_s": round(time.monotonic() - started, 2),
                "error": str(exc),
            }
        return {
            "ok": False,
            "detail": f"session_create_failed: {exc}",
            "response": "",
            "anthropic_status": "ok",
            "elapsed_s": round(time.monotonic() - started, 2),
            "error": str(exc),
        }

    session_id = getattr(session, "id", None) or ""
    deadline = time.monotonic() + timeout_seconds
    text_parts: list[str] = []
    anthropic_status: Literal["ok", "rate_limited", "unavailable"] = "ok"
    timed_out = False
    # Capture the most recent session.error so we can surface the actual
    # failure mode (e.g. ``mcp_authentication_failed_error``) instead of the
    # misleading ``sentinel_missing got: ''`` when the session dies before
    # the agent emits any text.
    session_error_type: str = ""
    session_error_message: str = ""

    user_text = f"Please respond with the word '{_SMOKE_SENTINEL}' and nothing else."

    try:
        with client.beta.sessions.events.stream(session_id=session_id) as stream:
            client.beta.sessions.events.send(
                session_id=session_id,
                events=[
                    {
                        "type": "user.message",
                        "content": [{"type": "text", "text": user_text}],
                    }
                ],
            )

            for event in stream:
                if time.monotonic() > deadline:
                    timed_out = True
                    break

                etype = getattr(event, "type", "")
                if etype == "agent.message":
                    for block in getattr(event, "content", []) or []:
                        text = getattr(block, "text", None)
                        if text:
                            text_parts.append(text)
                elif etype == "session.status_idle":
                    break
                elif etype == "session.error":
                    # Capture the error_type + message so the admin DM
                    # surfaces the actual failure (e.g.
                    # ``mcp_authentication_failed_error``) rather than the
                    # downstream ``sentinel_missing`` symptom.
                    err = getattr(event, "error", None)
                    session_error_type = (getattr(err, "type", "") if err else "") or ""
                    session_error_message = (
                        getattr(err, "message", "") if err else ""
                    ) or ""
                    break
                elif etype == "session.status_terminated":
                    break
    except Exception as exc:
        status = _classify_anthropic_exception(exc)
        if status:
            return {
                "ok": True,  # inconclusive PASS — see plan #42 D7
                "detail": (
                    f"Anthropic {status} during streaming — "
                    "treated as inconclusive PASS"
                ),
                "response": "".join(text_parts),
                "anthropic_status": status,
                "elapsed_s": round(time.monotonic() - started, 2),
                "error": str(exc),
            }
        return {
            "ok": False,
            "detail": f"stream_failed: {exc}",
            "response": "",
            "anthropic_status": "ok",
            "elapsed_s": round(time.monotonic() - started, 2),
            "error": str(exc),
        }

    elapsed = time.monotonic() - started
    raw = "".join(text_parts).strip()

    if timed_out:
        return {
            "ok": False,
            "detail": f"Quick Answer timeout after {timeout_seconds:.0f}s",
            "response": raw,
            "anthropic_status": anthropic_status,
            "elapsed_s": round(elapsed, 2),
            "error": "timeout",
        }

    # Surface session.error before sentinel checks — a session that died
    # before emitting any text is a session.error case, not a "missing
    # sentinel" case. The admin DM template includes the captured type +
    # message so the operator sees the real root cause.
    if session_error_type:
        return {
            "ok": False,
            "detail": (
                f"Quick Answer session.error: {session_error_type} — "
                f"{session_error_message[:200]}"
            ),
            "response": raw,
            "anthropic_status": anthropic_status,
            "elapsed_s": round(elapsed, 2),
            "error": session_error_type,
        }

    if _SMOKE_SENTINEL not in raw.lower():
        return {
            "ok": False,
            "detail": (f"Quick Answer response missing sentinel (got: {raw[:120]!r})"),
            "response": raw,
            "anthropic_status": anthropic_status,
            "elapsed_s": round(elapsed, 2),
            "error": "sentinel_missing",
        }

    # Cheap rough token count from char length; the admin DM template wants
    # a single number, not a per-block breakdown.
    approx_tokens = max(1, int(len(raw) / 4))

    return {
        "ok": True,
        "detail": f"{approx_tokens} tokens, {elapsed:.1f}s",
        "response": raw,
        "anthropic_status": anthropic_status,
        "elapsed_s": round(elapsed, 2),
        "error": "",
    }


# ──────────────────────────────────────────────────────────────────────────
# Check D — Coordinator multiagent single-turn session (Plan #44 Task #20)
# ──────────────────────────────────────────────────────────────────────────


def _check_coordinator_multiagent(
    *,
    timeout_seconds: float = CHECK_D_BUDGET_SECONDS,
) -> dict:
    """Open a fresh single-turn session against the Coordinator.

    The Coordinator is the only agent in the roster whose ``multiagent.agents``
    pin can drift away from the live sub-agent versions (see CLAUDE.md memory
    ``feedback_multiagent_pinning`` — pins are snapshotted at parent-update
    time, so a sub-agent prompt deploy without a Coordinator re-update leaves
    the Coordinator delegating to stale versions). Check D doesn't actually
    drive a delegation — it just asks the Coordinator to confirm it has access
    to its specialists. The mere fact that the Coordinator boots, evaluates
    its tools and multiagent block, and emits a coherent ``agent.message``
    proves the multiagent wiring survived the deploy.

    Returns ``{ok, detail, response, anthropic_status, elapsed_s, error}``,
    same shape as Check C so ``_render_admin_dm`` can format both with the
    same helper.

    The prompt explicitly instructs the Coordinator NOT to delegate — a real
    delegate-then-summarize round trip would cost ~$0.30 instead of the
    target ~$0.05 for this check.
    """
    started = time.monotonic()
    try:
        import anthropic

        import config as _config
    except Exception as exc:
        return {
            "ok": False,
            "detail": f"config import failed: {exc}",
            "response": "",
            "anthropic_status": "ok",
            "elapsed_s": round(time.monotonic() - started, 2),
            "error": str(exc),
        }

    coord_id = getattr(_config, "COORDINATOR_ID", "")
    if not coord_id:
        return {
            "ok": False,
            "detail": "COORDINATOR_ID unset — Coordinator agent not provisioned",
            "response": "",
            "anthropic_status": "ok",
            "elapsed_s": round(time.monotonic() - started, 2),
            "error": "missing_coordinator_id",
        }

    try:
        client = anthropic.Anthropic(api_key=_config.ANTHROPIC_API_KEY)
    except Exception as exc:
        return {
            "ok": False,
            "detail": f"Anthropic client init failed: {exc}",
            "response": "",
            "anthropic_status": "ok",
            "elapsed_s": round(time.monotonic() - started, 2),
            "error": str(exc),
        }

    # Prefer the pinned-version form of the agent param so Check D exercises
    # the same code path production callers use. If ``_resolve_agent_param``
    # is unreachable (circular imports, test envs) fall back to the bare ID
    # — Anthropic resolves to the latest version in that case.
    try:
        from session_runner import _resolve_agent_param

        agent_param = _resolve_agent_param(coord_id)
    except Exception:
        agent_param = coord_id

    # See Check C for the rationale — same vault attachment is required for
    # the Coordinator's Salesforce MCP toolset.
    try:
        from session_runner import VAULT_IDS as _VAULT_IDS

        vault_ids = list(_VAULT_IDS)
    except Exception:
        vault_ids = [
            v
            for v in (
                getattr(_config, "ACME_VAULT_ID", "") or "",
                getattr(_config, "SLACK_VAULT_ID", "") or "",
            )
            if v
        ]

    try:
        session = client.beta.sessions.create(
            agent=agent_param,
            environment_id=_config.ENVIRONMENT_ID,
            title="smoke-probe-check-d",
            vault_ids=vault_ids,
        )
    except Exception as exc:
        status = _classify_anthropic_exception(exc)
        if status:
            return {
                "ok": True,  # inconclusive PASS — see plan #42 D7
                "detail": (
                    f"Anthropic {status} during session create — "
                    "treated as inconclusive PASS"
                ),
                "response": "",
                "anthropic_status": status,
                "elapsed_s": round(time.monotonic() - started, 2),
                "error": str(exc),
            }
        return {
            "ok": False,
            "detail": f"session_create_failed: {exc}",
            "response": "",
            "anthropic_status": "ok",
            "elapsed_s": round(time.monotonic() - started, 2),
            "error": str(exc),
        }

    session_id = getattr(session, "id", None) or ""
    deadline = time.monotonic() + timeout_seconds
    text_parts: list[str] = []
    anthropic_status: Literal["ok", "rate_limited", "unavailable"] = "ok"
    timed_out = False
    # Same session.error capture as Check C — surface the real error_type
    # instead of the downstream ``sentinel_missing`` symptom.
    session_error_type: str = ""
    session_error_message: str = ""

    user_text = (
        "Smoke probe: please confirm you have access to your specialist "
        f"agents. Reply with the word '{_SMOKE_SENTINEL_MULTIAGENT}' and "
        "nothing else. Do NOT delegate; just answer directly."
    )

    try:
        with client.beta.sessions.events.stream(session_id=session_id) as stream:
            client.beta.sessions.events.send(
                session_id=session_id,
                events=[
                    {
                        "type": "user.message",
                        "content": [{"type": "text", "text": user_text}],
                    }
                ],
            )

            for event in stream:
                if time.monotonic() > deadline:
                    timed_out = True
                    break

                etype = getattr(event, "type", "")
                if etype == "agent.message":
                    for block in getattr(event, "content", []) or []:
                        text = getattr(block, "text", None)
                        if text:
                            text_parts.append(text)
                elif etype == "session.status_idle":
                    break
                elif etype == "session.error":
                    err = getattr(event, "error", None)
                    session_error_type = (getattr(err, "type", "") if err else "") or ""
                    session_error_message = (
                        getattr(err, "message", "") if err else ""
                    ) or ""
                    break
                elif etype == "session.status_terminated":
                    break
    except Exception as exc:
        status = _classify_anthropic_exception(exc)
        if status:
            return {
                "ok": True,  # inconclusive PASS — same treatment as Check C
                "detail": (
                    f"Anthropic {status} during streaming — "
                    "treated as inconclusive PASS"
                ),
                "response": "".join(text_parts),
                "anthropic_status": status,
                "elapsed_s": round(time.monotonic() - started, 2),
                "error": str(exc),
            }
        return {
            "ok": False,
            "detail": f"stream_failed: {exc}",
            "response": "",
            "anthropic_status": "ok",
            "elapsed_s": round(time.monotonic() - started, 2),
            "error": str(exc),
        }

    elapsed = time.monotonic() - started
    raw = "".join(text_parts).strip()

    if timed_out:
        return {
            "ok": False,
            "detail": f"Coordinator timeout after {timeout_seconds:.0f}s",
            "response": raw,
            "anthropic_status": anthropic_status,
            "elapsed_s": round(elapsed, 2),
            "error": "timeout",
        }

    if session_error_type:
        return {
            "ok": False,
            "detail": (
                f"Coordinator session.error: {session_error_type} — "
                f"{session_error_message[:200]}"
            ),
            "response": raw,
            "anthropic_status": anthropic_status,
            "elapsed_s": round(elapsed, 2),
            "error": session_error_type,
        }

    if _SMOKE_SENTINEL_MULTIAGENT not in raw.lower():
        return {
            "ok": False,
            "detail": (
                f"Coordinator response missing multiagent sentinel (got: {raw[:120]!r})"
            ),
            "response": raw,
            "anthropic_status": anthropic_status,
            "elapsed_s": round(elapsed, 2),
            "error": "sentinel_missing",
        }

    approx_tokens = max(1, int(len(raw) / 4))

    return {
        "ok": True,
        "detail": f"{approx_tokens} tokens, {elapsed:.1f}s",
        "response": raw,
        "anthropic_status": anthropic_status,
        "elapsed_s": round(elapsed, 2),
        "error": "",
    }


# ──────────────────────────────────────────────────────────────────────────
# Persistence + admin DM
# ──────────────────────────────────────────────────────────────────────────


def _persist_result(result: SmokeResult) -> None:
    """Insert one row into ``smoke_probe_runs``. Silent no-op without DB.

    Idempotency note: the same container can crash and restart, and we
    intentionally log both attempts. No unique constraint on
    ``(deploy_sha, started_at)`` because ``started_at`` is per-row.
    """
    try:
        import db_adapter
    except Exception:
        log.exception("smoke_probe: db_adapter import failed; not persisting row")
        return

    if not getattr(db_adapter, "DATABASE_URL", ""):
        log.info("smoke_probe: DATABASE_URL unset; skipping smoke_probe_runs insert")
        return

    try:
        conn = db_adapter._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO smoke_probe_runs (
                        deploy_sha, passed, check_a_ok, check_b_ok, check_c_ok,
                        check_d_ok, anthropic_status, elapsed_s, reason
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        result.deploy_sha or None,
                        result.passed,
                        result.check_a_ok,
                        result.check_b_ok,
                        result.check_c_ok,
                        result.check_d_ok,
                        result.anthropic_status,
                        result.elapsed_s,
                        result.reason or None,
                    ),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        log.exception("smoke_probe: failed to insert smoke_probe_runs row")


def _render_admin_dm(result: SmokeResult, *, state: str) -> tuple[str, str]:
    """Render the (summary, detail) admin DM payload for ``send_notification``.

    ``state`` is ``'pass' | 'fail' | 'inconclusive'``. The DISABLED state is
    rendered by ``main.py`` directly because this module never runs in that
    case.
    """
    deploy = _short_sha(result.deploy_sha)
    check_a = result.check_results.get("build_commit", {})
    check_b = result.check_results.get("dump_sf_query", {})
    check_c = result.check_results.get("quick_answer", {})
    check_d = result.check_results.get("coordinator_multiagent", {})
    rollback_hint = f"python bin/rollback-deploy.py --artifact-run {os.environ.get('GH_RUN_ID', '<gh_run_id>')}"

    def _line(ok_glyph: str, name: str, detail: str) -> str:
        return f"  {ok_glyph} {name:24s} {detail}"

    if state == "pass":
        summary = (
            f"[SMOKE PROBE OK] deploy `{deploy}` | level={result.probe_level} "
            f"| {result.elapsed_s:.0f}s"
        )
        lines = []
        if check_a:
            lines.append(_line("✓", "Check A (commit):", check_a.get("detail", "")))
        if check_b:
            lines.append(_line("✓", "Check B (dump_sf):", check_b.get("detail", "")))
        if check_c:
            lines.append(_line("✓", "Check C (quick_ans):", check_c.get("detail", "")))
        if check_d:
            lines.append(_line("✓", "Check D (coord_ma):", check_d.get("detail", "")))
        lines.append(f"  Rollback if needed: `{rollback_hint}`")
        return summary, "\n".join(lines)

    if state == "inconclusive":
        summary = (
            f"[SMOKE PROBE INCONCLUSIVE — DEPLOY ALLOWED] `{deploy}` "
            f"| level={result.probe_level}"
        )
        lines = []
        if check_a:
            glyph = "✓" if check_a.get("ok") else "✗"
            lines.append(_line(glyph, "Check A (commit):", check_a.get("detail", "")))
        if check_b:
            glyph = "✓" if check_b.get("ok") else "✗"
            lines.append(_line(glyph, "Check B (dump_sf):", check_b.get("detail", "")))
        if check_c:
            glyph = (
                "⚠"
                if (check_c.get("anthropic_status") or "ok") != "ok"
                else ("✓" if check_c.get("ok") else "✗")
            )
            lines.append(
                _line(glyph, "Check C (quick_ans):", check_c.get("detail", ""))
            )
        if check_d:
            glyph = (
                "⚠"
                if (check_d.get("anthropic_status") or "ok") != "ok"
                else ("✓" if check_d.get("ok") else "✗")
            )
            lines.append(_line(glyph, "Check D (coord_ma):", check_d.get("detail", "")))
        lines.append("")
        lines.append(
            "Customer impact:        Possible — Anthropic API is degraded; "
            "new image promoted anyway"
        )
        lines.append(
            "Why allowed:            Failing probe on Anthropic outage would "
            "prevent fixes from landing during the outage"
        )
        lines.append("Watch:                  https://status.anthropic.com/")
        lines.append(f"Rollback if needed:     `{rollback_hint}`")
        return summary, "\n".join(lines)

    # FAIL
    summary = f"[SMOKE PROBE FAILED] deploy `{deploy}` | level={result.probe_level}"
    lines = []
    for label, key in (
        ("Check A (commit):", "build_commit"),
        ("Check B (dump_sf):", "dump_sf_query"),
        ("Check C (quick_ans):", "quick_answer"),
        ("Check D (coord_ma):", "coordinator_multiagent"),
    ):
        cr = result.check_results.get(key)
        if cr is None:
            # SKIPPED for levels that didn't request the check (e.g. Check D
            # at quick). Don't list it at all so the DM stays compact.
            continue
        glyph = "✓" if cr.get("ok") else "✗"
        lines.append(_line(glyph, label, cr.get("detail", "")))
        err = cr.get("error")
        if err and not cr.get("ok"):
            lines.append(f"       Exception:         {err}")
    lines.append("")
    lines.append(
        "Customer impact:        NONE — previous image still serving Slack traffic"
    )
    lines.append("Railway deployment:     FAILED (held previous image)")
    lines.append("")
    lines.append("Next actions:")
    lines.append(
        "  1. Debug locally:       `python -m orchestrator.smoke_probe --local --check all`"
    )
    lines.append(
        "  2. Disable safety net:  Railway → Variables → prod → SMOKE_PROBE_ENABLED=false"
    )
    lines.append("  3. Runbook:             docs/runbook-smoke-probe.md")
    lines.append(
        "  4. /ready state:        "
        '{"ready": false, "reason": "smoke_probe_failed", '
        '"check_results": ...}'
    )
    lines.append(f"  5. Rollback if needed:  `{rollback_hint}`")
    return summary, "\n".join(lines)


def render_disabled_dm() -> tuple[str, str]:
    """Build the SMOKE_PROBE_ENABLED=false admin DM.

    Lives here so ``main.py`` does not have to duplicate the template — it
    calls ``smoke_probe.render_disabled_dm()`` when the env var is false.
    """
    deploy = _short_sha(os.environ.get("BUILD_COMMIT", "unknown"))
    rollback_hint = (
        f"python bin/rollback-deploy.py --artifact-run "
        f"{os.environ.get('GH_RUN_ID', '<gh_run_id>')}"
    )
    summary = f"[WARN] SMOKE_PROBE_ENABLED=false — deploy `{deploy}` NOT validated"
    detail = "\n".join(
        [
            f"  Build:                  {deploy}",
            "  Customer impact:        Possible — no safety net ran",
            "  Re-enable:              Railway → Variables → prod → "
            "SMOKE_PROBE_ENABLED=true",
            f"  Rollback if needed:     `{rollback_hint}`",
            "  Runbook:                docs/runbook-smoke-probe.md",
        ]
    )
    return summary, detail


def _send_admin_dm(state: str, result: SmokeResult) -> None:
    """Best-effort admin DM. Never raises into the caller."""
    try:
        from slack_bot import send_notification

        summary, detail = _render_admin_dm(result, state=state)
        severity = "info" if state in ("pass",) else "watch"
        send_notification(severity, summary, detail, admin_only=True)
    except Exception:
        log.exception("smoke_probe: admin DM render/send failed")


# ──────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────


def run_smoke_probe(
    *,
    local_mode: bool = False,
    checks: Optional[tuple[str, ...]] = None,
    persist: bool = True,
    send_dm: bool = True,
    level: Optional[str] = None,
) -> SmokeResult:
    """Run the smoke probe and return a ``SmokeResult``.

    Args:
        local_mode: When True, missing ``BUILD_COMMIT`` is a WARN in Check A
            (not a FAIL) and the row is still persisted to Postgres if
            ``DATABASE_URL`` is set.
        checks: Subset of ``("build", "sf", "agent", "coord")`` to run.
            When ``None`` (production path), the selection is derived from
            ``level`` — ``quick`` → A+B+C, ``full`` → A+B+C+D, ``off`` →
            nothing. Explicit ``checks`` always wins over the level so a
            CLI subset still works (Plan #44 Task #20).
        persist: When False, skip the smoke_probe_runs INSERT. Tests set this
            False to avoid pulling psycopg2 into mock environments.
        send_dm: When False, skip the admin DM. Tests set this False.
        level: Override for ``SMOKE_PROBE_LEVEL``. When ``None`` the level is
            resolved from ``flag_overrides.get_flag`` (with env-var fallback).
            Pass an explicit value for tests / CLI use.

    Never raises — every failure surface is captured in the returned result.
    """
    started = time.monotonic()
    deploy_sha = os.environ.get("BUILD_COMMIT", "")
    check_results: dict[str, dict] = {}
    check_a_ok: Optional[bool] = None
    check_b_ok: Optional[bool] = None
    check_c_ok: Optional[bool] = None
    check_d_ok: Optional[bool] = None
    anthropic_status: Literal["ok", "rate_limited", "unavailable"] = "ok"

    # ── Plan #44 Task #20 — resolve probe level ─────────────────────────
    if level is None:
        probe_level = _resolve_probe_level()
    else:
        cleaned = (level or "").strip().lower()
        if cleaned in _VALID_PROBE_LEVELS:
            probe_level = cleaned
        else:
            log.warning(
                "smoke_probe: invalid level=%r passed to run_smoke_probe — "
                "defaulting to %r",
                level,
                _DEFAULT_PROBE_LEVEL,
            )
            probe_level = _DEFAULT_PROBE_LEVEL

    # Honor SMOKE_PROBE_LEVEL=off — skip every check, return passed=True
    # immediately. Distinct from SMOKE_PROBE_ENABLED=false: ``off`` still
    # records a row so a Slack-issued flip is auditable.
    if probe_level == "off":
        elapsed = time.monotonic() - started
        result = SmokeResult(
            passed=True,
            reason="probe_disabled_via_level",
            elapsed_s=elapsed,
            check_results={},
            anthropic_status="ok",
            deploy_sha=deploy_sha,
            check_a_ok=None,
            check_b_ok=None,
            check_c_ok=None,
            check_d_ok=None,
            probe_level=probe_level,
        )
        log.info(
            "smoke_probe: SMOKE_PROBE_LEVEL=off — all checks skipped; deploy_sha=%s",
            _short_sha(deploy_sha),
        )
        if persist:
            _persist_result(result)
        # No admin DM for the ``off`` state — operators flipped the switch
        # deliberately and don't need a per-deploy nag. The
        # ``SMOKE_PROBE_ENABLED=false`` path keeps its dedicated WARN DM via
        # ``render_disabled_dm``.
        return result

    # Compute the requested checks if the caller didn't pin them. Default
    # selection comes from the level: quick → A+B+C, full → A+B+C+D.
    if checks is None:
        if probe_level == "full":
            checks = ("build", "sf", "agent", "coord")
        else:
            checks = ("build", "sf", "agent")

    log.info(
        "smoke_probe: starting (level=%s, local_mode=%s, checks=%s, deploy_sha=%s)",
        probe_level,
        local_mode,
        checks,
        _short_sha(deploy_sha),
    )

    if "build" in checks:
        a = _check_build_commit(local_mode=local_mode)
        check_results["build_commit"] = a
        check_a_ok = bool(a.get("ok"))
        log.info(
            "smoke_probe: Check A (build_commit) → %s — %s", check_a_ok, a.get("detail")
        )

    if "sf" in checks:
        b = _check_dump_sf_query()
        check_results["dump_sf_query"] = b
        check_b_ok = bool(b.get("ok"))
        log.info(
            "smoke_probe: Check B (dump_sf_query) → %s — %s",
            check_b_ok,
            b.get("detail"),
        )

    if "agent" in checks:
        # If Check B already failed, the runbook says Check C is SKIPPED to
        # keep the admin DM clean — running it would just compound the same
        # underlying outage. The dispatcher in main.py treats SKIPPED as
        # "did not contribute to overall status".
        if check_b_ok is False:
            check_results["quick_answer"] = {
                "ok": None,
                "detail": "SKIPPED (Check B failed)",
                "response": "",
                "anthropic_status": "ok",
                "elapsed_s": 0.0,
                "error": "skipped_due_to_check_b",
            }
            log.info("smoke_probe: Check C (quick_answer) → SKIPPED (Check B failed)")
        else:
            c = _check_quick_answer_agent()
            check_results["quick_answer"] = c
            check_c_ok = bool(c.get("ok"))
            anthropic_status = c.get("anthropic_status") or "ok"
            log.info(
                "smoke_probe: Check C (quick_answer) → %s — %s",
                check_c_ok,
                c.get("detail"),
            )

    if "coord" in checks:
        # Same SKIPPED-on-Check-B-fail policy as Check C — the Coordinator
        # also relies on SF being reachable when it eventually does real work
        # (even though this probe asks it not to delegate, a failing org is
        # a known confound).
        if check_b_ok is False:
            check_results["coordinator_multiagent"] = {
                "ok": None,
                "detail": "SKIPPED (Check B failed)",
                "response": "",
                "anthropic_status": "ok",
                "elapsed_s": 0.0,
                "error": "skipped_due_to_check_b",
            }
            log.info(
                "smoke_probe: Check D (coordinator_multiagent) → "
                "SKIPPED (Check B failed)"
            )
        else:
            d = _check_coordinator_multiagent()
            check_results["coordinator_multiagent"] = d
            check_d_ok = bool(d.get("ok"))
            # If Check C already classified an Anthropic outage, keep that
            # status — both checks reach the same API and one degraded run
            # is enough signal. If C was clean but D hit 429/503, promote
            # D's status.
            d_status = d.get("anthropic_status") or "ok"
            if d_status != "ok" and anthropic_status == "ok":
                anthropic_status = d_status
            log.info(
                "smoke_probe: Check D (coordinator_multiagent) → %s — %s",
                check_d_ok,
                d.get("detail"),
            )

    elapsed = time.monotonic() - started

    # Decision logic (Plan #42 D7, extended by Plan #44 Task #20):
    #  * All requested checks ok (including inconclusive-PASS from
    #    anthropic_status != 'ok') → passed=True.
    #  * Any required check fails outright (no Anthropic outage classification)
    #    → passed=False.
    required_results = [
        check_results.get(k)
        for k in (
            "build_commit",
            "dump_sf_query",
            "quick_answer",
            "coordinator_multiagent",
        )
        if k in check_results
    ]
    any_failed = any((r is not None and r.get("ok") is False) for r in required_results)
    passed = not any_failed
    reason = ""
    if not passed:
        failing = [
            name
            for name, key in (
                ("build", "build_commit"),
                ("sf", "dump_sf_query"),
                ("agent", "quick_answer"),
                ("coord", "coordinator_multiagent"),
            )
            if check_results.get(key, {}).get("ok") is False
        ]
        reason = f"failed_checks: {', '.join(failing)}"

    result = SmokeResult(
        passed=passed,
        reason=reason,
        elapsed_s=elapsed,
        check_results=check_results,
        anthropic_status=anthropic_status,
        deploy_sha=deploy_sha,
        check_a_ok=check_a_ok,
        check_b_ok=check_b_ok,
        check_c_ok=check_c_ok,
        check_d_ok=check_d_ok,
        probe_level=probe_level,
    )

    if persist:
        _persist_result(result)

    if send_dm:
        if not passed:
            state = "fail"
        elif anthropic_status != "ok":
            state = "inconclusive"
        else:
            state = "pass"
        _send_admin_dm(state, result)

    log.info(
        "smoke_probe: complete — passed=%s anthropic=%s elapsed=%.1fs reason=%s",
        result.passed,
        result.anthropic_status,
        result.elapsed_s,
        result.reason or "(none)",
    )
    return result


# ──────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────


def _parse_check_arg(value: str) -> Optional[tuple[str, ...]]:
    """Map ``--check`` argument → tuple of internal check names.

    Returns ``None`` for ``all`` so :func:`run_smoke_probe` can derive the
    selection from the resolved level (Plan #44 Task #20). Explicit single
    checks always run regardless of level.
    """
    value = (value or "all").lower()
    if value == "all":
        return None
    if value == "build":
        return ("build",)
    if value == "sf":
        return ("sf",)
    if value == "agent":
        return ("agent",)
    if value == "coord":
        return ("coord",)
    raise ValueError(f"unknown --check value: {value}")


def main_cli(argv: Optional[list[str]] = None) -> int:
    """Stdout-friendly CLI invoked by ``python -m orchestrator.smoke_probe``.

    Exit code: 0 if the probe passed (or PASSED inconclusively), 1 otherwise.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    parser = argparse.ArgumentParser(
        prog="python -m orchestrator.smoke_probe",
        description="Run the GTM Health Agent pre-deploy smoke probe.",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="Local laptop mode — missing BUILD_COMMIT is a WARN not a FAIL.",
    )
    parser.add_argument(
        "--check",
        default="all",
        choices=("build", "sf", "agent", "coord", "all"),
        help=(
            "Subset of checks to run. Default 'all' — A+B+C at quick level, "
            "A+B+C+D at full level."
        ),
    )
    parser.add_argument(
        "--level",
        default=None,
        choices=("off", "quick", "full"),
        help=(
            "Override SMOKE_PROBE_LEVEL for this run (Plan #44 Task #20). "
            "Default: resolve from flag_overrides / env."
        ),
    )
    parser.add_argument(
        "--no-persist",
        action="store_true",
        help="Skip the smoke_probe_runs INSERT (useful for local dry-runs).",
    )
    parser.add_argument(
        "--no-dm",
        action="store_true",
        help="Skip the admin Slack DM (useful for local dry-runs).",
    )
    args = parser.parse_args(argv)

    checks = _parse_check_arg(args.check)
    result = run_smoke_probe(
        local_mode=args.local,
        checks=checks,
        persist=not args.no_persist,
        send_dm=not args.no_dm,
        level=args.level,
    )

    print(json.dumps(result.to_dict(), indent=2, default=str))
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main_cli())
