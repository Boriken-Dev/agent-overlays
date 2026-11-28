"""Project scope: an injected AGENTS_PROJECT_ROOT does not leak into another project's directory."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("duho")

_LIB = Path(__file__).resolve().parents[1] / "lib" / "python"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import _scope  # noqa: E402


@pytest.fixture
def projects(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".agents").mkdir(parents=True)
    monkeypatch.setenv("AGENTS_HOME", str(home / ".agents"))
    monkeypatch.delenv("AGENTS_SCOPE_ROOT", raising=False)
    monkeypatch.delenv(_scope.PIN_ENV, raising=False)
    a, b = home / "a", home / "b"
    for p in (a, b):
        (p / ".agents").mkdir(parents=True)
        (p / "src").mkdir()
    (home / "loose").mkdir()
    monkeypatch.setenv("AGENTS_PROJECT_ROOT", str(a))
    return a, b, home


def test_a_directory_in_another_project_wins_over_the_pinned_root(projects, capsys):
    a, b, _ = projects
    assert _scope.resolve_project_scope(b / "src").root == b.resolve()
    assert "AGENTS_PROJECT_ROOT" in capsys.readouterr().err


def test_inside_the_pinned_project_or_in_no_project_the_pin_holds(projects):
    a, _, home = projects
    assert _scope.resolve_project_scope(a / "src").root == a.resolve()
    # the user's global store is not a project, so `loose` (under it) is in no project
    assert _scope.resolve_project_scope(home / "loose").root == a.resolve()


def test_the_pin_can_be_kept_by_the_operator(projects, monkeypatch, capsys):
    a, b, _ = projects
    monkeypatch.setenv(_scope.PIN_ENV, "1")
    assert _scope.resolve_project_scope(b / "src").root == a.resolve()
    assert capsys.readouterr().err == ""
