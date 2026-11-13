"""Update the explicitly registered ArtCraft portable applications from their publishers.

No discovery outside the registered suite, process termination, installer execution,
or modification of user documents is performed. Prior versions are retained.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import time
import urllib.parse
import urllib.request
import uuid
import zipfile

from .tls_context import create_update_tls_context

APPS = {'PhotoCraft': 'photocraft', 'VectorCraft': 'vectorcraft',
        'FilmCraft': 'filmcraft', 'LightCraft': 'lightcraft', 'PdfCraft': 'pdfcraft',
        'EffectCraft': 'effectcraft', 'DesignCraft': 'designcraft',
        'WordCraft': 'wordcraft', 'SoundCraft': 'soundcraft', 'DeckCraft': 'deckcraft',
        'CADCraft': 'cadcraft', 'GridCraft': 'gridcraft'}
_VERSION = re.compile(r'v?(0|[1-9]\d{0,5})\.(0|[1-9]\d{0,5})\.(0|[1-9]\d{0,5})\Z')
_DIGEST = re.compile(r'sha256:([0-9a-f]{64})\Z')
_MAX_DOWNLOAD = 512 * 1024 ** 2
_MAX_EXPANDED = 2 * 1024 ** 3
_HOSTS = frozenset(('api.github.com', 'github.com', 'release-assets.githubusercontent.com',
                    'objects.githubusercontent.com'))


class Cancelled(RuntimeError):
    pass


class RecoveryRequired(RuntimeError):
    pass


def _check(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise Cancelled('Craft update cancelled; installed applications are preserved.')


def _version(value):
    match = _VERSION.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError('Only stable three-part Craft versions are supported.')
    return tuple(map(int, match.groups()))


def _ordinary(path, *, file=False):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('The Craft installation path must be absolute.')
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Craft updates do not follow links or junctions.')
    if file and not path.is_file():
        raise ValueError('An expected ordinary Craft file is missing: ' + str(path))
    return path


def _read(path, maximum=2 * 1024 ** 2):
    _ordinary(path, file=True)
    with Path(path).open('rb') as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError('The Craft update record exceeds its size limit.')
    return json.loads(raw.decode('utf-8-sig'))


def _atomic(path, value):
    path = _ordinary(path)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, indent=2, ensure_ascii=True, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


class _Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        _url(newurl)
        return super().redirect_request(request, response, code, message, headers, newurl)


def _url(url):
    parts = urllib.parse.urlsplit(url)
    if (parts.scheme != 'https' or parts.hostname not in _HOSTS or parts.username
            or parts.password or parts.port not in (None, 443)):
        raise ValueError('Craft downloads must stay on official GitHub HTTPS hosts.')
    return url


class GitHub:
    def __init__(self):
        self.opener = urllib.request.build_opener(_Redirects(),
            urllib.request.HTTPSHandler(context=create_update_tls_context()))

    def _open(self, url):
        request = urllib.request.Request(_url(url), headers={
            'User-Agent': 'Optimus-CraftUpdater/0.12.47',
            'Accept': 'application/vnd.github+json'})
        response = self.opener.open(request, timeout=20)
        _url(response.geturl())
        return response

    def release(self, repo):
        with self._open('https://api.github.com/repos/storytold/' + repo + '/releases/latest') as response:
            raw = response.read(2 * 1024 ** 2 + 1)
        if len(raw) > 2 * 1024 ** 2:
            raise ValueError('GitHub release metadata exceeds its limit.')
        return json.loads(raw)

    def download(self, offer, path, cancel_event=None):
        digest, count = hashlib.sha256(), 0
        with self._open(offer['url']) as response, Path(path).open('xb') as stream:
            while True:
                _check(cancel_event)
                chunk = response.read(128 * 1024)
                if not chunk:
                    break
                count += len(chunk)
                if count > offer['size'] or count > _MAX_DOWNLOAD:
                    raise ValueError('The Craft download exceeded its advertised size.')
                digest.update(chunk)
                stream.write(chunk)
        if count != offer['size'] or digest.hexdigest() != offer['sha256']:
            raise ValueError('Craft download checksum or size verification failed.')


def release_offer(repo, release):
    if not isinstance(release, dict) or release.get('draft') is not False or release.get('prerelease') is not False:
        raise ValueError('A stable published Craft release is required.')
    tag = release.get('tag_name')
    version = '.'.join(map(str, _version(tag)))
    name = repo + '-' + version + '-windows-x64-portable.zip'
    matches = [item for item in release.get('assets', []) if isinstance(item, dict) and item.get('name') == name]
    if len(matches) != 1:
        raise ValueError('The release does not contain one Windows x64 portable download.')
    asset = matches[0]
    expected = 'https://github.com/storytold/' + repo + '/releases/download/' + tag + '/' + name
    digest = _DIGEST.fullmatch(asset.get('digest', ''))
    if (asset.get('browser_download_url') != expected or digest is None
            or type(asset.get('size')) is not int or not 0 < asset['size'] <= _MAX_DOWNLOAD):
        raise ValueError('The official portable release lacks a verified URL, size or SHA-256 digest.')
    return {'version': version, 'filename': name, 'url': expected,
            'size': asset['size'], 'sha256': digest[1], 'tag': tag}


def _running(directory):
    import psutil
    directory = str(Path(directory).resolve()).casefold().rstrip('\\/') + os.sep
    for process in psutil.process_iter(['name', 'exe']):
        try:
            executable = process.info.get('exe')
            if executable and os.path.abspath(executable).casefold().startswith(directory):
                return True
            # An unreadable process with an exact suite executable name is deferred.
            if not executable and str(process.info.get('name', '')).casefold() in (
                    Path(directory.rstrip('\\/')).name.casefold() + '.exe',
                    Path(directory.rstrip('\\/')).name.casefold() + '-cli.exe'):
                return True
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except psutil.AccessDenied:
            return True
    return False


def extract_archive(archive, destination, repo, cancel_event=None):
    destination = _ordinary(destination)
    destination.mkdir()
    with zipfile.ZipFile(archive) as source:
        members = source.infolist()
        if not members or len(members) > 20000 or sum(item.file_size for item in members) > _MAX_EXPANDED:
            raise ValueError('The Craft portable archive exceeds extraction limits.')
        seen = set()
        for item in members:
            _check(cancel_event)
            raw = item.filename
            name = PurePosixPath(raw)
            mode = item.external_attr >> 16
            if (not raw or '\\' in raw or ':' in raw or name.is_absolute()
                    or any(part in ('', '.', '..') or part.endswith((' ', '.')) for part in raw.rstrip('/').split('/'))
                    or stat.S_ISLNK(mode) or item.flag_bits & 1
                    or any(re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', part)
                           for part in name.parts)):
                raise ValueError('The Craft archive contains an unsafe path or link.')
            folded = raw.rstrip('/').casefold()
            if folded in seen:
                raise ValueError('The Craft archive contains duplicate paths.')
            seen.add(folded)
            target = destination.joinpath(*name.parts)
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open(item) as reader, target.open('xb') as writer:
                    shutil.copyfileobj(reader, writer, length=128 * 1024)
    children = list(destination.iterdir())
    payload = children[0] if len(children) == 1 and children[0].is_dir() else destination
    executable = payload / (repo + '.exe')
    _ordinary(executable, file=True)
    with executable.open('rb') as stream:
        if stream.read(2) != b'MZ':
            raise ValueError('The Craft download does not contain its expected Windows executable.')
    return payload


def _payload_files(directory):
    return sorted(path.relative_to(directory).as_posix().casefold()
                  for path in directory.rglob('*') if path.is_file())


def _prior_release_files(root, installed, saved, current):
    names = saved.get('release_files')
    if names is not None:
        if (not isinstance(names, list) or len(names) > 20000
                or any(not isinstance(name, str) or not name or '\\' in name or ':' in name
                       or PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts
                       for name in names)):
            raise ValueError('The saved Craft release file inventory is invalid.')
        return set(names)
    if _version(current) != _version(installed['Version']):
        raise ValueError('The previous release file inventory is unavailable; local data was preserved.')
    archive = _ordinary(Path(installed.get('DownloadPath', '')), file=True)
    if not archive.is_relative_to(root):
        raise ValueError('The original Craft archive is outside its registered installation.')
    expected = _DIGEST.fullmatch(installed.get('ExpectedDigest', ''))
    with archive.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    if expected is None or digest != expected[1] or archive.stat().st_size != installed.get('Size'):
        raise ValueError('The original Craft archive cannot be verified; local data was preserved.')
    with zipfile.ZipFile(archive) as source:
        names = [PurePosixPath(item.filename) for item in source.infolist() if not item.is_dir()]
    first = {name.parts[0] for name in names if name.parts}
    wrapper = len(first) == 1 and all(len(name.parts) > 1 for name in names)
    return {PurePosixPath(*name.parts[1:]).as_posix().casefold() if wrapper else name.as_posix().casefold()
            for name in names}


def _preserve_local_files(directory, payload, prior_files, cancel_event=None):
    """Carry portable preferences/recovery and non-release user files forward."""
    preserved = 0
    for folder, directories, files in os.walk(directory, followlinks=False):
        _check(cancel_event)
        for child in directories:
            _ordinary(Path(folder) / child)
        for name in files:
            _check(cancel_event)
            source = _ordinary(Path(folder) / name, file=True)
            relative = source.relative_to(directory)
            parts = tuple(part.casefold() for part in relative.parts)
            portable_data = parts[0] == 'data' or parts[0].endswith('craftdata')
            if relative.as_posix().casefold() in prior_files and not portable_data:
                continue
            target = payload / relative
            _ordinary(target)
            if target.exists() and not portable_data:
                if not target.is_file() or target.stat().st_size != source.stat().st_size:
                    raise ValueError('A local custom file conflicts with the new release: ' + str(relative))
                with target.open('rb') as left, source.open('rb') as right:
                    if hashlib.file_digest(left, 'sha256').digest() != hashlib.file_digest(right, 'sha256').digest():
                        raise ValueError('A local custom file conflicts with the new release: ' + str(relative))
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            preserved += 1
    return preserved


@contextmanager
def _lock(path):
    import msvcrt
    with _ordinary(path).open('a+b') as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def _registration(data_dir):
    registration = _read(data_dir / 'craft-installation.json', 16384)
    if (not isinstance(registration, dict) or registration.get('schema') != 1
            or registration.get('updates_authorized') is not True
            or registration.get('apps') != list(APPS)):
        raise ValueError('The exact Craft suite has not been registered for automatic updates.')
    root = _ordinary(Path(registration.get('root', '')))
    if not root.is_dir() or not (root / 'Apps').is_dir():
        raise ValueError('The registered Craft installation is unavailable.')
    return root


def _recover(data_dir, root, state, running):
    """Restore an interrupted swap before trusting the saved version state."""
    journal = data_dir / 'craft-update-transaction.json'
    if not journal.exists():
        return
    transaction = _read(journal, 16384)
    if not isinstance(transaction, dict) or transaction.get('schema') != 1:
        raise ValueError('The Craft update transaction record is invalid.')
    if transaction.get('status') == 'completed' or transaction.get('status') == 'rolled_back':
        return
    name = transaction.get('name')
    if name not in APPS or transaction.get('status') != 'prepared':
        raise ValueError('The interrupted Craft update cannot be identified.')
    directory = _ordinary(root / 'Apps' / name)
    backup = _ordinary(Path(transaction.get('backup', '')))
    prior = transaction.get('prior_version')
    _version(prior)
    if (transaction.get('directory') != str(directory)
            or backup.parent != root / 'Previous Versions'
            or not re.fullmatch(re.escape(name + '-' + str(prior).lstrip('v') + '-') + '[0-9a-f]{12}', backup.name)):
        raise ValueError('The interrupted Craft update is outside its registered directories.')
    if backup.exists():
        if directory.exists():
            if running(directory):
                raise ValueError('Close ' + name + ' before Optimus recovers its interrupted update.')
            quarantine = root / 'Previous Versions' / (name + '-interrupted-' + uuid.uuid4().hex)
            os.rename(directory, quarantine)
        os.rename(backup, directory)
    elif not directory.exists():
        raise ValueError('The interrupted Craft update has no installed app or recovery backup.')
    _ordinary(directory / (APPS[name] + '.exe'), file=True)
    previous_state = transaction.get('prior_state', {'version': prior})
    if not isinstance(previous_state, dict) or _version(previous_state.get('version', prior)) != _version(prior):
        raise ValueError('The interrupted Craft update has an invalid prior version record.')
    state['apps'][name] = dict(previous_state, version=prior, recovered_at=time.time())
    _atomic(data_dir / 'craft-update-state.json', state)
    _atomic(journal, dict(transaction, status='rolled_back'))


def run_craft_updates(data_dir, *, cancel_event=None, client=None, running=None, progress=None):
    """Check every registered app; failures and busy apps do not block the others."""
    data_dir = _ordinary(Path(data_dir))
    if os.name != 'nt':
        return {'skipped': True, 'note': 'This registered Craft suite uses Windows portable downloads.'}
    with _lock(data_dir / 'craft-updates.lock') as acquired:
        if not acquired:
            return {'skipped': True, 'items': [], 'note': 'Another Optimus Craft update check is already running.'}
        root = _registration(data_dir)
        manifest = _read(root / 'download-manifest.json')
        if not isinstance(manifest, list):
            raise ValueError('The original Craft installation manifest must be a list.')
        initial = {row.get('Name'): row for row in manifest if isinstance(row, dict)}
        state_path = data_dir / 'craft-update-state.json'
        state = _read(state_path) if state_path.exists() else {'schema': 1, 'apps': {}}
        if not isinstance(state, dict) or state.get('schema') != 1 or not isinstance(state.get('apps'), dict):
            raise ValueError('The saved Craft version state is invalid.')
        client, running = client or GitHub(), running or _running
        _recover(data_dir, root, state, running)
        items = []
        for name, repo in APPS.items():
            row = {'name': name, 'id': 'storytold/' + repo, 'source': 'github'}
            items.append(row)
            work = None
            try:
                _check(cancel_event)
                installed = initial.get(name)
                directory = _ordinary(root / 'Apps' / name)
                if (not installed or installed.get('Status') != 'Ready'
                        or Path(installed.get('AppDirectory', '')) != directory):
                    raise ValueError('The initial Craft installation is not verified and ready.')
                executable = _ordinary(directory / (repo + '.exe'), file=True)
                saved = state['apps'].get(name, {})
                if not isinstance(saved, dict):
                    raise ValueError('The saved Craft application state is invalid.')
                current = saved.get('version', installed.get('Version'))
                _version(current)
                row['from_version'] = current
                if progress is not None:
                    try:
                        progress({'description': 'Checking the official ' + name + ' release',
                                  'current': len(items) - 1, 'total': len(APPS), 'phase': 'scan',
                                  'item_progress': {'key': repo, 'label': name, 'phase': 'scan', 'percent': None}})
                    except Exception:
                        pass
                offer = release_offer(repo, client.release(repo))
                row['available_version'] = offer['version']
                if _version(offer['version']) <= _version(current):
                    row.update(status='current', note='Already at the latest stable version.')
                    continue
                if running(directory):
                    row.update(status='deferred', note='Close this Craft app; Optimus will retry at the next check.')
                    continue
                parent = _ordinary(root / '.updates')
                parent.mkdir(exist_ok=True)
                work = parent / (repo + '-' + uuid.uuid4().hex)
                work.mkdir()
                archive = work / offer['filename']
                client.download(offer, archive, cancel_event)
                # Recheck the bytes independently of the transport implementation.
                with archive.open('rb') as stream:
                    actual = hashlib.file_digest(stream, 'sha256').hexdigest()
                if archive.stat().st_size != offer['size'] or actual != offer['sha256']:
                    raise ValueError('Craft download checksum or size verification failed.')
                if shutil.disk_usage(root).free < _MAX_EXPANDED + 256 * 1024 ** 2:
                    raise ValueError('At least 2.25 GiB of free space is needed to stage a Craft update.')
                payload = extract_archive(archive, work / 'extracted', repo, cancel_event)
                release_files = _payload_files(payload)
                prior_files = _prior_release_files(root, installed, saved, current)
                preserved = _preserve_local_files(directory, payload, prior_files, cancel_event)
                _check(cancel_event)
                if running(directory):
                    row.update(status='deferred', note='The Craft app opened during download; its update was deferred.')
                    continue
                backups = _ordinary(root / 'Previous Versions')
                backups.mkdir(exist_ok=True)
                backup = backups / (name + '-' + str(current).lstrip('v') + '-' + uuid.uuid4().hex[:12])
                # A write-ahead journal permits exact rollback after interruption.
                journal = data_dir / 'craft-update-transaction.json'
                _atomic(journal, {'schema': 1, 'name': name, 'directory': str(directory),
                    'backup': str(backup), 'payload': str(payload), 'offer': offer,
                    'prior_version': current, 'prior_state': dict(saved, version=current), 'status': 'prepared'})
                os.rename(directory, backup)
                try:
                    os.rename(payload, directory)
                    _ordinary(directory / executable.name, file=True)
                    updated_state = copy.deepcopy(state)
                    updated_state['apps'][name] = {'version': offer['version'], 'sha256': offer['sha256'],
                        'updated_at': time.time(), 'backup': str(backup), 'release_files': release_files}
                    _atomic(state_path, updated_state)
                    state = updated_state
                except Exception:
                    try:
                        if directory.exists():
                            os.rename(directory, work / 'failed-payload')
                        os.rename(backup, directory)
                    except Exception as error:
                        raise RecoveryRequired('Craft rollback needs recovery; its exact journal and prior version were retained: ' + str(backup)) from error
                    raise
                try:
                    _atomic(journal, {'schema': 1, 'name': name, 'status': 'completed',
                        'directory': str(directory), 'backup': str(backup), 'version': offer['version']})
                except Exception as error:
                    raise RecoveryRequired('Craft update confirmation needs recovery; its exact journal and prior version were retained.') from error
                row.update(status='updated', to_version=offer['version'], backup=str(backup),
                           preserved_files=preserved,
                           note='Official portable release verified; local data and prior version retained.')
            except Cancelled as error:
                row.update(status='cancelled', note=str(error))
                break
            except Exception as error:
                row.update(status='failed', error=str(error)[:1500])
                if isinstance(error, RecoveryRequired):
                    row['recovery_required'] = True
                    break
            finally:
                # Only the unique work directory created by this exact attempt is removed.
                if work is not None and work.is_dir():
                    _ordinary(work)
                    if work.parent == root / '.updates' and work.name.startswith(repo + '-'):
                        shutil.rmtree(work)
        result = {'schema': 1, 'checked_at': time.time(), 'items': items,
            'updated_count': sum(row.get('status') == 'updated' for row in items),
            'deferred_count': sum(row.get('status') == 'deferred' for row in items),
            'failed_items': sum(row.get('status') == 'failed' for row in items),
            'cancelled': any(row.get('status') == 'cancelled' for row in items),
            'installer_started': False, 'outcomeKnown': True, 'running': False}
        _atomic(data_dir / 'craft-updates-last.json', result)
        return result


def run_saved_craft_updates(data_dir=None):
    """Narrow sign-in command, independent of the main GUI and future self-updates."""
    from .filesystem import default_data_root
    data_dir = _ordinary(Path(data_dir) if data_dir is not None else default_data_root())
    settings = _read(data_dir / 'preferences.json')
    registration = _read(data_dir / 'craft-installation.json', 16384)
    # Future upstream versions may discard an unknown preference. The durable
    # exact suite registration retains this user's explicit authorization.
    enabled = settings.get('craft_updates_enabled', registration.get('updates_authorized'))
    if settings.get('software_updates_enabled') is not True or enabled is not True:
        return {'skipped': True, 'items': [], 'note': 'Craft or software updates are disabled in your saved choices.'}
    return run_craft_updates(data_dir)
