"""A Flutter SDK built from Boriken Dev's mirrors alone: install it, move its pin, prove it builds Canoa.

Usage:
    $AGENTS_PYTHON -I $RELEASE_OVERLAY_ROOT/flutter/flutter_sdk.py install DIR [--rev SHA] [--tree DIR] [--source URL]
    $AGENTS_PYTHON -I $RELEASE_OVERLAY_ROOT/flutter/flutter_sdk.py update [--dry-run] [--old SDK_DIR] [--into DIR] [--tree DIR]
        [--state DIR] [--candidates N]
    $AGENTS_PYTHON -I $RELEASE_OVERLAY_ROOT/flutter/flutter_sdk.py verify DIR CANOA [--tree DIR] [--deps DIR] [--host CANOA]
        [--wrap "CMD ARGS --"] [--only STEPS | --skip STEPS] [--concurrency N] [--logs DIR]
    $AGENTS_PYTHON -I $RELEASE_OVERLAY_ROOT/flutter/flutter_sdk.py image web|linux|windows [--rev SHA] [--arch amd64,arm64] [--push]
        [--tool podman|docker] [--tree DIR] [--work DIR] [--bind ADDR] [--storage-host ADDR] [--squash] [--no-smoke]
        [--emit DIR]

Sources: the framework is github.com/Boriken-Dev/flutter (flutter_mirror.py keeps it scanner-clean); the engine
is the release `engine-<hash>` of the same repository (flutter_artifacts.py publishes it: Google's engine, Dart SDK
and web artifacts byte for byte, with a SHA-256 manifest). Nothing is fetched from storage.googleapis.com or from flutter/flutter (the SDK's own
origin is set as FLUTTER_GIT_URL, where the tool would fetch tags): the
tool starts `flutter_artifacts.py serve` on loopback and sets FLUTTER_STORAGE_BASE_URL to it for every Flutter command.

install DIR: clones the mirror (--source: another URL or a local repository) into DIR as branch `main` at --rev
(default the mirror's `main`), with all tags (Flutter names its version from the newest tag and the commits since it),
serves the engine its bin/internal/engine.version names (fetched into --tree, default
~/.cache/flutter_artifacts/tree, checked against the manifest's hashes), runs `flutter precache` for this host's platforms
(Windows: --windows --web; Linux: --linux --web) and checks that bin/cache/engine.stamp equals engine.version and
that no request missed the served tree. Prints `flutter --version`. DIR must be new or empty.

update: finds the newest upstream flutter/flutter master commit whose engine has every artifact flutter_artifacts.py
lists published on Google's storage (all four Windows and Linux platforms, web, the fonts and the Gradle wrapper),
walking back through the distinct engines of master. --dry-run stops after naming it: the commit, its engine, the
version Flutter will report, whether it is already the mirror's, and the commands it would run. Without it: runs
flutter_mirror.py (mirrors and pushes), `flutter_artifacts.py publish ENGINE --rev MIRROR_SHA`, installs the
candidate beside the old SDK (--old, default FLUTTER_ROOT; into --into, default `<old>-<mirror sha, 10>`), and prints
the pin to write into Canoa: the mirror SHA, the upstream SHA, the version and the engine. The mirror commit of an
upstream commit comes from the commit map flutter_mirror.py keeps in its --state (default ~/.cache/flutter_mirror, or $FLUTTER_MIRROR_STATE).

verify DIR CANOA: runs the SDK in DIR on the Canoa checkout CANOA (a worktree is fine) with only the mirrors
reachable: every Flutter command gets FLUTTER_STORAGE_BASE_URL (the served tree, offline) and a local CONNECT
proxy (HTTPS_PROXY) that refuses Google's hosts (*.googleapis.com, *.gstatic.com) and logs each attempt and each
allowed host. Steps, each with a log in --logs (default `<DIR>-verify-logs`), in order:
  pub      flutter pub get (app/pubspec.lock is restored afterwards)
  analyze  flutter analyze
  host     cargo build -p canoa-host --bin canoa --examples, unless --host names a built canoa; needs --deps or
           CANOA_DEPS for Office.js
  test     flutter test --concurrency N (default 4) with CANOA_TEST_HOST and CANOA_TEST_DART
  web      flutter build web --csp --no-web-resources-cdn as app/tool/build.py pwa runs it (build_noise.py first
           when app/web/noise is missing), then build.py's fallback-font step as `fonts`; lists the Google
           addresses that stay in the build as strings (Flutter's own defaults, not requests)
  desktop  app/tool/build.py desktop, release, this host's platform (CANOA must be a git checkout: build.py restores
           app/pubspec.lock from HEAD; on Linux the Flutter plugins need GTK and the appindicator headers)
--wrap prefixes the cargo, flutter test, web build and desktop commands (for example `python -I build_slot.py --`,
by full path). --only/--skip take a comma list of the step names. The summary gives each step's verdict with
counts, the served tree's request count and misses, and the hosts the proxy allowed and refused. Exit 1 if a step
failed, a request missed the served tree or a Google host was asked for.

image PLATFORM: builds ghcr.io/boriken-dev/flutter-PLATFORM:<mirror sha, 10>-<arch> (and an index tagged with the mirror sha
and with the Flutter version) on gmeligio's recipe (github.com/gmeligio/flutter-docker-image, MIT, commit GM_COMMIT), rebuilt stage
by stage and never taken from their finished image, whose deleted-and-replaced SDK would stay in a lower layer: their toolchain
up to the point where they clone Flutter (Debian by digest, git, clang, cmake, ninja, GTK for linux; Server Core by digest, MinGit
by version and SHA-256, the Visual Studio Build Tools workloads for windows, with the installer's leftovers removed in its layer),
then Boriken-Dev/flutter is cloned at --rev (default the mirror's
main) as branch main with its tags and FLUTTER_GIT_URL names the mirror, and `flutter precache` for the platform fills
bin/cache from `flutter_artifacts.py serve` of the engine's release, started on the host for the build and handed to it as
the build argument FLUTTER_STORAGE_BASE_URL (so the tree is not copied into a layer and the server's log proves every file
came from it; a Windows build needs --bind 0.0.0.0 and --storage-host the host's address as the container sees it). Then
the build checks bin/cache/engine.stamp against engine.version; the tool runs `flutter --version` in the result, with the
storage address set to a dead port, and builds a `flutter create` app (web: build web; linux: build linux; windows: build
windows) the same way. Both linux architectures build the same Debian recipe.
--push
logs in to ghcr.io with `gh auth token` on stdin (needs write:packages) and pushes every tag; if refused it prints the
command to run again. The images carry no Rust: wasm32 and wasm-bindgen-cli stay in the consumer's own stage.

Needs git, Flutter's own prerequisites for the platforms, and, for install of a published pin, network to
github.com (the release assets). Never prints a token. Pass the work to a clean DIR; remove DIR, the tree and the logs
afterwards yourself: the tool deletes nothing.
"""
import argparse
import contextlib
import importlib.util
import json
import os
import platform
import re
import select
import shutil
import shlex
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
TARGET = "Boriken-Dev/flutter"
MIRROR_URL = f"https://github.com/{TARGET}.git"
UPSTREAM_URL = "https://github.com/flutter/flutter.git"
DEFAULT_TREE = Path.home() / ".cache" / "flutter_artifacts" / "tree"
DEFAULT_STATE = Path(os.environ.get("FLUTTER_MIRROR_STATE") or Path.home() / ".cache" / "flutter_mirror")
DENY = (".googleapis.com", ".gstatic.com")
STEPS = ("pub", "analyze", "host", "test", "web", "desktop")


