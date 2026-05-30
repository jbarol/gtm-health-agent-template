"""Tests for orchestrator/learnings_compactor.py.

Run:
    cd orchestrator && python3 -m pytest learnings_compactor_test.py -v
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

# Required env vars for config.py to import without raising. setdefault means
# a real .env wins. Mirrors self_improve_test.py.
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


# ── Helpers ─────────────────────────────────────────────────────────────────


def _make_conflict_error(memory_id: str, path: str):
    """Build a real anthropic.ConflictError with the body shape the API returns."""
    import anthropic
    import httpx

    request = httpx.Request("POST", "https://api.anthropic.com/v1/memories")
    response = httpx.Response(409, request=request)
    body = {
        "error": {
            "type": "memory_path_conflict_error",
            "message": (
                f"path `{path}` is already used by `{memory_id}`; "
                "use update to modify it"
            ),
            "conflicting_memory_id": memory_id,
            "conflicting_path": path,
        }
    }
    return anthropic.ConflictError(
        "409 conflict",
        response=response,
        body=body,
    )


def _make_messages_response(text: str):
    """Build a minimal MagicMock that quacks like an Anthropic Messages response."""
    block = MagicMock()
    block.type = "text"
    block.text = text
    response = MagicMock()
    response.content = [block]
    usage = MagicMock()
    usage.input_tokens = 100
    usage.output_tokens = 50
    usage.cache_read_input_tokens = 0
    usage.cache_creation_input_tokens = 0
    response.usage = usage
    return response


def _patch_source(content: str):
    """Patch the memory list+retrieve so _read_source_learnings returns ``content``."""
    import learnings_compactor

    fake_memory = MagicMock()
    fake_memory.id = "mem_source_id"
    fake_memory.path = learnings_compactor._LEARNINGS_SOURCE_PATH

    list_resp = MagicMock()
    list_resp.data = [fake_memory]

    retrieve_resp = MagicMock()
    retrieve_resp.content = content

    mock_memories = MagicMock()
    mock_memories.list.return_value = list_resp
    mock_memories.retrieve.return_value = retrieve_resp
    return mock_memories


# ── Test 1: happy path — read memory, call Sonnet, write compact ────────────


def test_compact_learnings_happy_path_creates_compact():
    """compact_learnings reads /system/learnings.md, calls Sonnet, and writes the
    result to /system/learnings_compact.md."""
    import learnings_compactor

    source_content = (
        "# Session Learnings — sesn_EXAMPLE (2026-05-10)\n\n"
        "## SOQL CASE used\n"
        "- **Root cause:** Iteration 1 schema gap\n"
        "- **Memory note:** Never use CASE in SOQL — Salesforce rejects it\n"
    )

    mock_memories = _patch_source(source_content)
    mock_messages = MagicMock()
    sonnet_text = (
        "# Learnings — Compact\n\n"
        "## dump_sf_query\n"
        "- Never use CASE in SOQL — Salesforce rejects it.\n"
    )
    # compact_learnings strips leading/trailing whitespace from the model
    # response — replicate that here so length asserts match the persisted
    # content, not the raw model output.
    expected_compact = sonnet_text.strip()
    mock_messages.create.return_value = _make_messages_response(sonnet_text)

    with patch.object(
        learnings_compactor.client.beta.memory_stores, "memories", mock_memories
    ):
        with patch.object(learnings_compactor.client, "messages", mock_messages):
            input_chars, output_chars, tokens, success = (
                learnings_compactor.compact_learnings()
            )

    assert success is True
    assert input_chars == len(source_content)
    assert output_chars == len(expected_compact)
    assert tokens == 150  # input + output from _make_messages_response

    # Sonnet was called once with cache_control on the system block.
    assert mock_messages.create.call_count == 1
    create_kwargs = mock_messages.create.call_args.kwargs
    assert create_kwargs["model"] == "claude-sonnet-4-6"
    system = create_kwargs["system"]
    assert isinstance(system, list) and len(system) == 1
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert "rules-by-tool" in system[0]["text"].lower()

    # memories.create was called once with the compact path + content.
    assert mock_memories.create.call_count == 1
    create_args = mock_memories.create.call_args
    assert create_args.kwargs["path"] == "/system/learnings_compact.md"
    assert create_args.kwargs["content"] == expected_compact


# ── Test 2: 409 conflict path — fall back to memories.update ────────────────


def test_compact_learnings_falls_back_to_update_on_conflict():
    """A second run on the same day must NOT raise. compact_learnings must
    catch the 409, pull ``conflicting_memory_id``, and call ``memories.update``
    with the same content the create attempted."""
    import learnings_compactor

    source_content = (
        "# Session Learnings — sesn_EXAMPLE (2026-05-11)\n\n"
        "## Stale schema cache\n"
        "- **Memory note:** Run describeSObject-equivalent before assuming fields\n"
    )

    sonnet_text = (
        "# Learnings — Compact\n\n"
        "## dump_sf_query\n"
        "- Always run schema discovery before assuming custom-field names.\n"
    )
    expected_compact = sonnet_text.strip()  # mirror compact_learnings strip

    mock_memories = _patch_source(source_content)
    conflict = _make_conflict_error(
        "mem_012ABCDEFGHIJK", "/system/learnings_compact.md"
    )
    mock_memories.create.side_effect = conflict
    mock_memories.update.return_value = MagicMock(id="mem_012ABCDEFGHIJK")

    mock_messages = MagicMock()
    mock_messages.create.return_value = _make_messages_response(sonnet_text)

    with patch.object(
        learnings_compactor.client.beta.memory_stores, "memories", mock_memories
    ):
        with patch.object(learnings_compactor.client, "messages", mock_messages):
            _, _, _, success = learnings_compactor.compact_learnings()

    assert success is True

    # create was tried once and 409'd.
    assert mock_memories.create.call_count == 1
    # update was called with the conflicting_memory_id + same content.
    assert mock_memories.update.call_count == 1
    update_args = mock_memories.update.call_args
    assert update_args.args[0] == "mem_012ABCDEFGHIJK"
    assert update_args.kwargs["memory_store_id"] == learnings_compactor.HEALTH_STORE_ID
    assert update_args.kwargs["content"] == expected_compact


# ── Test 3: Sonnet API failure — return success=False, no exception ─────────


def test_compact_learnings_swallows_sonnet_api_failure():
    """When the Messages API raises, compact_learnings must return
    success=False rather than crashing the cron thread. memories.create
    must never be called — there's nothing to write."""
    import learnings_compactor

    source_content = (
        "# Session Learnings — sesn_EXAMPLE (2026-05-12)\n\nSomething went wrong.\n"
    )

    mock_memories = _patch_source(source_content)
    mock_messages = MagicMock()
    mock_messages.create.side_effect = RuntimeError("Anthropic 503")

    # Stub send_notification so the test doesn't depend on slack_bot wiring.
    with patch.object(
        learnings_compactor.client.beta.memory_stores, "memories", mock_memories
    ):
        with patch.object(learnings_compactor.client, "messages", mock_messages):
            with patch.dict(
                "sys.modules",
                {"slack_bot": MagicMock(send_notification=MagicMock())},
            ):
                input_chars, output_chars, tokens, success = (
                    learnings_compactor.compact_learnings()
                )

    assert success is False
    assert input_chars == len(source_content)
    assert output_chars == 0
    assert tokens == 0

    # Sonnet was called and raised.
    assert mock_messages.create.call_count == 1
    # No write attempted.
    assert mock_memories.create.call_count == 0


