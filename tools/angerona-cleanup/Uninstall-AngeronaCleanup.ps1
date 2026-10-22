$ErrorActionPreference='Stop';Set-StrictMode -Version 2
Import-Module (Join-Path $PSScriptRoot 'TaskRegistration.psm1') -Force
Import-Module (Join-Path $PSScriptRoot 'SafeCleanup.psm1') -Force
Import-Module (Join-Path $PSScriptRoot 'StartupPermission.psm1') -Force
$config=Get-Content -LiteralPath (Join-Path $PSScriptRoot 'settings.json') -Encoding UTF8 -Raw|ConvertFrom-Json
$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
if($config.schema -ne 1 -or $config.owner_sid -ne $sid -or [IO.Path]::GetFullPath($config.install_directory).TrimEnd('\') -ine [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\') -or $config.installation_id -notmatch '^[a-f0-9]{32}$' -or $config.task_name -ne ('AngeronaDisposableCleanup-'+$sid)){throw 'Installation ownership mismatch; nothing was changed.'}
$task=Get-RegisteredCleanupTask -Name $config.task_name
if($task -and -not (Test-CleanupTaskOwnership -Task $task -Configuration $config)){throw 'Task ownership changed; no stop request was sent.'}
if(-not (Request-CleanupWatcherStop -Configuration $config)){throw 'The owned watcher did not confirm release of its lease. Its stop request remains active; inspect status before retrying uninstall.'}
Remove-RegisteredCleanupTask -Configuration $config
Write-Output 'Cleanup watcher removed. Installation files and reports remain on D:. Angerona source, state, and Windows environment were unchanged.'
