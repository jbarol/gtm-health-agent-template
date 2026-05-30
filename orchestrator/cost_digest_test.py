"""Tests for the daily cost DM digest — Plan #35 task #41.

Covers:
  * Message body shape (with/without drift watch line, with/without admin list,
    with/without DB, with no spend).
  * Drift line rendering (n/a / within tolerance / outside / critical).
  * Admin-ID resolution preference: config.ADMIN_USER_IDS > config.SLACK_ADMIN_USER_ID
    > portco_registry.get_admin_user_ids().
  * Send loop: per-user DM, partial failure does not block other users,
    no-admin shortcut returns ``skipped_reason='no_admin_users'``.
  * Cron-wrapper exception swallowing in ``main.scheduled_cost_digest``.

DB and Slack side-effects are mocked. No live calls.

Run:
    cd orchestrator && python3 -m pytest cost_digest_test.py -q
"""

from __future__ import annotations

import importlib
import logging
import os
import sys
from datetime import date
from unittest.mock import MagicMock, patch


# Same defensive .env stubbing pattern as cost_reconcile_test / main_test —
# keep imports clean when the worktree has no .env file.
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

sys.modules.pop("config", None)
sys.modules.pop("cost_collector", None)
sys.modules.pop("cost_digest", None)


# ──────────────────────────────────────────────────────────────────────────
# build_digest_message — pure-function shape tests
# ──────────────────────────────────────────────────────────────────────────


def _recon(
    *,
    local: float = 0.0,
    anthropic: float = 0.0,
    drift_pct: float | None = None,
) -> dict:
    """Build a recon dict shaped like compute_reconciliation's return value."""
    drift_usd = (anthropic - local) if drift_pct is None else local * 0
    return {
        "date": "2026-05-10",
        "local_total_usd": local,
        "anthropic_total_usd": anthropic,
        "drift_usd": drift_usd,
        "drift_pct": drift_pct,
    }


def test_build_message_no_watch_line_when_drift_within_tolerance():
    """|drift_pct| <= 10% → no ``:warning: Watch`` banner at top."""
    import cost_digest

    msg = cost_digest.build_digest_message(
        date(2026, 5, 10),
        total=4.22,
        portco_rows=[
            {"portco": "acme", "cost_usd": 3.12, "sessions": 12},
            {"portco": "(messages-api)", "cost_usd": 1.10, "sessions": 4},
        ],
        trigger_rows=[
            {"trigger": "cron", "cost_usd": 2.40, "sessions": 4},
            {"trigger": "slack_mention", "cost_usd": 1.18, "sessions": 7},
            {"trigger": "messages-api", "cost_usd": 0.64, "sessions": 4},
        ],
        cache_pct=71.2,
        recon=_recon(local=4.22, anthropic=4.31, drift_pct=0.021),
        top_sessions=[
            {
                "cost_usd": 0.84,
                "trigger": "slack_mention",
                "portco": "acme",
                "session_id": "sesn_EXAMPLE",
                "thread_ts": "T0123",
            }
        ],
    )

    assert ":warning: Watch" not in msg
    assert "*Cost — 2026-05-10*" in msg
    assert "Total: $4.22" in msg
    # By-task inline summary embeds in the Total line.
    assert "$2.40 cron" in msg
    assert "$1.18 slack_mention" in msg
    # By-portco split present.
    assert "By portco: acme $3.12 · (messages-api) $1.10" in msg
    # Cache rate present with verdict.
    assert "Cache: 71% hit-rate (good)" in msg
    # Drift line present with within-tolerance label.
    assert "Drift vs Anthropic billing: +2.1% (within tolerance)" in msg
    # Top sessions block.
    assert "*Top sessions:*" in msg
    assert "$0.84  slack_mention  acme  (thread T0123)" in msg


