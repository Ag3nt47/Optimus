"""Recording/privacy policies use an injected OS fixture, never the host registry."""
import copy
from unittest.mock import Mock

import pytest

from optimus.native_settings import NativePreferences
from optimus.system_privacy import SystemPrivacy, _CONTROLS
from optimus.workflow import DEFAULTS, WINDOWS_REMOVAL_KEYS, Preferences, Workflow, full_optimization_settings
from test_system_privacy import NativeFixture, dword, mutation_calls
from test_workflow import MockBackend


FLAGS = ('privacy_disable_online_speech_enabled', 'privacy_disable_voice_activation_enabled',
         'privacy_disable_activity_history_enabled')
TARGETS = {
    FLAGS[0]: {'online_speech_preference', 'online_speech_policy'},
    FLAGS[1]: {'voice_activation', 'voice_activation_locked'},
    FLAGS[2]: {'activity_feed', 'activity_publish', 'activity_upload'},
}


def only(**changes):
    # Default-on product presets must not silently expand a unit's approved scope.
    return {key: False for key in DEFAULTS if key.startswith('privacy_')} | changes


@pytest.fixture
def fixture(tmp_path):
    runner = NativeFixture()
    return SystemPrivacy(tmp_path, runner=runner), runner


@pytest.mark.parametrize('key', FLAGS)
def test_defaults_on_do_not_overwrite_explicit_false_or_native_platform_limits(key):
    assert DEFAULTS[key] is True
    assert Preferences.validate({})[key] is True
    assert full_optimization_settings()[key] is True
    assert Preferences.validate({key: False})[key] is False
    assert full_optimization_settings({key: False})[key] is False
    assert Preferences.safe_defaults()[key] is False
    assert NativePreferences.validate({key: True})[key] is False


@pytest.mark.parametrize('key', FLAGS)
@pytest.mark.parametrize('invalid', [None, 0, 1, 'yes', [], {}])
def test_recording_preferences_and_direct_requests_require_real_booleans(fixture, key, invalid):
    tool, runner = fixture
    with pytest.raises(ValueError):
        Preferences.validate({key: invalid})
    with pytest.raises(ValueError):
        tool.apply_privacy(only(**{key: invalid}))
    assert runner.calls == []


@pytest.mark.parametrize('existing', [0, 1, 2])
def test_voice_activation_uses_documented_force_deny_value_two(fixture, existing):
    tool, runner = fixture
    runner.values.update({key: dword(existing) for key in TARGETS[FLAGS[1]]})
    result = tool.apply_privacy(only(**{FLAGS[1]: True}))
    assert {row['id'] for row in result['items']} == TARGETS[FLAGS[1]]
    assert result['failed_items'] == 0
    assert result['changed_count'] == (0 if existing == 2 else 2)
    assert all(row['desired'] == 2 for row in result['items'])
    assert all(row['status'] == ('already_configured' if existing == 2 else 'applied') for row in result['items'])
    assert all(runner.values[key] == dword(2) for key in TARGETS[FLAGS[1]])
    assert all(payload['value'] == 2 for _, payload, _ in mutation_calls(runner))


def test_unrecognized_voice_setting_skips_that_control_and_continues(fixture):
    tool, runner = fixture
    runner.values.update(voice_activation=dword(9), voice_activation_locked=dword(1))
    result = tool.apply_privacy(only(**{FLAGS[1]: True}))
    rows = {row['id']: row for row in result['items']}
    assert rows['voice_activation']['status'] == 'failed'
    assert rows['voice_activation_locked']['status'] == 'applied'
    assert runner.values['voice_activation'] == dword(9)
    assert [payload['control'] for _, payload, _ in mutation_calls(runner)] == ['voice_activation_locked']


@pytest.mark.parametrize('edition', ['Core', 'CoreN', 'CoreSingleLanguage'])
@pytest.mark.parametrize('existing', [0, 1, None])
def test_home_changes_only_an_existing_cloud_speech_preference(fixture, edition, existing):
    tool, runner = fixture
    runner.context.update(edition=edition, is_admin=False)
    runner.values.pop('online_speech_preference', None)
    if existing is not None:
        runner.values['online_speech_preference'] = dword(existing)
    result = tool.apply_privacy(only(**{FLAGS[0]: True}))
    rows = {row['id']: row for row in result['items']}
    assert rows['online_speech_policy']['status'] == 'not_applicable'
    assert rows['online_speech_preference']['status'] == {
        0: 'already_configured', 1: 'applied', None: 'not_applicable'}[existing]
    assert {payload['control'] for _, payload, _ in mutation_calls(runner)} == (
        {'online_speech_preference'} if existing == 1 else set())
    assert 'Microphone access and Windows App remote audio are preserved' in rows['online_speech_preference']['note']


