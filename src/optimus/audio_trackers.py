"""Find and block hidden microphone recorders and tracking executables.

Windows records every app that used the microphone under
``CapabilityAccessManager\\ConsentStore\\microphone``. ``scan`` reads those
records (and the startup Run keys) and classifies each executable:

* ``block`` - high confidence: the file exists, is not validly code-signed and
  runs from a user-writable drop location (Temp, Downloads, AppData Roaming,
  ProgramData root, Public), or is a script host / rundll32-style launcher
  that used the microphone while unsigned content is involved.
* ``review`` - unusual but plausibly legitimate (signed software in a drop
  location, a script host using the microphone).
* ``ok`` - signed software in Program Files, Windows or Store app folders.

``block`` applies only to ``block`` findings (or reviewed selections) and is
reversible: it ends the process, adds an outbound Windows Firewall block rule
so nothing more is "shipped off", denies its microphone consent, moves its
Run-key autostart into a backup, and moves the file into Optimus quarantine.
Every step is written to a receipt that Change History lists and ``restore``
undoes. Nothing is deleted.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import ntpath
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import uuid
from typing import Any, Callable, Iterable

FOLDER = 'tracker-blocking'
QUARANTINE = 'quarantine'
PLAN_TTL_SECONDS = 15 * 60
RULE_PREFIX = 'Optimus tracker block: '
_CONSENT = r'Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone'
_RUN_KEYS = (r'Software\Microsoft\Windows\CurrentVersion\Run', r'Software\Microsoft\Windows\CurrentVersion\RunOnce')
_LAUNCHERS = {'rundll32.exe', 'regsvr32.exe', 'mshta.exe', 'wscript.exe', 'cscript.exe',
              'powershell.exe', 'pwsh.exe', 'cmd.exe', 'conhost.exe'}
_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def _utc() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _norm(path: str) -> str:
    return ntpath.normcase(ntpath.normpath(str(path or '').strip().strip('"')))


def _env(name: str) -> str:
    value = os.environ.get(name, '')
    return _norm(value) + '\\' if value else ''


def _drop_roots() -> list[str]:
    roots = [_env('TEMP'), _env('TMP'), _env('APPDATA'),
             _norm(os.path.join(os.environ.get('USERPROFILE', ''), 'Downloads')) + '\\',
             _norm(os.environ.get('PUBLIC', 'C:\\Users\\Public')) + '\\',
             _norm(os.path.join(os.environ.get('SystemRoot', 'C:\\Windows'), 'Temp')) + '\\']
    return [root for root in roots if len(root) > 3]


def _trusted_roots() -> list[str]:
    roots = [_env('ProgramFiles'), _env('ProgramFiles(x86)'), _env('SystemRoot'), _env('ProgramW6432')]
    return [root for root in roots if len(root) > 3]


def _in_drop_location(path: str) -> bool:
    normalized = _norm(path)
    if any(normalized.startswith(root) for root in _drop_roots()):
        return True
    program_data = _norm(os.environ.get('ProgramData', 'C:\\ProgramData'))
    # An executable directly in ProgramData (or one level down) is a classic drop.
    parent = ntpath.dirname(normalized)
    return parent == program_data or ntpath.dirname(parent) == program_data


def _in_trusted_location(path: str) -> bool:
    normalized = _norm(path)
    return any(normalized.startswith(root) for root in _trusted_roots())


def classify(path: str, *, signed: bool | None, exists: bool) -> tuple[str, str]:
    """(verdict, reason) for one executable that used the microphone."""
    name = ntpath.basename(_norm(path))
    if not exists:
        return 'ok', 'The file no longer exists; only a stale Windows usage record remains.'
    drop = _in_drop_location(path)
    if name in _LAUNCHERS:
        return 'review', 'A script host or launcher used the microphone; check what it ran.'
    if drop and signed is False:
        return 'block', 'Unsigned program in a user-writable drop folder used the microphone.'
    if drop:
        return 'review', 'Signed program, but it runs from a user-writable folder.'
    if signed is False and not _in_trusted_location(path):
        return 'review', 'Unsigned program outside Program Files used the microphone.'
    return 'ok', 'Signed or installed in a protected location.'


def _filetime(value: Any) -> str | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    seconds = number / 10_000_000 - 11_644_473_600
    return datetime.fromtimestamp(seconds, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def read_microphone_usage() -> list[dict[str, Any]]:
    import winreg
    rows = []
    for hive, hive_name in ((winreg.HKEY_CURRENT_USER, 'HKCU'), (winreg.HKEY_LOCAL_MACHINE, 'HKLM')):
        for sub, packaged in ((_CONSENT + r'\NonPackaged', False), (_CONSENT, True)):
            try:
                key = winreg.OpenKey(hive, sub)
            except OSError:
                continue
            with key:
                index = 0
                while True:
                    try:
                        child = winreg.EnumKey(key, index)
                    except OSError:
                        break
                    index += 1
                    if packaged and child == 'NonPackaged':
                        continue
                    try:
                        with winreg.OpenKey(key, child) as entry:
                            values = {}
                            for name in ('LastUsedTimeStart', 'LastUsedTimeStop', 'Value'):
                                try:
                                    values[name] = winreg.QueryValueEx(entry, name)[0]
                                except OSError:
                                    values[name] = None
                    except OSError:
                        continue
                    if not values['LastUsedTimeStart']:
                        continue
                    rows.append({'hive': hive_name, 'key': sub + '\\' + child, 'packaged': packaged,
                                 'app': child if packaged else child.replace('#', '\\'),
                                 'last_used': _filetime(values['LastUsedTimeStart']),
                                 'in_use': values['LastUsedTimeStop'] in (0, None) and bool(values['LastUsedTimeStart']),
                                 'consent': values['Value']})
    return rows


def read_run_entries() -> list[dict[str, Any]]:
    import winreg
    rows = []
    for hive, hive_name in ((winreg.HKEY_CURRENT_USER, 'HKCU'), (winreg.HKEY_LOCAL_MACHINE, 'HKLM')):
        for sub in _RUN_KEYS:
            try:
                key = winreg.OpenKey(hive, sub)
            except OSError:
                continue
            with key:
                index = 0
                while True:
                    try:
                        name, value, kind = winreg.EnumValue(key, index)
                    except OSError:
                        break
                    index += 1
                    rows.append({'hive': hive_name, 'key': sub, 'name': name, 'command': str(value), 'type': kind})
    return rows


def check_signatures(paths: Iterable[str]) -> dict[str, bool]:
    """Authenticode status per path (True = Valid) using Windows itself."""
    paths = sorted({path for path in paths if path and os.path.isfile(path)})
    if not paths:
        return {}
    script = ('$p = $input | ConvertFrom-Json; $p | ForEach-Object { '
              '[pscustomobject]@{path=$_; valid=((Get-AuthenticodeSignature -LiteralPath $_).Status -eq "Valid")} } '
              '| ConvertTo-Json -Compress')
    completed = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                               input=json.dumps(paths), capture_output=True, text=True, timeout=120,
                               creationflags=_NO_WINDOW)
    data = json.loads(completed.stdout or '[]')
    data = data if isinstance(data, list) else [data]
    return {_norm(row['path']): bool(row['valid']) for row in data if isinstance(row, dict)}


def _powershell(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script, *args],
                          capture_output=True, text=True, timeout=120, creationflags=_NO_WINDOW)


def add_firewall_block(path: str, name: str) -> None:
    script = ('param($p,$n) if(-not (Get-NetFirewallRule -DisplayName $n -ErrorAction SilentlyContinue)){'
              'New-NetFirewallRule -DisplayName $n -Direction Outbound -Program $p -Action Block -Profile Any | Out-Null}')
    result = _powershell('& {' + script + '}', path, name)
    if result.returncode != 0:
        raise OSError((result.stderr or 'Windows Firewall refused the rule (administrator rights are required).').strip()[:200])


def remove_firewall_block(name: str) -> None:
    _powershell('& { param($n) Remove-NetFirewallRule -DisplayName $n -ErrorAction SilentlyContinue }', name)


class AudioTrackers:
    def __init__(self, backend: Any = None, *, data_dir: str | os.PathLike[str] | None = None,
                 usage: Callable[[], list[dict[str, Any]]] = read_microphone_usage,
                 run_entries: Callable[[], list[dict[str, Any]]] = read_run_entries,
                 signatures: Callable[[Iterable[str]], dict[str, bool]] = check_signatures,
                 firewall_block: Callable[[str, str], None] = add_firewall_block,
                 firewall_unblock: Callable[[str], None] = remove_firewall_block,
                 process_iter: Callable[..., Iterable[Any]] | None = None,
                 registry: Any = None, clock: Callable[[], float] = time.monotonic):
        root = data_dir if data_dir is not None else getattr(backend, 'data_dir', None)
        if root is None:
            raise ValueError('A data directory is required for tracker blocking receipts.')
        self.root = Path(root)
        self.directory = self.root / FOLDER
        self.usage, self.run_entries, self.signatures = usage, run_entries, signatures
        self.firewall_block, self.firewall_unblock = firewall_block, firewall_unblock
        self.process_iter, self.registry, self.clock = process_iter, registry, clock
        self.lock, self._plans = threading.RLock(), {}

    # -- scan --------------------------------------------------------------
    def scan(self) -> dict[str, Any]:
        usage = self.usage()
        desktop = [row for row in usage if not row['packaged']]
        signed = self.signatures(row['app'] for row in desktop)
        runs = self.run_entries()
        findings = []
        for row in usage:
            if row['packaged']:
                verdict, reason = 'ok', 'Store app with its own Windows permission entry.'
                exists, is_signed = True, None
            else:
                exists = os.path.isfile(row['app'])
                is_signed = signed.get(_norm(row['app'])) if exists else None
                verdict, reason = classify(row['app'], signed=is_signed, exists=exists)
            autostarts = [entry for entry in runs if not row['packaged'] and _norm(row['app']) in _norm(entry['command'])]
            findings.append({**row, 'exists': exists, 'signed': is_signed, 'verdict': verdict, 'reason': reason,
                             'autostarts': autostarts, 'selection_id': str(len(findings))})
        findings.sort(key=lambda row: ({'block': 0, 'review': 1, 'ok': 2}[row['verdict']], str(row['last_used'] or '')),
                      reverse=False)
        plan_id = uuid.uuid4().hex
        with self.lock:
            self._plans = {key: value for key, value in self._plans.items()
                           if self.clock() - value['created'] < PLAN_TTL_SECONDS}
            self._plans[plan_id] = {'created': self.clock(), 'findings': findings}
        return {'plan_id': plan_id, 'findings': findings,
                'block_count': sum(row['verdict'] == 'block' for row in findings),
                'review_count': sum(row['verdict'] == 'review' for row in findings),
                'note': 'Nothing was changed. "block" findings can be blocked automatically; "review" ones only on your selection.'}

    # -- block -------------------------------------------------------------
    def _stop_processes(self, path: str) -> int:
        if self.process_iter is None:
            import psutil
            iterator = psutil.process_iter(['exe'])
        else:
            iterator = self.process_iter(['exe'])
        stopped = 0
        for process in iterator:
            try:
                if _norm(process.info.get('exe') or '') == _norm(path):
                    process.kill()
                    stopped += 1
            except Exception:
                continue
        return stopped

    def _registry(self):
        if self.registry is not None:
            return self.registry
        import winreg
        return winreg

    def _deny_consent(self, row: dict[str, Any]) -> Any:
        reg = self._registry()
        hive = reg.HKEY_CURRENT_USER if row['hive'] == 'HKCU' else reg.HKEY_LOCAL_MACHINE
        with reg.OpenKey(hive, row['key'], 0, reg.KEY_READ | reg.KEY_SET_VALUE) as key:
            try:
                previous = reg.QueryValueEx(key, 'Value')[0]
            except OSError:
                previous = None
            reg.SetValueEx(key, 'Value', 0, reg.REG_SZ, 'Deny')
        return previous

    def _disable_autostart(self, entry: dict[str, Any]) -> None:
        reg = self._registry()
        hive = reg.HKEY_CURRENT_USER if entry['hive'] == 'HKCU' else reg.HKEY_LOCAL_MACHINE
        with reg.OpenKey(hive, entry['key'], 0, reg.KEY_SET_VALUE) as key:
            reg.DeleteValue(key, entry['name'])

    def _quarantine(self, path: str, receipt_id: str) -> str:
        target = self.directory / QUARANTINE / receipt_id / (ntpath.basename(path) + '.quarantined')
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(path, target)
        return str(target)

    def _block_one(self, row: dict[str, Any], receipt_id: str) -> dict[str, Any]:
        path, steps = row['app'], {}
        outcome = {'app': path, 'verdict': row['verdict'], 'reason': row['reason'], 'steps': steps}
        try:
            steps['stopped_processes'] = self._stop_processes(path)
            rule = RULE_PREFIX + receipt_id[:8] + ' ' + ntpath.basename(path)
            try:
                self.firewall_block(path, rule)
                steps['firewall_rule'] = rule
            except OSError as error:
                steps['firewall_error'] = str(error)[:200]
            try:
                steps['previous_consent'] = self._deny_consent(row)
                steps['consent_denied'] = True
                steps['consent_key'] = {'hive': row['hive'], 'key': row['key']}
            except OSError as error:
                steps['consent_error'] = str(error)[:200]
            steps['autostarts_removed'] = []
            for entry in row.get('autostarts') or []:
                try:
                    self._disable_autostart(entry)
                    steps['autostarts_removed'].append(entry)
                except OSError as error:
                    steps.setdefault('autostart_errors', []).append(str(error)[:200])
            if os.path.isfile(path):
                steps['quarantined_to'] = self._quarantine(path, receipt_id)
            blocked = 'firewall_rule' in steps or 'quarantined_to' in steps
            outcome['status'] = 'blocked' if blocked else 'failed'
        except Exception as error:  # keep going with the other findings
            outcome.update(status='failed', detail=str(error)[:200])
        return outcome

    def block(self, plan_id: str, selection_ids: Iterable[str] | None = None, *, automatic: bool = False,
              cancel_event: Any = None) -> dict[str, Any]:
        with self.lock:
            plan = self._plans.pop(plan_id, None)
        if plan is None or self.clock() - plan['created'] >= PLAN_TTL_SECONDS:
            raise ValueError('The tracker review expired; scan again.')
        if automatic:
            rows = [row for row in plan['findings'] if row['verdict'] == 'block']
        else:
            selected = {str(value) for value in selection_ids or ()}
            rows = [row for row in plan['findings'] if row['selection_id'] in selected and not row['packaged'] and row['exists']]
        receipt_id = uuid.uuid4().hex
        results = []
        for row in rows:
            if cancel_event is not None and cancel_event.is_set():
                results.append({'app': row['app'], 'status': 'not_changed', 'detail': 'Cancelled before this item.'})
                continue
            results.append(self._block_one(row, receipt_id))
        receipt = self._record(receipt_id, results, automatic=automatic)
        return {'status': receipt['status'], 'receipt_id': receipt_id, 'items': results,
                'note': 'Blocked items are quarantined, firewalled and denied the microphone. Restore undoes every step.'}

    def scan_and_block(self, *, cancel_event: Any = None, **_: Any) -> dict[str, Any]:
        scan = self.scan()
        if not scan['block_count']:
            with self.lock:
                self._plans.pop(scan['plan_id'], None)
            return {'scan': scan, 'blocked': None}
        return {'scan': scan, 'blocked': self.block(scan['plan_id'], automatic=True, cancel_event=cancel_event)}

    # -- receipts / restore --------------------------------------------------
    def _record(self, receipt_id: str, items: list[dict[str, Any]], *, automatic: bool) -> dict[str, Any]:
        from .speed_enhancements import _private_json
        receipt = {'id': receipt_id, 'schema': 1, 'kind': 'Tracker blocking', 'source': 'optimus',
                   'automatic': automatic, 'created_at': _utc(),
                   'status': 'completed' if all(item.get('status') != 'failed' for item in items) else 'partial',
                   'items': items, 'restored': False}
        if items:
            _private_json(self.directory / (receipt_id + '.json'), receipt)
        return receipt

    def history(self) -> list[dict[str, Any]]:
        if not self.directory.exists():
            return []
        rows = []
        for path in sorted(self.directory.glob('*.json')):
            try:
                rows.append(json.loads(path.read_text(encoding='utf-8')))
            except (OSError, ValueError):
                continue
        return sorted(rows, key=lambda row: str(row.get('created_at', '')), reverse=True)

    def restore(self, receipt_id: str) -> dict[str, Any]:
        from .speed_enhancements import _private_json
        if not receipt_id or not all(char in '0123456789abcdef' for char in receipt_id) or len(receipt_id) != 32:
            raise ValueError('Invalid tracker blocking receipt.')
        path = self.directory / (receipt_id + '.json')
        receipt = json.loads(path.read_text(encoding='utf-8'))
        if receipt.get('restored'):
            return {'status': 'already_restored'}
        reg, errors = self._registry(), []
        for item in receipt.get('items') or []:
            steps = item.get('steps') or {}
            try:
                if steps.get('quarantined_to') and os.path.isfile(steps['quarantined_to']) and not os.path.exists(item['app']):
                    os.makedirs(os.path.dirname(item['app']), exist_ok=True)
                    shutil.move(steps['quarantined_to'], item['app'])
                if steps.get('firewall_rule'):
                    self.firewall_unblock(steps['firewall_rule'])
                if steps.get('consent_denied') and steps.get('consent_key'):
                    where = steps['consent_key']
                    hive = reg.HKEY_CURRENT_USER if where['hive'] == 'HKCU' else reg.HKEY_LOCAL_MACHINE
                    with reg.OpenKey(hive, where['key'], 0, reg.KEY_SET_VALUE) as key:
                        if steps.get('previous_consent') is None:
                            try:
                                reg.DeleteValue(key, 'Value')
                            except OSError:
                                pass
                        else:
                            reg.SetValueEx(key, 'Value', 0, reg.REG_SZ, str(steps['previous_consent']))
                for entry in steps.get('autostarts_removed') or []:
                    hive = reg.HKEY_CURRENT_USER if entry['hive'] == 'HKCU' else reg.HKEY_LOCAL_MACHINE
                    with reg.CreateKey(hive, entry['key']) as key:
                        reg.SetValueEx(key, entry['name'], 0, entry.get('type') or reg.REG_SZ, entry['command'])
            except Exception as error:
                errors.append(f"{item.get('app')}: {str(error)[:160]}")
        receipt.update(restored=not errors, restored_at=_utc(), restore_errors=errors)
        _private_json(path, receipt)
        return {'status': 'restored' if not errors else 'partial', 'errors': errors}