def test_build_message_with_watch_line_when_drift_exceeds_10pct():
    """|drift_pct| > 10% → message starts with the watch line."""
    import cost_digest

    msg = cost_digest.build_digest_message(
        date(2026, 5, 10),
        total=4.22,
        portco_rows=[{"portco": "acme", "cost_usd": 4.22, "sessions": 9}],
        trigger_rows=[{"trigger": "cron", "cost_usd": 4.22, "sessions": 9}],
        cache_pct=55.0,
        recon=_recon(local=4.22, anthropic=5.00, drift_pct=0.156),
        top_sessions=[],
    )

    first_line = msg.split("\n", 1)[0]
    assert first_line.startswith(":warning: Watch"), (
        f"expected watch banner at top, got: {first_line!r}"
    )
    assert "under-estimated" in first_line
    assert "15.6%" in first_line
    assert "outside tolerance" in msg


def test_build_message_over_estimated_direction():
    """Negative drift → 'over-estimated' direction word in the watch line."""
    import cost_digest

    msg = cost_digest.build_digest_message(
        date(2026, 5, 10),
        total=10.00,
        portco_rows=[{"portco": "acme", "cost_usd": 10.00, "sessions": 1}],
        trigger_rows=[{"trigger": "cron", "cost_usd": 10.00, "sessions": 1}],
        cache_pct=80.0,
        recon=_recon(local=10.00, anthropic=8.00, drift_pct=-0.25),
        top_sessions=[],
    )
    first_line = msg.split("\n", 1)[0]
    assert ":warning: Watch" in first_line
    assert "over-estimated" in first_line


def test_build_message_drift_na_when_anthropic_zero():
    """recon.drift_pct=None → 'Drift: n/a' line; no watch banner."""
    import cost_digest

    msg = cost_digest.build_digest_message(
        date(2026, 5, 10),
        total=1.50,
        portco_rows=[{"portco": "acme", "cost_usd": 1.50, "sessions": 2}],
        trigger_rows=[{"trigger": "cron", "cost_usd": 1.50, "sessions": 2}],
        cache_pct=42.0,
        recon=_recon(local=1.50, anthropic=0.0, drift_pct=None),
        top_sessions=[],
    )
    assert ":warning: Watch" not in msg
    assert "Drift vs Anthropic billing: n/a" in msg
    assert "Anthropic billing not yet available" in msg


def test_build_message_no_spend_renders_friendly_lines():
    """Empty rollups still produce a readable digest (placeholder lines)."""
    import cost_digest

    msg = cost_digest.build_digest_message(
        date(2026, 5, 10),
        total=0.0,
        portco_rows=[],
        trigger_rows=[],
        cache_pct=None,
        recon=_recon(local=0.0, anthropic=0.0, drift_pct=None),
        top_sessions=[],
    )
    assert "Total: $0.00" in msg
    assert "By portco: _no spend recorded_" in msg
    assert "Cache: n/a (no sessions)" in msg
    assert "no spend recorded" in msg  # the drift line variant for zero spend


def test_build_message_top_sessions_uses_session_id_when_no_thread():
    """Cron-triggered sessions have no thread_ts → fall back to session_id."""
    import cost_digest

    msg = cost_digest.build_digest_message(
        date(2026, 5, 10),
        total=0.61,
        portco_rows=[{"portco": "system", "cost_usd": 0.61, "sessions": 1}],
        trigger_rows=[{"trigger": "cron", "cost_usd": 0.61, "sessions": 1}],
        cache_pct=60.0,
        recon=_recon(local=0.61, anthropic=0.61, drift_pct=0.0),
        top_sessions=[
            {
                "cost_usd": 0.61,
                "trigger": "cron",
                "portco": "system",
                "session_id": "sesn_EXAMPLE",
                "thread_ts": None,
            }
        ],
    )
    assert "session sesn_EXAMPLE" in msg
    assert "thread None" not in msg


