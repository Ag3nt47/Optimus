"""Archive deletion authorization and receipts using disposable local fixtures only."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
import uuid

import pytest

from optimus import codex_archives as archives


NOW = 1800000000
OLD = NOW - 14 * 86400
SETTINGS = {'codex_archives_cleanup_enabled': True, 'codex_archives_retention_days': 7}


class Fixture:
    def __init__(self, root, monkeypatch):
        self.home, self.data = root / 'codex', root / 'optimus'
        (self.home / 'archived_sessions').mkdir(parents=True)
        self.database = self.home / 'state_5.sqlite'
        self.connection = sqlite3.connect(self.database)
        self.connection.executescript('CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, '
            'archived INTEGER, archived_at INTEGER, updated_at INTEGER, is_pinned INTEGER); '
            'CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, child_thread_id TEXT, status TEXT);')
        self.connection.commit()
        self.executable = root / 'codex.exe'
        self.executable.write_bytes(b'disposable CLI fixture; never executed')
        monkeypatch.setattr(archives.shutil, 'which', lambda name: str(self.executable))
        self.calls, self.paths, self.mode = [], {}, 'delete'

    def thread(self, *, archived=1, pinned=0, archived_at=OLD, updated_at=OLD, file_age=OLD, present=True):
        identity = str(uuid.uuid4())
        path = self.home / 'archived_sessions' / ('rollout-2026-01-01T00-00-00-' + identity + '.jsonl')
        if present:
            path.write_bytes(b'private fixture history\n')
            os.utime(path, (file_age, file_age))
        self.connection.execute('INSERT INTO threads VALUES (?,?,?,?,?,?)',
            (identity, str(path), archived, archived_at, updated_at, pinned))
        self.connection.commit()
        self.paths[identity] = path
        return identity

    def runner(self, argv, home):
        assert Path(home) == self.home and argv[0] == str(self.executable.resolve())
        self.calls.append(argv)
        if argv[1:] == ['delete', '--help']:
            return subprocess.CompletedProcess(argv, 0, 'delete UUID --force', '')
        assert argv[1] == 'delete' and argv[3:] == ['--force']
        identity = argv[2]
        if self.mode == 'timeout':
            raise subprocess.TimeoutExpired(argv, 45)
        if self.mode == 'failure':
            return subprocess.CompletedProcess(argv, 1, '', 'failed fixture')
        if self.mode == 'unarchive':
            self.connection.execute('UPDATE threads SET archived=0 WHERE id=?', (identity,))
        else:
            self.connection.execute('DELETE FROM threads WHERE id=?', (identity,))
            path = self.paths[identity]
            if path.exists() and self.mode != 'leave_file':
                path.unlink()
        self.connection.commit()
        return subprocess.CompletedProcess(argv, 0, 'deleted', '')

    def run(self, **options):
        return archives.CodexArchiveCleanup(self.data, home=self.home, runner=self.runner,
            closed=options.pop('closed', lambda: True), clock=lambda: NOW,
            settings_reader=options.pop('settings_reader', None)).run(dict(SETTINGS), **options)


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    value = Fixture(tmp_path, monkeypatch)
    yield value
    value.connection.close()


def test_only_old_unpinned_leaf_is_deleted_and_actual_files_counted(fixture):
    approved = fixture.thread()
    preserved = [fixture.thread(pinned=1), fixture.thread(archived_at=NOW - 60),
                 fixture.thread(updated_at=NOW - 60), fixture.thread(archived=0)]
    parent = fixture.thread()
    child = fixture.thread(archived=0)
    fixture.connection.execute('INSERT INTO thread_spawn_edges VALUES (?,?,?)', (parent, child, 'done'))
    fixture.connection.commit()
    events = []
    result = fixture.run(progress_callback=events.append)
    assert result['deleted_threads'] == result['deleted_count'] == 1
    assert result['deleted_bytes'] == len(b'private fixture history\n')
    assert not fixture.paths[approved].exists()
    assert all(fixture.paths[value].exists() for value in preserved + [parent, child])
    assert fixture.calls[-1][2] == approved
    assert events[-1]['item_progress']['percent'] == 100
    assert events[-1]['deleted_bytes'] == result['deleted_bytes']


def test_disabled_or_open_codex_never_reads_metadata_or_calls_cli(fixture, monkeypatch):
    monkeypatch.setattr(archives, '_metadata', lambda home: pytest.fail('must not read'))
    result = archives.CodexArchiveCleanup(fixture.data, home=fixture.home,
        runner=fixture.runner, closed=lambda: pytest.fail('disabled must not inspect')).run({})
    assert result['skipped'] and not fixture.calls
    assert fixture.run(closed=lambda: False)['deferred']
    assert not fixture.calls


def test_empty_process_inventory_does_not_authorize_archive_deletion(monkeypatch):
    monkeypatch.setattr(archives.psutil, 'process_iter', lambda *args, **kwargs: iter(()))
    assert archives.codex_closed() is False


def test_missing_rollout_is_metadata_deletion_and_zero_removed_bytes(fixture):
    fixture.thread(present=False)
    result = fixture.run()
    assert result['deleted_threads'] == 1
    assert result['deleted_count'] == result['deleted_bytes'] == 0


@pytest.mark.parametrize('mode', ['unarchive', 'leave_file', 'failure', 'timeout'])
def test_uncertain_native_deletion_never_reports_success_or_retries(fixture, mode):
    fixture.thread()
    fixture.mode = mode
    result = fixture.run()
    assert result['failed_items'] == 1
    assert result['deleted_threads'] == result['deleted_count'] == result['deleted_bytes'] == 0
    saved = json.loads(Path(result['log_path']).read_text(encoding='utf-8'))
    assert saved['status'] == 'uncertain'
    calls = len(fixture.calls)
    assert fixture.run()['requires_review']
    assert len(fixture.calls) == calls


@pytest.mark.parametrize('kind', ['recent', 'outside', 'hardlink'])
def test_unverified_physical_rollout_is_preserved_before_delete(fixture, tmp_path, kind):
    identity = fixture.thread(file_age=NOW if kind == 'recent' else OLD)
    if kind == 'outside':
        fixture.connection.execute('UPDATE threads SET rollout_path=? WHERE id=?',
            (str(tmp_path / fixture.paths[identity].name), identity))
        fixture.connection.commit()
    elif kind == 'hardlink':
        try:
            os.link(fixture.paths[identity], tmp_path / 'linked-history.jsonl')
        except OSError:
            pytest.skip('fixture filesystem does not support hard links')
    result = fixture.run()
    assert result['failed_items'] == 1 and fixture.paths[identity].exists()
    assert len(fixture.calls) == 1  # Supported-CLI help only.
    assert result['items'][0]['status'] == 'preserved'


def test_compressed_rollout_alternative_is_verified_and_deleted_by_cli(fixture):
    identity = fixture.thread()
    path = fixture.paths[identity]
    compressed = Path(str(path) + '.zst')
    path.rename(compressed)
    fixture.paths[identity] = compressed
    assert fixture.run()['deleted_count'] == 1


def test_compressed_metadata_does_not_hide_unapproved_plain_sibling(fixture):
    identity = fixture.thread()
    compressed = Path(str(fixture.paths[identity]) + '.zst')
    compressed.write_bytes(b'fixture compressed duplicate')
    os.utime(compressed, (OLD, OLD))
    fixture.connection.execute('UPDATE threads SET rollout_path=? WHERE id=?', (str(compressed), identity))
    fixture.connection.commit()
    result = fixture.run()
    assert result['failed_items'] == 1 and result['deleted_threads'] == 0
    assert compressed.exists() and fixture.paths[identity].exists()
    assert len(fixture.calls) == 1


def test_changed_choices_or_closed_state_preserves_remaining_archives(fixture):
    fixture.thread()
    changed = dict(SETTINGS, codex_archives_retention_days=30)
    result = fixture.run(settings_reader=lambda: changed)
    assert result['cancelled'] and result['deleted_threads'] == 0
    assert json.loads(Path(result['log_path']).read_text())['status'] == 'cancelled'
    assert len(fixture.calls) == 1


def test_cancellation_does_not_show_fake_completion(fixture):
    fixture.thread()
    class Cancel:
        def is_set(self):
            return True
    events = []
    result = fixture.run(cancel_event=Cancel(), progress_callback=events.append)
    assert result['cancelled'] and result['deleted_threads'] == 0
    assert events[-1]['item_progress']['percent'] == 0


def test_ambiguous_or_unknown_database_schema_cannot_authorize_deletion(fixture):
    fixture.thread()
    extra = fixture.home / 'state_6.sqlite'
    extra.write_bytes(b'unknown database')
    with pytest.raises(ValueError, match='ambiguous'):
        fixture.run()
    assert not fixture.calls
    extra.unlink()
    fixture.connection.execute('DROP TABLE thread_spawn_edges')
    fixture.connection.commit()
    with pytest.raises(ValueError, match='schema'):
        fixture.run()
    assert not fixture.calls


def test_metadata_probe_is_read_only(fixture):
    identity = fixture.thread()
    before = fixture.database.read_bytes()
    rows, parents, all_ids = archives._metadata(fixture.home)
    assert rows[identity]['pinned'] is False and not parents and identity in all_ids
    assert fixture.database.read_bytes() == before


def test_malformed_history_cannot_be_silently_ignored(fixture):
    fixture.thread()
    root = fixture.data / 'retention-cleanup'
    root.mkdir(parents=True)
    (root / ('codex-archives-' + 'a' * 32 + '.json')).write_text('{"status":"completed"}')
    assert fixture.run()['requires_review']
    assert not fixture.calls


def test_terminal_audit_write_failure_stops_before_another_archive(fixture, monkeypatch):
    first, second = fixture.thread(), fixture.thread()
    real_write, writes = archives._private_json, []
    def write(path, value):
        writes.append(value['status'])
        if len(writes) >= 3:
            raise OSError('fixture terminal journal unavailable')
        return real_write(path, value)
    monkeypatch.setattr(archives, '_private_json', write)
    result = fixture.run()
    assert result['deleted_threads'] == 1 and result['requires_review']
    assert len([call for call in fixture.calls if call[-1] == '--force']) == 1
    assert sum(path.exists() for path in fixture.paths.values()) == 1
    assert not any(item['status'] == 'preserved' for item in result['items'])
    saved = json.loads(Path(result['log_path']).read_text(encoding='utf-8'))
    assert saved['status'] == 'prepared'
    assert saved['current_id'] in (first, second)


def test_final_activity_guard_after_intent_prevents_cli_delete(fixture, monkeypatch):
    identity = fixture.thread()
    real_write, running = archives._private_json, [False]
    def write(path, value):
        real_write(path, value)
        if value['status'] == 'prepared':
            running[0] = True
    monkeypatch.setattr(archives, '_private_json', write)
    events = []
    result = fixture.run(closed=lambda: not running[0], progress_callback=events.append)
    assert result['deferred'] and result['deleted_threads'] == 0
    assert fixture.paths[identity].exists() and len(fixture.calls) == 1
    assert json.loads(Path(result['log_path']).read_text())['status'] == 'deferred'
    assert events[-1]['item_progress']['percent'] == 0
