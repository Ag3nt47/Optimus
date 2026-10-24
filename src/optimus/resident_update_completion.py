"""Recognize the supported Ollama post-install runtime, never generic installers.

This is a conservative provider-completion proof, not console quiescence. It
requires the exact successful WinGet command and linked closed Inno log, the
current registered installation, and every remaining private-job image to match
an exact runtime file installed by that same installer. Unknown descendants keep
the original wait. No process is suspended, killed, detached or relaunched.
"""
from __future__ import annotations

import hashlib
import ntpath
import os
from pathlib import Path
import re
import time

from .automatic_update_recovery import _completed_evidence, _log_folder, _require
from .filesystem import _ordinary_data_path
from .update_recovery import _read

KIND = 'verified-ollama-resident-runtime-v1'
_RUNTIME_NAMES = ('ollama app.exe', 'ollama.exe')


def _image_digest(path):
    """Hash a stable ordinary image with one-MiB reads, rather than loading it."""
    path = Path(path)
    before = _ordinary_data_path(path, file=True)
    _require(before is not None and 2 <= before.st_size <= 64 * 1024 * 1024,
        'The installed runtime image is unavailable or exceeds its bound.')
    # Python 3.12 Windows lstat/fstat can expose different ctime semantics.
    # Compare each ctime with its own initial source, not with the other API.
    identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        _require(identity(opened) == identity(before), 'The runtime image changed before hashing.')
        prefix = stream.read(2)
        _require(prefix == b'MZ', 'The installed runtime is not a native Windows image.')
        digest.update(prefix)
        total = 2
        while block := stream.read(1024 * 1024):
            total += len(block)
            _require(total <= before.st_size, 'The runtime image grew during hashing.')
            digest.update(block)
        after = _ordinary_data_path(path, file=True)
        ended = os.fstat(stream.fileno())
        _require(total == before.st_size and after is not None
            and identity(ended) == identity(before) == identity(after)
            and after.st_ctime_ns == before.st_ctime_ns and ended.st_ctime_ns == opened.st_ctime_ns,
            'The runtime image changed while hashing.')
    return digest.hexdigest()


