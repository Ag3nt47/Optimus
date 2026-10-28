"""Stream bounded WinGet output without interrupting an active installer."""
from __future__ import annotations

import codecs
from decimal import Decimal
import os
import re
import subprocess
import threading
import time


_ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)')
_BAR = r'[\s\u2580-\u259f\u2500-\u2501]*'
_PERCENT = re.compile(_BAR + r'(?P<percent>[0-9]{1,3}(?:\.[0-9]{1,2})?)\s*%')
_BYTES = re.compile(_BAR + r'(?P<done>[0-9]{1,12}(?:\.[0-9]{1,4})?)\s*(?P<unit>B|KB|MB|GB|TB)\s*/\s*'
    r'(?P<total>[0-9]{1,12}(?:\.[0-9]{1,4})?)\s*(?P<total_unit>B|KB|MB|GB|TB)')
_UNITS = {name: 1 << power for name, power in (('B', 0), ('KB', 10), ('MB', 20), ('GB', 30), ('TB', 40))}


class WinGetProgressParser:
    """Accept explicit native progress bars only within a recognized phase.

    Rounded native byte counters are explicitly marked estimated. WinGet
    formats current and maximum independently, using binary KB/MB/GB units.
    Arbitrary installer log percentages and unknown text remain indeterminate.
    """
    def __init__(self, callback):
        self.callback, self.phase, self.buffer, self.overflow = callback, 'checking', '', False
        self.byte_total, self.byte_total_invalid = None, False

    def _emit(self, phase, percent=None, *, estimated=False, byte_progress=None):
        self.phase = phase
        try:
            value = {'phase': phase, 'percent': percent}
            if estimated:
                value['estimated'] = True
            if byte_progress is not None:
                value.update(byte_progress)
            self.callback(value)
        except Exception:
            pass

    def _line(self, text):
        text = _ANSI.sub('', text).strip()
        if not text or text in ('-', '\\', '|', '/'):
            return
        if text.startswith('Downloading '):
            self.byte_total, self.byte_total_invalid = None, False
            self._emit('download')
        elif text in ('Starting package install...', 'Starting package installation...'):
            self._emit('install')
        elif text in ('Starting package uninstall...', 'Starting package uninstallation...'):
            # WinGet's uninstall handlers expose native progress through the
            # same reporter as installation. Generic EXE handlers may expose
            # only a spinner; keep those removals indeterminate.
            self._emit('uninstall')
        elif text == 'Successfully verified installer hash':
            self._emit('verify')
        elif self.phase in ('download', 'install', 'uninstall') and (match := _PERCENT.fullmatch(text)) is not None:
            value = float(match['percent'])
            # Native 100 is withheld until the caller has a successful result;
            # this parser never synthesizes a terminal success percentage.
            self._emit(self.phase, value if 0 <= value < 100 else None)
        elif self.phase == 'download' and (match := _BYTES.fullmatch(text)) is not None:
            # ExecutionProgress.cpp::OutputBytes formats each side separately.
            # Mixed units such as 512 KB / 1 GB are normal native output.
            done = int(Decimal(match['done']) * _UNITS[match['unit']])
            total = int(Decimal(match['total']) * _UNITS[match['total_unit']])
            if total <= 0 or not 0 <= done <= total <= 2 ** 50:
                self._emit('download')
                return
            identity = total
            if self.byte_total is not None and identity != self.byte_total:
                self.byte_total_invalid = True
            self.byte_total = identity
            if self.byte_total_invalid:
                self._emit('download')
            else:
                # These are rounded native transfer counters, never queue/time
                # estimates. Even equal displays cannot prove phase completion.
                self._emit('download', min(99.0, done * 100.0 / total), estimated=True,
                    byte_progress={'downloaded_bytes': done, 'download_total_bytes': total,
                                   'bytes_estimated': True, 'bytes_source': 'winget_native'})
        else:
            self._emit('working')

    def feed(self, text):
        for part in re.split(r'([\r\n])', text):
            if part in ('\r', '\n'):
                if not self.overflow:
                    self._line(self.buffer)
                self.buffer, self.overflow = '', False
            elif not self.overflow:
                self.buffer += part
                if len(self.buffer) > 8192:
                    self.buffer, self.overflow = '', True

    def finish(self):
        if self.buffer and not self.overflow:
            self._line(self.buffer)
        self.buffer = ''


