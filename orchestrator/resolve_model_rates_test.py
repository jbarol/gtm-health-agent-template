"""Tests for _resolve_model_rates — handles dated model IDs returned by Anthropic.

Task #27 (Opus migration verification) found that dated model aliases
(e.g. 'claude-haiku-4-5-20251001') would fall through MODEL_COSTS_PER_MTOK
lookup silently and bill at Sonnet rates. The prefix-match resolution closes
that gap.
"""

from __future__ import annotations


def test_exact_match_returns_canonical_rates():
    from session_runner import _resolve_model_rates, MODEL_COSTS_PER_MTOK

    for key in (
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
    ):
        rates = _resolve_model_rates(key)
        assert rates == MODEL_COSTS_PER_MTOK[key], (
            f"{key!r} did not return canonical rates"
        )


def test_dated_suffix_resolves_to_canonical():
    """Dated model ID resolves via longest-prefix match."""
    from session_runner import _resolve_model_rates, MODEL_COSTS_PER_MTOK

    assert (
        _resolve_model_rates("claude-haiku-4-5-20251001")
        == MODEL_COSTS_PER_MTOK["claude-haiku-4-5"]
    )
    assert (
        _resolve_model_rates("claude-opus-4-8-20260101")
        == MODEL_COSTS_PER_MTOK["claude-opus-4-8"]
    )
    assert (
        _resolve_model_rates("claude-opus-4-7-20260101")
        == MODEL_COSTS_PER_MTOK["claude-opus-4-7"]
    )
    assert (
        _resolve_model_rates("claude-sonnet-4-6-20260315")
        == MODEL_COSTS_PER_MTOK["claude-sonnet-4-6"]
    )


def test_longest_prefix_wins():
    """If both 'claude-opus-4-7' and a longer key existed, the longer one wins.
    Today only one key per family exists, so we just verify the sort is stable.
    """
    from session_runner import _resolve_model_rates, MODEL_COSTS_PER_MTOK

    rates = _resolve_model_rates("claude-opus-4-7-anything-trailing")
    assert rates == MODEL_COSTS_PER_MTOK["claude-opus-4-7"]


def test_unknown_model_falls_back_to_sonnet_with_warning(caplog):
    import logging

    from session_runner import _resolve_model_rates, MODEL_COSTS_PER_MTOK

    with caplog.at_level(logging.WARNING):
        rates = _resolve_model_rates("gpt-4.5-turbo")
    assert rates == MODEL_COSTS_PER_MTOK["claude-sonnet-4-6"]
    assert any("no rate for model_hint" in rec.message for rec in caplog.records)


def test_none_hint_returns_sonnet_default():
    from session_runner import _resolve_model_rates, MODEL_COSTS_PER_MTOK

    assert _resolve_model_rates(None) == MODEL_COSTS_PER_MTOK["claude-sonnet-4-6"]
    assert _resolve_model_rates("") == MODEL_COSTS_PER_MTOK["claude-sonnet-4-6"]


def test_estimate_cost_uses_correct_rates_for_dated_haiku():
    """End-to-end: a dated Haiku model gets Haiku rates, not Sonnet."""
    from session_runner import _estimate_cost

    class FakeUsage:
        input_tokens = 1_000_000
        output_tokens = 0
        cache_read_input_tokens = 0
        cache_creation = None

    cost_with_dated = _estimate_cost(FakeUsage(), "claude-haiku-4-5-20251001")
    cost_with_canonical = _estimate_cost(FakeUsage(), "claude-haiku-4-5")
    cost_with_sonnet = _estimate_cost(FakeUsage(), "claude-sonnet-4-6")

    assert cost_with_dated == cost_with_canonical, (
        "dated form must price like canonical"
    )
    assert cost_with_dated < cost_with_sonnet, (
        "Haiku must cost less than Sonnet at same usage"
    )
    # Concretely: 1M input × $0.80/M = $0.80 for Haiku
    assert abs(cost_with_dated - 0.80) < 0.01
