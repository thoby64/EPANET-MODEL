# tests/test_majiscope_worker.py
"""
Regression tests for the MajiScope integration inside the simulation worker.

Every one of these guards a bug that was a *silent* NameError: the failing
statement sat inside a ``try/except Exception`` block, so the service kept
running and reported success while the MajiScope result delivery quietly
did nothing on every single run.
"""

import json

from app.core.config import get_settings
from app.workers.simulation_worker import (
    _majiscope_metadata_for_file,
    _majiscope_metadata_for_scenario,
)


def _write_sidecar(directory, filename: str, payload: dict) -> str:
    stem = filename[:-5] if filename.endswith(".gpkg") else filename
    path = f"{directory}/{stem}.json"
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    return path


def test_metadata_sidecar_is_read(tmp_path, monkeypatch):
    """The metadata sidecar must be parsed, not swallowed by a NameError."""
    settings = get_settings()
    monkeypatch.setattr(settings, "gpkg_dir", str(tmp_path), raising=False)

    payload = {
        "session_id": "sess-1",
        "dma_id": "dma-1",
        "dma_name": "NAKURU",
        "utility_id": "u-1",
        "utility_name": "AUWSA",
        "launch_token": "tok-1",
        "model_source": "gpkg",
    }
    _write_sidecar(str(tmp_path), "majiscope_case.gpkg", payload)

    result = _majiscope_metadata_for_file("majiscope_case.gpkg")
    assert result is not None, "MajiScope metadata was not read"
    assert result["dma_name"] == "NAKURU"
    assert result["session_id"] == "sess-1"


def test_metadata_ignored_for_foreign_files(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "gpkg_dir", str(tmp_path), raising=False)
    assert _majiscope_metadata_for_file("somebody-else.gpkg") is None
    assert _majiscope_metadata_for_file("not-a-gpkg.txt") is None


def test_scenario_metadata_prefers_the_embedded_copy():
    """A scenario that carries its own metadata must not need the sidecar."""

    class FakeScenario:
        id = 7
        gpkg_filename = "majiscope_missing.gpkg"
        extra_demands = {
            "_majiscope": {
                "session_id": "sess-embedded",
                "dma_name": "EMBEDDED",
            }
        }

    result = _majiscope_metadata_for_scenario(FakeScenario())
    assert result is not None
    assert result["session_id"] == "sess-embedded"
    assert result["dma_name"] == "EMBEDDED"
