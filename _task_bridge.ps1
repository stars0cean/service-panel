#requires -Version 5.1
# 服务中心 ⇄ 计划任务 桥接脚本
# 用法：
#   -Action query -Names "A,B"   -> 输出 JSON 数组
#   -Action start -Names "A"
#   -Action stop  -Names "A"
param(
  [Parameter(Mandatory=$true)][ValidateSet('query','start','stop')][string]$Action,
  [Parameter(Mandatory=$true)][string]$Names
)

[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
$ErrorActionPreference = 'SilentlyContinue'
$list = @($Names -split ',' | Where-Object { $_.Trim() -ne '' } | ForEach-Object { $_.Trim() })

if ($Action -eq 'query') {
  $res = @()
  foreach ($n in $list) {
    $t = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
    if ($null -eq $t) {
      $res += [pscustomobject]@{ name = $n; exists = $false; state = 'Missing'; last = ''; result = $null }
      continue
    }
    $i = $t | Get-ScheduledTaskInfo -ErrorAction SilentlyContinue
    $last = ''
    if ($null -ne $i -and $null -ne $i.LastRunTime) {
      $last = ([datetime]$i.LastRunTime).ToString('yyyy-MM-dd HH:mm:ss')
    }
    $r = $null
    if ($null -ne $i) { $r = $i.LastTaskResult }
    $res += [pscustomobject]@{
      name   = $n
      exists = $true
      state  = $t.State.ToString()
      last   = $last
      result = $r
    }
  }
  $res | ConvertTo-Json -Compress -Depth 4
  exit 0
}

$errs = @()
foreach ($n in $list) {
  try {
    if ($Action -eq 'start') { Start-ScheduledTask -TaskName $n -ErrorAction Stop }
    else                     { Stop-ScheduledTask  -TaskName $n -ErrorAction Stop }
  } catch {
    $errs += "$n : $($_.Exception.Message)"
  }
}
if ($errs.Count -gt 0) { Write-Output ('ERR ' + ($errs -join ' | ')); exit 1 }
Write-Output 'OK'
exit 0
