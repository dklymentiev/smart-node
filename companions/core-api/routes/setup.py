"""First-run onboarding -- consume the one-time setup token to create the first operator.

On a fresh install WITHOUT --owner-password, B06 leaves the node in a half-open
state that the browser had no way to finish:
  - the super-admin AuthZ rows are already seeded in the core DB by B06
    (actors / identity_users / user_roles) -- the AuthN half (a password) is not;
  - a one-time token sits at /var/lib/smart-node/panel/setup-token (also printed in
    /etc/smart-node/install-report.md);
  - the Authum operator store users.json is still empty ({}), so no one can log in.

This endpoint is that missing AuthN half. It authenticates the caller with the setup
token (the only credential a fresh node has), then writes the bcrypt password hash into
users.json -- mirroring B06's seed_superadmin_authn -- and re-asserts the AuthZ rows
idempotently. It is deliberately reachable WITHOUT a session: Traefik exempts
/api/setup from the smart-node-auth forwardAuth middleware, exactly as it exempts the
panel's /setup path. The token is the credential and is burned on success, so the
endpoint runs exactly once.

Safety: if an operator password already exists (users.json non-empty) the endpoint
refuses (409) and never overwrites -- this guards against a lingering token. Note the
AuthZ rows are NOT used as the "already set up" signal: B06 pre-seeds them for the
default admin at install time, so the authoritative "setup complete" marker is the
AuthN store (users.json), which is also what B06's superadmin_exists() checks.
"""

import datetime
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time

import bcrypt
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from db import cursor
from permissions import requires

log = logging.getLogger("core-api.setup")

router = APIRouter(prefix="/api/setup", tags=["setup"])

# Paths B06 templates into the panel container env; core-api reads the same files
# directly off the host (native service). Overridable for tests, real defaults here.
SETUP_TOKEN_FILE = os.environ.get(
    "SMART_NODE_SETUP_TOKEN_FILE", "/var/lib/smart-node/panel/setup-token"
)
AUTH_USERS_FILE = os.environ.get(
    "SMART_NODE_AUTH_USERS_FILE", "/var/lib/smart-node/authum/auth/users.json"
)

# Mirror B06's SMART_NODE_SUPERADMIN_USER guard exactly.
USERNAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
MIN_PASSWORD_LEN = 8
SUPERADMIN_DISPLAY = "Super Administrator"


ONETIME_FILE = os.path.join(os.path.dirname(AUTH_USERS_FILE), "onetime.json")
ONETIME_TTL_SECS = 300


class SetupComplete(BaseModel):
    token: str
    username: str = "admin"
    # Empty: the node picks a password nobody sees and signs the person in with a
    # one-time link instead (first-run page). They come back with a passkey or the
    # owner's login command, not with a password they had to invent.
    password: str = ""


def readable_password() -> str:
    """The first password, made to be read off a screen and typed: three groups of four
    from letters and digits that cannot be mistaken for one another (no 0/o, 1/l/i).
    31 symbols, 12 places: about 59 bits, against Authum's lockout after five tries."""
    alphabet = "23456789abcdefghjkmnpqrstuvwxyz"
    return "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3))


def issue_onetime_signin(username: str) -> str:
    """Write a one-time sign-in record for Authum's ?action=onetime and return the token.

    Only the SHA-256 of the token is stored; Authum deletes the record on first use."""
    token = secrets.token_urlsafe(32)
    rec = {"sha256": hashlib.sha256(token.encode()).hexdigest(), "user": username,
           "expires": int(time.time()) + ONETIME_TTL_SECS}
    tmp = ONETIME_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rec, f)
    # 0664: Authum's PHP runs as www-data (uid 82), the folder's owner but not in the
    # smart-node group; on the host the parent is 0750 root:smart-node, so "other" still
    # means nobody outside the node's group. Only a hash, gone in five minutes.
    os.chmod(tmp, 0o664)
    os.replace(tmp, ONETIME_FILE)
    return token


def _read_setup_token() -> str:
    """The one-time token, or '' when the file is absent/empty (setup already done)."""
    try:
        with open(SETUP_TOKEN_FILE, "r") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""
    except OSError as e:
        log.error("cannot read setup token %s: %s", SETUP_TOKEN_FILE, e)
        raise HTTPException(status_code=500, detail="setup token unreadable")


def _load_users() -> dict:
    """The Authum operator store as a dict; {} when absent or unparseable."""
    try:
        with open(AUTH_USERS_FILE, "r") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return {}


@router.post("/complete", response_model=dict,
             dependencies=[Depends(requires("setup.complete"))])
