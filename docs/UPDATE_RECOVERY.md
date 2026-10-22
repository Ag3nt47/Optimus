# Reviewing a deferred updater

An unresolved installer journal blocks another installation attempt. Maintenance can continue where independent work is safe. Deferred worker cards show a short explanation; their tooltips, accessibility descriptions and activity log retain the full record path and reason. Repeated warnings for the same record are grouped.

Version 0.8.0 could mark a software batch uncertain when its scan-cache check refused the next package before an installer was started. Version 0.9.2 includes an exact-record recovery tool for that historical error. It does not generally clear uncertain journals or retry failed updates.

Inspect one journal without opening the interface or running maintenance:

```text
Optimus.exe --update-recovery status --update-record <32-character-journal-id>
```

Reconciliation requires matching original journal, completed parent report, exact reviewed release source, approved package order and successful receipts for every earlier attempt. Historical runtime evidence must establish the original frozen release; missing launch arguments require an explicit, hash-bound operator review of native installer logs and current installer quiescence. An editable historical runtime, conflicting receipts, unfinished installer or unknown outcome remains blocked. A review is a technical investigation, not a substitute for missing evidence.

After that investigation, an operator can supply the reviewed evidence file:

```text
Optimus.exe --update-recovery reconcile --update-record <32-character-journal-id> --recovery-evidence <reviewed-evidence.json>
```

The tool holds the maintenance lock, checks evidence again and preserves exact original bytes, supporting evidence and an audit receipt under the local data directory's `update-recovery/<journal-id>` folder before changing metadata. It retains the failed-item count and earlier successful installations. Only the proven final preflight refusal is classified as having started no installer; the batch becomes `needs_review`. The command performs no cleanup, installation, process termination or reboot, and does not change saved approvals. Failed or incomplete reconciliation returns a nonzero exit status.

Existing completed run reports remain historical records. Start a fresh normal maintenance run after successful reconciliation so discovery can check current offers under the saved choices. A report that already deferred updates is not rewritten, and current disk-space, restart, signature, compatibility and approval checks still apply.
