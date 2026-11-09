"""Google account connect (OAuth) for the node.

The owner connects ONE Google account; the agent later acts as it (drive.file +
docs + sheets) to read/create/edit Google docs. We run the OAuth flow ourselves
(the registered redirect URI points here); the MCP connector consumes the stored
refresh token. Client creds + the refresh token live in the node sops store
(reused from routes.secrets); nothing is logged.
"""
import base64
import os
import hashlib
import hmac
import json
import logging
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from auth import get_actor
from db import cursor
from events_emit import emit
from lib import secret_store
from permissions import requires
from routes.secrets import _trusted

log = logging.getLogger("core-api.google")
router = APIRouter(prefix="/api/integrations/google", tags=["google"])

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"

# Derived from the node's OWN domain (Environment=SMART_NODE_DOMAIN, set by
# core-api.sh). The panel host convention is panel.<domain> (core-api is routed
# at panel.<domain>/api/* -- see derive_panel_host in core-api.sh). The operator
# MUST register this exact redirect URI in their Google Cloud console.
_NODE_DOMAIN = os.environ.get("SMART_NODE_DOMAIN") or "smart-node.local"
REDIRECT_URI = (os.environ.get("SN_GOOGLE_REDIRECT_URI")
                # panel.<domain> on purpose: this is the address registered with
                # Google on the nodes in use. The admin console moved to
                # admin.<domain> and panel.<domain> redirects there with
                # the path and query, so the callback still lands; changing it
                # here means registering the new one with Google first.
                # A node under .localhost is served on plain http (lib.sh web_local).
                or f"{'http' if _NODE_DOMAIN == 'localhost' or _NODE_DOMAIN.endswith('.localhost') else 'https'}"
                   f"://panel.{_NODE_DOMAIN}/api/integrations/google/callback")
SCOPE = (
    "openid email profile "
    "https://www.googleapis.com/auth/drive.file "
    "https://www.googleapis.com/auth/documents "
    "https://www.googleapis.com/auth/spreadsheets "
    "https://www.googleapis.com/auth/calendar.events "
    "https://www.googleapis.com/auth/calendar.readonly"
)


def _post_form(url: str, data: dict) -> dict:
    req = urllib.request.Request(url, data=urlencode(data).encode(), method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def _get_json(url: str, bearer: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {bearer}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def _google() -> dict:
    """The google.* leaves of the node secret store: client creds and connection."""
    try:
        with cursor() as cur:
            return secret_store.get_namespace(cur, "google")
    except secret_store.StoreUnavailable as e:
        log.error("secret store unavailable: %s", e)
        raise HTTPException(503, "secrets-store unavailable")


def _require_client() -> tuple[str, str]:
    g = _google()
    cid, cs = g.get("client_id"), g.get("client_secret")
    if not cid or not cs:
        raise HTTPException(503, "google client not configured (set google.client_id/client_secret)")
    return cid, cs


def _store_set(ref: str, value: str) -> None:
    """Persist one google.* value in the node secret store, journalled in the
    same transaction, so the token and its audit line land together or not at all."""
    try:
        with cursor(commit=True) as cur:
            replaced = secret_store.set_value(cur, ref, value, "component:core-api", note="google oauth")
            emit(cur, actor="component:core-api", event_type="secret.set",
                 entity_type="secret", entity_id=ref, payload={"via": "google-oauth", "replaced": replaced})
    except secret_store.StoreUnavailable as e:
        log.error("secret store unavailable: %s", e)
        raise HTTPException(503, "secrets-store unavailable")
    except Exception as e:  # noqa: BLE001
        log.error("store set failed for %s: %s", ref, e)
        raise HTTPException(500, "could not persist token")


def _sign_state() -> str:
    _, cs = _require_client()
    data = f"{int(time.time())}|{base64.urlsafe_b64encode(hashlib.sha256(str(time.time()).encode()).digest()[:9]).decode()}"
    sig = hmac.new(cs.encode(), data.encode(), hashlib.sha256).hexdigest()[:32]
    return base64.urlsafe_b64encode(f"{data}|{sig}".encode()).decode()


def _verify_state(state: str, max_age: int = 900) -> bool:
    try:
        _, cs = _require_client()
        raw = base64.urlsafe_b64decode(state.encode()).decode()
        ts, nonce, sig = raw.split("|")
        expected = hmac.new(cs.encode(), f"{ts}|{nonce}".encode(), hashlib.sha256).hexdigest()[:32]
        return hmac.compare_digest(sig, expected) and (time.time() - int(ts) <= max_age)
    except Exception:
        return False


@router.get("/connect", dependencies=[Depends(requires("google.oauth.connect"))])
def connect():
    """Start the OAuth dance: redirect the owner's browser to Google consent."""
    cid, _ = _require_client()
    url = AUTH_ENDPOINT + "?" + urlencode({
        "client_id": cid,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": _sign_state(),
    })
    return RedirectResponse(url, status_code=302)


def _ok_page(email: str) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8><title>Google connected</title>"
        "<body style='font:15px system-ui;padding:40px;color:#111'>"
        f"<h2>Google account connected</h2><p>Connected as <b>{email}</b>. "
        "You can close this tab.</p></body>")


def _err_page(msg: str) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8><title>Google connect failed</title>"
        "<body style='font:15px system-ui;padding:40px;color:#111'>"
        f"<h2>Could not connect Google</h2><p>{msg}</p></body>", status_code=400)


