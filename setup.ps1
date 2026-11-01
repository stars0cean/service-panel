#requires -Version 5.1
<#
  One-shot setup for the Service Panel on a fresh Windows machine.

    powershell -ExecutionPolicy Bypass -File .\setup.ps1

  What it does:
    1. check python (3.10+)
    2. create .venv and install requirements.txt
    3. create services.json from services.example.json if missing
    4. register the ServicePanel scheduled task (auto start at logon)
    5. start it and open the browser

  Re-running is safe: every step detects "already done" and skips.
#>
param(
  [string]$IndexUrl = '',
  [switch]$NoTask,
  [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$dir = $PSScriptRoot
Set-Location $dir

function Say($msg) { Write-Host $msg }
function Step($msg) { Write-Host ''; Write-Host ('==> ' + $msg) -ForegroundColor Cyan }

# --- 1. python -------------------------------------------------------------
Step 'Locating python'
$base = $null
foreach ($c in @('python.exe', 'py.exe')) {
  $cmd = Get-Command $c -ErrorAction SilentlyContinue
  if ($cmd) { $base = $cmd.Source; break }
}
if (-not $base) { throw 'python not found. Install Python 3.10+ and add it to PATH.' }

$ver = & $base -c "import sys;print('%d.%d' % sys.version_info[:2])"
Say ('found python ' + $ver)
if ([version]$ver -lt [version]'3.10') { throw ('python ' + $ver + ' is too old, need 3.10+') }

# --- 2. venv + deps --------------------------------------------------------
$venvPy = Join-Path $dir '.venv\Scripts\python.exe'
if (Test-Path $venvPy) {
  Say '.venv already exists, skipping creation'
} else {
  Step 'Creating .venv'
  & $base -m venv (Join-Path $dir '.venv')
  if ($LASTEXITCODE -ne 0) { throw 'venv creation failed' }
}

Step 'Installing requirements'
$pipArgs = @('-m', 'pip', 'install', '--upgrade', 'pip', '-q')
if ($IndexUrl) { $pipArgs += @('-i', $IndexUrl) }
& $venvPy @pipArgs
$reqArgs = @('-m', 'pip', 'install', '-r', (Join-Path $dir 'requirements.txt'))
if ($IndexUrl) { $reqArgs += @('-i', $IndexUrl) }
& $venvPy @reqArgs
if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }
& $venvPy -c "import fastapi, uvicorn, psutil; print('deps ok')"

# --- 3. services.json ------------------------------------------------------
$cfg = Join-Path $dir 'services.json'
if (Test-Path $cfg) {
  Say 'services.json already exists, keeping it'
} else {
  Step 'Creating services.json from services.example.json'
  Copy-Item (Join-Path $dir 'services.example.json') $cfg
}

# --- 4. scheduled task -----------------------------------------------------
if ($NoTask) {
  Say 'skipping scheduled task (-NoTask)'
} else {
  Step 'Registering ServicePanel scheduled task'
  & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $dir 'register_panel_task.ps1')
  if ($LASTEXITCODE -ne 0) { throw 'task registration failed' }
  Start-ScheduledTask -TaskName 'ServicePanel'
}

# --- 5. verify -------------------------------------------------------------
Step 'Waiting for the panel to answer'
$port = 8899
try {
  $c = Get-Content $cfg -Raw | ConvertFrom-Json
  if ($c.panel.port) { $port = [int]$c.panel.port }
} catch { }
$ok = $false
for ($i = 0; $i -lt 40; $i++) {
  Start-Sleep -Milliseconds 700
  try {
    $r = Invoke-WebRequest -Uri ("http://127.0.0.1:$port/api/ping") -TimeoutSec 2 -UseBasicParsing
    if ($r.StatusCode -eq 200) { $ok = $true; break }
  } catch { }
}
if ($ok) {
  Say ('panel is up: http://127.0.0.1:' + $port)
  if (-not $NoBrowser) { Start-Process ("http://127.0.0.1:$port/") }
} else {
  Say 'panel did not answer in time.'
  Say 'check panel.log, or run run_panel.cmd in a console to see the error'
  exit 1
}
