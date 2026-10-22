# One-click coverage and critical review

Optimus runs the maintenance choices saved during setup. Selecting Full applies the preset once; later individual changes persist. The saved automatic-continuation choice can carry a completed scan/cleanup into the supported security corrections and remaining already-approved updates, using the same guarded worker as the Auto continue button. One automatic follow-up is allowed per completed run; errors, cancellation, closed results and uncertain installer states do not authorize a loop.

The interface keeps three landing-screen buttons. Detailed controls, recovery and scope choices live in Settings. A low-motion option preserves readable, static operation. Progress describes stages and active work rather than pretending that elapsed time predicts completion.

With **Update apps and drivers in background while independent steps continue** enabled, app discovery, driver discovery and the initial resource inventory start together. Both updater workers have their own Windows engine and discovery cache. Maintenance waits for discovery before selected temp/browser cleanup, then opens the installer checkpoint. If discovery times out with a live native request, cleanup and installers are deferred. App and driver installer transactions share an exclusive slot; a ready driver can proceed while an app is still scanning. When both are ready at the checkpoint, apps retain priority. Application/resource checks, read-only startup/duplicate/cloud/driver metadata reviews and supported policy work continue alongside installations. Three persistent panels in one progress window show **Maintenance**, **App updates** and **Driver updates**, with truthful scanning, preparation, installation, counts, elapsed time and waiting reasons. A driver-only approval can run without app updates. On macOS/Linux, native app discovery overlaps the initial resource inventory; native driver checks remain a read-only foreground capability and the driver panel reports its platform scope.

Startup changes, duplicate recycling, Windows installers, debloat, drive optimization, final measurement and verification wait for the background queue. A dedicated **Waiting for background updates** state explains those dependencies. Cancelling stops further attempts and waits for the current installer to return. Unresolved installer outcomes retain a journal path for manual review and block another automatic attempt, including after the background option is disabled. This protection includes uncertain driver outcomes in serial runs. Existing unresolved-installer records also prevent pre-update cleanup until their state is reviewed. A refusal before an installer starts is recorded as a known preflight result; it does not invent a running installer. Historical uncertain records without launch evidence remain subject to review rather than being silently cleared.

Windows normal GUI sessions can use the saved **Prioritize Optimus interface (Above Normal CPU)** option. Pooled and background worker threads use normal base priority and restore their previous thread priority afterward. Windows child processes use the normal default priority class. The setting takes effect at the next launch; probes and command-line scans never apply it, and other externally configured priority classes are preserved. This does not reserve RAM or request Real-time scheduling.

| Area | Automatic behavior when configured | Remaining boundary |
| --- | --- | --- |
| Temporary files and junk | Revalidates selected supported categories and deletes eligible unlocked temporary files | Protected, locked, unsupported and excluded files are reported individually; personal files are outside a temporary-file category |
| Browser data | Cleans configured cache/history/cookie categories | Open browsers retain the selected close-or-skip interaction; cookie exceptions remain in force |
| SSD/HDD maintenance | Optimizes eligible healthy internal Windows volumes through the native media-aware optimizer | Requires AC, permissions, current identity/health/space checks and cadence. No repartitioning, formatting, secure wipe or universal native filesystem tuning |
| Application updates | Installs provably newer offers under saved package scopes, exclusions and agreements; optional background queue lets independent stages continue | App and driver installers share a serial background lane. Windows installers and final verification wait for that lane. Unknown outcomes are not replayed |
| Windows security updates | Installs eligible Windows Security/Critical offers under the saved install and licence choices | Feature upgrades, unoffered vendor patches and restarts retain separate paths |
| Drivers and GPU drivers | Installs eligible native Windows Update driver offers under exact saved device/update approval, with backup checks | A generic driver catalog cannot prove compatibility for every device. Kernel, firmware and native OS support remain platform/vendor specific |
| Security corrections | Saved automatic continuation or the explicit Auto continue button can apply supported Defender/firewall corrections and verify current state | Other provider settings, managed policy, restarts and unsupported remedies remain visible |
| CPU and power | Eligible native AC performance preferences; optional approved Codex priority profile; measured bottleneck analysis | No generic overclock, invented CPU capacity or override of thermal/firmware protection |
| RAM and background work | Measures memory pressure and explains process ownership/startup evidence. Saved startup actions can apply their exact approved scope | Process age or RAM use does not establish that an application is unnecessary. Selected process pauses/removals retain review; committed memory is not magically freed by suspension |
| GPU performance | GPU driver coverage participates in supported driver updates; power preferences and pressure analysis remain hardware dependent | No universal GPU overclock/VRAM cleaner is claimed; vendor-specific tuning requires exact hardware support |
| Legacy compatibility | Optional non-security WinGet force mode and legacy Windows media-Setup CPU policy preparation | Actual instruction sets, disk space, image applicability, signatures and hard installer blocks still matter |

## Critical-review bot

Native CI runs `tools/critical_review.py` against disposable fixture backends. The bot is a rule-based review harness, not an AI reviewer and not a guarantee that every usability issue has been found. It measures actual Qt controls and navigation/scroll reachability at compact desktop sizes, checks readable action labels and selected workflow transitions, and captures screenshots. Its report distinguishes observed failures from manual review questions and capability gaps.

Run it from a source checkout with the build dependencies installed:

```text
python tools/critical_review.py --output test-artifacts/critical-review
```

Review its Markdown/JSON and screenshots alongside native regression results. A failure must be investigated or explicitly corrected; an unavailable capability is not counted as an optimization. CI review uses fixture services only and does not clean files, install software, change drivers or tune its host computer.
