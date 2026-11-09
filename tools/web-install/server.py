#!/usr/bin/env python3
"""web-install: the installer in a browser page instead of a terminal.

A small server, standard library only, that starts in a second (the node's own web
interface is part of what gets installed, so it cannot show the install). The page asks
how the AI will work, starts the install with that answer, and draws its progress from
the install's log; when the install is done it hands over to the node's first-run page.

  tools/web-install/server.py -- <install-on.sh arguments without --all/--ai>
      runs orchestrator/install-on.sh --all --ai WAY --first-run-link-file F <arguments>

  tools/web-install/server.py --run 'CMD' --log FILE --link-file FILE
      runs CMD instead; {way} and {components} in it are replaced by the answer and by the
      modules it means (the laptop's local install uses this)

The page is served on 127.0.0.1 only (the machine the install is run from) and every call
needs the token printed with the address. Nothing the install prints is sent anywhere else.
"""
import argparse
import json
import os
import re
import secrets
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", ".."))
COMPONENTS = os.path.join(SRC, "tools", "sn-components")

# The layers before the modules, in the order both installers run them. A step is done
# when the log says so; the words are what a person reads.
BASE_STEPS = [
    ("host", "This machine", "server", r"Layer A OK|Layer A skipped"),
    ("B01", "Core", "cpu", r"stage B01 \(.*\) -- done|^== B01 verify exit=0"),
    ("B02", "Databases", "database", r"stage B02 \(.*\) -- done|^== B02 verify exit=0"),
    ("B03", "Routing", "globe", r"stage B03 \(.*\) -- done|^== B03 verify exit=0"),
    ("B04", "Secret store", "key-round", r"stage B04 \(.*\) -- done|^== B04 verify exit=0"),
    ("B05", "Authentication", "user-check", r"stage B05 \(.*\) -- done|^== B05 verify exit=0"),
    ("B06", "Admin panel", "layout-dashboard", r"stage B06 \(.*\) -- done|^== B06 verify exit=0"),
]
FAILED = re.compile(r"^== (\S+) (?:run|verify) exit=[1-9]|stage (B0\d) \(.*\) -- FAIL|companion (\S+) -- FAIL")


def comp(*args):
    out = subprocess.run([sys.executable, COMPONENTS, *args], capture_output=True, text=True,
                         input="", env={**os.environ, "SN_SRC": SRC})
    if out.returncode:
        raise RuntimeError(out.stderr.strip() or "sn-components failed")
    return out.stdout.strip()


def manifests():
    m = {}
    d = os.path.join(SRC, "companions")
    for f in sorted(os.listdir(d)):
        if f.endswith(".manifest.json"):
            j = json.load(open(os.path.join(d, f), encoding="utf-8"))
            m[j["name"]] = j
    return m


def ways():
    """The answers the page offers: one per subscription runtime, then a key (as sn-components ways)."""
    M = manifests()
    subs = sorted((n for n, j in M.items() if j.get("way")), key=lambda n: M[n]["way"])
    # "runtime": what a subscription adds to run the AI, shown on the page as a tile. A key
    # adds nothing: it runs on the runtime that is in the base (Flint), already listed there.
    run = lambda n: {"title": M[n].get("title", n), "icon": M[n].get("icon", "box")}
    out = [{"id": n, "title": M[n]["way"], "note": f"Sign in with your account. Agents run on {M[n]['title']}.",
            "runtime": run(n)} for n in subs]
    out.append({"id": "key", "title": "API key",
                "note": "Anthropic, OpenAI, Google, OpenRouter or your own address.", "runtime": None})
    return out


def modules():
    """What a person may leave out: every module that is neither core nor a subscription runtime."""
    M = manifests()
    return [{"id": n, "title": j.get("title", n), "provides": j.get("provides", ""), "requires": j.get("requires", []),
             "icon": j.get("icon", "box")}
            for n, j in sorted(M.items(), key=lambda kv: kv[1].get("title", kv[0]))
            if not j.get("core") and not j.get("way")]


