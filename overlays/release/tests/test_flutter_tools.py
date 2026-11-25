"""The Flutter mirror tools: each starts and describes itself without network or credentials, the mirror finds the engineering
overlay's scanner and needs a git identity only when it commits, and the dispatch workflow is the one the mirror carries."""
from __future__ import annotations

import importlib.util
import os
import py_compile
import subprocess
import sys
from pathlib import Path

import pytest

FLUTTER = Path(__file__).resolve().parents[1] / "flutter"
ENGINEERING = Path(__file__).resolve().parents[2] / "engineering"
ENV = dict(os.environ, ENGINEERING_OVERLAY_ROOT=str(ENGINEERING), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)


def run(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-I", *args], capture_output=True, text=True, env=env or ENV)


@pytest.mark.parametrize("name", ["flutter_sdk", "flutter_mirror", "flutter_artifacts", "flutter_logproxy"])
def test_each_tool_compiles(name: str, tmp_path: Path) -> None:
    py_compile.compile(str(FLUTTER / f"{name}.py"), cfile=str(tmp_path / "x.pyc"), doraise=True)


@pytest.mark.parametrize("name", ["flutter_sdk", "flutter_mirror", "flutter_artifacts"])
def test_help_runs_with_no_machine_state(name: str, tmp_path: Path) -> None:
    env = dict(ENV, HOME=str(tmp_path), USERPROFILE=str(tmp_path))
    done = run(str(FLUTTER / f"{name}.py"), "--help", env=env)
    assert done.returncode == 0, done.stderr
    assert "usage:" in done.stdout


def test_no_tool_names_a_private_path_or_a_person() -> None:
    for file in FLUTTER.iterdir():
        text = file.read_text(encoding="utf-8")
        assert "~/.agents" not in text, file.name
        assert "devel/" not in text and "C:\\Users" not in text, file.name


def load_mirror():
    os.environ["ENGINEERING_OVERLAY_ROOT"] = str(ENGINEERING)
    spec = importlib.util.spec_from_file_location("flutter_mirror_under_test", FLUTTER / "flutter_mirror.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_mirror_loads_the_engineering_overlays_scanner() -> None:
    mirror = load_mirror()
    assert mirror.scan.classify(b"PK\x03\x04" + bytes(10), {"a.zip"}) == ("flag", "zip")


def test_the_commit_identity_is_the_machines_and_required(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    mirror = load_mirror()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "none"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    with pytest.raises(SystemExit) as stopped:
        mirror.commit_identity()
    assert "user.name" in str(stopped.value)
    config = tmp_path / "gitconfig"
    config.write_text("[user]\n\tname = Someone\n\temail = someone@example.test\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    assert mirror.commit_identity() == ["-c", "user.name=Someone", "-c", "user.email=someone@example.test"]


def test_the_dispatch_workflow_is_started_by_hand() -> None:
    text = (FLUTTER / "flutter_images_dispatch.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in text
    assert "push:" not in text and "schedule:" not in text
