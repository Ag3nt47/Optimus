"""Persistent user preferences and actual-stage one-click optimization workflow."""
from __future__ import annotations
import copy
import json
import threading
import time
import uuid
from pathlib import Path
from .filesystem import replace_with_retry

DEFAULTS = {
    'intensity': 'Standard', 'cleanup_enabled': True, 'cleanup_age_days': 7,
    'browser_cache_enabled': True, 'consumer_debloat_enabled': False,
    'm365_debloat_enabled': False, 'outlook_debloat_enabled': False,
    'edge_debloat_enabled': False, 'codex_priority_enabled': False,
    'codex_priority': 'High', 'background_analysis_enabled': False,
    'analysis_interval': 5, 'reduce_motion': False,
}

def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    temporary.write_text(json.dumps(value, indent=2), encoding='utf-8')
    replace_with_retry(temporary, path)

class Preferences:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / 'preferences.json'
        self.lock = threading.RLock()

    def load(self):
        with self.lock:
            try:
                return self.validate(json.loads(self.path.read_text(encoding='utf-8')))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                return copy.deepcopy(DEFAULTS)

    @staticmethod
    def validate(value):
        if not isinstance(value, dict):
            raise ValueError('Settings must be an object.')
        result = copy.deepcopy(DEFAULTS)
        for key in DEFAULTS:
            if key not in value:
                continue
            if isinstance(DEFAULTS[key], bool):
                if not isinstance(value[key], bool):
                    raise ValueError(f'{key} must be on or off.')
            result[key] = value[key]
        if result['intensity'] not in ('Low', 'Standard', 'High'):
            raise ValueError('Unknown optimization intensity.')
        if result['codex_priority'] not in ('Normal', 'AboveNormal', 'High'):
            raise ValueError('Unsupported Codex priority.')
        if type(result['cleanup_age_days']) is not int or not 1 <= result['cleanup_age_days'] <= 3650:
            raise ValueError('Cleanup age must be 1 to 3650 days.')
        if type(result['analysis_interval']) is not int or not 2 <= result['analysis_interval'] <= 60:
            raise ValueError('Analysis interval must be 2 to 60 seconds.')
        return result

    def save(self, value):
        result = self.validate(value)
        with self.lock:
            atomic_json(self.path, result)
        return result

