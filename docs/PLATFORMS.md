# Native platforms and downloads

Optimus 0.11.12 runs as a native Qt desktop application on Windows, macOS and Linux. Each download contains its own Python runtime, the public editable source for Sandbox, and dependency licenses. Python is only needed when building from source.

| Platform | Direct download | Tested build environment |
| --- | --- | --- |
| Windows, Intel/AMD 64-bit | [Windows ZIP](https://github.com/Ag3nt47/Optimus/releases/latest/download/Optimus-Windows-x64.zip) | Windows Server 2025 GitHub runner; Windows 10/11 desktop target |
| macOS, Apple Silicon | [Apple Silicon app ZIP](https://github.com/Ag3nt47/Optimus/releases/latest/download/Optimus-macOS-arm64.zip) | macOS 14, arm64 |
| macOS, Intel | [Intel app ZIP](https://github.com/Ag3nt47/Optimus/releases/latest/download/Optimus-macOS-x64.zip) | macOS 15, x86_64 |
| Linux, Intel/AMD 64-bit | [Linux tar.gz](https://github.com/Ag3nt47/Optimus/releases/latest/download/Optimus-Linux-x86_64.tar.gz) | Ubuntu 22.04, x86_64; glibc 2.35 or newer |
| Every platform | [Editable source ZIP](https://github.com/Ag3nt47/Optimus/releases/latest/download/Optimus-Source.zip) | Source archive, not an executable |

[SHA256SUMS.txt](https://github.com/Ag3nt47/Optimus/releases/latest/download/SHA256SUMS.txt) covers all five downloadable archives. The [release page](https://github.com/Ag3nt47/Optimus/releases/latest) also lists each file separately. Downloading starts with one click; extracting and the normal platform security review are still required.

## Installation

- **Windows:** extract the complete ZIP to an ordinary writable location. Launch `Optimus/Optimus.exe`. Keep `source`, `_internal` and the executable together. Windows administrator consent is requested only when the saved option or an individual operation needs it. The executable is currently unsigned.
- **macOS:** choose the architecture that matches About This Mac. Extract and move `Optimus.app` to `/Applications` or `~/Applications`, then open it. The app is ad-hoc signed to seal its final bundle; it has no Apple Developer ID signature or notarization. If macOS blocks the first open, review it using Apple's normal Privacy & Security interface. Do not disable Gatekeeper or System Integrity Protection. Terminal use is `Optimus.app/Contents/MacOS/Optimus --process-scan --output <folder>`.
- **Linux:** extract with `tar -xzf Optimus-Linux-x86_64.tar.gz`, then run `./Optimus/Optimus`. The tar archive retains executable permissions and shared-library links. A graphical desktop, glibc 2.35 or newer, fontconfig, EGL/OpenGL and the Qt platform runtime libraries are required. X11 and Wayland desktop behavior depends on the installed Qt/desktop libraries; CI also exercises the offscreen interface. This release is a portable tar archive, not an AppImage, DEB or RPM.

Native startup registration uses the application's current location. Move the app first, then enable or repair **Launch on sign-in** in Settings. Do not run the entire GUI as root on macOS/Linux. Native package-manager authentication remains a separate operating-system decision.

## Capability boundaries

| Function family | Windows | macOS / Linux |
| --- | --- | --- |
| Main controls including Revert, launch consent, setup choices, responsive worker cards, stage progress and cancellation | Native Qt | Native Qt |
| GitHub startup release check | Stable release check, explicit update consent, verified side-by-side install and guarded restart on supported writable managed installations | Stable native-asset check and manual download; no automatic bundle replacement or privileged install |
| Worker Back/Next/Cancel and per-app update rules | Session/item-bound cooperative controls and exact WinGet update choices | Same worker controls and supported native-manager update choices |
| Keep installed / Keep uninstalled app policies | Opt-in, exact WinGet identities and supported quiet installer types with fresh verification | Explicitly unavailable for automatic presence changes; normal native update choices remain available |
| Smart threshold/browser-exit cleaning and calendar-month cadence | Opt-in, while open after launch approval | Opt-in, while open after launch approval; native path/ownership limits apply |
| Authenticated file encryption, split/join and separately reviewed overwrite | Shared native implementation | Shared native implementation |
| Driver rollback and System Restore management | Exact-device guarded rollback and reviewed restore-point controls | Unsupported; no Windows recovery APIs are called |
| Automatic speed enhancements and conditional undo | AC-only power mode on supported Windows 11; existing High Performance plan on eligible battery-free desktops | Charger-only Low Power Mode correction on supported macOS when already authorized; offered performance profile on eligible Linux mains-powered desktops. Battery/unknown/managed/unsupported states defer |
| Advanced update compatibility | Saved WinGet non-security force mode; legacy MoSetup CPU-policy preparation and a separately invoked media-upgrade script | Windows compatibility changes are unavailable; native package and OS requirements remain authoritative |
| Local allowlisted temporary/cache cleanup | Windows categories and paths | Native per-user temporary/cache paths; no indiscriminate system-folder deletion |
| Chromium/Firefox cleanup, browser close-or-skip choices | Supported installed browser profiles | Supported native browser profiles; Safari and arbitrary application databases are outside this release's browser cleaner |
| Process ownership, measured CPU/RAM/I/O and background analysis | Native psutil plus Windows launch evidence | Native psutil; permissions can hide ownership, command lines or counters |
| Priority, affinity, recoverable pause/resume | Supported Windows controls | Supported host controls; CPU affinity is unavailable on macOS. Raising priority or restoring an earlier higher priority can require permission; failures are reported |
| Disk inventory, bounded SHA256 duplicate scans, approved local recycling | Native Recycle Bin | Native Trash through Send2Trash; unsupported locations fail without permanent-delete fallback |
| Sandbox source editing, history, revert and restart | Bundled source workspace | Bundled source workspace, including the source inside the macOS app |
| App/sign-in management and supported software updates | Windows registrations and WinGet | Native startup registrations and supported installed package managers; only offered upgrades are eligible. Application removal provides a reviewed native removal route rather than executing an uninstall |
| Codex priority profile | Verified Windows installation profile | Unavailable; choose an exact owned process in native process controls |
| Consumer Appx removal, Microsoft Edge/365 scripts, registry repair | Windows-specific implementations | Unavailable; no Windows scripts are invoked |
| Windows Update driver installation, driver backup, Defender and Windows repair | Windows-specific implementations | Unavailable; use the host OS/vendor update mechanisms and review any native recommendations |
| DPAPI-protected personal OneDrive cloud connection | Windows-specific implementation | Unavailable in this release |

An unavailable feature must be reported as unavailable rather than counted as a successful optimization. Reading process/system information does not prove an application is unnecessary. Saved approvals remain scoped to the exact platform and supported action; arbitrary processes, startup registrations, drivers and user files are not removed merely because a scan lists them. The detailed historical Windows feature descriptions in the main README and User Guide apply to Windows unless the native capability table says otherwise.

Native startup changes affect the next sign-in. Disabling a macOS LaunchAgent or a Linux autostart entry does not kill an already running program; use the separately reviewed process controls for that. Homebrew covers its own installed packages on macOS. Apple's OS-update catalog is separate: installing one exact, freshly offered Apple update requires its own manual approval and license acceptance. That action can request native authorization; it does not join unattended Homebrew maintenance and does not request an automatic restart. Linux offers repository updates through apt/dnf; pacman upgrades remain review-only because partial Arch Linux upgrades are unsupported.

## Data and privacy

The app uses a per-user data location on each platform: `%LOCALAPPDATA%\Optimus` on Windows, `~/Library/Application Support/Optimus` on macOS, and `$XDG_STATE_HOME/optimus` (or `~/.local/state/optimus`) on Linux. Settings, reports, operation journals, browser rollback copies and the editable Sandbox stay local. An existing `Optimus.location.json` relocation file is an advanced per-installation option and is never packaged in public downloads.

Browser privacy backups still contain the original records. Cleanup is not secure erasure. Recycling uses the native Trash/Recycle Bin; Optimus does not empty it automatically. Source edits run as the current user after an explicit restart: the Sandbox is an editor with version history, not OS process isolation.

Native duplicate recycling records a durable recovery map before moving an extra copy into a private staging folder and then the OS Trash. The Trash entry retains its original filename. **Put Back** can return it to that recorded staging folder; move it from there to the original path shown in the journal to finish restoration. The verified keeper remains in place. If staging or identity verification fails, preserved bytes and their recovery locations are retained for review rather than silently deleted.

## Building and verification

Build each artifact on its own operating system and CPU architecture. PyInstaller does not cross-compile macOS from Windows or Linux from macOS. From an activated Python 3.12 virtual environment:

```sh
python tools/install_build_dependencies.py
python -m pip install -e . --no-deps
python tools/test_native.py
python tools/build_native.py
```

`tools/test_native.py` selects the same tests locally and in CI: the complete Python suite on Windows; every `tests/test_native*.py` plus its explicit `PORTABLE_SUITES` list on macOS/Linux. Native tests cover the actual platform counterparts; Windows contract fixtures remain in the Windows job. The build helper checks the actual interpreter architecture, builds an onedir application (`.app` on macOS), applies Windows Qt/ICU normalization only on Windows, copies allowlisted source and licenses, and runs `--smoke-test`, `--startup-probe`, `--process-scan` and `--tls-probe` against the packaged executable. The offline TLS probe validates bundled certificate roots and strict verification with empty default CA locations; it does not contact GitHub or apply maintenance. Those checks do not apply maintenance. Source packaging excludes local configuration, runtime reports, credentials and private folders. macOS source/licenses are under `Contents/Resources`; its final bundle is ad-hoc signed after packaging. Linux archives preserve links and executable bits.

GitHub Actions builds Windows x64, macOS arm64, macOS x86_64 and Linux x86_64 independently. Windows also runs all eight native PowerShell fixture suites. Each job uploads Python test results and packaged-check evidence, including when a later step fails. Tagged releases require all four native jobs to succeed before publishing the separate archives and combined checksums. Release publication verifies the complete artifact set and refuses to overwrite different existing release bytes.

The build dependency helper uses official binary cryptography/CFFI wheels on supported targets. Current cryptography releases no longer supply Intel Mac wheels, so Intel Mac source builds require Rust, Xcode tools and Homebrew OpenSSL. The helper requests static linkage with a fresh source build and checks that the Rust binding has no external OpenSSL dependency. Packaged smoke tests exercise AES-GCM and Scrypt in memory before publishing. See [PyCA's macOS build instructions](https://cryptography.io/en/50.0.0/installation/#building-cryptography-on-macos). Portable downloads include the result; end users do not need these build tools.

CI is evidence for the environments listed above. It cannot reproduce every distribution, GPU, security policy, driver catalog, browser installation or user's approvals; incomplete or permission-limited inventories remain visible for review.

Implementation references: [GitHub's runner architectures](https://docs.github.com/en/actions/reference/runners/github-hosted-runners), [PyInstaller's native packaging and glibc guidance](https://pyinstaller.org/en/stable/usage.html), [Send2Trash's platform behavior](https://github.com/arsenetar/send2trash), and [Apple's app security review](https://support.apple.com/en-us/102445).
