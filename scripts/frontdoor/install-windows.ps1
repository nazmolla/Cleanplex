# install-windows.ps1 - Cleanplex front door on Windows (Mode B, #184). Run in an elevated PowerShell.
#
# Windows cannot transparently redirect a port Plex already listens on, so Mode B:
#   1. nginx (front door) listens on port 32480 with Plex's own plex.direct certificate.
#   2. Plex publishes it to every client as a custom server access URL.
#   3. Windows Firewall blocks other machines from Plex's port 32400, so clients fail over to it.
# Plex keeps serving this machine and the front door on 127.0.0.1:32400.
#
# UNTESTED on real Windows clients: client failover to the custom URL is Gate 5 of #184.
#
#   .\install-windows.ps1 -NginxDir C:\nginx -CleanplexPython C:\Cleanplex\.venv\Scripts\python.exe
#   .\install-windows.ps1 -Uninstall

param(
    [string]$NginxDir = "C:\nginx",
    [string]$CleanplexPython = "$PSScriptRoot\..\..\.venv\Scripts\python.exe",
    [string]$DataDir = "$env:USERPROFILE\.cleanplex",
    [string]$CleanplexUrl = "http://127.0.0.1:7979",
    [int]$FrontPort = 32480,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$RuleName = "Cleanplex - block direct Plex access"
$TaskName = "Cleanplex front door"

function Set-Enforcement([bool]$On) {
    try {
        Invoke-RestMethod -Method Post -Uri "$CleanplexUrl/api/enforcement" -ContentType "application/json" `
            -Body (@{ enabled = $On } | ConvertTo-Json) | Out-Null
        Write-Host "[OK] Cleanplex enforcement set to $On"
    } catch { Write-Warning "Could not reach Cleanplex at $CleanplexUrl; set enforcement in Settings." }
}

if ($Uninstall) {
    Set-Enforcement $false
    Remove-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    & "$NginxDir\nginx.exe" -p $NginxDir -c "$DataDir\frontdoor\nginx.conf" -s stop 2>$null
    Write-Host "[OK] Front door removed. Clear the custom server access URL in Plex Settings > Network."
    exit 0
}

if (-not (Test-Path "$NginxDir\nginx.exe")) { throw "nginx.exe not found in $NginxDir (download nginx for Windows)." }

# 1. Certificate and config (as the account that runs Plex: it reads Plex's registry key).
$env:PYTHONPATH = (Resolve-Path "$PSScriptRoot\..\..").Path
$conf = & $CleanplexPython -m cleanplex.frontdoor $DataDir
& "$NginxDir\nginx.exe" -p $NginxDir -t -c $conf
if ($LASTEXITCODE -ne 0) { throw "nginx rejected $conf" }

# 2. Start nginx at logon of this account (it must read the certificate files it owns).
$action = New-ScheduledTaskAction -Execute "$NginxDir\nginx.exe" -Argument "-p `"$NginxDir`" -c `"$conf`"" -WorkingDirectory $NginxDir
$trigger = New-ScheduledTaskTrigger -AtStartup
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -RunLevel Highest -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 3
Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$FrontPort/identity" | Out-Null
Write-Host "[OK] Front door answering on $FrontPort"

# 3. Publish the front door to clients under the plex.direct name its certificate covers.
$ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -match '^(10|192\.168|172\.(1[6-9]|2\d|3[01]))\.' } | Select-Object -First 1).IPAddress
$cn = (& $CleanplexPython -c "from cryptography import x509;import sys;print(x509.load_pem_x509_certificate(open(sys.argv[1],'rb').read()).subject.rfc4514_string())" "$DataDir\tls\plex-cert.pem")
$hash = ($cn -replace '.*\*\.([0-9a-f]+)\.plex\.direct.*', '$1')
$url = "https://$($ip -replace '\.', '-').$hash.plex.direct:$FrontPort"
Write-Host "[INFO] Set this in Plex Settings > Network > Custom server access URLs: $url"

# 4. Block other machines from Plex's own port; loopback (the front door) is unaffected.
Remove-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Protocol TCP -LocalPort 32400 `
    -RemoteAddress Any -Action Block | Out-Null
New-NetFirewallRule -DisplayName "Cleanplex - front door" -Direction Inbound -Protocol TCP -LocalPort $FrontPort `
    -Action Allow -ErrorAction SilentlyContinue | Out-Null
Write-Host "[OK] Direct access to 32400 blocked for other machines"

Set-Enforcement $true
Write-Host "Undo: .\install-windows.ps1 -Uninstall"
