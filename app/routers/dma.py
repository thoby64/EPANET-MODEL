# app/routers/dma.py
"""
DMA (District Metered Area) simulation endpoints.

Workflow
--------
GET  /dma/{filename}/layers          → GeoJSON FeatureCollections for every
                                       asset layer in the DMA (pipes, sources,
                                       tanks, valves, bulk_meters, boundary).

POST /dma/{filename}/simulate        → Build a full DMA EPANET model
                                       (multi-source, tanks, Hazen-Williams,
                                       bulk-meter monitoring) and run it.
                                       Returns a scenario_id for polling.

GET  /dma/{filename}/simulate/{id}/nrw
                                     → NRW (Non-Revenue Water) estimate for
                                       a completed simulation: compares
                                       inlet bulk meter flow with sum of
                                       junction demands.
"""

import json
import logging
import os
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator

from app.core.auth import require_api_key
from app.core.config import get_settings
from app.core.database import get_db
from app.core.exceptions import GpkgNotFoundError, InvalidGpkgError
from app.core.scenario_types import normalize_scenario_type
from app.models.simulation import SimResult, SimScenario
from app.services.dma_builder import build_dma_inp, estimate_nrw
from app.services.dma_ingest import ingest_dma, list_dma_zones
from app.services.leakage_report import analyse_leakage
from app.workers.simulation_worker import _majiscope_metadata_for_file, run_simulation_task
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/dma", tags=["dma"])


def _load_optional_layer_geojson(filename: str, layer_name: str) -> dict:
    try:
        import geopandas as gpd
    except ImportError:
        return {"type": "FeatureCollection", "features": []}

    settings = get_settings()
    gpkg_path = os.path.join(settings.gpkg_dir, filename)
    if not os.path.isfile(gpkg_path):
        return {"type": "FeatureCollection", "features": []}

    try:
        gdf = gpd.read_file(gpkg_path, layer=layer_name)
    except Exception:
        return {"type": "FeatureCollection", "features": []}

    if gdf.empty:
        return {"type": "FeatureCollection", "features": []}

    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326")
    else:
        gdf = gdf.to_crs("EPSG:4326")

    try:
        return json.loads(gdf.to_json())
    except Exception:
        return {"type": "FeatureCollection", "features": []}


# ── GET zones (multi-DMA support) ────────────────────────────────────────────

@router.get("/{filename}/zones")
def get_dma_zones(
    filename: str,
    _: str = Depends(require_api_key),
):
    """
    List all DMA zones in the GeoPackage.
    Most files contain one DMA; files covering a whole utility network
    may contain dozens. Use the returned `name` as `zone_name` in
    subsequent `/layers` and `/simulate` calls to pick a specific zone.
    """
    try:
        zones = list_dma_zones(filename)
    except GpkgNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"filename": filename, "zones": zones, "count": len(zones)}


# ── GET layers ────────────────────────────────────────────────────────────────