class _Capture:
    def __init__(self, maximum=2 * 1024 ** 2):
        self.maximum, self.first, self.tail, self.size = maximum, '', '', 0
        self.lock = threading.Lock()

    def append(self, text):
        with self.lock:
            half = self.maximum // 2
            take = min(max(0, half - len(self.first)), len(text))
            self.first += text[:take]
            self.tail = (self.tail + text[take:])[-half:]
            self.size += len(text)

    def value(self):
        with self.lock:
            marker = '\n[WinGet output exceeded its capture bound; middle text omitted.]\n' if self.size > self.maximum else ''
            return self.first + marker + self.tail

    def complete(self):
        with self.lock:
            return self.size <= self.maximum


def run_winget_with_progress(arguments, *, timeout, progress_callback, environment=None):
    """Keep the same native arguments and capture result; never kill on a tick.

    At timeout, the caller retains its unknown-installer barrier. Reader threads
    continue draining the exact child without callbacks; no retry or replay is
    attempted and inherited vendor stdout handles must also reach EOF before a
    normal completion receipt is returned.
    """
    options = {'env': dict(os.environ, **environment)} if environment is not None else {}
    child = subprocess.Popen(arguments, shell=False, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), **options)
    output, errors, reporting = _Capture(), _Capture(), threading.Event()
    read_fault, ready, progress_lock, latest = threading.Event(), threading.Event(), threading.Lock(), []
    reporting.set()
    download_progress = []
    def remember(value):
        if reporting.is_set():
            with progress_lock:
                if value.get('phase') == 'download' and type(value.get('downloaded_bytes')) is int:
                    # Preserve a last measured receipt even when display ticks
                    # are coalesced or a vendor immediately starts installing.
                    if not download_progress or value['downloaded_bytes'] >= download_progress[0]['downloaded_bytes']:
                        download_progress[:] = [dict(value)]
                latest[:] = [value]
            ready.set()
    parser = WinGetProgressParser(remember)

    def dispatch():
        while reporting.is_set():
            ready.wait(0.1)
            ready.clear()
            with progress_lock:
                value = latest.pop() if latest else None
            if value is not None and reporting.is_set():
                try:
                    progress_callback(value)
                except Exception:
                    pass
    threading.Thread(target=dispatch, daemon=True).start()

    def read(stream, capture, observe=False):
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        try:
            while True:
                block = stream.read1(4096) if hasattr(stream, 'read1') else stream.read(4096)
                if not block:
                    break
                text = decoder.decode(block)
                capture.append(text)
                if observe and reporting.is_set():
                    parser.feed(text)
            text = decoder.decode(b'', final=True)
            capture.append(text)
            if observe and reporting.is_set():
                parser.feed(text)
                parser.finish()
        except Exception:
            read_fault.set()  # Never mistake missing lifecycle text for a clean result.
        finally:
            try:
                stream.close()
            except OSError:
                pass

    readers = [threading.Thread(target=read, args=(child.stdout, output, True), daemon=True),
               threading.Thread(target=read, args=(child.stderr, errors), daemon=True)]
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + timeout
    while True:
        try:
            returncode = child.poll()
        except OSError as error:
            reporting.clear()
            ready.set()
            raise RuntimeError('The started WinGet child state could not be verified; inspect its outcome before retrying.') from error
        if returncode is not None and not any(reader.is_alive() for reader in readers):
            reporting.clear()
            ready.set()
            result = subprocess.CompletedProcess(arguments, returncode, output.value(), errors.value())
            result.capture_complete = not read_fault.is_set() and output.complete() and errors.complete()
            with progress_lock:
                if download_progress:
                    result.download_progress = dict(download_progress[0])
            return result
        if time.monotonic() >= deadline:
            reporting.clear()
            ready.set()
            # Reap our exact child when it eventually exits. Never terminate a
            # WinGet wrapper that may still own or supervise a vendor installer.
            threading.Thread(target=child.wait, daemon=True).start()
            raise subprocess.TimeoutExpired(arguments, timeout, output=output.value(), stderr=errors.value())
        time.sleep(0.05)
