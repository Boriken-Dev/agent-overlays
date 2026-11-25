"""Keep a scanner-clean mirror of Flutter's stable and master branches (and main, master's alias).

Usage:
    $AGENTS_PYTHON -I $RELEASE_OVERLAY_ROOT/flutter/flutter_mirror.py [--no-push] [--rewrite]
        [--keep-work] [--state DIR] [--upstream URL] [--target OWNER/REPO]

What it does, each run (about an hour the first time, minutes after, because the
blob verdicts are cached):
  1. fetches `stable`, `master` (upstream's development branch) and every tag
     reachable from them into the bare clone STATE (default
     ~/.cache/flutter_mirror, or $FLUTTER_MIRROR_STATE; no other branch). Both branches are scanned and
     rewritten together, so a shared blob is removed consistently. The tags are
     those that stable or master reach, plus the release/dev/beta tags
     (`X.Y.Z`, `X.Y.Z-M.N.pre`) that version-sort above all of those. Those sit on a
     release branch forked from master, and Flutter's tool names a master checkout from
     the newest such tag and the commits since its merge base (a clone of master says
     `3.51.0-1.0.pre-291`, not the old tag's `3.33.0-1.0.pre-8660`). Such a tag is
     placed on the master commit where its branch left master (its merge base), so the
     tool counts the same commits without the branch's commits, whose presence would
     change how the rewrite numbers stable's;
  2. pass 1: streams every blob reachable from those refs through
     blob_scan.classify (first 64 KiB plus the size; a text-looking blob is read in
     full for the private-key check) and writes the ids of every blob it flags or notes
     (anything that is neither text nor an image: test data, wasm, pdf) to
     STATE/strip-ids.txt, with the verdicts cached in STATE/classified.tsv;
  3. pass 2: clones STATE to a work directory (its upstream-to-mirror commit map is kept as
     STATE/commit-map.txt) and runs
     `git filter-repo --strip-blobs-with-ids` there. The rewrite is deterministic:
     the same upstream commits and the same ids give the same SHAs, so a rerun on
     new upstream commits leaves the rewritten ones unchanged. The same pass (a
     --commit-callback) writes bin/internal/engine.version into every commit that
     has engine/ but lacks the file and is on master only (rewriting an ancestor of
     stable would move stable and its tags): the hash the tool would compute from
     that commit's original DEPS and engine tree ids. The tool prefers a tracked
     file, so master resolves to the engine upstream published with no
     FLUTTER_PREBUILT_ENGINE_VERSION (stripping blobs under engine/ would
     otherwise change that hash);
  4. verifies: scans every blob of the result with classify (it must pass), lists
     the NOTE kinds, checks bin/internal/engine.version is the same blob before
     and after on stable and every tag, and that master's is upstream's hash;
  5. pushes `stable`, `master`, `main` (upstream keeps it equal to master; here it names the
     rewritten master), the tags and an orphan branch `mirror-notes` (MIRROR.md only:
     the removed blob ids, paths, kinds and the upstream commit) to the public
     repository TARGET (default Boriken-Dev/flutter, created if missing), and
     prints which refs were created, moved or left alone. `stable`, the default branch, is pushed as
     the rewritten upstream head plus one commit that adds DISPATCH_PATH (flutter_images_dispatch.yml
     of this folder), the workflow that starts the container-image build kept on the orphan branch
     `images` (GitHub starts a manual run only from the default branch). That commit is not an
     upstream commit and is not in the commit map; it is deterministic (fixed dates, parent = the
     rewritten head) and is not made again when the head's tree already holds the identical file.
     `main`, `master`, the tags and `mirror-notes` carry no such commit. `images` is never touched.
     `--dispatcher-only` adds the commit to the target's `stable` as it is, without a rewrite.

A run whose upstream refs equal the last pushed ones does nothing unless --rewrite
is given (it reruns both passes and pushes only what differs, which shows the
rewrite is deterministic). --no-push stops after the verification. --keep-work
keeps the rewritten clone (STATE.work) instead of deleting it. A flagged-blob
verdict is sticky: once flagged, an id stays flagged, so the result does not drift.
Needs git, gh (logged in) and network. Never prints a token.
"""
import argparse
import codecs
import collections
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
FILTER_REPO_URL = "https://raw.githubusercontent.com/newren/git-filter-repo/v2.47.0/git-filter-repo"
FILTER_REPO_SHA256 = "67447413e273fc76809289111748870b6f6072f08b17efe94863a92d810b7d94"
HEAD = 65536
# stable with its tags, and upstream's development branch (master; main only if upstream renames it).
BRANCHES = ["refs/heads/stable", "refs/heads/master"]
ENGINE_FILE = "bin/internal/engine.version"
DISPATCH_PATH = ".github/workflows/flutter-images.yml"
DISPATCH_SRC = HERE / "flutter_images_dispatch.yml"


