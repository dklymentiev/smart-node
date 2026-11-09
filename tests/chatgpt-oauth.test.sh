#!/usr/bin/env bash
#
# Sign in with ChatGPT: the protocol module (companions/core-api/chatgpt_oauth.py).
#
# WHAT IS CHECKED, with no network and nothing on the host touched:
#
#   start         the first sign-in asks OpenAI to register the node (its host id, the app name,
#                 S256 challenge that matches the verifier); a later one reuses the issued id,
#                 sends the hints, and does not send the name again
#   read_landing  the pasted address is accepted, or refused with a message for the owner when it
#                 is another attempt's, was denied, has no code, is the wrong page, carries no
#                 issued client id, came back for another registration, or is too old
#   exchange      the code is traded with the verifier and the exact redirect; a refused trade
#                 says to sign in again and never contains a token
#   verify        an ID token signed with RS256 passes; a changed body, another key, another
#                 audience or issuer, an old expiry, another nonce and alg "none" are all refused
#   records       the saved record and a refresh keep the rotated refresh token and scopes
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/../companions/core-api"

python3 -I - <<'PY'
import base64
import hashlib
import json
import random
import sys
import urllib.parse

sys.path.insert(0, ".")
import chatgpt_oauth as o

failed = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        failed.append(name)


def raises(fn, *needles):
    try:
        fn()
    except o.AuthError as e:
        text = str(e).lower()
        return all(n in text for n in needles)
    return False


def q(url):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


# ---- start ----
HOST = "urn:uuid:11111111-2222-4333-8444-555555555555"
url, pend = o.start(HOST, now=1000.0)
p = q(url)
check("start: first sign-in registers the node", p["client_id"] == "dynamic_agent_client" and p["agent_name_hint"] == "Smart Node")
check("start: host id, resource, scope, redirect", p["ext_agent_host_id"] == HOST and p["resource"] == o.RESOURCE
      and p["scope"] == o.SCOPE and p["redirect_uri"] == o.REDIRECT and p["response_type"] == "code")
check("start: S256 challenge matches the verifier", p["code_challenge_method"] == "S256"
      and p["code_challenge"] == base64.urlsafe_b64encode(hashlib.sha256(pend["verifier"].encode()).digest()).rstrip(b"=").decode())
check("start: fresh state and nonce each time", o.start(HOST)[1]["state"] != o.start(HOST)[1]["state"] and p["state"] == pend["state"])
url2, pend2 = o.start(HOST, client_id="oaiapp_abc", id_token_hint="IDT", login_hint="me@example.com", now=1000.0)
p2 = q(url2)
check("start: a later sign-in reuses the issued id and sends hints, not the name",
      p2["client_id"] == "oaiapp_abc" and "agent_name_hint" not in p2 and p2["id_token_hint"] == "IDT" and p2["login_hint"] == "me@example.com")
check("start: the first sign-in sends no hints", "id_token_hint" not in p and "login_hint" not in p)


def landing(**kw):
    base = {"code": "CODE1", "state": pend["state"], "client_id": "oaiapp_abc", "scope": o.SCOPE}
    base.update(kw)
    return "http://127.0.0.1:1455/auth/callback?" + urllib.parse.urlencode({k: v for k, v in base.items() if v is not None})


got = o.read_landing(landing(), pend, now=1100.0)
check("landing: a good address is read", got == {"code": "CODE1", "client_id": "oaiapp_abc", "scope": o.SCOPE})
check("landing: only the query string pasted is read too",
      o.read_landing("code=CODE1&state=" + pend["state"] + "&client_id=oaiapp_abc", pend, now=1100.0)["code"] == "CODE1")
check("landing: another attempt's state is refused", raises(lambda: o.read_landing(landing(state="nope"), pend, now=1100.0), "another sign-in"))
check("landing: a denied sign-in says so", raises(lambda: o.read_landing(landing(error="access_denied"), pend, now=1100.0), "access_denied"))
check("landing: no code is refused", raises(lambda: o.read_landing(landing(code=None), pend, now=1100.0), "no code"))
check("landing: another page is refused",
      raises(lambda: o.read_landing("https://example.com/auth/callback?code=x&state=" + pend["state"], pend, now=1100.0), "not the page"))
