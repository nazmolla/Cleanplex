# One-line installer / updater / uninstaller for Hayaa for Plex (Windows PowerShell 5.1+).
# Usage:
#   irm https://nazmolla.github.io/Cleanplex/install.ps1 | iex
#   $env:HAYAA_ALLOW_NETWORK="1"; irm https://nazmolla.github.io/Cleanplex/install.ps1 | iex
#   $env:HAYAA_UNINSTALL="1"; irm https://nazmolla.github.io/Cleanplex/install.ps1 | iex

# Options come from environment variables, because `irm ... | iex` cannot pass parameters:
#   HAYAA_ALLOW_NETWORK=1, HAYAA_FRONTDOOR=1 or 0, HAYAA_UNINSTALL=1, HAYAA_BUNDLE_URL=<url or path>
$Uninstall = $env:HAYAA_UNINSTALL -eq "1"
$AllowNetwork = $env:HAYAA_ALLOW_NETWORK -eq "1"
$Frontdoor = $env:HAYAA_FRONTDOOR -eq "1"
$NoFrontdoor = $env:HAYAA_FRONTDOOR -eq "0"

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$AppDir = Join-Path $env:LOCALAPPDATA "HayaaForPlex"
$DataDir = Join-Path $env:USERPROFILE ".cleanplex"
$TaskName = "Hayaa for Plex"
$Port = 7979
$DefaultBundleUrl = "https://github.com/nazmolla/Cleanplex/releases/latest/download/hayaa-for-plex.zip"
$InstallDocsUrl = "https://nazmolla.github.io/Cleanplex/getting-started/installation/"

