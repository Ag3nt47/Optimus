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
from unittest.mock import Mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(ROOT / 'src'))

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QFont, QFontDatabase, QFontMetrics
from PySide6.QtWidgets import (QApplication, QAbstractButton, QMessageBox, QScrollArea,
    QTabWidget, QWizard)

from optimus.automation import CURRENT_SETUP_VERSION
from optimus.filesystem import _ordinary_data_path
from optimus.native_settings import full_native_settings
from optimus.native_ui import NativeOptimusWindow, NativeSetupWizard
from optimus.onboarding import InitialSetupWizard
from optimus.ui import OptimizationProgressWindow, OptimusWindow
from optimus.widgets.questions import make_yes_no_dialog
from optimus.workflow import full_optimization_settings


VIEWPORTS = ((850, 650), (1024, 768))
MANUAL_GAPS = [
    {'area': 'CPU and RAM', 'review': 'Run representative workloads and compare measured CPU, memory pressure and responsiveness. Fixture configuration readback cannot establish a speed gain or identify every unnecessary process.'},
    {'area': 'GPU', 'review': 'Check actual GPU utilization, VRAM, temperatures and graphics drivers separately. GPU/shader cache cleanup is not GPU tuning; no overclock or unsupported voltage change is tested.'},
    {'area': 'One-click scope', 'review': 'Confirm that saved approvals match the intended computer. Browser close-or-skip, restarts, vendor installers, process removal and native permissions may still require a decision; maximum automation is not universal unattended support.'},
    {'area': 'Updates and security', 'review': 'Validate real signed catalogs, hardware, reboot state and effective security controls on representative machines. No driver, firmware, Windows upgrade, compatibility workaround or security change is applied by this bot.'},
    {'area': 'Files, browsers and cloud', 'review': 'Exercise disposable real profiles, filesystem locks, Trash/Recycle Bin recovery and an explicitly connected test cloud account. No real user file or connected account is touched here.'},
    {'area': 'Accessibility and desktop behavior', 'review': 'Review screenshots, screen readers, high contrast, keyboard order, localization, text scaling, real multi-monitor desktops and animation quality. Offscreen reachability does not prove native desktop usability.'},
    {'area': 'Sandbox and privileged prompts', 'review': 'Review actual source editing/restart handoff, UAC/polkit/macOS authorization, unsaved work and packaging signatures separately. The bot never approves an OS dialog.'},
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

    def close(self):
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
        actual = [widget.width(), widget.height()]
        self.check(identity + '.viewport', actual[0] <= dimensions[0] and actual[1] <= dimensions[1],
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
                'current': 1, 'total': 7, 'elapsed_seconds': 125}]}
        try:
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
            self.screenshot(progress, prefix + '-updates-waiting')
            progress.cancel_button.click()
            progress.update_progress(waiting)
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
                self.check(prefix + '.main.three-actions', len(window.main_buttons) == 3,
                    'The home screen must retain its three primary actions.')
                self.buttons(window, dimensions, prefix + '.main', window.main_buttons)
                self.screenshot(window, prefix + '-main')
                self.review_setup(backend, dimensions, prefix)
                self.review_speed(window, backend, dimensions, prefix)
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
