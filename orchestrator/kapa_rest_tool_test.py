"""Tests for orchestrator/kapa_rest_tool.py (streaming endpoint).

Fully mocked — no real Kapa API calls. Patches the imported
``request.urlopen`` binding so the streaming consumer reads from a
fake ``read(n)``-shaped response.

Covers:
  * Guard clauses (empty query, missing API key, missing project_id).
  * Happy path with answer + sources + ``stream_end: true``.
  * No-sources case (LLM-only answer).
  * Mid-stream ``error`` chunk both with and without prior content
    (partial-success vs hard-failure paths).
  * Network drop after partial content (partial success).
  * Network drop before any content (stream_truncated).
  * Single bad-JSON chunk skipped; too many bad chunks aborts.
  * Pre-stream HTTPError (401) surfaces as ``http_401``.
  * Pre-stream ``urlopen`` timeout surfaces as ``network``.
  * Source dedup across multiple chunks.
  * Trailing chunk without final delimiter still parsed.
  * Streaming URL + ``Accept: application/json`` header land on the
    outbound request (NOT ``text/event-stream`` — Kapa 406s that despite
    the stream body framing).

Run:
    cd orchestrator && python3 -m pytest kapa_rest_tool_test.py -q
"""

from __future__ import annotations

import io
import json
import os
import socket
from typing import Iterable
from unittest.mock import patch

from urllib import error as urllib_error

# Required env vars for config.py to import without raising. We use
# setdefault so a real .env (when present) takes precedence. Mirrors
# compresr_client_test.py:34-46 — repo convention.
for _key, _value in {
    "ANTHROPIC_API_KEY": "sk-ant-test",
    "SLACK_BOT_TOKEN": "xoxb-test",
    "SLACK_APP_TOKEN": "xapp-test",
    "SLACK_CHANNEL_ID": "C0TEST",
    "ENVIRONMENT_ID": "env_test",
    "DREAM_AGENT_ID": "agent_test_dream",
    "COORDINATOR_ID": "agent_test_coord",
    "QUICK_AGENT_ID": "agent_test_quick",
    "METHODOLOGY_STORE_ID": "memstore_test_m",
    "HEALTH_STORE_ID": "memstore_test_h",
}.items():
    os.environ.setdefault(_key, _value)


import kapa_rest_tool  # noqa: E402 — env preamble must run first


# ---------------------------------------------------------------------------
# Fake streaming response — mirrors urllib's response object surface
# ---------------------------------------------------------------------------


class _FakeStreamResponse:
    """Stand-in for the object returned by ``urllib.request.urlopen``.

    Supports the context-manager protocol (``with resp:``) and serves
    bytes incrementally from a queue of pre-encoded chunks so the
    streaming consumer's buffer + split logic is exercised for real.

    ``raise_at_read`` lets a test inject a ``URLError`` / ``OSError``
    after N successful reads (mid-stream drop simulation).
    """

    def __init__(
        self,
        byte_chunks: Iterable[bytes],
        *,
        status: int = 200,
        raise_at_read: int | None = None,
        raise_exc: Exception | None = None,
    ):
        self._chunks = list(byte_chunks)
        self.status = status
        self._read_count = 0
        self._raise_at = raise_at_read
        self._raise_exc = raise_exc or urllib_error.URLError("simulated network drop")

    def read(self, n: int = -1) -> bytes:
        if self._raise_at is not None and self._read_count >= self._raise_at:
            raise self._raise_exc
        self._read_count += 1
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _encode_stream(chunks: list[dict], *, trailing_sep: bool = True) -> list[bytes]:
    """Encode a list of chunk-dicts as a list of byte chunks suitable for
    incremental reads, joined by Kapa's U+241E delimiter.

    Splits each chunk into a small initial fragment and a larger remainder
    so the streaming consumer's chunk-boundary logic is exercised even on
    short chunks. ``trailing_sep`` controls whether the final chunk has a
    closing delimiter (some servers omit it).
    """
    sep = "␞".encode("utf-8")
    parts: list[bytes] = []
    for i, c in enumerate(chunks):
        encoded = json.dumps(c).encode("utf-8")
        # Alternate single-byte split for the first half, full chunk for
        # the second half. This forces the consumer to handle a partial
        # buffer carrying across reads.
        if i % 2 == 0 and len(encoded) > 6:
            parts.append(encoded[:6])
            parts.append(encoded[6:])
        else:
            parts.append(encoded)
        is_last = i == len(chunks) - 1
        if not is_last or trailing_sep:
            parts.append(sep)
    return parts


