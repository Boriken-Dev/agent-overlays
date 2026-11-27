"""The turn-end sync runs in the background: the hook returns at once, one sync runs per store at a
time, a failure is reported by the next hook, git runs without auto-maintenance, and every linked
store with work to save is synced."""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

import _bgsync
import _link
from _link import link_project

ROOT = Path(__file__).resolve().parents[1]
SH = shutil.which("sh")
posix_only = pytest.mark.skipif(SH is None or os.name == "nt", reason="the hook scripts are POSIX shell")
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def git(cwd, *args):
    res = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    for name in _bgsync.SETTINGS:
        monkeypatch.delenv("PRIVATE_SYNC_" + name.upper(), raising=False)
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.delenv("DOTAGENTS_AGENTS_TOKEN", raising=False)
    monkeypatch.delenv("AGENTS_STORE_LAYOUT", raising=False)
    monkeypatch.delenv("AGENTS_STORE_DIR", raising=False)
    saved = (_link.git_timeout, _link.step_hook)
    yield
    _link.git_timeout, _link.step_hook = saved


# --- settings, the lock, the requests ---------------------------------------------------------


def test_settings_come_from_the_environment_then_the_file_then_the_defaults(tmp_path, monkeypatch):
    assert _bgsync.settings(tmp_path) == _bgsync.DEFAULTS
    (tmp_path / "private-sync.cfg").write_text("# a comment\ninterval = 7\ngit_timeout = 3 # too low\n"
                                               "background = false\nstale = soon\n", encoding="utf-8")
    cfg = _bgsync.settings(tmp_path)
    assert cfg["interval"] == 7 and cfg["background"] == 0
    assert cfg["git_timeout"] == _bgsync.DEFAULTS["git_timeout"], "out of bounds is ignored"
    assert cfg["stale"] == _bgsync.DEFAULTS["stale"], "not a number is ignored"
    monkeypatch.setenv("PRIVATE_SYNC_INTERVAL", "11")
    assert _bgsync.settings(tmp_path)["interval"] == 11


def test_the_lock_is_single_flight_and_a_stale_one_is_taken_over(tmp_path):
    state = _bgsync._ensure(tmp_path)
    assert _bgsync.acquire(state, 900)
    assert not _bgsync.acquire(state, 900), "a second runner coalesces"
    assert _bgsync.lock_is_fresh(state, 900)
    old = time.time() - 2000
    os.utime(str(state / "sync.lock"), (old, old))
    assert not _bgsync.lock_is_fresh(state, 900)
    assert _bgsync.acquire(state, 900), "its holder made no progress"
    _bgsync.release(state)
    assert not (state / "sync.lock").exists()


def test_requests_for_one_project_coalesce_into_one(tmp_path):
    state = _bgsync._ensure(tmp_path)
    _bgsync.add_pending(state, str(tmp_path / "a"), "first")
    _bgsync.add_pending(state, str(tmp_path / "a"), "second")
    _bgsync.add_pending(state, str(tmp_path / "b"), "other")
    taken = _bgsync.take_pending(state)
    assert sorted(item["message"] for item in taken) == ["other", "second"]
    assert not _bgsync.has_pending(state)


def test_a_log_line_never_carries_a_credential(monkeypatch):
    monkeypatch.setenv("DOTAGENTS_AGENTS_TOKEN", "s3cr3t-value")
    text = _bgsync.redact("fatal: https://x-access-token:abc@github.com/o/r s3cr3t-value")
    assert "abc" not in text and "s3cr3t-value" not in text


# --- git runs without auto-maintenance, and ends at its timeout -------------------------------


def test_git_runs_with_auto_maintenance_off(tmp_path, monkeypatch):
    seen = []
    real = subprocess.Popen

    def spy(command, *a, **kw):
        seen.append(list(command))
        return real(command, *a, **kw)

    monkeypatch.setattr(_link.subprocess, "Popen", spy)
    _link._git(tmp_path, "status")
    _link._git(tmp_path, "gc", "--auto", maintenance=True)
    assert seen[0][:5] == ["git", "-c", "gc.auto=0", "-c", "maintenance.auto=false"]
    assert "gc.auto=0" not in seen[1], "the explicit maintenance step is the one that may gc"


