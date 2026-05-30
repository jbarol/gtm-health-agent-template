"""Tests for ``orchestrator.main`` scheduler registration.

Plan #35 task #38 — verify the Anthropic Admin API daily cost-pull cron is
registered when ``ANTHROPIC_ADMIN_KEY`` is set and skipped (with a logged
warning, no scheduler crash) when it is not. Also exercises the wrapper
function's exception-swallowing so a transient pull failure cannot kill the
scheduler thread.

All Slack, Anthropic, and DB side effects are mocked. No live calls.

Run:
    cd orchestrator && python3 -m pytest main_test.py -q
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import sys
from unittest.mock import MagicMock, patch


# Set the env vars ``config.py`` requires BEFORE first import — the worktree
# checkout has no ``.env``, so without these the ``import config`` chain inside
# ``main`` raises at module load. ``setdefault`` means a real ``.env`` still
# wins on dev laptops. Same defensive pattern as cost_collector_test.
for _k in (
    "ANTHROPIC_API_KEY",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "SLACK_CHANNEL_ID",
    "ENVIRONMENT_ID",
    "DREAM_AGENT_ID",
    "COORDINATOR_ID",
    "QUICK_AGENT_ID",
    "METHODOLOGY_STORE_ID",
    "HEALTH_STORE_ID",
):
    os.environ.setdefault(_k, "test-stub")


def _import_main_fresh():
    """Re-import ``main`` and its ``config`` dependency so env-var patches stick.

    ``main`` reads ``config.ANTHROPIC_ADMIN_KEY`` from the module attribute at
    registration time. Test sequence:
      1. Drop any cached ``config`` and ``main`` modules.
      2. Re-import — both see whatever env / monkeypatches the caller staged.

    ``anthropic.Anthropic`` is patched during import so the module-level
    ``memory_client`` constructor doesn't try to validate a stub key.
    """
    sys.modules.pop("config", None)
    sys.modules.pop("main", None)
    with patch("anthropic.Anthropic", MagicMock()):
        import main  # noqa: F401  (intentional side-effect: load fresh)

        return importlib.import_module("main")


# ──────────────────────────────────────────────────────────────────────────
# scheduled_pull_anthropic_costs — runtime safety
# ──────────────────────────────────────────────────────────────────────────


def test_scheduled_pull_anthropic_costs_success_logs_rows(caplog):
    """Happy path: wrapper invokes the collector and logs the row count."""
    main = _import_main_fresh()
    with (
        patch(
            "cost_collector.pull_anthropic_daily_costs", return_value=7
        ) as mocked_pull,
        patch.object(main, "send_notification") as mocked_notify,
    ):
        with caplog.at_level(logging.INFO, logger="orchestrator"):
            main.scheduled_pull_anthropic_costs()
    mocked_pull.assert_called_once_with(days_back=3)
    mocked_notify.assert_not_called()
    assert any(
        "Anthropic daily cost pull completed: 7 rows" in r.message
        for r in caplog.records
    ), "expected the completion line with the row count"


def test_scheduled_pull_anthropic_costs_swallows_exceptions(caplog):
    """Wrapper must catch + log + Slack-notify, NEVER re-raise into APScheduler."""
    main = _import_main_fresh()
    with (
        patch(
            "cost_collector.pull_anthropic_daily_costs",
            side_effect=RuntimeError("boom"),
        ),
        patch.object(main, "send_notification") as mocked_notify,
    ):
        with caplog.at_level(logging.ERROR, logger="orchestrator"):
            # The point of the test: this must not raise.
            main.scheduled_pull_anthropic_costs()
    assert any("Anthropic daily cost pull failed" in r.message for r in caplog.records)
    # Send a watch notice so the operator notices a stale reconciliation.
    mocked_notify.assert_called_once()
    args, _kwargs = mocked_notify.call_args
    assert args[0] == "watch"
    assert "Anthropic daily cost pull failed" in args[1]


# ──────────────────────────────────────────────────────────────────────────
# main() — cron registration gating on ANTHROPIC_ADMIN_KEY
# ──────────────────────────────────────────────────────────────────────────


class _FakeScheduler:
    """Stand-in for ``BackgroundScheduler`` that records ``add_job`` calls.

    Timing and thread behavior are out of scope — we only care that the right
    ``(id, name, cron)`` tuple gets registered (or not) based on env.
    """

    def __init__(self, *args, **kwargs):
        self.jobs: list[dict] = []
        self.timezone = kwargs.get("timezone")
        self.started = False

    def add_job(self, func, trigger=None, *, id=None, name=None, **kwargs):  # noqa: A002
        self.jobs.append(
            {
                "func": func,
                "trigger": trigger,
                "id": id,
                "name": name,
                "kwargs": kwargs,
            }
        )

    def add_listener(self, *args, **kwargs):  # pragma: no cover - trivial
        pass

    def start(self):
        self.started = True

    def shutdown(self, **kwargs):  # pragma: no cover - trivial
        pass


def _run_main_with_fakes(*, admin_key: str | None):
    """Run ``main.main()`` with the heavy side-effects stubbed out.

    Returns the ``_FakeScheduler`` instance so the caller can inspect the
    registered jobs.
    """
    main = _import_main_fresh()
    fake_sched = _FakeScheduler()

    # Patch ``ANTHROPIC_ADMIN_KEY`` directly on the imported config module —
    # main.py reads ``config.ANTHROPIC_ADMIN_KEY`` at registration time.
    main.config.ANTHROPIC_ADMIN_KEY = admin_key or ""

    with (
        patch.object(main, "BackgroundScheduler", return_value=fake_sched),
        patch.object(main, "set_question_handler"),
        patch.object(main, "set_feedback_handler"),
        patch.object(main, "start_socket_mode"),
        patch.object(main, "is_db_available", return_value=False),
        patch.object(main, "send_notification"),
        patch("signal.signal"),
    ):
        main.main()

    return fake_sched


def test_main_registers_cost_cron_when_admin_key_set(caplog):
    """ANTHROPIC_ADMIN_KEY set → 06:00 PT cron is registered."""
    with caplog.at_level(logging.INFO, logger="orchestrator"):
        sched = _run_main_with_fakes(admin_key="sk-ant-admin-XYZ")

    cost_jobs = [j for j in sched.jobs if j["id"] == "anthropic-cost-pull"]
    assert len(cost_jobs) == 1, (
        f"expected exactly one anthropic-cost-pull job, got {len(cost_jobs)}: "
        f"{[j['id'] for j in sched.jobs]}"
    )
    job = cost_jobs[0]
    assert job["name"] == "Anthropic Admin API Daily Cost Pull"

    # Trigger must be a 6am cron in the configured timezone. CronTrigger doesn't
    # expose the original spec cleanly, but its repr includes the hour/minute
    # fields, so we assert on that.
    trigger_repr = repr(job["trigger"])
    assert "hour='6'" in trigger_repr, (
        f"expected 6am hour in trigger, got: {trigger_repr}"
    )
    assert "minute='0'" in trigger_repr

    # The registration log line should mention 06:00 so an operator scanning
    # logs at startup sees the right schedule.
    assert any(
        "Anthropic Admin API daily cost pull at 06:00" in r.message
        for r in caplog.records
    )


def test_main_skips_cost_cron_when_admin_key_unset(caplog):
    """No ANTHROPIC_ADMIN_KEY → registration is skipped and a warning logged."""
    with caplog.at_level(logging.WARNING, logger="orchestrator"):
        sched = _run_main_with_fakes(admin_key="")

    cost_jobs = [j for j in sched.jobs if j["id"] == "anthropic-cost-pull"]
    assert cost_jobs == [], (
        f"expected no anthropic-cost-pull job, got: {[j['id'] for j in sched.jobs]}"
    )
    assert any(
        "ANTHROPIC_ADMIN_KEY unset" in r.message
        and "skipping Anthropic daily cost pull" in r.message
        for r in caplog.records
    ), "expected a warning about the skipped registration"


def test_main_still_registers_db_sync_when_admin_key_unset():
    """Skipping the cost cron must not affect db-sync. The user-facing daily
    Slack-posting crons (dream/self-improve/forecast/cost-reconcile/cost-digest)
    were retired 2026-05-14 — confirm they are NOT re-registered by accident."""
    sched = _run_main_with_fakes(admin_key="")
    ids = {j["id"] for j in sched.jobs}
    assert "db-sync" in ids, f"db-sync must still register, got: {ids}"
    retired = {"dream", "self-improve", "forecast", "cost-reconcile", "cost-digest"}
    assert retired.isdisjoint(ids), (
        f"retired daily Slack crons should not be registered, found: {retired & ids}"
    )


# ──────────────────────────────────────────────────────────────────────────
# Plan #36 task #53 — Batch flush + poll cron registration
# ──────────────────────────────────────────────────────────────────────────


def _run_main_with_batch_flag(*, batch_enabled: bool):
    """Run ``main.main()`` with ``BATCH_PROCESSING_ENABLED`` set explicitly.

    Mirrors ``_run_main_with_fakes`` but flips the batch flag instead of the
    admin key. Returns the ``_FakeScheduler`` so callers can inspect the
    registered jobs.
    """
    main = _import_main_fresh()
    fake_sched = _FakeScheduler()

    # Patch ``BATCH_PROCESSING_ENABLED`` directly on the imported config
    # module — main.py reads ``config.BATCH_PROCESSING_ENABLED`` at
    # registration time.
    main.config.BATCH_PROCESSING_ENABLED = batch_enabled
    # Keep the admin-key path quiet — that's a separate test surface.
    main.config.ANTHROPIC_ADMIN_KEY = ""

    with (
        patch.object(main, "BackgroundScheduler", return_value=fake_sched),
        patch.object(main, "set_question_handler"),
        patch.object(main, "set_feedback_handler"),
        patch.object(main, "start_socket_mode"),
        patch.object(main, "is_db_available", return_value=False),
        patch.object(main, "send_notification"),
        patch("signal.signal"),
    ):
        main.main()

    return fake_sched


def test_main_registers_batch_jobs_when_enabled(caplog):
    """BATCH_PROCESSING_ENABLED=true → both flush + poll cron jobs are registered."""
    with caplog.at_level(logging.INFO, logger="orchestrator"):
        sched = _run_main_with_batch_flag(batch_enabled=True)

    job_ids = {j["id"] for j in sched.jobs}
    assert "batch-flush" in job_ids, (
        f"expected batch-flush job to be registered, got: {job_ids}"
    )
    assert "batch-poll" in job_ids, (
        f"expected batch-poll job to be registered, got: {job_ids}"
    )

    flush_job = next(j for j in sched.jobs if j["id"] == "batch-flush")
    poll_job = next(j for j in sched.jobs if j["id"] == "batch-poll")

    # Flush is an hourly interval trigger; poll runs every 15 minutes. The
    # interval-trigger keyword args land in ``kwargs`` on _FakeScheduler.
    assert flush_job["trigger"] == "interval"
    assert flush_job["kwargs"].get("hours") == 1
    assert flush_job["kwargs"].get("max_instances") == 1
    assert flush_job["kwargs"].get("coalesce") is True

    assert poll_job["trigger"] == "interval"
    assert poll_job["kwargs"].get("minutes") == 15
    assert poll_job["kwargs"].get("max_instances") == 1
    assert poll_job["kwargs"].get("coalesce") is True

    # Operators scanning startup logs should see a single confirmation that
    # the batch hooks went in.
    assert any(
        "Batch processing hooks registered" in r.message for r in caplog.records
    ), "expected a single confirmation log line at startup"


def test_main_skips_batch_jobs_when_disabled(caplog):
    """BATCH_PROCESSING_ENABLED=false → batch jobs are NOT registered, one info line logged."""
    with caplog.at_level(logging.INFO, logger="orchestrator"):
        sched = _run_main_with_batch_flag(batch_enabled=False)

    job_ids = {j["id"] for j in sched.jobs}
    assert "batch-flush" not in job_ids, f"expected no batch-flush job, got: {job_ids}"
    assert "batch-poll" not in job_ids, f"expected no batch-poll job, got: {job_ids}"

    # A single info line at startup explains the absence so operators don't
    # waste time hunting for a "missing" job.
    matching = [
        r for r in caplog.records if "BATCH_PROCESSING_ENABLED=false" in r.message
    ]
    assert len(matching) == 1, (
        f"expected exactly one skip log line, got {len(matching)}: "
        f"{[r.message for r in matching]}"
    )


def test_main_skips_batch_jobs_when_unset_defaults_off(caplog):
    """No env var → defaults to False → batch jobs skipped (same as explicit false)."""
    # _run_main_with_batch_flag(batch_enabled=False) covers the explicit case;
    # this asserts the default config value (declared in config.py) is False.
    main = _import_main_fresh()
    assert main.config.BATCH_PROCESSING_ENABLED is False, (
        "BATCH_PROCESSING_ENABLED must default to False so dev environments "
        "don't accidentally route through the Batches API"
    )


# ──────────────────────────────────────────────────────────────────────────
# scheduled_batch_flush + scheduled_batch_poll — wrapper exception safety
# ──────────────────────────────────────────────────────────────────────────


def test_scheduled_batch_flush_success_logs_count(caplog):
    """Happy path: wrapper calls recover_orphan_batches and logs the count."""
    main = _import_main_fresh()
    with (
        patch("batch_runner.recover_orphan_batches", return_value=3) as mocked_recover,
        patch.object(main, "send_notification") as mocked_notify,
    ):
        with caplog.at_level(logging.INFO, logger="orchestrator"):
            main.scheduled_batch_flush()
    mocked_recover.assert_called_once_with()
    mocked_notify.assert_not_called()
    assert any(
        "Batch flush completed: 3 row(s) reconciled" in r.message
        for r in caplog.records
    ), "expected completion line with the recovered-row count"


def test_scheduled_batch_flush_swallows_exceptions(caplog):
    """Wrapper must catch + log + Slack-notify, NEVER re-raise into APScheduler."""
    main = _import_main_fresh()
    with (
        patch("batch_runner.recover_orphan_batches", side_effect=RuntimeError("boom")),
        patch.object(main, "send_notification") as mocked_notify,
    ):
        with caplog.at_level(logging.ERROR, logger="orchestrator"):
            # The point of the test: this must not raise.
            main.scheduled_batch_flush()
    assert any("Batch flush failed" in r.message for r in caplog.records)
    mocked_notify.assert_called_once()
    args, _kwargs = mocked_notify.call_args
    assert args[0] == "watch"
    assert "Batch flush failed" in args[1]


def test_scheduled_batch_poll_success_logs_completions(caplog):
    """Happy path: wrapper calls poll_pending_batches with the registry."""
    main = _import_main_fresh()
    fake_registry = {"self_heal": MagicMock(), "self_improve": MagicMock()}
    with (
        patch.object(main, "_batch_callback_registry", return_value=fake_registry),
        patch("batch_runner.poll_pending_batches", return_value=2) as mocked_poll,
        patch.object(main, "send_notification") as mocked_notify,
    ):
        with caplog.at_level(logging.INFO, logger="orchestrator"):
            main.scheduled_batch_poll()
    mocked_poll.assert_called_once_with(fake_registry)
    mocked_notify.assert_not_called()
    assert any(
        "Batch poll completed: 2 batch(es) dispatched" in r.message
        for r in caplog.records
    )


def test_scheduled_batch_poll_swallows_exceptions(caplog):
    """Wrapper must catch + log + Slack-notify, NEVER re-raise into APScheduler."""
    main = _import_main_fresh()
    with (
        patch.object(main, "_batch_callback_registry", return_value={}),
        patch("batch_runner.poll_pending_batches", side_effect=RuntimeError("boom")),
        patch.object(main, "send_notification") as mocked_notify,
    ):
        with caplog.at_level(logging.ERROR, logger="orchestrator"):
            main.scheduled_batch_poll()
    assert any("Batch poll failed" in r.message for r in caplog.records)
    mocked_notify.assert_called_once()
    args, _kwargs = mocked_notify.call_args
    assert args[0] == "watch"
    assert "Batch poll failed" in args[1]


def test_batch_callback_registry_wires_handlers():
    """Registry maps the call_site keys to the self_heal / self_improve completion handlers."""
    main = _import_main_fresh()
    # The registry resolution lazily imports self_heal + self_improve, so
    # patch the real handlers and verify they land under the expected keys.
    import self_heal
    import self_improve

    registry = main._batch_callback_registry()
    assert set(registry.keys()) == {"self_heal", "self_improve"}, (
        f"expected exactly two registry keys, got: {set(registry.keys())}"
    )
    assert registry["self_heal"] is self_heal._handle_batch_completion
    assert registry["self_improve"] is self_improve._handle_batch_completion


# ──────────────────────────────────────────────────────────────────────────
# _scheduler_watch_notice — Slack alert-fatigue dedup (Plan #52 PR-Y)
# ──────────────────────────────────────────────────────────────────────────


def test_scheduler_watch_notice_dedupes_within_cooldown():
    """Two rapid calls with the same key → send_notification fires once.

    Root cause covered: a transient DB hiccup used to fire a fresh Slack
    "Batch poll failed" watch notice on every 15-min tick. The dedup helper
    suppresses the second-and-later calls within the cooldown window.
    """
    main = _import_main_fresh()
    with patch.object(main, "send_notification") as mocked_notify:
        main._scheduler_watch_notice("k", "msg")
        main._scheduler_watch_notice("k", "msg")
    mocked_notify.assert_called_once_with("watch", "msg")


def test_scheduler_watch_notice_fires_after_cooldown(monkeypatch):
    """Calling again after the cooldown elapses → send_notification fires twice.

    monkeypatches ``time.monotonic`` to advance just past the cooldown
    threshold so we don't have to sleep an hour in CI.
    """
    main = _import_main_fresh()

    # Pin a synthetic clock so the first call records ``base`` and the second
    # call lands cooldown+1 seconds later — guaranteed to be on the far side
    # of the gate regardless of how slow the test machine is.
    import time as _time_mod

    base = _time_mod.monotonic()

    def _fake_monotonic_initial():
        return base

    def _fake_monotonic_after():
        return base + main._SCHEDULER_WATCH_COOLDOWN_SECONDS + 1

    with patch.object(main, "send_notification") as mocked_notify:
        monkeypatch.setattr("time.monotonic", _fake_monotonic_initial)
        main._scheduler_watch_notice("k", "msg")
        monkeypatch.setattr("time.monotonic", _fake_monotonic_after)
        main._scheduler_watch_notice("k", "msg")
    assert mocked_notify.call_count == 2


def test_scheduler_watch_notice_swallows_send_notification_exception(
    caplog, monkeypatch
):
    """If send_notification raises, the helper logs and does NOT propagate.

    The helper is called from inside the catch-all in the scheduler wrapper,
    so an exception escaping here would defeat the wrapper's contract of
    never crashing APScheduler.

    Forces a fresh ``_scheduler_watch_last_post`` so the cooldown gate cannot
    short-circuit before we reach the ``send_notification`` call — without
    this the test depends on import-order luck (key ``"k"`` may already have
    a recent timestamp from a sibling test that didn't re-import) and we
    would silently never exercise the exception-swallow path.
    """
    main = _import_main_fresh()
    monkeypatch.setattr(main, "_scheduler_watch_last_post", {})
    with patch.object(
        main, "send_notification", side_effect=RuntimeError("slack down")
    ):
        with caplog.at_level(logging.ERROR, logger="orchestrator"):
            # The point of the test: this must not raise.
            main._scheduler_watch_notice("k", "msg")
    assert any("Scheduler watch notice failed" in r.message for r in caplog.records)


def test_scheduler_watch_notice_does_not_dedup_after_failed_send(
    caplog, monkeypatch
):
    """If send_notification raises, the cooldown timestamp must NOT update.

    Codex P2 on PR #249: previously the helper wrote the cooldown timestamp
    BEFORE calling send_notification, so a single transient Slack failure
    (network blip, expired token, API error) would suppress every subsequent
    alert for the full 60-min cooldown even though no notice was delivered.
    Fix: only persist the timestamp after a successful send. Regression
    pinned here — two consecutive calls under a raising send_notification
    must BOTH attempt the send.
    """
    main = _import_main_fresh()
    monkeypatch.setattr(main, "_scheduler_watch_last_post", {})

    sent_bodies: list[str] = []

    def _raising_send(channel: str, body: str) -> None:
        sent_bodies.append(body)
        raise RuntimeError("transient slack failure")

    monkeypatch.setattr(main, "send_notification", _raising_send)

    with caplog.at_level(logging.ERROR, logger="orchestrator"):
        main._scheduler_watch_notice("k", "first")
        # send_notification was called and raised; cooldown should NOT have
        # been recorded, so the very next call must attempt send_notification
        # again instead of being suppressed by the dedup gate.
        assert "k" not in main._scheduler_watch_last_post
        main._scheduler_watch_notice("k", "second")

    assert sent_bodies == ["first", "second"]
    # The cooldown timestamp is still absent: the second send also failed.
    assert "k" not in main._scheduler_watch_last_post


# ──────────────────────────────────────────────────────────────────────────
# RUN_NIGHTLY_NOW fire-once-per-day guard
# ──────────────────────────────────────────────────────────────────────────
#
# Root cause covered: ``os.environ.pop("RUN_NIGHTLY_NOW", None)`` after the
# pipeline ran only cleared the Python process's view of the env var — the
# Railway env var stays set, so the next container restart re-fires the
# pipeline. Symptom on 2026-05-11: an unrelated prompt PR merge triggered a
# Railway auto-deploy at 16:00 PT, the container restarted, and the full
# nightly pipeline ran at 16:02 PT. The marker file is the persistent guard.


def _run_main_with_nightly_now(
    *,
    env_set: bool,
    marker_today_exists: bool = False,
    marker_other_date_exists: bool = False,
    pacific_today: str = "2026-05-11",
    tmp_marker_dir: str | None = None,
):
    """Run ``main.main()`` with the RUN_NIGHTLY_NOW env var staged.

    Pins the Pacific date so the marker filename is deterministic. Optionally
    pre-creates a marker (for today or for a different date) so the test can
    assert how the guard reacts to existing state.

    Returns ``(fake_scheduler, marker_path_for_today)``.
    """
    import tempfile

    if tmp_marker_dir is None:
        tmp_marker_dir = tempfile.mkdtemp(prefix="nightly_now_test_")

    marker_today = os.path.join(tmp_marker_dir, f"nightly_now_fired_{pacific_today}")
    marker_other = os.path.join(tmp_marker_dir, "nightly_now_fired_2026-05-10")

    # Pre-seed marker files as requested by the test scenario.
    if marker_today_exists:
        with open(marker_today, "w") as fh:
            fh.write("pre-existing")
    if marker_other_date_exists:
        with open(marker_other, "w") as fh:
            fh.write("pre-existing")

    main = _import_main_fresh()
    fake_sched = _FakeScheduler()
    main.config.ANTHROPIC_ADMIN_KEY = ""
    main.config.BATCH_PROCESSING_ENABLED = False
    main.config.TIMEZONE = "America/Los_Angeles"

    # Stable Pacific "today" — patch the datetime.now() that resolves to
    # the Pacific date inside main.py. We re-import the symbol main.py uses.
    from datetime import datetime as real_datetime

    class _FakeDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: D401, ARG003
            return real_datetime.fromisoformat(f"{pacific_today}T09:00:00+00:00")

    if env_set:
        os.environ["RUN_NIGHTLY_NOW"] = "1"
    else:
        os.environ.pop("RUN_NIGHTLY_NOW", None)

    # Redirect the marker path. main.py builds ``/tmp/nightly_now_fired_<date>``
    # directly — patch ``os.path.exists`` and ``open`` for that prefix only.
    real_open = open
    real_exists = os.path.exists

    def _fake_exists(path):  # noqa: D401
        if isinstance(path, str) and path.startswith("/tmp/nightly_now_fired_"):
            redirect = os.path.join(tmp_marker_dir, os.path.basename(path))
            return real_exists(redirect)
        return real_exists(path)

    def _fake_open(path, *args, **kwargs):
        if isinstance(path, str) and path.startswith("/tmp/nightly_now_fired_"):
            redirect = os.path.join(tmp_marker_dir, os.path.basename(path))
            return real_open(redirect, *args, **kwargs)
        return real_open(path, *args, **kwargs)

    try:
        with (
            patch.object(main, "BackgroundScheduler", return_value=fake_sched),
            patch.object(main, "set_question_handler"),
            patch.object(main, "set_feedback_handler"),
            patch.object(main, "start_socket_mode"),
            patch.object(main, "is_db_available", return_value=False),
            patch.object(main, "send_notification"),
            patch("signal.signal"),
            patch("os.path.exists", side_effect=_fake_exists),
            patch("builtins.open", side_effect=_fake_open),
            # Patch datetime so the marker date is deterministic. Both the
            # ``datetime`` symbol imported inside the ``if`` block and the
            # one inside the ``_run_and_clear`` closure resolve from the
            # ``datetime`` module — patching the class on the module covers
            # both.
            patch("datetime.datetime", _FakeDateTime),
        ):
            main.main()
    finally:
        os.environ.pop("RUN_NIGHTLY_NOW", None)

    return fake_sched, marker_today


def test_run_nightly_now_skips_when_marker_for_today_exists(caplog):
    """Marker file already exists for today's Pacific date → no job scheduled."""
    with caplog.at_level(logging.INFO, logger="orchestrator"):
        sched, marker = _run_main_with_nightly_now(
            env_set=True, marker_today_exists=True
        )

    nightly_jobs = [j for j in sched.jobs if j["id"] == "nightly-now"]
    assert nightly_jobs == [], (
        f"expected NO nightly-now job because marker {marker} exists, "
        f"got: {[j['id'] for j in sched.jobs]}"
    )
    assert any(
        "already fired today" in r.message and "skipping" in r.message
        for r in caplog.records
    ), "expected explicit 'already fired today — skipping' log line"


def test_run_nightly_now_schedules_and_writes_marker_when_absent(caplog, tmp_path):
    """No marker for today → job scheduled, AND _run_and_clear writes the marker first."""
    with caplog.at_level(logging.INFO, logger="orchestrator"):
        sched, marker = _run_main_with_nightly_now(
            env_set=True,
            marker_today_exists=False,
            tmp_marker_dir=str(tmp_path),
        )

    nightly_jobs = [j for j in sched.jobs if j["id"] == "nightly-now"]
    assert len(nightly_jobs) == 1, (
        f"expected the nightly-now job to be scheduled, got: "
        f"{[j['id'] for j in sched.jobs]}"
    )
    assert nightly_jobs[0]["trigger"] == "date"

    # Invoke the scheduled callable directly — the marker must be written
    # BEFORE the pipeline runs so a crash mid-run still blocks re-fire.
    import main as main_mod  # already imported by _run_main_with_nightly_now

    # Repatch the marker redirect for the manual invocation.
    real_open = open
    real_exists = os.path.exists

    def _fake_exists(path):
        if isinstance(path, str) and path.startswith("/tmp/nightly_now_fired_"):
            return real_exists(os.path.join(str(tmp_path), os.path.basename(path)))
        return real_exists(path)

    def _fake_open(path, *args, **kwargs):
        if isinstance(path, str) and path.startswith("/tmp/nightly_now_fired_"):
            return real_open(
                os.path.join(str(tmp_path), os.path.basename(path)),
                *args,
                **kwargs,
            )
        return real_open(path, *args, **kwargs)

    pipeline_calls = {"n": 0}

    def _fake_pipeline():
        # When the pipeline runs, the marker must ALREADY exist (written
        # before the pipeline is invoked). Capture that ordering invariant.
        pipeline_calls["n"] += 1
        assert real_exists(os.path.join(str(tmp_path), os.path.basename(marker))), (
            "marker must be written BEFORE the pipeline runs"
        )

    with (
        patch.object(main_mod, "run_full_nightly_pipeline", _fake_pipeline),
        patch("os.path.exists", side_effect=_fake_exists),
        patch("builtins.open", side_effect=_fake_open),
    ):
        nightly_jobs[0]["func"]()

    assert pipeline_calls["n"] == 1, "pipeline should have run exactly once"
    assert os.path.exists(os.path.join(str(tmp_path), os.path.basename(marker))), (
        "marker should persist after the run"
    )


def test_run_nightly_now_replaces_marker_from_different_date(caplog, tmp_path):
    """Marker exists for a prior date → job scheduled (this is a new day)."""
    with caplog.at_level(logging.INFO, logger="orchestrator"):
        sched, _marker = _run_main_with_nightly_now(
            env_set=True,
            marker_today_exists=False,
            marker_other_date_exists=True,
            tmp_marker_dir=str(tmp_path),
        )

    nightly_jobs = [j for j in sched.jobs if j["id"] == "nightly-now"]
    assert len(nightly_jobs) == 1, (
        f"prior-date marker must NOT block a new day's intentional run, "
        f"got: {[j['id'] for j in sched.jobs]}"
    )


def test_run_nightly_now_warning_fires_on_every_env_set_startup(caplog):
    """RUN_NIGHTLY_NOW set → loud warning logs, regardless of marker state."""
    # Case A: marker present (job is skipped) — warning still fires.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="orchestrator"):
        _run_main_with_nightly_now(env_set=True, marker_today_exists=True)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        "RUN_NIGHTLY_NOW is set" in r.message and "REMOVE FROM RAILWAY ENV" in r.message
        for r in warnings
    ), "expected loud REMOVE-FROM-RAILWAY warning even when marker blocks the schedule"

    # Case B: marker absent (job is scheduled) — warning still fires.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="orchestrator"):
        _run_main_with_nightly_now(env_set=True, marker_today_exists=False)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        "RUN_NIGHTLY_NOW is set" in r.message and "REMOVE FROM RAILWAY ENV" in r.message
        for r in warnings
    ), "expected loud REMOVE-FROM-RAILWAY warning on the scheduled path"


