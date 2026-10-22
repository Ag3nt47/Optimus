Angerona Disposable Cleanup 1.1.0 (standalone, Windows PowerShell 5.1)

Install (no administrator token requested):
  .\Install-AngeronaCleanup.ps1 -RepositoryRoot D:\local-security-ai\AngeronaSuite -InstallDirectory D:\Agent47\AngeronaCleanup
Preview task XML with -ValidateOnly; it performs no installation or cleanup.
The installer audits only. The watcher starts about one minute afterward and
at sign-in. Its task remains least-privilege. On watcher startup only, it asks
Windows once for administrator consent so the same watcher can clean protected
Angerona folders. Accepting starts an elevated watcher for that session; declining
continues with standard access and truthful protected-folder skips, without
asking again in that watcher session. No consent is clicked or bypassed.
Already elevated sessions skip the request. Manual Audit/Apply never auto-elevate.
Set request_admin_on_startup=false in private settings.json to disable the request.

Installed manual controls:
  .\Run-AngeronaCleanup.ps1 -Mode Audit
  .\Run-AngeronaCleanup.ps1 -Mode Apply
  .\Uninstall-AngeronaCleanup.ps1
Uninstall validates the exact owned task, requests the same-installation watcher
to exit gracefully (including a RunAs child outside Task Scheduler's job), waits
for its lease to release, then removes only the owned task. Files remain on D:.

Administrator startup uses a private expiring ticket and two-way ready/ack
handoff. The parent checks the child process's real token SID/elevation/session,
image and creation time with limited query rights. The child verifies the same
account and original configuration/scope hashes after elevation. It holds the
single watcher lease before readiness, and waits for parent acknowledgement
before cleanup. Cancellation/timeout leaves standard fallback or a visible
deferred state; an internal relaunch flag never supplies administrator access.
scope-authority.json records the installation-approved source layout; changing
repository_root only in settings fails closed. These private machine files,
Handoffs, stop-request.json, and status.json never belong in public archives.

The watcher considers Angerona's executable, startup/repair helpers, module
workers, Python imports and repository scripts/tests. Sharing its venv Python
alone is not Angerona: Optimus can use that interpreter without being mistaken
for the security app. Unreadable Python commands or relative pytest runs from
that venv defer cleanup rather than guess. Closing Angerona's window may only
hide it to the tray; cleanup waits until no runtime/helper/worker/test remains.

Local WMI process start/stop events wake checks where permitted. Otherwise a
30-second process inventory fallback is used. An exit requires two clean
inventories two seconds apart. Startup also catches missed cleanup from an
earlier exit. A Windows SessionEnding notification requests best-effort cleanup;
Windows may terminate user processes before it finishes. There is no guarantee
of cleanup during shutdown/power loss. The next sign-in retries eligible work.
The watcher never stops Angerona, cancels shutdown, or clears forensic records.

Allowlisted roots (native Shell identity authority, not inherited env values):
  current LocalAppData\Angerona\SourceData\tmp
  current LocalAppData\Angerona\Startup\tmp
  sibling AngeronaData\tmp beside the verified source repository
  D:\AngeronaData\tmp (packaged default, if present)
In the corresponding logs folders, only these top-level diagnostic names:
  startup-preflight.log / startup-preflight.previous.log
  startup-dashboard.log / startup-dashboard.previous.log
  blackbox.log / crash.log / uiwatchdog.log
In each diagnostics folder: top-level crash.log only.

Temp contents are disposable only after the process gate passes. Files created
or written after each scan starts are skipped. Every deletion opens an exclusive
handle, rejects hard links/read-only/system/offline/reparse files, holds all
ancestors against replacement, and deletes using the verified handle. Each scope
is bounded to 5,000 inspected files, 1 GiB and 10 seconds. Runtime status is
rechecked during actual cleanup. Empty directories remain. Locked/unavailable
roots and files are reported honestly; no ACLs are changed to obtain access.
Protected administrator-owned D: data requires actual administrator access.
If startup consent is declined, an elevated manual run may request normal UAC.
The scheduled task itself never gains permanent highest-privilege registration.

Never touched: the Angerona checkout (including .tmp/repair-wheels), venv/build,
repair transactions or backups, current/archived runtime_alerts, signed flight
recorder/spool, evidence, cases, audit/security logs, credentials, databases,
shadow_cache recovery copies, remediation receipts, policies, or shared_logs.
The repository's destructive cleanup.ps1 is never called.

Private latest.json and up to 100 reports remain in this installation on D:.
They record skipped/uncertain states and deleted byte counts. No network upload.
Irreversible temp deletion cannot be undone by uninstall. Reports and settings
are private machine state and must not be included in public download archives.
