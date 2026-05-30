"""End-to-end mocked smoke test for the batch processing pipeline (Plan #36 task #57).

What this proves: if this test passes, the full batch_runner plumbing is wired
up correctly — submit -> Anthropic SDK call -> DB persistence -> poll -> result
dispatch -> cost ledger write with ``tier='batch'``.

Mocking strategy:
  * Anthropic SDK ``client.messages.batches.create | retrieve | results`` are
    patched on ``batch_runner.client`` so no network traffic occurs.
  * DB persistence helpers inside ``batch_runner`` (``_record_submission``,
    ``_list_submitted_batch_ids``, ``_fetch_pending_requests``,
    ``_mark_request_complete``, ``_mark_batch_status``) are replaced with an
    in-memory fake store so the lifecycle of a batch row is observable without
    Postgres.
  * The cost ledger boundary (``cost_collector.track_messages_call``) is
    patched at the DB layer — ``db_adapter._connect`` returns a ``_FakeConn``
    that captures executed SQL + params. This lets us assert directly on the
    ``INSERT INTO messages_api_calls`` row including ``tier='batch'``.

Run:
    pytest orchestrator/batch_smoke_test.py -q -m smoke

The whole module is marked ``@pytest.mark.smoke`` so it can be invoked
selectively from CI or local checks.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ── Bootstrapping ───────────────────────────────────────────────────────────
#
# Stub the env vars ``config.py`` requires BEFORE any of the orchestrator
# modules below load. Worktree checkouts don't carry a .env so without these
# stubs the import of ``batch_runner`` (which imports ``config``) raises at
# collection time. setdefault means a real .env (when present) still wins.
for _k, _v in {
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
    os.environ.setdefault(_k, _v)

# Drop any cached half-imported config — same defensive pattern used by
# cost_collector_test.py to avoid a partially-loaded module sticking after a
# prior test's import attempt.
sys.modules.pop("config", None)


# Apply the smoke marker to the whole module so callers can use ``-m smoke``.
pytestmark = pytest.mark.smoke


# ── Fakes ──────────────────────────────────────────────────────────────────


class _FakeCursor:
    """Captures executed SQL + params for assertions.

    Mirrors the helper in ``cost_collector_test.py`` so this test file stays
    self-contained — the smoke harness should not pick up cross-module
    fixtures.
    """

    def __init__(self, fetch_results=None):
        self._fetch_results = list(fetch_results or [])
        self.executed = []  # list of (sql, params) tuples

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        if self._fetch_results:
            return self._fetch_results.pop(0)
        return []


class _FakeConn:
    """Bare-bones psycopg2 conn stand-in. Returns the same cursor on every call."""

    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0
        self.closed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


def _make_succeeded_result(custom_id: str, text: str, usage: dict):
    """Build an SDK-shaped result MagicMock for ``batches.results``."""
    block = MagicMock()
    block.type = "text"
    block.text = text
    msg = MagicMock()
    msg.content = [block]
    msg.usage = SimpleNamespace(**usage)
    inner = MagicMock()
    inner.type = "succeeded"
    inner.message = msg
    res = MagicMock()
    res.custom_id = custom_id
    res.result = inner
    return res


# ── Smoke test ─────────────────────────────────────────────────────────────


def test_batch_e2e_smoke():
    """Submit -> poll -> dispatch -> cost-ledger round trip.

    The test exercises every public surface of ``batch_runner`` end-to-end:

    1. ``submit_batch`` is called with two simple prompt requests against
       ``claude-sonnet-4-6``. The Anthropic SDK ``messages.batches.create`` is
       patched to return a fake batch ID. The returned ``batch_id`` proves
       the submission path works without network access.

    2. ``poll_pending_batches`` is called with a callback registered for the
       ``self_heal`` call site. ``messages.batches.retrieve`` returns
       ``processing_status='ended'`` and ``messages.batches.results`` returns
       two succeeded result objects with realistic token usage. The callback
       captures dispatched ``(request_id, context, text, usage)`` tuples so
       we can verify the round-trip.

    3. Each succeeded result triggers ``_log_batch_cost`` which forwards to
       ``cost_collector.track_messages_call(tier='batch', batch_id=...)``.
       That call routes through ``db_adapter._connect`` — patched here to
       return a ``_FakeConn`` whose cursor captures the executed SQL +
       params. The smoke test asserts the resulting
       ``INSERT INTO messages_api_calls`` row carries ``tier='batch'``.
    """
    import batch_runner
    import db_adapter

    # Two simple prompt requests — matches the "small batch" scope of task #57.
    requests = [
        {
            "custom_id": "smoke_req_1",
            "params": {
                "model": "claude-sonnet-4-6",
                "max_tokens": 64,
                "messages": [{"role": "user", "content": "Say one short word."}],
            },
            "context": {"session_id": "smoke_req_1", "purpose": "smoke"},
        },
        {
            "custom_id": "smoke_req_2",
            "params": {
                "model": "claude-sonnet-4-6",
                "max_tokens": 64,
                "messages": [{"role": "user", "content": "Reply with OK."}],
            },
            "context": {"session_id": "smoke_req_2", "purpose": "smoke"},
        },
    ]

    # In-memory store standing in for the ``batch_jobs`` / ``batch_job_requests``
    # tables. The poll path reads from these via list_submitted / fetch_pending
    # after submit_batch records into them.
    submitted_rows: list[dict] = []
    pending_rows_by_batch: dict[str, list[dict]] = {}
    request_completion_log: list[tuple] = []
    batch_status_log: list[tuple] = []

    def _fake_record_submission(batch_id, call_site, model, reqs, callback_name):
        submitted_rows.append(
            {"batch_id": batch_id, "call_site": call_site, "model": model}
        )
        pending_rows_by_batch[batch_id] = [
            {
                "request_id": r["custom_id"],
                "callback_name": callback_name,
                "context_json": r.get("context") or {},
            }
            for r in reqs
        ]

    def _fake_list_submitted():
        return list(submitted_rows)

    def _fake_fetch_pending(batch_id):
        return list(pending_rows_by_batch.get(batch_id, []))

    def _fake_mark_request_complete(batch_id, request_id, text, usage, status):
        request_completion_log.append((batch_id, request_id, text, usage, status))

    def _fake_mark_batch_status(batch_id, status, error_message=None):
        batch_status_log.append((batch_id, status, error_message))

    # SDK-shaped batch returned by ``client.messages.batches.create``.
    fake_created_batch = MagicMock(id="msgbatch_smoke_001")
    fake_retrieved_batch = MagicMock(processing_status="ended")
    fake_results = [
        _make_succeeded_result(
            "smoke_req_1",
            "one",
            usage={
                "input_tokens": 50,
                "output_tokens": 5,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        ),
        _make_succeeded_result(
            "smoke_req_2",
            "OK",
            usage={
                "input_tokens": 60,
                "output_tokens": 2,
                "cache_read_input_tokens": 10,
                "cache_creation_input_tokens": 0,
            },
        ),
    ]

    # Cost-ledger DB stub: capture every INSERT.
    cursor = _FakeCursor()
    fake_conn = _FakeConn(cursor)

    # Callback used by poll_pending_batches. The real ``self_heal`` callback
    # would persist learnings — here we just record the dispatched payload so
    # we can assert the round-trip.
    received = []

    def smoke_callback(request_id, context, text, usage):
        received.append(
            {"request_id": request_id, "context": context, "text": text, "usage": usage}
        )

    with (
        # Force the kill-switch on.
        patch.object(batch_runner, "BATCH_PROCESSING_ENABLED", True),
        # Anthropic SDK calls stay offline.
        patch.object(
            batch_runner.client.messages.batches,
            "create",
            return_value=fake_created_batch,
        ) as mock_create,
        patch.object(
            batch_runner.client.messages.batches,
            "retrieve",
            return_value=fake_retrieved_batch,
        ) as mock_retrieve,
        patch.object(
            batch_runner.client.messages.batches,
            "results",
            return_value=fake_results,
        ) as mock_results,
        # In-memory persistence stand-ins for batch_runner DB helpers.
        patch.object(batch_runner, "_record_submission", _fake_record_submission),
        patch.object(batch_runner, "_list_submitted_batch_ids", _fake_list_submitted),
        patch.object(batch_runner, "_fetch_pending_requests", _fake_fetch_pending),
        patch.object(
            batch_runner, "_mark_request_complete", _fake_mark_request_complete
        ),
        patch.object(batch_runner, "_mark_batch_status", _fake_mark_batch_status),
        # Cost-ledger DB stub so track_messages_call writes to our fake cursor.
        patch.object(db_adapter, "DATABASE_URL", "postgres://smoke"),
        patch.object(db_adapter, "_connect", lambda: fake_conn),
    ):
        # ── Step 1: submit ──
        batch_id = batch_runner.submit_batch(
            call_site="self_heal",
            model="claude-sonnet-4-6",
            requests=requests,
        )

        # ── Step 2: poll ──
        completed = batch_runner.poll_pending_batches({"self_heal": smoke_callback})

    # ── Assertions ──

    # Submission proves the SDK call + persistence path.
    assert batch_id == "msgbatch_smoke_001"
    mock_create.assert_called_once()
    # The "context" field must be stripped before reaching Anthropic.
    api_requests = mock_create.call_args.kwargs["requests"]
    assert api_requests == [
        {"custom_id": "smoke_req_1", "params": requests[0]["params"]},
        {"custom_id": "smoke_req_2", "params": requests[1]["params"]},
    ]
    # Persistence captured the batch + per-request rows.
    assert submitted_rows == [
        {
            "batch_id": "msgbatch_smoke_001",
            "call_site": "self_heal",
            "model": "claude-sonnet-4-6",
        }
    ]
    assert {r["request_id"] for r in pending_rows_by_batch["msgbatch_smoke_001"]} == {
        "smoke_req_1",
        "smoke_req_2",
    }

    # Polling moved the batch to ended and dispatched callbacks.
    assert completed == 1
    mock_retrieve.assert_called_once_with("msgbatch_smoke_001")
    mock_results.assert_called_once_with("msgbatch_smoke_001")
    assert ("msgbatch_smoke_001", "ended", None) in batch_status_log

    # Round-trip: both requests came back through the callback with their
    # context preserved and their text + usage forwarded intact.
    assert len(received) == 2
    by_id = {r["request_id"]: r for r in received}
    assert by_id["smoke_req_1"]["context"] == {
        "session_id": "smoke_req_1",
        "purpose": "smoke",
    }
    assert by_id["smoke_req_1"]["text"] == "one"
    assert by_id["smoke_req_1"]["usage"]["input_tokens"] == 50
    assert by_id["smoke_req_1"]["usage"]["output_tokens"] == 5
    assert by_id["smoke_req_2"]["text"] == "OK"
    assert by_id["smoke_req_2"]["usage"]["cache_read_input_tokens"] == 10

    # Each request row was marked complete with succeeded status.
    completion_by_id = {row[1]: row for row in request_completion_log}
    assert completion_by_id["smoke_req_1"][4] == "succeeded"
    assert completion_by_id["smoke_req_2"][4] == "succeeded"

    # ── Cost ledger assertion ──
    # Two succeeded results must produce two INSERT INTO messages_api_calls
    # rows, each tagged tier='batch' with the same batch_id.
    inserts = [
        (sql, params)
        for sql, params in cursor.executed
        if "INSERT INTO messages_api_calls" in sql
    ]
    assert len(inserts) == 2, (
        f"expected 2 messages_api_calls rows, got {len(inserts)}: "
        f"{[s for s, _ in cursor.executed]}"
    )
    for _sql, params in inserts:
        # See cost_collector.track_messages_call:
        # (call_site, model, input_tokens, output_tokens, cache_read_tokens,
        #  cache_write_tokens, cost_usd, tier, batch_id)
        (
            call_site,
            model,
            _inp,
            _out,
            _cr,
            _cw,
            _cost,
            tier,
            batch_id_persisted,
        ) = params
        assert call_site == "self_heal"
        assert model == "claude-sonnet-4-6"
        assert tier == "batch"
        assert batch_id_persisted == "msgbatch_smoke_001"

    # And the cost must be computed at the batch-tier multiplier (50% on input
    # + output). Sonnet 4.6: input=$3, output=$15. Request 1 has 50 input + 5
    # output tokens => realtime $0.000225, batch $0.0001125. Cache read is 0
    # so the batch multiplier alone drives the math.
    req1_params = next(
        params for _, params in inserts if params[0] == "self_heal" and params[2] == 50
    )
    req1_cost = req1_params[6]
    assert abs(req1_cost - 0.0001125) < 1e-9
