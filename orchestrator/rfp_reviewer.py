"""RFP Reviewer (Opus 4.8) — gate that runs between draft and Slack post.

The Reviewer is a quality check the RFP Responder calls via the
``review_rfp_draft`` custom tool BEFORE its final summary message.
A fresh session is spun up against ``RFP_REVIEWER_ID``, the agent
receives the QA index inline as JSON, applies its six-check rubric
to EVERY question in the index (no sampling: citation coverage,
commitment leakage, marketing-speak density, Kapa fact verification
on every product answer, reference customer verification, flag
accuracy), and returns a verdict.

This module mirrors ``writing_agent.py`` deliberately: same lifecycle,
same failure mode (never raises — every failure path returns
``RFPReviewResult(ok=False, ...)``), same lazy env var read, same
no-memory contract. Multi-turn (the Reviewer fires a Kapa call per
product answer), but stateless across reviews.

Why pass the QA index inline instead of reading the sidecar JSON
from disk: the agent's ``/mnt/session/outputs/`` is on the agent's
ephemeral sandbox (~18min TTL per Theme B / CLAUDE.md notes), not
the orchestrator's persistent ``SESSION_OUTPUT_DIR`` volume. Passing
inline means we never depend on whether the agent's mount or the
Railway volume is the source of truth for any given session.

Public API
----------
    run_review(qa_index, feedback=None,
               timeout_seconds=600.0) -> RFPReviewResult

Failure mode
------------
Every failure (missing env var, timeout, malformed JSON, transport
error) returns ``RFPReviewResult(ok=False, ...)`` rather than
raising. The RFP Responder's prompt treats ``ok=False`` as a soft
PASS — proceed to summary with a ``[REVIEW_INCOMPLETE]`` audit tag
rather than blocking on a reviewer outage.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

import anthropic

import config as _config
from config import ANTHROPIC_API_KEY, ENVIRONMENT_ID

log = logging.getLogger(__name__)


# Read at call time so a Railway env var flip rotates the live agent
# pin without an orchestrator restart. Matches writing_agent.py.
def _rfp_reviewer_id() -> str:
    return os.environ.get("RFP_REVIEWER_ID", "")


def _rfp_reviewer_param():
    """Return the ``agent`` argument with version pin if available.

    Same resolution as ``writing_agent._writing_agent_param``: prefer
    the structured ``{type: agent, id, version}`` form when a pin is
    in ``config.AGENT_VERSIONS``; fall back to bare ID (latest).
    """
    agent_id = _rfp_reviewer_id()
    if not agent_id:
        return agent_id
    version = _config.AGENT_VERSIONS.get("rfp_reviewer")
    if not isinstance(version, int):
        return agent_id
    return {"type": "agent", "id": agent_id, "version": version}


# Hard ceiling per review call. Full-rubric review (Kapa call per
# product answer + commitment / marketing / citation / reference /
# flag checks on every record) takes meaningfully longer than the
# 120s spot-check budget the earlier draft assumed. Revised sizing
# after the first live RFP run on 2026-05-19 (session
# sesn_EXAMPLE) hit exactly 601s wall-clock without
# producing a JSON verdict at the prior 600s ceiling. The corrected
# math: a 50-question RFP with ~30 product answers fires ~30 Kapa
# calls at P90 TTFC ~10s (not the optimistic 5s the prior comment
# assumed) = 300s of cold-start cost, plus 20 rpm rate-limit
# serialization (~90s of holds to stay under the cap on 30 calls),
# plus post-batch model reasoning on Opus 4.8 over 30 results.
# Worst case ~900s; 1200s provides safe headroom. The Responder's
# revision loop allows up to 2 retries, so worst-case total review
# wall-clock is 3 × 1200s = 60 min — within Railway's session
# timeout margin and acceptable for a process that runs ~1/week.
DEFAULT_TIMEOUT_SECONDS = 1200.0


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RFPReviewResult:
    """Result of one RFP Reviewer call.

    ``ok=True`` and ``verdict`` populated means the reviewer ran and
    returned a parseable verdict. The RFP Responder branches on
    ``verdict``: PASS → final summary; REVISE → address findings and
    re-call (max 2x).

    ``ok=False`` means the call failed (missing env, timeout, parse
    error). The RFP Responder treats that as a soft PASS to avoid
    blocking the user on reviewer infrastructure.
    """

    ok: bool
    verdict: str = ""  # "PASS" | "REVISE"
    overall_assessment: str = ""
    findings: tuple[dict, ...] = ()
    questions_reviewed: int = 0
    kapa_calls_made: int = 0
    error: str = ""
    duration_seconds: float = 0.0
    session_id: str = ""

    def to_dict(self) -> dict:
        """JSON-serializable form for tool-result dispatch."""
        return {
            "ok": self.ok,
            "verdict": self.verdict,
            "overall_assessment": self.overall_assessment,
            "findings": list(self.findings),
            "questions_reviewed": self.questions_reviewed,
            "kapa_calls_made": self.kapa_calls_made,
            "error": self.error,
            "duration_seconds": round(self.duration_seconds, 3),
            "session_id": self.session_id,
        }


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def build_user_message(qa_index: list, feedback: Optional[str] = None) -> str:
    """Compose the user-side turn for one Reviewer call.

    The QA index is serialized inline so the Reviewer doesn't depend
    on the agent's ephemeral ``/mnt/session/outputs/`` mount being
    visible from the orchestrator's filesystem.
    """
    payload = json.dumps(qa_index, indent=2, default=str)
    msg = (
        "QA index from the drafted RFP response (one record per question):\n\n"
        f"```json\n{payload}\n```\n\n"
        "Review per your system prompt. Apply the six-check rubric to EVERY "
        "question in this index — no sampling. Fire one Kapa call per "
        "product-category answer to verify the cited claim. Return the JSON "
        "verdict with ``questions_reviewed`` and ``kapa_calls_made`` "
        "populated."
    )
    if feedback:
        msg += (
            "\n\nThis is a re-review after the RFP Responder addressed your "
            f"prior findings. Their fix notes:\n\n{feedback}\n\n"
            "Verify the fixes landed and re-grade."
        )
    return msg


# ---------------------------------------------------------------------------
# Core entry point
# ---------------------------------------------------------------------------


_CLIENT: Optional[anthropic.Anthropic] = None


def _client() -> anthropic.Anthropic:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _CLIENT


def _finalize(result: RFPReviewResult, agent_id: str) -> RFPReviewResult:
    """Archive the Reviewer session and write a session_costs row, then
    return ``result`` unchanged.

    Called from every exit path that has a non-empty ``session_id`` so the
    Reviewer matches the existing project contract: every Managed Agent
    session writes one row to ``session_costs`` and gets archived to free
    Anthropic-side resources. No-op when ``session_id`` is empty (early
    exits before ``sessions.create`` succeeded). Never raises — the
    cost-log and archive failures are logged and swallowed so finalization
    cannot promote an ``ok=True`` result to a partial failure.
    """
    if not result.session_id:
        return result
    try:
        from session_runner import (  # type: ignore[import-not-found]
            _archive_session,
            _log_session_usage,
        )
    except Exception:
        log.exception(
            "RFP Reviewer finalize: orchestrator import failed for session=%s",
            result.session_id,
        )
        return result
    try:
        _log_session_usage(
            result.session_id,
            "rfp-review",
            portco_key="acme",
            trigger="slack-rfp-review",
            agent_id=agent_id,
            response_length_chars=len(result.overall_assessment or ""),
            outcome="success" if result.ok else "error",
        )
    except Exception:
        log.exception(
            "RFP Reviewer cost logging failed for session=%s", result.session_id
        )
    try:
        _archive_session(result.session_id)
    except Exception:
        log.exception("RFP Reviewer archive failed for session=%s", result.session_id)
    return result


def run_review(
    qa_index: list,
    feedback: Optional[str] = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> RFPReviewResult:
    """Run one RFP Reviewer turn and return the parsed verdict.

    Multi-turn session (the Reviewer fires one Kapa call per product-
    category answer for fact verification), but it is one *review*:
    the Reviewer walks the entire QA index, applies the rubric, and
    returns a verdict. No memory store, no portco vault — the Reviewer
    has access to Kapa and db_query via the standard orchestrator
    dispatch, exactly as the RFP Responder does.

    ``feedback`` is the prior round's fix notes when the RFP Responder
    re-submits after a REVISE verdict; the Reviewer uses it to verify
    the fixes landed before re-grading.

    Never raises. Every failure path returns
    ``RFPReviewResult(ok=False, error=...)``. The RFP Responder treats
    that as a soft PASS to avoid blocking on reviewer outages. Every
    exit path that successfully created a session passes through
    ``_finalize`` which writes a ``session_costs`` row and archives the
    Anthropic session.
    """
    started = time.monotonic()

    agent_id = _rfp_reviewer_id()
    if not agent_id:
        return _finalize(
            RFPReviewResult(
                ok=False,
                error="RFP_REVIEWER_ID is unset — Reviewer unprovisioned",
                duration_seconds=time.monotonic() - started,
            ),
            agent_id,
        )

    client = _client()

    try:
        session = client.beta.sessions.create(
            # Same TypedDict-narrowing dance as the RFP runner; see
            # ``rfp_runner._process_rfp``.
            agent=_rfp_reviewer_param(),  # type: ignore[arg-type]
            environment_id=ENVIRONMENT_ID,
            title="RFP review pass",
        )
    except Exception as e:
        return _finalize(
            RFPReviewResult(
                ok=False,
                error=f"session_create_failed: {e}",
                duration_seconds=time.monotonic() - started,
            ),
            agent_id,
        )

    session_id = session.id
    log.info(f"RFP Reviewer session created: {session_id}")

    user_text = build_user_message(qa_index, feedback=feedback)

    # The Reviewer is multi-turn — it fires Kapa fact-verification on every
    # product answer plus reasoning_summary before its final JSON. Each of
    # those tool calls drives the session to ``requires_action`` and needs
    # the orchestrator-side dispatcher (``_dispatch_tool``) to run the
    # tool and ship the result back via ``user.custom_tool_result``.
    # ``session_runner._stream_and_handle`` is the existing loop that
    # implements this contract — reuse it instead of duplicating dispatch
    # logic here. Lazy import to break the circular dependency
    # (session_runner imports run_review for ``_dispatch_review_rfp_draft``).
    #
    # Wall-clock timeout: _stream_and_handle has no built-in deadline; we
    # enforce one with a per-event timer to bound runaway reviews. The
    # earlier manual-loop variant attempted dispatch via a ``continue`` on
    # ``requires_action`` and silently hung — the session reached idle, the
    # loop kept reading without ever sending a tool result, and the model
    # never produced its final JSON. Routing through _stream_and_handle
    # closes that gap.
    try:
        from session_runner import _stream_and_handle  # type: ignore[import-not-found]
    except Exception as e:
        return _finalize(
            RFPReviewResult(
                ok=False,
                error=f"stream_handle_import_failed: {e}",
                session_id=session_id,
                duration_seconds=time.monotonic() - started,
            ),
            agent_id,
        )

    text_parts: list[str] = []
    timed_out = False
    # Spawn the stream-and-handle call in a daemon thread so the main
    # caller (the RFP Responder's tool-dispatch thread) can enforce a
    # wall-clock ceiling without losing the partial result. On timeout,
    # we record what came back so far and return ``ok=False, error=timeout``;
    # the RFP Responder's prompt treats that as a soft PASS with the
    # ``[REVIEW_INCOMPLETE]`` audit tag.
    import threading as _threading

    review_error: list[str] = []

    def _run_stream():
        try:
            # thread_ts intentionally omitted (defaults to None) because the
            # Reviewer's session has no associated Slack thread — the result
            # goes back to the RFP Responder as a tool result, not Slack.
            # Tuple slots use ``_`` so Pyright doesn't flag unused names.
            parts, _, err_type, _ = _stream_and_handle(
                session_id,
                send_events=[
                    {
                        "type": "user.message",
                        "content": [{"type": "text", "text": user_text}],
                    }
                ],
                verbosity="normal",
                portco_key="acme",
            )
            text_parts.extend(parts or [])
            if err_type:
                review_error.append(str(err_type))
        except Exception as exc:
            review_error.append(f"stream_failed: {exc}")
        finally:
            # Plan #52 PR-C: daemon-thread archive belt-and-braces. When
            # _finalize runs after a timeout, the Reviewer's session may
            # still be in requires_action (Kapa mid-flight), so the main
            # thread's archive can be rejected. Having the daemon also
            # attempt archive on exit means a clean session terminalizes
            # from both paths. Swallow exceptions — _finalize is the
            # authoritative archive call.
            try:
                from session_runner import (  # type: ignore[import-not-found]
                    _archive_session as _arch,
                )

                _arch(session_id)
            except Exception as _exc:
                log.warning(
                    "RFP Reviewer daemon archive failed for session=%s: %s",
                    session_id,
                    _exc,
                )

    worker = _threading.Thread(
        target=_run_stream,
        daemon=True,
        name=f"rfp-review-{session_id[:12]}",
    )
    worker.start()
    worker.join(timeout=timeout_seconds)
    if worker.is_alive():
        timed_out = True
        log.warning(
            f"RFP Reviewer timed out after {timeout_seconds}s (session={session_id})"
        )

    raw = "".join(text_parts).strip()

    if timed_out:
        return _finalize(
            RFPReviewResult(
                ok=False,
                error="timeout",
                session_id=session_id,
                duration_seconds=time.monotonic() - started,
            ),
            agent_id,
        )

    if review_error:
        return _finalize(
            RFPReviewResult(
                ok=False,
                error=f"stream_failed: {review_error[0]}",
                session_id=session_id,
                duration_seconds=time.monotonic() - started,
            ),
            agent_id,
        )

    if not raw:
        return _finalize(
            RFPReviewResult(
                ok=False,
                error="empty_response",
                session_id=session_id,
                duration_seconds=time.monotonic() - started,
            ),
            agent_id,
        )

    parsed = _parse_response(raw)
    if parsed is None:
        return _finalize(
            RFPReviewResult(
                ok=False,
                error=f"json_parse_failed: {raw[:200]}",
                session_id=session_id,
                duration_seconds=time.monotonic() - started,
            ),
            agent_id,
        )

    verdict = str(parsed.get("verdict") or "").upper().strip()
    if verdict not in ("PASS", "REVISE"):
        return _finalize(
            RFPReviewResult(
                ok=False,
                error=f"invalid_verdict: {verdict!r}",
                session_id=session_id,
                duration_seconds=time.monotonic() - started,
            ),
            agent_id,
        )

    overall = str(parsed.get("overall_assessment") or "").strip()
    findings_raw = parsed.get("findings") or []
    if isinstance(findings_raw, list):
        findings = tuple(f for f in findings_raw if isinstance(f, dict))
    else:
        findings = ()

    questions_reviewed_raw = parsed.get("questions_reviewed")
    questions_reviewed = (
        int(questions_reviewed_raw)
        if isinstance(questions_reviewed_raw, (int, float))
        else 0
    )
    kapa_calls_raw = parsed.get("kapa_calls_made")
    kapa_calls = int(kapa_calls_raw) if isinstance(kapa_calls_raw, (int, float)) else 0

    return _finalize(
        RFPReviewResult(
            ok=True,
            verdict=verdict,
            overall_assessment=overall,
            findings=findings,
            questions_reviewed=questions_reviewed,
            kapa_calls_made=kapa_calls,
            session_id=session_id,
            duration_seconds=time.monotonic() - started,
        ),
        agent_id,
    )


def _parse_response(raw: str) -> Optional[dict]:
    """Pull the JSON blob out of the Reviewer's response.

    Tolerates markdown code-fence wrapping (the system prompt says
    no fences, but the model occasionally adds them anyway) and
    trailing whitespace. Returns None if no valid JSON object can
    be recovered.
    """
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1] if "\n" in cleaned else cleaned[3:]
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
        cleaned = cleaned.strip()
        if cleaned.startswith("json\n"):
            cleaned = cleaned[5:].lstrip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        first = cleaned.find("{")
        last = cleaned.rfind("}")
        if first != -1 and last != -1 and last > first:
            try:
                parsed = json.loads(cleaned[first : last + 1])
            except json.JSONDecodeError:
                return None
        else:
            return None
    if not isinstance(parsed, dict):
        return None
    return parsed
