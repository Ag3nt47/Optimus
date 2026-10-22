"""Priority fixtures never change the test host's process or threads."""
from unittest.mock import Mock
import pytest
from optimus import runtime_priority as priority


class Kernel:
    def __init__(self, process=priority.NORMAL, thread=0):
        self.process, self.thread, self.thread_id = process, thread, 123
        self.process_writes, self.thread_writes = [], []

    def GetCurrentProcess(self): return 1
    def GetCurrentThread(self): return 2
    def GetCurrentThreadId(self): return self.thread_id
    def GetPriorityClass(self, handle): return self.process
    def GetThreadPriority(self, handle): return self.thread
    def SetPriorityClass(self, handle, value):
        self.process = value
        self.process_writes.append(value)
        return True
    def SetThreadPriority(self, handle, value):
        self.thread = value
        self.thread_writes.append(value)
        return True


def test_interface_boost_is_session_owned_and_never_real_time(monkeypatch):
    kernel = Kernel()
    monkeypatch.setattr(priority, '_api', lambda: kernel)
    session = priority.InterfacePriority()
    assert session.apply(True)['status'] == 'applied'
    assert kernel.process_writes == [priority.ABOVE_NORMAL]
    session.restore()
    session.restore()
    assert kernel.process_writes == [priority.ABOVE_NORMAL, priority.NORMAL]
    assert 0x100 not in kernel.process_writes


def test_existing_boost_or_external_change_is_not_owned(monkeypatch):
    kernel = Kernel(process=priority.ABOVE_NORMAL)
    monkeypatch.setattr(priority, '_api', lambda: kernel)
    session = priority.InterfacePriority()
    assert session.apply(True)['status'] == 'already_configured'
    session.restore()
    assert kernel.process_writes == []
    kernel.process = priority.NORMAL
    session.apply(True)
    kernel.process = 0x80
    session.restore()
    assert kernel.process_writes == [priority.ABOVE_NORMAL]


def test_disabled_and_unsupported_sessions_do_not_request_priority(monkeypatch):
    api = Mock(return_value=None)
    monkeypatch.setattr(priority, '_api', api)
    session = priority.InterfacePriority()
    assert session.apply(False)['status'] == 'disabled'
    api.assert_not_called()
    assert session.apply(True)['status'] == 'unavailable'
    session.restore()


def test_priority_failure_is_visible_without_failing_startup(monkeypatch):
    monkeypatch.setattr(priority, '_api', Mock(side_effect=OSError('Denied')))
    session = priority.InterfacePriority()
    assert session.apply(True) == {'status': 'unavailable', 'changed': False, 'note': 'Denied'}
    session.restore()
    priority.lower_worker_priority()()


@pytest.mark.parametrize('existing', [0x40, 0x4000, 0x80, 0x100])
def test_interface_never_rewrites_external_priority_classes(monkeypatch, existing):
    kernel = Kernel(process=existing)
    monkeypatch.setattr(priority, '_api', lambda: kernel)
    session = priority.InterfacePriority()
    assert session.apply(True)['status'] == 'external_priority'
    session.restore()
    assert kernel.process == existing
    assert kernel.process_writes == []


def test_pooled_worker_has_normal_base_priority_then_restores(monkeypatch):
    kernel = Kernel(process=priority.ABOVE_NORMAL, thread=1)
    monkeypatch.setattr(priority, '_api', lambda: kernel)
    restore = priority.lower_worker_priority()
    assert kernel.thread_writes == [priority.THREAD_LOWEST]
    restore()
    restore()
    assert kernel.thread_writes == [priority.THREAD_LOWEST, 1]


def test_wrong_thread_cannot_change_ui_and_rightful_owner_can_restore(monkeypatch):
    kernel = Kernel(process=priority.ABOVE_NORMAL)
    monkeypatch.setattr(priority, '_api', lambda: kernel)
    restore = priority.lower_worker_priority()
    kernel.thread_id = 456
    restore()
    assert kernel.thread_writes == [priority.THREAD_LOWEST]
    kernel.thread_id = 123
    restore()
    assert kernel.thread_writes == [priority.THREAD_LOWEST, 0]


@pytest.mark.parametrize('process', [priority.NORMAL, 0x80, 0x100])
def test_worker_does_not_override_other_process_priority_classes(monkeypatch, process):
    kernel = Kernel(process=process)
    monkeypatch.setattr(priority, '_api', lambda: kernel)
    priority.lower_worker_priority()()
    assert kernel.thread_writes == []


def test_worker_denial_and_restore_failure_never_break_tasks(monkeypatch):
    kernel = Kernel(process=priority.ABOVE_NORMAL)
    kernel.SetThreadPriority = Mock(return_value=False)
    monkeypatch.setattr(priority, '_api', lambda: kernel)
    priority.lower_worker_priority()()
    assert kernel.thread == 0
    kernel.SetThreadPriority = Kernel.SetThreadPriority.__get__(kernel)
    restore = priority.lower_worker_priority()
    kernel.SetThreadPriority = Mock(side_effect=RuntimeError('Fixture API error'))
    restore()


def test_external_thread_priority_change_is_preserved(monkeypatch):
    kernel = Kernel(process=priority.ABOVE_NORMAL)
    monkeypatch.setattr(priority, '_api', lambda: kernel)
    restore = priority.lower_worker_priority()
    kernel.thread = 2
    restore()
    assert kernel.thread == 2
    assert kernel.thread_writes == [priority.THREAD_LOWEST]
