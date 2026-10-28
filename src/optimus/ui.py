

"""Optimus desktop interface; host operations run through the local backend."""
from __future__ import annotations
import json
import math
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, QTimer, Signal, Slot, QUrl, QElapsedTimer, QEvent, QSize
from PySide6.QtGui import QAction, QColor, QDesktopServices, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication, QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QSpinBox, QSplitter, QScrollArea,
    QSizePolicy, QStackedWidget, QStyle, QStyleOptionProgressBar, QSystemTrayIcon, QTableWidget, QTableWidgetItem, QTabWidget,
    QVBoxLayout, QWidget,
)
from optimus.widgets.theme import THEMES, build_qss
from optimus.widgets.questions import ask_yes_no
from optimus.widgets.header_controls import HeaderActionButton, install_global_window_reveal, motion_allowed
from optimus.widgets.wrapping_checkbox import WrappingCheckBox
from optimus.widgets.local_loading import LocalLoadingAnimation
from optimus.widgets.navigation_scroll import NavigationScrollArea


def field(record, *names, default=""):
    if not isinstance(record, dict):
        return default
    lowered = {str(k).lower(): v for k, v in record.items()}
    for name in names:
        if name in record:
            return record[name]
        if name.lower() in lowered:
            return lowered[name.lower()]
    return default


