"""Tests for orchestrator/compresr_regression_guard.py (Plan #37, Task #67).

Fully mocked — no real DB, no real Slack. Covers:

  * Baseline math: rate = failures / total per window.
  * Threshold gates: 1.9x baseline does NOT trip; 2.1x baseline DOES trip.
  * Insufficient-samples gates: both windows must have at least
    MIN_SAMPLES rows for the check to fire.
  * Zero-baseline edge case: when 14d baseline has zero failures, any recent
    failure rate above an absolute floor (5%) trips; below stays clean.
  * Dedup: ``disable_call_site`` posts the Slack notice on the first call but
    suppresses subsequent calls for the same call site on the same day.
  * Slack notice fan-out: every admin user gets a DM; Slack-side failures are
    swallowed so one bad user doesn't break the others.
  * ``record_parse_outcome``: stamps the most recent non-fallback row in
    ``compresr_calls.downstream_ok``.
  * ``is_disabled``: reads ``compresr_site_disabled``; integrated check via
    ``compresr_client.compress_prompt`` short-circuits when disabled and
    records the fallback row with reason ``regression_disabled``.

Run:
    cd orchestrator && python3 -m pytest compresr_regression_guard_test.py -q
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Required env vars for config.py to import without raising. setdefault means a
# real .env (when present) takes precedence — mirrors compresr_client_test.py.
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


# ---------------------------------------------------------------------------
# Fake DB plumbing
# ---------------------------------------------------------------------------


class _FakeCursor:
    """Cursor that routes a small set of recognized SQL statements through an
    in-memory store. Anything else is silently no-op'd (matches the guard's
    best-effort contract — a missing column on a future migration must not
    crash the calling code)."""

    def __init__(self, store: dict):
        self._store = store
        self._fetched = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql: str, params=None):
        sql_norm = " ".join(sql.split())  # collapse whitespace
        sql_lower = sql_norm.lower()
        params = params or ()

        # ─── compresr_calls aggregate (recent / baseline windows) ──────────
        if "from compresr_calls" in sql_lower and "count(*)" in sql_lower:
            # ``params`` is (call_site,); the window is encoded in the SQL
            # string so we use a marker the test set in the store.
            call_site = params[0] if params else None
            if (
                "interval '24 hours'" in sql_lower
                and "interval '14 days'" not in sql_lower
            ):
                window = "recent"
            elif "interval '14 days'" in sql_lower:
                window = "baseline"
            else:
                window = "unknown"
            counts = self._store.setdefault("windows", {}).get(
                (call_site, window), (0, 0)
            )
            self._fetched = (counts[0], counts[1])
            return

        # ─── compresr_calls UPDATE downstream_ok ─────────────────────────
        if "update compresr_calls" in sql_lower and "downstream_ok" in sql_lower:
            ok_value, call_site = params
            self._store.setdefault("recorded", []).append(
                {"call_site": call_site, "ok": ok_value}
            )
            return

        # ─── compresr_site_disabled SELECT (is_disabled) ─────────────────
        if (
            "select 1 from compresr_site_disabled" in sql_lower
            and "disabled_at::date" not in sql_lower
        ):
            call_site = params[0]
            row = self._store.setdefault("disabled", {}).get(call_site)
            self._fetched = (1,) if row else None
            return

        # ─── compresr_site_disabled SELECT (_already_notified_today) ─────
        if (
            "select 1 from compresr_site_disabled" in sql_lower
            and "disabled_at::date" in sql_lower
        ):
            call_site, today = params
            row = self._store.setdefault("disabled", {}).get(call_site)
            # Treat any pre-populated "notified_today" entry as today's row.
            notified_today = self._store.setdefault("notified_today", set())
            if call_site in notified_today:
                self._fetched = (1,)
            else:
                self._fetched = (1,) if row else None
            return

        # ─── compresr_site_disabled INSERT (disable_call_site) ──────────
        if "insert into compresr_site_disabled" in sql_lower:
            call_site, reason = params
            self._store.setdefault("disabled", {})[call_site] = {
                "reason": reason,
            }
            self._store.setdefault("notified_today", set()).add(call_site)
            return

        # Unknown SQL → no-op (the guard's best-effort behavior).
        self._fetched = None

    def fetchone(self):
        return self._fetched


class _FakeConn:
    def __init__(self, store: dict):
        self._store = store

    def cursor(self, cursor_factory=None):
        return _FakeCursor(self._store)

    def commit(self):
        pass

    def close(self):
        pass


@pytest.fixture
def fake_db():
    store: dict = {
        "windows": {},
        "disabled": {},
        "recorded": [],
        "notified_today": set(),
    }
    with (
        patch("db_adapter.DATABASE_URL", "postgres://test"),
        patch("db_adapter._connect", lambda: _FakeConn(store)),
    ):
        yield store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _set_window(store: dict, call_site: str, window: str, total: int, failures: int):
    """Pre-populate the (call_site, window) bucket so ``_failure_rate`` returns
    these numbers when the guard's SQL hits the fake cursor."""
    store.setdefault("windows", {})[(call_site, window)] = (total, failures)


