---
name: configure-pages
description: Configure a GitHub repository Pages settings (workflow permissions, Actions build source, and deployment branch/tag policy).
---

# configure-pages

Use `configure-pages` (`skills/configure-pages/configure_pages.py`) to apply standard GitHub Pages repository settings via `gh api`.

## Running

```bash
$AGENTS_PYTHON -I $ENGINEERING_OVERLAY_ROOT/skills/configure-pages/configure_pages.py --help
```

When the overlay's `[scripts]` launcher is on `PATH`, invoke `configure-pages` directly.
