"""The configuration and the commit-message rule shared by the agent tools.

`repo-guard` (release overlay) and `build-slot`, `gate`, `merge`, `worktrees` (engineering overlay) read
the same two files and refuse the same commit messages.

    <project>/.agents/agent-tools.toml     private, per project   [repo_guard] [gate] [merge] [worktrees]
    ~/.agents/agent-tools.local.toml       private, per machine   [identity] [build_slot] [gate] [wsl]

The overlay holds no default that names a project, a path, a host or a person: a key a command needs and
the files leave out is a `ConfigError` naming the file and the key. A key the schema does not know is
refused with its file and path, so a misspelling is never silently ignored.

Standard library only; Python 3.11 or later.
"""

from __future__ import annotations

import os
import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_FILE = Path(".agents") / "agent-tools.toml"
LOCAL_NAME = "agent-tools.local.toml"
#: Names the local file when set; otherwise `$AGENTS_HOME/agent-tools.local.toml`, else `~/.agents/...`.
LOCAL_ENV = "AGENT_TOOLS_LOCAL"

# A schema value is a type, a tuple of types, a list holding the schema of its elements
# (a table, or a type), or a dict (a table). `Any` accepts anything.
_AFFECTED = {"affected": bool, "full_when": [str], "slow": [{"package": str, "skip": (str, list)}]}
_STEP = {"name": str, "run": (str, list), "when": [str], "env": {"*": str}, "slot": bool, "cwd": str, **_AFFECTED}
_WSL_STEP = {"name": str, "run": (str, list), "env": {"*": str}, "cwd": str, **_AFFECTED}
_RULE = {"path": str, "how": str, "run": (str, list)}

PROJECT_SCHEMA: dict[str, Any] = {
    "repo_guard": {
        "main": str,
        "remote": str,
        "checkout": str,
        "log": str,
        "pre_push": [(str, list)],
        "forbidden": [str],
        "ci_timeout": int,
        "ci_find_timeout": int,
        "scan_allow": [{"path": str, "ref": str, "kind": str, "reason": str}],
    },
    "gate": {"steps": [_STEP], "restore": [str], "wsl_steps": [_WSL_STEP]},
    "merge": {"rules": [_RULE]},
    "worktrees": {"root": str},
}

LOCAL_SCHEMA: dict[str, Any] = {
    "identity": {"name": str, "email": str},
    "build_slot": {"limit": int, "processes": [str], "wsl": [str], "dir": str},
    "gate": {"lock": str},
    "wsl": {"distro": str, "home": str, "target_max_gb": (int, float), "jobs": int, "exec": [str]},
}


class ConfigError(Exception):
    """A file that cannot be read, a key the schema refuses, or a required key that is missing."""


def local_path() -> Path:
    override = os.environ.get(LOCAL_ENV)
    if override:
        return Path(override)
    home = os.environ.get("AGENTS_HOME")
    return (Path(home) if home else Path.home() / ".agents") / LOCAL_NAME


def _kind(want: Any) -> str:
    if isinstance(want, tuple):
        return " or ".join(_kind(w) for w in want)
    if isinstance(want, type):
        return {str: "a string", int: "an integer", float: "a number", bool: "true or false",
                list: "a list"}.get(want, want.__name__)
    if isinstance(want, list):
        return "a list"
    return "a table"


def _check(value: Any, want: Any, where: str, path: Path) -> None:
    if want is Any:
        return
    if isinstance(want, dict):
        if not isinstance(value, dict):
            raise ConfigError(f"{path}: {where} must be a table")
        wild = want.get("*")
        for key, item in value.items():
            if key in want:
                _check(item, want[key], f"{where}.{key}", path)
            elif wild is not None:
                _check(item, wild, f"{where}.{key}", path)
            else:
                known = ", ".join(sorted(k for k in want if k != "*"))
                raise ConfigError(f"{path}: unknown key {where}.{key} (known: {known})")
        return
    if isinstance(want, list):
        if not isinstance(value, list):
            raise ConfigError(f"{path}: {where} must be a list")
        for index, item in enumerate(value):
            _check(item, want[0], f"{where}[{index}]", path)
        return
    kinds = want if isinstance(want, tuple) else (want,)
    # bool is an int in Python; an integer key must not accept true or false.
    if isinstance(value, bool) and bool not in kinds:
        raise ConfigError(f"{path}: {where} must be {_kind(want)}")
    if not isinstance(value, kinds):
        raise ConfigError(f"{path}: {where} must be {_kind(want)}")


def _read(path: Path, schema: dict[str, Any]) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise ConfigError(f"{path}: cannot be read: {error}") from None
    for table, value in data.items():
        if table not in schema:
            known = ", ".join(sorted(schema))
            raise ConfigError(f"{path}: unknown table [{table}] (known: {known})")
        _check(value, schema[table], table, path)
    return data


