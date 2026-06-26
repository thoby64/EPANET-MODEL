# app/routers/simulation.py
"""
Simulation result endpoints (read-only).

Simulations are started via POST /dma/{file}/simulate.
These endpoints are for polling status and fetching results.

GET    /simulate                    List all scenarios
GET    /simulate/{id}               Status + summary
GET    /simulate/{id}/nodes         Node results (pressure, head, water age)
GET    /simulate/{id}/pipes         Pipe results (flow, velocity, headloss)
GET    /simulate/{id}/alerts        Low-pressure nodes + high-velocity pipes
GET    /simulate/{id}/geojson/nodes Node results as GeoJSON
GET    /simulate/{id}/geojson/pipes Pipe results as GeoJSON
DELETE /simulate/{id}               Delete scenario and all its results
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_api_key
from app.core.database import get_db
from app.core.exceptions import SimulationNotFoundError, SimulationStillRunningError
from app.models.simulation import SimResult, SimScenario

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/simulate", tags=["simulation"])


# ── response schemas ──────────────────────────────────────────────────────────

class ScenarioResponse(BaseModel):
    id:            int
    gpkg_filename: str
    name:          str
    status:        str
    base_demand:   float
    duration_hrs:  int
    time_step_min: int
    leakage_frac:  float
    created_at:    datetime
    started_at:    Optional[datetime]
    finished_at:   Optional[datetime]
    error_message: Optional[str]
    summary:       Optional[Dict[str, Any]]

    model_config = ConfigDict(from_attributes=True)


class NodeResultResponse(BaseModel):
    element_id:      str
    time_step:       int
    lat:             Optional[float]
    lon:             Optional[float]
    pressure:        Optional[float]
    head:            Optional[float]
    demand:          Optional[float]
    water_age:       Optional[float]
    is_low_pressure: bool

    model_config = ConfigDict(from_attributes=True)


class PipeResultResponse(BaseModel):
    element_id:       str
    time_step:        int
    lat:              Optional[float]
    lon:              Optional[float]
    flow_rate:        Optional[float]
    velocity:         Optional[float]
    headloss:         Optional[float]
    is_high_velocity: bool

    model_config = ConfigDict(from_attributes=True)


class AlertsResponse(BaseModel):
    scenario_id:         int
    time_step:           Optional[int]
    low_pressure_nodes:  List[NodeResultResponse]
    high_velocity_pipes: List[PipeResultResponse]


# ── helpers ───────────────────────────────────────────────────────────────────

async def _get_or_404(scenario_id: int, db: AsyncSession) -> SimScenario:
    s = await db.get(SimScenario, scenario_id)
    if not s:
        raise SimulationNotFoundError(scenario_id)
    return s


def _to_geojson_feature(r: SimResult) -> dict:
    geometry = (
        {"type": "Point", "coordinates": [r.lon, r.lat]}
        if r.lat and r.lon else None
    )
    props: Dict[str, Any] = {
        "element_id":   r.element_id,
        "element_type": r.element_type,
        "time_step":    r.time_step,
    }
    if r.element_type == "node":
        props.update({
            "pressure":        r.pressure,
            "head":            r.head,
            "demand":          r.demand,
            "water_age":       r.water_age,
            "is_low_pressure": r.is_low_pressure,
        })
    else:
        props.update({
            "flow_rate":        r.flow_rate,
            "velocity":         r.velocity,
            "headloss":         r.headloss,
            "is_high_velocity": r.is_high_velocity,
        })
    return {"type": "Feature", "geometry": geometry, "properties": props}


# ── endpoints ─────────────────────────────────────────────────────────────────

@router.get("", response_model=List[ScenarioResponse])
async def list_simulations(
    limit:  int           = Query(20, ge=1, le=100),
    offset: int           = Query(0,  ge=0),
    status: Optional[str] = Query(None),
    db:     AsyncSession  = Depends(get_db),
    _:      str           = Depends(require_api_key),
):
    """List simulation scenarios, newest first."""
    q = select(SimScenario).order_by(SimScenario.created_at.desc()).offset(offset).limit(limit)
    if status:
        q = q.where(SimScenario.status == status.upper())
    return (await db.execute(q)).scalars().all()


@router.get("/{scenario_id}", response_model=ScenarioResponse)
async def get_simulation(
    scenario_id: int,
    db:          AsyncSession = Depends(get_db),
    _:           str          = Depends(require_api_key),
):
    """Poll until `status` is `DONE` or `FAILED`."""
    return await _get_or_404(scenario_id, db)


@router.get("/{scenario_id}/nodes", response_model=List[NodeResultResponse])
async def get_node_results(
    scenario_id: int,
    time_step:   Optional[int] = Query(None, description="Hour index (0-based). Omit for all timesteps."),
    db:          AsyncSession  = Depends(get_db),
    _:           str           = Depends(require_api_key),
):
    s = await _get_or_404(scenario_id, db)
    if s.status != "DONE":
        raise SimulationStillRunningError(scenario_id)
    q = select(SimResult).where(
        SimResult.scenario_id  == scenario_id,
        SimResult.element_type == "node",
    )
    if time_step is not None:
        q = q.where(SimResult.time_step == time_step)
    return (await db.execute(q)).scalars().all()


@router.get("/{scenario_id}/pipes", response_model=List[PipeResultResponse])
async def get_pipe_results(
    scenario_id: int,
    time_step:   Optional[int] = Query(None),
    db:          AsyncSession  = Depends(get_db),
    _:           str           = Depends(require_api_key),
):
    s = await _get_or_404(scenario_id, db)
    if s.status != "DONE":
        raise SimulationStillRunningError(scenario_id)
    q = select(SimResult).where(
        SimResult.scenario_id  == scenario_id,
        SimResult.element_type == "pipe",
    )
    if time_step is not None:
        q = q.where(SimResult.time_step == time_step)
    return (await db.execute(q)).scalars().all()


@router.get("/{scenario_id}/alerts", response_model=AlertsResponse)
async def get_alerts(
    scenario_id: int,
    time_step:   Optional[int] = Query(None),
    db:          AsyncSession  = Depends(get_db),
    _:           str           = Depends(require_api_key),
):
    """Anomalous elements: nodes below pressure threshold + pipes above velocity threshold."""
    s = await _get_or_404(scenario_id, db)
    if s.status != "DONE":
        raise SimulationStillRunningError(scenario_id)

    def _ts(q, ts):
        return q.where(SimResult.time_step == ts) if ts is not None else q

    low_p  = (await db.execute(_ts(select(SimResult).where(
        SimResult.scenario_id == scenario_id,
        SimResult.element_type == "node",
        SimResult.is_low_pressure == True,   # noqa: E712
    ), time_step))).scalars().all()

    high_v = (await db.execute(_ts(select(SimResult).where(
        SimResult.scenario_id  == scenario_id,
        SimResult.element_type == "pipe",
        SimResult.is_high_velocity == True,  # noqa: E712
    ), time_step))).scalars().all()

    return AlertsResponse(
        scenario_id=scenario_id, time_step=time_step,
        low_pressure_nodes=low_p, high_velocity_pipes=high_v,
    )


@router.get("/{scenario_id}/geojson/nodes")
async def get_nodes_geojson(
    scenario_id: int,
    time_step:   Optional[int] = Query(None),
    db:          AsyncSession  = Depends(get_db),
    _:           str           = Depends(require_api_key),
):
    """Node results as GeoJSON FeatureCollection — load directly into Leaflet."""
    s = await _get_or_404(scenario_id, db)
    if s.status != "DONE":
        raise SimulationStillRunningError(scenario_id)
    q = select(SimResult).where(
        SimResult.scenario_id  == scenario_id,
        SimResult.element_type == "node",
    )
    if time_step is not None:
        q = q.where(SimResult.time_step == time_step)
    rows = (await db.execute(q)).scalars().all()
    return {"type": "FeatureCollection", "features": [_to_geojson_feature(r) for r in rows]}


@router.get("/{scenario_id}/geojson/pipes")
async def get_pipes_geojson(
    scenario_id: int,
    time_step:   Optional[int] = Query(None),
    db:          AsyncSession  = Depends(get_db),
    _:           str           = Depends(require_api_key),
):
    """Pipe results as GeoJSON FeatureCollection (centroid geometry)."""
    s = await _get_or_404(scenario_id, db)
    if s.status != "DONE":
        raise SimulationStillRunningError(scenario_id)
    q = select(SimResult).where(
        SimResult.scenario_id  == scenario_id,
        SimResult.element_type == "pipe",
    )
    if time_step is not None:
        q = q.where(SimResult.time_step == time_step)
    rows = (await db.execute(q)).scalars().all()
    return {"type": "FeatureCollection", "features": [_to_geojson_feature(r) for r in rows]}


@router.delete("/{scenario_id}", status_code=204)
async def delete_simulation(
    scenario_id: int,
    db:          AsyncSession = Depends(get_db),
    _:           str          = Depends(require_api_key),
):
    s = await _get_or_404(scenario_id, db)
    if s.status == "RUNNING":
        raise SimulationStillRunningError(scenario_id)
    await db.delete(s)
    await db.commit()
