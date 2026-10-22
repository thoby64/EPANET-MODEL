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

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from app.core.config import get_settings
from app.core.database import ensure_storage_paths, init_db
from app.core.tz import install_utc_datetime_serialization
from app.routers import dma, files, inp, majiscope, simulation, upload

install_utc_datetime_serialization()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


def _render_status_page(settings) -> str:
    """Minimal status page served at the service root.

    It only confirms the service is live and links to the hydraulic model
    workspace — the API surface stays out of the way.
    """
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{settings.service_name} — Live</title>
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; min-height: 100vh; display: flex; align-items: center;
    justify-content: center; padding: 2rem;
    font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    background: #0f172a; color: #e2e8f0;
  }}
  .card {{
    width: 100%; max-width: 26rem; text-align: center;
    background: #1e293b; border: 1px solid #334155;
    border-radius: 1rem; padding: 2.5rem 2rem; box-shadow: 0 24px 60px rgba(0,0,0,.45);
  }}
  .badge {{
    display: inline-flex; align-items: center; gap: .5rem;
    background: rgba(34,197,94,.15); color: #4ade80; border: 1px solid rgba(34,197,94,.35);
    border-radius: 999px; padding: .25rem .75rem; font-size: .8rem; font-weight: 600;
    letter-spacing: .04em; text-transform: uppercase;
  }}
  .dot {{ width: .5rem; height: .5rem; border-radius: 999px; background: #4ade80; }}
  h1 {{ margin: 1.25rem 0 .35rem; font-size: 1.45rem; letter-spacing: -.02em; }}
  p.sub {{ margin: 0 0 1.75rem; color: #94a3b8; font-size: .95rem; }}
  .app-link {{
    display: inline-block; width: 100%; padding: .85rem 1.25rem;
    background: linear-gradient(135deg, #0ea5e9, #2563eb); color: #fff;
    border-radius: .75rem; font-weight: 600; text-decoration: none;
  }}
  .app-link:hover {{ filter: brightness(1.08); }}
</style>
</head>
<body>
  <main class="card">
    <span class="badge"><span class="dot"></span>Live</span>
    <h1>{settings.service_name}</h1>
    <p class="sub">The hydraulic simulation service is running and accepting requests.</p>
    <a class="app-link" href="/app">Open Hydraulic Model</a>
  </main>
</body>
</html>"""


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
    app.include_router(inp.router)
    app.include_router(dma.router)
    app.include_router(simulation.router)
    app.include_router(majiscope.router)

    # ── health check (no auth required) ───────────────────────────────────────
    @app.get("/health", tags=["health"], include_in_schema=False)
    async def health():
        return JSONResponse({"status": "ok", "service": settings.service_name})

    @app.get("/", tags=["health"], include_in_schema=False)
    async def status_page():
        """Human friendly landing page so the service root is not a bare 404."""
        return HTMLResponse(_render_status_page(settings))

    @app.get("/app", tags=["frontend"], include_in_schema=False)
    async def hydraulic_model_app():
        frontend_path = Path(__file__).resolve().parents[1] / "frontend" / "dma_explorer.html"
        return FileResponse(frontend_path)

    # ── standalone simulator configuration (no MajiScope session) ─────────────
    @app.get("/standalone/config", tags=["frontend"], include_in_schema=False)
    async def standalone_config(request: Request):
        """Configuration for the standalone simulator UI.

        Used when somebody runs the simulator on its own instead of through a
        MajiScope session. It carries the shared API key so the operator types
        the same value the service accepts. Leave `STANDALONE_API_KEY` empty to
        keep the key out of the browser entirely.
        """
        return JSONResponse({
            "service_name": settings.service_name,
            "api_base": str(request.base_url).rstrip("/"),
            "api_key": settings.effective_standalone_api_key,
        })

    return app


app = create_app()
