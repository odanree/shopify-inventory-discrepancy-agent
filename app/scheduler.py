"""Scheduled inventory reconciliation job.

Runs every `settings.scheduler_interval_minutes` minutes. Scans Redis for
operator-set baseline keys (`inventory:baseline:{item_id}:{location_id}`),
fetches current Shopify inventory levels, and fires a discrepancy investigation
workflow for any item exceeding `settings.discrepancy_threshold_pct`.

Start via the FastAPI lifespan (not as a standalone process).
"""
import asyncio
import uuid

import structlog

logger = structlog.get_logger()

BASELINE_KEY_PATTERN = "inventory:baseline:*"


async def run_reconciliation(app) -> None:
    """One reconciliation cycle: scan baselines → compare → fire workflows."""
    from app.agent.graph import start_workflow
    from app.agent.state import DiscrepancyState
    from app.config import get_settings

    settings = get_settings()
    redis = app.state.redis
    shopify = app.state.shopify
    idempotency = app.state.idempotency
    proposal_cache = getattr(app.state, "proposal_cache", {})

    # Scan for all tracked items (non-blocking SCAN)
    baseline_keys = []
    async for key in redis.scan_iter(BASELINE_KEY_PATTERN):
        baseline_keys.append(key)

    if not baseline_keys:
        logger.debug("scheduler_no_baselines_configured")
        return

    logger.info("scheduler_reconciliation_start", items=len(baseline_keys))
    fired = 0

    for key in baseline_keys:
        try:
            # Key format: inventory:baseline:{inventory_item_id}:{location_id}
            parts = key.split(":")
            if len(parts) < 4:
                logger.warning("scheduler_invalid_baseline_key", key=key)
                continue
            inventory_item_id = parts[2]
            location_id = parts[3]

            baseline_raw = await redis.get(key)
            if baseline_raw is None:
                continue
            baseline = int(baseline_raw)

            # Fetch current level from Shopify
            levels = await shopify.get_inventory_levels(inventory_item_id, [location_id])
            if not levels:
                logger.warning("scheduler_no_level_returned", item=inventory_item_id)
                continue

            # Find the level for our specific location
            actual = None
            for level in levels:
                loc = level.get("location", {})
                loc_id = loc.get("id", "").split("/")[-1]
                if loc_id == location_id or level.get("location", {}).get("id", "").endswith(location_id):
                    actual = level.get("available")
                    break

            if actual is None:
                logger.warning("scheduler_location_not_found", item=inventory_item_id, location=location_id)
                continue

            discrepancy_pct = abs(baseline - actual) / max(baseline, 1) * 100

            if discrepancy_pct < settings.discrepancy_threshold_pct:
                continue

            # Check if there's already a pending workflow for this item
            pending_key = f"workflow:pending:sched:{inventory_item_id}:{location_id}"
            already_pending = not await idempotency.check_and_set(pending_key, ttl_seconds=3600)
            if already_pending:
                logger.info("scheduler_workflow_already_pending", item=inventory_item_id)
                continue

            # Try to get SKU from Shopify
            sku = inventory_item_id
            try:
                item_data = await shopify.get_inventory_item_by_sku(inventory_item_id)
                if item_data:
                    sku = item_data.get("sku", inventory_item_id)
            except Exception:
                pass

            run_id = str(uuid.uuid4())
            initial_state: DiscrepancyState = {
                "run_id": run_id,
                "sku": sku,
                "inventory_item_id": inventory_item_id,
                "location_id": location_id,
                "expected_quantity": baseline,
                "actual_quantity": actual,
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

            run_id_out, proposal = await start_workflow(initial_state)
            proposal_cache[run_id_out] = proposal.model_dump()
            await idempotency.save_workflow_state(
                run_id_out,
                {"run_id": run_id_out, "proposal": proposal.model_dump(), "status": "pending_approval"},
            )
            fired += 1
            logger.info(
                "scheduler_workflow_fired",
                run_id=run_id_out,
                sku=sku,
                discrepancy_pct=round(discrepancy_pct, 2),
            )

        except Exception as exc:
            logger.error("scheduler_item_error", key=key, error=str(exc), exc_info=True)

    logger.info("scheduler_reconciliation_complete", fired=fired, checked=len(baseline_keys))


async def start_scheduler(app) -> None:
    """Loop that runs reconciliation on the configured interval.

    Called from lifespan as asyncio.create_task(start_scheduler(app)).
    """
    from app.config import get_settings

    settings = get_settings()
    interval_secs = settings.scheduler_interval_minutes * 60

    logger.info("scheduler_started", interval_minutes=settings.scheduler_interval_minutes)

    while True:
        try:
            await run_reconciliation(app)
        except asyncio.CancelledError:
            logger.info("scheduler_stopped")
            return
        except Exception as exc:
            logger.error("scheduler_cycle_error", error=str(exc), exc_info=True)

        await asyncio.sleep(interval_secs)
