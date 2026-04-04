"""Global kill switch backed by Redis.

Lets operators disable the agent instantly without redeployment — critical
for contract work where you need to cut off a misbehaving agent or a
non-paying client without touching infrastructure.

Usage (via redis-cli):
    redis-cli SET agent:enabled:your-store.myshopify.com "0"  # disable
    redis-cli SET agent:enabled:your-store.myshopify.com "1"  # re-enable

The key defaults to ENABLED when absent — no pre-seeding required.
"""
import structlog

logger = structlog.get_logger()

_KEY_PREFIX = "agent:enabled:"


async def is_enabled(redis_client, store_domain: str) -> bool:
    """Return True if the agent is active for this store. Defaults to True."""
    raw = await redis_client.get(f"{_KEY_PREFIX}{store_domain}")
    if raw is None:
        return True  # absent key = enabled
    return raw not in ("0", "false", "False", "disabled")


async def set_enabled(redis_client, store_domain: str, enabled: bool) -> None:
    """Toggle the kill switch for a store. Persists indefinitely (no TTL)."""
    await redis_client.set(f"{_KEY_PREFIX}{store_domain}", "1" if enabled else "0")
    logger.info("kill_switch_updated", store_domain=store_domain, enabled=enabled)
