# app/workers/simulation_worker.py
"""
Background task — EPyT-Flow v0.17.x edition.

This service is a hydraulic simulation *engine*: it does not decide on
its own where leaks are. Every scenario carries a `scenario_type`
(see app/core/scenario_types.py) that determines where its leak events,
if any, come from:

    baseline / planned_shutdown / fire_flow   No leak events, ever.
    reported_leak   Leak events come only from `reported_leaks`,
                     resolved and validated against the network by the
                     router *before* the scenario was queued (see
                     routers/dma.py, routers/inp.py) and handed to this
                     worker pre-resolved via extra_demands["_leak_events"].
    research         The only scenario_type allowed to fall back to the
                     legacy random-per-node `leakage_frac` generator.

Pipeline
--------
1. Mark scenario RUNNING.
2. Look up the .inp already built for this scenario by the DMA ingest
   pipeline (dma_builder.build_dma_inp, invoked from routers/dma.py or
   routers/inp.py), via extra_demands["_dma_inp_path"]. There is no
   longer any direct .gpkg → .inp / subset-selection builder here —
   that used to live in network_builder.py / network_subset.py, which
   have been removed. A scenario without a pre-built .inp path fails
   fast with a clear error.
3. Resolve leak events per the scenario_type contract above.
4. Run EPyT-Flow simulation.
5. Persist results in batches. For scenario_type="reported_leak", also
   compute a service-impact summary and topological isolation
   recommendations per leak.
6. Mark DONE or FAILED; clean up temp .inp file.
"""

import logging
import math
import os
import shutil
from datetime import datetime

from app.core.config import get_settings
from app.core.database import get_session
from app.core.scenario_types import ALL_SCENARIO_TYPES, RANDOM_LEAKS_ALLOWED_FOR, REPORTED_LEAK
from app.core.tz import utc_isoformat
from app.models.simulation import SimResult, SimScenario
from app.services.leak_report import compute_service_impact, recommend_isolation
from app.services.rpt_parser import parse_rpt, rpt_nrw_summary
from app.services.simulation_service import run_simulation

logger = logging.getLogger(__name__)
BATCH_SIZE = 500


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

def _parse_inp_coordinates(inp_path: str) -> dict:
    """Parse [COORDINATES] from .inp → {node_id: (lon, lat)}."""
    coords, in_sec = {}, False
    with open(inp_path) as f:
        for line in f:
            s = line.strip()
            if s.upper().startswith("[COORDINATES]"):
                in_sec = True
                continue
            if s.startswith("[") and in_sec:
                break
            if not in_sec or s.startswith(";") or not s:
                continue
            parts = s.split()
            if len(parts) >= 3:
                coords[parts[0]] = (float(parts[1]), float(parts[2]))
    return coords


def _parse_inp_pipe_diameters(inp_path: str) -> dict:
    """
    Parse [PIPES] from .inp → {pipe_id: diam_mm}.

    Used so the leakage-risk report can compute real per-pipe velocity
    instead of assuming a fixed diameter for every pipe. Must be captured
    here, at build/run time, because the .inp is deleted once the scenario
    finishes (see the `finally` block below) and the leakage report is
    generated later, on demand, from persisted DB rows alone.
    """
    diam_mm, in_sec = {}, False
    with open(inp_path) as f:
        for line in f:
            s = line.strip()
            if s.upper().startswith("[PIPES]"):
                in_sec = True
                continue
            if s.startswith("[") and in_sec:
                break
            if not in_sec or s.startswith(";") or not s:
                continue
            parts = s.split()
            # ;ID  Node1  Node2  Length(m)  Diam(mm)  C(H-W)  Minor  Status
            if len(parts) >= 5:
                try:
                    diam_mm[parts[0]] = float(parts[4])
                except ValueError:
                    continue
    return diam_mm


def _nearest_node(lat: float, lon: float, coords: dict) -> str:
    best, best_d = None, float("inf")
    for nid, (nx, ny) in coords.items():
        d = math.hypot(nx - lon, ny - lat)
        if d < best_d:
            best_d, best = d, nid
    return best


