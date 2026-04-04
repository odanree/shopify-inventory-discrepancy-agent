import structlog
from fastapi import APIRouter, Request

logger = structlog.get_logger()

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(request: Request):
    """Health check: verifies Redis, DB connectivity, and notification worker heartbeat."""
    from datetime import datetime, timezone
    from app.services.event_router import NotificationWorker

    checks: dict[str, str] = {}

    # Redis
    try:
        await request.app.state.redis.ping()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"error: {exc}"

    # Database
    try:
        from app.db.session import async_engine
        if async_engine is not None:
            async with async_engine.connect() as conn:
                await conn.execute(__import__("sqlalchemy").text("SELECT 1"))
            checks["db"] = "ok"
        else:
            checks["db"] = "not_initialized"
    except Exception as exc:
        checks["db"] = f"error: {exc}"

    # Notification worker heartbeat
    try:
        raw = await request.app.state.redis.get(NotificationWorker.HEARTBEAT_KEY)
        if raw:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(raw)).total_seconds()
            checks["worker"] = "ok" if age < 600 else f"stale:{age:.0f}s"
        else:
            checks["worker"] = "not_started"
    except Exception as exc:
        checks["worker"] = f"error: {exc}"

    all_ok = all(v == "ok" for v in checks.values())

    if not all_ok:
        try:
            import sentry_sdk
            sentry_sdk.capture_message(
                "inventory-discrepancy-agent health degraded",
                level="critical",
                extras={"checks": checks},
            )
        except ImportError:
            pass

    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=200 if all_ok else 503,
        content={
            "status": "ok" if all_ok else "degraded",
            "service": "shopify-inventory-discrepancy-agent",
            "checks": checks,
        },
    )
