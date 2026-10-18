# Read-only process inventory helpers. Compatible with Windows PowerShell 5.1.

function Get-SCMUtcTimeString {
    param([object]$Value)
    if ($null -eq $Value) { return '' }
    try {
        if ($Value -is [datetime]) { return $Value.ToUniversalTime().ToString('o') }
        if ($Value -is [datetimeoffset]) { return $Value.UtcDateTime.ToString('o') }
        return ([System.Management.ManagementDateTimeConverter]::ToDateTime([string]$Value)).ToUniversalTime().ToString('o')
    } catch { return '' }
}

function Get-SCMFileMetadata {
    param([string]$Path)
    if (-not (Get-Variable -Name SCMFileMetadataCache -Scope Script -ErrorAction SilentlyContinue)) {
        $script:SCMFileMetadataCache = @{}
    }
    if ([string]::IsNullOrWhiteSpace($Path)) {
        return [pscustomobject]@{ Company = ''; Description = ''; ProductName = '' }
    }
    if ($script:SCMFileMetadataCache.ContainsKey($Path)) { return $script:SCMFileMetadataCache[$Path] }
    $metadata = [pscustomobject]@{ Company = ''; Description = ''; ProductName = '' }
    try {
        $version = [System.Diagnostics.FileVersionInfo]::GetVersionInfo($Path)
        $metadata.Company = [string]$version.CompanyName
        $metadata.Description = [string]$version.FileDescription
        $metadata.ProductName = [string]$version.ProductName
    } catch { }
    $script:SCMFileMetadataCache[$Path] = $metadata
    return $metadata
}

function Get-SCMSecurityProductPaths {
    if (Get-Variable -Name SCMSecurityProductPathsCache -Scope Script -ErrorAction SilentlyContinue) {
        return $script:SCMSecurityProductPathsCache
    }
    $paths = New-Object 'System.Collections.Generic.List[string]'
    try {
        $products = @(Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntiVirusProduct -ErrorAction Stop)
        foreach ($product in $products) {
            foreach ($propertyName in @('pathToSignedProductExe', 'pathToSignedReportingExe')) {
                $value = [Environment]::ExpandEnvironmentVariables([string]$product.$propertyName)
                $candidate = ''
                if ($value -match '^\s*"([^"]+\.exe)"') { $candidate = $matches[1] }
                elseif ($value -match '^\s*(.+?\.exe)(?:\s|$)') { $candidate = $matches[1] }
                if ($candidate -and [IO.Path]::IsPathRooted($candidate)) { $paths.Add($candidate) }
            }
        }
    } catch { }
    $script:SCMSecurityProductPathsCache = @($paths.ToArray())
    return $script:SCMSecurityProductPathsCache
}

