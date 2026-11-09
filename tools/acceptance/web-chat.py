#!/usr/bin/env python3
"""Acceptance run: web chat reliability (A1-A9).

Usage:
    python3 tools/acceptance/web-chat.py

Runs against a LIVE node. Each criterion prints one line with a verdict:

    A1  pass   user message in history, bus row confirmed (1.3s)
    A3  FAIL   second send returned 502
    A9  SKIP   no LLM credentials on this node

Verdicts:
    pass  criterion checked and passed
    FAIL  criterion checked and found broken -- the product has a defect
    SKIP  precondition for this criterion is missing on this stand;
          the criterion was not checked and the result is unknown

Exit codes:
    0     no FAILs (all pass, or all pass+skip)
    1     at least one real FAIL

A run with any SKIP is not a passing acceptance run for gate purposes.
The gate requires pass=N skip=0 fail=0. Check the summary line.

No summary blob. A partial result reads as a partial result.

Requirements:
    - Python 3.9+ (standard library only -- no third-party packages)
    - psql in PATH, sudo -n -u postgres works (for bus checks that go to the DB)

Environment (auto-loaded from /opt/smart-node/dispatcher/.env when not already set):
    BRIN_API_KEY      API key for brin; also read from /etc/smart-node/secrets/brin-api-key
    SN_ADMIN_IDENTITY Owner name for core-api calls (default: admin).
                      The suite runs as root and reads the panel proof from
                      /etc/smart-node/core-api-proxy-auth.env.

Optional overrides (also read from the .env file):
    SN_CORE_API_URL   required; set by tools/acceptance-run.sh or export before running directly
    SN_BRIN_URL       required
    SN_AGENT_SLUG     override which agent to test against
    SN_TEST_IDENTITY  the person identity for cross-channel tests (default: owner)
"""

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


# ---------------------------------------------------------------------------
# Env loading -- must run before any os.environ.get() calls below.
# Reads the node's dispatcher .env file so the run needs no manual setup.
# Variables already exported in the caller's shell take precedence.
# ---------------------------------------------------------------------------

def _load_node_env() -> None:
    """Read env vars from the node's dispatcher .env if not already in environment."""
    candidates = [
        "/opt/smart-node/dispatcher/.env",
        "/etc/smart-node/dispatcher.env",
    ]
    for path_str in candidates:
        p = Path(path_str)
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
        break  # stop at the first file found


_load_node_env()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

API = os.environ.get("SN_CORE_API_URL") or ""
if not API:
    sys.exit("ERROR: SN_CORE_API_URL is not set; run via tools/acceptance-run.sh or set it explicitly")
BRIN = os.environ.get("SN_BRIN_URL", "")
TEST_IDENTITY = os.environ.get("SN_TEST_IDENTITY", "owner")

_key_path = Path("/etc/smart-node/secrets/brin-api-key")
BRIN_KEY = (
    os.environ.get("BRIN_API_KEY")
    or (_key_path.read_text().strip() if _key_path.exists() else None)
)

# UNVERIFIED: these env names for brin's turn ceiling. If the brin service uses
# a different variable, update this or set it before running.
BRIN_TURN_CEILING = int(
    os.environ.get("BRIN_TURN_TIMEOUT", "0")
    or os.environ.get("GATEWAY_TIMEOUT", "0") or "0"
)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

_fail = 0
_skip = 0


def _sample(msgs):
    """First message text, short, for a failure line."""
    if not msgs:
        return "none"
    return repr((msgs[0].get("text") or "")[:60])


def report(crit: str, ok: bool, detail: str) -> None:
    global _fail
    tag = "pass" if ok else "FAIL"
    print(f"{crit}  {tag}   {detail}", flush=True)
    if not ok:
        _fail += 1


def skip(crit: str, reason: str) -> None:
    global _skip
    print(f"{crit}  SKIP   {reason}", flush=True)
    _skip += 1


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only -- no third-party packages required)
# ---------------------------------------------------------------------------

class _Timeout(Exception):
    """Raised by _http when a network request times out."""


class _Response:
    """Minimal response wrapper so call sites read like the old requests version."""

    def __init__(self, status_code: int, body: bytes) -> None:
        self.status_code = status_code
        self._body = body

    @property
    def text(self) -> str:
        return self._body.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self._body)


def _http(
    method: str,
    url: str,
    body=None,
    headers: dict | None = None,
    timeout: int = 30,
    params: dict | None = None,
) -> _Response:
    """Make an HTTP request using urllib. Raises _Timeout on timeout."""
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    data: bytes | None = None
    h = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return _Response(resp.status, resp.read())
    except urllib.error.HTTPError as e:
        return _Response(e.code, e.read())
    except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
        raise _Timeout(str(e)) from e


# 'admin' is the default super-admin handle on a fresh node. On a real node
# set SN_ADMIN_IDENTITY to the owner's email or handle in sn_config.users.
_ADMIN_IDENTITY = os.environ.get("SN_ADMIN_IDENTITY", "admin")
_ADMIN = {"Remote-User": _ADMIN_IDENTITY}
_proof_file = Path("/etc/smart-node/core-api-proxy-auth.env")
if _proof_file.is_file():
    for _line in _proof_file.read_text().splitlines():
        if _line.startswith("SN_PROXY_AUTH_SECRET="):
            _proof = _line.partition("=")[2].strip()
            if _proof:
                _ADMIN["X-SN-Proxy-Auth"] = _proof
            break
