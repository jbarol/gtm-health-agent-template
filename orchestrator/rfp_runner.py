"""RFP Responder runner — Slack file upload → Managed Agents session → drafted response.

Triggered by a Slack ``message`` event with ``subtype == "file_share"`` in
the channel identified by ``RFP_CHANNEL_ID``. Each invocation provisions a
fresh, standalone Managed Agents session against the RFP Responder agent
(``RFP_RESPONDER_ID``) — sessions do NOT join the Coordinator's multi-
agent roster.

Lifecycle for one RFP:

    1. Slack delivers the ``message`` event. ``slack_bot.handle_message``
       checks the channel against ``RFP_CHANNEL_ID`` and dispatches here.
    2. We post a quick ack in-thread and spawn a daemon thread so the
       Slack handler returns inside Bolt's 3s budget.
    3. The worker downloads the file from Slack (``url_private_download``
       + bot token), saves to a host-side tmp dir, uploads to the
       Anthropic Files API, and mounts it into the session at
       ``/workspace/rfp_input.<ext>``.
    4. ``session_runner._stream_and_handle`` drives the session — Kapa
       streaming search, db_query / dump_sf_query / query_artifact, and
       materialize_xlsx are dispatched server-side via the existing
       ``_dispatch_tool`` plumbing. No new tool wiring required.
    5. When the session goes idle with a terminal stop_reason, we post
       the agent's final ``agent.message`` text to the thread as a
       summary, then download every file the agent wrote to
       ``/mnt/session/outputs/`` and upload each one to Slack via
       ``_download_session_files``.
    6. ``_log_session_usage`` persists a cost row keyed by
       ``trigger='slack-rfp'``; ``_archive_session`` releases the
       session container.

Failure modes:

  * Unsupported extension → ack with the list of supported types; no
    session created.
  * Slack download failure → in-thread error message; no session.
  * Anthropic upload / session create failure → in-thread error
    message; nothing to archive.
  * Mid-session crash → the watchdog and ``_archive_session`` failure
    catch in the finally block contain the blast radius; the cost row
    still lands with ``outcome='error'``.

Volume target: ~60 RFPs/year (about 1/week). No queue, no retry, no
DB persistence of in-flight RFPs — if the orchestrator restarts mid-
draft, the user re-uploads. The investigation worker pattern is
overkill here.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Optional

import httpx

from config import ENVIRONMENT_ID, SLACK_BOT_TOKEN

log = logging.getLogger(__name__)

# Inputs we know how to handle today. The agent's prompt classifies the
# format from the actual file (not the extension) so the agent can fall
# back to "post a clarifying request" if a docx file arrives with a .pdf
# extension, but we still gate at the Slack-event boundary to avoid
# burning a session on a screenshot or zip.
SUPPORTED_EXTENSIONS = {"xlsx", "docx", "pdf"}

# Host-side tmp dir for the original Slack download. Distinct from
# session_runner.OUTPUTS_DIR (which holds agent OUTPUTS). The agent never
# sees this path; we upload from here straight to the Files API.
RFP_INPUT_DIR = Path("/tmp/gtm-health-agent/rfp-inputs")

# Wall-clock budget per RFP run. RFPs with 100+ questions and heavy Kapa
# fan-out can take ~10 min. Set generous; the watchdog handles truly
# stuck sessions independently.
RFP_SESSION_TIMEOUT_SECONDS = 30 * 60

# Slack download timeout — generous because RFP xlsx files can be a few
# MB and Slack's CDN occasionally lags. httpx default is 5s which would
# fail on legitimate large uploads.
SLACK_DOWNLOAD_TIMEOUT_SECONDS = 60.0


def _rfp_channel_id() -> str:
    """Read at call time so a Railway env var flip rotates without restart."""
    return os.environ.get("RFP_CHANNEL_ID", "")


def _rfp_responder_id() -> str:
    """Read at call time — matches the writing_agent.py pattern."""
    return os.environ.get("RFP_RESPONDER_ID", "")


def is_rfp_channel(channel_id: Optional[str]) -> bool:
    """True when ``channel_id`` is the configured RFP intake channel.

    Empty ``RFP_CHANNEL_ID`` returns False — the feature is opt-in via
    env var. Callers in slack_bot.py route here only when this returns
    True, so the existing investigation pipeline is never displaced.
    """
    expected = _rfp_channel_id()
    return bool(expected) and channel_id == expected


def handle_rfp_message(event: dict, say) -> None:
    """Slack ``message`` handler entry point for the RFP channel.

    Validates the file payload, posts an ack, and spawns a daemon thread
    to do the heavy work. Never blocks the Bolt event loop.
    """
    files = event.get("files") or []
    if not files:
        say(
            ":mag: Drop an RFP file (.xlsx, .docx, or .pdf) in this channel and I'll draft a response.",
            thread_ts=event.get("ts"),
        )
        return

    user_id = event.get("user", "unknown")
    channel_id = event.get("channel", "")
    # Always thread the response under the user's upload — never post at
    # top level. If multiple files arrive in one message we process the
    # first; the rest get a single in-thread note.
    thread_ts = event.get("thread_ts") or event.get("ts")
    primary = files[0]
    extra = files[1:]

    name = primary.get("name") or primary.get("title") or "rfp_input"
    ext = _extract_extension(name)
    if ext not in SUPPORTED_EXTENSIONS:
        say(
            f":warning: I can only draft responses for "
            f"`{', '.join(sorted(SUPPORTED_EXTENSIONS))}` files. Got `{name}` "
            f"(extension `{ext or '<none>'}`). Re-upload in a supported format.",
            thread_ts=thread_ts,
        )
        return

    if not _rfp_responder_id():
        say(
            ":warning: RFP Responder agent is not configured "
            "(`RFP_RESPONDER_ID` unset). Provision it via "
            "`python agents/provision_rfp_agent.py` and set the env var.",
            thread_ts=thread_ts,
        )
        return

    extra_note = ""
    if extra:
        names = ", ".join(f.get("name") or "<unnamed>" for f in extra)
        extra_note = (
            f"\n\nNote: only the first file is being drafted. "
            f"Re-upload separately to draft: {names}."
        )

    say(
        f":memo: Drafting RFP response from `{name}`. I'll pull product context "
        f"from Kapa and customer / market context from Salesforce, then post "
        f"the response file and a summary in this thread when done. Typical "
        f"runtime: 3–10 minutes depending on question count.{extra_note}",
        thread_ts=thread_ts,
    )

    thread = threading.Thread(
        target=_process_rfp_safe,
        kwargs={
            "file_info": primary,
            "ext": ext,
            "thread_ts": thread_ts,
            "channel_id": channel_id,
            "user_id": user_id,
            "say": say,
        },
        daemon=True,
        name=f"rfp-{primary.get('id') or 'noid'}",
    )
    thread.start()


def _process_rfp_safe(**kwargs) -> None:
    """Top-level catch so a worker crash never breaks the Slack handler."""
    say = kwargs.get("say")
    thread_ts = kwargs.get("thread_ts")
    try:
        _process_rfp(**kwargs)
    except Exception:
        log.exception("RFP worker crashed")
        if say and thread_ts:
            try:
                say(
                    ":x: RFP drafting failed unexpectedly — check orchestrator "
                    "logs. Re-upload to retry.",
                    thread_ts=thread_ts,
                )
            except Exception:
                log.exception("Failed to post RFP failure message")


def _process_rfp(
    file_info: dict,
    ext: str,
    thread_ts: str,
    channel_id: str,
    user_id: str,
    say,
) -> None:
    """Download → upload → session → stream → post outputs. Synchronous."""
    # Lazy import so module load doesn't pull session_runner's heavy
    # dependencies into the slack_bot import path. session_runner is
    # already imported by main.py at startup; this is just bookkeeping.
    from session_runner import (
        _archive_session,
        _download_session_files,
        _log_session_usage,
        _resolve_agent_param,
        _stream_and_handle,
        sanitize_session_title,
        VAULT_IDS,
        client,
    )

    file_id = file_info.get("id") or ""
    file_name = file_info.get("name") or f"rfp_input.{ext}"
    download_url = (
        file_info.get("url_private_download") or file_info.get("url_private") or ""
    )

    if not download_url:
        say(
            ":x: Slack didn't give me a download URL for that file. "
            "Re-upload to retry.",
            thread_ts=thread_ts,
        )
        return

    RFP_INPUT_DIR.mkdir(parents=True, exist_ok=True)
    # Suffix the file ID so concurrent uploads don't collide on the
    # host disk. The agent sees the canonical mount path regardless.
    safe_basename = re.sub(r"[^A-Za-z0-9._-]+", "_", file_name)[:80]
    local_path = RFP_INPUT_DIR / f"{file_id or 'rfp'}_{safe_basename}"

    log.info(
        "RFP intake: downloading %s (%d bytes, type=%s) for thread=%s user=%s",
        file_name,
        file_info.get("size", 0),
        file_info.get("mimetype", "?"),
        thread_ts,
        user_id,
    )

    try:
        with httpx.Client(
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
            timeout=SLACK_DOWNLOAD_TIMEOUT_SECONDS,
        ) as http:
            response = http.get(download_url)
            response.raise_for_status()
            local_path.write_bytes(response.content)
    except httpx.HTTPError as e:
        log.exception("Slack file download failed for %s", file_name)
        say(
            f":x: Couldn't download `{file_name}` from Slack ({e}). Re-upload to retry.",
            thread_ts=thread_ts,
        )
        return

    # Upload to Anthropic Files API and mount into the session.
    try:
        with open(local_path, "rb") as f:
            uploaded = client.beta.files.upload(file=f)
    except Exception as e:
        log.exception("Anthropic Files upload failed for %s", file_name)
        say(
            f":x: Couldn't stage `{file_name}` with Anthropic ({e}). "
            "Re-upload to retry.",
            thread_ts=thread_ts,
        )
        return

    # Canonical mount path the agent's prompt expects. Extension matters
    # — the agent's first action is to glob() and pick the right skill.
    mount_path = f"/workspace/rfp_input.{ext}"

    rfp_responder_id = _rfp_responder_id()

    session = None
    session_id = ""
    started = time.monotonic()
    outcome = "success"
    # Initialize before the try block so the finally clause can always
    # reference agent_text_parts for the cost row's response_length_chars.
    agent_text_parts: list[str] = []
    try:
        try:
            session = client.beta.sessions.create(
                # The SDK types ``agent`` as the ``Agent`` TypedDict but
                # ``_resolve_agent_param`` returns ``str | dict`` for back-compat
                # with bare-ID call sites. Same call shape session_runner.py
                # uses; cast suppresses the false-positive Pyright narrowing.
                agent=_resolve_agent_param(rfp_responder_id),  # type: ignore[arg-type]
                environment_id=ENVIRONMENT_ID,
                title=sanitize_session_title(f"RFP draft: {file_name}"),
                vault_ids=VAULT_IDS,
                resources=[
                    {
                        "type": "file",
                        "file_id": uploaded.id,
                        "mount_path": mount_path,
                    }
                ],
            )
        except Exception as e:
            log.exception("RFP session create failed")
            say(
                f":x: Couldn't start an RFP session ({e}). Re-upload to retry; "
                "check orchestrator logs if it keeps failing.",
                thread_ts=thread_ts,
            )
            outcome = "error"
            return

        session_id = session.id
        log.info("RFP session created: %s (file=%s ext=%s)", session_id, file_name, ext)

        kickoff = (
            f"A Acme team member has dropped an RFP at `{mount_path}` "
            f"(original Slack filename: `{file_name}`). Follow your system "
            f"prompt: inspect the file, classify each question, draft "
            f"answers with Kapa + Salesforce citations, write the response "
            f"to `/mnt/session/outputs/`, save the QA index sidecar, and "
            f"end with the Slack-shaped summary message described in your "
            f"prompt."
        )

        # Unused tuple slots use ``_`` (twice — Python allows reuse) so
        # Pyright doesn't flag them as unread variables.
        agent_text_parts, _, error_type, _ = _stream_and_handle(
            session_id,
            send_events=[
                {
                    "type": "user.message",
                    "content": [{"type": "text", "text": kickoff}],
                }
            ],
            thread_ts=thread_ts,
            verbosity="normal",
            portco_key="acme",
            user_id=user_id,
            channel_id=channel_id,
        )

        if error_type:
            outcome = "error"
            log.warning(
                "RFP session %s ended with error_type=%s", session_id, error_type
            )

        # Post the agent's final summary text into the thread. The
        # prompt enforces a phone-readable shape; we just relay it.
        summary = "".join(agent_text_parts).strip()
        if summary:
            try:
                say(summary, thread_ts=thread_ts)
            except Exception:
                log.exception("Posting RFP summary to Slack failed")
        else:
            say(
                ":warning: RFP session ended without a summary message. "
                "Files (if any) are attached below; logs have details.",
                thread_ts=thread_ts,
            )

        # Pull every output file the agent wrote to /mnt/session/outputs/
        # and upload to Slack in-thread. Reuses the existing download +
        # post path used by investigation / forecast / dream flows.
        # ``channel=channel_id`` is mandatory: without it ``post_file``
        # falls back to ``SLACK_CHANNEL_ID`` while keeping the RFP
        # thread_ts, which either uploads to the wrong channel or
        # fails the thread_ts validation. (codex P2, 2026-05-18)
        try:
            # ``channel`` kwarg added to ``_download_session_files`` in the
            # same commit; Pyright cache may lag — silence and move on.
            _download_session_files(
                session_id,
                reply_to=thread_ts,
                channel=channel_id,  # type: ignore[call-arg]
            )
        except Exception:
            log.exception("Downloading RFP outputs failed for session=%s", session_id)
            say(
                ":x: Failed to fetch the drafted response file from the "
                "session. Logs have details — the draft text above is your "
                "best fallback.",
                thread_ts=thread_ts,
            )
            outcome = "error"

    finally:
        elapsed = time.monotonic() - started
        log.info(
            "RFP session %s elapsed=%.1fs outcome=%s",
            session_id or "<no-session>",
            elapsed,
            outcome,
        )
        if session_id:
            try:
                _log_session_usage(
                    session_id,
                    "rfp",
                    portco_key="acme",
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    user_id=user_id,
                    trigger="slack-rfp",
                    verbosity="normal",
                    agent_id=rfp_responder_id,
                    response_length_chars=len("".join(agent_text_parts)),
                    outcome=outcome,
                )
            except Exception:
                log.exception("RFP cost logging failed for session=%s", session_id)
            try:
                _archive_session(session_id)
            except Exception:
                log.exception("RFP session archive failed for session=%s", session_id)

        # Clean up the host-side input file — keep the disk tidy between
        # runs. Anthropic Files API copy persists independently.
        try:
            local_path.unlink(missing_ok=True)
        except Exception:
            log.debug("Local RFP input cleanup failed", exc_info=True)


def _extract_extension(name: str) -> str:
    """Lowercased extension without the dot. Empty string if none."""
    if "." not in name:
        return ""
    return name.rsplit(".", 1)[-1].strip().lower()
