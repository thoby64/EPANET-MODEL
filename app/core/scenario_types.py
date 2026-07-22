"""Scenario classification helpers for hydraulic simulations."""

from typing import Final

BASELINE: Final[str] = "baseline"
REPORTED_LEAK: Final[str] = "reported_leak"
PLANNED_SHUTDOWN: Final[str] = "planned_shutdown"
FIRE_FLOW: Final[str] = "fire_flow"
RESEARCH: Final[str] = "research"

ALL_SCENARIO_TYPES: Final[frozenset[str]] = frozenset(
    {BASELINE, REPORTED_LEAK, PLANNED_SHUTDOWN, FIRE_FLOW, RESEARCH}
)


def normalize_scenario_type(value: str | None, has_reported_leaks: bool = False) -> str:
    if value:
        scenario_type = value.strip().lower()
    else:
        scenario_type = REPORTED_LEAK if has_reported_leaks else BASELINE

    if scenario_type not in ALL_SCENARIO_TYPES:
        raise ValueError(
            f"scenario_type must be one of {sorted(ALL_SCENARIO_TYPES)}, got '{value}'."
        )
    if scenario_type == REPORTED_LEAK and not has_reported_leaks:
        raise ValueError("reported_leak scenarios require at least one reported leak.")
    if scenario_type != REPORTED_LEAK and has_reported_leaks:
        raise ValueError("reported leaks can only be sent with reported_leak scenarios.")
    return scenario_type
