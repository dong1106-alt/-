# 超级智能体：注册计划任务（睡眠自动唤醒 + 开机补跑）
# 用法：右键“以管理员身份运行”本脚本，或：
#   powershell -ExecutionPolicy Bypass -File "本文件路径"
# 可重复执行，已存在的同名任务会被覆盖。
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$pyw = Join-Path $root '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $pyw)) { throw "pythonw not found: $pyw" }

$defs = @(
  @{ Name='超级智能体-信号扫描'; Args="-u scripts/run_once.py daily_signal_scan scripts/daily_signal_scan.py 3600";  Time='15:00'; Kind='weekday' },
  @{ Name='超级智能体-模拟交易'; Args="-u scripts/run_once.py sim_trade_tracker scripts/sim_trade_tracker.py 1800"; Time='15:20'; Kind='weekday' },
  @{ Name='超级智能体-巡检';     Args="-u scripts/run_once.py daily_pipeline_codeact scripts/daily_pipeline_codeact.py 1200"; Time='15:30'; Kind='weekday' },
  @{ Name='超级智能体-每日优化'; Args="-u scripts/run_once.py daily_optimize scripts/daily_optimize.py 1800"; Time='16:30'; Kind='weekday' },
  @{ Name='超级智能体-微信日报'; Args="-u scripts/run_once.py wechat_daily_summary scripts/daily_wechat_summary.py 300"; Time='17:45'; Kind='weekday' },
  @{ Name='超级智能体-月度优化'; Args="-u scripts/run_once.py --only-day 1 monthly_reoptimize scripts/monthly_reoptimize.py 3600"; Time='15:35'; Kind='weekday' },
  @{ Name='超级智能体-开机补跑'; Args='-u scripts/run_once.py --catchup'; Time=$null; Kind='logon' }
)

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 180) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
$wdays = 'Monday','Tuesday','Wednesday','Thursday','Friday'

foreach ($d in $defs) {
  $action = New-ScheduledTaskAction -Execute $pyw -Argument $d.Args -WorkingDirectory $root
  switch ($d.Kind) {
    'weekday' { $trig = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $wdays -At $d.Time }
    'monthly' { $trig = New-ScheduledTaskTrigger -Monthly -DaysOfMonth 1 -At $d.Time }
    'logon'   { $trig = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME" }
  }
  Register-ScheduledTask -TaskName $d.Name -Action $action -Trigger $trig -Settings $settings -Principal $principal -Force | Out-Null
  Write-Output ("registered: " + $d.Name)
}

# 移除旧的常驻自启快捷方式，避免与计划任务重复执行
$lnk = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup\超级智能体调度器.lnk'
if (Test-Path -LiteralPath $lnk) { Remove-Item -LiteralPath $lnk -Force; Write-Output 'removed old startup shortcut' }
Write-Output 'ALL DONE'
Read-Host '按回车键关闭窗口'