# ── Test 4: exact compact path — must be /system/learnings_compact.md ───────


def test_compact_learnings_writes_to_exact_compact_path():
    """The output path is contractual — every Specialist + Coordinator prompt
    reads from this exact location. If the path drifts the readback breaks
    silently. Locked here so a refactor catches it."""
    import learnings_compactor

    assert learnings_compactor._LEARNINGS_COMPACT_PATH == (
        "/system/learnings_compact.md"
    )

    source_content = "# Session Learnings — sesn_EXAMPLE\n\nNote: do X.\n"
    expected_compact = "## db_query\n- Always parameterize date ranges.\n"

    mock_memories = _patch_source(source_content)
    mock_messages = MagicMock()
    mock_messages.create.return_value = _make_messages_response(expected_compact)

    with patch.object(
        learnings_compactor.client.beta.memory_stores, "memories", mock_memories
    ):
        with patch.object(learnings_compactor.client, "messages", mock_messages):
            learnings_compactor.compact_learnings()

    assert mock_memories.create.call_count == 1
    create_args = mock_memories.create.call_args
    # The path is the single thing the agent prompt readback depends on —
    # locked verbatim.
    assert create_args.kwargs["path"] == "/system/learnings_compact.md"


# ── Test 5: empty source — writes a placeholder, does not call Sonnet ───────


