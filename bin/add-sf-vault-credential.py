#!/usr/bin/env python3
"""Add an mcp_oauth credential to the Acme vault for the SF Connected App.

Plan #44 Task #17 — operator script. Creates a vault-backed credential
that Anthropic can use to broker Salesforce MCP calls without surfacing
the refresh token to Railway. After the credential lands, runs the
``mcp_oauth_validate`` diagnostic so the operator sees on a single
terminal whether the credential is healthy.

Default mode is ``--dry-run`` — prints the planned ``credentials.create``
call body so the operator can eyeball before any network write happens.
``--apply`` is the explicit opt-in; the script never writes without it.

Runbook: ``docs/runbooks/managed-agents-conformance.md`` (Vault SF MCP
rollout). The flag flip (``SF_MCP_VIA_VAULT=true``) requires a
DEPLOY-TIME re-publish — re-run ``agents/update_subagent_tools.py`` and
``agents/update_coordinator_roster.py`` after flipping. Runtime flag
flips do nothing because tools[] is server-side agent configuration.

Usage:
    python bin/add-sf-vault-credential.py --dry-run                 # default
    python bin/add-sf-vault-credential.py --apply
    python bin/add-sf-vault-credential.py --apply --vault-id vlt_X

Env vars consumed:
    ANTHROPIC_API_KEY           — required (anthropic.Anthropic())
    ACME_VAULT_ID           — fallback when --vault-id not passed
    SF_MCP_SERVER_URL           — fallback for the MCP server URL
                                  (defaults to the production Acme
                                  sobject-reads endpoint)
    SF_ACME_CLIENT_ID       — Connected App consumer key
    SF_ACME_CLIENT_SECRET   — Connected App consumer secret
    SF_ACME_REFRESH_TOKEN   — current OAuth refresh token
    SF_ACME_ACCESS_TOKEN    — current OAuth access token (the vault
                                  needs at least one valid access_token
                                  at create; refresh kicks in later)
    SF_ACME_TOKEN_ENDPOINT  — token endpoint (default Salesforce login)
    SF_ACME_SCOPE           — OAuth scope (default 'api refresh_token')
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO_ROOT / "agents"
ORCH_DIR = REPO_ROOT / "orchestrator"

for _p in (AGENTS_DIR, ORCH_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

DEFAULT_SF_MCP_SERVER_URL = (
    "https://api.salesforce.com/platform/mcp/v1/platform/sobject-reads"
)
DEFAULT_SF_TOKEN_ENDPOINT = "https://login.salesforce.com/services/oauth2/token"
DEFAULT_SF_SCOPE = "api refresh_token"


def _load_env() -> None:
    """Manual dotenv loader (no python-dotenv) matching orchestrator/config.py."""
    dotenv = REPO_ROOT / ".env"
    if not dotenv.exists():
        return
    for line in dotenv.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def _build_client():
    """Return an Anthropic client. Imported lazily so tests can stub."""
    import anthropic  # noqa: WPS433

    return anthropic.Anthropic()


def _build_auth_payload() -> dict:
    """Assemble the ``auth`` dict for ``credentials.create``.

    The shape mirrors the Anthropic SDK's
    ``BetaManagedAgentsMCPOAuthCreateParams``:

        type: "mcp_oauth"
        access_token: str
        mcp_server_url: str
        refresh: {
            client_id: str
            refresh_token: str
            token_endpoint: str
            token_endpoint_auth: { type: "client_secret_post", client_secret }
            scope: str
        }

    Raises ``SystemExit`` when any required env var is missing — bail
    early rather than send a half-populated payload to the API.
    """
    mcp_server_url = os.environ.get("SF_MCP_SERVER_URL", DEFAULT_SF_MCP_SERVER_URL)
    client_id = os.environ.get("SF_ACME_CLIENT_ID", "")
    client_secret = os.environ.get("SF_ACME_CLIENT_SECRET", "")
    refresh_token = os.environ.get("SF_ACME_REFRESH_TOKEN", "")
    access_token = os.environ.get("SF_ACME_ACCESS_TOKEN", "")
    token_endpoint = os.environ.get(
        "SF_ACME_TOKEN_ENDPOINT", DEFAULT_SF_TOKEN_ENDPOINT
    )
    scope = os.environ.get("SF_ACME_SCOPE", DEFAULT_SF_SCOPE)

    missing = [
        name
        for name, val in (
            ("SF_ACME_CLIENT_ID", client_id),
            ("SF_ACME_CLIENT_SECRET", client_secret),
            ("SF_ACME_REFRESH_TOKEN", refresh_token),
            ("SF_ACME_ACCESS_TOKEN", access_token),
        )
        if not val
    ]
    if missing:
        raise SystemExit(
            "Missing required env vars: "
            + ", ".join(missing)
            + ". Set them in .env or Railway then re-run.\n"
            "See docs/runbooks/managed-agents-conformance.md for the rollout flow."
        )

    return {
        "type": "mcp_oauth",
        "access_token": access_token,
        "mcp_server_url": mcp_server_url,
        "refresh": {
            "client_id": client_id,
            "refresh_token": refresh_token,
            "token_endpoint": token_endpoint,
            "token_endpoint_auth": {
                "type": "client_secret_post",
                "client_secret": client_secret,
            },
            "scope": scope,
        },
    }


# Field names whose values must NEVER be printed. Used by both the
# auth-payload redactor and the mcp_oauth_validate diagnostic redactor —
# the SDK may echo the credential back verbatim in the validation
# response (Anthropic's diagnostic format isn't contractually stable),
# and we'd rather over-redact than leak a refresh_token to terminal
# output an operator pastes into a ticket.
_SENSITIVE_KEYS = frozenset(
    {
        "access_token",
        "refresh_token",
        "client_secret",
        "password",
        "secret",
        "token",
    }
)


def _redact_walk(obj):
    """Walk an arbitrary JSON-shaped object and redact sensitive keys in place.

    Returns the same object reference for fluent use. Recurses into
    dict values and list elements. Replaces any value whose key matches
    ``_SENSITIVE_KEYS`` with ``"[REDACTED]"`` — case-insensitive on key
    name to catch upper-case variants the SDK might emit.
    """
    if isinstance(obj, dict):
        for k in list(obj.keys()):
            if isinstance(k, str) and k.lower() in _SENSITIVE_KEYS:
                obj[k] = "[REDACTED]"
            else:
                _redact_walk(obj[k])
    elif isinstance(obj, list):
        for item in obj:
            _redact_walk(item)
    return obj


def _redact_for_print(payload: dict) -> dict:
    """Return a copy of the auth payload with secrets redacted for logging.

    Print-safe: replaces ``access_token``, ``client_secret``,
    ``refresh_token``, and any other sensitive-keyed field with
    ``[REDACTED]``. The script never prints the raw values so an
    operator can paste output into a ticket without leaking
    credentials.
    """
    redacted = json.loads(json.dumps(payload))  # deep copy via JSON
    return _redact_walk(redacted)


def _validation_to_dict(validation):
    """Flatten the SDK's validation object into a plain dict for printing.

    The result is recursively redacted against ``_SENSITIVE_KEYS``
    before return — the validation response from
    ``mcp_oauth_validate`` may echo the credential back verbatim
    (refresh_token, access_token, client_secret), and the diagnostic
    is printed to terminal where it can leak into operator tickets.
    Apply the redact pass at the source so every caller is safe.
    """
    if hasattr(validation, "model_dump"):
        raw = validation.model_dump(exclude_none=True)
    elif isinstance(validation, dict):
        raw = json.loads(json.dumps(validation))  # deep copy
    else:
        raw = {
            k: v
            for k, v in vars(validation).items()
            if not k.startswith("_") and v is not None
        }
        # Walk one layer down — vars() returns shallow values.
        raw = json.loads(json.dumps(raw, default=str))
    return _redact_walk(raw)


def add_credential(
    vault_id: str,
    *,
    apply: bool,
    client=None,
    display_name: str = "Salesforce — Acme (mcp_oauth)",
) -> int:
    """Create the credential and run mcp_oauth_validate.

    Returns the script's exit code (0 = success). Tests pass a mocked
    client; production resolves one via ``_build_client``.
    """
    if not vault_id:
        raise SystemExit(
            "Vault ID is required. Pass --vault-id or set ACME_VAULT_ID."
        )

    auth = _build_auth_payload()
    redacted_print = json.dumps(_redact_for_print(auth), indent=2, sort_keys=True)

    if not apply:
        print("[DRY-RUN] No network calls will be made. Use --apply to write.")
        print(f"[DRY-RUN] Vault:        {vault_id}")
        print(f"[DRY-RUN] Display name: {display_name}")
        print("[DRY-RUN] Auth payload (secrets redacted):")
        for line in redacted_print.splitlines():
            print(f"  {line}")
        print("\nNext: re-run with --apply to create the credential and validate it.")
        return 0

    client = client or _build_client()

    print(f"[APPLY] Creating credential in vault {vault_id}...")
    try:
        cred = client.beta.vaults.credentials.create(
            vault_id=vault_id,
            display_name=display_name,
            auth=auth,
        )
    except Exception as exc:
        print(f"[FAIL] credentials.create raised: {exc}")
        return 1

    cred_id = getattr(cred, "id", None)
    if not cred_id:
        print(f"[FAIL] credentials.create returned no id: {cred!r}")
        return 1
    print(f"[OK] credential created: id={cred_id}")

    print("[APPLY] Running mcp_oauth_validate diagnostic...")
    try:
        validation = client.beta.vaults.credentials.mcp_oauth_validate(
            cred_id, vault_id=vault_id
        )
    except Exception as exc:
        print(f"[FAIL] mcp_oauth_validate raised: {exc}")
        print(
            "\n[WARN] credential was created but validation failed. Inspect "
            "the credential via the Anthropic Console and either fix the "
            "OAuth payload or archive the credential and retry."
        )
        return 1

    validation_dict = _validation_to_dict(validation)
    print("[OK] mcp_oauth_validate diagnostic:")
    for line in json.dumps(validation_dict, indent=2, sort_keys=True).splitlines():
        print(f"  {line}")

    print("\n" + "=" * 72)
    print("Next steps")
    print("=" * 72)
    print(f"  1. credential_id: {cred_id}")
    print(
        "  2. Recommended Railway var: SF_MCP_VIA_VAULT=false initially "
        "(then true after agent re-deploy)."
    )
    print(
        "  3. Flipping SF_MCP_VIA_VAULT requires re-running "
        "agents/update_subagent_tools.py AND re-publishing the Coordinator "
        "multiagent roster (agents/update_coordinator_roster.py) so the "
        "new tool shape reaches production sessions."
    )
    print(
        "  4. Validate end-to-end: dream + adhoc smoke runs. Watch "
        "session_costs for token-blowup regressions (the 1M-context "
        "incident motivated keeping dump_sf_query as fallback)."
    )
    print(
        "\nFull runbook: docs/runbooks/managed-agents-conformance.md "
        "(Vault SF MCP rollout)."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Add an mcp_oauth credential to the Acme vault for the SF "
            "Connected App. See docs/runbooks/managed-agents-conformance.md "
            "for the deploy-time rollout flow."
        ),
        epilog=(
            "Flag flips are DEPLOY-TIME operations, not runtime. After "
            "creating the credential, set SF_MCP_VIA_VAULT=true (Railway "
            "build var), re-deploy, then run "
            "agents/update_subagent_tools.py to publish new agent shapes."
        ),
    )
    # --dry-run is the default behavior, --apply is the explicit opt-in
    # for the write path. Make them mutually exclusive so passing BOTH
    # (e.g. `--dry-run --apply`, easy to do during ops handoff) becomes
    # a hard argparse error rather than silently letting one win.
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print the planned payload (redacted) and exit. Default mode.",
    )
    mode_group.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Actually create the credential and run mcp_oauth_validate.",
    )
    parser.add_argument(
        "--vault-id",
        type=str,
        default=None,
        help="Vault ID. Falls back to ACME_VAULT_ID env when omitted.",
    )
    parser.add_argument(
        "--display-name",
        type=str,
        default="Salesforce — Acme (mcp_oauth)",
        help="Human-readable name for the credential (≤255 chars).",
    )
    args = parser.parse_args(argv)

    _load_env()

    vault_id = args.vault_id or os.environ.get("ACME_VAULT_ID", "").strip()
    apply_now = bool(args.apply)

    return add_credential(
        vault_id=vault_id,
        apply=apply_now,
        display_name=args.display_name,
    )


if __name__ == "__main__":
    sys.exit(main())
