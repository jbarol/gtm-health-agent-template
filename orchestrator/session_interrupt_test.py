"""Tests for ``orchestrator/session_interrupt.py`` (Plan #44 Task #15).

Covers the helper that emits ``user.interrupt`` to halt a running
session, including:

  - happy path: events.send fired, token total + cost returned
  - missing session_id rejected
  - retrieve failure does NOT prevent the interrupt
  - events.send failure surfaces as ok=False with error
  - thread_id forwarded only when set
  - pricing-table drift guard against ``session_runner.MODEL_COSTS_PER_MTOK``
    (closing-review MEDIUM #4, 2026-05-13)
"""

from __future__ import annotations

import os

# ``config.py`` (transitively reached by ``session_runner``) requires
# these env vars at module import time. Set BEFORE the first
# ``import session_runner`` anywhere. Values are placeholders — the
# repo conftest's ``slack_bolt`` stub keeps the import side-effect-free.
for _k, _v in (
    ("ANTHROPIC_API_KEY", "sk-ant-test"),
    ("SLACK_BOT_TOKEN", "xoxb-test"),
    ("SLACK_APP_TOKEN", "xapp-test"),
    ("SLACK_CHANNEL_ID", "C_TEST"),
    ("ENVIRONMENT_ID", "env_test"),
    ("DREAM_AGENT_ID", "agent_dream_test"),
    ("COORDINATOR_ID", "agent_coord_test"),
    ("QUICK_AGENT_ID", "agent_quick_test"),
    ("METHODOLOGY_STORE_ID", "memstore_test"),
    ("HEALTH_STORE_ID", "memstore_health_test"),
):
    os.environ.setdefault(_k, _v)

from types import SimpleNamespace  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402


def _make_client(
    *, tokens_in=1000, tokens_out=200, fail_send=False, fail_retrieve=False
):
    """Build a MagicMock anthropic.Anthropic client with controllable shape."""
    client = MagicMock(name="anthropic.Anthropic")
    usage = SimpleNamespace(
        input_tokens=tokens_in,
        output_tokens=tokens_out,
        cache_read_input_tokens=0,
        cache_creation=SimpleNamespace(
            ephemeral_5m_input_tokens=0,
            ephemeral_1h_input_tokens=0,
        ),
    )
    session_obj = SimpleNamespace(
        id="sesn_EXAMPLE",
        usage=usage,
        model="claude-opus-4-8",
    )

    if fail_retrieve:
        client.beta.sessions.retrieve.side_effect = RuntimeError("retrieve died")
    else:
        client.beta.sessions.retrieve.return_value = session_obj

    if fail_send:
        client.beta.sessions.events.send.side_effect = RuntimeError("send died")

    return client


def test_interrupt_happy_path_returns_ok_and_token_total():
    import session_interrupt

    client = _make_client(tokens_in=2_000, tokens_out=500)

    result = session_interrupt.interrupt_session("sesn_EXAMPLE", client=client)

    assert result["ok"] is True
    assert result["session_id"] == "sesn_EXAMPLE"
    assert result["tokens_burned"] == 2_500  # 2000 + 500
    assert result["cost_usd"] > 0  # Opus pricing × 2500 tokens
    assert result["error"] == ""
    # events.send was actually called with user.interrupt.
    client.beta.sessions.events.send.assert_called_once()
    send_kwargs = client.beta.sessions.events.send.call_args.kwargs
    assert send_kwargs["session_id"] == "sesn_EXAMPLE"
    assert send_kwargs["events"] == [{"type": "user.interrupt"}]
    # thread_id NOT in kwargs when None.
    assert "thread_id" not in send_kwargs


def test_interrupt_forwards_thread_id_when_set():
    import session_interrupt

    client = _make_client()
    result = session_interrupt.interrupt_session(
        "sesn_EXAMPLE", thread_id="thr_abc", client=client
    )

    assert result["ok"] is True
    send_kwargs = client.beta.sessions.events.send.call_args.kwargs
    assert send_kwargs["thread_id"] == "thr_abc"


