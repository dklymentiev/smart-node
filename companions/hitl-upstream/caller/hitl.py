#!/usr/bin/env python3
"""hitl caller: pin recipient keys, create requests, verify and consume signed answers.

Every command prints exactly one JSON object on stdout. Exit codes:
  0 ok / answered, 10 pending, 3 rejected or expired, 75 hold (service unreachable), 64 usage, 65 bad input.

Environment:
  HITL_URL    service base URL, required (for example https://decide.example.com)
  HITL_STATE  state directory (default ~/.hitl): caller key, pins, requests, consumed answers

Needs PyNaCl and cryptography.
"""

from __future__ import annotations

import base64
import calendar
import hashlib
import hmac
import json
import mimetypes
import os
import re
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid

import nacl.bindings as sodium
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

EXIT_OK, EXIT_REJECTED, EXIT_PENDING, EXIT_USAGE, EXIT_INPUT, EXIT_HOLD = 0, 3, 10, 64, 65, 75
BLOB_THRESHOLD = 2_000_000  # attachments above this size, and all video and audio, go as blobs
BLOB_CHUNK = 65536
CLOCK_SKEW = 300
ANSWER_BLOCK = 4096  # answers are padded to a multiple of this before sealing, so their length says nothing
MAX_INT = 2**53 - 1
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


class Hold(Exception):
    """The service could not be reached or failed. request_id is set when a request of this caller
    may exist at the service all the same."""
    request_id: str | None = None


class Rejected(Exception):
    pass


class UsageError(Exception):
    pass


# ---------------------------------------------------------------- encoding

def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s: object) -> bytes:
    if not isinstance(s, str) or not re.fullmatch(r"[A-Za-z0-9_-]*", s) or len(s) % 4 == 1:
        raise Rejected("malformed_base64")
    try:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    except ValueError:
        raise Rejected("malformed_base64")


def sha256(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()


def json_text(v: object, pretty: bool = False) -> str:
    if pretty:
        return json.dumps(v, ensure_ascii=False, indent=4)
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def strict_eq(a: object, b: object) -> bool:
    return type(a) is type(b) and a == b


def strict_in(v: object, items: list) -> bool:
    return any(strict_eq(v, x) for x in items)


def is_int(v: object) -> bool:
    return type(v) is int


def _jcs_string(s: str) -> str:
    return re.sub(r"[\ud800-\udfff]", lambda m: "\\u%04x" % ord(m.group()), json.dumps(s, ensure_ascii=False))


def jcs(v: object) -> str:
    """JSON Canonicalization Scheme (RFC 8785) for strings, integers, booleans, null, lists, objects."""
    if v is None or isinstance(v, bool):
        return json.dumps(v)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return _jcs_string(v)
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(jcs(x) for x in v) + "]"
    if isinstance(v, dict):
        keys = sorted((str(k) for k in v), key=lambda k: k.encode("utf-16-be", "surrogatepass"))
        return "{" + ",".join(_jcs_string(k) + ":" + jcs(v[k]) for k in keys) + "}"
    raise Rejected("non_canonical_value")


def jcs_bytes(v: object) -> bytes:
    return jcs(v).encode("utf-8", "surrogatepass")


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


_TS = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")


def parse_ts(s: object) -> int | None:
    """Unix seconds for an RFC 3339 timestamp in UTC with a Z suffix, the one form signed objects use; else None."""
    if not isinstance(s, str) or not _TS.fullmatch(s):
        return None
    try:
        t = calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return None
    # One spelling per instant: second 60, hour 24 and the like parse to a time that prints differently.
    return t if t >= 0 and time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)) == s else None


