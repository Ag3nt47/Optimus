# Changelog

## 0.12.18

- Serialize startup so the Optimus release check and any approved update/restart finish before launch review or saved unattended maintenance can run.
- Resume cleanup only through a verified single-use approval for unchanged saved choices. A failure after accepting an Optimus update, or a restart without valid cleanup approval, leaves maintenance paused for explicit review.
- Add a red STOP control to main and progress headers. Latch it immediately, reject pending questions, cancel supported work and prevent queued follow-ups or automation from restarting the session.
- Keep native installer progress and exit status honest: wait for outstanding operations and result records instead of killing unknown processes or claiming an instant cancellation.
- Retire driver cancellation markers only after their identity is recorded, and prevent late STOP callbacks from recreating completed requests.
- Retain saved preferences, scoped approvals, recovery records and the Sandbox unsaved-work guard.
- Add independent Windows/native Qt checks for early startup cancellation, repeated STOP, native draining, Settings/question handling, late callbacks and responsive STOP controls.

Download sizes remain explicitly labeled with the release they describe until the next native assets are published and verified.
