"""JSON API handlers."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

from .http import Request, Response, json_out
from .lib import (HttpError, assertion_problem, b64u, cfg, credential_problem, event, is_int, iso_at, jcs, jcs_bytes,
                  json_text, now, one, origin, parse_ts, q, rows, rp_id, sha256, shaped, strict_eq, token_hash,
                  transaction, unb64u)

TTL_MIN = 900
CLOCK_SKEW = 300
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# Bodies are bounded per route before they are parsed. Request creation and blob upload read
# theirs only after the caller's signature in the headers has been verified.
BODY_SMALL = 64 * 1024     # inbox, enrolment
BODY_ANSWER = 256 * 1024   # a signed answer
HEADER_BODY = 8 * 1024     # the canonical creation body carried in X-Hitl-Body
ANSWER_BLOCK = 4096        # answers are padded to a multiple of this before sealing
SEAL_OVERHEAD = 48         # libsodium sealed box: ephemeral public key and MAC


def max_payload() -> int:
    """Largest encrypted payload a deployment accepts, in bytes (attachments are inside it)."""
    return max(1_000_000, _int(cfg("HITL_MAX_PAYLOAD_BYTES", "15000000")))


def max_ttl() -> int:
    """Longest life of a request, in seconds (HITL_MAX_TTL, default 7 days)."""
    return max(TTL_MIN, _int(cfg("HITL_MAX_TTL", "604800")))


def max_blob() -> int:
    return max(1_000_000, _int(cfg("HITL_MAX_BLOB_BYTES", "250000000")))


def _int(s: str) -> int:
    """The leading integer of a string, else 0."""
    m = re.match(r"^\s*([+-]?\d+)", s or "")
    return int(m.group(1)) if m else 0


def blob_dir() -> str:
    d = os.path.join(os.path.dirname(cfg("HITL_DB", "/var/hitl-data/hitl.sqlite")), "blobs")
    os.makedirs(d, 0o770, exist_ok=True)
    return d


def sign_ok(sig: bytes, msg: bytes, pub: bytes) -> bool:
    if len(sig) != 64 or len(pub) != 32:
        return False
    try:
        VerifyKey(pub).verify(msg, sig)
    except BadSignatureError:
        return False
    return True


def str_field(o: dict, k: str, max_: int = 4096) -> str:
    v = o.get(k)
    if not isinstance(v, str) or v == "" or len(v.encode("utf-8", "surrogatepass")) > max_:
        raise HttpError(f"field {k} missing or invalid")
    return v


def _depth(v: object) -> int:
    if isinstance(v, dict):
        return 1 + max((_depth(x) for x in v.values()), default=0)
    if isinstance(v, list):
        return 1 + max((_depth(x) for x in v), default=0)
    return 0


def json_in(req: Request, limit: int) -> dict:
    raw = req.read(limit)
    if raw is None:
        raise HttpError("request too large", 413)
    try:
        data = json.loads(raw)
    except ValueError:
        raise HttpError("invalid JSON")
    if not isinstance(data, dict):
        raise HttpError("expected a JSON object")
    if _depth(data) > 32:
        raise HttpError("invalid JSON")
    return data


def _obj(v: object) -> dict | None:
    return v if isinstance(v, dict) else None


def signed_header(req: Request, name: str, type_: str) -> tuple[dict, bytes, dict, bytes]:
    """A caller-signed object carried in a header, checked before the HTTP body is touched: it must
    be canonical (so the bytes signed are the bytes stored and hashed), of the named type with
    exactly its fields, signed by a registered caller, and made within the clock allowance.
    Returns (object, its canonical bytes, caller row, signature)."""
    raw = req.header(name)
    if raw == "" or len(raw) > HEADER_BODY * 4 // 3 + 4:
        raise HttpError(f"{name} header required, at most {HEADER_BODY} bytes")
    canonical = unb64u(raw)
    try:
        o = json.loads(canonical)
    except ValueError:
        o = None
    if not shaped(o, type_) or _depth(o) > 8 or jcs_bytes(o) != canonical:
        raise HttpError(f"{name} is not a canonical {type_} object")
    caller = one("SELECT * FROM callers WHERE id = ?", (str_field(o, "caller_id", 64),))
    if not caller:
        raise HttpError("unknown caller", 403)
    sig = unb64u(req.header("X-Hitl-Signature"))
    if not sign_ok(sig, canonical, unb64u(caller["pubkey"])):
        raise HttpError("caller signature invalid", 403)
    created = parse_ts(o.get("created_at"))
    if created is None or abs(created - time.time()) > CLOCK_SKEW:
        raise HttpError("created_at too far from server time")
    return o, canonical, caller, sig


# ---------------------------------------------------------------- blobs

def blob_put(req: Request) -> Response:
    """Stores a large attachment the caller encrypted in chunks. The caller signs a header with the
    ciphertext hash and size; the body is streamed to disk and checked against it. A blob becomes
    downloadable only once a signed request claims it."""
    hdr, _, caller, _ = signed_header(req, "X-Hitl-Blob", "hitl.blob")
    size = hdr.get("size")
    sha = str_field(hdr, "sha256", 64)
    if not is_int(size) or size < 1 or size > max_blob() or not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise HttpError("invalid blob header")
    id_ = b64u(secrets.token_bytes(18))
    tmp = os.path.join(blob_dir(), f".{id_}.part")
    h = hashlib.sha256()
    n = 0
    try:
        with open(tmp, "wb") as out:
            for buf in req.stream_chunks(1 << 20):
                n += len(buf)
                if n > size:
                    raise HttpError("blob larger than its signed size", 413)
                h.update(buf)
                out.write(buf)
        if n != size or not hmac.compare_digest(sha, h.hexdigest()):
            raise HttpError("blob does not match its signed header")
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    os.rename(tmp, os.path.join(blob_dir(), id_))
    q("INSERT INTO blobs (id, caller_id, sha256, size, created_at) VALUES (?, ?, ?, ?, ?)",
      (id_, caller["id"], sha, size, now()))
    return json_out({"blob_id": id_}, 201)


def blob_get(req: Request, id_: str) -> Response:
    row = one("SELECT * FROM blobs WHERE id = ? AND request_id IS NOT NULL", (id_,))
    path = os.path.join(blob_dir(), id_)
    if not row or not os.path.isfile(path):
        raise HttpError("unknown blob", 404)
    # Stored bytes are a caller's and must never run in this origin, whatever a browser makes of them.
    return Response.file(path, [("Content-Type", "application/octet-stream"),
                                ("Content-Length", str(row["size"])),
                                ("Content-Security-Policy", "default-src 'none'; sandbox"),
                                ("Cache-Control", "private, max-age=3600")])


# ---------------------------------------------------------------- recipients and keys

def active_credentials(recipient_id: str, with_wrapped: bool) -> list[dict]:
    out = []
    for c in rows("SELECT * FROM credentials WHERE recipient_id = ? AND retired_at IS NULL ORDER BY id", (recipient_id,)):
        item = {"id": c["id"], "spki": c["spki"], "alg": int(c["alg"]), "label": c["label"]}
        if with_wrapped:
            item.update(prf_salt=c["prf_salt"], wrapped_key=c["wrapped_key"], wrap_nonce=c["wrap_nonce"])
        out.append(item)
    return out


def latest_statement(r: dict) -> dict | None:
    """The newest key statement of the recipient's current epoch."""
    return one("SELECT seq, statement FROM key_statements WHERE recipient_id = ? AND epoch = ? ORDER BY seq DESC LIMIT 1",
               (r["id"], r["epoch"]))


