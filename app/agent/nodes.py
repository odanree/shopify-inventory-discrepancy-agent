"""LangGraph node implementations for the inventory discrepancy state machine."""
import structlog
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage

from app.agent.state import DiscrepancyState
from app.config import get_settings

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Severity thresholds
# ---------------------------------------------------------------------------

SEVERITY_THRESHOLDS = {
    "critical": 50.0,
    "major": 20.0,
    "moderate": 5.0,
    "minor": 0.0,
}

ROOT_CAUSE_SYSTEM = """You are an inventory analyst for an e-commerce operations team.
Given inventory discrepancy data and recent adjustment context, write a concise 2-3 sentence
root cause analysis. Focus on the most likely explanation (e.g., uncounted shrinkage,
in-transit orders not reflected, data sync lag, fulfillment error).
Be specific and actionable. Do not repeat the numbers — the operator already has them."""


def _classify_severity(discrepancy_pct: float) -> str:
    if discrepancy_pct >= SEVERITY_THRESHOLDS["critical"]:
        return "critical"
    if discrepancy_pct >= SEVERITY_THRESHOLDS["major"]:
        return "major"
    if discrepancy_pct >= SEVERITY_THRESHOLDS["moderate"]:
        return "moderate"
    return "minor"


def _get_llm():
    settings = get_settings()
    return ChatAnthropic(
        model=settings.agent_model,
        api_key=settings.anthropic_api_key,
        max_tokens=512,
    )


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

async def detect_discrepancy(state: DiscrepancyState) -> DiscrepancyState:
    """Calculate discrepancy percentage and classify severity."""
    expected = state["expected_quantity"]
    actual = state["actual_quantity"]
    discrepancy_pct = abs(expected - actual) / max(expected, 1) * 100
    severity = _classify_severity(discrepancy_pct)

    logger.info(
        "discrepancy_detected",
        run_id=state["run_id"],
        sku=state["sku"],
        expected=expected,
        actual=actual,
        discrepancy_pct=round(discrepancy_pct, 2),
        severity=severity,
    )

    return {
        **state,
        "discrepancy_pct": round(discrepancy_pct, 2),
        "severity": severity,
    }


async def investigate(state: DiscrepancyState) -> DiscrepancyState:
    """Query Shopify for context and generate root cause analysis with Claude."""
    from app.agent.tools import (
        get_inventory_levels,
        get_open_orders_for_sku,
        get_recent_adjustments,
        _tool_calls_ctx,
    )

    sku = state["sku"]
    inventory_item_id = state["inventory_item_id"]
    location_id = state["location_id"]

    _tool_calls_ctx.set(list(state.get("tool_calls_log", [])))

    # Query inventory levels
    levels_result = await get_inventory_levels.ainvoke(
        {"inventory_item_id": inventory_item_id, "location_ids": [location_id]}
    )

    # Query recent adjustments
    adj_result = await get_recent_adjustments.ainvoke(
        {"inventory_item_id": inventory_item_id, "since_days": 7}
    )

    # Query open orders
    orders_result = await get_open_orders_for_sku.ainvoke({"sku": sku})
    open_orders = orders_result.get("data", []) if orders_result.get("success") else []
    open_orders_count = orders_result.get("count", 0)

    # Generate root cause analysis with Claude
    root_cause = "Unable to determine root cause — LLM unavailable."
    try:
        prompt = (
            f"SKU: {sku}\n"
            f"Expected quantity: {state['expected_quantity']}\n"
            f"Actual quantity: {state['actual_quantity']}\n"
            f"Discrepancy: {state['discrepancy_pct']}% ({state['severity']})\n"
            f"Open unfulfilled orders for this SKU: {open_orders_count}\n"
            f"Current inventory levels from Shopify: {levels_result.get('data', 'unavailable')}\n"
            f"Recent adjustment context: {adj_result.get('data', 'unavailable')}\n"
        )
        llm = _get_llm()
        messages = [
            SystemMessage(content=ROOT_CAUSE_SYSTEM),
            HumanMessage(content=prompt),
        ]
        response = await llm.ainvoke(messages)
        root_cause = response.content.strip()
    except Exception as exc:
        logger.error("investigate_llm_failed", run_id=state["run_id"], error=str(exc))

    updated_log = _tool_calls_ctx.get([])
    logger.info(
        "investigation_complete",
        run_id=state["run_id"],
        open_orders=open_orders_count,
    )

    return {
        **state,
        "recent_adjustments": adj_result.get("data"),
        "open_orders": open_orders,
        "open_orders_count": open_orders_count,
        "root_cause_analysis": root_cause,
        "tool_calls_log": updated_log,
    }