function Get-SCMProtectionReason {
    param(
        [int]$ProcessId,
        [int]$SessionId,
        [string]$Name,
        [string]$Path,
        [hashtable]$AncestorIds,
        [string[]]$SecurityProductPaths = @()
    )
    if ($AncestorIds -and $AncestorIds.ContainsKey($ProcessId)) { return 'This management tool or one of its parent processes.' }
    if ($ProcessId -le 4) { return 'Core Windows system process.' }
    if ($SessionId -eq 0) { return 'Session 0 system or service process; manage services separately.' }
    if ([string]::IsNullOrWhiteSpace($Path)) { return 'Executable path cannot be verified; protected conservatively.' }
    $windowsRoot = [Environment]::GetFolderPath('Windows').TrimEnd('\') + '\'
    if ($windowsRoot -ne '\' -and $Path.StartsWith($windowsRoot, [StringComparison]::OrdinalIgnoreCase)) {
        return 'Executable is inside the Windows directory.'
    }
    $baseName = [IO.Path]::GetFileNameWithoutExtension($Name).ToLowerInvariant()
    $criticalNames = @(
        'system', 'system idle process', 'registry', 'memory compression', 'secure system',
        'smss', 'csrss', 'wininit', 'winlogon', 'services', 'lsass', 'lsm', 'svchost',
        'fontdrvhost', 'dwm', 'sihost', 'taskhostw', 'taskeng', 'audiodg', 'spoolsv',
        'msmpeng', 'nissrv', 'mssense', 'sense', 'securityhealthservice',
        'securityhealthsystray', 'smartscreen', 'msseces', 'msmpengcp', 'msascuil',
        'mpdefendercoreservice', 'sgrmbroker', 'csfalconservice', 'sentinelagent',
        'sentinelservicehost', 'savservice', 'sophoshealth', 'mbamservice',
        'avp', 'avpui', 'avastsvc', 'avastui', 'avgsvc', 'avgui', 'bdagent',
        'bdservicehost', 'ekrn', 'egui', 'ccsvchst', 'nortonsecurity', 'mcshield'
    )
    if ($criticalNames -contains $baseName) { return 'Known Windows or security infrastructure process name.' }
    if ($Path -match '(?i)\\(Windows Defender|Microsoft Defender|Microsoft Antimalware|Windows Defender Advanced Threat Protection)\\') {
        return 'Microsoft security component.'
    }
    foreach ($securityPath in $SecurityProductPaths) {
        if ($Path.Equals($securityPath, [StringComparison]::OrdinalIgnoreCase)) { return 'Registered Windows security product.' }
        try {
            $securityDirectory = [IO.Path]::GetDirectoryName($securityPath).TrimEnd('\') + '\'
            if ($securityDirectory.Length -gt 4 -and $Path.StartsWith($securityDirectory, [StringComparison]::OrdinalIgnoreCase)) {
                return 'Executable shares a directory with a registered Windows security product.'
            }
        } catch { }
    }
    return ''
}

function Get-SCMProcessRole {
    param([string]$Name, [string]$CommandLine)
    $role = ''
    $evidence = ''
    if (-not $CommandLine) { return [pscustomobject]@{ Role = ''; Evidence = '' } }
    if ($CommandLine -match '(?:^|\s)--type(?:=|\s+)"?([a-zA-Z0-9_-]+)') {
        $type = $matches[1]
        switch ($type) {
            'renderer' { $role = 'App renderer'; break }
            'gpu-process' { $role = 'Graphics helper'; break }
            'utility' {
                $role = 'App utility helper'
                if ($CommandLine -match '(?:^|\s)--utility-sub-type(?:=|\s+)"?([a-zA-Z0-9_.:-]+)') {
                    $role += ': ' + $matches[1]
                }
                break
            }
            default { $role = 'App helper: ' + $type }
        }
        $evidence = 'Inferred role from app process type flags: ' + $role + '.'
    } elseif ($Name -match '^(?i)(node|python|pythonw|py)\.exe$') {
        if ($CommandLine -match '(?:^|\s)-m\s+([a-zA-Z_][a-zA-Z0-9_.]*)') {
            $role = 'Python module: ' + $matches[1]
        } else {
            $argumentTokens = @([regex]::Matches($CommandLine, '"[^"]*"|[^\s]+') | ForEach-Object { $_.Value.Trim('"') })
            for ($tokenIndex = 1; $tokenIndex -lt $argumentTokens.Count; $tokenIndex++) {
                $token = [string]$argumentTokens[$tokenIndex]
                if ($token -in @('-c', '-e', '--eval', '-p', '--print')) { break }
                if ($token -match '(?i)\.(js|cjs|mjs|ts|py|pyw)$') {
                    try { $role = 'Script: ' + [IO.Path]::GetFileName($token) } catch { }
                    break
                }
            }
        }
        $knownTools = [ordered]@{
            'playwright' = 'Playwright browser tooling'
            'puppeteer' = 'Puppeteer browser tooling'
            '@modelcontextprotocol' = 'Model Context Protocol tooling'
            'mcp-server' = 'MCP tool server'
            'mcp_server' = 'MCP tool server'
            'mcp-remote' = 'MCP remote connector'
            'codex' = 'Codex tooling'
        }
        foreach ($toolMarker in $knownTools.Keys) {
            if ($CommandLine.IndexOf([string]$toolMarker, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
                if ($role) { $role += ' (' + $knownTools[$toolMarker] + ')' }
                else { $role = [string]$knownTools[$toolMarker] }
                break
            }
        }
        if ($role) { $evidence = 'Inferred script/tool role: ' + $role + '. Only a script basename, module name, or recognized tool label is shown.' }
        else { $role = 'Runtime helper'; $evidence = 'Node/Python runtime; a script role could not be determined from safe metadata.' }
    }
    return [pscustomobject]@{ Role = $role; Evidence = $evidence }
}
function Get-SCMProcessSnapshot {
    [CmdletBinding()]
    param([ValidateRange(250, 5000)][int]$SampleMilliseconds = 650)
    $cimProcesses = @(Get-CimInstance -ClassName Win32_Process -ErrorAction Stop)
    $services = @(Get-CimInstance -ClassName Win32_Service -ErrorAction SilentlyContinue)
    $byId = @{}
    $serviceMap = @{}
    foreach ($item in $cimProcesses) { $byId[[int]$item.ProcessId] = $item }
    foreach ($service in $services) {
        $serviceProcessId = [int]$service.ProcessId
        if ($serviceProcessId -gt 0) {
            if (-not $serviceMap.ContainsKey($serviceProcessId)) { $serviceMap[$serviceProcessId] = @() }
            $serviceMap[$serviceProcessId] += [string]$service.Name
        }
    }
    $ancestorIds = @{}
    $ancestorId = [int]$PID
    while ($ancestorId -gt 0 -and -not $ancestorIds.ContainsKey($ancestorId)) {
        $ancestorIds[$ancestorId] = $true
        if (-not $byId.ContainsKey($ancestorId)) { break }
        $ancestorId = [int]$byId[$ancestorId].ParentProcessId
    }
    $securityPaths = @(Get-SCMSecurityProductPaths)
    $cpuBefore = @{}
    $startBefore = @{}
    $sampleBefore = @{}
    foreach ($process in @(Get-Process -ErrorAction SilentlyContinue)) {
        try {
            $cpuBefore[[int]$process.Id] = [double]$process.TotalProcessorTime.TotalMilliseconds
            $startBefore[[int]$process.Id] = $process.StartTime.ToUniversalTime().Ticks
            $sampleBefore[[int]$process.Id] = [Diagnostics.Stopwatch]::GetTimestamp()
        } catch { }
    }
    $timer = [Diagnostics.Stopwatch]::StartNew()
    Start-Sleep -Milliseconds $SampleMilliseconds
    $processesAfter = @(Get-Process -ErrorAction SilentlyContinue)
    $timer.Stop()
    $elapsedMilliseconds = [Math]::Max(1.0, $timer.Elapsed.TotalMilliseconds)
    $processorCount = [Math]::Max(1, [Environment]::ProcessorCount)
    $cpuAfter = @{}
    $sampleAfter = @{}
    foreach ($process in $processesAfter) {
        try {
            $cpuAfter[[int]$process.Id] = [double]$process.TotalProcessorTime.TotalMilliseconds
            $sampleAfter[[int]$process.Id] = [Diagnostics.Stopwatch]::GetTimestamp()
        } catch { }
    }
    $liveById = @{}
    foreach ($process in $processesAfter) { $liveById[[int]$process.Id] = $process }
    foreach ($cimProcess in $cimProcesses) {
        $processIdValue = [int]$cimProcess.ProcessId
        if (-not $liveById.ContainsKey($processIdValue) -and $processIdValue -gt 4) { continue }
        $live = $liveById[$processIdValue]
        $cpuPercent = 0.0
        $workingSet = 0.0
        $privateBytes = 0.0
        $startTimeUtc = Get-SCMUtcTimeString $cimProcess.CreationDate
        $currentPriority = ''
        $affinityMask = ''
        if ($null -ne $live) {
            try { $workingSet = [double]$live.WorkingSet64 } catch { }
            try { $privateBytes = [double]$live.PrivateMemorySize64 } catch { }
            try { $currentPriority = [string]$live.PriorityClass } catch { }
            try { $affinityMask = $live.ProcessorAffinity.ToInt64().ToString() } catch { }
            try {
                if ($cpuBefore.ContainsKey($processIdValue) -and $cpuAfter.ContainsKey($processIdValue) -and $sampleBefore.ContainsKey($processIdValue) -and $sampleAfter.ContainsKey($processIdValue) -and $startBefore.ContainsKey($processIdValue) -and
                    $startBefore[$processIdValue] -eq $live.StartTime.ToUniversalTime().Ticks) {
                    $cpuDelta = [double]$cpuAfter[$processIdValue] - [double]$cpuBefore[$processIdValue]
                    $elapsedMilliseconds = [Math]::Max(1.0, ([double]$sampleAfter[$processIdValue] - [double]$sampleBefore[$processIdValue]) / [Diagnostics.Stopwatch]::Frequency * 1000.0)
                    $cpuPercent = [Math]::Round([Math]::Min(100.0, [Math]::Max(0.0, $cpuDelta / $elapsedMilliseconds / $processorCount * 100.0)), 2)
                }
            } catch { }
            if (-not $startTimeUtc) { try { $startTimeUtc = $live.StartTime.ToUniversalTime().ToString('o') } catch { } }
        }
        $executablePath = [string]$cimProcess.ExecutablePath
        if (-not $executablePath -and $null -ne $live) { try { $executablePath = [string]$live.Path } catch { } }
        $metadata = Get-SCMFileMetadata $executablePath
        $parentIdValue = [int]$cimProcess.ParentProcessId
        $parentName = ''
        $parentPath = ''
        if ($byId.ContainsKey($parentIdValue)) {
            $parentRecord = $byId[$parentIdValue]
            # A reused parent PID is not evidence of the original parent.
            $parentTime = Get-SCMUtcTimeString $parentRecord.CreationDate
            if (-not $startTimeUtc -or -not $parentTime -or [datetime]$parentTime -le [datetime]$startTimeUtc) {
                $parentName = [string]$parentRecord.Name
                $parentPath = [string]$parentRecord.ExecutablePath
            }
        }
        $serviceNames = ''
        if ($serviceMap.ContainsKey($processIdValue)) { $serviceNames = ($serviceMap[$processIdValue] -join ', ') }
        $appName = [string]$metadata.ProductName
        if (-not $appName) { $appName = [string]$metadata.Description }
        if (-not $appName) { $appName = [IO.Path]::GetFileNameWithoutExtension([string]$cimProcess.Name) }
        $launchExplanation = 'Launch reason unavailable; a process list cannot establish whether an app is still needed.'
        if ($serviceNames) { $launchExplanation = 'Registered service process: ' + $serviceNames + '. Service purpose depends on its configuration.' }
        elseif ($parentName) { $launchExplanation = 'Inferred launcher: ' + $parentName + ' (parent process ' + $parentIdValue + '). Exact launch reason unavailable.' }
        $roleInfo = Get-SCMProcessRole -Name ([string]$cimProcess.Name) -CommandLine ([string]$cimProcess.CommandLine)
        if ($roleInfo.Evidence) { $launchExplanation += ' ' + $roleInfo.Evidence }
        $rootRecord = $cimProcess
        $rootWalkIds = @{}
        while ($byId.ContainsKey([int]$rootRecord.ParentProcessId)) {
            $rootParent = $byId[[int]$rootRecord.ParentProcessId]
            if ($rootWalkIds.ContainsKey([int]$rootParent.ProcessId)) { break }
            $rootWalkIds[[int]$rootParent.ProcessId] = $true
            $rootParentPath = [string]$rootParent.ExecutablePath
            $windowsPrefix = [Environment]::GetFolderPath('Windows').TrimEnd('\') + '\'
            if (-not $rootParentPath -or $rootParentPath.StartsWith($windowsPrefix, [StringComparison]::OrdinalIgnoreCase) -or [int]$rootParent.SessionId -eq 0) { break }
            $rootParentTime = Get-SCMUtcTimeString $rootParent.CreationDate
            $rootCurrentTime = Get-SCMUtcTimeString $rootRecord.CreationDate
            if ($rootParentTime -and $rootCurrentTime -and [datetime]$rootParentTime -gt [datetime]$rootCurrentTime) { break }
            $rootRecord = $rootParent
        }
        $rootMetadata = Get-SCMFileMetadata ([string]$rootRecord.ExecutablePath)
        $rootProgram = [string]$rootMetadata.ProductName
        if (-not $rootProgram) { $rootProgram = [string]$rootRecord.Name }
        $protectionReason = Get-SCMProtectionReason -ProcessId $processIdValue -SessionId ([int]$cimProcess.SessionId) -Name ([string]$cimProcess.Name) -Path $executablePath -AncestorIds $ancestorIds -SecurityProductPaths $securityPaths
        [pscustomobject]@{
            ProcessId = $processIdValue
            ParentProcessId = $parentIdValue
            Name = [string]$cimProcess.Name
            AppName = $appName
            CpuPercent = $cpuPercent
            MemoryMB = [Math]::Round($workingSet / 1MB, 1)
            PrivateMB = [Math]::Round($privateBytes / 1MB, 1)
            IoReadMBTotal = [Math]::Round([double]$cimProcess.ReadTransferCount / 1MB, 1)
            IoWriteMBTotal = [Math]::Round([double]$cimProcess.WriteTransferCount / 1MB, 1)
            Path = $executablePath
            Company = [string]$metadata.Company
            Description = [string]$metadata.Description
            StartTimeUtc = $startTimeUtc
            SessionId = [int]$cimProcess.SessionId
            Owner = ''
            IsProtected = [bool]$protectionReason
            ProtectionReason = $protectionReason
            IsService = [bool]$serviceNames
            ServiceNames = $serviceNames
            ParentName = $parentName
            ParentPath = $parentPath
            LaunchExplanation = $launchExplanation
            Role = [string]$roleInfo.Role
            RootProgram = $rootProgram
            RootProcessId = [int]$rootRecord.ProcessId
            CurrentPriority = $currentPriority
            AffinityMask = $affinityMask
        }
    }
}

function Get-SCMProcessIdentity {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][ValidateRange(0, 2147483647)][int]$ProcessId)
    $process = Get-CimInstance -ClassName Win32_Process -Filter ('ProcessId = ' + $ProcessId) -ErrorAction Stop
    if ($null -eq $process) { throw ('Process ' + $ProcessId + ' is no longer running. Refresh the list.') }
    $executablePath = [string]$process.ExecutablePath
    if (-not $executablePath) {
        try { $executablePath = [string](Get-Process -Id $ProcessId -ErrorAction Stop).Path } catch { }
    }
    [pscustomobject]@{
        ProcessId = [int]$process.ProcessId
        ParentProcessId = [int]$process.ParentProcessId
        Name = [string]$process.Name
        Path = $executablePath
        StartTimeUtc = Get-SCMUtcTimeString $process.CreationDate
        SessionId = [int]$process.SessionId
    }
}

function Test-SCMProcessIdentity {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][object]$Record)
    if ($null -eq $Record.ProcessId) { throw 'The selected record has no process ID. Refresh the list.' }
    if ([string]::IsNullOrWhiteSpace([string]$Record.Path) -or [string]::IsNullOrWhiteSpace([string]$Record.StartTimeUtc)) {
        throw 'The executable path or creation time could not be verified. This process cannot be changed safely.'
    }
    $fresh = Get-SCMProcessIdentity -ProcessId ([int]$Record.ProcessId)
    if (-not $fresh.Path -or -not $fresh.StartTimeUtc -or
        -not ([string]$fresh.Path).Equals([string]$Record.Path, [StringComparison]::OrdinalIgnoreCase) -or
        ([datetimeoffset]$fresh.StartTimeUtc).UtcDateTime.Ticks -ne ([datetimeoffset]$Record.StartTimeUtc).UtcDateTime.Ticks) {
        throw 'The process identity changed or cannot be verified. Refresh the list before taking action.'
    }
    return $fresh
}

