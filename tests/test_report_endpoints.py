# tests/test_report_endpoints.py
"""
The report endpoints must work even when the .rpt file is not on disk.

On the deployed service EPANET could not write its report file, so
"View report" and "Download report" both failed with a 404. The report text
is now also stored in the database (`sim_reports`), and the endpoints fall
back to that copy.
"""

import os

import pytest
from httpx import AsyncClient, ASGITransport

os.environ.setdefault("API_KEYS", "test-key-123")
os.environ.setdefault("GPKG_DIR", "/data/gpkg")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:////tmp/test_epanet.db")

from app.main import app                     # noqa: E402
from app.core.database import get_session    # noqa: E402
from app.models.simulation import SimReport, SimScenario  # noqa: E402
from app.services.rpt_parser import persisted_report_path  # noqa: E402

TEST_KEY = "test-key-123"
AUTH = {"X-API-Key": TEST_KEY}

REPORT_TEXT = """  Page 1                                    Thu Oct 22 11:57:04 2026
  Hydraulic Flow Balance (m3/h)
  ================================
  Total Inflow:           151.414
  Consumer Demand:          3.548
  Demand Deficit:           0.000
  Emitter Flow:             0.065
  Leakage Flow:             0.000
  Total Outflow:          186.040
  Storage Flow:           -34.627
  Flow Ratio:               1.000
  ================================

  Analysis ended Thu Oct 22 11:57:04 2026
"""


@pytest.fixture(scope="module")
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


@pytest.fixture(scope="module")
async def done_scenario():
    """A finished scenario that has a report in the database but no file."""
    async with get_session() as session:
        scenario = SimScenario(
            gpkg_filename = "majiscope_report_test.gpkg",
            input_type    = "gpkg",
            name          = "report fallback test",
            status        = "DONE",
            summary       = {"report_available": True},
        )
        session.add(scenario)
        await session.flush()
        await session.refresh(scenario)
        scenario_id = scenario.id
        session.add(SimReport(
            scenario_id = scenario_id,
            text        = REPORT_TEXT,
            size_bytes  = len(REPORT_TEXT.encode("utf-8")),
        ))
        await session.commit()

    # make sure there is no file for this scenario
    path = persisted_report_path(scenario_id)
    if os.path.isfile(path):
        os.remove(path)

    yield scenario_id


@pytest.mark.asyncio
async def test_report_text_falls_back_to_the_database(client, done_scenario):
    r = await client.get(f"/simulate/{done_scenario}/report", headers=AUTH)
    assert r.status_code == 200
    assert "Hydraulic Flow Balance" in r.text
    assert "Analysis ended" in r.text


@pytest.mark.asyncio
async def test_report_summary_falls_back_to_the_database(client, done_scenario):
    r = await client.get(f"/simulate/{done_scenario}/report/summary", headers=AUTH)
    assert r.status_code == 200
    data = r.json()
    assert data["balanced"] is True
    assert data["flow_balance"]["total_inflow_m3h"] == pytest.approx(151.414)


@pytest.mark.asyncio
async def test_report_download_falls_back_to_the_database(client, done_scenario):
    r = await client.get(f"/simulate/{done_scenario}/report/download", headers=AUTH)
    assert r.status_code == 200
    assert "Hydraulic Flow Balance" in r.text
    disposition = r.headers.get("content-disposition", "")
    assert "attachment" in disposition
    assert f"scenario_{done_scenario}.rpt" in disposition


@pytest.mark.asyncio
async def test_report_endpoints_404_when_nothing_is_stored(client):
    """A scenario with neither a file nor a database copy is a clean 404."""
    async with get_session() as session:
        scenario = SimScenario(
            gpkg_filename = "majiscope_noreport.gpkg",
            input_type    = "gpkg",
            name          = "no report",
            status        = "DONE",
        )
        session.add(scenario)
        await session.flush()
        await session.refresh(scenario)
        scenario_id = scenario.id
        await session.commit()

    path = persisted_report_path(scenario_id)
    if os.path.isfile(path):
        os.remove(path)

    for suffix in ("report", "report/summary", "report/download"):
        r = await client.get(f"/simulate/{scenario_id}/{suffix}", headers=AUTH)
        assert r.status_code == 404, suffix
