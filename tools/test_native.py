"""Run the regression suites appropriate to this host, as GitHub Actions does.

Windows runs the complete Python suite. Native macOS/Linux runs all native
counterparts and the shared portable suites below; Windows contract fixtures
are not evidence of native platform support. Extra arguments go to pytest.
"""
from pathlib import Path
import os
import subprocess
import sys


PORTABLE_SUITES = (
    'test_filesystem.py', 'test_source_workspace.py', 'test_package_source.py', 'test_cleaner_cancel.py',
    'test_automation.py', 'test_gui_startup.py', 'test_gui_instance.py', 'test_gui_activation.py',
    'test_sampler.py', 'test_slowdown.py', 'test_runtime_priority.py', 'test_runtime_priority_startup.py',
    'test_driver_coverage.py', 'test_app_scan_evidence.py', 'test_update_holds.py',
    'test_legacy_support.py', 'test_maintenance_insights.py',
    'test_smart_maintenance.py', 'test_smart_maintenance_panel.py', 'test_calendar_cadence.py',
    'test_low_space_cleanup.py',
    'test_opera_browser.py', 'test_system_restore.py', 'test_driver_backups.py', 'test_recovery_panel.py',
    'test_launch_review.py', 'test_smart_setup_integration.py', 'test_consumer_privacy_controls.py',
    'test_unattended_configuration.py',
    'test_change_history.py', 'test_revert_dialog.py',
    'test_live_metrics.py', 'test_app_presence.py', 'test_driver_update_backup.py',
    'test_software_app_rules.py',
    'test_codex_archives.py', 'test_retention_cleanup.py', 'test_retention_choices_ui.py',
    'test_worker_resource_totals.py',
    'test_update_payloads.py', 'test_update_payload_settings.py', 'test_update_payload_providers.py',
    'test_automatic_update_recovery.py', 'test_continue_updates.py',
    'test_application_caches.py', 'test_storage_growth.py', 'test_recovery_retention.py',
    'test_maintenance_toolkit_integration.py', 'test_browser_native_process_guard.py',
    'test_performance_history.py', 'test_system_diagnostics.py', 'test_additional_packages.py',
    'test_toolkit_configuration.py', 'test_maintenance_toolkit_ui.py',
    'test_update_resume.py', 'test_resident_update_completion.py',
    'test_adaptive_maintenance.py', 'test_adaptive_toolkit_integration.py',
    'test_critical_review_toolkit.py',
    'test_toolkit_cli.py', 'test_reboot_workflow.py',
    'test_integrity_lifecycle.py', 'test_toolkit_report_archive.py',
    'test_toolkit_verification.py',
    'test_navigation_scroll_geometry.py',
)


def selection(root, system=None):
    system = sys.platform if system is None else system
    if system == 'win32':
        return []
    if system not in ('darwin', 'linux'):
        raise ValueError('Optimus test selection supports Windows, macOS and Linux.')
    root = Path(root)
    native = sorted(path.relative_to(root).as_posix() for path in (root / 'tests').glob('test_native*.py'))
    if not native:
        raise ValueError('Native platform regression tests are missing.')
    shared = ['tests/' + name for name in PORTABLE_SUITES]
    if any(not (root / name).is_file() for name in shared):
        raise ValueError('A shared portable regression suite is missing.')
    return native + shared


def main(argv=None):
    root = Path(__file__).resolve().parents[1]
    arguments = list(sys.argv[1:] if argv is None else argv)
    environment = dict(os.environ)
    environment['PYTHONPATH'] = str(root / 'src') + (os.pathsep + environment['PYTHONPATH'] if environment.get('PYTHONPATH') else '')
    return subprocess.run([sys.executable, '-m', 'pytest', '-q', *selection(root), *arguments],
                          cwd=root, env=environment).returncode


if __name__ == '__main__':
    raise SystemExit(main())
