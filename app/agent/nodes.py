"""LangGraph node implementations for the inventory discrepancy state machine."""
import structlog
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage

from app.agent.state import DiscrepancyState
from app.config import get_settings

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Event router (set by inject_event_router() in main.py)
# ---------------------------------------------------------------------------
_event_router = None


def inject_event_router(router) -> None:
    global _event_router
    _event_router = router


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


def _get_langfuse_handler(session_id: str, node_name: str, tags: list[str] | None = None):
    """Return a LangFuse CallbackHandler for the given session, or None if disabled/unavailable."""
    try:
        settings = get_settings()
        if not settings.langfuse_enabled or not settings.langfuse_public_key:
            return None
        from langfuse.callback import CallbackHandler
        return CallbackHandler(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
            trace_name=node_name,
            session_id=session_id,
            user_id="system",
            tags=tags or ["inventory-discrepancy-agent"],
        )
    except ImportError:
        return None
    except Exception as exc:
        logger.warning("langfuse_handler_init_failed", node=node_name, error=str(exc))
        return None


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

    # Query inventory levels at the primary location
    levels_result = await get_inventory_levels.ainvoke(
        {"inventory_item_id": inventory_item_id, "location_ids": [location_id]}
    )

    # Query inventory levels at ALL locations (for transfer opportunity detection)
    available_locations: list[dict] = []
    try:
        from app.agent.tools import _shopify_client
        if _shopify_client is not None:
            all_levels = await _shopify_client.get_all_inventory_levels(inventory_item_id)
            available_locations = [
                {
                    "id": lv.get("location", {}).get("id", ""),
                    "name": lv.get("location", {}).get("name", ""),
                    "available": lv.get("available", 0),
                }
                for lv in all_levels
            ]
    except Exception as exc:
        logger.warning("investigate_all_levels_failed", run_id=state["run_id"], error=str(exc))

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
        lf_handler = _get_langfuse_handler(state["run_id"], "investigate")
        invoke_config = {"callbacks": [lf_handler]} if lf_handler else {}
        response = await llm.ainvoke(messages, config=invoke_config)
        root_cause = response.content.strip()
        usage = response.usage_metadata or {}
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        if lf_handler:
            lf_handler.flush()
    except Exception as exc:
        logger.error("investigate_llm_failed", run_id=state["run_id"], error=str(exc))
        input_tokens = 0
        output_tokens = 0

    updated_log = _tool_calls_ctx.get([])
    logger.info(
        "investigation_complete",
        run_id=state["run_id"],
        open_orders=open_orders_count,
        locations_found=len(available_locations),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )

    return {
        **state,
        "recent_adjustments": adj_result.get("data"),
        "open_orders": open_orders,
        "open_orders_count": open_orders_count,
        "root_cause_analysis": root_cause,
        "available_locations": available_locations,
        "llm_input_tokens": input_tokens,
        "llm_output_tokens": output_tokens,
        "tool_calls_log": updated_log,
    }


def _find_transfer_source(
    available_locations: list[dict],
    primary_location_id: str,
    shortage: int,
) -> dict | None:
    """Return the location with the most surplus that can cover the shortage, or None."""
    primary_gid = (
        primary_location_id
        if primary_location_id.startswith("gid://")
        else f"gid://shopify/Location/{primary_location_id}"
    )
    candidates = []
    for loc in available_locations:
        loc_id = loc.get("id", "")
        if loc_id == primary_gid or primary_location_id in loc_id:
            continue  # skip the deficient location itself
        available = loc.get("available", 0)
        if available >= shortage:
            candidates.append(loc)
    if not candidates:
        return None
    # Prefer location with the most available inventory
    return max(candidates, key=lambda loc: loc.get("available", 0))


