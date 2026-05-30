"""Tests for the Plan #44 admin slash commands in
``orchestrator/slack_bot.py``:

  - ``/pin <agent> <version>``  (Task #10)
  - ``/stop [thread_ts]``        (Task #15)
  - ``/flag <name> <value>``     (Task #24)

The conftest stubs slack_bolt at import time so the module imports
clean. Each test sets ``SLACK_ADMIN_USER_IDS`` per case so the auth gate
can be exercised on both sides.
"""

from __future__ import annotations

import os

# config.py requires these env vars at import time. Set them BEFORE the
# first ``import slack_bot`` anywhere — values are placeholders; we
# never call the real Slack/Anthropic APIs because the conftest stubs
# slack_bolt.
for _k, _v in (
    ("ANTHROPIC_API_KEY", "sk-ant-test"),
    ("SLACK_BOT_TOKEN", "xoxb-test"),
    ("SLACK_APP_TOKEN", "xapp-test"),
    ("SLACK_CHANNEL_ID", "C_TEST"),
    ("ENVIRONMENT_ID", "env_test"),
    ("DREAM_AGENT_ID", "agent_dream_test"),
    ("COORDINATOR_ID", "agent_EXAMPLE_coordinator"),
    ("QUICK_AGENT_ID", "agent_quick_test"),
    ("METHODOLOGY_STORE_ID", "memstore_test"),
    ("HEALTH_STORE_ID", "memstore_health_test"),
):
    os.environ.setdefault(_k, _v)


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _admin_user(monkeypatch):
    """Default every test to a known admin so the auth gate doesn't fire
    unless explicitly tested."""
    monkeypatch.setenv("SLACK_ADMIN_USER_IDS", "UADMIN")
    yield


# ─────────────────────────────────────────────────────────────────────────────
# /pin
# ─────────────────────────────────────────────────────────────────────────────


def test_pin_pure_handler_writes_override(monkeypatch):
    import slack_bot

    writes: list = []
    monkeypatch.setattr(
        "version_pin_overrides.set_override",
        lambda agent, version, actor: writes.append((agent, version, actor)) or True,
    )

    msg = slack_bot._handle_pin_command("coordinator 35", "UADMIN")

    assert writes == [("coordinator", 35, "UADMIN")]
    assert ":white_check_mark:" in msg
    assert "coordinator" in msg and "v35" in msg


def test_pin_pure_handler_rejects_unknown_agent(monkeypatch):
    import slack_bot

    writes: list = []
    monkeypatch.setattr(
        "version_pin_overrides.set_override",
        lambda *a, **kw: writes.append(a) or True,
    )

    msg = slack_bot._handle_pin_command("totally_fake 1", "UADMIN")

    assert writes == []
    assert ":warning:" in msg
    assert "Unknown agent" in msg


def test_pin_pure_handler_rejects_non_int_version(monkeypatch):
    import slack_bot

    msg = slack_bot._handle_pin_command("coordinator abc", "UADMIN")

    assert ":warning:" in msg
    assert "not an integer" in msg


def test_pin_pure_handler_rejects_zero_version(monkeypatch):
    import slack_bot

    msg = slack_bot._handle_pin_command("coordinator 0", "UADMIN")

    assert ":warning:" in msg
    assert "positive" in msg


def test_pin_pure_handler_shows_usage_with_no_args():
    import slack_bot

    msg = slack_bot._handle_pin_command("", "UADMIN")

    assert "Usage" in msg
    assert "/pin" in msg


def test_pin_pure_handler_db_failure_surfaces_warning(monkeypatch):
    import slack_bot

    monkeypatch.setattr("version_pin_overrides.set_override", lambda *a, **kw: False)

    msg = slack_bot._handle_pin_command("coordinator 35", "UADMIN")

    assert ":warning:" in msg
    assert "logs" in msg.lower() or "fail" in msg.lower()