@router.get("/{filename}/layers")
def get_dma_layers(
    filename:  str,
    zone_name: Optional[str] = Query(None, description="DMA zone name (for multi-DMA files). Omit to use the first zone."),
    _: str = Depends(require_api_key),
):
    """
    Return all DMA asset layers as a single GeoJSON-per-layer dict.
    The frontend uses this to render the base map with icons for each
    asset type (boreholes, tanks, valves, bulk meters, DMA boundary).
    """
    try:
        dma = ingest_dma(filename, clip_to_dma=True, zone_name=zone_name)
    except GpkgNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except InvalidGpkgError as e:
        raise HTTPException(status_code=422, detail=str(e))

    def point_feature(lon, lat, props):
        return {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": props,
        }

    def line_features(pipes):
        return [
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": [[x, y] for x, y in p.coords]},
                "properties": {
                    "fid": p.fid, "diam_mm": p.diam_mm, "hw_c": p.hw_c,
                    "material": p.material, "purpose": p.purpose,
                    "length_m": round(p.length_m, 1),
                },
            }
            for p in pipes
        ]

    reported_leaks = _load_optional_layer_geojson(filename, "reported_leaks")

    return {
        "dma_name":    dma.dma_name,
        "dma_bbox":    dma.dma_bbox,
        "boundary": {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [[[lon, lat] for lon, lat in dma.dma_polygon]]},
                "properties": {"name": dma.dma_name},
            }],
        },
        "pipes": {
            "type": "FeatureCollection",
            "features": line_features(dma.pipes),
        },
        "sources": {
            "type": "FeatureCollection",
            "features": [
                point_feature(s.lon, s.lat, {
                    "fid": s.fid, "name": s.name, "elev_m": s.elev_m,
                    "yield_m3h": s.yield_m3h, "total_head_m": round(s.total_head_m, 1),
                    "status": s.status, "type": "borehole",
                })
                for s in dma.sources
            ],
        },
        "tanks": {
            "type": "FeatureCollection",
            "features": [
                point_feature(t.lon, t.lat, {
                    "fid": t.fid, "name": t.name, "elev_m": t.elev_m,
                    "cap_m3": t.cap_m3, "max_level_m": t.max_level_m,
                    "diameter_m": round(t.diameter_m, 2), "status": t.status, "type": "tank",
                })
                for t in dma.tanks
            ],
        },
        "valves": {
            "type": "FeatureCollection",
            "features": [
                point_feature(v.lon, v.lat, {
                    "fid": v.fid, "valve_type": v.valve_type,
                    "diam_mm": v.diam_mm, "is_isolation": v.is_isolation, "type": "valve",
                })
                for v in dma.valves
            ],
        },
        "bulk_meters": {
            "type": "FeatureCollection",
            "features": [
                point_feature(b.lon, b.lat, {"fid": b.fid, "name": b.name, "type": "bulk_meter"})
                for b in dma.bulk_meters
            ],
        },
        "reported_leaks": reported_leaks,
        "stats": {
            "pipe_count":     len(dma.pipes),
            "source_count":   len(dma.sources),
            "tank_count":     len(dma.tanks),
            "valve_count":    len(dma.valves),
            "bulk_meter_count": len(dma.bulk_meters),
            "reported_leak_count": len(reported_leaks.get("features", [])),
            "total_pipe_length_m": round(sum(p.length_m for p in dma.pipes), 1),
        },
    }


# ── POST simulate ──────────────────────────────────────────────────────────────

class LeakReport(BaseModel):
    """A field leak report to inject as an EPyT-Flow AbruptLeakage event."""
    lat:             float = Field(..., description="Latitude of the reported leak")
    lon:             float = Field(..., description="Longitude of the reported leak")
    diameter_m:      float = Field(0.01,  gt=0, description="Orifice diameter (m)")
    start_time_s:    int   = Field(0,     ge=0)
    end_time_s:      int   = Field(86400, ge=0)
    description:     Optional[str] = None


class DMASimRequest(BaseModel):
    name:            str   = Field("DMA hydraulic run", max_length=200)
    zone_name:       Optional[str] = Field(
        None, description="DMA zone to simulate (for multi-zone files). "
                          "Omit to use the first zone."
    )
    duration_hrs:    int   = Field(24, ge=1, le=168)
    time_step_min:   int   = Field(60, ge=5,  le=360)
    base_demand_m3h: float = Field(0.011, gt=0, description="Demand per junction (m³/h)")
    leakage_frac:    float = Field(0.20,  ge=0, le=1.0,
                                   description="Extra demand fraction modelling background leakage (0.20 = +20%)")
    scenario_type:   Optional[str] = Field(
        None,
        description="Simulation scenario classification. If omitted, it is derived from reported leak data.",
    )
    demand_model:    str = Field("DDA", description="Hydraulic demand model")
    pda_pressure_min: Optional[float] = Field(None, ge=0)
    pda_pressure_required: Optional[float] = Field(None, ge=0)
    pda_pressure_exponent: Optional[float] = Field(None, ge=0)
    leak_reports:    Optional[List[LeakReport]] = Field(
        None,
        description="Active field leak reports to inject as EPyT-Flow AbruptLeakage events."
    )

    @model_validator(mode="after")
    def normalize_contract(self) -> "DMASimRequest":
        has_reports = bool(self.leak_reports)
        self.scenario_type = normalize_scenario_type(self.scenario_type, has_reports)
        self.demand_model = (self.demand_model or "DDA").strip().upper()
        if self.demand_model not in {"DDA", "PDA"}:
            raise ValueError("demand_model must be DDA or PDA.")
        return self


