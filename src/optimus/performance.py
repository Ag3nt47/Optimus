"""Explicit Windows application scheduling and recoverable sleep controls.

Import and inventory never change processes. All operations verify PID, creation
 time, executable, ownership, session and protected-process rules. Saved sleep
intent is durable before suspend; pending entries remain available to Wake All.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import math
import ntpath
import os
from pathlib import Path
from datetime import datetime, timezone
import re
import sys
import tempfile
import threading
import time
from typing import Any

import psutil
from .filesystem import replace_with_retry

_PLATFORM = sys.platform


class PerformanceError(RuntimeError):
    """A requested operation could not be performed safely."""


_SECURITY_NAMES = {
    "msmpeng.exe", "nissrv.exe", "mssense.exe", "sense.exe", "securityhealthservice.exe",
    "securityhealthsystray.exe", "smartscreen.exe", "mpdefendercoreservice.exe", "sgrmbroker.exe",
    "csfalconservice.exe", "sentinelagent.exe", "sentinelservicehost.exe", "savservice.exe",
    "sophoshealth.exe", "mbamservice.exe", "avp.exe", "avpui.exe", "avastsvc.exe", "avgsvc.exe",
    "bdservicehost.exe", "bdagent.exe", "ekrn.exe", "egui.exe", "ccsvchst.exe", "mcshield.exe",
    "avastui.exe", "avgui.exe", "msascuil.exe", "msseces.exe", "sophosui.exe", "nortonsecurity.exe", "lsass.exe", "lsaiso.exe", "services.exe", "csrss.exe", "smss.exe",
    "wininit.exe", "winlogon.exe", "svchost.exe", "dwm.exe", "fontdrvhost.exe", "audiodg.exe",
    "system", "registry", "memory compression", "secure system", "system idle process",
}
_CODEX_RUNTIME_NAMES = {"codex.exe", "codex-code-mode-host.exe", "codex-computer-use-swift.exe", "node_repl.exe"}
_TOOL_NAMES = {"node.exe", "node_repl.exe", "python.exe", "pythonw.exe"}
_PRIORITY_VALUES = {
    "Normal": getattr(psutil, "NORMAL_PRIORITY_CLASS", 0x20),
    "AboveNormal": getattr(psutil, "ABOVE_NORMAL_PRIORITY_CLASS", 0x8000),
    "High": getattr(psutil, "HIGH_PRIORITY_CLASS", 0x80),
}
if _PLATFORM != 'win32':
    _PRIORITY_VALUES = {'Normal': 0, 'AboveNormal': -5, 'High': -10, 'BelowNormal': 10, 'Idle': 19}
_PRIORITY_NAMES = {int(value): name for name, value in _PRIORITY_VALUES.items()}
if _PLATFORM == 'win32':
    _PRIORITY_NAMES.update({0x40: "Idle", 0x4000: "BelowNormal", 0x100: "RealTime"})
else:
    _PRIORITY_NAMES.update({value: f'Nice {value}' for value in range(-20, 20) if value not in _PRIORITY_NAMES})
_STATE_LOCK = threading.RLock()
_TRACKED_SLEEP_STATES = {"pending", "sleeping", "resume_pending"}
_SERVICE_CACHE: tuple[float, set[int] | None] = (0.0, None)


def _priority_choice_message() -> str:
    if _PLATFORM == 'win32':
        return 'Choose Normal, AboveNormal, or High priority.'
    return 'Choose Normal, AboveNormal, High, BelowNormal or Idle priority. Native presets use OS nice values.'


def _norm(path: str) -> str:
    module = ntpath if _PLATFORM == 'win32' else os.path
    return module.normcase(module.normpath(str(path))) if path else ""


def _within(path: str, root: str) -> bool:
    path, root = _norm(path), _norm(root)
    separator = '\\' if _PLATFORM == 'win32' else os.sep
    return bool(root) and (path == root or path.startswith(root.rstrip(separator) + separator))


def _session_id(process_id: int) -> int | None:
    if os.name != "nt":
        try:
            return os.getsid(int(process_id))
        except OSError:
            return None
    session = ctypes.c_ulong()
    if not ctypes.windll.kernel32.ProcessIdToSessionId(int(process_id), ctypes.byref(session)):
        return None
    return int(session.value)


def _current_user() -> str:
    try:
        user = psutil.Process(os.getpid()).username()
        return user.casefold() if _PLATFORM == 'win32' else user
    except psutil.Error as exc:
        raise PerformanceError("The current user could not be verified.") from exc


def _ancestor_ids() -> set[int]:
    identities = {os.getpid()}
    try:
        identities.update(process.pid for process in psutil.Process(os.getpid()).parents())
    except psutil.Error:
        # Fail closed when ancestry cannot be verified; caller checks this sentinel.
        identities.add(-1)
    return identities


def _trusted_codex_exe(executable: str) -> bool:
    """Recognize protected Store bundle paths or the known per-user Codex runtime.

    A matching process name elsewhere is insufficient. This is an installation
    path allowlist, not a claim that an Authenticode verification was performed.
    """
    executable = _norm(executable)
    if _PLATFORM != 'win32':
        # A basename is insufficient to give an arbitrary Unix process special
        # scheduling privileges. Its ordinary owner/ancestry rules still apply.
        return False
    name = ntpath.basename(executable)
    program_files = os.environ.get("ProgramW6432") or os.environ.get("ProgramFiles", r"C:\Program Files")
    store_root = ntpath.join(program_files, "WindowsApps")
    if _within(executable, store_root):
        relative = executable[len(_norm(store_root)):].lstrip("\\")
        if re.fullmatch(r"openai\.codex_[^\\]+\\app\\chatgpt\.exe", relative, re.IGNORECASE):
            return True
    local_data = os.environ.get("LOCALAPPDATA", "")
    runtime_root = ntpath.join(local_data, "OpenAI", "Codex", "bin") if local_data else ""
    if runtime_root and _within(executable, runtime_root) and name in _CODEX_RUNTIME_NAMES:
        relative = executable[len(_norm(runtime_root)):].lstrip("\\")
        return bool(re.fullmatch(r"[0-9a-f]{8,64}\\[^\\]+\.exe", relative, re.IGNORECASE))
    return False


def _related_to_codex(process: Any) -> bool:
    try:
        return _trusted_codex_exe(process.exe()) or any(_trusted_codex_exe(parent.exe()) for parent in process.parents())
    except psutil.Error:
        return False


def _service_ids(force: bool = False) -> set[int] | None:
    if _PLATFORM != 'win32':
        return set()
    global _SERVICE_CACHE
    with _STATE_LOCK:
        if not force and time.monotonic() - _SERVICE_CACHE[0] < 2:
            return _SERVICE_CACHE[1]
        try:
            identities = {int(service.pid()) for service in psutil.win_service_iter() if service.pid()}
        except (AttributeError, psutil.Error, OSError, ValueError):
            identities = None
        _SERVICE_CACHE = (time.monotonic(), identities)
        return identities


class _InventoryContext:
    """One read-only census, never authority for a process change.

    Current user, management ancestry and service membership are invariant enough
    for display within one scan. Mutations still call the live guards without
    this context and revalidate the selected process identity.
    """
    def __init__(self):
        try:
            self.current_user = _current_user()
        except (PerformanceError, psutil.Error, OSError):
            self.current_user = None
        self.ancestors = _ancestor_ids()
        self.services = _service_ids() if _PLATFORM == 'win32' else set()
        self.cpu_count = max(1, int(_safe_metric(lambda: psutil.cpu_count(logical=True), None) or os.cpu_count() or 1))
        self.sessions = {}

    def session_id(self, pid):
        if pid not in self.sessions:
            self.sessions[pid] = _session_id(pid)
        return self.sessions[pid]


def _base_protection(process: Any, *, for_sleep: bool = True,
                     inventory_context=None, details=None) -> str:
    if _PLATFORM != 'win32':
        return _native_protection(process, for_sleep=for_sleep,
                                  inventory_context=inventory_context, details=details)
    try:
        process_id = int(process.pid)
        if details is None:
            name, executable, username = process.name().casefold(), process.exe(), process.username().casefold()
        else:
            name, executable, username = details['name'].casefold(), details['exe'], details['username'].casefold()
        if process_id <= 4:
            return "Core Windows process."
        if not executable or not username:
            return "Executable or process ownership is inaccessible."
        if 'bingwallpaper' in re.sub(r'[^a-z0-9]', '', name + ' ' + executable.casefold()):
            return "Bing Wallpaper is preserved."
        current_user = inventory_context.current_user if inventory_context is not None else _current_user()
        if current_user is None:
            return "The current user could not be verified."
        if username != current_user:
            return "Only applications owned by the current user can be changed."
        session = inventory_context.session_id(process_id) if inventory_context is not None else _session_id(process_id)
        if session is None:
            return "Windows session could not be verified."
        services = inventory_context.services if inventory_context is not None else _service_ids()
        if services is None:
            return "Windows service ownership could not be verified."
        if process_id in services:
            return "Process hosts a registered Windows service."
        if session == 0:
            return "Session 0 service or system process."
        windows = os.environ.get("SystemRoot") or os.environ.get("WINDIR", r"C:\Windows")
        if _within(executable, windows):
            return "Windows executable."
        if name in _SECURITY_NAMES or re.search(r"\\(?:windows defender|microsoft defender|microsoft antimalware)\\", _norm(executable)):
            return "Security or core Windows infrastructure."
        if re.search(r"\\(?:eset|avast software|avg|sophos|bitdefender|malwarebytes|crowdstrike|sentinelone|kaspersky lab|norton|mcafee)\\", _norm(executable)):
            return "Security product installation path."
        ancestors = inventory_context.ancestors if inventory_context is not None else _ancestor_ids()
        if -1 in ancestors:
            return "Management-tool ancestry could not be verified."
        if process_id in ancestors:
            # Only an explicit priority request may address a verified Codex ancestor.
            if for_sleep or process_id == os.getpid() or not _trusted_codex_exe(executable):
                return "Optimus or a parent process of Optimus."
        if for_sleep and _related_to_codex(process):
            return "Codex and its verified tool family are excluded from sleeping."
        return ""
    except (psutil.Error, OSError, ValueError) as exc:
        return "Process details could not be verified: " + type(exc).__name__


def _native_protection(process: Any, *, for_sleep: bool = True,
                       inventory_context=None, details=None) -> str:
    """Only current-user applications are eligible; core session agents stay live."""
    try:
        pid = int(process.pid)
        if details is None:
            name, executable, owner = process.name().casefold(), process.exe(), process.username()
        else:
            name, executable, owner = details['name'].casefold(), details['exe'], details['username']
        if pid <= 4:
            return 'Kernel or system initialization process.'
        if not executable or not os.path.isabs(executable) or not owner:
            return 'Executable or process ownership is inaccessible.'
        current_user = inventory_context.current_user if inventory_context is not None else _current_user()
        if current_user is None:
            return 'The current user could not be verified.'
        if owner != current_user:
            return 'Only applications owned by the current user can be changed.'
        identities = process.uids()
        if identities.real != os.getuid() or identities.effective != os.geteuid():
            return 'Only applications with the current real and effective user IDs can be changed.'
        # Running Optimus as root does not authorize treating system daemons as
        # desktop applications. Use the normal desktop account for these tools.
        if owner == 'root':
            return 'Root-owned system processes are protected.'
        system_roots = ('/System', '/usr/libexec', '/usr/lib/systemd', '/lib/systemd', '/sbin', '/usr/sbin')
        if any(_within(executable, root) for root in system_roots):
            return 'Operating-system service executable.'
        protected_names = {'systemd', 'init', 'launchd', 'kernel_task', 'windowserver',
            'loginwindow', 'coreaudiod', 'mds', 'mdworker', 'mdworker_shared', 'securityd',
            'trustd', 'tccd', 'syspolicyd', 'amfid', 'xprotectservice', 'xprotectremediator',
            'gnome-shell', 'gnome-session-binary', 'plasmashell', 'kwin_wayland', 'kwin_x11',
            'xfce4-session', 'xfwm4', 'cinnamon', 'mate-session', 'sway', 'weston', 'xorg',
            'xwayland', 'dbus-daemon', 'dbus-broker', 'pipewire', 'wireplumber', 'pulseaudio',
            'gpg-agent', 'ssh-agent', 'gnome-keyring-daemon', 'kwalletd5', 'kwalletd6',
            'polkitd', 'polkit-gnome-authentication-agent-1', 'agent47-opt-guard',
            'xfce4-panel', 'xfsettingsd', 'mate-panel', 'budgie-panel', 'kded5', 'kded6',
            'kdeinit5', 'kdeinit6', 'kaccess', 'ksmserver', 'xdg-desktop-portal',
            'xdg-desktop-portal-gtk', 'xdg-desktop-portal-gnome', 'xdg-desktop-portal-kde',
            'gvfsd', 'ibus-daemon', 'fcitx', 'fcitx5', 'at-spi-bus-launcher', 'at-spi2-registryd'}
        if name in protected_names or any(token in name for token in ('falcon', 'sentinel', 'clamd', 'sophos', 'defender')):
            return 'Desktop session, security agent or core system infrastructure.'
        ancestors = inventory_context.ancestors if inventory_context is not None else _ancestor_ids()
        if -1 in ancestors:
            return 'Management-tool ancestry could not be verified.'
        if pid in ancestors:
            return 'Optimus or a parent process of Optimus.'
        if for_sleep and (name in {'codex', 'chatgpt'} or any(parent.name().casefold() in {'codex', 'chatgpt'} for parent in process.parents())):
            return 'Codex and its tool family are excluded from sleeping.'
        return ''
    except (psutil.Error, OSError, ValueError, AttributeError, TypeError) as exc:
        return 'Process details could not be verified: ' + type(exc).__name__


def _safe_metric(action: Any, default: Any = None) -> Any:
    try:
        return action()
    except (psutil.Error, OSError, AttributeError, NotImplementedError, ValueError):
        return default


def _record(process: Any, *, inventory_context=None) -> dict[str, Any]:
    with process.oneshot():
        executable = _safe_metric(process.exe, '')
        priority = _safe_metric(lambda: int(process.nice()))
        raw_cpu = _safe_metric(lambda: float(process.cpu_percent(interval=None)))
        cpu_count = (inventory_context.cpu_count if inventory_context is not None else
                     max(1, int(_safe_metric(lambda: psutil.cpu_count(logical=True), None) or os.cpu_count() or 1)))
        io = _safe_metric(lambda: process.io_counters())
        affinity = _safe_metric(lambda: list(process.cpu_affinity()))
        memory = _safe_metric(process.memory_info)
        record = {
            'pid': int(process.pid), 'name': _safe_metric(process.name, 'Unavailable'), 'exe': executable,
            'create_time': _safe_metric(lambda: float(process.create_time())), 'username': _safe_metric(process.username, ''),
            'ppid': _safe_metric(lambda: int(process.ppid())), 'priority': _PRIORITY_NAMES.get(priority, str(priority)) if priority is not None else 'Unavailable',
            'priority_value': priority, 'memory_rss': int(memory.rss) if memory else 0,
            'private_bytes': getattr(memory, 'private', 0 if _PLATFORM == 'win32' else None) if memory else (0 if _PLATFORM == 'win32' else None),
            'cpu_percent': round(min(100.0, max(0.0, raw_cpu / cpu_count)), 2) if raw_cpu is not None else 0.0,
            'cpu_percent_one_core': raw_cpu, 'cpu_sample_available': raw_cpu is not None,
            'io_read_bytes': getattr(io, 'read_bytes', None), 'io_write_bytes': getattr(io, 'write_bytes', None),
            'io_read_count': getattr(io, 'read_count', None), 'io_write_count': getattr(io, 'write_count', None),
            'affinity': affinity, 'affinity_text': ','.join(str(core) for core in affinity) if affinity is not None else 'Unavailable',
            'thread_count': _safe_metric(lambda: process.num_threads()), 'handle_count': _safe_metric(lambda: process.num_handles()),
        }
        reason = _base_protection(process, inventory_context=inventory_context, details=record)
        can_set_priority = not bool(_base_protection(process, for_sleep=False,
                                   inventory_context=inventory_context, details=record))
    record.update({'protected': bool(reason), 'reason': reason, 'can_sleep': not bool(reason),
                   'can_set_priority': can_set_priority,
                   'verified_codex_executable': _trusted_codex_exe(executable),
                   'affinity_supported': _PLATFORM != 'darwin' and affinity is not None,
                   'priority_note': 'Native presets use nice values: Normal 0, AboveNormal -5, High -10, BelowNormal 10, Idle 19. Raising priority or restoring a lower nice value may require OS permission; failures are reported without bypassing it.' if _PLATFORM != 'win32' else ''})
    return record

def list_performance_candidates() -> list[dict[str, Any]]:
    """Inventory all readable processes, including explicit protection reasons."""
    rows: list[dict[str, Any]] = []
    context = _InventoryContext()
    for process in psutil.process_iter():
        try:
            rows.append(_record(process, inventory_context=context))
        except (psutil.Error, OSError, ValueError):
            rows.append({"pid": int(process.pid), "name": "Unavailable", "exe": "", "create_time": None,
                         "username": "", "ppid": None, "priority": "Unavailable", "priority_value": None,
                         "memory_rss": 0, "protected": True, "reason": "Process identity is inaccessible.",
                         "can_set_priority": False, "can_sleep": False, "private_bytes": 0 if _PLATFORM == 'win32' else None, "verified_codex_executable": False, "cpu_percent": 0.0, "cpu_percent_one_core": None, "cpu_sample_available": False, "affinity": None, "affinity_text": "Unavailable", "io_read_bytes": None, "io_write_bytes": None, "io_read_count": None, "io_write_count": None, "thread_count": None, "handle_count": None})
    return sorted(rows, key=lambda row: row["memory_rss"], reverse=True)


def _fresh(record: dict[str, Any]) -> Any:
    try:
        process_id = int(record["pid"])
        expected_time = float(record["create_time"])
        expected_exe = str(record["exe"])
        if process_id <= 4 or not expected_exe or not math.isfinite(expected_time):
            raise PerformanceError("The selected process identity is incomplete or protected.")
        process = psutil.Process(process_id)
        if not math.isclose(float(process.create_time()), expected_time, abs_tol=0.000001, rel_tol=0):
            raise PerformanceError("The process ID has been reused. Refresh the list.")
        if _norm(process.exe()) != _norm(expected_exe):
            raise PerformanceError("The executable identity changed. Refresh the list.")
        actual_owner, expected_owner = process.username(), str(record.get('username', ''))
        if _PLATFORM == 'win32':
            actual_owner, expected_owner = actual_owner.casefold(), expected_owner.casefold()
        if expected_owner and actual_owner != expected_owner:
            raise PerformanceError("The process owner changed. Refresh the list.")
        return process
    except (KeyError, TypeError, ValueError, psutil.Error) as exc:
        raise PerformanceError("The selected process ended or its identity could not be verified.") from exc


def _set_priority_verified(record: dict[str, Any], priority: str) -> dict[str, Any]:
    """Change only one explicit verified current-user application; never RealTime."""
    if priority not in _PRIORITY_VALUES:
        raise PerformanceError(_priority_choice_message())
    with _STATE_LOCK:
        process = _fresh(record)
        _service_ids(force=True)
        reason = _base_protection(process, for_sleep=False)
        if reason:
            raise PerformanceError(reason)
        old_priority = int(process.nice())
        process = _fresh(record)
        process.nice(_PRIORITY_VALUES[priority])
        return {"pid": process.pid, "name": process.name(), "old_priority": _PRIORITY_NAMES.get(old_priority, str(old_priority)),
                "priority": priority, "old_priority_value": old_priority, "applied": True,
                "note": "A separate launch watcher may override this setting. Optimus does not modify such a watcher."}


class PerformanceController:
    """Thread-safe single-backend durable suspension ownership and recovery."""

    def __init__(self, state_path: str | Path | None = None):
        if state_path is None:
            from .filesystem import default_data_root
            self.state_path = default_data_root() / "suspended-processes.json"
        else:
            self.state_path = Path(state_path)

    def _load(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"version": 1, "entries": {}}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if data.get("version") != 1 or not isinstance(data.get("entries"), dict):
                raise ValueError("Unsupported suspension state")
            for key, entry in data['entries'].items():
                if not isinstance(key, str) or not isinstance(entry, dict) or not isinstance(entry.get('record'), dict) or not isinstance(entry.get('status'), str):
                    raise ValueError('Invalid suspension record')
            return data
        except (OSError, ValueError, TypeError) as exc:
            raise PerformanceError("Saved sleep records are unreadable. Preserve the state file for recovery before retrying.") from exc

    def _save(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.state_path.parent, prefix=".sleep-", suffix=".tmp", delete=False) as stream:
                temporary = stream.name
                json.dump(state, stream, ensure_ascii=True, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            replace_with_retry(Path(temporary), self.state_path)
            temporary = None
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    @staticmethod
    def _key(record: dict[str, Any]) -> str:
        identity = f"{int(record['pid'])}|{float(record['create_time']):.9f}|{_norm(str(record['exe']))}"
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def list_suspended(self) -> list[dict[str, Any]]:
        with _STATE_LOCK:
            return [{**entry["record"], **entry} for entry in self._load()["entries"].values() if entry.get("status") in _TRACKED_SLEEP_STATES]

    def sleep_process(self, record: dict[str, Any]) -> dict[str, Any]:
        with _STATE_LOCK:
            process = _fresh(record)
            _service_ids(force=True)
            reason = _base_protection(process, for_sleep=True)
            if reason:
                raise PerformanceError(reason)
            state, key = self._load(), self._key(record)
            existing = state["entries"].get(key)
            if existing and existing.get("status") in _TRACKED_SLEEP_STATES:
                return {"pid": process.pid, "status": existing["status"], "changed": False}
            if process.status() == psutil.STATUS_STOPPED:
                raise PerformanceError("This process is already suspended outside Optimus; it is not claimed for recovery.")
            verified = _record(process)
            entry = {"record": verified, "status": "pending", "time_utc": time.time(), "error": ""}
            state["entries"][key] = entry
            self._save(state)  # Durable intent exists before any process mutation.
            try:
                process = _fresh(verified)
                reason = _base_protection(process, for_sleep=True)
                if reason:
                    raise PerformanceError(reason)
                process.suspend()
            except Exception as exc:
                # Keep an ambiguous failed attempt recoverable: suspend may have succeeded before an API error.
                entry["status"], entry["error"] = "pending", str(exc)
                self._save(state)
                raise PerformanceError("Sleep did not complete. Its saved intent remains available in Wake All.") from exc
            entry["status"] = "sleeping"
            self._save(state)
            return {"pid": process.pid, "status": "sleeping", "changed": True}

    def wake_process(self, record: dict[str, Any]) -> dict[str, Any]:
        with _STATE_LOCK:
            state, key = self._load(), self._key(record)
            entry = state["entries"].get(key)
            if not entry or entry.get("status") not in _TRACKED_SLEEP_STATES:
                raise PerformanceError("This identity has no active Optimus sleep record.")
            try:
                process = _fresh(entry["record"])
                _service_ids(force=True)
                reason = _base_protection(process, for_sleep=True)
                if reason:
                    raise PerformanceError(reason)
            except PerformanceError as exc:
                # Retain the entry so access-denied or transient failures remain visible.
                entry["error"] = str(exc)
                self._save(state)
                raise
            entry["status"] = "resume_pending"
            self._save(state)
            process = _fresh(entry["record"])
            if process.status() == psutil.STATUS_STOPPED:
                process.resume()
            entry["status"], entry["error"] = "resumed", ""
            self._save(state)
            return {"pid": process.pid, "status": "resumed", "changed": True}

    def set_process_priority(self, record: dict[str, Any], priority: str) -> dict[str, Any]:
        if priority not in _PRIORITY_VALUES:
            raise PerformanceError(_priority_choice_message())
        with _STATE_LOCK:
            process = _fresh(record)
            _service_ids(force=True)
            reason = _base_protection(process, for_sleep=False)
            if reason:
                raise PerformanceError(reason)
            previous = int(process.nice())
            state = self._load()
            change_id = hashlib.sha256(os.urandom(32)).hexdigest()
            change = {'id': change_id, 'kind': 'priority', 'record': _record(process), 'previous': previous,
                      'selected': int(_PRIORITY_VALUES[priority]), 'status': 'pending', 'time_utc': time.time(), 'error': ''}
            history = state.setdefault('allocation_history', [])
            if not isinstance(history, list):
                raise PerformanceError('Allocation history is malformed; preserve the state file before retrying.')
            history.append(change)
            self._save(state)
            try:
                result = _set_priority_verified(record, priority)
            except Exception as exc:
                change['status'], change['error'] = 'failed', str(exc)
                self._save(state)
                raise
            change['previous'] = result['old_priority_value']
            change['status'] = 'applied'
            self._save(state)
            return {**result, 'change_id': change_id}

    def restore_priority(self, change_id: str) -> dict[str, Any]:
        with _STATE_LOCK:
            state = self._load()
            matches = [change for change in state.get('allocation_history', []) if change.get('id') == change_id and change.get('kind') == 'priority']
            if len(matches) != 1 or matches[0].get('status') not in {'applied', 'pending', 'failed', 'restore_pending'}:
                raise PerformanceError('This priority change has no pending restoration.')
            change = matches[0]
            process = _fresh(change['record'])
            _service_ids(force=True)
            reason = _base_protection(process, for_sleep=False)
            if reason:
                raise PerformanceError(reason)
            previous = int(change['previous'])
            if previous not in _PRIORITY_NAMES:
                raise PerformanceError('The recorded original priority class is invalid.')
            if _PLATFORM == 'win32' and previous == 0x100 and not _trusted_codex_exe(process.exe()):
                raise PerformanceError('Realtime restoration is limited to a verified Codex executable with a recorded original Realtime class.')
            change['status'] = 'restore_pending'
            self._save(state)
            process = _fresh(change['record'])
            process.nice(previous)
            change['status'], change['error'] = 'restored', ''
            self._save(state)
            return {'pid': process.pid, 'priority': _PRIORITY_NAMES[previous], 'change_id': change_id, 'restored': True}

    def restore_allocation(self, change_id: str) -> dict[str, Any]:
        with _STATE_LOCK:
            matches = [change for change in self._load().get('allocation_history', []) if change.get('id') == change_id]
            if len(matches) != 1:
                raise PerformanceError('The selected allocation record is unknown.')
            if matches[0]['kind'] == 'priority':
                return self.restore_priority(change_id)
            if matches[0]['kind'] == 'affinity':
                return self.restore_affinity(change_id)
            raise PerformanceError('Unsupported allocation record.')

    def performance_profile(self, priority: str = 'High', include_tool_helpers: bool = False, root_pid: int | None = None) -> dict[str, Any]:
        return performance_profile(priority=priority, include_tool_helpers=include_tool_helpers, root_pid=root_pid, controller=self)

    def set_affinity(self, record: dict[str, Any], cores: Any) -> dict[str, Any]:
        if _PLATFORM == 'darwin':
            raise PerformanceError('macOS does not expose supported per-process CPU affinity. Priority and resource analysis remain available.')
        with _STATE_LOCK:
            selected = _validate_cores(cores)
            process = _fresh(record)
            _service_ids(force=True)
            reason = _base_protection(process, for_sleep=False)
            if reason:
                raise PerformanceError(reason)
            previous = list(process.cpu_affinity())
            state = self._load()
            change_id = hashlib.sha256(os.urandom(32)).hexdigest()
            change = {'id': change_id, 'kind': 'affinity', 'record': _record(process), 'previous': previous,
                      'selected': selected, 'status': 'pending', 'time_utc': time.time(), 'error': ''}
            history = state.setdefault('allocation_history', [])
            if not isinstance(history, list):
                raise PerformanceError('Allocation history is malformed; preserve the state file before retrying.')
            history.append(change)
            self._save(state)
            try:
                process = _fresh(record)
                process.cpu_affinity(selected)
            except Exception as exc:
                change['status'], change['error'] = 'failed', str(exc)
                self._save(state)
                raise PerformanceError('The operating system rejected the selected CPU affinity.') from exc
            change['status'] = 'applied'
            self._save(state)
            return {'pid': process.pid, 'affinity': selected, 'previous': previous, 'change_id': change_id, 'applied': True}

    def allocation_history(self) -> list[dict[str, Any]]:
        with _STATE_LOCK:
            history = self._load().get('allocation_history', [])
            if not isinstance(history, list):
                raise PerformanceError('Allocation history is malformed.')
            return [{**change, 'Id': change['id'], 'Kind': change['kind'].capitalize(),
                     'Target': f"{change['record'].get('name', 'Application')} / {change['record'].get('pid')}",
                     'TimeUtc': datetime.fromtimestamp(change['time_utc'], timezone.utc).isoformat(),
                     'Status': change['status'].capitalize(), 'source': 'python-allocation',
                     'Undo': {'record': change['record'], 'previous': change['previous']}} for change in history]

    def restore_affinity(self, change_id: str) -> dict[str, Any]:
        if _PLATFORM == 'darwin':
            raise PerformanceError('macOS does not expose supported per-process CPU affinity.')
        with _STATE_LOCK:
            state = self._load()
            matches = [change for change in state.get('allocation_history', []) if change.get('id') == change_id and change.get('kind') == 'affinity']
            if len(matches) != 1 or matches[0].get('status') not in {'applied', 'pending', 'failed', 'restore_pending'}:
                raise PerformanceError('This affinity change has no pending restoration.')
            change = matches[0]
            process = _fresh(change['record'])
            _service_ids(force=True)
            reason = _base_protection(process, for_sleep=False)
            if reason:
                raise PerformanceError(reason)
            previous = _validate_cores(change['previous'])
            change['status'] = 'restore_pending'
            self._save(state)
            process = _fresh(change['record'])
            process.cpu_affinity(previous)
            change['status'], change['error'] = 'restored', ''
            self._save(state)
            return {'pid': process.pid, 'affinity': previous, 'change_id': change_id, 'restored': True}

    def wake_all(self) -> list[dict[str, Any]]:
        results = []
        with _STATE_LOCK:
            for entry in self.list_suspended():
                record = entry["record"]
                try:
                    results.append(self.wake_process(record))
                except Exception as exc:
                    results.append({"pid": record.get("pid"), "status": "needs_attention", "error": str(exc), "changed": False})
        return results


def performance_profile(priority: str = "High", include_tool_helpers: bool = False, root_pid: int | None = None, controller: PerformanceController | None = None) -> dict[str, Any]:
    """Explicitly apply a profile to installation-verified Codex processes only.

    Tool helpers are opt-in and must descend from a verified Codex executable.
    Optimus itself, Windows processes, services and security processes stay protected.
    """
    if _PLATFORM != 'win32':
        raise PerformanceError('The verified Codex installation profile is Windows-only. Select an exact owned process in native resource controls instead.')
    if priority not in _PRIORITY_VALUES:
        raise PerformanceError(_priority_choice_message())
    selected_root = None
    if root_pid is not None:
        selected_root = psutil.Process(int(root_pid))
        if not _trusted_codex_exe(selected_root.exe()):
            raise PerformanceError("The selected root is not in a verified Codex installation path.")
    results, skipped = [], []
    for process in psutil.process_iter():
        try:
            candidate_names = _CODEX_RUNTIME_NAMES | {"chatgpt.exe"} | (_TOOL_NAMES if include_tool_helpers else set())
            if process.name().casefold() not in candidate_names:
                continue
            executable = process.exe()
            trusted_executable = _trusted_codex_exe(executable)
            parents = process.parents() if selected_root is not None or include_tool_helpers else []
            if selected_root is not None and process.pid != selected_root.pid and not any(parent.pid == selected_root.pid and math.isclose(parent.create_time(), selected_root.create_time(), abs_tol=0.000001, rel_tol=0) for parent in parents):
                continue
            if not trusted_executable:
                if not include_tool_helpers or process.name().casefold() not in _TOOL_NAMES or not any(_trusted_codex_exe(parent.exe()) for parent in parents):
                    continue
            if process.pid == os.getpid():
                skipped.append({"pid": process.pid, "reason": "Optimus itself is protected."})
                continue
            record = _record(process)
            results.append((controller.set_process_priority if controller else set_process_priority)(record, priority))
        except (psutil.Error, PerformanceError, OSError, ValueError) as exc:
            skipped.append({"pid": int(process.pid), "reason": str(exc)})
    return {"profile": priority, "include_tool_helpers": include_tool_helpers, "results": results, "skipped": skipped,
            "note": "Explicit request only. Existing launch watchers are preserved and may reset a later process priority. No startup, service, affinity, or power setting is changed."}


_default_controller: PerformanceController | None = None


def _controller() -> PerformanceController:
    global _default_controller
    with _STATE_LOCK:
        if _default_controller is None:
            _default_controller = PerformanceController()
        return _default_controller


def sleep_process(record: dict[str, Any]) -> dict[str, Any]:
    return _controller().sleep_process(record)


def wake_process(record: dict[str, Any]) -> dict[str, Any]:
    return _controller().wake_process(record)


def wake_all() -> list[dict[str, Any]]:
    return _controller().wake_all()


def list_suspended() -> list[dict[str, Any]]:
    return _controller().list_suspended()


def diagnose_priority_watchers() -> dict[str, Any]:
    """Read-only hints; never disables a launcher or exports its command line."""
    hints: list[dict[str, Any]] = []
    for process in psutil.process_iter():
        try:
            if process.name().casefold() not in {"powershell.exe", "pwsh.exe", "wscript.exe", "cscript.exe"}:
                continue
            for argument in process.cmdline()[1:]:
                script_name = ntpath.basename(str(argument).strip('"'))
                if script_name.casefold().endswith((".ps1", ".vbs", ".js")) and re.search(r"priority|codex.*(?:optimi|boost)|(?:optimi|boost).*codex", script_name, re.IGNORECASE):
                    hints.append({"pid": int(process.pid), "name": process.name(), "script_name": script_name,
                                  "evidence": "Possible priority-related script, inferred from its basename. Contents and behavior were not verified."})
                    break
        except (psutil.Error, OSError):
            continue
    configured_images = []
    if os.name == "nt":
        import winreg
        for image in ["ChatGPT.exe", *_CODEX_RUNTIME_NAMES]:
            key_path = "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Image File Execution Options\\" + image + "\\PerfOptions"
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path, 0, winreg.KEY_READ) as key:
                    value, _ = winreg.QueryValueEx(key, "CpuPriorityClass")
                configured_images.append({"image": image, "cpu_priority_class": int(value)})
            except OSError:
                continue
    return {"possible_watchers": hints, "image_priority_configuration": configured_images,
            "note": "Existing launch watchers and persistent image settings are preserved. They may override an explicit Optimus priority choice. A script basename is a clue, not proof of its behavior."}


def _validate_cores(cores: Any) -> list[int]:
    count = max(1, int(psutil.cpu_count(logical=True) or 1))
    if count > 64 and _PLATFORM == 'win32':
        raise PerformanceError('Affinity editing is disabled on systems with more than 64 logical CPUs; keep Windows scheduling.')
    if isinstance(cores, str):
        cores = cores.split(',')
    try:
        values = list(cores)
        if any(isinstance(core, bool) or not re.fullmatch(r'\d+', str(core).strip()) for core in values):
            raise ValueError('Invalid core')
        selected = sorted({int(str(core).strip()) for core in values})
    except (TypeError, ValueError) as exc:
        raise PerformanceError('Choose comma-separated whole CPU numbers.') from exc
    if not selected or selected[0] < 0 or selected[-1] >= count:
        raise PerformanceError(f'Choose at least one logical CPU, numbered 0 through {count-1}.')
    return selected


def set_affinity(record: dict[str, Any], cores: Any) -> dict[str, Any]:
    return _controller().set_affinity(record, cores)


def restore_affinity(change_id: str) -> dict[str, Any]:
    return _controller().restore_affinity(change_id)


def allocation_history() -> list[dict[str, Any]]:
    return _controller().allocation_history()


def get_process_details(record: dict[str, Any], *, include_expensive_counts: bool = False) -> dict[str, Any]:
    """Explicit best-effort process details without collecting command lines."""
    process = _fresh(record)
    details = _record(process)
    notes = []

    def inspect(label: str, action: Any, default: Any = None) -> Any:
        try:
            return action()
        except (psutil.Error, OSError, AttributeError, NotImplementedError, ValueError) as exc:
            notes.append(label + ': ' + type(exc).__name__)
            return default

    def identity(item: Any) -> dict[str, Any]:
        return {'pid': int(item.pid), 'name': inspect('name', item.name, 'Unavailable'),
                'exe': inspect('path', item.exe, ''), 'create_time': inspect('creation time', item.create_time)}

    parent = inspect('parent', lambda: process.parent())
    details['parent'] = identity(parent) if parent else None
    details['children'] = [identity(child) for child in inspect('children', lambda: process.children(recursive=False), [])]
    times = inspect('CPU times', process.cpu_times)
    details['cpu_times'] = dict(times._asdict()) if times and hasattr(times, '_asdict') else None
    details['open_file_count'] = None
    details['network_connection_count'] = None
    if include_expensive_counts:
        details['open_file_count'] = inspect('open files', lambda: len(process.open_files()))
        details['network_connection_count'] = inspect('network connections', lambda: len(process.net_connections(kind='all')))
    else:
        notes.append('Open-file and network-connection counts were not collected because enumeration can be slow or unavailable. These are counts unavailable, not zero.')
    details['status'] = inspect('status', process.status)
    details['services'] = []
    details['services_supported'] = _PLATFORM == 'win32'
    if _PLATFORM != 'win32':
        notes.append('Windows service registration does not apply on this platform; parent/executable details remain available.')
    else:
        try:
            for service in psutil.win_service_iter():
                try:
                    if service.pid() == process.pid:
                        details['services'].append({'name': service.name(), 'display_name': service.display_name(), 'status': service.status(), 'pid': process.pid})
                except psutil.Error as exc:
                    notes.append('Service details: ' + type(exc).__name__)
        except (psutil.Error, AttributeError, OSError) as exc:
            notes.append('Service inventory: ' + type(exc).__name__)
    # Fail if PID/executable identity changed while details were being collected.
    _fresh(record)
    details['notes'] = notes
    details['measurement_note'] = 'CPU percent is normalized over all logical CPUs; first observation may be zero. I/O counters are lifetime totals, not rates. Suspended applications can retain memory. Process command lines are not returned.'
    return details


def set_process_priority(record: dict[str, Any], priority: str) -> dict[str, Any]:
    return _controller().set_process_priority(record, priority)


def restore_priority(change_id: str) -> dict[str, Any]:
    return _controller().restore_priority(change_id)


def restore_allocation(change_id: str) -> dict[str, Any]:
    return _controller().restore_allocation(change_id)
