"""Publishing a flow the node made itself.

A node's own flow is written by its author where the author may write: the
agent's own directory on the node. It is not run from there. The import door
checks the author's directory with the programs the node ships, and on green
copies it into the published tree and registers the copy:

    <authored>/<name>/        what the Runner runs, owned by this service,
                              in the node's state and not under /opt

So what runs is what was checked, the author cannot change a registered flow
without coming through the door again, and an update of the node never touches
this tree: the installer creates it and writes nothing inside.

What the door runs is OURS: flow-lint.py, which reads the flow and parses its
scripts without executing them. It never runs the author's own tests, and never
imports the flow's scripts. Those tests are shell and Python the author wrote;
this service holds the database login, and running them here would hand that to
whoever may write a flow. The author runs them as himself before he calls.
"""
import os
import re
import shutil
import stat
import subprocess
import tempfile

import yaml

SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
MAX_FILES = 200
MAX_BYTES = 2_000_000
LINT_SECONDS = 60


class Refused(Exception):
    """The flow is not published. `step` says which check refused, `findings`
    what to fix, one line each."""

    def __init__(self, step, findings):
        super().__init__("%s: %s" % (step, "; ".join(findings)))
        self.step = step
        self.findings = list(findings)


def _inside(path, roots):
    real = os.path.realpath(path)
    return any(real == r or real.startswith(r.rstrip("/") + "/")
               for r in (os.path.realpath(x) for x in roots if x))


def _files(source):
    """Every file of the author's directory, relative; refuses what a flow has
    no reason to hold."""
    out, total = [], 0
    for base, dirs, files in os.walk(source):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for d in dirs:
            if os.path.islink(os.path.join(base, d)):
                raise Refused("source", ["%s is a link; a flow holds its own files"
                                         % os.path.relpath(os.path.join(base, d), source)])
        for f in sorted(files):
            p = os.path.join(base, f)
            rel = os.path.relpath(p, source)
            if os.path.islink(p) or not stat.S_ISREG(os.lstat(p).st_mode):
                raise Refused("source", ["%s is not a plain file; a flow holds its own files" % rel])
            total += os.path.getsize(p)
            out.append(rel)
    if len(out) > MAX_FILES or total > MAX_BYTES:
        raise Refused("source", ["the directory holds %d files, %d bytes; a flow is at most %d files, %d bytes"
                                 % (len(out), total, MAX_FILES, MAX_BYTES)])
    return out


def check(name, source, cfg):
    """Check the author's directory and stage a copy of it. Returns the staging
    root, an agents tree with the copy at flows/<name>. Raises Refused.

    cfg: shipped, authored (flow roots), source_roots (where an author may
    write), tools (the shipped flow tools).
    """
    if not SAFE_NAME.match(name or ""):
        raise Refused("name", ["a flow name is lowercase letters, digits and dashes, starting with a letter or digit"])
    if not source or not os.path.isabs(source) or not os.path.isdir(source):
        raise Refused("source", ["source must be the absolute path of the directory you wrote the flow in"])
    if not _inside(source, cfg["source_roots"]):
        raise Refused("source", ["%s is not under %s" % (source, " or ".join(cfg["source_roots"]))])
    if os.path.exists(os.path.join(cfg["shipped"], name)):
        raise Refused("name", ["'%s' is the name of a flow this node ships; give your flow another name" % name])
    flow_file = os.path.join(source, name + ".yaml")
    if not os.path.isfile(flow_file):
        raise Refused("source", ["no %s.yaml in %s; the file is named after the flow" % (name, source)])

    files = _files(source)

    # The contract a flow carries, the same one flow-test.sh holds a flow to.
    try:
        doc = yaml.safe_load(open(flow_file)) or {}
    except Exception as exc:
        raise Refused("contract", ["%s.yaml does not parse: %s" % (name, exc)])
    if not isinstance(doc, dict):
        raise Refused("contract", ["%s.yaml is not a mapping" % name])
    missing = []
    if doc.get("name") != name:
        missing.append("name: in the file is %r, the flow is %r" % (doc.get("name"), name))
    meta = doc.get("metadata") or {}
    if meta.get("sample") is True:
        missing.append("metadata.sample is true: a sample is a flow to copy, not to register; "
                       "write your agents' names in and remove the line")
    if not meta.get("version"):
        missing.append("metadata.version is not set")
    for f in ("README.md", "CHANGELOG.md"):
        p = os.path.join(source, f)
        if not (os.path.isfile(p) and os.path.getsize(p)):
            missing.append("%s is missing or empty" % f)
    if not any(r.startswith("tests" + os.sep) and r.endswith(".test.sh") for r in files):
        missing.append("tests/ holds no *.test.sh: a flow carries its own tests")
    if missing:
        raise Refused("contract", missing)

    os.makedirs(cfg["authored"], exist_ok=True)
    staging = tempfile.mkdtemp(prefix=".staging-", dir=cfg["authored"])
    try:
        dest = os.path.join(staging, "flows", name)
        for rel in files:
            target = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copyfile(os.path.join(source, rel), target)
            # What the Runner needs and no more. A script keeps its execute
            # bit, and the flow's programs get one whatever the author's tool
            # left on them: a file written through the file tool has none.
            program = ((rel.startswith("logic" + os.sep) and rel.endswith(".py"))
                       or (rel.startswith("tests" + os.sep) and rel.endswith(".sh")))
            mode = 0o755 if program or os.stat(os.path.join(source, rel)).st_mode & 0o111 else 0o644
            os.chmod(target, mode)
        for base, dirs, _ in os.walk(staging):
            for d in dirs:
                os.chmod(os.path.join(base, d), 0o755)
        os.chmod(staging, 0o755)

        lint = os.path.join(cfg["tools"], "flow-lint.py")
        try:
            run = subprocess.run([lint, "--root", staging, "--flow", name],
                                 capture_output=True, text=True, timeout=LINT_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Refused("lint", ["flow-lint.py could not be run: %s" % exc])
        if run.returncode != 0:
            lines = [l.rstrip() for l in (run.stdout + run.stderr).splitlines() if l.strip()]
            raise Refused("lint", lines[:60] or ["flow-lint.py exited %d" % run.returncode])
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return staging


def publish(name, staging, cfg):
    """Put the staged copy in place of the published flow. Returns the path of
    the published flow file. The swap is two renames on one filesystem: a run
    that reads the flow sees the old one or the new one, never half of each."""
    target = os.path.join(cfg["authored"], name)
    old = None
    try:
        if os.path.exists(target):
            old = tempfile.mkdtemp(prefix=".old-", dir=cfg["authored"])
            os.rename(target, os.path.join(old, name))
        os.rename(os.path.join(staging, "flows", name), target)
    except OSError:
        if old and not os.path.exists(target):
            os.rename(os.path.join(old, name), target)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        if old:
            shutil.rmtree(old, ignore_errors=True)
    return os.path.join(target, name + ".yaml")