check("landing: the placeholder id is not an issued id", raises(lambda: o.read_landing(landing(client_id="dynamic_agent_client"), pend, now=1100.0), "incomplete"))
check("landing: no issued id on a first sign-in is refused", raises(lambda: o.read_landing(landing(client_id=None), pend, now=1100.0), "incomplete"))
check("landing: a later sign-in may leave the id out", o.read_landing(landing(client_id=None, state=pend2["state"]), pend2, now=1100.0)["client_id"] == "oaiapp_abc")
check("landing: a different registration is refused", raises(lambda: o.read_landing(landing(client_id="oaiapp_other", state=pend2["state"]), pend2, now=1100.0), "different registration"))
check("landing: an old attempt is refused", raises(lambda: o.read_landing(landing(), pend, now=1000.0 + o.ATTEMPT_LIFE + 5), "expired"))

# ---- exchange / refresh ----
calls = []


def fake_post(answer):
    def post(url, fields, timeout=30.0):
        calls.append((url, dict(fields)))
        return answer
    return post


o._post_form = fake_post((200, {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600, "id_token": "I.D.T", "scope": o.SCOPE}))
tok = o.exchange("CODE1", "oaiapp_abc", pend["verifier"])
f = calls[-1][1]
check("exchange: right endpoint and fields", calls[-1][0] == o.TOKEN and f["grant_type"] == "authorization_code" and f["code"] == "CODE1"
      and f["code_verifier"] == pend["verifier"] and f["redirect_uri"] == o.REDIRECT and f["resource"] == o.RESOURCE and f["client_id"] == "oaiapp_abc")
check("exchange: tokens come back to the caller", tok["access_token"] == "AT" and tok["refresh_token"] == "RT")
o._post_form = fake_post((400, {"error": "invalid_grant", "error_description": "code RT-SECRET used"}))
check("exchange: a refused trade says to sign in again and holds no secret", raises(lambda: o.exchange("c", "oaiapp_abc", "v"), "invalid_grant", "sign in again"))
try:
    o.exchange("c", "oaiapp_abc", "v")
except o.AuthError as e:
    check("exchange: the refusal text does not repeat the server's description", "RT-SECRET" not in str(e))
o._post_form = fake_post((200, {"access_token": "AT2", "refresh_token": "RT2", "expires_in": 3600}))
r = o.refresh("oaiapp_abc", "RT")
check("refresh: sends the refresh token and the resource", calls[-1][1]["grant_type"] == "refresh_token" and calls[-1][1]["refresh_token"] == "RT"
      and calls[-1][1]["resource"] == o.RESOURCE and r["refresh_token"] == "RT2")
check("plan scope: present or missing", o.has_plan_scope(o.SCOPE) and not o.has_plan_scope("openid profile"))


