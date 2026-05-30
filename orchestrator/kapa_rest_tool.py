"""Kapa REST API client — replaces the retired Kapa MCP integration.

Why this exists (2026-05-13 pivot)
----------------------------------
Kapa hosts an MCP server at ``https://acme.mcp.kapa.example`` that
requires OAuth with dynamic client registration (the ``mcp-remote`` flow).
Anthropic Managed Agents vault credentials of type ``static_bearer``
authenticate against the REST API at ``https://api.kapa.ai`` but NOT the
MCP server. Kapa support (2026-05-14) confirmed they will not provide
machine-to-machine OAuth client credentials for security reasons, so the
MCP path is permanently closed for our headless runtime.

Rather than wire OAuth, we route through the REST endpoint as a custom
tool. The agent-facing tool name (``search_knowledge_base``)
is preserved so existing prompts work unchanged.

Streaming endpoint (2026-05-14)
-------------------------------
``POST https://api.kapa.ai/query/v1/projects/{project_id}/chat/stream/``

Headers:
  ``X-API-KEY: <KAPA_ACME_API_KEY>``
  ``Content-Type: application/json``
  ``Accept: text/event-stream``

Body:
  ``{"query": "<natural-language question>"}``

Response is a sequence of JSON chunks delimited by the Unicode character
U+241E "SYMBOL FOR RECORD SEPARATOR" (the *visible glyph*, NOT the U+001E
RS control byte the literal docs reading would imply — see ``STREAM_SEP``
constant). Each chunk is double-wrapped:

  ``{"chunk": {"type": <kind>, "content": {...}, "stream_end": <bool>}}``

Chunk types:
  ``partial_answer`` — ``content.text`` is an incremental answer fragment;
                       concatenate in arrival order.
  ``relevant_sources`` — ``content.relevant_sources`` is a list of
                         ``{title, source_url, contains_internal_data}``.
  ``metadata`` — ``content.is_uncertain`` flips the result's
                 ``is_uncertain`` flag.
  ``identifiers`` — terminal chunk carrying ``thread_id`` and
                    ``question_answer_id``; we discard these because
                    each session is a one-shot lookup.
  ``error`` — ``content.reason`` carries a server-side error string.

Output shape we return to the agent
-----------------------------------
The agent receives a single ``content`` string that combines the
synthesized answer with a "Sources:" appendix. This mirrors what the
Kapa MCP search tool returned (markdown chunks with source URLs) and
matches the prior non-streaming return shape so neither the dispatcher
nor any agent prompt needs to change.

Rate limits / behavior
----------------------
- Kapa caps Chat at **20 req/min** per API key (60 req/min applies only
  to the Retrieval endpoint — a common doc-reading mistake we corrected
  on 2026-05-14 after Kapa's support reply).
- 60-second timeout per request — generous ceiling. The streaming
  endpoint's TTFC is ~4.5s in live probes (2026-05-14), so the practical
  user-perceived wait is far shorter than for the non-streaming path
  (which was timing out at 15s pre-PR #153).
- Network / 4xx / 5xx failures return a structured error dict the
  dispatcher relays to the agent; the agent prompt instructs it to
  treat tool errors as "knowledge base unavailable, proceed without."
- On a mid-stream ``error`` chunk or a network drop AFTER substantive
  partial content has already arrived, we return ``ok=True`` with
  ``is_uncertain=True`` — the partial answer is more useful to the
  agent than a hard failure.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any
from urllib import error, request

import kapa_rate_limiter

log = logging.getLogger(__name__)

# Non-streaming URL kept as a constant for documentation only. The
# active path is the streaming endpoint below; we do not silently
# fall back because the non-streaming endpoint was timing out at 15s
# in 5 of 6 live calls (see prior comment in PR #153 lineage).
KAPA_REST_URL_TEMPLATE = "https://api.kapa.ai/query/v1/projects/{project_id}/chat/"
KAPA_STREAM_URL_TEMPLATE = (
    "https://api.kapa.ai/query/v1/projects/{project_id}/chat/stream/"
)

# Kapa's chunk delimiter is the VISIBLE GLYPH for Record Separator
# (U+241E, UTF-8 bytes ``\xe2\x90\x9e``), NOT the U+001E control
# character. Live probe 2026-05-14 confirmed this: splitting on
# ``\x1e`` yielded zero chunks. Reading Kapa's docs literally
# ("split on `␞`") leads to the wrong delimiter — leaving the
# constant + comment in place to make the choice grep-able.
STREAM_SEP = "␞"
STREAM_SEP_BYTES = STREAM_SEP.encode("utf-8")

# 60s ceiling matches the prior non-streaming budget. TTFC is ~4.5s
# in live probes (2026-05-14), so practical wait is far shorter.
TIMEOUT_SECONDS = 60.0

# Per-call cap on the assembled ``content`` string. Matches the prior
# non-streaming cap and Kapa's documented Retrieval default. Truncation
# appends a clear ``[truncated]`` marker so the agent can decide whether
# to re-query with a narrower question.
MAX_RESPONSE_CHARS = 35_000

# How many bytes to pull off the socket per ``resp.read()`` call. Kapa
# chunks are small JSON objects (typically <200 bytes for
# ``partial_answer``), so a 4KB read usually delivers several complete
# chunks per syscall.
READ_CHUNK_BYTES = 4096

# Skip + log up to this many JSON-decode failures on individual chunks
# before treating the stream as corrupt and aborting. A single malformed
# chunk is plausible; six in a row means the stream is wrong shape.
MAX_BAD_CHUNKS = 5

# Below this many characters of accumulated answer, a mid-stream error
# or network drop is treated as a hard failure (return ``ok=False``).
# Above it, we return the partial content with ``is_uncertain=True`` —
# the agent prompt treats uncertain results as "use with caveat" which
# is preferable to a tool-error fallback when we already have most of
# the answer.
MIN_CONTENT_FOR_PARTIAL_SUCCESS = 100


def _assemble_content(answer: str, raw_sources: list[Any]) -> tuple[str, int]:
    """Compose the final ``content`` string from accumulated answer + sources.

    Dedupes sources by URL — Kapa returns one entry per matched chunk,
    so the same wiki page can appear 5x. We keep one bullet per
    distinct URL with the longest (most descriptive) title we saw.

    Returns ``(content_string, distinct_source_count)``. Truncates to
    ``MAX_RESPONSE_CHARS`` with a clear marker if needed.
    """
    by_url: dict[str, str] = {}
    for s in raw_sources:
        if not isinstance(s, dict):
            continue
        u = (s.get("source_url") or "").strip()
        if not u:
            continue
        t = (s.get("title") or "").strip()
        prev = by_url.get(u, "")
        if len(t) > len(prev):
            by_url[u] = t

    if by_url:
        source_lines = ["", "Sources:"]
        for url_, title in by_url.items():
            if title:
                source_lines.append(f"- [{title}]({url_})")
            else:
                source_lines.append(f"- {url_}")
        content = answer + "\n" + "\n".join(source_lines)
    else:
        content = answer

    if len(content) > MAX_RESPONSE_CHARS:
        content = content[: MAX_RESPONSE_CHARS - 100] + "\n\n[truncated]"

    return content, len(by_url)


def _process_chunk(
    obj: dict,
    answer_parts: list[str],
    raw_sources: list[Any],
) -> tuple[bool, str | None]:
    """Apply one parsed JSON chunk to the in-flight accumulators.

    Returns ``(is_uncertain_seen, error_reason)``. ``error_reason`` is
    non-None when the chunk is an ``error`` chunk — the caller should
    stop reading and decide between partial-success and hard-failure.
    """
    # Chunks are double-wrapped: ``{"chunk": {"type":..., "content":...}}``.
    # Tolerate both wrapped and unwrapped shapes defensively in case Kapa
    # ever flattens the schema.
    chunk = obj.get("chunk", obj)
    ctype = chunk.get("type")
    content = chunk.get("content") or {}

    if ctype == "partial_answer":
        text = content.get("text")
        if isinstance(text, str) and text:
            answer_parts.append(text)
        return bool(content.get("is_uncertain")), None

    if ctype == "relevant_sources":
        srcs = content.get("relevant_sources") or []
        if isinstance(srcs, list):
            raw_sources.extend(s for s in srcs if isinstance(s, dict))
        return False, None

    if ctype == "metadata":
        return bool(content.get("is_uncertain")), None

    if ctype == "identifiers":
        # thread_id, question_answer_id — not needed for one-shot lookups.
        return False, None

    if ctype == "error":
        reason = content.get("reason") or "stream_error"
        return False, str(reason)

    # Unknown chunk type — log and continue rather than aborting; Kapa
    # may add new chunk kinds without breaking existing clients.
    log.debug("Kapa stream: unknown chunk type %r — ignoring", ctype)
    return False, None


def search_kapa(
    query: str,
    *,
    api_key: str,
    project_id: str,
    timeout_seconds: float = TIMEOUT_SECONDS,
    caller: str = "unknown",
) -> dict[str, Any]:
    """Run a single Kapa streaming search against the Acme Internal project.

    Returns a result dict with shape::

        {
            "ok": True,
            "content": "<answer>\\n\\nSources:\\n- <url>\\n- <url>...",
            "is_uncertain": False,
            "source_count": 12,
            "elapsed_s": 2.3,
            "http_status": 200,
        }

    or, on failure::

        {
            "ok": False,
            "error": "<short error_type>",
            "detail": "<longer message>",
            "elapsed_s": 2.3,
        }

    Streaming-specific failure modes are surfaced via the ``error`` field:
      * ``http_<code>`` — pre-stream HTTP error (e.g. 401, 429).
      * ``network`` — connection failed before any chunk arrived.
      * ``stream_truncated`` — connection dropped after the headers but
        before any ``partial_answer`` chunk reached us.
      * ``stream_decode_failed`` — more than ``MAX_BAD_CHUNKS`` chunks
        failed to JSON-decode; the stream is likely corrupt.
      * ``kapa_error`` — Kapa sent an explicit ``error`` chunk with no
        prior substantive content.
      * ``empty_response`` — stream finished cleanly but yielded no
        ``partial_answer`` text.

    The dispatcher in ``session_runner._dispatch_tool`` wraps this and
    emits the right ``user.custom_tool_result`` shape back to the agent.
    """
    if not query or not query.strip():
        return {
            "ok": False,
            "error": "invalid_query",
            "detail": "query must be a non-empty string",
            "elapsed_s": 0.0,
        }
    if not api_key:
        return {
            "ok": False,
            "error": "missing_api_key",
            "detail": "KAPA_ACME_API_KEY env var unset on orchestrator",
            "elapsed_s": 0.0,
        }
    if not project_id:
        return {
            "ok": False,
            "error": "missing_project_id",
            "detail": "KAPA_ACME_PROJECT_ID env var unset on orchestrator",
            "elapsed_s": 0.0,
        }

    url = KAPA_STREAM_URL_TEMPLATE.format(project_id=project_id)
    payload = json.dumps({"query": query}).encode("utf-8")
    req = request.Request(
        url,
        data=payload,
        headers={
            "X-API-KEY": api_key,
            "Content-Type": "application/json",
            # Kapa's streaming endpoint rejects ``Accept: text/event-stream``
            # with HTTP 406 "Could not satisfy the request Accept header."
            # Live repro 2026-05-14 19:40 PT (session sesn_EXAMPLE
            # — every Kapa call 406'd post-PR #167). Despite the response
            # body being a U+241E-delimited stream, Kapa labels it
            # ``application/json``; the Accept header must match. The
            # streaming framing is preserved at the body layer, not the
            # transport/content-type layer.
            "Accept": "application/json",
            "User-Agent": "gtm-health-agent/kapa-rest-tool 0.2",
        },
        method="POST",
    )

    # Start the timer BEFORE acquiring a rate-limit token so that time
    # spent waiting in the bucket queue is included in ``elapsed_s``.
    # Without this, a saturated bucket can block for arbitrarily long
    # and then still run urlopen with a fresh network timeout, causing
    # elapsed_s to underreport the true user-visible latency.
    started = time.monotonic()

    # Layer 2 rate limit (see ``kapa_rate_limiter`` docstring): block on
    # the in-process token bucket BEFORE firing the HTTP request so the
    # REST path stays under Kapa's 20 RPM Chat-endpoint cap when multiple
    # sessions (RFP Responder + Reviewer) hit Kapa concurrently. When
    # ``KAPA_RATE_LIMIT_ENABLED=false`` this short-circuits to a no-op.
    kapa_rate_limiter.acquire(caller=caller)

    try:
        resp = request.urlopen(req, timeout=timeout_seconds)
    except error.HTTPError as e:
        elapsed = time.monotonic() - started
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        log.warning(
            "Kapa stream HTTP %d for project=%s elapsed=%.2fs body=%s",
            e.code,
            project_id,
            elapsed,
            body[:200],
        )
        return {
            "ok": False,
            "error": f"http_{e.code}",
            "detail": body or str(e),
            "elapsed_s": round(elapsed, 2),
        }
    except (error.URLError, TimeoutError, OSError) as e:
        elapsed = time.monotonic() - started
        log.warning(
            "Kapa stream network error project=%s elapsed=%.2fs err=%s",
            project_id,
            elapsed,
            e,
        )
        return {
            "ok": False,
            "error": "network",
            "detail": str(e),
            "elapsed_s": round(elapsed, 2),
        }
    except Exception as e:
        elapsed = time.monotonic() - started
        log.exception(
            "Kapa stream unexpected error project=%s elapsed=%.2fs",
            project_id,
            elapsed,
        )
        return {
            "ok": False,
            "error": "unexpected",
            "detail": str(e),
            "elapsed_s": round(elapsed, 2),
        }

    status = getattr(resp, "status", 200)
    buffer = b""
    answer_parts: list[str] = []
    raw_sources: list[Any] = []
    is_uncertain = False
    error_reason: str | None = None
    bad_chunks = 0
    stream_end_seen = False
    decode_failed = False

    def _decode_segment(seg: bytes) -> str:
        """Decode one chunk and strip an optional ``data:`` SSE prefix.

        Live probe 2026-05-14 saw raw JSON delimited by U+241E. Kapa's
        published docs reference Server-Sent-Event-style ``data: {...}``
        framing. Strip the prefix defensively so we tolerate either
        shape — costs nothing on the live wire format and saves a
        ``stream_decode_failed`` if Kapa ever switches.
        """
        text = seg.decode("utf-8", errors="replace").strip()
        if text.startswith("data:"):
            text = text[5:].lstrip()
        return text

    def _flush_complete_segments(segments: list[bytes]) -> bool:
        """Parse all but the trailing segment. Returns True to keep reading."""
        nonlocal is_uncertain, error_reason, bad_chunks, stream_end_seen
        nonlocal decode_failed
        for seg in segments[:-1]:
            text = _decode_segment(seg)
            if not text:
                continue
            try:
                obj = json.loads(text)
            except Exception as exc:
                bad_chunks += 1
                log.warning(
                    "Kapa stream: chunk decode failed (count=%d): %s",
                    bad_chunks,
                    exc,
                )
                if bad_chunks > MAX_BAD_CHUNKS:
                    decode_failed = True
                    return False
                continue
            if not isinstance(obj, dict):
                bad_chunks += 1
                continue
            seen_uncertain, err = _process_chunk(obj, answer_parts, raw_sources)
            if seen_uncertain:
                is_uncertain = True
            if err is not None:
                error_reason = err
                return False
            inner = obj.get("chunk", obj)
            if isinstance(inner, dict) and inner.get("stream_end"):
                stream_end_seen = True
        return True

    try:
        with resp:
            while True:
                try:
                    data = resp.read(READ_CHUNK_BYTES)
                except (error.URLError, TimeoutError, OSError) as exc:
                    # Network drop mid-stream — fall through to the
                    # partial-content path below if we have enough.
                    log.warning(
                        "Kapa stream: read failed mid-stream after %d bytes: %s",
                        len(buffer),
                        exc,
                    )
                    data = b""
                    error_reason = error_reason or f"network_mid_stream:{exc}"
                if not data:
                    break
                buffer += data
                segments = buffer.split(STREAM_SEP_BYTES)
                # All segments except the last are complete chunks.
                # The last one is the trailing partial we carry forward.
                keep_reading = _flush_complete_segments(segments)
                buffer = segments[-1]
                if not keep_reading:
                    break
    except Exception as exc:
        # Defensive: anything that escapes the loop should be logged but
        # not bubble. We still want to assemble whatever partial content
        # we have.
        log.exception("Kapa stream: unexpected error while consuming: %s", exc)
        error_reason = error_reason or f"unexpected:{exc}"

    # Try one final parse of any trailing buffer that wasn't followed
    # by a delimiter (some servers emit the last chunk without a
    # closing ␞).
    if buffer.strip():
        try:
            text = _decode_segment(buffer)
            obj = json.loads(text) if text else None
            if isinstance(obj, dict):
                seen_uncertain, err = _process_chunk(obj, answer_parts, raw_sources)
                if seen_uncertain:
                    is_uncertain = True
                if err is not None and error_reason is None:
                    error_reason = err
                inner = obj.get("chunk", obj)
                if isinstance(inner, dict) and inner.get("stream_end"):
                    stream_end_seen = True
        except Exception:
            log.debug("Kapa stream: trailing buffer not parseable, ignoring")

    elapsed = time.monotonic() - started

    if decode_failed:
        return {
            "ok": False,
            "error": "stream_decode_failed",
            "detail": (
                f"more than {MAX_BAD_CHUNKS} chunks failed to parse; "
                "stream is corrupt or wrong shape"
            ),
            "elapsed_s": round(elapsed, 2),
        }

    answer = "".join(answer_parts).strip()

    if error_reason and len(answer) < MIN_CONTENT_FOR_PARTIAL_SUCCESS:
        log.info(
            "Kapa stream: error_reason=%s answer_len=%d — returning error",
            error_reason,
            len(answer),
        )
        return {
            "ok": False,
            "error": (
                "kapa_error" if not error_reason.startswith("network_") else "network"
            ),
            "detail": error_reason,
            "elapsed_s": round(elapsed, 2),
        }

    if not answer:
        return {
            "ok": False,
            "error": "stream_truncated" if not stream_end_seen else "empty_response",
            "detail": (
                "stream completed but no partial_answer chunks received"
                if stream_end_seen
                else "stream closed before any partial_answer chunks arrived"
            ),
            "elapsed_s": round(elapsed, 2),
        }

    # If the stream ended in an error chunk but we already have substantive
    # content, surface as a partial-success with is_uncertain flipped.
    if error_reason:
        is_uncertain = True
        log.info(
            "Kapa stream: partial success — error_reason=%s answer_len=%d",
            error_reason,
            len(answer),
        )

    content_out, source_count = _assemble_content(answer, raw_sources)

    log.info(
        "Kapa stream OK project=%s elapsed=%.2fs chunks=%d sources=%d "
        "uncertain=%s stream_end=%s",
        project_id,
        elapsed,
        len(answer_parts),
        source_count,
        is_uncertain,
        stream_end_seen,
    )

    return {
        "ok": True,
        "content": content_out,
        "is_uncertain": is_uncertain,
        "source_count": source_count,
        "elapsed_s": round(elapsed, 2),
        "http_status": status,
    }
