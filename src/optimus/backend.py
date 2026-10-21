"""Application services consumed by the Qt interface."""
from __future__ import annotations
import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from .bridge import PowerShellBridge
from .cleaner import Cleaner
from .sampler import ResourceSampler
from .workflow import Preferences, Workflow
from . import performance

class Backend:
    def __init__(self, data_dir: Path | None = None):
        self.data_dir = Path(data_dir or Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'Optimus')
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # Resolve MSIX/Windows app-data redirection before constructing writers.
        self.data_dir = self.data_dir.resolve(strict=True)
        self.bridge = PowerShellBridge(self.data_dir / 'engine')
        self.cleaner = Cleaner(self.data_dir)
        self.sampler = ResourceSampler()
        self.preference_store = Preferences(self.data_dir)
        self.performance_controller = performance.PerformanceController(self.data_dir / 'suspended-processes.json')
        self.workflow = Workflow(self)
        from .browser_privacy import BrowserPrivacy
        self.browser_privacy_engine = BrowserPrivacy(self.data_dir)
        from .browser_cleanup import BrowserSessionController
        self.browser_sessions = BrowserSessionController()
        from .cloud_drive import CloudDrive
        self.cloud = CloudDrive(self.data_dir)
        from .maintenance import Maintenance
        self.maintenance = Maintenance(self.bridge, self.data_dir)
        self.last_audit = None
        from .process_harness import ProcessHarness
        self.process_harness = ProcessHarness(self)

    @staticmethod
    def _list(value):
        return [] if value is None else value if isinstance(value, list) else [value]

    def elevate(self):
        executable = sys.executable
        arguments = [] if getattr(sys, 'frozen', False) else ['-B', str(Path(__file__).parents[2] / 'launch.py')]
        result = ctypes.windll.shell32.ShellExecuteW(None, 'runas', executable, subprocess.list2cmdline(arguments), None, 1)
        if result <= 32:
            raise RuntimeError('Administrator relaunch was not completed.')
        return {'started': True, 'note': 'The administrator window is a separate process. Your current window remains open.'}

    def source_root(self):
        if os.environ.get('OPTIMUS_ORIGINAL_SOURCE'):
            return Path(os.environ['OPTIMUS_ORIGINAL_SOURCE'])
        return Path(sys.executable).parent / 'source' if getattr(sys, 'frozen', False) else Path(__file__).parents[2]

    def preferences(self):
        return self.preference_store.load()

    def save_preferences(self, settings):
        return self.preference_store.save(settings)

    def optimize(self, settings=None, progress_callback=None, cancel_event=None, *, browser_prompt=None):
        return self.workflow.run(settings or self.preferences(), progress_callback or (lambda event: None), cancel_event,
                                 browser_prompt=browser_prompt)

    def browser_cleanup_status(self, browser_id):
        return self.browser_sessions.browser_status(browser_id)

    def browser_cleanup_close(self, browser_id, cancel_event=None):
        return self.browser_sessions.close_browser(browser_id, cancel_event=cancel_event)

    def audit(self):
        from . import __version__
        snapshot = self.bridge.request('audit')
        if not isinstance(snapshot, dict):
            raise RuntimeError('The Windows audit returned no usable report.')
        self.last_audit = dict(snapshot, EngineVersion=snapshot.get('ToolVersion'),
                               ToolVersion=__version__, ApplicationVersion=__version__)
        return self.last_audit

    def processes(self):
        return performance.list_performance_candidates()

    def process_harness_scan(self, cancel_event=None):
        return self.process_harness.scan(cancel_event=cancel_event)

    def process_harness_apply(self, request, cancel_event=None):
        return self.process_harness.apply(request, cancel_event=cancel_event)

    def process_harness_export(self, plan_id, destination):
        return self.process_harness.export(plan_id, destination)

    def slowdown_report(self, cancel_event=None):
        from .slowdown import analyze_slowdowns
        return analyze_slowdowns(cancel_event=cancel_event)

    def set_priority(self, record, priority='High'):
        return self.performance_controller.set_process_priority(record, priority)

    def sleep_process(self, record):
        return self.performance_controller.sleep_process(record)

    def wake_process(self, record):
        return self.performance_controller.wake_process(record)

    def wake_all(self):
        return self.performance_controller.wake_all()

    def suspended(self):
        return self.performance_controller.list_suspended()

    def codex_profile(self, priority='High'):
        return self.performance_controller.performance_profile(priority=priority)

    def analysis_start(self, interval=5):
        return self.sampler.start(interval)

    def analysis_stop(self):
        return self.sampler.stop()

    def analysis_status(self):
        return self.sampler.status()

    def set_affinity(self, record, cores):
        return self.performance_controller.set_affinity(record, cores)

    def process_details(self, record):
        local = performance.get_process_details(record)
        local['launch_evidence'] = self.bridge.request('process_details', record=record)
        return local

    def cleanup_categories(self):
        return self.cleaner.categories()

    def cleanup_scan(self, category_ids=None, min_age_days=7):
        if category_ids is None:
            category_ids = ['user-temp']
        return self.cleaner.scan(list(category_ids), int(min_age_days))

    def cleanup_apply(self, plan):
        return self.cleaner.apply(plan)

    def storage_scan(self, root):
        return self.bridge.request('storage_scan', root=str(root), timeout=120)

    def duplicate_scan(self, root):
        return self.bridge.request('duplicate_scan', root=str(root), timeout=120)

    def duplicate_recycle(self, record):
        return self.bridge.request('duplicate_recycle', record=record)

    def duplicate_recycle_approved(self, record):
        approved_record = dict(record, AutomaticApproved=True)
        return self.bridge.request('duplicate_recycle', record=approved_record)

    def startup_list(self):
        return self._list(self.bridge.request('startup_list'))

    def startup_set(self, record, enabled):
        return self.bridge.request('startup_set', record=record, enabled=bool(enabled))

    def startup_remove(self, record):
        return self.bridge.request('startup_remove', record=record)

    def services(self):
        return self._list(self.bridge.request('services'))

    def service_set(self, record, action):
        return self.bridge.request('service_set', record=record, action=action)

    def installed_apps(self):
        return self._list(self.bridge.request('installed_apps'))

    def uninstall(self, record):
        current = self.installed_apps()
        if not any(item.get('Identity') == record.get('Identity') and item.get('Name') == record.get('Name') for item in current):
            raise ValueError('The installed-program selection changed. Refresh first.')
        os.startfile('ms-settings:appsfeatures')
        return {'opened': 'Windows Installed apps', 'search': record.get('Name'), 'note': 'Use the registered Windows uninstall action for this application.'}

    def updates_scan(self):
        return self.maintenance.updates_scan()

    def update_app(self, package_id):
        return self.maintenance.updates_apply(package_id, confirmed=True)

    def update_app_unattended(self, package_id, *, cancel_event=None):
        """Saved scope/agreements authorize updates; cancellation still gates installation."""
        options = {'cancel_event': cancel_event} if cancel_event is not None else {}
        return self.maintenance.updates_apply(package_id, confirmed=True, unattended=True, **options)

    def drivers_list(self):
        return self._list(self.bridge.request('drivers_list'))

    def _driver_updates_scan_raw(self):
        try:
            scan = self.bridge.request('driver_updates_scan', timeout=300)
        except Exception as error:
            return {'updates': [], 'resultCode': 4, 'source': 'Windows Update', 'error': str(error)}
        if not isinstance(scan, dict):
            return {'updates': [], 'resultCode': 4, 'source': 'Windows Update',
                    'error': 'Windows Update returned no usable scan report.'}
        return scan

    def driver_updates_scan(self):
        scan = dict(self._driver_updates_scan_raw())
        scan['coverage'] = self.driver_coverage(scan)
        return scan

    def driver_devices(self):
        """Setup inventory only; no online Windows Update scan or changes."""
        return self.driver_coverage({'updates': [], 'resultCode': None,
                                     'source': 'Not scanned (device inventory only)'})


    def driver_coverage(self, scan=None):
        """Present devices, device problems and exact WUA matches; no changes."""
        from .driver_coverage import build_driver_coverage
        if scan is None:
            scan = self._driver_updates_scan_raw()
        try:
            inventory = self.bridge.request('driver_device_inventory', timeout=120)
        except Exception as error:
            inventory = {'complete': False, 'devices': [],
                         'errors': [{'source': 'Device inventory', 'error': str(error)}]}
        return build_driver_coverage(inventory, scan)


    def is_admin(self):
        return bool(ctypes.windll.shell32.IsUserAnAdmin())

    def driver_backup_automatic(self):
        import uuid
        return self.backup_drivers(self.data_dir / 'driver-backups' / uuid.uuid4().hex)

    def driver_update_apply_unattended(self, record, accept_eula=False):
        if accept_eula is not True:
            raise ValueError('Saved driver agreement approval is required.')
        return self.bridge.request('driver_update_apply', id=record['id'], revision=record['revision'],
                                   unattended=True, acceptEula=True, record=record, timeout=900)

    def driver_update_apply(self, record):
        return self.bridge.request('driver_update_apply', id=record['id'], revision=record['revision'], timeout=900)

    def backup_drivers(self, destination):
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        result = subprocess.run([str(Path(os.environ['SystemRoot']) / 'System32/pnputil.exe'), '/export-driver', '*', str(destination)],
                                capture_output=True, text=True, errors='replace', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=300)
        return {'exit_code': result.returncode, 'output': result.stdout + result.stderr, 'destination': str(destination)}

    def debloat_actions(self):
        return self.maintenance.debloat_actions()

    def debloat_preview(self, action):
        return self.maintenance.debloat_preview(action)

    def debloat_apply(self, plan):
        return self.maintenance.debloat_apply(plan)

    def changes(self):
        return self._list(self.bridge.request('changes')) + self.performance_controller.allocation_history()

    def restore(self, change):
        if change.get('source') == 'python-allocation':
            return self.performance_controller.restore_allocation(change.get('Id') or change.get('id'))
        return self.bridge.request('restore', change=change)

    def registry_scan(self):
        return self.bridge.request('registry_scan')

    def registry_remove(self, record):
        return self.bridge.request('registry_remove', record=record)

    def registry_restore(self, change):
        return self.bridge.request('registry_restore', change=change)

    def power_plans(self):
        return self._list(self.bridge.request('power_plans'))

    def set_power_plan(self, guid):
        return self.bridge.request('set_power_plan', guid=guid)

    def schedule_list(self):
        return self.maintenance.schedules_list()

    def create_schedule(self, name='Daily audit', time='18:00', mode='audit'):
        if mode != 'audit':
            raise ValueError('Scheduled jobs currently run read-only audits.')
        hour, minute = map(int, time.split(':'))
        return self.maintenance.schedule_create(hour=hour, minute=minute, confirmed=True)

    def remove_schedule(self, name):
        return self.maintenance.schedule_delete(name, confirmed=True)

    def cloud_status(self):
        return self.cloud.status()

    def cloud_configure(self, client_id, tenant='consumers'):
        return self.cloud.configure(client_id, tenant)

    def cloud_connect(self):
        return self.cloud.begin_connect()

    def cloud_poll(self, session_id):
        return self.cloud.poll_connect(session_id)

    def cloud_logout(self):
        return self.cloud.logout()

    def cloud_scan(self, folder_id='root'):
        return self.cloud.scan(folder_id)

    def cloud_approval_scope(self, folder_id='root'):
        return self.cloud.approval_scope(folder_id)

    def cloud_recycle_approved(self, plan, policy, cancel_event=None, item_callback=None):
        return self.cloud.recycle_approved(plan, policy, cancel_event=cancel_event, item_callback=item_callback)

    def cloud_recycle(self, plan):
        return self.cloud.recycle(plan)

    def privacy_profiles(self):
        return self.browser_privacy_engine.profiles()

    def privacy_scan(self, profile_id, categories=None, keep_domains=None, *, browser_prompt=None, cancel_event=None):
        if browser_prompt is not None:
            profile = self.browser_privacy_engine._profile(profile_id)
            identifiers = {'Microsoft Edge': 'edge', 'Google Chrome': 'chrome', 'Brave': 'brave',
                           'Vivaldi': 'vivaldi', 'Mozilla Firefox': 'firefox'}
            browser_id = identifiers.get(profile['browser'])
            if not browser_id:
                raise ValueError('This browser is not supported by the close-and-clean prompt.')
            state = self.browser_cleanup_status(browser_id)
            def skip(note):
                return {'skipped': True, 'canApply': False,
                        'profile': {'browser': profile['browser'], 'name': profile['name']},
                        'counts': {}, 'note': note}
            if cancel_event is not None and cancel_event.is_set():
                return skip('Cancelled; this browser was not cleaned.')
            if state.get('complete') is not True or type(state.get('running')) is not bool:
                return skip('Browser process status could not be verified; this browser was skipped.')
            if state['running']:
                question = {'browser_id': browser_id, 'browser': profile['browser'],
                            'processes': [{'pid': item.get('pid'), 'name': item.get('name')} for item in state.get('processes', [])],
                            'categories': list(categories or []), 'profile_count': 1}
                if browser_prompt(question) is not True:
                    return skip('You chose to skip this open browser. It was left running and no data was cleaned.')
                if cancel_event is not None and cancel_event.is_set():
                    return skip('Cancelled before closing the browser; no data was cleaned.')
                result = self.browser_cleanup_close(browser_id, cancel_event=cancel_event)
                if result.get('closed') is not True or result.get('complete') is not True:
                    return skip('The browser did not close completely; it was skipped without forcing it to exit.')
        return self.browser_privacy_engine.scan(profile_id, categories=categories or [], keep_domains=keep_domains or [])

    def privacy_apply(self, plan):
        return self.browser_privacy_engine.apply(plan)

    def privacy_backups(self):
        return self.browser_privacy_engine.list_backups()

    def privacy_restore(self, backup_id, confirmed=False):
        return self.browser_privacy_engine.restore_backup(backup_id, confirmed=confirmed)

    def browser_privacy(self, browser_id='edge'):
        # Browser-native clearing preserves choices about passwords, logins and history.
        browsers = {
            'edge': ([Path(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')) / 'Microsoft/Edge/Application/msedge.exe', Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'Microsoft/Edge/Application/msedge.exe'], 'edge://settings/clearBrowserData'),
            'chrome': ([Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'Google/Chrome/Application/chrome.exe', Path(os.environ['LOCALAPPDATA']) / 'Google/Chrome/Application/chrome.exe'], 'chrome://settings/clearBrowserData'),
            'brave': ([Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'BraveSoftware/Brave-Browser/Application/brave.exe'], 'brave://settings/clearBrowserData'),
            'firefox': ([Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'Mozilla Firefox/firefox.exe'], 'about:preferences#privacy'),
        }
        if browser_id not in browsers:
            raise ValueError('Unknown browser.')
        candidates, url = browsers[browser_id]
        executable = next((path for path in candidates if path.is_file()), None)
        if not executable:
            raise RuntimeError('This browser was not found in its standard installation locations.')
        subprocess.Popen([str(executable), url])
        return {'opened': str(executable), 'page': url}

    def system_tool(self, tool_id):
        aliases = {'installed-apps': 'apps', 'windows-update': 'updates', 'device-manager': 'devices', 'task-scheduler': 'tasks', 'defrag': 'drives'}
        tool_id = aliases.get(tool_id, tool_id)
        settings = {'storage': 'ms-settings:storagesense', 'updates': 'ms-settings:windowsupdate', 'startup': 'ms-settings:startupapps',
                    'apps': 'ms-settings:appsfeatures', 'recovery': 'ms-settings:recovery', 'privacy': 'ms-settings:privacy-general', 'optional-updates': 'ms-settings:windowsupdate-optionalupdates', 'store-updates': 'ms-windows-store://downloadsandupdates'}
        programs = {'resource-monitor': 'System32/resmon.exe', 'drives': 'System32/dfrgui.exe', 'services': 'System32/services.msc',
                    'tasks': 'System32/taskschd.msc', 'devices': 'System32/devmgmt.msc', 'system-restore': 'System32/rstrui.exe'}
        if tool_id in settings:
            os.startfile(settings[tool_id])
        elif tool_id in programs:
            os.startfile(str(Path(os.environ['SystemRoot']) / programs[tool_id]))
        else:
            raise ValueError('Unknown Windows tool.')
        return {'opened': tool_id}

    def export_report(self, destination):
        snapshot = self.audit()
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        (destination / 'audit.json').write_text(json.dumps(snapshot, indent=2), encoding='utf-8')
        return {'path': str(destination / 'audit.json')}

    def close(self):
        self.sampler.stop()
        self.bridge.close()





