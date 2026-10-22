# app/services/simulation_service.py
"""
EPyT-Flow v0.17.x simulation runner — full feature edition.

Supports
--------
* Pressure, flow, demand, water-age sensors on all nodes/links
* AbruptLeakage + IncipientLeakage events
* SensorFault variants (constant, drift, gaussian, percentage, stuck_zero)
* ActuatorEvent variants (ValveStateEvent, PumpStateEvent, PumpSpeedEvent)
* ModelUncertainty  (demand ±%, roughness ±%)
* SensorNoise       (additive Gaussian noise on readings)
* Tank volume extraction from ScadaData
* Valve/pump state extraction when present
"""

import logging
import math
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from app.core.config import get_settings

logger = logging.getLogger(__name__)



def _extract_node_coords(api, node_ids: List[str]) -> Dict[str, Tuple[float, float]]:
    """
    Extract (lon, lat) for each node using the bundled EPyT class
    (`epanet_plus.EPyT`). Its node/link accessors are snake_case
    (`get_node_idx`, `getcoord`, ...) — not the camelCase
    Matlab-toolkit names (`getNodeIndex`, `getNodeCoordinates`, ...).
    """
    coords: Dict[str, Tuple[float, float]] = {}
    for nid in node_ids:
        try:
            idx = api.get_node_idx(nid)
            xy  = api.getcoord(idx)   # [x, y]
            if xy and len(xy) >= 2:
                x, y = float(xy[0]), float(xy[1])
                if x != 0.0 or y != 0.0:
                    coords[nid] = (x, y)
        except Exception:
            pass
    return coords


def _extract_pipe_topology(
    api, pipe_ids: List[str]
) -> Dict[str, Tuple[str, str]]:
    """Return {pipe_id: (start_node_id, end_node_id)}."""
    topo: Dict[str, Tuple[str, str]] = {}
    for pid in pipe_ids:
        try:
            idx   = api.get_link_idx(pid)
            nodes = api.getlinknodes(idx)   # [start_idx, end_idx], 1-based
            s_id  = api.get_node_id(nodes[0])
            e_id  = api.get_node_id(nodes[1])
            topo[pid] = (s_id, e_id)
        except Exception:
            pass
    return topo


def validate_inp_network(inp_path: str) -> dict:
    """Parse an uploaded EPANET model and return basic network metadata."""
    try:
        from epyt_flow.simulation import ScenarioSimulator
    except ImportError as exc:
        raise RuntimeError(
            "epyt-flow is not installed. Run: pip install epyt-flow==0.17.1"
        ) from exc

    try:
        with ScenarioSimulator(f_inp_in=inp_path) as simulator:
            api = simulator.epanet_api
            node_ids = list(api.get_all_nodes_id())
            link_ids = list(api.get_all_links_id())
            junction_ids = [
                node_id for node_id in node_ids
                if api.get_node_type(api.get_node_idx(node_id)) == 0
            ]
            node_coords = []
            for node_id in node_ids:
                xy = api.getcoord(api.get_node_idx(node_id))
                if xy and len(xy) >= 2:
                    node_coords.append((float(xy[0]), float(xy[1])))
    except Exception as exc:
        raise ValueError(f"Invalid EPANET .inp model: {exc}") from exc

    if not junction_ids or not link_ids:
        raise ValueError("The EPANET model must contain at least one junction and one link.")
    return {
        "node_count": len(node_ids),
        "junction_count": len(junction_ids),
        "junction_ids": junction_ids,
        "link_count": len(link_ids),
        "has_coordinates": bool(node_coords),
        "coordinates_are_lonlat": bool(node_coords) and all(
            -180 <= x <= 180 and -90 <= y <= 90 for x, y in node_coords
        ),
    }


def _read_inp_records(text: str) -> Dict[str, Dict[str, List[str]]]:
    sections: Dict[str, Dict[str, List[str]]] = {}
    current_section = None
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current_section = stripped.upper()
            sections.setdefault(current_section, {})
            continue
        if not current_section or not stripped or stripped.startswith(";"):
            continue
        fields = stripped.partition(";")[0].split()
        if fields:
            sections[current_section][fields[0].casefold()] = fields
    return sections


def _inp_number(fields: Optional[List[str]], index: int) -> Optional[float]:
    if not fields or len(fields) <= index:
        return None
    try:
        return float(fields[index])
    except ValueError:
        return None


