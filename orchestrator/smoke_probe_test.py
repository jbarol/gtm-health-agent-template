"""Tests for the Plan #42 PR2 pre-deploy smoke probe.

Mocks MCP (``sf_dump_tool.dump_sf_query``) and Anthropic (``anthropic.Anthropic``
client). Tests cover:

  * All-green probe → ``passed=True``, anthropic_status=``ok``.
  * Check A mismatch → ``passed=False`` with the right reason.
  * Check B failure → ``passed=False``; Check C SKIPPED.
  * Check C timeout → ``passed=False``; ``error="timeout"``.
  * Anthropic 429 → inconclusive PASS (``passed=True``,
    ``anthropic_status='rate_limited'``).
  * Anthropic 5xx → inconclusive PASS (``passed=True``,
    ``anthropic_status='unavailable'``).
  * ``smoke_probe_runs`` INSERT executes with the right column values.
  * ``--local`` mode allows missing ``BUILD_COMMIT`` in Check A.
  * CLI exit code matches outcome.

Run:
    cd orchestrator && python3 -m pytest smoke_probe_test.py -q
"""

from __future__ import annotations

import importlib
import os
import sys
from unittest.mock import MagicMock, patch


# Stub the required env vars BEFORE first import — the worktree has no .env.
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


def _import_smoke_probe_fresh():
    """Re-import ``smoke_probe`` so env-var patches stick across tests."""
    sys.modules.pop("config", None)
    sys.modules.pop("smoke_probe", None)
    return importlib.import_module("smoke_probe")


# ──────────────────────────────────────────────────────────────────────────
# Fake helpers
# ──────────────────────────────────────────────────────────────────────────


class _FakeAgentBlock:
    """Tiny stand-in for ``event.content[*]`` blocks."""

    def __init__(self, text: str):
        self.text = text


class _FakeEvent:
    """Stand-in for the anthropic stream event objects."""

    def __init__(self, type: str, content=None):
        self.type = type
        self.content = content or []


class _FakeStream:
    """Iterable + context-manager fake for ``sessions.events.stream``."""

    def __init__(self, events):
        self._events = list(events)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(self._events)


def _make_quick_answer_client(*, sentinel="smoke-probe-ok", events=None):
    """Build a MagicMock anthropic client that returns a sentinel response.

    Returns ``(client, sessions_create_mock)``.
    """
    client = MagicMock()
    session = MagicMock()
    session.id = "sesn_EXAMPLE"
    client.beta.sessions.create.return_value = session

    if events is None:
        events = [
            _FakeEvent(
                "agent.message",
                content=[_FakeAgentBlock(sentinel)],
            ),
            _FakeEvent("session.status_idle"),
        ]
    client.beta.sessions.events.stream.return_value = _FakeStream(events)
    return client


# ──────────────────────────────────────────────────────────────────────────
# Check A — build_commit
# ──────────────────────────────────────────────────────────────────────────


def test_check_a_passes_when_env_matches_pin_file(tmp_path, monkeypatch):
    sp = _import_smoke_probe_fresh()
    pin = tmp_path / "active_versions.json"
    pin.write_text('{"build_commit": "abc123def"}')
    monkeypatch.setattr(sp, "_ACTIVE_VERSIONS_PATH", pin)
    monkeypatch.setenv("BUILD_COMMIT", "abc123def")
    result = sp._check_build_commit(local_mode=False)
    assert result["ok"] is True
    assert "abc123de" in result["detail"]


def test_check_a_fails_on_mismatch(tmp_path, monkeypatch):
    sp = _import_smoke_probe_fresh()
    pin = tmp_path / "active_versions.json"
    pin.write_text('{"build_commit": "deadbeef00"}')
    monkeypatch.setattr(sp, "_ACTIVE_VERSIONS_PATH", pin)
    monkeypatch.setenv("BUILD_COMMIT", "abc123def")
    result = sp._check_build_commit(local_mode=False)
    assert result["ok"] is False
    assert "mismatch" in result["detail"]


def test_check_a_warns_in_local_mode_when_env_missing(monkeypatch):
    sp = _import_smoke_probe_fresh()
    monkeypatch.delenv("BUILD_COMMIT", raising=False)
    result = sp._check_build_commit(local_mode=True)
    assert result["ok"] is True
    assert "WARN" in result["detail"]


def test_check_a_fails_in_prod_mode_when_env_missing(monkeypatch):
    sp = _import_smoke_probe_fresh()
    monkeypatch.delenv("BUILD_COMMIT", raising=False)
    result = sp._check_build_commit(local_mode=False)
    assert result["ok"] is False
    assert "unset" in result["detail"]


def test_check_a_passes_when_pin_file_missing_but_env_set(tmp_path, monkeypatch):
    """Pre-Plan #41 deploys may not have build_commit in the pin file yet."""
    sp = _import_smoke_probe_fresh()
    pin = tmp_path / "does_not_exist.json"
    monkeypatch.setattr(sp, "_ACTIVE_VERSIONS_PATH", pin)
    monkeypatch.setenv("BUILD_COMMIT", "abc123def")
    result = sp._check_build_commit(local_mode=False)
    assert result["ok"] is True


# ──────────────────────────────────────────────────────────────────────────
# Check B — dump_sf_query
# ──────────────────────────────────────────────────────────────────────────


def test_check_b_passes_when_sf_returns_rows():
    sp = _import_smoke_probe_fresh()
    fake_dump = MagicMock(
        return_value={"file_path": "/tmp/x.parquet", "count": 1, "error": ""}
    )
    with patch.dict(sys.modules, {"sf_dump_tool": MagicMock(dump_sf_query=fake_dump)}):
        result = sp._check_dump_sf_query()
    assert result["ok"] is True
    assert result["count"] == 1
    fake_dump.assert_called_once()


def test_check_b_fails_when_dump_returns_error():
    sp = _import_smoke_probe_fresh()
    fake_dump = MagicMock(
        return_value={
            "file_path": None,
            "count": 0,
            "error": "sf_auth_failed: bad creds",
        }
    )
    with patch.dict(sys.modules, {"sf_dump_tool": MagicMock(dump_sf_query=fake_dump)}):
        result = sp._check_dump_sf_query()
    assert result["ok"] is False
    assert "sf_auth_failed" in result["detail"]


def test_check_b_fails_when_dump_raises_unexpectedly():
    sp = _import_smoke_probe_fresh()

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated MCP outage")

    with patch.dict(sys.modules, {"sf_dump_tool": MagicMock(dump_sf_query=_boom)}):
        result = sp._check_dump_sf_query()
    assert result["ok"] is False
    assert "exception" in result["detail"]


