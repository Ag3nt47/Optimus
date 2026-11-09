"""Recognize wallpaper and Store-managed state without opening or changing it.

These locations are never disposable cleanup or overwrite targets. Match lexical
components before filesystem traversal so relocated packages and redirected TEMP
cannot turn wallpaper data into an approved cache root.
"""
from __future__ import annotations

import os
import re
import sys
import time
from functools import lru_cache

_cached_wallpapers = ()
_cached_at = -float('inf')
_cached_monitors = ()
_monitors_at = -float('inf')


def _monitor_wallpapers():
    """Read each active monitor through IDesktopWallpaper's first four methods."""
    import ctypes
    import uuid
    ole = ctypes.WinDLL('ole32')
    ole.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    ole.CoInitializeEx.restype = ctypes.c_long
    # RPC_E_CHANGED_MODE means this thread already has a usable COM apartment.
    initialized = ole.CoInitializeEx(None, 2)
    if initialized not in (0, 1, -2147417850):
        return ()
    pointer = ctypes.c_void_p()
    ole.CoCreateInstance.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                                   ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    ole.CoCreateInstance.restype = ctypes.c_long
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    clsid = (ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID('C2CF3110-460E-4FC1-B9D0-8A1C0C9CC4BD').bytes_le)
    iid = (ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID('B92B56A9-8B55-4E14-9A89-0199BBB6F93B').bytes_le)
    result = []
    try:
        if ole.CoCreateInstance(clsid, None, 4, iid, ctypes.byref(pointer)) != 0:
            return ()
        table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        get_count = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint))(table[6])
        get_monitor = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p))(table[5])
        get_wallpaper = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(table[4])
        count = ctypes.c_uint()
        if get_count(pointer, ctypes.byref(count)) == 0:
            for index in range(min(count.value, 64)):
                monitor, wallpaper = ctypes.c_void_p(), ctypes.c_void_p()
                try:
                    if get_monitor(pointer, index, ctypes.byref(monitor)) == 0 and get_wallpaper(pointer, monitor, ctypes.byref(wallpaper)) == 0 and wallpaper.value:
                        value = ctypes.wstring_at(wallpaper)
                        if value:
                            result.append(value)
                finally:
                    if monitor.value:
                        ole.CoTaskMemFree(monitor)
                    if wallpaper.value:
                        ole.CoTaskMemFree(wallpaper)
        return tuple(result)
    finally:
        if pointer.value:
            release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(table[2])
            release(pointer)
        if initialized in (0, 1):
            ole.CoUninitialize()


def current_wallpaper_paths(*, refresh=False) -> tuple[str, ...]:
    """Read current/recent image paths; never write desktop or package settings."""
    global _cached_wallpapers, _cached_at, _cached_monitors, _monitors_at
    if sys.platform != 'win32':
        return ()
    if not refresh and time.monotonic() - _cached_at < 1:
        return _cached_wallpapers
    import ctypes
    import winreg
    values = set(_cached_wallpapers)
    # Shell COM activation costs tens of milliseconds on some PCs. Throttle
    # that read to once per second; SPI and registry are still refreshed at
    # every final deletion/overwrite boundary, including per-monitor caches.
    if time.monotonic() - _monitors_at >= 1:
        try:
            _cached_monitors = _monitor_wallpapers() or _cached_monitors
        except (OSError, AttributeError, ValueError):
            pass
        _monitors_at = time.monotonic()
    values.update(_cached_monitors)
    buffer = ctypes.create_unicode_buffer(32768)
    if ctypes.windll.user32.SystemParametersInfoW(0x73, len(buffer), buffer, 0) and buffer.value:
        values.add(buffer.value)
    for key_path in (r'Control Panel\Desktop', r'Software\Microsoft\Windows\CurrentVersion\Explorer\Wallpapers'):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                for index in range(winreg.QueryInfoKey(key)[1]):
                    name, value, kind = winreg.EnumValue(key, index)
                    if (name.casefold() == 'wallpaper' or name.startswith('BackgroundHistoryPath')) and kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) and value:
                        values.add(os.path.expandvars(value))
                    elif name.startswith('TranscodedImageCache') and kind == winreg.REG_BINARY and len(value) > 24:
                        value = value[24:].decode('utf-16-le', errors='ignore').split('\0', 1)[0]
                        if value and os.path.isabs(value):
                            values.add(value)
        except OSError:
            continue
    _cached_wallpapers = tuple(sorted(values))
    _cached_at = time.monotonic()
    _canonical_wallpapers.cache_clear()
    return _cached_wallpapers


def _canonical(path):
    # Resolve read-only before comparing: a moved user's Pictures directory may
    # itself be a junction. Deletion still independently rejects indirect paths.
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


@lru_cache(maxsize=4)
def _canonical_wallpapers(paths):
    return frozenset(_canonical(value) for value in paths)


def _named_wallpaper_path(path) -> bool:
    parts = tuple(part.casefold() for part in re.split(r'[\\/]+', os.fspath(path)) if part)
    if any('bingwallpaper' in re.sub(r'[^a-z0-9]', '', part) for part in parts):
        return True
    if any(part in {'windowsapps', 'wpsystem'} for part in parts):
        return True
    if any(parts[index:index + 3] in {
        ('microsoft', 'windows', 'themes'), ('windows', 'web', 'wallpaper')
    } for index in range(len(parts) - 2)):
        return True
    return False


def protected_wallpaper_path(path) -> bool:
    if _named_wallpaper_path(path):
        return True
    canonical = _canonical(path)
    return _named_wallpaper_path(canonical) or canonical in _canonical_wallpapers(tuple(current_wallpaper_paths()))


def protected_wallpaper_registry_target(path, name) -> bool:
    path = str(path).replace('/', '\\').strip('\\').casefold()
    name = str(name).casefold()
    if path == r'control panel\desktop':
        return name in {'wallpaper', 'wallpaperstyle', 'tilewallpaper'} or name.startswith('transcodedimagecache')
    if path.endswith((r'\explorer\wallpapers', r'\policies\activedesktop')):
        return True
    if path.endswith(r'\policies\system'):
        return name in {'wallpaper', 'wallpaperstyle', 'nochangingwallpaper'}
    return path.endswith(r'\policies\explorer') and name in {'noactivedesktop', 'noactivedesktopchanges', 'forceactivedesktopon'}
