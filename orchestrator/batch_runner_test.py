"""Tests for orchestrator/batch_runner.py (Plan #36).

All Anthropic SDK calls are mocked; no network. DB calls are gated on
``db_adapter.DATABASE_URL`` being truthy — the persistence helpers are
patched directly so tests don't need a live Postgres.

Run:
    cd orchestrator && python3 -m pytest batch_runner_test.py -q
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def _enabled():
    """Force BATCH_PROCESSING_ENABLED=True for the duration of a test."""
    with patch("batch_runner.BATCH_PROCESSING_ENABLED", True):
        yield


@pytest.fixture
def _stub_db():
    """No-op every DB helper so tests don't require Postgres."""
    with (
        patch("batch_runner._record_submission") as record,
        patch("batch_runner._list_submitted_batch_ids") as list_ids,
        patch("batch_runner._fetch_pending_requests") as fetch_pending,
        patch("batch_runner._mark_request_complete") as mark_req,
        patch("batch_runner._mark_batch_status") as mark_batch,
        patch("batch_runner._log_batch_cost") as log_cost,
    ):
        yield {
            "record_submission": record,
            "list_submitted": list_ids,
            "fetch_pending": fetch_pending,
            "mark_request_complete": mark_req,
            "mark_batch_status": mark_batch,
            "log_batch_cost": log_cost,
        }


# ── Cache TTL ──────────────────────────────────────────────────────────────


def test_batch_cache_ttl_is_1h():
    """Task #54: BATCH_CACHE_TTL must be '1h' so cache doesn't expire mid-batch.

    The Anthropic Batches API can take up to 24h to complete and most batches
    finish within an hour. A 5m TTL (the SDK default) routinely expires before
    a batch completes, forcing the system prompt to be re-billed as fresh
    input on the next call. The 1h variant is the documented recommendation
    for batched requests, and it also wins on repeat-payload reads — e.g.
    self_improve's nightly crawl hits near-identical doc pages run-over-run.
    """
    import batch_runner

    assert batch_runner.BATCH_CACHE_TTL == "1h"


def test_self_heal_and_self_improve_use_batch_cache_ttl():
    """Wiring check: both batched call sites reference BATCH_CACHE_TTL.

    The cache_control dict is built at call time inside the Messages API call,
    so there's no module-level value to introspect — read the source instead
    and confirm the symbol is wired into an ``ephemeral`` entry's ``ttl``.
    """
    import batch_runner
    import self_heal
    import self_improve

    self_heal_src = open(self_heal.__file__).read()
    self_improve_src = open(self_improve.__file__).read()
    assert "BATCH_CACHE_TTL" in self_heal_src
    assert "BATCH_CACHE_TTL" in self_improve_src
    assert '"ttl": BATCH_CACHE_TTL' in self_heal_src
    assert '"ttl": BATCH_CACHE_TTL' in self_improve_src
    assert batch_runner.BATCH_CACHE_TTL == "1h"


# ── submit_batch ───────────────────────────────────────────────────────────


