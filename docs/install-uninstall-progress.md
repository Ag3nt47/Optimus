# Install and uninstall progress

Optimus 0.12.3 separates a worker's overall queue progress from the current installation or removal. The ring and status label name the operation and show its native reported percentage. A new package or phase resets its own progress; completed queue items never become a fabricated installer percentage.

## Coverage

- Windows WinGet app updates and saved Keep installed/Keep uninstalled rules stream supported native install/uninstall progress through the existing hidden console connection. A generic EXE uninstaller may provide only a spinner.
- Selected consumer AppX, provisioned packages and supported Edge removal report removal and readback phases. These adapters publish no stable intermediate percentage, so the ring stays indeterminate until verified completion.
- Homebrew, APT, DNF and optional Chocolatey updates report installation and verification phases. Their current Optimus adapters do not publish trustworthy intermediate percentages; arbitrary package-script text is not treated as a native progress protocol.
- Driver updates keep their existing Windows Update progress. Manual Applications review opens supported system controls or a removal plan; an externally controlled uninstaller displays its own progress.

For the newly instrumented presence, debloat and native package adapters, terminal 100% requires the existing successful result and fresh installed/removed-state verification. Existing WinGet update progress marks a successful native installer phase; final maintenance verification separately assesses installed outcomes. Required restarts, uncertain native outcomes, failed readback and missing provisioning restart metadata remain explicit review states. Optimus does not change provider commands, close applications or grant new install/uninstall approval to obtain a percentage.

## Automatic low-space cleanup

The separate default-off Health setting can trigger already selected cleanup categories below the saved drive-space guard target while the approved app session is open. It uses the same junk-cleaning cooldown, selected age limits, current scope, workload checks and installer barrier. Only complete original Cleaner plans on verified low-space local volumes are eligible. Redirected, cloud-backed, healthy and unknown volumes stay excluded; incomplete previews are reported without applying them.

Space is measured again before applying and after each category. The cleaner can remove a full eligible category batch before observing recovery; it does not promise an exact byte reserve or a guaranteed 2 GiB result. Browser privacy, app uninstalls, personal history, new cache recipes and Recycle Bin expiry are not added to its deletion scope.
