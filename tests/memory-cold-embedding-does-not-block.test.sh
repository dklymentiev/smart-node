#!/usr/bin/env bash
# A cold sentence-transformers load must not monopolize uvicorn's event loop.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"
D="$(mktemp -d)"
trap 'rm -rf "$D"' EXIT

SOURCE="$REPO/companions/mesh/mesh/embeddings.py"
MEMORY_INSTALLER="$REPO/companions/memory.sh"
if [[ -n "$BASELINE" ]]; then
    git -C "$REPO" show "$BASELINE:companions/mesh/mesh/embeddings.py" >"$D/embeddings.py"
    git -C "$REPO" show "$BASELINE:companions/memory.sh" >"$D/memory.sh"
    SOURCE="$D/embeddings.py"
    MEMORY_INSTALLER="$D/memory.sh"
    echo "memory cold embedding (git:$BASELINE)"
else
    echo "memory cold embedding (working tree)"
fi

SOURCE="$SOURCE" python3 <<'PY'
import asyncio
import importlib.util
import os
import sys
import time
import types

loads = 0

class FakeSentenceTransformer:
    def __init__(self, _name):
        global loads
        loads += 1
        time.sleep(0.30)  # stand-in for the 20+ second cold model load

    def get_sentence_embedding_dimension(self):
        return 768

sentence_transformers = types.ModuleType("sentence_transformers")
sentence_transformers.SentenceTransformer = FakeSentenceTransformer
sys.modules["sentence_transformers"] = sentence_transformers

mesh = types.ModuleType("mesh")
mesh.__path__ = []
sys.modules["mesh"] = mesh
config = types.ModuleType("mesh.config")
config.get_embedding_model_name = lambda: "fake/e5"
sys.modules["mesh.config"] = config

spec = importlib.util.spec_from_file_location("mesh.embeddings", os.environ["SOURCE"])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

failed = 0

def check(label, condition, detail=""):
    global failed
    if condition:
        print(f"  ok   {label}{detail}")
    else:
        failed += 1
        print(f"  FAIL {label}{detail}")

async def probe():
    global loads
    module.embedding_service = module.EmbeddingService()
    started = time.monotonic()
    cold_load = asyncio.create_task(module.get_embedding_service())
    await asyncio.sleep(0.04)
    event_loop_delay = time.monotonic() - started
    await cold_load
    check(
        "cold model construction leaves the HTTP event loop responsive",
        event_loop_delay < 0.15,
        f" (40ms timer resumed after {event_loop_delay:.3f}s)",
    )

    loads = 0
    module.embedding_service = module.EmbeddingService()
    await asyncio.gather(*(module.get_embedding_service() for _ in range(3)))
    check("three queue workers initialize the shared model once", loads == 1,
          f" (loads={loads})")

asyncio.run(probe())
print()
if failed:
    print(f"FAIL ({failed})")
    raise SystemExit(1)
print("PASS")
PY
python_rc=$?

if grep -q '^STAGE_VERSION="0\.3\.4"' "$MEMORY_INSTALLER"; then
    echo "  ok   memory stage version deploys the changed source on update"
    stage_rc=0
else
    echo "  FAIL memory stage version deploys the changed source on update"
    stage_rc=1
fi

(( python_rc == 0 && stage_rc == 0 ))
