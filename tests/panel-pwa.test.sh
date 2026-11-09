#!/usr/bin/env bash
# The admin panel is an installable app: a manifest and icons of the sizes they claim, every page links them and
# registers the worker, the worker keeps only its offline page, the browser can fetch the app files without a
# session (their own route, no sign-in middleware), and the manifest is served with its own type.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
python3 - "$ROOT" <<'PY'
import json, pathlib, re, struct, sys

root = pathlib.Path(sys.argv[1])
ui = root / "bootstrap/smart-node-ui"
src = ui / "src"
fails = 0

def check(name, ok, extra=""):
    global fails
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else " -> " + str(extra)))
    if not ok:
        fails += 1

def png_size(p):
    b = p.read_bytes()
    return struct.unpack(">II", b[16:24]) if b[:8] == b"\x89PNG\r\n\x1a\n" else None

try:
    m = json.loads((src / "manifest.webmanifest").read_text())
except Exception as e:                      # noqa: BLE001
    m = {}
    check("the manifest is valid JSON", False, e)
check("the manifest names the app and opens it standalone at the root",
      m.get("name") == "Smart Node Admin" and m.get("short_name") == "Admin" and m.get("start_url") == "/"
      and m.get("scope") == "/" and m.get("display") == "standalone", m)
sizes_ok = True
for i in m.get("icons", []):
    s = png_size(src / i["src"].lstrip("/"))
    if s is None or "%dx%d" % s != i["sizes"]:
        sizes_ok = False
check("every icon in the manifest exists at the size it claims", bool(m.get("icons")) and sizes_ok)
check("there are 192, 512 and a maskable icon",
      {"192x192", "512x512"} <= {i["sizes"] for i in m.get("icons", [])} and any(i.get("purpose") == "maskable" for i in m.get("icons", [])))
check("the touch icon is 180x180", png_size(src / "icons/apple-touch-icon.png") == (180, 180))

pages = sorted(src.rglob("index.html"))
missing = [str(p.relative_to(src)) for p in pages
           if not all(s in p.read_text() for s in ('<link rel="manifest" href="/manifest.webmanifest">',
                                                   '<link rel="apple-touch-icon" href="/icons/apple-touch-icon.png">',
                                                   '<script src="/assets/pwa.js" defer></script>'))]
check("every page (%d) links the manifest, the touch icon and the registration" % len(pages), len(pages) >= 20 and not missing, missing[:3])
check("pwa.js registers /sw.js", "serviceWorker.register('/sw.js')" in (src / "assets/pwa.js").read_text())

sw = (src / "sw.js").read_text()
check("the worker keeps only its offline page and answers only failed page loads",
      (src / "offline.html").exists() and "c.add(new Request(OFFLINE" in sw and ".put(" not in sw and "addAll(" not in sw
      and "if (e.request.mode !== 'navigate') return;" in sw and "fetch(e.request).catch(() => caches.match(OFFLINE))" in sw)

b06 = (root / "bootstrap/B06-web-interface.sh").read_text()
rule = re.search(r'snpanelapp\.rule=\((.*?)"', b06)
check("the app files have their own route", rule is not None)
if rule:
    paths = sorted(re.findall(r"(?:Path|PathPrefix)\(\\`([^`\\]+)\\`\)", rule.group(1)))
    check("the route covers exactly the manifest, the worker, the offline page and the icons",
          paths == ["/icons/", "/manifest.webmanifest", "/offline.html", "/sw.js"], paths)
check("that route carries no sign-in middleware", "snpanelapp.middlewares" not in b06)
check("it outranks the gated panel route", 'snpanelapp.priority=200' in b06)
check("it has its TLS label, set in all three cases (resolver, self-signed, local http)", b06.count("tls_app") >= 5, b06.count("tls_app"))

for name in ("nginx.conf", "nginx.local.conf"):
    t = (ui / name).read_text()
    check(name + " serves the manifest with its own type", "location = /manifest.webmanifest" in t and "application/manifest+json" in t)

v = (ui / "VERSION").read_text().strip()
check("the panel version moved, so the image is rebuilt on update", v != "0.8.10", v)
sys.exit(1 if fails else 0)
PY
echo "PASS: panel pwa"
