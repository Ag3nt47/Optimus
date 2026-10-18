#requires -Version 5.1
[CmdletBinding()]
param([string]$FixtureRoot)
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot '..\src\optimus\engine\FileTools.ps1')
if (-not $FixtureRoot) { $FixtureRoot=Join-Path (Join-Path $PSScriptRoot 'test-artifacts') ('OptimusFileFixture-'+[guid]::NewGuid().ToString('N')) }
$FixtureRoot=[IO.Path]::GetFullPath($FixtureRoot)
[void](New-Item -ItemType Directory -Path $FixtureRoot -Force)
$checks=New-Object 'System.Collections.Generic.List[object]'
function Confirm-SCMFileTest {
    param([string]$Name,[bool]$Pass,[string]$Detail)
    $checks.Add([pscustomobject]@{Check=$Name;Pass=$Pass;Detail=$Detail})
    if (-not $Pass) { throw ($Name+': '+$Detail) }
}
try {
    $folderA=Join-Path $FixtureRoot 'Alpha'
    $folderB=Join-Path $FixtureRoot 'Beta'
    [void](New-Item -ItemType Directory -Path $folderA,$folderB -Force)
    $first=Join-Path $folderA 'first.bin'
    $second=Join-Path $folderB 'different-name.bin'
    $different=Join-Path $folderB 'same-length-different.bin'
    [IO.File]::WriteAllBytes($first,([Text.Encoding]::ASCII.GetBytes(('A'*4096))))
    [IO.File]::WriteAllBytes($second,([Text.Encoding]::ASCII.GetBytes(('A'*4096))))
    [IO.File]::WriteAllBytes($different,([Text.Encoding]::ASCII.GetBytes(('B'*4096))))
    [IO.File]::WriteAllText((Join-Path $FixtureRoot 'small.txt'),'seven77')
    $disk=Get-SCMStorageAnalysis -Root $FixtureRoot -MaxFiles 100 -MaxSeconds 15
    Confirm-SCMFileTest 'Storage totals' ($disk.FilesScanned -eq 4 -and $disk.TotalBytes -eq 12295 -and $disk.Complete) 'Known fixture file sizes and complete metadata traversal.'
    Confirm-SCMFileTest 'Storage groups' (@($disk.ExtensionGroups | Where-Object Extension -eq '.bin')[0].Bytes -eq 12288 -and @($disk.DirectoryGroups | Where-Object Directory -eq 'Beta')[0].Files -eq 2) 'Extension and first-level directory aggregation.'
    $limited=Get-SCMStorageAnalysis -Root $FixtureRoot -MaxFiles 1 -MaxSeconds 15
    Confirm-SCMFileTest 'File-count bound' ($limited.FilesScanned -eq 1 -and $limited.LimitReached -and -not $limited.Complete) 'Limited scans are explicitly partial.'
    $duplicates=Get-SCMDuplicateFiles -Root $FixtureRoot -MinBytes 1024 -MaxFiles 100 -MaxSeconds 15
    Confirm-SCMFileTest 'Hash-content duplicate detection' ($duplicates.Groups.Count -eq 1 -and $duplicates.Groups[0].Paths.Count -eq 2 -and $duplicates.FilesHashed -eq 3) 'Different names match by SHA256; equal-sized different bytes do not match.'
    Confirm-SCMFileTest 'Duplicate preview does not remove files' ((Test-Path -LiteralPath $first) -and (Test-Path -LiteralPath $second) -and (Test-Path -LiteralPath $different)) 'All fixture files still exist after preview.'
    $candidate=@($duplicates.Candidates | Where-Object FilePath -eq $first)[0]
    $forged=$candidate.PSObject.Copy(); $forged.ScanId='unapproved'
    $rejected=$false; try { [void](Move-SCMFileToRecycleBin -Record $forged) } catch { $rejected=$true }
    Confirm-SCMFileTest 'Unapproved record blocked' ($rejected -and (Test-Path -LiteralPath $first)) 'A caller cannot invent a scan approval.'
    $tampered=$candidate.PSObject.Copy(); $tampered.FilePath=$different
    $rejected=$false; try { [void](Move-SCMFileToRecycleBin -Record $tampered) } catch { $rejected=$true }
    Confirm-SCMFileTest 'Unapproved file blocked' ($rejected -and (Test-Path -LiteralPath $different)) 'An unrelated same-sized file is not an approved duplicate.'
    $rejected=$false; try { [void](Get-SCMDuplicateFiles -Root ([IO.Path]::GetPathRoot($FixtureRoot)) -MaxSeconds 1) } catch { $rejected=$true }
    Confirm-SCMFileTest 'Drive root rejected for duplicates' $rejected 'Duplicate scans require a selected personal-data folder.'
    $rejected=$false; try { [void](Get-SCMDuplicateFiles -Root $env:WINDIR -MaxSeconds 1) } catch { $rejected=$true }
    Confirm-SCMFileTest 'Windows root rejected' $rejected 'Protected system folder cannot become an approved recycle root.'
    $mutatedTime=[IO.File]::GetLastWriteTimeUtc($first)
    [IO.File]::WriteAllBytes($first,([Text.Encoding]::ASCII.GetBytes(('C'*4096))))
    [IO.File]::SetLastWriteTimeUtc($first,$mutatedTime)
    $rejected=$false; try { [void](Move-SCMFileToRecycleBin -Record $candidate) } catch { $rejected=$true }
    Confirm-SCMFileTest 'Content change with same identity blocked' ($rejected -and (Test-Path -LiteralPath $first)) 'Rehash catches changed bytes even when length and timestamp are preserved.'
    [IO.File]::WriteAllBytes($first,([Text.Encoding]::ASCII.GetBytes(('A'*4096))))
    $duplicates=Get-SCMDuplicateFiles -Root $FixtureRoot -MinBytes 1024 -MaxFiles 100 -MaxSeconds 15
    $selected=@($duplicates.Candidates | Where-Object FilePath -eq $first)[0]
    $remaining=@($duplicates.Candidates | Where-Object FilePath -eq $second)[0]
    $recycled=Move-SCMFileToRecycleBin -Record $selected
    Confirm-SCMFileTest 'One fixture duplicate recycled' ($recycled.Recycled -and -not (Test-Path -LiteralPath $first) -and (Test-Path -LiteralPath $second)) 'Only the explicitly selected disposable fixture duplicate goes to Windows Recycle Bin.'
    $rejected=$false; try { [void](Move-SCMFileToRecycleBin -Record $remaining) } catch { $rejected=$true }
    Confirm-SCMFileTest 'Last matching copy preserved' ($rejected -and (Test-Path -LiteralPath $second)) 'The surviving unchanged duplicate cannot be recycled by this scan.'
    $cloud=Test-SCMFileToolCloudAttributes ([long]0x400000)
    Confirm-SCMFileTest 'Cloud placeholder attribute recognized' $cloud 'Recall-on-data-access files are skipped without opening their content.'
    $driverRows=@(Get-SCMDriverInventory)
    Confirm-SCMFileTest 'Driver inventory read-only' ($driverRows.Count -gt 0 -and 'DriverVersion' -in $driverRows[0].PSObject.Properties.Name) ($driverRows.Count.ToString()+' driver records; no installs or updates.')
} catch { $checks.Add([pscustomobject]@{Check='Completion';Pass=$false;Detail=$_.Exception.Message}) }
$checks.ToArray() | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $PSScriptRoot 'file-tools-tests.json') -Encoding UTF8
$checks.ToArray() | Format-Table -AutoSize | Out-String | Write-Output
Write-Output ('Fixture root: '+$FixtureRoot)
if (@($checks | Where-Object {-not $_.Pass}).Count) { exit 1 }
exit 0