class Workflow:
    def __init__(self, backend):
        self.backend = backend
        self.lock = threading.Lock()
        self.active = False
        self.last_report = None

    def run(self, settings, progress_callback, cancel_event=None):
        configuration = Preferences.validate(settings)
        if not self.lock.acquire(blocking=False):
            raise RuntimeError('An optimization run is already active.')
        self.active = True
        report = None
        log_path = None
        try:
            run_id = uuid.uuid4().hex
            log_path = self.backend.data_dir / 'runs' / (run_id + '.json')
            report = {'id': run_id, 'started_at': time.time(), 'settings': configuration, 'stages': [], 'cancelled': False,
                      'status': 'running', 'note': 'Percent completion measures completed stages, not elapsed time. Resource snapshots fluctuate.'}
            self.last_report = report
            atomic_json(log_path, report)
            stages = [('Inspect current resource usage', 'Reading running programs, memory, storage, startup entries and services.', self.backend.audit)]
            age = configuration['cleanup_age_days']
            if configuration['intensity'] == 'Low':
                age = max(30, age)
            def clean(category_ids):
                plan = self.backend.cleanup_scan(category_ids, age)
                if cancel_event is not None and cancel_event.is_set():
                    return {'cancelled': True, 'reason': 'Cancelled after preview; no files were deleted.'}
                outcome = self.backend.cleanup_apply(plan) if plan.get('files') else {'deleted_count': 0, 'deleted_bytes': 0, 'skipped_count': 0}
                return {'preview_summary': {'files': len(plan.get('files', [])), 'bytes': plan.get('total_bytes', 0), 'complete': plan.get('complete'), 'skipped': plan.get('skipped')}, 'result': outcome}
            if configuration['cleanup_enabled']:
                stages.append(('Clean old temporary files', f'Permanently removing approved unlocked temporary files at least {age} days old.', lambda: clean(['user-temp'])))
            if configuration['browser_cache_enabled']:
                categories = [item['id'] for item in self.backend.cleanup_categories() if item.get('process_names') and item.get('available')]
                stages.append(('Clean closed-browser caches', f'Cache files at least {age} days old; passwords, cookies, history and bookmarks are preserved.', lambda: clean(categories)))
            for key, action, label in (
                ('consumer_debloat_enabled', 'consumer-apps', 'Remove selected consumer app group'),
                ('m365_debloat_enabled', 'm365', 'Remove Microsoft 365 consumer packages'),
                ('outlook_debloat_enabled', 'outlook', 'Remove new Outlook package'),
                ('edge_debloat_enabled', 'edge', 'Uninstall Microsoft Edge using the supported installer'),
            ):
                if not configuration[key]:
                    continue
                def remove_group(action=action):
                    plan = self.backend.debloat_preview(action)
                    if cancel_event is not None and cancel_event.is_set():
                        return {'cancelled': True, 'reason': 'Cancelled after preview; no packages were removed.'}
                    ids = [item['id'] for item in plan.get('targets', []) if item.get('id')]
                    if not ids:
                        return {'skipped': True, 'reason': 'No eligible installed targets found.'}
                    return self.backend.debloat_apply({'planId': plan['planId'], 'selectedIds': ids, 'confirmed': True})
                stages.append((label, 'Applying only the curated package group explicitly enabled in Settings. Store, security, frameworks and WebView2 are preserved.', remove_group))
            if configuration['codex_priority_enabled']:
                stages.append(('Apply Codex performance profile', 'Applying your selected process priority. Existing external priority watchers remain unchanged.', lambda: self.backend.codex_profile(configuration['codex_priority'])))
            if configuration['background_analysis_enabled']:
                stages.append(('Enable resource analysis', 'Starting the optional read-only CPU, memory, disk and network sampler.', lambda: self.backend.analysis_start(configuration['analysis_interval'])))
            stages.append(('Capture final resource report', 'Saving final measurements and the outcome of each operation.', self.backend.audit))
            completed = 0
            for title, description, operation in stages:
                if cancel_event is not None and cancel_event.is_set():
                    report['cancelled'] = True
                    break
                process_lines = ['Optimus workflow worker']
                process = getattr(self.backend.bridge, '_process', None)
                if process is not None and process.poll() is None:
                    process_lines.append(f'Windows PowerShell engine (PID {process.pid})')
                progress_callback({'percentage': int(completed * 100 / len(stages)), 'stage': title,
                                   'description': description, 'processes': process_lines, 'log': f'Starting: {title}', 'completed': completed, 'total': len(stages)})
                started = time.time()
                try:
                    result = operation()
                    stage = {'title': title, 'status': 'completed', 'started_at': started, 'finished_at': time.time(), 'result': result}
                except Exception as error:
                    stage = {'title': title, 'status': 'failed', 'started_at': started, 'finished_at': time.time(), 'error': str(error)}
                report['stages'].append(stage)
                completed += 1
                atomic_json(log_path, report)
                status = stage['status']
                progress_callback({'percentage': int(completed * 100 / len(stages)), 'stage': title, 'description': stage.get('error', f'{title}: {status}'),
                                   'processes': process_lines, 'log': f'{status.upper()}: {title}' + (': ' + stage['error'] if stage.get('error') else ''), 'completed': completed, 'total': len(stages)})
            report['finished_at'] = time.time()
            report['failed_stages'] = sum(item['status'] == 'failed' for item in report['stages'])
            report['log_path'] = str(log_path)
            report['status'] = 'cancelled' if report['cancelled'] else 'completed_with_errors' if report['failed_stages'] else 'completed'
            atomic_json(log_path, report)
            return report
        except Exception as error:
            if report is None:
                report = {'settings': configuration, 'stages': [], 'cancelled': False}
            report.update({'status': 'failed', 'error': str(error), 'finished_at': time.time()})
            if log_path is not None:
                report['log_path'] = str(log_path)
                try:
                    atomic_json(log_path, report)
                except Exception as journal_error:
                    report['journal_error'] = str(journal_error)
            self.last_report = report
            raise
        finally:
            self.active = False
            self.lock.release()



