import structlog
from fastapi import APIRouter, Request

logger = structlog.get_logger()

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(request: Request):
    """Health check: verifies Redis and DB connectivity."""
    checks = {}

    # Redis
    try:
        redis = request.app.state.redis
        await redis.ping()
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

    all_ok = all(v == "ok" for v in checks.values())
    return {
        "status": "ok" if all_ok else "degraded",
        "service": "shopify-inventory-discrepancy-agent",
        "checks": checks,
    }