def test_pin_bolt_wrapper_rejects_non_admin(monkeypatch):
    """The /pin Bolt wrapper enforces the admin gate before the pure
    handler ever runs."""
    monkeypatch.setenv("SLACK_ADMIN_USER_IDS", "UADMIN")
    import slack_bot

    ack_calls: list = []
    respond_calls: list = []

    def fake_ack():
        ack_calls.append(True)

    def fake_respond(text, response_type=None):
        respond_calls.append({"text": text, "response_type": response_type})

    # NOT an admin user.
    slack_bot.on_pin_command(
        fake_ack,
        {"text": "coordinator 35", "user_id": "URANDOM"},
        fake_respond,
    )

    assert len(ack_calls) == 1
    assert len(respond_calls) == 1
    assert ":no_entry:" in respond_calls[0]["text"]
    assert respond_calls[0]["response_type"] == "ephemeral"


def test_pin_bolt_wrapper_admin_path_writes(monkeypatch):
    monkeypatch.setenv("SLACK_ADMIN_USER_IDS", "UADMIN")
    import slack_bot

    writes: list = []
    monkeypatch.setattr(
        "version_pin_overrides.set_override",
        lambda agent, version, actor: writes.append((agent, version, actor)) or True,
    )

    respond_calls: list = []

    def fake_respond(text, response_type=None):
        respond_calls.append({"text": text, "response_type": response_type})

    slack_bot.on_pin_command(
        lambda: None,
        {"text": "coordinator 35", "user_id": "UADMIN"},
        fake_respond,
    )

    assert writes == [("coordinator", 35, "UADMIN")]
    assert ":white_check_mark:" in respond_calls[0]["text"]


# ─────────────────────────────────────────────────────────────────────────────
# /stop
# ─────────────────────────────────────────────────────────────────────────────


def test_stop_pure_handler_no_thread_shows_usage():
    import slack_bot

    msg = slack_bot._handle_stop_command("", "UADMIN", "C42", "")

    assert "Usage" in msg
    assert "/stop" in msg


def test_stop_pure_handler_no_investigation_warns(monkeypatch):
    import slack_bot

    monkeypatch.setattr("db_adapter.get_investigation_for_thread", lambda _ts: None)

    msg = slack_bot._handle_stop_command("", "UADMIN", "C42", "1737.000")

    assert ":warning:" in msg
    assert "No investigation" in msg


def test_stop_pure_handler_already_terminal_warns(monkeypatch):
    import slack_bot

    monkeypatch.setattr(
        "db_adapter.get_investigation_for_thread",
        lambda _ts: {
            "id": "inv_1",
            "session_id": "sesn_EXAMPLE",
            "user_id": "U_AUTHOR",
            "status": "completed",
            "agent_id": "agent_EXAMPLE_coordinator",
        },
    )

    msg = slack_bot._handle_stop_command("", "UADMIN", "C42", "1737.000")

    assert "already" in msg.lower() and "completed" in msg


def test_stop_pure_handler_rejects_non_admin_non_author(monkeypatch):
    import slack_bot

    monkeypatch.setattr(
        "db_adapter.get_investigation_for_thread",
        lambda _ts: {
            "id": "inv_1",
            "session_id": "sesn_EXAMPLE",
            "user_id": "U_AUTHOR",
            "status": "running",
            "agent_id": "agent_x",
        },
    )

    msg = slack_bot._handle_stop_command("", "URANDOM", "C42", "1737.000")

    assert ":no_entry:" in msg


