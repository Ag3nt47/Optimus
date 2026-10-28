"""Resize real Qt surfaces with disposable data and no maintenance actions."""
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

from PySide6.QtCore import QPoint, QRect
from PySide6.QtGui import QFontInfo
from PySide6.QtWidgets import QPushButton

from optimus.sandbox import SandboxWindow
from optimus.ui import DetailedSettingsWindow, OptimizationProgressWindow, OptimusWindow
from optimus.workflow import DEFAULTS
from test_native_worker_cards import app, fonts


def settle(app):
    for _ in range(12):
        app.processEvents()


def snapshot(folder):
    return {str(path.relative_to(folder)): path.read_bytes()
            for path in folder.rglob('*') if path.is_file()}


def dispose(window, app):
    if isinstance(window, SandboxWindow):
        window.editor.document().setModified(False)
    if hasattr(window, 'shutdown'):
        window.shutdown()
    window.close()
    window.deleteLater()
    settle(app)


def rect_in(widget, parent):
    return QRect(widget.mapTo(parent, QPoint()), widget.size())


def test_main_menu_text_and_equal_buttons_grow_then_restore_without_saving(app, tmp_path):
    (tmp_path / 'ui-settings.json').write_text('{"theme":"slate","motion":false}', encoding='utf-8')
    backend = SimpleNamespace(data_dir=tmp_path,
        preferences=lambda: dict(DEFAULTS, reduce_motion=True))
    window = OptimusWindow(backend, smoke_test=True)
    try:
        window.show()
        settle(app)
        # Offscreen window managers can clamp the initial show to their virtual
        # desktop. Establish the same explicit normal size used on restoration.
        window.resize(1280, 820)
        settle(app)
        before = snapshot(tmp_path)
        normal = [(button.width(), button.fontMetrics().height()) for button in window.main_buttons]
        normal_brand = window._brand_label.fontMetrics().height()
        window.resize(2560, 1440)
        settle(app)
        assert window._surface_scale == 1.35
        assert len({button.width() for button in window.main_buttons}) == 1
        assert window._brand_label.fontMetrics().height() > normal_brand
        assert 'background: #020617' in window.styleSheet()  # Saved slate theme.
        for button, (width, height) in zip(window.main_buttons, normal):
            assert button.width() > width
            assert button.fontMetrics().height() > height
            assert window.landing_scroll.viewport().rect().contains(rect_in(button, window.landing_scroll.viewport()))
            assert button.fontMetrics().horizontalAdvance(button.text()) < button.width() - 20
        assert window.landing_scroll.verticalScrollBar().maximum() == 0
        window.resize(1280, 820)
        settle(app)
        assert window._surface_scale == 1.0
        assert window._brand_label.fontMetrics().height() == normal_brand
        assert [(button.width(), button.fontMetrics().height()) for button in window.main_buttons] == normal
        assert snapshot(tmp_path) == before
    finally:
        dispose(window, app)


def test_main_resize_preserves_external_styles_and_short_window_scroll_access(app, tmp_path):
    window = OptimusWindow(SimpleNamespace(data_dir=tmp_path,
        preferences=lambda: dict(DEFAULTS, reduce_motion=True)), smoke_test=True)
    try:
        window.show()
        settle(app)
        custom = window.styleSheet() + '\nQLabel {color:#abcdef;}'
        window.setStyleSheet(custom)
        custom_button = 'QPushButton {font-size:27px;color:#abcdef;}'
        window.main_buttons[1].setStyleSheet(custom_button)
        window.resize(2560, 1440)
        settle(app)
        assert window.styleSheet() == custom
        assert window.main_buttons[1].styleSheet() == custom_button
        window.setMinimumSize(0, 0)
        window.resize(640, 360)
        settle(app)
        area = window.landing_scroll
        assert area.verticalScrollBar().maximum() > 0
        assert area.horizontalScrollBar().maximum() == 0
        for button in window.main_buttons:
            area.ensureWidgetVisible(button)
            settle(app)
            assert area.viewport().rect().contains(rect_in(button, area.viewport()))
    finally:
        dispose(window, app)


def test_settings_typography_sidebar_and_table_rows_grow_without_changing_choices(app, tmp_path):
    (tmp_path / 'ui-settings.json').write_text('{"theme":"crt","motion":false}', encoding='utf-8')
    window = DetailedSettingsWindow(SimpleNamespace(data_dir=tmp_path), smoke_test=True)
    try:
        window.show()
        settle(app)
        window.resize(1440, 920)
        settle(app)
        assert window._surface_scale == 1.0
        before = snapshot(tmp_path)
        original = window.page_description.fontMetrics().height()
        original_sidebar = window._settings_sidebar.width()
        window.resize(2560, 1440)
        settle(app)
        assert window._surface_scale == 1.35
        assert window.page_description.fontMetrics().height() > original
        assert window._settings_sidebar.width() > original_sidebar
        assert 'background: #000a00' in window.styleSheet()
        assert window.process_table.verticalHeader().defaultSectionSize() >= window.process_table.fontMetrics().height() + 12
        window.resize(1440, 920)
        settle(app)
        assert window._settings_sidebar.width() == original_sidebar
        assert window.page_description.fontMetrics().height() == original
        assert snapshot(tmp_path) == before
    finally:
        dispose(window, app)


