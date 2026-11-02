# Battery and physical-drive health

Optimus checks battery and physical-drive health once during each normal maintenance run by default. The Maintenance worker shows separate battery and drive summaries. **Health** opens the captured snapshot and local trend graphs while other work continues. Opening it reads local history; **Refresh hardware check** explicitly starts a fresh, expanded read in a separate worker, including while maintenance is running. **Settings → Dashboard → Battery / drive health & history** also offers a fresh scan. The automatic check can be turned off in setup or Settings.

Select displayed text with the mouse or keyboard and copy it normally. **Copy report** copies the complete current readings, timestamp, source, confidence and notes. Refresh updates the snapshot and local history without changing your saved cleanup choices. Duplicate refresh clicks are suppressed. Closing the window cancels its own read without stopping maintenance or waiting on the GUI thread.

These are provider observations, not a surface scan, repair or prediction of remaining service life. They do not run SMART self-tests, change power settings, install software, repair disks or replace firmware. A slow, missing or denied provider returns unavailable data and maintenance continues. Normal Windows reads retain a 30-second body budget; explicit expanded reads allow up to 60 seconds. A separate read-only Windows host handles explicit refresh and closes when its request finishes, rather than leaving another monitoring service resident.

![Battery and physical-drive measurements with a per-scan trend graph](media/optimus-hardware-health.png)

*Illustrative fixture data. These example readings are not measurements from a user's computer.*

## Battery readings

- Full-charge capacity compared with design capacity, where both are exposed in the same units. This is capacity retained, **not current charge**.
- A native maximum-capacity percentage on supported macOS batteries, without invented absolute capacities.
- Available cycles, temperature and reported condition.
- A bounded inbox Windows battery report when native battery capacity pairs are incomplete. Only installed-battery facts are parsed; usage history, computer names and raw serials are not exported. Its private scratch report is removed after parsing. A slow or unavailable report remains unavailable rather than producing an estimated health score.

Zero/invalid design or full-charge capacities do not produce a zero-health reading. Values slightly above 100% can reflect the provider's estimate and are shown as reported. Unsupported counters remain unavailable.

## Drive readings

Each physical drive has its own entry. Available fields include provider/SMART condition, temperature, a provider-rated normal temperature limit, power-on hours, corrected/uncorrected and total errors, start/stop and load/unload cycles, rated cycle limits, maximum observed I/O latency and manufacture week. Supported NVMe readers can also expose spare capacity, warning flags, media errors, shutdowns and rounded data-unit counts. Each extra reading includes source and confidence information. Partitions and drive letters are associated only through verified native identity mapping. A volume's free space is not its physical health.

**Estimated SSD endurance used** is separate from health: 100% used means the provider's endurance estimate has reached its design limit. Genuine NVMe estimates can exceed 100%; 255 represents more than 254%. A genuine firmware-reported zero remains an estimate and does not establish that the drive is unused. A generic Windows zero has insufficient evidence for that interpretation, so Optimus keeps it as a raw provider counter and shows endurance as unverified. HDDs show **Not applicable** for SSD endurance. Generic guessed ATA attributes are not promoted into a reliable SSD-wear percentage.

Optimus does not invert wear into a universal “health remaining” score. Healthy/normal, warning and unhealthy conditions use a categorical history graph. Powered-on days are a conversion of reported hours; approximate calendar age is shown only when a valid manufacture week is explicitly supplied. Neither observation predicts remaining years or a failure date. Temperature warnings compare against the device's exposed rated limit rather than inventing a model-specific threshold.

USB bridges, RAID controllers, virtual disks and vendor drivers can hide measurements. Unsupported SMART or missing counters are not a clean bill of health. macOS and Linux use supported native data; Linux can use a suitable, already-installed trusted SMART tool without downloading anything or waking a standby drive for a scan.

## Baseline and subsequent scans

Every completed observation is saved locally under the Optimus data folder in `hardware-health/history.json`. The first observation remains the baseline. Later scans retain their real timestamps and per-device readings. Missing measurements create gaps, and the graph shows the available fields for each selected device.

Older wear values without per-scan SSD provenance appear as gaps, including the generic zero values previously shown for HDDs. Their original measurements, scan IDs and baseline remain stored. New verified estimates include their media/evidence classification so that a reported NVMe zero can be distinguished from an unsupported generic counter.

Only an opaque hash of a reliable hardware identity allows connected trend lines. Replacing a device starts a separate baseline. Unidentified devices show isolated observations; names, slots and drive letters cannot prove hardware continuity. Raw serial numbers and hardware identifiers are not exported.

Graphs show up to 500 points per metric, retaining the baseline and recent scans and visibly breaking any omitted interval. The original observations remain in local history. Recording is bounded to 20,000 scans, 256 historical device identities and 32 MiB. Reaching a limit stops new history recording with a visible status; it never deletes the baseline or older scans. No new persistent monitoring service is installed.

![The graph section after scrolling, with the first battery analysis marked in blue and unavailable readings left as gaps](media/optimus-health-history.png)

*Illustrative local history after scrolling below the expanded facts. Hover a point for its scan time and reported value. Select the device and metric; provider conditions use categories rather than percentages.*

Provider references: [Microsoft storage reliability counters](https://learn.microsoft.com/en-us/windows-hardware/drivers/storage/msft-storagereliabilitycounter), [Windows battery information](https://learn.microsoft.com/en-us/windows/win32/power/battery-information-str), [Windows battery-report command](https://learn.microsoft.com/en-us/windows-hardware/design/device-experiences/powercfg-command-line-options#batteryreport), [Linux power-supply ABI](https://www.kernel.org/doc/Documentation/ABI/testing/sysfs-class-power), [Apple battery health](https://support.apple.com/en-us/102589), and the [NVMe 2.0c specification](https://www.nvmexpress.org/wp-content/uploads/NVM-Express-Base-Specification-2.0c-2022.10.04-Ratified.pdf) (© 2008–2022 NVM Express, Inc.).
