# 注册 Windows 任务计划程序：每天 22:00 自动跑一次 run_daily.py
# 用法（PowerShell 管理员非必需，仅当前用户任务）：
#   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -Time 08:30

param(
    [string]$Time = "22:00",
    [string]$TaskName = "TreasureRadar-Daily"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $root ".venv\Scripts\python.exe"
$python = if (Test-Path $venvPython) { $venvPython } else { "python" }
$script = Join-Path $root "run_daily.py"

if (-not (Test-Path $script)) { throw "找不到 run_daily.py：$script" }

$action = New-ScheduledTaskAction -Execute $python -Argument "`"$script`"" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 2)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "GitHub 宝藏雷达每日报告" -Force | Out-Null

Write-Host "已注册任务 $TaskName，每天 $Time 运行：$python $script" -ForegroundColor Green
Write-Host "查看：Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo"
