# Private agents sync — one private repo, per-user + per-project

Keep your global config **and** every project's private agent state in a single
private git repo, synced across machines and cloud sessions, without ever committing
any of it into the (often public) project repos.

## The model

Your global `~/.agents` **is** a private git repo. Its root is the per-user config
(`AGENTS.md`, the installed `overlays/` or a registry naming them, the user-scope
findings queue). A new `projects/` tree holds each project's private `.agents` payload:

```
~/.agents/                       # = clone of your private repo
├── AGENTS.md                     # per-user config (dotagents-managed block + your edits)
├── dotagents.toml                # (registry layout) one source per overlay
├── overlays/                     # your installed global overlays (tracked, or not)
├── findings/                     # the user-scope findings queue, private and synced
└── projects/                     # per-project private .agents payloads
    ├── <project-a>/              # the project's private files
    └── <project-b>/ ...
```

### Two store layouts

- **Branch layout (preferred):** each project's store is a **branch** of the
  private repo, named after the project, holding the payload at its root (with
  its own `.gitignore`), and `<project>/.agents` is a **git worktree** of
  `~/.agents` on that branch. `main` keeps only the per-user config. Nothing to
  symlink, works the same on Windows, and each project's history is its own.
  `link-project` checks the branch out (the local one, else `origin/<name>`,
  else a new orphan branch) and adopts a real `.agents/` onto it;
  `sync-project --project` commits and pushes that branch. It is chosen when
  `~/.agents` is a git repo with no `projects/<name>` directory, or always with
  `AGENTS_STORE_LAYOUT=branch` (`dir` forces the layout below).
- **Directory layout:** stores under `projects/<name>` on `main`, linked as below.

