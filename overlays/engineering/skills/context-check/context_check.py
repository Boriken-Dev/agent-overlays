"""`context-check` -- warn about agent context files outside the standard places.

    context-check [path ...] [--allow GLOB ...]

A project keeps the instructions for its agents in one place (the Agent Overlay
Specification's "Project context files"): ``AGENTS.md`` and ``AGENTS.*.md`` in a
directory, and the same names in that directory's ``.agents``. ``AGENTS.local.md``
and everything under ``.agents`` are the user's own and never committed. The names
and places can be changed with ``AGENTS_DIR`` and ``AGENTS_CONTEXT``, lists in the
platform's path-list separator; this check reads them as the specification does.

It looks at what a commit would hold (tracked files and untracked ones that are not
ignored; every file when there is no git) and reports:

* ``foreign``  another tool's context file (``CLAUDE.md``, ``GEMINI.md``,
  ``.cursorrules``, ``.github/copilot-instructions.md``, ...), with the move;
* ``name``     a context file with the wrong case or extension (``agents.md``);
* ``private``  a file under ``.agents`` or an ``AGENTS.local.md``, which is the
  user's own and not for the repository.

Exit codes: 0 nothing to report, 1 findings, 2 usage, 3 nothing could be checked.
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from pathlib import Path, PurePosixPath

from duho import Cli, Cmd, LoggingArgs

EXIT_OK = 0
EXIT_FOUND = 1
EXIT_CANNOT_CHECK = 3

#: Used only where git cannot list the files.
PRUNE_DIRS = {
    ".git", ".venv", ".pyvenv", "__pycache__", "node_modules", "target", "build", "dist",
    ".pytest_cache", ".mypy_cache", ".ruff_cache",
}

DEFAULT_DIRECTORIES = (".agents",)
DEFAULT_PATTERNS = ("AGENTS.md", "AGENTS.*.md")

#: Another tool's context file, by the path below the project (lower case) it is found at, and whose it is. A pattern with a
#: `/` matches the whole path; one without matches a file name at any depth.
FOREIGN = (
    ("claude.md", "Claude Code"),
    ("claude.local.md", "Claude Code"),
    (".claude/claude.md", "Claude Code"),
    ("gemini.md", "Gemini CLI"),
    ("qwen.md", "Qwen Code"),
    ("agent.md", "an agent that reads AGENT.md"),
    (".cursorrules", "Cursor"),
    (".cursor/rules/*", "Cursor"),
    (".windsurfrules", "Windsurf"),
    (".clinerules", "Cline"),
    (".github/copilot-instructions.md", "GitHub Copilot"),
)

#: Names that are meant to be `AGENTS.md` and are not.
_MISSPELLED = re.compile(r"^agents(\.[^.]+)*\.(md|markdown|txt|rst)$", re.IGNORECASE)


def _entries(value: "str | None", directory: bool) -> "list[str]":
    """The valid entries of ``AGENTS_DIR`` or ``AGENTS_CONTEXT``, once each; empty when none is valid."""
    kept: "list[str]" = []
    for entry in (value or "").split(os.pathsep):
        if not entry or "/" in entry or "\\" in entry or (directory and entry in (".", "..")):
            continue
        if entry not in kept:
            kept.append(entry)
    return kept


def directories() -> "list[str]":
    """The directories, inside a project directory, that hold private context files."""
    return _entries(os.environ.get("AGENTS_DIR"), True) or list(DEFAULT_DIRECTORIES)


def patterns() -> "list[str]":
    """The name patterns of a place's context files."""
    return _entries(os.environ.get("AGENTS_CONTEXT"), False) or list(DEFAULT_PATTERNS)


def is_local(name: str) -> bool:
    """Whether ``name`` is ``<stem>.local.<extension>``: the user's own file."""
    return re.match(r"^.+\.local\.[^.]+$", name, re.DOTALL) is not None


def matches(pattern: str, name: str) -> bool:
    """A name pattern: ``*`` is any run of characters, none included, and the rest is literal."""
    return re.fullmatch(".*".join(re.escape(part) for part in pattern.split("*")), name,
                        re.DOTALL) is not None