# ---------------------------------------------------------------------------
# Baseline math
# ---------------------------------------------------------------------------


def test_compute_rates_returns_expected_shape(fake_db):
    """The compute_rates dict carries both windows + threshold + trip flag."""
    import compresr_regression_guard as guard

    _set_window(fake_db, "self_heal", "recent", total=100, failures=10)  # 10%
    _set_window(fake_db, "self_heal", "baseline", total=140, failures=7)  # 5%

    out = guard.compute_rates("self_heal")
    assert out["call_site"] == "self_heal"
    assert out["recent"]["total"] == 100
    assert out["recent"]["failures"] == 10
    assert out["recent"]["rate"] == pytest.approx(0.10)
    assert out["baseline"]["total"] == 140
    assert out["baseline"]["failures"] == 7
    assert out["baseline"]["rate"] == pytest.approx(0.05)
    # threshold = baseline.rate * 2.0 = 0.10
    assert out["threshold"] == pytest.approx(0.10)
    # 10% > 10% is False — does NOT trip (use strict > in the guard).
    assert out["trips"] is False
    assert out["reason"] == "within_threshold"


# ---------------------------------------------------------------------------
# Threshold gate — 1.9x vs 2.1x
# ---------------------------------------------------------------------------


def test_threshold_gate_19x_does_not_trip(fake_db):
    """1.9x the baseline must NOT trip the guard."""
    import compresr_regression_guard as guard

    # baseline = 10%, recent = 19% → 1.9x baseline → no trip.
    _set_window(fake_db, "self_heal", "recent", total=100, failures=19)
    _set_window(fake_db, "self_heal", "baseline", total=200, failures=20)

    out = guard.compute_rates("self_heal")
    assert out["trips"] is False
    assert out["reason"] == "within_threshold"
    assert guard.should_auto_disable("self_heal") is False


def test_threshold_gate_21x_trips(fake_db):
    """2.1x the baseline must trip the guard."""
    import compresr_regression_guard as guard

    # baseline = 10%, recent = 21% → 2.1x baseline → trips.
    _set_window(fake_db, "self_heal", "recent", total=100, failures=21)
    _set_window(fake_db, "self_heal", "baseline", total=200, failures=20)

    out = guard.compute_rates("self_heal")
    assert out["trips"] is True
    assert out["reason"] == "recent_exceeds_2x_baseline"
    assert guard.should_auto_disable("self_heal") is True


def test_exactly_2x_does_not_trip(fake_db):
    """Exactly 2.0x the baseline is the boundary — the guard uses ``>``,
    not ``>=``, so the boundary does NOT trip."""
    import compresr_regression_guard as guard

    # baseline = 5%, recent = 10% → exactly 2x → boundary, no trip.
    _set_window(fake_db, "self_heal", "recent", total=100, failures=10)
    _set_window(fake_db, "self_heal", "baseline", total=200, failures=10)

    out = guard.compute_rates("self_heal")
    assert out["threshold"] == pytest.approx(0.10)
    assert out["recent"]["rate"] == pytest.approx(0.10)
    assert out["trips"] is False


