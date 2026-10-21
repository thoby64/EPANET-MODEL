# app/workers/simulation_worker.py
"""
Background task — DMA EPyT-Flow simulation runner.

Pipeline
--------
1. Mark scenario RUNNING.
2. Use pre-built .inp path stored in extra_demands["_dma_inp_path"].
3. Resolve any lat/lon leak events → nearest EPANET node.
4. Run EPyT-Flow ScenarioSimulator.
5. Persist node/pipe results to sim_results rows.
6. Parse the EPANET .rpt for real flow balance (NRW figures).
7. Mark scenario DONE (or FAILED).
"""

import logging
import json
import math
import os
import re
import shutil
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from sqlalchemy import select

from app.core.database import get_session
from app.core.config import get_settings
from app.core.tz import utc_isoformat
from app.models.simulation import SimResult, SimScenario
from app.services.leakage_report import analyse_leakage
from app.services.rpt_parser import parse_rpt, persisted_report_path, rpt_nrw_summary
from app.services.simulation_service import run_simulation

logger = logging.getLogger(__name__)


def _execution_duration_seconds(scenario: SimScenario) -> float | None:
    if not scenario.finished_at or not scenario.started_at:
        return None
    finished_at = scenario.finished_at
    started_at = scenario.started_at
    if (finished_at.tzinfo is None) != (started_at.tzinfo is None):
        finished_at = finished_at.replace(tzinfo=None)
        started_at = started_at.replace(tzinfo=None)
    return max(0.0, (finished_at - started_at).total_seconds())


def _public_hydraulic_snapshot_value(value):
    """Remove engine-specific EPANET naming from the MajiScope snapshot contract."""
    if isinstance(value, dict):
        return {
            str(key).replace("EPANET", "HYDRAULIC").replace("Epanet", "Hydraulic").replace("epanet", "hydraulic"):
            _public_hydraulic_snapshot_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_public_hydraulic_snapshot_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, str):
        return value.replace("EPANET", "HYDRAULIC").replace("Epanet", "Hydraulic").replace("epanet", "hydraulic")
    return value


def _status_event_payload(rpt, limit: int = 80) -> list[dict]:
    events = []
    for event in (getattr(rpt, "status_events", None) or [])[:limit]:
        events.append({
            "time": getattr(event, "time_hms", None),
            "message": getattr(event, "message", None),
        })
    return events


def _hydraulic_report_artifacts(rpt) -> dict:
    if not rpt:
        return {}

    flow_balance = rpt_nrw_summary(rpt)
    artifacts = {
        "source": "hydraulic_rpt",
        "balanced": bool(getattr(rpt, "balanced", True)),
        "mass_ratio": getattr(rpt, "mass_ratio", None),
        "flow_balance": flow_balance,
        "status_event_count": len(getattr(rpt, "status_events", None) or []),
        "status_events": _status_event_payload(rpt),
        "warnings": list(getattr(rpt, "warnings", None) or []),
    }

    fb = getattr(rpt, "flow_balance", None)
    if fb:
        artifacts["hydraulic_balance"] = {
            "total_inflow_m3h": round(fb.total_inflow_m3h, 3),
            "consumer_demand_m3h": round(fb.consumer_demand_m3h, 3),
            "demand_deficit_m3h": round(fb.demand_deficit_m3h, 3),
            "leakage_flow_m3h": round(fb.leakage_flow_m3h, 3),
            "total_outflow_m3h": round(fb.total_outflow_m3h, 3),
            "storage_flow_m3h": round(fb.storage_flow_m3h, 3),
            "flow_ratio": round(fb.flow_ratio, 4),
            "nrw_m3h": round(fb.nrw_m3h, 3),
            "nrw_pct": round(fb.nrw_pct, 1),
        }

    return artifacts


