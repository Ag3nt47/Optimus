"""Health workflow integration uses fake providers and isolated state only."""
import copy
import json
from types import SimpleNamespace
import threading
import time
from unittest.mock import Mock

import pytest

from optimus import hardware_health as health_module
from optimus.hardware_health import HEALTH_STAGE, HardwareHealth, collect_hardware_health, normalize_hardware_health
from optimus.hardware_health_history import HardwareHealthHistory
from optimus.native_workflow import NativeWorkflow
from optimus.parallel_updates import ProgressMux
from optimus.post_run_fixes import apply_available_fixes
from optimus.workflow import DEFAULTS, Preferences, Workflow


def configuration(**changes):
    result = copy.deepcopy(DEFAULTS)
    for key in result:
        if key.endswith('_enabled') and key != 'hardware_health_enabled':
            result[key] = False
    result['applications_review_enabled'] = True
    result.update(changes)
    return result


def verification(report, **options):
    return {'schema': 1, 'read_only': True, 'run_id': report['id'], 'status': 'not_applicable',
        'counts': {'confirmed': 0, 'needs_review': 0, 'unverified': 0, 'not_applicable': len(report['stages'])},
        'coverage_complete': True, 'failed_items': 0,
        'stages': [{'title': row['title'], 'status': 'not_applicable', 'checks': []} for row in report['stages']]}


def observation(system='win32', **changes):
    raw = {'schema': 1, 'read_only': True, 'platform': 'Windows' if system == 'win32' else system,
        'captured_at': time.time(), 'status': 'completed', 'errors': [],
        'storage': [{'name': 'Fixture physical drive', 'health_status': 0, 'media_type': 4,
            'temperature_c': 31, 'device_id': 'fixture-0', 'identity_hash': 'a' * 64,
            'identity_quality': 'stable', 'identity_source': 'fixture-native-physical-device'}],
        'batteries': [{'name': 'Fixture battery', 'full_charge_capacity': 800, 'design_capacity': 1000,
            'capacity_units': 'mWh', 'identity_hash': 'b' * 64, 'identity_quality': 'stable',
            'identity_source': 'fixture-native-battery-identity'}]}
    raw.update(changes)
    return normalize_hardware_health(raw)


@pytest.fixture(autouse=True)
def forbid_native_queries(monkeypatch):
    monkeypatch.setattr('optimus.runtime_priority.lower_worker_priority', lambda: lambda: None)
    monkeypatch.setattr('optimus.bridge.PowerShellBridge.request',
        Mock(side_effect=AssertionError('A workflow fixture must never query a native PowerShell bridge.')))
    monkeypatch.setattr(health_module, 'collect_hardware_health',
        Mock(side_effect=AssertionError('Each workflow fixture must supply its fake health provider.')))


def backend(tmp_path, monkeypatch, system='win32', *, settings=None, provider=None):
    saved = settings or configuration()
    collector = Mock(side_effect=lambda *args, **options: observation(system)) if provider is None else provider
    monkeypatch.setattr(health_module, 'collect_hardware_health', collector)
    tool = SimpleNamespace(data_dir=tmp_path, system=system, bridge=SimpleNamespace(_process=None),
        preference_store=SimpleNamespace(validate=Preferences.validate),
        preferences=Mock(return_value=copy.deepcopy(saved)),
        audit=Mock(return_value={'Processes': [], 'System': {}, 'InventoryErrors': []}),
        installed_apps=Mock(return_value=[]), verify_optimization=Mock(side_effect=verification),
        security_status=Mock(return_value={'status': 'completed', 'recommendations': [], 'reboot_required': False}))
    service = HardwareHealth(tool)
    tool.hardware_health = Mock(side_effect=service.scan)
    tool.hardware_health_cached = service.cached
    tool.hardware_health_history = lambda: HardwareHealthHistory(tmp_path).series()
    tool.workflow = Workflow(tool) if system == 'win32' else NativeWorkflow(tool)
    return tool, collector, service


