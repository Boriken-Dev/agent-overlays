"""repo-guard: commit and push under one identity with no agent attribution, deps in step, CI once.

Every test runs on a temporary repository with a local bare remote; `dotagents` is a stand-in that prints
the verdict the test asks for, and the host of `ci` is a fake."""

from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

OVERLAY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OVERLAY.parent / "engineering" / "lib" / "python"))
_spec = importlib.util.spec_from_file_location("repo_guard", OVERLAY / "bin" / "repo_guard.py")
guard = importlib.util.module_from_spec(_spec)
sys.modules["repo_guard"] = guard
_spec.loader.exec_module(guard)

NAME, EMAIL = "Guard Tester", "guard@example.test"
PLAIN = ["-c", "user.name=Someone Else", "-c", "user.email=else@example.test", "-c", "commit.gpgsign=false"]


def git(cwd, *args, check=True):
    done = subprocess.run(["git", "-C", str(cwd), "-c", "core.autocrlf=false", *args], capture_output=True, text=True)
    if check and done.returncode:
        raise AssertionError(done.stderr)
    return done.stdout.strip()


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A bare remote, a working clone with one pushed commit, the machine file, and a leak-check stand-in."""
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(bare), str(work)], check=True, capture_output=True)
    (work / "a.txt").write_text("a\n", encoding="utf-8")
    git(work, "checkout", "-q", "-B", "main")
    git(work, "add", "-A")
    git(work, "-c", f"user.name={NAME}", "-c", f"user.email={EMAIL}", "commit", "-q", "-m", "chore: start")
    git(work, "push", "-q", "origin", "main")
    local = tmp_path / "local.toml"
    local.write_text(f'[identity]\nname = "{NAME}"\nemail = "{EMAIL}"\n', encoding="utf-8")
    monkeypatch.setenv("AGENT_TOOLS_LOCAL", str(local))
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    if os.name == "nt":
        (stubs / "dotagents.cmd").write_text("@echo off\r\necho %LEAK_VERDICT%\r\nif \"%LEAK_VERDICT:~0,4%\"==\"PASS\" (exit /b 0) else (exit /b 1)\r\n", encoding="utf-8")
    else:
        stub = stubs / "dotagents"
        stub.write_text('#!/bin/sh\necho "$LEAK_VERDICT"\ncase "$LEAK_VERDICT" in PASS*) exit 0;; *) exit 1;; esac\n', encoding="utf-8")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(stubs) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("LEAK_VERDICT", "PASS")
    return work, bare


def project_file(work, text):
    (work / ".agents").mkdir(exist_ok=True)
    (work / ".agents" / "agent-tools.toml").write_text(text, encoding="utf-8")


def commit_file(work, name, message, *, plain=False):
    (work / name).write_text(name + "\n", encoding="utf-8")
    git(work, "add", name)
    identity = PLAIN if plain else ["-c", f"user.name={NAME}", "-c", f"user.email={EMAIL}", "-c", "commit.gpgsign=false"]
    git(work, *identity, "commit", "-q", "-m", message)
    return git(work, "rev-parse", "HEAD")


def remote_main(bare):
    return git(bare, "rev-parse", "main")


def main(*argv):
    return guard.main([str(a) for a in argv])


def test_commit_uses_the_identity(world, capsys):
    work, _ = world
    (work / "b.txt").write_text("b\n", encoding="utf-8")
    git(work, "add", "b.txt")
    assert main("commit", "-C", work, "-m", "feat: b\n\nCo-authored-by: Jane Doe <jane@example.test>") == 0
    assert git(work, "log", "-1", "--format=%an <%ae>|%cn <%ce>") == f"{NAME} <{EMAIL}>|{NAME} <{EMAIL}>"


@pytest.mark.parametrize("trailer", [
    "Claude-Session: https://claude.ai/code/session_01",
    "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>",
    "Generated with [Claude Code](https://claude.com/claude-code)",
])
def test_commit_refuses_attribution(world, capsys, trailer):
    work, _ = world
    (work / "b.txt").write_text("b\n", encoding="utf-8")
    git(work, "add", "b.txt")
    before = git(work, "rev-parse", "HEAD")
    assert main("commit", "-C", work, "-m", f"feat: b\n\n{trailer}") == 1
    assert "refused: agent attribution in the commit message" in capsys.readouterr().err
    assert git(work, "rev-parse", "HEAD") == before


def test_commit_without_an_identity_names_the_file_and_key(world, capsys, monkeypatch, tmp_path):
    work, _ = world
    empty = tmp_path / "empty.toml"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("AGENT_TOOLS_LOCAL", str(empty))
    assert main("commit", "-C", work, "-m", "x") == 2
    err = capsys.readouterr().err
    assert "empty.toml" in err and "[identity] name is required" in err


def test_push_publishes_and_fast_forwards_the_checkout(world, tmp_path, capsys):
    work, bare = world
    mirror = tmp_path / "mirror"
    subprocess.run(["git", "clone", "-q", str(bare), str(mirror)], check=True, capture_output=True)
    git(mirror, "checkout", "-q", "main")
    project_file(work, f'[repo_guard]\ncheckout = "{mirror.as_posix()}"\n')
    tip = commit_file(work, "b.txt", "feat: b")
    assert main("push", "-C", work) == 0
    assert remote_main(bare) == tip
    assert git(mirror, "rev-parse", "HEAD") == tip
    out = capsys.readouterr().out
    assert "PASS" in out and "checkout:" in out


def test_push_with_nothing_to_push_still_checks(world, capsys):
    work, bare = world
    before = remote_main(bare)
    assert main("push", "-C", work) == 0
    assert "nothing to push" in capsys.readouterr().out
    assert remote_main(bare) == before


def test_push_refuses_a_commit_by_another_identity_naming_it(world, capsys):
    work, bare = world
    before = remote_main(bare)
    bad = commit_file(work, "b.txt", "feat: b", plain=True)
    assert main("push", "-C", work) == 1
    err = capsys.readouterr().err
    assert bad[:10] in err and "Someone Else" in err
    assert remote_main(bare) == before


def test_push_refuses_a_session_trailer_naming_the_commit(world, capsys):
    work, bare = world
    before = remote_main(bare)
    bad = commit_file(work, "b.txt", "feat: b\n\nClaude-Session: https://claude.ai/code/session_01")
    assert main("push", "-C", work) == 1
    err = capsys.readouterr().err
    assert f"commit {bad[:10]}" in err and "a *-Session: trailer" in err
    assert remote_main(bare) == before


def test_push_allows_a_human_co_author(world):
    work, bare = world
    tip = commit_file(work, "b.txt", "feat: b\n\nCo-authored-by: Jane Doe <jane@example.test>")
    assert main("push", "-C", work) == 0
    assert remote_main(bare) == tip


def test_push_refuses_a_branch_that_is_not_a_fast_forward(world, tmp_path, capsys):
    work, bare = world
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(bare), str(other)], check=True, capture_output=True)
    git(other, "checkout", "-q", "main")
    (other / "x.txt").write_text("x\n", encoding="utf-8")
    git(other, "add", "x.txt")
    git(other, *PLAIN, "commit", "-q", "-m", "other")
    git(other, "push", "-q", "origin", "main")
    commit_file(work, "b.txt", "feat: b")
    assert main("push", "-C", work) == 1
    assert "not a fast-forward" in capsys.readouterr().err


def test_push_refuses_uncommitted_changes(world, capsys):
    work, _ = world
    (work / "a.txt").write_text("changed\n", encoding="utf-8")
    assert main("push", "-C", work) == 1
    assert "uncommitted changes" in capsys.readouterr().err


def test_push_refuses_when_leak_check_fails(world, capsys, monkeypatch):
    work, bare = world
    before = remote_main(bare)
    commit_file(work, "b.txt", "feat: b")
    monkeypatch.setenv("LEAK_VERDICT", "FAIL (1 hits)")
    assert main("push", "-C", work) == 1
    assert "leak-check: FAIL (1 hits)" in capsys.readouterr().err
    assert remote_main(bare) == before


def test_push_refuses_when_the_scan_flags_a_binary(world, capsys):
    work, bare = world
    before = remote_main(bare)
    (work / "tool.exe").write_bytes(b"MZ\x90\x00" + bytes(range(256)) * 8)
    git(work, "add", "tool.exe")
    git(work, "-c", f"user.name={NAME}", "-c", f"user.email={EMAIL}", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "feat: exe")
    assert main("push", "-C", work) == 1
    assert "scan: FAIL" in capsys.readouterr().err
    assert remote_main(bare) == before


def test_push_runs_pre_push_commands(world, capsys):
    work, bare = world
    before = remote_main(bare)
    project_file(work, f'[repo_guard]\npre_push = [["{Path(sys.executable).as_posix()}", "-c", "raise SystemExit(3)"]]\n')
    commit_file(work, "b.txt", "feat: b")
    assert main("push", "-C", work) == 1
    assert "failed (3)" in capsys.readouterr().err
    assert remote_main(bare) == before


def test_push_to_another_branch_and_remote_name(world, tmp_path):
    work, bare = world
    git(work, "remote", "rename", "origin", "upstream")
    git(work, "checkout", "-q", "-B", "trunk")
    git(work, "push", "-q", "upstream", "trunk")
    project_file(work, '[repo_guard]\nmain = "trunk"\nremote = "upstream"\n')
    tip = commit_file(work, "b.txt", "feat: b")
    assert main("push", "-C", work) == 0
    assert git(bare, "rev-parse", "trunk") == tip


# --- deps ---------------------------------------------------------------------------------------------------

BUILD_STUB = '''\
import subprocess, sys
from pathlib import Path
args = sys.argv[1:]
root = Path(args[args.index("--root") + 1])
rest = args[args.index("--root") + 2:]
deps = root.parent / "deps-checkout"
def git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=stub", "-c", "user.email=stub@example.test",
                           "-c", "commit.gpgsign=false", *a], check=True, capture_output=True, text=True).stdout.strip()
if rest[:2] == ["deps", "path"]:
    print(deps)
elif rest[:2] == ["deps", "refresh"]:
    git(deps, "checkout", "-q", "-B", "deps")
    (deps / "crates.txt").write_text(Path(root, "Cargo.lock").read_text())
    git(deps, "add", "-A")
    git(deps, "commit", "-q", "-m", "chore: crates")
    Path(root, "pin.txt").write_text(git(deps, "rev-parse", "HEAD"))
    git(root, "add", "pin.txt")
    git(root, "commit", "-q", "-m", "chore: pin")
    print("pinned deps")
elif rest[0] == "scan":
    print("PASS: 0 flagged, 1 paths scanned")
'''


def deps_world(world, tmp_path):
    work, bare = world
    stub = work / "tools"
    stub.mkdir(exist_ok=True)
    (stub / "build.py").write_text(BUILD_STUB, encoding="utf-8")
    (stub / "build.cfg").write_text('[deps]\ncargo_lock = "Cargo.lock"\n', encoding="utf-8")
    (work / "Cargo.lock").write_text("v1\n", encoding="utf-8")
    project_file(work, '[repo_guard]\nlog = "deps_refreshes.md"\n')
    git(work, "add", "-A", "-f")
    git(work, "-c", f"user.name={NAME}", "-c", f"user.email={EMAIL}", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "chore: tools")
    git(work, "push", "-q", "origin", "main")
    checkout = tmp_path / "deps-checkout"
    subprocess.run(["git", "init", "-q", "-b", "deps", str(checkout)], check=True)
    git(checkout, "remote", "add", "origin", str(bare))
    (checkout / "README").write_text("deps\n", encoding="utf-8")
    git(checkout, "add", "-A")
    git(checkout, "-c", "user.name=stub", "-c", "user.email=stub@example.test", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "chore: start")
    return work, bare, checkout


def test_a_changed_lock_refreshes_deps_then_pushes_both(world, tmp_path, capsys):
    work, bare, checkout = deps_world(world, tmp_path)
    commit = commit_file(work, "Cargo.lock", "chore: lock")
    assert main("push", "-C", work) == 0
    out = capsys.readouterr().out
    assert "pushed deps" in out
    assert git(bare, "rev-parse", "deps") == git(checkout, "rev-parse", "HEAD")
    pin = (work / "pin.txt").read_text(encoding="utf-8").strip()
    assert pin == git(bare, "rev-parse", "deps")
    assert git(bare, "rev-parse", "main") == git(work, "rev-parse", "HEAD") != commit
    line = (work / "deps_refreshes.md").read_text(encoding="utf-8")
    assert line.startswith("- ") and "deps " in line and line.endswith("\n")
    assert git(work, "log", "-1", "--format=%an <%ae>") == f"{NAME} <{EMAIL}>"


def test_no_deps_leaves_the_branch_alone(world, tmp_path):
    work, bare, _ = deps_world(world, tmp_path)
    commit_file(work, "Cargo.lock", "chore: lock")
    assert main("push", "-C", work, "--no-deps") == 0
    assert subprocess.run(["git", "-C", str(bare), "rev-parse", "-q", "--verify", "deps"], capture_output=True).returncode != 0


def test_an_unrelated_change_does_not_touch_deps(world, tmp_path, capsys):
    work, bare, _ = deps_world(world, tmp_path)
    commit_file(work, "b.txt", "feat: b")
    assert main("push", "-C", work) == 0
    assert "deps: no lock file changed" in capsys.readouterr().out


# --- ci -----------------------------------------------------------------------------------------------------


class FakeHost(guard.ReleaseHost):
    def __init__(self, results):
        super().__init__("https://example.test/o/r.git")
        self.results = list(results)
        self.refs = []

    def monitor_ci(self, ref, *, timeout=600):
        self.refs.append(ref)
        return self.results.pop(0)


def tags(path):
    return git(path, "tag", "--list", "ci-*").split()


def run_ci(work, host, jobs, topic="gate", extra=()):
    args = guard.build_parser().parse_args(["ci", "-C", str(work), *extra, topic])
    return guard.cmd_ci(args, host=host, jobs_of=jobs, sleep=lambda s: None)


def test_ci_prints_each_job_and_deletes_the_tag(world, capsys):
    work, bare = world
    host = FakeHost([{"status": "success", "run": {"databaseId": 7}, "message": "CI succeeded (u)"}])
    code = run_ci(work, host, lambda run_id, repo: ("success", [("linux", "success"), ("windows", "success")], ""))
    out = capsys.readouterr().out
    assert code == 0
    assert "  linux: success" in out and "  windows: success" in out
    assert host.refs[0].startswith("ci-gate-") and len(host.refs[0]) == len("ci-gate-") + 14
    assert tags(work) == [] and tags(bare) == []


def test_ci_failure_prints_the_failed_tail_and_still_deletes_the_tag(world, capsys):
    work, bare = world
    host = FakeHost([{"status": "failed", "run": {"databaseId": 8}, "message": "CI failure (u)"}])
    code = run_ci(work, host, lambda run_id, repo: ("failure", [("linux", "failure")], "test x ... FAILED"))
    out = capsys.readouterr().out
    assert code == 1 and "linux: failure" in out and "test x ... FAILED" in out
    assert tags(work) == [] and tags(bare) == []


def test_ci_deletes_the_tag_when_waiting_raises(world):
    work, bare = world

    class Broken(FakeHost):
        def monitor_ci(self, ref, *, timeout=600):
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_ci(work, Broken([]), lambda *a: ("", [], ""))
    assert tags(work) == [] and tags(bare) == []


def test_ci_waits_for_the_run_to_appear(world):
    work, _ = world
    waiting = {"status": "unknown", "message": "No CI runs found for ref 'x'"}
    host = FakeHost([waiting, waiting, {"status": "success", "run": {"databaseId": 1}, "message": "ok"}])
    assert run_ci(work, host, lambda *a: ("success", [], "")) == 0
    assert len(host.refs) == 3


def test_ci_gives_up_after_the_find_timeout(world, capsys):
    work, bare = world
    waiting = {"status": "unknown", "message": "No CI runs found for ref 'x'"}
    host = FakeHost([waiting] * 50)
    assert run_ci(work, host, lambda *a: ("", [], ""), extra=["--find-timeout", "10", "--every", "5"]) == 1
    assert len(host.refs) == 3
    assert tags(bare) == []
