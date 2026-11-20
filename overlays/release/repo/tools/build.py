#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# From Boriken-Dev/agent-overlays, overlays/release/repo/tools/build.py, at commit SOURCE.
# Copyright (c) 2026 Jose A.
"""One build tool for a repository: vendored crates, scans, SDK lock, offline and release builds.

This file is copied into a repository as tools/build.py and configured by tools/build.cfg; a section the
configuration leaves out is off. Standard library only, Python 3.11.0 or later.

    build.py help [config]        the command surface, or every build.cfg key with its default
    build.py self check|update    keep this copy equal to the one it was taken from
    build.py deps ...             the deps branch: crates, pub packages and CDN files
    build.py scan ...             refuse archives, executables, keys and opaque binaries
    build.py sdk ...              the SDK lock: lint, agree with the repository, install
    build.py offline plan         what an offline build of the repository needs, as JSON
    build.py release ...          release binaries, a universal macOS binary, container images
    build.py flutter ...          plugin junctions and the pinned Flutter checkout

Exit codes: 0 ok, 1 problems (one line each on stderr, prefixed `build.py:`), 2 usage.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, NamedTuple

CONFIG = "tools/build.cfg"
SELF_NAME = "build.py"
UPSTREAM_DIR = "overlays/release/repo/tools"
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")


class Problem(Exception):
    """What stops a command; each line of its text is printed as one problem."""


class Usage(Exception):
    """A command line that cannot be run, which exits 2."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run_git(*args: str, cwd: Path | None = None, binary: bool = False) -> Any:
    """Runs git and returns its stdout stripped (bytes when `binary`); a failure is a Problem."""
    command = ["git", *(["-C", str(cwd)] if cwd else []), *args]
    try:
        done = subprocess.run(command, capture_output=True)
    except FileNotFoundError as error:
        raise Problem("git is not installed") from error
    if done.returncode != 0:
        raise Problem(f"git {' '.join(args)}: {done.stderr.decode('utf-8', 'replace').strip()}")
    return done.stdout if binary else done.stdout.decode("utf-8").strip()


def git_ok(*args: str, cwd: Path | None = None) -> bool:
    command = ["git", *(["-C", str(cwd)] if cwd else []), *args]
    return subprocess.run(command, capture_output=True).returncode == 0


# Network settings, replaced from `[net]` once the configuration is read.
NET = {"retries": 3, "timeout": 120, "user_agent": "build.py"}


def fetch(url: str) -> bytes:
    """The body of a URL, retried on a connection failure or a server error."""
    request = urllib.request.Request(url, headers={"User-Agent": NET["user_agent"]})
    failure: Exception | None = None
    for _ in range(max(1, NET["retries"])):
        try:
            with urllib.request.urlopen(request, timeout=NET["timeout"]) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code < 500:
                raise Problem(f"{url}: {error}") from error
            failure = error
        except OSError as error:
            failure = error
    raise Problem(f"{url}: {failure}")


# --- configuration ---------------------------------------------------------------------------

REQUIRED = object()


class Key(NamedTuple):
    """One build.cfg key. `kind` is str, int, bool, strs (list of strings), map (string to string),
    table (fixed keys) or tables (a list of tables with fixed keys)."""

    kind: str
    default: Any
    doc: str
    keys: dict[str, "Key"] | None = None


