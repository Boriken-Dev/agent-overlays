"""repo-guard: commit to and push a repository's main under one identity, keep its deps branch in step, run CI once.

    repo-guard commit [-C DIR] -m MSG [-a] [PATH...]
    repo-guard push   [-C DIR] [--no-deps]
    repo-guard deps   [-C DIR]
    repo-guard ci     [-C DIR] [--ref REF] [--timeout S] [--find-timeout S] [--every S] TOPIC

`commit` refuses a message carrying agent attribution (a `Co-Authored-By:` naming a model or an assistant, a
`*-Session:` trailer, a session URL, a "Generated with" footer) and commits as the configured identity.

`push` checks every commit above REMOTE/MAIN (author and committer are the identity, no forbidden trailer), that
HEAD fast-forwards REMOTE/MAIN, the engineering overlay's `leak-check` script, the repository's `tools/build.py scan --all-refs` and each
`pre_push` command; then pushes `HEAD:MAIN` and fast-forwards the `checkout` when one is set. When
`tools/build.cfg` has a `[deps]` table and a lock file it names changed, `deps` follows.

`deps` runs `tools/build.py deps refresh`, scans the deps checkout, pushes the deps branch and then main, and
appends one line to the `log` file when one is set.

`ci` tags REF as `ci-TOPIC-<UTC yyyymmddHHMMSS>`, pushes the tag, waits for the run through the release host,
prints each job's conclusion and the failed steps' last lines, and deletes the tag locally and on the remote
even when the run failed or waiting was interrupted. Exit 0 only when the run succeeded.

Configuration (the schema is in the engineering overlay's kb/AGENT_TOOLS.md):
    ~/.agents/agent-tools.local.toml   [identity] name, email
    <repo>/.agents/agent-tools.toml    [repo_guard] main, remote, checkout, log, pre_push, forbidden,
                                       ci_timeout, ci_find_timeout, scan_allow

Exit codes: 0 ok, 1 refused or failed, 2 configuration or usage.
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
OVERLAY = HERE.parent


def _load_lib() -> Any:
    """The engineering overlay's reader: on the path when dotagents composes the overlays, else beside this one."""
    try:
        import agent_tools
        return agent_tools
    except ImportError:
        pass
    roots = [os.environ.get("ENGINEERING_OVERLAY_ROOT"), str(OVERLAY.parent / "engineering")]
    for root in roots:
        lib = Path(root) / "lib" / "python" if root else None
        if lib and (lib / "agent_tools.py").is_file():
            sys.path.insert(0, str(lib))
            import agent_tools
            return agent_tools
    raise SystemExit("repo-guard: the engineering overlay's lib/python/agent_tools.py is not on the path; "
                     "install the engineering overlay (the release overlay requires it)")


at = _load_lib()
sys.path.insert(0, str(OVERLAY / "tools"))
from hosts import ReleaseHost  # noqa: E402

DEFAULT_MAIN = "main"
DEFAULT_REMOTE = "origin"
DEFAULT_CI_TIMEOUT = 3600
DEFAULT_CI_FIND_TIMEOUT = 120
DEFAULT_EVERY = 5
FAILED_TAIL_LINES = 40


class Refused(Exception):
    """A check failed: nothing was committed or pushed past it."""


