"""Agent tools for the inventory discrepancy pipeline.

Services are injected at startup via inject_tool_dependencies().
The approval_granted context var is set in apply_mutation node before tool execution.
"""
import contextvars
from datetime import datetime, timezone
from typing import Any

import structlog
from langchain_core.tools import tool
from pydantic import BaseModel, Field

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Injected service references
# ---------------------------------------------------------------------------
_shopify_client = None
_slack_client = None
_sheets_client = None
_db_factory = None
_idempotency_service = None
_redis_client = None

# Context var: must be True before adjust_inventory_level will execute
_approval_granted_ctx: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_approval_granted_ctx", default=False
)

# Context var for per-request tool call log
_tool_calls_ctx: contextvars.ContextVar[list[dict]] = contextvars.ContextVar(
    "_tool_calls_ctx", default=[]
)


def inject_tool_dependencies(shopify, slack, sheets, db_factory, idempotency, redis=None):
    global _shopify_client, _slack_client, _sheets_client, _db_factory
    global _idempotency_service, _redis_client
    _shopify_client = shopify
    _slack_client = slack
    _sheets_client = sheets
    _db_factory = db_factory
    _idempotency_service = idempotency
    _redis_client = redis


def _log_call(tool_name: str, args: dict, result: Any, success: bool):
    entry = {
        "tool": tool_name,
        "args": args,
        "result": result,
        "timestamp_iso": datetime.now(timezone.utc).isoformat(),
        "success": success,
    }
    calls = _tool_calls_ctx.get([])
    _tool_calls_ctx.set(calls + [entry])
    return entry


# ---------------------------------------------------------------------------
# Tool arg schemas
# ---------------------------------------------------------------------------

class GetInventoryLevelsArgs(BaseModel):
    inventory_item_id: str
    location_ids: list[str] = Field(default_factory=list)


class GetRecentAdjustmentsArgs(BaseModel):
    inventory_item_id: str
    since_days: int = 7


class GetOpenOrdersArgs(BaseModel):
    sku: str


class AdjustInventoryArgs(BaseModel):
    inventory_item_id: str
    location_id: str
    available_quantity: int
    reason: str = "correction"


class UpdateOrderTagsHoldArgs(BaseModel):
    order_ids: list[str]
    tags: list[str]


class PostSlackArgs(BaseModel):
    channel: str
    title: str
    fields: dict = Field(default_factory=dict)
    severity: str = "info"
    run_id: str = ""


class AppendSheetsRowArgs(BaseModel):
    spreadsheet_id: str
    values: list


class WriteAuditRecordArgs(BaseModel):
    sku: str
    discrepancy_pct: float
    resolution: str
    approved_by: str = ""
    metadata: dict = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@tool(args_schema=GetInventoryLevelsArgs)
async def get_inventory_levels(inventory_item_id: str, location_ids: list[str]) -> dict:
    """Fetch current inventory levels for an item across locations."""
    args = {"inventory_item_id": inventory_item_id, "location_ids": location_ids}
    try:
        if _shopify_client is None:
            raise RuntimeError("Shopify client not injected")
        levels = await _shopify_client.get_inventory_levels(inventory_item_id, location_ids)
        result = {"success": True, "data": levels}
        _log_call("get_inventory_levels", args, result, True)
        return result
    except Exception as exc:
        result = {"success": False, "error": str(exc)}
        _log_call("get_inventory_levels", args, result, False)
        return result


@tool(args_schema=GetRecentAdjustmentsArgs)
async def get_recent_adjustments(inventory_item_id: str, since_days: int = 7) -> dict:
    """Get recent inventory adjustment history for an item."""
    args = {"inventory_item_id": inventory_item_id, "since_days": since_days}
    try:
        if _shopify_client is None:
            raise RuntimeError("Shopify client not injected")
        # Shopify doesn't have a direct adjustment history endpoint in GraphQL 2024-01;
        # we return the current levels as a proxy for investigation
        levels = await _shopify_client.get_inventory_levels(inventory_item_id, [])
        result = {"success": True, "data": {"levels": levels, "since_days": since_days}}
        _log_call("get_recent_adjustments", args, result, True)
        return result
    except Exception as exc:
        result = {"success": False, "error": str(exc)}
        _log_call("get_recent_adjustments", args, result, False)
        return result


@tool(args_schema=GetOpenOrdersArgs)
async def get_open_orders_for_sku(sku: str) -> dict:
    """Count unfulfilled orders that include a given SKU."""
    args = {"sku": sku}
    try:
        if _shopify_client is None:
            raise RuntimeError("Shopify client not injected")
        orders = await _shopify_client.get_unfulfilled_orders_for_sku(sku)
        result = {"success": True, "data": orders, "count": len(orders)}
        _log_call("get_open_orders_for_sku", args, result, True)
        return result
    except Exception as exc:
        result = {"success": False, "error": str(exc), "count": 0}
        _log_call("get_open_orders_for_sku", args, result, False)
        return result


