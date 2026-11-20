"""Build slots and the machine-wide gate lock, shared by `build-slot` and `gate`.

A heavy build is a running cargo, rustc, Flutter test or Dart build or test (the names are configurable).
`Slot` lets a caller start one only while fewer than a limit run on this machine; starts are serialised through
a gate directory so two callers never take the same free slot. `GateLock` is the lock that whole-workspace test
runs share: a directory created with `mkdir`, with a `holder` file inside saying whose it is.

State on disk, written the same way by every version of this tool, so old and new callers share it:

    <dir>/limit      one integer, read on every check (<dir> is $LOCALAPPDATA/build_slot, else ~/build_slot)
    <dir>/gate/pid   the gate directory and the pid of the caller that holds it
    <lock>/holder    "<label> pid <pid> <UTC time>", removed before the lock directory

Standard library only.
"""

from __future__ import annotations

import csv
import ctypes
import io
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

DEFAULT_LIMIT = 8
DEFAULT_PROCESSES = ["cargo", "rustc", "flutter_tester", "dart build", "dart test", "dart compile"]
DEFAULT_MAX_WAIT = 7200
DEFAULT_EVERY = 10
DEFAULT_SETTLE = 20
DEFAULT_POLL = 5
DEFAULT_LOCK_EVERY = 20
#: A gate directory without a readable pid file is taken over after this many seconds.
GATE_STALE_SECONDS = 30
EXIT_NOT_STARTED = 127


def base_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or str(Path.home()))


class SlotTimeout(Exception):
    def __init__(self, waited: int, builds: list[str]):
        super().__init__(f"no slot after {waited} s")
        self.waited = waited
        self.builds = builds


@dataclass
class SlotConfig:
    """`[build_slot]` of the machine file, defaults filled in."""

    dir: Path = field(default_factory=lambda: base_dir() / "build_slot")
    limit: int = DEFAULT_LIMIT
    processes: list[str] = field(default_factory=lambda: list(DEFAULT_PROCESSES))
    wsl: list[str] = field(default_factory=list)

    @classmethod
    def from_config(cls, config: Any) -> "SlotConfig":
        table = config.table("build_slot")
        made = cls()
        if "dir" in table:
            made.dir = Path(os.path.expandvars(os.path.expanduser(table["dir"])))
        made.limit = table.get("limit", made.limit)
        made.processes = list(table.get("processes", made.processes))
        made.wsl = list(table.get("wsl", made.wsl))
        return made

    @property
    def gate(self) -> Path:
        return self.dir / "gate"

    @property
    def limit_file(self) -> Path:
        return self.dir / "limit"


def _names(processes: list[str]) -> dict[str, set[str]]:
    """{process name: the words one of which its command line must hold; empty: any}."""
    table: dict[str, set[str]] = {}
    unrestricted: set[str] = set()
    for entry in processes:
        name, *words = entry.lower().split()
        name = name.removesuffix(".exe")
        table.setdefault(name, set()).update(words)
        if not words:
            unrestricted.add(name)
    for name in unrestricted:
        table[name] = set()
    return table


def heavy_tops(rows: list[tuple[str, str, str, str]], processes: list[str]) -> list[tuple[str, str]]:
    """[(name, command line)] of the heavy builds in `rows` (pid, ppid, name, command line), a process tree
    counted once by its top process."""
    wanted = _names(processes)
    named = [(pid, ppid, name.lower().removesuffix(".exe"), line) for pid, ppid, name, line in rows]
    named = [row for row in named if row[2] in wanted]
    ids = {row[0] for row in named}
    tops = []
    for pid, ppid, name, line in named:
        if ppid in ids:
            continue
        words = wanted[name]
        if not words or any(word in line.lower() for word in words):
            tops.append((name, line))
    return tops


def parse_ps(text: str) -> list[tuple[str, str, str, str]]:
    """Rows of `ps -eo pid=,ppid=,comm=,args=`."""
    rows = []
    for line in text.splitlines():
        parts = line.split(None, 3)
        if len(parts) >= 3 and parts[0].isdigit() and parts[1].isdigit():
            rows.append((parts[0], parts[1], os.path.basename(parts[2]), parts[3] if len(parts) > 3 else ""))
    return rows


