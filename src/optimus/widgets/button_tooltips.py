"""Short action descriptions for current and dynamically created controls."""
from __future__ import annotations

from html import escape

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QAbstractButton, QApplication, QCheckBox, QRadioButton

_DESCRIPTIONS = {
    '1 click optimize and debloat': 'Run your saved cleanup, update and optimization choices.',
    'settings': 'Choose what Optimus may clean, update, remove or run automatically.',
    'sandbox': 'Edit the full Optimus source code, save versions, revert edits and restart the edited copy.',
    'revert': 'Review past changes and undo selected changes where recovery is available.',
    'save': 'Save the choices or edits shown in this window.',
    'original version': 'Restore the original source in your Sandbox working copy.',
    'history': 'Review saved source versions and choose a revision to restore.',
    'restart optimus': 'Restart Optimus using the saved Sandbox working copy.',
    'export project': 'Export the editable source project as a ZIP archive.',
    'auto clean': 'Approve the displayed saved actions and start maintenance.',
    'adjust settings': 'Review your choices while automatic maintenance stays paused.',
    'select all': 'Select all available choices in this section; selection alone does not run them.',
    'clear selection': 'Clear the selected choices in this section.',
    'clear all': 'Clear all available choices in this section.',
    'deselect all': 'Clear the selected choices in this section.',
    'select none': 'Clear the selected choices in this section.',
    'close results': 'Close these results without starting additional recommendations.',
    'continue to recommendations': 'Review remaining recommendations and their individual approval choices.',
    'review selected recommendations': 'Review each selected recommendation before approving its action.',
    'not now / close': 'Close this review without approving further changes.',
    'yes': 'Approve the action described in this question.',
    'no': 'Decline the action described in this question.',
    'browse': 'Choose a file or folder for this option.',
    'cancel': 'Cancel this operation or close this question without approving it.',
    'close': 'Close this window.',
    'back': 'Return to the previous page or item.',
    'next': 'Continue to the next page or item.',
    'finish': 'Save the reviewed setup choices and finish setup.',
    'discard': 'Discard unsaved changes described in this question.',
    'ok': 'Accept the choices shown in this dialog.',
    'follow global / leave installed state': 'Remove this app’s custom rule and follow global update choices without changing its installed state.',
    'copy code': 'Copy the displayed sign-in code to the clipboard.',
    'pause for this session': 'Pause automatic monitoring until the next Optimus session.',
    'resume paused apps': 'Resume apps previously paused through Optimus.',
    'show details and logs': 'Expand worker explanations, measurements and the stage log.',
    'hide details and logs': 'Collapse the extra details while keeping the four workers visible.',
}
_NAVIGATION = {
    'next': 'Request a safe skip of the current item and continue to the next. An active installer may need to finish.',
    'back': 'Review previous item results without replaying completed changes.',
    'cancel': 'Cancel this worker’s current item at the next supported safe point.',
}


def button_description(button):
    action = button.property('_optimus_navigation_action')
    if isinstance(action, str) and action in _NAVIGATION:
        return _NAVIGATION[action]
    name = ' '.join(button.text().replace('&&', '\0').replace('&', '').replace('\0', '&').split())
    folded = name.casefold().rstrip('.…')
    window = button.window()
    if type(window).__name__ == 'SandboxWindow':
        if folded == 'save':
            return 'Save edits to the current source file and retain its previous version.'
        if folded == 'revert':
            return 'Restore a saved revision of the current Sandbox file.'
    if folded in _DESCRIPTIONS:
        return _DESCRIPTIONS[folded]
    safe_name = escape(name[:160], quote=False)
    if isinstance(button, (QCheckBox, QRadioButton)):
        return 'Choose this option: ' + safe_name if safe_name else 'Choose this option.'
    if folded.startswith('scan'):
        return 'Inspect the selected scope and show its findings.'
    if folded.startswith('preview'):
        return 'Show the proposed scope and actions before applying them.'
    if folded.startswith(('refresh ', 'load ', 'check ', 'analyze ', 'find ')):
        return 'Read the current information for this view; this button does not apply maintenance changes.'
    if folded.startswith(('add ', 'choose ', 'browse')):
        return 'Choose an item to add to this panel’s scope or saved configuration.'
    if folded.startswith(('select all ', 'clear all ')):
        return 'Select all choices in this group.' if folded.startswith('select') else 'Clear all choices in this group.'
    if folded.startswith(('yes ', 'continue current version')):
        return 'Start the offered Optimus update before continuing.' if folded.startswith('yes ') else 'Continue with the installed Optimus version.'
    if folded.startswith('no '):
        return 'Decline the offered action and continue with the current version.'
    if folded.startswith(('cancel ', 'stop ')):
        return 'Request a safe stop. Active native work may need to return before stopping.'
    if folded.startswith('save '):
        return 'Save the choices shown in this panel for future use.'
    if folded.startswith('configure '):
        return 'Open configuration choices for this feature.'
    if folded.startswith(('apply ', 'set ')):
        return 'Apply the choices shown in this panel using its saved scope and checks.'
    if folded.startswith(('delete ', 'remove ', 'uninstall ', 'wipe ')):
        return 'Run this removal action on the selected scope; review its description and recovery limits.'
    if folded.startswith(('open ', 'show ', 'view ', 'review ')):
        return 'Open the requested view: ' + safe_name + '.'
    if folded.startswith(('export ', 'download ')):
        return 'Save the selected export or download to the chosen destination.'
    if folded.startswith(('restore ', 'revert ', 'undo ')):
        return 'Review or restore the selected saved change where recovery is supported.'
    if folded.startswith(('clean ', 'clear ', 'trash ', 'recycle ')):
        return 'Clean or recycle the selected data using this panel’s scope and safety checks.'
    if folded.startswith('install '):
        return 'Install the selected package using its supported provider and saved approvals.'
    if folded.startswith('run setup'):
        return 'Open the step-by-step setup questions to review and change your saved choices.'
    accessible = escape(' '.join(button.accessibleName().split())[:160], quote=False)
    return 'Activate: ' + (safe_name or accessible) + '.' if safe_name or accessible else 'Activate this control.'


class ButtonDescriptions(QObject):
    def describe(self, button):
        current = button.toolTip()
        previous = button.property('_optimus_auto_tooltip')
        if current and current != previous:
            return  # Keep the tool's more specific, possibly stateful description.
        description = button_description(button)
        button.setProperty('_optimus_auto_tooltip', description)
        button.setToolTip(description)
        if not button.accessibleDescription() or button.accessibleDescription() == previous:
            button.setAccessibleDescription(description)

    def eventFilter(self, watched, event):
        if isinstance(watched, QAbstractButton) and event.type() in (QEvent.Polish, QEvent.ToolTip, QEvent.Enter):
            self.describe(watched)
        return False


def install_button_descriptions(owner):
    app = QApplication.instance()
    if app is None:
        return None
    helper = getattr(app, '_optimus_button_descriptions', None)
    if helper is None:
        helper = ButtonDescriptions(app)
        app.installEventFilter(helper)
        app._optimus_button_descriptions = helper
    for button in owner.findChildren(QAbstractButton):
        helper.describe(button)
    return helper
