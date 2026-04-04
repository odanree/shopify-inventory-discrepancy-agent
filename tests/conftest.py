"""Pytest fixtures for the inventory discrepancy agent."""
import fakeredis.aioredis
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.db.base import Base
import app.models.db  # noqa: F401

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture(scope="session")
def test_settings():
    return Settings(
        shopify_shop_domain="test-store.myshopify.com",
        shopify_access_token="test-token",
        anthropic_api_key="sk-ant-test",
        redis_url="redis://localhost:6379/0",
        database_url=TEST_DATABASE_URL,
        slack_webhook_url="https://hooks.slack.com/test",
        slack_alerts_channel="#test",
        google_service_account_json="/nonexistent/sa.json",
        audit_spreadsheet_id="test-sheet-id",
        app_env="test",
    )


@pytest.fixture
async def redis_client():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture(scope="session")
async def db_engine():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def db_session(db_engine):
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        yield session


def _make_state(**overrides):
    from app.agent.state import DiscrepancyState
    base: DiscrepancyState = {
        "run_id": "test-run-001",
        "sku": "SKU-ABC-123",
        "inventory_item_id": "inv-item-001",
        "location_id": "loc-001",
        "expected_quantity": 100,
        "actual_quantity": 80,
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
        "verification_passed": None,
        "retry_count": 0,
        "slack_notified": False,
        "sheets_row": None,
        "tool_calls_log": [],
        "error": None,
        "messages": [],
    }
    base.update(overrides)
    return base
