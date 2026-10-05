param(
    [string]$WslDistribution = "Ubuntu-24.04",
    [string]$ListenAddress,
    [int]$Port = 5000,
    [string]$AllowedVmAddress
)

$labConfigPath = Join-Path $PSScriptRoot "..\lab-config.json"
$labConfig = Get-Content -Raw -Path $labConfigPath | ConvertFrom-Json
if (-not $PSBoundParameters.ContainsKey("ListenAddress")) {
    $ListenAddress = $labConfig.host_only_ip
}
if (-not $PSBoundParameters.ContainsKey("AllowedVmAddress")) {
    $AllowedVmAddress = $labConfig.vm_ips.monitoring01
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this script from an elevated PowerShell window."
}

$wslAddresses = & wsl.exe -d $WslDistribution hostname -I
if ($LASTEXITCODE -ne 0) {
    throw "Could not query the WSL address for distribution '$WslDistribution'."
}

$wslAddress = [regex]::Match(($wslAddresses -join " "), '\b(?:\d{1,3}\.){3}\d{1,3}\b').Value
if (-not $wslAddress) {
    throw "No IPv4 address was returned for WSL distribution '$WslDistribution'."
}

Start-Service -Name iphlpsvc

$existingRule = Get-NetFirewallRule -DisplayName "Enterprise SRE Automation API" -ErrorAction SilentlyContinue
if ($existingRule) {
    $existingRule | Remove-NetFirewallRule
}
New-NetFirewallRule `
    -DisplayName "Enterprise SRE Automation API" `
    -Direction Inbound `
    -Action Allow `
    -Protocol TCP `
    -LocalAddress $ListenAddress `
    -LocalPort $Port `
    -RemoteAddress $AllowedVmAddress `
    -Profile Any | Out-Null

& netsh.exe interface portproxy delete v4tov4 `
    "listenaddress=$ListenAddress" "listenport=$Port" 2>$null | Out-Null
& netsh.exe interface portproxy add v4tov4 `
    "listenaddress=$ListenAddress" `
    "listenport=$Port" `
    "connectaddress=$wslAddress" `
    "connectport=$Port"
if ($LASTEXITCODE -ne 0) {
    throw "Failed to configure Windows port forwarding to WSL at $wslAddress`:$Port."
}

Write-Output "Forwarding ${ListenAddress}:$Port to WSL ${wslAddress}:$Port; firewall allows only $AllowedVmAddress."
