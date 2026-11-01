#requires -Version 5.1
<#
  Register / update the ServicePanel scheduled task:
    - auto start at logon
    - runs without a console window
    - unlimited run time (it is a daemon)
    - restarts up to 5 times, one minute apart, if it crashes

  The panel calls this script with no arguments from Settings -> System -> autostart,
  so every default must be derived from the script location.
#>
param(
  [string]$TaskName   = 'ServicePanel',
  [string]$ProjectDir = $PSScriptRoot
)

$ErrorActionPreference = 'Stop'

if (-not $ProjectDir) { $ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path }

# --- locate a python interpreter -------------------------------------------
$py = $null
foreach ($c in @("$ProjectDir\.venv\Scripts\python.exe",
                 "$ProjectDir\venv\Scripts\python.exe")) {
  if (Test-Path $c) { $py = $c; break }
}
if (-not $py) {
  $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
  if ($cmd) { $py = $cmd.Source }
}
if (-not $py) { throw "python.exe not found. Create a venv first, or add python to PATH." }
if (-not (Test-Path "$ProjectDir\panel.py")) { throw "panel.py not found in $ProjectDir" }

# --- scheduled task --------------------------------------------------------
$action = New-ScheduledTaskAction -Execute $py `
  -Argument ('-X utf8 "' + $ProjectDir + '\panel.py"') -WorkingDirectory $ProjectDir
$trigger  = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
  -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId ("{0}\{1}" -f $env:USERDOMAIN, $env:USERNAME) `
  -LogonType S4U -RunLevel Highest

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
  -Settings $settings -Principal $principal `
  -Description 'Service Panel: background services / port mappings / DNS records' | Out-Null

Write-Output ('OK ' + $TaskName + ' -> ' + $py)
