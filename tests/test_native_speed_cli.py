"""Speed scripts expose exact services without GUI auto-runs or host mutations."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus import app


ROOT = Path(__file__).resolve().parents[1]
RUN = '0123456789abcdef0123456789abcdef'


@pytest.fixture
def service(tmp_path):
    return SimpleNamespace(
        data_dir=tmp_path,
        speed_enhancements_scan=Mock(return_value={'status': 'available', 'read_only': True}),
        apply_speed_enhancements=Mock(return_value={'id': RUN, 'status': 'completed'}),
        speed_enhancements_history=Mock(return_value=[{'id': RUN, 'restore_available': True}]),
        restore_speed_enhancements=Mock(return_value={'id': RUN, 'status': 'restored'}),
        preferences=Mock(), save_preferences=Mock(), analysis_start=Mock(),
        optimize=Mock(), close=Mock(),
    )


def install_service(monkeypatch, service, platform):
    from optimus import backend, native_backend, elevation
    monkeypatch.setattr(backend, 'Backend', lambda: service)
    monkeypatch.setattr(native_backend, 'NativeBackend', lambda: service)
    gui = Mock(side_effect=AssertionError('Headless speed commands must never enter the GUI.'))
    elevation_call = Mock(side_effect=AssertionError('Headless commands must not request startup administration.'))
    resume = Mock(side_effect=AssertionError('Headless commands must not resume background analysis.'))
    monkeypatch.setattr(app, '_run_gui', gui)
    monkeypatch.setattr(app, '_resume_saved_analysis', resume)
    monkeypatch.setattr(elevation, 'startup_administrator', elevation_call)
    monkeypatch.setattr(app.sys, 'platform', platform)
    return gui, elevation_call, resume


@pytest.mark.parametrize('platform', ('win32', 'darwin', 'linux'))
@pytest.mark.parametrize('arguments,method', (
    (['--speed-enhancements', 'status'], 'speed_enhancements_scan'),
    (['--speed-enhancements', 'apply'], 'apply_speed_enhancements'),
    (['--speed-enhancements', 'history'], 'speed_enhancements_history'),
    (['--speed-restore', RUN], 'restore_speed_enhancements'),
))
def test_commands_use_exact_service_and_never_enable_saved_choices(
        tmp_path, monkeypatch, capsys, service, platform, arguments, method):
    guards = install_service(monkeypatch, service, platform)
    assert app.main(arguments) == 0
    assert json.loads(capsys.readouterr().out) == getattr(service, method).return_value
    getattr(service, method).assert_called_once_with(*([RUN] if method.startswith('restore_') else []))
    for other in ('speed_enhancements_scan', 'apply_speed_enhancements',
                  'speed_enhancements_history', 'restore_speed_enhancements'):
        if other != method:
            getattr(service, other).assert_not_called()
    for other in ('preferences', 'save_preferences', 'analysis_start', 'optimize'):
        getattr(service, other).assert_not_called()
    for guard in guards:
        guard.assert_not_called()
    service.close.assert_called_once_with()
    assert list(tmp_path.iterdir()) == []  # No implicit report or maintenance folder.


@pytest.mark.parametrize('mode,method', (
    ('status', 'speed_enhancements_scan'), ('apply', 'apply_speed_enhancements'),
    ('history', 'speed_enhancements_history'),
))
def test_output_exports_a_unique_json_envelope(tmp_path, monkeypatch, capsys, service, mode, method):
    install_service(monkeypatch, service, sys.platform)
    target = tmp_path / 'reports with spaces'
    assert app.main(['--speed-enhancements', mode, '--output', str(target)]) == 0
    printed = json.loads(capsys.readouterr().out)
    exported = Path(printed.pop('report'))
    assert exported.parent == target and exported.name.startswith('speed-' + mode + '-')
    assert json.loads(exported.read_text(encoding='utf-8')) == printed
    assert printed == {'schema': 1, 'command': mode, 'result': getattr(service, method).return_value}


def test_bad_output_path_prevents_any_apply(tmp_path, monkeypatch, capsys, service):
    install_service(monkeypatch, service, sys.platform)
    target = tmp_path / 'ordinary-file'
    target.write_text('existing', encoding='utf-8')
    assert app.main(['--speed-enhancements', 'apply', '--output', str(target)]) == 2
    assert json.loads(capsys.readouterr().err)['status'] == 'error'
    service.apply_speed_enhancements.assert_not_called()
    service.close.assert_called_once_with()
    assert target.read_text(encoding='utf-8') == 'existing'


@pytest.mark.parametrize('arguments,method', (
    (['--speed-enhancements', 'status'], 'speed_enhancements_scan'),
    (['--speed-enhancements', 'apply'], 'apply_speed_enhancements'),
    (['--speed-enhancements', 'history'], 'speed_enhancements_history'),
    (['--speed-restore', RUN], 'restore_speed_enhancements'),
))
@pytest.mark.parametrize('failure', (OSError('fixture IO failure'), ValueError('fixture validation'), RuntimeError('fixture native denial')))
def test_service_failure_is_json_and_always_closes(monkeypatch, capsys, service, arguments, method, failure):
    install_service(monkeypatch, service, sys.platform)
    getattr(service, method).side_effect = failure
    assert app.main(arguments) == 2
    assert json.loads(capsys.readouterr().err) == {'status': 'error', 'error': str(failure)}
    service.close.assert_called_once_with()


@pytest.mark.parametrize('identity', ('', '../preferences', 'A' * 32, 'a' * 31, 'a' * 33, 'f' * 31 + 'g'))
def test_restore_requires_one_exact_lowercase_record_identity(identity):
    with pytest.raises(SystemExit):
        app.parser().parse_args(['--speed-restore', identity])


@pytest.mark.parametrize('existing', (
    ['--audit'], ['--process-scan'], ['--verify-run', RUN], ['--startup-mode', 'on'],
    ['--startup-probe'], ['--smoke-test'], ['--speed-restore', RUN],
))
def test_speed_commands_are_exclusive_with_other_work(existing):
    with pytest.raises(SystemExit):
        app.parser().parse_args(['--speed-enhancements', 'apply', *existing])


def test_unknown_mode_rejected_before_backend_construction(monkeypatch):
    from optimus import backend
    constructor = Mock(side_effect=AssertionError('Must parse before constructing a backend.'))
    monkeypatch.setattr(backend, 'Backend', constructor)
    with pytest.raises(SystemExit):
        app.main(['--speed-enhancements', 'fastest'])
    constructor.assert_not_called()


def powershell():
    command = shutil.which('powershell') or shutil.which('pwsh')
    if command is None:
        pytest.skip('PowerShell is unavailable on this native test runner.')
    return command


@pytest.mark.parametrize('arguments,expected', (
    ([], ['--speed-enhancements', 'status']),
    (['-Mode', 'apply'], ['--speed-enhancements', 'apply']),
    (['-Mode', 'history'], ['--speed-enhancements', 'history']),
    (['-Restore', RUN], ['--speed-restore', RUN]),
))
def test_powershell_wrapper_forwards_only_exact_argv(tmp_path, arguments, expected):
    fixture = tmp_path / 'fixture executable.ps1'
    fixture.write_text('$args | ConvertTo-Json -Compress\nexit 17\n', encoding='utf-8')
    report = str(tmp_path / 'report space & literal')
    completed = subprocess.run([powershell(), '-NoLogo', '-NoProfile', '-NonInteractive',
        '-File', str(ROOT / 'scripts/Speed-Enhancements.ps1'), '-Executable', str(fixture),
        *arguments, '-OutputDirectory', report], text=True, capture_output=True, timeout=30)
    assert completed.returncode == 17, completed.stderr
    assert json.loads(completed.stdout) == [*expected, '--output', report]
    assert not Path(report).exists()  # Only the fixture received this value.


@pytest.mark.parametrize('arguments', (
    ['-Restore', '../bad'], ['-Restore', 'A' * 32],
    ['-Restore', RUN, '-Mode', 'apply'], ['-Mode', 'fastest'],
))
def test_powershell_wrapper_rejects_invalid_operation_without_execution(tmp_path, arguments):
    fixture = tmp_path / 'must not execute.ps1'
    marker = tmp_path / 'executed.txt'
    fixture.write_text('Set-Content -LiteralPath ' + repr(str(marker)) + ' -Value executed\n', encoding='utf-8')
    completed = subprocess.run([powershell(), '-NoLogo', '-NoProfile', '-NonInteractive',
        '-File', str(ROOT / 'scripts/Speed-Enhancements.ps1'), '-Executable', str(fixture),
        *arguments], text=True, capture_output=True, timeout=30)
    assert completed.returncode != 0
    assert not marker.exists()


def test_powershell_wrapper_discovers_only_an_adjacent_portable_executable(tmp_path):
    scripts = tmp_path / 'Optimus/source/scripts'
    scripts.mkdir(parents=True)
    copied = scripts / 'Speed-Enhancements.ps1'
    shutil.copyfile(ROOT / 'scripts/Speed-Enhancements.ps1', copied)
    completed = subprocess.run([powershell(), '-NoLogo', '-NoProfile', '-NonInteractive',
        '-File', str(copied)], text=True, capture_output=True, timeout=30)
    assert completed.returncode == 2
    assert 'Optimus.exe' in completed.stderr


def test_powershell_source_checkout_requires_explicit_executable(tmp_path):
    scripts = tmp_path / 'checkout/scripts'
    scripts.mkdir(parents=True)
    copied = scripts / 'Speed-Enhancements.ps1'
    shutil.copyfile(ROOT / 'scripts/Speed-Enhancements.ps1', copied)
    completed = subprocess.run([powershell(), '-NoLogo', '-NoProfile', '-NonInteractive',
        '-File', str(copied)], text=True, capture_output=True, timeout=30)
    assert completed.returncode == 2
    assert 'Source checkouts must supply' in completed.stderr


@pytest.fixture
def bash_fixture(tmp_path):
    if os.name == 'nt':
        pytest.skip('Bash wrappers run on the Linux/macOS native runners.')
    executable = tmp_path / 'fixture executable'
    executable.write_text('#!/usr/bin/env python3\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\nsys.exit(17)\n', encoding='utf-8')
    executable.chmod(0o755)
    return executable


@pytest.mark.parametrize('arguments,expected', (
    ([], ['--speed-enhancements', 'status']),
    (['--mode', 'apply'], ['--speed-enhancements', 'apply']),
    (['--mode', 'history'], ['--speed-enhancements', 'history']),
    (['--restore', RUN], ['--speed-restore', RUN]),
))
def test_bash_wrapper_uses_literal_argv(tmp_path, bash_fixture, arguments, expected):
    report = str(tmp_path / 'report space & $(literal)')
    completed = subprocess.run(['/bin/bash', str(ROOT / 'scripts/speed-enhancements.sh'),
        '--executable', str(bash_fixture), *arguments, '--output', report],
        text=True, capture_output=True, timeout=20)
    assert completed.returncode == 17, completed.stderr
    assert json.loads(completed.stdout) == [*expected, '--output', report]
    assert not Path(report).exists()


@pytest.mark.parametrize('arguments', (
    ['--restore', '../bad'], ['--restore', 'A' * 32], ['--mode', 'fastest'],
    ['--mode', 'apply', '--restore', RUN], ['--mode', 'status', '--mode', 'apply'],
    ['--executable'], ['--output'], ['--unknown'],
))
def test_bash_wrapper_rejects_ambiguous_or_invalid_operations(bash_fixture, arguments):
    completed = subprocess.run(['/bin/bash', str(ROOT / 'scripts/speed-enhancements.sh'),
        '--executable', str(bash_fixture), *arguments], text=True, capture_output=True, timeout=20)
    assert completed.returncode == 2
    assert not completed.stdout


@pytest.mark.parametrize('macos', (False, True))
def test_bash_wrapper_discovers_only_the_containing_native_package(tmp_path, bash_fixture, macos):
    package = tmp_path / ('Optimus.app/Contents/Resources' if macos else 'Optimus')
    scripts = package / 'source/scripts'
    scripts.mkdir(parents=True)
    copied = scripts / 'speed-enhancements.sh'
    shutil.copyfile(ROOT / 'scripts/speed-enhancements.sh', copied)
    adjacent = package.parent / 'MacOS/Optimus' if macos else package / 'Optimus'
    adjacent.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(bash_fixture, adjacent)
    adjacent.chmod(0o755)
    completed = subprocess.run(['/bin/bash', str(copied)], text=True, capture_output=True, timeout=20)
    assert completed.returncode == 17, completed.stderr
    assert json.loads(completed.stdout) == ['--speed-enhancements', 'status']


def test_bash_wrapper_rejects_executable_symlinks(tmp_path, bash_fixture):
    link = tmp_path / 'linked executable'
    link.symlink_to(bash_fixture)
    completed = subprocess.run(['/bin/bash', str(ROOT / 'scripts/speed-enhancements.sh'),
        '--executable', str(link)], text=True, capture_output=True, timeout=20)
    assert completed.returncode == 2
    assert not completed.stdout


def test_bash_wrapper_resolves_relative_executable_without_path_search(tmp_path, bash_fixture):
    completed = subprocess.run(['/bin/bash', str(ROOT / 'scripts/speed-enhancements.sh'),
        '--executable', bash_fixture.name], cwd=tmp_path,
        text=True, capture_output=True, timeout=20)
    assert completed.returncode == 17, completed.stderr
    assert json.loads(completed.stdout) == ['--speed-enhancements', 'status']


def test_bash_source_checkout_requires_explicit_executable(tmp_path, bash_fixture):
    scripts = tmp_path / 'checkout/scripts'
    scripts.mkdir(parents=True)
    copied = scripts / 'speed-enhancements.sh'
    shutil.copyfile(ROOT / 'scripts/speed-enhancements.sh', copied)
    completed = subprocess.run(['/bin/bash', str(copied)], text=True, capture_output=True, timeout=20)
    assert completed.returncode == 2
    assert 'Source checkouts must supply' in completed.stderr
