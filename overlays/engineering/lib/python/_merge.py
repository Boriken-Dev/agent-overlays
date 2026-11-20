"""Conflict resolvers and the rebase or cherry-pick loop that applies them by rule.

Every resolver keeps both sides of what both sides added and refuses to leave a marker behind. Text is read as
bytes: a byte that is not UTF-8 is taken as the Latin-1 character it was written as, and the result is UTF-8.

`continue_pick` walks a rebase or cherry-pick: each conflicted path takes the first `[merge] rules` entry whose
`path` glob matches; a path no rule matches stops the loop, naming it, before anything is staged.

Standard library only.
"""

from __future__ import annotations

import difflib
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any, Callable

import _gate
import agent_tools as at

OPEN, MID, CLOSE = "<<<<<<< ", "=======", ">>>>>>> "
MARKER = re.compile(r"^(<<<<<<< |=======\r?$|>>>>>>> )", re.MULTILINE)
PREFIX = 60  # characters a line must share with a nearby one to count as the same paragraph
WINDOW = 3  # how many following lines are compared
HOWS = ("union", "rows", "both-blocks", "pub-lock", "ours-then-run")


class MergeError(Exception):
    """The file is not what the resolver needs, or a marker would remain."""


def read_text(path: Path) -> tuple[str, list[int]]:
    """The file as text, and the byte values that were not UTF-8 (read as the Latin-1 characters they were)."""
    text = path.read_bytes().decode("utf-8", errors="surrogateescape")
    bad: list[int] = []
    out = []
    for char in text:
        if 0xDC80 <= ord(char) <= 0xDCFF:
            bad.append(ord(char) - 0xDC00)
            out.append(chr(ord(char) - 0xDC00))
        else:
            out.append(char)
    return "".join(out), bad


def write_text(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))


def _is(line: str, marker: str) -> bool:
    return line.rstrip("\r") == marker if marker == MID else line.startswith(marker)


def hunks(lines: list[str]):
    """Yield ('text', line) and ('conflict', ours, theirs) over the lines."""
    i = 0
    while i < len(lines):
        if _is(lines[i], OPEN):
            try:
                mid = next(k for k in range(i, len(lines)) if _is(lines[k], MID))
                end = next(k for k in range(mid, len(lines)) if _is(lines[k], CLOSE))
            except StopIteration:
                raise MergeError("a conflict marker is not closed") from None
            yield ("conflict", lines[i + 1:mid], lines[mid + 1:end])
            i = end + 1
        else:
            yield ("text", lines[i])
            i += 1


def _finish(path: Path, out: list[str], count: int) -> int:
    result = "\n".join(out)
    if MARKER.search(result):
        raise MergeError(f"{path}: a conflict marker would remain")
    write_text(path, result)
    return count


def union(path: Path) -> int:
    """Keep HEAD's side, then the incoming lines it lacks. A line with no letter or digit (a brace, a blank) is
    structure, never a duplicate entry. Returns the number of hunks."""
    text, _ = read_text(path)
    out: list[str] = []
    count = 0
    for item in hunks(text.split("\n")):
        if item[0] == "text":
            out.append(item[1])
        else:
            _, ours, theirs = item
            out += ours + [line for line in theirs if line not in ours or not any(c.isalnum() for c in line)]
            count += 1
    return _finish(path, out, count)


def _row_key(line: str) -> str | None:
    if not line.lstrip().startswith("|"):
        return None
    cells = line.strip().strip("|").split("|")
    key = cells[0].strip().strip("`").strip()
    return key if key and not set(key) <= set("-: ") else None


def rows(path: Path) -> int:
    """Keep HEAD's table rows, then each incoming row whose first cell HEAD does not already have."""
    text, _ = read_text(path)
    out: list[str] = []
    count = 0
    for item in hunks(text.split("\n")):
        if item[0] == "text":
            out.append(item[1])
        else:
            _, ours, theirs = item
            names = {key for key in map(_row_key, ours) if key}
            out += ours + [line for line in theirs if not ((key := _row_key(line)) and key in names)]
            count += 1
    return _finish(path, out, count)


