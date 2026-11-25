#!/usr/bin/env python3
"""Scan the full history of each repository under a directory for the leak patterns leak_check knows.

`leak_check` scans a working tree; a clean tree says nothing about what stays reachable in
history, and history is what a clone hands to anyone. This walks four independent surfaces
per repository, because clearing one does not clear the others:

    blobs      every unique blob reachable from any ref
    messages   every commit message
    paths      every path any commit touched
    identities every author and committer

Rules come from the pattern file leak_check uses (`$AGENTS_AUDIT_PATTERNS`, else
`audit_patterns.local.json` in the agent store, or `--patterns`). An optional key `"ours"`
lists literal substrings of the author identities that count as the owner's; without it
they are read from `git config user.name` and `user.email`.

Usage: leak_check_history.py ROOT [REPO_NAME] [BLOB_CAP] [--patterns FILE]
ROOT is a directory whose subdirectories are the repositories to scan; REPO_NAME limits the
scan to one of them; BLOB_CAP skips a repository with more blobs.

Upstream forks are handled by attribution, not exclusion: a hit is reported either way, but
one in a commit authored by someone else is labelled UPSTREAM, since rewriting it would
detach the fork. This holds on all four axes. Messages and identities carry their author;
blobs and paths are attributed by path, from one pass over `git log --all --name-only`, and
count as the owner's if the owner ever touched the path. Personal markers match as
case-insensitive substrings, wider than leak_check's case-sensitive axis: a history scan
over-reports and a person judges.

Exit 0 when no repository has hits of its own, 1 when one does, 3 without a pattern file.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

PATTERNS_ENV = "AGENTS_AUDIT_PATTERNS"


def patterns_path(override: str | None = None) -> Path:
    if override:
        return Path(override)
    if os.environ.get(PATTERNS_ENV):
        return Path(os.environ[PATTERNS_ENV])
    store = os.environ.get("AGENTS_HOME") or Path.home() / ".agents"
    return Path(store) / "audit_patterns.local.json"


# The owner's identities; anything else authoring a commit in a fork is upstream.
# Set by main() from the pattern file's `ours` or from git's own identity.
OURS = re.compile(r"(?!)")


def own_identities(data: dict) -> re.Pattern:
    names = [str(n) for n in data.get("ours", []) if str(n)]
    if not names:
        for key in ("user.name", "user.email"):
            out = subprocess.run(["git", "config", "--get", key], capture_output=True).stdout
            value = out.decode("utf-8", "replace").strip()
            if value:
                names.append(value)
    return re.compile("|".join(re.escape(n) for n in names), re.I) if names else re.compile(r"(?!)")


def git(repo: Path, *args: str, binary: bool = False):
    p = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
    )
    return p.stdout if binary else p.stdout.decode("utf-8", "replace")


def load_rules(path: Path):
    d = json.loads(path.read_text(encoding="utf-8"))
    global OURS
    OURS = own_identities(d)
    rules = []
    for marker in d.get("personal", []):
        # Case-insensitive substring: wider than leak_check's case-sensitive
        # axis, so a differently-cased marker (a certificate subject in
        # capitals) is still reported.
        rules.append(("personal:" + marker, re.compile(re.escape(marker), re.I)))
    for rule in d.get("infra", []):
        flags = re.I if rule.get("ignorecase") else 0
        rules.append(("infra:" + rule["id"], re.compile(rule["regex"], flags)))
    allow = [a for a in d.get("public_allowlist", [])]
    return rules, allow


def scrub_allowed(text: str, allow: list[str]) -> str:
    for a in allow:
        text = text.replace(a, " ")
    return text


def scan(repo: Path, rules, allow, blob_cap: int) -> dict:
    found: dict[str, set] = {}

    def hit(axis, rule_id, detail):
        found.setdefault(f"{axis} [{rule_id}]", set()).add(detail[:160])

    # --- messages + identities, one pass -------------------------------
    sep = "\x01"
    log = git(repo, "log", "--all", f"--format=%H{sep}%an <%ae>{sep}%cn <%ce>{sep}%B%x02")
    for entry in log.split("\x02"):
        if not entry.strip():
            continue
        parts = entry.split(sep, 3)
        if len(parts) < 4:
            continue
        sha, author, committer, body = parts
        upstream = not OURS.search(author)
        for surface, text in (("identity", author + " | " + committer),
                              ("message", body)):
            probe = scrub_allowed(text, allow)
            for rule_id, rx in rules:
                m = rx.search(probe)
                if m:
                    tag = f"{surface}{'(UPSTREAM)' if upstream else ''}"
                    hit(tag, rule_id, f"{sha[:9]} {m.group(0)!r} :: {author}")

    # --- paths, and the ours/upstream map the blob axis needs ----------
    #
    # One walk, two products. `--name-only` emits the paths a commit
    # touched; the `\x01` line before them carries that commit's author.
    # Merges list nothing by default, which is right: a merge introduces
    # no content of its own.
    touched_by_us: dict[str, bool] = {}
    ours_now = False
    for line in git(repo, "log", "--all", "--name-only",
                    "--format=\x01%an <%ae>").splitlines():
        if line.startswith("\x01"):
            ours_now = bool(OURS.search(line[1:]))
            continue
        line = line.strip()
        if not line:
            continue
        # Sticky: once we have touched a path it stays ours, whoever
        # commits over it afterwards.
        touched_by_us[line] = touched_by_us.get(line, False) or ours_now
        probe = scrub_allowed(line, allow)
        for rule_id, rx in rules:
            m = rx.search(probe)
            if m:
                tag = "path" if ours_now else "path(UPSTREAM)"
                hit(tag, rule_id, f"{m.group(0)!r} in {line}")

    # --- blobs ---------------------------------------------------------
    listing = git(repo, "rev-list", "--objects", "--all")
    blobs: dict[str, str] = {}
    for line in listing.splitlines():
        sha, _, path = line.partition(" ")
        if path:
            blobs.setdefault(sha, path)
    if blob_cap and len(blobs) > blob_cap:
        return {"__skipped__": {f"{len(blobs)} blobs exceeds cap {blob_cap}"}, **found}

    shas = list(blobs)
    CHUNK = 2000
    for i in range(0, len(shas), CHUNK):
        batch = shas[i:i + CHUNK]
        p = subprocess.run(["git", "-C", str(repo), "cat-file", "--batch"],
                           input="\n".join(batch).encode(), capture_output=True)
        out = p.stdout
        pos = 0
        for sha in batch:
            nl = out.find(b"\n", pos)
            if nl < 0:
                break
            header = out[pos:nl].decode("utf-8", "replace").split()
            if len(header) < 3:
                pos = nl + 1
                continue
            size = int(header[2])
            data = out[nl + 1:nl + 1 + size]
            pos = nl + 1 + size + 1
            if b"\x00" in data[:8000]:      # binary
                continue
            text = scrub_allowed(data.decode("utf-8", "replace"), allow)
            path = blobs[sha]
            # A blob whose path no commit of ours ever touched is upstream
            # content in a fork. Unknown paths (a blob reachable only from
            # a ref `log --all` did not walk) default to OURS -- an
            # unattributable hit should be looked at, not filed away.
            tag = "blob" if touched_by_us.get(path, True) else "blob(UPSTREAM)"
            for rule_id, rx in rules:
                m = rx.search(text)
                if m:
                    hit(tag, rule_id, f"{m.group(0)!r} in {path}")
    return found


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Scan the full history of every repository under ROOT.")
    ap.add_argument("root", type=Path)
    ap.add_argument("only", nargs="?", help="scan only the repository with this directory name")
    ap.add_argument("cap", nargs="?", type=int, default=0, help="skip a repository with more blobs than this")
    ap.add_argument("--patterns", help="the pattern file (default: $AGENTS_AUDIT_PATTERNS or the agent store's)")
    args = ap.parse_args(argv)
    root, only, cap = args.root, args.only, args.cap
    path = patterns_path(args.patterns)
    if not path.is_file():
        print(f"no pattern file at {path}", file=sys.stderr)
        return 3
    rules, allow = load_rules(path)
    print(f"{len(rules)} rules, allowlist={allow}\n")
    bad = 0
    for repo in sorted(p for p in root.iterdir() if (p / ".git").exists()):
        if only and only != repo.name:
            continue
        found = scan(repo, rules, allow, cap)
        if not found:
            print(f"  CLEAN  {repo.name}")
            continue
        if "__skipped__" in found:
            print(f"  SKIP   {repo.name}: {list(found.pop('__skipped__'))[0]}")
            if not found:
                continue
        ours = {k: v for k, v in found.items() if "UPSTREAM" not in k}
        up = {k: v for k, v in found.items() if "UPSTREAM" in k}
        print(f"  {'HITS  ' if ours else 'upstrm'} {repo.name}")
        for k, v in sorted(ours.items()):
            for d in sorted(v)[:6]:
                print(f"           {k}: {d}")
        for k, v in sorted(up.items()):
            print(f"           {k}: {len(v)} (upstream-authored, not ours)")
        if ours:
            bad += 1
    print(f"\nrepos with OUR leaks: {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