def _snapshot_input_parameters(scenario: SimScenario) -> dict:
    return {
        "gpkg_filename": scenario.gpkg_filename,
        "input_type": scenario.input_type,
        "input_filename": scenario.input_filename,
        "scenario_type": scenario.scenario_type,
        "demand_model": scenario.demand_model,
        "base_demand": scenario.base_demand,
        "duration_hrs": scenario.duration_hrs,
        "time_step_min": scenario.time_step_min,
        "leakage_frac": scenario.leakage_frac,
        "pda_pressure_min": scenario.pda_pressure_min,
        "pda_pressure_required": scenario.pda_pressure_required,
        "pda_pressure_exponent": scenario.pda_pressure_exponent,
        "extra_demands": {
            key: value
            for key, value in (scenario.extra_demands or {}).items()
            if key not in {"_majiscope", "_dma_inp_path"}
        },
    }


def _majiscope_metadata_for_file(gpkg_filename: str) -> dict | None:
    if not gpkg_filename.startswith("majiscope_") or not gpkg_filename.endswith(".gpkg"):
        return None
    settings = get_settings()
    stem = gpkg_filename[:-5]
    metadata_path = os.path.join(settings.gpkg_dir, f"{stem}.json")
    if not os.path.isfile(metadata_path):
        return None
    try:
        with open(metadata_path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception as exc:
        logger.warning("Failed to read MajiScope metadata for %s: %s", gpkg_filename, exc)
        return None


def _majiscope_metadata_for_scenario(scenario: SimScenario) -> dict | None:
    embedded = (scenario.extra_demands or {}).get("_majiscope")
    if isinstance(embedded, dict) and embedded.get("session_id"):
        return embedded
    return _majiscope_metadata_for_file(scenario.gpkg_filename)


def _build_pressure_time_series(output, time_step_min: int) -> list[dict]:
    """Return compact, graph-ready pressure aggregates for each model timestep."""
    grouped: Dict[float, list] = {}
    for row in output.node_results:
        if row.pressure is None:
            continue
        grouped.setdefault(float(row.time_step), []).append(row)

    series = []
    for time_step in sorted(grouped):
        rows = grouped[time_step]
        pressures = [float(row.pressure) for row in rows]
        series.append({
            "hour": round(time_step * time_step_min / 60, 4),
            "pressure_min_m": round(min(pressures), 3),
            "pressure_avg_m": round(sum(pressures) / len(pressures), 3),
            "pressure_max_m": round(max(pressures), 3),
            "low_pressure_nodes": sum(1 for row in rows if row.is_low_pressure),
            "critical_pressure_nodes": sum(1 for pressure in pressures if pressure < 5),
            "node_count": len(rows),
        })
    return series


def _build_detailed_results(output, time_step_min: int) -> dict:
    """Build the complete per-element result payload sent to MajiScope."""
    grouped_nodes: Dict[int, list] = {}
    grouped_pipes: Dict[int, list] = {}
    grouped_tanks: Dict[int, list] = {}
    for row in output.node_results:
        grouped_nodes.setdefault(int(row.time_step), []).append(row)
    for row in output.pipe_results:
        grouped_pipes.setdefault(int(row.time_step), []).append(row)
    for row in output.tank_results:
        grouped_tanks.setdefault(int(row.time_step), []).append(row)

    time_series = []
    for time_step in sorted(set(grouped_nodes) | set(grouped_pipes) | set(grouped_tanks)):
        nodes = grouped_nodes.get(time_step, [])
        pipes = grouped_pipes.get(time_step, [])
        tanks = grouped_tanks.get(time_step, [])
        pressures = [float(row.pressure) for row in nodes if row.pressure is not None]
        ages = [float(row.water_age) for row in nodes if row.water_age is not None]
        demands = [float(row.demand) for row in nodes if row.demand is not None]
        flows = [abs(float(row.flow_rate)) for row in pipes if row.flow_rate is not None]
        velocities = [abs(float(row.velocity)) for row in pipes if row.velocity is not None]
        time_series.append({
            "time_step": time_step,
            "hour": round(time_step * time_step_min / 60, 4),
            "pressure_min_m": min(pressures) if pressures else None,
            "pressure_avg_m": sum(pressures) / len(pressures) if pressures else None,
            "pressure_max_m": max(pressures) if pressures else None,
            "water_age_avg_hrs": sum(ages) / len(ages) if ages else None,
            "water_age_max_hrs": max(ages) if ages else None,
            "total_demand_m3h": sum(demands) * 3600 if demands else None,
            "low_pressure_nodes": sum(bool(row.is_low_pressure) for row in nodes),
            "flow_avg_abs_m3s": sum(flows) / len(flows) if flows else None,
            "flow_max_abs_m3s": max(flows) if flows else None,
            "velocity_max_abs_mps": max(velocities) if velocities else None,
            "tank_volumes_m3": {
                str(row.element_id): row.volume_m3 for row in tanks
            },
        })

    return {
        "version": 1,
        "time_step_min": time_step_min,
        "time_series": time_series,
        "nodes": [
            {
                "time_step": row.time_step,
                "element_id": row.element_id,
                "lat": row.lat,
                "lon": row.lon,
                "pressure_m": row.pressure,
                "head_m": row.head,
                "demand_m3s": row.demand,
                "water_age_hrs": row.water_age,
                "is_low_pressure": row.is_low_pressure,
            }
            for row in output.node_results
        ],
        "pipes": [
            {
                "time_step": row.time_step,
                "element_id": row.element_id,
                "lat": row.lat,
                "lon": row.lon,
                "flow_m3s": row.flow_rate,
                "velocity_mps": row.velocity,
                "headloss_m": row.headloss,
                "is_high_velocity": row.is_high_velocity,
            }
            for row in output.pipe_results
        ],
        "tanks": [
            {
                "time_step": row.time_step,
                "element_id": row.element_id,
                "lat": row.lat,
                "lon": row.lon,
                "volume_m3": row.volume_m3,
            }
            for row in output.tank_results
        ],
        "report_text": None,
    }


async def _deliver_majiscope_payload(*, scenario: SimScenario, session) -> bool:
    settings = get_settings()
    payload = scenario.majiscope_snapshot_payload
    if not payload:
        return False
    scenario.majiscope_snapshot_attempts = (scenario.majiscope_snapshot_attempts or 0) + 1
    if not settings.majiscope_backend_url or not settings.majiscope_callback_secret:
        scenario.majiscope_snapshot_status = "PENDING"
        scenario.majiscope_snapshot_last_error = "MajiScope callback configuration is incomplete"
        await session.commit()
        return False
    try:
        import httpx
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                settings.majiscope_backend_url.rstrip("/") + "/api/hydraulic-model/snapshots",
                json=payload,
                headers={"X-MajiScope-Hydraulic-Secret": settings.majiscope_callback_secret},
            )
        if response.status_code >= 400:
            raise RuntimeError(f"MajiScope returned HTTP {response.status_code}: {response.text[:500]}")
        scenario.majiscope_snapshot_status = "DELIVERED"
        scenario.majiscope_snapshot_last_error = None
        scenario.majiscope_snapshot_delivered_at = datetime.now(timezone.utc)
        await session.commit()
        logger.info("[%s] MajiScope hydraulic snapshot delivered", scenario.id)
        return True
    except Exception as exc:
        scenario.majiscope_snapshot_status = "RETRY"
        scenario.majiscope_snapshot_last_error = str(exc)[:2000]
        await session.commit()
        logger.warning("[%s] MajiScope snapshot delivery deferred: %s", scenario.id, exc)
        return False