def test_stop_pure_handler_non_admin_no_info_leak_on_thread_existence(
    monkeypatch,
):
    """Closing-review MEDIUM #5 (2026-05-13): the /stop handler must
    return the SAME error message to non-admin non-author callers
    whether the thread has an investigation or not. Otherwise the
    error-message divergence reveals "this thread has a running
    investigation owned by someone else" — a minor info leak.

    Auth check now happens BEFORE the DB-read result is used to choose
    the error string for non-admins.
    """
    import slack_bot

    # Case A: thread has someone else's running investigation.
    monkeypatch.setattr(
        "db_adapter.get_investigation_for_thread",
        lambda _ts: {
            "id": "inv_1",
            "session_id": "sesn_EXAMPLE",
            "user_id": "U_AUTHOR",  # not the caller
            "status": "running",
            "agent_id": "agent_x",
        },
    )
    msg_thread_has_inv = slack_bot._handle_stop_command(
        "", "URANDOM", "C42", "1737.000"
    )

    # Case B: thread has no investigation at all.
    monkeypatch.setattr("db_adapter.get_investigation_for_thread", lambda _ts: None)
    msg_thread_empty = slack_bot._handle_stop_command("", "URANDOM", "C42", "1737.000")

    # Both non-admin non-author paths must surface the SAME response,
    # so an attacker can't probe thread existence.
    assert msg_thread_has_inv == msg_thread_empty
    assert ":no_entry:" in msg_thread_has_inv


def test_stop_pure_handler_admin_still_sees_no_investigation_warning(monkeypatch):
    """Admins still get the differentiated warning so 2am incident
    triage isn't impeded — only non-admins see the unified message."""
    import slack_bot

    monkeypatch.setattr("db_adapter.get_investigation_for_thread", lambda _ts: None)

    msg = slack_bot._handle_stop_command("", "UADMIN", "C42", "1737.000")

    assert ":warning:" in msg
    assert "No investigation" in msg


def test_stop_pure_handler_admin_bypasses_db_when_thread_present(monkeypatch):
    """Auth flow ordering test: admin gate is evaluated BEFORE the
    non-admin path is taken. The admin path still reads the DB so
    session_id is available, but it does not consult ``user_id``."""
    import slack_bot

    lookups: list = []

    def _lookup(ts):
        lookups.append(ts)
        return {
            "id": "inv_1",
            "session_id": "sesn_EXAMPLE",
            "user_id": "U_AUTHOR",  # someone else owns it
            "status": "running",
            "agent_id": "agent_EXAMPLE_coordinator",
        }

    monkeypatch.setattr("db_adapter.get_investigation_for_thread", _lookup)
    monkeypatch.setattr(
        "session_interrupt.interrupt_session",
        lambda session_id, **kw: {
            "ok": True,
            "tokens_burned": 1,
            "cost_usd": 0.0,
            "session_id": session_id,
            "thread_id": "",
            "error": "",
        },
    )

    msg = slack_bot._handle_stop_command("", "UADMIN", "C42", "1737.000")

    # Admin successfully stops someone else's investigation.
    assert ":octagonal_sign:" in msg
    # Lookup did happen — admin path still needs the row.
    assert lookups == ["1737.000"]


def test_stop_pure_handler_admin_succeeds(monkeypatch):
    """Happy path: admin runs /stop in a thread with a running session."""
    import slack_bot

    monkeypatch.setattr(
        "db_adapter.get_investigation_for_thread",
        lambda _ts: {
            "id": "inv_1",
            "session_id": "sesn_EXAMPLE",
            "user_id": "U_AUTHOR",
            "status": "running",
            "agent_id": "agent_EXAMPLE_coordinator",
        },
    )
    monkeypatch.setattr(
        "session_interrupt.interrupt_session",
        lambda session_id, **kwargs: {
            "ok": True,
            "tokens_burned": 12_345,
            "cost_usd": 0.4567,
            "thread_id": "",
            "session_id": session_id,
            "error": "",
        },
    )

    msg = slack_bot._handle_stop_command("", "UADMIN", "C42", "1737.000")

    assert ":octagonal_sign:" in msg
    assert "sesn_EXAMPLE" in msg
    assert "12,345" in msg  # token formatting
    assert "$0.4567" in msg
    # FIX-COMMAND template includes a pre-filled rollback.
    assert "bin/rollback-agent.py coordinator" in msg