def run(cmd: list[Any], cwd: Path | str | None = None, env: dict[str, str] | None = None,
        check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    argv = [str(c) for c in cmd]
    print("+", " ".join(argv), flush=True)
    done = subprocess.run(argv, cwd=cwd, env=env, text=True, capture_output=capture, encoding="utf-8", errors="replace")
    if check and done.returncode != 0:
        if capture:
            sys.stdout.write((done.stdout or "")[-4000:])
            sys.stderr.write((done.stderr or "")[-4000:])
        raise Refused(f"failed ({done.returncode}): {' '.join(argv)}")
    return done


def git(repo: Path | str, *args: str, check: bool = True) -> str:
    return run(["git", "-C", repo, *args], check=check, capture=True).stdout.strip()


class Guard:
    """One repository's settings: the merged configuration and the paths the checks need."""

    def __init__(self, repo: Path | str):
        self.repo = Path(repo).resolve()
        self.config = at.load(self.repo)
        self.main = self.config.get("repo_guard", "main", DEFAULT_MAIN)
        self.remote = self.config.get("repo_guard", "remote", DEFAULT_REMOTE)

    @property
    def identity(self) -> tuple[str, str]:
        return self.config.identity()

    def identity_args(self) -> list[str]:
        name, email = self.identity
        return ["-c", f"user.name={name}", "-c", f"user.email={email}"]

    def identity_env(self) -> dict[str, str]:
        name, email = self.identity
        return dict(os.environ, GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email,
                    GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=email)

    def check_message(self, text: str, what: str = "the commit message") -> None:
        found = self.config.check_message(text)
        if found:
            raise Refused(f"agent attribution in {what}: " + "; ".join(found))

    def build_py(self) -> Path:
        mine = self.repo / "tools" / "build.py"
        return mine if mine.is_file() else OVERLAY / "repo" / "tools" / "build.py"

    def build(self, *args: Any, check: bool = True, capture: bool = True, env: dict[str, str] | None = None):
        return run([sys.executable, self.build_py(), "--root", self.repo, *args],
                   check=check, capture=capture, env=env)

    def build_cfg(self) -> dict[str, Any]:
        path = self.repo / "tools" / "build.cfg"
        if not path.is_file():
            return {}
        try:
            return tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise Refused(f"{path}: cannot be read: {error}") from None

    def deps_locks(self) -> list[str]:
        """The files the deps branch must follow; empty when the repository has no `[deps]` table.

        Each vendor's Cargo.lock (`cargo_lock` when no vendor is listed), the SDK lock when a vendor is a crate named by one of
        its rows, the pubspec.lock, each tree's pin file and the CDN index.
        """
        cfg = self.build_cfg()
        deps = cfg.get("deps")
        if not isinstance(deps, dict):
            return []
        vendors = [v for v in deps.get("vendor", []) if isinstance(v, dict)] if isinstance(deps.get("vendor"), list) else []
        if vendors:
            locks = [v.get("lock") for v in vendors]
            if any(v.get("item") for v in vendors):
                sdk = cfg.get("sdk")
                locks.append(sdk.get("lock", "tools/sdk.lock") if isinstance(sdk, dict) else "tools/sdk.lock")
        else:
            locks = [deps.get("cargo_lock", "Cargo.lock")]
        pub = deps.get("pub")
        if isinstance(pub, dict):
            locks.append(pub.get("lock"))
        if isinstance(deps.get("tree"), list):
            locks += [t.get("pin") for t in deps["tree"] if isinstance(t, dict)]
        cdn = deps.get("cdn")
        if isinstance(cdn, dict):
            locks.append(cdn.get("index"))
        return list(dict.fromkeys(lock for lock in locks if lock))

    def deps_branch(self) -> str:
        deps = self.build_cfg().get("deps")
        return (deps or {}).get("branch", "deps") if isinstance(deps, dict) else "deps"


def last_line(text: str) -> str:
    return (text.strip().splitlines() or ["(no output)"])[-1]


# --- commit ---------------------------------------------------------------------------------------------------


def cmd_commit(args: argparse.Namespace) -> None:
    guard = Guard(args.C)
    guard.check_message(args.m)
    extra = ["-a"] if args.a else []
    run(["git", "-C", guard.repo, *guard.identity_args(), "commit", *extra, "-m", args.m,
         *(["--", *args.paths] if args.paths else [])])
    print(git(guard.repo, "log", "--oneline", "-1"))


# --- push -----------------------------------------------------------------------------------------------------


def check_commits(guard: Guard, before: str, head: str) -> None:
    """Every commit in before..head: the identity on both ends and a message without attribution."""
    name, email = guard.identity
    want = f"{name} <{email}>"
    for sha in git(guard.repo, "rev-list", "--reverse", f"{before}..{head}").splitlines():
        author, committer, body = git(guard.repo, "show", "-s", "--format=%an <%ae>%x00%cn <%ce>%x00%B", sha).split("\0", 2)
        if author.strip() != want or committer.strip() != want:
            raise Refused(f"commit {sha[:10]} is by {author.strip()} / {committer.strip()}, not {want}")
        guard.check_message(body, f"commit {sha[:10]}")


def _leak_check_script() -> Path:
    """The engineering overlay's `leak-check` script: `$ENGINEERING_OVERLAY_ROOT`, else the overlay beside this one."""
    for root in (os.environ.get("ENGINEERING_OVERLAY_ROOT"), str(OVERLAY.parent / "engineering")):
        script = Path(root) / "skills" / "leak-check" / "leak_check.py" if root else None
        if script and script.is_file():
            return script
    raise Refused("the engineering overlay's skills/leak-check/leak_check.py is not found, so leak-check cannot run")


def leak_check(guard: Guard) -> None:
    python = os.environ.get("AGENTS_PYTHON") or sys.executable
    out = run([python, "-I", str(_leak_check_script()), guard.repo], check=False, capture=True)
    verdict = last_line(out.stdout)
    print(verdict)
    if out.returncode != 0 or not verdict.startswith("PASS"):
        raise Refused(f"leak-check: {verdict}")


FLAG_HEADER = re.compile(r"^FLAG (.+): (\d+)$")
FLAG_ROW = re.compile(r"^  ([0-9a-f]{6,64}) +\d+  (.+)$")
FAIL_LINE = re.compile(r"^FAIL: (\d+) flagged, (\d+) paths scanned$")


def _reachable(guard: Guard, ref: str) -> set[tuple[str, str]]:
    """(object id, path) of every object reachable from REF (or REMOTE/REF), for a ref-scoped allowance."""
    for name in (ref, f"{guard.remote}/{ref}"):
        out = run(["git", "-C", guard.repo, "rev-list", "--objects", name], check=False, capture=True)
        if out.returncode == 0:
            return {(sha, path) for sha, _, path in (row.partition(" ") for row in out.stdout.splitlines()) if path}
    return set()


def allowed_hits(guard: Guard, scan_output: str) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]]]:
    """Split a scan's flagged rows into (allowed, refused) by `[repo_guard] scan_allow`; each row is (kind, id, path).

    An entry names a path glob and may name a `kind`, a `ref` the blob must be reachable from, and a `reason`. A scan that
    truncated a list (`... N more`) cannot be matched row by row, so nothing in it is allowed.
    """
    rows: list[tuple[str, str, str]] = []
    kind = ""
    for line in scan_output.splitlines():
        header, row = FLAG_HEADER.match(line), FLAG_ROW.match(line)
        if header:
            kind = header.group(1)
        elif row:
            rows.append((kind, row.group(1), row.group(2)))
    entries = guard.config.get("repo_guard", "scan_allow", [])
    flagged = FAIL_LINE.match(last_line(scan_output))
    if not entries or not flagged or int(flagged.group(1)) != len(rows):
        return [], rows
    reach: dict[str, set[tuple[str, str]]] = {}
    allowed, refused = [], []
    for kind, sha, path in rows:
        reason = None
        for entry in entries:
            if not fnmatch.fnmatchcase(path, entry["path"]) or entry.get("kind", kind) != kind:
                continue
            ref = entry.get("ref")
            if ref:
                found = reach.setdefault(ref, _reachable(guard, ref))
                if not any(s.startswith(sha) and p == path for s, p in found):
                    continue
            reason = entry.get("reason", "")
            break
        if reason is None:
            refused.append((kind, sha, path))
        else:
            allowed.append((kind, sha, path + (f" ({reason})" if reason else "")))
    return allowed, refused


