"""Tests for ``bin/add-sf-vault-credential.py`` (Plan #44 Task #17).

The script's filename has hyphens so we load it by path via
``importlib.util``. All Anthropic network calls are mocked — no vault
side effects.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "bin" / "add-sf-vault-credential.py"


def _load_script():
    """Load ``bin/add-sf-vault-credential.py`` by path (hyphen in filename)."""
    for p in (REPO_ROOT / "agents", REPO_ROOT / "orchestrator"):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    spec = importlib.util.spec_from_file_location(
        "add_sf_vault_credential", SCRIPT_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def script_mod():
    return _load_script()


@pytest.fixture()
def populated_env(monkeypatch):
    """Set every required SF env var so _build_auth_payload succeeds."""
    monkeypatch.setenv("ACME_VAULT_ID", "vlt_test123")
    monkeypatch.setenv("SF_ACME_CLIENT_ID", "3MVG9_test_client_id")
    monkeypatch.setenv("SF_ACME_CLIENT_SECRET", "test_client_secret_value")
    monkeypatch.setenv("SF_ACME_REFRESH_TOKEN", "5Aep_test_refresh_token")
    monkeypatch.setenv("SF_ACME_ACCESS_TOKEN", "test_access_token_value")
    monkeypatch.setenv(
        "SF_ACME_TOKEN_ENDPOINT",
        "https://login.salesforce.com/services/oauth2/token",
    )
    monkeypatch.setenv("SF_ACME_SCOPE", "api refresh_token")


def _make_client(
    *, credential_id: str = "cred_test_abc", validation_status: str = "valid"
):
    """Build a MagicMock Anthropic client mimicking vaults.credentials shape."""
    client = MagicMock(name="anthropic.Anthropic")
    created = SimpleNamespace(
        id=credential_id,
        type="mcp_oauth",
        display_name="Salesforce — Acme (mcp_oauth)",
    )
    validation = SimpleNamespace(
        status=validation_status,
        valid=(validation_status == "valid"),
        diagnostics={"message": f"validation returned {validation_status}"},
    )
    client.beta.vaults.credentials.create.return_value = created
    client.beta.vaults.credentials.mcp_oauth_validate.return_value = validation
    return client


# ---------------------------------------------------------------------------
# Payload assembly
# ---------------------------------------------------------------------------


def test_build_auth_payload_assembles_full_mcp_oauth_shape(script_mod, populated_env):
    """Every required field is wired into the mcp_oauth payload."""
    payload = script_mod._build_auth_payload()
    assert payload["type"] == "mcp_oauth"
    assert payload["access_token"] == "test_access_token_value"
    assert "mcp_server_url" in payload

    refresh = payload["refresh"]
    assert refresh["client_id"] == "3MVG9_test_client_id"
    assert refresh["refresh_token"] == "5Aep_test_refresh_token"
    assert refresh["token_endpoint"].endswith("/services/oauth2/token")
    assert refresh["scope"] == "api refresh_token"

    auth = refresh["token_endpoint_auth"]
    assert auth["type"] == "client_secret_post"
    assert auth["client_secret"] == "test_client_secret_value"


def test_build_auth_payload_raises_on_missing_envs(script_mod, monkeypatch):
    """Missing any of the four required secrets aborts with a clear message."""
    monkeypatch.delenv("SF_ACME_CLIENT_ID", raising=False)
    monkeypatch.delenv("SF_ACME_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("SF_ACME_REFRESH_TOKEN", raising=False)
    monkeypatch.delenv("SF_ACME_ACCESS_TOKEN", raising=False)

    with pytest.raises(SystemExit) as exc_info:
        script_mod._build_auth_payload()

    msg = str(exc_info.value)
    assert "Missing required env vars" in msg
    assert "SF_ACME_CLIENT_ID" in msg
    assert "SF_ACME_REFRESH_TOKEN" in msg


def test_build_auth_payload_uses_defaults_for_optional_envs(script_mod, monkeypatch):
    """Token endpoint + scope default when env vars are absent."""
    monkeypatch.setenv("SF_ACME_CLIENT_ID", "cid")
    monkeypatch.setenv("SF_ACME_CLIENT_SECRET", "csec")
    monkeypatch.setenv("SF_ACME_REFRESH_TOKEN", "rt")
    monkeypatch.setenv("SF_ACME_ACCESS_TOKEN", "at")
    monkeypatch.delenv("SF_ACME_TOKEN_ENDPOINT", raising=False)
    monkeypatch.delenv("SF_ACME_SCOPE", raising=False)
    monkeypatch.delenv("SF_MCP_SERVER_URL", raising=False)

    payload = script_mod._build_auth_payload()
    assert payload["mcp_server_url"] == script_mod.DEFAULT_SF_MCP_SERVER_URL
    assert payload["refresh"]["token_endpoint"] == script_mod.DEFAULT_SF_TOKEN_ENDPOINT
    assert payload["refresh"]["scope"] == script_mod.DEFAULT_SF_SCOPE


def test_redact_for_print_replaces_secrets(script_mod, populated_env):
    """Output suitable for ops paste-into-ticket — no raw secrets."""
    payload = script_mod._build_auth_payload()
    redacted = script_mod._redact_for_print(payload)
    assert redacted["access_token"] == "[REDACTED]"
    assert redacted["refresh"]["refresh_token"] == "[REDACTED]"
    assert redacted["refresh"]["token_endpoint_auth"]["client_secret"] == "[REDACTED]"
    # Non-secret fields still pass through.
    assert redacted["refresh"]["client_id"] == "3MVG9_test_client_id"


# ---------------------------------------------------------------------------
# Dry-run / apply orchestration
# ---------------------------------------------------------------------------


def test_dry_run_does_not_call_client(script_mod, populated_env, capsys):
    """--dry-run (default) → no network calls, redacted payload printed."""
    client = _make_client()
    rc = script_mod.add_credential(
        vault_id="vlt_test123",
        apply=False,
        client=client,
    )
    assert rc == 0
    client.beta.vaults.credentials.create.assert_not_called()
    client.beta.vaults.credentials.mcp_oauth_validate.assert_not_called()

    captured = capsys.readouterr()
    assert "DRY-RUN" in captured.out
    assert "[REDACTED]" in captured.out
    # Real secret values must NOT appear in the printed body.
    assert "test_client_secret_value" not in captured.out
    assert "5Aep_test_refresh_token" not in captured.out


def test_apply_creates_credential_then_validates(script_mod, populated_env, capsys):
    """--apply → exactly one create + one validate, in that order, with the
    create's returned id passed to mcp_oauth_validate.
    """
    client = _make_client(credential_id="cred_xyz_999")
    rc = script_mod.add_credential(
        vault_id="vlt_test123",
        apply=True,
        client=client,
    )
    assert rc == 0

    # credentials.create was called once with vault_id + auth payload.
    client.beta.vaults.credentials.create.assert_called_once()
    create_kwargs = client.beta.vaults.credentials.create.call_args.kwargs
    assert create_kwargs["vault_id"] == "vlt_test123"
    assert create_kwargs["auth"]["type"] == "mcp_oauth"
    assert "display_name" in create_kwargs

    # mcp_oauth_validate received the new credential id + vault_id.
    client.beta.vaults.credentials.mcp_oauth_validate.assert_called_once()
    validate_args = client.beta.vaults.credentials.mcp_oauth_validate.call_args
    assert validate_args.args[0] == "cred_xyz_999"
    assert validate_args.kwargs["vault_id"] == "vlt_test123"

    # Next-steps block printed verbatim.
    captured = capsys.readouterr()
    assert "Next steps" in captured.out
    assert "cred_xyz_999" in captured.out
    assert "SF_MCP_VIA_VAULT" in captured.out
    assert "update_subagent_tools.py" in captured.out


def test_apply_prints_validation_diagnostic_verbatim(script_mod, populated_env, capsys):
    """The validation result is printed before the Next-steps block."""
    client = _make_client(validation_status="valid")
    script_mod.add_credential(vault_id="vlt_test123", apply=True, client=client)
    out = capsys.readouterr().out
    assert "mcp_oauth_validate diagnostic" in out
    assert "valid" in out


def test_apply_reports_failure_when_create_raises(script_mod, populated_env, capsys):
    """create() raising → exit code 1 and validate is NOT called."""
    client = _make_client()
    client.beta.vaults.credentials.create.side_effect = RuntimeError(
        "simulated API failure"
    )
    rc = script_mod.add_credential(vault_id="vlt_test123", apply=True, client=client)
    assert rc == 1
    client.beta.vaults.credentials.mcp_oauth_validate.assert_not_called()
    assert "credentials.create raised" in capsys.readouterr().out


def test_apply_reports_failure_when_validate_raises(script_mod, populated_env, capsys):
    """validate() raising → exit code 1, but credential is already created."""
    client = _make_client()
    client.beta.vaults.credentials.mcp_oauth_validate.side_effect = RuntimeError(
        "validation API failure"
    )
    rc = script_mod.add_credential(vault_id="vlt_test123", apply=True, client=client)
    assert rc == 1
    client.beta.vaults.credentials.create.assert_called_once()
    out = capsys.readouterr().out
    assert "mcp_oauth_validate raised" in out
    assert "credential was created but validation failed" in out


def test_missing_vault_id_raises_friendly_error(script_mod, populated_env):
    """No --vault-id and no ACME_VAULT_ID → SystemExit with hint."""
    with pytest.raises(SystemExit) as exc_info:
        script_mod.add_credential(vault_id="", apply=False, client=None)
    assert "Vault ID is required" in str(exc_info.value)


# ---------------------------------------------------------------------------
# CLI parser shape
# ---------------------------------------------------------------------------


def test_argparse_help_references_runbook(script_mod, capsys):
    """--help mentions the runbook (the script's contract per task spec)."""
    with pytest.raises(SystemExit):
        script_mod.main(["--help"])
    out = capsys.readouterr().out
    assert "managed-agents-conformance.md" in out


def test_argparse_apply_flag_overrides_dry_run(script_mod, populated_env, monkeypatch):
    """`--apply` triggers the real path; without it, dry-run wins."""
    captured_calls = []

    def fake_add_credential(*, vault_id, apply, display_name):
        captured_calls.append({"vault_id": vault_id, "apply": apply})
        return 0

    monkeypatch.setattr(script_mod, "add_credential", fake_add_credential)
    monkeypatch.setattr(script_mod, "_load_env", lambda: None)

    # No --apply → apply=False
    rc = script_mod.main(["--vault-id", "vlt_test123"])
    assert rc == 0
    assert captured_calls[-1] == {"vault_id": "vlt_test123", "apply": False}

    # With --apply → apply=True
    rc = script_mod.main(["--vault-id", "vlt_test123", "--apply"])
    assert rc == 0
    assert captured_calls[-1] == {"vault_id": "vlt_test123", "apply": True}


def test_argparse_rejects_both_dry_run_and_apply(script_mod, capsys):
    """Passing both --dry-run and --apply is a hard argparse error.

    Previously --dry-run had default=True and --apply silently won when
    both flags were passed. The mutually exclusive group makes the
    ambiguity explicit so ops can't accidentally pass both during a
    handoff and miss which path actually ran.
    """
    with pytest.raises(SystemExit) as exc_info:
        script_mod.main(["--vault-id", "vlt_test123", "--dry-run", "--apply"])
    # argparse exits with code 2 on usage errors.
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "not allowed with argument" in err or "mutually exclusive" in err


# ---------------------------------------------------------------------------
# Validation response redaction (MEDIUM #3)
# ---------------------------------------------------------------------------


def test_validation_to_dict_redacts_refresh_token(script_mod):
    """The mcp_oauth_validate response may echo the credential back —
    every sensitive key must be redacted before _validation_to_dict
    returns.
    """
    raw = SimpleNamespace(
        status="valid",
        valid=True,
        refresh_token="5Aep_LIVE_refresh_token_must_not_leak",
        access_token="LIVE_access_token_must_not_leak",
        client_secret="LIVE_client_secret_must_not_leak",
        diagnostics={
            "message": "ok",
            "credential": {
                "refresh_token": "nested_refresh_token_must_not_leak",
                "client_secret": "nested_client_secret_must_not_leak",
                "access_token": "nested_access_token_must_not_leak",
            },
        },
    )
    out = script_mod._validation_to_dict(raw)
    # Top-level secret values redacted.
    assert out["refresh_token"] == "[REDACTED]"
    assert out["access_token"] == "[REDACTED]"
    assert out["client_secret"] == "[REDACTED]"
    # Nested secret values redacted (recursive walk).
    cred = out["diagnostics"]["credential"]
    assert cred["refresh_token"] == "[REDACTED]"
    assert cred["client_secret"] == "[REDACTED]"
    assert cred["access_token"] == "[REDACTED]"
    # Non-secret fields pass through.
    assert out["status"] == "valid"
    assert out["valid"] is True


def test_validation_to_dict_model_dump_path_redacts(script_mod):
    """When the SDK returns a Pydantic-shaped object (model_dump), the
    redact pass still applies."""

    class _FakeValidation:
        def model_dump(self, exclude_none=True):
            return {
                "status": "valid",
                "refresh_token": "model_dump_refresh_token_LEAK",
                "details": {
                    "password": "nested_password_LEAK",
                },
            }

    out = script_mod._validation_to_dict(_FakeValidation())
    assert out["refresh_token"] == "[REDACTED]"
    assert out["details"]["password"] == "[REDACTED]"
    assert out["status"] == "valid"


def test_apply_does_not_print_refresh_token_from_validation(
    script_mod, populated_env, capsys
):
    """End-to-end: when the SDK echoes a refresh_token in the validation
    response, the printed diagnostic must not contain the raw value.
    """
    client = MagicMock(name="anthropic.Anthropic")
    created = SimpleNamespace(id="cred_abc", display_name="x")
    leak_value = "5Aep_REGRESSION_GUARD_refresh_token_VALUE"
    validation = SimpleNamespace(
        status="valid",
        valid=True,
        refresh_token=leak_value,
        access_token="REGRESSION_GUARD_access_token",
        client_secret="REGRESSION_GUARD_client_secret",
    )
    client.beta.vaults.credentials.create.return_value = created
    client.beta.vaults.credentials.mcp_oauth_validate.return_value = validation

    rc = script_mod.add_credential(vault_id="vlt_test123", apply=True, client=client)
    assert rc == 0
    out = capsys.readouterr().out
    # The raw secret values must not appear anywhere in the printed body.
    assert leak_value not in out
    assert "REGRESSION_GUARD_access_token" not in out
    assert "REGRESSION_GUARD_client_secret" not in out
    # The redaction sentinel must appear in their place.
    assert "[REDACTED]" in out