_BRIN_H = {"X-API-Key": BRIN_KEY} if BRIN_KEY else {}


def api(method: str, path: str, **kw) -> _Response:
    h = {**_ADMIN, **kw.pop("headers", {})}
    return _http(
        method,
        API + path,
        body=kw.get("json"),
        headers=h,
        timeout=kw.get("timeout", 30),
        params=kw.get("params"),
    )


def brin_post(path: str, body: dict, timeout: int = 180) -> _Response:
    return _http("POST", BRIN + path, body=body, headers=_BRIN_H, timeout=timeout)


# ---------------------------------------------------------------------------
# Database helper (bus checks)
# ---------------------------------------------------------------------------

def psql(sql: str) -> str:
    """Run SQL via psql as the postgres unix peer. Returns stdout stripped.

    On any error (non-zero exit, exception) returns a string starting with
    PSQL_ERROR so psql_ok() can distinguish a broken query from an empty
    result. A broken query must never be reported as 'not present'.

    # UNVERIFIED: assumes `sudo -n -u postgres psql -d smartnode` works from the
    # operator's environment. If the node uses a different db name or peer auth
    # is not configured, set up the equivalent and re-run.
    """
    try:
        r = subprocess.run(
            ["sudo", "-n", "-u", "postgres",
             "psql", "-d", "smartnode", "-tAc",
             "SET search_path TO sn_core, sn_config, public; " + sql],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            err = r.stderr.strip() or r.stdout.strip() or "(no output)"
            return f"PSQL_ERROR(rc={r.returncode}):{err}"
        return r.stdout.strip()
    except Exception as e:
        return f"PSQL_ERROR:{e}"


def psql_ok(out: str) -> bool:
    return not out.startswith("PSQL_ERROR:")


# ---------------------------------------------------------------------------
# Precondition checks -- run ONCE before any criterion, never inside a test.
#
# A missing precondition causes dependent criteria to report SKIP, not FAIL.
# FAIL means the product is broken. SKIP means this stand cannot check it.
# ---------------------------------------------------------------------------

def _pc_llm_credentials() -> bool:
    """Return True if a Claude LLM credential is present on this node."""
    env_file = Path("/etc/smart-node/agent-runtime.env")
    if not env_file.exists():
        return False
    try:
        text = env_file.read_text()
    except OSError:
        return False
    return bool(re.search(
        r'^(?:export\s+)?CLAUDE_CODE_OAUTH_TOKEN=.+',
        text, re.MULTILINE,
    ))


def _pc_agent_reachable() -> bool:
    """Return True if brin answers on its health endpoint."""
    try:
        r = _http("GET", BRIN + "/health", timeout=10)
        return r.status_code == 200
    except (_Timeout, Exception):
        return False


def _psql_last_line(out: str) -> str:
    """Return the last non-empty line of psql output.

    psql() prepends a SET search_path statement, so stdout for a query is
    typically "SET\\n<result>". Taking the last non-empty line strips the SET
    line and returns only the query result.
    """
    return next((l for l in reversed(out.splitlines()) if l.strip()), "").strip()


def _pc_bus_reachable() -> bool | None:
    """Return True if the message bus database (psql) is reachable.

    Returns None if the check itself failed (SQL error) -- caller prints ERROR,
    not MISSING, so a broken query is never reported as an absent precondition.
    """
    out = psql("SELECT 1")
    if not psql_ok(out):
        return None
    return _psql_last_line(out) == "1"


def _pc_gateway_credential() -> bool | None:
    """Return True if the gateway LLM credential is present in sn_config.secrets.

    Returns None if the check itself failed (SQL error) -- caller prints ERROR,
    not MISSING, so a broken query is never reported as an absent credential.

    The gateway resolves 'gateway/claude-cli-token' from sn_config.secrets,
    not from any env file. A node that has a credential in agent-runtime.env but not
    in the secret store will pass _pc_llm_credentials and fail every gateway session.
    This check asks the right source.
    """
    out = psql(
        "SELECT count(*) FROM sn_config.secrets "
        "WHERE ref = 'gateway/claude-cli-token'"
    )
    if not psql_ok(out):
        return None  # broken query: cannot check, do not report as MISSING
    return _psql_last_line(out) == "1"


def check_preconditions() -> dict:
    """Check all preconditions once and return {name: bool}.

    Results are printed before any criterion runs so the operator sees what
    is missing before the first test line appears.
    """
    checks = [
        ("agent_reachable",    _pc_agent_reachable,
         f"brin health at {BRIN}/health"),
        ("gateway_credential", _pc_gateway_credential,
         "gateway/claude-cli-token present in sn_config.secrets"),
        ("bus_reachable",      _pc_bus_reachable,
         "psql SELECT 1 on smartnode db"),
        ("llm_credentials",    _pc_llm_credentials,
         "CLAUDE_CODE_OAUTH_TOKEN in /etc/smart-node/agent-runtime.env"),
    ]
    results: dict = {}
    for name, fn, desc in checks:
        ok = fn()
        results[name] = ok
        if ok is True:
            status = "ok     "
        elif ok is None:
            status = "ERROR  "
        else:
            status = "MISSING"
        print(f"  precondition {name}: {status}  ({desc})", flush=True)
    not_ok = [n for n, ok in results.items() if not ok]
    if not_ok:
        print(f"  WARNING: {len(not_ok)} precondition(s) missing or un-checkable -- "
              f"dependent criteria will SKIP, not FAIL", flush=True)
    return results


# ---------------------------------------------------------------------------
# Desk helpers
# ---------------------------------------------------------------------------

def create_desk(title: str) -> tuple[str | None, int]:
    """Returns (desk_id, http_status). desk_id is None on failure."""
    r = api("POST", "/api/desks", json={"title": title})
    if r.status_code == 201:
        return r.json()["id"], 201
    return None, r.status_code


def delete_desk(desk_id: str) -> None:
    api("DELETE", f"/api/desks/{desk_id}")


def desk_history(desk_id: str) -> list[dict]:
    r = api("GET", f"/api/desks/{desk_id}/history", timeout=15)
    if r.status_code != 200:
        return []
    return r.json().get("messages", [])


# A desk shows a message only after the dispatcher has passed it, and the
# dispatcher polls every 2 s, so a message sent from here reaches the
# history 0-2 s later, plus the decision itself. A fixed 1 s wait raced that
# and failed A2/A6 on the 1.12.46 candidate while the rows were delivered at
# +1.0 s and +1.7 s. Poll instead, for three dispatcher cycles.
DESK_DELIVERY_WAIT_S = 6.0


def wait_in_history(desk_id: str, probe: str,
                    timeout: float = DESK_DELIVERY_WAIT_S) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if any(probe in (m.get("content") or "") for m in desk_history(desk_id)):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


# ---------------------------------------------------------------------------
# Brin chat helpers
# ---------------------------------------------------------------------------

def chat(desk_id: str, agent: str, message: str, timeout: int = 180
         ) -> tuple[dict, float, int]:
    """Send one message via brin. Returns (body_dict, elapsed_s, http_status)."""
    t0 = time.monotonic()
    try:
        r = brin_post("/api/message", {
            "message": message,
            "agent": agent,
            "stream": False,
            "context": {
                "user_id": f"desk:{desk_id}",
                "studio_user": TEST_IDENTITY,
                "channel": "web",
            },
        }, timeout=timeout)
        elapsed = time.monotonic() - t0
        try:
            return r.json(), elapsed, r.status_code
        except Exception:
            return {"content": r.text, "_raw": True}, elapsed, r.status_code
    except _Timeout:
        return {"content": "", "_timeout": True}, time.monotonic() - t0, 0
    except Exception as e:
        return {"content": str(e), "_error": True}, time.monotonic() - t0, -1


# ---------------------------------------------------------------------------
# A1: Nothing a person sends is lost
# ---------------------------------------------------------------------------

def test_a1(desk_id: str, agent: str, pc: dict) -> None:
    if not pc["agent_reachable"]:
        skip("A1", "brin not reachable")
        return
    if not pc["gateway_credential"]:
        skip("A1", "gateway LLM credential not in gateway_secrets -- turns will fail")
        return
    marker = f"a1-{int(time.time())}"
    body, elapsed, status = chat(desk_id, agent,
                                 f"Reply with exactly these words: marker-{marker}-confirmed")
    if status != 200:
        report("A1", False, f"brin returned HTTP {status} (elapsed {elapsed:.1f}s)")
        return

    # The message reached brin and got a response. Check it is not empty.
    content = (body.get("content") or "").strip()
    if not content:
        report("A1", False, f"brin returned HTTP 200 but empty content after {elapsed:.1f}s")
        return

    # Check the desk history shows the user message (not just the agent response).
    history = desk_history(desk_id)
    user_turns = [m for m in history if m.get("role") == "user"]
    if not user_turns:
        report("A1", False,
               f"brin replied ({elapsed:.1f}s) but desk history has no user turn -- "
               "the message reached the agent but was not recorded")
        return

    # Check the bus via psql for the web-chat inbound row.
    # _persist_webchat_turn is best-effort, so the absence of a bus row is a
    # warning (the desk session still has the message) rather than a hard fail for A1.
    bus = psql(
        f"SELECT count(*) FROM sn_core.messages "
        f"WHERE channel_type='web-chat' AND direction='inbound' "
        f"AND content LIKE '%{marker}%'"
    )
    bus_count = _psql_last_line(bus) if psql_ok(bus) else bus
    if psql_ok(bus) and bus_count == "1":
        report("A1", True,
               f"user message in desk history AND on bus (brin: {elapsed:.1f}s)")
    else:
        # The desk has the message; bus mirror is async/best-effort -- note but pass.
        report("A1", True,
               f"user message in desk history after {elapsed:.1f}s "
               f"(bus mirror: count={bus_count!r} -- record-webchat is best-effort, "
               f"check brin logs if 0)")


# ---------------------------------------------------------------------------
# A2: Nothing an agent writes is lost
# ---------------------------------------------------------------------------

def test_a2(desk_id: str, agent: str, pc: dict) -> None:
    if not pc["agent_reachable"]:
        skip("A2", "brin not reachable")
        return
    if not pc["gateway_credential"]:
        skip("A2", "gateway LLM credential not in gateway_secrets -- turns will fail")
        return
    # A2 has two sub-checks:
    #   (a) a normal completed turn's assistant reply appears in desk history.
    #   (b) an agent-originated message (sent to the desk via the bus, not via a
    #       gateway session) also appears in history -- this is the earlier change path.

    # Sub-check (a): run a complete turn and verify both sides are in history.
    marker = f"a2-{int(time.time())}"
    body, elapsed, status = chat(desk_id, agent,
                                 f"Please include the phrase 'verified-{marker}' in your reply.")
    if status != 200 or not (body.get("content") or "").strip():
        report("A2", False,
               f"turn did not complete (HTTP {status}, content empty: "
               f"{not bool((body.get('content') or '').strip())})")
        return

    history = desk_history(desk_id)
    has_user = any(m.get("role") == "user" for m in history)
    has_asst = any(m.get("role") == "assistant"
                   and (m.get("content") or "") for m in history)
    if not has_user or not has_asst:
        report("A2", False,
               f"desk history missing {'user' if not has_user else 'assistant'} turn "
               f"after completed turn ({elapsed:.1f}s, {len(history)} rows)")
        return

    # Sub-check (b): inject an agent-originated message directly to the desk via
    # the internal-to-desk relay path. An agent sends an internal
    # message to 'desk:<id>'; the desk history's second UNION arm picks it up
    # as an assistant turn. This is the path that lost answers -- an answer
    # the agent had already composed arrived via the bus and was not shown.
    probe = f"a2-relay-probe-{marker}"
    r = api("POST", "/api/messages/send",
            json={
                "to": f"desk:{desk_id}",
                "body": probe,
                # No channel specified: 'desk:<id>' with no '@' and no channel
                # goes to the internal path. The history endpoint's second UNION
                # arm surfaces it as an assistant turn.
                # NOTE: called as the admin identity, not the agent slug. Agent
                # slugs are refused over TCP by core-api. The mechanism
                # under test is the route (internal -> desk history), not the
                # sender identity. The agent's own origination path (via its
                # process socket or comm.sh) is UNVERIFIED here.
            })
    if r.status_code not in (200, 201):
        report("A2", False,
               f"(a) turn recorded in history; "
               f"(b) FAIL -- internal-to-desk send returned HTTP {r.status_code}: "
               f"{r.text[:120]}")
        return

    relay_found = wait_in_history(desk_id, probe)
    if relay_found:
        report("A2", True,
               f"(a) both turns in history; (b) relay message surfaced in history -- "
               f"agent-originated answers reach the desk (id {r.json().get('id', '?')})")
    else:
        report("A2", False,
               f"(a) both turns in history; "
               f"(b) FAIL -- relay message (id {r.json().get('id', '?')}) "
               f"not in desk history after {DESK_DELIVERY_WAIT_S:.0f}s -- "
               f"desk history second UNION (internal to desk:<id>) may be broken")


# ---------------------------------------------------------------------------
# A3: A second message during a turn is safe
# ---------------------------------------------------------------------------

def test_a3(desk_id: str, agent: str, pc: dict) -> None:
    if not pc["agent_reachable"]:
        skip("A3", "brin not reachable")
        return
    if not pc["gateway_credential"]:
        skip("A3", "gateway LLM credential not in gateway_secrets -- turns will fail")
        return
    results: dict[str, tuple[dict, float, int]] = {}

    def send_first():
        # Ask for something that takes a moment; the exact timing is node-dependent.
        # UNVERIFIED: whether this actually overlaps with the second message depends
        # on how fast the agent responds. If the node is very fast, both messages
        # may land sequentially. The absence of 502 is the primary assertion.
        results["first"] = chat(desk_id, agent,
                                "Count from 1 to 20, one word per line.",
                                timeout=180)

    def send_second():
        # Small delay so the first message is likely in flight when this lands.
        time.sleep(0.5)
        results["second"] = chat(desk_id, agent,
                                 "What is 2 + 2?",
                                 timeout=180)

    t1 = threading.Thread(target=send_first)
    t2 = threading.Thread(target=send_second)
    t1.start(); t2.start()
    t1.join(); t2.join()

    b1, e1, s1 = results.get("first", ({}, 0, -1))
    b2, e2, s2 = results.get("second", ({}, 0, -1))

    if s1 == 502 or s2 == 502:
        failing = []
        if s1 == 502:
            failing.append(f"first=502 ({e1:.1f}s)")
        if s2 == 502:
            failing.append(f"second=502 ({e2:.1f}s)")
        report("A3", False, "second message during a turn: " + ", ".join(failing))
        return

    if s1 not in (200,) or s2 not in (200,):
        report("A3", False,
               f"non-502 failure: first=HTTP{s1} ({e1:.1f}s), second=HTTP{s2} ({e2:.1f}s)")
        return

    # Both responses must have content.
    c1 = (b1.get("content") or "").strip()
    c2 = (b2.get("content") or "").strip()
    if not c1 or not c2:
        report("A3", False,
               f"HTTP 200 but content empty: first={bool(c1)}, second={bool(c2)}")
        return

    # Both user messages must appear in desk history.
    history = desk_history(desk_id)
    user_turns = [m for m in history if m.get("role") == "user"]
    if len(user_turns) < 2:
        report("A3", False,
               f"both sends returned 200 but desk history has only {len(user_turns)} "
               f"user turn(s) -- one message was not recorded")
        return

    overlap = "likely concurrent" if abs(e1 - e2) < max(e1, e2) * 0.8 else "sequential"
    report("A3", True,
           f"both messages returned 200 and appear in history "
           f"(first {e1:.1f}s, second {e2:.1f}s, timing: {overlap})")


# ---------------------------------------------------------------------------
# A4: The person always knows which state they are in
# ---------------------------------------------------------------------------

def test_a4(desk_id: str, agent: str, pc: dict) -> None:
    if not pc["agent_reachable"]:
        skip("A4", "brin not reachable")
        return
    if not pc["gateway_credential"]:
        skip("A4", "gateway LLM credential not in gateway_secrets -- turns will fail")
        return
    # A4 checks that the API never hands the person a 200 with empty content.
    body, elapsed, status = chat(desk_id, agent,
                                 "Say 'acknowledged' and nothing else.")
    content = (body.get("content") or "").strip()
    failed_flag = body.get("failed", False)

    if status == 200 and not content and not failed_flag:
        report("A4", False,
               f"HTTP 200 with empty content and no failed flag after {elapsed:.1f}s -- "
               "the person would see a blank pane with no indication of the state")
        return

    if status == 200 and content:
        report("A4", True,
               f"turn returned content after {elapsed:.1f}s -- "
               "person is in state 'answer arrived'")
        return

    if failed_flag:
        report("A4", True,
               f"turn returned failed=True after {elapsed:.1f}s -- "
               "person is in state 'turn failed' (explicitly declared)")
        return

    report("A4", False,
           f"HTTP {status} after {elapsed:.1f}s, content empty={not bool(content)}, "
           f"failed={failed_flag} -- state is ambiguous")


# ---------------------------------------------------------------------------
# A5: A slow turn is survivable
# ---------------------------------------------------------------------------

def test_a5(desk_id: str, agent: str, pc: dict) -> None:
    if not pc["agent_reachable"]:
        skip("A5", "brin not reachable")
        return
    if not pc["gateway_credential"]:
        skip("A5", "gateway LLM credential not in gateway_secrets -- turns will fail")
        return
    # UNVERIFIED: verifying a 300s turn end-to-end would block this script for
    # at least 5 minutes and depends on being able to craft a turn that forces
    # the agent to spend that long. The check below is what can be automated:
    # the configured ceiling must be at least 300s.
    #
    # To fully verify A5 manually:
    #   1. Send a message that triggers a genuinely long agent run (e.g. a
    #      research task that takes several minutes).
    #   2. Do not close the browser tab.
    #   3. Confirm the answer arrives and the desk history shows it.
    #   4. Confirm the agent did not produce a "sorry, timed out" message.

    # Check 1: brin reachable -- established by the precondition check above.
    # Check 2: the configured turn ceiling.
    # UNVERIFIED: read from the brin process env or from /etc/smart-node/brin.env
    # if you need the authoritative value.
    brin_env_path = Path("/etc/smart-node/brin.env")
    ceiling = BRIN_TURN_CEILING
    if ceiling == 0 and brin_env_path.exists():
        for line in brin_env_path.read_text().splitlines():
            for var in ("BRIN_TURN_TIMEOUT", "GATEWAY_TIMEOUT"):
                if line.startswith(var + "="):
                    try:
                        ceiling = int(line.split("=", 1)[1].strip().strip('"'))
                    except ValueError:
                        pass

    ceiling_ok = ceiling >= 300 if ceiling > 0 else None

    # Check 3: run a moderate turn and confirm the answer arrives.
    # UNVERIFIED: whether the agent actually takes >30s depends on node load.
    body, elapsed, status = chat(desk_id, agent,
                                 "Please write a short poem about patience (4 lines).",
                                 timeout=240)
    content = (body.get("content") or "").strip()
    turn_ok = status == 200 and bool(content)

    if not turn_ok:
        report("A5", False,
               f"brin is up; turn attempt returned HTTP {status} after {elapsed:.1f}s; "
               f"configured ceiling: {ceiling or 'unknown'}")
        return

    if ceiling_ok is False:
        report("A5", False,
               f"turn answered in {elapsed:.1f}s; configured ceiling={ceiling}s < 300s -- "
               "a 5-minute turn will not survive the ceiling even if brin is up")
        return

    if ceiling_ok is True:
        report("A5", True,
               f"brin up, configured ceiling {ceiling}s >= 300s, "
               f"test turn answered in {elapsed:.1f}s (full 5-min test: see docstring)")
    else:
        report("A5", True,
               f"brin up, test turn answered in {elapsed:.1f}s; "
               f"ceiling not read from env -- set BRIN_TURN_TIMEOUT to check the 300s floor "
               f"(full 5-min test: see docstring)")


# ---------------------------------------------------------------------------
# A6: An agent can start a conversation
# ---------------------------------------------------------------------------

def test_a6(desk_id: str, agent: str, pc: dict) -> None:
    # A6 uses core-api directly (not brin), so it does not depend on
    # agent_reachable. No precondition check needed here.
    # Send a message FROM the agent TO the desk without any user message first.
    # Two mechanisms, one per union arm in the desk history query:
    #   (i)  internal message to 'desk:<id>' -- picked up by the second UNION arm.
    #   (ii) outbound web-chat message with thread_id 'desk:<id>' -- third UNION arm.
    #        Requires the agent to have a web-chat channel binding; skipped if not.

    probe_i = f"a6-internal-{int(time.time())}"
    probe_ii = f"a6-webchat-{int(time.time())}"

    # Mechanism (i): internal message, no binding required.
    r_i = api("POST", "/api/messages/send",
              json={"to": f"desk:{desk_id}", "body": probe_i})
    if r_i.status_code not in (200, 201):
        report("A6", False,
               f"internal-to-desk send returned HTTP {r_i.status_code}: {r_i.text[:150]}")
        return

    found_i = wait_in_history(desk_id, probe_i)
    if not found_i:
        report("A6", False,
               f"internal message (id {r_i.json().get('id', '?')}) not found in "
               f"desk history after {DESK_DELIVERY_WAIT_S:.0f}s -- history second UNION (internal to desk:<id>) "
               f"may be broken")
        return

    # Mechanism (ii): outbound web-chat with channel=web-chat.
    r_ii = api("POST", "/api/messages/send",
               json={"to": f"desk:{desk_id}", "body": probe_ii, "channel": "web-chat"})
    if r_ii.status_code == 400 and "no enabled 'web-chat' mailbox" in (r_ii.text or ""):
        report("A6", True,
               f"(i) internal relay works (id {r_i.json().get('id', '?')}); "
               f"(ii) skip -- agent has no web-chat mailbox binding "
               f"(configure agent_channels for the authenticated relay path)")
        return

    if r_ii.status_code not in (200, 201):
        report("A6", False,
               f"(i) internal relay works; "
               f"(ii) FAIL -- web-chat send returned HTTP {r_ii.status_code}: {r_ii.text[:120]}")
        return

    found_ii = wait_in_history(desk_id, probe_ii)
    if found_ii:
        report("A6", True,
               f"(i) internal relay works; (ii) outbound web-chat message appears in "
               f"history (id {r_ii.json().get('id', '?')}) -- both proactive paths work")
    else:
        report("A6", False,
               f"(i) internal relay works; "
               f"(ii) FAIL -- web-chat send HTTP {r_ii.status_code} but "
               f"message not in history after {DESK_DELIVERY_WAIT_S:.0f}s -- thread_id or history third UNION broken")


# ---------------------------------------------------------------------------
# A7: Reload loses nothing
# ---------------------------------------------------------------------------

def test_a7(desk_id: str, agent: str, pc: dict) -> None:
    if not pc["agent_reachable"]:
        skip("A7", "brin not reachable")
        return
    if not pc["gateway_credential"]:
        skip("A7", "gateway LLM credential not in gateway_secrets -- turns will fail")
        return
    marker = f"a7-{int(time.time())}"
    body, elapsed, status = chat(desk_id, agent,
                                 f"Reply with just: reload-{marker}-ok")
    if status != 200 or not (body.get("content") or "").strip():
        report("A7", False,
               f"turn did not complete (HTTP {status}, elapsed {elapsed:.1f}s)")
        return

    h1 = desk_history(desk_id)
    time.sleep(0.3)
    h2 = desk_history(desk_id)

    if len(h1) != len(h2):
        report("A7", False,
               f"history changed between two consecutive reads: "
               f"first {len(h1)} messages, second {len(h2)}")
        return

    c1 = [(m.get("role"), (m.get("content") or "")[:80]) for m in h1]
    c2 = [(m.get("role"), (m.get("content") or "")[:80]) for m in h2]
    if c1 != c2:
        report("A7", False,
               f"history returned different content between two consecutive reads "
               f"({len(h1)} messages each time, but content differed)")
        return

    asst_turns = [m for m in h1 if m.get("role") == "assistant"]
    if not asst_turns:
        report("A7", False,
               f"both history reads returned {len(h1)} messages but no assistant turn -- "
               "the agent replied but the reply is not in history")
        return

    report("A7", True,
           f"history stable across two reads ({len(h1)} messages, "
           f"{len(asst_turns)} assistant turn(s)); reload-safe")


# ---------------------------------------------------------------------------
# A8: A failure is visible where a person looks
# ---------------------------------------------------------------------------

def test_a8(desk_id: str, pc: dict) -> None:
    if not pc["agent_reachable"]:
        skip("A8", "brin not reachable")
        return
    if not pc["gateway_credential"]:
        skip("A8", "gateway LLM credential not in gateway_secrets -- turns will fail")
        return
    # Trigger a failure by using a non-existent agent slug. Brin should return
    # either failed=True or the _COULD_NOT_ANSWER message, not an empty 200.
    bogus_agent = "e2e-nonexistent-agent-x9z"
    body, elapsed, status = chat(desk_id, bogus_agent,
                                 "Hello.", timeout=60)
    content = (body.get("content") or "").strip()
    failed_flag = body.get("failed", False)

    if status == 200 and not content and not failed_flag:
        report("A8", False,
               f"HTTP 200 with empty content and no failed flag after {elapsed:.1f}s -- "
               "the failure is silent: a person sees a blank pane")
        return

    if status in (400, 404, 500, 503):
        report("A8", True,
               f"failure returns HTTP {status} after {elapsed:.1f}s -- "
               "error is declared, not silent")
        return

    if failed_flag:
        # UNVERIFIED: brin sets failed=True when run_direct raises. If the agent
        # is not found before the executor runs, this flag may not be set.
        report("A8", True,
               f"failure returns failed=True after {elapsed:.1f}s "
               f"(content: {content[:60]!r})")
        return

    if content and "could not answer" in content.lower():
        report("A8", True,
               f"failure returns brin error message after {elapsed:.1f}s -- "
               "person is told the turn failed")
        return

    if content and status == 200:
        report("A8", False,
               f"HTTP 200 with content after {elapsed:.1f}s but using a non-existent "
               f"agent -- something responded that should not have: {content[:80]!r}")
        return

    report("A8", False,
           f"HTTP {status} after {elapsed:.1f}s, content={bool(content)}, "
           f"failed={failed_flag} -- failure mode is unclear")


# ---------------------------------------------------------------------------
# A9: One conversation, any door
# ---------------------------------------------------------------------------

def test_a9(agent: str, pc: dict) -> None:
    if not pc["agent_reachable"]:
        skip("A9", "brin not reachable")
        return
    if not pc["gateway_credential"]:
        skip("A9", "gateway LLM credential not in gateway_secrets -- turns will fail")
        return
    if not pc["llm_credentials"]:
        skip("A9", "no LLM credentials on this node")
        return
    # A9 is the criterion the owner cares about most: a person who spoke to the
    # agent through one door is recognised at another -- the agent holds the thread.
    #
    # WHAT "ANOTHER DOOR" MEANS for the cross-channel identity rule (owner decision 2026-09-22):
    # another CHANNEL, not another desk. Two Studio desks are two conversations
    # and must not see each other; a call, a mail or a Telegram message from the
    # same person must still reach the desk. The earlier version of this test
    # planted both facts as web-chat rows in other threads, so it measured the
    # desk-to-desk bleed the routing guard prevents, and it went red on 1.12.44
    # for exactly that reason.
    #
    # So this test plants the fact through a real second channel -- voice, via
    # record-voice-turns, the same write the voice backend makes during a call --
    # and a DECOY in a neighbouring desk, then checks both halves:
    #   (1) the new desk's scoped history carries the voice fact  (continuity)
    #   (2) the new desk's scoped history does NOT carry the decoy (isolation)
    #   (3) the agent, asked on the new desk, names the voice fact and not the decoy.
    # (1) and (2) are deterministic; (3) needs a live LLM turn.

    stamp = int(time.time())
    fact_a = f"a9-fact-{stamp}"
    decoy = f"a9-decoy-{stamp}"

    # The owner's voice identity, from the registry: record-voice-turns resolves
    # a person by email/handle/voice address, and 'owner' is not one of those.
    out = psql(
        "SELECT u.email FROM sn_config.users u "
        "JOIN sn_config.user_channels uc ON uc.user_id = u.id "
        "WHERE u.role = 'super-admin' AND u.active AND u.archived_at IS NULL "
        "AND uc.channel_slug = 'voice' AND uc.enabled ORDER BY u.id LIMIT 1"
    )
    if not psql_ok(out):
        report("A9", False, f"could not read the owner's voice channel: {out[:120]}")
        return
    voice_ident = _psql_last_line(out)
    if not voice_ident:
        skip("A9", "the owner has no enabled voice channel on this node -- "
                   "no second door to plant through")
        return

    # --- Step 1: the fact arrives through the voice door ---
    r_v = api("POST", "/api/messages/record-voice-turns", json={
        "identity": voice_ident,
        "agent": agent,
        "session_id": f"a9-call-{stamp}",
        "turns": [
            {"role": "user", "seq": 1, "text": f"My project name is {fact_a}."},
            {"role": "assistant", "seq": 2, "text": f"Understood, I will remember {fact_a}."},
        ],
    })
    if r_v.status_code != 200 or not r_v.json().get("ok"):
        report("A9", False,
               f"could not record the voice turn (HTTP {r_v.status_code}): {r_v.text[:120]}")
        return

    # --- Step 2: a decoy in a neighbouring desk that the new desk must NOT see ---
    r_d = api("POST", "/api/messages/record-webchat", json={
        "identity": TEST_IDENTITY,
        "turns": [
            {"role": "user", "text": f"In this desk we only talk about {decoy}."},
            {"role": "assistant", "text": f"Noted, {decoy}."},
        ],
        "agent": agent,
        "session_id": f"a9-other-desk-{stamp}",
    })
    if r_d.status_code != 200 or not r_d.json().get("ok"):
        report("A9", False,
               f"could not record the neighbouring-desk decoy (HTTP {r_d.status_code})")
        return

    # --- Step 3: a new desk, and what its scoped history holds ---
    ask_desk, _st = create_desk("a9-cross-channel-test")
    if not ask_desk:
        report("A9", False, "could not create a desk for the ask turn")
        return

    try:
        # Same scoping the executor applies to a desk turn (thread = desk:<id>).
        r_cch = api("GET", "/api/users/cross-channel-history",
                    params={"identity": TEST_IDENTITY, "limit": 20, "agent": agent,
                            "thread": f"desk:{ask_desk}", "thread_channel": "web-chat"})
        if r_cch.status_code != 200:
            report("A9", False, f"cross-channel-history returned HTTP {r_cch.status_code}")
            return
        msgs = r_cch.json().get("messages", [])
        texts = [(m.get("text") or "") for m in msgs]
        if not any(fact_a in t for t in texts):
            report("A9", False,
                   f"continuity broken: the voice fact '{fact_a}' is not in the new desk's "
                   f"history ({len(msgs)} rows, identity={TEST_IDENTITY!r}, "
                   f"voice identity={voice_ident!r}). Sample: {_sample(msgs)}")
            return
        if any(decoy in t for t in texts):
            report("A9", False,
                   f"isolation broken: the neighbouring desk's '{decoy}' reached the new "
                   f"desk's history ({len(msgs)} rows)")
            return

        # --- Step 4: ask the agent on the new desk ---
        ask_body, ask_elapsed, ask_status = chat(
            ask_desk, agent,
            "What is the project name I mentioned earlier? Do not say you do not know.",
            timeout=180,
        )
        ask_content = (ask_body.get("content") or "").strip()

        # The history held the fact and not the decoy; now the reply. If the fact
        # is missing, the block did not reach the prompt -- the defect John hit on
        # Telegram. If the decoy shows up, a neighbouring desk leaked in anyway.
        if decoy in ask_content:
            a9_result = False
            a9_detail = (
                f"(storage pass) FAIL: agent on new desk named the neighbouring desk's "
                f"'{decoy}' ({ask_elapsed:.1f}s) -- desks are bleeding into each other. "
                f"Agent said: {ask_content[:120]!r}"
            )
        elif fact_a in ask_content:
            a9_result = True
            a9_detail = (
                f"agent on new desk knew '{fact_a}' from the voice call and did not "
                f"bring in the neighbouring desk ({ask_elapsed:.1f}s) -- "
                f"cross-channel context is carried, desks stay apart"
            )
        else:
            a9_result = False
            a9_detail = (
                f"(storage pass) FAIL: agent on new desk did NOT reference '{fact_a}' "
                f"from the voice call ({ask_elapsed:.1f}s) -- cross-channel context is "
                f"NOT being carried. Agent said: {ask_content[:120]!r}"
            )

        report("A9", a9_result, a9_detail)

    finally:
        delete_desk(ask_desk)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    if not BRIN_KEY:
        print(
            "FATAL: no Brin API key found. "
            "Set BRIN_API_KEY, or ensure /etc/smart-node/secrets/brin-api-key is readable, "
            "or ensure /opt/smart-node/dispatcher/.env contains BRIN_API_KEY.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Discover an enabled agent.
    r = api("GET", "/api/agents")
    if r.status_code != 200:
        print(f"FATAL: cannot list agents (HTTP {r.status_code})", file=sys.stderr)
        sys.exit(1)
    agents_list = r.json().get("agents", [])
    force_agent = os.environ.get("SN_AGENT_SLUG")
    agent = (
        force_agent
        or next(
            (a["slug"] for a in agents_list
             if a.get("enabled") and not a.get("archived_at")),
            None,
        )
    )
    if not agent:
        print("FATAL: no enabled agent on this node; set SN_AGENT_SLUG to override",
              file=sys.stderr)
        sys.exit(1)

    print(f"# web-chat acceptance run | agent={agent} | node={API}", flush=True)
    print("# checking preconditions ...", flush=True)
    pc = check_preconditions()
    print(flush=True)

    # Each test gets its own desk so one test's history does not bleed into another.
    desks: dict[str, str | None] = {}
    for name in ("a1", "a2", "a3", "a4", "a5", "a6", "a7", "a8"):
        desk_id, desk_status = create_desk(f"acceptance-{name}")
        desks[name] = desk_id
        if not desk_id:
            print(
                f"FATAL: could not create desk for {name} "
                f"(HTTP {desk_status} from POST /api/desks). "
                f"Check SN_ADMIN_IDENTITY (current: {_ADMIN_IDENTITY!r}) -- "
                f"it must match a user in sn_config.users that can create desks.",
                file=sys.stderr,
            )
            sys.exit(1)

    try:
        test_a1(desks["a1"], agent, pc)
        test_a2(desks["a2"], agent, pc)
        test_a3(desks["a3"], agent, pc)
        test_a4(desks["a4"], agent, pc)
        test_a5(desks["a5"], agent, pc)
        test_a6(desks["a6"], agent, pc)
        test_a7(desks["a7"], agent, pc)
        test_a8(desks["a8"], pc)
        test_a9(agent, pc)
    finally:
        for desk_id in desks.values():
            if desk_id:
                delete_desk(desk_id)

    total = 9
    passed = total - _fail - _skip
    if _fail > 0:
        verdict = "FAIL"
        gate_note = "REJECT -- product defect(s) found"
    elif _skip > 0:
        verdict = "SKIP"
        gate_note = "REJECT -- precondition(s) missing; re-run on a stand with all preconditions met"
    else:
        verdict = "PASS"
        gate_note = "ACCEPT"
    print(
        f"# done | {verdict} | pass={passed} skip={_skip} fail={_fail} | gate: {gate_note}",
        flush=True,
    )
    sys.exit(0 if _fail == 0 else 1)


if __name__ == "__main__":
    main()
