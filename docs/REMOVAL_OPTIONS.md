# Cleanup, removal and privacy choices

Optimus focuses on local cleanup, explainable performance analysis, supported updates and privacy controls. The main menu has Optimize, Settings, Sandbox and a red **Revert** button; detailed tools and automation choices live in Settings. It contains independently written implementations, with no subscription required.

With unattended mode off (the default), normal GUI launches present **Auto Clean** and **Adjust settings**. The warning describes saved removal groups, cleanup categories and browser data before maintenance starts. **Adjust settings** is the default. Closing it leaves automatic work paused. First-time setup must be saved before cleaning. Read-only probes do not show questions or run maintenance.

The explicit **Auto clean with these settings** choice in setup/Settings enables sign-in launch and immediate startup maintenance for the exact approved configuration. It suppresses Optimus questions and leaves manual-only recommendations in results. Open browsers are skipped by default; alternatively approve graceful closing, which can lose unsaved form state. Operating-system administrator consent, quiet-installer eligibility and restart restrictions still apply. Changing saved scopes invalidates the stored approval; unrelated backend changes cannot silently approve additional removals. Turning the option off clears that approval.

## Application removal

| Choice | Actual action | Recovery and limits |
| --- | --- | --- |
| Selected consumer apps | Remove the selected supported Store packages listed below for the current account | Reinstall from the Store; app-local data is not restored automatically. Selecting Full initially chooses this list, which can be reduced before Auto Clean. |
| Microsoft 365 consumer apps | Remove `Microsoft.MicrosoftOfficeHub`, `Microsoft.M365Companions` and recognized `Microsoft.Microsoft365*` Store packages | Separate saved opt-in; this does not remove desktop Office. |
| New Outlook | Remove `Microsoft.OutlookForWindows` | Separate opt-in. Export anything needed and use the original provider to reinstall. |
| Microsoft Edge | Attempt the supported installer removal for the exact detected browser installation | Windows may refuse removal. WebView2 is preserved. Failure remains visible; it does not authorize folder deletion or security bypasses. |
| Desktop Microsoft Office / Microsoft 365 | Open Windows' uninstall controls for a reviewed Microsoft desktop product | A manual handoff, not verified automatic uninstall. Reinstallation requires the original account or organization deployment. |
| Provisioned optional Store apps | Separately preview and remove selected supported provisioning for future new accounts, where available | Administrator access; restoring provisioning needs original signed packages, dependencies and licenses. Not a universal block against future Windows feature updates. |
| Existing Desktop Edge script | Optional integration with an exact recognized user-owned script after preview and identity checks | Separate visible confirmation. Its forced removal and policy changes do not have complete undo. Never silently substituted for the supported Edge action. |
| Other desktop applications | Show installed inventory and open the supported uninstall route | No automatic judgment that an unfamiliar program is unwanted, and no general leftover-folder purge. |

Bing Wallpaper stays protected. Frameworks, security components, Microsoft Store support and WebView2 are excluded from curated consumer removal. News and Weather are separate apps from Bing Wallpaper.

The exact consumer list is:

<!-- consumer-package-list:start -->

- `Microsoft.BingNews`
- `Microsoft.BingWeather`
- `Microsoft.Getstarted`
- `Microsoft.MicrosoftSolitaireCollection`
- `Microsoft.Microsoft3DViewer`
- `Microsoft.Print3D`
- `Microsoft.MixedReality.Portal`
- `Microsoft.People`
- `Microsoft.WindowsMaps`
- `Microsoft.WindowsFeedbackHub`
- `Microsoft.549981C3F5F10`
- `Microsoft.SkypeApp`
- `Microsoft.Windows.DevHome`
- `Microsoft.MicrosoftPCManager`
- `MicrosoftCorporationII.Windows365`
- `MicrosoftCorporationII.MicrosoftFamily`
- `Clipchamp.Clipchamp`
- `MicrosoftTeams`
- `MSTeams`
- `Microsoft.Copilot`

<!-- consumer-package-list:end -->

An app's presence does not prove it tracks users or sells conversation data. Review publisher settings and account permissions for third-party services. Optimus does not inspect private conversations to classify programs.

## Cleanup and performance

