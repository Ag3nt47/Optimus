# Updating Optimus before cleanup

Each normal graphical startup checks the latest stable release in the official **Ag3nt47/Optimus** GitHub repository. The bounded check runs outside the interface thread, before maintenance, Smart Cleaning, browser-exit cleaning and background sampling are approved. Smoke tests, startup probes and read-only command-line tools do not check for or install Optimus updates.

The startup gate is established before queued interface or automation callbacks can start work. Maintenance remains paused while the check, update question, verified download or restart handoff is pending. No newer release, an unavailable check or an explicit decline can continue to normal launch review. After an update is accepted, a download, verification, preparation or handoff failure leaves maintenance paused for explicit review. Saved unattended Auto Clean follows the same ordering.

When a supported newer Windows release is available, Optimus presents clearly labeled choices:

- **Yes — update then Auto Clean** shows the current saved removal, file and browser actions. It approves that Optimus update and only that unchanged cleanup scope after restart.
- **No — continue current version** continues the normal launch review. It does not approve cleanup by itself.
- **Adjust settings** keeps automatic work paused and opens Settings. Closing the update prompt does not approve an update.

A first-time user gets **Yes — update then setup** instead. Completing setup and reviewing its actual saved choices remains required before cleanup. An unavailable release check or an explicitly declined update can return to the existing launch behavior. A network or other failure after accepting an update keeps maintenance paused; the current version can still be used through an explicit review.

## Download and restart

The Windows updater downloads the exact stable release asset and checks both GitHub's asset SHA256 digest and the release's SHA256SUMS entry. It rejects downgrades, changed release identities and malformed or incomplete archives. Extraction has path, link, file-count, size and expansion limits. The complete application is staged beside the existing version; a running executable is never overwritten.

The updater retains native certificate roots and also loads the bundled, pinned Mozilla CA data from Certifi. HTTPS certificate validation and hostname checking remain required. An offline `--tls-probe` verifies that the packaged app can load the bundled roots even when default CA paths are unavailable. The probe makes no network request; it does not prove live GitHub reachability. No global SSL or operating-system trust settings are changed.

Release checksums detect corrupt or inconsistent downloads. They are not independent publisher code signing: the current Windows executable is unsigned, and the release account remains part of the trust boundary.

A separate handoff process waits for the exact old interface to exit normally and for maintenance ownership to be released. It does not kill processes or approve Windows dialogs. Unsaved Sandbox work can cancel the handoff. The verified new version starts through the normal administrator-consent path. Declining elevation retains the application's normal protected-operation limits.

A short-lived, owner-bound one-use handoff binds the verified executable, data location, saved preferences and cleanup scope. Only the expected restarted version can consume it. A verified single-use cleanup approval resumes the unchanged saved scope once. Without that valid approval, the restarted window stays paused for explicit review, including changed settings and invalid, expired or reused handoffs. No arbitrary executable path, command line or cleanup scope is accepted from GitHub metadata.

The approved cleanup snapshot contains the effective settings shown by the current interface, produced through the same validation and defaults used at resume. An older saved file may omit those existing default fields. The updater retains the raw file's exact SHA256 binding separately and does not rewrite it to add defaults. It never silently expands an older approval: the caller must explicitly supply the complete effective snapshot, and a changed effective scope or future default pauses cleanup instead of granting new permission.

Existing plain owned launch shortcuts and enabled sign-in registration are retargeted only when their current identity still matches the old installation. Their previous bytes or registration are retained for conditional recovery. Unrelated or changed launchers are left alone. Settings, journals, Sandbox source and the existing data-location selection remain in place; old release folders are retained for recovery.

The update has its own visible progress and cancellation. Maintenance has not started while download, verification or restart is pending. A failed or cancelled update does not claim success or silently approve cleanup.

The red **STOP** requests exit for the entire Optimus session. It latches before queued callbacks, cancels supported update work and prevents the normal fallback from starting Auto Clean. Pending restart approval must be revoked or accounted for before exit. STOP does not approve an update or bypass the Sandbox unsaved-work guard. See [startup and STOP](startup-and-emergency-stop.md).

After an update is accepted, failures before the original window closes leave that window's maintenance paused for explicit review. If a later handoff fails after that window has exited, Optimus preserves the old release and the private update/launcher records for manual recovery; it does not launch another installer or assert that cleanup ran. Reopen the retained original `Optimus.exe` to review the current state. An uncertain launch or partly changed launcher is never blindly replayed.

## Platform and scope limits

Automatic installation currently supports recognized writable Windows managed installations using an ordinary `Releases/<version>` folder beside the existing `Data` folder and data locator. macOS and Linux can check for the correct native release and offer its official download; installing it uses the normal platform process. Unsupported or unwritable Windows installations also use a manual route. There is no automatic OS reboot, forced installer termination, UAC bypass, package-manager replacement or installation of prerelease builds.

The startup release check contacts GitHub and release downloads contact GitHub's asset hosts. It sends no process inventory, cleanup history or private settings. It is separate from the existing software updater: application, driver and Windows update scopes retain their saved approvals and individual result records.

Implementation references: [GitHub stable releases API](https://docs.github.com/en/rest/releases/releases#get-the-latest-release) and [release asset download and digest metadata](https://docs.github.com/en/rest/releases/assets#get-a-release-asset).