async def propose_resolution(state: DiscrepancyState) -> DiscrepancyState:
    """Generate a resolution proposal. Graph interrupts after this node for human approval."""
    severity = state.get("severity", "minor")
    open_orders_count = state.get("open_orders_count", 0)
    expected = state["expected_quantity"]

    # Rule-based proposal
    if severity == "critical":
        action = "hold_for_review"
    elif severity == "major" and open_orders_count > 5:
        action = "hold_for_review"
    elif severity == "major":
        action = "adjust_to_expected"
    elif severity == "moderate":
        action = "adjust_to_expected"
    else:
        # minor
        action = "hold_for_review"

    proposed_quantity = expected  # default: restore to expected

    logger.info(
        "proposal_generated",
        run_id=state["run_id"],
        action=action,
        severity=severity,
        proposed_quantity=proposed_quantity,
    )

    return {
        **state,
        "proposed_action": action,
        "proposed_quantity": proposed_quantity,
        # approval_granted remains None — graph will interrupt before apply_mutation
    }


async def apply_mutation(state: DiscrepancyState) -> DiscrepancyState:
    """Execute the approved inventory action. Blocked entirely if approval_granted != True."""
    from app.agent.tools import (
        adjust_inventory_level,
        update_order_tags_for_hold,
        _approval_granted_ctx,
        _tool_calls_ctx,
    )

    run_id = state["run_id"]
    approval = state.get("approval_granted")

    if approval is not True:
        logger.info("apply_mutation_skipped_not_approved", run_id=run_id, approval=approval)
        return {
            **state,
            "mutation_applied": False,
            "mutation_result": {"skipped": True, "reason": "not_approved"},
        }

    # Set the approval context var so the tool will execute
    _approval_granted_ctx.set(True)
    _tool_calls_ctx.set(list(state.get("tool_calls_log", [])))

    action = state.get("proposed_action")
    error = None
    mutation_result = None

    try:
        if action == "adjust_to_expected":
            result = await adjust_inventory_level.ainvoke(
                {
                    "inventory_item_id": state["inventory_item_id"],
                    "location_id": state["location_id"],
                    "available_quantity": state.get("proposed_quantity", state["expected_quantity"]),
                    "reason": "Inventory discrepancy correction — approved by operator",
                }
            )
            mutation_result = result
            if not result.get("success"):
                raise RuntimeError(result.get("error", "Mutation failed"))

        elif action == "hold_for_review":
            # Tag open orders as on-hold
            open_orders = state.get("open_orders") or []
            order_ids = [o["id"] for o in open_orders if "id" in o]
            if order_ids:
                result = await update_order_tags_for_hold.ainvoke(
                    {"order_ids": order_ids, "tags": ["inventory-hold", f"run:{run_id}"]}
                )
                mutation_result = result

        elif action == "adjust_to_erp":
            # Treated same as adjust_to_expected for now
            result = await adjust_inventory_level.ainvoke(
                {
                    "inventory_item_id": state["inventory_item_id"],
                    "location_id": state["location_id"],
                    "available_quantity": state.get("proposed_quantity", state["expected_quantity"]),
                    "reason": "ERP-aligned inventory correction — approved by operator",
                }
            )
            mutation_result = result
            if not result.get("success"):
                raise RuntimeError(result.get("error", "Mutation failed"))

    except PermissionError as exc:
        # Should never happen since we set _approval_granted_ctx above, but safety net
        error = str(exc)
        logger.error("apply_mutation_permission_error", run_id=run_id, error=error)
    except Exception as exc:
        error = str(exc)
        logger.error("apply_mutation_failed", run_id=run_id, error=error)

    updated_log = _tool_calls_ctx.get([])
    return {
        **state,
        "mutation_applied": error is None,
        "mutation_result": mutation_result,
        "tool_calls_log": updated_log,
        "error": error,
    }


