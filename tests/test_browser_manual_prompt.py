"""Manual browser preview never closes or accesses data after a declined prompt."""
import threading
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from optimus.backend import Backend


@pytest.fixture
def backend():
    service = Backend.__new__(Backend)
    service.browser_privacy_engine = SimpleNamespace(
        _profile=Mock(return_value={'browser': 'Google Chrome', 'name': 'Default'}),
        scan=Mock(return_value={'canApply': True, 'counts': {'history': 2}}))
    service.browser_cleanup_status = Mock(return_value={'complete': True, 'running': True,
        'processes': [{'pid': 123, 'name': 'chrome.exe', 'secret': 'do not forward'}]})
    service.browser_cleanup_close = Mock(return_value={'closed': True, 'complete': True})
    return service


def test_declined_browser_stays_open_and_no_database_scan(backend):
    prompt = Mock(return_value=False)
    result = backend.privacy_scan('fixture', categories=['history'], browser_prompt=prompt)
    assert result['skipped'] and not result['canApply']
    backend.browser_cleanup_close.assert_not_called()
    backend.browser_privacy_engine.scan.assert_not_called()
    assert prompt.call_args.args[0]['processes'] == [{'pid': 123, 'name': 'chrome.exe'}]


def test_yes_requests_close_before_fresh_preview(backend):
    events = []
    backend.browser_cleanup_close.side_effect = lambda *args, **kwargs: events.append('close') or {'closed': True, 'complete': True}
    backend.browser_privacy_engine.scan.side_effect = lambda *args, **kwargs: events.append('scan') or {'canApply': True}
    result = backend.privacy_scan('fixture', categories=['cookies'], browser_prompt=lambda info: True)
    assert result['canApply'] and events == ['close', 'scan']


@pytest.mark.parametrize('closed', [False, None])
def test_incomplete_close_skips_database(backend, closed):
    backend.browser_cleanup_close.return_value = {'closed': closed, 'complete': True}
    assert backend.privacy_scan('fixture', categories=['history'], browser_prompt=lambda info: True)['skipped']
    backend.browser_privacy_engine.scan.assert_not_called()


def test_unknown_process_state_never_prompts_or_closes(backend):
    backend.browser_cleanup_status.return_value = {'complete': False, 'running': False}
    prompt = Mock(return_value=True)
    assert backend.privacy_scan('fixture', categories=['history'], browser_prompt=prompt)['skipped']
    prompt.assert_not_called()
    backend.browser_cleanup_close.assert_not_called()
    backend.browser_privacy_engine.scan.assert_not_called()


def test_cancel_after_yes_prevents_close(backend):
    cancel = threading.Event()
    def answer(info):
        cancel.set()
        return True
    assert backend.privacy_scan('fixture', categories=['history'], browser_prompt=answer, cancel_event=cancel)['skipped']
    backend.browser_cleanup_close.assert_not_called()
    backend.browser_privacy_engine.scan.assert_not_called()


def test_closed_browser_uses_preview_without_dialog(backend):
    backend.browser_cleanup_status.return_value = {'complete': True, 'running': False}
    prompt = Mock(return_value=True)
    assert backend.privacy_scan('fixture', categories=['history'], browser_prompt=prompt)['canApply']
    prompt.assert_not_called()
    backend.browser_cleanup_close.assert_not_called()
