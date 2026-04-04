"""Shopify GraphQL client for inventory-focused queries.

Same token-bucket rate limiter pattern as the order exception agent, but using
a separate Redis key for the inventory bucket.
"""
import asyncio
import random
from typing import Any

import httpx
import structlog

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# GraphQL strings
# ---------------------------------------------------------------------------

INVENTORY_LEVELS_QUERY = """
query GetInventoryLevels($itemId: ID!, $locationIds: [ID!]!) {
  inventoryItem(id: $itemId) {
    id
    sku
    inventoryLevels(first: 50) {
      edges {
        node {
          id
          available
          location { id name }
        }
      }
    }
  }
}
"""

INVENTORY_ITEM_BY_SKU_QUERY = """
query GetInventoryItemBySku($sku: String!) {
  productVariants(first: 5, query: $sku) {
    edges {
      node {
        id
        sku
        inventoryItem { id }
        product { id title }
      }
    }
  }
}
"""

UNFULFILLED_ORDERS_QUERY = """
query GetUnfulfilledOrders($query: String!) {
  orders(first: 50, query: $query) {
    edges {
      node {
        id
        name
        lineItems(first: 50) {
          edges {
            node { sku quantity }
          }
        }
      }
    }
  }
}
"""

INVENTORY_SET_MUTATION = """
mutation SetInventoryQuantity($input: InventorySetOnHandQuantitiesInput!) {
  inventorySetOnHandQuantities(input: $input) {
    userErrors { field message }
    inventoryAdjustmentGroup {
      createdAt
      reason
      changes { name delta quantityAfterChange }
    }
  }
}
"""

INVENTORY_MOVE_MUTATION = """
mutation MoveInventory($input: InventoryMoveQuantitiesInput!) {
  inventoryMoveQuantities(input: $input) {
    userErrors { field message }
    inventoryAdjustmentGroup {
      createdAt
      reason
      changes { name delta quantityAfterChange }
    }
  }
}
"""

TAGS_ADD_MUTATION = """
mutation TagsAdd($id: ID!, $tags: [String!]!) {
  tagsAdd(id: $id, tags: $tags) {
    node { id }
    userErrors { field message }
  }
}
"""


