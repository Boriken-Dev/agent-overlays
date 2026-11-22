---
name: context-check
description: Warn about agent context files outside the standard places (`CLAUDE.md`, `GEMINI.md`, `.cursorrules`, a committed `.agents/` or `AGENTS.local.md`, a misnamed `agents.md`) and say where each belongs.
---

# context-check

Use `context-check` (`skills/context-check/context_check.py`) before a commit, a handoff or a release to verify a repository keeps its agent
context where the standard puts it: `AGENTS.md` and `AGENTS.*.md` in a directory, and the same names in that directory's `.agents`
(`AGENTS_DIR` and `AGENTS_CONTEXT` change the places and the names). It reports what a commit would hold: another tool's own file
(`foreign`, with the move), a context file with the wrong case or extension (`name`), and the user's own `.agents` or `.local` files
(`private`, which are never committed). Exit 0 clean, 1 findings, 3 nothing to check.

## Running

```bash
$AGENTS_PYTHON -I $ENGINEERING_OVERLAY_ROOT/skills/context-check/context_check.py --help
$AGENTS_PYTHON -I $ENGINEERING_OVERLAY_ROOT/skills/context-check/context_check.py [repo] [--allow PATH]
```

`--allow` leaves a path out when the project chose to keep it (a `CLAUDE.md` that only imports `AGENTS.md`).

When the overlay's `[scripts]` launcher is on `PATH`, invoke `context-check` directly.
