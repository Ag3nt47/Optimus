# Updating Optimus before cleanup

Each normal graphical startup checks the latest stable release in the official **Ag3nt47/Optimus** GitHub repository. The bounded check runs outside the interface thread, before maintenance, Smart Cleaning, browser-exit cleaning and background sampling are approved. Smoke tests, startup probes and read-only command-line tools do not check for or install Optimus updates.

When a supported newer Windows release is available, Optimus presents clearly labeled choices:

- **Yes — update then Auto Clean** shows the current saved removal, file and browser actions. It approves that Optimus update and only that unchanged cleanup scope after restart.
- **No — continue current version** continues the normal launch review. It does not approve cleanup by itself.
- **Adjust settings** keeps automatic work paused and opens Settings. Closing the update prompt does not approve an update.

A first-time user gets **Yes — update then setup** instead. Completing setup and reviewing its actual saved choices remains required before cleanup. An unavailable check, network error or refused update returns to the existing launch behavior; it does not prevent use of the current version.

## Download and restart

The Windows updater downloads the exact stable release asset and checks both GitHub's asset SHA256 digest and the release's SHA256SUMS entry. It rejects downgrades, changed release identities and malformed or incomplete archives. Extraction has path, link, file-count, size and expansion limits. The complete application is staged beside the existing version; a running executable is never overwritten.

Release checksums detect corrupt or inconsistent downloads. They are not independent publisher code signing: the current Windows executable is unsigned, and the release account remains part of the trust boundary.

A separate handoff process waits for the exact old interface to exit normally and for maintenance ownership to be released. It does not kill processes or approve Windows dialogs. Unsaved Sandbox work can cancel the handoff. The verified new version starts through the normal administrator-consent path. Declining elevation retains the application's normal protected-operation limits.

A short-lived, owner-bound one-use handoff binds the verified executable, data location, saved preferences and cleanup scope. Only the expected restarted version can consume it. Changed settings or an invalid, expired or reused handoff return to normal review. No arbitrary executable path, command line or cleanup scope is accepted from GitHub metadata.

Existing plain owned launch shortcuts and enabled sign-in registration are retargeted only when their current identity still matches the old installation. Their previous bytes or registration are retained for conditional recovery. Unrelated or changed launchers are left alone. Settings, journals, Sandbox source and the existing data-location selection remain in place; old release folders are retained for recovery.

The update has its own visible progress and cancellation. Maintenance has not started while download, verification or restart is pending. A failed or cancelled update does not claim success or silently approve cleanup.

Failures before the original window closes return to normal launch review. If a later handoff fails after that window has exited, Optimus preserves the old release and the private update/launcher records for manual recovery; it does not launch another installer or assert that cleanup ran. Reopen the retained original `Optimus.exe` to review the current state. An uncertain launch or partly changed launcher is never blindly replayed.

## Platform and scope limits

Automatic installation currently supports recognized writable Windows managed installations using an ordinary `Releases/<version>` folder beside the existing `Data` folder and data locator. macOS and Linux can check for the correct native release and offer its official download; installing it uses the normal platform process. Unsupported or unwritable Windows installations also use a manual route. There is no automatic OS reboot, forced installer termination, UAC bypass, package-manager replacement or installation of prerelease builds.

The startup release check contacts GitHub and release downloads contact GitHub's asset hosts. It sends no process inventory, cleanup history or private settings. It is separate from the existing software updater: application, driver and Windows update scopes retain their saved approvals and individual result records.

Implementation references: [GitHub stable releases API](https://docs.github.com/en/rest/releases/releases#get-the-latest-release) and [release asset download and digest metadata](https://docs.github.com/en/rest/releases/assets#get-a-release-asset).
