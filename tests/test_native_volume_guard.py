"""Deterministic multidrive targets, excluded filesystems and unknown readings."""
from __future__ import annotations

from types import SimpleNamespace
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

from optimus import volume_guard as guard


def partition(mountpoint, filesystem='ext4', device='/dev/fixture', options='rw'):
    return SimpleNamespace(mountpoint=mountpoint, fstype=filesystem, device=device, opts=options)


class DriveBufferTests(unittest.TestCase):
    def run_guard(self, rows, free=None, settings=None, platform='linux'):
        reader = mock.Mock(return_value=rows)
        usage = mock.Mock(side_effect=lambda path: SimpleNamespace(total=100 * guard.GIB, free=free[path]))
        with mock.patch.object(guard.sys, 'platform', platform):
            report = guard.check_drive_buffers(settings or {}, partition_reader=reader, usage_reader=usage)
        reader.assert_called_once_with(all=False)
        return report, usage

    def test_all_local_drives_report_independent_targets_and_shortfalls(self):
        report, usage = self.run_guard([partition('/'), partition('/data'), partition('/archive')],
            {'/': guard.GIB, '/data': 2 * guard.GIB, '/archive': 7 * guard.GIB})
        self.assertTrue(report['complete'])
        self.assertEqual(report['status'], 'needs_review')
        self.assertIs(report['review_only'], True)
        self.assertEqual(len(report['volumes']), 3)
        self.assertEqual(usage.call_count, 3)
        self.assertEqual([row['below_target'] for row in report['volumes']], [True, False, False])
        self.assertEqual(report['volumes'][0]['shortfall_bytes'], guard.GIB)
        self.assertIn('shortfall 1.00 GiB', report['warnings'][0])

    def test_exact_boundary_is_healthy_and_one_byte_shortfall_is_not(self):
        report, _ = self.run_guard([partition('/exact'), partition('/short')],
            {'/exact': 2 * guard.GIB, '/short': 2 * guard.GIB - 1})
        self.assertEqual(report['volumes'][0]['shortfall_bytes'], 0)
        self.assertIs(report['volumes'][0]['below_target'], False)
        self.assertEqual(report['volumes'][1]['shortfall_bytes'], 1)
        self.assertIs(report['volumes'][1]['below_target'], True)

    def test_selected_one_gib_target_is_independent_of_installer_minimum(self):
        report, _ = self.run_guard([partition('/')], {'/': guard.GIB},
            settings={'drive_space_guard_target_gib': 1.0})
        self.assertEqual(report['target_bytes'], guard.GIB)
        self.assertEqual(report['status'], 'completed')
        self.assertIs(report['volumes'][0]['below_target'], False)

    def test_fractional_target_rounds_up_to_a_real_byte(self):
        report, _ = self.run_guard([partition('/')], {'/': guard.GIB},
            settings={'drive_space_guard_target_gib': 1.1})
        self.assertEqual(report['target_bytes'], 1181116007)

    def test_network_memory_optical_and_pseudo_mounts_are_never_queried(self):
        excluded = [partition('/tmp', 'tmpfs'), partition('/network', 'nfs4', 'server:/share'),
            partition('/smb', 'cifs', '//server/share'), partition('/container', 'overlay'),
            partition('/optical', 'iso9660'), partition('/remote', 'ext4', '/dev/fake', 'rw,_netdev'),
            partition('/usb', 'ext4', '/dev/fake', 'rw,removable')]
        report, usage = self.run_guard([partition('/'), *excluded], {'/': 3 * guard.GIB})
        self.assertEqual(report['excluded_count'], len(excluded))
        self.assertEqual(len(report['volumes']), 1)
        usage.assert_called_once_with('/')
        self.assertTrue(report['complete'])

    def test_unrecognized_filesystem_stays_unknown_without_usage_query(self):
        report, usage = self.run_guard([partition('/mystery', 'fuse.unknown')], {})
        usage.assert_not_called()
        row = report['volumes'][0]
        self.assertIsNone(row['free_bytes'])
        self.assertIsNone(row['below_target'])
        self.assertIsNone(row['shortfall_bytes'])
        self.assertFalse(report['complete'])
        self.assertIn('unknown', row['warning'])

    def test_unknown_usage_does_not_hide_another_drives_real_warning(self):
        reader = mock.Mock(return_value=[partition('/busy'), partition('/small')])
        def usage(path):
            if path == '/busy':
                raise PermissionError('fixture inaccessible')
            return SimpleNamespace(total=50 * guard.GIB, free=0)
        with mock.patch.object(guard.sys, 'platform', 'linux'):
            report = guard.check_drive_buffers({}, partition_reader=reader, usage_reader=usage)
        self.assertFalse(report['complete'])
        self.assertEqual(len(report['warnings']), 2)
        self.assertIsNone(report['volumes'][0]['free_bytes'])
        self.assertIs(report['volumes'][1]['below_target'], True)
        self.assertEqual(report['volumes'][1]['shortfall_bytes'], 2 * guard.GIB)

    def test_disabled_guard_never_discovers_or_queries_storage(self):
        reader, usage = mock.Mock(), mock.Mock()
        report = guard.check_drive_buffers({'drive_space_guard_enabled': False},
            partition_reader=reader, usage_reader=usage)
        reader.assert_not_called()
        usage.assert_not_called()
        self.assertEqual(report['status'], 'disabled')
        self.assertTrue(report['complete'])
        self.assertEqual(report['volumes'], [])

    def test_invalid_settings_cannot_trigger_native_queries(self):
        reader, usage = mock.Mock(), mock.Mock()
        for field, value in (('drive_space_guard_enabled', 1), ('drive_space_guard_enabled', 'true'),
                ('drive_space_guard_target_gib', True), ('drive_space_guard_target_gib', 0.99),
                ('drive_space_guard_target_gib', 2.01), ('drive_space_guard_target_gib', float('nan')),
                ('drive_space_guard_target_gib', float('inf')), ('drive_space_guard_target_gib', '2')):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                guard.check_drive_buffers({field: value}, partition_reader=reader, usage_reader=usage)
        reader.assert_not_called()
        usage.assert_not_called()

    def test_invalid_free_or_total_stays_unknown(self):
        reader = lambda **_: [partition('/')]
        for free, total in ((True, 100), (1.0, 100), (-1, 100), (101, 100), (0, 0),
                            (0, True), (0, 1.0), (None, 100), (0, 2 ** 64)):
            with self.subTest(free=free, total=total), mock.patch.object(guard.sys, 'platform', 'linux'):
                report = guard.check_drive_buffers({}, partition_reader=reader,
                    usage_reader=lambda _: SimpleNamespace(free=free, total=total))
            self.assertFalse(report['complete'])
            self.assertIsNone(report['volumes'][0]['below_target'])

    def test_windows_fixed_drives_and_mounted_folder_are_included_once(self):
        rows = [partition('C:\\', 'NTFS', 'C:\\'), partition('c:\\', 'NTFS', 'C:\\'),
                partition('D:\\', 'NTFS', 'D:\\'), partition('C:\\Mounts\\Archive\\', 'NTFS', 'volume-fixture')]
        free = {'C:\\': guard.GIB, 'D:\\': 4 * guard.GIB, 'C:\\Mounts\\Archive\\': 0}
        with mock.patch.object(guard, '_windows_drive_type', return_value=3) as kind:
            report, usage = self.run_guard(rows, free, platform='win32')
        self.assertEqual(len(report['volumes']), 3)
        self.assertEqual(kind.call_count, 3)
        self.assertEqual(usage.call_count, 3)
        self.assertEqual([row['name'] for row in report['volumes']], ['C:', 'D:', 'C:\\Mounts\\Archive'])
        self.assertEqual(len(report['warnings']), 2)

    def test_windows_network_optical_removable_ram_and_unc_paths_are_excluded(self):
        rows = [partition(letter + ':\\', 'NTFS', letter) for letter in 'CDEFR']
        rows.append(partition('\\\\server\\share', 'NTFS', 'network'))
        types = {'C:\\': 3, 'D:\\': 2, 'E:\\': 4, 'F:\\': 5, 'R:\\': 6}
        with mock.patch.object(guard, '_windows_drive_type', side_effect=types.__getitem__):
            report, usage = self.run_guard(rows, {'C:\\': 3 * guard.GIB}, platform='win32')
        usage.assert_called_once_with('C:\\')
        self.assertEqual(report['excluded_count'], 5)
        self.assertTrue(report['complete'])

    def test_windows_unknown_drive_type_does_not_probe_possible_network_path(self):
        with mock.patch.object(guard, '_windows_drive_type', return_value=0):
            report, usage = self.run_guard([partition('Z:\\', 'NTFS', 'Z:')], {}, platform='win32')
        usage.assert_not_called()
        self.assertFalse(report['complete'])
        self.assertIsNone(report['volumes'][0]['below_target'])

    def test_discovery_failure_is_an_explicit_warning(self):
        with mock.patch.object(guard.sys, 'platform', 'linux'):
            report = guard.check_drive_buffers({}, partition_reader=mock.Mock(side_effect=OSError('fixture')),
                                               usage_reader=mock.Mock(side_effect=AssertionError('No inventory')))
        self.assertFalse(report['complete'])
        self.assertEqual(report['status'], 'needs_review')
        self.assertEqual(report['volumes'], [])
        self.assertTrue(report['warnings'])

    def test_empty_or_excluded_inventory_is_unverified_instead_of_healthy(self):
        for rows in ([], [partition('/tmp', 'tmpfs')]):
            with self.subTest(rows=rows):
                report, usage = self.run_guard(rows, {})
            self.assertFalse(report['complete'])
            self.assertEqual(report['status'], 'needs_review')
            usage.assert_not_called()

    def test_invalid_mountpoint_is_an_inventory_finding(self):
        report, usage = self.run_guard([partition('/ok'), partition('bad\nmount')], {'/ok': 3 * guard.GIB})
        self.assertFalse(report['complete'])
        self.assertEqual(len(report['volumes']), 1)
        usage.assert_called_once_with('/ok')

    @unittest.skipUnless(sys.platform == 'win32', 'Windows PowerShell math-only regression.')
    def test_standalone_script_handles_large_free_space_without_int32_overflow(self):
        path = str(Path(__file__).resolve().parents[1] / 'scripts/Check-OptimusDriveBuffers.ps1').replace("'", "''")
        # Parse the script, then run only its shortfall assignment with fixture
        # values. No inventory command or script body is executed.
        source = r"""$tokens=$null;$errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile('SCRIPT_PATH',[ref]$tokens,[ref]$errors)
if(@($errors).Count){throw 'Script parse failed.'}
$nodes=@($ast.FindAll({param($node) $node -is [System.Management.Automation.Language.AssignmentStatementAst] -and $node.Extent.Text -match '^\[long\]\$guardShortfall\s*='},$true))
if($nodes.Count -ne 1){throw 'Shortfall assignment is unavailable.'}
$mathOnly=[scriptblock]::Create($nodes[0].Extent.Text)
$values=@(0,1GB,2GB,100GB,[long]::MaxValue)
$observed=@(foreach($free in $values){$guardTargetBytes=[long]2GB;$guardFree=[long]$free;. $mathOnly;[long]$guardShortfall})
$observed | ConvertTo-Json -Compress
""".replace('SCRIPT_PATH', path)
        executable = str(Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe')
        result = subprocess.run([executable, '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', source],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=10, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [2 * guard.GIB, guard.GIB, 0, 0, 0])


if __name__ == '__main__':
    unittest.main()