@router.post("/{filename}/simulate", status_code=202)
async def simulate_dma(
    filename:   str,
    body:       DMASimRequest,
    background: BackgroundTasks,
    db:         AsyncSession = Depends(get_db),
    _:          str          = Depends(require_api_key),
):
    """
    Build a full DMA EPANET model and run it in the background.

    The model includes:
    - All OPERATIONAL boreholes as Reservoir nodes (pump-boosted head)
    - All OPERATING storage tanks with real capacity/elevation geometry
    - Junction demands calibrated to `base_demand_m3h` per node
    - `leakage_frac` extra demand at every node (background leakage signal)
    - Hazen-Williams C from pipe material
    - Bulk meter nodes at the DMA inlet/outlet (zero demand, used for NRW)

    Returns a scenario_id to poll via GET /simulate/{id} and GET /dma/{file}/simulate/{id}/nrw
    """
    try:
        dma = ingest_dma(filename, clip_to_dma=True, zone_name=body.zone_name)
    except GpkgNotFoundError:
        raise HTTPException(status_code=404, detail=f"File not found: {filename}")
    except InvalidGpkgError as e:
        raise HTTPException(status_code=422, detail=str(e))

    if not dma.pipes:
        raise HTTPException(status_code=422, detail="No operational pipes found in the DMA.")
    if not dma.sources and not dma.tanks:
        raise HTTPException(status_code=422, detail="No water sources or tanks found in the DMA.")

    import tempfile
    inp_dir  = tempfile.mkdtemp(prefix="dma_epyt_")
    try:
        inp_path, repair_report = build_dma_inp(
            dma             = dma,
            inp_dir         = inp_dir,
            duration_hrs    = body.duration_hrs,
            time_step_min   = body.time_step_min,
            base_demand_m3h = body.base_demand_m3h,
            leakage_frac    = body.leakage_frac,
        )
    except Exception as e:
        logger.exception("DMA .inp build failed")
        raise HTTPException(status_code=422, detail=f"Failed to build EPANET model: {e}")

    # Serialise connectors for the response
    connectors_geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [list(c.from_lonlat), list(c.to_lonlat)],
                },
                "properties": {
                    "id":        c.connector_id,
                    "length_m":  c.length_m,
                    "diam_mm":   c.diam_mm,
                    "material":  c.material,
                    "reason":    c.reason,
                },
            }
            for c in repair_report.connectors_added
        ],
    }

    majiscope_metadata = _majiscope_metadata_for_file(filename)
    scenario = SimScenario(
        gpkg_filename = filename,
        name          = body.name,
        description   = f"DMA simulation - {dma.dma_name}",
        base_demand   = body.base_demand_m3h,
        duration_hrs  = body.duration_hrs,
        time_step_min = body.time_step_min,
        leakage_frac  = body.leakage_frac,
        scenario_type = body.scenario_type or "baseline",
        demand_model  = body.demand_model,
        pda_pressure_min      = body.pda_pressure_min,
        pda_pressure_required = body.pda_pressure_required,
        pda_pressure_exponent = body.pda_pressure_exponent,
        extra_demands = {
            "_dma_inp_path":        inp_path,
            "_dma_name":            dma.dma_name,
            "_zone_name":           body.zone_name,
            "_leakage_frac":        body.leakage_frac,
            "_scenario_type":       body.scenario_type,
            "_demand_model":        body.demand_model,
            "_pda_pressure_min":      body.pda_pressure_min,
            "_pda_pressure_required": body.pda_pressure_required,
            "_pda_pressure_exponent": body.pda_pressure_exponent,
            "_total_demand_m3h":    round(body.base_demand_m3h * (1 + body.leakage_frac), 6),
            "_connectors_added":    len(repair_report.connectors_added),
            "_connector_length_m":  repair_report.total_connector_length_m,
            "_original_components": repair_report.original_component_count,
            "_repair_warnings":     repair_report.warnings,
            "_leak_reports": [
                lr.model_dump() for lr in body.leak_reports
            ] if body.leak_reports else [],
            "_majiscope": majiscope_metadata,
        },
        status = "PENDING",
    )
    db.add(scenario)
    await db.commit()
    await db.refresh(scenario)

    background.add_task(run_simulation_task, scenario.id)
    logger.info("Queued DMA scenario %d for '%s'", scenario.id, filename)
    return {
        "id":                   scenario.id,
        "status":               "PENDING",
        "dma_name":             dma.dma_name,
        "topology_repair": {
            "original_components":    repair_report.original_component_count,
            "connectors_added":       len(repair_report.connectors_added),
            "total_connector_length_m": repair_report.total_connector_length_m,
            "warnings":               repair_report.warnings,
            "connectors_geojson":     connectors_geojson,
        },
    }