def test_build_message_cache_low_verdict():
    """< 30% cache hit-rate is labelled 'low' so an operator notices."""
    import cost_digest

    msg = cost_digest.build_digest_message(
        date(2026, 5, 10),
        total=1.00,
        portco_rows=[{"portco": "acme", "cost_usd": 1.00, "sessions": 1}],
        trigger_rows=[{"trigger": "cron", "cost_usd": 1.00, "sessions": 1}],
        cache_pct=15.0,
        recon=_recon(local=1.00, anthropic=1.00, drift_pct=0.0),
        top_sessions=[],
    )
    assert "Cache: 15% hit-rate (low)" in msg


# ──────────────────────────────────────────────────────────────────────────
# _drift_line — branch coverage
# ──────────────────────────────────────────────────────────────────────────


def test_drift_line_critical_above_25pct():
    """|drift_pct| > 25% labels the line as 'critical drift'."""
    import cost_digest

    line, needs_watch = cost_digest._drift_line(
        _recon(local=6.00, anthropic=10.00, drift_pct=0.40)
    )
    assert needs_watch is True
    assert "+40.0%" in line
    assert "critical drift" in line


def test_drift_line_within_tolerance_no_watch():
    """At 5% drift the line is informative but doesn't trigger the watch banner."""
    import cost_digest

    line, needs_watch = cost_digest._drift_line(
        _recon(local=9.50, anthropic=10.00, drift_pct=0.05)
    )
    assert needs_watch is False
    assert "within tolerance" in line


# ──────────────────────────────────────────────────────────────────────────
# Admin-ID resolution
# ──────────────────────────────────────────────────────────────────────────


def test_resolve_admin_ids_falls_back_to_portco_registry(monkeypatch):
    """No config var set → falls back to portco_registry.get_admin_user_ids()."""
    import cost_digest

    # Ensure neither config attribute is present.
    import config

    monkeypatch.delattr(config, "ADMIN_USER_IDS", raising=False)
    monkeypatch.delattr(config, "SLACK_ADMIN_USER_ID", raising=False)

    fake_get = MagicMock(return_value=["U_REGISTRY"])
    monkeypatch.setattr("portco_registry.get_admin_user_ids", fake_get)

    assert cost_digest._resolve_admin_ids() == ["U_REGISTRY"]
    fake_get.assert_called_once()


def test_resolve_admin_ids_prefers_config_list(monkeypatch):
    """config.ADMIN_USER_IDS wins over the portco_registry fallback."""
    import cost_digest

    import config

    monkeypatch.setattr(config, "ADMIN_USER_IDS", ["U_ENV_A", "U_ENV_B"], raising=False)
    fake_get = MagicMock(return_value=["U_REGISTRY"])
    monkeypatch.setattr("portco_registry.get_admin_user_ids", fake_get)

    assert cost_digest._resolve_admin_ids() == ["U_ENV_A", "U_ENV_B"]
    fake_get.assert_not_called()


def test_resolve_admin_ids_accepts_comma_string(monkeypatch):
    """config.ADMIN_USER_IDS as a comma-separated string is split."""
    import cost_digest

    import config

    monkeypatch.setattr(config, "ADMIN_USER_IDS", "U_A, U_B ,U_C", raising=False)
    monkeypatch.delattr(config, "SLACK_ADMIN_USER_ID", raising=False)

    assert cost_digest._resolve_admin_ids() == ["U_A", "U_B", "U_C"]


def test_resolve_admin_ids_single_user_fallback(monkeypatch):
    """config.SLACK_ADMIN_USER_ID is used when no list var is set."""
    import cost_digest

    import config

    monkeypatch.delattr(config, "ADMIN_USER_IDS", raising=False)
    monkeypatch.setattr(config, "SLACK_ADMIN_USER_ID", "U_SOLO", raising=False)

    assert cost_digest._resolve_admin_ids() == ["U_SOLO"]


# ──────────────────────────────────────────────────────────────────────────
# send_daily_cost_digest — orchestration
# ──────────────────────────────────────────────────────────────────────────