def blob_scan(guard: Guard, *scan_args: Any) -> None:
    out = guard.build("scan", *scan_args, check=False)
    verdict = last_line(out.stdout)
    if out.returncode == 0 and verdict.startswith("PASS"):
        print(verdict)
        return
    allowed, refused = allowed_hits(guard, out.stdout)
    for _, _, path in allowed:
        print(f"allowed: {path}")
    if allowed and not refused and FAIL_LINE.match(verdict):
        print(f"PASS: 0 flagged, {len(allowed)} allowed")
        return
    for kind, sha, path in refused:
        print(f"refused {kind}: {sha} {path}")
    print(verdict)
    raise Refused(f"scan: {verdict}")


def fast_forward_checkout(guard: Guard) -> None:
    configured = guard.config.get("repo_guard", "checkout")
    if not configured:
        return
    checkout = Path(configured)
    if not checkout.is_absolute():
        checkout = guard.repo / checkout
    if git(checkout, "status", "--porcelain", "--untracked-files=no"):
        print(f"checkout {checkout} has local changes: not fast-forwarded")
        return
    git(checkout, "fetch", "-q", guard.remote)
    if git(checkout, "rev-parse", "--abbrev-ref", "HEAD") != guard.main:
        print(f"checkout {checkout} is not on {guard.main}: not fast-forwarded")
        return
    run(["git", "-C", checkout, "merge", "--ff-only", "-q", f"{guard.remote}/{guard.main}"])
    print("checkout:", git(checkout, "log", "--oneline", "-1"))


def cmd_push(args: argparse.Namespace) -> None:
    guard = Guard(args.C)
    guard.identity  # a missing identity is reported before anything runs
    push_main(guard, deps=not args.no_deps)


