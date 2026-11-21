"""The gate: the checks a branch passes before it is merged, run on the machine and in a Linux distribution.

`gate run` runs the project's steps, `gate wsl` the Linux leg, `gate merge` fast-forwards a branch into main,
runs both and pushes through `repo-guard`. The steps are the project's: `[gate] steps` and `[gate] wsl_steps` of
`<project>/.agents/agent-tools.toml`; the lock and the Linux distribution are the machine's.

Standard library only.
"""

from __future__ import annotations

import contextlib
import io
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

import _slots
import agent_tools as at

DEFAULT_REMOTE = "origin"
DEFAULT_MAIN = "main"
DEFAULT_WSL_JOBS = 4
DEFAULT_WSL_TARGET_MAX_GB = 40
DEFAULT_WSL_HOME = "~"


class GateError(Exception):
    """Something the gate refuses to start or finish; the message is the whole report."""


def git(repo: Path, *args: str, check: bool = True) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    if check and done.returncode:
        raise GateError(f"git {' '.join(args)}: {done.stderr.strip() or done.stdout.strip()}")
    return done.stdout.strip()


def unfinished(repo: Path) -> str | None:
    """Why a tree is not a branch's tip: mid-rebase, mid-pick or with unmerged paths."""
    gitdir = git(repo, "rev-parse", "--absolute-git-dir")
    for marker, what in (("rebase-merge", "a rebase"), ("rebase-apply", "a rebase"), ("MERGE_HEAD", "a merge"),
                         ("CHERRY_PICK_HEAD", "a cherry-pick")):
        if (Path(gitdir) / marker).exists():
            return f"in the middle of {what}"
    if git(repo, "diff", "--name-only", "--diff-filter=U"):
        return "holding unmerged paths"
    return None


# --- globs and changed paths ---------------------------------------------------------------------------------


