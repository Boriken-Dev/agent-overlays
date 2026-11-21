---
name: summarize-run
description: Run a noisy build, test, or lint command, save the full log to disk, and print a compact PASS/FAIL verdict with only the notable signal lines.
---

# summarize-run

Use `summarize-run` (`skills/summarize-run/summarize_run.py`) to run builds and test suites cheaply without flooding context.

## Running

```bash
$AGENTS_PYTHON -I $ENGINEERING_OVERLAY_ROOT/skills/summarize-run/summarize_run.py --help
```

When the overlay's `[scripts]` launcher is on `PATH`, invoke `summarize-run` directly.