def latest_seq(r: dict) -> int:
    last = latest_statement(r)
    return int(last["seq"]) if last else 0


def _assertion_fields(a: dict, with_credential: bool = True) -> dict:
    out = {"credential_id": str_field(a, "credential_id", 1400)} if with_credential else {}
    out.update(client_data_json=str_field(a, "client_data_json", 8192),
               authenticator_data=str_field(a, "authenticator_data", 8192),
               signature=str_field(a, "signature", 2048))
    return out


def check_key_statement(r: dict, in_: dict, cred_id: str, spki: str, alg: int, enc_pub: str) -> tuple[dict, dict, str]:
    """Checks the key statement that must accompany every credential change (R9). The statement
    lists the recipient's complete key set after the change, names the hash of the statement
    before it, and is signed, as a WebAuthn assertion over SHA-256 of its JCS encoding, by a
    credential that was already enrolled (or, for the first credential of an epoch, by that
    credential itself). Callers and devices replay the chain, so a credential the service adds
    on its own is never trusted. Returns (statement, assertion, the new device's label)."""
    st = in_.get("statement")
    sa = _obj(in_.get("statement_assertion"))
    if not shaped(st, "hitl.key-statement") or sa is None:
        raise HttpError("a signed key statement is required")
    listed = st["credentials"] if isinstance(st["credentials"], list) else []
    mine = next((c for c in listed if isinstance(c, dict) and c.get("id") == cred_id), None)
    label = mine.get("label") if mine else None
    # The label is whatever the device signed; the service stores it and never rewrites it.
    if not isinstance(label, str) or not 1 <= len(label) <= 80 or re.search(r"[\x00-\x1f\x7f\ud800-\udfff]", label):
        raise HttpError("the key statement must name the new device with a label of 1 to 80 characters")
    existing = active_credentials(r["id"], False)
    expected = sorted([*existing, {"id": cred_id, "spki": spki, "alg": alg, "label": label}], key=lambda c: c["id"])
    last = latest_statement(r)
    if (last is None) != (not existing):
        raise HttpError("the key statement chain and the enrolled devices disagree", 500)
    prev = b64u(sha256(last["statement"].encode("utf-8", "surrogatepass"))) if last else ""
    if (st["recipient_id"] != r["id"] or not strict_eq(st["epoch"], int(r["epoch"]))
            or not strict_eq(st["seq"], (int(last["seq"]) if last else 0) + 1) or not strict_eq(st["prev"], prev)
            or not strict_eq(st["enc_pubkey"], enc_pub) or jcs(st["credentials"]) != jcs(expected)
            or parse_ts(st["issued_at"]) is None):
        raise HttpError("key statement does not describe this enrolment")
    signers = existing if existing else [expected[0]]
    signer = next((c for c in signers if c["id"] == sa.get("credential_id")), None)
    if signer is None:
        raise HttpError("key statement must be signed by an already enrolled device", 403)
    assertion = {"credential_id": signer["id"], **_assertion_fields(sa, with_credential=False)}
    problem = assertion_problem(assertion, sha256(jcs_bytes(st)), unb64u(signer["spki"]), signer["alg"], origin(), rp_id())
    if problem is not None:
        raise HttpError("key statement rejected: " + problem, 403)
    return st, assertion, label


