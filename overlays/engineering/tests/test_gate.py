"""`dotagents gate`: steps through summarize-run, the lock, `when` globs, the Linux leg and the merge.

Every test runs on a temporary repository; the Linux distribution is a pass-through to a local bash and the
push is a recording stand-in for repo-guard."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import _gate
import _slots
import agent_tools

duho = pytest.importorskip("duho")

MODULE = Path(__file__).resolve().parents[1] / "cmds" / "gate.py"
_spec = importlib.util.spec_from_file_location("gate_cmd", MODULE)
gate_cmd = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = gate_cmd
_spec.loader.exec_module(gate_cmd)

PY = sys.executable
IDENT = ["-c", "user.name=T", "-c", "user.email=t@example.test", "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false"]


def git(cwd, *args, check=True):
    done = subprocess.run(["git", "-C", str(cwd), *IDENT, *args], capture_output=True, text=True)
    if check and done.returncode:
        raise AssertionError(done.stderr)
    return done.stdout.strip()


def toml_value(value):
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{k} = {toml_value(v)}" for k, v in value.items()) + " }"
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(value)


def write_project(repo, steps=None, **gate):
    table = dict(gate)
    if steps is not None:
        table["steps"] = steps
    lines = ["[gate]"] + [f"{k} = {toml_value(v)}" for k, v in table.items()]
    (repo / ".agents").mkdir(exist_ok=True)
    (repo / ".agents" / "agent-tools.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def py_step(name, code, **extra):
    return {"name": name, "run": [PY, "-c", code], **extra}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A work tree on main with an `origin` remote holding the same commit, and a machine file whose lock is the test's."""
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(bare), str(work)], check=True, capture_output=True)
    git(work, "checkout", "-q", "-B", "main")
    (work / "src").mkdir()
    (work / "src" / "a.txt").write_text("a\n", encoding="utf-8")
    (work / "docs").mkdir()
    (work / "docs" / "d.md").write_text("d\n", encoding="utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "chore: start")
    git(work, "push", "-q", "origin", "main")
    local = tmp_path / "local.toml"
    local.write_text(f'[gate]\nlock = {json.dumps(str(tmp_path / "gate.lock"))}\n', encoding="utf-8")
    monkeypatch.setenv("AGENT_TOOLS_LOCAL", str(local))
    return work


def run(repo, out, **kw):
    lines = []
    kw.setdefault("lock_every", 0.05)
    ok = _gate.run_gate(repo, out=out, say=lines.append, **kw)
    return ok, lines


# --- globs and the changed paths ------------------------------------------------------------------------------


@pytest.mark.parametrize("pattern, path, hit", [
    ("docs/**", "docs/a/b.md", True),
    ("docs/**", "src/docs/a.md", False),
    ("**/*.rs", "crates/x/src/lib.rs", True),
    ("**/*.rs", "lib.rs", True),
    ("*.md", "docs/a.md", False),
    ("*.md", "README.md", True),
    ("clients/app/", "clients/app/lib/x.dart", True),
    ("Cargo.lock", "Cargo.lock", True),
    ("Cargo.lock", "x/Cargo.lock", False),
    ("a?c", "abc", True),
    ("a?c", "a/c", False),
])
def test_globs(pattern, path, hit):
    assert bool(_gate.glob_regex(pattern).match(path)) is hit


# --- run ------------------------------------------------------------------------------------------------------


def test_every_step_passes_and_leaves_a_log_and_a_verdict(repo, tmp_path):
    write_project(repo, [py_step("one", "print('hello one')"), py_step("two", "print('hello two')")])
    ok, lines = run(repo, tmp_path / "out")
    assert ok and lines[:2] == ["PASS one", "PASS two"]
    assert "hello one" in (tmp_path / "out" / "one.log").read_text(encoding="utf-8")
    assert (tmp_path / "out" / "one.verdict").read_text(encoding="utf-8").startswith("=== PASS")
    assert not (tmp_path / "gate.lock").exists()


def test_a_failed_step_fails_the_gate_and_the_rest_still_run(repo, tmp_path):
    write_project(repo, [py_step("bad", "import sys; print('boom'); sys.exit(4)"), py_step("good", "pass")])
    ok, lines = run(repo, tmp_path / "out")
    assert not ok
    assert lines[0].startswith("FAIL bad") and lines[1] == "PASS good"
    assert "(exit 4)" in (tmp_path / "out" / "bad.verdict").read_text(encoding="utf-8")


def test_fail_fast_stops_at_the_first_failure(repo, tmp_path):
    write_project(repo, [py_step("bad", "raise SystemExit(1)"), py_step("good", "pass")])
    ok, lines = run(repo, tmp_path / "out", fail_fast=True)
    assert not ok and not any("good" in line for line in lines)


def test_only_the_named_steps_run(repo, tmp_path):
    write_project(repo, [py_step("a", "pass"), py_step("b", "raise SystemExit(1)")])
    ok, lines = run(repo, tmp_path / "out", only=["a"])
    assert ok and lines[0] == "PASS a"
    with pytest.raises(_gate.GateError, match="no step named zzz"):
        run(repo, tmp_path / "out", only=["zzz"])


def test_a_project_without_steps_names_the_file_and_key(repo, tmp_path):
    with pytest.raises(_gate.GateError, match=r"agent-tools.toml: \[gate\] steps is required"):
        run(repo, tmp_path / "out")


def test_env_cwd_and_worktree_are_substituted(repo, tmp_path):
    code = ("import os, pathlib; "
            "pathlib.Path('seen.txt').write_text(os.environ['WHERE'] + '|' + os.getcwd())")
    write_project(repo, [py_step("env", code, env={"WHERE": "{worktree}/x"}, cwd="src")])
    ok, _ = run(repo, tmp_path / "out")
    seen = (repo / "src" / "seen.txt").read_text(encoding="utf-8")
    assert ok and seen.startswith(str(repo.resolve()) + "/x|") and seen.endswith("src")


def test_restore_checks_a_file_out_again(repo, tmp_path):
    code = "open('src/a.txt', 'w').write('changed')"
    write_project(repo, [py_step("edit", code)], restore=["src/a.txt"])
    ok, _ = run(repo, tmp_path / "out")
    assert ok and (repo / "src" / "a.txt").read_text(encoding="utf-8") == "a\n"


def test_when_runs_a_step_only_for_a_matching_changed_path(repo, tmp_path):
    (repo / "src" / "b.txt").write_text("b\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "feat: b")
    write_project(repo, [py_step("rust", "pass", when=["src/**"]), py_step("docs", "pass", when=["docs/**"]),
                         py_step("always", "pass")])
    ok, lines = run(repo, tmp_path / "out")
    assert ok
    assert lines[0] == "PASS rust" and lines[1].startswith("SKIP docs") and lines[2] == "PASS always"


def test_when_runs_everything_without_a_remote_main(repo, tmp_path):
    git(repo, "remote", "remove", "origin")
    write_project(repo, [py_step("docs", "pass", when=["docs/**"])])
    ok, lines = run(repo, tmp_path / "out")
    assert ok and lines[0] == "PASS docs"


def test_a_tree_in_the_middle_of_a_merge_is_refused(repo, tmp_path):
    git(repo, "checkout", "-q", "-b", "side")
    (repo / "src" / "a.txt").write_text("side\n", encoding="utf-8")
    git(repo, "commit", "-q", "-am", "side")
    git(repo, "checkout", "-q", "main")
    (repo / "src" / "a.txt").write_text("main\n", encoding="utf-8")
    git(repo, "commit", "-q", "-am", "main")
    git(repo, "merge", "side", check=False)
    write_project(repo, [py_step("a", "pass")])
    with pytest.raises(_gate.GateError, match="in the middle of a merge"):
        run(repo, tmp_path / "out")


def test_the_gate_waits_while_another_process_holds_the_lock(repo, tmp_path):
    write_project(repo, [py_step("a", "pass")])
    path = tmp_path / "gate.lock"
    path.mkdir()
    (path / "holder").write_text("shell label pid 1 2026-01-01T00:00:00Z\n", encoding="utf-8")

    def leave():
        (path / "holder").unlink()
        path.rmdir()

    threading.Timer(0.5, leave).start()
    started = time.monotonic()
    ok, _ = run(repo, tmp_path / "out")
    assert ok and time.monotonic() - started >= 0.4
    assert not path.exists()


def test_the_lock_is_held_while_a_step_runs_and_freed_after_a_failure(repo, tmp_path):
    lock = tmp_path / "gate.lock"
    code = f"import os, sys; sys.exit(0 if os.path.isdir({str(lock)!r}) and os.path.isfile({str(lock / 'holder')!r}) else 9)"
    write_project(repo, [py_step("holds", code), py_step("bad", "raise SystemExit(1)")])
    ok, lines = run(repo, tmp_path / "out")
    assert lines[0] == "PASS holds" and not ok
    assert not lock.exists()


def test_a_step_with_slot_waits_for_a_free_slot(repo, tmp_path):
    write_project(repo, [py_step("heavy", "pass", slot=True)])
    state = {"busy": 2}

    def lister():
        state["busy"] = max(0, state["busy"] - 1)
        return ["one"] * state["busy"]

    config = _slots.SlotConfig(dir=tmp_path / "slots", limit=1)
    slot = _slots.Slot(config, lister=lister, say=lambda m: None)
    ok, lines = run(repo, tmp_path / "out", slot=slot, slot_options={"every": 0.05, "seconds": 0.1, "poll": 0.05})
    assert ok and lines[0] == "PASS heavy"
    assert not (tmp_path / "slots" / "gate").exists()


def test_the_command_line_runs_the_gate(repo, tmp_path, capsys):
    write_project(repo, [py_step("a", "pass")])
    command = duho.parse(gate_cmd.Gate.Run, [str(repo), "--out", str(tmp_path / "out"), "--lock-every", "0.05"])
    assert command() == 0
    assert "PASS a" in capsys.readouterr().out


def test_the_command_line_reports_a_missing_key_with_exit_2(repo, tmp_path, capsys):
    command = duho.parse(gate_cmd.Gate.Run, [str(repo), "--out", str(tmp_path / "out")])
    assert command() == 2
    assert "[gate] steps is required" in capsys.readouterr().err


def test_gate_lock_runs_a_command_under_the_lock(tmp_path, repo):
    lock = tmp_path / "gate.lock"
    code = f"import os, sys; sys.exit(0 if os.path.isdir({str(lock)!r}) else 9)"
    command = duho.parse(gate_cmd.Gate.Lock, ["label", "--lock-every", "0.05", "--", PY, "-c", code])
    assert command() == 0
    assert not lock.exists()
    command = duho.parse(gate_cmd.Gate.Lock, ["label", "--", PY, "-c", "raise SystemExit(5)"])
    assert command() == 5 and not lock.exists()


# --- wsl --------------------------------------------------------------------------------------------------------


def wsl_config(tmp_path, steps, **wsl):
    local = Path(os.environ["AGENT_TOOLS_LOCAL"])
    runner = tmp_path / "runner.py"
    runner.write_text("import os, subprocess, sys\n"
                      "argv = sys.argv[1:]\nargv[0] = os.environ.get('BASH_FOR_TEST', 'bash')\n"
                      "sys.exit(subprocess.run(argv).returncode)\n", encoding="utf-8")
    table = {"exec": [PY, str(runner)], **wsl}
    body = local.read_text(encoding="utf-8") + "[wsl]\n" + "".join(f"{k} = {toml_value(v)}\n" for k, v in table.items())
    local.write_text(body, encoding="utf-8")
    return steps


def test_the_script_unpacks_runs_each_step_and_caps_the_target(repo, tmp_path):
    steps = [{"name": "clippy", "run": "cargo clippy -- -D warnings", "env": {"A": "b c"}},
             {"name": "t", "run": ["cargo", "test", "-p", "x y"], "cwd": "sub"}]
    write_project(repo, [py_step("x", "pass")], wsl_steps=steps)
    config = agent_tools.load(repo, local=Path(os.environ["AGENT_TOOLS_LOCAL"]))
    script = _gate.wsl_script(config, "work", steps, "/mnt/c/a/src.tar", "/mnt/c/a")
    assert "-C \"$CLONE\" < /mnt/c/a/src.tar" in script
    assert "-gt 40" in script and "CARGO_BUILD_JOBS=4" in script
    assert "export A='b c'" in script and "cargo test -p 'x y'" in script
    assert script.count('"$CLONE"/sub') == 1
    assert "/mnt/c/a/wsl_clippy.log" in script


def test_the_distribution_is_a_required_setting(repo):
    write_project(repo, [py_step("x", "pass")], wsl_steps=[{"name": "a", "run": "true"}])
    with pytest.raises(agent_tools.ConfigError, match=r"\[wsl\] distro is required"):
        _gate.wsl_prefix(agent_tools.load(repo, local=Path(os.environ["AGENT_TOOLS_LOCAL"])))


def test_the_default_prefix_runs_wsl_exe_in_the_distribution(repo, tmp_path):
    local = Path(os.environ["AGENT_TOOLS_LOCAL"])
    local.write_text(local.read_text(encoding="utf-8") + '[wsl]\ndistro = "Box"\n', encoding="utf-8")
    config = agent_tools.load(repo, local=local)
    assert _gate.wsl_prefix(config) == ["wsl.exe", "-d", "Box", "--"]
    assert _gate.wsl_prefix(config, root=True) == ["wsl.exe", "-d", "Box", "-u", "root", "--"]


def find_bash():
    found = shutil.which("bash")
    if not found:
        return None
    try:
        done = subprocess.run([found, "-c", "echo ok"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return found if done.stdout.strip() == "ok" else None


def test_the_linux_leg_runs_on_an_archive_and_reports_each_step(repo, tmp_path, monkeypatch):
    bash = find_bash()
    if bash is None:
        pytest.skip("no usable bash on this machine")
    monkeypatch.setenv("BASH_FOR_TEST", bash)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    # Git for Windows' bash reads C:/x paths; the distribution's `/mnt/c/x` form is its own.
    monkeypatch.setattr(_gate, "wsl_path", lambda p: str(p.resolve()).replace("\\", "/"))
    steps = [{"name": "has_files", "run": "test -f src/a.txt && echo built > built.txt"},
             {"name": "target", "run": 'test "$CARGO_TARGET_DIR" = "$HOME/gate_work-target" && test "$CARGO_BUILD_JOBS" = 2'},
             {"name": "bad", "run": "exit 3"}]
    wsl_config(tmp_path, steps, jobs=2)
    write_project(repo, [py_step("x", "pass")], wsl_steps=steps)
    git(repo, "add", "-A", "-f")
    git(repo, "commit", "-q", "-m", "chore: config")
    lines = []
    ok = _gate.run_wsl(repo, out=tmp_path / "out", say=lines.append, lock_every=0.05)
    assert not ok
    assert lines[:3] == ["PASS wsl:has_files", "PASS wsl:target", lines[2]] and lines[2].startswith("FAIL wsl:bad")
    assert not (tmp_path / "home" / "gate_work").exists()  # the clone is removed, the target is kept
    assert not (tmp_path / "out" / "src.tar").exists()
    assert not (tmp_path / "gate.lock").exists()


# --- merge ------------------------------------------------------------------------------------------------------

STUB_GUARD = ("import pathlib, sys\n"
              "pathlib.Path(sys.argv[0]).with_name('pushed.txt').write_text(' '.join(sys.argv[1:]))\n")


def branch_commit(repo, branch, message="feat: b", base="main"):
    git(repo, "checkout", "-q", "-b", branch, base)
    (repo / f"{branch}.txt").write_text(branch + "\n", encoding="utf-8")
    git(repo, "add", f"{branch}.txt")
    git(repo, "commit", "-q", "-m", message)
    git(repo, "checkout", "-q", "main")


@pytest.fixture
def guard(tmp_path):
    script = tmp_path / "guard" / "stub.py"
    script.parent.mkdir()
    script.write_text(STUB_GUARD, encoding="utf-8")
    return lambda: [PY, str(script)], script.with_name("pushed.txt")


def merge(repo, branch, tmp_path, guard, **kw):
    lines = []
    ok = _gate.merge_branch(repo, branch, out=tmp_path / "out", say=lines.append, lock_every=0.05,
                            guard_command=guard[0], **kw)
    return ok, lines


def test_merge_fast_forwards_runs_the_gate_and_pushes_through_the_guard(repo, tmp_path, guard):
    write_project(repo, [py_step("a", "pass")])
    git(repo, "add", "-A", "-f")
    git(repo, "commit", "-q", "-m", "chore: config")
    branch_commit(repo, "feat")
    ok, lines = merge(repo, "feat", tmp_path, guard)
    assert ok and "PASS a" in lines
    assert git(repo, "rev-parse", "main") == git(repo, "rev-parse", "feat")
    assert guard[1].read_text(encoding="utf-8") == f"push -C {repo.resolve()}"


def test_merge_stops_before_pushing_when_a_step_fails(repo, tmp_path, guard):
    write_project(repo, [py_step("a", "raise SystemExit(1)")])
    git(repo, "add", "-A", "-f")
    git(repo, "commit", "-q", "-m", "chore: config")
    git(repo, "push", "-q", "origin", "main")
    branch_commit(repo, "feat")
    before = git(repo, "rev-parse", "main")
    ok, lines = merge(repo, "feat", tmp_path, guard)
    assert not ok and not guard[1].exists()
    assert any(f"git reset --hard {before}" in line for line in lines)


def test_merge_no_push(repo, tmp_path, guard):
    write_project(repo, [py_step("a", "pass")])
    git(repo, "add", "-A", "-f")
    git(repo, "commit", "-q", "-m", "chore: config")
    branch_commit(repo, "feat")
    ok, lines = merge(repo, "feat", tmp_path, guard, push=False)
    assert ok and not guard[1].exists() and any("not pushed" in line for line in lines)


def test_merge_refuses_a_branch_that_is_not_a_fast_forward(repo, tmp_path, guard):
    branch_commit(repo, "feat")
    (repo / "m.txt").write_text("m\n", encoding="utf-8")
    git(repo, "add", "m.txt")
    git(repo, "commit", "-q", "-m", "feat: m")
    before = git(repo, "rev-parse", "main")
    with pytest.raises(_gate.GateError, match="not a fast-forward"):
        merge(repo, "feat", tmp_path, guard)
    assert git(repo, "rev-parse", "main") == before


def test_merge_refuses_agent_attribution_naming_the_commit(repo, tmp_path, guard):
    branch_commit(repo, "feat", "feat: b\n\nClaude-Session: https://claude.ai/code/session_01")
    sha = git(repo, "rev-parse", "feat")
    before = git(repo, "rev-parse", "main")
    with pytest.raises(_gate.GateError, match=sha[:10]):
        merge(repo, "feat", tmp_path, guard)
    assert git(repo, "rev-parse", "main") == before


def test_merge_allows_a_human_co_author(repo, tmp_path, guard):
    write_project(repo, [py_step("a", "pass")])
    git(repo, "add", "-A", "-f")
    git(repo, "commit", "-q", "-m", "chore: config")
    branch_commit(repo, "feat", "feat: b\n\nCo-authored-by: Jane Doe <jane@example.test>")
    ok, _ = merge(repo, "feat", tmp_path, guard)
    assert ok


def test_merge_needs_main_checked_out(repo, tmp_path, guard):
    branch_commit(repo, "feat")
    git(repo, "checkout", "-q", "feat")
    with pytest.raises(_gate.GateError, match="is on feat, not main"):
        merge(repo, "feat", tmp_path, guard)