The branch need not live in `~/.agents`. `link-project --store-repo` puts it on
another repository: `self` for a branch of the project's own repo (`agents` by
default, so the project's remote carries its private state on a branch nobody
merges), a local git repo, or any git URL, a team account's repo for instance
(cloned into `<project>/.agents`). `--store-branch` names the branch (default:
the project's name). The choice is recorded in `~/.agents/dotagents/projects.toml`,
which syncs with the config, so `link-project` with no flags on another machine,
or in a cloud session, links the project the same way.

In the directory layout, for each checked-out project, `<project>/.agents` is a
**symlink** → `~/.agents/projects/<name>`. The project repo's own `.gitignore` already excludes
`.agents/` (the global Leakage rule), so the symlink is never committed to the project
— but everything behind it lives in, and syncs through, the one private repo.

`<name>` defaults to the project directory's basename, so a checkout at
`~/code/app` and a cloud checkout at `/home/user/app` both resolve to
`projects/app`. Override with `--name` when two different repos share a basename.

### Two ways to keep the overlays

- **Track the copies.** `overlays/` is committed like everything else. A clone,
  a second machine or a cloud session has them at once, offline. The cost: every
  `overlays sync` is a diff in the private repo, and the repo carries a second
  copy of upstream's files.
- **Name them in a registry.** `~/.agents/dotagents.toml` names one source per
  overlay (`<name> = "<repo>@<ref>#<path>"`), and `overlays/` is left out of the
  repo (an `overlays/` line in `.gitignore`). `dotagents overlays add '*' -g`
  installs every overlay the registry names, and `overlays sync -g` refreshes
  them. The repo stays small and its history is yours alone. The cost: each new
  machine and each cloud session fetches them, so it needs the network once;
  `tools/cloud-setup.sh` runs that `add` for you.

`add '*'` keeps going when one overlay fails: the others are installed (except
what requires the failed one), each failure is logged, and it exits non-zero.

Everything below is **one way to use `private-sync link` and `private-sync sync`, not
what the agent tooling requires**. Two knobs make that concrete: `--store-dir` /
`$AGENTS_STORE_DIR` moves the stores anywhere (relative to the agents dir, or an
absolute path outside it), and a `hooks/sync` script replaces git entirely. Both
`link` and `sync` are optional subcommands — `init`/`install` never
touch a project directory.

## Commands

- `private-sync link [PATH]` — symlink `PATH/.agents` (default: current dir) to its
  store. An existing real `.agents/` is **adopted** into an empty store on the first
  link (its content moves into the private repo, then the symlink replaces it), so you
  can run it in a project that already has `.agents/`. Re-running is idempotent.
  - `--copy` mirrors the store as a real directory instead of symlinking (Windows or
    any no-symlink environment; a symlink failure auto-falls back to this).
  - `--force` backs up a conflicting real `.agents/` (both project and store carry
    content) or replaces a stale symlink. A `.agents` that is itself a git checkout is
    backed up whole, `.git` included, to `.agents.bak`, and never adopted.
  - `--store-repo self|<path>|<url>` and `--store-branch <name>`: keep the store on a
    branch of another repository (see *Two store layouts*); recorded per project.
  - `--agents-dir` defaults to `$AGENTS_HOME`, else `~/.agents` (also for
    `sync-project`).
- `private-sync sync` — reconcile a copy-mode project, then move the store.
  - `--project PATH` first copies a **copy-mode** project's `.agents` back into its
    store (symlinked projects need no copy-back — their `.agents` *is* the store). A
    branch-layout store, on `~/.agents` or on the repository `--store-repo` recorded,
    is committed and pushed on its own branch to that repository's `origin`.
    This repairs the divergence the `--copy` fallback creates.
  - **Transport** is then handed to `<agents-dir>/hooks/sync` if that exists — it
    gets the store path as `$1` and the message as `$2` (plus `AGENTS_HOME`
    / `AGENTS_SYNC_MESSAGE`), and its exit code is returned. Use it for rsync, a
    cloud drive, or nothing at all.
  - With no hook, a built-in git path runs as a convenience: `git pull --rebase` /
    commit / push. `--remote <url>` bootstraps a fresh repo (`git init` + set
    `origin`) in one command; `--no-pull` / `--no-push` apply to this path only.
  - `-m <msg>` / `--dry-run` as expected (`--dry-run` never executes the hook).

## What the store's repository leaves out

A linked project's `.agents/scratch/` is `~/.agents/projects/<name>/scratch/`, inside
the private repo, and the built-in git path commits everything it is not told to
ignore. So the first `private-sync sync` on that path appends a block of default
rules to `~/.agents/.gitignore` (creating the file if there is none):

```
# dotagents private-sync: defaults for a store kept in git
scratch/
tmp/
__pycache__/
.*
!.gitignore
!.gitattributes
!.gitkeep
!.ignore
!.dotagents-install.json
*.local.*
local.env
```

- `scratch/` and `tmp/` are throwaway directories, at the store's root and in every
  project store.
- `.*` leaves out hidden files and directories (tool caches, the overlay source
  cache). The `!` lines keep the dot-files a store and its installed overlays need.
- `*.local.*` and `local.env` are the machine-local overrides (`AGENTS.local.md`, a
  project's `local.env`), which are never shared.

The block is written **once**: a `.gitignore` that already has its first line is left
alone. Delete a rule from it, or add a `!` exception below it, to opt out of one;
keep the first line so the block is not written again. Rules you already had are
kept, above the block. A rule only affects files git does not track yet: `git rm
--cached` one that was committed before. A `hooks/sync` script owns transport and
gets no block.

## First-time setup

```bash
# 1. Lay down the per-user base into ~/.agents, then the overlays you want (from a
#    checkout of the dotagents `repo` branch, or $AGENTS_OVERLAYS_REPO).
dotagents init -g
dotagents overlays add engineering private-sync --repo <overlays-checkout>/overlays -g
# 2. Make ~/.agents a git repo pointing at your private remote, and push.
private-sync sync --remote git@github.com:<you>/.agents.git -m "init: agents repo"
git -C ~/.agents push -u origin HEAD
# 3. In any project, link its .agents into the private repo, then sync.
cd ~/code/some-project
private-sync link .
private-sync sync -m "link some-project"
```

On a second machine: `git clone git@github.com:<you>/.agents.git ~/.agents`,
`dotagents init -g`, then (registry layout) `dotagents overlays add '*' -g`, and
`private-sync link .` inside each project you check out.

## Cloud sessions

Cloud containers start from a fresh clone with no `~/.agents`. Two overlay hooks (declared
in this overlay's `overlay.cfg`, scripts in its `hooks/`) make a cloud session identical to local:

- `session-start` runs `private-sync-start.sh`: clones the private repo to `~/.agents` on first
  run (or `git pull --rebase` if present), then `private-sync link`s the project.
- `turn-end` runs `private-sync-stop.sh`: records the request and starts the sync detached, then
  returns in well under a second (see *Background sync*).

**Background sync.** A turn can end every minute and several sessions share one store, while a
pull, commit and push of a large store is several network round trips (2 to 14 s each on a slow
link, so tens of seconds idle and minutes under load). A fixed hook deadline cannot hold that, so
`turn-end` only writes `<agents>/.cache/private-sync/pending/<project>.json` and starts a detached
runner (`hooks/run_link.py runner`, `lib/python/_bgsync.py`) unless one is already running:

- **One runner per store.** `sync.lock` is single-flight and refreshed at every git step; a request
  made while a runner works is merged into its next pass (requests for one project are one file).
  A lock with no progress for `stale` seconds is taken over.
- **Every linked store with work.** A pass syncs the named project, then sweeps the other linked
  store worktrees of `~/.agents` and syncs those with uncommitted or unpushed work (one local
  `git status` each), so a store a session wrote to without naming its project is not left behind.
- **No auto-maintenance in the sync.** Every git call runs with `gc.auto=0` and
  `maintenance.auto=false`, ends at `git_timeout` (the whole process tree), and never waits on a
  terminal. A rejected push (another process pushed first) is rebased and retried twice.
  Maintenance (`git worktree prune`, which forgets metadata for a vanished directory and never
  deletes one, then `git gc --auto`) is its own step, asked for at session start at most every
  `maintenance_interval` and run by the runner.
- **Failure is reported next turn.** A pass that hit a `WARN`, a timeout or an exception writes
  `failure.json`; the next `turn-end` or `session-start` says so once on standard error and
  the next good pass removes it. `sync.log` (bounded, one rotation, no credentials) holds each
  git step's seconds: read it to see which step was slow.
- **Windows.** The harness runs a hook in a job object that ends everything the hook started, even
  with breakaway, so the runner is started through the WMI service (about 0.4 s). That route cannot
  carry `DOTAGENTS_AGENTS_TOKEN` (a credential is never written to a file), so a run with a token
  syncs before the hook returns; cloud containers are Linux and detach normally.

Settings, each `PRIVATE_SYNC_<NAME>` in the environment (a hook's environment may not carry the
shell's), else `name = value` in `<agents>/private-sync.cfg`, else the default. An out-of-range or
non-numeric value is ignored.

| name | default | meaning |
| --- | --- | --- |
| `background` | 1 | 0: the hook syncs before it returns (a cloud container that ends with the turn) |
| `interval` | 30 | seconds between passes; requests made sooner wait and merge |
| `git_timeout` | 120 | seconds one git call may run |
| `stale` | 900 | seconds without progress before a lock is taken over |
| `sweep_interval` | 300 | seconds between sweeps of the other stores; 0 never |
| `maintenance_interval` | 86400 | seconds between maintenance runs; 0 never |

The hook's own `timeout` in `overlay.cfg` now bounds only the fallback that syncs in the hook; the
user's `timeout` setting of dotagents (`hooks-settings.json`) still overrides it.

They are AOS hooks, so they are **off until you allow them at the overlay's digest**, and
`dotagents init -g` wires the harness's own hooks to run them (Claude Code's `SessionStart` and
`Stop`, Codex's the same; a harness without native hooks cannot fire them, and `dotagents overlays
hooks private-sync` says so per harness):

```
dotagents overlays hooks private-sync               # what runs, where, and whether it is allowed
dotagents overlays allow-hooks private-sync         # shows the same and the digest to allow
dotagents overlays allow-hooks private-sync --digest sha256:<the digest shown>
dotagents init -g                                   # wires Claude Code's and Codex's own hooks
```

Updating the overlay changes its digest and turns the hooks off until you allow the new one.
`tools/cloud-setup.sh` (below) does these steps itself at container start, since it is your own
setup script. The hooks act on the project the harness names, `CLAUDE_PROJECT_DIR` or
`AGENTS_PROJECT_ROOT` (the latter is set when the session was started by `dotagents launch`); with
neither they do nothing and say so, because the payload's `cwd` is wherever the agent was and a
walk up from it could adopt another project. Run by hand, the scripts use the current directory.
They run the overlay's own `hooks/run_link.py` by path with an isolated Python, not the
`private-sync` script, which would load whatever the project's own `.agents` holds.

**Fresh cloud containers (first clone).** The `session-start` hook lives in the
installed overlay under `~/.agents/`, which doesn't exist until `~/.agents` is first cloned — so the
initial clone can't come from the hook. Use `tools/cloud-setup.sh` from the **public**
dotagents repo: a self-contained bootstrap (inlines its own token auth + rewrite bypass,
so it runs before `~/.agents` exists) that clones-or-pulls the repo, installs the CLI,
links the project, and wires and allows the hooks. Put a one-line
fetch-and-run in your web environment's **setup-script** field so it stays current
without re-pasting:

```
curl -fsSL https://raw.githubusercontent.com/<you>/dotagents/main/tools/cloud-setup.sh -o /tmp/dg-cloud-setup.sh && sh /tmp/dg-cloud-setup.sh
```

(Use `curl … -o file && sh file`, not `curl … | sh`: a pipe makes the field's exit code
`sh`'s, so a failed fetch is silently reported as success; `&&` surfaces it. Pin to a tag —
`.../dotagents/v0.2.0/tools/cloud-setup.sh` — for reproducibility.) The
per-session hooks then handle pulls and sync-back once `~/.agents` is present.

The setup-script clone runs early and can lose a race with egress/proxy readiness.
`cloud-setup.sh` retries the clone with backoff; if it still fails it persists a copy of
itself to `~/.dotagents/cloud-setup.sh` and wires a SessionStart **recovery hook** that
re-runs the bootstrap next session and then removes itself.

Auth for the clone/push comes from the environment, never a committed file:

- `AGENTS_REMOTE` — git URL of your private repo. Prefer a **tokenless** HTTPS
  URL (`https://github.com/<you>/.agents.git`) paired with `DOTAGENTS_AGENTS_TOKEN`.
- `DOTAGENTS_AGENTS_TOKEN` — **(recommended)** a fine-grained PAT scoped to just this
  repo (Contents: read/write). The hooks wire it via a git credential helper that reads
  it from the environment at auth time, so the secret is never written to `.git/config`
  or any file on disk. A token embedded directly in `AGENTS_REMOTE` also works
  but is then persisted in `.git/config` — avoid it.
- `AGENTS_HOME` — where the repo lives (default `$HOME/.agents`).

A dot-named private repo needs the PAT: a hosted runner's per-session, repo-scoped
credential cannot cover a repo whose name starts with `.` (a non-dot name can use the
runner's built-in auth with no token). Store the token as an environment secret; the
cloud network policy must allow `github.com`.

Runners often rewrite all `github.com` git traffic (a global `insteadOf`) to a scoped
proxy that won't serve a repo outside session scope. `hooks/_agents-git-auth.sh`
(sourced by both hooks) detects that rewrite and, when a token is present, points git
at an isolated config that bypasses it. Without a rewrite it offers the token only for
`$AGENTS_REMOTE` (else github.com) through git's `GIT_CONFIG_*` environment, and never
writes `~/.gitconfig`. Nothing to configure beyond `DOTAGENTS_AGENTS_TOKEN`.

## Gotchas

- **`.gitignore` must exclude `.agents/`** in every project, or the symlink/copy can be
  committed to the (public) project. `private-sync link` warns when it's missing; the base
  overlay's Leakage rule already requires it.
- **Copy mode is two-way**: run `private-sync sync --project .` to push local edits back
  *before* re-running `private-sync link --copy` to pull global changes, or a refresh can
  overwrite unsynced local edits. Symlink mode has no such hazard — prefer it.
- **A `.agents.bak` dir** appears when `--force` backs up a conflicting real `.agents/`;
  reconcile and delete it.
- **The store is the source of truth** once linked: edit `<project>/.agents/...`
  transparently through the symlink; those writes land in `~/.agents/projects/<name>/`
  and sync with `private-sync sync`.