def rows(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def display(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (list, tuple)):
        return "; ".join(display(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, default=str)
    return f"{value:,.1f}" if isinstance(value, float) else str(value)


def _report_preview(value, *, max_chars=48000):
    """Bound GUI work before serialization; complete reports remain in their journal."""
    remaining, nodes, shortened = max_chars // 2, 1200, False
    def trim(item, depth=0):
        nonlocal remaining, nodes, shortened
        nodes -= 1
        if nodes < 0 or remaining <= 0 or depth > 9:
            shortened = True
            return '[Further details omitted from this display preview]'
        if isinstance(item, dict):
            output = {}
            for index, (key, part) in enumerate(item.items()):
                if index >= 64 or nodes <= 0 or remaining <= 0:
                    shortened = True
                    output['display_preview'] = 'Further fields omitted'
                    break
                text = str(key)[:160]
                remaining -= len(text)
                output[text] = trim(part, depth + 1)
            return output
        if isinstance(item, (list, tuple)):
            output = []
            for index, part in enumerate(item):
                if index >= 80 or nodes <= 0 or remaining <= 0:
                    shortened = True
                    output.append(f'[{len(item) - index} further items omitted from this display preview]')
                    break
                output.append(trim(part, depth + 1))
            return output
        if item is None or isinstance(item, (bool, int, float)):
            return item
        text = str(item)
        limit = min(1800, max(0, remaining))
        remaining -= min(len(text), limit)
        if len(text) > limit:
            shortened = True
            return text[:limit] + ' [display preview shortened]'
        return text
    text = json.dumps(trim(value), indent=2, ensure_ascii=False, default=str)
    if len(text) > max_chars:
        shortened = True
        text = text[:max_chars] + '\n[Display preview shortened]'
    if shortened:
        path = value.get('log_path') if isinstance(value, dict) else None
        text += '\n\nDisplay preview shortened to keep the interface responsive.'
        text += '\nFull saved report: ' + str(path)[:1024] if path else '\nConsult the saved run report for complete item details.'
    return text


class NumericItem(QTableWidgetItem):
    def __lt__(self, other):
        a, b = self.data(Qt.UserRole + 1), other.data(Qt.UserRole + 1)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return a < b
        return display(a).casefold() < display(b).casefold()


class WorkerSignals(QObject):
    result = Signal(object)
    error = Signal(str)
    finished = Signal()


class BackendWorker(QRunnable):
    def __init__(self, operation, args, kwargs):
        super().__init__()
        self.operation, self.args, self.kwargs = operation, args, kwargs
        self.signals = WorkerSignals()

    def run(self):
        from .runtime_priority import lower_worker_priority
        restore_priority = lower_worker_priority()
        try:
            self.signals.result.emit(self.operation(*self.args, **self.kwargs))
        except Exception as exc:
            self.signals.error.emit(str(exc))
        finally:
            restore_priority()
            self.signals.finished.emit()


class DataTable(QTableWidget):
    def __init__(self, columns):
        super().__init__(0, len(columns))
        self.columns = columns
        self.setHorizontalHeaderLabels([c[0] for c in columns])
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setAlternatingRowColors(True)
        self.verticalHeader().hide()
        self.verticalHeader().setDefaultSectionSize(29)
        self.horizontalHeader().setStretchLastSection(True)
        self.setSortingEnabled(True)
        self.setWordWrap(False)
        self.setMinimumHeight(150)

    def populate(self, records):
        self.setSortingEnabled(False)
        records = rows(records)
        self.setRowCount(len(records))
        for row_index, record in enumerate(records):
            for column_index, (_, keys) in enumerate(self.columns):
                value = keys(record) if callable(keys) else field(record, *keys.split("|"))
                item = NumericItem(display(value))
                item.setData(Qt.UserRole + 1, value)
                if column_index == 0:
                    item.setData(Qt.UserRole, record)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                item.setToolTip(display(value))
                self.setItem(row_index, column_index, item)
        self.setSortingEnabled(True)
        for index, (label, _) in enumerate(self.columns):
            self.setColumnWidth(index, 108 if any(s in label for s in ("MB", "CPU", "PID", "Files", "Free")) else 185)
        if records:
            self.selectRow(0)
        self.itemSelectionChanged.emit()

    def selected(self):
        selected = self.selectionModel().selectedRows()
        if not selected:
            return None
        item = self.item(selected[0].row(), 0)
        return item.data(Qt.UserRole) if item else None


class MetricCard(QFrame):
    def __init__(self, label):
        super().__init__()
        self.setObjectName("Card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 15, 18, 15)
        self.value = QLabel("--")
        self.value.setObjectName("CardValue")
        caption = QLabel(label)
        caption.setObjectName("CardLabel")
        layout.addWidget(self.value)
        layout.addWidget(caption)
        self.setMinimumHeight(100)


class DetailedSettingsWindow(QMainWindow):
    appearanceChanged = Signal(dict)
    PAGE_NAMES = ("Dashboard", "Cleaner", "Browser privacy", "Performance", "Startup",
                  "Applications", "Updates", "Drivers", "Disk analyzer", "Duplicate finder",
                  "Debloat", "Cloud drive", "Schedules", "Smart cleaning", "Maintenance toolkit", "Workers", "File utilities", "Recovery", "Registry review", "Restore/history", "Settings")
    DESCRIPTIONS = {
        "Dashboard": "Measured resource use and practical next steps. Review each change before applying it.",
        "Cleaner": "Preview temporary files and browser caches, then delete only the reviewed list.",
        "Browser privacy": "Preview selected history, cookie and download-list counts; apply with a browser-closed backup.",
        "Performance": "Identify the program behind each process and manage selected application priorities.",
        "Startup": "Review sign-in registrations while keeping applications installed.",
        "Applications": "Review installed programs and use their supported uninstall workflow.",
        "Updates": "Check available application updates with Windows Package Manager.",
        "Drivers": "Inspect installed versions; update through Windows or the device manufacturer.",
        "Disk analyzer": "Measure a selected local folder without following junctions or downloading placeholders.",
        "Duplicate finder": "Find identical file contents and review one file at a time.",
        "Debloat": "Preview the exact packages and settings affected by each available action.",
        "Cloud drive": "Connect your personal OneDrive and review metadata-only duplicate candidates.",
        "Schedules": "Manage scheduled read-only Optimus audits in your Windows account.",
        "Registry review": "Review missing executable registrations in your own account and remove one reviewed entry with a backup.",
        "Restore/history": "Review changes and restore settings that have a supported undo record.",
        "Settings": "Appearance, window motion and supported Windows maintenance tools.",
        "Smart cleaning": "Opt-in cleanup thresholds and browser-exit privacy using your saved categories.",
        "Maintenance toolkit": "Scoped cache cleanup, workload limits, local performance history and supported health diagnostics.",
        "Workers": "Cached status for additional tools without starting another scan or installer.",
        "File utilities": "Authenticated file encryption, verified split/join and reviewed selected-file overwrite.",
        "Recovery": "Verified driver backups, exact-device rollback and Windows restore-point controls.",
    }

    def __init__(self, backend, *, smoke_test=False):
        super().__init__()
        self.backend, self.smoke_test = backend, smoke_test
        self.busy = False
        self._quitting = False
        self._workers = set()
        self._privacy_restore_dialog = None
        self._callback = None
        self.audit_data, self.process_records = {}, []
        self.cleanup_plan = self.debloat_plan = self.storage_result = None
        self.loaded_pages = set()
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.settings_path = Path(backend.data_dir) / "ui-settings.json"
        try:
            self.preferences = json.loads(self.settings_path.read_text(encoding="utf-8"))
            if not isinstance(self.preferences, dict):
                self.preferences = {}
        except (OSError, ValueError):
            self.preferences = {}
        self.config = SimpleNamespace(ui_motion_enabled=self.preferences.get("motion", True))
        self.setWindowTitle("Optimus: Computer optimization tool")
        self.resize(1440, 920)
        self.setMinimumSize(640, 480)
        self.setWindowIcon(self._icon())
        self.setStyleSheet(self._qss())
        self._build_shell()
        for builder in ("dashboard", "cleaner", "browser", "performance", "startup", "applications",
                        "updates", "drivers", "storage", "duplicates", "debloat", "cloud", "schedules", "registry", "history", "settings", "extended_tools"):
            getattr(self, "_build_" + builder)()
        self.tray = None
        self._window_reveal = None
        QApplication.instance().aboutToQuit.connect(self.shutdown)
        self.select_page("Dashboard", load=False)
        if not smoke_test:
            QTimer.singleShot(350, self.refresh_audit)

    def _qss(self):
        theme = self.preferences.get("theme", "cyber")
        if theme not in THEMES:
            theme = "cyber"
        p = THEMES[theme]
        return build_qss(theme, scale=1.0) + f"""
        #Sidebar {{background:{p['panel']};border-right:1px solid {p['border']};}}
        #NavButton {{text-align:left;border:none;padding:9px 15px;border-radius:7px;color:{p['dim']};background:transparent;}}
        #NavButton:hover {{background:{p['panel2']};color:{p['text']};}}
        #NavButton:checked {{color:{p['accent']};background:{p['panel2']};border-left:3px solid {p['accent']};font-weight:700;}}
        #PageDescription, #StatusText {{color:{p['dim']};}}
        #SidebarBrand {{color:{p['accent']};font-size:24px;font-weight:800;letter-spacing:4px;}}
        QProgressBar {{border:1px solid {p['border']};border-radius:4px;max-height:8px;background:{p['panel']};}}
        QProgressBar::chunk {{background:{p['accent']};}}
        QListWidget {{background:{p['panel']};border:1px solid {p['border']};border-radius:8px;padding:6px;}}
        QPlainTextEdit {{background:{p['panel']};color:{p['text']};border:1px solid {p['border']};border-radius:8px;padding:10px;}}
        """

    @staticmethod
    def _icon():
        px = QPixmap(64, 64)
        px.fill(Qt.transparent)
        p = QPainter(px)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor("#1f9cff"), 4))
        p.setBrush(QColor("#0f141c"))
        p.drawRoundedRect(5, 5, 54, 54, 14, 14)
        p.setPen(QPen(QColor("#ff7a1a"), 6))
        p.drawEllipse(19, 17, 26, 30)
        p.end()
        return QIcon(px)

    def _build_shell(self):
        central = QWidget()
        shell = QHBoxLayout(central)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        sidebar = QWidget()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(205)
        nav = QVBoxLayout(sidebar)
        nav.setContentsMargins(13, 22, 13, 15)
        nav.setSpacing(3)
        brand = QLabel("OPTIMUS")
        brand.setObjectName("SidebarBrand")
        nav.addWidget(brand)
        tag = QLabel("COMPUTER OPTIMIZATION")
        tag.setObjectName("Tagline")
        tag.setStyleSheet("font-size:9px;letter-spacing:1px;padding:0 2px 15px 2px;")
        nav.addWidget(tag)
        navigation_scroll = NavigationScrollArea(sidebar)
        navigation_scroll.setWidgetResizable(True)
        navigation_scroll.setFrameShape(QFrame.NoFrame)
        navigation_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        navigation_content = QWidget()
        navigation_layout = QVBoxLayout(navigation_content)
        navigation_layout.setContentsMargins(0, 0, 0, 0)
        navigation_layout.setSpacing(3)
        navigation_scroll.setWidget(navigation_content)
        nav.addWidget(navigation_scroll, 1)
        group = QButtonGroup(self)
        group.setExclusive(True)
        self.navigation = {}
        for name in self.PAGE_NAMES:
            button = QPushButton(name)
            button.setObjectName("NavButton")
            button.setCheckable(True)
            button.clicked.connect(lambda checked=False, n=name: self.select_page(n))
            group.addButton(button)
            navigation_layout.addWidget(button)
            self.navigation[name] = button
        navigation_layout.addStretch()
        foot = QLabel("LOCAL TOOLS\nNo subscription required")
        foot.setObjectName("Tagline")
        foot.setStyleSheet("padding:10px 4px;font-size:10px;")
        nav.addWidget(foot)
        content = QWidget()
        body = QVBoxLayout(content)
        body.setContentsMargins(24, 20, 24, 13)
        body.setSpacing(12)
        header = QHBoxLayout()
        heading = QVBoxLayout()
        self.page_title = QLabel()
        self.page_title.setObjectName("PageTitle")
        self.page_title.setWordWrap(True)
        self.page_title.setMinimumWidth(0)
        self.page_title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.page_description = QLabel()
        self.page_description.setObjectName("PageDescription")
        self.page_description.setWordWrap(True)
        heading.addWidget(self.page_title)
        heading.addWidget(self.page_description)
        header.addLayout(heading, 1)
        refresh = HeaderActionButton("Refresh", "selftest", "Refresh audit", "Read current resources, processes, startup entries and installed software.")
        refresh.clicked.connect(self.refresh_audit)
        header.addWidget(refresh)
        settings = HeaderActionButton("", "settings", "Appearance and tools", "Configure appearance and Windows maintenance tools.")
        settings.set_compact(True)
        settings.clicked.connect(lambda: self.select_page("Settings"))
        header.addWidget(settings)
        body.addLayout(header)
        self.activity = QProgressBar()
        self.activity.setRange(0, 0)
        self.activity.setTextVisible(False)
        self.activity.hide()
        body.addWidget(self.activity)
        self.stack = QStackedWidget()
        self.pages, self.layouts = {}, {}
        for name in self.PAGE_NAMES:
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(12)
            viewport = QScrollArea()
            viewport.setWidgetResizable(True)
            viewport.setFrameShape(QFrame.NoFrame)
            viewport.setWidget(page)
            self.stack.addWidget(viewport)
            self.pages[name], self.layouts[name] = viewport, layout
        body.addWidget(self.stack, 1)
        self.status = QLabel("Ready. Scans are read-only until you confirm an action.")
        self.status.setObjectName("StatusText")
        self.status.setWordWrap(True)
        body.addWidget(self.status)
        shell.addWidget(sidebar)
        shell.addWidget(content, 1)
        self.setCentralWidget(central)

    def _toolbar(self, layout):
        widget = QWidget()
        bar = QHBoxLayout(widget)
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(8)
        layout.addWidget(widget)
        return bar

    def _build_extended_tools(self):
        from .widgets.smart_maintenance_panel import SmartMaintenancePanel
        from .widgets.recovery_panel import RecoveryPanel
        from .widgets.file_utilities_panel import FileUtilitiesPanel
        from .widgets.worker_grid import WorkerBoard
        from .widgets.maintenance_toolkit_panel import MaintenanceToolkitPanel
        for page, attribute, factory in (
            ('Smart cleaning', 'smart_maintenance_panel', SmartMaintenancePanel),
            ('Maintenance toolkit', 'maintenance_toolkit_panel', MaintenanceToolkitPanel),
            ('Workers', 'worker_board', WorkerBoard),
            ('File utilities', 'file_utilities_panel', FileUtilitiesPanel),
            ('Recovery', 'recovery_panel', RecoveryPanel)):
            panel = factory(self)
            setattr(self, attribute, panel)
            self.layouts[page].addWidget(panel)

    def _button(self, layout, text, callback, *, primary=False):
        button = QPushButton(text)
        button.setCursor(Qt.PointingHandCursor)
        if primary:
            button.setObjectName("Primary")
        button.clicked.connect(callback)
        layout.addWidget(button)
        return button

    def _note(self, layout, text):
        label = QLabel(text)
        label.setObjectName("PageDescription")
        label.setWordWrap(True)
        layout.addWidget(label)
        return label

    def select_page(self, name, *, load=True):
        if name not in self.pages:
            return
        if not self.busy and self.status.text().startswith('Wait for the current operation before closing'):
            self.status.setText('Ready. Choose a tool to inspect its current scope.')
        self.stack.setCurrentWidget(self.pages[name])
        self.navigation[name].setChecked(True)
        self.page_title.setText(name.upper())
        self.page_description.setText(self.DESCRIPTIONS[name])
        if load and name not in self.loaded_pages and not self.busy:
            loaders = {"Cleaner": self.load_cleanup_categories, "Startup": self.load_startup,
                       "Applications": self.load_apps, "Drivers": self.load_drivers, "Browser privacy": self.privacy_panel.refresh,
                       "Debloat": self.load_debloat_actions, "Cloud drive": self.cloud_panel.refresh, "Schedules": self.load_schedules,
                       "Restore/history": self.load_changes}
            if name in loaders:
                loaders[name]()
            elif name == "Performance" and not self.process_records:
                self.load_processes()

    def _run(self, method, *args, done=None, **kwargs):
        if self.busy:
            self.status.setText("A task is already running. Wait for its result before starting another.")
            return
        operation = getattr(self.backend, method, None)
        if not callable(operation):
            self._error("Backend capability unavailable: " + method + ". No action was taken.")
            return
        self.busy, self._callback, self._operation_name = True, done, method
        self.status.setText("Working: " + method.replace("_", " ") + ". The window remains available.")
        self.activity.show()
        worker = BackendWorker(operation, args, kwargs)
        self._workers.add(worker)
        worker.signals.result.connect(self._worker_result)
        worker.signals.error.connect(self._worker_error)
        worker.signals.finished.connect(lambda w=worker: self._workers.discard(w))
        self.pool.start(worker)

    @Slot(object)
    def _worker_result(self, result):
        callback, name = self._callback, self._operation_name
        self.busy, self._callback = False, None
        self.activity.hide()
        self.status.setText("Completed " + name.replace("_", " ") + " at " + datetime.now().strftime("%H:%M:%S") + ".")
        try:
            if callback:
                callback(result)
        except Exception as exc:
            self._error(str(exc))

    @Slot(str)
    def _worker_error(self, message):
        self.busy, self._callback = False, None
        self.activity.hide()
        self._error(message)

    def _error(self, message):
        self.status.setText("Action could not complete: " + str(message))
        if self.smoke_test:
            self.last_error = str(message)
        else:
            QMessageBox.warning(self, "Optimus", str(message))

    def _show_result(self, result):
        self.result_text.setPlainText(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        self.select_page("Restore/history", load=False)
        self.loaded_pages.discard("Restore/history")
        self.status.setText("Review the returned result, warnings and skipped items below.")

    def _confirm(self, title, message, *, details=None, phrase=None):
        if self.smoke_test:
            raise RuntimeError("Mutations are disabled during the UI smoke test.")
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.resize(720, 440 if details is not None else 240)
        dialog.setProperty("_optimus_no_reveal", True)
        layout = QVBoxLayout(dialog)
        label = QLabel(message)
        label.setWordWrap(True)
        layout.addWidget(label)
        if details is not None:
            text = QPlainTextEdit()
            text.setReadOnly(True)
            text.setPlainText(details if isinstance(details, str) else json.dumps(details, indent=2, ensure_ascii=False, default=str))
            layout.addWidget(text, 1)
        entry = None
        if phrase:
            layout.addWidget(QLabel("Type " + phrase + " to continue:"))
            entry = QLineEdit()
            layout.addWidget(entry)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        apply = buttons.addButton("Apply reviewed action", QDialogButtonBox.AcceptRole)
        apply.setAutoDefault(False)
        apply.setDefault(False)
        buttons.button(QDialogButtonBox.Cancel).setDefault(True)
        if entry is not None:
            apply.setEnabled(False)
            entry.textChanged.connect(lambda value: apply.setEnabled(value == phrase))
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        return dialog.exec() == QDialog.Accepted

    def _selected(self, table):
        record = table.selected()
        if record is None:
            self._error("Select one row first.")
        return record

    def _build_dashboard(self):
        layout = self.layouts["Dashboard"]
        cards = QHBoxLayout()
        self.cards = [MetricCard(x) for x in ("CPU USAGE", "MEMORY IN USE", "SYSTEM DRIVE FREE", "SIGN-IN ENTRIES")]
        for card in self.cards:
            cards.addWidget(card)
        layout.addLayout(cards)
        bar = self._toolbar(layout)
        self._button(bar, "Run health review", self.refresh_audit, primary=True)
        self._button(bar, "Review cleanup", lambda: self.select_page("Cleaner"))
        self._button(bar, "Review memory", lambda: self.select_page("Performance"))
        bar.addStretch()
        self.health = DataTable([("Area", "area"), ("Finding", "finding"), ("Next step", "recommendation")])
        layout.addWidget(self.health, 1)
        self._note(layout, "Source: Windows resource, process and startup APIs. A long-running process is not automatically obsolete. Shared working-set pages can appear in several processes.")

    def refresh_audit(self):
        self._run("audit", done=self._audit_ready)

    def _audit_ready(self, audit):
        self.audit_data = audit if isinstance(audit, dict) else {}
        system = field(self.audit_data, "System", default={})
        cpu = field(system, "CpuPercent", default=None)
        memory = field(system, "MemoryUsedPercent", default=None)
        disks = rows(field(system, "Disks", default=[]))
        drive = os.environ.get("SystemDrive", "C:")
        disk = next((d for d in disks if field(d, "Drive") == drive), disks[0] if disks else {})
        self.cards[0].value.setText(f"{cpu:.0f}%" if isinstance(cpu, (int, float)) else "--")
        self.cards[1].value.setText(f"{memory:.0f}%" if isinstance(memory, (int, float)) else "--")
        free = field(disk, "FreeGB", default=None)
        self.cards[2].value.setText(f"{free:.1f} GB" if isinstance(free, (int, float)) else "--")
        self.cards[3].value.setText(str(len(rows(field(self.audit_data, "Startup", default=[])))))
        findings = []
        for d in disks:
            pct, amount = field(d, "FreePercent", default=0), field(d, "FreeGB", default=0)
            findings.append({"area": "Storage " + display(field(d, "Drive")), "finding": f"{amount:,.1f} GB free ({pct:,.1f}%)",
                             "recommendation": "Prioritize freeing space; review caches and large applications." if pct < 10 else "Review large files when needed."})
        if isinstance(memory, (int, float)):
            available = field(system, "MemoryAvailableGB", default=0)
            findings.append({"area": "Memory", "finding": f"{memory:.1f}% in use; {available:.1f} GB available",
                             "recommendation": "Review largest programs and close finished work." if memory > 85 else "Review program totals before changing priorities."})
        totals = rows(field(self.audit_data, "AppTotals", default=[]))
        if totals:
            top = max(totals, key=lambda r: field(r, "WorkingSetMB", default=0) or 0)
            findings.append({"area": "Largest program", "finding": display(field(top, "Program")) + f" - {field(top, 'WorkingSetMB', default=0):,.0f} MB working set",
                             "recommendation": "Inspect its processes and launch evidence in Performance."})
        findings.append({"area": "Startup", "finding": str(len(rows(field(self.audit_data, "Startup", default=[])))) + " inventoried entries",
                         "recommendation": "Disable only optional entries you recognize; programs stay installed."})
        for note in rows(field(self.audit_data, "InventoryErrors", default=[])):
            findings.append({"area": "Inventory note", "finding": display(note), "recommendation": "Administrator access may reveal more details."})
        self.health.populate(findings)
        if not self.process_records and not self.smoke_test:
            QTimer.singleShot(0, self.load_processes)
        self.startup_table.populate(field(self.audit_data, "Startup", default=[]))
        self.apps_table.populate(field(self.audit_data, "InstalledApps", default=[]))
        self.power_combo.clear()
        for plan in rows(field(self.audit_data, "PowerPlans", default=[])):
            self.power_combo.addItem(display(field(plan, "Name")) + (" (active)" if field(plan, "Active", default=False) else ""), plan)

    def _build_cleaner(self):
        layout = self.layouts["Cleaner"]
        self._note(layout, "Junk and cache cleanup: choose temporary files, error reports, shader caches or inactive-browser caches, then preview the exact files. Documents, downloads, browser history and passwords are not general cleanup targets.")
        bar = self._toolbar(layout)
        self._button(bar, "Refresh categories", self.load_cleanup_categories)
        bar.addWidget(QLabel("Minimum age:"))
        self.clean_age = QSpinBox()
        self.clean_age.setRange(1, 3650)
        self.clean_age.setValue(7)
        self.clean_age.setSuffix(" days")
        self.clean_age.valueChanged.connect(self._invalidate_cleanup)
        bar.addWidget(self.clean_age)
        self._button(bar, "Preview selected", self.scan_cleanup, primary=True)
        self.clean_apply = self._button(bar, "Delete reviewed files", self.apply_cleanup)
        self.clean_apply.setEnabled(False)
        bar.addStretch()
        split = QSplitter()
        self.clean_categories = QListWidget()
        self.clean_categories.setMinimumWidth(235)
        self.clean_categories.setMaximumWidth(350)
        self.clean_categories.itemChanged.connect(self._invalidate_cleanup)
        self.clean_categories.itemSelectionChanged.connect(self._cleanup_category_selected)
        split.addWidget(self.clean_categories)
        self.clean_files = DataTable([("Category", "category|CategoryName"), ("Size MB", "size_mb|SizeMB"), ("Age days", "age_days|AgeDays"), ("File path", "path|Path")])
        split.addWidget(self.clean_files)
        split.setStretchFactor(1, 1)
        layout.addWidget(split, 1)
        self.clean_summary = self._note(layout, "Select categories and run a preview. No file is deleted by a scan.")
        self.clean_category_note = self._note(layout, "Cache cleanup excludes history, cookies and saved passwords. Active browser caches are skipped.")

    def load_cleanup_categories(self):
        self._run("cleanup_categories", done=self._cleanup_categories_ready)

    def _cleanup_categories_ready(self, categories):
        self.clean_categories.clear()
        for category in rows(categories):
            item = QListWidgetItem(display(field(category, "name")))
            item.setData(Qt.UserRole, category)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            item.setToolTip(display(field(category, "description", "path")))
            if field(category, "available", default=True) is False:
                item.setFlags(item.flags() & ~Qt.ItemIsEnabled)
            self.clean_categories.addItem(item)
        self.loaded_pages.add("Cleaner")

    def _cleanup_category_selected(self):
        item = self.clean_categories.currentItem()
        if item:
            category = item.data(Qt.UserRole)
            self.clean_category_note.setText(display(field(category, "description")) + "\n" + display(field(category, "path", "Root")))

    def _invalidate_cleanup(self, *_):
        self.cleanup_plan = None
        self.clean_apply.setEnabled(False)

    def scan_cleanup(self):
        selected = [field(self.clean_categories.item(i).data(Qt.UserRole), "id") for i in range(self.clean_categories.count())
                    if self.clean_categories.item(i).checkState() == Qt.Checked]
        if not selected:
            self._error("Check one or more cleanup categories first.")
            return
        self._run("cleanup_scan", selected, min_age_days=self.clean_age.value(), done=self._cleanup_ready)

    def _cleanup_ready(self, plan):
        self.cleanup_plan = plan
        files = rows(field(plan, "files", default=[]))
        self.clean_files.populate(files)
        size = field(plan, "total_mb", default=0)
        complete = field(plan, "complete", default=False)
        skipped = len(rows(field(plan, "skipped", default=[])))
        errors = len(rows(field(plan, "errors", default=[])))
        self.clean_summary.setText(f"Preview: {len(files):,} listed files, {size:,.1f} MB. " + ("Scan complete." if complete and not errors else "Partial scan; some files could not be examined or a limit was reached.") + f" {errors} scan errors; {skipped} skipped notes. Only this list can be applied.")
        self.clean_apply.setEnabled(bool(files))

    def apply_cleanup(self):
        plan = self.cleanup_plan
        if not plan:
            self._error("Run a fresh cleanup preview first.")
            return
        files = rows(field(plan, "files", default=[]))
        size = field(plan, "total_mb", default=0)
        if self._confirm("Delete reviewed cache files", f"Permanently delete {len(files):,} files ({size:,.1f} MB) in this preview? Cache deletion has no undo. Files are revalidated and in-use files skipped.",
                         details="\n".join(display(field(f, "path")) for f in files), phrase="DELETE LISTED FILES"):
            self._run("cleanup_apply", plan, done=self._cleanup_applied)

    def _cleanup_applied(self, result):
        self._invalidate_cleanup()
        self._show_result(result)

    def _build_browser(self):
        from optimus.widgets.privacy_panel import PrivacyPanel
        self.privacy_panel = PrivacyPanel(self)
        self.layouts['Browser privacy'].addWidget(self.privacy_panel)

    def _build_performance(self):
        outer = self.layouts['Performance']
        self.performance_tabs = QTabWidget()
        controls = QWidget()
        layout = QVBoxLayout(controls)
        layout.setContentsMargins(0,0,0,0)
        analysis = self._toolbar(layout)
        self.analysis_toggle = QCheckBox("Background analysis OFF")
        self.analysis_toggle.setChecked(False)
        self.analysis_toggle.toggled.connect(self._analysis_toggled)
        analysis.addWidget(self.analysis_toggle)
        analysis.addWidget(QLabel("Sample every:"))
        self.analysis_interval = QSpinBox()
        self.analysis_interval.setRange(2, 60)
        self.analysis_interval.setValue(5)
        self.analysis_interval.setSuffix(" seconds")
        analysis.addWidget(self.analysis_interval)
        self.analysis_label = QLabel("Off - no background resource sampling")
        analysis.addWidget(self.analysis_label, 1)
        self._analysis_timer = QTimer(self)
        self._analysis_timer.setInterval(2000)
        self._analysis_timer.timeout.connect(self._poll_analysis)
        bar = self._toolbar(layout)
        self._button(bar, "Refresh processes", self.load_processes)
        self.process_search = QLineEdit()
        self.process_search.setPlaceholderText("Search program, process, PID or path")
        self.process_search.textChanged.connect(self._filter_processes)
        bar.addWidget(self.process_search, 1)
        self.show_protected = QCheckBox("Show essential processes")
        self.show_protected.toggled.connect(self._filter_processes)
        bar.addWidget(self.show_protected)
        self.process_table = DataTable([("Process", "name|Name"), ("PID", "pid|ProcessId"), ("CPU %", "cpu_percent|CpuPercent"),
            ("RAM MB", lambda r: field(r, "memory_rss", default=0) / 1048576),
            ("Private MB", lambda r: field(r, "private_bytes", default=0) / 1048576),
            ("Priority", "priority"), ("CPU cores", "affinity_text"),
            ("Read MB", lambda r: field(r, "io_read_bytes", default=0) / 1048576),
            ("Write MB", lambda r: field(r, "io_write_bytes", default=0) / 1048576)])
        self.process_table.itemSelectionChanged.connect(self._process_selected)
        layout.addWidget(self.process_table, 1)
        self.process_detail = QPlainTextEdit()
        self.process_detail.setReadOnly(True)
        self.process_detail.setMaximumHeight(140)
        layout.addWidget(self.process_detail)
        bar = self._toolbar(layout)
        self.priority_combo = QComboBox()
        self.priority_combo.addItems(["Normal", "AboveNormal", "High"])
        self.priority_combo.setCurrentText("Normal")
        bar.addWidget(self.priority_combo)
        self.priority_apply_button = self._button(bar, "Apply priority", self.apply_priority)
        self.pause_button = self._button(bar, "Pause selected app", self.pause_process)
        self._button(bar, "Resume paused apps", self.resume_processes)
        self._button(bar, "Codex priority profile", self.codex_profile)
        self._button(bar, "Full process details", self.process_details)
        bar.addStretch()
        affinity_bar = self._toolbar(layout)
        affinity_bar.addWidget(QLabel("Logical CPU cores:"))
        self.affinity_input = QLineEdit()
        self.affinity_input.setPlaceholderText("Zero-based numbers, for example 0,1,2,3")
        affinity_bar.addWidget(self.affinity_input, 1)
        self.affinity_button = self._button(affinity_bar, "Apply CPU affinity", self.apply_affinity)
        self.priority_apply_button.setEnabled(False)
        self.pause_button.setEnabled(False)
        self.affinity_button.setEnabled(False)
        bar = self._toolbar(layout)
        bar.addWidget(QLabel("Power plan:"))
        self.power_combo = QComboBox()
        bar.addWidget(self.power_combo, 1)
        self._button(bar, "Apply plan", self.apply_power)
        self._button(bar, "Resource Monitor", lambda: self._run("system_tool", "resource-monitor"))
        self._note(layout, "Pause suspends one verified application process; it can interrupt work and does not reduce committed memory. Higher priority changes CPU scheduling, not total CPU or RAM.")

        self.performance_tabs.addTab(controls, 'Process controls')
        review = QWidget()
        report_layout = QVBoxLayout(review)
        toolbar = self._toolbar(report_layout)
        self._button(toolbar, 'Measure programs slowing down PC', lambda: self._run('slowdown_report', done=self._slowdown_ready), primary=True)
        self._button(toolbar, 'View selected program processes', self._slowdown_open_processes)
        toolbar.addStretch()
        self.slowdown_summary = self._note(report_layout, 'No measurement collected. This short read-only sample compares CPU, memory and I/O over several snapshots; no program is changed automatically.')
        self.slowdown_table = DataTable([('Review level',lambda r:{'high':'High resource use','review':'Review','normal':'Normal'}.get(field(r,'severity'),'Review')),
            ('Program','name'),('CPU %',lambda r:field(r,'cpu_percent') if field(r,'measured_intervals',default=0)>0 else 'Unavailable'),('Peak CPU %',lambda r:field(r,'peak_cpu_percent') if field(r,'measured_intervals',default=0)>0 else 'Unavailable'),
            ('Memory MB',lambda r:float(field(r,'memory_rss',default=0) or 0)/1048576),
            ('RAM %','memory_percent'),('Read MB/s',lambda r:float(field(r,'read_bytes_per_second',default=0) or 0)/1048576 if field(r,'io_measured_intervals',default=0)>0 else 'Unavailable'),
            ('Write MB/s',lambda r:float(field(r,'write_bytes_per_second',default=0) or 0)/1048576 if field(r,'io_measured_intervals',default=0)>0 else 'Unavailable'),('Processes','process_count'),('Program path','exe')])
        self.slowdown_table.itemSelectionChanged.connect(self._slowdown_selected)
        report_layout.addWidget(self.slowdown_table,1)
        self.slowdown_details = QPlainTextEdit()
        self.slowdown_details.setReadOnly(True)
        self.slowdown_details.setMaximumHeight(185)
        report_layout.addWidget(self.slowdown_details)
        self._note(report_layout, 'Measured resource use identifies programs to review. It does not prove that a program is old, unwanted or the cause of every delay. Shared memory may be counted in several process working sets. Use the existing selected-process controls only after reviewing the program.')
        self.performance_tabs.addTab(review, 'Programs slowing down PC')
        from optimus.widgets.process_harness_panel import ProcessHarnessPanel
        self.process_harness_panel = ProcessHarnessPanel(self)
        self.performance_tabs.addTab(self.process_harness_panel, 'Process cleanup harness')
        outer.addWidget(self.performance_tabs,1)

    def _slowdown_ready(self, report):
        applications = rows(field(report,'applications',default=[]))
        self.slowdown_table.populate(applications)
        weights = {'high':2,'review':1,'normal':0}
        for index in range(self.slowdown_table.rowCount()):
            item = self.slowdown_table.item(index,0)
            item.setData(Qt.UserRole+1, weights.get(field(item.data(Qt.UserRole),'severity'),1))
        self.slowdown_table.sortItems(0,Qt.DescendingOrder)
        if applications:
            self.slowdown_table.selectRow(0)
        memory = field(report,'memory',default={}) or {}
        self.slowdown_summary.setText(str(field(report,'sample_count',default=0))+' samples over '+display(field(report,'duration_seconds',default=0))+' seconds. System CPU '+display(field(report,'system_cpu_percent'))+'%; RAM '+display(field(memory,'percent'))+'%. '+('Complete sample.' if field(report,'complete',default=False) else 'Partial/cancelled sample; interpret with care.')+' '+str(field(report,'inaccessible_process_count',default=0))+' inaccessible processes. '+display(field(report,'notes',default=[])))
        self.performance_tabs.setCurrentIndex(1)
        self._slowdown_selected()

    def _slowdown_selected(self):
        record = self.slowdown_table.selected()
        if not record:
            self.slowdown_details.clear()
            return
        self.slowdown_details.setPlainText('Program: '+display(field(record,'name'))+'\nPath: '+display(field(record,'exe'))+'\nProcesses: '+display(field(record,'pids'))+'\nWhy review: '+display(field(record,'reasons',default=[]))+'\nSuggested next step: '+display(field(record,'recommendation'))+'\nNo application was stopped, suspended or uninstalled by this report.')

    def _slowdown_open_processes(self):
        record = self.slowdown_table.selected()
        if record:
            self.process_search.setText(display(field(record,'exe','name')))
            self.performance_tabs.setCurrentIndex(0)
            self.load_processes()

    def _analysis_toggled(self, enabled):
        if self.busy:
            self.analysis_toggle.blockSignals(True)
            self.analysis_toggle.setChecked(not enabled)
            self.analysis_toggle.blockSignals(False)
            self.status.setText("Wait for the current operation before changing analysis.")
            return
        if enabled:
            self._run("analysis_start", self.analysis_interval.value(), done=self._analysis_state)
        else:
            self._analysis_timer.stop()
            self._run("analysis_stop", done=self._analysis_state)

    def _analysis_state(self, state):
        enabled = bool(field(state, "enabled", default=False))
        self.analysis_toggle.blockSignals(True)
        self.analysis_toggle.setChecked(enabled)
        self.analysis_toggle.setText("Background analysis ON" if enabled else "Background analysis OFF")
        self.analysis_toggle.blockSignals(False)
        self.analysis_interval.setEnabled(not enabled)
        if enabled:
            self._analysis_timer.start()
        else:
            self._analysis_timer.stop()
        latest = field(state, "latest", default={}) or {}
        memory = field(latest, "memory", default={})
        if latest:
            self.analysis_label.setText("CPU " + display(field(latest, "cpu_percent")) + "% | RAM " + display(field(memory, "percent")) + "% | " + display(field(latest, "timestamp")))
        else:
            self.analysis_label.setText("Waiting for first sample" if enabled else "Off - no background resource sampling")

    def _poll_analysis(self):
        if not self.busy and self.analysis_toggle.isChecked():
            try:
                self._analysis_state(self.backend.analysis_status())
            except Exception as exc:
                self.analysis_label.setText("Analysis status unavailable: " + str(exc))

    def load_processes(self):
        self._run("processes", done=self._processes_ready)

    def _processes_ready(self, result):
        self.process_records = rows(result)
        self._filter_processes()
        self.loaded_pages.add("Performance")

    def _filter_processes(self, *_):
        query = self.process_search.text().strip().casefold()
        values = [r for r in self.process_records if
                  (self.show_protected.isChecked() or not field(r, "protected", "IsProtected", default=False) or field(r, "can_set_priority", default=False)) and
                  (not query or query in " ".join(display(field(r, k)) for k in ("name", "exe", "pid", "username")).casefold())]
        self.process_table.populate(values)

    def _process_selected(self):
        r = self.process_table.selected()
        if hasattr(self, "priority_apply_button"):
            self.priority_apply_button.setEnabled(bool(r and field(r, "can_set_priority", default=False)))
            self.pause_button.setEnabled(bool(r and field(r, "can_sleep", default=not field(r, "protected", default=True))))
            self.affinity_button.setEnabled(bool(r and field(r, "can_set_priority", default=False)))
        if r:
            self.process_detail.setPlainText("Process: " + display(field(r, "name")) + "  PID " + display(field(r, "pid")) +
                "\nProgram path: " + display(field(r, "exe")) + "\nParent PID: " + display(field(r, "ppid")) +
                "  User: " + display(field(r, "username")) + "\nPriority: " + display(field(r, "priority")) +
                "  Logical cores: " + display(field(r, "affinity_text")) + "\nProtection: " + display(field(r, "reason")) +
                "\nChoose Full process details for verified parent, program metadata and launch evidence.")

    def process_details(self):
        r = self._selected(self.process_table)
        if r:
            self._run("process_details", r, done=lambda data: self.process_detail.setPlainText(json.dumps(data, indent=2, default=str)))

    def apply_affinity(self):
        self._process_action("set_affinity", "Limit this process to logical CPU cores " + self.affinity_input.text() + "? Fewer cores can reduce performance.", self.affinity_input.text())

    def _process_action(self, method, message, *extra):
        r = self._selected(self.process_table)
        if not r:
            return
        allowed = field(r, "can_sleep", default=False) if method == "sleep_process" else field(r, "can_set_priority", default=False)
        if not allowed:
            self._error("This action is protected: " + display(field(r, "reason", "ProtectionReason")))
            return
        name = display(field(r, "name", "Name")) + " (PID " + display(field(r, "pid", "ProcessId")) + ")"
        if self._confirm("Review process change", name + "\n\n" + message):
            self._run(method, r, *extra, done=self._show_result)

    def apply_priority(self):
        p = self.priority_combo.currentText()
        self._process_action("set_priority", "Set CPU scheduling priority to " + p + " until the process exits?", p)

    def pause_process(self):
        self._process_action("sleep_process", "Pause this application process? It can stop responding until resumed. Save your work first.")

    def resume_processes(self):
        if self._confirm("Resume paused processes", "Resume processes paused by Optimus in this session?"):
            self._run("wake_all", done=self._show_result)

    def codex_profile(self):
        if self._confirm("Codex CPU priority profile", "Set verified Codex application processes to High priority? Other programs may become less responsive."):
            self._run("codex_profile", priority="High", done=self._show_result)

    def apply_power(self):
        plan = self.power_combo.currentData()
        if not plan:
            self._error("Refresh the audit to load power plans.")
        elif self._confirm("Change power plan", "Activate " + display(field(plan, "Name")) + "? Performance plans can increase heat and power consumption."):
            self._run("set_power_plan", field(plan, "Guid"), done=self._show_result)

    def _page_table_ready(self, name, table, result):
        table.populate(result)
        self.loaded_pages.add(name)

    def _build_startup(self):
        tabs = QTabWidget()
        self.layouts['Startup'].addWidget(tabs, 1)
        registrations = QWidget()
        layout = QVBoxLayout(registrations)
        tabs.addTab(registrations, 'Sign-in entries')
        from .widgets.services_panel import ServicesPanel
        self.services_panel = ServicesPanel(self)
        tabs.addTab(self.services_panel, 'Windows services')
        bar = self._toolbar(layout)
        self._button(bar, 'Refresh entries', self.load_startup)
        self._button(bar, 'Disable selected', lambda: self.change_startup(False))
        self._button(bar, 'Enable selected', lambda: self.change_startup(True))
        self._button(bar, 'Delete entry', self.delete_startup)
        self._button(bar, 'Windows settings', lambda: self._run('system_tool', 'startup'))
        self.startup_table = DataTable([('Entry', 'Name'), ('Entry present', 'Enabled'), ('Source', 'Source'), ('Command', 'Command'), ('Reason', 'Reason')])
        layout.addWidget(self.startup_table, 1)
        self._note(layout, 'Entry present means the registration exists; Windows may separately suppress it in Startup settings. Deleting an entry does not uninstall its program. Supported changes save a restore record.')

    def load_startup(self):
        self._run('startup_list', done=lambda r: self._page_table_ready('Startup', self.startup_table, r))

    def change_startup(self, enabled):
        r = self._selected(self.startup_table)
        if r and self._confirm('Change startup entry', ('Enable ' if enabled else 'Disable ') + display(field(r, 'Name')) + '? The application stays installed.'):
            self._run('startup_set', r, enabled, done=self._show_result)

    def delete_startup(self):
        r = self._selected(self.startup_table)
        if r and self._confirm('Delete startup entry', 'Remove the startup registration for ' + display(field(r, 'Name')) + '? Supported changes save a restore record; application files remain installed.'):
            self._run('startup_remove', r, done=self._show_result)

    def _build_applications(self):
        layout = self.layouts['Applications']
        bar = self._toolbar(layout)
        self._button(bar, 'Refresh programs', self.load_apps)
        self._button(bar, 'Review uninstall', self.uninstall_app)
        self._button(bar, 'Windows Installed apps', lambda: self._run('system_tool', 'installed-apps'))
        bar.addStretch()
        self.apps_table = DataTable([('Program', 'Name'), ('Version', 'Version'), ('Publisher', 'Publisher'), ('Type', 'Type'), ('Location', 'InstallLocation')])
        layout.addWidget(self.apps_table, 1)
        self._note(layout, 'This review opens the supported system controls or a native removal plan; it does not run a managed uninstall. External uninstallers show their own progress. Automatic maintenance shows removal percentages when its provider reports them. Removal may affect application data and is not undone by restoring an Optimus settings record.')

    def load_apps(self):
        self._run('installed_apps', done=lambda r: self._page_table_ready('Applications', self.apps_table, r))

    def uninstall_app(self):
        r = self._selected(self.apps_table)
        if r and self._confirm('Review uninstall', 'Open the supported uninstall workflow for ' + display(field(r, 'Name')) + '? Review the uninstaller options before approving removal.', details=r):
            self._run('uninstall', r, done=self._show_result)

    def _build_updates(self):
        layout = self.layouts['Updates']
        bar = self._toolbar(layout)
        self._button(bar, 'Check software updates', lambda: self._run('updates_scan', done=self._updates_ready), primary=True)
        self._button(bar, 'Windows Update', lambda: self._run('system_tool', 'windows-update'))
        self._button(bar, 'Microsoft Store updates', lambda: self._run('system_tool', 'store-updates'))
        bar.addStretch()
        self.updates_summary = self._note(layout, 'No scan loaded. Save per-app choices here, or review the global update scope in setup.')
        from PySide6.QtWidgets import QTabWidget
        self.app_update_tabs = QTabWidget()
        rules_page, offers_page = QWidget(), QWidget()
        rules_layout, offers_layout = QVBoxLayout(rules_page), QVBoxLayout(offers_page)
        self._build_app_rules_controls(rules_layout)
        self.app_update_tabs.addTab(rules_page, 'Per-app choices')
        self.app_update_tabs.addTab(offers_page, 'Available updates')
        layout.addWidget(self.app_update_tabs, 1)
        self.updates_candidates_table = DataTable([('Application','name'),('Package ID','id|package_id'),('Installed','version'),('Available','available'),('Source','source')])
        self.updates_candidates_table.itemSelectionChanged.connect(self._update_selected)
        offers_layout.addWidget(self.updates_candidates_table,1)
        self.updates_output = QPlainTextEdit()
        self.updates_output.setReadOnly(True)
        self.updates_output.setMaximumHeight(160)
        self.updates_output.setPlaceholderText('WinGet source output and any unsupported or incomplete results appear here.')
        offers_layout.addWidget(self.updates_output)
        bar = self._toolbar(offers_layout)
        self.update_id = QLineEdit()
        self.update_id.setPlaceholderText('Selected or advanced exact package ID, for example Vendor.Application')
        bar.addWidget(self.update_id,1)
        self._button(bar,'Update this package',self.update_package)
        self._note(offers_layout,'This direct action updates one confirmed package. Automatic all-supported scope needs separate saved approval and agreement acceptance; exact exclusions and all WinGet pins are respected. Unknown or ambiguous update identities are not automatically included. Store apps use the Microsoft Store route; unsupported applications need their own updater.')

    def _build_app_rules_controls(self, layout, *, native=False):
        from .widgets.app_rules_editor import AppRulesEditor
        reader = getattr(self.backend, 'preferences', None)
        settings = reader() if callable(reader) else {}
        settings = settings if isinstance(settings, dict) else {}
        import copy
        self._app_rules_baseline = copy.deepcopy(settings)
        self.app_rules_enabled = QCheckBox('Include saved app rules and Software Updater in maintenance')
        self.app_rules_enabled.setChecked(settings.get('software_updates_enabled', False))
        layout.addWidget(self.app_rules_enabled)
        self.app_rules_global = QComboBox()
        for title, value in (('Global: review updates; saved per-app rules still apply', 'review'), ('Global: update existing selected IDs', 'selected'),
                             ('Global: update all supported apps except exclusions', 'all_supported')):
            self.app_rules_global.addItem(title, value)
        self.app_rules_global.setCurrentIndex(max(0, self.app_rules_global.findData(settings.get('software_update_policy', 'review'))))
        layout.addWidget(self.app_rules_global)
        self.app_rules_editor = AppRulesEditor(settings, native=native)
        layout.addWidget(self.app_rules_editor, 1)
        self.app_rules_all_approved = QCheckBox('Approve the global all-supported app update scope, including future matching apps')
        self.app_rules_all_approved.setChecked(settings.get('software_update_all_approved', False))
        layout.addWidget(self.app_rules_all_approved)
        self.app_rules_agreements = QCheckBox('Approve my saved automatic app actions and accept their package/source agreements')
        self.app_rules_agreements.setChecked(settings.get('software_update_agreements_accepted', False))
        layout.addWidget(self.app_rules_agreements)
        self.app_rules_save = QPushButton('Save app choices')
        self.app_rules_save.clicked.connect(self._save_app_rules)
        layout.addWidget(self.app_rules_save)
        def policy_changed():
            self.app_rules_agreements.setChecked(False)
            self.app_rules_all_approved.setChecked(False)
        self.app_rules_global.currentIndexChanged.connect(policy_changed)
        self.app_rules_editor.changed.connect(lambda: self.app_rules_agreements.setChecked(False))

    def _save_app_rules(self):
        if self.smoke_test or self.busy:
            self.status.setText('Save is unavailable during an operation or an interface smoke test.')
            return
        try:
            current = self.backend.preferences()
            keys = ('software_updates_enabled', 'software_update_policy', 'software_update_ids',
                'software_update_exclude_ids', 'software_update_all_approved',
                'software_update_agreements_accepted', 'software_app_rules')
            for key in keys:
                default = [] if key.endswith(('_ids', '_rules')) else None
                if current.get(key, default) != self._app_rules_baseline.get(key, default):
                    raise ValueError('Saved app choices changed outside this form. Reopen Settings before replacing them.')
            value = self.app_rules_editor.apply_to(current)
            value.update(software_updates_enabled=self.app_rules_enabled.isChecked(),
                software_update_policy=self.app_rules_global.currentData(),
                software_update_all_approved=self.app_rules_all_approved.isChecked(),
                software_update_agreements_accepted=self.app_rules_agreements.isChecked())
            validator = getattr(getattr(self.backend, 'preference_store', None), 'validate', None)
            if callable(validator):
                value = validator(value)
        except (ValueError, TypeError) as error:
            self._error(str(error)); return
        def saved(result):
            import copy
            settings = result if isinstance(result, dict) else value
            self._app_rules_baseline = copy.deepcopy(settings)
            self.app_rules_editor.reset(settings)
            self.status.setText('App choices saved. Exact identities, available versions, protections and installer checks still apply.')
        self._run('save_preferences', value, done=saved)

    def _updates_ready(self, result):
        self.updates_output.setPlainText(display(field(result,'output','text','Text',default=result)))
        candidates = field(result,'packages','updates',default=[])
        from .automated_software import fresh_candidates
        from .update_scan_evidence import app_scan_evidence
        evidence = app_scan_evidence(result)
        values=[dict(record,id=identity) for identity,record in fresh_candidates(result).items()]
        self.updates_candidates_table.populate(values)
        self.app_rules_editor.set_candidates(values)
        self.updates_summary.setText(evidence['summary'] + ' ' + evidence['coverage_limit']
            + (' Choose a row to fill its exact ID.' if values else '')
            + ' Exclusions/notes: ' + display(field(result,'excluded',default=[])))
        self.loaded_pages.add('Updates')

    def _update_selected(self):
        record=self.updates_candidates_table.selected()
        if record and hasattr(self,'update_id'):
            self.update_id.setText(display(field(record,'id','package_id')))

    def update_package(self):
        value = self.update_id.text().strip()
        if not value:
            self._error('Enter the exact package ID shown in the update result.')
        elif self._confirm('Install application update', 'Update only package ' + value + '? Its installer can close the program, request elevation or require a restart.'):
            self._run('update_app', value, done=self._show_result)

    def _build_drivers(self):
        from PySide6.QtWidgets import QTabWidget
        layout = self.layouts['Drivers']
        bar = self._toolbar(layout)
        self._button(bar, 'Refresh installed drivers', self.load_drivers)
        self._button(bar, 'Check all present hardware coverage', lambda: self._run('driver_coverage', done=self._driver_coverage_ready))
        self._button(bar, 'Scan available driver updates', lambda: self._run('driver_updates_scan', done=self._driver_updates_ready), primary=True)
        self.driver_install_button = self._button(bar, 'Install selected update', self.install_driver_update)
        self.driver_install_button.setEnabled(False)
        self._button(bar, 'Back up installed drivers', self.backup_installed_drivers)
        bar.addStretch()
        native = self._toolbar(layout)
        self._button(native, 'Windows Update', lambda: self._run('system_tool', 'windows-update'))
        self._button(native, 'Optional driver updates', lambda: self._run('system_tool', 'optional-updates'))
        self._button(native, 'Device Manager', lambda: self._run('system_tool', 'device-manager'))
        native.addStretch()
        self.driver_tabs = QTabWidget()
        installed = QWidget()
        installed_layout = QVBoxLayout(installed)
        installed_layout.setContentsMargins(0, 0, 0, 0)
        self.driver_table = DataTable([('Device', 'DeviceName'), ('Provider', 'DriverProviderName|Provider'), ('Version', 'DriverVersion|Version'), ('Date', 'DriverDate|Date'), ('Signed', 'IsSigned'), ('INF', 'InfName')])
        installed_layout.addWidget(self.driver_table)
        available = QWidget()
        available_layout = QVBoxLayout(available)
        available_layout.setContentsMargins(0, 0, 0, 0)
        self.driver_updates_table = DataTable([('Available driver update', 'title'), ('Revision', 'revision'), ('Downloaded', 'downloaded'), ('EULA accepted', 'eulaAccepted')])
        self.driver_updates_table.itemSelectionChanged.connect(lambda: self.driver_install_button.setEnabled(self.driver_updates_table.selected() is not None))
        available_layout.addWidget(self.driver_updates_table)
        self.driver_tabs.addTab(installed, 'Installed driver inventory')
        self.driver_tabs.addTab(available, 'Windows Update candidates')
        hardware=QWidget()
        hardware_layout=QVBoxLayout(hardware)
        self.driver_coverage_summary=self._note(hardware_layout,'No present-device coverage report loaded. No Windows Update offer does not establish that the installed driver is the latest vendor release.')
        self.driver_coverage_table=DataTable([('Present device','displayName'),('Class','deviceClass'),('Manufacturer','manufacturer'),('Coverage','statusDescription'),('Problem code','problemCode'),('Windows Update offers',lambda r:len(field(r,'updateOffers',default=[]) or []))])
        self.driver_coverage_table.itemSelectionChanged.connect(self._driver_coverage_selected)
        hardware_layout.addWidget(self.driver_coverage_table,1)
        support=self._toolbar(hardware_layout)
        support.addWidget(QLabel('Official support:'))
        self.driver_support_links=QComboBox()
        support.addWidget(self.driver_support_links,1)
        self.driver_support_button=self._button(support,'Open selected official support route',self.open_driver_support)
        self.driver_support_button.setEnabled(False)
        self.driver_coverage_details=QPlainTextEdit()
        self.driver_coverage_details.setReadOnly(True)
        self.driver_coverage_details.setMaximumHeight(200)
        hardware_layout.addWidget(self.driver_coverage_details)
        self.driver_tabs.addTab(hardware,'All present hardware coverage')
        self._driver_coverage_report={}
        layout.addWidget(self.driver_tabs, 1)
        self.driver_update_note = self._note(layout, 'Available updates are queried through Windows Update Agent. A version number alone does not prove a newer driver exists. Scanning is read-only. Manual installation asks for confirmation and administrator access; automatic policies reuse only the device/update scope and agreements you saved in setup.')
        self._note(layout, 'Back up third-party driver packages before changing them. Export is handled by Windows pnputil; inbox Windows drivers and device settings may require separate recovery. Hardware-manufacturer tools remain useful for vendor-specific packages.')

    def load_drivers(self):
        self._run('drivers_list', done=lambda r: self._page_table_ready('Drivers', self.driver_table, r))

    def _driver_updates_ready(self, result):
        if isinstance(field(result,'coverage',default=None),dict):
            self._driver_coverage_ready(result['coverage'],activate=False)
        candidates = rows(field(result, 'updates', default=[]))
        self.driver_updates_table.populate(candidates)
        self.driver_tabs.setCurrentIndex(1)
        from .driver_coverage import driver_scan_evidence
        evidence = driver_scan_evidence(result)
        self.driver_update_note.setText(evidence['summary'] + ' Result code: '
            + display(evidence.get('result_code')) + '. Review a title and revision before installation.')

    def _driver_coverage_ready(self, report, *, activate=True):
        self._driver_coverage_report=report
        self.driver_coverage_table.populate(field(report,'devices',default=[]))
        summary=field(report,'summary',default={}) or {}
        self.driver_coverage_summary.setText('Present devices: '+display(field(summary,'devices',default=0))+'; devices with Windows Update offers: '+display(field(summary,'offered',default=0))+'; device problems: '+display(field(summary,'deviceProblems',default=0))+'; no offer: '+display(field(summary,'noOffer',default=0))+'. '+('Device inventory complete. ' if field(report,'inventoryComplete',default=False) else 'Device inventory incomplete. ')+('Update scan complete. ' if field(report,'updateScanComplete',default=False) else 'Update scan incomplete. ')+display(field(report,'notes',default=[])))
        if activate:
            self.driver_tabs.setCurrentIndex(2)
        self._driver_coverage_selected()

    def _driver_coverage_selected(self):
        record=self.driver_coverage_table.selected()
        self.driver_support_links.clear()
        links=list(field(record,'supportLinks',default=[]) or [])+list(field(self._driver_coverage_report,'systemSupport',default=[]) or [])
        seen=set()
        for link in links:
            url=display(field(link,'url'))
            if url and url not in seen:
                self.driver_support_links.addItem(display(field(link,'label',default='Official support')),url)
                seen.add(url)
        self.driver_support_button.setEnabled(bool(seen))
        if not record:
            self.driver_coverage_details.clear()
            return
        installed=field(record,'installedDrivers',default=[]) or []
        versions='; '.join(display(field(value,'version','driverVersion','DriverVersion')) for value in installed)
        offers='; '.join(display(field(value,'title'))+' (revision '+display(field(value,'revision'))+')' for value in field(record,'updateOffers',default=[]) or [])
        self.driver_coverage_details.setPlainText('Device: '+display(field(record,'displayName'))+'\nCoverage: '+display(field(record,'statusDescription','status'))+'\nDevice ID: '+display(field(record,'deviceId'))+'\nHardware IDs: '+display(field(record,'hardwareIds',default=[]))+'\nInstalled driver versions: '+versions+'\nDevice problem: '+display(field(record,'problem'))+' (code '+display(field(record,'problemCode'))+')\nWindows Update offers: '+(offers or 'None observed. This is not proof that the latest vendor driver is installed.')+'\nOfficial links open the manufacturer support site; vendor tools/manual selection can cover devices not offered by Windows Update.')

    def open_driver_support(self):
        from .driver_coverage import is_trusted_support_url
        url=self.driver_support_links.currentData()
        if not isinstance(url,str) or not is_trusted_support_url(url):
            self._error('This is not a recognized official vendor support route.')
            return
        QDesktopServices.openUrl(QUrl(url))

    def _driver_admin(self):
        try:
            import ctypes
            elevated = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            elevated = False
        if not elevated:
            self._error('Use Settings > Appearance and Windows tools > Restart as administrator before installing or exporting drivers.')
        return elevated

    def install_driver_update(self):
        record = self._selected(self.driver_updates_table)
        if record and self._driver_admin() and self._confirm('Install one driver update', 'Install only this Windows Update driver? The device may disconnect temporarily, the driver license may require acceptance, and Windows may request a restart. Back up drivers first.', details=record, phrase='INSTALL DRIVER'):
            self._run('driver_update_apply', record, done=self._show_result)

    def backup_installed_drivers(self):
        if not self._driver_admin():
            return
        destination = QFileDialog.getExistingDirectory(self, 'Choose a folder for Windows driver-package backup', str(Path.home()))
        if destination:
            self._run('backup_drivers', destination, done=self._show_result)

    def _folder_bar(self, layout, callback):
        bar = self._toolbar(layout)
        entry = QLineEdit(str(Path.home()))
        entry.setPlaceholderText('Choose a local folder to scan')
        bar.addWidget(entry, 1)
        self._button(bar, 'Browse', lambda: self._choose_folder(entry))
        self._button(bar, 'Scan selected folder', callback, primary=True)
        return entry

    def _choose_folder(self, entry):
        value = QFileDialog.getExistingDirectory(self, 'Select local folder', entry.text())
        if value:
            entry.setText(value)

    def _build_storage(self):
        layout = self.layouts['Disk analyzer']
        self.disk_root = self._folder_bar(layout, lambda: self._run('storage_scan', self.disk_root.text().strip(), done=self._storage_ready))
        bar = self._toolbar(layout)
        self.disk_view = QComboBox()
        self.disk_view.addItems(['Largest folders', 'Largest files', 'File types'])
        self.disk_view.currentIndexChanged.connect(self._render_storage)
        bar.addWidget(self.disk_view)
        self._button(bar, 'Open selected location', self.open_storage_location)
        bar.addStretch()
        self.disk_table = DataTable([('Folder / file / type', 'name'), ('Size MB', 'mb'), ('Files', 'files')])
        layout.addWidget(self.disk_table, 1)
        self.disk_summary = self._note(layout, 'Existing local sync folders can be scanned. Cloud APIs and online-only downloads are not used. Logical sizes can count hardlinks more than once.')

    def _storage_ready(self, result):
        self.storage_result = result
        self._render_storage()
        count = field(result, 'FilesScanned', 'files_scanned', default=0)
        size = field(result, 'TotalBytes', 'total_bytes', default=0) / (1024 ** 3)
        complete = field(result, 'Complete', default=False)
        errors = field(result, 'ErrorCount', 'error_count', default=0)
        self.disk_summary.setText(f'Measured {count:,} files, {size:,.2f} GB logical. ' + ('Scan complete.' if complete else 'Bounded/incomplete scan; figures are partial.') + f' {errors} access errors. Junctions/placeholders skipped; hardlinks can inflate totals.')

    def _render_storage(self, *_):
        if not self.storage_result:
            return
        index = self.disk_view.currentIndex()
        keys = ('DirectoryGroups', 'directory_groups') if index == 0 else ('TopFiles', 'top_files') if index == 1 else ('ExtensionGroups', 'extension_groups')
        values = field(self.storage_result, *keys, default=[])
        records = []
        for r in rows(values):
            name = field(r, 'Directory', 'FilePath', 'Extension', 'path')
            records.append({'name': name, 'mb': field(r, 'Bytes', default=0) / (1024 ** 2), 'files': field(r, 'Files', default=1), 'path': name if index != 2 else ''})
        self.disk_table.populate(records)

    def open_storage_location(self):
        r = self._selected(self.disk_table)
        if r and r.get('path'):
            path = Path(r['path'])
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path if path.is_dir() else path.parent)))

    def _build_duplicates(self):
        layout = self.layouts['Duplicate finder']
        self.duplicate_root = self._folder_bar(layout, lambda: self._run('duplicate_scan', self.duplicate_root.text().strip(), done=self._duplicates_ready))
        bar = self._toolbar(layout)
        self._button(bar, 'Recycle selected file', self.recycle_duplicate)
        self._button(bar, 'Open selected folder', self.open_duplicate_folder)
        bar.addStretch()
        self.duplicate_table = DataTable([('Group', 'group'), ('Size MB', 'mb'), ('Matching file', 'path'), ('SHA-256', 'sha256')])
        layout.addWidget(self.duplicate_table, 1)
        self.duplicate_summary = self._note(layout, 'Compare contents, then review one file at a time. Matching content is not proof a file is unnecessary. Hardlinks are not independent extra copies.')

    def _duplicates_ready(self, result):
        groups = result if isinstance(result, list) else field(result, 'Groups', default=[])
        values = []
        for index, group in enumerate(rows(groups), 1):
            for c in rows(field(group, 'Candidates', default=[])):
                values.append({'group': index, 'mb': field(c, 'Length', default=field(group, 'SizeBytes', default=0)) / (1024 ** 2), 'path': field(c, 'FilePath', 'path'), 'sha256': field(group, 'SHA256'), '_candidate': c})
        self.duplicate_table.populate(values)
        self.duplicate_summary.setText(f'Found {len(rows(groups)):,} content-match groups and {len(values):,} reviewable files. Only one freshly revalidated selected file can be recycled. Keep a required copy.')

    def recycle_duplicate(self):
        r = self._selected(self.duplicate_table)
        if r and self._confirm('Recycle one duplicate', 'Send this one file to the Windows Recycle Bin? Check that another required copy remains.', details=r['path']):
            self._run('duplicate_recycle', r['_candidate'], done=self._duplicate_recycled)

    def _duplicate_recycled(self, result):
        self.duplicate_table.populate([])
        self._show_result(result)

    def open_duplicate_folder(self):
        r = self._selected(self.duplicate_table)
        if r:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(r['path']).parent)))

    def _build_debloat(self):
        layout = self.layouts['Debloat']
        bar = self._toolbar(layout)
        self.debloat_combo = QComboBox()
        bar.addWidget(self.debloat_combo, 1)
        self._button(bar, 'Refresh actions', self.load_debloat_actions)
        self._button(bar, 'Preview action', self.preview_debloat, primary=True)
        self.debloat_apply_button = self._button(bar, 'Apply reviewed selection', self.apply_debloat)
        self.debloat_apply_button.setEnabled(False)
        self.debloat_combo.currentIndexChanged.connect(self._invalidate_debloat)
        split = QSplitter()
        self.debloat_targets = QListWidget()
        self.debloat_targets.setMinimumWidth(320)
        self.debloat_targets.itemChanged.connect(self._debloat_selection_changed)
        split.addWidget(self.debloat_targets)
        self.debloat_details = QPlainTextEdit()
        self.debloat_details.setReadOnly(True)
        self.debloat_details.setPlaceholderText('Choose an action and preview it. Check only the concrete targets you want to remove. Nothing is selected automatically.')
        split.addWidget(self.debloat_details)
        split.setStretchFactor(0,1)
        split.setStretchFactor(1,1)
        layout.addWidget(split,1)
        self._note(layout,'Bing Wallpaper is protected from debloat. No target is selected by default. Review package names, scope, recovery and restart requirements before applying only your checked selection. Some removals require reinstallation.')

    def load_debloat_actions(self):
        self._run('debloat_actions',done=self._debloat_actions_ready)

    def _debloat_actions_ready(self,actions):
        self.debloat_combo.clear()
        for action in rows(actions):
            self.debloat_combo.addItem(display(field(action,'name')),action)
        self.loaded_pages.add('Debloat')
        self._invalidate_debloat()

    def _invalidate_debloat(self,*_):
        self.debloat_plan=None
        self.debloat_apply_button.setEnabled(False)
        if hasattr(self,'debloat_targets'):
            self.debloat_targets.clear()
        action=self.debloat_combo.currentData()
        if action and hasattr(self,'debloat_details'):
            self.debloat_details.setPlainText(display(field(action,'description')))

    def preview_debloat(self):
        action=self.debloat_combo.currentData()
        if not action:
            self._error('Refresh available actions first.')
        else:
            self._run('debloat_preview',field(action,'id'),done=self._debloat_ready)

    def _debloat_ready(self,plan):
        self.debloat_plan=plan
        self.debloat_targets.blockSignals(True)
        self.debloat_targets.clear()
        can_apply=bool(field(plan,'canApply',default=False))
        for target in rows(field(plan,'targets',default=[])):
            name=display(field(target,'name','displayName','packageName','id'))
            scope=display(field(target,'scope','kind'))
            item=QListWidgetItem(name + ('  ['+scope+']' if scope else ''))
            item.setData(Qt.UserRole,target)
            item.setToolTip(display(field(target,'packageFullName','path','description','id')))
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            if not can_apply:
                item.setFlags(item.flags() & ~Qt.ItemIsEnabled)
            self.debloat_targets.addItem(item)
        self.debloat_targets.blockSignals(False)
        summary=display(field(plan,'summary',default='Review the targets before selecting any removal.'))
        recovery=display(field(plan,'recovery'))
        self.debloat_details.setPlainText(summary+'\n\nRecovery: '+recovery+'\nRestart may be needed: '+display(field(plan,'restartNeeded',default=False))+'\n\nCheck only the target entries you intend to remove.')
        self._debloat_selection_changed()

    def _debloat_selection_changed(self,*_):
        selected=any(self.debloat_targets.item(i).checkState()==Qt.Checked for i in range(self.debloat_targets.count()))
        self.debloat_apply_button.setEnabled(bool(self.debloat_plan and field(self.debloat_plan,'canApply',default=False) and selected))

    def apply_debloat(self):
        if not self.debloat_plan or not field(self.debloat_plan,'canApply',default=False):
            self._error('Create an applicable preview first.')
            return
        targets=[self.debloat_targets.item(i).data(Qt.UserRole) for i in range(self.debloat_targets.count()) if self.debloat_targets.item(i).checkState()==Qt.Checked]
        if not targets:
            self._error('Check at least one reviewed target first.')
            return
        selected_ids=[field(target,'id') for target in targets]
        details={'summary':field(self.debloat_plan,'summary'),'selectedTargets':[{'name':field(t,'name','displayName','packageName','id'),'scope':field(t,'scope','kind'),'id':field(t,'id')} for t in targets],'recovery':field(self.debloat_plan,'recovery')}
        if self._confirm('Apply reviewed debloat selection',f'Apply this removal action to only the {len(targets)} checked target(s)? Application removal may require reinstallation and does not guarantee recovery of app-local data.',details=details,phrase='APPLY REVIEWED PLAN'):
            request={'planId':field(self.debloat_plan,'planId'),'selectedIds':selected_ids,'confirmed':True}
            self._run('debloat_apply',request,done=self._debloat_applied)

    def _debloat_applied(self,result):
        self._invalidate_debloat()
        self._show_result(result)

    def _build_cloud(self):
        from optimus.widgets.cloud_panel import CloudPanel
        self.cloud_panel=CloudPanel(self)
        self.layouts['Cloud drive'].addWidget(self.cloud_panel)

    def _build_schedules(self):
        layout = self.layouts['Schedules']
        bar = self._toolbar(layout)
        self._button(bar, 'Refresh schedules', self.load_schedules)
        self._button(bar, 'Remove selected schedule', self.remove_schedule)
        self._button(bar, 'Task Scheduler', lambda: self._run('system_tool', 'task-scheduler'))
        bar.addStretch()
        self.schedule_table = DataTable([('Task', 'Name|TaskName'), ('State', 'State'), ('Next run', 'NextRunTime|next_run'), ('Action', 'Command|mode')])
        layout.addWidget(self.schedule_table, 1)
        bar = self._toolbar(layout)
        self.schedule_name = QLineEdit('Optimus daily audit')
        self.schedule_time = QLineEdit('18:00')
        self.schedule_time.setMaximumWidth(90)
        bar.addWidget(QLabel('Name:'))
        bar.addWidget(self.schedule_name, 1)
        bar.addWidget(QLabel('Local time:'))
        bar.addWidget(self.schedule_time)
        self._button(bar, 'Create daily read-only audit', self.create_schedule)
        self._note(layout, 'Schedules run read-only local audits in your Windows account. Cleanup, removal and updates are not scheduled automatically.')

    def load_schedules(self):
        self._run('schedule_list', done=lambda r: self._page_table_ready('Schedules', self.schedule_table, r))

    def create_schedule(self):
        name, time = self.schedule_name.text().strip(), self.schedule_time.text().strip()
        if self._confirm('Schedule daily audit', f'Create a Windows task named {name!r} for a read-only audit daily at {time} local time?'):
            self._run('create_schedule', name=name, time=time, mode='audit', done=self._show_result)

    def remove_schedule(self):
        r = self._selected(self.schedule_table)
        name = field(r, 'Name', 'TaskName') if r else ''
        if r and self._confirm('Remove schedule', 'Remove Optimus scheduled task ' + display(name) + '?'):
            self._run('remove_schedule', name, done=self._show_result)

    def _build_registry(self):
        layout = self.layouts['Registry review']
        bar = self._toolbar(layout)
        self._button(bar, 'Scan current-user App Paths', self.load_registry, primary=True)
        self.registry_remove_button = self._button(bar, 'Remove selected registration', self.remove_registry_issue)
        self.registry_remove_button.setEnabled(False)
        self._button(bar, 'Open restore history', lambda: self.select_page('Restore/history'))
        bar.addStretch()
        self.registry_note = self._note(layout, 'Scan is read-only. A missing executable is evidence, not proof that an entry is unused. Only a selected HKCU App Paths entry can be removed; application files are untouched.')
        self.registry_table = DataTable([('Registration', 'Name'), ('Missing executable', 'Target'), ('Scope', 'Scope'), ('Values', 'ValueCount')])
        layout.addWidget(self.registry_table, 1)
        self.registry_details = QPlainTextEdit()
        self.registry_details.setReadOnly(True)
        self.registry_details.setMaximumHeight(200)
        self.registry_details.setPlaceholderText('Select a scan result to review its evidence and exact key path.')
        layout.addWidget(self.registry_details)
        self.registry_table.itemSelectionChanged.connect(self._registry_selection_changed)
        self._note(layout, 'Removal verifies the registration and missing target again, saves all raw registry value types and its access permissions, then records a restore entry. Restore refuses to overwrite a newer registration. System-wide, COM/CLSID, shared DLL and installer entries are excluded.')

    def load_registry(self):
        self.registry_remove_button.setEnabled(False)
        self._run('registry_scan', done=self._registry_ready)

    def _registry_ready(self, result):
        self.registry_table.populate(rows(field(result, 'issues', default=[])))
        self.registry_note.setText(display(field(result, 'note')) + ' Excluded or unreadable entries: ' + display(field(result, 'skipped', default=0)) + '.')
        self.loaded_pages.add('Registry review')
        self._registry_selection_changed()

    def _registry_selection_changed(self):
        record = self.registry_table.selected()
        self.registry_remove_button.setEnabled(bool(record and field(record, 'CanRemove', default=False) is True and field(record, 'Id')))
        self.registry_details.setPlainText(json.dumps(record, indent=2, ensure_ascii=False, default=str) if record else '')

    def remove_registry_issue(self):
        record = self._selected(self.registry_table)
        if not record:
            return
        if field(record, 'CanRemove', default=False) is not True or not field(record, 'Id'):
            self._error('This registration is not approved for selective removal. Scan again.')
            return
        if self._confirm('Remove one executable registration', 'Remove only the selected current-user App Paths registration ' + display(field(record, 'Name')) + '? A missing target does not prove it is unused. Its application files stay in place; a verified raw-value and access-permission backup is required.', details=record, phrase='REMOVE REGISTRATION'):
            self.registry_remove_button.setEnabled(False)
            self._run('registry_remove', record, done=self._registry_removed)

    def _registry_removed(self, result):
        self.registry_table.populate([])
        self.registry_remove_button.setEnabled(False)
        self.loaded_pages.discard('Registry review')
        self._show_result(result)

    def _build_history(self):
        layout = self.layouts['Restore/history']
        bar = self._toolbar(layout)
        self._button(bar, 'Refresh history', self.load_changes)
        self._button(bar, 'Restore selected setting change', self.restore_change)
        bar.addStretch()
        self.changes_table = DataTable([('UTC time', 'TimeUtc|created_at|time'), ('Type', 'Kind'), ('Target', 'Target'), ('Status', 'Status'), ('ID', 'Id')])
        layout.addWidget(self.changes_table, 1)
        self.result_text = QPlainTextEdit()
        self.result_text.setReadOnly(True)
        self.result_text.setMaximumHeight(230)
        self.result_text.setPlaceholderText('Operation results, skipped items and log paths appear here.')
        layout.addWidget(self.result_text)
        self._note(layout, 'Priority, power, selected App Paths registrations and supported startup/service settings have specific restore records. Cache deletion and uninstallers have different recovery requirements; history alone does not guarantee undo.')

    def load_changes(self):
        self._run('changes', done=lambda r: self._page_table_ready('Restore/history', self.changes_table, r))

    def restore_change(self):
        r = self._selected(self.changes_table)
        if r and self._confirm('Restore recorded setting', 'Restore supported settings for ' + display(field(r, 'Target')) + '? Current state and record identity are checked.', details=r):
            self._run('restore', r, done=self._show_result)

    def _build_settings(self):
        layout = self.layouts['Settings']
        panel = QFrame()
        panel.setObjectName('Panel')
        options = QVBoxLayout(panel)
        options.addWidget(QLabel('APPEARANCE'))
        self.theme_combo = QComboBox()
        for key, value in THEMES.items():
            self.theme_combo.addItem(value['label'], key)
        self.theme_combo.setCurrentIndex(max(0, self.theme_combo.findData(self.preferences.get('theme', 'cyber'))))
        options.addWidget(self.theme_combo)
        self.motion_toggle = QCheckBox('Animate reveal and reverse close (respects Windows reduced motion)')
        self.motion_toggle.setChecked(self.config.ui_motion_enabled)
        options.addWidget(self.motion_toggle)
        self._button(options, 'Save appearance', self.save_preferences, primary=True)
        layout.addWidget(panel)
        access = self._toolbar(layout)
        try:
            import ctypes
            admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            admin = False
        access.addWidget(QLabel('Current access: ' + ('Administrator' if admin else 'Standard user') + '. Some system-wide actions require elevation.'), 1)
        self._button(access, 'Restart as administrator', self._restart_as_administrator)
        tools = QGridLayout()
        for index, (label, identifier) in enumerate((('Windows Storage cleanup', 'storage'), ('Optimize drives', 'defrag'), ('Windows Update', 'windows-update'), ('Device Manager', 'device-manager'), ('Installed apps', 'installed-apps'), ('Task Scheduler', 'task-scheduler'))):
            button = QPushButton(label)
            button.clicked.connect(lambda checked=False, key=identifier: self._run('system_tool', key))
            tools.addWidget(button, index // 2, index % 2)
        layout.addLayout(tools)
        self._note(layout, 'Presentation adapted from your MIT-licensed AngeronaSuite: dark palettes, live-content reveal and reverse close. Optimus runs independently of Angerona services and AI providers.')
        self._note(layout, 'Minimize uses the taskbar. Close the main window, choose Exit Optimus here or Quit from the tray to exit after active work finishes. Unsaved Sandbox edits prompt before exit. Logs and settings stay in your local Optimus folder.')
        bar = self._toolbar(layout)
        self._button(bar, 'Exit Optimus', self.quit_application)
        bar.addStretch()
        layout.addStretch()

    def save_preferences(self):
        self.preferences = {'theme': self.theme_combo.currentData(), 'motion': self.motion_toggle.isChecked()}
        self.config.ui_motion_enabled = self.preferences['motion']
        try:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.settings_path.with_suffix('.tmp')
            temporary.write_text(json.dumps(self.preferences, indent=2), encoding='utf-8')
            temporary.replace(self.settings_path)
            self.setStyleSheet(self._qss())
            self.appearanceChanged.emit(self.preferences)
            self.status.setText('Appearance saved.')
        except OSError as exc:
            self._error(str(exc))

    def _restore_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _build_tray(self):
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        self.tray.setToolTip('Optimus: Computer optimization tool')
        menu = QMenu()
        show = QAction('Open Optimus', self)
        show.triggered.connect(self._restore_window)
        quit_action = QAction('Quit Optimus', self)
        quit_action.triggered.connect(self.quit_application)
        menu.addAction(show)
        menu.addSeparator()
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self._restore_window() if reason == QSystemTrayIcon.Trigger else None)
        if not self.smoke_test:
            self.tray.show()

    def closeEvent(self, event):
        if self.busy:
            self.status.setText('Wait for the current operation before closing settings.')
            event.ignore()
        else:
            event.accept()

    def _restart_as_administrator(self, checked=False):
        owner = getattr(self, '_main_window', None)
        if owner is not None:
            return owner.restart_as_administrator()
        self.status.setText('Open Optimus from its main window before restarting as administrator.')
        return False

    def quit_application(self):
        owner = getattr(self, '_main_window', None)
        if owner is not None:
            return owner.quit_application()
        if self.busy and not self._confirm('Exit Optimus', 'An operation is running. Waiting for completion is recommended. Exit the interface now?'):
            return
        self.close()

    def shutdown(self):
        if getattr(self, '_presentation_shutdown', False):
            return
        self._presentation_shutdown = True
        for name in ('smart_maintenance_panel', 'maintenance_toolkit_panel', 'recovery_panel', 'file_utilities_panel', 'worker_board'):
            panel = getattr(self, name, None)
            if panel is not None:
                panel.shutdown()
        if self._privacy_restore_dialog is not None:
            self._privacy_restore_dialog.shutdown()
        if hasattr(self, 'privacy_panel'):
            self.privacy_panel.shutdown()
        if hasattr(self, 'process_harness_panel'):
            self.process_harness_panel.shutdown()
        if hasattr(self, 'speed_enhancements_panel'):
            self.speed_enhancements_panel.shutdown()
        if hasattr(self, '_analysis_timer'):
            self._analysis_timer.stop()
        if getattr(self, '_window_reveal', None) is not None:
            self._window_reveal.shutdown()
        if getattr(self, 'tray', None) is not None:
            self.tray.hide()
        self.pool.waitForDone()


