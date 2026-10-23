"""Entry point for the portable Windows application and scheduled read-only audits."""
from __future__ import annotations
import argparse
import ctypes
import json
import os
import re
import sys
import time
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
    commands.add_argument('--tls-probe', action='store_true', help='Verify bundled TLS certificate trust without network requests, UAC, interface or maintenance.')
    commands.add_argument('--startup-mode', choices=('on', 'off', 'status'), help='Configure or inspect this user\'s native sign-in launch without running maintenance.')
    commands.add_argument('--speed-enhancements', choices=('status', 'apply', 'history'), help='Inspect, apply saved speed-enhancement choices, or list their recovery records without opening the interface.')
    commands.add_argument('--speed-restore', type=_speed_identity, metavar='RUN_ID', help='Restore one exact recorded speed-enhancement change without opening the interface.')
    commands.add_argument('--windows-compatibility', choices=('status', 'prepare'), help='Inspect or prepare the saved legacy Windows media-Setup CPU policy; no OS upgrade is launched.')
    commands.add_argument('--windows-compatibility-restore', type=_run_identity, metavar='CHANGE_ID', help='Restore one recorded Windows compatibility-policy change.')
    commands.add_argument('--update-recovery', choices=('status', 'reconcile'), help='Inspect one updater journal or reconcile a verified legacy preflight error; never starts an installer.')
    commands.add_argument('--safe-stop', action='store_true', help='Ask the running Optimus window to stop new work and exit after active operations save their outcomes; never force-kills installers.')
    commands.add_argument('--self-update-worker', type=_run_identity, help=argparse.SUPPRESS)
    commands.add_argument('--self-update-resume', type=_run_identity, help=argparse.SUPPRESS)
    result.add_argument('--update-record', type=_run_identity, metavar='JOURNAL_ID', help='Exact updater journal for --update-recovery.')
    result.add_argument('--recovery-evidence', type=Path, help='Reviewed, hash-bound evidence for an explicit legacy-journal reconciliation.')
    result.add_argument('--output', type=Path, help='Report destination folder.')
    commands.add_argument('--smoke-test', action='store_true', help='Build and inspect the interface without changing settings.')
    result.add_argument('--screenshot', type=Path)
    result.add_argument('--source-workspace', type=Path, help=argparse.SUPPRESS)
    result.add_argument('--restart-ready-file', type=Path, help=argparse.SUPPRESS)
    result.add_argument('--admin-relaunch', action='store_true', help=argparse.SUPPRESS)
    # Retain old shortcuts and restart arguments; native window behavior is now standard.
    result.add_argument('--no-orb', action='store_true', help=argparse.SUPPRESS)
    return result


def _run_identity(value):
    if not re.fullmatch('[0-9a-f]{32}', value):
        raise argparse.ArgumentTypeError('Use one exact lowercase workflow run ID (32 hexadecimal characters).')
    return value


def _speed_identity(value):
    if not re.fullmatch('[0-9a-f]{32}', value):
        raise argparse.ArgumentTypeError('Use one exact lowercase speed-enhancement run ID (32 hexadecimal characters).')
    return value


def _speed_command(backend, args):
    """Dispatch only the requested service; no GUI, elevation or saved auto-run."""
    mode = args.speed_enhancements or 'restore'
    destination = None
    if args.output is not None:
        from .filesystem import resolve_data_root
        destination = resolve_data_root(args.output)
        destination.mkdir(parents=True, exist_ok=True)
    if mode == 'status':
        result = backend.speed_enhancements_scan()
    elif mode == 'apply':
        # The service loads and enforces saved settings. A command-line request
        # never silently enables the package or changes the approved scope.
        result = backend.apply_speed_enhancements()
    elif mode == 'history':
        result = backend.speed_enhancements_history()
    else:
        result = backend.restore_speed_enhancements(args.speed_restore)
    if destination is not None:
        from .workflow import atomic_json
        exported = destination / ('speed-' + mode + '-' + uuid.uuid4().hex + '.json')
        payload = {'schema': 1, 'command': mode, 'result': result}
        atomic_json(exported, payload)
        return dict(payload, report=str(exported))
    return result