class Table(dict):
    """A validated table whose keys read as attributes."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as error:
            raise AttributeError(name) from error


# Each section adds its own table here when it is defined below.
SCHEMA: dict[str, Key] = {}
SCHEMA["tools"] = Key(
    "table",
    None,
    "The files copied from the overlay into this repository.",
    {
        "files": Key("strs", ["build.py"], "Files under tools/ that `self check` holds equal to the overlay's."),
        "source": Key("str", "Boriken-Dev/agent-overlays", "The GitHub repository the files come from."),
    },
)
SCHEMA["net"] = Key(
    "table",
    None,
    "Downloads.",
    {
        "retries": Key("int", 3, "Attempts per download."),
        "timeout": Key("int", 120, "Seconds before one attempt is abandoned."),
        "user_agent": Key("str", "build.py", "The User-Agent header."),
    },
)


def default_of(key: Key) -> Any:
    if key.kind == "table":
        return Table({name: default_of(child) for name, child in (key.keys or {}).items()})
    if key.kind == "tables":
        return []
    if key.default is REQUIRED:
        return None
    return list(key.default) if isinstance(key.default, list) else dict(key.default) if isinstance(key.default, dict) else key.default


def _check(value: Any, key: Key, path: str, errors: list[str]) -> Any:
    kind = key.kind
    if kind == "str" and isinstance(value, str):
        return value
    if kind == "int" and isinstance(value, int) and not isinstance(value, bool):
        return value
    if kind == "bool" and isinstance(value, bool):
        return value
    if kind == "strs" and isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    if kind == "map" and isinstance(value, dict) and all(isinstance(v, str) for v in value.values()):
        return dict(value)
    if kind == "table" and isinstance(value, dict):
        return _check_table(value, key.keys or {}, path, errors)
    if kind == "tables" and isinstance(value, list) and all(isinstance(v, dict) for v in value):
        return [_check_table(v, key.keys or {}, f"{path}[{i}]", errors) for i, v in enumerate(value)]
    errors.append(f"{path}: expected {key.kind}, found {type(value).__name__}")
    return default_of(key)


def _check_table(table: dict, keys: dict[str, Key], path: str, errors: list[str]) -> Table:
    out = Table()
    for name in table:
        if name not in keys:
            errors.append(f"{path + '.' if path else ''}{name}: unknown key")
    for name, key in keys.items():
        where = f"{path + '.' if path else ''}{name}"
        if name in table:
            out[name] = _check(table[name], key, where, errors)
        elif key.default is REQUIRED:
            errors.append(f"{where}: required")
            out[name] = None
        else:
            out[name] = default_of(key)
    return out


def parse_config(text: str, name: str = "build.cfg") -> Table:
    """The configuration with every default filled in; unknown keys are refused with their path."""
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise Problem(f"{name}: {error}") from error
    errors: list[str] = []
    table = _check_table(raw, SCHEMA, "", errors)
    if errors:
        raise Problem("\n".join(f"{name}: {line}" for line in errors))
    NET.update({k: table.net[k] for k in ("retries", "timeout", "user_agent")})
    return table


def load_config(root: Path) -> Table:
    path = root / CONFIG
    return parse_config(path.read_text(encoding="utf-8") if path.is_file() else "", CONFIG)


def config_help() -> str:
    """The table `parse_config` validates against: every key, its kind, default and meaning."""
    lines: list[str] = []

    def shown(key: Key) -> str:
        if key.default is REQUIRED:
            return "required"
        return repr(key.default) if key.kind not in ("table", "tables") else ""

    def walk(keys: dict[str, Key], path: str) -> None:
        for name, key in keys.items():
            where = f"{path}.{name}" if path else name
            if key.kind == "table":
                lines.append(f"[{where}]  {key.doc}")
                walk(key.keys or {}, where)
            elif key.kind == "tables":
                lines.append(f"[[{where}]]  {key.doc}")
                walk(key.keys or {}, where)
            else:
                lines.append(f"  {where}  ({key.kind}, {shown(key)})  {key.doc}")

    walk(SCHEMA, "")
    return "\n".join(lines)


class Ctx:
    """The repository a command runs on: its root and, read when first asked for, its configuration."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._cfg: Table | None = None

    @property
    def cfg(self) -> Table:
        if self._cfg is None:
            self._cfg = load_config(self.root)
        return self._cfg


# --- self: keep this copy equal to the overlay's ----------------------------------------------

HEADER = re.compile(rb"^# From (?P<repo>\S+), (?P<path>\S+), at commit (?P<commit>SOURCE|[0-9a-f]{40})\.$")


def header_of(data: bytes) -> re.Match[bytes] | None:
    lines = data.split(b"\n", 3)
    return HEADER.match(lines[2]) if len(lines) > 3 else None


