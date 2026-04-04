# ADR 011 — Shadow Mode for Safe Client Onboarding

**Status:** Accepted  
**Date:** 2026-04-03

## Context

The inventory discrepancy agent carries higher mutation risk than the order exception agent: an incorrect `inventorySetOnHandQuantities` or `inventoryMoveQuantities` call directly corrupts live stock counts and can cause overselling. Before deploying against a client's live store, operators and clients need a trust-building period where they can observe the full decision pipeline without write consequences.

## Decision

Add `AGENT_MODE=shadow` environment variable (default: `live`). When shadow mode is active:

- The full LangGraph pipeline executes through the approval interrupt: detect → investigate → propose → [interrupt] → apply_mutation
- In `apply_mutation`, after the approval gate passes, the shadow check fires **before** any tool call
- All Shopify write mutations (`adjust_inventory_level`, `transfer_inventory`) are **skipped**
- The intended action is logged at INFO level with `apply_mutation_shadowed` including `action`, `proposed_quantity`, `transfer_from`, `transfer_qty`
- `shadowed: True` is recorded in state; `mutation_applied: False` so `verify_mutation` trivially passes
- The notify → audit flow still runs: Slack notification and Google Sheets audit row are written
- Dashboard shows amber "SHADOW MODE" banner

**The human approval step still fires in shadow mode** — this is intentional. The approval Slack message reaching a real operator and them choosing to approve/reject is itself a trust-building data point, even though the resulting mutation is skipped.

## Alternatives Considered

| Option | Rejected because |
|--------|-----------------|
| Shadow only for high-discrepancy SKUs | Complex to configure; clients want blanket assurance |
| Separate staging Shopify store | Doesn't reflect real inventory levels or velocity |
| Dry-run via Shopify sandbox | Shopify GraphQL has no dry-run mode |
| Skip approval step in shadow mode | Defeats the purpose — approval UX is part of what clients need to evaluate |

## Consequences

- **Positive:** Clients observe the full pipeline (investigation, proposal, approval prompt) with no risk
- **Positive:** Google Sheets audit rows written in shadow mode give the client a pre-launch paper trail to review
- **Positive:** Mode switch is a single env var change; no code changes or redeployment required
- **Negative:** Human approvers must be briefed that approvals in shadow mode do not result in actual mutations
- **Negative:** `verify_mutation` always passes in shadow mode (nothing to verify); the retry/dead-letter path is not exercised
