# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

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
- A `Test` workflow running the release overlay's tests on Linux, Windows and macOS at Python 3.11 and 3.14.

### Changed

- rust: `unsafe` is allowed when proved necessary (no safe API does it, or a measured performance need), with the reason kept in
  the list of allowed files; a limitation kept only to avoid `unsafe` is a defect.
