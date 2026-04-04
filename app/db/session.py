from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.db.base import Base

async_engine = None
AsyncSessionLocal: async_sessionmaker[AsyncSession] | None = None


async def init_db() -> None:
    global async_engine, AsyncSessionLocal
    settings = get_settings()
    async_engine = create_async_engine(
        settings.database_url,
        echo=False,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
    )
    AsyncSessionLocal = async_sessionmaker(async_engine, expire_on_commit=False)

    import app.models.db  # noqa: F401

    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db():
    if AsyncSessionLocal is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    async with AsyncSessionLocal() as session:
        yield session


async def get_redis():
    """Dependency that returns the app-level Redis client (set during lifespan)."""
    # Imported here to avoid circular; actual client stored on app.state
    from fastapi import Request
    # This is used as a plain function in health checks; callers pass the client directly
    raise NotImplementedError("Use request.app.state.redis directly")
