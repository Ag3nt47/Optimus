# Optimus user guide

Optimus is a local Windows maintenance application. The main window has three controls: **1 click Optimize and debloat**, **Settings**, and **Sandbox**. Its settings window holds the detailed tools; the main screen stays simple.

## Start with your configuration

Open **Settings**, select **Settings** in its sidebar, and review **One-click configuration**. The default Standard configuration cleans temporary files and caches of closed browsers that meet the age limit. Consumer-app, Microsoft 365, Outlook and Edge removal are off. Codex priority changes and background analysis are also off.

Choose the intensity and the individual modules you want:

| Intensity | Cleanup age selected by the preset | Additional behavior |
| --- | --- | --- |
| Low | 30 days | Conservative cleanup. The workflow enforces at least 30 days. |
| Standard | 7 days | Uses your selected modules and age setting. |
| High | 1 day | Uses your selected modules; it does not automatically enable application removal or change security settings. |

The Codex profile is optional. Leaving it off preserves existing process priorities and external priority watchers. Higher priority changes CPU scheduling; it does not create more processing capacity or memory.

Save the configuration, return to the main window, and use **1 click Optimize and debloat**. Its progress window shows the actual current stage, description, worker/process information, log, and completed-stage percentage. A completed percentage does not mean every individual item succeeded: read the returned results, skipped items and failed stages.

**Cancel after current stage** requests a stop at a workflow boundary. A stage already applying changes must finish; cancellation does not roll back earlier stages.

## Use the detailed tools

**Dashboard** reads current Windows resource, storage, startup and program information. Low free space and high memory usage are observations to investigate, not a claim that every background process is unwanted. Process working sets can include shared pages, so adding them together can overstate unique physical memory use.

**Cleaner** lets you check categories, choose a minimum age, and preview individual file paths, sizes and ages. Review the list before choosing **Delete reviewed files** and typing the confirmation phrase. That action permanently deletes eligible listed cache files. The engine rechecks identities and skips changed, protected, redirected or in-use paths. Active browser caches are skipped. History, cookies, saved passwords and bookmarks are outside cache cleanup.

**Browser privacy** offers a separate record-count preview for supported browser profiles. Close the browser and its background processes, select the categories you want, and optionally list domains whose cookies should remain. The keep rule includes their subdomains. Clearing cookies can sign you out; clearing the download list does not delete downloaded files. Passwords and bookmarks are preserved. The apply step creates a private database backup, and the panel provides a separately confirmed restore operation. A restore can be refused if files have changed since the operation. Firefox download-list clearing uses the browser's native controls rather than the direct engine.

**Performance** shows process identity, executable path, memory, CPU, priority, logical CPU cores and I/O counters. Select **Full process details** for additional ownership and launch evidence. Parent-process and startup matches are evidence; Windows does not always expose the original reason a program was launched.

Use priority and affinity changes for a selected application only. Essential Windows and security processes are protected. **Pause selected app** suspends one eligible application process; it can become unresponsive and its committed memory is not automatically freed. Use **Resume paused apps** when finished. Codex and other protected process families can have stricter pause rules than priority rules.

Background analysis starts **OFF**. Turning it on enables a read-only sampler at the selected interval, from 2 to 60 seconds. The first CPU and I/O sample establishes a baseline. You can turn sampling off independently of the application's other tools. The one-click workflow starts it only when that module is enabled in configuration.

**Startup** manages supported Run registrations and Startup-folder shortcuts. An entry's presence is different from Windows' separate Startup settings approval state. Disable an optional entry to keep the application installed. Enable, disable and delete operations create supported restore records; protected entries are excluded.

The Startup tools also include **Services** for supported third-party services. Administrator access is required for changes. Protected and unknown service entries cannot be changed. **Stop** affects the currently running service; **Manual** and **Disabled** change its startup mode and do not stop an instance already running. Running dependent services cause a refusal rather than an automatic dependency shutdown. Review the recorded result and use **Restore/history** for supported prior-state restoration.

**Applications** inventories installed programs and opens the supported Windows uninstall workflow for your selection. Deleting an executable or program folder is not an uninstall method. Program removal may affect application data and is not generally reversed by an Optimus settings record.

**Updates** checks the Windows Package Manager catalog. Enter an exact package ID from the result to update that application after confirmation. Source agreements, catalog coverage, network access and individual installers affect availability and results.

**Drivers** separates installed-driver inventory from available Windows Update candidates. **Scan available driver updates** queries Windows Update Agent. Select a single title and revision to install after administrator access and explicit confirmation. **Back up installed drivers** exports supported third-party driver packages with Windows `pnputil`. Device settings and inbox Windows drivers may need other recovery methods. Windows Update, Device Manager and manufacturer tools remain available; Optimus does not maintain its own universal vendor-driver catalog.