def die(msg):
    sys.exit(f"flutter_sdk: {msg}")


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def host_os():
    if sys.platform == "win32":
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    die(f"{sys.platform}: only Windows and Linux are mirrored")


def host_arch():
    return "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"


def git(*args, cwd=None, check=True):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    if check and r.returncode:
        die(f"git {' '.join(args[:3])} failed: {r.stderr.strip()[-500:]}")
    return r.stdout.strip()


def flutter_exe(sdk):
    return str(Path(sdk) / "bin" / ("flutter.bat" if os.name == "nt" else "flutter"))


def dart_exe(sdk):
    return str(Path(sdk) / "bin" / "cache" / "dart-sdk" / "bin" / ("dart.exe" if os.name == "nt" else "dart"))


# ----------------------------------------------------------- served tree
class Served:
    """`flutter_artifacts.py serve` as a child process: the engine tree on loopback, with its request log."""

    def __init__(self, tree, log, engines=()):
        self.tree, self.log = Path(tree), Path(log)
        cmd = [sys.executable, "-I", str(HERE / "flutter_artifacts.py"), "serve", "--dir", str(self.tree),
               "--log", str(self.log)]
        cmd += list(engines) if engines else ["--offline"]
        self.log.parent.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
        self.url = None
        for line in self.proc.stdout:  # fetch-tree's progress, then the ready line
            if line.startswith("FLUTTER_STORAGE_BASE_URL="):
                self.url = line.strip().split("=", 1)[1]
                break
            print("  " + line.rstrip(), flush=True)
        if not self.url:
            die("flutter_artifacts.py serve ended without a URL")
        threading.Thread(target=lambda: [None for _ in self.proc.stdout], daemon=True).start()

    def requests(self):
        """(total, misses, distinct paths) of the log so far."""
        rows = [l.split("\t") for l in self.log.read_text(encoding="utf-8").splitlines() if l] if self.log.exists() else []
        return len(rows), [r[3] for r in rows if r[0] == "MISS"], len({r[3] for r in rows})

    def close(self):
        self.proc.terminate()
        with contextlib.suppress(Exception):
            self.proc.wait(timeout=10)