@router.get("/callback", dependencies=[Depends(requires("google.oauth.connect"))])
def callback(code: str = "", state: str = "", error: str = ""):
    """Google redirects here after consent. Exchange the code for a refresh
    token, record the connected account, store the token in sops."""
    if error:
        return _err_page(f"Google returned: {error}")
    if not code or not _verify_state(state):
        return _err_page("invalid or expired request (state check failed)")
    cid, cs = _require_client()
    try:
        tok = _post_form(TOKEN_ENDPOINT, {
            "code": code, "client_id": cid, "client_secret": cs,
            "redirect_uri": REDIRECT_URI, "grant_type": "authorization_code"})
        refresh = tok.get("refresh_token")
        access = tok.get("access_token")
        if not refresh:
            return _err_page("Google did not return a refresh token; remove the app "
                             "under Google Account access and retry.")
        email = ""
        try:
            email = (_get_json(USERINFO_ENDPOINT, access) or {}).get("email", "")
        except Exception:
            pass
    except Exception as e:
        log.warning("google token exchange failed: %s", e)
        return _err_page("token exchange failed")
    _store_set("google.refresh_token", refresh)
    _store_set("google.connected_email", email)
    log.info("google connected: %s", email)
    return _ok_page(email or "your account")


@router.get("/status", dependencies=[Depends(requires("google.read"))])
def status():
    g = _google()
    return {
        "client_configured": bool(g.get("client_id") and g.get("client_secret")),
        "connected": bool(g.get("refresh_token")),
        "email": g.get("connected_email") or "",
        "redirect_uri": REDIRECT_URI,
    }


_token_cache = {"access": "", "exp": 0.0}
DRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3"
_MP = "smartnodeMP"


def _mint_access_token() -> str:
    """Mint (and cache) an access token for the node's connected Google account."""
    g = _google()
    rt = g.get("refresh_token")
    if not rt:
        raise HTTPException(409, "no Google account connected")
    now = time.time()
    if _token_cache["access"] and _token_cache["exp"] - now > 60:
        return _token_cache["access"]
    cid, cs = _require_client()
    try:
        tok = _post_form(TOKEN_ENDPOINT, {
            "refresh_token": rt, "client_id": cid, "client_secret": cs,
            "grant_type": "refresh_token"})
    except Exception as e:
        log.warning("google token refresh failed: %s", e)
        raise HTTPException(502, "token refresh failed")
    at = tok.get("access_token")
    if not at:
        raise HTTPException(502, "no access token from refresh")
    _token_cache["access"] = at
    _token_cache["exp"] = now + int(tok.get("expires_in", 3600))
    return at


@router.get("/token", dependencies=[Depends(requires("google.read"))])
def token(request: Request):
    """Fresh access token for the connected account. Loopback/docker0 only."""
    if not _trusted(request):
        raise HTTPException(404, "not found")
    return {"access_token": _mint_access_token()}


@router.get("/picker-config", dependencies=[Depends(requires("google.read"))])
def picker_config(request: Request):
    """Browser config for the Google Picker (owner-gated). client_id + api_key are
    not secrets (client_id is public; the key is referrer-locked to the node's own domain).
    app_id is the project number (the client_id prefix)."""
    get_actor(request)
    g = _google()
    cid = g.get("client_id") or ""
    return {"client_id": cid, "api_key": g.get("picker_api_key") or "",
            "app_id": cid.split("-")[0] if "-" in cid else ""}


