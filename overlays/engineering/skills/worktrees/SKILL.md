---
name: worktrees
description: List linked git worktrees with their upstream merge status and target directory sizes, and remove clean merged worktrees.
---

# worktrees

Use `worktrees` (`skills/worktrees/worktrees.py`) to audit and clean up merged git worktrees.

## Running

```bash
$AGENTS_PYTHON -I $ENGINEERING_OVERLAY_ROOT/skills/worktrees/worktrees.py --help
```

When the overlay's `[scripts]` launcher is on `PATH`, invoke `worktrees` directly.