def create_window(backend, *, smoke_test=False):
    return OptimusWindow(backend, smoke_test=smoke_test)



# The primary app intentionally exposes only three actions. The full toolkit
# above remains a separate Settings window.
import math
import random
import threading
import time
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QLinearGradient, QRadialGradient
from optimus.widgets.loading import RunSpinner


class StarfieldCanvas(QWidget):
    def __init__(self, motion=True):
        super().__init__()
        self.motion = bool(motion)
        self.phase = 0.0
        self._motion_clock = QElapsedTimer()
        rng = random.Random(47)
        self.stars = [(rng.random(), rng.random(), rng.random(), rng.random()) for _ in range(190)]
        self.timer = QTimer(self)
        self.timer.setInterval(33)
        self.timer.timeout.connect(self._tick)
        self.setAutoFillBackground(False)

    def _tick(self):
        if not self._can_animate():
            self.timer.stop()
            return
        elapsed = min(0.1, max(0, self._motion_clock.restart()) / 1000)
        self.phase += elapsed * 0.12
        self.update()

    def _can_animate(self):
        owner = self.window()
        if not self.motion or not self.isVisible() or owner.isMinimized() or getattr(owner, 'busy', False):
            return False
        for name in ('_settings_window', '_sandbox_window', '_setup_wizard', '_progress_window', '_security_review_dialog'):
            child = getattr(owner, name, None)
            visible = getattr(child, 'isVisible', None)
            if callable(visible):
                try:
                    if visible():
                        return False
                except RuntimeError:
                    continue
        return True

    def sync_motion(self):
        if self._can_animate():
            if not self.timer.isActive():
                self._motion_clock.start()
            self.timer.start()
        else:
            self.timer.stop()
        self.update()

    def showEvent(self, event):
        super().showEvent(event)
        self.sync_motion()

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        painter.fillRect(self.rect(), QColor('#0a0e14'))
        cx, cy = w * 0.5, h * 0.5
        extent = max(w, h) * 0.72
        glow = QRadialGradient(QPointF(cx, cy), extent)
        glow.setColorAt(0, QColor(17, 53, 87, 165))
        glow.setColorAt(0.42, QColor(10, 29, 51, 130))
        glow.setColorAt(1, QColor(4, 9, 17, 0))
        painter.fillRect(self.rect(), glow)
        for index, (a, b, size, twinkle) in enumerate(self.stars):
            radius = (0.12 + a * 0.86) * extent
            angle = b * math.tau + self.phase * (0.32 + 0.35 * a) + radius / extent * 2.4
            x = cx + math.cos(angle) * radius
            y = cy + math.sin(angle) * radius * 0.63
            if not (-5 < x < w + 5 and -5 < y < h + 5):
                continue
            pulse = 0.68 + 0.32 * math.sin(self.phase * 2.2 + twinkle * 12)
            alpha = int((55 + size * 155) * pulse)
            color = QColor('#ff7a1a') if index % 19 == 0 else QColor('#7acbff')
            color.setAlpha(alpha)
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            dot = 0.7 + size * 1.5
            painter.drawEllipse(QPointF(x, y), dot, dot)
            if size > 0.91:
                faint = QColor(color)
                faint.setAlpha(int(alpha * 0.15))
                painter.setBrush(faint)
                painter.drawEllipse(QPointF(x, y), dot * 3.3, dot * 3.3)
        for strand in range(3):
            painter.setPen(QPen(QColor(31, 156, 255, 15 + strand * 3), 1.0))
            points = []
            for index in range(160):
                t = index / 159
                radius = extent * (0.16 + t * 0.72)
                angle = t * 5.3 + self.phase + strand * math.tau / 3
                points.append(QPointF(cx + math.cos(angle) * radius, cy + math.sin(angle) * radius * 0.63))
            for a, b in zip(points, points[1:]):
                painter.drawLine(a, b)
        painter.end()


class WorkflowSignals(QObject):
    progress = Signal(object)
    result = Signal(object)
    error = Signal(str)
    finished = Signal()


class WorkflowWorker(QRunnable):
    def __init__(self, backend, settings, cancel, *, prompt_parent=None):
        super().__init__()
        self.backend, self.settings, self.cancel = backend, settings, cancel
        self.signals = WorkflowSignals()
        self._browser_prompts = None
        from .unattended_configuration import matches_unattended
        self._unattended = matches_unattended(settings)
        if not self._unattended and (settings.get('browser_cache_enabled', True) or any(settings.get(key, False) for key in (
                'browser_history_enabled', 'browser_cookies_enabled', 'browser_download_history_enabled'))):
            from .browser_prompts import BrowserPromptBridge
            self._browser_prompts = BrowserPromptBridge(prompt_parent)

    def run(self):
        from .runtime_priority import lower_worker_priority
        restore_priority = lower_worker_priority()
        try:
            kwargs = {'progress_callback': self.signals.progress.emit, 'cancel_event': self.cancel}
            if self._unattended:
                def approved_browser_choice(info):
                    from .unattended_configuration import matches_unattended
                    try:
                        current = self.backend.preferences()
                        return (not self.cancel.is_set() and matches_unattended(current)
                            and current.get('unattended_scope_hash') == self.settings.get('unattended_scope_hash')
                            and self.settings.get('unattended_browser_policy') == 'close_gracefully')
                    except (AttributeError, OSError, ValueError, TypeError):
                        return False
                kwargs['browser_prompt'] = approved_browser_choice
            elif self._browser_prompts is not None:
                kwargs['browser_prompt'] = lambda info: self._browser_prompts.request(info, self.cancel)
            result = self.backend.optimize(self.settings, **kwargs)
            self.signals.result.emit(result)
        except Exception as exc:
            self.signals.error.emit(str(exc))
        finally:
            restore_priority()
            self.signals.finished.emit()


