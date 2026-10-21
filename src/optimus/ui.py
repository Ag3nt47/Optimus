"""Optimus desktop interface; host operations run through the local backend."""
from __future__ import annotations
import json
import os
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, QTimer, Signal, Slot, QUrl
from PySide6.QtGui import QAction, QColor, QDesktopServices, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication, QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QSpinBox, QSplitter,
    QStackedWidget, QSystemTrayIcon, QTableWidget, QTableWidgetItem, QTabWidget,
    QVBoxLayout, QWidget,
)
from optimus.widgets.theme import THEMES, build_qss
from optimus.widgets.header_controls import HeaderActionButton, install_global_window_reveal, motion_allowed
from optimus.widgets.holographic_orb import HolographicOrbController


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
        try:
            self.signals.result.emit(self.operation(*self.args, **self.kwargs))
        except Exception as exc:
            self.signals.error.emit(str(exc))
        finally:
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
                  "Debloat", "Cloud drive", "Schedules", "Registry review", "Restore/history", "Settings")
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
    }

    def __init__(self, backend, *, smoke_test=False):
        super().__init__()
        self.backend, self.smoke_test = backend, smoke_test
        self.busy = False
        self._quitting = False
        self._workers = set()
        self._callback = None
        self.audit_data, self.process_records = {}, []
        self.cleanup_plan = self.debloat_plan = self.storage_result = None
        self.loaded_pages = set()
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.settings_path = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Optimus" / "ui-settings.json"
        try:
            self.preferences = json.loads(self.settings_path.read_text(encoding="utf-8"))
            if not isinstance(self.preferences, dict):
                self.preferences = {}
        except (OSError, ValueError):
            self.preferences = {}
        self.config = SimpleNamespace(ui_motion_enabled=self.preferences.get("motion", True),
                                      holographic_orb_enabled=self.preferences.get("orb", True),
                                      holographic_orb_x=-1, holographic_orb_y=-1)
        self.setWindowTitle("Optimus: Computer optimization tool")
        self.resize(1440, 920)
        self.setMinimumSize(1080, 720)
        self.setWindowIcon(self._icon())
        self.setStyleSheet(self._qss())
        self._build_shell()
        for builder in ("dashboard", "cleaner", "browser", "performance", "startup", "applications",
                        "updates", "drivers", "storage", "duplicates", "debloat", "cloud", "schedules", "registry", "history", "settings"):
            getattr(self, "_build_" + builder)()
        self.tray = None
        self._window_reveal = None
        self._holographic_orb = None
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
        group = QButtonGroup(self)
        group.setExclusive(True)
        self.navigation = {}
        for name in self.PAGE_NAMES:
            button = QPushButton(name)
            button.setObjectName("NavButton")
            button.setCheckable(True)
            button.clicked.connect(lambda checked=False, n=name: self.select_page(n))
            group.addButton(button)
            nav.addWidget(button)
            self.navigation[name] = button
        nav.addStretch()
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
            self.stack.addWidget(page)
            self.pages[name], self.layouts[name] = page, layout
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
        bar = self._toolbar(layout)
        self._button(bar, "Refresh categories", self.load_cleanup_categories)
        bar.addWidget(QLabel("Minimum age:"))
        self.clean_age = QSpinBox()
        self.clean_age.setRange(7, 365)
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
        self.clean_summary.setText(f"Preview: {len(files):,} listed files, {size:,.1f} MB. " + ("Scan complete." if complete else "Bounded scan; additional files may exist.") + f" {skipped} skipped notes. Only this list can be applied.")
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
        layout = self.layouts["Performance"]
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
        self._button(bar, 'Uninstall selected', self.uninstall_app)
        self._button(bar, 'Windows Installed apps', lambda: self._run('system_tool', 'installed-apps'))
        bar.addStretch()
        self.apps_table = DataTable([('Program', 'Name'), ('Version', 'Version'), ('Publisher', 'Publisher'), ('Type', 'Type'), ('Location', 'InstallLocation')])
        layout.addWidget(self.apps_table, 1)
        self._note(layout, 'Use the supported uninstaller. Removal may affect application data and is not undone by restoring an Optimus settings record.')

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
        bar.addStretch()
        self.updates_output = QPlainTextEdit()
        self.updates_output.setReadOnly(True)
        self.updates_output.setPlaceholderText('Run a check to see Windows Package Manager output. Availability and source agreements can affect results.')
        layout.addWidget(self.updates_output, 1)
        bar = self._toolbar(layout)
        self.update_id = QLineEdit()
        self.update_id.setPlaceholderText('Exact package ID from the update result, for example Vendor.Application')
        bar.addWidget(self.update_id, 1)
        self._button(bar, 'Update this package', self.update_package)
        self._note(layout, 'Updates install individually after confirmation. Installers can close applications, request elevation or require a restart. Installed version alone does not establish that an update exists.')

    def _updates_ready(self, result):
        self.updates_output.setPlainText(display(field(result, 'output', 'Text', default=result)))
        self.loaded_pages.add('Updates')

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
        layout.addWidget(self.driver_tabs, 1)
        self.driver_update_note = self._note(layout, 'Available updates are queried through Windows Update Agent. A version number alone does not prove a newer driver exists. Scan is read-only; each driver installation needs your confirmation and administrator access.')
        self._note(layout, 'Back up third-party driver packages before changing them. Export is handled by Windows pnputil; inbox Windows drivers and device settings may require separate recovery. Hardware-manufacturer tools remain useful for vendor-specific packages.')

    def load_drivers(self):
        self._run('drivers_list', done=lambda r: self._page_table_ready('Drivers', self.driver_table, r))

    def _driver_updates_ready(self, result):
        candidates = rows(field(result, 'updates', default=[]))
        self.driver_updates_table.populate(candidates)
        self.driver_tabs.setCurrentIndex(1)
        self.driver_update_note.setText(f'Windows Update returned {len(candidates):,} driver candidates. Source: ' + display(field(result, 'source', default='Windows Update Agent')) + '. Result code: ' + display(field(result, 'resultCode')) + '. Review a title and revision before installation; this query does not cover every manufacturer catalog.')

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
        self.orb_toggle = QCheckBox('Collapse closed or minimized windows to the holographic orb')
        self.orb_toggle.setChecked(self.config.holographic_orb_enabled)
        options.addWidget(self.orb_toggle)
        self._button(options, 'Save appearance', self.save_preferences, primary=True)
        layout.addWidget(panel)
        access = self._toolbar(layout)
        try:
            import ctypes
            admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            admin = False
        access.addWidget(QLabel('Current access: ' + ('Administrator' if admin else 'Standard user') + '. Some system-wide actions require elevation.'), 1)
        self._button(access, 'Restart as administrator', lambda: self._run('elevate', done=self._show_result))
        tools = QGridLayout()
        for index, (label, identifier) in enumerate((('Windows Storage cleanup', 'storage'), ('Optimize drives', 'defrag'), ('Windows Update', 'windows-update'), ('Device Manager', 'device-manager'), ('Installed apps', 'installed-apps'), ('Task Scheduler', 'task-scheduler'))):
            button = QPushButton(label)
            button.clicked.connect(lambda checked=False, key=identifier: self._run('system_tool', key))
            tools.addWidget(button, index // 2, index % 2)
        layout.addLayout(tools)
        self._note(layout, 'Presentation adapted from your MIT-licensed AngeronaSuite: dark palettes, live-content reveal, reverse close and holographic minimize/restore. Optimus runs independently of Angerona services and AI providers.')
        self._note(layout, 'Close collapses to the orb when enabled. Choose Exit Optimus here or Quit from the tray to stop. Logs and settings stay in your local Optimus folder.')
        bar = self._toolbar(layout)
        self._button(bar, 'Exit Optimus', self.quit_application)
        bar.addStretch()
        layout.addStretch()

    def save_preferences(self):
        self.preferences = {'theme': self.theme_combo.currentData(), 'motion': self.motion_toggle.isChecked(), 'orb': self.orb_toggle.isChecked()}
        self.config.ui_motion_enabled = self.preferences['motion']
        self.config.holographic_orb_enabled = self.preferences['orb']
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

    def _build_tray(self):
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        self.tray.setToolTip('Optimus: Computer optimization tool')
        menu = QMenu()
        show = QAction('Open Optimus', self)
        show.triggered.connect(lambda: self._holographic_orb.restore_main())
        quit_action = QAction('Quit Optimus', self)
        quit_action.triggered.connect(self.quit_application)
        menu.addAction(show)
        menu.addSeparator()
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self._holographic_orb.restore_main() if reason == QSystemTrayIcon.Trigger else None)
        if not self.smoke_test:
            self.tray.show()

    def closeEvent(self, event):
        if self.busy:
            self.status.setText('Wait for the current operation before closing settings.')
            event.ignore()
        else:
            event.accept()

    def quit_application(self):
        if self.busy and not self._confirm('Exit Optimus', 'An operation is running. Waiting for completion is recommended. Exit the interface now?'):
            return
        self.close()

    def shutdown(self):
        if getattr(self, '_presentation_shutdown', False):
            return
        self._presentation_shutdown = True
        if hasattr(self, '_analysis_timer'):
            self._analysis_timer.stop()
        if getattr(self, '_holographic_orb', None) is not None:
            self._holographic_orb.shutdown()
        if getattr(self, '_window_reveal', None) is not None:
            self._window_reveal.shutdown()
        if getattr(self, 'tray', None) is not None:
            self.tray.hide()


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
        rng = random.Random(47)
        self.stars = [(rng.random(), rng.random(), rng.random(), rng.random()) for _ in range(190)]
        self.timer = QTimer(self)
        self.timer.setInterval(33)
        self.timer.timeout.connect(self._tick)
        self.setAutoFillBackground(False)

    def _tick(self):
        if not self.motion or not self.isVisible() or self.window().isMinimized():
            self.timer.stop()
            return
        self.phase += 0.0045
        self.update()

    def sync_motion(self):
        if self.motion and self.isVisible() and not self.window().isMinimized():
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
    def __init__(self, backend, settings, cancel):
        super().__init__()
        self.backend, self.settings, self.cancel = backend, settings, cancel
        self.signals = WorkflowSignals()

    def run(self):
        try:
            result = self.backend.optimize(self.settings, progress_callback=self.signals.progress.emit, cancel_event=self.cancel)
            self.signals.result.emit(result)
        except Exception as exc:
            self.signals.error.emit(str(exc))
        finally:
            self.signals.finished.emit()


