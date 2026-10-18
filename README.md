# Optimus: Computer optimization tool

An open-source Windows maintenance app with three main controls: **1 click Optimize and debloat**, **Settings**, and **Sandbox**.

![Optimus main screen](docs/optimus.png)

Optimus combines previewed cleanup, browser privacy tools, resource management, startup controls, debloat actions, disk analysis, duplicate finding and updates in one local application. Its dark blue/orange presentation, reveal animations and holographic window effects are adapted from the MIT-licensed [AngeronaSuite](https://github.com/Ag3nt47/AngeronaSuite).

## Download and run

Download **Optimus-Windows.zip** from [Releases](https://github.com/Ag3nt47/Optimus/releases). Extract the entire archive and open `Optimus.exe`. Keep `_internal` and `source` beside the executable. Python is not required for the portable build.

Windows 10/11, 64-bit. Start normally; administrator mode is available inside Settings for operations that need it. The Windows build is currently unsigned.

## Three controls

- **1 click Optimize and debloat** runs the modules selected in Settings. A live window shows actual completed-stage percentages, the current operation, process information and a detailed activity log. Cancellation stops at a safe boundary.
- **Settings** contains Low/Standard/High cleanup presets, individual removal choices, priority/affinity controls, optional background analysis, the full maintenance tools and appearance settings.
- **Sandbox** opens an editable working copy of the full source. Save, version history, Revert and Original version are available. **Restart Optimus** validates the saved Python source and starts that version; the current app remains open if the new interface does not report ready.

The default one-click configuration cleans old temporary files and caches of closed browsers. App removals, browser history/cookie deletion, Codex priority changes and background analysis require your choices. Existing Codex priority watchers are left unchanged.

## Tools

- System health and application-family CPU/memory reports.
- Optional background CPU, memory, disk-I/O and network measurements, with an Off/On control.
- Individual application priority, CPU affinity, recoverable sleep/wake and allocation history.
- Allowlisted temporary/cache cleanup with per-file identity checks, age limits and local logs.
- Chromium/Firefox privacy previews and private SQLite backups; passwords and bookmarks are preserved.
- Startup entry enable/disable/delete with backed-up restoration; third-party service controls.
- Installed applications and registered Windows uninstall access.
- Selected software updates through WinGet; signed-driver inventory and Windows Update driver tools.
- Bounded disk scans, SHA256 duplicate finding and confirmed single-file recycling.
- Selective stale HKCU App Paths review with backed-up removal and conflict-checked restoration.
- Optional personal OneDrive metadata duplicate review, using your own Microsoft app registration and a DPAPI-protected local sign-in.
- Curated consumer-app, Microsoft 365, Outlook and Edge removal plans. Store, security components, frameworks and WebView2 are preserved by the curated debloat actions.
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
.\.venv\Scripts\python.exe tools/package_source.py --source . --destination dist/Optimus/source
.\.venv\Scripts\python.exe tools/package_licenses.py --source . --destination dist/Optimus
```

The `source` folder is required for the Sandbox editor. Saved source executes with the current Windows account's permissions after an explicit restart; the editor is a source workspace, not an operating-system isolation boundary.

## Data and recovery

User settings, reports, operation journals, browser rollback copies and source history stay under `%LOCALAPPDATA%\Optimus`. They are not uploaded by Optimus. Cleanup permanently deletes the previewed cache files; duplicate removal uses the Recycle Bin. Application uninstalls and all debloat operations are not universally reversible. Each removal plan describes its recovery route.

No runtime reports, browser databases, machine inventories, credentials or proprietary CCleaner files are included in this repository. Network access is used by explicitly requested update and cloud operations.

## License

MIT for Optimus and the adapted Angerona presentation code. See [LICENSE](LICENSE) and [NOTICE](NOTICE) for third-party licenses. PySide6/Qt retain their own licenses.


