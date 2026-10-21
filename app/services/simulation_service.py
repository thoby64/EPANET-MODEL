# app/services/simulation_service.py
"""
EPyT-Flow v0.17.x simulation runner.

Fixes applied vs previous version
-----------------------------------
1. `ScenarioSimulator.epanet_api` is an `epanet_plus.EPyT` instance, whose
   node/link accessors are snake_case (`get_node_idx`, `get_node_type`,
   `get_link_idx`, `get_link_type`, `get_all_nodes_id`, `get_all_links_id`,
   `getcoord`, `getlinknodes`, `get_node_id`) — not the camelCase
   Matlab-toolkit names (`getNodeIndex`, `getNodeType`, ...) this module
   previously called, which don't exist on that class and raised
   AttributeError before a single simulation could complete.

2. Sensor API: use the explicit setter methods:
       sim.set_pressure_sensors(sensor_locations=[...])
       sim.set_flow_sensors(sensor_locations=[...])
       sim.set_node_quality_sensors(sensor_locations=[...])

3. ScadaData result access (v0.17):
       scada.get_data_pressures()    → ndarray (T, N)
       scada.get_data_flows()        → ndarray (T, N)  — in CMH (m³/h)
       scada.get_data_node_quality() → ndarray (T, N)  — water age in seconds

4. Sensor ordering: sensor_locations list order = column order in arrays.
   We keep a local ordered list so indexing is correct.

5. AbruptLeakage: uses keyword arg `diameter` (not `leak_diameter`).
"""

import logging
import math
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from app.core.config import get_settings

logger = logging.getLogger(__name__)


# ── result dataclasses ─────────────────────────────────────────────────────────

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
class SimulationOutput:
    node_results: List[NodeResult] = field(default_factory=list)
    pipe_results: List[PipeResult] = field(default_factory=list)
    tank_results: List["TankResult"] = field(default_factory=list)
    summary:      dict             = field(default_factory=dict)


@dataclass
class TankResult:
    element_id: str
    time_step:  int
    volume_m3:  Optional[float] = None
    lat:        Optional[float] = None
    lon:        Optional[float] = None


# ── coordinate extractor ───────────────────────────────────────────────────────

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