function Write-Step([string]$Message) {
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-Ok([string]$Message) {
    Write-Host "[OK] $Message" -ForegroundColor Green
}

function Write-Warn([string]$Message) {
    Write-Host "[!]  $Message" -ForegroundColor Yellow
}

function Stop-HayaaApp([string]$InstallDir, [string]$ScheduledTaskName) {
    try {
        Stop-ScheduledTask -TaskName $ScheduledTaskName -ErrorAction SilentlyContinue
    } catch {}
    $procs = Get-Process -Name "cleanplex" -ErrorAction SilentlyContinue
    foreach ($p in $procs) {
        try {
            if ($p.Path -and $p.Path.StartsWith($InstallDir, [System.StringComparison]::OrdinalIgnoreCase)) {
                Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
            }
        } catch {}
    }
    Start-Sleep -Seconds 1
}

# ── Uninstall ─────────────────────────────────────────────────────────────────
if ($Uninstall -or ($env:HAYAA_UNINSTALL -eq "1")) {
    Remove-Item Env:HAYAA_UNINSTALL -ErrorAction SilentlyContinue
    Write-Step "Uninstalling Hayaa for Plex..."
    Stop-HayaaApp -InstallDir $AppDir -ScheduledTaskName $TaskName
    try {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    } catch {}
    if (Test-Path $AppDir) {
        Remove-Item -LiteralPath $AppDir -Recurse -Force
    }
    Write-Ok "Hayaa for Plex has been removed."
    Write-Host "    Your settings and scanned library were kept in: $DataDir"
    return
}

# ── Check Plex on this PC (same computer is required) ─────────────────────────
$plexKey = "HKCU:\Software\Plex, Inc.\Plex Media Server"
if (Test-Path $plexKey) {
    Write-Ok "Found Plex Media Server for your Windows account."
} else {
    Write-Host "[ERROR] Plex Media Server was not found on this computer. Install Hayaa for Plex on the computer that runs Plex." -ForegroundColor Red
    Write-Host "        See all install options: $InstallDocsUrl"
    throw "Plex Media Server was not found on this computer."
}

# ── Network binding choice ────────────────────────────────────────────────────
$allowNet = $AllowNetwork -or ($env:HAYAA_ALLOW_NETWORK -eq "1")
$bindHost = if ($allowNet) { "0.0.0.0" } else { "127.0.0.1" }
if ($allowNet) {
    Write-Warn "Network access enabled: anyone on your network can open and change Hayaa for Plex because it has no login."
}

# ── Download & extract bundle (preserving .venv and .uv) ──────────────────────
$bundleUrl = if ($env:HAYAA_BUNDLE_URL) { $env:HAYAA_BUNDLE_URL } else { $DefaultBundleUrl }
$tmpRoot = Join-Path $env:TEMP ("hayaa-install-" + [guid]::NewGuid().ToString("N"))
$tmpZip = Join-Path $tmpRoot "hayaa-for-plex.zip"
$tmpExtract = Join-Path $tmpRoot "extracted"
New-Item -ItemType Directory -Path $tmpExtract -Force | Out-Null

try {
    Write-Step "Downloading Hayaa for Plex..."
    if (Test-Path -LiteralPath $bundleUrl) {
        Copy-Item -LiteralPath $bundleUrl -Destination $tmpZip -Force
    } else {
        Invoke-WebRequest -UseBasicParsing -Uri $bundleUrl -OutFile $tmpZip
    }

    Expand-Archive -LiteralPath $tmpZip -DestinationPath $tmpExtract -Force
    $sourceRoot = $tmpExtract
    if (-not (Test-Path (Join-Path $sourceRoot "pyproject.toml"))) {
        $sub = Get-ChildItem -LiteralPath $tmpExtract -Directory | Select-Object -First 1
        if ($sub -and (Test-Path (Join-Path $sub.FullName "pyproject.toml"))) {
            $sourceRoot = $sub.FullName
        }
    }

    Write-Step "Installing files into $AppDir..."
    Stop-HayaaApp -InstallDir $AppDir -ScheduledTaskName $TaskName
    New-Item -ItemType Directory -Path $AppDir -Force | Out-Null

    Get-ChildItem -LiteralPath $AppDir -Force | Where-Object {
        $_.Name -ne ".venv" -and $_.Name -ne ".uv"
    } | ForEach-Object {
        Remove-Item -LiteralPath $_.FullName -Recurse -Force
    }

    Get-ChildItem -LiteralPath $sourceRoot -Force | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $AppDir -Recurse -Force
    }
    Write-Ok "Files updated."
} finally {
    if (Test-Path $tmpRoot) {
        Remove-Item -LiteralPath $tmpRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# ── Set up uv, Python 3.12, and package inside $AppDir ────────────────────────
$uvBinDir = Join-Path $AppDir ".uv\bin"
$uvExe = Join-Path $uvBinDir "uv.exe"
$env:UV_INSTALL_DIR = $uvBinDir
$env:UV_PYTHON_INSTALL_DIR = Join-Path $AppDir ".uv\python"
$env:UV_CACHE_DIR = Join-Path $AppDir ".uv\cache"
$env:UV_NO_MODIFY_PATH = "1"

if (-not (Test-Path $uvExe)) {
    Write-Step "Setting up Python installer (uv)..."
    Invoke-RestMethod -UseBasicParsing "https://astral.sh/uv/install.ps1" | Invoke-Expression
}

$venvDir = Join-Path $AppDir ".venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Step "Downloading Python 3.12 (first run only)..."
    & $uvExe venv --python 3.12 $venvDir
    if ($LASTEXITCODE -ne 0) { throw "Failed to create Python environment." }
}

Write-Step "Installing Hayaa for Plex..."
& $uvExe pip install --python $venvPython --reinstall-package cleanplex $AppDir
if ($LASTEXITCODE -ne 0) { throw "Failed to install Hayaa for Plex Python package." }
Write-Ok "Python environment ready."

# ── Ensure ffmpeg ─────────────────────────────────────────────────────────────
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    $wingetCmd = Get-Command winget -ErrorAction SilentlyContinue
    if ($wingetCmd) {
        Write-Step "Installing ffmpeg (needed to scan videos)..."
        try {
            & $wingetCmd.Source install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
            if ($LASTEXITCODE -eq 0) {
                $machinePath = [System.Environment]::GetEnvironmentVariable("Path", "Machine")
                $userPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
                $env:PATH = "$machinePath;$userPath"
                Write-Ok "ffmpeg installed."
            } else {
                Write-Warn "winget could not install ffmpeg. Install it manually from https://www.gyan.dev/ffmpeg/builds/ so scanning works."
            }
        } catch {
            Write-Warn "Could not install ffmpeg automatically. Install it manually from https://www.gyan.dev/ffmpeg/builds/ so scanning works."
        }
    } else {
        Write-Warn "ffmpeg is not installed. Download it from https://www.gyan.dev/ffmpeg/builds/ or install with: winget install Gyan.FFmpeg"
    }
}

