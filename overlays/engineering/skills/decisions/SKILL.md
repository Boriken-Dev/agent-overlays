---
name: decisions
description: Record, query, decide, supersede, and migrate architectural decisions (`Dnn_<slug>.md`) in status folders (`proposed/`, `decided/`, `superseded/`).
---

# decisions

Use `decisions` (`skills/decisions/decisions.py`) to add, query with SQL, decide, or supersede architectural decisions without maintaining a manual INDEX.md.

## Running

```bash
$AGENTS_PYTHON -I $ENGINEERING_OVERLAY_ROOT/skills/decisions/decisions.py --help
```

When the overlay's `[scripts]` launcher is on `PATH`, invoke `decisions` directly.
