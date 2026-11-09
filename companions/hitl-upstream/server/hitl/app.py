"""WSGI application: routing, static client files, HTML shells.

All rendering of request content happens client-side after decryption; the shells only load
the client files, each with its integrity value.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import mimetypes
import os
import re
import sys
import time

from . import api
from .http import Request, Response, json_out
from .lib import HttpError

log = logging.getLogger("hitl")
PUBLIC = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "public"))

TOK = r"([A-Za-z0-9_-]{16,64})"  # enrolment tokens and short links
RID = r"([a-z0-9._-]{1,64})"
ROUTES = [
    ("GET", r"/", lambda r: page_home()),
    ("GET", r"/enroll", lambda r: page_app("enrol-sso", "sso")),
    ("GET", r"/api/v1/enroll-sso", api.sso_enrol),
    ("GET", rf"/enroll/{TOK}", lambda r, t: page_app("enrol", t)),
    ("GET", r"/r/([A-Za-z0-9_-]{16})", lambda r, s: page_app("decide", s)),
    ("GET", rf"/api/v1/enroll/{TOK}", api.enrol_get),
    ("POST", rf"/api/v1/enroll/{TOK}", api.enrol_finish),
    ("GET", rf"/api/v1/recipients/{RID}/keys", api.recipient_keys),
    ("GET", rf"/api/v1/recipients/{RID}/statements", api.recipient_statements),
    ("GET", r"/inbox", lambda r: page_app("inbox", "inbox")),
    ("POST", r"/api/v1/inbox", api.inbox),
    ("GET", rf"/api/v1/recipients/{RID}/unlock", api.recipient_unlock),
    ("PUT", r"/api/v1/blobs", api.blob_put),
    ("GET", r"/api/v1/blobs/([A-Za-z0-9_-]{24})", api.blob_get),
    ("POST", r"/api/v1/requests", api.request_create),
    ("GET", r"/api/v1/requests/([A-Za-z0-9_-]{16,36})", api.request_get),
    ("POST", rf"/api/v1/requests/({api.UUID})/response", api.request_respond),
]
ROUTES = [(m, re.compile(p), h) for m, p, h in ROUTES]


def application(environ: dict, start_response):
    req = Request(environ)
    try:
        res = static(req) or route(req)
    except HttpError as e:
        res = json_out({"error": str(e)}, e.status)
    except Exception:
        log.exception("hitl: %s %s", req.method, req.path)
        res = json_out({"error": "internal error"}, 500)
    # No response is ever sniffed into a type the service did not declare.
    res.headers.append(("X-Content-Type-Options", "nosniff"))
    return res(environ, start_response)


def route(req: Request) -> Response:
    method = "GET" if req.method == "HEAD" else req.method
    for m, pattern, handler in ROUTES:
        if m == method:
            x = pattern.fullmatch(req.path)
            if x:
                return handler(req, *x.groups())
    raise HttpError("not found", 404)


# ---------------------------------------------------------------- static files

TYPES = {".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css", ".json": "application/json",
         ".html": "text/html; charset=utf-8", ".wasm": "application/wasm", ".map": "application/json"}


def static(req: Request) -> Response | None:
    """Files under public/, served as they are (what a web server in front would do)."""
    if req.method not in ("GET", "HEAD") or req.path == "/":
        return None
    path = os.path.realpath(os.path.join(PUBLIC, req.path.lstrip("/")))
    if not path.startswith(PUBLIC + os.sep):
        return None
    if os.path.isdir(path):
        if not req.path.endswith("/"):
            if os.path.isfile(os.path.join(path, "index.html")):
                return Response(301, [("Location", req.path + "/")])
            return None
        path = os.path.join(path, "index.html")
    if not os.path.isfile(path):
        return None
    ext = os.path.splitext(path)[1].lower()
    ctype = TYPES.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream"
    st = os.stat(path)
    etag = '"%x-%x"' % (st.st_mtime_ns, st.st_size)
    headers = [("Content-Type", ctype), ("ETag", etag),
               ("Last-Modified", time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(st.st_mtime)))]
    if req.header("If-None-Match") == etag:
        return Response(304, headers)
    return Response.file(path, headers + [("Content-Length", str(st.st_size))])


# ---------------------------------------------------------------- HTML shells

_assets: dict[str, tuple[tuple[int, int], tuple[str, str]]] = {}


def asset_info(path: str) -> tuple[str, str]:
    """A URL carrying a content hash (never a stale cache after a deploy) and a Subresource
    Integrity value (the browser refuses a file that does not match). The same SRI values are
    listed in the release manifest, see tools/release.py."""
    full = PUBLIC + path
    st = os.stat(full)
    key = (st.st_mtime_ns, st.st_size)
    hit = _assets.get(path)
    if hit and hit[0] == key:
        return hit[1]
    with open(full, "rb") as f:
        body = f.read()
    info = (path + "?v=" + hashlib.sha256(body).hexdigest()[:12],
            "sha384-" + base64.b64encode(hashlib.sha384(body).digest()).decode())
    _assets[path] = (key, info)
    return info


def h(s: str) -> str:
    """Escapes text for HTML, both kinds of quote included."""
    return s.replace("&", "&amp;").replace('"', "&quot;").replace("'", "&#039;").replace("<", "&lt;").replace(">", "&gt;")


def asset(path: str, attr: str) -> str:
    url, sri = asset_info(path)
    return f'{attr}="{h(url)}" integrity="{sri}"'


# Inline styles are allowed so HTML content blocks keep their design. The page itself never inserts
# caller HTML (all of it goes into sandboxed frames, which inherit this policy), and remote images,
# fonts and fetches stay blocked, so a design can neither run code nor call home. frame-ancestors:
# the decision page is never shown inside another site, where it could be covered or clicked through.
CSP = ("default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; font-src 'self' data:; "
       "img-src 'self' data: blob:; frame-src 'self' blob:; worker-src 'self' blob:; media-src 'self' blob:; "
       "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def page(html: str) -> Response:
    body = html.encode()
    return Response(200, [("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store"),
                          ("Content-Security-Policy", CSP), ("Content-Length", str(len(body)))], body)


def page_head(title: str) -> str:
    css = asset("/assets/app.css", "href")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{h(title)}</title>
<link rel="stylesheet" {css}>
</head>
"""


