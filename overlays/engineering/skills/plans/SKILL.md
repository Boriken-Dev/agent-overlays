---
name: plans
description: Manage engineering plans in status folders (draft, ready, review, executing, on-hold, completed) with YAML frontmatter, complex directory support, and SQL queries.
---

# plans

Use `plans` (`skills/plans/plans.py`) to create, query, inspect, step (`start`, `check -m`, `block -m`, `note -m`), edit, validate, and close plans without hand-editing progress checkboxes or reading entire plan files.

## Running

```bash
$AGENTS_PYTHON -I $ENGINEERING_OVERLAY_ROOT/skills/plans/plans.py --help
```

When the overlay's `[scripts]` launcher is on `PATH`, invoke `plans` directly.
