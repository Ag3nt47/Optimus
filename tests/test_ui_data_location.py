"""Moving app state must cover appearance reads/writes and reloads too."""
import json
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from optimus.ui import DetailedSettingsWindow, OptimusWindow
from optimus.widgets.theme import THEMES


@pytest.fixture(scope='module')
def qapp():
    return QApplication.instance() or QApplication([])


def dispose(window, qapp):
    window.shutdown()
    window.close()
    window.deleteLater()
    qapp.processEvents()


def test_appearance_saves_to_moved_data_and_reopens_without_legacy_directory(qapp, tmp_path, monkeypatch):
    local = tmp_path / 'legacy-local'
    moved = tmp_path / 'moved-data'
    monkeypatch.setenv('LOCALAPPDATA', str(local))
    backend = SimpleNamespace(data_dir=moved, preferences=lambda: {})
    detail = DetailedSettingsWindow(backend, smoke_test=True)
    try:
        detail.theme_combo.setCurrentIndex(detail.theme_combo.findData(next(iter(THEMES))))
        detail.motion_toggle.setChecked(False)
        detail.orb_toggle.setChecked(False)
        detail.save_preferences()
        expected = {'theme': next(iter(THEMES)), 'motion': False, 'orb': False}
        assert detail.settings_path == moved / 'ui-settings.json'
        assert json.loads(detail.settings_path.read_text()) == expected
        assert not (local / 'Optimus').exists()
        assert not list(moved.glob('*.tmp'))
        main = OptimusWindow(backend, smoke_test=True)
        reopened = DetailedSettingsWindow(backend, smoke_test=True)
        try:
            assert main.preferences_path == detail.settings_path
            assert main.appearance == expected and reopened.preferences == expected
        finally:
            dispose(main, qapp)
            dispose(reopened, qapp)
    finally:
        dispose(detail, qapp)


def test_moved_appearance_authority_wins_over_stale_legacy_settings(qapp, tmp_path, monkeypatch):
    local, moved = tmp_path / 'legacy-local', tmp_path / 'moved-data'
    stale = local / 'Optimus' / 'ui-settings.json'
    stale.parent.mkdir(parents=True)
    stale.write_text(json.dumps({'theme': next(iter(THEMES)), 'motion': True, 'orb': True}))
    original = stale.read_bytes()
    moved.mkdir()
    selected = {'theme': next(reversed(THEMES)), 'motion': False, 'orb': False}
    (moved / 'ui-settings.json').write_text(json.dumps(selected))
    monkeypatch.setenv('LOCALAPPDATA', str(local))
    backend = SimpleNamespace(data_dir=moved, preferences=lambda: {})
    main = OptimusWindow(backend, smoke_test=True)
    detail = DetailedSettingsWindow(backend, smoke_test=True)
    try:
        assert main.appearance == selected and detail.preferences == selected
        detail.save_preferences()
        assert stale.read_bytes() == original
        assert json.loads((moved / 'ui-settings.json').read_text()) == selected
    finally:
        dispose(main, qapp)
        dispose(detail, qapp)


def test_default_performance_controller_uses_installation_data_authority(tmp_path, monkeypatch):
    from optimus import performance
    moved = tmp_path / 'moved-data'
    installation = tmp_path / 'portable-app'
    installation.mkdir()
    (installation / 'Optimus.location.json').write_text(json.dumps({'schema': 1, 'data_dir': str(moved)}))
    monkeypatch.setenv('OPTIMUS_ORIGINAL_SOURCE', str(installation))
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'unused-legacy-root'))
    controller = performance.PerformanceController()
    assert controller.state_path == moved / 'suspended-processes.json'
    assert not moved.exists()