def test_run_nightly_now_silent_when_env_var_unset():
    """RUN_NIGHTLY_NOW not set → no warning, no job, no marker check."""
    sched, _ = _run_main_with_nightly_now(env_set=False)
    nightly_jobs = [j for j in sched.jobs if j["id"] == "nightly-now"]
    assert nightly_jobs == [], (
        f"unset env var must produce no nightly-now job, got: "
        f"{[j['id'] for j in sched.jobs]}"
    )


# ──────────────────────────────────────────────────────────────────────────
# Plan #33 F9 — Daily surface refresh cron registration + wrapper safety
# ──────────────────────────────────────────────────────────────────────────


def test_main_registers_surface_refresh_at_8am_pacific():
    """The surface_refresh_daily job must be registered at 08:00 PT,
    max_instances=1 + coalesce=True so a slow tick doesn't queue."""
    sched = _run_main_with_fakes(admin_key="")
    surface_jobs = [j for j in sched.jobs if j["id"] == "surface_refresh_daily"]
    assert len(surface_jobs) == 1, (
        f"expected exactly one surface_refresh_daily job, got: "
        f"{[j['id'] for j in sched.jobs]}"
    )
    job = surface_jobs[0]
    assert job["name"] == "Daily Surface Refresh"

    # CronTrigger(hour=8, minute=0) — repr should include those fields.
    trigger_repr = repr(job["trigger"])
    assert "hour='8'" in trigger_repr, (
        f"expected 8am hour in trigger, got: {trigger_repr}"
    )
    assert "minute='0'" in trigger_repr

    # Resilience knobs match the canary pattern: at most one run at a time,
    # collapse missed ticks rather than queue them.
    assert job["kwargs"].get("max_instances") == 1
    assert job["kwargs"].get("coalesce") is True


