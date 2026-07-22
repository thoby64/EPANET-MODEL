# app/core/database.py
"""
Async SQLAlchemy + aiosqlite database setup.

Usage inside a router/worker:
    async with get_session() as session:
        session.add(obj)
        await session.commit()

Or as a FastAPI dependency:
    async def my_route(db: AsyncSession = Depends(get_db)):
        ...
"""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


def ensure_storage_paths() -> None:
    """Create runtime storage folders before SQLite or uploads use them.

    Render persistent disks are mounted at runtime, so directories created
    during the Docker build can be hidden by the mounted disk.
    """
    settings = get_settings()
    os.makedirs(settings.gpkg_dir, exist_ok=True)

    db_url = make_url(settings.database_url)
    if db_url.drivername.startswith("sqlite") and db_url.database:
        database_path = Path(db_url.database)
        if database_path.parent:
            database_path.parent.mkdir(parents=True, exist_ok=True)


def _make_engine():
    settings = get_settings()
    ensure_storage_paths()

    return create_async_engine(
        settings.database_url,
        echo=settings.debug,
        connect_args={"check_same_thread": False} if "sqlite" in settings.database_url else {},
    )


engine        = _make_engine()
async_session = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    """Create all tables on startup, then patch any new columns onto
    tables that already existed before this version (SQLite's
    `create_all` only creates missing *tables*, it never alters an
    existing one — so a pre-existing data/db/epanet_service.db needs
    this tiny migration the first time it starts with the new code)."""
    async with engine.begin() as conn:
        from app.models.simulation import SimScenario, SimResult  # noqa: F401
        await conn.run_sync(Base.metadata.create_all)
        await _ensure_sim_scenarios_columns(conn)


async def _ensure_sim_scenarios_columns(conn) -> None:
    result   = await conn.execute(text("PRAGMA table_info(sim_scenarios)"))
    existing = {row[1] for row in result.fetchall()}
    # leakage_frac was added when the DMA workflow replaced the whole-network one
    additions = {
        "leakage_frac": "FLOAT DEFAULT 0.20",
        "scenario_type": "VARCHAR(24) DEFAULT 'baseline'",
        "demand_model": "VARCHAR(12) DEFAULT 'DDA'",
        "pda_pressure_min": "FLOAT",
        "pda_pressure_required": "FLOAT",
        "pda_pressure_exponent": "FLOAT",
        "majiscope_snapshot_payload": "JSON",
        "majiscope_snapshot_status": "VARCHAR(20)",
        "majiscope_snapshot_attempts": "INTEGER DEFAULT 0",
        "majiscope_snapshot_last_error": "TEXT",
        "majiscope_snapshot_delivered_at": "DATETIME",
    }
    for column, coltype in additions.items():
        if column not in existing:
            await conn.execute(text(f"ALTER TABLE sim_scenarios ADD COLUMN {column} {coltype}"))
            logger.info("Migrated sim_scenarios: added column '%s'", column)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a database session."""
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Context-manager version for use outside FastAPI (e.g. background workers)."""
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
