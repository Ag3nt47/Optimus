# Optimus feature coverage

Optimus implements independent Windows maintenance tools. This table explains the current behavior and recovery limits; it does not claim identical CCleaner rule databases, native algorithms, service protocols, driver catalogs or subscription coverage.

**Direct** means Optimus implements the operation. **Windows integration** means it uses a supported Windows/package-manager mechanism. **Optional external integration** requires an account, third-party tool or an existing user-provided script.

| Capability | Implementation | Current scope and practical limits |
| --- | --- | --- |
| Health/resource review | Direct, using Windows APIs | Current CPU, memory, free space, processes, program totals, startup and inventory notes. No synthetic overall health score or guaranteed speedup. |
| One-click workflow | Direct | Runs only configured stages with actual stage-count progress, descriptions, process/log details and cancellation boundaries. Removal modules, Codex changes and background sampling are off by default. |
| Temporary/junk cleanup | Direct | Allowlisted temporary files, error reports, crash dumps and shader/browser caches, subject to category, age, identity, active-process, lock and path checks. Permanent deletion is limited to a fresh reviewed plan. |
| Browser cache cleanup | Direct | Recognized local Edge, Chrome, Brave, Vivaldi and Firefox cache profiles. Active browsers are skipped; cache cleanup preserves history, cookies, passwords and bookmarks. |
| Browser history, cookies and download-list cleanup | Direct plus browser-native fallback | Supported Chromium/Firefox SQLite stores, selected categories, optional kept cookie domains, count previews and backup-based rollback. Firefox downloads use native browser controls. Passwords/bookmarks are excluded. |
| Background analysis | Direct; opt-in | CPU, RAM, process memory, disk I/O and network counters at 2-60-second intervals. Starts off and never automatically stops applications. First samples establish baselines. |
| Process scheduling and allocation | Direct | Verified process identity, priority, logical CPU affinity, protected-process rules, allocation history and supported restoration. Higher priority or fewer cores is not automatically faster. |
| Application pause/resume | Direct | Selected eligible application-process suspension/resume with saved identity checks. It is not the same as CCleaner's private sleep engine and does not promise memory reclamation. |
| Codex priority profile | Direct; optional | The configured Normal/AboveNormal/High target is applied only when explicitly requested. Existing priorities and external priority watchers are otherwise preserved. |
| Startup management | Direct | Supported Run registrations and Startup-folder shortcuts, with backups and conflict-aware restore. Registration presence is separate from Windows Startup approval state. |
| Third-party service controls | Direct/Windows integration | Supported protected-service checks and recorded stop/start-mode changes. System-critical and security services are excluded; device/application effects still need review. |
| Installed applications and removal | Windows integration | Registered desktop/Store inventory and the supported uninstall workflow. No general-purpose post-uninstall leftover detector or raw program-folder deletion. |
| Software updates | Windows Package Manager integration | Read update results and install one exact reviewed package ID. Catalog availability and installer behavior are controlled by the package provider. |
| Driver inventory and updates | Windows integration | Installed signed-driver inventory, Windows Update Agent candidate scan, confirmed individual update installation, and third-party driver-package export with pnputil. No independent all-vendor catalog or blanket driver replacement. |
| Disk analysis | Direct | Bounded selected-folder scans with large files/folders, file-type totals, incomplete/error reporting and reparse/placeholder exclusions. Logical sizes are not guaranteed reclaim estimates. |
| Local duplicate finding | Direct | SHA-256 content groups and confirmed single-file recycling with identity/retained-copy checks. Matching contents alone do not make a file disposable. |
| Curated Windows debloat | Direct/Windows integration | Concrete preview and per-target selection for curated consumer apps, Microsoft 365 consumer packages, Outlook, Edge and desktop Office workflows. Recovery varies by action. Bing Wallpaper, frameworks, security components, Store support and WebView2 are preserved by curated groups. |
| Existing Desktop Edge script | Optional external integration | Available only when the recognized user-provided script exists. Preview and script identity checks precede its own visible elevated/typed-confirmation workflow. It is not silently run by the normal Edge action. |
| Registry review and selective repair | Direct, narrowly scoped | Missing local executable targets in supported current-user App Paths registrations. Reviewed single-key removal retains raw value types and security descriptors for guarded restore. No HKLM, COM/CLSID, shared-DLL, installer or wholesale registry cleaning. |
| Personal OneDrive duplicate review | Optional external integration | User-registered public-client device-code sign-in, encrypted current-user token storage, bounded metadata scan and confirmed one-file recycling. Provider hash/size matches are candidates, not downloaded byte comparisons. Restore uses OneDrive's website bin. |
| Cloud provider coverage | Limited | Personal OneDrive only. Business/shared drives, SharePoint, Google Drive and Dropbox are unsupported. Real account behavior requires an explicit user connection and has not been exercised by offline mocks. |
| Scheduling | Windows Task Scheduler integration | Per-user daily read-only audits. No automatic scheduled app removal, driver replacement or destructive cleanup. |
| Recovery/history | Direct plus native recovery | Supported setting undo records, browser database rollback copies, local/OneDrive Recycle Bins and vendor/Windows reinstall or rollback routes. There is no universal undo for all operations. |
| Source Sandbox | Direct | Editable source working copy, Python AST save validation, bounded history, Revert, Original version, export and ready-checked restart. It is not an OS/container security sandbox. |
| Themes and motion | Direct | Adapted MIT Angerona themes, real-content reveal/reverse-close transitions, holographic orb minimize/restore and a pausing starfield. Reduced motion is supported. |
| Secure overwrite, free-space wiping and deleted-file recovery | Not implemented | Normal cleanup/recycling does not securely erase storage, wipe unused sectors or recover deleted files. |

## How to assess a result

A scan is a preview or observation. An apply operation has its own confirmation, validation and recovery route. Reparse points, access failures, active applications, expired plans and changed identities can cause exclusions or refusal. A bounded scan must not be interpreted as a complete disk/account inventory.

Packaged maintenance operations use the native Windows PowerShell module search path to avoid module-loading failures from an inherited host environment.

Windows Update, WinGet, the Microsoft Graph connection and external scripts can require network access, agreements, permission changes or a restart. Local analysis, cache scanning and source editing do not require a cloud connection. No account is contacted merely by constructing the Cloud panel.

Core behavior is covered by disposable local fixtures, mocked external services and review-UI payload tests. Those tests do not establish successful live driver installation, real-account cloud authorization, every vendor uninstaller, or every Windows/hardware combination. Actual operations return logs and per-item results so their outcome can be assessed.

## Implementation map

The public source includes the [desktop UI](../src/optimus/ui.py), [cleanup engine](../src/optimus/cleaner.py), [browser privacy engine](../src/optimus/browser_privacy.py), [resource controls](../src/optimus/performance.py), [workflow and preferences](../src/optimus/workflow.py), [maintenance coordination](../src/optimus/maintenance.py), [personal OneDrive integration](../src/optimus/cloud_drive.py), [source workspace](../src/optimus/source_workspace.py) and [PowerShell engines](../src/optimus/engine/). Operation-specific instructions are in the [user guide](USER_GUIDE.md).