def test_send_digest_no_admin_users_short_circuits(monkeypatch, caplog):
    """No admins configured → return skipped_reason='no_admin_users', no DB calls."""
    import cost_digest

    monkeypatch.setattr(cost_digest, "_resolve_admin_ids", lambda: [])
    sender = MagicMock()
    with caplog.at_level(logging.WARNING):
        result = cost_digest.send_daily_cost_digest(
            date(2026, 5, 10), sender=sender, admin_ids=None
        )

    assert result["skipped_reason"] == "no_admin_users"
    assert result["sent"] == 0
    assert result["recipients"] == []
    sender.assert_not_called()
    assert any("no admin users configured" in r.message for r in caplog.records)


def test_send_digest_no_database_renders_degraded_body(monkeypatch):
    """DATABASE_URL unset → degraded body, but DM is still sent to admins."""
    import cost_digest
    import db_adapter

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "")
    sender = MagicMock()
    result = cost_digest.send_daily_cost_digest(
        date(2026, 5, 10), sender=sender, admin_ids=["U_TEST"]
    )

    assert result["skipped_reason"] is None
    assert result["sent"] == 1
    assert result["failed"] == 0
    sender.assert_called_once()
    uid_arg, msg_arg = sender.call_args.args
    assert uid_arg == "U_TEST"
    assert "Cost tracking not configured" in msg_arg
    assert "2026-05-10" in msg_arg


def test_send_digest_partial_failure_continues(monkeypatch):
    """One user's DM blowing up does not block the others; counts reflect it."""
    import cost_digest
    import db_adapter

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "")

    def _sender(uid, _text):
        if uid == "U_FAIL":
            raise RuntimeError("slack down")
        return None

    result = cost_digest.send_daily_cost_digest(
        date(2026, 5, 10),
        sender=_sender,
        admin_ids=["U_OK_1", "U_FAIL", "U_OK_2"],
    )
    assert result["sent"] == 2
    assert result["failed"] == 1
    assert result["recipients"] == ["U_OK_1", "U_FAIL", "U_OK_2"]


def test_send_digest_passes_message_to_each_admin(monkeypatch):
    """Same composed message goes to every admin (single render, fan-out send)."""
    import cost_digest
    import db_adapter

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "")
    sender = MagicMock()
    cost_digest.send_daily_cost_digest(
        date(2026, 5, 10),
        sender=sender,
        admin_ids=["U_A", "U_B"],
    )
    assert sender.call_count == 2
    first_msg = sender.call_args_list[0].args[1]
    second_msg = sender.call_args_list[1].args[1]
    assert first_msg == second_msg


def test_send_digest_uses_compute_reconciliation_result(monkeypatch):
    """End-to-end: DB rows → rendered body includes the drift line for the day."""
    import cost_digest
    import db_adapter

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "postgres://test")

    # Stub the helpers that hit the DB so we control the entire input shape.
    monkeypatch.setattr(cost_digest, "_total_cost", lambda d: 4.22)
    monkeypatch.setattr(
        cost_digest,
        "_portco_totals",
        lambda d: [{"portco": "acme", "cost_usd": 3.12, "sessions": 8}],
    )
    monkeypatch.setattr(
        cost_digest,
        "_trigger_totals",
        lambda d: [
            {"trigger": "cron", "cost_usd": 2.40, "sessions": 4},
            {"trigger": "slack_mention", "cost_usd": 1.18, "sessions": 4},
        ],
    )
    monkeypatch.setattr(cost_digest, "_cache_hit_pct", lambda d: 71.2)
    monkeypatch.setattr(
        cost_digest,
        "_top_sessions",
        lambda d, limit=5: [
            {
                "cost_usd": 0.84,
                "trigger": "slack_mention",
                "portco": "acme",
                "session_id": "sesn_EXAMPLE",
                "thread_ts": "T0123",
            }
        ],
    )
    monkeypatch.setattr(
        cost_digest,
        "compute_reconciliation",
        lambda d: _recon(local=4.22, anthropic=4.31, drift_pct=0.021),
    )

    sender = MagicMock()
    result = cost_digest.send_daily_cost_digest(
        date(2026, 5, 10), sender=sender, admin_ids=["U_TEST"]
    )

    sender.assert_called_once()
    msg = sender.call_args.args[1]
    assert "Total: $4.22" in msg
    assert "By portco: acme $3.12" in msg
    assert "Cache: 71% hit-rate" in msg
    assert "Drift vs Anthropic billing: +2.1%" in msg
    assert "$0.84  slack_mention  acme  (thread T0123)" in msg
    assert result["sent"] == 1