async def notify(state: DiscrepancyState) -> DiscrepancyState:
    """Send Slack alert and append Google Sheets audit row."""
    from app.agent.tools import append_google_sheets_row, post_slack_notification, _tool_calls_ctx
    from app.config import get_settings

    _tool_calls_ctx.set(list(state.get("tool_calls_log", [])))
    settings = get_settings()
    run_id = state["run_id"]
    approval_str = "Approved" if state.get("approval_granted") else "Rejected"
    action = state.get("proposed_action", "unknown")
    severity = state.get("severity", "unknown")

    slack_fields = {
        "SKU": state["sku"],
        "Discrepancy": f"{state['discrepancy_pct']}% ({severity})",
        "Action Taken": action,
        "Approval": approval_str,
        "Approved By": state.get("approved_by") or "—",
    }

    slack_result = await post_slack_notification.ainvoke(
        {
            "channel": settings.slack_alerts_channel,
            "title": f"Inventory Discrepancy Resolved — {state['sku']}",
            "fields": slack_fields,
            "severity": "critical" if severity in ("critical", "major") else "warning",
            "run_id": run_id,
        }
    )

    # Google Sheets audit row: [run_id, sku, expected, actual, discrepancy_pct, action, approved_by, notes, timestamp]
    from datetime import datetime, timezone

    sheets_values = [
        run_id,
        state["sku"],
        state["expected_quantity"],
        state["actual_quantity"],
        state["discrepancy_pct"],
        severity,
        action,
        approval_str,
        state.get("approved_by") or "",
        state.get("approval_notes") or "",
        state.get("root_cause_analysis") or "",
        datetime.now(timezone.utc).isoformat(),
    ]

    sheets_result = await append_google_sheets_row.ainvoke(
        {"spreadsheet_id": settings.audit_spreadsheet_id, "values": sheets_values}
    )

    sheets_row = None
    if sheets_result.get("success"):
        sheets_row = str(sheets_result.get("data", {}).get("updates", {}).get("updatedRange", ""))

    updated_log = _tool_calls_ctx.get([])
    return {
        **state,
        "slack_notified": bool(slack_result.get("success")),
        "sheets_row": sheets_row,
        "tool_calls_log": updated_log,
    }


async def audit(state: DiscrepancyState) -> DiscrepancyState:
    """Write final PostgreSQL audit record and clean up Redis workflow state."""
    from app.agent.tools import write_audit_record, _tool_calls_ctx
    from app.agent.tools import _idempotency_service

    _tool_calls_ctx.set(list(state.get("tool_calls_log", [])))

    resolution = state.get("proposed_action") or "unknown"
    if state.get("approval_granted") is False:
        resolution = "rejected"

    await write_audit_record.ainvoke(
        {
            "sku": state["sku"],
            "discrepancy_pct": state["discrepancy_pct"],
            "resolution": resolution,
            "approved_by": state.get("approved_by") or "",
            "metadata": {
                "run_id": state["run_id"],
                "inventory_item_id": state["inventory_item_id"],
                "location_id": state["location_id"],
                "expected_quantity": state["expected_quantity"],
                "actual_quantity": state["actual_quantity"],
                "root_cause_analysis": state.get("root_cause_analysis"),
                "proposed_action": state.get("proposed_action"),
                "approval_granted": state.get("approval_granted"),
                "approval_notes": state.get("approval_notes"),
                "sheets_row": state.get("sheets_row"),
            },
        }
    )

    # Clean up pending workflow state
    if _idempotency_service is not None:
        await _idempotency_service.delete_workflow_state(state["run_id"])

    updated_log = _tool_calls_ctx.get([])
    logger.info("audit_complete", run_id=state["run_id"], resolution=resolution)
    return {**state, "tool_calls_log": updated_log}
