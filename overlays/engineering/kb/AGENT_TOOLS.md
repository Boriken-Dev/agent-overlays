# Agent tools: build slots, gates, merges, worktrees, Pages

Commands the engineering overlay adds for the work an agent does around a build: waiting for a free build slot, running
the checks a branch passes before it is merged, resolving merge conflicts without losing text, cleaning up worktrees
and configuring GitHub Pages. `repo-guard`, which gets commits out, is the release overlay's
(`$RELEASE_OVERLAY_ROOT/kb/RELEASE.md`) and reads the same configuration.

Every command is a `dotagents NAME`, runs under dotagents' own Python and uses the standard library plus `duho`.

## Configuration

Two TOML files, each command reading only its tables. The overlay holds no default that names a project, a path, a
host or a person: a required key the files leave out is an error naming the file and the key, and a key the schema does
not know is refused with its path, so a misspelling is never silently ignored.

| File | Holds | Private to |
| --- | --- | --- |
| `<project>/.agents/agent-tools.toml` | `[repo_guard]` `[gate]` `[merge]` `[worktrees]` | the project |
| `~/.agents/agent-tools.local.toml` | `[identity]` `[build_slot]` `[gate]` `[wsl]` | this machine and person |

`$AGENT_TOOLS_LOCAL` names the machine file instead; otherwise it is `$AGENTS_HOME/agent-tools.local.toml`. Neither file
is ever committed. A table may appear in both files only with different keys (`[gate]` does: `lock` is the machine's,
the steps are the project's).

### Project file

```toml
[repo_guard]
main = "main"                 # default main
remote = "origin"             # default origin
checkout = "../checkout"      # a checkout of main to fast-forward after a push (path relative to the repository)
log = "deps_refreshes.md"     # one line appended per push of the deps branch
pre_push = [["cargo", "fmt", "--check"], "python tools/check.py"]   # extra commands; a string is split like a shell line
forbidden = ["^wip:"]         # extra regular expressions no commit message may match (one per line, case-insensitive)
ci_timeout = 3600             # seconds `repo-guard ci` waits for the run
ci_find_timeout = 120         # seconds it waits for the host to start the run

[gate]
restore = ["app/pubspec.lock"]            # checked out again after the steps
steps = [
  { name = "fmt",  run = "cargo fmt --all --check" },
  { name = "test", run = ["cargo", "test", "--workspace"], slot = true, env = { RUST_LOG = "warn" } },
  { name = "web",  run = "flutter build web", cwd = "app", when = ["app/**", "Cargo.lock"], slot = true },
]
wsl_steps = [
  { name = "clippy", run = "cargo clippy --workspace -- -D warnings" },
]

[merge]
rules = [
  { path = "CHANGELOG.md", how = "union" },
  { path = "Cargo.lock", how = "ours-then-run", run = "cargo metadata --format-version 1" },
]

[worktrees]
root = "**/wt/*"     # limit `worktrees` to worktrees whose path matches
```

A step is `name`, `run`, and optionally `when` (globs), `env`, `cwd` (relative to the worktree) and `slot` (default
false). `run` is a list (an argv) or a string split like a shell command line, so write paths with forward slashes and
use `sh -c '...'` for a pipeline; in `wsl_steps` a string is a bash line, run as is. `{worktree}` in `run` or `env` is
replaced by the worktree's path (in `wsl_steps`, by the unpacked clone's). Globs: `**` crosses directories, `*` and `?`
do not, a trailing `/` means everything below it.

### Machine file

```toml
[identity]            # repo-guard commit and push; required by them
name = "A Name"
email = "name@example.org"

[build_slot]          # all optional
limit = 8                                   # default 8, unless the limit file says otherwise
processes = ["cargo", "rustc", "flutter_tester", "dart build", "dart test", "dart compile"]   # the defaults
wsl = ["MyDistro"]                          # also count builds inside these WSL distributions (Windows)
dir = "$LOCALAPPDATA/build_slot"            # the state directory (default: that, else ~/build_slot)

[gate]
lock = "$LOCALAPPDATA/agent_gate.lock"      # the lock whole-workspace runs share ($VAR and ~ expand)

[wsl]                 # the Linux leg of `gate`
distro = "MyDistro"                         # required unless `exec` is set
home = "~"                                  # where the clone and the target live in the distribution
target_max_gb = 40                          # the target is cleared (then fstrim) past this size
jobs = 4                                    # CARGO_BUILD_JOBS
exec = ["wsl.exe", "-d", "MyDistro", "-u", "builder", "--"]   # instead of the default wsl.exe -d DISTRO --: the argv that runs a command
```

## Commands

### `dotagents build-slot [--limit N] [--max-wait S] [--every S] [--settle S] -- CMD...`

Runs a heavy build (a running cargo, rustc, Flutter test, Dart build or test) only while fewer than the limit run on
this machine. With no command it only waits for a slot.

- The limit is `--limit`, else the one integer in `<dir>/limit` (read on every check, so editing it reaches callers
  already waiting), else `[build_slot] limit`, else 8.
- Starts go through one gate (`<dir>/gate/pid`), held from the check until the new build shows in the process count or
  `--settle` seconds (default 20) pass, so two callers never take the same free slot. A gate whose holder exited is taken
  over. Nothing is ever stopped.
- The command's exit code is returned; only its output goes to stdout. Exit 1: no slot within `--max-wait` (default
  7200), with who holds the slots; 127: command not found.
