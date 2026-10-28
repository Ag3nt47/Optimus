"""Capture the real Qt UI with inert sample data; never construct a real Backend.

Windows example (PowerShell, from the source root)::

    $env:PYTHONPATH = 'src'
    $env:QT_QPA_PLATFORM = 'offscreen'
    $env:TEMP = 'D:/Agent47/SystemTempGuard/Temp'
    $env:TMP = $env:TEMP
    & 'D:/local-security-ai/AngeronaSuite/venv/Scripts/python.exe' tools/capture_demo.py `
        --ffmpeg 'path/to/ffmpeg.exe'

Requires PySide6 and an FFmpeg executable with libx264 and GIF support. The
optional imageio-ffmpeg package supplies an executable, without Pillow. Capture
uses QWidget.grab(); a surrounding QPainter title/footer labels every frame.
The primary button is connected to a local fixture handoff, not a workflow
engine. Actual production progress rendering and NavigationHub acknowledgements
are exercised. Metrics, packages, bytes and verification receipts are simulated;
they are not measured cleanup, performance, or proof of native operations.

Sandbox uses a mock workspace and an excerpt of the public source, with every
write/export/restart method forbidden. No installed Optimus Data is read. Only
the requested output directory and a disposable temporary directory are written.
Do not remove the sample-data labels when sharing these illustrative previews.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tools')]

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QImage, QImageReader, QPainter, QPen
from PySide6.QtWidgets import QApplication
from critical_review import FixtureBackend, drain, measure_dashboard
from optimus import sandbox
from optimus.ui import OptimusWindow, OptimizationProgressWindow

WIDTH, HEIGHT = 1120, 900
CLIENT_WIDTH, CLIENT_HEIGHT = 1080, 756
FPS, SECONDS = 12, 20
BASE = 'f23962099f3849290f01dc75b9658f6d5d2c98af'


def forbidden(*_args, **_kwargs):
    raise AssertionError('The sample-data capture cannot perform host operations.')


def encoder_path(argument):
    if argument:
        return str(Path(argument).resolve(strict=True))
    found = shutil.which('ffmpeg')
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError as error:
        raise SystemExit('Pass --ffmpeg with a local FFmpeg executable.') from error


def frame(widget, title, chapter, *, cursor=None, accent=None):
    """Composite a genuine widget grab into a visibly labelled demo frame."""
    assert widget.size().width() == CLIENT_WIDTH
    assert widget.size().height() == CLIENT_HEIGHT
    grabbed = widget.grab()
    assert grabbed.width() == CLIENT_WIDTH and grabbed.height() == CLIENT_HEIGHT
    picture = QImage(WIDTH, HEIGHT, QImage.Format_RGB32)
    picture.fill(QColor('#070b12'))
    painter = QPainter(picture)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(QColor('#e7f1ff'))
    painter.setFont(QFont('Segoe UI', 20, QFont.DemiBold))
    painter.drawText(QRect(22, 13, 735, 34), Qt.AlignVCenter, title)
    painter.setFont(QFont('Segoe UI', 10))
    painter.setPen(QColor('#92a6bf'))
    painter.drawText(QRect(24, 49, 740, 25), Qt.AlignVCenter, chapter)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor('#183449'))
    painter.drawRoundedRect(QRect(858, 22, 240, 37), 8, 8)
    painter.setPen(QColor('#9cddff'))
    painter.setFont(QFont('Segoe UI', 11, QFont.Bold))
    painter.drawText(QRect(858, 22, 240, 37), Qt.AlignCenter, 'SAMPLE DATA DEMO')
    painter.setPen(QPen(QColor('#293e54'), 1))
    painter.setBrush(Qt.NoBrush)
    painter.drawRoundedRect(QRect(19, 87, 1082, 758), 5, 5)
    painter.drawPixmap(20, 88, grabbed)
    if accent is not None:
        rectangle = accent.rect().translated(accent.mapTo(widget, QPoint())).translated(20, 88)
        painter.setPen(QPen(QColor('#79d5ff'), 2))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(rectangle.adjusted(-3, -3, 3, 3), 8, 8)
    if cursor is not None:
        point = cursor.mapTo(widget, cursor.rect().center()) + QPoint(20, 88)
        painter.setPen(QPen(QColor('#d4f0ff'), 2))
        painter.setBrush(QColor(61, 168, 255, 30))
        painter.drawEllipse(point, 17, 17)
        painter.drawEllipse(point, 4, 4)
    painter.setFont(QFont('Segoe UI', 10))
    painter.setPen(QColor('#a7b7ca'))
    painter.drawText(QRect(24, 858, 1080, 24), Qt.AlignVCenter,
        'Optimus 0.12.7  /  Real Qt widgets  /  Simulated data; no measured cleanup or performance results')
    painter.end()
    return picture


def make_state(fraction):
    return {
        'stage': 'Review approved sample maintenance', 'percentage': 28 + 38 * fraction,
        'stage_status': 'running',
        'description': 'Sample workflow only. No cleanup, installer, security scan or system setting is being applied.',
        'resource_totals': resource_totals(removed_known=True, removed_bytes=25165824, removed_files=48),
        'item_progress': {'key': 'sample-cache', 'label': 'Example cache', 'phase': 'cleanup',
                          'percent': round(20 + 65 * fraction)},
        'jobs': [
            {'id': 'software_updates', 'title': 'App updates', 'status': 'installing',
             'current': 1, 'total': 4, 'description': 'Sample package: Example.Editor',
             'resource_totals': resource_totals(installed_known=True, installed_items=1,
                 downloaded_known=True, downloaded_bytes=41943040),
             'item_progress': {'key': 'sample-editor', 'label': 'Example.Editor', 'phase': 'install',
                               'percent': round(12 + 75 * fraction)}},
            {'id': 'driver_updates', 'title': 'Driver updates', 'status': 'waiting_for_installer',
             'current': 0, 'total': 2, 'description': 'Sample driver waits for the installer lane.',
             'item_progress': {'key': 'sample-driver', 'label': 'Example.Network',
                               'phase': 'waiting', 'percent': None}},
            {'id': 'security_scan', 'title': 'Virus & malware scan', 'status': 'running',
             'operation_kind': 'antivirus_scan', 'percentage': round(25 + 50 * fraction),
             'description': 'Sample scan progress; no real security scan is running.',
             'item_progress': {'key': 'sample-scan', 'label': 'Example scan', 'phase': 'scan',
                               'percent': round(25 + 50 * fraction)}}],
    }


def resource_totals(**known):
    """Complete producer snapshot; unknown quantities stay explicitly unknown."""
    value = dict(removed_bytes=0, removed_files=0, downloaded_bytes=0, installed_items=0,
        removed_known=False, downloaded_known=False, downloaded_estimated=False,
        downloaded_partial=False, installed_known=False, installed_reported=False)
    value.update(known)
    return value


def complete(progress, backend, cursors):
    for cursor in cursors.values():
        cursor.finish('skipped' if cursor.id == 'software_updates' else 'completed',
                      mutation_started=False, outcome_known=True)
    backend.navigation.end()
    progress.worker_cards['software_updates'].update_state({
        'status': 'needs_review', 'current': 4, 'total': 4,
        'presentation_status': 'finished_with_skips', 'worker_finished': True,
        'continuation_blocked': False, 'scan_complete': True,
        'description': 'Sample receipt: Example.Editor skipped; other sample items returned.'})
    progress.worker_cards['driver_updates'].update_state({
        'status': 'completed', 'current': 2, 'total': 2,
        'description': 'Sample device checks complete; no driver installed.'})
    progress.worker_cards['security_scan'].update_state({
        'status': 'completed', 'operation_kind': 'antivirus_scan', 'completion_verified': True,
        'percentage': 100, 'description': 'Simulated completed scan receipt; no real scan.'})
    report = {
        'id': 'a' * 32, 'status': 'completed', 'cancelled': False, 'stages': [],
        'resource_totals': resource_totals(removed_known=True, removed_bytes=25165824, removed_files=48),
        'final_verification': {
            'scope': 'SIMULATED sample receipts only; no host changes or measured results.',
            'status': 'needs_review',
            'counts': {'confirmed': 3, 'needs_review': 1, 'unverified': 0, 'not_applicable': 2},
            'coverage_complete': False,
            'checks': ['Sample cache receipt', 'Sample app receipt', 'Sample settings readback'],
            'remaining': ['Review the skipped Example.Editor item'],
        },
        'summary': 'Illustrative completed workflow. Outcomes are sample data.',
    }
    progress.finish(report)
    progress.set_auto_continue_plan({
        'actions': [{'id': 'sample-recheck', 'title': 'Recheck the sample application offer'}],
        'manual_items': [{'id': 'sample-skipped', 'title': 'Skipped Example.Editor',
                          'reason': 'Review the recorded skip before a new run.'}],
        'summary': 'Sample scope for illustration only.',
    }, ready=True)
    progress.auto_continue_button.clicked.disconnect()
    progress.auto_continue_button.clicked.connect(forbidden)


def capture(output, scratch):
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    for filename in ('segoeui.ttf', 'segoeuib.ttf', 'seguisb.ttf', 'consola.ttf', 'CascadiaMono.ttf'):
        candidate = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts' / filename
        if candidate.is_file():
            QFontDatabase.addApplicationFont(str(candidate))
    app.setFont(QFont('Segoe UI', 10))
    backend = FixtureBackend(scratch / 'inert-state', 'win32')
    backend.settings['reduce_motion'] = False
    backend.settings['spyware_scan_enabled'] = True
    backend.optimize = Mock(side_effect=forbidden)
    backend.source_root = Mock(return_value=ROOT)
    backend.navigation.begin('b' * 32)
    cursors = {}
    for identity, title in (('maintenance', 'Maintenance'), ('software_updates', 'App updates'),
                            ('driver_updates', 'Driver updates'), ('security_scan', 'Virus & malware scan')):
        cursor = backend.navigation.cursor(identity, title)
        cursor.begin_item(identity + ':previous', 'Previous sample check', kind='check')
        cursor.finish('completed', mutation_started=False, outcome_known=True)
        cursor.begin_item(identity + ':current', 'Current sample item', kind='mutation')
        if identity == 'software_updates':
            cursor.phase('installing', mutation_started=True)
        cursors[identity] = cursor
    source = (ROOT / 'src/optimus/ui.py').read_text(encoding='utf-8').splitlines()
    excerpt = '# SAMPLE-DATA DEMO: excerpt of src/optimus/ui.py\n# No edits are saved or executed during capture.\n\n'
    source_start = next(index for index, line in enumerate(source)
                        if line.strip().startswith('self.canvas = StarfieldCanvas'))
    excerpt += '\n'.join(source[source_start:source_start + 61]) + '\n'
    workspace = SimpleNamespace(root=Path('Sample-data workspace'), source_root=ROOT,
        files=Mock(return_value=['src/optimus/ui.py', 'src/optimus/sandbox.py',
            'src/optimus/workflow.py', 'src/optimus/navigation.py', 'src/optimus/item_progress.py',
            'src/optimus/widgets/theme.py', 'src/optimus/widgets/worker_grid.py',
            'src/optimus/widgets/compact_worker_ring.py', 'pyproject.toml', 'README.md']),
        read=Mock(return_value=excerpt),
        **{key: Mock(side_effect=forbidden) for key in
           ('save', 'revert', 'restore_original', 'versions', 'export', 'validate_all')})
    safety = {}
    with ExitStack() as stack:
        stack.enter_context(patch('optimus.widgets.window_geometry.available_screen_geometry',
                                  return_value=QRect(0, 0, 1920, 1080)))
        source_factory = stack.enter_context(patch.object(sandbox, 'SourceWorkspace', return_value=workspace))
        process_guard = stack.enter_context(patch.object(sandbox.subprocess, 'Popen', side_effect=forbidden))
        main = OptimusWindow(backend, smoke_test=True)
        main.resize(CLIENT_WIDTH, CLIENT_HEIGHT)
        main.show()
        drain(app)
        progress = OptimizationProgressWindow(main, threading.Event())
        progress.resize(CLIENT_WIDTH, CLIENT_HEIGHT)
        assert progress._live_metrics_service is None
        progress.configure_workers(backend.preferences())
        editor = sandbox.SandboxWindow(backend)
        editor.resize(CLIENT_WIDTH, CLIENT_HEIGHT)
        editor.splitter.setSizes([240, 830])
        editor.editor.setFont(QFont('Consolas', 11))
        # Only this fixture dispatch is reachable from the captured primary click.
        main.main_buttons[0].clicked.disconnect()
        main.main_buttons[0].clicked.connect(progress.show)
        screenshot_indices = {12: 'optimus-main.png', 77: 'optimus-workers.png',
                              156: 'optimus-completed.png', 218: 'optimus-sandbox.png'}
        dashboard = None
        for index in range(FPS * SECONDS):
            t = index / FPS
            widget, cursor, accent = main, None, None
            title, chapter = 'Your configured tools. One place to start.', '01 / Launch the saved one-click workflow'
            if t < 3:
                main.canvas.phase = t * .12
                main.canvas.update()
                if t >= 2:
                    cursor = main.main_buttons[0]
                    accent = cursor
            elif t < 11:
                if index == 3 * FPS:
                    main.main_buttons[0].click()
                widget = progress
                fraction = min(1, (t - 3) / 8)
                progress.update_progress(make_state(fraction))
                progress._refresh_resource_comparison()
                progress.worker_navigation.refresh()
                if index == 5 * FPS:
                    progress.details_toggle.setChecked(True)
                elif index == 8 * FPS:
                    progress.details_toggle.setChecked(False)
                title = 'Four workers. Clear progress at every level.'
                chapter = '02 / Current-item rings and overall queue progress stay distinct'
                if t >= 8:
                    if index == 8 * FPS:
                        progress.worker_cards['software_updates'].navigation_buttons['next'].click()
                        assert len(backend.navigation_requests) == 1
                        assert cursors['software_updates'].pending['action'] == 'next'
                        assert 'Skip requested' in progress.worker_cards['software_updates'].status.text()
                    title = 'Skip one item at a safe boundary.'
                    chapter = '03 / The request is acknowledged while other workers continue'
                    accent = progress.worker_cards['software_updates']
                    cursor = accent.navigation_buttons['next'] if t < 8.5 else None
            elif t < 16:
                if index == 11 * FPS:
                    complete(progress, backend, cursors)
                widget = progress
                title = 'Review the evidence and the next action scope.'
                chapter = '04 / Completed stages, skipped items, verification and remaining choices'
            else:
                if index == 16 * FPS:
                    progress.hide()
                    editor.show()
                widget = editor
                title = 'A spacious Sandbox for the full source.'
                chapter = '05 / Source tree, syntax highlighting, history and ready-checked restart controls'
            drain(app)
            widget.resize(CLIENT_WIDTH, CLIENT_HEIGHT)
            drain(app)
            if index == 77:
                dashboard = measure_dashboard(progress)
                assert all(dashboard[key] for key in ('all_visible', 'essentials', 'no_overlap',
                                                      'no_internal_overlap', 'two_by_two', 'footer_clear'))
            picture = frame(widget, title, chapter, cursor=cursor, accent=accent)
            assert picture.pixelColor(900, 30).name() == '#183449', 'Sample badge must remain on every frame'
            assert picture.save(str(scratch / f'frame-{index:04d}.png'))
            if index in screenshot_indices:
                assert picture.save(str(output / screenshot_indices[index]))
        assert not backend.optimize.called
        assert not process_guard.called
        assert not any(getattr(workspace, key).called for key in
                       ('save', 'revert', 'restore_original', 'versions', 'export', 'validate_all'))
        assert not backend.saved
        safety = {'real_backend_constructed': False, 'host_maintenance_applied': False,
            'live_metrics_sampler_constructed': False, 'sandbox_writes_or_restart': False,
            'fixture_navigation_skip_requests': len(backend.navigation_requests),
            'sandbox_workspace_mocked': source_factory.called, 'worker_layout': dashboard}
        for window in (editor, progress, main):
            window.hide()
            window.deleteLater()
        drain(app)
    return safety


def encode(ffmpeg, scratch, output):
    def run(*arguments):
        command = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', *map(str, arguments)]
        subprocess.run(command, check=True, capture_output=True, timeout=120)
    run('-framerate', FPS, '-i', scratch / 'frame-%04d.png', '-c:v', 'libx264',
        '-crf', 21, '-preset', 'slow', '-pix_fmt', 'yuv420p', '-movflags', '+faststart',
        '-an', output / 'optimus-walkthrough.mp4')
    # Compress the entire 20-second walkthrough to 10 seconds at 8 FPS.
    run('-framerate', FPS, '-i', scratch / 'frame-%04d.png',
        '-filter_complex',
        '[0:v]setpts=0.5*PTS,fps=8,split[a][b];[a]palettegen=max_colors=192:stats_mode=diff[p];'
        '[b][p]paletteuse=dither=bayer:bayer_scale=3:diff_mode=rectangle',
        '-loop', 0, output / 'optimus-demo.gif')
    # FFmpeg's own decoder validates every frame and reports the actual metadata.
    decoded = {}
    for name in ('optimus-walkthrough.mp4', 'optimus-demo.gif'):
        result = subprocess.run([ffmpeg, '-hide_banner', '-nostats', '-i', str(output / name),
            '-map', '0:v:0', '-progress', 'pipe:1', '-f', 'null', '-'],
            check=True, capture_output=True, text=True, timeout=120)
        count = int(re.findall(r'^frame=(\d+)$', result.stdout, re.MULTILINE)[-1])
        duration = re.search(r'Duration: (\d+):(\d+):([\d.]+)', result.stderr)
        seconds = int(duration[1]) * 3600 + int(duration[2]) * 60 + float(duration[3])
        codec = re.search(r'Video: (\w+)', result.stderr)[1]
        dimensions = re.search(r'(\d+)x(\d+) \[SAR', result.stderr)
        assert count == (240 if name.endswith('.mp4') else 80)
        assert (int(dimensions[1]), int(dimensions[2])) == (WIDTH, HEIGHT)
        assert codec == ('h264' if name.endswith('.mp4') else 'gif')
        assert abs(seconds - (20 if name.endswith('.mp4') else 10)) <= .02
        decoded[name] = dict(decoded_frames=count, duration_seconds=seconds,
                             codec=codec, audio_stream='Audio:' in result.stderr)
    # Keep a temporary contact sheet for human visual inspection, outside Git.
    run('-i', output / 'optimus-walkthrough.mp4', '-vf',
        r'select=eq(n\,12)+eq(n\,77)+eq(n\,108)+eq(n\,156)+eq(n\,218),scale=560:450,tile=3x2',
        '-frames:v', 1, scratch / 'encoded-contact-sheet.png')
    return decoded


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'docs/media')
    parser.add_argument('--scratch', type=Path, help='Keep temporary PNG frames and a decoded contact sheet here.')
    parser.add_argument('--ffmpeg', help='Existing FFmpeg binary; no automatic dependency installation.')
    args = parser.parse_args(argv)
    ffmpeg = encoder_path(args.ffmpeg)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        scratch = args.scratch.resolve() if args.scratch else Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='optimus-demo-')))
        scratch.mkdir(parents=True, exist_ok=True)
        safety = capture(output, scratch)
        decoded = encode(ffmpeg, scratch, output)
        assets = []
        for path in sorted(output.iterdir()):
            if path.suffix not in ('.png', '.gif', '.mp4'):
                continue
            assert path.stat().st_size <= 10_000_000, f'{path.name} exceeds 10 MB'
            row = {'file': path.name, 'bytes': path.stat().st_size,
                   'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                   'width': WIDTH, 'height': HEIGHT}
            if path.suffix == '.png':
                reader = QImageReader(str(path))
                assert not reader.read().isNull()
                row['codec'] = 'PNG'
            else:
                row.update(codec='H.264 / AVC' if path.suffix == '.mp4' else 'GIF89a',
                    pixel_format='yuv420p' if path.suffix == '.mp4' else 'pal8',
                    fps=FPS if path.suffix == '.mp4' else 8,
                    duration_seconds=SECONDS if path.suffix == '.mp4' else 10.01,
                    frames=FPS * SECONDS if path.suffix == '.mp4' else 80,
                    audio=False)
            assets.append(row)
        manifest = {'schema': 1, 'version': '0.12.7', 'source_commit': BASE,
            'capture': 'Actual QWidget.grab() renders with labelled surrounding title/footer',
            'fixture_scope': 'In-memory backend, navigation and mock Sandbox; all metrics and outcomes are simulated. No measured cleanup, performance or native-operation proof.',
            'primary_button': 'Connected only to fixture progress-window handoff',
            'source_excerpt': 'Sandbox shows an excerpt of public src/optimus/ui.py, never saved or executed',
            'raccoon': 'Bundled decorative asset is visible in its production Details section during seconds 5-8; see src/optimus/assets/RACCOON-NOTICE.txt.',
            'safety': safety, 'assets': assets,
            'encoder': subprocess.run([ffmpeg, '-version'], capture_output=True, text=True, check=True).stdout.splitlines()[0],
            'decoder_verification': decoded,
            'validation': 'All PNGs decoded through Qt; both animations fully decoded through FFmpeg; human inspection of screenshots and encoded contact sheet required.'}
        (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'output': str(output), 'scratch': str(scratch), 'assets': assets}, indent=2))


if __name__ == '__main__':
    main()
