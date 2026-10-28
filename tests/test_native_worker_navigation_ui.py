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
from optimus.navigation import NavigationHub


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


@pytest.mark.parametrize('identity', ('maintenance', 'software_updates', 'driver_updates', 'security_scan'))
def test_next_click_reaches_each_real_local_cursor_and_acknowledges_immediately(app, identity):
    global_stop = threading.Event()
    hub = NavigationHub()
    hub.begin('e' * 32, cancel_event=global_stop)
    tokens = {}
    for key in ('maintenance', 'software_updates', 'driver_updates', 'security_scan'):
        cursor = hub.cursor(key)
        assert cursor.begin_item('fixture:' + key, 'Disposable ' + key, kind='mutation' if key in ('software_updates', 'driver_updates') else 'scan')
        if key in ('software_updates', 'driver_updates'):
            cursor.phase('installing', mutation_started=True)
        tokens[key] = cursor.cancellation()
    owner = QWidget()
    owner.smoke_test = True
    owner.backend = SimpleNamespace(workflow_navigation_status=hub.snapshot,
        request_workflow_navigation=hub.request)
    window = OptimizationProgressWindow(owner, global_stop)
    window._optimus_no_reveal = True
    try:
        window.configure_workers({'spyware_scan_enabled': True})
        window.resize(850, 650)
        window.show()
        for _ in range(3):
            app.processEvents()
        card = window.worker_cards[identity]
        button = card.navigation_buttons['next']
        assert button.isEnabled() and button.isVisibleTo(window)
        QTest.mouseClick(button, Qt.LeftButton)
        # No timer tick, backend refresh loop or GUI event turn is necessary to
        # acknowledge the accepted command in the actual visible worker card.
        assert tokens[identity].is_set()
        assert all(not token.is_set() for key, token in tokens.items() if key != identity)
        assert not global_stop.is_set()
        assert 'Skip requested' in card.status.text()
        assert card.status.visible_text.startswith('Skip requested')
        expected = 'finishing_current_installer' if identity in ('software_updates', 'driver_updates') else 'waiting_for_safe_stop'
        assert card._navigation_state['control_status'] == expected
        assert card._navigation_state['pending_action'] == 'next'
        assert all(not item.isEnabled() for item in card.navigation_buttons.values())
        assert 'Skip requested' in button.toolTip()
        assert 'Skip requested' in window.worker_details.toPlainText()
        assert all(window.worker_cards[key].navigation_buttons['next'].isEnabled()
            for key in tokens if key != identity)
        assert all(cursor.current is not None for cursor in hub.jobs.values())
    finally:
        window.completed = True
        window.close()
        owner.close()
        window.deleteLater()
        owner.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()


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


def test_worker_has_overall_and_current_item_progress_independent_of_cpu(progress):
    window, backend = progress
    window.update_progress({'stage': 'Measured operation', 'percentage': 50,
        'jobs': [{'id': 'software_updates', 'status': 'installing', 'current': 1, 'total': 4,
                  'item_progress': {'key': 'Fixture.App', 'label': 'Fixture App',
                                    'phase': 'download', 'percent': 62}}]})
    card = window.worker_cards['software_updates']
    card.set_live_metrics({'cpu_percent': 2.5, 'scope': 'Thread CPU only'}, {'pressure': 'normal'})
    assert card.completion.text() == 'Overall: 25%'
    assert card.activity_ring.value_text() == '62%'
    assert card.activity_ring.health == 'working'
    card.set_live_metrics({'cpu_percent': 0.0}, {'pressure': 'normal'})
    assert card.activity_ring.value_text() == '62%'
    card.update_state({'status': 'waiting_for_installer', 'current': 1, 'total': 4})
    assert card.activity_ring.value_text() == '—'
    assert card.activity_ring.health == 'waiting'
    assert 'Waiting' in card.activity_state.text()
    card.update_state({'status': 'running'})
    card.set_live_metrics({'cpu_percent': None}, {'pressure': 'high'})
    assert card.activity_ring.health == 'pressure'
    assert card.activity_ring.value_text() == '—'