def _build_leak_events(
    extra_demands, inp_path: str, duration_sec: int
) -> list:
    """
    Map lat/lon extra_demands → EPyT-Flow leak event dicts.
    Keys returned: node_id, diameter, start_time, end_time.

    DMA/inp scenarios store a metadata dict in extra_demands (keys start
    with "_") rather than a bare list of [{lat, lon, ...}] leak events.
    One exception: a pre-resolved `_leak_events` list nested inside that
    dict — set by routers/dma.py or routers/inp.py for scenario_type=
    "reported_leak", after validating the report(s) against the network
    via leak_report.resolve_and_validate_reports(). Those are already in
    the {node_id, diameter, start_time, end_time, ...} shape the worker
    needs, so they're returned as-is rather than re-resolved from lat/lon.
    """
    if not extra_demands:
        return []

    if isinstance(extra_demands, dict):
        pre_resolved = extra_demands.get("_leak_events")
        if isinstance(pre_resolved, list) and pre_resolved:
            return [
                {
                    "node_id":    e["node_id"],
                    "diameter":   e.get("diameter", 0.01),
                    "start_time": e.get("start_time", 0),
                    "end_time":   e.get("end_time", duration_sec),
                }
                for e in pre_resolved if "node_id" in e
            ]
        return []
    if not isinstance(extra_demands, list):
        return []

    node_coords = _parse_inp_coordinates(inp_path)
    if not node_coords:
        logger.warning("No coordinates parsed from .inp — leak events skipped")
        return []

    events = []
    for ed in extra_demands:
        lat = ed.get("lat", 0.0)
        lon = ed.get("lon", 0.0)
        # skip dummy 0,0 coordinates (happens when leak_events are not geo-tagged)
        if lat == 0.0 and lon == 0.0:
            logger.warning("Skipping leak event with (0,0) coordinates")
            continue
        nid = _nearest_node(lat, lon, node_coords)
        if nid:
            events.append({
                "node_id":    nid,
                "diameter":   ed.get("leak_diameter_m", 0.01),
                "start_time": ed.get("start_time_s",   0),
                "end_time":   ed.get("end_time_s",     duration_sec),
            })
            logger.info(
                "Leak event: (%.5f, %.5f) → node %s  Ø=%.3f m",
                lat, lon, nid, ed.get("leak_diameter_m", 0.01),
            )
    return events