def commit_identity():
    """`-c` options for the commits the mirror makes: the machine's own git identity, which its SHAs depend on."""
    out = []
    for key in ("user.name", "user.email"):
        value = subprocess.run(["git", "config", "--get", key], capture_output=True, text=True).stdout.strip()
        if not value:
            sys.exit(f"flutter_mirror: git config {key} is not set; the mirror's commits are made under it")
        out += ["-c", f"{key}={value}"]
    return out



def load_scan():
    # blob_scan.py is the engineering overlay's `blob-scan` skill.
    engineering = Path(os.environ.get("ENGINEERING_OVERLAY_ROOT") or HERE.parent / "engineering")
    spec = importlib.util.spec_from_file_location("blob_scan", engineering / "skills" / "blob-scan" / "blob_scan.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


scan = load_scan()


def run(args, cwd=None, check=True, data=None, env=None):
    r = subprocess.run(args, cwd=cwd, input=data, capture_output=True, env=env)
    if check and r.returncode:
        sys.exit(f"failed ({r.returncode}): {' '.join(map(str, args[:6]))}\n"
                 + r.stderr.decode("utf-8", "replace")[-2000:])
    return r


def git(repo, *args, data=None, check=True):
    return run(["git", "-C", str(repo), *args], data=data, check=check).stdout


def gitt(repo, *args):
    return git(repo, *args).decode("utf-8", "replace")


# ---------------------------------------------------------------- 1. fetch
TAG_VERSION = re.compile(r"^refs/tags/(\d+)\.(\d+)\.(\d+)(?:-(\d+)\.(\d+)\.pre)?$")


def version_key(ref):
    """Order of a release or dev/beta tag (`3.51.0` < `3.51.0-0.2.pre`, as git's version sort has it), else None."""
    m = TAG_VERSION.match(ref)
    return tuple(int(g) for g in m.groups() if g is not None) if m else None


def fetch(state, upstream):
    if not (state / "HEAD").exists():
        state.mkdir(parents=True, exist_ok=True)
        run(["git", "init", "--bare", "-q", str(state)])
        git(state, "remote", "add", "upstream", upstream)
    git(state, "config", "remote.upstream.url", upstream)
    git(state, "config", "gc.auto", "0")
    # `stable` is the one branch; its tags are chosen below from what it reaches.
    git(state, "fetch", "-q", "--no-tags", "upstream", *[f"+{b}:{b}" for b in BRANCHES])
    stable = gitt(state, "rev-parse", BRANCHES[0]).strip()
    reach = set(gitt(state, "rev-list", *BRANCHES).split())
    tags, peeled = {}, {}
    for line in gitt(state, "ls-remote", "--tags", "upstream").splitlines():
        sha, ref = line.split("\t")
        if ref.endswith("^{}"):
            peeled[ref[:-3]] = sha
        else:
            tags[ref] = sha
    keep = {r: s for r, s in tags.items() if peeled.get(r, s) in reach}
    # Flutter's tool takes the newest `X.Y.Z[-M.N.pre]` tag by version order, reachable or not, and counts
    # the commits since its merge base with HEAD. The newest dev/beta tags sit on a release branch forked
    # from master, in commits that neither branch reaches. Carrying those commits would change how the
    # rewrite orders (and so numbers) stable's own commits, so each such tag is placed on the commit where
    # its branch left master, which gives the tool the same merge base and the same count.
    newest = max((version_key(r) for r in keep if version_key(r)), default=())
    extra = [r for r in tags if r not in keep and (version_key(r) or ()) > newest]
    for line in gitt(state, "for-each-ref", "--format=%(refname)", "refs/anchors").splitlines():
        git(state, "update-ref", "-d", line)
    anchors = {}
    for i in range(0, len(extra), 100):
        git(state, "fetch", "-q", "--no-tags", "upstream", *[f"+{r}:refs/anchors/{r[len('refs/tags/'):]}" for r in extra[i:i + 100]])
    for r in extra:
        anchors[r] = gitt(state, "merge-base", BRANCHES[1], f"refs/anchors/{r[len('refs/tags/'):]}").strip()
    local = {}
    for line in gitt(state, "for-each-ref", "--format=%(refname) %(objectname)", "refs/tags").splitlines():
        r, s = line.split()
        local[r] = s
    for r in set(local) - set(keep) - set(anchors):
        git(state, "update-ref", "-d", r)
    for r, base in anchors.items():
        if local.get(r) != base:
            git(state, "update-ref", r, base)
    todo = [r for r in keep if local.get(r) != keep[r]]
    for i in range(0, len(todo), 100):
        git(state, "fetch", "-q", "--no-tags", "upstream", *[f"+{r}:{r}" for r in todo[i:i + 100]])
    keep.update(anchors)
    return stable, len(keep)


# ------------------------------------------------------- 2. pass 1: classify
def load_cache(path):
    cache = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            f = line.split("\t")
            if len(f) == 5 and f[3].isdigit():  # a run killed mid-write leaves a short last line
                cache[f[0]] = (f[1], f[2], int(f[3]), f[4])
    return cache


def stream_heads(repo, shas):
    """Yield (sha, size, body): the first 64 KiB, or the whole blob when it reads as text."""
    p = subprocess.Popen(["git", "-C", str(repo), "cat-file", "--batch"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    def feed():
        try:
            for s in shas:
                p.stdin.write(s.encode() + b"\n")
        finally:
            p.stdin.close()

    threading.Thread(target=feed, daemon=True).start()
    out = p.stdout
    while True:
        line = out.readline()
        if not line:
            break
        parts = line.split()
        if len(parts) != 3:
            sys.exit(f"cat-file: {line!r}")
        sha, size = parts[0].decode(), int(parts[2])
        body = out.read(min(size, HEAD))
        rest = size - len(body)
        if rest:
            try:
                codecs.getincrementaldecoder("utf-8")().decode(body, final=False)
                texty = True
            except UnicodeDecodeError:
                texty = False
            if texty:
                body += out.read(rest)
            else:
                while rest:
                    rest -= len(out.read(min(rest, 1 << 20)))
        out.read(1)
        yield sha, size, body
    p.wait()


def blob_paths(repo, refs):
    paths = {}
    r = subprocess.Popen(["git", "-C", str(repo), "rev-list", "--objects", "--filter=object:type=blob", *refs],
                         stdout=subprocess.PIPE)
    for line in r.stdout:
        sha, _, path = line.decode("utf-8", "replace").rstrip("\n").partition(" ")
        if path and sha not in paths:
            paths[sha] = path
    r.wait()
    return paths


def pass1(state, refs):
    cache_path = state / "classified.tsv"
    cache = load_cache(cache_path)
    print("pass 1: listing blobs", flush=True)
    paths = blob_paths(state, refs)
    new = []
    for sha, path in paths.items():
        c = cache.get(sha)
        if c is None:
            new.append(sha)
        elif c[0] != "flag" and scan.KEY_EXT.search(path) and c[2] > 0:
            cache[sha] = ("flag", "key or keystore file", c[2], path)  # sticky once named
    print(f"pass 1: {len(paths)} blobs, {len(new)} not yet classified", flush=True)
    done = 0
    with open(cache_path, "a", encoding="utf-8", newline="\n") as f:
        for sha, size, body in stream_heads(state, new):
            level, kind = scan.classify(body, [paths[sha]] if size else [])
            c = (level or "ok", kind or "", size, paths[sha])
            cache[sha] = c
            f.write("\t".join(map(str, (sha, *c))) + "\n")
            done += 1
            if done % 5000 == 0:
                f.flush()
                print(f"  classified {done}/{len(new)}", flush=True)
    # an upgraded verdict (a key name seen later) is appended too; the last line wins on load
    with open(cache_path, "w", encoding="utf-8", newline="\n") as f:
        for sha, c in cache.items():
            f.write("\t".join(map(str, (sha, *c))) + "\n")
    # files are text or images only, so a noted blob goes as well as a flagged one
    flagged = {s: c for s, c in cache.items() if c[0] in ("flag", "note") and s in paths}
    (state / "strip-ids.txt").write_text("".join(s + "\n" for s in sorted(flagged)), encoding="ascii", newline="\n")
    return paths, flagged


# ------------------------------------------------------------ 3. pass 2
def ensure_filter_repo(state):
    p = state / "git-filter-repo.py"
    if not p.exists():
        data = urllib.request.urlopen(FILTER_REPO_URL, timeout=60).read()
        if FILTER_REPO_SHA256 and hashlib.sha256(data).hexdigest() != FILTER_REPO_SHA256:
            sys.exit("git-filter-repo download does not match the pinned sha256")
        p.write_bytes(data)
    return p


def tree_text_hash(entries):
    """git hash-object of ls-tree output, as content_aware_hash.sh computes it."""
    text = "".join(f"{mode} {typ} {sha}\t{name}\n" for mode, typ, sha, name in entries).encode()
    return hashlib.sha1(b"blob %d\0" % len(text) + text).hexdigest()


def engine_map(state, commits):
    """Original commit -> engine hash, for each commit that has engine/ but no engine.version.

    The tool prefers a tracked bin/internal/engine.version over computing the hash
    from the DEPS and engine tree ids; master dropped the file, and stripping blobs
    under engine/ changes those ids, so the rewrite writes the hash of the original
    tree into such commits.
    """
    probes = ("bin/internal/engine.version", "engine", "DEPS", "bin/internal/release-candidate-branch.version")
    inp = "".join(f"{c}:{p}\n" for c in commits for p in probes).encode()
    out = git(state, "cat-file", "--batch-check", data=inp).decode().splitlines()
    emap = {}
    for i, c in enumerate(commits):
        have, eng, deps, rcb = (l.split() for l in out[4 * i:4 * i + 4])
        if have[-1] != "missing" or eng[-1] == "missing" or deps[-1] == "missing":
            continue
        entries = [("100644", "blob", deps[0], "DEPS")]
        if rcb[-1] != "missing":
            entries.append(("100644", "blob", rcb[0], "bin/internal/release-candidate-branch.version"))
        entries.append(("040000", "tree", eng[0], "engine"))
        emap[c] = tree_text_hash(entries)
    for c in commits[:: max(1, len(commits) // 100)]:  # the arithmetic against git's own ls-tree
        if c in emap and content_hash(state, c) != emap[c]:
            sys.exit(f"engine hash of {c} differs from git ls-tree's")
    return emap


ENGINE_CALLBACK = """
import json
global _EM
try:
    _EM
except NameError:
    _EM = json.load(open(%r))
b = _EM.get(commit.original_id.decode())
if b:
    commit.file_changes = [c for c in commit.file_changes if c.filename != b'bin/internal/engine.version']
    commit.file_changes.append(FileChange(b'M', b'bin/internal/engine.version', b.encode(), b'100644'))
"""


def write_engine_blobs(work, emap):
    """Write one blob per distinct hash into the work repo; return commit -> blob id."""
    blobs, stream = {}, bytearray()
    for h in sorted(set(emap.values())):
        data = h.encode() + b"\n"
        blobs[h] = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
        stream += b"blob\ndata %d\n" % len(data) + data + b"\n"
    run(["git", "-C", str(work), "fast-import", "--quiet"], data=bytes(stream))
    return {c: blobs[h] for c, h in emap.items()}


def rewrite(state, work, ids_file, emap):
    if work.exists():
        shutil.rmtree(work, onerror=lambda f, p, e: (os.chmod(p, 0o700), f(p)))
    run(["git", "clone", "-q", "--bare", "--no-local", state.as_uri(), str(work)])
    fr = ensure_filter_repo(state)
    mapfile = state / "engine-blobs.json"
    mapfile.write_text(json.dumps(write_engine_blobs(work, emap), sort_keys=True), encoding="ascii", newline="\n")
    r = run([sys.executable, "-I", str(fr), "--strip-blobs-with-ids", str(ids_file), "--force",
             "--commit-callback", ENGINE_CALLBACK % str(mapfile)],
            cwd=work, check=False)
    log = (r.stdout + r.stderr).decode("utf-8", "replace")
    (state / "filter-repo.log").write_text(log, encoding="utf-8")
    if r.returncode:
        sys.exit("git filter-repo failed:\n" + log[-2000:])
    # upstream commit -> mirror commit, for `flutter_sdk.py update` (a pruned commit maps to 0000...)
    shutil.copyfile(work / "filter-repo" / "commit-map", state / "commit-map.txt")
    return [l for l in log.splitlines() if l.startswith("NOTE")]


# ----------------------------------------------------------- 4. verify
def verify(work, upstream_state, blobmap):
    refs = BRANCHES + gitt(work, "for-each-ref", "--format=%(refname)", "refs/tags").split()
    paths = blob_paths(work, refs)
    flagged, notes, count = [], collections.defaultdict(list), 0
    for sha, size, body in stream_heads(work, list(paths)):
        # the real classifier on the whole blob, stricter than pass 1
        if size > len(body):
            body = git(work, "cat-file", "blob", sha)
        level, kind = scan.classify(body, [paths[sha]])
        count += 1
        if level == "flag":
            flagged.append((sha, kind, paths[sha]))
        elif level == "note":
            notes[kind].append((sha, paths[sha]))
    # engine.version: unchanged where upstream has it; where upstream lacks it, the rewrite's
    # computed blob if the commit is in the map, else still missing
    inp = "".join(f"{r}:{ENGINE_FILE}\n" for r in refs).encode()
    a = git(upstream_state, "cat-file", "--batch-check", data=inp).decode().splitlines()
    b = git(work, "cat-file", "--batch-check", data=inp).decode().splitlines()
    tips = git(upstream_state, "cat-file", "--batch-check=%(objectname)",
               data="".join(f"{r}^{{commit}}\n" for r in refs).encode()).decode().split()
    engine_diff = []
    for r, x, y, tip in zip(refs, a, b, tips):
        if not x.endswith("missing"):
            ok = x == y
        elif tip in blobmap:
            ok = y == f"{blobmap[tip]} blob 41"
        else:
            ok = y.endswith("missing")
        if not ok:
            engine_diff.append(r)
    return count, flagged, notes, engine_diff, refs


# ----------------------------------------------------------- 5. publish
def content_hash(repo, ref):
    """master's engine version: a hash of the DEPS and engine tree ids (content_aware_hash.sh)."""
    tree = git(repo, "ls-tree", ref, "--", "DEPS", "engine", "bin/internal/release-candidate-branch.version")
    return git(repo, "hash-object", "--stdin", data=tree).decode().strip()


def has_engine(version):
    url = f"https://storage.googleapis.com/flutter_infra_release/flutter/{version}/dart-sdk-linux-x64.zip"
    try:
        return urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=30).status == 200
    except Exception:
        return False


def write_notes(work, upstream_tip, mirror_tip, master_tip, flagged, count_tags, nblobs, notes, engine=()):
    by = collections.defaultdict(list)
    for sha, c in sorted(flagged.items(), key=lambda kv: (kv[1][1], kv[1][3])):
        by[c[1]].append((sha, c))
    lines = [
        "# Removed from this mirror", "",
        "A scanner-clean mirror of flutter/flutter's `stable` branch with its release tags, and of",
        "`master`, upstream's development branch.",
        "Every blob a malware, secret or archive scanner may refuse was removed from every",
        "commit (`git filter-repo --strip-blobs-with-ids`); the rest of the history is upstream's.", "",
        f"- upstream `stable` commit: `{upstream_tip}`",
        f"- mirror `stable` commit: `{mirror_tip}` (the pushed `stable` head is this commit plus one:",
        "  `.github/workflows/flutter-images.yml`, the dispatcher of the `images` branch's container image build;",
        "  it is no upstream commit, and `flutter --version` on `stable` counts it)",
        f"- mirror `master` commit: `{master_tip}`",
        f"- tags: {count_tags}",
        f"- blobs scanned in the result: {nblobs}",
        f"- blobs removed: {len(flagged)}", "",
        "A removed blob is absent from all commits that held it, under whatever path. The",
        "path shown is one place it was seen. Tests that read these fixtures fail.", ""]
    if engine:
        up, mine, up_ok = engine
        lines += [
            "## `master` and its engine", "",
            "Upstream's `master` has no `bin/internal/engine.version`: the tool hashes the `DEPS` and",
            "`engine` tree ids. Removing blobs under `engine/` changes that tree, so the rewrite",
            "writes `bin/internal/engine.version` (which the tool prefers when tracked) into every",
            "commit that has `engine/` and lacks the file, holding the hash of that commit's",
            "original upstream tree. No environment variable is needed.", "",
            f"- engine for this master: `{up}` (artifacts published: {'yes' if up_ok else 'NO'})",
            f"- hash of the mirror's own tree, not published and not used: `{mine}`",
            "- `stable` and the tags carry upstream's `engine.version`, unchanged.", ""]
    for kind, items in sorted(by.items()):
        lines += [f"## {kind} ({len(items)})", "", "| blob | bytes | path |", "| --- | ---: | --- |"]
        lines += [f"| `{s}` | {c[2]} | `{c[3]}` |" for s, c in items]
        lines.append("")
    if notes:
        lines += ["## Kept, but a scanner may note them", ""]
        lines += [f"- {k}: {len(v)}" for k, v in sorted(notes.items())] + [""]
    return "\n".join(lines)


def with_dispatcher(repo, head):
    """The commit that is `head` plus DISPATCH_PATH, or `head` when its tree holds that file already."""
    data = DISPATCH_SRC.read_bytes().replace(b"\r\n", b"\n")
    blob = git(repo, "hash-object", "-w", "--stdin", data=data).decode().strip()
    base = gitt(repo, "rev-parse", f"{head}^{{tree}}").strip()
    have = run(["git", "-C", str(repo), "rev-parse", f"{head}:{DISPATCH_PATH}"], check=False)
    if have.returncode == 0 and have.stdout.decode().strip() == blob:
        return head
    idx = Path(repo) / "dispatch.index"
    env = dict(os.environ, GIT_INDEX_FILE=str(idx))
    try:
        run(["git", "-C", str(repo), "read-tree", base], env=env)
        run(["git", "-C", str(repo), "update-index", "--add", "--cacheinfo", f"100644,{blob},{DISPATCH_PATH}"], env=env)
        tree = run(["git", "-C", str(repo), "write-tree"], env=env).stdout.decode().strip()
    finally:
        idx.unlink(missing_ok=True)
    date = "2026-11-25T00:00:00+00:00"
    cenv = dict(os.environ, GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    return run(["git", "-C", str(repo), *commit_identity(), "commit-tree", tree, "-p", head, "-m",
                "ci: the workflow that starts the Flutter container image build"], env=cenv).stdout.decode().strip()


def dispatcher_only(target):
    """Add the dispatcher commit to the target's `stable` as it is (a fast-forward push)."""
    import tempfile
    url = f"https://github.com/{target}.git"
    tmp = tempfile.mkdtemp(prefix="mirror-dispatch-")
    try:
        git(tmp, "init", "-q", "--bare")
        git(tmp, "fetch", "-q", "--depth", "1", url, "refs/heads/stable")
        head = gitt(tmp, "rev-parse", "FETCH_HEAD").strip()
        new = with_dispatcher(tmp, head)
        if new == head:
            print("stable already carries the dispatcher")
        else:
            git(tmp, "push", "-q", url, f"{new}:refs/heads/stable")
            print(f"stable {head} -> {new}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def remote_refs(target):
    out = gitt(".", "ls-remote", f"https://github.com/{target}.git")
    return {l.split("\t")[1]: l.split("\t")[0] for l in out.splitlines()}


def ensure_repo(target):
    r = run(["gh", "repo", "view", target], check=False)
    if r.returncode == 0:
        return
    desc = ("Scanner-clean mirror of flutter/flutter stable (with tags) and master; the removed files "
            "are listed in MIRROR.md on the mirror-notes branch")
    run(["gh", "repo", "create", target, "--private", "--description", desc])


def publish(work, target, notes_md, refs):
    ensure_repo(target)
    url = f"https://github.com/{target}.git"
    have = remote_refs(target)
    # the notes branch: one orphan root, then a commit only when MIRROR.md changes
    blob = gitt(work, "hash-object", "-w", "--stdin").strip() if False else None
    p = subprocess.run(["git", "-C", str(work), "hash-object", "-w", "--stdin"], input=notes_md.encode(),
                       capture_output=True, check=True)
    blob = p.stdout.decode().strip()
    tree = git(work, "mktree", data=f"100644 blob {blob}\tMIRROR.md\n".encode()).decode().strip()
    parent = have.get("refs/heads/mirror-notes")
    if parent:
        git(work, "fetch", "-q", url, "+refs/heads/mirror-notes:refs/remotes/mirror/mirror-notes")
        if gitt(work, "rev-parse", "refs/remotes/mirror/mirror-notes^{tree}").strip() == tree:
            notes_commit = parent
        else:
            notes_commit = None
    else:
        notes_commit = None
    if notes_commit is None:
        env = dict(os.environ, GIT_AUTHOR_DATE="2026-11-08T00:00:00+00:00", GIT_COMMITTER_DATE="2026-11-08T00:00:00+00:00")
        args = ["git", "-C", str(work), *commit_identity(), "commit-tree", tree, "-m", "docs: MIRROR.md, what was removed"]
        if parent:
            args += ["-p", parent]
            env = os.environ
        notes_commit = run(args, env=env).stdout.decode().strip()
    want = {r: gitt(work, "rev-parse", r).strip() for r in refs}
    want["refs/heads/main"] = want["refs/heads/master"]
    want["refs/heads/stable"] = with_dispatcher(work, want["refs/heads/stable"])
    want["refs/heads/mirror-notes"] = notes_commit
    moved = {"created": [], "moved": [], "same": 0}
    push = []
    for r, s in want.items():
        if r not in have:
            moved["created"].append(r)
        elif have[r] != s:
            moved["moved"].append(r)
        else:
            moved["same"] += 1
            continue
        push.append(f"+{s}:{r}" if r in moved["moved"] else f"{s}:{r}")
    for i in range(0, len(push), 50):
        git(work, "push", "-q", url, *push[i:i + 50])
    if push and "refs/heads/stable" not in have:
        run(["gh", "repo", "edit", target, "--default-branch", "stable"])
    return moved


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--state", default=os.environ.get("FLUTTER_MIRROR_STATE") or str(Path.home() / ".cache" / "flutter_mirror"))
    ap.add_argument("--upstream", default="https://github.com/flutter/flutter.git")
    ap.add_argument("--target", default="Boriken-Dev/flutter")
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--rewrite", action="store_true", help="rerun both passes even if upstream did not move")
    ap.add_argument("--keep-work", action="store_true")
    ap.add_argument("--dispatcher-only", action="store_true", help="only add the dispatcher commit to the target's stable")
    a = ap.parse_args(argv)
    if a.dispatcher_only:
        dispatcher_only(a.target)
        return 0
    state = Path(a.state)
    work = Path(str(state) + ".work")

    tip, ntags = fetch(state, a.upstream)
    print(f"upstream stable {tip}, {ntags} tags", flush=True)
    refs = BRANCHES + gitt(state, "for-each-ref", "--format=%(refname)", "refs/tags").split()
    snap = {r: gitt(state, "rev-parse", r).strip() for r in refs}
    snap["rules"] = "engine-file-1+branch-point-tags"  # bump when the rewrite changes, so a plain run redoes it
    last = state / "last-pushed.json"
    if not a.rewrite and not a.no_push and last.exists() and json.loads(last.read_text()) == snap:
        print("upstream refs unchanged since the last push: nothing to do (--rewrite forces both passes)")
        return 0

    paths, flagged = pass1(state, refs)
    kinds = collections.Counter(c[1] for c in flagged.values())
    print(f"flagged {len(flagged)}: {dict(kinds)}", flush=True)
    print("pass 2: filter-repo", flush=True)
    emap = engine_map(state, gitt(state, "rev-list", BRANCHES[1], "^" + BRANCHES[0]).split())  # master-only: stable's ancestors keep their SHAs
    print(f"engine.version written into {len(emap)} commits lacking it", flush=True)
    fr_notes = rewrite(state, work, state / "strip-ids.txt", emap)
    for n in fr_notes:
        print(n)
    blobmap = json.loads((state / "engine-blobs.json").read_text())
    nblobs, bad, notes, engine_diff, wrefs = verify(work, state, blobmap)
    mirror_tip = gitt(work, "rev-parse", "refs/heads/stable").strip()
    master_tip = gitt(work, "rev-parse", "refs/heads/master").strip()
    print(f"mirror master {master_tip}")
    print(f"verify: {nblobs} blobs, {len(bad)} flagged, notes { {k: len(v) for k, v in notes.items()} }")
    print(f"engine.version differs on {len(engine_diff)} refs; mirror stable {mirror_tip}")
    if bad:
        for s, k, p in bad[:20]:
            print("  STILL FLAGGED", s, k, p)
        return 1
    if engine_diff:
        print("engine.version changed:", engine_diff[:10])
        return 1
    if a.no_push:
        return 0
    up_hash = content_hash(state, "refs/heads/master")
    in_mirror = gitt(work, "show", f"refs/heads/master:{ENGINE_FILE}").strip()
    if in_mirror != up_hash:
        print(f"master's {ENGINE_FILE} is {in_mirror}, upstream's computed hash is {up_hash}")
        return 1
    engine = (up_hash, content_hash(work, "refs/heads/master"), has_engine(up_hash))
    print(f"master engine {engine[0]} artifacts={engine[2]}; hash of the mirror's own tree {engine[1]}")
    md = write_notes(work, tip, mirror_tip, master_tip, flagged, ntags, nblobs, notes, engine)
    moved = publish(work, a.target, md, wrefs)
    last.write_text(json.dumps(snap), encoding="utf-8", newline="\n")
    print(f"created {len(moved['created'])}, moved {len(moved['moved'])}, unchanged {moved['same']}")
    for r in moved["moved"] + moved["created"][:5]:
        print("  ", r)
    if not a.keep_work:
        shutil.rmtree(work, onerror=lambda f, p, e: (os.chmod(p, 0o700), f(p)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
