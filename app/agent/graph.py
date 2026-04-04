"""Compiled LangGraph workflow for inventory discrepancy resolution.

The graph stops (interrupts) BEFORE the apply_mutation node to require human approval.
Resume via the /api/approvals/{run_id} endpoint.
"""
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from app.agent.nodes import (
    apply_mutation,
    audit,
    detect_discrepancy,
    investigate,
    notify,
    propose_resolution,
)
from app.agent.state import DiscrepancyState
from app.models.discrepancy import ResolutionProposal

_checkpointer = MemorySaver()


def build_graph():
    builder = StateGraph(DiscrepancyState)

    builder.add_node("detect", detect_discrepancy)
    builder.add_node("investigate", investigate)
    builder.add_node("propose", propose_resolution)
    builder.add_node("apply_mutation", apply_mutation)
    builder.add_node("notify", notify)
    builder.add_node("audit", audit)

    builder.set_entry_point("detect")
    builder.add_edge("detect", "investigate")
    builder.add_edge("investigate", "propose")
    builder.add_edge("propose", "apply_mutation")
    builder.add_edge("apply_mutation", "notify")
    builder.add_edge("notify", "audit")
    builder.add_edge("audit", END)

    return builder.compile(
        checkpointer=_checkpointer,
        interrupt_before=["apply_mutation"],
    )


graph = build_graph()


async def start_workflow(initial_state: DiscrepancyState) -> tuple[str, ResolutionProposal]:
    """Start the workflow. Runs until the interrupt before apply_mutation.

    Returns (run_id, proposal) for the operator approval UI.
    """
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

    If approved=True, the graph runs apply_mutation → notify → audit.
    If approved=False, apply_mutation is called with approval_granted=False
    (it will skip the mutation) and the flow continues to audit.
    """
    config = {"configurable": {"thread_id": run_id}}

    # Inject the approval decision into the checkpoint state
    await graph.aupdate_state(
        config,
        {
            "approval_granted": approved,
            "approved_by": reviewer_id,
            "approval_notes": notes,
        },
    )

    # Resume: ainvoke(None, config) continues from the interrupt point
    result = await graph.ainvoke(None, config=config)
    return result


async def get_current_state(run_id: str) -> dict | None:
    """Return the current checkpointed state for a run, or None if not found."""
    config = {"configurable": {"thread_id": run_id}}
    snapshot = await graph.aget_state(config)
    if snapshot is None or snapshot.values is None:
        return None
    return snapshot.values
