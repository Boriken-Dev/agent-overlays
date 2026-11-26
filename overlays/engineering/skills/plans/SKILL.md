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
plans show NAME                                   # the whole plan; prefer a part:
plans show NAME --brief                           # frontmatter, title, progress lines only
plans show NAME --section facts                   # one section (progress, facts, phases, verification, reporting)
plans show NAME --phase 3                         # its progress line and its "### Phase 3" block
plans phases NAME [--open] [--format json]        # id, state, title of each phase
plans next NAME [--block]                         # the current phase (--block: only its block)
plans edit NAME --section facts --set "- text"    # replace ONLY that section (--set-file F for a file)
plans edit NAME --section facts --append "- a fact"   # --append-file F too
plans edit NAME [--section S] --replace "OLD TEXT=NEW TEXT"   # exact text, in the section if given
plans edit NAME --whole --body-file F             # the entire body; refused with --section, needs --whole
plans phase-add NAME "title" [--after N]          # progress line + block, renumbered (--after 0: first)
plans phase-rename NAME 2 "new title"
plans phase-remove NAME 2 [--force]               # --force to remove a checked phase
plans phase-split NAME 2 "part A" "part B"        # one combined box into several, renumbered
plans start NAME 2; plans check NAME 2 -m "evidence"; plans block NAME 2 -m "why"
plans note NAME -m "a fact"                       # into Known Facts & Context
plans note NAME --phase 2 -m "a note"             # under that phase's block
plans status NAME done -m "evidence"              # refused while a phase is open; --force closes anyway
plans status NAME on-hold -m "what it waits for"
plans list                                        # open and done, with progress
plans query --select name,status,title,description --where "status='executing'"
```

- `-p` takes one phase and repeats; several phases in one `-p` become a single checkbox.
- `--body` / `--body-file` replace the whole body (only with `--whole`, never with `--section`). Change a part with `--section S --set`, `--append` or `--replace`; an unknown section is an error that lists the plan's sections.
- Section text may not hold a `## ` line (it would start a section); `### ` and lower are fine. `phase-*` need plain numeric phase ids and Progress and the `### Phase` blocks in agreement (`plans validate`); a new or split phase gets a template block to fill.
- `query` columns: `name`, `status`, `folder`, `path`, `is_complex`, `extras_count`, `title`, `description`, plus the frontmatter attributes; there is no modified time.

## Migrating a legacy store

Run `plans migrate --dir DIR`, `dotagents findings migrate` and `decisions migrate --dir DIR` (each takes `--dry-run`), then check:

- `plans migrate` reads the status from the old body line, files each plan under `plans/<status>/`, merges a complex plan's parent into its `README.md` and removes `INDEX.md`. It also rewrites plans already in `completed/`. Afterwards a plan that was `executing` with nothing running is still `executing`: set it with `plans status NAME ready -m ...`.
- `dotagents findings migrate` moves flat files to `active/` and requotes values, but leaves `name:` and `status:` in the frontmatter of every finding, the processed ones too. Strip those two keys with a script that keeps the file's line endings.
- `decisions migrate` drops `id`, `name`, `title` and `status` from the frontmatter itself.
- References to the old paths (`plans/NAME.md`, `findings/SLUG.md`, `INDEX.md`) in notes and other plans are not rewritten; grep for them.
- `plans validate` takes one plan name (no validate-all) and reports `<id>`-style text in a body as an unfilled placeholder, so a plan that names a placeholder in prose never passes.
