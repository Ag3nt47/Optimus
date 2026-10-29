"""Injected updater/Qt fixtures only; never network, replace or launch an app."""
from __future__ import annotations

import copy
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QThread
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel

from optimus.launch_review import LaunchConsent, launch_warning
from optimus.native_ui import NativeOptimusWindow
from optimus.self_update_ui import StartupUpdateController, SelfUpdateProgressWindow, make_startup_update_dialog, make_manual_update_dialog
from optimus.ui import OptimusWindow
from optimus.workflow import DEFAULTS


def wait_for(app, predicate):
    deadline = time.monotonic() + 5
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        QTest.qWait(5)
    app.processEvents()
    assert predicate()


@pytest.fixture
def owner(tmp_path):
    app = QApplication.instance() or QApplication([])
    settings = dict(copy.deepcopy(DEFAULTS), setup_version=2, cleanup_enabled=True,
                    browser_cookies_enabled=True, consumer_debloat_enabled=True,
                    consumer_debloat_package_names=['Fixture.Consumer'])
    backend = SimpleNamespace(data_dir=tmp_path, preferences=lambda: copy.deepcopy(settings),
                              optimize=Mock(), close=Mock())
    window = OptimusWindow(backend, smoke_test=True)
    window.smoke_test = False
    window._launch_consent = LaunchConsent(required=True)
    window.start_optimization = Mock()
    window.review_launch = Mock()
    window.quit_application = Mock(return_value=True)
    window._session_approved_callback = Mock()
    window.show()
    yield app, window, backend, settings
    controller = window._self_update_controller
    if controller is not None:
        controller.cancel.set()
        for event in getattr(controller, '_fixture_release', []):
            event.set()
        controller.shutdown()
    window._settings_window = window._sandbox_window = window._setup_wizard = None
    window._quitting = True
    window.shutdown()
    window.close()
    window.deleteLater()
    app.processEvents()


def updater(**values):
    result = SimpleNamespace(check=Mock(return_value={'status': 'available',
        'automatic_install_supported': True, 'offer': {'version': '0.12.0',
        'release_url': 'https://github.com/Ag3nt47/Optimus/releases/tag/v0.12.0'}}),
        stage=Mock(return_value={'status': 'staged', 'stage_id': 'a' * 32}),
        prepare_handoff=Mock(return_value={'status': 'ready', 'handoff_id': 'b' * 32}),
        dispatch_worker=Mock(return_value={'status': 'dispatched', 'started': True, 'pid': 123}),
        cancel_handoff=Mock(return_value={'status': 'cancelled', 'revoked': True, 'cleanup_approved': False}),
        consume_resume=Mock(return_value={'status': 'unavailable'}),
        acknowledge_resume=Mock(return_value={'status': 'ready'}))
    result.__dict__.update(values)
    return result


def controller(owner, service=None, **kwargs):
    app, window, backend, settings = owner
    service = service or updater()
    value = StartupUpdateController(window, backend, updater=service,
        continue_startup=window.review_launch, **kwargs)
    window._self_update_controller = value
    return value, service


@pytest.mark.parametrize('check', [
    {'status': 'up_to_date'}, {'status': 'unavailable', 'reason': 'Offline fixture'},
    {'status': 'unavailable', 'reason': 'Unsupported metadata'},
])
def test_no_update_or_offline_continues_normal_review_without_approval_or_staging(owner, check):
    app, window, backend, settings = owner
    value, service = controller(owner, updater(check=Mock(return_value=check)), ask=Mock())
    value.start()
    assert not window.session_approved() and all(not button.isEnabled() for button in window.main_buttons)
    wait_for(app, lambda: window.review_launch.called)
    value.ask.assert_not_called()
    service.stage.assert_not_called()
    backend.optimize.assert_not_called()
    assert not window.session_approved() and not value.pending


def test_declining_or_adjusting_update_never_stages_and_keeps_saved_scope_untouched(owner):
    app, window, backend, settings = owner
    saved = copy.deepcopy(settings)
    gui_thread = QThread.currentThread()
    def choose(parent, offer, snapshot):
        assert QThread.currentThread() == gui_thread
        assert snapshot == saved and not parent.session_approved()
        return 'no_update'
    value, service = controller(owner, ask=choose)
    value.start()
    wait_for(app, lambda: window.review_launch.called)
    service.stage.assert_not_called()
    assert settings == saved and not window.session_approved()