class InventoryShopifyClient:
    BUCKET_KEY = "shopify:bucket:inventory:available"
    ESTIMATED_QUERY_COST = 100
    MAX_BUCKET = 1000.0
    RESTORE_RATE = 50.0

    def __init__(self, domain: str, token: str, redis_client, restore_rate: float = 50.0):
        self.endpoint = f"https://{domain}/admin/api/2024-01/graphql.json"
        self.headers = {
            "X-Shopify-Access-Token": token,
            "Content-Type": "application/json",
        }
        self.redis = redis_client
        self.restore_rate = restore_rate
        self._client = httpx.AsyncClient(timeout=30.0)

    async def _wait_for_budget(self):
        raw = await self.redis.get(self.BUCKET_KEY)
        available = float(raw) if raw is not None else self.MAX_BUCKET
        if available < self.ESTIMATED_QUERY_COST:
            wait_secs = (self.ESTIMATED_QUERY_COST - available) / self.restore_rate
            logger.info("shopify_bucket_wait", available=available, wait_secs=round(wait_secs, 2))
            await asyncio.sleep(wait_secs)

    async def _update_bucket(self, throttle_status: dict):
        currently = throttle_status.get("currentlyAvailable")
        if currently is not None:
            await self.redis.set(self.BUCKET_KEY, str(float(currently)))

    async def execute(
        self, query: str, variables: dict | None = None, max_retries: int = 3
    ) -> dict[str, Any]:
        await self._wait_for_budget()
        last_error: Exception | None = None
        for attempt in range(max_retries):
            try:
                resp = await self._client.post(
                    self.endpoint,
                    headers=self.headers,
                    json={"query": query, "variables": variables or {}},
                )
                resp.raise_for_status()
                body = resp.json()
                extensions = body.get("extensions", {})
                cost_info = extensions.get("cost", {})
                throttle_status = cost_info.get("throttleStatus", {})
                if throttle_status:
                    await self._update_bucket(throttle_status)
                errors = body.get("errors", [])
                if errors:
                    for err in errors:
                        if err.get("extensions", {}).get("code") == "THROTTLED":
                            backoff = (2 ** attempt) + random.uniform(-0.5, 0.5)
                            logger.warning("shopify_throttled", attempt=attempt + 1)
                            await asyncio.sleep(max(backoff, 0.1))
                            last_error = RuntimeError("Shopify API throttled")
                            break
                    else:
                        raise RuntimeError(f"GraphQL errors: {errors}")
                    continue
                return body.get("data", {})
            except httpx.HTTPStatusError as exc:
                logger.error("shopify_http_error", status=exc.response.status_code)
                last_error = exc
                if exc.response.status_code < 500:
                    raise
                await asyncio.sleep((2 ** attempt) + random.uniform(0, 1))
        raise last_error or RuntimeError("GraphQL request failed after retries")

    async def get_all_inventory_levels(self, inventory_item_id: str) -> list[dict]:
        """Return inventory levels at ALL locations for an item.

        Uses the same INVENTORY_LEVELS_QUERY with an empty locationIds list — Shopify
        returns all locations when the filter list is empty.
        """
        gid = (
            inventory_item_id
            if inventory_item_id.startswith("gid://")
            else f"gid://shopify/InventoryItem/{inventory_item_id}"
        )
        data = await self.execute(INVENTORY_LEVELS_QUERY, {"itemId": gid, "locationIds": []})
        item = data.get("inventoryItem") or {}
        levels_edges = item.get("inventoryLevels", {}).get("edges", [])
        return [edge["node"] for edge in levels_edges]

    async def move_inventory(
        self,
        inventory_item_id: str,
        from_location_id: str,
        to_location_id: str,
        quantity: int,
        reason: str = "other",
    ) -> dict:
        """Transfer inventory between two Shopify locations using inventoryMoveQuantities."""
        item_gid = (
            inventory_item_id
            if inventory_item_id.startswith("gid://")
            else f"gid://shopify/InventoryItem/{inventory_item_id}"
        )
        from_gid = (
            from_location_id
            if from_location_id.startswith("gid://")
            else f"gid://shopify/Location/{from_location_id}"
        )
        to_gid = (
            to_location_id
            if to_location_id.startswith("gid://")
            else f"gid://shopify/Location/{to_location_id}"
        )
        data = await self.execute(
            INVENTORY_MOVE_MUTATION,
            {
                "input": {
                    "reason": reason,
                    "moves": [
                        {
                            "inventoryItemId": item_gid,
                            "fromLocationId": from_gid,
                            "toLocationId": to_gid,
                            "quantity": quantity,
                        }
                    ],
                }
            },
        )
        result = data.get("inventoryMoveQuantities", {})
        errors = result.get("userErrors", [])
        if errors:
            raise RuntimeError(f"inventoryMoveQuantities errors: {errors}")
        return result

    async def get_inventory_levels(
        self, inventory_item_id: str, location_ids: list[str]
    ) -> list[dict]:
        gid = (
            inventory_item_id
            if inventory_item_id.startswith("gid://")
            else f"gid://shopify/InventoryItem/{inventory_item_id}"
        )
        loc_gids = [
            lid if lid.startswith("gid://") else f"gid://shopify/Location/{lid}"
            for lid in location_ids
        ]
        data = await self.execute(INVENTORY_LEVELS_QUERY, {"itemId": gid, "locationIds": loc_gids})
        item = data.get("inventoryItem") or {}
        levels_edges = item.get("inventoryLevels", {}).get("edges", [])
        return [edge["node"] for edge in levels_edges]

    async def get_inventory_item_by_sku(self, sku: str) -> dict | None:
        data = await self.execute(
            INVENTORY_ITEM_BY_SKU_QUERY, {"sku": f"sku:{sku}"}
        )
        edges = data.get("productVariants", {}).get("edges", [])
        for edge in edges:
            node = edge["node"]
            if node.get("sku") == sku:
                return node
        return None

    async def get_unfulfilled_orders_for_sku(self, sku: str) -> list[dict]:
        data = await self.execute(
            UNFULFILLED_ORDERS_QUERY,
            {"query": "fulfillment_status:unfulfilled"},
        )
        orders = []
        for edge in data.get("orders", {}).get("edges", []):
            order = edge["node"]
            for li_edge in order.get("lineItems", {}).get("edges", []):
                li = li_edge["node"]
                if li.get("sku") == sku:
                    orders.append({"id": order["id"], "name": order["name"], "quantity": li["quantity"]})
                    break
        return orders

    async def set_inventory_quantity(
        self, inventory_item_id: str, location_id: str, available: int, reason: str
    ) -> dict:
        item_gid = (
            inventory_item_id
            if inventory_item_id.startswith("gid://")
            else f"gid://shopify/InventoryItem/{inventory_item_id}"
        )
        loc_gid = (
            location_id
            if location_id.startswith("gid://")
            else f"gid://shopify/Location/{location_id}"
        )
        data = await self.execute(
            INVENTORY_SET_MUTATION,
            {
                "input": {
                    "reason": reason,
                    "setQuantities": [
                        {
                            "inventoryItemId": item_gid,
                            "locationId": loc_gid,
                            "quantity": available,
                        }
                    ],
                }
            },
        )
        result = data.get("inventorySetOnHandQuantities", {})
        errors = result.get("userErrors", [])
        if errors:
            raise RuntimeError(f"Inventory mutation errors: {errors}")
        return result

    async def add_tags_to_orders(self, order_ids: list[str], tags: list[str]) -> list[dict]:
        results = []
        for order_id in order_ids:
            gid = (
                order_id
                if order_id.startswith("gid://")
                else f"gid://shopify/Order/{order_id}"
            )
            data = await self.execute(TAGS_ADD_MUTATION, {"id": gid, "tags": tags})
            results.append(data.get("tagsAdd", {}))
        return results

    async def close(self):
        await self._client.aclose()
