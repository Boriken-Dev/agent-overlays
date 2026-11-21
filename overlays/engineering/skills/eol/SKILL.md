---
name: eol
description: Check and fix file line endings (LF vs CRLF) safely according to `.gitattributes` without corrupting binary or `-text` fixture files.
---

# eol

Use `eol` (`skills/eol/eol.py`) to check (`eol check [--control]`) or convert (`eol fix [--dry-run]`) line endings safely.

## Running

```bash
$AGENTS_PYTHON -I $ENGINEERING_OVERLAY_ROOT/skills/eol/eol.py --help
```

When the overlay's `[scripts]` launcher is on `PATH`, invoke `eol` directly.
