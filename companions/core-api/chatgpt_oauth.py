"""Sign in with ChatGPT, for an open-source app that runs on the owner's own server.

OpenAI lets a local or self-hosted open-source app use the owner's ChatGPT plan for model
requests once the owner has signed in and agreed. This module is the protocol and nothing
else: it builds the sign-in address, reads the address the browser lands on, trades the
code for tokens, refreshes them, and checks the ID token. Where tokens are kept, who may
sign in and what is done with the plan are the callers' business (routes/gateway_secrets.py).

The callback is a loopback address (127.0.0.1) that, for a server, points at the owner's
own computer and not at the server. So the owner signs in in their browser, lands on a page
that does not load, and pastes that page's address back; read_landing() takes it from there.

Standard library only. The ID token's signature is checked here (RS256, against OpenAI's
published keys) so that no cryptography package has to join the node's venv for it.

Documented at developers.openai.com/siwc/token-sharing-open-source (registration, sign-in,
token reference, preview limitations). Nothing here is read from elsewhere.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

ISSUER = "https://auth.openai.com"
AUTHORIZE = ISSUER + "/api/accounts/authorize"
TOKEN = ISSUER + "/api/accounts/oauth/token"
DISCOVERY = ISSUER + "/.well-known/openid-configuration"
RESOURCE = "https://api.openai.com/v1"
REDIRECT = "http://127.0.0.1:1455/auth/callback"
SCOPE = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
FIRST_CLIENT = "dynamic_agent_client"       # asks OpenAI to register this host; never kept as the id
ATTEMPT_LIFE = 15 * 60                      # seconds a sign-in attempt may wait for the pasted address
CLOCK_SKEW = 120


class AuthError(Exception):
    """A sign-in problem whose message is fit to show the owner. It never holds a token."""


def new_host_id() -> str:
    """The node's stable, opaque id for OpenAI (urn:uuid, one of the three forms it accepts).
    Make it once, keep it, never derive it from the owner."""
    return "urn:uuid:" + str(uuid.uuid4())


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def start(host_id: str, *, app_name: str = "Smart Node", client_id: str | None = None,
          id_token_hint: str | None = None, login_hint: str | None = None, now: float | None = None):
    """Begin a sign-in. Returns (address_to_open, pending). `pending` is what read_landing and
    exchange need later; keep it server-side until the owner pastes the address back.

    client_id is None for the first sign-in of this host (OpenAI registers it and hands the
    issued id back in the callback) and the issued id for a later one."""
    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    q = [("client_id", client_id or FIRST_CLIENT)]
    if not client_id:
        q.append(("agent_name_hint", app_name))        # display name, first registration only
    q += [("ext_agent_host_id", host_id)]
    if client_id and id_token_hint:
        q.append(("id_token_hint", id_token_hint))
    if client_id and login_hint:
        q.append(("login_hint", login_hint))
    q += [("response_type", "code"), ("redirect_uri", REDIRECT), ("scope", SCOPE), ("resource", RESOURCE),
          ("state", state), ("nonce", nonce), ("code_challenge", challenge), ("code_challenge_method", "S256")]
    pending = {"verifier": verifier, "state": state, "nonce": nonce, "client_id": client_id,
               "host_id": host_id, "created": now if now is not None else time.time()}
    return AUTHORIZE + "?" + urllib.parse.urlencode(q, quote_via=urllib.parse.quote), pending


def read_landing(landing: str, pending: dict, *, now: float | None = None) -> dict:
    """Read the address the browser landed on (the loopback page that did not load).
    Returns {"code", "client_id", "scope"} or raises AuthError."""
    if (now if now is not None else time.time()) - float(pending.get("created", 0)) > ATTEMPT_LIFE:
        raise AuthError("this sign-in attempt has expired; start again")
    landing = (landing or "").strip()
    parts = urllib.parse.urlsplit(landing)
    if landing and not parts.query and "=" in landing:
        parts = urllib.parse.urlsplit("?" + landing.lstrip("?"))
    q = dict(urllib.parse.parse_qsl(parts.query))
    if parts.scheme and (parts.hostname, parts.path) != ("127.0.0.1", "/auth/callback"):
        raise AuthError("that is not the page the sign-in ended on (it should start with " + REDIRECT + ")")
    if q.get("error"):
        raise AuthError("sign-in was not completed: " + q["error"])
    if not q.get("state") or not hmac.compare_digest(q["state"], pending["state"]):
        raise AuthError("this address belongs to another sign-in attempt; start again")
    if not q.get("code"):
        raise AuthError("the address has no code in it; copy the whole address from the browser")
    issued = q.get("client_id") or pending.get("client_id") or ""
    if not issued or issued == FIRST_CLIENT:
        raise AuthError("OpenAI did not register this node, so the sign-in is incomplete; start again")
    if pending.get("client_id") and issued != pending["client_id"]:
        raise AuthError("the sign-in came back for a different registration; refusing it")
    return {"code": q["code"], "client_id": issued, "scope": q.get("scope", "")}


def _post_form(url: str, fields: dict, timeout: float = 30.0):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"error": "http_" + str(e.code)}
    except (urllib.error.URLError, OSError) as e:
        raise AuthError("could not reach OpenAI: " + str(getattr(e, "reason", e))[:120])


def _token_call(fields: dict) -> dict:
    status, body = _post_form(TOKEN, fields)
    if status != 200 or not body.get("access_token"):
        code = str(body.get("error") or "http_" + str(status))
        raise AuthError("OpenAI refused the request: " + code + (" (sign in again)" if code == "invalid_grant" else ""))
    return body


def exchange(code: str, client_id: str, verifier: str) -> dict:
    """Trade the code for tokens. Tokens are returned to the caller and never logged here."""
    return _token_call({"grant_type": "authorization_code", "client_id": client_id, "code": code,
                        "code_verifier": verifier, "redirect_uri": REDIRECT, "resource": RESOURCE})


def refresh(client_id: str, refresh_token: str) -> dict:
    """A new access token and a replacement refresh token; the caller saves both together."""
    return _token_call({"grant_type": "refresh_token", "client_id": client_id,
                        "refresh_token": refresh_token, "resource": RESOURCE})


def has_plan_scope(scope_text: str) -> bool:
    return PLAN_SCOPE in (scope_text or "").split()


def claims(jwt: str) -> dict:
    """The payload of a JWT, NOT checked. Use verify_id_token for anything that matters."""
    try:
        return json.loads(_unb64url(jwt.split(".")[1]))
    except (IndexError, ValueError):
        raise AuthError("OpenAI sent a token that cannot be read")


def fetch_jwks() -> list:
    def get(url):
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                return json.loads(r.read())
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise AuthError("could not fetch OpenAI's signing keys: " + str(e)[:100])
    return get(get(DISCOVERY)["jwks_uri"]).get("keys", [])


_SHA256_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


def _rs256_ok(signing_input: bytes, sig: bytes, n: int, e: int) -> bool:
    k = (n.bit_length() + 7) // 8
    if len(sig) != k or k < len(_SHA256_INFO) + 32 + 11:
        return False
    m = pow(int.from_bytes(sig, "big"), e, n).to_bytes(k, "big")
    t = _SHA256_INFO + hashlib.sha256(signing_input).digest()
    return hmac.compare_digest(m, b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t)


def verify_id_token(id_token: str, client_id: str, nonce: str | None, *, jwks: list | None = None,
                    now: float | None = None) -> dict:
    """The ID token's claims once signature, issuer, audience, expiry and (when given) nonce check out."""
    try:
        head_b64, body_b64, sig_b64 = id_token.split(".")
        head = json.loads(_unb64url(head_b64))
        body = json.loads(_unb64url(body_b64))
        sig = _unb64url(sig_b64)
    except ValueError:
        raise AuthError("OpenAI sent an ID token that cannot be read")
    if head.get("alg") != "RS256":
        raise AuthError("the ID token is signed in a way this node does not accept")
    keys = jwks if jwks is not None else fetch_jwks()
    ok = False
    for key in keys:
        if key.get("kty") == "RSA" and (not head.get("kid") or key.get("kid") == head["kid"]):
            n, e = int.from_bytes(_unb64url(key["n"]), "big"), int.from_bytes(_unb64url(key["e"]), "big")
            if _rs256_ok((head_b64 + "." + body_b64).encode(), sig, n, e):
                ok = True
                break
    if not ok:
        raise AuthError("the ID token's signature does not match OpenAI's keys")
    t = now if now is not None else time.time()
    aud = body.get("aud")
    if body.get("iss") != ISSUER:
        raise AuthError("the ID token is from another issuer")
    if client_id not in (aud if isinstance(aud, list) else [aud]):
        raise AuthError("the ID token is for another registration")
    if float(body.get("exp", 0)) < t - CLOCK_SKEW:
        raise AuthError("the ID token has expired")
    if nonce is not None and body.get("nonce") != nonce:
        raise AuthError("the ID token does not belong to this sign-in attempt")
    return body


