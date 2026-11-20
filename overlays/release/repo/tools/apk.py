#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# From Boriken-Dev/agent-overlays, overlays/release/repo/tools/apk.py, at commit SOURCE.
# Copyright (c) 2026 Jose A.
"""Inspects built Android APKs: forbidden strings, permissions, ABIs and required libraries.

This file is copied into a repository as tools/apk.py and configured by the `[apk]` table of
tools/build.cfg. Standard library only, Python 3.11.0 or later.

    apk.py check [--root DIR] [ALLOW_FLAG] APK...

Keys of `[apk]` (all optional):

    forbid           [[apk.forbid]]  each `string` (a regex applied to the bytes of every zip entry),
                                     `control_base64` (base64 of a sample the regex must match) and
                                     an optional `name`; a hit is reported by entry and size, never
                                     by its text
    max_entry_bytes  int   268435456  entries larger than this are not scanned, and are listed
    allow_flag       str   ""         a command-line flag (for example "--allow-sideload-key") that
                                      reports forbidden hits without failing; such an APK must
                                      never go to a store
    permissions      table            `want` strs [] (must appear in AndroidManifest.xml) and
                                      `unwanted` strs [] (must not)
    abis             strs  []         when set, the ABIs under lib/ must equal this set
    libs             strs  []         file names every shipped ABI must hold under lib/<abi>/

Before scanning, each forbid entry's control sample must match its regex; a check that cannot
fail proves nothing, so a control that does not match (or is empty) exits 2.

Exit codes: 0 ok, 1 problems (one line each on stderr, prefixed `apk.py:`), 2 usage.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import re
import sys
import tomllib
import zipfile
from pathlib import Path
from typing import Any

NAME = "apk.py"
CONFIG = "tools/build.cfg"
MANIFEST = "AndroidManifest.xml"
DEFAULT_MAX_ENTRY_BYTES = 268435456


class Problem(Exception):
    """What stops a command; each line of its text is printed as one problem."""


class Usage(Exception):
    """A command line or check that cannot be run, which exits 2."""


def _is_strs(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _read_table(root: Path) -> dict[str, Any]:
    path = root / CONFIG
    if not path.is_file():
        return {}
    try:
        table = tomllib.loads(path.read_text(encoding="utf-8")).get("apk", {})
    except tomllib.TOMLDecodeError as error:
        raise Problem(f"build.cfg: {error}") from error
    if not isinstance(table, dict):
        raise Problem("build.cfg: apk: expected table")
    return table


def settings(table: dict[str, Any]) -> dict[str, Any]:
    """The `[apk]` table with defaults filled in; every fault is reported."""
    errors: list[str] = []
    top = {"forbid", "max_entry_bytes", "allow_flag", "permissions", "abis", "libs"}
    out: dict[str, Any] = {
        "forbid": [], "max_entry_bytes": DEFAULT_MAX_ENTRY_BYTES, "allow_flag": "",
        "permissions": {"want": [], "unwanted": []}, "abis": [], "libs": [],
    }

    def wrong(path: str, kind: str, value: Any) -> None:
        errors.append(f"build.cfg: {path}: expected {kind}, found {type(value).__name__}")

    for key, value in table.items():
        if key not in top:
            errors.append(f"build.cfg: apk.{key}: unknown key")
        elif key == "max_entry_bytes":
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                out[key] = value
            else:
                wrong("apk.max_entry_bytes", "non-negative int", value)
        elif key == "allow_flag":
            if isinstance(value, str):
                out[key] = value
            else:
                wrong("apk.allow_flag", "str", value)
        elif key in ("abis", "libs"):
            if _is_strs(value):
                out[key] = value
            else:
                wrong(f"apk.{key}", "strs", value)
        elif key == "permissions":
            if not isinstance(value, dict):
                wrong("apk.permissions", "table", value)
                continue
            for sub, item in value.items():
                if sub not in ("want", "unwanted"):
                    errors.append(f"build.cfg: apk.permissions.{sub}: unknown key")
                elif _is_strs(item):
                    out["permissions"][sub] = item
                else:
                    wrong(f"apk.permissions.{sub}", "strs", item)
        elif key == "forbid":
            if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
                wrong("apk.forbid", "tables", value)
                continue
            for index, entry in enumerate(value):
                where = f"apk.forbid[{index}]"
                for sub, item in entry.items():
                    if sub not in ("string", "control_base64", "name"):
                        errors.append(f"build.cfg: {where}.{sub}: unknown key")
                    elif not isinstance(item, str):
                        wrong(f"{where}.{sub}", "str", item)
                if not isinstance(entry.get("string"), str) or not entry.get("string"):
                    errors.append(f"build.cfg: {where}.string: required")
                    continue
                out["forbid"].append({
                    "label": entry.get("name") or str(index),
                    "string": entry["string"],
                    "control": entry.get("control_base64", ""),
                })
    if errors:
        raise Problem("\n".join(errors))
    return out


def compile_patterns(forbid: list[dict[str, str]]) -> list[tuple[str, re.Pattern[bytes]]]:
    """The compiled patterns, after each has matched its planted control sample."""
    patterns = []
    for item in forbid:
        try:
            pattern = re.compile(item["string"].encode("utf-8"))
        except re.error as error:
            raise Problem(f"build.cfg: forbidden pattern {item['label']}: invalid regex: {error}") from error
        try:
            sample = base64.b64decode(item["control"], validate=True)
        except (binascii.Error, ValueError):
            sample = b""
        if not sample or not pattern.search(sample):
            raise Usage(
                f"forbidden pattern {item['label']}: "
                "the check no longer detects its planted sample; fix it first"
            )
        patterns.append((item["label"], pattern))
    return patterns


def scan_entries(
    archive: zipfile.ZipFile, patterns: list[tuple[str, re.Pattern[bytes]]], limit: int
) -> tuple[int, list[str], list[str]]:
    """(entries scanned, hit lines, skipped-entry lines)."""
    hits: list[str] = []
    skipped: list[str] = []
    count = 0
    for info in archive.infolist():
        if info.is_dir():
            continue
        if info.file_size > limit:
            skipped.append(f"skipped {info.filename}: {info.file_size} bytes exceeds max_entry_bytes")
            continue
        count += 1
        data = archive.read(info)
        for label, pattern in patterns:
            for match in pattern.finditer(data):
                hits.append(
                    f"{info.filename}: a match of {match.end() - match.start()} bytes "
                    f"for forbidden {label}"
                )
    return count, hits, skipped


def check_permissions(archive: zipfile.ZipFile, want: list[str], unwanted: list[str]) -> list[str]:
    try:
        data = archive.read(MANIFEST)
    except KeyError:
        return [f"{MANIFEST}: missing, so permissions cannot be checked"]
    text = "\n".join((
        data.decode("utf-16-le", "ignore"),
        data[1:].decode("utf-16-le", "ignore"),
        data.decode("utf-8", "ignore"),
    ))
    problems = [f"wanted permission missing: {name}" for name in want if name not in text]
    problems += [f"unwanted permission present: {name}" for name in unwanted if name in text]
    return problems


def libs_by_abi(archive: zipfile.ZipFile) -> dict[str, dict[str, int]]:
    """ABI -> {library file name: uncompressed size} for lib/<abi>/*.so."""
    found: dict[str, dict[str, int]] = {}
    for info in archive.infolist():
        parts = info.filename.split("/")
        if len(parts) == 3 and parts[0] == "lib" and parts[2].endswith(".so"):
            found.setdefault(parts[1], {})[parts[2]] = info.file_size
    return found


def check_abis(found: dict[str, dict[str, int]], abis: list[str]) -> list[str]:
    problems = []
    for abi in sorted(set(found) - set(abis)):
        size = sum(found[abi].values())
        problems.append(f"surplus ABI {abi}: {len(found[abi])} libs, {size} bytes")
    problems += [f"missing ABI {abi}" for abi in abis if abi not in found]
    return problems


def check_libs(found: dict[str, dict[str, int]], libs: list[str]) -> list[str]:
    return [
        f"lib/{abi}/{name} missing"
        for abi in sorted(found) for name in libs if name not in found[abi]
    ]


def check_apk(path: Path, cfg: dict[str, Any], patterns: list[tuple[str, re.Pattern[bytes]]]
              ) -> tuple[list[str], list[str]]:
    """(forbidden hits, other problems) for one APK."""
    print(f"reading {path.name}")
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as error:
        return [], [f"not a zip archive: {error}"]
    with archive:
        count, hits, skipped = scan_entries(archive, patterns, cfg["max_entry_bytes"])
        print(f"scanned {count} entries")
        for line in skipped:
            print(line)
        problems: list[str] = []
        ran: list[str] = []
        if patterns:
            ran.append("forbidden strings")
        perms = cfg["permissions"]
        if perms["want"] or perms["unwanted"]:
            ran.append("permissions")
            problems += check_permissions(archive, perms["want"], perms["unwanted"])
        found = libs_by_abi(archive)
        if cfg["abis"]:
            ran.append("abis")
            problems += check_abis(found, cfg["abis"])
        if cfg["libs"]:
            ran.append("libs")
            problems += check_libs(found, cfg["libs"])
    if not hits and not problems:
        for name in ran:
            print(f"PASS {name}")
    return hits, problems


def run(root: Path, paths: list[Path], allow: bool, cfg: dict[str, Any]) -> int:
    for path in paths:
        if not path.is_file():
            raise Usage(f"APK not found: {path}")
    patterns = compile_patterns(cfg["forbid"])
    lines: list[str] = []
    allowed = False
    for path in paths:
        hits, problems = check_apk(path, cfg, patterns)
        lines += [f"{path.name}: {line}" for line in problems]
        if allow and hits:
            allowed = True
            for hit in hits:
                print(f"{NAME}: {path.name}: {hit}", file=sys.stderr)
        else:
            lines += [f"{path.name}: {hit}" for hit in hits]
    if allowed:
        print(
            f"NOTICE: {cfg['allow_flag']} reported forbidden content without failing; "
            "this artifact must never go to a store.",
            file=sys.stderr,
        )
    if lines:
        raise Problem("\n".join(lines))
    return 0


def main(argv: list[str] | None = None) -> int:
    default_root = Path(__file__).resolve().parent.parent
    first = argparse.ArgumentParser(add_help=False)
    first.add_argument("--root", type=Path, default=default_root)
    known, _ = first.parse_known_args(argv)
    try:
        cfg = settings(_read_table(known.root.resolve()))
    except Problem as error:
        for line in str(error).splitlines():
            print(f"{NAME}: {line}", file=sys.stderr)
        return 1
    parser = argparse.ArgumentParser(prog=NAME, description="Inspect built Android APKs.")
    parser.add_argument("command", choices=["check"])
    parser.add_argument("--root", type=Path, default=default_root)
    if cfg["allow_flag"]:
        parser.add_argument(cfg["allow_flag"], dest="allow", action="store_true",
                            help="report forbidden hits without failing")
    parser.add_argument("apks", nargs="*", type=Path, metavar="APK")
    try:
        args = parser.parse_intermixed_args(argv)
    except SystemExit as exit_:
        return 0 if exit_.code in (0, None) else 2
    try:
        if not args.apks:
            raise Usage("no APK given")
        return run(args.root.resolve(), args.apks, getattr(args, "allow", False), cfg)
    except Usage as error:
        print(f"{NAME}: {error}", file=sys.stderr)
        return 2
    except Problem as error:
        for line in str(error).splitlines():
            print(f"{NAME}: {line}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
