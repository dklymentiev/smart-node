#!/usr/bin/env python3
"""
Configuration management for Mesh API
"""
import os
from typing import List
from dotenv import load_dotenv

load_dotenv()

def get_database_url() -> str:
    """Get PostgreSQL database URL from environment (superuser, for migrations)"""
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise ValueError("DATABASE_URL environment variable is required")
    return database_url


def get_app_database_url() -> str:
    """Get PostgreSQL URL for the application role (RLS enforced).

    Falls back to DATABASE_URL if DATABASE_URL_APP is not set, which means
    RLS will be bypassed (superuser ignores RLS).  A warning is logged.
    """
    url = os.getenv("DATABASE_URL_APP")
    if url:
        return url
    import logging
    logging.getLogger(__name__).warning(
        "DATABASE_URL_APP not set -- using superuser connection. "
        "RLS will NOT be enforced. Set DATABASE_URL_APP for production."
    )
    return get_database_url()


def get_app_role_password() -> str:
    """Password for the mesh_app database role (created during migration)."""
    return os.getenv("MESH_APP_PASSWORD", "")

def get_ip_whitelist() -> List[str]:
    """Get IP whitelist from environment (comma-separated CIDR ranges)
    Returns empty list if not set (allows all IPs)
    """
    whitelist = os.getenv("IP_WHITELIST", "")
    if not whitelist.strip():
        return []
    return [ip.strip() for ip in whitelist.split(",") if ip.strip()]

def get_cors_origins() -> List[str]:
    """Get CORS allowed origins from environment (comma-separated).
    Returns empty list if not set (denies cross-origin requests).
    Set CORS_ORIGINS=* to allow all origins (not recommended for production).
    """
    origins = os.getenv("CORS_ORIGINS", "")
    if not origins.strip():
        return []
    return [origin.strip() for origin in origins.split(",") if origin.strip()]

def get_api_host() -> str:
    """Get API host from environment"""
    return os.getenv("API_HOST", "0.0.0.0")

def get_api_port() -> int:
    """Get API port from environment"""
    return int(os.getenv("API_PORT", "8000"))

def get_embedding_model_name() -> str:
    """Get embedding model name from environment"""
    return os.getenv(
        "EMBEDDING_MODEL",
        "intfloat/multilingual-e5-base"
    )

def verify_baked_model() -> None:
    """Check that runtime EMBEDDING_MODEL matches the model baked into the image.
    Raises RuntimeError if they diverge (wrong model cached, would silently fail)."""
    sentinel = os.path.join(os.getenv("HF_HOME", ".cache"), ".baked-model-name")
    if not os.path.exists(sentinel):
        return  # no sentinel = local dev, skip check
    with open(sentinel) as f:
        baked = f.read().strip()
    runtime = get_embedding_model_name()
    if baked != runtime:
        raise RuntimeError(
            f"EMBEDDING_MODEL mismatch: image was built with '{baked}' "
            f"but runtime env requests '{runtime}'. "
            f"Rebuild the image with: docker compose build --build-arg EMBEDDING_MODEL={runtime}"
        )

def get_similarity_threshold() -> float:
    """Get similarity threshold for search from environment"""
    return float(os.getenv("SIMILARITY_THRESHOLD", "0.1"))

def get_max_content_length() -> int:
    """Get maximum content length for documents"""
    return int(os.getenv("MAX_CONTENT_LENGTH", "50000"))

def is_debug_enabled() -> bool:
    """Check if debug mode is enabled"""
    return os.getenv("DEBUG", "false").lower() in ("true", "1", "yes")

def get_log_level() -> str:
    """Get logging level from environment"""
    return os.getenv("LOG_LEVEL", "INFO").upper()

# Database connection settings
def get_db_pool_min_size() -> int:
    """Get minimum database pool size"""
    return int(os.getenv("DB_POOL_MIN_SIZE", "1"))

def get_db_pool_max_size() -> int:
    """Get maximum database pool size"""
    return int(os.getenv("DB_POOL_MAX_SIZE", "10"))

def is_auth_required() -> bool:
    """Whether API key authentication is required.
    When true, all requests (except /health) must include X-API-Key header."""
    return os.getenv("AUTH_REQUIRED", "true").lower() in ("true", "1", "yes")

def get_api_keys() -> List[str]:
    """Get valid API keys from environment (comma-separated).
    Returns empty list if not set."""
    keys = os.getenv("API_KEYS", "")
    if not keys.strip():
        return []
    return [k.strip() for k in keys.split(",") if k.strip()]

def get_rate_limit_search() -> int:
    """Max requests per minute for /search endpoint (0 = unlimited)"""
    return int(os.getenv("RATE_LIMIT_SEARCH", "60"))

def get_rate_limit_embed() -> int:
    """Max requests per minute for /embed endpoints (0 = unlimited)"""
    return int(os.getenv("RATE_LIMIT_EMBED", "30"))

def get_rate_limit_heavy() -> int:
    """Max requests per minute for CPU-intensive endpoints like /bulk, create, delete (0 = unlimited)"""
    return int(os.getenv("RATE_LIMIT_HEAVY", "30"))

def trust_proxy_headers() -> bool:
    """Whether to trust proxy headers (CF-Connecting-IP, X-Real-IP) for client IP.
    MUST be set to true only when behind a trusted reverse proxy (Cloudflare, Traefik).
    Default: false - uses direct connection IP only."""
    return os.getenv("TRUST_PROXY_HEADERS", "false").lower() in ("true", "1", "yes")