def credential(tok: dict, ident: dict, client_id: str, host_id: str, scope_text: str, now: float | None = None) -> dict:
    """The record the node keeps for one sign-in (one issued client id and verified identity)."""
    return {"client_id": client_id, "ext_agent_host_id": host_id, "email": ident.get("email"),
            "subject": ident.get("sub"), "issuer": ident.get("iss"), "scopes": (scope_text or "").split(),
            "id_token": tok.get("id_token"), "access_token": tok["access_token"],
            "refresh_token": tok.get("refresh_token"), "token_type": tok.get("token_type", "Bearer"),
            "expires_in": int(tok.get("expires_in") or 3600), "saved_at": int(now if now is not None else time.time())}


def after_refresh(cred: dict, tok: dict, now: float | None = None) -> dict:
    """The record with a refresh's new tokens, expiry and granted scopes saved together."""
    out = dict(cred)
    out.update(access_token=tok["access_token"], refresh_token=tok.get("refresh_token") or cred.get("refresh_token"),
               expires_in=int(tok.get("expires_in") or 3600), saved_at=int(now if now is not None else time.time()))
    if tok.get("scope"):
        out["scopes"] = tok["scope"].split()
    if tok.get("id_token"):
        out["id_token"] = tok["id_token"]
    return out


def access_is_fresh(cred: dict, now: float | None = None) -> bool:
    t = now if now is not None else time.time()
    return t < float(cred.get("saved_at", 0)) + int(cred.get("expires_in") or 0) - CLOCK_SKEW
