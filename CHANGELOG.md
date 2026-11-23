# Changelog

## 0.12.53

- Show a live startup scan window for driver updates, app updates, the Defender
  scan, Zero Breach and tracker checks. Each selected job shows its current
  status and result; closing the window hides it while work continues.
- Reopening Optimus from its tray icon, shortcut or Optimize button restores
  the running startup scan instead of leaving it invisible in the background.
- Stop, quit and restart wait for startup operations to save their outcomes.
  Failed, skipped and cancelled checks remain visible in the results.

## 0.12.52

- Add background app cleanup (`background_apps`). A read-only scan finds
  non-essential Windows background apps by image name *and* expected install
  location: Bing Wallpaper and its Temp-folder `UnInstDaemon`, Phone Link,
  Widgets/Start feed and Cross-device resume. Selected items are stopped only
  after PID, creation time and executable are rechecked.
- Hidden PowerShell/WScript watchers that use 150 MB+ or whose parent has
  exited are reported for review only; Optimus never stops them.
- Each cleanup writes a private receipt to `Data\background-apps`, which Change
  History now lists. Receipts record whether Optimus or an outside tool did it.
  Startup entries (e.g. `BingWallpaperDaemon`) are reported, never edited.

- Restart and startup: the driver updater, app updater and Defender virus/
  spyware scan now re-run every time Optimus starts or restarts
  (`startup_jobs`), independent of the Auto Clean launch review. Each job
  still uses its own saved approvals. Interrupted installer journals are
  reconciled first, and a busy or failed pass retries after 15 minutes.
- New tray action **Restart Optimus** stops work safely and relaunches with
  the same access level, so admin-only driver and Defender work keeps running.
- Pressing X while work runs hides Optimus to the tray instead of cancelling.
  The progress window's X only hides it; Cancel still stops the run. Once
  everything is done, X closes Optimus normally.
- **Apply all fixes without review** (`apply_all_fixes_without_review`): after
  a run, every automatic and reviewed fix is applied in the background,
  including Defender threat removal with a fresh threat-list consent. Then one
  summary is shown in the progress window. No Security review, process Yes/No
  or Settings windows open, and the app can close afterwards. Processes are
  never ended automatically; "Open Windows Security"-style steps are listed.
- Zero Breach Toolkit (`zero_breach`): Optimus keeps its own copy current from
  GitHub releases. A download is accepted only when it matches both GitHub's
  per-file SHA-256 digest and the release's `SHA256SUMS.txt`. It then runs the
  read-only quick scan at start. Consilium's pinned copy is never modified.
- Hidden recorder / tracker blocking (`audio_trackers`): reads Windows'
  microphone-usage records and Run keys. Unsigned programs in user-writable
  drop folders that used the microphone are blocked automatically: the process
  is ended, outbound traffic is firewalled, microphone access is denied, its
  autostart is removed and the file is quarantined. Every step is recorded and
  can be restored. Script hosts and signed programs in drop folders are
  review-only.
- New maintenance action **Uninstall Windows App** removes Windows App
  (Windows 365 / Remote Desktop client) and its Teams VDI packages on explicit
  request. Presets still never select it.
- Edge removal: setup exit 93 ("Browser/WebView is sticky") is now retried
  once with a temporary empty legacy-Edge stub under `SystemApps`, which is
  removed afterwards. Setup reads the device region, so the EEA user-region
  retry alone could not succeed.
## 0.12.51