@pytest.mark.parametrize('system', ['win32', 'linux', 'darwin'])
def test_first_and_every_main_run_collect_once_and_keep_the_device_baseline(tmp_path, monkeypatch, system):
    choices = configuration()
    tool, collector, service = backend(tmp_path, monkeypatch, system, settings=choices)
    for index in range(2):
        events = []
        report = tool.workflow.run(choices, events.append)
        assert report['status'] == 'completed'
        assert collector.call_count == tool.hardware_health.call_count == index + 1
        stages = [stage for stage in report['stages'] if stage['title'] == HEALTH_STAGE]
        assert len(stages) == 1 and stages[0]['status'] == 'completed'
        assert report['hardware_health']['read_only'] is True
        assert events[-1]['hardware_health']['scan_id'] == report['hardware_health']['scan_id']
        assert service.cached()['scan_id'] == report['hardware_health']['scan_id']
        assert any(stage['title'].startswith('Review installed') for stage in report['stages'])
        recorded = json.loads((tmp_path / 'hardware-health/history.json').read_text())
        assert len(recorded['scans']) == index + 1
        if index == 0:
            first = copy.deepcopy(recorded['scans'][0])
        else:
            assert recorded['scans'][0] == first
    assert not tool.workflow.active
    assert not (tmp_path / 'preferences.json').exists()


@pytest.mark.parametrize('system', ['win32', 'linux', 'darwin'])
def test_explicit_health_opt_out_starts_no_provider_or_history_writer(tmp_path, monkeypatch, system):
    choices = configuration(hardware_health_enabled=False)
    tool, collector, service = backend(tmp_path, monkeypatch, system, settings=choices)
    report = tool.workflow.run(choices, lambda event: None)
    collector.assert_not_called()
    tool.hardware_health.assert_not_called()
    assert not any(stage['title'] == HEALTH_STAGE for stage in report['stages'])
    assert 'hardware_health' not in report and service.cached() is None
    assert not (tmp_path / 'hardware-health').exists()


@pytest.mark.parametrize('system', ['win32', 'linux', 'darwin'])
def test_old_saved_settings_gain_only_the_read_only_default_health_check(tmp_path, monkeypatch, system):
    choices = configuration()
    choices.pop('hardware_health_enabled')
    tool, collector, _ = backend(tmp_path, monkeypatch, system, settings=choices)
    report = tool.workflow.run(choices, lambda event: None)
    assert report['settings']['hardware_health_enabled'] is True
    collector.assert_called_once()
    assert not (tmp_path / 'preferences.json').exists()


@pytest.mark.parametrize('system', ['win32', 'linux', 'darwin'])
@pytest.mark.parametrize('failure', ['provider_unavailable', 'escaped_exception'])
def test_health_failure_does_not_stop_later_review_final_report_or_verification(tmp_path, monkeypatch, system, failure):
    provider = (Mock(return_value=observation(system, status='unavailable', storage=[], batteries=[],
                 errors=['Fixture provider unavailable.'])) if failure == 'provider_unavailable'
                else Mock(side_effect=RuntimeError('Fixture provider exception.')))
    tool, collector, _ = backend(tmp_path, monkeypatch, system, provider=provider)
    events = []
    report = tool.workflow.run(configuration(), events.append)
    collector.assert_called_once()
    assert any(stage['title'].startswith('Review installed') and stage['status'] == 'completed' for stage in report['stages'])
    assert tool.audit.call_count == 2 and tool.verify_optimization.call_count == 1
    assert events[-1]['percentage'] == 100 and report['status'] in ('completed', 'completed_with_errors')
    health_stage = next(stage for stage in report['stages'] if stage['title'] == HEALTH_STAGE)
    assert health_stage['status'] == ('failed' if failure == 'escaped_exception' else 'completed')
    if failure == 'provider_unavailable':
        assert report['hardware_health']['status'] == 'unavailable'
        assert not report['hardware_health']['drives'] and not report['hardware_health']['batteries']


@pytest.mark.parametrize('system', ['win32', 'linux', 'darwin'])
def test_preexisting_cancellation_does_not_collect_or_record_health(tmp_path, monkeypatch, system):
    tool, collector, _ = backend(tmp_path, monkeypatch, system)
    cancelled = threading.Event()
    cancelled.set()
    report = tool.workflow.run(configuration(), lambda event: None, cancelled)
    assert report['status'] == 'cancelled'
    collector.assert_not_called()
    assert not (tmp_path / 'hardware-health').exists()


