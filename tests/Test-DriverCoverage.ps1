# Offline inventory and WUA fixtures only. No device/driver mutation is available.
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot '..\src\optimus\engine\FileTools.ps1')
$script:Assertions=0
function Assert-Test {param([bool]$Condition,[string]$Message) if(-not $Condition){throw ('FAIL: '+$Message)};$script:Assertions++}
function Get-CimInstance {
    param($ClassName,$ErrorAction)
    if($ClassName -eq $script:FailClass){throw 'Fixture inventory access failure'}
    switch($ClassName){
        'Win32_PnPEntity' {return $script:Devices}
        'Win32_PnPSignedDriver' {return $script:Drivers}
        'Win32_ComputerSystem' {return [pscustomobject]@{Manufacturer='Dell Inc.';Model='Fixture PC'}}
        'Win32_BaseBoard' {return [pscustomobject]@{Manufacturer='ASUSTeK';Product='Fixture Board'}}
        default {throw 'Unexpected class query'}
    }
}
function Reset-Fixture {
    $script:FailClass=''
    $script:Devices=@(
        [pscustomobject]@{PNPDeviceID='PCI\fixture-instance';DeviceID='PCI\fixture-instance';Name='Fixture GPU';Present=$true;HardwareID=@('PCI\VEN_10DE&DEV_1234','PCI\VEN_10DE');CompatibleID=@('PCI\CC_0300');PNPClass='Display';Manufacturer='NVIDIA';ConfigManagerErrorCode=[uint32]0;Status='OK';Service='FixtureVideo'},
        [pscustomobject]@{DeviceID='USB\fixture-missing-driver';Name='Fixture problem device';Present=$true;HardwareID=@('USB\VID_1234');ConfigManagerErrorCode=[uint32]28;Status='Error'},
        [pscustomobject]@{DeviceID='USB\fixture-unknown-presence';Name='Fixture unknown presence';HardwareID=@('USB\VID_5678');ConfigManagerErrorCode=$null},
        [pscustomobject]@{DeviceID='USB\fixture-disconnected';Name='Fixture unplugged';Present=$false;HardwareID=@('USB\VID_0000');ConfigManagerErrorCode=[uint32]45}
    )
    $script:Drivers=@(
        [pscustomobject]@{DeviceID='pci\FIXTURE-instance';DriverVersion='1.2.3';DriverDate=[datetime]'2026-10-01';InfName='oem1.inf';IsSigned=$true;DriverProviderName='NVIDIA';Manufacturer='NVIDIA';DeviceClass='DISPLAY'}
    )
}
Reset-Fixture
$result=Get-SCMDeviceCoverageInventory
Assert-Test ($result.complete -and $result.pnpComplete -and $result.driversComplete) 'full successful read-only inventory'
Assert-Test ($result.devices.Count -eq 3 -and $result.excludedNotPresent -eq 1) 'present and unknown-presence included; disconnected excluded'
$gpu=@($result.devices|Where-Object deviceId -ieq 'PCI\fixture-instance')[0]
Assert-Test ($gpu.hardwareIds.Count -eq 2 -and $gpu.compatibleIds[0] -eq 'PCI\CC_0300') 'hardware and compatible arrays preserved'
Assert-Test ($gpu.installedDrivers.Count -eq 1 -and $gpu.installedDrivers[0].version -eq '1.2.3') 'installed metadata joined by case-insensitive instance identity'
Assert-Test ($gpu.installedDrivers[0].provider -eq 'NVIDIA' -and $gpu.installedDrivers[0].deviceClass -eq 'DISPLAY') 'known provider and installed driver class exposed for future approvals'
Assert-Test ($gpu.installedDrivers[0].date.StartsWith('2026-10-01')) 'driver date exported as ISO'
$missing=@($result.devices|Where-Object deviceId -eq 'USB\fixture-missing-driver')[0]
Assert-Test ($missing.problemCode -eq 28 -and $missing.installedDrivers.Count -eq 0) 'missing-driver code preserved without fake metadata'
$unknown=@($result.devices|Where-Object deviceId -eq 'USB\fixture-unknown-presence')[0]
Assert-Test ($null -eq $unknown.present -and $null -eq $unknown.problemCode) 'unknown presence/problem code never cast false or zero'
Assert-Test ($result.system.manufacturer -eq 'Dell Inc.' -and $result.system.baseboardManufacturer -eq 'ASUSTeK') 'OEM and motherboard route metadata'
Assert-Test ($result.errors.Count -eq 0) 'successful inventory no errors'
Reset-Fixture;$script:FailClass='Win32_PnPEntity';$result=Get-SCMDeviceCoverageInventory
Assert-Test (-not $result.complete -and -not $result.pnpComplete -and $result.driversComplete) 'PnP failure not reported as complete'
Assert-Test ($result.devices.Count -eq 0 -and $result.errors[0].source -eq 'Win32_PnPEntity') 'PnP error visible'
Reset-Fixture;$script:FailClass='Win32_PnPSignedDriver';$result=Get-SCMDeviceCoverageInventory
Assert-Test (-not $result.complete -and $result.devices.Count -eq 3 -and -not $result.driversComplete) 'signed-driver failure retains device rows and uncertainty'
Reset-Fixture;$script:FailClass='Win32_BaseBoard';$result=Get-SCMDeviceCoverageInventory
Assert-Test ($result.complete -and $result.errors[0].source -eq 'Win32_BaseBoard') 'optional board failure shown without discarding full device coverage'
# The original unrestricted required+optional scan criteria remain exact; no IsAssigned/BrowseOnly filter.
$script:FixtureSearcher=New-Object PSObject
$script:FixtureSearcher|Add-Member ScriptMethod Search {param($query)
    Assert-Test ($query -ceq "IsInstalled=0 and Type='Driver' and IsHidden=0") 'required and optional criteria retained'
    return [pscustomobject]@{ResultCode=[int]2;Updates=@()}
}
$script:FixtureSession=New-Object PSObject
$script:FixtureSession|Add-Member ScriptMethod CreateUpdateSearcher {return $script:FixtureSearcher}
function New-SCMWuaSession {return $script:FixtureSession}
$scan=Get-SCMDriverUpdateScan
Assert-Test ($scan.includesOptional -and $scan.resultCode -eq 2) 'scan advertises optional coverage without claiming vendor coverage'
[pscustomobject]@{Result='PASS';Assertions=$script:Assertions;RealScans=0;RealDownloads=0;RealInstalls=0;RealDeviceChanges=0;PSVersion=$PSVersionTable.PSVersion.ToString()}|ConvertTo-Json -Compress

