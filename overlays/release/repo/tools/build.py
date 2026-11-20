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
import collections
import fnmatch
import hashlib
import io
import json
import math
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Iterator, NamedTuple

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


def load_config(root: Path, path: Path | None = None) -> Table:
    path = path or root / CONFIG
    return parse_config(path.read_text(encoding="utf-8") if path.is_file() else "", path.name if path != root / CONFIG else CONFIG)


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

    def __init__(self, root: Path, config: Path | None = None) -> None:
        self.root = root
        self.config = config
        self._cfg: Table | None = None

    @property
    def cfg(self) -> Table:
        if self._cfg is None:
            self._cfg = load_config(self.root, self.config)
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

# --- the classifier: what an archive scanner would refuse --------------------------------------

# Judged by a file's leading bytes: archives, encrypted data, executables, packs, keys.
FLAG_MAGIC = [
    (b"PK\x03\x04", "zip"), (b"PK\x05\x06", "zip"), (b"PK\x07\x08", "zip"),
    (b"\x1f\x8b", "gzip"), (b"BZh", "bzip2"), (b"\xfd7zXZ\x00", "xz"),
    (b"7z\xbc\xaf\x27\x1c", "7z"), (b"Rar!\x1a\x07", "rar"), (b"\x28\xb5\x2f\xfd", "zstd"),
    (b"\x04\x22\x4d\x18", "lz4"), (b"MSCF", "cab"), (b"Cr24", "crx"), (b"!<arch>", "ar/deb"),
    (b"\xed\xab\xee\xdb", "rpm"), (b"Salted__", "openssl-encrypted"),
    (b"age-encryption.org", "age-encrypted"), (b"-----BEGIN PGP MESSAGE", "pgp-encrypted"),
    (b"MZ", "windows executable"), (b"\x7fELF", "elf executable"),
    (b"\xcf\xfa\xed\xfe", "mach-o executable"), (b"\xfe\xed\xfa\xcf", "mach-o executable"),
    (b"\xca\xfe\xba\xbe", "mach-o/java class"), (b"PACK", "git pack"),
    (b"\xfftOc", "git pack index"), (b"RIDX", "git reverse index"),
    (b"\x30\x82", "der (certificate or key)"),
]  # fmt: skip
# Other kinds that are refused unless `[deps] allow` names the path.
NOTE_MAGIC = [(b"\x00asm", "wasm"), (b"%PDF", "pdf"), (b"SQLite format 3", "sqlite")]
MEDIA = [
    b"\x89PNG", b"GIF8", b"\xff\xd8\xff", b"RIFF", b"\x00\x00\x01\x00", b"wOFF", b"wOF2",
    b"\x00\x01\x00\x00", b"OTTO",
]  # fmt: skip
KEY_EXT = re.compile(r"\.(pfx|p12|jks|keystore|der|key)$", re.IGNORECASE)
# A whole PEM block with a body, not a label quoted in code or docs.
PRIVATE_KEY = re.compile(
    rb"-----BEGIN ([A-Z0-9-]+ )?PRIVATE KEY-----\r?\n(?:[A-Za-z0-9+/=]{16,}\r?\n){2,}"
    rb"[A-Za-z0-9+/=]*\r?\n?-----END ([A-Z0-9-]+ )?PRIVATE KEY-----"
)
# The markers of a PEM private key, which one dash fewer no longer reads as.
PEM_MARKER = re.compile(rb"-----(BEGIN|END) ((?:[A-Z0-9-]+ )?PRIVATE KEY)-----")
PEM_REPLACEMENT = rb"----\1 \2-----"


def entropy(data: bytes) -> float:
    counts = collections.Counter(data)
    return -sum(v / len(data) * math.log2(v / len(data)) for v in counts.values()) if data else 0.0


def flagged_kind(body: bytes, name: str) -> str | None:
    """The kind of data a branch must not hold in a file's bytes, or None.

    Text and images pass. Archives, executables, keys, packs, wasm, pdf, sqlite and any other
    binary do not.
    """
    head = body[:600]
    if len(head) >= 262 and head[257:262] == b"ustar":
        return "tar"
    for magic, kind in FLAG_MAGIC:
        if head.startswith(magic):
            return kind
    if KEY_EXT.search(name):
        return "key or keystore file"
    for magic, kind in NOTE_MAGIC:
        if head.startswith(magic):
            return kind
    # ISO base media (HEIC, AVIF, MP4, MOV) names its brand after a 4-byte box size.
    if any(head.startswith(m) for m in MEDIA) or head[4:8] == b"ftyp":
        return None
    try:
        body.decode("utf-8")
        return "private key in text" if PRIVATE_KEY.search(body) else None
    except UnicodeDecodeError:
        pass
    return "opaque high-entropy data" if entropy(body[:65536]) > 7.5 else "unrecognised binary"


class Allow:
    """`[deps] allow`: entries `GLOB:KIND`, a kind of file that one path pattern may hold."""

    def __init__(self, entries: list[str] | None = None) -> None:
        self.rules: list[tuple[str, str]] = []
        for entry in entries or []:
            pattern, _, kind = entry.rpartition(":")
            if not pattern or not kind:
                raise Problem(f"deps.allow: {entry!r} is not GLOB:KIND")
            self.rules.append((pattern, kind))

    def permits(self, path: str, kind: str) -> bool:
        return any(kind == k and fnmatch.fnmatchcase(path, g) for g, k in self.rules)


# --- a Cargo.lock, a vendor directory and what makes one cover the other ----------------------------

CRATES_IO = "registry+https://github.com/rust-lang/crates.io-index"
# The address crates.io's own index gives in its config.json.
CRATES_URL = "https://static.crates.io/crates/{name}/{name}-{version}.crate"
CHECKSUM = ".cargo-checksum.json"
CONFIG_MARK = "# Written by tools/build.py."
MANIFEST = "manifest.json"
MANIFEST_FORMAT = 1
# Directories at a package's top that compiling never reads, and at any depth the ones that hold
# only test data.
DROP_TOP = {
    "test", "tests", "example", "examples", "bench", "benches", "benchmark", "benchmarks", "fuzz",
    "testdata", "test-data", "test_data", "fixture", "fixtures",
}  # fmt: skip
DROP_ANY = {"testdata", "test-data", "test_data", "fixture", "fixtures"}
INCLUDE = re.compile(r'include_(?:str|bytes)!\s*\(\s*"([^"]+)"\s*,?\s*\)|\binclude!\s*\(\s*"([^"]+)"\s*,?\s*\)')
INCLUDE_BYTES = re.compile(r'(\*\s*)?include_bytes!\s*\(\s*"([^"]+)"\s*,?\s*\)')
# A source file only tests, fuzzing or benchmarks compile; what it includes is not worth carrying.
TEST_ONLY = re.compile(r"(^|/)(fuzz|tests?|test_vectors|test-vectors|benches|bench|examples?)(\.rs)?(/|$)")
# The big-endian copy of a table; no target of ours has that byte order.
BIG_ENDIAN = re.compile(r"(^|[._-])bigendian([._-]|$)")
# A name Windows cannot create, which would make the checkout differ between platforms.
RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?$", re.IGNORECASE)


class Crate(NamedTuple):
    name: str
    version: str
    checksum: str

    @property
    def dir(self) -> str:
        return f"{self.name}-{self.version}"

    @property
    def file(self) -> str:
        return f"{self.name}-{self.version}.crate"


class Repo(NamedTuple):
    url: str
    commit: str

    @property
    def name(self) -> str:
        return self.url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")


class Lock(NamedTuple):
    crates: list[Crate]
    # Each repository at a revision, with the (name, version) Cargo.lock takes from it.
    repos: dict[Repo, list[tuple[str, str]]]
    # Each git source as Cargo.lock spells it, which is what a [source] entry has to replace.
    sources: dict[str, Repo]


class Tree(NamedTuple):
    """What a crates/ holds: each package directory's files with their SHA-256, and what is loose."""

    # Directory name -> file path below it -> SHA-256, `.cargo-checksum.json` included (empty
    # when only the metadata was read).
    dirs: dict[str, dict[str, str]]
    # Directory name -> its parsed `.cargo-checksum.json`.
    sums: dict[str, dict]
    # Directory name -> first line of its Cargo.toml, which names a git package's commit.
    heads: dict[str, str]
    # Files directly in crates/, which a vendor directory does not have.
    loose: list[str]
    # Directory name -> the files in it that are not text or images, as "path: kind".
    binaries: dict[str, list[str]]


