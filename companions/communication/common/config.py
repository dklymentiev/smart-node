"""Configuration for the communication component -- environment-driven.

Every setting comes from an environment variable. The `communication`
companion installer supplies them via a systemd EnvironmentFile;
for a standalone run, a sibling `.env` file (KEY=VALUE lines) is loaded
into the environment if present -- it never overrides an already-set var.

Adapters resolve their own channel settings via `require()` / `get()`
so importing this module never forces unrelated credentials to be set.
"""
import datetime
import os

_COMPONENT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_env_file(path):
    """Populate os.environ from a KEY=VALUE file. Existing vars win."""
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())
    except FileNotFoundError:
        pass


# Standalone convenience: a sibling .env, if any. Installed runs use the
# companion's systemd EnvironmentFile and simply have no .env here.
load_env_file(os.path.join(_COMPONENT_ROOT, ".env"))


def get(key, default=None):
    """Optional setting."""
    return os.environ.get(key, default)


def require(key):
    """Mandatory setting -- raise a clear error if absent."""
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(
            "communication: required setting %s is not set "
            "(supply it via the companion EnvironmentFile or a .env)" % key)
    return val


# Shared settings -- safe for any adapter to import.
# The messages registry lives in the node's B02 core DB; peer auth means
# no password in the DSN (the process runs as a mapped role).
DB_DSN = os.environ.get("SMARTNODE_DB_DSN", "dbname=smartnode")

# Per-adapter runtime state (e.g. the telegram poll offset).
STATE_DIR = os.environ.get(
    "COMMUNICATION_STATE_DIR", os.path.join(_COMPONENT_ROOT, "state"))


def LOG(adapter, *args):
    """Timestamped stdout line, tagged with the adapter name."""
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("[%s] [%s]" % (ts, adapter), *args, flush=True)
