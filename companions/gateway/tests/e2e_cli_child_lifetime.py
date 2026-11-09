#!/usr/bin/env python3
"""The gateway must own the lifetime of the `claude` children it spawns.

Constructs each failing state rather than inspecting a healthy one, and runs the
same scenarios against the pre-fix module so the difference is demonstrated:

    python3 e2e_cli_child_lifetime.py                 # working tree, expect PASS
    python3 e2e_cli_child_lifetime.py --baseline REF  # git:REF, expect FAIL

Uses REAL child processes (`sleep`), so the kill path -- process group, SIGTERM,
grace, SIGKILL -- is genuinely exercised. No model is called.
"""
import argparse
import asyncio
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(HERE, "..", "backends", "cli_claude.py")


def load(path, name):
    """Import cli_claude with its gateway-local imports stubbed out."""
    # Every gateway-local module cli_claude imports has to be named here. The
    # list fell behind the imports once (core_api_proof, cli_prompt, turn_limits)
    # and this file then could not load the module at all, in a tree where no
    # gate ran it. tests/gateway-cli-child-lifetime.test.sh runs it now.
    for mod, attrs in {
        "db": {}, "secrets_store": {}, "core_api_proof": {}, "cli_prompt": {},
        "turn_limits": {}, "anthropic_relay": {}, "openai_relay": {},
        # a reclaim revokes the session's model token (gateway model path)
        "model_tokens": {"revoke_session": lambda *a, **k: None},
        "backends": {"SessionBackend": object,
                     "BackendResponse": type("BackendResponse", (), {})},
        "config": {"CLI_PATH": "claude", "CLI_DEFAULT_CWD": "/tmp",
                   "SKIP_PERMISSIONS": True},
    }.items():
        if mod not in sys.modules:
            m = types.ModuleType(mod)
            for k, v in attrs.items():
                setattr(m, k, v)
            m.__getattr__ = lambda n: None       # tolerate anything else it wants
            sys.modules[mod] = m
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