# ---------------------------------------------------------------------------
# Insufficient samples
# ---------------------------------------------------------------------------


def test_insufficient_recent_samples_does_not_trip(fake_db):
    """Below MIN_SAMPLES in the recent window, guard stays silent."""
    import compresr_regression_guard as guard

    # Recent has only 5 rows even though 100% are failures.
    _set_window(fake_db, "self_heal", "recent", total=5, failures=5)
    _set_window(fake_db, "self_heal", "baseline", total=200, failures=2)

    out = guard.compute_rates("self_heal")
    assert out["trips"] is False
    assert out["reason"] == "insufficient_recent_samples"


def test_insufficient_baseline_samples_does_not_trip(fake_db):
    """Below MIN_SAMPLES in the baseline window, guard stays silent."""
    import compresr_regression_guard as guard

    _set_window(fake_db, "self_heal", "recent", total=100, failures=80)
    _set_window(fake_db, "self_heal", "baseline", total=3, failures=0)

    out = guard.compute_rates("self_heal")
    assert out["trips"] is False
    assert out["reason"] == "insufficient_baseline_samples"


# ---------------------------------------------------------------------------
# Zero-baseline edge case
# ---------------------------------------------------------------------------


def test_zero_baseline_with_high_recent_trips(fake_db):
    """When baseline has zero failures but recent shows >= 5%, trip."""
    import compresr_regression_guard as guard

    _set_window(fake_db, "self_heal", "recent", total=100, failures=10)  # 10%
    _set_window(fake_db, "self_heal", "baseline", total=200, failures=0)  # 0%

    out = guard.compute_rates("self_heal")
    assert out["trips"] is True
    assert out["reason"] == "baseline_zero_recent_nonzero"
    assert out["threshold"] == pytest.approx(0.05)


def test_zero_baseline_with_low_recent_does_not_trip(fake_db):
    """When baseline has zero failures and recent is below 5%, stay clean."""
    import compresr_regression_guard as guard

    _set_window(fake_db, "self_heal", "recent", total=100, failures=3)  # 3%
    _set_window(fake_db, "self_heal", "baseline", total=200, failures=0)  # 0%

    out = guard.compute_rates("self_heal")
    assert out["trips"] is False
    assert out["reason"] == "baseline_zero_recent_clean"


# ---------------------------------------------------------------------------
# Dedup — Slack notice posted once per call site per day
# ---------------------------------------------------------------------------


def test_disable_call_site_posts_slack_once_per_day(fake_db, monkeypatch):
    """First disable_call_site call sends DMs; second on the same day suppresses
    the Slack notice but still refreshes the DB row."""
    import compresr_regression_guard as guard

    # Pre-populate windows so compute_rates returns non-trivial numbers.
    _set_window(fake_db, "self_heal", "recent", total=100, failures=25)
    _set_window(fake_db, "self_heal", "baseline", total=200, failures=10)

    sent_messages: list[tuple[str, str]] = []

    def _fake_send_dm(uid, text):
        sent_messages.append((uid, text))

    fake_slack_bot = MagicMock(name="slack_bot_module")
    fake_slack_bot.send_dm = _fake_send_dm
    monkeypatch.setitem(sys.modules, "slack_bot", fake_slack_bot)

    monkeypatch.setattr("cost_digest._resolve_admin_ids", lambda: ["U001", "U002"])

    # First disable: DMs both admins.
    guard.disable_call_site("self_heal", "test-reason")
    assert len(sent_messages) == 2
    assert {m[0] for m in sent_messages} == {"U001", "U002"}
    assert "Compresr auto-disabled" in sent_messages[0][1]
    assert "`self_heal`" in sent_messages[0][1]

    # Second disable on the same day: no new DMs (notified_today dedup).
    guard.disable_call_site("self_heal", "test-reason-again")
    assert len(sent_messages) == 2  # unchanged

    # DB row is refreshed even when the Slack notice is suppressed.
    assert fake_db["disabled"]["self_heal"]["reason"] == "test-reason-again"


