"""Make the overlay's lib/python/ importable for the tests, regardless of cwd.

The skill and the hooks load `lib/python/_link.py` by path; the tests import
`_link` directly, so they put `lib/python/` on `sys.path` here.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _p in (_ROOT / "lib" / "python",):
    s = str(_p)
    if s not in sys.path:
        sys.path.insert(0, s)
