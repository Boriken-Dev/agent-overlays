---
name: gate
description: Run a project pre-merge verification gate (`run`, `wsl`, `merge`, `lock`) under the machine gate lock.
---

# gate

Use `gate` (`skills/gate/gate.py`) to execute configured pre-merge steps on the host and in WSL under the shared gate lock.

## Running

```bash
$AGENTS_PYTHON -I $ENGINEERING_OVERLAY_ROOT/skills/gate/gate.py --help
```

When the overlay's `[scripts]` launcher is on `PATH`, invoke `gate` directly.

## Working with the lock

- **Holder names.** `gate run WORKTREE` writes the worktree directory's name into the lock's `holder` file,
  `gate wsl` writes `NAME-wsl`, and `gate merge` writes `REPO-merge`. Another session coordinating turns can wait for
  one of these names.
- **Separate takes.** `gate run` and `gate wsl` take the lock one after the other, so another caller can take it in
  between. `gate merge` runs both under one lock.
- **Holder pid.** The holder's pid belongs to the process that took the lock. A gate started from Python records a
  Windows pid. A shell script under Git Bash records an MSYS pid, which `tasklist` and `Get-Process` do not show.
  Look it up with Git Bash's `ps -ef` before calling a lock stale. Never remove a lock you do not hold.
- **Waiting in the background.** `gate` waits in the foreground and nothing tells a caller when a detached gate
  ends. An agent that starts one in the background arms something that wakes it: the task's own completion notice,
  or a watcher that stays silent until the lock is taken or a `PASS`/`FAIL` line appears. A watcher that reports
  "still queued" every few seconds wastes the caller's turns.
- **Rerunning one step.** `gate run WORKTREE --step NAME` reruns just that step, for example the tests after a known
  flake.
- **Rust test steps.** `cargo test` stops at the first test binary that fails, so one known flake hides every later
  binary and forces a whole rerun. Use `cargo test --workspace --no-fail-fast` in a project's `[gate]` test step,
  then rerun only the binary that flaked.
- **Copying the configuration.** A fresh worktree has no `.agents/`, so copy `.agents/agent-tools.toml` into it
  before gating. Copy it again after the project's file changes, because a running gate reads the copy it started
  with.

## Affected tests

A branch's gate runs only the tests it can change. Give a cargo step `affected = true` in `[gate]` and `gate run` and
`gate wsl` test only the workspace packages whose files changed against REMOTE/MAIN plus every package that depends on
them, transitively: `--workspace` becomes `-p NAME` for each. The step prints the package list and which file or
dependency put each one there, and is `SKIP` when none is reached. A change to `Cargo.toml`, `Cargo.lock`, the toolchain
files, `.cargo/` or a path in the step's `full_when` runs the whole workspace.

- **Slow tests.** `slow = [{ package = "x", skip = ["live_", "load_"] }]` on a `cargo test` step passes each filter
  as `--skip` after `--` and so leaves those tests out unless package `x` itself changed (not only a dependency of it).
  Name long live, load and timing tests there.
- **Full run.** `gate run --full`, `gate wsl --full` and `gate merge --full` ignore `affected` and `slow`. A full run
  happens once per merge batch, deploy or release, not per branch.
- **A script's list.** `gate affected WORKTREE [--step NAME] --args` prints `-p a -p b` (`--workspace` for all, nothing
  for none) for a manual run on another host; without `--args` it prints the reasons.
