#!/usr/bin/env python3
"""A large line must not kill a session.

REPORTED FROM A NODE, not from a test, twice.

First: reading a ~15KB document through the agent failed the session with
"Separator is found, but chunk is longer than limit". That is asyncio's
StreamReader refusing a line longer than its buffer, which defaults to 64 KiB,
because the CLI was spawned without a `limit=`. Nothing caught it, so an ordinary
read surfaced as a crash. Note what the 15 KB does NOT do: it does not trip the
limit by itself. The turn that quotes the document back, escaped into one JSON
line, is what clears 64 KiB.

Second, and the reason this test was rewritten: the fix went onto ONE of
the two spawn paths. `backends/cli_claude.py` -- the path the Runner drives for
every agent wake -- kept asyncio's 64 KiB and had no ValueError handler at all,
so a wake died at ~80ms on the CLI's first JSON line, which echoes the assembled
system prompt. Measured on live nodes: those prompts are 27-35 KiB today, half
the ceiling, and one node's was over it.

THE PREVIOUS VERSION OF THIS FILE ASSERTED EXACTLY THAT AND DID NOT TEST IT. Its
helper docstring read "Every CLI spawn passes the ceiling. Both of them: the fix
is worthless on whichever path is left behind, and there are two" -- while TARGET
pointed at session_manager.py alone. It counted that file's spawns, found 2 of 2,
and passed while the other path was broken in production. If you change this
test, change what it READS, not only what it says.

WHAT IS ASSERTED
  1. the ceiling is declared ONCE, in config.py, and is above the old default --
     these two files held two copies of a spawn constant before and drifted
    , which is why it lives in config now;
  2. EVERY spawn that pipes stdout, in EITHER module, passes that ceiling.
     Spawns that do not read stdout (the sudo kill helper) are exempt, and the
     distinction is made by parsing the call, not by counting text;
  3. every module that reads a pipe catches ValueError, because raising the
     ceiling alone would only move the cliff.

The exception to catch is ValueError, NOT LimitOverrunError: asyncio catches
LimitOverrunError inside readline() and re-raises ValueError. Written against the
documented name first, this test passed while the bug was still live.

    python3 e2e_stream_limit.py                 # working tree, expect PASS
    python3 e2e_stream_limit.py --baseline REF  # git:REF, expect FAIL

Real child processes, real pipes. No model is called.
"""
import argparse
import ast
import asyncio
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GW = os.path.join(HERE, "..")

# Where the ceiling is declared, and every module that spawns a CLI it reads.
CONFIG = os.path.join(GW, "config.py")
SPAWNERS = (
    os.path.join(GW, "session_manager.py"),
    os.path.join(GW, "backends", "cli_claude.py"),
)

DEFAULT_ASYNCIO_LIMIT = 64 * 1024   # what a module gets when it passes no limit=


def _repo_rel(path):
    root = subprocess.check_output(["git", "rev-parse", "--show-toplevel"],
                                   cwd=HERE, text=True).strip()
    return os.path.relpath(os.path.abspath(path), root)


def source(path, baseline=None):
    """A module's text: the working tree, or a git revision of it. Missing at
    that revision is not an error -- the file may not have existed yet."""
    if baseline:
        try:
            return subprocess.check_output(
                ["git", "show", f"{baseline}:{_repo_rel(path)}"],
                cwd=HERE, text=True, stderr=subprocess.DEVNULL)
        except subprocess.CalledProcessError:
            return None
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def stream_limit(srcs):
    """The declared ceiling. Looked for in config.py first (where it belongs
    now) and then in any spawner, so --baseline still means something across
    the move. None = nothing declares it, and asyncio's 64 KiB applies."""
    for src in srcs:
        if not src:
            continue
        m = re.search(r"^STREAM_LIMIT\s*=\s*(.+)$", src, re.M)
        if m:
            try:
                return eval(m.group(1).split("#")[0].strip(),
                            {"__builtins__": {}}, {})
            except Exception:                      # noqa: BLE001
                return None
    return None