def test_a_git_call_past_its_timeout_is_ended_and_reported(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    _link.git_timeout = 1
    started = time.monotonic()
    res = _link._git(tmp_path, "slow", capture=True, config_args=["-c", "alias.slow=!sleep 30"])
    assert res.returncode == 124 and "timed out" in res.stderr
    assert time.monotonic() - started < 15


# --- the passes, on real repositories ---------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    agents = tmp_path / "agents"
    subprocess.run(["git", "clone", "-q", str(origin), str(agents)], check=True)
    (agents / "AGENTS.md").write_text("global\n", encoding="utf-8")
    git(agents, "add", "-A")
    git(agents, "commit", "-q", "-m", "init")
    git(agents, "push", "-q", "-u", "origin", "main")
    return origin, agents


def linked(tmp_path, agents, name):
    project = tmp_path / name
    project.mkdir()
    (project / ".gitignore").write_text(".agents\n", encoding="utf-8")
    link_project(project, agents)
    return project


def pushed(origin, branch, path):
    return subprocess.run(["git", "-C", str(origin), "cat-file", "-e", "%s:%s" % (branch, path)],
                          capture_output=True).returncode == 0


def run_pass(agents, project, monkeypatch):
    monkeypatch.setenv("PRIVATE_SYNC_INTERVAL", "0")
    state = _bgsync._ensure(agents)
    _bgsync.add_pending(state, str(project), "sync: test")
    assert _bgsync.acquire(state, 900)
    try:
        return _bgsync.drain(agents, _link)
    finally:
        _bgsync.release(state)


def test_a_pass_syncs_the_named_project_and_every_other_store_with_work(tmp_path, store, monkeypatch):
    origin, agents = store
    named = linked(tmp_path, agents, "named")
    other = linked(tmp_path, agents, "other")
    clean = linked(tmp_path, agents, "clean")
    (named / ".agents" / "a.md").write_text("a\n", encoding="utf-8")
    (other / ".agents" / "b.md").write_text("b\n", encoding="utf-8")
    assert run_pass(agents, named, monkeypatch) == 0
    assert pushed(origin, "named", "a.md")
    assert pushed(origin, "other", "b.md"), "a store the session never named, but wrote to"
    assert not pushed(origin, "clean", "a.md")
    log = (_bgsync.state_dir(agents) / "sync.log").read_text(encoding="utf-8")
    assert "git pull" in log and "git push" in log and "rc=0" in log, "each step is timed"
    assert not (_bgsync.state_dir(agents) / "failure.json").exists()
    assert not (_bgsync.state_dir(agents) / "sync.lock").exists()


def test_a_push_that_raced_another_is_rebased_and_retried(tmp_path, store, monkeypatch):
    origin, agents = store
    project = linked(tmp_path, agents, "app")
    (project / ".agents" / "mine.md").write_text("m\n", encoding="utf-8")
    run_pass(agents, project, monkeypatch)
    # Another machine pushes the branch between this one's pull and push: the first push is rejected.
    other = tmp_path / "other-clone"
    subprocess.run(["git", "clone", "-q", "-b", "app", str(origin), str(other)], check=True)
    (other / "theirs.md").write_text("t\n", encoding="utf-8")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "theirs")
    git(other, "push", "-q", "origin", "app")
    (project / ".agents" / "mine2.md").write_text("m2\n", encoding="utf-8")
    git(project / ".agents", "add", "-A")
    git(project / ".agents", "commit", "-q", "-m", "mine2")
    res = _link._push_retrying(project / ".agents", ("push", "-u", "origin", "app"),
                               ("pull", "--rebase", "--autostash", "origin", "app"),
                               config_args=[], auth_env=None, log=lambda *a: None)
    assert res.returncode == 0, res.stderr
    assert pushed(origin, "app", "theirs.md") and pushed(origin, "app", "mine2.md")