async def retry_pending_majiscope_snapshots() -> int:
    delivered = 0
    async with get_session() as session:
        rows = (await session.execute(
            select(SimScenario).where(
                SimScenario.majiscope_snapshot_payload.is_not(None),
                SimScenario.majiscope_snapshot_status != "DELIVERED",
                SimScenario.majiscope_snapshot_attempts < 100,
            ).order_by(SimScenario.finished_at.asc()).limit(25)
        )).scalars().all()
        for scenario in rows:
            if await _deliver_majiscope_payload(scenario=scenario, session=session):
                delivered += 1
    return delivered


async def _post_majiscope_snapshot(
    *,
    scenario: SimScenario,
    output,
    merged_summary: dict,
    session,
) -> None:
    settings = get_settings()
    metadata = _majiscope_metadata_for_scenario(scenario)
    if not metadata:
        return

    try:
        report = analyse_leakage(
            output=output,
            scenario_id=scenario.id,
            dma_name=(scenario.extra_demands or {}).get("_dma_name", metadata.get("dma_name", "DMA")),
            base_demand_m3h=scenario.base_demand or 0.011,
            leakage_frac=(scenario.extra_demands or {}).get("_leakage_frac", 0.20),
        )
        leakage_json = {
            "scenario_id": scenario.id,
            "dma_name": report.dma_name,
            "warnings": report.warnings,
            "nrw": {
                "system_input_m3h": report.nrw.system_input_m3h,
                "authorised_m3h": report.nrw.authorised_m3h,
                "nrw_m3h": report.nrw.nrw_m3h,
                "nrw_pct": report.nrw.nrw_pct,
                "real_loss_m3h": report.nrw.real_loss_m3h,
                "apparent_loss_m3h": report.nrw.apparent_loss_m3h,
                "ili": report.nrw.ili,
            },
            "pressure_zones": [
                {"zone": z.zone, "count": z.count, "pct": z.avg_pct, "node_ids": z.node_ids[:20]}
                for z in report.pressure_zones
            ],
            "pipe_risks_top20": [
                {
                    "pipe_id": r.pipe_id,
                    "lat": r.lat,
                    "lon": r.lon,
                    "risk_score": r.risk_score,
                    "risk_level": r.risk_level,
                    "drivers": r.drivers,
                    "avg_flow": r.avg_flow,
                    "min_pressure_adjacent": r.min_pressure_adjacent,
                }
                for r in report.pipe_risks[:20]
            ],
            "hotspots_geojson": report.hotspots,
            "timestep_balance": [
                {"hour": b.hour, "inflow_m3h": b.inflow_m3h, "demand_m3h": b.demand_m3h, "nrw_m3h": b.nrw_m3h}
                for b in report.timestep_balance
            ],
        }
    except Exception as exc:
        logger.warning("[%s] MajiScope leakage snapshot enrichment failed: %s", scenario.id, exc)
        leakage_json = None

    nodes_geojson, pipes_geojson, heat_modes = _build_snapshot_heat_sources(
        output=output,
        leakage_json=leakage_json,
    )
    pressure_time_series = _build_pressure_time_series(output, scenario.time_step_min)
    if heat_modes:
        merged_summary["heat_map_modes"] = heat_modes
    if pressure_time_series:
        merged_summary["pressure_time_series"] = pressure_time_series

    detailed_results = _build_detailed_results(output, scenario.time_step_min)
    persisted_report = persisted_report_path(scenario.id)
    if os.path.isfile(persisted_report):
        with open(persisted_report, encoding="utf-8", errors="replace") as report_file:
            detailed_results["report_text"] = report_file.read()

    payload = {
        "launch_session_id": metadata.get("session_id"),
        "utility_id": metadata.get("utility_id"),
        "dma_id": metadata.get("dma_id"),
        "hydraulic_scenario_id": str(scenario.id),
        "scenario_name": scenario.name,
        "scenario_status": scenario.status,
        "input_parameters_json": _snapshot_input_parameters(scenario),
        "summary_json": _public_hydraulic_snapshot_value(merged_summary),
        "nrw_json": _public_hydraulic_snapshot_value(merged_summary.get("epanet_flow_balance")),
        "leakage_json": leakage_json,
        "detailed_results_json": _public_hydraulic_snapshot_value(detailed_results),
        "alerts_json": {
            "warnings": merged_summary.get("warnings", []),
        },
        "nodes_geojson": nodes_geojson,
        "pipes_geojson": pipes_geojson,
        "hotspots_geojson": leakage_json.get("hotspots_geojson") if isinstance(leakage_json, dict) else None,
        "completed_at": utc_isoformat(scenario.finished_at) if scenario.finished_at else None,
        "execution_duration_seconds": _execution_duration_seconds(scenario),
        "snapshot_version": 3,
        "result_quality": "complete" if merged_summary else "partial",
    }
    payload = _public_hydraulic_snapshot_value(payload)
    # Keep the raw report byte-for-byte readable; public snapshot key
    # normalization must not rewrite the report's original text.
    payload["detailed_results_json"]["report_text"] = detailed_results["report_text"]
    if not payload["utility_id"] or not payload["dma_id"]:
        logger.warning("[%s] MajiScope callback skipped: utility_id or dma_id missing in launch metadata", scenario.id)
        return

    scenario.majiscope_snapshot_payload = payload
    scenario.majiscope_snapshot_status = "PENDING"
    scenario.majiscope_snapshot_last_error = None
    await session.commit()
    await _deliver_majiscope_payload(scenario=scenario, session=session)