def test_disable_call_site_per_site_dedup_isolated(fake_db, monkeypatch):
    """Disabling self_heal does NOT suppress a same-day self_improve notice."""
    import compresr_regression_guard as guard

    _set_window(fake_db, "self_heal", "recent", total=100, failures=25)
    _set_window(fake_db, "self_heal", "baseline", total=200, failures=10)
    _set_window(fake_db, "self_improve", "recent", total=100, failures=20)
    _set_window(fake_db, "self_improve", "baseline", total=200, failures=5)

    sent_messages: list = []
    fake_slack_bot = MagicMock(name="slack_bot_module")
    fake_slack_bot.send_dm = lambda u, t: sent_messages.append((u, t))
    monkeypatch.setitem(sys.modules, "slack_bot", fake_slack_bot)
    monkeypatch.setattr("cost_digest._resolve_admin_ids", lambda: ["U001"])

    guard.disable_call_site("self_heal", "r1")
    assert len(sent_messages) == 1
    guard.disable_call_site("self_improve", "r2")
    assert len(sent_messages) == 2  # per-site notice — not deduped


def test_slack_failure_does_not_propagate(fake_db, monkeypatch):
    """A raising send_dm must not bubble out of disable_call_site."""
    import compresr_regression_guard as guard

    _set_window(fake_db, "self_heal", "recent", total=100, failures=25)
    _set_window(fake_db, "self_heal", "baseline", total=200, failures=10)

    fake_slack_bot = MagicMock(name="slack_bot_module")
    fake_slack_bot.send_dm = MagicMock(side_effect=RuntimeError("slack down"))
    monkeypatch.setitem(sys.modules, "slack_bot", fake_slack_bot)
    monkeypatch.setattr("cost_digest._resolve_admin_ids", lambda: ["U001"])

    # Must NOT raise.
    guard.disable_call_site("self_heal", "boom")
    # Row still persisted.
    assert fake_db["disabled"]["self_heal"]["reason"] == "boom"


def test_no_admins_logged_not_raised(fake_db, monkeypatch):
    """When no admins are configured, disable_call_site logs and returns."""
    import compresr_regression_guard as guard

    _set_window(fake_db, "self_heal", "recent", total=100, failures=25)
    _set_window(fake_db, "self_heal", "baseline", total=200, failures=10)

    monkeypatch.setattr("cost_digest._resolve_admin_ids", lambda: [])

    guard.disable_call_site("self_heal", "no-admins")
    assert fake_db["disabled"]["self_heal"]["reason"] == "no-admins"


# ---------------------------------------------------------------------------
# is_disabled (PK lookup)
# ---------------------------------------------------------------------------


def test_is_disabled_reads_compresr_site_disabled(fake_db):
    """is_disabled returns True iff there's a row for the call site."""
    import compresr_regression_guard as guard

    assert guard.is_disabled("self_heal") is False
    fake_db["disabled"]["self_heal"] = {"reason": "x"}
    assert guard.is_disabled("self_heal") is True
    assert guard.is_disabled("self_improve") is False


def test_is_disabled_returns_false_without_database(monkeypatch):
    """No DATABASE_URL → is_disabled is False (compression unblocked)."""
    import compresr_regression_guard as guard

    monkeypatch.setattr("db_adapter.DATABASE_URL", "")
    assert guard.is_disabled("self_heal") is False


def test_is_disabled_swallows_db_errors(monkeypatch):
    """A raising _connect() → is_disabled returns False (fail open)."""
    import compresr_regression_guard as guard

    monkeypatch.setattr("db_adapter.DATABASE_URL", "postgres://test")
    monkeypatch.setattr(
        "db_adapter._connect", MagicMock(side_effect=RuntimeError("db down"))
    )
    assert guard.is_disabled("self_heal") is False


# ---------------------------------------------------------------------------
# record_parse_outcome
# ---------------------------------------------------------------------------


def test_record_parse_outcome_stamps_latest_row(fake_db):
    """record_parse_outcome writes a row keyed on (call_site, ok)."""
    import compresr_regression_guard as guard

    guard.record_parse_outcome("self_heal", parsed_ok=True)
    guard.record_parse_outcome("self_heal", parsed_ok=False)
    guard.record_parse_outcome("self_improve", parsed_ok=True)

    recorded = fake_db["recorded"]
    assert len(recorded) == 3
    assert recorded[0] == {"call_site": "self_heal", "ok": True}
    assert recorded[1] == {"call_site": "self_heal", "ok": False}
    assert recorded[2] == {"call_site": "self_improve", "ok": True}