# ──────────────────────────────────────────────────────────────────────────
# Check C — Quick Answer agent
# ──────────────────────────────────────────────────────────────────────────


def test_check_c_passes_when_agent_returns_sentinel(monkeypatch):
    sp = _import_smoke_probe_fresh()
    monkeypatch.setenv("QUICK_AGENT_ID", "agent_test")
    monkeypatch.setenv("ENVIRONMENT_ID", "env_test")
    client = _make_quick_answer_client()
    with patch("anthropic.Anthropic", return_value=client):
        result = sp._check_quick_answer_agent()
    assert result["ok"] is True
    assert result["anthropic_status"] == "ok"


def test_check_c_fails_when_response_missing_sentinel(monkeypatch):
    sp = _import_smoke_probe_fresh()
    monkeypatch.setenv("QUICK_AGENT_ID", "agent_test")
    monkeypatch.setenv("ENVIRONMENT_ID", "env_test")
    client = _make_quick_answer_client(sentinel="totally-different-answer")
    with patch("anthropic.Anthropic", return_value=client):
        result = sp._check_quick_answer_agent()
    assert result["ok"] is False
    assert "sentinel" in result["error"]


def test_check_c_handles_anthropic_rate_limit_inconclusive(monkeypatch):
    """RateLimitError → ok=True, anthropic_status='rate_limited' (D7)."""
    import anthropic

    sp = _import_smoke_probe_fresh()
    monkeypatch.setenv("QUICK_AGENT_ID", "agent_test")
    monkeypatch.setenv("ENVIRONMENT_ID", "env_test")

    client = MagicMock()
    session = MagicMock()
    session.id = "sesn_EXAMPLE"
    client.beta.sessions.create.return_value = session

    def _raise_429(*args, **kwargs):
        # The constructor signature varies across anthropic versions; build a
        # RateLimitError directly by going through its base.
        err = anthropic.RateLimitError.__new__(anthropic.RateLimitError)
        err.status_code = 429
        raise err

    client.beta.sessions.events.stream.side_effect = _raise_429
    with patch("anthropic.Anthropic", return_value=client):
        result = sp._check_quick_answer_agent()
    assert result["ok"] is True, "429 should be inconclusive PASS"
    assert result["anthropic_status"] == "rate_limited"


def test_check_c_handles_anthropic_5xx_inconclusive(monkeypatch):
    """503-class error → ok=True, anthropic_status='unavailable' (D7)."""
    sp = _import_smoke_probe_fresh()
    monkeypatch.setenv("QUICK_AGENT_ID", "agent_test")
    monkeypatch.setenv("ENVIRONMENT_ID", "env_test")

    client = MagicMock()
    session = MagicMock()
    session.id = "sesn_EXAMPLE"
    client.beta.sessions.create.return_value = session

    class _FakeServerError(Exception):
        status_code = 503

    def _raise_503(*args, **kwargs):
        raise _FakeServerError("service unavailable")

    client.beta.sessions.events.stream.side_effect = _raise_503
    with patch("anthropic.Anthropic", return_value=client):
        result = sp._check_quick_answer_agent()
    assert result["ok"] is True, "5xx should be inconclusive PASS"
    assert result["anthropic_status"] == "unavailable"


def test_check_c_fails_on_timeout(monkeypatch):
    sp = _import_smoke_probe_fresh()
    monkeypatch.setenv("QUICK_AGENT_ID", "agent_test")
    monkeypatch.setenv("ENVIRONMENT_ID", "env_test")

    client = MagicMock()
    session = MagicMock()
    session.id = "sesn_EXAMPLE"
    client.beta.sessions.create.return_value = session

    # A finite stream of non-terminal events. Combined with a monotonic
    # clock that jumps past the deadline on the first event, the loop must
    # break with timed_out=True.
    events = [_FakeEvent("agent.message", content=[]) for _ in range(5)]
    client.beta.sessions.events.stream.return_value = _FakeStream(events)

    # The probe records ``started`` (initial monotonic call), computes
    # ``deadline = monotonic() + timeout_seconds`` (second call), then checks
    # ``monotonic() > deadline`` on each event. Return a giant value from the
    # third call onward so the timeout trips on the first event.
    monotonic_values = iter([0.0, 0.0, 100.0, 100.0, 100.0, 100.0, 100.0])

    def _fake_monotonic():
        try:
            return next(monotonic_values)
        except StopIteration:
            return 100.0

    with (
        patch("anthropic.Anthropic", return_value=client),
        patch.object(sp.time, "monotonic", side_effect=_fake_monotonic),
    ):
        result = sp._check_quick_answer_agent(timeout_seconds=5.0)
    assert result["ok"] is False
    assert result["error"] == "timeout"


def test_check_c_fails_when_agent_id_unset(monkeypatch):
    sp = _import_smoke_probe_fresh()
    # Force QUICK_AGENT_ID to empty after import.
    import config as _config

    monkeypatch.setattr(_config, "QUICK_AGENT_ID", "")
    result = sp._check_quick_answer_agent()
    assert result["ok"] is False
    assert "QUICK_AGENT_ID" in result["detail"]


# ──────────────────────────────────────────────────────────────────────────
# run_smoke_probe — end-to-end orchestration
# ──────────────────────────────────────────────────────────────────────────


def test_run_smoke_probe_all_green(monkeypatch, tmp_path):
    sp = _import_smoke_probe_fresh()
    pin = tmp_path / "active_versions.json"
    pin.write_text('{"build_commit": "abc12345"}')
    monkeypatch.setattr(sp, "_ACTIVE_VERSIONS_PATH", pin)
    monkeypatch.setenv("BUILD_COMMIT", "abc12345")
    monkeypatch.setenv("QUICK_AGENT_ID", "agent_test")

    fake_dump = MagicMock(
        return_value={"file_path": "/tmp/x.parquet", "count": 1, "error": ""}
    )
    quick_client = _make_quick_answer_client()
    with (
        patch.dict(sys.modules, {"sf_dump_tool": MagicMock(dump_sf_query=fake_dump)}),
        patch("anthropic.Anthropic", return_value=quick_client),
    ):
        result = sp.run_smoke_probe(persist=False, send_dm=False)
    assert result.passed is True
    assert result.anthropic_status == "ok"
    assert result.check_a_ok is True
    assert result.check_b_ok is True
    assert result.check_c_ok is True


