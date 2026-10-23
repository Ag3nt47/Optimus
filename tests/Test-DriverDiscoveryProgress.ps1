# Offline fake WUA/CIM fixtures only. Never creates a native update session.
$ErrorActionPreference='Stop'
$base=. (Join-Path $PSScriptRoot 'Test-DriverCoverage.ps1')
$baseReport=$base|ConvertFrom-Json
$script:Assertions=0
function Assert-Discovery {param([bool]$Condition,[string]$Message)
    if(-not $Condition){throw ('FAIL: '+$Message)};$script:Assertions++}
$script:Events=New-Object 'Collections.Generic.List[object]'
$observer={param($Value) $script:Events.Add($Value)}
function Reset-SearchFixture {
    $script:Events.Clear()
    $script:SearchCode=[int]2;$script:SearchThrows=$false;$script:SearchMalformed=$false;$script:SearchWarning=$false
    $script:Offers=@(1,2|ForEach-Object {
        [pscustomobject]@{Identity=[pscustomobject]@{UpdateID=('00000000-0000-0000-0000-00000000000'+$_);RevisionNumber=[int]1}
            Title=('Fixture driver '+$_);IsDownloaded=$false;EulaAccepted=$true
            DriverHardwareID=('PCI\fixture-'+$_);DriverProvider='Fixture provider';DriverClass='System'
            DriverManufacturer='Fixture';DriverModel='Fixture';DriverVerDate=[datetime]'2026-10-01'
            InstallationBehavior=[pscustomobject]@{CanRequestUserInput=$false;RebootBehavior=[int]0}}
    })
    $script:FixtureSearcher|Add-Member ScriptMethod Search {param($query)
        Assert-Discovery ($query -ceq "IsInstalled=0 and Type='Driver' and IsHidden=0") 'exact read-only query unchanged'
        Assert-Discovery ($script:Events.Count -eq 0 -or ($script:Events[-1].phase -eq 'scan' -and $null -eq $script:Events[-1].percent)) 'opaque query indeterminate'
        if($script:SearchThrows){throw 'Fixture search unavailable'}
        if($script:SearchMalformed){return [pscustomobject]@{ResultCode=[int]2}}
        $warnings=if($script:SearchWarning){@([pscustomobject]@{Message='Fixture warning';HResult=5})}else{@()}
        return [pscustomobject]@{ResultCode=$script:SearchCode;Updates=@($script:Offers);Warnings=@($warnings)}
    } -Force
}
Reset-SearchFixture;$plain=Get-SCMDriverUpdateScan
Reset-SearchFixture;$report=Get-SCMDriverUpdateScan -ProgressCallback $observer
Assert-Discovery ($report.resultCode -eq 2 -and $report.updates.Count -eq 2) 'observer preserves native report'
Assert-Discovery (($report.updates|ConvertTo-Json -Depth 8 -Compress) -eq ($plain.updates|ConvertTo-Json -Depth 8 -Compress)) 'descriptors unchanged'
Assert-Discovery ($report.criteria -eq $plain.criteria -and $report.note -eq $plain.note) 'scope unchanged'
Assert-Discovery ($script:Events[0].phase -eq 'scan' -and $null -eq $script:Events[0].percent) 'query starts unknown'
foreach($percent in @(0,50,100)){
    Assert-Discovery (@($script:Events|Where-Object {$_.phase -eq 'driver_offers' -and $_.percent -eq $percent}).Count -eq 1) ('actual returned offer processing '+$percent)
}
Assert-Discovery (@($script:Events|Where-Object {$_.phase -eq 'scan' -and $null -ne $_.percent}).Count -eq 0) 'no fabricated WUA search percentage'
Reset-SearchFixture;$script:Offers=@();$empty=Get-SCMDriverUpdateScan -ProgressCallback $observer
Assert-Discovery ($empty.resultCode -eq 2 -and $script:Events[-1].percent -eq 100) 'verified empty collection completes'
foreach($kind in @('failed','warning','throw','malformed')){
    Reset-SearchFixture
    switch($kind){'failed'{$script:SearchCode=[int]4};'warning'{$script:SearchWarning=$true};'throw'{$script:SearchThrows=$true};'malformed'{$script:SearchMalformed=$true}}
    $value=Get-SCMDriverUpdateScan -ProgressCallback $observer
    Assert-Discovery (@($script:Events|Where-Object {$_.percent -eq 100}).Count -eq 0) ($kind+' no terminal100')
    Assert-Discovery ($null -eq $script:Events[-1].percent) ($kind+' final unknown')
}
Reset-SearchFixture;$value=Get-SCMDriverUpdateScan -ProgressCallback {param($Value) 'ignored display output';throw 'display error'}
Assert-Discovery ($value.resultCode -eq 2 -and $value.updates.Count -eq 2) 'display errors cannot replace report'
Reset-Fixture;$script:Drivers=@($script:Drivers[0],$script:Drivers[0]);$script:Events.Clear()
$inventory=Get-SCMDeviceCoverageInventory -ProgressCallback $observer
Assert-Discovery ($inventory.complete -and $inventory.devices.Count -eq 3 -and $inventory.excludedNotPresent -eq 1) 'same present inventory'
Assert-Discovery ($script:Events[0].phase -eq 'device_query' -and $null -eq $script:Events[0].percent) 'opaque PnP query unknown'
Assert-Discovery (@($script:Events|Where-Object {$_.phase -eq 'installed_driver_query' -and $null -eq $_.percent}).Count -eq 1) 'opaque signed-driver query unknown'
Assert-Discovery (@($script:Events|Where-Object {$_.phase -eq 'driver_records' -and $_.percent -eq 50}).Count -eq 1) 'one of two real driver records processed'
foreach($percent in @(0,25,50,75,100)){
    Assert-Discovery (@($script:Events|Where-Object {$_.phase -eq 'device_records' -and $_.percent -eq $percent}).Count -eq 1) ('actual device processing '+$percent)
}
Assert-Discovery (@($script:Events|Where-Object {$_.phase -eq 'system_query' -and $null -eq $_.percent}).Count -eq 1) 'next opaque query resets numeric progress'
Assert-Discovery ($script:Events[-1].phase -eq 'verify' -and $script:Events[-1].percent -eq 100) 'clean final inventory verification'
Reset-Fixture;$script:FailClass='Win32_PnPEntity';$script:Events.Clear()
$failed=Get-SCMDeviceCoverageInventory -ProgressCallback $observer
Assert-Discovery (-not $failed.complete -and $failed.errors.Count -gt 0) 'inventory error remains'
Assert-Discovery (@($script:Events|Where-Object {$_.phase -eq 'device_records' -and $null -ne $_.percent}).Count -eq 0) 'failed query does not invent a counted empty collection'
Assert-Discovery (@($script:Events|Where-Object {$_.phase -eq 'verify' -and $_.percent -eq 100}).Count -eq 0) 'incomplete inventory never verified100'
Reset-Fixture;$script:FailClass='Win32_BaseBoard';$script:Events.Clear()
$failed=Get-SCMDeviceCoverageInventory -ProgressCallback $observer
Assert-Discovery ($failed.complete -and $failed.errors.Count -gt 0 -and $null -eq $script:Events[-1].percent) 'optional errors not clean verified100'
Reset-Fixture;$value=Get-SCMDeviceCoverageInventory -ProgressCallback {param($Value) throw 'display error'}
Assert-Discovery ($value.complete -and $value.devices.Count -eq 3) 'display errors cannot discard inventory'
$script:Events.Clear();$previous=-1
foreach($done in 1..10000){Publish-SCMDriverCollectionProgress -ProgressCallback $observer -Phase device_records -Completed $done -Total 10000 -Previous ([ref]$previous)}
Assert-Discovery ($script:Events.Count -le 100) 'collection frame volume bounded'
Assert-Discovery (@($script:Events|Where-Object {$_.percent -eq 100}).Count -eq 0) 'collection helper never prematurely emits100'
$script:Events.Clear()
foreach($invalid in @(-1,101,[double]::NaN,[double]::PositiveInfinity,'50',$true)){Publish-SCMDriverDiscoveryProgress -ProgressCallback $observer -Phase scan -Percent $invalid}
Assert-Discovery (@($script:Events|Where-Object {$null -ne $_.percent}).Count -eq 0) 'invalid percentages unknown'
$bridge=Join-Path $PSScriptRoot '..\src\optimus\engine\OptimusBridge.ps1'
$tokens=$null;$errors=$null;$ast=[Management.Automation.Language.Parser]::ParseFile($bridge,[ref]$tokens,[ref]$errors)
Assert-Discovery ($errors.Count -eq 0) 'real bridge parses'
foreach($name in @('New-OptimusProgressCallback','Invoke-OptimusRequest')){
    $function=$ast.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name},$true)[0]
    Invoke-Expression $function.Extent.Text
}
foreach($operation in @('driver_updates_scan','driver_device_inventory')){
    Reset-SearchFixture;Reset-Fixture
    $writer=New-Object IO.StringWriter;$before=[Console]::Out
    try{[Console]::SetOut($writer);$result=Invoke-OptimusRequest ([pscustomobject]@{id='aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';operation=$operation;arguments=[pscustomobject]@{reportProgress=$true}})}
    finally{[Console]::SetOut($before)}
    $frames=@($writer.ToString().Split([char]10)|Where-Object {$_}|ForEach-Object {$_|ConvertFrom-Json})
    Assert-Discovery ($frames.Count -gt 1) ($operation+' emits actual native progress')
    Assert-Discovery (@($frames|Where-Object {$_.id -ne 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' -or $_.kind -ne 'progress'}).Count -eq 0) 'exact request-bound frames'
    Assert-Discovery (@($frames|Where-Object {$null -ne $_.ok}).Count -eq 0) 'progress is not final receipt'
    Assert-Discovery ($null -ne $result) 'native receipt retained'
    foreach($flag in @($false,'true')){
        Reset-SearchFixture;Reset-Fixture;$writer=New-Object IO.StringWriter;$before=[Console]::Out
        try{[Console]::SetOut($writer);$silent=Invoke-OptimusRequest ([pscustomobject]@{id='bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb';operation=$operation;arguments=[pscustomobject]@{reportProgress=$flag}})}
        finally{[Console]::SetOut($before)}
        Assert-Discovery ($writer.ToString() -eq '') 'exact bool opt-in only'
    }
}
[pscustomobject]@{Result='PASS';Assertions=$script:Assertions;BaseAssertions=$baseReport.Assertions;RealScans=0;RealInstalls=0;RealDeviceChanges=0;PSVersion=$PSVersionTable.PSVersion.ToString()}|ConvertTo-Json -Compress