@dataclass
class Config:
    """The merged tables, remembering which file each came from."""

    project_path: Path | None = None
    local_path: Path | None = None
    _tables: dict[str, dict[str, Any]] = field(default_factory=dict)
    _origin: dict[tuple[str, str], Path] = field(default_factory=dict)

    def table(self, name: str) -> dict[str, Any]:
        """The merged table, empty when neither file has it."""
        return dict(self._tables.get(name, {}))

    def get(self, table: str, key: str, default: Any = None) -> Any:
        return self._tables.get(table, {}).get(key, default)

    def where(self, table: str, key: str) -> str:
        """The file that holds the key, or the file that would."""
        origin = self._origin.get((table, key))
        if origin is not None:
            return str(origin)
        if table in LOCAL_SCHEMA and key in LOCAL_SCHEMA[table]:
            return str(self.local_path or local_path())
        return str(self.project_path or PROJECT_FILE)

    def require(self, table: str, key: str) -> Any:
        """The key's value; a `ConfigError` naming the file and key when it is missing or empty."""
        value = self.get(table, key)
        if value in (None, "", []):
            raise ConfigError(f"{self.where(table, key)}: [{table}] {key} is required")
        return value

    def identity(self) -> tuple[str, str]:
        return self.require("identity", "name"), self.require("identity", "email")

    def forbidden_trailers(self) -> list[re.Pattern]:
        extra = self.get("repo_guard", "forbidden", [])
        patterns = forbidden_trailers()
        for source in extra:
            try:
                patterns.append(re.compile(source, re.IGNORECASE | re.MULTILINE))
            except re.error as error:
                raise ConfigError(f"{self.where('repo_guard', 'forbidden')}: [repo_guard] forbidden: "
                                  f"{source!r} is not a regular expression ({error})") from None
        return patterns

    def check_message(self, text: str) -> list[str]:
        return check_message(text, self.forbidden_trailers())


def project_root(worktree: Path | str) -> Path:
    """The directory whose `.agents/agent-tools.toml` governs `worktree`: itself when it has one, else the
    main checkout of a linked worktree (`.agents` is private, so a worktree never carries it)."""
    path = Path(worktree)
    if (path / PROJECT_FILE).is_file():
        return path
    try:
        common = subprocess.run(["git", "-C", str(path), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                capture_output=True, text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return path
    main = Path(common).parent if common else path
    return main if (main / PROJECT_FILE).is_file() else path


def load(project: Path | str | None = None, local: Path | str | None = None) -> Config:
    """Both files, merged. A missing file is an empty one; a table in a file that is not that file's
    to hold is an unknown table."""
    project_file = project_root(project) / PROJECT_FILE if project is not None else None
    local_file = Path(local) if local is not None else local_path()
    config = Config(project_path=project_file, local_path=local_file)
    sources = [(local_file, LOCAL_SCHEMA)]
    if project_file is not None:
        sources.append((project_file, PROJECT_SCHEMA))
    for path, schema in sources:
        for table, value in _read(path, schema).items():
            into = config._tables.setdefault(table, {})
            for key in value:
                if key in into:
                    raise ConfigError(f"{path}: [{table}] {key} is also set in {config._origin[(table, key)]}")
                config._origin[(table, key)] = path
            into.update(value)
    return config


# --- the commit-message rule ---------------------------------------------------------------------------
#
# What `~/.claude/CLAUDE.md` and `~/.agents/AGENTS.md` forbid in a commit message or a pull request body:
# a `Co-Authored-By:` naming a model or an assistant, any `*-Session:` trailer, a session URL and a
# "Generated with" footer. A co-author line naming a person is ordinary and passes.

_ASSISTANT = (
    r"claude|anthropic|openai|chatgpt|\bgpt[-\w.]*|codex|copilot|gemini|devin|aider|antigravity|"
    r"\bllm\b|\bassistant\b"
)

_RULES: list[tuple[str, str]] = [
    ("a Co-Authored-By naming a model or an assistant",
     rf"^[ \t]*co-authored-by[ \t]*:[^\n]*(?:{_ASSISTANT})"),
    ("a *-Session: trailer", r"^[ \t]*[A-Za-z][\w-]*-session[ \t]*:"),
    ("a session URL", r"claude\.ai/code/\S+|\b(?:claude\.ai|chatgpt\.com|chat\.openai\.com)/\S*session\S*"),
    ("a \"Generated with\" footer", r"^[ \t]*(?:\U0001F916[ \t]*)?generated[ \t]+with\b"),
    ("a robot-emoji footer", r"^[ \t]*\U0001F916"),
]


def forbidden_trailers() -> list[re.Pattern]:
    """The patterns no commit message may match, each compiled line-anchored and case-insensitive."""
    return [re.compile(source, re.IGNORECASE | re.MULTILINE) for _, source in _RULES]


def _label(pattern: re.Pattern) -> str:
    for label, source in _RULES:
        if pattern.pattern == source:
            return label
    return f"a line matching {pattern.pattern!r}"


def check_message(text: str, patterns: list[re.Pattern] | None = None) -> list[str]:
    """One line per forbidden line of `text`: `line N: what: 'the line'`. Empty when the message is fine."""
    found: list[str] = []
    patterns = patterns if patterns is not None else forbidden_trailers()
    lines = text.splitlines()
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line) + 1)
    seen: set[tuple[int, str]] = set()
    for pattern in patterns:
        for hit in pattern.finditer(text):
            index = max(i for i, start in enumerate(starts) if start <= hit.start())
            label = _label(pattern)
            if (index, label) in seen:
                continue
            seen.add((index, label))
            shown = lines[index].strip() if index < len(lines) else hit.group(0)
            found.append(f"line {index + 1}: {label}: {shown[:100]!r}")
    return sorted(found, key=lambda line: int(line.split(":", 1)[0].split()[1]))
