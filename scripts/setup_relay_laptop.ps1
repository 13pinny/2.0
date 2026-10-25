# One-shot setup for an always-on home "relay laptop" (Windows 10/11).
#
# Some sites (Eventim US, Shotgun, the DICE API) block the VPS's datacenter IP
# but load fine from a home connection. This machine runs the two desktop
# relays that fetch them from home and push the results to kartis.homes:
#   eventim_relay.py   every 5 min, its own Chrome (CDP :9333), eventim + shotgun
#   dice_relay.py      --loop, every 60s, plain HTTP
# Tixr and Marquee are NOT relayed from here: the VPS fetches Tixr itself
# (every 2 min, faster than this relay) and the box's own relay does Marquee.
#
# Run from the repo root, in a normal (not admin) PowerShell:
#   powershell -ExecutionPolicy Bypass -File scripts\setup_relay_laptop.ps1 -EnvFile <path to kartis-relay.env>
# Re-running is safe: it updates the tasks and .env in place.
param(
    [string]$EnvFile = "",
    [string]$Sources = "eventim,shotgun"
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
function Step($m) { Write-Host "`n== $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "   ok: $m" -ForegroundColor Green }
function Warn($m) { Write-Host "   !! $m" -ForegroundColor Yellow }

# --- Python venv ---------------------------------------------------------
Step "Python virtualenv"
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    $py = (Get-Command py -ErrorAction SilentlyContinue)
    if ($py) { & py -3 -m venv .venv } else { & python -m venv .venv }
    if (-not (Test-Path ".venv\Scripts\python.exe")) { throw "Could not create .venv - is Python 3.11+ installed and on PATH?" }
}
# The relays only need these two; the full requirements.txt is for the dashboard.
& .venv\Scripts\python.exe -m pip install --quiet --upgrade pip
& .venv\Scripts\python.exe -m pip install --quiet patchright python-dotenv
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
Ok (& .venv\Scripts\python.exe --version)

# --- Chrome --------------------------------------------------------------
Step "Google Chrome"
$chrome = @(
    "C:\Program Files\Google\Chrome\Application\chrome.exe",
    "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $chrome) { throw "Chrome not found - install it first (winget install Google.Chrome)" }
Ok $chrome

# --- .env ----------------------------------------------------------------
Step ".env"
$envPath = Join-Path $Root ".env"
$lines = @()
if (Test-Path $envPath) { $lines = @(Get-Content $envPath) }
function Set-EnvLine([string]$key, [string]$value) {
    $script:lines = @($script:lines | Where-Object { $_ -notmatch "^\s*$([regex]::Escape($key))=" })
    $script:lines += "$key=$value"
}
if ($EnvFile) {
    if (-not (Test-Path $EnvFile)) { throw "EnvFile not found: $EnvFile" }
    foreach ($l in Get-Content $EnvFile) {
        if ($l -match "^\s*([A-Z0-9_]+)=(.*)$") { Set-EnvLine $Matches[1] $Matches[2] }
    }
    Ok "copied keys from $EnvFile"
}
Set-EnvLine "KARTIS_RELAY_SOURCES" $Sources
if ($chrome -ne "C:\Program Files\Google\Chrome\Application\chrome.exe") { Set-EnvLine "KARTIS_CHROME_EXE" $chrome }
Set-Content -Path $envPath -Value $lines -Encoding ASCII
$missing = @("KARTIS_CVAUTH_SECRET", "KARTIS_WEB_USER", "KARTIS_WEB_PASS") |
    Where-Object { -not ($lines -match "^$_=.+") }
if ($missing) { throw "Missing in .env: $($missing -join ', ') - pass -EnvFile with the values from the main PC" }
Ok "relay sources: $Sources"

# --- Scheduled tasks -----------------------------------------------------
Step "Scheduled tasks"
$pyw = Join-Path $Root ".venv\Scripts\pythonw.exe"
$me = "$env:USERDOMAIN\$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited

$relayTriggers = @(
    (New-ScheduledTaskTrigger -AtLogOn -User $me),
    (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5))
)
$relaySettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName "Kartis relay (eventim + shotgun)" -Force -Principal $principal `
    -Action (New-ScheduledTaskAction -Execute $pyw -Argument "`"$Root\eventim_relay.py`"" -WorkingDirectory $Root) `
    -Trigger $relayTriggers -Settings $relaySettings | Out-Null
$rep = (Get-ScheduledTask -TaskName "Kartis relay (eventim + shotgun)").Triggers[1].Repetition
Ok "eventim/shotgun relay: every $($rep.Interval), for $(if ($rep.Duration) { $rep.Duration } else { 'ever' })"

$diceSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "Kartis DICE relay" -Force -Principal $principal `
    -Action (New-ScheduledTaskAction -Execute $pyw -Argument "dice_relay.py --loop" -WorkingDirectory $Root) `
    -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $me) -Settings $diceSettings | Out-Null
Ok "DICE relay: at logon, loops forever"

# Nightly code update (relay changes are rare; nothing else updates this box).
$git = (Get-Command git -ErrorAction SilentlyContinue).Source
if ($git) {
    Register-ScheduledTask -TaskName "Kartis relay update" -Force -Principal $principal `
        -Action (New-ScheduledTaskAction -Execute $git -Argument "-C `"$Root`" pull --ff-only" -WorkingDirectory $Root) `
        -Trigger (New-ScheduledTaskTrigger -Daily -At 4am) `
        -Settings (New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -StartWhenAvailable) | Out-Null
    Ok "git pull nightly at 4am"
} else { Warn "git not on PATH - skipped the nightly update task" }

# --- Power: never sleep on AC, lid closed = do nothing --------------------
Step "Power settings (plugged in)"
try {
    powercfg /change standby-timeout-ac 0 | Out-Null
    powercfg /change hibernate-timeout-ac 0 | Out-Null
    powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0 | Out-Null
    powercfg /setactive SCHEME_CURRENT | Out-Null
    Ok "no sleep / no hibernate on AC; closing the lid does nothing"
} catch { Warn "couldn't change power settings ($_) - set them by hand (Settings > System > Power)" }

# --- Smoke test ----------------------------------------------------------
Step "Test: one DICE relay pass"
& .venv\Scripts\python.exe dice_relay.py
Step "Test: one eventim/shotgun relay pass (opens its own Chrome off-screen)"
& .venv\Scripts\python.exe eventim_relay.py
Get-Content "logs\eventim_relay.log" -Tail 8 -ErrorAction SilentlyContinue

Step "Starting the tasks"
Start-ScheduledTask -TaskName "Kartis DICE relay"
Start-ScheduledTask -TaskName "Kartis relay (eventim + shotgun)"
Ok "running. Logs: $Root\logs\dice_relay.log and eventim_relay.log"
Write-Host "`nDone. Tell Claude it's running so it can turn the relays off on the main PC and the VPS." -ForegroundColor Cyan
