"""Pure-Python scope resolution for standalone overlay skill commands."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from duho import Cmd


@dataclass(frozen=True)
class ScopeInfo:
    root: Path
    agents_root: Path
    scope: str = "project"


#: Set to a non-empty value to keep ``AGENTS_PROJECT_ROOT`` even when the current directory is inside a
#: different project.
PIN_ENV = "AGENTS_SCOPE_PIN"


def _global_agents() -> "set[Path]":
    stores = {(Path.home() / ".agents").resolve()}
    home = os.environ.get("AGENTS_HOME")
    if home:
        stores.add(Path(home).expanduser().resolve())
    return stores


def _other_project(start: Path, pinned: Path) -> Optional[Path]:
    """The nearest project above ``start`` with its own ``.agents`` when it is not ``pinned``.

    ``None`` when ``start`` is inside ``pinned`` or in no project. The user's global store is not a project."""
    if start == pinned or pinned in start.parents:
        return None
    stores = _global_agents()
    for candidate in (start, *start.parents):
        agents = candidate / ".agents"
        if agents.is_dir() and agents.resolve() not in stores:
            return candidate
    return None


def resolve_project_scope(cwd: Optional[Path] = None) -> ScopeInfo:
    start = (cwd or Path.cwd()).resolve()
    env_root = os.environ.get("AGENTS_PROJECT_ROOT") or os.environ.get("AGENTS_SCOPE_ROOT")
    if env_root:
        root = Path(env_root).expanduser().resolve()
        # A session pins the root of the project it started in; a command run from another project's
        # directory means that project.
        other = None if os.environ.get(PIN_ENV) else _other_project(start, root)
        if other is None:
            return ScopeInfo(root=root, agents_root=root / ".agents")
        print("note: %s is inside another project than AGENTS_PROJECT_ROOT (%s); using %s "
              "(set %s=1 to keep the pinned root)" % (start, root, other, PIN_ENV), file=sys.stderr)
        return ScopeInfo(root=other, agents_root=other / ".agents")

    for candidate in (start, *start.parents):
        if (candidate / ".agents").is_dir():
            return ScopeInfo(root=candidate, agents_root=candidate / ".agents")

    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(start),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            git_root = Path(proc.stdout.strip()).resolve()
            return ScopeInfo(root=git_root, agents_root=git_root / ".agents")
    except OSError:
        pass

    return ScopeInfo(root=start, agents_root=start / ".agents")


class StandaloneScopeArgs(Cmd):
    """Base class providing `resolve_scope()` for project and global `.agents` directories."""

    global_scope: bool = False
    "Operate on the user global store instead of the project."
    ("--global", "-g")

    def resolve_scope(self) -> ScopeInfo:
        if self.global_scope:
            home = os.environ.get("AGENTS_HOME")
            root = Path(home).expanduser().resolve() if home else (Path.home() / ".agents")
            return ScopeInfo(root=root, agents_root=root, scope="global")
        return resolve_project_scope()