def base():
    """What every node gets (core in the manifests), shown on the page as always installed."""
    M = manifests()
    steps = [{"title": t, "icon": i} for _, t, i, _ in BASE_STEPS]
    mods = sorted(({"title": j.get("title", n), "icon": j.get("icon", "box")} for n, j in M.items() if j.get("core")),
                  key=lambda x: x["title"].lower())
    return steps + mods


class Install:
    def __init__(self, args):
        self.a = args
        self.lock = threading.Lock()
        self.state, self.way, self.proc, self.started = "idle", "", None, 0.0
        self.steps = []
        self.log = args.log or os.path.join(tempfile.mkdtemp(prefix="sn-web-install-"), "install.log")
        self.link_file = args.link_file or os.path.join(os.path.dirname(self.log), "first-run-link")

    def start(self, way, chosen=None):
        """chosen: the optional modules to install (None: all of them, as for-way gives)."""
        with self.lock:
            if self.state != "idle":
                return False, "the install has already started"
            if way not in {w["id"] for w in ways()}:
                return False, f"unknown answer '{way}'"
            if chosen is None:
                components = comp("for-way", way)
            else:
                known = {m["id"] for m in modules()}
                bad = [c for c in chosen if c not in known]
                if bad:
                    return False, f"unknown module '{bad[0]}'"
                # what they require is added by the plan (sn-components), not here
                components = ",".join(sorted(set(chosen)) + ([way] if way != "key" else []))
            # Simple with a key chooses nothing: that is the core alone. An empty
            # --components means "decide for me" to install-on.sh, which is every module.
            components = components or ",".join(sorted(n for n, j in manifests().items() if j.get("core")))
            order = comp("plan", components).splitlines()
            order = next((l.split(":", 1)[1].split() for l in order if l.startswith("order:")), [])
            M = manifests()
            self.steps = [{"id": i, "title": t, "icon": ic, "re": r, "status": "wait"} for i, t, ic, r in BASE_STEPS]
            self.steps += [{"id": n, "title": M[n].get("title", n), "icon": M[n].get("icon", "box"),
                            "re": rf"companion {re.escape(n)} -- done|^== {re.escape(n)} verify exit=0",
                            "status": "wait"} for n in order]
            if self.a.run:
                cmd = self.a.run.replace("{way}", shlex.quote(way)).replace("{components}", shlex.quote(components))
                argv, shell = cmd, True
            else:
                argv = [os.path.join(SRC, "orchestrator", "install-on.sh"), "--all", "--ai", way,
                        "--components", components,
                        "--first-run-link-file", self.link_file, *self.a.rest]
                shell = False
            if os.path.exists(self.link_file):
                os.remove(self.link_file)
            out = open(self.log, "ab", buffering=0) if not self.a.log else subprocess.DEVNULL
            self.proc = subprocess.Popen(argv, shell=shell, cwd=SRC, stdin=subprocess.DEVNULL,
                                         stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
            self.state, self.way, self.started = "running", way, time.time()
            return True, ""

    def stop(self):
        """Stop the install the page started: the whole process group (the installer and what it ran)."""
        with self.lock:
            if self.state != "running" or not self.proc:
                return False
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            self.state = "stopped"
            return True

    def progress(self):
        with self.lock:
            text = ""
            try:
                with open(self.log, "rb") as f:
                    text = f.read().decode("utf-8", "replace")
            except OSError:
                pass
            lines = [re.sub(r"\x1b\[[0-9;]*m", "", l) for l in text.splitlines()]
            failed = None
            for l in lines:
                m = FAILED.search(l)
                if m:
                    failed = next(g for g in m.groups() if g)
            for s in self.steps:
                if any(re.search(s["re"], l) for l in lines):
                    s["status"] = "done"
            nxt = next((s for s in self.steps if s["status"] != "done"), None)
            rc = self.proc.poll() if self.proc else None
            if self.state == "running" and rc is not None:  # (a stop set "stopped" already)
                self.state = "done" if rc == 0 else "failed"
            if failed and self.state != "stopped":
                self.state = "failed"
            if nxt and self.state == "running":
                nxt["status"] = "now"
            if self.state == "failed":
                # the step the log names, else the first one not done
                bad = next((x for x in self.steps if x["id"] == failed), nxt)
                if bad:
                    bad["status"] = "failed"
            link = ""
            if self.state == "done":
                try:
                    link = open(self.link_file, encoding="utf-8").read().strip()
                except OSError:
                    link = ""
            return {"state": self.state, "way": self.way,
                    "elapsed": int(time.time() - self.started) if self.started else 0,
                    "steps": [{k: s[k] for k in ("id", "title", "icon", "status")} for s in self.steps],
                    "tail": [l for l in lines if l.strip()][-400:], "log": self.log, "link": link}


def handler(inst, token):
    kit = os.path.join(SRC, "companions", "studio", "public", "ui")
    lucide = os.path.join(SRC, "companions", "studio", "public", "vendor", "lucide.min.js")

    class H(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="application/json"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _ok(self):
            q = parse_qs(urlsplit(self.path).query)
            return secrets.compare_digest((q.get("t") or [""])[0], token) or \
                secrets.compare_digest(self.headers.get("X-Token", ""), token)

        def do_GET(self):
            p = urlsplit(self.path).path
            if p.startswith("/ui/") and p.endswith(".css"):
                f = os.path.join(kit, os.path.basename(p))
                return self._send(200, open(f, "rb").read(), "text/css") if os.path.isfile(f) else self._send(404, b"")
            if p == "/vendor/lucide.min.js":
                with open(lucide, "rb") as f:
                    return self._send(200, f.read(), "text/javascript")
            if not self._ok():
                return self._send(403, b"Open the address the installer printed (it carries the token).", "text/plain")
            if p == "/":
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if p == "/api/ways":
                return self._send(200, ways())
            if p == "/api/base":
                return self._send(200, base())
            if p == "/api/modules":
                return self._send(200, modules())
            if p == "/api/progress":
                return self._send(200, inst.progress())
            self._send(404, {"detail": "not found"})

        def do_POST(self):
            if not self._ok():
                return self._send(403, {"detail": "no token"})
            if urlsplit(self.path).path == "/api/stop":
                return self._send(200, {"stopped": inst.stop()})
            if urlsplit(self.path).path != "/api/start":
                return self._send(404, {"detail": "not found"})
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
                chosen = body.get("modules")
                ok, why = inst.start(body.get("way", ""), chosen if isinstance(chosen, list) else None)
            except (ValueError, RuntimeError) as e:
                ok, why = False, str(e)
            self._send(202 if ok else 409, {"started": ok, "detail": why})

        def log_message(self, *a):
            pass

    return H


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--token", default=os.environ.get("SN_WEB_INSTALL_TOKEN") or secrets.token_urlsafe(18))
    ap.add_argument("--run", help="the install command instead of install-on.sh; {way} and {components} are filled in")
    ap.add_argument("--log", help="the log the install writes (with --run); progress is read from it")
    ap.add_argument("--link-file", help="where the install writes the first-run link")
    ap.add_argument("rest", nargs=argparse.REMAINDER, help="after --: install-on.sh arguments")
    a = ap.parse_args()
    a.rest = a.rest[1:] if a.rest[:1] == ["--"] else a.rest
    inst = Install(a)
    srv = ThreadingHTTPServer((a.bind, a.port), handler(inst, a.token))
    print(f"Smart Node installer: open http://{'127.0.0.1' if a.bind == '0.0.0.0' else a.bind}:{a.port}/?t={a.token}",
          flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