def test_record_parse_outcome_silent_when_db_unavailable(monkeypatch):
    """No DATABASE_URL → record_parse_outcome is a silent no-op."""
    import compresr_regression_guard as guard

    monkeypatch.setattr("db_adapter.DATABASE_URL", "")
    # Must not raise.
    guard.record_parse_outcome("self_heal", parsed_ok=False)


def test_record_parse_outcome_swallows_db_errors(monkeypatch):
    """A raising _connect() must not propagate."""
    import compresr_regression_guard as guard

    monkeypatch.setattr("db_adapter.DATABASE_URL", "postgres://test")
    monkeypatch.setattr(
        "db_adapter._connect", MagicMock(side_effect=RuntimeError("db down"))
    )
    guard.record_parse_outcome("self_heal", parsed_ok=False)


# ---------------------------------------------------------------------------
# run_regression_check cron entry point
# ---------------------------------------------------------------------------


def test_run_regression_check_disables_only_tripped_sites(fake_db, monkeypatch):
    """run_regression_check sweeps known sites and only disables those that
    cross the threshold."""
    import compresr_regression_guard as guard

    # self_heal trips (recent 22%, baseline 10%).
    _set_window(fake_db, "self_heal", "recent", total=100, failures=22)
    _set_window(fake_db, "self_heal", "baseline", total=200, failures=20)
    # self_improve stays clean (recent 5%, baseline 4%).
    _set_window(fake_db, "self_improve", "recent", total=100, failures=5)
    _set_window(fake_db, "self_improve", "baseline", total=200, failures=8)

    monkeypatch.setattr("cost_digest._resolve_admin_ids", lambda: [])

    summary = guard.run_regression_check()
    assert summary["tripped"] == ["self_heal"]
    assert "self_heal" in fake_db["disabled"]
    assert "self_improve" not in fake_db["disabled"]


def test_run_regression_check_does_not_raise_on_db_error(fake_db, monkeypatch):
    """A query failure inside compute_rates for one site must not break the
    sweep — the other site still gets evaluated."""
    import compresr_regression_guard as guard

    _set_window(fake_db, "self_improve", "recent", total=100, failures=22)
    _set_window(fake_db, "self_improve", "baseline", total=200, failures=10)

    # Force compute_rates to raise for self_heal only.
    real_compute_rates = guard.compute_rates

    def _maybe_raise(call_site):
        if call_site == "self_heal":
            raise RuntimeError("simulated DB error")
        return real_compute_rates(call_site)

    monkeypatch.setattr(guard, "compute_rates", _maybe_raise)
    monkeypatch.setattr("cost_digest._resolve_admin_ids", lambda: [])

    summary = guard.run_regression_check()
    # self_improve still tripped despite self_heal failing.
    assert "self_improve" in summary["tripped"]


# ---------------------------------------------------------------------------
# Integration with compresr_client.compress_prompt
# ---------------------------------------------------------------------------


