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
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from sqlalchemy import select

from app.core.database import get_session
from app.core.config import get_settings
from app.models.simulation import SimResult, SimScenario
from app.services.leakage_report import analyse_leakage
from app.services.rpt_parser import parse_rpt, rpt_nrw_summary
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
    if isinstance(value, str):
        return value.replace("EPANET", "HYDRAULIC").replace("Epanet", "Hydraulic").replace("epanet", "hydraulic")
    return value


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
        async with httpx.AsyncClient(timeout=30.0) as client:
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

    payload = {
        "launch_session_id": metadata.get("session_id"),
        "utility_id": metadata.get("utility_id"),
        "dma_id": metadata.get("dma_id"),
        "hydraulic_scenario_id": str(scenario.id),
        "scenario_name": scenario.name,
        "scenario_status": scenario.status,
        "input_parameters_json": {
            "gpkg_filename": scenario.gpkg_filename,
            "base_demand": scenario.base_demand,
            "duration_hrs": scenario.duration_hrs,
            "time_step_min": scenario.time_step_min,
            "leakage_frac": scenario.leakage_frac,
            "extra_demands": {key: value for key, value in (scenario.extra_demands or {}).items() if key != "_majiscope"},
        },
        "summary_json": _public_hydraulic_snapshot_value(merged_summary),
        "nrw_json": _public_hydraulic_snapshot_value(merged_summary.get("epanet_flow_balance")),
        "leakage_json": leakage_json,
        "alerts_json": {
            "warnings": merged_summary.get("warnings", []),
        },
        "hotspots_geojson": leakage_json.get("hotspots_geojson") if isinstance(leakage_json, dict) else None,
        "completed_at": scenario.finished_at.isoformat() if scenario.finished_at else None,
        "execution_duration_seconds": _execution_duration_seconds(scenario),
        "snapshot_version": 1,
        "result_quality": "complete" if merged_summary else "partial",
    }
    payload = _public_hydraulic_snapshot_value(payload)
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
        "input_parameters_json": {
            "gpkg_filename": scenario.gpkg_filename,
            "base_demand": scenario.base_demand,
            "duration_hrs": scenario.duration_hrs,
            "time_step_min": scenario.time_step_min,
            "leakage_frac": scenario.leakage_frac,
            "extra_demands": {key: value for key, value in (scenario.extra_demands or {}).items() if key != "_majiscope"},
        },
        "alerts_json": {"warnings": [error_message]},
        "completed_at": scenario.finished_at.isoformat() if scenario.finished_at else None,
        "execution_duration_seconds": _execution_duration_seconds(scenario),
        "snapshot_version": 1,
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
    DMA scenarios store a metadata dict (not a list) — detected and skipped.
    """
    if not extra_demands:
        return []
    if isinstance(extra_demands, dict):
        return []       # DMA metadata dict, not leak events
    if not isinstance(extra_demands, list):
        return []

    node_coords = _parse_inp_coordinates(inp_path)
    if not node_coords:
        logger.warning("No coordinates in .inp — leak events skipped")
        return []

    events = []
    for lr in extra_demands:
        lat = lr.get("lat", 0.0)
        lon = lr.get("lon", 0.0)
        if lat == 0.0 and lon == 0.0:
            continue
        nid = _nearest_node(lat, lon, node_coords)
        if nid:
            events.append({
                "node_id":    nid,
                "diameter":   lr.get("diameter_m",     0.01),
                "start_time": lr.get("start_time_s",   0),
                "end_time":   lr.get("end_time_s",     duration_sec),
            })
    return events


# ── main task ─────────────────────────────────────────────────────────────────

async def run_simulation_task(scenario_id: int) -> None:
    """
    Called by FastAPI BackgroundTasks after the DMA router queues a scenario.
    Runs entirely inside an async context, using the async DB session.
    """
    logger.info("[%d] Starting DMA simulation task", scenario_id)

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
            inp_path = extra.get("_dma_inp_path") if isinstance(extra, dict) else None

            if not inp_path or not os.path.isfile(inp_path):
                raise FileNotFoundError(
                    f"Pre-built .inp not found: {inp_path!r}. "
                    "Re-submit the simulation via POST /dma/{{file}}/simulate."
                )

            logger.info("[%d] DMA .inp → %s", scenario_id, inp_path)

            # ── field leak reports (AbruptLeakage events) ─────────────────────
            duration_sec = scenario.duration_hrs * 3_600
            raw_leaks    = extra.get("_leak_reports", []) if isinstance(extra, dict) else []
            leak_events  = _build_leak_events(raw_leaks, inp_path, duration_sec)
            if leak_events:
                logger.info("[%d] %d leak report(s) will be injected", scenario_id, len(leak_events))

            # ── run simulation ────────────────────────────────────────────────
            output = run_simulation(inp_path, leak_events=leak_events)

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

            session.add_all(rows)

            # ── enrich summary with .rpt flow balance ─────────────────────────
            merged_summary = dict(output.summary)
            rpt = parse_rpt(inp_path)
            if rpt:
                merged_summary["epanet_flow_balance"] = rpt_nrw_summary(rpt)
                merged_summary["epanet_status_events"] = len(rpt.status_events)
                if rpt.flow_balance:
                    merged_summary["inlet_flow_m3h"]   = rpt.flow_balance.total_inflow_m3h
                    merged_summary["total_demand_m3h"] = rpt.flow_balance.consumer_demand_m3h
                if not rpt.balanced:
                    merged_summary.setdefault("warnings", []).extend(rpt.warnings)

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