| Tool | Available behavior |
| --- | --- |
| Temporary files | Saved age/category rules, redirected and original user temp locations, Windows temp and guarded identity/lock checks. Eligible temp deletion is permanent. |
| Optional diagnostic and graphics caches | Windows error archives/queues, application crash dumps, Windows minidumps and DirectX/NVIDIA/AMD shader caches. Old diagnostics can be useful; shader caches rebuild and can briefly stutter. These categories require selection. |
| Browsers | Selected cache, history, cookies and download-list records. Open browsers ask **close and clean** or **skip** separately. Passwords, bookmarks and downloaded files are excluded. Cookie cleanup can sign users out. Private rollback copies retain original browser records. |
| Opera | Available only when explicitly selected. Off in defaults and Full presets; existing browser selections are preserved. |
| Smart Cleaning | Optional background checks while Optimus is open; default junk threshold is 500,000,000 bytes. Uses saved categories, cooldowns and bounded previews, and defers for incomplete scans or active/uncertain work. Default off. |
| Browser-exit cleaning | Optional cleanup after an observed browser-process exit and grace period. Uses saved browser categories and cookie exceptions; never forces a browser closed. Polling can miss transitions. Default off. |
| Scheduling | Manual, each launch, daily, weekly or calendar-month cadence while open. Launch approval still applies. Optional sign-in launch is separate; no closed-app cleanup service is implied. |
| Process harness | Identify programs behind processes, sampled CPU/RAM/I/O, current parent/startup evidence and protected entries. Selected eligible pause/resume or startup-disable actions require current identity checks; no automatic executable deletion. |
| Performance changes | Supported power preferences with readback/undo, selected priority/affinity controls and media-aware SSD TRIM/HDD optimization. No partition edits, indiscriminate RAM flushing, automatic overclocking or guaranteed speedup. |
| Updates | Newer offered WinGet/native package updates within saved approvals; signed applicable Windows Update driver offers with quiet mode, backup and restart checks. No offer means no offer from the checked source, not universal freshness. |
| Local duplicates | SHA-256 comparisons, verified keeper and selected/saved folder-age-count limits before recycling. No personal-file removal just because it is large or old. |
| Cloud | Personal OneDrive with explicit user connection and scoped approvals. Google Drive, Dropbox and business/shared-drive cleanup are not implemented. |

## Privacy and reinstall controls

Supported Windows advertising, tailored-experience, optional diagnostic and suggestion controls have individual selection, saved previous values, readback and guarded undo. Optional silent consumer-install/cloud-promotion, cloud-clipboard and Recall controls are limited to the exact Windows versions, editions and existing settings reported by the scan. Unsupported or managed controls stay visible. See [Microsoft's Experience policies](https://learn.microsoft.com/windows/client-management/mdm/policy-csp-experience) and [Windows AI policies](https://learn.microsoft.com/windows/client-management/mdm/policy-csp-windowsai).

**Recall:** the supported policy asks Windows to stop saving snapshots and delete previously saved snapshots. That deletion is permanent; restoring the policy cannot restore those snapshots. Registry readback verifies the policy value, not snapshot erasure. Optimus requires supported Pro/Enterprise/Education/IoT editions and Windows build 26100 revision 3915 or later. Cross-device clipboard policy requires supported Pro editions or above; the consumer/cloud enterprise policies are not advertised as effective on Home/Pro. Home can use recognized existing user preferences where present.

These controls reduce the selected Windows behavior. They cannot prove that all third-party tracking has stopped, revoke data already uploaded, stop all companies from collecting data, or force every installer to ask Optimus first. Windows-required diagnostics, organization policy, browser/account choices and separate software updaters have their own controls. Optimus does not disable Defender, Windows Update, SmartScreen, UAC or secure driver-signing protections to make privacy or performance claims.

## Recovery and file tools

The main red **Revert** button collects recorded changes and supported undo choices. Undo selected changes or review all available undo actions. Only supported recovery paths can run; permanent file deletion, removed app data and other irreversible effects remain visible without an invented undo. Each available handler revalidates its own recovery record and current state.

Revert does not silently change saved maintenance settings. Disable the matching modules or unattended cleaning in Settings to prevent a later run from applying the same changes again.

**Recovery** provides verified third-party driver-package exports, hash checks and exact-device rollback to the previous driver retained by Windows. Rollback can disconnect the device and can require a restart; Optimus does not restart automatically. System Restore can list, create and delete one reviewed point. Applying restoration remains a clearly labeled Windows wizard handoff.

**File utilities** offers authenticated password encryption/decryption, checksummed splitting/joining and separately reviewed selected-file overwriting. Encryption creates a new file without deleting the original. Overwriting is irreversible and never part of Full/Auto Clean. SSD wear-leveling, snapshots, cloud copies and backups prevent a universal secure-erasure guarantee. Whole-drive wiping, free-space wiping and deleted-file recovery are not exposed workflows.

## Verification and remaining coverage

Final workflow verification distinguishes confirmed, failed, skipped and unknown outcomes. Additional tools report their own result and appear in worker status cards. Safe stop requests cooperative cancellation, waits for entered operations and preserves uncertain installer receipts. It never treats cancellation as proof that a vendor installer exited.

See [feature coverage](FEATURES.md), [one-click behavior](ONE_CLICK_COVERAGE.md), [platform differences](PLATFORMS.md) and [comparison/roadmap](COMPARISON-2026-10.md). Automatic foreground-wake sleep services, arbitrary leftover deletion, broad registry/forensic trace purges, every package manager, universal driver catalogs, VPN service, Android app and human priority support are outside the current implementation.
