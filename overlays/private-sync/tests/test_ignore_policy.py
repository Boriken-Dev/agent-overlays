"""A store's default ignore rules deny everything and name what is kept."""
import os
import shutil
import subprocess

import pytest

import _link
from _link import BRANCH_IGNORE_DENY_ALL, STORE_IGNORE_MARK, ensure_store_gitignore

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _ignored(repo, rels):
    out = subprocess.run(["git", "-C", str(repo), "check-ignore", "--no-index", *rels],
                         capture_output=True, text=True)
    return set(out.stdout.split())


def _repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return tmp_path


KEPT = ["AGENTS.md", "plans/a.md", "dotagents/cmds/x.py", "kb/n.toml", "plans/.gitkeep",
        ".gitignore", "overlays/demo/.dotagents-install.json"]
DROPPED = ["data.sqlite", "build/out.bin", "scratch/a.md", "tmp/b.md", ".pyvenv/x/y.py",
           "AGENTS.local.md", "dir/__pycache__/m.py", "notes.docx"]


def test_the_default_block_keeps_only_what_it_names(tmp_path, monkeypatch):
    monkeypatch.delenv(_link.IGNORE_POLICY_ENV, raising=False)
    repo = _repo(tmp_path)
    assert ensure_store_gitignore(repo)
    assert _ignored(repo, KEPT) == set()
    assert _ignored(repo, DROPPED) == set(DROPPED)


def test_the_operator_can_weaken_the_policy_and_it_is_logged(tmp_path, monkeypatch):
    monkeypatch.setenv(_link.IGNORE_POLICY_ENV, "allow-all")
    repo = _repo(tmp_path)
    seen = []
    ensure_store_gitignore(repo, log=lambda *a: seen.append(a[0] % a[1:]))
    assert _ignored(repo, ["data.sqlite", "notes.docx"]) == set()
    assert _ignored(repo, ["scratch/a.md", "AGENTS.local.md"]) == {"scratch/a.md", "AGENTS.local.md"}
    assert any(_link.IGNORE_POLICY_ENV in line and "allow-all" in line for line in seen)


def test_an_unknown_policy_value_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv(_link.IGNORE_POLICY_ENV, "everything")
    with pytest.raises(SystemExit):
        ensure_store_gitignore(_repo(tmp_path))


def test_a_store_with_the_earlier_block_is_left_alone_with_a_notice(tmp_path, monkeypatch):
    monkeypatch.delenv(_link.IGNORE_POLICY_ENV, raising=False)
    path = tmp_path / ".gitignore"
    old = STORE_IGNORE_MARK + "\nscratch/\n.*\n!.gitignore\n"
    path.write_bytes(old.encode("utf-8"))
    seen = []
    assert ensure_store_gitignore(tmp_path, log=lambda *a: seen.append(a[0] % a[1:])) is False
    assert path.read_bytes().decode("utf-8") == old
    assert len(seen) == 1 and "allow-all" in seen[0]


def test_a_store_with_the_current_block_gets_no_notice(tmp_path, monkeypatch):
    monkeypatch.delenv(_link.IGNORE_POLICY_ENV, raising=False)
    ensure_store_gitignore(tmp_path)
    seen = []
    assert ensure_store_gitignore(tmp_path, log=lambda *a: seen.append(a)) is False
    assert seen == []


def test_an_existing_rule_of_the_store_still_wins_over_the_block(tmp_path, monkeypatch):
    monkeypatch.delenv(_link.IGNORE_POLICY_ENV, raising=False)
    repo = _repo(tmp_path)
    (repo / ".gitignore").write_bytes(b"overlays/\n")
    ensure_store_gitignore(repo)
    assert _ignored(repo, ["overlays/demo/AGENTS.md"]) == {"overlays/demo/AGENTS.md"}


def test_a_new_project_branch_is_seeded_with_the_same_policy(monkeypatch):
    monkeypatch.delenv(_link.IGNORE_POLICY_ENV, raising=False)
    lines = _link.branch_ignore_lines()
    assert lines[:1] == ["*"] and lines == list(BRANCH_IGNORE_DENY_ALL)
    monkeypatch.setenv(_link.IGNORE_POLICY_ENV, "allow-all")
    assert ".*" in _link.branch_ignore_lines()
