# Shared repository tools

`repo/tools/` holds files a repository copies verbatim into its own `tools/`:

| File | What it does |
| --- | --- |
| `build.py` | The deps branch (vendored crates, pub packages, CDN files), the archive and binary scan, the SDK lock, offline and release builds, Flutter helpers |
| `public_api.py` | Checks or rewrites the public API snapshots of Rust crates |
| `apk.py` | Checks a built Android APK: forbidden strings, permissions, ABIs, required libraries |

Each is standard library only, Python 3.11.0 or later, and is configured by one file, `tools/build.cfg` (TOML). A section
the file leaves out is off. Flags are documented by `build.py --help` and `build.py help`; every `build.cfg` key with its
default is printed by `build.py help config`, and a key that is not in that table is refused with its path.

## Adopting

1. Make the first copy from a checkout of this repository, naming the repository to copy into:
   `python overlays/release/repo/tools/build.py --root REPO self update COMMIT --from CHECKOUT`. `COMMIT` is the commit of
   this repository to copy from (a branch or tag works too); without `--from` the files are downloaded from GitHub.
2. Write `tools/build.cfg`. `[tools] files = ["build.py"]` is the default; list every shared file the repository copies
   (`"public_api.py"`, `"apk.py"`).
3. Run `python tools/build.py self check`. CI runs the same command.

## Keeping the copy equal

Line 3 of every shared file is `# From Boriken-Dev/agent-overlays, overlays/release/repo/tools/FILE, at commit COMMIT.`; in
this repository `COMMIT` is the word `SOURCE`, since a commit cannot hold its own hash. `self check` downloads each listed
file at the commit its line 3 names, puts `SOURCE` back and compares bytes. It fails on a difference, on a listed file that
is missing, on a file with the header that is not listed, and on copies naming different commits.

`self update REV` writes every listed file at `REV` and runs the check. A shared file is edited here, never in a copy: a
copy that must change is changed in this repository and updated.

## The deps branch (`deps`, `scan`)

`deps` keeps an orphan branch (default `deps`) equal to `Cargo.lock`: `crates/` is a cargo vendor directory with one
`NAME-VERSION/` per package, from crates.io or git, each with a `.cargo-checksum.json`. Tests, examples, benchmarks,
fixtures and dot-directories are left out; a binary a build reads is written as hex or as an array literal; the markers of a
PEM private key are defanged; nothing an archive scanner would refuse stays. `manifest.json` records the format, the source
commit and the SHA-256 of `Cargo.lock`, and the checks read it.

- Run `deps init` once, then `deps refresh` on a clean main: it syncs, commits on the deps branch, rewrites `[[deps.repin]]`
  files to the new commit, then commits the `[deps] pin` row of an SDK lock on main and checks the result. Push the deps
  branch first, then main. Refresh runs locally and commits as whoever `git config` names; CI only checks.
- `deps check --ref COMMIT` holds a commit of the repository (fetched with `git fetch --depth 1 origin COMMIT`) to the lock and
  to the scanner: symlinks, `.git` paths and refused kinds are named. `--quick` reads package metadata only, as `deps use` does.
- `deps use` writes a cargo home outside the repository that replaces every source by the checkout's `crates/`;
  `--repo-config` also writes `.cargo/config.toml`, `--json` prints the result for a script.
- The checkout is found by `--deps`, `$BUILD_DEPS`, `[deps] checkout`, else a detached worktree `../REPO-deps`
  of `origin/deps` that `deps path` adds when it is missing. `deps refresh` needs the worktree on the branch (`deps init`).
- A mirror of crates.io is `[deps] crates_url` or `$BUILD_CRATES_URL`.
- `scan [REF...|--all-refs|--tree DIR]` prints what is refused and `PASS|FAIL: N flagged, M paths scanned`. It is strict: a
  file that is not text or a known image or font is flagged, including `wasm`, `pdf`, `sqlite` and unrecognised binaries.
  `[deps] allow` lets one kind through one path pattern (`"cdn/www.gstatic.com/flutter-canvaskit/*:wasm"`); a repository with
  text files that are not UTF-8 allows `"*:unrecognised binary"` for the whole-history scan.
