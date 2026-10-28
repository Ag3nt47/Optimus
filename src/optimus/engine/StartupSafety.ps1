# Defines helpers only. No registration, directory creation, deletion or retry.
function Get-OptimusStartupWindowsDirectory { return [Environment]::GetFolderPath('Windows') }
function Get-OptimusStartupAvailableFree([string]$Volume) { return ([IO.DriveInfo]::new($Volume)).AvailableFreeSpace }
function Assert-OptimusStartupHeadroom {
    $windows=Get-OptimusStartupWindowsDirectory
    if($windows -notmatch '^[A-Za-z]:\\') { throw 'The Windows system volume is unavailable.' }
    $volume=[IO.Path]::GetPathRoot($windows)
    $free=Get-OptimusStartupAvailableFree $volume
    if($null -eq $free -or $free -isnot [long]){throw 'Available Windows system-volume space is unknown.'}
    if($free -lt 2147483648){
        $error=[IO.IOException]::new('At least 2 GiB free on the Windows system volume is required before task registration.')
        $error.Data['OptimusFailureKind']='system_volume_space'
        throw $error
    }
    return @{system_volume=$volume;free_bytes=[long]$free;required_bytes=2147483648}
}
function ConvertTo-OptimusCanonicalTaskXml([string]$Text) {
    if([string]::IsNullOrWhiteSpace($Text) -or $Text.Length -gt 65536){throw 'Persisted task XML is empty or exceeds its bound.'}
    $settings=[Xml.XmlReaderSettings]::new();$settings.DtdProcessing=[Xml.DtdProcessing]::Prohibit;$settings.XmlResolver=$null;$settings.MaxCharactersInDocument=65536
    $reader=[Xml.XmlReader]::Create([IO.StringReader]::new($Text),$settings)
    try{$document=[Xml.XmlDocument]::new();$document.PreserveWhitespace=$false;$document.XmlResolver=$null;$document.Load($reader)}finally{$reader.Dispose()}
    if($document.DocumentElement.LocalName -cne 'Task' -or $document.DocumentElement.NamespaceURI -cne 'http://schemas.microsoft.com/windows/2004/02/mit/task'){throw 'Persisted task XML has an unexpected root.'}
    # The principalType xs:all permits Windows to reorder these singleton children.
    # https://learn.microsoft.com/windows/win32/taskschd/taskschedulerschema-principaltype-complextype
    # Keep every value/attribute and all other XML order, including Actions/Triggers.
    $namespace=[Xml.XmlNamespaceManager]::new($document.NameTable)
    $namespace.AddNamespace('t','http://schemas.microsoft.com/windows/2004/02/mit/task')
    $principalOrder=@('UserId','LogonType','GroupId','DisplayName','RunLevel','ProcessTokenSidType','RequiredPrivileges')
    foreach($principal in $document.SelectNodes('/t:Task/t:Principals/t:Principal',$namespace)){
        $singletons=@{}
        foreach($child in @($principal.ChildNodes)){
            if($child.NodeType -ne [Xml.XmlNodeType]::Element -or $child.NamespaceURI -cne $document.DocumentElement.NamespaceURI -or $child.LocalName -cnotin $principalOrder -or $singletons.ContainsKey($child.LocalName)){
                throw 'Persisted task Principal has an unknown, indirect or duplicate child.'
            }
            $singletons[$child.LocalName]=$child
        }
        foreach($name in $principalOrder){
            if($singletons.ContainsKey($name)){[void]$principal.AppendChild($singletons[$name])}
        }
    }
    Add-Type -AssemblyName System.Security
    $canonical=[Security.Cryptography.Xml.XmlDsigC14NTransform]::new();$canonical.LoadInput($document)
    $stream=$canonical.GetOutput([IO.Stream]);$hash=[Security.Cryptography.SHA256]::Create()
    try{return ([BitConverter]::ToString($hash.ComputeHash($stream))).Replace('-','').ToLowerInvariant()}finally{$stream.Dispose();$hash.Dispose()}
}
function Read-OptimusPersistedTask([string]$Name,[string]$ExpectedXml) {
    try{
        if($Name -notmatch '^Optimus-SignIn-S-1-\d+(?:-\d+){1,15}$'){throw 'The exact owned task name is required.'}
        $windows=Get-OptimusStartupWindowsDirectory
        $path=Join-Path $windows ('System32\Tasks\'+$Name)
        $current=$path
        while($current){$info=Get-Item -LiteralPath $current -Force -ErrorAction Stop;if($info.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Indirect persisted task storage is refused.'};$current=[IO.Path]::GetDirectoryName($current)}
        $stream=[IO.File]::Open($path,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::ReadWrite)
        try{
            $length=$stream.Length
            if($length -le 0 -or $length -gt 262144){throw 'Persisted task storage is empty or exceeds its bound.'}
            $reader=[IO.StreamReader]::new($stream,[Text.Encoding]::UTF8,$true)
            try{$xml=$reader.ReadToEnd();if($stream.Length -ne $length){throw 'Persisted task storage changed during readback.'}}finally{$reader.Dispose()}
        }finally{$stream.Dispose()}
        $digest=ConvertTo-OptimusCanonicalTaskXml $xml
        if($digest -cne (ConvertTo-OptimusCanonicalTaskXml $ExpectedXml)){throw 'Persisted task XML differs from the native registered definition.'}
        return @{verified=$true;canonical_sha256=$digest;bytes=[long]$length;path=$path}
    }catch{
        $error=[IO.IOException]::new('Persisted sign-in task storage could not be independently verified; inspect its actual state before retrying.',$_.Exception)
        $error.Data['OptimusFailureKind']='persisted_task_unverified'
        throw $error
    }
}
function Get-OptimusStartupFailure($Exception,[bool]$RegistrationStarted) {
    $chain=@();$kind=$null;$at=$Exception
    for($depth=0;$at -and $depth -lt 8;$depth++){
        $row=@{type=$at.GetType().FullName;hresult=('0x'+([BitConverter]::ToUInt32([BitConverter]::GetBytes([int]$at.HResult),0)).ToString('X8'))}
        if($at -is [ComponentModel.Win32Exception]){$row.win32_code=$at.NativeErrorCode}
        if($at.Data['OptimusFailureKind'] -in @('system_volume_space','persisted_task_unverified')){$kind=$at.Data['OptimusFailureKind']}
        $chain+=,$row;$at=$at.InnerException
    }
    return @{ok=$false;error='Windows startup operation failed; inspect its actual state before retrying.';native_error_chain=$chain;failure_kind=$kind;task_registration_started=$RegistrationStarted}
}
