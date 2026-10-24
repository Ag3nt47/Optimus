from pathlib import Path
import csv
import io
import json
import subprocess
import tempfile
import time
import types
import sys
import unittest
from unittest.mock import patch

source = Path(__file__).resolve().parents[1] / 'src' / 'optimus' / 'maintenance.py'
sys.path.insert(0, str(source.parents[1]))
module = types.ModuleType('optimus_maintenance_isolated')
module.__package__ = 'optimus'
exec(compile(source.read_text(encoding='ascii'), str(source), 'exec'), module.__dict__)
Maintenance = module.Maintenance

class Bridge:
    def __init__(self, folder):
        self.folder = folder
        self.calls = []
        self.fail = False
        self.preview = {'targets': [{'id': 'appx-fixture', 'name': 'Microsoft.BingWeather', 'fingerprint': 'f' * 64, 'packageFullName': 'Fixture_Package_1'}], 'summary': 'Fixture only', 'recovery': 'Reinstall'}
    def request(self, operation, **arguments):
        self.calls.append((operation, arguments))
        if operation == 'maintenance_preview':
            return json.loads(json.dumps(self.preview))
        if operation == 'maintenance_apply':
            plans = list((Path(self.folder) / 'MaintenancePlans').glob('*.json'))
            assert any(json.loads(p.read_text())['state'] == 'applying' for p in plans), 'manifest must precede mutation'
            if self.fail:
                raise RuntimeError('fixture operation failure')
            return {'results': [{'id': 'appx-fixture', 'removed': True}], 'restartNeeded': False}
        return {'fixtureOperation': operation}

