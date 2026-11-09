"""Operation registry. Each op is self-describing (name, summary, dangerous flag,
input_schema for the kind=api tool catalog) with a pure handler(args)->dict.

`dangerous` is a bool OR a callable(args)->bool. The admin agent is broad by design;
run_command executes as root via sudo. The boundary is owner + audit + confirm
(enforced in app.py), NOT capability restriction.
"""
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Callable, Union

import config
from cmd_safety import is_safe_command
from pty_login import login as _claude_login


@dataclass
class Op:
    name: str
    summary: str
    handler: Callable
    dangerous: Union[bool, Callable] = False
    input_schema: dict = field(default_factory=lambda: {"type": "object", "properties": {}})
    # Each op declares the one permission its caller must hold. The name mirrors
    # sn_config.permissions.name; the central check in permissions.require_permission
    # reads sn_config.role_permissions for the (role, permission) pair. Required:
    # a REGISTRY row with no permission would be reachable without any role check.
    permission: str = ""

    def is_dangerous(self, args: dict) -> bool:
        d = self.dangerous
        return bool(d(args)) if callable(d) else bool(d)


def _node_status(args: dict) -> dict:
    uptime = None
    try:
        with open("/proc/uptime") as f:
            uptime = int(float(f.read().split()[0]))
    except Exception:
        pass
    try:
        load = os.getloadavg()
    except Exception:
        load = None
    du = shutil.disk_usage("/")
    return {"hostname": socket.gethostname(), "uptime_secs": uptime,
            "loadavg": load,
            "disk_root": {"total": du.total, "used": du.used, "free": du.free}}


def _run_command(args: dict) -> dict:
    cmd = args.get("command")
    if not isinstance(cmd, str) or not cmd.strip():
        raise ValueError("command (non-empty string) is required")
    timeout = int(args.get("timeout", 120))
    proc = subprocess.run(["sudo", "-n", "bash", "-lc", cmd],
                          capture_output=True, text=True, timeout=timeout)
    return {"exit_code": proc.returncode,
            "stdout": proc.stdout[-8000:], "stderr": proc.stderr[-4000:]}


_HEALTH_PROBE = os.environ.get(
    "HEALTH_PROBE_PATH", "/srv/tools/health-checker/health-probe.py"
)


def _run_health_probe(args: dict) -> dict:
    """Run the agent health probe immediately outside its 15-minute cron schedule.

    Writes updated rows to sn_core.agent_health_checks (upsert, idempotent).
    Read-only with respect to agent state: no messages sent, no sessions started.
    The probe path defaults to /srv/tools/health-checker/health-probe.py and can
    be overridden by HEALTH_PROBE_PATH.
    """
    if not os.path.isfile(_HEALTH_PROBE):
        raise ValueError(f"health probe not installed at {_HEALTH_PROBE}")
    proc = subprocess.run(
        ["python3", _HEALTH_PROBE],
        capture_output=True, text=True, timeout=120
    )
    return {
        "exit_code": proc.returncode,
        "stdout": proc.stdout[-4000:],
        "stderr": proc.stderr[-2000:],
    }


_SVC_ACTIONS = {"status", "start", "stop", "restart", "reload"}


def _service_control(args: dict) -> dict:
    unit = args.get("unit")
    action = args.get("action")
    if action not in _SVC_ACTIONS:
        raise ValueError("action must be one of " + ", ".join(sorted(_SVC_ACTIONS)))
    if not isinstance(unit, str) or not unit.strip():
        raise ValueError("unit (string) is required")
    proc = subprocess.run(["sudo", "-n", "systemctl", action, unit],
                          capture_output=True, text=True, timeout=60)
    return {"exit_code": proc.returncode,
            "stdout": proc.stdout[-6000:], "stderr": proc.stderr[-2000:]}


def _claude_login_start(args: dict) -> dict:
    return _claude_login.start(config.CLAUDE_HOME)


def _claude_login_complete(args: dict) -> dict:
    code = args.get("code")
    if not isinstance(code, str) or not code.strip():
        raise ValueError("code (string) is required")
    return _claude_login.complete(code)


# ---------------------------------------------------------------------------
# Cron materialize / rollback
#
# core-api renders sn_core.crons -> a cron.d body and hands it here. This
# service owns the privileged write to /etc/cron.d so core-api stays
# unprivileged. The op is narrow by construction: ONE fixed target file, a
# strictly validated body (every job line must route through cron-runner.sh),
# pre-write backup + a last-good pointer, atomic install, and a sibling
# rollback op. That narrowness -- not a confirm prompt -- is the safety
# boundary, so these are dangerous=False (callable by the reconcile timer and
# the panel button without a round-trip). Owner-gating + audit still apply.
# ---------------------------------------------------------------------------
CRON_TARGET = "/etc/cron.d/smart-node-agents"
CRON_BACKUP_DIR = "/var/backups/smart-node/cron"
CRON_LAST_GOOD = CRON_BACKUP_DIR + "/smart-node-agents.last-good.bak"
CRON_RUNNER = "/usr/local/lib/smart-node/cron-runner.sh"
CRON_MAX_BACKUPS = 10

_JOB_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_ENV_KEYS = {"SHELL", "PATH", "MAILTO"}
_BACKUP_NAME_RE = re.compile(r"^smart-node-agents\.[0-9A-Za-z:_-]+\.bak$")


