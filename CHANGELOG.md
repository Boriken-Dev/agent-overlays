# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- engineering: the command `context-check` warns about agent context files outside the standard places (`AGENTS.md` and
  `AGENTS.*.md` in a directory and in its `.agents`; `AGENTS_DIR` and `AGENTS_CONTEXT` change the names): another tool's file
  such as `CLAUDE.md`, `GEMINI.md` or `.cursorrules` (with where its text goes), a misnamed `agents.md`, and a `.agents` folder or
  `AGENTS.local.md` that a commit would hold. `--allow` leaves out a path the project keeps on purpose. `flows/REPO.md` states the
  standard.
- engineering: `lib/python/agent_tools.py`, the reader of the two agent-tools files (`<project>/.agents/agent-tools.toml`, the
  machine's `agent-tools.local.toml`; unknown keys refused with file and key) and the one commit-message rule: no
  `Co-Authored-By:` naming a model or an assistant, no `*-Session:` trailer, no session URL, no "Generated with" footer.
- engineering: the commands `build-slot` (heavy builds only while a slot is free, sharing its limit file and gate directory with
  the earlier scripts), `gate` (`run`, `wsl`, `merge`, `lock`: a project's steps under the shared gate lock, the Linux leg, the
  fast-forward merge that pushes through `repo-guard`), `merge` (conflict resolvers that keep both sides, and a rebase or
  cherry-pick loop that applies them by rule), `worktrees` (merged worktrees and their `target/` sizes, `--remove`) and
  `configure-pages` (the repository settings a Pages workflow cannot set for itself). `kb/AGENT_TOOLS.md` has the schema and
  each command.
- engineering: `leak-check` reads `[agents_paths]` in its private `leak-check.toml`: a path into the private agent directory is
  then a hit nothing waives, and a plain mention is allowed only in files the `allow` globs name.
- release: the command `repo-guard` (`commit`, `push`, `deps`, `ci`): commits and pushes under the configured identity with the
  commit-message rule, runs `dotagents leak-check` and the repository's `build.py scan`, refreshes and pushes a deps branch, and
  runs CI once on a throwaway tag. The overlay now requires `engineering`.
- The `Test` workflow also runs the engineering overlay's tests.

- The eight overlays `engineering`, `net`, `node`, `private-sync`, `python`, `recovery`, `release` and `rust`, as they stood on the
  `repo` branch of `jose-pr/dotagents`, which keeps their earlier history.
- `catalog.toml`, listing each overlay with its summary, icon, homepage and tags, for stores that read catalogs.
- An `[about]` table in each overlay's `overlay.cfg` (summary, icon, homepage, repository, license, tags) and an `icon.png`
  drawn by `tools/make_icons.py`. Nothing in them changes what an overlay does.
- `tools/validate.py` and the manual `Validate` workflow.

- release: `repo/tools/build.py`, a standard-library build tool a repository copies into its `tools/` and configures with
  `tools/build.cfg`; `self check` and `self update` keep the copy equal to the overlay's, and `help config` prints every
  configuration key with its default. `kb/BUILD.md` says how to adopt and update it.
- release: `build.py deps` (`sync`, `check [--quick] [--ref REF]`, `use`, `refresh`, `path`, `init`) keeps a `deps` branch of
  vendored crates equal to `Cargo.lock`, and `build.py scan` refuses archives, executables, keys, wasm and opaque binaries in
  a tree, a commit or all of history, with `[deps] allow` for the exceptions.
- release: the Flutter section of `build.py`: pub packages (`[deps.pub]`) and CDN files (`[deps.cdn]`) on the deps branch with a
  coverage check of `pubspec.lock`, `offline plan --json`, `flutter junctions` and `flutter precache-check`.
- release: `build.py sdk check` (lint an SDK lock, cover the deps pin, compare `[[sdk.agree]]` facts, `--online` publisher
  hashes) and `build.py sdk install` (rustup, toolchains, archives, git items, virtual environments, `env.sh` and `env.ps1`,
  every download verified; `--check` and `--print-system`).
- release: `build.py release` (`matrix`, `build --target [--offline]`, `universal`, `image --arch`, `verify`): per-target
  binary archives with `SHA256SUMS`, a universal macOS binary, OCI image archives, and a static-link check, all from
  `[release]` in `tools/build.cfg`.
- release: `repo/tools/public_api.py` (check or update public API snapshots, one file or one per package) and
  `repo/tools/apk.py` (forbidden strings with a planted control, permissions, ABIs, required libraries of a built APK),
  configured by `[public_api]` and `[apk]` in `tools/build.cfg`.
- A `Test` workflow running the release overlay's tests on Linux, Windows and macOS at Python 3.11 and 3.14.

### Changed

- rust: `unsafe` is allowed when proved necessary (no safe API does it, or a measured performance need), with the reason kept in
  the list of allowed files; a limitation kept only to avoid `unsafe` is a defect.

### Fixed

- release: `build.py scan` no longer flags text in a single-byte encoding (ISO-8859-1, Windows-1252, or UTF-8 holding a stray
  such byte) as an unrecognised binary. Text with no NUL, no control character but tab, LF, FF and CR, and at most 10% of its
  bytes from 0x80 up passes and is reported as `NOTE text, not UTF-8`; a private key in it is still refused, and binaries,
  random bytes and UTF-16 are still flagged.
