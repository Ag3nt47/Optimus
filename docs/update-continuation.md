# Continue approved updates

Run `Optimus.exe --continue-updates --output "D:\OptimusReports\Updates"` after the current run finishes, or use the portable helper:

```powershell
.\source\scripts\Continue-OptimusUpdates.ps1 -Executable 'D:\Optimus\Optimus.exe'
```

With Optimus 0.11.12 or later, the packaged `source\scripts\Continue-OptimusUpdates.bat` launches the helper with a double-click and finds the executable in its package automatically.

The helper requests normal Windows administrator approval once if needed. `-StandardUser` suppresses this request; protected updates may then be deferred. It prints the folder containing live `.progress.json` and final `.json` reports, which remain inspectable even though Optimus is a windowed executable. `-ReportDirectory` selects another ordinary local folder.

This command reads the current saved app/driver switches, package selections, device rules and agreements. It never enables disabled updates, changes preferences, runs cleanup or debloat, or executes always-install/always-uninstall app presence rules. Always-update and always-ignore update rules still apply. Native macOS and Linux installations use their approved software updater; Windows driver updates are unavailable there.

App and driver discovery run concurrently; actual installers share the existing exclusive installation lane. Exact held offers and known package-specific failures are recorded and skipped so the next eligible package can proceed. Completed native updates with sufficient exact evidence can be reconciled automatically through the existing recovery service. Journals and individual offer holds are retained.

An active maintenance run, an installer with an unknown outcome, a pending native operation, or a required restart can still prevent conflicting installations. The script neither kills nor blindly restarts installers. A final `completed_with_skips` status means the queue drained while individual offers need attention; it does not claim those offers were installed. Exit code `0` means `completed`, `completed_with_skips`, or `no_enabled_updates`. Exit code `2` means blocked, incomplete, cancelled or unavailable; consult the report.