class Scan(NamedTuple):
    """A whole commit: its crates/, and every path or file a scanner would refuse."""

    tree: Tree
    flagged: list[str]


def read_lock(path: Path) -> Lock:
    lock = Lock([], {}, {})
    try:
        packages = tomllib.loads(path.read_text(encoding="utf-8")).get("package", [])
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise Problem(f"{path}: {error}") from error
    for package in packages:
        name, version, source = package["name"], package["version"], package.get("source")
        if source is None:
            continue
        if source == CRATES_IO:
            if "checksum" not in package:
                raise Problem(f"{path.name} gives {name} {version} no checksum")
            lock.crates.append(Crate(name, version, package["checksum"]))
        elif source.startswith("git+"):
            parts = urllib.parse.urlsplit(source.removeprefix("git+"))
            repo = Repo(f"{parts.scheme}://{parts.netloc}{parts.path}", parts.fragment)
            lock.sources[source] = repo
            lock.repos.setdefault(repo, []).append((name, version))
        else:
            raise Problem(f"{name} {version} comes from {source}, which only crates.io and git are")
    return lock


def wanted_packages(lock: Lock) -> dict[str, tuple[str | None, Repo | None]]:
    """Directory name -> (the registry checksum or None, the repository or None)."""
    wanted: dict[str, tuple[str | None, Repo | None]] = {}
    entries = [(c.dir, (c.checksum, None)) for c in lock.crates]
    entries += [(f"{n}-{v}", (None, r)) for r, packages in lock.repos.items() for n, v in packages]
    for name, entry in entries:
        if name in wanted:
            raise Problem(f"{name} is taken from more than one source; a vendor directory has one")
        wanted[name] = entry
    return wanted


class Adder:
    """Files into a Tree, whether they come from a directory or from a commit.

    `prefix` is what a path read here is below in the whole branch, for `[deps] allow`.
    """

    def __init__(self, tree: Tree, allow: Allow | None = None, prefix: str = "crates/") -> None:
        self.tree = tree
        self.allow = allow or Allow()
        self.prefix = prefix

    def add(self, path: str, data: bytes) -> None:
        directory, _, rest = path.partition("/")
        if not rest:
            self.tree.loose.append(directory)
            return
        self.tree.dirs.setdefault(directory, {})[rest] = sha256(data)
        if rest != CHECKSUM:
            kind = flagged_kind(data, rest.rsplit("/", 1)[-1])
            if kind and not self.allow.permits(self.prefix + path, kind):
                self.tree.binaries.setdefault(directory, []).append(f"{rest}: {kind}")
        if rest == CHECKSUM:
            try:
                self.tree.sums[directory] = json.loads(data)
            except ValueError:
                self.tree.sums[directory] = {}
        elif rest == "Cargo.toml":
            self.tree.heads[directory] = data.split(b"\n", 1)[0].decode("utf-8", "replace")


def empty_tree() -> Tree:
    return Tree({}, {}, {}, [], {})


def read_dir(crates: Path, deep: bool = True, allow: Allow | None = None) -> Tree:
    """A crates/ as it is on disk; `deep=False` hashes nothing and reads only what names a package."""
    tree = empty_tree()
    if not crates.is_dir():
        return tree
    adder = Adder(tree, allow)
    for entry in sorted(crates.iterdir()):
        if not entry.is_dir():
            tree.loose.append(entry.name)
            continue
        tree.dirs.setdefault(entry.name, {})
        try:
            tree.sums[entry.name] = json.loads((entry / CHECKSUM).read_bytes())
        except (OSError, ValueError):
            tree.sums[entry.name] = {}
        manifest = entry / "Cargo.toml"
        if manifest.is_file():
            with manifest.open("rb") as handle:
                tree.heads[entry.name] = handle.readline().decode("utf-8", "replace").rstrip("\r\n")
        if not deep:
            continue
        for path in sorted(entry.rglob("*")):
            if path.is_file():
                adder.add(f"{entry.name}/{path.relative_to(entry).as_posix()}", path.read_bytes())
    return tree


def package_problems(
    tree: Tree, name: str, checksum: str | None, repo: Repo | None, deep: bool = True
) -> list[str]:
    """What keeps one directory from being the package Cargo.lock names, for a line each."""
    where = f"crates/{name}"
    if name not in tree.dirs:
        return [f"{where}: missing, Cargo.lock names it"]
    sums = tree.sums.get(name)
    if not sums:
        return [f"{where}: no {CHECKSUM}"]
    found = []
    if sums.get("package") != checksum:
        found.append(f"{where}: package checksum {sums.get('package')}, Cargo.lock says {checksum}")
    if repo and f" at {repo.commit}" not in tree.heads.get(name, ""):
        found.append(f"{where}: not {repo.name} at {repo.commit}")
    if not deep:
        return found
    found += [f"{where}/{line}; run sync" for line in tree.binaries.get(name, [])]
    listed = sums.get("files", {})
    have = {p: d for p, d in tree.dirs[name].items() if p != CHECKSUM}
    for path in sorted(set(listed) - set(have)):
        found.append(f"{where}/{path}: listed in {CHECKSUM}, missing")
    for path in sorted(set(have) - set(listed)):
        found.append(f"{where}/{path}: not listed in {CHECKSUM}")
    for path in sorted(set(have) & set(listed)):
        if have[path] != listed[path]:
            found.append(f"{where}/{path}: SHA-256 {have[path]}, {CHECKSUM} says {listed[path]}")
    return found


def problems(lock: Lock, tree: Tree, deep: bool = True) -> list[str]:
    """One line for each thing keeping a crates/ from covering Cargo.lock, and nothing more."""
    found = []
    wanted = wanted_packages(lock)
    for name, (checksum, repo) in wanted.items():
        found += package_problems(tree, name, checksum, repo, deep)
    for name in sorted(set(tree.dirs) - set(wanted)):
        found.append(f"crates/{name}: stale, Cargo.lock does not name it")
    for name in sorted(tree.loose):
        found.append(f"crates/{name}: a file; a vendor directory holds only package directories")
    return found


# --- commits as data: every blob of a commit, with its mode ------------------------------------------


