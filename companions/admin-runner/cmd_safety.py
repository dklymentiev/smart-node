"""Best-effort classifier: is a shell command read-only (safe to run without confirm)?

CONSERVATIVE by construction -- anything not clearly read-only returns False, so the
run_command confirm-gate still applies. This only RELAXES confirmation for obvious
reads (ps, df, journalctl, systemctl status, ...); it never grants new capability, and
the owner-gate + audit apply to every call regardless. When in doubt, return False.
"""
import re
import shlex

# Pure read-only binaries (no state-changing mode).
_SAFE = {
    "ps", "top", "uptime", "free", "df", "du", "ls", "cat", "head", "tail",
    "grep", "egrep", "fgrep", "wc", "sort", "uniq", "cut", "tr", "awk",
    "ss", "netstat", "lsof", "who", "w", "id", "whoami", "tty", "uname",
    "hostname", "hostnamectl", "date", "dmesg", "getent", "env", "printenv",
    "stat", "file", "vmstat", "iostat", "mpstat", "pgrep", "lscpu", "lsmem",
    "lsblk", "nproc", "echo", "true", "basename", "dirname", "readlink", "realpath",
}
_SAFE_SYSTEMCTL = {"status", "show", "list-units", "list-unit-files", "list-timers",
                   "list-sockets", "is-active", "is-enabled", "is-failed", "cat",
                   "get-default", "show-environment"}
_SAFE_DOCKER = {"ps", "logs", "inspect", "images", "stats", "top", "version",
                "info", "port", "history"}
# Destructive/irreversible binaries -- hard deny anywhere in the command.
_DENY = re.compile(r"(^|[\s/])(rm|rmdir|mv|dd|mkfs\S*|shred|truncate|tee|chmod|chown|"
                   r"chgrp|kill|pkill|killall|reboot|shutdown|halt|poweroff|wipefs|"
                   r"fdisk|parted|sed|apt|apt-get|yum|dnf|pip|npm)\b")
# Shell features that could smuggle a write.
_SHELL = re.compile(r"[<>]|\$\(|`")


def _seg_ok(toks):
    if not toks:
        return False
    b = toks[0].rsplit("/", 1)[-1]
    rest = toks[1:]
    if b == "systemctl":
        sub = next((t for t in rest if not t.startswith("-")), None)
        return sub in _SAFE_SYSTEMCTL
    if b == "docker":
        sub = next((t for t in rest if not t.startswith("-")), None)
        return sub in _SAFE_DOCKER
    if b in ("dpkg", "dpkg-query"):
        return any(t in ("-l", "-L", "-s", "--list", "--status") for t in rest)
    if b == "ip":
        return (any(t in ("show", "list", "s", "l") for t in rest)
                and not any(t in ("add", "del", "set", "change", "flush", "replace") for t in rest))
    if b == "journalctl":
        return not any(t.startswith(("--vacuum", "--rotate", "--flush", "--relinquish")) for t in rest)
    if b == "find":
        return not any(t in ("-delete", "-exec", "-execdir", "-fprint", "-fprintf", "-ok") for t in rest)
    return b in _SAFE


def is_safe_command(cmd):
    cmd = (cmd or "").strip()
    if not cmd or _SHELL.search(cmd) or _DENY.search(cmd):
        return False
    for seg in re.split(r"\|\||&&|;|\||&", cmd):
        seg = seg.strip()
        if not seg:
            continue
        try:
            toks = shlex.split(seg)
        except ValueError:
            return False
        if not _seg_ok(toks):
            return False
    return True