def _partial_answer(text: str, *, stream_end: bool = False) -> dict:
    return {
        "chunk": {
            "type": "partial_answer",
            "content": {"text": text},
            "stream_end": stream_end,
        }
    }


def _relevant_sources(items: list[dict], *, stream_end: bool = False) -> dict:
    return {
        "chunk": {
            "type": "relevant_sources",
            "content": {"relevant_sources": items},
            "stream_end": stream_end,
        }
    }


def _identifiers(thread_id: str, *, stream_end: bool = True) -> dict:
    return {
        "chunk": {
            "type": "identifiers",
            "content": {"thread_id": thread_id, "question_answer_id": "qa-xyz"},
            "stream_end": stream_end,
        }
    }


def _error_chunk(reason: str, *, stream_end: bool = True) -> dict:
    return {
        "chunk": {
            "type": "error",
            "content": {"reason": reason},
            "stream_end": stream_end,
        }
    }


def _metadata_uncertain(*, stream_end: bool = False) -> dict:
    return {
        "chunk": {
            "type": "metadata",
            "content": {"is_uncertain": True},
            "stream_end": stream_end,
        }
    }


def _call_search(byte_chunks: list[bytes], **fake_kwargs) -> dict:
    """Patch ``urlopen`` to return a fake stream response and run search_kapa."""
    fake = _FakeStreamResponse(byte_chunks, **fake_kwargs)
    with patch("kapa_rest_tool.request.urlopen", return_value=fake):
        return kapa_rest_tool.search_kapa(
            query="What is SOA?",
            api_key="key",
            project_id="proj",
        )


# ---------------------------------------------------------------------------
# Guard clauses
# ---------------------------------------------------------------------------


def test_empty_query_short_circuits():
    result = kapa_rest_tool.search_kapa(query="   ", api_key="k", project_id="p")
    assert result["ok"] is False
    assert result["error"] == "invalid_query"


def test_missing_api_key_short_circuits():
    result = kapa_rest_tool.search_kapa(query="x", api_key="", project_id="p")
    assert result["ok"] is False
    assert result["error"] == "missing_api_key"


def test_missing_project_id_short_circuits():
    result = kapa_rest_tool.search_kapa(query="x", api_key="k", project_id="")
    assert result["ok"] is False
    assert result["error"] == "missing_project_id"


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_happy_path_streams_answer_with_sources():
    chunks = [
        _partial_answer("Inventory "),
        _partial_answer("management system "),
        _partial_answer("for SMBs."),
        _relevant_sources(
            [
                {
                    "title": "Acme Inventory Overview",
                    "source_url": "https://help.acme.com/overview",
                    "contains_internal_data": False,
                },
                {
                    "title": "SOA Module",
                    "source_url": "https://help.acme.com/soa",
                    "contains_internal_data": False,
                },
            ]
        ),
        _identifiers("thread-abc", stream_end=True),
    ]
    result = _call_search(_encode_stream(chunks))
    assert result["ok"] is True
    assert "Inventory management system for SMBs." in result["content"]
    assert "Sources:" in result["content"]
    assert "Acme Inventory Overview" in result["content"]
    assert "SOA Module" in result["content"]
    assert result["source_count"] == 2
    assert result["is_uncertain"] is False
    assert result["http_status"] == 200


def test_no_sources_chunk_omits_sources_block():
    chunks = [
        _partial_answer("Pure LLM answer."),
        _identifiers("thread-xyz", stream_end=True),
    ]
    result = _call_search(_encode_stream(chunks))
    assert result["ok"] is True
    assert result["content"] == "Pure LLM answer."
    assert "Sources:" not in result["content"]
    assert result["source_count"] == 0


