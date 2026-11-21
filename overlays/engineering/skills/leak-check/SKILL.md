---
name: leak-check
description: Scan a repository working tree and commit messages for private `.agents/` paths, plan names, decision IDs, personal/infra markers, and AI attribution trailers before commit or release.
---

# leak-check

Use `leak-check` (`skills/leak-check/leak_check.py`) before any commit, handoff, push, or release tag to verify no private notes or attribution trailers leak.

## Running

```bash
$AGENTS_PYTHON -I $ENGINEERING_OVERLAY_ROOT/skills/leak-check/leak_check.py --help
```

When the overlay's `[scripts]` launcher is on `PATH`, invoke `leak-check` directly.
