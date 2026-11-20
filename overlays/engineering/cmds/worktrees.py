"""`dotagents worktrees` -- find the worktrees whose work is already upstream, and remove them.

    dotagents worktrees [-C DIR] [--root GLOB] [--remove]

Lists each linked worktree of the repository as `merged` (every commit's patch is in REMOTE/MAIN, as a
cherry-picked stack leaves it, checked with `git cherry`), `open` (with the count of commits not upstream) or
`dirty`, each with the size of its `target/` directory. `--root GLOB` (default `[worktrees] root` of
`<project>/.agents/agent-tools.toml`, else every linked worktree) limits it to worktrees whose path matches; `**`
crosses directories, `*` does not. `--remove` runs `git worktree remove` on each merged one and skips a worktree
with uncommitted changes. The main worktree is never listed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from duho import Cmd, LoggingArgs

_LIB = str(Path(__file__).resolve().parents[1] / "lib" / "python")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import _gate  # noqa: E402
import agent_tools  # noqa: E402


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace")


def linked_worktrees(repo: Path) -> "list[tuple[Path, str | None]]":
    """(path, branch or None when detached) of every worktree but the first, which is the main one."""
    found: "list[tuple[Path, str | None]]" = []
    path = None
    branch = None
    for line in _git(repo, "worktree", "list", "--porcelain").stdout.splitlines() + [""]:
        if line.startswith("worktree "):
            path, branch = Path(line[9:]), None
        elif line.startswith("branch "):
            branch = line[7:].removeprefix("refs/heads/")
        elif not line and path is not None:
            found.append((path, branch))
            path = None
    return found[1:]


def dir_size(path: Path) -> int:
    total = 0
    for current, _, names in os.walk(path):
        for name in names:
            try:
                total += os.lstat(os.path.join(current, name)).st_size
            except OSError:
                pass
    return total


def human(size: int) -> str:
    value = float(size)
    for unit in ("B", "K", "M", "G", "T"):
        if value < 1024 or unit == "T":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{size}B"


class Worktrees(LoggingArgs, Cmd):
    """List worktrees whose commits are all upstream, with their target sizes; --remove removes them."""

    _parsername_ = "worktrees"

    repo: Path = Path(".")
    "A worktree of the repository (default: the current directory)."
    ("-C",)

    root: str = ""
    "Only worktrees whose path matches this glob (default: [worktrees] root, else all)."
    ("--root",)

    remove: bool = False
    "Remove each merged worktree that has no uncommitted changes."
    ("--remove",)

    def __call__(self) -> int:
        repo = Path(self.repo).resolve()
        try:
            config = agent_tools.load(repo)
        except agent_tools.ConfigError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        remote = config.get("repo_guard", "remote", _gate.DEFAULT_REMOTE)
        main = config.get("repo_guard", "main", _gate.DEFAULT_MAIN)
        pattern = self.root or config.get("worktrees", "root", "")
        regex = _gate.glob_regex(pattern) if pattern else None
        _git(repo, "fetch", "-q", remote)
        upstream = f"{remote}/{main}"
        for path, branch in linked_worktrees(repo):
            if regex is not None and not regex.match(path.as_posix()):
                continue
            label = branch or "(detached)"
            extra = sum(1 for line in _git(repo, "cherry", upstream, branch or str(_git(path, "rev-parse", "HEAD").stdout.strip())).stdout.splitlines()
                        if line.startswith("+"))
            target = path / "target"
            size = human(dir_size(target)) if target.is_dir() else "0"
            if extra:
                print(f"open    {label}  {size}  ({extra} commits not upstream)")
                continue
            dirty = bool(_git(path, "status", "--porcelain").stdout.strip())
            print(f"merged  {label}  {size}" + ("  (uncommitted changes)" if dirty else ""))
            if self.remove:
                if dirty:
                    print(f"  skipped {path}: uncommitted changes")
                    continue
                done = _git(repo, "worktree", "remove", str(path))
                print(f"  removed {path}" if not done.returncode else f"  could not remove {path}: {done.stderr.strip()}")
        return 0
