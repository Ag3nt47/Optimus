# Approved Python relocation repair

Windows Installer can reject a junction created without administrator access,
even when its target files have sufficient permissions. The Python MSI reports
Error 1326 and GetLastError 448: an untrusted mount point. Re-downloading the
installer or resetting file permissions does not repair that trust metadata.

Optimus supports an optional, narrow repair for a previously approved per-user
Python junction. The saved approval contains the exact WinGet Python package,
source path, target on another local drive and SHA-256 of the existing reparse
buffer. No personal path is shipped in the program. The repair setting remains
off for existing and new users unless explicitly approved; the Settings toggle
can revoke automatic repair without deleting its evidence.

Before an update, Optimus requires matching failed-installer evidence in its
owned, exact-offer verification directory. It locks and checks the source,
physical target and their ancestors, confirms ordinary local NTFS paths and
expected security descriptors, and rejects a namespace another account can
replace or change after the temporary handles close. Parent permissions are a
separate, durable prerequisite: approving a junction does not approve changes
to drive-root or shared-folder permissions. Ordinary creation of unrelated
siblings is considered separately from replacement and reparse-point rights.
The check reproduces error 448 in an isolated process
with RedirectionGuard enabled. A genuinely elevated process re-applies the
identical reparse buffer on the same source handle. It then verifies unchanged
identity/bytes and a successful enforced security read. Only then does the
normal updater run once, retaining its source, signature/hash, pin, disk,
installer-liveness, cancellation and installed-version checks.

This changes neither Windows security policy nor file permissions. It never
deletes or moves Python, removes other Python versions, erases failure journals,
automatically clicks UAC, or retries a still-running installer. A failed repair
is recorded for that package; unrelated eligible updates can continue. A new
or changed mapping requires new approval.

Microsoft documents how administrator creation/modification affects junction
trust in [RedirectionGuard: mitigating unsafe junction traversal](https://www.microsoft.com/en-us/msrc/blog/2025/06/redirectionguard-mitigating-unsafe-junction-traversal-in-windows)
and the [FSCTL_SET_REPARSE_POINT API](https://learn.microsoft.com/windows/win32/api/winioctl/ni-winioctl-fsctl_set_reparse_point).
The same-buffer repair is an implementation inference from that mechanism;
Optimus requires the separate enforced probe to confirm its result rather than
assuming the native call alone proves success.