def _windows_compatibility_command(backend, args):
    if sys.platform != 'win32':
        raise RuntimeError('Windows media-Setup compatibility policies are unavailable on this platform.')
    destination = None
    if args.output is not None:
        from .filesystem import resolve_data_root
        destination = resolve_data_root(args.output)
        destination.mkdir(parents=True, exist_ok=True)
    mode = args.windows_compatibility or 'restore'
    if mode == 'status':
        result = backend.windows_compatibility_status()
    elif mode == 'prepare':
        result = backend.prepare_windows_compatibility()
    else:
        result = backend.restore({'source': 'python-compatibility', 'Id': args.windows_compatibility_restore})
    if destination is not None:
        from .workflow import atomic_json
        exported = destination / ('windows-compatibility-' + mode + '-' + uuid.uuid4().hex + '.json')
        payload = {'schema': 1, 'command': mode, 'result': result}
        atomic_json(exported, payload)
        return dict(payload, report=str(exported))
    return result


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


def _update_recovery_command(backend, args):
    from .update_recovery import inspect_legacy_cache_guard, reconcile_legacy_cache_guard
    if not args.update_record:
        raise ValueError('Choose one exact updater journal with --update-record.')
    if args.update_recovery == 'status':
        if args.recovery_evidence is not None:
            raise ValueError('Reviewed recovery evidence is accepted only for explicit reconciliation.')
        result = inspect_legacy_cache_guard(backend.data_dir, args.update_record)
    else:
        from .automation import RunLease
        with RunLease(backend.data_dir) as lease:
            result = reconcile_legacy_cache_guard(backend.data_dir, args.update_record,
                lease=lease, reviewed_evidence=args.recovery_evidence)
    if not isinstance(result, dict):
        raise ValueError('The update recovery service returned an invalid result.')
    result = dict(result, maintenance_actions_executed=0, installers_started_by_recovery=0)
    if args.output is not None:
        from .filesystem import resolve_data_root
        from .workflow import atomic_json
        folder = resolve_data_root(args.output)
        folder.mkdir(parents=True, exist_ok=True)
        report = folder / ('update-recovery-' + args.update_record + '-' + uuid.uuid4().hex + '.json')
        atomic_json(report, result)
        result = dict(result, report=str(report))
    return result


def _safe_stop_command(backend):
    """Contact only this user's existing GUI; never open one or request UAC."""
    from .gui_instance import GuiInstance
    instance = GuiInstance(backend.data_dir)
    try:
        accepted = instance.stop_existing()
    finally:
        instance.close()
    return {'status': 'stop_requested' if accepted else 'unavailable',
            'request_accepted': accepted, 'force_terminated_processes': 0,
            'note': ('Optimus will stop new work and exit after active operations return and their outcomes are saved. '
                     'This acknowledges the request, not completion; active installers are allowed to finish.'
                     if accepted else 'No compatible running Optimus window acknowledged the stop request. No process was terminated.')}

def _tls_probe_command(destination=None):
    """Inspect packaged certificate trust without constructing any app service."""
    from .tls_context import trust_probe
    result = trust_probe()
    if (not isinstance(result, dict) or result.get('status') != 'ready'
            or result.get('trust_source') != 'system_plus_bundled_mozilla'
            or result.get('hostname_checking') is not True
            or result.get('certificate_verification') != 'CERT_REQUIRED'
            or type(result.get('network_requests')) is not int or result['network_requests'] != 0
            or result.get('maintenance_applied') is not False
            or not isinstance(result.get('bundle_sha256'), str)
            or re.fullmatch('[0-9a-f]{64}', result['bundle_sha256']) is None
            or any(type(result.get(key)) is not int or not 0 < result[key] <= maximum
                   for key, maximum in (('bundle_size_bytes', 16 * 1024 * 1024),
                                        ('bundled_ca_count', 100000), ('trusted_ca_count', 100000)))):
        raise ValueError('The TLS trust probe did not prove a strict bundled certificate context.')
    payload = dict(result, schema=1, command='tls-probe')
    raw = (json.dumps(payload, sort_keys=True) + '\n').encode('utf-8')
    if len(raw) > 16384:
        raise ValueError('The TLS trust probe exceeds its report limit.')
    if destination is not None:
        from .filesystem import _ordinary_data_path
        folder = Path(os.path.abspath(destination))
        if len(str(folder)) > 1024:
            raise ValueError('The TLS report directory exceeds its path limit.')
        _ordinary_data_path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        _ordinary_data_path(folder)
        report = folder / ('tls-trust-' + uuid.uuid4().hex + '.json')
        # A probe never opens preferences/the locator or replaces a report.
        # Exclusive creation also makes windowed builds inspectable.
        with report.open('xb') as stream:
            stream.write(raw)
            stream.flush()
        _ordinary_data_path(report, file=True)
        payload['report'] = str(report)
    return payload


