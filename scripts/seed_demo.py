#!/usr/bin/env python3
"""Demo seed script for the Inventory Discrepancy Agent.

Posts synthetic discrepancy events directly to the discrepancy detection
endpoint, simulating what the scheduler would fire after detecting divergence
from a Redis baseline.

Usage:
    python scripts/seed_demo.py                     # sends all scenarios
    python scripts/seed_demo.py --scenario major    # single scenario
    python scripts/seed_demo.py --url http://host:8000

Requires:
    pip install httpx

The service must be running: docker-compose up
Set baselines first (or the scheduler handles it automatically).
"""
import argparse
import asyncio
import json
import sys
import uuid

try:
    import httpx
except ImportError:
    print("pip install httpx")
    sys.exit(1)

BASE_URL = "http://localhost:8000"
DISCREPANCY_PATH = "/api/discrepancies/detect"

SCENARIOS = {
    "critical": {
        "name": "Critical — 75% shortage (likely theft or scan error)",
        "payload": {
            "sku": "SNEAKER-AIR-42",
            "inventory_item_id": "demo-item-001",
            "location_id": "demo-location-warehouse-a",
            "expected_quantity": 200,
            "actual_quantity": 50,
            "reported_by": "demo-seed",
            "source": "manual",
        },
    },
    "major": {
        "name": "Major — 30% shortage with open orders (transfer candidate)",
        "payload": {
            "sku": "TSHIRT-BLK-L",
            "inventory_item_id": "demo-item-002",
            "location_id": "demo-location-warehouse-b",
            "expected_quantity": 100,
            "actual_quantity": 70,
            "reported_by": "demo-seed",
            "source": "manual",
        },
    },
    "moderate": {
        "name": "Moderate — 12% variance (data sync lag likely cause)",
        "payload": {
            "sku": "HOODIE-GREY-M",
            "inventory_item_id": "demo-item-003",
            "location_id": "demo-location-store-1",
            "expected_quantity": 50,
            "actual_quantity": 44,
            "reported_by": "demo-seed",
            "source": "manual",
        },
    },
    "minor": {
        "name": "Minor — 3% variance (within normal shrinkage)",
        "payload": {
            "sku": "SOCK-WHITE-ONE",
            "inventory_item_id": "demo-item-004",
            "location_id": "demo-location-store-2",
            "expected_quantity": 300,
            "actual_quantity": 291,
            "reported_by": "demo-seed",
            "source": "manual",
        },
    },
    "surplus": {
        "name": "Surplus — actual exceeds expected (overcount or return not processed)",
        "payload": {
            "sku": "CAP-RED-OS",
            "inventory_item_id": "demo-item-005",
            "location_id": "demo-location-warehouse-a",
            "expected_quantity": 80,
            "actual_quantity": 120,
            "reported_by": "demo-seed",
            "source": "manual",
        },
    },
}


async def send_discrepancy(client: httpx.AsyncClient, base_url: str, scenario_key: str, scenario: dict):
    try:
        resp = await client.post(
            f"{base_url}{DISCREPANCY_PATH}",
            json=scenario["payload"],
            timeout=10.0,
        )
        status = "OK" if resp.status_code in (200, 201, 202) else "FAIL"
        try:
            result = resp.json()
            run_id = result.get("run_id", result.get("detail", "?"))
        except Exception:
            run_id = resp.text[:60]
        print(f"  {status} [{resp.status_code}] {scenario['name']}")
        print(f"     run_id={run_id}")
    except Exception as exc:
        print(f"  FAIL {scenario['name']}: {exc}")


async def main():
    parser = argparse.ArgumentParser(description="Seed demo inventory discrepancy events")
    parser.add_argument("--url", default=BASE_URL, help="Base URL of the agent service")
    parser.add_argument("--scenario", choices=list(SCENARIOS.keys()), help="Run a single scenario")
    parser.add_argument("--delay", type=float, default=1.0, help="Seconds between requests")
    args = parser.parse_args()

    scenarios = {args.scenario: SCENARIOS[args.scenario]} if args.scenario else SCENARIOS

    print(f"\nInventory Discrepancy Agent -- Demo Seed")
    print(f"   Target: {args.url}")
    print(f"   Scenarios: {len(scenarios)}\n")

    async with httpx.AsyncClient() as client:
        try:
            health = await client.get(f"{args.url}/health", timeout=5.0)
            hdata = health.json()
            print(f"   /health -> {hdata.get('status', '?')} | checks: {hdata.get('checks', {})}\n")
        except Exception as exc:
            print(f"   WARNING: Could not reach {args.url}/health: {exc}")
            print("   Make sure the service is running: docker-compose up\n")

        for key, scenario in scenarios.items():
            await send_discrepancy(client, args.url, key, scenario)
            await asyncio.sleep(args.delay)

    print(f"\n   Done. Watch the dashboard:     {args.url}/dashboard")
    print(f"   Pending approvals:              {args.url}/api/approvals/pending\n")


if __name__ == "__main__":
    asyncio.run(main())
