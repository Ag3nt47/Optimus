"""Live worker controls use cached, exact-session commands and disposable Qt fixtures."""
import copy
import os
from pathlib import Path
import threading
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from optimus.ui import OptimizationProgressWindow, WorkflowWorkerCard
from optimus.widgets.worker_navigation import WorkerNavigationController


@pytest.fixture(scope='module')
def app():
    app = QApplication.instance() or QApplication([])
    loaded = []
    if os.name == 'nt' and not QFontDatabase.families():
        for name in ('segoeui.ttf', 'seguisb.ttf'):
            path = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'Fonts' / name
            if path.is_file():
                loaded.append(QFontDatabase.addApplicationFont(str(path)))
    yield app
    for identity in loaded:
        if identity >= 0:
            QFontDatabase.removeApplicationFont(identity)


class BackendFixture:
    def __init__(self):
        self.snapshot = {'session_id': 'a' * 32, 'active': True, 'jobs': [dict(id='software_updates', revision=3,
            can_back=True, can_next=True, can_cancel=True, pending=False,
            back_reason='Review the previous result.', next_reason='Skip the current exact package.',
            cancel_reason='Cancel only this package.') ]}
        self.requests = []
        self.accept = True

    def workflow_navigation_status(self):
        return copy.deepcopy(self.snapshot)

    def request_workflow_navigation(self, session, identity, revision, action):
        self.requests.append((session, identity, revision, action))
        if self.accept:
            self.snapshot['jobs'][0]['pending'] = action
        return {'accepted': self.accept, 'message': 'Waiting for current installer.' if self.accept else 'Stale item; refresh.'}


@pytest.fixture
def progress(app):
    owner = QWidget()
    owner.backend = BackendFixture()
    value = OptimizationProgressWindow(owner, threading.Event())
    value.show()
    app.processEvents()
    yield value, owner.backend
    value.completed = True
    value.spinner.stop()
    value.close()
    owner.close()
    value.deleteLater()
    owner.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()


@pytest.mark.parametrize('action', ['back', 'next', 'cancel'])
def test_each_button_submits_the_exact_visible_session_job_and_revision(progress, action):
    window, backend = progress
    card = window.worker_cards['software_updates']
    button = card.navigation_buttons[action]
    assert button.isEnabled() and button.accessibleName()
    QTest.mouseClick(button, Qt.LeftButton)
    assert backend.requests == [('a' * 32, 'software_updates', 3, action)]
    assert all(not item.isEnabled() for item in card.navigation_buttons.values())
    assert 'Waiting for current installer' in card.navigation_note.text()
    assert not window.cancel.is_set()  # Per-item navigation is not whole-run cancellation.


def test_stale_click_is_not_retargeted_to_a_new_revision(progress):
    window, backend = progress
    backend.accept = False
    card = window.worker_cards['software_updates']
    backend.snapshot['jobs'][0]['revision'] = 4
    card.navigation_buttons['next'].click()
    assert backend.requests[0][2] == 3
    assert 'Stale item' in card.navigation_note.text()
    assert card._navigation_state['revision'] == 4


@pytest.mark.parametrize('input_mode', ['mouse', 'keyboard'])
@pytest.mark.parametrize('change_session', [False, True])
def test_refresh_between_press_and_release_never_retargets_the_command(progress, input_mode, change_session):
    window, backend = progress
    backend.accept = False
    card = window.worker_cards['software_updates']
    button = card.navigation_buttons['cancel']
    if input_mode == 'mouse':
        QTest.mousePress(button, Qt.LeftButton)
    else:
        button.setFocus()
        QTest.keyPress(button, Qt.Key_Space)
    backend.snapshot['jobs'][0]['revision'] = 4
    if change_session:
        backend.snapshot['session_id'] = 'b' * 32
    window.worker_navigation.refresh()
    if input_mode == 'mouse':
        QTest.mouseRelease(button, Qt.LeftButton)
    else:
        QTest.keyRelease(button, Qt.Key_Space)
    assert backend.requests == [('a' * 32, 'software_updates', 3, 'cancel')]
    assert card._navigation_state['revision'] == 4
    assert 'Stale item' in card.navigation_note.text()
    assert not card._navigation_presses


