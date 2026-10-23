import os
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtWidgets import QApplication, QWidget

from optimus.smart_maintenance import SMART_DEFAULTS
from optimus.widgets.smart_maintenance_panel import SmartMaintenancePanel


def test_panel_construction_only_reads_preferences_and_cached_status():
    application = QApplication.instance() or QApplication([])
    owner = QWidget()
    owner.busy, owner.smoke_test = False, False
    owner.backend = SimpleNamespace(preferences=Mock(return_value=dict(SMART_DEFAULTS)),
        smart_maintenance_status=Mock(return_value={'status': 'disabled', 'running': False}),
        configure_smart_maintenance=Mock(), smart_maintenance_preview=Mock(), smart_maintenance_stop=Mock())
    owner._run = Mock()
    panel = SmartMaintenancePanel(owner)
    try:
        owner._run.assert_not_called()
        assert panel.choices()['smart_cleaning_threshold_bytes'] == 500000000
        panel.controls['smart_cleaning_enabled'].setChecked(True)
        panel.save()
        assert owner._run.call_args.args[0] == 'configure_smart_maintenance'
        assert owner._run.call_args.args[1]['smart_cleaning_enabled'] is True
        panel.preview()
        assert owner._run.call_args.args[0] == 'smart_maintenance_preview'
        assert owner._run.call_args.kwargs['cancel_event'] is panel._cancel
        panel.cancel_button.click()
        assert panel._cancel.is_set()
        owner.smoke_test = True
        owner._run.reset_mock()
        panel.save()
        panel.pause()
        owner._run.assert_not_called()
    finally:
        panel.shutdown()
        panel.deleteLater()
        owner.deleteLater()
        application.processEvents()


def test_missing_or_unverifiable_preferences_disable_new_actions_without_breaking_settings():
    application = QApplication.instance() or QApplication([])
    for reader in (None, Mock(side_effect=ValueError('Fixture malformed choices'))):
        owner = QWidget()
        owner.busy, owner.smoke_test = False, True
        owner.backend = SimpleNamespace()
        if reader is not None:
            owner.backend.preferences = reader
        owner._run = Mock()
        panel = SmartMaintenancePanel(owner)
        try:
            assert not panel.save_button.isEnabled() and not panel.preview_button.isEnabled()
            panel.preview()
            owner._run.assert_not_called()
            assert not panel._preferences_available
        finally:
            panel.shutdown()
            panel.deleteLater()
            owner.deleteLater()
            application.processEvents()
