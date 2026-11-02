"""Recommendation action labels and results use only disposable fixture calls."""
import copy
import itertools
import os
import time
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QCheckBox, QHeaderView, QWidget

from optimus.ui import SecurityReviewDialog
from optimus.widgets.theme import build_qss


_UPDATE_KEYS = ('windows_security_updates_enabled', 'windows_security_updates_install_approved',
                'windows_security_update_agreements_accepted')
_IDENTITIES = ('enable_firewall', 'enable_defender_realtime', 'update_defender_signatures',
               'review_windows_update', 'review_security', 'review_legacy_patches',
               'remediate_defender_threats', 'restart_required', 'unknown-fixture')


def _report():
    return {'recommendations': [{'id': identity, 'title': identity.replace('_', ' ').title(),
        'reason': 'Fixture finding; no host operation is allowed.'} for identity in _IDENTITIES]}


@pytest.fixture(scope='module')
def app():
    value = QApplication.instance() or QApplication([])
    value.setQuitOnLastWindowClosed(False)
    return value


@pytest.fixture
def dialog_factory(app, monkeypatch):
    monkeypatch.setenv('OPTIMUS_REDUCE_MOTION', '1')
    dialogs = []
    def create(settings=None, report=None, outcome=None):
        owner = QWidget()
        owner.setStyleSheet(build_qss('cyber'))
        choices = dict(settings or {})
        backend = SimpleNamespace(preferences=Mock(return_value=choices),
            apply_security_recommendation=Mock(return_value=outcome or {'status': 'completed'}))
        dialog = SecurityReviewDialog(backend, copy.deepcopy(report or _report()), owner)
        dialog.resize(850, 650)
        dialog.show()
        app.processEvents()
        dialogs.append((dialog, owner))
        return dialog, backend
    yield create
    for dialog, owner in dialogs:
        dialog.shutdown()
        dialog.close()
        dialog.deleteLater()
        owner.close()
        owner.deleteLater()
    app.processEvents()


def _settle_until(app, predicate):
    deadline = time.monotonic() + 3
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        QTest.qWait(5)
    app.processEvents()
    assert predicate(), 'Disposable UI fixture did not settle.'


def _by_id(dialog):
    return {identity: dialog._selection_controls[identity] for identity in _IDENTITIES}


@pytest.mark.parametrize('flags', list(itertools.product((True, False), repeat=3)))
def test_known_actions_describe_current_saved_update_route(dialog_factory, flags):
    dialog, backend = dialog_factory(dict(zip(_UPDATE_KEYS, flags)))
    controls = _by_id(dialog)
    expected = {'enable_firewall': 'Apply', 'enable_defender_realtime': 'Apply',
        'update_defender_signatures': 'Apply', 'review_windows_update': 'Install' if all(flags) else 'Open',
        'review_security': 'Open', 'review_legacy_patches': 'View',
        'remediate_defender_threats': 'Review', 'restart_required': 'Open', 'unknown-fixture': 'Review'}
    assert {identity: control.text() for identity, control in controls.items()} == expected
    assert all(not control.isChecked() for control in controls.values())
    assert not dialog.apply_button.isEnabled()
    backend.apply_security_recommendation.assert_not_called()


@pytest.mark.parametrize('bad', ['true', 1, None])
def test_truthy_nonboolean_saved_flags_do_not_promise_install(dialog_factory, bad):
    flags = dict.fromkeys(_UPDATE_KEYS, True)
    flags[_UPDATE_KEYS[1]] = bad
    dialog, _ = dialog_factory(flags)
    assert dialog._selection_controls['review_windows_update'].text() == 'Open'


@pytest.mark.parametrize('approved', [False, True])
def test_select_supported_fixes_excludes_manual_threat_and_unknown_actions(dialog_factory, approved):
    dialog, backend = dialog_factory(dict.fromkeys(_UPDATE_KEYS, approved))
    QTest.mouseClick(dialog.select_fixes_button, Qt.LeftButton)
    chosen = {identity for identity, choice in dialog._selection_controls.items() if choice.isChecked()}
    expected = {'enable_firewall', 'enable_defender_realtime', 'update_defender_signatures'}
    if approved:
        expected.add('review_windows_update')
    assert chosen == expected
    backend.apply_security_recommendation.assert_not_called()
    QTest.mouseClick(dialog.clear_selection_button, Qt.LeftButton)
    assert all(not choice.isChecked() for choice in dialog._selection_controls.values())
    assert not dialog.apply_button.isEnabled()
    backend.apply_security_recommendation.assert_not_called()


