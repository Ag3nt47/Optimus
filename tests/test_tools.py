"""Mock-only secure-wipe gating tests. Cipher is never launched."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

module_path = Path(__file__).parents[1] / 'src' / 'optimus' / 'tools.py'
spec = importlib.util.spec_from_file_location('optimus_tools_mock_test', module_path)
tools = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tools)

class WipeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='optimus-wipe-test-')
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(tools._JOBS.clear)

    def media(self, media_type='HDD', bus='SATA', matches=None):
        return [{'DriveLetter': 'D', 'DiskUniqueId': 'device-123', 'BusType': bus,
                 'Matches': matches if matches is not None else [{'UniqueId': 'device-123', 'MediaType': media_type}]}]

    def probe(self, payload):
        result = types.SimpleNamespace(returncode=0, stdout=json.dumps(payload))
        with patch.object(tools, '_system_executable', return_value=Path('fixture-powershell.exe')), patch.object(tools.subprocess, 'run', return_value=result):
            return tools._physical_hdd('D')

    def test_only_positive_unique_hdd_mapping_is_eligible(self):
        self.assertTrue(self.probe(self.media())[0])
        for payload in (self.media('SSD'), self.media('Unspecified'), self.media(bus='RAID'), self.media(matches=[]), self.media()+self.media(), self.media(matches=[{'UniqueId':'different','MediaType':'HDD'}])):
            with self.subTest(payload=payload): self.assertFalse(self.probe(payload)[0])

    def test_confirmation_is_required_before_probe_or_launch(self):
        with patch.object(tools, 'free_space_wipe_status') as status, patch.object(tools.subprocess, 'Popen') as popen:
            with self.assertRaises(tools.FreeSpaceWipeUnavailable): tools.launch_free_space_wipe('fixture', confirmed=False)
            status.assert_not_called(); popen.assert_not_called()

    def test_unknown_or_ssd_media_never_launches_cipher(self):
        with patch.object(tools, 'free_space_wipe_status', return_value={'available':False,'reason':'Unverified media'}), patch.object(tools.subprocess, 'Popen') as popen:
            with self.assertRaises(tools.FreeSpaceWipeUnavailable): tools.launch_free_space_wipe('fixture', confirmed=True)
            popen.assert_not_called()

    def test_only_builtin_cipher_is_launched_after_revalidation(self):
        selected=Path(self.temp.name)/'selected'; selected.mkdir()
        process=types.SimpleNamespace(pid=99999, poll=lambda:None)
        status={'available':True,'drive':'D:','note':'Whole-volume free-space overwrite'}
        with patch.dict(os.environ, {'LOCALAPPDATA':self.temp.name}), patch.object(tools, 'free_space_wipe_status', return_value=status), patch.object(tools, '_validated_directory', return_value=(selected,'D')), patch.object(tools, '_system_executable', return_value=Path(r'C:\Windows\System32\cipher.exe')), patch.object(tools.subprocess,'Popen',return_value=process) as popen:
            result=tools.launch_free_space_wipe(selected, confirmed=True)
            command=popen.call_args.args[0]
            self.assertEqual(command,[r'C:\Windows\System32\cipher.exe','/w:'+str(selected)])
            self.assertNotIn('shell',popen.call_args.kwargs)
            self.assertTrue(result['started'])
            self.assertTrue(tools.free_space_wipe_job(result['job_id'])['running'])
            self.assertTrue(Path(result['log_path']).is_file())

    def test_volume_change_blocks_launch(self):
        status={'available':True,'drive':'D:','note':'Fixture'}
        with patch.object(tools,'free_space_wipe_status',return_value=status),patch.object(tools,'_validated_directory',return_value=(Path(self.temp.name),'E')),patch.object(tools.subprocess,'Popen') as popen:
            with self.assertRaises(tools.FreeSpaceWipeUnavailable):tools.launch_free_space_wipe('fixture',confirmed=True)
            popen.assert_not_called()

    def test_unowned_job_cannot_be_inspected_as_owned(self):
        with self.assertRaises(tools.FreeSpaceWipeUnavailable):tools.free_space_wipe_job('unknown')

    def test_injected_data_directory_owns_wipe_logs_without_creating_legacy_state(self):
        selected=Path(self.temp.name)/'selected'; selected.mkdir()
        moved=Path(self.temp.name)/'moved-data'
        spacer=Path(self.temp.name)/'spacer'; spacer.mkdir()
        alias=spacer/'..'/'moved-data'
        self.assertNotEqual(alias,moved)
        legacy=Path(self.temp.name)/'legacy-local'
        process=types.SimpleNamespace(pid=99998,poll=lambda:None)
        status={'available':True,'drive':'D:','note':'Fixture only; no wipe runs'}
        for injected in (moved,alias):
            with self.subTest(data_dir=injected):
                with patch.dict(os.environ,{'LOCALAPPDATA':str(legacy)}),patch.object(tools,'free_space_wipe_status',return_value=status),patch.object(tools,'_validated_directory',return_value=(selected,'D')),patch.object(tools,'_system_executable',return_value=Path(r'C:\Windows\System32\cipher.exe')),patch.object(tools.subprocess,'Popen',return_value=process) as popen:
                    result=tools.launch_free_space_wipe(selected,confirmed=True,data_dir=injected)
                popen.assert_called_once()
                # Canonical Windows paths can expand an 8.3 alias such as RUNNER~1.
                self.assertTrue(Path(result['log_path']).parent.samefile(moved/'wipe-logs'))
                self.assertTrue(Path(result['log_path']).is_file())
                self.assertFalse((legacy/'Optimus').exists())

    def test_default_wipe_logs_follow_installation_location(self):
        selected=Path(self.temp.name)/'selected'; selected.mkdir()
        moved=Path(self.temp.name)/'installed-data'
        process=types.SimpleNamespace(pid=99997,poll=lambda:None)
        status={'available':True,'drive':'D:','note':'Fixture only; no wipe runs'}
        with patch('optimus.filesystem.default_data_root',return_value=moved) as root,patch.object(tools,'free_space_wipe_status',return_value=status),patch.object(tools,'_validated_directory',return_value=(selected,'D')),patch.object(tools,'_system_executable',return_value=Path(r'C:\Windows\System32\cipher.exe')),patch.object(tools.subprocess,'Popen',return_value=process):
            result=tools.launch_free_space_wipe(selected,confirmed=True)
        root.assert_called_once_with()
        self.assertEqual(Path(result['log_path']).parent,moved/'wipe-logs')

if __name__=='__main__':unittest.main(verbosity=2)
