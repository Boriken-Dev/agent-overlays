"""`dotagents gate` -- the checks a branch passes before it is merged.

    dotagents gate run   WORKTREE [--step NAME]... [--out DIR] [--fail-fast]
    dotagents gate wsl   WORKTREE [--out DIR]
    dotagents gate merge BRANCH [-C DIR] [--no-push] [--out DIR]
    dotagents gate lock  LABEL -- CMD...

`run` refuses a tree in the middle of a rebase, a merge or a cherry-pick, or with unmerged paths. It takes the
machine-wide gate lock (waiting for it), then runs each step of `[gate] steps` through `summarize-run`, each
under `build-slot` when the step says `slot = true`, and prints `PASS name`, `FAIL name` or `SKIP name`. A step with
`when` globs runs only when a changed path (against REMOTE/MAIN) matches one. The files in `[gate] restore` are
checked out again afterwards. Every step's full log and verdict go to `--out` (default: a `gate_NAME` directory
under the temporary directory). Exit 1 when a step failed.

`wsl` runs `[gate] wsl_steps` in a Linux distribution on a `git archive` of HEAD, with the target directory kept
between runs and cleared (then `fstrim`) once it passes `[wsl] target_max_gb`.

`merge` refuses a branch that is not a fast-forward of main or whose commits carry agent attribution,
fast-forwards main, runs `run` and `wsl` under one lock, and pushes only through `repo-guard push`. After a failed
check main stays advanced locally and the output names the command that undoes it.

`lock` runs a command while holding the gate lock, waiting for it like the other gates.

The configuration is `<project>/.agents/agent-tools.toml` (`[gate]`, `[repo_guard]`) and
`~/.agents/agent-tools.local.toml` (`[gate] lock`, `[wsl]`, `[build_slot]`); see the engineering overlay's
kb/AGENT_TOOLS.md.
"""

from __future__ import annotations

import sys
from pathlib import Path

from duho import Cli, Cmd, LoggingArgs

_LIB = str(Path(__file__).resolve().parents[1] / "lib" / "python")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import _gate  # noqa: E402
import _slots  # noqa: E402
import agent_tools  # noqa: E402


def _guarded(call):
    """The exit code of `call()`, a refusal or a configuration error reported on stderr."""
    try:
        return call()
    except (_gate.GateError, agent_tools.ConfigError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


class Gate(LoggingArgs, Cli):
    """Run a project's checks before a merge: on this machine, in a Linux distribution, or both and push."""

    _parsername_ = "gate"

    class _Common(Cmd):
        out: str = ""
        "Where logs and verdicts go (default: gate_NAME under the temporary directory)."
        ("--out",)

        lock_every: float = _slots.DEFAULT_LOCK_EVERY
        "Seconds between looks for the gate lock while waiting."
        ("--lock-every",)

        limit: int = 0
        "build-slot limit for steps that take a slot (default: build-slot's own)."
        ("--limit",)

        max_wait: int = _slots.DEFAULT_MAX_WAIT
        "build-slot seconds to wait for a slot."
        ("--max-wait",)

        def slot_options(self) -> dict:
            options = {"max_wait": self.max_wait}
            if self.limit:
                options["limit"] = self.limit
            return options

    class Run(_Common):
        """Run the project's steps in a worktree."""

        _parsername_ = "run"

        worktree: Path = Path(".")
        "The worktree to check (default: the current directory)."
        ("worktree",)

        step: "list[str]" = []
        "Run only this step; repeat for several."
        ("--step",)

        fail_fast: bool = False
        "Stop at the first failed step."
        ("--fail-fast",)

        def __call__(self) -> int:
            return _guarded(lambda: 0 if _gate.run_gate(
                Path(self.worktree), only=list(self.step), out=Path(self.out) if self.out else None,
                fail_fast=self.fail_fast, lock_every=self.lock_every, slot_options=self.slot_options()) else 1)

    class Wsl(_Common):
        """Run the Linux leg: the project's wsl_steps on an archive of HEAD."""

        _parsername_ = "wsl"

        worktree: Path = Path(".")
        "The worktree to check (default: the current directory)."
        ("worktree",)

        def __call__(self) -> int:
            return _guarded(lambda: 0 if _gate.run_wsl(
                Path(self.worktree), out=Path(self.out) if self.out else None, lock_every=self.lock_every,
                slot_options=self.slot_options()) else 1)

    class Merge(_Common):
        """Fast-forward main to a branch, run both legs, push through repo-guard."""

        _parsername_ = "merge"

        branch: str = ""
        "The branch to merge."
        ("branch",)

        repo: Path = Path(".")
        "The repository, on its main branch (default: the current directory)."
        ("-C",)

        no_push: bool = False
        "Run the checks and stop before pushing."
        ("--no-push",)

        def __call__(self) -> int:
            if not self.branch:
                print("error: a branch is required", file=sys.stderr)
                return 2
            return _guarded(lambda: 0 if _gate.merge_branch(
                Path(self.repo), self.branch, push=not self.no_push, out=Path(self.out) if self.out else None,
                lock_every=self.lock_every, slot_options=self.slot_options()) else 1)

    class Lock(Cmd):
        """Run a command while holding the gate lock."""

        _parsername_ = "lock"

        label: str = ""
        "Who holds the lock, written to its holder file."
        ("label",)

        command: "list[str]" = []
        "The command, after a literal --."
        ("command",)

        lock_every: float = _slots.DEFAULT_LOCK_EVERY
        "Seconds between looks for the lock while waiting."
        ("--lock-every",)

        def __call__(self) -> int:
            import subprocess
            cmd = list(self.command) or list(getattr(self, "_passthrough_", None) or [])
            if cmd and cmd[0] == "--":
                cmd = cmd[1:]
            if not self.label or not cmd:
                print("error: gate lock LABEL -- CMD...", file=sys.stderr)
                return 2

            def hold() -> int:
                config = agent_tools.load(None)
                with _gate.lock_for(config, self.label, self.lock_every):
                    return subprocess.run(cmd).returncode
            return _guarded(hold)

    _subcommands_ = [Run, Wsl, Merge, Lock]