def _foreign(path: str) -> "str | None":
    lower = path.lower()
    name = lower.rsplit("/", 1)[-1]
    for pattern, tool in FOREIGN:
        if "/" in pattern:
            if fnmatch.fnmatchcase(lower, pattern) or lower.endswith("/" + pattern):
                return tool
        elif name == pattern:
            return tool
    return None


def check(paths: "list[str]", allow: "tuple[str, ...] | list[str]" = ()) -> "list[tuple[str, str, str]]":
    """``(kind, path, what to do)`` for each file of ``paths`` (relative, with ``/``) outside the standard places."""
    private_dirs = directories()
    context = patterns()
    found: "list[tuple[str, str, str]]" = []
    for path in paths:
        if any(fnmatch.fnmatchcase(path, glob) for glob in allow):
            continue
        parts = PurePosixPath(path).parts
        name = parts[-1]
        tool = _foreign(path)
        if tool is not None:
            where = "AGENTS.md" if len(parts) == 1 else "%s/AGENTS.md" % "/".join(parts[:-1])
            found.append((
                "foreign", path,
                "%s's own file: move the text into %s (the host feeds it to an agent that does not "
                "read AGENTS.md), or into %s when it is private"
                % (tool, where, "%s/%s" % (private_dirs[0], "AGENTS.md")),
            ))
            continue
        under_private = name in private_dirs or any(part in private_dirs for part in parts[:-1])
        if under_private:
            found.append(("private", path,
                          "the user's own working area is not committed: add it to .gitignore"))
            continue
        if any(matches(pattern, name) for pattern in context) and is_local(name):
            found.append(("private", path,
                          "a .local file is the user's own and is not committed: add it to .gitignore"))
            continue
        if _MISSPELLED.match(name) and not any(matches(pattern, name) for pattern in [*context, *DEFAULT_PATTERNS]):
            found.append(("name", path,
                          "context files are named %s: rename it" % " or ".join(context)))
    return found


def _git(args: "list[str]") -> "subprocess.CompletedProcess":
    return subprocess.run(["git", *args], capture_output=True, stdin=subprocess.DEVNULL,
                          timeout=120)


def listing(root: Path) -> "list[str]":
    """The files under ``root`` a commit would hold, relative to it with ``/``: git's view where there is one."""
    try:
        inside = _git(["-C", str(root), "rev-parse", "--is-inside-work-tree"])
        if inside.returncode == 0 and inside.stdout.strip() == b"true":
            out = _git(["-C", str(root), "ls-files", "--cached", "--others",
                        "--exclude-standard", "-z"])
            if out.returncode == 0:
                names = out.stdout.decode("utf-8", "surrogateescape").split("\0")
                return sorted({name for name in names if name})
    except (OSError, subprocess.TimeoutExpired):
        pass
    names = []
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in PRUNE_DIRS)
        for name in sorted(files):
            names.append(Path(os.path.relpath(os.path.join(current, name), root)).as_posix())
    return names


class ContextCheck(LoggingArgs, Cli):
    """Warn about agent context files outside the standard places."""

    _parsername_ = "context-check"

    paths: "list[Path]" = []
    "Project directories to examine (default: the current directory)."
    ("paths",)

    allow: "list[str]" = []
    "A path pattern (relative to the project, `*` crosses `/`) to leave out of the report, such as a CLAUDE.md that only imports AGENTS.md."
    ("--allow",)

    def __call__(self) -> int:
        checked = 0
        found = 0
        for given in (list(self.paths) or [Path(".")]):
            root = Path(given)
            if not root.is_dir():
                raise SystemExit("error: no such directory: %s" % root)
            names = listing(root)
            checked += len(names)
            for kind, path, advice in check(names, list(self.allow)):
                found += 1
                print("%-7s %s: %s" % (kind, (root / path).as_posix(), advice))
        if not checked:
            print("CANNOT CHECK: no file found")
            return EXIT_CANNOT_CHECK
        print("%d files: %s" % (checked, "%d to move or rename" % found if found
                                else "context files are in the standard places"))
        return EXIT_FOUND if found else EXIT_OK


if __name__ == "__main__":
    from duho import main as _duho_main

    raise SystemExit(_duho_main(ContextCheck))