def test_send_digest_reconciliation_failure_falls_back_to_na(monkeypatch):
    """compute_reconciliation raising → drift line shows n/a, digest still ships."""
    import cost_digest
    import db_adapter

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "postgres://test")
    monkeypatch.setattr(cost_digest, "_total_cost", lambda d: 1.50)
    monkeypatch.setattr(cost_digest, "_portco_totals", lambda d: [])
    monkeypatch.setattr(cost_digest, "_trigger_totals", lambda d: [])
    monkeypatch.setattr(cost_digest, "_cache_hit_pct", lambda d: None)
    monkeypatch.setattr(cost_digest, "_top_sessions", lambda d, limit=5: [])

    def _boom(d):
        raise RuntimeError("recon down")

    monkeypatch.setattr(cost_digest, "compute_reconciliation", _boom)

    sender = MagicMock()
    result = cost_digest.send_daily_cost_digest(
        date(2026, 5, 10), sender=sender, admin_ids=["U_TEST"]
    )
    msg = sender.call_args.args[1]
    assert "Drift vs Anthropic billing: n/a" in msg
    assert result["sent"] == 1


def test_send_digest_no_sender_returns_skipped_reason(monkeypatch, caplog):
    """No Slack sender available → message composed but skipped_reason='no_sender'.

    Force the no-sender path deterministically by removing ``slack_bot`` from
    ``sys.modules`` and making a fresh import raise. The lazy import inside
    ``send_daily_cost_digest`` is then guaranteed to fail.
    """
    import cost_digest
    import db_adapter
    import builtins

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "")
    monkeypatch.delitem(sys.modules, "slack_bot", raising=False)

    real_import = builtins.__import__

    def _block_slack_bot(name, *args, **kwargs):
        if name == "slack_bot":
            raise ImportError("slack_bot disabled for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _block_slack_bot)

    with caplog.at_level(logging.WARNING):
        result = cost_digest.send_daily_cost_digest(
            date(2026, 5, 10), sender=None, admin_ids=["U_X"]
        )
    assert result["date"] == "2026-05-10"
    assert result["recipients"] == ["U_X"]
    assert result["sent"] == 0
    assert result["failed"] == 0
    assert result["skipped_reason"] == "no_sender"
    assert result["message"]  # body was still composed
    assert any("no Slack sender available" in r.message for r in caplog.records)


def test_send_digest_default_target_is_yesterday(monkeypatch):
    """target_date=None → date.today() - 1 day."""
    import cost_digest
    import db_adapter

    monkeypatch.setattr(db_adapter, "DATABASE_URL", "")
    captured = {}

    def _sender(uid, text):
        captured["date_in_body"] = text

    result = cost_digest.send_daily_cost_digest(sender=_sender, admin_ids=["U_TEST"])
    expected = date.today().toordinal() - 1
    assert result["date"] == date.fromordinal(expected).isoformat()


# ──────────────────────────────────────────────────────────────────────────
# main.scheduled_cost_digest — cron wrapper exception swallowing
# ──────────────────────────────────────────────────────────────────────────


def _import_main_fresh():
    """Re-import ``main`` with anthropic.Anthropic patched so module load is cheap."""
    sys.modules.pop("config", None)
    sys.modules.pop("main", None)
    with patch("anthropic.Anthropic", MagicMock()):
        import main  # noqa: F401

        return importlib.import_module("main")


def test_scheduled_cost_digest_success_logs_summary(caplog):
    """Happy path: wrapper invokes the digest, logs recipients/sent/failed."""
    main = _import_main_fresh()
    fake_result = {
        "date": "2026-05-10",
        "message": "...",
        "recipients": ["U_X", "U_Y"],
        "sent": 2,
        "failed": 0,
        "skipped_reason": None,
    }
    with (
        patch("cost_digest.send_daily_cost_digest", return_value=fake_result) as mocked,
        patch.object(main, "send_notification") as mocked_notify,
    ):
        with caplog.at_level(logging.INFO, logger="orchestrator"):
            main.scheduled_cost_digest()
    mocked.assert_called_once_with()
    mocked_notify.assert_not_called()
    assert any(
        "Cost digest completed" in r.message
        and "recipients=2" in r.message
        and "sent=2" in r.message
        for r in caplog.records
    )


def test_scheduled_cost_digest_swallows_exceptions(caplog):
    """Wrapper must catch + log + Slack-notify, NEVER re-raise into APScheduler."""
    main = _import_main_fresh()
    with (
        patch("cost_digest.send_daily_cost_digest", side_effect=RuntimeError("kaboom")),
        patch.object(main, "send_notification") as mocked_notify,
    ):
        with caplog.at_level(logging.ERROR, logger="orchestrator"):
            # The point of the test: must not raise.
            main.scheduled_cost_digest()
    assert any("Daily cost digest failed" in r.message for r in caplog.records)
    mocked_notify.assert_called_once()
    args, _kwargs = mocked_notify.call_args
    assert args[0] == "watch"
    assert "Daily cost digest failed" in args[1]


def test_scheduled_cost_digest_skipped_no_admins_logs_reason(caplog):
    """When the digest returns skipped_reason, the wrapper logs it but does NOT alert."""
    main = _import_main_fresh()
    fake_result = {
        "date": "2026-05-10",
        "message": "",
        "recipients": [],
        "sent": 0,
        "failed": 0,
        "skipped_reason": "no_admin_users",
    }
    with (
        patch("cost_digest.send_daily_cost_digest", return_value=fake_result),
        patch.object(main, "send_notification") as mocked_notify,
    ):
        with caplog.at_level(logging.INFO, logger="orchestrator"):
            main.scheduled_cost_digest()
    mocked_notify.assert_not_called()
    assert any("skipped_reason=no_admin_users" in r.message for r in caplog.records)


# ──────────────────────────────────────────────────────────────────────────
# main.main() — cost-digest cron registration
# ──────────────────────────────────────────────────────────────────────────


class _FakeScheduler:
    """Mirror of main_test._FakeScheduler — record add_job calls only."""

    def __init__(self, *args, **kwargs):
        self.jobs: list[dict] = []
        self.timezone = kwargs.get("timezone")
        self.started = False

    def add_job(self, func, trigger=None, *, id=None, name=None, **kwargs):  # noqa: A002
        self.jobs.append(
            {"func": func, "trigger": trigger, "id": id, "name": name, "kwargs": kwargs}
        )

    def add_listener(self, *args, **kwargs):  # pragma: no cover - trivial
        pass

    def start(self):
        self.started = True

    def shutdown(self, **kwargs):  # pragma: no cover - trivial
        pass


def _run_main_with_fakes():
    main = _import_main_fresh()
    fake_sched = _FakeScheduler()
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


def test_main_does_not_register_cost_digest_cron():
    """Retired 2026-05-14: ``cost-digest`` job is no longer scheduled.
    The underlying ``send_daily_cost_digest`` function stays callable for
    on-demand use, but the daily DM cron was retired pending JTBD redefinition."""
    sched = _run_main_with_fakes()
    matches = [j for j in sched.jobs if j["id"] == "cost-digest"]
    assert matches == [], (
        f"cost-digest cron was retired and must not be re-registered, "
        f"got: {[j['id'] for j in sched.jobs]}"
    )
