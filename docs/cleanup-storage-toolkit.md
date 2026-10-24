# Application caches, storage growth and recovery retention

These independent tools are opt-in. Enabling Full Optimization does not approve
new application-cache deletion or expiry of old browser undo backups. Optimus
keeps the selected applications, retention age and resource bounds in the
Maintenance toolkit settings. Existing saved settings are not silently expanded.

## Application web-cache cleanup

Discord, Spotify, Steam and Teams recipes target only existing, fixed user cache
locations. Windows paths come from the native Shell known-folder API; macOS and
Linux use fixed home-relative locations. Teams has Windows/macOS recipes; there
is no Linux Teams recipe. Unrecognized installations are reported as unavailable.

Only known Chromium/Electron cache entry names inside `Cache` and `Code Cache`
are eligible. A recipe does not delete an entire profile. Cookies, account and
history databases, passwords, settings, offline music, completed downloads,
Steam games, game saves and GPU/shader cache directories are outside these
recipes. Removing web caches can make the next app launch rebuild or redownload
its cached web assets; it is not a promise of a faster subsequent launch.

The preview shows exact files and logical bytes. Apply consumes a session-bound
preview, expires after 15 minutes, and requires the same saved choices. Native
location authority, relevant process state and file identities are checked again.
An active application, an incomplete process inventory, open/locked files,
hardlinks, changed files, cloud placeholders or linked directory ancestry are
preserved. A locked entry does not prevent other eligible entries from being
checked. Entry, byte, time and minimum-age limits are independent controls.
Only confirmed removals contribute to the worker's deleted-byte total.

References for the underlying app cache conventions:

- [Discord's troubleshooting guide](https://support.discord.com/hc/en-us/articles/31623498041623-Discord-Troubleshooting-Guide)
- [Microsoft's Teams cache guidance](https://learn.microsoft.com/en-us/microsoftteams/troubleshoot/teams-administration/clear-teams-cache)

Optimus deliberately uses narrower web-cache leaves than a whole-application
reset, and does not implement the complete-profile deletion commands in those
guides. Spotify media storage is not an eligible cache category.

## Storage growth

Select up to 16 local directories below a drive root. The scanner reads metadata,
not file contents, and records logical file sizes and a bounded top-level folder
breakdown. It rejects remote drives/mounts, cloud placeholders, links and unknown
filesystem identity. Entry, time, depth and logical-byte budgets bound each run.
Hardlinks and other excluded/unreadable paths make the result partial.

Growth is shown only when both snapshots are complete, the root's device/inode
identity and depth configuration match, and the clock moved forward. Partial
scans never replace a complete baseline or claim that missing files were removed.
The latest 30 complete metadata observations are retained locally. The data
directory itself is excluded to avoid measuring the scanner's own history writes.

Application ownership is shown only for a fixed application-cache recipe match,
with that exact evidence included. Other selected folders remain unattributed;
a folder name alone is not evidence that an application owns it. Logical size is
not the same as allocated disk space or reclaimable bytes.

## Recovery retention and undo loss

Browser backup expiry requires both its own enable toggle and an explicit
approval that old browser cleanup can no longer be undone. The preview explains
the lost undo ability and total bytes for every eligible group. Saving this
approval permits the selected age policy on later unattended runs.

Only completely applied browser groups are eligible: exact backup identity,
valid age, a complete database inventory, matching hashes, ordinary unlinked
files and no unexpected children or restore activity. The entire group is
revalidated before expiry. Browser cleanup, restore and retention share an
exclusive lock across threads and processes; a busy operation is deferred.
Partial/prepared/failed/restored groups, pre-restore safety snapshots, unknown
files and changed backup groups remain protected. If a file cannot be removed,
the result explicitly reports a partial group and counts only confirmed deleted
bytes; it does not claim that undo is still available for a partially expired group.

Completed exported report copies created by this tool can expire separately.
The export must be a completed read-only folder-growth report or a completed
read-only health check with no unresolved collection errors. A caller cannot
turn an arbitrary dictionary into a report by adding a `complete` flag. The UI
archives only its own last eligible report; the export is disposable and original
maintenance journals remain in place. Driver
exports, system restore points, update journals, unresolved installer records and
other operation/recovery journals are never purged by this retention feature.

No cache cleanup, growth scan or retention operation uploads local metadata,
private browser databases or reports. Constructors start no monitor or background
service.

## Repeating fixture performance checks

Run `python tools/benchmark_cleanup_toolkit.py --entries 1000` from the source
checkout to measure preview, guarded deletion, metadata comparisons and backup
verification on a private temporary fixture. Add `--native-process-checks` to
include real read-only process-census overhead. All cleanup targets remain inside
the newly created fixture; existing apps, their caches and browser backups are
not cleaned. The JSON report distinguishes fixture timing from actual-machine
performance gains, and checks that the temporary fixture was removed.