def upstream(cfg: Table, commit: str, name: str, local: Path | None) -> bytes:
    """A file of the overlay's repository at a commit: from a local checkout, else from GitHub."""
    path = f"{UPSTREAM_DIR}/{name}"
    if local is not None:
        return run_git("show", f"{commit}:{path}", cwd=local, binary=True)
    return fetch(f"https://raw.githubusercontent.com/{cfg.tools.source}/{commit}/{path}")


def self_problems(root: Path, cfg: Table, local: Path | None = None) -> list[str]:
    """One line for each way the listed files differ from the overlay's, or are not what they claim."""
    found: list[str] = []
    listed = list(cfg.tools.files)
    commits: dict[str, list[str]] = {}
    copies: dict[str, bytes] = {}
    for name in listed:
        path = root / "tools" / name
        if not path.is_file():
            found.append(f"tools/{name}: listed in [tools] files, missing")
            continue
        data = path.read_bytes()
        match = header_of(data)
        if match is None:
            found.append(f"tools/{name}: line 3 is not '# From REPO, PATH, at commit COMMIT.'")
            continue
        if match["commit"] == b"SOURCE":
            found.append(f"tools/{name}: line 3 still says SOURCE; make the copy with `self update`")
            continue
        if match["repo"].decode() != cfg.tools.source or match["path"].decode() != f"{UPSTREAM_DIR}/{name}":
            found.append(f"tools/{name}: line 3 names {match['repo'].decode()}, {match['path'].decode()}")
            continue
        commits.setdefault(match["commit"].decode(), []).append(name)
        copies[name] = data
    tools = root / "tools"
    prefix = f"# From {cfg.tools.source}, {UPSTREAM_DIR}/".encode()
    if tools.is_dir():
        for path in sorted(p for p in tools.iterdir() if p.is_file() and p.name not in listed):
            if prefix in b"\n".join(path.read_bytes()[:2000].split(b"\n")[:4]):
                found.append(f"tools/{path.name}: has the overlay's header but is not in [tools] files")
    if len(commits) > 1:
        found.append("copies name different commits: " + "; ".join(f"{c[:12]} ({', '.join(n)})" for c, n in sorted(commits.items())))
    for commit, names in sorted(commits.items()):
        for name in names:
            try:
                expected = upstream(cfg, commit, name, local)
            except Problem as problem:
                found.append(f"tools/{name}: cannot read the overlay at {commit[:12]}: {problem}")
                continue
            lines = copies[name].split(b"\n")
            lines[2] = lines[2].replace(f"at commit {commit}.".encode(), b"at commit SOURCE.")
            actual = b"\n".join(lines)
            if actual != expected:
                theirs = expected.split(b"\n")
                where = next((i + 1 for i, (a, b) in enumerate(zip(actual.split(b"\n"), theirs)) if a != b), min(len(theirs), len(lines)) + 1)
                found.append(f"tools/{name}: differs from the overlay at {commit[:12]} (first at line {where})")
    return found


def resolve_commit(cfg: Table, rev: str, local: Path | None) -> str:
    if local is not None:
        return run_git("rev-parse", "--verify", f"{rev}^{{commit}}", cwd=local)
    if HEX40.match(rev):
        return rev
    listing = run_git("ls-remote", f"https://github.com/{cfg.tools.source}.git", rev, f"refs/heads/{rev}", f"refs/tags/{rev}^{{}}", f"refs/tags/{rev}")
    for row in listing.splitlines():
        commit, _, ref = row.partition("\t")
        if HEX40.match(commit) and (ref == rev or ref.endswith(f"/{rev}") or ref.endswith(f"/{rev}^{{}}")):
            return commit
    raise Problem(f"{cfg.tools.source} has no {rev}")


def self_check(ctx: Ctx, args: argparse.Namespace) -> int:
    found = self_problems(ctx.root, ctx.cfg, args.src)
    if found:
        raise Problem("\n".join(found))
    names = ", ".join(ctx.cfg.tools.files)
    print(f"ok: {names} equal the overlay's")
    return 0