def page_home() -> Response:
    return page(page_head("hitl")
                + "<body><main><h1>hitl</h1><p>Human decision service. Open the link you were sent.</p>"
                + "<p>The code this page runs is listed, file by file with its hash, in the "
                + "<a href=\"/assets/release/manifest.json\">release manifest</a>. "
                + "tools/verify_deployment.py in the source compares what is served here with a checkout.</p></main></body></html>\n")


def page_app(name: str, key: str) -> Response:
    title = {"decide": "Decision requested", "inbox": "Pending requests"}.get(name, "Enrol a device")
    sodium = asset("/assets/vendor/libsodium.js", "src")
    wrappers = asset("/assets/vendor/libsodium-wrappers.js", "src")
    js = asset("/assets/app.js", "src")
    # pdf.js loads only when a request contains a PDF; the module is preloaded with its SRI value
    # and the worker is fetched with its SRI value by app.js.
    pdf = asset("/assets/vendor/pdfjs/pdf.min.mjs", "href")
    pdf_url, _ = asset_info("/assets/vendor/pdfjs/pdf.min.mjs")
    worker_url, worker_sri = asset_info("/assets/vendor/pdfjs/pdf.worker.min.mjs")
    return page(page_head(title) + f"""<link rel="modulepreload" {pdf}>
<body data-page="{h(name)}" data-key="{h(key)}" data-pdfjs="{h(pdf_url)}" data-pdfjs-worker="{h(worker_url)}" data-pdfjs-worker-sri="{worker_sri}">
<main id="app"><p id="status">Loading...</p></main>
<script {sodium}></script>
<script {wrappers}></script>
<script {js}></script>
</body>
</html>
""")


logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
