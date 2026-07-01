# app/routers/majiscope.py
"""
MajiScope integration endpoints.

These endpoints are service-to-service only. MajiScope remains responsible for
authorization and sends only the selected utility/DMA assets that are allowed
for the current user.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import tempfile
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel

from app.core.auth import require_api_key
from app.core.config import get_settings


router = APIRouter(prefix="/majiscope", tags=["majiscope"])

_SAFE_NAME_RE = re.compile(r"[^\w\-.]")
_ASSET_LAYER_MAP = {
    "pipe_network": "waterpipes",
    "water_sources": "watersources",
    "storage_facilities": "storagefacility",
    "valves": "valves",
    "bulk_meters": "bulk_meters",
}
_DMA_BOUNDARY_TOLERANCE_METERS = 5
_REPORTED_LEAKS_LAYER = "reported_leaks"


class MajiScopePreparedGpkg(BaseModel):
    filename: str
    layers: list[str]
    feature_counts: dict[str, int]
    message: str


class MajiScopeGpkgValidation(BaseModel):
    feature_counts: dict[str, int]
    message: str


class MajiScopeCleanupResult(BaseModel):
    session_id: str
    removed_files: list[str]
    message: str


def _safe_stem(value: str) -> str:
    safe = _SAFE_NAME_RE.sub("_", value)
    safe = re.sub(r"_+", "_", safe).strip("_.")
    return safe or "majiscope_model"


def _hash_launch_token(secret: str, token: str) -> str:
    return hashlib.sha256(f"{secret}:{token}".encode("utf-8")).hexdigest()


def _read_first_layer(path: str):
    try:
        import geopandas as gpd
    except ImportError as exc:
        raise HTTPException(
            status_code=500,
            detail="GeoPandas is required by the hydraulic service to prepare MajiScope model files.",
        ) from exc

    try:
        return gpd.read_file(path, on_invalid="ignore")
    except TypeError:
        return gpd.read_file(path)


def _boundary_gdf(dma_geojson: str, dma_id: str, dma_name: str):
    try:
        import geopandas as gpd
        from shapely.geometry import shape
    except ImportError as exc:
        raise HTTPException(
            status_code=500,
            detail="GeoPandas and Shapely are required to prepare MajiScope model files.",
        ) from exc

    try:
        geometry = json.loads(dma_geojson)
        boundary = shape(geometry)
    except Exception as exc:
        raise HTTPException(status_code=422, detail="DMA boundary GeoJSON is invalid.") from exc

    if boundary.is_empty or not boundary.is_valid:
        raise HTTPException(status_code=422, detail="DMA boundary geometry is empty or invalid.")

    return gpd.GeoDataFrame(
        [{"dma_id": dma_id, "name": dma_name, "geometry": boundary}],
        geometry="geometry",
        crs="EPSG:4326",
    )


def _boundary_with_tolerance(boundary):
    try:
        import geopandas as gpd
    except ImportError as exc:
        raise HTTPException(
            status_code=500,
            detail="GeoPandas is required by the hydraulic service to prepare MajiScope model files.",
        ) from exc

    boundary_gdf = gpd.GeoDataFrame([{"geometry": boundary}], geometry="geometry", crs="EPSG:4326")
    projected = boundary_gdf.to_crs("EPSG:3857")
    buffered = projected.geometry.iloc[0].buffer(_DMA_BOUNDARY_TOLERANCE_METERS)
    return gpd.GeoSeries([buffered], crs="EPSG:3857").to_crs("EPSG:4326").iloc[0]


def _filter_to_boundary(asset_path: str, boundary):
    gdf = _read_first_layer(asset_path)
    if gdf.empty:
        return gdf
    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326")
    else:
        gdf = gdf.to_crs("EPSG:4326")
    gdf = gdf[gdf.geometry.notna()]
    if gdf.empty:
        return gdf
    return gdf[gdf.geometry.intersects(_boundary_with_tolerance(boundary))].copy()


def _geojson_to_gdf(raw_geojson: str):
    try:
        import geopandas as gpd
        from shapely.geometry import shape
    except ImportError as exc:
        raise HTTPException(
            status_code=500,
            detail="GeoPandas and Shapely are required to prepare MajiScope model files.",
        ) from exc

    try:
        payload = json.loads(raw_geojson) if raw_geojson else {"type": "FeatureCollection", "features": []}
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail="Reported leak GeoJSON is invalid.") from exc

    features = payload.get("features") if isinstance(payload, dict) else []
    rows: list[dict[str, Any]] = []
    for feature in features or []:
        geometry = feature.get("geometry") if isinstance(feature, dict) else None
        if not geometry:
            continue
        props = feature.get("properties") or {}
        rows.append({**props, "geometry": shape(geometry)})

    if not rows:
        return gpd.GeoDataFrame({"geometry": []}, geometry="geometry", crs="EPSG:4326")

    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")
    return gdf[gdf.geometry.notna()].copy()


async def _write_upload(upload: UploadFile, directory: str, name: str) -> str | None:
    if upload is None:
        return None
    content = await upload.read()
    if not content:
        return None
    path = os.path.join(directory, name)
    with open(path, "wb") as handle:
        handle.write(content)
    return path


@router.post("/validate-gpkg", response_model=MajiScopeGpkgValidation)
async def validate_majiscope_gpkg(
    dma_id: str = Form(...),
    dma_name: str = Form(...),
    dma_geojson: str = Form(...),
    pipe_network: UploadFile | None = File(None),
    water_sources: UploadFile | None = File(None),
    storage_facilities: UploadFile | None = File(None),
    valves: UploadFile | None = File(None),
    bulk_meters: UploadFile | None = File(None),
    _: str = Depends(require_api_key),
):
    boundary_layer = _boundary_gdf(dma_geojson, dma_id, dma_name)
    boundary = boundary_layer.geometry.iloc[0]
    feature_counts: dict[str, int] = {"dma": 1}

    with tempfile.TemporaryDirectory(prefix="majiscope_validate_") as tmpdir:
        uploads: dict[str, UploadFile | None] = {
            "pipe_network": pipe_network,
            "water_sources": water_sources,
            "storage_facilities": storage_facilities,
            "valves": valves,
            "bulk_meters": bulk_meters,
        }

        for asset_type, upload in uploads.items():
            if upload is None:
                continue

            asset_path = await _write_upload(upload, tmpdir, f"{asset_type}.gpkg")
            if not asset_path:
                continue

            layer_name = _ASSET_LAYER_MAP[asset_type]
            filtered = _filter_to_boundary(asset_path, boundary)
            feature_counts[layer_name] = int(len(filtered))

    return MajiScopeGpkgValidation(
        feature_counts=feature_counts,
        message="DMA-scoped hydraulic model assets validated.",
    )


@router.post("/prepare-gpkg", response_model=MajiScopePreparedGpkg, status_code=201)
async def prepare_majiscope_gpkg(
    session_id: str = Form(...),
    launch_token: str = Form(...),
    expires_at: str = Form(...),
    utility_id: str = Form(...),
    dma_id: str = Form(...),
    dma_name: str = Form(...),
    dma_geojson: str = Form(...),
    pipe_network: UploadFile = File(...),
    water_sources: UploadFile | None = File(None),
    storage_facilities: UploadFile | None = File(None),
    valves: UploadFile | None = File(None),
    bulk_meters: UploadFile | None = File(None),
    reported_leaks_geojson: str | None = Form(None),
    _: str = Depends(require_api_key),
):
    settings = get_settings()
    os.makedirs(settings.gpkg_dir, exist_ok=True)
    if not settings.majiscope_launch_secret:
        raise HTTPException(status_code=500, detail="MajiScope launch secret is not configured.")

    boundary_layer = _boundary_gdf(dma_geojson, dma_id, dma_name)
    boundary = boundary_layer.geometry.iloc[0]
    output_name = f"majiscope_{_safe_stem(session_id)}.gpkg"
    output_path = os.path.join(settings.gpkg_dir, output_name)
    metadata_path = os.path.join(settings.gpkg_dir, f"majiscope_{_safe_stem(session_id)}.json")

    if os.path.exists(output_path):
        os.remove(output_path)
    if os.path.exists(metadata_path):
        os.remove(metadata_path)

    feature_counts: dict[str, int] = {}
    written_layers: list[str] = []

    with tempfile.TemporaryDirectory(prefix="majiscope_gpkg_") as tmpdir:
        boundary_layer.to_file(output_path, layer="dma", driver="GPKG")
        feature_counts["dma"] = 1
        written_layers.append("dma")

        uploads: dict[str, UploadFile | None] = {
            "pipe_network": pipe_network,
            "water_sources": water_sources,
            "storage_facilities": storage_facilities,
            "valves": valves,
            "bulk_meters": bulk_meters,
        }

        for asset_type, upload in uploads.items():
            if upload is None:
                continue

            asset_path = await _write_upload(upload, tmpdir, f"{asset_type}.gpkg")
            if not asset_path:
                continue

            layer_name = _ASSET_LAYER_MAP[asset_type]
            filtered = _filter_to_boundary(asset_path, boundary)
            count = int(len(filtered))
            feature_counts[layer_name] = count

            if count <= 0:
                if asset_type == "pipe_network":
                    raise HTTPException(
                        status_code=422,
                        detail="No pipe network features intersect the selected DMA boundary.",
                    )
                continue

            filtered.to_file(output_path, layer=layer_name, driver="GPKG")
            written_layers.append(layer_name)

        if reported_leaks_geojson:
            leaks_gdf = _geojson_to_gdf(reported_leaks_geojson)
            if not leaks_gdf.empty:
                if leaks_gdf.crs is None:
                    leaks_gdf = leaks_gdf.set_crs("EPSG:4326")
                else:
                    leaks_gdf = leaks_gdf.to_crs("EPSG:4326")
                leaks_gdf = leaks_gdf[leaks_gdf.geometry.intersects(_boundary_with_tolerance(boundary))].copy()
            leak_count = int(len(leaks_gdf)) if not leaks_gdf.empty else 0
            feature_counts[_REPORTED_LEAKS_LAYER] = leak_count
            if leak_count > 0:
                leaks_gdf.to_file(output_path, layer=_REPORTED_LEAKS_LAYER, driver="GPKG")
                written_layers.append(_REPORTED_LEAKS_LAYER)
        else:
            feature_counts[_REPORTED_LEAKS_LAYER] = 0

    if feature_counts.get("waterpipes", 0) <= 0:
        raise HTTPException(status_code=422, detail="Prepared model has no water pipe features.")
    if feature_counts.get("watersources", 0) <= 0 and feature_counts.get("storagefacility", 0) <= 0:
        raise HTTPException(
            status_code=422,
            detail="Prepared model needs at least one water source or storage facility inside the DMA boundary.",
        )

    with open(metadata_path, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "session_id": session_id,
                "filename": output_name,
                "utility_id": utility_id,
                "dma_id": dma_id,
                "dma_name": dma_name,
                "launch_token_hash": _hash_launch_token(settings.majiscope_launch_secret, launch_token),
                "expires_at": expires_at,
                "layers": written_layers,
                "feature_counts": feature_counts,
            },
            handle,
            indent=2,
        )

    return MajiScopePreparedGpkg(
        filename=output_name,
        layers=written_layers,
        feature_counts=feature_counts,
        message="Temporary hydraulic model GeoPackage prepared.",
    )


@router.post("/cleanup/{session_id}", response_model=MajiScopeCleanupResult)
async def cleanup_majiscope_session(
    session_id: str,
    request: Request,
    _: str = Depends(require_api_key),
):
    settings = get_settings()
    stem = _safe_stem(session_id)
    metadata_path = os.path.join(settings.gpkg_dir, f"majiscope_{stem}.json")
    gpkg_path = os.path.join(settings.gpkg_dir, f"majiscope_{stem}.gpkg")
    removed: list[str] = []

    for path in (gpkg_path, metadata_path):
        if os.path.isfile(path):
            os.remove(path)
            removed.append(os.path.basename(path))

    if settings.majiscope_backend_url and settings.majiscope_callback_secret:
        try:
            import httpx

            async with httpx.AsyncClient(timeout=15.0) as client:
                await client.post(
                    settings.majiscope_backend_url.rstrip("/") + f"/api/hydraulic-model/sessions/{session_id}/cleanup",
                    headers={"X-MajiScope-Hydraulic-Secret": settings.majiscope_callback_secret},
                )
        except Exception:
            pass

    return MajiScopeCleanupResult(
        session_id=session_id,
        removed_files=removed,
        message="Temporary hydraulic model files cleaned.",
    )


def cleanup_expired_majiscope_files() -> int:
    settings = get_settings()
    if not os.path.isdir(settings.gpkg_dir):
        return 0

    now = datetime.now(timezone.utc)
    removed = 0
    for filename in os.listdir(settings.gpkg_dir):
        if not filename.startswith("majiscope_") or not filename.endswith(".json"):
            continue

        metadata_path = os.path.join(settings.gpkg_dir, filename)
        try:
            with open(metadata_path, "r", encoding="utf-8") as handle:
                metadata = json.load(handle)
            raw_expires_at = metadata.get("expires_at")
            if not raw_expires_at:
                continue
            expires_at = datetime.fromisoformat(str(raw_expires_at).replace("Z", "+00:00"))
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at > now:
                continue
            gpkg_name = metadata.get("filename")
            if gpkg_name:
                gpkg_path = os.path.join(settings.gpkg_dir, str(gpkg_name))
                if os.path.isfile(gpkg_path):
                    os.remove(gpkg_path)
                    removed += 1
            os.remove(metadata_path)
            removed += 1
        except Exception:
            continue
    return removed
