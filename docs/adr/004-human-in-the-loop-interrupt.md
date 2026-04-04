# ADR-004: LangGraph interrupt_before for Human-in-the-Loop Approval

**Status**: Accepted  
**Date**: 2026-04-03

## Context

Inventory mutations (adjusting Shopify inventory levels, placing open orders on hold)
are irreversible in the short term and have downstream fulfillment consequences. The
system must not execute mutations automatically without operator sign-off, particularly
for discrepancies classified as "critical" or "major".

Two implementation strategies were considered:

**Option A — Polling loop**: The `apply_mutation` node sleeps in a loop checking a
database table or Redis key for approval until granted or the timeout expires. This
blocks an asyncio coroutine for up to 24 hours, which is incompatible with FastAPI's
event loop model.

**Option B — Graph interrupt**: LangGraph's `interrupt_before` mechanism serializes
state at a defined node boundary and returns immediately. A separate resume call
continues execution after the human decision is injected.

## Decision

Use `interrupt_before=["apply_mutation"]`. The graph pauses after `propose_resolution`.
When the operator POSTs to `POST /api/approvals/{run_id}`, `graph.aupdate_state`
injects `approval_granted`, `approved_by`, and `approval_notes`, then
`graph.ainvoke(None, config)` resumes from the interrupt point.

Defense in depth: the `apply_mutation` node also checks `state["approval_granted"] is True`
directly and skips the mutation if not, independent of the interrupt mechanism.

## Consequences

**Positive**:
- No polling loop: no coroutine is blocked during the approval window.
- The approval decision and reviewer identity are stored directly in the checkpoint,
  making the full audit trail available from a single state snapshot.
- Rejection is handled naturally: `approval_granted=False` lets the graph continue to
  `notify` and `audit` while `apply_mutation` skips the mutation — no separate
  rejection code path.
- Defense in depth: the tool-level `PermissionError` guard in `adjust_inventory_level`
  ensures the mutation cannot execute even if the graph's interrupt mechanism is bypassed.

**Negative**:
- Requires a durable checkpointer to survive process restarts across the approval window.
  `MemorySaver` does not satisfy this — migration to `AsyncRedisSaver` is required before
  production use (see Item 1 in the operational roadmap).
- `graph.ainvoke(None, config)` as the resume call is an unusual API. Comments in
  `graph.py` must explain the pattern.
- No approval timeout enforcement: if an operator never responds, the workflow state
  occupies Redis until the 24-hour TTL expires. A background expiry job with reminder
  Slack notifications should be added in a future iteration.
