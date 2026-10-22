"""The review bot detects real fixture layout defects and never runs a host backend."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
from unittest.mock import Mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QPushButton, QScrollArea, QWidget


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('optimus_critical_review_tool', ROOT / 'tools/critical_review.py')
bot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bot)


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def reviewer(tmp_path, app):
    original = app.quitOnLastWindowClosed()
    value = bot.CriticalReview(tmp_path / 'review')
    yield value
    value.close()
    assert app.quitOnLastWindowClosed() == original


def show(window, app):
    window.setAttribute(Qt.WA_DontShowOnScreen, True)
    window.show()
    app.processEvents()


@pytest.mark.parametrize('label,x,width', (('', 20, 180), ('Apply', 280, 120),
                                         ('A deliberately long primary action', 20, 35)))
def test_measurement_rejects_blank_clipped_or_truncated_required_actions(reviewer, app, label, x, width):
    window = QWidget()
    window.resize(320, 180)
    button = QPushButton(label, window)
    button.setGeometry(x, 30, width, 40)
    show(window, app)
    try:
        measured = bot.measure_button(button, window, (320, 180), app)
        assert measured['status'] == 'failed'
        assert measured['geometry']['width'] == width
        assert 'unreadable/blank' in measured['reason']
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()


def test_scrollable_content_is_measured_after_actual_reveal(reviewer, app):
    window = QWidget()
    window.resize(320, 220)
    area = QScrollArea(window)
    area.setGeometry(0, 0, 320, 220)
    content = QWidget()
    content.resize(280, 1400)
    button = QPushButton('Reach this action', content)
    button.setGeometry(20, 1200, 220, 40)
    area.setWidget(content)
    show(window, app)
    try:
        assert area.verticalScrollBar().value() == 0
        measured = bot.measure_button(button, window, (320, 220), app)
        assert measured['status'] == 'passed'
        assert measured['scrolls'][0]['vertical'] > 0
        assert measured['visible_region']['height'] >= 38
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()


def test_horizontal_scroll_does_not_fake_a_fully_reachable_oversized_button(reviewer, app):
    window = QWidget()
    window.resize(320, 220)
    area = QScrollArea(window)
    area.setGeometry(0, 0, 320, 220)
    content = QWidget()
    content.resize(900, 180)
    button = QPushButton('Oversized action', content)
    button.setGeometry(10, 30, 850, 40)
    area.setWidget(content)
    show(window, app)
    try:
        measured = bot.measure_button(button, window, (320, 220), app)
        assert measured['status'] == 'failed'
        assert measured['scrolls'][0]['horizontal_max'] > 0
        assert measured['visible_region']['width'] < measured['geometry']['width']
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()


def test_oversized_minimum_is_a_recorded_critical_failure(reviewer, app):
    window = QWidget()
    window.setMinimumSize(1100, 720)
    try:
        reviewer.viewport(window, (850, 650), 'oversized')
        finding = reviewer.report['checks'][-1]
        assert finding['status'] == 'failed' and finding['severity'] == 'critical'
        assert finding['evidence']['actual'] == [1100, 720]
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()


def test_end_to_end_fixture_review_covers_flow_and_exports_honest_gaps(reviewer, monkeypatch):
    from optimus import backend, native_backend
    forbidden = Mock(side_effect=AssertionError('No host Backend may be constructed by the review bot.'))
    monkeypatch.setattr(backend, 'Backend', forbidden)
    monkeypatch.setattr(native_backend, 'NativeBackend', forbidden)
    report = reviewer.run()
    forbidden.assert_not_called()
    assert report['host_maintenance_applied'] is False and report['coverage_complete'] is False
    assert report['status'] in ('failed', 'passed_with_manual_review')
    by_id = {item['id']: item for item in report['checks']}
    for dimensions in bot.VIEWPORTS:
        prefix = f'{dimensions[0]}x{dimensions[1]}'
        for suffix in ('main.three-actions', 'setup.no-implicit-save', 'speed.no-implicit-actions',
            'speed.unsaved-approval', 'speed.single-worker', 'speed.close-guard',
            'speed.cancel-completes', 'speed.deferred-honesty', 'workflow.single-initial-worker',
            'workflow.explicit-auto-continue', 'workflow.unverified-honesty',
            'workflow.single-follow-up-worker', 'workflow.follow-up-deferred',
            'workflow.stale-request', 'workflow.manual-continue',
            'updates-waiting.explicit-state', 'updates-waiting.current-worker',
            'updates-waiting.progress-honesty', 'updates-waiting.cancel-available',
            'updates-waiting.failure-history', 'updates-waiting.cancellation-stable',
            'updates-waiting.foreground-resumes'):
            assert by_id[prefix + '.' + suffix]['status'] == 'passed'
    assert by_id['confirmation.negative-default']['status'] == 'passed'
    assert report['screenshots'] and all((reviewer.output / item['file']).is_file() for item in report['screenshots'])
    assert {item['name'] for item in report['screenshots']} >= {
        f'{width}x{height}-updates-waiting' for width, height in bot.VIEWPORTS}
    assert {item['area'] for item in report['manual_review']} >= {'GPU', 'CPU and RAM', 'Updates and security'}
    assert all(item['status'] == 'not_exercised' for item in report['skipped_checks'])
    assert {item['source'] for item in report['documentation_comparison']} == {'docs/FEATURES.md', 'docs/PLATFORMS.md'}
    assert all(item['status'] == 'manual_comparison_required' for item in report['documentation_comparison'])
    markdown = bot.markdown_report(report)
    summary = bot.github_summary(report)
    assert 'not AI review' in markdown and 'No host maintenance was applied' in summary
    assert 'critical-review.json' in summary and 'Manual review remains required' in summary


@pytest.mark.parametrize(('defect', 'expected_failure'), [
    ('stale-stage', 'explicit-state'), ('missing-package', 'current-worker'),
    ('false-completion', 'progress-honesty'), ('disabled-cancel', 'cancel-available')])
def test_waiting_review_detects_the_reintroduced_regression(reviewer, app, monkeypatch,
                                                           defect, expected_failure):
    original = bot.OptimizationProgressWindow.update_progress
    def broken_update(progress, state):
        original(progress, state)
        if state.get('waiting_for') != 'software_updates':
            return
        if defect == 'stale-stage':
            progress.title.setText('OPTIMIZATION IN PROGRESS')
            progress.stage.setText('Apply saved privacy controls')
            progress.description.setText('Privacy controls failed.')
        elif defect == 'missing-package':
            progress.background_status.clear()
            progress.processes.clear()
        elif defect == 'false-completion':
            progress.progress.setValue(100)
        elif defect == 'disabled-cancel':
            progress.cancel_button.setEnabled(False)
    monkeypatch.setattr(bot.OptimizationProgressWindow, 'update_progress', broken_update)
    owner = QWidget()
    try:
        reviewer.review_update_wait(owner, bot.VIEWPORTS[0], 'regression')
        findings = {item['id']: item for item in reviewer.report['checks']}
        failure = findings['regression.updates-waiting.' + expected_failure]
        assert failure['status'] == 'failed' and failure['severity'] == 'critical'
        assert (reviewer.output / 'regression-updates-waiting.png').is_file()
    finally:
        owner.close()
        owner.deleteLater()
        app.processEvents()


def test_cli_persists_failure_evidence_and_returns_nonzero(tmp_path, monkeypatch, capsys):
    report = {'status': 'failed', 'counts': {'passed': 2, 'failed': 1, 'critical': 1},
        'checks': [{'id': 'fixture.clipped', 'severity': 'critical', 'status': 'failed', 'reason': 'Action clipped.'}],
        'manual_review': [], 'documentation_comparison': [], 'screenshots': [],
        'limitations': 'Rule-based only.', 'platform': sys.platform}
    output = tmp_path / 'explicit-output'
    close = Mock()
    fake = Mock(output=output, run=Mock(return_value=report), close=close)
    def construct(path):
        assert path == output
        output.mkdir()
        return fake
    monkeypatch.setattr(bot, 'CriticalReview', construct)
    assert bot.main(['--output', str(output)]) == 1
    printed = json.loads(capsys.readouterr().out)
    assert printed['coverage_complete'] is False and printed['counts']['critical'] == 1
    assert json.loads((output / 'critical-review.json').read_text()) == report
    assert 'fixture.clipped' in (output / 'critical-review.md').read_text()
    close.assert_called_once_with()
    assert set(path.name for path in output.iterdir()) == {'critical-review.json', 'critical-review.md'}


def test_github_summary_is_opt_in_and_requires_runner_destination(tmp_path, monkeypatch):
    monkeypatch.delenv('GITHUB_STEP_SUMMARY', raising=False)
    forbidden = Mock(side_effect=AssertionError('Invalid summary configuration must fail before UI construction.'))
    monkeypatch.setattr(bot, 'CriticalReview', forbidden)
    with pytest.raises(SystemExit):
        bot.main(['--output', str(tmp_path / 'review'), '--github-summary'])
    forbidden.assert_not_called()


def test_output_does_not_follow_a_linked_directory(tmp_path, app, monkeypatch):
    output = tmp_path / 'linked-output'
    output.mkdir()
    original = Path.lstat
    from types import SimpleNamespace
    def lstat(path, *args, **kwargs):
        value = original(path, *args, **kwargs)
        if path == output:
            return SimpleNamespace(st_mode=value.st_mode, st_file_attributes=0x400)
        return value
    monkeypatch.setattr(Path, 'lstat', lstat)
    with pytest.raises(ValueError, match='Indirect'):
        bot.CriticalReview(output)
    assert not list(output.iterdir())


def test_documentation_inventory_does_not_certify_missing_or_matching_claims(tmp_path):
    docs = tmp_path / 'docs'
    docs.mkdir()
    (docs / 'FEATURES.md').write_text('# Feature claims\n\n| Capability | Implementation |\n| --- | --- |\n| CPU review | Integration |\n', encoding='utf-8')
    compared = bot.documentation_comparison(tmp_path)
    assert compared[0]['status'] == 'manual_comparison_required'
    assert 'CPU review' in compared[0]['capabilities']
    assert compared[1]['status'] == 'missing'


def test_ci_runs_the_bot_before_publishing_and_retains_failure_artifacts():
    source = (ROOT / '.github/workflows/windows.yml').read_text(encoding='utf-8')
    review = source.index('python tools/critical_review.py')
    build = source.index('name: Build, verify and archive')
    assert review < build
    assert '--github-summary' in source[review:build]
    assert 'if: always()' in source and 'path: test-artifacts/' in source