def test_metadata_chunk_flips_is_uncertain():
    chunks = [
        _partial_answer("Speculative answer."),
        _metadata_uncertain(),
        _identifiers("t-1", stream_end=True),
    ]
    result = _call_search(_encode_stream(chunks))
    assert result["ok"] is True
    assert result["is_uncertain"] is True


def test_trailing_chunk_without_final_delimiter_still_parsed():
    """Some servers omit the closing ␞ on the last chunk."""
    chunks = [
        _partial_answer("Answer body"),
        _identifiers("t-2", stream_end=True),
    ]
    result = _call_search(_encode_stream(chunks, trailing_sep=False))
    assert result["ok"] is True
    assert "Answer body" in result["content"]


# ---------------------------------------------------------------------------
# Error chunks
# ---------------------------------------------------------------------------


def test_mid_stream_error_with_substantive_prior_content_returns_partial_success():
    pre = "x" * 150  # > MIN_CONTENT_FOR_PARTIAL_SUCCESS (100)
    chunks = [
        _partial_answer(pre),
        _error_chunk("internal_synthesis_failed"),
    ]
    result = _call_search(_encode_stream(chunks))
    assert result["ok"] is True
    assert result["is_uncertain"] is True
    assert pre in result["content"]


def test_mid_stream_error_with_no_prior_content_returns_kapa_error():
    chunks = [_error_chunk("rate_limited")]
    result = _call_search(_encode_stream(chunks))
    assert result["ok"] is False
    assert result["error"] == "kapa_error"
    assert "rate_limited" in result["detail"]


# ---------------------------------------------------------------------------
# Network drops mid-stream
# ---------------------------------------------------------------------------


def test_network_drop_after_partial_returns_partial_success():
    chunks = [_partial_answer("x" * 200)]
    byte_chunks = _encode_stream(chunks, trailing_sep=True)
    # Let the consumer drain the first read, then raise on the next.
    result = _call_search(byte_chunks[:2] + [b""], raise_at_read=2)
    # We deliver the content with no terminating drop — should be ok=True.
    assert result["ok"] is True
    assert "x" * 200 in result["content"]


def test_network_drop_before_any_content_returns_stream_truncated():
    # No bytes arrive at all before the network drops.
    result = _call_search(
        [],
        raise_at_read=0,
        raise_exc=urllib_error.URLError("connection reset"),
    )
    # Without any bytes at all, the consumer breaks out with empty
    # answer_parts — stream_truncated path.
    assert result["ok"] is False
    assert result["error"] in ("stream_truncated", "network")


# ---------------------------------------------------------------------------
# JSON decode resilience
# ---------------------------------------------------------------------------


def test_single_bad_chunk_skipped_rest_assembles():
    sep = "␞".encode("utf-8")
    parts = [
        json.dumps(_partial_answer("good ")).encode("utf-8"),
        sep,
        b"{not valid json",
        sep,
        json.dumps(_partial_answer("answer")).encode("utf-8"),
        sep,
        json.dumps(_identifiers("t", stream_end=True)).encode("utf-8"),
    ]
    result = _call_search(parts)
    assert result["ok"] is True
    assert "good answer" in result["content"]


def test_too_many_bad_chunks_aborts_with_decode_failed():
    sep = "␞".encode("utf-8")
    parts: list[bytes] = []
    for _ in range(kapa_rest_tool.MAX_BAD_CHUNKS + 2):
        parts.append(b"{garbage")
        parts.append(sep)
    result = _call_search(parts)
    assert result["ok"] is False
    assert result["error"] == "stream_decode_failed"


# ---------------------------------------------------------------------------
# Pre-stream HTTP / network errors
# ---------------------------------------------------------------------------


def test_http_401_pre_stream_surfaces_http_error():
    err = urllib_error.HTTPError(
        url="https://api.kapa.ai/x",
        code=401,
        msg="Unauthorized",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(b'{"detail":"bad key"}'),
    )
    with patch("kapa_rest_tool.request.urlopen", side_effect=err):
        result = kapa_rest_tool.search_kapa(query="x", api_key="k", project_id="p")
    assert result["ok"] is False
    assert result["error"] == "http_401"
    assert "bad key" in result["detail"]