function Get-SCMSystemSummary {
    [CmdletBinding()]
    param()
    $operatingSystem = Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction Stop
    $processors = @(Get-CimInstance -ClassName Win32_Processor -ErrorAction SilentlyContinue)
    $totalMemory = [double]$operatingSystem.TotalVisibleMemorySize * 1KB
    $availableMemory = [double]$operatingSystem.FreePhysicalMemory * 1KB
    $usedMemory = [Math]::Max(0.0, $totalMemory - $availableMemory)
    $disks = @(Get-CimInstance -ClassName Win32_LogicalDisk -Filter 'DriveType = 3' -ErrorAction SilentlyContinue | ForEach-Object {
        $size = [double]$_.Size
        [pscustomobject]@{
            Drive = [string]$_.DeviceID
            Label = [string]$_.VolumeName
            SizeGB = [Math]::Round($size / 1GB, 1)
            FreeGB = [Math]::Round([double]$_.FreeSpace / 1GB, 1)
            FreePercent = $(if ($size -gt 0) { [Math]::Round([double]$_.FreeSpace / $size * 100, 1) } else { 0 })
        }
    })
    $cpuAverage = 0.0
    if ($processors.Count -gt 0) { $cpuAverage = [double](($processors | Measure-Object -Property LoadPercentage -Average).Average) }
    [pscustomobject]@{
        CapturedAtUtc = [datetime]::UtcNow.ToString('o')
        ComputerName = [Environment]::MachineName
        Windows = [string]$operatingSystem.Caption
        CpuName = (($processors | ForEach-Object { [string]$_.Name }) -join '; ')
        CpuPercent = [Math]::Round($cpuAverage, 1)
        LogicalProcessors = [Environment]::ProcessorCount
        MemoryTotalGB = [Math]::Round($totalMemory / 1GB, 2)
        MemoryUsedGB = [Math]::Round($usedMemory / 1GB, 2)
        MemoryAvailableGB = [Math]::Round($availableMemory / 1GB, 2)
        MemoryUsedPercent = $(if ($totalMemory -gt 0) { [Math]::Round($usedMemory / $totalMemory * 100, 1) } else { 0 })
        LastBootUtc = Get-SCMUtcTimeString $operatingSystem.LastBootUpTime
        Disks = $disks
    }
}

