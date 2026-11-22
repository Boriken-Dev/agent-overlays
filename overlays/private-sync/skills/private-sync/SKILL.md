---
name: private-sync
description: Keep a project's private `.agents` in one git-synced store, link a project's `.agents` to its store, and sync the store between machines.
---

# private-sync

Use `private-sync` (`skills/private-sync/private_sync.py`) to point a project's `.agents` at its store (`private-sync link [PATH]`: a worktree of the store's branch, a symlink, or a copy) and to move the store (`private-sync sync`: copy a copy-mode project back, then `hooks/sync` or git pull, commit and push). `kb/PRIVATE_SYNC.md` has the setup and the layout.

## Running

```bash
$AGENTS_PYTHON -I $PRIVATE_SYNC_OVERLAY_ROOT/skills/private-sync/private_sync.py --help
```

When the overlay's `[scripts]` launcher is on `PATH`, invoke `private-sync` directly.
