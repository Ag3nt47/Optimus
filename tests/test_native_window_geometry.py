"""Responsive app windows use fixture screens, never the real desktop state."""
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QLabel, QMainWindow, QMessageBox, QVBoxLayout, QWizard

from test_native_worker_cards import fonts

from optimus.widgets import window_geometry as geometry


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def windows(app, monkeypatch):
    created = []
    available = QRect(0, 0, 1600, 1000)
    monkeypatch.setattr(geometry, 'available_screen_geometry', lambda _: QRect(available))
    old_quit = app.quitOnLastWindowClosed()
    app.setQuitOnLastWindowClosed(False)
    def make(kind=QMainWindow, *, parent=None, title='Fixture window', minimum=(0, 0), size=(900, 650)):
        window = kind(parent)
        window.setWindowTitle(title)
        window.setAttribute(Qt.WA_DontShowOnScreen, True)
        window.setMinimumSize(*minimum)
        window.resize(*size)
        created.append(window)
        return window
    yield make, available
    coordinator = getattr(app, '_optimus_window_geometry', None)
    for window in reversed(created):
        window.hide()
        window.deleteLater()
    app.processEvents()
    if coordinator is not None:
        coordinator.clear_session_geometry()
    app.setQuitOnLastWindowClosed(old_quit)


def settle(app):
    for _ in range(3):
        app.processEvents()


@pytest.mark.parametrize('bounds', [QRect(0, 0, 800, 600), QRect(1920, 0, 1280, 720),
                                  QRect(-1280, -200, 640, 480), QRect(0, 0, 640, 360)])
def test_logical_screen_bounds_cap_oversized_minima_and_include_frame(windows, app, bounds):
    make, _ = windows
    window = make(QDialog, minimum=(850, 650), size=(1440, 920))
    window.show()
    settle(app)
    geometry.fit_window_to_screen(window, available=bounds)
    settle(app)
    assert bounds.contains(window.frameGeometry())
    assert window.minimumWidth() <= bounds.width() - 2 * geometry.SCREEN_MARGIN
    assert window.minimumHeight() <= bounds.height() - 2 * geometry.SCREEN_MARGIN


def test_global_coordinator_only_fits_owned_content_and_never_os_prompts(windows, app):
    make, available = windows
    available.setRect(0, 0, 800, 600)
    owner = make(minimum=(640, 480), size=(1280, 850))
    coordinator = geometry.install_window_geometry(owner)
    owner.show()
    child = make(QDialog, parent=owner, size=(1440, 920))
    child.show()
    unrelated = make(QDialog, title='Unrelated application fixture', size=(1100, 850))
    unrelated.show()
    chooser = make(QFileDialog, parent=owner, size=(1100, 850))
    # Native file dialogs and message boxes are excluded without displaying or
    # interacting with an OS chooser in the fixture.
    prompt = make(QMessageBox, parent=owner)
    settle(app)
    assert available.contains(owner.frameGeometry()) and available.contains(child.frameGeometry())
    assert unrelated.width() == 1100 and unrelated.height() == 850
    assert not coordinator._owned(chooser) and not coordinator._owned(prompt)
    assert child.isSizeGripEnabled()


def test_first_open_centers_and_caps_to_owner_but_user_resize_survives_reopen(windows, app):
    make, _ = windows
    owner = make(size=(1000, 720))
    geometry.install_window_geometry(owner)
    owner.show()
    settle(app)
    child = make(QDialog, parent=owner, minimum=(320, 240), size=(1440, 920))
    child.show()
    settle(app)
    assert owner.geometry().contains(child.frameGeometry())
    assert abs(owner.geometry().center().x() - child.frameGeometry().center().x()) <= 2
    assert abs(owner.geometry().center().y() - child.frameGeometry().center().y()) <= 2
    child.resize(1150, 800)  # User may intentionally outgrow the owner.
    settle(app)
    chosen = child.size()
    child.hide()
    child.show()
    settle(app)
    assert child.size() == chosen


def test_recreated_window_retains_chosen_session_geometry_without_file_writes(windows, app):
    make, _ = windows
    owner = make(size=(1100, 780))
    geometry.install_window_geometry(owner)
    owner.show()
    first = make(QDialog, parent=owner, title='Remember this dialog', size=(850, 650))
    first.show()
    settle(app)
    first.resize(700, 500)
    first.move(90, 100)
    settle(app)
    chosen = first.geometry()
    first.hide()
    second = make(QDialog, parent=owner, title='Remember this dialog', size=(1400, 900))
    second.show()
    settle(app)
    assert second.geometry() == chosen