def push_main(guard: Guard, deps: bool) -> None:
    repo, remote, main = guard.repo, guard.remote, guard.main
    git(repo, "fetch", "-q", remote)
    upstream = f"{remote}/{main}"
    before = git(repo, "rev-parse", upstream)
    head = git(repo, "rev-parse", "HEAD")
    if git(repo, "status", "--porcelain", "--untracked-files=no"):
        raise Refused(f"{repo} has uncommitted changes")
    if run(["git", "-C", repo, "merge-base", "--is-ancestor", before, head], check=False).returncode != 0:
        raise Refused(f"HEAD is not a fast-forward of {upstream}")
    if head != before:
        check_commits(guard, before, head)
    leak_check(guard)
    blob_scan(guard, "--all-refs")
    for extra in guard.config.get("repo_guard", "pre_push", []):
        run(extra if isinstance(extra, list) else shlex.split(extra), cwd=repo)
    if head == before:
        print(f"nothing to push: {main} is {git(repo, 'log', '--oneline', '-1', upstream)}")
    else:
        run(["git", "-C", repo, "push", remote, f"HEAD:{main}"])
        git(repo, "fetch", "-q", remote)
        print(f"{main}:", git(repo, "log", "--oneline", "-1", upstream))
    fast_forward_checkout(guard)
    locks = guard.deps_locks()
    if not (deps and locks):
        return
    if head != before and git(repo, "diff", "--name-only", before, head, "--", *locks):
        refresh_deps(guard)
    else:
        print("deps: no lock file changed")


# --- deps -----------------------------------------------------------------------------------------------------


def refresh_deps(guard: Guard) -> None:
    repo, remote = guard.repo, guard.remote
    if not guard.deps_locks():
        raise Refused(f"{repo / 'tools' / 'build.cfg'} has no [deps] table")
    env = guard.identity_env()
    git(repo, "fetch", "-q", remote)
    where = last_line(guard.build("deps", "path", env=env).stdout)
    deps = Path(where)
    main_before = git(repo, "rev-parse", "HEAD")
    deps_before = git(deps, "rev-parse", "HEAD")
    out = guard.build("deps", "refresh", env=env)
    print(out.stdout.strip())
    main_after = git(repo, "rev-parse", "HEAD")
    deps_after = git(deps, "rev-parse", "HEAD")
    if (main_before, deps_before) == (main_after, deps_after):
        print("deps: already current")
        return
    blob_scan(guard, "--tree", deps)
    branch = guard.deps_branch()
    run(["git", "-C", deps, "push", remote, f"HEAD:{branch}"])
    if main_after != main_before:
        push_main(guard, deps=False)
    sha = git(deps, "rev-parse", "--short=8", "HEAD")
    log = guard.config.get("repo_guard", "log")
    if log:
        target = Path(log)
        target = target if target.is_absolute() else repo / target
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8", newline="") as handle:
            handle.write(f"- {datetime.now(timezone.utc).date().isoformat()}: {branch} {sha} from {main_after[:8]}\n")
    print(f"deps: pushed {branch} {sha}")


def cmd_deps(args: argparse.Namespace) -> None:
    guard = Guard(args.C)
    guard.identity
    refresh_deps(guard)


# --- ci -------------------------------------------------------------------------------------------------------


