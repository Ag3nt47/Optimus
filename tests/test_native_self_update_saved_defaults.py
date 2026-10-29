"""Effective update consent never rewrites or silently broadens saved choices."""
import copy
import hashlib
import hmac
import json
from unittest.mock import Mock

import pytest

from optimus import self_update as module
from optimus import workflow
from optimus.launch_review import scope_fingerprint
from test_native_self_update import updater, staged, handoff, new_service
from test_native_self_update_ui import owner, controller, updater as ui_updater, wait_for


MISSING_DEFAULT_KEYS = {
    'codex_archives_cleanup_enabled', 'codex_archives_retention_days',
    'angerona_logs_cleanup_enabled', 'angerona_logs_retention_days',
    'failed_downloads_cleanup_enabled', 'failed_downloads_retention_days',
    'drive_space_guard_enabled', 'drive_space_guard_target_gib',
    'update_payload_cleanup_enabled', 'toolkit_settings',
}


def saved_choices(p, *, complete=False):
    effective = workflow.Preferences.validate(dict(copy.deepcopy(workflow.DEFAULTS), setup_version=2))
    raw_choices = copy.deepcopy(effective) if complete else {
        key: value for key, value in effective.items() if key not in MISSING_DEFAULT_KEYS}
    assert len(effective) == 101
    assert len(raw_choices) == (101 if complete else 91)
    p.preferences = raw_choices
    p.raw = (json.dumps(raw_choices, indent=3) + '\n').encode()
    (p.data / 'preferences.json').write_bytes(p.raw)
    return effective


def resign_fixture_record(p, identity, name, update):
    """Model a valid older producer in disposable fixture state, not tampering."""
    path = p.service.directory / identity / name
    value = json.loads(path.read_bytes())['value']
    update(value)
    body = module._json(value)
    path.write_bytes(module._json({'value': value,
        'signature': hmac.new(p.service._key(), body, hashlib.sha256).hexdigest()}))


def test_sparse_saved_file_uses_explicit_effective_approval_and_preserves_all_bytes(updater):
    p = updater
    effective = saved_choices(p)
    assert module.canonical_approval_scope(p.preferences) == effective
    assert module.canonical_approval_scope(effective) == effective
    assert len(p.preferences) == 91
    identity = handoff(p)
    prepared, _ = p.service._load(identity, 'handoff.json')
    assert prepared['binding']['preferences_sha256'] == hashlib.sha256(p.raw).hexdigest()
    assert prepared['cleanup_approval_schema'] == 2
    assert prepared['approved_scope_sha256'] == scope_fingerprint(effective)
    p.service.finish_handoff(identity)
    accepted = new_service(p).consume_resume(identity)
    assert accepted['cleanup_approved'] is True
    assert accepted['cleanup_approval_schema'] == 2
    assert accepted['settings'] == effective
    assert accepted['approved_scope_sha256'] == scope_fingerprint(effective)
    consumed, _ = p.service._load(identity, 'resume-consumed.json')
    assert consumed['cleanup_approval_schema'] == 2
    assert consumed['approved_scope_sha256'] == scope_fingerprint(effective)
    assert consumed['cleanup_approved'] is True and consumed['at_most_once'] is True
    assert (p.data / 'preferences.json').read_bytes() == p.raw
    assert len(json.loads(p.raw)) == 91
    with pytest.raises(FileExistsError):
        new_service(p).consume_resume(identity)


@pytest.mark.parametrize('kind', ['raw_sparse', 'integer_boolean', 'changed_choice', 'extra_field'])
def test_prepare_requires_the_exact_already_effective_snapshot(updater, kind):
    p = updater
    effective = saved_choices(p)
    snapshot = copy.deepcopy(effective)
    if kind == 'raw_sparse':
        snapshot = copy.deepcopy(p.preferences)
    elif kind == 'integer_boolean':
        snapshot['cleanup_enabled'] = int(snapshot['cleanup_enabled'])
        assert snapshot == effective  # Python equality alone is insufficient.
    elif kind == 'changed_choice':
        snapshot['cleanup_enabled'] = not snapshot['cleanup_enabled']
    else:
        snapshot['unreviewed_field'] = False
    stage = staged(p)
    with pytest.raises(ValueError, match='explicitly approved'):
        p.service.prepare_handoff(stage['stage_id'], cleanup_approved=True, approved_settings=snapshot)
    assert not (p.service.directory / stage['stage_id'] / 'handoff.json').exists()
    assert (p.data / 'preferences.json').read_bytes() == p.raw