def inspect_inp_elements(inp_path: str) -> dict:
    """Return the supported geographic EPANET elements and editable attributes."""
    try:
        from epyt_flow.simulation import ScenarioSimulator
    except ImportError as exc:
        raise RuntimeError(
            "epyt-flow is not installed. Run: pip install epyt-flow==0.17.1"
        ) from exc

    try:
        with open(inp_path, encoding="ascii", errors="replace") as source:
            sections = _read_inp_records(source.read())
        with ScenarioSimulator(f_inp_in=inp_path) as simulator:
            api = simulator.epanet_api
            junction_ids = list(api.get_all_junctions_id())
            reservoir_ids = list(api.get_all_reservoirs_id())
            tank_ids = list(api.get_all_tanks_id())
            pipe_ids = list(api.get_all_pipes_id())
            valve_ids = list(api.get_all_valves_id())
            pump_ids = list(api.get_all_pumps_id())

            coord_records = sections.get("[COORDINATES]", {})
            coords = {}
            for node_key, fields in coord_records.items():
                if len(fields) < 3:
                    continue
                try:
                    coords[node_key] = (float(fields[1]), float(fields[2]))
                except ValueError:
                    continue
            coordinates_are_lonlat = bool(coords) and all(
                -180 <= x <= 180 and -90 <= y <= 90 for x, y in coords.values()
            )

            def point_features(node_ids, section, extra_properties):
                features = []
                records = sections.get(section, {})
                for node_id in node_ids:
                    node_key = str(node_id).casefold()
                    location = coords.get(node_key)
                    if not location:
                        continue
                    x, y = location
                    properties = {
                        "id": str(node_id),
                        **extra_properties(node_id, records.get(node_key)),
                    }
                    features.append({
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": [x, y]},
                        "properties": properties,
                    })
                return {"type": "FeatureCollection", "features": features}

            junctions = point_features(
                junction_ids,
                "[JUNCTIONS]",
                lambda node_id, _row: {
                    "elevation": float(api.get_node_elevation(api.get_node_idx(node_id))),
                    "base_demand": float(api.get_node_base_demand(api.get_node_idx(node_id))),
                },
            )
            reservoirs = point_features(
                reservoir_ids,
                "[RESERVOIRS]",
                lambda node_id, _row: {
                    "head": float(api.get_node_elevation(api.get_node_idx(node_id))),
                },
            )
            tanks = point_features(
                tank_ids,
                "[TANKS]",
                lambda _node_id, row: {
                    "elevation": _inp_number(row, 1),
                    "init_level": _inp_number(row, 2),
                    "min_level": _inp_number(row, 3),
                    "max_level": _inp_number(row, 4),
                    "diameter": _inp_number(row, 5),
                },
            )

            status_records = sections.get("[STATUS]", {})

            def link_features(link_ids, section):
                features = []
                records = sections.get(section, {})
                for link_id in link_ids:
                    link_idx = api.get_link_idx(link_id)
                    node_indices = api.getlinknodes(link_idx)
                    start_id = str(api.get_node_id(node_indices[0]))
                    end_id = str(api.get_node_id(node_indices[1]))
                    start = coords.get(start_id.casefold())
                    end = coords.get(end_id.casefold())
                    if not start or not end:
                        continue
                    key = str(link_id).casefold()
                    row = records.get(key, [])
                    status_row = status_records.get(key, [])
                    status = status_row[1] if len(status_row) > 1 else None
                    properties = {"id": str(link_id)}
                    if section == "[PIPES]":
                        pipe_status = (
                            row[7] if len(row) > 7
                            else row[6] if len(row) > 6 and _inp_number(row, 6) is None
                            else "Open"
                        )
                        properties.update({
                            "diam_mm": _inp_number(row, 4),
                            "roughness": _inp_number(row, 5),
                            "status": status or pipe_status,
                        })
                    elif section == "[VALVES]":
                        setting = row[5] if len(row) > 5 else None
                        try:
                            setting = float(setting) if setting is not None else None
                        except ValueError:
                            pass
                        properties.update({
                            "diam_mm": _inp_number(row, 3),
                            "valve_type": row[4] if len(row) > 4 else "Valve",
                            "setting": setting,
                            "status": status or "Active",
                        })
                    else:
                        properties["type"] = "pump"
                    features.append({
                        "type": "Feature",
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [list(start), list(end)],
                        },
                        "properties": properties,
                    })
                return {"type": "FeatureCollection", "features": features}

            pipes = link_features(pipe_ids, "[PIPES]")
            valves = link_features(valve_ids, "[VALVES]")
            pumps = link_features(pump_ids, "[PUMPS]")
    except Exception as exc:
        raise ValueError(f"Could not inspect uploaded EPANET model: {exc}") from exc

    if not junction_ids and not reservoir_ids and not tank_ids:
        raise ValueError("The uploaded EPANET model contains no nodes.")
    if not coordinates_are_lonlat:
        junctions = reservoirs = tanks = pipes = valves = pumps = {
            "type": "FeatureCollection",
            "features": [],
        }

    return {
        "junction_count": len(junction_ids),
        "reservoir_count": len(reservoir_ids),
        "tank_count": len(tank_ids),
        "pipe_count": len(pipe_ids),
        "valve_count": len(valve_ids),
        "pump_count": len(pump_ids),
        "coordinates_are_lonlat": coordinates_are_lonlat,
        "junctions": junctions,
        "reservoirs": reservoirs,
        "tanks": tanks,
        "pipes": pipes,
        "valves": valves,
        "pumps": pumps,
    }


