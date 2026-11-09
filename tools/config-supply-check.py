#!/usr/bin/env python3
"""config-supply-check.py -- refuse a tree that reads a required address from an
environment variable nothing supplies.

WHY THIS EXISTS. Twice in one night a node was broken by the same shape:

    companions/flows/run-workflow.py:35   CORE = os.environ.get("SN_CORE_EVENTS", "")
    tools/update-on.sh:429                CORE_API="${SN_CORE_API:-}"

Neither name is set anywhere in the tree, and neither read is guarded, so the
value was always the empty string. One killed every workflow launch with
`ValueError: unknown url type: ''`; the other made every update end in
`HTTP 000` and the word FAILED even when the install and the validation had both
passed. Both came out of the same change, the one that took deployment
addresses out of the source, and both were found by running a server for an hour
rather than by reading, though either was a four-second grep.

An empty default is what makes this invisible. It converts a missing
configuration into a runtime error far from its cause, and it defeats every gate
we have, because nothing about the source looks wrong.

WHAT COUNTS AS A FINDING. All three at once:

  1. the name is read with an EMPTY default -- os.environ.get("X", ""),
     os.getenv("X", ""), os.environ.get("X") or "", or "${X:-}";
  2. nothing in the tree SUPPLIES it -- no assignment, export, Environment=,
     inline `env X=...`, or python mapping entry anywhere;
  3. the read is not GUARDED -- no `[[ -n ... ]]` around it, and no refusal
     (exit / die / raise / sys.exit) within a few lines of it.

A guarded read is the correct pattern for something genuinely optional, and it
stays green here. So does a name the tree supplies. What is refused is the third
thing: required, unsupplied, unguarded.

ALLOWLIST. A name that is legitimately configured from OUTSIDE the tree, by an
operator or a CI environment, belongs in ALLOW below WITH ITS REASON. An entry
with no reason is not an entry.

KNOWN is a different thing: the debt that already existed when this check was
written. Those names are reported and do not refuse, so the gate is green from
the first day and can only be made greener. A name that is neither supplied,
guarded, allowed nor already known refuses the release.

  tools/config-supply-check.py            scan the working tree
  tools/config-supply-check.py --tree DIR scan a directory
  tools/config-supply-check.py --self-test  plant a violation, prove it is caught
  tools/config-supply-check.py --quiet    findings only

Exit 0 clean, 1 findings, 2 could not run.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

SELF = os.path.basename(__file__)
SKIP_DIRS = {".git", "venv", ".venv", "node_modules", "vendor", "__pycache__",
             ".pytest_cache", "dist", "build"}
SKIP_EXT = {".pyc", ".tar", ".gz", ".zip", ".png", ".jpg", ".jpeg", ".gif",
            ".ico", ".woff", ".woff2", ".ttf", ".pdf", ".lock"}

# TWO SETS, one rule: a confirmed configuration defect blocks a release, while
# an unevaluated or accepted debt entry remains visible without blocking it.
#
# BLOCKING = names that pass that test and hold the release. Add here only after
# confirming that the name is supplied, required, and broken.
# The gate refuses a name in BLOCKING the same way it refuses an unknown name:
# the release cannot be cut until the defect is resolved.
#
# KNOWN = names that are confirmed debt or not yet evaluated. A KNOWN name that
# is NOT in BLOCKING passes through the gate with a debt note; it does not hold
# the release. The gate refuses anything NOT on either list, so the debt can
# only shrink; adding a name to KNOWN is a deliberate act and shows up in
# review. Tracked as
#
# the three-condition test -- apply to every KNOWN entry before a tag:
#   1. The named defect renders a capability non-functional (not merely degraded).
#   2. The capability is reachable on a clean install under default configuration.
#   3. No post-install workaround is documented in the release notes.
# If all three hold, move the entry from KNOWN to BLOCKING. The release is then
# held until the defect is resolved.
KNOWN = {
    "BROADCAST_SENDER": "brin runner broadcast identity",
    "BUDGET_NOTIFY_ACTOR": "gateway budget notification actor",
    "REIN_MEM_CLI": "rein memory CLI path",
    "SMART_NODE_TOOLS_DIR": "health-checker tools directory",
    "SMART_NODE_VOICE_MESH_URL": "voice companion mesh address",
    "SN_ADMIN_INSTALLER_URL_BASE": "admin-runner LXC installer base URL",
    "SN_CORE_API": "left only in tests/ and node-versioning; update-on.sh now supplies it from the canonical config",
    "SN_DOCKER_CIDR": "docker bridge CIDR used by the auth guards",
    "SN_STUDIO_URL": "voice backend Studio address",
    "SN_SECRET_STORE": "gdrive MCP secret store address",
    "VOICE_CROSSCHANNEL_CONVERSATIONS": "voice cross-channel list; empty is plausibly meant",
}

# BLOCKING = names that have passed the three-condition test for KNOWN entries and hold
# the release. Add here only after applying all three conditions.
# KNOWN = names that are confirmed debt or not yet evaluated.
# Start empty: no KNOWN entry has been evaluated yet.
BLOCKING: set = set()

# Names configured from outside the tree. Each entry carries its reason.
ALLOW = {
    "OPENAI_BASE_URL":
        "optional endpoint for Rein's OpenAI provider, set by an operator who points it at a "
        "compatible server; empty is guarded (providers/openai.py passes base_url only when "
        "set) and means the SDK's default endpoint",
    "SN_ANTHROPIC_FALLBACK_MODEL":
        "the node's switch for the Anthropic rate-limit fallback: set it to a model and a 429 "
        "from the account is retried through the fallback provider; empty is the supported "
        "default and means no fallback, nothing fails at runtime for want of it",
    "SN_TEST_PYTHON":
        "optional override of the Python the source tests run under (tests/run-source.sh); "
        "unset, the runner probes python3 and the usual venvs and says so when none will do",
    "SN_REIN_SCHEMAS":
        "optional override for the flow linter: where the engine schemas are "
        "when the tool runs outside a checkout and outside a node; without it "
        "the tool looks beside itself and under /opt/smart-node/workflows",
    "SN_REIN_SRC":
        "same, for the flow tracer and the engine source",
    "REGISTRY_TOKEN":
        "operator credential for a private image registry; the installer asks "
        "for it and works without one",
    "GHCR_TOKEN":
        "same, for GitHub's registry",
    "RELEASE_OBJECTIVE_ID":
        "release-time override typed by the maintainer, not node configuration",
    "SMART_NODE_ANTHROPIC_API_KEY":
        "operator credential passed to the installer; a headless install "
        "deliberately defers it",
    "SN_EVENT_DIGEST_DRYRUN":
        "operator rehearsal flag for the daily event digest. An empty "
        "default is the correct value (production runs send the e-mail); "
        "operators export SN_EVENT_DIGEST_DRYRUN=1 ad-hoc to inspect the "
        "composed body without sending",
    "SN_EVENT_DIGEST_DATE":
        "operator override for the daily event digest window. Empty default "
        "means 'yesterday local'; set to YYYY-MM-DD to re-run the digest "
        "for a specific past date (ad-hoc inspection, not a service "
        "parameter)",
}

READ_PATTERNS = [
    re.compile(r'os\.environ\.get\(\s*["\']([A-Z][A-Z0-9_]*)["\']\s*,\s*["\']["\']\s*\)'),
    re.compile(r'os\.getenv\(\s*["\']([A-Z][A-Z0-9_]*)["\']\s*,\s*["\']["\']\s*\)'),
    re.compile(r'os\.environ\.get\(\s*["\']([A-Z][A-Z0-9_]*)["\']\s*\)\s*or\s*["\']["\']'),
    re.compile(r'\$\{([A-Z][A-Z0-9_]*):-\}'),
]

# Anywhere the name is given a value counts, not only an assignment at the start
# of a line: most of these are supplied by a contract file the installer WRITES,
# so the supply lives inside a heredoc or a printf format ("NAME=%s\n") rather
# than in a statement. A detector that only looks at line starts calls every one
# of those missing, which is how a check like this ends up ignored.
SUPPLY_PATTERNS = [
    re.compile(r'\b([A-Z][A-Z0-9_]*)\s*='),
    re.compile(r'os\.environ\[\s*["\']([A-Z][A-Z0-9_]*)["\']\s*\]\s*='),
    re.compile(r'["\']([A-Z][A-Z0-9_]*)["\']\s*:\s*[^,}\s]'),
]

# A refusal close to the read: the value is checked before it is used.
GUARD_NEAR = re.compile(
    r'\b(?:sys\.exit|raise\b|die\b|exit\s+[0-9]|abort\b|assert\b|'
    r'RuntimeError|ValueError|refuse\b|FAIL\b)')
GUARD_WINDOW = 8


def supplies(line, name):
    """True if LINE gives NAME a value, rather than re-reading the same hole."""
    if re.search(r'=\s*["\']?\$\{%s:-\}' % re.escape(name), line):
        return False
    if re.search(r'=\s*os\.environ\.get\(\s*["\']%s["\']\s*,\s*["\']["\']'
                 % re.escape(name), line):
        return False
    if re.search(r'=\s*os\.getenv\(\s*["\']%s["\']\s*,\s*["\']["\']'
                 % re.escape(name), line):
        return False
    for rx in SUPPLY_PATTERNS:
        if name in rx.findall(line):
            return True
    return False


def guarded(lines, idx, name):
    """True if the read at LINES[idx] is checked before its value is used."""
    line = lines[idx]
    # Shell: the read sits inside its own emptiness test.
    if re.search(r'\[\[?\s+-[nz]\s+"?\$\{%s:-\}' % re.escape(name), line):
        return True
    # Python: `or ""` immediately followed by a refusal, or the common
    # `X = os.environ.get(...)` then `if not X: sys.exit(...)`.
    m = re.match(r'\s*([A-Za-z_][A-Za-z0-9_]*)\s*=', line)
    var = m.group(1) if m else None
    for j in range(idx + 1, min(idx + 1 + GUARD_WINDOW, len(lines))):
        nxt = lines[j]
        if not GUARD_NEAR.search(nxt):
            continue
        window = "\n".join(lines[idx + 1:j + 1])
        if name in window:
            return True
        if var and re.search(r'\b%s\b' % re.escape(var), window):
            return True
    return False


def scan(root):
    reads = {}
    supplied = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            # Bundled minified JavaScript is not configuration source. A single
            # megabyte-long line also makes the regex scan needlessly slow.
            if fn.endswith((".min.js", ".min.mjs")):
                continue
            if os.path.splitext(fn)[1] in SKIP_EXT:
                continue
            # This file carries planted violations as string literals, on
            # purpose, so --self-test can prove the check catches them. Scanning
            # itself would make the gate refuse every tree that contains it.
            if fn == SELF:
                continue
            path = os.path.join(dirpath, fn)
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    lines = fh.read().splitlines()
            except OSError:
                continue
            rel = os.path.relpath(path, root)
            for i, line in enumerate(lines):
                for rx in READ_PATTERNS:
                    for name in rx.findall(line):
                        if guarded(lines, i, name):
                            continue
                        reads.setdefault(name, []).append((rel, i + 1, line.strip()))
                for rx in SUPPLY_PATTERNS:
                    for name in rx.findall(line):
                        if supplies(line, name):
                            supplied.add(name)
    return {n: v for n, v in reads.items() if n not in supplied and n not in ALLOW}


def self_test():
    """Plant one violation of each shape and prove each is caught."""
    tmp = tempfile.mkdtemp(prefix="config-supply-selftest.")
    try:
        with open(os.path.join(tmp, "planted.py"), "w") as fh:
            fh.write('CORE = os.environ.get("SN_PLANTED_PY", "")\n'
                     'urllib.request.Request(CORE)\n')
        with open(os.path.join(tmp, "planted.sh"), "w") as fh:
            fh.write('API="${SN_PLANTED_SH:-}"\n'
                     'curl "$API/health"\n')
        # And one of each shape that must NOT be caught.
        with open(os.path.join(tmp, "clean.py"), "w") as fh:
            fh.write('URL = os.environ.get("SN_CLEAN_GUARDED", "")\n'
                     'if not URL:\n'
                     '    sys.exit("SN_CLEAN_GUARDED is not set")\n')
        with open(os.path.join(tmp, "clean.sh"), "w") as fh:
            fh.write('SN_CLEAN_SUPPLIED=http://127.0.0.1:8030\n'
                     'OTHER="${SN_CLEAN_SUPPLIED:-}"\n'
                     'curl "$OTHER/health"\n')
        found = scan(tmp)
        ok = True
        for want in ("SN_PLANTED_PY", "SN_PLANTED_SH"):
            if want in found:
                print("  ok       planted %s is caught" % want)
            else:
                print("  REFUSED  planted %s was NOT caught" % want); ok = False
        for unwanted in ("SN_CLEAN_GUARDED", "SN_CLEAN_SUPPLIED"):
            if unwanted in found:
                print("  REFUSED  %s is clean but was reported" % unwanted); ok = False
            else:
                print("  ok       %s stays clean" % unwanted)
        return 0 if ok else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    args = sys.argv[1:]
    quiet = "--quiet" in args
    if "--self-test" in args:
        return self_test()
    if "--help" in args or "-h" in args:
        print(__doc__)
        return 0
    root = "."
    if "--tree" in args:
        root = args[args.index("--tree") + 1]
    if not os.path.isdir(root):
        print("[config-supply-check] not a directory: %s" % root, file=sys.stderr)
        return 2
    findings = scan(root)
    blocking = {n: v for n, v in findings.items() if n in BLOCKING}
    debt = {n: v for n, v in findings.items() if n in KNOWN and n not in BLOCKING}
    new = {n: v for n, v in findings.items() if n not in KNOWN and n not in BLOCKING}
    if debt and not quiet:
        print("  ok       %d known name(s) still to triage: %s"
              % (len(debt), ", ".join(sorted(debt))))
    if blocking:
        print("  REFUSED  %d BLOCKING name(s) hold this release:"
              % len(blocking))
        for name in sorted(blocking):
            print("    %s" % name)
            for rel, lineno, line in blocking[name][:3]:
                print("        %s:%d  %s" % (rel, lineno, line[:110]))
        print("""
  Each entry in BLOCKING passed the three-condition test for KNOWN entries: the defect
  renders a capability non-functional, the capability is reachable on a clean
  install, and no post-install workaround is documented. Resolve the defect or
  remove the name from BLOCKING only after re-applying all three conditions.""")
    if not new and not blocking:
        if not quiet:
            print("  ok       no new required address is read from a name "
                  "nothing supplies")
        return 0
    if new:
        print("  REFUSED  %d environment name(s) read with an empty default, "
              "supplied nowhere, and unguarded:" % len(new))
        for name in sorted(new):
            print("    %s" % name)
            for rel, lineno, line in new[name][:3]:
                print("        %s:%d  %s" % (rel, lineno, line[:110]))
        print("""
  Each one is a node that will fail at runtime with an empty value, far from
  the line that should have refused. Fix it by taking the address from the
  node's own contract, or refuse at startup and name what is missing. If the
  name is genuinely supplied from outside the tree, add it to ALLOW in this
  script WITH ITS REASON.""")
    return 1


if __name__ == "__main__":
    sys.exit(main())
