"""Explicit local file tools with exclusive outputs and reviewed erasure.

AES-GCM and Scrypt use the maintained cryptography library, never a homemade
cipher: https://cryptography.io/en/stable/hazmat/primitives/aead/
https://cryptography.io/en/stable/hazmat/primitives/key-derivation-functions/
Overwriting a file does not guarantee erasure on SSDs, snapshots or backups.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
import ctypes
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import struct
import sys
import threading
import time
import uuid

from .automation import RunLease
from .filesystem import _ordinary_data_path
from .parallel_updates import pending_updates_barrier
from .speed_enhancements import _current_owner, _private_json

_CHUNK = 1024 * 1024
_MAGIC = b'OPTIMUS-AESGCM\x01\n'
_MAX_HEADER = 4096
_MAX_PARTS = 10000
_FLAGS = 0x400 | 0x1000 | 0x40000 | 0x400000  # reparse / offline / recall
_ID = re.compile(r'[0-9a-f]{32}\Z')
_LIMITATION = ('Overwrite passes are best effort for ordinary files. SSD wear leveling, copy-on-write, '
    'snapshots, backups and cloud copies can retain data. No secure physical-erasure guarantee is made.')


class Cancelled(RuntimeError):
    pass


def _check(cancel):
    if cancel is not None and cancel.is_set():
        raise Cancelled('The operation was cancelled.')


def _progress(callback, current, total, description):
    if callback:
        callback({'current': current, 'total': total, 'percentage': min(99, int(current * 100 / total)) if total else 0,
                  'description': description})


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate manifest/header fields are invalid.')
        result[key] = value
    return result


def _json(raw):
    return json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Nonfinite manifest data is invalid.')))


def _bytes(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode('utf-8')


def _path(value):
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts or '\x00' in str(path):
        raise ValueError('Select an absolute ordinary local path without parent traversal.')
    if os.name == 'nt' and (str(path).startswith(('\\\\', '//')) or ':' in str(path)[2:]):
        raise ValueError('Network paths and alternate data streams are excluded.')
    return path


def _directory(path):
    path = _path(path)
    _ordinary_data_path(path)
    if not path.is_dir():
        raise ValueError('Select an existing ordinary output directory.')
    for part in (path, *path.parents):
        if getattr(part.lstat(), 'st_file_attributes', 0) & _FLAGS:
            raise ValueError('Cloud, offline and indirect directories are excluded.')
    return path


def _info(path):
    path = _path(path)
    info = _ordinary_data_path(path, file=True)
    if info is None:
        raise FileNotFoundError('The selected ordinary file is unavailable.')
    _directory(path.parent)
    if getattr(info, 'st_file_attributes', 0) & (_FLAGS | 0x4):
        raise ValueError('System, cloud and offline files are excluded.')
    return info


def _stamp(info):
    # Recent Windows Python versions expose different legacy st_ctime meanings
    # through stat versus fstat. Compare stable birthtime there; POSIX ctime is
    # the metadata-change timestamp and remains useful for detecting replacement.
    changed = getattr(info, 'st_birthtime_ns', None) if os.name == 'nt' else info.st_ctime_ns
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, changed


def _same_open(path, stream, before):
    current, opened = _info(path), os.fstat(stream.fileno())
    if _stamp(current) != _stamp(before) or _stamp(opened) != _stamp(before) or opened.st_nlink != 1:
        raise ValueError('The selected file changed while the operation was running.')


@contextmanager
def _source(value):
    path = _path(value)
    before = _info(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        _same_open(path, stream, before)
        yield path, stream, before
        _same_open(path, stream, before)


def _private_windows(path):
    """Restrict the empty staging file before decrypted bytes are written."""
    if os.name != 'nt':
        return
    from ctypes import wintypes
    owner = _current_owner()
    if not isinstance(owner, str) or not re.fullmatch(r'sid:S-1-[0-9-]+', owner):
        raise OSError('The private staging-file owner could not be verified.')
    advapi = ctypes.WinDLL('advapi32.dll', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32.dll', use_last_error=True)
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    convert.restype = wintypes.BOOL
    setter = advapi.SetFileSecurityW
    setter.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    setter.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    sddl = 'D:P(A;;FA;;;' + owner[4:] + ')(A;;FA;;;SY)'
    if not convert(sddl, 1, ctypes.byref(descriptor), None):
        raise OSError('Private staging permissions could not be created.')
    try:
        if not setter(str(path), 0x80000004, descriptor):
            raise OSError('Private staging permissions could not be applied.')
    finally:
        kernel.LocalFree(descriptor)


class _Output:
    def __init__(self, destination):
        self.path = _path(destination)
        self.parent = _directory(self.path.parent)
        self.parent_identity = self.parent.stat().st_dev, self.parent.stat().st_ino
        if _ordinary_data_path(self.path, file=True) is not None:
            raise FileExistsError('An output already exists; choose a new filename.')
        self.temporary = self.parent / ('.optimus-' + uuid.uuid4().hex + '.partial')
        descriptor = os.open(self.temporary, os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, 'O_BINARY', 0), 0o600)
        self.stream = os.fdopen(descriptor, 'w+b')
        self.identity = os.fstat(self.stream.fileno()).st_dev, os.fstat(self.stream.fileno()).st_ino
        self.published = False
        try:
            _private_windows(self.temporary)
        except Exception:
            self.close()
            raise

    def publish(self):
        self.stream.flush()
        os.fsync(self.stream.fileno())
        current_parent = _directory(self.parent).stat()
        if (current_parent.st_dev, current_parent.st_ino) != self.parent_identity:
            raise ValueError('The output directory changed during the operation.')
        info = _info(self.temporary)
        if (info.st_dev, info.st_ino) != self.identity:
            raise ValueError('The private output changed identity.')
        if _ordinary_data_path(self.path, file=True) is not None:
            raise FileExistsError('An output appeared during the operation; it was preserved.')
        self.stream.close()
        # link() is an atomic no-replace publication on supported local file
        # systems. An unsupported filesystem fails instead of overwriting data.
        os.link(self.temporary, self.path)
        self.temporary.unlink()
        self.published = True

    def close(self):
        if not self.stream.closed:
            self.stream.close()
        if self.temporary.exists():
            info = self.temporary.lstat()
            if stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) == self.identity:
                self.temporary.unlink()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _cipher(password, salt):
    if not isinstance(password, str) or not 8 <= len(password) <= 1024:
        raise ValueError('Use a password of 8 to 1024 characters.')
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    except ImportError as error:
        raise RuntimeError('Authenticated encryption requires the cryptography package; no fallback cipher is used.') from error
    key = Scrypt(salt=salt, length=32, n=32768, r=8, p=1).derive(password.encode('utf-8'))
    return AESGCM(key)


def _exact(stream, count):
    value = stream.read(count)
    if len(value) != count:
        raise ValueError('The encrypted file is truncated or incomplete.')
    return value


class FileUtilities:
    def __init__(self, backend, *, owner=None, lease_factory=RunLease):
        self.backend, self.owner, self.lease_factory = backend, owner, lease_factory
        self.lock = threading.Lock()
        self._plans = {}

    def capabilities(self):
        return {'read_only': True, 'encryption_available': importlib.util.find_spec('cryptography') is not None,
                'encryption_format': 'Optimus AES-256-GCM chunk container v1; Scrypt n=32768,r=8,p=1',
                'split_join_available': True, 'selected_file_overwrite_available': True,
                'overwrite_passes': [1, 3, 7, 35], 'free_space_wipe_available': False,
                'whole_drive_wipe_available': False, 'file_recovery_available': False,
                'limitation': _LIMITATION}

    def _owner(self):
        if self.owner is None:
            self.owner = _current_owner()
        return self.owner

    def _run(self, operation, callback):
        if not self.lock.acquire(blocking=False):
            return {'status': 'blocked', 'reason': 'Another file utility operation is active.'}
        try:
            with self.lease_factory(self.backend.data_dir):
                if operation == 'shred':
                    barrier = pending_updates_barrier(self.backend)
                    if barrier:
                        return {'status': 'blocked', 'reason': str(barrier)[:1200], 'operation': operation}
                return callback()
        except Cancelled:
            return {'status': 'cancelled', 'operation': operation, 'reason': (
                'Cancelled before overwriting; no selected file was changed.' if operation == 'shred'
                else 'Cancelled; no completed copy output was published.')}
        except Exception as error:
            # Do not include arguments/passwords/native stack frames in reports.
            return {'status': 'failed', 'operation': operation, 'reason': str(error)[:1200]}
        finally:
            self.lock.release()

    def encrypt(self, source, destination, password, *, cancel_event=None, progress_callback=None):
        def apply():
            _check(cancel_event)
            salt, prefix = os.urandom(16), os.urandom(8)
            cipher = _cipher(password, salt)
            header = _bytes({'schema': 1, 'kdf': 'scrypt-32768-8-1', 'salt': salt.hex(),
                             'nonce_prefix': prefix.hex(), 'chunk_size': _CHUNK})
            digest, count, size = hashlib.sha256(), 0, 0
            with _source(source) as (path, stream, before), _Output(destination) as output:
                output.stream.write(_MAGIC + struct.pack('>I', len(header)) + header)
                bound = hashlib.sha256(header).digest()
                while size < before.st_size:
                    _check(cancel_event)
                    chunk = stream.read(min(_CHUNK, before.st_size - size))
                    if not chunk:
                        raise ValueError('The source ended before its approved file size.')
                    if count >= 0xfffffffe:
                        raise ValueError('The encrypted file exceeds the supported chunk count.')
                    length = struct.pack('>I', len(chunk) + 16)
                    index = struct.pack('>I', count)
                    encrypted = cipher.encrypt(prefix + index, chunk, bound + b'C' + index + length)
                    output.stream.write(b'C' + length + encrypted)
                    digest.update(chunk)
                    count += 1
                    size += len(chunk)
                    _progress(progress_callback, size, before.st_size, 'Encrypting authenticated file chunks')
                manifest = _bytes({'schema': 1, 'chunks': count, 'bytes': size, 'sha256': digest.hexdigest()})
                index, length = struct.pack('>I', count), struct.pack('>I', len(manifest) + 16)
                output.stream.write(b'F' + length + cipher.encrypt(prefix + index, manifest, bound + b'F' + index + length))
                _same_open(path, stream, before)
                _check(cancel_event)
                output.publish()
            return {'status': 'completed', 'operation': 'encrypt', 'output': str(_path(destination)),
                    'bytes': size, 'chunks': count, 'original_preserved': True}
        return self._run('encrypt', apply)

    def decrypt(self, source, destination, password, *, cancel_event=None, progress_callback=None):
        def apply():
            _check(cancel_event)
            with _source(source) as (path, stream, before):
                if _exact(stream, len(_MAGIC)) != _MAGIC:
                    raise ValueError('This file is not an Optimus encrypted container.')
                length = struct.unpack('>I', _exact(stream, 4))[0]
                if not 1 <= length <= _MAX_HEADER:
                    raise ValueError('The encryption header exceeds its supported size.')
                header = _exact(stream, length)
                metadata = _json(header)
                if (not isinstance(metadata, dict) or set(metadata) != {'schema', 'kdf', 'salt', 'nonce_prefix', 'chunk_size'}
                        or type(metadata.get('schema')) is not int or metadata['schema'] != 1
                        or metadata.get('kdf') != 'scrypt-32768-8-1' or type(metadata.get('chunk_size')) is not int
                        or metadata['chunk_size'] != _CHUNK
                        or not isinstance(metadata.get('salt'), str) or not re.fullmatch('[0-9a-f]{32}', metadata['salt'])
                        or not isinstance(metadata.get('nonce_prefix'), str) or not re.fullmatch('[0-9a-f]{16}', metadata['nonce_prefix'])):
                    raise ValueError('The encrypted file has an unsupported or malformed header.')
                cipher = _cipher(password, bytes.fromhex(metadata['salt']))
                prefix, bound = bytes.fromhex(metadata['nonce_prefix']), hashlib.sha256(header).digest()
                count, size, digest = 0, 0, hashlib.sha256()
                with _Output(destination) as output:
                    while True:
                        _check(cancel_event)
                        kind = _exact(stream, 1)
                        raw_length = _exact(stream, 4)
                        length = struct.unpack('>I', raw_length)[0]
                        if kind not in (b'C', b'F') or not 16 <= length <= (_CHUNK + 16 if kind == b'C' else _MAX_HEADER):
                            raise ValueError('An encrypted frame has an invalid type or length.')
                        if length > before.st_size - stream.tell():
                            raise ValueError('The encrypted frame exceeds the scanned input size.')
                        if count >= 0xffffffff:
                            raise ValueError('The encrypted file exceeds the supported chunk count.')
                        index = struct.pack('>I', count)
                        try:
                            plaintext = cipher.decrypt(prefix + index, _exact(stream, length), bound + kind + index + raw_length)
                        except Exception as error:
                            raise ValueError('Authentication failed: the password or encrypted data is incorrect.') from error
                        if kind == b'F':
                            manifest = _json(plaintext)
                            expected = {'schema': 1, 'chunks': count, 'bytes': size, 'sha256': digest.hexdigest()}
                            if manifest != expected or any(type(manifest.get(key)) is not int for key in ('schema', 'chunks', 'bytes')) or stream.read(1):
                                raise ValueError('The authenticated final manifest is missing, changed or inconsistent.')
                            break
                        if not plaintext:
                            raise ValueError('Empty encrypted data chunks are invalid.')
                        output.stream.write(plaintext)
                        digest.update(plaintext)
                        size += len(plaintext)
                        count += 1
                        _progress(progress_callback, stream.tell(), before.st_size, 'Authenticating and decrypting file chunks')
                    _same_open(path, stream, before)
                    _check(cancel_event)
                    output.publish()
            return {'status': 'completed', 'operation': 'decrypt', 'output': str(_path(destination)),
                    'bytes': size, 'chunks': count, 'authenticated': True, 'original_preserved': True}
        return self._run('decrypt', apply)

    def split(self, source, directory, part_size=64 * _CHUNK, *, cancel_event=None, progress_callback=None):
        def apply():
            if type(part_size) is not int or not 1 <= part_size <= 1024 * _CHUNK:
                raise ValueError('Choose a whole-byte part size between 1 byte and 1 GiB.')
            folder = _directory(directory)
            identity, parts, created = uuid.uuid4().hex, [], []
            manifest_path = folder / ('Optimus-' + identity + '.split.json')
            try:
                with _source(source) as (path, stream, before):
                    if (before.st_size + part_size - 1) // part_size > _MAX_PARTS:
                        raise ValueError('The selected part size exceeds the 10000-part limit.')
                    total, digest = 0, hashlib.sha256()
                    while total < before.st_size:
                        _check(cancel_event)
                        target = folder / (identity + f'.part{len(parts):05d}')
                        size, part_digest = 0, hashlib.sha256()
                        with _Output(target) as output:
                            while size < min(part_size, before.st_size - total):
                                _check(cancel_event)
                                block = stream.read(min(_CHUNK, part_size - size, before.st_size - total - size))
                                if not block:
                                    raise ValueError('The source changed or ended before all split bytes were read.')
                                output.stream.write(block)
                                part_digest.update(block)
                                digest.update(block)
                                size += len(block)
                            output.publish()
                        created.append((target, _info(target)))
                        parts.append({'index': len(parts), 'name': target.name, 'bytes': size, 'sha256': part_digest.hexdigest()})
                        total += size
                        _progress(progress_callback, total, before.st_size, 'Writing split parts and checksums')
                    _same_open(path, stream, before)
                    _check(cancel_event)
                    manifest = {'schema': 1, 'kind': 'optimus-split', 'id': identity,
                                'bytes': total, 'sha256': digest.hexdigest(), 'parts': parts}
                    with _Output(manifest_path) as output:
                        output.stream.write(_bytes(manifest))
                        output.publish()
                return {'status': 'completed', 'operation': 'split', 'manifest': str(manifest_path),
                        'parts': len(parts), 'bytes': total, 'original_preserved': True}
            except Exception:
                # Remove only outputs created by this request whose identities
                # remain unchanged. A replacement is preserved for manual review.
                for target, expected in created:
                    try:
                        if _stamp(_info(target)) == _stamp(expected):
                            target.unlink()
                    except (OSError, ValueError):
                        pass
                raise
        return self._run('split', apply)

    def join(self, manifest, destination, *, cancel_event=None, progress_callback=None):
        def apply():
            with _source(manifest) as (manifest_path, source, before):
                if before.st_size > 4 * _CHUNK:
                    raise ValueError('The split manifest exceeds its supported size.')
                value = _json(source.read(4 * _CHUNK + 1))
                if (not isinstance(value, dict) or set(value) != {'schema', 'kind', 'id', 'bytes', 'sha256', 'parts'}
                        or type(value.get('schema')) is not int or value['schema'] != 1
                        or value.get('kind') != 'optimus-split' or not isinstance(value.get('id'), str)
                        or not _ID.fullmatch(value['id']) or type(value.get('bytes')) is not int or value['bytes'] < 0
                        or not isinstance(value.get('sha256'), str) or not re.fullmatch('[0-9a-f]{64}', value['sha256'])
                        or not isinstance(value.get('parts'), list) or len(value['parts']) > _MAX_PARTS):
                    raise ValueError('The split manifest has an invalid schema.')
                parts = value['parts']
                for index, part in enumerate(parts):
                    if (not isinstance(part, dict) or set(part) != {'index', 'name', 'bytes', 'sha256'}
                            or type(part.get('index')) is not int or part['index'] != index
                            or part.get('name') != value['id'] + f'.part{index:05d}'
                            or type(part.get('bytes')) is not int or not 1 <= part['bytes'] <= 1024 * _CHUNK
                            or not isinstance(part.get('sha256'), str) or not re.fullmatch('[0-9a-f]{64}', part['sha256'])):
                        raise ValueError('The split manifest order, names or part metadata changed.')
                if sum(part['bytes'] for part in parts) != value['bytes']:
                    raise ValueError('The split manifest total does not match its parts.')
                total, digest = 0, hashlib.sha256()
                with _Output(destination) as output:
                    for part in parts:
                        _check(cancel_event)
                        part_digest, size = hashlib.sha256(), 0
                        with _source(manifest_path.parent / part['name']) as (_, stream, info):
                            if info.st_size != part['bytes']:
                                raise ValueError('A split part changed size.')
                            while True:
                                _check(cancel_event)
                                block = stream.read(_CHUNK)
                                if not block:
                                    break
                                if size + len(block) > part['bytes']:
                                    raise ValueError('A split part grew during verification.')
                                output.stream.write(block)
                                part_digest.update(block)
                                digest.update(block)
                                size += len(block)
                            if size != part['bytes'] or part_digest.hexdigest() != part['sha256']:
                                raise ValueError('A split part failed SHA-256 verification.')
                        total += size
                        _progress(progress_callback, total, value['bytes'], 'Verifying and joining split parts')
                    if total != value['bytes'] or digest.hexdigest() != value['sha256']:
                        raise ValueError('The joined data failed whole-file SHA-256 verification.')
                    _same_open(manifest_path, source, before)
                    _check(cancel_event)
                    output.publish()
            return {'status': 'completed', 'operation': 'join', 'output': str(_path(destination)),
                    'bytes': total, 'sha256_verified': True, 'parts_preserved': True}
        return self._run('join', apply)

    def _shred_allowed(self, path, *, folder):
        path = _path(path)
        home = Path.home()
        if path == Path(path.anchor) or path == home:
            raise ValueError('Drive roots and the user profile cannot be selected for overwriting.')
        if getattr(sys, 'frozen', False):
            executable = Path(sys.executable)
            # Protect the entire distribution, not only PyInstaller's _internal
            # modules. A macOS distribution owns its complete .app bundle.
            bundle = next((part for part in executable.parents if part.name.lower().endswith('.app')), None)
            application_roots = [bundle or executable.parent, Path(__file__).parent]
        else:
            application_roots = [Path(__file__).parents[2]]
        forbidden = [Path(self.backend.data_dir), *application_roots]
        if os.name == 'nt':
            forbidden += [Path(value) for key in ('SystemRoot', 'ProgramFiles', 'ProgramFiles(x86)', 'ProgramData')
                          if (value := os.environ.get(key))]
        else:
            forbidden += [Path(value) for value in ('/System', '/Library', '/Applications', '/usr', '/etc', '/bin', '/sbin')]
        forbidden += [home / name for name in ('.ssh', '.gnupg', '.aws', '.azure', '.codex')]
        if any(path == root or root in path.parents for root in forbidden):
            raise ValueError('System, application, credential and Optimus data locations are excluded from overwriting.')
        document_roots = self._document_roots()
        if folder and any(path == root or root in path.parents for root in document_roots):
            raise ValueError('Select individual files explicitly in personal document/profile locations; broad folder overwriting is excluded.')

    @staticmethod
    def _document_roots():
        home = Path.home()
        roots = [home / name for name in ('Desktop', 'Documents', 'Downloads', 'Pictures', 'Music', 'Videos', 'AppData', 'Library')]
        if os.name == 'nt':
            import winreg
            # Honor redirected/OneDrive user folders, not just their default
            # profile paths. These reads occur only in an explicit preview/apply.
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                        r'Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders') as key:
                    for name in ('Desktop', 'Personal', 'My Pictures', 'My Music', 'My Video',
                                 'AppData', 'Local AppData', '{374DE290-123F-4565-9164-39C4925E467B}'):
                        try:
                            value, kind = winreg.QueryValueEx(key, name)
                            if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) and isinstance(value, str):
                                candidate = Path(os.path.expandvars(value))
                                if candidate.is_absolute():
                                    roots.append(candidate)
                        except FileNotFoundError:
                            pass
            except FileNotFoundError:
                pass
        return roots

    def shred_plan(self, paths, passes=1, *, cancel_event=None, progress_callback=None):
        if type(passes) is not int or passes not in (1, 3, 7, 35) or not isinstance(paths, list) or not 1 <= len(paths) <= 20:
            return {'status': 'blocked', 'reason': 'Select 1 to 20 exact files/folders and 1, 3, 7 or 35 overwrite passes.'}
        try:
            files, directories, seen, size = [], [], set(), 0
            deadline = time.monotonic() + 30
            for selected in paths:
                selected = _path(selected)
                _ordinary_data_path(selected, file=not selected.is_dir())
                is_folder = selected.is_dir()
                self._shred_allowed(selected, folder=is_folder)
                stack = [selected]
                while stack:
                    _check(cancel_event)
                    if time.monotonic() > deadline:
                        raise ValueError('The overwrite preview exceeded its 30-second bound; select a smaller scope.')
                    path = stack.pop()
                    if path.is_dir():
                        _directory(path)
                        self._shred_allowed(path, folder=True)
                        directories.append({'path': str(path), 'identity': (path.stat().st_dev, path.stat().st_ino)})
                        stack.extend(path.iterdir())
                        if len(stack) + len(files) + len(directories) > 2000:
                            raise ValueError('The selected folder exceeds the preview entry limit.')
                        continue
                    self._shred_allowed(path, folder=False)
                    info = _info(path)
                    if os.name != 'nt' and info.st_uid != os.getuid():
                        raise ValueError('Only files owned by the current user can be overwritten.')
                    if getattr(info, 'st_file_attributes', 0) & (0x1 | 0x200 | 0x800 | 0x4000):
                        raise ValueError('Read-only, sparse, compressed and EFS files are excluded from overwriting.')
                    key = os.path.normcase(str(path))
                    if key in seen:
                        continue
                    seen.add(key)
                    files.append({'path': str(path), 'identity': _stamp(info), 'bytes': info.st_size})
                    size += info.st_size
                    if len(files) > 1000 or size > 8 * 1024**3 or size * passes > 64 * 1024**3:
                        raise ValueError('The overwrite scope exceeds 1000 files, 8 GiB, or 64 GiB of write passes.')
                    _progress(progress_callback, len(files), 1000, 'Reviewing explicitly selected ordinary files')
            identity = uuid.uuid4().hex
            now = time.monotonic()
            self._plans = {key: value for key, value in self._plans.items() if value['expires'] > now}
            if len(self._plans) >= 10:
                self._plans.pop(next(iter(self._plans)))
            self._plans[identity] = {'owner': self._owner(), 'expires': now + 600, 'files': files,
                                     'directories': directories, 'passes': passes, 'bytes': size}
            return {'status': 'review', 'plan_id': identity, 'files': copy.deepcopy(files),
                    'file_count': len(files), 'bytes': size, 'passes': passes, 'limitation': _LIMITATION,
                    'warning': 'Selected files will be permanently overwritten and removed. Cancellation can leave partially overwritten files.'}
        except Cancelled:
            return {'status': 'cancelled', 'reason': 'Overwrite review cancelled; no file was changed.'}
        except Exception as error:
            return {'status': 'blocked', 'reason': str(error)[:1200]}

    @contextmanager
    def _modify(self, path, expected):
        if _stamp(_info(path)) != tuple(expected):
            raise ValueError('A selected file changed after preview.')
        if os.name == 'nt':
            from ctypes import wintypes
            import msvcrt
            kernel = ctypes.WinDLL('kernel32.dll', use_last_error=True)
            create = kernel.CreateFileW
            create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                               wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
            create.restype = wintypes.HANDLE
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = create(str(path), 0xc0000000, 0, None, 3, 0x00200000, None)
            if handle == ctypes.c_void_p(-1).value:
                raise OSError('The selected file is locked or cannot be opened exclusively.')
            try:
                descriptor = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
            except Exception:
                kernel.CloseHandle(handle)
                raise
        else:
            descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        with os.fdopen(descriptor, 'r+b', buffering=0) as stream:
            opened = os.fstat(stream.fileno())
            if _stamp(opened) != tuple(expected) or opened.st_nlink != 1 or not stat.S_ISREG(opened.st_mode):
                raise ValueError('The opened file no longer matches the approved preview.')
            if os.name != 'nt':
                import fcntl
                if opened.st_uid != os.getuid():
                    raise ValueError('The opened file is no longer owned by the current user.')
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield stream

    def shred_apply(self, plan_id, confirmed=False, *, cancel_event=None, progress_callback=None):
        if confirmed is not True:
            return {'status': 'blocked', 'reason': 'Explicit confirmation of the exact overwrite preview is required.'}
        def apply():
            if not isinstance(plan_id, str) or not _ID.fullmatch(plan_id):
                raise ValueError('Choose one exact current overwrite plan.')
            plan = self._plans.pop(plan_id, None)
            if plan is None or plan['owner'] != self._owner() or time.monotonic() >= plan['expires']:
                raise ValueError('The overwrite preview expired or belongs to another owner; review it again.')
            _check(cancel_event)
            directory = Path(self.backend.data_dir) / 'file-utilities'
            _ordinary_data_path(directory)
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            journal = directory / (uuid.uuid4().hex + '.json')
            record = {'schema': 1, 'owner': self._owner(), 'plan_id': plan_id, 'status': 'prepared',
                      'passes': plan['passes'], 'files': plan['files'], 'items': [], 'limitation': _LIMITATION}
            _ordinary_data_path(journal, file=True)
            _private_json(journal, record)
            written, removed, write_started = 0, 0, False
            deadline = time.monotonic() + 3600
            try:
                for row in plan['files']:
                    _check(cancel_event)
                    path = Path(row['path'])
                    self._shred_allowed(path, folder=False)
                    item = {'path': row['path'], 'status': 'overwriting', 'passes_completed': 0}
                    record['items'].append(item)
                    _private_json(journal, record)
                    with self._modify(path, row['identity']) as stream:
                        for current_pass in range(plan['passes']):
                            expected_hash = hashlib.sha256()
                            stream.seek(0)
                            remaining = row['bytes']
                            while remaining:
                                _check(cancel_event)
                                if time.monotonic() >= deadline:
                                    raise Cancelled('The overwrite operation reached its one-hour bound.')
                                opened = os.fstat(stream.fileno())
                                if opened.st_nlink != 1 or opened.st_size != row['bytes']:
                                    raise ValueError('The selected file changed links or size during overwriting.')
                                amount = min(_CHUNK, remaining)
                                block = (os.urandom(amount) if current_pass == plan['passes'] - 1 or current_pass % 3 == 2
                                         else bytes([0xff if current_pass % 3 else 0]) * amount)
                                offset = 0
                                while offset < len(block):
                                    _check(cancel_event)
                                    write_started = True
                                    count = stream.write(memoryview(block)[offset:])
                                    if type(count) is not int or not 0 < count <= len(block) - offset:
                                        raise OSError('The overwrite did not acknowledge its written bytes.')
                                    expected_hash.update(block[offset:offset + count])
                                    written += count
                                    remaining -= count
                                    offset += count
                                _progress(progress_callback, written, plan['bytes'] * plan['passes'], 'Overwriting explicitly selected files')
                            stream.flush()
                            os.fsync(stream.fileno())
                            stream.seek(0)
                            checked_hash = hashlib.sha256()
                            readback_remaining = row['bytes']
                            while readback_remaining:
                                _check(cancel_event)
                                if time.monotonic() >= deadline:
                                    raise Cancelled('The overwrite operation reached its one-hour bound.')
                                block = stream.read(min(_CHUNK, readback_remaining))
                                if not block:
                                    raise ValueError('The overwritten file changed size during readback.')
                                checked_hash.update(block)
                                readback_remaining -= len(block)
                            if stream.read(1):
                                raise ValueError('The overwritten file grew during readback.')
                            if checked_hash.digest() != expected_hash.digest():
                                raise ValueError('The overwritten bytes failed readback verification.')
                            item['passes_completed'] = current_pass + 1
                        after = os.fstat(stream.fileno())
                    _check(cancel_event)
                    from .cleaner import Cleaner
                    Cleaner._delete_exact({'path': str(path), 'root': str(path.parent), 'size': after.st_size,
                        'modified_ns': after.st_mtime_ns, 'inode': after.st_ino, 'device': after.st_dev})
                    item['status'] = 'removed'
                    removed += 1
                    _private_json(journal, record)
                for row in sorted(plan['directories'], key=lambda value: len(Path(value['path']).parts), reverse=True):
                    _check(cancel_event)
                    path = _directory(row['path'])
                    info = path.stat()
                    if (info.st_dev, info.st_ino) == tuple(row['identity']):
                        try:
                            path.rmdir()  # Empty reviewed directories only; no recursive delete.
                        except OSError:
                            pass
                record['status'] = 'completed'
            except Cancelled as error:
                record.update(status='cancelled', reason=str(error), irreversible_partial=write_started or removed > 0)
            except Exception as error:
                record.update(status='failed', reason=str(error)[:1200], irreversible_partial=write_started or removed > 0)
            record.update(removed_files=removed, written_bytes=written)
            try:
                _ordinary_data_path(journal, file=True)
                _private_json(journal, record)
            except Exception:
                return {'status': 'uncertain', 'reason': 'The final overwrite receipt could not be saved; inspect the prepared journal.',
                        'journal_path': str(journal), 'irreversible_partial': write_started or removed > 0, 'limitation': _LIMITATION}
            return dict(record, journal_path=str(journal))
        return self._run('shred', apply)