async def run_simulation_task(scenario_id: int) -> None:
    inp_path = None

    async with get_session() as session:
        scenario = await session.get(SimScenario, scenario_id)
        if not scenario:
            logger.error("Scenario %d not found.", scenario_id)
            return
        scenario.status     = "RUNNING"
        scenario.started_at = datetime.utcnow()
        await session.commit()
    logger.info("[%d] Status → RUNNING", scenario_id)

    try:
        duration_sec = scenario.duration_hrs * 3600

        # step 1: locate the .inp for this scenario
        #
        # The only supported source is a .inp pre-built by the DMA ingest
        # pipeline (dma_builder.build_dma_inp), whose path is stashed in
        # extra_demands["_dma_inp_path"] by routers/dma.py or routers/inp.py.
        # Building an .inp directly from a raw .gpkg (whole-network or a
        # pipe_ids subset) is no longer supported — network_builder.py and
        # network_subset.py, which used to do that, have been removed.
        extra = scenario.extra_demands or {}
        dma_inp_path = extra.get("_dma_inp_path") if isinstance(extra, dict) else None

        if not (dma_inp_path and os.path.isfile(dma_inp_path)):
            raise RuntimeError(
                "No pre-built .inp found for this scenario "
                "(extra_demands['_dma_inp_path'] missing or file not found). "
                "Direct .gpkg-to-.inp building (including pipe_ids subsets) is "
                "no longer supported — build the scenario via the DMA ingest "
                "pipeline first."
            )

        inp_path = dma_inp_path
        logger.info("[%d] Using pre-built .inp: %s", scenario_id, inp_path)

        logger.info(
            "[%d] scenario_type=%s leakage_frac=%s demand_model=%s",
            scenario_id,
            scenario.scenario_type,
            scenario.leakage_frac,
            scenario.demand_model,
        )

        if scenario.scenario_type not in ALL_SCENARIO_TYPES:
            raise RuntimeError(
                f"Unknown scenario_type '{scenario.scenario_type}' — must be one of "
                f"{sorted(ALL_SCENARIO_TYPES)}."
            )

        # step 2: resolve leak events
        leak_events = _build_leak_events(
            scenario.extra_demands or [], inp_path, duration_sec
        )

        if scenario.scenario_type == REPORTED_LEAK:
            # The router (routers/dma.py / routers/inp.py) must already have
            # resolved and validated the report(s) against this network and
            # stashed them in extra_demands["_leak_events"] before queuing.
            # If none made it through, this scenario was queued incorrectly
            # — fail loudly rather than silently running a "reported_leak"
            # scenario with no leak in it.
            if not leak_events:
                raise RuntimeError(
                    "scenario_type='reported_leak' but no resolved leak events "
                    "were found in extra_demands['_leak_events']. The report(s) "
                    "must be validated against the network at request time."
                )
        elif not leak_events and scenario.leakage_frac > 0:
            # Random/synthetic per-node leak generation — a research and
            # testing convenience only. Never a silent default for a
            # production run driven by the main water-management system.
            if scenario.scenario_type not in RANDOM_LEAKS_ALLOWED_FOR:
                raise RuntimeError(
                    f"leakage_frac={scenario.leakage_frac} is set but scenario_type="
                    f"'{scenario.scenario_type}' — random/synthetic leak generation "
                    "is only permitted for scenario_type='research'. This should "
                    "have been rejected when the scenario was created; failing the "
                    "run instead of silently injecting leaks."
                )

            coords = _parse_inp_coordinates(inp_path)

            all_nodes = list(coords.keys())

            leak_count = max(
                1,
                int(len(all_nodes) * scenario.leakage_frac)
            )

            import random

            selected_nodes = random.sample(
                all_nodes,
                min(leak_count, len(all_nodes))
            )

            leak_events = [
                {
                    "node_id": node_id,
                    "diameter": 0.01,
                    "start_time": 0,
                    "end_time": duration_sec,
                }
                for node_id in selected_nodes
            ]

            logger.info(
                "[%d] Generated %d research-mode synthetic leak events (fraction=%.2f)",
                scenario_id,
                len(leak_events),
                scenario.leakage_frac,
            )

        # step 3: EPyT-Flow
        # Advanced scenarios (routers/dma.py `/simulate/advanced`) store their
        # event configuration in extra_demands["_events"] together with the
        # uncertainty / noise blocks. They are turned into real EPyT-Flow
        # event objects here so the advanced controls actually take effect.
        leakage_objects = []
        sensor_faults = []
        actuator_events = []
        model_uncertainty = None
        sensor_noise = None

        events_cfg = extra.get("_events") if isinstance(extra, dict) else None
        if events_cfg:
            try:
                from app.services.epyt_events import build_events

                built = build_events(events_cfg)
                leakage_objects = built.get("leakage_events", []) or []
                sensor_faults = built.get("sensor_faults", []) or []
                actuator_events = built.get("actuator_events", []) or []
            except Exception as exc:
                logger.warning(
                    "[%d] Could not build advanced EPyT-Flow events: %s", scenario_id, exc
                )

        raw_uncertainty = extra.get("_model_uncertainty") if isinstance(extra, dict) else None
        if raw_uncertainty:
            try:
                from app.services.epyt_events import build_model_uncertainty

                model_uncertainty = build_model_uncertainty(raw_uncertainty)
            except Exception as exc:
                logger.warning(
                    "[%d] Could not build model uncertainty: %s", scenario_id, exc
                )

        raw_noise = extra.get("_sensor_noise") if isinstance(extra, dict) else None
        if raw_noise:
            try:
                from app.services.epyt_events import build_sensor_noise

                sensor_noise = build_sensor_noise(raw_noise)
            except Exception as exc:
                logger.warning("[%d] Could not build sensor noise: %s", scenario_id, exc)

        output = run_simulation(
            inp_path      = inp_path,
            duration_hrs  = scenario.duration_hrs,
            time_step_min = scenario.time_step_min,
            leak_events   = leak_events,
            leakage_objects  = leakage_objects,
            sensor_faults    = sensor_faults,
            actuator_events  = actuator_events,
            model_uncertainty = model_uncertainty,
            sensor_noise      = sensor_noise,
            demand_model          = scenario.demand_model,
            pda_pressure_min      = scenario.pda_pressure_min,
            pda_pressure_required = scenario.pda_pressure_required,
            pda_pressure_exponent = scenario.pda_pressure_exponent,
        )

        # step 4: persist
        total = len(output.node_results) + len(output.pipe_results)
        logger.info("[%d] Persisting %d records …", scenario_id, total)

        records = []
        for n in output.node_results:
            records.append(SimResult(
                scenario_id     = scenario_id,
                time_step       = n.time_step,
                element_type    = "node",
                element_id      = n.element_id,
                lat             = n.lat,
                lon             = n.lon,
                pressure        = n.pressure,
                head            = n.head,
                demand          = n.demand,
                water_age       = n.water_age,
                is_low_pressure = n.is_low_pressure,
            ))
        for p in output.pipe_results:
            records.append(SimResult(
                scenario_id      = scenario_id,
                time_step        = p.time_step,
                element_type     = "pipe",
                element_id       = p.element_id,
                lat              = p.lat,
                lon              = p.lon,
                flow_rate        = p.flow_rate,
                velocity         = p.velocity,
                headloss         = p.headloss,
                is_high_velocity = p.is_high_velocity,
            ))

        # tank results — stored as "tank" element_type (no schema change needed)
        for t in output.tank_results:
            records.append(SimResult(
                scenario_id  = scenario_id,
                time_step    = t.time_step,
                element_type = "tank",
                element_id   = t.element_id,
                lat          = t.lat,
                lon          = t.lon,
                volume_m3    = t.volume_m3,
            ))

        total = len(records)

        async with get_session() as session:
            for i in range(0, len(records), BATCH_SIZE):
                session.add_all(records[i : i + BATCH_SIZE])
                await session.flush()
            scenario = await session.get(SimScenario, scenario_id)
            scenario.status      = "DONE"
            scenario.finished_at = datetime.utcnow()

            # Merge EPANET's own .rpt flow balance into the summary
            # so the NRW endpoint gets real figures, not estimates.
            merged_summary = dict(output.summary)

            # Real per-pipe diameters (mm) — captured now because the .inp
            # (and with it the [PIPES] section) is deleted in `finally` below,
            # but the leakage report is generated later, on demand.
            try:
                merged_summary["pipe_diam_mm"] = _parse_inp_pipe_diameters(inp_path)
            except OSError as diam_exc:
                logger.warning("[%d] Could not parse pipe diameters: %s", scenario_id, diam_exc)
                merged_summary["pipe_diam_mm"] = {}

            rpt = parse_rpt(inp_path)
            if rpt:
                merged_summary["epanet_flow_balance"] = rpt_nrw_summary(rpt)
                merged_summary["epanet_status_events"] = len(rpt.status_events)
                if rpt.flow_balance:
                    merged_summary["inlet_flow_m3h"]    = rpt.flow_balance.total_inflow_m3h
                    merged_summary["total_demand_m3h"]  = rpt.flow_balance.consumer_demand_m3h
                if not rpt.balanced:
                    merged_summary.setdefault("warnings", []).extend(rpt.warnings)

            # Reported-leak analysis: service impact (pressure-drop footprint)
            # and topological isolation candidates per leak. Computed now,
            # while inp_path (needed for pipe topology) still exists — it's
            # deleted in the `finally` block below.
            if scenario.scenario_type == REPORTED_LEAK:
                resolved_leaks = (scenario.extra_demands or {}).get("_leak_events") or []
                try:
                    isolation = [
                        recommend_isolation(rl, inp_path) for rl in resolved_leaks
                    ]
                except OSError as iso_exc:
                    logger.warning(
                        "[%d] Could not compute isolation recommendations: %s",
                        scenario_id, iso_exc,
                    )
                    isolation = []

                merged_summary["reported_leak_analysis"] = {
                    "service_impact": compute_service_impact(output.node_results),
                    "isolation_recommendations": isolation,
                }

            # Persist the raw .rpt file itself (not just the parsed summary)
            # so the user can view/download the full EPANET report after the
            # temp .inp/.rpt working directory is cleaned up below.
            rpt_src = inp_path + ".rpt"
            report_available = False
            if os.path.isfile(rpt_src):
                settings = get_settings()
                try:
                    os.makedirs(settings.reports_dir, exist_ok=True)
                    from app.services.rpt_parser import persisted_report_path
                    shutil.copy2(rpt_src, persisted_report_path(scenario_id))
                    report_available = True
                except OSError as copy_exc:
                    logger.warning(
                        "[%d] Could not persist .rpt file: %s", scenario_id, copy_exc
                    )
            merged_summary["report_available"] = report_available

            scenario.summary = merged_summary
            await session.commit()

            # Hand the finished run back to MajiScope when this scenario was
            # started from a MajiScope launch session.
            await _post_majiscope_snapshot(
                scenario=scenario,
                output=output,
                merged_summary=merged_summary,
                session=session,
            )

        logger.info("[%d] Status → DONE  (%d records)", scenario_id, total)

    except Exception as exc:
        logger.exception("[%d] FAILED: %s", scenario_id, exc)
        async with get_session() as session:
            scenario = await session.get(SimScenario, scenario_id)
            if scenario:
                scenario.status        = "FAILED"
                scenario.error_message = str(exc)
                scenario.finished_at   = datetime.utcnow()
                await session.commit()
                await _post_majiscope_failed_snapshot(
                    scenario=scenario, error_message=str(exc), session=session
                )
    finally:
        if inp_path and os.path.isfile(inp_path):
            try:
                os.remove(inp_path)
                os.rmdir(os.path.dirname(inp_path))
            except OSError:
                pass