def test_run_smoke_probe_build_commit_mismatch_fails(monkeypatch, tmp_path):
    sp = _import_smoke_probe_fresh()
    pin = tmp_path / "active_versions.json"
    pin.write_text('{"build_commit": "deadbeef"}')
    monkeypatch.setattr(sp, "_ACTIVE_VERSIONS_PATH", pin)
    monkeypatch.setenv("BUILD_COMMIT", "abc12345")
    monkeypatch.setenv("QUICK_AGENT_ID", "agent_test")

    fake_dump = MagicMock(
        return_value={"file_path": "/tmp/x.parquet", "count": 1, "error": ""}
    )
    quick_client = _make_quick_answer_client()
    with (
        patch.dict(sys.modules, {"sf_dump_tool": MagicMock(dump_sf_query=fake_dump)}),
        patch("anthropic.Anthropic", return_value=quick_client),
    ):
        result = sp.run_smoke_probe(persist=False, send_dm=False)
    assert result.passed is False
    assert result.check_a_ok is False
    assert "build" in result.reason


def test_run_smoke_probe_skips_check_c_when_check_b_fails(monkeypatch, tmp_path):
    sp = _import_smoke_probe_fresh()
    pin = tmp_path / "active_versions.json"
    pin.write_text('{"build_commit": "abc12345"}')
    monkeypatch.setattr(sp, "_ACTIVE_VERSIONS_PATH", pin)
    monkeypatch.setenv("BUILD_COMMIT", "abc12345")
    monkeypatch.setenv("QUICK_AGENT_ID", "agent_test")

    fake_dump = MagicMock(
        return_value={"file_path": None, "count": 0, "error": "sf_auth_failed"}
    )
    with patch.dict(sys.modules, {"sf_dump_tool": MagicMock(dump_sf_query=fake_dump)}):
        result = sp.run_smoke_probe(persist=False, send_dm=False)
    assert result.passed is False
    assert result.check_b_ok is False
    # Check C ran but was logged as SKIPPED, not as a real ok/false outcome.
    assert result.check_c_ok is None
    quick = result.check_results.get("quick_answer", {})
    assert "SKIPPED" in (quick.get("detail") or "")


def test_run_smoke_probe_inconclusive_pass_on_anthropic_outage(monkeypatch, tmp_path):
    sp = _import_smoke_probe_fresh()
    pin = tmp_path / "active_versions.json"
    pin.write_text('{"build_commit": "abc12345"}')
    monkeypatch.setattr(sp, "_ACTIVE_VERSIONS_PATH", pin)
    monkeypatch.setenv("BUILD_COMMIT", "abc12345")
    monkeypatch.setenv("QUICK_AGENT_ID", "agent_test")

    fake_dump = MagicMock(
        return_value={"file_path": "/tmp/x.parquet", "count": 1, "error": ""}
    )

    client = MagicMock()
    session = MagicMock()
    session.id = "sesn_EXAMPLE"
    client.beta.sessions.create.return_value = session

    class _FakeServerError(Exception):
        status_code = 503

    client.beta.sessions.events.stream.side_effect = _FakeServerError("503")

    with (
        patch.dict(sys.modules, {"sf_dump_tool": MagicMock(dump_sf_query=fake_dump)}),
        patch("anthropic.Anthropic", return_value=client),
    ):
        result = sp.run_smoke_probe(persist=False, send_dm=False)
    assert result.passed is True, "5xx should be inconclusive PASS"
    assert result.anthropic_status == "unavailable"


# ──────────────────────────────────────────────────────────────────────────
# Persistence
# ──────────────────────────────────────────────────────────────────────────


def test_persist_result_inserts_row_with_correct_columns():
    sp = _import_smoke_probe_fresh()
    fake_cursor = MagicMock()
    fake_conn = MagicMock()
    fake_conn.cursor.return_value.__enter__ = MagicMock(return_value=fake_cursor)
    fake_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    fake_db_adapter = MagicMock()
    fake_db_adapter.DATABASE_URL = "postgres://test"
    fake_db_adapter._connect.return_value = fake_conn

    result = sp.SmokeResult(
        passed=True,
        reason="",
        elapsed_s=3.2,
        check_results={},
        anthropic_status="ok",
        deploy_sha="abc12345",
        check_a_ok=True,
        check_b_ok=True,
        check_c_ok=True,
    )

    with patch.dict(sys.modules, {"db_adapter": fake_db_adapter}):
        sp._persist_result(result)

    fake_cursor.execute.assert_called_once()
    args = fake_cursor.execute.call_args[0]
    sql = args[0]
    params = args[1]
    assert "INSERT INTO smoke_probe_runs" in sql
    # Plan #44 Task #20 — column order now includes check_d_ok between
    # check_c_ok and anthropic_status.
    # (deploy_sha, passed, check_a_ok, check_b_ok, check_c_ok, check_d_ok,
    #  anthropic_status, elapsed_s, reason)
    assert params == (
        "abc12345",
        True,
        True,
        True,
        True,
        None,
        "ok",
        3.2,
        None,
    )
    fake_conn.commit.assert_called_once()


def test_persist_result_silently_skips_when_no_database_url():
    sp = _import_smoke_probe_fresh()
    fake_db_adapter = MagicMock()
    fake_db_adapter.DATABASE_URL = ""
    fake_db_adapter._connect = MagicMock(
        side_effect=AssertionError("should not connect")
    )
    result = sp.SmokeResult(passed=True, reason="", elapsed_s=1.0, check_results={})
    with patch.dict(sys.modules, {"db_adapter": fake_db_adapter}):
        # Must NOT raise even though _connect would explode.
        sp._persist_result(result)


# ──────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────


def test_cli_returns_zero_on_pass(monkeypatch, capsys):
    sp = _import_smoke_probe_fresh()
    monkeypatch.setattr(
        sp,
        "run_smoke_probe",
        MagicMock(
            return_value=sp.SmokeResult(
                passed=True, reason="", elapsed_s=1.0, check_results={}
            )
        ),
    )
    rc = sp.main_cli(["--local", "--no-persist", "--no-dm"])
    assert rc == 0


def test_cli_returns_one_on_fail(monkeypatch):
    sp = _import_smoke_probe_fresh()
    monkeypatch.setattr(
        sp,
        "run_smoke_probe",
        MagicMock(
            return_value=sp.SmokeResult(
                passed=False,
                reason="failed_checks: sf",
                elapsed_s=1.0,
                check_results={},
            )
        ),
    )
    rc = sp.main_cli(["--local", "--no-persist", "--no-dm"])
    assert rc == 1


def test_cli_check_subset_passed_through(monkeypatch):
    sp = _import_smoke_probe_fresh()
    capture = {}

    def _fake_run(*, local_mode, checks, persist, send_dm, level):
        capture["checks"] = checks
        return sp.SmokeResult(passed=True, reason="", elapsed_s=0.0, check_results={})

    monkeypatch.setattr(sp, "run_smoke_probe", _fake_run)
    sp.main_cli(["--check", "sf", "--no-persist", "--no-dm"])
    assert capture["checks"] == ("sf",)