@pytest.mark.parametrize('identity', ['software_updates', 'driver_updates'])
def test_item_ring_resets_for_each_package_and_phase_and_does_not_fake_terminal_success(progress, identity, monkeypatch):
    # Animation policy belongs to the environment, independently of progress.
    # Pin it here; the reduced-motion behavior is exercised separately below.
    monkeypatch.setattr("optimus.widgets.header_controls.motion_allowed", lambda config=None: True)
    window, backend = progress
    def publish(key, phase, percent, *, current=0, status='installing'):
        window.update_progress({'stage': 'Maintenance continues', 'percentage': 40,
            'jobs': [{'id': identity, 'status': status, 'current': current, 'total': 2,
                'item_progress': {'key': key, 'label': key, 'phase': phase, 'percent': percent}}]})
    card = window.worker_cards[identity]
    publish('Fixture.A', 'download', 100)
    assert card.activity_ring.value_text() == '100%'
    assert card.completion.text() == 'Overall: 0%'
    assert card.activity_ring.phase_caption == 'Download'
    publish('Fixture.A', 'install', None)
    assert card.activity_ring.value_text() == '—' and card.activity_ring.timer.isActive()
    assert card.activity_ring.phase_caption == 'Install'
    publish('Fixture.A', 'install', 77)
    assert card.activity_ring.value_text() == '77%'
    publish('Fixture.B', 'download', 0, current=1)
    assert card.activity_ring.value_text() == '0%'
    assert card.completion.text() == 'Overall: 50%'
    publish('Fixture.B', 'install', None, current=1)
    assert card.activity_ring.value_text() == '—'
    monkeypatch.setattr("optimus.widgets.header_controls.motion_allowed", lambda config=None: False)
    card.activity_ring._sync()
    assert card.activity_ring.value_text() == '—' and not card.activity_ring.timer.isActive()
    monkeypatch.setattr("optimus.widgets.header_controls.motion_allowed", lambda config=None: True)
    publish('Fixture.B', 'install', 99.99, current=1)
    assert card.activity_ring.value_text() == '99%'
    for status in ('failed', 'needs_review', 'cancelled', 'deferred', 'completed_with_errors', 'completed'):
        publish('Fixture.B', 'install', 100, current=2, status=status)
        assert card.activity_ring.value_text() == '—'
        assert not card.activity_ring.timer.isActive()
    # An omitted item is not permission to retain a previous item's percent.
    card.update_state({'status': 'installing', 'current': 0, 'total': 2})
    assert card.activity_ring.value_text() == '—'


def test_foreground_maintenance_and_sequential_updates_route_current_item_percentage(progress):
    window, backend = progress
    item = {'key': 'Fixture.Drive', 'label': 'Fixture drive', 'phase': 'optimize', 'percent': 72}
    window.update_progress({'stage': 'Optimize eligible internal drives', 'percentage': 44,
                           'item_progress': item})
    card = window.worker_cards['maintenance']
    assert card.completion.text() == 'Overall: 44%'
    assert card.activity_ring.value_text() == '72%'
    assert 'Fixture drive' in card.activity_ring.toolTip()
    item = {'key': 'Fixture.App', 'label': 'Fixture App', 'phase': 'install', 'percent': 18}
    window.update_progress({'stage': 'Review or update configured software', 'percentage': 46,
                           'item_progress': item})
    assert window.worker_cards['software_updates'].activity_ring.value_text() == '18%'
    window.update_progress({'stage': 'Capture final resource report', 'percentage': 90})
    assert card.activity_ring.value_text() == '—'




