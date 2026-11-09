"""API key authentication for Brin HTTP endpoints."""

import os
from pathlib import Path

from fastapi import Depends, HTTPException, Security
from fastapi.security import APIKeyHeader

_KEY_FILE = Path("/etc/smart-node/secrets/brin-api-key")

# Load key once at import time (fail-fast)
_api_key = os.environ.get("BRIN_API_KEY") or (
    _KEY_FILE.read_text().strip() if _KEY_FILE.exists() else None
)
if not _api_key:
    raise RuntimeError(
        "BRIN_API_KEY env or /etc/smart-node/secrets/brin-api-key required"
    )

_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_api_key(key: str = Security(_header)) -> str:
    """FastAPI dependency that validates the X-API-Key header."""
    if not key or key != _api_key:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    return key
