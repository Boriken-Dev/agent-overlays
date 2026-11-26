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

## Common forms

```bash
plans add NAME --status ready -t "Title" -e "role (model)" -p "First phase" -p "Second phase" -a owner=NAME
plans show NAME                                   # the whole plan, read through the tool
plans edit NAME --section facts --append "- a fact"
plans edit NAME --replace "OLD TEXT=NEW TEXT"     # exact text; the progress line too
plans start NAME 2; plans check NAME 2 -m "evidence"; plans note NAME -m "..."; plans block NAME 2 -m "why"
plans status NAME done -m "evidence"              # refused while a phase is open; --force closes anyway
plans status NAME on-hold -m "what it waits for"
plans list                                        # open and done, with progress
plans query --select name,status,title,description --where "status='executing'"
```

- `-p` takes one phase and repeats; several phases in one `-p` become a single checkbox.
- `--body-file` replaces the whole body; use `--section ... --append` or `--replace` for a part.
- `query` columns: `name`, `status`, `folder`, `path`, `is_complex`, `extras_count`, `title`, `description`, plus the frontmatter attributes; there is no modified time.
