"""Native macOS/Linux application services; Windows remains a separate backend."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import platform
import re
import shutil
import sys
import threading
import time
import uuid

import psutil

from .backend import Backend
from .cleaner import Cleaner
from .filesystem import default_data_root, resolve_data_root
from .native_operations import (NativeFiles, NativePackages, NativeRunner,
                                NativeStartup, UnsupportedOperation, protected_startup)
from .sampler import ResourceSampler
from .workflow import atomic_json
from . import performance


class _UnavailableBridge:
    def request(self, *args, **kwargs):
        raise UnsupportedOperation('This operation belongs to the Windows engine and is unavailable on this operating system.')

    def close(self):
        pass


def _application(executable, fallback=''):
    parts = Path(executable).parts if executable else ()
    bundle = next((part[:-4] for part in parts if part.endswith('.app')), None)
    return bundle or Path(executable).name or str(fallback) or 'Unidentified application'


class NativeProcessHarness:
    """All visible processes and explicit exact-lifetime optional actions."""
    def __init__(self, backend):
        self.backend = backend
        self._plans = {}
        self._reports = {}
        self._lock = threading.RLock()

    def scan(self, cancel_event=None):
        created = time.time()
        report = {'id': uuid.uuid4().hex, 'created_at': created, 'expires_at': created + 900,
                  'complete': True, 'cancelled': False, 'errors': [], 'processes': [], 'actions': [],
                  'notes': ['Read-only scan; no process or startup action is preselected.',
                            'Memory, process age and a helper name do not establish that a program is unnecessary.',
                            'Parent and startup matches are current evidence, not proof of historical launch source.',
                            'Process command-line arguments are omitted from exported reports.',
                            'Application removal remains a separate native package-manager decision.']}
        private = {}
        processes = self.backend.processes()
        by_pid = {row['pid']: row for row in processes}
        entries = self.backend.startup_list()
        for source in processes:
            if cancel_event is not None and cancel_event.is_set():
                report.update(complete=False, cancelled=True)
                break
            record = copy.deepcopy(source)
            exe = record.get('exe', '')
            protected = record.get('protected') is not False or record.get('can_sleep') is not True
            parent = by_pid.get(record.get('ppid'))
            if parent and (not parent.get('create_time') or not record.get('create_time') or parent['create_time'] > record['create_time']):
                parent = None
            parent = {key: parent.get(key) for key in ('pid', 'name', 'exe')} if parent else None
            matches = [row for row in entries if row.get('Enabled') is True and row.get('Program') and
                       os.path.isabs(row['Program']) and os.path.normpath(row['Program']) == os.path.normpath(exe)]
            evidence = []
            if parent:
                evidence.append('The current recorded parent is ' + str(parent['name']) + '; its lifetime predates this child.')
            if matches:
                evidence.append('The executable exactly matches enabled user startup registration(s): ' + ', '.join(row['Name'] for row in matches) + '.')
            row = {key: record.get(key) for key in ('pid', 'ppid', 'create_time', 'name', 'exe', 'memory_rss', 'private_bytes')}
            row.update(application=_application(exe, record.get('name')), owners=[],
                       ownership_evidence='Application bundle containment or executable filename; native package ownership is available in process details. This label is not a claim that the process is unnecessary.',
                       parent=parent, protected=protected,
                       protection_reason=record.get('reason', '') if protected else '',
                       why_running=' '.join(evidence) or 'The historical launch source is unknown from this snapshot.',
                       why_running_confidence='evidence' if evidence else 'unknown',
                       startup_matches=[{'name': item['Name'], 'source': item['Source']} for item in matches],
                       measurement_available=False, metrics=None,
                       recommendation='Protected or identity unavailable: details only.' if protected else 'Save work and review this program. Reversible Sleep is available only when explicitly selected.')
            report['processes'].append(row)
            if not protected:
                action = {'id': uuid.uuid4().hex, 'kind': 'sleep_process', 'selected': False,
                          'label': 'Sleep ' + str(row['name']) + ' (PID ' + str(row['pid']) + ')',
                          'target': str(row['exe']), 'reason': 'Pause this exact owned process lifetime; Wake All reverses the pause. Save work first.'}
                report['actions'].append(action)
                private[action['id']] = {'kind': 'sleep_process', 'record': record}
        for record in entries:
            if record.get('Enabled') is not True or record.get('CanDisable') is not True or protected_startup(record):
                continue
            action = {'id': uuid.uuid4().hex, 'kind': 'disable_startup', 'selected': False,
                      'label': 'Disable sign-in launch: ' + record['Name'], 'target': record['Path'],
                      'reason': 'Disable this exact user startup file with a recovery backup. This does not uninstall the application.'}
            report['actions'].append(action)
            private[action['id']] = {'kind': 'disable_startup', 'record': copy.deepcopy(record)}
        report['summary'] = {'processes': len(report['processes']), 'protected': sum(row['protected'] for row in report['processes']),
                             'optional_actions': len(report['actions']), 'measured': 0}
        report['identity_coverage_complete'] = all(row.get('exe') and row.get('create_time') is not None for row in report['processes'])
        report['unavailable_identities'] = sum(not row.get('exe') or row.get('create_time') is None for row in report['processes'])
        with self._lock:
            self._plans = {key: value for key, value in self._plans.items() if value['expires_at'] >= created}
            while len(self._plans) >= 10:
                del self._plans[next(iter(self._plans))]
            self._plans[report['id']] = {'created_at': created, 'expires_at': report['expires_at'],
                                         'complete': report['complete'], 'cancelled': report['cancelled'], 'errors': list(report['errors']), 'actions': private}
            self._reports[report['id']] = copy.deepcopy(report)
            while len(self._reports) > 20:
                del self._reports[next(iter(self._reports))]
        return report

    def apply(self, request, cancel_event=None):
        if not isinstance(request, dict) or request.get('confirmed') is not True:
            raise ValueError('Explicit review and confirmation of selected actions is required.')
        identity = request.get('plan_id', request.get('id'))
        selected = request.get('selected_ids', request.get('action_ids', request.get('actions', [])))
        if not isinstance(selected, list) or not 1 <= len(selected) <= 25 or any(not isinstance(item, str) or not re.fullmatch('[0-9a-f]{32}', item) for item in selected) or len(selected) != len(set(selected)):
            raise ValueError('Select at most 25 unique current action identities.')
        with self._lock:
            plan = self._plans.get(identity)
            if not plan or not plan['created_at'] <= time.time() <= plan['expires_at']:
                raise ValueError('This process scan expired. Scan again before applying changes.')
            if plan['complete'] is not True or plan['cancelled'] or plan['errors']:
                raise ValueError('The process scan was incomplete or cancelled. Scan again before applying changes.')
            if any(not isinstance(item, str) or item not in plan['actions'] for item in selected):
                raise ValueError('The selected action does not belong to this scan.')
            del self._plans[identity]  # Single-use authority; retry needs a fresh scan.
            results = []
            journal = Path(self.backend.data_dir) / 'process-harness/actions' / (uuid.uuid4().hex + '.json')
            receipt = {'plan_id': identity, 'selected_ids': list(selected), 'status': 'prepared', 'items': results}
            atomic_json(journal, receipt)
            for action_id in selected:
                if cancel_event is not None and cancel_event.is_set():
                    break
                action = plan['actions'].pop(action_id)
                try:
                    if action['kind'] == 'sleep_process':
                        expected = action['record']
                        fresh = self.backend.processes()
                        matches = [row for row in fresh if all(row.get(key) == expected.get(key) for key in ('pid', 'create_time', 'exe'))]
                        if len(matches) != 1 or matches[0].get('protected') is not False or matches[0].get('can_sleep') is not True:
                            raise ValueError('The selected process ended, changed or is now protected.')
                    if cancel_event is not None and cancel_event.is_set():
                        break
                    result = (self.backend.sleep_process(action['record']) if action['kind'] == 'sleep_process'
                              else self.backend.startup_set(action['record'], False))
                    results.append({'id': action_id, 'status': 'completed', 'result': result})
                except Exception as error:
                    results.append({'id': action_id, 'status': 'failed', 'error': str(error)})
                atomic_json(journal, receipt)
            receipt.update(status='completed', failed_items=sum(row['status'] == 'failed' for row in results),
                           cancelled=cancel_event is not None and cancel_event.is_set(), log_path=str(journal))
            atomic_json(journal, receipt)
            return receipt

    def export(self, identity, destination):
        with self._lock:
            if identity not in self._reports:
                raise ValueError('Choose a process report from this application session.')
            destination = Path(destination)
            if destination.suffix.casefold() == '.json':
                path = destination
                path.parent.mkdir(parents=True, exist_ok=True)
            else:
                destination.mkdir(parents=True, exist_ok=True)
                path = destination / ('process-scan-' + identity + '.json')
            atomic_json(path, self._reports[identity])
            return {'path': str(path), 'read_only': True}


class NativeBackend(Backend):
    def __init__(self, data_dir=None):
        if sys.platform not in ('darwin', 'linux'):
            # Tests may construct this class with mocked platforms; actual users
            # enter via the factory which preserves the Windows backend.
            raise UnsupportedOperation('The native backend supports macOS and Linux.')
        self.system = sys.platform
        self.data_dir = resolve_data_root(Path(data_dir)) if data_dir is not None else default_data_root()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.bridge = _UnavailableBridge()
        self.cleaner = Cleaner(self.data_dir)
        self.sampler = ResourceSampler()
        from .native_settings import NativePreferences
        self.preference_store = NativePreferences(self.data_dir)
        self.performance_controller = performance.PerformanceController(self.data_dir / 'suspended-processes.json')
        self.native_runner = NativeRunner(self.system)
        self.native_packages = NativePackages(self.data_dir, system=self.system, runner=self.native_runner)
        self.native_files = NativeFiles(data_dir=self.data_dir)
        self.native_startup = NativeStartup(self.data_dir, system=self.system, runner=self.native_runner)
        from .browser_privacy import BrowserPrivacy
        from .browser_cleanup import BrowserSessionController
        self.browser_privacy_engine = BrowserPrivacy(self.data_dir)
        self.browser_sessions = BrowserSessionController()
        self.process_harness = NativeProcessHarness(self)
        from .native_workflow import NativeWorkflow
        self.workflow = NativeWorkflow(self)
        self.last_audit = None

    def capabilities(self):
        supported = ['audit', 'process-inventory', 'process-harness', 'resource-sampling', 'process-sleep',
                     'process-priority', 'cleanup', 'browser-cleanup', 'storage', 'duplicates',
                     'native-startup', 'startup-launch', 'installed-apps', 'software-update-scan',
                     'security-status', 'kernel-driver-status', 'outcome-verification', 'speed-enhancements', 'sandbox']
        if self.system == 'linux':
            supported.append('process-affinity')
        if self.system == 'darwin':
            supported.extend(['os-update-scan', 'os-update-install'])
        if self.native_packages.status()['automatic_install_supported']:
            supported.append('software-update-install')
        unavailable = {'windows-registry': 'Windows Registry does not exist on this platform.',
                       'windows-consumer-debloat': 'Windows AppX, Edge and Microsoft 365 removal scripts do not apply to this platform.',
                       'independent-driver-install': 'Kernel, firmware and hardware support come from the OS/vendor update system. Universal third-party driver replacement is not supported.',
                       'arbitrary-system-service-mutation': 'Service inspection is available; system service deletion or automatic disabling is not supported.',
                       'universal-spyware-removal': 'Optimus cannot certify a clean system or replace native security software.',
                       'cloud-signin': 'The Windows DPAPI cloud token store is not used on macOS/Linux. Cloud mutations are disabled until a native encrypted token-store implementation is available.',
                       'power-plans': 'Windows power plan GUIDs do not apply to this platform.',
                       'scheduled-audit': 'App-open daily/weekly cadence and launch-at-sign-in are supported. Native background audit scheduling is not installed.'}
        unavailable['codex-priority-profile'] = 'The Windows Codex installation-path identity is not reused on macOS/Linux. Select an exact owned application in process controls instead; permission to increase Unix priority remains controlled by the OS.'
        return {'platform': self.system, 'system': platform.system(), 'supported': supported,
                'unavailable': unavailable, 'package_manager': self.native_packages.status(),
                'note': 'Each action is limited to capabilities supported by the current OS and available native tools. Unsupported Windows operations are not reported as completed.'}

    def source_root(self):
        if os.environ.get('OPTIMUS_ORIGINAL_SOURCE'):
            return Path(os.environ['OPTIMUS_ORIGINAL_SOURCE'])
        if getattr(sys, 'frozen', False):
            executable = Path(sys.executable)
            if self.system == 'darwin' and executable.parent.name == 'MacOS':
                return executable.parent.parent / 'Resources/source'
            return executable.parent / 'source'
        return Path(__file__).parents[2]

    def _startup_launch_service(self):
        from .native_startup_registration import NativeStartupRegistration
        if not hasattr(self, '_startup_registration'):
            self._startup_registration = NativeStartupRegistration(self.data_dir)
        return self._startup_registration

    def elevate(self):
        raise UnsupportedOperation('Optimus runs as your normal user. The operating system requests administrator authorization separately for each approved package installation.')

    def is_admin(self):
        return hasattr(os, 'geteuid') and os.geteuid() == 0

    def audit(self):
        from . import __version__
        memory = psutil.virtual_memory()
        disks, errors = [], []
        seen = set()
        for partition in psutil.disk_partitions(all=False):
            if partition.mountpoint in seen:
                continue
            seen.add(partition.mountpoint)
            try:
                usage = psutil.disk_usage(partition.mountpoint)
                disks.append({'Drive': partition.mountpoint, 'Device': partition.device, 'FileSystem': partition.fstype,
                              'Size': usage.total, 'FreeSpace': usage.free, 'UsedBytes': usage.used,
                              'FreeGB': round(usage.free / 1024 ** 3, 2), 'SizeGB': round(usage.total / 1024 ** 3, 2)})
            except (OSError, psutil.Error) as error:
                errors.append('Disk status unavailable: ' + partition.mountpoint + ' (' + type(error).__name__ + ').')
        processes = self.processes()
        report = {'ToolVersion': __version__, 'ApplicationVersion': __version__, 'Platform': self.system,
                  'ComputerName': platform.node(), 'OS': platform.platform(), 'Kernel': platform.release(),
                  'CPU': {'Name': platform.processor() or platform.machine(), 'LogicalProcessors': psutil.cpu_count(), 'PhysicalCores': psutil.cpu_count(logical=False)},
                  'Memory': {'TotalBytes': memory.total, 'AvailableBytes': memory.available, 'UsedPercent': memory.percent,
                             'TotalGB': round(memory.total / 1024 ** 3, 2), 'AvailableGB': round(memory.available / 1024 ** 3, 2)},
                  'Disks': disks, 'Processes': processes, 'ProcessCount': len(processes),
                  'Startup': self.startup_list(), 'Capabilities': self.capabilities(), 'errors': errors,
                  'complete': not errors, 'read_only': True, 'timestamp': time.time(),
                  'note': 'Protected or inaccessible process details may be unavailable under this user account. A process snapshot does not establish that a program is unnecessary.'}
        self.last_audit = report
        return report

    def process_details(self, record):
        details = performance.get_process_details(record)
        exe = record.get('exe', '')
        details['application'] = _application(exe, record.get('name'))
        evidence = {'note': 'Current parent and native package ownership are evidence, not a verified historical launch reason.', 'package_owner': None}
        if exe and Path(exe).is_absolute():
            manager = self.native_packages.manager
            command = (['/usr/bin/dpkg-query', '-S', '--', exe] if manager == 'apt' else
                       ['/usr/bin/rpm', '-qf', '--', exe] if manager == 'dnf' else
                       ['/usr/bin/pacman', '-Qo', '--', exe] if manager == 'pacman' else None)
            if command:
                result = self.native_runner.run(command, timeout=15)
                if result['exitCode'] == 0:
                    evidence['package_owner'] = result['stdout'].strip()[:2000]
        details['launch_evidence'] = evidence
        return details

    def storage_scan(self, root):
        return self.native_files.storage(root)

    def duplicate_scan(self, root):
        return self.native_files.duplicates(root)

    def duplicate_recycle(self, record):
        return self.native_files.recycle(record)

    def duplicate_recycle_approved(self, record):
        return self.native_files.recycle(record)

    def startup_list(self):
        return self.native_startup.list()

    def startup_set(self, record, enabled):
        return self.native_startup.set(record, enabled)

    def startup_remove(self, record):
        raise UnsupportedOperation('Disable the selected user startup registration instead. Optimus preserves a recovery backup rather than deleting it.')

    def services(self):
        if self.system == 'darwin':
            result = self.native_runner.run(['/bin/launchctl', 'list'])
            if result['exitCode'] != 0:
                raise RuntimeError('macOS user services could not be read.')
            return [{'Name': parts[2], 'PID': parts[0], 'Status': parts[1], 'Manageable': False}
                    for line in result['stdout'].splitlines()[1:] if len(parts := line.split(None, 2)) == 3]
        executable = Path('/usr/bin/systemctl')
        if not executable.is_file():
            raise UnsupportedOperation('This Linux system does not expose systemd user-service inventory.')
        result = self.native_runner.run([str(executable), '--user', '--no-pager', '--plain', '--no-legend', 'list-units', '--type=service', '--all'])
        if result['exitCode'] != 0:
            raise RuntimeError('The user service manager is unavailable in this session.')
        return [{'Name': parts[0], 'LoadState': parts[1], 'State': parts[2], 'Status': parts[3],
                 'Description': parts[4] if len(parts) > 4 else '', 'Manageable': False}
                for line in result['stdout'].splitlines() if len(parts := line.split(None, 4)) >= 4]

    def service_set(self, record, action):
        raise UnsupportedOperation('Native system services are read-only in Optimus. Use the operating-system service manager for reviewed changes.')

    def installed_apps(self):
        records = self.native_packages.installed()
        if self.system == 'darwin':
            known = {row.get('Name', '').casefold() for row in records}
            for root in (Path('/Applications'), Path.home() / 'Applications'):
                if root.is_dir():
                    for path in root.glob('*.app'):
                        if path.stem.casefold() not in known:
                            records.append({'Identity': 'app-bundle:' + str(path), 'Name': path.stem,
                                            'InstallLocation': str(path), 'Version': 'Unknown',
                                            'source': 'macOS application bundle', 'review_only': True})
        return records

    def uninstall(self, record):
        current = self.installed_apps()
        matches = [item for item in current if item.get('Identity') == record.get('Identity') and item.get('Name') == record.get('Name')]
        if len(matches) != 1:
            raise ValueError('This installed application changed. Refresh before reviewing removal.')
        item = matches[0]
        if item.get('source') == 'macOS application bundle':
            return {'review_only': True, 'application': item['Name'], 'path': item['InstallLocation'],
                    'note': 'Review this exact application in Finder or its vendor uninstaller. Optimus did not delete the bundle or its user data.'}
        manager, name = item.get('source'), item.get('native_name')
        if not name:
            raise UnsupportedOperation('The selected application has no verified native package identity.')
        commands = {'apt': ['/usr/bin/apt-get', '--simulate', 'remove', '--', name],
                    'dnf': ['/usr/bin/dnf', '--assumeno', 'remove', name],
                    'pacman': ['/usr/bin/pacman', '-R', '--print', '--', name],
                    'brew': [self.native_packages.executable, 'uninstall', '--formula' if item.get('kind') == 'formula' else '--cask', '--', name]}
        return {'review_only': True, 'application': item['Name'], 'id': item['id'],
                'native_command_for_review': commands[manager],
                'note': 'Review reverse dependencies and the removal plan in the native package manager before accepting removal. The displayed command was not executed; package removal and user-data deletion are never automatic.'}

    def codex_profile(self, priority='High'):
        raise UnsupportedOperation(self.capabilities()['unavailable']['codex-priority-profile'])

    def updates_scan(self):
        return self.native_packages.scan()

    def update_app(self, package_id):
        return self.native_packages.apply(package_id, confirmed=True)

    def update_app_unattended(self, package_id, *, cancel_event=None, progress_callback=None):
        options = {'progress_callback': progress_callback} if progress_callback is not None else {}
        return self.native_packages.apply(package_id, confirmed=True, cancel_event=cancel_event, **options)

    def app_presence_inspect(self, package_id, source='winget'):
        return {'id': package_id, 'source': source, 'supported': False, 'read_only': True,
                'note': 'Automatic install/uninstall presence is not supported by this native manager. Native package updates remain separate.'}

    def app_presence_capabilities(self):
        return {'supported': False, 'installer_types': [],
                'note': 'Native package updates are supported separately; saved application presence rules are reported without installing or removing native packages.'}

    def update_disk_readiness(self):
        try:
            free = shutil.disk_usage(Path.home()).free
            minimum = 2 * 1024 ** 3
            return {'ready': free >= minimum, 'free_bytes': free, 'minimum_free_bytes': minimum,
                    'reason': 'Installation-space floor checked on the user filesystem; the native installer may require more on other filesystems.'}
        except OSError:
            return {'ready': False, 'reason': 'The user filesystem free-space check failed.'}

    def drivers_list(self):
        if self.system == 'linux':
            modules = Path('/proc/modules')
            if modules.is_file():
                return [{'Name': parts[0], 'Size': int(parts[1]), 'State': parts[4], 'Source': 'Linux kernel module inventory'}
                        for line in modules.read_text(encoding='utf-8', errors='replace').splitlines()
                        if len(parts := line.split()) >= 5 and parts[1].isdigit()]
        return [{'Name': platform.release(), 'Source': platform.system() + ' kernel and vendor hardware support',
                 'note': 'Independent device driver inventory is not provided. Use System Information and the OS/vendor updater.'}]

    def driver_devices(self):
        return {'platform': self.system, 'kernel': platform.release(), 'devices': self.drivers_list(),
                'complete': False, 'read_only': True, 'source': 'Native OS/kernel inventory',
                'note': 'This is not a complete per-device driver catalog. Kernel, firmware and hardware support are provided by the native OS/vendor updater.'}

    def driver_coverage(self, scan=None):
        return self.driver_devices()

    def driver_updates_scan(self):
        scan = self.os_updates_scan() if self.system == 'darwin' else self.updates_scan()
        relevant = (list(scan.get('updates', [])) if self.system == 'darwin' else
                    [item for item in scan.get('updates', []) if re.search(r'(^|[/:-])(linux|kernel|firmware|mesa|nvidia|intel-media|intel-microcode|amd|libdrm|nouveau)', item['id'], re.I)])
        return {'updates': relevant, 'source': scan.get('source'), 'coverage': self.driver_devices(),
                'resultCode': 2 if scan.get('complete') else 4,
                'note': 'Repository package-name matches are hardware-support update candidates, not proof of a match to every device. macOS driver updates are bundled with OS/vendor updates.'}

    def os_updates_scan(self):
        if self.system != 'darwin':
            scan = self.updates_scan()
            return dict(scan, note='Linux OS, kernel and firmware updates are offered by the distribution package manager. ' + scan.get('note', ''))
        result = self.native_runner.run(['/usr/sbin/softwareupdate', '--list'], timeout=240)
        if result['exitCode'] != 0:
            return {'source': 'Apple Software Update', 'structured': False, 'complete': False,
                    'exitCode': result['exitCode'], 'updates': [], 'note': result.get('stderr', '')[:1000]}
        updates = []
        label = None
        for line in result['stdout'].splitlines():
            match = re.match(r'^\s*\*\s+Label:\s*(.+?)\s*$', line)
            if match:
                label = match[1]
                if len(label) > 512 or label.startswith('-') or any(ord(c) < 32 for c in label):
                    raise ValueError('Apple Software Update returned an invalid update label.')
                continue
            if label and 'Title:' in line:
                identity = 'apple-os:' + __import__('hashlib').sha256(label.encode()).hexdigest()
                version = re.search(r'\bVersion:\s*([^,]+)', line)
                title = re.search(r'\bTitle:\s*(.*?),\s*Version:', line)
                updates.append({'id': identity, 'name': title[1].strip() if title else label,
                                'label': label, 'available': version[1].strip() if version else 'Offered by Apple',
                                'source': 'Apple Software Update', 'offered_by_manager': True,
                                'restart_required': 'restart' in line.casefold(),
                                'review_only': True, 'detail': line.strip()[:2000]})
                label = None
        text = result['stdout'].casefold()
        complete = bool(updates) or 'no new software available' in text or 'no updates are available' in text
        return {'source': 'Apple Software Update', 'structured': complete, 'complete': complete,
                'exitCode': 0, 'updates': updates, 'note': 'Apple controls OS, firmware and bundled driver compatibility. Installation requires explicit review and native authorization; no automatic restart is requested.'}

    def install_os_update(self, identity, *, confirmed=False, agreements_accepted=False, cancel_event=None):
        if self.system != 'darwin':
            raise UnsupportedOperation('Use the distribution package update controls for Linux OS updates.')
        if confirmed is not True or agreements_accepted is not True:
            raise ValueError('Explicit Apple OS update approval and license acceptance are required.')
        scan = self.os_updates_scan()
        matches = [row for row in scan.get('updates', []) if row['id'] == identity]
        if scan.get('complete') is not True or len(matches) != 1:
            raise ValueError('This exact Apple update is no longer offered. Refresh before installing.')
        if cancel_event is not None and cancel_event.is_set():
            return {'exitCode': 0, 'cancelled': True, 'skipped': True}
        readiness = self.update_disk_readiness()
        if not readiness['ready']:
            return {'exitCode': 0, 'skipped': True, 'requiresReview': True, 'disk_readiness': readiness}
        item = matches[0]
        result = self.native_runner.privileged(['/usr/sbin/softwareupdate', '--install', item['label'], '--agree-to-license'],
                                               timeout=1800, cancel_event=cancel_event)
        result.update(id=identity, restartNeeded=item.get('restart_required', False),
                      note='No automatic restart was requested. Apple silicon/volume-owner authorization can require completion in System Settings.')
        if result.get('exitCode') == 0 and not result.get('cancelled'):
            after = self.os_updates_scan()
            result['verified'] = after.get('complete') is True and not any(row['id'] == identity for row in after['updates'])
            result['success'] = result['verified']
            result['requiresReview'] = not result['verified']
        return result

    def driver_update_apply(self, record):
        raise UnsupportedOperation('Review and install hardware-support packages through the native package updater. Optimus does not install an independent driver feed.')

    def driver_update_apply_unattended(self, record, accept_eula=False):
        return self.driver_update_apply(record)

    def backup_drivers(self, destination):
        raise UnsupportedOperation('Windows driver export does not apply. Use the native OS backup or snapshot system before kernel/firmware updates.')

    def changes(self):
        return self.native_startup.history() + self.performance_controller.allocation_history()

    def restore(self, change):
        if change.get('source') == 'native-startup':
            return self.native_startup.restore(change.get('Id'))
        if change.get('source') == 'python-allocation':
            return self.performance_controller.restore_allocation(change.get('Id') or change.get('id'))
        raise ValueError('This is not a recoverable native change record.')

    def security_status(self):
        checks = []
        if self.system == 'darwin':
            for name, command in [('Gatekeeper', ['/usr/sbin/spctl', '--status']),
                                  ('System Integrity Protection', ['/usr/bin/csrutil', 'status'])]:
                try:
                    result = self.native_runner.run(command, timeout=20)
                    checks.append({'name': name, 'status': 'reported' if result['exitCode'] == 0 else 'unavailable',
                                   'detail': (result.get('stdout', '') + result.get('stderr', ''))[:2000]})
                except (OSError, RuntimeError) as error:
                    checks.append({'name': name, 'status': 'unavailable', 'detail': str(error)})
        else:
            for name, path in [('Address-space randomization', Path('/proc/sys/kernel/randomize_va_space')),
                               ('Active Linux security modules', Path('/sys/kernel/security/lsm'))]:
                try:
                    value = path.read_text(encoding='utf-8').strip()
                    checks.append({'name': name, 'status': 'reported', 'detail': value})
                except OSError:
                    checks.append({'name': name, 'status': 'unavailable', 'detail': 'This kernel interface is unavailable or not readable by the current user.'})
        return {'platform': self.system, 'kernel': platform.release(), 'read_only': True,
                'checks': checks, 'recommendations': [{'id': 'native-system-updates', 'title': 'Review available native OS and repository security updates', 'automatic': False}],
                'note': 'These limited checks are observations, not a security certification. Firewall, disk encryption, application permissions and security software must also be reviewed using native OS tools.'}

    def windows_security_updates(self, settings, cancel_event=None, item_callback=None):
        raise UnsupportedOperation('Windows Update is unavailable. Use the native software/OS update review for this platform.')

    def legacy_patch_review(self, context=None):
        return {'review_only': True, 'platform': self.system, 'kernel': platform.release(),
                'recommendations': ['Use the supported OS, distribution and hardware-vendor security channels. Community patch compatibility must be evaluated for an exact product/version.'],
                'note': 'MITRE ATT&CK is a knowledge base, not an installable patch catalog. Optimus does not invent updates after vendor support ends or install unverified community patches.'}

    def apply_security_recommendation(self, recommendation_id, cancel_event=None):
        if recommendation_id == 'native-system-updates':
            return self.updates_scan()
        raise UnsupportedOperation('This security recommendation requires review in native system settings.')

    def scan_spyware(self, cancel_event=None):
        raise UnsupportedOperation('A native antimalware scan is not bundled. Use installed security software; Optimus does not label ordinary background applications as spyware.')

    def apply_system_privacy(self, settings, cancel_event=None):
        raise UnsupportedOperation('Windows advertising and diagnostic registry controls do not apply. Review native application privacy and telemetry settings.')

    def restore_system_privacy(self, change_id, cancel_event=None):
        raise UnsupportedOperation('No native global privacy setting changes are made by Optimus.')

    def system_privacy_history(self):
        return []

    def debloat_actions(self):
        return []

    def debloat_preview(self, action):
        raise UnsupportedOperation('Windows debloat scripts do not apply to macOS or Linux.')

    def debloat_apply(self, plan):
        return self.debloat_preview(plan)

    def registry_scan(self, *args, **kwargs):
        raise UnsupportedOperation('This operating system has no Windows Registry.')

    registry_remove = registry_scan
    registry_restore = registry_scan

    def power_plans(self):
        raise UnsupportedOperation('Windows power-plan controls are unavailable on this platform.')

    def set_power_plan(self, guid):
        return self.power_plans()

    def schedule_list(self):
        return []

    def create_schedule(self, name='Daily audit', time='18:00', mode='audit'):
        raise UnsupportedOperation('Use the app-open daily/weekly cadence or native operating-system scheduler. Optimus does not install background native audit jobs.')

    def remove_schedule(self, name):
        return self.create_schedule()

    def cloud_status(self):
        return {'connected': False, 'available': False, 'note': self.capabilities()['unavailable']['cloud-signin']}

    def _cloud_unavailable(self, *args, **kwargs):
        raise UnsupportedOperation(self.capabilities()['unavailable']['cloud-signin'])

    cloud_configure = cloud_connect = cloud_poll = cloud_logout = _cloud_unavailable
    cloud_scan = cloud_approval_scope = cloud_recycle = cloud_recycle_approved = _cloud_unavailable

    def browser_privacy(self, browser_id='chrome'):
        import webbrowser
        pages = {'chrome': 'chrome://settings/clearBrowserData', 'edge': 'edge://settings/clearBrowserData',
                 'brave': 'brave://settings/clearBrowserData', 'vivaldi': 'vivaldi://settings/clearBrowserData',
                 'firefox': 'about:preferences#privacy'}
        if browser_id not in pages:
            raise ValueError('Unknown browser identifier.')
        raise UnsupportedOperation('Use this browser\'s Clear browsing data settings or Optimus profile cleanup. A default-browser URL cannot safely select the requested browser.')

    def system_tool(self, tool_id):
        raise UnsupportedOperation('Open the corresponding native System Settings or package manager. Windows settings aliases are unavailable on this platform.')

    def verify_optimization(self, report, cancel_event=None, timeout=30):
        from .native_workflow import verify_native_outcomes
        return verify_native_outcomes(self, report, cancel_event=cancel_event, timeout=timeout)

    def close(self):
        self._close_optional_tools()
        self.sampler.stop()
