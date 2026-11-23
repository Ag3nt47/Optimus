"""Reviewed cleanup of non-essential Windows background apps.

Scanning never changes a process. Only processes that match a known
non-essential rule *and* the rule's exact executable location are stoppable,
and only after the caller selects them from a plan produced by ``scan``. Each
stop rechecks PID, creation time and executable before terminating, and a
private receipt is written to ``<data>/background-apps`` so Change History
shows what was done. Startup registrations are reported, never edited.

Hidden script watchers (PowerShell/WScript running a script with a hidden
window) are reported as review-only findings: they are usually something the
user installed on purpose, so Optimus never stops them automatically.
"""
from __future__ import annotations

from datetime import datetime, timezone
import ntpath
import os
from pathlib import Path
import threading
import time
import uuid
from typing import Any, Callable, Iterable

import psutil

from .app_removal_protection import is_windows_app_path

RECEIPT_FOLDER = 'background-apps'
PLAN_TTL_SECONDS = 15 * 60
WATCHER_REVIEW_BYTES = 150 * 1024 * 1024
_SCRIPT_HOSTS = {'powershell.exe', 'pwsh.exe', 'wscript.exe', 'cscript.exe'}


def _norm(path: str) -> str:
    return ntpath.normcase(ntpath.normpath(str(path or '')))


def _temp_roots() -> list[str]:
    roots = {os.environ.get(name, '') for name in ('TEMP', 'TMP')}
    local = os.environ.get('LOCALAPPDATA', '')
    if local:
        roots.add(ntpath.join(local, 'Temp'))
    return [_norm(root) for root in roots if root]


def _in_windows_apps(exe: str, package_prefix: str) -> bool:
    parts = _norm(exe).split('\\')
    try:
        index = parts.index('windowsapps')
    except ValueError:
        return False
    return len(parts) > index + 1 and parts[index + 1].startswith(package_prefix.casefold())


def _in_system_apps(exe: str, package_prefix: str) -> bool:
    parts = _norm(exe).split('\\')
    try:
        index = parts.index('systemapps')
    except ValueError:
        return False
    return len(parts) > index + 1 and parts[index + 1].startswith(package_prefix.casefold())


def _in_temp(exe: str) -> bool:
    folder = ntpath.dirname(_norm(exe))
    for root in _temp_roots():
        if folder == root or folder.startswith(root + '\\'):
            return True
    # Temp folders relocated by the user (e.g. D:\...\Temp\bwp...) keep a Temp segment.
    return '\\temp\\' in folder + '\\'


# Each rule: (rule id, image name, location check, category, explanation).
RULES: tuple[dict[str, Any], ...] = (
    {'id': 'bing-wallpaper', 'image': 'bingwallpaper.exe',
     'location': lambda exe: _in_windows_apps(exe, 'Microsoft.BingWallpaper_'),
     'category': 'Wallpaper', 'startup': 'BingWallpaperDaemon',
     'reason': 'Bing Wallpaper only rotates the desktop image; it can use 150 MB+ in the background.'},
    {'id': 'bing-wallpaper-daemon', 'image': 'uninstdaemon.exe',
     'location': _in_temp,
     'category': 'Wallpaper', 'startup': 'BingWallpaperDaemon',
     'reason': 'Bing Wallpaper helper daemon launched from a Temp folder at sign-in.'},
    {'id': 'phone-link', 'image': 'phoneexperiencehost.exe',
     'location': lambda exe: _in_windows_apps(exe, 'Microsoft.YourPhone_'),
     'category': 'Phone Link', 'startup': None,
     'reason': 'Phone Link background host; not needed unless a phone is being used with this PC.'},
    {'id': 'widgets-board', 'image': 'widgetboard.exe',
     'location': lambda exe: _in_windows_apps(exe, 'MicrosoftWindows.Client.WebExperience_'),
     'category': 'Widgets', 'startup': None,
     'reason': 'Taskbar Widgets board; Windows restarts it when Widgets is opened.'},
    {'id': 'widgets-service', 'image': 'widgetservice.exe',
     'location': lambda exe: _in_windows_apps(exe, 'Microsoft.WidgetsPlatformRuntime_'),
     'category': 'Widgets', 'startup': None,
     'reason': 'Widgets platform runtime; Windows restarts it when Widgets is opened.'},
    {'id': 'start-feed', 'image': 'microsoftstartfeedprovider.exe',
     'location': lambda exe: _in_windows_apps(exe, 'Microsoft.StartExperiencesApp_'),
     'category': 'Widgets', 'startup': None,
     'reason': 'Microsoft Start news feed provider for Widgets.'},
    {'id': 'cross-device-resume', 'image': 'crossdeviceresume.exe',
     'location': lambda exe: _in_system_apps(exe, 'MicrosoftWindows.Client.CBS_'),
     'category': 'Cross-device', 'startup': None,
     'reason': 'Cross-device resume helper; only used to continue phone activities on this PC.'},
)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe(action: Callable[[], Any], default: Any = None) -> Any:
    try:
        return action()
    except (psutil.Error, OSError, AttributeError, ValueError):
        return default


