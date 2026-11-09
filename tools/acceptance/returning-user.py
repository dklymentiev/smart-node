#!/usr/bin/env python3
"""Returning-user acceptance scenario.

Verifies that a returning user's data -- planner task, doci document, mesh
memory entry, agent session, and an extra agent grant -- survives an update.

TWO MODES:

  --seed          Create one record in each service. Write a JSON state blob to
                  stdout. The orchestrator saves this to the controller before
                  the update runs and passes it back as --state after the update.

  --check --state JSON
                  Read the recorded IDs from the state blob, verify each record
                  still exists and its content matches. Print one line:
                    RETURNING_USER: PASS
                  or
                    RETURNING_USER: FAIL <what failed>

A fresh stand (no --state file passed) produces RETURNING_USER: FAIL with the
reason "no pre-update state". This is correct: a fresh install is not a valid
publish target for this gate.

REQUIREMENTS
  - Python 3.9+ (stdlib only)
  - psql in PATH, sudo -n -u postgres psql -d smartnode works
  - docker in PATH (to resolve container IPs)
  - /opt/smart-node/dispatcher/.env readable (for MESH_API_URL)
  - /opt/smart-node/documents/documents.env readable (for SMART_NODE_DOCI_API_KEY)
  - SN_CORE_API_URL reachable (core-api / planner)

SCHEMA NOTES (verified against live migrations 2026-09-01):
  sn_core.ai_sessions  -- one row per LLM session
  status = 'completed' AND error IS NULL  -- successful turn
"""

import http.client
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Env loading -- reads dispatcher .env first (MESH_API_URL), then documents
# .env (SMART_NODE_DOCI_API_KEY). Variables already in the environment win.
# ---------------------------------------------------------------------------

def _load_env(path: str) -> None:
    p = Path(path)
    if not p.exists():
        return
    try:
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
    except OSError:
        pass


_load_env("/opt/smart-node/dispatcher/.env")
_load_env("/opt/smart-node/documents/documents.env")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CORE_API = os.environ.get("SN_CORE_API_URL") or ""
if not CORE_API:
    sys.exit("ERROR: SN_CORE_API_URL is not set; run via tools/acceptance-run.sh or set it explicitly")
MESH_API = os.environ.get("MESH_API_URL", "").rstrip("/")
DOCI_KEY = (
    os.environ.get("SMART_NODE_DOCI_API_KEY") or
    os.environ.get("DOCI_API_KEY") or
    ""
)
# The admin identity for planner calls; set SN_ADMIN_IDENTITY if the default
# 'admin' is not a valid planner user on this node.
PLANNER_USER = os.environ.get("SN_ADMIN_IDENTITY", "admin")
# Planner calls go over core-api's unix socket, not TCP. now core-api
# refuses a planner mutation whose only identity is an asserted Remote-User
# header; on the socket the kernel names the caller (SO_PEERCRED), and this
# script runs as root, which core-api maps to the operator actor (admin).
CORE_API_UDS = os.environ.get("CORE_API_UDS", "/run/smart-node/api/core-api.sock")

# Seed agent: the agent to wake for the session check.
# Set SN_SMOKE_AGENT to override. Otherwise it is the node's setup agent, found by its capability (configurator): a node
# installed from 1.13 has john there, an older one foreman, and a fixed slug waited five minutes for an agent that was
# not there. SEED_AGENT_FALLBACK is only what is used when the database names no such agent.
SEED_AGENT_FALLBACK = "foreman"
SEED_AGENT = os.environ.get("SN_SMOKE_AGENT") or None

SEED_MARKER = "returning-user-acceptance-seed"


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only)
# ---------------------------------------------------------------------------

class _Timeout(Exception):
    pass


class _Response:
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
) -> _Response:
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
    except (urllib.error.URLError, OSError) as e:
        raise _Timeout(str(e)) from e


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: int) -> None:
        super().__init__("localhost", timeout=timeout)
        self._uds_path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._uds_path)


