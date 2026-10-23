"""File tools exercise only temporary fixtures; no host documents are touched."""
from contextlib import contextmanager
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import struct
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus import file_utilities as files


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(files, 'pending_updates_barrier', Mock(return_value=None))
    value = files.FileUtilities(SimpleNamespace(data_dir=tmp_path / 'state'), owner='fixture-owner')
    monkeypatch.setattr(value, '_document_roots', lambda: [tmp_path / 'Documents'])
    return value


@pytest.fixture
def original(tmp_path):
    path = tmp_path / 'original.bin'
    path.write_bytes(b'fixture content\x00with binary data\xff' * 100)
    return path


CRYPTO = pytest.mark.skipif(importlib.util.find_spec('cryptography') is None,
                            reason='Optional cryptography dependency unavailable')
PASSWORD = 'Fixture-only 2026 password'


def test_constructing_file_tools_has_no_scan_or_host_calls(tmp_path, monkeypatch):
    native = Mock(side_effect=AssertionError('No native call during construction'))
    monkeypatch.setattr(files, '_current_owner', native)
    files.FileUtilities(SimpleNamespace(data_dir=tmp_path / 'state'))
    native.assert_not_called()
    assert list(tmp_path.iterdir()) == []


@CRYPTO
@pytest.mark.parametrize('content', [b'', b'a', b'\x00\xff' * 100, b'chunked' * (files._CHUNK // 3)],
                         ids=['empty', 'one_byte', 'binary', 'multi_chunk'])
def test_authenticated_stream_roundtrip_preserves_original(service, tmp_path, content):
    source, encrypted, decrypted = (tmp_path / name for name in ('input', 'encrypted.opt', 'decrypted'))
    source.write_bytes(content)
    before = hashlib.sha256(content).hexdigest()
    result = service.encrypt(source, encrypted, PASSWORD)
    assert result['status'] == 'completed', result
    assert encrypted.read_bytes().startswith(files._MAGIC)
    result = service.decrypt(encrypted, decrypted, PASSWORD)
    assert result['status'] == 'completed' and result['authenticated'], result
    assert decrypted.read_bytes() == content and source.read_bytes() == content
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert PASSWORD not in json.dumps(result)
    assert list(tmp_path.glob('.optimus-*.partial')) == []


def encrypted_frames(raw):
    offset = len(files._MAGIC)
    length = struct.unpack('>I', raw[offset:offset+4])[0]
    start = offset + 4 + length
    frames = []
    offset = start
    while offset < len(raw):
        length = struct.unpack('>I', raw[offset+1:offset+5])[0]
        frames.append(raw[offset:offset+5+length])
        offset += 5 + length
    return raw[:start], frames


@CRYPTO
@pytest.mark.parametrize('damage', ['wrong_password', 'ciphertext', 'truncate', 'missing_final', 'trailing', 'reorder', 'duplicate'])
def test_tampered_or_truncated_container_never_publishes_plaintext(service, tmp_path, damage):
    source, encrypted, output = (tmp_path / name for name in ('input', 'encrypted', 'output'))
    source.write_bytes(b'a' * files._CHUNK + b'b' * 37)
    assert service.encrypt(source, encrypted, PASSWORD)['status'] == 'completed'
    raw = encrypted.read_bytes()
    header, frames = encrypted_frames(raw)
    password = PASSWORD
    if damage == 'wrong_password':
        password = 'A different fixture password'
    elif damage == 'ciphertext':
        data = bytearray(frames[0]); data[10] ^= 1; frames[0] = bytes(data)
        raw = header + b''.join(frames)
    elif damage == 'truncate':
        raw = raw[:-1]
    elif damage == 'missing_final':
        raw = header + b''.join(frames[:-1])
    elif damage == 'trailing':
        raw += b'extra'
    elif damage == 'reorder':
        raw = header + frames[1] + frames[0] + frames[2]
    else:
        raw = header + frames[0] + frames[0] + frames[1] + frames[2]
    encrypted.write_bytes(raw)
    result = service.decrypt(encrypted, output, password)
    assert result['status'] == 'failed', result
    assert not output.exists() and list(tmp_path.glob('.optimus-*.partial')) == []
    assert source.read_bytes() == b'a' * files._CHUNK + b'b' * 37


@CRYPTO
def test_encrypt_and_decrypt_never_overwrite_existing_outputs(service, original, tmp_path):
    output = tmp_path / 'existing'
    output.write_bytes(b'preserve this')
    assert service.encrypt(original, output, PASSWORD)['status'] == 'failed'
    assert output.read_bytes() == b'preserve this'
    encrypted = tmp_path / 'valid'
    assert service.encrypt(original, encrypted, PASSWORD)['status'] == 'completed'
    assert service.decrypt(encrypted, output, PASSWORD)['status'] == 'failed'
    assert output.read_bytes() == b'preserve this'


@CRYPTO
def test_cancelled_encryption_or_decryption_discards_staging(service, tmp_path):
    source = tmp_path / 'input'; source.write_bytes(b'z' * (files._CHUNK + 9))
    encrypted, decrypted = tmp_path / 'encrypted', tmp_path / 'decrypted'
    cancel = threading.Event()
    result = service.encrypt(source, encrypted, PASSWORD, cancel_event=cancel,
                             progress_callback=lambda state: cancel.set())
    assert result['status'] == 'cancelled' and not encrypted.exists()
    cancel.clear()
    assert service.encrypt(source, encrypted, PASSWORD)['status'] == 'completed'
    result = service.decrypt(encrypted, decrypted, PASSWORD, cancel_event=cancel,
                             progress_callback=lambda state: cancel.set())
    assert result['status'] == 'cancelled' and not decrypted.exists()
    assert list(tmp_path.glob('.optimus-*.partial')) == []


@CRYPTO
def test_output_appearing_mid_encryption_is_preserved(service, original, tmp_path):
    destination = tmp_path / 'destination'
    result = service.encrypt(original, destination, PASSWORD,
        progress_callback=lambda state: destination.write_bytes(b'concurrent choice'))
    assert result['status'] == 'failed'
    assert destination.read_bytes() == b'concurrent choice'
    assert list(tmp_path.glob('.optimus-*.partial')) == []


def test_split_join_stream_verifies_all_bytes_and_keeps_sources(service, original, tmp_path):
    folder = tmp_path / 'parts'; folder.mkdir()
    result = service.split(original, folder, part_size=700)
    assert result['status'] == 'completed', result
    manifest = Path(result['manifest'])
    data = json.loads(manifest.read_text())
    assert [row['index'] for row in data['parts']] == list(range(result['parts']))
    output = tmp_path / 'joined'
    result = service.join(manifest, output)
    assert result['status'] == 'completed' and result['sha256_verified'], result
    assert output.read_bytes() == original.read_bytes()
    assert manifest.exists() and all((folder / row['name']).exists() for row in data['parts'])


@pytest.mark.parametrize('damage', ['part_content', 'part_missing', 'order', 'part_name', 'whole_hash'])
def test_join_rejects_changed_missing_or_reordered_parts(service, original, tmp_path, damage):
    folder = tmp_path / 'parts'; folder.mkdir()
    result = service.split(original, folder, part_size=700)
    manifest = Path(result['manifest'])
    value = json.loads(manifest.read_text())
    if damage == 'part_content':
        target = folder / value['parts'][0]['name']
        target.write_bytes(b'X' * target.stat().st_size)
    elif damage == 'part_missing':
        (folder / value['parts'][0]['name']).unlink()
    elif damage == 'order':
        value['parts'][0], value['parts'][1] = value['parts'][1], value['parts'][0]
    elif damage == 'part_name':
        value['parts'][0]['name'] = '../original.bin'
    else:
        value['sha256'] = '0' * 64
    if damage in ('order', 'part_name', 'whole_hash'):
        manifest.write_text(json.dumps(value))
    output = tmp_path / 'joined'
    result = service.join(manifest, output)
    assert result['status'] == 'failed' and not output.exists(), result
    assert list(tmp_path.glob('.optimus-*.partial')) == []


def test_split_cancellation_removes_only_unchanged_owned_parts(service, original, tmp_path):
    folder = tmp_path / 'parts'; folder.mkdir()
    unrelated = folder / 'keep'; unrelated.write_text('preserve')
    cancel = threading.Event()
    result = service.split(original, folder, part_size=700, cancel_event=cancel,
                           progress_callback=lambda state: cancel.set())
    assert result['status'] == 'cancelled'
    assert list(folder.iterdir()) == [unrelated]


def test_hardlinked_source_is_excluded_from_read_and_overwrite(service, original, tmp_path):
    alias = tmp_path / 'alias'
    try:
        os.link(original, alias)
    except OSError:
        pytest.skip('Hardlinks unavailable on this fixture filesystem')
    folder = tmp_path / 'parts'; folder.mkdir()
    assert service.split(alias, folder)['status'] == 'failed'
    assert service.shred_plan([str(alias)])['status'] == 'blocked'
    assert original.read_bytes() == alias.read_bytes()


@pytest.mark.parametrize('passes', [1, 3, 7, 35])
def test_selected_file_overwrite_verifies_each_pass_before_exact_removal(service, original, passes):
    size = original.stat().st_size
    plan = service.shred_plan([str(original)], passes=passes)
    assert plan['status'] == 'review', plan
    assert original.exists()
    result = service.shred_apply(plan['plan_id'], confirmed=True)
    assert result['status'] == 'completed', result
    assert result['removed_files'] == 1 and result['written_bytes'] == size * passes
    assert result['items'][0]['passes_completed'] == passes
    assert not original.exists()
    assert 'No secure physical-erasure guarantee' in result['limitation']


def test_overwrite_needs_confirmation_and_preserves_changed_file(service, original):
    plan = service.shred_plan([str(original)])
    assert service.shred_apply(plan['plan_id'])['status'] == 'blocked'
    original.write_bytes(b'a later user choice')
    result = service.shred_apply(plan['plan_id'], confirmed=True)
    assert result['status'] == 'failed'
    assert original.read_bytes() == b'a later user choice'


def test_overwrite_preview_cannot_be_widened_by_caller_editing_display_data(service, original, tmp_path):
    other = tmp_path / 'other'; other.write_text('keep')
    plan = service.shred_plan([str(original)])
    plan['files'][0]['path'] = str(other)
    result = service.shred_apply(plan['plan_id'], confirmed=True)
    assert result['status'] == 'completed'
    assert other.read_text() == 'keep'


def test_cancelled_overwrite_reports_partial_irreversible_change_without_removal(service, tmp_path):
    source = tmp_path / 'large'; source.write_bytes(b'A' * (files._CHUNK + 10))
    plan = service.shred_plan([str(source)])
    cancel = threading.Event()
    result = service.shred_apply(plan['plan_id'], confirmed=True, cancel_event=cancel,
                                progress_callback=lambda state: cancel.set())
    assert result['status'] == 'cancelled' and result['irreversible_partial']
    assert result['removed_files'] == 0 and source.exists()
    assert source.read_bytes()[-10:] == b'A' * 10
    assert result['items'][0]['passes_completed'] == 0
    assert json.loads(Path(result['journal_path']).read_text())['status'] == 'cancelled'


def test_broad_personal_or_optimus_folders_are_excluded_but_exact_document_file_can_be_selected(service, tmp_path):
    documents = tmp_path / 'Documents'; documents.mkdir()
    selected = documents / 'selected'; selected.write_text('explicit')
    assert service.shred_plan([str(documents)])['status'] == 'blocked'
    assert service.shred_plan([str(selected)])['status'] == 'review'
    state_file = service.backend.data_dir / 'private'; state_file.parent.mkdir(exist_ok=True); state_file.write_text('keep')
    assert service.shred_plan([str(state_file)])['status'] == 'blocked'


@pytest.mark.parametrize('layout', ['onedir', 'onefile', 'mac_app'])
def test_frozen_overwrite_protects_distribution_and_modules_without_protecting_all_temp(service, tmp_path, monkeypatch, layout):
    distribution = tmp_path / 'Optimus'
    if layout == 'mac_app':
        distribution = tmp_path / 'Optimus.app'
        executable = distribution / 'Contents' / 'MacOS' / 'Optimus'
        module = distribution / 'Contents' / 'Frameworks' / 'optimus' / 'file_utilities.py'
        sibling = distribution / 'Contents' / 'Resources' / 'asset.dat'
    else:
        executable = distribution / 'Optimus.exe'
        module = ((distribution / '_internal') if layout == 'onedir' else (tmp_path / '_MEIfixture')) / 'optimus' / 'file_utilities.py'
        sibling = distribution / 'assets' / 'asset.dat'
    monkeypatch.setattr(files.sys, 'frozen', True, raising=False)
    monkeypatch.setattr(files.sys, 'executable', str(executable))
    monkeypatch.setattr(files, '__file__', str(module))
    for selected in (executable, sibling, module, module.parent / 'another.py'):
        with pytest.raises(ValueError, match='application'):
            service._shred_allowed(selected, folder=False)
    # A one-file extraction does not make its whole temporary parent an app root.
    service._shred_allowed(tmp_path / 'unrelated-explicit-selection.tmp', folder=False)


def test_source_overwrite_protects_checkout_root(service, tmp_path, monkeypatch):
    checkout = tmp_path / 'checkout'
    monkeypatch.setattr(files.sys, 'frozen', False, raising=False)
    monkeypatch.setattr(files, '__file__', str(checkout / 'src' / 'optimus' / 'file_utilities.py'))
    with pytest.raises(ValueError, match='application'):
        service._shred_allowed(checkout / 'launch.py', folder=False)
    service._shred_allowed(tmp_path / 'explicit-selection.tmp', folder=False)


def test_selected_folder_removes_only_its_reviewed_files_and_empty_directories(service, tmp_path):
    folder = tmp_path / 'selected'; folder.mkdir()
    (folder / 'child').mkdir(); (folder / 'child' / 'one').write_text('one')
    keep = tmp_path / 'keep'; keep.write_text('outside scope')
    plan = service.shred_plan([str(folder)])
    assert plan['status'] == 'review', plan
    result = service.shred_apply(plan['plan_id'], confirmed=True)
    assert result['status'] == 'completed', result
    assert not folder.exists() and keep.read_text() == 'outside scope'


def test_overwrite_plan_is_owner_bound_and_expires(service, original):
    plan = service.shred_plan([str(original)])
    service._plans[plan['plan_id']]['expires'] = 0
    assert service.shred_apply(plan['plan_id'], confirmed=True)['status'] == 'failed'
    assert original.exists()
    plan = service.shred_plan([str(original)])
    service.owner = 'other-owner'
    assert service.shred_apply(plan['plan_id'], confirmed=True)['status'] == 'failed'
    assert original.exists()


def test_empty_split_join_and_exclusive_join_output(service, tmp_path):
    source = tmp_path / 'empty'; source.touch()
    folder = tmp_path / 'parts'; folder.mkdir()
    result = service.split(source, folder)
    assert result['status'] == 'completed' and result['parts'] == 0
    destination = tmp_path / 'joined'
    assert service.join(result['manifest'], destination)['status'] == 'completed'
    assert destination.read_bytes() == b''
    destination.write_bytes(b'keep this choice')
    assert service.join(result['manifest'], destination)['status'] == 'failed'
    assert destination.read_bytes() == b'keep this choice'


def test_link_added_after_overwrite_preview_prevents_mutation(service, original, tmp_path):
    plan = service.shred_plan([str(original)])
    before = original.read_bytes()
    alias = tmp_path / 'alias'
    try:
        os.link(original, alias)
    except OSError:
        pytest.skip('Hardlinks unavailable on this fixture filesystem')
    result = service.shred_apply(plan['plan_id'], confirmed=True)
    assert result['status'] == 'failed' and result['written_bytes'] == 0
    assert alias.read_bytes() == before and original.read_bytes() == before


@CRYPTO
def test_changed_input_is_not_published_as_a_verified_copy(service, tmp_path):
    source = tmp_path / 'input'; source.write_bytes(b'A' * (files._CHUNK + 19))
    destination = tmp_path / 'encrypted'
    changed = [False]
    def progress(_):
        if not changed[0]:
            changed[0] = True
            with source.open('r+b') as stream:
                stream.write(b'B')
            os.utime(source, ns=(source.stat().st_atime_ns, source.stat().st_mtime_ns + 1000000000))
    result = service.encrypt(source, destination, PASSWORD, progress_callback=progress)
    assert result['status'] == 'failed' and not destination.exists()
    assert source.read_bytes()[0:1] == b'B'
    assert list(tmp_path.glob('.optimus-*.partial')) == []


def test_overwrite_final_journal_failure_reports_uncertainty(service, original, monkeypatch):
    plan = service.shred_plan([str(original)])
    real_write = files._private_json
    def write(path, record):
        if record.get('status') == 'completed':
            raise OSError('fixture terminal receipt failure')
        return real_write(path, record)
    monkeypatch.setattr(files, '_private_json', write)
    result = service.shred_apply(plan['plan_id'], confirmed=True)
    assert result['status'] == 'uncertain' and result['irreversible_partial']
    assert Path(result['journal_path']).exists() and not original.exists()


def test_unresolved_recovery_or_installer_receipt_blocks_overwriting(service, original):
    plan = service.shred_plan([str(original)])
    before = original.read_bytes()
    files.pending_updates_barrier.return_value = 'Fixture native operation remains unresolved.'
    result = service.shred_apply(plan['plan_id'], confirmed=True)
    assert result['status'] == 'blocked' and 'unresolved' in result['reason']
    assert original.read_bytes() == before
    assert not (service.backend.data_dir / 'file-utilities').exists()


@pytest.mark.parametrize('fault', ['short_write', 'write_then_error'])
def test_overwrite_handles_partial_native_writes_without_false_byte_claim(service, original, monkeypatch, fault):
    original.write_bytes(b'123456789')
    plan = service.shred_plan([str(original)])
    real_modify = service._modify
    class Proxy:
        def __init__(self, stream):
            self.stream = stream
        def __getattr__(self, name):
            return getattr(self.stream, name)
        def write(self, block):
            if fault == 'write_then_error':
                self.stream.write(block[:1])
                raise OSError('Fixture write outcome is partial.')
            return self.stream.write(block[:2])
    @contextmanager
    def modify(path, expected):
        with real_modify(path, expected) as stream:
            yield Proxy(stream)
    monkeypatch.setattr(service, '_modify', modify)
    result = service.shred_apply(plan['plan_id'], confirmed=True)
    if fault == 'short_write':
        assert result['status'] == 'completed' and result['written_bytes'] == 9
        assert not original.exists()
    else:
        assert result['status'] == 'failed' and result['irreversible_partial']
        assert result['written_bytes'] == 0 and original.exists()


os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QThreadPool
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QWidget
from optimus.widgets.file_utilities_panel import FileUtilitiesPanel


@pytest.fixture
def panel(tmp_path):
    qapp = QApplication.instance() or QApplication([])
    host = QWidget()
    host.backend = SimpleNamespace(**{name: Mock(return_value={'status': 'completed'}) for name in (
        'file_utilities_encrypt', 'file_utilities_decrypt', 'file_utilities_split', 'file_utilities_join',
        'file_utilities_shred_plan', 'file_utilities_shred_apply')})
    host.busy, host.smoke_test = False, False
    host.pool = QThreadPool(host)
    host.pool.setMaxThreadCount(1)
    host._workers = set()
    host._confirm = Mock(return_value=False)
    value = FileUtilitiesPanel(host)
    value.show()
    yield value, qapp
    value.shutdown()
    host.pool.waitForDone(5000)
    qapp.processEvents()
    value.close(); host.close()
    value.deleteLater(); host.deleteLater()
    qapp.processEvents()


def wait_ui(qapp, predicate):
    deadline = time.monotonic() + 5
    while not predicate() and time.monotonic() < deadline:
        qapp.processEvents()
        QTest.qWait(5)
    qapp.processEvents()
    assert predicate()


def test_panel_construction_is_idle_and_overwrite_is_disabled(panel):
    value, _ = panel
    for hook in vars(value.backend).values():
        hook.assert_not_called()
    assert not value.overwrite_button.isEnabled()
    assert value.details.isReadOnly() and value.password.echoMode() == QLineEdit.Password


def test_panel_encrypt_runs_asynchronously_and_releases_password_fields(panel):
    value, qapp = panel
    observed = []
    def encrypt(source, destination, password, **kwargs):
        observed.append(threading.get_ident())
        assert password == PASSWORD
        kwargs['progress_callback']({'percentage': 50, 'description': 'Fixture encryption'})
        return {'status': 'completed', 'output': 'fixture encrypted output'}
    value.backend.file_utilities_encrypt.side_effect = encrypt
    value.crypto_source.setText('fixture source')
    value.crypto_destination.setText('fixture destination')
    value.password.setText(PASSWORD); value.password_confirmation.setText(PASSWORD)
    value.encrypt_button.click()
    assert value.window.busy and not value.password.text() and not value.password_confirmation.text()
    wait_ui(qapp, lambda: not value.window.busy)
    assert observed and observed[0] != threading.get_ident()
    assert PASSWORD not in value.details.toPlainText()
    assert 'fixture encrypted output' in value.details.toPlainText()


def test_panel_requires_review_and_exact_overwrite_confirmation(panel):
    value, qapp = panel
    plan = {'status': 'review', 'plan_id': 'a' * 32, 'file_count': 1, 'bytes': 5, 'passes': 1}
    value.backend.file_utilities_shred_plan.return_value = plan
    value._paths = ['fixture explicitly selected file']
    value.review_button.click()
    wait_ui(qapp, lambda: not value.window.busy)
    assert value.overwrite_button.isEnabled()
    value.overwrite_button.click()
    value.backend.file_utilities_shred_apply.assert_not_called()
    assert value.window._confirm.call_args.kwargs['phrase'] == 'OVERWRITE'
    value.window._confirm.return_value = True
    value.overwrite_button.click()
    wait_ui(qapp, lambda: not value.window.busy)
    args, kwargs = value.backend.file_utilities_shred_apply.call_args
    assert args == ('a' * 32, True)
    assert 'cancel_event' in kwargs and 'progress_callback' in kwargs
    assert not value.overwrite_button.isEnabled()


def test_panel_pass_change_invalidates_review_and_cancel_is_cooperative(panel):
    value, qapp = panel
    value._plan = {'status': 'review', 'plan_id': 'a' * 32}
    value._update()
    value.passes.setCurrentIndex(1)
    assert value._plan is None and not value.overwrite_button.isEnabled()
    began = threading.Event()
    def split(*args, **kwargs):
        began.set()
        assert kwargs['cancel_event'].wait(5)
        return {'status': 'cancelled', 'reason': 'Fixture cancellation'}
    value.backend.file_utilities_split.side_effect = split
    value.split_button.click()
    wait_ui(qapp, began.is_set)
    assert value.cancel_button.isEnabled()
    value.cancel_button.click()
    wait_ui(qapp, lambda: not value.window.busy)
    assert 'Cancelled' in value.status.text()


def test_panel_smoke_mode_blocks_writes_and_missing_hook_is_explicit(panel):
    value, _ = panel
    value.window.smoke_test = True
    value.split_button.click()
    value.backend.file_utilities_split.assert_not_called()
    assert 'smoke test' in value.status.text()
    value.window.smoke_test = False
    del value.backend.file_utilities_join
    value.join_button.click()
    assert 'unavailable' in value.status.text()


from optimus.extended_backend import ExtendedMaintenanceBackend


class ExtendedFixture(ExtendedMaintenanceBackend):
    def __init__(self, directory):
        self.data_dir = directory


@pytest.mark.parametrize('kind', ['create', 'delete'])
def test_extended_restore_wrappers_do_not_inject_unsupported_progress_keyword(tmp_path, kind):
    backend = ExtendedFixture(tmp_path / 'state')
    received = []
    def create(description, confirmed=False, cancel_event=None):
        received.append((description, confirmed, cancel_event.is_set()))
        return {'status': 'deferred', 'reason': 'No new point was created.'}
    def delete(plan, selection, confirmed=False, cancel_event=None):
        received.append((plan, selection, confirmed, cancel_event.is_set()))
        return {'status': 'completed', 'reason': 'Exact deletion verified.'}
    backend._system_restore = SimpleNamespace(create=create, delete=delete)
    if kind == 'create':
        result = backend.system_restore_create('Fixture', confirmed=True, progress_callback=Mock())
        assert received == [('Fixture', True, False)]
        assert backend.worker_status()[0]['status'] == 'needs_review'
    else:
        result = backend.system_restore_delete('a' * 32, 'b' * 32, confirmed=True, progress_callback=Mock())
        assert received == [('a' * 32, 'b' * 32, True, False)]
        assert backend.worker_status()[0]['status'] == 'completed'
    assert result['status'] in ('completed', 'deferred')


def test_extended_worker_stop_combines_with_caller_cancellation_and_drains(tmp_path):
    backend = ExtendedFixture(tmp_path / 'state')
    began, release = threading.Event(), threading.Event()
    received, results = [], []
    def operation(*args, cancel_event=None, progress_callback=None):
        progress_callback({'current': 1, 'total': 2, 'description': 'Fixture active copy'})
        began.set()
        deadline = time.monotonic() + 5
        while not cancel_event.is_set() and time.monotonic() < deadline:
            release.wait(.01)
        received.append(cancel_event.is_set())
        return {'status': 'cancelled', 'reason': 'Fixture acknowledged stop; no force-kill.'}
    backend._file_utilities = SimpleNamespace(split=operation)
    caller = threading.Event()
    worker = threading.Thread(target=lambda: results.append(backend.file_utilities_split('fixture', 'out',
        cancel_event=caller)))
    worker.start()
    try:
        assert began.wait(5)
        assert backend.worker_status()[0]['status'] == 'running'
        assert backend.request_optional_stop() == ['file_tools']
        assert not caller.is_set()
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and received == [True]
    assert results[0]['status'] == 'cancelled'
    snapshot = backend.worker_status()[0]
    assert snapshot['status'] == 'cancelled' and snapshot['terminal']
    assert snapshot['cancellation_requested']


def test_extended_unknown_recovery_outcome_is_never_completed(tmp_path):
    backend = ExtendedFixture(tmp_path / 'state')
    backend._system_restore = SimpleNamespace(create=Mock(return_value={
        'status': 'uncertain', 'reason': 'Fixture Windows outcome remains unresolved.'}))
    result = backend.system_restore_create('Fixture', confirmed=True)
    assert result['status'] == 'uncertain'
    snapshot = backend.worker_status()[0]
    assert snapshot['status'] == 'needs_review' and snapshot['terminal']
    assert 'unresolved' in snapshot['description']


def test_extended_recovery_barrier_preserves_service_refusal(tmp_path):
    backend = ExtendedFixture(tmp_path / 'state')
    path = backend.data_dir / 'recovery' / 'system-restore'; path.mkdir(parents=True)
    backend._system_restore = SimpleNamespace(_pending=Mock(side_effect=ValueError('Fixture prepared restore receipt.')))
    result = backend.recovery_mutation_barrier()
    assert 'Fixture prepared restore receipt' in result
    backend._system_restore._pending.assert_called_once_with()


def test_extended_status_poll_and_service_creation_start_no_work(tmp_path):
    backend = ExtendedFixture(tmp_path / 'state')
    assert backend.worker_status() == []
    assert backend._file_utilities_service() is backend._file_utilities_service()
    assert backend.worker_status() == [] and not backend.data_dir.exists()
