# Worker dashboard and navigation

The maintenance, application-update, driver-update and virus/malware-scan cards stay in the main dashboard with separate overall and current-item progress. The header retains overall workflow progress and the deleted/downloaded comparison. A responsive bottom rectangle contains the gag animation, live computer/network resource usage and disk-buffer targets, observed free space and shortfalls. These stay outside the Details toggle.

Only Current processes and Stage log collapse behind **Show/Hide details and logs**. The main body scrolls vertically when needed instead of forcing controls outside the screen. Primary actions remain reachable; full-screen buttons and F11 expand the window, and Escape returns from full screen without cancelling the current worker. Tooltips and accessible descriptions retain complete explanations when labels are shortened.

Full-screen key handling stays shared across panels, including panels retained while hidden. Reopening panels does not add another application-wide handler for every window. The controls preserve each window's normal or maximized size without saving new settings.

After completion, verification evidence and the exact available-fix scope stay in a bounded review area above the action buttons. Their read-only panes can scroll independently. Collapsing the logs does not hide these results or add another confirmation to the existing manual Apply action.

## Next / skip

Next is a request for the current item in that worker. The interface acknowledges it immediately and binds it to that exact live item, rather than retargeting an old click to a later operation. Other workers continue. Back only reviews saved item results and does not replay completed actions.

| Current operation | Response to Next |
| --- | --- |
| Cleanup discovery | Stop at a checked boundary; a cancelled preview cannot authorize deletion |
| Temporary-file removal | Finish the current file check, stop before another deletion, save actual counts and preserve remaining files |
| App source/pin checks | Retire the owned read-only query and verify its exit before advancing |
| Windows Update driver search/download | Request abort on the exact native job and wait for its matching terminal result |
| App/vendor or driver installation already applying changes | Show the pending skip; wait for the actual installer result and required safety checks before advancing |
| Defender quick scan | Stop Optimus's owned client/monitoring at the supported boundary; Windows may continue scanning under its own policy |

Next cannot guarantee an immediate stop for a provider that has no safe cancellation API. Optimus does not kill Windows Installer, Defender, update services or arbitrary vendor processes. A timed-out or otherwise unknown native outcome stays visible and continues to block conflicting writes; a skip does not erase that evidence or silently retry an installer. Saved approvals and package/device identity checks still apply to the next item.

## Sandbox and hover descriptions

Sandbox exposes an editable copy of the full source. Its file tree and code pane grow with the window; Save, Revert, History, Original version, Export and Restart remain available above the editor. Existing saved source versions and unsaved-edit questions remain in effect. Restart uses the saved working copy rather than silently changing installed release files.

Main actions, worker arrows, Sandbox controls and dynamically created buttons have short hover descriptions. Existing tool-specific safety and state explanations take precedence over generic descriptions.