def complete_setup(body: SetupComplete):
    """Create the first operator from the one-time setup token (auth-exempt path).

    The permission is held by no role. It is admitted while the setup window is
    open, which is step 1 below asked by the permission check; the steps here
    are unchanged and still decide whether this caller holds the token.

    See module docstring for the full contract. On any failure the token is left on
    disk so the operator can retry; it is deleted only after the operator exists.
    """
    submitted = (body.token or "").strip()
    username = (body.username or "admin").strip()
    password = body.password or ""
    # No password given: the node invents a readable one, hands it back ONCE to whoever
    # holds the setup token (the first-run page shows it with a copy button), and signs
    # the person in with a one-time link at the end (step 9). Before, it was a hidden
    # token_urlsafe(24) and the owner had a password nobody could ever know.
    sign_in = not password
    if sign_in:
        password = readable_password()

    # 1. Token must be present. Absent/empty -> onboarding already completed or not
    #    available on this node. 409 (not 403) so the UI can say "already set up".
    stored = _read_setup_token()
    if not stored:
        raise HTTPException(status_code=409, detail="setup is not available (already completed?)")

    # 2. Constant-time compare. Mismatch -> 403.
    if not hmac.compare_digest(submitted, stored):
        raise HTTPException(status_code=403, detail="invalid setup token")

    # 3. Re-seed guard: if a password already exists for anyone, refuse. The token
    #    lingering must never let a second operator be minted or the first reset.
    users = _load_users()
    if users:
        raise HTTPException(status_code=409, detail="an operator already exists; setup already completed")

    # 4. Validate inputs.
    if not USERNAME_RE.match(username):
        raise HTTPException(status_code=400, detail="invalid username (lowercase letter, then alnum/_/-)")
    if len(password) < MIN_PASSWORD_LEN:
        raise HTTPException(status_code=400, detail=f"password must be at least {MIN_PASSWORD_LEN} characters")

    # 5. Hash the password. bcrypt output ($2b$) is accepted by PHP password_verify.
    pw_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")

    # 6. AuthZ half -- idempotent, mirrors B06's seed_superadmin_authz. core-api's DB
    #    connection already authenticates as the `smartnode` role that owns these
    #    tables, so no SET ROLE is needed. ON CONFLICT DO NOTHING: B06 usually pre-
    #    seeded these for the default admin, but a non-default username lands here.
    try:
        with cursor(commit=True) as cur:
            cur.execute(
                "INSERT INTO actors (id, display_name, type, layer) "
                "VALUES (%s, %s, 'operator', 'human') ON CONFLICT (id) DO NOTHING",
                (f"human:{username}", SUPERADMIN_DISPLAY),
            )
            cur.execute(
                "INSERT INTO identity_users (username, display_name, actor_id) "
                "VALUES (%s, %s, %s) ON CONFLICT (username) DO NOTHING",
                (username, SUPERADMIN_DISPLAY, f"human:{username}"),
            )
            cur.execute(
                "INSERT INTO user_roles (username, role_id) "
                "VALUES (%s, 'super-admin') ON CONFLICT DO NOTHING",
                (username,),
            )
    except Exception as e:
        log.error("setup: failed to seed AuthZ rows for '%s': %s", username, e)
        raise HTTPException(status_code=500, detail="failed to create operator (AuthZ)")

    # 7. AuthN half -- write the password into the Authum store, preserving any
    #    existing dict (mirrors B06's python writer). users.json is group-writable by
    #    the smart-node group (see B05) so this native service can write it in place.
    users[username] = {
        "password": pw_hash,
        "role": "superadmin",
        "created": datetime.datetime.now().astimezone().isoformat(),
    }
    try:
        with open(AUTH_USERS_FILE, "w") as f:
            json.dump(users, f, indent=2)
    except OSError as e:
        log.error("setup: failed to write %s: %s", AUTH_USERS_FILE, e)
        raise HTTPException(status_code=500, detail="failed to create operator (AuthN)")

    # 8. Burn the token -- setup can run exactly once. If the unlink fails the
    #    operator still exists, and step 3 now guards replay, so this is best-effort.
    try:
        os.remove(SETUP_TOKEN_FILE)
    except OSError as e:
        log.warning("setup: operator created but could not remove token %s: %s", SETUP_TOKEN_FILE, e)

    log.info("setup: first operator '%s' created via setup token", username)

    # 9. One-time sign-in for the first-run page, when the node chose the password.
    if sign_in:
        try:
            return {"ok": True, "signin_token": issue_onetime_signin(username),
                    "username": username, "password": password}
        except OSError as e:
            log.error("setup: operator created but the one-time sign-in failed: %s", e)
            raise HTTPException(status_code=500, detail="account created, but signing in failed")
    return {"ok": True}
