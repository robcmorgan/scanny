from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from config import settings

engine = create_async_engine(settings.database_url, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


async def init_db():
    from models import Group, Subgroup, PendingScan, PendingScanFile, Document  # noqa: F401 — registers models
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # Self-healing migration: create_all() won't add columns to a table that already
        # exists, so backfill last_activity_at on pending_scans if it's missing (no Alembic
        # in this project — see CLAUDE.md).
        result = await conn.execute(text("PRAGMA table_info(pending_scans)"))
        cols = {row[1] for row in result}
        if "last_activity_at" not in cols:
            await conn.execute(text("ALTER TABLE pending_scans ADD COLUMN last_activity_at DATETIME"))
            await conn.execute(text(
                "UPDATE pending_scans SET last_activity_at = COALESCE(modified_at, created_at)"
            ))
