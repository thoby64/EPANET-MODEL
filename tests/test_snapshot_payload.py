from types import SimpleNamespace

from app.services.simulation_service import NodeResult, PipeResult, TankResult
from app.workers.simulation_worker import _build_detailed_results


def test_detailed_snapshot_includes_per_element_results_and_aggregates():
    output = SimpleNamespace(
        node_results=[
            NodeResult(
                element_id="N1",
                time_step=1,
                pressure=12.0,
                head=30.0,
                demand=0.001,
                water_age=0.0,
                is_low_pressure=False,
            ),
            NodeResult(
                element_id="N2",
                time_step=1,
                pressure=4.0,
                head=22.0,
                demand=0.002,
                water_age=2.0,
                is_low_pressure=True,
            ),
        ],
        pipe_results=[
            PipeResult(
                element_id="P1",
                time_step=1,
                flow_rate=-0.5,
                velocity=3.0,
                headloss=1.5,
                is_high_velocity=True,
            ),
        ],
        tank_results=[
            TankResult(element_id="T1", time_step=1, volume_m3=75.0),
        ],
    )

    details = _build_detailed_results(output, time_step_min=30)

    assert details["nodes"][0]["water_age_hrs"] == 0.0
    assert details["pipes"][0]["flow_m3s"] == -0.5
    assert details["tanks"][0]["volume_m3"] == 75.0
    assert details["time_series"] == [{
        "time_step": 1,
        "hour": 0.5,
        "pressure_min_m": 4.0,
        "pressure_avg_m": 8.0,
        "pressure_max_m": 12.0,
        "water_age_avg_hrs": 1.0,
        "water_age_max_hrs": 2.0,
        "total_demand_m3h": 10.8,
        "low_pressure_nodes": 1,
        "flow_avg_abs_m3s": 0.5,
        "flow_max_abs_m3s": 0.5,
        "velocity_max_abs_mps": 3.0,
        "tank_volumes_m3": {"T1": 75.0},
    }]
