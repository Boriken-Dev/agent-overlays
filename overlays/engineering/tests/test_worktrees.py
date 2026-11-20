"""`dotagents worktrees`: merged worktrees are the ones whose patches are all upstream; --remove skips a dirty one."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

duho = pytest.importorskip("duho")

MODULE = Path(__file__).resolve().parents[1] / "cmds" / "worktrees.py"
_spec = importlib.util.spec_from_file_location("worktrees_cmd", MODULE)
worktrees = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = worktrees
_spec.loader.exec_module(worktrees)

IDENT = ["-c", "user.name=T", "-c", "user.email=t@example.test", "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false"]


def git(cwd, *args):
    done = subprocess.run(["git", "-C", str(cwd), *IDENT, *args], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


@pytest.fixture
def world(tmp_path):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    main = tmp_path / "main"
    subprocess.run(["git", "clone", "-q", str(bare), str(main)], check=True, capture_output=True)
    git(main, "checkout", "-q", "-B", "main")
    (main / "a.txt").write_text("a\n", encoding="utf-8")
    (main / ".gitignore").write_text("target/\n", encoding="utf-8")
    git(main, "add", "-A")
    git(main, "commit", "-q", "-m", "chore: base")
    git(main, "push", "-q", "origin", "main")
    return main


def add_worktree(main, name, *, commit=True):
    path = main.parent / "wt" / name
    git(main, "worktree", "add", "-q", "-b", name, str(path), "main")
    if commit:
        (path / f"{name}.txt").write_text(name + "\n", encoding="utf-8")
        git(path, "add", "-A")
        git(path, "commit", "-q", "-m", f"feat: {name}")
    (path / "target").mkdir()
    (path / "target" / "big.bin").write_bytes(b"0" * 3000)
    return path


def publish(main, branch):
    """Land the branch's patch upstream as a cherry-pick would."""
    git(main, "cherry-pick", branch)
    git(main, "push", "-q", "origin", "main")


def run(main, *flags):
    return duho.parse(worktrees.Worktrees, ["-C", str(main), *flags])()


def test_merged_and_open_worktrees_are_told_apart_with_target_sizes(world, capsys):
    done = add_worktree(world, "done")
    add_worktree(world, "wip")
    publish(world, "done")
    assert run(world) == 0
    out = capsys.readouterr().out
    assert "merged  done  2.9K" in out
    assert "open    wip  2.9K  (1 commits not upstream)" in out
    assert done.is_dir()


def test_remove_takes_only_the_merged_clean_ones(world, capsys):
    done = add_worktree(world, "done")
    dirty = add_worktree(world, "dirty")
    wip = add_worktree(world, "wip")
    publish(world, "done")
    publish(world, "dirty")
    (dirty / "scratch.txt").write_text("x\n", encoding="utf-8")
    assert run(world, "--remove") == 0
    out = capsys.readouterr().out
    assert not done.exists() and dirty.is_dir() and wip.is_dir()
    assert "removed" in out and "skipped" in out and "uncommitted changes" in out


def test_root_limits_the_worktrees_considered(world, capsys):
    add_worktree(world, "one")
    add_worktree(world, "two")
    assert run(world, "--root", "**/wt/one") == 0
    out = capsys.readouterr().out
    assert "one" in out and "two" not in out


def test_the_configured_root_applies(world, capsys):
    add_worktree(world, "one")
    add_worktree(world, "two")
    (world / ".agents").mkdir()
    (world / ".agents" / "agent-tools.toml").write_text('[worktrees]\nroot = "**/wt/two"\n', encoding="utf-8")
    run(world)
    out = capsys.readouterr().out
    assert "two" in out and " one" not in out


def test_a_worktree_with_no_commits_of_its_own_is_merged(world, capsys):
    add_worktree(world, "fresh", commit=False)
    run(world)
    assert "merged  fresh" in capsys.readouterr().out


def test_the_main_worktree_is_never_listed(world, capsys):
    run(world)
    assert capsys.readouterr().out == ""