def _uds_http(method: str, path: str, body=None, timeout: int = 30) -> _Response:
    """Call core-api over its unix socket; the kernel supplies the identity."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    h = {"Content-Type": "application/json"} if data is not None else {}
    conn = _UnixHTTPConnection(CORE_API_UDS, timeout)
    try:
        conn.request(method, path, body=data, headers=h)
        resp = conn.getresponse()
        return _Response(resp.status, resp.read())
    except OSError as e:
        raise _Timeout(f"{CORE_API_UDS}: {e}") from e
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Database helper
# ---------------------------------------------------------------------------

def psql(sql: str) -> str:
    """Run SQL via psql as the postgres unix peer. Returns stdout stripped."""
    try:
        # search_path through PGOPTIONS, NOT as a leading "SET ...;" in the same
        # -c: psql echoes "SET" for that statement, so an empty result set came
        # back as the string "SET" and every caller read it as a row.
        env = dict(os.environ)
        env["PGOPTIONS"] = "-c search_path=sn_core,sn_config,public"
        r = subprocess.run(
            ["sudo", "-n", "-u", "postgres",
             "psql", "-d", "smartnode", "-tAc", sql],
            capture_output=True, text=True, timeout=15, env=env,
        )
        if r.returncode != 0:
            err = r.stderr.strip() or r.stdout.strip() or "(no output)"
            return f"PSQL_ERROR(rc={r.returncode}):{err}"
        return r.stdout.strip()
    except Exception as e:
        return f"PSQL_ERROR:{e}"


def psql_ok(out: str) -> bool:
    return not out.startswith("PSQL_ERROR:")


def _sql_literal(value: str) -> str:
    return value.replace("'", "''")


# ---------------------------------------------------------------------------
# Service helpers
# ---------------------------------------------------------------------------

def _doci_base_url() -> str:
    """The Doci address native callers use: /etc/smart-node/doci.env, written by
    companions/documents.sh. Falls back to the container of the Docker-based
    releases, for a node not yet migrated."""
    try:
        for line in Path("/etc/smart-node/doci.env").read_text().splitlines():
            if line.startswith("DOCI_URL="):
                url = line.split("=", 1)[1].strip()
                if url:
                    return url.rstrip("/")
    except OSError:
        pass
    ip = _resolve_container_ip("smart-node-doci")
    return f"http://{ip}" if ip else ""


def _resolve_container_ip(name: str) -> str:
    """Return the first docker network IP for a container, or empty string."""
    try:
        r = subprocess.run(
            ["docker", "inspect", "-f",
             "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
             name],
            capture_output=True, text=True, timeout=10,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _planner_create_task(title: str) -> str | None:
    """Create a planner task. Returns task ID string or None on failure."""
    try:
        resp = _uds_http(
            "POST",
            "/api/planner/api/v1/tasks",
            body={
                "title": title,
                "description": (
                    f"Returning-user acceptance seed task. "
                    f"Created by returning-user.py. Safe to close."
                ),
                "assignee": PLANNER_USER,
                "requester": PLANNER_USER,
                "priority": "low",
            },
            timeout=20,
        )
        if resp.status_code in (200, 201):
            return str(resp.json().get("id") or "")
        print(f"[seed] planner create failed: HTTP {resp.status_code} {resp.text[:200]}", file=sys.stderr)
        return None
    except _Timeout as e:
        print(f"[seed] planner create timeout: {e}", file=sys.stderr)
        return None


def _planner_get_task(task_id: str) -> dict | None:
    """Return planner task dict or None on failure/not found."""
    try:
        resp = _uds_http(
            "GET",
            f"/api/planner/api/v1/tasks/{task_id}",
            timeout=20,
        )
        if resp.status_code == 200:
            return resp.json()
        return None
    except _Timeout:
        return None


def _doci_create(path: str, content: str, doci_url: str) -> str | None:
    """Create a doci document. Returns GUID string or None on failure."""
    if not DOCI_KEY:
        print("[seed] doci: no Doci API key in environment", file=sys.stderr)
        return None
    try:
        resp = _http(
            "POST",
            f"{doci_url}/api/documents.php",
            body={"path": path, "content": content, "title": "Returning-user seed"},
            headers={"X-API-Key": DOCI_KEY},
            timeout=20,
        )
        if resp.status_code in (200, 201):
            data = resp.json()
            return data.get("guid") or data.get("document", {}).get("guid") or ""
        print(f"[seed] doci create failed: HTTP {resp.status_code} {resp.text[:120]}", file=sys.stderr)
        return None
    except _Timeout as e:
        print(f"[seed] doci create timeout: {e}", file=sys.stderr)
        return None


def _doci_get(guid: str, doci_url: str) -> str | None:
    """Return doci document content or None on failure/not found."""
    if not DOCI_KEY:
        return None
    try:
        resp = _http(
            "GET",
            f"{doci_url}/api/documents.php?guid={guid}&content=1",
            headers={"X-API-Key": DOCI_KEY},
            timeout=20,
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("content") or data.get("document", {}).get("content") or ""
        return None
    except _Timeout:
        return None


def _mesh_create(content: str) -> str | None:
    """Create a mesh memory entry. Returns GUID string or None on failure."""
    if not MESH_API:
        print("[seed] mesh: MESH_API_URL not set", file=sys.stderr)
        return None
    try:
        resp = _http(
            "PUT",
            f"{MESH_API}/",
            body={
                "content": content,
                "tags": [f"type:acceptance-seed", "source:returning-user-check"],
                "filename": f"acceptance-seed.md",
            },
            headers={"X-Workspace": "default"},
            timeout=20,
        )
        if resp.status_code in (200, 201):
            return resp.json().get("guid") or ""
        print(f"[seed] mesh create failed: HTTP {resp.status_code} {resp.text[:120]}", file=sys.stderr)
        return None
    except _Timeout as e:
        print(f"[seed] mesh create timeout: {e}", file=sys.stderr)
        return None


def _mesh_get(guid: str) -> str | None:
    """Return mesh document content or None on failure/not found."""
    if not MESH_API:
        return None
    try:
        resp = _http(
            "GET",
            # GET /{guid}. /doc/{guid} is the WRITE path (PUT only) and answers
            # 405 to a GET, which this check reported as a missing entry.
            f"{MESH_API}/{guid}",
            headers={"X-Workspace": "default"},
            timeout=20,
        )
        if resp.status_code == 200:
            return resp.json().get("content") or ""
        return None
    except _Timeout:
        return None


def _inject_and_wait_session(agent: str, timestamp: str) -> str | None:
    """POST a message to agent, poll for a completed session. Returns session ID or None."""
    try:
        resp = _uds_http(
            "POST",
            "/api/messages/inbound",
            body={
                "recipient": agent,
                "content": (
                    "Returning-user acceptance seed. "
                    "Reply with the word SEEDED."
                ),
                "channel_type": "internal",
                "sender": PLANNER_USER,
            },
            timeout=20,
        )
        if resp.status_code not in (200, 201):
            print(f"[seed] session inject failed: HTTP {resp.status_code}", file=sys.stderr)
            return None
        d = resp.json()
        if not (d.get("ok") or d.get("id")):
            print(f"[seed] session inject: API did not confirm: {str(d)[:120]}", file=sys.stderr)
            return None
    except _Timeout as e:
        print(f"[seed] session inject timeout: {e}", file=sys.stderr)
        return None

    print(f"[seed] message injected to {agent}, waiting for completed session ...", file=sys.stderr)

    # Poll sn_core.ai_sessions for a completed turn by this agent after the timestamp.
    poll_interval = 15
    poll_max = 20  # 20 * 15s = 5 minutes
    for i in range(1, poll_max + 1):
        time.sleep(poll_interval)
        row = psql(f"""
            SELECT s.id
            FROM   sn_core.ai_sessions s
            JOIN   sn_config.agents a ON a.id = s.agent_id
            JOIN   sn_core.ai_session_messages m ON m.session_id = s.id
            WHERE  a.slug = '{agent}'
              AND  m.role = 'assistant'
              AND  m.content IS NOT NULL AND m.content <> ''
              AND  m.created_at >= '{timestamp}'::timestamptz - interval '30 seconds'
              AND  s.error IS NULL
            ORDER  BY m.created_at DESC
            LIMIT  1
        """)
        if psql_ok(row) and row:
            print(f"[seed] session complete (id={row})", file=sys.stderr)
            return row
        print(f"[seed] poll {i}/{poll_max} -- no completed session yet", file=sys.stderr)

    print(f"[seed] TIMEOUT -- no completed session for {agent} within 5 minutes", file=sys.stderr)
    return None


def _seed_extra_grant() -> tuple[str, str, str] | None:
    """Add one real, non-baseline DB tool grant without relying on merge=true.

    Seed runs against N-1, where the merge endpoint may not exist yet, so read
    the complete grants document and PUT its union back. Secret refs are copied
    verbatim. Prefer a non-MCP tool so this proves the DB boundary independently
    of server rendering.
    """
    agent = psql("""
        SELECT slug FROM sn_config.agents
        WHERE archived_at IS NULL
          AND (slug = 'foreman' OR capability = 'configurator' OR role = 'Foreman')
        ORDER BY (slug = 'foreman') DESC, id
        LIMIT 1
    """)
    if not psql_ok(agent) or not agent:
        print("[seed] extra grant: setup agent not found", file=sys.stderr)
        return None
    quoted_agent = _sql_literal(agent)
    tool = psql(f"""
        SELECT t.name
        FROM sn_core.tools t
        JOIN sn_config.agents a ON a.slug = '{quoted_agent}'
                              AND a.archived_at IS NULL
        LEFT JOIN sn_config.agent_tools at
               ON at.agent_id = a.id AND at.tool_id = t.id
        WHERE at.tool_id IS NULL AND t.name <> 'secrets'
        ORDER BY CASE WHEN t.kind = 'mcp' THEN 1 ELSE 0 END, t.name
        LIMIT 1
    """)
    if not psql_ok(tool) or not tool:
        print(f"[seed] extra grant: no ungranted registered tool for {agent}", file=sys.stderr)
        return None

    path = f"/api/agents/{urllib.parse.quote(agent, safe='')}/grants"
    try:
        current = _uds_http("GET", path)
        if current.status_code != 200:
            print(f"[seed] extra grant read failed: HTTP {current.status_code}", file=sys.stderr)
            return None
        body = current.json()
        grants = dict(body.get("grants") or {})
        grants[tool] = "r--"
        payload = {"grants": grants, "secret_refs": body.get("secret_refs") or []}
        written = _uds_http("PUT", path, body=payload)
        if written.status_code not in (200, 201):
            print(f"[seed] extra grant write failed: HTTP {written.status_code} {written.text[:160]}", file=sys.stderr)
            return None
    except (_Timeout, ValueError) as exc:
        print(f"[seed] extra grant failed: {exc}", file=sys.stderr)
        return None
    print(f"[seed] extra grant {agent}/{tool}=r--", file=sys.stderr)
    return agent, tool, "r--"


# ---------------------------------------------------------------------------
# Seed mode
# ---------------------------------------------------------------------------

def _seed_agent() -> str:
    """The agent to wake: the override, else the live setup agent by capability, else the older slug."""
    if SEED_AGENT:
        return SEED_AGENT
    found = psql("""
        SELECT slug FROM sn_config.agents
        WHERE archived_at IS NULL AND enabled AND os_user IS NOT NULL AND lower(capability) = 'configurator'
        ORDER BY id LIMIT 1
    """)
    return found if psql_ok(found) and found else SEED_AGENT_FALLBACK


def do_seed() -> int:
    """Create one record in each service, write state JSON to stdout."""
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    seed_agent = _seed_agent()
    state: dict = {"seeded_at": ts, "seed_agent": seed_agent}
    errors: list[str] = []

    # 1. Planner task
    task_title = f"Returning-user seed {ts}"
    task_id = _planner_create_task(task_title)
    if task_id:
        state["task_id"] = task_id
        state["task_title"] = task_title
        print(f"[seed] planner task #{task_id}: {task_title}", file=sys.stderr)
    else:
        errors.append("planner task creation failed")

    # 2. doci document
    doci_url = _doci_base_url()
    doc_content = f"Returning-user acceptance seed created at {ts}. Marker: {SEED_MARKER}."
    doc_path = f"acceptance/returning-user-seed-{ts}.md"
    if doci_url:
        doci_guid = _doci_create(doc_path, doc_content, doci_url)
        if doci_guid:
            state["doci_guid"] = doci_guid
            state["doci_content_marker"] = SEED_MARKER
            state["doci_url"] = doci_url
            print(f"[seed] doci document {doci_guid} at {doc_path}", file=sys.stderr)
        else:
            errors.append("doci document creation failed")
    else:
        errors.append("doci container IP not resolved (smart-node-doci not running?)")

    # 3. mesh memory entry
    mesh_content = f"Returning-user acceptance seed created at {ts}. Marker: {SEED_MARKER}."
    mesh_guid = _mesh_create(mesh_content)
    if mesh_guid:
        state["mesh_guid"] = mesh_guid
        state["mesh_content_marker"] = SEED_MARKER
        print(f"[seed] mesh entry {mesh_guid}", file=sys.stderr)
    else:
        errors.append("mesh memory creation failed")

    # 4. agent session
    session_id = _inject_and_wait_session(seed_agent, ts)
    if session_id:
        state["session_id"] = session_id
        state["session_agent"] = seed_agent
    else:
        errors.append(f"agent session for {seed_agent} not confirmed within 5 minutes")

    # 5. extra grant outside the onboarding baseline, written AFTER the session.
    # The setup agent rewrites its own grants during its first turn; a grant
    # seeded before that turn was revoked by the agent (grant.revoked on
    # foreman/bash, 2026-10-17) and the check then blamed the update for it.
    # What the update must preserve is the grant that stood when the update began.
    extra_grant = _seed_extra_grant()
    if extra_grant:
        grant_agent, grant_tool, grant_mode = extra_grant
        state["extra_grant_agent"] = grant_agent
        state["extra_grant_tool"] = grant_tool
        state["extra_grant_mode"] = grant_mode
    else:
        errors.append("extra agent grant could not be seeded")

    if errors:
        print(f"[seed] INCOMPLETE -- {len(errors)} error(s):", file=sys.stderr)
        for e in errors:
            print(f"[seed]   {e}", file=sys.stderr)
        # Still write whatever state we have; the check will FAIL on missing keys.
        state["seed_errors"] = errors

    json.dump(state, sys.stdout, indent=2)
    print()  # trailing newline
    return 0 if not errors else 1


# ---------------------------------------------------------------------------
# Check mode
# ---------------------------------------------------------------------------

def do_check(state: dict) -> int:
    """Verify each recorded record still exists and content matches."""
    failures: list[str] = []

    if not state:
        print("RETURNING_USER: FAIL no pre-update state (fresh install or seed was not run)")
        return 1

    # 1. Planner task
    task_id = state.get("task_id")
    task_title_expected = state.get("task_title", "")
    if task_id:
        task = _planner_get_task(str(task_id))
        if task is None:
            failures.append(f"planner task #{task_id} not found or unreachable")
        elif task.get("title") != task_title_expected:
            actual = task.get("title", "")[:80]
            failures.append(
                f"planner task #{task_id} title mismatch: "
                f"expected {task_title_expected!r}, got {actual!r}"
            )
        else:
            print(f"[check] planner task #{task_id}: ok", file=sys.stderr)
    else:
        failures.append("planner task was not seeded (seed failure)")

    # 2. doci document
    doci_guid = state.get("doci_guid")
    doci_marker = state.get("doci_content_marker", SEED_MARKER)
    # Resolve container IP fresh: Docker reassigns bridge addresses when
    # containers are recreated during an update, so the IP frozen in state
    # at seed time may point at the wrong container after the update.
    doci_url = _doci_base_url() or state.get("doci_url", "")
    if doci_guid and doci_url:
        if not DOCI_KEY:
            failures.append("doci API key not in environment -- cannot verify doci document")
        else:
            content = _doci_get(doci_guid, doci_url)
            if content is None:
                failures.append(f"doci document {doci_guid} not found or unreachable")
            elif doci_marker not in content:
                failures.append(
                    f"doci document {doci_guid} content does not contain expected marker"
                )
            else:
                print(f"[check] doci document {doci_guid}: ok", file=sys.stderr)
    elif not doci_guid:
        failures.append("doci document was not seeded (seed failure)")
    else:
        failures.append("doci container unreachable and no URL in state")

    # 3. mesh memory entry
    mesh_guid = state.get("mesh_guid")
    mesh_marker = state.get("mesh_content_marker", SEED_MARKER)
    if mesh_guid:
        if not MESH_API:
            failures.append("MESH_API_URL not set -- cannot verify mesh entry")
        else:
            content = _mesh_get(mesh_guid)
            if content is None:
                failures.append(f"mesh entry {mesh_guid} not found or unreachable")
            elif mesh_marker not in content:
                failures.append(
                    f"mesh entry {mesh_guid} content does not contain expected marker"
                )
            else:
                print(f"[check] mesh entry {mesh_guid}: ok", file=sys.stderr)
    else:
        failures.append("mesh entry was not seeded (seed failure)")

    # 4. extra effective grant
    grant_agent = state.get("extra_grant_agent")
    grant_tool = state.get("extra_grant_tool")
    grant_mode = state.get("extra_grant_mode")
    if grant_agent and grant_tool and grant_mode:
        present = psql(f"""
            SELECT count(*)
            FROM sn_config.agent_tools at
            JOIN sn_config.agents a ON a.id = at.agent_id
            JOIN sn_core.tools t ON t.id = at.tool_id
            WHERE a.archived_at IS NULL
              AND a.slug = '{_sql_literal(str(grant_agent))}'
              AND t.name = '{_sql_literal(str(grant_tool))}'
              AND 'read' = ANY(at.granted_caps)
        """)
        if not psql_ok(present) or not present.isdigit() or int(present) < 1:
            failures.append(
                f"extra grant {grant_agent}/{grant_tool}={grant_mode} was not preserved"
            )
        else:
            print(f"[check] extra grant {grant_agent}/{grant_tool}: ok", file=sys.stderr)
    else:
        failures.append("extra agent grant was not seeded (seed failure)")

    # 5. agent session
    session_id = state.get("session_id")
    if session_id:
        row = psql(f"""
            SELECT id FROM sn_core.ai_sessions WHERE id = '{session_id}'::uuid
        """)
        if not psql_ok(row) or not row:
            failures.append(f"ai session id={session_id} not found in sn_core.ai_sessions")
        else:
            print(f"[check] ai session id={session_id}: ok", file=sys.stderr)
    else:
        failures.append("agent session was not seeded (seed failure)")

    # Output the gate line
    if failures:
        detail = "; ".join(failures)
        print(f"RETURNING_USER: FAIL {detail}")
        return 1
    else:
        print("RETURNING_USER: PASS")
        return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    args = sys.argv[1:]

    if "--seed" in args:
        return do_seed()

    if "--check" in args:
        state: dict = {}
        if "--state-file" in args:
            sf_idx = args.index("--state-file")
            if sf_idx + 1 < len(args):
                try:
                    with open(args[sf_idx + 1]) as _f:
                        state = json.load(_f)
                except (json.JSONDecodeError, OSError) as e:
                    print(f"RETURNING_USER: FAIL could not read state file: {e}")
                    return 1
            else:
                print("RETURNING_USER: FAIL --state-file requires a path argument")
                return 1
        elif "--state" in args:
            state_arg_idx = args.index("--state")
            if state_arg_idx + 1 < len(args):
                try:
                    state = json.loads(args[state_arg_idx + 1])
                except json.JSONDecodeError as e:
                    print(f"RETURNING_USER: FAIL could not parse --state JSON: {e}")
                    return 1
        # else: no state -- fresh install, do_check will FAIL correctly
        return do_check(state)

    print(
        "usage: returning-user.py --seed | --check [--state-file <path> | --state <json>]",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
