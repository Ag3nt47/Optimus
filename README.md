# Optimus: Computer optimization tool

An open-source Windows maintenance app with three main controls: **1 click Optimize and debloat**, **Settings**, and **Sandbox**.

![Optimus main screen](docs/optimus.png)

Optimus combines previewed cleanup, browser privacy tools, resource management, startup controls, debloat actions, disk analysis, duplicate finding and updates in one local application. Its dark blue/orange presentation, reveal animations and holographic window effects are adapted from the MIT-licensed [AngeronaSuite](https://github.com/Ag3nt47/AngeronaSuite).

## Download and run

Download **Optimus-Windows.zip** from [Releases](https://github.com/Ag3nt47/Optimus/releases). Extract the entire archive and open `Optimus.exe`. Keep `_internal` and `source` beside the executable. Python is not required for the portable build.

Windows 10/11, 64-bit. Start normally; administrator mode is available inside Settings for operations that need it. The Windows build is currently unsigned.

**Version 0.6.1** makes **Full Optimization** the new-install default. After first-start choices are saved it runs once whenever Optimus opens, until you change the mode or cadence. Full includes eligible temporary files at age 0, browser cleanup with close-or-skip prompts, the selected consumer-app removal list, supported update checks, Windows advertising/optional diagnostics preferences, a Defender quick scan, and final security/process recommendations. Startup can request administrator permission once; Windows still requires the UAC Yes click. Scans and smoke tests never request elevation or apply maintenance.

The workflow finishes with a bounded, read-only verification of recorded outcomes before presenting recommendations. It distinguishes confirmed effects, items needing review, unavailable evidence and operations that made no changes. Review the verification summary, then continue to the security and background-program choices. A completed worker operation alone is not proof that its requested effect occurred.

Software updates require a provably newer offered version; equal, older, equivalent and incomparable versions are skipped. Automatic app/driver/Windows Security/Critical update installation retains separate saved scopes and agreement acceptance. No feature upgrade or reboot is automatically requested. Vendor-only and legacy/community patch reports show source, applicability and licensing gaps; they do not install arbitrary unofficial drivers or promise support where no compatible patch exists.

**Version 0.5.0** adds **Select all / Clear all** to multi-choice setup groups, browser history/cookie/download-list options with per-browser **close and clean / skip** prompts, and a process cleanup harness with explicit, reversible selected actions. Existing saved settings do not enable the new privacy options. The reporting fixes from 0.4.1 remain included.

**Version 0.4.0** expands the driver updater with coverage for present hardware and future device approvals even when no update is currently offered. Software Updater can install selected applications or all supported WinGet applications with a separate saved approval and exclusions. Junk Clean adds optional diagnostics and graphics-cache categories. Performance measures application CPU, RAM and I/O across multiple samples and explains potential slowdown contributors. Existing duplicate, OneDrive and startup approvals and Bing Wallpaper protection are retained.

## Three controls

- **1 click Optimize and debloat** runs the modules selected in Settings. A live window shows actual completed-stage percentages, the current operation, process information and a detailed activity log. Cancellation stops at a safe boundary.
- **Settings** contains Low/Standard/High cleanup presets, individual removal choices, priority/affinity controls, optional background analysis, the full maintenance tools and appearance settings.
- **Sandbox** opens an editable working copy of the full source. Save, version history, Revert and Original version are available. **Restart Optimus** validates the saved Python source and starts that version; the current app remains open if the new interface does not report ready.

New installations start in Full Optimization. Setup displays the concrete selected consumer packages and browser categories before saving. Bing Wallpaper remains protected. Existing configurations keep their saved individual choices; choosing Full applies its preset once, and later changes are retained. Existing Codex priority watchers are left unchanged. If you previously enabled background analysis in the saved configuration, it resumes at that saved interval when the interface reopens. Scheduled/command-line audits do not start background analysis.

## First-start questions and automatic maintenance

![Optimus first-start questions](docs/optimus-setup.png)

The setup opens on the first normal GUI launch. It covers Health Check, Custom Clean, Uninstaller/debloat, Duplicate Finder, personal OneDrive review, Performance Optimizer, Startup Manager, Software Updater and Driver Updater. Cancel keeps your existing settings. **Save choices** stores the reviewed selections; **Run selected maintenance now** is a separate opt-in. Rerun the questions from Settings at any time.

Use **Select all** or **Clear all** for cleanup, debloat and performance options, or for the eligible entries in loaded startup, software and driver lists. Load an inventory first; software selection uses Selected mode, and hardware selection uses matching-device mode. The buttons do not accept agreements, grant automatic-change consent, change cadence or start maintenance. Changes to selected entries clear the related consent so you can review the new scope before saving. Existing 100-entry approval limits and the 2048 unique hardware-rule limit remain in effect; excess entries stay unchecked and the status explains why.

Custom Clean includes separate choices for caches, browsing history, cookies/site sign-ins and Chromium download-list records. Select which supported browsers to include and any cookie domains to keep. Each open browser gets its own Yes/No prompt during maintenance: **Yes** requests a normal close and cleans the selected data once every process exits; **No** skips that browser for this run. Optimus never force-kills a browser. History/cookie cleanup uses private database backups and excludes passwords, bookmarks and downloaded files; those backups retain the original records and are not secure erasure.

In **Settings → Performance → Process cleanup harness**, scan all process inventory rows and inspect measured resource use, parent/launch evidence, program ownership and protection reasons. Choose exact eligible actions, then **Clean up selected**. Supported harness actions pause a verified process or disable a freshly revalidated startup registration; they do not delete programs or guess that an old/high-memory process is unnecessary. Resume paused apps and supported startup History provide recovery. A read-only `--process-scan --output <folder>` harness export is available for local analysis, including Codex.

The command-line `--output <folder>` option selects the report destination for read-only audits, process scans and saved-run verification.

Choose manual operation, each Optimus startup, daily while Optimus is open, or weekly while Optimus is open. Daily/weekly timing is measured from the last attempt, including failures, to avoid repeated installer attempts. These modes do not install a background Windows service or run when Optimus is closed. The existing Task Scheduler feature remains a separate read-only audit.

Your selected cleanup, curated removals and Codex priority profile can apply automatically. Software Updater offers review, selected exact WinGet IDs, or all supported installed WinGet applications. All-supported mode needs its own explicit saved approval; excluded IDs remain unchanged. Each apply uses a fresh recognized source-checked scan and verified package identity. Pinned, ambiguous, unknown-version and unsupported applications are skipped. Package/source agreements require consent. Vendor installers may require administrator permission or close applications; Optimus does not request automatic reboots.

Duplicate, cloud, startup and driver modules each offer review mode or a saved apply approval. Local recycling uses your chosen folders, oldest/newest keeper, minimum age and per-run count limit; a retained copy is revalidated before each recycle. Personal OneDrive recycling is bound to the verified drive and concrete folders you approve. It additionally streams candidate contents for SHA256 comparison, limited to 64 MiB per file and 256 MiB per run; unsupported or oversized comparisons are skipped. Files go to the corresponding Recycle Bin; the bins are not emptied.

Startup automation disables only the exact registrations you selected, with their saved command/location/fingerprint still matching; it keeps recovery records. Driver automation can install exact approved update IDs/revisions or future Windows Update drivers matching the approved hardware ID, provider, class and date floor. Load present hardware in matching mode to approve eligible devices even when no update is currently offered. Hardware coverage distinguishes available offers, device problems, incomplete scans and no offer; no offer does not establish that the installed driver is the newest available from the manufacturer. Compatible hardware IDs can identify multiple devices of the same model. Quiet installation requires an already elevated Optimus session, current metadata and a successful driver export first. Pending-reboot, interactive, changed or unverified candidates are skipped. No automatic reboot is requested.

Each approval is optional and can be revoked by turning its module off or choosing review mode in setup. Revocation stops future changes; it does not restore items already recycled, disabled or installed. Use the Recycle Bin, History/startup restoration, Windows driver rollback or saved driver exports as applicable. Arbitrary app uninstalls and service changes still use their individual tools. Cloud connection is an explicit account action; cancelling the maintenance questions does not undo a connection you initiated.

The completion window records actual per-stage results, exclusions and failures. Pause automatic maintenance by choosing **Manual** in setup. Background resource sampling has its own saved On/Off choice.

## Tools

- System health and measured application-family CPU/memory/I/O reports with explanations and links to process controls.
- Optional background CPU, memory, disk-I/O and network measurements, with an Off/On control.
- Individual application priority, CPU affinity, recoverable sleep/wake and allocation history.
- Junk Clean with allowlisted temporary/cache files, optional user/system error reports, crash dumps and graphics caches; per-file identity checks, age limits and local logs.
- Chromium/Firefox privacy previews and private SQLite backups; passwords and bookmarks are preserved.
- Startup entry enable/disable/delete with backed-up restoration; third-party service controls.
- Installed applications and registered Windows uninstall access.
- Selected or explicitly approved all-supported software updates through WinGet, pin protection and exclusions; all-present hardware coverage and Windows Update driver tools.
- Bounded disk scans, SHA256 duplicate finding and confirmed single-file recycling.
- Selective stale HKCU App Paths review with backed-up removal and conflict-checked restoration.
- Optional personal OneDrive metadata duplicate review, using your own Microsoft app registration and a DPAPI-protected local sign-in.
- Curated consumer-app, Microsoft 365, Outlook and Edge removal plans. Bing Wallpaper, Store, security components, frameworks and WebView2 are preserved by the curated debloat actions.
- Optional integration with an existing Desktop `Specialized Computer Management/Remove-Edge.ps1`: preview and exact script-hash verification, followed by its own visible administrator/typed-confirmation flow.
- Read-only scheduled reports, local history and supported setting restoration.

See [the user guide](docs/USER_GUIDE.md) and [feature coverage](docs/FEATURES.md) for operation-specific behavior. Optimus is an independent implementation, not a patched CCleaner executable or a claim of identical vendor catalogs, cloud integrations or native algorithms.

## Build from source

Install Python 3.12, then run:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[build,test]"
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m optimus
```

`Install-Source.bat`, `Start-Optimus.bat` and `Build-Windows.bat` provide the same basic workflow. Exact dependency versions used for the release build are in `requirements-build.txt`.

To build the portable executable:

```powershell
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --windowed --onedir --name Optimus --icon src/optimus/assets/optimus.ico --paths src --collect-all optimus --collect-submodules psutil --add-data "LICENSE;." --add-data "NOTICE;." launch.py
.\.venv\Scripts\python.exe tools/normalize_bundle.py --bundle dist/Optimus
.\.venv\Scripts\python.exe tools/package_source.py --source . --destination dist/Optimus/source
.\.venv\Scripts\python.exe tools/package_licenses.py --source . --destination dist/Optimus
```

The build normalization step checks Qt's required Windows ICU symbols and removes only a recognized conflicting ICU library collected from the build PATH. It refuses unknown dependencies and never changes Windows system files.

The `source` folder is required for the Sandbox editor. Saved source executes with the current Windows account's permissions after an explicit restart; the editor is a source workspace, not an operating-system isolation boundary.

## Data and recovery

User settings, reports, operation journals, browser rollback copies and source history use `%LOCALAPPDATA%\Optimus` by default. A relocated installation can place `Optimus.location.json` beside `Optimus.exe`, with exactly `{"schema":1,"data_dir":"D:\\OptimusData"}`. The target must be an ordinary absolute local folder. Invalid, indirect or oversized configuration is rejected; it never silently falls back to another folder. Sandbox restarts use the original installation location too. Machine-specific location files are excluded from public downloads. They are not uploaded by Optimus. Cleanup permanently deletes the previewed cache files; duplicate removal uses the Recycle Bin. Application uninstalls and all debloat operations are not universally reversible. Each removal plan describes its recovery route.

No runtime reports, browser databases, machine inventories, credentials or proprietary CCleaner files are included in this repository. Network access is used by explicitly requested update and cloud operations.

## License

MIT for Optimus and the adapted Angerona presentation code. See [LICENSE](LICENSE) and [NOTICE](NOTICE) for third-party licenses. PySide6/Qt retain their own licenses.


