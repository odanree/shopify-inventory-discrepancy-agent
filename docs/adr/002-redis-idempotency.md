# ADR-002: Redis SET NX for Workflow and Webhook Idempotency

**Status**: Accepted  
**Date**: 2026-04-03

## Context

The inventory discrepancy agent can be triggered from three sources: manual API calls,
Shopify `inventory_levels/update` webhooks, and a scheduled reconciliation job. Without
idempotency controls, the same discrepancy could spawn multiple concurrent investigation
workflows for the same `(inventory_item_id, location_id)` combination, leading to
duplicate Slack alerts, conflicting inventory mutations, and bloated audit logs.

## Decision

Use `IdempotencyService` (backed by Redis `SET NX EX`) for two purposes:

1. **Webhook deduplication**: the `X-Shopify-Webhook-Id` header is used as the key
   (TTL: 3600 seconds), matching the order exception agent pattern.

2. **Workflow state tracking**: `save_workflow_state(run_id, ...)` stores the pending
   proposal under `workflow:state:{run_id}` and a tracking key under
   `workflow:pending:{run_id}`, both with 24-hour TTL matching `approval_expiry_hours`.
   `delete_workflow_state` in the `audit` node cleans up on completion.

3. **Inventory mutation guard**: `adjust_inventory_level` tool checks
   `SET NX EX 3600 shopify:inventory:mutate:{item}:{location}:{quantity}` before
   calling the Shopify mutation, preventing duplicate adjustments on graph retry.

## Consequences

**Positive**:
- `SET NX` prevents duplicate workflow creation even under concurrent webhook delivery.
- 24-hour TTL on workflow state auto-expires unapproved workflows, preventing unbounded
  growth.

**Negative**:
- `list_pending_run_ids` uses `KEYS workflow:pending:*`, which is O(N) and blocks the
  Redis event loop. At higher scale, replace with `SCAN`-based iteration or a Redis Set
  of pending run IDs.
- No deduplication across the scheduler and webhook trigger: if both fire for the same
  item simultaneously, two workflows are created. A check against the pending set before
  firing should be added to the scheduler.
