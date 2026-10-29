# Update installation and restart verification

## Updating Optimus

On a managed Windows installation, approving a GitHub update downloads the exact
official release, verifies its asset digest and checksum manifest, and preserves
the saved preferences and Data locator. The old window closes through the normal
safe shutdown path. Unsaved Sandbox work retains its close guard.

The updater creates an exclusive publication copy under Releases and verifies
every file before publishing the new version. This lets the installed files
inherit the release directory's permissions rather than retain a private staging
folder's administrator-only permissions. Private keys and signed evidence stay
in their protected workspace. Publication requires space for the copy plus the
existing 2 GiB buffer, and never overwrites another installation.

The new window waits for the old instance lock, consumes its authenticated,
single-use resume, and confirms its exact executable, version, manifest and saved
choices. Only this signed acknowledgement confirms a successful restart. Saved
maintenance remains gated until that acknowledgement. A failed restart retains
the installation phase and actual bounded error instead of reporting success
from a process launch alone.
The restarted child inherits the current privilege context; it does not request
a second UAC relaunch after the original startup's permission decision.

Private Windows update directories use the current account's TokenUser SID so
ordinary and elevated launches use the same account permissions. Repair of the
specific legacy administrator-owned OWNER RIGHTS policy requires the existing
user-scoped DPAPI key first. Other permissions and protected historical attempts
are preserved. Restart processes explicitly reset the frozen bootloader state,
as required by the [PyInstaller restart guidance](https://pyinstaller.org/en/stable/common-issues-and-pitfalls.html#using-sys-executable-to-spawn-subprocesses-that-outlive-the-application-process-implementing-application-restart).

The progress window keeps its committed restart status during shutdown; it no
longer changes that message to “Update not applied” while the new worker runs.
The update itself does not change saved cleanup choices or approve new removals.

## Updating applications

Windows WinGet updates read back the installed version even when optional
installer-download cleanup is disabled. Equivalent supported versions or a
verified newer version satisfy the target; exit code zero alone does not.

Some MSIX installers report success while registration waits for a running app
to close. Optimus records that pending state, the exact package offer and any
verified blocking package/process identity from its own bounded installer log.
It rechecks installed state and avoids downloading the identical offer repeatedly
while the same prerequisite remains. Other applications continue through their
queue. Optimus does not force-close Codex to replace the client running this work.

## Updating drivers

Automatic Windows Update driver installation retains the saved matching rules
and a fresh applicable-driver search. An unmatched offer has an explicit skipped
result, including its missing approval/matching reason. An offer for disconnected
hardware is not evidence that an installed device needs a replacement driver.

Completion requires the exact Windows Update success fields and fresh follow-up
evidence. Pending restart, incomplete verification and already-current driver
reoffers stay distinct from newly verified installations. A failed or skipped
item does not become a successful install in the worker totals.