def test_native_byte_counter_estimate_is_labeled_and_does_not_replace_overall(progress):
    window, backend = progress
    window.update_progress({'stage': 'Maintenance continues', 'percentage': 37,
        'jobs': [{'id': 'software_updates', 'status': 'installing', 'current': 1, 'total': 4,
                  'item_progress': {'key': 'Fixture.App', 'label': 'Fixture App',
                                    'phase': 'download', 'percent': 62.5, 'estimated': True}}]})
    card = window.worker_cards['software_updates']
    assert card.activity_ring.value_text() == '~62%'
    assert card.completion.text() == 'Overall: 25%'
    assert 'Approximate' in card.activity_ring.toolTip()
    card.set_live_metrics({'cpu_percent': 99})
    assert card.activity_ring.value_text() == '~62%'


@pytest.mark.parametrize('phase,caption,verb', [('install', 'Install', 'Installing'),
    ('installing', 'Install', 'Installing'), ('uninstall', 'Uninstall', 'Uninstalling'),
    ('uninstalling', 'Uninstall', 'Uninstalling')])
@pytest.mark.parametrize('percent,expected', [(0, '0%'), (49.9, '49%'), (99.99, '99%'), (100, '100%')])
def test_install_and_uninstall_percent_is_visible_and_independent_of_queue(progress, phase, caption, verb, percent, expected):
    window, _ = progress
    window.update_progress({'stage': 'Maintenance continues', 'percentage': 35,
        'jobs': [{'id': 'software_updates', 'status': 'running', 'current': 1, 'total': 4,
            'item_progress': {'key': 'presence:winget:Fixture.App', 'label': 'Fixture App',
                              'phase': phase, 'percent': percent}}]})
    card = window.worker_cards['software_updates']
    assert card.status.text() == verb + ' · ' + expected
    assert card.activity_ring.value_text() == expected
    assert card.activity_ring.phase_caption == caption
    assert card.activity_ring.accessibleDescription().startswith(caption + ': ' + expected)
    assert card.completion.text() == 'Overall: 25%'
    assert card.progress.value() == 1 and card.progress.maximum() == 4
    assert '— ' + verb.lower() in window.background_status.text()
    card.set_live_metrics({'cpu_percent': 90})
    assert card.activity_ring.value_text() == expected


@pytest.mark.parametrize('phase,verb', [('install', 'Installing'), ('uninstall', 'Uninstalling')])
def test_opaque_installer_reports_unknown_and_resets_previous_item_percent(progress, phase, verb):
    window, _ = progress
    card = window.worker_cards['software_updates']
    card.update_state({'status': 'running', 'current': 1, 'total': 2,
        'item_progress': {'key': 'Fixture.A', 'label': 'A', 'phase': 'download', 'percent': 83}})
    card.update_state({'status': 'running', 'current': 1, 'total': 2,
        'item_progress': {'key': 'Fixture.B', 'label': 'B', 'phase': phase, 'percent': None}})
    assert card.status.text() == verb + ' · percentage unavailable'
    assert card.activity_ring.value_text() == '—'
    assert 'indeterminate' in card.activity_ring.toolTip()
    assert card.completion.text() == 'Overall: 50%'
    # A failed/reviewed operation does not borrow a reported or queue-based 100%.
    card.update_state({'status': 'failed', 'current': 2, 'total': 2,
        'item_progress': {'key': 'Fixture.B', 'label': 'B', 'phase': phase, 'percent': 100}})
    assert card.status.text() == 'Failed'
    assert card.activity_ring.value_text() == '—'


def test_foreground_debloat_uses_uninstall_item_progress_not_workflow_percentage(progress):
    window, _ = progress
    window.update_progress({'stage': 'Remove selected consumer app group', 'percentage': 60,
        'item_progress': {'key': 'debloat:remove-bloat:fixture', 'label': 'Fixture consumer app',
                          'phase': 'uninstall', 'percent': 42}})
    card = window.worker_cards['maintenance']
    assert card.status.text() == 'Uninstalling · 42%'
    assert card.completion.text() == 'Overall: 60%'
    assert card.activity_ring.value_text() == '42%'
    assert card.activity_ring.phase_caption == 'Uninstall'
    assert 'Fixture consumer app' in card.activity_ring.toolTip()


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
    ring.set_progress(None, 'working', active=True)
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
    ring.set_progress(None, 'waiting', active=True)
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