def test_pre_stream_timeout_surfaces_network_error():
    with patch(
        "kapa_rest_tool.request.urlopen",
        side_effect=socket.timeout("connect timed out"),
    ):
        result = kapa_rest_tool.search_kapa(query="x", api_key="k", project_id="p")
    assert result["ok"] is False
    assert result["error"] == "network"


# ---------------------------------------------------------------------------
# Source dedup
# ---------------------------------------------------------------------------


def test_source_dedup_across_chunks_keeps_longest_title():
    chunks = [
        _partial_answer("Body."),
        _relevant_sources(
            [
                {
                    "title": "Short",
                    "source_url": "https://example.com/page",
                    "contains_internal_data": False,
                },
                {
                    "title": "A much more descriptive title",
                    "source_url": "https://example.com/page",
                    "contains_internal_data": False,
                },
                {
                    "title": "Other",
                    "source_url": "https://example.com/other",
                    "contains_internal_data": False,
                },
            ]
        ),
        _identifiers("t", stream_end=True),
    ]
    result = _call_search(_encode_stream(chunks))
    assert result["ok"] is True
    assert result["source_count"] == 2
    assert "A much more descriptive title" in result["content"]
    assert "[Short]" not in result["content"]


# ---------------------------------------------------------------------------
# Outbound request shape — confirms streaming URL + Accept header
# ---------------------------------------------------------------------------


def test_outbound_request_uses_streaming_url_and_accept_header():
    captured: dict = {}

    def _capture(req, timeout):  # noqa: ARG001
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        return _FakeStreamResponse(
            _encode_stream(
                [
                    _partial_answer("ok"),
                    _identifiers("t", stream_end=True),
                ]
            )
        )

    with patch("kapa_rest_tool.request.urlopen", side_effect=_capture):
        kapa_rest_tool.search_kapa(query="x", api_key="k", project_id="proj-id")

    assert captured["url"].endswith("/chat/stream/")
    assert "proj-id" in captured["url"]
    # urllib title-cases header keys on Request — match case-insensitive.
    norm = {k.lower(): v for k, v in captured["headers"].items()}
    # MUST be application/json, NOT text/event-stream — Kapa 406s the
    # latter despite the response body being a U+241E-delimited stream.
    # Live repro 2026-05-14 19:40 PT (sesn_EXAMPLE).
    assert norm["accept"] == "application/json"
    assert norm["x-api-key"] == "k"
    assert norm["content-type"] == "application/json"


# ---------------------------------------------------------------------------
# SSE prefix tolerance (codex review 2026-05-14)
# ---------------------------------------------------------------------------


def test_sse_data_prefix_stripped_before_json_decode():
    """Kapa's published docs reference ``data: {...}`` SSE framing even
    though the live wire format is raw JSON delimited by U+241E. Strip
    the prefix defensively so a future framing switch on Kapa's side
    doesn't break us silently."""
    sep = "␞".encode("utf-8")
    parts = [
        b"data: " + json.dumps(_partial_answer("hello ")).encode("utf-8"),
        sep,
        b"data: " + json.dumps(_partial_answer("world.")).encode("utf-8"),
        sep,
        b"data: " + json.dumps(_identifiers("t", stream_end=True)).encode("utf-8"),
    ]
    result = _call_search(parts)
    assert result["ok"] is True
    assert "hello world." in result["content"]


def test_mixed_sse_and_raw_chunks_both_parse():
    """Half the segments arrive with SSE prefix, half raw — both must parse."""
    sep = "␞".encode("utf-8")
    parts = [
        json.dumps(_partial_answer("raw ")).encode("utf-8"),
        sep,
        b"data: " + json.dumps(_partial_answer("framed ")).encode("utf-8"),
        sep,
        json.dumps(_partial_answer("end.")).encode("utf-8"),
        sep,
        json.dumps(_identifiers("t", stream_end=True)).encode("utf-8"),
    ]
    result = _call_search(parts)
    assert result["ok"] is True
    assert "raw framed end." in result["content"]


