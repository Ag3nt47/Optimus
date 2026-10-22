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
    'test_filesystem.py', 'test_source_workspace.py', 'test_package_source.py',
    'test_automation.py', 'test_gui_startup.py', 'test_gui_instance.py', 'test_gui_activation.py',
    'test_sampler.py', 'test_slowdown.py', 'test_runtime_priority.py', 'test_runtime_priority_startup.py',
    'test_driver_coverage.py', 'test_app_scan_evidence.py', 'test_legacy_support.py', 'test_maintenance_insights.py',
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
