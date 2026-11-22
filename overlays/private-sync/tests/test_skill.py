"""The `private-sync` script: `link` and `sync` reach the library with their arguments,
from a plain interpreter with duho and nothing else."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("duho")

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "private-sync" / "private_sync.py"
GIT_ENV = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)


def run(*args, cwd=None, **env):
    environ = {k: v for k, v in GIT_ENV.items() if not k.startswith(("AGENTS_", "DOTAGENTS_"))}
    environ.update(env)
    return subprocess.run([sys.executable, "-I", str(SCRIPT), *args], capture_output=True,
                          text=True, cwd=cwd, env=environ, timeout=120)


def test_help_names_both_subcommands():
    out = run("--help")
    assert out.returncode == 0 and "link" in out.stdout and "sync" in out.stdout


def test_link_symlinks_a_project_into_its_store(tmp_path):
    agents, project = tmp_path / "agents", tmp_path / "proj"
    agents.mkdir()
    project.mkdir()
    out = run("link", str(project), "--agents-dir", str(agents), "--copy")
    assert out.returncode == 0, out.stderr
    assert (project / ".agents").is_dir()
    assert (agents / "projects" / "proj").is_dir()


def test_link_dry_run_writes_nothing(tmp_path):
    agents, project = tmp_path / "agents", tmp_path / "proj"
    agents.mkdir()
    project.mkdir()
    out = run("link", str(project), "--agents-dir", str(agents), "--dry-run")
    assert out.returncode == 0, out.stderr
    assert not (project / ".agents").exists()


def test_sync_runs_the_stores_own_hook(tmp_path):
    if os.name == "nt":
        pytest.skip("the hook is a POSIX script")
    agents = tmp_path / "agents"
    (agents / "hooks").mkdir(parents=True)
    hook = agents / "hooks" / "sync"
    hook.write_text('#!/bin/sh\necho "$2" > "%s"\nexit 5\n' % (tmp_path / "msg"), encoding="utf-8")
    hook.chmod(0o755)
    out = run("sync", "--agents-dir", str(agents), "-m", "hello")
    assert out.returncode == 5, out.stderr
    assert (tmp_path / "msg").read_text(encoding="utf-8").strip() == "hello"
