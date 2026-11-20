"""`dotagents build-slot` -- run a heavy build only while fewer than a limit of them run on this machine.

    dotagents build-slot [--limit N] [--max-wait S] [--every S] [--settle S] -- CMD...

A heavy build is a running cargo, rustc, Flutter test or Dart build or test; `[build_slot] processes` in
`~/.agents/agent-tools.local.toml` changes the names, and `wsl = [distro, ...]` counts them inside those WSL
distributions too (Windows). Pass the build after `--`; with no command it only waits for a slot, which two
callers can both see free at once.

The limit is `--limit`, else the one integer in `<dir>/limit` (read on every check, so a change reaches callers
already waiting), else `[build_slot] limit`, else 8. `<dir>` is `$LOCALAPPDATA/build_slot` (else
`~/build_slot`), or `[build_slot] dir`.

Starts are serialised through one machine-wide gate (the directory `<dir>/gate`, holding a `pid` file), held from
the check until the new build shows in the process count or `--settle` seconds pass, so two callers never take
the same free slot. A gate whose holder has exited is taken over. Nothing else is ever stopped.

The command's exit code is returned and only its output goes to stdout: this command's own lines go to stderr.
Exit 1, running nothing, when no slot frees within `--max-wait` (it prints who holds the slots); 127 when the
command cannot be found. The state files are the ones earlier `build_slot.py` scripts wrote, so old and new
callers share one limit and one gate.
"""

from __future__ import annotations

import sys
from pathlib import Path

from duho import Cmd, LoggingArgs

_LIB = str(Path(__file__).resolve().parents[1] / "lib" / "python")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import _slots  # noqa: E402
import agent_tools  # noqa: E402


class BuildSlot(LoggingArgs, Cmd):
    """Run a heavy build once a slot is free; return its exit code."""

    _parsername_ = "build-slot"

    command: "list[str]" = []
    "The command and its arguments, after a literal --. Empty: only wait for a slot."
    ("command",)

    limit: int = 0
    "How many heavy builds may run (default: the limit file, else [build_slot] limit, else 8)."
    ("--limit",)

    max_wait: int = _slots.DEFAULT_MAX_WAIT
    "Seconds to wait for a slot before giving up with exit 1."
    ("--max-wait",)

    every: float = _slots.DEFAULT_EVERY
    "Seconds between checks while waiting."
    ("--every",)

    settle: float = _slots.DEFAULT_SETTLE
    "Seconds the gate is held after a start, unless the new build shows sooner."
    ("--settle",)

    def __call__(self) -> int:
        cmd = list(self.command) or list(getattr(self, "_passthrough_", None) or [])
        if cmd and cmd[0] == "--":
            cmd = cmd[1:]
        try:
            config = agent_tools.load(None)
        except agent_tools.ConfigError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        slot = _slots.Slot(_slots.SlotConfig.from_config(config))
        return slot.run(cmd, limit=self.limit or None, max_wait=self.max_wait, every=self.every,
                        settle=self.settle)
