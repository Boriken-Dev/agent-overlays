"""Make the pyvenv skill importable for the tests, regardless of cwd."""
import sys
from pathlib import Path

_SKILL = Path(__file__).resolve().parents[1] / "skills" / "pyvenv"
if str(_SKILL) not in sys.path:
    sys.path.insert(0, str(_SKILL))
