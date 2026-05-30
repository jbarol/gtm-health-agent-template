"""Tests for orchestrator/config.py — env-var parsing and degraded-mode warnings.

Covers Plan #35 (ANTHROPIC_ADMIN_KEY), Plan #36 (BATCH_PROCESSING_ENABLED),
and Plan #37 (COMPRESR_API_KEY + three COMPRESS_*_ENABLED flags).

Tests exercise the parsing helpers directly (parse_bool,
warn_if_enabled_without_key) plus a module-reload check that confirms the
module-level defaults are correct when env vars are unset and that the
degraded-mode warning fires when an enable flag is set without its key.

Run:
    cd orchestrator && python3 -m pytest config_test.py
"""

from __future__ import annotations

import importlib
import logging

import pytest


# ---------------------------------------------------------------------------
# parse_bool
# ---------------------------------------------------------------------------


def test_parse_bool_returns_default_when_unset(monkeypatch):
    monkeypatch.delenv("MY_FLAG", raising=False)
    import config

    assert config.parse_bool("MY_FLAG", default=False) is False
    assert config.parse_bool("MY_FLAG", default=True) is True


@pytest.mark.parametrize(
    "value,expected",
    [
        ("true", True),
        ("TRUE", True),
        ("True", True),
        ("yes", True),
        ("1", True),
        ("on", True),
        ("false", False),
        ("FALSE", False),
        ("False", False),
        ("no", False),
        ("0", False),
        ("off", False),
        ("", False),
    ],
)
def test_parse_bool_recognized_values(monkeypatch, value, expected):
    monkeypatch.setenv("MY_FLAG", value)
    import config

    assert config.parse_bool("MY_FLAG", default=not expected) is expected


def test_parse_bool_unknown_value_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("MY_FLAG", "maybe")
    import config

    assert config.parse_bool("MY_FLAG", default=False) is False
    assert config.parse_bool("MY_FLAG", default=True) is True


def test_parse_bool_strips_whitespace(monkeypatch):
    monkeypatch.setenv("MY_FLAG", "  TRUE  ")
    import config

    assert config.parse_bool("MY_FLAG", default=False) is True


# ---------------------------------------------------------------------------
# warn_if_enabled_without_key
# ---------------------------------------------------------------------------


def test_warn_if_enabled_without_key_force_disables_and_warns(caplog):
    import config

    with caplog.at_level(logging.WARNING, logger=config.logger.name):
        result = config.warn_if_enabled_without_key(
            "MY_KEY", "", "MY_FLAG", flag_value=True
        )

    assert result is False
    assert any("MY_FLAG" in rec.message for rec in caplog.records)
    assert any("MY_KEY" in rec.message for rec in caplog.records)
    assert any("degraded mode" in rec.message for rec in caplog.records)


def test_warn_if_enabled_without_key_silent_when_flag_off(caplog):
    import config

    with caplog.at_level(logging.WARNING, logger=config.logger.name):
        result = config.warn_if_enabled_without_key(
            "MY_KEY", "", "MY_FLAG", flag_value=False
        )

    assert result is False
    assert not caplog.records


def test_warn_if_enabled_without_key_passes_through_when_key_present(caplog):
    import config

    with caplog.at_level(logging.WARNING, logger=config.logger.name):
        result = config.warn_if_enabled_without_key(
            "MY_KEY", "cmp_real_key", "MY_FLAG", flag_value=True
        )

    assert result is True
    assert not caplog.records


# ---------------------------------------------------------------------------
# Module-level defaults (Plan #35, #36, #37)
#
# These tests reload the config module under controlled env to confirm the
# top-level constants behave correctly. Required vars (ANTHROPIC_API_KEY etc.)
# are set to dummy values so require_env() doesn't blow up at import time.
# ---------------------------------------------------------------------------


_REQUIRED_DUMMIES = {
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
}


def _set_required(monkeypatch):
    for key, value in _REQUIRED_DUMMIES.items():
        monkeypatch.setenv(key, value)


def _reload_config():
    return importlib.reload(importlib.import_module("config"))