async def _post_majiscope_failed_snapshot(*, scenario: SimScenario, error_message: str, session) -> None:
    settings = get_settings()
    metadata = _majiscope_metadata_for_scenario(scenario)
    if not metadata:
        return
    payload = {
        "launch_session_id": metadata.get("session_id"),
        "utility_id": metadata.get("utility_id"),
        "dma_id": metadata.get("dma_id"),
        "hydraulic_scenario_id": str(scenario.id),
        "scenario_name": scenario.name,
        "scenario_status": "FAILED",
        "input_parameters_json": _snapshot_input_parameters(scenario),
        "alerts_json": {"warnings": [error_message]},
        "completed_at": utc_isoformat(scenario.finished_at) if scenario.finished_at else None,
        "execution_duration_seconds": _execution_duration_seconds(scenario),
        "snapshot_version": 3,
        "result_quality": "failed",
        "error_message": error_message,
    }
    payload = _public_hydraulic_snapshot_value(payload)
    if not payload["utility_id"] or not payload["dma_id"]:
        return
    scenario.majiscope_snapshot_payload = payload
    scenario.majiscope_snapshot_status = "PENDING"
    scenario.majiscope_snapshot_last_error = None
    await session.commit()
    await _deliver_majiscope_payload(scenario=scenario, session=session)