class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='optimus-maintenance-test-')
        self.bridge = Bridge(self.temp.name)
        self.tool = Maintenance(self.bridge, self.temp.name)
    def tearDown(self):
        self.temp.cleanup()
    def test_01_actions_and_preview(self):
        self.assertEqual(len(self.tool.debloat_actions()), 6)
        self.assertFalse((Path(self.temp.name) / 'MaintenancePlans').exists())
        plan = self.tool.debloat_preview('consumer-apps')
        self.assertFalse(plan['targets'][0]['selected'])
        self.assertTrue(plan['canApply'])
        self.assertEqual(self.bridge.calls[0][0], 'maintenance_preview')
    def test_02_unknown_and_provisioning_scope(self):
        with self.assertRaises(ValueError): self.tool.debloat_preview('unknown')
        with self.assertRaises(ValueError): self.tool.debloat_preview({'action': 'edge', 'includeProvisioned': True})
        self.assertEqual(self.bridge.calls, [])
    def test_03_confirmation_required(self):
        plan = self.tool.debloat_preview('consumer-apps')
        with self.assertRaises(ValueError): self.tool.debloat_apply({'planId': plan['planId'], 'selectedIds': ['appx-fixture']})
        self.assertEqual(len(self.bridge.calls), 1)
    def test_04_selection_guards(self):
        plan = self.tool.debloat_preview('consumer-apps')
        for selected in ([], ['unknown'], ['appx-fixture', 'appx-fixture']):
            with self.assertRaises(ValueError): self.tool.debloat_apply({'planId': plan['planId'], 'selectedIds': selected, 'confirmed': True})
        self.assertEqual(len(self.bridge.calls), 1)
    def test_05_saved_payload_and_single_use(self):
        plan = self.tool.debloat_preview('consumer-apps')
        result = self.tool.debloat_apply({'planId': plan['planId'], 'selectedIds': ['appx-fixture'], 'confirmed': True, 'action': 'edge', 'targets': [{'payload': 'malicious caller text'}]})
        passed = self.bridge.calls[-1][1]
        self.assertEqual(passed['action'], 'consumer-apps')
        self.assertEqual(passed['targets'][0]['packageFullName'], 'Fixture_Package_1')
        self.assertTrue(Path(result['historyFile']).is_file())
        with self.assertRaises(ValueError): self.tool.debloat_apply({'planId': plan['planId'], 'selectedIds': ['appx-fixture'], 'confirmed': True})
    def test_06_expiry(self):
        plan = self.tool.debloat_preview('consumer-apps')
        plan['createdEpoch'] = time.time() - 1900
        self.tool._write_json(self.tool.plan_dir / (plan['planId'] + '.json'), plan)
        with self.assertRaises(ValueError): self.tool.debloat_apply({'planId': plan['planId'], 'selectedIds': ['appx-fixture'], 'confirmed': True})
        self.assertEqual(len(self.bridge.calls), 1)
    def test_07_operation_failure_is_recorded(self):
        plan = self.tool.debloat_preview('consumer-apps'); self.bridge.fail = True
        with self.assertRaises(RuntimeError): self.tool.debloat_apply({'planId': plan['planId'], 'selectedIds': ['appx-fixture'], 'confirmed': True})
        saved = json.loads((self.tool.plan_dir / (plan['planId'] + '.json')).read_text())
        self.assertEqual(saved['state'], 'failed')
    def test_08_external_preview_error_blocks_apply(self):
        self.bridge.preview['externalPreview'] = {'exitCode': 1, 'text': 'fixture'}
        plan = self.tool.debloat_preview('desktop-edge-script')
        self.assertFalse(plan['canApply'])
        with self.assertRaises(ValueError): self.tool.debloat_apply({'planId': plan['planId'], 'selectedIds': ['appx-fixture'], 'confirmed': True})
    def test_09_readonly_registry_and_drivers(self):
        self.assertEqual(self.tool.registry_scan()['fixtureOperation'], 'registry_audit')
        self.assertEqual(self.tool.drivers_get()['fixtureOperation'], 'drivers_list')
        with self.assertRaises(ValueError): self.tool.driver_update_apply('bad-guid', 1, confirmed=True)
        with self.assertRaises(ValueError): self.tool.driver_update_apply('00000000-0000-0000-0000-000000000001', 1)
    def test_10_software_updates_are_exact_and_confirmed(self):
        header = f"{'Name':18}{'Id':24}{'Version':14}{'Available':14}Source"
        row = f"{'Example':18}{'Vendor.Example':24}{'1.0':14}{'2.0':14}winget"
        completed = subprocess.CompletedProcess([], 0, header + '\n' + '-'*80 + '\n' + row + '\n', '')
        with patch.object(Maintenance, '_winget', return_value='winget-fixture.exe'), patch.object(Maintenance, '_run', side_effect=[completed,completed,subprocess.CompletedProcess([],0,'There are no pins configured.',''),subprocess.CompletedProcess([],0,'Fixture updated','')]) as run:
            scan = self.tool.updates_scan()
            self.assertEqual(scan['updates'][0]['id'], 'Vendor.Example')
            with self.assertRaises(ValueError): self.tool.updates_apply('Vendor.Example')
            with self.assertRaises(ValueError): self.tool.updates_apply('Other.Package', confirmed=True)
            self.tool.updates_apply('Vendor.Example', confirmed=True)
            self.assertEqual(run.call_args.args[0], ['winget-fixture.exe', 'upgrade', '--id', 'Vendor.Example', '--exact', '--source', 'winget', '--version', '2.0', '--disable-interactivity'])
    def test_11_task_scope_and_no_implicit_creation(self):
        with patch.object(Maintenance, '_run') as run:
            with self.assertRaises(ValueError): self.tool.schedule_create()
            with self.assertRaises(ValueError): self.tool.schedule_delete('Microsoft-Windows-Security', confirmed=True)
            run.assert_not_called()
            with self.assertRaises(RuntimeError): self.tool.schedule_create(12, 0, confirmed=True)
            run.assert_not_called()
    def test_12_task_list_limits_to_optimus(self):
        text = '"TaskName","Next Run Time","Status"\n"\\Microsoft\\Task","Tomorrow","Ready"\n"\\Optimus-Audit-fixture","Tomorrow","Ready"\n'
        with patch.object(Maintenance, '_run', return_value=subprocess.CompletedProcess([], 0, text, '')):
            rows = self.tool.schedules_list()
            self.assertEqual([r['name'] for r in rows], ['\\Optimus-Audit-fixture'])

    def test_13_removal_progress_is_bound_to_exact_saved_selection(self):
        plan = self.tool.debloat_preview('consumer-apps')
        events = []
        original = self.bridge.request
        def request(operation, **arguments):
            if operation == 'maintenance_apply':
                observer = arguments['progress_callback']
                self.assertIs(arguments['reportProgress'], True)
                for key, percent in [('debloat:consumer-apps:another-app', 90),
                                     ('debloat:consumer-apps:appx-fixture', None),
                                     ('debloat:consumer-apps:appx-fixture', 100)]:
                    observer({'key': key, 'label': 'Untrusted label', 'phase': 'uninstall', 'percent': percent})
            return original(operation, **arguments)
        self.bridge.request = request
        self.tool.debloat_apply({'planId': plan['planId'], 'selectedIds': ['appx-fixture'], 'confirmed': True},
                               progress_callback=events.append)
        self.assertEqual(len(events), 2)
        self.assertEqual([event['item_progress']['percent'] for event in events], [None, 100.0])
        self.assertTrue(all(event['item_progress']['label'] == 'Microsoft.BingWeather' for event in events))
        self.assertTrue(all(event['current'] == 0 and event['total'] == 1 and event['id'] == 'appx-fixture' for event in events))

    def test_14_invalid_removal_observer_is_rejected_before_persisting_or_bridge(self):
        plan = self.tool.debloat_preview('consumer-apps')
        with self.assertRaises(ValueError):
            self.tool.debloat_apply({'planId': plan['planId'], 'selectedIds': ['appx-fixture'], 'confirmed': True},
                                   progress_callback=42)
        saved = json.loads((self.tool.plan_dir / (plan['planId'] + '.json')).read_text())
        self.assertEqual(saved['state'], 'preview')
        self.assertEqual(len(self.bridge.calls), 1)

if __name__ == '__main__': unittest.main(verbosity=2)
