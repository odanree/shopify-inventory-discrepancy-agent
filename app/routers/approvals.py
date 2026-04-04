"""Human-in-the-loop approval endpoints."""
import structlog
from fastapi import APIRouter, HTTPException, Request

from app.agent.graph import get_current_state, resume_workflow
from app.models.discrepancy import ApprovalRequest

logger = structlog.get_logger()

router = APIRouter(prefix="/api/approvals", tags=["approvals"])


@router.post("/{run_id}")
async def submit_approval(run_id: str, body: ApprovalRequest, request: Request):
    """Submit an approval or rejection decision for a pending workflow run.

    On approval: resumes from apply_mutation → notify → audit.
    On rejection: applies_mutation is skipped, audit still runs.
    """
    # Validate the run exists
    current = await get_current_state(run_id)
    if current is None:
        raise HTTPException(status_code=404, detail=f"Workflow run '{run_id}' not found")

    if current.get("approval_granted") is not None:
        raise HTTPException(
            status_code=409, detail=f"Run '{run_id}' already has a decision: {current.get('approval_granted')}"
        )

    logger.info(
        "approval_submitted",
        run_id=run_id,
        approved=body.approved,
        reviewer=body.reviewer_id,
    )

    try:
        final_state = await resume_workflow(
            run_id=run_id,
            approved=body.approved,
            reviewer_id=body.reviewer_id,
            notes=body.notes or "",
        )
    except Exception as exc:
        logger.error("approval_resume_failed", run_id=run_id, error=str(exc))
        raise HTTPException(status_code=500, detail=f"Failed to resume workflow: {exc}")

    status = "completed" if body.approved else "rejected"
    return {
        "run_id": run_id,
        "status": status,
        "sku": final_state.get("sku"),
        "resolution": final_state.get("proposed_action"),
        "mutation_applied": final_state.get("mutation_applied"),
        "slack_notified": final_state.get("slack_notified"),
    }


@router.get("/pending")
async def list_pending_approvals(request: Request):
    """List all workflows currently awaiting operator approval."""
    idempotency = request.app.state.idempotency
    run_ids = await idempotency.list_pending_run_ids()

    results = []
    for run_id in run_ids:
        state_data = await idempotency.get_workflow_state(run_id)
        if state_data:
            results.append(
                {
                    "run_id": run_id,
                    "status": state_data.get("status", "pending_approval"),
                    "proposal": state_data.get("proposal"),
                }
            )

    return {"pending": results, "count": len(results)}