def _build_snapshot_heat_sources(*, output, leakage_json: Optional[dict]) -> tuple[dict, dict, list[str]]:
    node_index: Dict[str, dict] = {}
    for row in output.node_results:
        if row.lat is None or row.lon is None:
            continue
        entry = node_index.setdefault(
            row.element_id,
            {
                "lat": row.lat,
                "lon": row.lon,
                "pressures": [],
                "low_pressure_hits": 0,
                "total_steps": 0,
            },
        )
        if row.pressure is not None:
            entry["pressures"].append(float(row.pressure))
        entry["total_steps"] += 1
        if row.is_low_pressure:
            entry["low_pressure_hits"] += 1

    node_features = []
    low_pressure_available = False
    for element_id, entry in node_index.items():
        pressures = entry["pressures"]
        if not pressures:
            continue
        low_hits = int(entry["low_pressure_hits"])
        low_pressure_available = low_pressure_available or low_hits > 0
        total_steps = max(1, int(entry["total_steps"]))
        node_features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [entry["lon"], entry["lat"]],
            },
            "properties": {
                "element_id": element_id,
                "pressure": round(min(pressures), 3),
                "pressure_min": round(min(pressures), 3),
                "pressure_avg": round(sum(pressures) / len(pressures), 3),
                "pressure_max": round(max(pressures), 3),
                "low_pressure_hits": low_hits,
                "low_pressure_ratio": round(low_hits / total_steps, 4),
                "is_low_pressure": low_hits > 0,
            },
        })

    pipe_index: Dict[str, dict] = {}
    for row in output.pipe_results:
        if row.lat is None or row.lon is None:
            continue
        entry = pipe_index.setdefault(
            row.element_id,
            {
                "lat": row.lat,
                "lon": row.lon,
                "flows": [],
            },
        )
        if row.flow_rate is not None:
            entry["flows"].append(float(row.flow_rate))

    pipe_features = []
    for element_id, entry in pipe_index.items():
        flows = entry["flows"]
        if not flows:
            continue
        pipe_features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [entry["lon"], entry["lat"]],
            },
            "properties": {
                "element_id": element_id,
                "flow_rate": round(max(flows, key=lambda value: abs(value)), 6),
                "flow_rate_avg": round(sum(flows) / len(flows), 6),
                "flow_rate_max_abs": round(max(abs(value) for value in flows), 6),
            },
        })

    heat_modes: list[str] = []
    if node_features:
        heat_modes.append("pressure")
    if low_pressure_available:
        heat_modes.append("low_pressure")
    if pipe_features:
        heat_modes.append("pipe_flow")
    hotspot_features = ((leakage_json or {}).get("hotspots_geojson") or {}).get("features") or []
    if hotspot_features:
        heat_modes.append("leakage_risk")

    return (
        {"type": "FeatureCollection", "features": node_features},
        {"type": "FeatureCollection", "features": pipe_features},
        heat_modes,
    )