def test_interrupt_empty_session_id_rejected():
    import session_interrupt

    result = session_interrupt.interrupt_session("", client=MagicMock())

    assert result["ok"] is False
    assert "session_id is required" in result["error"]


def test_interrupt_retrieve_failure_still_attempts_interrupt():
    """If retrieve dies we lose the token/cost numbers, but we still
    fire user.interrupt — the interrupt is the priority."""
    import session_interrupt

    client = _make_client(fail_retrieve=True)
    result = session_interrupt.interrupt_session("sesn_EXAMPLE", client=client)

    assert result["ok"] is True
    assert result["tokens_burned"] == 0
    assert result["cost_usd"] == 0.0
    client.beta.sessions.events.send.assert_called_once()


def test_interrupt_events_send_failure_returns_ok_false():
    import session_interrupt

    client = _make_client(fail_send=True)
    result = session_interrupt.interrupt_session("sesn_EXAMPLE", client=client)

    assert result["ok"] is False
    assert "events.send failed" in result["error"]
    # Token/cost still populated from the successful retrieve.
    assert result["tokens_burned"] > 0


# ─────────────────────────────────────────────────────────────────────────────
# Pricing-table drift guard (closing-review MEDIUM #4, 2026-05-13)
#
# ``session_interrupt._MODEL_COSTS_PER_MTOK`` is duplicated from
# ``session_runner.MODEL_COSTS_PER_MTOK`` to avoid a circular import
# (Bundle E couldn't pull ``session_runner`` without dragging Slack /
# Anthropic init through ``config.py``). Anytime the canonical table in
# ``session_runner`` moves, this duplicate must move with it or the
# ``/stop`` cost line will drift. This test asserts key + value parity
# so a future pricing refresh fails loud.
# ─────────────────────────────────────────────────────────────────────────────


def _import_session_runner_safely():
    """Import ``session_runner`` for the drift test only.

    ``session_runner`` imports ``slack_bot`` at module load, which our
    repo-level ``conftest.py`` already stubs (``slack_bolt`` MagicMocked).
    Env vars come pre-set via the same conftest .env loader. If either
    fails, this test fails — and that's intentional, because something
    upstream of pricing must also be wrong.
    """
    import session_runner  # noqa: WPS433 — late-bind on purpose

    return session_runner


def test_model_costs_per_mtok_matches_session_runner():
    """Bundle E's duplicated pricing table MUST match the canonical copy
    in ``session_runner.MODEL_COSTS_PER_MTOK`` byte-for-byte.

    Closing-review MEDIUM #4 (2026-05-13): without this assertion the
    two tables can drift silently — /stop would over- or under-estimate
    cost the same day Anthropic ships a price change.
    """
    import session_interrupt

    sr = _import_session_runner_safely()

    canonical = sr.MODEL_COSTS_PER_MTOK
    duplicate = session_interrupt._MODEL_COSTS_PER_MTOK

    # Set equality on the model keys.
    assert set(canonical.keys()) == set(duplicate.keys()), (
        "session_interrupt and session_runner disagree on which models "
        "have pricing — fix one of MODEL_COSTS_PER_MTOK to match the "
        "other before merging."
    )

    # Per-model: same inner shape AND same numeric values.
    for model_id, canonical_rates in canonical.items():
        dup_rates = duplicate[model_id]
        assert set(canonical_rates.keys()) == set(dup_rates.keys()), (
            f"pricing keys disagree for {model_id!r}: "
            f"canonical={sorted(canonical_rates.keys())} "
            f"vs duplicate={sorted(dup_rates.keys())}"
        )
        for rate_key, canonical_val in canonical_rates.items():
            assert dup_rates[rate_key] == canonical_val, (
                f"pricing drift for {model_id!r}.{rate_key}: "
                f"canonical={canonical_val} vs duplicate={dup_rates[rate_key]}"
            )