def test_adjust_choice_opens_settings_paused_without_starting_normal_review(owner):
    app, window, backend, settings = owner
    window.open_settings = Mock()
    value, service = controller(owner, ask=lambda *_: 'adjust_settings')
    value.start()
    wait_for(app, lambda: window.open_settings.called)
    window.open_settings.assert_called_once_with(page='Settings')
    window.review_launch.assert_not_called()
    service.stage.assert_not_called()
    assert not window.session_approved()


@pytest.mark.parametrize('choice', [None, 'unrecognized_choice'])
def test_closing_update_scope_warning_leaves_maintenance_paused(owner, choice):
    app, window, backend, settings = owner
    value, service = controller(owner, ask=lambda *_: choice)
    value.start()
    wait_for(app, lambda: not value.pending)
    assert not window.session_approved()
    window.review_launch.assert_not_called()
    window.start_optimization.assert_not_called()
    service.stage.assert_not_called()


def test_available_unsupported_native_release_is_explicit_manual_notice(owner):
    app, window, backend, settings = owner
    result = {'status': 'available', 'automatic_install_supported': False,
        'offer': {'version': '0.12.0', 'release_url': 'https://github.com/Ag3nt47/Optimus/releases/tag/v0.12.0'},
        'reason': 'Use native platform download; automatic replacement is unsupported.'}
    notice = Mock()
    value, service = controller(owner, updater(check=Mock(return_value=result)), manual_notice=notice, ask=Mock())
    value.start()
    wait_for(app, lambda: window.review_launch.called)
    notice.assert_called_once_with(window, result)
    value.ask.assert_not_called()
    service.stage.assert_not_called()


def test_yes_stages_then_handoffs_before_any_cleanup_or_session_approval(owner):
    app, window, backend, settings = owner
    gui_thread = QThread.currentThread()
    calls = []
    service = updater()
    def stage(offer, **options):
        assert QThread.currentThread() != gui_thread
        assert options['confirmed'] is True
        assert not window.start_optimization.called
        calls.append('stage')
        options['progress_callback']({'phase': 'download', 'received': 50, 'total': 100})
        return {'status': 'staged', 'stage_id': 'a' * 32}
    service.stage.side_effect = stage
    service.prepare_handoff.side_effect = lambda *args, **kwargs: (calls.append('prepare') or {'status': 'ready', 'handoff_id': 'b' * 32})
    service.dispatch_worker.side_effect = lambda identity: (calls.append('dispatch') or {'status': 'dispatched', 'started': True})
    value, _ = controller(owner, service, ask=lambda *_: 'yes_update')
    value.start()
    wait_for(app, lambda: window.quit_application.called)
    assert calls == ['stage', 'prepare', 'dispatch']
    service.prepare_handoff.assert_called_once_with('a' * 32, cleanup_approved=True, approved_settings=settings)
    service.dispatch_worker.assert_called_once_with('b' * 32)
    window.start_optimization.assert_not_called()
    window._session_approved_callback.assert_not_called()
    backend.optimize.assert_not_called()
    assert not window.session_approved()


def test_verified_dispatch_uses_real_quit_and_shutdown_without_another_prompt(owner, monkeypatch):
    """Exercise the actual close path, not just the controller's mocked callback."""
    app, window, backend, settings = owner
    quit_app = Mock()
    monkeypatch.setattr(app, 'quit', quit_app)
    window.quit_application = lambda: OptimusWindow.quit_application(window)
    value, service = controller(owner, ask=lambda *_: 'yes_update')
    value.start()
    wait_for(app, lambda: window._quitting and value._closed)
    quit_app.assert_called_once_with()
    assert window._presentation_shutdown is True
    assert value.pool.activeThreadCount() == 0
    assert value._handoff_committed and not value.pending
    service.dispatch_worker.assert_called_once_with('b' * 32)
    service.cancel_handoff.assert_not_called()
    window.review_launch.assert_not_called()
    window.start_optimization.assert_not_called()
    backend.optimize.assert_not_called()
    assert value._progress.stage.text() == 'Restarting Optimus automatically'
    assert not value._progress.cancel_button.isEnabled()


def test_dispatch_progress_commits_to_restart_then_restores_controls_if_close_declined(owner):
    app, window, backend, settings = owner
    window.quit_application.return_value = False
    value, service = controller(owner, ask=lambda *_: 'yes_update')
    value.start()
    wait_for(app, lambda: window.review_launch.called)
    assert value._revoked and not value._handoff_committed
    assert value._progress.stage.text() == 'Continuing the current version'
    assert value._progress.cancel_button.text() == 'Close update results'
    assert value._progress.cancel_button.isEnabled()
    service.cancel_handoff.assert_called_once_with('b' * 32)
    backend.optimize.assert_not_called()


