---
name: records
description: Query, add, update, edit, move, and delete frontmatter-Markdown records across any folder (findings, plans, decisions, kb) using read-only SQL and pluggable providers.
---

# records

Use `records` (`skills/records/records.py`) to run SQL queries (`SELECT ... FROM records WHERE ...`), inspect sections, or mutate YAML frontmatter attributes across any `.agents/<folder>` store.

## Running

```bash
$AGENTS_PYTHON -I $ENGINEERING_OVERLAY_ROOT/skills/records/records.py --help
```

When the overlay's `[scripts]` launcher is on `PATH`, invoke `records` directly.
