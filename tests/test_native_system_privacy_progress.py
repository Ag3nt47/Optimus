"""Policy-decision progress uses injected native responses, never host writes."""
import copy
import json
import threading

import pytest

from optimus.item_progress import normalize_item_progress
from optimus.system_privacy import SystemPrivacy, _CONTROLS
from test_system_privacy import NativeFixture, dword, mutation_calls, settings


def make_tool(tmp_path):
    native = NativeFixture()
    return SystemPrivacy(tmp_path, runner=native), native


def one_control():
    return settings(privacy_diagnostics_enabled=False,
                    privacy_disable_tailored_experiences=False,
                    privacy_disable_suggestions=False)


def test_progress_counts_only_durable_policy_decisions_and_final_receipt(tmp_path):
    tool, native = make_tool(tmp_path)
    events = []
    def receive(event):
        item = normalize_item_progress(event['item_progress'])
        assert item is not None and item['key'] == 'privacy:controls'
        assert item['phase'] == 'verify'
        if item['percent'] is not None:
            paths = list(tool.directory.glob('*.json'))
            assert len(paths) == 1
            saved = json.loads(paths[0].read_text(encoding='utf-8'))
            assert saved['checked_count'] == event['current']
            assert item['percent'] == event['current'] * 100 / event['total']
            if item['percent'] == 100:
                assert saved['status'] == 'completed' and saved['finished_at']
        events.append(copy.deepcopy(event))
    result = tool.apply_privacy(settings(), progress_callback=receive)
    assert events[0]['item_progress']['percent'] is None
    percentages = [e['item_progress']['percent'] for e in events if e['item_progress']['percent'] is not None]
    assert percentages[0] == 0 and percentages[-1] == 100
    assert percentages == sorted(percentages)
    assert result['checked_count'] == result['selected_count'] == len(result['items'])
    assert result['changed_count'] == result['selected_count']
    assert 'does not mean every setting was changed' in events[-1]['description']
    assert len(events) <= 2 * result['selected_count'] + 2
    assert len(mutation_calls(native)) == result['selected_count']


def test_actual_home_shape_is_not_applicable_without_claiming_policies_applied(tmp_path):
    tool, native = make_tool(tmp_path)
    native.context['edition'] = 'Core'
    for key, control in _CONTROLS.items():
        native.values[key] = dword(control['desired'])
    events = []
    result = tool.apply_privacy(settings(), progress_callback=events.append)
    expected = {'tailored_experiences', 'optional_diagnostics', 'app_diagnostics'}
    assert {x['id'] for x in result['items'] if x['status'] == 'not_applicable'} == expected
    assert result['status'] == 'completed' and result['not_applicable_count'] == 3
    assert result['failed_items'] == result['changed_count'] == 0
    assert not mutation_calls(native)
    assert events[-1]['item_progress']['percent'] == 100
    assert '0 applied, 3 not applicable, 0 need review' in events[-1]['description']


@pytest.mark.parametrize('edition', ['unknown', '', None, 'ProfessionalFutureUnknown'])
def test_unknown_edition_does_not_become_verified_nonapplicability(tmp_path, edition):
    tool, native = make_tool(tmp_path)
    native.context['edition'] = edition
    result = tool.apply_privacy(settings())
    assert result['not_applicable_count'] == 0
    # Preserve existing supported-prefix behavior; unknown consumer policies
    # independently must remain unverified, rather than silently skipped.
    extra = tool.apply_privacy({'privacy_disable_cloud_promotions_enabled': True})
    assert extra['failed_items'] == 2 and extra['not_applicable_count'] == 0
    assert all(row['status'] == 'unsupported' for row in extra['items'])


