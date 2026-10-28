# Installer-download cleanup

**Automatically delete tracked installer downloads after verified updates** is on by default, with a toggle in setup and Settings. Reading older settings supplies this default without rewriting saved choices. An explicit off choice is preserved. Corrupt or unreadable settings do not authorize this cleanup. Changing this choice invalidates an existing unattended scope approval until the user reviews and saves it again.

This feature cleans temporary installer payloads created for an Optimus-owned update attempt, rather than treating the user's Downloads folder as disposable. It does not clean arbitrary old installer downloads, package-manager shared caches, installed applications, Windows Installer data or the Driver Store.

## Eligible Windows application updates

Supported WinGet updates receive child-only temporary-folder overrides pointing to a new private per-attempt payload folder. Global `TEMP` and `TMP` remain unchanged. Metadata and cleanup receipts live outside the disposable folder. A saved receipt or an old directory cannot recreate deletion authority after the process exits.

Cleanup requires the supported installer metadata, a fresh exact installed-version readback, successful native exit, verified exit of the tracked installer descendants, and no required or pending restart. Generic executables, portable or unknown installers retain payloads because some use their extraction directory as an installed home. A cleanup refusal does not fail an otherwise verified update or stop other eligible packages.

Only bounded ordinary single-link disposable payload files matching the freshly verified catalog's installer SHA256 are removed. The file is hashed again under its exclusive Windows deletion handle. Unknown extracted runtime, repair and uninstall executables stay out of cleanup even if their extension resembles an installer. Reparse points, redirected ancestry, changed identities, offline files, logs, audit evidence, receipts, rollback and uninstall material remain protected. Count, byte, depth and time limits bound each attempt. Remaining or inaccessible files are reported, not forcibly removed. Cleanup counts describe actual deleted files; preview sizes never count.

## Windows drivers and native package managers

Windows Update Agent owns its driver downloads. Its documented update API provides discovery, download, installation and cache-copy operations, but no proven per-update cache-deletion authority used by Optimus. Receipts therefore state **Windows-managed cache retained**, with zero files deleted. Optimus does not reset Windows Update services, purge SoftwareDistribution or remove Driver Store/backup files to claim a cleanup.

macOS/Linux providers retain their provider-managed caches unless an operation can establish private ownership and installer quiescence. The toggle is visible across platforms; unsupported cleanup remains explicit rather than silently deleting shared data.

## Reporting

Each applicable worker retains actual removal counts, completed installation counts and reported download bytes. The comparison beneath global progress shows deleted file sizes against downloaded bytes. Its percentages are shares of combined reported byte activity, not queue completion, speed, installed disk footprint or a guaranteed amount of space recovered. Unknown sizes remain unknown; approximate native counters and incomplete downloads are labelled. Repeated ticks and final receipts do not add the same attempt twice.

References: [WinGet installation flow](https://github.com/microsoft/winget-cli/tree/master/src/AppInstallerCLICore/Workflows), [Windows temporary-path resolution](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-gettemppathw), [Windows Update IUpdate interface](https://learn.microsoft.com/en-us/windows/win32/api/wuapi/nn-wuapi-iupdate).