@pytest.mark.parametrize('change', ['incomplete', 'changed'])
def test_incomplete_or_changed_settings_do_not_gain_restart_cleanup_approval(owner, change):
    app, window, backend, settings = owner
    if change == 'incomplete':
        settings['setup_version'] = 0
    service = updater()
    def stage(*args, **kwargs):
        if change == 'changed':
            settings['cleanup_age_days'] += 1
        return {'status': 'staged', 'stage_id': 'a' * 32}
    service.stage.side_effect = stage
    value, _ = controller(owner, service, ask=lambda *_: 'yes_update')
    value.start()
    wait_for(app, lambda: window.quit_application.called)
    service.prepare_handoff.assert_called_once_with('a' * 32, cleanup_approved=False, approved_settings=None)
    window.start_optimization.assert_not_called()


def test_unsaved_sandbox_cancel_prevents_dispatch_and_old_window_closure(owner):
    app, window, backend, settings = owner
    sandbox = SimpleNamespace(close=Mock(return_value=False), _optimus_close_bypass=False)
    window._sandbox_window = sandbox
    value, service = controller(owner, ask=lambda *_: 'yes_update')
    value.start()
    wait_for(app, lambda: window.review_launch.called)
    sandbox.close.assert_called_once()
    assert sandbox._optimus_close_bypass is False
    assert window._sandbox_window is sandbox
    service.prepare_handoff.assert_not_called()
    service.dispatch_worker.assert_not_called()
    window.quit_application.assert_not_called()


def test_normal_close_guard_cancel_after_dispatch_revokes_before_returning_to_review(owner):
    app, window, backend, settings = owner
    window.quit_application.return_value = False
    value, service = controller(owner, ask=lambda *_: 'yes_update')
    value.start()
    wait_for(app, lambda: window.review_launch.called)
    service.dispatch_worker.assert_called_once()
    service.cancel_handoff.assert_called_once_with('b' * 32)
    assert value._revoked and not value.pending and not value._handoff_committed
    assert all(button.isEnabled() for button in window.main_buttons)
    window.start_optimization.assert_not_called()


@pytest.mark.parametrize('phase', ['check', 'stage', 'prepare'])
def test_known_pre_handoff_failure_falls_forward_to_normal_review(owner, phase):
    app, window, backend, settings = owner
    service = updater()
    getattr(service, phase if phase != 'prepare' else 'prepare_handoff').side_effect = ValueError('Fixture ' + phase + ' refusal')
    value, _ = controller(owner, service, ask=lambda *_: 'yes_update')
    value.start()
    wait_for(app, lambda: window.review_launch.called)
    window.start_optimization.assert_not_called()
    window.quit_application.assert_not_called()
    service.dispatch_worker.assert_not_called()


@pytest.mark.parametrize('dispatch', [{'status': 'uncertain'}, {'status': 'wrong', 'started': True}])
def test_uncertain_dispatch_is_revoked_before_continuing_old_version_without_cleanup(owner, dispatch):
    app, window, backend, settings = owner
    value, service = controller(owner, updater(dispatch_worker=Mock(return_value=dispatch)), ask=lambda *_: 'yes_update')
    value.start()
    wait_for(app, lambda: window.review_launch.called)
    service.cancel_handoff.assert_called_once_with('b' * 32)
    assert not value.pending and not window.session_approved()
    window.start_optimization.assert_not_called()
    window.quit_application.assert_not_called()


def test_failed_revocation_keeps_maintenance_and_auto_exit_paused(owner):
    app, window, backend, settings = owner
    service = updater(dispatch_worker=Mock(return_value={'status': 'uncertain'}),
                      cancel_handoff=Mock(return_value={'status': 'needs_review', 'revoked': False}))
    value, _ = controller(owner, service, ask=lambda *_: 'yes_update')
    value.start()
    wait_for(app, lambda: 'could not be revoked' in window.status.text())
    assert value.pending and not window.session_approved()
    window.review_launch.assert_not_called()
    window.quit_application.assert_not_called()
    window.start_optimization.assert_not_called()


