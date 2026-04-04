# ADR 010 — Multi-Location Inventory Transfer

**Status:** Accepted  
**Date:** 2026-04-03

## Context

The initial implementation resolved inventory discrepancies only by adjusting the on-hand quantity at the deficient location (`inventorySetOnHandQuantities`). This is a destructive operation: it changes the absolute count without a paper trail of where inventory moved from.

In real warehouse operations, a common cause of a location-level shortage is that inventory exists at a different fulfilment location (e.g. a secondary warehouse or in-transit depot) but hasn't been transferred in Shopify yet. In this case, a raw adjustment inflates total inventory; the correct fix is a transfer between locations, which preserves total stock count and creates an accurate movement record.

## Decision

Extend the investigation and proposal nodes to detect cross-location transfer opportunities, and add a `transfer_inventory` tool that calls Shopify's `inventoryMoveQuantities` mutation.

**Changes:**

1. **`investigate` node** — after querying the primary location level, calls `get_all_inventory_levels` to fetch inventory at all locations. Stores them in `available_locations: list[{id, name, available}]` in state.

2. **`propose_resolution` node** — `_find_transfer_source` checks if any non-primary location has `available >= shortage`. If found, `proposed_action = "transfer_inventory"` with `transfer_from_location_id` and `transfer_quantity` stored in state. Raw `adjust_to_expected` is only proposed when no transfer source exists.

3. **`apply_mutation` node** — new `elif action == "transfer_inventory"` branch calls `transfer_inventory.ainvoke`. Requires the same `_approval_granted_ctx` gate as `adjust_inventory_level`.

4. **Shopify `inventoryMoveQuantities` mutation** — added to `InventoryShopifyClient` as `move_inventory(inventory_item_id, from_location_id, to_location_id, quantity, reason)`.

**Transfer source selection:** The location with the highest `available` count that can cover the full shortage. Partial transfers (multiple sources) are not supported in this iteration.

**Approval flow:** Transfer proposals go through the same human-approval interrupt as adjustments. The Slack interactive message includes `transfer_from_location` and `transfer_quantity` fields when applicable.

## Alternatives Considered

| Option | Rejected because |
|--------|-----------------|
| Always use `inventorySetOnHandQuantities` | Inflates total inventory; no movement audit trail |
| Automatic transfer without approval | Inventory transfers have cross-location financial implications; human gate is required |
| Always propose transfer if any other location has stock | Too aggressive; small buffer stock at other locations shouldn't trigger a transfer proposal |
| Multiple-source transfers | Increases mutation complexity; single-source covers the vast majority of cases |

## Consequences

- **Positive:** Resolves shortages without inflating total inventory counts when stock exists elsewhere
- **Positive:** Shopify records the movement as a proper transfer, visible in inventory history
- **Positive:** Transfer proposals include source location name in Slack approval message for operator context
- **Negative:** Adds one extra Shopify API call per investigation (`get_all_inventory_levels`), consuming ~100 rate-limit points
- **Negative:** Transfer requires both locations to have active fulfillment service assignments in Shopify; transfers to/from locations without the item tracked will fail at the mutation level (non-fatal: falls back to audit with error)
- **Negative:** Partial transfer scenarios (shortage > any single location's surplus) still fall back to `adjust_to_expected` rather than orchestrating multiple moves
