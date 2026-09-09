# 卸载 Windows 定时任务
param([string]$TaskName = "TreasureRadar-Daily")

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Write-Host "已卸载任务 $TaskName" -ForegroundColor Yellow
