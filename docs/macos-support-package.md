# Optional macOS support package

`scripts/Get-Optimus-macOS-Support.command` offers an optional package from
[Ag3nt47/Optimus-macOS-Support](https://github.com/Ag3nt47/Optimus-macOS-Support).
Open it in Finder (or run it with `sh`). It asks first; choosing **Not now**
downloads nothing, and Optimus works the same without the package.

## What it adds

Mac versions of the Windows-only Optimus scripts:

| macOS script | Windows script |
| --- | --- |
| `safe-stop-optimus.sh` | `Safe-Stop-Optimus.ps1` |
| `read-optimus-performance.sh` | `Read-OptimusPerformance.ps1` |
| `continue-optimus-updates.sh` | `Continue-OptimusUpdates.ps1` / `.bat` |
| `check-optimus-drive-space.sh` | `Check-OptimusDriveBuffers.ps1` |

and Mac reviews for features that are Windows-only in the app: built-in
protection (XProtect, Gatekeeper, SIP, FileVault, firewall) in place of the
Defender scan, launchd startup items in place of startup entries and services,
TRIM/SMART/APFS health in place of drive optimization, and DNS/network reset
guidance. `optimus-compat.sh` reports which download and tools fit a given
Mac, including older macOS versions.

## How it is verified

The command pins one commit of the support repository and the SHA-256 and size
of its `pack-manifest.txt`. Every listed file must match its own SHA-256 and
size, paths are restricted to `scripts/`, `docs/`, `README.md` and `LICENSE`,
downloads are HTTPS-only with redirects refused, and nothing is installed until
every file has verified. It uses only `/bin/sh`, `curl` and `shasum`, so it
works with the prebuilt app and on older macOS, and never asks for
administrator rights.

Files are installed in
`~/Library/Application Support/Optimus/macos-support/current/`.

```sh
sh scripts/Get-Optimus-macOS-Support.command --status
sh scripts/Get-Optimus-macOS-Support.command --uninstall
```

In the prebuilt Mac app the command is inside the bundle at
`Optimus.app/Contents/Resources/source/scripts/`.