def test_main_surface_refresh_registered_alongside_other_jobs():
    """Skipping the cost cron (no ANTHROPIC_ADMIN_KEY) must NOT skip the
    surface refresh — the two are independent."""
    sched = _run_main_with_fakes(admin_key="")
    job_ids = {j["id"] for j in sched.jobs}
    assert "surface_refresh_daily" in job_ids
    # db-sync still registers; the user-facing daily Slack-posting crons
    # (retired 2026-05-14) do not.
    assert "db-sync" in job_ids
    retired = {"dream", "self-improve", "forecast", "cost-reconcile", "cost-digest"}
    assert retired.isdisjoint(job_ids), (
        f"retired daily Slack crons should not be registered, found: {retired & job_ids}"
    )


def test_scheduled_surface_refresh_walks_all_active_portcos(caplog):
    """Happy path: iterate active portcos and push each."""
    main = _import_main_fresh()
    fake_portcos = [
        {"key": "acme", "name": "Acme"},
        {"key": "acme", "name": "Acme"},
    ]
    fake_push = MagicMock()

    fake_pusher_mod = MagicMock()
    fake_pusher_mod.push_to_canvas = fake_push
    fake_registry_mod = MagicMock()
    fake_registry_mod.get_all_portcos.return_value = fake_portcos

    with (
        patch.dict(
            "sys.modules",
            {
                "surface_pusher": fake_pusher_mod,
                "portco_registry": fake_registry_mod,
            },
        ),
        patch.object(main, "send_notification") as mocked_notify,
    ):
        with caplog.at_level(logging.INFO, logger="orchestrator"):
            main.scheduled_surface_refresh()

    assert fake_push.call_count == 2
    assert fake_push.call_args_list[0].args == ("acme",)
    assert fake_push.call_args_list[1].args == ("acme",)
    mocked_notify.assert_not_called()
    assert any(
        "Daily surface refresh complete: 2/2 pushed" in r.message
        for r in caplog.records
    )