- A finished update record that asked for a restart no longer blocks app
  updates, driver updates and Edge removal once Windows has booted after it.
  Windows can keep its own restart-pending flags set across restarts, which held
  installs back indefinitely ("A previous update requested a restart; its
  cleared restart state has not been verified").
- Add `tools/Clear-OptimusUpdateBlock.ps1`, which reports stale update records
  from before the current Windows boot and, with `-Apply`, archives them (never
  deletes) and holds the exact package version an unfinished record was
  installing.

## 0.12.50

- When Windows still refuses Edge browser uninstall after AllowUninstall (setup
  exit 93), retry once while the user and default-profile region read as an EEA
  country (Ireland), then restore the exact previous region values. No system
  file, ownership or permission is changed. The end of Microsoft's installer log
  is attached to a refused result.
- Release an orphaned app-update record after a verified newer Windows boot even
  when Windows servicing reports another restart pending in that newer boot. This
  pending restart kept blocking app, driver and Edge removal stages.

## 0.12.49

- Edge removal now also removes the Edge-based Copilot app (registered Microsoft
  uninstaller) and unused EdgeCore engine files, and deletes the leftover Edge and
  Copilot folders after a verified uninstall. Folders are deleted only when no
  updater-registered product uses them and nothing runs from them; WebView2, the
  Edge updater and browser profiles are kept.
- When Windows refuses Edge uninstall for the region (setup exit 93), retry once
  with the Edge updater AllowUninstall value and restore it afterwards. Region
  settings are never changed. The registered --channel=stable argument is passed.

## 0.12.48

- Fix App updates and Driver updates staying "install blocked" indefinitely after
  Optimus closed while an approved WinGet package was being prepared. After a
  verified Windows restart, that orphaned record is reconciled: only its exact
  package and version are placed on the saved update hold for review (never
  retried or reported as installed), and all other approved updates continue.

## 0.12.47

- Add a Review button to the App updates and Driver updates workers when they stop
  with needs review, blocked, failed or approval-required states. It opens the full
  recorded reason, search errors and each offer's outcome, with Proceed with update
  (runs only already-approved updates through the existing continuation) and Deny
  (installs nothing; records the decision and leaves approvals and journals intact).
- Clean macOS-only locations, on by default and added once to existing settings:
  crash and diagnostic reports, app logs (both keep the last day), the Homebrew
  download cache, Xcode DerivedData and device support files, Simulator caches,
  and downloaded iPhone/iPad software update images. Unticking one is kept.
- Ship the Optimus artwork as the macOS app icon instead of PyInstaller's default.
- Never register an App Translocation copy for sign-in launch; report such an
  existing entry as stale instead of enabled.
- Inventory macOS applications through Spotlight and standard nested application
  folders, retain malformed bundles as bounded metadata-only rows, and exclude
  embedded helper apps from the installed-application count.
- Inventory installed macOS extensions and include exact eligible Apple Software
  Update offers in the existing saved native update scope alongside Homebrew.
  Major macOS upgrades and automatic restarts remain outside that scope.
- Add an explicit one-click macOS setup for exact Mac App Store updates through
  the official `mas-cli/mas` 7.0.0 release. The unsigned upstream package and
  embedded executable are hash-pinned, installed only for the current user and
  reverified before use; vendor-only apps without a supported provider remain
  inventory-only.
- Add an opt-in Windows updater for the explicitly registered twelve-app ArtCraft portable suite, using exact official stable GitHub x64 ZIP releases with SHA-256 and size verification.
- Defer running Craft apps, stage replacements, preserve portable settings/recovery and non-release files, and retain prior app directories with journaled interrupted-swap recovery.
- Add a separate Craft-only command for explicitly configured sign-in checks; saved Craft and Software Updater choices control it without launching the interface or other maintenance.

## 0.12.46

- Add **Empty the Recycle Bin/Trash each run** (Windows Recycle Bin, macOS and Linux Trash), on in Full Optimization. It runs before duplicate recycling, reports measured item and byte counts, and is verified from its own readback.
- Load the Settings process inventory about four times faster on Windows (7.3 s to 1.7 s with about 290 processes) by reading the parent-process map once per census.
- Add CodeQL, Dependabot, Gitleaks and Ruff defect checks to GitHub.

## 0.12.45

- Verify the Optimus sign-in task after Windows rewrites its stored XML without default elements or in another order; this previously blocked launcher updates and sign-in changes.
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
