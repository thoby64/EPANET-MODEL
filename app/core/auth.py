# app/core/auth.py
"""
API-key authentication.

Every protected endpoint declares:
    _ = Depends(require_api_key)

The caller must send:
    X-API-Key: <key>

Keys are read from the API_KEYS environment variable (comma-separated).
"""

from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader
import hashlib
import json
import os

from app.core.config import get_settings

API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)
LAUNCH_TOKEN_HEADER = APIKeyHeader(name="X-MajiScope-Launch-Token", auto_error=False)
SESSION_ID_HEADER = APIKeyHeader(name="X-MajiScope-Session-Id", auto_error=False)


def _hash_launch_token(secret: str, token: str) -> str:
    return hashlib.sha256(f"{secret}:{token}".encode("utf-8")).hexdigest()


def _launch_metadata(session_id: str | None, launch_token: str | None) -> dict | None:
    settings = get_settings()
    if not session_id or not launch_token or not settings.majiscope_launch_secret:
        return None

    metadata_path = os.path.join(settings.gpkg_dir, f"majiscope_{session_id}.json")
    if not os.path.isfile(metadata_path):
        return None

    try:
        with open(metadata_path, "r", encoding="utf-8") as handle:
            metadata = json.load(handle)
    except Exception:
        return None

    expected_hash = metadata.get("launch_token_hash")
    if expected_hash and expected_hash == _hash_launch_token(settings.majiscope_launch_secret, launch_token):
        return metadata
    return None


def require_api_key(
    request: Request,
    api_key: str = Security(API_KEY_HEADER),
    launch_token: str = Security(LAUNCH_TOKEN_HEADER),
    session_id: str = Security(SESSION_ID_HEADER),
) -> str:
    settings = get_settings()
    if api_key and api_key in settings.api_key_list:
        return api_key

    metadata = _launch_metadata(session_id, launch_token)
    if metadata:
        path = request.url.path
        filename = str(metadata.get("filename") or "")
        if path.startswith("/files/upload") or request.method == "DELETE":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="MajiScope launch tokens cannot upload or delete hydraulic service data.",
            )
        if path.startswith("/dma/") and filename and f"/dma/{filename}" not in path:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="MajiScope launch token is valid only for its prepared DMA model file.",
            )
        return f"majiscope:{session_id}"

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing API key. "
               "Provide a valid X-API-Key header or MajiScope launch token.",
    )