@pytest.mark.parametrize('identity', ['enable_firewall', 'review_security', 'review_legacy_patches', 'review_windows_update'])
def test_selection_requires_its_own_confirmation_before_any_operation(dialog_factory, app, identity):
    dialog, backend = dialog_factory(dict.fromkeys(_UPDATE_KEYS, True))
    choice = dialog._selection_controls[identity]
    choice.setChecked(True)
    backend.apply_security_recommendation.assert_not_called()
    dialog._ask_recommendation = Mock(return_value=False)
    dialog.apply_selected()
    backend.apply_security_recommendation.assert_not_called()
    assert 'Declined' in dialog.table.item(dialog._row(identity), 3).text()
    choice.setChecked(True)
    dialog._ask_recommendation.return_value = True
    dialog.apply_selected()
    _settle_until(app, lambda: dialog._worker is None)
    backend.apply_security_recommendation.assert_called_once()
    assert backend.apply_security_recommendation.call_args.args == (identity,)
    assert all(not control.isChecked() for control in dialog._selection_controls.values())


@pytest.mark.parametrize('display_approved,current_approved', list(itertools.product((False, True), repeat=2)))
def test_windows_update_route_is_bound_to_the_confirmed_label(dialog_factory, app,
                                                            display_approved, current_approved):
    dialog, backend = dialog_factory(dict.fromkeys(_UPDATE_KEYS, display_approved))
    identity = 'review_windows_update'
    choice = dialog._selection_controls[identity]
    assert choice.text() == ('Install' if display_approved else 'Open')
    backend.preferences.return_value = dict.fromkeys(_UPDATE_KEYS, current_approved)
    dialog._ask_recommendation = Mock(return_value=True)
    choice.setChecked(True)
    backend.apply_security_recommendation.assert_not_called()
    dialog.apply_selected()
    _settle_until(app, lambda: dialog._worker is None)
    backend.apply_security_recommendation.assert_called_once()
    assert backend.apply_security_recommendation.call_args.args == (identity,)
    assert backend.apply_security_recommendation.call_args.kwargs['allow_saved_update_install'] is display_approved
    # The captured route can narrow a later settings change; its display never
    # substitutes for the service's independent fresh approval validation.
    assert dialog._presentations[identity]['selection_label'] == choice.text()


def test_manual_outcome_and_specific_reason_remain_visible_without_json(dialog_factory, app):
    identity = 'review_security'
    report = {'recommendations': [{'id': identity, 'title': 'Historical detection review ' * 12,
        'reason': 'Fixture retained detection history.'}]}
    result = {'status': 'manual_action_required', 'opened': 'windowsdefender:',
        'reason': 'Review the retained detections in Windows Security.'}
    dialog, backend = dialog_factory(report=report, outcome=result)
    dialog._ask_recommendation = Mock(return_value=True)
    dialog._selection_controls[identity].setChecked(True)
    dialog.apply_selected()
    _settle_until(app, lambda: dialog._worker is None)
    assert dialog.outcome.isVisibleTo(dialog)
    assert 'No automatic installation or removal was applied' in dialog.outcome.text()
    assert result['reason'] in dialog.outcome.text()
    assert 'Review the retained detections' in dialog.outcome.visible_text
    assert dialog.rect().contains(dialog.outcome.rect().translated(dialog.outcome.mapTo(dialog, QPoint())))
    backend.apply_security_recommendation.assert_called_once()


def test_long_result_does_not_expand_table_result_column_offscreen(dialog_factory, app):
    dialog, backend = dialog_factory()
    dialog._active = 'enable_firewall'
    dialog._item_result({'status': 'blocked', 'reason': 'A policy-controlled fixture restriction. ' * 100})
    app.processEvents()
    assert dialog.table.horizontalHeader().sectionResizeMode(3) == QHeaderView.Interactive
    assert dialog.table.columnWidth(3) == 230
    right = dialog.table.columnViewportPosition(3) + dialog.table.columnWidth(3)
    assert right <= dialog.table.viewport().width() + 1
    assert 'policy-controlled fixture restriction' in dialog.table.item(dialog._row('enable_firewall'), 3).toolTip()
    assert dialog.rect().contains(dialog.close_button.rect().translated(dialog.close_button.mapTo(dialog, QPoint())))
    backend.apply_security_recommendation.assert_not_called()


