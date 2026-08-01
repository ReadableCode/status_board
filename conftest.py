"""Pytest bootstrap for the unit suite (``tests/``).

pytest imports the repo-root ``conftest.py`` for any test session, so the
import-path setup lives here and individual test files never manipulate
``sys.path`` themselves. It makes the first-party code importable:

* the repo root, so ``from src.status_board import ...`` resolves
* ``src``, so ``from utils.statusboard_tools import ...`` (the same import
  the scripts use at runtime) resolves
"""

import os
import sys

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(ROOT_DIR, "src")

for _path in (ROOT_DIR, SRC_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)
