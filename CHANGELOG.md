# Changelog

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
