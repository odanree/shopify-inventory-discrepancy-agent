#!/usr/bin/env python3
"""Register Shopify webhook subscriptions for the Inventory Discrepancy Agent.

Usage:
    python scripts/register_webhooks.py --endpoint-url https://abc123.ngrok.io
    python scripts/register_webhooks.py --endpoint-url https://abc123.ngrok.io --dry-run

Reads SHOPIFY_SHOP_DOMAIN and SHOPIFY_ACCESS_TOKEN from the environment (or .env file).
Prints the webhook secret on creation — copy it to SHOPIFY_WEBHOOK_SECRET in .env.
"""
import argparse
import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2024-01")

# topic → path suffix on our FastAPI app
TOPICS = {
    "inventory_levels/update": "/api/webhooks/inventory-levels/update",
}


def _headers(token: str) -> dict:
    return {
        "X-Shopify-Access-Token": token,
        "Content-Type": "application/json",
    }


def list_webhooks(client: httpx.Client, domain: str, token: str) -> list[dict]:
    url = f"https://{domain}/admin/api/{API_VERSION}/webhooks.json"
    resp = client.get(url, headers=_headers(token))
    resp.raise_for_status()
    return resp.json().get("webhooks", [])


def register_webhook(
    client: httpx.Client, domain: str, token: str, topic: str, address: str
) -> dict:
    url = f"https://{domain}/admin/api/{API_VERSION}/webhooks.json"
    payload = {"webhook": {"topic": topic, "address": address, "format": "json"}}
    resp = client.post(url, headers=_headers(token), json=payload)
    if resp.status_code == 422:
        return {"status": "already_exists", "topic": topic, "address": address}
    resp.raise_for_status()
    return resp.json().get("webhook", {})


def main(endpoint_url: str, dry_run: bool) -> None:
    domain = os.getenv("SHOPIFY_SHOP_DOMAIN")
    token = os.getenv("SHOPIFY_ACCESS_TOKEN")

    if not domain or not token:
        print("ERROR: SHOPIFY_SHOP_DOMAIN and SHOPIFY_ACCESS_TOKEN must be set.", file=sys.stderr)
        sys.exit(1)

    endpoint_url = endpoint_url.rstrip("/")
    print(f"Shop: {domain}")
    print(f"Endpoint base URL: {endpoint_url}")
    print(f"Dry run: {dry_run}\n")

    with httpx.Client(timeout=15.0) as client:
        existing = list_webhooks(client, domain, token)
        existing_addresses = {w["address"] for w in existing}

        print(f"Found {len(existing)} existing webhook(s).")

        for topic, path in TOPICS.items():
            address = f"{endpoint_url}{path}"
            if address in existing_addresses:
                print(f"  [SKIP]     {topic} → already registered at {address}")
                continue

            if dry_run:
                print(f"  [DRY RUN]  {topic} → would register at {address}")
                continue

            result = register_webhook(client, domain, token, topic, address)
            if result.get("status") == "already_exists":
                print(f"  [SKIP]     {topic} → {address} (422 already exists)")
            else:
                webhook_id = result.get("id")
                print(f"  [CREATED]  {topic} → {address} (id={webhook_id})")
                print(f"             Copy HMAC secret to SHOPIFY_WEBHOOK_SECRET in .env")

    print("\nDone.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Register Shopify webhooks for inventory discrepancy agent"
    )
    parser.add_argument(
        "--endpoint-url",
        default=os.getenv("WEBHOOK_ENDPOINT_URL", ""),
        help="Public base URL of the app (e.g. https://abc123.ngrok.io)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview without registering")
    args = parser.parse_args()

    if not args.endpoint_url:
        print("ERROR: --endpoint-url or WEBHOOK_ENDPOINT_URL env var required.", file=sys.stderr)
        sys.exit(1)

    main(args.endpoint_url, args.dry_run)
