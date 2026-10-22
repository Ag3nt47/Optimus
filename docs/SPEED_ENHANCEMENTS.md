# Speed enhancements and update compatibility

Optimus can automatically apply the native speed enhancement supported by the current computer's power controls. Enable **Automatically apply supported speed enhancements during Optimize** in Settings and save. Full mode selects this option once; reopening the app preserves your later changes. Existing saved configurations do not silently gain the option. Setup must be complete before automatic changes run.

The package complements the existing CPU/RAM/I/O analysis, scoped cleanup, approved startup controls and supported updates. It does not assign a synthetic speed score. A verified power preference is evidence of configuration, rather than a measured speed gain. Performance preferences can increase energy use, fan noise or heat.

| System | Automatic power action | Eligibility |
| --- | --- | --- |
| Windows 11 | Set the configured **AC** power mode to Best Performance | Known plugged-in state, supported API, and a supported standard active plan. Battery configuration is preserved. Custom/OEM or intentional energy-saving choices remain visible for review. |
| Older Windows | Activate an existing standard High Performance plan | Verified battery-free desktop on AC, standard Balanced currently selected, and High Performance already available. No plan is manufactured. |
| macOS | Turn off an existing Low Power Mode setting for the charger only | Exact supported `pmset` AC setting, known AC power and already authorized administrator access. Battery settings and High Power settings are preserved. |
| Linux | Select an offered performance power profile | Verified battery-free mains-powered computer, working native power-profile service, no conflicting holds or thermal/inhibited state. Missing or ambiguous metadata defers the change. |

Settings provides **Review current opportunities**, **Apply supported enhancements now**, progress/cancel controls and a list of recorded changes for **Restore selected enhancements**. The original exact value is recorded before any setter runs. Restore checks that the current policy still belongs to that recorded change, so it preserves a later user or OS choice. Disabled, unavailable, deferred and uncertain actions remain labelled separately.

## Scripts and command line

Use the packaged executable without opening the interface:

```text
Optimus.exe --speed-enhancements status
Optimus.exe --speed-enhancements apply
Optimus.exe --speed-enhancements history
Optimus.exe --speed-restore <32-character-change-id>
```

On macOS/Linux, use the native `Optimus` executable in place of `Optimus.exe`. Status and history are read-only. Apply honors the saved enable choice; it does not enable unrelated maintenance. `--output <folder>` saves a report. The editable source includes `scripts/Speed-Enhancements.ps1` and `scripts/speed-enhancements.sh` as literal-argument launch wrappers. Source-checkout users supply the executable path explicitly; the wrappers do not download software or bypass platform authorization.

## Advanced Windows compatibility

Two separate saved Windows choices are available:

- **WinGet compatibility mode** adds the documented `--force` option to eligible, exact, provably newer app updates. Microsoft defines it for non-security issues. Fresh versions, selected scopes, exclusions, native pin checks, integrity checks, cancellation and restart/uncertain-installer barriers still apply. It does not repeatedly launch an installer after a timeout or override an application's hard OS/CPU requirement.
- **Unsupported-CPU media preparation** records and sets only the legacy `HKLM\SYSTEM\Setup\MoSetup\AllowUpgradesWithUnsupportedTPMOrCPU` DWORD. Administrator access is required. This is a best-effort legacy Windows media-Setup policy, not a current Microsoft support guarantee or a reliable override for Installation Assistant. It leaves TPM, Secure Boot and installer binaries unchanged. The Changes page can restore the exact recorded value conditionally.

Headless policy commands are `Optimus.exe --windows-compatibility status`, `Optimus.exe --windows-compatibility prepare` and `Optimus.exe --windows-compatibility-restore <change-id>`. Preparation honors its saved choice and does not launch an OS upgrade, accept a new OS licence or reboot.

The separate media tool requires an explicit Microsoft Windows `setup.exe` from selected installation media, administrator access, an output directory and explicit licence acceptance:

```powershell
.\Invoke-WindowsMediaUpgrade.ps1 -SetupPath 'X:\setup.exe' -Mode Scan -AcceptLicense -OutputDirectory 'D:\WindowsUpgradeLogs'
```

Review its JSON result. `-Mode Upgrade` first repeats the preflight and compatibility scan, then starts visible Setup only when the scan reports no blocking issue. It checks signature, image metadata, edition, architecture, language, target version and system-drive space. It neither downloads nor modifies installation media. The selected media's provenance remains the caller's responsibility; a signed Setup launcher alone is not an independent authenticity guarantee for every image payload.

Setup's `/Compat IgnoreWarning` applies only to dismissible messages. It cannot turn missing CPU instructions, unsupported kernel/drivers, insufficient disk space or an inapplicable patch into a compatible update. `/NoReboot` suppresses Setup's first down-level reboot; a later user restart can resume an upgrade that requires further restarts. Pending installation remains pending until Windows confirms completion. Optimus does not describe compatibility-policy preparation or a launched installer as a successfully installed OS update.

## Sources

- [Microsoft AC power mode setter](https://learn.microsoft.com/en-us/windows/win32/api/powrprof/nf-powrprof-powersetuserconfiguredacpowermode), including its configured-preference limitation.
- [Apple power modes](https://support.apple.com/en-us/101613) and [Apple pmset implementation](https://github.com/apple-oss-distributions/PowerManagement/blob/main/pmset/pmset.m).
- [Upstream power-profile API](https://upower.pages.freedesktop.org/power-profiles-daemon/gdbus-org.freedesktop.UPower.PowerProfiles.html).
- [WinGet upgrade options](https://learn.microsoft.com/en-us/windows/package-manager/winget/upgrade).
- [Windows Setup options and compatibility exit codes](https://learn.microsoft.com/en-us/windows-hardware/manufacture/desktop/windows-setup-command-line-options?view=windows-11).
- [Current Microsoft installation guidance](https://support.microsoft.com/en-us/windows/deployment/install-upgrade/ways-to-install-windows-11), which does not promise support for the legacy policy.
