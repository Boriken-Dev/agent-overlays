"""Affected-package selection for a gate step: which workspace packages a branch's changes reach.

A package is affected when a changed file lies under its directory, or when it depends (as a normal, dev or build
dependency) on an affected package, transitively. The step's cargo command then runs with `-p` for each of them in
place of `--workspace`. Pure functions over `cargo metadata --format-version 1 --no-deps` output; the gate does the
git and the process work.

Standard library only.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

DEFAULT_FULL = ("Cargo.toml", "Cargo.lock", "rust-toolchain", "rust-toolchain.toml", ".cargo/**")
ROOT_PACKAGE_DIRS = ("src/", "tests/", "benches/", "examples/")
SHELL_WORDS = {"&&", "||", "|", ";", "&", ">", ">>", "<"}


class AffectedError(Exception):
    """A step cannot take part in affected selection; the message says why."""


@dataclass
class Package:
    name: str
    dir: str  # relative to the repository top level, "" when the package is the top level itself
    deps: set[str] = field(default_factory=set)


@dataclass
class Selection:
    full: bool = False
    why_full: str = ""
    packages: list[str] = field(default_factory=list)
    direct: set[str] = field(default_factory=set)
    reasons: dict[str, list[str]] = field(default_factory=dict)  # package -> why it is in the set
    outside: list[str] = field(default_factory=list)  # changed paths that belong to no package


def load_metadata(cwd: Path) -> dict[str, Any]:
    done = subprocess.run(["cargo", "metadata", "--format-version", "1", "--no-deps"], cwd=str(cwd),
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if done.returncode:
        raise AffectedError(f"cargo metadata failed in {cwd}: {done.stderr.strip() or done.stdout.strip()}")
    return json.loads(done.stdout)


def _rel(path: str, top: Path) -> str:
    text = os.path.relpath(str(Path(path).resolve()), str(top.resolve())).replace("\\", "/")
    return "" if text == "." else text


def packages_from_metadata(meta: dict[str, Any], top: Path) -> list[Package]:
    """The workspace's packages; `top` is the repository top level that changed paths are relative to."""
    found = [Package(p["name"], _rel(str(Path(p["manifest_path"]).parent), top)) for p in meta["packages"]]
    names = {p.name for p in found}
    for package, raw in zip(found, meta["packages"]):
        package.deps = {d["name"] for d in raw.get("dependencies", []) if d["name"] in names} - {package.name}
    return found


def workspace_root_rel(meta: dict[str, Any], top: Path) -> str:
    return _rel(meta["workspace_root"], top)


def full_patterns(root_rel: str, extra: list[str]) -> list[str]:
    prefix = root_rel + "/" if root_rel else ""
    return [prefix + g for g in DEFAULT_FULL] + list(extra)


def _owner(path: str, packages: list[Package]) -> Package | None:
    best: Package | None = None
    for package in packages:
        if package.dir == "":
            inside = path.startswith(ROOT_PACKAGE_DIRS) or path in ("build.rs", "Cargo.toml")
        else:
            inside = path.startswith(package.dir + "/")
        if inside and (best is None or len(package.dir) > len(best.dir)):
            best = package
    return best


def select(packages: list[Package], changed: list[str] | None, is_full: Callable[[str], bool],
           excluded: set[str] = frozenset()) -> Selection:
    """The packages to test for `changed` (paths relative to the top level; None when there is nothing to compare
    with). `is_full` says a path means the whole workspace."""
    if changed is None:
        return Selection(full=True, why_full="there is no REMOTE/MAIN ref to compare against")
    for path in changed:
        if is_full(path):
            return Selection(full=True, why_full=f"{path} changed")
    selection = Selection()
    for path in changed:
        owner = _owner(path, packages)
        if owner is None:
            selection.outside.append(path)
        else:
            selection.direct.add(owner.name)
            selection.reasons.setdefault(owner.name, []).append(path)
    affected = set(selection.direct)
    queue = [p.name for p in packages if p.name in affected]
    while queue:
        current = queue.pop(0)
        for package in packages:
            if current in package.deps and package.name not in affected:
                affected.add(package.name)
                selection.reasons[package.name] = [f"depends on {current}"]
                queue.append(package.name)
    selection.packages = [p.name for p in packages if p.name in affected and p.name not in excluded]
    return selection