# ---- ID token ----
def is_prime(n):
    if n < 4:
        return n in (2, 3)
    if n % 2 == 0:
        return False
    d, s = n - 1, 0
    while d % 2 == 0:
        d, s = d // 2, s + 1
    for a in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(s - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


rng = random.Random(7)


def prime(bits):
    while True:
        c = rng.getrandbits(bits) | (1 << (bits - 1)) | 1
        if is_prime(c):
            return c


def make_key():
    while True:
        p_, q_ = prime(512), prime(512)
        n, phi, e = p_ * q_, (p_ - 1) * (q_ - 1), 65537
        try:
            return n, e, pow(e, -1, phi)
        except ValueError:
            continue


def b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def sign(body, key, alg="RS256", kid="k1"):
    n, e, d = key
    head = b64(json.dumps({"alg": alg, "kid": kid}).encode())
    payload = b64(json.dumps(body).encode())
    k = (n.bit_length() + 7) // 8
    t = o._SHA256_INFO + hashlib.sha256((head + "." + payload).encode()).digest()
    em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    sig = pow(int.from_bytes(em, "big"), d, n).to_bytes(k, "big")
    return head + "." + payload + "." + b64(sig)


def jwk(key, kid="k1"):
    n, e, _ = key
    return {"kty": "RSA", "kid": kid, "n": b64(n.to_bytes((n.bit_length() + 7) // 8, "big")), "e": b64(e.to_bytes(3, "big"))}


KEY, OTHER = make_key(), make_key()
JWKS = [jwk(KEY)]
NOW = 5000.0
BODY = {"iss": o.ISSUER, "aud": "oaiapp_abc", "exp": NOW + 600, "nonce": "N1", "sub": "user-1", "email": "me@example.com"}
check("verify: a good token gives its claims", o.verify_id_token(sign(BODY, KEY), "oaiapp_abc", "N1", jwks=JWKS, now=NOW)["sub"] == "user-1")
check("verify: audience may be a list", o.verify_id_token(sign(dict(BODY, aud=["x", "oaiapp_abc"]), KEY), "oaiapp_abc", "N1", jwks=JWKS, now=NOW)["sub"] == "user-1")
good = sign(BODY, KEY)
h, b, s = good.split(".")
forged = h + "." + b64(json.dumps(dict(BODY, sub="someone-else")).encode()) + "." + s
check("verify: a changed body is refused", raises(lambda: o.verify_id_token(forged, "oaiapp_abc", "N1", jwks=JWKS, now=NOW), "signature"))
check("verify: another key is refused", raises(lambda: o.verify_id_token(sign(BODY, OTHER), "oaiapp_abc", "N1", jwks=JWKS, now=NOW), "signature"))
check("verify: another audience is refused", raises(lambda: o.verify_id_token(sign(dict(BODY, aud="oaiapp_zzz"), KEY), "oaiapp_abc", "N1", jwks=JWKS, now=NOW), "registration"))
check("verify: another issuer is refused", raises(lambda: o.verify_id_token(sign(dict(BODY, iss="https://evil.example"), KEY), "oaiapp_abc", "N1", jwks=JWKS, now=NOW), "issuer"))
check("verify: an old token is refused", raises(lambda: o.verify_id_token(sign(dict(BODY, exp=NOW - 1000), KEY), "oaiapp_abc", "N1", jwks=JWKS, now=NOW), "expired"))
check("verify: another nonce is refused", raises(lambda: o.verify_id_token(sign(BODY, KEY), "oaiapp_abc", "N2", jwks=JWKS, now=NOW), "attempt"))
check("verify: no nonce check is possible for a later refresh", o.verify_id_token(sign(BODY, KEY), "oaiapp_abc", None, jwks=JWKS, now=NOW)["sub"] == "user-1")
none_tok = b64(json.dumps({"alg": "none"}).encode()) + "." + b64(json.dumps(BODY).encode()) + "."
check("verify: alg none is refused", raises(lambda: o.verify_id_token(none_tok, "oaiapp_abc", "N1", jwks=JWKS, now=NOW), "signed"))
check("verify: a key with another kid is not tried", raises(lambda: o.verify_id_token(sign(BODY, KEY, kid="k9"), "oaiapp_abc", "N1", jwks=JWKS, now=NOW), "signature"))

# ---- records ----
rec = o.credential(tok, {"email": "me@example.com", "sub": "user-1", "iss": o.ISSUER}, "oaiapp_abc", HOST, o.SCOPE, now=NOW)
check("record: holds the identity and tokens", rec["email"] == "me@example.com" and rec["refresh_token"] == "RT" and rec["scopes"] == o.SCOPE.split() and rec["saved_at"] == 5000)
check("record: fresh until shortly before it expires", o.access_is_fresh(rec, now=NOW + 3400) and not o.access_is_fresh(rec, now=NOW + 3500))
rec2 = o.after_refresh(rec, {"access_token": "AT2", "refresh_token": "RT2", "expires_in": 3600, "scope": "openid " + o.PLAN_SCOPE}, now=NOW + 3500)
check("refresh record: new tokens saved together, identity kept", rec2["access_token"] == "AT2" and rec2["refresh_token"] == "RT2"
      and rec2["email"] == "me@example.com" and rec2["scopes"] == ["openid", o.PLAN_SCOPE] and rec2["saved_at"] == 8500)
rec3 = o.after_refresh(rec, {"access_token": "AT3", "expires_in": 3600}, now=NOW)
check("refresh record: a reply without a new refresh token keeps the old one", rec3["refresh_token"] == "RT")
sys.exit(1 if failed else 0)
PY
echo "PASS: chatgpt oauth protocol"
