"""`merge` -- resolve merge conflicts without losing text.

    merge union FILE...          keep HEAD's side, then the incoming lines it lacks; Latin-1 bytes read as such
    merge show FILE              the word-level differences between the sides of each conflict
    merge pub-lock OURS THEIRS OUT   merge two pubspec.lock files package by package
    merge dupes FILE...          lines that start like a nearby line (what a union leaves); exit 1 if any
    merge fix-utf8 FILE...       bytes that are not UTF-8 become the Latin-1 characters they were
    merge rows FILE              keep HEAD's table rows, add incoming rows whose first cell is new
    merge both-blocks FILE       both sides appended a block before one closing brace: keep both
    merge continue [--rounds N]  continue a rebase or cherry-pick, resolving by rule

`continue` resolves each conflicted path by the first `[merge] rules = [{ path, how, run }]` entry of
`<project>/.agents/agent-tools.toml` whose `path` glob matches. `how` is `union`, `rows`, `both-blocks`, `pub-lock`
or `ours-then-run` (take HEAD's copy, then run `run` to regenerate it: a lock file). A path no rule matches stops
the loop, naming it, before anything is staged; a file still holding a marker is never staged. Exit 0 finished,
1 a path needs a person, 2 the round limit.

Every resolver refuses to leave a conflict marker in the file.
"""

from __future__ import annotations

import sys
from pathlib import Path

from duho import Cli, Cmd, LoggingArgs

def _resolve_lib_dir() -> Path:
    import os
    for env_key in ("ENGINEERING_OVERLAY_ROOT", "AOS_OVERLAY_ROOT"):
        val = os.environ.get(env_key)
        if val and (Path(val) / "lib" / "python").is_dir():
            return Path(val) / "lib" / "python"
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "lib" / "python"
        if candidate.is_dir():
            return candidate
    return here.parents[2] / "lib" / "python"


_LIB = _resolve_lib_dir()
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import _merge  # noqa: E402
import agent_tools  # noqa: E402


def _each(files, resolver, label: str) -> int:
    if not files:
        print("error: no file given", file=sys.stderr)
        return 2
    status = 0
    for name in files:
        try:
            print(name, label, resolver(Path(name)))
        except (_merge.MergeError, OSError) as error:
            print(f"error: {error}", file=sys.stderr)
            status = 1
    return status


class Merge(LoggingArgs, Cli):
    """Conflict resolvers that keep both sides, and a rebase loop that applies them by rule."""

    _parsername_ = "merge"

    class _Files(Cmd):
        files: "list[Path]" = []
        "The files."
        ("files",)

    class Union(_Files):
        """Keep both sides of every conflict (HEAD's first)."""

        _parsername_ = "union"

        def __call__(self) -> int:
            return _each(self.files, _merge.union, "hunks")

    class Show(Cmd):
        """Print the word-level differences between the sides of each conflict."""

        _parsername_ = "show"

        file: Path = Path(".")
        "The conflicted file."
        ("file",)

        def __call__(self) -> int:
            try:
                for line in _merge.show(Path(self.file)):
                    print(line)
            except (_merge.MergeError, OSError) as error:
                print(f"error: {error}", file=sys.stderr)
                return 1
            return 0

    class PubLock(Cmd):
        """Merge two pubspec.lock files package by package."""

        _parsername_ = "pub-lock"

        ours: Path = Path(".")
        "The lock whose entry wins."
        ("ours",)

        theirs: Path = Path(".")
        "The other lock."
        ("theirs",)

        out: Path = Path(".")
        "Where the merged lock is written."
        ("out",)

        def __call__(self) -> int:
            try:
                print(_merge.pub_lock(Path(self.ours), Path(self.theirs), Path(self.out)), "packages")
            except (_merge.MergeError, OSError) as error:
                print(f"error: {error}", file=sys.stderr)
                return 1
            return 0

    class Dupes(_Files):
        """List lines that start like a nearby line; exit 1 when there are any."""

        _parsername_ = "dupes"

        def __call__(self) -> int:
            found = 0
            for name in self.files:
                for line in _merge.dupes(Path(name)):
                    print(line)
                    found += 1
            return 1 if found else 0

    class FixUtf8(_Files):
        """Turn bytes that are not UTF-8 into the Latin-1 characters they were."""

        _parsername_ = "fix-utf8"

        def __call__(self) -> int:
            return _each(self.files, lambda p: "fixed %d bytes: %s" % (
                len(bad := _merge.fix_utf8(p)), sorted({hex(b) for b in bad})), "")

    class Rows(_Files):
        """Merge table rows keyed by the first cell."""

        _parsername_ = "rows"

        def __call__(self) -> int:
            return _each(self.files, _merge.rows, "hunks")

    class BothBlocks(_Files):
        """Keep both blocks two sides appended before one closing brace."""

        _parsername_ = "both-blocks"

        def __call__(self) -> int:
            return _each(self.files, _merge.both_blocks, "hunks")

    class Continue(Cmd):
        """Continue a rebase or cherry-pick, resolving conflicted paths by `[merge] rules`."""

        _parsername_ = "continue"

        repo: Path = Path(".")
        "The repository (default: the current directory)."
        ("-C",)

        rounds: int = 40
        "How many continue steps to take before giving up."
        ("--rounds",)

        def __call__(self) -> int:
            try:
                return _merge.continue_pick(Path(self.repo), self.rounds)
            except agent_tools.ConfigError as error:
                print(f"error: {error}", file=sys.stderr)
                return 2

    _subcommands_ = [Union, Show, PubLock, Dupes, FixUtf8, Rows, BothBlocks, Continue]


if __name__ == "__main__":
    from duho import main as _duho_main

    raise SystemExit(_duho_main(Merge))