def both_blocks(path: Path) -> int:
    """Both sides appended a different block before the same closing brace: keep HEAD's, close it, then the
    incoming one, which the shared brace after the markers closes."""
    text, _ = read_text(path)
    out: list[str] = []
    count = 0
    for item in hunks(text.split("\n")):
        if item[0] == "text":
            out.append(item[1])
        else:
            out += item[1] + ["}", ""] + item[2]
            count += 1
    return _finish(path, out, count)


def show(path: Path) -> list[str]:
    """For each conflict, the word-level differences between its sides, for a merge of long lines by hand."""
    text, _ = read_text(path)
    lines = text.split("\n")
    report: list[str] = []
    n = 0
    i = 0
    while i < len(lines):
        if _is(lines[i], OPEN):
            n += 1
            mid = next(k for k in range(i, len(lines)) if _is(lines[k], MID))
            end = next(k for k in range(mid, len(lines)) if _is(lines[k], CLOSE))
            report.append(f"--- conflict {n} at line {i + 1}: ours {mid - i - 1} lines, theirs {end - mid - 1} lines")
            ours = "\n".join(lines[i + 1:mid]).split(" ")
            theirs = "\n".join(lines[mid + 1:end]).split(" ")
            for op, a1, a2, b1, b2 in difflib.SequenceMatcher(None, ours, theirs, autojunk=False).get_opcodes():
                if op != "equal":
                    report.append(f"{op} OURS: {' '.join(ours[a1:a2])[:600]} || THEIRS: {' '.join(theirs[b1:b2])[:600]}")
            i = end
        i += 1
    return report


def dupes(path: Path) -> list[str]:
    """Lines that start like a nearby line: what a union leaves when both sides of a changed paragraph were kept."""
    lines = read_text(path)[0].split("\n")
    found = []
    for i, line in enumerate(lines):
        head = line.strip()
        if len(head) < PREFIX or head.startswith(("|", "```")):
            continue
        for j in range(i + 1, min(i + 1 + WINDOW, len(lines))):
            if lines[j].strip()[:PREFIX] == head[:PREFIX] and lines[j].strip() != head:
                found.append(f"{path}:{i + 1} and {j + 1}: {head[:80]}")
    return found


def fix_utf8(path: Path) -> list[int]:
    """Rewrite bytes that are not UTF-8 as the Latin-1 characters they were written as; the bytes found."""
    text, bad = read_text(path)
    if bad:
        write_text(path, text)
    return bad


def _pub_split(text: str) -> tuple[list[str], dict[str, list[str]], list[str]]:
    lines = text.split("\n")
    if "packages:" not in lines:
        raise MergeError("no `packages:` section")
    start = lines.index("packages:") + 1
    end = next((i for i in range(start, len(lines)) if lines[i] and not lines[i].startswith(" ")), len(lines))
    packages: dict[str, list[str]] = {}
    name = None
    for line in lines[start:end]:
        if line.startswith("  ") and not line.startswith("   "):
            name = line.strip().rstrip(":")
            packages[name] = []
        if name is not None:
            packages[name].append(line)
    return lines[:start], packages, lines[end:]


def pub_lock(ours: Path, theirs: Path, out: Path) -> int:
    """Merge two pubspec.lock files package by package: every package of either, the first file's entry when both
    have one. A line-wise union cannot: entries share identical lines, which a union keeps only once."""
    head, mine, tail = _pub_split(read_text(ours)[0])
    _, other, _ = _pub_split(read_text(theirs)[0])
    for name, block in other.items():
        mine.setdefault(name, block)
    body = [line for name in sorted(mine) for line in mine[name]]
    write_text(out, "\n".join(head + body + tail))
    return len(mine)


# --- continuing a rebase or cherry-pick ----------------------------------------------------------------------------


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env)


