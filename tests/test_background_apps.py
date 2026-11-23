"""Background app cleanup uses only fake processes and a disposable data folder."""
import json
from types import SimpleNamespace

import psutil
import pytest

from optimus import background_apps as module
from optimus import change_history

APPS = r'C:\Program Files\WindowsApps'


class FakeProcess:
    def __init__(self, pid, name, exe, rss, *, cmdline=None, ppid=4, create_time=100.0):
        self.pid, self._name, self._exe, self._rss = pid, name, exe, rss
        self._cmdline, self._ppid, self._create = cmdline or [exe], ppid, create_time
        self.terminated = self.killed = False

    def name(self): return self._name
    def exe(self): return self._exe
    def memory_info(self): return SimpleNamespace(rss=self._rss)
    def create_time(self): return self._create
    def cmdline(self): return self._cmdline
    def ppid(self): return self._ppid
    def terminate(self): self.terminated = True
    def wait(self, timeout=None): return 0
    def kill(self): self.killed = True


MB = 1024 * 1024


def fixtures():
    return [
        FakeProcess(10, 'BingWallpaper.exe', APPS + r'\Microsoft.BingWallpaper_1.1.474.0_x86__8wekyb3d8bbwe\BingWallpaper.exe', 173 * MB),
        FakeProcess(11, 'UnInstDaemon.exe', r'D:\Agent47\SystemTempGuard\Temp\bwpd76f0e12\UnInstDaemon.exe', 35 * MB),
        FakeProcess(12, 'PhoneExperienceHost.exe', APPS + r'\Microsoft.YourPhone_1.26101.118.0_x64__8wekyb3d8bbwe\PhoneExperienceHost.exe', 151 * MB),
        FakeProcess(13, 'WidgetBoard.exe', APPS + r'\MicrosoftWindows.Client.WebExperience_526.0_x64__cw5n1h2txyewy\WidgetBoard.exe', 28 * MB),
        FakeProcess(14, 'CrossDeviceResume.exe', r'C:\WINDOWS\SystemApps\MicrosoftWindows.Client.CBS_cw5n1h2txyewy\CrossDeviceResume.exe', 48 * MB),
        # Same image name outside the expected location must not be stoppable.
        FakeProcess(15, 'BingWallpaper.exe', r'C:\Users\x\Downloads\BingWallpaper.exe', 50 * MB),
        FakeProcess(16, 'powershell.exe', r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe', 229 * MB,
                    cmdline=['powershell.exe', '-WindowStyle', 'Hidden', '-File', r'C:\Games\Watch-Skyrim.ps1']),
        FakeProcess(17, 'powershell.exe', r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe', 20 * MB,
                    cmdline=['powershell.exe', '-WindowStyle', 'Hidden', '-File', r'C:\small.ps1']),
        FakeProcess(18, 'chrome.exe', r'C:\Program Files\Google\Chrome\Application\chrome.exe', 900 * MB),
    ]


@pytest.fixture(autouse=True)
def known_pids(monkeypatch):
    monkeypatch.setattr(module.psutil, 'pids', lambda: [4, 10, 11, 12, 13, 14, 15, 16, 17, 18])


def test_scan_finds_known_apps_and_flags_watchers_without_stopping():
    processes = fixtures()
    rows = module.scan_processes(lambda: processes, own_pid=1)
    by_pid = {row['pid']: row for row in rows}
    assert {10, 11, 12, 13, 14} <= {pid for pid, row in by_pid.items() if row['stoppable']}
    assert 15 not in by_pid and 18 not in by_pid and 17 not in by_pid
    assert by_pid[16]['stoppable'] is False and by_pid[16]['script_name'] == 'Watch-Skyrim.ps1'
    assert 'unusually large' in by_pid[16]['reason']
    assert not any(process.terminated for process in processes)


def test_orphaned_unreadable_script_host_is_review_only():
    orphan = FakeProcess(30, 'powershell.exe', r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe', 60 * MB,
                         cmdline=[], ppid=9999)
    orphan.cmdline = lambda: (_ for _ in ()).throw(psutil.AccessDenied(30))
    rows = module.scan_processes(lambda: [orphan], own_pid=1)
    assert len(rows) == 1 and rows[0]['stoppable'] is False
    assert 'parent process has exited' in rows[0]['reason']


def test_stop_verifies_identity_writes_receipt_and_history_lists_it(tmp_path):
    processes = {process.pid: process for process in fixtures()}
    service = module.BackgroundApps(data_dir=tmp_path, process_iter=lambda: list(processes.values()),
                                    process_factory=lambda pid: processes[pid])
    plan = service.scan()
    assert plan['startup_entries'] == ['BingWallpaperDaemon']
    selection = [row['selection_id'] for row in plan['findings'] if row['pid'] in (10, 12, 16)]
    processes[12]._create = 999.0  # PID reused since review.
    result = service.stop(plan['plan_id'], selection)
    status = {row['pid']: row['status'] for row in result['items']}
    assert status == {10: 'stopped', 12: 'failed', 16: 'skipped'}
    assert processes[10].terminated and not processes[12].terminated and not processes[16].terminated
    assert result['freed_bytes'] == 173 * MB and result['status'] == 'partial'

    receipt = json.loads((tmp_path / module.RECEIPT_FOLDER / (result['receipt_id'] + '.json')).read_text())
    assert receipt['kind'] == 'Background app cleanup' and receipt['source'] == 'optimus'
    assert service.history()[0]['id'] == result['receipt_id']
    assert 'background-apps' in change_history._FOLDERS

    with pytest.raises(ValueError):
        service.stop(plan['plan_id'], selection)  # A plan is single-use.


def test_cancel_stops_nothing(tmp_path):
    processes = {process.pid: process for process in fixtures()}
    service = module.BackgroundApps(data_dir=tmp_path, process_iter=lambda: list(processes.values()),
                                    process_factory=lambda pid: processes[pid])
    plan = service.scan()
    ids = [row['selection_id'] for row in plan['findings'] if row['stoppable']]
    result = service.stop(plan['plan_id'], ids, cancel_event=SimpleNamespace(is_set=lambda: True))
    assert all(row['status'] == 'not_changed' for row in result['items'])
    assert not any(process.terminated for process in processes.values())


def test_external_receipt_is_recorded(tmp_path):
    receipt = module.record_receipt(tmp_path, [
        {'name': 'BingWallpaper.exe', 'pid': 1, 'rule': 'bing-wallpaper', 'memory_rss': 5, 'status': 'stopped'}],
        source='claude-code-session')
    saved = json.loads((tmp_path / 'background-apps' / (receipt['id'] + '.json')).read_text())
    assert saved['freed_bytes'] == 5 and saved['status'] == 'completed'
