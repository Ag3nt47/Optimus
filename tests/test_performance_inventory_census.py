"""Inventory census ancestry reads: fake processes only, no real process inspection."""
from types import SimpleNamespace

from optimus import performance


class FakeProcess:
    def __init__(self, table, pid):
        self.pid, self.table = pid, table
        table['opened'].append(pid)

    def exe(self):
        return self.table['rows'][self.pid]['exe']

    def create_time(self):
        return self.table['rows'][self.pid]['created']

    def ppid(self):
        self.table['ppid_calls'] += 1
        return self.table['rows'][self.pid]['parent']


def census(rows, parents):
    table = {'rows': rows, 'opened': [], 'ppid_calls': 0}
    context = object.__new__(performance._InventoryContext)
    context._parents, context._codex_related = dict(parents), {}
    return context, table


def test_shared_ancestors_are_read_once_and_codex_children_are_found(monkeypatch):
    codex = performance._trusted_codex_exe
    rows = {1: {'exe': 'C:/codex.exe', 'created': 1, 'parent': 0},
            2: {'exe': 'C:/shell.exe', 'created': 2, 'parent': 1},
            3: {'exe': 'C:/tool-a.exe', 'created': 3, 'parent': 2},
            4: {'exe': 'C:/tool-b.exe', 'created': 4, 'parent': 2}}
    context, table = census(rows, {1: 0, 2: 1, 3: 2, 4: 2})
    monkeypatch.setattr(performance, '_trusted_codex_exe', lambda exe: exe == 'C:/codex.exe')
    monkeypatch.setattr(performance.psutil, 'Process', lambda pid: FakeProcess(table, pid))
    assert context.related_to_codex(FakeProcess(table, 3)) is True
    assert context.related_to_codex(FakeProcess(table, 4)) is True
    assert table['opened'].count(2) == 1 and table['opened'].count(1) == 1  # Ancestors are opened once.
    assert table['ppid_calls'] == 0  # The census map replaces per-process native parent snapshots.
    monkeypatch.setattr(performance, '_trusted_codex_exe', codex)


def test_reused_parent_pid_newer_than_child_ends_the_chain(monkeypatch):
    rows = {10: {'exe': 'C:/codex.exe', 'created': 50, 'parent': 0},
            11: {'exe': 'C:/child.exe', 'created': 20, 'parent': 10}}
    context, table = census(rows, {11: 10})
    monkeypatch.setattr(performance, '_trusted_codex_exe', lambda exe: exe == 'C:/codex.exe')
    monkeypatch.setattr(performance.psutil, 'Process', lambda pid: FakeProcess(table, pid))
    assert context.related_to_codex(FakeProcess(table, 11)) is False


def test_cyclic_parent_map_terminates(monkeypatch):
    rows = {5: {'exe': 'C:/a.exe', 'created': 1, 'parent': 6}, 6: {'exe': 'C:/b.exe', 'created': 1, 'parent': 5}}
    context, table = census(rows, {5: 6, 6: 5})
    monkeypatch.setattr(performance, '_trusted_codex_exe', lambda exe: False)
    monkeypatch.setattr(performance.psutil, 'Process', lambda pid: FakeProcess(table, pid))
    assert context.related_to_codex(FakeProcess(table, 5)) is False


def test_missing_native_map_falls_back_to_the_live_parent(monkeypatch):
    rows = {7: {'exe': 'C:/x.exe', 'created': 1, 'parent': 3}}
    context, table = census(rows, {})
    assert context.ppid(FakeProcess(table, 7)) == 3 and table['ppid_calls'] == 1


def test_parent_map_reader_is_optional(monkeypatch):
    monkeypatch.setattr(performance, '_PLATFORM', 'linux')
    assert performance._parent_map() is None
    monkeypatch.setattr(performance, '_PLATFORM', 'win32')
    import psutil
    monkeypatch.setattr(psutil, '_psplatform', SimpleNamespace(cext=SimpleNamespace(ppid_map=lambda: {8: 4})), raising=False)
    assert performance._parent_map() == {8: 4}
    monkeypatch.setattr(psutil, '_psplatform', SimpleNamespace(cext=SimpleNamespace(ppid_map=lambda: ['bad'])), raising=False)
    assert performance._parent_map() is None
