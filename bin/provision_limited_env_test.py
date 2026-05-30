"""Tests for ``bin/provision-limited-env.py`` (Plan #44 Task #11).

The script's filename has a hyphen, so we load it by path via ``importlib.util``.
Tests mock the Anthropic SDK; no network or DB side effects.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "bin" / "provision-limited-env.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("provision_limited_env", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod():
    return _load_module()


def test_build_config_shape_matches_docs(mod):
    """The payload sent to environments.create must use limited networking
    with HTTPS-prefixed allowed_hosts and the documented toggles."""
    cfg = mod._build_config()

    assert cfg["type"] == "cloud"
    assert cfg["networking"]["type"] == "limited"
    assert isinstance(cfg["networking"]["allowed_hosts"], list)
    assert len(cfg["networking"]["allowed_hosts"]) >= 8

    for host in cfg["networking"]["allowed_hosts"]:
        assert host.startswith("https://"), (
            f"Allowlist entry {host!r} must be HTTPS-prefixed per docs"
        )

    # The expanded allowlist from Plan #44 decision row #8.
    hosts = set(cfg["networking"]["allowed_hosts"])
    assert "https://api.anthropic.com" in hosts
    assert "https://files.api.anthropic.com" in hosts
    assert "https://*.slack.com" in hosts
    assert "https://wss-primary.slack.com" in hosts
    # SOAP username/password default host (session_runner.py SOAP fallback).
    assert "https://login.salesforce.com" in hosts
    assert "https://*.salesforce.com" in hosts
    assert "https://*.my.salesforce.com" in hosts
    assert "https://quickchart.io" in hosts
    assert "https://api.compresr.com" in hosts
    assert "https://api.github.com" in hosts
    # pypi.org / files.pythonhosted.org are intentionally OMITTED because
    # ``allow_package_managers=false`` blocks pip at the sandbox layer.
    assert "https://files.pythonhosted.org" not in hosts
    assert "https://pypi.org" not in hosts

    # Bundle D's vault SF MCP path may need this; keep allow_mcp_servers=True
    # until that decision is locked.
    assert cfg["allow_mcp_servers"] is True
    # Sandbox attack-surface reduction: deny runtime pip/etc since we
    # pre-install the packages we need.
    assert cfg["allow_package_managers"] is False
    # Pre-installed pip packages mirror agents/setup_agents.py.
    assert "pandas" in cfg["packages"]["pip"]
    assert "openpyxl" in cfg["packages"]["pip"]


def test_dry_run_returns_none_and_skips_api(mod, capsys):
    """``provision(apply=False)`` prints the payload and returns None with
    no client call — safe to run anywhere."""
    client = MagicMock()
    result = mod.provision(apply=False, client=client)
    assert result is None
    client.beta.environments.create.assert_not_called()

    out = capsys.readouterr().out
    assert "networking.type=limited" in out
    assert "DRY-RUN" in out


def test_apply_calls_environments_create_with_limited_payload(mod):
    """``provision(apply=True)`` calls ``client.beta.environments.create`` with
    the correct name + config and returns the env id."""
    client = MagicMock()
    client.beta.environments.create.return_value = SimpleNamespace(id="env_LIMITED_xyz")

    result = mod.provision(name="gtm-health-env-limited", apply=True, client=client)

    assert result == "env_LIMITED_xyz"
    client.beta.environments.create.assert_called_once()
    call_kwargs = client.beta.environments.create.call_args.kwargs
    assert call_kwargs["name"] == "gtm-health-env-limited"
    cfg = call_kwargs["config"]
    assert cfg["networking"]["type"] == "limited"
    assert all(h.startswith("https://") for h in cfg["networking"]["allowed_hosts"])


def test_apply_with_custom_name_passes_through(mod):
    """``--name`` flag wires through to environments.create."""
    client = MagicMock()
    client.beta.environments.create.return_value = SimpleNamespace(id="env_X")

    mod.provision(name="gtm-health-env-limited-v2", apply=True, client=client)

    call_kwargs = client.beta.environments.create.call_args.kwargs
    assert call_kwargs["name"] == "gtm-health-env-limited-v2"


def test_main_dry_run_returns_zero(mod, monkeypatch):
    """``main()`` without ``--apply`` returns 0 and does not import the SDK."""
    # Ensure no .env is loaded by pointing dotenv elsewhere.
    monkeypatch.setattr(mod, "_load_env", lambda: None)

    rc = mod.main([])
    assert rc == 0


def test_main_apply_passes_args_through(mod, monkeypatch):
    """``main(['--apply', '--name', X])`` wires through to provision()."""
    monkeypatch.setattr(mod, "_load_env", lambda: None)

    fake_client = MagicMock()
    fake_client.beta.environments.create.return_value = SimpleNamespace(id="env_TEST")
    monkeypatch.setattr(mod, "_build_client", lambda: fake_client)

    rc = mod.main(["--apply", "--name", "custom-env-name"])

    assert rc == 0
    fake_client.beta.environments.create.assert_called_once()
    call_kwargs = fake_client.beta.environments.create.call_args.kwargs
    assert call_kwargs["name"] == "custom-env-name"


def test_help_references_runbook(mod, capsys):
    """``--help`` mentions the conformance runbook so operators can find it."""
    with pytest.raises(SystemExit):
        mod.main(["--help"])
    out = capsys.readouterr().out
    assert "managed-agents-conformance" in out


def test_print_next_steps_includes_env_id_and_railway_var(mod, capsys):
    """The next-steps block names the env id and the Railway variable."""
    mod._print_next_steps("env_NEW_123", "gtm-health-env-limited")
    out = capsys.readouterr().out
    assert "env_NEW_123" in out
    assert "ENVIRONMENT_ID_LIMITED" in out
    assert "LIMITED_NETWORKING_SHADOW_PCT" in out
    assert "managed-agents-conformance" in out


def test_apply_and_dry_run_are_mutually_exclusive(mod, capsys, monkeypatch):
    """Passing both ``--apply`` and ``--dry-run`` is a hard argparse error,
    not a silent ``--apply`` win. This closes the dead-code review concern
    where ``args.dry_run`` was previously never referenced by ``provision()``.
    """
    monkeypatch.setattr(mod, "_load_env", lambda: None)
    with pytest.raises(SystemExit) as excinfo:
        mod.main(["--apply", "--dry-run"])
    # argparse exits with code 2 on argument-validation failures.
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "not allowed with argument" in err or "mutually exclusive" in err


def test_explicit_dry_run_skips_api_call(mod, monkeypatch):
    """``--dry-run`` explicitly (alone, no ``--apply``) is the safe path:
    return 0, do not import the SDK, do not call environments.create."""
    monkeypatch.setattr(mod, "_load_env", lambda: None)

    fake_client = MagicMock()
    monkeypatch.setattr(mod, "_build_client", lambda: fake_client)

    rc = mod.main(["--dry-run"])

    assert rc == 0
    fake_client.beta.environments.create.assert_not_called()


def test_no_flags_defaults_to_dry_run(mod, monkeypatch):
    """No flags == dry-run. ``provision()`` must NOT call environments.create."""
    monkeypatch.setattr(mod, "_load_env", lambda: None)

    fake_client = MagicMock()
    monkeypatch.setattr(mod, "_build_client", lambda: fake_client)

    rc = mod.main([])

    assert rc == 0
    fake_client.beta.environments.create.assert_not_called()