# ---------------------------------------------------------------------------
# Delimiter regression guard — keep this loud so a future "split on \x1e"
# regression fails immediately.
# ---------------------------------------------------------------------------


def test_delimiter_is_u241e_not_u001e():
    """Kapa uses the VISIBLE GLYPH U+241E (UTF-8 \\xe2\\x90\\x9e), NOT the
    control byte U+001E. Live probe 2026-05-14 confirmed splitting on
    \\x1e yields zero chunks."""
    assert kapa_rest_tool.STREAM_SEP == "␞"
    assert kapa_rest_tool.STREAM_SEP_BYTES == b"\xe2\x90\x9e"
    assert kapa_rest_tool.STREAM_SEP_BYTES != b"\x1e"


# ---------------------------------------------------------------------------
# Rate limiter integration (Layer 2)
# ---------------------------------------------------------------------------


def test_rate_limiter_acquire_called_before_http_fire(monkeypatch):
    """Confirm ``kapa_rate_limiter.acquire`` runs BEFORE ``urlopen``.

    The limiter must block on the bucket FIRST so we never burn an HTTP
    request when we're already over the cap. We record the order of
    calls into a list and assert ``acquire`` precedes ``urlopen``.
    Also verifies the ``caller`` tag is plumbed through to the limiter.
    """
    import kapa_rate_limiter

    # Limiter must be enabled for this test (autouse cleanups in the
    # other test file don't apply here — set explicitly).
    monkeypatch.setenv("KAPA_RATE_LIMIT_ENABLED", "true")

    call_order: list[tuple[str, str]] = []

    def fake_acquire(caller: str = "default") -> None:
        call_order.append(("acquire", caller))

    def fake_urlopen(req, timeout):  # noqa: ARG001
        call_order.append(("urlopen", req.full_url))
        return _FakeStreamResponse(
            _encode_stream(
                [
                    _partial_answer("ok"),
                    _identifiers("t", stream_end=True),
                ]
            )
        )

    monkeypatch.setattr(kapa_rate_limiter, "acquire", fake_acquire)
    monkeypatch.setattr("kapa_rest_tool.request.urlopen", fake_urlopen)

    result = kapa_rest_tool.search_kapa(
        query="What is SOA?",
        api_key="k",
        project_id="proj",
        caller="sesn_EXAMPLE",
    )
    assert result["ok"] is True
    # Both calls happened, and acquire was first.
    kinds = [c[0] for c in call_order]
    assert kinds == ["acquire", "urlopen"], (
        f"expected acquire before urlopen, got {kinds!r}"
    )
    # Caller tag plumbed through.
    assert call_order[0][1] == "sesn_EXAMPLE"


def test_rate_limiter_blocks_before_http_fire_when_bucket_empty(monkeypatch):
    """With an empty bucket, the limiter waits before any HTTP request runs.

    We mock the limiter's ``acquire`` to record that it blocked, and
    mock the HTTP call to assert urlopen never ran during the block.
    Order is what matters — the bucket logic itself is covered in
    kapa_rate_limiter_test.py.
    """
    import kapa_rate_limiter

    monkeypatch.setenv("KAPA_RATE_LIMIT_ENABLED", "true")

    events: list[str] = []

    def blocking_acquire(caller: str = "default") -> None:  # noqa: ARG001
        # Simulate the limiter blocking on an empty bucket.
        events.append("limiter_block_start")
        events.append("limiter_block_end")

    def fake_urlopen(req, timeout):  # noqa: ARG001
        events.append("urlopen_called")
        return _FakeStreamResponse(
            _encode_stream(
                [
                    _partial_answer("done"),
                    _identifiers("t", stream_end=True),
                ]
            )
        )

    monkeypatch.setattr(kapa_rate_limiter, "acquire", blocking_acquire)
    monkeypatch.setattr("kapa_rest_tool.request.urlopen", fake_urlopen)

    result = kapa_rest_tool.search_kapa(
        query="x", api_key="k", project_id="p", caller="caller-x"
    )
    assert result["ok"] is True
    # Limiter block fully resolved before urlopen ran.
    assert events == [
        "limiter_block_start",
        "limiter_block_end",
        "urlopen_called",
    ]
