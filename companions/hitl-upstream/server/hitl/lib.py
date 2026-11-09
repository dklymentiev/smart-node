"""Shared helpers: config, storage, encoding, WebAuthn assertion checks."""

from __future__ import annotations

import base64
import calendar
import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

SCHEMA = os.path.join(os.path.dirname(__file__), "..", "schema.sql")
DB_VERSION = 3


class HttpError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def cfg(key: str, default: str = "") -> str:
    v = os.environ.get(key, "")
    return default if v == "" else v


def origin() -> str:
    """The origin the decision page is served from; rpId is its host."""
    return cfg("HITL_ORIGIN", "https://localhost").rstrip("/")


def rp_id() -> str:
    from urllib.parse import urlsplit

    return urlsplit(origin()).hostname or ""


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def iso_at(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


_TS = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")


def parse_ts(s: object) -> int | None:
    """Unix seconds for an RFC 3339 timestamp in UTC with a Z suffix, or None. Signed objects use
    this one form only: a second spelling of the same instant would be a second encoding to sign."""
    if not isinstance(s, str) or not _TS.fullmatch(s):
        return None
    try:
        t = calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return None
    # One spelling per instant: second 60, hour 24 and the like parse to a time that prints differently.
    return t if t >= 0 and iso_at(t) == s else None


# ---------------------------------------------------------------- storage

_local = threading.local()


def db() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn
    conn = sqlite3.connect(cfg("HITL_DB", "/var/hitl-data/hitl.sqlite"), isolation_level=None, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == 0 and conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'requests'").fetchone():
        # A database written before protocol version 2 holds requests, keys and enrolment links in a
        # form this code cannot verify. Reading it as if it were current would be worse than stopping.
        raise RuntimeError("the database at HITL_DB predates protocol version 2: start with a new file, "
                           "register the callers and have every recipient enrol again")
    if version not in (0, 2, DB_VERSION):
        raise RuntimeError(f"the database at HITL_DB has schema version {version}, this code needs {DB_VERSION}")
    with open(SCHEMA, encoding="utf-8") as f:
        conn.executescript(f.read())
    if version == 2:
        # A theme changes only the service-owned chrome. It does not alter any signed object.
        # Check the column as well as user_version so a stopped migration can resume safely.
        if "theme" not in {r[1] for r in conn.execute("PRAGMA table_info(callers)")}:
            try:
                conn.execute("ALTER TABLE callers ADD COLUMN theme TEXT NOT NULL DEFAULT '{}' ")
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
    conn.execute(f"PRAGMA user_version = {DB_VERSION}")
    _local.conn = conn
    return conn


def q(sql: str, args: tuple | list = ()) -> sqlite3.Cursor:
    return db().execute(sql, tuple(args))


def one(sql: str, args: tuple | list = ()) -> dict | None:
    r = q(sql, args).fetchone()
    return dict(r) if r is not None else None


def rows(sql: str, args: tuple | list = ()) -> list[dict]:
    return [dict(r) for r in q(sql, args).fetchall()]


class transaction:
    def __enter__(self):
        q("BEGIN IMMEDIATE")

    def __exit__(self, exc_type, exc, tb):
        q("ROLLBACK" if exc_type else "COMMIT")
        return False


def event(type_: str, request_id: str | None, caller_id: str | None, data: dict) -> None:
    q("INSERT INTO events (at, type, request_id, caller_id, data) VALUES (?, ?, ?, ?, ?)",
      (now(), type_, request_id, caller_id, json_text(data)))


# ---------------------------------------------------------------- encoding

def json_text(v: object) -> str:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


_B64U = re.compile(r"[A-Za-z0-9_-]*")


def unb64u(s: str) -> bytes:
    if not isinstance(s, str) or not _B64U.fullmatch(s) or len(s) % 4 == 1:
        raise HttpError("invalid base64url")
    try:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    except ValueError:
        raise HttpError("invalid base64url")


def sha256(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()


def token_hash(token: str) -> str:
    """What the database keeps of an enrolment token."""
    return hashlib.sha256(token.encode()).hexdigest()


def strict_eq(a: object, b: object) -> bool:
    """Same type and equal (so True never equals 1)."""
    return type(a) is type(b) and a == b


def is_int(v: object) -> bool:
    return type(v) is int


def _jcs_string(s: str) -> str:
    out = json.dumps(s, ensure_ascii=False)
    # Lone surrogates are escaped the way JSON.stringify does it.
    return re.sub(r"[\ud800-\udfff]", lambda m: "\\u%04x" % ord(m.group()), out)


def jcs(v: object, error: type[Exception] = HttpError) -> str:
    """JSON Canonicalization Scheme (RFC 8785) for the value types hitl uses:
    objects, lists, strings, integers, booleans, null. Floats are rejected."""
    if v is None or isinstance(v, bool):
        return json.dumps(v)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return _jcs_string(v)
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(jcs(x, error) for x in v) + "]"
    if isinstance(v, dict):
        keys = sorted((str(k) for k in v), key=lambda k: k.encode("utf-16-be", "surrogatepass"))
        return "{" + ",".join(_jcs_string(k) + ":" + jcs(v[k], error) for k in keys) + "}"
    raise error("unsupported value in canonical JSON")


def jcs_bytes(v: object) -> bytes:
    return jcs(v).encode("utf-8", "surrogatepass")


# ---------------------------------------------------------------- signed objects

# Every signed object names its type and has exactly these fields (R11): a signature over one kind
# of object can then never pass as another, and nothing unsigned-for rides along in a signed one.
SHAPES = {
    "hitl.request": (2, {"type", "version", "request_id", "caller_id", "recipient_id", "config_ct_hash",
                         "payload_ct_hash", "answer_pubkey", "ttl", "caller_ref", "created_at", "policy", "blobs"}),
    "hitl.blob": (2, {"type", "version", "caller_id", "sha256", "size", "created_at"}),
    "hitl.response": (2, {"type", "version", "request_id", "creation_hash", "config_hash", "payload_hash",
                          "answer_ct", "signed_at"}),
    "hitl.key-statement": (2, {"type", "version", "recipient_id", "epoch", "seq", "prev", "enc_pubkey",
                               "credentials", "issued_at"}),
    "hitl.session": (1, {"type", "version", "recipient_id", "session_pubkey", "issued_at", "expires_at"}),
}


def shaped(o: object, type_: str, extra: tuple = ()) -> bool:
    """True when o is an object of this type and version with exactly its fields (plus extra)."""
    version, fields = SHAPES[type_]
    return (isinstance(o, dict) and o.get("type") == type_ and strict_eq(o.get("version"), version)
            and set(o) == fields | set(extra))


# ---------------------------------------------------------------- WebAuthn

def credential_problem(spki: bytes, alg: object) -> str | None:
    """A credential is ES256 with a P-256 key or RS256 with an RSA key of at least 2048 bits, and the
    declared algorithm must be the key's own: otherwise a key could be verified under rules it was
    not enrolled with."""
    key = load_spki(spki)
    if strict_eq(alg, -7):
        ok = isinstance(key, ec.EllipticCurvePublicKey) and isinstance(key.curve, ec.SECP256R1)
    elif strict_eq(alg, -257):
        ok = isinstance(key, rsa.RSAPublicKey) and key.key_size >= 2048
    else:
        return "unsupported credential algorithm"
    return None if ok else "credential key does not match its algorithm"


def load_spki(spki: bytes):
    """The credential public key, or None when it does not parse."""
    try:
        return serialization.load_der_public_key(spki)
    except (ValueError, TypeError):
        return None


def verify_sha256(spki: bytes, alg: object, data: bytes, sig: bytes) -> bool:
    if credential_problem(spki, alg) is not None:
        return False
    key = load_spki(spki)
    try:
        if alg == -7:
            key.verify(sig, data, ec.ECDSA(hashes.SHA256()))
        else:
            key.verify(sig, data, padding.PKCS1v15(), hashes.SHA256())
    except (InvalidSignature, ValueError):
        return False
    return True


def assertion_problem(a: dict, challenge: bytes, spki: bytes, alg: object, origin_: str, rp_id_: str) -> str | None:
    """Checks a WebAuthn assertion. Returns None when valid, otherwise a rejection reason."""
    for k in ("client_data_json", "authenticator_data", "signature"):
        if not isinstance(a.get(k), str):
            return "assertion_incomplete"
    cdj_raw = unb64u(a["client_data_json"])
    try:
        cdj = json.loads(cdj_raw)
    except ValueError:
        cdj = None
    if not isinstance(cdj, dict) or cdj.get("type") != "webauthn.get":
        return "client_data_type"
    got = cdj.get("challenge")
    if not isinstance(got, str) or not hmac.compare_digest(b64u(challenge), got):
        return "challenge_mismatch"
    if cdj.get("origin") != origin_ or cdj.get("crossOrigin"):
        return "origin_mismatch"
    ad = unb64u(a["authenticator_data"])
    if len(ad) < 37:
        return "authenticator_data"
    if not hmac.compare_digest(sha256(rp_id_.encode()), ad[:32]):
        return "rp_id_mismatch"
    flags = ad[32]
    if not flags & 0x01:
        return "not_user_present"
    if not flags & 0x04:
        return "not_user_verified"
    return None if verify_sha256(spki, alg, ad + sha256(cdj_raw), unb64u(a["signature"])) else "signature_invalid"
