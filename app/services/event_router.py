"""Redis pub/sub event router for decoupled Slack notifications (clawhip pattern).

The agent emits structured events to Redis pub/sub; the NotificationWorker subscribes
and delivers them to Slack. Two event types are handled:

  - inventory_notification: resolved discrepancy alert (informational)
  - approval_request: interactive Slack message with Approve/Reject buttons

This keeps notification side-effects out of the LLM context window.
"""
import asyncio
import json
import structlog
from datetime import datetime, timezone

logger = structlog.get_logger()

NOTIFICATIONS_CHANNEL = "shopify:events:inventory-notifications"


class EventRouter:
    """Publish structured events to a Redis pub/sub channel. Fire-and-forget."""

    def __init__(self, redis_client):
        self._redis = redis_client

    async def emit(self, event_type: str, payload: dict) -> None:
        """Publish an event. Never raises — failures are logged and dropped."""
        try:
            event = {
                "type": event_type,
                "payload": payload,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            await self._redis.publish(NOTIFICATIONS_CHANNEL, json.dumps(event))
        except Exception as exc:
            logger.error("event_emit_failed", event_type=event_type, error=str(exc))


class NotificationWorker:
    """Asyncio daemon: subscribe to NOTIFICATIONS_CHANNEL, dispatch to Slack."""

    HEARTBEAT_KEY = "agent:heartbeat:inventory-discrepancy-worker"
    HEARTBEAT_INTERVAL = 300
    HEARTBEAT_TTL = 700

    def __init__(self, redis_client, slack_client):
        self._redis = redis_client
        self._slack = slack_client

    async def run(self, settings) -> None:
        """Run subscribe loop and heartbeat loop concurrently. Runs until cancelled."""
        await asyncio.gather(
            self._run_subscribe_with_retry(settings),
            self._heartbeat_loop(),
        )

    async def _run_subscribe_with_retry(self, settings) -> None:
        while True:
            try:
                await self._subscribe_loop(settings)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("notification_worker_crashed", error=str(exc))
                await asyncio.sleep(5)

    async def _heartbeat_loop(self) -> None:
        """Pulse a Redis TTL key every HEARTBEAT_INTERVAL seconds to prove the worker is alive."""
        while True:
            try:
                await self._redis.set(
                    self.HEARTBEAT_KEY,
                    datetime.now(timezone.utc).isoformat(),
                    ex=self.HEARTBEAT_TTL,
                )
                logger.debug("worker_heartbeat_pulsed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("worker_heartbeat_failed", error=str(exc))
            await asyncio.sleep(self.HEARTBEAT_INTERVAL)

    async def _subscribe_loop(self, settings) -> None:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(NOTIFICATIONS_CHANNEL)
        logger.info("notification_worker_started", channel=NOTIFICATIONS_CHANNEL)
        try:
            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                try:
                    event = json.loads(message["data"])
                    await self._dispatch(event, settings)
                except Exception as exc:
                    logger.error("notification_dispatch_failed", error=str(exc))
        finally:
            await pubsub.unsubscribe(NOTIFICATIONS_CHANNEL)
            await pubsub.aclose()

    async def _dispatch(self, event: dict, settings) -> None:
        event_type = event.get("type")
        payload = event.get("payload", {})

        if event_type == "inventory_notification":
            await self._slack.post_inventory_alert(
                channel=payload.get("channel", settings.slack_alerts_channel),
                title=payload.get("title", "Inventory Discrepancy"),
                fields=payload.get("fields", {}),
                severity=payload.get("severity", "info"),
                run_id=payload.get("run_id", ""),
                redis_client=None,  # dedup handled upstream via run_id uniqueness
            )
        elif event_type == "approval_request":
            await self._slack.post_interactive_approval(
                channel=payload.get("channel", settings.slack_alerts_channel),
                run_id=payload["run_id"],
                sku=payload.get("sku", ""),
                discrepancy_pct=payload.get("discrepancy_pct", 0.0),
                severity=payload.get("severity", "unknown"),
                proposed_action=payload.get("proposed_action", "hold_for_review"),
                proposed_quantity=payload.get("proposed_quantity"),
                expected_quantity=payload.get("expected_quantity", 0),
                open_orders_count=payload.get("open_orders_count", 0),
                root_cause_analysis=payload.get("root_cause_analysis", ""),
            )
        else:
            logger.warning("notification_unknown_event_type", event_type=event_type)