def test_aborted_mouse_press_clears_snapshot_and_unpaired_clicked_never_retargets(progress, app):
    window, backend = progress
    card = window.worker_cards['software_updates']
    button = card.navigation_buttons['next']
    QTest.mousePress(button, Qt.LeftButton)
    QTest.mouseRelease(button, Qt.LeftButton, pos=QPoint(-4, -4))
    app.processEvents()
    assert not backend.requests and not card._navigation_presses
    button.clicked.emit()
    assert not backend.requests
    button.click()  # Ordinary programmatic activation still emits pressed/clicked.
    assert backend.requests == [('a' * 32, 'software_updates', 3, 'next')]


def test_no_backend_capability_and_extra_status_workers_have_disabled_controls(app):
    card = WorkflowWorkerCard('Optional worker')
    controller = WorkerNavigationController(SimpleNamespace(), {'extra': card}, card)
    controller.refresh()
    assert all(not button.isEnabled() for button in card.navigation_buttons.values())
    assert 'No live eligible item' in card.navigation_buttons['next'].toolTip()
    card.close()


def test_unknown_status_does_not_authorize_navigation(progress):
    window, backend = progress
    backend.snapshot = {'session_id': 'a' * 32, 'jobs': [{'id': 'software_updates',
        'revision': True, 'can_next': True, 'can_back': 'true', 'can_cancel': 1}]}
    window.worker_navigation.refresh()
    assert all(not button.isEnabled() for button in window.worker_cards['software_updates'].navigation_buttons.values())


def test_status_failure_disables_commands_without_starting_any_work(progress):
    window, backend = progress
    def fail():
        raise RuntimeError('Fixture status unavailable')
    backend.workflow_navigation_status = fail
    window.worker_navigation.refresh()
    assert not backend.requests
    assert all(not button.isEnabled() for card in window.worker_cards.values() for button in card.navigation_buttons.values())


def test_missing_active_state_never_authorizes_a_command(progress):
    window, backend = progress
    del backend.snapshot['active']
    window.worker_navigation.refresh()
    assert all(not button.isEnabled() for card in window.worker_cards.values() for button in card.navigation_buttons.values())


def test_request_failure_has_a_readable_outcome_without_an_unbound_result(progress):
    window, backend = progress
    def fail(*args):
        raise RuntimeError('Fixture request failed')
    backend.request_workflow_navigation = fail
    card = window.worker_cards['software_updates']
    card.navigation_buttons['next'].click()
    assert 'could not be verified' in card.navigation_note.text()
    assert not window.cancel.is_set()


def test_history_keeps_live_status_and_shows_recorded_result(progress):
    window, backend = progress
    row = backend.snapshot['jobs'][0]
    row.update(viewing_history=True, next_mode='history',
        view_item={'label': 'Earlier package', 'status': 'completed', 'message': 'Verified earlier result.'})
    window.update_progress({'stage': 'Continue maintenance', 'percentage': 45,
        'jobs': [{'id': 'software_updates', 'status': 'installing', 'current': 1, 'total': 3,
                  'description': 'Current package stays live.'}]})
    card = window.worker_cards['software_updates']
    assert 'Earlier package' in card.navigation_note.text()
    assert 'Verified earlier result' in card.navigation_note.text()
    assert 'Current package' in card.details.text()
    assert 'Next saved result' in card.navigation_buttons['next'].accessibleName()


