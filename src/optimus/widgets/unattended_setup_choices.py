"""Unsaved unattended setup controls; approval is stamped only on Save."""
from PySide6.QtWidgets import QComboBox, QLabel

from ..unattended_configuration import validate_unattended_configuration


class UnattendedSetupChoices:
    def __init__(self, layout, initial, *, add_check, add_note):
        saved = validate_unattended_configuration(initial)
        self.enabled = add_check('unattended_auto_clean_enabled',
            'Enable unattended Auto Clean at sign-in and each launch for these saved choices')
        add_note('Optional and off by default. Saving this option approves the exact saved scopes and enables '
            'sign-in launch with startup maintenance. Later launches skip the launch warning and post-run prompts '
            'only while that scope still matches. Temporary files, selected browser history/cookies and selected '
            'apps can be permanently removed; cookies can sign you out. Per-app rules can reinstall missing '
            'apps. Individually selected retention cleanup can permanently remove archived personal Codex conversations, '
            'inactive Angerona logs and old incomplete or paused-download fragments. Per-app rules can also uninstall selected '
            'installed apps. Review all choices before Save. '
            'Changed scopes require fresh approval. Unknown installers, missing permissions and restarts are deferred.')
        label = QLabel('Open browsers during unattended runs')
        label.setWordWrap(True)
        layout.addWidget(label)
        self.browser_policy = QComboBox()
        self.browser_policy.addItem('Skip open browsers for this run', 'skip_open')
        self.browser_policy.addItem('Request graceful closure, then clean only verified closed browsers', 'close_gracefully')
        self.browser_policy.setCurrentIndex(max(0, self.browser_policy.findData(saved['unattended_browser_policy'])))
        self.browser_policy.setEnabled(self.enabled.isChecked())
        self.enabled.toggled.connect(self.browser_policy.setEnabled)
        layout.addWidget(self.browser_policy)
        add_note('Graceful closure can interrupt a browser session. Optimus never force-kills a browser or '
            'dismisses its save dialogs; browsers that remain open or whose state is unknown are skipped. '
            'Saving an unattended choice does not bypass Windows UAC or vendor/security prompts.')

    def collect(self):
        return {'unattended_auto_clean_enabled': self.enabled.isChecked(),
                'unattended_browser_policy': self.browser_policy.currentData()}
