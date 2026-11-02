#requires -Version 5.1
# Parse the real bridge and load only its dispatch function. Never bootstrap the
# bridge, import hardware collectors, query Windows, or start any child process.
param([Parameter(Mandatory=$true)][string]$ReceiptsPath)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
$source=Join-Path $PSScriptRoot '../src/optimus/engine/OptimusBridge.ps1'
$tokens=$null;$parseErrors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($source,[ref]$tokens,[ref]$parseErrors)
if($parseErrors.Count){throw 'The actual bridge source must parse before dispatch is tested.'}
$definitions=@($ast.FindAll({param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -ceq 'Invoke-OptimusRequest'
},$true))
if($definitions.Count -ne 1){throw 'Exactly one actual Invoke-OptimusRequest definition is required.'}
. ([scriptblock]::Create($definitions[0].Extent.Text))
$script:CollectorCalls=New-Object 'System.Collections.Generic.List[object]'
$script:CallbackCalls=New-Object 'System.Collections.Generic.List[object]'
function New-OptimusCancellationCallback {
    param($Request)
    $script:CallbackCalls.Add($Request)
    if($Request.arguments.cooperativeCancellation -isnot [bool] -or -not $Request.arguments.cooperativeCancellation){return $null}
    $cancel=$Request.arguments.fixture_cancel
    return {return $cancel}.GetNewClosure()
}
function Get-OptimusHardwareHealth {
    param([scriptblock]$CancelRequested,[switch]$ForceRefresh)
    $cancelled=$false
    if($null -ne $CancelRequested){$cancelled=& $CancelRequested}
    $record=[pscustomobject]@{force_refresh=[bool]$ForceRefresh;callback_present=($null -ne $CancelRequested);cancelled=$cancelled}
    $script:CollectorCalls.Add($record)
    return $record
}
function Invoke-DispatchFixture {
    param([string]$Name,[bool]$IncludeForce=$false,$Force=$null,[bool]$Cooperative=$true,[bool]$Cancel=$false,
        [bool]$IncludeRootForce=$false,$RootForce=$null,[bool]$ExpectRejected=$false)
    $script:CollectorCalls.Clear();$script:CallbackCalls.Clear()
    $arguments=[ordered]@{cooperativeCancellation=$Cooperative;fixture_cancel=$Cancel}
    if($IncludeForce){$arguments.force_refresh=$Force}
    $envelope=[ordered]@{id=('a'*32);operation='hardware_health';arguments=$arguments}
    if($IncludeRootForce){$envelope.force_refresh=$RootForce}
    # This round trip is the wire representation that Python sends: operation
    # controls live under Request.arguments, not at the envelope root.
    $request=$envelope | ConvertTo-Json -Depth 6 -Compress | ConvertFrom-Json
    $rejected=$false;$result=$null
    try {$result=Invoke-OptimusRequest -Request $request}catch{$rejected=$true}
    if($rejected -ne $ExpectRejected){throw ('Unexpected dispatch validation for fixture '+$Name)}
    if($ExpectRejected){
        if($script:CollectorCalls.Count -ne 0 -or $script:CallbackCalls.Count -ne 0){throw 'Malformed force input must stop before native/cancellation dispatch.'}
    }else{
        if($script:CollectorCalls.Count -ne 1 -or $script:CallbackCalls.Count -ne 1){throw 'One valid dispatch must call the collector and cancellation factory once.'}
        if(-not [object]::ReferenceEquals($script:CallbackCalls[0],$request)){throw 'Cancellation must receive the exact original request object.'}
        $expected=($IncludeForce -and $Force -is [bool] -and $Force)
        if($result.force_refresh -ne $expected){throw ('The actual bridge failed to forward nested force_refresh for '+$Name)}
        if($result.callback_present -ne $Cooperative -or $result.cancelled -ne ($Cooperative -and $Cancel)){
            throw 'The actual bridge must preserve the cancellation callback result.'
        }
    }
    return [pscustomobject]@{name=$Name;rejected=$rejected;collector_calls=$script:CollectorCalls.Count;
        callback_factory_calls=$script:CallbackCalls.Count;result=$result}
}
$receipts=@(
    Invoke-DispatchFixture -Name missing
    Invoke-DispatchFixture -Name nested_true -IncludeForce $true -Force $true
    Invoke-DispatchFixture -Name nested_false -IncludeForce $true -Force $false
    Invoke-DispatchFixture -Name root_true_nested_false -IncludeForce $true -Force $false -IncludeRootForce $true -RootForce $true
    Invoke-DispatchFixture -Name root_only_ignored -IncludeRootForce $true -RootForce $true
    Invoke-DispatchFixture -Name string_true -IncludeForce $true -Force 'true' -ExpectRejected $true
    Invoke-DispatchFixture -Name integer_one -IncludeForce $true -Force 1 -ExpectRejected $true
    Invoke-DispatchFixture -Name explicit_null -IncludeForce $true -Force $null -ExpectRejected $true
    Invoke-DispatchFixture -Name cooperative_cancel -IncludeForce $true -Force $true -Cancel $true
    Invoke-DispatchFixture -Name no_cooperative_callback -IncludeForce $true -Force $true -Cooperative $false -Cancel $true
)
[IO.File]::WriteAllText($ReceiptsPath,($receipts | ConvertTo-Json -Depth 8),[Text.UTF8Encoding]::new($false))
[pscustomobject]@{Result='PASS';Cases=$receipts.Count;RealQueries=0;HardwareProcessesStarted=0;FullBridgeBootstraps=0;ActualDispatchAst=$true} | ConvertTo-Json -Compress