def test_stop_pure_handler_author_can_stop_own_investigation(monkeypatch):
    import slack_bot

    monkeypatch.setattr(
        "db_adapter.get_investigation_for_thread",
        lambda _ts: {
            "id": "inv_1",
            "session_id": "sesn_EXAMPLE",
            "user_id": "U_AUTHOR",
            "status": "running",
            "agent_id": "agent_x",
        },
    )
    monkeypatch.setattr(
        "session_interrupt.interrupt_session",
        lambda session_id, **kw: {
            "ok": True,
            "tokens_burned": 100,
            "cost_usd": 0.01,
            "session_id": session_id,
            "thread_id": "",
            "error": "",
        },
    )

    # Author (not admin) — should pass auth.
    msg = slack_bot._handle_stop_command("", "U_AUTHOR", "C42", "1737.000")

    assert ":octagonal_sign:" in msg


def test_stop_pure_handler_explicit_thread_ts_arg(monkeypatch):
    import slack_bot

    called_with: list = []

    def fake_lookup(ts):
        called_with.append(ts)
        return None

    monkeypatch.setattr("db_adapter.get_investigation_for_thread", fake_lookup)

    slack_bot._handle_stop_command("1737654.999", "UADMIN", "C42", "")

    assert called_with == ["1737654.999"]


def test_stop_pure_handler_kill_switch_disables_command(monkeypatch):
    import slack_bot

    monkeypatch.setattr(slack_bot, "_stop_command_enabled", lambda: False)

    msg = slack_bot._handle_stop_command("", "UADMIN", "C42", "1737.000")

    assert "disabled" in msg.lower()
    assert "STOP_COMMAND_ENABLED" in msg


def test_stop_pure_handler_interrupt_failure_surfaces(monkeypatch):
    import slack_bot

    monkeypatch.setattr(
        "db_adapter.get_investigation_for_thread",
        lambda _ts: {
            "id": "inv_1",
            "session_id": "sesn_EXAMPLE",
            "user_id": "U_AUTHOR",
            "status": "running",
            "agent_id": "agent_x",
        },
    )
    monkeypatch.setattr(
        "session_interrupt.interrupt_session",
        lambda session_id, **kw: {
            "ok": False,
            "tokens_burned": 0,
            "cost_usd": 0.0,
            "session_id": session_id,
            "thread_id": "",
            "error": "events.send failed: 500",
        },
    )

    msg = slack_bot._handle_stop_command("", "UADMIN", "C42", "1737.000")

    assert ":x:" in msg
    assert "events.send failed: 500" in msg


# ─────────────────────────────────────────────────────────────────────────────
# /flag
# ─────────────────────────────────────────────────────────────────────────────


def test_flag_pure_handler_lists_flags_with_no_args(monkeypatch):
    import slack_bot

    monkeypatch.setattr("flag_overrides.get_flag", lambda name, default: "<unset>")

    msg = slack_bot._handle_flag_command("", "UADMIN")

    # Every whitelisted flag appears.
    for name in slack_bot.FLAG_ALLOWED:
        assert name in msg


def test_flag_pure_handler_rejects_unknown_flag():
    import slack_bot

    msg = slack_bot._handle_flag_command("WHATEVER true", "UADMIN")

    assert ":warning:" in msg
    assert "WHATEVER" in msg


def test_flag_pure_handler_bool_flag_normalizes(monkeypatch):
    import slack_bot

    writes: list = []
    monkeypatch.setattr(
        "flag_overrides.set_flag",
        lambda name, value, actor: writes.append((name, value, actor)) or True,
    )

    msg = slack_bot._handle_flag_command("COMPRESSION_ENABLED yes", "UADMIN")

    assert writes == [("COMPRESSION_ENABLED", "true", "UADMIN")]
    assert ":white_check_mark:" in msg


def test_flag_pure_handler_bool_flag_rejects_invalid_value(monkeypatch):
    import slack_bot

    writes: list = []
    monkeypatch.setattr(
        "flag_overrides.set_flag",
        lambda *a, **kw: writes.append(a) or True,
    )

    msg = slack_bot._handle_flag_command("COMPRESSION_ENABLED maybe", "UADMIN")

    assert writes == []
    assert ":warning:" in msg
    assert "maybe" in msg


