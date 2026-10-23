"""Opt-in retention for closed Codex archives through its supported delete CLI.

Codex metadata is read with SQLite mode=ro. Only Codex deletes its own threads;
Optimus never edits its databases, active sessions, worktrees or credentials.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import threading
import time
import uuid

import psutil

from .filesystem import _ordinary_data_path
from .speed_enhancements import _private_json

_SCOPES = ('codex_archives_cleanup_enabled', 'codex_archives_retention_days')
_UUID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z')
_MAX_ROWS = 10000


def _ordinary(path, *, file=False):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('Codex retention requires an absolute ordinary local path.')
    return _ordinary_data_path(path, file=file)


def _fingerprint(path, *, executable=False):
    info = _ordinary(path, file=True)
    if info is None:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or (not executable and getattr(info, 'st_file_attributes', 0) & 5):
        raise ValueError('Protected or multiply linked archive files are preserved.')
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def codex_closed():
    """An incomplete process inventory cannot authorize personal-history deletion."""
    observed = 0
    try:
        for process in psutil.process_iter(['name'], ad_value=None):
            try:
                name = process.info.get('name')
                if not isinstance(name, str) or not name:
                    return False
                observed += 1
                name = name.casefold()
                if name.startswith(('codex', 'chatgpt')) or name.startswith('openai') and 'codex' in name:
                    return False
            except psutil.NoSuchProcess:
                continue
            except psutil.AccessDenied:
                return False
    except (OSError, psutil.Error):
        return False
    return observed > 0


def _execute(arguments, home):
    """Never kill a delete command whose metadata mutation may still be active."""
    child = subprocess.Popen(arguments, shell=False, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        env=dict(os.environ, CODEX_HOME=str(home)))
    buffers, overflow = [bytearray(), bytearray()], threading.Event()
    def drain(stream, buffer):
        try:
            while block := stream.read(65536):
                remaining = 1024**2 - len(buffer)
                buffer.extend(block[:max(0, remaining)])
                if len(block) > remaining:
                    overflow.set()
        finally:
            stream.close()
    readers = [threading.Thread(target=drain, args=(stream, buffer),
               name='Optimus Codex deletion drain', daemon=False)
               for stream, buffer in zip((child.stdout, child.stderr), buffers)]
    for reader in readers:
        reader.start()
    # A timeout leaves the exact native process and its bounded non-daemon
    # drainers alive. The prepared receipt remains unresolved; no retry or kill.
    child.wait(timeout=45)
    for reader in readers:
        reader.join(timeout=2)
    if any(reader.is_alive() for reader in readers):
        raise subprocess.TimeoutExpired(arguments, 45)
    if overflow.is_set():
        raise ValueError('The Codex command exceeded its bounded response.')
    return subprocess.CompletedProcess(arguments, child.returncode,
        buffers[0].decode('utf-8', 'replace'), buffers[1].decode('utf-8', 'replace'))


def _metadata(home):
    """Read bounded archive metadata; unknown schemas never authorize deletion."""
    _ordinary(home)
    candidates = [path for path in home.glob('state_*.sqlite')
                  if re.fullmatch(r'state_[0-9]+\.sqlite', path.name)]
    if len(candidates) != 1:
        raise ValueError('Codex archive metadata is unavailable or ambiguous.')
    database = candidates[0]
    identity = _fingerprint(database)
    if identity is None:
        raise ValueError('The Codex metadata database is missing.')
    for suffix in ('-wal', '-shm', '-journal'):
        _fingerprint(Path(str(database) + suffix))
    connection = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=2)
    try:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        columns = {row[1] for row in connection.execute('PRAGMA table_info(threads)')}
        edge_columns = {row[1] for row in connection.execute('PRAGMA table_info(thread_spawn_edges)')}
        if not {'id', 'rollout_path', 'archived', 'archived_at', 'updated_at', 'is_pinned'} <= columns or not {
                'parent_thread_id', 'child_thread_id'} <= edge_columns:
            raise ValueError('This Codex metadata schema cannot verify archived age, pin state and descendants.')
        rows = connection.execute('SELECT id,rollout_path,archived,archived_at,updated_at,is_pinned '
                                  'FROM threads WHERE archived=1 LIMIT ?', (_MAX_ROWS + 1,)).fetchall()
        all_rows = connection.execute('SELECT id FROM threads LIMIT ?', (_MAX_ROWS + 1,)).fetchall()
        edges = connection.execute('SELECT parent_thread_id,child_thread_id FROM thread_spawn_edges LIMIT ?',
                                   (_MAX_ROWS * 5 + 1,)).fetchall()
        if len(rows) > _MAX_ROWS or len(all_rows) > _MAX_ROWS or len(edges) > _MAX_ROWS * 5:
            raise ValueError('The archive metadata inventory exceeded its bound.')
        all_ids = {row[0] for row in all_rows if isinstance(row[0], str) and _UUID.fullmatch(row[0])}
        if len(all_ids) != len(all_rows):
            raise ValueError('Codex thread identities are incomplete or malformed.')
        archives = {}
        for row in rows:
            identity, path, archived, archived_at, updated_at, pinned = row
            if (not isinstance(identity, str) or not _UUID.fullmatch(identity) or identity in archives
                    or not isinstance(path, str) or not 0 < len(path) <= 4096
                    or any(ord(char) < 32 for char in path)
                    or type(archived) is not int or archived != 1 or type(pinned) is not int or pinned not in (0, 1)
                    or any(type(value) is not int or not 0 < value <= 32503680000
                           for value in (archived_at, updated_at))):
                raise ValueError('Codex archive metadata is incomplete or malformed.')
            archives[identity] = dict(id=identity, path=path, archived_at=archived_at,
                                      updated_at=updated_at, pinned=bool(pinned))
        if any(not isinstance(value, str) or not _UUID.fullmatch(value) for edge in edges for value in edge):
            raise ValueError('Codex descendant metadata is incomplete.')
        return archives, {edge[0] for edge in edges}, all_ids
    finally:
        connection.close()


def _archive_files(home, row, cutoff):
    root = home / 'archived_sessions'
    _ordinary(root)
    path = Path(row['path'])
    if path.parent != root or not re.fullmatch(r'rollout-[0-9T_:.\-]+' + re.escape(row['id']) + r'\.jsonl(?:\.zst)?', path.name):
        raise ValueError('Only an exact rollout inside the archived-sessions folder can be removed.')
    plain = path.with_suffix('') if path.suffix == '.zst' else path
    candidates = [plain, Path(str(plain) + '.zst')]
    files = {}
    for candidate in candidates:
        identity = _fingerprint(candidate)
        if identity is not None:
            if identity[3] / 1e9 > cutoff:
                raise ValueError('A recently modified archive is preserved.')
            files[str(candidate)] = identity
    if len(files) > 1:
        raise ValueError('Multiple physical rollout versions require review.')
    return files


class CodexArchiveCleanup:
    def __init__(self, data_dir, *, home=None, runner=None, closed=None, clock=None, settings_reader=None):
        self.data_dir = Path(data_dir)
        self.home = Path(home or os.environ.get('CODEX_HOME') or Path.home() / '.codex')
        self.runner, self.closed, self.clock = runner or _execute, closed or codex_closed, clock or time.time
        self.settings_reader = settings_reader

    def run(self, settings, *, progress_callback=None, cancel_event=None):
        enabled = settings.get(_SCOPES[0], False)
        days = settings.get(_SCOPES[1], 7)
        if type(enabled) is not bool or type(days) is not int or not 1 <= days <= 3650:
            raise ValueError('Choose an explicit archive-cleanup toggle and 1–3650 retention days.')
        if progress_callback is not None and not callable(progress_callback):
            raise ValueError('The optional archive progress reporter must be callable.')
        result = {'deleted_count': 0, 'deleted_bytes': 0, 'deleted_threads': 0,
                  'skipped_count': 0, 'failed_items': 0, 'items': [], 'retention_days': days,
                  'note': 'Only closed, unpinned, old archived leaf threads use Codex deletion. Optimus does not run database compaction or worktree-cleanup commands.'}
        if not enabled:
            return dict(result, skipped=True, review_only=True, note='Codex archive retention is not selected.')
        if not self.closed():
            return dict(result, deferred=True, note='Codex is running or its process state is unknown. Archived chats are preserved until Codex is closed.')
        root = self.data_dir / 'retention-cleanup'
        _ordinary(root)
        previous = list(root.glob('codex-archives-*.json')) if root.exists() else []
        if len(previous) > 1000:
            return dict(result, failed_items=1, requires_review=True, note='Archive cleanup history requires review.')
        for path in previous:
            info = _ordinary(path, file=True)
            if info is None or info.st_size > 2 * 1024**2:
                return dict(result, failed_items=1, requires_review=True, note='Archive cleanup history could not be verified.')
            saved = json.loads(path.read_text(encoding='utf-8'))
            if (not isinstance(saved, dict) or type(saved.get('schema')) is not int or saved['schema'] != 1
                    or saved.get('kind') != 'codex-archive-retention'
                    or not isinstance(saved.get('id'), str) or not re.fullmatch('[0-9a-f]{32}', saved['id'])
                    or path.name != 'codex-archives-' + saved['id'] + '.json'
                    or saved.get('status') not in ('prepared', 'running', 'uncertain', 'cancelled',
                                                  'deferred', 'partial', 'completed_with_errors', 'completed')):
                return dict(result, failed_items=1, requires_review=True, note='Archive cleanup history could not be verified.')
            if saved.get('status') in ('prepared', 'running', 'uncertain'):
                return dict(result, failed_items=1, requires_review=True,
                            note='A previous Codex deletion has an unresolved outcome. No deletion was retried.')
        now = self.clock()
        if type(now) not in (int, float) or not math.isfinite(now) or now <= 0:
            raise ValueError('Archive retention needs a known current timestamp.')
        cutoff, started = now - days * 86400, time.monotonic()
        archives, parents, _ = _metadata(self.home)
        selected = [row for row in archives.values() if not row['pinned'] and row['id'] not in parents
                    and max(row['archived_at'], row['updated_at']) <= cutoff]
        result['skipped_count'] = len(archives) - len(selected)
        if not selected:
            return dict(result, review_only=True, note='No verified unpinned archived leaf chats exceed the retention age.')
        executable = shutil.which('codex')
        if not executable or not Path(executable).is_absolute() or os.name == 'nt' and Path(executable).suffix.casefold() != '.exe':
            return dict(result, failed_items=1, note='The installed native Codex CLI is unavailable; no archives were removed.')
        executable = str(Path(executable).resolve(strict=True))
        executable_identity = _fingerprint(Path(executable), executable=True)
        if executable_identity is None:
            return dict(result, failed_items=1, note='The Codex executable is missing; no archives were removed.')
        help_result = self.runner([executable, 'delete', '--help'], self.home)
        if type(help_result.returncode) is not int or help_result.returncode != 0 or '--force' not in help_result.stdout:
            return dict(result, failed_items=1, note='This Codex CLI does not report support for exact-UUID noninteractive deletion.')
        identity = uuid.uuid4().hex
        result['cleanup_id'] = 'codex-archives-' + identity
        path = root / (result['cleanup_id'] + '.json')
        root.mkdir(parents=True, exist_ok=True)
        journal = {'schema': 1, 'id': identity, 'kind': 'codex-archive-retention', 'status': 'running',
                   'retention_days': days, 'started_at': now, 'result': result}
        _private_json(path, journal)

        def emit(percent, description):
            if progress_callback is not None:
                try:
                    progress_callback({'item_progress': {'key': 'cleanup:' + result['cleanup_id'],
                        'label': 'Archived Codex chats', 'phase': 'cleanup', 'percent': percent},
                        'deleted_bytes': result['deleted_bytes'], 'deleted_count': result['deleted_count'],
                        'description': description, 'progress_only': True})
                except Exception:
                    pass
        emit(0, f'Checking {len(selected)} old archived Codex chats.')
        uncertain = False
        def current_scope():
            if self.settings_reader is None:
                return True
            latest = self.settings_reader()
            return isinstance(latest, dict) and all(
                latest.get(key, False if key.endswith('enabled') else 7) == settings.get(key, False if key.endswith('enabled') else 7)
                for key in _SCOPES)
        for index, approved in enumerate(selected):
            pending_deletion = False
            if (cancel_event is not None and cancel_event.is_set()) or time.monotonic() - started >= 120:
                result['cancelled'] = cancel_event is not None and cancel_event.is_set()
                result['partial'] = True
                break
            if not current_scope():
                result.update(cancelled=True, note='Archive cleanup choices changed; remaining chats were preserved.')
                break
            if not self.closed():
                result.update(deferred=True, note='Codex started; remaining archived chats were preserved.')
                break
            try:
                current, parents, _ = _metadata(self.home)
                if current.get(approved['id']) != approved or approved['id'] in parents:
                    raise ValueError('Archive age, pin, path or descendant state changed.')
                files = _archive_files(self.home, approved, cutoff)
                if _fingerprint(Path(executable), executable=True) != executable_identity:
                    raise ValueError('The Codex executable changed after review.')
                journal.update(current_id=approved['id'], status='prepared')
                pending_deletion = True
                _private_json(path, journal)
                if not self.closed() or not current_scope() or (cancel_event is not None and cancel_event.is_set()):
                    result.update(deferred=True, note='Codex or saved choices changed at the deletion boundary; remaining archived chats were preserved.')
                    result['items'].append({'id': approved['id'], 'status': 'preserved',
                                            'reason': 'The final pre-delete activity/approval guard changed; no delete command was started.'})
                    journal.update(status='deferred')
                    _private_json(path, journal)
                    pending_deletion = False
                    break
                completed = self.runner([executable, 'delete', approved['id'], '--force'], self.home)
                _, _, remaining_ids = _metadata(self.home)
                if type(completed.returncode) is not int or completed.returncode != 0 or approved['id'] in remaining_ids:
                    uncertain = True
                    raise RuntimeError('Codex deletion did not establish a verified terminal removal.')
                removed = {name: value for name, value in files.items() if _fingerprint(Path(name)) is None}
                if len(removed) != len(files):
                    raise RuntimeError('Codex removed thread metadata but did not confirm removal of every approved rollout file.')
                result['deleted_threads'] += 1
                result['deleted_count'] += len(removed)
                result['deleted_bytes'] += sum(value[2] for value in removed.values())
                result['items'].append({'id': approved['id'], 'status': 'deleted', 'rollout_files_removed': len(removed)})
                journal.update(status='running')
                _private_json(path, journal)
                pending_deletion = False
            except (OSError, ValueError, sqlite3.Error, RuntimeError, subprocess.TimeoutExpired) as error:
                uncertain = uncertain or pending_deletion
                result['failed_items'] += 1
                result['items'].append({'id': approved['id'], 'status': 'uncertain' if uncertain else 'preserved',
                                        'reason': str(error)[:500]})
                if uncertain:
                    break
            emit(min(99, (index + 1) * 100 / len(selected)),
                 f'{result["deleted_threads"]} archived chats deleted; {result["deleted_count"]} rollout files removed.')
        result['log_path'] = str(path)
        journal.update(status='uncertain' if uncertain else 'cancelled' if result.get('cancelled') else
                       'deferred' if result.get('deferred') else 'partial' if result.get('partial') else
                       'completed_with_errors' if result['failed_items'] else 'completed', finished_at=self.clock())
        try:
            _private_json(path, journal)
        except (OSError, ValueError) as error:
            result.update(failed_items=result['failed_items'] + 1, requires_review=True,
                          note='The final archive audit could not be saved; no remaining chat was deleted.',
                          journal_error=str(error)[:500])
            uncertain = True
        if not uncertain and not any(result.get(key) for key in ('cancelled', 'deferred', 'partial')):
            emit(100, f'Archive retention finished: {result["deleted_threads"]} chats removed; results saved.')
        return result


def run_archive_cleanup(backend, settings, *, progress_callback=None, cancel_event=None):
    return CodexArchiveCleanup(backend.data_dir, settings_reader=backend.preferences).run(
        settings, progress_callback=progress_callback, cancel_event=cancel_event)
