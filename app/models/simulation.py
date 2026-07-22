# app/models/simulation.py
"""
ORM models for the DMA hydraulic simulation service.

Tables
------
  sim_scenarios  — one row per simulation job (inputs, status, summary)
  sim_results    — per-element results (nodes and pipes, per time step)
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey,
    Integer, JSON, String, Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class SimScenario(Base):
    __tablename__ = "sim_scenarios"

    id:            Mapped[int]           = mapped_column(Integer, primary_key=True, index=True)
    gpkg_filename: Mapped[str]           = mapped_column(String(256), nullable=False)
    name:          Mapped[str]           = mapped_column(String(128), default="Unnamed")
    description:   Mapped[Optional[str]] = mapped_column(Text,      nullable=True)

    # DMA simulation parameters (stored for reproducibility)
    base_demand:    Mapped[float] = mapped_column(Float,   default=0.011)
    duration_hrs:   Mapped[int]   = mapped_column(Integer, default=24)
    time_step_min:  Mapped[int]   = mapped_column(Integer, default=60)
    leakage_frac:   Mapped[float] = mapped_column(Float,   default=0.20)
    scenario_type:  Mapped[str]   = mapped_column(String(24), default="baseline")
    demand_model:   Mapped[str]   = mapped_column(String(12), default="DDA")
    pda_pressure_min:      Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pda_pressure_required: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pda_pressure_exponent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # DMA metadata / pre-built .inp path stored here as a JSON dict.
    # Keys produced by the DMA router:
    #   _dma_inp_path, _dma_name, _leakage_frac, _total_demand_m3h,
    #   _connectors_added, _connector_length_m, _original_components,
    #   _repair_warnings
    extra_demands:  Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # lifecycle
    status:        Mapped[str]           = mapped_column(String(20), default="PENDING")
    error_message: Mapped[Optional[str]] = mapped_column(Text,      nullable=True)
    created_at:    Mapped[datetime]      = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    started_at:    Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at:   Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # summary JSON written at completion (includes epanet_flow_balance from .rpt)
    summary: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Durable MajiScope snapshot outbox.
    majiscope_snapshot_payload: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    majiscope_snapshot_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    majiscope_snapshot_attempts: Mapped[int] = mapped_column(Integer, default=0)
    majiscope_snapshot_last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    majiscope_snapshot_delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    results: Mapped[list["SimResult"]] = relationship(
        "SimResult", back_populates="scenario", cascade="all, delete-orphan"
    )


class SimResult(Base):
    __tablename__ = "sim_results"

    id:           Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    scenario_id:  Mapped[int] = mapped_column(
        Integer, ForeignKey("sim_scenarios.id", ondelete="CASCADE"), index=True
    )
    time_step:    Mapped[int] = mapped_column(Integer, default=0)   # hour index
    element_type: Mapped[str] = mapped_column(String(8))            # 'node' | 'pipe'
    element_id:   Mapped[str] = mapped_column(String(128))
    lat:          Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    lon:          Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # node columns
    pressure:        Mapped[Optional[float]] = mapped_column(Float,   nullable=True)
    head:            Mapped[Optional[float]] = mapped_column(Float,   nullable=True)
    demand:          Mapped[Optional[float]] = mapped_column(Float,   nullable=True)
    water_age:       Mapped[Optional[float]] = mapped_column(Float,   nullable=True)
    is_low_pressure: Mapped[bool]            = mapped_column(Boolean, default=False)

    # pipe columns
    flow_rate:        Mapped[Optional[float]] = mapped_column(Float,   nullable=True)
    velocity:         Mapped[Optional[float]] = mapped_column(Float,   nullable=True)
    headloss:         Mapped[Optional[float]] = mapped_column(Float,   nullable=True)
    is_high_velocity: Mapped[bool]            = mapped_column(Boolean, default=False)

    scenario: Mapped["SimScenario"] = relationship("SimScenario", back_populates="results")
