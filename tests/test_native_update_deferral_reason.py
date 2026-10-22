"""Pre-install refusals explain the actual guard without changing update scope."""
import copy
from types import SimpleNamespace
from unittest.mock import Mock

from optimus.automated_software import run_software
from optimus.workflow import DEFAULTS


def test_known_preflight_reports_disk_reason_and_never_attempts_remaining_packages():
    configuration = dict(copy.deepcopy(DEFAULTS), software_updates_enabled=True,
        software_update_policy='all_supported', software_update_all_approved=True,
        software_update_agreements_accepted=True)
    rows = [dict(id=identity, package_id=identity, source='winget', name=identity,
                 version='1.0', available='2.0') for identity in ('Fixture.First', 'Fixture.Later')]
    reason = 'System-drive free space is below the 2 GiB installation floor. Free space before retrying updates.'
    service = SimpleNamespace(updates_scan=Mock(return_value=dict(structured=True, exitCode=0,
        source='winget', updates=rows)), update_app_unattended=Mock(return_value=dict(
        skipped=True, requiresReview=True, exitCode=0, installer_started=False,
        outcomeKnown=True, running=False, status='deferred', note=reason)))
    result = run_software(service, configuration)
    assert result['stop_reason'] == 'Update deferred: ' + reason
    assert result['deferred_ids'] == ['Fixture.Later']
    assert result['items'][0]['status'] == 'skipped'
    assert result['items'][0]['result']['installer_started'] is False
    service.update_app_unattended.assert_called_once_with('Fixture.First')


def test_unknown_installer_outcome_does_not_become_a_known_preflight_explanation():
    configuration = dict(copy.deepcopy(DEFAULTS), software_updates_enabled=True,
        software_update_policy='all_supported', software_update_all_approved=True,
        software_update_agreements_accepted=True)
    row = dict(id='Fixture.App', package_id='Fixture.App', source='winget', name='Fixture.App',
               version='1.0', available='2.0')
    service = SimpleNamespace(updates_scan=Mock(return_value=dict(structured=True, exitCode=0,
        source='winget', updates=[row])), update_app_unattended=Mock(return_value=dict(
        requiresReview=True, outcomeKnown=False, running=True, note='Incomplete vendor message')))
    result = run_software(service, configuration)
    assert result['stop_reason'] == 'The update safety checks need review before remaining applications can be updated.'
    assert result['items'][0]['result']['running'] is True