@pytest.mark.parametrize('change', ['changed_default', 'new_default'])
def test_new_runtime_default_drift_cannot_inherit_old_scope(updater, monkeypatch, change):
    p = updater
    original = saved_choices(p)
    identity = handoff(p)
    p.service.finish_handoff(identity)
    if change == 'changed_default':
        monkeypatch.setitem(workflow.DEFAULTS, 'codex_archives_cleanup_enabled', True)
    else:
        monkeypatch.setitem(workflow.DEFAULTS, 'future_action_enabled', False)
    accepted = new_service(p).consume_resume(identity)
    assert accepted['status'] == 'accepted' and accepted['cleanup_approved'] is False
    assert accepted['settings'] != original
    assert accepted['approved_scope_sha256'] is None
    consumed, _ = p.service._load(identity, 'resume-consumed.json')
    assert consumed['cleanup_approved'] is False and consumed['approved_scope_sha256'] is None
    assert (p.data / 'preferences.json').read_bytes() == p.raw


@pytest.mark.parametrize('explicit_schema', [False, True])
@pytest.mark.parametrize('complete', [False, True])
def test_legacy_approval_cannot_fill_previously_unbound_defaults(updater, complete, explicit_schema):
    p = updater
    effective = saved_choices(p, complete=complete)
    identity = handoff(p)
    p.service.finish_handoff(identity)
    def legacy(value):
        if explicit_schema:
            value['cleanup_approval_schema'] = 1
        else:
            value.pop('cleanup_approval_schema')
        value['approved_scope_sha256'] = scope_fingerprint(p.preferences)
    resign_fixture_record(p, identity, 'ready.json', legacy)
    accepted = new_service(p).consume_resume(identity)
    assert accepted['cleanup_approved'] is complete
    assert accepted['settings'] == effective
    assert accepted['cleanup_approval_schema'] == 1
    assert (p.data / 'preferences.json').read_bytes() == p.raw


@pytest.mark.parametrize('schema', [0, 3, True, '2', None])
def test_unrecognized_approval_schema_never_grants_cleanup(updater, schema):
    p = updater
    saved_choices(p, complete=True)
    identity = handoff(p)
    p.service.finish_handoff(identity)
    resign_fixture_record(p, identity, 'ready.json', lambda value: value.update(cleanup_approval_schema=schema))
    assert new_service(p).consume_resume(identity)['cleanup_approved'] is False
    assert (p.data / 'preferences.json').read_bytes() == p.raw


def test_effective_resume_reaches_strict_gui_gate_once_without_migrating_file(updater, owner):
    p = updater
    effective = saved_choices(p)
    identity = handoff(p)
    p.service.finish_handoff(identity)
    child = new_service(p)
    app, window, backend, settings = owner
    settings.clear()
    settings.update(copy.deepcopy(effective))
    value, service = controller(owner, ui_updater(consume_resume=Mock(side_effect=child.consume_resume)),
                                resume_id=identity)
    value.start()
    wait_for(app, lambda: window.start_optimization.called)
    assert window.session_approved()
    service.consume_resume.assert_called_once_with(identity)
    service.acknowledge_resume.assert_called_once_with(identity)
    window.start_optimization.assert_called_once()
    value._complete_resume(service.consume_resume.call_args)
    window.start_optimization.assert_called_once()
    backend.optimize.assert_not_called()
    assert (p.data / 'preferences.json').read_bytes() == p.raw


def test_invalid_raw_preferences_are_not_silently_reinterpreted(updater):
    p = updater
    saved_choices(p)
    p.preferences['cleanup_enabled'] = 1
    p.raw = json.dumps(p.preferences).encode()
    (p.data / 'preferences.json').write_bytes(p.raw)
    stage = staged(p)
    with pytest.raises(ValueError, match='on or off'):
        p.service.prepare_handoff(stage['stage_id'])
    assert (p.data / 'preferences.json').read_bytes() == p.raw