def run_simulation(
    inp_path:      str,
    duration_hrs:  int  = 24,
    time_step_min: int  = 60,
    leak_events:   Optional[List[dict]] = None,
    demand_model:  str = "DDA",
    pda_pressure_min: Optional[float] = None,
    pda_pressure_required: Optional[float] = None,
    pda_pressure_exponent: Optional[float] = None,
) -> SimulationOutput:
    """
    Run EPyT-Flow simulation from an EPANET .inp file.

    Parameters
    ----------
    inp_path      : path to the .inp file produced by network_builder
    duration_hrs  : total EPS duration (hours)
    time_step_min : hydraulic timestep (minutes)
    leak_events   : list of dicts:
                    { node_id, diameter, start_time, end_time }
                    Each becomes an AbruptLeakage event in EPyT-Flow.
    """
    try:
        from epyt_flow.simulation import ScenarioSimulator
        from epyt_flow.simulation.events import AbruptLeakage
    except ImportError as exc:
        raise RuntimeError(
            "epyt-flow is not installed.  Run: pip install epyt-flow==0.17.1"
        ) from exc

    settings      = get_settings()
    duration_sec  = duration_hrs  * 3600
    timestep_sec  = time_step_min * 60

    logger.info(
        "EPyT-Flow: loading .inp '%s'  (duration=%dh, ts=%dmin, leaks=%d)",
        inp_path, duration_hrs, time_step_min, len(leak_events or []),
    )

    # ── pass 1: probe network to discover node/pipe IDs + coordinates ──────────
    with ScenarioSimulator(f_inp_in=inp_path) as probe:
        probe.set_general_parameters(
            simulation_duration = duration_sec,
            hydraulic_time_step = timestep_sec,
            reporting_time_step = timestep_sec,
        )
        api = probe.epanet_api

        # all node / link names from EPANET
        all_node_ids: List[str] = api.get_all_nodes_id()
        all_link_ids: List[str] = api.get_all_links_id()

        # separate junction IDs from reservoir IDs
        junction_ids: List[str] = []
        reservoir_ids: List[str] = []
        tank_ids: List[str] = []
        for nid in all_node_ids:
            idx  = api.get_node_idx(nid)
            ntype = api.get_node_type(idx)   # returns int; 0=Junction,1=Reservoir,2=Tank
            if ntype == 1:
                reservoir_ids.append(nid)
            elif ntype == 2:
                tank_ids.append(nid)
            else:
                junction_ids.append(nid)

        # pipe IDs only (exclude pumps/valves)
        pipe_ids: List[str] = []
        pipe_diameters_m: Dict[str, float] = {}
        for lid in all_link_ids:
            idx   = api.get_link_idx(lid)
            ltype = api.get_link_type(idx)   # 1=PIPE, 2=PUMP, etc.
            if ltype == 1:
                pipe_ids.append(lid)
                diameter_mm = float(api.get_link_diameter(idx))
                if diameter_mm > 0:
                    pipe_diameters_m[lid] = diameter_mm / 1000.0

        # extract coordinates and topology
        node_coords  = _extract_node_coords(api, all_node_ids)
        pipe_topology = _extract_pipe_topology(api, pipe_ids)

    logger.info(
        "Network: %d junctions, %d reservoirs, %d pipes | coords resolved: %d",
        len(junction_ids), len(reservoir_ids), len(pipe_ids), len(node_coords),
    )

    # ── pass 2: build AbruptLeakage events ─────────────────────────────────────
    system_events = []
    for ev in (leak_events or []):
        nid = ev.get("node_id")
        if nid not in junction_ids:
            raise ValueError(
                f"Leak event node '{nid}' is not a junction in the EPANET model."
            )
        diameter = float(ev.get("diameter", 0.01))
        start_time = int(ev.get("start_time", 0))
        end_time = int(ev.get("end_time", duration_sec))
        if diameter <= 0 or start_time < 0 or end_time <= start_time or end_time > duration_sec:
            raise ValueError(f"Invalid leakage event parameters for node '{nid}'.")
        system_events.append(
            AbruptLeakage(
                node_id    = nid,
                link_id    = None,
                diameter   = diameter,
                start_time = start_time,
                end_time   = end_time,
            )
        )
        logger.info(
            "  AbruptLeakage → node=%s  Ø=%.3f m  t=[%d, %d]s",
            nid, ev.get("diameter", 0.01),
            ev.get("start_time", 0), ev.get("end_time", duration_sec),
        )

    # ── pass 3: run with sensors + leakage events ──────────────────────────────
    with ScenarioSimulator(f_inp_in=inp_path) as sim:
        sim.set_general_parameters(
            simulation_duration = duration_sec,
            hydraulic_time_step = timestep_sec,
            reporting_time_step = timestep_sec,
        )

        # register sensors — use junction_ids for pressure/quality (not reservoirs)
        sim.set_pressure_sensors(sensor_locations=junction_ids)
        sim.set_flow_sensors(sensor_locations=pipe_ids)
        if junction_ids:
            sim.place_demand_sensors_everywhere()
        if junction_ids:
            sim.set_node_quality_sensors(sensor_locations=junction_ids)
        if tank_ids:
            sim.place_tank_sensors_everywhere()

        # inject leakage events
        for leak in system_events:
            sim.add_leakage(leak)

        logger.info("Running EPyT-Flow simulation …")
        scada = sim.run_simulation()
        logger.info("EPyT-Flow simulation complete.")

        # result arrays — shape (T, N_sensors)
        pressure_arr = scada.get_data_pressures()     # m
        flow_arr     = scada.get_data_flows()         # m³/h (CMH)
        quality_arr  = scada.get_data_nodes_quality()  # seconds (water age)
        demand_arr   = scada.get_data_demands()       # m³/h (CMH)
        tank_volume_arr = (
            scada.get_data_tanks_water_volume() if tank_ids else None
        )

        # sensor ordering matches the lists we passed in
        pressure_node_ids = junction_ids
        flow_pipe_ids     = pipe_ids
        demand_node_ids   = list(getattr(scada.sensor_config, "demand_sensors", []) or [])
        tank_sensor_ids   = list(
            getattr(scada.sensor_config, "tank_volume_sensors", []) or []
        )
        demand_index = {node_id: index for index, node_id in enumerate(demand_node_ids)}

        n_steps = pressure_arr.shape[0] if pressure_arr is not None else 0
        logger.info("Result time steps: %d", n_steps)

    # ── parse results ──────────────────────────────────────────────────────────
    output = SimulationOutput()

    for t in range(n_steps):
        t_hr = t  # 1 step = 1 hour when time_step_min=60

        # nodes
        for j, nid in enumerate(pressure_node_ids):
            pressure = float(pressure_arr[t, j]) if pressure_arr is not None else None
            age_s    = (
                float(quality_arr[t, j])
                if quality_arr is not None and quality_arr.shape[1] > j
                else None
            )
            demand_index_for_node = demand_index.get(nid)
            demand_cmh = (
                float(demand_arr[t, demand_index_for_node])
                if demand_arr is not None and demand_index_for_node is not None
                and demand_arr.shape[1] > demand_index_for_node
                else None
            )
            coords = node_coords.get(nid, (None, None))
            output.node_results.append(NodeResult(
                element_id      = nid,
                time_step       = t_hr,
                lon             = coords[0],
                lat             = coords[1],
                pressure        = round(pressure, 3) if pressure is not None else None,
                demand          = round(demand_cmh / 3600.0, 6) if demand_cmh is not None else None,
                water_age       = round(age_s / 3600.0, 2) if age_s is not None else None,
                is_low_pressure = (
                    pressure is not None and pressure < settings.min_pressure_m
                ),
            ))

        # pipes
        for p, pid in enumerate(flow_pipe_ids):
            flow_cmh = float(flow_arr[t, p]) if flow_arr is not None else None
            flow_m3s = flow_cmh / 3600.0 if flow_cmh is not None else None
            diameter_m = pipe_diameters_m.get(pid)
            velocity = (
                abs(flow_m3s) / (math.pi * (diameter_m / 2) ** 2)
                if flow_m3s is not None and diameter_m is not None
                else None
            )

            s_nid, e_nid = pipe_topology.get(pid, (None, None))
            s_xy = node_coords.get(s_nid, (None, None))
            e_xy = node_coords.get(e_nid, (None, None))
            mid_lon = (s_xy[0] + e_xy[0]) / 2 if s_xy[0] and e_xy[0] else None
            mid_lat = (s_xy[1] + e_xy[1]) / 2 if s_xy[1] and e_xy[1] else None

            output.pipe_results.append(PipeResult(
                element_id = pid,
                time_step  = t_hr,
                lon        = round(mid_lon, 7) if mid_lon else None,
                lat        = round(mid_lat, 7) if mid_lat else None,
                flow_rate  = round(flow_m3s, 6) if flow_m3s is not None else None,
                velocity   = round(velocity, 4) if velocity is not None else None,
                is_high_velocity=bool(
                    velocity is not None and velocity > settings.max_velocity_ms
                ),
            ))

        for tank_index, tank_id in enumerate(tank_sensor_ids):
            volume = (
                float(tank_volume_arr[t, tank_index])
                if tank_volume_arr is not None and tank_volume_arr.shape[1] > tank_index
                else None
            )
            coords = node_coords.get(tank_id, (None, None))
            output.tank_results.append(TankResult(
                element_id=tank_id,
                time_step=t_hr,
                volume_m3=round(volume, 2) if volume is not None else None,
                lon=coords[0],
                lat=coords[1],
            ))

    # ── summary ────────────────────────────────────────────────────────────────
    pressures = [n.pressure  for n in output.node_results if n.pressure  is not None]
    flows     = [abs(p.flow_rate) for p in output.pipe_results if p.flow_rate is not None]
    ages      = [n.water_age for n in output.node_results if n.water_age is not None]
    demands   = [n.demand for n in output.node_results if n.demand is not None]

    output.summary = {
        "pressure_min_m":       round(min(pressures),               2) if pressures else None,
        "pressure_max_m":       round(max(pressures),               2) if pressures else None,
        "pressure_avg_m":       round(sum(pressures)/len(pressures), 2) if pressures else None,
        "flow_max_m3s":         round(max(flows),                   6) if flows     else None,
        "flow_avg_m3s":         round(sum(flows) / len(flows),       6) if flows     else None,
        "water_age_max_hrs":    round(max(ages),                    2) if ages      else None,
        "water_age_avg_hrs":    round(sum(ages) / len(ages),         2) if ages      else None,
        "total_demand_m3h":     round(sum(demands) * 3600.0 / n_steps, 3) if demands and n_steps else None,
        "low_pressure_nodes":   sum(1 for n in output.node_results if n.is_low_pressure),
        "high_velocity_pipes":  sum(1 for p in output.pipe_results if p.is_high_velocity),
        "leak_events_injected": len(system_events),
        "total_nodes":          len(junction_ids),
        "total_pipes":          len(pipe_ids),
        "total_tanks":          len(tank_sensor_ids),
        "duration_hrs":         duration_hrs,
        "time_steps":           n_steps,
        "demand_model":         demand_model,
        "pda_pressure_min":      pda_pressure_min,
        "pda_pressure_required": pda_pressure_required,
        "pda_pressure_exponent": pda_pressure_exponent,
        "engine":               "EPyT-Flow v0.17.1",
    }

    logger.info(
        "Parsed %d node records, %d pipe records, %d tank records",
        len(output.node_results), len(output.pipe_results), len(output.tank_results),
    )
    return output