def self_update(ctx: Ctx, args: argparse.Namespace) -> int:
    cfg = ctx.cfg
    commit = resolve_commit(cfg, args.rev, args.src)
    data = {name: upstream(cfg, commit, name, args.src) for name in cfg.tools.files}
    for name, body in data.items():
        lines = body.split(b"\n")
        if len(lines) < 4 or not (match := HEADER.match(lines[2])) or match["commit"] != b"SOURCE":
            raise Problem(f"the overlay's {name} has no 'at commit SOURCE.' on line 3")
        lines[2] = lines[2].replace(b"at commit SOURCE.", f"at commit {commit}.".encode())
        data[name] = b"\n".join(lines)
    tools = ctx.root / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    for name, body in data.items():
        (tools / name).write_bytes(body)
        print(f"wrote tools/{name} at {commit[:12]}")
    found = self_problems(ctx.root, cfg, args.src)
    if found:
        raise Problem("\n".join(found))
    print(f"ok: {len(data)} file(s) at {commit}")
    return 0


def add_self(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("self", help="keep this copy equal to the overlay's")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    check = commands.add_parser("check", help="fail when a listed file differs from the overlay's at the commit it names")
    check.add_argument("--from", dest="src", type=Path, metavar="DIR", help="read a local checkout of the overlay's repository instead of GitHub")
    check.set_defaults(run=self_check)
    update = commands.add_parser("update", help="write every listed file at REV, then check")
    update.add_argument("rev", metavar="REV", help="a commit, branch or tag of the overlay's repository")
    update.add_argument("--from", dest="src", type=Path, metavar="DIR", help="read a local checkout (git -C DIR show) instead of GitHub")
    update.set_defaults(run=self_update)


# --- help --------------------------------------------------------------------------------------


def surface(parser: argparse.ArgumentParser, depth: int = 0) -> list[str]:
    """Every command with its arguments, one block each, read from the parser itself."""
    lines: list[str] = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            helps = {choice.dest: choice.help for choice in action._choices_actions}
            for name, child in action.choices.items():
                usage = child.format_usage().replace("usage: ", "", 1).strip()
                lines.append(f"{usage}\n    {helps.get(name) or child.description or ''}".rstrip())
                lines += surface(child, depth + 1)
    return lines


def help_command(ctx: Ctx, args: argparse.Namespace) -> int:
    if args.topic == "config":
        print(config_help())
    else:
        print(build_parser().format_help())
        print("\n".join(surface(build_parser())))
    return 0


def add_help(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("help", help="the command surface, or `help config` for build.cfg")
    parser.add_argument("topic", nargs="?", choices=("config",), help="config: every build.cfg key with its default")
    parser.set_defaults(run=help_command)


# --- command line --------------------------------------------------------------------------------

SECTIONS: list[Callable[[argparse._SubParsersAction], None]] = [add_help, add_self]

# @@END-OF-SECTIONS@@


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=SELF_NAME, description="One build tool for a repository.")
    parser.add_argument("--root", type=Path, help="the repository (default: the directory above this file's tools/)")
    sub = parser.add_subparsers(dest="section", required=True, metavar="SECTION")
    for add in SECTIONS:
        add(sub)
    return parser


def own_commit() -> str | None:
    """The commit line 3 of this file names, or None for the overlay's own copy (SOURCE)."""
    match = header_of(Path(__file__).read_bytes())
    return None if match is None or match["commit"] == b"SOURCE" else match["commit"].decode()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.root is None:
            if own_commit() is None and args.section != "help":
                parser.error("this is the overlay's own copy: pass --root DIR to name the repository")
            args.root = Path(__file__).resolve().parent.parent
        return args.run(Ctx(args.root.resolve()), args)
    except Problem as problem:
        for line in str(problem).splitlines():
            print(f"{SELF_NAME}: {line}", file=sys.stderr)
        return 1
    except Usage as usage:
        parser.error(str(usage))
        return 2


if __name__ == "__main__":
    sys.exit(main())