@pytest.mark.parametrize('system', ['win32', 'linux', 'darwin'])
def test_cached_details_and_followup_fix_batch_do_not_recapture_or_change_history(tmp_path, monkeypatch, system):
    choices = configuration()
    tool, collector, service = backend(tmp_path, monkeypatch, system, settings=choices)
    source = tool.workflow.run(choices, lambda event: None)
    history_path = tmp_path / 'hardware-health/history.json'
    before = history_path.read_bytes()
    first = service.cached()
    first['drives'][0]['name'] = 'Changing a returned copy must not change the cache.'
    assert service.cached()['drives'][0]['name'] == 'Fixture physical drive'
    tool.hardware_health_history()
    followup = apply_available_fixes(tool, source, choices)
    assert followup['status'] == 'completed'
    collector.assert_called_once()
    assert service.cached()['scan_id'] == source['hardware_health']['scan_id']
    assert history_path.read_bytes() == before


@pytest.mark.parametrize('system', ['win32', 'linux', 'darwin'])
def test_legacy_fixture_backend_without_a_health_method_never_falls_back_to_the_host(tmp_path, monkeypatch, system):
    tool, collector, _ = backend(tmp_path, monkeypatch, system)
    del tool.hardware_health
    report = tool.workflow.run(configuration(), lambda event: None)
    assert report['status'] == 'completed'
    collector.assert_not_called()
    assert not any(stage['title'] == HEALTH_STAGE for stage in report['stages'])


def test_progress_mux_carries_a_detached_health_snapshot_through_later_background_updates():
    events = []
    mux = ProgressMux(events.append)
    report = observation()
    mux.update_hardware_health(report)
    report['drives'][0]['temperature_c'] = 99
    mux({'stage': 'Later maintenance', 'percentage': 40, 'description': 'A later fixture stage.'})
    events[-1]['hardware_health']['drives'][0]['temperature_c'] = 1
    mux.background('running', {'description': 'Fixture app update', 'current': 0, 'total': 1})
    assert events[-1]['hardware_health']['drives'][0]['temperature_c'] == 31
    assert events[-1]['stage'] == 'Later maintenance'
    assert events[-1]['jobs'][0]['id'] == 'software_updates'


def test_windows_collection_forwards_the_current_worker_cancellation_token_to_its_fake_bridge(monkeypatch):
    monkeypatch.setattr(health_module.platform, 'system', lambda: 'Windows')
    cancelled = threading.Event()
    bridge = SimpleNamespace(request=Mock(return_value={
        'schema': 1, 'read_only': True, 'platform': 'Windows', 'status': 'completed',
        'captured_at': time.time(), 'storage': [], 'batteries': [], 'errors': []}))
    result = collect_hardware_health(SimpleNamespace(system='win32', bridge=bridge), cancel_event=cancelled)
    bridge.request.assert_called_once_with('hardware_health', timeout=45, cancel_event=cancelled)
    assert result['status'] == 'completed'


def test_a_battery_numeric_status_cannot_be_interpreted_as_the_physical_drive_health_enum():
    report = normalize_hardware_health({'schema': 1, 'read_only': True, 'status': 'completed',
        'storage': [{'name': 'Fixture drive', 'health_status': 0}],
        'batteries': [{'name': 'Fixture battery', 'health_status': 0}], 'errors': []})
    assert report['drives'][0]['provider_condition_code'] == 0
    assert report['batteries'][0]['condition'] == 'Unknown'
    assert report['batteries'][0]['provider_condition_code'] is None


def test_oversized_numeric_sensor_value_stays_unknown_without_overflowing_the_normalizer():
    report = normalize_hardware_health({'schema': 1, 'read_only': True, 'status': 'completed',
        'storage': [{'name': 'Fixture drive', 'wear_percent_used': 10**400}],
        'batteries': [], 'errors': []})
    assert report['drives'][0]['wear_percent_used'] is None