def test_flag_pure_handler_pct_flag_clamps(monkeypatch):
    import slack_bot

    writes: list = []
    monkeypatch.setattr(
        "flag_overrides.set_flag",
        lambda name, value, actor: writes.append((name, value, actor)) or True,
    )

    # In range
    msg = slack_bot._handle_flag_command("LIMITED_NETWORKING_SHADOW_PCT 50", "UADMIN")
    assert ":white_check_mark:" in msg
    assert writes == [("LIMITED_NETWORKING_SHADOW_PCT", "50", "UADMIN")]

    # Out of range
    writes.clear()
    msg = slack_bot._handle_flag_command("LIMITED_NETWORKING_SHADOW_PCT 250", "UADMIN")
    assert ":warning:" in msg
    assert writes == []


def test_flag_pure_handler_enum_flag_accepts_only_allowed(monkeypatch):
    """SMOKE_PROBE_LEVEL accepts Plan #44 Task #20 vocabulary:
    ``off | quick | full``. ``deep``/``shallow`` (the pre-fix typo set)
    are now rejected. Closing-review HIGH #2 (2026-05-13)."""
    import slack_bot

    writes: list = []
    monkeypatch.setattr(
        "flag_overrides.set_flag",
        lambda name, value, actor: writes.append((name, value, actor)) or True,
    )

    # Plan #44 vocabulary: each of off/quick/full is accepted.
    for level in ("off", "quick", "full"):
        writes.clear()
        msg = slack_bot._handle_flag_command(f"SMOKE_PROBE_LEVEL {level}", "UADMIN")
        assert ":white_check_mark:" in msg, f"level={level!r} should succeed"
        assert writes == [("SMOKE_PROBE_LEVEL", level, "UADMIN")]

    # Legacy typo set is now rejected.
    for bad in ("deep", "shallow", "nuclear"):
        writes.clear()
        msg = slack_bot._handle_flag_command(f"SMOKE_PROBE_LEVEL {bad}", "UADMIN")
        assert ":warning:" in msg, f"level={bad!r} should be rejected"
        assert writes == []


def test_flag_pure_handler_show_single_flag_value(monkeypatch):
    """`/flag SMOKE_PROBE_LEVEL` shows that single flag's value."""
    import slack_bot

    monkeypatch.setattr("flag_overrides.get_flag", lambda name, default: "full")

    msg = slack_bot._handle_flag_command("SMOKE_PROBE_LEVEL", "UADMIN")

    assert "SMOKE_PROBE_LEVEL" in msg
    assert "full" in msg


def test_flag_smoke_probe_description_uses_plan_44_vocabulary():
    """The human-readable description in FLAG_ALLOWED must match Plan
    #44 Task #20 — ``off | quick | full`` (closing-review HIGH #2)."""
    import slack_bot

    validator, desc = slack_bot.FLAG_ALLOWED["SMOKE_PROBE_LEVEL"]
    assert "quick" in desc
    assert "full" in desc
    assert "shallow" not in desc and "deep" not in desc


def test_flag_pure_handler_db_failure_surfaces_warning(monkeypatch):
    import slack_bot

    monkeypatch.setattr("flag_overrides.set_flag", lambda *a, **kw: False)

    msg = slack_bot._handle_flag_command("COMPRESSION_ENABLED false", "UADMIN")

    assert ":warning:" in msg
    assert "logs" in msg.lower()


def test_flag_bolt_wrapper_rejects_non_admin(monkeypatch):
    monkeypatch.setenv("SLACK_ADMIN_USER_IDS", "UADMIN")
    import slack_bot

    respond_calls: list = []

    def fake_respond(text, response_type=None):
        respond_calls.append({"text": text, "response_type": response_type})

    slack_bot.on_flag_command(
        lambda: None,
        {"text": "COMPRESSION_ENABLED false", "user_id": "URANDOM"},
        fake_respond,
    )

    assert ":no_entry:" in respond_calls[0]["text"]
