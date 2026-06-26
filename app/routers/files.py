# app/routers/files.py
"""
GET /files — lists all .gpkg files in the shared volume.

DMA-format files contain at minimum: dma, waterpipes layers.
The response includes which layers are present so the UI can
warn if mandatory layers are missing.
"""

import os
import json
import sqlite3
from typing import List

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import require_api_key
from app.core.config import get_settings

router = APIRouter(prefix="/files", tags=["files"])

_MANDATORY_LAYERS = {"dma", "waterpipes"}
_EXPECTED_LAYERS  = {"dma", "waterpipes", "watersources", "storagefacility", "valves", "bulk_meters"}


class GpkgFileInfo(BaseModel):
    filename:          str
    size_kb:           float
    layers:            List[str]
    missing_layers:    List[str]
    has_dma_structure: bool


@router.get("", response_model=List[GpkgFileInfo])
def list_files(auth_context: str = Depends(require_api_key)):
    """
    List all .gpkg files in the shared volume.
    Each entry reports which DMA layers are present so the frontend can
    flag incomplete files before a simulation is attempted.
    """
    settings = get_settings()
    gpkg_dir = settings.gpkg_dir
    if not os.path.isdir(gpkg_dir):
        return []

    allowed_filename = None
    if auth_context.startswith("majiscope:"):
        session_id = auth_context.split(":", 1)[1]
        metadata_path = os.path.join(gpkg_dir, f"majiscope_{session_id}.json")
        try:
            with open(metadata_path, "r", encoding="utf-8") as handle:
                allowed_filename = json.load(handle).get("filename")
        except Exception:
            allowed_filename = None

    result = []
    for fname in sorted(os.listdir(gpkg_dir)):
        if not fname.lower().endswith(".gpkg"):
            continue
        if allowed_filename and fname != allowed_filename:
            continue
        path = os.path.join(gpkg_dir, fname)
        size_kb = round(os.path.getsize(path) / 1024, 1)
        layers: List[str] = []
        try:
            conn = sqlite3.connect(path)
            rows = conn.execute("SELECT table_name FROM gpkg_contents").fetchall()
            layers = [r[0] for r in rows]
            conn.close()
        except Exception:
            pass

        layer_set      = set(layers)
        missing        = sorted(_MANDATORY_LAYERS - layer_set)
        has_structure  = _MANDATORY_LAYERS.issubset(layer_set)

        result.append(GpkgFileInfo(
            filename          = fname,
            size_kb           = size_kb,
            layers            = layers,
            missing_layers    = missing,
            has_dma_structure = has_structure,
        ))
    return result