@pytest.mark.parametrize('identity', ['review_windows_update', 'review_legacy_patches', 'enable_firewall'])
def test_single_labeled_action_stays_readable_after_late_font_change(dialog_factory, app, identity):
    dialog, backend = dialog_factory(dict.fromkeys(_UPDATE_KEYS, True))
    row = dialog._row(identity)
    choice = dialog._selection_controls[identity]
    cell = dialog.table.cellWidget(row, 0)
    assert cell.findChildren(QCheckBox) == [choice]
    assert dialog.table.item(row, 0).data(Qt.CheckStateRole) is None
    assert not dialog.table.item(row, 0).flags() & Qt.ItemIsUserCheckable
    choice.setStyleSheet('font-size:24px;')
    _settle_until(app, lambda: choice.font().pixelSize() == 24
        and choice.width() >= choice.fontMetrics().horizontalAdvance(choice.text()) + 22
        and cell.geometry().height() >= choice.sizeHint().height() + 4)
    assert dialog.table.columnWidth(0) >= choice.minimumWidth() + 16
    assert choice.isVisibleTo(dialog)
    assert choice.width() >= choice.fontMetrics().horizontalAdvance(choice.text()) + 22
    assert not choice.isChecked()
    backend.apply_security_recommendation.assert_not_called()


@pytest.mark.parametrize('first', [
    {'status': 'uncertain', 'outcomeKnown': False, 'running': True, 'may_continue': True},
    {'status': 'completed', 'reboot_required': True},
    {'status': 'completed', 'result': {'status': 'running', 'running': True}},
    {'status': 'cancelled', 'cancelled': True},
    None,
])
def test_unresolved_or_restart_outcome_defers_remaining_actions(dialog_factory, app, first):
    dialog, backend = dialog_factory()
    backend.apply_security_recommendation.side_effect = [first, {'status': 'completed'}]
    dialog._ask_recommendation = Mock(return_value=True)
    for identity in ('enable_firewall', 'update_defender_signatures'):
        dialog._selection_controls[identity].setChecked(True)
    dialog.apply_selected()
    _settle_until(app, lambda: dialog._worker is None)
    backend.apply_security_recommendation.assert_called_once()
    assert 'Deferred:' in dialog.table.item(dialog._row('update_defender_signatures'), 3).text()
    assert any(row['result'].get('status') == 'deferred' for row in dialog._results
        if isinstance(row.get('result'), dict))
    assert not dialog._queue


def test_known_finished_failure_still_runs_next_approved_action(dialog_factory, app):
    dialog, backend = dialog_factory()
    backend.apply_security_recommendation.side_effect = [
        {'status': 'failed', 'outcomeKnown': True, 'running': False, 'may_continue': False,
         'reason': 'The finished fixture control was declined.'}, {'status': 'completed'}]
    dialog._ask_recommendation = Mock(return_value=True)
    for identity in ('enable_firewall', 'update_defender_signatures'):
        dialog._selection_controls[identity].setChecked(True)
    dialog.apply_selected()
    _settle_until(app, lambda: dialog._worker is None)
    assert backend.apply_security_recommendation.call_count == 2
    assert dialog.table.item(dialog._row('update_defender_signatures'), 3).text() == 'completed'


def test_worker_exception_retains_result_and_defers_remaining_actions(dialog_factory, app):
    dialog, backend = dialog_factory()
    backend.apply_security_recommendation.side_effect = RuntimeError('Fixture unknown native outcome')
    dialog._ask_recommendation = Mock(return_value=True)
    for identity in ('enable_firewall', 'update_defender_signatures'):
        dialog._selection_controls[identity].setChecked(True)
    dialog.apply_selected()
    _settle_until(app, lambda: dialog._worker is None)
    backend.apply_security_recommendation.assert_called_once()
    assert 'Fixture unknown native outcome' in dialog.table.item(dialog._row('enable_firewall'), 3).text()
    assert 'Deferred:' in dialog.table.item(dialog._row('update_defender_signatures'), 3).text()
