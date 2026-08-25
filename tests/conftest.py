"""Shared test configuration.

Ensures the src-layout package is importable both when the project is
installed (`pip install -e .`) and when running straight from a checkout
(pytest's ``pythonpath = ["src"]`` also covers this, but inserting here
keeps the suite runnable via plain ``python3 -m pytest`` too).
"""

import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