async def propose_resolution(state: DiscrepancyState) -> DiscrepancyState:
    """Generate a resolution proposal. Graph interrupts after this node for human approval."""
    from app.config import get_settings

    severity = state.get("severity", "minor")
    open_orders_count = state.get("open_orders_count", 0)
    expected = state["expected_quantity"]
    actual = state["actual_quantity"]
    shortage = expected - actual  # positive means under-stocked at primary location

    transfer_from_location_id: str | None = None
    transfer_quantity: int | None = None

    # Check for transfer opportunity: primary location is short AND another has enough
    available_locations = state.get("available_locations") or []
    transfer_source = None
    if shortage > 0 and available_locations:
        transfer_source = _find_transfer_source(
            available_locations, state["location_id"], shortage
        )

    # Rule-based proposal
    if severity == "critical":
        action = "hold_for_review"
    elif severity == "major" and open_orders_count > 5:
        action = "hold_for_review"
    elif severity in ("major", "moderate") and transfer_source:
        # Surplus available elsewhere — propose a transfer instead of a raw adjustment
        action = "transfer_inventory"
        transfer_from_location_id = transfer_source["id"]
        transfer_quantity = shortage
    elif severity in ("major", "moderate"):
        action = "adjust_to_expected"
    else:
        # minor
        action = "hold_for_review"

    proposed_quantity = expected  # target quantity at primary location after resolution

    logger.info(
        "proposal_generated",
        run_id=state["run_id"],
        action=action,
        severity=severity,
        proposed_quantity=proposed_quantity,
        transfer_source=transfer_source.get("name") if transfer_source else None,
    )

    # Emit approval request — NotificationWorker posts the Slack interactive message.
    # The agent does not block on delivery; the interrupt handles the approval window.
    if _event_router is not None:
        settings = get_settings()
        event_payload = {
            "run_id": state["run_id"],
            "sku": state["sku"],
            "discrepancy_pct": state.get("discrepancy_pct", 0.0),
            "severity": severity,
            "proposed_action": action,
            "proposed_quantity": proposed_quantity,
            "expected_quantity": expected,
            "open_orders_count": open_orders_count or 0,
            "root_cause_analysis": state.get("root_cause_analysis", ""),
            "channel": settings.slack_alerts_channel,
        }
        if transfer_source:
            event_payload["transfer_from_location"] = transfer_source.get("name", "")
            event_payload["transfer_quantity"] = transfer_quantity
        await _event_router.emit("approval_request", event_payload)

    return {
        **state,
        "proposed_action": action,
        "proposed_quantity": proposed_quantity,
        "transfer_from_location_id": transfer_from_location_id,
        "transfer_quantity": transfer_quantity,
        # approval_granted remains None — graph will interrupt before apply_mutation
    }


async def apply_mutation(state: DiscrepancyState) -> DiscrepancyState:
    """Execute the approved inventory action. Blocked entirely if approval_granted != True."""
    from app.agent.tools import (
        adjust_inventory_level,
        transfer_inventory,
        update_order_tags_for_hold,
        _approval_granted_ctx,
        _tool_calls_ctx,
    )

    run_id = state["run_id"]
    approval = state.get("approval_granted")
    action = state.get("proposed_action")

    if approval is not True:
        logger.info("apply_mutation_skipped_not_approved", run_id=run_id, approval=approval)
        return {
            **state,
            "mutation_applied": False,
            "shadowed": False,
            "mutation_result": {"skipped": True, "reason": "not_approved"},
        }

    # Shadow mode: log the full intended mutation but skip it
    settings = get_settings()
    if settings.agent_mode == "shadow":
        logger.info(
            "apply_mutation_shadowed",
            run_id=run_id,
            action=action,
            proposed_quantity=state.get("proposed_quantity"),
            transfer_from=state.get("transfer_from_location_id"),
            transfer_qty=state.get("transfer_quantity"),
        )
        return {
            **state,
            "mutation_applied": False,
            "shadowed": True,
            "mutation_result": {"shadow": True, "would_have": action},
            "tool_calls_log": state.get("tool_calls_log", []),
            "error": None,
        }

    # Set the approval context var so the tool will execute
    _approval_granted_ctx.set(True)
    _tool_calls_ctx.set(list(state.get("tool_calls_log", [])))

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

        elif action == "transfer_inventory":
            from_loc = state.get("transfer_from_location_id")
            qty = state.get("transfer_quantity")
            if not from_loc or not qty:
                raise RuntimeError(
                    "transfer_inventory: missing transfer_from_location_id or transfer_quantity"
                )
            result = await transfer_inventory.ainvoke(
                {
                    "inventory_item_id": state["inventory_item_id"],
                    "from_location_id": from_loc,
                    "to_location_id": state["location_id"],
                    "quantity": qty,
                    "reason": "Inventory rebalance — approved by operator",
                }
            )
            mutation_result = result
            if not result.get("success"):
                raise RuntimeError(result.get("error", "Transfer mutation failed"))

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
        "shadowed": False,
        "mutation_result": mutation_result,
        "tool_calls_log": updated_log,
        "error": error,
    }


