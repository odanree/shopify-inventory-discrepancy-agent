# ADR-001: Use LangGraph for Stateful Inventory Discrepancy Investigation

**Status**: Accepted  
**Date**: 2026-04-03

## Context

The inventory discrepancy workflow is a multi-step, stateful process:
1. Detect and quantify the discrepancy.
2. Investigate root cause using Shopify data and Claude.
3. Propose a resolution.
4. **Wait for human approval** (minutes to hours).
5. Execute the approved mutation.
6. Notify and audit.

The gap between step 3 and step 5 can span up to 24 hours. A stateless function cannot
survive this gap — the full intermediate state (inventory levels, open orders, LLM root
cause analysis, proposed action) must be persisted so the resume path picks up exactly
where it left off.

## Decision

Use LangGraph `StateGraph` with `interrupt_before=["apply_mutation"]`. The graph pauses
after `propose_resolution`, serializes the full `DiscrepancyState` to the checkpointer,
and returns control to the caller. `resume_workflow` uses `graph.aupdate_state` to
inject the operator's approval decision and `graph.ainvoke(None, config)` to continue.

## Consequences

**Positive**:
- The entire intermediate state is stored in a single checkpoint, eliminating the need
  for a custom state machine table or a separate pending-approvals database.
- `interrupt_before` is a first-class LangGraph feature — no custom synchronization
  primitives are needed.
- Switching checkpointers (MemorySaver → AsyncRedisSaver → AsyncPostgresSaver) requires
  only changing the `checkpointer` argument — no business logic changes.
- The compiled graph is a static DAG, inspectable with `draw_mermaid()`.

**Negative**:
- The `ainvoke(None, config)` resume API is unintuitive to developers unfamiliar with
  LangGraph's interrupt semantics.
- The in-memory `proposal_cache` in `app/main.py` duplicates some checkpoint state as
  a temporary workaround for the status endpoint. It should be replaced once
  `AsyncRedisSaver` is in place (see ADR-004).
