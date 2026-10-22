# tests/test_api.py
"""
Integration tests for the EPANET simulation service.

Run with:
    pytest tests/ -v

Requires the service to have a test .gpkg file available.
Set TEST_GPKG_FILE env var to your filename, e.g.:
    TEST_GPKG_FILE=duwas_network_clean.gpkg pytest tests/ -v
"""

import os
import asyncio
import pytest
from httpx import AsyncClient, ASGITransport

# set test env before importing the app
os.environ.setdefault("API_KEYS",       "test-key-123")
os.environ.setdefault("GPKG_DIR",       "/data/gpkg")
os.environ.setdefault("DATABASE_URL",   "sqlite+aiosqlite:////tmp/test_epanet.db")

from app.main import app  # noqa: E402
from app.core.config import get_settings  # noqa: E402


TEST_KEY      = "test-key-123"
TEST_GPKG     = os.getenv("TEST_GPKG_FILE", "duwas_network_clean.gpkg")
AUTH_HEADERS  = {"X-API-Key": TEST_KEY}
AUTH          = AUTH_HEADERS


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


# ── auth tests ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_api_key_rejected(client):
    r = await client.get("/files")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_wrong_api_key_rejected(client):
    r = await client.get("/files", headers={"X-API-Key": "wrong"})
    assert r.status_code == 401


# ── health ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# ── files ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_files(client):
    r = await client.get("/files", headers=AUTH_HEADERS)
    assert r.status_code == 200
    assert isinstance(r.json(), list)


# ── simulation lifecycle ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_submit_simulation_missing_file(client):
    r = await client.post("/simulate", headers=AUTH_HEADERS, json={
        "gpkg_filename": "does_not_exist.gpkg",
        "name": "Should fail",
    })
    # 202 queued, but will transition to FAILED
    assert r.status_code in (202, 404)


@pytest.mark.asyncio
async def test_simulate_not_found(client):
    r = await client.get("/simulate/99999", headers=AUTH_HEADERS)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_list_simulations(client):
    r = await client.get("/simulate", headers=AUTH_HEADERS)
    assert r.status_code == 200
    assert isinstance(r.json(), list)


@pytest.mark.asyncio
async def test_list_simulations_filter_status(client):
    r = await client.get("/simulate?status=DONE", headers=AUTH_HEADERS)
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_delete_nonexistent(client):
    r = await client.delete("/simulate/99999", headers=AUTH_HEADERS)
    assert r.status_code == 404


# ── full simulation (skipped if no gpkg file present) ─────────────────────────

@pytest.mark.asyncio
async def test_full_simulation_lifecycle(client):
    """
    End-to-end test: submit → poll until DONE → fetch results → alerts → GeoJSON.
    Skipped if TEST_GPKG_FILE is not present on disk.
    """
    gpkg_dir = os.getenv("GPKG_DIR", "/data/gpkg")
    if not os.path.isfile(os.path.join(gpkg_dir, TEST_GPKG)):
        pytest.skip(f"Test gpkg not found: {TEST_GPKG}")

    # submit
    r = await client.post("/simulate", headers=AUTH_HEADERS, json={
        "gpkg_filename": TEST_GPKG,
        "name":          "Test run",
        "duration_hrs":  1,
        "time_step_min": 60,
        "base_demand":   0.001,
    })
    assert r.status_code == 202
    scenario_id = r.json()["id"]

    # poll until done or failed (timeout after 120 s)
    import asyncio as aio
    for _ in range(40):
        await aio.sleep(3)
        r = await client.get(f"/simulate/{scenario_id}", headers=AUTH_HEADERS)
        data = r.json()
        if data["status"] in ("DONE", "FAILED"):
            break

    assert data["status"] == "DONE", f"Simulation failed: {data.get('error_message')}"
    assert data["summary"] is not None

    # node results at hour 0
    r = await client.get(f"/simulate/{scenario_id}/nodes?time_step=0", headers=AUTH_HEADERS)
    assert r.status_code == 200
    nodes = r.json()
    assert len(nodes) > 0
    assert "pressure" in nodes[0]

    # pipe results at hour 0
    r = await client.get(f"/simulate/{scenario_id}/pipes?time_step=0", headers=AUTH_HEADERS)
    assert r.status_code == 200
    assert len(r.json()) > 0

    # alerts
    r = await client.get(f"/simulate/{scenario_id}/alerts?time_step=0", headers=AUTH_HEADERS)
    assert r.status_code == 200
    alerts = r.json()
    assert "low_pressure_nodes" in alerts
    assert "high_velocity_pipes" in alerts

    # GeoJSON
    r = await client.get(f"/simulate/{scenario_id}/geojson/nodes?time_step=0", headers=AUTH_HEADERS)
    assert r.status_code == 200
    gj = r.json()
    assert gj["type"] == "FeatureCollection"
    assert len(gj["features"]) > 0

    # cleanup
    r = await client.delete(f"/simulate/{scenario_id}", headers=AUTH_HEADERS)
    assert r.status_code == 204


# ── landing page + standalone configuration ──────────────────────────────────────

async def test_root_serves_live_status_page(client):
    r = await client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Live" in r.text
    assert get_settings().service_name in r.text
    assert "/app" in r.text
    # The landing page only links to the model workspace.
    assert "/docs" not in r.text
    assert "/majiscope/prepare-gpkg" not in r.text


async def test_standalone_config_exposes_shared_api_key(client):
    r = await client.get("/standalone/config")
    assert r.status_code == 200
    payload = r.json()
    assert payload["service_name"] == get_settings().service_name
    assert payload["api_base"]
    assert payload["api_key"] == get_settings().effective_standalone_api_key


async def test_standalone_config_hides_key_when_not_configured(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "standalone_api_key", "")
    r = await client.get("/standalone/config")
    assert r.status_code == 200
    assert r.json()["api_key"] == ""


async def test_upload_requires_auth(client):
    r = await client.post("/files/upload")
    assert r.status_code == 401


async def test_upload_wrong_extension(client):
    from httpx import _multipart
    r = await client.post(
        "/files/upload",
        headers={**AUTH},
        files={"file": ("test.csv", b"bad data", "text/csv")},
    )
    assert r.status_code == 422


async def test_upload_bad_zip(client):
    r = await client.post(
        "/files/upload",
        headers={**AUTH},
        files={"file": ("data.zip", b"not a zip", "application/zip")},
    )
    assert r.status_code == 422


async def test_upload_gpkg_roundtrip(client):
    """Upload the existing DUWASA.gpkg back under a different name — proves the full path works."""
    import os
    gpkg_path = os.path.join(os.getenv("GPKG_DIR", "/data/gpkg"), "DUWASA.gpkg")
    if not os.path.isfile(gpkg_path):
        pytest.skip("DUWASA.gpkg not found")

    with open(gpkg_path, "rb") as fh:
        content = fh.read()

    r = await client.post(
        "/files/upload",
        headers={**AUTH},
        files={"file": ("test_upload.gpkg", content, "application/octet-stream")},
    )
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["filename"] == "test_upload.gpkg"
    assert d["has_dma_structure"] is True
    assert "dma" in d["layers"]
    assert "waterpipes" in d["layers"]
    assert d["missing_layers"] == []

    # confirm it appears in /files list
    files_r = await client.get("/files", headers=AUTH)
    names = [f["filename"] for f in files_r.json()]
    assert "test_upload.gpkg" in names

    # cleanup
    import os as _os
    _os.remove(os.path.join(os.getenv("GPKG_DIR", "/data/gpkg"), "test_upload.gpkg"))