async def notify(state: DiscrepancyState) -> DiscrepancyState:
    """Emit Slack resolution event and append Google Sheets audit row."""
    from app.agent.tools import append_google_sheets_row, _tool_calls_ctx
    from app.config import get_settings
    from datetime import datetime, timezone

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

    # Emit via event router (clawhip pattern) — NotificationWorker delivers to Slack
    notification_emitted = False
    if _event_router is not None:
        await _event_router.emit(
            "inventory_notification",
            {
                "channel": settings.slack_alerts_channel,
                "title": f"Inventory Discrepancy Resolved — {state['sku']}",
                "fields": slack_fields,
                "severity": "critical" if severity in ("critical", "major") else "warning",
                "run_id": run_id,
            },
        )
        notification_emitted = True

    # Google Sheets audit row (kept as tool — deterministic, not LLM-driven)
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
        "slack_notified": notification_emitted,
        "sheets_row": sheets_row,
        "tool_calls_log": updated_log,
    }


async def verify_mutation(state: DiscrepancyState) -> DiscrepancyState:
    """Re-query Shopify to confirm the inventory mutation actually took effect.

    Skips verification if mutation was not applied (approval rejected or hold action).
    For inventory adjustments, re-queries the level and compares to proposed_quantity.
    Retries apply_mutation up to 2 times if verification fails; then proceeds to notify.
    """
    from app.agent.tools import _shopify_client

    run_id = state["run_id"]
    action = state.get("proposed_action")

    # Nothing to verify if mutation was not applied (not approved or hold action)
    if not state.get("mutation_applied") or action == "hold_for_review":
        return {**state, "verification_passed": True}

    verification_passed = False
    error = None

    try:
        if _shopify_client is None:
            raise RuntimeError("Shopify client not injected")

        levels = await _shopify_client.get_inventory_levels(
            state["inventory_item_id"], [state["location_id"]]
        )
        proposed_qty = state.get("proposed_quantity", state["expected_quantity"])
        actual_qty_after = None

        for level in levels:
            loc_id = level.get("location", {}).get("id", "")
            target = state["location_id"]
            if not target.startswith("gid://"):
                target = f"gid://shopify/Location/{target}"
            if loc_id == target or state["location_id"] in loc_id:
                actual_qty_after = level.get("available")
                break

        if actual_qty_after is None:
            # Unable to locate level — treat as unverifiable and proceed
            logger.warning("verify_mutation_location_not_found", run_id=run_id)
            verification_passed = True
        elif actual_qty_after == proposed_qty:
            verification_passed = True
            logger.info(
                "verify_mutation_passed",
                run_id=run_id,
                qty=actual_qty_after,
            )
        else:
            error = (
                f"verify_mutation_qty_mismatch: "
                f"expected={proposed_qty} got={actual_qty_after}"
            )
            logger.warning(error, run_id=run_id)

    except Exception as exc:
        error = str(exc)
        logger.error("verify_mutation_query_failed", run_id=run_id, error=error)

    retry_count = state.get("retry_count", 0)
    if not verification_passed:
        retry_count += 1

    return {
        **state,
        "verification_passed": verification_passed,
        "retry_count": retry_count,
        "error": error if not verification_passed else state.get("error"),
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
                "input_tokens": state.get("llm_input_tokens"),
                "output_tokens": state.get("llm_output_tokens"),
            },
        }
    )

    # Clean up pending workflow state
    if _idempotency_service is not None:
        await _idempotency_service.delete_workflow_state(state["run_id"])

    # Capture mutation failures to Sentry
    if state.get("error") and state.get("mutation_applied") is False and not state.get("shadowed"):
        try:
            import sentry_sdk
            sentry_sdk.capture_message(
                f"Inventory mutation failed: {state['sku']}",
                level="error",
                extras={
                    "run_id": state["run_id"],
                    "sku": state["sku"],
                    "proposed_action": state.get("proposed_action"),
                    "error": state.get("error"),
                    "retry_count": state.get("retry_count", 0),
                },
            )
        except ImportError:
            pass

    updated_log = _tool_calls_ctx.get([])
    logger.info("audit_complete", run_id=state["run_id"], resolution=resolution)
    return {**state, "tool_calls_log": updated_log}