def test_admin_key_defaults_to_empty_when_unset(monkeypatch, caplog):
    _set_required(monkeypatch)
    monkeypatch.delenv("ANTHROPIC_ADMIN_KEY", raising=False)

    with caplog.at_level(logging.WARNING):
        config = _reload_config()

    assert config.ANTHROPIC_ADMIN_KEY == ""
    # Degraded-mode warning must fire when key is absent.
    assert any(
        "ANTHROPIC_ADMIN_KEY" in rec.message and "degraded mode" in rec.message
        for rec in caplog.records
    )


def test_admin_key_present_no_warning(monkeypatch, caplog):
    _set_required(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_ADMIN_KEY", "sk-ant-admin-real")

    with caplog.at_level(logging.WARNING):
        config = _reload_config()

    assert config.ANTHROPIC_ADMIN_KEY == "sk-ant-admin-real"
    assert not any(
        "ANTHROPIC_ADMIN_KEY" in rec.message and "degraded mode" in rec.message
        for rec in caplog.records
    )


def test_batch_processing_disabled_by_default(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.delenv("BATCH_PROCESSING_ENABLED", raising=False)

    config = _reload_config()

    assert config.BATCH_PROCESSING_ENABLED is False


def test_batch_processing_enabled_when_true(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("BATCH_PROCESSING_ENABLED", "true")

    config = _reload_config()

    assert config.BATCH_PROCESSING_ENABLED is True


def test_batch_processing_explicit_false(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("BATCH_PROCESSING_ENABLED", "false")

    config = _reload_config()

    assert config.BATCH_PROCESSING_ENABLED is False


def test_compresr_defaults_all_disabled(monkeypatch):
    _set_required(monkeypatch)
    for key in (
        "COMPRESR_API_KEY",
        "COMPRESS_SELF_HEAL_ENABLED",
        "COMPRESS_SELF_IMPROVE_ENABLED",
        "COMPRESS_ADHOC_KICKOFF",
    ):
        monkeypatch.delenv(key, raising=False)

    config = _reload_config()

    assert config.COMPRESR_API_KEY == ""
    assert config.COMPRESS_SELF_HEAL_ENABLED is False
    assert config.COMPRESS_SELF_IMPROVE_ENABLED is False
    assert config.COMPRESS_ADHOC_KICKOFF is False


def test_compresr_self_heal_force_disabled_without_key(monkeypatch, caplog):
    _set_required(monkeypatch)
    monkeypatch.delenv("COMPRESR_API_KEY", raising=False)
    monkeypatch.setenv("COMPRESS_SELF_HEAL_ENABLED", "true")
    monkeypatch.delenv("COMPRESS_SELF_IMPROVE_ENABLED", raising=False)
    monkeypatch.delenv("COMPRESS_ADHOC_KICKOFF", raising=False)

    with caplog.at_level(logging.WARNING):
        config = _reload_config()

    assert config.COMPRESS_SELF_HEAL_ENABLED is False
    assert any(
        "COMPRESS_SELF_HEAL_ENABLED" in rec.message
        and "COMPRESR_API_KEY" in rec.message
        for rec in caplog.records
    )


def test_compresr_self_improve_force_disabled_without_key(monkeypatch, caplog):
    _set_required(monkeypatch)
    monkeypatch.delenv("COMPRESR_API_KEY", raising=False)
    monkeypatch.setenv("COMPRESS_SELF_IMPROVE_ENABLED", "true")
    monkeypatch.delenv("COMPRESS_SELF_HEAL_ENABLED", raising=False)
    monkeypatch.delenv("COMPRESS_ADHOC_KICKOFF", raising=False)

    with caplog.at_level(logging.WARNING):
        config = _reload_config()

    assert config.COMPRESS_SELF_IMPROVE_ENABLED is False
    assert any(
        "COMPRESS_SELF_IMPROVE_ENABLED" in rec.message
        and "COMPRESR_API_KEY" in rec.message
        for rec in caplog.records
    )


def test_compresr_adhoc_force_disabled_without_key(monkeypatch, caplog):
    _set_required(monkeypatch)
    monkeypatch.delenv("COMPRESR_API_KEY", raising=False)
    monkeypatch.setenv("COMPRESS_ADHOC_KICKOFF", "true")
    monkeypatch.delenv("COMPRESS_SELF_HEAL_ENABLED", raising=False)
    monkeypatch.delenv("COMPRESS_SELF_IMPROVE_ENABLED", raising=False)

    with caplog.at_level(logging.WARNING):
        config = _reload_config()

    assert config.COMPRESS_ADHOC_KICKOFF is False
    assert any(
        "COMPRESS_ADHOC_KICKOFF" in rec.message and "COMPRESR_API_KEY" in rec.message
        for rec in caplog.records
    )


def test_compresr_all_enabled_with_key(monkeypatch, caplog):
    _set_required(monkeypatch)
    monkeypatch.setenv("COMPRESR_API_KEY", "cmp_real_test_key")
    monkeypatch.setenv("COMPRESS_SELF_HEAL_ENABLED", "true")
    monkeypatch.setenv("COMPRESS_SELF_IMPROVE_ENABLED", "true")
    monkeypatch.setenv("COMPRESS_ADHOC_KICKOFF", "true")

    with caplog.at_level(logging.WARNING):
        config = _reload_config()

    assert config.COMPRESR_API_KEY == "cmp_real_test_key"
    assert config.COMPRESS_SELF_HEAL_ENABLED is True
    assert config.COMPRESS_SELF_IMPROVE_ENABLED is True
    assert config.COMPRESS_ADHOC_KICKOFF is True
    # No Compresr-scoped warnings expected (other warnings, e.g. for an unset
    # ANTHROPIC_ADMIN_KEY, are out of scope for this test).
    compresr_warnings = [rec for rec in caplog.records if "COMPRESS_" in rec.message]
    assert compresr_warnings == []


# ---------------------------------------------------------------------------
# Plan #44 decision row #18 — QUICK_AGENT_ID vs QUICK_ANSWER_ID naming
# clash. config.py reads BOTH env-var names with a backward-compat
# fallback so an environment that names the variable either way keeps
# working without surgery. The CI deploy workflow uses QUICK_ANSWER_ID
# (matching the verify gate); setup_agents.py prints QUICK_AGENT_ID.
# ---------------------------------------------------------------------------


def test_quick_agent_id_resolves_from_canonical_name(monkeypatch):
    """When only QUICK_AGENT_ID is set, that's the value used."""
    _set_required(monkeypatch)
    monkeypatch.setenv("QUICK_AGENT_ID", "agent_test_canonical")
    monkeypatch.delenv("QUICK_ANSWER_ID", raising=False)

    config = _reload_config()

    assert config.QUICK_AGENT_ID == "agent_test_canonical"


def test_quick_agent_id_resolves_from_legacy_name(monkeypatch):
    """When only QUICK_ANSWER_ID is set, the back-compat fallback fires.

    The CI deploy workflow writes ``QUICK_ANSWER_ID`` into the synthesized
    .env (the verify gate downstream also uses that name). Without the
    fallback the module would crash at import. Closes Plan #44 decision
    row #18 review concern HIGH #1.
    """
    _set_required(monkeypatch)
    # setenv("") rather than delenv: config.py's module-level dotenv block
    # runs os.environ.setdefault(...) on every importlib.reload, which would
    # restore the .env value of QUICK_AGENT_ID and undo a delenv. Setting an
    # empty string keeps the key present (setdefault is a no-op) AND falsy
    # (so the `or` fallback to QUICK_ANSWER_ID fires). Audit finding #2.
    monkeypatch.setenv("QUICK_AGENT_ID", "")
    monkeypatch.setenv("QUICK_ANSWER_ID", "agent_test_legacy")

    config = _reload_config()

    assert config.QUICK_AGENT_ID == "agent_test_legacy"


def test_quick_agent_id_canonical_wins_when_both_set(monkeypatch):
    """When BOTH are set, QUICK_AGENT_ID takes precedence.

    Keeps the canonical name as the source of truth so a future deploy
    can sunset QUICK_ANSWER_ID without behavior changing. The fallback
    is a compat shim, not a co-equal alias.
    """
    _set_required(monkeypatch)
    monkeypatch.setenv("QUICK_AGENT_ID", "agent_canonical_wins")
    monkeypatch.setenv("QUICK_ANSWER_ID", "agent_legacy_loses")

    config = _reload_config()

    assert config.QUICK_AGENT_ID == "agent_canonical_wins"


def test_quick_agent_id_missing_both_raises(monkeypatch):
    """Both env vars absent → RuntimeError with a message naming both names."""
    _set_required(monkeypatch)
    # See note above on setenv("") vs delenv — same dotenv setdefault leak.
    monkeypatch.setenv("QUICK_AGENT_ID", "")
    monkeypatch.setenv("QUICK_ANSWER_ID", "")

    with pytest.raises(RuntimeError) as excinfo:
        _reload_config()

    err_msg = str(excinfo.value)
    assert "QUICK_AGENT_ID" in err_msg, "Error must name the canonical env var"
    assert "QUICK_ANSWER_ID" in err_msg, (
        "Error must also mention the back-compat fallback so the operator "
        "knows both names were checked"
    )


# Plan #44 Task #8 — AGENT_VERSIONS pin file
# ---------------------------------------------------------------------------


def test_agent_versions_loads_from_pin_file(monkeypatch, tmp_path):
    """A POPULATED pin file round-trips through _load_agent_versions().

    This template ships agents/active_versions.json as an empty object ({}) on
    purpose: a fresh fork's freshly-provisioned agents are all version 1, and
    pinning to anything else 404s the first session ("agent.version N not
    found"); shipping no pins makes every session resolve to the agent's latest
    version, which is always correct for a fork. We therefore test the LOADER
    mechanism against a fixture rather than asserting the (empty) shipped file
    carries specific pins."""
    _set_required(monkeypatch)
    import config as _cfg

    pin = tmp_path / "active_versions.json"
    pin.write_text('{"coordinator": 12, "writing_agent": 3}')
    monkeypatch.setattr(_cfg, "_ACTIVE_VERSIONS_PATH", pin)
    loaded = _cfg._load_agent_versions()
    assert loaded == {"coordinator": 12, "writing_agent": 3}
    assert isinstance(loaded["coordinator"], int)

    # And the SHIPPED file must always parse to a dict (empty is fine).
    config = _reload_config()
    assert isinstance(config.AGENT_VERSIONS, dict)
    assert all(isinstance(v, int) for v in config.AGENT_VERSIONS.values())


def test_agent_versions_tolerates_missing_file(monkeypatch, tmp_path):
    """When the pin file path is absent, _load_agent_versions returns {}."""
    _set_required(monkeypatch)
    import config as _cfg

    # Point the pin path at a directory that does not contain the file.
    monkeypatch.setattr(_cfg, "_ACTIVE_VERSIONS_PATH", tmp_path / "does_not_exist.json")
    assert _cfg._load_agent_versions() == {}


def test_agent_versions_tolerates_malformed_json(monkeypatch, tmp_path, caplog):
    """Malformed JSON in the pin file produces an empty dict and a logged exception."""
    _set_required(monkeypatch)
    bad_pin = tmp_path / "active_versions.json"
    bad_pin.write_text("{not valid json")
    import config as _cfg

    monkeypatch.setattr(_cfg, "_ACTIVE_VERSIONS_PATH", bad_pin)
    with caplog.at_level(logging.WARNING):
        result = _cfg._load_agent_versions()
    assert result == {}


def test_agent_versions_coerces_non_int_values(monkeypatch, tmp_path):
    """Non-int values in the pin file are silently dropped."""
    _set_required(monkeypatch)
    pin = tmp_path / "active_versions.json"
    pin.write_text('{"coordinator": 35, "writing_agent": "v3", "broken": null}')
    import config as _cfg

    monkeypatch.setattr(_cfg, "_ACTIVE_VERSIONS_PATH", pin)
    result = _cfg._load_agent_versions()
    assert result == {"coordinator": 35}


# ---------------------------------------------------------------------------
# Plan #44 Task #22 — MCP_AUTO_APPROVE_ALLOWLIST
# ---------------------------------------------------------------------------


def test_mcp_auto_approve_allowlist_defaults_empty(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.delenv("MCP_AUTO_APPROVE_ALLOWLIST", raising=False)
    config = _reload_config()
    assert config.MCP_AUTO_APPROVE_ALLOWLIST == set()


def test_mcp_auto_approve_allowlist_parses_csv(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("MCP_AUTO_APPROVE_ALLOWLIST", "sf_vault, slack_vault ,  ")
    config = _reload_config()
    assert config.MCP_AUTO_APPROVE_ALLOWLIST == {"sf_vault", "slack_vault"}
