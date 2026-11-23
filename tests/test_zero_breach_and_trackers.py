import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from optimus import zero_breach as zb  # noqa: E402
from optimus import audio_trackers as at  # noqa: E402

TOOL = b'print("zero breach fixture")\n'
SHA = hashlib.sha256(TOOL).hexdigest()


def release(tag='v3.0.3', digest=SHA, size=len(TOOL), with_sums=True):
    base = zb.DOWNLOAD_PREFIX + tag + '/'
    assets = [{'name': zb.ASSET, 'size': size, 'digest': 'sha256:' + digest, 'browser_download_url': base + zb.ASSET}]
    if with_sums:
        assets.append({'name': zb.SUMS, 'size': 100, 'digest': 'sha256:' + '0' * 64, 'browser_download_url': base + zb.SUMS})
    return {'tag_name': tag, 'draft': False, 'prerelease': False, 'assets': assets}


class Fetch:
    def __init__(self, rel, tool=TOOL, sums_hash=SHA):
        self.rel, self.tool, self.sums_hash, self.urls = rel, tool, sums_hash, []

    def __call__(self, url, limit):
        self.urls.append(url)
        if url == zb.API:
            return json.dumps(self.rel).encode()
        if url.endswith('/' + zb.SUMS):
            return f'{self.sums_hash}  {zb.ASSET}\n'.encode()
        return self.tool


class ZeroBreachTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def make(self, fetch, **kwargs):
        return zb.ZeroBreach(data_dir=self.root, fetch=fetch, seed=self.root / 'no-seed.py', **kwargs)

    def test_update_installs_verified_copy_then_reports_up_to_date(self):
        tool = self.make(Fetch(release()))
        result = tool.update()
        self.assertEqual(result['status'], 'updated')
        self.assertEqual((self.root / 'zero-breach' / zb.ASSET).read_bytes(), TOOL)
        self.assertTrue(tool.installed()['verified'])
        self.assertEqual(tool.update()['status'], 'up_to_date')

    def test_hash_mismatch_never_replaces(self):
        for fetch in (Fetch(release(), tool=b'tampered'), Fetch(release(), sums_hash='1' * 64)):
            result = self.make(fetch).update()
            self.assertEqual(result['status'], 'update_failed')
            self.assertFalse((self.root / 'zero-breach' / zb.ASSET).exists())
        self.assertEqual(list((self.root / 'zero-breach').glob('*.tmp')), [])

    def test_rejects_foreign_urls_prereleases_and_missing_sums(self):
        bad_url = release()
        bad_url['assets'][0]['browser_download_url'] = 'https://evil.example/zero-breach-toolkit.py'
        pre = release()
        pre['prerelease'] = True
        for rel in (bad_url, pre, release(with_sums=False)):
            self.assertEqual(self.make(Fetch(rel)).update()['status'], 'check_failed')

    def test_default_fetch_refuses_other_hosts(self):
        with self.assertRaises(ValueError):
            zb._default_fetch('https://example.com/x.py', 10)

    def test_consilium_seed_is_copied_not_modified(self):
        seed = self.root / 'seed.py'
        seed.write_bytes(TOOL)
        fetch = Fetch(release())
        tool = zb.ZeroBreach(data_dir=self.root, fetch=fetch, seed=seed)
        self.assertEqual(tool.update()['source'], 'consilium-seed')
        self.assertFalse(any(url.endswith('/' + zb.ASSET) for url in fetch.urls))
        self.assertEqual(seed.read_bytes(), TOOL)

    def test_scan_runs_quick_read_only_scan(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 2)
        tool = self.make(Fetch(release()), runner=runner, python=lambda: 'C:/Python/python.exe')
        self.assertEqual(tool.scan()['status'], 'not_installed')
        tool.update()
        result = tool.scan()
        self.assertEqual(result['status'], 'matches_found')
        self.assertEqual(calls[0][:5], ['C:/Python/python.exe', '-X', 'utf8', str(tool.tool), '--quick'])
        self.assertIn('--no-menu', calls[0])
        self.assertEqual(tool.state()['last_scan']['status'], 'matches_found')

    def test_scan_needs_python(self):
        tool = self.make(Fetch(release()), python=lambda: None)
        tool.update()
        self.assertEqual(tool.scan()['status'], 'python_missing')