@pytest.mark.parametrize('phase', ['prepare', 'dispatch'])
@pytest.mark.parametrize('close', [False, True], ids=['cancel-update', 'close-app'])
def test_cancel_racing_handoff_waits_for_durable_revocation_before_review_or_exit(owner, tmp_path, phase, close):
    app, window, backend, settings = owner
    active, release = threading.Event(), threading.Event()
    receipt = tmp_path / 'fixture-revoked.json'
    service = updater()
    def held(*args, **kwargs):
        active.set()
        assert release.wait(4)
        return {'status': 'ready', 'handoff_id': 'b' * 32} if phase == 'prepare' else {'status': 'dispatched', 'started': True}
    getattr(service, 'prepare_handoff' if phase == 'prepare' else 'dispatch_worker').side_effect = held
    def revoke(identity):
        window.quit_application.assert_not_called()
        window.review_launch.assert_not_called()
        receipt.write_text('{"revoked":true,"cleanup_approved":false}')
        return {'status': 'cancelled', 'revoked': True, 'cleanup_approved': False}
    service.cancel_handoff.side_effect = revoke
    value, _ = controller(owner, service, ask=lambda *_: 'yes_update')
    value._fixture_release = [release]
    value.start()
    wait_for(app, active.is_set)
    if close:
        assert window.request_safe_stop()
    else:
        value._progress.request_close()
    window.quit_application.assert_not_called()
    window.review_launch.assert_not_called()
    assert not receipt.exists()
    release.set()
    wait_for(app, lambda: window.quit_application.called if close else window.review_launch.called)
    assert receipt.exists() and value._revoked
    window.start_optimization.assert_not_called()
    backend.optimize.assert_not_called()


def test_safe_close_waits_for_owned_download_then_does_not_dispatch_or_auto_clean(owner, tmp_path):
    app, window, backend, settings = owner
    active, release = threading.Event(), threading.Event()
    receipt = tmp_path / 'fixture-download-settled.json'
    def stage(*args, **kwargs):
        active.set()
        assert release.wait(4)
        assert kwargs['cancel_event'].is_set()
        receipt.write_text('{"status":"cancelled","installer_started":false}')
        return {'status': 'cancelled'}
    service = updater(stage=Mock(side_effect=stage))
    value, _ = controller(owner, service, ask=lambda *_: 'yes_update')
    value._fixture_release = [release]
    value.start()
    wait_for(app, active.is_set)
    assert window.request_safe_stop()
    assert value.cancel.is_set() and value.pending
    window.quit_application.assert_not_called()
    assert not receipt.exists()
    release.set()
    wait_for(app, lambda: window.quit_application.called)
    assert receipt.exists()
    service.dispatch_worker.assert_not_called()
    window.review_launch.assert_not_called()
    window.start_optimization.assert_not_called()


def test_valid_core_one_shot_resume_grants_current_launch_consent_then_normal_start(owner):
    app, window, backend, settings = owner
    service = updater(consume_resume=Mock(return_value={'status': 'accepted', 'cleanup_approved': True, 'settings': copy.deepcopy(settings)}))
    value, _ = controller(owner, service, resume_id='b' * 32)
    value.start()
    wait_for(app, lambda: window.start_optimization.called)
    service.consume_resume.assert_called_once_with('b' * 32)
    service.acknowledge_resume.assert_called_once_with('b' * 32)
    service.check.assert_not_called()
    assert window.session_approved() and not value.pending
    window.start_optimization.assert_called_once_with()
    window._session_approved_callback.assert_called_once_with()
    window.review_launch.assert_not_called()


def test_resume_waits_for_visible_gui_readiness_ack_before_maintenance(owner):
    app, window, backend, settings = owner
    active, release = threading.Event(), threading.Event()
    gui_thread = QThread.currentThread()

    def acknowledge(identity):
        assert identity == 'b' * 32
        assert QThread.currentThread() != gui_thread
        active.set()
        assert release.wait(5)
        return {'status': 'ready'}

    service = updater(consume_resume=Mock(return_value={'status': 'accepted',
        'cleanup_approved': True, 'settings': copy.deepcopy(settings)}),
        acknowledge_resume=Mock(side_effect=acknowledge))
    value, _ = controller(owner, service, resume_id='b' * 32)
    value._fixture_release = [release]
    value.start()
    wait_for(app, active.is_set)
    assert window.isVisible() and value.pending
    assert not window.session_approved()
    assert all(not button.isEnabled() for button in window.main_buttons)
    window.start_optimization.assert_not_called()
    window.review_launch.assert_not_called()
    backend.optimize.assert_not_called()
    release.set()
    wait_for(app, lambda: window.start_optimization.called)
    assert window.session_approved() and not value.pending


