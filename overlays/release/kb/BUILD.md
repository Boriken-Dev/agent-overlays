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

## Flutter (`[deps.pub]`, `[deps.cdn]`, `offline`, `flutter`)

A section the configuration leaves out is off.

- `[deps.pub] lock = "app/pubspec.lock"` puts the Dart packages on the deps branch: `deps sync --pub-cache DIR` copies the
  hosted packages and their hashes (`pub/hosted/HOST/NAME-VERSION`, `pub/hosted-hashes/`) and, with `git = "checkout"`, each git
  package's checkout without `.git` (`pub/git/REPO-REVISION/`); with `git = "record"` git packages are only listed in
  `manifest.json`. Only packages the lock names are copied. The `drop` directories (default `test`, `tests`, `example`,
  `examples`, `benchmark`, `benchmarks`, `doc`, `docs`, `.github`) and every file the scanner would refuse are left out and
  listed in the manifest. `require` names packages that must be present whatever pub resolves. `deps check` fails on any
  hosted or git package the lock names that the branch lacks.
- `[deps.cdn] index = "app/tool/cdn_files.json"` (`{"files": [{"host", "path", "sha256"}]}`) fills `cdn/HOST/PATH`, each file
  verified against its hash; `hosts` limits the index to some hosts.
- `offline plan [--json]` checks the deps checkout against `pubspec.lock` and `Cargo.lock`, seeds a pub cache below
  `target/offline-pub-cache` (git packages the branch only records come from the user's pub cache, which is only read) and
  writes the cargo home, or prints why a build must go online.
- `flutter junctions` recreates the Windows plugin links `flutter build windows` needs, as directory junctions that need no
  Developer Mode; `flutter precache-check` compares a Flutter checkout's version, engine stamp and Dart with the SDK lock rows
  named in `[flutter]`.

## The SDK lock (`sdk`)

`[sdk] lock` (default `tools/sdk.lock`) is the only file to trust for what a build is made with: one row per line,
`GROUP NAME VERSION OS-ARCH|any SHA256|- URL|-`, space separated, `#` for a comment, so a shell can read it without a parser. A
download has a hash, a git item (a URL ending in `.git`) is at a commit, a `system` row names what is only checked (its
version column is the group that needs it), and no version floats (`latest`, `stable`, `main`).

- `sdk check [--online] [--deps-ref REF]` lints the lock, covers the `[deps] pin` commit with `deps check --ref`, and compares
  every `[[sdk.agree]]` rule: a regular expression over a file of the repository (or of a git ref, `@pin` for the deps commit,
  `@deps` for `--deps-ref`) whose value must equal, prefix-match, set-match or be among the lock's rows (`match`). That is how
  `rust-version`, a pinned nightly in workflows, a container digest, `ARG`s in a Dockerfile or a requirements file are held to
  the lock. `--online` compares each download's hash with what its publisher says (rustup's `.sha256`, Node's
  `SHASUMS256.txt`, a GitHub release asset's `digest`).
- `sdk install [--groups a,b] [--prefix DIR] [--check] [--print-system] [--platform OS-ARCH]` installs the groups under one
  prefix (default `$BUILD_SDK_HOME`, else `~/.REPO-sdk`) and never touches the shell profile, PATH or the system, and never
  elevates: rustup without profile edits, each `rust` row as a toolchain (numeric versions with the `rust-component` and
  `rust-target` rows; a `nightly-DATE` without), `cargo-*` rows with `cargo install --locked`, archives unpacked member by
  member by version, git items cloned at their commit (`[[sdk.post]]` runs a command once in one), `venv` items from a hashed
  requirements file, the locked LLVM on Windows ARM64 only when no clang is found, `docker://` rows reported, and
  `env.sh`/`env.ps1` written (`[sdk.env]` and `[sdk] path` add variables and directories). Every download is verified against
  the lock before it is used. `--check` installs nothing and exits 1 when something is missing; `system` rows are only checked,
  and `--print-system` prints `[sdk.system] print` for the platform.