- The state files are those of earlier `build_slot.py` scripts, so old and new callers share one limit and one gate and
  may run at the same time.

### `dotagents gate run WORKTREE [--step NAME]... [--out DIR] [--fail-fast]`

Runs `[gate] steps`. Refuses a tree in the middle of a rebase, merge or cherry-pick or with unmerged paths. Takes the
gate lock (`[gate] lock`, a `mkdir` lock with a `holder` file inside; it waits, naming the holder), runs each step through
`summarize-run` (and `build-slot` when `slot = true`), prints `PASS name`, `FAIL name` or `SKIP name`, checks the
`restore` files out again and releases the lock. A step with `when` runs only when a changed path against `REMOTE/MAIN`
matches one of its globs (every step runs when that ref does not exist). Each step's log and verdict are in `--out`
(default `gate_NAME` under the temporary directory). Exit 1 when a step failed.

### `dotagents gate wsl WORKTREE [--out DIR]`

Runs `[gate] wsl_steps` in a Linux distribution on a `git archive` of HEAD, under the same lock: the archive is unpacked
into `[wsl] home`/`gate_NAME`, each step runs there with `CARGO_TARGET_DIR` set beside it and `CARGO_BUILD_JOBS` from
`[wsl] jobs`, and `PASS wsl:name` or `FAIL wsl:name` is printed. The target is kept between runs and cleared (then
`fstrim` as root) once it passes `[wsl] target_max_gb`; the clone is removed.

### `dotagents gate merge BRANCH [-C DIR] [--no-push] [--out DIR]`

In a repository on its main branch: refuses a branch that is not a fast-forward of main or whose commits carry agent
attribution, fast-forwards main, runs `run` and `wsl` under one lock and, only if all passed, pushes through
`repo-guard push`. After a failed check main stays advanced locally and the output names the `git reset --hard` that
undoes it.

### `dotagents gate lock LABEL -- CMD...`

Runs a command while holding the gate lock, waiting for it like the other gates.

### `dotagents merge ...`

| Command | Does |
| --- | --- |
| `union FILE...` | Resolves every conflict by keeping HEAD's side, then the incoming lines it lacks; stray Latin-1 bytes are read as such and the file written as UTF-8; a line without a letter or digit (a brace, a blank) is structure and never dropped |
| `show FILE` | Prints, for each conflict, the word-level differences between its sides |
| `pub-lock OURS THEIRS OUT` | Merges two `pubspec.lock` files package by package |
| `dupes FILE...` | Lists lines that start like a nearby line (what a union leaves when both sides of a changed paragraph were kept); exit 1 if any |
| `fix-utf8 FILE...` | Turns bytes that are not UTF-8 into the Latin-1 characters they were |
| `rows FILE` | Merges Markdown table rows keyed by the first cell |
| `both-blocks FILE` | Both sides appended a different block before the same closing brace: keeps both |
| `continue [-C DIR] [--rounds 40]` | Continues a rebase or cherry-pick, resolving each conflicted path by the first `[merge] rules` entry whose `path` glob matches |

A rule's `how` is `union`, `rows`, `both-blocks`, `pub-lock`, or `ours-then-run` (take HEAD's copy, then run `run` to
regenerate it: a lock file). `continue` stops, naming the path and staging nothing, on a path no rule matches or when a
resolved file would still hold a marker. Exit 0 finished, 1 a path needs a person, 2 the round limit. Every resolver
refuses to leave a conflict marker.

### `dotagents worktrees [-C DIR] [--root GLOB] [--remove]`

Lists each linked worktree as `merged` (all its patches are in `REMOTE/MAIN`, by `git cherry`) or `open`, with the size
of its `target/`. `--remove` removes the merged ones with `git worktree remove` and skips one with uncommitted changes.

### `dotagents configure-pages OWNER/REPO [--tag-pattern v*] [--branch B] [--dry-run]`

Applies the three repository settings a Pages workflow cannot set for itself (default workflow permissions when a Pages
job relies on them, Pages building from Actions, and the `github-pages` environment allowing the release tag pattern and
the default branch). Needs `gh`, authenticated, with admin rights. Failures are listed at the end, exit 1.

### `repo-guard` (release overlay)

`repo-guard commit|push|deps|ci`: commit and push under the configured identity with the commit-message rule, refresh a
deps branch, run CI once. See `$RELEASE_OVERLAY_ROOT/kb/RELEASE.md`.

## The commit-message rule

One rule, used by `repo-guard` and `gate merge` (`lib/python/agent_tools.py`): a commit message may not carry a
`Co-Authored-By:` line naming a model or an assistant, any `*-Session:` trailer, a session URL, or a "Generated with"
footer (with or without the robot emoji). A co-author line naming a person is ordinary and passes. `[repo_guard]
forbidden` adds patterns.

## `leak-check`: the private store

A `[agents_paths]` table in the repository's private `leak-check.toml` makes `dotagents leak-check` strict about the
private agent directory: a path into it (its plans, findings, decisions, kb, tools, references, scratch and the like)
is a `private store path` hit in every file and nothing waives it; a plain mention of the directory is still a hit,
except in files matching `allow` globs, for a tool that keeps its own files in a directory of that name.

```toml
[agents_paths]
allow = ["docs/hooks.md", "crates/providers/**"]
```
