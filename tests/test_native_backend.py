"""Native operations use real temporary fixtures and mocked system commands."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

from optimus.native_backend import NativeBackend, NativeProcessHarness
from optimus.native_operations import (NativeFiles, NativePackages, NativeRunner,
                                       NativeStartup, UnsupportedOperation)
from optimus.native_workflow import run_native_software, run_native_startup, verify_native_outcomes


class FileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='optimus-native-files-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.root = self.home / 'Documents'
        self.root.mkdir()
        self.engine = NativeFiles(home=self.home)

    def files(self):
        for name in ('keeper.txt', 'extra.txt'):
            (self.root / name).write_text('identical contents', encoding='utf-8')
        (self.root / 'different.txt').write_text('other', encoding='utf-8')

    def test_scan_hashes_and_trashes_only_one_verified_extra(self):
        self.files()
        report = self.engine.duplicates(self.root)
        self.assertTrue(report['complete'])
        self.assertEqual(len(report['groups']), 1)
        rows = report['groups'][0]['files']
        self.assertEqual(rows[0]['SHA256'], hashlib.sha256(b'identical contents').hexdigest())
        trash = self.home / '.trash'
        trash.mkdir()
        fake = types.SimpleNamespace(send2trash=lambda path: Path(path).rename(trash / Path(path).name))
        with patch.dict(sys.modules, {'send2trash': fake}):
            result = self.engine.recycle(rows[0])
            self.assertTrue(result['verified'])
            self.assertTrue(Path(result['keeper']).is_file())
            with self.assertRaisesRegex(ValueError, 'keeper'):
                self.engine.recycle(rows[1])
        self.assertEqual(len(list(trash.iterdir())), 1)

    def test_changed_content_is_not_deleted_and_caller_cannot_retarget(self):
        self.files()
        row = self.engine.duplicates(self.root)['duplicates'][0]
        forged = dict(row, Path=str(self.root / 'different.txt'))
        Path(row['Path']).write_text('changed contents', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.engine.recycle(forged)
        self.assertTrue((self.root / 'different.txt').exists())
        self.assertTrue(Path(row['Path']).exists())

    def test_unscanned_export_cannot_authorize_recycling(self):
        self.files()
        row = self.engine.duplicates(self.root)['duplicates'][0]
        other = NativeFiles(home=self.home)
        with self.assertRaisesRegex(ValueError, 'expired'):
            other.recycle(row)

    def test_root_and_link_scopes_are_rejected(self):
        with self.assertRaises(ValueError):
            self.engine.duplicates(self.home)
        outside = self.home.parent
        with self.assertRaises(ValueError):
            self.engine.storage(outside)
        link = self.home / 'Linked'
        try:
            link.symlink_to(self.root, target_is_directory=True)
        except OSError:
            return
        with self.assertRaises(ValueError):
            self.engine.duplicates(link)

    def test_cancelled_scan_grants_no_authority(self):
        self.files()
        cancel = threading.Event()
        cancel.set()
        report = self.engine.duplicates(self.root, cancel_event=cancel)
        self.assertFalse(report['complete'])
        self.assertNotIn(report['id'], self.engine._plans)

    def test_trash_failure_keeps_original_bytes_and_durable_recovery_map(self):
        self.files()
        row = self.engine.duplicates(self.root)['duplicates'][0]
        fake = types.SimpleNamespace(send2trash=Mock(side_effect=OSError('Trash unavailable')))
        with patch.dict(sys.modules, {'send2trash': fake}), self.assertRaisesRegex(OSError, 'Trash unavailable'):
            self.engine.recycle(row)
        self.assertEqual(Path(row['Path']).read_text(encoding='utf-8'), 'identical contents')
        journal = next((self.engine.data_dir / 'native-recycle').glob('*.json'))
        value = json.loads(journal.read_text(encoding='utf-8'))
        self.assertEqual(value['path'], row['Path'])
        self.assertEqual(value['state'], 'preserved')
        self.assertTrue(Path(value['keeper']).is_file())

    def test_journal_failure_cannot_stage_or_recycle_a_duplicate(self):
        self.files()
        row = self.engine.duplicates(self.root)['duplicates'][0]
        fake = types.SimpleNamespace(send2trash=Mock())
        with patch.dict(sys.modules, {'send2trash': fake}), patch('optimus.native_operations.atomic_json', side_effect=OSError('Journal full')):
            with self.assertRaisesRegex(OSError, 'Journal full'):
                self.engine.recycle(row)
        fake.send2trash.assert_not_called()
        self.assertTrue(Path(row['Path']).is_file())

    @unittest.skipUnless(sys.platform == 'linux', 'Isolates the real Linux XDG Trash API in a fresh process.')
    def test_real_linux_trash_is_confined_to_fixture_xdg_data_home(self):
        self.files()
        code = '''
import json, sys
from pathlib import Path
from optimus.native_operations import NativeFiles
home = Path(sys.argv[1])
engine = NativeFiles(home=home, data_dir=home / 'data')
scan = engine.duplicates(home / 'Documents')
assert scan['complete'], scan
result = engine.recycle(scan['duplicates'][0])
journal = json.loads(Path(result['journal_path']).read_text(encoding='utf-8'))
trash = home / 'xdg-data/Trash/files'
assert result['verified'] is True, result
assert not Path(result['path']).exists(), result
assert Path(result['keeper']).is_file(), result
assert (trash / result['trash_name']).read_bytes() == b'identical contents', result
assert journal['state'] == 'recycled' and journal['path'] == result['path'], journal
assert Path(journal['staged_path']).parent == Path(result['restore_staging_folder']), journal
assert Path(result['path']).name == result['trash_name'], result
print(json.dumps({'verified': True, 'isolated_trash': str(trash), 'original_filename': result['trash_name']}))
'''
        environment = dict(os.environ, XDG_DATA_HOME=str(self.home / 'xdg-data'))
        source = str(Path(__file__).resolve().parents[1] / 'src')
        environment['PYTHONPATH'] = source + (os.pathsep + environment['PYTHONPATH'] if environment.get('PYTHONPATH') else '')
        run = subprocess.run([sys.executable, '-c', code, str(self.home)], env=environment,
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        result = json.loads(run.stdout.strip())
        self.assertTrue(result['verified'])
        self.assertTrue(Path(result['isolated_trash']).is_relative_to(self.home))


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.runner = Mock()
        self.packages = NativePackages(Path('unused'), system='linux', runner=self.runner)
        self.packages.manager, self.packages.executable = 'apt', '/usr/bin/apt'

    @staticmethod
    def result(text='', code=0):
        return {'exitCode': code, 'stdout': text, 'stderr': ''}

    def apt_command(self, argv, **kwargs):
        if argv[0] == '/usr/bin/dpkg-query':
            return self.result('editor\t1:2.0~rc1-1\tinstalled\nheld\t3.0\tinstalled\n')
        if argv[0] == '/usr/bin/apt':
            return self.result('Listing...\neditor/stable 1:2.0-1 amd64 [upgradable from: 1:2.0~rc1-1]\n')
        if argv[0] == '/usr/bin/dpkg':
            self.assertEqual(argv, ['/usr/bin/dpkg', '--compare-versions', '1:2.0-1', 'gt', '1:2.0~rc1-1'])
            return self.result()
        self.fail('Unexpected native command: ' + repr(argv))

    def test_debian_versions_use_dpkg_and_only_offered_installed_package(self):
        self.runner.run.side_effect = self.apt_command
        scan = self.packages.scan()
        self.assertEqual([row['id'] for row in scan['updates']], ['apt:editor'])
        self.assertEqual(scan['updates'][0]['version'], '1:2.0~rc1-1')
        self.assertTrue(scan['updates'][0]['eligible'])
        self.runner.privileged.assert_not_called()

    def test_update_is_exact_version_and_verified_with_fresh_installed_inventory(self):
        self.runner.run.side_effect = self.apt_command
        row = self.packages._record('apt', 'editor', '1.0', '2.0')
        self.packages.scan = Mock(return_value={'updates': [row]})
        self.packages.installed = Mock(return_value=[self.packages._record('apt', 'editor', '2.0')])
        self.runner.privileged.return_value = self.result()
        with patch('optimus.native_operations.shutil.disk_usage', return_value=types.SimpleNamespace(free=10 * 1024 ** 3)):
            result = self.packages.apply('apt:editor', confirmed=True)
        command = self.runner.privileged.call_args.args[0]
        self.assertEqual(command, ['/usr/bin/apt-get', '--assume-yes', '--no-remove', '--only-upgrade', 'install', 'editor=2.0'])
        self.assertTrue(result['verified'])
        self.packages.scan.assert_called_once()

    def test_no_offered_update_and_failed_version_check_never_claim_success(self):
        self.packages.scan = Mock(return_value={'updates': []})
        result = self.packages.apply('apt:editor', confirmed=True)
        self.assertTrue(result['skipped'])
        self.runner.privileged.assert_not_called()
        self.packages.scan.return_value = {'updates': [self.packages._record('apt', 'editor', '1', '2')]}
        self.packages.installed = Mock(return_value=[self.packages._record('apt', 'editor', '1')])
        self.runner.privileged.return_value = self.result()
        with patch('optimus.native_operations.shutil.disk_usage', return_value=types.SimpleNamespace(free=10 * 1024 ** 3)):
            result = self.packages.apply('apt:editor', confirmed=True)
        self.assertFalse(result['success'])
        self.assertTrue(result['requiresReview'])

    def test_no_confirmation_and_cancelled_install_cannot_invoke_broker(self):
        with self.assertRaises(ValueError):
            self.packages.apply('apt:editor')
        self.packages.scan = Mock(return_value={'updates': [self.packages._record('apt', 'editor', '1', '2')]})
        cancelled = threading.Event()
        cancelled.set()
        self.assertTrue(self.packages.apply('apt:editor', confirmed=True, cancel_event=cancelled)['cancelled'])
        self.runner.privileged.assert_not_called()

    def test_arch_does_not_create_partial_system_upgrades(self):
        self.packages.manager = 'pacman'
        self.packages.scan = Mock(return_value={'updates': [self.packages._record('pacman', 'editor', '1', '2')]})
        with self.assertRaisesRegex(UnsupportedOperation, 'partial'):
            self.packages.apply('pacman:editor', confirmed=True)
        self.runner.privileged.assert_not_called()

    def test_brew_does_not_offer_pinned_or_uninstalled_formulae(self):
        self.packages.manager, self.packages.executable = 'brew', '/opt/homebrew/bin/brew'
        def command(argv, **kwargs):
            if argv[1] == 'info':
                return self.result(json.dumps({'formulae': [{'name': 'editor', 'installed': [{'version': '1.0'}]},
                                                           {'name': 'pinned', 'installed': [{'version': '1.0'}]}]}))
            return self.result(json.dumps({'formulae': [{'name': 'editor', 'current_version': '2.0'},
                                                        {'name': 'pinned', 'current_version': '2.0', 'pinned': True},
                                                        {'name': 'missing', 'current_version': '2.0'}]}))
        self.runner.run.side_effect = command
        self.assertEqual([row['id'] for row in self.packages.scan()['updates']], ['brew:editor'])

    def test_dnf_zero_epoch_offer_matches_exact_post_install_rpm_version(self):
        self.packages.manager, self.packages.executable = 'dnf', '/usr/bin/dnf'
        installed = {'version': '0:1.0-1'}
        def command(argv, **kwargs):
            if argv[0] == '/usr/bin/rpm':
                return self.result('editor.x86_64\t' + installed['version'] + '\n')
            return self.result('editor.x86_64 2.0-1 updates\n', code=100)
        def apply(argv, **kwargs):
            self.assertEqual(argv, ['/usr/bin/dnf', '--assumeyes', 'upgrade', 'editor-0:2.0-1.x86_64'])
            installed['version'] = '0:2.0-1'
            return self.result()
        self.runner.run.side_effect = command
        self.runner.privileged.side_effect = apply
        scan = self.packages.scan()
        self.assertEqual(scan['updates'][0]['available'], '0:2.0-1')
        with patch('optimus.native_operations.shutil.disk_usage', return_value=types.SimpleNamespace(free=10 * 1024 ** 3)):
            result = self.packages.apply('dnf:editor.x86_64', confirmed=True)
        self.assertTrue(result['verified'])
        self.assertEqual(result['installed_version'], '0:2.0-1')

    def test_privilege_broker_uses_argument_vector_and_cancellation_gates(self):
        native = NativeRunner(system='linux')
        native.run = Mock(return_value=self.result())
        with patch('optimus.native_operations.os.geteuid', return_value=1000, create=True), patch('optimus.native_operations.Path.is_file', return_value=True):
            native.privileged(['/usr/bin/apt-get', 'install', 'example=1.0'])
        self.assertEqual(native.run.call_args.args[0], [str(Path('/usr/bin/pkexec')), '/usr/bin/apt-get', 'install', 'example=1.0'])


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='optimus-native-startup-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.data = self.home / 'data'
        self.runner = Mock()
        self.engine = NativeStartup(self.data, system='linux', home=self.home, runner=self.runner)
        self.engine.root = self.home / '.config/autostart'
        self.engine.root.mkdir(parents=True)
        self.entry = self.engine.root / 'editor.desktop'
        self.original = b'[Desktop Entry]\nType=Application\nName=Editor\nExec=/usr/bin/editor --background\n'
        self.entry.write_bytes(self.original)

    def test_disabled_user_entry_recovers_exact_original_bytes(self):
        current = self.engine.list()[0]
        self.assertEqual(current['Program'], '/usr/bin/editor')
        result = self.engine.set(current, False)
        self.assertTrue(result['verified'])
        self.assertFalse(self.engine.list()[0]['Enabled'])
        restored = self.engine.restore(result['change']['Id'])
        self.assertTrue(restored['verified'])
        self.assertEqual(self.entry.read_bytes(), self.original)
        self.runner.run.assert_not_called()

    def test_changed_selection_and_changed_postcondition_are_rejected(self):
        current = self.engine.list()[0]
        self.entry.write_bytes(self.original + b'Comment=changed\n')
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.engine.set(current, False)
        current = self.engine.list()[0]
        result = self.engine.set(current, False)
        self.entry.write_bytes(self.entry.read_bytes() + b'Comment=morechanges\n')
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.engine.restore(result['change']['Id'])

    def test_own_optimus_entry_is_controlled_by_its_setting(self):
        self.entry.write_text('[Desktop Entry]\nType=Application\nName=Optimus\nExec=/opt/optimus/Optimus\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'setting'):
            self.engine.set(self.engine.list()[0], False)

    def test_macos_launchagent_changes_only_exact_user_registration(self):
        engine = NativeStartup(self.data, system='darwin', home=self.home, runner=self.runner)
        engine.root.mkdir(parents=True)
        path = engine.root / 'org.example.Editor.plist'
        content = plistlib.dumps({'Label': 'org.example.Editor', 'ProgramArguments': ['/Applications/Editor.app/Contents/MacOS/editor'], 'RunAtLoad': True})
        path.write_bytes(content)
        self.runner.run.return_value = {'exitCode': 0, 'stdout': '', 'stderr': ''}
        with patch('optimus.native_operations._uid', return_value=501), patch('optimus.native_operations._owned', return_value=True):
            result = engine.set(engine.list()[0], False)
            self.assertFalse(path.exists())
            self.assertTrue(path.with_suffix('.disabled').is_file())
            self.runner.run.assert_not_called()  # Next sign-in only; never stops a live application.
            self.assertTrue(engine.restore(result['change']['Id'])['verified'])
        self.assertEqual(path.read_bytes(), content)

    def test_recovery_journal_failure_blocks_startup_mutation(self):
        record = self.engine.list()[0]
        with patch('optimus.native_operations.atomic_json', side_effect=OSError('Disk full')):
            with self.assertRaisesRegex(OSError, 'Disk full'):
                self.engine.set(record, False)
        self.assertEqual(self.entry.read_bytes(), self.original)

    def test_final_journal_failure_leaves_usable_prepared_recovery(self):
        from optimus.workflow import atomic_json
        def persist(path, value):
            if value[-1].get('state') == 'applied':
                raise OSError('Final journal failed')
            return atomic_json(path, value)
        with patch('optimus.native_operations.atomic_json', side_effect=persist), self.assertRaisesRegex(OSError, 'Final journal failed'):
            self.engine.set(self.engine.list()[0], False)
        history = self.engine.history()
        self.assertEqual(history[-1]['state'], 'prepared')
        self.assertTrue(self.engine.restore(history[-1]['Id'])['verified'])
        self.assertEqual(self.entry.read_bytes(), self.original)

    def test_macos_own_registration_is_protected_by_label_and_filename(self):
        engine = NativeStartup(self.data, system='darwin', home=self.home, runner=self.runner)
        engine.root.mkdir(parents=True)
        path = engine.root / 'io.github.ag3nt47.optimus.plist'
        path.write_bytes(plistlib.dumps({'Label': 'io.github.ag3nt47.optimus', 'Program': '/Applications/Optimus.app/Contents/MacOS/Optimus'}))
        record = engine.list()[0]
        self.assertTrue(record['Protected'])
        self.assertFalse(record['CanManage'])
        with self.assertRaisesRegex(ValueError, 'setting'):
            engine.set(record, False)


class WorkflowTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform in ('linux', 'darwin'), 'Exercises real native maintenance primitives on macOS/Linux.')
    def test_real_native_one_click_only_cleans_owned_fixture_scope_and_verifies(self):
        from optimus.cleaner import Cleaner, Category
        from optimus.native_settings import NativePreferences
        with tempfile.TemporaryDirectory(prefix='optimus-real-native-') as directory:
            root = Path(directory).resolve()  # macOS /var is an OS-owned alias.
            cache = root / 'personal-cache'
            cache.mkdir()
            target = cache / 'stale.tmp'
            target.write_bytes(b'approved temporary fixture')
            unrelated = root / 'unrelated-document.txt'
            unrelated.write_bytes(b'personal document must survive')
            backend = NativeBackend(root / 'data')
            try:
                backend.cleaner = Cleaner(backend.data_dir, categories=[Category('fixture-cache', 'Test cache', cache, 'Owned fixture only.')])
                configuration = NativePreferences.safe_defaults()
                configuration.update(setup_version=2, cleanup_enabled=True, cleanup_age_days=0,
                                     cleanup_category_ids=['fixture-cache'])
                events = []
                report = backend.optimize(configuration, events.append)
                self.assertEqual(report['status'], 'completed', report)
                self.assertFalse(target.exists())
                self.assertEqual(unrelated.read_bytes(), b'personal document must survive')
                self.assertEqual(report['final_verification']['status'], 'confirmed')
                self.assertGreaterEqual(report['final_verification']['counts']['confirmed'], 1)
                saved = json.loads(Path(report['log_path']).read_text(encoding='utf-8'))
                self.assertEqual(saved['status'], 'completed')
                self.assertEqual(events[-1]['percentage'], 100)
            finally:
                backend.close()

    def test_saved_selected_package_scope_and_exclusions_are_enforced(self):
        backend = Mock()
        backend.updates_scan.return_value = {'source': 'apt', 'complete': True, 'structured': True,
                                              'exitCode': 0, 'updates': [{'id': 'apt:editor', 'version': '1'}, {'id': 'apt:other', 'version': '1'}]}
        backend.update_app_unattended.return_value = {'exitCode': 0, 'verified': True, 'installed_version': '2'}
        settings = {'setup_version': 2, 'software_update_policy': 'selected', 'software_update_agreements_accepted': True,
                    'software_update_ids': ['apt:editor', 'apt:other'], 'software_update_exclude_ids': ['apt:other']}
        result = run_native_software(backend, settings)
        self.assertEqual(result['failed_items'], 0)
        backend.update_app_unattended.assert_called_once_with('apt:editor', cancel_event=None)

    def test_unapproved_update_never_starts_and_changed_startup_is_skipped(self):
        backend = Mock()
        backend.updates_scan.return_value = {'complete': True, 'structured': True, 'exitCode': 0, 'updates': []}
        with self.assertRaises(ValueError):
            run_native_software(backend, {'setup_version': 2, 'software_update_policy': 'selected', 'software_update_agreements_accepted': False})
        backend.update_app_unattended.assert_not_called()
        backend.startup_list.return_value = []
        backend.native_startup.descriptor.side_effect = lambda row: row
        result = run_native_startup(backend, {'setup_version': 2, 'startup_policy': 'disable_selected', 'startup_changes_approved': True,
                                             'startup_approved_entries': [{'Name': 'Old registration'}]})
        self.assertEqual(result['items'][0]['status'], 'skipped')
        backend.startup_set.assert_not_called()

    def test_outcome_verification_detects_real_current_version_mismatch(self):
        backend = Mock()
        backend.native_packages.installed.return_value = [{'id': 'apt:editor', 'version': '1'}]
        report = {'stages': [{'title': 'Review or install approved native package updates', 'status': 'completed',
                              'result': {'items': [{'id': 'apt:editor', 'status': 'completed', 'from_version': '1',
                                                     'result': {'installed_version': '2'}}]}}]}
        result = verify_native_outcomes(backend, report)
        self.assertEqual(result['status'], 'needs_review')
        backend.update_app_unattended.assert_not_called()

    def test_cancelled_final_verification_does_not_claim_a_confirmed_empty_result(self):
        cancelled = threading.Event()
        cancelled.set()
        result = verify_native_outcomes(Mock(), {'stages': []}, cancel_event=cancelled)
        self.assertEqual(result['status'], 'unverified')
        self.assertFalse(result['coverage_complete'])

    def test_exported_process_report_is_inert_and_both_export_paths_work(self):
        backend = Mock()
        backend.processes.return_value = [{'pid': 100, 'ppid': 1, 'name': 'editor', 'exe': '/opt/editor',
                                           'create_time': 1.0, 'protected': False, 'can_sleep': True}]
        backend.startup_list.return_value = []
        backend.sleep_process.return_value = {'status': 'sleeping', 'verified': True}
        harness = NativeProcessHarness(backend)
        report = harness.scan()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        directory = temp.name
        backend.data_dir = Path(directory)
        exported = harness.export(report['id'], Path(directory) / 'report.json')
        data = json.loads(Path(exported['path']).read_text(encoding='utf-8'))
        self.assertNotIn('cmdline', data['processes'][0])
        self.assertTrue(Path(harness.export(report['id'], Path(directory) / 'folder')['path']).is_file())
        other = NativeProcessHarness(backend)
        action_id = report['actions'][0]['id']
        with self.assertRaises(ValueError):
            other.apply({'plan_id': report['id'], 'selected_ids': [action_id], 'confirmed': True})
        result = harness.apply({'plan_id': report['id'], 'selected_ids': [action_id], 'confirmed': True})
        self.assertEqual(result['failed_items'], 0)
        backend.sleep_process.assert_called_once()

    def test_apple_os_update_labels_are_exact_and_never_auto_restart(self):
        backend = object.__new__(NativeBackend)
        backend.system = 'darwin'
        backend.native_runner = Mock()
        backend.native_runner.run.return_value = {'exitCode': 0, 'stderr': '', 'stdout': 'Software Update Tool\n* Label: Security Update 2026-001-1.0\n\tTitle: Security Update, Version: 1.0, Size: 2000KiB, Recommended: YES, Action: restart,\n'}
        scan = backend.os_updates_scan()
        self.assertTrue(scan['complete'])
        self.assertEqual(len(scan['updates']), 1)
        with self.assertRaises(ValueError):
            backend.install_os_update(scan['updates'][0]['id'], confirmed=True)
        backend.update_disk_readiness = Mock(return_value={'ready': True})
        backend.native_runner.privileged.return_value = {'exitCode': 0}
        backend.os_updates_scan = Mock(side_effect=[scan, dict(scan, updates=[])])
        result = backend.install_os_update(scan['updates'][0]['id'], confirmed=True, agreements_accepted=True)
        command = backend.native_runner.privileged.call_args.args[0]
        self.assertEqual(command, ['/usr/sbin/softwareupdate', '--install', 'Security Update 2026-001-1.0', '--agree-to-license'])
        self.assertNotIn('--restart', command)
        self.assertTrue(result['verified'])


if __name__ == '__main__':
    unittest.main()
