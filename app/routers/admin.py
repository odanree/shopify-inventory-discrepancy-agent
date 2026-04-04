"""Admin endpoints for operational control.

All routes require the X-Admin-Key header to match ADMIN_API_KEY from config
when that setting is non-empty. In dev (empty key) the endpoints are open.
"""
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.config import get_settings
from app.services import kill_switch

logger = structlog.get_logger()
router = APIRouter(prefix="/api/admin", tags=["admin"])


def _require_admin_key(request: Request) -> None:
    key = get_settings().admin_api_key
    if key and request.headers.get("X-Admin-Key") != key:
        raise HTTPException(status_code=403, detail="Invalid or missing X-Admin-Key")


class AgentControlRequest(BaseModel):
    enabled: bool


@router.post("/agent-control")
async def set_agent_enabled(
    body: AgentControlRequest,
    request: Request,
    _: None = Depends(_require_admin_key),
):
    """Enable or disable the agent for this store."""
    settings = get_settings()
    store_domain = settings.shopify_shop_domain
    await kill_switch.set_enabled(request.app.state.redis, store_domain, body.enabled)
    status = "enabled" if body.enabled else "disabled"
    logger.info("admin_kill_switch", store_domain=store_domain, status=status)
    return {"store_domain": store_domain, "agent_enabled": body.enabled}


@router.get("/agent-status")
async def get_agent_status(
    request: Request,
    _: None = Depends(_require_admin_key),
):
    """Return the current kill-switch state for this store."""
    settings = get_settings()
    store_domain = settings.shopify_shop_domain
    enabled = await kill_switch.is_enabled(request.app.state.redis, store_domain)
    return {"store_domain": store_domain, "agent_enabled": enabled}