def test_new_detached_window_follows_owner_on_secondary_screen(windows, app, monkeypatch):
    make, _ = windows
    primary, secondary = QRect(0, 0, 800, 600), QRect(1600, 0, 1200, 800)
    owner = make(size=(1000, 700))
    monkeypatch.setattr(geometry, 'available_screen_geometry', lambda window:
        QRect(secondary if window is owner or window.frameGeometry().left() >= 1600 else primary))
    coordinator = geometry.install_window_geometry(owner)
    owner.move(1660, 30)
    owner.show()
    settle(app)
    child = make(QDialog, title='Detached tools', minimum=(320, 240), size=(1440, 920))
    child._main_window = owner
    coordinator._initial_geometry(child)
    assert secondary.contains(child.frameGeometry())
    assert owner.geometry().contains(child.frameGeometry())


def test_identical_dialog_title_under_different_owner_never_reuses_other_monitor_geometry(windows, app, monkeypatch):
    make, _ = windows
    primary, secondary = QRect(0, 0, 1400, 900), QRect(1600, 0, 1200, 800)
    first_owner = make(size=(1100, 780), title='First owner')
    second_owner = make(size=(1000, 700), title='Second owner')
    monkeypatch.setattr(geometry, 'available_screen_geometry', lambda window:
        QRect(secondary if window is second_owner or window.parentWidget() is second_owner
              or window.frameGeometry().left() >= 1600 else primary))
    coordinator = geometry.install_window_geometry(first_owner)
    geometry.install_window_geometry(second_owner)
    first_owner.show()
    second_owner.move(1660, 30)
    second_owner.show()
    settle(app)
    first = make(QDialog, parent=first_owner, title='Same dialog role', size=(850, 650))
    first.show()
    settle(app)
    first.resize(700, 500)
    first.move(100, 100)
    first.hide()
    second = make(QDialog, parent=second_owner, title='Same dialog role', size=(900, 650))
    second.show()
    settle(app)
    assert secondary.contains(second.frameGeometry())
    assert second_owner.geometry().contains(second.frameGeometry())
    assert len(coordinator._session) >= 1
    # The first owner's subsequent replacement still receives its own geometry.
    replacement = make(QDialog, parent=first_owner, title='Same dialog role', size=(1000, 700))
    replacement.show()
    settle(app)
    assert (replacement.width(), replacement.height()) == (700, 500)
    assert primary.contains(replacement.frameGeometry())


def test_smaller_work_area_refits_without_reapplying_author_default_size(windows, app):
    make, available = windows
    owner = make(minimum=(640, 480), size=(1400, 900))
    coordinator = geometry.install_window_geometry(owner)
    owner.show()
    settle(app)
    owner.resize(900, 650)
    settle(app)
    coordinator._screen_changed()
    settle(app)
    assert (owner.width(), owner.height()) == (900, 650)
    available.setRect(0, 0, 700, 500)
    coordinator._screen_changed()
    settle(app)
    assert available.contains(owner.frameGeometry())
    # Increasing screen area must not expand the user's chosen/fitted window.
    chosen = owner.size()
    available.setRect(0, 0, 1600, 1000)
    coordinator._screen_changed()
    settle(app)
    assert owner.size() == chosen


def test_dialog_controls_remain_reachable_in_small_screen_fixture(windows, app):
    make, available = windows
    available.setRect(0, 0, 640, 480)
    owner = make(size=(900, 650))
    geometry.install_window_geometry(owner)
    dialog = make(QDialog, parent=owner, minimum=(640, 480), size=(850, 650))
    body = QVBoxLayout(dialog)
    controls = [QLabel('Reachable fixture control ' + str(index)) for index in range(4)]
    for label in controls:
        label.setWordWrap(True)
        body.addWidget(label)
    dialog.show()
    settle(app)
    assert available.contains(dialog.frameGeometry())
    assert all(label.isVisibleTo(dialog) and dialog.rect().contains(label.geometry()) for label in controls)


@pytest.mark.parametrize('screen_height', [480, 360])
@pytest.mark.parametrize('kind', ['main', 'settings', 'native-settings', 'setup', 'native-setup', 'sandbox',
                                  'progress', 'security-review', 'privacy-restore'])
