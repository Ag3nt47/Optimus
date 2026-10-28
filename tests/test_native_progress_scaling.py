"""Disposable dashboard geometry grows and contracts without host maintenance."""
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace
import uuid

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QWidget

from optimus.ui import OptimizationProgressWindow
from optimus.widgets.theme import build_qss
from test_native_worker_cards import app, fonts


CORE = ('maintenance', 'software_updates', 'driver_updates', 'security_scan')


def settle(value, app):
    previous, stable = None, 0
    for _ in range(80):
        app.processEvents()
        QTest.qWait(3)
        current = (value.size().toTuple(), value.worker_grid.size().toTuple(),
            tuple((card.size().toTuple(), card.heading.font().key(), card._spacious)
                for card in value.worker_cards.values()),
            value.body_scroll.verticalScrollBar().maximum())
        stable = stable + 1 if current == previous else 0
        if stable >= 3:
            return
        previous = current
    raise AssertionError('The actual responsive dashboard geometry did not settle.')


def mapped(widget, ancestor):
    return widget.rect().translated(widget.mapTo(ancestor, QPoint()))


def assert_hit(value, widget):
    point = widget.mapTo(value, widget.rect().center())
    assert value.rect().contains(mapped(widget, value))
    hit = value.childAt(point)
    assert hit is widget or hit is not None and widget.isAncestorOf(hit)


def assert_geometry(value, app):
    cards = [value.worker_cards[key] for key in CORE]
    assert len({card.width() for card in cards}) <= 2
    assert max(card.width() for card in cards) - min(card.width() for card in cards) <= 1
    assert max(card.height() for card in cards) - min(card.height() for card in cards) <= 1
    for index, card in enumerate(cards):
        assert value.worker_grid.rect().contains(card.geometry())
        assert value.body_scroll.widget().rect().contains(mapped(card, value.body_scroll.widget()))
        assert not card.heading.geometry().intersects(card.activity_ring.geometry())
        essentials = (card.heading, card.status, card.activity_ring, card.progress, card.totals,
            *card.navigation_buttons.values())
        if card._spacious:
            essentials += (card.completion, card.details, card.elapsed)
        for field in essentials:
            assert field.isVisibleTo(value), field.objectName()
            assert card.contentsRect().contains(mapped(field, card)), (field.objectName(), field.geometry(), card.rect())
        for other in cards[index + 1:]:
            assert not card.geometry().intersects(other.geometry())
        for button in card.navigation_buttons.values():
            value.body_scroll.ensureWidgetVisible(button, 0, 2)
            settle(value, app)
            assert value.body_scroll.viewport().rect().contains(mapped(button, value.body_scroll.viewport()))
            assert_hit(value, button)
    assert value.body_scroll.horizontalScrollBar().maximum() == 0
    assert value.content_scroll.horizontalScrollBar().maximum() == 0
    assert_hit(value, value.cancel_button)
    assert_hit(value, value.details_toggle)
    assert not value.cancel.is_set()
    assert not value.details_toggle.isChecked()
    assert not value.log.isVisible() and not value.processes.isVisible()


def observation(value):
    first = value.worker_cards['maintenance']
    return {'window': value.size().toTuple(), 'card': first.size().toTuple(),
        'heading_pixels': first.heading.fontMetrics().height(),
        'title_pixels': value.title.fontMetrics().height(),
        'ring': first.activity_ring.size().toTuple(), 'spacious': first._spacious,
        'body_scroll_maximum': value.body_scroll.verticalScrollBar().maximum()}


def capture(value, label, measured):
    directory = os.environ.get('OPTIMUS_CAPTURE_LAYOUT_DIR')
    if not directory:
        return
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    identity = label + '-' + uuid.uuid4().hex
    image = directory / (identity + '.png')
    assert value.grab().save(str(image))
    record = directory / (identity + '.json')
    with record.open('x', encoding='utf-8') as stream:
        json.dump(dict(measured, image=str(image), fixture_only=True), stream, indent=2)


