#Requires -Version 5.1
<#
.SYNOPSIS
    A股实时看板 · Windows 定时启动任务（工作日 09:15）

.DESCRIPTION
    注册一个 Windows 计划任务：每周一至周五 09:15 自动运行「启动实时看板.ps1」，
    由该脚本拉起看板并用 Edge 打开页面；看板自身会在 15:15 收盘后自动退出归档。

    ⚠ 计划任务只能按“星期”触发，无法识别法定节假日：休市日会照常启动看板。
      遇到长假可先 -Unregister 注销，或临时停用：
        Disable-ScheduledTask -TaskName AShare-Dashboard-0915
      恢复：
        Enable-ScheduledTask  -TaskName AShare-Dashboard-0915

.PARAMETER Unregister
    注销该计划任务。
.PARAMETER TaskName
    任务名称，默认 AShare-Dashboard-0915。
.PARAMETER At
    触发时间 HH:mm，默认 09:15。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File "注册看板定时任务.ps1"

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File "注册看板定时任务.ps1" -Unregister
#>
[CmdletBinding()]
param(
    [switch]$Unregister,
    [string]$TaskName = 'AShare-Dashboard-0915',
    [string]$At = '09:15'
)

$ErrorActionPreference = 'Stop'

if ($Unregister) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "[任务] 已注销：$TaskName"
    } else {
        Write-Host "[任务] 未找到：$TaskName"
    }
    exit 0
}

$launcher = Join-Path $PSScriptRoot '启动实时看板.ps1'
if (-not (Test-Path -LiteralPath $launcher)) {
    Write-Host "[任务] 找不到启动脚本：$launcher"
    exit 1
}

try {
    $triggerTime = [datetime]::ParseExact($At, 'HH:mm', $null)
} catch {
    Write-Host "[任务] 时间格式应为 HH:mm，例如 09:15"
    exit 1
}

# 优先 PowerShell 7（pwsh，原生 UTF-8），回退 Windows PowerShell 5.1
$shell = (Get-Command 'pwsh.exe' -ErrorAction SilentlyContinue).Source
if (-not $shell) { $shell = (Get-Command 'powershell.exe' -ErrorAction SilentlyContinue).Source }
if (-not $shell) {
    Write-Host "[任务] 未找到 PowerShell 可执行文件"
    exit 1
}

$action = New-ScheduledTaskAction -Execute $shell `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$launcher`""

$trigger = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday `
    -At $triggerTime

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 8)

$description = 'A股实时筛选看板：工作日 09:15 自动启动并用 Edge 打开页面，看板 15:15 自行退出归档。计划任务不识别法定节假日。'

Register-ScheduledTask -TaskName $TaskName `
    -Action $action -Trigger $trigger -Settings $settings `
    -Description $description -Force | Out-Null

Write-Host "[任务] 已注册：$TaskName（每周一至周五 $($triggerTime.ToString('HH:mm'))，执行器 $shell）"
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State | Format-List
Get-ScheduledTaskInfo -TaskName $TaskName |
    Select-Object NextRunTime, LastRunTime, LastTaskResult | Format-List
exit 0
