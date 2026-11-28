# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- engineering: `leak-check --gate` fails a scan in which a class of pattern did not run (INCOMPLETE) instead of passing it, and
  `--install-hook pre-push|pre-commit` writes a git hook that runs the gate (skipped by `--no-verify` or
  `AGENTS_LEAK_CHECK_GATE=off`, which it logs; a hook it did not write is never replaced).
- private-sync: `link --prefer-project` backs the project's `.agents` up and puts its files into a store that is the
  partial side; a refusal says whether one side is a strict subset of the other.
- engineering: `AGENTS_SCOPE_PIN=1` keeps `AGENTS_PROJECT_ROOT` when a command runs from another project's directory.

- private-sync: the turn-end hook returns at once. It records the request and starts the sync as a detached runner with a
  single-flight lock, so turns that end during a sync join its next pass instead of racing it and the hook no longer
  reports `hook-failed private-sync turn-end timeout`. The runner syncs every linked store with uncommitted or unpushed work,
  not only the project's, runs git without auto-maintenance and with a per-call timeout, retries a rejected push, leaves a
  failure the next hook reports, and logs each git step's seconds. `background`, `interval`, `git_timeout`, `stale`,
  `sweep_interval` and `maintenance_interval` are settings; maintenance (`worktree prune`, `gc --auto`) is its own step.

- engineering: the plans skill documents migrating a legacy plans, findings and decisions store and the gaps the migrate commands leave.

- engineering: the command `blob-scan <repo>` finds the blobs a malware or archive scanner may refuse (archives, encrypted data,
  executables, key files, git packs, opaque binaries) in every ref of any repository, with nothing but git. `skills/blob-scan/SKILL.md`
  says how it differs from the shared `build.py scan`.
- engineering: the command `leak-check-history ROOT` scans the whole history (blobs, messages, paths, identities) of every
  repository under ROOT with `leak-check`'s pattern file, and labels a hit in someone else's commit `UPSTREAM`. The owner's
  identities are the pattern file's optional `ours` list, else `git config`.
- release: `flutter/` and `kb/FLUTTER.md`: the tools that mirror Flutter's repository and engine artifacts, install an SDK from the
  mirrors and build its container images (`flutter-mirror`, `flutter-artifacts`, `flutter-sdk`).
- engineering: the command `context-check` warns about agent context files outside the standard places (`AGENTS.md` and
  `AGENTS.*.md` in a directory and in its `.agents`; `AGENTS_DIR` and `AGENTS_CONTEXT` change the names): another tool's file
  such as `CLAUDE.md`, `GEMINI.md` or `.cursorrules` (with where its text goes), a misnamed `agents.md`, and a `.agents` folder or
  `AGENTS.local.md` that a commit would hold. `--allow` leaves out a path the project keeps on purpose. `flows/REPO.md` states the
  standard.
- engineering: `records` can keep a folder's records in GitHub issues or pull requests instead of Markdown files
  (`.agents/records.toml`, `provider = "github"`): the issue number is the record's name, a `status:<status>` label its status,
  and the other frontmatter fields a block at the top of the issue's text. It runs through the person's own `gh` login.
  GitLab issues and merge requests (`provider = "gitlab"`, through `glab api`) and Jira issues (`provider = "jira"`, REST, the
  token from the environment variable `token_env` names) work the same way.
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

- private-sync: a store's default ignore rules, and a new project branch's, deny everything except the file types they name
  (`AGENTS_STORE_IGNORE=allow-all` writes the earlier rules and is logged). A store that already has the earlier block keeps
  it and each sync prints one line saying so.
- engineering: a command run from inside another project's directory uses that project, not `AGENTS_PROJECT_ROOT`.

- rust: `unsafe` is allowed when proved necessary (no safe API does it, or a measured performance need), with the reason kept in
  the list of allowed files; a limitation kept only to avoid `unsafe` is a defect.

### Fixed

- engineering: `plans next` on a plan whose only open phase is blocked says so, instead of calling every phase checked.
- private-sync: adopting a `.agents` into a store copies into a sibling directory and renames it into place, and skips a
  symlink that points nowhere with a warning, so a failure no longer leaves a partial store.
- release: `build.py scan` no longer flags text in a single-byte encoding (ISO-8859-1, Windows-1252, or UTF-8 holding a stray
  such byte) as an unrecognised binary. Text with no NUL, no control character but tab, LF, FF and CR, and at most 10% of its
  bytes from 0x80 up passes and is reported as `NOTE text, not UTF-8`; a private key in it is still refused, and binaries,
  random bytes and UTF-16 are still flagged.
