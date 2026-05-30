"""Tests for self_improve F5 trigger registry + notify formatting.

The full check_for_updates flow hits the network; tests focus on the
testable units: trigger registry contents and notification formatting.

Run:
    cd orchestrator && python3 -m pytest self_improve_test.py
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

# Required env vars for config.py to import without raising. setdefault means
# a real .env wins.
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


def test_trigger_pages_has_structured_outputs():
    """F5: structured-outputs slug is registered and points at Plan #34."""
    from self_improve import TRIGGER_PAGES

    assert "structured-outputs" in TRIGGER_PAGES
    trig = TRIGGER_PAGES["structured-outputs"]
    assert trig["plan_id"] == 34
    assert "structured outputs" in trig["title"].lower()
    assert "Plan #34" in trig["action"] or "plan #34" in trig["action"].lower()


def test_trigger_pages_covers_alternative_slugs():
    """Multiple candidate slugs all route to Plan #34 — Anthropic's URL may differ."""
    from self_improve import TRIGGER_PAGES

    for slug in ("structured-outputs", "response-format", "json-mode"):
        assert slug in TRIGGER_PAGES, f"slug {slug!r} not registered"
        assert TRIGGER_PAGES[slug]["plan_id"] == 34


def test_trigger_pages_in_docs_pages():
    """Every TRIGGER_PAGES key must be tracked by the crawler — otherwise the
    transition will never be observed."""
    from self_improve import DOCS_PAGES, TRIGGER_PAGES

    missing = [page for page in TRIGGER_PAGES if page not in DOCS_PAGES]
    assert not missing, f"Trigger pages not in crawler: {missing}"


def test_notify_user_without_triggered_uses_normal_header():
    from self_improve import _notify_user

    sent_dms = []
    with patch("self_improve.send_dm") as mock_dm:
        mock_dm.side_effect = lambda uid, body: sent_dms.append((uid, body))
        with patch("self_improve.SLACK_NOTIFY_USER_IDS", ["U_TEST"]):
            _notify_user("summary text", ["overview"], [])

    assert len(sent_dms) == 1
    body = sent_dms[0][1]
    assert ":sparkles:" in body
    assert "TRIGGER" not in body
    assert "Changed pages:" in body
    assert "summary text" in body


# ── Rename: "Self-Improvement Report" → "Managed Agents Docs — Daily Diff" ───


def test_notify_user_title_uses_managed_agents_docs_diff():
    """The DM title must reflect what the report actually is — a daily diff
    of Anthropic's Managed Agents documentation. The old
    "Self-Improvement Report" wording was inherited from the orchestration
    layer and didn't describe the deliverable."""
    from self_improve import _notify_user

    sent_dms = []
    with patch("self_improve.send_dm") as mock_dm:
        mock_dm.side_effect = lambda uid, body: sent_dms.append((uid, body))
        with patch("self_improve.SLACK_NOTIFY_USER_IDS", ["U_TEST"]):
            _notify_user("summary text", ["overview"], [])

    body = sent_dms[0][1]
    assert "Managed Agents Docs — Daily Diff" in body
    # The legacy phrasing must NOT survive.
    assert "Self-Improvement Report" not in body


def test_notify_user_with_trigger_also_uses_renamed_subheader():
    """The F5 trigger banner is followed by the docs-diff sub-header; that
    sub-header is the same rename surface."""
    from self_improve import _notify_user, TRIGGER_PAGES

    sent_dms = []
    triggered = [("structured-outputs", TRIGGER_PAGES["structured-outputs"])]
    with patch("self_improve.send_dm") as mock_dm:
        mock_dm.side_effect = lambda uid, body: sent_dms.append((uid, body))
        with patch("self_improve.SLACK_NOTIFY_USER_IDS", ["U_TEST"]):
            _notify_user("summary", ["structured-outputs"], [], triggered=triggered)

    body = sent_dms[0][1]
    assert "Managed Agents Docs — Daily Diff" in body
    assert "Self-Improvement Report" not in body


# ── Slack mrkdwn: bullets + headers in the docs-diff body ───────────────────