def test_a_failed_sync_leaves_a_failure_the_next_hook_reports_once(tmp_path, store, monkeypatch):
    origin, agents = store
    project = linked(tmp_path, agents, "app")
    (project / ".agents" / "a.md").write_text("a\n", encoding="utf-8")
    git(project / ".agents", "remote", "set-url", "origin", str(tmp_path / "no-such-remote.git"))
    assert run_pass(agents, project, monkeypatch) == 1
    state = _bgsync.state_dir(agents)
    assert (state / "failure.json").exists()
    said = []
    _bgsync.report_failure(state, lambda msg, *a: said.append(msg % a))
    _bgsync.report_failure(state, lambda msg, *a: said.append(msg % a))
    assert len(said) == 1 and "did not finish" in said[0]
    # The next pass that works clears it.
    git(project / ".agents", "remote", "set-url", "origin", str(origin))
    assert run_pass(agents, project, monkeypatch) == 0
    assert not (state / "failure.json").exists()


def test_maintenance_prunes_worktree_metadata_and_never_deletes_a_directory(tmp_path, store, monkeypatch):
    origin, agents = store
    gone = linked(tmp_path, agents, "gone")
    kept = linked(tmp_path, agents, "kept")
    shutil.rmtree(str(gone / ".agents"))
    assert "gone" in git(agents, "worktree", "list")
    monkeypatch.setenv("PRIVATE_SYNC_INTERVAL", "0")
    state = _bgsync._ensure(agents)
    (state / "maintain.request").write_text("", encoding="utf-8")
    assert _bgsync.acquire(state, 900)
    try:
        _bgsync.drain(agents, _link)
    finally:
        _bgsync.release(state)
    assert "gone" not in git(agents, "worktree", "list")
    assert (kept / ".agents").is_dir()
    assert not (state / "maintain.request").exists()
    assert _bgsync._read_json(state / "last.json")["maintenance"] > 0


def test_maintenance_is_asked_for_only_when_due(tmp_path, monkeypatch):
    spawned = []
    monkeypatch.setattr(_bgsync, "_spawn", lambda *a, **k: spawned.append(a) or True)
    log = lambda *a: None
    _bgsync.session_start(tmp_path, Path("run_link.py"), log)
    state = _bgsync.state_dir(tmp_path)
    assert (state / "maintain.request").exists() and len(spawned) == 1
    (state / "maintain.request").unlink()
    _bgsync._write_json(state / "last.json", {"maintenance": time.time()})
    _bgsync.session_start(tmp_path, Path("run_link.py"), log)
    assert not (state / "maintain.request").exists() and len(spawned) == 1


# --- the hook: returns at once, one sync at a time --------------------------------------------

#: Stands in for _link: a sync that takes time and can fail, recorded with its start and end.
SLOW_LINK = '''\
import os, time

git_timeout = None
step_hook = None

def _log(text):
    with open(os.environ["LINK_LOG"], "a") as fh:
        fh.write(text + "\\n")

def link_project(project, agents, **kw):
    pass

def sync_agents(agents, *, message="", project_dir=None, logger=None, **kw):
    _log("start %s" % os.path.basename(str(project_dir)))
    time.sleep(float(os.environ.get("LINK_SLEEP", "0")))
    if os.environ.get("LINK_FAIL") and logger is not None:
        logger.info("WARN: git push failed: boom")
    _log("end")
    return 0
'''