def test_compress_prompt_short_circuits_when_site_disabled(monkeypatch):
    """compress_prompt must fall back when compresr_regression_guard.is_disabled
    returns True, and the telemetry row carries reason='regression_disabled'."""
    import compresr_client
    import compresr_regression_guard
    import config

    # Reset any module state from earlier tests.
    compresr_client._CIRCUIT_STATE.clear()
    compresr_client._WARNED_NO_KEY.clear()
    compresr_client._WARNED_SDK_MISSING = False

    monkeypatch.setattr(config, "COMPRESR_API_KEY", "cmp_test_key")
    monkeypatch.setattr(config, "COMPRESS_SELF_HEAL_ENABLED", True)

    # Force is_disabled to return True regardless of DB state.
    monkeypatch.setattr(
        compresr_regression_guard, "is_disabled", lambda call_site: True
    )

    calls_recorded: list = []

    def _capture_record_call(**kwargs):
        calls_recorded.append(kwargs)

    monkeypatch.setattr(compresr_client, "_record_call", _capture_record_call)

    # Fake SDK should NOT be called.
    fake_sdk_invocations: list = []

    def _fake_call_compresr(text, *, model, query):
        fake_sdk_invocations.append((text, model, query))
        return "SHOULD-NOT-BE-RETURNED"

    monkeypatch.setattr(compresr_client, "_call_compresr", _fake_call_compresr)

    payload = "x" * 5000
    out = compresr_client.compress_prompt(
        payload,
        model="latte_v1",
        query="why",
        call_site="self_heal",
        min_chars=100,
    )
    assert out == payload  # original returned
    assert fake_sdk_invocations == []  # SDK not invoked
    assert len(calls_recorded) == 1
    row = calls_recorded[0]
    assert row["fallback"] is True
    assert row["fallback_reason"] == "regression_disabled"


def test_compress_prompt_proceeds_when_site_not_disabled(monkeypatch):
    """When is_disabled returns False, normal compression proceeds."""
    import compresr_client
    import compresr_regression_guard
    import config

    compresr_client._CIRCUIT_STATE.clear()
    compresr_client._WARNED_NO_KEY.clear()
    compresr_client._WARNED_SDK_MISSING = False

    monkeypatch.setattr(config, "COMPRESR_API_KEY", "cmp_test_key")
    monkeypatch.setattr(config, "COMPRESS_SELF_HEAL_ENABLED", True)

    monkeypatch.setattr(
        compresr_regression_guard, "is_disabled", lambda call_site: False
    )

    def _fake_call_compresr(text, *, model, query):
        return "COMPRESSED"

    monkeypatch.setattr(compresr_client, "_call_compresr", _fake_call_compresr)
    monkeypatch.setattr(compresr_client, "_cache_lookup", lambda *a, **k: None)
    monkeypatch.setattr(compresr_client, "_cache_put", lambda *a, **k: None)
    monkeypatch.setattr(compresr_client, "_record_call", lambda **kw: None)

    out = compresr_client.compress_prompt(
        "x" * 5000,
        model="latte_v1",
        query="why",
        call_site="self_heal",
        min_chars=100,
    )
    assert out == "COMPRESSED"


def test_compress_prompt_guard_unimportable_does_not_break(monkeypatch):
    """If compresr_regression_guard fails to import, compress_prompt must still
    work end-to-end (fail open)."""
    import compresr_client
    import config

    compresr_client._CIRCUIT_STATE.clear()
    compresr_client._WARNED_NO_KEY.clear()
    compresr_client._WARNED_SDK_MISSING = False

    monkeypatch.setattr(config, "COMPRESR_API_KEY", "cmp_test_key")
    monkeypatch.setattr(config, "COMPRESS_SELF_IMPROVE_ENABLED", True)

    # Make the guard module unimportable.
    monkeypatch.setitem(sys.modules, "compresr_regression_guard", None)

    monkeypatch.setattr(
        compresr_client, "_call_compresr", lambda t, **k: "COMPRESSED-OK"
    )
    monkeypatch.setattr(compresr_client, "_cache_lookup", lambda *a, **k: None)
    monkeypatch.setattr(compresr_client, "_cache_put", lambda *a, **k: None)
    monkeypatch.setattr(compresr_client, "_record_call", lambda **kw: None)

    out = compresr_client.compress_prompt(
        "x" * 5000,
        model="espresso_v1",
        call_site="self_improve",
        min_chars=100,
    )
    assert out == "COMPRESSED-OK"


# ---------------------------------------------------------------------------
# Constants sanity
# ---------------------------------------------------------------------------


def test_constants_match_plan():
    """Plan #37 task #67 says: 2x baseline over 24h vs 14-day window."""
    import compresr_regression_guard as guard

    assert guard.REGRESSION_MULTIPLIER == 2.0
    assert guard.BASELINE_DAYS == 14
    assert guard.RECENT_HOURS == 24
    assert guard.KNOWN_CALL_SITES == ("self_heal", "self_improve")