def main(argv=None):
    actual_arguments = list(sys.argv[1:] if argv is None else argv)
    args = parser().parse_args(actual_arguments)
    if args.tls_probe:
        try:
            print(json.dumps(_tls_probe_command(args.output)))
            return 0
        except (ImportError, OSError, ValueError, RuntimeError) as error:
            print(json.dumps({'status': 'error', 'command': 'tls-probe',
                              'network_requests': 0, 'maintenance_applied': False,
                              'error': str(error)[:2048]}), file=sys.stderr)
            return 2
    if args.self_update_worker:
        # The exact core transaction is self-contained. Do not initialize a
        # backend/GUI, request UAC, acquire a GUI lease or start saved actions.
        from .self_update import worker_main
        try:
            outcome = worker_main(args.self_update_worker)
            return outcome if type(outcome) is int and outcome in (0, 2) else 2
        except (OSError, ValueError, RuntimeError):
            return 2
    if (args.update_record or args.recovery_evidence) and not args.update_recovery:
        parser().error('--update-record and --recovery-evidence require --update-recovery.')
    if sys.platform not in ('win32', 'darwin', 'linux'):
        print('Optimus supports Windows, macOS and Linux.', file=sys.stderr)
        return 2
    if sys.platform == 'win32':
        from .backend import Backend
    else:
        from .native_backend import NativeBackend as Backend
    backend = Backend()
    if (args.verify_run or args.startup_mode or args.speed_enhancements or args.speed_restore
            or args.windows_compatibility or args.windows_compatibility_restore or args.update_recovery or args.safe_stop):
        try:
            if args.safe_stop:
                result = _safe_stop_command(backend)
            elif args.update_recovery:
                result = _update_recovery_command(backend, args)
            elif args.verify_run:
                result = _verify_saved_run(backend, args.verify_run, args.output)
            elif args.speed_enhancements or args.speed_restore:
                result = _speed_command(backend, args)
            elif args.windows_compatibility or args.windows_compatibility_restore:
                result = _windows_compatibility_command(backend, args)
            else:
                if args.startup_mode != 'status':
                    backend.set_startup_launch(args.startup_mode == 'on')
                result = backend.startup_launch_status()
            print(json.dumps(result))
            if args.safe_stop and result.get('request_accepted') is not True:
                return 2
            if args.update_recovery == 'reconcile' and result.get('status') not in ('reconciled', 'already_reconciled'):
                return 2
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
    return _run_gui(backend, args, actual_arguments)