function Get-SCMProcessDetails {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][object]$Record)
    $fresh = Get-SCMProcessIdentity -ProcessId ([int]$Record.ProcessId)
    if ($Record.StartTimeUtc -and $Record.Path) { $fresh = Test-SCMProcessIdentity -Record $Record }
    if ($Record.StartTimeUtc -and $fresh.StartTimeUtc -and ([datetimeoffset]$Record.StartTimeUtc).UtcDateTime.Ticks -ne ([datetimeoffset]$fresh.StartTimeUtc).UtcDateTime.Ticks) { throw 'Process identity changed. Refresh before inspecting details.' }
    $owner = 'Unavailable'
    try {
        $instance = Get-CimInstance -ClassName Win32_Process -Filter ('ProcessId = ' + $fresh.ProcessId) -ErrorAction Stop
        if ((Get-SCMUtcTimeString $instance.CreationDate) -ne $fresh.StartTimeUtc) { throw 'Process changed.' }
        $ownerResult = Invoke-CimMethod -InputObject $instance -MethodName GetOwner -ErrorAction Stop
        if ($ownerResult.ReturnValue -eq 0) { $owner = ([string]$ownerResult.Domain + '\' + [string]$ownerResult.User).TrimStart('\') }
    } catch { }
    $signatureStatus = 'Unavailable'
    $signatureSubject = ''
    $signatureThumbprint = ''
    if ($fresh.Path) {
        try {
            $signature = Get-AuthenticodeSignature -LiteralPath $fresh.Path -ErrorAction Stop
            $signatureStatus = [string]$signature.Status
            if ($signature.SignerCertificate) {
                $signatureSubject = [string]$signature.SignerCertificate.Subject
                $signatureThumbprint = [string]$signature.SignerCertificate.Thumbprint
            }
        } catch { $signatureStatus = 'Unavailable: ' + $_.Exception.Message }
    }
    $parent = $null
    try {
        $candidateParent = Get-SCMProcessIdentity -ProcessId $fresh.ParentProcessId
        if ($candidateParent.StartTimeUtc -and $fresh.StartTimeUtc -and
            [datetime]$candidateParent.StartTimeUtc -le [datetime]$fresh.StartTimeUtc) { $parent = $candidateParent }
    } catch { }
    $children = @(Get-CimInstance -ClassName Win32_Process -Filter ('ParentProcessId = ' + $fresh.ProcessId) -ErrorAction SilentlyContinue | ForEach-Object {
        $childCreation = Get-SCMUtcTimeString $_.CreationDate
        if (-not $fresh.StartTimeUtc -or -not $childCreation -or [datetime]$childCreation -ge [datetime]$fresh.StartTimeUtc) {
            [pscustomobject]@{ ProcessId = [int]$_.ProcessId; Name = [string]$_.Name; Path = [string]$_.ExecutablePath; StartTimeUtc = $childCreation }
        }
    })
    $hostedServices = @(Get-CimInstance -ClassName Win32_Service -Filter ('ProcessId = ' + $fresh.ProcessId) -ErrorAction SilentlyContinue | Select-Object Name, DisplayName, Description, State, StartMode, StartName)
    $modulePaths = @()
    $moduleStatus = 'Available'
    try { $modulePaths = @((Get-Process -Id $fresh.ProcessId -ErrorAction Stop).Modules | ForEach-Object { [string]$_.FileName } | Sort-Object -Unique) }
    catch { $moduleStatus = 'Unavailable: ' + $_.Exception.Message }
    [pscustomobject]@{
        Identity = $fresh
        App = Get-SCMFileMetadata $fresh.Path
        Owner = $owner
        SignatureStatus = $signatureStatus
        SignatureSubject = $signatureSubject
        SignatureThumbprint = $signatureThumbprint
        Parent = $parent
        Children = $children
        HostedServices = $hostedServices
        ModuleStatus = $moduleStatus
        ModulePaths = $modulePaths
        Explanation = [string]$Record.LaunchExplanation
        Note = 'Parent and file metadata describe observed relationships. They do not prove that a process is unnecessary or explain every trigger that launched it. Working sets overlap, so their sum is not total physical memory usage. I/O values are lifetime totals, not current rates.'
    }
}