def batch_bodies(cwd: Path, shas: list[str]) -> Iterator[tuple[str, bytes]]:
    """Each object's bytes exactly as git stores them, in the order asked, from one `cat-file --batch`."""
    reader = subprocess.Popen(
        ["git", "-C", str(cwd), "cat-file", "--batch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE
    )

    def feed() -> None:
        try:
            for sha in shas:
                reader.stdin.write(sha.encode() + b"\n")
        except OSError:
            pass
        finally:
            try:
                reader.stdin.close()
            except OSError:
                pass

    threading.Thread(target=feed, daemon=True).start()
    try:
        for sha in shas:
            header = reader.stdout.readline().split()
            if len(header) != 3:
                raise Problem(f"object {sha} is not in {cwd}")
            body = reader.stdout.read(int(header[2]))
            reader.stdout.read(1)
            yield sha, body
    finally:
        reader.kill()
        reader.wait()
        reader.stdout.close()


def commit_entries(checkout: Path, commit: str) -> list[tuple[str, str, str]]:
    """(path, mode, object id) of each blob of a commit's tree; raises when it is not there."""
    listing = subprocess.run(["git", "-C", str(checkout), "ls-tree", "-r", "-z", commit], capture_output=True)
    if listing.returncode != 0:
        raise Problem(f"commit {commit} is not in {checkout}")
    entries = []
    for entry in listing.stdout.split(b"\0"):
        if not entry:
            continue
        meta, _, raw = entry.partition(b"\t")
        mode, kind, sha = meta.decode().split()
        if kind == "blob":
            entries.append((raw.decode("utf-8", "surrogateescape"), mode, sha))
    return entries


def has_commit(checkout: Path, commit: str) -> bool:
    return git_ok("cat-file", "-e", f"{commit}^{{commit}}", cwd=checkout)


def scan_ref(checkout: Path, commit: str, allow: Allow | None = None) -> Scan | None:
    """A commit's crates/ and everything in it a scanner would refuse; None when it is not here."""
    allow = allow or Allow()
    if not has_commit(checkout, commit):
        return None
    tree = empty_tree()
    adder = Adder(tree, allow)
    flagged = []
    entries = commit_entries(checkout, commit)
    for (path, mode, _), (_, body) in zip(entries, batch_bodies(checkout, [e[2] for e in entries])):
        if any(part.lower() == ".git" for part in path.split("/")):
            flagged.append(f"{path}: a path below .git")
        if mode == "120000":
            flagged.append(f"{path}: a symbolic link")
        elif (kind := flagged_kind(body, path.rsplit("/", 1)[-1])) and not allow.permits(path, kind):
            flagged.append(f"{path}: {kind}")
        if path.startswith("crates/"):
            adder.add(path.removeprefix("crates/"), body)
    return Scan(tree, flagged)


# --- turning a package into a vendor directory ---------------------------------------------------------


def settings_crates_url(cfg: Table) -> str:
    return os.environ.get("BUILD_CRATES_URL") or cfg.deps.crates_url or CRATES_URL


def fetch_crate(crate: Crate, template: str = CRATES_URL) -> bytes:
    # A version's build metadata has a + that a URL path would otherwise leave ambiguous.
    version = urllib.parse.quote(crate.version, safe="")
    data = fetch(template.format(name=crate.name, version=version))
    if sha256(data) != crate.checksum:
        raise Problem(f"refused {crate.file}: SHA-256 {sha256(data)}, Cargo.lock says {crate.checksum}")
    return data


def archive_files(data: bytes, prefix: str) -> dict[str, bytes]:
    """The regular files of a .crate below its `<name>-<version>/` directory, by relative path."""
    files = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar:
            if not member.isfile():
                continue
            if not member.name.startswith(prefix):
                raise Problem(f"{prefix.rstrip('/')}.crate holds {member.name}, outside {prefix}")
            files[member.name.removeprefix(prefix)] = tar.extractfile(member).read()
    return files


def safe_extract(tar: tarfile.TarFile, dest: Path, links: str = "refuse") -> None:
    """Writes an archive member by member: regular files and directories, below `dest` only.

    A path that is absolute or climbs out with `..` is refused. A link is refused (`links =
    "refuse"`), left out (`"skip"`), or made when it points inside `dest` (`"inside"`).
    """
    base = dest.resolve()
    for member in tar:
        parts = posixpath.normpath(member.name).split("/") if member.name not in ("", ".") else []
        if member.name.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", member.name) or ".." in member.name.replace("\\", "/").split("/"):
            raise Problem(f"archive member {member.name} is outside the archive")
        target = base.joinpath(*parts) if parts else base
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
        elif member.isfile():
            target.parent.mkdir(parents=True, exist_ok=True)
            source = tar.extractfile(member)
            target.write_bytes(source.read())
            if member.mode & 0o111 and os.name != "nt":
                target.chmod(target.stat().st_mode | 0o111)
        elif member.issym() or member.islnk():
            if links == "refuse":
                raise Problem(f"archive member {member.name} is a link; links are refused")
            if links == "inside" and os.name != "nt":
                resolved = (target.parent / member.linkname).resolve() if member.issym() else base / member.linkname
                if base not in resolved.parents and resolved != base:
                    raise Problem(f"archive link {member.name} points outside the archive")
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists() and not target.is_symlink():
                    if member.issym():
                        os.symlink(member.linkname, target)
                    else:
                        shutil.copyfile(base / member.linkname, target)
        else:
            raise Problem(f"archive member {member.name} is not a regular file or directory")


def keeps(path: str) -> bool:
    """Whether a package's file (by path below the package) is something compiling can read."""
    *dirs, name = path.split("/")
    if dirs and dirs[0].lower() in DROP_TOP:
        return False
    if any(d.lower() in DROP_ANY or d.startswith(".") for d in dirs):
        return False
    return not (name.startswith(".git") or name == ".cargo-ok")


def drop_missing_targets(text: str, present: set[str]) -> str:
    """The manifest without the [[test]], [[bench]] and [[example]] tables whose file is not here.

    Cargo refuses a manifest naming a target it cannot find, even in a dependency.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return text
    folders = {"test": "tests", "bench": "benches", "example": "examples"}
    index = dict.fromkeys(folders, -1)
    out, skipping = [], False
    for line in text.split("\n"):
        header = re.match(r"^\[\[(test|bench|example)\]\]\s*(#.*)?$", line)
        if header:
            kind = header[1]
            index[kind] += 1
            target = data.get(kind, [])[index[kind]]
            name = target.get("name", "")
            path = target.get("path") or None
            options = [path] if path else [f"{folders[kind]}/{name}.rs", f"{folders[kind]}/{name}/main.rs"]
            skipping = not any(Path(p).as_posix() in present for p in options if p)
        elif re.match(r"^\[", line):
            skipping = False
        if not skipping:
            out.append(line)
    return "\n".join(out)


def inline_bytes(kept: dict[str, bytes], files: dict[str, bytes], refused: set[str]) -> None:
    """Writes the bytes of each refused file into the `include_bytes!` that reads it.

    The array literal has the type `include_bytes!` gives, so a crate that embeds a DER file or a
    table compiles without the binary file being in the branch. A file that only tests read, or
    the big-endian copy of a table, is left out and its `include_bytes!` as it was.
    """

    def literal(base: str):
        def replace(found: re.Match[str]) -> str:
            target = posixpath.normpath(posixpath.join(base, found[2]))
            if target not in refused or BIG_ENDIAN.search(target) or TEST_ONLY.search(base + "/"):
                return found[0]
            body = files[target]
            array = "[" + ",".join(f"0x{b:02x}u8" if i == 0 else f"0x{b:02x}" for i, b in enumerate(body)) + "]"
            # `*include_bytes!(..)` is the array itself; plain `include_bytes!(..)` a reference to it.
            return array if found[1] else "&" + array

        return replace

    for path in [p for p in kept if p.endswith(".rs") and not TEST_ONLY.search(p)]:
        try:
            text = kept[path].decode("utf-8")
        except UnicodeDecodeError:
            continue
        changed = INCLUDE_BYTES.sub(literal(posixpath.dirname(path)), text)
        if changed != text:
            kept[path] = changed.encode("utf-8")


def hex_lines(data: bytes) -> bytes:
    digits = data.hex()
    return ("\n".join(digits[i : i + 128] for i in range(0, len(digits), 128)) + "\n").encode("ascii")


RING_OBJECT = re.compile(r"^pregenerated/.+\.o$")
RING_USE = """            let obj_srcs = asm_srcs
                .iter()
                .map(|src| obj_path(generated_dir, src.as_path()))
                .collect::<Vec<_>>();"""
RING_FN = """
/// The object file this crate carries as `<name>.o.hex`, written below `out_dir` for the linker.
fn object_from_text(generated_dir: &Path, out_dir: &Path, src: &Path) -> PathBuf {
    let text = fs::read_to_string(format!("{}.hex", obj_path(generated_dir, src).display())).unwrap();
    let digits: Vec<u8> = text.bytes().filter(|b| b.is_ascii_hexdigit()).collect();
    let bytes: Vec<u8> = digits
        .chunks(2)
        .map(|pair| u8::from_str_radix(std::str::from_utf8(pair).unwrap(), 16).unwrap())
        .collect();
    let path = obj_path(out_dir, src);
    fs::write(&path, bytes).unwrap();
    path
}
"""


def patch_ring(name: str, kept: dict[str, bytes], refused: dict[str, bytes]) -> None:
    """Keeps ring's prebuilt nasm objects (linked on x86 Windows) as hex, decoded by build.rs."""
    objects = sorted(p for p in refused if RING_OBJECT.match(p))
    if not objects:
        return
    build = kept["build.rs"].decode("utf-8")
    if build.count(RING_USE) != 1:
        raise Problem(f"{name}: build.rs no longer links its objects as build.py expects")
    for path in objects:
        kept[f"{path}.hex"] = hex_lines(refused.pop(path))
    fixed = build.replace(
        RING_USE, RING_USE.replace("obj_path(generated_dir, src.as_path())", "object_from_text(generated_dir, out_dir, src.as_path())")
    )
    kept["build.rs"] = (fixed.rstrip("\n") + "\n" + RING_FN).encode("utf-8")


WINDOWS_CRATE = re.compile(r"^windows_[a-z0-9_]+$")
WINDOWS_BUILD = """fn main() {
    let dir = std::env::var("CARGO_MANIFEST_DIR").unwrap();

    println!(
        "cargo:rustc-link-search=native={}",
        std::path::Path::new(&dir).join("lib").display()
    );
}
"""
WINDOWS_BUILD_TEXT = """use std::{env, fs, path::Path};

/// Writes each `lib/<name>.hex` below `OUT_DIR` as `<name>` and points the linker there.
fn main() {
    let dir = env::var("CARGO_MANIFEST_DIR").unwrap();
    let out = env::var("OUT_DIR").unwrap();
    for entry in fs::read_dir(Path::new(&dir).join("lib")).unwrap() {
        let path = entry.unwrap().path();
        let name = path.file_name().unwrap().to_string_lossy().into_owned();
        let Some(lib) = name.strip_suffix(".hex") else { continue };
        println!("cargo:rerun-if-changed={}", path.display());
        let text = fs::read_to_string(&path).unwrap();
        let digits: Vec<u8> = text.bytes().filter(|b| b.is_ascii_hexdigit()).collect();
        let bytes: Vec<u8> = digits
            .chunks(2)
            .map(|pair| u8::from_str_radix(std::str::from_utf8(pair).unwrap(), 16).unwrap())
            .collect();
        fs::write(Path::new(&out).join(lib), bytes).unwrap();
    }
    println!("cargo:rustc-link-search=native={out}");
}
"""


def patch_windows_libraries(name: str, kept: dict[str, bytes], refused: dict[str, bytes]) -> None:
    """Keeps the import libraries of a windows_<arch>_<abi> crate as hex, decoded into OUT_DIR by build.rs."""
    libs = sorted(p for p in refused if p.startswith("lib/"))
    if not libs:
        return
    if kept.get("build.rs", b"").decode("utf-8", "replace").replace("\r\n", "\n") != WINDOWS_BUILD:
        raise Problem(f"{name}: build.rs no longer links its import libraries as build.py expects")
    for path in libs:
        kept[f"{path}.hex"] = hex_lines(refused.pop(path))
    kept["build.rs"] = WINDOWS_BUILD_TEXT.encode("utf-8")


def package_patch(crate: str) -> Callable[[str, dict[str, bytes], dict[str, bytes]], None] | None:
    """The patch for a crate whose build links binary files that cannot be inlined, by crate name."""
    if WINDOWS_CRATE.match(crate):
        return patch_windows_libraries
    return {"ring": patch_ring}.get(crate)


def include_closure(files: dict[str, bytes]) -> dict[str, bytes]:
    """The files compiling can read: `keeps`, and whatever a kept source file includes by path."""
    kept = {p: b for p, b in files.items() if keeps(p)}
    pending = [p for p in kept if p.endswith(".rs")]
    while pending:
        path = pending.pop()
        try:
            text = kept[path].decode("utf-8")
        except UnicodeDecodeError:
            continue
        for found in INCLUDE.finditer(text):
            target = posixpath.normpath(posixpath.join(posixpath.dirname(path), found[1] or found[2]))
            if target in files and target not in kept:
                kept[target] = files[target]
                if target.endswith(".rs"):
                    pending.append(target)
    return kept


def write_package(target: Path, everything: dict[str, bytes], checksum: str | None) -> None:
    """Writes a package directory with its .cargo-checksum.json, leaving out what a scanner refuses.

    The files map lists what was written, so cargo checks each file it reads against it.
    """
    files = include_closure(everything)
    seen: dict[str, str] = {}
    for path in files:
        if seen.setdefault(path.lower(), path) != path:
            raise Problem(f"{target.name} has {path} and {seen[path.lower()]}, one file on Windows")
        if any(RESERVED.match(part) for part in path.split("/")):
            raise Problem(f"{target.name}/{path} is a name Windows cannot create")
    kept, refused = {}, {}
    for path, body in files.items():
        name = path.rsplit("/", 1)[-1]
        if flagged_kind(body, name) == "private key in text":
            body = PEM_MARKER.sub(PEM_REPLACEMENT, body)
        (refused if flagged_kind(body, name) else kept)[path] = body
    if refused:
        inline_bytes(kept, files, set(refused))
        patch = package_patch(target.name.rsplit("-", 1)[0])
        if patch:
            patch(target.name, kept, refused)
    if "Cargo.toml" in kept:
        text = kept["Cargo.toml"].decode("utf-8")
        fixed = drop_missing_targets(text, set(kept))
        if fixed != text:
            kept["Cargo.toml"] = fixed.encode("utf-8")
    partial = target.with_name(target.name + ".partial")
    shutil.rmtree(partial, ignore_errors=True)
    for path, body in kept.items():
        (partial / path).parent.mkdir(parents=True, exist_ok=True)
        (partial / path).write_bytes(body)
    listed = {path: sha256(body) for path, body in sorted(kept.items())}
    (partial / CHECKSUM).write_bytes(json.dumps({"files": listed, "package": checksum}, separators=(",", ":")).encode())
    shutil.rmtree(target, ignore_errors=True)
    partial.rename(target)


def materialize_crate(crate: Crate, crates: Path, template: str = CRATES_URL) -> None:
    write_package(crates / crate.dir, archive_files(fetch_crate(crate, template), f"{crate.dir}/"), crate.checksum)


def export_commit(repo: Repo, into: Path) -> Path:
    """The tree of exactly this commit below `into`, with no history: git checks the hash it fetched."""
    work = into / "fetch"
    work.mkdir()
    run_git("init", "-q", cwd=work)
    run_git("fetch", "-q", "--depth", "1", repo.url, repo.commit, cwd=work)
    if run_git("rev-parse", "FETCH_HEAD", cwd=work) != repo.commit:
        raise Problem(f"{repo.url} did not give commit {repo.commit}")
    if git_ok("cat-file", "-e", "FETCH_HEAD:.gitmodules", cwd=work):
        raise Problem(f"{repo.name} has submodules, which a source tree would leave out")
    tree = into / "tree"
    tree.mkdir()
    made = subprocess.Popen(
        ["git", "-C", str(work), "-c", "core.autocrlf=false", "-c", "core.eol=lf", "archive", "--format=tar", "FETCH_HEAD"],
        stdout=subprocess.PIPE,
    )
    try:
        with tarfile.open(fileobj=made.stdout, mode="r|") as tar:
            safe_extract(tar, tree)
    finally:
        # git pads the archive to a whole record after its end marker; read it, or the pipe stays
        # full and git never exits (a Windows pipe holds 4 KiB).
        made.stdout.read()
        code = made.wait()
    if code:
        raise Problem(f"git archive of {repo.name} at {repo.commit} failed")
    shutil.rmtree(work, ignore_errors=True)
    return tree


def toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        # What JSON escapes is a TOML basic string too, as long as it writes no surrogate pairs.
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        pairs = (f"{toml_key(key)} = {toml_value(item)}" for key, item in value.items())
        return "{ " + ", ".join(pairs) + " }"
    raise Problem(f"cannot write {value!r} back as TOML")


def toml_key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else json.dumps(key, ensure_ascii=False)


def toml_table(table: dict, path: list[str], lines: list[str]) -> None:
    """Writes one table, its plain values before its subtables as TOML requires."""
    subtables = {key: item for key, item in table.items() if isinstance(item, dict)}
    if path:
        lines.append("[" + ".".join(toml_key(part) for part in path) + "]")
    for key, item in table.items():
        if key not in subtables:
            lines.append(f"{toml_key(key)} = {toml_value(item)}")
    for key, item in subtables.items():
        toml_table(item, [*path, key], lines)


def inherit_dependency(name: str, own: dict, workspace: dict, manifest: str) -> dict:
    """A `{ workspace = true }` dependency, with cargo's rules for what the member may add."""
    shared = workspace.get("dependencies", {}).get(name)
    if shared is None:
        raise Problem(f"{manifest} inherits dependency {name}, which its workspace does not list")
    merged = {"version": shared} if isinstance(shared, str) else dict(shared)
    features = list(merged.get("features", [])) + list(own.get("features", []))
    if features:
        merged["features"] = features
    if "optional" in own:
        merged["optional"] = own["optional"]
    for spelling in ("default-features", "default_features"):
        if spelling in own and "default-features" not in merged and "default_features" not in merged:
            merged[spelling] = own[spelling]
    return merged


def normalized_manifest(manifest: dict, root: dict, member_dir: Path, root_dir: Path, label: str) -> dict:
    """A workspace member's manifest with nothing left to inherit, as `cargo package` would write it.

    A directory source reads each package on its own, with no workspace above it. A `path`
    dependency stays: within a directory source it means this same source, wherever the path points.
    """
    workspace = root.get("workspace", {})
    shared = workspace.get("package", {})
    result = {k: v for k, v in manifest.items() if k not in ("workspace", "patch", "replace", "profile")}
    package = dict(result["package"])
    package.pop("workspace", None)
    for key, value in list(package.items()):
        if isinstance(value, dict) and value.get("workspace") is True:
            if key not in shared:
                raise Problem(f"{label} inherits package.{key}, which its workspace does not set")
            value = shared[key]
            if key in ("readme", "license-file") and isinstance(value, str):
                value = Path(os.path.relpath(root_dir / value, member_dir)).as_posix()
            package[key] = value
    result["package"] = package
    lints = result.get("lints")
    if isinstance(lints, dict) and lints.get("workspace") is True:
        result.pop("lints")
        if "lints" in workspace:
            result["lints"] = workspace["lints"]

    def inherit_tables(table: dict) -> None:
        for kind in ("dependencies", "dev-dependencies", "dev_dependencies", "build-dependencies", "build_dependencies"):
            if kind in table:
                table[kind] = {
                    name: (
                        inherit_dependency(name, value, workspace, label)
                        if isinstance(value, dict) and value.get("workspace") is True
                        else value
                    )
                    for name, value in table[kind].items()
                }

    inherit_tables(result)
    for target in result.get("target", {}).values():
        inherit_tables(target)
    return result


def materialize_repo(repo: Repo, wanted: list[tuple[str, str]], crates: Path) -> None:
    """Puts each package Cargo.lock takes from a repository in a directory of its own in crates/."""
    with tempfile.TemporaryDirectory(prefix="build-git-") as scratch:
        tree = export_commit(repo, Path(scratch))
        manifests: dict[Path, dict] = {}
        for path in tree.rglob("Cargo.toml"):
            if any(part.startswith(".") for part in path.relative_to(tree).parts[:-1]):
                continue
            try:
                manifests[path.parent] = tomllib.loads(path.read_text(encoding="utf-8"))
            except (tomllib.TOMLDecodeError, UnicodeDecodeError):
                # A fixture, or anything else cargo would also skip when it searches a git source.
                continue

        def workspace_root(member: Path) -> Path | None:
            for directory in (member, *member.parents):
                if directory == tree.parent:
                    return None
                if "workspace" in manifests.get(directory, {}):
                    return directory
            return None

        packages = {d for d, m in manifests.items() if "package" in m}
        for name, version in wanted:
            found = []
            for directory in packages:
                manifest = manifests[directory]
                package = manifest["package"]
                if package.get("name") != name:
                    continue
                root_dir = workspace_root(directory)
                root = manifests.get(root_dir, {}) if root_dir else {}
                shown = package.get("version")
                if isinstance(shown, dict):
                    shown = root.get("workspace", {}).get("package", {}).get("version")
                if shown == version:
                    found.append((directory, manifest, root_dir or directory, root))
            if len(found) != 1:
                raise Problem(f"{repo.name} at {repo.commit} has {len(found)} packages {name} {version}")
            directory, manifest, root_dir, root = found[0]
            label = f"{repo.name}/{directory.relative_to(tree).as_posix()}"
            written = normalized_manifest(manifest, root, directory, root_dir, label)
            nested = [d for d in packages if d != directory and directory in d.parents]
            files = {}
            for path in directory.rglob("*"):
                if path.is_file() and not any(n in path.parents for n in nested):
                    files[path.relative_to(directory).as_posix()] = path.read_bytes()
            lines = [f"# {label} at {repo.commit}, with what it inherits written out."]
            toml_table(written, [], lines)
            files["Cargo.toml"] = ("\n".join(lines) + "\n").encode("utf-8")
            # Git packages have no registry checksum.
            write_package(crates / f"{name}-{version}", files, None)


def sync_crates(deps_dir: Path, lock: Lock, template: str, jobs: int) -> list[str]:
    """Makes deps_dir/crates cover the lock and hold nothing else; returns a line per change."""
    crates = deps_dir / "crates"
    wanted = wanted_packages(lock)
    crates.mkdir(parents=True, exist_ok=True)
    for entry in sorted(crates.iterdir()):
        if entry.name.endswith(".partial") and entry.is_dir():
            shutil.rmtree(entry)
        elif not entry.is_dir() or (entry.name not in wanted and not (entry / CHECKSUM).is_file()):
            raise Problem(f"{entry} was not made by this tool: is {crates} from a deps checkout?")
    tree = read_dir(crates)
    changes = []
    for name in sorted(set(tree.dirs) - set(wanted)):
        shutil.rmtree(crates / name)
        changes.append(f"removed {name}")
    stale = {n for n, (c, r) in wanted.items() if package_problems(tree, n, c, r)}
    crate_jobs = [c for c in lock.crates if c.dir in stale]
    repo_jobs = [(r, p) for r, p in lock.repos.items() if any(f"{n}-{v}" in stale for n, v in p)]
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        for future in [pool.submit(materialize_crate, c, crates, template) for c in crate_jobs] + [
            pool.submit(materialize_repo, r, p, crates) for r, p in repo_jobs
        ]:
            future.result()
    return changes + [f"{'replaced' if n in tree.dirs else 'added'} {n}" for n in sorted(stale)]


# --- deps: the commands -------------------------------------------------------------------------------

SCHEMA["deps"] = Key(
    "table",
    None,
    "The deps branch: every crate Cargo.lock names, vendored as text, so a build needs no network.",
    {
        "branch": Key("str", "deps", "The orphan branch that holds the vendored files."),
        "source_name": Key("str", "vendored", "The name of the cargo source the written cargo config replaces crates.io with."),
        "crates_url": Key("str", "", "A mirror of crates.io's .crate files, with {name} and {version}; the environment variable BUILD_CRATES_URL wins."),
        "cargo_lock": Key("str", "Cargo.lock", "The lock the deps branch must cover, relative to the repository."),
        "checkout": Key("str", "", "Where the deps branch is checked out; the environment variable BUILD_DEPS wins. Empty: a detached worktree beside the repository, named REPO-deps."),
        "allow": Key("strs", [], "Files a scan lets through, as GLOB:KIND (for example cdn/www.gstatic.com/flutter-canvaskit/*:wasm)."),
        "jobs": Key("int", 8, "Packages fetched at once."),
        "pin": Key(
            "table",
            None,
            "Where main records the deps commit it was built with: a row of an SDK lock, `GROUP NAME COMMIT ...`.",
            {
                "lock": Key("str", "", "The lock file, relative to the repository; empty: main records no pin."),
                "item": Key("str", "", "The row, `GROUP NAME` (for example core deps)."),
            },
        ),
        "repin": Key(
            "tables",
            None,
            "A file on the deps branch that names the deps commit; refresh rewrites it to the new commit.",
            {
                "file": Key("str", REQUIRED, "The file, relative to the deps branch."),
                "pattern": Key("str", REQUIRED, "A regular expression (multi-line) with one group: the text to replace by the commit."),
            },
        ),
    },
)


def short(commit: str) -> str:
    return commit[:8]


def find_deps(ctx: Ctx, given: Path | None = None) -> Path:
    """The deps checkout: --deps, $BUILD_DEPS, `[deps] checkout`, else a worktree beside the repository.

    The worktree is added detached at origin/deps (or deps) when missing, and moved to that
    branch's tip when it is detached; one on a branch is its owner's and is left alone.
    """
    if given is not None:
        return given.resolve()
    if os.environ.get("BUILD_DEPS"):
        return Path(os.environ["BUILD_DEPS"]).resolve()
    cfg = ctx.cfg.deps
    if cfg.checkout:
        path = Path(cfg.checkout)
        return path if path.is_absolute() else (ctx.root / path).resolve()
    ref = next((r for r in (f"origin/{cfg.branch}", cfg.branch) if git_ok("rev-parse", "--verify", "-q", r, cwd=ctx.root)), None)
    if ref is None:
        raise Problem(f"no deps checkout: pass --deps, set BUILD_DEPS or [deps] checkout, or fetch the branch (git fetch origin {cfg.branch})")
    path = ctx.root.with_name(ctx.root.name + "-deps")
    if path.is_dir():
        if not git_ok("symbolic-ref", "-q", "HEAD", cwd=path):
            run_git("checkout", "-q", "--detach", ref, cwd=path)
    else:
        run_git("worktree", "add", "--detach", str(path), ref, cwd=ctx.root)
    return path


class DirView:
    """A deps checkout on disk, for the checks that ask whether a path is there."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.label = str(path)

    def exists(self, rel: str) -> bool:
        return (self.path / rel).exists()

    def read(self, rel: str) -> bytes | None:
        target = self.path / rel
        return target.read_bytes() if target.is_file() else None


class RefView:
    """A commit of the repository, for the same checks."""

    def __init__(self, root: Path, commit: str) -> None:
        self.root, self.commit = root, commit
        self.label = f"deps at {commit[:12]}"

    def exists(self, rel: str) -> bool:
        return git_ok("cat-file", "-e", f"{self.commit}:{rel}", cwd=self.root)

    def read(self, rel: str) -> bytes | None:
        done = subprocess.run(["git", "-C", str(self.root), "show", f"{self.commit}:{rel}"], capture_output=True)
        return done.stdout if done.returncode == 0 else None


# Extra coverage checks and sync steps, added by the sections below (pub, CDN).
DEPS_CHECKS: list[Callable[[Ctx, Any, Any], tuple[list[str], list[str]]]] = []


def manifest_problems(text: bytes | None, lock: Lock) -> tuple[list[str], list[str]]:
    """(problems, notes) about manifest.json: absent is a note, wrong is a problem."""
    if text is None:
        return [], [f"no {MANIFEST} on the deps branch; `deps refresh` writes one"]
    try:
        data = json.loads(text)
    except ValueError:
        return [f"{MANIFEST}: not JSON"], []
    found = []
    if data.get("format") != MANIFEST_FORMAT:
        found.append(f"{MANIFEST}: format {data.get('format')}, this tool reads {MANIFEST_FORMAT}")
    if data.get("crates") != len(lock.crates) or data.get("gitSources") != len(lock.repos):
        found.append(
            f"{MANIFEST}: says {data.get('crates')} crates and {data.get('gitSources')} git sources, "
            f"Cargo.lock names {len(lock.crates)} and {len(lock.repos)}"
        )
    return found, []


def write_manifest(deps_dir: Path, ctx: Ctx, lock: Lock, lock_path: Path, extra: dict) -> None:
    """manifest.json at the root of the deps branch. `sourceCommit` is where main stood when the
    contents last changed, so a commit that changes nothing else leaves the file as it was."""
    commit = run_git("rev-parse", "HEAD", cwd=ctx.root) if git_ok("rev-parse", "HEAD", cwd=ctx.root) else ""
    data = {
        "format": MANIFEST_FORMAT,
        "sourceCommit": commit,
        "cargoLockSha256": sha256(lock_path.read_bytes()),
        "crates": len(lock.crates),
        "gitSources": len(lock.repos),
        **extra,
    }
    path = deps_dir / MANIFEST
    try:
        old = json.loads(path.read_bytes())
    except (OSError, ValueError):
        old = None
    if isinstance(old, dict) and {**old, "sourceCommit": ""} == {**data, "sourceCommit": ""}:
        return
    path.write_bytes((json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def deps_sync_all(ctx: Ctx, deps_dir: Path, lock: Lock, lock_path: Path, args: argparse.Namespace | None = None) -> list[str]:
    """Crates, then the pub and CDN files when configured, then manifest.json; a line per change."""
    cfg = ctx.cfg.deps
    changes = sync_crates(deps_dir, lock, settings_crates_url(ctx.cfg), cfg.jobs)
    extra: dict = {}
    for sync in DEPS_SYNCS:
        more, info = sync(ctx, deps_dir, args)
        changes += more
        extra.update(info)
    write_manifest(deps_dir, ctx, lock, lock_path, extra)
    return changes


DEPS_SYNCS: list[Callable[[Ctx, Path, Any], tuple[list[str], dict]]] = []


def config_text(ctx: Ctx, lock: Lock, crates: Path) -> str:
    """The config `cargo vendor` prints: every source replaced by the one vendor directory."""
    name = ctx.cfg.deps.source_name
    lines = [
        f"{CONFIG_MARK} Every source Cargo.lock names, from a directory.",
        "[net]",
        "offline = true",
        "",
        "[source.crates-io]",
        f'replace-with = "{name}"',
        "",
        f"[source.{name}]",
        f"directory = {toml_value(crates.as_posix())}",
    ]
    for source in sorted(lock.sources):
        parts = urllib.parse.urlsplit(source.removeprefix("git+"))
        entry = [f"git = {toml_value(f'{parts.scheme}://{parts.netloc}{parts.path}')}"]
        entry += [f"{key} = {toml_value(value)}" for key, value in urllib.parse.parse_qsl(parts.query)]
        lines += ["", f"[source.{toml_value(source.partition('#')[0])}]", *entry, f'replace-with = "{name}"']
    return "\n".join(lines) + "\n"


def use_crates(
    ctx: Ctx, deps_dir: Path, lock: Lock, lock_path: Path, out: Path | None = None, repo_config: bool = False, full: bool = False
) -> dict:
    """Writes a cargo home whose config builds from deps_dir/crates, only when it covers the lock.

    The check reads each package's metadata; `full` also re-hashes every file.
    """
    crates = (deps_dir / "crates").resolve()
    found = problems(lock, read_dir(crates, full, Allow(ctx.cfg.deps.allow)), full)
    if found:
        raise Problem("; ".join(found[:5]) + f" ({len(found)} in all); run `deps sync` first")
    root = ctx.root.resolve()
    out = (out or Path(tempfile.gettempdir()) / f"build-crates-{sha256(lock_path.read_bytes())[:12]}").resolve()
    if out == root or root in out.parents:
        raise Problem(f"{out} is inside the repository; --out names a directory outside it")
    home = out / "cargo-home"
    config = home / "config.toml"
    if config.is_file() and not config.read_text(encoding="utf-8").startswith(CONFIG_MARK):
        raise Problem(f"{config} is not one this command wrote; it was left alone")
    home.mkdir(parents=True, exist_ok=True)
    text = config_text(ctx, lock, crates)
    config.write_bytes(text.encode("utf-8"))
    info = {"cargo_home": str(home), "config": str(config), "crates": len(lock.crates), "git_sources": len(lock.repos), "repo_config": None}
    if repo_config:
        target = root / ".cargo" / "config.toml"
        if target.exists() and not target.read_text(encoding="utf-8").startswith(CONFIG_MARK):
            raise Problem(f"{target} is not one this command wrote; it was left alone")
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
        info["repo_config"] = str(target)
    return info


def ref_problems(ctx: Ctx, lock: Lock, commit: str) -> tuple[list[str], list[str]]:
    """What keeps a commit of the repository from being a deps branch covering the lock."""
    scan = scan_ref(ctx.root, commit, Allow(ctx.cfg.deps.allow))
    if scan is None:
        return [f"deps at {commit[:12]} is not here: git fetch --depth 1 origin {commit}"], []
    label = f"deps at {commit[:12]}"
    found = [f"{label}: {line}" for line in problems(lock, scan.tree)]
    found += [f"{label}: {line}; the branch holds no such file" for line in scan.flagged]
    view = RefView(ctx.root, commit)
    more, notes = manifest_problems(view.read(MANIFEST), lock)
    found += [f"{label}: {line}" for line in more]
    for check in DEPS_CHECKS:
        extra, extra_notes = check(ctx, view, lock)
        found += [f"{label}: {line}" for line in extra]
        notes += extra_notes
    return found, notes


def lock_path_of(ctx: Ctx, args: argparse.Namespace) -> Path:
    return args.cargo_lock.resolve() if args.cargo_lock else ctx.root / ctx.cfg.deps.cargo_lock


def deps_sync(ctx: Ctx, args: argparse.Namespace) -> int:
    path = lock_path_of(ctx, args)
    lock = read_lock(path)
    deps = find_deps(ctx, args.deps)
    changes = deps_sync_all(ctx, deps, lock, path, args)
    for change in changes:
        print(change)
    print(f"ok: {len(lock.crates)} crates, {len(lock.repos)} git sources, {len(changes)} changed")
    return 0


def deps_check(ctx: Ctx, args: argparse.Namespace) -> int:
    path = lock_path_of(ctx, args)
    lock = read_lock(path)
    if args.ref:
        if args.quick:
            raise Usage("--quick reads a checkout's metadata; --ref reads every file of a commit")
        found, notes = ref_problems(ctx, lock, args.ref)
    else:
        deps = find_deps(ctx, args.deps)
        allow = Allow(ctx.cfg.deps.allow)
        deep = not args.quick
        found = problems(lock, read_dir(deps / "crates", deep, allow), deep)
        view = DirView(deps)
        more, notes = manifest_problems(view.read(MANIFEST), lock)
        found += more
        for check in DEPS_CHECKS:
            extra, extra_notes = check(ctx, view, lock)
            found += extra
            notes += extra_notes
    for note in notes:
        print(f"note: {note}")
    if found:
        raise Problem("\n".join(found))
    print(f"ok: {len(lock.crates)} crates, {len(lock.repos)} git sources cover {path.name}")
    return 0


def deps_use(ctx: Ctx, args: argparse.Namespace) -> int:
    path = lock_path_of(ctx, args)
    lock = read_lock(path)
    deps = find_deps(ctx, args.deps)
    info = use_crates(ctx, deps, lock, path, args.out, args.repo_config, args.full)
    if args.json:
        print(json.dumps(info, indent=2))
        return 0
    if info["repo_config"]:
        print(f"wrote {info['repo_config']}; delete it to build from the network again")
    home = info["cargo_home"]
    print(f"offline build ready: {info['crates']} crates, {info['git_sources']} git repositories: {home}")
    print(f"  sh:          export CARGO_HOME='{home}' CARGO_NET_OFFLINE=true")
    print(f"  PowerShell:  $env:CARGO_HOME = '{home}'; $env:CARGO_NET_OFFLINE = 'true'")
    print(f"  or one command, keeping your CARGO_HOME:  cargo --config '{info['config']}' build --locked")
    return 0


def deps_path(ctx: Ctx, args: argparse.Namespace) -> int:
    print(find_deps(ctx, args.deps))
    return 0


def pin_row(text: str, item: str) -> re.Match[str]:
    group, _, name = item.partition(" ")
    found = re.search(rf"^({re.escape(group)}[ \t]+{re.escape(name)}[ \t]+)(\S+)([ \t])", text, re.MULTILINE)
    if not found:
        raise Problem(f"no `{item}` row to pin the deps commit in")
    return found


def repin_match(found: re.Match[str], commit: str) -> str:
    """The match with its one group replaced by the commit (the whole match without a group)."""
    if not found.re.groups:
        return commit
    start = found.start()
    return found.group(0)[: found.start(1) - start] + commit + found.group(0)[found.end(1) - start :]


def deps_refresh(ctx: Ctx, args: argparse.Namespace) -> int:
    """Sync, then the commits that keep a pin true: deps' files, its repinned files, then main's lock.

    The lock pins the deps commit, and the files on deps naming that commit come after it, since a
    commit cannot hold its own hash.
    """
    cfg = ctx.cfg.deps
    lock_path = lock_path_of(ctx, args)
    lock = read_lock(lock_path)
    deps = find_deps(ctx, args.deps)
    if not git_ok("symbolic-ref", "-q", "HEAD", cwd=deps):
        raise Problem(f"{deps} is not on a branch; `deps init` makes one, or git -C {deps} checkout -B {cfg.branch} origin/{cfg.branch}")
    generated = ["crates", "pub", "cdn", MANIFEST]
    dirty = run_git("status", "--porcelain", "--", ".", *[f":!{g}" for g in generated], cwd=deps)
    if dirty:
        raise Problem(f"{deps} has changes outside {', '.join(generated)}: {dirty.splitlines()[0]}")
    changes = deps_sync_all(ctx, deps, lock, lock_path, args)
    for change in changes:
        print(change)
    source = run_git("rev-parse", "--short=8", "HEAD", cwd=ctx.root)
    run_git("add", "-A", "--", *[g for g in generated if (deps / g).exists()], cwd=deps)
    committed = bool(run_git("diff", "--cached", "--name-only", cwd=deps))
    if committed:
        body = "\n".join(changes) if len(changes) <= 40 else f"{len(changes)} files changed."
        run_git("commit", "-q", "-m", f"chore: crates/ for main at {source}", "-m", body or "no package changed", cwd=deps)
    pin_lock = ctx.root / cfg.pin.lock if cfg.pin.lock else None
    text = pinned = None
    if pin_lock:
        text = pin_lock.read_bytes().decode("utf-8")
        pinned = pin_row(text, cfg.pin.item)[2]
        scan = scan_ref(ctx.root, pinned, Allow(cfg.allow)) if has_commit(ctx.root, pinned) else None
        if not committed and scan is not None and not problems(lock, scan.tree) and not scan.flagged:
            print(f"current: deps {pinned} covers {lock_path.name}")
            return 0
    pin = run_git("rev-parse", "HEAD", cwd=deps)
    repinned = []
    for rule in cfg.repin:
        path = deps / rule.file
        data = path.read_bytes().decode("utf-8")
        rewritten, count = re.subn(rule.pattern, lambda m: repin_match(m, pin), data, flags=re.MULTILINE)
        if not count:
            raise Problem(f"{rule.file} on the deps branch has no match for {rule.pattern}")
        path.write_bytes(rewritten.encode("utf-8"))
        repinned.append(rule.file)
    if repinned:
        run_git("add", "--", *repinned, cwd=deps)
        if run_git("diff", "--cached", "--name-only", cwd=deps):
            run_git("commit", "-q", "-m", f"chore: {', '.join(repinned)} pin deps at {short(pin)}", cwd=deps)
    # The pin is the commit that holds the vendored files; the repin commit above follows it.
    if not has_commit(ctx.root, pin):
        run_git("fetch", "-q", str(deps), "HEAD", cwd=ctx.root)
    if pin_lock:
        assert text is not None and pinned is not None
        row = pin_row(text, cfg.pin.item)
        pin_lock.write_bytes((text[: row.start(2)] + pin + text[row.end(2) :]).encode("utf-8"))
        run_git("commit", "-q", "-m", f"chore: {Path(cfg.pin.lock).name} pins deps at {short(pin)}", "--", cfg.pin.lock, cwd=ctx.root)
    found, _ = ref_problems(ctx, lock, pin)
    if found:
        raise Problem(f"deps {pin} does not cover {lock_path.name}: {found[0]}")
    print(f"pinned deps {pin}; push {deps}'s branch to {cfg.branch} first, not rebased, then this one")
    return 0


README_TEXT = """# deps

Everything a build of the main branch needs from the network, as text: `crates/` is a cargo vendor
directory with one `NAME-VERSION/` directory for each package Cargo.lock names, and `manifest.json`
says which commit and lock it was made from. Nothing here is edited by hand: `tools/build.py deps
refresh` on main rewrites it, and `tools/build.py deps check` holds it to Cargo.lock.

No archive, executable, key or opaque binary is kept: a file a build reads that is one is written
as text.
"""


def deps_init(ctx: Ctx, args: argparse.Namespace) -> int:
    cfg = ctx.cfg.deps
    path = (args.path or ctx.root.with_name(ctx.root.name + "-deps")).resolve()
    if path.exists() and any(path.iterdir()):
        raise Problem(f"{path} exists and is not empty")
    if any(git_ok("rev-parse", "--verify", "-q", f"{prefix}{cfg.branch}", cwd=ctx.root) for prefix in ("", "origin/")):
        raise Problem(f"the branch {cfg.branch} exists; `deps path` checks it out")
    empty = subprocess.run(["git", "-C", str(ctx.root), "hash-object", "-t", "tree", "-w", "--stdin"], input=b"", capture_output=True)
    if empty.returncode:
        raise Problem(f"git hash-object: {empty.stderr.decode(errors='replace').strip()}")
    first = run_git("commit-tree", empty.stdout.decode().strip(), "-m", "chore: start the deps branch", cwd=ctx.root)
    run_git("branch", cfg.branch, first, cwd=ctx.root)
    run_git("worktree", "add", str(path), cfg.branch, cwd=ctx.root)
    (path / "README.md").write_bytes(README_TEXT.encode("utf-8"))
    (path / ".gitattributes").write_bytes(b"* -text\n")
    (path / "crates").mkdir(exist_ok=True)
    run_git("add", "--", "README.md", ".gitattributes", cwd=path)
    run_git("commit", "-q", "-m", "chore: README and attributes for the deps branch", cwd=path)
    print(f"made branch {cfg.branch} in {path}")
    return 0


# --- scan: refuse what an archive scanner would -----------------------------------------------------------

SCAN_LIST = 25


def report_scan(found: dict[str, list[tuple[str, int, str]]], scanned: int) -> int:
    flagged = 0
    for kind in sorted(found):
        items = found[kind]
        flagged += len(items)
        print(f"FLAG {kind}: {len(items)}")
        for sha, size, path in items[:SCAN_LIST]:
            print(f"  {sha[:10]} {size:>10}  {path}")
        if len(items) > SCAN_LIST:
            print(f"  ... {len(items) - SCAN_LIST} more")
    print(f"{'FAIL' if flagged else 'PASS'}: {flagged} flagged, {scanned} paths scanned")
    return 1 if flagged else 0


def scan_command(ctx: Ctx, args: argparse.Namespace) -> int:
    """`blob_scan.py`'s contract: what is flagged, then `PASS|FAIL: N flagged, M paths scanned`."""
    allow = Allow(ctx.cfg.deps.allow)
    found: dict[str, list[tuple[str, int, str]]] = collections.defaultdict(list)
    chosen = sum(bool(x) for x in (args.refs, args.all_refs, args.tree))
    if chosen > 1:
        raise Usage("name refs, --all-refs or --tree, not several")
    if args.tree:
        scanned = 0
        base = args.tree.resolve()
        for current, dirs, names in os.walk(base):
            dirs[:] = sorted(d for d in dirs if not (d == ".git" and Path(current) == base))
            for name in sorted(names):
                file = Path(current) / name
                rel = file.relative_to(base).as_posix()
                scanned += 1
                if file.is_symlink():
                    found["a symbolic link"].append(("-", 0, rel))
                    continue
                body = file.read_bytes()
                if any(part.lower() == ".git" for part in rel.split("/")):
                    found["a path below .git"].append((sha256(body), len(body), rel))
                if (kind := flagged_kind(body, name)) and not allow.permits(rel, kind):
                    found[kind].append((sha256(body), len(body), rel))
        return report_scan(found, scanned)
    if args.all_refs:
        names: dict[str, set[str]] = collections.defaultdict(set)
        listing = run_git("rev-list", "--objects", "--all", cwd=ctx.root, binary=True).decode("utf-8", "replace")
        for line in listing.splitlines():
            sha, _, path = line.partition(" ")
            if path:
                names[sha].add(path)
        shas = list(names)
        kinds = {}
        if shas:
            batch = subprocess.run(
                ["git", "-C", str(ctx.root), "cat-file", "--batch-check=%(objectname) %(objecttype)"],
                input=("\n".join(shas) + "\n").encode(),
                capture_output=True,
            )
            kinds = {row.split()[0]: row.split()[1] for row in batch.stdout.decode().splitlines() if row.strip()}
        blobs = [s for s in shas if kinds.get(s) == "blob"]
        for sha, body in batch_bodies(ctx.root, blobs):
            for path in sorted(names[sha]):
                if any(part.lower() == ".git" for part in path.split("/")):
                    found["a path below .git"].append((sha, len(body), path))
                kind = flagged_kind(body, path.rsplit("/", 1)[-1])
                if kind and not allow.permits(path, kind):
                    found[kind].append((sha, len(body), path))
        return report_scan(found, len(names))
    refs = args.refs or ["HEAD"]
    seen: set[tuple[str, str]] = set()
    scanned = 0
    for ref in refs:
        entries = commit_entries(ctx.root, ref)
        wanted = [e for e in entries if (e[2], e[0]) not in seen]
        seen.update((e[2], e[0]) for e in wanted)
        scanned += len(wanted)
        by_sha: dict[str, list[tuple[str, str]]] = collections.defaultdict(list)
        for path, mode, sha in wanted:
            by_sha[sha].append((path, mode))
        for sha, body in batch_bodies(ctx.root, list(by_sha)):
            for path, mode in by_sha[sha]:
                if any(part.lower() == ".git" for part in path.split("/")):
                    found["a path below .git"].append((sha, len(body), path))
                if mode == "120000":
                    found["a symbolic link"].append((sha, len(body), path))
                elif (kind := flagged_kind(body, path.rsplit("/", 1)[-1])) and not allow.permits(path, kind):
                    found[kind].append((sha, len(body), path))
    return report_scan(found, scanned)


def add_deps(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("deps", help="the deps branch: crates (and, when configured, pub packages and CDN files)")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--cargo-lock", type=Path, metavar="FILE", help="read this Cargo.lock instead of the repository's")
    common.add_argument("--deps", type=Path, metavar="DIR", help="the deps checkout (default: `deps path`)")
    sync = commands.add_parser("sync", parents=[common], help="make the deps checkout match Cargo.lock: fetch, verify, remove")
    sync.set_defaults(run=deps_sync)
    check = commands.add_parser("check", parents=[common], help="fail, naming what is missing, changed or stale")
    check.add_argument("--quick", action="store_true", help="read each package's metadata only, without re-hashing its files")
    check.add_argument("--ref", metavar="REF", help="check a commit of this repository instead of the checkout")
    check.set_defaults(run=deps_check)
    use = commands.add_parser("use", parents=[common], help="write a cargo home that replaces every source by the checkout's crates/")
    use.add_argument("--out", type=Path, metavar="DIR", help="where to write it (default: a directory in the system temp dir named for the lock)")
    use.add_argument("--repo-config", action="store_true", help="also write the repository's .cargo/config.toml")
    use.add_argument("--json", action="store_true", help="print the result as JSON")
    use.add_argument("--full", action="store_true", help="re-hash every file first (default: package metadata only)")
    use.set_defaults(run=deps_use)
    refresh = commands.add_parser("refresh", parents=[common], help="sync, commit on the deps branch, repin, then pin the commit on main")
    refresh.set_defaults(run=deps_refresh)
    where = commands.add_parser("path", parents=[common], help="print the deps checkout, adding the worktree when it is missing")
    where.set_defaults(run=deps_path)
    init = commands.add_parser("init", help="make the orphan deps branch in a new worktree")
    init.add_argument("--path", type=Path, metavar="DIR", help="the worktree (default: REPO-deps beside the repository)")
    init.set_defaults(run=deps_init)


def add_scan(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("scan", help="refuse archives, executables, keys and opaque binaries: PASS or FAIL")
    parser.add_argument("refs", nargs="*", metavar="REF", help="commits whose trees are scanned (default: HEAD)")
    parser.add_argument("--all-refs", action="store_true", help="every blob reachable from any ref, history included")
    parser.add_argument("--tree", type=Path, metavar="DIR", help="the files of a directory")
    parser.set_defaults(run=scan_command)


SECTIONS.extend([add_deps, add_scan])

# @@END-OF-SECTIONS@@


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=SELF_NAME, description="One build tool for a repository.")
    parser.add_argument("--root", type=Path, help="the repository (default: the directory above this file's tools/)")
    parser.add_argument("--config", type=Path, metavar="FILE", help="read this configuration instead of ROOT/tools/build.cfg")
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
        return args.run(Ctx(args.root.resolve(), args.config.resolve() if args.config else None), args)
    except Problem as problem:
        for line in str(problem).splitlines():
            print(f"{SELF_NAME}: {line}", file=sys.stderr)
        return 1
    except Usage as usage:
        parser.error(str(usage))
        return 2


if __name__ == "__main__":
    sys.exit(main())
