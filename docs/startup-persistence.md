# Windows startup persistence safeguards

Optimus 0.12.4 checks the actual Windows system volume before enabling or restoring its owned sign-in task, or retargeting that task during an approved self-update. Moving Optimus or its temporary folder to another drive does not move Windows Task Scheduler's storage.

- Require at least 2 GiB of available system-volume space before startup manifest changes and check again immediately before registration. Unknown free space refuses the change.
- Verify an existing owned task against its nonempty, bounded, ordinary task file before changing it. Compare the persisted XML with the native registered definition after registration too. Only the schema-unordered Principal singleton fields are normalized for Windows serialization differences; values, attributes and other element order remain significant.
- Preserve existing ownership, task ACL and exact launch-action constraints. Self-update retargeting updates only the already owned task; it does not adopt or create a replacement.
- Retain bounded native exception types, HRESULTs and available Win32 codes when an operation fails. Unknown outcomes remain for review; there is no automatic retry or task deletion to hide a failed registration.

The 2 GiB check reduces the risk of a disk-full persistence failure. It is not an exclusive space reservation, and another application can still consume space after a check. A successful COM response alone does not establish that Windows saved a healthy task file. If the persisted file is inaccessible, malformed, empty or different, Optimus reports the uncertainty instead of claiming success.

These checks apply to Windows startup registration and its rollback, plus the self-update task retarget. They do not authorize extra file deletion, change saved preferences, run cleanup, reboot Windows or bypass administrator consent. An already damaged registration requires a separately reviewed repair; ordinary startup operations do not erase its evidence or automatically recreate it.
