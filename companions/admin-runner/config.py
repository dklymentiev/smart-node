"""Runner configuration -- entirely environment-driven, fail-loud.

House rules: no hardcoded host-specifics, no silent fallbacks for required values.
Anything node-specific (owner role names, db role, privileged identity) is supplied
by the deployment, never baked into the code -- this service is open-source and must
run on any node.
"""
import os


def _req(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise RuntimeError(f"admin-runner: required env {name!r} is not set")
    return v


# OWNER_ROLES is retained read-only for backward-compatible env parsing on
# existing installs. It is no longer consulted: the role matrix in
# `sn_config.role_permissions` decides authorization. Removing the env var
# requirement here is deliberate, the service must boot on a node where
# SN_ADMIN_OWNER_ROLES was never set.
OWNER_ROLES = {
    r.strip()
    for r in os.environ.get("SN_ADMIN_OWNER_ROLES", "").split(",")
    if r.strip()
}

# Postgres role the runner connects as (peer auth via pg_ident). Must be explicit.
DB_USER = _req("SN_ADMIN_DB_USER")
DB_NAME = os.environ.get("SN_ADMIN_DB_NAME", "smartnode")

# Extra trusted source CIDRs beyond loopback + docker0 (comma-separated). Optional.
TRUSTED_CIDRS = os.environ.get("SN_TRUSTED_CIDRS", "")

# Operational knobs (documented defaults, not security-bearing).
BIND_HOST = os.environ.get("SN_ADMIN_BIND_HOST", "127.0.0.1")
BIND_PORT = int(os.environ.get("SN_ADMIN_BIND_PORT", "8041"))
CONFIRM_TTL_SECS = int(os.environ.get("SN_ADMIN_CONFIRM_TTL", "300"))

# Canonical HOME where `claude setup-token` writes credentials. The credential
# provider then distributes ~/.claude/.credentials.json from here to agent homes.
CLAUDE_HOME = os.environ.get("SN_ADMIN_CLAUDE_HOME", "/etc/smart-node/claude")

# LXD test-node lifecycle.
# Base LXD image used for every test container.
LXC_BASE_IMAGE = os.environ.get("SN_ADMIN_LXC_BASE_IMAGE", "ubuntu:24.04")
# LXD network (bridge) to attach the test container eth0 to.
LXC_NETWORK = os.environ.get("SN_ADMIN_LXC_NETWORK", "lxdbr0")
# Base URL from which the Smart Node installer is fetched.
# The template appends /install-{VERSION}.sh.  Empty string means the
# installer step is skipped (container is launched but left bare).
LXC_INSTALLER_URL_BASE = os.environ.get("SN_ADMIN_INSTALLER_URL_BASE", "")
# Flat file: one container name per line.  node-provision appends on success;
# node-destroy removes on success; node-destroy refuses if the name is absent.
TEST_NODE_REGISTRY = os.environ.get(
    "SN_ADMIN_TEST_NODE_REGISTRY", "/var/lib/admin-runner/test-nodes.txt"
)