# ──────────────────────────────────────────────────────────────────────────
# Admin DM templates
# ──────────────────────────────────────────────────────────────────────────


def test_admin_dm_pass_template_includes_streak_and_rollback(monkeypatch):
    sp = _import_smoke_probe_fresh()
    result = sp.SmokeResult(
        passed=True,
        reason="",
        elapsed_s=4.0,
        check_results={
            "build_commit": {"ok": True, "detail": "abc12345 == BUILD_COMMIT"},
            "dump_sf_query": {"ok": True, "detail": "1 Account row in 1.0s"},
            "quick_answer": {"ok": True, "detail": "10 tokens, 1.5s"},
        },
        deploy_sha="abc12345",
    )
    summary, detail = sp._render_admin_dm(result, state="pass")
    assert "SMOKE PROBE OK" in summary
    assert "rollback-deploy" in detail


def test_admin_dm_fail_template_lists_failing_check(monkeypatch):
    sp = _import_smoke_probe_fresh()
    result = sp.SmokeResult(
        passed=False,
        reason="failed_checks: sf",
        elapsed_s=20.0,
        check_results={
            "build_commit": {"ok": True, "detail": "abc12345 == BUILD_COMMIT"},
            "dump_sf_query": {
                "ok": False,
                "detail": "sf_auth_failed: bad creds",
                "error": "bad creds",
            },
            "quick_answer": {
                "ok": None,
                "detail": "SKIPPED (Check B failed)",
            },
        },
        deploy_sha="abc12345",
        check_a_ok=True,
        check_b_ok=False,
    )
    summary, detail = sp._render_admin_dm(result, state="fail")
    assert "FAILED" in summary
    assert "sf_auth_failed" in detail
    assert "previous image" in detail


def test_admin_dm_inconclusive_template_links_status_page(monkeypatch):
    sp = _import_smoke_probe_fresh()
    result = sp.SmokeResult(
        passed=True,
        reason="",
        elapsed_s=15.0,
        check_results={
            "build_commit": {"ok": True, "detail": "abc12345 == BUILD_COMMIT"},
            "dump_sf_query": {"ok": True, "detail": "1 Account row in 1.0s"},
            "quick_answer": {
                "ok": True,
                "detail": "Anthropic 503 — inconclusive PASS",
                "anthropic_status": "unavailable",
            },
        },
        anthropic_status="unavailable",
        deploy_sha="abc12345",
    )
    summary, detail = sp._render_admin_dm(result, state="inconclusive")
    assert "INCONCLUSIVE" in summary
    assert "status.anthropic.com" in detail


def test_render_disabled_dm_template_includes_re_enable_instructions(monkeypatch):
    sp = _import_smoke_probe_fresh()
    monkeypatch.setenv("BUILD_COMMIT", "abc12345")
    summary, detail = sp.render_disabled_dm()
    assert "NOT validated" in summary
    assert "Re-enable" in detail
    assert "SMOKE_PROBE_ENABLED=true" in detail


# ──────────────────────────────────────────────────────────────────────────
# Plan #44 Task #20 — SMOKE_PROBE_LEVEL + Check D (multiagent)
# ──────────────────────────────────────────────────────────────────────────


def _make_coordinator_client(*, sentinel="multiagent-ok", events=None):
    """Build a MagicMock anthropic client whose Coordinator returns ``sentinel``.

    Distinct from ``_make_quick_answer_client`` only by default sentinel so
    Check D tests stay readable.
    """
    client = MagicMock()
    session = MagicMock()
    session.id = "sesn_EXAMPLE"
    client.beta.sessions.create.return_value = session

    if events is None:
        events = [
            _FakeEvent(
                "agent.message",
                content=[_FakeAgentBlock(sentinel)],
            ),
            _FakeEvent("session.status_idle"),
        ]
    client.beta.sessions.events.stream.return_value = _FakeStream(events)
    return client