@pytest.mark.parametrize('acknowledgement', [None, {'status': 'unavailable'}, {'status': 'started'}])
def test_unconfirmed_resume_readiness_returns_to_review_without_running_cleanup(owner, acknowledgement):
    app, window, backend, settings = owner
    service = updater(consume_resume=Mock(return_value={'status': 'accepted',
        'cleanup_approved': True, 'settings': copy.deepcopy(settings)}),
        acknowledge_resume=Mock(return_value=acknowledgement))
    value, _ = controller(owner, service, resume_id='b' * 32)
    value.start()
    wait_for(app, lambda: window.review_launch.called)
    assert not window.session_approved() and not value.pending
    window.start_optimization.assert_not_called()
    backend.optimize.assert_not_called()


def test_hidden_updated_window_does_not_acknowledge_or_start_cleanup(owner):
    app, window, backend, settings = owner
    window.hide()
    service = updater(consume_resume=Mock(return_value={'status': 'accepted',
        'cleanup_approved': True, 'settings': copy.deepcopy(settings)}))
    value, _ = controller(owner, service, resume_id='b' * 32)
    value.start()
    wait_for(app, lambda: window.review_launch.called)
    service.acknowledge_resume.assert_not_called()
    window.start_optimization.assert_not_called()
    backend.optimize.assert_not_called()


def test_settings_changed_during_readiness_ack_do_not_gain_maintenance_consent(owner):
    app, window, backend, settings = owner

    def acknowledge(identity):
        settings['cleanup_age_days'] += 1
        return {'status': 'ready'}

    service = updater(consume_resume=Mock(return_value={'status': 'accepted',
        'cleanup_approved': True, 'settings': copy.deepcopy(settings)}),
        acknowledge_resume=Mock(side_effect=acknowledge))
    value, _ = controller(owner, service, resume_id='b' * 32)
    value.start()
    wait_for(app, lambda: window.review_launch.called)
    assert not window.session_approved()
    window.start_optimization.assert_not_called()
    backend.optimize.assert_not_called()


def test_valid_resume_surfaces_manual_launcher_items_without_inventing_a_fix(owner):
    app, window, backend, settings = owner
    service = updater(consume_resume=Mock(return_value={'status': 'accepted', 'cleanup_approved': True,
        'settings': copy.deepcopy(settings), 'launchers': {'manual_action_required': True,
            'manual_items': [{'kind': 'shortcut', 'reason': 'An externally edited pin was preserved.'}]}}))
    value, _ = controller(owner, service, resume_id='b' * 32)
    value.start()
    wait_for(app, lambda: window.start_optimization.called)
    assert 'manual review' in window._self_update_notice
    assert 'externally edited pin was preserved' in window._self_update_notice
    assert window.session_approved()


@pytest.mark.parametrize('kind', ['already_consumed', 'changed', 'not_approved'])
def test_rejected_changed_or_unapproved_resume_requires_normal_review(owner, kind):
    app, window, backend, settings = owner
    accepted = {'status': 'accepted', 'cleanup_approved': True, 'settings': copy.deepcopy(settings)}
    if kind == 'already_consumed':
        accepted = {'status': 'unavailable'}
    elif kind == 'changed':
        accepted['settings']['cleanup_age_days'] += 1
    else:
        accepted['cleanup_approved'] = False
    value, service = controller(owner, updater(consume_resume=Mock(return_value=accepted)), resume_id='b' * 32)
    value.start()
    wait_for(app, lambda: window.review_launch.called)
    assert not window.session_approved()
    window.start_optimization.assert_not_called()
    window._session_approved_callback.assert_not_called()


def test_resumed_incomplete_setup_opens_questions_before_fresh_review(owner):
    app, window, backend, settings = owner
    settings['setup_version'] = 0
    events = []
    window.open_initial_setup = Mock(side_effect=lambda **kwargs: events.append('setup'))
    window.review_launch.side_effect = lambda: events.append('review')
    service = updater(consume_resume=Mock(return_value={'status': 'accepted', 'cleanup_approved': False, 'settings': copy.deepcopy(settings)}))
    value, _ = controller(owner, service, resume_id='b' * 32)
    value.start()
    wait_for(app, lambda: window.review_launch.called)
    assert events == ['setup', 'review'] and not window.session_approved()
    window.open_initial_setup.assert_called_once_with(first_run=True)
    window.start_optimization.assert_not_called()