class OptimizationProgressWindow(QDialog):
    def __init__(self, owner, cancel):
        super().__init__(owner)
        self.cancel = cancel
        self.completed = False
        self.setWindowTitle('Optimus - optimization progress')
        self.setWindowModality(Qt.WindowModal)
        self.resize(850, 650)
        layout = QVBoxLayout(self)
        title = QLabel('OPTIMIZATION IN PROGRESS')
        title.setObjectName('PageTitle')
        layout.addWidget(title)
        self.spinner = RunSpinner()
        self.spinner.start('Preparing')
        layout.addWidget(self.spinner)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat('%p%')
        layout.addWidget(self.progress)
        self.stage = QLabel('Waiting for workflow preparation')
        self.stage.setObjectName('SectionTitle')
        layout.addWidget(self.stage)
        self.description = QLabel('Progress reflects completed workflow stages. No estimated completion is fabricated.')
        self.description.setWordWrap(True)
        layout.addWidget(self.description)
        layout.addWidget(QLabel('CURRENT PROCESSES / ITEMS'))
        self.processes = QPlainTextEdit()
        self.processes.setReadOnly(True)
        self.processes.setMaximumHeight(130)
        layout.addWidget(self.processes)
        layout.addWidget(QLabel('STAGE LOG'))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        layout.addWidget(self.log, 1)
        self.cancel_button = QPushButton('Cancel after current stage')
        self.cancel_button.clicked.connect(self.request_close)
        layout.addWidget(self.cancel_button)

    @Slot(object)
    def update_progress(self, state):
        if not isinstance(state, dict):
            self.log.appendPlainText(display(state))
            return
        percentage = field(state, 'percentage', default=0)
        percentage = max(0, min(100, float(percentage or 0)))
        self.progress.setValue(int(percentage))
        self.spinner.set_pct(percentage)
        stage = display(field(state, 'stage', default='Working'))
        self.stage.setText(stage)
        self.spinner.set_text(stage)
        self.description.setText(display(field(state, 'description')))
        self.processes.setPlainText(display(field(state, 'processes', default=[])))
        log = field(state, 'log', default='')
        if log:
            self.log.appendPlainText(display(log))

    def finish(self, result, error=None):
        self.completed = True
        self.spinner.stop()
        if error:
            self.stage.setText('WORKFLOW STOPPED')
            self.description.setText(str(error))
            self.log.appendPlainText('Error: ' + str(error))
        else:
            cancelled = bool(field(result, 'cancelled', 'canceled', default=False))
            self.stage.setText('CANCELLED AT STAGE BOUNDARY' if cancelled else 'WORKFLOW RETURNED RESULTS')
            self.description.setText('Review the actual results and any skipped or failed stages below.')
            self.log.appendPlainText(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        self.cancel_button.setText('Close results')
        self.cancel_button.setEnabled(True)

    def request_close(self):
        if self.completed:
            self.close()
        else:
            self.cancel.set()
            self.cancel_button.setEnabled(False)
            self.description.setText('Cancellation requested. The current stage must finish before the workflow stops.')

    def closeEvent(self, event):
        if self.completed:
            event.accept()
        else:
            self.request_close()
            event.ignore()


class OptimusWindow(QMainWindow):
    def __init__(self, backend, *, smoke_test=False):
        super().__init__()
        self.backend, self.smoke_test = backend, smoke_test
        self.busy = False
        self._quitting = False
        self._settings_window = None
        self._sandbox_window = None
        self._setup_wizard = None
        self._worker = None
        self._progress_window = None
        self._cancel = threading.Event()
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.preferences_path = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'Optimus' / 'ui-settings.json'
        try:
            self.appearance = json.loads(self.preferences_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            self.appearance = {}
        try:
            self.workflow_settings = self._settings()
        except Exception:
            self.workflow_settings = {}
        self.config = SimpleNamespace(ui_motion_enabled=self.appearance.get('motion', True) and not bool(self.workflow_settings.get('reduce_motion', False)),
            holographic_orb_enabled=self.appearance.get('orb', True), holographic_orb_x=-1, holographic_orb_y=-1)
        self.setWindowTitle('Optimus: Computer optimization tool')
        self.setWindowIcon(DetailedSettingsWindow._icon())
        self.resize(1280, 820)
        self.setMinimumSize(850, 650)
        self.setStyleSheet(self._qss())
        self.canvas = StarfieldCanvas(motion_allowed(self.config))
        layout = QVBoxLayout(self.canvas)
        layout.setContentsMargins(35, 35, 35, 35)
        layout.addStretch(3)
        brand = QLabel('OPTIMUS')
        brand.setAlignment(Qt.AlignCenter)
        brand.setStyleSheet('font-size:52px;font-weight:800;letter-spacing:11px;color:#1f9cff;background:transparent;')
        layout.addWidget(brand)
        subtitle = QLabel('COMPUTER OPTIMIZATION TOOL')
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setStyleSheet('font-size:12px;letter-spacing:4px;color:#94a3b8;background:transparent;')
        layout.addWidget(subtitle)
        layout.addSpacing(38)
        self.main_buttons = []
        for label, callback, primary in (
            ('1 click Optimize and debloat', self.start_optimization, True),
            ('Settings', self.open_settings, False), ('Sandbox', self.open_sandbox, False)):
            row = QHBoxLayout()
            row.addStretch()
            button = QPushButton(label)
            button.setFixedSize(430, 62)
            button.setCursor(Qt.PointingHandCursor)
            button.setStyleSheet('QPushButton {font-size:16px;font-weight:700;border-radius:10px;padding:14px;'+
                ('background:#1f9cff;color:#07111f;border:1px solid #6ac0ff;' if primary else 'background:#101925;color:#d6e2f0;border:1px solid #33455d;')+
                '} QPushButton:hover {background:' + ('#54b4ff' if primary else '#192b40') + ';} QPushButton:pressed {padding-top:17px;}')
            button.clicked.connect(callback)
            row.addWidget(button)
            row.addStretch()
            layout.addLayout(row)
            layout.addSpacing(10)
            self.main_buttons.append(button)
        layout.addSpacing(16)
        self.status = QLabel('Your configured tools. One local workspace.')
        self.status.setAlignment(Qt.AlignCenter)
        self.status.setWordWrap(True)
        self.status.setStyleSheet('color:#94a3b8;background:transparent;')
        layout.addWidget(self.status)
        layout.addStretch(4)
        self.setCentralWidget(self.canvas)
        self._window_reveal = install_global_window_reveal(self, config=self.config)
        self._holographic_orb = HolographicOrbController(self, self.config)
        self._build_tray()
        QApplication.instance().setQuitOnLastWindowClosed(False)
        QApplication.instance().aboutToQuit.connect(self.shutdown)

    def _qss(self):
        theme = self.appearance.get('theme', 'cyber')
        return build_qss(theme if theme in THEMES else 'cyber')

    def select_page(self, name):
        self.open_settings(page=name)

    def _settings(self):
        method = getattr(self.backend, 'preferences', None)
        return method() if callable(method) else {}

    def open_initial_setup(self, checked=False, *, first_run=False):
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
                analysis_error = None
                # Apply only the explicitly saved read-only sampler choice here.
                method = getattr(self.backend, 'analysis_start' if self.workflow_settings.get('background_analysis_enabled') else 'analysis_stop', None)
                if callable(method):
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
                        elif isinstance(control, QSpinBox):
                            control.setValue(int(value))
                        elif isinstance(control, QComboBox):
                            control.setCurrentText(str(value))
                    finally:
                        control.blockSignals(previous)
                message = 'Setup choices saved. ' + ('Starting the maintenance you selected.' if run_now else 'Maintenance will follow your saved cadence; no run was started now.')
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
        if self._setup_wizard is not None:
            self.status.setText('Finish or cancel the setup questions first.')
            return
        if self.busy:
            self.status.setText('Wait for the active workflow or request cancellation in its progress window.')
            return
        if self._settings_window is None:
            self._settings_window = DetailedSettingsWindow(self.backend, smoke_test=self.smoke_test)
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
        description = QLabel('The main Optimize button runs the modules enabled here. Application removals are off until you choose them. Existing Codex priority and its watcher stay unchanged unless its optional profile is enabled.')
        description.setWordWrap(True)
        layout.addWidget(description)
        try:
            settings = self._settings()
        except Exception as exc:
            settings = {}
            window.status.setText('Workflow settings unavailable: ' + str(exc))
        self.workflow_settings = settings
        controls = {}
        grid = QGridLayout()
        intensity = QComboBox()
        intensity.addItems(['Low', 'Standard', 'High'])
        intensity.setCurrentText(str(settings.get('intensity', 'Standard')))
        controls['intensity'] = intensity
        grid.addWidget(QLabel('Intensity'), 0, 0)
        grid.addWidget(intensity, 0, 1)
        age = QSpinBox()
        age.setRange(1,365)
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
        labels = [('cleanup_enabled','Clean reviewed temporary files'),('browser_cache_enabled','Include inactive browser caches'),
            ('consumer_debloat_enabled','Remove selected consumer bloatware'),('m365_debloat_enabled','Run Microsoft 365 debloat'),
            ('outlook_debloat_enabled','Run Outlook debloat'),('edge_debloat_enabled','Run Edge debloat'),
            ('codex_priority_enabled','Apply optional Codex priority profile'),('background_analysis_enabled','Enable background resource analysis'),
            ('reduce_motion','Reduce window and starfield motion')]
        for index,(key,label) in enumerate(labels):
            checkbox=QCheckBox(label)
            checkbox.setChecked(bool(settings.get(key,False)))
            if 'debloat' in key:
                checkbox.setStyleSheet('color:#ffad73;')
                checkbox.setToolTip('Opt in to this removal stage. Review the Debloat page and target list before enabling it.')
            controls[key]=checkbox
            toggles.addWidget(checkbox,index//2,index%2)
        layout.addLayout(toggles)
        note=QLabel('Low: conservative cleanup age (30 days). Standard: 7 days. High: 1 day plus only the modules you select. Security software, blanket driver removal and application deletion are not enabled by an intensity preset.')
        note.setWordWrap(True)
        layout.addWidget(note)
        intensity.currentTextChanged.connect(lambda value: age.setValue({'Low':30,'Standard':7,'High':1}[value]))
        setup_button=QPushButton('Run setup questions again')
        setup_button.clicked.connect(self.open_initial_setup)
        layout.addWidget(setup_button)
        save=QPushButton('Save one-click configuration')
        def save_configuration():
            value=dict(self._settings())
            for key,control in controls.items():
                value[key]=control.isChecked() if isinstance(control,QCheckBox) else control.value() if isinstance(control,QSpinBox) else control.currentText()
            def saved(result):
                self.workflow_settings=result if isinstance(result,dict) else value
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

    def apply_appearance(self, settings):
        self.appearance = settings
        self.config.ui_motion_enabled = bool(settings.get('motion', True)) and not bool(getattr(self,'workflow_settings',{}).get('reduce_motion',False))
        self.config.holographic_orb_enabled = bool(settings.get('orb', True))
        self.canvas.motion = motion_allowed(self.config)
        self.canvas.sync_motion()
        self._holographic_orb.sync_config()
        self.setStyleSheet(self._qss())

    def open_sandbox(self, checked=False):
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
        if self.smoke_test:
            self.status.setText('Optimization is disabled during a UI smoke test.')
            return
        if self.busy or (self._settings_window is not None and self._settings_window.busy):
            self.status.setText('Wait for the current task before starting optimization.')
            return
        try:
            settings = self._settings()
        except Exception as exc:
            QMessageBox.warning(self, 'Optimus configuration', str(exc))
            return
        if self._setup_wizard is not None:
            self.status.setText('Finish or cancel the setup questions first.')
            return
        from .automation import CURRENT_SETUP_VERSION
        if int(settings.get('setup_version', 0)) < CURRENT_SETUP_VERSION:
            self.open_initial_setup(first_run=True)
            return
        if not callable(getattr(self.backend, 'optimize', None)):
            QMessageBox.warning(self, 'Optimus', 'The workflow engine is unavailable. No action was taken.')
            return
        self.busy = True
        self._cancel = threading.Event()
        self._progress_window = OptimizationProgressWindow(self, self._cancel)
        self._progress_window.show()
        self._worker = WorkflowWorker(self.backend, settings, self._cancel)
        self._worker.signals.progress.connect(self._progress_window.update_progress)
        self._worker.signals.result.connect(self._workflow_result)
        self._worker.signals.error.connect(self._workflow_error)
        self._worker.signals.finished.connect(self._workflow_finished)
        self.main_buttons[0].setEnabled(False)
        self.pool.start(self._worker)

    @Slot(object)
    def _workflow_result(self, result):
        self._progress_window.finish(result)
        self.status.setText('Workflow returned results. Review completed, skipped and failed stages in the progress window.')

    @Slot(str)
    def _workflow_error(self, error):
        self._progress_window.finish(None, error=error)
        self.status.setText('Workflow stopped: ' + error)

    @Slot()
    def _workflow_finished(self):
        self.busy = False
        self.main_buttons[0].setEnabled(True)
        self._worker = None

    def _build_tray(self):
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        self.tray.setToolTip('Optimus: Computer optimization tool')
        menu = QMenu()
        show = QAction('Open Optimus', self)
        show.triggered.connect(self._holographic_orb.restore_main)
        quit_action = QAction('Quit Optimus', self)
        quit_action.triggered.connect(self.quit_application)
        menu.addAction(show)
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self._holographic_orb.restore_main() if reason == QSystemTrayIcon.Trigger else None)
        if not self.smoke_test:
            self.tray.show()

    def changeEvent(self, event):
        super().changeEvent(event)
        if hasattr(self, 'canvas'):
            QTimer.singleShot(0, self.canvas.sync_motion)

    def closeEvent(self, event):
        if self._quitting or self.smoke_test:
            event.accept()
        elif self._holographic_orb.enabled() and not self.property('disable_orb'):
            event.ignore()
            self._holographic_orb.collapse_window(self)
        elif self.busy:
            self.status.setText('The workflow is still active. Cancel from its progress window and wait for the current stage.')
            event.ignore()
        else:
            if self.quit_application():
                event.accept()
            else:
                event.ignore()

    def quit_application(self):
        if self._setup_wizard is not None:
            self.status.setText('Finish or cancel the setup questions before exiting.')
            return False
        if self.busy or (self._settings_window is not None and self._settings_window.busy):
            self.status.setText('An operation is active. Wait for completion or cancel at a workflow stage boundary before exiting.')
            if self._progress_window is not None:
                self._progress_window.show()
                self._progress_window.raise_()
            return False
        self._quitting = True
        self.shutdown()
        QApplication.instance().quit()
        return True

    def shutdown(self):
        if getattr(self, '_presentation_shutdown', False):
            return
        self._presentation_shutdown = True
        self.canvas.timer.stop()
        self._holographic_orb.shutdown()
        self._window_reveal.shutdown()
        self.tray.hide()
        if self._settings_window is not None:
            self._settings_window.shutdown()


def create_window(backend, *, smoke_test=False):
    return OptimusWindow(backend, smoke_test=smoke_test)
