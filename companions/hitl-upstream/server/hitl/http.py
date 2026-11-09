"""Minimal WSGI request and response types."""

from __future__ import annotations

from urllib.parse import parse_qsl

from .lib import json_text

REASONS = {200: "OK", 201: "Created", 301: "Moved Permanently", 304: "Not Modified", 400: "Bad Request",
           403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed", 409: "Conflict", 410: "Gone",
           413: "Payload Too Large", 500: "Internal Server Error"}


class Request:
    def __init__(self, environ: dict):
        self.environ = environ
        self.method = environ.get("REQUEST_METHOD", "GET").upper()
        self.path = environ.get("PATH_INFO", "/") or "/"
        self.query = dict(parse_qsl(environ.get("QUERY_STRING", ""), keep_blank_values=True))
        try:
            self.length = max(0, int(environ.get("CONTENT_LENGTH") or 0))
        except ValueError:
            self.length = 0

    def header(self, name: str) -> str:
        key = name.upper().replace("-", "_")
        if key in ("CONTENT_TYPE", "CONTENT_LENGTH"):
            return self.environ.get(key, "")
        return self.environ.get("HTTP_" + key, "")

    def read(self, limit: int) -> bytes | None:
        """The whole body, or None when it is larger than limit."""
        if self.chunked():
            data = self.environ["wsgi.input"].read(limit + 1)
            return None if len(data) > limit else data
        if self.length > limit:
            return None
        return self.environ["wsgi.input"].read(self.length) if self.length else b""

    def chunked(self) -> bool:
        return "chunked" in self.header("Transfer-Encoding").lower()

    def stream_chunks(self, size: int):
        left = float("inf") if self.chunked() else self.length
        stream = self.environ["wsgi.input"]
        while left > 0:
            buf = stream.read(min(size, left))
            if not buf:
                break
            left -= len(buf)
            yield buf


class Response:
    def __init__(self, status: int = 200, headers: list[tuple[str, str]] | None = None, body: bytes = b""):
        self.status = status
        self.headers = headers or []
        self.body = body
        self.path: str | None = None

    @classmethod
    def file(cls, path: str, headers: list[tuple[str, str]]) -> "Response":
        r = cls(200, headers)
        r.path = path
        return r

    def __call__(self, environ: dict, start_response):
        start_response(f"{self.status} {REASONS.get(self.status, 'Status')}", self.headers)
        if environ.get("REQUEST_METHOD") == "HEAD":
            return [b""]
        if self.path is not None:
            f = open(self.path, "rb")
            wrapper = environ.get("wsgi.file_wrapper")
            return wrapper(f, 1 << 20) if wrapper else iter(lambda: f.read(1 << 20), b"")
        return [self.body]


def json_out(data: dict, status: int = 200) -> Response:
    body = json_text(data).encode("utf-8", "surrogatepass")
    return Response(status, [("Content-Type", "application/json"), ("Cache-Control", "no-store"),
                             ("Content-Length", str(len(body)))], body)