# ── coordinate helpers ────────────────────────────────────────────────────────

def _parse_inp_coordinates(inp_path: str) -> Dict[str, Tuple[float, float]]:
    """Extract {node_id: (lat, lon)} from [COORDINATES] section of the .inp."""
    coords: Dict[str, Tuple[float, float]] = {}
    if not inp_path or not os.path.isfile(inp_path):
        return coords
    in_section = False
    try:
        with open(inp_path, encoding="ascii", errors="replace") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped.startswith("["):
                    in_section = stripped.upper().startswith("[COORDINATES]")
                    continue
                if not in_section or not stripped or stripped.startswith(";"):
                    continue
                parts = stripped.split()
                if len(parts) >= 3:
                    try:
                        nid, x_lon, y_lat = parts[0], float(parts[1]), float(parts[2])
                        coords[nid] = (y_lat, x_lon)   # (lat, lon)
                    except ValueError:
                        pass
    except OSError:
        pass
    return coords


def _parse_inp_junction_ids(inp_path: str) -> set[str]:
    """Read junction IDs from the model so coordinate reports avoid reservoirs and tanks."""
    junction_ids: set[str] = set()
    in_section = False
    try:
        with open(inp_path, encoding="ascii", errors="replace") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped.startswith("["):
                    in_section = stripped.upper() == "[JUNCTIONS]"
                    continue
                if not in_section or not stripped or stripped.startswith(";"):
                    continue
                junction_ids.add(stripped.split()[0])
    except OSError:
        logger.exception("Could not read junction IDs from .inp file %s", inp_path)
    return junction_ids


def _nearest_node(lat: float, lon: float,
                  coords: Dict[str, Tuple[float, float]]) -> Optional[str]:
    if not coords:
        return None
    lat_r = math.radians(lat)
    best, best_d = None, 1e18
    for nid, (nlat, nlon) in coords.items():
        dx = (nlon - lon) * 111_320 * math.cos(lat_r)
        dy = (nlat - lat) * 111_320
        d  = math.hypot(dx, dy)
        if d < best_d:
            best_d, best = d, nid
    return best


def _build_leak_events(extra_demands, inp_path: str, duration_sec: int) -> list:
    """
    Map lat/lon extra_demands list → EPyT-Flow leak event dicts.
    DMA scenarios store metadata; resolved leak events are read from that dict.
    """
    if not extra_demands:
        return []
    if isinstance(extra_demands, dict):
        pre_resolved = extra_demands.get("_leak_events")
        if isinstance(pre_resolved, list):
            return [
                {
                    "node_id": event["node_id"],
                    "diameter": event["diameter"],
                    "start_time": event["start_time"],
                    "end_time": event["end_time"],
                }
                for event in pre_resolved
            ]
        return []
    if not isinstance(extra_demands, list):
        return []

    node_coords = _parse_inp_coordinates(inp_path)
    junction_ids = _parse_inp_junction_ids(inp_path)
    junction_coords = {
        node_id: coordinates
        for node_id, coordinates in node_coords.items()
        if node_id in junction_ids
    }
    if not junction_coords:
        logger.warning("No junction coordinates in .inp — leak events skipped")
        return []

    events = []
    for lr in extra_demands:
        nid = lr.get("node_id")
        if nid is None:
            lat = lr.get("lat")
            lon = lr.get("lon")
            if lat is None or lon is None or (lat == 0.0 and lon == 0.0):
                continue
            nid = _nearest_node(lat, lon, junction_coords)
        if nid:
            start_time = int(lr.get("start_time_s", 0))
            end_time = min(int(lr.get("end_time_s", duration_sec)), duration_sec)
            events.append({
                "node_id":    nid,
                "diameter":   lr.get("diameter_m",     0.01),
                "start_time": start_time,
                "end_time":   end_time,
            })
    return events


