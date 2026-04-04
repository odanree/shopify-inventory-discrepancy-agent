import structlog
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.db.session import init_db

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer()
            if settings.app_env == "development"
            else structlog.processors.JSONRenderer(),
        ]
    )

    app.state.redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    await init_db()

    from app.services.shopify_client import InventoryShopifyClient
    from app.services.idempotency import IdempotencyService
    from app.services.slack_client import SlackClient
    from app.services.google_sheets import GoogleSheetsClient
    from app.agent.tools import inject_tool_dependencies
    from app.db.session import AsyncSessionLocal

    app.state.shopify = InventoryShopifyClient(
        domain=settings.shopify_shop_domain,
        token=settings.shopify_access_token,
        redis_client=app.state.redis,
    )
    app.state.idempotency = IdempotencyService(app.state.redis)
    app.state.slack = SlackClient(settings.slack_webhook_url)
    app.state.sheets = GoogleSheetsClient(
        service_account_json_path=settings.google_service_account_json,
        spreadsheet_id=settings.audit_spreadsheet_id,
    )
    # Proposal cache: run_id → proposal dict (in-memory, single-process)
    app.state.proposal_cache = {}

    inject_tool_dependencies(
        shopify=app.state.shopify,
        slack=app.state.slack,
        sheets=app.state.sheets,
        db_factory=AsyncSessionLocal,
        idempotency=app.state.idempotency,
        redis=app.state.redis,
    )

    logger.info("startup_complete", env=settings.app_env)
    yield

    await app.state.shopify.close()
    await app.state.slack.close()
    await app.state.redis.aclose()
    logger.info("shutdown_complete")


app = FastAPI(
    title="Shopify Inventory Discrepancy Agent", version="0.1.0", lifespan=lifespan
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.error("unhandled_exception", path=str(request.url.path), error=str(exc), exc_info=True)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


from app.routers import discrepancies, approvals, health  # noqa: E402

app.include_router(discrepancies.router)
app.include_router(approvals.router)
app.include_router(health.router)