def piped_spawns(src):
    """Every asyncio.create_subprocess_exec call that PIPES STDOUT, and whether
    it passes a limit.

    Parsed, not counted. A text count cannot tell the session spawn from the
    sudo kill helper, which pipes nothing and needs no ceiling -- and a test
    that demands one there would be wrong forever, which is how a bad check
    becomes an ignored check.

    Returns (piped_total, piped_with_limit).
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return (0, 0)
    total = limited = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
        if name != "create_subprocess_exec":
            continue
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        out = kw.get("stdout")
        pipes_stdout = (isinstance(out, ast.Attribute) and out.attr == "PIPE")
        if not pipes_stdout:
            continue
        total += 1
        if "limit" in kw:
            limited += 1
    return (total, limited)


def reads_a_pipe(src):
    """The module reads a stream itself (rather than only communicate())."""
    return ".readline()" in src or ".readuntil(" in src


def catches_oversize(src):
    """The read catches ValueError. Without this the raised ceiling just moves
    the cliff and the next big line dies the same obscure death."""
    return src.count("except ValueError")


async def read_one_line(nbytes, limit):
    """A child emits ONE json line of about `nbytes`, read back through a pipe
    opened with `limit`. Returns the line length, or the exception."""
    payload = json.dumps({"type": "assistant",
                          "message": {"role": "assistant",
                                      "content": [{"type": "text",
                                                   "text": "x" * nbytes}]}})
    # Through stdin, not argv: a 200KB argument is E2BIG, which would fail this
    # test for a reason that has nothing to do with what it tests.
    child = ("import sys; d=sys.stdin.read(); "
             "sys.stdout.write(d); sys.stdout.flush()")
    kw = {"limit": limit} if limit else {}
    p = await asyncio.create_subprocess_exec(
        sys.executable, "-c", child,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, **kw)
    p.stdin.write((payload + "\n").encode())
    await p.stdin.drain()
    p.stdin.close()
    try:
        return len(await p.stdout.readline())
    except Exception as exc:                      # noqa: BLE001 -- that IS the result
        return exc
    finally:
        try:
            p.kill()
        except Exception:
            pass
        await p.wait()


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", help="git revision to run against instead")
    args = ap.parse_args()
    what = args.baseline or "working tree"

    cfg = source(CONFIG, args.baseline)
    mods = [(os.path.basename(p), source(p, args.baseline)) for p in SPAWNERS]
    limit = stream_limit([cfg] + [s for _, s in mods])
    failures = []

    def check(ok, what_):
        print(("  ok   " if ok else "  FAIL ") + what_)
        if not ok:
            failures.append(what_)

    print(f"gateway stream limit ({what})")

    # --- what is declared, and where ----------------------------------------
    check(limit is not None,
          "a STREAM_LIMIT is declared rather than inheriting asyncio's 64 KiB")
    check(limit is not None and limit > DEFAULT_ASYNCIO_LIMIT,
          "the ceiling is above the default that produced the report")
    check(bool(cfg) and re.search(r"^STREAM_LIMIT\s*=", cfg, re.M) is not None,
          "the ceiling is declared in config.py -- ONE definition for both "
          "paths (these two files held two copies of a spawn constant before "
          "and drifted)")

    # --- and that every reading path actually asks for it --------------------
    for name, src in mods:
        if not src:
            check(False, f"{name} is readable")
            continue
        total, limited = piped_spawns(src)
        check(total > 0 and total == limited,
              f"{name}: every stdout-piping CLI spawn passes the ceiling "
              f"({limited}/{total})")
        if reads_a_pipe(src):
            check(catches_oversize(src) >= 1,
                  f"{name}: the read catches ValueError (asyncio re-raises "
                  f"LimitOverrunError as ValueError -- the documented name is "
                  f"not the one that arrives)")

    # --- and what that actually does through a real pipe ---------------------
    # The size that produced the first field report. Proves the claim rather
    # than trusting the constant.
    big = 70_000
    got = await read_one_line(big, None)
    check(isinstance(got, ValueError),
          f"a {big}-char line is REFUSED at asyncio's default "
          f"(this is the reported failure: {type(got).__name__})")

    if limit:
        got = await read_one_line(big, limit)
        check(isinstance(got, int) and got > big,
              f"the same line is read whole at the declared ceiling ({got})")

    print()
    if failures:
        print(f"FAILED ({len(failures)}) against {what}")
        return 1
    print(f"PASS against {what}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