def _match_rule(name: str, exe: str) -> dict[str, Any] | None:
    name = (name or '').casefold()
    for rule in RULES:
        if rule['image'] == name and exe and rule['location'](exe):
            return rule
    return None


def _script_name(cmdline: list[str]) -> str:
    for argument in cmdline[1:]:
        base = ntpath.basename(str(argument).strip('"'))
        if base.casefold().endswith(('.ps1', '.vbs', '.js')):
            return base
    return ''


def _hidden(cmdline: list[str]) -> bool:
    lowered = [str(arg).casefold() for arg in cmdline]
    for index, arg in enumerate(lowered):
        if arg in ('-windowstyle', '-w', '-win') and index + 1 < len(lowered) and lowered[index + 1] == 'hidden':
            return True
    return any(arg in ('//b', '/b') for arg in lowered)


def scan_processes(process_iter: Callable[..., Iterable[Any]] = psutil.process_iter,
                   own_pid: int | None = None) -> list[dict[str, Any]]:
    """Return findings without changing anything."""
    own_pid = os.getpid() if own_pid is None else own_pid
    findings: list[dict[str, Any]] = []
    pids = None
    for process in process_iter():
        try:
            pid = int(process.pid)
            if pid == own_pid:
                continue
            name = _safe(process.name, '') or ''
            exe = _safe(process.exe, '') or ''
            memory = _safe(process.memory_info)
            rss = int(getattr(memory, 'rss', 0) or 0)
            created = _safe(lambda: float(process.create_time()))
            base = {'pid': pid, 'name': name, 'exe': exe, 'create_time': created, 'memory_rss': rss}
            rule = _match_rule(name, exe)
            if rule is not None:
                findings.append(dict(base, rule=rule['id'], category=rule['category'], reason=rule['reason'],
                                     startup_entry=rule['startup'], stoppable=True,
                                     returns='Returns at next sign-in or when the feature is opened.'))
                continue
            if name.casefold() not in _SCRIPT_HOSTS:
                continue
            cmdline = _safe(process.cmdline, []) or []
            script = _script_name(cmdline)
            ppid = _safe(process.ppid)
            if pids is None:
                pids = set(_safe(psutil.pids, []) or [])
            orphaned = ppid is not None and ppid not in pids
            hidden = _hidden(cmdline)
            if not cmdline:
                note = 'Command line unreadable (often an elevated task); identify it in Task Manager before acting.'
                if not orphaned:
                    continue
            elif not (hidden and script) and not orphaned:
                continue
            else:
                note = ('Hidden script watcher; usually installed on purpose.' if hidden and script
                        else 'Script host process.')
            if rss < WATCHER_REVIEW_BYTES and not orphaned:
                continue
            if rss >= WATCHER_REVIEW_BYTES:
                note += ' Memory use is unusually large for a watcher script.'
            if orphaned:
                note += ' Its parent process has exited.'
            findings.append(dict(base, rule='script-watcher', category='Script watcher', script_name=script,
                                 reason=note, startup_entry=None, stoppable=False,
                                 returns='Review only; Optimus does not stop script watchers.'))
        except (psutil.Error, OSError, ValueError):
            continue
    return sorted(findings, key=lambda row: row['memory_rss'], reverse=True)