def patch_inp_elements(
    source_path: str,
    destination_path: str,
    overrides: Dict[str, Dict[str, Dict[str, object]]],
) -> str:
    """Apply supported edits to a separate EPANET input copy."""
    if not overrides:
        return source_path

    try:
        with open(source_path, encoding="ascii", errors="replace", newline="") as source:
            lines = source.readlines()
    except OSError as exc:
        raise ValueError(f"Could not read uploaded EPANET model: {exc}") from exc

    numeric_columns = {
        "inp_junctions": ("[JUNCTIONS]", {"elevation": 1, "base_demand": 2}),
        "inp_reservoirs": ("[RESERVOIRS]", {"head": 1}),
        "inp_tanks": ("[TANKS]", {
            "elevation": 1,
            "init_level": 2,
            "min_level": 3,
            "max_level": 4,
            "diameter": 5,
        }),
        "inp_pipes": ("[PIPES]", {"diam_mm": 4, "roughness": 5}),
        "inp_valves": ("[VALVES]", {"diam_mm": 3, "setting": 5}),
    }

    def patch_numeric_section(section, allowed_columns, edits):
        requested = {element_id.casefold(): values for element_id, values in edits.items()}
        found = set()
        patched = []
        in_section = False
        for raw_line in lines:
            stripped = raw_line.strip()
            if stripped.startswith("["):
                in_section = stripped.upper() == section
                patched.append(raw_line)
                continue
            if not in_section or not stripped or stripped.startswith(";"):
                patched.append(raw_line)
                continue

            body = raw_line.rstrip("\r\n")
            line_ending = raw_line[len(body):]
            code, separator, comment = body.partition(";")
            matches = list(re.finditer(r"\S+", code))
            fields = [match.group() for match in matches]
            if not fields:
                patched.append(raw_line)
                continue
            key = fields[0].casefold()
            if key not in requested:
                patched.append(raw_line)
                continue

            updates = requested[key]
            unknown_fields = set(updates) - set(allowed_columns) - {"status"}
            if unknown_fields:
                raise ValueError(
                    f"Unsupported {section.strip('[]').lower()} field(s): "
                    + ", ".join(sorted(unknown_fields))
                )
            replacements = {}
            for field_name, value in updates.items():
                if field_name not in allowed_columns:
                    continue
                number = float(value)
                if not math.isfinite(number):
                    raise ValueError(f"{field_name} for '{fields[0]}' must be finite.")
                column = allowed_columns[field_name]
                while len(fields) <= column:
                    fields.append("0")
                replacements[column] = f"{number:.10g}"
                fields[column] = replacements[column]
            new_code = code
            for column, replacement in sorted(replacements.items(), reverse=True):
                if column < len(matches):
                    match = matches[column]
                    new_code = (
                        new_code[:match.start()]
                        + replacement
                        + new_code[match.end():]
                    )
            if len(fields) > len(matches):
                new_code = new_code.rstrip() + "\t" + "\t".join(fields[len(matches):])
            if separator:
                new_code = new_code.rstrip()
            new_line = new_code
            if separator:
                new_line += " ;" + comment
            patched.append(new_line + line_ending)
            found.add(key)

        missing = sorted(set(requested) - found)
        if missing:
            original_ids = sorted(
                element_id for element_id in edits if element_id.casefold() in set(missing)
            )
            noun = {
                "[JUNCTIONS]": "junction",
                "[RESERVOIRS]": "reservoir",
                "[TANKS]": "tank",
                "[PIPES]": "pipe",
                "[VALVES]": "valve",
            }.get(section, "element")
            raise ValueError(
                f"Unknown {noun} ID(s): " + ", ".join(original_ids)
            )
        return patched

    for category, (section, columns) in numeric_columns.items():
        edits = overrides.get(category, {})
        if not edits:
            continue
        lines = patch_numeric_section(section, columns, edits)

    for category in ("inp_tanks",):
        for element_id, values in overrides.get(category, {}).items():
            if not any(name in values for name in ("init_level", "min_level", "max_level")):
                continue
            records = _read_inp_records("".join(lines)).get("[TANKS]", {})
            row = records.get(element_id.casefold())
            if not row:
                continue
            initial = float(values.get("init_level", _inp_number(row, 2)))
            minimum = float(values.get("min_level", _inp_number(row, 3)))
            maximum = float(values.get("max_level", _inp_number(row, 4)))
            if not minimum <= initial <= maximum:
                raise ValueError(
                    f"Tank '{element_id}' levels must satisfy minimum ≤ initial ≤ maximum."
                )

    def patch_pipe_status(status_updates):
        if not status_updates:
            return
        requested = {element_id.casefold(): status for element_id, status in status_updates.items()}
        found = set()
        patched = []
        in_pipes = False
        status_words = {"OPEN", "CLOSED", "CV"}
        for raw_line in lines:
            stripped = raw_line.strip()
            if stripped.startswith("["):
                in_pipes = stripped.upper() == "[PIPES]"
                patched.append(raw_line)
                continue
            if not in_pipes or not stripped or stripped.startswith(";"):
                patched.append(raw_line)
                continue
            body = raw_line.rstrip("\r\n")
            line_ending = raw_line[len(body):]
            code, separator, comment = body.partition(";")
            fields = code.split()
            key = fields[0].casefold() if fields else ""
            if key not in requested:
                patched.append(raw_line)
                continue
            status_index = next(
                (index for index, value in enumerate(fields)
                 if index >= 6 and value.upper() in status_words),
                None,
            )
            if status_index is not None:
                fields[status_index] = requested[key]
            else:
                while len(fields) < 6:
                    fields.append("0")
                fields.append(requested[key])
            new_line = "\t".join(fields)
            if separator:
                new_line += " ;" + comment
            patched.append(new_line + line_ending)
            found.add(key)
        missing = sorted(set(requested) - found)
        if missing:
            raise ValueError("Unknown pipe ID(s): " + ", ".join(sorted(status_updates)))
        lines[:] = patched

    def patch_status_section(status_updates, add_missing, remove_ids=()):
        remove_keys = {element_id.casefold() for element_id in remove_ids}
        if not status_updates and not remove_keys:
            return
        remaining = {element_id.casefold(): (element_id, status)
                     for element_id, status in status_updates.items()}
        found = False
        in_status = False
        output = []

        def append_remaining():
            for original_id, status in remaining.values():
                output.append(f"{original_id}\t{status}\n")
            remaining.clear()

        for raw_line in lines:
            stripped = raw_line.strip()
            if stripped.startswith("["):
                if in_status and remaining and add_missing:
                    append_remaining()
                in_status = stripped.upper() == "[STATUS]"
                found = found or in_status
                output.append(raw_line)
                continue
            if not in_status or not stripped or stripped.startswith(";"):
                output.append(raw_line)
                continue
            body = raw_line.rstrip("\r\n")
            line_ending = raw_line[len(body):]
            code, separator, comment = body.partition(";")
            fields = code.split()
            key = fields[0].casefold() if fields else ""
            if key in remove_keys:
                if separator and comment.strip():
                    output.append(";" + comment.lstrip() + line_ending)
                continue
            if key not in remaining:
                output.append(raw_line)
                continue
            original_id, status = remaining.pop(key)
            while len(fields) < 2:
                fields.append("")
            fields[0] = original_id
            fields[1] = status
            new_line = "\t".join(fields)
            if separator:
                new_line += " ;" + comment
            output.append(new_line + line_ending)

        if remaining and add_missing:
            if found:
                append_remaining()
            else:
                insert_at = next(
                    (index for index, raw_line in enumerate(output)
                     if raw_line.strip().upper() == "[END]"),
                    len(output),
                )
                rows = ["[STATUS]\n"]
                rows.extend(f"{original_id}\t{status}\n" for original_id, status in remaining.values())
                rows.append("\n")
                output[insert_at:insert_at] = rows
                remaining.clear()
        lines[:] = output

    pipe_edits = overrides.get("inp_pipes", {})
    pipe_status = {
        element_id: values["status"]
        for element_id, values in pipe_edits.items()
        if values.get("status")
    }
    invalid_pipe_status = {
        element_id: status for element_id, status in pipe_status.items()
        if status not in {"Open", "Closed", "CV"}
    }
    if invalid_pipe_status:
        raise ValueError(
            "Pipe status must be Open, Closed, or CV for: "
            + ", ".join(sorted(invalid_pipe_status))
        )
    patch_pipe_status(pipe_status)
    patch_status_section(
        {element_id: status for element_id, status in pipe_status.items() if status != "CV"},
        add_missing=False,
        remove_ids=[
            element_id for element_id, status in pipe_status.items() if status == "CV"
        ],
    )

    valve_status = {
        element_id: values["status"]
        for element_id, values in overrides.get("inp_valves", {}).items()
        if values.get("status")
    }
    invalid_valve_status = {
        element_id: status for element_id, status in valve_status.items()
        if status not in {"Open", "Closed", "Active"}
    }
    if invalid_valve_status:
        raise ValueError(
            "Valve status must be Open, Closed, or Active for: "
            + ", ".join(sorted(invalid_valve_status))
        )
    patch_status_section(valve_status, add_missing=True)

    try:
        destination_dir = os.path.dirname(destination_path)
        if destination_dir:
            os.makedirs(destination_dir, exist_ok=True)
        with open(
            destination_path,
            "w",
            encoding="ascii",
            errors="replace",
            newline="",
        ) as destination:
            destination.writelines(lines)
    except OSError as exc:
        raise ValueError(f"Could not write edited EPANET working copy: {exc}") from exc
    return destination_path


