"""Live round-trip test for the Kapa REST custom tool.

This test calls the real Kapa API. It is gated by the ``KAPA_LIVE_PROBE=1``
env var so CI does not hit the network on every run — the regular Kapa
test (``kapa_rest_tool_test.py``) is fully mocked and covers correctness.
This file's job is the periodic verification that the streaming endpoint
contract is still intact end-to-end.

Run manually:

    cd <repo>
    KAPA_LIVE_PROBE=1 python -m pytest \
        orchestrator/kapa_rest_tool_live_probe_test.py -q -s

Requires:
  * ``KAPA_ACME_API_KEY`` and ``KAPA_ACME_PROJECT_ID`` set in env
    or ``.env``. The probe module loads ``.env`` itself.
  * Network access to ``api.kapa.ai``.

When ``KAPA_LIVE_PROBE`` is unset (the default), every test in this file
is skipped — no flakes in CI, no surprise paid API calls, no rate-limit
exposure.

The probe logic lives in ``bin/probe_kapa.py``; this test just imports it
and asserts on the structured result. Single source of truth.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Make ``bin/`` importable so we can pull ``run_probe`` from the probe
# script directly. We import the function rather than ``subprocess``ing
# the CLI so pytest collects + reports it like any other test.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "bin"))


LIVE = os.environ.get("KAPA_LIVE_PROBE", "").strip() == "1"

pytestmark = pytest.mark.skipif(
    not LIVE,
    reason="KAPA_LIVE_PROBE=1 not set; live network test skipped by default",
)


def test_live_probe_returns_non_empty_content() -> None:
    """End-to-end: real query → real Kapa stream → non-empty content.

    Uses the canonical "What is FATI?" query — Acme term with dense
    coverage in the Kapa index, so an OK response should carry real
    content rather than an "I don't know." answer.
    """
    from probe_kapa import run_probe  # noqa: E402 — import after path setup

    result = run_probe(query="What is FATI?")

    assert result.ok, (
        f"Kapa live probe failed: error={result.error!r} "
        f"detail={result.detail!r} status={result.http_status} "
        f"elapsed_s={result.elapsed_s}"
    )
    assert result.content.strip(), (
        "Kapa returned ok=True but content was empty — stream framing "
        "regression suspected (Accept header / U+241E delimiter / etc.)"
    )
    assert result.http_status == 200, f"Expected HTTP 200, got {result.http_status}"
    # FATI ("Acme Automated Training Initiative") is a Acme
    # internal term. If the answer says "I don't know" or comes back
    # with <50 chars, the index has lost coverage or auth is partial —
    # surface this as a test failure rather than silent green.
    assert len(result.content) > 50, (
        f"Content suspiciously short ({len(result.content)} chars). "
        "Index degradation or auth scope regression? "
        f"Preview: {result.content[:200]!r}"
    )


def test_live_probe_source_count_is_present() -> None:
    """Sources field should be populated for a query the index covers.

    Soft assertion — Kapa occasionally returns zero sources when the
    answer is fully synthesized rather than chunk-cited. We don't want
    this test to break on a real Kapa behavior change, but we do want
    a non-zero count to be the common case for an internal-term query.
    Asserting ``>= 0`` documents the shape; the real check is that
    ``run_probe`` populates the field at all.
    """
    from probe_kapa import run_probe  # noqa: E402

    result = run_probe(query="What is FATI?")
    assert result.ok, f"probe failed before source check: {result.error}"
    assert result.source_count >= 0, "source_count missing from probe result"