class AvailableFixesWorker(QRunnable):
    """Run only the batch explicitly approved on the completed results screen."""
    def __init__(self, backend, report, settings, cancel):
        super().__init__()
        self.backend, self.report, self.settings, self.cancel = backend, report, settings, cancel
        self.signals = WorkflowSignals()

    def run(self):
        from .runtime_priority import lower_worker_priority
        restore_priority = lower_worker_priority()
        try:
            result = self.backend.apply_available_fixes(self.report, self.settings,
                progress_callback=self.signals.progress.emit, cancel_event=self.cancel)
            self.signals.result.emit(result)
        except Exception as exc:
            self.signals.error.emit(str(exc))
        finally:
            restore_priority()
            self.signals.finished.emit()


def _compact_worker_reason(text):
    """Use a short factual caption for the recurring unresolved-installer guard."""
    lowered = text.casefold()
    if ('previous update installer' in lowered and 'unresolved outcome' in lowered
            and 'no automatic retry was started' in lowered):
        return 'Previous installer needs review.\nNo automatic retry started.'
    return text


class WorkflowSummaryLabel(QLabel):
    """Paint bounded fitting lines while retaining complete accessible text."""
    def __init__(self, text='', parent=None, *, max_lines=2):
        super().__init__(parent)
        self._max_lines = max_lines if max_lines in (1, 2, 3) else 2
        self._summary = ''
        self.visible_text = ''
        self.setTextFormat(Qt.PlainText)
        self.setWordWrap(False)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.setFixedHeight(self.fontMetrics().lineSpacing() * self._max_lines + 4)
        self.setText(text)

    def setText(self, text):
        self.set_summary(text, text)

    def set_max_lines(self, lines):
        self._max_lines = max(1, min(3, int(lines)))
        self.setFixedHeight(self.fontMetrics().lineSpacing() * self._max_lines + 4)
        self._fit_summary()

    def clear(self):
        self.setText('')

    def set_summary(self, summary, full_text):
        self._summary = '\n'.join(' '.join(line.split()) for line in str(summary).splitlines())
        full_text = str(full_text)
        super().setText(full_text)
        self.setToolTip(full_text)
        self.setAccessibleDescription(full_text)
        self.setWhatsThis(full_text)
        self._fit_summary()

    def _fit_summary(self):
        text, metrics, width = self._summary, self.fontMetrics(), max(1, self.contentsRect().width())
        if self._max_lines == 3:
            parts = text.splitlines()
            if len(parts) > 3:
                parts = parts[:2] + [' '.join(parts[2:])]
            self.visible_text = '\n'.join(metrics.elidedText(part, Qt.ElideRight, width) for part in parts)
            self.update()
            return
        if self._max_lines == 1:
            self.visible_text = metrics.elidedText(' '.join(text.split()), Qt.ElideRight, width)
            self.update()
            return
        first, separator, rest = text.partition('\n')
        if separator and metrics.horizontalAdvance(first) <= width:
            self.visible_text = first + '\n' + metrics.elidedText(' '.join(rest.split()), Qt.ElideRight, width)
            self.update()
            return
        text = ' '.join(text.split())
        if metrics.horizontalAdvance(text) <= width:
            self.visible_text = text
        else:
            low, high = 0, len(text)
            while low < high:
                middle = (low + high + 1) // 2
                if metrics.horizontalAdvance(text[:middle]) <= width:
                    low = middle
                else:
                    high = middle - 1
            boundary = text.rfind(' ', 0, low + 1)
            split = boundary if boundary >= max(1, low // 2) else low
            if split:
                self.visible_text = (text[:split].rstrip() + '\n'
                    + metrics.elidedText(text[split:].lstrip(), Qt.ElideRight, width))
            else:
                self.visible_text = metrics.elidedText(text, Qt.ElideRight, width)
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_summary()

    def changeEvent(self, event):
        super().changeEvent(event)
        if hasattr(self, '_summary') and event.type() in (QEvent.FontChange, QEvent.StyleChange):
            self.setFixedHeight(self.fontMetrics().lineSpacing() * self._max_lines + 4)
            self._fit_summary()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setPen(self.palette().color(self.foregroundRole()))
        painter.drawText(self.contentsRect(), (self.alignment() & Qt.AlignHorizontal_Mask) | Qt.AlignVCenter, self.visible_text)


class WorkflowProgressBar(QProgressBar):
    """Keep the full counter accessible while painting a caption that fits."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.visible_text = ''
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self._fit_caption()

    def _fit_caption(self):
        text = self.text()
        self.visible_text = self.fontMetrics().elidedText(text, Qt.ElideRight, max(1, self.width() - 12))
        self.setToolTip(text)
        self.setAccessibleDescription(text)
        self.setFixedHeight(max(18, self.fontMetrics().height() + 8))
        self.update()

    def setFormat(self, text):
        super().setFormat(text)
        self._fit_caption()

    def setValue(self, value):
        super().setValue(value)
        self._fit_caption()

    def setRange(self, minimum, maximum):
        super().setRange(minimum, maximum)
        self._fit_caption()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_caption()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.FontChange, QEvent.StyleChange):
            self._fit_caption()

    def paintEvent(self, event):
        option = QStyleOptionProgressBar()
        self.initStyleOption(option)
        option.text = self.visible_text
        painter = QPainter(self)
        self.style().drawControl(QStyle.CE_ProgressBar, option, painter, self)


def _worker_bytes(value):
    """Format confirmed logical bytes without claiming an installed footprint."""
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'):
        if value < 1024 or unit == 'PiB':
            return (f'{value:,} B' if unit == 'B' else f'{value:.1f} {unit}')
        value /= 1024


class WorkflowWorkerCard(QFrame):
    """A persistent worker status, with observed item counts and no invented ETA."""
    navigationRequested = Signal(str, object)

    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.setObjectName('WorkflowWorkerCard')
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setStyleSheet('QFrame#WorkflowWorkerCard {background:#101925;border:1px solid #293c4f;border-radius:7px;}')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(9, 7, 9, 7)
        layout.setSpacing(5)
        layout.setAlignment(Qt.AlignTop)
        self._compact = False
        from .widgets.live_activity import WorkerActivityRing
        header = QHBoxLayout()
        heading_layout = QVBoxLayout()
        self._header_layout, self._heading_layout = header, heading_layout
        heading_layout.setSpacing(2)
        self.heading = WorkflowSummaryLabel(title)
        self.heading.setMinimumWidth(0)
        self.heading.setStyleSheet('font-weight:700;color:#78c8ff;')
        heading_layout.addWidget(self.heading)
        self.completion = WorkflowSummaryLabel('Overall: —', max_lines=1)
        heading_layout.addWidget(self.completion)
        self.activity_ring = WorkerActivityRing(self)
        if getattr(parent, 'pinned', False):
            self.activity_ring.hide()
            self.activity_ring.deleteLater()
            from .widgets.compact_worker_ring import CompactWorkerActivityRing
            self.activity_ring = CompactWorkerActivityRing(self)
        header.addLayout(heading_layout, 1)
        header.addWidget(self.activity_ring, 0, Qt.AlignRight)
        layout.addLayout(header)
        self._raw_status = 'not_started'
        self._scan_completion_verified = False
        self._last_progress = None
        self._item_progress = None
        self._live_worker_metrics = {}
        self._live_computer_metrics = {}
        self._progress_activity = QElapsedTimer()
        self.status = WorkflowSummaryLabel('Not started')
        self._status_text = 'Not started'
        self._current_item_label = ''
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.details = WorkflowSummaryLabel('Waiting for this run to start.')
        layout.addWidget(self.details)
        self.progress = WorkflowProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setFormat('No item count yet')
        layout.addWidget(self.progress)
        self.totals = WorkflowSummaryLabel(max_lines=3)
        self.totals.setObjectName('WorkerResourceTotals')
        self._resource_totals = {}
        self._resource_scope = ''
        self._maintenance = False
        layout.addWidget(self.totals)
        self.reset_resource_totals()
        self.elapsed = WorkflowSummaryLabel('Elapsed: —', max_lines=1)
        self._elapsed_base = None
        self._elapsed_clock = QElapsedTimer()
        self._elapsed_active = False
        layout.addWidget(self.elapsed)
        self.activity_state = WorkflowSummaryLabel('Activity: unverified')
        layout.addWidget(self.activity_state)
        self._navigation_state = {}
        self._navigation_revision = None
        self.navigation_buttons = {}
        self._navigation_presses = {}
        navigation = QHBoxLayout()
        self._navigation_layout = navigation
        navigation.setSpacing(5)
        for action, text, name in (('back', '←', 'Previous item'),
                                   ('next', '→', 'Skip to next item'),
                                   ('cancel', '×', 'Cancel current item')):
            button = QPushButton(text)
            button.setObjectName('WorkerNavigation_' + action)
            button.setAutoDefault(False)
            button.setDefault(False)
            button.setFixedHeight(30)
            button.setMinimumWidth(30)
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            button.setStyleSheet('padding:3px 5px;font-size:18px;font-weight:700;min-width:24px;')
            button.setAccessibleName(title + ': ' + name)
            button.setProperty('_optimus_navigation_action', action)
            button.installEventFilter(self)
            button.pressed.connect(lambda action=action: self._capture_navigation_press(action))
            button.released.connect(lambda action=action: self._clear_navigation_press_later(action))
            button.clicked.connect(lambda checked=False, action=action: self._emit_navigation_press(action))
            self.navigation_buttons[action] = button
            navigation.addWidget(button, 1)
        layout.addLayout(navigation)
        self.navigation_note = WorkflowSummaryLabel()
        layout.addWidget(self.navigation_note)
        self.set_navigation({}, enabled=False)

    def set_compact(self, compact=True):
        """Keep the live controls visible; full secondary text stays in Details."""
        if not compact or self._compact:
            return
        self._compact = True
        layout = self.layout()
        while layout.count():
            layout.takeAt(0)
        while self._header_layout.count():
            self._header_layout.takeAt(0)
        while self._heading_layout.count():
            self._heading_layout.takeAt(0)
        layout.setContentsMargins(7, 3, 7, 3)
        layout.setSpacing(1)
        self.heading.set_max_lines(1)
        self.status.set_max_lines(1)
        self.totals.set_max_lines(1)
        self.completion.hide()
        from .widgets.compact_worker_ring import CompactWorkerHeader
        self._compact_header = CompactWorkerHeader(self.heading, self.status, self.activity_ring, self)
        layout.addWidget(self._compact_header)
        layout.addWidget(self.progress)
        layout.addWidget(self.totals)
        layout.addLayout(self._navigation_layout)
        for label in (self.details, self.elapsed, self.activity_state, self.navigation_note):
            label.hide()
        self._fit_status()

    def _capture_navigation_press(self, action):
        import copy
        self._navigation_presses[action] = copy.deepcopy(self._navigation_state)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.MouseButtonRelease, QEvent.KeyRelease, QEvent.Hide, QEvent.FocusOut):
            action = watched.property('_optimus_navigation_action')
            if action in ('back', 'next', 'cancel'):
                self._clear_navigation_press_later(action)
        return super().eventFilter(watched, event)

    def _clear_navigation_press_later(self, action):
        state = self._navigation_presses.get(action)
        # Qt emits clicked immediately after released. Clear an aborted click
        # on the next event turn, without clearing a later new press.
        QTimer.singleShot(0, lambda: self._navigation_presses.pop(action, None)
            if self._navigation_presses.get(action) is state else None)

    def _emit_navigation_press(self, action):
        state = self._navigation_presses.pop(action, None)
        if isinstance(state, dict):
            self.navigationRequested.emit(action, state)

    def set_navigation(self, state, *, enabled=True):
        state = state if isinstance(state, dict) else {}
        revision = (state.get('session_id'), state.get('revision'))
        if revision != self._navigation_revision:
            self.navigation_note.clear()
            self._navigation_revision = revision
            if self._compact:
                self._show_live_status()
        self._navigation_state = dict(state)
        valid = (enabled is True and isinstance(state.get('session_id'), str)
                 and bool(state['session_id']) and type(state.get('revision')) is int
                 and state['revision'] >= 0 and not state.get('pending'))
        captions = {'back': 'Return to the previous eligible item after current work stops safely.',
                    'next': 'Skip this item and continue after its current operation returns.',
                    'cancel': 'Cancel this item at a safe boundary.'}
        for action, button in self.navigation_buttons.items():
            allowed = valid and state.get('can_' + action) is True
            button.setEnabled(allowed)
            reason = state.get(action + '_reason')
            if not allowed and state.get('pending') and isinstance(state.get('message'), str):
                reason = state['message']
            if not isinstance(reason, str) or not reason.strip():
                reason = captions[action] if allowed else 'No live eligible item. Completed changes remain in results; supported undo is in Revert.'
            button.setToolTip(reason[:1500])
            button.setAccessibleDescription(reason[:1500])
        self.navigation_buttons['next'].setAccessibleName(self.heading.text() + ': '
            + ('Next saved result' if state.get('next_mode') == 'history' else 'Skip to next item'))
        message = state.get('message')
        if state.get('viewing_history') is True and isinstance(state.get('view_item'), dict):
            item = state['view_item']
            message = 'Previous result: ' + str(item.get('label') or item.get('key') or 'Saved item')[:500]
            message += ' — ' + str(item.get('status') or 'Outcome recorded')[:80]
            if item.get('message'):
                message += '\n' + str(item['message'])[:800]
        if isinstance(message, str) and message.strip():
            self.set_navigation_message(message, prominent=bool(state.get('pending')))
        else:
            self.navigation_note.setVisible(not self._compact and bool(self.navigation_note.text()))
            if self._compact and not self.navigation_note.text():
                self._show_live_status()
        self._fit_status()

    def set_navigation_message(self, message, *, prominent=True):
        message = str(message)[:1500]
        self.navigation_note.setText(message)
        self.navigation_note.setVisible(not self._compact and bool(message))
        if self._compact:
            # A click must acknowledge immediately without increasing the card
            # height or requiring the user to open the log.
            if prominent:
                self.status.setText(message)
            else:
                self._show_live_status()
        self._fit_status()

    def _show_live_status(self):
        summary = self._status_text
        if self._compact and self._current_item_label:
            summary += ' · ' + self._current_item_label
        # Keep raw worker status separate from its bounded presentation. The
        # complete current item is still available to hover and screen readers.
        self.status.set_summary(summary, self._status_text)
        self.status.setToolTip(summary)
        self.status.setAccessibleDescription(summary)
        self.status.setWhatsThis(summary)

    def _fit_status(self):
        self.status._fit_summary()
        if self._compact:
            metrics = tuple((label.font().key(), label.fontMetrics().lineSpacing(), label._max_lines)
                for label in (self.heading, self.status, self.totals))
            changed = metrics != getattr(self, '_compact_layout_metrics', None)
            self._compact_layout_metrics = metrics
            for label in (self.heading, self.status, self.totals):
                height = label.fontMetrics().lineSpacing() * label._max_lines + 4
                if label.height() != height:
                    label.setFixedHeight(height)
                    changed = True
                label._fit_summary()
            height = max(self.activity_ring.height(), self.heading.height() + self.status.height())
            if self._compact_header.height() != height:
                self._compact_header.setFixedHeight(height)
                changed = True
            if changed:
                self.layout().invalidate()
                self.layout().activate()
            self._compact_header.fit()
        margins = self.contentsMargins()
        height = self.layout().minimumSize().height() + margins.top() + margins.bottom()
        if self.minimumHeight() != height:
            self.setMinimumHeight(height)

    def event(self, event):
        result = super().event(event)
        if event.type() == QEvent.LayoutRequest and hasattr(self, 'navigation_note'):
            self._fit_status()
        return result

    def minimum_readable_width(self):
        # Font scaling may need fewer columns even when the window width is
        # unchanged. Full long captions remain available through accessibility.
        metrics = self.completion.fontMetrics()
        margins = self.layout().contentsMargins()
        if self._compact:
            return max(190, metrics.horizontalAdvance('Overall: 100%') + self.activity_ring.width()
                + margins.left() + margins.right() + 8)
        return max(245, metrics.horizontalAdvance('Overall: blocked') + self.activity_ring.width()
            + margins.left() + margins.right() + 12,
            metrics.horizontalAdvance('Downloaded: size not reported') + margins.left() + margins.right(),
            metrics.horizontalAdvance('Completed installs: 999 apps') + margins.left() + margins.right())

    def reset_resource_totals(self):
        self._resource_scope = ''
        self._set_resource_totals({}, maintenance=self._maintenance or self.heading.text() == 'Maintenance')

    def _set_resource_totals(self, value, *, maintenance=False, scope=None):
        if isinstance(value, dict):
            self._resource_totals = dict(value)
        if isinstance(scope, str):
            self._resource_scope = scope if scope == 'last_check' else ''
        if self._resource_scope == 'last_check':
            maintenance = True
        values = self._resource_totals
        valid = lambda key: type(values.get(key)) is int and 0 <= values[key] <= 2 ** 63 - 1
        removed_known = values.get('removed_known') is True and valid('removed_bytes') and valid('removed_files')
        downloaded_known = values.get('downloaded_known') is True and valid('downloaded_bytes')
        installed_known = values.get('installed_known') is True and valid('installed_items')
        lines, details = [], []
        if maintenance or removed_known:
            lines.append(('Removed: ' + _worker_bytes(values['removed_bytes'])
                + f" · {values['removed_files']:,} " + ('file' if values['removed_files'] == 1 else 'files'))
                if removed_known else 'Removed: size not reported')
            details.append('Removed size is the logical length of confirmed removed files, not a measurement of free disk space gained.')
        if not maintenance or installed_known or downloaded_known:
            lines.append(('Completed installs: ' + f"{values['installed_items']:,} "
                + ('app' if self.heading.text() == 'App updates' and values['installed_items'] == 1 else
                   'apps' if self.heading.text() == 'App updates' else
                   'update' if values['installed_items'] == 1 else 'updates'))
                if installed_known else 'Installs: not reported')
            download = ('Downloaded: ' + ('~' if values.get('downloaded_estimated') is True else '')
                + _worker_bytes(values['downloaded_bytes'])
                + (' observed' if values.get('downloaded_partial') is True else '')) if downloaded_known else 'Downloaded: size not reported'
            # Update cards reserve three aligned rows. Maintenance can include
            # installations without treating their download size as disk usage.
            if maintenance and len(lines) == 2:
                details.append(download)
            else:
                lines.append(download)
            details.append('Completed install counts include only successful native completion receipts or independently verified installations. Downloaded bytes are reported transfer bytes, not installed disk size.'
                + (' Some successful completions were reported by the package provider without an independent installed-version readback.' if values.get('installed_reported') is True else '')
                + (' Some package transfer sizes are unavailable or incomplete.' if values.get('downloaded_partial') is True else '')
                + (' The native transfer reporter supplied approximate bytes.' if values.get('downloaded_estimated') is True else ''))
        if self._resource_scope == 'last_check':
            lines.append('Latest smart check')
            details.append('These totals cover only the latest Smart Maintenance check; they are separate from this one-click run and lifetime totals.')
        summary = '\n'.join(lines[:3])
        if self._compact:
            summary = ' · '.join(lines[:3]).replace('Completed installs:', 'Installs:').replace('Downloaded:', 'Down:')
        self.totals.set_summary(summary, '\n'.join(lines + details))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_status()

    def changeEvent(self, event):
        super().changeEvent(event)
        if hasattr(self, 'status') and event.type() in (QEvent.FontChange, QEvent.StyleChange):
            self._fit_status()

    def update_state(self, state, *, maintenance=False):
        state = state if isinstance(state, dict) else {}
        self._maintenance = maintenance
        self._set_resource_totals(state.get('resource_totals'), maintenance=maintenance,
            scope=state.get('resource_totals_scope'))
        raw_status = str(state.get('status', 'not_started'))
        self._raw_status = raw_status
        scan_worker = state.get('operation_kind') == 'antivirus_scan'
        self._scan_completion_verified = scan_worker and state.get('completion_verified') is True
        from .item_progress import normalize_item_progress
        # Every snapshot supplies its own item. Missing/unknown progress must
        # clear the old phase rather than inheriting the last package's value.
        self._item_progress = normalize_item_progress(state.get('item_progress'))
        self._finished_with_skips = (not maintenance and state.get('presentation_status') == 'finished_with_skips'
            and state.get('worker_finished') is True and state.get('continuation_blocked') is False
            and raw_status in ('needs_review', 'completed', 'completed_with_errors'))
        percentage = state.get('percentage') if maintenance or scan_worker else None
        current, total = state.get('current'), state.get('total')
        if not maintenance and type(current) is int and type(total) is int and 0 <= current <= total and total > 0:
            percentage = current / total * 100
        if (isinstance(percentage, (int, float)) and not isinstance(percentage, bool)
                and math.isfinite(percentage) and 0 <= percentage <= 100):
            if percentage != self._last_progress:
                self._progress_activity.start()
            self._last_progress = percentage
            self.completion.setText(f'Overall: {percentage:.0f}%')
        elif raw_status == 'completed':
            self.completion.setText('Overall: 100%')
        else:
            self.completion.setText('Overall: —')
        installation_blocked = state.get('installation_blocked') is True
        if installation_blocked:
            self.completion.setText('Overall: blocked')
        scan_complete = state.get('scan_complete') is True
        offers = state.get('offers_count')
        scan_label = state.get('scan_label')
        # Old saved workflows stopped at the prior-installer guard before any
        # discovery ran. Their zero-second placeholder is not a completed search.
        legacy_unsearched = (not any(key in state for key in ('scan_complete', 'scan_status', 'scan_started'))
            and not scan_label and raw_status in ('deferred', 'needs_review')
            and state.get('current', 0) == state.get('total', 0) == 0
            and state.get('elapsed_seconds') == 0
            and _compact_worker_reason(str(state.get('description', ''))).startswith('Previous installer needs review.'))
        scan_not_started = (state.get('scan_status') == 'not_started'
            or state.get('scan_started') is False or scan_label in ('Scan not started', 'Search not started')
            or legacy_unsearched)
        labels = {'not_started': 'Not started', 'not_run': 'Not run', 'not_selected': 'Not selected', 'pending': 'Queued',
                  'running': 'Running', 'waiting': 'Running; maintenance is waiting',
                  'scanning': 'Scanning', 'preparing': 'Preparing', 'installing': 'Installing', 'uninstalling': 'Uninstalling',
                  'waiting_for_maintenance': 'Waiting for maintenance',
                  'waiting_for_installer': 'Waiting for installer',
                  'waiting_for_preparation': 'Waiting for update preparation',
                  'completed': 'Completed', 'completed_with_errors': 'Completed; review findings',
                  'deferred': 'Deferred', 'cancelled': 'Cancelled', 'failed': 'Failed',
                  'needs_review': 'Needs review', 'stopped': 'Stopped'}
        self.status.setText({'software_updates': 'Waiting for application updates', 'driver_updates': 'Waiting for driver updates'}
                            .get(state.get('waiting_for'), 'Waiting for update lane') if maintenance and raw_status == 'waiting' else
                            labels.get(raw_status, raw_status.replace('_', ' ').capitalize()))
        if (not maintenance and raw_status in ('running', 'installing') and self._item_progress is not None
                and self._item_progress.get('phase') in ('download', 'downloading')):
            self.status.setText('Downloading')
        if raw_status in ('running', 'installing', 'uninstalling') and self._item_progress is not None:
            item_phase = self._item_progress.get('phase')
            operation = {'install': 'Installing', 'installing': 'Installing',
                         'uninstall': 'Uninstalling', 'uninstalling': 'Uninstalling'}.get(item_phase)
            if operation:
                item_percent = self._item_progress.get('percent')
                percent_text = ('percentage unavailable' if item_percent is None else
                    ('~' if self._item_progress.get('estimated') is True else '') + str(int(item_percent)) + '%')
                self.status.setText(operation + ' · ' + percent_text)
        if self._finished_with_skips:
            self.status.setText('Finished with skips' + ('; scan incomplete' if state.get('scan_complete') is not True else ''))
        self.status.setStyleSheet('color:#e8b34b;' if self._finished_with_skips else '')
        if installation_blocked and raw_status in ('deferred', 'needs_review', 'completed', 'completed_with_errors', 'not_run', 'failed'):
            self.status.setText('Search not started; install blocked' if scan_not_started else
                'Updates found; install blocked' if scan_complete and type(offers) is int and offers > 0 else
                'Search complete; install blocked' if scan_complete else 'Search needs review; install blocked')
        self._status_text = self.status.text()
        label = self._item_progress.get('label') if self._item_progress else state.get('current_package')
        self._current_item_label = (' '.join(label.split())[:300] if isinstance(label, str)
            and raw_status not in ('not_started', 'not_selected', 'not_run', 'completed', 'failed',
                'cancelled', 'stopped', 'deferred', 'needs_review', 'completed_with_errors') else '')
        if self._compact:
            self._show_live_status()
        self._fit_status()
        description = display(state.get('description') or 'Waiting for this run to start.')
        reason = state.get('wait_reason')
        if isinstance(reason, str) and reason.strip() and reason not in description:
            description = (reason + ' — ' + description if raw_status.startswith('waiting_for_')
                           else description + ' — ' + reason)
        block_reason = state.get('installation_block_reason')
        if isinstance(block_reason, str) and block_reason.strip() and block_reason not in description:
            description += ' — ' + block_reason
        package = state.get('current_package')
        if isinstance(package, str) and package.strip() and package not in description:
            description += ' — ' + package
        summary = _compact_worker_reason(description)
        if installation_blocked and scan_complete:
            summary = (str(scan_label) + '\n' if isinstance(scan_label, str) and scan_label else '') + (
                'Previous installer needs review.' if state.get('blocked_by_prior_installer') is True else
                'Installation blocked. Review the saved reason.')
        self.details.set_summary(summary, description)
        current, total = state.get('current'), state.get('total')
        if scan_worker and type(percentage) in (int, float) and math.isfinite(percentage) and 0 <= percentage <= 100:
            self.progress.setRange(0, 100)
            self.progress.setValue(int(percentage))
            self.progress.setFormat(('~' if self._item_progress and self._item_progress.get('estimated') else '') + '%p% scan progress')
        elif scan_worker:
            self.progress.setRange(0, 1)
            self.progress.setValue(0)
            self.progress.setFormat('Percentage unavailable' if raw_status in ('running', 'scanning') else
                'Not started' if raw_status == 'not_started' else 'Scan completion unverified')
        elif maintenance:
            self.progress.setRange(0, 100)
            self.progress.setValue(max(0, min(100, int(state.get('percentage') or 0))))
            self.progress.setFormat('%p% of workflow stages')
        elif not installation_blocked and (type(current) is int and type(total) is int and 0 < total <= 1000000
              and 0 <= current <= total):
            self.progress.setRange(0, total)
            self.progress.setValue(current)
            self.progress.setFormat('%p% overall · %v / %m processed' + (' · ' + scan_label
                if self._finished_with_skips and not scan_complete and isinstance(scan_label, str) and scan_label else ''))
        else:
            self.progress.setRange(0, 1)
            self.progress.setValue(0)
            self.progress.setFormat('Search not started' if scan_not_started else
                                    str(scan_label)[:200] if isinstance(scan_label, str) and scan_label else
                                    'Not selected' if raw_status == 'not_selected' else
                                    'Queued' if raw_status == 'pending' else
                                    'Waiting' if raw_status.startswith('waiting_for_') else
                                    'Scanning; total unknown' if raw_status == 'scanning' else
                                    'Preparing' if raw_status == 'preparing' else 'No item count reported')
        elapsed = state.get('elapsed_seconds')
        if scan_not_started:
            self._elapsed_base = None
        elif (isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool)
                and 0 <= elapsed < 3155760000):
            self._elapsed_base = float(elapsed)
        elif raw_status in ('not_started', 'not_selected', 'not_run'):
            self._elapsed_base = None
        elif self._elapsed_base is not None and self._elapsed_active and self._elapsed_clock.isValid():
            self._elapsed_base += self._elapsed_clock.elapsed() / 1000
        self._elapsed_active = not scan_not_started and raw_status in ('pending', 'running', 'waiting', 'scanning', 'preparing', 'installing', 'uninstalling',
                                             'waiting_for_maintenance', 'waiting_for_installer', 'waiting_for_preparation')
        self._elapsed_clock.start()
        self.refresh_elapsed()
        self.set_live_metrics(self._live_worker_metrics, self._live_computer_metrics)

    def set_live_metrics(self, metrics, computer=None):
        metrics = metrics if isinstance(metrics, dict) else {}
        computer = computer if isinstance(computer, dict) else {}
        self._live_worker_metrics, self._live_computer_metrics = dict(metrics), dict(computer)
        cpu = metrics.get('cpu_percent')
        terminal = self._raw_status in ('completed', 'completed_with_errors', 'failed', 'needs_review',
            'cancelled', 'stopped', 'not_started', 'not_selected', 'not_run', 'deferred')
        if getattr(self, '_finished_with_skips', False):
            health, label = 'waiting', 'Finished with skips; review findings'
        elif self._raw_status in ('failed', 'needs_review', 'completed_with_errors'):
            health, label = 'error', 'Needs attention'
        elif self._raw_status in ('not_started', 'not_selected', 'not_run'):
            health, label = 'unverified', 'Not running'
        elif self._raw_status == 'completed':
            health, label = 'complete', 'Finished'
        elif self._raw_status in ('cancelled', 'stopped', 'deferred') or self._navigation_state.get('pending'):
            health, label = 'waiting', 'Stopping / deferred' if terminal else 'Stopping safely'
        elif self._raw_status.startswith('waiting') or self._raw_status == 'pending':
            health, label = 'waiting', 'Waiting for a prerequisite'
        elif computer.get('pressure') is True or computer.get('pressure') == 'high':
            health, label = 'pressure', 'System under resource pressure'
        elif (isinstance(cpu, (int, float)) and not isinstance(cpu, bool) and math.isfinite(cpu) and 0 <= cpu <= 100):
            observed = cpu > 0.01 or self._progress_activity.isValid() and self._progress_activity.elapsed() < 8000
            health, label = ('working', 'Activity observed') if observed else ('waiting', 'Waiting / external work')
        else:
            health, label = 'unverified', 'Activity not yet measurable'
        verified_scan = (self._scan_completion_verified and isinstance(self._item_progress, dict)
            and self._item_progress.get('phase') == 'scan' and self._item_progress.get('percent') == 100)
        item = self._item_progress if not terminal or verified_scan else None
        percent = item.get('percent') if item is not None else None
        estimated = item.get('estimated') is True if item is not None else False
        phase = item.get('phase', '').replace('_', ' ') if item is not None else ''
        detail = ((item['label'] + ' · ' + phase.capitalize() + '. ') if item is not None else '')
        estimate_source = ('% reported by Defender.' if item and item.get('progress_source') == 'defender_mpclient' else
            '% estimated by the Defender scan provider; not whole-disk coverage.' if item and item.get('progress_source') == 'defender_cim_progress' else
            '% from native byte counters.')
        detail += (('Approximate current operation: ' + str(int(percent))
                    + estimate_source if estimated else
                    'Current operation: ' + str(int(percent)) + '% reported.') if percent is not None else
                   'This operation has not reported a percentage; the ring is indeterminate while running.')
        detail += ' Queue and workflow totals are shown separately in the bars.'
        observed_phase = (item.get('phase', '') if item is not None else
            'scan' if self._raw_status == 'scanning' else
            'waiting' if self._raw_status.startswith('waiting') or self._raw_status == 'pending' else
            'working' if not terminal else '')
        self.activity_ring.set_progress(percent, health, active=not terminal, description=detail,
            phase=observed_phase, estimated=estimated)
        self.activity_state.setText(label)
        self.activity_state.setStyleSheet('color:' + self.activity_ring.COLORS[health] + ';')
        # CPU observations can explain activity or pressure, but cannot replace
        # the current operation's reported percentage inside the ring.
        activity_detail = str(metrics.get('reason') or metrics.get('scope') or
            'Worker-thread activity excludes work performed by Windows Update or vendor installer processes.')
        self.activity_state.setToolTip(label + '. ' + activity_detail[:1500])

    def refresh_elapsed(self):
        if self._elapsed_base is not None:
            elapsed = self._elapsed_base + (self._elapsed_clock.elapsed() / 1000 if self._elapsed_active else 0)
            minutes, seconds = divmod(int(elapsed), 60)
            hours, minutes = divmod(minutes, 60)
            text = (f'{hours}h {minutes:02d}m {seconds:02d}s' if hours else
                    f'{minutes}m {seconds:02d}s' if minutes else f'{seconds}s')
            self.elapsed.setText('Elapsed: ' + text)
        else:
            self.elapsed.setText('Elapsed: —')


class OptimizationProgressWindow(QDialog):
    resultsAcknowledged = Signal()
    resultsDismissed = Signal()
    autoContinueRequested = Signal()

    def __init__(self, owner, cancel):
        super().__init__(owner)
        self.owner = owner
        # Cancellation/dismissal must run immediately, before a queued saved
        # continuation can start; this window keeps its own close guard.
        self._optimus_reveal_open_only = True
        self.cancel = cancel
        self.completed = False
        self._waiting_for_updates = False
        self._title_before_update_wait = None
        self._waiting_update_kind = None
        self._worker_selection = {}
        self._foreground_update_clocks = {}
        self._logged_update_reasons = set()
        self._maintenance_clock = QElapsedTimer()
        self._maintenance_clock.start()
        self._recommendations_ready = False
        self._results_dismissed = False
        self.setWindowTitle('Optimus - optimization progress')
        self.setWindowModality(Qt.WindowModal)
        self.resize(850, 650)
        outer = QVBoxLayout(self)
        self._outer_layout = outer
        outer.setContentsMargins(7, 7, 7, 7)
        outer.setSpacing(3)
        self.content_scroll = QScrollArea(self)
        self.content_scroll.setWidgetResizable(True)
        self.content_scroll.setFrameShape(QFrame.NoFrame)
        self.content_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content = QWidget()
        content.setMinimumWidth(0)
        content.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        self.content_scroll.setWidget(content)
        self.content_scroll.setMinimumHeight(0)
        header = QWidget(self)
        self._progress_header = header
        header_layout = QGridLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setVerticalSpacing(3)
        header_layout.setColumnStretch(0, 1)
        self.title = WorkflowSummaryLabel('OPTIMIZATION IN PROGRESS', max_lines=1)
        self.title.setObjectName('PageTitle')
        self.title.setWordWrap(True)
        self.title.setMinimumWidth(0)
        self.title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        header_layout.addWidget(self.title, 0, 0)
        self.spinner = RunSpinner(self)
        old_spinner_label = self.spinner._label
        self.spinner._label = WorkflowSummaryLabel(max_lines=1)
        self.spinner.layout().replaceWidget(old_spinner_label, self.spinner._label)
        old_spinner_label.hide()
        old_spinner_label.deleteLater()
        self.spinner._label.setWordWrap(False)
        self.spinner._label.setMinimumWidth(0)
        self.spinner._label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.spinner.start('Preparing')
        header_layout.addWidget(self.spinner, 1, 0)
        self.loading_animation = LocalLoadingAnimation(self, extent=128)
        self.progress = WorkflowProgressBar()
        self.progress.setObjectName('WorkflowProgress')
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat('%p%')
        header_layout.addWidget(self.progress, 2, 0)
        from .widgets.resource_comparison import ResourceComparison
        self.resource_comparison = ResourceComparison(self)
        self.resource_comparison.layout().setVerticalSpacing(2)
        # Preserve the comparison values, colours and explanation while fitting
        # one bounded line per label. Complete text remains available on hover.
        for name in ('removed', 'downloaded', 'note'):
            previous = getattr(self.resource_comparison, name)
            bounded = WorkflowSummaryLabel(previous.text(), max_lines=1)
            bounded.setStyleSheet(previous.styleSheet())
            bounded.setAlignment(previous.alignment())
            self.resource_comparison.layout().replaceWidget(previous, bounded)
            setattr(self.resource_comparison, name, bounded)
            previous.hide()
            previous.deleteLater()
        header_layout.addWidget(self.resource_comparison, 3, 0)
        self.stage = QLabel('Waiting for workflow preparation')
        self.stage.setObjectName('SectionTitle')
        self.stage.setWordWrap(True)
        self.stage.setMinimumWidth(0)
        self.stage.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout.addWidget(self.stage)
        self.description = WorkflowSummaryLabel('Progress reflects completed workflow stages. No estimated completion is fabricated.')
        layout.addWidget(self.description)
        layout.addWidget(self.loading_animation, 0, Qt.AlignRight)
        self.guard_summary = WorkflowSummaryLabel(max_lines=1)
        self.guard_summary.setStyleSheet('color:#e8b34b;')
        self.guard_summary.hide()
        header_layout.addWidget(self.guard_summary, 4, 0)
        outer.addWidget(header)
        from .widgets.live_activity import LiveSystemStatus
        self.live_system_status = LiveSystemStatus(self)
        layout.addWidget(self.live_system_status)
        self.space_guard_panel = QWidget()
        guard_row = QHBoxLayout(self.space_guard_panel)
        guard_row.setContentsMargins(0, 2, 0, 2)
        guard_row.setSpacing(7)
        self.space_guard_icon = QLabel()
        self.space_guard_icon.setFixedSize(24, 24)
        self.space_guard_icon.setPixmap(self.style().standardIcon(QStyle.SP_MessageBoxWarning).pixmap(22, 22))
        self.space_guard_icon.setAccessibleName('Drive-space warning')
        guard_row.addWidget(self.space_guard_icon, 0, Qt.AlignTop)
        self.space_guard_status = QLabel()
        self.space_guard_status.setObjectName('DriveSpaceGuardStatus')
        self.space_guard_status.setTextFormat(Qt.PlainText)
        self.space_guard_status.setWordWrap(True)
        self.space_guard_status.setMinimumWidth(0)
        self.space_guard_status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.space_guard_status.hide()
        guard_row.addWidget(self.space_guard_status, 1)
        self.space_guard_panel.hide()
        layout.addWidget(self.space_guard_panel)
        self._live_metrics_service = None
        if getattr(owner, 'smoke_test', True) is False:
            from .live_metrics import LiveMetrics
            self._live_metrics_service = LiveMetrics(getattr(owner, 'backend', None))
        from .widgets.worker_grid import WorkerGrid
        self.extra_worker_grid = WorkerGrid(WorkflowWorkerCard, content)
        self.extra_worker_grid.hide()
        layout.addWidget(self.extra_worker_grid)
        self.worker_grid = WorkerGrid(WorkflowWorkerCard, self, pinned=True, extras=self.extra_worker_grid)
        self.worker_cards = self.worker_grid.cards
        for identity, title in (('maintenance', 'Maintenance'), ('software_updates', 'App updates'),
                                ('driver_updates', 'Driver updates'), ('security_scan', 'Virus & malware scan')):
            self.worker_grid.ensure(identity, title)
        self.worker_cards['security_scan'].update_state({'status': 'not_selected',
            'description': 'The virus, malware and spyware scan is disabled for this run.'})
        outer.addWidget(self.worker_grid, 1)
        self.worker_details = QPlainTextEdit()
        self.worker_details.setReadOnly(True)
        self.worker_details.setMinimumHeight(100)
        self.worker_details.setMaximumHeight(280)
        layout.addWidget(self.worker_details)
        self.background_status = WorkflowSummaryLabel()
        self.background_status.setObjectName('BackgroundStatus')
        self.background_status.hide()
        layout.addWidget(self.background_status)
        self.insights_summary = QLabel()
        self.insights_summary.setObjectName('SectionTitle')
        self.insights_summary.setTextFormat(Qt.PlainText)
        self.insights_summary.setWordWrap(True)
        self.insights_summary.hide()
        layout.addWidget(self.insights_summary)
        self.verification_summary = QLabel()
        self.verification_summary.setWordWrap(True)
        self.verification_summary.hide()
        layout.addWidget(self.verification_summary)
        self.verification_details = QPlainTextEdit()
        self.verification_details.setReadOnly(True)
        self.verification_details.setMaximumHeight(160)
        self.verification_details.hide()
        layout.addWidget(self.verification_details)
        self.processes_label = QLabel('CURRENT PROCESSES / ITEMS')
        layout.addWidget(self.processes_label)
        self.processes = QPlainTextEdit()
        self.processes.setReadOnly(True)
        self.processes.setMaximumHeight(130)
        layout.addWidget(self.processes)
        layout.addWidget(QLabel('STAGE LOG'))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        layout.addWidget(self.log, 1)
        self.auto_continue_summary = QLabel()
        self.auto_continue_summary.setTextFormat(Qt.PlainText)
        self.auto_continue_summary.setWordWrap(True)
        layout.addWidget(self.auto_continue_summary)
        self.auto_continue_details = QPlainTextEdit()
        self.auto_continue_details.setReadOnly(True)
        self.auto_continue_details.setMaximumHeight(140)
        layout.addWidget(self.auto_continue_details)
        self.details_toggle = QPushButton('Show details and logs')
        self.details_toggle.setCheckable(True)
        self.details_toggle.setAutoDefault(False)
        self.details_toggle.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.details_toggle.toggled.connect(self._toggle_details)
        outer.addWidget(self.details_toggle)
        outer.addWidget(self.content_scroll)
        self.content_scroll.hide()
        self.auto_continue_button = QPushButton('Auto continue — apply available fixes')
        self.auto_continue_button.setAutoDefault(False)
        self.auto_continue_button.setDefault(False)
        self.auto_continue_button.setObjectName('Primary')
        self.auto_continue_button.setMinimumHeight(36)
        self.auto_continue_button.setMinimumWidth(0)
        self.auto_continue_button.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.auto_continue_button.clicked.connect(self.autoContinueRequested.emit)
        outer.addWidget(self.auto_continue_button)
        self.clear_auto_continue()
        self.cancel_button = QPushButton('Cancel after current stage')
        self.cancel_button.setMinimumHeight(36)
        self.cancel_button.setMinimumWidth(0)
        self.cancel_button.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.cancel_button.clicked.connect(self.request_close)
        outer.addWidget(self.cancel_button)
        self._worker_elapsed_timer = QTimer(self)
        self._worker_elapsed_timer.setInterval(1000)
        self._worker_elapsed_timer.timeout.connect(self._refresh_worker_elapsed)
        from .widgets.worker_navigation import WorkerNavigationController
        self.worker_navigation = WorkerNavigationController(getattr(owner, 'backend', None), self.worker_cards, self)
        self.worker_navigation.stateChanged.connect(self._refresh_worker_details)
        self.worker_navigation.refresh(enabled=True)
        self._responsive_timer = QTimer(self)
        self._responsive_timer.setSingleShot(True)
        self._responsive_timer.timeout.connect(self._update_responsive_layout)
        self.worker_grid._reflow_timer.timeout.connect(self._update_responsive_layout)
        self.loading_animation.start()
        self._update_responsive_layout()

    def _toggle_details(self, visible):
        self.content_scroll.setVisible(visible)
        self.content_scroll.setMinimumHeight(88 if visible else 0)
        self.content_scroll.setMaximumHeight(180 if visible else 0)
        self.details_toggle.setText('Hide details and logs' if visible else 'Show details and logs')
        self._refresh_worker_details()
        self._update_responsive_layout()

    def _refresh_worker_details(self):
        parts = [self.stage.text(), self.description.text()]
        for card in self.worker_cards.values():
            parts.append('\n'.join(label.text() for label in (card.heading, card.status, card.completion,
                card.details, card.totals, card.elapsed, card.activity_state, card.navigation_note) if label.text()))
        text = '\n\n'.join(part for part in parts if part)
        if text != self.worker_details.toPlainText():
            position = self.worker_details.verticalScrollBar().value()
            self.worker_details.setPlainText(text)
            self.worker_details.verticalScrollBar().setValue(position)

    def _update_responsive_layout(self):
        if not hasattr(self, 'details_toggle') or getattr(self, '_fitting_workers', False):
            return
        self._fitting_workers = True
        try:
            self.worker_grid.reflow()
            self._progress_header.layout().activate()
            self._outer_layout.activate()
            self._worker_minimum = QSize(max(480, self.worker_grid.minimumWidth() + 14),
                max(450, self._outer_layout.minimumSize().height()))
            if self.minimumSize() != self._worker_minimum:
                super().setMinimumSize(self._worker_minimum)
            self._refresh_worker_details()
        finally:
            self._fitting_workers = False

    def setMinimumSize(self, *args):
        # The general screen-fit coordinator may lower desktop minima. This
        # pinned control surface must retain its measured usable bound instead
        # of allowing an OS resize to obscure live worker controls.
        requested = QSize(*args)
        super().setMinimumSize(requested.expandedTo(getattr(self, '_worker_minimum', QSize())))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, '_responsive_timer') and not self._responsive_timer.isActive():
            self._responsive_timer.start(0)

    def event(self, event):
        result = super().event(event)
        if (event.type() == QEvent.LayoutRequest and hasattr(self, '_responsive_timer')
                and not self._responsive_timer.isActive() and not getattr(self, '_fitting_workers', False)):
            self._responsive_timer.start(0)
        return result

    def _refresh_worker_elapsed(self):
        status = getattr(getattr(self.owner, 'backend', None), 'worker_status', None)
        if callable(status):
            self.worker_grid.update_jobs(status())
        for card in self.worker_cards.values():
            card.refresh_elapsed()
        self._refresh_resource_comparison()
        self.worker_navigation.refresh(enabled=not self.completed and not self.cancel.is_set())
        self._update_live_metrics()
        self._refresh_worker_details()

    def _refresh_resource_comparison(self):
        # Smart Maintenance's last check is a different scope from this run.
        self.resource_comparison.update_totals({identity: card._resource_totals
            for identity, card in self.worker_cards.items()
            if identity in ('maintenance', 'software_updates', 'driver_updates')
            and card._resource_scope != 'last_check'})

    def _update_space_guard(self, payload):
        if not isinstance(payload, dict) or type(payload.get('enabled')) is not bool:
            return
        if payload['enabled'] is False:
            self.space_guard_status.clear()
            self.space_guard_status.hide()
            self.space_guard_panel.hide()
            self.guard_summary.hide()
            return
        def byte_text(value):
            return f'{value / 1073741824:.2f} GiB' if type(value) is int and 0 <= value <= 2 ** 63 - 1 else 'unknown'
        volumes = payload.get('volumes')
        volumes = volumes if isinstance(volumes, list) else []
        warnings = payload.get('warnings')
        attention = (isinstance(warnings, list) and any(isinstance(text, str) and text.strip() for text in warnings)
            or any(isinstance(row, dict) and row.get('below_target') is True for row in volumes))
        lines = ['WARNING: Drive space below target' if attention else
            'Drive-space check needs review' if payload.get('complete') is not True else 'Drive-space check']
        for row in volumes[:64]:
            if not isinstance(row, dict):
                continue
            name = ' '.join(str(row.get('name') or row.get('mountpoint') or 'Unidentified drive').split())[:160]
            target = row.get('target_bytes', payload.get('target_bytes'))
            lines.append(name + (' ' if name.endswith(':') else ': ') + 'free ' + byte_text(row.get('free_bytes')) + ' · target ' + byte_text(target)
                + ' · shortfall ' + byte_text(row.get('shortfall_bytes')))
        if len(volumes) > 64:
            lines.append(f'{len(volumes) - 64} additional volume results are retained in the report.')
        if not volumes:
            lines.append('Free-space target: ' + byte_text(payload.get('target_bytes'))
                + '; no verified volume measurement is available.')
        if payload.get('complete') is not True or not volumes:
            summary = payload.get('summary')
            lines.append(str(summary)[:1000] if isinstance(summary, str) and summary.strip()
                else 'Free-space verification is incomplete; unknown volumes are not confirmed healthy.')
        text = '\n'.join(lines)
        self.space_guard_status.setText(text)
        self.guard_summary.set_summary(' · '.join(lines[:2]), text)
        self.guard_summary.setVisible(bool(attention or payload.get('complete') is not True))
        self.space_guard_status.setToolTip(text + '\nRead-only observation; this does not reserve disk space or authorize extra deletion.')
        self.space_guard_status.setAccessibleDescription(self.space_guard_status.toolTip())
        self.space_guard_status.setStyleSheet('color:#e8b34b;' if attention or payload.get('complete') is not True else '')
        self.space_guard_icon.setVisible(bool(attention or payload.get('complete') is not True))
        self.space_guard_status.show()
        self.space_guard_panel.show()

    def _update_live_metrics(self):
        service = self._live_metrics_service
        payload = service.status() if service is not None else {}
        self.live_system_status.update_metrics(payload)
        workers = payload.get('workers', {}) if isinstance(payload, dict) else {}
        computer = payload.get('computer', {}) if isinstance(payload, dict) else {}
        for identity, card in self.worker_cards.items():
            card.set_live_metrics(workers.get(identity), computer)

    def _sync_live_metrics(self, active):
        service = getattr(self, '_live_metrics_service', None)
        if service is not None:
            if active and not self.completed:
                service.start()
            else:
                service.stop(wait=False)
        for card in getattr(self, 'worker_cards', {}).values():
            card.activity_ring._sync()

    def showEvent(self, event):
        super().showEvent(event)
        if not self.completed:
            self._sync_live_metrics(True)
            self._refresh_worker_elapsed()
            self._worker_elapsed_timer.start()

    def hideEvent(self, event):
        self._worker_elapsed_timer.stop()
        self._sync_live_metrics(False)
        super().hideEvent(event)

    def changeEvent(self, event):
        super().changeEvent(event)
        if hasattr(self, '_worker_elapsed_timer'):
            if self.isMinimized() or not self.isVisible() or self.completed:
                self._worker_elapsed_timer.stop()
                self._sync_live_metrics(False)
            else:
                self._worker_elapsed_timer.start()
                self._sync_live_metrics(True)

    def configure_workers(self, settings):
        self._worker_selection = dict(settings) if isinstance(settings, dict) else {}
        for card in self.worker_cards.values():
            card.reset_resource_totals()
        self._refresh_resource_comparison()
        for identity, key in (('software_updates', 'software_updates_enabled'),
                              ('driver_updates', 'driver_updates_enabled')):
            selected = self._worker_selection.get(key) is not False
            self.worker_cards[identity].update_state({'status': 'not_started' if selected else 'not_selected',
                'description': ('Waiting for the saved update scope to be checked.' if selected else
                                'This update module is disabled for this run.')})
        if self._worker_selection.get('spyware_scan_enabled') is True or 'security_scan' in self.worker_cards:
            selected = self._worker_selection.get('spyware_scan_enabled') is True
            self._update_scan_card({'stage_status': 'not_started' if selected else 'not_selected',
                'description': 'Waiting for the saved virus, malware and spyware scan.' if selected else
                    'The virus, malware and spyware scan is disabled for this run.'})

    def _update_scan_card(self, state):
        card = self.worker_grid.ensure('security_scan', 'Virus & malware scan')
        clock = self._foreground_update_clocks.get('security_scan')
        status = state.get('scan_status') or state.get('stage_status') or 'scanning'
        if status not in ('not_started', 'not_selected') and clock is None:
            clock = self._foreground_update_clocks['security_scan'] = QElapsedTimer()
            clock.start()
        verified = state.get('completion_verified') is True
        count = state.get('active_threat_count')
        if status == 'completed' and not verified:
            status = 'needs_review'
        elif status == 'completed' and (type(count) is not int or count > 0):
            status = 'needs_review'
        elif status in ('blocked', 'incomplete'):
            status = 'needs_review'
        elif status == 'preparing':
            status = 'running'
        elif status in ('provider_scan_progress', 'waiting_for_defender'):
            status = 'scanning'
        item = state.get('item_progress')
        from .item_progress import normalize_item_progress
        item = normalize_item_progress(item)
        if item is not None and item['key'] != 'security:defender:quick_scan':
            item = None
        if verified:
            item = dict(key='security:defender:quick_scan', label='Virus, malware and spyware scan',
                phase='scan', percent=100)
        percentage = item.get('percent') if item is not None else None
        elapsed = state.get('scan_elapsed_seconds', state.get('elapsed_seconds'))
        if type(elapsed) not in (int, float):
            elapsed = clock.elapsed() / 1000 if clock is not None else None
        description = display(state.get('description') or state.get('reason'))
        if verified and type(count) is int and count > 0:
            description += ' Active threats found; review removal before Optimus requests remediation.'
        elif verified and type(count) is not int:
            description += ' Scan completed; threat results need review.'
        card.update_state({'status': status, 'description': description,
            'operation_kind': 'antivirus_scan', 'completion_verified': verified,
            'percentage': percentage, 'item_progress': item, 'elapsed_seconds': elapsed})
        threat_text = f'Active threats: {count}' if type(count) is int and 0 <= count <= 1000000 else 'Threat results: not yet verified'
        if verified and type(count) is not int:
            threat_text = 'Scan completed; review threat results'
        card.totals.set_summary(threat_text + '\nQuarantine: Defender policy',
            threat_text + '\nDefender controls automatic protection and quarantine. Optimus requests removal only after explicit approval.')

    @staticmethod
    def _update_stage_worker(stage):
        return {'Review or update configured software': 'software_updates',
                'Review or install approved native package updates': 'software_updates',
                'Recheck remaining approved application updates': 'software_updates',
                'Install approved driver updates': 'driver_updates',
                'Recheck remaining approved Windows Update drivers': 'driver_updates'}.get(stage)

    @Slot(object)
    def update_progress(self, state):
        self._update_space_guard(state.get('space_guard') if isinstance(state, dict) else None)
        if not isinstance(state, dict):
            self.log.appendPlainText(display(state))
            return
        stage = display(field(state, 'stage', default='Working'))
        if stage == 'Scan for spyware and malware':
            self._update_scan_card(state)
        waiting_kind = state.get('waiting_for')
        if waiting_kind not in ('software_updates', 'driver_updates', 'installer_updates'):
            waiting_kind = {'Waiting for background application updates': 'software_updates',
                            'Waiting for background driver updates': 'driver_updates',
                            'Waiting for background updates': 'installer_updates'}.get(stage)
        waiting_for_updates = waiting_kind is not None
        if waiting_for_updates and not self._waiting_for_updates:
            self._title_before_update_wait = self.title.text()
        elif not waiting_for_updates and self._waiting_for_updates:
            self.title.setText(self._title_before_update_wait or 'OPTIMIZATION IN PROGRESS')
            self._title_before_update_wait = None
            if self.cancel_button.isEnabled():
                self.cancel_button.setText('Cancel after current stage')
        self._waiting_for_updates = waiting_for_updates
        self._waiting_update_kind = waiting_kind
        if waiting_for_updates:
            self.title.setText({'software_updates': 'WAITING FOR APPLICATION UPDATES',
                                'driver_updates': 'WAITING FOR DRIVER UPDATES',
                                'installer_updates': 'WAITING FOR BACKGROUND UPDATES'}[waiting_kind])
            if self.cancel_button.isEnabled():
                self.cancel_button.setText('Cancel after current update')
        percentage = field(state, 'percentage', default=self.progress.value() if waiting_for_updates else 0)
        percentage = max(0, min(99 if waiting_for_updates else 100, float(percentage or 0)))
        self.progress.setValue(int(percentage))
        self.spinner.set_pct(percentage)
        visible_stage = 'Scan for viruses, malware and spyware' if stage == 'Scan for spyware and malware' else stage
        self.stage.setText(visible_stage)
        self.spinner.set_text(visible_stage)
        description = display(field(state, 'description'))
        if waiting_for_updates and not description:
            description = ('Independent work has finished. Waiting for background updates before '
                           'conflicting maintenance and final verification. You can cancel after the current update.')
        if waiting_for_updates and self.cancel.is_set():
            description = self._update_cancellation_message()
        self.description.setText(description)
        self.processes.setPlainText(display(field(state, 'processes', default=[])))
        maintenance_phase = state.get('maintenance_status')
        if not isinstance(maintenance_phase, str) or not maintenance_phase.strip():
            maintenance_phase = ('waiting' if waiting_for_updates else
                'failed' if state.get('stage_status') == 'failed' else 'running')
        maintenance_details = display(state.get('maintenance_wait_reason'))
        if not maintenance_details:
            maintenance_details = (description if maintenance_phase.startswith('waiting_for_') or state.get('item_progress') is not None else
                'Ready for: ' + display(state['next_stage']) if waiting_for_updates and state.get('next_stage') else stage)
        self.worker_cards['maintenance'].update_state({'status': maintenance_phase,
            'description': maintenance_details,
            'waiting_for': waiting_kind,
            'percentage': percentage, 'item_progress': state.get('item_progress'),
            'resource_totals': state.get('resource_totals'),
            'elapsed_seconds': self._maintenance_clock.elapsed() / 1000}, maintenance=True)
        jobs = state.get('jobs')
        job_ids = {job.get('id') for job in jobs if isinstance(job, dict)} if isinstance(jobs, list) else set()
        sequential_worker = self._update_stage_worker(stage)
        if sequential_worker and sequential_worker not in job_ids:
            status = state.get('stage_status')
            if not status:
                marker = display(state.get('log')).partition(':')[0].casefold()
                status = marker if marker in ('completed', 'failed', 'cancelled') else 'running'
            clock = self._foreground_update_clocks.get(sequential_worker)
            if clock is None:
                clock = self._foreground_update_clocks[sequential_worker] = QElapsedTimer()
                clock.start()
            self.worker_cards[sequential_worker].update_state({'status': status, 'description': description or stage,
                'item_progress': state.get('item_progress'),
                'elapsed_seconds': clock.elapsed() / 1000})
        if isinstance(jobs, list):
            descriptions = []
            compact_descriptions = []
            reason_groups = {}
            def job_status_text(job):
                item = job.get('item_progress')
                if job.get('status') in ('running', 'installing', 'uninstalling') and isinstance(item, dict):
                    item_phase = item.get('phase')
                    if item_phase in ('download', 'downloading'):
                        return 'downloading'
                    if item_phase in ('install', 'installing'):
                        return 'installing'
                    if item_phase in ('uninstall', 'uninstalling'):
                        return 'uninstalling'
                if (job.get('presentation_status') == 'finished_with_skips' and job.get('worker_finished') is True
                        and job.get('continuation_blocked') is False):
                    return 'finished with skips' + ('; scan incomplete' if job.get('scan_complete') is not True else '')
                return str(job.get('status', '')).replace('_', ' ')
            blocked_statuses = {'deferred', 'needs_review', 'failed', 'waiting_for_maintenance',
                                'waiting_for_installer', 'waiting_for_preparation'}
            for job in jobs:
                if isinstance(job, dict) and job.get('status') in blocked_statuses:
                    reason = display(job.get('description'))
                    extra = job.get('wait_reason')
                    if isinstance(extra, str) and extra.strip() and extra not in reason:
                        reason += ' — ' + extra
                    block_reason = job.get('installation_block_reason')
                    if isinstance(block_reason, str) and block_reason.strip() and block_reason not in reason:
                        reason += ' — ' + block_reason
                    if reason:
                        reason_groups.setdefault(reason, []).append(job)
            seen_reasons = set()
            for job in jobs:
                if not isinstance(job, dict):
                    continue
                card = self.worker_grid.ensure(job.get('id'), job.get('title') or str(job.get('id', '')).replace('_', ' ').title())
                if card is not None:
                    card.update_state(job)
                reason = display(job.get('description'))
                extra = job.get('wait_reason')
                if isinstance(extra, str) and extra.strip() and extra not in reason:
                    reason += ' — ' + extra
                block_reason = job.get('installation_block_reason')
                if isinstance(block_reason, str) and block_reason.strip() and block_reason not in reason:
                    reason += ' — ' + block_reason
                if job.get('status') in blocked_statuses and reason:
                    if reason in seen_reasons:
                        continue
                    seen_reasons.add(reason)
                    grouped = reason_groups[reason]
                    names = '; '.join(({'software_updates': 'App updates', 'driver_updates': 'Driver updates'}
                        .get(item.get('id'), display(item.get('id')))) + ': '
                        + job_status_text(item) for item in grouped)
                    descriptions.append(names + ' — ' + reason)
                    compact_descriptions.append(names + ' — ' + _compact_worker_reason(reason))
                    if len(reason) > 150 and reason not in self._logged_update_reasons:
                        self._logged_update_reasons.add(reason)
                        self.log.appendPlainText('UPDATE DETAILS — ' + names + '\n' + reason)
                    continue
                text = display(job.get('description') or str(job.get('id', '')).replace('_', ' '))
                package = job.get('current_package')
                if isinstance(package, str) and package.strip() and package not in text:
                    text += ' — current package: ' + package
                if job.get('status'):
                    text += ' — ' + job_status_text(job)
                if isinstance(job.get('total'), (int, float)) and job['total'] > 0:
                    if waiting_for_updates and job.get('id') in ('software_updates', 'driver_updates'):
                        unit = 'packages' if job['id'] == 'software_updates' else 'driver updates'
                        text += f" — {job.get('current', 0)}/{job['total']} {unit} processed"
                    else:
                        text += f" ({job.get('current', 0)}/{job['total']})"
                elapsed = job.get('elapsed_seconds')
                if (isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool)
                        and math.isfinite(elapsed) and 0 <= elapsed < 3155760000):
                    seconds = int(elapsed)
                    minutes, seconds = divmod(seconds, 60)
                    hours, minutes = divmod(minutes, 60)
                    elapsed_text = (f'{hours}h {minutes:02d}m {seconds:02d}s' if hours else
                                    f'{minutes}m {seconds:02d}s' if minutes else f'{seconds}s')
                    text += ' — elapsed ' + elapsed_text
                descriptions.append(text)
                compact_descriptions.append(text)
            self.background_status.set_summary('Background: ' + '; '.join(compact_descriptions),
                                               'Background: ' + '; '.join(descriptions))
            self.background_status.setVisible(bool(descriptions))
        self._refresh_resource_comparison()
        log = field(state, 'log', default='')
        if log:
            self.log.appendPlainText(display(log))
        self.worker_navigation.refresh(enabled=not self.completed and not self.cancel.is_set())
        self._refresh_worker_details()

    def _update_cancellation_message(self):
        kind = ('application' if self._waiting_update_kind == 'software_updates' else
                'driver' if self._waiting_update_kind == 'driver_updates' else 'background')
        return f'Cancellation requested. The current {kind} update must return before the workflow stops.'

    def finish(self, result, error=None):
        self._update_space_guard(field(result, 'space_guard', default=None))
        self.completed = True
        self._sync_live_metrics(False)
        self.worker_navigation.refresh(enabled=False)
        self._worker_elapsed_timer.stop()
        self.loading_animation.stop()
        self._waiting_for_updates = False
        self._title_before_update_wait = None
        self._waiting_update_kind = None
        self.spinner.stop()
        self.title.setText('OPTIMIZATION RESULTS')
        self.processes_label.hide()
        self.processes.hide()
        self.background_status.hide()
        self.clear_auto_continue()
        self.insights_summary.clear()
        self.insights_summary.hide()
        if error:
            self.stage.setText('WORKFLOW STOPPED')
            self.description.setText(str(error))
            self.log.appendPlainText('Error: ' + str(error))
        else:
            cancelled = bool(field(result, 'cancelled', 'canceled', default=False))
            if field(result, 'status') in ('completed', 'completed_with_errors') and not cancelled:
                self.progress.setValue(100)
            self.stage.setText('CANCELLED AT STAGE BOUNDARY' if cancelled else 'WORKFLOW RETURNED RESULTS')
            verification = field(result, 'final_verification', default=None)
            if isinstance(verification, dict):
                labels = {'confirmed': 'Selected checks confirmed', 'needs_review': 'Needs review',
                          'unverified': 'Not fully verified', 'not_applicable': 'Review or no-change checks only'}
                counts = verification.get('counts') if isinstance(verification.get('counts'), dict) else {}
                summary = 'Final verification: ' + labels.get(verification.get('status'), 'Unavailable; outcomes unverified')
                summary += '. ' + '; '.join(str(counts.get(key, 0)) + ' ' + label for key, label in (
                    ('confirmed', 'confirmed'), ('needs_review', 'need review'), ('unverified', 'unverified'), ('not_applicable', 'review/no change'))) + '.'
                self.verification_summary.setText(summary)
                self.verification_details.setPlainText(_report_preview(verification))
            else:
                self.verification_summary.setText('Final verification was not run because the workflow was cancelled.' if cancelled else 'Final verification is unavailable; optimization outcomes have not been confirmed.')
                self.verification_details.setPlainText('No final verification report was produced. Review the recorded stage outcomes below.')
            insights = field(result, 'maintenance_insights', default=None)
            if isinstance(insights, dict) and insights.get('read_only') is True:
                observed_summary = insights.get('summary')
                if isinstance(observed_summary, str) and observed_summary.strip():
                    self.insights_summary.setText('Observed resource and startup changes: ' + observed_summary)
                    self.insights_summary.show()
                self.verification_details.appendPlainText('\nREAD-ONLY MAINTENANCE OBSERVATIONS\n' + _report_preview(insights))
            self.verification_summary.show()
            self.verification_details.show()
            self.description.setText('Review the final verification and any skipped or failed stages. Recommendations open only after you continue; closing this window skips those prompts.')
            self.log.appendPlainText(_report_preview(result))
        self.worker_cards['maintenance'].update_state({'status': 'stopped' if error else
            'cancelled' if field(result, 'cancelled', 'canceled', default=False) else field(result, 'status', default='completed'),
            'description': self.stage.text(), 'percentage': self.progress.value(),
            'resource_totals': field(result, 'resource_totals', default=None),
            'elapsed_seconds': self._maintenance_clock.elapsed() / 1000}, maintenance=True)
        # Sequential mode and supported follow-ups report to these same panels;
        # a stage returning is distinct from every package being fixed.
        for item in rows(field(result, 'stages', default=[]))[:256]:
            if not isinstance(item, dict):
                continue
            if display(field(item, 'title', 'name')) == 'Scan for spyware and malware':
                outcome = item.get('result') if isinstance(item.get('result'), dict) else {}
                self._update_scan_card(dict(outcome, scan_status=outcome.get('status', item.get('status')),
                    description=outcome.get('note') or outcome.get('reason') or item.get('error') or
                        'Scan returned; review its recorded result.'))
            worker = self._update_stage_worker(display(field(item, 'title', 'name')))
            if worker:
                outcome = item.get('result') if isinstance(item.get('result'), dict) else {}
                status = 'deferred' if outcome.get('skipped') else item.get('status', 'needs_review')
                clock = self._foreground_update_clocks.get(worker)
                details = display(outcome.get('stop_reason') or item.get('error') or outcome.get('note')
                                  or (display(field(item, 'title', 'name')) + ': ' + status))
                if clock is not None or self.worker_cards[worker].status.text() in ('Not started', 'Not selected'):
                    from .parallel_updates import update_completion_state, update_completion_status
                    completion = update_completion_state(outcome)
                    evidence = outcome.get('driver_scan') or outcome.get('app_scan')
                    if isinstance(evidence, dict):
                        details = completion['description']
                        status = update_completion_status(outcome)
                    self.worker_cards[worker].update_state(dict(completion, status=status, description=details,
                        elapsed_seconds=clock.elapsed() / 1000 if clock is not None else None))
        for identity in ('software_updates', 'driver_updates'):
            card = self.worker_cards[identity]
            if card.status.text() == 'Not started':
                card.update_state({'status': 'not_run', 'description': 'No update operation was reported for this worker in this run.'})
            elif error and card._elapsed_active:
                card.update_state({'status': 'needs_review',
                    'description': 'The run stopped before a final updater status was reported. Review the saved outcome before retrying.'})
        worker_totals = field(result, 'worker_resource_totals', default={})
        if isinstance(worker_totals, dict):
            for identity, totals in worker_totals.items():
                card = self.worker_cards.get(identity)
                if card is not None and isinstance(totals, dict):
                    card._set_resource_totals(totals, maintenance=identity == 'maintenance')
        self._refresh_resource_comparison()
        self.cancel_button.setText('Close results')
        self.cancel_button.setEnabled(True)
        self._update_live_metrics()

    def clear_auto_continue(self):
        self.auto_continue_button.setEnabled(False)
        self.auto_continue_button.hide()
        self.auto_continue_summary.hide()
        self.auto_continue_details.hide()

    def set_auto_continue_plan(self, plan, *, ready=False):
        actions = rows(field(plan, 'actions', default=[]))
        manual = rows(field(plan, 'manual_items', default=[]))
        self.auto_continue_summary.setText(
            f'{len(actions)} automatic {"action" if len(actions) == 1 else "actions"} available; '
            f'{len(manual)} {"item requires" if len(manual) == 1 else "items require"} manual review. '
            + (' Clicking Auto continue approves the automatic actions listed below.' if actions
               else 'No automatic fixes are available for this result. ' + display(field(plan, 'summary'))))
        details = ['AUTOMATIC ACTIONS']
        details.extend('• ' + display(field(item, 'title')) for item in actions)
        if not actions:
            details.append('None available.')
        details.append('\nMANUAL REVIEW / REMAINING ITEMS')
        details.extend('• ' + display(field(item, 'title')) + ': ' + display(field(item, 'reason'))
                       for item in manual)
        if not manual:
            details.append('None listed.')
        self.auto_continue_details.setPlainText('\n'.join(details))
        self.auto_continue_summary.show()
        self.auto_continue_details.show()
        self.auto_continue_button.show()
        self.auto_continue_button.setEnabled(bool(actions) and ready and self.completed)
        self.auto_continue_button.setToolTip('Apply the listed automatic actions once.' if actions
            else 'No automatic fixes are available; use Continue for any remaining recommendations.')

    def begin_auto_continue(self, cancel, *, automatic=False):
        self.cancel = cancel
        self.completed = False
        self._waiting_for_updates = False
        self._title_before_update_wait = None
        self._waiting_update_kind = None
        self._maintenance_clock.restart()
        self._foreground_update_clocks.clear()
        if self.isVisible():
            self._worker_elapsed_timer.start()
            self._sync_live_metrics(True)
        self.configure_workers(self._worker_selection)
        self.worker_cards['maintenance'].update_state({'status': 'pending',
            'description': 'Preparing the approved follow-up checks and fixes.', 'percentage': 0}, maintenance=True)
        self._results_dismissed = False
        self.set_recommendations_available(False)
        self.clear_auto_continue()
        self.title.setText('APPLYING AVAILABLE FIXES')
        self.loading_animation.start()
        self.progress.setValue(0)
        self.progress.setFormat('Follow-up %p%')
        self.spinner.start('Preparing available fixes')
        self.stage.setText('Preparing available fixes')
        self.description.setText('Applying the automatic actions you approved. Progress is recorded by stage.')
        self.insights_summary.hide()
        self.verification_summary.hide()
        self.verification_details.hide()
        self.processes_label.show()
        self.processes.show()
        self.processes.clear()
        self.log.appendPlainText('\nAUTO CONTINUE — ' + ('SAVED AUTOMATIC FIX CHOICE' if automatic else 'USER APPROVED AVAILABLE FIXES'))
        self.cancel_button.setText('Cancel after current stage')
        self.cancel_button.setEnabled(True)

    def finish_auto_continue(self, result, error=None):
        self.finish(result, error=error)
        self.title.setText('AVAILABLE FIXES RESULTS')
        if not error:
            counts = field(result, 'counts', default={})
            outcomes = '; '.join(str(value) + ' ' + str(key).replace('_', ' ')
                for key, value in counts.items()) if isinstance(counts, dict) else display(counts)
            self.auto_continue_summary.setText('Available fixes: ' + display(field(result, 'summary'))
                + ('\nRecorded action/check outcomes: ' + outcomes if outcomes else '')
                + '\nThis batch has been attempted. Review the verification and remaining recommendations.')
            self.auto_continue_summary.show()

    def set_recommendations_available(self, available):
        self._recommendations_ready = bool(available)
        if self.completed:
            self.cancel_button.setText('Continue to recommendations' if self._recommendations_ready else 'Close results')

    def request_close(self):
        if self.completed:
            if self._recommendations_ready:
                self.set_recommendations_available(False)
                self.resultsAcknowledged.emit()
            else:
                self.close()
        else:
            self.cancel.set()
            self.worker_navigation.refresh(enabled=False)
            self.cancel_button.setEnabled(False)
            self.description.setText(self._update_cancellation_message()
                if self._waiting_for_updates else
                'Cancellation requested. The current stage must finish before the workflow stops.')

    def closeEvent(self, event):
        if self.completed:
            self._dismiss_results()
            event.accept()
        else:
            self.request_close()
            event.ignore()

    def reject(self):
        if not self.completed:
            self.request_close()
            return
        self._dismiss_results()
        super().reject()

    def _dismiss_results(self):
        self.set_recommendations_available(False)
        self.clear_auto_continue()
        if not self._results_dismissed:
            self._results_dismissed = True
            self.resultsDismissed.emit()


class PrivacyRestoreDialog(QDialog):
    """Choose one recorded privacy change; restore it only after a fresh Yes."""
    def __init__(self, window, report):
        super().__init__(window)
        self.window = window
        self._cancel = threading.Event()
        self._running = False
        self.setWindowTitle('Optimus — restore privacy settings')
        self.resize(850, 500)
        layout = QVBoxLayout(self)
        note = QLabel('Choose one recorded Windows privacy change to restore. Original values are restored only '
                      'where the saved record and current state still pass validation. Changed, unavailable or '
                      'policy-protected settings can be blocked or skipped. No record is selected automatically.')
        note.setWordWrap(True)
        layout.addWidget(note)
        self.choices = QComboBox()
        self.choices.addItem('Choose one recorded privacy change…', None)
        for item in report.get('items', []) if isinstance(report, dict) else []:
            if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not item['id']:
                continue
            label = str(item.get('created_at') or 'Unknown date') + ' | ' + str(item.get('status') or 'Unknown status')
            label += ' | ' + str(item.get('changed_count', 0)) + ' changed settings | ' + item['id']
            self.choices.addItem(label, dict(item))
        self.choices.currentIndexChanged.connect(self._selection_changed)
        layout.addWidget(self.choices)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setPlainText(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        layout.addWidget(self.details, 1)
        self.status = QLabel('Choose a change and review its metadata before restoring.')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        bar = QHBoxLayout()
        self.restore_button = QPushButton('Restore selected privacy change')
        self.restore_button.setEnabled(False)
        self.restore_button.clicked.connect(self.restore_selected)
        bar.addWidget(self.restore_button)
        bar.addStretch()
        self.close_button = QPushButton('Close')
        self.close_button.clicked.connect(self.reject)
        bar.addWidget(self.close_button)
        layout.addLayout(bar)
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._check_finished)

    def _selection_changed(self, *_):
        record = self.choices.currentData()
        self.restore_button.setEnabled(bool(not self._running and isinstance(record, dict)
                                            and record.get('id') and record.get('changed_count', 0) > 0))

    def _approve_restore(self, record):
        return ask_yes_no(self, 'Restore one recorded privacy change',
            'Restore the original supported settings from this record?\n\n' + str(record['id'])
            + '\n\nCurrent values must still match the recorded change. Blocked and skipped items are reported. '
              'No leaves the settings unchanged.', yes_label='Yes — restore', no_label='No — cancel')

    def restore_selected(self):
        if self._running or getattr(self.window, 'busy', False):
            return
        record = self.choices.currentData()
        if not isinstance(record, dict) or not record.get('id') or record.get('changed_count', 0) <= 0:
            return
        if not self._approve_restore(record):
            return
        self._cancel.clear()
        self._running = True
        self.choices.setEnabled(False)
        self.restore_button.setEnabled(False)
        self.close_button.setText('Cancel after current step')
        self.status.setText('Restoring the selected recorded settings. Current values and permissions are being checked.')
        self.window._run('restore_system_privacy', record['id'], cancel_event=self._cancel, done=self._restored)
        self._timer.start()

    def _restored(self, result):
        self._running = False
        self._timer.stop()
        self.close_button.setText('Close')
        self.status.setText('Restore returned results. Review restored, blocked, skipped and failed items below. Reopen history for a fresh list before another restore.')
        self.details.setPlainText(json.dumps(result, indent=2, ensure_ascii=False, default=str))

    def _check_finished(self):
        if self._running and not getattr(self.window, 'busy', False):
            self._running = False
            self._timer.stop()
            self.close_button.setText('Close')
            message = getattr(self.window, 'last_error', '') or self.window.status.text()
            self.status.setText('The restore did not return a result. ' + str(message))
            self.details.appendPlainText('\n' + str(message))

    def reject(self):
        if self._running:
            self._cancel.set()
            self.status.setText('Cancellation requested. Wait for the current restore step to return.')
            return
        super().reject()

    def closeEvent(self, event):
        if self._running:
            self.reject()
            event.ignore()
        else:
            event.accept()

    def shutdown(self):
        self._cancel.set()
        self._timer.stop()


class SecurityReviewDialog(QDialog):
    """Apply only individually approved security recommendations on workers."""
    def __init__(self, backend, report, parent=None):
        super().__init__(parent)
        self.backend = backend
        self.setWindowTitle('Optimus — review security recommendations')
        self.resize(930, 620)
        self._cancel = threading.Event()
        self._queue = []
        self._worker = None
        self._active = None
        self._results = []
        self._review_data = dict(report) if isinstance(report, dict) else {}
        self._remediation_approvals = {}
        self._recommendations = {}
        self._selection_controls = {}
        self._selection_fit_timer = QTimer(self)
        self._selection_fit_timer.setSingleShot(True)
        self._selection_fit_timer.timeout.connect(self._fit_selection_column)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        layout = QVBoxLayout(self)
        intro = QLabel('Review the Windows security findings below. Nothing is selected automatically. '
                       'Choose recommendations to consider, then answer Yes or No for each one. '
                       'Windows security policy and administrator access can prevent a change. '
                       'Restart recommendations open Windows controls; Optimus does not restart the PC.')
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(['Select', 'Recommendation', 'Why', 'Result'])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.setColumnWidth(0, 112)
        self.table.setColumnWidth(1, 230)
        for recommendation in report.get('recommendations', []) if isinstance(report, dict) else []:
            if not isinstance(recommendation, dict):
                continue
            identifier = recommendation.get('id')
            if not isinstance(identifier, str) or not identifier or identifier in self._recommendations:
                continue
            self._recommendations[identifier] = dict(recommendation)
            row = self.table.rowCount()
            self.table.insertRow(row)
            selection = QTableWidgetItem()
            selection.setData(Qt.UserRole, identifier)
            # A labeled widget avoids the native empty-cell focus rectangle
            # looking like a second unlabeled checkbox on Windows.
            selection.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.table.setItem(row, 0, selection)
            choice = QCheckBox('Review', self.table)
            choice.setMinimumWidth(choice.fontMetrics().horizontalAdvance('Review') + 28)
            self.table.setColumnWidth(0, max(self.table.columnWidth(0), choice.minimumWidth() + 16))
            choice.setAccessibleName('Review recommendation: ' + str(recommendation.get('title', identifier)))
            choice.setToolTip('Select for review. A separate Yes or No prompt is required before applying this recommendation.')
            choice.toggled.connect(self._selection_changed)
            self._selection_controls[identifier] = choice
            choice.installEventFilter(self)
            cell = QWidget(self.table)
            cell_layout = QHBoxLayout(cell)
            cell_layout.setContentsMargins(8, 2, 8, 2)
            cell_layout.addWidget(choice)
            cell_layout.addStretch()
            cell.setFocusProxy(choice)
            cell.setMinimumHeight(choice.sizeHint().height() + 4)
            self.table.setCellWidget(row, 0, cell)
            self.table.setRowHeight(row, max(34, choice.sizeHint().height() + 6))
            for column, value in ((1, recommendation.get('title', identifier)), (2, recommendation.get('reason', 'Review this finding.')), (3, 'Not approved')):
                item = QTableWidgetItem(str(value))
                item.setToolTip(str(value))
                self.table.setItem(row, column, item)
        layout.addWidget(self.table, 1)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(170)
        self._render_details()
        layout.addWidget(self.details)
        self.status = QLabel('No security changes have been approved.')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        bar = QHBoxLayout()
        self.apply_button = QPushButton('Review selected recommendations')
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self.apply_selected)
        bar.addWidget(self.apply_button)
        bar.addStretch()
        self.close_button = QPushButton('Not now / close')
        self.close_button.clicked.connect(self.reject)
        bar.addWidget(self.close_button)
        layout.addLayout(bar)

    def _fit_selection_column(self):
        width = 112
        for identifier, choice in self._selection_controls.items():
            # A theme or desktop font can be polished after construction.
            # Recompute from the current metrics rather than an earlier font.
            choice.setMinimumWidth(choice.fontMetrics().horizontalAdvance('Review') + 28)
            width = max(width, choice.minimumWidth() + 16)
            row = self._row(identifier)
            if row is not None:
                height = choice.sizeHint().height()
                cell = self.table.cellWidget(row, 0)
                if cell is not None:
                    cell.setMinimumHeight(height + 4)
                    cell.layout().invalidate()
                self.table.setRowHeight(row, max(34, height + 6))
        self.table.setColumnWidth(0, width)

    def eventFilter(self, watched, event):
        if (event.type() in (QEvent.FontChange, QEvent.StyleChange, QEvent.Polish)
                and watched in self._selection_controls.values()):
            self._fit_selection_column()
            # Style polish invalidates size hints after event filters return.
            # Coalesce a second fit for the next layout turn using final hints.
            self._selection_fit_timer.start(0)
        return super().eventFilter(watched, event)

    def selection_control(self, row):
        return self._selection_controls[self.table.item(row, 0).data(Qt.UserRole)]

    def _selection_changed(self, *_):
        chosen = any(self.selection_control(row).isChecked() for row in range(self.table.rowCount()))
        self.apply_button.setEnabled(chosen and self._worker is None and callable(getattr(self.backend, 'apply_security_recommendation', None)))

    def _render_details(self):
        self.details.setPlainText(json.dumps({'security_review': self._review_data,
                                             'approved_action_results': self._results}, indent=2, ensure_ascii=False, default=str))

    def _row(self, identifier):
        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).data(Qt.UserRole) == identifier:
                return row
        return None

    def _set_result(self, identifier, text):
        row = self._row(identifier)
        if row is not None:
            self.table.item(row, 3).setText(str(text))
            self.table.item(row, 3).setToolTip(str(text))

    def _ask_recommendation(self, recommendation):
        text = str(recommendation.get('title') or recommendation['id']) + '\n\n' + str(recommendation.get('reason') or '')
        text += '\n\nAction: ' + str(recommendation.get('action') or 'Apply this supported Windows security recommendation.')
        if recommendation['id'] == 'remediate_defender_threats':
            consent = self._remediation_scope()
            if consent is None:
                return False
            text += ('\n\nThis requests Defender remediation of ALL currently active threats shown in this review: '
                + ', '.join(str(identity) for identity in consent['threat_ids'])
                + '. Defender chooses its supported remediation under your existing Windows policy. '
                  'A changed active-threat list requires a new review. Quarantined items are reviewed in Windows Security.')
        if recommendation.get('requires_admin'):
            text += '\nAdministrator access may be required; a refused or policy-blocked change is reported.'
        text += '\n\nApply this recommendation now? No leaves this item unchanged.'
        return ask_yes_no(self, 'Approve one security recommendation', text)

    def _remediation_scope(self):
        threats = self._review_data.get('threats')
        if not isinstance(threats, dict) or threats.get('active_ids_complete') is not True:
            return None
        identities = threats.get('active_ids')
        fingerprint = threats.get('active_fingerprint')
        if (not isinstance(identities, list) or not 0 < len(identities) <= 1000
                or any(type(identity) is not int or not 0 <= identity <= 2 ** 63 - 1 for identity in identities)
                or len(set(identities)) != len(identities)
                or threats.get('active_fingerprint_complete') is not True
                or not isinstance(fingerprint, str) or re.fullmatch('[0-9a-f]{64}', fingerprint) is None):
            return None
        return {'confirmed': True, 'scope': 'all_current_active_defender_threats',
                'threat_ids': sorted(identities), 'resource_fingerprint': fingerprint}

    def apply_selected(self):
        if self._worker is not None:
            return
        self._queue = []
        self._remediation_approvals.clear()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            choice = self.selection_control(row)
            if not choice.isChecked():
                continue
            identifier = item.data(Qt.UserRole)
            recommendation = self._recommendations[identifier]
            if identifier == 'remediate_defender_threats' and self._remediation_scope() is None:
                self._set_result(identifier, 'Fresh active-threat list required; no removal requested')
                choice.setChecked(False)
                continue
            self._set_result(identifier, 'Approved for this attempt' if self._ask_recommendation(recommendation) else 'Declined; unchanged')
            if self.table.item(row, 3).text() == 'Approved for this attempt':
                self._queue.append(identifier)
                if identifier == 'remediate_defender_threats':
                    self._remediation_approvals[identifier] = self._remediation_scope()
            choice.setChecked(False)
        if not self._queue:
            self.status.setText('No recommendation was approved. You can close this review and continue to process suggestions.')
            return
        self._cancel.clear()
        self._start_next()

    def _start_next(self):
        if self._cancel.is_set():
            self._queue.clear()
        if not self._queue:
            self._worker = None
            self._active = None
            self.table.setEnabled(True)
            self.close_button.setEnabled(True)
            self.close_button.setText('Close review')
            self._selection_changed()
            self.status.setText('Security recommendations returned results. Review completed, blocked, failed and manual actions below.')
            return
        self._active = self._queue.pop(0)
        self.table.setEnabled(False)
        self.apply_button.setEnabled(False)
        self.status.setText('Working on: ' + str(self._recommendations[self._active].get('title') or self._active))
        self._set_result(self._active, 'Running')
        arguments = {'cancel_event': self._cancel}
        if self._active == 'remediate_defender_threats':
            arguments['remediation_consent'] = self._remediation_approvals.pop(self._active, None)
        worker = BackendWorker(self.backend.apply_security_recommendation, (self._active,), arguments)
        self._worker = worker
        worker.signals.result.connect(self._item_result)
        worker.signals.error.connect(self._item_error)
        worker.signals.finished.connect(self._item_finished)
        self.pool.start(worker)

    @Slot(object)
    def _item_result(self, result):
        self._results.append({'id': self._active, 'result': result})
        status = result.get('status', 'Returned result') if isinstance(result, dict) else 'Returned result'
        self._set_result(self._active, status)
        self._render_details()

    @Slot(str)
    def _item_error(self, error):
        self._set_result(self._active, 'Failed: ' + str(error))
        self._results.append({'id': self._active, 'error': str(error)})
        self._render_details()

    @Slot()
    def _item_finished(self):
        self._worker = None
        self._start_next()

    def reject(self):
        if self._worker is not None:
            self._cancel.set()
            self._queue.clear()
            self.close_button.setEnabled(False)
            self.status.setText('Cancellation requested. Wait for the current Windows operation to return; remaining recommendations will be skipped.')
            return
        super().reject()

    def closeEvent(self, event):
        if self._worker is not None:
            self.reject()
            event.ignore()
        else:
            event.accept()

    def shutdown(self):
        self._cancel.set()
        self._queue.clear()
        self.pool.waitForDone()


class OptimusWindow(QMainWindow):
    def __init__(self, backend, *, smoke_test=False, defer_activation=False, require_launch_review=False):
        super().__init__()
        self.backend, self.smoke_test = backend, smoke_test
        self._defer_activation = defer_activation
        from .launch_review import LaunchConsent
        self._launch_consent = LaunchConsent(required=require_launch_review and not smoke_test)
        self._launch_review_active = False
        self._launch_review_dialog = None
        self._session_approved_callback = None
        self._session_invalidated_callback = None
        self._startup_update_pending = False
        self._self_update_controller = None
        self._self_update_dialog = None
        self._self_update_notice = ''
        self.busy = False
        self._quitting = False
        self._close_when_idle = False
        self._settings_window = None
        self._sandbox_window = None
        self._revert_dialog = None
        self._setup_wizard = None
        self._worker = None
        self._pending_post_review = None
        self._pending_auto_fixes = None
        self._auto_continue_attempted = False
        self._post_review_generation = 0
        self._post_review_active = False
        self._security_review_dialog = None
        self._progress_window = None
        self._cancel = threading.Event()
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.preferences_path = Path(backend.data_dir) / 'ui-settings.json'
        try:
            self.appearance = json.loads(self.preferences_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            self.appearance = {}
        try:
            self.workflow_settings = self._settings()
        except Exception:
            self.workflow_settings = {}
        self.config = SimpleNamespace(ui_motion_enabled=self.appearance.get('motion', True) and not bool(self.workflow_settings.get('reduce_motion', False)))
        self.setWindowTitle('Optimus: Computer optimization tool')
        self.setWindowIcon(DetailedSettingsWindow._icon())
        self.resize(1280, 820)
        self.setMinimumSize(640, 480)
        self.setStyleSheet(self._qss())
        self.canvas = StarfieldCanvas(motion_allowed(self.config))
        layout = QVBoxLayout(self.canvas)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.addStretch(3)
        brand = QLabel('OPTIMUS')
        brand.setAlignment(Qt.AlignCenter)
        brand.setMinimumWidth(0)
        brand.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        brand.setStyleSheet('font-size:52px;font-weight:800;letter-spacing:11px;color:#1f9cff;background:transparent;')
        layout.addWidget(brand)
        subtitle = QLabel('COMPUTER OPTIMIZATION TOOL')
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setStyleSheet('font-size:12px;letter-spacing:4px;color:#94a3b8;background:transparent;')
        layout.addWidget(subtitle)
        layout.addSpacing(10)
        self.main_buttons = []
        for label, callback, role in (
            ('1 click Optimize and debloat', self.start_optimization, 'primary'),
            ('Settings', self.open_settings, 'secondary'), ('Sandbox', self.open_sandbox, 'secondary'),
            ('Revert', self.open_revert, 'danger')):
            row = QHBoxLayout()
            row.addStretch()
            button = QPushButton(label)
            button.setMinimumHeight(52)
            button.setMaximumWidth(430)
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            button.setCursor(Qt.PointingHandCursor)
            button.setStyleSheet('QPushButton {font-size:16px;font-weight:700;border-radius:10px;padding:10px;'+
                ('background:#1f9cff;color:#07111f;border:1px solid #6ac0ff;' if role == 'primary' else
                 'background:#a42b3e;color:#fff;border:1px solid #f07788;' if role == 'danger' else
                 'background:#101925;color:#d6e2f0;border:1px solid #33455d;')+
                '} QPushButton:hover {background:' + ('#54b4ff' if role == 'primary' else '#c83950' if role == 'danger' else '#192b40')
                + ';} QPushButton:pressed {padding-top:12px;} QPushButton:focus {border:2px solid #d5eaff;}'
                + ' QPushButton:disabled {background:#152333;color:#8394aa;border:1px solid #283a50;}')
            button.clicked.connect(callback)
            row.addWidget(button, 4)
            row.addStretch()
            layout.addLayout(row)
            layout.addSpacing(6)
            self.main_buttons.append(button)
        layout.addSpacing(8)
        self.status = QLabel('Your configured tools. One local workspace.')
        self.status.setAlignment(Qt.AlignCenter)
        self.status.setWordWrap(True)
        self.status.setStyleSheet('color:#94a3b8;background:transparent;')
        layout.addWidget(self.status)
        layout.addStretch(4)
        self.landing_scroll = QScrollArea(self)
        self.landing_scroll.setWidgetResizable(True)
        self.landing_scroll.setFrameShape(QFrame.NoFrame)
        self.landing_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.landing_scroll.setWidget(self.canvas)
        self.setCentralWidget(self.landing_scroll)
        self._window_reveal = install_global_window_reveal(self, config=self.config)
        self._build_tray()
        QApplication.instance().setQuitOnLastWindowClosed(True)
        QApplication.instance().aboutToQuit.connect(self.shutdown)

    def _qss(self):
        theme = self.appearance.get('theme', 'cyber')
        return build_qss(theme if theme in THEMES else 'cyber')

    def select_page(self, name):
        self.open_settings(page=name)

    def _settings(self):
        method = getattr(self.backend, 'preferences', None)
        return method() if callable(method) else {}

    def session_approved(self, settings=None):
        """Pure approval check, also usable by background busy predicates."""
        if self._startup_update_pending:
            return False
        if not self._launch_consent.required:
            return True
        try:
            return self._launch_consent.allows(self._settings() if settings is None else settings)
        except (OSError, ValueError, TypeError):
            return False

    def invalidate_session_review(self, reason='Saved actions changed. Review this launch before automatic maintenance.'):
        if not self._launch_consent.required:
            return
        self._launch_consent.invalidate()
        controller = getattr(self, '_automation_controller', None)
        if controller is not None:
            controller.stop()
        timer = getattr(self, '_automation_timer', None)
        if timer is not None:
            timer.stop()
        callback = self._session_invalidated_callback
        note = ''
        if callable(callback):
            try:
                note = callback() or ''
            except Exception as error:
                note = 'Automatic-runtime stop needs review: ' + str(error)
        self.status.setText(reason + (' ' + str(note) if note else ''))

    def _ask_launch_review(self, settings):
        from .launch_review import ask_launch_review
        return ask_launch_review(self, settings)

    def review_launch(self):
        """Approve this process's exact saved scope or open Settings paused."""
        if (self.smoke_test or self._quitting or self._close_when_idle or self.busy
                or self._launch_review_active or self._startup_update_pending):
            return False
        if self._settings_window is not None and self._settings_window.busy:
            self.status.setText('Wait for the active settings operation before reviewing Auto Clean.')
            return False
        if not self._launch_consent.required:
            return True
        self._launch_review_active = True
        approved = None
        try:
            self.invalidate_session_review('Review the saved actions before Auto Clean. Automatic maintenance is paused.')
            from .automation import CURRENT_SETUP_VERSION
            from .launch_review import scope_fingerprint
            for unused in range(4):
                settings = self._settings()
                from .unattended_configuration import matches_unattended
                choice = 'auto_clean' if matches_unattended(settings) else self._ask_launch_review(settings)
                if self._close_when_idle or self._quitting:
                    return False
                if choice == 'adjust_settings':
                    self.open_settings(page='Settings')
                    self.status.setText('Adjust your saved choices. Auto Clean and automatic maintenance remain paused until reviewed.')
                    return False
                if choice != 'auto_clean':
                    self.status.setText('Automatic maintenance is paused. Choose Optimize to review this launch again.')
                    return False
                if settings.get('setup_version', 0) < CURRENT_SETUP_VERSION:
                    if not self.open_initial_setup(first_run=True):
                        self.status.setText('Setup was not completed. Automatic maintenance remains paused.')
                        return False
                    continue  # The saved setup must be shown and approved again.
                current = self._settings()
                if scope_fingerprint(current) != scope_fingerprint(settings):
                    self.status.setText('Saved actions changed during review. Review the refreshed choices.')
                    continue
                self._launch_consent.approve(current)
                approved = current
                break
            if approved is None:
                self.status.setText('Saved actions keep changing. Automatic maintenance remains paused; review again when ready.')
                return False
        except (OSError, ValueError, TypeError) as error:
            self.status.setText('Launch review could not finish. Automatic maintenance remains paused: ' + str(error))
            return False
        finally:
            self._launch_review_active = False
        self.start_optimization()
        callback = self._session_approved_callback
        if callable(callback) and self.session_approved():
            callback()
        return True

    def open_initial_setup(self, checked=False, *, first_run=False):
        if self._startup_update_pending:
            self.status.setText('Finish the Optimus update check before setup. Maintenance is paused.')
            return False
        if self.busy or (self._settings_window is not None and self._settings_window.busy):
            self.status.setText('Wait for the current operation before opening setup questions.')
            return False
        if self._setup_wizard is not None:
            self._setup_wizard.show()
            self._setup_wizard.raise_()
            self._setup_wizard.activateWindow()
            return False
        from .onboarding import InitialSetupWizard
        wizard = InitialSetupWizard(self.backend, self, preferences=self._settings(), first_run=first_run)
        self._setup_wizard = wizard
        accepted, run_now = False, False
        try:
            accepted = wizard.exec() == QDialog.Accepted
            if accepted:
                self.workflow_settings = dict(wizard.saved_preferences)
                run_now = bool(wizard.run_now)
                if self._launch_consent.required:
                    self.invalidate_session_review('Setup choices saved. Review their actions before Auto Clean.')
                analysis_error = None
                # Apply only the explicitly saved read-only sampler choice here.
                method = getattr(self.backend, 'analysis_start' if self.workflow_settings.get('background_analysis_enabled') else 'analysis_stop', None)
                if callable(method) and self.session_approved(self.workflow_settings):
                    try:
                        if self.workflow_settings.get('background_analysis_enabled'):
                            method(self.workflow_settings.get('analysis_interval', 5))
                        else:
                            method()
                    except Exception as error:
                        analysis_error = str(error)
                settings_window = self._settings_window
                for key, control in getattr(settings_window, 'workflow_controls', {}).items():
                    if key not in self.workflow_settings:
                        continue
                    previous = control.blockSignals(True)
                    try:
                        value = self.workflow_settings[key]
                        if isinstance(control, QCheckBox):
                            control.setChecked(bool(value))
                        elif isinstance(control, QDoubleSpinBox):
                            control.setValue(float(self.workflow_settings[key]))
                        elif isinstance(control, QSpinBox):
                            control.setValue(int(value))
                        elif isinstance(control, QComboBox):
                            if key in ('optimization_mode', 'automation_mode'):
                                control.setCurrentIndex(max(0, control.findData(value)))
                            else:
                                control.setCurrentText(str(value))
                    finally:
                        control.blockSignals(previous)
                if settings_window is not None:
                    for name in ('retention_cleanup', 'drive_guard'):
                        helper = getattr(settings_window, name, None)
                        if helper is not None:
                            helper.sync_enabled()
                if settings_window is not None:
                    settings_window.workflow_mode_overlay = {}
                    settings_window.workflow_force_keys = set()
                    for name, control in getattr(settings_window, 'consumer_package_controls', {}).items():
                        control.setChecked(name in self.workflow_settings.get('consumer_debloat_package_names', []))
                    settings_window.workflow_control_baseline = {
                        key: control.isChecked() if isinstance(control,QCheckBox) else control.value() if isinstance(control,(QSpinBox,QDoubleSpinBox))
                        else control.currentData() if key in ('optimization_mode','automation_mode') else control.currentText()
                        for key,control in getattr(settings_window, 'workflow_controls', {}).items()}
                    settings_window.workflow_consumer_baseline = [name for name,control in getattr(settings_window, 'consumer_package_controls', {}).items() if control.isChecked()]
                    if hasattr(settings_window, 'speed_enhancements_panel'):
                        settings_window.speed_enhancements_panel.sync_settings(self.workflow_settings)
                    if hasattr(settings_window, 'workflow_unattended_browser'):
                        combo = settings_window.workflow_unattended_browser
                        combo.setCurrentIndex(max(0, combo.findData(self.workflow_settings.get('unattended_browser_policy', 'skip_open'))))
                    from .launch_review import scope_fingerprint
                    settings_window.workflow_review_baseline = scope_fingerprint(self.workflow_settings)
                message = 'Setup choices saved. ' + ('Starting the maintenance you selected.' if run_now else 'Maintenance will follow your saved cadence; no run was started now.')
                if self._launch_consent.required and not self.session_approved(self.workflow_settings):
                    message = 'Setup choices saved. Review the updated actions before Auto Clean; automatic maintenance is paused.'
                if analysis_error:
                    message += ' Background analysis could not be updated: ' + analysis_error
                self.status.setText(message)
        finally:
            self._setup_wizard = None
            wizard.setParent(None)
            wizard.deleteLater()
        if accepted and run_now:
            self.start_optimization()
        return accepted

    def open_settings(self, checked=False, page=None):
        if self._startup_update_pending:
            self.status.setText('Finish the Optimus update prompt before opening Settings. Maintenance is paused.')
            return
        if self._setup_wizard is not None:
            self.status.setText('Finish or cancel the setup questions first.')
            return
        if self.busy:
            self.status.setText('Wait for the active workflow or request cancellation in its progress window.')
            return
        if self._launch_consent.required:
            self.invalidate_session_review('Settings opened. Automatic maintenance is paused until you review Auto Clean again.')
        if self._settings_window is None:
            self._settings_window = DetailedSettingsWindow(self.backend, smoke_test=self.smoke_test)
            self._settings_window._main_window = self
            self._settings_window.setWindowTitle('Optimus - Settings and tools')
            self._settings_window.appearanceChanged.connect(self.apply_appearance)
            self._install_workflow_preferences(self._settings_window)
        self._settings_window.show()
        self._settings_window.raise_()
        self._settings_window.activateWindow()
        if page:
            self._settings_window.select_page(page)

    def _install_workflow_preferences(self, window):
        from PySide6.QtWidgets import QTabWidget, QScrollArea
        from .widgets.flow_layout import FlowLayout
        outer = window.layouts['Settings']
        appearance_page = QWidget()
        appearance_layout = QVBoxLayout(appearance_page)
        while outer.count():
            appearance_layout.addItem(outer.takeAt(0))
        tabs = QTabWidget()
        workflow_page = QWidget()
        layout = QVBoxLayout(workflow_page)
        title = QLabel('ONE-CLICK WORKFLOW')
        title.setObjectName('SectionTitle')
        layout.addWidget(title)
        description = QLabel('The main Optimize button uses the saved mode and individual choices below. Full includes your selected consumer packages; other application removals follow their separate choices. Existing Codex priority and its watcher stay unchanged unless its optional profile is enabled.')
        description.setWordWrap(True)
        layout.addWidget(description)
        try:
            settings = self._settings()
        except Exception as exc:
            settings = {}
            window.status.setText('Workflow settings unavailable: ' + str(exc))
        self.workflow_settings = settings
        controls = {}
        window.workflow_mode_overlay = {}
        window.workflow_force_keys = set()
        mode = QComboBox()
        mode.addItem('Full Optimization (individual choices remain editable)', 'full')
        mode.addItem('Custom - use my individual choices', 'custom')
        mode.setCurrentIndex(max(0, mode.findData(settings.get('optimization_mode', 'custom'))))
        controls['optimization_mode'] = mode
        layout.addWidget(QLabel('OPTIMIZATION MODE'))
        layout.addWidget(mode)
        mode_note = QLabel('Selecting Full applies the preset once. Saved changes, browser scopes, cookie exceptions and update exclusions remain in effect on later opens. Package/device installation still needs scoped approval and agreement acceptance in setup. Process and security recommendations are reviewed after completion.')
        mode_note.setWordWrap(True)
        layout.addWidget(mode_note)
        from .widgets.speed_enhancements_panel import SpeedEnhancementsPanel
        window.speed_enhancements_panel = SpeedEnhancementsPanel(window, settings)
        controls['speed_enhancements_enabled'] = window.speed_enhancements_panel.enabled
        layout.addWidget(window.speed_enhancements_panel)
        compatibility_heading = QLabel('WINDOWS UPDATE COMPATIBILITY — SAVE WITH ONE-CLICK CONFIGURATION')
        compatibility_heading.setObjectName('WindowsUpdateCompatibilityHeading')
        compatibility_heading.setWordWrap(True)
        compatibility_heading.setMinimumWidth(0)
        layout.addWidget(compatibility_heading)
        for key, label, explanation in (
            ('update_compatibility_mode_enabled', 'Use WinGet compatibility mode for approved application updates',
             'Uses the non-security --force option on the initial approved attempt. Exact identity/version, pins, signature/hash and restart checks remain in effect; unknown installer state never triggers a repeated attempt.'),
            ('windows_cpu_compatibility_enabled', 'Prepare unsupported-CPU Windows media upgrades\n(MoSetup; administrator access)',
             'Prepares the legacy media-Setup policy as a best-effort workaround. Installation Assistant may still block the upgrade. Bootability, security, personal-data and free-space checks remain in effect. Windows security/driver update scopes stay as saved; an OS upgrade is not run by automatic cleanup.')):
            control = WrappingCheckBox(label)
            control.setChecked(bool(settings.get(key, False)))
            controls[key] = control
            layout.addWidget(control)
            explanation_label = QLabel(explanation)
            explanation_label.setTextFormat(Qt.PlainText)
            explanation_label.setWordWrap(True)
            layout.addWidget(explanation_label)
        from .onboarding import CADENCES
        cadence = QComboBox()
        for value, label in CADENCES.items():
            cadence.addItem(label, value)
        cadence.setCurrentIndex(max(0, cadence.findData(settings.get('automation_mode', 'manual'))))
        controls['automation_mode'] = cadence
        layout.addWidget(QLabel('Maintenance cadence'))
        layout.addWidget(cadence)
        grid = QGridLayout()
        intensity = QComboBox()
        intensity.addItems(['Low', 'Standard', 'High'])
        intensity.setCurrentText(str(settings.get('intensity', 'Standard')))
        controls['intensity'] = intensity
        grid.addWidget(QLabel('Intensity'), 0, 0)
        grid.addWidget(intensity, 0, 1)
        age = QSpinBox()
        age.setRange(0,3650)
        age.setSpecialValueText('All eligible ages')
        age.setSuffix(' days')
        age.setValue(int(settings.get('cleanup_age_days',7)))
        controls['cleanup_age_days'] = age
        grid.addWidget(QLabel('Minimum cleanup age'), 1, 0)
        grid.addWidget(age, 1, 1)
        interval = QSpinBox()
        interval.setRange(2,60)
        interval.setSuffix(' seconds')
        interval.setValue(int(settings.get('analysis_interval',5)))
        controls['analysis_interval'] = interval
        grid.addWidget(QLabel('Background analysis interval'), 2, 0)
        grid.addWidget(interval, 2, 1)
        priority = QComboBox()
        priority.addItems(['High','AboveNormal','Normal'])
        priority.setCurrentText(str(settings.get('codex_priority','High')))
        controls['codex_priority'] = priority
        grid.addWidget(QLabel('Optional Codex target priority'), 3, 0)
        grid.addWidget(priority, 3, 1)
        layout.addLayout(grid)
        toggles = QGridLayout()
        labels = [('cleanup_enabled','Clean reviewed temporary files'),('browser_cache_enabled','Clean selected browser caches (ask before closing active browsers)'),
            ('launch_at_sign_in','Launch Optimus when I sign in to Windows'),
            ('request_admin_on_startup','Ask Windows for administrator access once at app startup (UAC)'),
            ('interface_priority_boost_enabled','Prioritize Optimus interface (Above Normal CPU; takes effect the next time Optimus opens)'),
            ('browser_history_enabled','Clear selected browsers’ history'),('browser_cookies_enabled','Clear selected cookies (can sign me out)'),
            ('browser_download_history_enabled','Clear selected browser download-list records'),
            ('consumer_debloat_enabled','Remove selected consumer bloatware'),('m365_debloat_enabled','Run Microsoft 365 debloat'),
            ('outlook_debloat_enabled','Run Outlook debloat'),('edge_debloat_enabled','Run Edge debloat'),
            ('software_updates_enabled','Check/apply supported software updates under saved approval'),
            ('update_payload_cleanup_enabled','Automatically delete tracked installer downloads after verified updates\n(keeps personal Downloads, Windows caches, logs and rollback backups)'),
            ('legacy_patch_review_enabled','Review known legacy/unofficial support provider paths (no automatic replacement installs)'),
            ('windows_security_updates_enabled','Check Windows Security/Critical patches'),
            ('windows_security_updates_install_approved','Approve automatic eligible Windows Security/Critical patch installation'),
            ('windows_security_update_agreements_accepted','Accept Microsoft agreements for this Windows Security/Critical update scope'),
            ('driver_updates_enabled','Check/apply driver offers under saved device approval'),
            ('drive_optimization_enabled','Optimize eligible internal drives (SSD TRIM / HDD optimization)'),
            ('parallel_app_updates_enabled','Update apps and Windows drivers in background while independent steps continue'),
            ('auto_continue_fixes_enabled','Automatically continue with supported post-scan fixes\n(Defender/firewall and already-approved updates)'),
            ('post_process_review_enabled','Offer process/program review after completion'),
            ('security_review_enabled','Check Windows security and offer reviewed fixes'),
            ('spyware_scan_enabled','Run a quick virus, malware and spyware scan with Microsoft Defender'),
            ('privacy_advertising_enabled','Apply selected Windows advertising/suggestion privacy preferences'),
            ('privacy_diagnostics_enabled','Apply selected optional Windows diagnostic preferences'),
            ('codex_priority_enabled','Apply optional Codex priority profile'),('background_analysis_enabled','Enable background resource analysis'),
            ('reduce_motion','Reduce window and starfield motion')]
        for index,(key,label) in enumerate(labels):
            checkbox=WrappingCheckBox(label)
            checkbox.setChecked(bool(settings.get(key,False)))
            if 'debloat' in key:
                checkbox.setStyleSheet('color:#ffad73;')
                checkbox.setToolTip('Opt in to this removal stage. Review the Debloat page and target list before enabling it.')
            controls[key]=checkbox
            toggles.addWidget(checkbox,index,0)
        layout.addLayout(toggles)
        from .widgets.retention_cleanup_choices import RetentionCleanupChoices
        def add_retention_check(key, text):
            control = WrappingCheckBox(text)
            control.setChecked(settings.get(key, False) is True)
            controls[key] = control
            layout.addWidget(control)
            return control
        def add_retention_note(text):
            label = QLabel(text)
            label.setTextFormat(Qt.PlainText)
            label.setWordWrap(True)
            layout.addWidget(label)
        window.retention_cleanup = RetentionCleanupChoices(layout, settings,
            add_check=add_retention_check, add_note=add_retention_note, controls=controls)
        from .widgets.drive_guard_choices import DriveGuardChoices
        def add_guard_check(key, text):
            control = WrappingCheckBox(text)
            control.setChecked(settings.get(key, True) is True)
            controls[key] = control
            layout.addWidget(control)
            return control
        window.drive_guard = DriveGuardChoices(layout, settings,
            add_check=add_guard_check, add_note=add_retention_note, controls=controls)
        unattended = WrappingCheckBox('Auto clean with these settings — launch at sign-in and run without Optimus prompts')
        unattended.setChecked(settings.get('unattended_auto_clean_enabled', False) is True)
        controls['unattended_auto_clean_enabled'] = unattended
        layout.addWidget(unattended)
        unattended_browser = QComboBox()
        unattended_browser.addItem('Skip open browsers without asking', 'skip_open')
        unattended_browser.addItem('Request graceful browser close and clean without asking', 'close_gracefully')
        unattended_browser.setCurrentIndex(max(0, unattended_browser.findData(settings.get('unattended_browser_policy', 'skip_open'))))
        window.workflow_unattended_browser = unattended_browser
        layout.addWidget(unattended_browser)
        unattended_note = QLabel('Saving this opt-in approves the selected temporary/browser deletion, app removals, updates and privacy actions '
                                'for unattended launches. It enables sign-in launch and startup cadence. Windows administrator consent can still appear. '
                                'Individually selected retention cleanup can permanently delete archived Codex conversations, inactive Angerona logs and old incomplete or paused-download fragments. '
                                'Choosing graceful browser close can close windows and lose unsaved forms; the default skips open browsers. '
                                'Unsupported or interactive actions remain in results. No automatic reboot or forced browser termination is requested. '
                                'Changing saved scopes elsewhere invalidates this approval until you save it again here or in setup.')
        unattended_note.setTextFormat(Qt.PlainText)
        unattended_note.setWordWrap(True)
        layout.addWidget(unattended_note)
        def sync_unattended_controls():
            enabled = unattended.isChecked()
            if enabled:
                controls['launch_at_sign_in'].setChecked(True)
                cadence.setCurrentIndex(cadence.findData('startup'))
            controls['launch_at_sign_in'].setEnabled(not enabled)
            cadence.setEnabled(not enabled)
            unattended_browser.setEnabled(enabled)
        unattended.toggled.connect(sync_unattended_controls)
        sync_unattended_controls()
        continuation_note = QLabel('Saving automatic continuation approves supported Defender/firewall fixes and updates within your existing saved scopes and agreements after a completed scan. Process choices, application removal and restarts remain manual. App and Windows driver updates share a single background installer lane while independent steps continue. Drive optimization uses eligible Windows SSD TRIM/HDD operations after fresh checks.')
        continuation_note.setWordWrap(True)
        layout.addWidget(continuation_note)
        sign_in_note = QLabel('Saving the sign-in option enables or disables the managed Windows startup entry. Windows starts Optimus as your regular user about one minute after sign-in; maintenance follows its separate cadence. The administrator option requests normal Windows UAC consent once per app launch. Already elevated sessions do not ask again; declining keeps standard access. A failed registration leaves the saved preference unchanged. A startup entry you disable manually stays disabled until you explicitly apply this choice again.')
        sign_in_note.setWordWrap(True)
        layout.addWidget(sign_in_note)
        sign_in_status = QLabel('Windows sign-in launch status has not been checked in this session.')
        sign_in_status.setObjectName('SignInLaunchStatus')
        sign_in_status.setWordWrap(True)
        layout.addWidget(sign_in_status)
        sign_in_buttons = FlowLayout()
        check_sign_in = QPushButton('Check sign-in launch status')
        apply_sign_in = QPushButton('Apply sign-in launch choice now')
        check_sign_in.setObjectName('CheckSignInLaunch')
        apply_sign_in.setObjectName('ApplySignInLaunch')
        sign_in_buttons.addWidget(check_sign_in)
        sign_in_buttons.addWidget(apply_sign_in)
        layout.addLayout(sign_in_buttons)
        def show_sign_in_status(result):
            if not isinstance(result, dict):
                sign_in_status.setText('Windows sign-in launch could not be checked. No startup change was made.')
                return
            states = {'enabled':'Enabled', 'disabled':'Disabled', 'absent':'Missing',
                      'foreign':'Unrecognized entry; Optimus will preserve it', 'unavailable':'Unavailable'}
            state = states.get(result.get('state'), 'Unavailable')
            reason = str(result.get('reason') or '')[:600]
            sign_in_status.setText('Windows sign-in entry: ' + state + ('. ' + reason if reason else '.'))
        def apply_sign_in_choice():
            enabled = controls['launch_at_sign_in'].isChecked()
            action = 'Enable' if enabled else 'Disable'
            if not window._confirm('Windows sign-in launch', action + ' the managed Optimus sign-in launch for this user now? This saves only the sign-in option. Administrator access still uses a normal Windows UAC prompt when its separate startup option is saved and enabled.'):
                return
            def applied(result):
                window.workflow_control_baseline['launch_at_sign_in'] = enabled
                self.workflow_settings['launch_at_sign_in'] = enabled
                show_sign_in_status(result)
            window._run('set_startup_launch', enabled, done=applied)
        check_sign_in.clicked.connect(lambda: window._run('startup_launch_status', done=show_sign_in_status))
        apply_sign_in.clicked.connect(apply_sign_in_choice)
        privacy_layout = QVBoxLayout()
        privacy_layout.addWidget(QLabel('INDIVIDUAL WINDOWS PRIVACY CHOICES'))
        for key, label in (
            ('privacy_disable_advertising_id','Turn off Windows advertising ID'),
            ('privacy_disable_tailored_experiences','Turn off diagnostic-data tailored experiences'),
            ('privacy_disable_suggestions','Turn off supported Windows consumer suggestions'),
            ('privacy_disable_optional_diagnostics','Limit optional diagnostic collection'),
            ('privacy_disable_app_diagnostics','Turn off supported app diagnostic access')):
            control = WrappingCheckBox(label)
            control.setChecked(bool(settings.get(key, True)))
            controls[key] = control
            privacy_layout.addWidget(control)
        layout.addLayout(privacy_layout)
        for key, label in (
            ('privacy_block_silent_consumer_apps_enabled', 'Reduce silent Windows consumer-app installations (supported preferences/policies)'),
            ('privacy_disable_cloud_promotions_enabled', 'Disable supported Windows cloud and account promotions (edition dependent)'),
            ('privacy_disable_clipboard_sync_enabled', 'Disable Windows cross-device clipboard sharing (supported editions)'),
            ('privacy_disable_recall_snapshots_enabled', 'Disable Recall snapshots and ask Windows to delete previous snapshots — irreversible')):
            control = WrappingCheckBox(label)
            control.setChecked(settings.get(key, False) is True)
            controls[key] = control
            layout.addWidget(control)
        privacy_limit = QLabel('Reinstall controls cover supported Windows consumer-content settings, not every vendor installer. '
                              'Recall policy changes can delete existing snapshots; restoring the policy cannot recover them. '
                              'Unsupported editions/builds are reported without writing ineffective policy values.')
        privacy_limit.setTextFormat(Qt.PlainText)
        privacy_limit.setWordWrap(True)
        layout.addWidget(privacy_limit)
        security_note = QLabel('Defender and Windows security policy control detections and remediation. Optimus does not identify ordinary apps as spyware from their names or network activity, or block arbitrary app traffic. Privacy preferences affect supported Windows settings; they do not prevent all required diagnostics or third-party data collection. Each selected security fix asks Yes or No after the run.')
        security_note.setWordWrap(True)
        layout.addWidget(security_note)
        privacy_restore = QPushButton('Restore privacy settings')
        privacy_restore.setAccessibleName('Load recorded Windows privacy changes and restore a selected change')
        def show_privacy_history(report):
            dialog = PrivacyRestoreDialog(window, report)
            window._privacy_restore_dialog = dialog
            try:
                dialog.exec()
            finally:
                dialog.shutdown()
                window._privacy_restore_dialog = None
                dialog.deleteLater()
        privacy_restore.clicked.connect(lambda: window._run('system_privacy_history', done=show_privacy_history))
        layout.addWidget(privacy_restore)
        from .workflow import CONSUMER_PACKAGE_NAMES
        layout.addWidget(QLabel('CURRENT-USER CONSUMER PACKAGE SCOPE'))
        consumer_bar = FlowLayout()
        select_consumer, clear_consumer = QPushButton('Select all consumer packages'), QPushButton('Clear all consumer packages')
        consumer_bar.addWidget(select_consumer)
        consumer_bar.addWidget(clear_consumer)
        layout.addLayout(consumer_bar)
        consumer_controls = {}
        selected_names = set(settings.get('consumer_debloat_package_names', CONSUMER_PACKAGE_NAMES))
        for name in CONSUMER_PACKAGE_NAMES:
            control = QCheckBox(name)
            control.setChecked(name in selected_names)
            consumer_controls[name] = control
            layout.addWidget(control)
        window.consumer_package_controls = consumer_controls
        select_consumer.clicked.connect(lambda: [control.setChecked(True) for control in consumer_controls.values()])
        clear_consumer.clicked.connect(lambda: [control.setChecked(False) for control in consumer_controls.values()])
        package_note = QLabel('The consumer stage removes only the selected exact current-user package names. Bing Wallpaper, security components, frameworks and shared WebView2 remain protected. Review other application removals separately. Run setup questions to edit browser scopes, cookie exceptions, package exclusions and update/device approvals.')
        package_note.setWordWrap(True)
        layout.addWidget(package_note)
        note=QLabel('Full starts with age 0: all eligible temporary/cache ages. Low selects 30 days, Standard 7 days and High 1 day; an intensity change does not enable other modules. Active, locked and protected files remain excluded.')
        note.setWordWrap(True)
        layout.addWidget(note)
        intensity.currentTextChanged.connect(lambda value: age.setValue({'Low':30,'Standard':7,'High':1}[value]))
        def change_mode():
            if mode.currentData() != 'full':
                window.workflow_mode_overlay['optimization_mode'] = 'custom'
                return
            from .workflow import full_optimization_settings
            current = dict(self._settings())
            current.update(window.workflow_mode_overlay)
            for key, control in controls.items():
                current[key] = control.isChecked() if isinstance(control,QCheckBox) else control.value() if isinstance(control,(QSpinBox,QDoubleSpinBox)) else control.currentData() if key in ('optimization_mode','automation_mode') else control.currentText()
            current['consumer_debloat_package_names'] = [name for name, control in consumer_controls.items() if control.isChecked()]
            preset = full_optimization_settings(current)
            window.workflow_force_keys.update(controls)
            window.workflow_force_keys.add('consumer_debloat_package_names')
            window.workflow_mode_overlay.update({key:value for key,value in preset.items() if current.get(key) != value})
            for key, control in controls.items():
                if key not in preset:
                    continue
                blocked = control.blockSignals(True)
                try:
                    if isinstance(control,QCheckBox):
                        control.setChecked(bool(preset[key]))
                    elif isinstance(control,QDoubleSpinBox):
                        control.setValue(float(preset[key]))
                    elif isinstance(control,QSpinBox):
                        control.setValue(int(preset[key]))
                    elif key in ('optimization_mode','automation_mode'):
                        control.setCurrentIndex(max(0, control.findData(preset[key])))
                    else:
                        control.setCurrentText(str(preset[key]))
                finally:
                    control.blockSignals(blocked)
            for name, control in consumer_controls.items():
                control.setChecked(name in preset['consumer_debloat_package_names'])
        mode.currentIndexChanged.connect(change_mode)
        setup_button=QPushButton('Run setup questions again')
        setup_button.clicked.connect(self.open_initial_setup)
        layout.addWidget(setup_button)
        save=QPushButton('Save one-click configuration')
        def save_configuration():
            current_settings = dict(self._settings())
            value = dict(current_settings)
            value.update(window.workflow_mode_overlay)
            for key,control in controls.items():
                chosen = control.isChecked() if isinstance(control,QCheckBox) else control.value() if isinstance(control,(QSpinBox,QDoubleSpinBox)) else control.currentData() if key in ('optimization_mode','automation_mode') else control.currentText()
                if key in window.workflow_force_keys or chosen != window.workflow_control_baseline.get(key) or key not in value:
                    value[key] = chosen
            chosen_consumers = [name for name,control in consumer_controls.items() if control.isChecked()]
            if 'consumer_debloat_package_names' in window.workflow_force_keys or chosen_consumers != window.workflow_consumer_baseline or 'consumer_debloat_package_names' not in value:
                value['consumer_debloat_package_names'] = chosen_consumers
            value['unattended_browser_policy'] = unattended_browser.currentData()
            if value.get('unattended_auto_clean_enabled') is True:
                from .workflow import Preferences
                from .unattended_configuration import approve_unattended_settings
                from .launch_review import scope_fingerprint
                try:
                    if scope_fingerprint(current_settings) != window.workflow_review_baseline:
                        raise ValueError('Saved choices changed outside this form. Run setup questions again to review them before approving unattended cleaning.')
                    value = approve_unattended_settings(Preferences.validate(value))
                except (ValueError, TypeError) as error:
                    window._error('Unattended approval was not saved: ' + str(error) + ' Complete setup before enabling this option.')
                    return
            if value.get('windows_security_updates_enabled') and value.get('windows_security_updates_install_approved') and not value.get('windows_security_update_agreements_accepted'):
                window._error('Accept Microsoft update agreements for automatic Windows Security/Critical patches, or leave installation approval off. No automatic reboot is requested.')
                return
            def saved(result):
                window.workflow_mode_overlay = {}
                window.workflow_force_keys = set()
                window.workflow_control_baseline = {key:control.isChecked() if isinstance(control,QCheckBox) else control.value() if isinstance(control,(QSpinBox,QDoubleSpinBox)) else control.currentData() if key in ('optimization_mode','automation_mode') else control.currentText() for key,control in controls.items()}
                window.workflow_consumer_baseline = [name for name,control in consumer_controls.items() if control.isChecked()]
                self.workflow_settings=result if isinstance(result,dict) else value
                from .launch_review import scope_fingerprint
                window.workflow_review_baseline = scope_fingerprint(self.workflow_settings)
                window.speed_enhancements_panel.sync_settings(self.workflow_settings)
                self.config.ui_motion_enabled=bool(self.appearance.get('motion',True)) and not bool(value.get('reduce_motion',False))
                self.canvas.motion=motion_allowed(self.config)
                self.canvas.sync_motion()
                window.status.setText('One-click configuration saved. The main Optimize button will use these selected modules.')
            window._run('save_preferences',value,done=saved)
        save.clicked.connect(save_configuration)
        layout.addWidget(save)
        layout.addStretch()
        scroll=QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(workflow_page)
        tabs.addTab(scroll,'One-click configuration')
        appearance_scroll=QScrollArea()
        appearance_scroll.setWidgetResizable(True)
        appearance_scroll.setWidget(appearance_page)
        tabs.addTab(appearance_scroll,'Appearance and Windows tools')
        outer.addWidget(tabs)
        window.workflow_controls=controls
        from .launch_review import scope_fingerprint
        window.workflow_review_baseline = scope_fingerprint(settings)
        window.workflow_control_baseline = {key:control.isChecked() if isinstance(control,QCheckBox) else control.value() if isinstance(control,(QSpinBox,QDoubleSpinBox)) else control.currentData() if key in ('optimization_mode','automation_mode') else control.currentText() for key,control in controls.items()}
        window.workflow_consumer_baseline = [name for name,control in consumer_controls.items() if control.isChecked()]

    def apply_appearance(self, settings):
        self.appearance = settings
        self.config.ui_motion_enabled = bool(settings.get('motion', True)) and not bool(getattr(self,'workflow_settings',{}).get('reduce_motion',False))
        self.canvas.motion = motion_allowed(self.config)
        self.canvas.sync_motion()
        self.setStyleSheet(self._qss())

    def open_revert(self, checked=False):
        if self._startup_update_pending:
            self.status.setText('Finish the Optimus update prompt before opening Revert.')
            return
        if (self.busy or self._quitting or self._close_when_idle or self._setup_wizard is not None
                or self._post_review_active or self._settings_window is not None and self._settings_window.busy):
            self.status.setText('Wait for the active operation or questions before reviewing Revert.')
            return
        self.invalidate_session_review('Revert opened. Automatic maintenance is paused while you review existing changes.')
        if self._revert_dialog is None:
            from .widgets.revert_dialog import RevertDialog
            self._revert_dialog = RevertDialog(self)
        self._revert_dialog.show()
        self._revert_dialog.raise_()
        self._revert_dialog.activateWindow()
        if not self.smoke_test:
            QTimer.singleShot(0, self._revert_dialog.refresh)

    def open_sandbox(self, checked=False):
        if self._startup_update_pending:
            self.status.setText('Finish the Optimus update prompt before opening Sandbox.')
            return
        if self._setup_wizard is not None:
            self.status.setText('Finish or cancel the setup questions before opening Sandbox.')
            return
        if self.busy:
            self.status.setText('Wait for the active workflow before opening the Sandbox.')
            return
        try:
            if self._sandbox_window is None:
                from optimus.sandbox import SandboxWindow
                self._sandbox_window = SandboxWindow(self.backend)
            self._sandbox_window.show()
            self._sandbox_window.raise_()
            self._sandbox_window.activateWindow()
        except Exception as exc:
            self.status.setText('Sandbox could not open: ' + str(exc))
            if not self.smoke_test:
                QMessageBox.warning(self, 'Optimus Sandbox', str(exc))

    def start_optimization(self, checked=False):
        if self._startup_update_pending:
            self.status.setText('Optimus checks and updates itself before Auto Clean. Maintenance is paused.')
            return
        if self._close_when_idle:
            self.status.setText('Optimus is stopping safely. Wait for active operations to finish.')
            return
        if self.smoke_test:
            self.status.setText('Optimization is disabled during a UI smoke test.')
            return
        if self.busy or self._post_review_active or (self._settings_window is not None and self._settings_window.busy):
            self.status.setText('Wait for the current task before starting optimization.')
            self.activate_existing_window()
            return
        try:
            settings = self._settings()
        except Exception as exc:
            QMessageBox.warning(self, 'Optimus configuration', str(exc))
            return
        if self._setup_wizard is not None:
            self.status.setText('Finish or cancel the setup questions first.')
            self.activate_existing_window()
            return
        from .automation import CURRENT_SETUP_VERSION
        if not self.session_approved(settings):
            if self._launch_review_active:
                self.status.setText('Finish the current launch review before Auto Clean.')
                return
            self.review_launch()
            return
        if int(settings.get('setup_version', 0)) < CURRENT_SETUP_VERSION:
            self.open_initial_setup(first_run=True)
            return
        if not callable(getattr(self.backend, 'optimize', None)):
            QMessageBox.warning(self, 'Optimus', 'The workflow engine is unavailable. No action was taken.')
            return
        if self._progress_window is not None and not self._progress_window.completed:
            self.status.setText('Wait for the previous progress window to finish before starting another workflow.')
            self.activate_existing_window()
            return
        self.busy = True
        self._pending_post_review = None
        self._pending_auto_fixes = None
        self._auto_continue_attempted = False
        self._post_review_generation += 1
        if self._progress_window is not None:
            previous, self._progress_window = self._progress_window, None
            previous.clear_auto_continue()
            previous.close()
            previous.deleteLater()
        self._cancel = threading.Event()
        self._progress_window = OptimizationProgressWindow(self, self._cancel)
        progress = self._progress_window
        progress.configure_workers(settings)
        progress.resultsAcknowledged.connect(lambda: self._continue_post_optimization_reviews()
            if progress is self._progress_window else None)
        progress.resultsDismissed.connect(lambda: self._dismiss_post_optimization_reviews()
            if progress is self._progress_window else None)
        progress.autoContinueRequested.connect(lambda: self._start_auto_continue(progress))
        self._progress_window.show()
        if self._self_update_notice:
            self._progress_window.log.appendPlainText(self._self_update_notice)
        self._worker = WorkflowWorker(self.backend, settings, self._cancel, prompt_parent=self._progress_window)
        self._worker.signals.progress.connect(self._progress_window.update_progress)
        self._worker.signals.result.connect(self._workflow_result)
        self._worker.signals.error.connect(self._workflow_error)
        self._worker.signals.finished.connect(self._workflow_finished)
        self.main_buttons[0].setEnabled(False)
        self.pool.start(self._worker)

    @Slot(object)
    def _workflow_result(self, result):
        self._progress_window.finish(result)
        if self._close_when_idle:
            self._pending_post_review = None
            self._pending_auto_fixes = None
            self.status.setText('Current work returned. Closing after its worker finishes saving outcomes.')
            return
        settings = dict(getattr(self._worker, 'settings', {}))
        self._prepare_post_reviews(result, settings)
        self._pending_auto_fixes = None
        if self._completed_result(result):
            plan = {'actions': [], 'manual_items': [], 'summary': 'Automatic follow-up is unavailable with this backend.'}
            preview = getattr(self.backend, 'available_fixes', None)
            apply = getattr(self.backend, 'apply_available_fixes', None)
            if callable(preview) and callable(apply):
                try:
                    candidate = preview(result, settings)
                    if isinstance(candidate, dict):
                        plan = candidate
                except Exception as exc:
                    plan['summary'] = 'Available fixes could not be reviewed: ' + str(exc)
            self._progress_window.set_auto_continue_plan(plan)
            if isinstance(plan.get('actions'), list) and plan['actions']:
                self._pending_auto_fixes = (result, settings, plan)
                if settings.get('auto_continue_fixes_enabled') is True:
                    self._progress_window.title.setText('CONTINUING OPTIMIZATION')
                    self._progress_window.stage.setText('SCAN COMPLETE — SAVED FOLLOW-UP QUEUED')
                    self._progress_window.description.setText('Your saved automatic fix choice continues with the supported actions listed below. Closing these results skips the queued continuation.')
        self.status.setText('Workflow returned results. Review available fixes and final verification before continuing.')

    @staticmethod
    def _completed_result(result):
        return (isinstance(result, dict) and not result.get('cancelled') and not result.get('canceled')
            and result.get('status') in ('completed', 'completed_with_errors'))

    def _prepare_post_reviews(self, result, settings):
        self._pending_post_review = None
        from .unattended_configuration import matches_unattended
        if matches_unattended(settings):
            return  # Findings remain in the report; unattended mode opens no questions.
        if self._completed_result(result):
            security, processes = result.get('security_review'), result.get('process_review')
            if ((isinstance(security, dict) and security.get('recommendations'))
                    or settings.get('post_process_review_enabled') and isinstance(processes, dict)):
                self._pending_post_review = (result, settings)

    @Slot(str)
    def _workflow_error(self, error):
        self._pending_post_review = None
        self._pending_auto_fixes = None
        self._progress_window.finish(None, error=error)
        self.status.setText('Workflow stopped: ' + error)

    @Slot()
    def _workflow_finished(self):
        self.busy = False
        self.main_buttons[0].setEnabled(True)
        bridge = getattr(self._worker, '_browser_prompts', None)
        if bridge is not None:
            bridge.shutdown()
            bridge.deleteLater()
        self._worker = None
        if self._close_when_idle:
            QTimer.singleShot(0, self.quit_application)
            return
        self._enable_result_actions()

    def _enable_result_actions(self):
        if self.smoke_test or self._quitting or not self._progress_window.isVisible():
            self._pending_post_review = None
            self._pending_auto_fixes = None
            self._post_review_active = False
            return
        self._post_review_active = self._pending_post_review is not None or self._pending_auto_fixes is not None
        self._progress_window.set_recommendations_available(self._pending_post_review is not None)
        if self._pending_auto_fixes is not None and not self._auto_continue_attempted:
            self._progress_window.set_auto_continue_plan(self._pending_auto_fixes[2], ready=True)
            if self._pending_auto_fixes[1].get('auto_continue_fixes_enabled') is True:
                progress = self._progress_window
                generation = self._post_review_generation
                parent = self._pending_auto_fixes[0]
                self.status.setText('Scan complete. Continuing with your saved automatic fix choice.')
                QTimer.singleShot(0, lambda: self._continue_saved_fixes(progress, generation, parent))

    def _continue_saved_fixes(self, progress, generation, parent):
        if (generation != self._post_review_generation or progress is not self._progress_window
                or self._pending_auto_fixes is None or self._pending_auto_fixes[0] is not parent):
            return
        try:
            enabled = self._settings().get('auto_continue_fixes_enabled') is True
        except Exception:
            enabled = False
        if enabled:
            if not self.session_approved():
                self.invalidate_session_review('Saved choices changed. Automatic follow-up is paused until a new launch review.')
                return
            self._start_auto_continue(progress, automatic=True)

    def _start_auto_continue(self, progress, *, automatic=False):
        if (self.smoke_test or self._quitting or self._close_when_idle or self.busy or self._auto_continue_attempted
                or self._pending_auto_fixes is None or progress is not self._progress_window
                or not progress.isVisible() or not progress.completed
                or self._security_review_dialog is not None or self._setup_wizard is not None
                or self._settings_window is not None and self._settings_window.busy):
            return
        if not self.session_approved():
            self.invalidate_session_review('Saved choices changed. Review Auto Clean before starting additional automatic actions.')
            return
        report, settings, plan = self._pending_auto_fixes
        if not plan.get('actions') or not callable(getattr(self.backend, 'apply_available_fixes', None)):
            return
        self._auto_continue_attempted = True
        self._post_review_generation += 1
        self._pending_auto_fixes = None
        self._pending_post_review = None
        self._post_review_active = False
        self.busy = True
        self._cancel = threading.Event()
        progress.begin_auto_continue(self._cancel, automatic=automatic)
        self._worker = AvailableFixesWorker(self.backend, report, settings, self._cancel)
        self._worker.signals.progress.connect(progress.update_progress)
        self._worker.signals.result.connect(self._auto_fixes_result)
        self._worker.signals.error.connect(self._auto_fixes_error)
        self._worker.signals.finished.connect(self._auto_fixes_finished)
        self.main_buttons[0].setEnabled(False)
        self.status.setText('Continuing with your saved automatic fix choice. Cancellation is available in the progress window.'
            if automatic else 'Applying the available fixes you approved. Cancellation is available in the progress window.')
        self.pool.start(self._worker)

    @Slot(object)
    def _auto_fixes_result(self, result):
        self._progress_window.finish_auto_continue(result)
        if self._close_when_idle:
            self._pending_post_review = None
            self._pending_auto_fixes = None
            self.status.setText('Current fixes returned. Closing after their worker finishes saving outcomes.')
            return
        self._prepare_post_reviews(result, dict(getattr(self._worker, 'settings', {})))
        self.status.setText('Available fixes returned results. Review the recorded outcomes and final verification.')

    @Slot(str)
    def _auto_fixes_error(self, error):
        self._pending_post_review = None
        self._progress_window.finish_auto_continue(None, error=error)
        self.status.setText('Available fixes stopped: ' + error)

    @Slot()
    def _auto_fixes_finished(self):
        self.busy = False
        self.main_buttons[0].setEnabled(True)
        self._worker = None
        if self._close_when_idle:
            QTimer.singleShot(0, self.quit_application)
            return
        self._enable_result_actions()

    def _continue_post_optimization_reviews(self):
        self._pending_auto_fixes = None
        self._progress_window.clear_auto_continue()
        pending, self._pending_post_review = self._pending_post_review, None
        if pending is not None and not self.smoke_test and not self._quitting:
            generation = self._post_review_generation
            QTimer.singleShot(0, lambda: self._offer_post_optimization_reviews(*pending)
                if generation == self._post_review_generation and not self._quitting else None)
        else:
            self._post_review_active = False

    def _dismiss_post_optimization_reviews(self):
        self._pending_post_review = None
        self._pending_auto_fixes = None
        self._post_review_generation += 1
        self._post_review_active = False
        self.status.setText('Results are saved. Remaining recommendation prompts were dismissed.')

    def _ask_process_review(self, report):
        actions = report.get('actions', [])
        count = len(actions) if isinstance(actions, list) else 0
        return ask_yes_no(self, 'Optimization complete — review background programs?',
            f'The final process scan lists {count} optional process/startup choices. Review them now?\n\n'
            'Yes opens the Process cleanup harness with nothing selected. You decide which applications to pause '
            'or startup entries to disable. Installed-application review is available there. No continues without those changes. '
            'RAM use or process age alone does not prove an application is unwanted.',
            yes_label='Yes — review', no_label='No — skip')

    def _offer_post_optimization_reviews(self, result, settings):
        from .unattended_configuration import matches_unattended
        if self.smoke_test or self._quitting or matches_unattended(settings):
            self._post_review_active = False
            return
        self._post_review_active = True
        opened_process_review = False
        if self._progress_window is not None:
            self._progress_window.hide()
        try:
            security = result.get('security_review')
            if isinstance(security, dict) and security.get('recommendations'):
                dialog = None
                try:
                    dialog = SecurityReviewDialog(self.backend, security, self)
                    self._security_review_dialog = dialog
                    dialog.exec()
                except Exception as error:
                    self.status.setText('The security recommendation review could not open: ' + str(error))
                finally:
                    self._security_review_dialog = None
                    if dialog is not None:
                        dialog.shutdown()
                        dialog.deleteLater()
            processes = result.get('process_review')
            if settings.get('post_process_review_enabled') and isinstance(processes, dict) and self._ask_process_review(processes):
                self.open_settings(page='Performance')
                if self._settings_window is not None:
                    self._settings_window.performance_tabs.setCurrentIndex(2)
                    self._settings_window.process_harness_panel._scan_ready(processes)
                    self._settings_window.raise_()
                    self._settings_window.activateWindow()
                    opened_process_review = True
        finally:
            self._post_review_active = False
            if not opened_process_review and self._progress_window is not None and not self._quitting:
                self._progress_window.show()

    def activate_existing_window(self):
        """Restore the current question or progress instead of opening new work."""
        if self._quitting:
            return False
        target = QApplication.activeModalWidget()
        if target is None:
            for candidate in (self._setup_wizard, self._security_review_dialog,
                              self._progress_window, self._settings_window):
                if candidate is not None and (candidate.isVisible() or
                        candidate is self._progress_window and self.busy):
                    target = candidate
                    break
        target = target or self
        if target.isMinimized():
            target.showNormal()
        else:
            target.show()
        target.raise_()
        target.activateWindow()
        if sys.platform == 'win32':
            try:
                import ctypes
                ctypes.windll.user32.SetForegroundWindow(int(target.winId()))
            except (AttributeError, OSError, TypeError, ValueError):
                pass
        return True

    def _restore_window(self):
        self.activate_existing_window()

    def restart_as_administrator(self):
        if self._startup_update_pending:
            self.status.setText('Finish the Optimus update prompt before requesting an administrator restart.')
            return False
        if self.smoke_test:
            self.status.setText('Administrator restart is disabled during a UI smoke test.')
            return False
        if getattr(self, '_admin_restarting', False):
            return False
        bridge = getattr(self.backend, 'bridge', None)
        if (self.busy or self._post_review_active or self._setup_wizard is not None
                or self._settings_window is not None and self._settings_window.busy
                or bool(getattr(getattr(self.backend, 'workflow', None), 'active', False))
                or bool(getattr(bridge, '_busy', False)) or bool(getattr(bridge, 'busy', False))
                or bool(getattr(bridge, '_pending', {})) or bool(getattr(bridge, 'pending', {}))):
            self.status.setText('Finish the current operation or questions before restarting as administrator.')
            self.activate_existing_window()
            return False
        is_admin = getattr(self.backend, 'is_admin', None)
        if callable(is_admin) and is_admin():
            self.status.setText('Optimus already has administrator access.')
            return False
        if self._sandbox_window is not None and not self._sandbox_window._unsaved():
            return False
        self._admin_restarting = True
        try:
            from .elevation import request_administrator
            result = request_administrator()
        finally:
            self._admin_restarting = False
        self.status.setText(result.get('note', 'Administrator relaunch could not start.'))
        if result.get('started') is not True:
            return False
        # The UAC child waits on our GUI lease. Exit only after Windows accepts
        # its normal consent flow, and release the lease during orderly shutdown.
        self._quitting = True
        self.shutdown()
        QApplication.instance().quit()
        return True

    def _build_tray(self):
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        self.tray.setToolTip('Optimus: Computer optimization tool')
        menu = QMenu()
        show = QAction('Open Optimus', self)
        show.triggered.connect(self._restore_window)
        quit_action = QAction('Quit Optimus', self)
        quit_action.triggered.connect(self.quit_application)
        menu.addAction(show)
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self._restore_window() if reason == QSystemTrayIcon.Trigger else None)
        if not self.smoke_test and not self._defer_activation:
            self.tray.show()

    def changeEvent(self, event):
        super().changeEvent(event)
        if hasattr(self, 'canvas'):
            QTimer.singleShot(0, self.canvas, self.canvas.sync_motion)

    def closeEvent(self, event):
        if self._quitting or self.smoke_test:
            event.accept()
        elif self.busy:
            self.request_safe_stop()
            event.ignore()
        else:
            if self.quit_application():
                event.accept()
            else:
                event.ignore()

    def quit_application(self):
        if self._startup_update_pending and self._self_update_controller is not None:
            self._self_update_controller.request_safe_stop()
            return False
        if self.busy:
            self.request_safe_stop()
            return False
        if self._post_review_active:
            self.status.setText('Finish or close the post-optimization recommendations before exiting.')
            return False
        if self._setup_wizard is not None:
            self.status.setText('Finish or cancel the setup questions before exiting.')
            return False
        if self.busy or (self._settings_window is not None and self._settings_window.busy):
            self.status.setText('An operation is active. Wait for completion or cancel at a workflow stage boundary before exiting.')
            if self._progress_window is not None:
                self._progress_window.show()
                self._progress_window.raise_()
            return False
        if self._sandbox_window is not None:
            # Run the editor's unsaved-work guard synchronously before tearing
            # down the application; its reverse-close animation defers close().
            sandbox = self._sandbox_window
            previous_bypass = getattr(sandbox, '_optimus_close_bypass', False)
            sandbox._optimus_close_bypass = True
            try:
                closed = sandbox.close()
            finally:
                sandbox._optimus_close_bypass = previous_bypass
            if not closed:
                self._close_when_idle = False
                self.status.setText('Save or discard the Sandbox edits before exiting, or cancel to keep working.')
                return False
        self._quitting = True
        self.shutdown()
        QApplication.instance().quit()
        return True

    def request_safe_stop(self):
        """Stop dispatching work, drain the current operation, then close.

        Keep Qt processing signals while our workflow joins its installer
        workers and durably saves their receipts. Closing never kills a native
        installer or clears an unresolved outcome.
        """
        if self.smoke_test or self._quitting:
            return False
        if self._startup_update_pending and self._self_update_controller is not None:
            return self._self_update_controller.request_safe_stop()
        if self._setup_wizard is not None or self._post_review_active:
            self.status.setText('Finish the active setup, recommendation or settings operation before safely exiting.')
            return False
        self._close_when_idle = True
        self._cancel.set()
        self._pending_post_review = None
        self._pending_auto_fixes = None
        self._post_review_generation += 1
        dialog = self._launch_review_dialog
        if dialog is not None:
            dialog.reject()
        optional_stop = getattr(self.backend, 'request_optional_stop', None)
        if callable(optional_stop):
            try:
                optional_stop()
            except Exception as error:
                self.status.setText('Optional tools could not acknowledge cancellation: ' + str(error))
        if self._revert_dialog is not None and self._revert_dialog._worker is not None:
            self._revert_dialog.cancel()
        settings_busy = self._settings_window is not None and self._settings_window.busy
        if settings_busy:
            for name in ('recovery_panel', 'file_utilities_panel', 'smart_maintenance_panel', 'maintenance_toolkit_panel'):
                panel = getattr(self._settings_window, name, None)
                event = getattr(panel, '_cancel', None)
                if event is not None:
                    event.set()
        controller = getattr(self, '_automation_controller', None)
        if controller is not None:
            controller.stop()
        timer = getattr(self, '_automation_timer', None)
        if timer is not None:
            timer.stop()
        bridge = getattr(self._worker, '_browser_prompts', None)
        if bridge is not None:
            bridge.shutdown()
        if self.busy:
            progress = self._progress_window
            if progress is not None:
                progress.request_close()
                progress.description.setText('Stopping safely. No further work will start. Active operations are allowed to finish and save their outcomes before Optimus closes.')
                progress.show()
            self.status.setText('Stopping safely. Waiting for the current operations and saved outcomes before exiting.')
        elif settings_busy:
            self.status.setText('Stopping safely. Waiting for the current settings operation to save its outcome.')
            QTimer.singleShot(100, self._finish_optional_stop)
        else:
            QTimer.singleShot(0, self.quit_application)
        return True

    def _finish_optional_stop(self):
        if self._quitting or not self._close_when_idle:
            return
        if self.busy or self._settings_window is not None and self._settings_window.busy:
            QTimer.singleShot(200, self._finish_optional_stop)
            return
        self.quit_application()

    def shutdown(self):
        if getattr(self, '_presentation_shutdown', False):
            return
        self._presentation_shutdown = True
        if self._self_update_controller is not None:
            self._self_update_controller.shutdown()
        self.invalidate_session_review('Optimus is closing; automatic maintenance is stopped.')
        if self._revert_dialog is not None:
            self._revert_dialog.shutdown()
        if self._security_review_dialog is not None:
            self._security_review_dialog.shutdown()
        if self.busy:
            self._cancel.set()
        self._pending_auto_fixes = None
        bridge = getattr(self._worker, '_browser_prompts', None)
        if bridge is not None:
            self._cancel.set()
            bridge.shutdown()
        # Normal Close reaches this only after the worker finishes. An external
        # QApplication.quit() can bypass closeEvent, so it must drain our own
        # workers before app.py closes the backend. Prompt shutdown above makes
        # this independent of GUI event processing. Native timeouts retain
        # their durable uncertain receipts; no pending OS process is killed.
        self.pool.waitForDone()
        self.canvas.timer.stop()
        self._window_reveal.shutdown()
        self.tray.hide()
        if self._settings_window is not None:
            self._settings_window.shutdown()


def create_window(backend, *, smoke_test=False):
    return OptimusWindow(backend, smoke_test=smoke_test)


