# Shared repository tools

`repo/tools/` holds files a repository copies verbatim into its own `tools/`:

| File | What it does |
| --- | --- |
| `build.py` | The deps branch (vendored crates, pub packages, CDN files), the archive and binary scan, the SDK lock, offline and release builds, container builds from the deps branch, Flutter helpers |
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
fixtures, `doc`, `docs`, `ci` and dot-directories are left out, and so are container files (`Containerfile`, `Dockerfile*`,
`.dockerignore`, compose files) and, at a package's top, developer scripts (`*.sh`, `*.bat`, `*.cmd`, `*.ps1`) and tool
dotfiles (`.editorconfig`, `.travis.yml`, `justfile`, `flake.nix`, ...); a file a kept source includes by path stays, and
`deps sync` re-writes a package already on the branch that holds a file these rules now leave out; a binary a build reads is written as hex or as an array literal; the markers of a
PEM private key are defanged; nothing an archive scanner would refuse stays. `manifest.json` records the format, the source
commit and the SHA-256 of `Cargo.lock`, and the checks read it.

- **Several vendor folders.** `[[deps.vendor]]` lists one folder per lock, since two locks may need two sources of one
  package (another git revision of a crate) and a folder is never shared. `folder` is one path segment; exactly one of
  `lock` (a `Cargo.lock` of the repository) and `item` (an SDK-lock row `GROUP NAME` naming a crate that `cargo install
  --locked` builds: its lock is the `Cargo.lock` packaged in `FOLDER/NAME-VERSION/`, and the row's SHA-256 is the `.crate`
  file's `cksum` from the crates.io index, which `deps sync` needs and `deps check` only notes when it is `-`) is given;
  `bin` is the binary an item installs (default the row's name). The first table is the primary vendor, the one `deps use`,
  `offline plan` and `release build --offline` take, and `[deps] cargo_lock` is unused once a list exists; without a list
  there is one vendor, `crates`, over `cargo_lock`. `deps sync` and `deps check` cover every vendor, `deps refresh` always
  does, and `--vendor FOLDER` on `sync`, `check`, `use` and `offline plan` picks one (`--cargo-lock` then replaces that
  vendor's lock; an item vendor refuses it). A problem names its folder: `crates-web_noise/js-sys-0.3.105: missing, Cargo.lock
  names it`, and `deps check` ends with `ok: FOLDER N crates, M git sources cover LOCK` for each.
- **`manifest.json`, formats 1 and 2.** Without `[[deps.vendor]]` it is format 1 as always (`cargoLockSha256`, `crates`,
  `gitSources`). With vendors it is format 2: `vendors` maps each folder to `{lock | item, lockSha256, crates, gitSources}`
  beside the `pub` and `cdn` sections. The tool owns only `format`, `sourceCommit`, `cargoLockSha256`, `crates`,
  `gitSources`, `vendors`, `pub`, `cdn` and `trees`; every other top-level key another writer put there is kept, and an owned
  section that is turned off disappears at the next sync. A format-2 manifest that does not record a configured vendor gives
  a note, never a problem; any other format is a problem.

- Run `deps init` once, then `deps refresh` on a clean main: it syncs, commits on the deps branch, rewrites `[[deps.repin]]`
  files to the new commit, then commits the `[deps] pin` row of an SDK lock on main and checks the result. Push the deps
  branch first, then main. Refresh runs locally and commits as whoever `git config` names; CI only checks.
- `deps check --ref COMMIT` holds a commit of the repository (fetched with `git fetch --depth 1 origin COMMIT`) to the lock and
  to the scanner: symlinks, `.git` paths and refused kinds are named. `--quick` reads package metadata only, as `deps use` does.
- **A whole repository tree.** `[[deps.tree]]` (`name`, `repo`, `pin`, `path`, `license`, `url`) carries one commit of a
  GitHub repository whole and unmodified under `path` on the deps branch, for a library whose licence allows it only in its
  original form. `pin` is a JSON file of this repository with a 40-hex `commit` and `files`, the SHA-256 of every file of the
  tree (its other keys are the repository's own, such as a carried subset, and are kept). `deps sync` leaves a file that
  already matches, otherwise fetches the archive of the commit once (`url`, default codeload.github.com), holds it in
  memory, refuses anything but regular files with a path inside the tree, requires its files to be exactly the pin's with the
  pin's hashes, and writes them as they are (no trimming, no line-ending change); files under `path` the pin does not name
  are removed. A file the scanner would refuse stops the sync, naming its path and kind, unless `[deps] allow` lets that
  kind through that path; nothing is dropped silently. The CDN step leaves every tree's `path` alone, and the tree step
  touches nothing outside it. `deps check` (the checkout and `--ref`) names a missing, changed or extra file (`--quick`
  skips the hash), a `license` the pin does not name, and a manifest entry at another commit, and notes a manifest that
  records no `trees.NAME`. `manifest.json` gets `trees: {NAME: {repository, commit, path, files: [{path, sha256, bytes}]}}`.
  `deps tree-pin NAME COMMIT` fetches the archive at a 40-hex commit (a branch name is refused, so a pin never floats) and
  rewrites the pin file's `commit` and `files`, keeping its other keys and their order (LF, `indent=1`).
- `deps use` writes a cargo home outside the repository that replaces every source by the checkout's `crates/`;
  `--repo-config` also writes `.cargo/config.toml`, `--json` prints the result for a script (one vendor: the first, or
  `--vendor`).
- The checkout is found by `--deps`, `$BUILD_DEPS`, `[deps] checkout`, else a detached worktree `../REPO-deps`
  of `origin/deps` that `deps path` adds when it is missing. `deps refresh` needs the worktree on the branch (`deps init`).
- A mirror of crates.io is `[deps] crates_url` or `$BUILD_CRATES_URL`.
- `scan [REF...|--all-refs|--tree DIR]` prints what is refused and `PASS|FAIL: N flagged, M paths scanned`. It is strict: a
  file that is not text or a known image or font is flagged, including `wasm`, `pdf`, `sqlite` and unrecognised binaries.
  `[deps] allow` lets one kind through one path pattern (`"cdn/www.gstatic.com/flutter-canvaskit/*:wasm"`).
  Text that is not UTF-8 (ISO-8859-1, Windows-1252, or UTF-8 with a stray such byte) passes and prints
  `NOTE text, not UTF-8`, as `file` calls it text: no NUL, no control character but tab, LF, FF and CR, and at most 10% of
  its bytes from 0x80 up. A private key in it is still refused; UTF-16 and anything with NULs is still a binary.

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
  writes the cargo home, or prints why a build must go online. For a git package of `git = "checkout"` it writes what pub
  needs to take the checkout without the network: `.git/pub-packages` lists each folder of the checkout that holds a
  `pubspec.yaml` (`.` for its root; rewritten on every run), and `git/cache/REPO-SHA1` (the SHA-1 of the package's URL) is a
  bare repository in which the locked revision resolves, through `refs/replace/REV`, to an empty commit; an existing one
  is kept. Without them `flutter pub get --offline` clones from the network.
- `offline env [--json]` runs the plan and prints the variables an offline build runs under, as `KEY=VALUE` lines or a JSON
  object (with `cache` and `cargo_home`): `PUB_CACHE`, `CARGO_HOME`, `CARGO_NET_OFFLINE=true`,
  `FLUTTER_SUPPRESS_ANALYTICS=true`, `DART_SUPPRESS_ANALYTICS=true` and `FLUTTER_STORAGE_BASE_URL` (the process's own value
  when set, else `[offline] storage_url`, default `http://127.0.0.1:9`, which nothing answers so a build that reaches for
  Google's storage fails; empty: not set). `[offline.env]` adds variables after those, and an empty value removes one.
  `offline run [plan flags] -- COMMAND ...` runs the plan, then the command in the current directory under those
  variables, and exits with its code; when offline is not possible it prints why, runs nothing and exits 1. Both take the
  plan's flags (`--deps`, `--vendor`, `--cargo-lock`, `--pubspec-lock`, `--pub-cache`, `--out`). A repository that wants
  the network does not call `offline run`; there is no flag to turn it off.
- `flutter junctions` recreates the Windows plugin links `flutter build windows` needs, as directory junctions that need no
  Developer Mode; `flutter precache-check` compares a Flutter checkout's version, engine stamp and Dart with the SDK lock rows
  named in `[flutter]`.
- `[flutter] commit_items` (rows `GROUP NAME`, for example the mirror's `app flutter` and the upstream commit it mirrors,
  `app flutter-upstream`) makes `precache-check` also require `git rev-parse HEAD` to be the version of one of them; empty
  leaves the commit unchecked. `precache-check --platform web,windows,linux,macos` also requires each platform's files under
  `bin/cache`: `flutter_web_sdk`, `artifacts/engine/windows-ARCH`, `artifacts/engine/linux-ARCH` and
  `artifacts/engine/darwin-x64` (`ARCH` is `x64` or `arm64`, this machine's), one line for each that is missing.
- `sdk install` precaches the Flutter SDK from the engine's release, never from Google's storage. `[flutter] sdk_item` names
  the git row that is the SDK: its clone is checked out with `git checkout -B BRANCH COMMIT` (`branch`, default `main`, since
  Flutter names its channel from it) and its tags are fetched. `engine_release` is the directory of the engine's release,
  with `{engine}` replaced by the `engine_item` row's version (`https://github.com/OWNER/flutter/releases/download/engine-{engine}`):
  it holds `MANIFEST.txt` (`path TAB sha256 TAB size TAB asset`, `#` comments) and the assets. When the stamp
  `bin/cache/engine.stamp` or a `precache` platform's folder (default `["web"]`) is missing, build.py fetches each asset into
  `PREFIX/flutter-artifacts/PATH` (a file already there with the manifest's size and SHA-256 is kept, a download that is not
  what the manifest says is refused), serves that tree on `127.0.0.1` itself, and runs `flutter config --no-analytics
  --no-cli-animations`, `flutter precache --web ...` and `flutter --version` under `FLUTTER_STORAGE_BASE_URL` pointing at it,
  `FLUTTER_GIT_URL` the row's URL, `FLUTTER_ROOT`, `CI=true`, `TAR_OPTIONS=--no-same-owner` and both analytics variables. A
  request the release does not hold fails the install and is listed. With `engine_release` empty nothing is precached
  (`flutter precache  not configured ([flutter] engine_release)`); nothing ever falls back to Google, and build.py runs no
  downloaded script. `sdk install --check` prints `flutter precache P  ok|missing` for each platform.

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
  `SHASUMS256.txt`, a GitHub release asset's `digest`). `expect` is a Python format string, so a slice works: a Containerfile's
  `ARG FLUTTER_IMAGE_TAG=` that is the first ten digits of a commit is held to the row with `expect = "{version:.10}"`.
- `sdk install [--groups a,b] [--prefix DIR] [--check] [--print-system] [--platform OS-ARCH]` installs the groups under one
  prefix (default `$BUILD_SDK_HOME`, else `~/.REPO-sdk`) and never touches the shell profile, PATH or the system, and never
  elevates: rustup without profile edits, each `rust` row as a toolchain (numeric versions with the `rust-component` and
  `rust-target` rows; a `nightly-DATE` without), `cargo-*` rows with `cargo install --locked`, archives unpacked member by
  member by version, git items cloned at their commit (`[[sdk.post]]` runs a command once in one), `venv` items from a hashed
  requirements file, the locked LLVM on Windows ARM64 only when no clang is found, `docker://` rows reported, and
  `env.sh`/`env.ps1` written (`[sdk.env]` and `[sdk] path` add variables and directories). Every download is verified against
  the lock before it is used. `--check` installs nothing and exits 1 when something is missing; `system` rows are only checked,
  and `--print-system` prints `[sdk.system] print` for the platform.
- A `[[deps.vendor]]` item (a tool built from the crate on the deps branch) is installed with `cargo --config CONFIG install
  --offline --locked NAME --version V`, `CONFIG` being the cargo home `deps use` writes for its folder under the prefix; it
  needs the deps checkout (`--deps DIR`, else the usual lookup) and never falls back to the network: with none, `--check`
  says `needs a deps checkout` and the install stops. Each `rust-target` row of a numeric toolchain prints a line
  `rust-target TARGET (TOOLCHAIN)  ok|missing`; a missing target is named, and no build command ever adds one.

## Release builds (`release`)

`[release]` names the packages and targets; the next repository adds targets without code.

- `release matrix [--section binaries|universal|images]` prints the JSON a workflow's `fromJSON` reads: one source of truth for
  the runners (`[[release.target]] triple, runner`) and for the image architectures (`[release.image] archs`).
- `release build --target TRIPLE [--offline]` runs `cargo build --release --locked --target TRIPLE -p PACKAGE` for each
  `[[release.packages]]` entry (`--bin` and `--features` from it), with the target's `rustflags` and `env` (`{triple}` and
  `{triple_}` are replaced), packs `dist/NAME-VERSION-TRIPLE.tar.gz` (or `.zip`) with the binaries and the licence and readme
  files, and adds it to `dist/SHA256SUMS`. Archives carry no timestamps or owners, so the same inputs give the same bytes.
  `--offline` builds from the deps branch (`cargo --config` the cargo home `deps use` writes, `CARGO_NET_OFFLINE`).
- `release universal` joins the two built `[release.universal] macos` targets with `lipo` and checks `lipo -info` lists both.
- `release image --arch ARCH` runs `podman build` (or `docker`) on the native runner and saves an OCI archive
  (`podman save --format oci-archive`) as `dist/NAME-VERSION-image-ARCH.tar`. Nothing is pushed to a registry. `[release.image] from_deps = true` builds it as `container build` does (see "Container builds"), for a Containerfile made from the template.
- `release verify` checks with `file` that each Linux binary in `dist/` is statically linked.

A workflow runs `release matrix`, builds each target on its runner, and uploads `dist/`.

## Container builds (`container`, `.env`)

A repository with a deps branch builds in a container whose network is off: the SDK stage installs the toolchain online
from pinned bases, then `build` and `test` copy the deps branch in and run cargo with `--network=none`. The template is
`repo/packaging/container/Containerfile.rust` (stages `sdk`, `build`, `test`, `runtime`; a library deletes `runtime`);
`repo/.env.example` lists every key a repository can override. Copy both, replace the placeholders (`PACKAGE`, `BINARY`, the
digests, the workspace's directories), and hold the pins to the lock with `[[sdk.agree]]` rules:

```toml
[container]
base_item = "release rust-image"
runtime_item = "release debian-image"

[[sdk.agree]]
item = "release rust-image"
file = "packaging/container/Containerfile"
pattern = '^ARG BASE_IMAGE=docker\.io/library/rust:([^@\s]+)@sha256:[0-9a-f]{64}$'

[[sdk.agree]]
item = "release rust-image"
file = "packaging/container/Containerfile"
pattern = '^ARG BASE_IMAGE=docker\.io/library/rust:[^@\s]+@sha256:([0-9a-f]{64})$'
expect = "{sha256}"

[[sdk.agree]]
item = "release debian-image"
file = "packaging/container/Containerfile"
pattern = '^ARG RUNTIME_IMAGE=docker\.io/library/debian:([^@\s]+)@sha256:[0-9a-f]{64}$'

[[sdk.agree]]
item = "release debian-image"
file = "packaging/container/Containerfile"
pattern = '^ARG RUNTIME_IMAGE=docker\.io/library/debian:[^@\s]+@sha256:([0-9a-f]{64})$'
expect = "{sha256}"

[[sdk.agree]]
item = "core rust"
file = "packaging/container/Containerfile"
pattern = '^ARG RUST_VERSION=(\S+)$'
first = true

[[sdk.agree]]
item = "core rustup-init"
file = "packaging/container/Containerfile"
pattern = '^ARG RUSTUP_VERSION=(\S+)$'
first = true

[[sdk.agree]]
item = "core rustup-init"
file = "packaging/container/Containerfile"
pattern = '^ARG RUSTUP_INIT_SHA256_X86_64=([0-9a-f]{64})$'
platform = "linux-x64"
expect = "{sha256}"

[[sdk.agree]]
item = "core rustup-init"
file = "packaging/container/Containerfile"
pattern = '^ARG RUSTUP_INIT_SHA256_AARCH64=([0-9a-f]{64})$'
platform = "linux-arm64"
expect = "{sha256}"
```

- `container build [--target STAGE] [--tag T]` and `container test` read the settings (below), fetch the deps commit the lock's
  `deps crates` row names (from that row's URL, into `[container] work_dir`, checked against `Cargo.lock`), and run
  `ENGINE build --file F --target S --tag T [--platform P] --build-context deps=DIR --build-context ca=DIR --build-arg ... --label ... CONTEXT`.
  `container test` builds `[container] test_stage`. The deps checkout is a build context rather than a `COPY` of the repository,
  so no `.git`, no `target/` and no other branch enters the build. Docker and Podman take the same line: one `--build-arg` for
  each key, never `--build-arg-file`.
- `--print` prints the effective non-default settings first, then the deps checkout, then the command, fetching and running
  nothing; `--json` prints the same as JSON. Neither needs an engine. `[container] file`, `context` and `name` fall back to
  `[release.image]`.
- The image carries `org.opencontainers.image.revision` (the repository's `HEAD`), `build.py.deps-commit` and
  `build.py.non-default` (the names of the keys that differ from the lock, never their values).
- **The settings** are read, later wins, from `.env`, `.env.local`, the process environment (known keys only) and
  `--env KEY=VALUE`. `.env` and `.env.local` are ignored by git; `.env.example` is committed. A key that is not in the
  catalogue (`build.py help env`) is an error naming the file and line; an empty value clears a key. `sdk install` and
  `release build` read only the native keys (below) from the same files and leave the rest to `container`, so another tool's
  `.env` in the same directory breaks nothing there.
- Every key is a source or a location: mirrors for rustup (`RUSTUP_DIST_SERVER`, `RUSTUP_UPDATE_ROOT`,
  `RUSTUP_INIT_BASE_URL`) and the package archive (`APT_MIRROR`); `BASE_IMAGE` and `RUNTIME_IMAGE`, which need an
  `@sha256:` digest unless `ALLOW_UNPINNED_IMAGE=1`; `DEPS_DIR`, or `DEPS_GIT_URL` and `DEPS_REF` (a commit other than the
  lock's is read file by file against `Cargo.lock`); `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY` and `EXTRA_CA_CERT` for the SDK
  stage only; `CONTAINER_ENGINE` and `CONTAINER_PLATFORM`. A version or a hash is overridden only as `RUSTUP_VERSION` with both
  `RUSTUP_INIT_SHA256_*` keys; the Containerfile checks rustup-init against its hash whatever mirror serves it.
- A key whose value carries credentials (a proxy, a mirror with a user) goes to the engine as `--build-arg KEY` with the
  value in the engine's environment: it is on no command line and in no output, and a URL printed anywhere has its user and
  password replaced.
- **Native keys** point a build on this machine at Visual Studio, the Windows SDK and clang, and are read by `sdk install`
  and `release build` (and reported, then ignored, by `container`). Each path is checked when it is read, and a wrong one
  fails naming the key and the layer it came from, before anything is installed or cargo runs:

  | Key | Names | Must hold | Used as |
  | --- | --- | --- | --- |
  | `VSINSTALLDIR` | Visual Studio or Build Tools root | `VC/` | set for cargo (the `cc` crate reads it); stands in for vswhere in `sdk install`'s component checks |
  | `VCTOOLSINSTALLDIR` | the MSVC toolset (`VC/Tools/MSVC/VERSION`) | `bin/`, `include/`, `lib/` | set for cargo |
  | `WINDOWSSDKDIR` | the Windows SDK root (`Windows Kits/10`) | `Include/`, `Lib/` | set for cargo; stands in for the default location in `sdk install`'s SDK check |
  | `WINDOWSSDKVERSION` | the SDK version, such as `10.0.26100.0` (a trailing backslash, as vcvars writes it, is fine) | `Lib/VERSION` under `WINDOWSSDKDIR` when both are set | set for cargo |
  | `LIBCLANG_PATH` | the directory of libclang | `libclang.*` | set for cargo (bindgen, clang-sys) |
  | `LLVM_DIR` | an LLVM installation | `bin/clang*` | its `bin` first on `PATH`; `sdk install` fetches no LLVM; `LIBCLANG_PATH` defaults to its `bin` when that holds libclang |
  | `SDK_HOME` | where `sdk install` puts things | a directory, or nothing yet | the prefix: `--prefix`, then `SDK_HOME`, then `$BUILD_SDK_HOME`, then `~/.REPO-sdk` |

  `sdk install` writes the set keys into `env.sh` and `env.ps1`; `release build` adds them to cargo's environment and prints
  `native: KEY=VALUE (where)` for each. A directory given as `VSINSTALLDIR` replaces vswhere, so `sdk install` cannot tell
  which components it has: the build then proves it. Nothing of Microsoft's is copied: the keys only point.
- **Cross building to Windows** from Linux: the template's optional `cross-sdk`, `cross` and `cross-out` stages build
  `x86_64-pc-windows-msvc` with clang-cl and lld-link against `MSVC_SPLAT_DIR`, an xwin-style splat (`crt/` and `sdk/`).
  The directory is the `msvc` build context (always passed, empty when unset) and the one `RUN` that links mounts it with
  `--mount=type=bind,from=msvc,target=/mnt/msvc,readonly`, so no layer of any image holds a file of it, and the key is never
  a build argument. A splat is made from Microsoft's files under Microsoft's terms: read them before making or sharing one.
  A repository without a Windows binary deletes the three stages. `container build --target cross-out` builds it; a build
  with no splat stops on a line that says `MSVC_SPLAT_DIR is not set`.
- Nothing proprietary goes in an image: Microsoft's compilers and SDK files are never baked in or committed.

## `public_api.py` and `apk.py`

Both read their own table of `tools/build.cfg`, carry the same three header lines as `build.py`, and are kept equal to the
overlay's by `self check` once they are in `[tools] files`. An unknown key in their table is refused with its path.

- `public_api.py check|update` replaces a script that runs `cargo +TOOLCHAIN public-api` and compares its output with
  checked-in snapshots. `[public_api]`: `packages`; `toolchain`, or `sdk = "api rust"` to take it from the SDK lock; `layout =
  "dir"` (`PATH/PACKAGE.txt` for each package, default `tools/public-api`) or `"file"` (one `tools/public-api.txt`);
  `args`; `cargo`. `check` also names a snapshot no listed package owns.
- `apk.py check APK...` inspects a built Android APK, the artifact that ships rather than its inputs. `[apk]`: `forbid`
  (patterns no entry may match, each with a `control_base64` sample the pattern must match: a check whose planted control is
  not found exits 2 instead of passing, and a hit is reported by its place, never its text), `allow_flag` (a flag that
  reports hits without failing, for a build made on purpose), `permissions` (`want`, `unwanted`, read from the binary
  manifest), `abis` (exactly these), `libs` (carried by every shipped ABI), `max_entry_bytes`.