def _windows_rows(processes: list[str]) -> list[tuple[str, str, str, str]]:
    names = " OR ".join(f"Name='{name}.exe'" for name in _names(processes))
    done = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f'Get-CimInstance Win32_Process -Filter "{names}" '
         "| Select-Object ProcessId,ParentProcessId,Name,CommandLine | ConvertTo-Csv -NoTypeInformation"],
        capture_output=True, text=True, timeout=60)
    return [(r["ProcessId"], r["ParentProcessId"], r["Name"], r["CommandLine"] or "")
            for r in csv.DictReader(io.StringIO(done.stdout))]


def local_builds(config: SlotConfig) -> list[str]:
    try:
        if os.name == "nt":
            rows = _windows_rows(config.processes)
        else:
            rows = parse_ps(subprocess.run(["ps", "-eo", "pid=,ppid=,comm=,args="], capture_output=True,
                                           text=True, timeout=60).stdout)
    except (OSError, subprocess.TimeoutExpired):
        return []
    system = "windows" if os.name == "nt" else "local"
    return [f"{system} {name}: {line[:120]}" for name, line in heavy_tops(rows, config.processes)]


def wsl_builds(config: SlotConfig) -> list[str]:
    found: list[str] = []
    if os.name != "nt":
        return found
    for distro in config.wsl:
        try:
            done = subprocess.run(["wsl", "-d", distro, "--", "ps", "-eo", "pid=,ppid=,comm=,args="],
                                  capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            continue
        # When WSL itself fails, wsl.exe prints a UTF-16 error; only parsed process rows count.
        if done.returncode != 0:
            continue
        found += [f"wsl {distro} {name}: {line[:120]}"
                  for name, line in heavy_tops(parse_ps(done.stdout), config.processes)]
    return found


def alive(pid: int) -> bool:
    # os.kill(pid, 0) sends CTRL_C_EVENT on Windows, so ask the process table instead.
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True
        except OSError:
            return False
    kernel = ctypes.windll.kernel32
    handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    code = ctypes.c_ulong()
    ok = kernel.GetExitCodeProcess(handle, ctypes.byref(code))
    kernel.CloseHandle(handle)
    return bool(ok) and code.value == 259  # STILL_ACTIVE


def _say(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


class Slot:
    """The limit, the gate and the wait loop. `lister` returns the running heavy builds, one string each."""

    def __init__(self, config: SlotConfig, *, lister: Callable[[], list[str]] | None = None,
                 say: Callable[[str], None] = _say):
        self.config = config
        self.say = say
        self.lister = lister or (lambda: local_builds(config) + wsl_builds(config))

    def limit(self, fixed: int | None) -> int:
        """`fixed`, else the limit file (read on every check, so a change reaches callers already waiting), else
        `[build_slot] limit`."""
        if fixed:
            return fixed
        try:
            return max(1, int(self.config.limit_file.read_text().strip()))
        except (OSError, ValueError):
            return self.config.limit

    # -- the gate ---------------------------------------------------------------------------------------------

    def take_gate(self) -> bool:
        gate = self.config.gate
        gate.parent.mkdir(parents=True, exist_ok=True)
        try:
            gate.mkdir()
        except FileExistsError:
            try:
                holder = int((gate / "pid").read_text())
            except (OSError, ValueError):
                try:
                    stale = time.time() - gate.stat().st_mtime > GATE_STALE_SECONDS
                except OSError:
                    return False
                return self._take_over() if stale else False  # else: being written by its new holder
            return self._take_over() if not alive(holder) else False
        (gate / "pid").write_text(str(os.getpid()))
        return True

    def _take_over(self) -> bool:
        self.release_gate(force=True)
        return self.take_gate()

    def release_gate(self, force: bool = False) -> None:
        gate = self.config.gate
        try:
            if force or int((gate / "pid").read_text()) == os.getpid():
                (gate / "pid").unlink(missing_ok=True)
                gate.rmdir()
        except (OSError, ValueError):
            pass

    # -- waiting and running ----------------------------------------------------------------------------------

    def acquire(self, *, limit: int | None = None, command: bool = True, max_wait: int = DEFAULT_MAX_WAIT,
                every: float = DEFAULT_EVERY) -> list[str]:
        """Wait for a free slot and, when a command will follow, take the gate; the running builds seen last.
        The gate is held only while a slot is free: a caller waiting on its own lower limit must not keep
        everyone else from starting. Raises `SlotTimeout`."""
        deadline = time.monotonic() + max_wait
        gated = False
        try:
            while True:
                builds = self.lister()
                free = len(builds) < self.limit(limit)
                if free and command:
                    gated = self.take_gate()
                    if gated:
                        builds = self.lister()
                        if len(builds) < self.limit(limit):
                            self._held = True
                            gated = False
                            self.say("slot free: %d heavy build(s) running" % len(builds))
                            return builds
                        self.release_gate()
                        gated = False
                    else:
                        builds = ["(another caller is starting a build)"]
                elif free:
                    self.say("slot free: %d heavy build(s) running" % len(builds))
                    return builds
                if time.monotonic() >= deadline:
                    raise SlotTimeout(max_wait, builds)
                self.say("waiting: " + ("another caller holds the gate" if free and command
                                        else "%d heavy builds running" % len(builds)))
                time.sleep(every)
        except BaseException:
            if gated:
                self.release_gate()
            raise

    _held = False

    def settle(self, baseline: int, done: threading.Event | None = None, *, seconds: float = DEFAULT_SETTLE,
               poll: float = DEFAULT_POLL) -> None:
        """Keep the gate until the new build shows in the process count, `done` is set, or `seconds` pass; then
        release it."""
        try:
            end = time.monotonic() + seconds
            while time.monotonic() < end and not (done and done.is_set()) and len(self.lister()) <= baseline:
                if done is not None:
                    done.wait(poll)
                else:
                    time.sleep(poll)
        finally:
            self.release_gate()
            self._held = False

    def run(self, command: list[str], *, limit: int | None = None, max_wait: int = DEFAULT_MAX_WAIT,
            every: float = DEFAULT_EVERY, settle: float = DEFAULT_SETTLE, poll: float = DEFAULT_POLL,
            popen: Callable[..., subprocess.Popen] = subprocess.Popen, **popen_kwargs: Any) -> int:
        """Run `command` once a slot is free; its exit code. With no command, only wait for a slot (which two
        callers can both see free at once). 1 when no slot frees; 127 when the command is not found."""
        try:
            builds = self.acquire(limit=limit, command=bool(command), max_wait=max_wait, every=every)
        except SlotTimeout as timeout:
            self.say("no slot after %d s; running:" % timeout.waited)
            for build in timeout.builds:
                self.say("  " + build)
            return 1
        if not command:
            return 0
        try:
            # A bare name like `flutter` or `dart` is a .bat on Windows, which CreateProcess finds only by full path.
            program = shutil.which(command[0])
            if program is None:
                self.say("not found: %s" % command[0])
                return EXIT_NOT_STARTED
            child = popen([program] + command[1:], **popen_kwargs)
            finished = threading.Event()
            watcher = threading.Thread(target=self.settle, args=(len(builds), finished),
                                       kwargs={"seconds": settle, "poll": poll}, daemon=True)
            watcher.start()
            try:
                return child.wait()
            finally:
                finished.set()
                watcher.join()
        finally:
            if self._held:
                self.release_gate()
                self._held = False


# --- the gate lock ---------------------------------------------------------------------------------------------


def lock_path(config: Any) -> Path:
    """`[gate] lock` of the machine file (`$VAR` and `~` expanded), else a file beside the slot directory."""
    configured = config.get("gate", "lock")
    if configured:
        return Path(os.path.expandvars(os.path.expanduser(configured)))
    return base_dir() / "agent_gate.lock"


class GateLock:
    """A `mkdir` lock with a `holder` file, compatible with the shell scripts that took it before."""

    def __init__(self, path: Path, label: str, *, every: float = DEFAULT_LOCK_EVERY,
                 say: Callable[[str], None] = _say):
        self.path, self.label, self.every, self.say = Path(path), label, every, say
        self.held = False

    def holder(self) -> str:
        try:
            return (self.path / "holder").read_text(encoding="utf-8").strip() or "(unknown)"
        except OSError:
            return "(unknown)"

    def acquire(self, max_wait: float | None = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = None if max_wait is None else time.monotonic() + max_wait
        while True:
            try:
                self.path.mkdir()
                break
            except FileExistsError:
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError(f"the gate lock {self.path} is held by {self.holder()}") from None
                self.say(f"waiting for the gate lock {self.path}: held by {self.holder()}")
                time.sleep(self.every)
        self.held = True
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        (self.path / "holder").write_text(f"{self.label} pid {os.getpid()} {stamp}\n", encoding="utf-8")

    def release(self) -> None:
        if not self.held:
            return
        self.held = False
        try:
            # The holder file goes first, or the next caller's rmdir fails and the lock stays.
            (self.path / "holder").unlink(missing_ok=True)
            self.path.rmdir()
        except OSError:
            pass

    def __enter__(self) -> "GateLock":
        self.acquire()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()