def test_notify_user_renders_dash_bullets_as_unicode_bullets():
    """``_notify_user`` runs the LLM summary through ``slack_bot._md_to_slack``.
    Dash bullets must come out as ``•`` glyphs so Slack renders them as a
    bulleted list instead of a literal dash."""
    from self_improve import _notify_user

    sent_dms = []
    summary_with_bullets = (
        "## What changed\n"
        "- New structured outputs feature\n"
        "- Updated session lifecycle\n"
    )
    with patch("self_improve.send_dm") as mock_dm:
        mock_dm.side_effect = lambda uid, body: sent_dms.append((uid, body))
        with patch("self_improve.SLACK_NOTIFY_USER_IDS", ["U_TEST"]):
            _notify_user(summary_with_bullets, ["sessions"], [])

    body = sent_dms[0][1]
    # The LLM-written dash bullets should be rewritten to unicode bullets.
    assert "• New structured outputs feature" in body
    assert "• Updated session lifecycle" in body
    # The literal markdown header must be gone.
    assert "## What changed" not in body
    assert "*What changed*" in body


# ── Persisted snapshot baseline (Investigation: v0 vs. real diff) ──────────


def test_load_state_prefers_database_when_available():
    """When DATABASE_URL is set, ``_load_state`` reads hashes from the
    persisted snapshot table. This is the fix for the v0-baseline bug: on
    Railway, /tmp is wiped each deploy, so the only reliable baseline lives
    in Postgres."""
    import self_improve

    fake_hashes = {
        "overview": "abc123",
        "sessions": "def456",
    }
    fake_db = MagicMock()
    fake_db.DATABASE_URL = "postgres://test"
    fake_db.load_managed_agents_doc_snapshots.return_value = fake_hashes

    with patch.dict("sys.modules", {"db_adapter": fake_db}):
        state = self_improve._load_state()

    assert state["hashes"] == fake_hashes
    # Non-empty hashes imply a prior run → ``is_first_run`` must be False so
    # F5 triggers fire on the next newly_published transition.
    assert state["last_run"] is not None


def test_load_state_returns_empty_first_run_when_database_empty(tmp_path):
    """No rows in the DB AND no /tmp file → truly first run, hashes empty."""
    import self_improve

    fake_db = MagicMock()
    fake_db.DATABASE_URL = "postgres://test"
    fake_db.load_managed_agents_doc_snapshots.return_value = {}

    # Point STATE_FILE at a path inside the pytest tmp dir that doesn't exist
    # so the filesystem fallback returns the empty baseline.
    nonexistent = tmp_path / "no_file_here.json"
    with patch.dict("sys.modules", {"db_adapter": fake_db}):
        with patch.object(self_improve, "STATE_FILE", nonexistent):
            with patch.object(self_improve, "STATE_DIR", tmp_path):
                state = self_improve._load_state()

    assert state["hashes"] == {}
    assert state["last_run"] is None


def test_load_state_falls_back_to_tmp_without_database(tmp_path):
    """Dev environments without DATABASE_URL still work via the filesystem
    cache. This is the legacy path; the DB-backed path is the production fix."""
    import json

    import self_improve

    fake_db = MagicMock()
    fake_db.DATABASE_URL = ""  # not set

    state_file = tmp_path / "doc_hashes.json"
    state_file.write_text(
        json.dumps({"hashes": {"overview": "old"}, "last_run": "2026-05-10"})
    )

    with patch.dict("sys.modules", {"db_adapter": fake_db}):
        with patch.object(self_improve, "STATE_FILE", state_file):
            with patch.object(self_improve, "STATE_DIR", tmp_path):
                state = self_improve._load_state()

    assert state["hashes"] == {"overview": "old"}
    assert state["last_run"] == "2026-05-10"


def test_save_state_writes_to_database_when_available(tmp_path):
    """The new persistence layer writes hashes through to Postgres so the
    next deploy still has the baseline."""
    import self_improve

    fake_db = MagicMock()
    fake_db.DATABASE_URL = "postgres://test"
    fake_db.save_managed_agents_doc_snapshots.return_value = True

    state = {
        "hashes": {"overview": "h1", "sessions": "h2"},
        "last_run": "2026-05-11",
    }

    state_file = tmp_path / "doc_hashes.json"
    with patch.dict("sys.modules", {"db_adapter": fake_db}):
        with patch.object(self_improve, "STATE_FILE", state_file):
            with patch.object(self_improve, "STATE_DIR", tmp_path):
                self_improve._save_state(state)

    fake_db.save_managed_agents_doc_snapshots.assert_called_once_with(
        {"overview": "h1", "sessions": "h2"}
    )
    # Filesystem belt-and-suspenders write still happens.
    assert state_file.exists()