def test_compact_learnings_writes_placeholder_on_empty_source():
    """First nightly run before any sessions have flushed learnings. The
    function should NOT call Sonnet (waste of tokens on nothing) but SHOULD
    write a placeholder so the agent readback finds a file instead of failing
    open and probing the filesystem."""
    import learnings_compactor

    # Empty source content.
    mock_memories = _patch_source("")
    mock_messages = MagicMock()

    with patch.object(
        learnings_compactor.client.beta.memory_stores, "memories", mock_memories
    ):
        with patch.object(learnings_compactor.client, "messages", mock_messages):
            _, _, _, success = learnings_compactor.compact_learnings()

    assert success is True
    # Sonnet was NOT called.
    assert mock_messages.create.call_count == 0
    # A placeholder was written to the compact path.
    assert mock_memories.create.call_count == 1
    create_args = mock_memories.create.call_args
    assert create_args.kwargs["path"] == "/system/learnings_compact.md"
    assert "clean slate" in create_args.kwargs["content"].lower()


# ── Test 6: transient read failure preserves any prior compact ──────────────


def test_compact_learnings_transient_read_failure_does_not_overwrite():
    """If _read_source_learnings raises a transient error, the function MUST
    return without writing anything — overwriting the existing compact via
    the empty-source path would destroy real content from prior days.

    Regression for the 2026-05-14 review P1: previously _read_source_learnings
    returned "" on any exception, which then routed to the placeholder write
    path, which used upsert semantics. A transient Anthropic API blip on the
    read side could silently clobber a real compact file with the "clean
    slate" placeholder.
    """
    import learnings_compactor

    # Patch _read_source_learnings to return None (signaling transient failure).
    # Patch the memory client so we can assert no create/update fired.
    mock_memories = MagicMock()
    mock_messages = MagicMock()

    with patch.object(learnings_compactor, "_read_source_learnings", return_value=None):
        with patch.object(
            learnings_compactor.client.beta.memory_stores, "memories", mock_memories
        ):
            with patch.object(learnings_compactor.client, "messages", mock_messages):
                _, _, _, success = learnings_compactor.compact_learnings()

    # Success=False signals a failed read; next nightly run will retry.
    assert success is False
    # CRITICAL: nothing was written. Existing compact (if any) survives intact.
    assert mock_memories.create.call_count == 0
    assert mock_memories.update.call_count == 0
    # Sonnet was NOT called on this path.
    assert mock_messages.create.call_count == 0


# ── Test 7: empty source does NOT overwrite an existing compact ─────────────


def test_compact_learnings_empty_source_preserves_existing_compact():
    """When the source is genuinely empty AND a prior compact already exists
    (e.g. from a real run yesterday), the placeholder write path must NOT
    overwrite it. The create-only helper catches the 409 and leaves the
    existing content alone.

    Confirms the 2026-05-14 fix: ``_create_compact_if_missing`` returns
    False on ConflictError without calling update — the prior compact is
    treated as authoritative.
    """
    import learnings_compactor

    mock_memories = _patch_source("")  # source is empty

    # First create call (for the compact path) raises ConflictError —
    # simulating that a real compact already exists.
    conflict = _make_conflict_error(
        memory_id="mem_REAL_COMPACT_FROM_YESTERDAY",
        path="/system/learnings_compact.md",
    )
    mock_memories.create.side_effect = conflict

    mock_messages = MagicMock()

    with patch.object(
        learnings_compactor.client.beta.memory_stores, "memories", mock_memories
    ):
        with patch.object(learnings_compactor.client, "messages", mock_messages):
            _, _, _, success = learnings_compactor.compact_learnings()

    assert success is True  # System is healthy, just nothing new.
    # create was attempted exactly once (for the placeholder).
    assert mock_memories.create.call_count == 1
    # CRITICAL: update was NEVER called. The prior compact survives.
    assert mock_memories.update.call_count == 0
    # Sonnet was NOT called.
    assert mock_messages.create.call_count == 0
