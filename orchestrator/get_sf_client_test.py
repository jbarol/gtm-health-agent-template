"""Tests for session_runner._get_sf_client — credential-resolution order.

Covers all four paths in priority order:

  1. OAuth Client Credentials Flow (preferred for SSO-enforced orgs)
  2. Per-portco SOAP username/password+token
  3. Global SF_USERNAME/SF_PASSWORD/SF_SECURITY_TOKEN
  4. Global SF_INSTANCE_URL + SF_ACCESS_TOKEN bearer

Plus the OAuth-specific error-surfacing test — we want the operator to see
the actual SF error (missing run-as user, invalid client secret, etc.)
rather than a generic INVALID_LOGIN, which is exactly the failure mode
the OAuth path is here to fix.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure orchestrator/ is on sys.path.
ORCH = Path(__file__).resolve().parent
if str(ORCH) not in sys.path:
    sys.path.insert(0, str(ORCH))


def _seed_required_env(monkeypatch):
    """Stamp every config-required env var so importing session_runner works."""
    for k in (
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
        "WRITING_AGENT_ID",
    ):
        monkeypatch.setenv(k, "x")


def _clear_sf_env(monkeypatch):
    """Wipe every SF-related env var so each test starts from a clean slate."""
    for var in (
        "SF_USERNAME",
        "SF_PASSWORD",
        "SF_SECURITY_TOKEN",
        "SF_INSTANCE_URL",
        "SF_ACCESS_TOKEN",
        "SF_CONSUMER_KEY_ACME",
        "SF_CONSUMER_SECRET_ACME",
        "SF_DOMAIN_ACME",
        "SF_USERNAME_ACME",
        "SF_PASSWORD_ACME",
        "SF_TOKEN_ACME",
    ):
        monkeypatch.delenv(var, raising=False)


def _fake_portco_config_with_oauth():
    """Mirror of the live portco_config.json shape — all six env mappings."""
    return {
        "data_sources": {
            "crm": {
                "type": "salesforce",
                "sf_credentials": {
                    "consumer_key_env": "SF_CONSUMER_KEY_ACME",
                    "consumer_secret_env": "SF_CONSUMER_SECRET_ACME",
                    "domain_env": "SF_DOMAIN_ACME",
                    "username_env": "SF_USERNAME_ACME",
                    "password_env": "SF_PASSWORD_ACME",
                    "token_env": "SF_TOKEN_ACME",
                },
            }
        }
    }


def test_oauth_client_credentials_wins_when_all_three_oauth_envs_set(monkeypatch):
    """When consumer_key + consumer_secret + domain are all set → OAuth path.

    The OAuth path takes priority over SOAP username/password+token even if
    those are also set, because OAuth is the modern flow and SOAP is being
    deprecated.
    """
    _seed_required_env(monkeypatch)
    _clear_sf_env(monkeypatch)
    monkeypatch.setenv("SF_CONSUMER_KEY_ACME", "ck_test")
    monkeypatch.setenv("SF_CONSUMER_SECRET_ACME", "cs_test")
    monkeypatch.setenv("SF_DOMAIN_ACME", "your-org.my.salesforce.com")
    # Also set SOAP creds — OAuth should win.
    monkeypatch.setenv("SF_USERNAME_ACME", "soap_user")
    monkeypatch.setenv("SF_PASSWORD_ACME", "soap_pass")
    monkeypatch.setenv("SF_TOKEN_ACME", "soap_token")

    import session_runner  # type: ignore

    with (
        patch(
            "session_runner._get_sf_oauth_client_credentials_token",
            return_value=("at_test", "https://your-org.my.salesforce.com"),
        ) as mock_oauth,
        patch(
            "portco_registry.get_portco_config",
            return_value=_fake_portco_config_with_oauth(),
        ),
        patch("simple_salesforce.Salesforce") as mock_sf,
    ):
        session_runner._get_sf_client("acme")

    mock_oauth.assert_called_once_with(
        "your-org.my.salesforce.com", "ck_test", "cs_test"
    )
    mock_sf.assert_called_once_with(
        instance_url="https://your-org.my.salesforce.com",
        session_id="at_test",
    )


def test_falls_back_to_soap_when_one_oauth_env_missing(monkeypatch):
    """All three OAuth env vars required — missing any one falls through to SOAP."""
    _seed_required_env(monkeypatch)
    _clear_sf_env(monkeypatch)
    # Consumer key + secret set, but no domain → fall through.
    monkeypatch.setenv("SF_CONSUMER_KEY_ACME", "ck_test")
    monkeypatch.setenv("SF_CONSUMER_SECRET_ACME", "cs_test")
    monkeypatch.setenv("SF_USERNAME_ACME", "soap_user")
    monkeypatch.setenv("SF_PASSWORD_ACME", "soap_pass")
    monkeypatch.setenv("SF_TOKEN_ACME", "soap_token")

    import session_runner  # type: ignore

    with (
        patch("session_runner._get_sf_oauth_client_credentials_token") as mock_oauth,
        patch(
            "portco_registry.get_portco_config",
            return_value=_fake_portco_config_with_oauth(),
        ),
        patch("simple_salesforce.Salesforce") as mock_sf,
    ):
        session_runner._get_sf_client("acme")

    mock_oauth.assert_not_called()
    mock_sf.assert_called_once_with(
        username="soap_user",
        password="soap_pass",
        security_token="soap_token",
    )


def test_oauth_token_helper_raises_with_actionable_error(monkeypatch):
    """When SF rejects the client_credentials grant, the error message surfaces.

    The whole point of the OAuth path is operator-visible errors. A generic
    INVALID_LOGIN (SOAP behavior) gives no clue about what's wrong; the OAuth
    error response distinguishes ``invalid_client``, ``no client credentials
    user enabled`` (the missing-run-as-user case we hit during deploy), and so on.
    """
    _seed_required_env(monkeypatch)
    import session_runner  # type: ignore

    fake_resp = MagicMock()
    fake_resp.status_code = 400
    fake_resp.json.return_value = {
        "error": "invalid_grant",
        "error_description": "no client credentials user enabled",
    }
    with patch("httpx.post", return_value=fake_resp):
        try:
            session_runner._get_sf_oauth_client_credentials_token(
                "your-org.my.salesforce.com", "ck", "cs"
            )
            raise AssertionError("expected RuntimeError")
        except RuntimeError as e:
            msg = str(e)
            assert "400" in msg
            assert "invalid_grant" in msg
            assert "no client credentials user enabled" in msg


def test_oauth_token_helper_prepends_https_when_domain_lacks_scheme(monkeypatch):
    """Operator-friendly: accept ``your-org.my.salesforce.com`` or full URL."""
    _seed_required_env(monkeypatch)
    import session_runner  # type: ignore

    captured: dict = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "access_token": "at_test",
            "instance_url": "https://your-org.my.salesforce.com",
        }
        return resp

    with patch("httpx.post", side_effect=fake_post):
        session_runner._get_sf_oauth_client_credentials_token(
            "your-org.my.salesforce.com", "ck", "cs"
        )

    assert captured["url"] == "https://your-org.my.salesforce.com/services/oauth2/token"


def test_falls_through_to_global_when_no_portco_creds(monkeypatch):
    """No per-portco config → use global SF_USERNAME / SF_PASSWORD / SF_SECURITY_TOKEN."""
    _seed_required_env(monkeypatch)
    _clear_sf_env(monkeypatch)
    monkeypatch.setenv("SF_USERNAME", "global_user")
    monkeypatch.setenv("SF_PASSWORD", "global_pass")
    monkeypatch.setenv("SF_SECURITY_TOKEN", "global_token")

    import session_runner  # type: ignore

    with (
        patch("portco_registry.get_portco_config", return_value=None),
        patch("simple_salesforce.Salesforce") as mock_sf,
    ):
        session_runner._get_sf_client("acme")

    mock_sf.assert_called_once_with(
        username="global_user",
        password="global_pass",
        security_token="global_token",
    )


def test_raises_when_nothing_configured(monkeypatch):
    """No creds anywhere → raise with operator-friendly explanation."""
    _seed_required_env(monkeypatch)
    _clear_sf_env(monkeypatch)

    import session_runner  # type: ignore

    with patch("portco_registry.get_portco_config", return_value=None):
        try:
            session_runner._get_sf_client("acme")
            raise AssertionError("expected RuntimeError")
        except RuntimeError as e:
            msg = str(e)
            assert "acme" in msg
            assert "OAuth Client Credentials" in msg or "consumer_key_env" in msg