def test_check_for_updates_with_persisted_baseline_reports_no_changes(tmp_path):
    """End-to-end-ish: when the DB has the *same* hashes that the next crawl
    produces, ``check_for_updates`` short-circuits with no DM. This is the
    behavior that was broken by the /tmp-wipe bug — the baseline was always
    empty on Railway, so every nightly run looked like "everything is new"."""
    import self_improve

    # Synthetic page that hashes deterministically to "stable_hash".
    fake_hashes = {page: "stable_hash" for page in self_improve.DOCS_PAGES}
    fake_db = MagicMock()
    fake_db.DATABASE_URL = "postgres://test"
    fake_db.load_managed_agents_doc_snapshots.return_value = fake_hashes
    fake_db.save_managed_agents_doc_snapshots.return_value = True

    state_file = tmp_path / "doc_hashes.json"
    with patch.dict("sys.modules", {"db_adapter": fake_db}):
        with (
            patch.object(self_improve, "STATE_FILE", state_file),
            patch.object(self_improve, "STATE_DIR", tmp_path),
            patch.object(self_improve, "_fetch_page", return_value="content"),
            patch.object(self_improve, "_hash_content", return_value="stable_hash"),
            patch.object(self_improve, "_analyze_changes") as mock_analyze,
            patch.object(self_improve, "_notify_user") as mock_notify,
        ):
            self_improve.check_for_updates()

    # No changes → no analysis, no DM. This is the contract the persisted
    # baseline restores.
    mock_analyze.assert_not_called()
    mock_notify.assert_not_called()


def test_notify_user_with_trigger_leads_with_alert():
    """F5: when triggered list is non-empty, the alert header is FIRST."""
    from self_improve import _notify_user, TRIGGER_PAGES

    sent_dms = []
    triggered = [("structured-outputs", TRIGGER_PAGES["structured-outputs"])]

    with patch("self_improve.send_dm") as mock_dm:
        mock_dm.side_effect = lambda uid, body: sent_dms.append((uid, body))
        with patch("self_improve.SLACK_NOTIFY_USER_IDS", ["U_TEST"]):
            _notify_user(
                "regular summary",
                ["structured-outputs"],
                [],
                triggered=triggered,
            )

    body = sent_dms[0][1]
    # Trigger banner must come first
    trigger_pos = body.find(":rotating_light:")
    sparkles_pos = body.find(":sparkles:")
    assert trigger_pos != -1, "trigger banner missing"
    assert sparkles_pos == -1 or trigger_pos < sparkles_pos, (
        "trigger banner must precede sparkles header"
    )
    # Plan reference present
    assert "Plan: #34" in body or "plan #34" in body.lower()
    # Page name appears
    assert "structured-outputs" in body


def test_notify_user_handles_multiple_triggers():
    from self_improve import _notify_user, TRIGGER_PAGES

    sent_dms = []
    triggered = [
        ("structured-outputs", TRIGGER_PAGES["structured-outputs"]),
        ("response-format", TRIGGER_PAGES["response-format"]),
    ]

    with patch("self_improve.send_dm") as mock_dm:
        mock_dm.side_effect = lambda uid, body: sent_dms.append((uid, body))
        with patch("self_improve.SLACK_NOTIFY_USER_IDS", ["U_TEST"]):
            _notify_user(
                "summary",
                ["structured-outputs", "response-format"],
                [],
                triggered=triggered,
            )

    body = sent_dms[0][1]
    assert "structured-outputs" in body
    assert "response-format" in body


# ── Compresr wiring (audit fix 2026-05-11) ──────────────────────────────────