def _validate_cron_body(body) -> int:
    """Fail-closed lint. Returns job count, or raises ValueError.

    Allowed lines: blank, comments (#...), env KEY=VALUE for SHELL/PATH/MAILTO,
    and cron job lines whose command routes through cron-runner.sh with a
    valid job slug. Anything else is rejected so this op can never become an
    arbitrary-root-write primitive.
    """
    if not isinstance(body, str) or not body.strip():
        raise ValueError("body (non-empty string) is required")
    if "\x00" in body:
        raise ValueError("body contains a NUL byte")
    jobs = 0
    for i, raw in enumerate(body.splitlines(), 1):
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_]+)=", s)
        if m:
            if m.group(1) not in _ENV_KEYS:
                raise ValueError("line %d: disallowed env assignment %r" % (i, m.group(1)))
            continue
        fields = s.split()
        # Either a @macro schedule (1 field) or a classic 5-field schedule.
        if fields[0].startswith("@"):
            if len(fields) < 4:
                raise ValueError("line %d: malformed @-schedule cron line" % i)
            user, runner, job = fields[1], fields[2], fields[3]
        else:
            if len(fields) < 8:
                raise ValueError("line %d: malformed cron line (need 5-field schedule + user + runner + job)" % i)
            user, runner, job = fields[5], fields[6], fields[7]
        if not _USER_RE.match(user):
            raise ValueError("line %d: invalid crontab user %r" % (i, user))
        if runner != CRON_RUNNER:
            raise ValueError("line %d: command must route through %s (got %r)" % (i, CRON_RUNNER, runner))
        if not _JOB_RE.match(job):
            raise ValueError("line %d: invalid job name %r" % (i, job))
        jobs += 1
    # jobs == 0 is legitimate: the "all crons disabled" projection is a
    # comment-only (header) body, which cron treats as inert. Only a truly
    # empty/whitespace body (caught above) or a malformed line is rejected.
    return jobs


# Privileged install: backup current -> timestamped + last-good, atomic
# replace, prune. Runs as root via sudo. Args: TMP TARGET BKDIR LASTGOOD MAX.
_CRON_INSTALL_SH = r"""
set -euo pipefail
TMP="$1"; TARGET="$2"; BKDIR="$3"; LASTGOOD="$4"; MAX="$5"
mkdir -p "$BKDIR"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
if [ -f "$TARGET" ]; then
  cp -p "$TARGET" "$BKDIR/smart-node-agents.$TS.bak"
fi
install -m 0644 -o root -g root "$TMP" "$TARGET.new"
mv -f "$TARGET.new" "$TARGET"
cp -p "$TARGET" "$LASTGOOD"
chmod 0644 "$LASTGOOD"
{ ls -1t "$BKDIR"/smart-node-agents.*.bak 2>/dev/null | grep -v 'last-good' | tail -n +$((MAX+1)) | xargs -r rm -f; } || true
echo "OK $TS"
"""

_CRON_RESTORE_SH = r"""
set -euo pipefail
SRC="$1"; TARGET="$2"
[ -f "$SRC" ] || { echo "no such backup: $SRC" >&2; exit 3; }
cp -p "$SRC" "$TARGET.new"
mv -f "$TARGET.new" "$TARGET"
echo "RESTORED $SRC"
"""


