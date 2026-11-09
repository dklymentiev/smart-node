"""
AI Gateway Configuration

All settings configurable via environment variables.
See .env.example for documentation.
"""
import os
import logging
import signal
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger(__name__)

# CLI Provider
CLI_PATH = Path(os.getenv("CLI_PATH", "claude"))
FLINT_PATH = Path(os.getenv("FLINT_PATH", "/usr/local/bin/flint"))
# Flint is opt-in until its stdio security checks and node-level sandbox are
# verified. The installer never switches an existing agent automatically.
FLINT_ENABLED = os.getenv("FLINT_ENABLED", "false").lower() == "true"
FLINT_SKIP_PERMISSIONS = os.getenv("FLINT_SKIP_PERMISSIONS", "false").lower() == "true"
FLINT_SKIP_PERMISSIONS_USERS = frozenset(
    user.strip() for user in os.getenv("FLINT_SKIP_PERMISSIONS_USERS", "").split(",")
    if user.strip()
)


@lru_cache(maxsize=8)
def _flint_stdio_probe(binary: str, mtime_ns: int, size: int) -> bool:
    with tempfile.TemporaryDirectory(prefix="flint-stdio-probe-") as home:
        try:
            # --stdio prevents an old build from starting its interactive
            # launcher. A separate process group lets us reap that launcher
            # too if an even older binary ignores both flags.
            process = subprocess.Popen(
                [binary, "--stdio", "--version"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, cwd=home,
                start_new_session=True,
                env={"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                     "HOME": home, "FLINT_DATA_DIR": home, "MCP_SERVERS": ""},
            )
            try:
                stdout, _ = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
                return False
        except OSError:
            return False
    name, _, version = stdout.strip().partition(" ")
    try:
        found = tuple(int(part) for part in version.split("."))
    except ValueError:
        return False
    return process.returncode == 0 and name == "flint" and found >= FLINT_MIN_VERSION


# 1.16.0 is the first release that keeps host-provided keys ephemeral, reads
# FLINT.md and narrows its tools by --tools; it no longer answers the older
# --sn-stdio-ephemeral-key-check, so the version is the check.
FLINT_MIN_VERSION = (1, 16, 0)


def flint_stdio_safe() -> bool:
    """Require a Flint release from FLINT_MIN_VERSION on."""
    if not FLINT_PATH.is_absolute():
        return False
    try:
        stat = FLINT_PATH.stat()
    except OSError:
        return False
    return _flint_stdio_probe(str(FLINT_PATH), stat.st_mtime_ns, stat.st_size)
CLI_SESSION_TIMEOUT = int(os.getenv("CLI_SESSION_TIMEOUT", "3600"))
CLI_MAX_SESSIONS = int(os.getenv("CLI_MAX_SESSIONS", "10"))
CLI_QUEUE_TIMEOUT = int(os.getenv("CLI_QUEUE_TIMEOUT", "300"))  # max wait for slot (seconds)
# Per-request context budget for handoff/reset.  This is deliberately based on
# the latest provider response, not the session's accumulated usage.
CONTEXT_HANDOFF_THRESHOLD = int(os.getenv("CONTEXT_HANDOFF_THRESHOLD", "120000"))
MESH_API_URL = os.getenv("MESH_API_URL", "http://127.0.0.1:8000").rstrip("/")
# Empty on this node's own memory, which runs with AUTH_REQUIRED=false and
# selects the workspace from the X-Workspace header. Set it (and nothing else
# changes) if memory is ever put behind keys: /etc/smart-node/mesh.env is the
# file the unit reads, and memory.sh writes it.
MESH_API_KEY = os.getenv("MESH_API_KEY", "")
MESH_HANDOFF_TIMEOUT = float(os.getenv("MESH_HANDOFF_TIMEOUT", "15"))
# Reading one document back is on the path of every agent session creation, so
# it gets a short deadline of its own: slow memory must not hold up a new
# session, it can only cost that session its injected handoff.
MESH_HANDOFF_READ_TIMEOUT = float(os.getenv("MESH_HANDOFF_READ_TIMEOUT", "5"))
# After a failed attempt: wait this long, or until the session has grown by this
# factor, before paying for another full-context generation.
CONTEXT_HANDOFF_RETRY_COOLDOWN = float(os.getenv("CONTEXT_HANDOFF_RETRY_COOLDOWN", "600"))
CONTEXT_HANDOFF_RETRY_GROWTH = float(os.getenv("CONTEXT_HANDOFF_RETRY_GROWTH", "1.15"))
SKIP_PERMISSIONS = os.getenv("SKIP_PERMISSIONS", "false").lower() == "true"

# The gateway relays every model call of an agent's Claude Code process and
# hands the process a token of its own instead of the node credential
# (anthropic_relay.py). Off, the process carries the credential itself and
# calls the provider directly, as before.
MODEL_RELAY = os.getenv("MODEL_RELAY", "true").lower() == "true"
CLI_DEFAULT_CWD = os.getenv("CLI_DEFAULT_CWD", "/opt/smart-node/scratch")

# ---------------------------------------------------------------------------
# What a spawned CLI inherits.
#
# One list, in one place, on purpose. This existed as two copies -- one in
# backends/cli_claude.py and one in session_manager.py -- and they drifted: the
# OAuth token was added to the first and not the second, so every session that
# took the older path spawned a CLI that was simply not logged in, on a node
# whose credential was present and correct. An API-key node was unaffected,
# which is why it survived so long.
SPAWN_ENV_KEYS = (
    "PATH", "HOME", "USER", "LANG", "TERM", "SHELL",
    "ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_BEDROCK",
    # OAuth token from `claude setup-token` (sk-ant-oat...). The node's own
    # login mints this, and the CLI accepts it ONLY under this name -- passed
    # as ANTHROPIC_API_KEY it is rejected.
    "CLAUDE_CODE_OAUTH_TOKEN",
)

# ---------------------------------------------------------------------------
# How big ONE line of a spawned CLI's output may be.
#
# Here for the same reason SPAWN_ENV_KEYS is here, and it is not a coincidence
# that it is the same two files. This started in session_manager.py alone, and
# backends/cli_claude.py -- the path the Runner drives for every agent wake --
# was left on asyncio's default. That default is 64 KiB, and an agent's
# assembled system prompt is 27-35 KiB today (measured on both live nodes), so
# the CLI's very first JSON line, which echoes that prompt escaped, was already
# close to it, and over it on one node. The wake died in ~80ms, the Runner
# marked the agent broken, and the node stopped doing autonomous work while
# every service still read healthy. One definition, both paths, no drift.
#
# The CLI speaks a JSON object per line, and a turn that quotes a document back
# is legitimately a single very long line. Past the buffer readline() raises
# ValueError("Separator is found, but chunk is longer than limit") -- asyncio
# catches LimitOverrunError internally and re-raises as ValueError, so THAT is
# what has to be caught downstream, not the name the documentation suggests.
#
# Measured: a 15 KB document does not trip it on its own; the turn that quotes
# it back, escaped, does. Reading a document then became "the session crashed",
# which is how this reached us from a node rather than from a test.
#
# 64 MiB is not a target, it is a ceiling far above any real turn. The ceiling
# still exists on purpose: an unbounded reader turns a runaway child into the
# gateway's own memory problem.
STREAM_LIMIT = 64 * 1024 * 1024

# Where the node projects its own model credential. The gateway unit also names
# this file in EnvironmentFile=, but systemd reads that once, at process start:
# a credential stored afterwards (the panel Connect flow) never reaches a
# running gateway, while an agent picks it up on its next wake because its
# run.sh sources the same file every time. Reading it at spawn removes that
# asymmetry, and with it a node that looks healthy while nothing can think.
AGENT_RUNTIME_ENV = os.getenv("SMART_NODE_AGENT_ENV",
                              "/etc/smart-node/agent-runtime.env")

_CREDENTIAL_KEYS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")


def credential_from_file() -> dict:
    """The projected credential, so a spawn does not depend on start order.

    Deliberately narrow: only the credential keys, only simple KEY=value lines.
    Not a general env loader and must not become one -- everything else in that
    file belongs to the agent runtime, not to a CLI the gateway spawns. A
    missing or unreadable file is not an error: a node may hold its credential
    only in the environment, and a node holding it nowhere is already reported
    by validate.sh in its own words.
    """
    found = {}
    try:
        with open(AGENT_RUNTIME_ENV) as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("export "):
                    line = line[7:].lstrip()
                key, sep, value = line.partition("=")
                if not sep or key not in _CREDENTIAL_KEYS:
                    continue
                value = value.strip().strip("'\"")
                if value:
                    found[key] = value
    except OSError:
        pass
    return found


def spawn_env() -> dict:
    """The environment a spawned CLI gets: allowlist, then the projected file.

    The process environment wins where it has a value -- an operator who
    exported a token for this gateway meant it. The file only fills gaps.
    """
    env = {k: v for k, v in os.environ.items() if k in SPAWN_ENV_KEYS}
    for key, value in credential_from_file().items():
        env.setdefault(key, value)
    return env


# Run each agent's CLI as that agent's own OS user instead of as the gateway
# service account. Off means every agent shares one uid on this path,
# so per-agent group membership -- which is how a project work area grants
# access -- means nothing here. Left switchable because turning it on requires
# the sudoers rule installed by gateway.sh; a node that somehow has one without
# the other should be able to keep serving chat while that is repaired.
CLI_RUNAS_ENABLED = os.getenv("CLI_RUNAS_ENABLED", "true").lower() == "true"
# Absolute paths: a sudoers rule matches the command as written, and CLI_PATH
# defaults to the bare name `claude` resolved through PATH, which no rule can
# match. gateway.sh resolves and writes the real one.
SUDO_PATH = os.getenv("SUDO_PATH", "/usr/bin/sudo")
KILL_PATH = os.getenv("KILL_PATH", "/usr/bin/kill")
# Credential wrapper. When the gateway drops to an agent's OS user via
# sudo, credentials are excluded from the preserved environment so sudo's
# audit logger never sees a token value. The wrapper reads them from
# AGENT_RUNTIME_ENV directly and execs the real CLI binary. gateway.sh
# installs the wrapper and writes its path into the unit as CLI_AGENT_WRAPPER_PATH.
WRAPPER_PATH = Path(os.getenv("CLI_AGENT_WRAPPER_PATH",
                               "/opt/smart-node/gateway/claude-agent-wrapper"))

# Anthropic
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY") or None
ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1"
ANTHROPIC_DEFAULT_MODEL = "claude-3-haiku-20240307"

# Gemini
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or None
GEMINI_DEFAULT_MODEL = "gemini-pro"

# OpenRouter
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY") or None
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_DEFAULT_MODEL = "anthropic/claude-sonnet-4.5"

# Model whitelist (allowed models per provider)
ALLOWED_MODELS = {
    "cli": {"haiku", "sonnet", "opus"},
    "anthropic": {
        "claude-3-haiku-20240307", "claude-3-5-haiku-20241022",
        "claude-3-5-sonnet-20241022", "claude-3-5-sonnet-20240620",
        "claude-sonnet-4-20250514", "claude-opus-4-20250514",
    },
    "gemini": {"gemini-pro", "gemini-1.5-pro", "gemini-1.5-flash", "gemini-2.0-flash",
               "gemini-flash-latest"},
    "openrouter": {
        "anthropic/claude-3.5-haiku", "anthropic/claude-haiku-4.5",
        "anthropic/claude-sonnet-4.5", "anthropic/claude-sonnet-4.6",
        "anthropic/claude-opus-4.5", "google/gemini-2.5-flash",
        "google/gemini-2.5-pro", "openai/gpt-4o-mini",
        "meta-llama/llama-3.1-8b-instruct",
    },
}

# Server
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))
# The gateway is two processes of one companion. The door (main.py) listens
# on HOST:PORT and is the only address callers and agent processes know. The
# runtime (runtime_main.py) owns the agent CLI processes and listens on
# loopback for the door alone; the door forwards /api/v1/sessions/* and CLI
# chats to RUNTIME_URL. DOOR_URL is what an agent process is told to call.
RUNTIME_HOST = os.getenv("RUNTIME_HOST", "127.0.0.1")
RUNTIME_PORT = int(os.getenv("RUNTIME_PORT", "8002"))
RUNTIME_URL = os.getenv("RUNTIME_URL", f"http://{RUNTIME_HOST}:{RUNTIME_PORT}").rstrip("/")
DOOR_URL = os.getenv("GATEWAY_URL", f"http://{HOST}:{PORT}").rstrip("/")
CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",")]

# Security
GATEWAY_API_KEY = os.getenv("GATEWAY_API_KEY") or None
ALLOWED_IPS = [ip.strip() for ip in os.getenv("ALLOWED_IPS", "127.0.0.1").split(",")]
TRUSTED_PROXIES = [p.strip() for p in os.getenv("TRUSTED_PROXIES", "127.0.0.1").split(",")]

# Logging
LOG_CONTENT = os.getenv("LOG_CONTENT", "false").lower() == "true"
LOGS_DIR = Path(os.getenv("LOGS_DIR", "./logs"))

# Features
ENABLE_DOCS = os.getenv("ENABLE_DOCS", "false").lower() == "true"
ENABLE_USAGE_ENDPOINT = os.getenv("ENABLE_USAGE_ENDPOINT", "false").lower() == "true"
