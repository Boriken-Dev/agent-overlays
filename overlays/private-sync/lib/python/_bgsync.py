"""Background sync for the private-sync hooks.

A turn-end hook must return at once: a turn can end every minute, several sessions share one
store, and a pull, commit and push of a large store on a loaded machine outlasts any fixed
deadline. So the hook only records what to sync (``pending/``) and starts a detached runner
(:func:`runner`) unless one is already running; the runner does the git work and the next hook
reports a failure it left behind.

State, under ``<agents>/.cache/private-sync/`` (the store ignores dot-directories):

- ``pending/<id>.json``  a project whose store wants a sync; one file per project, so requests
  made while a sync runs coalesce into the runner's next pass.
- ``sync.lock``          the single-flight lock. Held by a runner, refreshed at every step, and
  taken over when older than ``stale`` seconds.
- ``last.json``          when the last pass, sweep and maintenance finished.
- ``failure.json``       what the last failed pass could not do; reported once by the next hook,
  removed by the next pass that succeeds.
- ``sync.log``           bounded log with the seconds each git step took.

Settings, each ``PRIVATE_SYNC_<NAME>`` in the environment, else ``name = value`` in
``<agents>/private-sync.cfg``, else the default (:data:`DEFAULTS`).

Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

#: name -> (default, low, high). ``0`` for ``sweep_interval`` and ``maintenance_interval`` is "never".
SETTINGS = {
    # 1: the turn-end hook starts a detached sync and returns; 0: it syncs before returning.
    "background": (1, 0, 1),
    # Seconds between two passes of the runner: requests made sooner wait and merge.
    "interval": (30, 0, 3600),
    # Seconds one git call may run before it is ended.
    "git_timeout": (120, 5, 1800),
    # Seconds without progress after which a lock is taken over.
    "stale": (900, 60, 86400),
    # Seconds between sweeps of every other linked store for uncommitted or unpushed work.
    "sweep_interval": (300, 0, 86400),
    # Seconds between maintenance runs (worktree prune, gc --auto), started at session start.
    "maintenance_interval": (86400, 0, 30 * 86400),
}
DEFAULTS = {name: spec[0] for name, spec in SETTINGS.items()}

LOG_LIMIT = 200 * 1024
_SECRET_NAME = re.compile(r"TOKEN|SECRET|PASSW|CREDENTIAL|_KEY$", re.IGNORECASE)


def settings(agents_dir) -> "dict[str, int]":
    """The settings in force: environment, else the store's ``private-sync.cfg``, else defaults.
    A value that is not an integer or is out of bounds is ignored."""
    values = dict(DEFAULTS)
    file_values: "dict[str, str]" = {}
    try:
        for line in (Path(agents_dir) / "private-sync.cfg").read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if "=" in line:
                key, _, value = line.partition("=")
                file_values[key.strip().lower()] = value.strip().strip("\"'")
    except OSError:
        pass
    for name, (_, low, high) in SETTINGS.items():
        for raw in (os.environ.get("PRIVATE_SYNC_" + name.upper()), file_values.get(name)):
            if raw is None:
                continue
            word = raw.strip().lower()
            raw = {"true": "1", "false": "0", "yes": "1", "no": "0"}.get(word, raw)
            try:
                number = int(raw)
            except ValueError:
                continue
            if low <= number <= high:
                values[name] = number
                break
    return values


def state_dir(agents_dir) -> Path:
    return Path(agents_dir) / ".cache" / "private-sync"


def _ensure(agents_dir) -> Path:
    state = state_dir(agents_dir)
    (state / "pending").mkdir(parents=True, exist_ok=True)
    return state


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".%d.tmp" % os.getpid())
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(str(tmp), str(path))


def redact(text: str) -> str:
    """Text safe for a log: no credential in a URL, and not the token itself."""
    text = re.sub(r"://[^/@\s]+@", "://***@", text)
    token = os.environ.get("DOTAGENTS_AGENTS_TOKEN")
    return text.replace(token, "***") if token else text


# --------------------------------------------------------------------------- #
# The single-flight lock.
# --------------------------------------------------------------------------- #


def _lock_path(state: Path) -> Path:
    return state / "sync.lock"


def lock_is_fresh(state: Path, stale: int) -> bool:
    try:
        return time.time() - _lock_path(state).stat().st_mtime <= stale
    except OSError:
        return False


def acquire(state: Path, stale: int) -> bool:
    """Take the lock, or take it over when its holder has made no progress for ``stale`` seconds."""
    path = _lock_path(state)
    for _ in range(2):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if lock_is_fresh(state, stale):
                return False
            try:
                os.unlink(str(path))
            except OSError:
                return False
            continue
        except OSError:
            return False
        with os.fdopen(fd, "w") as handle:
            handle.write(str(os.getpid()))
        return True
    return False


def _heartbeat(state: Path) -> None:
    try:
        os.utime(str(_lock_path(state)), None)
    except OSError:
        pass


def release(state: Path) -> None:
    try:
        os.unlink(str(_lock_path(state)))
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Requests, and what the last pass left behind.
# --------------------------------------------------------------------------- #


def add_pending(state: Path, project: str, message: str) -> None:
    ident = hashlib.sha1(os.path.normcase(os.path.abspath(project)).encode("utf-8")).hexdigest()[:12]
    _write_json(state / "pending" / (ident + ".json"),
                {"project": str(project), "message": message, "at": time.time()})


def take_pending(state: Path) -> "list[dict]":
    items = []
    folder = state / "pending"
    try:
        names = sorted(os.listdir(str(folder)))
    except OSError:
        return items
    for name in names:
        if not name.endswith(".json"):
            continue
        path = folder / name
        data = _read_json(path)
        try:
            os.unlink(str(path))
        except OSError:
            continue
        if data.get("project"):
            items.append(data)
    return items


def has_pending(state: Path) -> bool:
    try:
        return any(name.endswith(".json") for name in os.listdir(str(state / "pending")))
    except OSError:
        return False


def report_failure(state: Path, log) -> None:
    """Say, once, what the last background pass could not do."""
    path = state / "failure.json"
    data = _read_json(path)
    if not data or data.get("reported"):
        return
    log("private-sync: the last background sync did not finish (%s: %s); see %s",
        data.get("step", "?"), data.get("message", "?"), state / "sync.log")
    data["reported"] = True
    try:
        _write_json(path, data)
    except OSError:
        pass


class _Capture:
    """The logger the sync library writes to: lines go to the bounded log, a ``WARN`` or a timed
    out git call becomes the pass's failure, and every git step is timed."""

    def __init__(self, state: Path):
        self.state = state
        self.path = state / "sync.log"
        self.failure: "Optional[dict]" = None
        self.context = ""
        try:
            if self.path.stat().st_size > LOG_LIMIT:
                os.replace(str(self.path), str(self.path) + ".1")
        except OSError:
            pass

    def _write(self, text: str) -> None:
        line = "%s %s%s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), self.context, redact(text))
        try:
            with open(str(self.path), "a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError:
            pass

    def info(self, msg, *args):
        text = msg % args if args else str(msg)
        self._write(text)
        if text.startswith("WARN"):
            self.fail("sync", text)

    def fail(self, step: str, message: str) -> None:
        if self.failure is None:
            self.failure = {"step": step, "message": redact(message)[:400], "at": time.time(),
                            "project": self.context.strip(" []")}

    def step(self, label: str, seconds: float, code: int) -> None:
        self._write("%s %.2fs rc=%d" % (label, seconds, code))
        if code == 124:
            self.fail(label, "timed out")


# --------------------------------------------------------------------------- #
# The passes.
# --------------------------------------------------------------------------- #


def _linked_worktrees(agents: Path, link) -> "list[tuple[Path, str]]":
    """``(path, branch)`` of each linked store worktree of the agents repository that is on its
    own branch and present on disk."""
    out = link._git(agents, "worktree", "list", "--porcelain", capture=True).stdout or ""
    found = []
    for block in out.replace("\r\n", "\n").split("\n\n"):
        fields = {}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            fields[key] = value
        path, branch = fields.get("worktree"), fields.get("branch", "")
        if not path or not branch.startswith("refs/heads/") or "prunable" in fields:
            continue
        where = Path(path)
        if where.is_dir() and os.path.normcase(str(where.resolve())) != os.path.normcase(str(agents.resolve())):
            found.append((where, branch[len("refs/heads/"):]))
    return found


def _wants_sync(path: Path, link) -> bool:
    """Uncommitted changes, or commits not yet on the upstream: read from one local git call."""
    out = link._git(path, "status", "--porcelain=v2", "--branch", capture=True)
    if out.returncode != 0:
        return False
    for line in (out.stdout or "").splitlines():
        if not line.startswith("#"):
            return True
        if line.startswith("# branch.ab "):
            ahead = line.split()[2]
            if ahead.startswith("+") and ahead != "+0":
                return True
    return False


def _sweep(agents: Path, link, capture: _Capture, done: "set[str]") -> None:
    """Sync every other linked store that has work to save (a session may have written to a store
    whose own project this session never named)."""
    for path, branch in _linked_worktrees(agents, link):
        key = os.path.normcase(str(path.resolve()))
        if key in done:
            continue
        _heartbeat(capture.state)
        capture.context = "[%s] " % (path.parent.name if path.name == ".agents" else path.name)
        if _wants_sync(path, link):
            capture.info("sweep: syncing %s", branch)
            link.sync_worktree(path, branch, "sync: %s session" % capture.context.strip(" []"),
                               logger=capture)
    capture.context = ""


def _maintain(agents: Path, link, capture: _Capture, cfg: dict) -> None:
    """The maintenance a sync must not do: forget worktrees whose directory is gone (the
    directories themselves are never touched), and let git repack when it wants to."""
    capture.context = "[maintenance] "
    link._git(agents, "worktree", "prune", maintenance=True)
    saved = link.git_timeout
    link.git_timeout = max(cfg["git_timeout"], 900)
    try:
        link._git(agents, "gc", "--auto", "--quiet", maintenance=True)
    finally:
        link.git_timeout = saved
    capture.context = ""


def drain(agents, link, *, sleep=time.sleep) -> int:
    """Run passes until nothing is pending. The caller holds the lock."""
    agents = Path(agents)
    state = _ensure(agents)
    cfg = settings(agents)
    capture = _Capture(state)
    link.git_timeout = cfg["git_timeout"]
    link.step_hook = lambda label, seconds, code: (_heartbeat(state), capture.step(label, seconds, code))
    os.environ["GIT_TERMINAL_PROMPT"] = "0"
    failed: "Optional[dict]" = None
    for _ in range(50):
        last = _read_json(state / "last.json")
        maintain_wanted = (state / "maintain.request").exists()
        if not has_pending(state) and not maintain_wanted:
            break
        # Requests made within `interval` of the last pass wait and merge into one.
        wait = cfg["interval"] - (time.time() - last.get("finished", 0)) if has_pending(state) else 0
        while wait > 0:
            _heartbeat(state)
            sleep(min(wait, 5))
            wait -= 5
        batch = take_pending(state)
        pass_started = time.time()
        capture.failure = None
        done: "set[str]" = set()
        first = True
        for item in batch:
            project = item["project"]
            capture.context = "[%s] " % Path(project).name
            _heartbeat(state)
            try:
                link.sync_agents(agents, message=item.get("message") or "sync", project_dir=project,
                                 main=first, logger=capture)
                first = False
                done.add(os.path.normcase(str((Path(project) / ".agents").resolve())))
            except Exception as exc:  # a failed project must not stop the others
                capture.fail("sync", "%s: %s" % (type(exc).__name__, exc))
                capture.info("WARN: sync failed: %s: %s", type(exc).__name__, exc)
        stamps = dict(last)
        if batch and cfg["sweep_interval"] and time.time() - last.get("sweep", 0) >= cfg["sweep_interval"]:
            try:
                _sweep(agents, link, capture, done)
            except Exception as exc:
                capture.fail("sweep", "%s: %s" % (type(exc).__name__, exc))
            stamps["sweep"] = time.time()
        if maintain_wanted:
            try:
                _maintain(agents, link, capture, cfg)
            except Exception as exc:
                capture.fail("maintenance", "%s: %s" % (type(exc).__name__, exc))
            stamps["maintenance"] = time.time()
            try:
                os.unlink(str(state / "maintain.request"))
            except OSError:
                pass
        stamps["finished"] = time.time()
        _write_json(state / "last.json", stamps)
        if capture.failure is not None:
            failed = capture.failure
            _write_json(state / "failure.json", failed)
        elif failed is None:
            try:
                os.unlink(str(state / "failure.json"))
            except OSError:
                pass
        capture.info("pass finished in %.1fs", time.time() - pass_started)
    return 1 if failed else 0


def runner(agents, link, *, env_file: "Optional[str]" = None) -> int:
    """The detached process: take the lock and drain; look again after releasing it, so a request
    that arrived as the last pass ended is not stranded."""
    if env_file:
        try:
            os.environ.update(json.loads(Path(env_file).read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
        try:
            os.unlink(env_file)
        except OSError:
            pass
    state = _ensure(agents)
    cfg = settings(agents)
    code = 0
    for _ in range(3):
        if not acquire(state, cfg["stale"]):
            break
        try:
            code = drain(agents, link)
        finally:
            release(state)
        if not has_pending(state):
            break
    return code


# --------------------------------------------------------------------------- #
# What the hooks call.
# --------------------------------------------------------------------------- #


def _spawn(agents, script: Path, *, env_extra: "Optional[dict]" = None) -> bool:
    """Start ``script runner`` detached from this process and from its process tree. True when it
    started."""
    argv = [sys.executable, "-I", str(script), "runner", str(agents)]
    state = _ensure(agents)
    if os.name != "nt":
        try:
            subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True,
                             cwd=str(state))
            return True
        except OSError:
            return False
    # The harness runs a hook in a job object that ends every process in it when the hook returns,
    # and a process created with breakaway is still ended; the WMI service is outside it. It starts
    # the runner without our environment, so the (non-secret) environment goes in a file the runner
    # reads and deletes; a credential is never written, so a run that needs one is not detached.
    if os.environ.get("DOTAGENTS_AGENTS_TOKEN"):
        return False
    env_file = state / ("spawn-%d.json" % os.getpid())
    try:
        env_file.write_text(json.dumps({k: v for k, v in os.environ.items() if not _SECRET_NAME.search(k)}),
                            encoding="utf-8")
    except OSError:
        return False
    command = subprocess.list2cmdline(argv + ["--env-file", str(env_file)]).replace("'", "''")
    cwd = str(state).replace("'", "''")
    script_text = (
        "$s = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ShowWindow=[uint16]0}; "
        "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments "
        "@{CommandLine='%s'; CurrentDirectory='%s'; ProcessStartupInformation=$s}; "
        "if ($r.ReturnValue -ne 0) { exit 1 }" % (command, cwd))
    try:
        done = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script_text],
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
        ok = done.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    if not ok:
        try:
            os.unlink(str(env_file))
        except OSError:
            pass
    return ok


def turn_end(agents, project: str, message: str, link, script: Path, log) -> int:
    """The turn-end hook: record the request and start a sync that outlives the hook. Returns at
    once. With ``background`` off, or when no detached process can be started, it syncs here."""
    started = time.monotonic()
    agents = Path(agents)
    cfg = settings(agents)
    state = _ensure(agents)
    report_failure(state, log)
    add_pending(state, project, message)
    if cfg["background"]:
        if lock_is_fresh(state, cfg["stale"]):
            log("private-sync: a sync is running; this turn joins its next pass (%.2fs)",
                time.monotonic() - started)
            return 0
        if _spawn(agents, script):
            log("private-sync: sync started in the background (%.2fs)", time.monotonic() - started)
            return 0
        log("private-sync: could not start a background sync; syncing now")
    return runner(agents, link)


def session_start(agents, script: Path, log) -> None:
    """The session-start hook's part: report a failure, and ask the runner for maintenance when it
    is due (never in the hook itself)."""
    agents = Path(agents)
    cfg = settings(agents)
    state = _ensure(agents)
    report_failure(state, log)
    if not cfg["maintenance_interval"] or not cfg["background"]:
        return
    last = _read_json(state / "last.json")
    if time.time() - last.get("maintenance", 0) < cfg["maintenance_interval"]:
        return
    try:
        (state / "maintain.request").write_text("", encoding="utf-8")
    except OSError:
        return
    if not lock_is_fresh(state, cfg["stale"]):
        _spawn(agents, script)
