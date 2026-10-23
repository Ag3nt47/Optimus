"""Rule-based, fixture-only Optimus UX/workflow review with visual evidence.

No real Backend is constructed. These checks measure layout and exercise a few
explicit UI state transitions; they are not AI criticism, host optimization,
benchmarks or a claim to find every defect. See the report's manual-review gaps.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(ROOT / 'src'))

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QFont, QFontDatabase, QFontMetrics
from PySide6.QtWidgets import (QApplication, QAbstractButton, QMessageBox, QScrollArea,
    QDialog, QPlainTextEdit, QPushButton, QTabWidget, QWizard)

from optimus.automation import CURRENT_SETUP_VERSION
from optimus.filesystem import _ordinary_data_path
from optimus.native_settings import full_native_settings
from optimus.native_ui import NativeOptimusWindow, NativeSetupWizard
from optimus.onboarding import InitialSetupWizard
from optimus.ui import OptimizationProgressWindow, OptimusWindow
from optimus.launch_review import ask_launch_review
from optimus.widgets.recovery_panel import RecoveryPanel
from optimus.widgets.questions import make_yes_no_dialog
from optimus.navigation import NavigationHub
from optimus.workflow import full_optimization_settings


VIEWPORTS = ((850, 650), (1024, 768))
OPTIONAL_OPERATIONS = ('configure_smart_maintenance', 'activate_smart_maintenance',
    'smart_maintenance_preview', 'smart_maintenance_stop', 'driver_recovery_scan',
    'driver_backup_export', 'driver_backup_verify', 'driver_rollback', 'system_restore_scan',
    'system_restore_create', 'system_restore_delete', 'system_restore_open',
    'file_utilities_capabilities', 'file_utilities_encrypt', 'file_utilities_decrypt',
    'file_utilities_split', 'file_utilities_join', 'file_utilities_shred_plan', 'file_utilities_shred_apply',
    'change_history_scan', 'change_history_revert')
MANUAL_GAPS = [
    {'area': 'CPU and RAM', 'review': 'Run representative workloads and compare measured CPU, memory pressure and responsiveness. Fixture configuration readback cannot establish a speed gain or identify every unnecessary process.'},
    {'area': 'Live CPU and network metrics', 'review': 'Displayed fixture values prove rendering only. Worker CPU covers identified Optimus threads and excludes external installers. Passive adapter traffic, reported link speed and cached OS connectivity do not measure Internet capacity or prove maximum computer performance.'},
    {'area': 'GPU', 'review': 'Check actual GPU utilization, VRAM, temperatures and graphics drivers separately. GPU/shader cache cleanup is not GPU tuning; no overclock or unsupported voltage change is tested.'},
    {'area': 'One-click scope', 'review': 'Confirm that saved approvals match the intended computer. Browser close-or-skip, restarts, vendor installers, process removal and native permissions may still require a decision; maximum automation is not universal unattended support.'},
    {'area': 'Updates and security', 'review': 'Validate real signed catalogs, hardware, reboot state and effective security controls on representative machines. No driver, firmware, Windows upgrade, compatibility workaround or security change is applied by this bot.'},
    {'area': 'Files, browsers and cloud', 'review': 'Exercise disposable real profiles, filesystem locks, Trash/Recycle Bin recovery and an explicitly connected test cloud account. No real user file or connected account is touched here.'},
    {'area': 'Accessibility and desktop behavior', 'review': 'Review screenshots, screen readers, high contrast, keyboard order, localization, text scaling, real multi-monitor desktops and animation quality. Offscreen reachability does not prove native desktop usability.'},
    {'area': 'Sandbox and privileged prompts', 'review': 'Review actual source editing/restart handoff, UAC/polkit/macOS authorization, unsaved work and packaging signatures separately. The bot never approves an OS dialog.'},
    {'area': 'Optional recovery and file tools', 'review': 'Constructors, unsupported UI states and cached worker displays use in-memory fixtures. Separate service tests cover disposable files and injected native adapters. This bot does not export drivers, restore Windows, encrypt user documents or prove physical erasure.'},
]


class FixtureBackend:
    """In-memory services only; every mutation is a recorded fixture call."""
    def __init__(self, data_dir, system):
        self.data_dir, self.system = Path(data_dir), system
        self.settings = (full_optimization_settings if system == 'win32' else full_native_settings)()
        self.settings.update(setup_version=CURRENT_SETUP_VERSION, automation_mode='manual',
            reduce_motion=True, background_analysis_enabled=False, speed_enhancements_enabled=True,
            auto_continue_fixes_enabled=False, parallel_app_updates_enabled=False,
            drive_optimization_enabled=False,
            browser_cache_enabled=False, browser_history_enabled=False, browser_cookies_enabled=False,
            browser_download_history_enabled=False, post_process_review_enabled=True)
        self.saved = []
        self.optimize = Mock(side_effect=AssertionError('Only an explicitly installed fixture workflow may run.'))
        self.available_fixes = Mock(return_value={
            'actions': [{'id': 'fixture-correction', 'title': 'Apply the eligible fixture correction', 'kind': 'fixture'}],
            'manual_items': [{'id': 'fixture-restart', 'title': 'Review a restart', 'reason': 'Manual action remains required.'}],
            'summary': 'One fixture action and one manual item.'})
        self.apply_available_fixes = Mock()
        self.speed_enhancements_scan = Mock(return_value=speed_report())
        self.apply_speed_enhancements = Mock()
        self.speed_enhancements_history = Mock(return_value=[])
        self.restore_speed_enhancements = Mock()
        self.analysis_start, self.analysis_stop = Mock(), Mock()
        for name in OPTIONAL_OPERATIONS:
            setattr(self, name, Mock(side_effect=AssertionError('Only explicitly requested fixture operations may run.')))
        self.smart_maintenance_status = Mock(return_value={'status': 'stopped', 'running': False,
            'note': 'Fixture monitoring remains stopped until launch approval.'})
        self.worker_status = Mock(return_value=[])
        self.navigation = NavigationHub()
        self.navigation_requests = []

    def workflow_navigation_status(self):
        return self.navigation.snapshot()

    def request_workflow_navigation(self, session, job, revision, action):
        self.navigation_requests.append((session, job, revision, action))
        return self.navigation.request(session, job, revision, action)

    def preferences(self):
        return copy.deepcopy(self.settings)

    def save_preferences(self, settings):
        self.saved.append(copy.deepcopy(settings))
        self.settings = copy.deepcopy(settings)
        return self.preferences()

    def cleanup_categories(self):
        return [{'id': 'fixture-temp', 'name': 'Disposable fixture temporary files',
                 'available': True, 'process_names': []}]


def speed_report(status='deferred'):
    return {'id': 'b' * 32, 'status': status, 'restore_available': False,
        'counts': {'applied': 0, 'deferred': 1}, 'failed_items': 0,
        'summary': '0 applied; 1 deferred. Permission or hardware support requires review.',
        'items': [{'id': 'fixture-power', 'title': 'Fixture power policy', 'status': 'deferred',
                   'reason': 'The supported control is unavailable in this fixture.'}]}


def workflow_report(identity='a' * 32, **changes):
    report = {'id': identity, 'status': 'completed', 'cancelled': False, 'stages': [],
        'security_review': {'recommendations': [{'id': 'fixture-manual', 'title': 'Review the remaining fixture finding'}]},
        'process_review': {'complete': True, 'actions': []},
        'final_verification': {'status': 'unverified', 'counts': {'confirmed': 0, 'unverified': 1},
            'coverage_complete': False}, 'summary': 'Fixture stages returned; one outcome remains unverified.'}
    report.update(changes)
    return report


def drain(app, predicate=lambda: True, *, timeout=5):
    deadline = time.monotonic() + timeout
    while True:
        app.processEvents()
        if predicate():
            return
        if time.monotonic() >= deadline:
            raise RuntimeError('The bounded fixture UI transition did not finish.')
        time.sleep(.005)


def _rect(rect):
    return {'x': rect.x(), 'y': rect.y(), 'width': rect.width(), 'height': rect.height()}


def reveal_control(control, window, app):
    """Select containing tabs, then test actual scroll reachability inside out."""
    parents, parent = [], control.parentWidget()
    while parent is not None:
        parents.append(parent)
        if parent is window:
            break
        parent = parent.parentWidget()
    for parent in reversed(parents):
        if isinstance(parent, QTabWidget):
            for index in range(parent.count()):
                page = parent.widget(index)
                if page is control or page.isAncestorOf(control):
                    parent.setCurrentIndex(index)
                    break
    app.processEvents()
    scrolls = []
    for parent in parents:
        if isinstance(parent, QScrollArea) and parent.widget() is not None and parent.widget().isAncestorOf(control):
            parent.ensureWidgetVisible(control, 4, 4)
            app.processEvents()
            scrolls.append({'vertical': parent.verticalScrollBar().value(),
                'vertical_max': parent.verticalScrollBar().maximum(),
                'horizontal': parent.horizontalScrollBar().value(),
                'horizontal_max': parent.horizontalScrollBar().maximum()})
    return scrolls


def measure_button(control, window, viewport, app):
    """A scrolled control passes only when its whole button can be reached."""
    scrolls = reveal_control(control, window, app)
    text = control.text().replace('&', '').strip()
    name = text or control.accessibleName().strip() or control.toolTip().strip()
    top_left = control.mapTo(window, QPoint(0, 0))
    geometry = control.rect().translated(top_left)
    visible = control.visibleRegion().boundingRect()
    minimum_text_width = control.fontMetrics().horizontalAdvance(text) + (12 if text else 0)
    if not control.icon().isNull():
        minimum_text_width += control.iconSize().width() + 4
    visible_to_window = control.isVisibleTo(window) and control.isVisible()
    reachable = (visible_to_window and control.width() > 0 and control.height() > 0
        and visible.width() >= control.width() - 2 and visible.height() >= control.height() - 2
        and geometry.left() >= 0 and geometry.top() >= 0
        and geometry.right() < viewport[0] and geometry.bottom() < viewport[1])
    legible = bool(name) and (not text or minimum_text_width <= control.width() + 2)
    return {'label': name, 'status': 'passed' if reachable and legible else 'failed',
        'reason': 'Label and full button are reachable after any required scrolling.' if reachable and legible
            else 'The required action is hidden, clipped, outside the requested viewport or has an unreadable/blank label.',
        'geometry': _rect(geometry), 'visible_region': _rect(visible),
        'minimum_text_width': minimum_text_width, 'enabled': control.isEnabled(),
        'visible': visible_to_window, 'scrolls': scrolls}


def documentation_comparison(root=ROOT):
    """Inventory existing claims for human comparison; do not certify prose."""
    result = []
    for relative in ('docs/FEATURES.md', 'docs/PLATFORMS.md'):
        path = root / relative
        if not path.is_file():
            result.append({'source': relative, 'status': 'missing', 'sections': [], 'capabilities': []})
            continue
        lines = path.read_text(encoding='utf-8').splitlines()
        headings = [line.lstrip('#').strip() for line in lines if line.startswith('#')]
        capabilities = [line.split('|')[1].strip() for line in lines
                        if line.startswith('|') and len(line.split('|')) >= 4
                        and line.split('|')[1].strip() not in ('---', 'Capability', 'Feature')]
        result.append({'source': relative, 'status': 'manual_comparison_required',
            'sections': headings, 'capabilities': capabilities,
            'reason': 'Documentation inventory only. Compare native limits with the exercised fixture paths and manual-review gaps; prose matching is not proof of support.'})
    return result


class CriticalReview:
    def __init__(self, output, *, system=None):
        self.output = Path(output).absolute()
        _ordinary_data_path(self.output)
        self.output.mkdir(parents=True, exist_ok=True)
        self.output = self.output.resolve()
        self.system = system or sys.platform
        self.app = QApplication.instance() or QApplication([])
        self._previous_quit, self._previous_font = self.app.quitOnLastWindowClosed(), self.app.font()
        self._font_ids = []
        self.app.setQuitOnLastWindowClosed(False)
        # Qt's Windows offscreen plugin can expose no system font database.
        # Register installed fonts in this process, without downloading or
        # installing anything. Missing glyphs are review-environment failures.
        loaded_fonts = []
        if sys.platform == 'win32' and self.app.platformName() == 'offscreen':
            fonts = Path(os.environ.get('WINDIR', r'C:\Windows')) / 'Fonts'
            for name in ('segoeui.ttf', 'segoeuib.ttf', 'seguisb.ttf', 'arial.ttf'):
                candidate = fonts / name
                if candidate.is_file():
                    identity = QFontDatabase.addApplicationFont(str(candidate))
                    if identity >= 0:
                        self._font_ids.append(identity)
                        loaded_fonts.append(name)
            self.app.setFont(QFont('Segoe UI', 10))
        elif not QFontMetrics(self.app.font()).inFont('A'):
            candidates = (('/System/Library/Fonts/Helvetica.ttc',
                           '/System/Library/Fonts/SFNS.ttf',
                           '/System/Library/Fonts/Supplemental/Arial.ttf') if sys.platform == 'darwin' else
                          ('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
                           '/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf'))
            for value in candidates:
                candidate = Path(value)
                if not candidate.is_file():
                    continue
                identity = QFontDatabase.addApplicationFont(str(candidate))
                families = QFontDatabase.applicationFontFamilies(identity) if identity >= 0 else []
                if families:
                    self._font_ids.append(identity)
                    loaded_fonts.append(candidate.name)
                    self.app.setFont(QFont(families[0], 10))
                    if QFontMetrics(self.app.font()).inFont('A'):
                        break
        self.report = {'schema': 1, 'review_kind': 'deterministic fixture UX/workflow checks',
            'platform': self.system, 'runtime_platform': sys.platform, 'qt_platform': self.app.platformName(),
            'review_fonts_registered': loaded_fonts,
            'viewports': [list(value) for value in VIEWPORTS], 'host_maintenance_applied': False,
            'coverage_complete': False, 'checks': [], 'screenshots': [],
            'manual_review': copy.deepcopy(MANUAL_GAPS),
            'skipped_checks': [{'area': item['area'], 'status': 'not_exercised', 'reason': item['review']}
                               for item in MANUAL_GAPS],
            'documentation_comparison': documentation_comparison(),
            'limitations': 'Rule-based geometry and selected fixture transitions only. This is not AI review, a performance benchmark, exhaustive bug detection or verification of real operating-system changes.'}
        metrics = QFontMetrics(self.app.font())
        self.check('review-environment.font-glyphs', all(metrics.inFont(character) for character in 'Optimus Yes No'),
            'The review renderer must have real glyphs before text geometry is meaningful.')
        # An offscreen plugin commonly exposes an artificial 800x800 screen.
        # Exercise the real fitter against an explicit virtual desktop so the
        # requested client viewports stay exact; small-screen fitting has its
        # own independent regression fixtures. No production bypass is added.
        self._geometry_override = patch('optimus.widgets.window_geometry.available_screen_geometry',
                                        return_value=QRect(0, 0, 1920, 1080))
        self._geometry_override.start()
        self.report['fixture_available_screen'] = [0, 0, 1920, 1080]

    def close(self):
        if self._geometry_override is not None:
            self._geometry_override.stop()
            self._geometry_override = None
        self.app.setQuitOnLastWindowClosed(self._previous_quit)
        self.app.setFont(self._previous_font)
        for identity in self._font_ids:
            QFontDatabase.removeApplicationFont(identity)
        self._font_ids.clear()

    def check(self, identity, passed, reason, *, evidence=None, severity='critical'):
        item = {'id': identity, 'status': 'passed' if passed else 'failed',
                'severity': severity, 'reason': reason}
        if evidence is not None:
            item['evidence'] = evidence
        self.report['checks'].append(item)
        return passed

    def viewport(self, widget, dimensions, identity):
        widget.setAttribute(Qt.WA_DontShowOnScreen, True)
        widget.resize(*dimensions)
        widget.show()
        drain(self.app)
        # Initial owner-relative fitting remains active. Then exercise a real
        # user resize to this particular fixture viewport before measuring it.
        widget.resize(*dimensions)
        drain(self.app)
        actual = [widget.width(), widget.height()]
        contained = actual[0] <= dimensions[0] and actual[1] <= dimensions[1]
        exact = actual == list(dimensions)
        self.check(identity + '.viewport', contained if isinstance(widget, QMessageBox) else exact,
            'The requested client viewport must contain the complete window; an oversized minimum is a usability failure.',
            evidence={'requested': list(dimensions), 'actual': actual,
                      'minimum': [widget.minimumWidth(), widget.minimumHeight()],
                      'device_pixel_ratio': widget.devicePixelRatioF()})

    def buttons(self, widget, dimensions, identity, controls):
        for index, control in enumerate(controls):
            measured = measure_button(control, widget, dimensions, self.app)
            self.check(identity + f'.button-{index}', measured['status'] == 'passed',
                       measured['reason'], evidence=measured)

    def screenshot(self, widget, name):
        path = self.output / (name + '.png')
        _ordinary_data_path(path, file=True)
        if not widget.grab().save(str(path)):
            raise RuntimeError('A review screenshot could not be saved.')
        self.report['screenshots'].append({'name': name, 'file': path.name,
            'logical_size': [widget.width(), widget.height()],
            'device_pixel_ratio': widget.devicePixelRatioF()})

    def dispose(self, window):
        settings = getattr(window, '_settings_window', None)
        if settings is not None:
            settings.speed_enhancements_panel.shutdown()
            settings.pool.waitForDone(5000)
            drain(self.app)
            settings.busy = False
            settings.close()
            settings.deleteLater()
        window._cancel.set()
        window.pool.waitForDone(5000)
        drain(self.app)
        window._pending_post_review = window._pending_auto_fixes = None
        window._post_review_active = False
        window._quitting = True
        if window._progress_window is not None:
            window._progress_window.completed = True
            window._progress_window.close()
        window.shutdown()
        window.close()
        window.deleteLater()
        drain(self.app)

    def review_setup(self, backend, dimensions, prefix):
        factory = InitialSetupWizard if self.system == 'win32' else NativeSetupWizard
        wizard = factory(backend, preferences=backend.preferences(), first_run=False)
        try:
            self.viewport(wizard, dimensions, prefix + '.setup')
            original = backend.preferences()
            pages = []
            for index, page_id in enumerate(wizard.pageIds()):
                # QWizard owns navigation and page initialization; next() uses
                # real validation without ever accepting/saving the last page.
                if index:
                    wizard.next()
                    drain(self.app)
                current = wizard.currentId()
                self.check(prefix + f'.setup.page-{index}', current == page_id,
                    'Setup pages must be reachable through normal Next navigation.',
                    evidence={'expected': page_id, 'observed': current})
                if current != page_id:
                    break
                pages.append(current)
                controls = [wizard.button(which) for which in (QWizard.BackButton, QWizard.NextButton,
                    QWizard.FinishButton, QWizard.CancelButton) if wizard.button(which).isVisible()]
                self.buttons(wizard, dimensions, prefix + f'.setup.page-{index}', controls)
                # Explicit inventory buttons inside scroll content must be
                # reachable, but are never clicked against a real backend.
                body_buttons = [button for button in wizard.currentPage().findChildren(QAbstractButton)
                                if button.isVisibleTo(wizard) and button.inherits('QPushButton')]
                self.buttons(wizard, dimensions, prefix + f'.setup.content-{index}', body_buttons)
                if index in (0, len(wizard.pageIds()) - 1):
                    self.screenshot(wizard, prefix + f'-setup-{index}')
            self.check(prefix + '.setup.no-implicit-save', not backend.saved and backend.preferences() == original,
                'Navigating setup must not save or run maintenance before Save choices.')
        finally:
            wizard.reject()
            wizard.deleteLater()
            drain(self.app)

    def review_navigation(self, settings, dimensions, prefix):
        for name, page in settings.pages.items():
            settings.select_page(name, load=False)
            drain(self.app)
            selected = (settings.tabs.currentIndex() == page if isinstance(page, int)
                        else settings.stack.currentWidget() is page)
            self.check(prefix + '.navigation.' + name, selected,
                'The settings navigation route must reach its corresponding page without invoking a backend operation.')
        settings.select_page('Settings', load=False)
        if hasattr(settings, 'navigation'):
            self.buttons(settings, dimensions, prefix + '.navigation', list(settings.navigation.values()))

    def review_speed(self, window, backend, dimensions, prefix):
        window.open_settings(page='Settings')
        settings = window._settings_window
        self.viewport(settings, dimensions, prefix + '.settings')
        self.review_navigation(settings, dimensions, prefix)
        panel = settings.speed_enhancements_panel
        panel.window.smoke_test = False  # Writes still target in-memory fixtures only.
        self.buttons(settings, dimensions, prefix + '.speed', [panel.save_button, panel.review_button,
            panel.apply_button, panel.history_button, panel.restore_button])
        self.check(prefix + '.speed.no-implicit-actions', backend.apply_speed_enhancements.call_count == 0
            and backend.speed_enhancements_scan.call_count == 0,
            'Opening Settings and changing pages must not apply or scan speed policies automatically.')
        self.screenshot(settings, prefix + '-speed-controls')
        panel.sync_settings({'speed_enhancements_enabled': False})
        panel.enabled.setChecked(True)
        panel.apply_now()
        self.check(prefix + '.speed.unsaved-approval', not panel.apply_button.isEnabled()
            and backend.apply_speed_enhancements.call_count == 0 and 'save' in panel.status.text().lower(),
            'An unsaved enabled checkbox must not authorize Apply now.')
        panel.restore_selected()
        self.check(prefix + '.speed.no-empty-restore', not panel.restore_button.isEnabled()
            and backend.restore_speed_enhancements.call_count == 0,
            'Restore must require an exact recorded selection.')
        panel.sync_settings({'speed_enhancements_enabled': True})
        entered = threading.Event()

        def apply(*, progress_callback, cancel_event):
            entered.set()
            progress_callback({'percentage': 40, 'description': 'Fixture policy waits for cancellation.'})
            if not cancel_event.wait(10):
                raise RuntimeError('Fixture cancellation was not delivered.')
            return dict(speed_report('cancelled'), summary='Cancelled; 0 applied; 1 deferred.')

        backend.apply_speed_enhancements.side_effect = apply
        panel.apply_now()
        drain(self.app, entered.is_set)
        self.check(prefix + '.speed.busy-controls', settings.busy and not panel.apply_button.isEnabled()
            and not panel.enabled.isEnabled() and panel.cancel_button.isVisible(),
            'An active speed operation must prevent duplicate writes and expose cancellation.')
        panel.apply_now()
        self.check(prefix + '.speed.single-worker', backend.apply_speed_enhancements.call_count == 1,
            'Repeated Apply requests must not create concurrent workers.')
        self.check(prefix + '.speed.close-guard', settings.close() is False,
            'Settings must retain the active operation when Close is requested.')
        panel.cancel_button.click()
        drain(self.app, lambda: not settings.busy)
        self.check(prefix + '.speed.cancel-completes', panel._cancel.is_set() and panel.cancel_button.isHidden()
            and panel.apply_button.isEnabled(), 'Cancellation must return the panel to an usable idle state.')
        panel._show_report(speed_report())
        shown = panel.status.text() + '\n' + panel.details.toPlainText()
        self.check(prefix + '.speed.deferred-honesty', '0 applied' in panel.status.text()
            and '1 deferred' in panel.status.text() and 'unavailable' in shown
            and not panel.restore_button.isEnabled(),
            'A deferred fixture policy must remain deferred, with no fabricated applied enhancement or restore authority.')
        self.screenshot(settings, prefix + '-speed-deferred')
        settings.close()

    def review_launch(self, window, backend, dimensions, prefix):
        """Render the real launch dialog; Adjust is the only simulated choice."""
        viewport = (480, 400) if dimensions == VIEWPORTS[0] else (760, 660)
        saved = backend.preferences()
        saved.update(cleanup_enabled=True, cleanup_age_days=0, m365_debloat_enabled=True,
            consumer_debloat_enabled=True, consumer_debloat_package_names=['Microsoft.Copilot'],
            browser_cookies_enabled=True, browser_history_enabled=True,
            browser_cleanup_browsers=['chrome', 'opera'], browser_keep_domains=['example.com'],
            privacy_disable_clipboard_sync_enabled=True, privacy_disable_recall_snapshots_enabled=True)
        original = backend.preferences()

        def inspect(dialog):
            self.viewport(dialog, viewport, prefix + '.launch-review')
            controls = {button.text(): button for button in dialog.findChildren(QPushButton)}
            self.buttons(dialog, viewport, prefix + '.launch-review', list(controls.values()))
            negative, positive = controls.get('Adjust settings'), controls.get('Auto Clean')
            self.check(prefix + '.launch-review.negative-default', negative is not None
                and positive is not None and negative.isDefault() and not positive.autoDefault(),
                'The actual per-launch warning must visibly offer Auto Clean and default to Adjust settings.')
            text = dialog.findChild(QPlainTextEdit)
            shown = text.toPlainText() if text is not None else ''
            self.check(prefix + '.launch-review.actual-warning', text is not None and text.isReadOnly()
                and 'Microsoft 365 hub/companion' in shown and 'Microsoft.Copilot' in shown
                and 'cookies' in shown and 'sign you out' in shown and 'example.com' in shown
                and 'permanently deletes previous Recall snapshots' in shown
                and 'cannot recover deleted snapshots' in shown
                and 'local clipboard history is unchanged' in shown,
                'Real saved-action text must disclose selected app removals, browser sign-out and irreversible Recall snapshot deletion.')
            self.check(prefix + '.launch-review.scroll-access', text is not None
                and text.height() > 80 and (viewport[0] > 480 or text.verticalScrollBar().maximum() > 0),
                'A narrow review must keep the action buttons visible while its complete warning remains scrollable.')
            self.screenshot(dialog, prefix + '-launch-review')
            if negative is not None:
                negative.click()
            return QDialog.Accepted

        with patch('PySide6.QtWidgets.QDialog.exec', inspect):
            choice = ask_launch_review(window, saved)
        self.check(prefix + '.launch-review.no-implicit-actions', choice == 'adjust_settings'
            and backend.preferences() == original and not backend.analysis_start.called
            and not backend.optimize.called
            and not any(getattr(backend, name).called for name in OPTIONAL_OPERATIONS),
            'Rendering and choosing Adjust must not save preferences, collect data, start cleanup or activate optional services.')

    def review_optional_tools(self, window, backend, dimensions, prefix):
        """Inspect production panels with inert services and an unsupported backend."""
        window.open_settings(page='Settings')
        settings = window._settings_window
        self.viewport(settings, dimensions, prefix + '.optional-settings')
        self.check(prefix + '.optional.no-constructor-actions',
            not any(getattr(backend, name).called for name in OPTIONAL_OPERATIONS),
            'Constructing Smart Cleaning, Recovery and File utilities panels must not scan, save, start monitors or mutate files.')
        for page, attribute, controls in (
            ('Smart cleaning', 'smart_maintenance_panel', ('save_button', 'preview_button', 'pause_button')),
            ('File utilities', 'file_utilities_panel', ('encrypt_button', 'decrypt_button', 'split_button', 'join_button')),
            ('Recovery', 'recovery_panel', ('driver_refresh', 'driver_export', 'driver_rollback', 'backup_verify',
                                           'restore_refresh', 'restore_create', 'restore_delete', 'restore_open'))):
            settings.select_page(page, load=False)
            panel = getattr(settings, attribute)
            self.buttons(settings, dimensions, prefix + '.optional.' + attribute,
                [getattr(panel, name) for name in controls])
            self.screenshot(settings, prefix + '-' + attribute.replace('_', '-'))
        smart = settings.smart_maintenance_panel
        self.check(prefix + '.optional.smart-stopped-honesty', 'stopped' in smart.summary.text().lower()
            and not backend.activate_smart_maintenance.called and not backend.configure_smart_maintenance.called,
            'The Smart panel must report stopped fixture monitoring without activating or saving it.')
        files = settings.file_utilities_panel
        self.check(prefix + '.optional.file-explicit-selection', files._plan is None
            and not files.overwrite_button.isEnabled() and 'No files have been inspected or changed' in files.status.text(),
            'File utilities must start without a file selection, overwrite plan or fabricated completed action.')
        unsupported = RecoveryPanel(SimpleNamespace(backend=SimpleNamespace(), busy=False, smoke_test=False))
        try:
            self.viewport(unsupported, dimensions, prefix + '.recovery-unsupported')
            controls = (unsupported.driver_refresh, unsupported.driver_export, unsupported.driver_rollback,
                unsupported.backup_verify, unsupported.restore_refresh, unsupported.restore_create,
                unsupported.restore_delete, unsupported.restore_open)
            self.check(prefix + '.optional.recovery-unsupported-disabled', all(not control.isEnabled() for control in controls)
                and unsupported._driver_plan is None and unsupported._restore_plan is None,
                'A backend with no recovery APIs must expose no enabled mutation or inventory operation.')
            unsupported.refresh_drivers()
            self.check(prefix + '.optional.recovery-unsupported-honesty', 'unavailable' in unsupported.status.text().lower()
                and 'No operation was started' in unsupported.status.text(),
                'An unsupported direct request must report unavailable and never pretend recovery completed.')
            self.screenshot(unsupported, prefix + '-recovery-unsupported')
        finally:
            unsupported.shutdown()
            unsupported.close()
            unsupported.deleteLater()
            drain(self.app)
        self.check(prefix + '.optional.no-navigation-actions',
            not any(getattr(backend, name).called for name in OPTIONAL_OPERATIONS),
            'Reviewing optional pages and disabled controls must leave every operation counter at zero.')
        settings.close()

    def review_additional_workers(self, window, backend, dimensions, prefix):
        """Exercise production polling/reflow with named cached fixture workers."""
        jobs = [
            {'id': 'driver_backup', 'title': 'Driver backup', 'status': 'running',
             'description': 'Hashing the exported fixture driver packages', 'current': None, 'total': None},
            {'id': 'system_restore', 'title': 'System Restore', 'status': 'needs_review',
             'description': 'Fixture provider unavailable; no restore point was created', 'current': None, 'total': None},
            {'id': 'file_tools', 'title': 'Verify file parts', 'status': 'completed',
             'description': 'Two disposable fixture parts matched their manifest', 'current': 2, 'total': 2}]
        progress = OptimizationProgressWindow(window, threading.Event())
        original = backend.worker_status.return_value
        backend.worker_status.return_value = jobs
        try:
            progress.update_progress({'stage': 'Inspect current resource usage', 'percentage': 10,
                'description': 'Production status cards are displaying cached fixtures only.', 'jobs': []})
            progress._refresh_worker_elapsed()
            self.viewport(progress, dimensions, prefix + '.additional-workers')
            cards = progress.worker_cards
            self.check(prefix + '.additional-workers.actual-poll', len(cards) == 6
                and all(identity in cards for identity in ('driver_backup', 'system_restore', 'file_tools'))
                and backend.worker_status.called,
                'The real progress-window status poll must add actual named optional workers to its three maintenance/update cards.')
            self.check(prefix + '.additional-workers.count-honesty',
                'No item count' in cards['driver_backup'].progress.format()
                and 'No item count' in cards['system_restore'].progress.format()
                and cards['system_restore'].status.text() == 'Needs review'
                and cards['file_tools'].progress.maximum() == 2 and cards['file_tools'].progress.value() == 2,
                'Unknown item counts and unavailable recovery remain explicit; only the fixture with a real count shows 2/2.')
            grid = progress.worker_grid
            for width, expected in ((440, 1), (1000, 3)):
                grid.resize(width, grid.height())
                grid.reflow(force=True)
                self.check(prefix + f'.additional-workers.reflow-{expected}', grid._columns == expected
                    and grid.grid.getItemPosition(3)[:2] == (3 // expected, 3 % expected),
                    'Additional worker cards must reflow through the production grid, without opening another window.',
                    evidence={'requested_grid_width': width, 'columns': grid._columns,
                              'fourth_card_position': list(grid.grid.getItemPosition(3))})
            drain(self.app)

            self.screenshot(progress, prefix + '-additional-workers')
            settings = window._settings_window
            settings.select_page('Workers', load=False)
            board = settings.worker_board
            board.refresh()
            self.viewport(settings, dimensions, prefix + '.worker-board')
            self.check(prefix + '.additional-workers.board-poll', set(board.grid.cards) == {job['id'] for job in jobs}
                and 'Needs review' == board.grid.cards['system_restore'].status.text(),
                'The Workers settings page must display the same cached optional workers without starting operations.')
            self.screenshot(settings, prefix + '-worker-board')
            settings.close()
            self.check(prefix + '.additional-workers.no-operation',
                not any(getattr(backend, name).called for name in OPTIONAL_OPERATIONS),
                'Polling worker status and resizing cards must never dispatch an optional operation.')
        finally:
            backend.worker_status.return_value = original
            progress.completed = True
            progress.spinner.stop()
            progress.close()
            progress.deleteLater()
            drain(self.app)

    def review_worker_controls_and_metrics(self, window, backend, dimensions, prefix):
        """Production navigation plus cached metric payloads; no live sampler."""
        hub, cursors = backend.navigation, {}
        hub.begin('c' * 32)
        for identity, title in (('maintenance', 'Maintenance'), ('software_updates', 'App updates'),
                                ('driver_updates', 'Driver updates')):
            cursor = hub.cursor(identity, title)
            cursor.begin_item(identity + ':previous', title + ' prior fixture check', kind='check')
            cursor.finish('completed', mutation_started=False, outcome_known=True)
            cursor.begin_item(identity + ':current', title + ' current fixture item', kind='mutation')
            cursors[identity] = cursor
        progress = OptimizationProgressWindow(window, threading.Event())
        cached = SimpleNamespace(start=Mock(), stop=Mock(), status=Mock(return_value={
            'computer': {'cpu_percent': 73.5, 'cpu_frequency_mhz': 3200, 'memory_percent': 42.},
            'network': {'kind': 'wifi', 'adapter': 'Fixture radio', 'receive_mbps': 8., 'send_mbps': 1.,
                'link_mbps': 866., 'is_up': True, 'internet_state': 'unverified',
                'reason': 'Fixture passive adapter traffic. Internet capacity is unverified.'},
            'workers': {'maintenance': {'cpu_percent': 2.5, 'reason': 'Fixture Optimus thread only.'},
                'software_updates': {'cpu_percent': 0., 'reason': 'Fixture thread waiting on an external installer.'},
                'driver_updates': {'cpu_percent': None, 'reason': 'No observed thread interval yet.'}}}))
        try:
            self.viewport(progress, dimensions, prefix + '.worker-controls')
            self.check(prefix + '.worker-controls.no-live-sampler', progress._live_metrics_service is None,
                'Smoke-test progress must not construct or start a machine metrics sampler.')
            progress._live_metrics_service = cached
            progress.update_progress({'stage': 'Fixture maintenance', 'percentage': 37, 'stage_status': 'running',
                'jobs': [{'id': 'software_updates', 'status': 'installing', 'current': 1, 'total': 4,
                          'description': 'Fixture installer owns the update lane.'},
                         {'id': 'driver_updates', 'status': 'waiting_for_installer', 'current': 0, 'total': 2,
                          'description': 'Waiting for the fixture app installer.'}]})
            progress._update_live_metrics()
            progress.worker_navigation.refresh()
            cards = progress.worker_cards
            self.check(prefix + '.worker-controls.three-controls', all(set(cards[identity].navigation_buttons)
                == {'back', 'next', 'cancel'} and all(button.isEnabled() and button.accessibleName()
                    for button in cards[identity].navigation_buttons.values()) for identity in cursors),
                'Each of the three live workers must expose labelled Back, Next and Cancel controls.')
            self.buttons(progress, dimensions, prefix + '.worker-controls',
                [button for identity in cursors for button in cards[identity].navigation_buttons.values()])
            self.check(prefix + '.metrics.measured-values', cards['maintenance'].activity_ring.cpu_percent == 2.5
                and cards['software_updates'].activity_ring.cpu_percent == 0
                and cards['driver_updates'].activity_ring.cpu_percent is None
                and '37%' in cards['maintenance'].completion.text()
                and cards['software_updates'].activity_ring.health != 'error'
                and cards['driver_updates'].activity_ring.health == 'waiting',
                'CPU rings must retain measured zero versus unavailable values, separate progress, and a waiting state without inventing an error.')
            strip = progress.live_system_status
            self.check(prefix + '.metrics.system-network', all(text in strip.computer.text() for text in ('73.5%', '42.0%', '3.20 GHz'))
                and all(text in strip.network.text() for text in ('Wi-Fi', '8.00 Mbps', '866 Mbps', 'unverified')),
                'The computer/network strip must show cached CPU, RAM, clock, passive Mbps and explicit Internet uncertainty.')
            progress.content_scroll.verticalScrollBar().setValue(0)
            drain(self.app)
            self.screenshot(progress, prefix + '-worker-cpu-network')
            stale = dict(cards['software_updates']._navigation_state)
            cursors['software_updates'].phase('waiting_for_installer')
            progress.worker_navigation.request('software_updates', 'next', stale)
            self.check(prefix + '.worker-controls.stale-revision', cursors['software_updates'].pending is None
                and backend.navigation_requests[-1][2] == stale['revision'],
                'A stale click must submit its old revision and be rejected instead of retargeting the new item phase.')
            cards['software_updates'].navigation_buttons['back'].click()
            self.check(prefix + '.worker-controls.history-only', cursors['software_updates'].view_index is not None
                and not cursors['software_updates'].local_requested()
                and 'prior fixture check' in cards['software_updates'].navigation_note.text(),
                'Back must show the recorded prior item without rerunning or undoing its changes.')
            self.screenshot(progress, prefix + '-worker-history')
            cards['software_updates'].navigation_buttons['next'].click()
            self.check(prefix + '.worker-controls.history-forward', cursors['software_updates'].view_index is None
                and not cursors['software_updates'].local_requested(),
                'Next while viewing history must return to the live item without cancelling it.')
            cards['software_updates'].navigation_buttons['next'].click()
            self.check(prefix + '.worker-controls.pending-local', cursors['software_updates'].local_requested()
                and cursors['software_updates'].pending['item_key'] == 'software_updates:current'
                and all(not button.isEnabled() for button in cards['software_updates'].navigation_buttons.values())
                and cards['driver_updates'].navigation_buttons['next'].isEnabled() and not progress.cancel.is_set(),
                'Next must bind to the exact current app item, disable duplicate requests, and leave other workers/global cancellation unchanged.')
            self.screenshot(progress, prefix + '-worker-skip-pending')
            old_session = dict(cards['driver_updates']._navigation_state)
            hub.end('completed')
            hub.begin('d' * 32)
            hub.cursor('driver_updates').begin_item('new-run', 'Different workflow item')
            progress.worker_navigation.request('driver_updates', 'cancel', old_session)
            self.check(prefix + '.worker-controls.stale-session', not hub.cursor('driver_updates').local_requested()
                and backend.navigation_requests[-1][0] == old_session['session_id'],
                'A command captured in an old session must never cancel a different workflow.')
            self.check(prefix + '.metrics.fixture-only', not cached.start.called and cached.status.called
                and not backend.optimize.called and not backend.apply_available_fixes.called,
                'Review metrics must use only the injected cached payload; navigation and rendering must not start maintenance or a live sampler.')
        finally:
            hub.end('completed')
            progress.completed = True
            progress._worker_elapsed_timer.stop()
            progress.close()
            progress.deleteLater()
            drain(self.app)

    def review_app_rules(self, backend, dimensions, prefix):
        from optimus.widgets.app_rules_editor import AppRulesEditor, UPDATE_CHOICES, PRESENCE_CHOICES
        from optimus.widgets.theme import build_qss
        from PySide6.QtWidgets import QLabel, QVBoxLayout
        original = backend.preferences()
        settings = dict(original, software_updates_enabled=False, software_app_rules=[])
        dialog = QDialog()
        dialog.setStyleSheet(build_qss('cyber'))
        body = QVBoxLayout(dialog)
        editor = AppRulesEditor(settings, native=self.system != 'win32', parent=dialog)
        body.addWidget(editor)
        try:
            self.viewport(dialog, dimensions, prefix + '.app-rules')
            self.check(prefix + '.app-rules.exact-choices', {value for _, value in UPDATE_CHOICES}
                == {'follow_global', 'always_update', 'always_ignore'} and {value for _, value in PRESENCE_CHOICES}
                == {'keep_current', 'keep_installed', 'keep_uninstalled'},
                'Per-app update and installed-state choices must be explicit and separate.')
            source, identity = ('winget', 'Fixture.Reader') if self.system == 'win32' else ('apt', 'apt:fixture-reader')
            editor.set_candidates([{'id': identity, 'source': source, 'name': 'Disposable fixture reader'}])
            editor.table.selectRow(0)
            editor.update_choice.setCurrentIndex(editor.update_choice.findData('always_update'))
            if self.system == 'win32':
                editor.presence_choice.setCurrentIndex(editor.presence_choice.findData('keep_uninstalled'))
            draft = editor.apply_to(settings)
            self.check(prefix + '.app-rules.master-off-draft', draft['software_updates_enabled'] is False
                and backend.preferences() == original and not backend.optimize.called
                and (draft['software_app_rules'][0]['update_policy'] == 'always_ignore' if self.system == 'win32'
                     else not editor.presence_choice.isEnabled()),
                'Editing is draft-only; the updater master remains off, uninstall implies ignore, and unsupported native presence is disabled.')
            notes = ' '.join(label.text() for label in editor.findChildren(QLabel)).casefold()
            self.check(prefix + '.app-rules.presence-warning', 'each maintenance run' in notes
                and 'app-local data' in notes and 'does not restore' in notes and 'agreements' in notes,
                'App rules must disclose repeated application, app-local data removal, limits of reinstall recovery and saved agreements.')
            self.screenshot(dialog, prefix + '-app-policy-choices')
        finally:
            dialog.close()
            dialog.deleteLater()
            drain(self.app)

    def review_revert(self, window, backend, dimensions, prefix):
        window.open_revert()  # Smoke construction never queues inventory.
        dialog = window._revert_dialog
        original = window.smoke_test
        window.smoke_test = False  # All hooks are still forbidden fixture mocks.
        try:
            dialog._set_inventory({'plan_id': 'a' * 32, 'complete': True, 'items': [
                {'selection_id': 'b' * 32, 'record_id': 'c' * 32, 'provider': 'change',
                 'kind': 'Startup', 'target': 'Disposable fixture registration', 'status': 'applied',
                 'undo_available': True, 'reason': 'Native restorer revalidates this record.'},
                {'selection_id': 'd' * 32, 'record_id': 'e' * 32, 'provider': 'history:runs',
                 'kind': 'Temporary-file deletion', 'target': 'Fixture receipt', 'status': 'completed',
                 'undo_available': False, 'reason': 'No universal automatic undo is available.'}]})
            dialog._controls()
            self.viewport(dialog, dimensions, prefix + '.revert')
            self.buttons(dialog, dimensions, prefix + '.revert',
                [button for button in dialog._buttons if button.isVisible()])
            self.check(prefix + '.revert.explicit-selection', dialog._selected() == []
                and dialog.revert_all_button.isEnabled() and not dialog.revert_selected_button.isEnabled()
                and dialog.close_button.isDefault(),
                'Revert must default to no selection and a non-destructive Close action; available does not mean already restored.')
            dialog.select_all()
            self.check(prefix + '.revert.supported-only', [row['selection_id'] for row in dialog._selected()] == ['b' * 32]
                and not dialog.table.item(1, 0).flags() & Qt.ItemIsUserCheckable,
                'Select all available must exclude the irreversible cleanup receipt.')
            dialog.clear_selection()
            self.screenshot(dialog, prefix + '-revert-history')
            self.check(prefix + '.revert.no-constructor-actions',
                not any(getattr(backend, name).called for name in OPTIONAL_OPERATIONS),
                'Constructing Revert, showing fixture history and selecting rows must never run a scan or undo operation.')
        finally:
            window.smoke_test = original
            dialog.close()

    def review_held_app_wait(self, window, dimensions, prefix):
        """A finished skipped app must not appear to own an active driver wait."""
        from optimus.parallel_updates import ProgressMux, update_completion_state, installer_barrier
        from optimus.update_holds import held_update_result
        from optimus.update_scan_evidence import with_app_scan
        package = {'id': 'Fixture.HeldEditor', 'version': '1.0', 'available': '1.1', 'source': 'winget'}
        hold = {'id': package['id'], 'available_version': '1.1',
                'reason': 'The reviewed fixture release had an inconsistent installer version.'}
        held = held_update_result(package, hold)
        result = with_app_scan({'scan': {'source': 'winget', 'exitCode': 0, 'structured': True,
                'updates': [package], 'source_reported_offers_count': 3,
                'excluded': ['Two fixture offers have unsupported version metadata.']},
            'items': [{'id': package['id'], 'status': 'skipped', 'result': held}],
            'failed_items': 2, 'held_count': 1, 'held_ids': [package['id']],
            'automatic_update_holds': [{'id': package['id'], 'available_version': '1.1', 'reason': held['note']}],
            'deferred_ids': [], 'stop_reason': '', 'installer_started': False,
            'outcomeKnown': True, 'running': False})
        progress = OptimizationProgressWindow(window, threading.Event())
        try:
            mux = ProgressMux(progress.update_progress)
            mux({'stage': 'Independent fixture maintenance', 'percentage': 44})
            state = update_completion_state(result)
            mux.background('needs_review', state)
            mux.background('installing', {'description': 'Install approved Fixture.Network driver',
                'current': 1, 'total': 3}, job_id='driver_updates')
            mux.wait_for_updates('Optimize eligible internal drives')
            self.viewport(progress, dimensions, prefix + '.held-app-finished')
            cards = progress.worker_cards
            app_card = cards['software_updates']
            self.check(prefix + '.held-app-finished.no-false-block',
                installer_barrier(result) is None and state.get('worker_finished') is True
                and state.get('continuation_blocked') is False
                and state.get('presentation_status') == 'finished_with_skips'
                and 'finished' in app_card.status.text().casefold()
                and 'skip' in app_card.status.text().casefold()
                and app_card.activity_ring.health == 'waiting',
                'A known held app must be shown as a finished skipped queue, not an active blocker or failed installer.')
            self.check(prefix + '.held-app-finished.coverage-honesty',
                result['app_scan']['complete'] is False and not result['app_scan']['no_more_offered']
                and 'incomplete' in app_card.status.text().casefold()
                and 'incomplete' in app_card.progress.format().casefold()
                and state.get('app_counts', {}).get('held') == 1
                and state.get('app_counts', {}).get('source_unverified') == 2,
                'A finished queue must preserve the incomplete search and two unverified source offers without claiming all apps are current.')
            self.check(prefix + '.held-app-finished.actual-driver-wait',
                'DRIVER' in progress.title.text() and 'driver' in cards['maintenance'].status.text().casefold()
                and 'Fixture.Network' in cards['maintenance'].details.text()
                and cards['driver_updates'].status.text() == 'Installing'
                and progress.progress.value() == 44 and not progress.completed,
                'Maintenance must identify the live driver installer while the skipped app stays finished and total maintenance remains incomplete.',
                evidence={'title': progress.title.text(), 'maintenance_status': cards['maintenance'].status.text(),
                    'maintenance_details': cards['maintenance'].details.text(),
                    'driver_status': cards['driver_updates'].status.text(), 'percentage': progress.progress.value()})
            self.buttons(progress, dimensions, prefix + '.held-app-finished', [progress.cancel_button])
            self.screenshot(progress, prefix + '-held-app-finished-driver-wait')
        finally:
            progress.completed = True
            progress.close()
            progress.deleteLater()
            drain(self.app)

    def review_update_wait(self, window, dimensions, prefix):
        """Replay the stale-stage regression without starting an updater."""
        cancel = threading.Event()
        progress = OptimizationProgressWindow(window, cancel)
        waiting = {'stage': 'Waiting for background application updates',
            'waiting_for': 'software_updates', 'percentage': 29,
            'description': ('Independent maintenance is complete. Application updates must finish '
                            'before conflicting maintenance and final verification.'),
            'processes': ['In-memory application update fixture: Fixture.App'],
            'jobs': [{'id': 'software_updates', 'status': 'waiting',
                'description': 'Updating approved package 2/7: Fixture.App (1.0 to 1.1)',
                'current': 1, 'total': 7, 'elapsed_seconds': 125},
                {'id': 'driver_updates', 'status': 'pending',
                 'description': 'Approved driver updates are queued behind app updates.',
                 'current': 0, 'total': 2, 'elapsed_seconds': 0}]}
        try:
            discovery = {'stage': 'Inspect current resource usage', 'maintenance_status': 'scanning',
                'description': 'Maintenance and both update workers have begun their independent discovery.', 'percentage': 8,
                'processes': ['In-memory maintenance discovery', 'In-memory application update scan', 'In-memory driver update scan'],
                'jobs': [{'id': 'software_updates', 'status': 'scanning', 'description': 'Read available application updates',
                          'current': 0, 'total': 0, 'elapsed_seconds': 3},
                         {'id': 'driver_updates', 'status': 'scanning', 'description': 'Read available driver updates',
                          'current': 0, 'total': 0, 'elapsed_seconds': 4}]}
            progress.update_progress(discovery)
            self.viewport(progress, dimensions, prefix + '.updates-concurrent')
            self.buttons(progress, dimensions, prefix + '.updates-concurrent', [progress.cancel_button])
            self.check(prefix + '.updates-concurrent.discovery',
                all(card.status.text() == 'Scanning' and card.isVisible()
                    for card in progress.worker_cards.values()) and progress.progress.value() == 8
                and 'application update scan' in progress.processes.toPlainText()
                and 'driver update scan' in progress.processes.toPlainText(),
                'All three worker cards must show concurrent discovery without claiming that installers have started.')
            self.screenshot(progress, prefix + '-concurrent-workers')
            maintenance_gate = dict(discovery, maintenance_status='waiting_for_preparation',
                maintenance_wait_reason='Waiting for driver preparation before temporary cleanup.',
                jobs=[{'id': 'software_updates', 'status': 'waiting_for_maintenance',
                       'description': 'Application update scope is prepared.', 'wait_reason': 'Temporary cleanup must finish first.',
                       'current': 0, 'total': 2, 'elapsed_seconds': 5},
                      {'id': 'driver_updates', 'status': 'preparing', 'description': 'Validate the approved driver scope.',
                       'current': 0, 'total': 1, 'elapsed_seconds': 5}])
            progress.update_progress(maintenance_gate)
            cards = progress.worker_cards
            self.check(prefix + '.updates-concurrent.maintenance-gate',
                cards['maintenance'].status.text() == 'Waiting for update preparation'
                and 'driver preparation' in cards['maintenance'].details.text()
                and cards['software_updates'].status.text() == 'Waiting for maintenance'
                and 'Temporary cleanup must finish first.' in cards['software_updates'].details.text()
                and cards['software_updates'].progress.value() == 0
                and cards['driver_updates'].status.text() == 'Preparing',
                'Prepared workers must name the maintenance dependency and preserve their real item counters while blocked.')
            installer_gate = dict(discovery, stage='Review installed applications', maintenance_status='running',
                jobs=[{'id': 'software_updates', 'status': 'installing', 'description': 'Install approved Fixture.App',
                       'current': 0, 'total': 2, 'elapsed_seconds': 7},
                      {'id': 'driver_updates', 'status': 'waiting_for_installer', 'description': 'Driver preparation is complete.',
                       'wait_reason': 'The application installer owns the mutation lane.',
                       'current': 0, 'total': 1, 'elapsed_seconds': 7}])
            progress.update_progress(installer_gate)
            self.check(prefix + '.updates-concurrent.installer-gate',
                cards['maintenance'].status.text() == 'Running'
                and cards['software_updates'].status.text() == 'Installing'
                and cards['driver_updates'].status.text() == 'Waiting for installer'
                and 'application installer owns' in cards['driver_updates'].details.text(),
                'A blocked driver worker must not claim Running or Installing while another installer owns the lane.')
            journal = r'D:\FixtureUser\Optimus\Data\parallel-updates\aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.json'
            unresolved = ('A previous update installer has an unresolved outcome; no automatic retry was started. '
                'Review installer state and the saved record before continuing: ' + journal)
            progress.update_progress({'stage': 'Apply saved privacy controls', 'percentage': 40,
                'description': 'The maintenance worker continues applying the saved privacy controls.',
                'jobs': [{'id': identity, 'status': 'deferred', 'description': unresolved,
                          'current': 0, 'total': 0, 'elapsed_seconds': 0}
                         for identity in ('software_updates', 'driver_updates')]})
            self.viewport(progress, dimensions, prefix + '.updates-deferred')
            self.buttons(progress, dimensions, prefix + '.updates-deferred', [progress.cancel_button])
            self.check(prefix + '.updates-deferred.compact-reason',
                all('Previous installer needs review.' in cards[identity].details.visible_text
                    and 'No automatic retry started.' in cards[identity].details.visible_text
                    and cards[identity].details.toolTip() == unresolved
                    and cards[identity].details.accessibleDescription() == unresolved
                    for identity in ('software_updates', 'driver_updates'))
                and progress.background_status.text().count(journal) == 1
                and journal not in progress.background_status.visible_text
                and progress.log.toPlainText().count(unresolved) == 1,
                'Shared unresolved-installer warnings must have short readable captions and preserve one full saved-record reason in the log.')
            self.check(prefix + '.updates-deferred.layout',
                all(card.details.geometry().bottom() < card.progress.geometry().top()
                    and card.progress.geometry().bottom() < card.elapsed.geometry().top()
                    and len(card.details.visible_text.splitlines()) <= 2
                    and all(card.details.fontMetrics().horizontalAdvance(line) <= card.details.contentsRect().width()
                            for line in card.details.visible_text.splitlines())
                    for card in cards.values()),
                'Long worker descriptions must stay within two lines and never overlap item counts or elapsed time.')
            self.check(prefix + '.updates-deferred.honest-state',
                all(cards[identity].status.text() == 'Deferred' and cards[identity].progress.value() == 0
                    for identity in ('software_updates', 'driver_updates'))
                and cards['maintenance'].status.text() == 'Running' and progress.progress.value() == 40,
                'A deferred updater must not claim Installing or block the independently running maintenance worker.')
            self.screenshot(progress, prefix + '-deferred-update-details')
            progress.update_progress({'stage': 'Apply saved privacy controls', 'percentage': 29,
                'description': 'One or more selected operations or scan checks failed.',
                'log': 'FAILED: Apply saved privacy controls: fixture result requires review.'})
            progress.update_progress(waiting)
            self.viewport(progress, dimensions, prefix + '.updates-waiting')
            self.buttons(progress, dimensions, prefix + '.updates-waiting', [progress.cancel_button])
            self.check(prefix + '.updates-waiting.explicit-state',
                progress.title.text() == 'WAITING FOR APPLICATION UPDATES'
                and progress.stage.text() == waiting['stage']
                and 'privacy' not in progress.description.text().casefold(),
                'Waiting for an updater must replace the previous failed privacy stage and its active description.',
                evidence={'title': progress.title.text(), 'stage': progress.stage.text(),
                          'description': progress.description.text()})
            job_text = progress.background_status.text()
            self.check(prefix + '.updates-waiting.current-worker',
                progress.background_status.isVisible() and 'Fixture.App' in job_text
                and '2/7' in job_text and '1/7 packages processed' in job_text
                and 'elapsed 2m 05s' in job_text
                and 'Fixture.App' in progress.processes.toPlainText(),
                'The waiting view must identify the current application, processed count and observed elapsed time.',
                evidence={'background': job_text, 'processes': progress.processes.toPlainText()})
            self.check(prefix + '.updates-waiting.progress-honesty',
                progress.progress.value() == 29 and progress.progress.value() < 100 and not progress.completed,
                'Waiting must retain completed-stage progress and must not claim full completion.',
                evidence={'percentage': progress.progress.value(), 'completed': progress.completed})
            self.check(prefix + '.updates-waiting.cancel-available',
                progress.cancel_button.isEnabled() and 'Cancel' in progress.cancel_button.text(),
                'An updater wait must keep cancellation available until the current update returns.')
            self.check(prefix + '.updates-waiting.failure-history',
                'FAILED: Apply saved privacy controls' in progress.log.toPlainText(),
                'Replacing a completed stage caption must preserve its failure evidence in the stage log.')
            cards = progress.worker_cards
            self.check(prefix + '.updates-waiting.three-worker-cards',
                [card.heading.text() for card in cards.values()] == ['Maintenance', 'App updates', 'Driver updates']
                and all(card.isVisible() and not card.visibleRegion().isEmpty() for card in cards.values())
                and cards['software_updates'].status.text().startswith('Running')
                and cards['driver_updates'].status.text() == 'Queued',
                'All three worker panels must remain visible and distinguish the running app installer from queued drivers.')
            self.screenshot(progress, prefix + '-updates-waiting')
            driver_wait = dict(waiting, stage='Waiting for background updates', waiting_for='installer_updates',
                active_job_id='driver_updates', next_stage='Check Windows security updates', percentage=57,
                description='The driver worker is still running. Conflicting maintenance and final verification wait for the update queue.',
                processes=['In-memory driver update fixture: Fixture GPU driver'],
                jobs=[{'id': 'software_updates', 'status': 'completed', 'description': 'Approved application updates returned.',
                       'current': 7, 'total': 7, 'elapsed_seconds': 145},
                      {'id': 'driver_updates', 'status': 'running', 'description': 'Installing approved Fixture GPU driver',
                       'current_package': 'Fixture GPU driver', 'current': 0, 'total': 2, 'elapsed_seconds': 12}])
            progress.update_progress(driver_wait)
            drain(self.app)
            self.check(prefix + '.updates-waiting.driver-handoff',
                progress.title.text() == 'WAITING FOR BACKGROUND UPDATES'
                and cards['maintenance'].status.text() == 'Waiting for update lane'
                and cards['software_updates'].status.text() == 'Completed'
                and cards['driver_updates'].status.text() == 'Running'
                and 'Fixture GPU driver' in cards['driver_updates'].details.text()
                and cards['driver_updates'].elapsed.text() == 'Elapsed: 12s'
                and cards['driver_updates'].progress.value() == 0
                and cards['driver_updates'].progress.maximum() == 2,
                'The same window must show application completion and the current driver installer without claiming simultaneous installers.')
            self.screenshot(progress, prefix + '-driver-updates-waiting')
            progress.cancel_button.click()
            progress.update_progress(driver_wait)
            self.check(prefix + '.updates-waiting.cancellation-stable',
                cancel.is_set() and not progress.cancel_button.isEnabled()
                and 'Cancellation requested.' in progress.description.text(),
                'Cancellation must remain visible when the updater sends a later heartbeat.')
            progress.update_progress({'stage': 'Verify maintenance outcomes', 'percentage': 85,
                'description': 'Fixture updater returned. No operating-system changes were performed.', 'jobs': []})
            self.check(prefix + '.updates-waiting.foreground-resumes',
                progress.title.text() == 'OPTIMIZATION IN PROGRESS'
                and progress.stage.text() == 'Verify maintenance outcomes'
                and progress.background_status.isHidden(),
                'A later foreground stage must replace the waiting title and remove the finished background display.')
        finally:
            progress.completed = True
            progress.spinner.stop()
            progress.close()
            progress.deleteLater()
            drain(self.app)

    def review_workflow(self, window, backend, dimensions, prefix):
        entered, release = threading.Event(), threading.Event()
        entered_batch, release_batch = threading.Event(), threading.Event()

        def optimize(settings, progress_callback, cancel_event, **unused):
            entered.set()
            progress_callback({'stage': 'Fixture inspection', 'percentage': 30,
                'description': 'No operating-system operation is being performed.', 'processes': ['In-memory fixture worker']})
            if not release.wait(10):
                raise RuntimeError('Fixture initial release timed out.')
            return workflow_report()

        def apply(parent, settings, *, progress_callback, cancel_event):
            entered_batch.set()
            progress_callback({'stage': 'Fixture correction review', 'percentage': 50,
                'description': 'The fixture correction is deferred; no host changes.', 'processes': ['In-memory fixture worker']})
            if not release_batch.wait(10):
                raise RuntimeError('Fixture follow-up release timed out.')
            return workflow_report('c' * 32, parent_run_id=parent['id'], status='completed_with_errors',
                counts={'completed': 0, 'deferred': 1}, summary='0 action/check completed; 1 deferred.')

        backend.optimize.side_effect, backend.apply_available_fixes.side_effect = optimize, apply
        window.smoke_test = False
        window._offer_post_optimization_reviews = Mock()
        try:
            window.main_buttons[0].click()
            drain(self.app, entered.is_set)
            progress = window._progress_window
            self.viewport(progress, dimensions, prefix + '.progress-active')
            self.buttons(progress, dimensions, prefix + '.progress-active', [progress.cancel_button])
            window.start_optimization()
            self.check(prefix + '.workflow.single-initial-worker', backend.optimize.call_count == 1
                and window.busy and not window.main_buttons[0].isEnabled(),
                'A repeated Optimize request must not launch a second active workflow.')
            release.set()
            drain(self.app, lambda: not window.busy)
            self.viewport(progress, dimensions, prefix + '.progress-results')
            self.buttons(progress, dimensions, prefix + '.progress-results',
                [progress.auto_continue_button, progress.cancel_button])
            self.check(prefix + '.workflow.explicit-auto-continue', progress.auto_continue_button.isEnabled()
                and not progress.auto_continue_button.isDefault() and not progress.auto_continue_button.autoDefault()
                and backend.apply_available_fixes.call_count == 0,
                'Auto continue must be available after completed results and require an explicit click.')
            self.check(prefix + '.workflow.unverified-honesty', 'Not fully verified' in progress.verification_summary.text()
                and '0 confirmed' in progress.verification_summary.text(),
                'An unverified fixture outcome must not display a verified-success summary.')
            self.screenshot(progress, prefix + '-completed-results')
            progress.auto_continue_button.click()
            drain(self.app, entered_batch.is_set)
            progress.autoContinueRequested.emit()
            window.start_optimization()
            self.check(prefix + '.workflow.single-follow-up-worker', backend.apply_available_fixes.call_count == 1
                and backend.optimize.call_count == 1 and window.busy,
                'Auto continue reentrancy must not repeat the batch or the original cleanup.')
            release_batch.set()
            drain(self.app, lambda: not window.busy)
            self.viewport(progress, dimensions, prefix + '.follow-up-results')
            self.buttons(progress, dimensions, prefix + '.follow-up-results', [progress.cancel_button])
            self.check(prefix + '.workflow.follow-up-deferred', '1 deferred' in progress.auto_continue_summary.text()
                and 'Not fully verified' in progress.verification_summary.text()
                and progress.auto_continue_button.isHidden(),
                'Deferred follow-up work must remain visible and the consumed batch must not rearm itself.')
            self.screenshot(progress, prefix + '-follow-up-results')
            progress.autoContinueRequested.emit()
            self.check(prefix + '.workflow.stale-request', backend.apply_available_fixes.call_count == 1,
                'A stale completed-result signal must not repeat applied or deferred work.')
            progress.cancel_button.click()
            drain(self.app, lambda: window._offer_post_optimization_reviews.called)
            self.check(prefix + '.workflow.manual-continue', window._offer_post_optimization_reviews.call_count == 1,
                'Manual recommendations must open only once after explicit Continue.')
        finally:
            release.set()
            release_batch.set()
            window.pool.waitForDone(5000)
            drain(self.app)

    def run(self):
        for dimensions in VIEWPORTS:
            prefix = f'{dimensions[0]}x{dimensions[1]}'
            backend = FixtureBackend(self.output / 'fixture-state' / prefix, self.system)
            window = None
            try:
                factory = OptimusWindow if self.system == 'win32' else NativeOptimusWindow
                window = factory(backend, smoke_test=True)
                self.viewport(window, dimensions, prefix + '.main')
                self.check(prefix + '.main.four-actions', len(window.main_buttons) == 4
                    and window.main_buttons[-1].text() == 'Revert',
                    'The home screen must offer Optimize, Settings, Sandbox and the requested Revert action.')
                self.buttons(window, dimensions, prefix + '.main', window.main_buttons)
                self.screenshot(window, prefix + '-main')
                self.review_launch(window, backend, dimensions, prefix)
                self.review_setup(backend, dimensions, prefix)
                self.review_speed(window, backend, dimensions, prefix)
                self.review_optional_tools(window, backend, dimensions, prefix)
                self.review_additional_workers(window, backend, dimensions, prefix)
                self.review_worker_controls_and_metrics(window, backend, dimensions, prefix)
                self.review_app_rules(backend, dimensions, prefix)
                self.review_revert(window, backend, dimensions, prefix)
                self.review_held_app_wait(window, dimensions, prefix)
                self.review_update_wait(window, dimensions, prefix)
                self.review_workflow(window, backend, dimensions, prefix)
                self.check(prefix + '.no-background-analysis', backend.analysis_start.call_count == 0,
                    'Fixture navigation must never resume background collection or automatic startup maintenance.')
            except Exception as error:
                self.check(prefix + '.scenario-exception', False,
                    'A fixture review could not finish: ' + type(error).__name__ + ': ' + str(error))
            finally:
                if window is not None:
                    self.dispose(window)
        question = make_yes_no_dialog(None, 'Fixture permission choice', 'Review this fixture action?')
        try:
            self.viewport(question, VIEWPORTS[0], 'confirmation')
            yes, no = question.button(QMessageBox.Yes), question.button(QMessageBox.No)
            self.buttons(question, VIEWPORTS[0], 'confirmation', [yes, no])
            self.check('confirmation.negative-default', question.defaultButton() is no
                and question.escapeButton() is no and yes.text().startswith('Yes') and no.text().startswith('No'),
                'Yes/No must be visibly labelled and No must remain the default and Escape action.')
        finally:
            question.close()
            question.deleteLater()
            drain(self.app)
        checks = self.report['checks']
        critical = sum(item['status'] == 'failed' and item['severity'] == 'critical' for item in checks)
        self.report['counts'] = {'passed': sum(item['status'] == 'passed' for item in checks),
            'failed': sum(item['status'] == 'failed' for item in checks), 'critical': critical,
            'manual_review_areas': len(MANUAL_GAPS)}
        self.report['status'] = 'failed' if critical else 'passed_with_manual_review'
        return self.report


def markdown_report(report):
    counts = report['counts']
    lines = ['# Optimus critical UX/workflow review', '',
        f"Result: **{report['status']}** on `{report['platform']}`. "
        f"{counts['passed']} checks passed; {counts['failed']} failed; {counts['critical']} critical.", '',
        report['limitations'], '', 'No real maintenance was applied. Fixture coverage is intentionally incomplete.', '',
        '## Findings', '']
    failed = [item for item in report['checks'] if item['status'] == 'failed']
    lines.extend([f"- **{item['severity']} — {item['id']}**: {item['reason']}" for item in failed]
                 or ['No failures were detected by the implemented rules. This does not establish that the app has no defects.'])
    lines += ['', '## Manual review still required', '']
    lines.extend(f"- **{item['area']}**: {item['review']}" for item in report['manual_review'])
    lines += ['', '## Evidence and capability claims', '',
        'JSON contains individual observations, geometry, scroll positions, disabled states and selected workflow transitions. '
        'Screenshots are fixture views. Existing feature/capability tables are inventoried for human comparison; their text is not certified by this bot.', '']
    lines.extend(f"- `{item['source']}`: {len(item['sections'])} sections; {len(item['capabilities'])} table entries; {item['status']}."
                 for item in report['documentation_comparison'])
    lines += ['', '## Screenshots', '']
    lines.extend(f"- [{item['name']}]({item['file']})" for item in report['screenshots'])
    return '\n'.join(lines) + '\n'


def github_summary(report):
    counts = report['counts']
    lines = ['### Critical fixture UX/workflow review', '',
        f"**{report['status']}**: {counts['passed']} passed, {counts['failed']} failed, {counts['critical']} critical.", '',
        'No host maintenance was applied. These are deterministic checks with incomplete fixture coverage.', '']
    lines.extend(f"- `{item['id']}`: {item['reason']}" for item in report['checks'] if item['status'] == 'failed')
    lines += ['', 'Download `Optimus-test-results` for this target and inspect `critical-review/critical-review.md`, '
              '`critical-review.json` and the screenshots. Manual review remains required for: '
              + ', '.join(item['area'] for item in report['manual_review']) + '.', '']
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='Explicit directory for fixture evidence and screenshots.')
    parser.add_argument('--github-summary', action='store_true', help='Also append the generated summary to the GitHub Actions summary file.')
    arguments = parser.parse_args(argv)
    summary_destination = None
    if arguments.github_summary:
        value = os.environ.get('GITHUB_STEP_SUMMARY')
        if not value:
            parser.error('--github-summary requires the GitHub Actions summary destination.')
        summary_destination = Path(value).absolute()
        _ordinary_data_path(summary_destination, file=True)
    reviewer = CriticalReview(arguments.output)
    try:
        report = reviewer.run()
    finally:
        reviewer.close()
    text = markdown_report(report)
    for name, content in (('critical-review.json', json.dumps(report, indent=2, ensure_ascii=False) + '\n'),
                          ('critical-review.md', text)):
        path = reviewer.output / name
        _ordinary_data_path(path, file=True)
        path.write_text(content, encoding='utf-8')
    if summary_destination is not None:
        _ordinary_data_path(summary_destination, file=True)
        with summary_destination.open('a', encoding='utf-8') as stream:
            stream.write(github_summary(report))
    print(json.dumps({'status': report['status'], 'counts': report['counts'],
                      'report': str(reviewer.output / 'critical-review.json'),
                      'host_maintenance_applied': False, 'coverage_complete': False}))
    return int(report['counts']['critical'] > 0)


if __name__ == '__main__':
    raise SystemExit(main())