# ── Register hidden autostart Scheduled Task ──────────────────────────────────
Write-Step "Configuring automatic startup..."
$cleanplexExe = Join-Path $venvDir "Scripts\cleanplex.exe"
$vbsPath = Join-Path $AppDir "hayaa-launcher.vbs"
$vbsContent = @"
Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = "$AppDir"
Set env = shell.Environment("PROCESS")
env("CLEANPLEX_HOST") = "$bindHost"
env("CLEANPLEX_PROXY_HOST") = "127.0.0.1"
env("CLEANPLEX_PORT") = "$Port"
rc = shell.Run("""$cleanplexExe""", 0, True)
WScript.Quit rc
"@
Set-Content -LiteralPath $vbsPath -Value $vbsContent -Encoding ASCII

$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "//B //Nologo `"$vbsPath`"" -WorkingDirectory $AppDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 0) `
    -Hidden

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null

# ── Optional Windows Firewall rule (only when HAYAA_ALLOW_NETWORK=1) ───────
if ($allowNet) {
    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    $fwCommand = "New-NetFirewallRule -DisplayName 'Hayaa for Plex (Web UI)' -Direction Inbound -Protocol TCP -LocalPort $Port -Profile Private -Action Allow"
    if ($isAdmin) {
        Remove-NetFirewallRule -DisplayName "Hayaa for Plex (Web UI)" -ErrorAction SilentlyContinue
        New-NetFirewallRule -DisplayName "Hayaa for Plex (Web UI)" -Direction Inbound -Protocol TCP -LocalPort $Port -Profile Private -Action Allow | Out-Null
        Write-Ok "Added Windows Firewall rule for port $Port (Private networks)."
    } else {
        Write-Warn "To let other devices on your network reach port $Port, run this in an Administrator PowerShell:"
        Write-Host "    $fwCommand"
    }
}

Start-ScheduledTask -TaskName $TaskName

# ── Wait for Web UI ───────────────────────────────────────────────────────────
Write-Step "Waiting for Hayaa for Plex to start..."
$ready = $false
$probeUrl = "http://127.0.0.1:$Port/"
for ($i = 0; $i -lt 60; $i++) {
    try {
        $resp = Invoke-WebRequest -UseBasicParsing -Uri $probeUrl -TimeoutSec 2
        if ($resp.StatusCode -ge 200 -and $resp.StatusCode -lt 500) {
            $ready = $true
            break
        }
    } catch {}
    Start-Sleep -Seconds 1
}

# ── Optional front door (filter every Plex client) ────────────────────────────
$fdChoice = ""
if ($Frontdoor) {
    $fdChoice = "yes"
} elseif ($NoFrontdoor) {
    $fdChoice = "no"
} elseif ($env:HAYAA_FRONTDOOR -eq "1") {
    $fdChoice = "yes"
} elseif ($env:HAYAA_FRONTDOOR -eq "0") {
    $fdChoice = "no"
}

Write-Host ""
Write-Host "  The front door opens nothing new to the internet: clients keep using Plex's usual port."
if (-not $fdChoice) {
    $ans = ""
    try {
        $ans = Read-Host "  Also filter Plex on every device (TVs, phones, web)? This routes Plex's port 32400 through Hayaa's front door. [y/N]"
    } catch { $ans = "" }
    if ($ans -match '^(y|yes)$') { $fdChoice = "yes" } else { $fdChoice = "no" }
}

if ($fdChoice -eq "yes") {
    $fdScript = Join-Path $AppDir "scripts\frontdoor\install-windows.ps1"
    Write-Step "Front door setup on Windows requires an Administrator PowerShell (and nginx for Windows in C:\nginx)."
    Write-Host "    Open PowerShell as Administrator and run:"
    Write-Host "    & `"$fdScript`""
}

# ── Open browser & print final URL ────────────────────────────────────────────
$openUrl = "http://localhost:$Port"
$displayUrl = $openUrl
if ($allowNet) {
    $lanIp = $null
    try {
        $lanIp = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object {
            $_.IPAddress -match '^(10|192\.168|172\.(1[6-9]|2\d|3[01]))\.'
        } | Select-Object -First 1).IPAddress
    } catch {}
    if ($lanIp) {
        $displayUrl = "http://${lanIp}:$Port"
    }
}

Write-Host ""
if ($ready) {
    Start-Process $openUrl
    Write-Ok "Hayaa for Plex is running. Open $displayUrl to finish setup."
} else {
    Write-Warn "Hayaa for Plex is still starting up. Open $displayUrl in a moment to finish setup."
}