def _run_gui(backend, args, actual_arguments):
    """Claim the interactive session before UAC or any automatic GUI work."""
    from .gui_instance import GuiInstance
    read_only_gui = args.smoke_test or args.startup_probe
    instance = None
    interface_priority = None
    closed = False

    def close():
        nonlocal closed
        if closed:
            return
        closed = True
        try:
            backend.close()
        finally:
            if instance is not None:
                instance.close()

    try:
        staged_restart = False
        if not read_only_gui:
            instance = GuiInstance(backend.data_dir)
            if not instance.try_claim():
                if args.restart_ready_file and args.source_workspace:
                    # Build the saved source hidden. The current editor only
                    # exits after its child has initialized and reported ready.
                    staged_restart = True
                elif args.admin_relaunch:
                    if not instance.wait_for_handoff():
                        if instance.activate_existing():
                            return 0
                        raise RuntimeError('The previous Optimus window is still open. The administrator relaunch could not take over.')
                else:
                    if instance.activate_existing():
                        return 0
                    raise RuntimeError('Optimus is already open, but its window could not be activated. Wait for it to finish starting or respond, then launch it again.')
        from .elevation import startup_administrator
        elevation = (startup_administrator(backend, args, actual_arguments) if sys.platform == 'win32'
                     else {'started': False, 'status': 'per_operation', 'note': ''})
        if elevation.get('started') is True:
            return 0
        if not read_only_gui:
            from .runtime_priority import InterfacePriority
            interface_priority = InterfacePriority()
            preference_reader = getattr(backend, 'preferences', None)
            preference = (preference_reader().get('interface_priority_boost_enabled', True)
                          if callable(preference_reader) else False)
            priority_status = interface_priority.apply(preference)
            if priority_status.get('status') in ('unavailable', 'external_priority') and sys.platform == 'win32':
                elevation = dict(elevation, note=' '.join(filter(None, (
                    elevation.get('note'), priority_status.get('note')))))
        return _show_gui(backend, args, elevation, instance, staged_restart, close)
    except (OSError, ValueError, RuntimeError) as error:
        print(json.dumps({'status': 'error', 'error': str(error)}), file=sys.stderr)
        return 2
    finally:
        try:
            if interface_priority is not None:
                interface_priority.restore()
        finally:
            close()