def test_worker_activity_has_separate_progress_cpu_and_waiting_state(progress):
    window, backend = progress
    window.update_progress({'stage': 'Measured operation', 'percentage': 50,
        'jobs': [{'id': 'software_updates', 'status': 'installing', 'current': 1, 'total': 4}]})
    card = window.worker_cards['software_updates']
    card.set_live_metrics({'cpu_percent': 2.5, 'scope': 'Thread CPU only'}, {'pressure': 'normal'})
    assert card.completion.text() == 'Progress: 25%'
    assert card.activity_ring.value_text() == '2.5%'
    assert card.activity_ring.health == 'working'
    card.update_state({'status': 'waiting_for_installer', 'current': 1, 'total': 4})
    card.set_live_metrics({'cpu_percent': 0.0}, {'pressure': 'normal'})
    assert card.activity_ring.value_text() == '0.0%'
    assert card.activity_ring.health == 'waiting'
    assert 'Waiting' in card.activity_state.text()
    card.update_state({'status': 'running'})
    card.set_live_metrics({'cpu_percent': None}, {'pressure': 'high'})
    assert card.activity_ring.health == 'pressure'
    assert card.activity_ring.value_text() == '—'


def test_connection_strip_reports_cached_internet_state_and_passive_rate(app):
    from optimus.widgets.live_activity import LiveSystemStatus
    strip = LiveSystemStatus()
    strip.update_metrics({'computer': {'cpu_percent': 30, 'memory_percent': 91,
        'cpu_frequency_mhz': 3200, 'pressure': 'high'}, 'network': {'kind': 'wifi',
        'adapter': 'Fixture WiFi', 'is_up': True, 'receive_mbps': 15.5,
        'send_mbps': .2, 'link_mbps': 433, 'internet_state': 'os_reported_internet',
        'internet_report_age_seconds': 120}})
    assert 'CPU 30.0%' in strip.computer.text() and '3.20 GHz' in strip.computer.text()
    assert 'Resource pressure' in strip.computer.text()
    assert 'Wi-Fi' in strip.network.text() and '15.50 Mbps' in strip.network.text()
    assert '120s old' in strip.network.text()
    strip.update_metrics({'computer': {'cpu_percent': float('nan')}, 'network': {}})
    assert 'CPU —' in strip.computer.text() and 'unverified' in strip.network.text().lower()
    strip.close()


def test_activity_ring_pauses_hidden_and_honors_reduced_motion(app, monkeypatch):
    from optimus.widgets.live_activity import WorkerActivityRing
    monkeypatch.setattr('optimus.widgets.header_controls.motion_allowed', lambda config=None: True)
    ring = WorkerActivityRing()
    ring.set_activity(1.2, 'working', active=True)
    ring.show()
    app.processEvents()
    assert ring.timer.isActive()
    ring.hide()
    app.processEvents()
    assert not ring.timer.isActive()
    ring.show()
    app.processEvents()
    assert ring.timer.isActive()
    monkeypatch.setattr('optimus.widgets.header_controls.motion_allowed', lambda config=None: False)
    ring.set_activity(0, 'waiting', active=True)
    assert not ring.timer.isActive()
    ring.close()


@pytest.mark.parametrize('completion', ['cancel', 'finish'])
def test_global_cancel_or_completed_results_disable_all_navigation_immediately(progress, completion):
    window, backend = progress
    if completion == 'cancel':
        window.request_close()
    else:
        window.finish({'status': 'completed', 'stages': []})
    assert all(not button.isEnabled() for card in window.worker_cards.values() for button in card.navigation_buttons.values())
    window._refresh_worker_elapsed()
    assert all(not button.isEnabled() for card in window.worker_cards.values() for button in card.navigation_buttons.values())
    assert not backend.requests


@pytest.mark.parametrize('width', [180, 245, 350])
def test_three_buttons_remain_visible_within_a_narrow_worker_card(app, width):
    card = WorkflowWorkerCard('Fixture app updates')
    card.resize(width, 240)
    card.show()
    app.processEvents()
    try:
        bounds = card.rect()
        for button in card.navigation_buttons.values():
            assert button.isVisible() and button.height() >= 28
            assert bounds.contains(button.geometry())
    finally:
        card.close()
