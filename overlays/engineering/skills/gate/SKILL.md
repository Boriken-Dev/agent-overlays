---
name: gate
description: Run a project pre-merge verification gate (`run`, `wsl`, `merge`, `lock`) under the machine gate lock.
---

# gate

Use `gate` (`skills/gate/gate.py`) to execute configured pre-merge steps on the host and in WSL under the shared gate lock.

## Running

```bash
$AGENTS_PYTHON -I $ENGINEERING_OVERLAY_ROOT/skills/gate/gate.py --help
```

When the overlay's `[scripts]` launcher is on `PATH`, invoke `gate` directly.