@pytest.fixture
def dashboard(tmp_path, app, monkeypatch):
    monkeypatch.setenv('OPTIMUS_REDUCE_MOTION', '1')
    data = tmp_path / 'data'
    data.mkdir()
    original = b'{"saved_choice": "keep", "automatic_actions": false}\n'
    preferences = data / 'preferences.json'
    preferences.write_bytes(original)
    actions = []

    def forbidden(*args, **kwargs):
        actions.append((args, kwargs))
        raise AssertionError('A geometry fixture must not perform a backend action.')

    backend = SimpleNamespace(data_dir=data, workflow_navigation_status=lambda: {},
        request_workflow_navigation=forbidden, run_workflow=forbidden,
        save_preferences=forbidden, apply_privacy=forbidden)
    owner = QWidget()
    owner.backend = backend
    owner.smoke_test = True
    owner.setProperty('_optimus_no_reveal', True)
    owner.setStyleSheet(build_qss())
    value = OptimizationProgressWindow(owner, threading.Event())
    value._optimus_no_reveal = True
    value.configure_workers({'software_updates_enabled': True,
        'driver_updates_enabled': True, 'spyware_scan_enabled': True})
    value.update_progress({'stage': 'Clean selected temporary files', 'percentage': 44,
        'description': 'Sample cleanup checks supported files and records measured outcomes.',
        'item_progress': {'key': 'maintenance:fixture', 'label': 'Selected temporary files',
            'phase': 'cleanup', 'percent': 62},
        'resource_totals': {'removed_known': True, 'removed_bytes': 2147483648, 'removed_files': 321},
        'jobs': [{'id': 'software_updates', 'status': 'installing', 'current': 1, 'total': 4,
            'description': 'Downloading the approved fixture application.', 'current_package': 'Fixture.Application',
            'elapsed_seconds': 65, 'item_progress': {'key': 'app:fixture', 'label': 'Fixture.Application',
                'phase': 'download', 'percent': 81},
            'resource_totals': {'installed_known': True, 'installed_items': 1,
                'downloaded_known': True, 'downloaded_bytes': 104857600}},
            {'id': 'driver_updates', 'status': 'waiting_for_installer', 'current': 0, 'total': 2,
                'description': 'Waiting for the exact approved fixture driver.', 'elapsed_seconds': 25}]})
    value.show()
    settle(value, app)
    yield value
    assert actions == []
    assert preferences.read_bytes() == original
    assert sorted(path.relative_to(data).as_posix() for path in data.rglob('*')) == ['preferences.json']
    value.completed = True
    value.close()
    owner.close()
    value.deleteLater()
    owner.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()


def test_same_live_dashboard_fills_large_windows_and_restores_compact_geometry(dashboard, app):
    value = dashboard
    observations = []
    for width, height in ((850, 650), (1920, 1080), (2560, 1440), (850, 650)):
        value.resize(width, height)
        settle(value, app)
        assert value.size().toTuple() == (width, height)
        assert_geometry(value, app)
        assert value.worker_cards['maintenance'].activity_ring.value_percent == 62
        assert value.worker_cards['software_updates'].activity_ring.value_percent == 81
        value.body_scroll.verticalScrollBar().setValue(0)
        measured = observation(value)
        observations.append(measured)
        capture(value, f'dashboard-{width}x{height}', measured)
    compact, full_hd, large, restored = observations
    assert full_hd['heading_pixels'] > compact['heading_pixels']
    assert full_hd['title_pixels'] > compact['title_pixels']
    assert full_hd['card'][0] > compact['card'][0] and full_hd['card'][1] > compact['card'][1]
    assert full_hd['ring'][0] > compact['ring'][0]
    assert full_hd['spacious'] and full_hd['body_scroll_maximum'] == 0
    assert large['spacious'] and large['body_scroll_maximum'] == 0
    assert large['card'][1] > full_hd['card'][1]
    assert restored == compact
    assert all(card._compact and not card._spacious for card in value.worker_cards.values())


def test_live_inherited_accessibility_fonts_survive_expansion_and_contraction(dashboard, app):
    value = dashboard
    value.owner.setStyleSheet(build_qss() + '\nQFrame#WorkflowWorkerCard QLabel, '
        'QFrame#WorkflowWorkerCard QProgressBar {font-size:26px;}')
    value.resize(850, 650)
    settle(value, app)
    small = observation(value)
    assert all(card.heading.fontMetrics().height() >= 26 for card in value.worker_cards.values())
    for size in ((1920, 1080), (2560, 1440), (850, 650)):
        value.resize(*size)
        settle(value, app)
        assert_geometry(value, app)
        assert all(card.heading.fontMetrics().height() >= 26 for card in value.worker_cards.values())
        capture(value, f'dashboard-large-font-{size[0]}x{size[1]}', observation(value))
    assert observation(value)['heading_pixels'] == small['heading_pixels']


def test_wide_short_dashboard_scrolls_without_reducing_readable_text(dashboard, app):
    value = dashboard
    compact = observation(value)
    value.resize(1920, 450)
    settle(value, app)
    assert_geometry(value, app)
    wide = observation(value)
    assert wide['heading_pixels'] == compact['heading_pixels']
    assert value.body_scroll.verticalScrollBar().maximum() > 0
    assert all(not card._spacious for card in value.worker_cards.values())


def test_security_findings_and_quarantine_summary_survive_roomy_layout_transitions(dashboard, app):
    value = dashboard
    value._update_scan_card({'scan_status': 'scanning', 'active_threat_count': 2,
        'description': 'Synthetic scan findings, never a Defender invocation.',
        'item_progress': {'key': 'security:defender:quick_scan', 'label': 'Fixture security scan',
            'phase': 'scan', 'percent': 37, 'progress_source': 'defender_mpclient'}})
    card = value.worker_cards['security_scan']
    full_description = card.totals.toolTip()
    for size in ((2560, 1440), (850, 650), (2560, 1440)):
        value.resize(*size)
        settle(value, app)
        assert 'Active threats: 2' in card.totals.visible_text
        assert 'Quarantine: Defender policy' in card.totals.visible_text
        assert card.totals.toolTip() == full_description
        assert 'Installs:' not in card.totals.text()
        assert 'Downloaded:' not in card.totals.text()
        assert card.activity_ring.value_percent == 37
        value.body_scroll.verticalScrollBar().setValue(0)
        capture(value, f'dashboard-scan-summary-{size[0]}x{size[1]}', observation(value))