def valid_enrol_token(token: str) -> dict:
    t = one("SELECT * FROM enrol_tokens WHERE token_hash = ?", (token_hash(token),))
    if not t or t["used_at"] is not None or t["expires_at"] < now():
        raise HttpError("enrolment link is invalid, used or expired", 404)
    return t


def sso_enrol(req: Request) -> Response:
    """Enrolment vouched for by an identity provider in front of the service (an SSO forward-auth
    proxy, Kerberos, anything that can set a request header). The identity header is trusted
    only when the proxy also sends HITL_SSO_PROXY_SECRET, so another client on the same network
    cannot claim an identity by setting the header itself. Issues a short-lived enrolment token
    for that person; the rest of the flow is the normal enrolment."""
    id_header = cfg("HITL_SSO_ID_HEADER")
    secret = cfg("HITL_SSO_PROXY_SECRET")
    if id_header == "" or secret == "":
        raise HttpError("single sign-on enrolment is not configured", 404)
    if not hmac.compare_digest(secret.encode(), req.header("X-Hitl-Proxy-Secret").encode()):
        raise HttpError("identity was not vouched for by the configured proxy", 403)
    id_ = req.header(id_header).strip().lower()
    if not re.fullmatch(r"[a-z0-9._-]{1,64}", id_):
        raise HttpError("the identity provider did not supply a usable user id", 403)
    allow = [x.strip() for x in cfg("HITL_SSO_ALLOW").lower().split(",") if x.strip()]
    if allow and id_ not in allow:
        raise HttpError(f"{id_} is not allowed to enrol on this deployment", 403)
    via = "sso:" + cfg("HITL_SSO_NAME", "proxy")
    q("INSERT INTO recipients (id, display_name, created_at) VALUES (?, ?, ?) ON CONFLICT(id) DO NOTHING", (id_, id_, now()))
    token = b64u(secrets.token_bytes(24))
    q("INSERT INTO enrol_tokens (token_hash, recipient_id, expires_at, via) VALUES (?, ?, ?, ?)",
      (token_hash(token), id_, iso_at(time.time() + 900), via))
    event("hitl.recipient.enrol_started", None, None, {"recipient_id": id_, "via": via})
    return json_out({"token": token, "recipient_id": id_, "via": via})


