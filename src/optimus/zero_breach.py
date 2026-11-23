"""Keep an Optimus-owned copy of Zero Breach Toolkit current and run it.

Zero Breach (https://github.com/interplains/Zero-Breach-Toolkit) is a
read-only, offline spyware-indicator checker shipped as one Python file.

* ``update`` asks the GitHub releases API for the latest stable release and,
  when it is newer than the installed copy, downloads ``zero-breach-toolkit.py``
  to a temporary file. The file is accepted only when its SHA-256 matches both
  GitHub's per-asset ``digest`` and the line for it in the release's
  ``SHA256SUMS.txt``; otherwise nothing is replaced.
* The copy lives under ``<data>/zero-breach``. Consilium's copy (pinned to an
  exact hash) is never modified; it is used only as an initial seed when it
  matches a published release digest.
* ``scan`` runs the same read-only quick scan Consilium uses
  (``--quick --no-menu --json --report``) with a Python interpreter, never the
  frozen Optimus executable, and keeps the reports.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.request
from typing import Any, Callable

REPOSITORY = 'interplains/Zero-Breach-Toolkit'
ASSET = 'zero-breach-toolkit.py'
SUMS = 'SHA256SUMS.txt'
FOLDER = 'zero-breach'
REPORTS = 'reports'
API = f'https://api.github.com/repos/{REPOSITORY}/releases/latest'
DOWNLOAD_PREFIX = f'https://github.com/{REPOSITORY}/releases/download/'
MAX_TOOL_BYTES = 64 * 1024 * 1024
SCAN_TIMEOUT_SECONDS = 60 * 60
_TAG = re.compile(r'v?(\d+)\.(\d+)\.(\d+)\Z')
_DIGEST = re.compile(r'sha256:([0-9a-f]{64})\Z')
_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def _utc() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _version(tag: str) -> tuple[int, int, int] | None:
    match = _TAG.fullmatch(str(tag or '').strip())
    return tuple(int(part) for part in match.groups()) if match else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _default_fetch(url: str, limit: int) -> bytes:
    if not (url == API or url.startswith(DOWNLOAD_PREFIX)):
        raise ValueError('Zero Breach downloads are limited to its GitHub release.')
    request = urllib.request.Request(url, headers={'User-Agent': 'Optimus-ZeroBreach-Updater',
                                                   'Accept': 'application/vnd.github+json' if url == API else '*/*'})
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - fixed https hosts above
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError('The Zero Breach download is larger than expected.')
    return data


def _sums_line(text: str, name: str) -> str | None:
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and parts[1].lstrip('*') == name and re.fullmatch('[0-9a-fA-F]{64}', parts[0]):
            return parts[0].lower()
    return None


def find_python() -> str | None:
    """A real Python interpreter (the frozen Optimus executable cannot run scripts)."""
    for name in ('python.exe', 'python3.exe', 'python', 'python3'):
        found = shutil.which(name)
        if found and 'windowsapps' not in found.casefold():
            return found
    local = Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs' / 'Python'
    candidates = sorted(local.glob('Python3*/python.exe'), reverse=True) if local.is_dir() else []
    return str(candidates[0]) if candidates else None


class ZeroBreach:
    def __init__(self, backend: Any = None, *, data_dir: str | os.PathLike[str] | None = None,
                 fetch: Callable[[str, int], bytes] = _default_fetch,
                 runner: Callable[..., Any] = subprocess.run, python: Callable[[], str | None] = find_python,
                 seed: str | os.PathLike[str] | None = None):
        root = data_dir if data_dir is not None else getattr(backend, 'data_dir', None)
        if root is None:
            raise ValueError('A data directory is required for Zero Breach.')
        self.root = Path(root) / FOLDER
        self.tool = self.root / ASSET
        self.state_path = self.root / 'state.json'
        self.fetch, self.runner, self.python = fetch, runner, python
        appdata = os.environ.get('APPDATA', '')
        self.seed = Path(seed) if seed is not None else (
            Path(appdata) / 'io.github.ag3nt47.consilium' / 'security' / FOLDER / ASSET if appdata else None)
        self.lock = threading.Lock()

    # -- state -------------------------------------------------------------
    def state(self) -> dict[str, Any]:
        try:
            value = json.loads(self.state_path.read_text(encoding='utf-8'))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self, **changes: Any) -> dict[str, Any]:
        from .speed_enhancements import _private_json
        value = {**self.state(), **changes}
        _private_json(self.state_path, value)
        return value

    def installed(self) -> dict[str, Any]:
        state = self.state()
        present = self.tool.is_file()
        ok = present and state.get('sha256') and _sha256(self.tool) == state['sha256']
        return {'present': present, 'verified': bool(ok), 'version': state.get('version') if ok else None,
                'path': str(self.tool), 'last_update_check': state.get('last_update_check'),
                'last_scan': state.get('last_scan')}

    # -- update ------------------------------------------------------------
    def _latest(self) -> dict[str, Any]:
        release = json.loads(self.fetch(API, 2 * 1024 * 1024).decode('utf-8'))
        if not isinstance(release, dict) or release.get('draft') or release.get('prerelease'):
            raise ValueError('GitHub did not report a stable Zero Breach release.')
        tag = str(release.get('tag_name') or '')
        if _version(tag) is None:
            raise ValueError('The latest Zero Breach release has an unexpected version tag.')
        assets = {asset.get('name'): asset for asset in release.get('assets') or [] if isinstance(asset, dict)}
        tool, sums = assets.get(ASSET), assets.get(SUMS)
        if not tool or not sums:
            raise ValueError('The latest Zero Breach release is missing its script or checksum list.')
        match = _DIGEST.fullmatch(str(tool.get('digest') or ''))
        if not match:
            raise ValueError('GitHub did not publish a SHA-256 digest for the Zero Breach script.')
        size = int(tool.get('size') or 0)
        if not 0 < size <= MAX_TOOL_BYTES:
            raise ValueError('The Zero Breach script has an unexpected size.')
        for asset in (tool, sums):
            if str(asset.get('browser_download_url') or '') != DOWNLOAD_PREFIX + tag + '/' + asset['name']:
                raise ValueError('A Zero Breach asset is not served from its own release.')
        return {'tag': tag, 'sha256': match.group(1), 'size': size,
                'url': tool['browser_download_url'], 'sums_url': sums['browser_download_url']}

    def update(self, *, progress_callback=None, cancel_event=None) -> dict[str, Any]:
        with self.lock:
            checked = _utc()
            try:
                latest = self._latest()
            except Exception as error:  # network, JSON or validation
                self._save_state(last_update_check=checked, last_update_error=str(error)[:300])
                return {'status': 'check_failed', 'detail': str(error)[:300], 'installed': self.installed()}
            current = self.installed()
            if current['verified'] and _version(current['version'] or '') >= _version(latest['tag']):
                self._save_state(last_update_check=checked, last_update_error=None)
                return {'status': 'up_to_date', 'version': current['version'], 'installed': self.installed()}
            if cancel_event is not None and cancel_event.is_set():
                return {'status': 'not_changed', 'detail': 'Cancelled before download.'}
            # A seed copy that already matches the published digest saves a download.
            seeded = (self.seed is not None and self.seed.is_file()
                      and self.seed.stat().st_size == latest['size'] and _sha256(self.seed) == latest['sha256'])
            self.root.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix='zero-breach-', suffix='.tmp', dir=self.root)
            os.close(descriptor)
            temporary = Path(temporary)
            try:
                if seeded:
                    shutil.copyfile(self.seed, temporary)
                else:
                    if progress_callback is not None:
                        progress_callback({'description': 'Downloading Zero Breach ' + latest['tag']})
                    temporary.write_bytes(self.fetch(latest['url'], MAX_TOOL_BYTES))
                sums_hash = _sums_line(self.fetch(latest['sums_url'], 64 * 1024).decode('utf-8', 'replace'), ASSET)
                actual = _sha256(temporary)
                if actual != latest['sha256'] or sums_hash != latest['sha256'] or temporary.stat().st_size != latest['size']:
                    raise ValueError('The downloaded Zero Breach script did not match its published SHA-256.')
                os.replace(temporary, self.tool)
            except Exception as error:
                self._save_state(last_update_check=checked, last_update_error=str(error)[:300])
                return {'status': 'update_failed', 'detail': str(error)[:300], 'installed': self.installed()}
            finally:
                if temporary.exists():
                    temporary.unlink()
            self._save_state(version=latest['tag'], sha256=latest['sha256'], installed_at=_utc(),
                             last_update_check=checked, last_update_error=None)
            return {'status': 'updated', 'version': latest['tag'], 'previous': current['version'],
                    'source': 'consilium-seed' if seeded else 'github', 'installed': self.installed()}

    # -- scan --------------------------------------------------------------
    def scan(self, *, progress_callback=None, cancel_event=None) -> dict[str, Any]:
        current = self.installed()
        if not current['verified']:
            return {'status': 'not_installed', 'detail': 'Run the Zero Breach update first; no verified copy is installed.'}
        python = self.python()
        if not python:
            return {'status': 'python_missing', 'detail': 'Zero Breach needs Python 3 installed on this PC.'}
        if cancel_event is not None and cancel_event.is_set():
            return {'status': 'not_changed', 'detail': 'Cancelled before the scan started.'}
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        reports = self.root / REPORTS
        reports.mkdir(parents=True, exist_ok=True)
        json_path, text_path = reports / f'zero-breach-{stamp}.json', reports / f'zero-breach-{stamp}.txt'
        if progress_callback is not None:
            progress_callback({'description': 'Zero Breach quick spyware-indicator scan'})
        command = [python, '-X', 'utf8', str(self.tool), '--quick', '--no-menu',
                   '--json', str(json_path), '--report', str(text_path)]
        try:
            completed = self.runner(command, capture_output=True, text=True, timeout=SCAN_TIMEOUT_SECONDS,
                                    creationflags=_NO_WINDOW, cwd=str(self.root))
            code = completed.returncode
        except subprocess.TimeoutExpired:
            code = None
        status = {0: 'clean', 2: 'matches_found'}.get(code, 'error')
        detail = {'clean': 'No known Zero Breach indicators were found (not a guarantee of a clean PC).',
                  'matches_found': 'Zero Breach reported actionable matches. Review the report.',
                  'error': f'Zero Breach ended with exit code {code}.' if code is not None else 'Zero Breach timed out.'}[status]
        self._save_state(last_scan={'at': _utc(), 'status': status, 'exit_code': code,
                                    'report': str(text_path), 'json': str(json_path)})
        return {'status': status, 'exit_code': code, 'detail': detail, 'report': str(text_path), 'json': str(json_path),
                'version': current['version']}

    def update_and_scan(self, *, progress_callback=None, cancel_event=None) -> dict[str, Any]:
        updated = self.update(progress_callback=progress_callback, cancel_event=cancel_event)
        scanned = self.scan(progress_callback=progress_callback, cancel_event=cancel_event)
        return {'update': updated, 'scan': scanned}
