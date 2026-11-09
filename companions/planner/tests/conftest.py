"""Test-suite fixtures and import plumbing for the planner companion.

Another ``planner`` package may resolve first on ``sys.path`` on some hosts,
for example an upstream OSS copy installed elsewhere on the machine. Without
this file, ``import planner.*`` from a test could resolve to that other tree
and drift silently when the companion source and the other copy diverge.

Putting ``companions/planner/src`` at the head of ``sys.path`` once, here,
means every test in this folder tree exercises the companion source that
this change edits. That is the only version the planner service actually
runs.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
_src_str = str(_SRC)
if _src_str not in sys.path:
    sys.path.insert(0, _src_str)

# Drop any pre-imported ``planner`` module that resolved to the other tree so
# the next import re-resolves against the path we just put in front.
for _name in list(sys.modules):
    if _name == "planner" or _name.startswith("planner."):
        del sys.modules[_name]