def stop_hook(tmp_path, project, **extra):
    overlay = tmp_path / "overlay"
    if not overlay.exists():
        shutil.copytree(str(ROOT / "hooks"), str(overlay / "hooks"))
        (overlay / "lib" / "python").mkdir(parents=True)
        shutil.copy(str(ROOT / "lib" / "python" / "_bgsync.py"), str(overlay / "lib" / "python" / "_bgsync.py"))
        (overlay / "lib" / "python" / "_link.py").write_text(SLOW_LINK, encoding="utf-8")
    agents = tmp_path / "agents-home"
    agents.mkdir(exist_ok=True)
    (agents / ".git").mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in ("AGENTS_PROJECT_ROOT",)}
    env.update(AGENTS_HOME=str(agents), CLAUDE_PROJECT_DIR=str(project), AGENTS_PYTHON=sys.executable,
               LINK_LOG=str(tmp_path / "calls.log"), PRIVATE_SYNC_INTERVAL="0",
               PRIVATE_SYNC_SWEEP_INTERVAL="0", PRIVATE_SYNC_MAINTENANCE_INTERVAL="0", **extra)
    started = time.monotonic()
    done = subprocess.run([SH, str(overlay / "hooks" / "private-sync-stop.sh")], input=b"{}", env=env,
                          capture_output=True, cwd=str(tmp_path), timeout=60)
    assert done.returncode == 0, done.stderr
    assert done.stdout == b"", "a hook's standard output is its answer"
    return time.monotonic() - started, done.stderr.decode(), agents


def wait_for(predicate, seconds=30):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def calls(tmp_path):
    path = tmp_path / "calls.log"
    return path.read_text(encoding="utf-8").split() if path.exists() else []


@posix_only
def test_the_hook_returns_at_once_and_the_sync_finishes_after_it(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    took, err, agents = stop_hook(tmp_path, project, LINK_SLEEP="4")
    assert took < 3, "the hook waited for the sync (%.1fs)" % took
    assert "background" in err
    assert wait_for(lambda: "end" in calls(tmp_path)), "the background sync never completed"
    assert calls(tmp_path)[:2] == ["start", "proj"]
    assert wait_for(lambda: not (_bgsync.state_dir(agents) / "sync.lock").exists())


@posix_only
def test_turns_that_end_during_a_sync_join_its_next_pass_and_never_run_beside_it(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    stop_hook(tmp_path, project, LINK_SLEEP="3")
    assert wait_for(lambda: "start" in calls(tmp_path)), "the first sync never started"
    for _ in range(3):
        took, _err, agents = stop_hook(tmp_path, project, LINK_SLEEP="3")
        assert took < 2.5
    assert wait_for(lambda: len([c for c in calls(tmp_path) if c == "end"]) >= 2, 40)
    assert wait_for(lambda: not (_bgsync.state_dir(agents) / "sync.lock").exists())
    # One runner at a time (start/end alternate), and the three later turns made one more pass.
    kinds = [line.split()[0] for line in (tmp_path / "calls.log").read_text(encoding="utf-8").splitlines()]
    assert kinds == ["start", "end", "start", "end"], kinds


@posix_only
def test_a_background_failure_is_reported_by_the_next_turn(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    _, _, agents = stop_hook(tmp_path, project, LINK_FAIL="1")
    state = _bgsync.state_dir(agents)
    assert wait_for(lambda: (state / "failure.json").exists())
    assert wait_for(lambda: not (state / "sync.lock").exists())
    _, err, _ = stop_hook(tmp_path, project, LINK_FAIL="1")
    assert "did not finish" in err
    assert err.count("did not finish") == 1


@posix_only
def test_with_background_off_the_hook_syncs_before_it_returns(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    stop_hook(tmp_path, project, PRIVATE_SYNC_BACKGROUND="0")
    assert calls(tmp_path)[0] == "start" and "end" in calls(tmp_path)


def test_the_overlay_ships_the_background_library_and_the_hook_calls_it():
    assert (ROOT / "lib" / "python" / "_bgsync.py").is_file()
    stop = (ROOT / "hooks" / "private-sync-stop.sh").read_text(encoding="utf-8")
    assert "run_link.py\" turn-end" in stop
    start = (ROOT / "hooks" / "private-sync-start.sh").read_text(encoding="utf-8")
    assert "gc.auto=0" in start and "maintenance.auto=false" in start