@tool(args_schema=AdjustInventoryArgs)
async def adjust_inventory_level(
    inventory_item_id: str, location_id: str, available_quantity: int, reason: str = "correction"
) -> dict:
    """Apply an inventory quantity adjustment in Shopify. REQUIRES prior human approval."""
    args = {
        "inventory_item_id": inventory_item_id,
        "location_id": location_id,
        "available_quantity": available_quantity,
        "reason": reason,
    }

    # Safety gate — refuse to execute without explicit approval
    if not _approval_granted_ctx.get(False):
        raise PermissionError(
            "adjust_inventory_level requires approval_granted=True. "
            "This tool must only be called after human approval."
        )

    # Idempotency guard
    idempotency_key = f"shopify:inventory:mutate:{inventory_item_id}:{location_id}:{available_quantity}"
    if _idempotency_service is not None:
        is_new = await _idempotency_service.check_and_set(idempotency_key, ttl_seconds=3600)
        if not is_new:
            logger.info("inventory_mutation_deduped", key=idempotency_key)
            result = {"success": True, "deduped": True}
            _log_call("adjust_inventory_level", args, result, True)
            return result

    try:
        if _shopify_client is None:
            raise RuntimeError("Shopify client not injected")
        mutation_result = await _shopify_client.set_inventory_quantity(
            inventory_item_id, location_id, available_quantity, reason
        )
        result = {"success": True, "data": mutation_result}
        _log_call("adjust_inventory_level", args, result, True)
        return result
    except Exception as exc:
        result = {"success": False, "error": str(exc)}
        _log_call("adjust_inventory_level", args, result, False)
        return result


@tool(args_schema=UpdateOrderTagsHoldArgs)
async def update_order_tags_for_hold(order_ids: list[str], tags: list[str]) -> dict:
    """Tag a list of orders as on-hold."""
    args = {"order_ids": order_ids, "tags": tags}
    try:
        if _shopify_client is None:
            raise RuntimeError("Shopify client not injected")
        results = await _shopify_client.add_tags_to_orders(order_ids, tags)
        result = {"success": True, "data": results}
        _log_call("update_order_tags_for_hold", args, result, True)
        return result
    except Exception as exc:
        result = {"success": False, "error": str(exc)}
        _log_call("update_order_tags_for_hold", args, result, False)
        return result


@tool(args_schema=PostSlackArgs)
async def post_slack_notification(
    channel: str, title: str, fields: dict, severity: str = "info", run_id: str = ""
) -> dict:
    """Post a Slack Block Kit inventory alert."""
    args = {"channel": channel, "title": title, "severity": severity, "run_id": run_id}
    try:
        if _slack_client is None:
            raise RuntimeError("Slack client not injected")
        success = await _slack_client.post_inventory_alert(
            channel=channel,
            title=title,
            fields=fields,
            severity=severity,
            run_id=run_id,
            redis_client=_redis_client,
        )
        result = {"success": success}
        _log_call("post_slack_notification", args, result, success)
        return result
    except Exception as exc:
        result = {"success": False, "error": str(exc)}
        _log_call("post_slack_notification", args, result, False)
        return result


@tool(args_schema=AppendSheetsRowArgs)
async def append_google_sheets_row(spreadsheet_id: str, values: list) -> dict:
    """Append an audit row to Google Sheets."""
    args = {"spreadsheet_id": spreadsheet_id, "values": values}
    try:
        if _sheets_client is None:
            raise RuntimeError("Google Sheets client not injected")
        # Idempotency: check if run_id already exists
        run_id = values[0] if values else ""
        if run_id and _sheets_client._spreadsheet_id:
            existing_row = await _sheets_client.find_row_by_run_id(run_id)
            if existing_row is not None:
                logger.info("sheets_row_deduped", run_id=run_id, row=existing_row)
                result = {"success": True, "deduped": True, "row": existing_row}
                _log_call("append_google_sheets_row", args, result, True)
                return result

        sheet_result = await _sheets_client.append_row(values)
        result = {"success": True, "data": sheet_result}
        _log_call("append_google_sheets_row", args, result, True)
        return result
    except Exception as exc:
        result = {"success": False, "error": str(exc)}
        _log_call("append_google_sheets_row", args, result, False)
        return result


@tool(args_schema=WriteAuditRecordArgs)
async def write_audit_record(
    sku: str, discrepancy_pct: float, resolution: str, approved_by: str = "", metadata: dict = None
) -> dict:
    """Write the final audit record to PostgreSQL."""
    if metadata is None:
        metadata = {}
    args = {
        "sku": sku,
        "discrepancy_pct": discrepancy_pct,
        "resolution": resolution,
        "approved_by": approved_by,
        "metadata": metadata,
    }
    try:
        if _db_factory is None:
            raise RuntimeError("DB factory not injected")
        from app.models.db import DiscrepancyAuditLog
        from datetime import timezone

        async with _db_factory() as session:
            record = DiscrepancyAuditLog(
                run_id=metadata.get("run_id", "unknown"),
                sku=sku,
                inventory_item_id=metadata.get("inventory_item_id", ""),
                location_id=metadata.get("location_id", ""),
                expected_qty=metadata.get("expected_quantity", 0),
                actual_qty=metadata.get("actual_quantity", 0),
                discrepancy_pct=discrepancy_pct,
                root_cause=metadata.get("root_cause_analysis"),
                proposed_action=metadata.get("proposed_action"),
                approved=metadata.get("approval_granted"),
                approved_by=approved_by or None,
                resolution_applied=resolution,
                resolution_notes=metadata.get("approval_notes"),
                google_sheets_row_id=metadata.get("sheets_row"),
                resolved_at=datetime.now(timezone.utc) if resolution != "pending" else None,
            )
            session.add(record)
            await session.commit()
            await session.refresh(record)

        result = {"success": True, "audit_id": str(record.id)}
        _log_call("write_audit_record", args, result, True)
        return result
    except Exception as exc:
        result = {"success": False, "error": str(exc)}
        _log_call("write_audit_record", args, result, False)
        return result


ALL_TOOLS = [
    get_inventory_levels,
    get_recent_adjustments,
    get_open_orders_for_sku,
    adjust_inventory_level,
    update_order_tags_for_hold,
    post_slack_notification,
    append_google_sheets_row,
    write_audit_record,
]
