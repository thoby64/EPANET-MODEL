# app/main.py
"""
EPANET Hydraulic Simulation Service — FastAPI entry point.

Start with:
    uvicorn app.main:app --host 0.0.0.0 --port 8080
"""

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from app.core.config import get_settings
from app.core.database import ensure_storage_paths, init_db
from app.core.tz import install_utc_datetime_serialization
from app.routers import dma, files, majiscope, simulation, upload

install_utc_datetime_serialization()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


async def _snapshot_delivery_loop() -> None:
    from app.workers.simulation_worker import retry_pending_majiscope_snapshots
    while True:
        try:
            delivered = await retry_pending_majiscope_snapshots()
            if delivered:
                logger.info("Delivered %d pending MajiScope hydraulic snapshot(s)", delivered)
        except Exception:
            logger.exception("Pending MajiScope snapshot delivery check failed")
        await asyncio.sleep(300)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise the database on startup."""
    ensure_storage_paths()
    removed = majiscope.cleanup_expired_majiscope_files()
    if removed:
        logger.info("Cleaned %d expired MajiScope temporary hydraulic file(s)", removed)
    logger.info("Initialising database …")
    await init_db()
    logger.info("Database ready.")
    snapshot_delivery_task = asyncio.create_task(_snapshot_delivery_loop())
    yield
    snapshot_delivery_task.cancel()
    with suppress(asyncio.CancelledError):
        await snapshot_delivery_task
    logger.info("Shutting down.")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title       = settings.service_name,
        version     = settings.service_version,
        description = (
            "DUWASA DMA Hydraulic Simulation Service.\n\n"
            "Reads multi-layer `.gpkg` files (pipes, sources, tanks, valves, DMA boundary) "
            "from a shared volume, builds EPANET models with automatic topology repair, "
            "runs 24-hour Extended Period Simulations via **EPyT-Flow / EPANET**, and "
            "exposes pressure, flow, NRW, and leakage-risk results through a REST API "
            "secured by API-key authentication.\n\n"
            "All endpoints require the `X-API-Key` header.\n\n"
            "Start a simulation: `POST /dma/{filename}/simulate`"
        ),
        lifespan    = lifespan,
        docs_url    = "/docs",
        redoc_url   = "/redoc",
    )

    # ── CORS (restrict origins for production) ────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins     = ["*"],
        allow_credentials = True,
        allow_methods     = ["*"],
        allow_headers     = ["*"],
    )

    # ── routers ───────────────────────────────────────────────────────────────
    app.include_router(files.router)
    app.include_router(upload.router)
    app.include_router(dma.router)
    app.include_router(simulation.router)
    app.include_router(majiscope.router)

    # ── health check (no auth required) ───────────────────────────────────────
    @app.get("/health", tags=["health"], include_in_schema=False)
    async def health():
        return JSONResponse({"status": "ok", "service": settings.service_name})

    @app.get("/app", tags=["frontend"], include_in_schema=False)
    async def hydraulic_model_app():
        frontend_path = Path(__file__).resolve().parents[1] / "frontend" / "dma_explorer.html"
        return FileResponse(frontend_path)

    return app


app = create_app()