def _show_gui(backend, args, elevation, instance, staged_restart, close):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QIcon
    if sys.platform == 'win32':
        from .ui import OptimusWindow
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('Ag3nt47.Optimus.Desktop')
    else:
        from .native_ui import NativeOptimusWindow as OptimusWindow
    app = QApplication(sys.argv[:1])
    app.setApplicationName('Optimus')
    app.setApplicationDisplayName('Optimus: Computer optimization tool')
    app.setOrganizationName('Optimus')
    app.setWindowIcon(QIcon(str(Path(__file__).with_name('assets') / 'optimus.svg')))
    read_only_gui = args.smoke_test or args.startup_probe
    window = OptimusWindow(backend, smoke_test=read_only_gui,
                           **({'defer_activation': True} if staged_restart else {}),
                           **({'require_launch_review': True} if not read_only_gui else {}))
    if not read_only_gui:
        window._startup_update_pending = True
    if args.startup_probe:
        window.setEnabled(False)
    if elevation.get('note'):
        window.status.setText(elevation['note'])
    app.aboutToQuit.connect(close)
    if args.restart_ready_file:
        marker = args.restart_ready_file.resolve()
        allowed = (backend.data_dir / 'Sandbox' / 'restart').resolve()
        if marker.parent != allowed:
            raise ValueError('Restart readiness marker is outside the app data folder.')
        marker.parent.mkdir(parents=True, exist_ok=True)
        QTimer.singleShot(750, lambda: marker.write_text(json.dumps({'ready': True, 'pid': os.getpid()}), encoding='utf-8'))

    def start_session():
        if instance is not None:
            instance.listen(window.activate_existing_window, safe_stop=window.request_safe_stop)
        window.show()
        if not read_only_gui:
            window.tray.show()
            from .gui_startup import SetupAutomationController
            controller = SetupAutomationController(backend, window, modal_active=lambda: app.activeModalWidget() is not None)
            controller.stop()  # No cadence or first-run prompt before launch review.
            window._automation_controller = controller
            timer = QTimer(window)
            timer.setInterval(30000)
            def automatic_tick():
                if not window.session_approved():
                    window.invalidate_session_review('Saved actions changed. Automatic maintenance is paused until reviewed.')
                    return
                controller.tick()
            timer.timeout.connect(automatic_tick)
            app.aboutToQuit.connect(controller.stop)
            app.aboutToQuit.connect(timer.stop)
            window._automation_timer = timer

            def pause_automatic_session():
                controller.stop()
                timer.stop()
                errors = []
                stop = getattr(backend, 'smart_maintenance_stop', None)
                if callable(stop):
                    try:
                        stop(wait=False)
                    except Exception as error:
                        errors.append('Smart Maintenance stop: ' + str(error))
                stop = getattr(backend, 'analysis_stop', None)
                if callable(stop):
                    try:
                        stop()
                    except Exception as error:
                        errors.append('Background analysis stop: ' + str(error))
                return '; '.join(errors)

            def resume_approved_session():
                if (not window.session_approved() or window._quitting or window._close_when_idle
                        or getattr(window, '_startup_update_pending', False) is True):
                    return
                try:
                    _resume_saved_analysis(backend)
                except Exception as error:
                    window.status.setText('Background analysis could not resume: ' + str(error))
                activate = getattr(backend, 'activate_smart_maintenance', None)
                if callable(activate):
                    try:
                        activate(lambda: not window.session_approved() or window.busy or window._close_when_idle
                                 or window._quitting or window._setup_wizard is not None
                                 or window._settings_window is not None and window._settings_window.busy)
                    except Exception as error:
                        window.status.setText('Smart Maintenance could not resume: ' + str(error))
                controller.stopped = False
                timer.start()
                QTimer.singleShot(1500, automatic_tick)

            window._session_invalidated_callback = pause_automatic_session
            window._session_approved_callback = resume_approved_session
            def begin_startup_review():
                if window._quitting or window._close_when_idle:
                    return
                from .self_update_ui import StartupUpdateController
                window._self_update_controller = StartupUpdateController(window, backend,
                    resume_id=getattr(args, 'self_update_resume', None))
                window._self_update_controller.start()
            QTimer.singleShot(0, begin_startup_review)

    if staged_restart:
        window.tray.hide()
        deadline = time.monotonic() + 25
        handoff = QTimer(window)
        window._handoff_timer = handoff

        def take_over():
            try:
                if instance.try_claim():
                    handoff.stop()
                    start_session()
                elif time.monotonic() >= deadline:
                    handoff.stop()
                    print(json.dumps({'status': 'error', 'error': 'The previous Optimus window did not finish its source restart handoff.'}), file=sys.stderr)
                    app.exit(2)
            except (OSError, ValueError, RuntimeError) as error:
                handoff.stop()
                print(json.dumps({'status': 'error', 'error': str(error)}), file=sys.stderr)
                app.exit(2)
        handoff.timeout.connect(take_over)
        handoff.start(100)
    else:
        start_session()
    if read_only_gui:
        def inspect():
            try:
                if window.windowTitle() != 'Optimus: Computer optimization tool':
                    raise AssertionError('Incorrect application title.')
                if args.smoke_test:
                    # Exercise the bundled GIF decoder too: a source-only UI
                    # test cannot prove the native package contains Qt's plugin.
                    from .widgets.local_loading import LocalLoadingAnimation
                    decorative = LocalLoadingAnimation(window)
                    if not decorative.ready:
                        raise AssertionError('The bundled decorative loading GIF could not be decoded.')
                    decorative.deleteLater()
                    # Exercise frozen dependencies without reading user files,
                    # saving passwords or applying any maintenance.
                    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
                    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
                    from PySide6.QtGui import QPixmap
                    key = Scrypt(salt=os.urandom(16), length=32, n=16384, r=8, p=1).derive(b'Optimus packaging probe')
                    cipher, nonce = AESGCM(key), os.urandom(12)
                    probe = b'Optimus in-memory file-utility self-test'
                    if cipher.decrypt(nonce, cipher.encrypt(nonce, probe, b'package'), b'package') != probe:
                        raise AssertionError('The bundled file-utility cryptography self-test failed.')
                    if QPixmap(str(Path(__file__).parent / 'assets' / 'checkbox-check.svg')).isNull():
                        raise AssertionError('The bundled checkbox indicator could not be decoded.')
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