def github_jobs(run_id: Any, repo: Path) -> tuple[str, list[tuple[str, str]], str]:
    """(run conclusion, [(job, conclusion)], the last lines of the failed steps' logs) through `gh`."""
    gh = shutil.which("gh")
    if not gh:
        return "unknown", [], ""
    view = subprocess.run([gh, "run", "view", str(run_id), "--json", "conclusion,jobs"], cwd=repo,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if view.returncode != 0:
        return "unknown", [], view.stderr.strip()
    import json
    data = json.loads(view.stdout or "{}")
    jobs = [(job.get("name", "?"), job.get("conclusion") or job.get("status", "?")) for job in data.get("jobs", [])]
    tail = ""
    if data.get("conclusion") not in ("success", None):
        failed = subprocess.run([gh, "run", "view", str(run_id), "--log-failed"], cwd=repo, capture_output=True,
                                text=True, encoding="utf-8", errors="replace")
        lines = [re.sub(r"^.*?\d{4}-\d\d-\d\dT[\d:.]+Z ", "", line) for line in failed.stdout.splitlines()]
        tail = "\n".join(lines[-FAILED_TAIL_LINES:])
    return data.get("conclusion") or "unknown", jobs, tail


def cmd_ci(args: argparse.Namespace, host: ReleaseHost | None = None,
           jobs_of: Callable[[Any, Path], tuple[str, list[tuple[str, str]], str]] = github_jobs,
           sleep: Callable[[float], None] = time.sleep) -> int:
    guard = Guard(args.C)
    repo, remote = guard.repo, guard.remote
    cfg = guard.config
    timeout = args.timeout if args.timeout is not None else cfg.get("repo_guard", "ci_timeout", DEFAULT_CI_TIMEOUT)
    find_timeout = (args.find_timeout if args.find_timeout is not None
                    else cfg.get("repo_guard", "ci_find_timeout", DEFAULT_CI_FIND_TIMEOUT))
    if host is None:
        host = ReleaseHost.from_remote(git(repo, "remote", "get-url", remote))
    if host is None:
        raise Refused(f"the {remote} remote is on a host this tool does not know")
    ref = args.ref or guard.main
    tag = f"ci-{args.topic}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    run(["git", "-C", repo, "tag", tag, ref])
    try:
        run(["git", "-C", repo, "push", "-q", remote, tag])
        print(f"pushed {tag}", flush=True)
        os.chdir(repo)  # the host's CLI finds the repository from the working directory
        result = wait_for_run(host, tag, find_timeout, timeout, args.every, sleep)
        run_id = (result.get("run") or {}).get("databaseId")
        print(result.get("message", result.get("status")))
        if run_id is not None:
            conclusion, jobs, tail = jobs_of(run_id, repo)
            print(f"run {run_id}: {conclusion}")
            for name, outcome in jobs:
                print(f"  {name}: {outcome}")
            if tail:
                print(tail)
        return 0 if result.get("status") == "success" else 1
    finally:
        run(["git", "-C", repo, "push", "-q", remote, "--delete", tag], check=False)
        run(["git", "-C", repo, "tag", "-d", tag], check=False, capture=True)


def wait_for_run(host: ReleaseHost, tag: str, find_timeout: int, timeout: int, every: float,
                 sleep: Callable[[float], None]) -> dict[str, Any]:
    """`monitor_ci`, repeated while the host has not yet registered a run for the tag."""
    waited = 0.0
    while True:
        result = host.monitor_ci(tag, timeout=timeout)
        if result.get("status") != "unknown" or "No CI runs" not in str(result.get("message", "")):
            return result
        if waited >= find_timeout:
            return result
        sleep(every)
        waited += every


# --- command line ---------------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="repo-guard", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    here = os.getcwd()
    commit = sub.add_parser("commit", help="commit as the configured identity; refuses agent attribution")
    commit.add_argument("-C", default=here, metavar="DIR", help="the repository (default: the current directory)")
    commit.add_argument("-m", required=True, metavar="MSG")
    commit.add_argument("-a", action="store_true", help="stage every tracked change first")
    commit.add_argument("paths", nargs="*", metavar="PATH")
    push = sub.add_parser("push", help="check, push HEAD to main, fast-forward the checkout, refresh deps when a lock changed")
    push.add_argument("-C", default=here, metavar="DIR")
    push.add_argument("--no-deps", action="store_true", help="do not refresh the deps branch")
    deps = sub.add_parser("deps", help="refresh and push the deps branch, then main")
    deps.add_argument("-C", default=here, metavar="DIR")
    ci = sub.add_parser("ci", help="tag, push, watch the run, print each job, delete the tag")
    ci.add_argument("-C", default=here, metavar="DIR")
    ci.add_argument("--ref", help="what to tag (default: the configured main branch)")
    ci.add_argument("--timeout", type=int, help="seconds to wait for the run (default: [repo_guard] ci_timeout, else %d)" % DEFAULT_CI_TIMEOUT)
    ci.add_argument("--find-timeout", type=int, help="seconds to wait for the host to start the run (default: [repo_guard] ci_find_timeout, else %d)" % DEFAULT_CI_FIND_TIMEOUT)
    ci.add_argument("--every", type=float, default=DEFAULT_EVERY, help="seconds between looks for the run (default %(default)s)")
    ci.add_argument("topic", help="the middle of the tag name: ci-TOPIC-<UTC time>")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "commit":
            cmd_commit(args)
        elif args.command == "push":
            cmd_push(args)
        elif args.command == "deps":
            cmd_deps(args)
        else:
            return cmd_ci(args)
    except Refused as error:
        print(f"refused: {error}", file=sys.stderr)
        return 1
    except at.ConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