def _build_fake_messages_response(text: str = "ok"):
    """SimpleNamespace shaped like an Anthropic Messages API response."""
    from types import SimpleNamespace

    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(
            input_tokens=100,
            output_tokens=50,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


def test_analyze_changes_calls_compress_prompt_with_espresso_v1():
    """``self_improve._analyze_changes`` must call ``compress_prompt`` with
    espresso_v1 / call_site='self_improve' / min_chars=12000 per CLAUDE.md
    and the audit at docs/proposals/compresr-audit-2026-05-11.md §1.

    This is the second Compresr integration site — the audit caught that
    self_improve was never wired despite Plan #37 calling it out as the
    primary expected savings driver. min_chars lowered from 20000 → 12000
    on 2026-05-14: today's payload is ~15K chars after the Anthropic Managed
    Agents docs reshuffle, so the old threshold always fell back as
    "below_min_chars" and compression never ran.
    """
    import self_improve

    fake_response = _build_fake_messages_response("analysis text")

    with (
        patch.object(self_improve, "compress_prompt") as mock_compress,
        patch.object(self_improve, "_fetch_page", return_value="page content"),
        patch.object(
            self_improve.client.messages, "create", return_value=fake_response
        ),
        # Force batch path off so the realtime fallback runs and hits compress_prompt.
        patch.object(self_improve.batch_runner, "submit_batch", return_value=None),
    ):
        mock_compress.return_value = "COMPRESSED-PAYLOAD"

        self_improve._analyze_changes(
            changed_pages=["overview", "sessions"],
            new_pages=[],
        )

    assert mock_compress.call_count == 1
    args, kwargs = mock_compress.call_args
    # First positional arg is the combined doc-page payload.
    assert args and isinstance(args[0], str)
    assert "page content" in args[0]
    # Kwargs must match the audit-fix contract.
    assert kwargs["model"] == "espresso_v1"
    assert kwargs["call_site"] == "self_improve"
    assert kwargs["min_chars"] == 12000
    # espresso_v1 is the general-purpose, no-query path — query MUST NOT be
    # passed.
    assert "query" not in kwargs or kwargs.get("query") is None


def test_analyze_changes_triggers_compression_for_13k_payload():
    """Regression test for the 2026-05-14 min_chars retune.

    A realistic post-reshuffle nightly payload is ~15K chars total. With the
    old threshold (20000) every call fell into the ``below_min_chars``
    fallback inside ``compress_prompt`` and compression never ran, defeating
    Plan #37's expected savings driver. With the new threshold (12000) a
    13K-char payload must reach ``compress_prompt`` and pass its
    ``min_chars`` gate, so the SDK actually gets a chance to compress.
    """
    import self_improve

    fake_response = _build_fake_messages_response("analysis text")
    # 13_000 chars / 2 pages → ~6_500 chars per page. _fetch_page truncates
    # at 5_000 inside _analyze_changes, so we hand back exactly 5_000 per
    # page; two pages → 10_000 chars of page content + the joining and
    # heading boilerplate puts the combined string just above 10_000. Use
    # three pages to comfortably clear the 12_000 floor.
    per_page = "x" * 5_000

    with (
        patch.object(self_improve, "compress_prompt") as mock_compress,
        patch.object(self_improve, "_fetch_page", return_value=per_page),
        patch.object(
            self_improve.client.messages, "create", return_value=fake_response
        ),
        patch.object(self_improve.batch_runner, "submit_batch", return_value=None),
    ):
        mock_compress.return_value = "COMPRESSED"

        self_improve._analyze_changes(
            changed_pages=["overview", "sessions", "tools"],
            new_pages=[],
        )

    # compress_prompt was called — that's the gate this test cares about.
    assert mock_compress.call_count == 1
    args, kwargs = mock_compress.call_args
    payload = args[0]
    # Sanity: the payload we sent through is in the 13K range, above the
    # new 12000 threshold and below the old 20000 threshold.
    assert 12_000 < len(payload) < 20_000, (
        f"Test fixture sized payload outside the target band: {len(payload)} chars"
    )
    # The compression contract is unchanged otherwise.
    assert kwargs["min_chars"] == 12_000
    assert kwargs["model"] == "espresso_v1"
    assert kwargs["call_site"] == "self_improve"


def test_analyze_changes_uses_compressed_text_in_request():
    """The Messages API user content must contain whatever compress_prompt returns."""
    import self_improve

    fake_response = _build_fake_messages_response("analysis text")
    sentinel = "<<COMPRESSED-DOC-PAGES-12345>>"

    with (
        patch.object(self_improve, "compress_prompt", return_value=sentinel),
        patch.object(self_improve, "_fetch_page", return_value="page content"),
        patch.object(
            self_improve.client.messages, "create", return_value=fake_response
        ) as mock_create,
        patch.object(self_improve.batch_runner, "submit_batch", return_value=None),
    ):
        self_improve._analyze_changes(
            changed_pages=["overview"],
            new_pages=[],
        )

    assert mock_create.call_count == 1
    _, kwargs = mock_create.call_args
    user_msg = kwargs["messages"][0]
    assert user_msg["role"] == "user"
    assert sentinel in user_msg["content"]


def test_analyze_changes_fallback_to_original_text_still_works():
    """When compress_prompt returns the original (fallback), _analyze_changes
    still produces a valid response — never breaks the call chain."""
    import self_improve

    fake_response = _build_fake_messages_response("real analysis")

    def _passthrough(text, **kwargs):
        return text

    with (
        patch.object(self_improve, "compress_prompt", side_effect=_passthrough),
        patch.object(self_improve, "_fetch_page", return_value="page content"),
        patch.object(
            self_improve.client.messages, "create", return_value=fake_response
        ),
        patch.object(self_improve.batch_runner, "submit_batch", return_value=None),
    ):
        result = self_improve._analyze_changes(
            changed_pages=["overview"],
            new_pages=[],
        )

    # Result is the text from the (mocked) Messages API response.
    assert result == "real analysis"


# ── _save_to_memory upsert: 409 conflict → update by conflicting_memory_id ───


def _make_conflict_error(memory_id: str):
    """Build a real ConflictError with the body shape the API returns."""
    import anthropic
    import httpx

    request = httpx.Request("POST", "https://api.anthropic.com/v1/memories")
    response = httpx.Response(409, request=request)
    body = {
        "error": {
            "type": "memory_path_conflict_error",
            "message": (
                "path `/system/doc_updates/2026-05-14.md` is already used "
                f"by `{memory_id}`; use update to modify it"
            ),
            "conflicting_memory_id": memory_id,
            "conflicting_path": "/system/doc_updates/2026-05-14.md",
        }
    }
    return anthropic.ConflictError(
        "409 conflict",
        response=response,
        body=body,
    )


def test_save_to_memory_falls_back_to_update_on_conflict():
    """A second nightly run on the same day must NOT raise. The function
    must catch the 409, pull ``conflicting_memory_id`` from the body, and
    call ``memories.update`` with the same content the create attempted."""
    import self_improve

    conflict = _make_conflict_error("mem_012RSTECdYmQ5GAgZwnthELR")

    mock_memories = MagicMock()
    mock_memories.create.side_effect = conflict
    mock_memories.update.return_value = MagicMock(id="mem_012RSTECdYmQ5GAgZwnthELR")

    with patch.object(
        self_improve.client.beta.memory_stores, "memories", mock_memories
    ):
        # Must NOT raise.
        self_improve._save_to_memory("today's summary")

    # create was tried once.
    assert mock_memories.create.call_count == 1
    create_kwargs = mock_memories.create.call_args.kwargs
    assert create_kwargs["path"].startswith("/system/doc_updates/")
    assert create_kwargs["path"].endswith(".md")
    expected_content = create_kwargs["content"]
    assert "today's summary" in expected_content

    # update was called with the conflicting_memory_id from the error body,
    # and with the SAME content the create attempted (idempotency).
    assert mock_memories.update.call_count == 1
    update_args = mock_memories.update.call_args
    assert update_args.args[0] == "mem_012RSTECdYmQ5GAgZwnthELR"
    assert update_args.kwargs["memory_store_id"] == self_improve.HEALTH_STORE_ID
    assert update_args.kwargs["content"] == expected_content


def test_save_to_memory_swallows_when_conflict_body_missing_id():
    """If the SDK ever shapes the 409 body differently and the
    ``conflicting_memory_id`` is missing, ``_save_to_memory`` must NOT crash
    the nightly pipeline — the outer ``except Exception`` logs and the
    function returns. update must never be called with ``None``."""
    import anthropic
    import httpx

    import self_improve

    request = httpx.Request("POST", "https://api.anthropic.com/v1/memories")
    response = httpx.Response(409, request=request)
    malformed_conflict = anthropic.ConflictError(
        "409 conflict",
        response=response,
        body={"error": {"type": "memory_path_conflict_error"}},  # no id
    )

    mock_memories = MagicMock()
    mock_memories.create.side_effect = malformed_conflict

    with patch.object(
        self_improve.client.beta.memory_stores, "memories", mock_memories
    ):
        # Must NOT raise — the outer except logs the trace.
        self_improve._save_to_memory("summary")

    assert mock_memories.create.call_count == 1
    assert mock_memories.update.call_count == 0


def test_save_to_memory_happy_path_creates_only():
    """First run of the day: no conflict, no update call."""
    import self_improve

    mock_memories = MagicMock()
    mock_memories.create.return_value = MagicMock(id="mem_new")

    with patch.object(
        self_improve.client.beta.memory_stores, "memories", mock_memories
    ):
        self_improve._save_to_memory("first-run summary")

    assert mock_memories.create.call_count == 1
    assert mock_memories.update.call_count == 0


# ─────────────────────────────────────────────────────────────────────────────
# Task #24 — 7-day TTL + hot-file auto-issue
# ─────────────────────────────────────────────────────────────────────────────


def test_save_to_memory_stamps_expires_at_seven_days_out():
    """The doc-update memory entry must carry an ``expires_at`` frontmatter
    7 days from the moment of write so ``prune_stale_doc_updates`` can sweep
    it next week. Operators who want to keep an entry forever can edit the
    frontmatter; the prune sweep is read-then-write per file."""
    from datetime import datetime, timedelta, timezone

    import self_improve

    mock_memories = MagicMock()
    mock_memories.create.return_value = MagicMock(id="mem_new")

    before = datetime.now(timezone.utc)
    with patch.object(
        self_improve.client.beta.memory_stores, "memories", mock_memories
    ):
        self_improve._save_to_memory("today's summary")
    after = datetime.now(timezone.utc)

    create_kwargs = mock_memories.create.call_args.kwargs
    content = create_kwargs["content"]

    # Frontmatter present and well-formed.
    assert content.startswith("---\n"), (
        f"expected leading frontmatter delimiter, got: {content[:30]!r}"
    )
    assert "expires_at:" in content.splitlines()[1]

    # The parsed timestamp must be ~7 days in the future.
    expires_at = self_improve._parse_expires_at(content)
    assert expires_at is not None
    expected_low = (
        before + timedelta(days=self_improve.DOC_UPDATE_TTL_DAYS) - timedelta(seconds=2)
    )
    expected_high = (
        after + timedelta(days=self_improve.DOC_UPDATE_TTL_DAYS) + timedelta(seconds=2)
    )
    assert expected_low <= expires_at <= expected_high, (
        f"expires_at {expires_at} not within {expected_low} .. {expected_high}"
    )

    # Original heading and summary still in the body.
    assert "today's summary" in content
    assert "# Managed Agents Doc Updates" in content


def test_prune_stale_doc_updates_drops_expired_entries():
    """``prune_stale_doc_updates`` must DELETE memory entries whose
    ``expires_at`` is in the past, leave fresh ones alone, and leave files
    without frontmatter alone (migration safety)."""
    from datetime import datetime, timedelta, timezone

    import self_improve

    now = datetime.now(timezone.utc)
    past_ts = (now - timedelta(days=1)).isoformat()
    future_ts = (now + timedelta(days=3)).isoformat()

    expired = (
        f"---\nexpires_at: {past_ts}\n---\n"
        "# Managed Agents Doc Updates — 2026-05-01\n\nstale summary"
    )
    fresh = (
        f"---\nexpires_at: {future_ts}\n---\n"
        "# Managed Agents Doc Updates — 2026-05-13\n\nfresh summary"
    )
    legacy = "# Managed Agents Doc Updates — 2026-04-01\n\nno frontmatter"

    listing = MagicMock()
    listing.data = [
        MagicMock(id="mem_old", path="/system/doc_updates/2026-05-01.md"),
        MagicMock(id="mem_new", path="/system/doc_updates/2026-05-13.md"),
        MagicMock(id="mem_legacy", path="/system/doc_updates/2026-04-01.md"),
    ]

    retrieve_by_id = {
        "mem_old": MagicMock(content=expired),
        "mem_new": MagicMock(content=fresh),
        "mem_legacy": MagicMock(content=legacy),
    }

    mock_memories = MagicMock()
    mock_memories.list.return_value = listing
    mock_memories.retrieve.side_effect = lambda mid, memory_store_id: retrieve_by_id[
        mid
    ]
    deletes = []
    mock_memories.delete.side_effect = lambda mid, memory_store_id: deletes.append(mid)

    with patch.object(
        self_improve.client.beta.memory_stores, "memories", mock_memories
    ):
        dropped = self_improve.prune_stale_doc_updates(now=now)

    assert dropped == 1
    assert deletes == ["mem_old"]


def test_create_doc_drift_issue_fires_for_hot_file_touch():
    """A doc-page change that touches a HOT_FILES entry must call the gh
    issue helper. Dedupe path: when no existing issue with the label is
    open, the create runs."""
    import self_improve

    with (
        patch.object(
            self_improve, "list_open_issues_with_label", return_value=[]
        ) as mock_list,
        patch.object(
            self_improve,
            "create_gh_issue",
            return_value="https://github.com/example/repo/issues/77",
        ) as mock_create,
    ):
        url = self_improve.create_doc_drift_issue(
            "https://platform.claude.com/docs/en/managed-agents/sessions.md",
            "sessions API changed",
            "orchestrator/session_runner.py",
        )

    assert url == "https://github.com/example/repo/issues/77"
    mock_list.assert_called_once_with("auto-doc-drift")
    assert mock_create.call_count == 1
    title_arg, body_arg, label_arg = mock_create.call_args.args
    assert title_arg.startswith("[auto-doc-drift]")
    assert "sessions" in title_arg
    assert "orchestrator/session_runner.py" in title_arg
    assert label_arg == "auto-doc-drift"
    # Body must mention the doc URL, the local file, and the summary.
    assert "sessions.md" in body_arg
    assert "orchestrator/session_runner.py" in body_arg
    assert "sessions API changed" in body_arg


def test_create_doc_drift_issue_dedups_against_open_issue():
    """If an open issue with the same title already exists under the
    auto-doc-drift label, the cron must NOT open a duplicate."""
    import self_improve

    title = self_improve._build_drift_issue_title(
        "sessions", "orchestrator/session_runner.py"
    )

    with (
        patch.object(self_improve, "list_open_issues_with_label", return_value=[title]),
        patch.object(self_improve, "create_gh_issue") as mock_create,
    ):
        url = self_improve.create_doc_drift_issue(
            "https://platform.claude.com/docs/en/managed-agents/sessions.md",
            "summary",
            "orchestrator/session_runner.py",
        )

    assert url is None
    mock_create.assert_not_called()


def test_non_hot_file_touch_does_not_create_issue():
    """A doc-page change that doesn't map to any HOT_FILES entry must NOT
    trigger an issue. The page-to-file routing is the gate; ``onboarding``
    isn't in HOT_FILE_BY_DOC_PAGE so the helper short-circuits."""
    import self_improve

    with patch.object(self_improve, "create_doc_drift_issue") as mock_create:
        urls = self_improve.open_doc_drift_issues_for_pages(
            ["onboarding", "overview"], "summary"
        )

    # Neither slug routes to a hot file, so create was never called.
    assert urls == []
    mock_create.assert_not_called()


def test_hot_files_constant_matches_plan():
    """Regression guard: the HOT_FILES set must contain exactly the five
    files the autoplan F5 trigger calls out — Coordinator/Specialist prompts
    (in agents/setup_agents.py), kapa_rest_tool, session_runner SDK call
    sites, db_adapter (which owns persisted Managed Agents doc snapshots
    plus cost/messages/session ledgers), and the Dockerfile. Adding files
    here is a deliberate decision; a stray expansion is a noise-vector
    for false-positive issues.

    Codex review (PR #196) flagged that db_adapter.py was missing — doc
    changes under ``observability`` / session usage surfaces silently
    skipped auto-issue routing without it.
    """
    import self_improve

    assert self_improve.HOT_FILES == {
        "agents/setup_agents.py",
        "orchestrator/kapa_rest_tool.py",
        "orchestrator/session_runner.py",
        "orchestrator/db_adapter.py",
        "Dockerfile",
    }


def test_hot_file_by_doc_page_keys_are_tracked_pages():
    """Every key in ``HOT_FILE_BY_DOC_PAGE`` must be a page the crawler
    actually fetches. A stale key (typo, renamed slug) is a silent
    no-op because the crawler will never observe a change for it.

    Codex review (PR #196, P1 invariant): without this guard a misspelled
    slug in the routing table would never fire an issue and no test
    would catch it.
    """
    import self_improve

    untracked = [
        page
        for page in self_improve.HOT_FILE_BY_DOC_PAGE
        if page not in self_improve.DOCS_PAGES
    ]
    assert not untracked, (
        f"HOT_FILE_BY_DOC_PAGE keys not tracked by crawler: {untracked}"
    )


def test_hot_file_by_doc_page_values_are_in_hot_files():
    """Every file in the routing table must appear in ``HOT_FILES``. The
    intersection in ``_hot_files_for`` would silently drop a stray entry,
    so adding a non-hot file here is a stealth bug. This test makes the
    map self-consistent.

    Codex review (PR #196, P1 invariant).
    """
    import self_improve

    extras: dict[str, set[str]] = {}
    for page, files in self_improve.HOT_FILE_BY_DOC_PAGE.items():
        diff = set(files) - self_improve.HOT_FILES
        if diff:
            extras[page] = diff
    assert not extras, (
        f"HOT_FILE_BY_DOC_PAGE references files outside HOT_FILES: {extras}"
    )


def test_hot_file_by_doc_page_includes_observability_routing():
    """Codex review (PR #196): the ``observability`` doc page is tracked
    by the crawler and touches the ledger surfaces that ``db_adapter.py``
    owns, so it must route. The previous mapping left it unmapped, which
    silently dropped routing for one of the most-likely-to-shift pages.
    """
    import self_improve

    assert "observability" in self_improve.HOT_FILE_BY_DOC_PAGE
    assert (
        "orchestrator/db_adapter.py"
        in self_improve.HOT_FILE_BY_DOC_PAGE["observability"]
    )


def test_parse_expires_at_normalizes_naive_to_utc():
    """Codex review (PR #196, P2 fix): a hand-edited frontmatter that
    drops the timezone suffix (``2026-05-21T12:00:00``) must NOT crash the
    prune sweep. ``_parse_expires_at`` coerces naive timestamps to UTC so
    the comparison against ``datetime.now(timezone.utc)`` is monotonic.
    """
    import self_improve

    content = "---\nexpires_at: 2026-05-21T12:00:00\n---\n# header\nbody\n"
    parsed = self_improve._parse_expires_at(content)
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0
    # Same value with explicit Z should compare equal after normalization.
    aware = self_improve._parse_expires_at(
        "---\nexpires_at: 2026-05-21T12:00:00Z\n---\n"
    )
    assert aware == parsed


def test_prune_does_not_raise_on_naive_expires_at():
    """End-to-end of the P2 fix: a naive ``expires_at`` in the past must
    drop the entry, not raise TypeError. Before the fix the prune sweep
    aborted mid-loop on the first naive timestamp it encountered."""
    from datetime import datetime, timedelta, timezone
    from unittest.mock import MagicMock

    import self_improve

    now = datetime.now(timezone.utc)
    past_naive = (now - timedelta(days=1)).replace(tzinfo=None).isoformat()
    expired_naive = (
        f"---\nexpires_at: {past_naive}\n---\n"
        "# Managed Agents Doc Updates — old\n\nstale"
    )

    listing = MagicMock()
    listing.data = [
        MagicMock(id="mem_naive", path="/system/doc_updates/2026-05-01.md"),
    ]
    retrieve_by_id = {"mem_naive": MagicMock(content=expired_naive)}

    mock_memories = MagicMock()
    mock_memories.list.return_value = listing
    mock_memories.retrieve.side_effect = lambda mid, memory_store_id: retrieve_by_id[
        mid
    ]
    deletes: list = []
    mock_memories.delete.side_effect = lambda mid, memory_store_id: deletes.append(mid)

    with patch.object(
        self_improve.client.beta.memory_stores, "memories", mock_memories
    ):
        dropped = self_improve.prune_stale_doc_updates(now=now)

    assert dropped == 1
    assert deletes == ["mem_naive"]
