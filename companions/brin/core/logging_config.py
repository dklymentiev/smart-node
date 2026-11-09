"""Structured JSON logging and correlation middleware for Brin."""

import contextvars
import json
import logging
import os
import time
import uuid
from pathlib import Path

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# Correlation ID propagation
correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default="-"
)

LOG_DIR = Path(__file__).parent.parent / "logs"
SERVICE_NAME = "brin"


class JSONFormatter(logging.Formatter):
    """Emit each log record as a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "service": SERVICE_NAME,
            "correlation_id": correlation_id_var.get("-"),
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Merge structured extras
        for key in ("action", "duration_ms", "status", "method", "path"):
            val = getattr(record, key, None)
            if val is not None:
                entry[key] = val
        if record.exc_info and record.exc_info[0]:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


class CorrelationMiddleware(BaseHTTPMiddleware):
    """Reads or generates X-Correlation-ID, logs request lifecycle."""

    async def dispatch(self, request: Request, call_next) -> Response:
        cid = request.headers.get("X-Correlation-ID") or uuid.uuid4().hex[:12]
        correlation_id_var.set(cid)

        logger = logging.getLogger(SERVICE_NAME)
        start = time.time()
        logger.info(
            "%s %s", request.method, request.url.path,
            extra={"action": "request_start", "method": request.method, "path": request.url.path},
        )

        response = await call_next(request)

        duration_ms = round((time.time() - start) * 1000, 1)
        logger.info(
            "%s %s -> %s (%.0fms)",
            request.method, request.url.path, response.status_code, duration_ms,
            extra={
                "action": "request_end",
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        response.headers["X-Correlation-ID"] = cid
        return response


def setup_logging() -> None:
    """Configure structured logging: file + console on 'brin' logger."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    formatter = JSONFormatter()

    file_handler = logging.FileHandler(LOG_DIR / "brin.log")
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    logger = logging.getLogger(SERVICE_NAME)
    logger.setLevel(logging.INFO)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    # Suppress noisy uvicorn access logs (we log requests ourselves)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
