"""`dotagents build-slot`: a heavy build starts only while a slot is free, and the limit file, the gate directory and
the lock directory are the ones the earlier `build_slot.py` script and the shell gates used, so old and new
callers can run at the same time."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tools_env import no_machine_agent_files  # noqa: F401  (an autouse fixture)

import _slots

duho = pytest.importorskip("duho")

MODULE = Path(__file__).resolve().parents[1] / "cmds" / "build_slot.py"
_spec = importlib.util.spec_from_file_location("build_slot_cmd", MODULE)
build_slot = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = build_slot
_spec.loader.exec_module(build_slot)

# The gate protocol of the earlier script, verbatim in behaviour: `mkdir <dir>/gate`, a `pid` file, takeover of a
# gate whose holder exited, release only by the pid that wrote it. `hold` takes it and sleeps; `leave` takes it and
# exits without releasing; `try` takes it once and says whether it did.
LEGACY = '''
import ctypes, os, pathlib, sys, time
GATE = pathlib.Path(os.environ.get("LOCALAPPDATA", str(pathlib.Path.home()))) / "build_slot" / "gate"

def alive(pid):
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    kernel = ctypes.windll.kernel32
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    code = ctypes.c_ulong()
    ok = kernel.GetExitCodeProcess(handle, ctypes.byref(code))
    kernel.CloseHandle(handle)
    return bool(ok) and code.value == 259

def take_gate():
    GATE.parent.mkdir(parents=True, exist_ok=True)
    try:
        GATE.mkdir()
    except FileExistsError:
        try:
            holder = int((GATE / "pid").read_text())
        except (OSError, ValueError):
            if time.time() - GATE.stat().st_mtime > 30:
                return take_over()
            return False
        return take_over() if not alive(holder) else False
    (GATE / "pid").write_text(str(os.getpid()))
    return True

def take_over():
    release_gate(force=True)
    return take_gate()

def release_gate(force=False):
    try:
        if force or int((GATE / "pid").read_text()) == os.getpid():
            (GATE / "pid").unlink(missing_ok=True)
            GATE.rmdir()
    except (OSError, ValueError):
        pass

mode = sys.argv[1]
if mode == "try":
    got = take_gate()
    print("took" if got else "busy", flush=True)
    release_gate()
elif mode == "hold":
    assert take_gate()
    print("held", flush=True)
    time.sleep(float(sys.argv[2]))
    release_gate()
elif mode == "leave":
    assert take_gate()
    print("left", flush=True)
'''


@pytest.fixture
def slot_dir(tmp_path):
    return tmp_path / "localappdata" / "build_slot"


@pytest.fixture
def legacy(tmp_path):
    path = tmp_path / "legacy_gate.py"
    path.write_text(LEGACY, encoding="utf-8")
    return path


def legacy_run(legacy, *argv, wait=True):
    env = dict(os.environ)  # LOCALAPPDATA is the test's, from conftest
    proc = subprocess.Popen([sys.executable, str(legacy), *argv], stdout=subprocess.PIPE, text=True, env=env)
    if wait:
        out, _ = proc.communicate(timeout=60)
        return out.strip()
    assert proc.stdout.readline().strip() in ("held", "left")
    return proc


def make_slot(builds=None, **kw):
    config = _slots.SlotConfig(**kw)
    messages = []
    slot = _slots.Slot(config, lister=(lambda: list(builds)) if builds is not None else (lambda: []),
                       say=messages.append)
    return slot, messages


# --- which processes are heavy ---------------------------------------------------------------------------------

DEFAULTS = list(_slots.DEFAULT_PROCESSES)


def rows(*items):
    return [(str(pid), str(ppid), name, line) for pid, ppid, name, line in items]


def test_a_process_tree_counts_once_by_its_top_process():
    found = _slots.heavy_tops(rows((1, 0, "cargo.exe", "cargo test"), (2, 1, "cargo.exe", "cargo-real test"),
                                   (3, 2, "rustc.exe", "rustc x")), DEFAULTS)
    assert found == [("cargo", "cargo test")]


def test_dart_counts_only_for_a_test_build_or_compile():
    found = _slots.heavy_tops(rows((1, 0, "dart.exe", "dart pub get"), (2, 0, "dart.exe", "dart test x"),
                                   (3, 0, "dart.exe", "dart compile exe y"), (4, 0, "flutter_tester.exe", "t")),
                              DEFAULTS)
    assert [line for _, line in found] == ["dart test x", "dart compile exe y", "t"]


def test_names_and_words_are_configurable():
    found = _slots.heavy_tops(rows((1, 0, "ninja", "ninja all"), (2, 0, "cargo", "cargo b"), (3, 0, "make", "make x")),
                              ["ninja", "make install"])
    assert [name for name, _ in found] == ["ninja"]


def test_ps_rows_parse():
    text = "  10     1 cargo           cargo test --workspace\n 11 10 /usr/bin/rustc rustc --edition 2021\nbad\n"
    assert _slots.parse_ps(text) == [("10", "1", "cargo", "cargo test --workspace"),
                                     ("11", "10", "rustc", "rustc --edition 2021")]


# --- the limit ---------------------------------------------------------------------------------------------------


def test_limit_is_the_flag_then_the_file_then_the_configuration(slot_dir):
    slot, _ = make_slot(dir=slot_dir, limit=6)
    assert slot.limit(None) == 6
    slot_dir.mkdir(parents=True)
    (slot_dir / "limit").write_text("3\n", encoding="utf-8")
    assert slot.limit(None) == 3
    assert slot.limit(9) == 9
    (slot_dir / "limit").write_text("zero\n", encoding="utf-8")
    assert slot.limit(None) == 6


def test_the_default_limit_is_eight(slot_dir):
    assert make_slot(dir=slot_dir)[0].limit(None) == 8


def test_a_changed_limit_file_reaches_a_waiting_caller(slot_dir):
    slot_dir.mkdir(parents=True)
    (slot_dir / "limit").write_text("1\n", encoding="utf-8")
    slot, _ = make_slot(["one running"], dir=slot_dir)
    threading.Timer(0.4, lambda: (slot_dir / "limit").write_text("2\n", encoding="utf-8")).start()
    started = time.monotonic()
    assert slot.run([sys.executable, "-c", "pass"], every=0.1, settle=0, poll=0.05) == 0
    assert 0.3 < time.monotonic() - started < 20


# --- running ---------------------------------------------------------------------------------------------------


def test_the_command_runs_and_its_exit_code_comes_back(slot_dir):
    slot, messages = make_slot(dir=slot_dir)
    assert slot.run([sys.executable, "-c", "raise SystemExit(7)"], settle=0, poll=0.05) == 7
    assert any(m.startswith("slot free") for m in messages)
    assert not (slot_dir / "gate").exists()


def test_a_missing_command_is_127_and_frees_the_gate(slot_dir):
    slot, messages = make_slot(dir=slot_dir)
    assert slot.run(["no-such-command-xyz"], settle=0) == 127
    assert "not found: no-such-command-xyz" in messages
    assert not (slot_dir / "gate").exists()


def test_no_free_slot_is_exit_1_naming_who_holds_them(slot_dir):
    slot, messages = make_slot(["windows cargo: cargo test", "wsl d cargo: x"], dir=slot_dir, limit=2)
    assert slot.run([sys.executable, "-c", "pass"], max_wait=0, every=0.05) == 1
    assert "no slot after 0 s; running:" in messages
    assert "  windows cargo: cargo test" in messages


def test_without_a_command_it_only_waits(slot_dir):
    slot, _ = make_slot(dir=slot_dir)
    assert slot.run([]) == 0
    assert not (slot_dir / "gate").exists()


def test_the_gate_is_held_until_the_settle_time(slot_dir):
    slot, _ = make_slot(dir=slot_dir)
    seen = []
    slot.run([sys.executable, "-c", "import time; time.sleep(1.5)"], settle=0.5, poll=0.1,
             popen=lambda argv, **kw: (seen.append((slot_dir / "gate").exists()), subprocess.Popen(argv, **kw))[1])
    assert seen == [True]
    assert not (slot_dir / "gate").exists()


def test_the_command_line_runs_it(slot_dir, tmp_path, capsys, monkeypatch):
    slot_dir.mkdir(parents=True)
    (slot_dir / "limit").write_text("1000\n", encoding="utf-8")
    marker = tmp_path / "ran.txt"
    command = duho.parse(build_slot.BuildSlot, ["--settle", "0", "--", sys.executable, "-c",
                                               f"open({str(marker)!r}, 'w').write('x'); print('out-only')"])
    assert command() == 0
    out = capsys.readouterr()
    assert marker.read_text() == "x"
    assert "slot free" in out.err and "slot free" not in out.out


# --- the lock directory, shared with the shell gates ------------------------------------------------------------


def test_the_gate_lock_writes_a_holder_file_and_removes_it_first(tmp_path):
    path = tmp_path / "canoa_gate.lock"
    lock = _slots.GateLock(path, "label")
    lock.acquire()
    text = (path / "holder").read_text(encoding="utf-8")
    assert text.startswith(f"label pid {os.getpid()} ") and text.endswith("Z\n")
    lock.release()
    assert not path.exists()


def test_the_gate_lock_waits_for_a_directory_a_shell_script_made(tmp_path):
    path = tmp_path / "gate.lock"
    path.mkdir()
    (path / "holder").write_text("shell label pid 1 2026-01-01T00:00:00Z\n", encoding="utf-8")
    messages = []
    lock = _slots.GateLock(path, "new", every=0.05, say=messages.append)

    def finish():
        (path / "holder").unlink()
        path.rmdir()

    threading.Timer(0.3, finish).start()
    lock.acquire()
    assert any("held by shell label pid 1" in m for m in messages)
    lock.release()


def test_a_second_process_makes_the_first_wait(tmp_path):
    """Two callers on one lock directory: the second does not enter until the first leaves."""
    path = tmp_path / "gate.lock"
    script = tmp_path / "holder.py"
    script.write_text(
        "import sys, time\nsys.path.insert(0, %r)\nimport _slots\n"
        "lock = _slots.GateLock(__import__('pathlib').Path(sys.argv[1]), 'other')\n"
        "lock.acquire(); print('held', flush=True); time.sleep(1.0); lock.release()\n"
        % str(Path(_slots.__file__).parent), encoding="utf-8")
    other = subprocess.Popen([sys.executable, str(script), str(path)], stdout=subprocess.PIPE, text=True)
    assert other.stdout.readline().strip() == "held"
    started = time.monotonic()
    lock = _slots.GateLock(path, "first", every=0.1, say=lambda m: None)
    lock.acquire()
    assert time.monotonic() - started >= 0.5
    assert "first pid" in lock.holder()
    lock.release()
    other.wait(timeout=30)


def test_the_lock_path_is_configured_or_beside_the_slots(tmp_path, monkeypatch):
    import agent_tools
    local = tmp_path / "local.toml"
    local.write_text('[gate]\nlock = "$LOCALAPPDATA/shared_gate.lock"\n', encoding="utf-8")
    config = agent_tools.load(None, local=local)
    assert _slots.lock_path(config) == Path(os.environ["LOCALAPPDATA"]) / "shared_gate.lock"
    assert _slots.lock_path(agent_tools.load(None, local=tmp_path / "none.toml")).name == "agent_gate.lock"


# --- old and new callers at the same time ----------------------------------------------------------------------


def test_a_gate_held_by_the_old_script_keeps_the_new_command_out(slot_dir, legacy):
    holder = legacy_run(legacy, "hold", "1.5", wait=False)
    try:
        slot, _ = make_slot(dir=slot_dir)
        assert slot.take_gate() is False
        started = time.monotonic()
        assert slot.run([sys.executable, "-c", "pass"], every=0.1, settle=0, poll=0.05) == 0
        assert time.monotonic() - started > 0.5  # it waited for the old script to leave
    finally:
        holder.wait(timeout=30)
    assert not (slot_dir / "gate").exists()


def test_a_gate_held_by_the_new_command_keeps_the_old_script_out(slot_dir, legacy):
    slot, _ = make_slot(dir=slot_dir)
    assert slot.take_gate() is True
    try:
        assert legacy_run(legacy, "try") == "busy"
    finally:
        slot.release_gate()
    assert legacy_run(legacy, "try") == "took"
    assert not (slot_dir / "gate").exists()


def test_a_gate_the_old_script_left_behind_is_taken_over(slot_dir, legacy):
    assert legacy_run(legacy, "leave") == "left"
    assert (slot_dir / "gate" / "pid").exists()
    slot, _ = make_slot(dir=slot_dir)
    assert slot.take_gate() is True
    assert int((slot_dir / "gate" / "pid").read_text()) == os.getpid()
    slot.release_gate()


def test_a_gate_the_new_command_left_behind_is_taken_over_by_the_old_script(slot_dir, legacy, tmp_path):
    script = tmp_path / "leave_new.py"
    script.write_text(
        "import sys, pathlib\nsys.path.insert(0, %r)\nimport _slots\n"
        "s = _slots.Slot(_slots.SlotConfig(), lister=lambda: [])\nassert s.take_gate()\n"
        % str(Path(_slots.__file__).parent), encoding="utf-8")
    subprocess.run([sys.executable, str(script)], check=True)
    assert (slot_dir / "gate" / "pid").exists()
    assert legacy_run(legacy, "try") == "took"


def test_the_old_script_and_the_new_command_run_at_once_on_one_slot_directory(slot_dir, legacy):
    """BUILD_SLOT_LEGACY, when set, names the earlier build_slot.py: it then runs for real, beside the new
    command, on the test's own slot directory and with a limit no machine reaches. Otherwise the stand-in holds
    the gate the way that script does."""
    slot_dir.mkdir(parents=True)
    (slot_dir / "limit").write_text("1000\n", encoding="utf-8")
    old =os.environ.get("BUILD_SLOT_LEGACY")
    if old and Path(old).is_file():
        first = subprocess.Popen([sys.executable, "-I", old, "--max-wait", "60", "--every", "1", "--settle", "1", "--",
                                  sys.executable, "-c", "import time; time.sleep(3)"], env=dict(os.environ))
    else:
        first = legacy_run(legacy, "hold", "1.5", wait=False)
    time.sleep(0.5)
    slot = _slots.Slot(_slots.SlotConfig(), say=lambda m: None)
    assert slot.run([sys.executable, "-c", "pass"], every=0.2, settle=0, poll=0.1) == 0
    assert first.wait(timeout=60) == 0
    assert not (slot_dir / "gate").exists()
