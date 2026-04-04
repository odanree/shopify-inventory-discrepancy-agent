"""Shopify inventory_levels/update webhook endpoint.

Shopify fires this when any inventory level changes. We compare the incoming
`available` quantity against a stored baseline and fire the investigation
workflow if the discrepancy exceeds settings.discrepancy_threshold_pct.

Baseline resolution order:
1. Redis key `inventory:baseline:{inventory_item_id}:{location_id}` (set by operator)
2. `previous_quantity` field in the webhook payload (Shopify's previous value)
3. If neither is present: skip (no baseline to compare against)
"""
import base64
import hashlib
import hmac
import json
import time
import uuid

import structlog
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from app.config import get_settings
from app.services import kill_switch

logger = structlog.get_logger()

router = APIRouter(prefix="/api/webhooks", tags=["inventory-webhook"])


async def _verify_hmac(request: Request, settings) -> dict:
    """Inline HMAC-SHA256 verification (no shared middleware yet in this project)."""
    raw_body = await request.body()
    signature_header = request.headers.get("X-Shopify-Hmac-Sha256", "")
    if not signature_header:
        raise HTTPException(status_code=401, detail="Missing webhook signature")

    secret = settings.shopify_webhook_secret.encode("utf-8")
    computed = base64.b64encode(
        hmac.new(secret, raw_body, hashlib.sha256).digest()
    ).decode()

    if not hmac.compare_digest(computed, signature_header):
        logger.warning(
            "inventory_webhook_invalid_signature",
            shop=request.headers.get("X-Shopify-Shop-Domain", "unknown"),
        )
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    return json.loads(raw_body)


async def _resolve_baseline(redis_client, inventory_item_id: str, location_id: str) -> int | None:
    """Fetch operator-set baseline from Redis, if present."""
    key = f"inventory:baseline:{inventory_item_id}:{location_id}"
    raw = await redis_client.get(key)
    return int(raw) if raw is not None else None


async def _run_discrepancy_workflow(
    sku: str,
    inventory_item_id: str,
    location_id: str,
    expected: int,
    actual: int,
    discrepancy_pct: float,
    idempotency_svc,
    proposal_cache: dict,
):
    from app.agent.graph import start_workflow
    from app.agent.state import DiscrepancyState

    run_id = str(uuid.uuid4())
    initial_state: DiscrepancyState = {
        "run_id": run_id,
        "sku": sku,
        "inventory_item_id": inventory_item_id,
        "location_id": location_id,
        "expected_quantity": expected,
        "actual_quantity": actual,
        "discrepancy_pct": 0.0,
        "severity": None,
        "available_locations": None,
        "recent_adjustments": None,
        "open_orders": None,
        "open_orders_count": None,
        "root_cause_analysis": None,
        "proposed_action": None,
        "proposed_quantity": None,
        "transfer_from_location_id": None,
        "transfer_quantity": None,
        "approval_granted": None,
        "approved_by": None,
        "approval_notes": None,
        "mutation_applied": False,
        "mutation_result": None,
        "verification_passed": None,
        "retry_count": 0,
        "shadowed": None,
        "slack_notified": False,
        "sheets_row": None,
        "tool_calls_log": [],
        "error": None,
        "messages": [],
    }
    try:
        run_id_out, proposal = await start_workflow(initial_state)
        proposal_cache[run_id_out] = proposal.model_dump()
        await idempotency_svc.save_workflow_state(
            run_id_out,
            {"run_id": run_id_out, "proposal": proposal.model_dump(), "status": "pending_approval"},
        )
        logger.info(
            "inventory_workflow_started",
            run_id=run_id_out,
            sku=sku,
            discrepancy_pct=discrepancy_pct,
        )
    except Exception as exc:
        logger.error("inventory_workflow_failed", sku=sku, error=str(exc), exc_info=True)


@router.post("/inventory-levels/update")
async def inventory_level_updated(
    request: Request,
    background_tasks: BackgroundTasks,
):
    """Handle Shopify inventory_levels/update webhooks.

    Returns 200 immediately; discrepancy investigation (if triggered) runs in background.
    If SHOPIFY_WEBHOOK_SECRET is empty (dev/test), HMAC verification is skipped.
    """
    settings = get_settings()

    webhook_id = request.headers.get("X-Shopify-Webhook-Id", f"no-id-{time.time()}")

    # Skip HMAC check if secret not configured (local dev without ngrok)
    if settings.shopify_webhook_secret:
        payload = await _verify_hmac(request, settings)
    else:
        raw_body = await request.body()
        payload = json.loads(raw_body)
        logger.warning("inventory_webhook_hmac_skipped", reason="SHOPIFY_WEBHOOK_SECRET not set")

    # Kill switch check
    settings_obj = get_settings()
    if not await kill_switch.is_enabled(request.app.state.redis, settings_obj.shopify_shop_domain):
        logger.warning("kill_switch_active", event="inventory_levels/update")
        return {"status": "accepted", "action": "suppressed_kill_switch"}

    # Idempotency check
    idempotency = request.app.state.idempotency
    from app.services.idempotency import IdempotencyService
    is_new = await idempotency.check_and_set(
        f"shopify:webhook:{webhook_id}", ttl_seconds=3600
    )
    if not is_new:
        return {"status": "duplicate", "webhook_id": webhook_id}

    inventory_item_id = str(payload.get("inventory_item_id", ""))
    location_id = str(payload.get("location_id", ""))
    actual = int(payload.get("available", 0))
    sku = payload.get("sku", "")

    # Resolve baseline
    redis = request.app.state.redis
    baseline = await _resolve_baseline(redis, inventory_item_id, location_id)
    if baseline is None:
        previous = payload.get("previous_quantity")
        if previous is None:
            logger.info(
                "inventory_webhook_no_baseline",
                inventory_item_id=inventory_item_id,
                location_id=location_id,
            )
            return {"status": "accepted", "action": "skipped_no_baseline"}
        baseline = int(previous)

    discrepancy_pct = abs(baseline - actual) / max(baseline, 1) * 100

    if discrepancy_pct < settings.discrepancy_threshold_pct:
        logger.info(
            "inventory_webhook_below_threshold",
            discrepancy_pct=round(discrepancy_pct, 2),
            threshold=settings.discrepancy_threshold_pct,
        )
        return {"status": "accepted", "action": "below_threshold", "discrepancy_pct": round(discrepancy_pct, 2)}

    # If SKU is missing from the payload, try to fetch it from Shopify
    if not sku:
        try:
            shopify = request.app.state.shopify
            item = await shopify.get_inventory_item_by_sku.__wrapped__(inventory_item_id)
            sku = (item or {}).get("sku", inventory_item_id)
        except Exception:
            sku = inventory_item_id  # fallback to item ID as identifier

    proposal_cache = getattr(request.app.state, "proposal_cache", {})

    background_tasks.add_task(
        _run_discrepancy_workflow,
        sku=sku,
        inventory_item_id=inventory_item_id,
        location_id=location_id,
        expected=baseline,
        actual=actual,
        discrepancy_pct=round(discrepancy_pct, 2),
        idempotency_svc=idempotency,
        proposal_cache=proposal_cache,
    )

    logger.info(
        "inventory_discrepancy_detected",
        sku=sku,
        expected=baseline,
        actual=actual,
        discrepancy_pct=round(discrepancy_pct, 2),
    )
    return {
        "status": "accepted",
        "action": "workflow_started",
        "discrepancy_pct": round(discrepancy_pct, 2),
    }