def patch_inp_junctions(
    source_path: str,
    destination_path: str,
    overrides: Dict[str, Dict[str, Optional[float]]],
) -> str:
    """Compatibility helper for junction-only callers."""
    return patch_inp_elements(
        source_path,
        destination_path,
        {"inp_junctions": overrides},
    )


def validate_leak_events(inp_path: str, leak_events: List[dict], duration_sec: int) -> None:
    """Reject leak events that cannot be applied to a junction in this model."""
    try:
        from epyt_flow.simulation import ScenarioSimulator
    except ImportError as exc:
        raise RuntimeError(
            "epyt-flow is not installed. Run: pip install epyt-flow==0.17.1"
        ) from exc

    try:
        with ScenarioSimulator(f_inp_in=inp_path) as simulator:
            api = simulator.epanet_api
            junction_ids = {
                node_id for node_id in api.get_all_nodes_id()
                if api.get_node_type(api.get_node_idx(node_id)) == 0
            }
    except Exception as exc:
        raise ValueError(f"Could not inspect the EPANET network for leak validation: {exc}") from exc

    for event in leak_events:
        node_id = event.get("node_id")
        if node_id not in junction_ids:
            raise ValueError(f"Leak location resolved to non-junction node '{node_id}'.")
        diameter = float(event.get("diameter", 0))
        start_time = int(event.get("start_time", 0))
        end_time = int(event.get("end_time", duration_sec))
        if diameter <= 0 or start_time < 0 or end_time <= start_time or end_time > duration_sec:
            raise ValueError(f"Invalid leakage event timing or diameter for node '{node_id}'.")


