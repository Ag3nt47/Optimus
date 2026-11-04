import json
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from optimus.app_removal_protection import EXCLUSIONS_KEY, WINDOWS_APP_RECORD
from optimus.backend import Backend
from optimus.ui import DetailedSettingsWindow
from optimus.unattended_configuration import approve_unattended_settings, matches_unattended
from optimus.workflow import Preferences, full_optimization_settings


def test_save_protection_keeps_other_raw_settings_and_narrows_approval(tmp_path):
    backend = Backend.__new__(Backend)
    backend.data_dir = tmp_path
    backend.preference_store = Preferences(tmp_path)
    current = full_optimization_settings()
    current.update(setup_version=2, unattended_auto_clean_enabled=True)
    current = approve_unattended_settings(Preferences.validate(current))
    current['unrelated_private_setting'] = 'retained'
    backend.preference_store.path.write_text(json.dumps(current), encoding='utf-8')
    assert backend.save_app_removal_exclusions([WINDOWS_APP_RECORD], [])['saved'] == [WINDOWS_APP_RECORD]
    saved = json.loads(backend.preference_store.path.read_text(encoding='utf-8'))
    assert all(saved[key] == value for key, value in current.items()
               if key not in (EXCLUSIONS_KEY, 'unattended_scope_hash'))
    assert matches_unattended(Preferences.validate(saved))
    with pytest.raises(ValueError, match='changed'):
        backend.save_app_removal_exclusions([], [])
    backend.save_app_removal_exclusions([], [WINDOWS_APP_RECORD])
    assert not matches_unattended(backend.preferences())


def test_search_keeps_hidden_protections_when_saving(tmp_path):
    app = QApplication.instance() or QApplication([])
    backend = SimpleNamespace(data_dir=tmp_path, preferences=lambda: {}, cleanup_categories=lambda: [])
    window = DetailedSettingsWindow(backend, smoke_test=True)
    desktop = {'kind': 'desktop', 'id': 'Editor.Install', 'name': 'Editor', 'label': 'Editor'}
    window._app_protection_ready({'saved': [desktop], 'installed': [desktop, WINDOWS_APP_RECORD]})
    window.protected_app_search.setText('Windows App')
    items = [window.protected_apps_list.item(i) for i in range(window.protected_apps_list.count())]
    visible = [item for item in items if not item.isHidden()]
    assert len(visible) == 1 and visible[0].data(Qt.UserRole) == WINDOWS_APP_RECORD
    visible[0].setCheckState(Qt.Checked)
    assert window.protected_app_save.isEnabled()
    assert desktop in window._checked_app_protections()
    assert WINDOWS_APP_RECORD in window._checked_app_protections()
    window.shutdown()
    window.close()
    window.deleteLater()
