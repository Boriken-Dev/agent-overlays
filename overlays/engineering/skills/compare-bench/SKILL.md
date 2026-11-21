---
name: compare-bench
description: Compare two benchmark JSON result files on median latency and exit non-zero if any metric regressed beyond the threshold.
---

# compare-bench

Use `compare-bench` (`skills/compare-bench/compare_bench.py`) to compare baseline and candidate benchmark JSON results.

## Running

```bash
$AGENTS_PYTHON -I $ENGINEERING_OVERLAY_ROOT/skills/compare-bench/compare_bench.py --help
```

When the overlay's `[scripts]` launcher is on `PATH`, invoke `compare-bench` directly.
