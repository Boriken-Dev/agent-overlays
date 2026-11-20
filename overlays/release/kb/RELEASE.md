# Release flow (host-agnostic)

`tools/release.py` automates a release: an agent-driven **commit-plan loop**,
then tag + **CI monitoring** that works the same on GitHub and GitLab. The
VCS-host surface (remote parse, CI monitor, push) lives in `tools/hosts.py`
behind a `ReleaseHost` base with `GitHubHost` / `GitLabHost` impls; everything
else in `release.py` is host-agnostic.

## Commit-plan loop (host-agnostic)

The script never invents commits. You (the agent) decide commit boundaries:

1. **Emit context** — `release.py --emit-plan-context` prints, as YAML (or JSON
   if PyYAML is absent): `status --porcelain`, `diff --name-status`, `--stat`,
   per-file patch excerpts, and the highest-priority `RELEASE.md` found
   (`RELEASE.md` > `.agents/RELEASE.md` > overlay `kb/RELEASE.md`).
2. **Write a plan** — produce a YAML/JSON commit plan:
   ```yaml
   version: 1
   commits:
     - message: "feat: ..."
       add: [{ path: "src/x.py" }]        # whole-file stage
     - message: "docs: ..."
       patches: [{ path: "README.md", apply: "<unified diff>" }]  # partial stage
   ```
   Split logical commits (feature+tests / docs / CI) — never one omnibus.
   **No agent attribution** in messages (no `Co-Authored-By:` naming a model, no
   session trailer/URL). `type: desc` format.
3. **Apply** — `release.py --commit-plan plan.yml` (add `-y` to skip the
   confirm). Requires a clean index; applies each commit via `git reset` +
   `git add`/`git apply --cached` + `git commit -m`.

## Tag + push (v* consent hard-stop)

`release.py <major|minor|patch|x.y.z>` bumps from the latest `v*` tag, runs
`validate_repository` (README/CHANGELOG/kb checks; `--force` overrides), and
creates an annotated `v<version>` tag.

**A v* tag is NOT auto-pushed.** Pushing it is irreversible, so the tool
hard-stops (exit 3) and hands back for explicit per-release consent:

```
git push && git push origin v<version>   # do it yourself, or
release.py <version> --push               # push in this run (explicit consent)
```

`ci-*` tags would be safe to auto-push, but this tool only mints `v*` tags.

## CI monitoring (uniform across hosts)

After a push, unless `--skip-ci` (alias `--skip-pipeline`), the tool monitors CI
for the tag and prints a one-line verdict. The host is auto-detected from
`origin`:

- **GitHub** (`github.com`, GHE, any `*github*` host): via the `gh` CLI —
  `gh run list --branch <ref> --json ...`, then `gh run watch` to completion.
  `gh` handles auth. If `gh` is absent → clean `status: unknown`,
  "gh CLI not found", no crash.
- **GitLab** (`gitlab.com`, self-hosted `*gitlab*`): shells to an external
  `gitlabq` tool (resolved via `$GITLABQ` or `PATH`) running `pipeline-monitor`.
  If `gitlabq` is not installed → clean `status: unknown`, "GitLab CI monitoring
  unavailable", no crash. `gitlabq` is an optional external tool, not vendored.
- **Unknown host** → `status: unknown`, never crashes.

All hosts return the same shape:
`{status: success|failed|unknown|error, message: "...", ...extras}`.

## repo-guard

`repo-guard` gets commits out of a repository under one identity, with one rule for commit messages. It reads the engineering
overlay's configuration (`$ENGINEERING_OVERLAY_ROOT/kb/AGENT_TOOLS.md`): `[identity]` in the machine file, `[repo_guard]` in the
project's.

```text
repo-guard commit [-C DIR] -m MSG [-a] [PATH...]
repo-guard push   [-C DIR] [--no-deps]
repo-guard deps   [-C DIR]
repo-guard ci     [-C DIR] [--ref REF] [--timeout S] [--find-timeout S] [--every S] TOPIC
```

- `commit` refuses a message carrying agent attribution (a `Co-Authored-By:` naming a model or an assistant, a `*-Session:`
  trailer, a session URL, a "Generated with" footer; a person's co-author line is fine) and commits as the identity.
- `push` checks every commit above `REMOTE/MAIN` (author and committer are the identity, no forbidden trailer), that HEAD
  fast-forwards `REMOTE/MAIN`, `dotagents leak-check`, the repository's `tools/build.py scan --all-refs` and each `pre_push`
  command; then pushes `HEAD:MAIN` and fast-forwards `[repo_guard] checkout` when set. When `tools/build.cfg` has a `[deps]`
  table and a lock file it names changed, `deps` follows. With nothing to push it still runs the checks.
- `deps` runs `tools/build.py deps refresh`, scans the deps checkout, pushes the deps branch and then main, and appends a line to
  `[repo_guard] log`.
- `ci` tags `REF` (default main) as `ci-TOPIC-<UTC yyyymmddHHMMSS>`, pushes it, waits for the run through the release host,
  prints each job's conclusion and the failed steps' last lines, and deletes the tag locally and on the remote even when the run
  fails. Exit 0 only when the run succeeded.

Exit codes: 0 ok, 1 refused or failed, 2 configuration or usage.

## Notes

- Perf/CI claims in release notes come from CI, not local runs. The monitor
  output is factual signal — quote it, don't editorialize.
- Python 3.9 floor; stdlib + `subprocess` only (plus optional PyYAML for plan
  I/O). `gh`/`gitlabq` are invoked as subprocesses.