**Disk analyzer** measures a selected local folder and shows large folders, files and file types. Scans are bounded and report incomplete coverage and access failures. Junctions, reparse points and online-only placeholders are skipped. File lengths are logical sizes; hardlinks, sparse files and shared storage can make a sum differ from reclaimable disk space.

**Duplicate finder** compares eligible local file contents using SHA-256. It offers a confirmed single-file Recycle Bin action, with identity and retained-copy checks. Equal contents do not establish that a file is unnecessary. Protected application/system locations and hardlinked names have additional restrictions.

**Debloat** provides curated optional-app actions. Preview an action, read its scope and recovery information, and check only the concrete targets you want. Nothing is selected by default. The apply step uses a typed confirmation and only the checked IDs from that preview. Microsoft 365 consumer apps, desktop Office, new Outlook, Edge and an optional existing Desktop Edge script are separate actions with different effects. An external script keeps its own visible administrator and typed-confirmation flow. Bing Wallpaper is protected from debloat. Store components, security software, frameworks and shared WebView2 are also preserved by the curated app groups.

Packaged maintenance uses native Windows PowerShell module locations, so a host application's inherited module path does not prevent Windows security modules from loading. This does not change your system or user environment settings.

**Registry review** is limited to supported current-user App Paths registrations whose local executable target is missing. Scan, inspect one concrete entry, and use the selected-entry removal only if you recognize it as obsolete. A missing target is evidence, not proof that the registration is unused. The tool revalidates a fresh preview and saves raw value types and security descriptors before removal. Supported restoration is available through History. It does not perform wholesale registry cleaning or remove system-wide COM, installer or shared-DLL registrations.

**Cloud drive** is an optional personal OneDrive integration. It requires your own Microsoft public-client application registration and device-code sign-in; Optimus does not supply another application's client ID or ask for your Microsoft password. Configure an application that supports personal accounts and the delegated permissions described by the panel. Tokens are encrypted for the current Windows user.

Cloud scans read metadata, not file contents, and do not hydrate local placeholders. Size plus a provider SHA-1 or QuickXor hash identifies a candidate group; it is not a collision-free byte comparison. A reviewed action recycles one file after checking its identity and an observed retained copy. Recovery uses the personal OneDrive website Recycle Bin. Business/shared drives, SharePoint, Google Drive and Dropbox are not supported. Real account connection is optional and is not exercised by the offline fixture tests.

**Schedules** creates read-only daily audit tasks for your Windows account. Times use the computer's local clock. It does not schedule destructive cleanup, application removal or updates automatically.

**Restore/history** shows recorded operations and the available supported setting restores. Read the operation's result and recovery information: cache deletion, app removal, browser database rollback, file recycling and Windows driver rollback have different recovery paths.

## Administrator access and appearance

Use **Settings > Appearance and Windows tools > Restart as administrator** for a system-wide action that needs elevation. The elevated window is a separate process; the current window stays open. A read-only inventory can be useful in a normal user session even when some protected details are unavailable.

The appearance page contains the Angerona-derived dark themes, reduced-motion control and holographic minimize/restore behavior. Swirling stars pause while the main window is hidden or minimized. Windows' reduced-motion preference is also respected. Closing a window can collapse it to the orb; use the tray's **Quit Optimus** or the settings **Exit Optimus** control to exit. Active operations must finish or reach a cancellation boundary before exit.

## Edit the source with Sandbox

**Sandbox is an editable source workspace, not an operating-system isolation boundary.** Saved code runs with the current Windows account's permissions after you explicitly restart it.

1. Open **Sandbox**. It seeds a separate working copy of supported source text, launchers, configuration and documentation. Existing working-copy edits are retained.
2. Select a file in the source tree. Edit it and use **Save** or Ctrl+S. Python files must parse before they are saved; this checks syntax, not runtime correctness. Other text formats need their own validation.
3. Use **Revert** for the previous saved content, **History** to choose a revision, or **Original version** to copy the packaged release version back into the working copy. Replacing a file retains its previous version. Up to 50 saved revisions per file are kept.
4. Choose **Restart Optimus** to use the saved working copy. All saved Python source is checked first. A new process must report the expected readiness marker within the restart window; the current app remains open if that does not happen. Every Optimus operation must finish before restart, including a Windows request that remains pending after a timeout.
5. Use **Export project** to copy editable source to an empty destination for work in another editor or a normal development checkout.

The release source and the working copy remain separate. Restoring an original file changes the working copy; it does not alter the installed release. Indirect source, working-copy and history paths are excluded to prevent following junctions outside their intended folders.

## Local data and useful boundaries

Settings, reports, operation logs, supported backups and Sandbox history live under `%LOCALAPPDATA%\Optimus`. Keep this data private: reports can contain executable paths, startup commands, device details and account/profile metadata. A browser rollback copy contains browser data and needs the same care as the profile it backs up.

Optimus does not promise a fixed performance gain. Use the measured results to choose what to change, then compare your normal workload. See [feature coverage](FEATURES.md) for the direct engines, Windows integrations and unsupported areas.




