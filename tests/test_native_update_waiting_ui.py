"""Updater waits replace completed foreground labels without inventing progress."""
import os
import threading

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtWidgets import QApplication, QWidget

from optimus.ui import OptimizationProgressWindow


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def progress(app):
    owner = QWidget()
    value = OptimizationProgressWindow(owner, threading.Event())
    value.show()
    app.processEvents()
    yield value
    value.finish({'status': 'completed', 'stages': []})
    value.close()
    owner.close()
    value.deleteLater()
    owner.deleteLater()
    app.processEvents()


def waiting(**changes):
    state = {'stage': 'Waiting for background application updates',
        'waiting_for': 'software_updates', 'percentage': 29,
        'description': 'Independent work has finished. Application updates must finish before cleanup and verification.',
        'processes': ['Application update worker; package Ollama.Ollama'],
        'jobs': [{'id': 'software_updates', 'status': 'running',
            'description': 'Updating approved package 2/7: Ollama.Ollama (0.40.7 to 0.40.8)',
            'current': 1, 'total': 7, 'elapsed_seconds': 125}]}
    state.update(changes)
    return state


def test_wait_replaces_failed_privacy_stage_but_preserves_its_log(progress):
    progress.update_progress({'stage': 'Apply saved privacy controls', 'percentage': 29,
        'description': 'One or more selected operations or scan checks failed.',
        'log': 'FAILED: Apply saved privacy controls: review the saved item results.'})
    progress.update_progress(waiting())
    assert progress.title.text() == 'WAITING FOR APPLICATION UPDATES'
    assert progress.stage.text() == 'Waiting for background application updates'
    assert 'privacy' not in progress.description.text().lower()
    assert 'cleanup and verification' in progress.description.text()
    assert progress.progress.value() == 29
    assert 'Ollama.Ollama' in progress.background_status.text()
    assert '2/7' in progress.background_status.text()
    assert '1/7 packages processed' in progress.background_status.text()
    assert 'elapsed 2m 05s' in progress.background_status.text()
    assert 'Ollama.Ollama' in progress.processes.toPlainText()
    assert 'FAILED: Apply saved privacy controls' in progress.log.toPlainText()
    assert progress.cancel_button.isEnabled()
    assert progress.cancel_button.text() == 'Cancel after current update'


def test_wait_heartbeat_keeps_overall_stage_count_and_never_claims_complete(progress):
    progress.update_progress(waiting())
    state = waiting(jobs=[{'id': 'software_updates', 'status': 'running',
        'description': 'Installing current package', 'current_package': 'Fixture.App',
        'current': 1, 'total': 7, 'elapsed_seconds': 3661}])
    del state['percentage']
    progress.update_progress(state)
    assert progress.progress.value() == 29
    assert 'current package: Fixture.App' in progress.background_status.text()
    assert 'elapsed 1h 01m 01s' in progress.background_status.text()
    progress.update_progress(waiting(percentage=100))
    assert progress.progress.value() == 99
    assert not progress.completed


def test_wait_cancellation_is_available_and_survives_background_refresh(progress):
    progress.update_progress(waiting())
    progress.cancel_button.click()
    assert progress.cancel.is_set()
    assert not progress.cancel_button.isEnabled()
    assert 'current application update must return' in progress.description.text()
    progress.update_progress(waiting())
    assert 'Cancellation requested.' in progress.description.text()
    assert not progress.cancel_button.isEnabled()


def test_next_foreground_stage_and_results_replace_waiting_title(progress):
    progress.update_progress(waiting())
    progress.update_progress({'stage': 'Clean temporary files', 'percentage': 35,
        'description': 'The updater has returned. Cleaning approved temporary files.', 'jobs': []})
    assert progress.title.text() == 'OPTIMIZATION IN PROGRESS'
    assert progress.stage.text() == 'Clean temporary files'
    assert progress.progress.value() == 35
    assert progress.cancel_button.text() == 'Cancel after current stage'
    assert progress.background_status.isHidden()
    progress.update_progress(waiting())
    progress.finish({'status': 'completed_with_errors', 'stages': []})
    assert progress.title.text() == 'OPTIMIZATION RESULTS'
    assert not progress._waiting_for_updates
    assert progress.cancel_button.text() == 'Close results'


def test_stage_only_wait_event_supports_older_producers_and_uses_clear_explanation(progress):
    state = waiting(description='')
    del state['waiting_for']
    progress.update_progress(state)
    assert progress.title.text() == 'WAITING FOR APPLICATION UPDATES'
    assert 'Independent work has finished.' in progress.description.text()
    assert 'final verification' in progress.description.text()


@pytest.mark.parametrize('elapsed', [None, -1, True, float('nan'), float('inf'), '125'])
def test_malformed_elapsed_metadata_does_not_invent_duration(progress, elapsed):
    state = waiting()
    state['jobs'][0]['elapsed_seconds'] = elapsed
    progress.update_progress(state)
    assert 'elapsed' not in progress.background_status.text()
    assert 'Ollama.Ollama' in progress.background_status.text()


def test_followup_title_is_restored_after_a_wait(progress):
    progress.begin_auto_continue(threading.Event(), automatic=True)
    progress.update_progress(waiting())
    progress.update_progress({'stage': 'Verify available fixes', 'percentage': 90, 'jobs': []})
    assert progress.title.text() == 'APPLYING AVAILABLE FIXES'