def test_resolve_probe_level_defaults_to_quick_when_unset(monkeypatch):
    sp = _import_smoke_probe_fresh()
    monkeypatch.delenv("SMOKE_PROBE_LEVEL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert sp._resolve_probe_level() == "quick"


def test_resolve_probe_level_reads_env_when_no_db(monkeypatch):
    sp = _import_smoke_probe_fresh()
    monkeypatch.setenv("SMOKE_PROBE_LEVEL", "full")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert sp._resolve_probe_level() == "full"


def test_resolve_probe_level_invalid_value_warns_and_defaults(monkeypatch, caplog):
    sp = _import_smoke_probe_fresh()
    monkeypatch.setenv("SMOKE_PROBE_LEVEL", "nonsense")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with caplog.at_level("WARNING"):
        assert sp._resolve_probe_level() == "quick"
    assert any("invalid SMOKE_PROBE_LEVEL" in r.message for r in caplog.records)


def test_resolve_probe_level_db_override_wins_over_env(monkeypatch):
    """``flag_overrides.get_flag`` is checked first when DATABASE_URL is set."""
    sp = _import_smoke_probe_fresh()
    monkeypatch.setenv("SMOKE_PROBE_LEVEL", "quick")

    fake_flag_overrides = MagicMock()
    fake_flag_overrides.get_flag = MagicMock(return_value="full")
    with patch.dict(sys.modules, {"flag_overrides": fake_flag_overrides}):
        level = sp._resolve_probe_level()
    assert level == "full"
    fake_flag_overrides.get_flag.assert_called_once_with("SMOKE_PROBE_LEVEL", "quick")


# ──────────────────────────────────────────────────────────────────────────
# Check D — Coordinator multiagent
# ──────────────────────────────────────────────────────────────────────────


def test_check_d_passes_when_coordinator_returns_sentinel(monkeypatch):
    sp = _import_smoke_probe_fresh()
    monkeypatch.setenv("COORDINATOR_ID", "agent_coord")
    monkeypatch.setenv("ENVIRONMENT_ID", "env_test")
    client = _make_coordinator_client()
    with patch("anthropic.Anthropic", return_value=client):
        result = sp._check_coordinator_multiagent()
    assert result["ok"] is True
    assert result["anthropic_status"] == "ok"


def test_check_d_fails_when_response_missing_sentinel(monkeypatch):
    sp = _import_smoke_probe_fresh()
    monkeypatch.setenv("COORDINATOR_ID", "agent_coord")
    monkeypatch.setenv("ENVIRONMENT_ID", "env_test")
    client = _make_coordinator_client(sentinel="some-other-text")
    with patch("anthropic.Anthropic", return_value=client):
        result = sp._check_coordinator_multiagent()
    assert result["ok"] is False
    assert "sentinel" in result["error"]


def test_check_d_handles_anthropic_rate_limit_inconclusive(monkeypatch):
    """RateLimitError → ok=True, anthropic_status='rate_limited' (D7)."""
    import anthropic

    sp = _import_smoke_probe_fresh()
    monkeypatch.setenv("COORDINATOR_ID", "agent_coord")
    monkeypatch.setenv("ENVIRONMENT_ID", "env_test")

    client = MagicMock()
    session = MagicMock()
    session.id = "sesn_EXAMPLE"
    client.beta.sessions.create.return_value = session

    def _raise_429(*args, **kwargs):
        err = anthropic.RateLimitError.__new__(anthropic.RateLimitError)
        err.status_code = 429
        raise err

    client.beta.sessions.events.stream.side_effect = _raise_429
    with patch("anthropic.Anthropic", return_value=client):
        result = sp._check_coordinator_multiagent()
    assert result["ok"] is True, "429 should be inconclusive PASS for Check D"
    assert result["anthropic_status"] == "rate_limited"


def test_check_d_handles_anthropic_5xx_inconclusive(monkeypatch):
    sp = _import_smoke_probe_fresh()
    monkeypatch.setenv("COORDINATOR_ID", "agent_coord")
    monkeypatch.setenv("ENVIRONMENT_ID", "env_test")

    client = MagicMock()
    session = MagicMock()
    session.id = "sesn_EXAMPLE"
    client.beta.sessions.create.return_value = session

    class _FakeServerError(Exception):
        status_code = 503

    client.beta.sessions.events.stream.side_effect = _FakeServerError("503")
    with patch("anthropic.Anthropic", return_value=client):
        result = sp._check_coordinator_multiagent()
    assert result["ok"] is True, "5xx should be inconclusive PASS for Check D"
    assert result["anthropic_status"] == "unavailable"


def test_check_d_fails_on_timeout(monkeypatch):
    sp = _import_smoke_probe_fresh()
    monkeypatch.setenv("COORDINATOR_ID", "agent_coord")
    monkeypatch.setenv("ENVIRONMENT_ID", "env_test")

    client = MagicMock()
    session = MagicMock()
    session.id = "sesn_EXAMPLE"
    client.beta.sessions.create.return_value = session

    events = [_FakeEvent("agent.message", content=[]) for _ in range(5)]
    client.beta.sessions.events.stream.return_value = _FakeStream(events)

    # See test_check_c_fails_on_timeout for the monotonic-call cadence.
    monotonic_values = iter([0.0, 0.0, 999.0, 999.0, 999.0, 999.0, 999.0])

    def _fake_monotonic():
        try:
            return next(monotonic_values)
        except StopIteration:
            return 999.0

    with (
        patch("anthropic.Anthropic", return_value=client),
        patch.object(sp.time, "monotonic", side_effect=_fake_monotonic),
    ):
        result = sp._check_coordinator_multiagent(timeout_seconds=30.0)
    assert result["ok"] is False
    assert result["error"] == "timeout"
    assert "30s" in result["detail"]


def test_check_d_fails_when_coordinator_id_unset(monkeypatch):
    sp = _import_smoke_probe_fresh()
    import config as _config

    monkeypatch.setattr(_config, "COORDINATOR_ID", "")
    result = sp._check_coordinator_multiagent()
    assert result["ok"] is False
    assert "COORDINATOR_ID" in result["detail"]


# ──────────────────────────────────────────────────────────────────────────
# run_smoke_probe — level gating
# ──────────────────────────────────────────────────────────────────────────


def test_run_smoke_probe_off_skips_all_checks(monkeypatch):
    """SMOKE_PROBE_LEVEL=off → no checks run; passed=True; reason populated."""
    sp = _import_smoke_probe_fresh()
    monkeypatch.setenv("SMOKE_PROBE_LEVEL", "off")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    # Sentinel patches: if any check function runs, the test should fail
    # loudly. We patch them to raise so a regression in the off-short-circuit
    # is immediately visible.
    def _boom(*args, **kwargs):  # pragma: no cover — never invoked
        raise AssertionError("check should not run at level=off")

    monkeypatch.setattr(sp, "_check_build_commit", _boom)
    monkeypatch.setattr(sp, "_check_dump_sf_query", _boom)
    monkeypatch.setattr(sp, "_check_quick_answer_agent", _boom)
    monkeypatch.setattr(sp, "_check_coordinator_multiagent", _boom)

    result = sp.run_smoke_probe(persist=False, send_dm=False)
    assert result.passed is True
    assert result.reason == "probe_disabled_via_level"
    assert result.probe_level == "off"
    assert result.check_a_ok is None
    assert result.check_b_ok is None
    assert result.check_c_ok is None
    assert result.check_d_ok is None
    assert result.check_results == {}


def test_run_smoke_probe_quick_runs_abc_not_d(monkeypatch, tmp_path):
    """Quick level → A+B+C; Check D never invoked."""
    sp = _import_smoke_probe_fresh()
    pin = tmp_path / "active_versions.json"
    pin.write_text('{"build_commit": "abc12345"}')
    monkeypatch.setattr(sp, "_ACTIVE_VERSIONS_PATH", pin)
    monkeypatch.setenv("BUILD_COMMIT", "abc12345")
    monkeypatch.setenv("SMOKE_PROBE_LEVEL", "quick")
    monkeypatch.setenv("QUICK_AGENT_ID", "agent_test")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    fake_dump = MagicMock(
        return_value={"file_path": "/tmp/x.parquet", "count": 1, "error": ""}
    )
    quick_client = _make_quick_answer_client()

    # Make Check D blow up so we know it didn't run.
    def _boom_d(*args, **kwargs):  # pragma: no cover
        raise AssertionError("Check D must not run at level=quick")

    monkeypatch.setattr(sp, "_check_coordinator_multiagent", _boom_d)

    with (
        patch.dict(sys.modules, {"sf_dump_tool": MagicMock(dump_sf_query=fake_dump)}),
        patch("anthropic.Anthropic", return_value=quick_client),
    ):
        result = sp.run_smoke_probe(persist=False, send_dm=False)
    assert result.passed is True
    assert result.probe_level == "quick"
    assert result.check_a_ok is True
    assert result.check_b_ok is True
    assert result.check_c_ok is True
    assert result.check_d_ok is None
    assert "coordinator_multiagent" not in result.check_results


def test_run_smoke_probe_full_runs_all_four_checks(monkeypatch, tmp_path):
    """Full level → A+B+C+D all run and all pass."""
    sp = _import_smoke_probe_fresh()
    pin = tmp_path / "active_versions.json"
    pin.write_text('{"build_commit": "abc12345"}')
    monkeypatch.setattr(sp, "_ACTIVE_VERSIONS_PATH", pin)
    monkeypatch.setenv("BUILD_COMMIT", "abc12345")
    monkeypatch.setenv("SMOKE_PROBE_LEVEL", "full")
    monkeypatch.setenv("QUICK_AGENT_ID", "agent_quick")
    monkeypatch.setenv("COORDINATOR_ID", "agent_coord")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    fake_dump = MagicMock(
        return_value={"file_path": "/tmp/x.parquet", "count": 1, "error": ""}
    )

    # Both Quick Answer and Coordinator share the same mocked client class —
    # they go through ``anthropic.Anthropic`` once each. Wire a client whose
    # stream returns the right sentinel for whichever session ID is asked
    # for. Simplest: keep one stream that returns BOTH sentinels in one
    # ``agent.message`` block; both Check C and Check D will see their token.
    client = MagicMock()
    session = MagicMock()
    session.id = "sesn_EXAMPLE"
    client.beta.sessions.create.return_value = session
    client.beta.sessions.events.stream.return_value = _FakeStream(
        [
            _FakeEvent(
                "agent.message",
                content=[_FakeAgentBlock("smoke-probe-ok multiagent-ok")],
            ),
            _FakeEvent("session.status_idle"),
        ]
    )

    with (
        patch.dict(sys.modules, {"sf_dump_tool": MagicMock(dump_sf_query=fake_dump)}),
        patch("anthropic.Anthropic", return_value=client),
    ):
        result = sp.run_smoke_probe(persist=False, send_dm=False)
    assert result.passed is True, result.reason
    assert result.probe_level == "full"
    assert result.check_a_ok is True
    assert result.check_b_ok is True
    assert result.check_c_ok is True
    assert result.check_d_ok is True
    assert "coordinator_multiagent" in result.check_results


def test_run_smoke_probe_full_check_d_429_inconclusive(monkeypatch, tmp_path):
    """At full level a Check D 429 → inconclusive PASS, not FAIL."""
    import anthropic

    sp = _import_smoke_probe_fresh()
    pin = tmp_path / "active_versions.json"
    pin.write_text('{"build_commit": "abc12345"}')
    monkeypatch.setattr(sp, "_ACTIVE_VERSIONS_PATH", pin)
    monkeypatch.setenv("BUILD_COMMIT", "abc12345")
    monkeypatch.setenv("SMOKE_PROBE_LEVEL", "full")
    monkeypatch.setenv("QUICK_AGENT_ID", "agent_quick")
    monkeypatch.setenv("COORDINATOR_ID", "agent_coord")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    fake_dump = MagicMock(
        return_value={"file_path": "/tmp/x.parquet", "count": 1, "error": ""}
    )

    # Make Check C pass and Check D raise 429 from the streaming call.
    quick_client = _make_quick_answer_client()

    def _check_d_with_429(*args, **kwargs):
        err = anthropic.RateLimitError.__new__(anthropic.RateLimitError)
        err.status_code = 429
        return {
            "ok": True,
            "detail": "Anthropic rate_limited — inconclusive PASS",
            "response": "",
            "anthropic_status": "rate_limited",
            "elapsed_s": 0.1,
            "error": "429",
        }

    monkeypatch.setattr(sp, "_check_coordinator_multiagent", _check_d_with_429)

    with (
        patch.dict(sys.modules, {"sf_dump_tool": MagicMock(dump_sf_query=fake_dump)}),
        patch("anthropic.Anthropic", return_value=quick_client),
    ):
        result = sp.run_smoke_probe(persist=False, send_dm=False)
    assert result.passed is True
    assert result.anthropic_status == "rate_limited"
    assert result.check_d_ok is True


def test_run_smoke_probe_full_check_d_timeout_fails(monkeypatch, tmp_path):
    """A real Check D timeout (no 429/5xx classification) → passed=False."""
    sp = _import_smoke_probe_fresh()
    pin = tmp_path / "active_versions.json"
    pin.write_text('{"build_commit": "abc12345"}')
    monkeypatch.setattr(sp, "_ACTIVE_VERSIONS_PATH", pin)
    monkeypatch.setenv("BUILD_COMMIT", "abc12345")
    monkeypatch.setenv("SMOKE_PROBE_LEVEL", "full")
    monkeypatch.setenv("QUICK_AGENT_ID", "agent_quick")
    monkeypatch.setenv("COORDINATOR_ID", "agent_coord")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    fake_dump = MagicMock(
        return_value={"file_path": "/tmp/x.parquet", "count": 1, "error": ""}
    )

    quick_client = _make_quick_answer_client()

    def _check_d_timeout(*args, **kwargs):
        return {
            "ok": False,
            "detail": "Coordinator timeout after 30s",
            "response": "",
            "anthropic_status": "ok",
            "elapsed_s": 30.0,
            "error": "timeout",
        }

    monkeypatch.setattr(sp, "_check_coordinator_multiagent", _check_d_timeout)

    with (
        patch.dict(sys.modules, {"sf_dump_tool": MagicMock(dump_sf_query=fake_dump)}),
        patch("anthropic.Anthropic", return_value=quick_client),
    ):
        result = sp.run_smoke_probe(persist=False, send_dm=False)
    assert result.passed is False
    assert "coord" in result.reason
    assert result.check_d_ok is False


def test_run_smoke_probe_full_missing_sentinel_fails(monkeypatch, tmp_path):
    """Check D returns non-empty prose without the multiagent sentinel."""
    sp = _import_smoke_probe_fresh()
    pin = tmp_path / "active_versions.json"
    pin.write_text('{"build_commit": "abc12345"}')
    monkeypatch.setattr(sp, "_ACTIVE_VERSIONS_PATH", pin)
    monkeypatch.setenv("BUILD_COMMIT", "abc12345")
    monkeypatch.setenv("SMOKE_PROBE_LEVEL", "full")
    monkeypatch.setenv("QUICK_AGENT_ID", "agent_quick")
    monkeypatch.setenv("COORDINATOR_ID", "agent_coord")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    fake_dump = MagicMock(
        return_value={"file_path": "/tmp/x.parquet", "count": 1, "error": ""}
    )

    # One client; Check C gets smoke-probe-ok, Check D gets prose that
    # never contains multiagent-ok.
    call_count = {"n": 0}

    def _stream_factory(*args, **kwargs):
        call_count["n"] += 1
        # First stream call = Check C session; second = Check D.
        if call_count["n"] == 1:
            return _FakeStream(
                [
                    _FakeEvent(
                        "agent.message",
                        content=[_FakeAgentBlock("smoke-probe-ok")],
                    ),
                    _FakeEvent("session.status_idle"),
                ]
            )
        return _FakeStream(
            [
                _FakeEvent(
                    "agent.message",
                    content=[
                        _FakeAgentBlock(
                            "I would be happy to assist with that question."
                        )
                    ],
                ),
                _FakeEvent("session.status_idle"),
            ]
        )

    client = MagicMock()
    session = MagicMock()
    session.id = "sesn_EXAMPLE"
    client.beta.sessions.create.return_value = session
    client.beta.sessions.events.stream.side_effect = _stream_factory

    with (
        patch.dict(sys.modules, {"sf_dump_tool": MagicMock(dump_sf_query=fake_dump)}),
        patch("anthropic.Anthropic", return_value=client),
    ):
        result = sp.run_smoke_probe(persist=False, send_dm=False)
    assert result.passed is False
    assert result.check_d_ok is False
    assert "coord" in result.reason
    check_d = result.check_results.get("coordinator_multiagent", {})
    assert check_d.get("error") == "sentinel_missing"


def test_run_smoke_probe_explicit_level_override_wins(monkeypatch, tmp_path):
    """``level=`` kwarg beats the env var so tests / CLI can pin behaviour."""
    sp = _import_smoke_probe_fresh()
    pin = tmp_path / "active_versions.json"
    pin.write_text('{"build_commit": "abc12345"}')
    monkeypatch.setattr(sp, "_ACTIVE_VERSIONS_PATH", pin)
    monkeypatch.setenv("BUILD_COMMIT", "abc12345")
    monkeypatch.setenv("SMOKE_PROBE_LEVEL", "full")  # env says full
    monkeypatch.delenv("DATABASE_URL", raising=False)

    result = sp.run_smoke_probe(persist=False, send_dm=False, level="off")
    # Caller-supplied 'off' wins over env 'full'.
    assert result.probe_level == "off"
    assert result.reason == "probe_disabled_via_level"


def test_run_smoke_probe_invalid_explicit_level_defaults(monkeypatch):
    sp = _import_smoke_probe_fresh()
    monkeypatch.delenv("SMOKE_PROBE_LEVEL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("BUILD_COMMIT", raising=False)

    fake_dump = MagicMock(
        return_value={"file_path": "/tmp/x.parquet", "count": 1, "error": ""}
    )
    quick_client = _make_quick_answer_client()

    with (
        patch.dict(sys.modules, {"sf_dump_tool": MagicMock(dump_sf_query=fake_dump)}),
        patch("anthropic.Anthropic", return_value=quick_client),
    ):
        result = sp.run_smoke_probe(persist=False, send_dm=False, level="garbage")
    # Invalid level → defaults to quick.
    assert result.probe_level == "quick"
    assert result.check_d_ok is None


# ──────────────────────────────────────────────────────────────────────────
# Plan #44 Task #20 — admin DM templates with Check D
# ──────────────────────────────────────────────────────────────────────────


def test_admin_dm_pass_template_includes_check_d_when_present(monkeypatch):
    sp = _import_smoke_probe_fresh()
    result = sp.SmokeResult(
        passed=True,
        reason="",
        elapsed_s=12.0,
        check_results={
            "build_commit": {"ok": True, "detail": "abc12345 == BUILD_COMMIT"},
            "dump_sf_query": {"ok": True, "detail": "1 Account row in 1.0s"},
            "quick_answer": {"ok": True, "detail": "10 tokens, 1.5s"},
            "coordinator_multiagent": {
                "ok": True,
                "detail": "12 tokens, 4.0s",
            },
        },
        deploy_sha="abc12345",
        probe_level="full",
    )
    summary, detail = sp._render_admin_dm(result, state="pass")
    assert "SMOKE PROBE OK" in summary
    assert "level=full" in summary
    assert "Check D" in detail


def test_admin_dm_fail_template_lists_check_d_failure(monkeypatch):
    sp = _import_smoke_probe_fresh()
    result = sp.SmokeResult(
        passed=False,
        reason="failed_checks: coord",
        elapsed_s=40.0,
        check_results={
            "build_commit": {"ok": True, "detail": "abc12345 == BUILD_COMMIT"},
            "dump_sf_query": {"ok": True, "detail": "1 Account row in 1.0s"},
            "quick_answer": {"ok": True, "detail": "10 tokens, 1.5s"},
            "coordinator_multiagent": {
                "ok": False,
                "detail": "Coordinator response missing multiagent sentinel",
                "error": "sentinel_missing",
            },
        },
        deploy_sha="abc12345",
        check_a_ok=True,
        check_b_ok=True,
        check_c_ok=True,
        check_d_ok=False,
        probe_level="full",
    )
    summary, detail = sp._render_admin_dm(result, state="fail")
    assert "FAILED" in summary
    assert "level=full" in summary
    assert "Check D" in detail
    assert "sentinel" in detail


# ──────────────────────────────────────────────────────────────────────────
# CLI — --level + --check coord
# ──────────────────────────────────────────────────────────────────────────


def test_cli_passes_level_arg_through(monkeypatch):
    sp = _import_smoke_probe_fresh()
    capture = {}

    def _fake_run(*, local_mode, checks, persist, send_dm, level):
        capture["level"] = level
        capture["checks"] = checks
        return sp.SmokeResult(passed=True, reason="", elapsed_s=0.0, check_results={})

    monkeypatch.setattr(sp, "run_smoke_probe", _fake_run)
    sp.main_cli(["--level", "full", "--no-persist", "--no-dm"])
    assert capture["level"] == "full"
    # --check defaulted to 'all', which now translates to None so
    # run_smoke_probe derives the selection from level.
    assert capture["checks"] is None


def test_cli_check_coord_is_accepted(monkeypatch):
    sp = _import_smoke_probe_fresh()
    capture = {}

    def _fake_run(*, local_mode, checks, persist, send_dm, level):
        capture["checks"] = checks
        return sp.SmokeResult(passed=True, reason="", elapsed_s=0.0, check_results={})

    monkeypatch.setattr(sp, "run_smoke_probe", _fake_run)
    sp.main_cli(["--check", "coord", "--no-persist", "--no-dm"])
    assert capture["checks"] == ("coord",)


# ──────────────────────────────────────────────────────────────────────────
# Vault attachment + session.error surfacing
#
# Regression coverage for 2026-05-13 incident: smoke probe shipped without
# ``vault_ids`` on ``sessions.create``, causing the Quick Answer agent's
# Salesforce MCP toolset to fail authentication. The session emitted
# ``session.error`` with type ``mcp_authentication_failed_error``, the
# stream closed with no agent text, and Check C reported the misleading
# ``sentinel_missing got: ''``. These tests pin the contract:
#   1. ``sessions.create`` MUST receive ``vault_ids`` derived from
#      ``session_runner.VAULT_IDS`` (or, when session_runner is stubbed,
#      the union of the three vault env vars on _config).
#   2. ``session.error`` MUST be reported with the actual ``error_type``
#      in the result's ``error`` field, not ``sentinel_missing``.
# ──────────────────────────────────────────────────────────────────────────


def test_check_c_passes_vault_ids_to_sessions_create(monkeypatch):
    sp = _import_smoke_probe_fresh()
    monkeypatch.setenv("QUICK_AGENT_ID", "agent_test")
    monkeypatch.setenv("ENVIRONMENT_ID", "env_test")

    # Stub session_runner so the import succeeds and we control VAULT_IDS.
    fake_session_runner = MagicMock()
    fake_session_runner.VAULT_IDS = ["vlt_acme_test", "vlt_kapa_test"]

    client = _make_quick_answer_client()
    with (
        patch.dict(sys.modules, {"session_runner": fake_session_runner}),
        patch("anthropic.Anthropic", return_value=client),
    ):
        sp._check_quick_answer_agent()

    # Check that the kwarg landed on sessions.create with the expected list.
    create_kwargs = client.beta.sessions.create.call_args.kwargs
    assert "vault_ids" in create_kwargs, (
        "smoke probe must forward vault_ids to sessions.create — without it "
        "the MCP toolset can't authenticate and the session dies before any "
        "agent.message is emitted (2026-05-13 incident)."
    )
    assert create_kwargs["vault_ids"] == ["vlt_acme_test", "vlt_kapa_test"]


def test_check_c_vault_ids_fall_back_to_config_env_when_session_runner_unavailable(
    monkeypatch,
):
    """If session_runner can't be imported (e.g. circular import), the probe
    should still attach the vaults via the config env-var fallback."""
    sp = _import_smoke_probe_fresh()
    monkeypatch.setenv("QUICK_AGENT_ID", "agent_test")
    monkeypatch.setenv("ENVIRONMENT_ID", "env_test")

    # Override config attributes after import so the fallback finds them.
    import config as _config

    monkeypatch.setattr(_config, "ACME_VAULT_ID", "vlt_fb_fallback", raising=False)
    monkeypatch.setattr(_config, "SLACK_VAULT_ID", "vlt_slack_fallback", raising=False)

    # Make ``from session_runner import VAULT_IDS`` raise so the except branch
    # is exercised. We stub the module to be a MagicMock without VAULT_IDS,
    # then make attribute access raise.
    class _NoVaultIds:
        def __getattr__(self, name):
            if name == "VAULT_IDS":
                raise ImportError("simulated circular import")
            return MagicMock()

    client = _make_quick_answer_client()
    with (
        patch.dict(sys.modules, {"session_runner": _NoVaultIds()}),
        patch("anthropic.Anthropic", return_value=client),
    ):
        sp._check_quick_answer_agent()

    create_kwargs = client.beta.sessions.create.call_args.kwargs
    # Both SF + Slack vaults land in the list.
    assert create_kwargs["vault_ids"] == ["vlt_fb_fallback", "vlt_slack_fallback"]


def test_check_c_surfaces_session_error_type_instead_of_sentinel_missing(monkeypatch):
    """A session that dies on session.error before emitting agent.message
    should report the underlying error_type (e.g.
    ``mcp_authentication_failed_error``), NOT the downstream
    ``sentinel_missing`` symptom."""
    sp = _import_smoke_probe_fresh()
    monkeypatch.setenv("QUICK_AGENT_ID", "agent_test")
    monkeypatch.setenv("ENVIRONMENT_ID", "env_test")

    class _FakeError:
        type = "mcp_authentication_failed_error"
        message = (
            "MCP server 'salesforce' initialize failed: no credential is "
            "stored for this server URL"
        )

    class _FakeErrorEvent(_FakeEvent):
        def __init__(self):
            super().__init__("session.error")
            self.error = _FakeError()

    client = _make_quick_answer_client(events=[_FakeErrorEvent()])
    with patch("anthropic.Anthropic", return_value=client):
        result = sp._check_quick_answer_agent()

    assert result["ok"] is False
    assert result["error"] == "mcp_authentication_failed_error"
    assert "mcp_authentication_failed_error" in result["detail"]
    # Make doubly sure the downstream sentinel-missing branch did NOT fire.
    assert result["error"] != "sentinel_missing"


def test_check_d_passes_vault_ids_to_sessions_create(monkeypatch):
    sp = _import_smoke_probe_fresh()
    monkeypatch.setenv("COORDINATOR_ID", "agent_coord")
    monkeypatch.setenv("ENVIRONMENT_ID", "env_test")

    fake_session_runner = MagicMock()
    fake_session_runner.VAULT_IDS = ["vlt_acme_test", "vlt_kapa_test"]
    fake_session_runner._resolve_agent_param = MagicMock(return_value="agent_coord")

    client = _make_coordinator_client()
    with (
        patch.dict(sys.modules, {"session_runner": fake_session_runner}),
        patch("anthropic.Anthropic", return_value=client),
    ):
        sp._check_coordinator_multiagent()

    create_kwargs = client.beta.sessions.create.call_args.kwargs
    assert create_kwargs["vault_ids"] == ["vlt_acme_test", "vlt_kapa_test"]


def test_check_d_surfaces_session_error_type_instead_of_sentinel_missing(monkeypatch):
    sp = _import_smoke_probe_fresh()
    monkeypatch.setenv("COORDINATOR_ID", "agent_coord")
    monkeypatch.setenv("ENVIRONMENT_ID", "env_test")

    class _FakeError:
        type = "mcp_authentication_failed_error"
        message = "MCP server 'salesforce' initialize failed"

    class _FakeErrorEvent(_FakeEvent):
        def __init__(self):
            super().__init__("session.error")
            self.error = _FakeError()

    client = _make_coordinator_client(events=[_FakeErrorEvent()])
    with patch("anthropic.Anthropic", return_value=client):
        result = sp._check_coordinator_multiagent()

    assert result["ok"] is False
    assert result["error"] == "mcp_authentication_failed_error"
    assert "mcp_authentication_failed_error" in result["detail"]
