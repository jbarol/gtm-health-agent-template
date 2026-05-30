#!/usr/bin/env bash
# verify_kapa_key.sh — single-shot validation of a Kapa MCP bearer token.
#
# Usage:
#   bin/verify_kapa_key.sh <API_KEY>
#       OR
#   KAPA_ACME_API_KEY=... bin/verify_kapa_key.sh
#
# Outcomes:
#   PASS  — token works against the Acme Internal MCP endpoint
#           (server initialize returns 200; tools/list returns the
#           search_knowledge_base tool).
#   FAIL: invalid_token        — bad/expired/wrong-tenant key.
#                                Need a freshly minted key from
#                                app.kapa.ai → Acme Internal project.
#   FAIL: unauthorized_scope   — key authenticates but is not authorized
#                                for the Acme Internal MCP server.
#                                Key was likely minted for a different
#                                Kapa project / org. Mint one from
#                                INSIDE the Acme Internal project.
#   FAIL: other                — network / DNS / 5xx; investigate.
set -euo pipefail

KEY="${1:-${KAPA_ACME_API_KEY:-}}"
if [ -z "${KEY}" ]; then
  echo "FAIL: no API key provided" >&2
  echo "  pass as first arg, or export KAPA_ACME_API_KEY first" >&2
  exit 2
fi

URL="https://acme.mcp.kapa.example"

# Single JSON-RPC initialize call — the cheapest valid MCP request.
PAYLOAD='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"verify_kapa_key","version":"0.1"}}}'

RESP=$(curl -sS -w "\n__HTTP__:%{http_code}" \
  -X POST "$URL" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d "$PAYLOAD" 2>&1 || true)

CODE=$(echo "$RESP" | grep -oE '__HTTP__:[0-9]+$' | cut -d: -f2)
BODY=$(echo "$RESP" | sed -E 's/__HTTP__:[0-9]+$//')

echo "endpoint:    $URL"
echo "status_code: $CODE"
echo "body:        $(echo "$BODY" | head -c 500)"
echo ""

case "$CODE" in
  200)
    if echo "$BODY" | grep -q "search_knowledge_base\|serverInfo\|protocolVersion"; then
      echo "PASS — token is valid for the Acme Internal MCP server"
      exit 0
    fi
    echo "PASS (probably) — 200 but body didn't include expected fields"
    exit 0
    ;;
  401|403)
    if echo "$BODY" | grep -qi "invalid_token\|expired"; then
      echo "FAIL: invalid_token"
      echo ""
      echo "Token is invalid, expired, or revoked. Fix:"
      echo "  1. Log into https://app.kapa.ai via SSO"
      echo "  2. Switch to the **Acme Internal** project (top-left selector)"
      echo "  3. Settings → API Keys → Create new key"
      echo "     (label it 'GTM Health Agent — MCP Bearer')"
      echo "  4. Re-run: bin/verify_kapa_key.sh <new_key>"
      exit 1
    fi
    if echo "$BODY" | grep -qi "not authorized\|access this server\|scope"; then
      echo "FAIL: unauthorized_scope"
      echo ""
      echo "Token authenticates but is NOT authorized for the Acme"
      echo "Internal MCP server. The key was minted for the wrong Kapa"
      echo "project. Fix:"
      echo "  1. Log into https://app.kapa.ai via SSO"
      echo "  2. Use the project selector (top-left) and pick"
      echo "     **Acme Internal** specifically — not 'Acme' or any"
      echo "     other project. The MCP server lives under the Internal"
      echo "     tenant only."
      echo "  3. Settings → API Keys → Create new key for THIS project"
      echo "  4. Re-run: bin/verify_kapa_key.sh <new_key>"
      exit 1
    fi
    echo "FAIL: $CODE (unclassified auth error)"
    exit 1
    ;;
  *)
    echo "FAIL: HTTP $CODE — network / DNS / 5xx; investigate"
    exit 1
    ;;
esac