# Every signed object names its type and has exactly these fields (R11): a signature over one kind
# of object can never pass as another, and nothing unsigned-for rides along in a signed one.
SHAPES = {
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


def leading_int(s: object) -> int:
    m = re.match(r"^\s*([+-]?\d+)", s) if isinstance(s, str) else None
    return int(m.group(1)) if m else 0


# ---------------------------------------------------------------- state and transport

def base_url() -> str:
    url = (os.environ.get("HITL_URL") or "").rstrip("/")
    if not url:
        # No default: a caller that guesses its service pins keys from, and sends requests to, the wrong one.
        raise UsageError("HITL_URL is not set: give the base URL of the hitl service")
    return url


def expected_origin() -> str:
    u = urllib.parse.urlsplit(base_url())
    return f"{u.scheme}://{u.hostname}" + (f":{u.port}" if u.port else "")


def state(rel: str) -> str:
    d = os.environ.get("HITL_STATE") or os.path.join(os.path.expanduser("~"), ".hitl")
    path = os.path.join(d, rel)
    os.makedirs(os.path.dirname(path), 0o700, exist_ok=True)
    return path


def read_json(rel: str) -> dict | None:
    p = state(rel)
    if not os.path.isfile(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def create_json(rel: str, data: dict) -> None:
    """Writes a new state file and fails if one is already there: a request's record is made once,
    under the id this caller minted, and nothing can overwrite it."""
    fd = os.open(state(rel), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json_text(data, pretty=True) + "\n")


def write_json(rel: str, data: dict) -> None:
    p = state(rel)
    fd = os.open(p + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json_text(data, pretty=True) + "\n")
    os.chmod(p + ".tmp", 0o600)
    os.replace(p + ".tmp", p)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


# The service is the origin in HITL_URL and nothing else: a redirect is answered as the refusal it is.
_opener = urllib.request.build_opener(_NoRedirect)


def _send(req: urllib.request.Request, timeout: int) -> tuple[int, bytes]:
    try:
        with _opener.open(req, timeout=timeout) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, OSError, ValueError):
        raise Hold("service_unreachable")


def _decode(raw: bytes) -> dict:
    try:
        v = json.loads(raw)
    except ValueError:
        return {}
    return v if isinstance(v, dict) else {}


def http(method: str, path: str, body: dict | None = None, headers: dict | None = None) -> tuple[int, dict]:
    data = json_text(body).encode("utf-8", "surrogatepass") if body is not None else None
    req = urllib.request.Request(base_url() + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", "Accept": "application/json", **(headers or {})})
    status, raw = _send(req, 60 if body is not None else 15)
    if status >= 500 or status == 0:
        raise Hold("service_error")
    return status, _decode(raw)


def caller() -> dict:
    c = read_json("caller.json")
    if c is None:
        raise UsageError("no caller key: run keygen first")
    return c


def sign(msg: bytes, secret_key: bytes) -> bytes:
    return sodium.crypto_sign(msg, secret_key)[:64]


def sign_ok(sig: bytes, msg: bytes, pub: bytes) -> bool:
    if len(sig) != 64 or len(pub) != 32:
        return False
    try:
        VerifyKey(pub).verify(msg, sig)
    except BadSignatureError:
        return False
    return True


# ---------------------------------------------------------------- key pinning (R9, R10)
#
# Nothing about a recipient's keys is taken on the service's word. A pin is the result of replaying
# the recipient's key statement chain: the first statement of an epoch is signed by the one
# credential it lists and is trusted only through the fingerprint the operator compares with the
# recipient over another channel; every later statement names the hash of the one before and is
# signed by a credential of the set before it.

def credential_problem(spki: bytes, alg: object) -> str | None:
    """ES256 with a P-256 key, or RS256 with an RSA key of at least 2048 bits; the declared algorithm
    must be the key's own."""
    try:
        key = serialization.load_der_public_key(spki)
    except (ValueError, TypeError):
        return "credential_key_unreadable"
    if strict_eq(alg, -7):
        ok = isinstance(key, ec.EllipticCurvePublicKey) and isinstance(key.curve, ec.SECP256R1)
    elif strict_eq(alg, -257):
        ok = isinstance(key, rsa.RSAPublicKey) and key.key_size >= 2048
    else:
        return "credential_algorithm_unsupported"
    return None if ok else "credential_key_not_of_its_algorithm"


def statement_hash(st: dict) -> str:
    return b64u(sha256(jcs_bytes(st)))


def key_fingerprint(st: dict) -> str:
    """The first 128 bits of SHA-256 of the latest key statement. Each statement names the hash of the
    one before, so this commits to the whole history of the epoch."""
    hexed = sha256(jcs_bytes(st)).hex()
    return " ".join(hexed[i:i + 4] for i in range(0, 32, 4))


def recipient_arg(recipient_id: str) -> str:
    if not re.fullmatch(r"[a-z0-9._-]{1,64}", recipient_id or ""):
        raise UsageError("recipient id must be 1 to 64 of a-z, 0-9, dot, dash, underscore")
    return recipient_id


def pinned(recipient_id: str) -> dict:
    p = read_json(f"pins/{recipient_arg(recipient_id)}.json")
    if p is None:
        raise UsageError(f"recipient {recipient_id} is not pinned: run pin first")
    return p


CRED_FIELDS = {"id", "spki", "alg", "label"}


def _listed_credentials(st: dict) -> list[dict]:
    """The credential list of a statement, in the one form a statement may give it."""
    creds = st["credentials"]
    if not isinstance(creds, list) or not creds:
        raise Rejected("key_statement_malformed")
    for c in creds:
        if (not isinstance(c, dict) or set(c) != CRED_FIELDS or not isinstance(c["id"], str) or c["id"] == ""
                or not isinstance(c["spki"], str) or not isinstance(c["label"], str)):
            raise Rejected("key_statement_malformed")
        problem = credential_problem(unb64u(c["spki"]), c["alg"])
        if problem is not None:
            raise Rejected("key_statement_" + problem)
    ids = [c["id"] for c in creds]
    if ids != sorted(set(ids)):
        raise Rejected("key_statement_malformed")
    return creds


def apply_statement(pin: dict, item: object) -> None:
    """Advances a pin by one key statement, or refuses. pin holds recipient_id, epoch, seq, head (the
    hash of the statement it stands on, "" before the first), enc_pubkey and credentials."""
    item = item if isinstance(item, dict) else {}
    st = item.get("statement")
    a = item.get("assertion") if isinstance(item.get("assertion"), dict) else {}
    if (not shaped(st, "hitl.key-statement") or st["recipient_id"] != pin["recipient_id"]
            or not strict_eq(st["epoch"], pin["epoch"]) or not strict_eq(st["seq"], pin["seq"] + 1)
            or not strict_eq(st["prev"], pin["head"]) or not isinstance(st["enc_pubkey"], str)
            or len(unb64u(st["enc_pubkey"])) != 32 or parse_ts(st["issued_at"]) is None):
        raise Rejected("key_statement_malformed")
    listed = _listed_credentials(st)
    if pin["seq"] == 0:
        # The first statement of an epoch enrols one credential and is signed by it.
        if len(listed) != 1:
            raise Rejected("key_statement_malformed")
        signers = listed
    else:
        # Rotating the encryption key is not part of the protocol yet: a statement that changes it is refused.
        if not strict_eq(st["enc_pubkey"], pin["enc_pubkey"]):
            raise Rejected("key_statement_changes_encryption_key")
        signers = pin["credentials"]
    want = str(a.get("credential_id") or "")
    signer = next((c for c in signers if hmac.compare_digest(c["id"].encode(), want.encode())), None)
    if signer is None:
        raise Rejected("key_statement_unpinned_signer")
    problem = assertion_problem(a, sha256(jcs_bytes(st)), signer)
    if problem is not None:
        raise Rejected("key_statement_" + problem)
    next_ids = [c["id"] for c in listed]
    for c in pin["credentials"]:
        if c["id"] not in next_ids:
            pin.setdefault("history", []).append({**c, "retired_at": st["issued_at"], "retired_seq": st["seq"]})
    pin.update(credentials=listed, enc_pubkey=st["enc_pubkey"], seq=st["seq"], head=statement_hash(st),
               fingerprint=key_fingerprint(st))


def replay(pin: dict) -> bool:
    """Fetches and applies every statement after the pin, then requires the key set the service
    presents as current to be exactly the result. True when the pin moved."""
    rid = urllib.parse.quote(pin["recipient_id"], safe="")
    status, res = http("GET", f"/api/v1/recipients/{rid}/statements?after={pin['seq']}")
    if status != 200:
        raise Rejected("statements_unavailable")
    items = res.get("statements")
    items = items if isinstance(items, list) else []
    for item in items:
        apply_statement(pin, item)
    status, keys = http("GET", f"/api/v1/recipients/{rid}/keys")
    if status != 200:
        raise Rejected("unknown_recipient")
    if not strict_eq(keys.get("epoch"), pin["epoch"]):
        # A reset by the operator. A caller never follows a change of epoch on its own.
        raise Rejected("epoch_changed_pin_again")
    if (pin["seq"] == 0 or not strict_eq(keys.get("enc_pubkey"), pin["enc_pubkey"])
            or not isinstance(keys.get("credentials"), list) or jcs(keys["credentials"]) != jcs(pin["credentials"])):
        raise Rejected("keys_changed_without_statement")
    return bool(items)


def refresh_pin(recipient_id: str) -> dict:
    """Follows key statements published after the pinned one. Credentials that drop out stay in the
    pin history with the time they were retired, so answers signed before that still verify."""
    pin = pinned(recipient_id)
    if replay(pin):
        pin["refreshed_at"] = now()
        write_json(f"pins/{recipient_id}.json", pin)
    return pin


# ---------------------------------------------------------------- verification

def _verify_sha256(spki: bytes, alg: object, data: bytes, sig: bytes) -> bool:
    if credential_problem(spki, alg) is not None:
        return False
    try:
        key = serialization.load_der_public_key(spki)
        if alg == -7:
            key.verify(sig, data, ec.ECDSA(hashes.SHA256()))
        else:
            key.verify(sig, data, padding.PKCS1v15(), hashes.SHA256())
    except (InvalidSignature, ValueError, TypeError):
        return False
    return True


def assertion_problem(a: dict, challenge: bytes, cred: dict) -> str | None:
    """Checks a WebAuthn assertion by a credential {spki, alg}: type, challenge, origin, rpId, user
    presence and verification, and the signature under the credential's own algorithm."""
    cdj_raw = unb64u(str(a.get("client_data_json") or ""))
    try:
        cdj = json.loads(cdj_raw)
    except ValueError:
        cdj = None
    if not isinstance(cdj, dict) or cdj.get("type") != "webauthn.get":
        return "client_data_type"
    got = cdj.get("challenge")
    if not isinstance(got, str) or not hmac.compare_digest(b64u(challenge).encode(), got.encode()):
        return "challenge_mismatch"
    if cdj.get("origin") != expected_origin() or cdj.get("crossOrigin"):
        return "origin_mismatch"
    ad = unb64u(str(a.get("authenticator_data") or ""))
    host = urllib.parse.urlsplit(base_url()).hostname or ""
    if len(ad) < 37 or not hmac.compare_digest(sha256(host.encode()), ad[:32]):
        return "rp_id_mismatch"
    if not ad[32] & 0x01:
        return "not_user_present"
    if not ad[32] & 0x04:
        return "not_user_verified"
    sig = unb64u(str(a.get("signature") or ""))
    return None if _verify_sha256(unb64u(cred["spki"]), cred["alg"], ad + sha256(cdj_raw), sig) else "signature_invalid"


def session_problem(proof: dict, record: dict, local: dict, cred: dict) -> None:
    """A session-confirmed answer: the device (pinned credential) signed a delegation to a session key,
    the session key signed the record, and the caller's own policy allows sessions of that age."""
    policy = local["body"]["policy"]
    if policy["assurance"] != "session":
        raise Rejected("fresh_confirmation_required")
    d = proof.get("delegation")
    if not shaped(d, "hitl.session") or d["recipient_id"] != local["recipient_id"]:
        raise Rejected("session_delegation_malformed")
    da = proof.get("delegation_assertion")
    problem = assertion_problem(da if isinstance(da, dict) else {}, sha256(jcs_bytes(d)), cred)
    if problem is not None:
        raise Rejected("session_" + problem)
    issued = parse_ts(d.get("issued_at"))
    expires = parse_ts(d.get("expires_at"))
    signed = parse_ts(record.get("signed_at"))
    if (issued is None or expires is None or signed is None or expires - issued > 86400 + CLOCK_SKEW
            or signed < issued - CLOCK_SKEW or signed > expires):
        raise Rejected("session_not_valid_at_signing")
    if policy.get("max_session_age") is not None and signed - issued > policy["max_session_age"]:
        raise Rejected("session_too_old")
    if not sign_ok(unb64u(str(proof.get("signature") or "")), jcs_bytes(record), unb64u(str(d.get("session_pubkey") or ""))):
        raise Rejected("session_signature_invalid")


def _list(v: object) -> list:
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        return list(v.values())
    return []


def _values(items: object) -> list:
    return [x["value"] for x in _list(items) if isinstance(x, dict) and "value" in x]


def answer_problem(answer: object, schema: dict) -> str | None:
    """The answer must fit the caller's own schema exactly: a declared choice (or none when the
    schema has no choices), declared fields only with values of their declared type, and an
    optional comment. Anything else is refused."""
    if not isinstance(answer, dict):
        return "schema_mismatch"
    if any(k not in ("choice", "fields", "comment") for k in answer):
        return "schema_mismatch"
    choices = _values(schema.get("choices"))
    # A person can cancel any request, including one whose caller did not
    # declare that choice. The page signs this explicit answer.
    cancel = strict_eq(answer.get("choice"), "cancel")
    if not cancel and ((not strict_in(answer.get("choice"), choices)) if choices else ("choice" in answer)):
        return "schema_mismatch"
    if "comment" in answer:
        c = schema.get("comment")
        limit = c.get("max_length") if isinstance(c, dict) else None
        if not c or not isinstance(answer["comment"], str) or len(answer["comment"]) > (limit if is_int(limit) else 0):
            return "schema_mismatch"
    fields = _list(schema.get("fields"))
    if not fields or cancel:
        return "schema_mismatch" if "fields" in answer else None
    given = answer.get("fields")
    if not isinstance(given, dict):
        return "schema_mismatch"
    by_name = {f.get("name"): f for f in fields if isinstance(f, dict)}
    for name, value in given.items():
        if name not in by_name or field_problem(by_name[name], value):
            return "schema_mismatch"
    for f in fields:
        if isinstance(f, dict) and f.get("required") and f.get("name") not in given:
            return "schema_mismatch"
    return None


def field_problem(f: dict, v: object) -> bool:
    options = _values(f.get("options"))
    t = f.get("type") if f.get("type") is not None else "text"
    if t == "checkbox":
        return not isinstance(v, bool) or (bool(f.get("required")) and v is not True)
    if t == "number":
        return (not is_int(v) or (f.get("min") is not None and v < f["min"])
                or (f.get("max") is not None and v > f["max"]))
    if t == "date":
        return not isinstance(v, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", v)
    if t == "select":
        return not strict_in(v, options)
    if t == "multiselect":
        if not isinstance(v, list) or not v or len({json.dumps(x, sort_keys=True) for x in v}) != len(v):
            return True
        return any(not strict_in(x, options) for x in v)
    limit = f.get("max_length") if f.get("max_length") is not None else 2000
    return not isinstance(v, str) or v == "" or len(v) > limit


FIELD_TYPES = ("text", "textarea", "number", "date", "select", "multiselect", "checkbox")


def _bad_number(v: object) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, float) or (isinstance(v, int) and abs(v) > MAX_INT):
        return True
    if isinstance(v, dict):
        return any(_bad_number(x) for x in v.values())
    return isinstance(v, list) and any(_bad_number(x) for x in v)


def schema_problem(schema: object) -> str | None:
    """A schema the page can render and the caller can check: fields with unique names, known types,
    options where needed; choices with unique values; whole numbers only."""
    if not isinstance(schema, dict) or not schema:
        return "answers must be an object"
    if _bad_number(schema):
        # A fraction or a number past 2^53 - 1 has no canonical encoding here, so no answer could verify.
        return "every number must be a whole number of magnitude at most 2^53 - 1"
    if any(k not in ("choices", "fields", "comment") for k in schema):
        return "answers may hold choices, fields and comment"
    if "comment" in schema:
        c = schema["comment"]
        if not isinstance(c, dict) or set(c) != {"max_length"} or not is_int(c["max_length"]) or not 1 <= c["max_length"] <= 2000:
            return "comment must be {max_length: 1 to 2000}"
    values: list = []
    for c in _list(schema.get("choices")):
        if (not isinstance(c, dict) or not isinstance(c.get("value"), str) or not isinstance(c.get("label"), str)
                or c["value"] in values):
            return "each choice needs a unique string value and a label"
        values.append(c["value"])
    names: list = []
    for f in _list(schema.get("fields")):
        if not isinstance(f, dict):
            return "each field needs a unique name, a label and a known type"
        t = f.get("type") if f.get("type") is not None else "text"
        name = f.get("name")
        if (not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", name) or name in names
                or not isinstance(f.get("label"), str) or t not in FIELD_TYPES):
            return "each field needs a unique name, a label and a known type"
        if t in ("select", "multiselect"):
            options = f.get("options")
            # The page reads an option back as the string in its control: only strings can be answered.
            if (not isinstance(options, list) or not options
                    or any(not isinstance(o, dict) or not isinstance(o.get("value"), str) or o["value"] == ""
                           or not isinstance(o.get("label"), str) for o in options)
                    or len({o["value"] for o in options}) != len(options)):
                return f"field {name} needs options with unique string values and labels"
        if t in ("text", "textarea") and "max_length" in f and (not is_int(f["max_length"]) or not 1 <= f["max_length"] <= 2000):
            return f"field {name}: max_length must be 1 to 2000"
        if t == "number" and any(k in f and not is_int(f[k]) for k in ("min", "max")):
            return f"field {name}: min and max must be whole numbers"
        names.append(name)
    if not _list(schema.get("choices")) and not _list(schema.get("fields")):
        return "answers need choices, fields, or both"
    return None


def deadline(local: dict) -> int:
    """When the request expires: created_at + ttl, by this caller's own clock and its own record."""
    return parse_ts(local["body"]["created_at"]) + local["body"]["ttl"]


def unpad(padded: bytes) -> bytes:
    """Removes ISO/IEC 7816-4 padding to a multiple of ANSWER_BLOCK (libsodium's sodium_pad)."""
    try:
        if len(padded) == 0 or len(padded) % ANSWER_BLOCK:
            raise ValueError
        return sodium.sodium_unpad(padded, ANSWER_BLOCK)
    except Exception:
        raise Rejected("answer_padding")


def open_answer(local: dict, sealed: object, schema: dict) -> dict:
    """Opens the answer with the key made for this request and requires it to be padded, canonical
    and inside the caller's own schema."""
    try:
        padded = sodium.crypto_box_seal_open(unb64u(sealed), unb64u(local["answer_pubkey"]), unb64u(local["answer_secret_key"]))
    except Exception:
        raise Rejected("answer_not_sealed_to_this_request")
    plain = unpad(padded)
    try:
        answer = json.loads(plain)
    except ValueError:
        raise Rejected("answer_not_canonical")
    if not isinstance(answer, dict) or _bad_number(answer) or jcs_bytes(answer) != plain:
        raise Rejected("answer_not_canonical")
    problem = answer_problem(answer, schema)
    if problem is not None:
        raise Rejected(problem)
    return answer


def verify(request_id: str) -> tuple[str, dict | None, dict | None, dict | None]:
    """Runs every check of the spec's "Verifying an answer" except single use.
    Returns (status, record, assertion, answer); status is pending, expired or answered."""
    if not re.fullmatch(UUID, request_id or ""):
        raise UsageError("unknown request id (not created from this state directory)")
    local = read_json(f"requests/{request_id}.json")
    if local is None:
        raise UsageError("unknown request id (not created from this state directory)")
    clock = int(time.time())
    seen = local.get("seen_at")
    if seen is None and clock > deadline(local) + CLOCK_SKEW:
        # Past the deadline with no verified answer seen in time: whatever arrives now is refused
        # anyway, so the outcome does not depend on reaching the service.
        return "expired", None, None, None
    status, remote = http("GET", "/api/v1/requests/" + request_id)
    if status != 200:
        raise Rejected("unknown_request")
    response = remote.get("response")
    if response is None:
        return ("expired" if clock > deadline(local) else "pending"), None, None, None
    record = response.get("record") if isinstance(response, dict) else None
    assertion = response.get("assertion") if isinstance(response, dict) else None
    assertion = assertion if isinstance(assertion, dict) else {}
    policy = local["body"]["policy"]
    if not shaped(record, "hitl.response", ("answer_self_ct",) if policy["review"] else ()):
        raise Rejected("malformed_record")

    manifest = unb64u(local["manifest"])
    expect = {
        "request_id": request_id,
        "creation_hash": local["creation_hash"],
        "config_hash": b64u(sha256(manifest)),
        "payload_hash": b64u(sha256(unb64u(local["payload"]))),
    }
    for k, v in expect.items():
        if not strict_eq(record[k], v):
            raise Rejected("record_mismatch:" + k)
    created = parse_ts(local["body"]["created_at"])
    signed = parse_ts(record["signed_at"])
    if signed is None or signed < created - CLOCK_SKEW or signed > deadline(local):
        raise Rejected("signed_outside_the_request_lifetime")
    # An approval held back is not an approval: it must reach this caller by its own deadline.
    if (seen if seen is not None else clock) > deadline(local) + CLOCK_SKEW:
        raise Rejected("answer_arrived_after_the_deadline")

    def find(pin: dict) -> dict | None:
        id_ = str(assertion.get("credential_id") or "").encode()
        for c in pin["credentials"]:
            if hmac.compare_digest(c["id"].encode(), id_):
                return c
        # A retired device still verifies what it signed before it was retired.
        for c in pin.get("history") or []:
            if hmac.compare_digest(c["id"].encode(), id_) and signed < (parse_ts(c["retired_at"]) or 0):
                return c
        return None

    cred = find(pinned(local["recipient_id"]))
    if cred is None:
        cred = find(refresh_pin(local["recipient_id"]))
    if cred is None:
        raise Rejected("unpinned_key")
    if assertion.get("kind") == "session":
        session_problem(assertion, record, local, cred)
    else:
        problem = assertion_problem(assertion, sha256(jcs_bytes(record)), cred)
        if problem is not None:
            raise Rejected(problem)
    answer = open_answer(local, record["answer_ct"], json.loads(manifest)["answers"])
    if seen is None:
        write_json(f"requests/{request_id}.json", {**local, "seen_at": clock})
    return "answered", record, assertion, answer


def answered_output(request_id: str, local: dict, record: dict, assertion: dict, answer: dict) -> dict:
    schema = json.loads(unb64u(local["manifest"]))["answers"]
    choice = answer.get("choice")
    label = "Submitted" if choice is None else "Cancel" if strict_eq(choice, "cancel") else ""
    for c in _list(schema.get("choices")):
        if strict_eq(c.get("value"), choice):
            label = c["label"]
    return {
        "status": "answered",
        "request_id": request_id,
        "recipient_id": local["recipient_id"],
        "caller_ref": local["body"]["caller_ref"],
        "answer": answer,
        "answer_label": label,
        "signed_at": record["signed_at"],
        "credential_id": assertion["credential_id"],
        "assurance": "session" if assertion.get("kind") == "session" else "fresh",
        # The signed record and its assertion. Anyone holding the recipient's pinned keys can re-check
        # the signature; showing what was approved also takes the plaintext this caller kept.
        # proof_token is the same thing as one string, for storing next to the decision.
        "proof": {"record": record, "assertion": assertion},
        "proof_token": "hitl-proof-v2." + b64u(jcs_bytes({"record": record, "assertion": assertion})),
    }


# ---------------------------------------------------------------- commands

def parse_args(args: list[str], values: tuple = (), flags: tuple = (), repeated: tuple = ()) -> tuple[dict, list[str]]:
    """Options and positional arguments in one pass. Whatever follows a value option is its value,
    even if it looks like an option; an option that is not known is an error."""
    opts: dict = {}
    rest: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        name = a[2:] if a.startswith("--") else None
        if name in values or name in repeated:
            if i + 1 >= len(args):
                raise UsageError(f"--{name} needs a value")
            if name in repeated:
                opts.setdefault(name, []).append(args[i + 1])
            else:
                opts[name] = args[i + 1]
            i += 2
        elif name in flags:
            opts[name] = True
            i += 1
        elif name is not None:
            raise UsageError(f"unknown option --{name}")
        else:
            rest.append(a)
            i += 1
    return opts, rest


def cmd_keygen(args: list[str]) -> dict:
    id_ = args[0] if args else ""
    if not re.fullmatch(r"[a-z0-9._-]{1,64}", id_):
        raise UsageError("usage: keygen <caller_id>")
    if read_json("caller.json") is not None:
        raise UsageError("caller key already exists in this state directory")
    pk, sk = sodium.crypto_sign_keypair()
    write_json("caller.json", {"caller_id": id_, "secret_key": b64u(sk)})
    return {"status": "ok", "caller_id": id_, "pubkey": b64u(pk)}


def cmd_pin(args: list[str]) -> dict:
    """Pins a recipient by replaying the key statement chain of the current epoch from its first
    statement and comparing the fingerprint of the result with what the recipient reads out from
    their own device. That comparison is the whole of the trust, so there is no way to skip it:
    give the value with --fingerprint, or confirm it at the prompt."""
    if "--yes" in args:
        raise UsageError("pin has no --yes: pass --fingerprint with the value the recipient read to you")
    opts, rest = parse_args(args, values=("fingerprint",), flags=("replace",))
    replace = bool(opts.get("replace"))
    given = opts.get("fingerprint")
    id_ = recipient_arg(rest[0] if rest else "")
    if read_json(f"pins/{id_}.json") is not None and not replace:
        # Already pinned: follow signed key statements instead of trusting the service again.
        pin = refresh_pin(id_)
        return {"status": "pinned", "recipient_id": id_, "fingerprint": pin["fingerprint"], "epoch": pin["epoch"],
                "seq": pin["seq"], "devices": len(pin["credentials"])}
    status, keys = http("GET", f"/api/v1/recipients/{urllib.parse.quote(id_, safe='')}/keys")
    if status != 200:
        raise Rejected("unknown_recipient")
    if not is_int(keys.get("epoch")) or keys["epoch"] < 1:
        raise Rejected("keys_malformed")
    # The epoch is the service's word and stays so: what makes it safe to pin is the fingerprint.
    pin = {"recipient_id": id_, "epoch": keys["epoch"], "seq": 0, "head": "", "enc_pubkey": None,
           "credentials": [], "history": []}
    replay(pin)
    fp = pin["fingerprint"]

    def plain(x: str) -> bytes:
        return re.sub(r"\s+", "", x).lower().encode()

    if given is not None:
        if not hmac.compare_digest(plain(given), plain(fp)):
            raise Rejected("fingerprint_mismatch")
    else:
        # The prompt is for a person looking at both values. Anything else gives the value itself.
        if not sys.stdin.isatty():
            raise UsageError("no terminal to confirm at: pass --fingerprint with the value the recipient read to you")
        sys.stderr.write(f"Fingerprint for {id_}: {fp}\nConfirm it matches what the recipient's device shows [y/N]: ")
        sys.stderr.flush()
        if sys.stdin.readline().strip().lower() != "y":
            raise Rejected("fingerprint_not_confirmed")
    pin["pinned_at"] = now()
    write_json(f"pins/{id_}.json", pin)
    return {"status": "pinned", "recipient_id": id_, "fingerprint": fp, "epoch": pin["epoch"], "seq": pin["seq"],
            "devices": len(pin["credentials"])}


def cmd_ask(args: list[str]) -> dict:
    opts, rest = parse_args(args, values=("title", "text", "html-file", "choices", "comment", "ttl", "ref", "manifest",
                                          "assurance", "max-session-age"), flags=("review",), repeated=("attach",))
    title = opts.get("title", "")
    text = opts.get("text", "")
    html_file = opts.get("html-file")
    choices_spec = opts.get("choices", "A:Approve,B:Reject,cancel:Cancel")
    comment_max = leading_int(opts.get("comment", "0"))
    ttl = leading_int(opts.get("ttl", "86400"))
    ref = opts.get("ref", "")
    policy = policy_opts(opts)
    manifest_file = opts.get("manifest")
    attachments = opts.get("attach", [])
    recipient_id = rest[0] if rest else ""
    if manifest_file is not None:
        return ask_with_manifest(recipient_id, manifest_file, attachments, ttl, ref, policy)
    if recipient_id == "" or (title == "" and text == ""):
        raise UsageError("usage: ask <recipient_id> --title T --text X [--html-file F] [--choices A:Send,B:Edit,cancel:Cancel] "
                         "[--comment 500] [--ttl 86400] [--ref opaque-reference]")
    choices = []
    for pair in choices_spec.split(","):
        value, _, label = pair.partition(":")
        if value not in ("A", "B", "cancel") or label == "":
            raise UsageError("choices must be A, B and cancel with labels")
        choices.append({"value": value, "label": label})
    schema: dict = {"choices": choices}
    if comment_max > 0:
        schema["comment"] = {"max_length": min(comment_max, 2000)}
    blocks: list = [{"type": "text", "text": text}] if text else []
    if html_file:
        with open(html_file, encoding="utf-8") as f:
            blocks.append({"type": "html", "html": f.read()})
    return send_request(recipient_id, {"title": title, "blocks": blocks, "answers": schema}, {}, ttl, ref, policy)


def ask_with_manifest(recipient_id: str, file: str, attachments: list[str], ttl: int, ref: str, policy: dict) -> dict:
    """Content blocks (text, html, image, pdf, video, audio, file) and an answer schema with choices
    and/or form fields. Attachments are given as ref=path and travel encrypted in the payload."""
    try:
        with open(file, encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, ValueError):
        m = None
    if not isinstance(m, dict) or not isinstance(m.get("title"), str) or not isinstance(m.get("blocks"), list):
        raise UsageError("manifest needs a title, blocks and answers")
    problem = schema_problem(m.get("answers"))
    if problem is not None:
        raise UsageError("answers: " + problem)
    files: dict = {}
    blob_ids = []
    for spec in attachments:
        name, _, path = spec.partition("=")
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            data = None
        if not re.fullmatch(r"[a-zA-Z0-9._-]{1,64}", name) or data is None:
            raise UsageError(f"attachment {spec}: expected ref=path to a readable file")
        mime = mime_of(path)
        if len(data) > BLOB_THRESHOLD or mime.startswith("video/") or mime.startswith("audio/"):
            files[name] = upload_blob(path)
            blob_ids.append(files[name]["blob"]["id"])
        else:
            files[name] = {"mime": mime, "name": os.path.basename(path), "data": b64u(data)}
    for b in m["blocks"]:
        t = b.get("type") if isinstance(b, dict) else None
        if t not in ("text", "html", "image", "pdf", "video", "audio", "file"):
            raise UsageError(f"unknown block type {t if isinstance(t, str) else '?'}")
        if b.get("ref") is not None and b["ref"] not in files:
            raise UsageError(f"block refers to missing attachment {b['ref']}")
    if any(k in m for k in ("version", "salt", "request", "payload_hash")):
        raise UsageError("version, salt, request and payload_hash are written by this program, not by the manifest file")
    if m.get("layout", "standard") not in ("standard", "full"):
        raise UsageError("layout must be standard or full")
    return send_request(recipient_id, m, files, ttl, ref, policy, blob_ids)


def upload_blob(path: str) -> dict:
    """Encrypts a file in chunks with a fresh secretstream key, uploads the ciphertext as a blob with a
    signed header, and returns the payload entry that carries the key (inside the encrypted payload)."""
    c = caller()
    size = os.path.getsize(path)
    key = sodium.crypto_secretstream_xchacha20poly1305_keygen()
    st = sodium.crypto_secretstream_xchacha20poly1305_state()
    header = sodium.crypto_secretstream_xchacha20poly1305_init_push(st, key)
    h = hashlib.sha256()
    with tempfile.TemporaryFile() as out:
        with open(path, "rb") as src:
            read = 0
            while True:
                chunk = src.read(BLOB_CHUNK)
                read += len(chunk)
                final = read >= size or chunk == b""
                tag = (sodium.crypto_secretstream_xchacha20poly1305_TAG_FINAL if final
                       else sodium.crypto_secretstream_xchacha20poly1305_TAG_MESSAGE)
                ct = sodium.crypto_secretstream_xchacha20poly1305_push(st, chunk, None, tag)
                h.update(ct)
                out.write(ct)
                if final:
                    break
        ct_size = out.tell()
        out.seek(0)
        hdr = {"type": "hitl.blob", "version": 2, "caller_id": c["caller_id"], "sha256": h.hexdigest(),
               "size": ct_size, "created_at": now()}
        sig = sign(jcs_bytes(hdr), unb64u(c["secret_key"]))
        req = urllib.request.Request(base_url() + "/api/v1/blobs", data=out, method="PUT", headers={
            "Content-Type": "application/octet-stream", "Content-Length": str(ct_size),
            "X-Hitl-Blob": b64u(jcs_bytes(hdr)), "X-Hitl-Signature": b64u(sig)})
        status, raw = _send(req, 600)
    res = _decode(raw)
    if status != 201 or "blob_id" not in res:
        raise Rejected("blob_refused:" + str(res.get("error") or status))
    return {"mime": mime_of(path), "name": os.path.basename(path), "size": size, "blob": {
        "id": res["blob_id"], "sha256": hdr["sha256"], "size": hdr["size"],
        "key": b64u(key), "header": b64u(header), "chunk": BLOB_CHUNK,
    }}


MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif",
        "webp": "image/webp", "pdf": "application/pdf", "html": "text/html", "htm": "text/html",
        "txt": "text/plain", "csv": "text/csv", "mp4": "video/mp4", "webm": "video/webm",
        "mov": "video/quicktime", "mp3": "audio/mpeg", "m4a": "audio/mp4", "wav": "audio/wav",
        "ogg": "audio/ogg",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}


def mime_of(path: str) -> str:
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    return MIME.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream"


def policy_opts(opts: dict) -> dict:
    """--assurance session|fresh and --max-session-age 12h (or seconds): the caller's policy for how the
    answer must be confirmed; fresh unless said otherwise. --review lets the recipient open the request
    again after answering and see their answer (read-only). The policy is always sent, in one form."""
    assurance = opts.get("assurance") or "fresh"
    max_age = opts.get("max-session-age")
    if assurance not in ("session", "fresh"):
        raise UsageError("--assurance must be session or fresh")
    policy: dict = {"assurance": assurance, "review": bool(opts.get("review"))}
    if max_age is not None:
        if assurance != "session":
            raise UsageError("--max-session-age only applies to --assurance session")
        m = re.fullmatch(r"([0-9]+)h", max_age)
        seconds = int(m.group(1)) * 3600 if m else leading_int(max_age)
        if seconds < 60 or seconds > 86400:
            raise UsageError("--max-session-age must be 60 s to 24 h")
        policy["max_session_age"] = seconds
    return policy


def send_request(recipient_id: str, manifest: dict, files: dict, ttl: int, ref: str, policy: dict,
                 blob_ids: list[str] | None = None) -> dict:
    """Creates a request. This caller mints the id, the answer key and the times, so nothing about
    the request's identity or lifetime is the service's to choose (R10). The same facts go into
    the signed creation body, which the service sees, and into the sealed manifest, which only the
    recipient's page sees and acts on."""
    c = caller()
    pin = refresh_pin(recipient_id)
    if not is_int(ttl) or ttl < 1:
        raise UsageError("--ttl must be a number of seconds")
    if len(ref) > 200 or not re.fullmatch(r"[\x20-\x7e]*", ref):
        raise UsageError("--ref must be at most 200 printable ASCII characters")
    problem = schema_problem(manifest.get("answers"))
    if problem is not None:
        raise UsageError("answers: " + problem)
    request_id = str(uuid.uuid4())
    answer_pk, answer_sk = sodium.crypto_box_keypair()
    created = now()
    facts = {"request_id": request_id, "caller_id": c["caller_id"], "caller_ref": ref, "policy": policy,
             "answer_pubkey": b64u(answer_pk), "created_at": created, "ttl": ttl}
    # The salts make the plaintext hashes in the signed record confirm nobody's guess about the content.
    # Manifest and payload also name each other, by values only someone who can open them knows: the
    # payload carries the manifest's salt and the manifest the payload's hash. A sealed box says
    # nothing about who sealed it, so without this a service could put a manifest of its own, with
    # its own answer key, in front of the caller's attachments.
    manifest_salt = b64u(os.urandom(32))
    payload_b = json_text({"salt": b64u(os.urandom(32)), "manifest_salt": manifest_salt, "files": files}).encode("utf-8", "surrogatepass")
    manifest_b = json_text({**manifest, "version": 3, "salt": manifest_salt, "payload_hash": b64u(sha256(payload_b)),
                            "request": facts}).encode("utf-8", "surrogatepass")
    enc_pub = unb64u(pin["enc_pubkey"])
    config_ct = sodium.crypto_box_seal(manifest_b, enc_pub)
    payload_ct = sodium.crypto_box_seal(payload_b, enc_pub)
    body = {
        "type": "hitl.request",
        "version": 2,
        "request_id": request_id,
        "caller_id": c["caller_id"],
        "recipient_id": recipient_id,
        "config_ct_hash": b64u(sha256(config_ct)),
        "payload_ct_hash": b64u(sha256(payload_ct)),
        "answer_pubkey": b64u(answer_pk),
        "ttl": ttl,
        "caller_ref": ref,
        "created_at": created,
        "policy": policy,
        "blobs": blob_ids or [],
    }
    canonical = jcs_bytes(body)
    sig = b64u(sign(canonical, unb64u(c["secret_key"])))
    rel = f"requests/{request_id}.json"
    local = {
        "request_id": request_id,
        "recipient_id": recipient_id,
        "body": body,
        "creation_hash": b64u(sha256(canonical)),
        "caller_signature": sig,
        "manifest": b64u(manifest_b),
        "payload": b64u(payload_b),
        "answer_pubkey": b64u(answer_pk),
        "answer_secret_key": b64u(answer_sk),
    }
    # The record exists before the service hears of the request, under this caller's own id, and is
    # created exclusively: nothing the service returns can name, overwrite or redirect it.
    create_json(rel, local)
    try:
        status, res = http("POST", "/api/v1/requests", {"config_ct": b64u(config_ct), "payload_ct": b64u(payload_ct)},
                           {"X-Hitl-Body": b64u(canonical), "X-Hitl-Signature": sig})
        if status != 201:
            raise Rejected("create_refused:" + str(res.get("error") or status))
        link = res.get("short_link")
        # The link goes to a person, so it must lead to this service and nowhere else.
        if not isinstance(link, str) or not re.fullmatch(re.escape(expected_origin()) + r"/r/[A-Za-z0-9_-]{16}", link):
            raise Rejected("create_returned_a_foreign_link")
    except Rejected:
        os.unlink(state(rel))
        raise
    except Hold as e:
        # The request may have reached the service. The record stays, so an answer can still be
        # checked under this id, and the id is reported: asking again would be a second request.
        e.request_id = request_id
        raise
    write_json(rel, {**local, "link": link})
    return {"status": "created", "request_id": request_id, "link": link,
            "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(parse_ts(created) + ttl))}


def cmd_refresh(args: list[str]) -> dict:
    pin = refresh_pin(args[0] if args else "")
    return {"status": "pinned", "recipient_id": pin["recipient_id"], "fingerprint": pin["fingerprint"],
            "epoch": pin["epoch"], "seq": pin["seq"], "devices": len(pin["credentials"])}


def cmd_check(args: list[str]) -> dict:
    id_ = args[0] if args else ""
    status, record, assertion, answer = verify(id_)
    if status != "answered":
        return {"status": status, "request_id": id_}
    out = answered_output(id_, read_json(f"requests/{id_}.json"), record, assertion, answer)
    out["consumed"] = os.path.isfile(state(f"consumed/{id_}.json"))
    return out


def cmd_consume(args: list[str]) -> dict:
    id_ = args[0] if args else ""
    status, record, assertion, answer = verify(id_)
    if status != "answered":
        return {"status": status, "request_id": id_}
    # Exactly once (R6b): creating the marker is atomic; a second consume finds it.
    try:
        fh = open(state(f"consumed/{id_}.json"), "x", encoding="utf-8")
    except FileExistsError:
        raise Rejected("replay")
    local = read_json(f"requests/{id_}.json")
    # Audit copy: the signed record, the assertion, the answer it sealed and the plaintext that was shown.
    with fh:
        fh.write(json_text({
            "consumed_at": now(), "record": record, "record_jcs": jcs(record), "assertion": assertion,
            "answer": answer, "creation_body": local["body"], "manifest": local["manifest"], "payload": local["payload"],
        }, pretty=True) + "\n")
    return answered_output(id_, local, record, assertion, answer)


# ---------------------------------------------------------------- main

COMMANDS = {"keygen": cmd_keygen, "pin": cmd_pin, "refresh": cmd_refresh, "ask": cmd_ask,
            "check": cmd_check, "consume": cmd_consume}


def main(argv: list[str]) -> int:
    args = argv[1:]
    cmd = args.pop(0) if args else ""

    def emit(out: dict) -> None:
        sys.stdout.write(json_text(out) + "\n")

    try:
        if cmd not in COMMANDS:
            raise UsageError("usage: hitl.py keygen|pin|refresh|ask|check|consume ...")
        out = COMMANDS[cmd](args)
        emit(out)
        return {"pending": EXIT_PENDING, "expired": EXIT_REJECTED}.get(out["status"], EXIT_OK)
    except Hold as e:
        # Degraded mode: never proceed, never invent an answer.
        emit({"status": "hold", "reason": str(e), **({"request_id": e.request_id} if e.request_id else {})})
        return EXIT_HOLD
    except Rejected as e:
        emit({"status": "rejected", "reason": str(e)})
        return EXIT_REJECTED
    except UsageError as e:
        emit({"status": "error", "reason": str(e)})
        return EXIT_USAGE if cmd not in COMMANDS else EXIT_INPUT
    except Exception:
        # A bug or unreadable state: never proceed on it.
        traceback.print_exc()
        emit({"status": "hold", "reason": "caller_error"})
        return EXIT_HOLD


if __name__ == "__main__":
    sys.exit(main(sys.argv))
