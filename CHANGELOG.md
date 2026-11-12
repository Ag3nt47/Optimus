# Changelog

## 0.12.45

- Add **Empty the Recycle Bin/Trash each run** (Windows Recycle Bin, macOS and Linux Trash), on in Full Optimization. It runs before duplicate recycling, reports measured item and byte counts, and is verified from its own readback.
- Verify the Optimus sign-in task after Windows rewrites its stored XML without default elements or in another order; this previously blocked launcher updates and sign-in changes.
- Load the Settings process inventory about four times faster on Windows (7.3 s to 1.7 s with about 290 processes) by reading the parent-process map once per census.
- Add CodeQL, Dependabot, Gitleaks and Ruff defect checks to GitHub.
- Cut the opt-in background resource sampler's cost per sample by about two thirds on Windows by reading process CPU and I/O counters in one snapshot per process.
- Reuse network adapter up/link-speed reports in live metrics for up to 30 seconds while the adapter list is unchanged; traffic is still read every sample.

## 0.12.44

- Add separate full Microsoft 365 and perpetual Office uninstall choices using exact signed native uninstallers.
- Fix Edge removal when WinGet has no matching registration, with scoped vendor and removable-package fallback and actual absence checks.
- Keep Windows App protected in setup, Settings and native removal boundaries; preserve shared WebView2 and Windows runtimes.
- Add default-on removal choices and individual consumer-app controls, preserving saved off choices and showing concrete launch warnings.
- Add saved-scope reappearance enforcement at sign-in, periodic checks and Windows Update completion, with honest policy applicability reporting.
- Add supported cloud-speech, voice-activation and activity-history privacy controls without blocking remote-desktop audio or security updates.
- Keep Defender status/remediation commands within Windows' command-line limit as the privacy catalog expands, and continue after known terminal uninstall findings.

## 0.12.43

- Fit worker cards to the actual dashboard space after header layout changes, retaining access to controls and hardware status.

## 0.12.42

- Accept a fully visible wrapped title in the header's font tests while retaining clipping and control-bound checks.

## 0.12.41

- Measure the progress header's actual title and controls before arranging them, keeping the restart control and full title readable across fonts and window sizes.

## 0.12.40

- Add a green Restart optimization control that waits for active work to drain before starting a fresh run through the saved setup and review gates.
- Explain when another maintenance process owns the active run.
- Record native boot provenance before update-only workers launch, allowing eligible interrupted attempts to recover after a verified restart.
- Update driver cancellation and download-cleanup fixtures to supply current saved driver approvals.

## 0.12.39

- Recover recorded application update timeouts after a verified Windows restart and idle check. Hold the affected offer while remaining approved updates continue.
- Match Windows Update's generic hardware class against fresh signed device evidence when applying an existing exact driver approval.
- Preserve authenticated installer completion at the timeout boundary and identify unresolved timeouts explicitly.
- Retain the installed protection for Bing Wallpaper and the active desktop image during cleanup and duplicate scans.

## 0.12.38

- Fix the native self-update regression fixture for settings saved before Protected apps was added.

## 0.12.37

- Reconcile eligible unresolved WinGet attempts after a verified Windows restart and idle check. Hold the affected offer and allow the remaining updates to proceed.
- Use GitHub's macOS 26 Apple Silicon runner image for native builds.

## 0.12.36

- Add searchable Protected apps checkboxes to Windows Settings. Saved protections override Optimus debloat selections and per-app removal rules.

## 0.12.35

- Record verified WinGet launch refusals and download failures per package so eligible updates can continue.
- Keep optional payload cleanup and read-only version checks separate from installer lifecycle reporting.
- Allow independent update follow-up when an app batch stops before launch and its other installer outcomes are known.

## 0.12.34

- Make hardware snapshot text selectable and add Copy report with source and confidence information.
- Allow explicit asynchronous hardware refresh during maintenance, with an isolated read-only worker, safe cancellation and fresh local history.
- Correct generic zero wear and HDD endurance interpretation; keep raw counters as evidence and preserve older records while unverified graph values become gaps.
- Expand available reliability counters, rated temperature limits, usage conversions, manufacture-week age estimates and bounded inbox battery-report fallback.
- Preserve genuine NVMe endurance estimates, including reported zero and over-100 values, separately from remaining-life predictions.
- Fit complete worker ring captions without ellipsis, including small windows and scaled fonts.

## 0.12.33

- Add automatic read-only battery and physical-drive health checks to maintenance, with worker summaries and a snapshot/history view.
- Show reported capacity retained, native condition and available wear, temperature, cycle, power-on and error measurements; unavailable values remain unknown.
- Record local per-scan trends, retain the first baseline and identify device replacements using opaque hardware hashes. Unknown readings create gaps and unidentified devices are not line-joined.
- Add scalable graphs with metric selection, baseline markers, per-scan tooltips and categorical condition charts without a fabricated health percentage.
- Bound queries and history storage; failures do not stop unrelated maintenance, and no repair, SMART self-test, firmware change or new monitoring service is started.
- Verify snapshot windows against each platform's available work area, including small headless Linux test screens.

## 0.12.31

- Continue unrelated automatic fixes after a proven finished security-control failure; preserve running, unknown and restart barriers.
- Install saved-approved Windows Security/Critical update recommendations through the native updater and verify exact installation history, with durable results.
- Recheck a healthy unchanged app version briefly after a successful finished installer, avoiding unnecessary pending-verification holds without replaying the installer.
- Retain approval and explain automatic future rechecks for drivers with no matching present device.
- Distinguish Apply, Install, Open and View recommendation actions, show readable outcome reasons and add supported-fix selection controls.
- Keep original recommendations accessible after an automatic follow-up exception without repeating the failed batch. Preserve saved preferences and approval scopes.

## 0.12.19

- Compare the explicitly approved effective settings at update handoff and resume while preserving the original saved-preference bytes. Older files can omit existing defaults without causing a false mismatch; changed effective choices or defaults still pause cleanup for review.
- Serialize startup so the Optimus release check and any approved update/restart finish before launch review or saved unattended maintenance can run.
- Resume cleanup only through a verified single-use approval for unchanged saved choices. A failure after accepting an Optimus update, or a restart without valid cleanup approval, leaves maintenance paused for explicit review.
- Add a red STOP control to main and progress headers. Latch it immediately, reject pending questions, cancel supported work and prevent queued follow-ups or automation from restarting the session.
- Keep native installer progress and exit status honest: wait for outstanding operations and result records instead of killing unknown processes or claiming an instant cancellation.
- Retire driver cancellation markers only after their identity is recorded, and prevent late STOP callbacks from recreating completed requests.
- Retain saved preferences, scoped approvals, recovery records and the Sandbox unsaved-work guard.
- Add independent Windows/native Qt checks for early startup cancellation, repeated STOP, native draining, Settings/question handling, late callbacks and responsive STOP controls.

Download sizes remain explicitly labeled with the release they describe until the next native assets are published and verified.
