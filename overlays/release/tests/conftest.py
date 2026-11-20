"""Shared helpers: the repo tools are loaded from their files, since tools/ is not a package."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_TOOLS = Path(__file__).resolve().parents[1] / "repo" / "tools"
GIT_ENV = ["-c", "user.name=test", "-c", "user.email=test@example.test", "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false"]


def load_tool(file: str, name: str):
    path = REPO_TOOLS / file
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *GIT_ENV, "-C", str(cwd), *args], capture_output=True, check=True)
    return done.stdout.decode().strip()


def commit_all(cwd: Path, message: str = "x") -> str:
    git(cwd, "add", "-A")
    git(cwd, "commit", "-q", "-m", message)
    return git(cwd, "rev-parse", "HEAD")


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    return path


@pytest.fixture(scope="session")
def build():
    return load_tool("build.py", "build_tool")


@pytest.fixture(autouse=True)
def isolated_files(monkeypatch, tmp_path):
    """Text a test writes is LF on every platform, as the tools read and write it, and what a
    tool puts in the system temporary directory goes to the test's own."""

    def write_text(self, data, encoding="utf-8", errors=None, newline=None):
        return self.write_bytes(data.encode(encoding or "utf-8"))

    monkeypatch.setattr(Path, "write_text", write_text)
    scratch = tmp_path / "system-temp"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