def test_real_content_windows_fit_small_work_area_with_visible_primary_controls(windows, app, tmp_path, monkeypatch, kind, screen_height):
    import threading
    from test_native_critical_review import bot
    from optimus.native_ui import NativeSettingsWindow, NativeSetupWizard
    from optimus.onboarding import InitialSetupWizard
    from optimus.sandbox import SandboxWindow
    from optimus.ui import DetailedSettingsWindow, OptimizationProgressWindow, OptimusWindow, PrivacyRestoreDialog, SecurityReviewDialog

    _, available = windows
    available.setRect(0, 0, 640, screen_height)
    backend = bot.FixtureBackend(tmp_path / 'fixture-state', 'win32')
    source = tmp_path / 'fixture-source'
    (source / 'src/optimus').mkdir(parents=True)
    (source / 'src/optimus/ui.py').write_text('VALUE = 1\n')
    backend.source_root = lambda: source
    monkeypatch.setattr(app, 'quit', lambda: None)
    owner = OptimusWindow(backend, smoke_test=True)
    owner.setAttribute(Qt.WA_DontShowOnScreen, True)
    owner.show()
    app.setQuitOnLastWindowClosed(False)
    settle(app)
    child = None
    controls = []
    try:
        if kind == 'main':
            child, controls = owner, owner.main_buttons
        elif kind == 'settings':
            child = DetailedSettingsWindow(backend, smoke_test=True)
            child._main_window = owner
        elif kind == 'native-settings':
            child = NativeSettingsWindow(backend, owner, smoke_test=True)
        elif kind == 'setup':
            child = InitialSetupWizard(backend, owner)
            controls = [child.button(QWizard.WizardButton.NextButton), child.button(QWizard.WizardButton.CancelButton)]
        elif kind == 'native-setup':
            child = NativeSetupWizard(backend, owner)
            controls = [child.button(QWizard.WizardButton.NextButton), child.button(QWizard.WizardButton.CancelButton)]
        elif kind == 'sandbox':
            child = SandboxWindow(backend)
            controls = [button for button in child.findChildren(bot.QAbstractButton)
                        if button.text() in ('Save', 'Revert', 'Restart Optimus', 'Export project')]
        elif kind == 'progress':
            child = OptimizationProgressWindow(owner, threading.Event())
            controls = [child.cancel_button]
        elif kind == 'security-review':
            child = SecurityReviewDialog(backend, {'recommendations': []}, owner)
            controls = [child.close_button]
        else:
            child = PrivacyRestoreDialog(owner, {'items': []})
            controls = [child.close_button]
        child.setAttribute(Qt.WA_DontShowOnScreen, True)
        child.show()
        settle(app)
        if kind == 'progress':
            # Small desktops fit the header/footer while the ordinary body
            # scrolls each unchanged worker into its real viewport.
            assert available.contains(child.frameGeometry())
            controls.extend([child.fullscreen.button, child.details_toggle])
            for card in child.worker_cards.values():
                assert child.body_scroll.widget().rect().contains(card.rect().translated(card.mapTo(child.body_scroll.widget(), QPoint())))
                for control in card.navigation_buttons.values():
                    child.body_scroll.ensureWidgetVisible(control, 0, 2)
                    settle(app)
                    assert child.body_scroll.viewport().rect().contains(control.rect().translated(control.mapTo(child.body_scroll.viewport(), QPoint())))
                    measured = bot.measure_button(control, child, (child.width(), child.height()), app)
                    assert measured['status'] == 'passed', (kind, measured)
                    hit = child.childAt(control.mapTo(child, control.rect().center()))
                    assert hit is control or hit is not None and control.isAncestorOf(hit)
        else:
            assert available.contains(child.frameGeometry()), (kind, child.frameGeometry(), child.minimumSizeHint())
        for control in controls:
            measured = bot.measure_button(control, child, (child.width(), child.height()), app)
            assert measured['status'] == 'passed', (kind, measured['label'], measured['geometry'],
                measured['visible_region'], measured['minimum_text_width'], measured['visible'])
    finally:
        if child is not None and child is not owner:
            if isinstance(child, DetailedSettingsWindow):
                child.shutdown()
            child.hide()
            child.deleteLater()
        owner._quitting = True
        owner.shutdown()
        owner.hide()
        owner.deleteLater()
        settle(app)