# ── GET NRW ───────────────────────────────────────────────────────────────────

@router.get("/{filename}/simulate/{scenario_id}/nrw")
async def get_nrw(
    filename:    str,
    scenario_id: int,
    db:          AsyncSession = Depends(get_db),
    _:           str          = Depends(require_api_key),
):
    """
    Compute Non-Revenue Water (NRW) for a completed DMA simulation.

    NRW = system input − authorised consumption
    Where:
      system_input   = sum of flows at inlet bulk-meter pipe segments
      authorised     = sum of all junction demands in the simulation
    """
    scenario = await db.get(SimScenario, scenario_id)
    if not scenario or scenario.gpkg_filename != filename:
        raise HTTPException(status_code=404, detail="Scenario not found.")
    if scenario.status != "DONE":
        raise HTTPException(status_code=409, detail=f"Simulation is {scenario.status}, not DONE.")
    if not scenario.extra_demands or "_dma_inp_path" not in scenario.extra_demands:
        raise HTTPException(status_code=422, detail="This scenario is not a DMA simulation.")

    summary = scenario.summary or {}
    extra   = scenario.extra_demands or {}

    # Prefer the EPANET .rpt flow balance (most accurate)
    rpt_fb = summary.get("epanet_flow_balance")
    if rpt_fb and rpt_fb.get("source") == "epanet_rpt":
        nrw_source = "epanet_rpt"
        inlet_flow   = rpt_fb.get("total_inflow_m3h",    0.0)
        outlet_flow  = 0.0   # EPANET inflow is already net
        sim_demand   = rpt_fb.get("consumer_demand_m3h", 0.0)
    else:
        nrw_source   = "estimated"
        total_demand = extra.get("_total_demand_m3h", 0.011)
        inlet_flow   = summary.get("inlet_flow_m3h",  0.0)
        outlet_flow  = summary.get("outlet_flow_m3h", 0.0)
        sim_demand   = summary.get("total_demand_m3h", total_demand)
        if inlet_flow == 0.0:
            inlet_flow  = sim_demand
            outlet_flow = 0.0

    nrw = estimate_nrw(
        inlet_flow_m3h   = inlet_flow,
        total_demand_m3h = sim_demand,
        outlet_flow_m3h  = outlet_flow,
    )

    return {
        "scenario_id":        scenario_id,
        "dma_name":           extra.get("_dma_name"),
        "nrw_source":         nrw_source,
        "simulation_summary": summary,
        "nrw":                nrw,
        "epanet_flow_balance": rpt_fb,
    }


# ── GET leakage report ────────────────────────────────────────────────────────