@pytest.mark.parametrize('key', [FLAGS[1], FLAGS[2]])
def test_home_does_not_claim_unsupported_device_policies_are_applied(fixture, key):
    tool, runner = fixture
    runner.context.update(edition='Core', is_admin=True)
    result = tool.apply_privacy(only(**{key: True}))
    assert {row['id'] for row in result['items']} == TARGETS[key]
    assert all(row['status'] == 'not_applicable' for row in result['items'])
    assert result['changed_count'] == 0 and not mutation_calls(runner)


@pytest.mark.parametrize('key,build', [
    (FLAGS[0], 10239), (FLAGS[1], 18361), (FLAGS[2], 16298),
])
def test_old_builds_do_not_invent_unsupported_policy_values(fixture, key, build):
    tool, runner = fixture
    runner.context['build'] = build
    result = tool.apply_privacy(only(**{key: True}))
    assert all(row['status'] in ('not_applicable', 'unsupported') for row in result['items'])
    assert not mutation_calls(runner)


def test_activity_upload_waits_for_1803_while_1709_controls_still_apply(fixture):
    tool, runner = fixture
    runner.context['build'] = 16299
    result = tool.apply_privacy(only(**{FLAGS[2]: True}))
    rows = {row['id']: row for row in result['items']}
    assert rows['activity_feed']['status'] == rows['activity_publish']['status'] == 'applied'
    assert rows['activity_upload']['status'] == 'not_applicable'
    assert all(payload['value'] == 0 for _, payload, _ in mutation_calls(runner))


@pytest.mark.parametrize('key', FLAGS)
def test_exact_saved_scope_is_backed_up_verified_and_can_be_restored(fixture, key):
    tool, runner = fixture
    runner.values.update({control: dword(1) for control in TARGETS[key]})
    before = copy.deepcopy(runner.values)
    result = tool.apply_privacy(only(**{key: True}))
    assert result['status'] == 'completed'
    assert {row['id'] for row in result['items']} == TARGETS[key]
    assert {payload['control'] for _, payload, _ in mutation_calls(runner)} == TARGETS[key]
    restored = tool.restore_privacy(result['id'])
    assert restored['status'] == 'completed'
    assert runner.values == before


def test_recording_toggles_cannot_deny_microphone_camera_or_remote_desktop_resources():
    actual = {key: (control['hive'], control['path'], control['name'], control['desired'])
              for key, control in _CONTROLS.items() if control['master'] in FLAGS}
    assert actual == {
        'online_speech_preference': ('CurrentUser', r'Software\Microsoft\Speech_OneCore\Settings\OnlineSpeechPrivacy', 'HasAccepted', 0),
        'online_speech_policy': ('LocalMachine', r'Software\Policies\Microsoft\InputPersonalization', 'AllowInputPersonalization', 0),
        'voice_activation': ('LocalMachine', r'Software\Policies\Microsoft\Windows\AppPrivacy', 'LetAppsActivateWithVoice', 2),
        'voice_activation_locked': ('LocalMachine', r'Software\Policies\Microsoft\Windows\AppPrivacy', 'LetAppsActivateWithVoiceAboveLock', 2),
        'activity_feed': ('LocalMachine', r'Software\Policies\Microsoft\Windows\System', 'EnableActivityFeed', 0),
        'activity_publish': ('LocalMachine', r'Software\Policies\Microsoft\Windows\System', 'PublishUserActivities', 0),
        'activity_upload': ('LocalMachine', r'Software\Policies\Microsoft\Windows\System', 'UploadUserActivities', 0),
    }
    names = {control['name'] for control in _CONTROLS.values()}
    assert names.isdisjoint({'LetAppsAccessMicrophone', 'LetAppsAccessCamera', 'fDenyTSConnections',
                             'fDisableAudioCapture', 'fDisableClip', 'DisableRealtimeMonitoring'})


@pytest.mark.parametrize('key', FLAGS)
def test_independent_recording_toggle_dispatches_privacy_maintenance(tmp_path, key):
    backend = MockBackend(tmp_path)
    backend.apply_system_privacy = Mock(return_value={'status': 'completed', 'failed_items': 0,
                                                     'changed_count': 0, 'items': []})
    config = dict(DEFAULTS, cleanup_enabled=False, browser_cache_enabled=False)
    config.update(dict.fromkeys(WINDOWS_REMOVAL_KEYS, False))
    config.update(only(**{key: True}))
    report = Workflow(backend).run(config, lambda _: None)
    backend.apply_system_privacy.assert_called_once()
    assert report['failed_stages'] == 0
