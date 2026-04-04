"""Weekly impact report for the inventory discrepancy agent.

Aggregates 7-day stats from Postgres and delivers a Slack message every Monday.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import func, select

logger = structlog.get_logger()

_SEVEN_DAYS = timedelta(days=7)
_LAST_SENT_KEY = "agent:weekly_report:inventory:last_sent"


async def send_weekly_report(db_factory, slack_client, settings) -> None:
    """Generate and deliver the weekly report."""
    from app.models.db import DiscrepancyAuditLog
    from sqlalchemy import func, select
    seven_days_ago = datetime.now(timezone.utc) - _SEVEN_DAYS

    try:
        async with db_factory() as session:
            total = await session.scalar(select(func.count(DiscrepancyAuditLog.id)).where(DiscrepancyAuditLog.created_at >= seven_days_ago)) or 0
            approved = await session.scalar(select(func.count(DiscrepancyAuditLog.id)).where(DiscrepancyAuditLog.approved.is_(True), DiscrepancyAuditLog.created_at >= seven_days_ago)) or 0
            pending = await session.scalar(select(func.count(DiscrepancyAuditLog.id)).where(DiscrepancyAuditLog.approved.is_(None), DiscrepancyAuditLog.proposed_action.isnot(None))) or 0
            transfers = await session.scalar(select(func.count(DiscrepancyAuditLog.id)).where(DiscrepancyAuditLog.resolution_applied == "transfer_inventory", DiscrepancyAuditLog.created_at >= seven_days_ago)) or 0
            avg_pct = await session.scalar(select(func.avg(DiscrepancyAuditLog.discrepancy_pct)).where(DiscrepancyAuditLog.created_at >= seven_days_ago)) or 0.0

        week_start = (datetime.now(timezone.utc) - _SEVEN_DAYS).strftime("%b %d")
        week_end = datetime.now(timezone.utc).strftime("%b %d")
        approval_rate = round(approved / max(total, 1) * 100, 1)

        fields = {
            "Period": f"{week_start} – {week_end}",
            "Discrepancies Detected": str(total),
            "Resolutions Approved": f"{approved} ({approval_rate}%)",
            "Inventory Transfers": str(transfers),
            "Pending Review": str(pending),
            "Avg Discrepancy": f"{round(avg_pct, 1)}%",
        }

        await slack_client.post_inventory_alert(
            channel=settings.slack_alerts_channel,
            title="📦 Inventory Discrepancy Agent — Weekly Impact Report",
            fields=fields,
            severity="info",
            run_id=f"weekly-report-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
            redis_client=None,
        )
        logger.info("weekly_report_sent", channel=settings.slack_alerts_channel)
    except Exception as exc:
        logger.error("weekly_report_failed", error=str(exc))


async def start_weekly_report_scheduler(app) -> None:
    """Asyncio task: send report every Monday at ~8:00 AM UTC."""
    from app.config import get_settings
    from app.db.session import AsyncSessionLocal

    settings = get_settings()

    while True:
        try:
            now = datetime.now(timezone.utc)
            if now.weekday() == 0 and now.hour >= 8:
                today_str = now.strftime("%Y-%m-%d")
                redis = app.state.redis
                last_sent = await redis.get(_LAST_SENT_KEY)
                if last_sent != today_str:
                    await send_weekly_report(AsyncSessionLocal, app.state.slack, settings)
                    await redis.set(_LAST_SENT_KEY, today_str, ex=90000)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.error("weekly_report_scheduler_error", error=str(exc))

        await asyncio.sleep(3600)