# ------------------------------------------------------------ the proxy
class Proxy:
    """A CONNECT proxy that refuses Google's hosts and records every host asked for."""

    def __init__(self):
        self.allowed, self.denied, self.other = {}, {}, 0
        outer = self

        class H(socketserver.BaseRequestHandler):
            def handle(self):
                s = self.request
                try:
                    buf = b""
                    while b"\r\n\r\n" not in buf and len(buf) < 65536:
                        d = s.recv(4096)
                        if not d:
                            return
                        buf += d
                    first = buf.split(b"\r\n", 1)[0].decode("latin-1").split()
                    if len(first) < 2 or first[0].upper() != "CONNECT":
                        outer.other += 1
                        s.sendall(b"HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\n\r\n")
                        return
                    host, _, port = first[1].rpartition(":")
                    host = host.lower()
                    if any(host.endswith(d) or host == d[1:] for d in DENY):
                        outer.denied[host] = outer.denied.get(host, 0) + 1
                        s.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
                        return
                    try:
                        up = socket.create_connection((host, int(port)), timeout=30)
                    except OSError:
                        s.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
                        return
                    outer.allowed[host] = outer.allowed.get(host, 0) + 1
                    s.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    rest = buf.split(b"\r\n\r\n", 1)[1]
                    if rest:
                        up.sendall(rest)
                    socks = [s, up]
                    while True:
                        r, _, _ = select.select(socks, [], [], 120)
                        if not r:
                            break
                        for a in r:
                            d = a.recv(65536)
                            if not d:
                                return
                            (up if a is s else s).sendall(d)
                except OSError:
                    pass
                finally:
                    with contextlib.suppress(Exception):
                        s.close()

        class S(socketserver.ThreadingMixIn, socketserver.TCPServer):
            daemon_threads = True
            allow_reuse_address = True

        self.srv = S(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def sdk_env(sdk, base_url, proxy=None, extra=None):
    """The environment of a Flutter command: this SDK first, the served tree for storage, optionally the proxy."""
    env = dict(os.environ)
    sdk = str(Path(sdk))
    env.update(FLUTTER_ROOT=sdk, FLUTTER_STORAGE_BASE_URL=base_url, FLUTTER_SUPPRESS_ANALYTICS="true", CI="true",
               PATH=os.pathsep.join([str(Path(sdk) / "bin"), str(Path(sdk) / "bin" / "cache" / "dart-sdk" / "bin"),
                                     env.get("PATH", "")]))
    for k in ("FLUTTER_PREBUILT_ENGINE_VERSION", "FLUTTER_ALREADY_LOCKED"):
        env.pop(k, None)
    # On a checkout that is not on stable or beta the tool runs `git fetch $FLUTTER_GIT_URL --tags -f` (default
    # github.com/flutter/flutter) to count commits since the newest tag; the clone's own origin holds the tags that fit.
    origin = git("config", "remote.origin.url", cwd=sdk, check=False)
    if origin:
        env["FLUTTER_GIT_URL"] = origin
    if proxy:
        for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            env[k] = proxy.url
        for k in ("NO_PROXY", "no_proxy"):
            env[k] = "127.0.0.1,localhost"
    env.update(extra or {})
    return env


# ---------------------------------------------------------------- install
def mirror_head(url):
    """(branch, sha) of the source's development branch: `main`, or `master` for a repository that has only that."""
    for branch in ("main", "master"):
        out = git("ls-remote", url, f"refs/heads/{branch}")
        if out:
            return branch, out.split()[0]
    die(f"{url} has neither main nor master")


def precache_flags():
    return ["--windows", "--web"] if host_os() == "windows" else ["--linux", "--web"]


def run_flutter(sdk, args, env, cwd=None, check=True):
    r = subprocess.run([flutter_exe(sdk), *args], cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if check and r.returncode:
        die(f"flutter {' '.join(args[:3])} failed ({r.returncode}):\n{(r.stdout + r.stderr)[-3000:]}")
    return r


def do_install(d, rev, tree, source, log):
    d = Path(d).resolve()
    if d.exists() and any(d.iterdir()):
        die(f"{d} is not empty")
    branch, head = mirror_head(source)
    rev = rev or head
    print(f"clone {source} -> {d} at {rev}", flush=True)
    cfg = ["-c", "core.autocrlf=false", "-c", "core.longpaths=true"]
    git(*cfg, "clone", "-q", "--no-tags", "--branch", branch, "--single-branch", source, str(d))
    for k, v in (("core.autocrlf", "false"), ("core.longpaths", "true")):
        git("config", k, v, cwd=d)
    if git("rev-parse", "HEAD", cwd=d) != rev:
        if git("cat-file", "-t", rev, cwd=d, check=False) != "commit":
            git("fetch", "-q", "origin", rev, cwd=d)
        git("checkout", "-q", "-B", "main", rev, cwd=d)
    git("fetch", "-q", "--no-tags", "origin", "+refs/tags/*:refs/tags/*", cwd=d)
    engine = (d / "bin" / "internal" / "engine.version").read_text(encoding="ascii").strip()
    print(f"engine.version {engine}", flush=True)
    srv = Served(tree, log, [engine])
    try:
        env = sdk_env(d, srv.url)
        print(f"flutter precache {' '.join(precache_flags())}  (storage {srv.url})", flush=True)
        r = run_flutter(d, ["precache", *precache_flags()], env)
        stamp = (d / "bin" / "cache" / "engine.stamp")
        got = stamp.read_text(encoding="ascii").strip() if stamp.exists() else "(no engine.stamp)"
        if got != engine:
            die(f"bin/cache/engine.stamp is {got}, not the release's engine {engine}")
        v = run_flutter(d, ["--version", "--machine"], env)
        info = json.loads(v.stdout[v.stdout.index("{"):v.stdout.rindex("}") + 1])
        total, misses, distinct = srv.requests()
        print(f"flutter {info.get('frameworkVersion')}  channel {info.get('channel')}  engine.stamp {got[:12]}  "
              f"dart {info.get('dartSdkVersion')}")
        print(f"served tree: {total} requests, {distinct} distinct paths, {len(misses)} missed")
        if misses:
            for m in misses[:10]:
                print("  MISS", m)
            die("a request missed the served tree")
        print(f"installed {d}  (mirror {rev})")
        return info
    finally:
        srv.close()


def cmd_install(a):
    log = Path(a.log or Path(a.dir).resolve().with_name(Path(a.dir).name + "-install.log"))
    do_install(a.dir, a.rev, a.tree, a.source, log)
    return 0


# ----------------------------------------------------------------- update
def commit_map(state):
    p = Path(state) / "commit-map.txt"
    m = {}
    if p.exists():
        for line in p.read_text(encoding="ascii").splitlines()[1:]:
            old, new = line.split()
            m[old] = new
    return m


def version_for(state, commit):
    """What Flutter reports for `commit` of master: its tool's rule (version.dart frameworkVersionFor)."""
    tag = git("for-each-ref", "--sort=-v:refname", "--count=1", "--format=%(refname:short)", "refs/tags/[0-9]*.*.*", cwd=state)
    base = git("merge-base", commit, tag, cwd=state)
    n = git("rev-list", "--count", f"{base}..{commit}", cwd=state)
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:-(\d+)\.(\d+)\.pre)?$", tag)
    x, y, z, dev = m.group(1), m.group(2), int(m.group(3)), m.group(4)
    if dev is not None:
        return f"{x}.{y}.0-{int(dev) + 1}.0.pre-{n}", tag
    return f"{x}.{y}.{z + 1}-0.0.pre-{n}", tag


def candidate(state, limit, art, fm):
    """The newest upstream master commit whose engine is fully published on Google's storage."""
    commits = git("rev-list", "--max-count=4000", "refs/heads/master", cwd=state).split()
    emap = fm.engine_map(state, commits)
    seen, tried = {}, 0
    for c in commits:  # newest first
        h = emap.get(c)
        if not h or h in seen:
            continue
        seen[h] = c
        if tried >= limit:
            break
        tried += 1
        fonts = git("show", f"{c}:bin/internal/material_fonts.version", cwd=state)
        gradle = git("show", f"{c}:bin/internal/gradle_wrapper.version", cwd=state)
        paths = art.artifact_paths(h, art.ALL_PLATFORMS, fonts, gradle)
        bad = [p for p in paths if art.head(p)[0] != 200]
        if not bad:
            return c, h, tried
        print(f"  {c[:10]} engine {h[:12]}: {len(bad)} of {len(paths)} not published ({bad[0].rsplit('/', 1)[-1]}...)")
    die(f"no master commit among the newest {limit} engines has published artifacts")


def cmd_update(a):
    fm, art = load("flutter_mirror"), load("flutter_artifacts")
    state = Path(a.state)
    print("fetching upstream master and its tags", flush=True)
    fm.fetch(state, UPSTREAM_URL)
    tip = git("rev-parse", "refs/heads/master", cwd=state)
    c, engine, tried = candidate(state, a.candidates, art, fm)
    version, tag = version_for(state, c)
    mirror_main = mirror_head(MIRROR_URL)[1]
    cur = art.raw_version(TARGET, mirror_main, "engine.version")
    cmap = commit_map(state)
    mirror_sha = cmap.get(c)
    print(f"upstream master tip       {tip}")
    print(f"candidate upstream commit {c}  ({git('log', '-1', '--format=%ci %s', c, cwd=state)[:90]})")
    print(f"engine                    {engine}  (all {len(art.ALL_PLATFORMS)} platforms and web published on Google; {tried} engine(s) tried)")
    print(f"flutter will report       {version}  (newest tag {tag})")
    released = bool(art.read_manifest(TARGET, f"engine-{engine}"))
    print(f"mirror main               {mirror_main}, engine {cur}{'  (the candidate engine)' if cur == engine else ''}")
    print(f"artifacts release         engine-{engine}: {'published' if released else 'not published yet'}")
    print(f"mirror commit of it       {mirror_sha if mirror_sha and set(mirror_sha) != {'0'} else 'not in the last commit map (flutter_mirror.py will make it)'}")
    old = a.old or os.environ.get("FLUTTER_ROOT")
    steps = [f"{sys.executable} -I {HERE / 'flutter_mirror.py'}",
             f"{sys.executable} -I {HERE / 'flutter_artifacts.py'} publish {engine} --rev <mirror sha>",
             f"install beside {old or '<old sdk: pass --old>'}"]
    if a.dry_run:
        print("dry run; would run:")
        for s in steps:
            print("  " + s)
        return 0
    if not old and not a.into:
        die("pass --old (the SDK it goes beside) or --into")
    subprocess.run([sys.executable, "-I", str(HERE / "flutter_mirror.py"), "--state", str(state)], check=True)
    mirror_sha = commit_map(state).get(c)
    if not mirror_sha or set(mirror_sha) == {"0"}:
        die(f"the commit map has no mirror commit for {c}")
    subprocess.run([sys.executable, "-I", str(HERE / "flutter_artifacts.py"), "publish", engine, "--rev", mirror_sha], check=True)
    into = Path(a.into) if a.into else Path(old).with_name(f"{Path(old).name}-{mirror_sha[:10]}")
    info = do_install(into, mirror_sha, a.tree, MIRROR_URL, into.with_name(into.name + "-install.log"))
    print("\nPin for Canoa:")
    print(f"  mirror commit   {mirror_sha}")
    print(f"  upstream commit {c}")
    print(f"  version         {info.get('frameworkVersion')}")
    print(f"  engine          {engine}")
    print(f"  sdk             {into}")
    return 0


# ----------------------------------------------------------------- verify
class Run:
    def __init__(self, logs):
        self.logs, self.rows = Path(logs), []
        self.logs.mkdir(parents=True, exist_ok=True)

    def step(self, name, cmd, cwd, env, parse=None):
        log = self.logs / f"{name}.log"
        t = time.time()
        print(f"[{name}] {' '.join(map(str, cmd))[:200]}", flush=True)
        with open(log, "wb") as f:
            rc = subprocess.run(cmd, cwd=cwd, env=env, stdout=f, stderr=subprocess.STDOUT).returncode
        text = log.read_text(encoding="utf-8", errors="replace")
        detail = parse(text) if parse else ""
        row = (name, rc == 0, f"{time.time() - t:.0f}s", detail, str(log))
        self.rows.append(row)
        print(f"[{name}] {'PASS' if rc == 0 else 'FAIL (' + str(rc) + ')'} {row[2]} {detail}  log {log}", flush=True)
        return rc == 0


def parse_analyze(text):
    m = re.findall(r"(\d+) issues? found|No issues found", text)
    return "no issues" if "No issues found" in text else (f"{m[-1]} issues" if m else "")


def parse_test(text):
    m = re.findall(r"\+(\d+)(?: ~(\d+))?(?: -(\d+))?: (All tests passed!|Some tests failed|[^\n]*)", text)
    if not m:
        return "no test summary"
    p, s, f, msg = m[-1]
    return f"{p} passed, {s or 0} skipped, {f or 0} failed ({msg[:40]})"


def scan_web(dist):
    hits = {}
    for p in Path(dist).rglob("*"):
        if p.is_file() and p.suffix in (".js", ".html", ".json", ".css"):
            t = p.read_bytes()
            for needle in (b"storage.googleapis.com", b"fonts.gstatic.com", b"fonts.googleapis.com", b"www.gstatic.com"):
                if needle in t:
                    hits.setdefault(needle.decode(), []).append(p.name)
    return hits


FINISH_WEB = """import sys
sys.path.insert(0, sys.argv[1])
import build
build.configure_environment()
build.finish_web(build.APP / "build" / "web")
"""


def web_step(run, app, app_root, fl, env, wrap):
    """flutter build web as build.py pwa runs it (--csp, no service worker), then its fallback-font step and a scan."""
    ok = True
    if not (app / "web" / "noise" / "canoa_noise.js").is_file():
        ok &= run.step("noise", [*wrap, sys.executable, str(app / "tool" / "build_noise.py")], app_root, env)
    ok &= run.step("web", [*wrap, fl, "build", "web", "--release", "--csp", "--no-web-resources-cdn", "--no-pub"], app, env)
    finish = run.logs / "finish_web.py"
    finish.write_text(FINISH_WEB, encoding="utf-8", newline="\n")
    ok &= run.step("fonts", [sys.executable, str(finish), str(app / "tool")], app_root, env)
    dist = app / "build" / "web"
    hits = scan_web(dist) if dist.exists() else {"(no build)": []}
    files = sum(1 for p in dist.rglob("*") if p.is_file()) if dist.exists() else 0
    print(f"[web] {files} files in app/build/web; canvaskit {'present' if (dist / 'canvaskit').is_dir() else 'ABSENT'}")
    if hits:
        # Flutter's own scripts carry these as default addresses (canvasKitBaseUrl, the font fallback base); a page
        # fetches none of them with --no-web-resources-cdn and the fallback fonts installed, which the e2e drivers prove.
        print(f"[web] Google addresses as strings in the build (not requests): {hits}")
    return ok and (dist / "canvaskit").is_dir() and (dist / "main.dart.js").is_file()


def cmd_verify(a):
    sdk, app_root = Path(a.dir).resolve(), Path(a.canoa).resolve()
    app = app_root / "app"
    if not (app / "pubspec.yaml").is_file():
        die(f"{app_root} has no app/pubspec.yaml")
    steps = [s for s in STEPS if (not a.only or s in a.only.split(",")) and s not in (a.skip or "").split(",")]
    deps = a.deps or os.environ.get("CANOA_DEPS")
    wrap = shlex.split(a.wrap) if a.wrap else []
    logs = Path(a.logs or sdk.with_name(sdk.name + "-verify-logs"))
    run = Run(logs)
    srv = Served(a.tree, logs / "served.log")
    proxy = Proxy()
    lock = app / "pubspec.lock"
    lock_before = lock.read_bytes() if lock.exists() else None
    print(f"storage {srv.url}, proxy {proxy.url} (refuses {', '.join('*' + d for d in DENY)})", flush=True)
    extra = {"CANOA_DEPS": deps} if deps else {}
    fl = flutter_exe(sdk)
    env = sdk_env(sdk, srv.url, proxy, extra)
    host = Path(a.host).resolve() if a.host else None
    ok = True
    try:
        if "pub" in steps:
            ok &= run.step("pub", [fl, "pub", "get"], app, env)
        if "analyze" in steps:
            ok &= run.step("analyze", [fl, "analyze"], app, env, parse_analyze)
        if "host" in steps or "test" in steps:
            if host is None:
                if not deps:
                    die("the host build needs --deps (a deps checkout) or CANOA_DEPS, or --host")
                target = Path(env.get("CARGO_TARGET_DIR") or app_root / "target")
                henv = dict(env, CARGO_BUILD_JOBS=env.get("CARGO_BUILD_JOBS", "8"))
                if "host" in steps:
                    ok &= run.step("host", [*wrap, shutil.which("cargo") or "cargo", "build", "-p", "canoa-host", "--bin", "canoa", "--examples"],
                                   app_root, henv)
                host = target / "debug" / ("canoa.exe" if os.name == "nt" else "canoa")
        if "test" in steps:
            if not host or not host.exists():
                die(f"no canoa to test with at {host}")
            tenv = dict(env, CANOA_TEST_HOST=str(host), CANOA_TEST_DART=dart_exe(sdk))
            ok &= run.step("test", [*wrap, fl, "test", "--no-pub", "--concurrency", str(a.concurrency)], app, tenv, parse_test)
        if "web" in steps:
            ok &= web_step(run, app, app_root, fl, env, wrap)
        if "desktop" in steps:
            ok &= run.step("desktop", [*wrap, sys.executable, str(app / "tool" / "build.py"), "desktop"], app_root, env)
    finally:
        if lock_before is not None and lock.exists() and lock.read_bytes() != lock_before:
            lock.write_bytes(lock_before)
            print("restored app/pubspec.lock")
        time.sleep(1)
        total, misses, distinct = srv.requests()
        srv.close()
        proxy.close()
    print("\nSummary")
    for name, passed, secs, detail, _ in run.rows:
        print(f"  {name:8} {'PASS' if passed else 'FAIL'}  {secs:>6}  {detail}")
    print(f"served tree: {total} requests, {distinct} distinct paths, {len(misses)} missed")
    print(f"proxy allowed: {dict(sorted(proxy.allowed.items()))}")
    print(f"proxy refused (requests that still went to Google): {dict(sorted(proxy.denied.items())) or 'none'}")
    if proxy.other:
        print(f"proxy saw {proxy.other} non-CONNECT request(s)")
    clean = all(r[1] for r in run.rows) and ok and not misses and not proxy.denied
    print("verify: " + ("PASS" if clean else "FAIL"))
    return 0 if clean else 1


# ------------------------------------------------------------------ image
GM_COMMIT = "05eeacdf86d0e7376f86106e61718d67cc2a922c"  # gmeligio/flutter-docker-image: android.Dockerfile and windows.Dockerfile as read
REGISTRY = "ghcr.io/boriken-dev"
# What each platform enables, and the architectures it is built for. The toolchain under the Flutter SDK is gmeligio's
# recipe repeated stage by stage (never their finished image): a layer holding a Flutter SDK that a later layer deletes
# keeps its bytes in the image.
BASES = {
    "web": dict(archs=("amd64", "arm64"), flag="--web", config="--enable-web", off="--no-enable-linux-desktop", build="web"),
    "linux": dict(archs=("amd64", "arm64"), flag="--linux", config="--enable-linux-desktop", off="--no-enable-web", build="linux"),
    "windows": dict(archs=("amd64",), flag="--windows", config="--enable-windows-desktop", off="", build="windows"),
}
DEBIAN = "debian:13.7-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2"
SERVERCORE = "mcr.microsoft.com/windows/servercore:ltsc2025@sha256:a43c2d41a3d6e940dec865731e2f5db5c3e03f705f5300ef0b5817d3e01ecfb1"
LINUX_PACKAGES = {"web": [], "linux": ["clang", "cmake", "ninja-build", "pkg-config", "libgtk-3-dev"]}
ENTRYPOINT_SH = """#!/usr/bin/env bash

analytic_tools_str="Dart, Flutter and Fastlane"

if [ "$ENABLE_ANALYTICS" = "true" ]; then
    echo -e "Received 'ENABLE_ANALYTICS=true'.\\nEnabling analytics for $analytic_tools_str."

    dart --enable-analytics
    flutter config --analytics
    unset FASTLANE_OPT_OUT_USAGE
fi

exec "$@"
"""
ENTRYPOINT_PS1 = """$analytic_tools_str = "Dart, Flutter and Fastlane"

if ($env:ENABLE_ANALYTICS -eq "true") {
    Write-Output "Received 'ENABLE_ANALYTICS=true'.`nEnabling analytics for $analytic_tools_str."

    dart --enable-analytics
    flutter config --analytics

    if (Test-Path env:FASTLANE_OPT_OUT_USAGE) {
        Remove-Item env:FASTLANE_OPT_OUT_USAGE
    }
}
else {
    dart --disable-analytics
    flutter --disable-analytics
    $env:POWERSHELL_TELEMETRY_OPTOUT = 1
    $env:FASTLANE_OPT_OUT_USAGE = "YES"
}

if ($args.length -gt 0) {
    Invoke-Expression "$args"
}
else {
    powershell
}
"""

# Tokens are written @@NAME@@ so that the shell's and PowerShell's own braces stay as they are.
UNIX_RECIPE = """# The toolchain (the Debian base by digest, the packages, the flutter user, the entrypoint) is the `flutter` stage and
# the `web` and `linux` stages of gmeligio/flutter-docker-image's android.Dockerfile at @@GM@@,
# up to the point where it clones Flutter (MIT, Copyright gmeligio; https://github.com/gmeligio/flutter-docker-image).
# Not copied: their Flutter SDK, the Android, Ruby and Fastlane stages, and the sample app they build into a layer.
# Their exact apt version pins are not repeated: the archive no longer serves two of them (ca-certificates 20250419,
# ninja-build 1.12.1-1), so the packages are the same names at the digest's archive.
FROM @@DEBIAN@@
SHELL ["/bin/bash", "-euxo", "pipefail", "-c"]
ENV LANG=C.UTF-8
USER root
RUN apt-get update \\
    && apt-get install -y --no-install-recommends @@PACKAGES@@ \\
    && rm -rf /var/lib/apt/lists/*
ENV HOME=/home/flutter
RUN groupadd --gid 1001 flutter \\
    && useradd --create-home --shell /bin/bash --uid 1001 --gid flutter flutter
USER flutter:flutter
WORKDIR /home/flutter
ENV SDK_ROOT=/home/flutter/sdks
ENV FLUTTER_ROOT=/home/flutter/sdks/flutter
ENV PATH="$PATH:/home/flutter/sdks/flutter/bin:/home/flutter/sdks/flutter/bin/cache/dart-sdk/bin"
COPY --chown=flutter:flutter docker_entrypoint.sh /home/flutter/docker_entrypoint.sh
RUN chmod +x /home/flutter/docker_entrypoint.sh
ENTRYPOINT [ "/home/flutter/docker_entrypoint.sh" ]

ARG MIRROR_URL
ARG MIRROR_REV
ARG IMAGE_DESCRIPTION
ARG FLUTTER_STORAGE_BASE_URL
LABEL org.opencontainers.image.source="https://github.com/Boriken-Dev/flutter" \\
      org.opencontainers.image.description="${IMAGE_DESCRIPTION}" \\
      org.opencontainers.image.revision="${MIRROR_REV}" \\
      org.opencontainers.image.base.name="@@DEBIAN_NAME@@"
ENV FLUTTER_GIT_URL="$MIRROR_URL"
# Flutter, ours: the framework comes from the mirror at the pin with every tag (Flutter names its version from the newest
# tag and the commits since it); the engine and the Dart SDK from the artifact release, served at FLUTTER_STORAGE_BASE_URL
# for this build only.
RUN git -c core.autocrlf=false clone --quiet --filter=blob:none --no-tags --branch main --single-branch "$MIRROR_URL" "$FLUTTER_ROOT" \\
    && git -C "$FLUTTER_ROOT" config core.autocrlf false \\
    && git -C "$FLUTTER_ROOT" checkout --quiet -B main "$MIRROR_REV" \\
    && git -C "$FLUTTER_ROOT" fetch --quiet --no-tags origin '+refs/tags/*:refs/tags/*'
RUN flutter --version \\
    && dart --disable-analytics \\
    && flutter config --no-cli-animations --no-analytics --no-enable-android --no-enable-fuchsia --no-enable-custom-devices \\
         --no-enable-ios --no-enable-macos-desktop @@OFF@@ @@CONFIG@@ \\
    && flutter precache @@FLAG@@ \\
    && test "$(cat "$FLUTTER_ROOT/bin/cache/engine.stamp")" = "$(cat "$FLUTTER_ROOT/bin/internal/engine.version")" \\
    && flutter --version
"""

# MinGit's SHA-256 is the digest GitHub lists for the release asset (gmeligio downloads it unchecked). The Visual Studio
# Build Tools installer is the channel's current one, as in their file: config/version.json there names the versions
# their image tests expect, and the build prints the ones installed.
WIN_RECIPE = r"""# escape=`
# The toolchain (Server Core by digest, MinGit, the user paths, the Visual Studio Build Tools workloads) is gmeligio/flutter-docker-image's
# windows.Dockerfile at @@GM@@ up to the point where it clones Flutter (MIT, Copyright gmeligio;
# https://github.com/gmeligio/flutter-docker-image). Not copied: their Flutter SDK, the sample app they build into a layer,
# and their test stage. The installer's logs, the installer, TEMP and the package cache are removed in the layer that makes them.
FROM @@SERVERCORE@@
SHELL ["powershell", "-Command", "$ErrorActionPreference = 'Stop'; $ProgressPreference = 'SilentlyContinue';"]
ARG git_version=2.56.1
ARG git_sha256=baa40c1b8d5897826f47c2b819156553b89833bc52cb42cdfca7d1f0faeed34a
ARG git_installation_path="C:\Program Files\Git"
ARG vs_win11sdk_build=22621
ENV USERPROFILE="@@HOME@@"
ENV SDK_ROOT="${USERPROFILE}\sdks"
ENV FLUTTER_ROOT="${SDK_ROOT}\flutter"
WORKDIR "$USERPROFILE"
RUN $installer = \"MinGit-${env:git_version}-busybox-64-bit.zip\"; `
    $url = \"https://github.com/git-for-windows/git/releases/download/v${env:git_version}.windows.1/${installer}\"; `
    Invoke-WebRequest -Uri $url -OutFile $installer; `
    if ((Get-FileHash $installer -Algorithm SHA256).Hash -ne $env:git_sha256) { throw \"$installer does not match its SHA-256\" }; `
    Expand-Archive -Path $installer -DestinationPath $env:git_installation_path; `
    Remove-Item -Path $installer
USER ContainerAdministrator
RUN [Environment]::SetEnvironmentVariable('PATH', \"${env:PATH};${env:git_installation_path}\cmd;${env:git_installation_path}\usr\bin;${env:FLUTTER_ROOT}\bin;${env:FLUTTER_ROOT}\bin\cache\dart-sdk\bin;C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\msbuild\current\bin\", 'Machine')
RUN $env:GIT_CONFIG_NOSYSTEM=1; git config --system --unset-all include.path
RUN Invoke-WebRequest -Uri https://aka.ms/vs/17/release/vs_buildtools.exe -OutFile vs_BuildTools.exe; `
    $p = Start-Process vs_BuildTools.exe -ArgumentList \"--quiet --wait --norestart --nocache `
    --add Microsoft.VisualStudio.Component.VC.CMake.Project `
    --add Microsoft.VisualStudio.Component.Windows11SDK.${env:vs_win11sdk_build} `
    --add Microsoft.VisualStudio.Workload.VCTools\" `
    -Wait -PassThru; `
    if ($p.ExitCode -ne 0 -and $p.ExitCode -ne 3010) { `
      Write-Host \"vs_buildtools.exe failed with exit code $($p.ExitCode)\"; `
      Get-Content -Path \"$env:TEMP\dd_*.log\" -ErrorAction SilentlyContinue; `
      exit $p.ExitCode `
    }; `
    Remove-Item vs_BuildTools.exe; `
    Get-ChildItem (Join-Path $env:ProgramData 'Microsoft\VisualStudio\Packages') -Directory | `
      Where-Object { $_.Name -match 'VC.CMake.Project|Windows11SDK|Workload.VCTools' } | ForEach-Object { Write-Host $_.Name }; `
    foreach ($d in @($env:TEMP, 'C:\Windows\Temp', (Join-Path $env:ProgramData 'Package Cache'))) { `
      $s = (Get-ChildItem $d -Recurse -Force -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum; `
      Write-Host ('removing {0,14:N0} bytes from {1}' -f $s, $d); `
      Get-ChildItem $d -Force -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue `
    }
# Server Core ships a stale root store and Dart's TLS fails against pub.dev without the current roots.
RUN certutil -generateSSTFromWU C:\roots.sst; if ($LASTEXITCODE -ne 0) { exit 1 }; `
    Import-Certificate -FilePath C:\roots.sst -CertStoreLocation Cert:\LocalMachine\Root | Out-Null; `
    Remove-Item C:\roots.sst
USER ContainerUser
ARG MIRROR_URL
ARG MIRROR_REV
ARG IMAGE_DESCRIPTION
ARG FLUTTER_STORAGE_BASE_URL
LABEL org.opencontainers.image.source="https://github.com/Boriken-Dev/flutter" `
      org.opencontainers.image.description="${IMAGE_DESCRIPTION}" `
      org.opencontainers.image.revision="${MIRROR_REV}" `
      org.opencontainers.image.base.name="@@SERVERCORE_NAME@@"
ENV FLUTTER_GIT_URL="${MIRROR_URL}"
RUN git -c core.autocrlf=false -c core.longpaths=true clone --quiet --filter=blob:none --no-tags --branch main --single-branch $env:MIRROR_URL $env:FLUTTER_ROOT; `
    if ($LASTEXITCODE -ne 0) { exit 1 }; `
    git config --global --add safe.directory '*'; `
    git -C $env:FLUTTER_ROOT config core.autocrlf false; `
    git -C $env:FLUTTER_ROOT config core.longpaths true; `
    git -C $env:FLUTTER_ROOT checkout --quiet -B main $env:MIRROR_REV; `
    if ($LASTEXITCODE -ne 0) { exit 1 }; `
    git -C $env:FLUTTER_ROOT fetch --quiet --no-tags origin '+refs/tags/*:refs/tags/*'; `
    if ($LASTEXITCODE -ne 0) { exit 1 }
RUN flutter --version; if ($LASTEXITCODE -ne 0) { exit 1 }; `
    dart --disable-analytics; `
    flutter config --no-cli-animations --no-analytics --no-enable-android --no-enable-web --no-enable-linux-desktop `
      --no-enable-fuchsia --no-enable-custom-devices --no-enable-ios --no-enable-macos-desktop @@CONFIG@@; `
    if ($LASTEXITCODE -ne 0) { exit 1 }; `
    flutter precache @@FLAG@@; if ($LASTEXITCODE -ne 0) { exit 1 }; `
    $stamp = (Get-Content -Raw \"$env:FLUTTER_ROOT\bin\cache\engine.stamp\").Trim(); `
    $want = (Get-Content -Raw \"$env:FLUTTER_ROOT\bin\internal\engine.version\").Trim(); `
    if ($stamp -ne $want) { Write-Host \"engine.stamp $stamp is not $want\"; exit 1 }; `
    flutter --version; if ($LASTEXITCODE -ne 0) { exit 1 }
COPY docker_entrypoint.ps1 "docker_entrypoint.ps1"
ENTRYPOINT "@@HOME@@\docker_entrypoint.ps1"
"""

# A plain `flutter create` app; the storage address the smoke run is given is a dead port, so a fetch of an engine
# file would fail it.
SMOKE_UNIX = ("cd /tmp && flutter create smoke_app >/dev/null && cd smoke_app && flutter build {build} 2>&1 | tail -n 8"
              " && ls build/{build}")
SMOKE_WIN = ("Set-Location $env:USERPROFILE; flutter create smoke_app | Out-Null; Set-Location smoke_app; "
             "flutter build windows 2>&1 | Select-Object -Last 8; if ($LASTEXITCODE -ne 0) { exit 1 }")


def containerfile(platform):
    """(Containerfile text, the files it COPYs) for one platform: gmeligio's toolchain stages repeated, then Flutter from the mirror."""
    b = BASES[platform]
    fields = {"HOME": "C:\\Users\\ContainerUser", "GM": GM_COMMIT, "FLAG": b["flag"], "CONFIG": b["config"], "OFF": b["off"],
              "DEBIAN": DEBIAN, "DEBIAN_NAME": DEBIAN, "SERVERCORE": SERVERCORE, "SERVERCORE_NAME": SERVERCORE,
              "PACKAGES": " ".join(["curl", "git", "lcov", "ca-certificates", "unzip"] + LINUX_PACKAGES.get(platform, []))}
    text = WIN_RECIPE if platform == "windows" else UNIX_RECIPE
    for k, v in fields.items():
        text = text.replace(f"@@{k}@@", v)
    return text, ({"docker_entrypoint.ps1": ENTRYPOINT_PS1} if platform == "windows" else {"docker_entrypoint.sh": ENTRYPOINT_SH})


def which_tool(want):
    for t in ([want] if want else ["podman", "docker"]):
        if shutil.which(t):
            return t
    die("neither podman nor docker is on PATH")


def sh(cmd, check=True, **kw):
    print("+ " + " ".join(map(str, cmd))[:300], flush=True)
    r = subprocess.run(cmd, **kw)
    if check and r.returncode:
        die(f"{cmd[0]} {cmd[1]} failed ({r.returncode})")
    return r


def full_rev(rev):
    if rev is None:
        return mirror_head(MIRROR_URL)[1]
    if len(rev) == 40:
        return rev
    out = subprocess.run(["gh", "api", f"repos/{TARGET}/commits/{rev}", "--jq", ".sha"], capture_output=True, text=True)
    return out.stdout.strip() or die(f"cannot resolve {rev} in {TARGET}")


def emit_containerfile(platform, out):
    """Write the platform's Containerfile and the files it copies as the Boriken-Dev/flutter repository carries
    them; the revision and the description are build arguments (MIRROR_REV, IMAGE_DESCRIPTION)."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    text, files = containerfile(platform)
    for fn, body in files.items():
        (out / fn).write_text(body, encoding="utf-8", newline="\n")
    (out / f"Containerfile.{platform}").write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {out / ('Containerfile.' + platform)}")


def cmd_image(a):
    if a.emit:
        emit_containerfile(a.platform, a.emit)
        return 0
    art = load("flutter_artifacts")
    b = BASES[a.platform]
    tool = which_tool(a.tool)
    if a.platform == "windows" and tool == "podman":
        die("a Windows image builds with docker on a Windows x64 host")
    rev = full_rev(a.rev)
    short = rev[:10]
    engine = art.raw_version(TARGET, rev, "engine.version")
    if not art.read_manifest(TARGET, f"engine-{engine}"):
        die(f"release engine-{engine} of {TARGET} has no manifest: flutter_artifacts.py publish {engine} --rev {rev}")
    archs = a.arch.split(",") if a.arch else list(b["archs"])
    for x in archs:
        if x not in b["archs"]:
            die(f"{a.platform} is built for {', '.join(b['archs'])}, not {x}")
    name = f"{REGISTRY}/flutter-{a.platform}"
    base_name = SERVERCORE if a.platform == "windows" else DEBIAN
    desc = (f"Flutter SDK from Boriken-Dev/flutter at {short}, engine {engine[:12]} from its release engine-{engine[:12]}, "
            f"on {base_name.split('@')[0]}")
    work = Path(a.work or (Path.home() / ".cache" / "flutter_image" / a.platform)).resolve()
    work.mkdir(parents=True, exist_ok=True)
    log = work / "served.log"
    log.unlink(missing_ok=True)
    print(f"{a.platform}: mirror {rev} engine {engine}\n  base {base_name}\n  architectures {', '.join(archs)}", flush=True)
    results = []
    cmd = [sys.executable, "-I", str(HERE / "flutter_artifacts.py"), "serve", "--dir", str(a.tree), "--log", str(log),
           "--bind", a.bind, engine]
    srv = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
    try:
        url = None
        for line in srv.stdout:
            if line.startswith("FLUTTER_STORAGE_BASE_URL="):
                url = line.strip().split("=", 1)[1]
                break
            print("  " + line.rstrip(), flush=True)
        if not url:
            die("flutter_artifacts.py serve ended without a URL")
        threading.Thread(target=lambda: [None for _ in srv.stdout], daemon=True).start()
        storage = f"http://{a.storage_host}:{url.rsplit(':', 1)[1]}"
        print(f"storage {storage} (the tree {a.tree}, listening on {a.bind})", flush=True)
        smoke_env = ["-e", "FLUTTER_STORAGE_BASE_URL=http://127.0.0.1:9"]
        for arch in archs:
            cf, files = containerfile(a.platform)
            ctx = work / arch
            if ctx.exists():
                shutil.rmtree(ctx)
            ctx.mkdir(parents=True)
            for fn, text in files.items():
                (ctx / fn).write_text(text, encoding="utf-8", newline="\n")
            cfp = ctx / "Containerfile"
            cfp.write_text(cf.replace("\n", "\r\n") if a.platform == "windows" else cf, encoding="utf-8", newline="")
            tag = f"{name}:{short}-{arch}"
            build = [tool, "build", "-f", str(cfp), "-t", tag, "--build-arg", f"MIRROR_URL={MIRROR_URL}",
                     "--build-arg", f"MIRROR_REV={rev}", "--build-arg", f"FLUTTER_STORAGE_BASE_URL={storage}",
                     "--build-arg", f"IMAGE_DESCRIPTION={desc}"]
            if tool == "podman":
                build += ["--platform", f"linux/{arch}", "--network=host"]
                if a.squash:
                    build += ["--squash-all"]
            build.append(str(ctx))
            t0 = time.time()
            sh(build)
            secs = time.time() - t0
            plat = ["--platform", f"linux/{arch}", "--network=host"] if tool == "podman" else []
            ver = subprocess.run([tool, "run", "--rm", *plat, *smoke_env, tag] +
                                 (["flutter", "--version", "--machine"] if a.platform != "windows" else ["flutter --version --machine"]),
                                 capture_output=True, text=True, encoding="utf-8", errors="replace")
            try:
                info = json.loads(ver.stdout[ver.stdout.index("{"):ver.stdout.rindex("}") + 1])
            except ValueError:
                die(f"flutter --version --machine in {tag}:\n{ver.stdout[-800:]}{ver.stderr[-800:]}")
            size = subprocess.run([tool, "image", "inspect", tag, "--format", "{{.Size}}"], capture_output=True, text=True).stdout.strip()
            results.append((arch, tag, info.get("frameworkVersion"), size, secs))
            print(f"built {tag}: flutter {info.get('frameworkVersion')} engine {info.get('engineContentHash')} "
                  f"dart {info.get('dartSdkVersion')} size {size} bytes, build {secs:.0f}s", flush=True)
            if info.get("engineContentHash") != engine:
                die(f"the image reports engine {info.get('engineContentHash')}, not {engine}")
            if a.smoke:
                script = SMOKE_WIN if a.platform == "windows" else SMOKE_UNIX.format(build=b["build"])
                t0 = time.time()
                r = sh([tool, "run", "--rm", *plat, *smoke_env, tag] + (["bash", "-c", script] if a.platform != "windows" else [script]),
                       check=False)
                print(f"smoke {tag}: {'PASS' if r.returncode == 0 else 'FAIL'} in {time.time() - t0:.0f}s", flush=True)
                if r.returncode:
                    die("the sample app did not build in the image")
        rows = [l.split("\t") for l in log.read_text(encoding="utf-8").splitlines() if l] if log.exists() else []
        misses = [r[3] for r in rows if r[0] == "MISS"]
        print(f"served tree: {len(rows)} requests, {len({r[3] for r in rows})} distinct paths, {len(misses)} missed")
        for m in misses[:10]:
            print("  MISS", m)
        if misses:
            die("a request missed the served tree")
    finally:
        srv.terminate()
    version = results[0][2]
    names = [f"{name}:{short}", f"{name}:{version}"]
    index = tool == "podman" and len(results) > 1
    for n in names:
        if index:
            sh([tool, "manifest", "rm", n], check=False, capture_output=True)
            sh([tool, "manifest", "create", n])
            for _, tag, *_ in results:
                sh([tool, "manifest", "add", n, f"containers-storage:{tag}"])
        else:
            sh([tool, "tag", results[0][1], n])
    if not a.push:
        print("not pushed (no --push); tagged " + ", ".join(names))
        return 0
    token = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True).stdout.strip()
    user = subprocess.run(["gh", "api", "user", "--jq", ".login"], capture_output=True, text=True).stdout.strip()
    if not user:
        die("pushing needs a GitHub login: gh auth login")
    r = subprocess.run([tool, "login", "ghcr.io", "-u", user, "--password-stdin"], input=token, text=True, capture_output=True)
    if r.returncode:
        die(f"login to ghcr.io failed: {(r.stderr or r.stdout).strip()[-300:]}")
    for n in ([r_[1] for r_ in results] if index else []) + names:
        cmd = ([tool, "manifest", "push", "--all", n, f"docker://{n}"] if index and n in names else [tool, "push", n])
        rc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if rc.returncode:
            print(f"push {n} refused: {(rc.stdout + rc.stderr).strip()[-400:]}")
            print(f"with write:packages on the token (gh auth refresh -h github.com -s write:packages), run again:\n"
                  f"  {sys.executable} -I {Path(__file__).resolve()} image {a.platform} --rev {rev} --push")
            return 2
        print(f"pushed {n}")
    return 0


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("install")
    p.add_argument("dir")
    p.add_argument("--rev", help="mirror commit (default: the mirror's main)")
    p.add_argument("--tree", default=str(DEFAULT_TREE))
    p.add_argument("--source", default=MIRROR_URL, help="the framework repository (default the mirror)")
    p.add_argument("--log", help="the served tree's request log")
    p = sub.add_parser("update")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--old", help="the SDK to install beside (default FLUTTER_ROOT)")
    p.add_argument("--into")
    p.add_argument("--tree", default=str(DEFAULT_TREE))
    p.add_argument("--state", default=str(DEFAULT_STATE))
    p.add_argument("--candidates", type=int, default=40, help="distinct engines to try, newest first")
    p = sub.add_parser("verify")
    p.add_argument("dir")
    p.add_argument("canoa")
    p.add_argument("--tree", default=str(DEFAULT_TREE))
    p.add_argument("--deps")
    p.add_argument("--host", help="a built canoa (CANOA_TEST_HOST); else the host step builds one in CANOA/target")
    p.add_argument("--wrap", help="a prefix for the cargo and flutter test and desktop commands")
    p.add_argument("--only")
    p.add_argument("--skip")
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--logs")
    p = sub.add_parser("image")
    p.add_argument("platform", choices=sorted(BASES))
    p.add_argument("--rev", help="mirror commit (default: the mirror's main)")
    p.add_argument("--arch", help="comma list of amd64, arm64 (default: every one the platform has)")
    p.add_argument("--push", action="store_true")
    p.add_argument("--tool", choices=("podman", "docker"))
    p.add_argument("--tree", default=str(DEFAULT_TREE))
    p.add_argument("--work", help="build folders and the request log (default ~/.cache/flutter_image/PLATFORM)")
    p.add_argument("--bind", default="127.0.0.1", help="where the artifact server listens (a Windows container needs the host's address)")
    p.add_argument("--storage-host", default="127.0.0.1", help="the host's address as the build reaches it")
    p.add_argument("--squash", action="store_true", help="podman: flatten the image, so the base's deleted SDK takes no space")
    p.add_argument("--emit", metavar="DIR", help="write only the platform's Containerfile (build arguments for the base, description and revision) into DIR")
    p.add_argument("--no-smoke", dest="smoke", action="store_false", help="skip the sample app's build in the image")
    a = ap.parse_args(argv)
    return {"install": cmd_install, "update": cmd_update, "verify": cmd_verify, "image": cmd_image}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
