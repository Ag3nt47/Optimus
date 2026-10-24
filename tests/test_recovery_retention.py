"""Recovery expiry is opt-in and serialized against actual browser restore APIs."""
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import uuid
import subprocess
import sys

import pytest

from optimus import recovery_retention as retention
from optimus.browser_privacy import BrowserPrivacy


@pytest.fixture
def environment(tmp_path):
    data = tmp_path / 'data'
    data.mkdir()
    now = time.time() + 60 * 86400
    config = dict(retention.DEFAULTS, enabled=True, browser_backups=True, lose_browser_undo_approved=True)
    def group(*, state='applied', extra=False, age=45):
        identity = uuid.uuid4().hex
        folder = data / 'BrowserPrivacyBackups' / identity
        folder.mkdir(parents=True)
        database = folder / 'database-0.sqlite'
        database.write_bytes(b'private browser database fixture')
        manifest = {'backupId': identity, 'profileId': uuid.uuid4().hex, 'browser': 'Google Chrome',
            'profilePath': str(tmp_path / 'profile'), 'profileName': 'fixture', 'categories': ['cookies'],
            'state': state, 'createdUtc': datetime.fromtimestamp(now - age * 86400, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'databases': [{'file': database.name, 'sha256': hashlib.sha256(database.read_bytes()).hexdigest(), 'committed': True}]}
        (folder / 'manifest.json').write_text(json.dumps(manifest))
        if extra:
            (folder / 'unexpected-personal-file.txt').write_text('preserve')
        return folder, manifest
    def service(**changes):
        return retention.RecoveryRetention(data, clock=lambda: now, **changes)
    return data, config, group, service, now


def test_expire_exact_completed_group_and_count_only_actual_bytes(environment):
    _, config, group, service, _ = environment
    folder, _ = group()
    expected = sum(path.stat().st_size for path in folder.iterdir())
    cleaner = service()
    plan = cleaner.preview(config)
    assert len(plan['eligible']) == 1 and plan['total_bytes'] == expected
    assert 'no longer be undone' in plan['eligible'][0]['undo_loss']
    result = cleaner.apply(plan, config, confirmed=True)
    assert result['expired_groups'] == 1 and result['deleted_bytes'] == expected and result['deleted_count'] == 2
    assert not folder.exists()


@pytest.mark.parametrize('state', ['prepared', 'partial', 'failed', 'restored', 'restore-partial', 'unknown'])
def test_incomplete_or_restoration_backups_protected(environment, state):
    _, config, group, service, _ = environment
    folder, _ = group(state=state)
    assert not service().preview(config)['eligible'] and folder.exists()


def test_backup_with_unexpected_file_or_changed_database_is_preserved(environment):
    _, config, group, service, _ = environment
    first, _ = group(extra=True)
    second, _ = group()
    (second / 'database-0.sqlite').write_bytes(b'changed')
    result = service().run(config)
    assert result['deleted_count'] == 0 and first.exists() and second.exists()


def test_backup_updated_after_preview_is_preserved(environment):
    _, config, group, service, _ = environment
    folder, manifest = group()
    cleaner = service()
    plan = cleaner.preview(config)
    manifest['state'] = 'restore-partial'
    (folder / 'manifest.json').write_text(json.dumps(manifest))
    result = cleaner.apply(plan, config, confirmed=True)
    assert result['deleted_count'] == 0 and folder.exists()


def test_preview_tampering_cannot_target_driver_or_unresolved_update_files(environment):
    data, config, group, service, _ = environment
    folder, _ = group()
    protected = data / 'parallel-updates/active.json'
    protected.parent.mkdir()
    protected.write_text('{"prepared_attempt":true}')
    driver = data / 'driver-backups/driver.sys'
    driver.parent.mkdir()
    driver.write_bytes(b'driver backup')
    cleaner = service()
    plan = cleaner.preview(config)
    plan['eligible'][0]['files'][0]['path'] = str(protected)
    cleaner.apply(plan, config, confirmed=True)
    assert not folder.exists() and protected.exists() and driver.exists()


def test_explicit_undo_loss_approval_required(environment):
    _, config, group, service, _ = environment
    folder, _ = group()
    with pytest.raises(ValueError, match='Approve losing'):
        service().run(dict(config, lose_browser_undo_approved=False))
    cleaner = service()
    plan = cleaner.preview(config)
    with pytest.raises(ValueError, match='Confirm permanent'):
        cleaner.apply(plan, config)
    assert folder.exists()


def test_disabled_expiry_inventories_but_never_removes_backups(environment):
    _, config, group, service, _ = environment
    folder, _ = group()
    assert not service().preview(dict(config, enabled=False))['eligible']
    assert service().run(dict(config, enabled=False))['deleted_count'] == 0 and folder.exists()


def test_recent_backup_and_safety_snapshot_protected(environment):
    data, config, group, service, _ = environment
    folder, _ = group(age=2)
    safety = data / 'BrowserPrivacyBackups/restore-safety-fixture'
    safety.mkdir()
    (safety / 'private.sqlite').write_bytes(b'safety')
    assert not service().preview(config)['eligible'] and folder.exists() and safety.exists()


def test_hardlinked_database_is_not_expired(environment):
    data, config, group, service, _ = environment
    folder, _ = group()
    os.link(folder / 'database-0.sqlite', data / 'private-duplicate.sqlite')
    assert not service().preview(config)['eligible'] and folder.exists()


def test_shared_lock_blocks_actual_browser_restore_and_cleanup_before_any_action(environment):
    data, _, _, _, _ = environment
    browser = BrowserPrivacy(data)
    with retention.browser_backup_lease(data):
        with pytest.raises(retention.BrowserBackupBusy):
            browser.restore_backup(uuid.uuid4().hex, confirmed=True)
        with pytest.raises(retention.BrowserBackupBusy):
            browser.apply({'confirmed': True, 'planId': uuid.uuid4().hex})
    assert not (data / 'BrowserPrivacyBackups').exists()


def test_actual_restore_and_expiry_thread_race_serialized(environment):
    data, config, group, service, _ = environment
    folder, _ = group()
    entered, release, failures = threading.Event(), threading.Event(), []
    def hold():
        with retention.browser_backup_lease(data):
            entered.set()
            release.wait(5)
    thread = threading.Thread(target=hold)
    thread.start()
    assert entered.wait(3)
    try:
        with pytest.raises(retention.BrowserBackupBusy):
            service().run(config)
        with pytest.raises(retention.BrowserBackupBusy):
            BrowserPrivacy(data).restore_backup(folder.name, confirmed=True)
        assert folder.exists()
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()


def test_saved_approval_revoked_preserves_group(environment):
    _, config, group, service, _ = environment
    folder, _ = group()
    saved = copy.deepcopy(config)
    cleaner = service(scope_reader=lambda: saved)
    plan = cleaner.preview(config)
    saved['enabled'] = False
    with pytest.raises(ValueError, match='choices changed'):
        cleaner.apply(plan, config, confirmed=True)
    assert folder.exists()


def test_report_copy_expiry_preserves_original_operation_journal(environment):
    data, config, _, service, now = environment
    source = data / 'runs/owned.json'
    source.parent.mkdir()
    source.write_text('{"state":"completed"}')
    cleaner = service()
    cleaner.clock = lambda: now - 45 * 86400
    exported = Path(cleaner.archive_completed_report({'schema': 1, 'id': uuid.uuid4().hex, 'read_only': True,
        'complete': True, 'examined': 1, 'scanned_bytes': 0,
        'roots': [{'root': str(data), 'bytes': 0, 'file_count': 0, 'complete': True}]}, kind='storage_growth'))
    # Fixture creation time must precede selected age too; clock is advanced.
    cleaner.clock = lambda: now
    result = cleaner.run(config)
    assert result['expired_groups'] == 1 and not exported.exists() and source.exists()


def test_arbitrary_json_in_export_directory_never_eligible(environment):
    data, config, _, service, _ = environment
    directory = data / 'ExportedReports'
    directory.mkdir()
    path = directory / (uuid.uuid4().hex + '.json')
    path.write_text('{"state":"completed","private":"preserve"}')
    assert not service().preview(config)['eligible'] and path.exists()


def test_partial_delete_counts_confirmed_bytes_and_preserves_remaining_file(environment):
    _, config, group, service, _ = environment
    folder, _ = group()
    def delete(record):
        if Path(record['path']).name == 'manifest.json':
            raise PermissionError('fixture locked')
        retention.Cleaner._delete_exact(record)
    result = service(delete=delete).run(config)
    assert not result['complete'] and result['expired_groups'] == 0 and result['deleted_count'] == 1
    assert result['deleted_bytes'] == len(b'private browser database fixture') and (folder / 'manifest.json').exists()


def test_byte_cap_preserves_entire_backup_group(environment):
    _, config, group, service, _ = environment
    folder, _ = group()
    result = service().run(dict(config, max_bytes=1))
    assert result['deleted_count'] == 0 and folder.exists()


def test_os_lease_blocks_independent_process_before_restore(environment):
    data, _, _, _, _ = environment
    code = ('import sys; from optimus.recovery_retention import browser_backup_lease,BrowserBackupBusy; '\
            'exec("try:\\n with browser_backup_lease(sys.argv[1]): pass\\nexcept BrowserBackupBusy:\\n sys.exit(13)\\n")')
    with retention.browser_backup_lease(data):
        result = subprocess.run([sys.executable, '-c', code, str(data)], capture_output=True, text=True,
            timeout=10, creationflags=0x08000000 if os.name == 'nt' else 0)
    assert result.returncode == 13, result.stderr


def test_file_entry_bound_preserves_group_as_a_whole(environment):
    _, config, group, service, _ = environment
    folder, _ = group()
    result = service().run(dict(config, max_entries=1))
    assert result['deleted_count'] == 0 and folder.exists()


def test_actual_browser_cleanup_manifest_is_compatible_with_expiry(environment, tmp_path):
    import sqlite3
    data, config, _, service, _ = environment
    profile = tmp_path / 'Profiles/Default'
    profile.mkdir(parents=True)
    connection = sqlite3.connect(profile / 'Cookies')
    try:
        connection.execute('CREATE TABLE cookies(host_key TEXT, name TEXT, value TEXT)')
        connection.execute('INSERT INTO cookies VALUES(?,?,?)', ('example.fixture', 'fixture', 'private'))
        connection.commit()
    finally:
        connection.close()
    browser = BrowserPrivacy(data)
    browser._discover = lambda: [{'browser': 'Google Chrome', 'name': 'Default', 'path': str(profile),
        'root': str(profile.parent), 'family': 'chromium', 'process': 'fixture-chrome.exe'}]
    browser._closed = lambda _: None
    identity = browser.profiles()[0]['id']
    plan = browser.scan(identity, categories=['cookies'])
    result = browser.apply({'planId': plan['planId'], 'confirmed': True})
    backup = data / 'BrowserPrivacyBackups' / result['backupId']
    expiry = service().run(config)
    assert expiry['expired_groups'] == 1 and not backup.exists() and (profile / 'Cookies').exists()


@pytest.mark.parametrize('kind', [None, 'unknown', 'storage_growth', 'health_check'])
def test_arbitrary_complete_dict_cannot_be_attested_as_export(environment, kind):
    data, _, _, service, _ = environment
    with pytest.raises(ValueError):
        service().archive_completed_report({'complete': True}, kind=kind)
    assert not (data / 'ExportedReports').exists()


def test_only_completed_readonly_health_report_is_archived(environment):
    data, _, _, service, _ = environment
    report = {'schema': 1, 'id': uuid.uuid4().hex, 'read_only': True, 'maintenance_actions_executed': 0,
        'resource_window': {'read_only': True, 'complete': True, 'samples': [{'cpu_percent': 1}]},
        'diagnostics': {'read_only': True, 'status': 'completed', 'errors': []},
        'installer_headroom': {'read_only': True, 'errors': []}}
    path = service().archive_completed_report(report, kind='health_check')
    assert Path(path).exists()
    for changed in (dict(report, maintenance_actions_executed=1), dict(report, read_only=False),
                    dict(report, diagnostics={'read_only': True, 'status': 'partial', 'errors': ['unavailable']})):
        with pytest.raises(ValueError):
            service().archive_completed_report(changed, kind='health_check')
