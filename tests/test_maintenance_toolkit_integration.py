"""Facade approval, global lease and fail-forward tests use injected services."""
import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from optimus.automation import RunLease
from optimus.maintenance_toolkit import MaintenanceToolkit, CACHE_STAGE, RETENTION_STAGE, PROVIDER_STAGE
from optimus.toolkit_configuration import DEFAULTS, validate_configuration, launch_descriptions


class Backend:
    def __init__(self, data_dir, configuration=None):
        self.data_dir, self.system = data_dir, 'linux'
        self.saved = {'toolkit_settings': copy.deepcopy(configuration or {})}
        self.host_calls = []
    def preferences(self):
        return copy.deepcopy(self.saved)
    def __getattr__(self, name):
        def forbidden(*args, **kwargs):
            self.host_calls.append(name)
            raise AssertionError('Unexpected host operation: ' + name)
        return forbidden


def configuration(**sections):
    return validate_configuration(sections)


def test_default_off_builds_no_stages_or_host_services_and_does_not_write(tmp_path):
    backend = Backend(tmp_path / 'data')
    facade = MaintenanceToolkit(backend)
    assert facade.stages({}, lease=None) == ([], [])
    assert facade._services == {} and backend.host_calls == []
    assert not backend.data_dir.exists()
    assert launch_descriptions({}) == ([], [])


@pytest.mark.parametrize('key,title', [('application_caches', CACHE_STAGE), ('recovery_retention', RETENTION_STAGE), ('providers', PROVIDER_STAGE)])
def test_only_saved_enabled_service_registers_a_stage(tmp_path, key, title):
    section = {'enabled': True}
    if key == 'application_caches':
        section['apps'] = ['discord']
    if key == 'providers':
        section.update(ids=['fixture-package'], agreements_accepted=True)
    config = configuration(**{key: section})
    backend = Backend(tmp_path / 'data', config)
    before, after = MaintenanceToolkit(backend).stages({'toolkit_settings': config}, lease=None)
    assert [stage[0] for stage in before] == [title] and after == []
    assert backend.host_calls == [] and not backend.data_dir.exists()


def test_manual_apply_owns_global_maintenance_lease_before_service(tmp_path, monkeypatch):
    config = configuration(application_caches={'enabled': True, 'apps': ['discord']})
    backend = Backend(tmp_path / 'data', config)
    facade = MaintenanceToolkit(backend)
    monkeypatch.setattr('optimus.parallel_updates.pending_updates_barrier', lambda _: None)
    observed = []
    def run(config, **kwargs):
        with pytest.raises(RuntimeError, match='already running maintenance'):
            RunLease(backend.data_dir).acquire()
        observed.append(config)
        return {'complete': True, 'deleted_bytes': 0}
    facade._services['application_caches'] = SimpleNamespace(run=run)
    assert facade.apply('application_caches')['complete'] is True and len(observed) == 1
    with RunLease(backend.data_dir):
        pass


def test_unknown_installer_barrier_defers_before_any_cleanup_service(tmp_path, monkeypatch):
    config = configuration(application_caches={'enabled': True, 'apps': ['discord']})
    backend = Backend(tmp_path / 'data', config)
    facade = MaintenanceToolkit(backend)
    monkeypatch.setattr('optimus.parallel_updates.pending_updates_barrier', lambda _: 'An active installer owns its files.')
    result = facade.apply('application_caches')
    assert result['deferred'] and result['installer_started'] is False and facade._services == {}
    assert not (backend.data_dir / 'application-cache-cleanup').exists()


def test_unowned_supplied_global_lease_rejected_before_mutation(tmp_path, monkeypatch):
    config = configuration(application_caches={'enabled': True, 'apps': ['discord']})
    facade = MaintenanceToolkit(Backend(tmp_path / 'data', config))
    monkeypatch.setattr('optimus.parallel_updates.pending_updates_barrier', lambda _: None)
    with pytest.raises(ValueError, match='owned maintenance lease'):
        facade._mutate('application_caches', lease=RunLease(tmp_path / 'data'))
    assert facade._services == {}