def test_submit_batch_returns_none_when_disabled():
    """Kill-switch: BATCH_PROCESSING_ENABLED=False -> None, no SDK call."""
    import batch_runner

    with (
        patch.object(batch_runner, "BATCH_PROCESSING_ENABLED", False),
        patch.object(batch_runner.client.messages.batches, "create") as mock_create,
    ):
        result = batch_runner.submit_batch(
            call_site="self_heal",
            model="claude-sonnet-4-6",
            requests=[
                {
                    "custom_id": "r1",
                    "params": {
                        "model": "claude-sonnet-4-6",
                        "max_tokens": 100,
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                }
            ],
        )

    assert result is None
    mock_create.assert_not_called()


def test_submit_batch_returns_none_on_empty_requests(_enabled):
    import batch_runner

    with patch.object(batch_runner.client.messages.batches, "create") as mock_create:
        assert (
            batch_runner.submit_batch(
                call_site="self_heal", model="claude-sonnet-4-6", requests=[]
            )
            is None
        )

    mock_create.assert_not_called()


def test_submit_batch_creates_db_rows(_enabled, _stub_db):
    """Happy path: SDK call succeeds → persistence helper called → batch_id returned."""
    import batch_runner

    fake_batch = MagicMock(id="msgbatch_abc123")
    requests = [
        {
            "custom_id": "session_1",
            "params": {
                "model": "claude-sonnet-4-6",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": "x"}],
            },
            "context": {"session_id": "session_1", "session_type": "ad-hoc"},
        },
        {
            "custom_id": "session_2",
            "params": {
                "model": "claude-sonnet-4-6",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": "y"}],
            },
            "context": {"session_id": "session_2"},
        },
    ]

    with patch.object(
        batch_runner.client.messages.batches, "create", return_value=fake_batch
    ) as mock_create:
        result = batch_runner.submit_batch(
            call_site="self_heal", model="claude-sonnet-4-6", requests=requests
        )

    assert result == "msgbatch_abc123"
    mock_create.assert_called_once()
    # Verify the context field is stripped before sending to Anthropic.
    api_requests = mock_create.call_args.kwargs["requests"]
    assert api_requests == [
        {"custom_id": "session_1", "params": requests[0]["params"]},
        {"custom_id": "session_2", "params": requests[1]["params"]},
    ]
    # Verify persistence helper called with original requests (including context).
    _stub_db["record_submission"].assert_called_once_with(
        "msgbatch_abc123", "self_heal", "claude-sonnet-4-6", requests, "self_heal"
    )


def test_submit_batch_returns_none_on_sdk_error(_enabled, _stub_db):
    """SDK exception → log + return None, never raise to caller."""
    import batch_runner

    with patch.object(
        batch_runner.client.messages.batches,
        "create",
        side_effect=RuntimeError("API down"),
    ):
        result = batch_runner.submit_batch(
            call_site="self_improve",
            model="claude-sonnet-4-6",
            requests=[
                {
                    "custom_id": "r1",
                    "params": {
                        "model": "claude-sonnet-4-6",
                        "max_tokens": 100,
                        "messages": [{"role": "user", "content": "x"}],
                    },
                }
            ],
        )
    assert result is None
    _stub_db["record_submission"].assert_not_called()


def test_submit_batch_uses_explicit_callback_name(_enabled, _stub_db):
    """callback_name override is forwarded to persistence helper."""
    import batch_runner

    fake_batch = MagicMock(id="msgbatch_xyz")
    requests = [
        {
            "custom_id": "r1",
            "params": {
                "model": "claude-sonnet-4-6",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "x"}],
            },
        }
    ]

    with patch.object(
        batch_runner.client.messages.batches, "create", return_value=fake_batch
    ):
        batch_runner.submit_batch(
            call_site="self_heal",
            model="claude-sonnet-4-6",
            requests=requests,
            callback_name="custom_cb",
        )

    _stub_db["record_submission"].assert_called_once_with(
        "msgbatch_xyz", "self_heal", "claude-sonnet-4-6", requests, "custom_cb"
    )


# ── Cost computation ───────────────────────────────────────────────────────


def test_batch_cost_is_50_percent_of_realtime():
    """Sanity check: every line item in REALTIME_RATES_PER_MTOK halves."""
    import batch_runner

    usage = {
        "input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
        "cache_read_input_tokens": 1_000_000,
        "cache_creation_input_tokens": 1_000_000,
    }
    rates = batch_runner.REALTIME_RATES_PER_MTOK["claude-sonnet-4-6"]
    expected_realtime = (
        rates["input"] + rates["output"] + rates["cache_read"] + rates["cache_write_5m"]
    )
    expected_batch = expected_realtime * batch_runner.BATCH_RATE_MULTIPLIER

    actual = batch_runner._batch_cost_for_usage(usage, "claude-sonnet-4-6")
    assert actual == pytest.approx(expected_batch, rel=1e-9)


def test_batch_cost_unknown_model_falls_back_to_sonnet():
    """Unknown model → log + fall back to Sonnet rates, never raise."""
    import batch_runner

    usage = {"input_tokens": 1_000_000}
    cost = batch_runner._batch_cost_for_usage(usage, "claude-fake-9-9")
    # 1MTok input @ Sonnet rate $3 × batch multiplier 0.5 = $1.50
    assert cost == pytest.approx(1.50, rel=1e-9)


def test_batch_cost_handles_missing_usage_fields():
    """Empty / partial usage dicts → 0 for missing fields."""
    import batch_runner

    assert batch_runner._batch_cost_for_usage({}, "claude-sonnet-4-6") == 0.0


# ── poll_pending_batches ───────────────────────────────────────────────────