class FakeReg:
    HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE = 'HKCU', 'HKLM'
    KEY_READ, KEY_SET_VALUE, REG_SZ = 1, 2, 1

    def __init__(self):
        self.values = {}

    class _Key:
        def __init__(self, path):
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def OpenKey(self, hive, key, *args):
        return self._Key((hive, key))

    CreateKey = OpenKey

    def QueryValueEx(self, key, name):
        if (key.path, name) not in self.values:
            raise OSError('missing')
        return self.values[(key.path, name)], 1

    def SetValueEx(self, key, name, reserved, kind, value):
        self.values[(key.path, name)] = value

    def DeleteValue(self, key, name):
        self.values.pop((key.path, name), None)


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.drop = self.root / 'AppData' / 'Roaming' / 'adsdk'
        self.drop.mkdir(parents=True)
        self.bad = self.drop / 'beacon.exe'
        self.bad.write_bytes(b'MZ fixture')
        self.good = self.root / 'ProgramFiles' / 'Discord.exe'
        self.good.parent.mkdir()
        self.good.write_bytes(b'MZ fixture')
        self.env = dict(os.environ)
        os.environ['APPDATA'] = str(self.root / 'AppData' / 'Roaming')
        os.environ['ProgramFiles'] = str(self.root / 'ProgramFiles')
        # The fixture lives under the real TEMP; point TEMP elsewhere so only APPDATA is a drop folder.
        os.environ['TEMP'] = os.environ['TMP'] = str(self.root / 'tmp')
        self.reg = FakeReg()
        self.rules = []
        self.run_entry = {'hive': 'HKCU', 'key': at._RUN_KEYS[0], 'name': 'AdBeacon', 'command': f'"{self.bad}" /bg', 'type': 1}
        self.reg.values[(('HKCU', at._RUN_KEYS[0]), 'AdBeacon')] = self.run_entry['command']

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)
        self.temp.cleanup()

    def tool(self):
        usage = [
            {'hive': 'HKCU', 'key': at._CONSENT + r'\NonPackaged\x', 'packaged': False, 'app': str(self.bad),
             'last_used': '2026-11-23T10:00:00Z', 'in_use': True, 'consent': None},
            {'hive': 'HKCU', 'key': at._CONSENT + r'\NonPackaged\y', 'packaged': False, 'app': str(self.good),
             'last_used': '2026-11-22T10:00:00Z', 'in_use': False, 'consent': None},
            {'hive': 'HKCU', 'key': at._CONSENT + r'\Store', 'packaged': True, 'app': 'Microsoft.Store_8wekyb3d8bbwe',
             'last_used': '2026-11-21T10:00:00Z', 'in_use': False, 'consent': 'Allow'},
        ]
        return at.AudioTrackers(
            data_dir=self.root / 'data', usage=lambda: usage, run_entries=lambda: [self.run_entry],
            signatures=lambda paths: {at._norm(str(self.bad)): False, at._norm(str(self.good)): True},
            firewall_block=lambda path, name: self.rules.append(name),
            firewall_unblock=lambda name: self.rules.remove(name),
            process_iter=lambda attrs: [], registry=self.reg)

    def test_classification(self):
        scan = self.tool().scan()
        verdicts = {Path(row['app']).name: row['verdict'] for row in scan['findings']}
        self.assertEqual(verdicts['beacon.exe'], 'block')
        self.assertEqual(verdicts['Discord.exe'], 'ok')
        self.assertEqual(scan['block_count'], 1)
        self.assertEqual(at.classify(r'C:\Windows\System32\rundll32.exe', signed=True, exists=True)[0], 'review')
        self.assertEqual(at.classify(r'C:\gone.exe', signed=None, exists=False)[0], 'ok')

    def test_auto_block_then_restore(self):
        tool = self.tool()
        result = tool.scan_and_block()
        item = result['blocked']['items'][0]
        self.assertEqual(item['status'], 'blocked')
        self.assertFalse(self.bad.exists())
        self.assertTrue(Path(item['steps']['quarantined_to']).exists())
        self.assertEqual(len(self.rules), 1)
        self.assertEqual(self.reg.values[(('HKCU', at._CONSENT + r'\NonPackaged\x'), 'Value')], 'Deny')
        self.assertNotIn((('HKCU', at._RUN_KEYS[0]), 'AdBeacon'), self.reg.values)
        self.assertTrue(self.good.exists())
        self.assertEqual(len(tool.history()), 1)
        restored = tool.restore(result['blocked']['receipt_id'])
        self.assertEqual(restored['status'], 'restored')
        self.assertTrue(self.bad.exists())
        self.assertEqual(self.rules, [])
        self.assertNotIn((('HKCU', at._CONSENT + r'\NonPackaged\x'), 'Value'), self.reg.values)
        self.assertEqual(self.reg.values[(('HKCU', at._RUN_KEYS[0]), 'AdBeacon')], self.run_entry['command'])

    def test_manual_selection_never_touches_store_apps_and_plans_expire(self):
        tool = self.tool()
        scan = tool.scan()
        store = [row['selection_id'] for row in scan['findings'] if row['packaged']]
        result = tool.block(scan['plan_id'], store)
        self.assertEqual(result['items'], [])
        with self.assertRaises(ValueError):
            tool.block(scan['plan_id'], store)

    def test_clean_machine_changes_nothing(self):
        tool = at.AudioTrackers(data_dir=self.root / 'data', usage=lambda: [], run_entries=lambda: [],
                                signatures=lambda paths: {}, firewall_block=lambda *a: self.fail('no rule'),
                                registry=self.reg)
        self.assertIsNone(tool.scan_and_block()['blocked'])
        self.assertEqual(tool.history(), [])


if __name__ == '__main__':
    unittest.main()
