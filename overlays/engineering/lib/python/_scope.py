"""Pure-Python scope resolution for standalone overlay skill commands."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from duho import Cmd


@dataclass(frozen=True)
class ScopeInfo:
    root: Path
    agents_root: Path
    scope: str = "project"


def resolve_project_scope(cwd: Optional[Path] = None) -> ScopeInfo:
    env_root = os.environ.get("AGENTS_PROJECT_ROOT") or os.environ.get("AGENTS_SCOPE_ROOT")
    if env_root:
        root = Path(env_root).expanduser().resolve()
        return ScopeInfo(root=root, agents_root=root / ".agents")

    start = (cwd or Path.cwd()).resolve()
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
