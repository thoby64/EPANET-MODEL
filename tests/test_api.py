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
