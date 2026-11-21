---
name: merge
description: Deterministic conflict resolvers (`union`, `show`, `pub-lock`, `dupes`, `fix-utf8`, `rows`, `both-blocks`, `continue`) that refuse to leave conflict markers.
---

# merge

Use `merge` (`skills/merge/merge.py`) to resolve rebase and merge conflicts deterministically without losing text.

## Running

```bash
$AGENTS_PYTHON -I $ENGINEERING_OVERLAY_ROOT/skills/merge/merge.py --help
```

When the overlay's `[scripts]` launcher is on `PATH`, invoke `merge` directly.
