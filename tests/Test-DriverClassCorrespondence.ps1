# Fake WUA and device objects only. No native scan, download, install or settings.
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'Test-AutomatedSystem.ps1') | Out-Null
$baseAssertions=$script:Assertions
function Get-CimInstance {
    param($ClassName,$ErrorAction)
    if($ClassName -eq 'Win32_PnPEntity'){return $script:ClassDevices}
    if($ClassName -eq 'Win32_PnPSignedDriver'){return $script:ClassDrivers}
    throw 'Unexpected fixture device query'
}
function Reset-ClassFixture {
    Reset-Fixture
    $script:Update.DriverClass='OtherHardware'
    $script:ClassDevices=@([pscustomobject]@{PNPDeviceID='fixture-device';Present=$true;
        PNPClass='AudioProcessingObject';HardwareID=@('PCI\VEN_1234&DEV_5678');CompatibleID=@()})
    $script:ClassDrivers=@([pscustomobject]@{DeviceID='fixture-device';DriverDate=[datetime]'2026-10-10';
        DeviceClass='AUDIOPROCESSINGOBJECT';IsSigned=$true;DriverProviderName='Fixture provider';DriverVersion='1.0.0.1'})
    $record=Record-Fixture
    $record | Add-Member NoteProperty approvalMode 'matching_installed_class'
    $record | Add-Member NoteProperty notOlderThanDriverDate '2026-10-01'
    $record | Add-Member NoteProperty installedClassApproval ([pscustomobject]@{
        hardwareId='PCI\VEN_1234&DEV_5678';provider='Fixture provider';class='AUDIOPROCESSINGOBJECT'})
    return $record
}
function Assert-ClassRefusal {
    param($Value,[int]$Downloads,[string]$Label)
    Assert-Test ($Value.installer_started -eq $false -and $Value.outcomeKnown -eq $true -and
        $Value.running -eq $false -and $Value.failure_scope -eq 'package' -and $Value.failureKind -eq 'version_changed') $Label
    Assert-Test ($script:Downloads -eq $Downloads -and $script:Installs -eq 0) ('no Install after '+$Label)
}
$record=Reset-ClassFixture
$value=Invoke-SCMDriverUpdateApply -Record $record -Unattended $true -AcceptEula $true
Assert-Test ($script:Downloads -eq 1 -and $script:Installs -eq 1) 'verified installed-class binding reaches quiet Install'
Assert-Test ($record.driverClass -eq 'OtherHardware') 'original WUA class remains exact'
$record=Reset-ClassFixture
$script:ClassDevices+=([pscustomobject]@{PNPDeviceID='second-device';Present=$true;
    PNPClass='AudioProcessingObject';HardwareID=@('PCI\VEN_1234&DEV_5678');CompatibleID=@()})
$script:ClassDrivers+=([pscustomobject]@{DeviceID='second-device';DriverDate=[datetime]'2026-10-10';
    DeviceClass='AUDIOPROCESSINGOBJECT';IsSigned=$true;DriverProviderName='Fixture provider';DriverVersion='1.0.0.1'})
$value=Invoke-SCMDriverUpdateApply -Record $record -Unattended $true -AcceptEula $true
Assert-Test ($script:Downloads -eq 1 -and $script:Installs -eq 1) 'all matching signed present devices agree on class'

foreach($invalid in @('missing-binding','extra-binding','wrong-hardware','wrong-provider','generic-class',
    'not-present','unknown-presence','unsigned','unknown-signature','live-provider','live-class',
    'missing-driver','ambiguous-class','ambiguous-provider','duplicate-device')){
    $record=Reset-ClassFixture
    switch($invalid){
        'missing-binding' {$record.PSObject.Properties.Remove('installedClassApproval')}
        'extra-binding' {$record.installedClassApproval | Add-Member NoteProperty unexpected 'fixture'}
        'wrong-hardware' {$record.installedClassApproval.hardwareId='USB\AnotherDevice'}
        'wrong-provider' {$record.installedClassApproval.provider='Another publisher'}
        'generic-class' {$record.installedClassApproval.class='OtherHardware'}
        'not-present' {$script:ClassDevices[0].Present=$false}
        'unknown-presence' {$script:ClassDevices[0].Present=$null}
        'unsigned' {$script:ClassDrivers[0].IsSigned=$false}
        'unknown-signature' {$script:ClassDrivers[0].IsSigned=$null}
        'live-provider' {$script:ClassDrivers[0].DriverProviderName='Another publisher'}
        'live-class' {$script:ClassDrivers[0].DeviceClass='Display'}
        'missing-driver' {$script:ClassDrivers=@()}
        'ambiguous-class' {
            $script:ClassDevices+=([pscustomobject]@{PNPDeviceID='second-device';Present=$true;
                PNPClass='Display';HardwareID=@('PCI\VEN_1234&DEV_5678');CompatibleID=@()})
        }
        'ambiguous-provider' {
            $script:ClassDrivers+=([pscustomobject]@{DeviceID='fixture-device';DriverDate=[datetime]'2026-10-10';
                DeviceClass='AUDIOPROCESSINGOBJECT';IsSigned=$true;DriverProviderName='Another publisher'})
        }
        'duplicate-device' {$script:ClassDevices+= $script:ClassDevices[0]}
    }
    $value=Invoke-SCMDriverUpdateApply -Record $record -Unattended $true -AcceptEula $true
    Assert-ClassRefusal $value 0 $invalid
}
foreach($drift in @('class','provider','presence','signature','date')){
    $record=Reset-ClassFixture
    $script:ClassDrift=$drift
    $script:Downloader | Add-Member -Force ScriptMethod Download {
        $script:Downloads++
        switch($script:ClassDrift){
            'class' {$script:ClassDevices[0].PNPClass='Display'}
            'provider' {$script:ClassDrivers[0].DriverProviderName='Another publisher'}
            'presence' {$script:ClassDevices[0].Present=$false}
            'signature' {$script:ClassDrivers[0].IsSigned=$false}
            'date' {$script:ClassDrivers[0].DriverDate=[datetime]'2026-10-21'}
        }
        return $script:DownloadResult
    }
    $value=Invoke-SCMDriverUpdateApply -Record $record -Unattended $true -AcceptEula $true
    Assert-ClassRefusal $value 1 ('post-download '+$drift+' drift')
}
[pscustomobject]@{Result='PASS';Assertions=($script:Assertions-$baseAssertions);
    RealScans=0;RealDownloads=0;RealInstalls=0}|ConvertTo-Json -Compress