class BackgroundApps:
    """Scan, plan and reviewed stop of non-essential background apps."""

    def __init__(self, backend: Any = None, *, data_dir: str | os.PathLike[str] | None = None,
                 process_iter: Callable[..., Iterable[Any]] = psutil.process_iter,
                 process_factory: Callable[[int], Any] = psutil.Process,
                 clock: Callable[[], float] = time.monotonic):
        root = data_dir if data_dir is not None else getattr(backend, 'data_dir', None)
        if root is None:
            raise ValueError('A data directory is required for background app receipts.')
        self.root = Path(root)
        self.directory = self.root / RECEIPT_FOLDER
        self.process_iter, self.process_factory, self.clock = process_iter, process_factory, clock
        self.lock, self._plans = threading.RLock(), {}

    def scan(self) -> dict[str, Any]:
        findings = scan_processes(self.process_iter)
        plan_id = uuid.uuid4().hex
        for index, row in enumerate(findings):
            row['selection_id'] = str(index)
        with self.lock:
            self._plans = {key: value for key, value in self._plans.items()
                           if self.clock() - value['created'] < PLAN_TTL_SECONDS}
            self._plans[plan_id] = {'created': self.clock(), 'findings': findings}
        stoppable = [row for row in findings if row['stoppable']]
        return {'plan_id': plan_id, 'findings': findings,
                'stoppable_bytes': sum(row['memory_rss'] for row in stoppable),
                'startup_entries': sorted({row['startup_entry'] for row in findings if row.get('startup_entry')}),
                'note': 'Nothing was changed. Select stoppable items to end them; startup entries are only reported.'}

    def _verify(self, row: dict[str, Any]) -> Any:
        if is_windows_app_path(row.get('exe')):
            raise ValueError('Windows App remote access is protected from process changes.')
        process = self.process_factory(int(row['pid']))
        created = float(process.create_time())
        if row.get('create_time') is None or abs(created - float(row['create_time'])) > 0.01:
            raise ValueError('The process ended and its PID was reused.')
        if is_windows_app_path(process.exe()):
            raise ValueError('Windows App remote access is protected from process changes.')
        if _norm(process.exe()) != _norm(row['exe']) or _match_rule(process.name(), process.exe()) is None:
            raise ValueError('The process no longer matches a reviewed non-essential app.')
        return process

    def stop(self, plan_id: str, selection_ids: Iterable[str], *, cancel_event: Any = None) -> dict[str, Any]:
        with self.lock:
            plan = self._plans.pop(plan_id, None)
        if plan is None or self.clock() - plan['created'] >= PLAN_TTL_SECONDS:
            raise ValueError('The background app review expired; scan again.')
        selected = {str(value) for value in selection_ids}
        rows = [row for row in plan['findings'] if row['selection_id'] in selected]
        if not rows:
            raise ValueError('No reviewed background apps were selected.')
        results = []
        for row in rows:
            outcome = {'pid': row['pid'], 'name': row['name'], 'rule': row['rule'], 'memory_rss': row['memory_rss']}
            if cancel_event is not None and cancel_event.is_set():
                outcome.update(status='not_changed', detail='Cancelled before this item.')
            elif not row.get('stoppable'):
                outcome.update(status='skipped', detail='Review-only finding; not stopped.')
            else:
                try:
                    process = self._verify(row)
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except psutil.TimeoutExpired:
                        process.kill()
                    outcome.update(status='stopped', detail='Ended after identity verification.')
                except psutil.NoSuchProcess:
                    outcome.update(status='not_changed', detail='Already exited.')
                except (psutil.Error, OSError, ValueError) as error:
                    outcome.update(status='failed', detail=str(error)[:180])
            results.append(outcome)
        receipt = record_receipt(self.root, results, source='optimus')
        return {'status': receipt['status'], 'receipt_id': receipt['id'], 'items': results,
                'note': 'Stopped apps return at next sign-in unless their startup entry is disabled.',
                'freed_bytes': sum(row['memory_rss'] for row in results if row['status'] == 'stopped')}

    def history(self) -> list[dict[str, Any]]:
        if not self.directory.exists():
            return []
        import json
        rows = []
        for path in sorted(self.directory.glob('*.json')):
            try:
                rows.append(json.loads(path.read_text(encoding='utf-8')))
            except (OSError, ValueError):
                continue
        return sorted(rows, key=lambda row: str(row.get('created_at', '')), reverse=True)


def record_receipt(data_dir: str | os.PathLike[str], items: list[dict[str, Any]], *, source: str) -> dict[str, Any]:
    """Write a private receipt that Change History lists as a recorded operation.

    ``source`` names who performed the cleanup (Optimus itself, or an outside
    tool/session) so later reviews can tell the difference.
    """
    from .speed_enhancements import _private_json
    stopped = [row for row in items if row.get('status') == 'stopped']
    receipt = {
        'id': uuid.uuid4().hex, 'schema': 1, 'kind': 'Background app cleanup', 'source': str(source)[:64],
        'created_at': _utc(), 'status': 'completed' if all(row.get('status') != 'failed' for row in items) else 'partial',
        'freed_bytes': sum(int(row.get('memory_rss') or 0) for row in stopped),
        'items': [{key: row.get(key) for key in ('name', 'pid', 'rule', 'memory_rss', 'status', 'detail')} for row in items],
    }
    path = Path(data_dir) / RECEIPT_FOLDER / (receipt['id'] + '.json')
    _private_json(path, receipt)
    return receipt
