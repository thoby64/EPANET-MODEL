# app/core/config.py
from functools import lru_cache
from typing import Any, List
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # service identity
    service_name:    str  = "EPANET Hydraulic Service"
    service_version: str  = "1.0.0"
    debug:           bool = False

    # auth — stored as a comma-separated string, exposed as a list
    api_keys: str = "change-me-key-1"

    # Key handed to the standalone simulator UI so an operator can type the same
    # value that the service accepts. Empty means the UI keeps asking for it.
    standalone_api_key: str = ""

    @field_validator("api_keys", mode="before")
    @classmethod
    def parse_api_keys(cls, v):
        return v  # kept as raw string; split in property below

    @field_validator("debug", mode="before")
    @classmethod
    def parse_debug_value(cls, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on", "debug", "development"}:
                return True
            if normalized in {"0", "false", "no", "off", "release", "production"}:
                return False
        return bool(value)

    @property
    def api_key_list(self) -> List[str]:
        return [k.strip() for k in self.api_keys.split(",") if k.strip()]

    @property
    def effective_standalone_api_key(self) -> str:
        """The key the standalone simulator UI offers to the operator."""
        return (self.standalone_api_key or "").strip()

    # paths
    gpkg_dir:     str = "./data/gpkg"
    inp_dir:      str = "./data/inp"
    database_url: str = "sqlite+aiosqlite:///./data/db/epanet_service.db"
    reports_dir:  str = "./data/reports"

    # simulation defaults
    default_duration_hrs: int   = 24
    default_timestep_min: int   = 60
    default_base_demand:  float = 0.001
    min_pressure_m:       float = 7.0
    max_velocity_ms:      float = 3.0

    # MajiScope integration
    majiscope_backend_url: str = ""
    majiscope_return_url: str = ""
    majiscope_launch_secret: str = ""
    majiscope_callback_secret: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
