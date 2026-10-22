"""Entry point for the portable Windows application and scheduled read-only audits."""
from __future__ import annotations
import argparse
import ctypes
import json
import os
import re
import sys
import uuid
from pathlib import Path

def _resume_saved_analysis(backend):
    """Resume only a previously saved opt-in; never change preferences."""
    preferences = backend.preferences()
    if preferences.get('background_analysis_enabled') is True:
        return backend.analysis_start(preferences.get('analysis_interval', 5))
    return None

def parser():
    result = argparse.ArgumentParser(prog='Optimus', description='Optimus: Computer optimization tool')
    commands = result.add_mutually_exclusive_group()
    commands.add_argument('--audit', action='store_true', help='Save a read-only audit without opening the interface.')
    commands.add_argument('--process-scan', action='store_true', help='Export a read-only process and launch-evidence report; no actions are applied.')
    commands.add_argument('--verify-run', type=_run_identity, metavar='RUN_ID', help='Verify a completed saved workflow without repeating its actions or opening the interface.')
    commands.add_argument('--startup-probe', action='store_true', help='Inspect GUI startup without UAC, setup, background analysis or maintenance, then exit.')
    commands.add_argument('--startup-mode', choices=('on', 'off', 'status'), help='Configure or inspect this user\'s Windows sign-in launch without running maintenance.')
    result.add_argument('--output', type=Path, help='Report destination folder.')
    commands.add_argument('--smoke-test', action='store_true', help='Build and inspect the interface without changing settings.')
    result.add_argument('--screenshot', type=Path)
    result.add_argument('--source-workspace', type=Path, help=argparse.SUPPRESS)
    result.add_argument('--restart-ready-file', type=Path, help=argparse.SUPPRESS)
    result.add_argument('--admin-relaunch', action='store_true', help=argparse.SUPPRESS)
    result.add_argument('--no-orb', action='store_true', help='Use normal Windows close behavior for this session.')
    return result


def _run_identity(value):
    if not re.fullmatch('[0-9a-f]{32}', value):
        raise argparse.ArgumentTypeError('Use one exact lowercase workflow run ID (32 hexadecimal characters).')
    return value


def _verify_saved_run(backend, run_id, destination=None):
    """Read one completed owned journal; export evidence without rerunning work."""
    from .filesystem import _ordinary_data_path
    path = backend.data_dir / 'runs' / (run_id + '.json')
    original = _ordinary_data_path(path, file=True)
    if original is None:
        raise ValueError('The saved workflow journal is unavailable.')
    with path.open('rb') as source:
        opened = os.fstat(source.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_nlink) != (original.st_dev, original.st_ino, 1):
            raise ValueError('The saved workflow journal changed identity.')
        raw = source.read(16 * 1024 * 1024 + 1)
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError('The saved workflow exceeds the verification input limit.')
    current = _ordinary_data_path(path, file=True)
    if current is None or (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (original.st_dev, original.st_ino, original.st_size, original.st_mtime_ns):
        raise ValueError('The saved workflow journal changed during verification.')
    report = json.loads(raw.decode('utf-8-sig'))
    if not isinstance(report, dict) or report.get('id') != run_id:
        raise ValueError('The saved workflow identity does not match the requested run.')
    if report.get('status') not in ('completed', 'completed_with_errors') or not report.get('finished_at'):
        raise ValueError('Wait until this workflow has finished before verifying its saved result.')
    verification = backend.verify_optimization(report)
    if not isinstance(verification, dict):
        raise ValueError('The verification service returned an invalid result.')
    from .filesystem import resolve_data_root
    from .workflow import atomic_json
    folder = resolve_data_root(destination or backend.data_dir / 'VerificationReports')
    folder.mkdir(parents=True, exist_ok=True)
    exported = folder / ('verification-' + run_id + '-' + uuid.uuid4().hex + '.json')
    atomic_json(exported, {'schema': 1, 'source_run_id': run_id, 'verification': verification})
    return {'read_only': True, 'report': str(exported), 'status': verification.get('status'),
            'counts': verification.get('counts'), 'coverage_complete': verification.get('coverage_complete')}

def main(argv=None):
    actual_arguments = list(sys.argv[1:] if argv is None else argv)
    args = parser().parse_args(actual_arguments)
    if sys.platform != 'win32':
        print('Optimus currently supports Windows 10/11.', file=sys.stderr)
        return 2
    from .backend import Backend
    backend = Backend()
    if args.verify_run or args.startup_mode:
        try:
            if args.verify_run:
                result = _verify_saved_run(backend, args.verify_run, args.output)
            else:
                if args.startup_mode != 'status':
                    backend.set_startup_launch(args.startup_mode == 'on')
                result = backend.startup_launch_status()
            print(json.dumps(result))
            return 0
        except (OSError, ValueError, RuntimeError) as error:
            print(json.dumps({'status': 'error', 'error': str(error)}), file=sys.stderr)
            return 2
        finally:
            backend.close()
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
    from .elevation import startup_administrator
    elevation = startup_administrator(backend, args, actual_arguments)
    if elevation.get('started') is True:
        backend.close()
        return 0
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
    read_only_gui = args.smoke_test or args.startup_probe
    window = OptimusWindow(backend, smoke_test=read_only_gui)
    if args.startup_probe:
        window.setEnabled(False)
    if args.no_orb:
        window.setProperty('disable_orb', True)
    window.show()
    if not read_only_gui:
        try:
            _resume_saved_analysis(backend)
        except Exception as error:
            window.status.setText('Background analysis could not resume: ' + str(error))
    if elevation.get('note'):
        window.status.setText(elevation['note'])
    if args.restart_ready_file:
        marker = args.restart_ready_file.resolve()
        allowed = (backend.data_dir / 'Sandbox' / 'restart').resolve()
        if marker.parent != allowed:
            raise ValueError('Restart readiness marker is outside the app data folder.')
        marker.parent.mkdir(parents=True, exist_ok=True)
        QTimer.singleShot(750, lambda: marker.write_text(json.dumps({'ready': True, 'pid': os.getpid()}), encoding='utf-8'))
    app.aboutToQuit.connect(backend.close)
    if not read_only_gui:
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
    if read_only_gui:
        def inspect():
            try:
                if window.windowTitle() != 'Optimus: Computer optimization tool':
                    raise AssertionError('Incorrect application title.')
                if args.screenshot:
                    args.screenshot.parent.mkdir(parents=True, exist_ok=True)
                    if not window.grab().save(str(args.screenshot)):
                        raise AssertionError('Screenshot could not be saved.')
                print(json.dumps({'status': 'passed', 'title': window.windowTitle(), 'width': window.width(), 'height': window.height(), 'mutations': 0, 'read_only': True}))
            except Exception as error:
                print(json.dumps({'status': 'failed', 'error': str(error)}))
                app.exit(1)
                return
            app.quit()
        QTimer.singleShot(750 if args.startup_probe else 11000, inspect)
    return app.exec()

if __name__ == '__main__':
    raise SystemExit(main())