def test_scheduled_surface_refresh_isolates_per_portco_failures(caplog):
    """One portco's push failing must not block subsequent portcos."""
    main = _import_main_fresh()
    fake_portcos = [
        {"key": "acme", "name": "Acme"},
        {"key": "acme", "name": "Acme"},
        {"key": "third", "name": "Third"},
    ]
    fake_push = MagicMock(side_effect=[None, RuntimeError("canvas 500"), None])

    fake_pusher_mod = MagicMock()
    fake_pusher_mod.push_to_canvas = fake_push
    fake_registry_mod = MagicMock()
    fake_registry_mod.get_all_portcos.return_value = fake_portcos

    with (
        patch.dict(
            "sys.modules",
            {
                "surface_pusher": fake_pusher_mod,
                "portco_registry": fake_registry_mod,
            },
        ),
    ):
        with caplog.at_level(logging.INFO, logger="orchestrator"):
            # The wrapper MUST NOT raise.
            main.scheduled_surface_refresh()

    assert fake_push.call_count == 3
    assert any(
        "SURFACE_PUSH_FAILED" in r.message and "acme" in r.message
        for r in caplog.records
    )
    assert any(
        "Daily surface refresh complete: 2/3 pushed, 1 failed" in r.message
        for r in caplog.records
    )


