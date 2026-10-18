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
    Add-Type -AssemblyName Microsoft.VisualBasic
    [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile($canonicalFile, [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs, [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin, [Microsoft.VisualBasic.FileIO.UICancelOption]::ThrowException)
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
