#!/usr/bin/env bash
# Dev dry-run script for the Shopify Inventory Discrepancy Agent.
#
# Usage:
#   ./scripts/dev_run.sh
#   ./scripts/dev_run.sh --send-test-webhook
#
# Prerequisites:
#   - ngrok v3 installed (https://ngrok.com/download)
#   - .env file present with SHOPIFY_SANDBOX_MODE (or just dev credentials)
#   - App running locally on port 8000 (docker-compose up or uvicorn)
set -euo pipefail

# ---------------------------------------------------------------------------
# Guard checks
# ---------------------------------------------------------------------------
if [[ ! -f ".env" ]]; then
  echo "ERROR: .env file not found. Copy .env.example and fill in dev store values." >&2
  exit 1
fi

set -a
source .env 2>/dev/null || true
set +a

if [[ -z "${SHOPIFY_SHOP_DOMAIN:-}" ]] || [[ -z "${SHOPIFY_ACCESS_TOKEN:-}" ]]; then
  echo "ERROR: SHOPIFY_SHOP_DOMAIN and SHOPIFY_ACCESS_TOKEN must be set in .env" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Parse flags
# ---------------------------------------------------------------------------
SEND_TEST=false
for arg in "$@"; do
  case "$arg" in
    --send-test-webhook) SEND_TEST=true ;;
  esac
done

# ---------------------------------------------------------------------------
# Find ngrok binary
# ---------------------------------------------------------------------------
NGROK_BIN=""
if command -v ngrok &>/dev/null; then
  NGROK_BIN="ngrok"
elif command -v ngrok.exe &>/dev/null; then
  NGROK_BIN="ngrok.exe"
else
  echo "ERROR: ngrok not found. Install from https://ngrok.com/download" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Start ngrok
# ---------------------------------------------------------------------------
echo "Starting ngrok tunnel on port 8000..."
"$NGROK_BIN" http 8000 --log stdout >/tmp/ngrok-inventory-agent.log 2>&1 &
NGROK_PID=$!
trap 'kill $NGROK_PID 2>/dev/null; echo "ngrok stopped."' EXIT

for i in {1..10}; do
  PUBLIC_URL=$(curl -s http://localhost:4040/api/tunnels 2>/dev/null \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['tunnels'][0]['public_url'])" 2>/dev/null || true)
  if [[ -n "$PUBLIC_URL" ]]; then
    break
  fi
  sleep 1
done

if [[ -z "$PUBLIC_URL" ]]; then
  echo "ERROR: Could not get ngrok public URL. Check /tmp/ngrok-inventory-agent.log" >&2
  exit 1
fi

echo "ngrok tunnel:  $PUBLIC_URL"
echo "Shop domain:   $SHOPIFY_SHOP_DOMAIN"
echo ""

# ---------------------------------------------------------------------------
# Register webhooks
# ---------------------------------------------------------------------------
echo "Registering webhooks..."
WEBHOOK_ENDPOINT_URL="$PUBLIC_URL" python3 scripts/register_webhooks.py
echo ""

# ---------------------------------------------------------------------------
# Print manual test curl
# ---------------------------------------------------------------------------
ITEM_ID="12345678"
LOC_ID="87654321"
echo "To send a test inventory_levels/update webhook manually:"
echo ""
echo "  PAYLOAD='{\"inventory_item_id\":$ITEM_ID,\"location_id\":$LOC_ID,\"available\":45,\"previous_quantity\":100,\"sku\":\"SKU-TEST-001\"}'"
echo "  WEBHOOK_ID=\"test-\$(date +%s)\""
echo "  SIG=\$(python3 -c \"import hmac,hashlib,base64,os; print(base64.b64encode(hmac.new(os.environ['SHOPIFY_WEBHOOK_SECRET'].encode(),'\$PAYLOAD'.encode(),hashlib.sha256).digest()).decode())\")"
echo "  curl -X POST $PUBLIC_URL/api/webhooks/inventory-levels/update \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -H \"X-Shopify-Hmac-Sha256: \$SIG\" \\"
echo "    -H \"X-Shopify-Webhook-Id: \$WEBHOOK_ID\" \\"
echo "    -H 'X-Shopify-Topic: inventory_levels/update' \\"
echo "    -d \"\$PAYLOAD\""
echo ""

# ---------------------------------------------------------------------------
# --send-test-webhook: fire one automatically (55% discrepancy → triggers workflow)
# ---------------------------------------------------------------------------
if [[ "$SEND_TEST" == "true" ]]; then
  if [[ -z "${SHOPIFY_WEBHOOK_SECRET:-}" ]]; then
    echo "ERROR: SHOPIFY_WEBHOOK_SECRET not set — cannot compute HMAC for test webhook." >&2
    exit 1
  fi

  PAYLOAD="{\"inventory_item_id\":$ITEM_ID,\"location_id\":$LOC_ID,\"available\":45,\"previous_quantity\":100,\"sku\":\"SKU-TEST-001\"}"
  WEBHOOK_ID="test-$(date +%s)"

  SIG=$(python3 -c "
import hmac as _hmac, hashlib, base64, os
secret = os.environ['SHOPIFY_WEBHOOK_SECRET'].encode()
body = '''$PAYLOAD'''.encode()
print(base64.b64encode(_hmac.new(secret, body, hashlib.sha256).digest()).decode())
")

  echo "Sending test inventory webhook (id=$WEBHOOK_ID, discrepancy=55%)..."
  RESPONSE=$(curl -s -w "\nHTTP_STATUS:%{http_code}" -X POST "$PUBLIC_URL/api/webhooks/inventory-levels/update" \
    -H "Content-Type: application/json" \
    -H "X-Shopify-Hmac-Sha256: $SIG" \
    -H "X-Shopify-Webhook-Id: $WEBHOOK_ID" \
    -H "X-Shopify-Topic: inventory_levels/update" \
    -d "$PAYLOAD")

  HTTP_STATUS=$(echo "$RESPONSE" | grep "HTTP_STATUS:" | cut -d: -f2)
  BODY=$(echo "$RESPONSE" | grep -v "HTTP_STATUS:")

  echo "Response ($HTTP_STATUS): $BODY"

  if [[ "$HTTP_STATUS" == "200" ]]; then
    # Poll for workflow status using run_id from response
    RUN_ID=$(echo "$BODY" | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(d.get('run_id',''))" 2>/dev/null || true)
    if [[ -n "$RUN_ID" ]]; then
      echo ""
      echo "Workflow started: run_id=$RUN_ID"
      echo "Check status:  curl $PUBLIC_URL/api/discrepancies/$RUN_ID"
      echo "Approve:       curl -X POST $PUBLIC_URL/api/approvals/$RUN_ID -H 'Content-Type: application/json' -d '{\"approved\":true,\"reviewer_id\":\"dev\"}'"
    fi
  else
    echo "ERROR: Unexpected status $HTTP_STATUS" >&2
  fi
fi

# ---------------------------------------------------------------------------
# Keep ngrok alive until Ctrl+C
# ---------------------------------------------------------------------------
echo "Press Ctrl+C to stop ngrok and exit."
wait $NGROK_PID
