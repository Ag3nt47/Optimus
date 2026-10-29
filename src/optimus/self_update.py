"""Explicit GitHub self-update, managed side-by-side publication and one-shot resume.

Constructors perform no IO/network. Only stable releases of Ag3nt47/Optimus are
accepted; GitHub asset digests AND the separately digest-checked SHA256SUMS must
agree. Windows managed installations can update without overwriting a running
release. Other published platforms get an honest manual-download route.

https://docs.github.com/rest/releases/releases#get-the-latest-release
https://docs.github.com/rest/releases/assets#get-a-release-asset
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import math
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import struct
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

from .automation import CURRENT_SETUP_VERSION, RunLease
from .tls_context import create_update_tls_context
from .filesystem import _ordinary_data_path, resolve_data_root
from .speed_enhancements import _current_owner

REPOSITORY = 'Ag3nt47/Optimus'
API = 'https://api.github.com/repos/' + REPOSITORY
WEB = 'https://github.com/' + REPOSITORY
ASSETS = frozenset(('Optimus-Windows-x64.zip', 'Optimus-macOS-arm64.zip', 'Optimus-macOS-x64.zip',
                    'Optimus-Linux-x86_64.tar.gz', 'Optimus-Source.zip'))
_SHA = re.compile(r'[a-f0-9]{64}\Z')
_ID = re.compile(r'[a-f0-9]{32}\Z')
_VERSION = re.compile(r'(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\Z')
_MAX_DOWNLOAD = 512 * 1024 ** 2
_MAX_EXPANDED = 2 * 1024 ** 3
_MAX_FILES = 20000
_RESERVE = 2 * 1024 ** 3
_TTL = 1800


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()


def _version(value):
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        raise ValueError('Only stable three-part Optimus release versions are supported.')
    return tuple(map(int, value.split('.')))


def asset_name(system=None, machine=None):
    system, machine = system or sys.platform, (machine or platform.machine()).casefold()
    values = {('win32', 'amd64'): 'Optimus-Windows-x64.zip', ('win32', 'x86_64'): 'Optimus-Windows-x64.zip',
              ('darwin', 'arm64'): 'Optimus-macOS-arm64.zip', ('darwin', 'aarch64'): 'Optimus-macOS-arm64.zip',
              ('darwin', 'x86_64'): 'Optimus-macOS-x64.zip', ('darwin', 'amd64'): 'Optimus-macOS-x64.zip',
              ('linux', 'x86_64'): 'Optimus-Linux-x86_64.tar.gz', ('linux', 'amd64'): 'Optimus-Linux-x86_64.tar.gz'}
    if (system, machine) not in values:
        raise ValueError('No official Optimus download is available for this OS/architecture.')
    return values[system, machine]


def _check(event, deadline=None):
    if event is not None and event.is_set():
        raise RuntimeError('Self-update was cancelled; no cleanup approval was changed.')
    if deadline is not None and time.monotonic() >= deadline:
        raise RuntimeError('Self-update exceeded its bounded operation time.')


def _read(path, maximum=8 * 1024 ** 2):
    path = Path(path)
    before = _ordinary_data_path(path, file=True)
    if before is None or before.st_size > maximum:
        raise ValueError('An ordinary bounded update evidence file is required.')
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        raw = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
    final = _ordinary_data_path(path, file=True)
    identity = before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns
    if len(raw) > maximum or final is None or any((row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns) != identity for row in (opened, after, final)):
        raise ValueError('Update evidence changed while reading.')
    return raw


def _hash_file(path, maximum=_MAX_DOWNLOAD, event=None, deadline=None):
    path = Path(path)
    before = _ordinary_data_path(path, file=True)
    if before is None or before.st_size > maximum:
        raise ValueError('An update file is missing or exceeds its size limit.')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        while True:
            _check(event, deadline)
            block = stream.read(1024 ** 2)
            if not block:
                break
            digest.update(block)
        after = os.fstat(stream.fileno())
    final = _ordinary_data_path(path, file=True)
    identity = before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns
    if final is None or any((row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns) != identity for row in (opened, after, final)):
        raise ValueError('An update file changed while hashing.')
    return {'size': before.st_size, 'sha256': digest.hexdigest()}


def _new(path, raw):
    path = Path(path)
    _ordinary_data_path(path, file=True)
    descriptor = os.open(path,os.O_WRONLY | os.O_CREAT | os.O_EXCL,0o600)
    with os.fdopen(descriptor,'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    if _read(path, max(len(raw), 1)) != raw:
        raise OSError('Self-update evidence failed readback.')


def _tls(url):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443)
            or parsed.hostname not in ('api.github.com', 'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com')):
        raise ValueError('Only approved HTTPS GitHub release endpoints are allowed.')


class _Redirects(urllib.request.HTTPRedirectHandler):
    max_redirections = 5
    def redirect_request(self, request, response, code, message, headers, url):
        _tls(url)
        return super().redirect_request(request, response, code, message, headers, url)


class GitHubTransport:
    """Unauthenticated public requests; no signed redirect URL is persisted."""
    def _open(self, url, accept):
        _tls(url)
        opener = urllib.request.build_opener(_Redirects(), urllib.request.HTTPSHandler(context=create_update_tls_context()))
        request = urllib.request.Request(url, headers={'User-Agent': 'Optimus-Self-Update', 'Accept': accept,
            'X-GitHub-Api-Version': '2022-11-28', 'Accept-Encoding': 'identity'})
        return opener.open(request, timeout=15)

    def get_json(self, url, *, cancel_event=None):
        deadline = time.monotonic() + 30
        chunks, total = [], 0
        _check(cancel_event,deadline)
        with self._open(url, 'application/vnd.github+json') as response:
            _tls(response.geturl())
            while True:
                _check(cancel_event,deadline)
                # HTTPResponse.read1 performs at most one buffered/raw read;
                # a trickling response cannot hold one read until a large
                # requested byte count fills while resetting socket timeouts.
                block = response.read1(65536)
                _check(cancel_event,deadline)
                if not block: break
                total += len(block)
                if total > 2 * 1024 ** 2: raise ValueError('GitHub release metadata exceeds its limit.')
                chunks.append(block)
        return json.loads(b''.join(chunks))

    def download(self, asset, destination, *, cancel_event=None, progress_callback=None):
        deadline, total = time.monotonic() + 600, 0
        digest = hashlib.sha256()
        with self._open(asset['api_url'], 'application/octet-stream') as response, Path(destination).open('xb') as stream:
            _tls(response.geturl())
            while True:
                _check(cancel_event, deadline)
                block = response.read1(1024 ** 2)
                if not block:
                    break
                total += len(block)
                if total > asset['size'] or total > _MAX_DOWNLOAD:
                    raise ValueError('The release download exceeds its exact approved size.')
                stream.write(block); digest.update(block)
                if progress_callback:
                    progress_callback({'phase': 'download', 'description': 'Downloading ' + asset['name'],
                                       'received': total, 'total': asset['size'], 'bytes_received':total,
                                       'bytes_total':asset['size'], 'percentage': int(total * 100 / asset['size'])})
            stream.flush(); os.fsync(stream.fileno())
        if total != asset['size'] or digest.hexdigest() != asset['sha256']:
            raise ValueError('The downloaded asset differs from its approved GitHub digest or size.')


def release_offer(release, current_version, name):
    if not isinstance(release, dict) or release.get('draft') is not False or release.get('prerelease') is not False:
        raise ValueError('A published stable public release is required.')
    identity = release.get('id')
    if type(identity) is not int or identity <= 0 or release.get('url') != API + '/releases/' + str(identity):
        raise ValueError('The public release repository identity differs.')
    tag = release.get('tag_name')
    if not isinstance(tag, str) or not tag.startswith('v'):
        raise ValueError('A stable Optimus version tag is required.')
    version = tag[1:]; _version(version)
    if release.get('html_url') != WEB + '/releases/tag/' + tag or not release.get('published_at'):
        raise ValueError('The published release location differs.')
    if _version(version) <= _version(current_version):
        return None
    assets = release.get('assets')
    if not isinstance(assets, list) or len(assets) > 20:
        raise ValueError('The release asset list is incomplete or exceeds its limit.')
    selected = {}
    for expected in (name, 'SHA256SUMS.txt'):
        matches = [row for row in assets if isinstance(row, dict) and row.get('name') == expected]
        if len(matches) != 1:
            raise ValueError('The platform download/checksum asset is missing or ambiguous.')
        row = matches[0]; asset_id = row.get('id'); size = row.get('size'); digest = row.get('digest')
        if (type(asset_id) is not int or asset_id <= 0 or type(size) is not int or not 0 < size <= (_MAX_DOWNLOAD if expected == name else 65536)
                or row.get('state') != 'uploaded' or not isinstance(digest, str) or not digest.startswith('sha256:') or not _SHA.fullmatch(digest[7:])
                or row.get('url') != API + '/releases/assets/' + str(asset_id)
                or row.get('browser_download_url') != WEB + '/releases/download/' + tag + '/' + expected):
            raise ValueError('The exact public asset ID/digest/size/download binding is invalid.')
        selected[expected] = {'id': asset_id, 'name': expected, 'size': size, 'sha256': digest[7:],
                              'api_url': row['url'], 'download_url': row['browser_download_url']}
    return {'repository': REPOSITORY, 'release_id': identity, 'version': version, 'tag': tag,
            'release_url': release['html_url'], 'asset': selected[name], 'checksums': selected['SHA256SUMS.txt']}


def _checksums(raw):
    if len(raw) > 65536:
        raise ValueError('The checksum manifest exceeds its limit.')
    result = {}
    for line in raw.decode('ascii').splitlines():
        match = re.fullmatch(r'([a-f0-9]{64})  ([A-Za-z0-9_.-]+)', line)
        if not match or match[2] not in ASSETS or match[2] in result:
            raise ValueError('The official checksum manifest contains an invalid or duplicate entry.')
        result[match[2]] = match[1]
    if set(result) != ASSETS:
        raise ValueError('The official checksum manifest does not cover all native/source downloads.')
    return result


def _safe_name(name):
    if (not isinstance(name, str) or not name or '\\' in name or any(ord(c) < 32 for c in name)
            or any(c in name for c in ':<>"|?*') or name.startswith('/') or len(name) > 1024):
        raise ValueError('An unsafe release archive path was refused.')
    parts = PurePosixPath(name).parts
    if (not parts or parts[0] != 'Optimus' or len(parts) < 2 or any(part in ('', '.', '..') or part.endswith((' ', '.'))
            or re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', part, re.I) for part in parts[1:])):
        raise ValueError('The release archive must contain ordinary files below Optimus/.')
    return Path(*parts[1:])


def _zip_directory_bound(archive):
    # Check EOCD BEFORE ZipFile allocates/parses the entire central directory.
    # Official native bundles fit ordinary single-volume ZIP limits; oversized
    # ZIP64/multi-volume archives require a manual install instead.
    with Path(archive).open('rb') as stream:
        stream.seek(0,2); length=stream.tell()
        stream.seek(max(0,length-65557)); tail=stream.read(65557)
    offset=tail.rfind(b'PK\x05\x06')
    if offset<0 or len(tail)-offset<22: raise ValueError('The native ZIP directory is missing.')
    _,disk,central_disk,local_count,count,size,start,comment=struct.unpack_from('<4s4H2LH',tail,offset)
    if (disk or central_disk or local_count!=count or count>_MAX_FILES or count==65535
            or size>32*1024**2 or start==0xFFFFFFFF or len(tail)-offset!=22+comment
            or start+size>length-(len(tail)-offset)):
        raise ValueError('The native archive directory exceeds safe single-volume bounds.')


def extract_windows(archive, destination, *, cancel_event=None, progress_callback=None):
    deadline = time.monotonic() + 600
    destination = Path(destination); _ordinary_data_path(destination)
    _zip_directory_bound(archive)
    with zipfile.ZipFile(archive) as source:
        rows = source.infolist()
        if len(rows) > _MAX_FILES or sum(row.file_size for row in rows) > _MAX_EXPANDED:
            raise ValueError('The native archive exceeds its extraction bounds.')
        seen, members, total = set(), [], 0
        for row in rows:
            # ZipInfo normalizes backslashes on Windows. Inspect the original
            # central-directory spelling as well, before that normalization.
            original_name = row.orig_filename
            if original_name in ('Optimus/', 'Optimus') and row.is_dir():
                continue
            path = _safe_name(original_name.rstrip('/'))
            key = path.as_posix().casefold()
            mode = row.external_attr >> 16
            if (key in seen or row.flag_bits & 1 or stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)
                    or row.file_size > _MAX_DOWNLOAD or row.file_size > max(1, row.compress_size) * 2000
                    or path.parts[0].casefold() in ('data', 'optimus.location.json')):
                raise ValueError('A duplicate, indirect, encrypted, oversized or machine-specific archive entry was refused.')
            seen.add(key); members.append((row, path))
        if shutil.disk_usage(destination).free < sum(row.file_size for row, _ in members) + _RESERVE:
            raise OSError('Insufficient destination space for the verified native release and 2 GiB buffer.')
        for index, (row, relative) in enumerate(members):
            _check(cancel_event, deadline)
            target = destination / relative
            if row.is_dir():
                target.mkdir(parents=True, exist_ok=True, mode=0o700); _ordinary_data_path(target)
                continue
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700); _ordinary_data_path(target.parent)
            written = 0
            with source.open(row) as origin, target.open('xb') as output:
                while True:
                    _check(cancel_event, deadline)
                    block = origin.read(1024 ** 2)
                    if not block: break
                    written += len(block); total += len(block)
                    if written > row.file_size or total > _MAX_EXPANDED: raise ValueError('Archive expansion exceeded its verified bounds.')
                    output.write(block)
                output.flush(); os.fsync(output.fileno())
            if written != row.file_size: raise ValueError('A native archive member was truncated.')
            if progress_callback: progress_callback({'phase':'extract','description':'Verifying native release files', 'current':index + 1,'total':len(members)})


def _tree(directory, event=None):
    directory = Path(directory); _ordinary_data_path(directory)
    result, total, deadline = [], 0, time.monotonic() + 120
    def fail(error): raise error
    for current, folders, names in os.walk(directory, followlinks=False, onerror=fail):
        _ordinary_data_path(Path(current))
        for folder in folders: _ordinary_data_path(Path(current) / folder)
        for name in sorted(names):
            path = Path(current) / name
            value = _hash_file(path, event=event,deadline=deadline)
            total += value['size']; result.append(dict(path=path.relative_to(directory).as_posix(), **value))
            if len(result) > _MAX_FILES or total > _MAX_EXPANDED: raise ValueError('The staged release tree exceeds its limit.')
    return sorted(result, key=lambda row: row['path'])


def _windows_executable(path):
    before = _ordinary_data_path(Path(path),file=True)
    if before is None or before.st_size < 128: raise ValueError('A native x64 Windows executable is required.')
    with Path(path).open('rb') as stream:
        header = stream.read(64)
        if header[:2] != b'MZ': raise ValueError('The release executable is not a Windows PE image.')
        offset = struct.unpack_from('<I',header,60)[0]
        if not 64 <= offset <= min(before.st_size - 6,1024 ** 2): raise ValueError('The executable PE header is invalid.')
        stream.seek(offset); signature = stream.read(6)
    if signature != b'PE\0\0\x64\x86': raise ValueError('The release executable does not target Windows x64.')


class NativeUpdateProcess:
    def __init__(self):
        self._children = {}

    def current(self):
        import psutil
        process = psutil.Process()
        return {'pid': process.pid, 'created_at': process.create_time(), 'exe': process.exe()}

    def alive(self, identity):
        import psutil
        try:
            process = psutil.Process(identity['pid'])
            return process.create_time() == identity['created_at'] and Path(process.exe()) == Path(identity['exe'])
        except psutil.NoSuchProcess:
            return False

    def other_instances(self):
        import psutil
        return [p.pid for p in psutil.process_iter(['name']) if p.pid != os.getpid() and (p.info['name'] or '').casefold() == 'optimus.exe']

    def launch(self, executable, arguments, *, background=False):
        if any(os.environ.get(name) for name in ('OPTIMUS_SOURCE_WORKSPACE', 'OPTIMUS_ORIGINAL_SOURCE')):
            raise ValueError('Editable-source overrides cannot participate in managed self-update.')
        environment = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT='1')
        process = subprocess.Popen([str(executable), *arguments], cwd=str(Path(executable).parent), shell=False,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) if background else 0, env=environment)
        code = process.poll()
        if type(code) is int:
            return {'started': False, 'pid': process.pid, 'exit_code': code}
        self._children[process.pid] = process
        return {'started': True, 'pid': process.pid}

    def exit_code(self, pid):
        child = self._children.get(pid)
        return child.poll() if child is not None else None


class SelfUpdater:
    def __init__(self, backend, *, installation=None, current_version=None, transport=None, native=None,
                 system=None, machine=None, owner=None, clock=None, sleep=None, lease_factory=RunLease, launchers=None,key_codec=None,
                 directory_policy=None):
        from . import __version__
        self.backend, self.data_dir = backend, Path(backend.data_dir)
        self.installation = Path(installation) if installation is not None else Path(sys.executable).parent
        self.current_version = current_version or __version__
        self.system, self.machine = system or sys.platform, machine or platform.machine()
        self.transport, self.native = transport, native
        self.owner, self.clock, self.sleep = owner, clock or time.time, sleep or time.sleep
        self.lease_factory, self.launchers = lease_factory, launchers
        self.key_codec = key_codec
        self.directory_policy = directory_policy
        self._authenticated_workspace = None
        self.directory = self.data_dir / 'self-update'

    def _transport(self):
        if self.transport is None: self.transport = GitHubTransport()
        return self.transport

    def _native(self):
        if self.native is None: self.native = NativeUpdateProcess()
        return self.native

    def _owner(self):
        if self.owner is None: self.owner = _current_owner()
        return self.owner

    def _managed(self):
        if any(os.environ.get(name) for name in ('OPTIMUS_SOURCE_WORKSPACE','OPTIMUS_ORIGINAL_SOURCE')):
            raise ValueError('An editable-source session cannot authorize a managed self-update.')
        if (self.system != 'win32' or asset_name(self.system, self.machine) != 'Optimus-Windows-x64.zip'
                or self.installation.name != self.current_version or self.installation.parent.name != 'Releases'
                or self.installation.parent.parent != self.data_dir.parent or self.data_dir.name != 'Data'):
            raise ValueError('Automatic installation requires the existing ordinary managed Releases/version + Data layout. Use the explicit platform download for this installation.')
        _version(self.current_version)
        if resolve_data_root(self.data_dir) != self.data_dir: raise ValueError('The managed data authority changed.')
        _ordinary_data_path(self.installation)
        return self.installation.parent.parent

    def _binding(self):
        self._managed()
        locator_raw = _read(self.installation / 'Optimus.location.json', 4096)
        locator = json.loads(locator_raw)
        if (not isinstance(locator,dict) or set(locator) != {'schema','data_dir'}
                or type(locator.get('schema')) is not int or locator['schema'] != 1
                or not isinstance(locator.get('data_dir'),str) or Path(locator['data_dir']) != self.data_dir):
            raise ValueError('The managed data locator differs.')
        raw = _read(self.data_dir / 'preferences.json', 4 * 1024 ** 2)
        settings = json.loads(raw)
        if not isinstance(settings, dict): raise ValueError('Saved settings must remain an object.')
        executable = self.installation / 'Optimus.exe'
        return {'owner':self._owner(),'data_dir':str(self.data_dir),'old_version':self.current_version,
                'old_executable':str(executable),'old_executable_sha256':_hash_file(executable)['sha256'],
                'preferences_sha256':_digest(raw),'locator_sha256':_digest(locator_raw)}, raw, locator_raw

    def _key(self, create=False):
        path = self.directory / 'owner.key'
        _ordinary_data_path(self.directory)
        if create:
            if not self.directory.exists():
                try:
                    self._create_private_directory(self.directory)
                except FileExistsError:
                    pass
            try: _new(path, self._key_bytes(os.urandom(32)))
            except FileExistsError: pass
        raw = self._key_bytes(_read(path,65536),decrypt=True)
        if os.name != 'nt' and (path.stat().st_uid != os.getuid() or stat.S_IMODE(path.stat().st_mode) & 0o077):
            raise ValueError('The self-update owner key is not private to this account.')
        if len(raw) != 32: raise ValueError('The self-update owner key is invalid.')
        if os.name == 'nt':
            info = self.directory.lstat()
            identity = (info.st_dev, info.st_ino, _digest(raw), self._owner())
            if identity != self._authenticated_workspace:
                # Only authenticated current-user DPAPI material may repair the
                # exact legacy OWNER RIGHTS ACL. Private evidence is not reset.
                self._directory_policy().repair_legacy_private_directory(
                    self.directory, self._owner(), authenticated_owner=self._owner())
                self._authenticated_workspace = identity
        return raw

    def _directory_policy(self):
        if self.directory_policy is None:
            from . import self_update_permissions
            self.directory_policy = self_update_permissions
        return self.directory_policy

    def _create_private_directory(self, path):
        return self._directory_policy().create_private_directory(path, self._owner())

    def _key_bytes(self,raw,decrypt=False):
        if self.key_codec is not None: return self.key_codec(raw,decrypt)
        if os.name == 'nt':
            # User-scoped DPAPI, UI forbidden; no machine-wide protection and
            # no extra elevation. The same SID can resume across normal UAC.
            from .cloud_drive import DPAPITokenStore
            return DPAPITokenStore._crypt(raw,decrypt)
        return raw

    def _write(self, folder, name, value):
        value = dict(value)
        value['record_type'] = name
        raw = _json(value)
        if len(raw) > 8 * 1024 ** 2: raise ValueError('Self-update evidence exceeds its limit.')
        _new(folder / name, _json({'value':value,'signature':hmac.new(self._key(),raw,hashlib.sha256).hexdigest()}))

    def _load(self, identity, name='stage.json', *, allow_expired=False):
        if not isinstance(identity, str) or not _ID.fullmatch(identity): raise ValueError('Invalid private self-update identity.')
        folder = self.directory / identity
        value = json.loads(_read(folder / name))
        payload, signature = value.get('value'), value.get('signature')
        if (not isinstance(payload,dict) or not isinstance(signature,str)
                or not hmac.compare_digest(signature,hmac.new(self._key(),_json(payload),hashlib.sha256).hexdigest())):
            raise ValueError('Self-update consent/evidence authentication failed.')
        if payload.get('id') != identity or payload.get('owner') != self._owner() or payload.get('record_type') != name:
            raise ValueError('The self-update record belongs to another owner, operation or phase.')
        if (type(payload.get('expires_at')) not in (int,float) or not math.isfinite(payload['expires_at'])
                or (not allow_expired and not self.clock() <= payload['expires_at'])):
            raise ValueError('The one-shot self-update approval expired.')
        return payload, folder

    def _not_revoked(self,identity,folder):
        if (folder / 'revoked.json').exists():
            self._load(identity,'revoked.json')
            raise RuntimeError('This self-update or cleanup-resume approval was cancelled.')

    def cancel_handoff(self,handoff_id):
        # Expiration revokes execution authority, but must not prevent closing a
        # cancelled update. Only this revoke-only path accepts expired evidence;
        # identity/owner/phase/HMAC authentication is still mandatory.
        value,folder = self._load(handoff_id,allow_expired=True)
        if (folder / 'revoked.json').exists():
            self._load(handoff_id,'revoked.json',allow_expired=True)
        else:
            self._write(folder,'revoked.json',{'id':handoff_id,'owner':self._owner(),'expires_at':value['expires_at'],
                'revoked_at':self.clock(),'cleanup_approved':False,'reason':'The user cancelled self-update or cleanup resume.'})
        return {'status':'cancelled','revoked':True,'cleanup_approved':False}

    def _check_workspace_access(self):
        """Read-only UX preflight; staging still verifies every write and key."""
        try:
            _ordinary_data_path(self.directory)
            try: self.directory.lstat()
            except FileNotFoundError: return
            with os.scandir(self.directory) as entries:
                next(entries, None)
        except PermissionError as error:
            raise PermissionError('The current Windows privilege context cannot read the existing private update folder. '
                'Use the same administrator level as the earlier update, or the verified manual download. '
                'Existing update records are preserved.') from error

    def check(self, *, cancel_event=None):
        try:
            name = asset_name(self.system,self.machine)
            value = release_offer(self._transport().get_json(API + '/releases/latest',cancel_event=cancel_event), self.current_version,name)
            if value is None: return {'status':'up_to_date','current_version':self.current_version,'offer':None}
            try: self._binding(); self._check_workspace_access(); automatic = True; reason = ''
            except (ValueError,OSError) as error: automatic = False; reason = str(error)
            return {'status':'available','offer':value,'automatic_install_supported':automatic,'reason':reason,
                    'current_version':self.current_version,'target_version':value['version'],'release_url':value['release_url']}
        except Exception as error:
            return {'status':'unavailable','offer':None,'reason':'The public release check could not be verified (' + type(error).__name__ + ').'}

    def stage(self, offer, *, confirmed=False, cancel_event=None, progress_callback=None):
        if confirmed is not True: raise ValueError('Choose Yes for this exact public release before staging it.')
        if not isinstance(offer,dict) or type(offer.get('release_id')) is not int or offer['release_id'] <= 0:
            raise ValueError('An exact previously checked public release identity is required.')
        self._managed(); binding, preferences, locator = self._binding(); _check(cancel_event)
        expected = release_offer(self._transport().get_json(API + '/releases/' + str(offer.get('release_id')),cancel_event=cancel_event),
                                 self.current_version,asset_name(self.system,self.machine))
        if expected is None or expected != offer: raise ValueError('The approved release or asset identity changed; review the new offer.')
        self._key(create=True)
        identity = uuid.uuid4().hex; folder = self.directory / identity
        self._create_private_directory(folder)
        payload = folder / 'payload'
        self._create_private_directory(payload)
        created = self.clock()
        if shutil.disk_usage(folder).free < offer['asset']['size'] + _RESERVE: raise OSError('The update would consume the reserved destination buffer.')
        for asset in (offer['checksums'],offer['asset']):
            self._transport().download(asset,folder / asset['name'],cancel_event=cancel_event,progress_callback=progress_callback)
            actual = _hash_file(folder / asset['name'],maximum=asset['size'],event=cancel_event)
            if actual != {'size':asset['size'],'sha256':asset['sha256']}: raise ValueError('The downloaded asset failed independent digest/size readback.')
        checksums = _checksums(_read(folder / 'SHA256SUMS.txt',65536))
        if checksums[offer['asset']['name']] != offer['asset']['sha256']: raise ValueError('GitHub digest and SHA256SUMS disagree.')
        extract_windows(folder / offer['asset']['name'],payload,cancel_event=cancel_event,progress_callback=progress_callback)
        project = tomllib.loads(_read(payload / 'source' / 'pyproject.toml',1024 ** 2).decode())
        if project.get('project',{}).get('version') != offer['version']: raise ValueError('The archive source version differs from the approved release.')
        executable = payload / 'Optimus.exe'
        if _hash_file(executable)['size'] == 0: raise ValueError('The staged native executable is empty.')
        _windows_executable(executable)
        app_source = _read(payload / 'source' / 'src' / 'optimus' / 'app.py',4 * 1024 ** 2)
        if b'--self-update-resume' not in app_source or b'--self-update-worker' not in app_source:
            raise ValueError('This release lacks the managed self-update handoff contract; use its manual download.')
        _new(payload / 'Optimus.location.json',locator)
        if self._binding()[0] != binding: raise ValueError('Saved settings or the current executable changed during staging.')
        manifest = _tree(payload,cancel_event)
        value = {'schema':1,'id':identity,'owner':self._owner(),'created_at':created,'expires_at':created + _TTL,
                 'binding':binding,'offer':copy.deepcopy(offer),'manifest':manifest,'manifest_sha256':_digest(_json(manifest)),
                 'target_executable':str(self.installation.parent / offer['version'] / 'Optimus.exe'),
                 'new_executable_sha256':_hash_file(executable)['sha256']}
        self._write(folder,'stage.json',value)
        return {'status':'staged','stage_id':identity,'target_version':offer['version'],'target_executable':value['target_executable']}

    def _verify_stage(self, value, folder):
        binding, _, locator = self._binding()
        if binding != value['binding']: raise ValueError('The exact saved configuration/current installation changed; update approval is paused.')
        version = value['offer']['version']
        if _version(version) <= _version(binding['old_version']) or value['target_executable'] != str(self.installation.parent / version / 'Optimus.exe'):
            raise ValueError('A self-update target cannot downgrade or leave the managed release directory.')
        payload = folder / 'payload'
        if _tree(payload) != value['manifest'] or _digest(_json(value['manifest'])) != value['manifest_sha256']:
            raise ValueError('The verified staged native files changed.')
        if _read(payload / 'Optimus.location.json',4096) != locator: raise ValueError('The preserved data locator changed.')
        return payload, self.installation.parent / version

    def prepare_handoff(self, stage_id, *, cleanup_approved=False, approved_settings=None, old_process=None):
        value, folder = self._load(stage_id)
        self._not_revoked(stage_id,folder)
        with self.lease_factory(self.data_dir):
            self._verify_stage(value,folder)
            raw = _read(self.data_dir / 'preferences.json',4 * 1024 ** 2); settings = json.loads(raw)
            if type(cleanup_approved) is not bool: raise ValueError('An explicit cleanup approval choice is required.')
            if cleanup_approved and (approved_settings != settings or type(settings.get('setup_version')) is not int
                                     or settings['setup_version'] != CURRENT_SETUP_VERSION):
                raise ValueError('Cleanup requires explicitly approved unchanged completed-setup settings.')
            process = old_process or self._native().current()
            if (not isinstance(process,dict) or type(process.get('pid')) is not int or process['pid'] <= 0
                    or type(process.get('created_at')) not in (int,float) or not math.isfinite(process['created_at']) or process['created_at'] <= 0
                    or Path(process.get('exe','')) != Path(value['binding']['old_executable'])):
                raise ValueError('The exact running original GUI identity is required.')
            handoff = dict(value,old_process=copy.deepcopy(process),cleanup_approved=cleanup_approved,
                           approved_scope_sha256=_digest(_json(settings)) if cleanup_approved else None)
            self._not_revoked(stage_id,folder)
            self._write(folder,'handoff.json',handoff)
        return {'status':'ready','handoff_id':stage_id,'worker_command':[value['binding']['old_executable'],'--self-update-worker',stage_id]}

    def dispatch_worker(self, handoff_id):
        value,folder = self._load(handoff_id,'handoff.json'); self._verify_stage(value,folder)
        self._not_revoked(handoff_id,folder)
        self._write(folder,'dispatch-intent.json',{'id':handoff_id,'owner':self._owner(),'expires_at':value['expires_at'],'at_most_once':True})
        self._not_revoked(handoff_id,folder)
        result = self._native().launch(value['binding']['old_executable'],['--self-update-worker',handoff_id],background=True)
        return dict(result,status='dispatched' if result.get('started') is True else 'needs_review')

    def finish_handoff(self, handoff_id, *, cancel_event=None, progress_callback=None):
        value,folder = self._load(handoff_id,'handoff.json')
        self._not_revoked(handoff_id,folder)
        self._load(handoff_id,'dispatch-intent.json')
        self._write(folder,'worker-intent.json',{'id':handoff_id,'owner':self._owner(),'expires_at':value['expires_at'],'at_most_once':True})
        result = {'id':handoff_id,'owner':self._owner(),'expires_at':value['expires_at'],'status':'needs_review',
                  'started':False,'installed':False,'restart_confirmed':False}
        phase = 'wait_for_original_window'
        try:
            deadline = time.monotonic() + 120
            while self._native().alive(value['old_process']):
                self._not_revoked(handoff_id,folder)
                _check(cancel_event,deadline)
                if self.clock() > value['expires_at']: raise ValueError('The handoff approval expired while waiting for the old GUI.')
                if progress_callback: progress_callback({'phase':'wait','description':'Waiting for the original Optimus window to close safely.'})
                self.sleep(0.25)
            lease = None
            while lease is None:
                _check(cancel_event,deadline)
                candidate = self.lease_factory(self.data_dir)
                try: candidate.acquire(); lease = candidate
                except RuntimeError: self.sleep(0.25)
            try:
                self._load(handoff_id,'handoff.json')
                self._not_revoked(handoff_id,folder)
                phase = 'verify_staged_release'
                payload,target = self._verify_stage(value,folder)
                if self._native().other_instances(): raise RuntimeError('Another Optimus instance appeared; no automatic restart was attempted.')
                backup = folder / 'original-state'
                self._create_private_directory(backup)
                _new(backup / 'preferences.raw.json',_read(self.data_dir / 'preferences.json',4 * 1024 ** 2))
                _new(backup / 'Optimus.location.json',_read(self.installation / 'Optimus.location.json',4096))
                if target.exists():
                    if _tree(target) != value['manifest']: raise ValueError('The target release already exists with different files; it was preserved.')
                else:
                    self._load(handoff_id,'handoff.json'); self._not_revoked(handoff_id,folder)
                    _ordinary_data_path(target)
                    phase = 'publish_release'
                    # A rename out of a protected private workspace retains its
                    # Windows ACL. Create normal objects under Releases so the
                    # new executable can start at the user's saved UAC level.
                    publication = target.parent / ('.' + target.name + '-' + handoff_id + '-stage')
                    _ordinary_data_path(publication)
                    if publication.exists():
                        raise FileExistsError('An earlier publication stage exists and was preserved.')
                    expanded = sum(row['size'] for row in value['manifest'])
                    if shutil.disk_usage(target.parent).free < expanded + _RESERVE:
                        raise OSError('Publishing the release requires its expanded size and the reserved buffer.')
                    shutil.copytree(payload, publication, copy_function=shutil.copyfile)
                    if _tree(payload) != value['manifest'] or _tree(publication) != value['manifest']:
                        raise ValueError('The publication copy differs from the verified private payload.')
                    self._load(handoff_id,'handoff.json'); self._not_revoked(handoff_id,folder)
                    os.rename(publication,target)  # Exclusive Windows rename preserves any existing target.
                if _tree(target) != value['manifest']: raise ValueError('The published release failed complete readback.')
                result['installed'] = True
                phase = 'retarget_launchers'
                if self.launchers is None:
                    from .self_update_launchers import ManagedLaunchers
                    self.launchers = ManagedLaunchers(self.data_dir)
                self._not_revoked(handoff_id,folder)
                integrations = self.launchers.retarget(Path(value['binding']['old_executable']),target / 'Optimus.exe',
                    backup / 'launchers',json.loads(_read(self.data_dir / 'preferences.json',4 * 1024 ** 2)))
                result['launchers'] = integrations
                if integrations.get('outcomeKnown') is not True or integrations.get('running') is not False:
                    raise RuntimeError('Launcher integration outcome is unknown; review the preserved backups before restarting.')
                if _digest(_read(self.data_dir / 'preferences.json',4 * 1024 ** 2)) != value['binding']['preferences_sha256']:
                    raise ValueError('Saved settings changed; no cleanup resume was authorized.')
                if _tree(target) != value['manifest']:
                    raise ValueError('The new release changed during launcher integration; it was not launched.')
                self._write(folder,'ready.json',dict(value,status='ready_to_launch',launchers=integrations))
            finally:
                lease.release()
            phase = 'restart_new_version'
            self._write(folder,'launch-intent.json',{'id':handoff_id,'owner':self._owner(),'expires_at':value['expires_at'],'at_most_once':True})
            self._load(handoff_id,'handoff.json'); self._not_revoked(handoff_id,folder)
            if _hash_file(Path(value['target_executable']))['sha256'] != value['new_executable_sha256']:
                raise ValueError('The new executable changed immediately before restart.')
            # The new child inherits the already-approved privilege context.
            # Do not open a second UAC relaunch that loses this child's identity
            # or repeats a consent decision made during the original startup.
            dispatched = self._native().launch(value['target_executable'],
                ['--self-update-resume',handoff_id,'--admin-relaunch'],background=False)
            result.update(dispatched,target_version=value['offer']['version'])
            if dispatched.get('started') is not True:
                raise RuntimeError('The new Optimus process exited before it could acknowledge startup.')
            phase = 'verify_restarted_window'
            ready = self._wait_for_startup(handoff_id, value, folder, dispatched, cancel_event, progress_callback)
            result.update(status='launched', restart_confirmed=True, restarted_process=ready['process'])
        except Exception as error:
            result.update(status='cancelled' if cancel_event is not None and cancel_event.is_set() else 'needs_review',
                          phase=phase,error_type=type(error).__name__,
                          reason=('Optimus update ' + phase + ' did not complete: ' + str(error))[:1500])
        self._write(folder,'worker-result.json',result)
        return result

    def _wait_for_startup(self, identity, value, folder, dispatched, cancel_event, progress_callback):
        deadline = time.monotonic() + 120
        while True:
            _check(cancel_event, deadline)
            self._not_revoked(identity, folder)
            if (folder / 'startup-ready.json').exists():
                ready, _ = self._load(identity, 'startup-ready.json')
                process = ready.get('process')
                if (ready.get('status') != 'ready' or ready.get('version') != value['offer']['version']
                        or ready.get('manifest_sha256') != value['manifest_sha256'] or not isinstance(process, dict)
                        or type(process.get('pid')) is not int or process['pid'] <= 0
                        or type(process.get('created_at')) not in (int, float) or not math.isfinite(process['created_at'])
                        or process['created_at'] <= 0 or Path(process.get('exe', '')) != Path(value['target_executable'])
                        or not self._native().alive(process)):
                    raise ValueError('The restarted window did not provide its exact authenticated process identity.')
                return ready
            exit_reader = getattr(self._native(), 'exit_code', None)
            code = exit_reader(dispatched.get('pid')) if callable(exit_reader) else None
            if type(code) is int:
                raise RuntimeError('The new Optimus process exited before startup acknowledgement (exit ' + str(code) + ').')
            if progress_callback:
                progress_callback({'phase': 'restart', 'description': 'Waiting for the new Optimus window to verify its version and saved choices.'})
            self.sleep(0.25)

    def consume_resume(self, handoff_id):
        value,folder = self._load(handoff_id,'ready.json'); self._load(handoff_id,'launch-intent.json')
        self._not_revoked(handoff_id,folder)
        with self.lease_factory(self.data_dir):
            self._managed()
            if (self.current_version != value['offer']['version'] or self.installation / 'Optimus.exe' != Path(value['target_executable'])
                    or _hash_file(self.installation / 'Optimus.exe')['sha256'] != value['new_executable_sha256']
                    or _tree(self.installation) != value['manifest']):
                raise ValueError('Only the exact verified new executable may consume this resume approval.')
            raw = _read(self.data_dir / 'preferences.json',4 * 1024 ** 2); settings = json.loads(raw)
            if (_digest(raw) != value['binding']['preferences_sha256']
                    or _digest(_read(self.installation / 'Optimus.location.json',4096)) != value['binding']['locator_sha256']):
                raise ValueError('Saved settings/data location changed after update; use the normal launch review.')
            approved = (value.get('cleanup_approved') is True and type(settings.get('setup_version')) is int
                        and settings['setup_version'] == CURRENT_SETUP_VERSION
                        and _digest(_json(settings)) == value.get('approved_scope_sha256'))
            self._not_revoked(handoff_id,folder)
            self._write(folder,'resume-consumed.json',{'id':handoff_id,'owner':self._owner(),'expires_at':value['expires_at'],
                'consumed_at':self.clock(),'cleanup_approved':approved,'at_most_once':True})
            return {'status':'accepted','consumed':True,'cleanup_approved':approved,'settings':settings,
                    'approved_scope_sha256':value.get('approved_scope_sha256') if approved else None,'version':self.current_version,
                    'launchers':value.get('launchers',{})}

    def acknowledge_resume(self, handoff_id):
        """Called by the visible, resumed GUI before releasing maintenance."""
        value, folder = self._load(handoff_id, 'ready.json')
        consumed, _ = self._load(handoff_id, 'resume-consumed.json')
        self._load(handoff_id, 'launch-intent.json')
        self._not_revoked(handoff_id, folder)
        with self.lease_factory(self.data_dir):
            self._managed()
            if consumed.get('at_most_once') is not True or self.current_version != value['offer']['version']:
                raise ValueError('Only the consumed new-version resume can acknowledge its window.')
            process = self._native().current()
            if (not isinstance(process, dict) or type(process.get('pid')) is not int or process['pid'] <= 0
                    or type(process.get('created_at')) not in (int, float) or not math.isfinite(process['created_at'])
                    or process['created_at'] <= 0 or Path(process.get('exe', '')) != Path(value['target_executable'])
                    or self.installation / 'Optimus.exe' != Path(value['target_executable'])
                    or _hash_file(self.installation / 'Optimus.exe')['sha256'] != value['new_executable_sha256']
                    or _tree(self.installation) != value['manifest']
                    or _digest(_read(self.data_dir / 'preferences.json', 4 * 1024 ** 2)) != value['binding']['preferences_sha256']):
                raise ValueError('The restarted GUI, release or saved choices changed before acknowledgement.')
            self._not_revoked(handoff_id, folder)
            self._write(folder, 'startup-ready.json', {'id': handoff_id, 'owner': self._owner(),
                'expires_at': value['expires_at'], 'status': 'ready', 'version': self.current_version,
                'manifest_sha256': value['manifest_sha256'], 'process': process})
        return {'status': 'ready', 'version': self.current_version, 'process': process}


def worker_main(handoff_id):
    from types import SimpleNamespace
    from .filesystem import default_data_root
    try:
        result = SelfUpdater(SimpleNamespace(data_dir=default_data_root())).finish_handoff(handoff_id)
        return 0 if result.get('status') == 'launched' else 2
    except Exception:
        return 2
