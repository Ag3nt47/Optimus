#requires -Version 5.1
# Office suite removal through its registered, signed Microsoft uninstaller.
# Dot-sourcing defines functions only. No folder/registry deletion or downloads.
function Get-OptimusOfficeHash {
    param([Parameter(Mandatory=$true)][string]$Text)
    $hash=[Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($Text)))).Replace('-','').ToLowerInvariant() }
    finally { $hash.Dispose() }
}
function Test-OptimusOfficePreservedIdentity {
    param([string]$Name,[string]$Identity)
    return ($Identity -ieq 'MicrosoftCorporationII.Windows365_8wekyb3d8bbwe' -or
        $Name -iin @('MicrosoftCorporationII.Windows365','Windows App','Microsoft Windows App') -or
        ($Name+'|'+$Identity) -match '(?i)WebView2|WindowsAppRuntime|Bing.?Wallpaper|VCLibs|Access Database Engine|Access Runtime')
}
function Get-OptimusOfficeProductFamily {
    param([string]$ProductId)
    if ($ProductId -cmatch '^O365[A-Za-z0-9]+(?:Retail|Volume)$') { return 'm365' }
    if ($ProductId -cmatch '^(?:ProPlus|Professional|Standard|HomeBusiness|HomeStudent|Personal|SmallBusiness|Academic|Education)(?:20(?:13|16|19|21|24))?(?:Retail|Volume)$') { return 'office' }
    return ''
}
function Get-OptimusOfficeRegistryRows {
    # Per-user ARP entries are writable by unelevated software and cannot grant
    # authority to uninstall an arbitrary system MSI product after elevation.
    $roots=@('HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall')
    foreach($root in $roots) {
        foreach($key in @(Get-ChildItem -LiteralPath $root -ErrorAction SilentlyContinue)) {
            $row=Get-ItemProperty -LiteralPath $key.PSPath -ErrorAction SilentlyContinue
            if(-not $row -or -not $row.DisplayName) { continue }
            [pscustomobject]@{path=($root+'\'+$key.PSChildName);identity=[string]$key.PSChildName;
                name=[string]$row.DisplayName;publisher=[string]$row.Publisher;version=[string]$row.DisplayVersion;
                uninstall=[string]$row.UninstallString;windowsInstaller=($row.WindowsInstaller -eq 1)}
        }
    }
}
function Get-OptimusOfficeInstalledProducts {
    $products=@()
    foreach($path in @('HKLM:\SOFTWARE\Microsoft\Office\ClickToRun\Configuration',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Office\ClickToRun\Configuration',
        'HKLM:\SOFTWARE\Microsoft\Office\15.0\ClickToRun\Configuration')) {
        $configuration=Get-ItemProperty -LiteralPath $path -ErrorAction SilentlyContinue
        if($configuration) { $products+=@(([string]$configuration.ProductReleaseIds).Split(',') | ForEach-Object {$_.Trim()} | Where-Object {$_ -cmatch '^[A-Za-z][A-Za-z0-9]{2,79}$'}) }
    }
    return @($products | Sort-Object -Unique)
}
function Get-OptimusOfficeTrustedBinary {
    param([Parameter(Mandatory=$true)][string]$Path,[ValidateSet('clicktorun','msi')][string]$Method)
    if(-not [IO.Path]::IsPathRooted($Path)) { throw 'The Office uninstaller must have an absolute path.' }
    $full=[IO.Path]::GetFullPath($Path)
    $allowed=@()
    if($Method -eq 'msi') { $allowed+=Join-Path $env:SystemRoot 'System32\msiexec.exe' }
    else {
        foreach($root in @([Environment]::GetFolderPath('CommonProgramFiles'),[Environment]::GetFolderPath('CommonProgramFilesX86')) | Where-Object {$_} | Select-Object -Unique) {
            $allowed+=Join-Path $root 'Microsoft Shared\ClickToRun\OfficeClickToRun.exe'
        }
    }
    if(-not @($allowed | Where-Object {[IO.Path]::GetFullPath($_) -ieq $full}).Count) { throw 'Office executable is outside the supported Microsoft installer locations.' }
    for($part=$full;$part;$part=[IO.Path]::GetDirectoryName($part)) {
        $item=Get-Item -LiteralPath $part -Force -ErrorAction Stop
        if($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'An indirect Office uninstaller path is not supported.' }
    }
    $file=Get-Item -LiteralPath $full -Force -ErrorAction Stop
    if($file.PSIsContainer) { throw 'The Office uninstaller is not a regular file.' }
    $signature=Get-AuthenticodeSignature -LiteralPath $full -ErrorAction Stop
    $subject=if($signature.SignerCertificate){[string]$signature.SignerCertificate.Subject}else{''}
    $publisherAllowed=($subject -match '(?i)(?:^|,\s*)CN=Microsoft Corporation(?:,|$)' -or
        ($Method -eq 'msi' -and $subject -match '(?i)(?:^|,\s*)CN=Microsoft Windows(?:,|$)' -and $subject -match '(?i)(?:^|,\s*)O=Microsoft Corporation(?:,|$)'))
    if($signature.Status -ne 'Valid' -or -not $publisherAllowed) {
        throw 'A valid Microsoft Corporation signature is required for Office removal.'
    }
    return [pscustomobject]@{path=$full;sha256=(Get-FileHash -LiteralPath $full -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant();signer=[string]$signature.SignerCertificate.Subject}
}
function ConvertTo-OptimusOfficeRemovalCommand {
    param([Parameter(Mandatory=$true)]$Row,[string[]]$InstalledProducts)
    if($Row.windowsInstaller) {
        $guid=[string]$Row.identity
        if($guid -cnotmatch '^\{[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}\}$') { throw 'An exact registered Office MSI product code is required.' }
        $pattern='^(?:"[^"\r\n]*\\msiexec\.exe"|msiexec(?:\.exe)?)\s+/[iIxX]\s*'+[regex]::Escape($guid)+'(?:\s+.*)?$'
        if([string]$Row.uninstall -notmatch $pattern) { throw 'The Office MSI command does not identify its own product code.' }
        $proof=Get-OptimusOfficeTrustedBinary -Path (Join-Path $env:SystemRoot 'System32\msiexec.exe') -Method msi
        return [pscustomobject]@{kind='office-msi';method='msi';family='office';productId=$guid;binary=$proof;arguments=@('/x',$guid,'/qn','/norestart');registration=$Row.path}
    }
    $command=[regex]::Match([string]$Row.uninstall,'^"(?<exe>[^"\r\n]+)"\s+(?<arguments>[^"\r\n;&|<>]+)$')
    if(-not $command.Success) { throw 'The Office registration does not contain a supported quoted Click-to-Run command.' }
    $tokens=@{}
    foreach($token in @($command.Groups['arguments'].Value -split '\s+' | Where-Object {$_})) {
        $pair=[regex]::Match($token,'^(?<key>[A-Za-z][A-Za-z0-9.]*)=(?<value>[A-Za-z0-9._-]+)$')
        if(-not $pair.Success) { throw 'An Office uninstall argument is not a literal supported value.' }
        $key=$pair.Groups['key'].Value.ToLowerInvariant()
        if($tokens.ContainsKey($key) -or $key -notin @('scenario','scenariosubtype','sourcetype','productstoremove','culture','version.15','version.16','displaylevel','forceappshutdown')) { throw 'The Office uninstall command contains a duplicate or unsupported argument.' }
        $tokens[$key]=$pair.Groups['value'].Value
    }
    if($tokens.scenario -ine 'install' -or $tokens.scenariosubtype -ine 'ARP' -or $tokens.sourcetype -ine 'None') { throw 'Only the registered Office ARP uninstall scenario is supported.' }
    $product=[regex]::Match([string]$tokens.productstoremove,'^(?<id>[A-Za-z][A-Za-z0-9]{2,79})\.(?<major>15|16)_(?<culture>[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){1,2})_x-none$')
    if(-not $product.Success -or [string]$tokens.culture -ine $product.Groups['culture'].Value) { throw 'An exact Office product and matching language are required.' }
    $productId=$product.Groups['id'].Value;$family=Get-OptimusOfficeProductFamily $productId
    if(-not $family -or $productId -cnotin @($InstalledProducts)) { throw 'The registered product is not a currently installed Office suite.' }
    $major=$product.Groups['major'].Value
    foreach($key in @('version.15','version.16')) {if($tokens.ContainsKey($key) -and ($key -cne ('version.'+$major) -or $tokens[$key] -cne ($major+'.0'))) { throw 'The Office uninstall version does not match the selected product.' }}
    $proof=Get-OptimusOfficeTrustedBinary -Path $command.Groups['exe'].Value -Method clicktorun
    $arguments=@('scenario=install','scenariosubtype=ARP','sourcetype=None',('productstoremove='+$tokens.productstoremove),('culture='+$tokens.culture),('version.'+$major+'='+$major+'.0'),'DisplayLevel=False','ForceAppShutdown=False')
    return [pscustomobject]@{kind='office-clicktorun';method='clicktorun';family=$family;productId=$productId;culture=[string]$tokens.culture;binary=$proof;arguments=$arguments;registration=$Row.path}
}
function Get-OptimusOfficeRemovalTargets {
    param([ValidateSet('m365','office','all')][string]$Family='all')
    $products=@(Get-OptimusOfficeInstalledProducts)
    foreach($row in @(Get-OptimusOfficeRegistryRows)) {
        if((Test-OptimusOfficePreservedIdentity -Name $row.name -Identity $row.identity) -or
            $row.publisher -notmatch '^(?i:Microsoft Corporation|Microsoft Corp\.)$' -or
            $row.name -notmatch '^(?i:Microsoft (?:365|Office)(?:\s|$))' -or
            $row.name -match '(?i)update|proof|language|extensibility|runtime|components|MUI|interop|licensing|\b(?:Visio|Project|Access)\b') { continue }
        $payload=$null;$problem=''
        try { $payload=ConvertTo-OptimusOfficeRemovalCommand -Row $row -InstalledProducts $products }
        catch { $problem=$_.Exception.Message }
        $familyValue=if($payload){$payload.family}elseif($row.name -match '^(?i:Microsoft 365|Microsoft Office 365)'){'m365'}else{'office'}
        if($Family -ne 'all' -and $Family -cne $familyValue) { continue }
        $kind=if($payload){$payload.kind}else{'office-review'}
        $binding=[ordered]@{identity=$row.identity;registryPath=$row.path;name=$row.name;publisher=$row.publisher;version=$row.version;family=$familyValue;payload=$payload;problem=$problem}
        $fingerprint=Get-OptimusOfficeHash (ConvertTo-Json -InputObject $binding -Depth 8 -Compress)
        $idHash=Get-OptimusOfficeHash ([string]$row.path)
        [pscustomobject]@{id=('office-'+$idHash.Substring(0,20));name=$row.name;version=$row.version;family=$familyValue;scope='desktop';kind=$kind;
            method=if($payload){$payload.method}else{'manual-review'};appIdentity=$row.identity;registryPath=$row.path;productId=if($payload){$payload.productId}else{''};
            fingerprint=$fingerprint;payload=$payload;selected=$false;admin=$true;reason=$problem;
            recovery='Reinstall this exact Office product from its original Microsoft account or organization. Personal documents are not deleted by Optimus.'}
    }
}
function Test-OptimusOfficeRemovalExcluded {
    param($Target,[array]$Exclusions)
    if(Test-OptimusOfficePreservedIdentity -Name ([string]$Target.name) -Identity ([string]$Target.appIdentity)) { return $true }
    foreach($row in @($Exclusions)) {
        if($row.kind -ceq 'desktop' -and (($row.id -and $row.id -ieq $Target.appIdentity) -or $row.name -ieq $Target.name)) { return $true }
    }
    return $false
}
function Test-OptimusOfficeRemovalAdmin {
    $identity=[Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}
function Get-OptimusOfficeActivePrograms {
    # Do not close these programs or force unsaved documents out of memory.
    return @(Get-Process -ErrorAction SilentlyContinue | Where-Object {$_.ProcessName -iin @('WINWORD','EXCEL','POWERPNT','OUTLOOK','ONENOTE','MSACCESS','MSPUB','VISIO','WINPROJ','lync') } | Select-Object Id,ProcessName)
}
function Publish-OptimusOfficeRemovalProgress {
    param([scriptblock]$Callback,[string]$Action,$Target,[string]$Phase,$Percent)
    if($Callback) { try { $null=& $Callback ([pscustomobject]@{key=('debloat:'+$Action+':'+$Target.id);phase=$Phase;percent=$Percent;name=$Target.name;process='Microsoft Office uninstaller'}) } catch {} }
}
function Invoke-OptimusOfficeNativeUninstall {
    param([Parameter(Mandatory=$true)]$Payload)
    $child=Start-Process -FilePath ([string]$Payload.binary.path) -ArgumentList @($Payload.arguments) -WindowStyle Hidden -PassThru -ErrorAction Stop
    try { $null=$child.Handle } finally { $child.WaitForExit() }
    $child.Refresh()
    if($null -eq $child.ExitCode) { throw 'The Office uninstaller did not provide an exit code.' }
    return [int]$child.ExitCode
}
function Get-OptimusOfficeRemovalReadback {
    param($Target)
    $registrations=@(Get-OptimusOfficeRegistryRows | Where-Object {$_.path -ieq $Target.registryPath})
    $productRemains=($Target.method -eq 'clicktorun' -and $Target.productId -cin @(Get-OptimusOfficeInstalledProducts))
    return [pscustomobject]@{registrationPresent=($registrations.Count -gt 0);productPresent=[bool]$productRemains;absent=($registrations.Count -eq 0 -and -not $productRemains)}
}
function Invoke-OptimusOfficeRemovalTarget {
    param([Parameter(Mandatory=$true)]$Target,[array]$Exclusions=@(),[scriptblock]$ProgressCallback,
        [string]$Action='office-desktop',[ValidateRange(0,90)][int]$VerificationSeconds=30)
    if(Test-OptimusOfficeRemovalExcluded -Target $Target -Exclusions $Exclusions) {
        return [pscustomobject]@{id=$Target.id;name=$Target.name;removed=$false;protected=$true;skipped=$true;status='Protected from removal in Settings';reason='app_removal_excluded';restartNeeded=$false}
    }
    if($Target.kind -cnotin @('office-clicktorun','office-msi','office-review')) { throw 'Unsupported Office removal target.' }
    $fresh=@(Get-OptimusOfficeRemovalTargets -Family all | Where-Object {$_.id -ceq $Target.id -and $_.fingerprint -ceq $Target.fingerprint})
    if($fresh.Count -ne 1) { throw 'The Office product, publisher, command or uninstaller changed. Preview again.' }
    $current=$fresh[0]
    if(Test-OptimusOfficeRemovalExcluded -Target $current -Exclusions $Exclusions) { throw 'This Office target is protected from removal.' }
    if($current.kind -ceq 'office-review') {
        return [pscustomobject]@{id=$current.id;name=$current.name;removed=$false;requiresReview=$true;status='Office installation needs manual review';reason='unsupported_office_uninstaller';detail=$current.reason;restartNeeded=$false}
    }
    if(-not(Test-OptimusOfficeRemovalAdmin)) { throw 'Administrator rights are required to remove this Office product.' }
    $active=@(Get-OptimusOfficeActivePrograms)
    if($active.Count) {
        return [pscustomobject]@{id=$current.id;name=$current.name;removed=$false;skipped=$true;requiresReview=$true;status='Close Office programs before removal';reason='office_programs_open';programs=$active;restartNeeded=$false}
    }
    Publish-OptimusOfficeRemovalProgress -Callback $ProgressCallback -Action $Action -Target $current -Phase uninstall -Percent $null
    $exitCode=Invoke-OptimusOfficeNativeUninstall -Payload $current.payload
    Publish-OptimusOfficeRemovalProgress -Callback $ProgressCallback -Action $Action -Target $current -Phase verify -Percent $null
    $readback=Get-OptimusOfficeRemovalReadback -Target $current
    $deadline=[datetime]::UtcNow.AddSeconds($VerificationSeconds)
    while($exitCode -in @(0,3010,1641) -and -not $readback.absent -and [datetime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 1000
        $readback=Get-OptimusOfficeRemovalReadback -Target $current
    }
    $restart=($exitCode -in @(3010,1641))
    $removed=($exitCode -in @(0,3010,1641,1605,1614) -and $readback.absent)
    $reason=if($removed){'removal_verified'}elseif($exitCode -in @(0,3010,1641)){'office_uninstall_not_verified'}else{'office_uninstaller_error'}
    $status=if($removed -and $restart){'Office product removed; restart required'}elseif($removed){'Office product removed'}elseif($exitCode -eq 0){'Uninstaller exited successfully, but the Office product is still registered'}else{'Office removal was not verified'}
    if($removed -and -not $restart) { Publish-OptimusOfficeRemovalProgress -Callback $ProgressCallback -Action $Action -Target $current -Phase uninstall -Percent 100 }
    return [pscustomobject]@{id=$current.id;name=$current.name;family=$current.family;scope=$current.scope;productId=$current.productId;removed=$removed;status=$status;
        reason=$reason;exitCode=$exitCode;restartNeeded=$restart;requiresReview=(-not $removed -or $restart);verification=$readback;
        # Native invocation waits for and retires its child before returning an
        # exit code. Unverified product absence is a finding, not a live installer.
        outcomeKnown=$true;running=$false;installer_started=$true;
        note='Only the selected registered Office suite is passed to its signed Microsoft uninstaller. Windows App, WebView2, shared Windows runtimes and personal documents are not removal targets. Other Office products or languages can retain shared files.'}
}
