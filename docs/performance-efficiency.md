# Performance and efficiency in 0.12.12

Optimus does less repeated work while preserving the scope and evidence of each
operation. Saved preferences and approvals are unchanged by this update.

| Area | Change | Behavior retained |
| --- | --- | --- |
| Process analysis | Build normalized app-directory and startup indexes once per scan instead of once per process. | Fresh inventory, longest matching app directory, exact startup evidence and protected processes. |
| Browser cleanup | Prepare browser matching rules once per process check rather than rebuilding every browser definition for every process. | Fresh process enumeration and the existing checks immediately before each file deletion. |
| Worker totals | Update aggregate counters when an item changes; snapshots no longer rescan every completed package. | Deduplication, late download receipts, unknown sizes, estimated bytes and verified installation counts. |
| Animated background | Draw precomputed spiral paths through Qt's transform and polyline operations. | The same stars, colors, geometry and animation phase; reduced-motion behavior remains available. |
| Live labels | Skip identical style/text measurement work. | Current-item progress, theme changes, accessible font sizes and hover descriptions. |

## Measurements

These are paired, inert fixture measurements on one Windows PC, not a prediction
of another computer's speed or proof of an overall system performance gain.
Operating-system, network and vendor-installer time still varies.

| Fixture | Previous implementation | 0.12.12 |
| --- | ---: | ---: |
| 3,000 totals snapshots with 1,000 completed packages, median of five runs | 1.649 s | 0.00166 s |
| Process attribution and report: 400 processes, 100 distinct executables, 300 app roots | 0.937 s | 0.178 s |
| Browser matching: 400 processes, 80 fresh checks, median of five runs | 0.891 s | 0.0185 s |
| 1,000 repeated spinner payloads | 2,000 Qt style-change events | 0 style-change events |
| Spiral path draw calls per frame | 477 individual segment calls | 3 polyline calls |
| Background paint at 1280 × 820, paired fixture median | 10.66 ms | 8.75 ms |

The totals fixture produced identical public snapshots. Geometry tests compare
the old and new spiral paths across window sizes and animation phases. Other
regressions exercise duplicate, reordered, late and malformed progress receipts,
plus changes to browser processes and installed-application data between scans.
The process fixture excludes native inventory and sampling time. The browser
fixture measures matching overhead; it still enumerates the processes on every
check. Neither establishes the same speedup for a complete scan or file cleanup.

## Verification

Run the regression tests in a development environment with the project's test
dependencies installed:

```text
python -m pytest -q tests/test_worker_resource_totals.py tests/test_native_ui_efficiency.py tests/test_process_harness.py tests/test_process_harness_attribution.py tests/test_cleanup_process_matching.py tests/test_cleaner_cancel.py
python tools/critical_review.py --output test-artifacts/critical-review
python tools/test_native.py
```

The critical review uses fixture backends and records its remaining manual review
areas. Native build verification checks the packaged interface, startup, read-only
process reporting, TLS configuration and progress reporting. It does not clean or
update the host as a side effect of testing.

No new tuning, automatic deletion, real-time priority, security exceptions or
background service is enabled by these performance changes. Windows privacy
checks retain the 0.12.11 batching improvement and their separate per-control
recovery, pre-write comparison and post-write verification.