def glob_regex(pattern: str) -> re.Pattern:
    """`**` crosses directories, `*` and `?` do not; a trailing `/` means everything below it."""
    if pattern.endswith("/"):
        pattern += "**"
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def changed_paths(repo: Path, config: at.Config) -> list[str] | None:
    """Paths differing from REMOTE/MAIN, or None when that ref does not exist (every step then runs)."""
    remote = config.get("repo_guard", "remote", DEFAULT_REMOTE)
    main = config.get("repo_guard", "main", DEFAULT_MAIN)
    done = subprocess.run(["git", "-C", str(repo), "diff", "--name-only", f"{remote}/{main}", "HEAD"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if done.returncode:
        return None
    return [line for line in done.stdout.splitlines() if line]


def wanted(step: dict[str, Any], changed: list[str] | None) -> bool:
    patterns = step.get("when") or []
    if not patterns or changed is None:
        return True
    regexes = [glob_regex(p) for p in patterns]
    return any(rx.match(path) for path in changed for rx in regexes)


# --- running one step ----------------------------------------------------------------------------------------


def argv_of(run: Any, worktree: Path) -> list[str]:
    parts = list(run) if isinstance(run, list) else shlex.split(run)
    return [part.replace("{worktree}", str(worktree)) for part in parts]


def summarized(argv: list[str], log: Path, cwd: Path, env: dict[str, str]) -> tuple[int, str]:
    """Run `argv` through `summarize-run`: (exit code, its verdict text). The full output goes to `log`."""
    from duho import parse  # the command is duho's, as everywhere in this overlay
    module = _summarize_module()
    saved_cwd, saved_env = os.getcwd(), dict(os.environ)
    out = io.StringIO()
    try:
        os.chdir(cwd)
        os.environ.update(env)
        with contextlib.redirect_stdout(out):
            code = parse(module.SummarizeRun, ["--output", str(log), "--", *argv])()
    finally:
        os.chdir(saved_cwd)
        os.environ.clear()
        os.environ.update(saved_env)
    return code, out.getvalue()


_SUMMARIZE: Any = None


def _summarize_module() -> Any:
    global _SUMMARIZE
    if _SUMMARIZE is None:
        import importlib.util
        path = Path(__file__).resolve().parents[2] / "skills" / "summarize-run" / "summarize_run.py"
        spec = importlib.util.spec_from_file_location("gate_summarize_run", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module  # duho resolves field annotations through sys.modules
        spec.loader.exec_module(module)
        _SUMMARIZE = module
    return _SUMMARIZE


def run_step(step: dict[str, Any], worktree: Path, out: Path, slot: "_slots.Slot | None",
             slot_options: dict[str, Any]) -> bool:
    name = step["name"]
    cwd = worktree / step["cwd"] if step.get("cwd") else worktree
    env = {key: value.replace("{worktree}", str(worktree)) for key, value in (step.get("env") or {}).items()}
    argv = argv_of(step["run"], worktree)
    log = out / f"{name}.log"
    held = slot is not None
    finished = threading.Event()
    watcher = None
    if held:
        builds = slot.acquire(**{k: v for k, v in slot_options.items() if k in ("limit", "max_wait", "every")})
        watcher = threading.Thread(target=slot.settle, args=(len(builds), finished), daemon=True,
                                   kwargs={k: v for k, v in slot_options.items() if k in ("seconds", "poll")})
        watcher.start()
    try:
        code, verdict = summarized(argv, log, cwd, env)
    finally:
        finished.set()
        if watcher is not None:
            watcher.join()
    (out / f"{name}.verdict").write_text(verdict, encoding="utf-8", newline="")
    return code == 0


# --- gate run ------------------------------------------------------------------------------------------------


def lock_for(config: at.Config, label: str, every: float) -> _slots.GateLock:
    return _slots.GateLock(_slots.lock_path(config), label, every=every)


def default_out(worktree: Path) -> Path:
    path = Path(tempfile.gettempdir()) / f"gate_{worktree.name}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def step_list(config: at.Config, key: str, only: list[str]) -> list[dict[str, Any]]:
    steps = config.get("gate", key, [])
    if only:
        known = {s["name"] for s in steps}
        unknown = [n for n in only if n not in known]
        if unknown:
            raise GateError(f"no step named {', '.join(unknown)} in [gate] {key} (known: {', '.join(sorted(known))})")
        steps = [s for s in steps if s["name"] in only]
    return steps


def run_gate(worktree: Path, *, only: list[str] | None = None, out: Path | None = None, fail_fast: bool = False,
             lock: _slots.GateLock | None = None, lock_every: float = _slots.DEFAULT_LOCK_EVERY,
             slot_options: dict[str, Any] | None = None, say: Callable[[str], None] = print,
             slot: "_slots.Slot | None" = None) -> bool:
    """Run the project's steps in `worktree`; True when none failed. Prints `PASS name`, `FAIL name` or
    `SKIP name` per step. Takes the gate lock unless the caller already holds it."""
    worktree = worktree.resolve()
    config = at.load(worktree)
    steps = step_list(config, "steps", only or [])
    if not config.get("gate", "steps"):
        raise GateError(f"{config.where('gate', 'steps')}: [gate] steps is required")
    problem = unfinished(worktree)
    if problem:
        raise GateError(f"{worktree} is {problem}: refusing to pass a partial tree")
    out = (out or default_out(worktree))
    out.mkdir(parents=True, exist_ok=True)
    changed = changed_paths(worktree, config)
    own_lock = lock is None
    lock = lock or lock_for(config, worktree.name, lock_every)
    if own_lock:
        lock.acquire()
    slot = slot or _slots.Slot(_slots.SlotConfig.from_config(config))
    ok = True
    try:
        for step in steps:
            if not wanted(step, changed):
                say(f"SKIP {step['name']} (no changed path matches {', '.join(step['when'])})")
                continue
            passed = run_step(step, worktree, out, slot if step.get("slot") else None, slot_options or {})
            say(f"{'PASS' if passed else 'FAIL'} {step['name']}" + ("" if passed else f"  (see {out / (step['name'] + '.verdict')})"))
            if not passed:
                ok = False
                if fail_fast:
                    break
    finally:
        for name in config.get("gate", "restore", []):
            git(worktree, "checkout", "--", name, check=False)
        if own_lock:
            lock.release()
    say(f"logs: {out}")
    return ok


# --- gate wsl ------------------------------------------------------------------------------------------------


def wsl_path(path: Path) -> str:
    """The path as the Linux side sees it: `C:\\x` is `/mnt/c/x` on Windows, unchanged elsewhere."""
    text = str(path.resolve())
    match = re.match(r"^([A-Za-z]):[\\/](.*)$", text)
    if os.name == "nt" and match:
        return f"/mnt/{match.group(1).lower()}/" + match.group(2).replace("\\", "/")
    return text.replace("\\", "/")


def wsl_prefix(config: at.Config, root: bool = False) -> list[str] | None:
    """The argv that runs a command in the Linux distribution: `[wsl] exec`, else `wsl.exe -d DISTRO --`.
    None for `root` with a custom `exec` (there is no portable way to ask for root)."""
    custom = config.get("wsl", "exec")
    if custom:
        return None if root else list(custom)
    distro = config.require("wsl", "distro")
    return ["wsl.exe", "-d", distro, *(["-u", "root"] if root else []), "--"]


def quote(text: str) -> str:
    return shlex.quote(text)


def wsl_script(config: at.Config, name: str, steps: list[dict[str, Any]], archive: str, out: str) -> str:
    """The bash script run in the distribution: unpack the archive, run each step, report `step=exit code`."""
    home = config.get("wsl", "home", DEFAULT_WSL_HOME)
    jobs = config.get("wsl", "jobs", DEFAULT_WSL_JOBS)
    cap = math.floor(config.get("wsl", "target_max_gb", DEFAULT_WSL_TARGET_MAX_GB))
    home_sh = '"$HOME"' + quote(home[1:]) if home.startswith("~") else quote(home)
    lines = [
        "#!/bin/bash",
        f"CLONE={home_sh}/gate_{name}",
        f"TARGET={home_sh}/gate_{name}-target",
        "trim() {",
        '  gb=$(du -s --block-size=1G "$TARGET" 2>/dev/null | cut -f1)',
        f'  if [ "${{gb:-0}}" -gt {cap} ]; then rm -rf "$TARGET"; echo "cleared target (${{gb}} GB)"; fi',
        "}",
        "trim",
        "trap trim EXIT",
        'rm -rf "$CLONE" && mkdir -p "$CLONE" && tar -xf - -C "$CLONE" < ' + quote(archive) + ' || { echo "unpack=1"; exit 1; }',
        'export CARGO_TARGET_DIR="$TARGET"',
        f"export CARGO_BUILD_JOBS={int(jobs)}",
        "failed=0",
    ]
    for step in steps:
        run = step["run"]
        command = shlex.join(run) if isinstance(run, list) else run
        chain = ['cd "$CLONE"/' + quote(step["cwd"]) if step.get("cwd") else 'cd "$CLONE"']
        chain += [f"export {key}={quote(value)}" for key, value in (step.get("env") or {}).items()]
        chain.append(command.replace("{worktree}", "$CLONE"))
        log = quote(f"{out}/wsl_{step['name']}.log")
        lines.append(f"( {' && '.join(chain)} ) > {log} 2>&1; rc=$?")
        lines.append(f'echo "{step["name"]}=$rc"; [ $rc -eq 0 ] || failed=1')
    lines += ['rm -rf "$CLONE"', "exit $failed", ""]
    return "\n".join(lines)


def run_wsl(worktree: Path, *, out: Path | None = None, lock: _slots.GateLock | None = None,
            lock_every: float = _slots.DEFAULT_LOCK_EVERY, slot: "_slots.Slot | None" = None,
            slot_options: dict[str, Any] | None = None, say: Callable[[str], None] = print) -> bool:
    worktree = worktree.resolve()
    config = at.load(worktree)
    steps = config.get("gate", "wsl_steps", [])
    if not steps:
        raise GateError(f"{config.where('gate', 'wsl_steps')}: [gate] wsl_steps is required")
    prefix = wsl_prefix(config)
    problem = unfinished(worktree)
    if problem:
        raise GateError(f"{worktree} is {problem}: refusing to pass a partial tree")
    out = out or default_out(worktree)
    out.mkdir(parents=True, exist_ok=True)
    archive = out / "src.tar"
    git(worktree, "archive", "--format=tar", "-o", str(archive), "HEAD")
    script = out / "wsl.sh"
    script.write_text(wsl_script(config, worktree.name, steps, wsl_path(archive), wsl_path(out)),
                      encoding="utf-8", newline="")
    own_lock = lock is None
    lock = lock or lock_for(config, f"{worktree.name}-wsl", lock_every)
    if own_lock:
        lock.acquire()
    slot = slot or _slots.Slot(_slots.SlotConfig.from_config(config))
    try:
        options = {k: v for k, v in (slot_options or {}).items() if k in ("limit", "max_wait", "every", "settle", "poll")}
        env = dict(os.environ, MSYS_NO_PATHCONV="1")
        with (out / "wsl.verdict").open("w", encoding="utf-8", newline="") as verdict:
            code = slot.run([*prefix, "bash", wsl_path(script)], env=env, stdout=verdict, stderr=subprocess.STDOUT,
                            **options)
        text = (out / "wsl.verdict").read_text(encoding="utf-8", errors="replace")
    finally:
        archive.unlink(missing_ok=True)
        if own_lock:
            lock.release()
    results = dict(re.findall(r"^(\S+)=(\d+)$", text, re.MULTILINE))
    ok = code == 0
    for step in steps:
        passed = results.get(step["name"]) == "0"
        ok = ok and passed
        say(f"{'PASS' if passed else 'FAIL'} wsl:{step['name']}" + ("" if passed else f"  (see {out / ('wsl_' + step['name'] + '.log')})"))
    if "cleared target" in text:
        root = wsl_prefix(config, root=True)
        if root:  # hand the freed space back to the host's disk image
            subprocess.run([*root, "fstrim", "/"], capture_output=True)
    if "unpack=1" in text:
        say("FAIL wsl:unpack")
        ok = False
    return ok


# --- gate merge ----------------------------------------------------------------------------------------------


def repo_guard_command() -> list[str]:
    found = shutil.which("repo-guard")
    if found:
        return [found]
    root = os.environ.get("RELEASE_OVERLAY_ROOT")
    script = Path(root) / "bin" / "repo_guard.py" if root else None
    if script and script.is_file():
        return [sys.executable, str(script)]
    raise GateError("repo-guard is not available: install the release overlay")


def merge_branch(repo: Path, branch: str, *, push: bool = True, out: Path | None = None,
                 lock_every: float = _slots.DEFAULT_LOCK_EVERY, slot_options: dict[str, Any] | None = None,
                 say: Callable[[str], None] = print, guard_command: Callable[[], list[str]] = repo_guard_command) -> bool:
    """Fast-forward main to `branch`, run the gate and the Linux leg, and push through `repo-guard push`.
    Refuses a branch that is not ahead of main or whose commits carry agent attribution; on a failed check main
    stays advanced locally and the report says how to undo that."""
    repo = repo.resolve()
    config = at.load(repo)
    main = config.get("repo_guard", "main", DEFAULT_MAIN)
    current = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if current != main:
        raise GateError(f"{repo} is on {current}, not {main}")
    if subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", main, branch]).returncode:
        raise GateError(f"{branch} is not a fast-forward of {main}: rebase it first")
    for sha in git(repo, "rev-list", f"{main}..{branch}").splitlines():
        found = config.check_message(git(repo, "show", "-s", "--format=%B", sha))
        if found:
            raise GateError(f"agent attribution in commit {sha[:10]}: " + "; ".join(found))
    before = git(repo, "rev-parse", main)
    git(repo, "merge", "-q", "--ff-only", branch)
    say(git(repo, "log", "--oneline", "-1"))
    lock = lock_for(config, f"{repo.name}-merge", lock_every)
    lock.acquire()
    try:
        ok = run_gate(repo, out=out, lock=lock, slot_options=slot_options, say=say, fail_fast=True)
        if ok and config.get("gate", "wsl_steps"):
            ok = run_wsl(repo, out=out, lock=lock, slot_options=slot_options, say=say)
    finally:
        lock.release()
    if not ok:
        say(f"not pushed: {main} is ahead locally; undo with `git reset --hard {before}`")
        return False
    if not push:
        say("checks passed; not pushed (--no-push)")
        return True
    done = subprocess.run([*guard_command(), "push", "-C", str(repo)])
    return done.returncode == 0