@router.get("/{filename}/simulate/{scenario_id}/leakage")
async def get_leakage_report(
    filename:    str,
    scenario_id: int,
    db:          AsyncSession = Depends(get_db),
    _:           str          = Depends(require_api_key),
):
    """
    Full leakage analysis for a completed DMA simulation.

    Returns NRW estimate, pressure zone breakdown, per-pipe risk scores,
    hotspot GeoJSON (top-50 risk pipes), and hourly flow balance.
    """
    scenario = await db.get(SimScenario, scenario_id)
    if not scenario or scenario.gpkg_filename != filename:
        raise HTTPException(status_code=404, detail="Scenario not found.")
    if scenario.status != "DONE":
        raise HTTPException(status_code=409, detail=f"Simulation is {scenario.status}, not DONE.")
    if not scenario.extra_demands or "_dma_inp_path" not in (scenario.extra_demands or {}):
        raise HTTPException(status_code=422, detail="Not a DMA simulation.")

    # Re-run the leakage analysis from stored results
    # (We store node/pipe results in SimResult rows — fetch and reconstruct)
    from sqlalchemy import select as sa_select
    from app.models.simulation import SimResult
    from app.services.simulation_service import NodeResult, PipeResult, SimulationOutput

    results_q = await db.execute(
        sa_select(SimResult).where(SimResult.scenario_id == scenario_id)
    )
    sim_results = results_q.scalars().all()

    node_results, pipe_results = [], []
    for sr in sim_results:
        if sr.element_type == "node":
            node_results.append(NodeResult(
                element_id      = sr.element_id,
                time_step       = sr.time_step,
                lat             = sr.lat,
                lon             = sr.lon,
                pressure        = sr.pressure,
                head            = sr.head,
                demand          = sr.demand,
                water_age       = sr.water_age,
                is_low_pressure = sr.is_low_pressure,
            ))
        elif sr.element_type == "pipe":
            pipe_results.append(PipeResult(
                element_id       = sr.element_id,
                time_step        = sr.time_step,
                lat              = sr.lat,
                lon              = sr.lon,
                flow_rate        = sr.flow_rate,
                velocity         = sr.velocity,
                headloss         = sr.headloss,
                is_high_velocity = sr.is_high_velocity,
            ))

    sim_output = SimulationOutput(
        node_results = node_results,
        pipe_results = pipe_results,
        summary      = scenario.summary or {},
    )

    extra = scenario.extra_demands or {}
    report = analyse_leakage(
        output           = sim_output,
        scenario_id      = scenario_id,
        dma_name         = extra.get("_dma_name", "DMA"),
        base_demand_m3h  = scenario.base_demand or 0.011,
        leakage_frac     = extra.get("_leakage_frac", 0.20),
    )

    return {
        "scenario_id":      scenario_id,
        "dma_name":         report.dma_name,
        "warnings":         report.warnings,
        "nrw": {
            "system_input_m3h":   report.nrw.system_input_m3h,
            "authorised_m3h":     report.nrw.authorised_m3h,
            "nrw_m3h":            report.nrw.nrw_m3h,
            "nrw_pct":            report.nrw.nrw_pct,
            "real_loss_m3h":      report.nrw.real_loss_m3h,
            "apparent_loss_m3h":  report.nrw.apparent_loss_m3h,
            "ili":                report.nrw.ili,
        },
        "pressure_zones": [
            {"zone": z.zone, "count": z.count, "pct": z.avg_pct, "node_ids": z.node_ids[:20]}
            for z in report.pressure_zones
        ],
        "pipe_risks_top20": [
            {
                "pipe_id":    r.pipe_id,
                "lat":        r.lat,
                "lon":        r.lon,
                "risk_score": r.risk_score,
                "risk_level": r.risk_level,
                "drivers":    r.drivers,
                "avg_flow":   r.avg_flow,
                "min_pressure_adjacent": r.min_pressure_adjacent,
            }
            for r in report.pipe_risks[:20]
        ],
        "hotspots_geojson":   report.hotspots,
        "timestep_balance":   [
            {"hour": b.hour, "inflow_m3h": b.inflow_m3h,
             "demand_m3h": b.demand_m3h, "nrw_m3h": b.nrw_m3h}
            for b in report.timestep_balance
        ],
    }
