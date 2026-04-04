import json

import structlog

logger = structlog.get_logger()

WORKFLOW_KEY_PREFIX = "workflow:state:"
PENDING_KEY_PREFIX = "workflow:pending:"


class IdempotencyService:
    def __init__(self, redis_client):
        self.redis = redis_client

    async def check_and_set(self, key: str, ttl_seconds: int = 3600) -> bool:
        """Returns True if NEW (not seen before), False if duplicate. Uses SET NX EX."""
        result = await self.redis.set(key, "1", nx=True, ex=ttl_seconds)
        return result is not None

    async def save_workflow_state(self, run_id: str, state: dict, ttl_hours: int = 24):
        """Serialize and store workflow state in Redis."""
        key = f"{WORKFLOW_KEY_PREFIX}{run_id}"
        pending_key = f"{PENDING_KEY_PREFIX}{run_id}"
        serialized = json.dumps(state, default=str)
        ttl_seconds = ttl_hours * 3600
        await self.redis.set(key, serialized, ex=ttl_seconds)
        # Also track as pending (for listing)
        await self.redis.set(pending_key, "1", ex=ttl_seconds)

    async def get_workflow_state(self, run_id: str) -> dict | None:
        key = f"{WORKFLOW_KEY_PREFIX}{run_id}"
        raw = await self.redis.get(key)
        if raw is None:
            return None
        return json.loads(raw)

    async def delete_workflow_state(self, run_id: str):
        await self.redis.delete(f"{WORKFLOW_KEY_PREFIX}{run_id}")
        await self.redis.delete(f"{PENDING_KEY_PREFIX}{run_id}")

    async def list_pending_run_ids(self) -> list[str]:
        """Return all run_ids currently awaiting approval."""
        keys = await self.redis.keys(f"{PENDING_KEY_PREFIX}*")
        prefix_len = len(PENDING_KEY_PREFIX)
        return [k[prefix_len:] for k in keys]