def test_live_privacy_counts_and_driver_discovery_reach_item_rings(progress):
    window, _ = progress
    window.update_progress({'stage': 'Apply saved privacy controls', 'percentage': 40,
        'item_progress': {'key': 'privacy:controls', 'label': 'Privacy controls: 4/8 checked',
                          'phase': 'verify', 'percent': 50},
        'jobs': [{'id': 'driver_updates', 'status': 'scanning', 'current': 0, 'total': 0,
                  'item_progress': {'key': 'driver:discovery', 'label': 'Device inventory',
                                    'phase': 'device_records', 'percent': 62}}]})
    maintenance = window.worker_cards['maintenance']
    driver = window.worker_cards['driver_updates']
    assert maintenance.completion.text() == 'Overall: 40%'
    assert maintenance.activity_ring.value_text() == '50%'
    assert maintenance.activity_ring.phase_caption == 'Verify'
    assert driver.activity_ring.value_text() == '62%'
    assert driver.activity_ring.phase_caption == 'Devices'
    assert driver.completion.text() != 'Overall: 62%'
    window.update_progress({'stage': 'Apply saved privacy controls', 'percentage': 40,
        'jobs': [{'id': 'driver_updates', 'status': 'scanning', 'current': 0, 'total': 0}]})
    assert driver.activity_ring.value_percent is None
    assert driver.activity_ring.phase_caption == 'Scan'


def test_blocked_search_counts_are_not_shown_as_install_completion(progress):
    window, _ = progress
    window.update_progress({'stage': 'Measured operation', 'percentage': 40,
        'jobs': [{'id': 'software_updates', 'status': 'needs_review',
                  'current': 1, 'total': 3, 'installation_blocked': True,
                  'blocked_by_prior_installer': True, 'scan_complete': False,
                  'description': 'Previous installer needs review.'}]})
    card = window.worker_cards['software_updates']
    assert card.completion.text() == 'Overall: blocked'
    assert card.activity_ring.value_percent is None
    assert not card.activity_ring.timer.isActive()


def test_failed_maintenance_item_shows_error_then_next_item_runs(progress):
    window, _ = progress
    window.update_progress({'stage': 'Fixture refused step', 'stage_status': 'failed',
                            'percentage': 40, 'description': 'Known fixture failure.'})
    card = window.worker_cards['maintenance']
    assert card.activity_ring.health == 'error'
    assert card.activity_ring.value_percent is None
    window.update_progress({'stage': 'Next safe fixture step', 'stage_status': 'running',
        'percentage': 41, 'item_progress': {'key': 'next:fixture', 'label': 'Next checks',
                                           'phase': 'verify', 'percent': 25}})
    assert card.activity_ring.value_text() == '25%'
    assert card.status.text() == 'Running'
    assert not window.cancel.is_set()


def test_long_current_step_description_fits_and_keeps_full_accessible_text(progress):
    window, backend = progress
    text = 'Privacy control checks completed; unsupported settings stay visible. ' * 8
    window.update_progress({'stage': 'Apply saved privacy controls', 'percentage': 40,
        'description': text, 'item_progress': {'key': 'privacy:controls', 'label': 'Privacy controls',
                                              'phase': 'verify', 'percent': 33}})
    for width in (850, 520):
        window.resize(width, 650)
        from PySide6.QtWidgets import QApplication
        QApplication.processEvents()
        label = window.description
        assert label.text() == text
        assert label.toolTip() == text
        assert len(label.visible_text.splitlines()) == 1
        assert label.height() >= label.fontMetrics().lineSpacing()
        assert all(label.fontMetrics().horizontalAdvance(line) <= label.contentsRect().width()
                   for line in label.visible_text.splitlines())
        assert window.worker_cards['maintenance'].details.text() == text
