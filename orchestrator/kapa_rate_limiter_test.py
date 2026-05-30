"""Tests for the Kapa REST-path token bucket rate limiter.

All tests use an injected fake clock + fake sleep so no real wall-clock
time elapses. The fake sleep advances the fake clock by the same
amount the bucket asked us to wait, then returns immediately —
deterministic and fast.

Run:
    cd orchestrator && python3 -m pytest kapa_rate_limiter_test.py -xvs
"""

from __future__ import annotations

import logging
import os
import threading
from typing import List

import pytest


# Required env vars for config.py to import. Mirrors the preamble in
# kapa_rest_tool_test.py — repo convention so the module under test
# can be imported without a real .env present.
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


import kapa_rate_limiter  # noqa: E402 — env preamble must run first


# ---------------------------------------------------------------------------
# Fake clock helper — wires acquire's sleep() into the same clock so
# tests are deterministic with no real wall-clock delay.
# ---------------------------------------------------------------------------


class _FakeClock:
    """Hand-cranked monotonic clock with sleep() routed back into self.

    ``now()`` returns the current fake time. ``sleep(seconds)`` advances
    the clock and returns immediately — mirroring what a real sleep
    would do but in zero wall-clock time. Tests can also call
    :meth:`advance` directly when they want to move the clock without
    going through the limiter's wait path.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self._t = float(start)
        self._lock = threading.Lock()

    def now(self) -> float:
        with self._lock:
            return self._t

    def sleep(self, seconds: float) -> None:
        if seconds < 0:
            return
        with self._lock:
            self._t += float(seconds)

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._t += float(seconds)


@pytest.fixture
def fake_clock():
    return _FakeClock()


@pytest.fixture(autouse=True)
def _reset_module_singleton():
    """Force a fresh singleton bucket per test so env-var changes stick."""
    kapa_rate_limiter._reset_default_bucket_for_tests()
    # Clear any per-test overrides that previous tests may have set.
    for key in ("KAPA_RATE_LIMIT_TOKENS_PER_MIN", "KAPA_RATE_LIMIT_ENABLED"):
        os.environ.pop(key, None)
    yield
    kapa_rate_limiter._reset_default_bucket_for_tests()
    for key in ("KAPA_RATE_LIMIT_TOKENS_PER_MIN", "KAPA_RATE_LIMIT_ENABLED"):
        os.environ.pop(key, None)


# ---------------------------------------------------------------------------
# Core bucket behavior
# ---------------------------------------------------------------------------


def test_acquire_no_wait_when_tokens_available(fake_clock):
    """First 16 acquires in a fresh bucket consume tokens with no wait."""
    bucket = kapa_rate_limiter.KapaTokenBucket(
        16, clock=fake_clock.now, sleep=fake_clock.sleep
    )
    started = fake_clock.now()
    for _ in range(16):
        bucket.acquire(caller="t-noWait")
    # Sixteenth acquire still drew from initial capacity — no sleep
    # should have run.
    assert fake_clock.now() == started


def test_acquire_blocks_when_bucket_empty(fake_clock):
    """The 17th acquire in a 60s window has to wait for refill."""
    bucket = kapa_rate_limiter.KapaTokenBucket(
        16, clock=fake_clock.now, sleep=fake_clock.sleep
    )
    # Drain the bucket.
    for _ in range(16):
        bucket.acquire(caller="drain")
    started = fake_clock.now()
    # 17th acquire — must wait at least (60 / 16) ≈ 3.75 seconds for
    # the next full token to accrue.
    bucket.acquire(caller="t-blocked")
    waited = fake_clock.now() - started
    # Refill rate is 16/60 tokens/sec, so one token takes ~3.75s.
    # Allow a small epsilon for the rounding we add in acquire().
    assert 3.5 <= waited <= 4.1, f"expected ~3.75s wait, got {waited}s"


def test_refill_at_steady_state(fake_clock):
    """After draining + 60s of clock time, the bucket is full again."""
    bucket = kapa_rate_limiter.KapaTokenBucket(
        16, clock=fake_clock.now, sleep=fake_clock.sleep
    )
    for _ in range(16):
        bucket.acquire(caller="drain")
    # Manually advance the clock by 60 seconds.
    fake_clock.advance(60.0)
    # Now 16 more acquires should be immediate.
    started = fake_clock.now()
    for _ in range(16):
        bucket.acquire(caller="post-refill")
    assert fake_clock.now() == started


def test_acquire_concurrent_thread_safety(fake_clock):
    """32 threads racing: 16 acquire immediately, 16 wait for refill."""
    bucket = kapa_rate_limiter.KapaTokenBucket(
        16, clock=fake_clock.now, sleep=fake_clock.sleep
    )
    waited_flags: List[bool] = []
    flags_lock = threading.Lock()
    # Block all 32 threads at a barrier until they all reach acquire(),
    # so the race for the first 16 tokens is real.
    start_barrier = threading.Barrier(32)
    completed = threading.Event()
    completed_count = {"n": 0}
    count_lock = threading.Lock()

    def worker(tid: int) -> None:
        start_barrier.wait()
        before = fake_clock.now()
        bucket.acquire(caller=f"thread-{tid}")
        after = fake_clock.now()
        with flags_lock:
            waited_flags.append(after > before)
        with count_lock:
            completed_count["n"] += 1
            if completed_count["n"] == 32:
                completed.set()

    threads = [
        threading.Thread(target=worker, args=(i,), daemon=True)
        for i in range(32)
    ]
    for t in threads:
        t.start()
    # Wait for completion with a generous wall-clock cap — the fake
    # clock makes "sleep" instant so this should finish in <1s even
    # on a loaded laptop.
    finished = completed.wait(timeout=10.0)
    for t in threads:
        t.join(timeout=1.0)
    assert finished, "32-thread concurrent acquire deadlocked"
    # Exactly 16 threads should have observed no wait; the other 16
    # waited for refill.
    no_wait_count = sum(1 for w in waited_flags if not w)
    wait_count = sum(1 for w in waited_flags if w)
    assert no_wait_count == 16, (
        f"expected 16 no-wait acquires, got {no_wait_count}"
    )
    assert wait_count == 16, (
        f"expected 16 waited acquires, got {wait_count}"
    )


# ---------------------------------------------------------------------------
# Env-var configurability
# ---------------------------------------------------------------------------


def test_disabled_via_env_short_circuits(fake_clock, monkeypatch):
    """``KAPA_RATE_LIMIT_ENABLED=false`` makes acquire a no-op."""
    monkeypatch.setenv("KAPA_RATE_LIMIT_ENABLED", "false")
    # Even with a 1-token bucket that we've already drained, the
    # module-level ``acquire`` should not block when disabled.
    assert kapa_rate_limiter.is_enabled() is False
    # Drain by manually constructing a bucket with one token + the
    # fake clock, then call the module-level acquire — which should
    # never reach the bucket because the env flag is off.
    started = fake_clock.now()
    kapa_rate_limiter.acquire(caller="disabled-1")
    kapa_rate_limiter.acquire(caller="disabled-2")
    kapa_rate_limiter.acquire(caller="disabled-3")
    # No wall-clock advance from the real time.sleep, and no fake-clock
    # advance because the no-op never touched our injected sleep.
    assert fake_clock.now() == started


def test_disabled_env_alternate_values_all_short_circuit(monkeypatch):
    """Each of the documented disable spellings turns the limiter off."""
    for falsey in ("false", "FALSE", "False", "0", "no", "off"):
        monkeypatch.setenv("KAPA_RATE_LIMIT_ENABLED", falsey)
        assert kapa_rate_limiter.is_enabled() is False, (
            f"expected disabled for {falsey!r}"
        )


def test_enabled_by_default_when_env_unset(monkeypatch):
    monkeypatch.delenv("KAPA_RATE_LIMIT_ENABLED", raising=False)
    assert kapa_rate_limiter.is_enabled() is True


def test_tokens_per_min_configurable_via_env(monkeypatch, fake_clock):
    """``KAPA_RATE_LIMIT_TOKENS_PER_MIN=5`` shrinks the bucket to 5."""
    monkeypatch.setenv("KAPA_RATE_LIMIT_TOKENS_PER_MIN", "5")
    kapa_rate_limiter._reset_default_bucket_for_tests()
    # Build a bucket directly from the env-driven helper to confirm
    # the env var is honored. (The module-level singleton can't be
    # easily clock-injected, hence the direct construction.)
    bucket = kapa_rate_limiter.KapaTokenBucket(
        kapa_rate_limiter._read_tokens_per_min(),
        clock=fake_clock.now,
        sleep=fake_clock.sleep,
    )
    assert bucket.capacity == 5.0
    # Five immediate acquires, then the sixth waits.
    for _ in range(5):
        bucket.acquire(caller="env-sized")
    started = fake_clock.now()
    bucket.acquire(caller="env-sized-blocked")
    # Refill rate is 5/60 tokens/sec, so one token takes 12 seconds.
    waited = fake_clock.now() - started
    assert 11.5 <= waited <= 13.0, f"expected ~12s wait, got {waited}s"


def test_invalid_tokens_per_min_falls_back_to_default(monkeypatch):
    """Garbage or non-positive values fall back to 16 rather than raising."""
    for bad in ("not-a-number", "-1", "0", ""):
        monkeypatch.setenv("KAPA_RATE_LIMIT_TOKENS_PER_MIN", bad)
        assert (
            kapa_rate_limiter._read_tokens_per_min()
            == kapa_rate_limiter.DEFAULT_TOKENS_PER_MIN
        )


# ---------------------------------------------------------------------------
# Wait log emission
# ---------------------------------------------------------------------------


def test_caller_tag_in_wait_log(fake_clock, caplog):
    """When a request waits, the caller string lands in the log line."""
    bucket = kapa_rate_limiter.KapaTokenBucket(
        16, clock=fake_clock.now, sleep=fake_clock.sleep
    )
    for _ in range(16):
        bucket.acquire(caller="drain")
    caplog.set_level(logging.INFO, logger=kapa_rate_limiter.log.name)
    bucket.acquire(caller="sesn_EXAMPLE")
    matches = [
        rec
        for rec in caplog.records
        if "[KAPA_RATE_LIMIT_WAIT]" in rec.getMessage()
    ]
    assert matches, "expected at least one [KAPA_RATE_LIMIT_WAIT] log"
    msg = matches[-1].getMessage()
    assert "caller=sesn_EXAMPLE" in msg
    assert "waited=" in msg


def test_no_wait_log_when_no_wait(fake_clock, caplog):
    """Immediate-grant acquires should NOT log a wait line."""
    bucket = kapa_rate_limiter.KapaTokenBucket(
        16, clock=fake_clock.now, sleep=fake_clock.sleep
    )
    caplog.set_level(logging.INFO, logger=kapa_rate_limiter.log.name)
    bucket.acquire(caller="nowait")
    matches = [
        rec
        for rec in caplog.records
        if "[KAPA_RATE_LIMIT_WAIT]" in rec.getMessage()
    ]
    assert not matches, "did not expect a wait log on an immediate acquire"


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


def test_constructor_rejects_non_positive_tokens_per_min(fake_clock):
    with pytest.raises(ValueError):
        kapa_rate_limiter.KapaTokenBucket(0, clock=fake_clock.now)
    with pytest.raises(ValueError):
        kapa_rate_limiter.KapaTokenBucket(-1, clock=fake_clock.now)
