"""Entry point for the portable Windows application and scheduled read-only audits."""
from __future__ import annotations
import argparse
import ctypes
import json
import os
import sys
from pathlib import Path

def _resume_saved_analysis(backend):
    """Resume only a previously saved opt-in; never change preferences."""
    preferences = backend.preferences()
    if preferences.get('background_analysis_enabled') is True:
        return backend.analysis_start(preferences.get('analysis_interval', 5))
    return None

def parser():
    result = argparse.ArgumentParser(prog='Optimus', description='Optimus: Computer optimization tool')
    result.add_argument('--audit', action='store_true', help='Save a read-only audit without opening the interface.')
    result.add_argument('--process-scan', action='store_true', help='Export a read-only process and launch-evidence report; no actions are applied.')
    result.add_argument('--output', type=Path, help='Audit destination folder.')
    result.add_argument('--smoke-test', action='store_true', help='Build and inspect the interface without changing settings.')
    result.add_argument('--screenshot', type=Path)
    result.add_argument('--source-workspace', type=Path, help=argparse.SUPPRESS)
    result.add_argument('--restart-ready-file', type=Path, help=argparse.SUPPRESS)
    result.add_argument('--no-orb', action='store_true', help='Use normal Windows close behavior for this session.')
    return result

def main(argv=None):
    args = parser().parse_args(argv)
    if sys.platform != 'win32':
        print('Optimus currently supports Windows 10/11.', file=sys.stderr)
        return 2
    from .backend import Backend
    backend = Backend()
    if args.audit or args.process_scan:
        from datetime import datetime
        destination = args.output or backend.data_dir / 'reports' / datetime.now().strftime('%Y%m%d-%H%M%S')
        try:
            if args.process_scan:
                report = backend.process_harness_scan()
                print(json.dumps(backend.process_harness_export(report['id'], destination)))
            else:
                print(json.dumps(backend.export_report(destination)))
            return 0
        finally:
            backend.close()
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QIcon
    from .ui import OptimusWindow
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('Ag3nt47.Optimus.Desktop')
    app = QApplication(sys.argv[:1])
    app.setApplicationName('Optimus')
    app.setApplicationDisplayName('Optimus: Computer optimization tool')
    app.setOrganizationName('Optimus')
    app.setWindowIcon(QIcon(str(Path(__file__).with_name('assets') / 'optimus.svg')))
    window = OptimusWindow(backend, smoke_test=args.smoke_test)
    if args.no_orb:
        window.setProperty('disable_orb', True)
    window.show()
    if not args.smoke_test:
        try:
            _resume_saved_analysis(backend)
        except Exception as error:
            window.status.setText('Background analysis could not resume: ' + str(error))
    if args.restart_ready_file:
        marker = args.restart_ready_file.resolve()
        allowed = (backend.data_dir / 'Sandbox' / 'restart').resolve()
        if marker.parent != allowed:
            raise ValueError('Restart readiness marker is outside the app data folder.')
        marker.parent.mkdir(parents=True, exist_ok=True)
        QTimer.singleShot(750, lambda: marker.write_text(json.dumps({'ready': True, 'pid': os.getpid()}), encoding='utf-8'))
    app.aboutToQuit.connect(backend.close)
    if not args.smoke_test:
        from .gui_startup import SetupAutomationController
        controller = SetupAutomationController(backend, window, modal_active=lambda: app.activeModalWidget() is not None)
        window._automation_controller = controller
        timer = QTimer(window)
        timer.setInterval(30000)
        timer.timeout.connect(controller.tick)
        app.aboutToQuit.connect(controller.stop)
        app.aboutToQuit.connect(timer.stop)
        timer.start()
        window._automation_timer = timer
        QTimer.singleShot(1500, controller.tick)
    if args.smoke_test:
        def inspect():
            try:
                if window.windowTitle() != 'Optimus: Computer optimization tool':
                    raise AssertionError('Incorrect application title.')
                if args.screenshot:
                    args.screenshot.parent.mkdir(parents=True, exist_ok=True)
                    if not window.grab().save(str(args.screenshot)):
                        raise AssertionError('Screenshot could not be saved.')
                print(json.dumps({'status': 'passed', 'title': window.windowTitle(), 'width': window.width(), 'height': window.height(), 'mutations': 0}))
            except Exception as error:
                print(json.dumps({'status': 'failed', 'error': str(error)}))
                app.exit(1)
                return
            app.quit()
        QTimer.singleShot(11000, inspect)
    return app.exec()

if __name__ == '__main__':
    raise SystemExit(main())
