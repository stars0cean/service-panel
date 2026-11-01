#requires -Version 5.1
# 面板自重启：由面板以 DETACHED 方式拉起，不受 Stop-ScheduledTask 影响
Start-Sleep -Seconds 2
try { Stop-ScheduledTask -TaskName 'ServicePanel' -ErrorAction SilentlyContinue } catch { }
Start-Sleep -Seconds 3
try { Start-ScheduledTask -TaskName 'ServicePanel' -ErrorAction SilentlyContinue } catch { }