@pytest.mark.parametrize('size', [(850, 650), (1280, 820)])
def test_prompt_and_update_progress_have_labeled_fitting_controls_and_no_maintenance_claim(owner, size):
    app, window, backend, settings = owner
    offer = {'version': '0.12.0+' + 'longrelease' * 12}
    dialog = make_startup_update_dialog(window, offer, settings)
    dialog.resize(*size)
    dialog.show()
    QTest.qWait(40)
    app.processEvents()
    assert dialog.update_choice is None and not window.session_approved()
    assert dialog.saved_actions.toPlainText() == launch_warning(settings)
    assert dialog.update_buttons['yes'].text().startswith('Yes')
    assert dialog.update_buttons['no'].text().startswith('No')
    assert dialog.update_buttons['adjust'].isDefault()
    assert not dialog.update_buttons['yes'].isDefault()
    for button in dialog.update_buttons.values():
        assert button.accessibleName()
        assert button.width() >= button.fontMetrics().horizontalAdvance(button.text()) + 12
        assert button.height() >= button.fontMetrics().lineSpacing() + 8
        assert dialog.rect().contains(button.geometry())
    assert 'permanently' in dialog.saved_actions.toPlainText().casefold()
    dialog.update_buttons['no'].click()
    assert dialog.update_choice == 'no_update'
    progress = SelfUpdateProgressWindow(window, threading.Event())
    progress.resize(*size)
    progress.show()
    progress.update_phase({'phase': 'Download verified release', 'description': 'Long description of verified release staging. ' * 20,
                           'received': 50, 'total': 100})
    app.processEvents()
    assert progress.progress.value() == 50
    assert '50%' in progress.spinner._label.text()
    progress.update_phase({'phase': 'Extract release files', 'current': 2, 'total': 4})
    assert progress.progress.value() == 50 and '50%' in progress.spinner._label.text()
    progress.update_phase({'phase': 'Validate restart receipt'})
    assert progress.progress.maximum() == 0 and '%' not in progress.spinner._label.text()
    assert 'UPDATING OPTIMUS' == progress.title.text()
    assert progress.worker_grid.isHidden() and progress.live_system_status.isHidden()
    assert 'no maintenance' in progress.processes.toPlainText()
    assert not progress.verification_summary.isVisible()
    progress.settled('Fixture download checked. Ready to restart.', staged=True)
    assert progress.progress.format() == 'Download verified'
    progress.close()
    progress.deleteLater()
    dialog.deleteLater()


@pytest.mark.parametrize('url,linked', [('https://github.com/Ag3nt47/Optimus/releases/tag/v0.12.0', True),
    ('https://untrusted.example/update.exe', False), ('javascript:alert(1)', False)])
def test_manual_update_notice_is_scrollable_and_links_only_verified_github_release(owner, url, linked):
    app, window, backend, settings = owner
    dialog = make_manual_update_dialog(window, {'offer': {'version': '0.12.0', 'release_url': url},
        'reason': 'A reviewed native installer is required. ' * 200})
    dialog.resize(850, 650)
    dialog.show()
    app.processEvents()
    links = [label for label in dialog.findChildren(QLabel) if label.openExternalLinks()]
    assert bool(links) is linked
    assert dialog.height() <= 650
    assert not window.session_approved()
    dialog.close()
    dialog.deleteLater()


@pytest.mark.parametrize('window_class', [OptimusWindow, NativeOptimusWindow])
def test_pending_startup_blocks_settings_setup_sandbox_revert_and_normal_work(tmp_path, window_class):
    app = QApplication.instance() or QApplication([])
    backend = SimpleNamespace(data_dir=tmp_path, preferences=lambda: dict(copy.deepcopy(DEFAULTS), setup_version=2), optimize=Mock())
    window = window_class(backend, smoke_test=True)
    window.smoke_test = False
    window._startup_update_pending = True
    window._launch_consent.approve(backend.preferences())
    try:
        assert not window.session_approved()
        assert window.open_initial_setup() is False
        window.open_settings()
        window.open_sandbox()
        window.open_revert()
        window.start_optimization()
        assert not window.review_launch()
        assert window._settings_window is None and window._sandbox_window is None and window._setup_wizard is None
        backend.optimize.assert_not_called()
    finally:
        window._quitting = True
        window.shutdown()
        window.close()
        window.deleteLater()
        app.processEvents()
