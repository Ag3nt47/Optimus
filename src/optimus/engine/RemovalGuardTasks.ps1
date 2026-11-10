#requires -Version 5.1
param([switch]$RequestJson)
# Scheduled actions contain native code in the protected task definition.
# Writable user data is only read for revocation and for an existing read-only lease.
$script:OptimusRemovalGuardSourceRoot=$PSScriptRoot
function Get-OptimusRemovalGuardSid { [Security.Principal.WindowsIdentity]::GetCurrent().User.Value }
function Test-OptimusRemovalGuardAdmin { ([Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator) }
function Get-OptimusRemovalGuardPowerShell { Join-Path ([Environment]::GetFolderPath('System')) 'WindowsPowerShell\v1.0\powershell.exe' }
function Get-OptimusRemovalGuardDigest {
    param([byte[]]$Bytes)
    $sha=[Security.Cryptography.SHA256]::Create()
    try { ([BitConverter]::ToString($sha.ComputeHash($Bytes))).Replace('-','').ToLowerInvariant() } finally { $sha.Dispose() }
}
function ConvertTo-OptimusGuardJsonString {
    param([string]$Value)
    $b=[Text.StringBuilder]::new();[void]$b.Append('"')
    foreach($c in $Value.ToCharArray()){
        $n=[int]$c
        if($n -eq 34){[void]$b.Append('\"')}
        elseif($n -eq 92){[void]$b.Append('\\')}
        elseif($n -eq 8){[void]$b.Append('\b')}
        elseif($n -eq 9){[void]$b.Append('\t')}
        elseif($n -eq 10){[void]$b.Append('\n')}
        elseif($n -eq 12){[void]$b.Append('\f')}
        elseif($n -eq 13){[void]$b.Append('\r')}
        elseif($n -lt 32 -or $n -gt 126){[void]$b.Append(('\u{0:x4}' -f $n))}
        else {[void]$b.Append($c)}
    }
    [void]$b.Append('"');$b.ToString()
}
function ConvertTo-OptimusGuardCanonicalJson {
    param($Value)
    if($null -eq $Value){return 'null'}
    if($Value -is [bool]){if($Value){return 'true'};return 'false'}
    if($Value -is [string]){return ConvertTo-OptimusGuardJsonString $Value}
    if($Value -is [byte] -or $Value -is [int16] -or $Value -is [int32] -or $Value -is [int64]){return $Value.ToString([Globalization.CultureInfo]::InvariantCulture)}
    if($Value -is [System.Collections.IDictionary] -or $Value -is [pscustomobject]){
        $keys=if($Value -is [System.Collections.IDictionary]){@($Value.Keys)}else{@($Value.PSObject.Properties.Name)}
        [Array]::Sort($keys,[StringComparer]::Ordinal)
        $parts=foreach($key in $keys){$json=if($Value -is [System.Collections.IDictionary]){ConvertTo-OptimusGuardCanonicalJson $Value[$key]}else{ConvertTo-OptimusGuardCanonicalJson $Value.$key};(ConvertTo-OptimusGuardJsonString ([string]$key))+':'+$json}
        return '{'+($parts -join ',')+'}'
    }
    if($Value -is [System.Collections.IEnumerable]){$parts=foreach($item in $Value){ConvertTo-OptimusGuardCanonicalJson $item};return '['+($parts -join ',')+']'}
    throw 'Unsupported value in the approved removal scope.'
}
function Assert-OptimusRemovalGuardOrdinary {
    param([string]$Path,[switch]$File,[switch]$AllowHardLinks)
    if($Path -notmatch '^[A-Za-z]:\\' -or $Path -match '[\x00-\x1f%"]' -or $Path.Length -gt 4096){throw 'An ordinary absolute local path is required.'}
    $full=[IO.Path]::GetFullPath($Path)
    for($part=$full;$part;$part=[IO.Path]::GetDirectoryName($part)){
        $item=Get-Item -LiteralPath $part -Force -ErrorAction Stop
        if($item.Attributes -band [IO.FileAttributes]::ReparsePoint -or (-not $AllowHardLinks -and $item.LinkType -ceq 'HardLink')){throw 'Indirect removal-guard paths are not supported.'}
    }
    if($File -and -not [IO.File]::Exists($full)){throw 'A required removal-guard file is unavailable.'}
    $full
}
function Read-OptimusGuardPreferences {
    param([string]$DataRoot)
    $path=Assert-OptimusRemovalGuardOrdinary (Join-Path $DataRoot 'preferences.json') -File
    if((Get-Item -LiteralPath $path).Length -gt 2097152){throw 'Preferences exceed the size limit.'}
    $bytes=[IO.File]::ReadAllBytes($path)
    if($bytes.Length -gt 2097152){throw 'Preferences changed size.'}
    (New-Object Text.UTF8Encoding($false,$true)).GetString($bytes)|ConvertFrom-Json
}
function Get-OptimusGuardPreferenceScope {
    param($Preferences)
    $flags=@{}
    foreach($name in @('consumer_debloat_enabled','m365_debloat_enabled','office365_desktop_debloat_enabled','office_desktop_debloat_enabled','outlook_debloat_enabled','edge_debloat_enabled')){
        $value=$Preferences.$name;if($null -eq $value){$value=$false};if($value -isnot [bool]){throw 'Removal choices must be Boolean.'};$flags[$name]=$value
    }
    foreach($property in $Preferences.PSObject.Properties){if($property.Name.StartsWith('privacy_',[StringComparison]::Ordinal)){if($property.Value -isnot [bool]){throw 'Privacy choices must be Boolean.'};$flags[$property.Name]=$property.Value}}
    $names=[Collections.Generic.SortedSet[string]]::new([StringComparer]::Ordinal)
    foreach($name in @($Preferences.consumer_debloat_package_names)){if($name -isnot [string] -or $name.Length -gt 256){throw 'Invalid consumer package selection.'};[void]$names.Add($name)}
    $rows=@()
    foreach($row in @($Preferences.app_removal_exclusions)){
        if($null -eq $row){continue}
        $keys=@($row.PSObject.Properties.Name);if($keys.Count -ne 4){throw 'Invalid exclusion fields.'}
        $out=@{};foreach($key in @('kind','id','name','label')){if($key -cnotin $keys -or $row.$key -isnot [string] -or $row.$key.Length -gt 2048){throw 'Invalid exclusion.'};$out[$key]=$row.$key};$rows+=$out
    }
    @{schema=1;flags=$flags;consumer_packages=@($names);exclusions=@($rows);preserved=@('MicrosoftCorporationII.Windows365_8wekyb3d8bbwe','Microsoft.EdgeWebView2');include_provisioned=$true}
}
function Test-OptimusGuardCurrentConsent {
    param($Context)
    try{
        $p=Read-OptimusGuardPreferences $Context.data_root
        if($p.setup_version -ne 2 -or $p.windows_removal_enforcement_enabled -isnot [bool] -or -not $p.windows_removal_enforcement_enabled -or $p.windows_removal_enforcement_scope_hash -cne $Context.scope_hash){return $false}
        $scope=Get-OptimusGuardPreferenceScope $p
        return (Get-OptimusRemovalGuardDigest ([Text.Encoding]::UTF8.GetBytes((ConvertTo-OptimusGuardCanonicalJson $scope)))) -ceq $Context.scope_hash
    }catch{return $false}
}
function Assert-OptimusGuardMachineExecutable {
    param([string]$Path)
    # Windows servicing legitimately hard-links System32 binaries. Every link shares
    # the protected executable's ACL; reparse points remain forbidden.
    $full=Assert-OptimusRemovalGuardOrdinary $Path -File -AllowHardLinks
    $roots=@([Environment]::GetFolderPath('ProgramFiles'),[Environment]::GetFolderPath('ProgramFilesX86'),[Environment]::GetFolderPath('System'))|Where-Object {$_}
    $boundary=$null;foreach($root in $roots){if($full.StartsWith($root.TrimEnd('\')+'\',[StringComparison]::OrdinalIgnoreCase)){$boundary=$root;break}}
    if(-not $boundary){throw 'Background removal requires a machine-protected native executable.'}
    $trusted=@('S-1-5-18','S-1-5-32-544','S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464')
    for($part=$full;$part;$part=[IO.Path]::GetDirectoryName($part)){
        $acl=Get-Acl -LiteralPath $part -ErrorAction Stop
        if($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -cnotin $trusted){throw 'The native executable or parent has an untrusted owner.'}
        foreach($rule in $acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])){
            $mask=([long]$rule.FileSystemRights -band 4294967295)
            if($rule.AccessControlType -eq 'Allow' -and -not($rule.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly) -and $rule.IdentityReference.Value -cnotin $trusted -and ($mask -band 0x500D0156)){throw 'The native executable or parent is writable outside administrators.'}
        }
        if($part -ieq $boundary){return}
    }
    throw 'The native executable boundary could not be verified.'
}
function Test-OptimusRemovalGuardSameUser {
    param([string]$UserId,[string]$Sid)
    if($UserId -ceq $Sid){return $true}
    try{([Security.Principal.NTAccount]::new($UserId)).Translate([Security.Principal.SecurityIdentifier]).Value -ceq $Sid}catch{$false}
}
function Get-OptimusRemovalGuardScheduler { $service=New-Object -ComObject 'Schedule.Service';$service.Connect();$service }
function Read-OptimusRemovalGuardTask {
    param($Folder,[string]$Name)
    try{$Folder.GetTask($Name)}catch{$e=$_.Exception;for($i=0;$e -and $i -lt 16;$i++) {if($e.HResult -eq -2147024894 -or ($e -is [ComponentModel.Win32Exception] -and $e.NativeErrorCode -eq 2)){return $null};$e=$e.InnerException};throw}
}
function Get-OptimusRemovalGuardTaskName {
    param([string]$Sid,[string]$DataRoot)
    'Optimus-RemovalGuard-'+(Get-OptimusRemovalGuardDigest ([Text.Encoding]::UTF8.GetBytes($Sid+'|'+$DataRoot.ToLowerInvariant()))).Substring(0,20)
}
function Get-OptimusRemovalGuardEventQuery { '<QueryList><Query Id="0" Path="Microsoft-Windows-WindowsUpdateClient/Operational"><Select Path="Microsoft-Windows-WindowsUpdateClient/Operational">*[System[Provider[@Name=''Microsoft-Windows-WindowsUpdateClient''] and EventID=19]]</Select></Query></QueryList>' }
function Get-OptimusRemovalGuardArguments {
    param([string]$Base64)
    '-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -Command "$b=[Convert]::FromBase64String('''+$Base64+''');$m=[IO.MemoryStream]::new($b);$g=[IO.Compression.GZipStream]::new($m,[IO.Compression.CompressionMode]::Decompress);$r=[IO.StreamReader]::new($g,[Text.Encoding]::UTF8);$script:OptimusGuardEmbeddedBody=$r.ReadToEnd();&([scriptblock]::Create($script:OptimusGuardEmbeddedBody))"'
}
function Test-OptimusGuardProtectedTask {
    param($Task,[string]$Sid)
    try{
        $sd=[Security.AccessControl.RawSecurityDescriptor]::new([string]$Task.GetSecurityDescriptor(7))
        if($sd.Owner.Value -cnotin @('S-1-5-18','S-1-5-32-544') -or -not($sd.ControlFlags -band [Security.AccessControl.ControlFlags]::DiscretionaryAclProtected) -or $sd.DiscretionaryAcl.Count -ne 3){return $false}
        $seen=@{}
        foreach($ace in $sd.DiscretionaryAcl){
            $who=$ace.SecurityIdentifier.Value;$mask=[long]$ace.AccessMask -band 4294967295
            if($ace.AceType -ne [Security.AccessControl.AceType]::AccessAllowed -or $ace.AceFlags -ne 0 -or $seen.ContainsKey($who)){return $false}
            $seen[$who]=$true
            if($who -ceq $Sid){if($mask -notin @(2684354560,1179817)){return $false}}
            elseif($who -cin @('S-1-5-18','S-1-5-32-544')){if($mask -notin @(268435456,2032127)){return $false}}
            else{return $false}
        }
        return $true
    }catch{return $false}
}
function Read-OptimusGuardTaskBundle {
    param($Task,[string]$Sid,[string]$DataRoot)
    if(-not(Test-OptimusGuardProtectedTask $Task $Sid)){throw 'The removal task security descriptor is not protected.'}
    $d=$Task.Definition;$description=[string]$d.RegistrationInfo.Description
    if($d.RegistrationInfo.Source -cne 'Optimus/RemovalGuard/2' -or $description -cnotmatch '^Optimus approved removal guard v2 ([a-f0-9]{32}) ([a-f0-9]{64}) ([a-f0-9]{64})$'){throw 'The existing task is not this protected removal guard.'}
    $token=$Matches[1];$scopeHash=$Matches[2];$bodyHash=$Matches[3]
    if(-not(Test-OptimusRemovalGuardSameUser $d.Principal.UserId $Sid) -or $d.Principal.LogonType -ne 3 -or $d.Principal.RunLevel -ne 1 -or $d.Actions.Count -ne 1 -or $d.Triggers.Count -ne 3){throw 'The removal task principal or action count differs.'}
    $a=$d.Actions.Item(1)
    if($a.Type -ne 0 -or $a.Path -ine (Get-OptimusRemovalGuardPowerShell) -or $a.WorkingDirectory -ine [Environment]::GetFolderPath('System') -or $a.Arguments.Length -gt 30000){throw 'The removal task action differs.'}
    $match=[regex]::Match([string]$a.Arguments,"FromBase64String\('([A-Za-z0-9+/=]+)'\)")
    if(-not $match.Success -or [string]$a.Arguments -cne (Get-OptimusRemovalGuardArguments $match.Groups[1].Value)){throw 'The task action contains unexpected code.'}
    $bytes=[Convert]::FromBase64String($match.Groups[1].Value);$memory=[IO.MemoryStream]::new($bytes);$gzip=[IO.Compression.GZipStream]::new($memory,[IO.Compression.CompressionMode]::Decompress);$out=[IO.MemoryStream]::new()
    try{$chunk=New-Object byte[] 8192;while(($n=$gzip.Read($chunk,0,$chunk.Length)) -gt 0){if($out.Length+$n -gt 262144){throw 'The embedded task code exceeds its bound.'};$out.Write($chunk,0,$n)};$raw=$out.ToArray()}finally{$gzip.Dispose();$memory.Dispose();$out.Dispose()}
    if((Get-OptimusRemovalGuardDigest $raw) -cne $bodyHash){throw 'The embedded removal code differs from its protected description.'}
    $body=(New-Object Text.UTF8Encoding($false,$true)).GetString($raw)
    $match=[regex]::Match($body,"\A# Optimus embedded removal guard v2\r?\n\`$script:GuardContext=\(\[Text.Encoding\]::UTF8.GetString\(\[Convert\]::FromBase64String\('([A-Za-z0-9+/=]+)'\)\)\|ConvertFrom-Json\)\r?\n")
    if(-not $match.Success){throw 'The protected task context is unavailable.'}
    $context=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($match.Groups[1].Value))|ConvertFrom-Json
    if($context.schema -ne 2 -or $context.owner_sid -cne $Sid -or $context.data_root -ine $DataRoot -or $context.owner_token -cne $token -or $context.scope_hash -cne $scopeHash -or $context.task_name -cne (Get-OptimusRemovalGuardTaskName $Sid $DataRoot)){throw 'The protected removal scope belongs to another registration.'}
    if($d.Settings.MultipleInstances -ne 2 -or $d.Settings.ExecutionTimeLimit -cne 'PT0S' -or $d.Settings.AllowHardTerminate -or $d.Settings.StopIfGoingOnBatteries -or $d.Settings.DisallowStartIfOnBatteries){throw 'The task termination settings differ.'}
    $logon=$d.Triggers.Item(1);$time=$d.Triggers.Item(2);$event=$d.Triggers.Item(3)
    if($logon.Type -ne 9 -or -not(Test-OptimusRemovalGuardSameUser $logon.UserId $Sid) -or $logon.Delay -cne 'PT30S' -or $time.Type -ne 1 -or $time.StartBoundary -cne $context.time_start_boundary -or $time.Repetition.Interval -cne 'PT15M' -or [string]$time.Repetition.Duration -cne '' -or $time.Repetition.StopAtDurationEnd -or $event.Type -ne 0 -or $event.Subscription -cne (Get-OptimusRemovalGuardEventQuery) -or $event.Delay -cne 'PT1M'){throw 'The removal task schedule differs.'}
    foreach($t in @($logon,$time,$event)){if(-not $t.Enabled -or [string]$t.EndBoundary -cne ''){throw 'The removal task trigger differs.'}}
    [pscustomobject]@{context=$context;body=$body;payload_hash=$bodyHash}
}
function Test-OptimusGuardLiveApproval {
    param($Context)
    try{
        if(-not(Test-OptimusGuardCurrentConsent $Context)){return $false}
        $service=Get-OptimusRemovalGuardScheduler;$task=Read-OptimusRemovalGuardTask ($service.GetFolder('\')) $Context.task_name
        if(-not $task -or -not $task.Enabled){return $false}
        $bundle=Read-OptimusGuardTaskBundle $task $Context.owner_sid $Context.data_root
        return $bundle.context.owner_token -ceq $Context.owner_token -and $bundle.context.scope_hash -ceq $Context.scope_hash -and $bundle.body -ceq $script:OptimusGuardEmbeddedBody
    }catch{return $false}
}
function Get-OptimusRemovalGuardRuntimeText {
    @'
$ErrorActionPreference='Stop'
$sys=[Environment]::GetFolderPath('System')
$env:PSModulePath=Join-Path $PSHOME 'Modules'
$env:SystemRoot=[IO.Path]::GetDirectoryName($sys)
$env:windir=$env:SystemRoot
$env:PATH=$sys+';'+$env:SystemRoot+';'+(Join-Path $sys 'WindowsPowerShell\v1.0')
$env:ProgramFiles=[Environment]::GetFolderPath('ProgramFiles')
${env:ProgramFiles(x86)}=[Environment]::GetFolderPath('ProgramFilesX86')
Set-Location -LiteralPath $sys
if((Get-OptimusRemovalGuardSid) -cne $script:GuardContext.owner_sid -or -not(Test-OptimusGuardLiveApproval $script:GuardContext)){exit 2}
# Overrides cover both outer inventory and inner per-target revalidation.
$script:GuardOfficeRows=${function:Get-OptimusOfficeRegistryRows}
function Get-OptimusOfficeRegistryRows { & $script:GuardOfficeRows | Where-Object {$_.path.StartsWith('HKLM:\',[StringComparison]::OrdinalIgnoreCase)} }
$script:GuardEdgeRoots=${function:Get-OptimusEdgeKnownRoots}
function Get-OptimusEdgeKnownRoots { & $script:GuardEdgeRoots | Where-Object {$_.scope -ceq 'machine'} }
function Get-OptimusEdgeWinget { return $null }
function Remove-AppxPackage {
    [CmdletBinding(SupportsShouldProcess=$true,ConfirmImpact='None')]
    param([Parameter(Mandatory=$true)][string]$Package)
    if(-not(Test-OptimusGuardLiveApproval $script:GuardContext)){throw 'Removal approval was revoked.'}
    Appx\Remove-AppxPackage -Package $Package -Confirm:$false -ErrorAction Stop
}
function Remove-AppxProvisionedPackage {
    [CmdletBinding()]
    param([switch]$Online,[Parameter(Mandatory=$true)][string]$PackageName)
    if(-not $Online -or -not(Test-OptimusGuardLiveApproval $script:GuardContext)){throw 'Removal approval was revoked.'}
    Dism\Remove-AppxProvisionedPackage -Online -PackageName $PackageName -ErrorAction Stop
}
function Invoke-OptimusGuardNativeChild {
    param([string]$Path,[string[]]$Arguments)
    Assert-OptimusGuardMachineExecutable $Path
    if(-not(Test-OptimusGuardLiveApproval $script:GuardContext)){throw 'Removal approval was revoked.'}
    $child=Start-Process -FilePath $Path -ArgumentList $Arguments -WorkingDirectory ([Environment]::GetFolderPath('System')) -WindowStyle Hidden -PassThru -ErrorAction Stop
    try{$null=$child.Handle}finally{$child.WaitForExit()}
    $child.Refresh();if($null -eq $child.ExitCode){throw 'Native uninstall outcome is unknown.'};[int]$child.ExitCode
}
function Invoke-OptimusOfficeNativeUninstall { param($Payload) Invoke-OptimusGuardNativeChild ([string]$Payload.binary.path) @($Payload.arguments) }
function Invoke-OptimusEdgeProcess { param([string]$Path,[string[]]$Arguments) Invoke-OptimusGuardNativeChild $Path $Arguments }
$lease=$null;$issues=$false
try{
    $lock=Assert-OptimusRemovalGuardOrdinary (Join-Path $script:GuardContext.data_root 'maintenance-run.lock') -File
    $lease=[IO.File]::Open($lock,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::None)
    if(-not(Test-OptimusGuardLiveApproval $script:GuardContext)){exit 2}
    foreach($pair in @(@('consumer_debloat_enabled','consumer-apps'),@('m365_debloat_enabled','m365'),@('office365_desktop_debloat_enabled','office365-desktop'),@('office_desktop_debloat_enabled','office-desktop'),@('outlook_debloat_enabled','outlook'),@('edge_debloat_enabled','edge'))){
        if($script:GuardContext.approved_scope.flags.($pair[0]) -ne $true){continue}
        $action=$pair[1];$include=($action -notin @('office365-desktop','office-desktop'))
        try{
            if(-not(Test-OptimusGuardLiveApproval $script:GuardContext)){exit 2}
            $args=@{action=$action;includeProvisioned=$include;removalExclusions=@($script:GuardContext.approved_scope.exclusions)}
            $preview=Invoke-OptimusMaintenanceRequest -Operation maintenance_preview -Arguments $args
            foreach($target in @($preview.targets)){
                if(-not(Test-OptimusGuardLiveApproval $script:GuardContext)){exit 2}
                if(Test-OptimusPreservedPackage ($target.name+'|'+$target.packageFullName)){continue}
                if($target.kind -notin @('appx','provisioned','edge','office-clicktorun','office-msi')){$issues=$true;continue}
                if($action -eq 'consumer-apps' -and $target.name -cnotin @($script:GuardContext.approved_scope.consumer_packages)){continue}
                try{
                    $args.targets=@($target);$args.confirmation='REMOVE'
                    $result=Invoke-OptimusMaintenanceRequest -Operation maintenance_apply -Arguments $args
                    foreach($row in @($result.results)){if($row.removed -ne $true -or $row.requiresReview -eq $true){$issues=$true};if($row.outcomeKnown -eq $false -or $row.installerRunning -eq $true){exit 3}}
                }catch{$issues=$true}
            }
        }catch{$issues=$true}
    }
}catch{$issues=$true}finally{if($lease){$lease.Dispose()}}
if($issues){exit 3};exit 0
'@
}
function New-OptimusRemovalGuardBundle {
    param([string]$DataRoot,[string]$Sid,$ApprovedScope,[string]$ScopeHash)
    $scopeJson=ConvertTo-OptimusGuardCanonicalJson $ApprovedScope
    if($ScopeHash -cnotmatch '^[a-f0-9]{64}$' -or (Get-OptimusRemovalGuardDigest ([Text.Encoding]::UTF8.GetBytes($scopeJson))) -cne $ScopeHash -or $ApprovedScope.schema -ne 1 -or $ApprovedScope.include_provisioned -ne $true -or (ConvertTo-OptimusGuardCanonicalJson $ApprovedScope.preserved) -cne '["MicrosoftCorporationII.Windows365_8wekyb3d8bbwe","Microsoft.EdgeWebView2"]'){throw 'The administrator-approved scope is invalid.'}
    $context=[ordered]@{schema=2;owner_sid=$Sid;owner_token=[Guid]::NewGuid().ToString('N');data_root=$DataRoot;task_name=(Get-OptimusRemovalGuardTaskName $Sid $DataRoot);scope_hash=$ScopeHash;approved_scope=$ApprovedScope;time_start_boundary=(Get-Date).AddMinutes(1).ToString('yyyy-MM-ddTHH:mm:ss')}
    $encoded=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes((ConvertTo-OptimusGuardCanonicalJson $context)))
    $parts=@('# Optimus embedded removal guard v2',('$script:GuardContext=([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('''+$encoded+'''))|ConvertFrom-Json)'))
    $functions=@('Get-OptimusRemovalGuardSid','Get-OptimusRemovalGuardPowerShell','Get-OptimusRemovalGuardDigest','ConvertTo-OptimusGuardJsonString','ConvertTo-OptimusGuardCanonicalJson','Assert-OptimusRemovalGuardOrdinary','Read-OptimusGuardPreferences','Get-OptimusGuardPreferenceScope','Test-OptimusGuardCurrentConsent','Assert-OptimusGuardMachineExecutable','Test-OptimusRemovalGuardSameUser','Get-OptimusRemovalGuardScheduler','Read-OptimusRemovalGuardTask','Get-OptimusRemovalGuardTaskName','Get-OptimusRemovalGuardEventQuery','Get-OptimusRemovalGuardArguments','Test-OptimusGuardProtectedTask','Read-OptimusGuardTaskBundle','Test-OptimusGuardLiveApproval')
    $self=Assert-OptimusRemovalGuardOrdinary (Join-Path $script:OptimusRemovalGuardSourceRoot 'RemovalGuardTasks.ps1') -File
    $parseErrors=$null;$selfAst=[Management.Automation.Language.Parser]::ParseFile($self,[ref]$null,[ref]$parseErrors)
    if(@($parseErrors).Count){throw 'The native guard source is invalid.'}
    foreach($name in $functions){$nodes=@($selfAst.EndBlock.Statements|Where-Object {$_ -is [Management.Automation.Language.FunctionDefinitionAst] -and $_.Name -ceq $name});if($nodes.Count -ne 1){throw 'A required native guard function is unavailable.'};$parts+=$nodes[0].Extent.Text}
    foreach($name in @('OfficeRemoval.ps1','EdgeRemoval.ps1','MaintenanceOperations.ps1')){
        $path=Assert-OptimusRemovalGuardOrdinary (Join-Path $script:OptimusRemovalGuardSourceRoot $name) -File
        if((Get-Item -LiteralPath $path).Length -gt 131072){throw 'A native module exceeds its size limit.'}
        $code=[IO.File]::ReadAllText($path,[Text.Encoding]::UTF8);$errors=$null;$ast=[Management.Automation.Language.Parser]::ParseInput($code,[ref]$null,[ref]$errors)
        if(@($errors).Count -or @($ast.EndBlock.Statements|Where-Object {$_ -isnot [Management.Automation.Language.FunctionDefinitionAst]}).Count){throw 'A native module contains unsupported top-level code.'}
        $selected=@($ast.EndBlock.Statements)
        if($name -ceq 'MaintenanceOperations.ps1'){
            $allow=@('Test-OptimusMaintenanceAdmin','Get-OptimusMaintenanceHash','Test-OptimusPreservedPackage','Test-OptimusOptionalPackage','Test-OptimusExcludedRemovalTarget','New-OptimusProtectedRemovalResult','Get-OptimusMaintenanceTargets','Publish-OptimusMaintenanceProgress','Invoke-OptimusMaintenanceRequest')
            $selected=@($selected|Where-Object {$_.Name -cin $allow})
            if($selected.Count -ne $allow.Count){throw 'A required scoped maintenance function is unavailable.'}
        }elseif($name -ceq 'EdgeRemoval.ps1'){
            # Background guard neither writes policy backup records nor launches
            # per-user package managers. Those foreground functions are not embedded.
            $selected=@($selected|Where-Object {$_.Name -cnotin @('Get-OptimusEdgeWinget','Get-OptimusEdgeInstallPolicyContext','Set-OptimusEdgeInstallBlock')})
        }
        $parts+=@($selected|ForEach-Object {$_.Extent.Text})
    }
    $parts+=Get-OptimusRemovalGuardRuntimeText
    $body=$parts -join "`n";$raw=[Text.Encoding]::UTF8.GetBytes($body)
    if($raw.Length -gt 262144){throw 'The native guard exceeds its uncompressed size limit.'}
    $memory=[IO.MemoryStream]::new();$gzip=[IO.Compression.GZipStream]::new($memory,[IO.Compression.CompressionMode]::Compress,$true)
    try{$gzip.Write($raw,0,$raw.Length)}finally{$gzip.Dispose()}
    $base64=[Convert]::ToBase64String($memory.ToArray());$memory.Dispose();$arguments=Get-OptimusRemovalGuardArguments $base64
    if($arguments.Length -gt 30000){throw 'The native guard exceeds the Windows task command-line limit.'}
    [pscustomobject]@{context=[pscustomobject]$context;body=$body;arguments=$arguments;payload_hash=(Get-OptimusRemovalGuardDigest $raw);compressed_chars=$base64.Length}
}
function New-OptimusRemovalGuardTaskDefinition {
    param($Scheduler,$Bundle,[string]$Sid)
    $d=$Scheduler.NewTask(0);$d.RegistrationInfo.Source='Optimus/RemovalGuard/2'
    $d.RegistrationInfo.Description='Optimus approved removal guard v2 '+$Bundle.context.owner_token+' '+$Bundle.context.scope_hash+' '+$Bundle.payload_hash
    $d.Principal.UserId=$Sid;$d.Principal.LogonType=3;$d.Principal.RunLevel=1
    $d.Settings.Enabled=$true;$d.Settings.MultipleInstances=2;$d.Settings.ExecutionTimeLimit='PT0S';$d.Settings.AllowHardTerminate=$false
    $d.Settings.StopIfGoingOnBatteries=$false;$d.Settings.DisallowStartIfOnBatteries=$false;$d.Settings.RunOnlyIfIdle=$false;$d.Settings.StartWhenAvailable=$true
    $logon=$d.Triggers.Create(9);$logon.UserId=$Sid;$logon.Delay='PT30S';$logon.Enabled=$true
    $time=$d.Triggers.Create(1);$time.StartBoundary=$Bundle.context.time_start_boundary;$time.Repetition.Interval='PT15M';$time.Repetition.StopAtDurationEnd=$false;$time.Enabled=$true
    $event=$d.Triggers.Create(0);$event.Subscription=Get-OptimusRemovalGuardEventQuery;$event.Delay='PT1M';$event.Enabled=$true
    $a=$d.Actions.Create(0);$a.Path=Get-OptimusRemovalGuardPowerShell;$a.Arguments=$Bundle.arguments;$a.WorkingDirectory=[Environment]::GetFolderPath('System')
    $d
}
function Invoke-OptimusRemovalGuardRequest {
    param($Request)
    $operation=[string]$Request.operation
    if($operation -cnotin @('status','enable','disable')){throw 'Unknown removal-guard operation.'}
    $allowed=@('operation','dataRoot','launchManifest');if($operation -ceq 'enable'){$allowed+=@('approvedScope','scopeHash')}
    if($Request -is [Collections.IDictionary]){$Request=[pscustomobject]$Request}
    $keys=@($Request.PSObject.Properties.Name)
    if($keys.Count -ne $allowed.Count -or @($keys|Where-Object {$_ -cnotin $allowed}).Count){throw 'Unexpected removal-guard request fields.'}
    $sid=Get-OptimusRemovalGuardSid
    if($sid -cnotmatch '^S-1-5-21-(?:\d+-){2}\d+-\d+$'){throw 'An interactive user account is required.'}
    $data=(Assert-OptimusRemovalGuardOrdinary ([string]$Request.dataRoot)).TrimEnd('\')
    if($data -ieq [IO.Path]::GetPathRoot($data).TrimEnd('\') -or [string]$Request.launchManifest -ine (Join-Path $data 'startup-launch.json')){throw 'The removal-guard data directory differs.'}
    $name=Get-OptimusRemovalGuardTaskName $sid $data;$service=Get-OptimusRemovalGuardScheduler;$folder=$service.GetFolder('\');$task=Read-OptimusRemovalGuardTask $folder $name;$owned=$false;$bundle=$null
    if($task){$bundle=Read-OptimusGuardTaskBundle $task $sid $data;$owned=$true}
    if($operation -ceq 'enable'){
        if(-not(Test-OptimusRemovalGuardAdmin)){throw 'Administrator approval is required to register protected removal enforcement.'}
        $bundle=New-OptimusRemovalGuardBundle $data $sid $Request.approvedScope ([string]$Request.scopeHash)
        $definition=New-OptimusRemovalGuardTaskDefinition $service $bundle $sid
        # TASK_DONT_ADD_PRINCIPAL_ACE prevents Scheduler from granting the user write access.
        $flags=if($owned){20}else{18};$sddl='O:BAG:BAD:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GRGX;;;'+$sid+')'
        $null=$folder.RegisterTaskDefinition($name,$definition,$flags,$sid,$null,3,$sddl)
        try{$task=Read-OptimusRemovalGuardTask $folder $name;$actual=Read-OptimusGuardTaskBundle $task $sid $data;if(-not $task.Enabled -or $actual.body -cne $bundle.body){throw 'The registered removal task differs.'}}catch{try{$folder.DeleteTask($name,0)}catch{};throw}
        $owned=$true
    }elseif($operation -ceq 'disable' -and $task){
        if(-not(Test-OptimusRemovalGuardAdmin)){throw 'Administrator approval is required to remove protected enforcement.'}
        # Unregister only. Never stop a task that may be waiting for an active native installer.
        $folder.DeleteTask($name,0);$task=$null;$owned=$false
    }
    $enabled=($null -ne $task -and [bool]$task.Enabled)
    $status=[pscustomobject]@{available=$true;enabled=$enabled;owned=$owned;state=$(if($enabled){'enabled'}else{'disabled'});task_name=$name;data_root=$data;schedule='At sign-in, every 15 minutes, and after Windows Update installation';scope_hash=$(if($bundle){$bundle.context.scope_hash}else{''});last_result=$(if($task){$task.LastTaskResult}else{$null});self_contained=$true}
    [pscustomobject]@{ok=$true;status=$status}
}
if($RequestJson){
    try{[Console]::InputEncoding=[Text.UTF8Encoding]::new($false);[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);$json=[Console]::In.ReadToEnd();if($json.Length -gt 131072){throw 'Request exceeds its size limit.'};$request=$json|ConvertFrom-Json;$result=Invoke-OptimusRemovalGuardRequest $request;$result|ConvertTo-Json -Depth 20 -Compress;exit 0}catch{[pscustomobject]@{ok=$false;error=$_.Exception.Message}|ConvertTo-Json -Compress;exit 1}
}