def test_known_policy_refusal_is_checked_but_not_fixed(tmp_path):
    tool, native = make_tool(tmp_path)
    original = native.__call__
    def refuse(operation, payload, **options):
        if operation == 'registry_write':
            native.calls.append((operation, copy.deepcopy(payload), options))
            return {'ok': False, 'permission_denied': True}
        return original(operation, payload, **options)
    tool.runner = refuse
    events = []
    result = tool.apply_privacy(one_control(), progress_callback=events.append)
    assert result['status'] == 'completed_with_errors' and result['failed_items'] == 1
    assert result['items'][0]['status'] == 'failed' and result['changed_count'] == 0
    assert events[-1]['item_progress']['percent'] == 100
    assert '0 applied, 0 not applicable, 1 need review' in events[-1]['description']


def test_callback_failure_cannot_abort_or_retry_registry_mutation(tmp_path):
    tool, native = make_tool(tmp_path)
    def fail(_):
        raise RuntimeError('Display observer failed')
    result = tool.apply_privacy(one_control(), progress_callback=fail)
    assert result['status'] == 'completed' and result['changed_count'] == 1
    assert len(mutation_calls(native)) == 1


@pytest.mark.parametrize('after', [0, 1])
def test_cancellation_at_policy_callback_stops_new_writes_without_completion(tmp_path, after):
    tool, native = make_tool(tmp_path)
    cancel = threading.Event()
    events = []
    def receive(event):
        events.append(copy.deepcopy(event))
        if event['item_progress']['percent'] is not None and event['current'] == after:
            cancel.set()
    result = tool.apply_privacy(settings(), cancel, progress_callback=receive)
    assert result['status'] == 'cancelled' and result['cancelled']
    assert len(mutation_calls(native)) == after
    assert all(event['item_progress']['percent'] != 100 for event in events)
    assert events[-1]['item_progress']['percent'] is None


@pytest.mark.parametrize('outcome', ['timeout', 'cancelled', 'malformed', 'exception'])
def test_unknown_write_stops_further_controls_and_never_reports_complete(tmp_path, outcome):
    tool, native = make_tool(tmp_path)
    original = native.__call__
    def uncertain(operation, payload, **options):
        if operation != 'registry_write':
            return original(operation, payload, **options)
        native.calls.append((operation, copy.deepcopy(payload), options))
        if outcome == 'exception':
            raise OSError('Native response lost')
        if outcome == 'malformed':
            return []
        return {'ok': False, 'may_continue': True, 'cancelled': outcome == 'cancelled',
                'timed_out': outcome == 'timeout'}
    tool.runner = uncertain
    events = []
    result = tool.apply_privacy(settings(), progress_callback=events.append)
    assert result['items'][0]['status'] == 'uncertain' and result['failed_items'] == 1
    assert result['checked_count'] == 0 and result['changed_count'] == 0
    assert len(mutation_calls(native)) == 1
    assert all(event['item_progress']['percent'] != 100 for event in events)
    assert events[-1]['item_progress']['percent'] is None
    saved = json.loads((tool.directory / (result['id'] + '.json')).read_text(encoding='utf-8'))
    assert saved['items'][0]['before'] == dword(1)
    assert saved['items'][0]['status'] == 'uncertain'


@pytest.mark.parametrize('final', [False, True])
def test_journal_failure_never_emits_terminal_progress(tmp_path, monkeypatch, final):
    tool, native = make_tool(tmp_path)
    write = tool._journal
    def denied(report):
        if not final or report['status'] != 'running':
            raise PermissionError('Fixture journal refused')
        return write(report)
    monkeypatch.setattr(tool, '_journal', denied)
    events = []
    with pytest.raises(PermissionError):
        tool.apply_privacy(one_control(), progress_callback=events.append)
    assert all(event['item_progress']['percent'] != 100 for event in events)
    assert len(mutation_calls(native)) == int(final)


def test_disabled_or_precancelled_choices_emit_nothing_and_run_no_native_query(tmp_path):
    tool, native = make_tool(tmp_path)
    events = []
    assert tool.apply_privacy({}, progress_callback=events.append)['status'] == 'completed'
    event = threading.Event()
    event.set()
    assert tool.apply_privacy(settings(), event, progress_callback=events.append)['cancelled']
    assert not events and not native.calls and not tool.directory.exists()
