# ADR 005 — Reviewer Node for Mutation Verification

**Status:** Accepted  
**Date:** 2026-04-03

## Context

After `apply_mutation` executes (inventory quantity adjustment or order hold tagging), there was no confirmation that the Shopify inventory actually reflected the new value. The Shopify `inventorySetOnHandQuantities` mutation can return without `userErrors` but still not propagate if a downstream sync conflict occurs.

Since inventory discrepancy corrections are the central purpose of this agent — and human approval was required to authorize them — it is critical to confirm the change landed before writing the audit record.

## Decision

Add a `verify_mutation` node between `apply_mutation` and `notify`. The node:

1. Skips verification if `mutation_applied` is False (approval was rejected) or if the action was `hold_for_review` (tag state is lower risk to verify).
2. Re-queries `get_inventory_levels` for the item and location.
3. Compares the returned `available` quantity to `proposed_quantity`.
4. If they match → `verification_passed = True` → proceeds to `notify`.
5. If they do not match and `retry_count < 2` → retries `apply_mutation`.
6. If retries exhausted or location not found in response → proceeds to `notify` with failure noted in state (best-effort audit).

The routing function `_route_after_verify` drives this logic.

## Consequences

**Positive:**
- The audit record reflects verified ground truth, not optimistic mutation results.
- Operator trust in the agent is higher when the audit log explicitly records verification status.
- Retry loop is bounded at 2 retries, preventing infinite cycles.

**Negative:**
- One additional Shopify API read per approved workflow (rate-limit cost).
- Location-not-found case is treated as "unverifiable and proceeding" rather than an error, which could mask configuration mistakes in `location_id`.
- `hold_for_review` actions are not verified (order tag state). This is an accepted trade-off; tagging is lower-risk than quantity adjustments.
