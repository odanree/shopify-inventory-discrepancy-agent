"""Compiled LangGraph workflow for inventory discrepancy resolution.

The graph stops (interrupts) BEFORE the apply_mutation node to require human approval.
Resume via the /api/approvals/{run_id} endpoint or the Slack interactive message.

Checkpointer is initialized at startup via init_graph(checkpointer) so that
AsyncRedisSaver can be set up asynchronously in the FastAPI lifespan context.
MemorySaver is used as a fallback when running tests (injected by conftest).
"""
from langgraph.graph import END, StateGraph

from app.agent.nodes import (
    apply_mutation,
    audit,
    detect_discrepancy,
    investigate,
    notify,
    propose_resolution,
    verify_mutation,
)
from app.agent.state import DiscrepancyState
from app.models.discrepancy import ResolutionProposal

# Module-level graph reference — set by init_graph() during app startup.
# None until init_graph() is called; accessing it before that raises AttributeError.
graph = None


def _route_after_verify(state: DiscrepancyState) -> str:
    """Route after verify_mutation:
    - passed → notify
    - failed + retries remaining → apply_mutation (cycle back)
    - failed + max retries → notify (proceed with failure recorded in state)
    """
    if state.get("verification_passed"):
        return "notify"
    if state.get("retry_count", 0) >= 2:
        return "notify"  # best effort: audit the failure rather than looping forever
    return "apply_mutation"


def build_graph(checkpointer):
    """Build and compile the LangGraph state machine with the given checkpointer.

    Args:
        checkpointer: Any LangGraph checkpointer (AsyncRedisSaver, MemorySaver, etc.)
    """
    builder = StateGraph(DiscrepancyState)

    builder.add_node("detect", detect_discrepancy)
    builder.add_node("investigate", investigate)
    builder.add_node("propose", propose_resolution)
    builder.add_node("apply_mutation", apply_mutation)
    builder.add_node("verify", verify_mutation)
    builder.add_node("notify", notify)
    builder.add_node("audit", audit)

    builder.set_entry_point("detect")
    builder.add_edge("detect", "investigate")
    builder.add_edge("investigate", "propose")
    builder.add_edge("propose", "apply_mutation")
    builder.add_edge("apply_mutation", "verify")
    builder.add_conditional_edges("verify", _route_after_verify)
    builder.add_edge("notify", "audit")
    builder.add_edge("audit", END)

    return builder.compile(
        checkpointer=checkpointer,
        interrupt_before=["apply_mutation"],
    )


def init_graph(checkpointer) -> None:
    """Called once from the FastAPI lifespan after the checkpointer is ready.

    This function sets the module-level `graph` variable so all callers
    (start_workflow, resume_workflow, get_current_state) use the same instance.
    """
    global graph
    graph = build_graph(checkpointer)


async def start_workflow(initial_state: DiscrepancyState) -> tuple[str, ResolutionProposal]:
    """Start the workflow. Runs until the interrupt before apply_mutation.

    Returns (run_id, proposal) for the operator approval UI.
    """
    if graph is None:
        raise RuntimeError("Graph not initialized. Call init_graph() first.")

    config = {"configurable": {"thread_id": initial_state["run_id"]}}
    result = await graph.ainvoke(initial_state, config=config)

    proposal = ResolutionProposal(
        run_id=initial_state["run_id"],
        sku=result.get("sku", initial_state["sku"]),
        inventory_item_id=result.get("inventory_item_id", initial_state["inventory_item_id"]),
        location_id=result.get("location_id", initial_state["location_id"]),
        discrepancy_pct=result.get("discrepancy_pct", 0.0),
        severity=result.get("severity", "unknown"),
        root_cause_analysis=result.get("root_cause_analysis", ""),
        proposed_action=result.get("proposed_action", "hold_for_review"),
        proposed_quantity=result.get("proposed_quantity", initial_state["expected_quantity"]),
        affected_orders=[o["id"] for o in (result.get("open_orders") or [])],
        estimated_impact=f"{result.get('open_orders_count', 0)} open orders affected",
    )
    return initial_state["run_id"], proposal


async def resume_workflow(
    run_id: str, approved: bool, reviewer_id: str, notes: str = ""
) -> DiscrepancyState:
    """Resume after human approval decision.

    If approved=True, the graph runs apply_mutation → verify → notify → audit.
    If approved=False, apply_mutation skips the mutation and the flow continues to audit.

    Note: graph.ainvoke(None, config) is the LangGraph API for resuming from an
    interrupt point. Passing None as the input means "continue from checkpoint state".
    """
    if graph is None:
        raise RuntimeError("Graph not initialized. Call init_graph() first.")

    config = {"configurable": {"thread_id": run_id}}

    # Inject the approval decision into the checkpointed state before resuming
    await graph.aupdate_state(
        config,
        {
            "approval_granted": approved,
            "approved_by": reviewer_id,
            "approval_notes": notes,
        },
    )

    # Resume from the interrupt point (apply_mutation node)
    result = await graph.ainvoke(None, config=config)
    return result


async def get_current_state(run_id: str) -> dict | None:
    """Return the current checkpointed state for a run, or None if not found."""
    if graph is None:
        return None
    config = {"configurable": {"thread_id": run_id}}
    snapshot = await graph.aget_state(config)
    if snapshot is None or snapshot.values is None:
        return None
    return snapshot.values