def test_scheduled_surface_refresh_handles_missing_pusher_import(caplog):
    """F6 may not yet be on main — the wrapper logs and exits cleanly when
    the lazy ``from surface_pusher import push_to_canvas`` raises.

    We inject a builtin ``__import__`` shim that raises ``ImportError`` for
    ``surface_pusher`` so the test doesn't depend on whether F6 happens to
    be on disk in the worktree at test time.
    """
    main = _import_main_fresh()

    import builtins

    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "surface_pusher":
            raise ImportError("surface_pusher not yet on this branch")
        return real_import(name, globals, locals, fromlist, level)

    # The wrapper MUST NOT raise even when the import errors.
    with patch("builtins.__import__", side_effect=fake_import):
        with caplog.at_level(logging.ERROR, logger="orchestrator"):
            main.scheduled_surface_refresh()

    assert any(
        "SURFACE_PUSH_FAILED" in r.message and "import failed" in r.message
        for r in caplog.records
    )


# ──────────────────────────────────────────────────────────────────────────
# B10 — /health endpoint returns build_commit + active_versions + status
# ──────────────────────────────────────────────────────────────────────────


def test_health_endpoint_returns_expected_shape(monkeypatch, tmp_path):
    """GET /health returns the documented JSON shape.

    Verifies the payload assembly logic (``_build_health_payload``) without
    actually binding the HTTP server. The server itself is a stdlib
    HTTPServer on a daemon thread; the payload-assembly function is what
    matters for the deploy-verification contract.
    """
    monkeypatch.setenv("BUILD_COMMIT", "deadbeef1234")

    main = _import_main_fresh()

    # Point active_versions.json at a fixture file.
    fixture = tmp_path / "active_versions.json"
    import json as _json

    fixture.write_text(_json.dumps({"coordinator": "v42", "writing_agent": "v7"}))
    monkeypatch.setattr(main, "_ACTIVE_VERSIONS_PATH", fixture)

    payload = main._build_health_payload()
    assert payload["status"] == "ok"
    assert payload["build_commit"] == "deadbeef1234"
    assert payload["active_versions"] == {"coordinator": "v42", "writing_agent": "v7"}
    assert "deploy_started_at" in payload
    # ISO-8601 with offset suffix.
    assert "T" in payload["deploy_started_at"]


def test_health_payload_handles_missing_active_versions_file(monkeypatch, tmp_path):
    """No active_versions.json → empty dict, status still ok, no crash."""
    monkeypatch.setenv("BUILD_COMMIT", "abc123")

    main = _import_main_fresh()
    monkeypatch.setattr(main, "_ACTIVE_VERSIONS_PATH", tmp_path / "does_not_exist.json")

    payload = main._build_health_payload()
    assert payload["build_commit"] == "abc123"
    assert payload["active_versions"] == {}
    assert payload["status"] == "ok"


def test_health_payload_defaults_build_commit_to_unknown(monkeypatch, tmp_path):
    """BUILD_COMMIT unset → 'unknown'. Loud signal that the build arg wasn't wired."""
    monkeypatch.delenv("BUILD_COMMIT", raising=False)

    main = _import_main_fresh()
    monkeypatch.setattr(main, "_ACTIVE_VERSIONS_PATH", tmp_path / "does_not_exist.json")

    payload = main._build_health_payload()
    assert payload["build_commit"] == "unknown"


