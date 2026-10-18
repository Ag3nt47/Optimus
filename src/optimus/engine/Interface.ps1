function New-SCMUIGrid {
    param([object[]]$Columns)
    $grid = New-Object System.Windows.Forms.DataGridView
    $grid.Dock = 'Fill'; $grid.ReadOnly = $true; $grid.AllowUserToAddRows = $false
    $grid.AllowUserToDeleteRows = $false; $grid.AllowUserToOrderColumns = $true
    $grid.AutoGenerateColumns = $true; $grid.AutoSizeColumnsMode = 'Fill'
    $grid.SelectionMode = 'FullRowSelect'; $grid.MultiSelect = $false
    $grid.RowHeadersVisible = $false; $grid.BackgroundColor = [System.Drawing.Color]::White
    $grid.BorderStyle = 'None'; $grid.EnableHeadersVisualStyles = $false
    $grid.ColumnHeadersDefaultCellStyle.BackColor = [System.Drawing.Color]::FromArgb(231,237,245)
    $grid.ColumnHeadersDefaultCellStyle.ForeColor = [System.Drawing.Color]::FromArgb(25,40,60)
    $grid.ColumnHeadersHeight = 32; $grid.RowTemplate.Height = 25
    $grid.AlternatingRowsDefaultCellStyle.BackColor = [System.Drawing.Color]::FromArgb(246,249,252)
    $grid.Tag = $Columns
    $grid.add_DataBindingComplete({ param($sender,$eventArgs)
        if ($sender.Columns.Contains('__Record')) { $sender.Columns['__Record'].Visible = $false }
    })
    return $grid
}
function Set-SCMUIRows {
    param($Grid,[object[]]$Rows)
    $table = New-Object System.Data.DataTable
    foreach ($spec in $Grid.Tag) {
        $type = if ($spec.Count -gt 2) { $spec[2] } else { [string] }
        [void]$table.Columns.Add([string]$spec[0],$type)
    }
    [void]$table.Columns.Add('__Record',[object])
    foreach ($record in $Rows) {
        if ($null -eq $record) { continue }
        $row = $table.NewRow()
        foreach ($spec in $Grid.Tag) {
            $value = $record.([string]$spec[1])
            if ($null -ne $value) {
                if ($value -is [array]) { $value = $value -join ', ' }
                try { $row[[string]$spec[0]] = $value } catch { }
            }
        }
        $row['__Record'] = $record; [void]$table.Rows.Add($row)
    }
    $Grid.DataSource = $table
    if ($Grid.Columns.Contains('__Record')) { $Grid.Columns['__Record'].Visible = $false }
    foreach ($spec in $Grid.Tag) {
        if ($spec.Count -gt 2 -and $spec[2] -eq [double] -and $Grid.Columns.Contains($spec[0])) {
            $Grid.Columns[$spec[0]].DefaultCellStyle.Format = 'N1'
            $Grid.Columns[$spec[0]].DefaultCellStyle.Alignment = 'MiddleRight'
        }
    }
}
function Get-SCMUISelected {
    param($Grid)
    if ($null -eq $Grid.CurrentRow -or $null -eq $Grid.CurrentRow.DataBoundItem) { return $null }
    return $Grid.CurrentRow.DataBoundItem.Row['__Record']
}
function New-SCMUIToolbar {
    $panel = New-Object System.Windows.Forms.FlowLayoutPanel
    $panel.Dock = 'Top'; $panel.Height = 43; $panel.Padding = New-Object System.Windows.Forms.Padding(6)
    $panel.WrapContents = $false; $panel.AutoScroll = $true
    return $panel
}
function Add-SCMUIButton {
    param($Parent,[string]$Text,[scriptblock]$OnClick)
    $button = New-Object System.Windows.Forms.Button
    $button.Text = $Text; $button.AutoSize = $true; $button.Height = 29
    $button.Padding = New-Object System.Windows.Forms.Padding(6,0,6,0)
    $button.FlatStyle = 'System'; $button.add_Click($OnClick)
    [void]$Parent.Controls.Add($button)
    return $button
}
function Add-SCMUILabel {
    param($Parent,[string]$Text)
    $label = New-Object System.Windows.Forms.Label
    $label.Text = $Text; $label.AutoSize = $true
    $label.Margin = New-Object System.Windows.Forms.Padding(5,7,8,0)
    [void]$Parent.Controls.Add($label)
    return $label
}
function Show-SCMUIError {
    param([string]$Message)
    if ($script:SCMUI.SmokeTest) { throw $Message }
    [void][System.Windows.Forms.MessageBox]::Show($script:SCMUI.Form,$Message,'Computer Management','OK','Error')
}
function Set-SCMUIBusy {
    param([bool]$Value)
    $script:SCMUI.Busy = $Value
    $script:SCMUI.Form.UseWaitCursor = $Value
    foreach ($control in $script:SCMUI.Form.Controls) { $control.Enabled = -not $Value }
}
function Invoke-SCMUIRead {
    param([scriptblock]$Operation)
    if ($script:SCMUI.Busy) { return }
    try {
        Set-SCMUIBusy $true
        & $Operation
    } catch { Show-SCMUIError $_.Exception.Message }
    finally { Set-SCMUIBusy $false }
}
function Invoke-SCMUIChange {
    param($Record,[string]$Prompt,[scriptblock]$Action,[switch]$AllowProtected)
    if ($null -eq $Record) { Show-SCMUIError 'Select one row first.'; return }
    if (-not $AllowProtected -and ($Record.IsProtected -or $Record.Protected)) {
        $reason = if ($Record.ProtectionReason) { $Record.ProtectionReason } else { $Record.Reason }
        Show-SCMUIError ('This entry is protected. ' + $reason); return
    }
    if ($Record.PSObject.Properties['CanManage'] -and -not $Record.CanManage) {
        Show-SCMUIError ([string]$Record.Reason); return
    }
    $answer = [System.Windows.Forms.MessageBox]::Show($script:SCMUI.Form,$Prompt,'Confirm change','YesNo','Warning','Button2')
    if ($answer -ne [System.Windows.Forms.DialogResult]::Yes) { return }
    Invoke-SCMUIRead {
        & $Action $Record
        Update-SCMUIAudit
    }
}
function Update-SCMUIProcessFilter {
    $filter = $script:SCMUI.Search.Text.Trim()
    $rows = @($script:SCMUI.Audit.Processes | Where-Object {
        ($script:SCMUI.ShowProtected.Checked -or -not $_.IsProtected) -and
        ([string]::IsNullOrEmpty($filter) -or
            (($_.Name,$_.AppName,$_.RootProgram,$_.ParentName,$_.Path,$_.LaunchExplanation -join ' ') -like ('*' + [System.Management.Automation.WildcardPattern]::Escape($filter) + '*')))
    })
    $rows = @($rows | Sort-Object MemoryMB -Descending)
    Set-SCMUIRows $script:SCMUI.ProcessGrid $rows
    $script:SCMUI.ProcessCount.Text = '{0} shown / {1} total' -f $rows.Count,@($script:SCMUI.Audit.Processes).Count
}
function Update-SCMUIServiceFilter {
    $rows = @($script:SCMUI.Audit.Services | Where-Object { $script:SCMUI.ServiceProtected.Checked -or -not $_.Protected })
    Set-SCMUIRows $script:SCMUI.ServiceGrid $rows
}
function Set-SCMUIProcessSummary {
    $r = Get-SCMUISelected $script:SCMUI.ProcessGrid
    if ($null -eq $r) { $script:SCMUI.Details.Text = 'Select a process to see its owner program, launch evidence, and path.'; return }
    $script:SCMUI.Details.Text = ('PROGRAM: {0}' -f $r.AppName) + "`r`n" +
        ('PROGRAM GROUP: {0}    PROCESS: {1} (PID {2})    PARENT: {3}' -f $r.RootProgram,$r.Name,$r.ProcessId,$r.ParentName) + "`r`n" +
        ('WHY IT IS RUNNING: {0}' -f $r.LaunchExplanation) + "`r`n" +
        ('PATH: {0}' -f $r.Path) + "`r`n" +
        ('STARTUP / TASK EVIDENCE: {0}' -f ($r.PersistenceEvidence -join '; ')) + "`r`n" +
        ('PROTECTION: {0}' -f $r.ProtectionReason) + "`r`n`r`nLaunch evidence explains observable links; it does not prove an application is unnecessary. Memory is a snapshot and CPU is sampled."
}
function Update-SCMUIAudit {
    $ownsBusy = -not $script:SCMUI.Busy
    if ($ownsBusy) { Set-SCMUIBusy $true }
    try {
    $script:SCMUI.Status.Text = 'Reading processes, startup entries, services, scheduled tasks, and installed programs...'
    [System.Windows.Forms.Application]::DoEvents()
    $audit = Get-SCMFullAudit
    $script:SCMUI.Audit = $audit
    Update-SCMUIProcessFilter
    Set-SCMUIRows $script:SCMUI.AppGrid @($audit.AppTotals)
    Set-SCMUIRows $script:SCMUI.StartupGrid @($audit.Startup)
    Update-SCMUIServiceFilter
    Set-SCMUIRows $script:SCMUI.TaskGrid @($audit.ScheduledTasks)
    Set-SCMUIRows $script:SCMUI.InstalledGrid @($audit.InstalledApps)
    Set-SCMUIRows $script:SCMUI.PowerGrid @($audit.PowerPlans)
    Set-SCMUIRows $script:SCMUI.ChangeGrid @(Get-SCMChanges)
    $script:SCMUI.System.Text = ($audit.System | Format-List | Out-String).Trim()
    $errors = @($audit.InventoryErrors | Where-Object { $null -ne $_ })
    $script:SCMUI.Status.Text = ('Updated {0}. {1} processes. {2} inventory notes. Manual refresh only.' -f (Get-Date -Format 'HH:mm:ss'),@($audit.Processes).Count,$errors.Count)
    $script:SCMUI.Notes.Text = if ($errors.Count) { ($errors | Out-String).Trim() } else { 'All inventory sources completed. Some protected-process paths may still be unavailable.' }
    Set-SCMUIProcessSummary
    } finally { if ($ownsBusy) { Set-SCMUIBusy $false } }
}
function Show-SCMInterface {
    param([switch]$SmokeTest)
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    [System.Windows.Forms.Application]::EnableVisualStyles()
    $script:SCMUI = @{ SmokeTest = [bool]$SmokeTest; Audit = $null; SmokeError = $null; Busy = $false }
    $form = New-Object System.Windows.Forms.Form
    $script:SCMUI.Form = $form
    $form.Text = 'Specialized Computer Management | ' + $script:SCMVersion
    $form.Width = 1360; $form.Height = 880; $form.MinimumSize = New-Object System.Drawing.Size(1050,700)
    $form.StartPosition = 'CenterScreen'; $form.Font = New-Object System.Drawing.Font('Segoe UI',9)
    $form.BackColor = [System.Drawing.Color]::FromArgb(244,247,251)
    if ($SmokeTest) { $form.Opacity = 0; $form.ShowInTaskbar = $false }
    $top = New-SCMUIToolbar; $top.Height = 47
    [void](Add-SCMUIButton $top 'Refresh all' { Invoke-SCMUIRead { Update-SCMUIAudit } })
    [void](Add-SCMUIButton $top 'Export HTML + CSV audit' { Invoke-SCMUIRead {
        if ($null -eq $script:SCMUI.Audit) { Update-SCMUIAudit }
        $picker = New-Object System.Windows.Forms.FolderBrowserDialog
        $picker.Description = 'Choose a folder for a new timestamped audit directory.'
        $picker.SelectedPath = $script:SCMDataRoot
        if ($picker.ShowDialog($script:SCMUI.Form) -eq 'OK') {
            $destination = Join-Path $picker.SelectedPath ('SCM-Audit-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
            $result = Export-SCMAudit -Snapshot $script:SCMUI.Audit -Destination $destination
            $script:SCMUI.Status.Text = 'Audit saved to ' + $result
            Start-Process -FilePath (Join-Path $result 'report.html')
        }
        $picker.Dispose()
    } })
    [void](Add-SCMUIButton $top 'Resource Monitor' { Invoke-SCMUIRead { Start-Process -FilePath 'resmon.exe' } })
    [void](Add-SCMUIButton $top 'Run as administrator' { Invoke-SCMUIRead { Start-SCMUIElevated } })
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    [void](Add-SCMUILabel $top $(if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { 'Administrator' } else { 'Standard user - some details/actions require elevation' }))
    $status = New-Object System.Windows.Forms.Label
    $status.Dock = 'Bottom'; $status.Height = 27; $status.Padding = New-Object System.Windows.Forms.Padding(9,5,0,0)
    $status.Text = 'Ready'; $script:SCMUI.Status = $status
    $tabs = New-Object System.Windows.Forms.TabControl; $tabs.Dock = 'Fill'
    $form.Controls.Add($tabs); $form.Controls.Add($top); $form.Controls.Add($status)
    $pages = @{}
    foreach ($name in @('Processes','Program totals','Startup','Services','Scheduled tasks','Installed programs','Power and maintenance','Changes and restore')) {
        $page = New-Object System.Windows.Forms.TabPage; $page.Text = $name
        $page.BackColor = [System.Drawing.Color]::White; $page.Padding = New-Object System.Windows.Forms.Padding(6)
        $tabs.TabPages.Add($page); $pages[$name] = $page
    }
    $script:SCMUI.Tabs = $tabs
    # Process list and launch explanation.
    $processBar = New-SCMUIToolbar; $processBar.Height = 40
    [void](Add-SCMUILabel $processBar 'Search:')
    $search = New-Object System.Windows.Forms.TextBox; $search.Width = 220
    $search.Margin = New-Object System.Windows.Forms.Padding(0,3,8,0); $processBar.Controls.Add($search)
    $script:SCMUI.Search = $search
    $showProtected = New-Object System.Windows.Forms.CheckBox
    $showProtected.Text = 'Show protected Windows / essential processes'; $showProtected.AutoSize = $true
    $showProtected.Margin = New-Object System.Windows.Forms.Padding(4,6,9,0)
    $processBar.Controls.Add($showProtected); $script:SCMUI.ShowProtected = $showProtected
    $script:SCMUI.ProcessCount = Add-SCMUILabel $processBar ''
    $processActions = New-SCMUIToolbar
    [void](Add-SCMUIButton $processActions 'Full process details' { Invoke-SCMUIRead {
        $r = Get-SCMUISelected $script:SCMUI.ProcessGrid
        if ($null -eq $r) { throw 'Select a process first.' }
        $details = Get-SCMProcessDetails -Record $r
        $script:SCMUI.Details.Text = ($details | ConvertTo-Json -Depth 8)
    } })
    [void](Add-SCMUIButton $processActions 'Close normally' {
        $r = Get-SCMUISelected $script:SCMUI.ProcessGrid
        Invoke-SCMUIChange -Record $r -Prompt ('Ask {0} (PID {1}) to close its main window? Save your work first. Programs without a main window require Force end.' -f $r.Name,$r.ProcessId) -Action { param($r) Stop-SCMSelectedProcess -Record $r }
    })
    [void](Add-SCMUIButton $processActions 'Force end' {
        $r = Get-SCMUISelected $script:SCMUI.ProcessGrid
        Invoke-SCMUIChange -Record $r -Prompt ('Force end only {0} (PID {1})? Unsaved work can be lost. Use this only when normal End fails.' -f $r.Name,$r.ProcessId) -Action { param($r) Stop-SCMSelectedProcess -Record $r -Force }
    })
    [void](Add-SCMUILabel $processActions 'Priority:')
    $priority = New-Object System.Windows.Forms.ComboBox
    $priority.DropDownStyle = 'DropDownList'; $priority.Width = 110
    [void]$priority.Items.AddRange(@('Idle','BelowNormal','Normal','AboveNormal')); $priority.SelectedIndex = 2
    $processActions.Controls.Add($priority); $script:SCMUI.Priority = $priority
    [void](Add-SCMUIButton $processActions 'Apply priority' {
        $r = Get-SCMUISelected $script:SCMUI.ProcessGrid
        Invoke-SCMUIChange -Record $r -Prompt ('Set {0} priority to {1}? This affects CPU scheduling until the process exits.' -f $r.Name,$script:SCMUI.Priority.Text) -Action { param($r) Set-SCMProcessPriority -Record $r -Priority $script:SCMUI.Priority.Text }
    })
    [void](Add-SCMUILabel $processActions 'CPU cores:')
    $cores = New-Object System.Windows.Forms.TextBox; $cores.Width = 80
    $processActions.Controls.Add($cores); $script:SCMUI.Cores = $cores
    [void](Add-SCMUIButton $processActions 'Apply cores' {
        $r = Get-SCMUISelected $script:SCMUI.ProcessGrid
        Invoke-SCMUIChange -Record $r -Prompt ('Limit {0} to logical cores {1}? Enter zero-based core numbers, for example 0,1. Limiting cores can reduce performance.' -f $r.Name,$script:SCMUI.Cores.Text) -Action { param($r) Set-SCMProcessAffinity -Record $r -Cores $script:SCMUI.Cores.Text }
    })
    $split = New-Object System.Windows.Forms.SplitContainer
    $split.Size = New-Object System.Drawing.Size(1200,620)
    $split.Dock = 'Fill'; $split.Orientation = 'Horizontal'; $split.SplitterDistance = 350
    $grid = New-SCMUIGrid @(@('Process','Name'),@('PID','ProcessId',[int]),@('Program','AppName'),@('Program group','RootProgram'),@('CPU %','CpuPercent',[double]),@('RAM MB','MemoryMB',[double]),@('Private MB','PrivateMB',[double]),@('Parent','ParentName'),@('Role','Role'))
    $script:SCMUI.ProcessGrid = $grid; $split.Panel1.Controls.Add($grid)
    $detailsBox = New-Object System.Windows.Forms.TextBox
    $detailsBox.Multiline = $true; $detailsBox.ReadOnly = $true; $detailsBox.Dock = 'Fill'
    $detailsBox.ScrollBars = 'Both'; $detailsBox.WordWrap = $true
    $detailsBox.BackColor = [System.Drawing.Color]::FromArgb(246,249,252)
    $script:SCMUI.Details = $detailsBox; $split.Panel2.Controls.Add($detailsBox)
    $pages['Processes'].Controls.Add($split); $pages['Processes'].Controls.Add($processActions); $pages['Processes'].Controls.Add($processBar)
    $search.add_TextChanged({ if ($script:SCMUI.Audit) { Update-SCMUIProcessFilter } })
    $showProtected.add_CheckedChanged({ if ($script:SCMUI.Audit) { Update-SCMUIProcessFilter } })
    $grid.add_SelectionChanged({ Set-SCMUIProcessSummary })
    # Program totals help locate multi-process applications.
    $grid = New-SCMUIGrid @(@('Program','Program'),@('Processes','Processes',[int]),@('RAM MB','WorkingSetMB',[double]),@('Private MB','PrivateMB',[double]),@('CPU %','CPUPercent',[double]),@('Executables','Executables'))
    $script:SCMUI.AppGrid = $grid; $pages['Program totals'].Controls.Add($grid)
    $bar = New-SCMUIToolbar
    [void](Add-SCMUILabel $bar 'Click a column to sort. Shared pages can appear in multiple processes; RAM totals are approximate.')
    $pages['Program totals'].Controls.Add($bar)
    # Startup entries are backed up by the core before changes.
    $grid = New-SCMUIGrid @(@('Name','Name'),@('Entry present','Enabled',[bool]),@('Location','Location'),@('Command','Command'),@('Manageable','CanManage',[bool]),@('Reason','Reason'))
    $script:SCMUI.StartupGrid = $grid; $pages['Startup'].Controls.Add($grid)
    $bar = New-SCMUIToolbar
    [void](Add-SCMUIButton $bar 'Disable selected' {
        $r = Get-SCMUISelected $script:SCMUI.StartupGrid
        Invoke-SCMUIChange -Record $r -Prompt ('Disable startup entry {0}? The program stays installed and can still be opened manually.' -f $r.Name) -Action { param($r)
            $undo = Set-SCMStartupState -Entry $r -Enabled $false
            New-SCMChange -Kind 'Startup' -Target $r.Name -Undo $undo | Out-Null
        }
    })
    [void](Add-SCMUIButton $bar 'Enable selected' {
        $r = Get-SCMUISelected $script:SCMUI.StartupGrid
        Invoke-SCMUIChange -Record $r -Prompt ('Enable startup entry {0}?' -f $r.Name) -Action { param($r)
            $undo = Set-SCMStartupState -Entry $r -Enabled $true
            New-SCMChange -Kind 'Startup' -Target $r.Name -Undo $undo | Out-Null
        }
    })
    [void](Add-SCMUIButton $bar 'Delete startup entry' {
        $r = Get-SCMUISelected $script:SCMUI.StartupGrid
        Invoke-SCMUIChange -Record $r -Prompt ('Delete startup entry {0}? This removes only its startup registration. An undo record is saved.' -f $r.Name) -Action { param($r)
            $undo = Remove-SCMStartupEntry -Entry $r
            New-SCMChange -Kind 'Startup' -Target $r.Name -Undo $undo | Out-Null
        }
    })
    [void](Add-SCMUIButton $bar 'Windows Startup settings' { Invoke-SCMUIRead { Start-Process 'ms-settings:startupapps' } })
    $pages['Startup'].Controls.Add($bar)
    # Service controls deliberately require explicit per-entry confirmation.
    $grid = New-SCMUIGrid @(@('Service','Name'),@('Display name','DisplayName'),@('State','State'),@('Start mode','StartMode'),@('Program / path','PathName'),@('PID','ProcessId',[int]),@('Protected','Protected',[bool]))
    $script:SCMUI.ServiceGrid = $grid; $pages['Services'].Controls.Add($grid)
    $bar = New-SCMUIToolbar
    [void](Add-SCMUIButton $bar 'Stop selected' { Invoke-SCMUIServiceAction 'Stop' })
    [void](Add-SCMUIButton $bar 'Set to manual start' { Invoke-SCMUIServiceAction 'Manual' })
    [void](Add-SCMUIButton $bar 'Disable selected' { Invoke-SCMUIServiceAction 'Disabled' })
    [void](Add-SCMUIButton $bar 'Open Services' { Invoke-SCMUIRead { Start-Process 'services.msc' } })
    $showServices = New-Object System.Windows.Forms.CheckBox
    $showServices.Text = 'Show protected services'; $showServices.AutoSize = $true
    $showServices.Margin = New-Object System.Windows.Forms.Padding(10,6,0,0)
    $bar.Controls.Add($showServices); $script:SCMUI.ServiceProtected = $showServices
    $showServices.add_CheckedChanged({ if ($script:SCMUI.Audit) { Update-SCMUIServiceFilter } })
    $pages['Services'].Controls.Add($bar)
    $grid = New-SCMUIGrid @(@('Task','TaskName'),@('Folder','TaskPath'),@('State','State'),@('Action / command','Command'),@('Author','Author'),@('Reason','Reason'))
    $script:SCMUI.TaskGrid = $grid; $pages['Scheduled tasks'].Controls.Add($grid)
    $bar = New-SCMUIToolbar
    [void](Add-SCMUIButton $bar 'Open Task Scheduler' { Invoke-SCMUIRead { Start-Process 'taskschd.msc' } })
    [void](Add-SCMUILabel $bar 'Review task triggers and actions in Task Scheduler. Tasks here are read-only.')
    $pages['Scheduled tasks'].Controls.Add($bar)
    $grid = New-SCMUIGrid @(@('Program','Name'),@('Version','Version'),@('Publisher','Publisher'),@('Install location','InstallLocation'),@('Type','Type'))
    $script:SCMUI.InstalledGrid = $grid; $pages['Installed programs'].Controls.Add($grid)
    $bar = New-SCMUIToolbar
    [void](Add-SCMUIButton $bar 'Uninstall in Windows Settings' { Invoke-SCMUIRead {
        $r = Get-SCMUISelected $script:SCMUI.InstalledGrid
        if ($null -eq $r) { throw 'Select an installed program first.' }
        [void][System.Windows.Forms.MessageBox]::Show($script:SCMUI.Form,('Search for "{0}" in Installed apps, then choose Uninstall. Windows and the program handle the removal.' -f $r.Name),'Uninstall program','OK','Information')
        Start-Process 'ms-settings:appsfeatures'
    } })
    [void](Add-SCMUILabel $bar 'Remove software through its uninstaller. Deleting executable files can break applications.')
    $pages['Installed programs'].Controls.Add($bar)
    # Power and maintenance launch supported Windows tools.
    $powerLayout = New-Object System.Windows.Forms.SplitContainer
    $powerLayout.Size = New-Object System.Drawing.Size(1200,620)
    $powerLayout.Dock = 'Fill'; $powerLayout.Orientation = 'Horizontal'; $powerLayout.SplitterDistance = 220
    $grid = New-SCMUIGrid @(@('Power plan','Name'),@('Active','Active',[bool]),@('GUID','Guid'))
    $script:SCMUI.PowerGrid = $grid; $powerLayout.Panel1.Controls.Add($grid)
    $systemText = New-Object System.Windows.Forms.TextBox
    $systemText.Dock = 'Fill'; $systemText.Multiline = $true; $systemText.ReadOnly = $true
    $systemText.ScrollBars = 'Vertical'; $systemText.BackColor = [System.Drawing.Color]::White
    $systemText.Font = New-Object System.Drawing.Font('Consolas',10)
    $script:SCMUI.System = $systemText; $powerLayout.Panel2.Controls.Add($systemText)
    $pages['Power and maintenance'].Controls.Add($powerLayout)
    $bar = New-SCMUIToolbar
    [void](Add-SCMUIButton $bar 'Activate selected plan' {
        $r = Get-SCMUISelected $script:SCMUI.PowerGrid
        Invoke-SCMUIChange -Record $r -Prompt ('Activate power plan {0}? High performance plans can increase heat and power consumption.' -f $r.Name) -Action { param($r) Set-SCMPowerPlan -Guid $r.Guid }
    })
    [void](Add-SCMUIButton $bar 'Storage cleanup' { Invoke-SCMUIRead { Start-Process 'ms-settings:storagesense' } })
    [void](Add-SCMUIButton $bar 'Optimize drives' { Invoke-SCMUIRead { Start-Process 'dfrgui.exe' } })
    [void](Add-SCMUIButton $bar 'Windows Update' { Invoke-SCMUIRead { Start-Process 'ms-settings:windowsupdate' } })
    $pages['Power and maintenance'].Controls.Add($bar)
    # History and inventory completeness.
    $changeSplit = New-Object System.Windows.Forms.SplitContainer
    $changeSplit.Size = New-Object System.Drawing.Size(1200,620)
    $changeSplit.Dock = 'Fill'; $changeSplit.Orientation = 'Horizontal'; $changeSplit.SplitterDistance = 340
    $grid = New-SCMUIGrid @(@('UTC time','TimeUtc'),@('Type','Kind'),@('Target','Target'),@('Status','Status'),@('ID','Id'))
    $script:SCMUI.ChangeGrid = $grid; $changeSplit.Panel1.Controls.Add($grid)
    $notes = New-Object System.Windows.Forms.TextBox
    $notes.Dock = 'Fill'; $notes.Multiline = $true; $notes.ReadOnly = $true; $notes.ScrollBars = 'Vertical'
    $notes.BackColor = [System.Drawing.Color]::FromArgb(246,249,252)
    $script:SCMUI.Notes = $notes; $changeSplit.Panel2.Controls.Add($notes)
    $pages['Changes and restore'].Controls.Add($changeSplit)
    $bar = New-SCMUIToolbar
    [void](Add-SCMUIButton $bar 'Restore selected change' {
        $r = Get-SCMUISelected $script:SCMUI.ChangeGrid
        Invoke-SCMUIChange -Record $r -Prompt ('Restore the prior settings for {0}? Review the selected change before continuing.' -f $r.Target) -Action { param($r) Restore-SCMChange -Change $r }
    })
    [void](Add-SCMUIButton $bar 'Open reports and backups' { Invoke-SCMUIRead { Start-Process -FilePath 'explorer.exe' -ArgumentList @($script:SCMDataRoot) } })
    [void](Add-SCMUILabel $bar 'Ending a process cannot restore unsaved work. History covers supported setting changes.')
    $pages['Changes and restore'].Controls.Add($bar)
    $form.add_Shown({
        try {
            Update-SCMUIAudit
            if ($script:SCMUI.SmokeTest) {
                $originalCount = $script:SCMUI.ProcessGrid.Rows.Count
                $script:SCMUI.Search.Text = 'SCM_no_such_process_1b77af'
                if ($script:SCMUI.ProcessGrid.Rows.Count -ne 0) { throw 'Process search smoke test failed.' }
                $script:SCMUI.Search.Text = ''
                $script:SCMUI.ShowProtected.Checked = $true
                if ($script:SCMUI.ProcessGrid.Rows.Count -ne @($script:SCMUI.Audit.Processes).Count) { throw 'Protected-process toggle smoke test failed.' }
                if ($script:SCMUI.ProcessGrid.Rows.Count -gt 0) {
                    $script:SCMUI.ProcessGrid.CurrentCell = $script:SCMUI.ProcessGrid.Rows[0].Cells[0]
                    Set-SCMUIProcessSummary
                    if ([string]::IsNullOrWhiteSpace($script:SCMUI.Details.Text)) { throw 'Process details smoke test failed.' }
                    $script:SCMUI.ProcessGrid.Sort($script:SCMUI.ProcessGrid.Columns['RAM MB'],[System.ComponentModel.ListSortDirection]::Descending)
                    $largest = ($script:SCMUI.Audit.Processes | Measure-Object MemoryMB -Maximum).Maximum
                    $first = $script:SCMUI.ProcessGrid.Rows[0].DataBoundItem.Row['__Record']
                    if ([double]$first.MemoryMB -ne [double]$largest) { throw 'Numeric RAM sorting smoke test failed.' }
                }
                $serviceVisible = @($script:SCMUI.Audit.Services | Where-Object { -not $_.Protected }).Count
                if ($script:SCMUI.ServiceGrid.Rows.Count -ne $serviceVisible) { throw 'Protected services were not hidden by default.' }
                $script:SCMUI.ServiceProtected.Checked = $true
                if ($script:SCMUI.ServiceGrid.Rows.Count -ne @($script:SCMUI.Audit.Services).Count) { throw 'Service protection toggle smoke test failed.' }
                $script:SCMUI.ServiceProtected.Checked = $false
                foreach ($tab in $script:SCMUI.Tabs.TabPages) { $script:SCMUI.Tabs.SelectedTab = $tab; [System.Windows.Forms.Application]::DoEvents() }
                $script:SCMUI.Tabs.SelectedIndex = 0
                $script:SCMUI.ShowProtected.Checked = $false
                [System.Windows.Forms.Application]::DoEvents()
                $screenshotVariable = Get-Variable -Name SCMUIScreenshotPath -Scope Script -ErrorAction SilentlyContinue
                if ($screenshotVariable -and $screenshotVariable.Value) {
                    $bitmap = New-Object System.Drawing.Bitmap($script:SCMUI.Form.Width,$script:SCMUI.Form.Height)
                    try {
                        $bounds = New-Object System.Drawing.Rectangle(0,0,$script:SCMUI.Form.Width,$script:SCMUI.Form.Height)
                        $script:SCMUI.Form.DrawToBitmap($bitmap,$bounds)
                        $bitmap.Save([string]$screenshotVariable.Value,[System.Drawing.Imaging.ImageFormat]::Png)
                    } finally { $bitmap.Dispose() }
                }
                $script:SCMUI.SmokeResult = [pscustomobject]@{
                    Status = 'Passed'; Tabs = $script:SCMUI.Tabs.TabPages.Count
                    Processes = @($script:SCMUI.Audit.Processes).Count; VisibleByDefault = $originalCount
                    Startup = $script:SCMUI.StartupGrid.Rows.Count; Services = $script:SCMUI.ServiceGrid.Rows.Count
                    ScheduledTasks = $script:SCMUI.TaskGrid.Rows.Count; Installed = $script:SCMUI.InstalledGrid.Rows.Count
                    PowerPlans = $script:SCMUI.PowerGrid.Rows.Count; Changes = $script:SCMUI.ChangeGrid.Rows.Count
                    Search = 'Passed'; Selection = 'Passed'; NumericSort = 'Passed'; Mutations = 0
                }
            }
        } catch {
            if ($script:SCMUI.SmokeTest) { $script:SCMUI.SmokeError = $_ }
            else { Show-SCMUIError $_.Exception.Message }
        } finally { if ($script:SCMUI.SmokeTest) { $script:SCMUI.Form.Close() } }
    })
    try { [void]$form.ShowDialog() }
    finally { $form.Dispose() }
    if ($SmokeTest) {
        if ($script:SCMUI.SmokeError) { throw $script:SCMUI.SmokeError }
        $script:SCMUI.SmokeResult | ConvertTo-Json -Compress
    }
}
function Invoke-SCMUIServiceAction {
    param([ValidateSet('Stop','Manual','Disabled')][string]$Action)
    $r = Get-SCMUISelected $script:SCMUI.ServiceGrid
    $script:SCMUI.ServiceAction = $Action
    Invoke-SCMUIChange -Record $r -Prompt ('Apply {0} to service {1}? Dependent applications or devices can stop working. Startup changes take effect on the next service start.' -f $Action,$r.Name) -Action { param($r)
        $undo = Set-SCMServiceState -Entry $r -Action $script:SCMUI.ServiceAction
        New-SCMChange -Kind 'Service' -Target $r.Name -Undo $undo | Out-Null
        if ($undo.Warning) {
            [void][System.Windows.Forms.MessageBox]::Show($script:SCMUI.Form,[string]$undo.Warning,'Service change recorded','OK','Information')
        }
    }
}
function Start-SCMUIElevated {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        $script:SCMUI.Status.Text = 'Already running as administrator.'; return
    }
    $entry = Join-Path $script:SCMHome 'ComputerManager.ps1'
    $arguments = '-NoProfile -ExecutionPolicy Bypass -STA -File "' + $entry + '" -ExpectedUserSid "' + $identity.User.Value + '"'
    Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') -Verb RunAs -ArgumentList $arguments
}





