"""Single source of truth for the core-api version.

Reads the `VERSION` file next to this module so every endpoint reports the
same number. Bump `VERSION`; do not hardcode versions anywhere else.
"""

from pathlib import Path

__version__ = (Path(__file__).resolve().parent / "VERSION").read_text().strip()