def test_health_http_server_serves_health_json(monkeypatch):
    """End-to-end: start the HTTP server, GET /health, parse the JSON."""
    import http.client
    import json as _json

    monkeypatch.setenv("BUILD_COMMIT", "abcd1234")
    # Use an ephemeral port so we don't collide with anything else.
    monkeypatch.setenv("PORT", "0")

    main = _import_main_fresh()
    server = main._start_health_server()
    if server is None:
        # Port allocation failed for some reason — skip rather than fail.
        import pytest

        pytest.skip("could not bind ephemeral port")

    try:
        port = server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", "/health")
        resp = conn.getresponse()
        assert resp.status == 200
        body = _json.loads(resp.read().decode("utf-8"))
        assert body["status"] == "ok"
        assert body["build_commit"] == "abcd1234"
        assert "active_versions" in body
        assert "deploy_started_at" in body
        conn.close()
    finally:
        server.shutdown()
        server.server_close()


def test_health_http_server_returns_404_for_other_paths(monkeypatch):
    """Anything other than /health and /healthz returns 404."""
    import http.client

    monkeypatch.setenv("PORT", "0")
    main = _import_main_fresh()
    server = main._start_health_server()
    if server is None:
        import pytest

        pytest.skip("could not bind ephemeral port")

    try:
        port = server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", "/")
        resp = conn.getresponse()
        assert resp.status == 404
        conn.close()
    finally:
        server.shutdown()
        server.server_close()


# ──────────────────────────────────────────────────────────────────────────
# Outer-exception terminalize gap (12th lifecycle path, 2026-05-14)
# ──────────────────────────────────────────────────────────────────────────
#
# When ``run_adhoc_investigation`` raises BEFORE it can mint the
# investigations row + call terminalize_lifecycle itself (e.g. Anthropic
# returns HTTP 500 three times in a row on POST /v1/sessions, the SDK
# gives up, the inner runner raises), the user's message must still flip
# from ⏰ → ❌. The outer ``except`` in ``_run_investigation`` is the
# last-mile safety net. Live repro 2026-05-14 20:17 PT.


def test_run_investigation_terminalizes_on_inner_exception():
    """Inner runner raises → outer except calls terminalize_lifecycle with
    inv_id=None so the reaction emoji flips even when no DB row exists."""
    main = _import_main_fresh()

    captured: dict = {}

    def fake_terminalize(state, *, event_ts, channel_id, inv_id, error_message=None):
        captured["state"] = state
        captured["event_ts"] = event_ts
        captured["channel_id"] = channel_id
        captured["inv_id"] = inv_id
        captured["error_message"] = error_message
        return state

    # Build a fake ``lifecycle`` module so the lazy import inside
    # ``_run_investigation`` resolves to our recorder.
    import sys
    import types

    fake_lifecycle = types.ModuleType("lifecycle")

    class _FakeState:
        TERMINAL_FAILURE = "terminal_failure"

    fake_lifecycle.DeliveryState = _FakeState  # type: ignore[attr-defined]
    fake_lifecycle.terminalize_lifecycle = fake_terminalize  # type: ignore[attr-defined]

    with (
        patch.dict(sys.modules, {"lifecycle": fake_lifecycle}),
        patch.object(
            main,
            "run_adhoc_investigation",
            side_effect=RuntimeError("anthropic 500 outage"),
        ),
    ):
        main._run_investigation(
            question="What does FATI mean in Acme?",
            user_id="U_TEST",
            thread_ts="1747252608.123456",
            channel_id="C0TEST",
            event_ts="1747252608.123456",
        )

    assert captured["state"] == _FakeState.TERMINAL_FAILURE, (
        f"terminalize_lifecycle was not called with TERMINAL_FAILURE; "
        f"captured={captured}"
    )
    assert captured["event_ts"] == "1747252608.123456"
    assert captured["channel_id"] == "C0TEST"
    assert captured["inv_id"] is None, (
        "inv_id must be None — the inner runner crashed before minting the DB row"
    )
    # The error_message should carry the original exception type + message
    # so the operator has something to grep for.
    assert "RuntimeError" in (captured["error_message"] or "")
    assert "anthropic 500" in (captured["error_message"] or "")


def test_run_investigation_swallows_terminalize_failure(caplog):
    """Even if terminalize_lifecycle ITSELF raises (DB down, Slack rate
    limit), _run_investigation must not propagate — the user's reaction
    emoji invariant is broken loudly via logs, not by killing the thread
    pool worker."""
    import logging as _logging

    main = _import_main_fresh()

    import sys
    import types

    fake_lifecycle = types.ModuleType("lifecycle")

    class _FakeState:
        TERMINAL_FAILURE = "terminal_failure"

    def boom(*args, **kwargs):
        raise RuntimeError("slack 429")

    fake_lifecycle.DeliveryState = _FakeState  # type: ignore[attr-defined]
    fake_lifecycle.terminalize_lifecycle = boom  # type: ignore[attr-defined]

    with (
        patch.dict(sys.modules, {"lifecycle": fake_lifecycle}),
        patch.object(
            main,
            "run_adhoc_investigation",
            side_effect=RuntimeError("anthropic 500"),
        ),
    ):
        # MUST NOT raise.
        with caplog.at_level(_logging.ERROR, logger="orchestrator"):
            main._run_investigation(
                question="x",
                user_id="U",
                thread_ts="1.0",
                channel_id="C",
                event_ts="1.0",
            )

    assert any(
        "Outer-exception terminalize_lifecycle failed" in r.message
        for r in caplog.records
    ), "expected the loud 'invariant broken' log line"


def test_run_investigation_does_not_terminalize_on_happy_path():
    """When run_adhoc_investigation completes normally, the outer wrapper
    does NOT call terminalize_lifecycle — the inner runner already handled
    terminalization, and a second call would log an idempotency warning."""
    main = _import_main_fresh()

    import sys
    import types

    fake_lifecycle = types.ModuleType("lifecycle")
    fake_terminalize = MagicMock(name="terminalize_lifecycle")

    class _FakeState:
        TERMINAL_FAILURE = "terminal_failure"

    fake_lifecycle.DeliveryState = _FakeState  # type: ignore[attr-defined]
    fake_lifecycle.terminalize_lifecycle = fake_terminalize  # type: ignore[attr-defined]

    with (
        patch.dict(sys.modules, {"lifecycle": fake_lifecycle}),
        patch.object(main, "run_adhoc_investigation", return_value=None),
    ):
        main._run_investigation(
            question="x",
            user_id="U",
            thread_ts="1.0",
            channel_id="C",
            event_ts="1.0",
        )

    fake_terminalize.assert_not_called()


# ──────────────────────────────────────────────────────────────────────────
# Plan P2 — _preprocess_prompt observability + retry (sub3 incident 2026-05-19)
# ──────────────────────────────────────────────────────────────────────────
#
# Root cause covered: ``_preprocess_prompt`` had 5 return-None paths and 4 log
# lines — the silent ``if not text_parts: return None`` branch left no signal
# at all. Transient SDK errors had no retry, so a one-shot blip produced a
# boilerplate-fallback Slack ack. The two consecutive 2026-05-19 incidents
# (10:28 PM + 10:57 PM PT) demonstrated the regression in production.
#
# These tests pin the three behavioral contracts that fix it: (a) retry once
# on transient failures, (b) do NOT retry on deterministic 400 / JSON drift,
# (c) record a ``messages_api_calls`` row on every invocation so the
# forensic signal lands even when the function returns None.