def test_revoked_or_expanded_saved_choices_refuse_manual_preview_apply(tmp_path, monkeypatch):
    from optimus.application_caches import ApplicationCacheCleaner, CacheRecipe
    config = configuration(application_caches={'enabled': True, 'apps': ['discord']})
    backend = Backend(tmp_path / 'data', config)
    facade = MaintenanceToolkit(backend)
    root = tmp_path / 'Cache'
    root.mkdir()
    path = root / 'data_0'
    path.write_bytes(b'preserve')
    recipe = CacheRecipe('fixture:cache', 'discord', root, ('fixture',))
    cleaner = ApplicationCacheCleaner(backend.data_dir, recipe_provider=lambda: [recipe],
        process_reader=lambda _: {'complete': True, 'active': False}, open_reader=lambda: (set(), True),
        scope_reader=lambda: facade.configuration()['application_caches'])
    facade._services['application_caches'] = cleaner
    monkeypatch.setattr('optimus.parallel_updates.pending_updates_barrier', lambda _: None)
    plan = facade.preview('application_caches')
    backend.saved['toolkit_settings']['application_caches']['apps'] = ['steam']
    with pytest.raises(ValueError, match='choices changed'):
        facade.apply('application_caches', plan)
    assert path.read_bytes() == b'preserve'


def test_queued_automatic_stage_cannot_expand_to_new_saved_app_approval(tmp_path, monkeypatch):
    config = configuration(application_caches={'enabled': True, 'apps': ['discord']})
    backend = Backend(tmp_path / 'data', config)
    facade = MaintenanceToolkit(backend)
    monkeypatch.setattr('optimus.parallel_updates.pending_updates_barrier', lambda _: None)
    calls = []
    facade._services['application_caches'] = SimpleNamespace(run=lambda config, **kwargs: calls.append(config) or {'complete': True})
    before, _ = facade.stages({'toolkit_settings': config}, lease=None)
    backend.saved['toolkit_settings']['application_caches']['apps'] = ['steam']
    try:
        result = before[0][2]()
    except ValueError:
        result = {'skipped': True}
    assert calls == [], 'A queued stage must not adopt an expanded later saved scope.'
    assert result.get('skipped') is True or result.get('deferred') is True


def test_growth_stage_cannot_adopt_later_selected_private_roots(tmp_path):
    original = tmp_path / 'original'
    original.mkdir()
    later = tmp_path / 'new-private-folder'
    later.mkdir()
    config = configuration(storage_growth={'enabled': True, 'roots': [str(original)]})
    backend = Backend(tmp_path / 'data', config)
    facade = MaintenanceToolkit(backend)
    seen = []
    facade._services['storage_growth'] = SimpleNamespace(scan=lambda config, **kwargs: seen.append(copy.deepcopy(config)) or {'read_only': True})
    before, _ = facade.stages({'toolkit_settings': config}, lease=None)
    backend.saved['toolkit_settings']['storage_growth']['roots'] = [str(later)]
    try:
        before[0][2]()
    except ValueError:
        pass
    assert not seen or seen[0]['roots'] == [str(original)]


def test_readonly_health_check_never_calls_actuators_or_repair(tmp_path, monkeypatch):
    backend = Backend(tmp_path / 'data')
    facade = MaintenanceToolkit(backend)
    monkeypatch.setattr('optimus.performance_history.collect_resource_window', lambda *args, **kwargs: {'complete': True, 'duration_seconds': 1})
    monkeypatch.setattr(facade, 'diagnostics', lambda **kwargs: {'read_only': True})
    monkeypatch.setattr(facade, 'headroom', lambda *args: {'read_only': True})
    monkeypatch.setattr(facade, 'readiness', lambda **kwargs: {'allowed': True, 'read_only': True})
    result = facade.check()
    assert result['read_only'] is True and result['maintenance_actions_executed'] == 0
    assert facade._services == {} and backend.host_calls == [] and not backend.data_dir.exists()


def test_launch_warning_details_both_retention_loss_and_app_scopes():
    config = configuration(application_caches={'enabled': True, 'apps': ['discord', 'steam']},
        recovery_retention={'enabled': True, 'browser_backups': True, 'lose_browser_undo_approved': True})
    actions, _ = launch_descriptions({'toolkit_settings': config})
    text = ' '.join(actions)
    assert 'discord' in text and 'steam' in text and 'undo will be lost' in text