def operation(repo: Path) -> str | None:
    gitdir = Path(_git(repo, "rev-parse", "--absolute-git-dir").stdout.strip())
    if (gitdir / "rebase-merge").is_dir() or (gitdir / "rebase-apply").is_dir():
        return "rebase"
    if (gitdir / "CHERRY_PICK_HEAD").exists():
        return "cherry-pick"
    return None


def conflicted(repo: Path) -> list[str]:
    return [p for p in _git(repo, "diff", "--name-only", "--diff-filter=U").stdout.splitlines() if p]


def match_rule(rules: list[dict[str, Any]], path: str) -> dict[str, Any] | None:
    return next((r for r in rules if _gate.glob_regex(r["path"]).match(path)), None)


def _run_rule(repo: Path, rule: dict[str, Any], path: str) -> None:
    how = rule["how"]
    target = repo / path
    if how == "union":
        union(target)
    elif how == "rows":
        rows(target)
    elif how == "both-blocks":
        both_blocks(target)
    elif how == "pub-lock":
        ours, theirs = repo / (path + ".ours"), repo / (path + ".theirs")
        try:
            for stage, file in ((2, ours), (3, theirs)):
                file.write_bytes(subprocess.run(["git", "-C", str(repo), "show", f":{stage}:{path}"],
                                                capture_output=True, check=True).stdout)
            pub_lock(ours, theirs, target)
        finally:
            ours.unlink(missing_ok=True)
            theirs.unlink(missing_ok=True)
    elif how == "ours-then-run":
        done = _git(repo, "checkout", "--ours", "--", path)
        if done.returncode:
            raise MergeError(f"git checkout --ours {path}: {done.stderr.strip()}")
        command = rule.get("run")
        if not command:
            raise MergeError(f"rule for {rule['path']}: ours-then-run needs `run`")
        argv = list(command) if isinstance(command, list) else shlex.split(command)
        done = subprocess.run(argv, cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if done.returncode:
            raise MergeError(f"{' '.join(argv)} failed ({done.returncode}): {(done.stderr or done.stdout).strip()[-400:]}")
    else:
        raise MergeError(f"rule for {rule['path']}: unknown how {how!r} (one of {', '.join(HOWS)})")


def continue_pick(repo: Path, rounds: int, say: Callable[[str], None] = print) -> int:
    """0 when the rebase or cherry-pick is finished, 1 when a path needs a person (named), 2 at the round limit."""
    repo = repo.resolve()
    rules = at.load(repo).get("merge", "rules", [])
    env = dict(os.environ, GIT_EDITOR="true")
    for _ in range(rounds):
        kind = operation(repo)
        if kind is None:
            say("done")
            return 0
        paths = conflicted(repo)
        manual = [p for p in paths if match_rule(rules, p) is None]
        if manual:
            say("MANUAL: " + " ".join(manual))
            return 1
        for path in paths:
            try:
                _run_rule(repo, match_rule(rules, path), path)
            except (MergeError, OSError, subprocess.CalledProcessError) as error:
                say(f"STOP: {path}: {error}")
                return 1
            say(f"resolved {path} ({match_rule(rules, path)['how']})")
        left = [p for p in paths if MARKER.search(read_text(repo / p)[0])]
        if left:
            say("STOP: a conflict marker remains in " + " ".join(left))
            return 1
        if paths:
            _git(repo, "add", "--", *paths)
        done = _git(repo, kind, "--continue", env=env)
        nothing_staged = _git(repo, "diff", "--cached", "--quiet").returncode == 0
        if done.returncode and kind == "cherry-pick" and not conflicted(repo) and nothing_staged:
            _git(repo, "cherry-pick", "--skip", env=env)  # the pick became empty
        elif done.returncode and not conflicted(repo) and operation(repo):
            say(f"STOP: git {kind} --continue failed: {(done.stderr or done.stdout).strip()[-300:]}")
            return 1
    say("round limit reached")
    return 2