# ── main task ─────────────────────────────────────────────────────────────────

async def run_simulation_task(scenario_id: int) -> None:
    """
    Called by FastAPI BackgroundTasks after the DMA router queues a scenario.
    Runs entirely inside an async context, using the async DB session.
    """
    logger.info("[%d] Starting DMA simulation task", scenario_id)

    temporary_inp_dir = None
    async with get_session() as session:
        try:
            # ── mark RUNNING ──────────────────────────────────────────────────
            scenario = await session.get(SimScenario, scenario_id)
            if not scenario:
                logger.error("[%d] Scenario not found in DB", scenario_id)
                return
            scenario.status     = "RUNNING"
            scenario.started_at = datetime.now(timezone.utc)
            await session.commit()

            # ── resolve .inp path ─────────────────────────────────────────────
            extra = scenario.extra_demands or {}
            temporary_inp_dir = extra.get("_temporary_inp_dir") if isinstance(extra, dict) else None
            inp_path = extra.get("_dma_inp_path") if isinstance(extra, dict) else None
            if not inp_path and scenario.input_type == "inp":
                inp_path = os.path.join(
                    get_settings().inp_dir, scenario.input_filename or ""
                )

            if not inp_path or not os.path.isfile(inp_path):
                raise FileNotFoundError(
                    f"Pre-built .inp not found: {inp_path!r}. "
                    "Re-submit the simulation via POST /dma/{{file}}/simulate."
                )

            logger.info("[%d] DMA .inp → %s", scenario_id, inp_path)

            # ── field leak reports (AbruptLeakage events) ─────────────────────
            duration_sec = scenario.duration_hrs * 3_600
            raw_leaks = extra.get("_leak_reports", []) if isinstance(extra, dict) else []
            leak_input = (
                extra if isinstance(extra, dict) and "_leak_events" in extra
                else raw_leaks
            )
            leak_events = _build_leak_events(leak_input, inp_path, duration_sec)
            if scenario.scenario_type == "reported_leak":
                if not raw_leaks or len(leak_events) != len(raw_leaks):
                    raise ValueError(
                        "Reported-leak scenario could not resolve every report to "
                        "a network node with coordinates."
                    )
            if leak_events:
                logger.info("[%d] %d leak report(s) will be injected", scenario_id, len(leak_events))

            # ── run simulation ────────────────────────────────────────────────
            output = run_simulation(
                inp_path,
                duration_hrs=scenario.duration_hrs,
                time_step_min=scenario.time_step_min,
                leak_events=leak_events,
                demand_model=scenario.demand_model,
                pda_pressure_min=scenario.pda_pressure_min,
                pda_pressure_required=scenario.pda_pressure_required,
                pda_pressure_exponent=scenario.pda_pressure_exponent,
            )

            # ── persist results ───────────────────────────────────────────────
            rows: List[SimResult] = []

            for nr in output.node_results:
                rows.append(SimResult(
                    scenario_id      = scenario_id,
                    time_step        = nr.time_step,
                    element_type     = "node",
                    element_id       = nr.element_id,
                    lat              = nr.lat,
                    lon              = nr.lon,
                    pressure         = nr.pressure,
                    head             = nr.head,
                    demand           = nr.demand,
                    water_age        = nr.water_age,
                    is_low_pressure  = nr.is_low_pressure,
                ))

            for pr in output.pipe_results:
                rows.append(SimResult(
                    scenario_id      = scenario_id,
                    time_step        = pr.time_step,
                    element_type     = "pipe",
                    element_id       = pr.element_id,
                    lat              = pr.lat,
                    lon              = pr.lon,
                    flow_rate        = pr.flow_rate,
                    velocity         = pr.velocity,
                    headloss         = pr.headloss,
                    is_high_velocity = pr.is_high_velocity,
                ))

            for tank in output.tank_results:
                rows.append(SimResult(
                    scenario_id = scenario_id,
                    time_step = tank.time_step,
                    element_type = "tank",
                    element_id = tank.element_id,
                    lat = tank.lat,
                    lon = tank.lon,
                    volume_m3 = tank.volume_m3,
                ))

            session.add_all(rows)

            # ── enrich summary with .rpt flow balance ─────────────────────────
            merged_summary = dict(output.summary)
            merged_summary["scenario_type"] = scenario.scenario_type
            merged_summary["input_type"] = scenario.input_type
            merged_summary["input_filename"] = (
                extra.get("_input_original_filename") or scenario.input_filename
            )
            merged_summary["input_sha256"] = extra.get("_input_sha256")
            merged_summary["demand_model"] = scenario.demand_model
            merged_summary["time_step_min"] = scenario.time_step_min
            if extra.get("_model_overrides"):
                merged_summary["model_overrides"] = extra["_model_overrides"]
            rpt = parse_rpt(inp_path)
            if rpt:
                report_artifacts = _hydraulic_report_artifacts(rpt)
                merged_summary["epanet_flow_balance"] = rpt_nrw_summary(rpt)
                merged_summary["epanet_status_events"] = len(rpt.status_events)
                merged_summary["hydraulic_report_artifacts"] = report_artifacts
                if rpt.flow_balance:
                    merged_summary["inlet_flow_m3h"]   = rpt.flow_balance.total_inflow_m3h
                    merged_summary["total_demand_m3h"] = rpt.flow_balance.consumer_demand_m3h
                if rpt.warnings:
                    merged_summary.setdefault("warnings", []).extend(rpt.warnings)

            report_available = False
            rpt_source = inp_path + ".rpt"
            if os.path.isfile(rpt_source):
                try:
                    os.makedirs(get_settings().reports_dir, exist_ok=True)
                    shutil.copy2(rpt_source, persisted_report_path(scenario_id))
                    report_available = True
                except OSError:
                    logger.exception("[%d] Failed to persist EPANET report", scenario_id)
            merged_summary["report_available"] = report_available

            # ── mark DONE ─────────────────────────────────────────────────────
            scenario = await session.get(SimScenario, scenario_id)
            scenario.status      = "DONE"
            scenario.finished_at = datetime.now(timezone.utc)
            scenario.summary     = merged_summary
            await session.commit()

            await _post_majiscope_snapshot(
                scenario=scenario,
                output=output,
                merged_summary=merged_summary,
                session=session,
            )

            logger.info(
                "[%d] DONE — %d node results, %d pipe results",
                scenario_id, len(output.node_results), len(output.pipe_results),
            )

        except Exception as exc:
            logger.exception("[%d] FAILED: %s", scenario_id, exc)
            try:
                scenario = await session.get(SimScenario, scenario_id)
                if scenario:
                    scenario.status        = "FAILED"
                    scenario.error_message = str(exc)
                    scenario.finished_at   = datetime.now(timezone.utc)
                    await session.commit()
                    await _post_majiscope_failed_snapshot(scenario=scenario, error_message=str(exc), session=session)
            except Exception:
                pass
        finally:
            if (
                temporary_inp_dir
                and os.path.basename(temporary_inp_dir).startswith("dma_inp_editor_")
                and os.path.isdir(temporary_inp_dir)
            ):
                try:
                    shutil.rmtree(temporary_inp_dir)
                except OSError:
                    logger.exception(
                        "[%d] Failed to remove temporary EPANET edit directory %s",
                        scenario_id,
                        temporary_inp_dir,
                    )