def _make_result(custom_id: str, text: str, *, succeeded=True, usage=None):
    """Build a MagicMock that mimics an SDK result object."""
    if usage is None:
        usage = {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
    block = MagicMock()
    block.type = "text"
    block.text = text
    msg = MagicMock()
    msg.content = [block]
    msg.usage = usage
    inner = MagicMock()
    inner.type = "succeeded" if succeeded else "errored"
    inner.message = msg if succeeded else None
    res = MagicMock()
    res.custom_id = custom_id
    res.result = inner
    return res


def test_poll_routes_completion_to_registered_callback(_enabled, _stub_db):
    """ended batch → callback fires with (request_id, context, text, usage)."""
    import batch_runner

    _stub_db["list_submitted"].return_value = [
        {
            "batch_id": "msgbatch_1",
            "call_site": "self_heal",
            "model": "claude-sonnet-4-6",
        }
    ]
    _stub_db["fetch_pending"].return_value = [
        {
            "request_id": "session_1",
            "callback_name": "self_heal",
            "context_json": {"session_id": "session_1", "type": "ad-hoc"},
        }
    ]

    fake_batch = MagicMock(processing_status="ended")
    result1 = _make_result("session_1", "review text here")
    received = []

    def cb(request_id, context, text, usage):
        received.append((request_id, context, text, usage))

    with (
        patch.object(
            batch_runner.client.messages.batches, "retrieve", return_value=fake_batch
        ),
        patch.object(
            batch_runner.client.messages.batches, "results", return_value=[result1]
        ),
    ):
        completed = batch_runner.poll_pending_batches({"self_heal": cb})

    assert completed == 1
    assert len(received) == 1
    request_id, context, text, usage = received[0]
    assert request_id == "session_1"
    assert context == {"session_id": "session_1", "type": "ad-hoc"}
    assert text == "review text here"
    assert usage["input_tokens"] == 100
    _stub_db["mark_batch_status"].assert_any_call("msgbatch_1", "ended")
    _stub_db["log_batch_cost"].assert_called_once()


def test_poll_skips_batches_still_in_progress(_enabled, _stub_db):
    """processing_status='in_progress' → no callback, no status update."""
    import batch_runner

    _stub_db["list_submitted"].return_value = [
        {
            "batch_id": "msgbatch_2",
            "call_site": "self_heal",
            "model": "claude-sonnet-4-6",
        }
    ]
    fake_batch = MagicMock(processing_status="in_progress")

    callback_calls = []

    with (
        patch.object(
            batch_runner.client.messages.batches, "retrieve", return_value=fake_batch
        ),
        patch.object(batch_runner.client.messages.batches, "results") as mock_results,
    ):
        completed = batch_runner.poll_pending_batches(
            {"self_heal": lambda *a, **k: callback_calls.append(a)}
        )

    assert completed == 0
    mock_results.assert_not_called()
    assert callback_calls == []
    _stub_db["mark_batch_status"].assert_not_called()


def test_poll_handles_unknown_callback_without_crashing(_enabled, _stub_db):
    """Missing registry entry → log warning, still mark row complete."""
    import batch_runner

    _stub_db["list_submitted"].return_value = [
        {
            "batch_id": "msgbatch_3",
            "call_site": "self_heal",
            "model": "claude-sonnet-4-6",
        }
    ]
    _stub_db["fetch_pending"].return_value = [
        {"request_id": "r1", "callback_name": "unknown_cb", "context_json": {}}
    ]
    fake_batch = MagicMock(processing_status="ended")
    result1 = _make_result("r1", "x")

    with (
        patch.object(
            batch_runner.client.messages.batches, "retrieve", return_value=fake_batch
        ),
        patch.object(
            batch_runner.client.messages.batches, "results", return_value=[result1]
        ),
    ):
        completed = batch_runner.poll_pending_batches({})  # empty registry

    assert completed == 1
    _stub_db["mark_request_complete"].assert_called_once()
    _stub_db["mark_batch_status"].assert_any_call("msgbatch_3", "ended")


def test_poll_handles_errored_results_without_billing(_enabled, _stub_db):
    """result.type='errored' → callback still fires but no cost row logged."""
    import batch_runner

    _stub_db["list_submitted"].return_value = [
        {
            "batch_id": "msgbatch_4",
            "call_site": "self_heal",
            "model": "claude-sonnet-4-6",
        }
    ]
    _stub_db["fetch_pending"].return_value = [
        {"request_id": "r1", "callback_name": "self_heal", "context_json": {}}
    ]
    fake_batch = MagicMock(processing_status="ended")
    err_result = _make_result("r1", "", succeeded=False)

    received = []
    with (
        patch.object(
            batch_runner.client.messages.batches, "retrieve", return_value=fake_batch
        ),
        patch.object(
            batch_runner.client.messages.batches, "results", return_value=[err_result]
        ),
    ):
        batch_runner.poll_pending_batches(
            {"self_heal": lambda *a, **k: received.append(a)}
        )

    # Errored results aren't billed.
    _stub_db["log_batch_cost"].assert_not_called()
    # But the row still gets marked complete with the actual status.
    args = _stub_db["mark_request_complete"].call_args
    assert args[0][4] == "errored"


def test_poll_returns_zero_when_no_pending(_enabled, _stub_db):
    import batch_runner

    _stub_db["list_submitted"].return_value = []

    with patch.object(
        batch_runner.client.messages.batches, "retrieve"
    ) as mock_retrieve:
        completed = batch_runner.poll_pending_batches({})

    assert completed == 0
    mock_retrieve.assert_not_called()


def test_poll_retrieve_error_does_not_mark_failed(_enabled, _stub_db):
    """Transient retrieve error → log + continue, don't poison the row."""
    import batch_runner

    _stub_db["list_submitted"].return_value = [
        {
            "batch_id": "msgbatch_5",
            "call_site": "self_heal",
            "model": "claude-sonnet-4-6",
        }
    ]

    with patch.object(
        batch_runner.client.messages.batches,
        "retrieve",
        side_effect=RuntimeError("transient"),
    ):
        completed = batch_runner.poll_pending_batches({})

    assert completed == 0
    _stub_db["mark_batch_status"].assert_not_called()


def test_poll_pending_batches_skips_row_on_fetch_pending_failure(_enabled, _stub_db):
    """Plan #52 PR-Y: one bad row no longer crashes the whole poll cycle.

    Repro shape: ``_fetch_pending_requests`` raises on a transient DB hiccup
    (or a missing batch_job_requests row). Before the fix, this propagated
    out of ``poll_pending_batches`` and the scheduler wrapper fired a fresh
    Slack watch notice every 15 minutes. After the fix, the row is logged
    and skipped; the next poll retries.
    """
    import batch_runner

    _stub_db["list_submitted"].return_value = [
        {
            "batch_id": "msgbatch_bad",
            "call_site": "self_heal",
            "model": "claude-sonnet-4-6",
        },
        {
            "batch_id": "msgbatch_good",
            "call_site": "self_improve",
            "model": "claude-sonnet-4-6",
        },
    ]

    def _fetch_side_effect(batch_id):
        if batch_id == "msgbatch_bad":
            raise RuntimeError("transient DB drop")
        return []

    _stub_db["fetch_pending"].side_effect = _fetch_side_effect

    fake_batch = MagicMock(processing_status="ended")
    with (
        patch.object(
            batch_runner.client.messages.batches,
            "retrieve",
            return_value=fake_batch,
        ),
        patch.object(
            batch_runner.client.messages.batches, "results", return_value=[]
        ),
    ):
        # Crucially: this must not raise.
        completed = batch_runner.poll_pending_batches(
            {"self_heal": lambda *a, **k: None, "self_improve": lambda *a, **k: None}
        )

    # Only the good batch counts as completed; the bad one is skipped.
    assert completed == 1
    # The bad row's status is NOT touched — the next poll retries.
    status_calls = _stub_db["mark_batch_status"].call_args_list
    touched_ids = [c[0][0] for c in status_calls]
    assert "msgbatch_bad" not in touched_ids
    # The good row is marked ended.
    assert ("msgbatch_good", "ended") in [(c[0][0], c[0][1]) for c in status_calls]


# ── recover_orphan_batches ─────────────────────────────────────────────────


def test_recover_detects_ended_batches(_enabled, _stub_db):
    """Anthropic says ended → counted, status left 'submitted' so poll picks it up."""
    import batch_runner

    _stub_db["list_submitted"].return_value = [
        {
            "batch_id": "msgbatch_old",
            "call_site": "self_heal",
            "model": "claude-sonnet-4-6",
        }
    ]
    fake_batch = MagicMock(processing_status="ended")

    with patch.object(
        batch_runner.client.messages.batches, "retrieve", return_value=fake_batch
    ):
        recovered = batch_runner.recover_orphan_batches()

    assert recovered == 1
    # We don't mark ended at recovery time — that's the poll loop's job.
    _stub_db["mark_batch_status"].assert_not_called()


def test_recover_marks_canceled_batches_failed(_enabled, _stub_db):
    """Anthropic status=canceled → local status='failed' with error_message."""
    import batch_runner

    _stub_db["list_submitted"].return_value = [
        {
            "batch_id": "msgbatch_cancel",
            "call_site": "self_heal",
            "model": "claude-sonnet-4-6",
        }
    ]
    fake_batch = MagicMock(processing_status="canceled")

    with patch.object(
        batch_runner.client.messages.batches, "retrieve", return_value=fake_batch
    ):
        recovered = batch_runner.recover_orphan_batches()

    assert recovered == 1
    _stub_db["mark_batch_status"].assert_called_once()
    args = _stub_db["mark_batch_status"].call_args[0]
    assert args[0] == "msgbatch_cancel"
    assert args[1] == "failed"
    assert "canceled" in args[2]


def test_recover_leaves_in_progress_alone(_enabled, _stub_db):
    """Anthropic status=in_progress → no status change, not counted."""
    import batch_runner

    _stub_db["list_submitted"].return_value = [
        {
            "batch_id": "msgbatch_inflight",
            "call_site": "self_heal",
            "model": "claude-sonnet-4-6",
        }
    ]
    fake_batch = MagicMock(processing_status="in_progress")

    with patch.object(
        batch_runner.client.messages.batches, "retrieve", return_value=fake_batch
    ):
        recovered = batch_runner.recover_orphan_batches()

    assert recovered == 0
    _stub_db["mark_batch_status"].assert_not_called()


def test_recover_returns_zero_when_no_rows(_enabled, _stub_db):
    import batch_runner

    _stub_db["list_submitted"].return_value = []
    assert batch_runner.recover_orphan_batches() == 0


# ── Helper coverage ────────────────────────────────────────────────────────


def test_extract_text_handles_dict_blocks():
    """JSON-decoded blobs should work just like SDK objects."""
    import batch_runner

    message = {
        "content": [
            {"type": "text", "text": "hello "},
            {"type": "text", "text": "world"},
        ]
    }
    assert batch_runner._extract_text(message) == "hello world"


def test_extract_text_handles_none():
    import batch_runner

    assert batch_runner._extract_text(None) == ""


def test_usage_to_dict_normalizes_both_shapes():
    import batch_runner

    # dict passes through.
    d = {"input_tokens": 5, "output_tokens": 7}
    assert batch_runner._usage_to_dict(d) == d
    # SDK-like object gets converted.
    obj = MagicMock(
        input_tokens=10,
        output_tokens=20,
        cache_read_input_tokens=3,
        cache_creation_input_tokens=4,
    )
    out = batch_runner._usage_to_dict(obj)
    assert out["input_tokens"] == 10
    assert out["cache_creation_input_tokens"] == 4
    # None → empty.
    assert batch_runner._usage_to_dict(None) == {}


# ── Cost telemetry (Plan #36 task #55) ─────────────────────────────────────
#
# These tests verify that completed batch results write a row to
# ``messages_api_calls`` with ``tier='batch'`` via
# ``cost_collector.track_messages_call``. We patch the cost collector at the
# entry point ``batch_runner`` uses so the test can assert on call shape
# without spinning up Postgres.


@pytest.fixture
def _stub_db_real_log_cost():
    """Variant of _stub_db that leaves ``_log_batch_cost`` intact.

    Lets tests assert end-to-end that the batch poll path actually invokes
    ``cost_collector.track_messages_call`` rather than just the local helper.
    """
    with (
        patch("batch_runner._record_submission") as record,
        patch("batch_runner._list_submitted_batch_ids") as list_ids,
        patch("batch_runner._fetch_pending_requests") as fetch_pending,
        patch("batch_runner._mark_request_complete") as mark_req,
        patch("batch_runner._mark_batch_status") as mark_batch,
    ):
        yield {
            "record_submission": record,
            "list_submitted": list_ids,
            "fetch_pending": fetch_pending,
            "mark_request_complete": mark_req,
            "mark_batch_status": mark_batch,
        }


def test_completed_batch_writes_messages_api_calls_row_with_tier_batch(
    _enabled, _stub_db_real_log_cost
):
    """End-to-end: succeeded result → track_messages_call(tier='batch', batch_id=...).

    Confirms Plan #36 task #55: the batch retrieval path forwards each
    completed request's usage to the canonical cost ledger
    (``messages_api_calls``) with the batch tier flag set so Plan #35
    reconciliation can split spend.
    """
    import batch_runner

    _stub_db_real_log_cost["list_submitted"].return_value = [
        {
            "batch_id": "msgbatch_cost_1",
            "call_site": "self_heal",
            "model": "claude-sonnet-4-6",
        }
    ]
    _stub_db_real_log_cost["fetch_pending"].return_value = [
        {
            "request_id": "session_abc",
            "callback_name": "self_heal",
            "context_json": {"session_id": "session_abc"},
        }
    ]
    fake_batch = MagicMock(processing_status="ended")
    result1 = _make_result(
        "session_abc",
        "review text",
        usage={
            "input_tokens": 12_000,
            "output_tokens": 2_500,
            "cache_read_input_tokens": 1_000,
            "cache_creation_input_tokens": 500,
        },
    )

    with (
        patch.object(
            batch_runner.client.messages.batches,
            "retrieve",
            return_value=fake_batch,
        ),
        patch.object(
            batch_runner.client.messages.batches,
            "results",
            return_value=[result1],
        ),
        patch.object(batch_runner.cost_collector, "track_messages_call") as mock_track,
    ):
        completed = batch_runner.poll_pending_batches(
            {"self_heal": lambda *a, **k: None}
        )

    assert completed == 1
    # The contract for Plan #36 task #55: each succeeded batch result writes
    # one messages_api_calls row via track_messages_call with tier='batch'
    # and the batch_id forwarded for cross-reference into batch_jobs.
    mock_track.assert_called_once()
    kwargs = mock_track.call_args.kwargs
    assert kwargs["tier"] == "batch"
    assert kwargs["batch_id"] == "msgbatch_cost_1"
    assert kwargs["call_site"] == "self_heal"
    assert kwargs["model"] == "claude-sonnet-4-6"
    forwarded_usage = kwargs["usage"]
    assert forwarded_usage.input_tokens == 12_000
    assert forwarded_usage.output_tokens == 2_500
    assert forwarded_usage.cache_read_input_tokens == 1_000
    assert forwarded_usage.cache_creation_input_tokens == 500


def test_errored_batch_does_not_write_cost_row(_enabled, _stub_db_real_log_cost):
    """``result.type='errored'`` skips track_messages_call entirely.

    Errored/canceled/expired requests are not billed per the Batches API docs,
    so the cost ledger must not gain a row for them — otherwise reconciliation
    would over-report local spend vs Anthropic ground truth.
    """
    import batch_runner

    _stub_db_real_log_cost["list_submitted"].return_value = [
        {
            "batch_id": "msgbatch_err_1",
            "call_site": "self_heal",
            "model": "claude-sonnet-4-6",
        }
    ]
    _stub_db_real_log_cost["fetch_pending"].return_value = [
        {"request_id": "r1", "callback_name": "self_heal", "context_json": {}}
    ]
    fake_batch = MagicMock(processing_status="ended")
    err_result = _make_result("r1", "", succeeded=False)

    with (
        patch.object(
            batch_runner.client.messages.batches,
            "retrieve",
            return_value=fake_batch,
        ),
        patch.object(
            batch_runner.client.messages.batches,
            "results",
            return_value=[err_result],
        ),
        patch.object(batch_runner.cost_collector, "track_messages_call") as mock_track,
    ):
        batch_runner.poll_pending_batches({"self_heal": lambda *a, **k: None})

    mock_track.assert_not_called()


def test_log_batch_cost_forwards_to_cost_collector(_enabled):
    """Unit: _log_batch_cost calls track_messages_call with tier='batch'.

    Verifies the helper signature without going through the polling path.
    Tier and batch_id must be set so the row lands in messages_api_calls with
    the right ledger split.
    """
    import batch_runner

    usage = {
        "input_tokens": 1000,
        "output_tokens": 500,
        "cache_read_input_tokens": 4000,
        "cache_creation_input_tokens": 2000,
    }

    with patch.object(batch_runner.cost_collector, "track_messages_call") as mock_track:
        batch_runner._log_batch_cost(
            "self_heal._analyze_session",
            "claude-sonnet-4-6",
            "msgbatch_unit_1",
            usage,
        )

    mock_track.assert_called_once()
    kwargs = mock_track.call_args.kwargs
    assert kwargs["call_site"] == "self_heal._analyze_session"
    assert kwargs["model"] == "claude-sonnet-4-6"
    assert kwargs["tier"] == "batch"
    assert kwargs["batch_id"] == "msgbatch_unit_1"
    # usage is wrapped in a SimpleNamespace so track_messages_call's getattr
    # reads work — assert the token counts round-trip.
    forwarded_usage = kwargs["usage"]
    assert forwarded_usage.input_tokens == 1000
    assert forwarded_usage.output_tokens == 500
    assert forwarded_usage.cache_read_input_tokens == 4000
    assert forwarded_usage.cache_creation_input_tokens == 2000


def test_log_batch_cost_handles_missing_usage_keys(_enabled):
    """Defensive: empty / partial usage dict should still call track_messages_call.

    Token counts default to 0; track_messages_call will record a zero-cost row
    so we keep one ledger entry per completed request even if Anthropic
    omitted the usage block.
    """
    import batch_runner

    with patch.object(batch_runner.cost_collector, "track_messages_call") as mock_track:
        batch_runner._log_batch_cost(
            "self_improve._analyze_changes",
            "claude-sonnet-4-6",
            "msgbatch_empty",
            {},
        )

    mock_track.assert_called_once()
    forwarded_usage = mock_track.call_args.kwargs["usage"]
    assert forwarded_usage.input_tokens == 0
    assert forwarded_usage.output_tokens == 0
    assert forwarded_usage.cache_read_input_tokens == 0
    assert forwarded_usage.cache_creation_input_tokens == 0


def test_per_request_call_site_overrides_batch_level(_enabled, _stub_db):
    """``context.call_site`` per-request wins over the batch-level call_site.

    Lets callers attribute cost to the original function (e.g.
    ``self_heal._analyze_session``) rather than the buffer-flush site
    (``self_heal``) that submitted the batch. Plan #36 task #55 description.
    """
    import batch_runner

    _stub_db["list_submitted"].return_value = [
        {
            "batch_id": "msgbatch_attr_1",
            "call_site": "self_heal",  # batch-level
            "model": "claude-sonnet-4-6",
        }
    ]
    _stub_db["fetch_pending"].return_value = [
        {
            "request_id": "r1",
            "callback_name": "self_heal",
            # Per-request override:
            "context_json": {"call_site": "self_heal._analyze_session"},
        }
    ]
    fake_batch = MagicMock(processing_status="ended")
    result1 = _make_result("r1", "ok")

    with (
        patch.object(
            batch_runner.client.messages.batches,
            "retrieve",
            return_value=fake_batch,
        ),
        patch.object(
            batch_runner.client.messages.batches,
            "results",
            return_value=[result1],
        ),
    ):
        batch_runner.poll_pending_batches({"self_heal": lambda *a, **k: None})

    _stub_db["log_batch_cost"].assert_called_once()
    # First positional arg is call_site (model, batch_id, usage follow).
    args = _stub_db["log_batch_cost"].call_args.args
    assert args[0] == "self_heal._analyze_session"
    assert args[1] == "claude-sonnet-4-6"
    assert args[2] == "msgbatch_attr_1"


def test_missing_call_site_in_context_falls_back_to_batch_level(_enabled, _stub_db):
    """No per-request override → batch-level call_site used.

    Backstop: ensures the existing self_heal / self_improve call sites that
    don't yet thread per-request call_site still write a meaningful row.
    """
    import batch_runner

    _stub_db["list_submitted"].return_value = [
        {
            "batch_id": "msgbatch_fb_1",
            "call_site": "self_improve",
            "model": "claude-sonnet-4-6",
        }
    ]
    _stub_db["fetch_pending"].return_value = [
        {
            "request_id": "r1",
            "callback_name": "self_improve",
            "context_json": {},  # no call_site key
        }
    ]
    fake_batch = MagicMock(processing_status="ended")
    result1 = _make_result("r1", "ok")

    with (
        patch.object(
            batch_runner.client.messages.batches,
            "retrieve",
            return_value=fake_batch,
        ),
        patch.object(
            batch_runner.client.messages.batches,
            "results",
            return_value=[result1],
        ),
    ):
        batch_runner.poll_pending_batches({"self_improve": lambda *a, **k: None})

    _stub_db["log_batch_cost"].assert_called_once()
    args = _stub_db["log_batch_cost"].call_args.args
    assert args[0] == "self_improve"
