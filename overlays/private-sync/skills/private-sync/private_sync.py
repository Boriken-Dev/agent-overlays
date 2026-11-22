"""`private-sync` -- keep a project's `.agents` in a private store, and sync the store.

    private-sync link [PATH]    point PATH/.agents at its store
    private-sync sync           reconcile a copy-mode project, then move the store

The logic is `lib/python/_link.py` (standard library only), loaded by path so no
PYTHONPATH setup is needed; the session hooks run the same file through
`hooks/run_link.py`.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Optional

from duho import Cli, Cmd, LoggingArgs

#: `<overlay>/skills/private-sync/private_sync.py` -> `<overlay>`.
OVERLAY_ROOT = Path(__file__).resolve().parents[2]
_LIB_KEY = "private_sync_link"


def _load_link():
    """This overlay's `_link` module, by path."""
    cached = sys.modules.get(_LIB_KEY)
    if cached is not None:
        return cached
    source = OVERLAY_ROOT / "lib" / "python" / "_link.py"
    if not source.is_file():
        raise ImportError("private-sync overlay is incomplete: no %s" % source)
    spec = importlib.util.spec_from_file_location(_LIB_KEY, str(source))
    module = importlib.util.module_from_spec(spec)
    sys.modules[_LIB_KEY] = module  # registered before it runs: the module refers to itself
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(_LIB_KEY, None)
        raise
    return module


_LOG = logging.getLogger("private-sync")


class PrivateSync(LoggingArgs, Cli):
    """Keep each project's private `.agents` in one store that can move between machines."""

    _parsername_ = "private-sync"

    class _Store(Cmd):
        agents_dir: Optional[Path] = None
        "The user's agents dir that holds the stores (default: $AGENTS_HOME, else ~/.agents)."
        ("--agents-dir",)

        store_dir: Optional[str] = None
        ("Where stores live: relative to --agents-dir, or absolute to put them "
         "elsewhere entirely (default: projects, or $AGENTS_STORE_DIR).")
        ("--store-dir",)

        name: Optional[str] = None
        "Store name (default: the project directory's basename)."
        ("--name",)

        dry_run: bool = False
        "Show what would happen without touching anything."
        ("--dry-run",)

    class Link(_Store):
        """Point a project's .agents at a store outside the project repo.

        With a git-kept agents dir, `<project>/.agents` becomes a worktree of it on a
        branch named after the project; `--store-repo` puts that branch on another
        repository instead (the project's own, or a team's), and the choice is
        recorded. Otherwise it symlinks `<project>/.agents` to its store directory
        (`--copy` mirrors it as a real dir instead, for Windows or no-symlink
        environments). An existing real `.agents/` is adopted on the first link."""

        _parsername_ = "link"

        path: Path = Path(".")
        "Project directory to link (default: current directory)."
        ("path",)

        copy: bool = False
        "Copy the store into the project instead of symlinking (no-symlink fallback)."
        ("--copy",)

        force: bool = False
        "Replace an existing .agents symlink, or back up a conflicting real .agents dir."
        ("--force",)

        store_repo: Optional[str] = None
        ("Keep the store on another repository: 'self' (a branch of the project's own "
         "repo), a local git repo, or a git URL (default: a branch of the agents repo). "
         "Recorded, so later links of this project do the same.")
        ("--store-repo",)

        store_branch: Optional[str] = None
        ("The store's branch there (default: the store name; 'agents' for --store-repo "
         "self). Recorded too.")
        ("--store-branch",)

        def __call__(self) -> int:
            link = _load_link()
            link.link_project(
                self.path, self.agents_dir or link.default_agents_dir(), name=self.name,
                store_dir=self.store_dir, copy=self.copy, force=self.force,
                dry_run=self.dry_run, logger=_LOG, store_repo=self.store_repo,
                store_branch=self.store_branch,
            )
            if self.dry_run:
                _LOG.info("dry-run: no files were written")
            return 0

    class Sync(_Store):
        """Reconcile a copy-mode project, then hand off to whatever moves the store.

        Pass `--project` so a copy-mode project's .agents is copied back into its
        store first (symlinked projects need no copy-back: their .agents is the
        store). If `<agents-dir>/hooks/sync` exists it owns the transport entirely
        and its exit code is returned; use it for rsync, a cloud drive, or anything
        else. Otherwise a built-in git path runs (`pull --rebase`, commit, push),
        with `--remote` bootstrapping a fresh repo (`git init` and `origin`). When
        `DOTAGENTS_AGENTS_TOKEN` is set the pull and push authenticate against
        github.com with that token, bypassing a hosted runner's rewrite of github
        traffic."""

        _parsername_ = "sync"

        message: str = "sync"
        "Commit message for the sync (also passed to a hooks/sync script)."
        ("--message", "-m")

        project: Optional[Path] = None
        "A project whose (copy-mode) .agents should be copied back into the store first."
        ("--project",)

        remote: Optional[str] = None
        "Set origin to this URL (git init first if needed) before syncing."
        ("--remote",)

        no_pull: bool = False
        "Skip the git pull --rebase step (built-in git path only)."
        ("--no-pull",)

        no_push: bool = False
        "Skip the git push step (built-in git path only)."
        ("--no-push",)

        def __call__(self) -> int:
            link = _load_link()
            return link.sync_agents(
                self.agents_dir or link.default_agents_dir(), message=self.message,
                project_dir=self.project, name=self.name, store_dir=self.store_dir,
                remote=self.remote, pull=not self.no_pull, push=not self.no_push,
                dry_run=self.dry_run, logger=_LOG,
            )

    _subcommands_ = [Link, Sync]


if __name__ == "__main__":
    from duho import main as _duho_main

    raise SystemExit(_duho_main(PrivateSync))
