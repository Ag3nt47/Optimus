"""Speed integration uses fixture services; no host performance setting changes."""
import copy
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus.automation import CURRENT_SETUP_VERSION, RunLease
from optimus.backend import Backend
from optimus.native_settings import NativePreferences, full_native_settings
from optimus.native_workflow import NativeWorkflow, verify_native_outcomes
from optimus.speed_verification import normalized_speed_checks
from optimus.verification import OutcomeVerification
from optimus.workflow import DEFAULTS, Preferences, Workflow, full_optimization_settings


def settings(**changes):
    return dict(copy.deepcopy(DEFAULTS), setup_version=CURRENT_SETUP_VERSION, **changes)


def readback(state='confirmed'):
    return {'status': state, 'checks': [{'target': 'Fixture AC power preference', 'status': state,
                                        'reason': 'Fresh fixture configuration readback.'}],
            'summary': 'Configuration evidence only; no speed benchmark.'}


def test_old_and_disabled_choices_are_preserved_but_full_explicitly_enables(tmp_path):
    assert not Preferences.validate({'optimization_mode': 'full'})['speed_enhancements_enabled']
    assert full_optimization_settings()['speed_enhancements_enabled']
    assert full_native_settings()['speed_enhancements_enabled']
    assert not Preferences.safe_defaults()['speed_enhancements_enabled']
    store = Preferences(tmp_path)
    store.save(dict(full_optimization_settings(), speed_enhancements_enabled=False))
    assert not store.load()['speed_enhancements_enabled']
    with pytest.raises(ValueError):
        Preferences.validate({'speed_enhancements_enabled': 'yes'})


def test_standalone_wrapper_serializes_with_workflow_and_releases_after_error(tmp_path):
    backend = Backend.__new__(Backend)
    backend.data_dir = tmp_path
    backend.preference_store = Preferences(tmp_path)
    backend.workflow = SimpleNamespace(lock=threading.Lock(), active=False)
    backend.preferences = Mock(return_value=settings(speed_enhancements_enabled=True))
    service = Mock()
    backend._speed_service = service
    def apply(value, **kwargs):
        assert value['speed_enhancements_enabled'] is True
        assert backend.workflow.active
        with pytest.raises(RuntimeError):
            RunLease(tmp_path).acquire()
        raise OSError('Fixture write refusal')
    service.apply.side_effect = apply
    with pytest.raises(OSError, match='Fixture write refusal'):
        backend.apply_speed_enhancements()
    assert not backend.workflow.active and not backend.workflow.lock.locked()
    with RunLease(tmp_path):
        pass
    backend.workflow.lock.acquire()
    with pytest.raises(RuntimeError, match='current optimization'):
        backend.restore_speed_enhancements('a' * 32)
    backend.workflow.lock.release()
    service.restore.assert_not_called()


@pytest.mark.parametrize('enabled', [False, True])
def test_windows_workflow_applies_enabled_stage_once_before_final_readback(tmp_path, enabled):
    from test_workflow import MockBackend
    backend = MockBackend(tmp_path)
    backend._apply_speed_enhancements_stage = Mock(return_value={'items': [], 'failed_items': 0})
    result = Workflow(backend).run(settings(speed_enhancements_enabled=enabled,
        cleanup_enabled=False, browser_cache_enabled=False), lambda event: None)
    assert backend._apply_speed_enhancements_stage.call_count == int(enabled)
    titles = [item['title'] for item in result['stages']]
    assert ('Apply supported speed enhancements' in titles) is enabled
    if enabled:
        assert titles.index('Apply supported speed enhancements') < titles.index('Capture final resource report')
        assert titles[-1] == 'Verify optimization outcomes'


def test_native_workflow_applies_without_a_standalone_nested_global_lease(tmp_path):
    backend = SimpleNamespace(data_dir=tmp_path, system='linux',
        preference_store=SimpleNamespace(validate=NativePreferences.validate),
        audit=Mock(return_value={'Processes': [], 'System': {}}),
        verify_optimization=Mock(return_value={'read_only': True, 'failed_items': 0}),
        _apply_speed_enhancements_stage=Mock(return_value={'items': [], 'failed_items': 0}))
    result = NativeWorkflow(backend).run(settings(speed_enhancements_enabled=True,
        cleanup_enabled=False, browser_cache_enabled=False), lambda event: None)
    backend._apply_speed_enhancements_stage.assert_called_once()
    assert result['status'] == 'completed'
    assert result['stages'][1]['title'] == 'Apply supported speed enhancements'


@pytest.mark.parametrize('state', ['confirmed', 'needs_review', 'unverified', 'not_applicable'])
def test_both_final_verifiers_request_fresh_speed_readback(tmp_path, state):
    original = {'id': 'a' * 32, 'stages': [{'title': 'Apply supported speed enhancements',
                 'status': 'completed', 'result': {'id': 'b' * 32, 'items': []}}]}
    reader = Mock(return_value=readback(state))
    windows = OutcomeVerification(tmp_path, speed_reader=reader).verify(original)
    assert windows['counts'][state] == 1
    reader.assert_called_once()
    assert reader.call_args.args[0] is original['stages'][0]['result']
    backend = SimpleNamespace(verify_speed_enhancements=Mock(return_value=readback(state)))
    native = verify_native_outcomes(backend, original)
    assert native['counts'][state] == 1
    backend.verify_speed_enhancements.assert_called_once()


@pytest.mark.parametrize('invalid', [None, {}, {'status': 'confirmed', 'checks': []},
    {'status': 'confirmed', 'checks': [{'target': 'Fixture', 'status': 'unverified', 'reason': 'Unknown'}]},
    {'status': 'confirmed', 'checks': [{'target': 'Fixture', 'status': 'confirmed', 'reason': ''}]}])
def test_invalid_or_inconsistent_speed_evidence_stays_unverified(tmp_path, invalid):
    with pytest.raises(ValueError):
        normalized_speed_checks(invalid)
    report = {'stages': [{'title': 'Apply supported speed enhancements', 'status': 'completed', 'result': {}}]}
    value = OutcomeVerification(tmp_path, speed_reader=Mock(return_value=invalid)).verify(report)
    assert value['counts']['confirmed'] == 0 and value['counts']['unverified'] == 1


def test_packaged_public_source_keeps_launch_scripts_and_excludes_private_data(tmp_path):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location('speed_source_packager', Path(__file__).parents[1] / 'tools/package_source.py')
    packager = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(packager)
    source, destination = tmp_path / 'source', tmp_path / 'public'
    (source / 'scripts').mkdir(parents=True)
    (source / 'scripts/Speed-Enhancements.ps1').write_text('# public fixture')
    (source / 'scripts/speed-enhancements.sh').write_text('# public fixture')
    (source / 'private').mkdir()
    (source / 'private/record.json').write_text('private fixture')
    packager.package(source, destination)
    assert (destination / 'scripts/Speed-Enhancements.ps1').is_file()
    assert (destination / 'scripts/speed-enhancements.sh').is_file()
    assert not (destination / 'private').exists()
