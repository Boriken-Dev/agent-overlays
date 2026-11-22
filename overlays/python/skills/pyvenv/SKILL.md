---
name: pyvenv
description: Create a project's Python venv for one interpreter version (`<project>/.pyvenv/<version>-<os>-<arch>/`, `-g` for `~/.pyvenv/`) and run Python in it.
---

# pyvenv

Use `pyvenv` (`skills/pyvenv/pyvenv.py`) to make a per-project venv once (`pyvenv create [version]`) and to run Python in it (`pyvenv run [version] -- <args>`), instead of locating a venv's interpreter by hand. `run` uses the project's venv for that version when one exists, else the user's in `~/.pyvenv/`, and never makes a project venv.

## Running

```bash
$AGENTS_PYTHON -I $PYTHON_OVERLAY_ROOT/skills/pyvenv/pyvenv.py --help
```

When the overlay's `[scripts]` launcher is on `PATH`, invoke `pyvenv` directly.