def enrol_get(req: Request, token: str) -> Response:
    t = valid_enrol_token(token)
    r = one("SELECT * FROM recipients WHERE id = ?", (t["recipient_id"],))
    return json_out({
        "recipient_id": r["id"],
        "display_name": r["display_name"],
        "enc_pubkey": r["enc_pubkey"],
        "epoch": int(r["epoch"]),
        "seq": latest_seq(r),
        "via": t["via"],
        "credentials": active_credentials(r["id"], True),
    })


def enrol_finish(req: Request, token: str) -> Response:
    t = valid_enrol_token(token)
    in_ = json_in(req, BODY_SMALL)
    cred_id = str_field(in_, "credential_id", 1400)
    spki = unb64u(str_field(in_, "spki"))
    alg = in_.get("alg")
    problem = credential_problem(spki, alg)
    if problem is not None:
        raise HttpError(problem)
    enc_pub = str_field(in_, "enc_pubkey", 64)
    if len(unb64u(enc_pub)) != 32:
        raise HttpError("enc_pubkey must be an X25519 public key")

    with transaction():
        r = one("SELECT * FROM recipients WHERE id = ?", (t["recipient_id"],))
        if r["enc_pubkey"] is None:
            q("UPDATE recipients SET enc_pubkey = ? WHERE id = ?", (enc_pub, r["id"]))
        elif not hmac.compare_digest(r["enc_pubkey"], enc_pub):
            raise HttpError("a new device must wrap the existing encryption key")
        statement, st_assertion, label = check_key_statement(r, in_, cred_id, b64u(spki), alg, enc_pub)
        q("INSERT INTO key_statements (recipient_id, epoch, seq, statement, signer_credential_id, assertion, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
          (r["id"], statement["epoch"], statement["seq"], jcs(statement), st_assertion["credential_id"], json_text(st_assertion), now()))
        q("""INSERT INTO credentials (id, recipient_id, spki, alg, prf_salt, wrapped_key, wrap_nonce, label, enrolled_via, created_at)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
          (cred_id, r["id"], b64u(spki), alg, str_field(in_, "prf_salt", 64), str_field(in_, "wrapped_key", 1024),
           str_field(in_, "wrap_nonce", 64), label, t["via"], now()))
        n = q("UPDATE enrol_tokens SET used_at = ? WHERE token_hash = ? AND used_at IS NULL", (now(), token_hash(token))).rowcount
        if n != 1:
            raise HttpError("enrolment link already used", 409)
        event("hitl.recipient.keys", None, None, {"recipient_id": r["id"], "added_credential": cred_id, "epoch": statement["epoch"],
                                                  "seq": statement["seq"], "statement_hash": b64u(sha256(jcs_bytes(statement)))})
    return json_out({"ok": True})


def _recipient(id_: str) -> dict:
    r = one("SELECT * FROM recipients WHERE id = ?", (id_,))
    if not r or r["enc_pubkey"] is None:
        raise HttpError("unknown recipient", 404)
    return r


def recipient_keys(req: Request, id_: str) -> Response:
    r = _recipient(id_)
    return json_out({
        "recipient_id": r["id"],
        "display_name": r["display_name"],
        "enc_pubkey": r["enc_pubkey"],
        "epoch": int(r["epoch"]),
        "seq": latest_seq(r),
        "credentials": active_credentials(r["id"], False),
    })


def recipient_statements(req: Request, id_: str) -> Response:
    """The key statement chain of the recipient's current epoch after a sequence number. A claim like
    everything else here: callers and devices verify every link themselves."""
    r = _recipient(id_)
    after = max(0, _int(req.query.get("after", "0")))
    items = rows("SELECT seq, statement, assertion FROM key_statements WHERE recipient_id = ? AND epoch = ? AND seq > ? ORDER BY seq",
                 (id_, r["epoch"], after))
    return json_out({"recipient_id": id_, "epoch": int(r["epoch"]), "statements": [
        {"seq": int(x["seq"]), "statement": json.loads(x["statement"]), "assertion": json.loads(x["assertion"])} for x in items
    ]})


def recipient_unlock(req: Request, id_: str) -> Response:
    """What a page needs to unlock for this recipient without a request: the same wrapped keys every
    request already carries (useless without the recipient's authenticator)."""
    r = _recipient(id_)
    return json_out({"recipient_id": r["id"], "enc_pubkey": r["enc_pubkey"], "epoch": int(r["epoch"]),
                     "credentials": active_credentials(id_, True)})


# ---------------------------------------------------------------- requests

def policy_ok(p: object) -> bool:
    """The caller's policy in its one canonical form: assurance fresh or session, review true or
    false, and max_session_age (60 to 86400 seconds) only with session. Always present and never
    defaulted here: a default applied by the service would be a decision made on the caller's behalf."""
    if not isinstance(p, dict) or p.get("assurance") not in ("fresh", "session") or not isinstance(p.get("review"), bool):
        return False
    allowed = {"assurance", "review"} | ({"max_session_age"} if p["assurance"] == "session" else set())
    if not set(p) <= allowed:
        return False
    return "max_session_age" not in p or (is_int(p["max_session_age"]) and 60 <= p["max_session_age"] <= 86400)


def _hash_field(o: dict, k: str) -> str:
    v = o.get(k)
    if not isinstance(v, str) or len(unb64u(v)) != 32:
        raise HttpError(f"field {k} must be 32 bytes in base64url")
    return v


def request_create(req: Request) -> Response:
    """The caller mints the request id and states the lifetime; the service mints nothing a caller
    would have to trust. Everything is checked from the signed header before the HTTP body, which
    holds only the two ciphertexts, is read: an unsigned party cannot make the service read or
    parse a large body, and a copied body cannot create a second request."""
    body, canonical, caller, sig = signed_header(req, "X-Hitl-Body", "hitl.request")
    id_ = body["request_id"]
    if not isinstance(id_, str) or not re.fullmatch(UUID, id_):
        raise HttpError("request_id must be a lower-case UUID")
    if one("SELECT 1 FROM requests WHERE id = ?", (id_,)):
        raise HttpError("request_id already used", 409)
    ttl = body["ttl"]
    if not is_int(ttl) or ttl < TTL_MIN or ttl > max_ttl():
        raise HttpError("ttl out of range")
    caller_ref = body["caller_ref"]
    if not isinstance(caller_ref, str) or len(caller_ref) > 200 or not re.fullmatch(r"[\x20-\x7e]*", caller_ref):
        raise HttpError("caller_ref must be at most 200 printable ASCII characters")
    if not policy_ok(body["policy"]):
        raise HttpError("policy must give assurance (fresh or session), review (true or false) and, "
                        "with session only, max_session_age of 60 to 86400 seconds")
    blob_ids = body["blobs"]
    if not isinstance(blob_ids, list) or len(blob_ids) > 50 or len(set(map(str, blob_ids))) != len(blob_ids):
        raise HttpError("blobs must be a list of at most 50 distinct ids")
    recipient_id = str_field(body, "recipient_id", 64)
    config_hash, payload_hash = _hash_field(body, "config_ct_hash"), _hash_field(body, "payload_ct_hash")
    _hash_field(body, "answer_pubkey")

    in_ = json_in(req, 1_000_000 + max_payload() * 4 // 3 + 4096)
    config_ct = str_field(in_, "config_ct", 1_000_000)
    payload_ct = str_field(in_, "payload_ct", max_payload() * 4 // 3 + 64)
    if (not hmac.compare_digest(b64u(sha256(unb64u(config_ct))), config_hash)
            or not hmac.compare_digest(b64u(sha256(unb64u(payload_ct))), payload_hash)):
        raise HttpError("ciphertext does not match the signed hashes")

    with transaction():
        for b in blob_ids:
            if not isinstance(b, str) or not one("SELECT 1 FROM blobs WHERE id = ? AND caller_id = ? AND request_id IS NULL", (b, caller["id"])):
                raise HttpError("unknown or already used blob")
        r = one("SELECT * FROM recipients WHERE id = ?", (recipient_id,))
        if not r or r["enc_pubkey"] is None or not active_credentials(recipient_id, False):
            raise HttpError("unknown or unenrolled recipient", 404)
        short = b64u(secrets.token_bytes(12))
        try:
            q("""INSERT INTO requests (id, short, caller_id, recipient_id, creation_body, caller_signature,
                                       config_ct, payload_ct, created_at, expires_at)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
              (id_, short, caller["id"], recipient_id, canonical.decode("utf-8", "surrogatepass"), b64u(sig), config_ct,
               payload_ct, body["created_at"], iso_at(parse_ts(body["created_at"]) + ttl)))
        except sqlite3.IntegrityError:
            raise HttpError("request_id already used", 409)
        for b in blob_ids:
            q("UPDATE blobs SET request_id = ? WHERE id = ?", (id_, b))
        event("hitl.request.created", id_, caller["id"], {
            "recipient_id": recipient_id, "config_ct_hash": config_hash, "payload_ct_hash": payload_hash, "ttl": ttl,
        })
    return json_out({"short_link": origin() + "/r/" + short}, 201)


def delegation_check(recipient_id: str, d: object, a: object) -> tuple[dict, dict, bytes, int, int]:
    """A device-signed session delegation for this recipient: exact shape, lifetime at most 24 h, and
    a valid assertion by an enrolled credential over SHA-256 of its JCS encoding."""
    if not shaped(d, "hitl.session") or not isinstance(a, dict) or d["recipient_id"] != recipient_id:
        raise HttpError("session delegation malformed")
    issued = parse_ts(d["issued_at"])
    expires = parse_ts(d["expires_at"])
    pub = unb64u(str_field(d, "session_pubkey", 64))
    if issued is None or expires is None or expires - issued > 86400 + CLOCK_SKEW or len(pub) != 32:
        raise HttpError("session delegation invalid", 403)
    assertion = _assertion_fields(a)
    cred = one("SELECT * FROM credentials WHERE id = ? AND recipient_id = ? AND retired_at IS NULL",
               (assertion["credential_id"], recipient_id))
    if not cred:
        raise HttpError("credential is not enrolled for this recipient", 403)
    problem = assertion_problem(assertion, sha256(jcs_bytes(d)), unb64u(cred["spki"]), int(cred["alg"]), origin(), rp_id())
    if problem is not None:
        raise HttpError("session delegation rejected: " + problem, 403)
    return d, assertion, pub, issued, expires


def inbox(req: Request) -> Response:
    """The recipient's pending requests, for a live device session only. Metadata only: who asks,
    expiry and policy. Titles and content stay encrypted."""
    in_ = json_in(req, BODY_SMALL)
    if set(in_) != {"type", "recipient_id", "ts", "delegation", "assertion", "signature"} or in_["type"] != "hitl.inbox":
        raise HttpError("inbox request malformed")
    rid = str_field(in_, "recipient_id", 64)
    _, _, pub, _, expires = delegation_check(rid, in_["delegation"], in_["assertion"])
    ts = str_field(in_, "ts", 32)
    t = parse_ts(ts)
    if t is None or abs(t - time.time()) > CLOCK_SKEW or time.time() > expires:
        raise HttpError("session expired or clock skew", 403)
    msg = {"type": "hitl.inbox", "recipient_id": rid, "ts": ts}
    if not sign_ok(unb64u(str_field(in_, "signature", 128)), jcs_bytes(msg), pub):
        raise HttpError("inbox request signature invalid", 403)
    items = rows("""SELECT r.id, r.short, r.created_at, r.expires_at, r.creation_body, c.display_name, c.display_domain
                    FROM requests r JOIN callers c ON c.id = r.caller_id
                    WHERE r.recipient_id = ? AND r.response_record IS NULL AND r.expires_at > ?
                    ORDER BY r.expires_at LIMIT 200""", (rid, now()))
    return json_out({"requests": [{
        "request_id": r["id"], "short": r["short"], "created_at": r["created_at"], "expires_at": r["expires_at"],
        "policy": json.loads(r["creation_body"])["policy"],
        "caller": {"display_name": r["display_name"], "display_domain": r["display_domain"]},
    } for r in items]})


def session_proof(req_row: dict, policy: dict, record: dict, record_jcs: str, s: dict) -> dict:
    """Checks an answer confirmed through a device session. Returns the stored proof."""
    if policy["assurance"] != "session":
        raise HttpError("this request requires a fresh device confirmation", 403)
    d, assertion, pub, issued, expires = delegation_check(req_row["recipient_id"], s.get("delegation"), s.get("assertion"))
    signed = parse_ts(record["signed_at"])
    if signed is None or signed < issued - CLOCK_SKEW or signed > expires:
        raise HttpError("session not valid at signing time", 403)
    # signed_at is the device's word and a device clock can be set, so the session must also be
    # alive by this service's own clock.
    if time.time() > expires:
        raise HttpError("session expired", 403)
    if "max_session_age" in policy and signed - issued > policy["max_session_age"]:
        raise HttpError("session older than the caller allows", 403)
    sig = unb64u(str_field(s, "signature", 128))
    if not sign_ok(sig, record_jcs.encode("utf-8", "surrogatepass"), pub):
        raise HttpError("session signature invalid", 403)
    return {"kind": "session", "credential_id": assertion["credential_id"], "delegation": d,
            "delegation_assertion": assertion, "signature": b64u(sig)}


def find_request(key: str) -> dict:
    r = one("SELECT * FROM requests WHERE id = ? OR short = ?", (key, key))
    if not r:
        raise HttpError("unknown request", 404)
    return r


def request_status(r: dict) -> str:
    if r["response_record"] is not None:
        return "signed"
    return "expired" if r["expires_at"] < now() else "pending"


def request_get(req: Request, key: str) -> Response:
    """Everything here is a claim the reader checks: the page against the sealed manifest and the
    ciphertexts, the caller against what it sent."""
    r = find_request(key)
    # Opening through the short link is the recipient; the caller polls by id.
    if key == r["short"] and r["opened_at"] is None:
        q("UPDATE requests SET opened_at = ? WHERE id = ? AND opened_at IS NULL", (now(), r["id"]))
        event("hitl.request.opened", r["id"], r["caller_id"], {"ua_hash": b64u(sha256(req.header("User-Agent").encode()))})
    caller = one("SELECT id, display_name, display_domain, verified_since, theme FROM callers WHERE id = ?", (r["caller_id"],))
    caller["theme"] = json.loads(caller["theme"] or "{}")
    recipient = one("SELECT id, display_name, enc_pubkey, epoch FROM recipients WHERE id = ?", (r["recipient_id"],))
    return json_out({
        "request_id": r["id"],
        "status": request_status(r),
        "caller": caller,
        "creation_body": json.loads(r["creation_body"]),
        "caller_signature": r["caller_signature"],
        "recipient": recipient,
        "credentials": active_credentials(r["recipient_id"], True),
        "config_ct": r["config_ct"],
        "payload_ct": r["payload_ct"],
        "expires_at": r["expires_at"],
        "response": None if r["response_record"] is None else {
            "record": json.loads(r["response_record"]),
            "assertion": json.loads(r["response_assertion"]),
        },
    })


def _sealed(record: dict, k: str) -> None:
    """A padded, sealed answer: its length says nothing about the answer inside."""
    v = record[k]
    n = len(unb64u(v)) - SEAL_OVERHEAD if isinstance(v, str) else 0
    if n < ANSWER_BLOCK or n % ANSWER_BLOCK:
        raise HttpError(f"record field {k} is not a padded sealed answer")


def request_respond(req: Request, id_: str) -> Response:
    r = find_request(id_)
    if r["id"] != id_:
        raise HttpError("respond by request id", 404)
    in_ = json_in(req, BODY_ANSWER)
    record = _obj(in_.get("record"))
    a = _obj(in_.get("assertion"))
    sess = _obj(in_.get("session"))
    if record is None or (a is None) == (sess is None):
        raise HttpError("record and either an assertion or a session proof required")
    record_jcs = jcs(record)
    assertion = _assertion_fields(a) if a is not None else {"signature": str_field(sess, "signature", 128)}

    # Same signed response again: one effect (R6a).
    if r["response_record"] is not None:
        prev = json.loads(r["response_assertion"])
        if hmac.compare_digest(r["response_record"].encode("utf-8", "surrogatepass"), record_jcs.encode("utf-8", "surrogatepass")) \
                and hmac.compare_digest(prev["signature"].encode(), assertion["signature"].encode()):
            return json_out({"status": "signed", "duplicate": True})
        raise HttpError("request already answered", 409)
    if r["expires_at"] < now():
        raise HttpError("request expired", 410)
    policy = json.loads(r["creation_body"])["policy"]
    if not shaped(record, "hitl.response", ("answer_self_ct",) if policy["review"] else ()):
        raise HttpError("the record must be a hitl.response of version 2 with exactly its fields")
    creation_hash = b64u(sha256(r["creation_body"].encode("utf-8", "surrogatepass")))
    if not strict_eq(record["request_id"], r["id"]) or not strict_eq(record["creation_hash"], creation_hash):
        raise HttpError("the record does not answer this request")
    _hash_field(record, "config_hash")
    _hash_field(record, "payload_hash")
    _sealed(record, "answer_ct")
    if policy["review"]:
        _sealed(record, "answer_self_ct")
    # The device wrote signed_at and a device clock can be set: it must agree with this clock.
    signed = parse_ts(record["signed_at"])
    if signed is None or abs(signed - time.time()) > CLOCK_SKEW:
        raise HttpError("signed_at too far from server time", 403)
    # The service cannot read the answer or the schema (R2, R3); it checks the confirmation is sound.
    if sess is not None:
        assertion = session_proof(r, policy, record, record_jcs, sess)
    else:
        cred = one("SELECT * FROM credentials WHERE id = ? AND recipient_id = ? AND retired_at IS NULL",
                   (assertion["credential_id"], r["recipient_id"]))
        if not cred:
            raise HttpError("credential is not enrolled for this recipient", 403)
        problem = assertion_problem(assertion, sha256(record_jcs.encode("utf-8", "surrogatepass")),
                                    unb64u(cred["spki"]), int(cred["alg"]), origin(), rp_id())
        if problem is not None:
            raise HttpError("assertion rejected: " + problem, 403)
    n = q("UPDATE requests SET response_record = ?, response_assertion = ?, signed_at = ? WHERE id = ? AND response_record IS NULL",
          (record_jcs, json_text(assertion), now(), id_)).rowcount
    if n != 1:
        raise HttpError("request already answered", 409)
    event("hitl.request.signed", id_, r["caller_id"], {"signature_hash": b64u(sha256(unb64u(assertion["signature"])))})
    return json_out({"status": "signed"})
