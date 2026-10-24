# Performance observations and native health diagnostics

Optimus can observe a short resource window without running a synthetic stress
test. The window reports time-weighted system CPU use, memory pressure, disk
read/write traffic and network send/receive traffic. An initial CPU/counter
sample establishes a baseline. Missing or reset counters remain unavailable.
Network traffic is **not** a Wi-Fi capacity or Internet speed measurement; disk
traffic is **not** disk latency. Optimus activity is included in the observations.

Optional local history retains compact summaries, not process names, command
lines or file paths. The default bound is 180 windows / 90 days. Comparable
windows must match the operating-system release, logical CPU count, installed
RAM, observation duration, phase and supplied power/activity context. Trends
describe observed differences; changing workloads prevent a claim that cleanup
caused a particular percentage speed improvement. Incomplete or cancelled
windows are displayed but cannot become a comparison baseline.

## Native health evidence

Windows can expose storage health, operating temperature, wear, uncorrected
error counters and power-on hours through its inbox storage provider. The wear
counter means estimated wear **used**; 100 means the estimated wear limit is
reached. An absent counter does not mean zero wear or a healthy device. Some
controllers, USB bridges and devices do not expose these counters. Device serial
numbers are omitted. See Microsoft's [storage reliability counter definition](https://learn.microsoft.com/en-us/windows-hardware/drivers/storage/msft-storagereliabilitycounter).

Battery design/full-charge capacities and cycle counts are shown when the
native provider exposes them. Recent Windows Diagnostics-Performance boot and
startup-delay events can identify evidence to review. Missing event access or
an empty log does not establish a fast boot. Optimus reads pagefile configuration
and current/peak usage; it does not disable or resize the pagefile.

Linux reports available memory/swap facts, sysfs battery health and the current
boot summary through an existing `systemd-analyze`. macOS reads memory/swap facts
and available battery health from `system_profiler`. These collectors explicitly
report unsupported storage wear and boot-detail capabilities instead of claiming
Windows parity. No extra package, kernel module or resident service is installed.

## Installer space preparation

The headroom check groups the actual system, temporary and destination paths by
volume, including mounted volumes. Separate known download/extraction/install
allocations on the same volume are added once, with one reserve per volume.
Unknown allocations remain unknown and cannot produce a “sufficient space”
result. The check does not reserve storage, delete files or guarantee that an
installer will fit. Callers must not label the same allocation twice.

## Windows integrity checks and explicit repair

Integrity checks are eligible only after reported update/component/system-file
errors, repeated system crashes, or an explicit user request. Supported inbox
commands are DISM `/CheckHealth`, `/ScanHealth` and SFC `/verifyonly`. A separate,
explicitly approved repair uses DISM `/RestoreHealth /NoRestart`, then SFC
`/scannow` if DISM completes without an error or restart requirement. Administrative
access is required by the native servicing tools.

An owner-bound durable intent precedes native dispatch. The native engine
retains an atomic completion receipt after its tracked steps exit. The bridge
retains the operation if the UI observation times out. Conflicting maintenance
also checks these records after Optimus reopens: a missing/invalid completion
stays blocked, while an exact late terminal receipt can establish completion
without replaying the command. Restart-required receipts need a fresh cleared
Windows restart state before that gate releases. Optimus does not
kill a running repair, retry it, reset Windows Update, force a restart, download
an unofficial patch or weaken security settings. Each native step retains its
command, timestamps, exit code and stdout/stderr logs. A zero exit code is not
translated into an invented “all files healthy” result. Review the native logs
and any restart requirement. Microsoft's documented [image repair procedure](https://learn.microsoft.com/en-us/windows-hardware/manufacture/desktop/repair-a-windows-image?view=windows-11)
and [System File Checker guidance](https://support.microsoft.com/en-au/windows/experience/backup-recovery/using-system-file-checker-in-windows)
explain the supported mechanisms.

## Integration API

```python
from optimus.performance_history import collect_resource_window, PerformanceHistory
from optimus.system_diagnostics import (
    collect_system_diagnostics, check_installer_headroom,
    integrity_check_plan, run_windows_integrity,
)

window = collect_resource_window(duration=5, interval=0.5, cancel_event=cancel)
history = PerformanceHistory(backend.data_dir)
if window['complete'] and history_enabled:
    report = history.record(window, phase='manual', context={'power_source': 'ac'})
health = collect_system_diagnostics(backend, cancel_event=cancel)
space = check_installer_headroom(
    {'system': system_path, 'temp': temp_path, 'destination': install_path},
    required_bytes={'system': None, 'temp': known_temp_bytes, 'destination': None},
)
plan = integrity_check_plan(['windows_update_errors'])
checks = run_windows_integrity(backend, plan, cancel_event=cancel)
# Separate user approval, outside automatic maintenance, is required for repair.
repair = run_windows_integrity(backend, plan, repair=True, approved=user_approved)
```

Windows bridge operation `system_diagnostics` invokes `Get-OptimusSystemDiagnostics`
from `engine/SystemDiagnostics.ps1`. `windows_integrity` invokes
`Invoke-OptimusWindowsIntegrity -Repair ... -Approved ... -DataRoot $DataRoot`;
the dispatcher must accept only actual Boolean repair/approval values. Mutation
callers must use the existing maintenance lease and must not retry a request
whose native outcome is still unknown. History writers likewise use the owning
maintenance operation to serialize different processes. Preferences and UI
integration govern optional history and health collection; these modules do not
save or migrate preferences themselves.
