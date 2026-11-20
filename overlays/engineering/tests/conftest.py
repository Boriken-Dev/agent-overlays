"""Puts the overlay's lib/python/ on sys.path for the tests, and keeps them off the real machine files.

The commands load their library by path and do not rely on this."""
import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parents[1] / "lib" / "python"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))


@pytest.fixture(autouse=True)
def no_machine_agent_files(monkeypatch, tmp_path):
    """A test never reads the machine's own agent-tools file or build-slot state."""
    monkeypatch.setenv("AGENT_TOOLS_LOCAL", str(tmp_path / "no-such-local.toml"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
