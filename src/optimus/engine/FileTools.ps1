# Read-only storage/driver inventory and explicit, validated duplicate recycling.
# Windows PowerShell 5.1. No invocation or scanning occurs when this file loads.

function Test-SCMFileToolPathWithin {
    param([string]$Path, [string]$Root)
    $prefix = $Root.TrimEnd('\') + '\'
    return $Path.Equals($Root.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase) -or $Path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)
}

function Get-SCMFileToolExcludedRoots {
    $values = New-Object 'System.Collections.Generic.List[string]'
    foreach ($name in @('Windows','ProgramFiles','ProgramFilesX86','CommonApplicationData','LocalApplicationData','ApplicationData')) {
        try { $value = [Environment]::GetFolderPath($name); if ($value) { $values.Add([IO.Path]::GetFullPath($value).TrimEnd('\')) } } catch { }
    }
    foreach ($value in @($env:ProgramW6432, ${env:ProgramFiles(x86)}, $env:ProgramData)) {
        if ($value) { try { $values.Add([IO.Path]::GetFullPath($value).TrimEnd('\')) } catch { } }
    }
    if ($env:USERPROFILE) {
        foreach ($part in @('.codex','.agents','.cache\codex-runtimes')) { $values.Add((Join-Path $env:USERPROFILE $part).TrimEnd('\')) }
    }
    return @($values.ToArray() | Sort-Object -Unique)
}

function Test-SCMFileToolExcludedPath {
    param([string]$Path, [string[]]$ExcludedRoots)
    foreach ($excludedRoot in $ExcludedRoots) { if (Test-SCMFileToolPathWithin $Path $excludedRoot) { return $true } }
    if ($Path -match '(?i)(?:^|\\)(?:\$Recycle\.Bin|System Volume Information|Recovery|Config\.Msi|MSOCache|\.codex|\.agents|node_modules|\.git)(?:\\|$)') { return $true }
    if ($Path -match '(?i)\\(?:codex-runtimes|codex-primary-runtime|WindowsApps)(?:\\|$)') { return $true }
    return $false
}

function Assert-SCMFileToolNoReparse {
    param([Parameter(Mandatory=$true)][string]$Path)
    $full = [IO.Path]::GetFullPath($Path)
    if ($full -notmatch '^[a-zA-Z]:\\') { throw 'Choose a local drive folder; network and device paths are not supported.' }
    $volume = [IO.Path]::GetPathRoot($full)
    $driveInfo = New-Object IO.DriveInfo($volume)
    if ($driveInfo.DriveType -eq [IO.DriveType]::Network) { throw 'Mapped network drives are not supported.' }
    $current = $volume
    $attributes = [IO.File]::GetAttributes($current)
    if (($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'A root or ancestor is a reparse point.' }
    foreach ($part in $full.Substring($volume.Length).Split('\', [StringSplitOptions]::RemoveEmptyEntries)) {
        $current = [IO.Path]::Combine($current, $part)
        $attributes = [IO.File]::GetAttributes($current)
        if (($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'A file or ancestor is a reparse point; it is not followed.' }
    }
    return $full
}

function Get-SCMFileToolRoot {
    param([Parameter(Mandatory=$true)][string]$Root, [switch]$ForDuplicates)
    $item = Get-Item -LiteralPath $Root -Force -ErrorAction Stop
    if (-not $item.PSIsContainer -or $item.PSProvider.Name -ne 'FileSystem') { throw 'Choose an existing filesystem folder.' }
    $full = Assert-SCMFileToolNoReparse -Path $item.FullName
    $volume = [IO.Path]::GetPathRoot($full)
    if ($full.Length -gt $volume.Length) { $full = $full.TrimEnd('\') }
    if ($ForDuplicates) {
        if ($full.TrimEnd('\').Equals($volume.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)) { throw 'For duplicate review, choose a specific personal-data folder, not a whole drive.' }
        if (Test-SCMFileToolExcludedPath -Path $full -ExcludedRoots @(Get-SCMFileToolExcludedRoots)) { throw 'System, program, application-data, repository internals, and Codex runtime folders are excluded from duplicate review.' }
    }
    return $full
}

function Test-SCMFileToolCloudAttributes {
    param([long]$Attributes)
    # OFFLINE, RECALL_ON_OPEN, RECALL_ON_DATA_ACCESS; metadata scanning does not hydrate files.
    $cloudMask = [long]0x1000 -bor [long]0x40000 -bor [long]0x400000
    return (([long]$Attributes -band $cloudMask) -ne 0)
}

function Add-SCMFileToolError {
    param($State, [string]$Message)
    $State.ErrorCount++
    if ($State.Errors.Count -lt 50) { $State.Errors.Add($Message) }
}

function Get-SCMFileToolEntries {
    param([string]$Root, [int]$MaxFiles, [int]$MaxSeconds, $State, [switch]$ForDuplicates)
    $pending = New-Object 'System.Collections.Generic.Stack[string]'
    $pending.Push($Root)
    $excludedRoots = @(Get-SCMFileToolExcludedRoots)
    while ($pending.Count -gt 0) {
        if ($State.Timer.Elapsed.TotalSeconds -ge $MaxSeconds -or $State.FilesScanned -ge $MaxFiles) { $State.LimitReached = $true; break }
        $directory = $pending.Pop()
        $enumerator = $null
        try {
            [void](Assert-SCMFileToolNoReparse -Path $directory)
            $enumerator = [IO.Directory]::EnumerateFileSystemEntries($directory).GetEnumerator()
            while ($enumerator.MoveNext()) {
                if ($State.Timer.Elapsed.TotalSeconds -ge $MaxSeconds -or $State.FilesScanned -ge $MaxFiles) { $State.LimitReached = $true; break }
                $entryPath = [string]$enumerator.Current
                try {
                    $attributes = [IO.File]::GetAttributes($entryPath)
                    $isDirectory = ($attributes -band [IO.FileAttributes]::Directory) -ne 0
                    if (($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { if ($isDirectory) { $State.SkippedDirectories++ } else { $State.SkippedFiles++ }; continue }
                    if ($ForDuplicates -and ((Test-SCMFileToolExcludedPath -Path $entryPath -ExcludedRoots $excludedRoots) -or ($attributes -band [IO.FileAttributes]::System) -ne 0)) { if ($isDirectory) { $State.SkippedDirectories++ } else { $State.SkippedFiles++ }; continue }
                    if ($isDirectory) { $pending.Push($entryPath); continue }
                    if ($ForDuplicates -and (Test-SCMFileToolCloudAttributes $attributes)) { $State.SkippedFiles++; continue }
                    $file = New-Object IO.FileInfo($entryPath)
                    $State.FilesScanned++
                    [pscustomobject]@{ FilePath = $file.FullName; Length = [long]$file.Length; LastWriteTimeUtcTicks = [long]$file.LastWriteTimeUtc.Ticks; LastWriteTimeUtc = $file.LastWriteTimeUtc.ToString('o'); Extension = $file.Extension.ToLowerInvariant() }
                } catch { Add-SCMFileToolError $State ($entryPath + ': ' + $_.Exception.Message) }
            }
        } catch { Add-SCMFileToolError $State ($directory + ': ' + $_.Exception.Message) }
        finally { if ($enumerator -is [IDisposable]) { $enumerator.Dispose() } }
        if ($State.LimitReached) { break }
    }
}

function New-SCMFileToolScanState {
    [pscustomobject]@{ Timer = [Diagnostics.Stopwatch]::StartNew(); FilesScanned = 0; SkippedDirectories = 0; SkippedFiles = 0; ErrorCount = 0; Errors = (New-Object 'System.Collections.Generic.List[string]'); LimitReached = $false }
}

function Get-SCMStorageAnalysis {
    [CmdletBinding()]
    param([Parameter(Mandatory=$true)][string]$Root, [ValidateRange(1,1000000)][int]$MaxFiles=100000, [ValidateRange(1,600)][int]$MaxSeconds=45)
    $canonicalRoot = Get-SCMFileToolRoot -Root $Root
    $state = New-SCMFileToolScanState
    $topFiles = New-Object 'System.Collections.Generic.List[object]'
    $extensions = @{}
    $directories = @{}
    [long]$totalBytes = 0
    Get-SCMFileToolEntries -Root $canonicalRoot -MaxFiles $MaxFiles -MaxSeconds $MaxSeconds -State $state | ForEach-Object {
        $file = $_
        $totalBytes += $file.Length
        $extension = $file.Extension
        if (-not $extension) { $extension = '(no extension)' }
        if (-not $extensions.ContainsKey($extension)) { $extensions[$extension] = [pscustomobject]@{ Extension=$extension; Files=0; Bytes=[long]0 } }
        $extensions[$extension].Files++; $extensions[$extension].Bytes += $file.Length
        $relativePath = $file.FilePath.Substring(($canonicalRoot.TrimEnd('\') + '\').Length)
        $separator = $relativePath.IndexOf('\')
        $firstDirectory = '(root files)'
        if ($separator -ge 0) { $firstDirectory = $relativePath.Substring(0,$separator) }
        if (-not $directories.ContainsKey($firstDirectory)) { $directories[$firstDirectory] = [pscustomobject]@{ Directory=$firstDirectory; Files=0; Bytes=[long]0 } }
        $directories[$firstDirectory].Files++; $directories[$firstDirectory].Bytes += $file.Length
        if ($topFiles.Count -lt 100) { $topFiles.Add($file) }
        else {
            $smallest = $topFiles | Sort-Object Length | Select-Object -First 1
            if ($file.Length -gt $smallest.Length) { [void]$topFiles.Remove($smallest); $topFiles.Add($file) }
        }
    }
    $state.Timer.Stop()
    [pscustomobject]@{ Root=$canonicalRoot; ScanId=[guid]::NewGuid().ToString('N'); CapturedUtc=[datetime]::UtcNow.ToString('o'); Complete=(-not $state.LimitReached -and $state.ErrorCount -eq 0 -and $state.SkippedDirectories -eq 0 -and $state.SkippedFiles -eq 0); FilesScanned=$state.FilesScanned; TotalBytes=$totalBytes; TopFiles=@($topFiles.ToArray() | Sort-Object Length -Descending | Select-Object @{Name='FilePath';Expression={$_.FilePath}},@{Name='Bytes';Expression={$_.Length}},LastWriteTimeUtc); ExtensionGroups=@($extensions.Values | Sort-Object Bytes -Descending); DirectoryGroups=@($directories.Values | Sort-Object Bytes -Descending); ErrorCount=$state.ErrorCount; Errors=@($state.Errors.ToArray()); SkippedDirectories=$state.SkippedDirectories; SkippedFiles=$state.SkippedFiles; LimitReached=$state.LimitReached; ElapsedSeconds=[math]::Round($state.Timer.Elapsed.TotalSeconds,2); Note='Logical file lengths only; allocated space, compressed size, hard links, and shared storage can differ. Reparse paths are skipped. A limited or inaccessible scan is partial.' }
}

function Get-SCMFileToolVerifiedHash {
    param($Record, [Diagnostics.Stopwatch]$Budget, [int]$MaxSeconds=120)
    [void](Assert-SCMFileToolNoReparse -Path $Record.FilePath)
    $file = New-Object IO.FileInfo($Record.FilePath)
    $file.Refresh()
    if (-not $file.Exists -or $file.Length -ne [long]$Record.Length -or $file.LastWriteTimeUtc.Ticks -ne [long]$Record.LastWriteTimeUtcTicks) { throw 'The file changed since it was listed.' }
    if (($file.Attributes -band [IO.FileAttributes]::System) -ne 0 -or (Test-SCMFileToolCloudAttributes $file.Attributes)) { throw 'System files and cloud placeholders are not hashed or recycled.' }
    $stream = $null; $hasher = $null
    try {
        $stream = [IO.File]::Open($file.FullName, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
        $hasher = [Security.Cryptography.SHA256]::Create()
        $buffer = New-Object byte[] 1048576
        while (($readCount = $stream.Read($buffer,0,$buffer.Length)) -gt 0) {
            if ($Budget -and $Budget.Elapsed.TotalSeconds -ge $MaxSeconds) { throw 'SCM scan time budget reached.' }
            [void]$hasher.TransformBlock($buffer,0,$readCount,$buffer,0)
        }
        [void]$hasher.TransformFinalBlock((New-Object byte[] 0),0,0)
        $hash = [BitConverter]::ToString($hasher.Hash).Replace('-','').ToLowerInvariant()
    } finally {
        if ($stream) { $stream.Dispose() }
        if ($hasher) { $hasher.Dispose() }
    }
    [void](Assert-SCMFileToolNoReparse -Path $Record.FilePath)
    $file.Refresh()
    if (-not $file.Exists -or $file.Length -ne [long]$Record.Length -or $file.LastWriteTimeUtc.Ticks -ne [long]$Record.LastWriteTimeUtcTicks) { throw 'The file changed during verification.' }
    return $hash
}

function Get-SCMDuplicateFiles {
    [CmdletBinding()]
    param([Parameter(Mandatory=$true)][string]$Root, [ValidateRange(0,9223372036854775807)][long]$MinBytes=1MB, [ValidateRange(1,200000)][int]$MaxFiles=50000, [ValidateRange(1,600)][int]$MaxSeconds=45)
    $canonicalRoot = Get-SCMFileToolRoot -Root $Root -ForDuplicates
    $state = New-SCMFileToolScanState
    $scanId = [guid]::NewGuid().ToString('N')
    if (-not (Get-Variable -Name SCMFileToolApprovals -Scope Script -ErrorAction SilentlyContinue)) { $script:SCMFileToolApprovals = @{} }
    # Keep at most ten scans approved in this PowerShell session.
    if ($script:SCMFileToolApprovals.Count -ge 10) {
        $oldest = $script:SCMFileToolApprovals.Values | Sort-Object CapturedUtc | Select-Object -First 1
        [void]$script:SCMFileToolApprovals.Remove($oldest.ScanId)
    }
    $lengthGroups = @{}
    Get-SCMFileToolEntries -Root $canonicalRoot -MaxFiles $MaxFiles -MaxSeconds $MaxSeconds -State $state -ForDuplicates | ForEach-Object {
        if ($_.Length -ge $MinBytes) {
            $key = ([long]$_.Length).ToString()
            if (-not $lengthGroups.ContainsKey($key)) { $lengthGroups[$key] = New-Object 'System.Collections.Generic.List[object]' }
            $lengthGroups[$key].Add($_)
        }
    }
    $hashGroups = @{}
    $hashedFiles = 0
    foreach ($lengthGroup in $lengthGroups.Values) {
        if ($lengthGroup.Count -lt 2) { continue }
        foreach ($file in $lengthGroup) {
            if ($state.Timer.Elapsed.TotalSeconds -ge $MaxSeconds) { $state.LimitReached=$true; break }
            try {
                $hash = Get-SCMFileToolVerifiedHash -Record $file -Budget $state.Timer -MaxSeconds $MaxSeconds
                $hashedFiles++
                $key = ([long]$file.Length).ToString() + '|' + $hash
                if (-not $hashGroups.ContainsKey($key)) { $hashGroups[$key] = New-Object 'System.Collections.Generic.List[object]' }
                $hashGroups[$key].Add($file)
            } catch {
                if ($_.Exception.Message -eq 'SCM scan time budget reached.') { $state.LimitReached=$true; break }
                Add-SCMFileToolError $state ($file.FilePath + ': ' + $_.Exception.Message)
            }
        }
        if ($state.LimitReached -and $state.Timer.Elapsed.TotalSeconds -ge $MaxSeconds) { break }
    }
    $groups = New-Object 'System.Collections.Generic.List[object]'
    $candidates = New-Object 'System.Collections.Generic.List[object]'
    $approval = @{ ScanId=$scanId; CapturedUtc=[datetime]::UtcNow.ToString('o'); Root=$canonicalRoot; Records=@{}; Groups=@{} }
    foreach ($key in $hashGroups.Keys) {
        $files = $hashGroups[$key]
        if ($files.Count -lt 2) { continue }
        $hash = $key.Split('|')[1]
        $paths = @($files | ForEach-Object { [string]$_.FilePath })
        $publicCandidates = New-Object 'System.Collections.Generic.List[object]'
        foreach ($file in $files) {
            $approved = @{ FilePath=[string]$file.FilePath; Length=[long]$file.Length; LastWriteTimeUtcTicks=[long]$file.LastWriteTimeUtcTicks; ScanId=$scanId; Root=$canonicalRoot; Fingerprint=$hash; GroupKey=$key; Recycled=$false }
            $approval.Records[$file.FilePath] = $approved
            $public = [pscustomobject]@{ FilePath=[string]$file.FilePath; Length=[long]$file.Length; LastWriteTimeUtcTicks=[long]$file.LastWriteTimeUtcTicks; ScanId=$scanId; Root=$canonicalRoot; Fingerprint=$hash }
            $publicCandidates.Add($public); $candidates.Add($public)
        }
        $approval.Groups[$key] = [string[]]$paths.Clone()
        $groups.Add([pscustomobject]@{ SHA256=$hash; SizeBytes=[long]$files[0].Length; Paths=$paths; Candidates=@($publicCandidates.ToArray()); Copies=$files.Count; PotentialRedundantBytes=([long]$files[0].Length * ($files.Count-1)) })
    }
    $script:SCMFileToolApprovals[$scanId] = $approval
    $state.Timer.Stop()
    [pscustomobject]@{ Root=$canonicalRoot; ScanId=$scanId; CapturedUtc=$approval.CapturedUtc; Complete=(-not $state.LimitReached -and $state.ErrorCount -eq 0 -and $state.SkippedDirectories -eq 0 -and $state.SkippedFiles -eq 0); FilesScanned=$state.FilesScanned; FilesHashed=$hashedFiles; Groups=@($groups.ToArray() | Sort-Object PotentialRedundantBytes -Descending); Candidates=@($candidates.ToArray()); ErrorCount=$state.ErrorCount; Errors=@($state.Errors.ToArray()); SkippedDirectories=$state.SkippedDirectories; SkippedFiles=$state.SkippedFiles; LimitReached=$state.LimitReached; ElapsedSeconds=[math]::Round($state.Timer.Elapsed.TotalSeconds,2); Note='Exact SHA256 content matches, grouped by length first. Protected/app-data paths, repository internals, reparse paths, and cloud placeholders are skipped. Each recycle action revalidates one selected file and another surviving copy. Logical redundant bytes are not guaranteed disk savings (hard links/compression/shared storage may differ). Scan approval is valid only in this running PowerShell session.' }
}

function Move-SCMFileToRecycleBin {
    [CmdletBinding()]
    param([Parameter(Mandatory=$true)]$Record)
    if (-not (Get-Variable -Name SCMFileToolApprovals -Scope Script -ErrorAction SilentlyContinue)) { throw 'No duplicate scan is approved in this session. Scan the folder again.' }
    $scanId = [string]$Record.ScanId
    if (-not $scanId -or -not $script:SCMFileToolApprovals.ContainsKey($scanId)) { throw 'This duplicate scan is unknown or expired. Scan the folder again.' }
    $approval = $script:SCMFileToolApprovals[$scanId]
    $selectedPath = [string]$Record.FilePath
    if (-not $selectedPath -or -not $approval.Records.ContainsKey($selectedPath)) { throw 'The selected file was not approved by this duplicate scan.' }
    $approved = $approval.Records[$selectedPath]
    if ($approved.Recycled) { throw 'This selected file has already been recycled.' }
    foreach ($field in @('FilePath','Root','ScanId','Fingerprint')) {
        if (-not ([string]$approved[$field]).Equals([string]$Record.$field, [StringComparison]::OrdinalIgnoreCase)) { throw 'The selected duplicate record changed. Scan again.' }
    }
    if ([long]$Record.Length -ne $approved.Length -or [long]$Record.LastWriteTimeUtcTicks -ne $approved.LastWriteTimeUtcTicks) { throw 'The selected duplicate identity changed. Scan again.' }
    $root = Get-SCMFileToolRoot -Root $approval.Root -ForDuplicates
    $canonicalFile = [IO.Path]::GetFullPath($approved.FilePath)
    if (-not (Test-SCMFileToolPathWithin $canonicalFile $root) -or $canonicalFile.Equals($root, [StringComparison]::OrdinalIgnoreCase)) { throw 'The selected file is outside its approved folder.' }
    if (Test-SCMFileToolExcludedPath -Path $canonicalFile -ExcludedRoots @(Get-SCMFileToolExcludedRoots)) { throw 'This file is in an excluded system, program, or application-data path.' }
    $recycleDrive = New-Object IO.DriveInfo([IO.Path]::GetPathRoot($canonicalFile))
    if ($recycleDrive.DriveType -ne [IO.DriveType]::Fixed) { throw 'Recycling is supported only on a fixed local drive. Review this file in Explorer instead.' }
    if (([IO.File]::GetAttributes($canonicalFile) -band [IO.FileAttributes]::ReadOnly) -ne 0) { throw 'Read-only files are not recycled by this tool.' }
    foreach ($policyPath in @('HKCU:\Software\Microsoft\Windows\CurrentVersion\Policies\Explorer','HKLM:\Software\Microsoft\Windows\CurrentVersion\Policies\Explorer')) {
        $policy = Get-ItemProperty -LiteralPath $policyPath -ErrorAction SilentlyContinue
        if ($policy -and $policy.NoRecycleFiles -eq 1) { throw 'A Windows policy disables Recycle Bin. Review the file in Explorer instead.' }
    }
    $binSettings = @('HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket')
    $volumeSettings = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket\Volume'
    if (Test-Path -LiteralPath $volumeSettings) { $binSettings += @(Get-ChildItem -LiteralPath $volumeSettings -ErrorAction SilentlyContinue | ForEach-Object { $_.PSPath }) }
    foreach ($binPath in $binSettings) {
        $binValue = Get-ItemProperty -LiteralPath $binPath -ErrorAction SilentlyContinue
        if ($binValue -and $binValue.NukeOnDelete -eq 1) { throw 'A Windows permanent-delete Recycle Bin setting is enabled. Review the file in Explorer instead.' }
    }
    $budget = [Diagnostics.Stopwatch]::StartNew()
    $verifiedHash = Get-SCMFileToolVerifiedHash -Record ([pscustomobject]$approved) -Budget $budget -MaxSeconds 120
    if ($verifiedHash -ne $approved.Fingerprint) { throw 'The selected file content changed. Scan again.' }
    $survivor = $false
    foreach ($otherPath in $approval.Groups[$approved.GroupKey]) {
        if ($otherPath.Equals($canonicalFile, [StringComparison]::OrdinalIgnoreCase)) { continue }
        $other = $approval.Records[$otherPath]
        if ($other.Recycled) { continue }
        try {
            if (-not (Test-SCMFileToolPathWithin $other.FilePath $root)) { continue }
            if (Test-SCMFileToolExcludedPath -Path $other.FilePath -ExcludedRoots @(Get-SCMFileToolExcludedRoots)) { continue }
            $otherHash = Get-SCMFileToolVerifiedHash -Record ([pscustomobject]$other) -Budget $budget -MaxSeconds 120
            if ($otherHash -eq $approved.Fingerprint) { $survivor = $true; break }
        } catch { }
    }
    if (-not $survivor) { throw 'No other unchanged matching copy remains from this scan. The last verified copy will not be recycled.' }
    # Recheck after sibling hashing, immediately before the shell recycle request.
    if ((Get-SCMFileToolVerifiedHash -Record ([pscustomobject]$approved) -Budget $budget -MaxSeconds 120) -ne $approved.Fingerprint) { throw 'The selected file changed during verification.' }
    $automaticProperty = $Record.PSObject.Properties['AutomaticApproved']
    if ($automaticProperty) {
        if ($automaticProperty.Value -isnot [bool] -or $automaticProperty.Value -ne $true) { throw 'Automatic recycling requires an explicit boolean saved-policy request.' }
        if (-not (Get-Command -Name Invoke-SCMSafeRecycleFile -CommandType Function -ErrorAction SilentlyContinue)) { throw 'The unattended Recycle Bin engine is unavailable; no alternate delete was attempted.' }
        $null = Invoke-SCMSafeRecycleFile -Path $canonicalFile
    } else {
        Add-Type -AssemblyName Microsoft.VisualBasic
        [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile($canonicalFile, [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs, [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin, [Microsoft.VisualBasic.FileIO.UICancelOption]::ThrowException)
    }
    if ([IO.File]::Exists($canonicalFile)) { throw 'The recycle request did not remove the selected file.' }
    $approved.Recycled = $true
    [pscustomobject]@{ FilePath=$canonicalFile; ScanId=$scanId; Recycled=$true; TimeUtc=[datetime]::UtcNow.ToString('o'); Note='Sent to Windows Recycle Bin. Restore it from Recycle Bin if needed; this function has no permanent-delete fallback.' }
}

function Get-SCMDriverInventory {
    [CmdletBinding()]
    param()
    @(Get-CimInstance -ClassName Win32_PnPSignedDriver -ErrorAction Stop | ForEach-Object {
        $date = ''
        if ($_.DriverDate) { try { $date=([datetime]$_.DriverDate).ToUniversalTime().ToString('o') } catch { $date=[string]$_.DriverDate } }
        [pscustomobject]@{ DeviceName=[string]$_.DeviceName; Manufacturer=[string]$_.Manufacturer; DriverVersion=[string]$_.DriverVersion; DriverDate=$date; InfName=[string]$_.InfName; IsSigned=[bool]$_.IsSigned; DeviceId=[string]$_.DeviceID }
    } | Sort-Object DeviceName,DriverVersion)
}

# Windows Update Agent operations. Loading defines functions; no scan/install runs.
function Publish-SCMDriverDiscoveryProgress {
    param([scriptblock]$ProgressCallback,
        [Parameter(Mandatory=$true)][ValidatePattern('^[a-z][a-z0-9_-]{0,47}$')][string]$Phase,
        $Percent=$null)
    if($null -eq $ProgressCallback){return}
    $value=$null
    if(($Percent -is [int] -or $Percent -is [long] -or $Percent -is [double]) -and
        $Percent -ge 0 -and $Percent -le 100 -and -not [double]::IsNaN([double]$Percent) -and
        -not [double]::IsInfinity([double]$Percent)){$value=[double]$Percent}
    try{$null=& $ProgressCallback ([pscustomobject]@{phase=$Phase;percent=$value})}catch{}
}
function Publish-SCMDriverCollectionProgress {
    param([scriptblock]$ProgressCallback,[string]$Phase,[int]$Completed,[int]$Total,[ref]$Previous)
    if($null -eq $ProgressCallback -or $Total -le 0){return}
    $percent=[int][math]::Floor(100.0*$Completed/$Total)
    # Final 100 is published only after the whole native phase validates.
    if($percent -ge 100 -or $percent -le $Previous.Value){return}
    $Previous.Value=$percent
    Publish-SCMDriverDiscoveryProgress -ProgressCallback $ProgressCallback -Phase $Phase -Percent $percent
}
function Get-SCMDeviceCoverageInventory {
    param([scriptblock]$ProgressCallback)
    # Read-only. Never invokes a device rescan, enable/disable or installer.
    $errors=New-Object 'Collections.Generic.List[object]'
    $devices=@();$drivers=@();$pnpComplete=$false;$driversComplete=$false
    Publish-SCMDriverDiscoveryProgress -ProgressCallback $ProgressCallback -Phase device_query
    try{$devices=@(Get-CimInstance -ClassName Win32_PnPEntity -ErrorAction Stop);$pnpComplete=$true}
    catch{$errors.Add([pscustomobject]@{source='Win32_PnPEntity';error=$_.Exception.Message})}
    Publish-SCMDriverDiscoveryProgress -ProgressCallback $ProgressCallback -Phase installed_driver_query
    try{$drivers=@(Get-CimInstance -ClassName Win32_PnPSignedDriver -ErrorAction Stop);$driversComplete=$true}
    catch{$errors.Add([pscustomobject]@{source='Win32_PnPSignedDriver';error=$_.Exception.Message})}
    $byDevice=@{};$driverDone=0;$driverPercent=-1
    Publish-SCMDriverDiscoveryProgress -ProgressCallback $ProgressCallback -Phase driver_records -Percent $(if($driversComplete){0}else{$null})
    foreach($driver in $drivers){
        $key=[string]$driver.DeviceID
        if($key){
            if(-not $byDevice.ContainsKey($key)){$byDevice[$key]=New-Object 'Collections.Generic.List[object]'}
            $date=Get-SCMWuaProperty $driver 'DriverDate' ''
            if($date){try{$date=([datetime]$date).ToUniversalTime().ToString('o')}catch{$date=[string]$date}}
            $byDevice[$key].Add([pscustomobject]@{
                version=[string]$driver.DriverVersion;date=[string]$date;inf=[string]$driver.InfName
                signed=(Get-SCMWuaProperty $driver 'IsSigned')
                provider=[string]$driver.DriverProviderName;manufacturer=[string]$driver.Manufacturer
                deviceClass=[string]$driver.DeviceClass
            })
        }
        $driverDone++
        Publish-SCMDriverCollectionProgress -ProgressCallback $ProgressCallback -Phase driver_records -Completed $driverDone -Total $drivers.Count -Previous ([ref]$driverPercent)
    }
    if($driversComplete){Publish-SCMDriverDiscoveryProgress -ProgressCallback $ProgressCallback -Phase driver_records -Percent 100}
    $excluded=0;$deviceDone=0;$devicePercent=-1
    Publish-SCMDriverDiscoveryProgress -ProgressCallback $ProgressCallback -Phase device_records -Percent $(if($pnpComplete){0}else{$null})
    $rows=@(foreach($device in $devices){
        $present=Get-SCMWuaProperty $device 'Present'
        if($null -ne $present -and $present -eq $false){$excluded++}else{
            $identity=[string]$device.PNPDeviceID
            if(-not $identity){$identity=[string]$device.DeviceID}
            $installed=@()
            if($identity -and $byDevice.ContainsKey($identity)){$installed=@($byDevice[$identity].ToArray())}
            $class=[string](Get-SCMWuaProperty $device 'PNPClass' '')
            if(-not $class -and $installed.Count){$class=[string]$installed[0].deviceClass}
            if(-not $class){$class=[string](Get-SCMWuaProperty $device 'ClassGuid' '')}
            [pscustomobject]@{
                deviceId=$identity;displayName=[string]$device.Name;deviceClass=$class
                manufacturer=[string]$device.Manufacturer;present=$present
                problemCode=(Get-SCMWuaProperty $device 'ConfigManagerErrorCode')
                deviceStatus=[string]$device.Status;service=[string]$device.Service
                hardwareIds=@($device.HardwareID | Where-Object {$_})
                compatibleIds=@($device.CompatibleID | Where-Object {$_})
                installedDrivers=@($installed)
            }
        }
        $deviceDone++
        Publish-SCMDriverCollectionProgress -ProgressCallback $ProgressCallback -Phase device_records -Completed $deviceDone -Total $devices.Count -Previous ([ref]$devicePercent)
    })
    if($pnpComplete){Publish-SCMDriverDiscoveryProgress -ProgressCallback $ProgressCallback -Phase device_records -Percent 100}
    $system=[pscustomobject]@{manufacturer='';model='';baseboardManufacturer='';baseboardModel=''}
    Publish-SCMDriverDiscoveryProgress -ProgressCallback $ProgressCallback -Phase system_query
    try{
        $computer=@(Get-CimInstance -ClassName Win32_ComputerSystem -ErrorAction Stop)
        if($computer.Count){$system.manufacturer=[string]$computer[0].Manufacturer;$system.model=[string]$computer[0].Model}
    }catch{$errors.Add([pscustomobject]@{source='Win32_ComputerSystem';error=$_.Exception.Message})}
    try{
        $board=@(Get-CimInstance -ClassName Win32_BaseBoard -ErrorAction Stop)
        if($board.Count){$system.baseboardManufacturer=[string]$board[0].Manufacturer;$system.baseboardModel=[string]$board[0].Product}
    }catch{$errors.Add([pscustomobject]@{source='Win32_BaseBoard';error=$_.Exception.Message})}
    $complete=($pnpComplete -and $driversComplete)
    Publish-SCMDriverDiscoveryProgress -ProgressCallback $ProgressCallback -Phase verify -Percent $(if($complete -and $errors.Count -eq 0){100}else{$null})
    [pscustomobject]@{
        capturedUtc=[datetime]::UtcNow.ToString('o');complete=$complete
        pnpComplete=$pnpComplete;driversComplete=$driversComplete;devices=@($rows)
        excludedNotPresent=$excluded;system=$system;errors=@($errors.ToArray())
    }
}

function Get-SCMWuaProperty {
    param($Object,[string]$Name,$Default=$null)
    try { $value=$Object.$Name; if($null -eq $value){return $Default};return $value }
    catch {return $Default}
}
function ConvertTo-SCMDriverUpdateDescriptor {
    param([Parameter(Mandatory=$true)]$Update)
    $behavior=Get-SCMWuaProperty $Update 'InstallationBehavior'
    $canInput=Get-SCMWuaProperty $behavior 'CanRequestUserInput'
    $reboot=Get-SCMWuaProperty $behavior 'RebootBehavior'
    $date=Get-SCMWuaProperty $Update 'DriverVerDate' ''
    if($date){try{$date=([datetime]$date).ToUniversalTime().ToString('o')}catch{$date=[string]$date}}
    [pscustomobject]@{
        id=[string]$Update.Identity.UpdateID;revision=[int]$Update.Identity.RevisionNumber;title=[string]$Update.Title
        downloaded=[bool]$Update.IsDownloaded;eulaAccepted=[bool]$Update.EulaAccepted
        hardwareId=[string](Get-SCMWuaProperty $Update 'DriverHardwareID' '')
        provider=[string](Get-SCMWuaProperty $Update 'DriverProvider' '')
        driverClass=[string](Get-SCMWuaProperty $Update 'DriverClass' '')
        manufacturer=[string](Get-SCMWuaProperty $Update 'DriverManufacturer' '')
        model=[string](Get-SCMWuaProperty $Update 'DriverModel' '')
        driverDate=[string]$date;canRequestUserInput=$canInput;rebootBehavior=$reboot
        optional=(Get-SCMWuaProperty $Update 'BrowseOnly');autoSelected=(Get-SCMWuaProperty $Update 'AutoSelectOnWebSites')
    }
}
function New-SCMWuaSession { New-Object -ComObject Microsoft.Update.Session }
function New-SCMWuaCollection { New-Object -ComObject Microsoft.Update.UpdateColl }
function Test-SCMWuaRebootPending {
    $system=New-Object -ComObject Microsoft.Update.SystemInfo
    return [bool]$system.RebootRequired
}
function Get-SCMDriverUpdateScan {
    param([scriptblock]$ProgressCallback)
    # Read-only search. Online explicitly requests the configured update service;
    # it never overrides organization policy or downloads/installs an update.
    $started=[datetime]::UtcNow.ToString('o');$clock=[Diagnostics.Stopwatch]::StartNew()
    $criteria="IsInstalled=0 and Type='Driver' and IsHidden=0"
    $updates=@();$errors=@();$warnings=@();$code=4;$online=$false;$server=$null;$service=''
    # ISearchJob exposes completion, not a numeric search-progress interface.
    # Keep the online WUA query unknown; never estimate it from elapsed time.
    # https://learn.microsoft.com/windows/win32/api/wuapi/nn-wuapi-isearchjob
    Publish-SCMDriverDiscoveryProgress -ProgressCallback $ProgressCallback -Phase scan
    try {
        $session=New-SCMWuaSession
        $searcher=$session.CreateUpdateSearcher()
        $searcher.Online=$true
        $online=[bool]$searcher.Online
        $server=Get-SCMWuaProperty $searcher 'ServerSelection'
        $service=[string](Get-SCMWuaProperty $searcher 'ServiceID' '')
        $result=$searcher.Search($criteria)
        if($null -eq $result -or $null -eq $result.Updates){throw 'Windows Update returned no usable driver offer collection.'}
        $code=[int]$result.ResultCode
        $offers=@($result.Updates);$offerDone=0;$offerPercent=-1
        Publish-SCMDriverDiscoveryProgress -ProgressCallback $ProgressCallback -Phase driver_offers -Percent 0
        $updates=@(foreach($update in $offers){
            ConvertTo-SCMDriverUpdateDescriptor -Update $update
            $offerDone++
            Publish-SCMDriverCollectionProgress -ProgressCallback $ProgressCallback -Phase driver_offers -Completed $offerDone -Total $offers.Count -Previous ([ref]$offerPercent)
        })
        $warnings=@(foreach($warning in @((Get-SCMWuaProperty $result 'Warnings' @()) | Select-Object -First 20)){
            [pscustomobject]@{source='Windows Update';message=[string](Get-SCMWuaProperty $warning 'Message' 'Update search reported a warning.');hresult=(Get-SCMWuaProperty $warning 'HResult')}
        })
    } catch {
        $errors=@([pscustomobject]@{source='Windows Update';error=$_.Exception.Message})
    }
    $clock.Stop()
    Publish-SCMDriverDiscoveryProgress -ProgressCallback $ProgressCallback -Phase driver_offers -Percent $(if($code -eq 2 -and $errors.Count -eq 0 -and $warnings.Count -eq 0){100}else{$null})
    [pscustomobject]@{
        updates=@($updates);source='Windows Update';resultCode=$code;includesOptional=$true
        started_utc=$started;completed_utc=[datetime]::UtcNow.ToString('o');duration_seconds=$clock.Elapsed.TotalSeconds
        online=$online;criteria=$criteria;server_selection=$server;service_id=$service
        returned_offer_count=$updates.Count;errors=@($errors);warnings=@($warnings)
        note='Online read-only search of applicable required and optional non-hidden drivers from the configured Windows Update service. No download or installation. OEM catalogs are not exhaustively searched. A hardware or compatible ID can cover multiple devices; matching rules are not a physical device instance identity.'
    }
}
function ConvertTo-SCMDriverDate {
    param($Value)
    if(-not $Value){throw 'A driver date is unavailable.'}
    try{return ([datetime]$Value).ToUniversalTime().Date}catch{
        try{return [Management.ManagementDateTimeConverter]::ToDateTime([string]$Value).ToUniversalTime().Date}catch{throw 'A driver date could not be verified.'}
    }
}
function Assert-SCMDriverNotOlderThanInstalled {
    param([Parameter(Mandatory=$true)]$Record,[Parameter(Mandatory=$true)]$Fresh)
    $candidate=ConvertTo-SCMDriverDate $Fresh.driverDate
    $floor=ConvertTo-SCMDriverDate $Record.notOlderThanDriverDate
    if($candidate -lt $floor){throw 'The offered driver predates the approved device-rule date floor.'}
    $hardware=[string]$Fresh.hardwareId
    if(-not $hardware){throw 'The offered driver has no exact hardware match metadata.'}
    $devices=@(Get-CimInstance -ClassName Win32_PnPEntity -ErrorAction Stop | Where-Object {
        (@($_.HardwareID)+@($_.CompatibleID)) -icontains $hardware
    })
    if(-not $devices.Count){throw 'The approved hardware/compatible ID could not be matched to current local devices.'}
    $installed=@(Get-CimInstance -ClassName Win32_PnPSignedDriver -ErrorAction Stop)
    foreach($device in $devices){
        $identity=if($device.PNPDeviceID){[string]$device.PNPDeviceID}else{[string]$device.DeviceID}
        $matches=@($installed | Where-Object {$_.DeviceID -ieq $identity})
        if(-not $identity -or -not $matches.Count){throw 'An installed matching device driver could not be verified.'}
        foreach($driver in $matches){
            $date=ConvertTo-SCMDriverDate $driver.DriverDate
            if($candidate -lt $date){throw 'The offered driver is older than an installed matching device driver. Automatic downgrade was refused.'}
        }
    }
}
function Assert-SCMDriverQuietMode {
    param([Parameter(Mandatory=$true)]$Installer)
    if($Installer.ForceQuiet -isnot [bool] -or $Installer.ForceQuiet -ne $true -or
       $Installer.AllowSourcePrompts -isnot [bool] -or $Installer.AllowSourcePrompts -ne $false){
        throw 'Windows Update could not establish quiet installation with source prompts disabled.'
    }
}
function New-SCMDriverQuietRefusal {
    param([Parameter(Mandatory=$true)]$Record,[int]$HResult,[bool]$InstallerStarted=$false,[bool]$RestartNeeded=$false)
    [pscustomobject]@{
        id=$Record.id;revision=$Record.revision;title=$Record.title
        resultCode=4;aggregateResultCode=4;hresult=$HResult;aggregateHresult=$HResult
        restartNeeded=$RestartNeeded;unattended=$true
        installer_started=$InstallerStarted;outcomeKnown=$true;running=$false
        requiresReview=$true;failure_scope='package';failureKind='quiet_mode_unavailable'
        note='Windows Update could not complete the approved driver in quiet mode. Review this update in Windows Update. No interactive fallback or forced installation was started.'
    }
}
function Get-SCMDriverPreflightHResult {
    param([Parameter(Mandatory=$true)]$Exception)
    # PowerShell can wrap a COM property failure. Keep an authorization/busy/
    # service HRESULT from any native inner exception instead of masking it
    # with the generic MethodInvocationException HRESULT.
    $failure=$Exception;$code=[int]$Exception.HResult
    for($depth=0;$null -ne $failure -and $depth -lt 8;$depth++){
        $code=[int]$failure.HResult
        if($code -in @([int]0x80240016,[int]0x80240018,[int]0x80240019,[int]0x8024001B,
            [int]0x8024001E,[int]0x8024001F,[int]0x80240020,[int]0x80070005,[int]0x80070070,[int]0x8A150102,1618)){
            return $code
        }
        $failure=$failure.InnerException
    }
    return $code
}
function New-SCMDriverPreflightRefusal {
    param([Parameter(Mandatory=$true)]$Record,
        [Parameter(Mandatory=$true)][ValidateSet('package_unavailable','version_changed','unsupported_installer','download_failed')][string]$FailureKind,
        [Parameter(Mandatory=$true)][string]$Note,[int]$HResult=0,[int]$AggregateHResult=0,
        [ValidateSet(2,3,4,5)][int]$ResultCode=4,[ValidateSet(2,3,4,5)][int]$AggregateResultCode=4,
        [bool]$RestartNeeded=$false)
    # Only callers before Install() may use this no-launch receipt. Preserve
    # native download HRESULTs so shared authorization/service/busy failures
    # override package scope in the Python installer gate.
    [pscustomobject]@{
        id=$Record.id;revision=$Record.revision;title=$Record.title
        resultCode=$ResultCode;aggregateResultCode=$AggregateResultCode
        hresult=$HResult;aggregateHresult=$AggregateHResult;restartNeeded=$RestartNeeded
        unattended=$true;installer_started=$false;outcomeKnown=$true;running=$false
        requiresReview=$true;failure_scope='package';failureKind=$FailureKind
        note=$Note.Substring(0,[Math]::Min(2000,$Note.Length))
    }
}
function Invoke-SCMDriverUpdateApplyCore {
    param([Parameter(Mandatory=$true)]$Record,[bool]$Unattended=$false,[bool]$AcceptEula=$false,[scriptblock]$ProgressCallback)
    if(-not(Test-SCMAdministrator)){throw 'Administrator access is required; no automatic elevation was requested.'}
    if($Unattended -and -not $AcceptEula){throw 'Saved driver license agreement approval is required for unattended installation.'}
    $parsed=[guid]::Empty
    if(-not [guid]::TryParse([string]$Record.id,[ref]$parsed) -or $Record.revision -isnot [int] -or $Record.revision -lt 0){throw 'Invalid exact Windows Update driver identity or revision.'}
    $session=New-SCMWuaSession
    $result=$session.CreateUpdateSearcher().Search("IsInstalled=0 and Type='Driver' and IsHidden=0")
    if([int]$result.ResultCode -ne 2){throw 'Windows Update did not return a complete successful applicability scan.'}
    $selected=@($result.Updates | Where-Object {$_.Identity.UpdateID -ieq $parsed.ToString() -and $_.Identity.RevisionNumber -eq $Record.revision})
    if($selected.Count -ne 1){
        if($Unattended){return New-SCMDriverPreflightRefusal -Record $Record -FailureKind package_unavailable -Note 'The exact approved update is changed, ambiguous, installed or no longer applicable. No installer was started; other approved packages may be checked.'}
        throw 'The exact approved update is changed, ambiguous, installed or no longer applicable.'
    }
    $update=$selected[0]
    $fresh=ConvertTo-SCMDriverUpdateDescriptor -Update $update
    foreach($field in @('title','hardwareId','provider','driverClass','manufacturer','model','driverDate')){
        if($Record.PSObject.Properties.Name -contains $field -and [string]$Record.$field -cne [string]$fresh.$field){
            if($Unattended){return New-SCMDriverPreflightRefusal -Record $Record -FailureKind version_changed -Note 'The approved driver title, device or publisher metadata changed. No installer was started; review this exact update again.'}
            throw 'The approved driver title, device or publisher metadata changed. Review the update again.'
        }
    }
    if ($Unattended -and [string]$Record.approvalMode -eq 'matching') { Assert-SCMDriverNotOlderThanInstalled -Record $Record -Fresh $fresh }
    $collection=New-SCMWuaCollection
    [void]$collection.Add($update)
    $installer=$session.CreateUpdateInstaller()
    $installer.Updates=$collection
    $installer.IsForced=$false
    if($Unattended){
        if($fresh.rebootBehavior -isnot [int] -or $fresh.rebootBehavior -notin @(0,2)){throw 'Unknown reboot metadata or mandatory-restart updates are excluded from unattended installation.'}
        if($fresh.canRequestUserInput -isnot [bool]){
            return New-SCMDriverPreflightRefusal -Record $Record -FailureKind unsupported_installer -Note 'This exact driver has unverified interaction capability. No installer or interactive fallback was started; review this package in Windows Update.'
        }
        # CanRequestUserInput=True can still support ForceQuiet. This documented
        # WUA mode suppresses interaction; it does not force an incompatible
        # package or bypass Windows security. Missing quiet support fails closed.
        try {
            $installer.ForceQuiet=$true
            $installer.AllowSourcePrompts=$false
            Assert-SCMDriverQuietMode -Installer $installer
        } catch {
            return New-SCMDriverQuietRefusal -Record $fresh -HResult (Get-SCMDriverPreflightHResult $_.Exception)
        }
        if($installer.RebootRequiredBeforeInstallation -or (Test-SCMWuaRebootPending)){throw 'A pending Windows restart must be completed before automated driver installation.'}
    }
    if($installer.IsBusy){throw 'Another Windows update installation is already running.'}
    if(-not $update.EulaAccepted){
        if(-not ($Unattended -and $AcceptEula)){throw 'This driver requires explicit license agreement approval. Use Windows Update or approve this update in setup.'}
        $update.AcceptEula()
        if(-not $update.EulaAccepted){throw 'Windows Update did not accept the approved driver license agreement.'}
    }
    $downloader=$session.CreateUpdateDownloader()
    $downloader.Updates=$collection
    $downloader.IsForced=$false
    if($Unattended){
        try { Assert-SCMDriverQuietMode -Installer $installer }
        catch { return New-SCMDriverQuietRefusal -Record $fresh -HResult (Get-SCMDriverPreflightHResult $_.Exception) }
    }
    $downloadState = @{last=$null}
    $download=if($null -ne $ProgressCallback){
        $displayCallback = $ProgressCallback
        # Windows PowerShell 5.1 closures use a dynamic module: retain the
        # normalizer itself instead of relying on script-scope name lookup.
        $byteNormalizer = ${function:ConvertTo-SCMProgressByteCount}
        $downloadCallback = {
            param($Progress)
            if ($Progress.phase -eq 'downloading' -and $null -ne $Progress.downloaded_bytes) {
                $done = & $byteNormalizer $Progress.downloaded_bytes
                $total = & $byteNormalizer $Progress.download_total_bytes
                if ($null -ne $done -and ($null -eq $Progress.download_total_bytes -or ($null -ne $total -and $total -ge $done))) {
                    if ($null -eq $downloadState.last -or $done -ge $downloadState.last.downloaded_bytes) {
                        $saved = [ordered]@{phase='downloading';percent=$Progress.percent;
                            downloaded_bytes=$done;bytes_source='windows_update';bytes_estimated=$false}
                        if ($null -ne $total) { $saved.download_total_bytes=$total }
                        $downloadState.last = [pscustomobject]$saved
                    }
                }
            }
            try { $null = & $displayCallback $Progress } catch { }
        }.GetNewClosure()
        Invoke-SCMUpdateProgress -Owner $downloader -Phase Download -ProgressCallback $downloadCallback
    }else{$downloader.Download()}
    $downloadItem=$download.GetUpdateResult(0)
    # https://learn.microsoft.com/windows/win32/wua_sdk/idownloadresult-properties
    # https://learn.microsoft.com/windows/win32/api/wuapi/nn-wuapi-iupdatedownloadresult
    # Synchronous download completion needs both native terminal result codes
    # and typed HRESULTs. Missing/in-progress results and COM throws stay closed.
    if($null -eq $download -or $null -eq $downloadItem -or
       $download.ResultCode -isnot [int] -or $download.ResultCode -notin @(2,3,4,5) -or
       $downloadItem.ResultCode -isnot [int] -or $downloadItem.ResultCode -notin @(2,3,4,5) -or
       $download.HResult -isnot [int] -or $downloadItem.HResult -isnot [int]){
        throw 'Windows Update returned an incomplete or nonterminal driver download result. No driver installation was started; verify Windows Update before continuing.'
    }
    if($download.ResultCode -ne 2 -or $downloadItem.ResultCode -ne 2 -or $downloadItem.HResult -ne 0 -or $download.HResult -ne 0){
        if($Unattended){
            $refusal = New-SCMDriverPreflightRefusal -Record $fresh -FailureKind download_failed -ResultCode $downloadItem.ResultCode -AggregateResultCode $download.ResultCode -HResult $downloadItem.HResult -AggregateHResult $download.HResult -RestartNeeded (Test-SCMWuaRebootPending) -Note 'Windows Update returned a terminal failure downloading this driver. No driver installer was started. The exact download result is retained; no retry or forced installation was requested.'
            if ($null -ne $downloadState.last) { $refusal | Add-Member -NotePropertyName download_progress -NotePropertyValue $downloadState.last }
            return $refusal
        }
        throw 'Windows Update did not completely download the selected driver.'
    }
    if($Unattended -and ($installer.RebootRequiredBeforeInstallation -or (Test-SCMWuaRebootPending))){throw 'A restart became pending before installation. The downloaded update was not installed.'}
    if($installer.IsBusy){throw 'Another Windows update installation started. The selected driver was not installed.'}
    if($Unattended){
        try { Assert-SCMDriverQuietMode -Installer $installer }
        catch { return New-SCMDriverQuietRefusal -Record $fresh -HResult (Get-SCMDriverPreflightHResult $_.Exception) }
    }
    try {
        $installed=if($null -ne $ProgressCallback){
            Invoke-SCMUpdateProgress -Owner $installer -Phase Install -ProgressCallback $ProgressCallback
        }else{$installer.Install()}
    } catch {
        # These exact synchronous handler rejections establish a terminal quiet
        # failure. All other COM errors/timeouts remain unknown to the caller.
        $failure=$_.Exception;$quietHResult=$null
        for($depth=0;$null -ne $failure -and $depth -lt 8;$depth++){
            if([int]$failure.HResult -in @(-2145116156,-2145116150)){$quietHResult=[int]$failure.HResult;break}
            $failure=$failure.InnerException
        }
        if($Unattended -and $null -ne $quietHResult){
            return New-SCMDriverQuietRefusal -Record $fresh -HResult $quietHResult -InstallerStarted $true -RestartNeeded (Test-SCMWuaRebootPending)
        }
        throw
    }
    $item=$installed.GetUpdateResult(0)
    # A synchronous return is terminal only with complete native result fields.
    # Missing HRESULTs must not become zero through PowerShell conversion, and
    # NotStarted/InProgress must never become an idle, known-outcome receipt.
    if($null -eq $installed -or $null -eq $item -or
       $item.ResultCode -isnot [int] -or $item.ResultCode -notin @(2,3,4,5) -or
       $installed.ResultCode -isnot [int] -or $installed.ResultCode -notin @(2,3,4,5) -or
       $item.HResult -isnot [int] -or $installed.HResult -isnot [int] -or
       $item.RebootRequired -isnot [bool] -or $installed.RebootRequired -isnot [bool]){
        throw 'Windows Update returned an incomplete or nonterminal driver installation result. Verify installer state before continuing.'
    }
    $receipt = [ordered]@{
        id=$fresh.id;revision=$fresh.revision;title=$fresh.title
        resultCode=[int]$item.ResultCode;aggregateResultCode=[int]$installed.ResultCode
        restartNeeded=([bool]$installed.RebootRequired -or [bool]$item.RebootRequired)
        hresult=[int]$item.HResult;aggregateHresult=[int]$installed.HResult
        unattended=$Unattended;installer_started=$true;outcomeKnown=$true;running=$false
        requiresReview=([int]$item.ResultCode -ne 2 -or [int]$item.HResult -ne 0 -or [int]$installed.ResultCode -ne 2 -or [int]$installed.HResult -ne 0)
        note='Windows Update returned the selected applicable driver installation result. No interactive fallback, forced installation, reboot API or automatic elevation was invoked.'
    }
    if ($null -ne $downloadState.last) { $receipt.download_progress = $downloadState.last }
    [pscustomobject]$receipt
}

function Get-SCMDriverPayloadCleanupReport {
    param($Outcome,[bool]$RequestedEnabled=$true)
    # WUA downloads into its own cache; this provider never creates Optimus-
    # owned driver installers. IUpdate has no scoped cache-delete method:
    # https://learn.microsoft.com/windows/win32/api/wuapi/nn-wuapi-iupdate
    # Delivery Optimization supports -FileId, but this WUA flow supplies no
    # proven binding to an exclusively owned DO file. Do not infer ownership
    # from update GUIDs, URLs, timestamps or a before/after cache inventory.
    # https://learn.microsoft.com/windows/deployment/do/waas-delivery-optimization-monitor
    $status='retained_unverified'
    $reason='Driver download cleanup was not attempted: installation completion is unverified; Windows retains its update files.'
    if(-not $RequestedEnabled){
        $status='disabled';$reason='Automatic update-download cleanup is off in Settings. Windows manages its driver download cache.'
    }elseif($null -ne $Outcome -and $Outcome.restartNeeded -is [bool] -and $Outcome.restartNeeded){
        $status='retained_pending_restart';$reason='Windows reports a restart is needed. Driver download files remain managed by Windows.'
    }elseif($null -ne $Outcome -and $Outcome.outcomeKnown -is [bool] -and $Outcome.outcomeKnown -and
        $Outcome.running -is [bool] -and -not $Outcome.running -and
        $Outcome.installer_started -is [bool] -and $Outcome.installer_started -and
        $Outcome.resultCode -is [int] -and $Outcome.resultCode -eq 2 -and
        $Outcome.aggregateResultCode -is [int] -and $Outcome.aggregateResultCode -eq 2 -and
        $Outcome.hresult -is [int] -and $Outcome.hresult -eq 0 -and
        $Outcome.aggregateHresult -is [int] -and $Outcome.aggregateHresult -eq 0 -and
        $Outcome.restartNeeded -is [bool] -and -not $Outcome.restartNeeded){
        $status='os_managed'
        $reason='The driver installation returned success. Windows manages these download files; Optimus cannot safely remove files for only this update. No driver payload was deleted.'
    }
    [pscustomobject]@{
        provider='windows_update_agent';ownership='operating_system';supported=$false
        requested_enabled=$RequestedEnabled;status=$status;removed_files=0;removed_bytes=0
        reason=$reason
    }
}

function Invoke-SCMDriverUpdateApply {
    param([Parameter(Mandatory=$true)]$Record,[bool]$Unattended=$false,[bool]$AcceptEula=$false,
        [scriptblock]$ProgressCallback,[bool]$CleanupDownloads=$true)
    # Preserve the installer's original exceptions and all restart/quiet-mode/
    # applicability checks. A failed or unknown operation authorizes no cleanup.
    $outcome=Invoke-SCMDriverUpdateApplyCore -Record $Record -Unattended $Unattended -AcceptEula $AcceptEula -ProgressCallback $ProgressCallback
    if($null -ne $outcome){
        $outcome | Add-Member -NotePropertyName payload_cleanup -NotePropertyValue (Get-SCMDriverPayloadCleanupReport -Outcome $outcome -RequestedEnabled $CleanupDownloads)
    }
    return $outcome
}

