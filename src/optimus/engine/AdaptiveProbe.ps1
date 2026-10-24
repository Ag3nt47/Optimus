# Read-only connection-cost observation; unknown is never called unrestricted.
function Get-OptimusAdaptiveNetwork {
    try {
        $profile=[Windows.Networking.Connectivity.NetworkInformation,Windows.Networking.Connectivity,ContentType=WindowsRuntime]::GetInternetConnectionProfile()
        if($null -eq $profile){return @{metered=$null;available=$false;reason='No internet connection profile.'}}
        $cost=$profile.GetConnectionCost()
        $kind=[string]$cost.NetworkCostType
        $metered=$null
        if($kind -eq 'Unrestricted'){$metered=$false}
        elseif($kind -in @('Fixed','Variable')){$metered=$true}
        if($cost.Roaming -eq $true -or $cost.OverDataLimit -eq $true){$metered=$true}
        return @{metered=$metered;available=($null -ne $metered);cost_type=$kind}
    } catch {return @{metered=$null;available=$false;reason=$_.Exception.Message}}
}
