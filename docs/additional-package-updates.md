# Optional Chocolatey updates

The optional Windows adapter uses an **existing standard Chocolatey 2 or newer**
installation. It starts off. Users select exact package IDs and accept the
provider/package agreements before enabling it. Optimus does not install a
package manager, change configured sources, update project environments or
guess which WinGet and Chocolatey names refer to the same application.

To prevent two providers from reinstalling or downgrading the same application,
Chocolatey automatic updates run only when the main Software Updater is off,
or WinGet is review-only without automatic per-app rules. Existing WinGet
exclusions and saved approvals are never changed automatically. A future exact
cross-provider ownership mapping would be needed to safely relax this rule.

Each update rechecks the current approved choices, exact package/version offer,
pin status and package-manager file identity. The command selects the fixed
[Chocolatey Community Repository](https://community.chocolatey.org/), requires
checksums, ignores remembered installer arguments, excludes prereleases and
prohibits installing a missing target. Dependencies are not automatically
installed or upgraded. The reserved `all` command and Chocolatey self-updates
are excluded. Packages with unrecognized version ordering are skipped.

The existing private console and shared maintenance lease track native
completion. Each intent retains hashes of the approval, executable and exact
argument vector. A complete authenticated receipt must establish matching
arguments/exit code, output completion, an empty owned job and a quiescent
console before Optimus advances. A separate read-only provider-version check
confirms Chocolatey's local metadata when possible; that is not a guarantee
that the vendor application's internal version matches the package metadata.

A known failed package or unavailable post-install version check is recorded,
then the next eligible package continues. An active or unknown native outcome
retains the installer gate. Restart-required results stop new installations
until Windows reports the restart requirement cleared. Optimus does not kill,
replay or force an uncertain installer, disable checksum validation or reboot
the computer automatically.

Provider behavior follows Chocolatey's documented [outdated command](https://docs.chocolatey.org/en-us/choco/commands/outdated/),
[upgrade options](https://docs.chocolatey.org/en-us/choco/commands/upgrade/)
and [local list command](https://docs.chocolatey.org/en-us/choco/commands/list/).
In particular, an optional enhanced exit code of `2` on read-only inventory
commands is parsed as documented rather than mistaken for an installer error.