# --- the step's command ---------------------------------------------------------------------------------------


def cargo_argv(run: Any) -> list[str]:
    argv = list(run) if isinstance(run, list) else shlex.split(run)
    if not argv or Path(argv[0]).stem != "cargo":
        raise AffectedError("`affected` needs a step that runs cargo")
    if any(token in SHELL_WORDS for token in argv):
        raise AffectedError("`affected` needs a single cargo command, not a shell pipeline")
    return argv


def subcommand_index(argv: list[str]) -> int:
    i = 1
    while i < len(argv) and argv[i].startswith("+"):
        i += 1
    if i >= len(argv):
        raise AffectedError("`affected`: no cargo subcommand in the step")
    return i


def split_excluded(argv: list[str]) -> tuple[list[str], set[str]]:
    """`argv` without `--exclude` options, and the packages they named."""
    out, excluded, i = [], set(), 0
    while i < len(argv):
        token = argv[i]
        if token == "--exclude" and i + 1 < len(argv):
            excluded.add(argv[i + 1])
            i += 2
        elif token.startswith("--exclude="):
            excluded.add(token.split("=", 1)[1])
            i += 1
        else:
            out.append(token)
            i += 1
    return out, excluded


def slow_skips(slow: list[dict[str, Any]], known: set[str], selection: Selection) -> tuple[list[str], list[str]]:
    """(libtest `--skip` filters, report lines) for the `slow` entries whose package has no change of its own.
    Entry: `{ package = "name", skip = "filter" }` or `skip = ["a", "b"]`."""
    skips, notes = [], []
    for entry in slow:
        package = entry.get("package")
        raw = entry.get("skip")
        if not package or not raw:
            raise AffectedError(f"a `slow` entry needs `package` and `skip`: {entry}")
        if package not in known:
            raise AffectedError(f"`slow` names {package}, which is not a workspace package")
        filters = [raw] if isinstance(raw, str) else list(raw)
        if selection.full or package in selection.direct:
            notes.append(f"slow {', '.join(filters)}: run ({package} changed)")
        else:
            skips += filters
            notes.append(f"slow {', '.join(filters)}: skipped ({package} unchanged)")
    return skips, notes


def rewrite(argv: list[str], names: list[str], skips: list[str]) -> list[str]:
    """`argv` with `--workspace` replaced by `-p NAME` for each package and `--skip FILTER` added after `--`."""
    argv, _ = split_excluded(argv)
    sub = subcommand_index(argv)
    for token in argv:
        if token in ("-p", "--package") or token.startswith(("--package=", "-p=")):
            raise AffectedError("an `affected` step names no `-p` of its own: use `--workspace`")
    argv = [t for t in argv if t not in ("--workspace", "--all")]
    argv = argv[:sub + 1] + [x for name in names for x in ("-p", name)] + argv[sub + 1:]
    if skips:
        if sub < len(argv) and argv[sub] != "test":
            raise AffectedError("`slow` applies to a `cargo test` step")
        if "--" not in argv:
            argv.append("--")
        argv += [x for filt in skips for x in ("--skip", filt)]
    return argv


def report(step_name: str, selection: Selection, notes: list[str], names_args: list[str] | None) -> list[str]:
    """The lines that say what the step runs and why."""
    if selection.full:
        return [f"AFFECTED {step_name}: whole workspace ({selection.why_full})"]
    if not selection.packages:
        lines = [f"AFFECTED {step_name}: no package affected"]
        if selection.outside:
            lines.append("  outside every package: " + _some(selection.outside))
        return lines
    lines = [f"AFFECTED {step_name}: {' '.join(names_args or [])}"]
    for name in selection.packages:
        lines.append(f"  {name} <- {_some(selection.reasons.get(name, []))}")
    if selection.outside:
        lines.append("  outside every package: " + _some(selection.outside))
    return lines + [f"  {n}" for n in notes]


def _some(items: list[str], limit: int = 3) -> str:
    shown = ", ".join(items[:limit])
    return shown + (f" and {len(items) - limit} more" if len(items) > limit else "")
