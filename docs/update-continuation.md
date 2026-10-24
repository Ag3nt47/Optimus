# Continue approved updates

Reboot continuation and experimental Ollama resident-runtime handoff are optional
and off by default. The experimental handoff can leave a passive Optimus owner
process running after the window closes, until the vendor runtime exits. It is
intended to release an update slot, not reduce the memory footprint after close.

Run `Optimus.exe --continue-updates --output "D:\OptimusReports\Updates"` after the current run finishes, or use the portable helper:

```powershell
.\source\scripts\Continue-OptimusUpdates.ps1 -Executable 'D:\Optimus\Optimus.exe'
```

With Optimus 0.11.12 or later, the packaged `source\scripts\Continue-OptimusUpdates.bat` launches the helper with a double-click and finds the executable in its package automatically.

The helper requests normal Windows administrator approval once if needed. `-StandardUser` suppresses this request; protected updates may then be deferred. It prints the folder containing live `.progress.json` and final `.json` reports, which remain inspectable even though Optimus is a windowed executable. `-ReportDirectory` selects another ordinary local folder.

This command reads the current saved app/driver switches, package selections, device rules and agreements. It never enables disabled updates, changes preferences, runs cleanup or debloat, or executes always-install/always-uninstall app presence rules. Always-update and always-ignore update rules still apply. Native macOS and Linux installations use their approved software updater; Windows driver updates are unavailable there.

App and driver discovery run concurrently; actual installers share the existing exclusive installation lane. Exact held offers and known package-specific failures are recorded and skipped so the next eligible package can proceed. Completed native updates with sufficient exact evidence can be reconciled automatically through the existing recovery service. Journals and individual offer holds are retained.

An active maintenance run, an installer with an unknown outcome, a pending native operation, or a required restart can still prevent conflicting installations. The script neither kills nor blindly restarts installers. A final `completed_with_skips` status means the queue drained while individual offers need attention; it does not claim those offers were installed. Exit code `0` means `completed`, `completed_with_skips`, or `no_enabled_updates`. Exit code `2` means blocked, incomplete, cancelled or unavailable; consult the report.

## Continue after your restart

When enabled in settings, Optimus records a durable update-only continuation
intent after a known terminal installer explicitly reports that a restart is
required. You choose when to restart Windows; Optimus never restarts it for you.
On the next launch, use the resume action to recheck the saved intent. A new OS
boot, unchanged update approvals and a clear current installer/restart barrier
are required before fresh discovery can proceed. The whole toolkit approval
snapshot is compared, so enabling another package source invalidates an older
intent rather than silently broadening it.

The plan stores approvals rather than executable commands or downloaded paths.
The ordinary updater rescans installed versions and currently offered updates,
then skips already current packages and individual held offers. It does not run
cleanup, debloat or application presence rules. A started continuation with no
terminal receipt is retained for review and is never replayed on another launch.
No scheduled task, service or background daemon is installed by this feature.

An eligible intent is also consumed automatically inside the next approved
normal optimization request. That request runs its normal fresh workflow once
under the same maintenance lease, and the intent is completed only after the
workflow returns. It does not create another update queue. Any cleanup in that
workflow needs the normal current launch approval or saved unattended scope;
the update-only reboot intent grants no additional removal permissions. The
separate resume action continues to run updates only.

## Experimental Ollama installer handoff

This opt-in feature supports the WinGet `Ollama.Ollama` silent Inno installer
case. It requires an exact successful command, a hash-verified native installer,
its closed success/no-restart log, the current registered target version and
installation directory, and a complete native private-job inventory. Every
remaining image must be an exact `ollama app.exe` or `ollama.exe` file installed
by that same installer; no other client may remain attached to Optimus's console.

An authenticated **resident handoff** receipt then allows the next approved
installer to proceed while Ollama keeps running. That receipt explicitly says
the job is still active and the console has not reached EOF. A passive owner
continues draining and retaining that console until the runtime exits. Optimus
never kills the application or fabricates a quiescent receipt. Other publishers,
incomplete logs, mismatched files, unknown GUI descendants, and actual installers
retain the normal wait. Source-mode handoff requires its installed `psutil`
dependency; a missing dependency is rejected before launching a target.