class _FakePEEventStream:
    """Stand-in for ``client.beta.sessions.events.stream(session_id=...)``.

    Returns ``events`` in order on iteration. Mirrors the real SDK shape:
    a context manager whose ``__enter__`` returns the iterable.
    """

    def __init__(self, events):
        self._events = events

    def __enter__(self):
        return iter(self._events)

    def __exit__(self, exc_type, exc, tb):
        return False


def _pe_event(event_type, **kwargs):
    """Build a fake PE session event with the same attribute shape the real
    SDK uses (event.type, event.content, …)."""
    from types import SimpleNamespace

    return SimpleNamespace(type=event_type, **kwargs)


def _pe_agent_message(text):
    """Construct an ``agent.message`` event whose content block carries
    ``.text``. Mirrors ``getattr(block, 'text', None)`` semantics from the
    real SDK."""
    from types import SimpleNamespace

    return _pe_event("agent.message", content=[SimpleNamespace(text=text)])


def _pe_idle():
    """``session.status_idle`` event — the canonical "stream ended cleanly"
    signal that ``_preprocess_prompt`` listens for."""
    return _pe_event("session.status_idle")


def _build_pe_client_mock(*, message_text=None, session_id="sesn_EXAMPLE_pe"):
    """Build a MagicMock that quacks like ``anthropic.Anthropic(...)`` enough
    for ``_preprocess_prompt`` to walk the happy path.

    Single emitted ``agent.message`` event with ``message_text``, followed
    by ``session.status_idle``. Set ``message_text=None`` to simulate the
    silent ``empty_text_parts`` branch (stream ends with no text events).
    """
    from types import SimpleNamespace

    client = MagicMock()
    fake_session = SimpleNamespace(id=session_id)
    client.beta.sessions.create.return_value = fake_session
    events = []
    if message_text is not None:
        events.append(_pe_agent_message(message_text))
    events.append(_pe_idle())
    client.beta.sessions.events.stream.return_value = _FakePEEventStream(events)
    client.beta.sessions.events.send = MagicMock()
    # ``sessions.retrieve(...).usage`` is what ``_preprocess_prompt`` queries
    # for the cost ledger. Default to a tiny usage object so the cost
    # estimator runs cleanly.
    usage = SimpleNamespace(
        input_tokens=100,
        output_tokens=50,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    client.beta.sessions.retrieve.return_value = SimpleNamespace(
        id=session_id, usage=usage
    )
    client.beta.sessions.archive = MagicMock()
    return client


def _pe_happy_json():
    """JSON payload shaped exactly like the Prompt Engineer's contract —
    every required key present, schema-valid."""
    return json.dumps(
        {
            "improved_prompt": "Improved version of the question",
            "summary": "Test summary",
            "plan_steps": ["Step 1", "Step 2"],
            "expected_output": "Test output",
            "risk_flags": [],
            "response_shape": "briefing",
        }
    )


def test_preprocess_retries_on_apiconnection(caplog):
    """First PE attempt raises ``APIConnectionError`` → retry succeeds.

    Asserts:
      * the function returns the parsed result dict (success)
      * exactly 2 ``sessions.create`` calls were made (original + retry)
      * the retry log line ``[PE_RETRY:transient]`` fired with attempt=1
    """
    main = _import_main_fresh()
    main.config.PROMPT_ENGINEER_ID = "agent_pe_test"

    # Build a working client for the retry — first call raises, second succeeds.
    good_client = _build_pe_client_mock(message_text=_pe_happy_json())

    import anthropic
    import httpx

    transient_exc = anthropic.APIConnectionError(
        message="connection reset",
        request=httpx.Request("POST", "https://api.anthropic.com/v1/sessions"),
    )

    # First call to sessions.create raises; second returns the real session.
    good_client.beta.sessions.create.side_effect = [
        transient_exc,
        good_client.beta.sessions.create.return_value,
    ]

    with (
        patch.object(main.anthropic, "Anthropic", return_value=good_client),
        patch.object(main.time, "sleep") as mock_sleep,
        patch("cost_collector.track_prompt_engineer_call") as mock_track,
    ):
        with caplog.at_level(logging.WARNING, logger="orchestrator"):
            result = main._preprocess_prompt("a real user question", portco_key="acme")

    assert result is not None, "retry should have produced a parsed dict"
    assert result["improved_prompt"] == "Improved version of the question"
    assert good_client.beta.sessions.create.call_count == 2, (
        f"expected exactly 2 sessions.create calls (original + 1 retry), got "
        f"{good_client.beta.sessions.create.call_count}"
    )
    # Backoff sleep ran exactly once with the documented 1.5s value.
    mock_sleep.assert_called_once_with(1.5)
    # The retry log line names the attempt counter so an operator can grep.
    assert any(
        "[PE_RETRY:transient]" in r.message and "attempt 1/2" in r.message
        for r in caplog.records
    ), "expected the retry log line with attempt counter"
    # The cost-ledger hook fires once on the successful outcome.
    mock_track.assert_called_once()
    assert mock_track.call_args.kwargs["outcome"] == "ok"


def test_preprocess_no_retry_on_badrequest(caplog):
    """``BadRequestError`` (deterministic 400) → no retry, returns None.

    Asserts:
      * the function returns ``None``
      * exactly 1 ``sessions.create`` call was made (NO retry)
      * the structured log line ``[PE_RETURN_NONE:session_create_failed]``
        fired
      * ``time.sleep`` was NOT called (no backoff because no retry)
    """
    main = _import_main_fresh()
    main.config.PROMPT_ENGINEER_ID = "agent_pe_test"

    pe_client = _build_pe_client_mock(message_text=_pe_happy_json())

    import anthropic
    import httpx

    bad_request = anthropic.BadRequestError(
        message="invalid agent id",
        response=httpx.Response(
            status_code=400,
            request=httpx.Request("POST", "https://api.anthropic.com/v1/sessions"),
        ),
        body={"error": {"message": "invalid agent id"}},
    )
    pe_client.beta.sessions.create.side_effect = bad_request

    with (
        patch.object(main.anthropic, "Anthropic", return_value=pe_client),
        patch.object(main.time, "sleep") as mock_sleep,
        patch("cost_collector.track_prompt_engineer_call") as mock_track,
    ):
        with caplog.at_level(logging.WARNING, logger="orchestrator"):
            result = main._preprocess_prompt("question", portco_key="acme")

    assert result is None, "BadRequestError should produce None without retry"
    assert pe_client.beta.sessions.create.call_count == 1, (
        f"BadRequestError is deterministic — must NOT retry; got "
        f"{pe_client.beta.sessions.create.call_count} attempts"
    )
    mock_sleep.assert_not_called()
    assert any(
        "[PE_RETURN_NONE:session_create_failed]" in r.message for r in caplog.records
    ), "expected the session_create_failed log line"
    # Cost ledger still fires — that's the forensic value.
    mock_track.assert_called_once()
    assert mock_track.call_args.kwargs["outcome"] == "session_create_failed"


def test_preprocess_records_messages_api_call_on_success():
    """Every successful invocation persists ONE row in ``messages_api_calls``
    via ``track_prompt_engineer_call``.

    Asserts the hook fires exactly once and is called with ``outcome="ok"``.
    """
    main = _import_main_fresh()
    main.config.PROMPT_ENGINEER_ID = "agent_pe_test"

    pe_client = _build_pe_client_mock(message_text=_pe_happy_json())

    with (
        patch.object(main.anthropic, "Anthropic", return_value=pe_client),
        patch("cost_collector.track_prompt_engineer_call") as mock_track,
    ):
        result = main._preprocess_prompt("question", portco_key="acme")

    assert result is not None
    mock_track.assert_called_once()
    kwargs = mock_track.call_args.kwargs
    assert kwargs["outcome"] == "ok"
    assert kwargs["model"] == "claude-sonnet-4-6"
    assert kwargs["portco_key"] == "acme"
    assert kwargs["elapsed_s"] >= 0


def test_preprocess_records_messages_api_call_on_failure():
    """Same hook fires when the function returns None — that's the forensic
    value the sub3 incident exposed. Use the ``empty_text_parts`` path to
    exercise the most common silent-failure mode."""
    main = _import_main_fresh()
    main.config.PROMPT_ENGINEER_ID = "agent_pe_test"

    # Stream ends with no agent.message events → ``empty_text_parts`` branch.
    pe_client = _build_pe_client_mock(message_text=None)

    with (
        patch.object(main.anthropic, "Anthropic", return_value=pe_client),
        patch("cost_collector.track_prompt_engineer_call") as mock_track,
    ):
        result = main._preprocess_prompt("question", portco_key="acme")

    assert result is None
    mock_track.assert_called_once()
    assert mock_track.call_args.kwargs["outcome"] == "empty_text_parts"


def test_preprocess_logs_empty_text_parts(caplog):
    """The silent ``if not text_parts: return None`` branch now emits the
    structured ``[PE_RETURN_NONE:empty_text_parts]`` log line with the
    session_id and elapsed time — the sub3 incident exposed that this
    path had no log signal at all before P2."""
    main = _import_main_fresh()
    main.config.PROMPT_ENGINEER_ID = "agent_pe_test"

    pe_client = _build_pe_client_mock(
        message_text=None, session_id="sesn_EXAMPLE_test"
    )

    with (
        patch.object(main.anthropic, "Anthropic", return_value=pe_client),
        patch("cost_collector.track_prompt_engineer_call"),
    ):
        with caplog.at_level(logging.WARNING, logger="orchestrator"):
            result = main._preprocess_prompt("question", portco_key="acme")

    assert result is None
    matching = [
        r for r in caplog.records
        if "[PE_RETURN_NONE:empty_text_parts]" in r.message
    ]
    assert len(matching) == 1, (
        f"expected exactly one empty_text_parts log line, got "
        f"{len(matching)}: {[r.message for r in matching]}"
    )
    # The log line must include the session_id and elapsed time so the
    # operator can correlate with Anthropic Console events.
    assert "sesn_EXAMPLE_test" in matching[0].message
    assert "elapsed=" in matching[0].message


def test_preprocess_logs_json_parse_failed(caplog):
    """Non-JSON model output → ``[PE_RETURN_NONE:json_parse_failed]`` log line
    with the raw[:200] snippet so the operator can see what the model
    actually produced."""
    main = _import_main_fresh()
    main.config.PROMPT_ENGINEER_ID = "agent_pe_test"

    # Model returns prose instead of JSON.
    bad_text = "Sorry, I can't comply with that format — here's a paragraph."
    pe_client = _build_pe_client_mock(message_text=bad_text)

    with (
        patch.object(main.anthropic, "Anthropic", return_value=pe_client),
        patch("cost_collector.track_prompt_engineer_call") as mock_track,
        patch.object(main.time, "sleep") as mock_sleep,
    ):
        with caplog.at_level(logging.WARNING, logger="orchestrator"):
            result = main._preprocess_prompt("question", portco_key="acme")

    assert result is None
    mock_sleep.assert_not_called(), (
        "JSON drift is deterministic — must NOT retry"
    )
    matching = [
        r for r in caplog.records
        if "[PE_RETURN_NONE:json_parse_failed]" in r.message
    ]
    assert len(matching) == 1
    assert bad_text[:50] in matching[0].message, (
        "log line must echo the raw[:200] snippet so operator can debug"
    )
    # Cost ledger row still lands.
    mock_track.assert_called_once()
    assert mock_track.call_args.kwargs["outcome"] == "json_parse_failed"


def test_preprocess_logs_invalid_schema(caplog):
    """Valid JSON but missing required ``improved_prompt`` key →
    ``[PE_RETURN_NONE:invalid_schema]`` log line with the keys list."""
    main = _import_main_fresh()
    main.config.PROMPT_ENGINEER_ID = "agent_pe_test"

    # Valid JSON, but missing the required `improved_prompt` key.
    bad_json = json.dumps({"summary": "no improved prompt here"})
    pe_client = _build_pe_client_mock(message_text=bad_json)

    with (
        patch.object(main.anthropic, "Anthropic", return_value=pe_client),
        patch("cost_collector.track_prompt_engineer_call") as mock_track,
    ):
        with caplog.at_level(logging.WARNING, logger="orchestrator"):
            result = main._preprocess_prompt("question", portco_key="acme")

    assert result is None
    matching = [
        r for r in caplog.records
        if "[PE_RETURN_NONE:invalid_schema]" in r.message
    ]
    assert len(matching) == 1
    mock_track.assert_called_once()
    assert mock_track.call_args.kwargs["outcome"] == "invalid_schema"


def test_preprocess_logs_agent_unconfigured(caplog):
    """No ``PROMPT_ENGINEER_ID`` → ``[PE_RETURN_NONE:agent_unconfigured]`` log
    line plus the cost-ledger hook STILL fires (with usage=None and
    elapsed=0) so the rate of mis-configured deploys is observable."""
    main = _import_main_fresh()
    main.config.PROMPT_ENGINEER_ID = ""

    with (
        patch("cost_collector.track_prompt_engineer_call") as mock_track,
    ):
        with caplog.at_level(logging.WARNING, logger="orchestrator"):
            result = main._preprocess_prompt("question", portco_key="acme")

    assert result is None
    assert any(
        "[PE_RETURN_NONE:agent_unconfigured]" in r.message for r in caplog.records
    )
    mock_track.assert_called_once()
    kwargs = mock_track.call_args.kwargs
    assert kwargs["outcome"] == "agent_unconfigured"
    assert kwargs["usage"] is None
    assert kwargs["elapsed_s"] == 0.0


def test_preprocess_persisted_transient_failure_returns_none(caplog):
    """Both retry attempts raise ``InternalServerError`` → return None with
    the ``[PE_RETURN_NONE:session_error]`` log line. Confirms the retry
    cap is respected and the final transient is logged + tracked."""
    main = _import_main_fresh()
    main.config.PROMPT_ENGINEER_ID = "agent_pe_test"

    pe_client = _build_pe_client_mock(message_text=_pe_happy_json())

    import anthropic
    import httpx

    transient_exc = anthropic.InternalServerError(
        message="upstream timeout",
        response=httpx.Response(
            status_code=500,
            request=httpx.Request(
                "POST", "https://api.anthropic.com/v1/sessions"
            ),
        ),
        body={"error": {"message": "upstream timeout"}},
    )

    # Both calls raise → no recovery.
    pe_client.beta.sessions.create.side_effect = [transient_exc, transient_exc]

    with (
        patch.object(main.anthropic, "Anthropic", return_value=pe_client),
        patch.object(main.time, "sleep") as mock_sleep,
        patch("cost_collector.track_prompt_engineer_call") as mock_track,
    ):
        with caplog.at_level(logging.WARNING, logger="orchestrator"):
            result = main._preprocess_prompt("question", portco_key="acme")

    assert result is None
    assert pe_client.beta.sessions.create.call_count == 2, (
        "expected exactly 2 attempts (original + 1 retry) before giving up"
    )
    mock_sleep.assert_called_once_with(1.5)
    assert any(
        "[PE_RETURN_NONE:session_error]" in r.message for r in caplog.records
    )
    mock_track.assert_called_once()
    assert mock_track.call_args.kwargs["outcome"] == "session_error"