def _registry_applications():
    import winreg
    rows = []
    for hive, view in ((winreg.HKEY_CURRENT_USER, 0),
                       (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
                       (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY)):
        try:
            root = winreg.OpenKey(hive, r'Software\Microsoft\Windows\CurrentVersion\Uninstall',
                                 0, winreg.KEY_READ | view)
        except FileNotFoundError:
            continue
        with root:
            count = winreg.QueryInfoKey(root)[0]
            _require(count <= 10000, 'The registered application inventory exceeds its bound.')
            for index in range(count):
                with winreg.OpenKey(root, winreg.EnumKey(root, index)) as key:
                    def field(name):
                        try:
                            value, kind = winreg.QueryValueEx(key, name)
                        except FileNotFoundError:
                            return ''
                        return value if kind == winreg.REG_SZ and isinstance(value, str) else ''
                    rows.append({'name': field('DisplayName'), 'publisher': field('Publisher'),
                        'version': field('DisplayVersion'), 'location': field('InstallLocation')})
                _require(len(rows) <= 10000, 'The registered application inventory exceeds its bound.')
    return rows


def _installed_runtime_files(raw, location):
    text = raw.decode('utf-8-sig', errors='strict').replace('\r\n', '\n')
    expected = {ntpath.normcase(ntpath.join(location, name)) for name in _RUNTIME_NAMES}
    installed = set()
    blocks = re.split(r'^\d{4}-\d{2}-\d{2} [^\n]+?\s+-- ', text, flags=re.M)
    for block in blocks:
        if not block.startswith('File entry --\n'):
            continue
        destinations = re.findall(r'^\d{4}-\d{2}-\d{2} [^\n]+?\s+Dest filename: ([^\n]+)$', block, re.M)
        if len(destinations) == 1 and 'Successfully installed the file.\n' in block:
            installed.add(ntpath.normcase(destinations[0]))
    _require(expected <= installed, 'The runtime files were not installed by the completed native installer.')
    runs = re.findall(r'^\d{4}-\d{2}-\d{2} [^\n]+?\s+Parameters: (.+)$', text, re.M)
    command = '/C set PATH=' + location + ';%PATH% & "' + ntpath.join(location, 'ollama app.exe') + '"'
    _require(sum(value.casefold() == command.casefold() for value in runs) == 1,
        'The exact installed Ollama post-install launch is unavailable.')
    return expected


def verify_resident_completion(arguments, target_pid, started_at, rows, *, folder=None, applications=None, now=None):
    """Return bounded proof or raise ValueError; caller independently owns job IDs.

    Production supplies native job rows. Optional folders/application inventories
    exist for offline fixtures; no configuration or executable path is accepted
    from an update plan or settings file.
    """
    _require(isinstance(arguments, list) and len(arguments) >= 10
        and ntpath.basename(arguments[0]).casefold() == 'winget.exe'
        and arguments[1:8] == ['upgrade', '--id', 'Ollama.Ollama', '--exact', '--source', 'winget', '--version'],
        'This provider/runtime combination is not supported for resident handoff.')
    version = arguments[8]
    _require(isinstance(version, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.+\-]{0,79}', version),
        'The native target version is unavailable.')
    _require(type(target_pid) is int and target_pid > 0 and isinstance(rows, list) and 1 <= len(rows) <= 8,
        'The remaining native job inventory is unavailable or unsupported.')
    record = {'title': 'Review or update configured software', 'status': 'running', 'installer_prepared': True,
        'current_item': {'id': 'Ollama.Ollama', 'description': 'Updating approved package 1/1: Ollama.Ollama (0 to ' + version + ')'},
        'package_attempt': {'kind': 'terminal-package-attempt-v1', 'id': '0' * 32,
            'item_id': 'Ollama.Ollama', 'state': 'prepared', 'prepared_at': started_at}}
    current = time.time() if now is None else now
    evidence = _completed_evidence(Path(folder) if folder is not None else _log_folder(), record, now=current)
    _require(evidence['native_pid'] == target_pid and evidence['native_arguments'] == arguments[1:],
        'The completed native process does not match the exact supervised command.')
    apps = _registry_applications() if applications is None else applications
    _require(isinstance(apps, list) and len(apps) <= 10000, 'The installed application inventory is unavailable.')
    matching = [app for app in apps if isinstance(app, dict)
        and app.get('name') == evidence['application_name'] and app.get('publisher') == evidence['publisher']]
    _require(len(matching) == 1 and matching[0].get('version') == version,
        'The current installed application differs from native completion.')
    location = matching[0].get('location')
    _require(isinstance(location, str) and ntpath.isabs(location) and bool(ntpath.splitdrive(location)[0])
        and '\x00' not in location and location.rstrip('\\/') == ntpath.normpath(location),
        'The registered installation directory is unavailable.')
    location = ntpath.normpath(location)
    inno = _read(Path(evidence['installer_log']), 4 * 1024 * 1024)
    expected = _installed_runtime_files(inno, location)
    processes, seen = [], set()
    for row in rows:
        _require(isinstance(row, dict) and type(row.get('pid')) is int and row['pid'] > 0
            and row['pid'] not in seen and row['pid'] != target_pid,
            'The native job contains an ambiguous or still-active provider identity.')
        seen.add(row['pid'])
        path, created = row.get('path'), row.get('created_at')
        _require(isinstance(path, str) and ntpath.normcase(path) in expected
            and type(created) in (int, float) and evidence['started_at'] <= created <= current,
            'A remaining job process is not an exact installed Ollama runtime.')
        processes.append({'pid': row['pid'], 'path': path, 'created_at': created,
                          'sha256': _image_digest(path)})
    return {'schema': 1, 'kind': KIND, 'package_id': 'Ollama.Ollama', 'version': version,
        'install_location': location,
        'target_pid': target_pid, 'verified_at': current, 'processes': processes,
        'winget_log_sha256': evidence['winget_log_sha256'],
        'installer_log_sha256': evidence['installer_log_sha256'],
        'native_finished_at': evidence['finished_at'], 'restart_required': False}


def verified_resident_handoff(result, arguments):
    """Validate the in-process transport result before maintenance accepts it.

    This accepts only the private transport's already-authenticated distinct
    receipt. Serialized settings, cached reports and resume plans cannot stand
    in for a freshly supervised result.
    """
    receipt = getattr(result, 'console_receipt', None)
    if not (getattr(result, 'native_completion_verified', None) is True
            and getattr(result, 'capture_complete', None) is False
            and type(getattr(result, 'returncode', None)) is int and result.returncode == 0
            and isinstance(receipt, dict) and receipt.get('resident_handoff_verified') is True
            and type(receipt.get('schema')) is int and receipt['schema'] == 1
            and receipt.get('event') == 'resident_handoff'
            and isinstance(receipt.get('nonce'), str) and re.fullmatch('[a-f0-9]{32}', receipt['nonce']) is not None
            and type(receipt.get('supervisor_pid')) is int and receipt['supervisor_pid'] > 0
            and type(receipt.get('target_pid')) is int and receipt['target_pid'] > 0
            and receipt.get('quiescent') is False and receipt.get('output_eof') is False
            and receipt.get('job_verified') is True
            and type(receipt.get('console_client_count')) is int and receipt['console_client_count'] == 1
            and receipt.get('supervisor_exit_code') is None
            and type(receipt.get('target_exit_code')) is int and receipt['target_exit_code'] == 0
            and isinstance(arguments, list) and len(arguments) >= 10
            and ntpath.basename(arguments[0]).casefold() == 'winget.exe'
            and arguments[1:8] == ['upgrade', '--id', 'Ollama.Ollama', '--exact', '--source', 'winget', '--version']):
        return False
    from .winget_console import _digest
    proof = receipt.get('resident_proof')
    if not (receipt.get('argv_sha256') == _digest(arguments)
            and isinstance(proof, dict) and type(proof.get('schema')) is int and proof['schema'] == 1
            and proof.get('kind') == KIND and proof.get('package_id') == 'Ollama.Ollama'
            and proof.get('version') == arguments[8] and proof.get('restart_required') is False
            and type(proof.get('target_pid')) is int and proof['target_pid'] == receipt['target_pid']
            and type(receipt.get('job_active_processes')) is int
            and isinstance(proof.get('processes'), list)
            and 1 <= len(proof['processes']) == receipt['job_active_processes'] <= 8):
        return False
    for field in ('winget_log_sha256', 'installer_log_sha256'):
        if not isinstance(proof.get(field), str) or not re.fullmatch('[a-f0-9]{64}', proof[field]):
            return False
    location = proof.get('install_location')
    if not isinstance(location, str) or not ntpath.isabs(location):
        return False
    expected = {ntpath.normcase(ntpath.join(location, name)) for name in _RUNTIME_NAMES}
    seen = set()
    for process in proof['processes']:
        if (not isinstance(process, dict) or type(process.get('pid')) is not int or process['pid'] <= 0
                or process['pid'] in seen or not isinstance(process.get('path'), str)
                or ntpath.normcase(process['path']) not in expected
                or not isinstance(process.get('sha256'), str) or re.fullmatch('[a-f0-9]{64}', process['sha256']) is None):
            return False
        seen.add(process['pid'])
    return True
