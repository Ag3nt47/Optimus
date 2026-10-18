"""Personal OneDrive connection and metadata-only duplicate review panel."""
from __future__ import annotations
from urllib.parse import urlsplit
from PySide6.QtCore import Qt,QTimer,QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QWidget,QVBoxLayout,QHBoxLayout,QFormLayout,QLabel,QLineEdit,QPushButton,QTableWidget,QTableWidgetItem,QAbstractItemView,QApplication,QPlainTextEdit,QHeaderView


class CloudPanel(QWidget):
    def __init__(self,window):
        super().__init__(window)
        self.window=window;self._session=None;self._uri=None;self._plan=None;self._rows=[]
        self._timer=QTimer(self);self._timer.setSingleShot(True);self._timer.timeout.connect(self._poll)
        layout=QVBoxLayout(self);layout.setContentsMargins(0,0,0,0);layout.setSpacing(10)
        intro=QLabel('Review duplicate candidates in your personal OneDrive using file metadata. File contents are not downloaded, synced placeholders are not opened, and nothing is selected automatically. Google Drive, Dropbox, business drives and shared files are not supported.')
        intro.setWordWrap(True);layout.addWidget(intro)
        form=QFormLayout();self.client=QLineEdit();self.client.setPlaceholderText('Your own registered Microsoft public-client application ID')
        self.tenant=QLineEdit('consumers');form.addRow('Application client ID',self.client);form.addRow('Tenant',self.tenant);layout.addLayout(form)
        setup=QLabel('Use a public-client Microsoft app registration that permits personal Microsoft accounts and device-code sign-in, with delegated Files.ReadWrite and User.Read permissions. Optimus does not supply or borrow another application\'s client ID.')
        setup.setWordWrap(True);layout.addWidget(setup)
        config_bar=QHBoxLayout()
        for label,callback in (('Save connection settings',self._configure),('Connect personal OneDrive',self._connect),('Disconnect',self._logout),('Refresh status',self.refresh)):
            button=QPushButton(label);button.clicked.connect(callback);config_bar.addWidget(button)
        layout.addLayout(config_bar)
        self.status=QLabel('Not connected. Save your own application registration before connecting.');self.status.setWordWrap(True);layout.addWidget(self.status)
        code_bar=QHBoxLayout();code_bar.addWidget(QLabel('Sign-in code'));self.code=QLineEdit();self.code.setReadOnly(True);self.code.setMaximumWidth(210);code_bar.addWidget(self.code)
        copy=QPushButton('Copy code');copy.clicked.connect(lambda:QApplication.clipboard().setText(self.code.text()));code_bar.addWidget(copy)
        website=QPushButton('Open Microsoft sign-in');website.clicked.connect(self._open_signin);code_bar.addWidget(website)
        check=QPushButton('Check sign-in');check.clicked.connect(self._poll);code_bar.addWidget(check);code_bar.addStretch();layout.addLayout(code_bar)
        scan_bar=QHBoxLayout();scan_bar.addWidget(QLabel('Folder item ID'))
        self.folder=QLineEdit('root');self.folder.setPlaceholderText('root, or an owned personal folder item ID');self.folder.textChanged.connect(self._invalidate);scan_bar.addWidget(self.folder,1)
        scan=QPushButton('Scan metadata');scan.clicked.connect(self._scan);scan_bar.addWidget(scan)
        self.recycle=QPushButton('Recycle selected file');self.recycle.setEnabled(False);self.recycle.clicked.connect(self._recycle);scan_bar.addWidget(self.recycle);layout.addLayout(scan_bar)
        self.table=QTableWidget(0,5);self.table.setHorizontalHeaderLabels(['Provider hash','File','Folder/path','Size','State'])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows);self.table.setSelectionMode(QAbstractItemView.SingleSelection);self.table.setEditTriggers(QAbstractItemView.NoEditTriggers);self.table.itemSelectionChanged.connect(self._selection)
        self.table.horizontalHeader().setSectionResizeMode(2,QHeaderView.Stretch);layout.addWidget(self.table,1)
        self.details=QPlainTextEdit();self.details.setReadOnly(True);self.details.setMaximumHeight(125);self.details.setPlaceholderText('A metadata scan lists candidates only. Same size and provider hash do not prove collision-free, byte-for-byte equivalence.');layout.addWidget(self.details)
        note=QLabel('Each confirmation recycles one file while revalidating an observed retained copy. Restore through the OneDrive website recycle bin. Optimus never empties that bin or performs a bulk cloud delete. Tokens are stored encrypted for your Windows account.')
        note.setWordWrap(True);layout.addWidget(note)
    def refresh(self):self.window._run('cloud_status',done=self._show_status)
    def _show_status(self,result):
        self.client.setText(result.get('clientId',''));self.tenant.setText(result.get('tenant','consumers'))
        self.status.setText('Personal OneDrive connected.' if result.get('connected') else ('Registration saved; connect when ready.' if result.get('configured') else 'Configure your own application registration to connect.'))
    def _configure(self):
        self._timer.stop();self._session=None;self._invalidate()
        self.window._run('cloud_configure',self.client.text().strip(),self.tenant.text().strip() or 'consumers',done=self._show_status)
    def _connect(self):
        self._timer.stop();self._session=None;self._invalidate()
        self.window._run('cloud_connect',done=self._device)
    def _device(self,response):
        self._session=response['sessionId'];self._uri=response['verificationUri'];self.code.setText(response['userCode'])
        self.status.setText('Open the Microsoft sign-in website and enter this code. Optimus never asks for your Microsoft password.')
        self._timer.start(max(5,int(response.get('interval',5)))*1000)
    def _open_signin(self):
        if not self._uri:return
        value=urlsplit(self._uri)
        if value.scheme!='https' or value.hostname not in ('microsoft.com','www.microsoft.com','login.microsoftonline.com','login.live.com') or value.username or value.password:
            self.window._error('An unexpected Microsoft sign-in URL was blocked.');return
        QDesktopServices.openUrl(QUrl(self._uri))
    def _poll(self):
        if not self._session:return
        if getattr(self.window,'busy',False):self._timer.start(5000);return
        self.window._run('cloud_poll',self._session,done=self._polled)
    def _polled(self,response):
        state=response.get('state')
        if state=='pending':self._timer.start(max(5,int(response.get('retryAfter',5)))*1000);return
        self._timer.stop();self._session=None;self.code.clear();self._uri=None
        if state=='connected':self._show_status(response.get('status',{'connected':True,'configured':True,'clientId':self.client.text(),'tenant':self.tenant.text()}))
        else:self.status.setText('Sign-in expired or was declined. Start a new connection only when you are ready.')
    def _logout(self):
        self._timer.stop();self._session=None;self._uri=None;self.code.clear();self._invalidate();self.table.setRowCount(0)
        self.window._run('cloud_logout',done=lambda response:self.status.setText('Disconnected. Optimus removed only its local encrypted token; Microsoft account consent is managed separately.'))
    def _invalidate(self,*args):self._plan=None;self.recycle.setEnabled(False)
    def _scan(self):
        self._invalidate();self.window._run('cloud_scan',self.folder.text().strip() or 'root',done=self._show_scan)
    def _show_scan(self,plan):
        self._plan=plan;self._rows=[]
        for group in plan.get('groups',[]):
            for item in group.get('items',[]):self._rows.append(dict(item,state='Candidate'))
        self.table.setRowCount(len(self._rows))
        for index,item in enumerate(self._rows):
            values=[item.get('algorithm',''),item.get('name',''),item.get('path',''),f"{item.get('size',0)/1024/1024:.2f} MB",item['state']]
            for column,value in enumerate(values):self.table.setItem(index,column,QTableWidgetItem(str(value)))
        self.table.clearSelection();self.table.setCurrentCell(-1,-1);self.recycle.setEnabled(False)
        coverage='Complete within selected scope' if plan.get('complete') else 'Partial scan: item or time limit reached'
        self.details.setPlainText(f"{coverage}. Examined: {plan.get('examined',0):,}; skipped or unsupported: {plan.get('skipped',0):,}; duplicate groups: {len(plan.get('groups',[]))}. Preview expires after 15 minutes.\n\n{plan.get('note','')}")
    def _selected(self):
        selected=self.table.selectionModel().selectedRows()
        return self._rows[selected[0].row()] if len(selected)==1 and selected[0].row()<len(self._rows) else None
    def _selection(self):
        item=self._selected();remaining=[]
        if item:remaining=[other for other in self._rows if other.get('groupId')==item.get('groupId') and other.get('state')=='Candidate']
        self.recycle.setEnabled(bool(self._plan and item and item['state']=='Candidate' and len(remaining)>1))
    def _recycle(self):
        item=self._selected()
        if not self._plan or not item or item['state']!='Candidate':return
        details={'file':item.get('name'),'path':item.get('path'),'bytes':item.get('size'),'providerHash':item.get('algorithm'),'recovery':'Restore from the personal OneDrive website recycle bin. Concurrent remote changes cannot be made atomic across files.'}
        if not self.window._confirm('Recycle one personal OneDrive file','Send this one selected duplicate candidate to the OneDrive recycle bin? Optimus will revalidate the file and an observed retained copy. Provider hashes are not a byte-for-byte comparison.',details=details,phrase='RECYCLE'):return
        self.recycle.setEnabled(False);self.window._run('cloud_recycle',{'planId':self._plan['planId'],'selectedIds':[item['id']],'confirmed':True},done=self._recycled)
    def _recycled(self,result):
        for index,item in enumerate(self._rows):
            if item['id']==result.get('id'):item['state']='Recycled';self.table.setItem(index,4,QTableWidgetItem('Recycled'))
        self.status.setText(result.get('note','Sent to the personal OneDrive recycle bin.'))
        self._selection()
