<#
.SYNOPSIS
Read-only available-space targets for every mounted local fixed Windows volume.
.DESCRIPTION
Reports free space, the selected 1-2 GiB target and any shortfall. The target is
available space, not an allocated reservation. This script performs no cleanup.
#>
[CmdletBinding()]
param(
    [ValidateRange(1.0, 2.0)]
    [double]$TargetGiB = 2.0,
    [switch]$AsJson
)

$ErrorActionPreference = 'Stop'
if ([double]::IsNaN($TargetGiB) -or [double]::IsInfinity($TargetGiB)) {
    throw 'Choose a finite available-space target from 1 to 2 GiB.'
}
[long]$guardTargetBytes = [math]::Ceiling($TargetGiB * 1GB)
$guardRows = [Collections.Generic.List[object]]::new()
$guardWarnings = [Collections.Generic.List[string]]::new()
$guardErrors = [Collections.Generic.List[object]]::new()
$guardSeen = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)

try {
    $guardVolumes = @(Get-CimInstance -ClassName Win32_Volume -Filter 'DriveType = 3' -ErrorAction Stop)
    foreach ($guardVolume in $guardVolumes) {
        $guardMounts = @()
        if ([string]$guardVolume.DriveLetter -match '^[A-Za-z]:$') {
            $guardMounts = @(([string]$guardVolume.DriveLetter) + '\')
        } else {
            try {
                $guardMounts = @(Get-CimAssociatedInstance -InputObject $guardVolume -Association Win32_MountPoint `
                    -ResultClassName Win32_Directory -ErrorAction Stop | ForEach-Object { [string]$_.Name })
            } catch {
                $guardErrors.Add([pscustomobject]@{operation='mounted-folder inventory';error_type=$_.Exception.GetType().Name})
                $guardWarnings.Add('A fixed-volume mounted-folder inventory was unavailable; its buffer is unverified.')
                continue
            }
        }
        foreach ($guardMount in $guardMounts) {
            if ($guardMount -notmatch '^[A-Za-z]:\\' -or $guardMount.IndexOfAny([char[]]"`r`n`0") -ge 0) { continue }
            if (-not $guardSeen.Add($guardMount.TrimEnd('\'))) { continue }
            $guardName = $guardMount.TrimEnd('\')
            $guardRow = [ordered]@{mountpoint=$guardMount;name=$guardName;device=[string]$guardVolume.DeviceID;
                filesystem=[string]$guardVolume.FileSystem;free_bytes=$null;total_bytes=$null;
                target_bytes=$guardTargetBytes;shortfall_bytes=$null;below_target=$null;status='unknown';warning=$null}
            try {
                if ($null -eq $guardVolume.FreeSpace -or $null -eq $guardVolume.Capacity) {
                    throw 'Native free-space values are missing.'
                }
                [decimal]$guardFreeValue = $guardVolume.FreeSpace
                [decimal]$guardTotalValue = $guardVolume.Capacity
                if ($guardFreeValue -lt 0 -or $guardTotalValue -le 0 -or $guardFreeValue -gt $guardTotalValue -or
                    $guardTotalValue -gt [long]::MaxValue -or [math]::Truncate($guardFreeValue) -ne $guardFreeValue -or
                    [math]::Truncate($guardTotalValue) -ne $guardTotalValue) {
                    throw 'Native free-space values are inconsistent.'
                }
                [long]$guardFree = $guardFreeValue
                [long]$guardTotal = $guardTotalValue
                [long]$guardShortfall = [math]::Max([long]0, [long]($guardTargetBytes - $guardFree))
                $guardRow.free_bytes = $guardFree
                $guardRow.total_bytes = $guardTotal
                $guardRow.shortfall_bytes = $guardShortfall
                $guardRow.below_target = ($guardShortfall -gt 0)
                $guardRow.status = if ($guardShortfall -gt 0) { 'below_target' } else { 'ok' }
                if ($guardShortfall -gt 0) {
                    $guardRow.warning = ('{0}: {1:N2} GiB free; target {2:N2} GiB (shortfall {3:N2} GiB).' -f
                        $guardName, ($guardFree / 1GB), $TargetGiB, ($guardShortfall / 1GB))
                }
            } catch {
                $guardRow.warning = $guardName + ': free space is unknown; its buffer could not be verified.'
                $guardErrors.Add([pscustomobject]@{mountpoint=$guardMount;operation='local free-space check';
                    error_type=$_.Exception.GetType().Name})
            }
            if ($guardRow.warning) { $guardWarnings.Add([string]$guardRow.warning) }
            $guardRows.Add([pscustomobject]$guardRow)
        }
    }
} catch {
    $guardErrors.Add([pscustomobject]@{operation='fixed-volume inventory';error_type=$_.Exception.GetType().Name})
    $guardWarnings.Add('Mounted fixed-volume inventory was unavailable; drive-space buffers are unverified.')
}

if ($guardRows.Count -eq 0 -and $guardErrors.Count -eq 0) {
    $guardErrors.Add([pscustomobject]@{operation='fixed-volume inventory';error_type='NoMountedFixedVolumes'})
    $guardWarnings.Add('No mounted local fixed volumes were reported; drive-space buffers are unverified.')
}
$guardBelow = @($guardRows | Where-Object { $_.below_target -eq $true }).Count
$guardUnknown = @($guardRows | Where-Object { $null -eq $_.below_target }).Count
$guardReport = [pscustomobject]@{
    schema=1;enabled=$true;review_only=$true;target_gib=$TargetGiB;target_bytes=$guardTargetBytes;
    complete=($guardErrors.Count -eq 0);status=$(if($guardErrors.Count -gt 0 -or $guardBelow -gt 0){'needs_review'}else{'completed'});
    checked_at_utc=[datetime]::UtcNow.ToString('o');volumes=@($guardRows.ToArray());
    warnings=@($guardWarnings.ToArray());errors=@($guardErrors.ToArray());
    summary=('Drive-space buffers: {0} local volumes checked; {1} below {2:N2} GiB; {3} unknown.' -f
        $guardRows.Count,$guardBelow,$TargetGiB,$guardUnknown);
    note='Available-space targets only. No storage is reserved and no files, partitions or pagefile settings are changed.'
}
foreach ($guardWarning in $guardWarnings) { Write-Warning $guardWarning }
if ($AsJson) { $guardReport | ConvertTo-Json -Depth 8 } else { $guardReport }
