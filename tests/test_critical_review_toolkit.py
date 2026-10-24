"""The UI review inspects new toolkit controls using fixture services only."""
import importlib.util
import json
import os
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import Qt
from PySide6.QtCore import QRect
from PySide6.QtWidgets import QApplication, QTabBar, QWidget

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('optimus_critical_review_toolkit', ROOT / 'tools/critical_review.py')
bot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bot)


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize('system', ['win32', 'linux'])
@pytest.mark.parametrize('dimensions', bot.VIEWPORTS)
def test_toolkit_geometry_and_production_preview_handlers_use_only_inert_fixtures(tmp_path, app, system, dimensions):
    reviewer = bot.CriticalReview(tmp_path / 'review', system=system)
    backend = bot.FixtureBackend(tmp_path / 'in-memory-only', system)
    factory = bot.OptimusWindow if system == 'win32' else bot.NativeOptimusWindow
    window = factory(backend, smoke_test=True)
    try:
        reviewer.viewport(window, dimensions, 'fixture-main')
        reviewer.review_toolkit(window, backend, dimensions, 'fixture')
        failures = [row for row in reviewer.report['checks'] if row['status'] == 'failed']
        assert not failures, json.dumps(failures, indent=2, ensure_ascii=False)
        by_id = {row['id']: row for row in reviewer.report['checks']}
        for suffix in ('no-constructor-actions', 'apply-starts-disabled', 'unsaved-preview-refused',
                       'explicit-save', 'preview-enables-exact-apply', 'negative-approval', 'explicit-apply-once',
                       'consumed-plan-disabled', 'preview-tab-ownership', 'changed-scope-invalidates-preview',
                       'explicit-report-archive', 'no-other-actions'):
            assert by_id['fixture.toolkit.' + suffix]['status'] == 'passed'
        assert len(reviewer.report['screenshots']) == 8
        assert not (tmp_path / 'in-memory-only').exists()
    finally:
        reviewer.dispose(window)
        reviewer.close()


@pytest.mark.parametrize('system', ['win32', 'linux'])
def test_setup_toolkit_pages_all_reachable_without_saving_or_running(tmp_path, app, system):
    reviewer = bot.CriticalReview(tmp_path / 'review', system=system)
    backend = bot.FixtureBackend(tmp_path / 'in-memory-only', system)
    try:
        reviewer.review_setup(backend, (850, 650), 'fixture')
        failures = [row for row in reviewer.report['checks'] if row['status'] == 'failed']
        assert not failures, json.dumps(failures, indent=2, ensure_ascii=False)
        assert any(row['id'] == 'fixture.setup-toolkit.selection-controls' for row in reviewer.report['checks'])
        assert backend.saved == [] and not (tmp_path / 'in-memory-only').exists()
        assert not any(getattr(backend, name).called for name in bot.OPTIONAL_OPERATIONS)
        screenshots = {row['name'] for row in reviewer.report['screenshots']}
        assert {'fixture-setup-toolkit-1', 'fixture-setup-toolkit-3'} <= screenshots
    finally:
        reviewer.close()


def test_wrapped_checkbox_measurement_uses_real_text_height_and_detects_clipping(tmp_path, app):
    reviewer = bot.CriticalReview(tmp_path / 'review')
    window = QWidget()
    window.setAttribute(Qt.WA_DontShowOnScreen, True)
    window.resize(320, 220)
    control = bot.WrappingCheckBox('I explicitly approve losing the old browser history and cookie undo backup', window)
    control.setGeometry(10, 10, 280, control.heightForWidth(280))
    window.show()
    app.processEvents()
    try:
        measured = bot.measure_button(control, window, (320, 220), app)
        assert measured['status'] == 'passed' and measured['wrapped_label']
        control.resize(280, 20)
        app.processEvents()
        assert bot.measure_button(control, window, (320, 220), app)['status'] == 'failed'
    finally:
        window.close()
        window.deleteLater()
        reviewer.close()
        app.processEvents()


def test_tab_decorative_overhang_does_not_masquerade_as_clipped_painted_label(tmp_path, app):
    reviewer = bot.CriticalReview(tmp_path / 'review')
    class NativeBorderTab(QTabBar):
        def initStyleOption(self, option, index):
            super().initStyleOption(option, index)
            option.rect = option.rect.adjusted(-1, 0, 1, 0)
    bar = NativeBorderTab()
    bar.addTab('Updates')
    bar.resize(180, 38)
    bar.show()
    app.processEvents()
    try:
        measured = bot.measure_tab_label(bar, 0)
        assert measured['tab_geometry']['x'] == -1
        assert measured['status'] == 'passed', measured
        assert measured['painted_text_geometry']['x'] > 0
    finally:
        bar.close()
        bar.deleteLater()
        reviewer.close()
        app.processEvents()


def test_tab_actual_text_clipping_or_elision_remains_a_failure(tmp_path, app):
    reviewer = bot.CriticalReview(tmp_path / 'review')
    owner = QWidget()
    owner.resize(100, 70)
    bar = QTabBar(owner)
    bar.setExpanding(False)
    bar.addTab('A deliberately long label that cannot fit')
    bar.setGeometry(0, 0, 65, 38)
    owner.show()
    app.processEvents()
    try:
        assert bar.width() == 65
        assert bot.measure_tab_label(bar, 0)['status'] == 'failed'
    finally:
        owner.close()
        owner.deleteLater()
        reviewer.close()
        app.processEvents()


@pytest.mark.parametrize('scale', [1.0, 1.25])
def test_compact_toolkit_uses_full_readable_tab_labels_without_overflow(tmp_path, app, scale):
    from optimus.widgets.maintenance_toolkit_panel import ToolkitChoices
    from optimus.widgets.theme import build_qss
    from PySide6.QtWidgets import QToolButton
    reviewer = bot.CriticalReview(tmp_path / 'review')
    editor = ToolkitChoices()
    editor.setAttribute(Qt.WA_DontShowOnScreen, True)
    editor.setStyleSheet(build_qss(scale=scale))
    editor.resize(560, 450)
    editor.show()
    try:
        bot.drain(app)
        assert editor.width() == 560
        bar = editor.tabs.tabBar()
        assert [bar.tabText(i) for i in range(bar.count())] == [
            'Workload', 'Caches', 'Growth', 'Recovery', 'Health', 'Updates']
        assert bar.accessibleTabName(1) == 'App caches'
        assert bar.accessibleTabName(2) == 'Folder growth'
        assert bar.accessibleTabName(3) == 'Recovery data'
        assert bar.accessibleTabName(4) == 'Measurements'
        for index in range(bar.count()):
            editor.tabs.setCurrentIndex(index)
            bot.drain(app)
            measured = bot.measure_tab_label(bar, index)
            assert measured['status'] == 'passed', json.dumps(measured, indent=2)
        assert not any(button.isVisibleTo(bar) for button in bar.findChildren(QToolButton))
    finally:
        editor.close()
        editor.deleteLater()
        reviewer.close()
        app.processEvents()