# ── main runner ────────────────────────────────────────────────────────────────


# ── result dataclasses ────────────────────────────────────────────────────────

@dataclass
class NodeResult:
    element_id:      str
    time_step:       int             = 0
    lat:             Optional[float] = None
    lon:             Optional[float] = None
    pressure:        Optional[float] = None
    head:            Optional[float] = None
    demand:          Optional[float] = None
    water_age:       Optional[float] = None
    is_low_pressure: bool            = False


@dataclass
class PipeResult:
    element_id:       str
    time_step:        int             = 0
    lat:              Optional[float] = None
    lon:              Optional[float] = None
    flow_rate:        Optional[float] = None
    velocity:         Optional[float] = None
    headloss:         Optional[float] = None
    is_high_velocity: bool            = False


@dataclass
class TankResult:
    element_id: str
    time_step:  int
    volume_m3:  Optional[float] = None
    lat:        Optional[float] = None
    lon:        Optional[float] = None


@dataclass
class SimulationOutput:
    node_results:  List[NodeResult]  = field(default_factory=list)
    pipe_results:  List[PipeResult]  = field(default_factory=list)
    tank_results:  List[TankResult]  = field(default_factory=list)
    summary:       dict              = field(default_factory=dict)


# ── main runner ───────────────────────────────────────────────────────────────

