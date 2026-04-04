"""Discrepancy detection and workflow status endpoints."""
import uuid

import structlog
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from app.agent.graph import get_current_state, start_workflow
from app.agent.state import DiscrepancyState
from app.models.discrepancy import DiscrepancyEvent

logger = structlog.get_logger()

router = APIRouter(prefix="/api/discrepancies", tags=["discrepancies"])


async def _run_workflow(initial_state: DiscrepancyState, idempotency_svc, proposal_cache: dict):
    run_id = initial_state["run_id"]
    try:
        run_id_out, proposal = await start_workflow(initial_state)
        # Cache proposal for the status endpoint
        proposal_cache[run_id_out] = proposal.model_dump()
        # Save pending state for approval listing
        await idempotency_svc.save_workflow_state(
            run_id_out,
            {"run_id": run_id_out, "proposal": proposal.model_dump(), "status": "pending_approval"},
        )
        logger.info(
            "workflow_interrupted_awaiting_approval",
            run_id=run_id_out,
            action=proposal.proposed_action,
        )
    except Exception as exc:
        logger.error("workflow_start_failed", run_id=run_id, error=str(exc), exc_info=True)


@router.post("/detect")
async def detect_and_investigate(
    event: DiscrepancyEvent,
    request: Request,
    background_tasks: BackgroundTasks,
):
    """Start the investigation workflow for a reported discrepancy.

    Returns immediately with a run_id. The workflow runs to the approval interrupt
    in the background, then waits for a POST /api/approvals/{run_id}.
    """
    run_id = str(uuid.uuid4())

    initial_state: DiscrepancyState = {
        "run_id": run_id,
        "sku": event.sku,
        "inventory_item_id": event.inventory_item_id,
        "location_id": event.location_id,
        "expected_quantity": event.expected_quantity,
        "actual_quantity": event.actual_quantity,
        "discrepancy_pct": 0.0,
        "severity": None,
        "recent_adjustments": None,
        "open_orders": None,
        "open_orders_count": None,
        "root_cause_analysis": None,
        "proposed_action": None,
        "proposed_quantity": None,
        "approval_granted": None,
        "approved_by": None,
        "approval_notes": None,
        "mutation_applied": False,
        "mutation_result": None,
        "slack_notified": False,
        "sheets_row": None,
        "tool_calls_log": [],
        "error": None,
        "messages": [],
    }

    idempotency = request.app.state.idempotency
    proposal_cache = getattr(request.app.state, "proposal_cache", {})
    if not hasattr(request.app.state, "proposal_cache"):
        request.app.state.proposal_cache = proposal_cache

    background_tasks.add_task(_run_workflow, initial_state, idempotency, proposal_cache)

    logger.info("discrepancy_workflow_started", run_id=run_id, sku=event.sku)
    return {"run_id": run_id, "status": "investigating", "sku": event.sku}


@router.get("/{run_id}")
async def get_workflow_status(run_id: str, request: Request):
    """Return the current state of a workflow run."""
    # Try checkpointer first
    current = await get_current_state(run_id)
    if current is not None:
        proposal_cache = getattr(request.app.state, "proposal_cache", {})
        proposal = proposal_cache.get(run_id)
        return {
            "run_id": run_id,
            "status": "pending_approval" if current.get("approval_granted") is None else "completed",
            "severity": current.get("severity"),
            "proposed_action": current.get("proposed_action"),
            "proposal": proposal,
        }

    # Try Redis
    idempotency = request.app.state.idempotency
    state_data = await idempotency.get_workflow_state(run_id)
    if state_data:
        return state_data

    raise HTTPException(status_code=404, detail=f"Workflow run '{run_id}' not found")
