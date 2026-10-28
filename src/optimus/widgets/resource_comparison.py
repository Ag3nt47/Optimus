"""Read-only comparison of reported cleanup and transfer bytes for one run."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGridLayout, QLabel, QProgressBar, QSizePolicy, QWidget

from ..worker_totals import normalize_resource_totals


def comparison_snapshot(workers):
    """Combine absolute worker snapshots; repeated ticks never add work twice."""
    values = [normalize_resource_totals(value) for value in
              workers.values()] if isinstance(workers, dict) else []
    values = [value for value in values if value is not None]
    removed = sum(value['removed_bytes'] for value in values if value['removed_known'])
    files = sum(value['removed_files'] for value in values if value['removed_known'])
    downloaded = sum(value['downloaded_bytes'] for value in values if value['downloaded_known'])
    removal_known = any(value['removed_known'] for value in values)
    download_known = any(value['downloaded_known'] for value in values)
    total = removed + downloaded
    share = removed * 100 / total if removal_known and download_known and total else None
    return {'removed_bytes': removed, 'removed_files': files, 'downloaded_bytes': downloaded,
            'removed_known': removal_known, 'downloaded_known': download_known,
            'removed_percent': share, 'downloaded_percent': 100 - share if share is not None else None,
            'estimated': any(value['downloaded_estimated'] for value in values),
            'partial': any(value['downloaded_partial'] for value in values)}


def _bytes(value):
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'):
        if value < 1024 or unit == 'PiB':
            return f'{value:,} {unit}' if unit == 'B' else f'{value:.1f} {unit}'
        value /= 1024


class ResourceComparison(QWidget):
    """Two labelled sides stay readable even when one segment is very small."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('ResourceComparison')
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setHorizontalSpacing(12)
        self.removed = QLabel()
        self.downloaded = QLabel()
        self.note = QLabel()
        for label in (self.removed, self.downloaded, self.note):
            label.setTextFormat(Qt.PlainText)
            label.setWordWrap(True)
            label.setMinimumWidth(0)
            label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.removed.setStyleSheet('color:#52cf9a;')
        self.downloaded.setStyleSheet('color:#f4b860;')
        self.downloaded.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        layout.addWidget(self.removed, 0, 0)
        layout.addWidget(self.downloaded, 0, 1)
        layout.setColumnStretch(0, 1)
        layout.setColumnStretch(1, 1)
        self.bar = QProgressBar()
        self.bar.setAccessibleName('Deleted versus downloaded share of reported bytes')
        self.bar.setRange(0, 10000)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(9)
        layout.addWidget(self.bar, 1, 0, 1, 2)
        layout.addWidget(self.note, 2, 0, 1, 2)
        self.update_totals({})

    def update_totals(self, workers):
        self.snapshot = state = comparison_snapshot(workers)
        share = state['removed_percent']
        removed_share = f" · {share:.1f}%" if share is not None else ''
        downloaded_share = f" · {state['downloaded_percent']:.1f}%" if share is not None else ''
        self.removed.setText(('Deleted: ' + _bytes(state['removed_bytes'])
            + f" · {state['removed_files']:,} files" + removed_share)
            if state['removed_known'] else 'Deleted: size / file count not reported')
        self.downloaded.setText(('Downloaded: ' + ('≈ ' if state['estimated'] else '')
            + _bytes(state['downloaded_bytes']) + downloaded_share)
            if state['downloaded_known'] else 'Downloaded: size not reported')
        known = share is not None
        self.bar.setValue(round(share * 100) if known else 0)
        self.bar.setStyleSheet('QProgressBar {border:0; border-radius:4px; background:'
            + ('#f4b860' if known else '#243746') + ';} QProgressBar::chunk '
              '{background:#52cf9a; border-radius:4px;}')
        explanation = (('Approximate shares' if state['estimated'] else 'Shares') + ' of reported byte activity' if known else
            'Byte comparison available when both sides report sizes' if not state['removed_known'] or not state['downloaded_known']
            else 'No measured byte activity yet')
        if state['partial']:
            explanation += ' · download totals incomplete'
        self.note.setText(explanation + '. Deleted file sizes are not a measured free-space gain.')
        self.bar.setToolTip(self.removed.text() + '\n' + self.downloaded.text() + '\n' + self.note.text())
