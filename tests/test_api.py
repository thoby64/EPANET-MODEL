# tests/test_api.py
"""
Basic API integration tests — auth, /health, /files, /simulate (GET only).
DMA-specific lifecycle tests are in test_dma.py.
"""

import os

import pytest
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("API_KEYS",     "test-key-123")
os.environ.setdefault("GPKG_DIR",     "/data/gpkg")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:////tmp/test_api.db")

from app.main import app  # noqa: E402

AUTH = {"X-API-Key": "test-key-123"}


@pytest.fixture(scope="session")
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ── health ────────────────────────────────────────────────────────────────────

async def test_health_no_auth(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# ── auth ──────────────────────────────────────────────────────────────────────

async def test_files_requires_auth(client):
    r = await client.get("/files")
    assert r.status_code == 401


async def test_simulate_list_requires_auth(client):
    r = await client.get("/simulate")
    assert r.status_code == 401


async def test_simulate_get_requires_auth(client):
    r = await client.get("/simulate/1")
    assert r.status_code == 401


# ── /files ───────────────────────────────────────────────────────────────────

async def test_files_returns_list(client):
    r = await client.get("/files", headers=AUTH)
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    # All entries must have the new DMA-aware fields
    for f in data:
        assert "filename" in f
        assert "size_kb" in f
        assert "layers" in f
        assert "missing_layers" in f
        assert "has_dma_structure" in f


async def test_files_duwasa_present(client):
    r = await client.get("/files", headers=AUTH)
    names = [f["filename"] for f in r.json()]
    if "DUWASA.gpkg" in names:
        entry = next(f for f in r.json() if f["filename"] == "DUWASA.gpkg")
        assert entry["has_dma_structure"] is True
        assert "dma" in entry["layers"]
        assert "waterpipes" in entry["layers"]


# ── /simulate (GET only — POST is via /dma) ───────────────────────────────────

async def test_simulate_list_empty(client):
    r = await client.get("/simulate", headers=AUTH)
    assert r.status_code == 200
    assert isinstance(r.json(), list)


async def test_simulate_get_not_found(client):
    r = await client.get("/simulate/99999", headers=AUTH)
    assert r.status_code == 404


async def test_simulate_delete_not_found(client):
    r = await client.delete("/simulate/99999", headers=AUTH)
    assert r.status_code == 404


async def test_simulate_post_removed(client):
    """
    POST /simulate is intentionally removed — DMA simulations go through
    POST /dma/{file}/simulate.  Verify the route no longer exists.
    """
    r = await client.post("/simulate", headers=AUTH, json={})
    assert r.status_code == 405   # Method Not Allowed


# ── docs ──────────────────────────────────────────────────────────────────────

async def test_openapi_schema(client):
    r = await client.get("/openapi.json")
    assert r.status_code == 200
    schema = r.json()
    paths = schema["paths"]
    # DMA simulate must be present
    assert "/dma/{filename}/simulate" in paths
    # Old generic POST /simulate must be gone
    assert "post" not in paths.get("/simulate", {})
    prepare_model = paths["/majiscope/prepare-gpkg"]["post"]
    request_schema_ref = prepare_model["requestBody"]["content"]["multipart/form-data"]["schema"]["$ref"]
    request_schema_name = request_schema_ref.rsplit("/", 1)[-1]
    request_schema = schema["components"]["schemas"][request_schema_name]
    assert "inp_file" in request_schema["properties"]
    assert "inp_file" not in request_schema.get("required", [])


# ── /files/upload ─────────────────────────────────────────────────────────────

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


# ── /dma/{file}/zones ─────────────────────────────────────────────────────────

async def test_dma_zones(client):
    if not os.path.isfile(
        os.path.join(os.getenv("GPKG_DIR", "/data/gpkg"), "DUWASA.gpkg")
    ):
        pytest.skip("DUWASA.gpkg not found")
    r = await client.get("/dma/DUWASA.gpkg/zones", headers=AUTH)
    assert r.status_code == 200
    d = r.json()
    assert "zones" in d
    assert "count" in d
    assert d["count"] >= 1
    zone = d["zones"][0]
    assert "name" in zone
    assert "bbox" in zone
    assert len(zone["bbox"]) == 4


async def test_detailed_result_aggregates_and_tanks(client):
    from app.core.database import get_session
    from app.models.simulation import SimResult, SimScenario

    filename = "detailed-results-test.gpkg"
    async with get_session() as db:
        scenario = SimScenario(
            gpkg_filename=filename,
            name="Detailed result API test",
            status="DONE",
            duration_hrs=1,
            time_step_min=30,
        )
        db.add(scenario)
        await db.flush()
        scenario_id = scenario.id
        db.add_all([
            SimResult(
                scenario_id=scenario_id, time_step=0, element_type="node",
                element_id="J1", pressure=5, water_age=0, demand=0.001,
                is_low_pressure=True,
            ),
            SimResult(
                scenario_id=scenario_id, time_step=0, element_type="node",
                element_id="J2", pressure=15, water_age=2, demand=0.002,
            ),
            SimResult(
                scenario_id=scenario_id, time_step=1, element_type="node",
                element_id="J1", pressure=7, water_age=3, demand=0.001,
            ),
            SimResult(
                scenario_id=scenario_id, time_step=0, element_type="pipe",
                element_id="P1", flow_rate=0.01,
            ),
            SimResult(
                scenario_id=scenario_id, time_step=0, element_type="pipe",
                element_id="P2", flow_rate=-0.02,
            ),
            SimResult(
                scenario_id=scenario_id, time_step=1, element_type="tank",
                element_id="T1", volume_m3=42.5,
            ),
        ])

    try:
        response = await client.get(
            f"/simulate/{scenario_id}/timeseries",
            headers=AUTH,
        )
        assert response.status_code == 200, response.text
        result = response.json()
        first_step = result["pressure"][0]
        assert first_step["hour"] == 0
        assert first_step["pressure_min_m"] == 5
        assert first_step["pressure_avg_m"] == 10
        assert first_step["pressure_max_m"] == 15
        assert first_step["water_age_avg_hrs"] == 1
        assert first_step["water_age_max_hrs"] == 2
        assert first_step["total_demand_m3h"] == 10.8
        assert first_step["low_pressure_nodes"] == 1
        assert result["pressure"][1]["hour"] == 0.5
        assert result["pipe_flow"][0]["flow_avg_m3s"] == 0.015
        assert result["pipe_flow"][0]["flow_max_abs_m3s"] == 0.02

        response = await client.get(
            f"/dma/{filename}/simulate/{scenario_id}/tanks",
            headers=AUTH,
        )
        assert response.status_code == 200, response.text
        tank = response.json()["tanks"][0]
        assert tank["tank_id"] == "T1"
        assert tank["series"] == [{
            "time_step": 1,
            "hour": 0.5,
            "volume_m3": 42.5,
        }]
    finally:
        async with get_session() as db:
            scenario = await db.get(SimScenario, scenario_id)
            if scenario:
                await db.delete(scenario)