def test_sandbox_editor_uses_extra_space_and_preserves_code_and_splitter_choice(app, tmp_path):
    source = tmp_path / 'source'
    (source / 'src/optimus').mkdir(parents=True)
    (source / 'src/optimus/ui.py').write_text('VALUE = 1\n', encoding='utf-8')
    window = SandboxWindow(SimpleNamespace(source_root=lambda: source, data_dir=tmp_path / 'data'))
    try:
        window.show()
        settle(app)
        before = snapshot(tmp_path)
        original_font = window.editor.fontMetrics().height()
        original_height = window.editor.height()
        window.editor.setPlainText('VALUE = 2\n')
        window.editor.document().setModified(True)
        window.resize(2560, 1440)
        settle(app)
        assert window._surface_scale == 1.35
        assert window.editor.fontMetrics().height() > original_font
        assert window.editor.height() > original_height
        assert window.splitter.height() > window.centralWidget().height() * 0.75
        assert window.editor.toPlainText() == 'VALUE = 2\n' and window.editor.document().isModified()
        window.splitter.moveSplitter(500, 1)
        settle(app)
        assert window._splitter_user_adjusted
        window.resize(2200, 1300)
        settle(app)
        assert window._splitter_user_adjusted
        assert window.splitter.sizes()[0] >= 450
        assert snapshot(tmp_path) == before
        assert all(button.toolTip() for button in window.findChildren(QPushButton))
    finally:
        dispose(window, app)


def test_native_settings_scales_saved_theme_without_changing_selected_tools(app, tmp_path):
    from optimus.native_settings import NativePreferences, full_native_settings
    from optimus.native_ui import NativeOptimusWindow, NativeSettingsWindow
    (tmp_path / 'ui-settings.json').write_text('{"theme":"slate","motion":false}', encoding='utf-8')
    backend = SimpleNamespace(data_dir=tmp_path,
        preferences=lambda: NativePreferences.validate(full_native_settings()),
        cleanup_categories=lambda: [], save_preferences=Mock(), optimize=Mock(),
        analysis_stop=Mock(), analysis_start=Mock(), close=Mock())
    owner = NativeOptimusWindow(backend, smoke_test=True)
    window = NativeSettingsWindow(backend, owner, smoke_test=True)
    try:
        window.show()
        settle(app)
        window.resize(1160, 800)
        window.tabs.setCurrentIndex(2)
        settle(app)
        before = snapshot(tmp_path)
        original = window.status.fontMetrics().height()
        window.resize(2560, 1440)
        settle(app)
        assert window._surface_scale == 1.35
        assert window.status.fontMetrics().height() > original
        assert window.tabs.currentIndex() == 2
        assert 'background: #020617' in window.styleSheet()
        backend.save_preferences.assert_not_called()
        backend.optimize.assert_not_called()
        assert snapshot(tmp_path) == before
    finally:
        dispose(window, app)
        dispose(owner, app)


def test_main_explicit_theme_changes_remain_responsive_through_later_resizes(app, tmp_path):
    window = OptimusWindow(SimpleNamespace(data_dir=tmp_path,
        preferences=lambda: dict(DEFAULTS, reduce_motion=True)), smoke_test=True)
    try:
        window.show()
        settle(app)
        window.resize(1280, 820)
        settle(app)
        before = snapshot(tmp_path)
        normal_font = window.status.fontMetrics().height()
        for theme, background in (('crt', '#000a00'), ('slate', '#020617'), ('cyber', '#0a0e14')):
            window.apply_appearance({'theme': theme, 'motion': False})
            assert window.property('_optimus_surface_style') == window.styleSheet()
            window.resize(2560, 1440)
            settle(app)
            assert window.status.fontMetrics().height() > normal_font
            assert f'background: {background}' in window.styleSheet()
            window.resize(1280, 820)
            settle(app)
            assert window._surface_scale == 1.0
            assert window.property('_optimus_surface_style') == window.styleSheet()
        assert snapshot(tmp_path) == before
    finally:
        dispose(window, app)


def test_settings_explicit_theme_save_keeps_resize_ownership_and_saved_choices(app, tmp_path):
    window = DetailedSettingsWindow(SimpleNamespace(data_dir=tmp_path), smoke_test=True)
    try:
        window.show()
        settle(app)
        window.resize(1440, 920)
        settle(app)
        window.theme_combo.setCurrentIndex(window.theme_combo.findData('crt'))
        window.motion_toggle.setChecked(False)
        window.save_preferences()  # Explicit appearance save in this disposable fixture.
        settle(app)
        assert json.loads(window.settings_path.read_text(encoding='utf-8')) == {'theme': 'crt', 'motion': False}
        assert window.property('_optimus_surface_style') == window.styleSheet()
        before = snapshot(tmp_path)
        normal_font = window.page_description.fontMetrics().height()
        window.resize(2560, 1440)
        settle(app)
        assert window.page_description.fontMetrics().height() > normal_font
        assert 'background: #000a00' in window.styleSheet()
        assert window.property('_optimus_surface_style') == window.styleSheet()
        assert snapshot(tmp_path) == before
    finally:
        dispose(window, app)