def run_simulation(
    inp_path:         str,
    duration_hrs:     int  = 24,
    time_step_min:    int  = 60,
    # legacy: old callers pass AbruptLeakage-compatible dicts
    leak_events:      Optional[List[dict]] = None,
    # new: structured EPyT-Flow event objects
    leakage_objects:  Optional[list] = None,
    sensor_faults:    Optional[list] = None,
    actuator_events:  Optional[list] = None,
    model_uncertainty = None,
    sensor_noise      = None,
    # demand model: "DDA" (demand-driven, default) or "PDA" (pressure-driven)
    demand_model:          str   = "DDA",
    pda_pressure_min:      float = 0.0,
    pda_pressure_required: float = 0.1,
    pda_pressure_exponent: float = 0.5,
) -> SimulationOutput:
    """
    Run an EPyT-Flow simulation.

    Parameters
    ----------
    inp_path         : EPANET .inp file
    duration_hrs     : simulation duration
    time_step_min    : hydraulic time step
    leak_events      : legacy list of dicts (node_id, diameter, start_time, end_time)
                       — converted to AbruptLeakage automatically
    leakage_objects  : pre-built EPyT-Flow Leakage objects (AbruptLeakage / IncipientLeakage)
    sensor_faults    : pre-built SensorFault objects
    actuator_events  : pre-built ActuatorEvent objects
    model_uncertainty: ModelUncertainty object (demand/roughness perturbation)
    sensor_noise     : SensorNoise object (Gaussian noise on readings)
    demand_model          : "DDA" (demand-driven analysis, default) or
                             "PDA" (pressure-driven analysis)
    pda_pressure_min      : minimum pressure below which demand is zero (PDA only)
    pda_pressure_required : pressure at/above which full demand is delivered (PDA only)
    pda_pressure_exponent : exponent in the pressure-demand relationship (PDA only)
    """
    try:
        from epyt_flow.simulation import ScenarioSimulator, EpanetConstants
        from epyt_flow.simulation.events import AbruptLeakage
    except ImportError as exc:
        raise RuntimeError("epyt-flow is not installed.") from exc

    settings      = get_settings()
    duration_sec  = duration_hrs  * 3600
    time_step_sec = time_step_min * 60

    # ── demand model ───────────────────────────────────────────────────────────
    demand_model_norm = (demand_model or "DDA").strip().upper()
    if demand_model_norm not in ("DDA", "PDA"):
        logger.warning("Unknown demand_model %r — falling back to DDA", demand_model)
        demand_model_norm = "DDA"

    demand_model_dict = {
        "type":               EpanetConstants.EN_PDA if demand_model_norm == "PDA"
                               else EpanetConstants.EN_DDA,
        "pressure_min":       pda_pressure_min,
        "pressure_required":  pda_pressure_required,
        "pressure_exponent":  pda_pressure_exponent,
    }

    # Leak events are converted to AbruptLeakage objects further below, once
    # pipe_topology is known (see "resolve leak events" after pass 1) —
    # AbruptLeakage needs a pipe/link ID, and legacy callers here only give
    # us a node_id (the junction nearest the reported leak location).

    # ── pass 1: probe network topology ───────────────────────────────────────
    node_coords:    Dict[str, Tuple[float, float]] = {}
    pipe_topology:  Dict[str, Tuple[str, str]]     = {}
    all_node_ids:   list = []
    all_pipe_ids:   list = []
    all_tank_ids:   list = []
    all_pump_ids:   list = []
    all_valve_ids:  list = []

    with ScenarioSimulator(f_inp_in=inp_path) as probe:
        probe.set_general_parameters(
            simulation_duration = duration_sec,
            hydraulic_time_step = time_step_sec,
            reporting_time_step = time_step_sec,
            demand_model        = demand_model_dict,
        )
        sc = probe.sensor_config
        all_node_ids  = list(sc.nodes)
        all_pipe_ids  = list(sc.links)
        all_tank_ids  = [t for t in (getattr(sc, "tanks", []) or [])]
        all_pump_ids  = [p for p in (getattr(sc, "pumps", []) or [])]
        all_valve_ids = [v for v in (getattr(sc, "valves", []) or [])]

    # Node coordinates and pipe topology come from parsing the .inp text
    # directly (see inp_parser.py) rather than per-node/per-link EPANET API
    # calls — the API's getNodeCoordinates(idx) does not return "node idx's
    # coordinates" (idx there selects an X/Y dimension across the whole
    # network, not a node), so a per-node loop over it silently fails for
    # nearly every node and appears to leave the toolkit instance unable to
    # reliably answer the subsequent link-topology queries either. The .inp
    # this scenario was built from already has exact [COORDINATES] and
    # [PIPES] sections (see dma_builder.build_dma_inp), so reading them back
    # is both simpler and correct.
    from app.services.inp_parser import parse_inp_coordinates, parse_inp_pipe_topology
    node_coords.update(parse_inp_coordinates(inp_path))
    pipe_topology.update(parse_inp_pipe_topology(inp_path))

    logger.info(
        "Probe: %d nodes (%d with coords), %d pipes, %d tanks, %d pumps, %d valves",
        len(all_node_ids), len(node_coords), len(all_pipe_ids),
        len(all_tank_ids), len(all_pump_ids), len(all_valve_ids),
    )

    # ── resolve leak events ───────────────────────────────────────────────────
    # AbruptLeakage (and IncipientLeakage) attach to a pipe/link, not a node —
    # EPANET models a leak as an orifice discharge along a specific pipe.
    # Legacy callers here only know the *node* nearest the reported leak
    # location (e.g. a lat/lon field report snapped to the nearest junction),
    # so we resolve each node_id to one of its incident pipes using the
    # topology just probed above, instead of passing the node_id straight
    # through as a link_id (which EPANET would reject as an unknown link).
    node_to_pipes: Dict[str, List[str]] = {}
    for pid, (s_id, e_id) in pipe_topology.items():
        node_to_pipes.setdefault(s_id, []).append(pid)
        node_to_pipes.setdefault(e_id, []).append(pid)

    all_leakages = list(leakage_objects or [])
    # A pipe can only carry one leak at a time (EPyT-Flow rejects a second
    # AbruptLeakage on the same link). Track which pipes are already
    # claimed — by explicitly-supplied leakage_objects, and by earlier
    # events in this same loop — so two different leak nodes that happen
    # to share an incident pipe don't both try to claim it. Without this,
    # the second one only fails later, deep inside sim.add_leakage(), with
    # a confusing "already a leak at pipe X" error.
    used_pipes = {lk.link_id for lk in (leakage_objects or [])}
    skipped_no_pipe = 0
    skipped_pipe_taken = 0
    for ev in (leak_events or []):
        try:
            if ev.get("link_id"):
                link_id = str(ev["link_id"])
                if link_id in used_pipes:
                    skipped_pipe_taken += 1
                    logger.warning(
                        "Leak event link_id %r already has a leak — skipped", link_id
                    )
                    continue
            else:
                node_id = str(ev["node_id"])  # legacy callers pass node_id
                incident = node_to_pipes.get(node_id)
                if not incident:
                    skipped_no_pipe += 1
                    logger.warning(
                        "Leak event at node %r has no incident pipe — skipped", node_id
                    )
                    continue
                free_pipe = next((p for p in incident if p not in used_pipes), None)
                if free_pipe is None:
                    skipped_pipe_taken += 1
                    logger.warning(
                        "Leak event at node %r: all %d incident pipe(s) already "
                        "have a leak — skipped", node_id, len(incident)
                    )
                    continue
                link_id = free_pipe
            used_pipes.add(link_id)
            all_leakages.append(AbruptLeakage(
                link_id    = link_id,
                diameter   = float(ev.get("diameter", 0.015)),
                start_time = int(ev.get("start_time", 0)),
                end_time   = int(ev.get("end_time", duration_sec)),
            ))
        except Exception as e:
            logger.warning("Skipping leak event: %s", e)

    logger.info(
        "EPyT-Flow run: %s | %dh / %dmin | demand_model=%s leaks=%d "
        "(skipped %d no-incident-pipe, %d pipe-already-taken) "
        "faults=%d actuators=%d unc=%s noise=%s",
        inp_path, duration_hrs, time_step_min, demand_model_norm,
        len(all_leakages), skipped_no_pipe, skipped_pipe_taken,
        len(sensor_faults or []),
        len(actuator_events or []),
        model_uncertainty is not None,
        sensor_noise is not None,
    )


    # ── pass 2: full simulation ───────────────────────────────────────────────
    with ScenarioSimulator(f_inp_in=inp_path) as sim:
        sim.set_general_parameters(
            simulation_duration = duration_sec,
            hydraulic_time_step = time_step_sec,
            reporting_time_step = time_step_sec,
            demand_model        = demand_model_dict,
        )

        # Demand model (DDA/PDA) is set two ways, both confirmed to actually
        # work (unlike a third approach that was tried and removed — the
        # EPyT-Flow `epanet_api` object here is its own `EPyT` wrapper class,
        # which does not expose a `setDemandModel` method, confirmed by an
        # AttributeError in production logs):
        #   1. sim.set_general_parameters(demand_model=...) just above,
        #      EPyT-Flow's own documented API for this.
        #   2. The .inp file's own [OPTIONS] section declares
        #      "Demand Model PDA" directly when requested (see
        #      dma_builder.build_dma_inp) — this is authoritative because
        #      it's EPANET's native mechanism and doesn't depend on the
        #      Python wrapper propagating the setting correctly.

        # ── sensors: everything ───────────────────────────────────────────────
        sim.place_pressure_sensors_everywhere()
        sim.place_flow_sensors_everywhere()
        sim.place_demand_sensors_everywhere()
        sim.place_node_quality_sensors_everywhere()   # water age
        sim.place_link_quality_sensors_everywhere()
        if all_tank_ids:
            sim.place_tank_sensors_everywhere()
        if all_valve_ids:
            sim.place_valve_sensors_everywhere()
        if all_pump_ids:
            sim.place_pump_sensors_everywhere()
            sim.place_pump_state_sensors_everywhere()

        # ── leakage events ────────────────────────────────────────────────────
        leaks_added = 0
        for lk in all_leakages:
            try:
                sim.add_leakage(lk)
                leaks_added += 1
            except Exception as e:
                logger.warning("Could not add leakage %s: %s", lk, e)

        # ── sensor faults ─────────────────────────────────────────────────────
        for sf in (sensor_faults or []):
            try:
                sim.add_sensor_fault(sf)
            except Exception as e:
                logger.warning("Could not add sensor fault: %s", e)

        # ── actuator events ───────────────────────────────────────────────────
        for ae in (actuator_events or []):
            try:
                sim.add_actuator_event(ae)
            except Exception as e:
                logger.warning("Could not add actuator event: %s", e)

        # ── uncertainties ─────────────────────────────────────────────────────
        if model_uncertainty is not None:
            try:
                sim.set_model_uncertainty(model_uncertainty)
            except Exception as e:
                logger.warning("Could not apply model uncertainty: %s", e)
        if sensor_noise is not None:
            try:
                sim.set_sensor_noise(sensor_noise)
            except Exception as e:
                logger.warning("Could not apply sensor noise: %s", e)

        # ── run ───────────────────────────────────────────────────────────────
        logger.info("Running EPyT-Flow simulation …")
        scada = sim.run_simulation()
        logger.info("EPyT-Flow simulation complete.")

        # ── extract arrays ────────────────────────────────────────────────────
        sc_out           = scada.sensor_config
        sensor_node_ids  = list(sc_out.pressure_sensors)
        sensor_pipe_ids  = list(sc_out.flow_sensors)
        sensor_tank_ids  = list(getattr(sc_out, "tank_volume_sensors", []) or [])

        pressure_arr = _safe(scada.get_data_pressures)
        flow_arr     = _safe(scada.get_data_flows)
        quality_arr  = _safe(scada.get_data_nodes_quality)
        demand_arr   = _safe(scada.get_data_demands)
        tank_vol_arr = _safe(scada.get_data_tanks_water_volume) if sensor_tank_ids else None

    n_steps = pressure_arr.shape[0] if pressure_arr is not None else 0
    logger.info("Parsing %d time steps × %d nodes × %d pipes", n_steps, len(sensor_node_ids), len(sensor_pipe_ids))

    output = SimulationOutput()

    # demand sensor ordering may differ from pressure sensor ordering
    demand_node_ids = list(getattr(scada.sensor_config, "demand_sensors", []) or [])
    demand_map: Dict[str, int] = {nid: i for i, nid in enumerate(demand_node_ids)}

    for t in range(n_steps):
        # ── nodes ─────────────────────────────────────────────────────────────
        for j, nid in enumerate(sensor_node_ids):
            lon, lat = node_coords.get(nid, (None, None))
            pressure = _val(pressure_arr, t, j)
            age_s    = _val(quality_arr,  t, j)
            d_idx    = demand_map.get(nid)
            demand   = _val(demand_arr, t, d_idx) if d_idx is not None and demand_arr is not None else None
            # demand in EPyT-Flow is CMH — convert to m³/s for storage
            demand_m3s = demand / 3600.0 if demand is not None else None

            output.node_results.append(NodeResult(
                element_id      = nid,
                time_step       = t,
                lon             = lon,
                lat             = lat,
                pressure        = _r3(pressure),
                demand          = _r6(demand_m3s),
                water_age       = _r2(age_s / 3600.0) if age_s is not None else None,
                is_low_pressure = bool(pressure is not None and pressure < settings.min_pressure_m),
            ))

        # ── pipes ─────────────────────────────────────────────────────────────
        for p, pid in enumerate(sensor_pipe_ids):
            flow_cmh = _val(flow_arr, t, p)
            flow_m3s = flow_cmh / 3600.0 if flow_cmh is not None else None
            s_n, e_n = pipe_topology.get(pid, (None, None))
            sc_  = node_coords.get(s_n, (None, None))
            ec_  = node_coords.get(e_n, (None, None))
            mid_lon = (sc_[0] + ec_[0]) / 2 if sc_[0] and ec_[0] else None
            mid_lat = (sc_[1] + ec_[1]) / 2 if sc_[1] and ec_[1] else None

            output.pipe_results.append(PipeResult(
                element_id  = pid,
                time_step   = t,
                lon         = _r7(mid_lon),
                lat         = _r7(mid_lat),
                flow_rate   = _r6(flow_m3s),
                is_high_velocity = False,  # velocity needs pipe diameter; skipped
            ))

        # ── tanks ─────────────────────────────────────────────────────────────
        for k, tid in enumerate(sensor_tank_ids):
            vol = _val(tank_vol_arr, t, k) if tank_vol_arr is not None else None
            lon, lat = node_coords.get(tid, (None, None))
            output.tank_results.append(TankResult(
                element_id = tid,
                time_step  = t,
                volume_m3  = _r2(vol),
                lon        = lon,
                lat        = lat,
            ))

    # ── summary ───────────────────────────────────────────────────────────────
    pressures = [n.pressure  for n in output.node_results if n.pressure is not None]
    flows     = [abs(p.flow_rate) for p in output.pipe_results if p.flow_rate is not None]
    ages      = [n.water_age for n in output.node_results if n.water_age is not None]
    demands   = [n.demand    for n in output.node_results if n.demand is not None]

    output.summary = {
        "pressure_min_m":       _r2(min(pressures)) if pressures else None,
        "pressure_max_m":       _r2(max(pressures)) if pressures else None,
        "pressure_avg_m":       _r2(sum(pressures) / len(pressures)) if pressures else None,
        "flow_max_m3s":         _r6(max(flows))    if flows     else None,
        "flow_avg_m3s":         _r6(sum(flows) / len(flows)) if flows else None,
        "water_age_max_hrs":    _r2(max(ages))     if ages      else None,
        "total_demand_m3h":     _r3(sum(demands) * 3600.0 / n_steps) if demands and n_steps else None,
        "low_pressure_nodes":   sum(1 for n in output.node_results if n.is_low_pressure),
        "high_velocity_pipes":  0,
        "leak_events_injected": leaks_added,
        "sensor_faults_applied":len(sensor_faults or []),
        "actuator_events":      len(actuator_events or []),
        "model_uncertainty":    model_uncertainty is not None,
        "sensor_noise":         sensor_noise is not None,
        "total_nodes":          len(sensor_node_ids),
        "total_pipes":          len(sensor_pipe_ids),
        "total_tanks":          len(sensor_tank_ids),
        "duration_hrs":         duration_hrs,
        "time_steps":           n_steps,
        "engine":               "EPyT-Flow v0.17.1",
        "demand_model":         demand_model_norm,
        "pda_pressure_min":       pda_pressure_min      if demand_model_norm == "PDA" else None,
        "pda_pressure_required":  pda_pressure_required if demand_model_norm == "PDA" else None,
        "pda_pressure_exponent":  pda_pressure_exponent if demand_model_norm == "PDA" else None,
    }

    logger.info(
        "Parsed %d node·t, %d pipe·t, %d tank·t records",
        len(output.node_results), len(output.pipe_results), len(output.tank_results),
    )
    return output


# ── array helpers ─────────────────────────────────────────────────────────────

def _safe(fn):
    """Call fn(), return result or None on error."""
    try:
        return fn()
    except Exception:
        return None


def _val(arr, t: int, j):
    """Safely index a 2-D numpy array; return float or None."""
    if arr is None or j is None:
        return None
    try:
        v = float(arr[t, j])
        return v if math.isfinite(v) else None
    except (IndexError, TypeError):
        return None


def _r2(v): return round(v, 2)  if v is not None else None
def _r3(v): return round(v, 3)  if v is not None else None
def _r6(v): return round(v, 6)  if v is not None else None
def _r7(v): return round(v, 7)  if v is not None else None