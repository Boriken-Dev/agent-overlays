---
name: build-slot
description: Serialize and cap concurrent heavy builds (cargo, rustc, flutter_tester, dart) across sessions on one machine.
---

# build-slot

Use `build-slot` (`skills/build-slot/build_slot.py`) to wait for and acquire a machine-wide build slot before running heavy compilations or test suites.

## Running

```bash
$AGENTS_PYTHON -I $ENGINEERING_OVERLAY_ROOT/skills/build-slot/build_slot.py --help
```

When the overlay's `[scripts]` launcher is on `PATH`, invoke `build-slot` directly.
