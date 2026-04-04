import asyncio
import structlog
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.db.session import init_db

logger = structlog.get_logger()


def _parse_redis_conn_info(redis_url: str) -> dict:
    """Parse redis://host:port/db into kwargs for AsyncRedisSaver.from_conn_info."""
    from urllib.parse import urlparse
    parsed = urlparse(redis_url)
    return {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 6379,
        "db": int(parsed.path.lstrip("/") or 0),
    }


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

    # Primary Redis client (decode_responses=True for business logic keys)
    app.state.redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    await init_db()

    from app.services.shopify_client import InventoryShopifyClient
    from app.services.idempotency import IdempotencyService
    from app.services.slack_client import SlackClient
    from app.services.google_sheets import GoogleSheetsClient
    from app.services.event_router import EventRouter, NotificationWorker
    from app.agent.tools import inject_tool_dependencies
    from app.agent.nodes import inject_event_router
    from app.agent.graph import init_graph
    from app.db.session import AsyncSessionLocal

    # AsyncRedisSaver uses its own binary-safe connection (NOT decode_responses=True)
    # so it must not share the app.state.redis client.
    try:
        from langgraph.checkpoint.redis.aio import AsyncRedisSaver
        conn_info = _parse_redis_conn_info(settings.redis_url)
        checkpointer = AsyncRedisSaver.from_conn_info(**conn_info)
        await checkpointer.asetup()
        app.state.checkpointer = checkpointer
        logger.info("checkpointer_initialized", backend="AsyncRedisSaver")
    except ImportError:
        from langgraph.checkpoint.memory import MemorySaver
        checkpointer = MemorySaver()
        app.state.checkpointer = checkpointer
        logger.warning(
            "checkpointer_fallback",
            reason="langgraph-checkpoint-redis not installed; using MemorySaver",
        )

    init_graph(checkpointer)

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
    # Proposal cache: run_id → proposal dict (in-memory, supplements checkpoint state)
    app.state.proposal_cache = {}

    app.state.event_router = EventRouter(app.state.redis)
    inject_event_router(app.state.event_router)

    inject_tool_dependencies(
        shopify=app.state.shopify,
        sheets=app.state.sheets,
        db_factory=AsyncSessionLocal,
        idempotency=app.state.idempotency,
    )

    # Start notification worker as background asyncio task
    worker = NotificationWorker(app.state.redis, app.state.slack)
    app.state.notification_task = asyncio.create_task(
        worker.run(settings), name="notification-worker"
    )

    # Start scheduler as background task (fires reconciliation on interval)
    app.state.scheduler_task = None
    if settings.scheduler_enabled:
        from app.scheduler import start_scheduler
        app.state.scheduler_task = asyncio.create_task(start_scheduler(app))

    logger.info("startup_complete", env=settings.app_env)
    yield

    app.state.notification_task.cancel()
    try:
        await app.state.notification_task
    except asyncio.CancelledError:
        pass

    if app.state.scheduler_task is not None:
        app.state.scheduler_task.cancel()
        try:
            await app.state.scheduler_task
        except asyncio.CancelledError:
            pass

    await app.state.shopify.close()
    await app.state.slack.close()
    await app.state.redis.aclose()
    # AsyncRedisSaver may expose aclose; MemorySaver does not
    if hasattr(app.state.checkpointer, "aclose"):
        await app.state.checkpointer.aclose()
    logger.info("shutdown_complete")


app = FastAPI(
    title="Shopify Inventory Discrepancy Agent", version="0.1.0", lifespan=lifespan
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.error("unhandled_exception", path=str(request.url.path), error=str(exc), exc_info=True)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


from app.routers import discrepancies, approvals, health, inventory_webhook, slack_actions, admin, dashboard  # noqa: E402

app.include_router(discrepancies.router)
app.include_router(approvals.router)
app.include_router(health.router)
app.include_router(inventory_webhook.router)
app.include_router(slack_actions.router)
app.include_router(admin.router)
app.include_router(dashboard.router)