class DocCreate(BaseModel):
    title: str
    content: str = ""


@router.post("/docs", dependencies=[Depends(requires("google.docs.create"))])
def create_doc(payload: DocCreate, request: Request):
    """Create a Google Doc (markdown -> Doc) in the connected account's designated
    folder, on behalf of the owner from the Studio UI. Returns {id, link, name}."""
    get_actor(request)  # owner-gated: requires a Remote-User
    title = payload.title.strip()
    if not title:
        raise HTTPException(400, "title required")
    at = _mint_access_token()
    meta = {"name": title, "mimeType": "application/vnd.google-apps.document"}
    folder = _google().get("docs_folder_id")
    if folder:
        meta["parents"] = [folder]
    body = (f"--{_MP}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(meta)}\r\n--{_MP}\r\nContent-Type: text/markdown\r\n\r\n").encode() \
        + (payload.content or "").encode("utf-8") + f"\r\n--{_MP}--".encode()
    req = urllib.request.Request(
        f"{DRIVE_UPLOAD}/files?uploadType=multipart&supportsAllDrives=true",
        data=body, method="POST",
        headers={"Authorization": f"Bearer {at}",
                 "Content-Type": f"multipart/related; boundary={_MP}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            j = json.loads(r.read())
    except urllib.error.HTTPError as e:
        log.warning("drive create failed: %s", e)
        raise HTTPException(502, f"drive create failed ({e.code})")
    fid = j.get("id")
    return {"id": fid, "name": title,
            "link": f"https://docs.google.com/document/d/{fid}/edit"}


# ---- Calendar (server-side, loopback only) -------------------------------------
# HTTP path to the connected account's calendar for components that run OUTSIDE the
# agent's MCP loop: the booking flow's check block (a deterministic logic step, no
# tool loop) and the Runner's approval_tick (creates the event on owner approval).
# Mirrors the google-calendar MCP connector's calls; same account, same scopes
# (calendar.events + calendar.readonly). Owner-gated (Remote-User), like /docs;
# the internal callers (dispatcher logic block, runner) pass Remote-User: admin.
CAL = "https://www.googleapis.com/calendar/v3"


def _cal_api(method: str, url: str, body: dict | None = None) -> dict:
    at = _mint_access_token()
    headers = {"Authorization": f"Bearer {at}"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        log.warning("calendar API %s failed: %s %s", url, e.code, detail)
        raise HTTPException(502, f"calendar API {e.code}")


class FreeBusyReq(BaseModel):
    time_min: str
    time_max: str
    calendar_id: str = "primary"


@router.post("/calendar/freebusy", dependencies=[Depends(requires("google.calendar.read"))])
def calendar_freebusy(payload: FreeBusyReq, request: Request):
    """Busy intervals on the connected calendar in [time_min, time_max] (RFC3339).
    Owner-gated (Remote-User), like /docs. Returns {"busy": [{"start","end"}, ...]}."""
    get_actor(request)
    d = _cal_api("POST", f"{CAL}/freeBusy", {
        "timeMin": payload.time_min, "timeMax": payload.time_max,
        "items": [{"id": payload.calendar_id}]})
    busy = ((d.get("calendars") or {}).get(payload.calendar_id) or {}).get("busy") or []
    return {"busy": busy}


class EventReq(BaseModel):
    summary: str
    start: str
    end: str
    calendar_id: str = "primary"
    description: str = ""
    location: str = ""


@router.post("/calendar/events", dependencies=[Depends(requires("google.calendar.write"))])
def calendar_create_event(payload: EventReq, request: Request):
    """Create an event on the connected calendar. start/end are RFC3339 (with offset).
    Owner-gated (Remote-User), like /docs. Returns {"id","htmlLink"}."""
    get_actor(request)
    body = {"summary": payload.summary,
            "start": {"dateTime": payload.start},
            "end": {"dateTime": payload.end}}
    if payload.description:
        body["description"] = payload.description
    if payload.location:
        body["location"] = payload.location
    d = _cal_api("POST", f"{CAL}/calendars/{quote(payload.calendar_id)}/events", body)
    return {"id": d.get("id"), "htmlLink": d.get("htmlLink")}
