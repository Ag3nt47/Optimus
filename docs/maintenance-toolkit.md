# Optional maintenance toolkit

Optimus 0.12.0 adds a **Maintenance toolkit** page in Settings and a matching
question page in initial setup. Existing saved choices are preserved. Every new
automatic scope starts off; enabling one does not approve the others. The regular
launch review lists enabled actions, and changing these choices invalidates an
older unattended approval.

## Workload-aware starts

The Workload tab can defer new maintenance actions above selected CPU or memory
thresholds, outside a daily time window, before a selected idle period, on battery,
while another application is fullscreen, or on a metered connection. Windows uses
native idle, power, foreground-window and connection-cost observations. Unknown
required observations defer the action with a reason.

Checks run at action entry and again before an update obtains its installer slot.
An installer already running is allowed to finish. These are start conditions;
they do not impose CPU, disk or network quotas or suspend vendor installers.
Fullscreen detection is best effort across mixed-DPI displays. A deferred action
is recorded and the remaining eligible work continues. A later approved run or
Smart Cleaning tick can retry when conditions improve.

## Clean exact application caches

App caches offers separate Discord, Spotify, Steam and Teams selections, an age
threshold, and entry, byte and elapsed-time limits. Select all selects applications;
it does not enable cleanup. Only exact rebuildable web-cache locations are eligible.
Saved accounts, personal data, downloaded media, Steam games and shader caches
are outside these recipes. Running applications, incomplete process inventories,
changed file identities and paths outside the reviewed scope are preserved.

Save choices, preview the eligible files, then apply the reviewed plan. A plan is
single use and expires; changing choices requires another preview. Automatic runs
create a fresh plan from the exact saved scope. Removed cache bytes are measured,
not inferred from RAM usage. See [cleanup and retention details](cleanup-storage-toolkit.md).

## Storage and recovery data

Storage growth measures only explicitly selected local subfolders, with bounded
depth, time and entry limits. Complete observations form a local comparison history;
partial scans do not replace a baseline. Reports show growth and observed ownership
evidence without claiming that a particular application caused every change.

Recovery data can expire completed exported report copies and, with a separate
acknowledgement, fully applied browser-cleanup backups older than the selected age.
Deleting those backups loses their undo ability. Original change journals, driver
backups, System Restore data, partial browser operations and safety snapshots stay
protected. **Archive last report copy** accepts only a health check or growth report
produced by the current tool instance; incomplete reports are rejected.

## Measurements and supported repair

Run a read-only health check for sustained CPU, memory, disk and network observations,
available disk reliability/battery data, Windows boot events, pagefile facts and
installer-volume headroom. Unavailable sensors and unknown download sizes remain
unknown. Installer headroom compares actual volumes and entered requirements; it
does not reserve disk space. Optional before/after observations form a bounded local
history using comparable hardware, sampling, power and activity contexts.

Windows integrity checking uses native DISM and SFC. **Repair Windows** has a
separate explicit confirmation and requires administrator access. It uses official
component repair; it neither downloads unofficial patches nor reboots the computer.
No pagefile, memory-compression, hardware-clock or disk-partition settings are changed
by these diagnostics. See [performance diagnostics](performance-diagnostics.md).

The read-only command is available without opening the interface or requesting UAC:

```powershell
.\Optimus.exe --toolkit check --output 'D:\OptimusReports'
.\source\scripts\Read-OptimusPerformance.ps1
```

Source checkouts must pass an installed executable to the script with `-Executable`.
`--toolkit status`, `--toolkit integrity-check` and `--toolkit resume` expose the
corresponding explicit tools. There is no headless repair shortcut. A successful
diagnostic exit means a report was produced; inspect its unavailable/error fields.

## Update continuation and additional packages

Reboot continuation is opt-in. Only known native restart-required outcomes create
a plan. After a real reboot, unchanged approvals and a clear live installer barrier
allow fresh update discovery. Commands from an old plan are never replayed. An
ordinary approved maintenance launch can complete that plan through its fresh run;
the explicit Resume command runs only saved update choices. Optimus never restarts
the computer or enables sign-in launch merely because continuation is selected.

An experimental Ollama-only option can accept an authenticated completed Inno
installer while its installed runtime remains alive. Its owning Optimus process
retains a passive console/job monitor until that runtime exits. It is off by default,
does not cover other vendors and does not make an unknown installer outcome safe.

An existing standard Chocolatey installation can update exact approved installed
package IDs from its official community source. It never installs Chocolatey or
missing applications. To avoid competing providers, automatic Chocolatey updates
defer while automatic WinGet/presence rules could own the same applications. See
[additional package updates](additional-package-updates.md) and
[update continuation](update-continuation.md).

## Verification

Cleanup and repair tests use disposable fixtures. Native read-only checks report
actual capabilities and resource observations. The test suite covers changed scopes,
process restarts, file replacement, interrupted scans, recovery lifecycle, installer
ownership, cancellation, narrow-screen layout and unsupported platforms. A green
workflow does not prove every sensor is available or every third-party installer
can update without a restart or vendor interaction.