async def _spawn_child():
    """A real process in its own group, standing in for a `claude` child."""
    return await asyncio.create_subprocess_exec(
        "sleep", "600",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# --- scenarios --------------------------------------------------------------

def scenario_idle_child_is_reclaimed(mod):
    """An idle CLI child is reclaimed on a CLOCK, not only when a new spawn hits
    the cap. That distinction is the whole bug: archive-node sat at exactly four
    children for four days because no fifth session ever arrived to evict one."""
    async def run():
        if not hasattr(mod, "_idle_sweep_once"):
            return {"has an idle sweep at all": False, "ok": False}
        proc = await _spawn_child()
        pid = proc.pid
        mod._PROCESSES["idle-sess"] = proc
        mod._LAST_USED["idle-sess"] = mod.time.monotonic() - 10_000  # long idle
        mod.IDLE_TIMEOUT_SEC = 900
        n = await mod._idle_sweep_once()
        await asyncio.sleep(0.3)
        still = alive(pid)
        if still:
            os.kill(pid, 9)
        return {"reclaimed": n, "process still alive": still,
                "dropped from registry": "idle-sess" not in mod._PROCESSES,
                "ok": n == 1 and not still and "idle-sess" not in mod._PROCESSES}
    return asyncio.run(run())


def scenario_busy_child_is_spared(mod):
    """A session with a turn IN FLIGHT is never reclaimed, however long it runs.
    A long turn is work, not a leak -- and the longest real wake measured was
    210s against a 900s window, so this is the case that must not regress."""
    async def run():
        if not hasattr(mod, "_idle_sweep_once"):
            return {"has an idle sweep at all": False, "ok": False}
        proc = await _spawn_child()
        pid = proc.pid
        mod._PROCESSES["busy-sess"] = proc
        mod._LAST_USED["busy-sess"] = mod.time.monotonic() - 10_000
        lock = asyncio.Lock()
        await lock.acquire()                     # mid-turn
        mod._SEND_LOCKS["busy-sess"] = lock
        mod.IDLE_TIMEOUT_SEC = 900
        n = await mod._idle_sweep_once()
        still = alive(pid)
        lock.release()
        if still:
            os.kill(pid, 9)
        return {"reclaimed": n, "process survived": still,
                "ok": n == 0 and still}
    return asyncio.run(run())


def scenario_shutdown_leaves_nothing(mod):
    """No child outlives the gateway. On a live node the orphans' parents were three
    DIFFERENT gateway pids, so a restart was leaving children behind."""
    async def run():
        if not hasattr(mod, "shutdown_all"):
            return {"has a shutdown sweep at all": False, "ok": False}
        pids = []
        for i in range(3):
            p = await _spawn_child()
            pids.append(p.pid)
            mod._PROCESSES["sess-%d" % i] = p
            mod._LAST_USED["sess-%d" % i] = mod.time.monotonic()
        killed = await mod.shutdown_all("test shutdown")
        await asyncio.sleep(0.3)
        survivors = [p for p in pids if alive(p)]
        for p in survivors:
            os.kill(p, 9)
        return {"killed": killed, "survivors": survivors,
                "registry emptied": not mod._PROCESSES,
                "ok": killed == 3 and not survivors and not mod._PROCESSES}
    return asyncio.run(run())


def scenario_child_count_is_reportable(mod):
    """The count of live children is exposed, so 'how many agents are running'
    stops being answerable only by ps."""
    async def run():
        if not hasattr(mod, "live_process_count"):
            return {"exposes a count at all": False, "ok": False}
        before = mod.live_process_count()
        p = await _spawn_child()
        mod._PROCESSES["counted"] = p
        during = mod.live_process_count()
        await mod.shutdown_all("test")
        await asyncio.sleep(0.2)
        after = mod.live_process_count()
        if alive(p.pid):
            os.kill(p.pid, 9)
        return {"before": before, "during": during, "after": after,
                "ok": during == before + 1 and after == 0}
    return asyncio.run(run())


def _record_db(fail_events=False):
    """Replace the stubbed db.query with a recorder; returns the list it fills.
    With fail_events, a write to the journal raises, as an unreachable DB would."""
    calls = []

    def query(sql, params=None, **kw):
        if fail_events and "sn_core.events" in sql:
            raise RuntimeError("journal unavailable")
        calls.append((sql, params))
    sys.modules["db"].query = query
    return calls


def _events(calls):
    """The journal rows among recorded db calls: (event_type, session, payload)."""
    out = []
    for sql, params in calls:
        if "INSERT INTO sn_core.events" not in sql:
            continue
        m = re.search(r"'component:gateway', '([a-z_.]+)', 'ai_session'", sql)
        out.append((m.group(1) if m else None, params[0], json.loads(params[1])))
    return out


def scenario_turn_in_flight_stays_active_for_the_reaper(mod):
    """A turn in flight is stamped active on every sweep. The session reaper
    kills by ai_sessions.last_activity_at alone, and a CLI turn writes no
    message row until it ends, so without the stamp a turn longer than the
    reaper's fifteen minutes is killed as idle while it works. A session with
    no turn is NOT stamped: it has to go on ageing or nothing is ever reaped."""
    async def run():
        calls = _record_db()
        busy, quiet = await _spawn_child(), await _spawn_child()
        mod._PROCESSES["busy-sess"], mod._PROCESSES["quiet-sess"] = busy, quiet
        now = mod.time.monotonic()
        mod._LAST_USED["busy-sess"] = now - 10_000
        mod._LAST_USED["quiet-sess"] = now - 10
        lock = asyncio.Lock()
        await lock.acquire()                     # mid-turn, long past both windows
        mod._SEND_LOCKS["busy-sess"] = lock
        mod.IDLE_TIMEOUT_SEC = 900
        n = await mod._idle_sweep_once()
        lock.release()
        for p in (busy, quiet):
            if alive(p.pid):
                os.kill(p.pid, 9)
        stamped = [params[0] for sql, params in calls
                   if "last_activity_at = now()" in sql and "sn_core.ai_sessions" in sql]
        return {"reclaimed": n, "sessions stamped active": stamped,
                "ok": n == 0 and stamped == ["busy-sess"]}
    return asyncio.run(run())


def scenario_reclaim_is_journaled(mod):
    """Ending a CLI process leaves a row in the node's journal that names the
    agent, the reason and how long the session had been idle. Before, the only
    trace of an agent's process being ended was a line in the service log."""
    async def run():
        calls = _record_db()
        proc = await _spawn_child()
        pid = proc.pid
        mod._PROCESSES["idle-sess"] = proc
        mod._SESSION_AGENT["idle-sess"] = "agent-b"
        mod._LAST_USED["idle-sess"] = mod.time.monotonic() - 1_000
        mod.IDLE_TIMEOUT_SEC = 900
        n = await mod._idle_sweep_once()
        await asyncio.sleep(0.3)
        still = alive(pid)
        if still:
            os.kill(pid, 9)
        events = _events(calls)
        ev = events[0] if events else (None, None, {})
        p = ev[2]
        return {"reclaimed": n, "events": [(e[0], e[1]) for e in events], "payload": p,
                "ok": n == 1 and not still and len(events) == 1
                      and ev[0] == "session.reclaimed" and ev[1] == "idle-sess"
                      and p.get("agent") == "agent-b"
                      and str(p.get("reason", "")).startswith("idle ")
                      and p.get("turn_in_flight") is False
                      and 990 <= (p.get("idle_seconds") or 0) <= 1_010}
    return asyncio.run(run())


def scenario_reclaim_of_a_live_turn_says_so(mod):
    """A process ended with a turn in flight (a restart, a ceiling) is journaled
    as cut work, with how long the turn had run, not as an idle session."""
    async def run():
        calls = _record_db()
        proc = await _spawn_child()
        pid = proc.pid
        mod._PROCESSES["live-sess"] = proc
        mod._SESSION_AGENT["live-sess"] = "agent-b"
        now = mod.time.monotonic()
        mod._LAST_USED["live-sess"] = now - 400
        mod._TURN_STARTED["live-sess"] = now - 400
        await mod.shutdown_all("gateway shutting down")
        await asyncio.sleep(0.3)
        still = alive(pid)
        if still:
            os.kill(pid, 9)
        events = _events(calls)
        p = events[0][2] if events else {}
        return {"events": [(e[0], e[1]) for e in events], "payload": p,
                "ok": not still and len(events) == 1 and p.get("turn_in_flight") is True
                      and 395 <= (p.get("turn_seconds") or 0) <= 410
                      and p.get("idle_seconds") is None
                      and p.get("reason") == "gateway shutting down"}
    return asyncio.run(run())


def scenario_journal_failure_does_not_stop_the_reclaim(mod):
    """The journal being unavailable must not leave the process running."""
    async def run():
        _record_db(fail_events=True)
        proc = await _spawn_child()
        pid = proc.pid
        mod._PROCESSES["idle-sess"] = proc
        mod._LAST_USED["idle-sess"] = mod.time.monotonic() - 10_000
        mod.IDLE_TIMEOUT_SEC = 900
        n = await mod._idle_sweep_once()
        await asyncio.sleep(0.3)
        still = alive(pid)
        if still:
            os.kill(pid, 9)
        return {"reclaimed": n, "process still alive": still,
                "has a journal writer to fail": hasattr(mod, "_journal_reclaim"),
                "ok": n == 1 and not still and hasattr(mod, "_journal_reclaim")}
    return asyncio.run(run())


SCENARIOS = [
    ("an idle child is reclaimed on a clock", scenario_idle_child_is_reclaimed),
    ("a child mid-turn is spared", scenario_busy_child_is_spared),
    ("shutdown leaves no child behind", scenario_shutdown_leaves_nothing),
    ("the live child count is reportable", scenario_child_count_is_reportable),
    ("a turn in flight stays active for the session reaper",
     scenario_turn_in_flight_stays_active_for_the_reaper),
    ("a reclaim is journaled with agent, reason and duration", scenario_reclaim_is_journaled),
    ("a reclaim of a live turn is journaled as cut work", scenario_reclaim_of_a_live_turn_says_so),
    ("a journal failure does not stop the reclaim",
     scenario_journal_failure_does_not_stop_the_reclaim),
]


def run_all(path, tag, expect_pass):
    print("\n=== %s ===" % tag)
    results = []
    for i, (name, fn) in enumerate(SCENARIOS):
        mod = load(path, "cli_claude_%s_%d" % (abs(hash(tag)) % 9999, i))
        try:
            out = fn(mod)
            ok = out.pop("ok")
        except Exception as e:
            out, ok = {"raised": repr(e)}, False
        results.append(ok)
        print("  [%s] %s" % ("PASS" if ok else "FAIL", name))
        for k, v in out.items():
            print("         %s: %s" % (k, v))
    return all(results) if expect_pass else not all(results)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", help="git ref of the pre-fix module, expected to FAIL")
    args = ap.parse_args()
    ok = run_all(os.path.abspath(TARGET), "working tree (expected: PASS)", True)
    if args.baseline:
        repo = subprocess.check_output(["git", "rev-parse", "--show-toplevel"],
                                       cwd=HERE, text=True).strip()
        blob = subprocess.check_output(
            ["git", "show", "%s:companions/gateway/backends/cli_claude.py" % args.baseline],
            cwd=repo, text=True)
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
            fh.write(blob)
            base = fh.name
        ok = run_all(base, "baseline %s (expected: FAIL)" % args.baseline, False) and ok
        os.unlink(base)
    print("\n%s" % ("ALL GOOD" if ok else "SOMETHING IS WRONG"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