def _materialize_crons(args: dict) -> dict:
    body = args.get("body")
    job_count = _validate_cron_body(body)
    if not body.endswith("\n"):
        body += "\n"
    # No-op if identical to the live file. This keeps the periodic reconcile
    # timer from creating a backup on every tick (backup only on real change).
    try:
        with open(CRON_TARGET, "r", encoding="utf-8") as f:
            if f.read() == body:
                return {"target": CRON_TARGET, "jobs": job_count,
                        "bytes": len(body), "unchanged": True}
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(prefix="sn-crons-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(body)
        proc = subprocess.run(
            ["sudo", "-n", "bash", "-c", _CRON_INSTALL_SH, "_",
             tmp, CRON_TARGET, CRON_BACKUP_DIR, CRON_LAST_GOOD, str(CRON_MAX_BACKUPS)],
            capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    if proc.returncode != 0:
        raise RuntimeError("cron install failed: " + (proc.stderr or proc.stdout)[-2000:])
    return {"target": CRON_TARGET, "jobs": job_count, "bytes": len(body),
            "unchanged": False, "result": proc.stdout.strip()[-500:]}


def _rollback_crons(args: dict) -> dict:
    which = args.get("backup") or "last-good"
    if which == "last-good":
        src = CRON_LAST_GOOD
    else:
        if not _BACKUP_NAME_RE.match(str(which)):
            raise ValueError("invalid backup name (expect 'last-good' or smart-node-agents.<ts>.bak)")
        src = CRON_BACKUP_DIR + "/" + which
    proc = subprocess.run(
        ["sudo", "-n", "bash", "-c", _CRON_RESTORE_SH, "_", src, CRON_TARGET],
        capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError("cron rollback failed: " + (proc.stderr or proc.stdout)[-2000:])
    return {"restored_from": src, "target": CRON_TARGET, "result": proc.stdout.strip()[-500:]}


# ---------------------------------------------------------------------------
# Agent OS users
#
# An agent whose sn_config.agents row carries an os_user needs that system user
# to EXIST on the host: so its wake-cron can run AS it (not a fallback service
# user that cannot write the agent dir), and so it has a HOME for ~/.claude.
# core-api is unprivileged and cannot create host users, so it hands the agent
# roster (os_user + slug, from sn_config.agents) here. Narrow by construction --
# the same safety model as materialize_crons, NOT a confirm prompt:
#   * os_user MUST match ^agent-<slug> (an operator/service user like `root` or
#     `smart-node-gateway` can never be created/chowned through this op),
#   * the HOME is forced under one fixed base, the group is fixed,
#   * chown is limited to the agent's own logs/ + state/ under a fixed root.
# Idempotent (create-if-missing, repair group/home, re-chown) so the reconcile
# timer can call it every tick. dangerous=False for the same reason.
# ---------------------------------------------------------------------------
AGENTS_ROOT = "/srv/agents"
AGENT_HOME_BASE = "/var/lib/smart-node/agents"
# The AGENT group -- deliberately NOT the smart-node service group.
#
# previously this was "smart-node", the group that owns the core tree and the
# node's secrets. Agents were put in it for wake-cron writes so their wake-cron could write
# their own log dir; the side effect was that every agent could create files in
# /opt/smart-node, /srv/tools and /srv/agents, read the age key, and rewrite the
# Authum account store.
#
# NOTE for whoever changes this again: _ENSURE_USER_SH below only ever ADDS a
# group to an existing user (usermod -a -G). Changing this constant alone does
# NOT remove a stale membership of the old group, so an already-installed node
# would look fixed while staying open. bootstrap/permissions.sh does the removal.
AGENT_GROUP = "smart-node-agents"
AGENT_SHELL = "/usr/sbin/nologin"
_AGENT_USER_RE = re.compile(r"^agent-[a-z][a-z0-9-]{0,40}$")
_AGENT_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")

# Privileged, idempotent user+ownership ensure. Runs as root via sudo.
# Args: USER HOME_DIR GROUP SHELL_BIN AGENT_DIR.
_ENSURE_USER_SH = r"""
set -euo pipefail
USER="$1"; HOME_DIR="$2"; GROUP="$3"; SHELL_BIN="$4"; AGENT_DIR="$5"
getent group "$GROUP" >/dev/null 2>&1 || groupadd --system "$GROUP"
if ! id -u "$USER" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "$HOME_DIR" \
          --gid "$GROUP" --shell "$SHELL_BIN" "$USER"
else
  usermod -a -G "$GROUP" "$USER" >/dev/null 2>&1 || true
  if [ ! -d "$HOME_DIR" ]; then
    mkdir -p "$HOME_DIR"
    chown "$USER":"$GROUP" "$HOME_DIR"
    chmod 0750 "$HOME_DIR"
  fi
fi
# Own the agent's writable runtime dirs so the wake-cron (running as USER) can
# write them. Only logs/ + state/ + work/ -- never the whole agent dir (config
# files stay owned by the provisioner).
#
# work/ is the agent's own work area. Until it existed an agent could
# write nowhere but /tmp, which is why "the developer writes code" was
# impossible on a node -- a permissions fact, not a policy. /srv/projects is the
# SHARED area and does not cover this: an agent with no project still needs
# somewhere of its own. Ownership is the boundary here; the fs_write line in the
# prompt is an instruction, not a lock (docs/node/entities.md says so plainly).
#
# The mode is set here, to exactly what bootstrap/permissions.list declares for
# these three (2750). mkdir alone gave 2755, so every agent created since the
# last permissions apply showed as drift from the declared surface, and the
# e2e permissions check failed on the 1.12.46 stand.
if [ -d "$AGENT_DIR" ]; then
  for sub in logs state work; do
    mkdir -p "$AGENT_DIR/$sub"
    chown -R "$USER":"$GROUP" "$AGENT_DIR/$sub"
    chmod 2750 "$AGENT_DIR/$sub"
  done
fi
# The CLI state directory, group-readable ON PURPOSE.
#
# The Claude CLI creates ~/.claude/projects itself, mode 0700, owned by the
# agent. The gateway runs as smart-node-gateway, a member of GROUP but not the
# agent, so it cannot read that directory -- and it MUST, because it looks for
# an existing transcript there to decide between --session-id and --resume.
# Unreadable, that lookup answers "no transcript" for every session, the CLI is
# handed the wrong flag, and it exits 1 with "No conversation found". The turn
# is then retried, which fails the same way: on the live node this produced 316
# spawns in eight minutes, no completed turn and no spend, while validate
# reported 195/195. A permission that reads as a state answer is invisible.
#
# 0750 gives the group exactly what it needs, read and traverse, and nothing
# else: the same arrangement /etc/smart-node/agent-runtime.env already uses for
# the same reason. Re-applied on every reconcile tick because the CLI recreates
# the directory 0700 whenever it is missing.
for d in "$HOME_DIR/.claude" "$HOME_DIR/.claude/projects"; do
  mkdir -p "$d"
  chown "$USER":"$GROUP" "$d"
  chmod 0750 "$d"
done
echo "OK $USER"
"""


# ---------------------------------------------------------------------------
# Project provisioning
#
# A project is a registry row, a unix group and a directory. This op owns the
# second and third: the row is core-api's, and it calls here.
#
# FLAT, at /srv/projects/<slug> -- never nested under an owner. A filesystem
# path expresses exactly ONE hierarchy, so putting the workspace in the path
# leaves a project shared by two workspaces with no home and makes a change of
# owner a physical move. the owner-in-path layout is superseded.
#
# 2770 setgid is the whole mechanism. setgid makes every file created inside
# inherit the project group, so a member writes and the next member can read it
# without anyone remembering to chgrp. Without setgid the directory works on
# the first day and silently stops working on the second.
#
# Same class of privilege as _ENSURE_USER_SH above, deliberately: groupadd,
# mkdir, chgrp, usermod. No new component, no new sudo rule.
_PROJECT_ROOT = "/srv/projects"
_PROJECT_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
_PROJECT_GROUP_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
_MEMBER_USER_RE = re.compile(r"^(agent-[a-z][a-z0-9-]{0,40}|[a-z][a-z0-9._-]{0,31})$")

# Args: ROOT SLUG GROUP [MEMBER...]
_ENSURE_PROJECT_SH = r"""
set -euo pipefail
ROOT="$1"; SLUG="$2"; GROUP="$3"; shift 3
# The shared root itself is 0755 root:root: everyone may traverse it to reach a
# project they belong to, and nobody may create a project by making a directory.
mkdir -p "$ROOT"
chown root:root "$ROOT"
chmod 0755 "$ROOT"

getent group "$GROUP" >/dev/null 2>&1 || groupadd --system "$GROUP"

DIR="$ROOT/$SLUG"
mkdir -p "$DIR"
chgrp "$GROUP" "$DIR"
# 2770: members read/write, setgid so new files inherit the group, and NOTHING
# for anyone else. The last part is the boundary a non-member hits.
chmod 2770 "$DIR"

for m in "$@"; do
  id -u "$m" >/dev/null 2>&1 || { echo "SKIP unknown user $m" >&2; continue; }
  usermod -a -G "$GROUP" "$m" >/dev/null 2>&1 || echo "SKIP could not add $m" >&2
done
echo "OK $SLUG"
"""


def _ensure_project(args: dict) -> dict:
    """Create or reconcile one project's group + directory + membership.

    Idempotent: run it again after adding a member and only the membership
    changes. It never REMOVES a member -- same limitation as
    _ENSURE_USER_SH, and stated here rather than discovered later: revoking
    access needs an explicit removal path, and until that exists a member
    removed from the registry row still holds the group.
    """
    slug = (args.get("slug") or "").strip()
    if not _PROJECT_SLUG_RE.match(slug):
        raise ValueError("invalid project slug %r (a-z, digits, hyphen; 2-41 chars)" % slug)
    group = (args.get("unix_group") or ("project-" + slug)).strip()
    if not _PROJECT_GROUP_RE.match(group):
        raise ValueError("invalid unix_group %r" % group)
    members = args.get("members") or []
    if not isinstance(members, list):
        raise ValueError("members must be a list of OS user names")
    for m in members:
        if not isinstance(m, str) or not _MEMBER_USER_RE.match(m.strip()):
            raise ValueError("invalid member os_user %r" % m)
    members = [m.strip() for m in members]

    proc = subprocess.run(
        ["sudo", "-n", "bash", "-c", _ENSURE_PROJECT_SH, "_",
         _PROJECT_ROOT, slug, group] + members,
        capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError("ensure_project failed for %r: %s"
                           % (slug, (proc.stderr or proc.stdout)[-1000:]))
    # A SKIP is not a failure of the run but it IS a member who did not get
    # access, and returning it unread is how "I added them and it did not work"
    # becomes a mystery.
    skipped = [ln for ln in (proc.stderr or "").splitlines() if ln.startswith("SKIP")]
    return {"slug": slug, "unix_group": group,
            "fs_path": _PROJECT_ROOT + "/" + slug,
            "members": members, "skipped": skipped}


def _ensure_agent_users(args: dict) -> dict:
    agents = args.get("agents")
    if not isinstance(agents, list) or not agents:
        raise ValueError("agents (non-empty list of {os_user, slug}) is required")
    ensured, failed = [], []
    for a in agents:
        if not isinstance(a, dict):
            raise ValueError("each agent must be an object with os_user + slug")
        user = (a.get("os_user") or "").strip()
        slug = (a.get("slug") or "").strip()
        if not _AGENT_USER_RE.match(user):
            raise ValueError("invalid agent os_user %r (must match agent-<slug>)" % user)
        if not _AGENT_SLUG_RE.match(slug):
            raise ValueError("invalid agent slug %r" % slug)
        home = AGENT_HOME_BASE + "/" + user
        agent_dir = AGENTS_ROOT + "/" + slug
        try:
            proc = subprocess.run(
                ["sudo", "-n", "bash", "-c", _ENSURE_USER_SH, "_",
                 user, home, AGENT_GROUP, AGENT_SHELL, agent_dir],
                capture_output=True, text=True, timeout=60)
            if proc.returncode != 0:
                failed.append({"os_user": user, "slug": slug,
                               "error": (proc.stderr or proc.stdout)[-1000:]})
            else:
                ensured.append({"os_user": user, "slug": slug, "home": home})
        except subprocess.SubprocessError as e:
            failed.append({"os_user": user, "slug": slug, "error": str(e)[-500:]})
    return {"ensured": len(ensured), "failed": len(failed),
            "agents": ensured, "errors": failed}


# ---------------------------------------------------------------------------
# Agent runtime env projection
#
# The node's LLM credential (a Claude OAuth token) is the source-of-truth in
# sn_config.gateway_secrets (pgcrypto). The gateway consumes it as an ENV VAR
# from /etc/smart-node/agent-runtime.env (EnvironmentFile=). core-api reads +
# decrypts the credential from the DB, renders a KEY=VALUE body, and hands it
# here; this service owns the privileged write (0640, group below) and restarts
# the gateway so it picks up the new value. Same safety model as
# materialize_crons: ONE fixed target, a strictly validated body (only KEY=VALUE
# lines -- no shell injection), atomic install. dangerous=False (callable
# on-store and on-demand without a confirm).
# ---------------------------------------------------------------------------
AGENT_ENV_TARGET = "/etc/smart-node/agent-runtime.env"
# The AGENT group, not the service group.
#
# This file is how an agent gets its model credential, and agents left the
# service group when credentials moved to the agent group. Rendering
# it root:smart-node made it unreadable to
# every agent -- and the failure is silent: the wake runs, the CLI reports
# "Not logged in", the agent exits 0 with an empty log, and it reads as an LLM
# outage rather than a permission error. Service users that need it (the
# gateway) are members of the agent group, so both sides can read it.
AGENT_ENV_GROUP = "smart-node-agents"
GATEWAY_UNIT = "smart-node-gateway.service"
_ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _validate_env_body(body) -> int:
    """Fail-closed lint for an EnvironmentFile body. Returns the KEY count, or
    raises ValueError. Allowed lines: blank, comments (#...), and KEY=VALUE where
    KEY is an uppercase env identifier and VALUE is single-line + control-char
    free (an opaque token). Anything else is rejected so this op can never become
    an arbitrary-root-write primitive."""
    if not isinstance(body, str) or not body.strip():
        raise ValueError("body (non-empty string) is required")
    if "\x00" in body:
        raise ValueError("body contains a NUL byte")
    n = 0
    for i, raw in enumerate(body.splitlines(), 1):
        st = raw.strip()
        if not st or st.startswith("#"):
            continue
        if "=" not in raw:
            raise ValueError("line %d: not a KEY=VALUE assignment" % i)
        key, _, val = raw.partition("=")
        key = key.strip()
        if not _ENV_KEY_RE.match(key):
            raise ValueError("line %d: invalid env key %r" % (i, key))
        if any(ord(c) < 0x20 for c in val):
            raise ValueError("line %d: control character in value" % i)
        n += 1
    if n == 0:
        raise ValueError("body declares no KEY=VALUE pairs")
    return n


# Privileged, atomic install + gateway restart. Runs as root via sudo.
# Args: TMP TARGET GROUP UNIT.
_ENV_INSTALL_SH = r"""
set -euo pipefail
TMP="$1"; TARGET="$2"; GROUP="$3"; UNIT="$4"
install -m 0640 -o root -g "$GROUP" "$TMP" "$TARGET.new"
mv -f "$TARGET.new" "$TARGET"
# systemd re-reads EnvironmentFile only on (re)start, not reload; try-restart
# restarts iff the unit is already running (won't spuriously start a stopped
# gateway). Best-effort: a not-yet-installed gateway must not fail the write.
systemctl try-restart "$UNIT" >/dev/null 2>&1 || true
echo "OK"
"""


def _render_agent_runtime_env(args: dict) -> dict:
    body = args.get("body")
    keys = _validate_env_body(body)
    if not body.endswith("\n"):
        body += "\n"
    # No-op if identical to the live file (keeps a re-projection from bouncing
    # the gateway on every call).
    try:
        with open(AGENT_ENV_TARGET, "r", encoding="utf-8") as f:
            if f.read() == body:
                return {"target": AGENT_ENV_TARGET, "keys": keys,
                        "bytes": len(body), "unchanged": True}
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(prefix="sn-agentenv-", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(body)
        proc = subprocess.run(
            ["sudo", "-n", "bash", "-c", _ENV_INSTALL_SH, "_",
             tmp, AGENT_ENV_TARGET, AGENT_ENV_GROUP, GATEWAY_UNIT],
            capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    if proc.returncode != 0:
        raise RuntimeError("agent-runtime.env install failed: " + (proc.stderr or proc.stdout)[-2000:])
    return {"target": AGENT_ENV_TARGET, "keys": keys, "bytes": len(body),
            "unchanged": False, "result": proc.stdout.strip()[-500:]}


# ---------------------------------------------------------------------------
# LXD test-node lifecycle
#
# Three operations: lxc_node_provision, lxc_node_destroy, lxc_node_status.
# Same pattern as all other ops: short idempotent bash templates, strictly
# validated argument lists, confirm-on-destructive gate.
#
# PREREQUISITE (operator, not this task): the LXD unix socket must be
# bind-mounted into the admin-runner environment and the admin-runner service
# user must be a member of the lxd group on the host. Without this the lxc
# commands will fail at runtime, not at import time.
#
# Test-node registry: a flat file (one container name per line).
# lxc_node_provision appends the name on success.
# lxc_node_destroy removes the name on success and refuses to act if the name
# is absent -- this prevents accidental deletion of non-test containers.
#
# Credential isolation: lxc_node_provision accepts an optional seed_manifest
# dict. Before lxc launch the bash template reads /etc/smart-node/*.env and
# refuses any manifest value that also appears in a live env file. Disconnected
# mode (no manifest) is the safe default and is accepted without any credential
# arguments.
# ---------------------------------------------------------------------------
_CONTAINER_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
_INSTALLER_VERSION_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._+-]{0,60}$")
_SEED_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

# Args passed as argv: NAME VERSION REGISTRY BASE_IMAGE NETWORK INSTALLER_URL_BASE SEED_PATH
# SEED_PATH is an absolute path to a KEY=VALUE temp file, or the literal string
# "disconnected" when no seed manifest is provided.
_LXC_PROVISION_SH = r"""
set -euo pipefail
NAME="$1"; VERSION="$2"; REGISTRY="$3"; BASE_IMAGE="$4"
NETWORK="$5"; INSTALLER_URL_BASE="$6"; SEED="$7"

# Guard 1: refuse if NAME matches the live node hostname
LIVE=$(hostname)
if [ "$NAME" = "$LIVE" ]; then
    printf 'REFUSED: "%s" is the live node hostname\n' "$NAME" >&2
    exit 4
fi

# Guard 2: idempotency -- already exists, report state and return cleanly
if lxc info "$NAME" >/dev/null 2>&1; then
    STATE=$(lxc info "$NAME" 2>/dev/null | awk '/^Status:/{print $2}')
    printf '{"name":"%s","state":"%s","idempotent":true}\n' "$NAME" "${STATE:-unknown}"
    exit 0
fi

# Guard 3: credential isolation -- runs BEFORE lxc launch so nothing is
# created if a live credential is found in the seed manifest.
if [ "$SEED" != "disconnected" ] && [ -f "$SEED" ]; then
    while IFS= read -r line; do
        [[ "$line" == '#'* ]] && continue     # comment
        [[ "$line" != *'='* ]] && continue    # not KEY=VALUE
        val="${line#*=}"                       # everything after first =
        [ -z "$val" ] && continue             # empty value is not a credential
        key="${line%%=*}"
        for envfile in /etc/smart-node/*.env; do
            [ -f "$envfile" ] || continue
            if grep -qF -- "$val" "$envfile" 2>/dev/null; then
                printf 'REFUSED: credential "%s" matches a live node secret (found in %s)\n' \
                    "$key" "$envfile" >&2
                exit 5
            fi
        done
    done < "$SEED"
fi

# Launch the container
lxc launch "$BASE_IMAGE" "$NAME" --network "$NETWORK"

# Wait for exec readiness (up to 60 s)
READY=0
for _i in $(seq 1 30); do
    if lxc exec "$NAME" -- true >/dev/null 2>&1; then
        READY=1; break
    fi
    sleep 2
done
if [ "$READY" -eq 0 ]; then
    printf 'FAILED: container "%s" not ready within 60s\n' "$NAME" >&2
    lxc delete --force "$NAME" >/dev/null 2>&1 || true
    exit 6
fi

# Push seed manifest into container if provided
if [ "$SEED" != "disconnected" ] && [ -f "$SEED" ]; then
    lxc file push "$SEED" "$NAME/tmp/seed-manifest.env"
    SEED_FLAG="--seed-manifest"
    SEED_VALUE="/tmp/seed-manifest.env"
else
    SEED_FLAG="--disconnected"
    SEED_VALUE=""
fi

# Run the installer if a URL base is configured
if [ -n "$INSTALLER_URL_BASE" ]; then
    INSTALLER_URL="${INSTALLER_URL_BASE%/}/install-${VERSION}.sh"
    lxc exec "$NAME" -- curl -fsSL "$INSTALLER_URL" -o /tmp/sn-install.sh
    lxc exec "$NAME" -- chmod +x /tmp/sn-install.sh
    if [ -n "$SEED_VALUE" ]; then
        lxc exec "$NAME" -- /tmp/sn-install.sh "$SEED_FLAG" "$SEED_VALUE"
    else
        lxc exec "$NAME" -- /tmp/sn-install.sh "$SEED_FLAG"
    fi
fi

# Register the container in the test-node registry
mkdir -p "$(dirname "$REGISTRY")"
grep -qxF "$NAME" "$REGISTRY" 2>/dev/null || echo "$NAME" >> "$REGISTRY"

printf '{"name":"%s","version":"%s","state":"running","idempotent":false}\n' "$NAME" "$VERSION"
"""

# Args passed as argv: NAME REGISTRY
_LXC_DESTROY_SH = r"""
set -euo pipefail
NAME="$1"; REGISTRY="$2"

# Guard 1: refuse if NAME matches the live node hostname
LIVE=$(hostname)
if [ "$NAME" = "$LIVE" ]; then
    printf 'REFUSED: "%s" is the live node hostname\n' "$NAME" >&2
    exit 4
fi

# Guard 2: must be present in the test-node registry
if ! grep -qxF "$NAME" "$REGISTRY" 2>/dev/null; then
    printf 'REFUSED: "%s" is not in the test-node registry (%s)\n' "$NAME" "$REGISTRY" >&2
    exit 5
fi

# Stop and delete
if lxc info "$NAME" >/dev/null 2>&1; then
    lxc stop "$NAME" --force >/dev/null 2>&1 || true
    lxc delete "$NAME" --force
fi

# Remove from registry
if [ -f "$REGISTRY" ]; then
    TMP=$(mktemp)
    grep -vxF "$NAME" "$REGISTRY" > "$TMP" 2>/dev/null || true
    mv -f "$TMP" "$REGISTRY"
fi

printf '{"name":"%s","destroyed":true}\n' "$NAME"
"""

# Args passed as argv: NAME
_LXC_STATUS_SH = r"""
set -euo pipefail
NAME="$1"

if ! lxc info "$NAME" >/dev/null 2>&1; then
    printf '{"name":"%s","found":false}\n' "$NAME"
    exit 0
fi

INFO=$(lxc info "$NAME" 2>&1)
STATE=$(printf '%s\n' "$INFO" | awk '/^Status:/{print $2}')

HEALTH=null
if [ "${STATE:-}" = "Running" ]; then
    HEALTH_RAW=$(lxc exec "$NAME" -- \
        curl -fsSL --connect-timeout 5 --max-time 10 http://localhost/health \
        2>/dev/null || true)
    if [ -n "$HEALTH_RAW" ]; then
        # Wrap in a JSON string (escape backslash and double-quote)
        ESCAPED=$(printf '%s' "$HEALTH_RAW" | sed 's/\\/\\\\/g; s/"/\\"/g')
        HEALTH="\"$ESCAPED\""
    fi
fi

printf '{"name":"%s","found":true,"state":"%s","health":%s}\n' \
    "$NAME" "${STATE:-unknown}" "$HEALTH"
"""


def _validate_container_name(args: dict) -> str:
    name = (args.get("name") or "").strip()
    if not _CONTAINER_NAME_RE.match(name):
        raise ValueError(
            "invalid container name %r (lowercase a-z, digits, hyphens; "
            "2-41 chars; must start with a letter)" % name
        )
    return name


def _lxc_node_provision(args: dict) -> dict:
    name = _validate_container_name(args)

    version = (args.get("installer_version") or "").strip()
    if not _INSTALLER_VERSION_RE.match(version):
        raise ValueError("invalid installer_version %r" % version)

    seed_manifest = args.get("seed_manifest")  # dict or None
    if seed_manifest is not None and not isinstance(seed_manifest, dict):
        raise ValueError("seed_manifest must be an object (KEY: VALUE pairs) or null")

    # Validate seed manifest keys and values before writing to disk.
    if seed_manifest:
        for k, v in seed_manifest.items():
            if not isinstance(k, str) or not _SEED_KEY_RE.match(k):
                raise ValueError(
                    "invalid seed_manifest key %r "
                    "(must be uppercase letters, digits and underscores)" % k
                )
            if not isinstance(v, str):
                raise ValueError("seed_manifest value for %r must be a string" % k)
            if "\n" in v or "\r" in v:
                raise ValueError(
                    "seed_manifest value for %r must be a single line" % k
                )

    seed_path = "disconnected"
    tmp_path = None
    try:
        if seed_manifest:
            fd, tmp_path = tempfile.mkstemp(prefix="sn-seed-", suffix=".env")
            try:
                with os.fdopen(fd, "w") as f:
                    for k, v in seed_manifest.items():
                        f.write("%s=%s\n" % (k, v))
            except Exception:
                os.close(fd)
                raise
            seed_path = tmp_path

        proc = subprocess.run(
            ["sudo", "-n", "bash", "-c", _LXC_PROVISION_SH, "_",
             name, version,
             config.TEST_NODE_REGISTRY, config.LXC_BASE_IMAGE,
             config.LXC_NETWORK, config.LXC_INSTALLER_URL_BASE,
             seed_path],
            capture_output=True, text=True, timeout=600)
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    if proc.returncode == 4:
        raise ValueError(
            "refused: container name %r matches the live node hostname" % name
        )
    if proc.returncode == 5:
        raise RuntimeError(
            "refused: seed manifest contains a live node credential. "
            + (proc.stderr or "")[-500:]
        )
    if proc.returncode != 0:
        raise RuntimeError(
            "lxc_node_provision failed: "
            + (proc.stderr or proc.stdout)[-2000:]
        )

    try:
        return json.loads(proc.stdout.strip())
    except Exception:
        return {"raw": proc.stdout[-2000:]}


def _lxc_node_destroy(args: dict) -> dict:
    name = _validate_container_name(args)

    proc = subprocess.run(
        ["sudo", "-n", "bash", "-c", _LXC_DESTROY_SH, "_",
         name, config.TEST_NODE_REGISTRY],
        capture_output=True, text=True, timeout=120)

    if proc.returncode == 4:
        raise ValueError(
            "refused: container name %r matches the live node hostname" % name
        )
    if proc.returncode == 5:
        raise ValueError(
            "refused: %r is not in the test-node registry" % name
        )
    if proc.returncode != 0:
        raise RuntimeError(
            "lxc_node_destroy failed: "
            + (proc.stderr or proc.stdout)[-2000:]
        )

    try:
        return json.loads(proc.stdout.strip())
    except Exception:
        return {"raw": proc.stdout[-2000:]}


def _lxc_node_status(args: dict) -> dict:
    name = _validate_container_name(args)

    proc = subprocess.run(
        ["sudo", "-n", "bash", "-c", _LXC_STATUS_SH, "_", name],
        capture_output=True, text=True, timeout=60)

    if proc.returncode != 0:
        raise RuntimeError(
            "lxc_node_status failed: "
            + (proc.stderr or proc.stdout)[-1000:]
        )

    try:
        return json.loads(proc.stdout.strip())
    except Exception:
        return {"raw": proc.stdout[-2000:]}


# ---------------------------------------------------------------------------
# What a Flint agent remembers about the people it talks to
#
# Flint keeps it in its own store in the agent's home: facts it was told and
# traits it inferred (tables facts and user_model, both mirrored in memory_fts
# for its search). The store is SQLite in WAL mode, owned by the agent, so even
# a read must run as the agent (a reader writes the -shm index); core-api has
# no such right and asks here. The script runs as agent-<slug> via sudo, never
# as root, so the files stay the agent's. Only these two tables are touched:
# patterns, reflections and skills are how the agent works, not what it knows
# about a person.
#
# dangerous=False on purpose: forgetting is the owner's own click on the
# agent's page, which asks before "forget everything".
# ---------------------------------------------------------------------------
_FLINT_MEMORY_DB = ".flint/memory/index.sqlite"
_MEMORY_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_AGENT_MEMORY_PY = r'''
import json, os, sqlite3, sys
action, db, entry = sys.argv[1], sys.argv[2], sys.argv[3]
if not os.path.isfile(db):
    print(json.dumps({"items": [], "removed": 0})); sys.exit(0)
con = sqlite3.connect(db, timeout=10)
if action == "list":
    items = [{"id": i, "kind": "fact", "text": c, "at": t}
             for i, c, t in con.execute("SELECT id, content, created_at FROM facts ORDER BY created_at DESC")]
    items += [{"id": i, "kind": "trait", "text": k.replace("_", " ") + ": " + v, "at": t}
              for i, k, v, t in con.execute("SELECT id, trait, value, updated_at FROM user_model ORDER BY updated_at DESC")]
    print(json.dumps({"items": items})); sys.exit(0)
with con:
    if action == "forget_all":
        n = con.execute("DELETE FROM facts").rowcount + con.execute("DELETE FROM user_model").rowcount
        con.execute("DELETE FROM memory_fts WHERE layer IN ('facts', 'user_model')")
    else:
        n = con.execute("DELETE FROM facts WHERE id = ?", (entry,)).rowcount \
            + con.execute("DELETE FROM user_model WHERE id = ?", (entry,)).rowcount
        con.execute("DELETE FROM memory_fts WHERE entry_id = ? AND layer IN ('facts', 'user_model')", (entry,))
print(json.dumps({"removed": n}))
'''


def _agent_memory(args: dict) -> dict:
    user = (args.get("os_user") or "").strip()
    action = args.get("action") or "list"
    entry = (args.get("id") or "").strip()
    if not _AGENT_USER_RE.match(user):
        raise ValueError("invalid agent os_user %r (must match agent-<slug>)" % user)
    if action not in ("list", "forget", "forget_all"):
        raise ValueError("action must be list, forget or forget_all")
    if action == "forget" and not _MEMORY_ID_RE.match(entry):
        raise ValueError("forget needs the id of one memory entry")
    db = AGENT_HOME_BASE + "/" + user + "/" + _FLINT_MEMORY_DB
    proc = subprocess.run(
        ["sudo", "-n", "-u", user, "python3", "-I", "-c", _AGENT_MEMORY_PY, action, db, entry or "-"],
        capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError("agent_memory failed: " + (proc.stderr or proc.stdout)[-1000:])
    return json.loads(proc.stdout.strip())


REGISTRY = {
    "node_status": Op("node_status",
        "Read-only snapshot of the node (host, uptime, load, root disk).",
        _node_status, dangerous=False,
        permission="admin.node_status"),
    "run_command": Op("run_command",
        "Run a shell command on the node as root (broad sysadmin). Always confirmed.",
        _run_command, dangerous=lambda a: not is_safe_command(a.get('command', '')),
        input_schema={"type": "object", "properties": {
            "command": {"type": "string", "description": "Shell command to run as root."},
            "timeout": {"type": "integer", "description": "Seconds before kill (default 120)."}},
            "required": ["command"]},
        permission="admin.run_command"),
    "service_control": Op("service_control",
        "systemctl status/start/stop/restart/reload a unit. Non-status is confirmed.",
        _service_control, dangerous=lambda a: a.get("action") != "status",
        input_schema={"type": "object", "properties": {
            "unit": {"type": "string", "description": "systemd unit name."},
            "action": {"type": "string", "enum": ["status", "start", "stop", "restart", "reload"]}},
            "required": ["unit", "action"]},
        permission="admin.service_control"),
    "materialize_crons": Op("materialize_crons",
        "Install a validated cron.d body to /etc/cron.d/smart-node-agents "
        "(fail-closed lint, backs up prior + keeps last-good, atomic).",
        _materialize_crons, dangerous=False,
        input_schema={"type": "object", "properties": {
            "body": {"type": "string", "description": "Rendered cron.d body from core-api render_crontab."}},
            "required": ["body"]},
        permission="admin.materialize_crons"),
    "render_agent_runtime_env": Op("render_agent_runtime_env",
        "Install a validated KEY=VALUE body to /etc/smart-node/agent-runtime.env "
        "(root:smart-node 0640, atomic) and restart the gateway so it picks up the "
        "new credential. Body is rendered by core-api from gateway_secrets (DB SoT).",
        _render_agent_runtime_env, dangerous=False,
        input_schema={"type": "object", "properties": {
            "body": {"type": "string", "description": "Rendered agent-runtime.env body (KEY=VALUE lines)."}},
            "required": ["body"]},
        permission="admin.render_agent_runtime_env"),
    "rollback_crons": Op("rollback_crons",
        "Restore /etc/cron.d/smart-node-agents from last-good (default) or a named timestamped backup.",
        _rollback_crons, dangerous=False,
        input_schema={"type": "object", "properties": {
            "backup": {"type": "string", "description": "'last-good' (default) or smart-node-agents.<ts>.bak"}}},
        permission="admin.rollback_crons"),
    "ensure_agent_users": Op("ensure_agent_users",
        "Ensure each agent's os_user exists on the host (system user + HOME + "
        "smart-node group) and owns its /srv/agents/<slug>/{logs,state}. Narrow: "
        "os_user must match agent-<slug>. Idempotent; callable by the reconcile timer.",
        _ensure_agent_users, dangerous=False,
        input_schema={"type": "object", "properties": {
            "agents": {"type": "array", "description":
                "Agent roster from sn_config.agents (os_user + slug).",
                "items": {"type": "object", "properties": {
                    "os_user": {"type": "string", "description": "agent-<slug>"},
                    "slug": {"type": "string"}},
                    "required": ["os_user", "slug"]}}},
            "required": ["agents"]},
        permission="admin.ensure_agent_users"),
    "ensure_project": Op("ensure_project",
        "Ensure a project's unix group and /srv/projects/<slug> exist (2770 "
        "setgid, group-owned) and that its members are in the group. Flat "
        "layout per Narrow: slug and group are validated, members must "
        "be existing OS users. Idempotent; never removes a member.",
        _ensure_project, dangerous=False,
        input_schema={"type": "object", "properties": {
            "slug": {"type": "string", "description": "Project slug (a-z, digits, hyphen)."},
            "unix_group": {"type": "string", "description": "Group name; defaults to project-<slug>."},
            "members": {"type": "array", "description":
                "OS users to add to the group (agent-<slug> or a human account).",
                "items": {"type": "string"}}},
            "required": ["slug"]},
        permission="admin.ensure_project"),
    "claude_login_start": Op("claude_login_start",
        "Begin claude CLI login: returns an OAuth URL for the owner to authorize.",
        _claude_login_start, dangerous=False,
        permission="admin.claude_login_start"),
    "claude_login_complete": Op("claude_login_complete",
        "Finish claude CLI login by submitting the code the owner pasted from the URL.",
        _claude_login_complete, dangerous=False,
        input_schema={"type": "object", "properties": {
            "code": {"type": "string", "description": "Authorization code from the OAuth page."}},
            "required": ["code"]},
        permission="admin.claude_login_complete"),
    "lxc_node_provision": Op("lxc_node_provision",
        "Create a test LXC container from the standard base image and run the Smart "
        "Node installer at the specified version. Credential-isolated by construction: "
        "a seed manifest containing a live node credential is refused before lxc "
        "launch. Disconnected mode (no seed_manifest) is the safe default. Idempotent: "
        "if the container already exists, reports its state and returns. Records the "
        "container name in the test-node registry on success. Requires host-level "
        "prerequisite: LXD socket bind-mounted into admin-runner + lxd group membership "
        "(one-time operator action).",
        _lxc_node_provision, dangerous=False,
        input_schema={"type": "object", "properties": {
            "name": {"type": "string",
                     "description": "Container name (lowercase, a-z/digits/hyphens, max 41 chars)."},
            "installer_version": {"type": "string",
                     "description": "Smart Node version string (e.g. 1.12.21)."},
            "seed_manifest": {"type": "object", "nullable": True,
                     "description":
                         "Credential manifest for the test container "
                         "(KEY: VALUE dict). Omit or set null for disconnected mode. "
                         "Any value that matches a live node secret is refused."}},
            "required": ["name", "installer_version"]},
        permission="admin.lxc_node_provision"),
    "lxc_node_destroy": Op("lxc_node_destroy",
        "Stop and delete a test LXC container and remove it from the test-node "
        "registry. DESTRUCTIVE -- always confirmed. Two hard guards run before any "
        "lxc command: (1) refuses if name matches the live node hostname, "
        "(2) refuses if name is not in the test-node registry.",
        _lxc_node_destroy, dangerous=True,
        input_schema={"type": "object", "properties": {
            "name": {"type": "string",
                     "description": "Container name (must be in the test-node registry)."}},
            "required": ["name"]},
        permission="admin.lxc_node_destroy"),
    "lxc_node_status": Op("lxc_node_status",
        "Read-only: lxc info state for a test container plus its /health endpoint "
        "response (if the container is running). No confirmation required.",
        _lxc_node_status, dangerous=False,
        input_schema={"type": "object", "properties": {
            "name": {"type": "string", "description": "Container name."}},
            "required": ["name"]},
        permission="admin.lxc_node_status"),
    "run_health_probe": Op("run_health_probe",
        "Run the agent health probe immediately, outside the 15-minute cron schedule. "
        "Upserts results into sn_core.agent_health_checks (idempotent). Read-only "
        "with respect to agent state: no messages sent, no sessions started.",
        _run_health_probe, dangerous=False,
        permission="admin.run_health_probe"),
    "agent_memory": Op("agent_memory",
        "List or forget what a Flint agent remembers about people (its facts and "
        "traits), run as the agent's own user. Narrow: os_user must match agent-<slug>.",
        _agent_memory, dangerous=False,
        input_schema={"type": "object", "properties": {
            "os_user": {"type": "string", "description": "agent-<slug>"},
            "action": {"type": "string", "enum": ["list", "forget", "forget_all"]},
            "id": {"type": "string", "description": "The entry to forget (action forget)."}},
            "required": ["os_user"]},
        permission="admin.agent_memory"),
}


def validate_registry() -> None:
    """Fail-loud at import or process start: every REGISTRY row MUST declare a
    permission string, and the string MUST match the catalogue naming
    `admin.<registry_key>` so a row cannot be added without a seed row.

    Called from app.py's lifespan. Keeping it a function (not a module side-
    effect) lets tests exercise both the happy and failure path.
    """
    missing = [k for k, op in REGISTRY.items() if not op.permission]
    if missing:
        raise RuntimeError(
            "admin-runner REGISTRY rows missing .permission: " + ", ".join(sorted(missing))
        )
    mismatched = [
        (k, op.permission)
        for k, op in REGISTRY.items()
        if op.permission != "admin." + k
    ]
    if mismatched:
        raise RuntimeError(
            "admin-runner REGISTRY permission names must be 'admin.<key>'; drift: "
            + ", ".join(f"{k}={p}" for k, p in mismatched)
        )