def test_native_explicit_theme_save_keeps_both_windows_responsive(app, tmp_path):
    from optimus.native_settings import NativePreferences, full_native_settings
    from optimus.native_ui import NativeOptimusWindow, NativeSettingsWindow
    backend = SimpleNamespace(data_dir=tmp_path,
        preferences=lambda: NativePreferences.validate(full_native_settings()),
        cleanup_categories=lambda: [], save_preferences=Mock(), optimize=Mock(),
        analysis_stop=Mock(), analysis_start=Mock(), close=Mock())
    owner = NativeOptimusWindow(backend, smoke_test=True)
    window = NativeSettingsWindow(backend, owner, smoke_test=True)
    try:
        window.show()
        settle(app)
        window.resize(1160, 800)
        settle(app)
        window.theme_choice.setCurrentText('slate')
        window.motion_choice.setChecked(False)
        # Only this explicitly invoked appearance handler can write the fixture
        # file; all operation callbacks remain inert mocks throughout the test.
        window.smoke_test = False
        window._save_appearance()
        window.smoke_test = True
        settle(app)
        assert owner.appearance == {'theme': 'slate', 'motion': False}
        assert json.loads(owner.preferences_path.read_text(encoding='utf-8')) == owner.appearance
        assert owner.property('_optimus_surface_style') == owner.styleSheet()
        assert window.property('_optimus_surface_style') == window.styleSheet()
        before = snapshot(tmp_path)
        normal_font = window.status.fontMetrics().height()
        window.resize(2560, 1440)
        settle(app)
        assert window.status.fontMetrics().height() > normal_font
        assert 'background: #020617' in window.styleSheet()
        assert window.property('_optimus_surface_style') == window.styleSheet()
        backend.save_preferences.assert_not_called()
        backend.optimize.assert_not_called()
        assert snapshot(tmp_path) == before
    finally:
        dispose(window, app)
        dispose(owner, app)


def test_generated_main_scaling_does_not_multiply_child_progress_typography(app, tmp_path):
    owner = OptimusWindow(SimpleNamespace(data_dir=tmp_path,
        preferences=lambda: dict(DEFAULTS, reduce_motion=True)), smoke_test=True)
    measured = []
    try:
        owner.show()
        settle(app)
        before = snapshot(tmp_path)
        for owner_size in ((1280, 820), (2560, 1440)):
            owner.resize(*owner_size)
            settle(app)
            progress = OptimizationProgressWindow(owner, threading.Event())
            try:
                progress.show()
                settle(app)
                readings = []
                for size, expected_heading, expected_title in (((850, 650), 13, 20), ((1920, 1080), 18, 27)):
                    progress.resize(*size)
                    settle(app)
                    card = progress.worker_cards['software_updates']
                    reading = (QFontInfo(card.heading.font()).pixelSize(), QFontInfo(progress.title.font()).pixelSize(),
                               progress.body_scroll.verticalScrollBar().maximum())
                    assert reading == (expected_heading, expected_title, 0)
                    if size == (1920, 1080):
                        assert all(card._spacious for card in progress.worker_cards.values())
                    readings.append(reading)
                measured.append(readings)
            finally:
                progress.completed = True
                dispose(progress, app)
        assert measured[0] == measured[1]
        assert snapshot(tmp_path) == before
    finally:
        dispose(owner, app)


def test_external_main_accessibility_font_remains_authoritative_for_child_progress(app, tmp_path):
    owner = OptimusWindow(SimpleNamespace(data_dir=tmp_path,
        preferences=lambda: dict(DEFAULTS, reduce_motion=True)), smoke_test=True)
    progress = None
    try:
        owner.show()
        settle(app)
        owner.resize(2560, 1440)
        settle(app)
        external = owner.styleSheet() + '\nQWidget {font-size:26px;}'
        owner.setStyleSheet(external)
        settle(app)
        assert owner.styleSheet() == external
        assert owner.canvas.styleSheet() == ''
        assert QFontInfo(owner.status.font()).pixelSize() == 26
        progress = OptimizationProgressWindow(owner, threading.Event())
        progress.show()
        settle(app)
        for size, expected in (((850, 650), 26), ((1920, 1080), 35)):
            progress.resize(*size)
            settle(app)
            assert QFontInfo(progress.worker_cards['software_updates'].heading.font()).pixelSize() == expected
        assert owner.styleSheet() == external
    finally:
        if progress is not None:
            progress.completed = True
            dispose(progress, app)
        dispose(owner, app)
