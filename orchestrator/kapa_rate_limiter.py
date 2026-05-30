"""In-process token bucket for Kapa REST-path rate limiting.

Why this exists (Layer 2 of the Kapa rate-limit architecture)
-------------------------------------------------------------
Kapa enforces a **20 requests/minute** server-side cap on its Chat
endpoint, shared across the workspace's API key. The cap is shared
between:

  * **MCP-path agents** (Coordinator, Quick Answer, Dream Agent,
    Post-Sales Monitor, Cross-Domain Synthesizer) — these go from
    Anthropic's runtime directly to ``acme.mcp.kapa.example``
    via the vault Bearer credential. Our orchestrator never sees
    those calls. A planned Layer 3 (MCP proxy) will eventually catch
    them.
  * **REST-path agents** (RFP Responder, RFP Reviewer) — these go
    through ``orchestrator/kapa_rest_tool.py`` which the orchestrator
    owns. This module meters that path.

PR #253 (Layer 1) bounds per-agent burst rates inside the Reviewer
prompt. THIS module bounds cross-session contention on the REST path:
if the RFP Responder is mid-processing one file and the Reviewer is
verifying another, the bucket caps total RPM regardless of which
session originated the call.

Design
------
* **Capacity**: 16 tokens by default (80% of the 20 RPM cap). The
  remaining 20% headroom is left for the MCP-path agents which still
  bypass this layer.
* **Refill**: tokens accrue continuously at ``capacity / 60`` per
  second. The bucket's effective level at any instant is
  ``min(capacity, last_tokens + elapsed * refill_rate)``.
* **Acquire**: blocking. Callers either get a token immediately (the
  common case) or sleep until enough refill has accrued. Logged at
  INFO level whenever ``waited_ms > 0`` so ops can spot saturation.
* **Storage**: module-level singleton. The Railway deployment is
  single-replica, so an in-memory bucket is correct. Postgres /
  Redis would add per-call latency for zero benefit — switch only
  if we go multi-replica.

Configurability
---------------
* ``KAPA_RATE_LIMIT_TOKENS_PER_MIN`` — int, default 16. Adjusting
  this is the ops-side knob for tuning headroom without a deploy.
* ``KAPA_RATE_LIMIT_ENABLED`` — ``"true"`` / ``"false"``, default
  true. Emergency kill switch: when false, ``acquire()`` is a no-op
  pass-through and the limiter never blocks.

Both env vars are read **at call time** via ``is_enabled()`` and at
import time for the default singleton. ``KAPA_RATE_LIMIT_ENABLED`` is
deliberately re-read each call so flipping the Railway variable takes
effect on the next request without a redeploy.

Test seam
---------
``KapaTokenBucket.__init__`` takes an optional ``clock`` callable
(default ``time.monotonic``) so tests can inject a fake clock and
deterministically verify wait behavior without spending real seconds.
The blocking ``acquire`` path also accepts a ``sleep`` callable (default
``time.sleep``) so the fake clock doesn't have to advance through real
wall-clock waits.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, Optional

log = logging.getLogger(__name__)


DEFAULT_TOKENS_PER_MIN = 16


def _read_tokens_per_min() -> int:
    """Resolve ``KAPA_RATE_LIMIT_TOKENS_PER_MIN`` with safe fallback.

    Invalid (non-integer, non-positive) values fall back to the
    default rather than raising at import time — a malformed env var
    on Railway should not crash the orchestrator process.
    """
    raw = os.environ.get("KAPA_RATE_LIMIT_TOKENS_PER_MIN", "").strip()
    if not raw:
        return DEFAULT_TOKENS_PER_MIN
    try:
        value = int(raw)
    except (TypeError, ValueError):
        log.warning(
            "KAPA_RATE_LIMIT_TOKENS_PER_MIN=%r not parseable as int; "
            "using default %d",
            raw,
            DEFAULT_TOKENS_PER_MIN,
        )
        return DEFAULT_TOKENS_PER_MIN
    if value <= 0:
        log.warning(
            "KAPA_RATE_LIMIT_TOKENS_PER_MIN=%d must be > 0; using default %d",
            value,
            DEFAULT_TOKENS_PER_MIN,
        )
        return DEFAULT_TOKENS_PER_MIN
    return value


def is_enabled() -> bool:
    """Read ``KAPA_RATE_LIMIT_ENABLED`` at call time (default ``True``).

    Any value other than ``"false"`` / ``"0"`` / ``"no"`` (case-
    insensitive) leaves the limiter enabled. Re-read per call so
    operators can flip the Railway variable without a redeploy.
    """
    raw = os.environ.get("KAPA_RATE_LIMIT_ENABLED", "").strip().lower()
    if raw in ("false", "0", "no", "off"):
        return False
    return True


class KapaTokenBucket:
    """Thread-safe token bucket with continuous refill.

    The bucket starts full. Each :meth:`acquire` removes one token,
    blocking if the bucket is empty until enough refill has accrued.
    Refill is computed lazily (no background thread) — every call
    advances the level by ``elapsed * refill_rate`` before deciding
    whether to grant or wait.

    All state mutations happen under ``self._lock``; the lock is
    released for the actual sleep so concurrent callers can race for
    the next token.
    """

    def __init__(
        self,
        tokens_per_min: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if tokens_per_min <= 0:
            raise ValueError(
                f"tokens_per_min must be > 0, got {tokens_per_min}"
            )
        self.capacity: float = float(tokens_per_min)
        self.refill_per_second: float = float(tokens_per_min) / 60.0
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        # Start with a full bucket so the first burst of up to
        # ``capacity`` requests proceeds without waiting.
        self._tokens: float = float(tokens_per_min)
        self._last_refill: float = clock()

    def _refill_locked(self) -> None:
        """Advance ``self._tokens`` based on elapsed clock time.

        Caller must hold ``self._lock``.
        """
        now = self._clock()
        elapsed = now - self._last_refill
        if elapsed > 0:
            self._tokens = min(
                self.capacity,
                self._tokens + elapsed * self.refill_per_second,
            )
            self._last_refill = now

    def acquire(self, caller: str = "default") -> None:
        """Block until one token is available, then consume it.

        ``caller`` is a free-form tag (session id, agent name) used
        in the ``[KAPA_RATE_LIMIT_WAIT]`` log line so ops can see
        which session bore the wait. Pass ``"unknown"`` (or any
        placeholder) if no obvious identifier is available.
        """
        started = self._clock()
        while True:
            wait_seconds: float
            with self._lock:
                self._refill_locked()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    waited_ms = int((self._clock() - started) * 1000)
                    if waited_ms > 0:
                        log.info(
                            "[KAPA_RATE_LIMIT_WAIT] caller=%s waited=%dms",
                            caller,
                            waited_ms,
                        )
                    return
                # Compute how long until we have a full token. The
                # window is (1 - current_tokens) / refill_rate. Add a
                # tiny epsilon so the next loop iteration finds the
                # bucket >= 1.0 after the sleep — without it, FP
                # rounding can spin briefly at the boundary.
                wait_seconds = max(
                    0.001,
                    (1.0 - self._tokens) / self.refill_per_second + 1e-4,
                )
            # Release the lock before sleeping so other threads can
            # also notice the empty bucket and queue up. They'll race
            # for tokens after the sleeps complete; the lock-checked
            # ``_refill_locked`` ensures only one wins per token.
            self._sleep(wait_seconds)


# Module-level singleton. Initialized at import time from env vars;
# tests that want a fresh bucket should construct ``KapaTokenBucket``
# directly with an injected clock.
_DEFAULT_BUCKET: Optional[KapaTokenBucket] = None
_DEFAULT_BUCKET_LOCK = threading.Lock()


def _get_default_bucket() -> KapaTokenBucket:
    """Lazy singleton accessor — read env at first call."""
    global _DEFAULT_BUCKET
    if _DEFAULT_BUCKET is None:
        with _DEFAULT_BUCKET_LOCK:
            if _DEFAULT_BUCKET is None:
                _DEFAULT_BUCKET = KapaTokenBucket(_read_tokens_per_min())
    return _DEFAULT_BUCKET


def acquire(caller: str = "default") -> None:
    """Acquire one token from the module-level singleton bucket.

    Convenience wrapper used by ``kapa_rest_tool.search_kapa`` so
    callers don't construct their own bucket. When the limiter is
    disabled (``KAPA_RATE_LIMIT_ENABLED=false``) this short-circuits
    to a no-op without touching the bucket.
    """
    if not is_enabled():
        return
    _get_default_bucket().acquire(caller=caller)


def _reset_default_bucket_for_tests() -> None:
    """Force re-initialization of the singleton on next ``acquire``.

    Test-only helper. Production code never calls this; tests that
    mutate env vars between cases use it to ensure the next
    ``acquire()`` picks up the new ``KAPA_RATE_LIMIT_TOKENS_PER_MIN``.
    """
    global _DEFAULT_BUCKET
    with _DEFAULT_BUCKET_LOCK:
        _DEFAULT_BUCKET = None
